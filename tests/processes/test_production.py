from __future__ import annotations

from wish_builder.adapters import FilesystemExternalEvidenceStore

from wish_builder.adapters.fakes import FakeBackendChannelPort, FakeExternalState

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.processes.test_coordinator import (
    BASE_TIME,
    COORDINATOR_ID,
    CoordinatorHarness,
    one_task_manifest,
)
from tests.adapters.test_trellis_graph_import import (
    payload as trellis_payload,
)
from tests.adapters.test_trellis_graph_import import (
    snapshot as trellis_snapshot,
)
from tests.adapters.test_trellis_graph_import import (
    task as trellis_task,
)
from tests.processes.test_production_routing import attempt_worktree
from tests.services.test_gate_b_bootstrap import material as gate_b_material
from wish_builder.adapters.trellis import (
    FakeTrellisGraphPort,
)
from wish_builder.adapters.process_identity import (
    LeaseOwnerProcessProbeResult,
    LeaseOwnerProcessState,
)
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts import WorkerProvider
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.contracts.runtime import (
    EffectOperation,
    EffectStatus,
    ExecutionIdentity,
    JournalEventType,
    RuntimeState,
)
from wish_builder.processes import (
    CoordinatorReason,
    CoordinatorStatus,
    ForegroundCoordinator,
)
from wish_builder.processes import production as production_module
from wish_builder.processes.coordinator import _recovered_cancel_authority_matches
from wish_builder.processes.foreground import PreparedForegroundAttempt, WorkerBatchResult
from wish_builder.processes.production import (
    DeterministicBackendDispatchPlanFactory,
    ProductionForegroundRunComponents,
    ProductionRuntimeConfig,
    ProductionRuntimeLayout,
    channel_capabilities_from_compatibility,
)
from wish_builder.processes.workflow import LocalExecutionWorkflow
from wish_builder.services.backend_admission import current_platform
from wish_builder.services.execution_admission import (
    ExecutionAdmissionReason,
    ExecutionAdmissionResult,
)
from wish_builder.services.execution_checkpoints import (
    ExecutionCheckpointReason,
    ExecutionCheckpointStatus,
)
from wish_builder.services.gate_b_bootstrap import bootstrap_gate_b
from wish_builder.services.ports import BackendCapabilities, TurnObservation, TurnState
from wish_builder.services.promotion import PromotionRecord
from wish_builder.services.backend_effects import (
    BackendDispatchEffectCrash,
    BackendDispatchEffectService,
)


def channel_capabilities(manifest, *, max_task_packet_bytes: int = 1_048_576):
    return BackendCapabilities(
        provider=manifest.provider,
        platform="windows",
        capability_digest=manifest.capability_digest,
        launch_profile_digest=manifest.launch_profile_digest,
        policy_digest=manifest.policy_digest,
        max_task_packet_bytes=max_task_packet_bytes,
    )


def one_task_graph_snapshot():
    value = trellis_payload()
    value["requirements"] = [value["requirements"][0]]
    value["tasks"] = [trellis_task("trellis/only", "REQ-001")]
    return trellis_snapshot(value)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout.decode("utf-8", errors="strict").strip()


def initialize_repository(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Wish Builder Tests")
    git(path, "config", "user.email", "tests@wish-builder.invalid")
    git(path, "config", "core.autocrlf", "false")
    source = path / "src" / "req-001" / "base.txt"
    source.parent.mkdir(parents=True)
    source.write_text("base\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")


class IncrementingAuthorityClock:
    def __init__(self) -> None:
        self._value = BASE_TIME
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            value = self._value
            self._value += timedelta(microseconds=1)
            return value


class ProductionRuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = one_task_manifest()
        self.repository = self.root / "missing-repository"
        self.state_root = self.root / "missing-state"

    def layout(self, manifest=None) -> ProductionRuntimeLayout:
        selected = self.manifest if manifest is None else manifest
        return ProductionRuntimeLayout.for_run(
            self.repository,
            self.state_root,
            selected.run_id,
        )

    def test_takeover_cancel_route_resolution_requires_one_older_lineage(self) -> None:
        operation = production_module.AttemptOperationRoute(
            ExecutionIdentity(
                self.manifest.run_id,
                3,
                self.manifest.tasks[0].id,
                1,
                "CANCEL-ROUTE-001",
            ),
            EffectOperation.CANCEL_TURN,
            "sha256:" + "a" * 64,
        )
        exact = (self.manifest.run_id, 3, self.manifest.tasks[0].id, 1)
        older = (self.manifest.run_id, 2, self.manifest.tasks[0].id, 1)

        self.assertEqual(
            exact,
            production_module._recovery_route_key(operation, {exact, older}),
        )
        self.assertEqual(
            older,
            production_module._recovery_route_key(operation, {older}),
        )
        rejected = (
            set(),
            {
                (self.manifest.run_id, 1, self.manifest.tasks[0].id, 1),
                older,
            },
            {(self.manifest.run_id, 4, self.manifest.tasks[0].id, 1)},
            {("WISH-FOREIGN", 2, self.manifest.tasks[0].id, 1)},
            {(self.manifest.run_id, 2, "TASK-FOREIGN", 1)},
        )
        for projected in rejected:
            with self.subTest(projected=projected):
                with self.assertRaisesRegex(ValueError, "one older attempt"):
                    production_module._recovery_route_key(operation, projected)

    def config(
        self,
        *,
        manifest=None,
        capabilities=None,
    ) -> ProductionRuntimeConfig:
        selected = self.manifest if manifest is None else manifest
        selected_capabilities = (
            channel_capabilities(selected)
            if capabilities is None
            else capabilities
        )
        return ProductionRuntimeConfig(
            selected,
            self.layout(selected),
            selected_capabilities,
            "wish-builder-test",
        )

    def identity(self, *, attempt: int = 1, dispatch: str = "DISPATCH-001"):
        return ExecutionIdentity(
            self.manifest.run_id,
            1,
            self.manifest.tasks[0].id,
            attempt,
            dispatch,
        )

    def test_layout_derivation_is_deterministic_and_performs_no_io(self) -> None:
        self.assertFalse(self.repository.exists())
        self.assertFalse(self.state_root.exists())

        first = self.layout()
        second = self.layout()

        self.assertEqual(first, second)
        self.assertTrue(first.repository.is_absolute())
        self.assertTrue(first.journal_root.is_relative_to(first.control_root))
        self.assertTrue(first.evidence_root.is_relative_to(first.control_root))
        self.assertTrue(first.checkpoint_root.is_relative_to(first.control_root))
        self.assertTrue(first.attempts_root.is_relative_to(first.run_root))
        self.assertFalse(first.attempts_root.is_relative_to(first.control_root))
        self.assertFalse(self.repository.exists())
        self.assertFalse(self.state_root.exists())

    def test_layout_rejects_repository_overlap_and_protected_root_aliases(self) -> None:
        with self.assertRaisesRegex(ValueError, "disjoint"):
            ProductionRuntimeLayout.for_run(
                self.repository,
                self.repository / "state",
                self.manifest.run_id,
            )

        baseline = self.layout()
        with self.assertRaisesRegex(ValueError, "must be disjoint"):
            ProductionRuntimeLayout(
                baseline.run_id,
                baseline.repository,
                baseline.run_root,
                baseline.control_root,
                baseline.journal_root,
                baseline.journal_root,
                baseline.checkpoint_root,
                baseline.attempts_root,
            )

    def test_layout_rejects_unsafe_path_inputs_and_every_boundary_escape(self) -> None:
        baseline = self.layout()
        root = Path(self.root.anchor)

        class BytesPath(os.PathLike):
            def __fspath__(self):
                return b"not-a-text-path"

        invalid_for_run = (
            (object(), self.state_root, self.manifest.run_id, TypeError, "path"),
            (BytesPath(), self.state_root, self.manifest.run_id, ValueError, "non-empty"),
            ("relative-repository", self.state_root, self.manifest.run_id, ValueError, "absolute"),
            (root, self.state_root, self.manifest.run_id, ValueError, "filesystem root"),
            (self.repository, self.state_root, "", ValueError, "run_id"),
        )
        for repository, state_root, run_id, error, message in invalid_for_run:
            with self.subTest(repository=repository, run_id=run_id):
                with self.assertRaisesRegex(error, message):
                    ProductionRuntimeLayout.for_run(repository, state_root, run_id)

        invalid_layouts = (
            ({"run_id": " "}, "run_id"),
            ({"control_root": self.root / "outside-control"}, "control_root"),
            ({"attempts_root": self.root / "outside-attempts"}, "attempts_root"),
            ({"attempts_root": baseline.control_root}, "disjoint"),
            ({"journal_root": baseline.run_root / "journal"}, "inside control_root"),
            ({"checkpoint_root": baseline.journal_root / "nested"}, "disjoint"),
        )
        for changes, message in invalid_layouts:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, message):
                    dataclasses.replace(baseline, **changes)

    def test_compatibility_projection_preserves_the_pinned_channel_cell(self) -> None:
        bundle = load_bundled_compatibility()
        cell = bundle.platform(Provider.OMP, Platform.LINUX)

        projected = channel_capabilities_from_compatibility(cell)

        self.assertIs(projected.provider, WorkerProvider.OH_MY_PI)
        self.assertEqual("linux", projected.platform)
        self.assertEqual(
            cell.capabilities.capability_digest,
            projected.capability_digest,
        )
        self.assertEqual(cell.launch_profile_digest, projected.launch_profile_digest)
        self.assertEqual(cell.capabilities.policy_digest, projected.policy_digest)
        self.assertTrue(projected.caller_supplied_ids)
        self.assertTrue(projected.idempotent_operations)
        self.assertTrue(projected.inspect_operations)
        self.assertTrue(projected.fresh_session_per_attempt)

        with self.assertRaisesRegex(TypeError, "PlatformCompatibility"):
            channel_capabilities_from_compatibility(object())

    def test_runtime_config_is_inert_and_binds_manifest_capabilities(self) -> None:
        executable = self.root / "not-installed" / "node.exe"
        bridge = self.root / "not-installed" / "bridge.mjs"
        with mock.patch.object(
            Path,
            "is_file",
            side_effect=AssertionError("config must not inspect bridge files"),
        ):
            config = ProductionRuntimeConfig(
                self.manifest,
                self.layout(),
                channel_capabilities(self.manifest),
                "wish-builder-test",
                (str(executable), str(bridge)),
            )

        self.assertEqual((str(executable), str(bridge)), config.bridge_command)
        self.assertFalse(executable.parent.exists())

        mismatched_provider = dataclasses.replace(
            config.channel_capabilities,
            provider=WorkerProvider.PI,
        )
        with self.assertRaisesRegex(ValueError, "provider"):
            self.config(capabilities=mismatched_provider)

        mismatched_digest = dataclasses.replace(
            config.channel_capabilities,
            capability_digest="sha256:" + "f" * 64,
        )
        with self.assertRaisesRegex(ValueError, "capability_digest"):
            self.config(capabilities=mismatched_digest)

        missing_guarantee = dataclasses.replace(
            config.channel_capabilities,
            inspect_operations=False,
        )
        with self.assertRaisesRegex(ValueError, "dispatch guarantees"):
            self.config(capabilities=missing_guarantee)

    def test_runtime_config_rejects_mismatched_types_tokens_and_capabilities(self) -> None:
        layout = self.layout()
        capabilities = channel_capabilities(self.manifest)
        valid = self.config()

        invalid_constructor_values = (
            ((object(), layout, capabilities, "project"), TypeError, "manifest"),
            ((self.manifest, object(), capabilities, "project"), TypeError, "layout"),
            ((self.manifest, layout, object(), "project"), TypeError, "channel_capabilities"),
            (
                (
                    self.manifest,
                    dataclasses.replace(layout, run_id="different-run"),
                    capabilities,
                    "project",
                ),
                ValueError,
                "run_id",
            ),
        )
        for arguments, error, message in invalid_constructor_values:
            with self.subTest(message=message):
                with self.assertRaisesRegex(error, message):
                    ProductionRuntimeConfig(*arguments)

        for project_key in (None, "", ".", "..", "-prefix", "has space", "x" * 129):
            with self.subTest(project_key=project_key):
                with self.assertRaisesRegex(ValueError, "project_key"):
                    dataclasses.replace(valid, project_key=project_key)

        invalid_capabilities = (
            (dataclasses.replace(capabilities, platform="darwin"), "platform"),
            (
                dataclasses.replace(
                    capabilities,
                    max_task_packet_bytes=production_module.MAX_TASK_PACKET_BYTES + 1,
                ),
                "packet limit",
            ),
        )
        for field_name in ("capability_digest", "launch_profile_digest", "policy_digest"):
            invalid_capabilities += (
                (
                    dataclasses.replace(
                        capabilities,
                        **{field_name: "sha256:" + "e" * 64},
                    ),
                    field_name,
                ),
            )
        for field_name in (
            "caller_supplied_ids",
            "idempotent_operations",
            "inspect_operations",
            "fresh_session_per_attempt",
        ):
            invalid_capabilities += (
                (dataclasses.replace(capabilities, **{field_name: False}), "guarantees"),
            )
        for candidate, message in invalid_capabilities:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.config(capabilities=candidate)

    def test_runtime_config_rejects_ambiguous_bridge_and_clock_values(self) -> None:
        valid = self.config()
        executable = str((self.root / "node.exe").absolute())
        bridge = str((self.root / "bridge.mjs").absolute())
        invalid_commands = (
            [executable, bridge],
            (),
            (executable,),
            (executable, bridge, bridge),
            ("relative-node", bridge),
            (executable, ""),
            (executable, "bad\x00bridge"),
            (executable, object()),
        )
        for command in invalid_commands:
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "bridge_command"):
                    dataclasses.replace(valid, bridge_command=command)

        for field_name in ("worker_timeout_seconds", "poll_interval_seconds"):
            for value in (True, "1", 0, -1, float("nan"), float("inf")):
                with self.subTest(field_name=field_name, value=value):
                    with self.assertRaisesRegex(ValueError, field_name):
                        dataclasses.replace(valid, **{field_name: value})
        with self.assertRaisesRegex(ValueError, "poll interval"):
            dataclasses.replace(
                valid,
                worker_timeout_seconds=1,
                poll_interval_seconds=2,
            )

    def test_compatibility_config_factory_normalizes_sequences_and_rejects_text(self) -> None:
        bundle = load_bundled_compatibility()
        cell = bundle.platform(Provider.CODEX, Platform.WINDOWS)
        manifest = dataclasses.replace(
            self.manifest,
            capability_digest=cell.capabilities.capability_digest,
            launch_profile_digest=cell.launch_profile_digest,
            policy_digest=cell.capabilities.policy_digest,
        )
        executable = str((self.root / "node.exe").absolute())
        bridge = str((self.root / "bridge.mjs").absolute())

        config = ProductionRuntimeConfig.from_compatibility_cell(
            manifest,
            self.layout(manifest),
            cell,
            project_key="compatibility-factory",
            bridge_command=[executable, bridge],
            worker_timeout_seconds=12,
            poll_interval_seconds=2,
        )

        self.assertEqual((executable, bridge), config.bridge_command)
        self.assertEqual(12.0, config.worker_timeout_seconds)
        without_bridge = ProductionRuntimeConfig.from_compatibility_cell(
            manifest,
            self.layout(manifest),
            cell,
            project_key="compatibility-with-injected-channel",
        )
        self.assertIsNone(without_bridge.bridge_command)
        with self.assertRaisesRegex(TypeError, "sequence of paths"):
            ProductionRuntimeConfig.from_compatibility_cell(
                manifest,
                self.layout(manifest),
                cell,
                project_key="compatibility-factory",
                bridge_command=executable,
            )

    def test_dispatch_plan_and_packet_are_exactly_reproducible(self) -> None:
        factory = DeterministicBackendDispatchPlanFactory(self.config())
        identity = self.identity()

        first = factory(identity)
        second = factory(identity)

        self.assertEqual(first, second)
        self.assertEqual(identity.correlation_id, first.reserve.dispatch_id)
        self.assertEqual(identity.correlation_id, first.send.dispatch_id)
        self.assertEqual(first.reserve.attempt_id, first.send.attempt_id)
        self.assertEqual(first.reserve.channel_id, first.send.channel_id)
        self.assertEqual(
            "sha256:"
            + hashlib.sha256(first.send.task_packet.encode("utf-8")).hexdigest(),
            first.send.task_packet_digest,
        )

        packet = json.loads(first.send.task_packet)
        self.assertEqual("wish_builder_task_packet", packet["kind"])
        self.assertEqual(1, packet["schema_version"])
        self.assertEqual(identity.to_primitive(), packet["execution"]["identity"])
        self.assertEqual(
            self.manifest.canonical_sha256(),
            packet["execution"]["manifest_digest"],
        )
        self.assertEqual(
            self.manifest.task_id_mapping[0].trellis_task_id,
            packet["task"]["trellis_task_id"],
        )
        self.assertEqual(
            list(self.manifest.tasks[0].owned_paths),
            packet["task"]["owned_paths"],
        )
        self.assertEqual(4, len(packet["instructions"]))

        next_attempt = factory(self.identity(attempt=2, dispatch="DISPATCH-002"))
        self.assertNotEqual(first.reserve.operation_id, next_attempt.reserve.operation_id)
        self.assertNotEqual(first.send.turn_id, next_attempt.send.turn_id)
        self.assertNotEqual(first.send.task_packet, next_attempt.send.task_packet)

    def test_generated_plan_dispatches_only_through_an_injected_fake_channel(self) -> None:
        harness = CoordinatorHarness(self.root / "coordinator")
        manifest = harness.manifest
        capabilities = channel_capabilities(manifest)
        config = ProductionRuntimeConfig(
            manifest,
            ProductionRuntimeLayout.for_run(
                self.root / "repository-placeholder",
                self.root / "runtime-placeholder",
                manifest.run_id,
            ),
            capabilities,
            "wish-builder-fake-channel",
        )
        channel = FakeBackendChannelPort(capabilities)
        effects = BackendDispatchEffectService(
            harness.journal,
            channel,
            FilesystemExternalEvidenceStore(self.root / "fake-evidence"),
            coordinator_id=COORDINATOR_ID,
            fencing_token=1,
        )
        coordinator = ForegroundCoordinator(
            manifest,
            harness.coordinator.cursor,
            harness.journal,
            None,
            backend_effects=effects,
            backend_plan_factory=DeterministicBackendDispatchPlanFactory(config),
            coordinator_id=COORDINATOR_ID,
            owner=harness.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME,
        )

        reserved = coordinator.reserve_ready(limit=1)
        self.assertIs(reserved.status, CoordinatorStatus.PROGRESSED)
        dispatched = coordinator.dispatch_reserved(reserved.reserved[0])

        self.assertIs(dispatched.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual(reserved.reserved, dispatched.dispatched)
        self.assertEqual(2, channel.effect_count)

    def test_factory_rejects_wrong_identity_and_capability_packet_limit(self) -> None:
        factory = DeterministicBackendDispatchPlanFactory(self.config())
        with self.assertRaisesRegex(ValueError, "run_id"):
            factory(
                ExecutionIdentity(
                    "another-run",
                    1,
                    self.manifest.tasks[0].id,
                    1,
                    "DISPATCH-001",
                )
            )
        with self.assertRaisesRegex(ValueError, "task_id"):
            factory(
                ExecutionIdentity(
                    self.manifest.run_id,
                    1,
                    "TASK-999",
                    1,
                    "DISPATCH-999",
                )
            )

        constrained = self.config(
            capabilities=channel_capabilities(
                self.manifest,
                max_task_packet_bytes=256,
            )
        )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            DeterministicBackendDispatchPlanFactory(constrained)(self.identity())

    def test_approved_template_must_be_supplied_with_exact_bytes(self) -> None:
        packet = "Implement the exact approved Trellis task.\n"
        digest = "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()
        task = dataclasses.replace(
            self.manifest.tasks[0],
            instruction_context_digest=None,
            approved_document_digests=(),
            task_packet_template_digest=digest,
        )
        manifest = dataclasses.replace(self.manifest, tasks=(task,))
        identity = ExecutionIdentity(
            manifest.run_id,
            1,
            task.id,
            1,
            "DISPATCH-TEMPLATE-001",
        )
        config = self.config(manifest=manifest)

        with self.assertRaisesRegex(ValueError, "template is unavailable"):
            DeterministicBackendDispatchPlanFactory(config)(identity)
        with self.assertRaisesRegex(ValueError, "do not match"):
            DeterministicBackendDispatchPlanFactory(
                config,
                task_packet_templates={digest: "changed packet\n"},
            )

        plan = DeterministicBackendDispatchPlanFactory(
            config,
            task_packet_templates={digest: packet},
        )(identity)
        self.assertEqual(packet, plan.send.task_packet)
        self.assertEqual(digest, plan.send.task_packet_digest)

    def test_plan_factory_rejects_invalid_configuration_and_template_maps(self) -> None:
        config = self.config()
        digest = "sha256:" + hashlib.sha256(b"packet\n").hexdigest()
        invalid_inputs = (
            ((object(),), {}, TypeError, "config"),
            ((config,), {"task_packet_templates": []}, TypeError, "mapping"),
            ((config,), {"task_packet_templates": {"bad": "packet"}}, ValueError, "sha256"),
            (
                (config,),
                {"task_packet_templates": {digest: b"packet\n"}},
                TypeError,
                "strings",
            ),
        )
        for arguments, keywords, error, message in invalid_inputs:
            with self.subTest(message=message):
                with self.assertRaisesRegex(error, message):
                    DeterministicBackendDispatchPlanFactory(*arguments, **keywords)

    def test_task_packet_api_normalizes_approved_text_and_validates_identity(self) -> None:
        normalized = "caf\N{LATIN SMALL LETTER E WITH ACUTE}\nline two\n"
        unnormalized = "cafe\N{COMBINING ACUTE ACCENT}\r\nline two\r"
        digest = "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        task = dataclasses.replace(
            self.manifest.tasks[0],
            instruction_context_digest=None,
            approved_document_digests=(),
            task_packet_template_digest=digest,
        )
        manifest = dataclasses.replace(self.manifest, tasks=(task,))
        config = self.config(manifest=manifest)
        identity = ExecutionIdentity(
            manifest.run_id,
            1,
            task.id,
            1,
            "DISPATCH-TEMPLATE-NORMALIZED",
        )
        factory = DeterministicBackendDispatchPlanFactory(
            config,
            task_packet_templates={digest: unnormalized},
        )

        self.assertEqual(normalized, factory.task_packet(identity))
        with self.assertRaisesRegex(ValueError, "complete attempt"):
            factory.task_packet(ExecutionIdentity(manifest.run_id, 1))
        with self.assertRaisesRegex(ValueError, "run_id"):
            factory.task_packet(
                ExecutionIdentity("different-run", 1, task.id, 1, "DISPATCH-WRONG-RUN")
            )
        with self.assertRaisesRegex(ValueError, "task_id"):
            factory.task_packet(
                ExecutionIdentity(
                    manifest.run_id,
                    1,
                    "TASK-NOT-PRESENT",
                    1,
                    "DISPATCH-WRONG-TASK",
                )
            )
        with self.assertRaisesRegex(ValueError, "complete attempt"):
            factory(ExecutionIdentity(manifest.run_id, 1))


class ProductionHostBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = one_task_manifest()
        self.layout = ProductionRuntimeLayout.for_run(
            self.root / "repository",
            self.root / "runtime",
            self.manifest.run_id,
        )

    def test_bridge_environment_preserves_only_provider_and_normalized_trellis_paths(
        self,
    ) -> None:
        relative_core = Path("vendor") / "trellis-core"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "PATH": "provider-path",
                    "UNRELATED_SECRET": "must-not-leak",
                    "WISH_BUILDER_TRELLIS_CORE_ROOT": str(relative_core),
                    "WISH_BUILDER_TRELLIS_CORE_MODULE": "file:../trellis-core/index.js",
                    "WISH_BUILDER_TRELLIS_CLI_ARCHIVE": str(
                        (self.root / "cli.tgz").absolute()
                    ),
                    "WISH_BUILDER_TRELLIS_CLI_ROOT": "",
                },
                clear=True,
            ),
            mock.patch.object(Path, "cwd", return_value=self.root),
        ):
            environment = production_module._bridge_environment(self.layout)

        self.assertEqual("provider-path", environment["PATH"])
        self.assertNotIn("UNRELATED_SECRET", environment)
        self.assertEqual(
            str((self.root / relative_core).absolute()),
            environment["WISH_BUILDER_TRELLIS_CORE_ROOT"],
        )
        self.assertEqual(
            "file:../trellis-core/index.js",
            environment["WISH_BUILDER_TRELLIS_CORE_MODULE"],
        )
        self.assertEqual(
            str((self.root / "cli.tgz").absolute()),
            environment["WISH_BUILDER_TRELLIS_CLI_ARCHIVE"],
        )
        self.assertNotIn("WISH_BUILDER_TRELLIS_CLI_ROOT", environment)
        self.assertNotIn("BACKEND_CHANNEL_ROOT", environment)

    def test_explicit_trellis_core_pin_overrides_environment(self) -> None:
        environment_root = self.root / "environment-core"
        environment_root.mkdir()
        explicit_root = self.root / "explicit-core"
        explicit_root.mkdir()
        environment_archive = self.root / "environment-core.tgz"
        environment_archive.write_bytes(b"environment")
        explicit_archive = self.root / "explicit-core.tgz"
        explicit_archive.write_bytes(b"explicit")

        with mock.patch.dict(
            os.environ,
            {
                "WISH_BUILDER_TRELLIS_CORE_ROOT": str(environment_root),
                "WISH_BUILDER_TRELLIS_CORE_ARCHIVE": str(environment_archive),
            },
            clear=True,
        ):
            environment = production_module._bridge_environment(
                self.layout,
                trellis_core_root=explicit_root,
                trellis_core_archive=explicit_archive,
            )

        self.assertEqual(
            str(explicit_root.resolve(strict=True)),
            environment["WISH_BUILDER_TRELLIS_CORE_ROOT"],
        )
        self.assertEqual(
            str(explicit_archive.resolve(strict=True)),
            environment["WISH_BUILDER_TRELLIS_CORE_ARCHIVE"],
        )

    def test_host_id_is_sanitized_bounded_and_has_a_stable_fallback(self) -> None:
        with mock.patch.object(
            production_module.host_platform,
            "node",
            return_value=" ./unsafe host!?/" + "x" * 200,
        ):
            host_id = production_module._safe_host_id()
        self.assertLessEqual(len(host_id), 128)
        self.assertNotIn(" ", host_id)
        self.assertFalse(host_id.startswith((".", "/", "-")))

        with mock.patch.object(production_module.host_platform, "node", return_value=".../"):
            self.assertEqual("localhost", production_module._safe_host_id())

    def test_compatibility_lookup_rejects_unsupported_hosts_and_policy_drift(self) -> None:
        with mock.patch.object(
            production_module,
            "current_platform",
            return_value=mock.sentinel.unsupported_platform,
        ):
            with self.assertRaisesRegex(RuntimeError, "outside the M1"):
                production_module._compatibility_cell(self.manifest)

        with self.assertRaisesRegex(ValueError, "policy digest"):
            production_module._compatibility_cell(self.manifest)

        bundle = load_bundled_compatibility()
        platform = current_platform()
        cell = bundle.platform(Provider.CODEX, platform)
        matching = dataclasses.replace(
            self.manifest,
            policy_digest=bundle.policy_digest,
        )
        self.assertEqual(cell, production_module._compatibility_cell(matching))

    def test_bridge_command_fails_closed_without_node_or_runtime_files(self) -> None:
        with mock.patch.object(production_module.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "Node.js"):
                production_module._bridge_command()

        fake_node = self.root / "node.exe"
        fake_node.write_text("not executable", encoding="utf-8")
        with (
            mock.patch.object(
                production_module.shutil,
                "which",
                return_value=str(fake_node),
            ),
            mock.patch.object(Path, "is_file", return_value=False),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "runtime is unavailable"):
                production_module._bridge_command()

        with mock.patch.object(
            production_module.shutil,
            "which",
            return_value=str(fake_node),
        ):
            node, bridge = production_module._bridge_command()
        self.assertEqual(fake_node.resolve(), Path(node))
        self.assertTrue(Path(bridge).is_file())


class ProductionForegroundCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.runtime_root = self.root / "runtime"
        initialize_repository(self.repository)
        bundle = load_bundled_compatibility()
        self.cell = bundle.platform(Provider.CODEX, current_platform())
        self.manifest = dataclasses.replace(
            one_task_manifest(),
            capability_digest=self.cell.capabilities.capability_digest,
            launch_profile_digest=self.cell.launch_profile_digest,
            policy_digest=self.cell.capabilities.policy_digest,
        )
        self.authority_clock = IncrementingAuthorityClock()

    def components(self) -> ProductionForegroundRunComponents:
        command = (
            str((self.root / "node.exe").absolute()),
            str((self.root / "bridge.mjs").absolute()),
        )
        with (
            mock.patch(
                "wish_builder.processes.production._compatibility_cell",
                return_value=self.cell,
            ),
            mock.patch(
                "wish_builder.processes.production._bridge_command",
                return_value=command,
            ),
            mock.patch.object(
                production_module,
                "TrellisCoreGraphPort",
                return_value=FakeTrellisGraphPort(one_task_graph_snapshot()),
            ),
        ):
            built = ProductionForegroundRunComponents.from_runtime_inputs(
                self.manifest,
                runtime_root=self.runtime_root,
                workspace_root=self.repository,
                authority_clock=self.authority_clock,
            )
        self.addCleanup(built.close)
        return built

    def acquired_components(
        self,
    ) -> tuple[ProductionForegroundRunComponents, object]:
        built = self.components()
        recovered = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        acquired = built.acquire_lease(recovered)
        self.assertIsNotNone(acquired)
        return built, acquired

    def running_attempt_takeover(
        self,
    ) -> tuple[
        ProductionForegroundRunComponents,
        object,
        FakeExternalState,
        list[str],
        ExecutionIdentity,
        str,
    ]:
        state = FakeExternalState()
        cancel_calls: list[str] = []

        class CountingBackendChannel(FakeBackendChannelPort):
            def cancel(self, effect):
                cancel_calls.append(effect.command.operation_id)
                return super().cancel(effect)

        first = self.components()
        capabilities = first._config.channel_capabilities
        bootstrapped = bootstrap_gate_b(
            gate_b_material(
                self.manifest,
                workspace_hash=first._workspace.workspace_hash,
            ),
            (),
            first._journal,
        )
        self.assertTrue(bootstrapped.admitted)

        def channel_factory(_attempt):
            return CountingBackendChannel(
                capabilities,
                state=state,
                send_state=TurnState.RUNNING,
            )

        first._channel_factory = channel_factory
        initial = first.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(initial)
        active = first.acquire_lease(initial)
        self.assertIsNotNone(active)
        reserved = first.coordinator(active).reserve_ready()
        identity = reserved.reserved[0]
        prepared = first.workflow(reserved.cursor).prepare_attempt(identity)
        self.assertIsNotNone(prepared.attempt)
        lifecycle = production_module._LifecycleProjection(
            True,
            CoordinatorReason.NONE,
            (),
        )
        with (
            mock.patch.object(
                first,
                "_project_prepare_lifecycle",
                return_value=lifecycle,
            ),
            mock.patch.object(
                first,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
        ):
            dispatched = first.coordinator(prepared.cursor).dispatch_reserved(identity)
        self.assertIs(CoordinatorStatus.PROGRESSED, dispatched.status)
        first.close()

        self.authority_clock._value += timedelta(seconds=1_000)
        with mock.patch.object(
            production_module,
            "capture_process_start_id",
            return_value="test-takeover-helper",
        ):
            second = self.components()
        second._lease_service._prior_owner_process_probe = lambda *args, **kwargs: (
            LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
        )
        second._channel_factory = channel_factory
        recovered = second.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        cancel_suffix = production_module.canonical_sha256(
            {
                "fencing_token": 2,
                "identity": identity.to_primitive(),
                "operation": EffectOperation.CANCEL_TURN.value,
            }
        )[:48].upper()
        return (
            second,
            recovered,
            state,
            cancel_calls,
            identity,
            f"CANCEL-{cancel_suffix}",
        )

    def test_real_git_repository_builds_an_inert_production_composition(self) -> None:
        built = self.components()

        self.assertTrue(built.protect_control_root())
        self.assertTrue(built.verify_workspace_identity(self.manifest))
        self.assertIsNotNone(built.recover_verified_cursor(self.manifest))
        self.assertTrue(self.runtime_root.is_dir())

    def test_composition_keeps_live_projection_and_gate_clean_git_identity(self) -> None:
        task_file = (
            self.repository / ".trellis" / "tasks" / "task-a" / "task.json"
        )
        task_file.parent.mkdir(parents=True)
        task_file.write_text('{"status":"planning"}\n', encoding="utf-8")
        git(self.repository, "add", ".trellis/tasks/task-a/task.json")
        git(self.repository, "commit", "-m", "add Trellis task")
        self.manifest = dataclasses.replace(
            self.manifest,
            protected_paths=tuple(
                sorted({*self.manifest.protected_paths, ".trellis/tasks/**"})
            ),
        )
        gate_workspace = production_module.capture_workspace_identity(
            self.repository,
            production_module._workspace_scopes(self.manifest),
        )
        task_file.write_text('{"status":"in_progress"}\n', encoding="utf-8")

        built = self.components()

        self.assertNotEqual(gate_workspace, built._workspace)
        self.assertEqual(gate_workspace, built._repository.expected_workspace)
        self.assertEqual(
            built._workspace.workspace_hash,
            built._owner.workspace_hash,
        )

    def test_factory_rejects_invalid_inputs_before_host_or_runtime_effects(self) -> None:
        with mock.patch.object(
            production_module,
            "_compatibility_cell",
            side_effect=AssertionError("host lookup must remain lazy"),
        ):
            with self.assertRaisesRegex(TypeError, "manifest"):
                ProductionForegroundRunComponents.from_runtime_inputs(
                    object(),
                    runtime_root=self.runtime_root,
                    workspace_root=self.repository,
                )
            with self.assertRaisesRegex(ValueError, "runtime_root"):
                ProductionForegroundRunComponents.from_runtime_inputs(
                    self.manifest,
                    runtime_root=None,
                    workspace_root=self.repository,
                )
            with self.assertRaisesRegex(TypeError, "authority_clock"):
                ProductionForegroundRunComponents.from_runtime_inputs(
                    self.manifest,
                    runtime_root=self.runtime_root,
                    workspace_root=self.repository,
                    authority_clock=object(),
                )
        self.assertFalse(self.runtime_root.exists())

    def test_factory_shares_the_injected_authority_clock(self) -> None:
        built = self.components()

        self.assertIs(self.authority_clock, built._authority_clock)
        self.assertIs(
            self.authority_clock,
            built._journal._storage._authority_clock,
        )

    def test_cancel_effect_rechecks_lease_after_request_append(self) -> None:
        built, active = self.acquired_components()
        effects = built.coordinator(active)._backend_effects
        self.assertIsInstance(effects, BackendDispatchEffectService)
        identity = ExecutionIdentity(
            self.manifest.run_id,
            active.snapshot.coordinator_epoch,
            self.manifest.tasks[0].id,
            1,
            "CANCEL-" + "A" * 48,
        )
        self.authority_clock._value += timedelta(seconds=1_000)

        self.assertFalse(effects._effect_admitter(active.head, identity))

    def test_takeover_rejects_owned_path_write_during_cancellation(self) -> None:
        second, recovered, _, cancel_calls, identity, cancel_operation_id = (
            self.running_attempt_takeover()
        )
        path_observations = iter(((), ("src/req-001/late.txt",)))

        with (
            mock.patch.object(
                second,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
            mock.patch.object(second, "_retry_admitted", return_value=True),
            mock.patch.object(
                second._repository,
                "inspect_owned_path_changes",
                side_effect=lambda _command: next(path_observations),
            ) as inspected,
        ):
            active = second.acquire_lease(recovered)

        self.assertIsNone(active)
        self.assertEqual(2, inspected.call_count)
        self.assertEqual([cancel_operation_id], cancel_calls)
        replay = second._recover().replay.snapshot
        self.assertEqual(1, len(replay.attempts))
        self.assertEqual(identity.attempt, replay.attempts[0].attempt)
        self.assertIs(RuntimeState.RUNNING, replay.attempts[0].state)

    def test_takeover_does_not_cancel_after_lease_expires_post_request(self) -> None:
        second, recovered, _, cancel_calls, _, cancel_operation_id = (
            self.running_attempt_takeover()
        )
        original_trigger = BackendDispatchEffectService._trigger

        def expire_after_request(service, point, operation_id):
            if point == "after_request_append" and operation_id == cancel_operation_id:
                self.authority_clock._value += timedelta(seconds=1_000)
            return original_trigger(service, point, operation_id)

        with (
            mock.patch.object(
                second,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
            mock.patch.object(second, "_retry_admitted", return_value=True),
            mock.patch.object(
                BackendDispatchEffectService,
                "_trigger",
                new=expire_after_request,
            ),
        ):
            active = second.acquire_lease(recovered)

        self.assertIsNone(active)
        self.assertEqual([], cancel_calls)
        pending = tuple(
            item
            for item in second._recover().pending_external_effects
            if item.operation is EffectOperation.CANCEL_TURN
        )
        self.assertEqual(1, len(pending))
        self.assertEqual(cancel_operation_id, pending[0].operation_id)

    def test_factory_closes_protected_control_handle_when_late_composition_fails(
        self,
    ) -> None:
        command = (
            str((self.root / "node.exe").absolute()),
            str((self.root / "bridge.mjs").absolute()),
        )
        closed = []
        original_close = production_module.ProtectedControlRoot.close

        def recording_close(handle):
            closed.append(handle)
            return original_close(handle)

        with (
            mock.patch.object(
                production_module,
                "_compatibility_cell",
                return_value=self.cell,
            ),
            mock.patch.object(
                production_module,
                "_bridge_command",
                return_value=command,
            ),
            mock.patch.object(
                production_module,
                "WishBuilderBackendAttemptChannelFactory",
                side_effect=RuntimeError("late channel construction failure"),
            ) as channel_constructor,
            mock.patch.object(
                production_module.ProtectedControlRoot,
                "close",
                autospec=True,
                side_effect=recording_close,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "late channel"):
                ProductionForegroundRunComponents.from_runtime_inputs(
                    self.manifest,
                    runtime_root=self.runtime_root,
                    workspace_root=self.repository,
                )

        channel_constructor.assert_called_once_with(compatibility_cell=self.cell)
        self.assertEqual(1, len(closed))
        self.assertTrue(closed[0].closed)

    def test_constructor_rejects_each_invalid_composition_dependency(self) -> None:
        built = self.components()
        base_arguments = [
            built._config,
            built._workspace,
            built._control_root,
            built._journal,
            built._repository,
            built._evidence_store,
            built._checkpoint_store,
            built._lease_service,
            built._graph_admission,
            built._channel_factory,
        ]
        messages = (
            "config",
            "workspace",
            "control_root",
            "journal",
            "repository",
            "evidence_store",
            "checkpoint_store",
            "lease_service",
            "graph_admission",
            "channel_factory",
        )
        for index, message in enumerate(messages):
            arguments = list(base_arguments)
            arguments[index] = object()
            expected_error = ValueError if message == "owner" else TypeError
            with self.subTest(dependency=message):
                with self.assertRaisesRegex(expected_error, message):
                    ProductionForegroundRunComponents(
                        *arguments,
                        coordinator_id=built._coordinator_id,
                        owner=built._owner,
                    )

        with self.assertRaisesRegex(ValueError, "coordinator_id"):
            ProductionForegroundRunComponents(
                *base_arguments,
                coordinator_id="",
                owner=built._owner,
            )
        with self.assertRaisesRegex(ValueError, "owner"):
            ProductionForegroundRunComponents(
                *base_arguments,
                coordinator_id=built._coordinator_id,
                owner=object(),
            )
        wrong_actor = dataclasses.replace(
            built._owner.actor,
            actor_id="different-coordinator",
        )
        with self.assertRaisesRegex(ValueError, "owner"):
            ProductionForegroundRunComponents(
                *base_arguments,
                coordinator_id=built._coordinator_id,
                owner=dataclasses.replace(built._owner, actor=wrong_actor),
            )
        with self.assertRaisesRegex(TypeError, "authority_clock"):
            ProductionForegroundRunComponents(
                *base_arguments,
                coordinator_id=built._coordinator_id,
                owner=built._owner,
                authority_clock=object(),
            )

    def test_execution_validation_fails_closed_and_delegates_verified_events(self) -> None:
        built = self.components()
        mismatch = built.validate_execution(object())
        self.assertFalse(mismatch.admitted)
        self.assertIs(
            mismatch.reason,
            ExecutionAdmissionReason.MANIFEST_DIGEST_MISMATCH,
        )

        with mock.patch.object(
            built,
            "_read_verified_events",
            side_effect=ValueError("corrupt journal"),
        ):
            invalid = built.validate_execution(self.manifest)
        self.assertFalse(invalid.admitted)
        self.assertIs(invalid.reason, ExecutionAdmissionReason.JOURNAL_CHAIN_INVALID)

        expected = ExecutionAdmissionResult(
            False,
            ExecutionAdmissionReason.GATE_B_DECISION_MISSING,
        )
        events = ()
        with (
            mock.patch.object(built, "_read_verified_events", return_value=events),
            mock.patch.object(built, "_live_graph_admitted", return_value=True),
            mock.patch.object(
                production_module,
                "admit_execution_snapshot",
                return_value=expected,
            ) as admission,
        ):
            actual = built.validate_execution(self.manifest)
        self.assertIs(expected, actual)
        admission.assert_called_once_with(
            self.manifest,
            events,
            workspace_hash=built._workspace.workspace_hash,
        )

        admitted = mock.Mock(admitted=True)
        with (
            mock.patch.object(built, "_read_verified_events", return_value=events),
            mock.patch.object(
                production_module,
                "admit_execution_snapshot",
                return_value=admitted,
            ),
            mock.patch.object(built, "_live_graph_admitted", return_value=False),
        ):
            changed = built.validate_execution(self.manifest)
        self.assertFalse(changed.admitted)
        self.assertIs(
            ExecutionAdmissionReason.TRELLIS_GRAPH_CHANGED,
            changed.reason,
        )

    def test_execution_validation_accepts_only_reconstructed_task_projection_drift(
        self,
    ) -> None:
        built = self.components()
        events = ()
        workspace_drift = ExecutionAdmissionResult(
            False,
            ExecutionAdmissionReason.WORKSPACE_DRIFT,
        )
        admitted = mock.Mock(admitted=True)
        reconstructed = dataclasses.replace(
            built._workspace,
            index_dirty_fingerprint="sha256:" + "f" * 64,
        )
        provider = mock.Mock()
        provider.ensure.side_effect = (
            mock.Mock(workspace=built._workspace),
            mock.Mock(workspace=built._workspace),
        )

        with (
            mock.patch.object(built, "_read_verified_events", return_value=events),
            mock.patch.object(built, "_live_graph_admitted", return_value=True),
            mock.patch.object(
                production_module,
                "admit_execution_snapshot",
                side_effect=(workspace_drift, admitted),
            ) as admission,
            mock.patch.object(
                production_module,
                "TrellisAuthoritativeProjectionProvider",
                return_value=provider,
            ),
            mock.patch.object(
                production_module,
                "reconstruct_pristine_workspace_identity",
                return_value=reconstructed,
            ),
        ):
            actual = built.validate_execution(self.manifest)

        self.assertIs(admitted, actual)
        self.assertEqual(2, provider.ensure.call_count)
        self.assertEqual(
            [
                mock.call(
                    self.manifest,
                    events,
                    workspace_hash=built._workspace.workspace_hash,
                ),
                mock.call(
                    self.manifest,
                    events,
                    workspace_hash=reconstructed.workspace_hash,
                ),
            ],
            admission.call_args_list,
        )

    def test_local_lease_cursor_workflow_and_empty_worker_batch_are_composed(self) -> None:
        built = self.components()
        initial = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(initial)

        with mock.patch.object(
            built,
            "_recover_cancelled_dispatch",
            wraps=built._recover_cancelled_dispatch,
        ) as takeover_recovery:
            active = built.acquire_lease(initial)
        self.assertIsNotNone(active)
        takeover_recovery.assert_called_once()
        self.assertTrue(active.lease_state.active)
        lease = active.lease_state.lease
        self.assertIsNotNone(lease)
        self.assertRegex(lease.lease_id, r"\ALEASE-[0-9A-F]{48}\Z")
        acquired_event = built._last_recovery.last_lease_event
        self.assertIsNotNone(acquired_event)
        self.assertRegex(
            acquired_event.event_id,
            r"\AEVENT-LEASE-ACQ-[0-9A-F]{48}\Z",
        )
        self.assertLessEqual(len(acquired_event.event_id), 64)
        self.assertIsNone(built.acquire_lease(initial))

        coordinator = built.coordinator(active)
        workflow = built.workflow(active)
        self.assertIsInstance(coordinator, ForegroundCoordinator)
        self.assertIsInstance(workflow, LocalExecutionWorkflow)

        batch = built.run_workers((), active)
        self.assertTrue(batch.outcomes_known)
        self.assertIs(active, batch.cursor)
        self.assertEqual({}, built.acceptance._identities)

        with self.assertRaisesRegex(TypeError, "attempts"):
            built.run_workers([], active)
        unknown = built.run_workers(
            (PreparedForegroundAttempt(self.identity(), object()),),
            active,
        )
        self.assertFalse(unknown.outcomes_known)
        self.assertIs(active, unknown.cursor)

    def test_acquire_lease_recovers_a_takeover_cancel_without_a_new_attempt(self) -> None:
        state = FakeExternalState()
        cancel_calls: list[str] = []

        class CountingBackendChannel(FakeBackendChannelPort):
            def cancel(self, effect):
                cancel_calls.append(effect.command.operation_id)
                return super().cancel(effect)

        first = self.components()
        capabilities = first._config.channel_capabilities
        bootstrapped = bootstrap_gate_b(
            gate_b_material(
                self.manifest,
                workspace_hash=first._workspace.workspace_hash,
            ),
            (),
            first._journal,
        )
        self.assertTrue(bootstrapped.admitted)

        def channel_factory(_attempt):
            return CountingBackendChannel(
                capabilities,
                state=state,
                send_state=TurnState.RUNNING,
            )

        first._channel_factory = channel_factory
        initial = first.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(initial)
        active = first.acquire_lease(initial)
        self.assertIsNotNone(active)
        reserved = first.coordinator(active).reserve_ready()
        self.assertIs(CoordinatorStatus.PROGRESSED, reserved.status)
        identity = reserved.reserved[0]
        prepared = first.workflow(reserved.cursor).prepare_attempt(identity)
        self.assertIsNotNone(prepared.attempt)
        cancel_suffix = production_module.canonical_sha256(
            {
                "fencing_token": 2,
                "identity": identity.to_primitive(),
                "operation": EffectOperation.CANCEL_TURN.value,
            }
        )[:48].upper()
        cancel_operation_id = f"CANCEL-{cancel_suffix}"
        lifecycle = production_module._LifecycleProjection(
            True,
            CoordinatorReason.NONE,
            (),
        )
        with (
            mock.patch.object(
                first,
                "_project_prepare_lifecycle",
                return_value=lifecycle,
            ),
            mock.patch.object(
                first,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
        ):
            dispatched = first.coordinator(prepared.cursor).dispatch_reserved(identity)
        self.assertIs(CoordinatorStatus.PROGRESSED, dispatched.status)
        self.assertIs(RuntimeState.RUNNING, dispatched.cursor.snapshot.attempts[0].state)
        first.close()

        self.authority_clock._value += timedelta(seconds=1_000)
        with mock.patch.object(
            production_module,
            "capture_process_start_id",
            return_value="test-restart-2",
        ):
            second = self.components()
        second._lease_service._prior_owner_process_probe = lambda *args, **kwargs: (
            LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
        )
        second._channel_factory = channel_factory
        recovered = second.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        original_trigger = BackendDispatchEffectService._trigger

        def crash_after_request(service, point, operation_id):
            if (
                point == "after_request_append"
                and operation_id == cancel_operation_id
            ):
                raise BackendDispatchEffectCrash(operation_id)
            return original_trigger(service, point, operation_id)

        with (
            mock.patch.object(
                second,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
            mock.patch.object(
                BackendDispatchEffectService,
                "_trigger",
                new=crash_after_request,
            ),
        ):
            self.assertIsNone(second.acquire_lease(recovered))
        blocked_recovery = second._recover()
        pending_cancel = next(
            (
                item
                for item in blocked_recovery.pending_external_effects
                if item.operation is EffectOperation.CANCEL_TURN
            ),
            None,
        )
        self.assertIsNotNone(
            pending_cancel,
            tuple(
                (event.sequence, event.event_type.value, event.identity.to_primitive())
                for event in second._read_verified_events()
            ),
        )
        assert pending_cancel is not None
        self.assertEqual(cancel_operation_id, pending_cancel.operation_id)
        self.assertEqual(2, pending_cancel.request_event.identity.coordinator_epoch)

        self.assertNotIn(cancel_operation_id, state.channel_records)
        second.close()

        self.authority_clock._value += timedelta(seconds=1_000)
        with mock.patch.object(
            production_module,
            "capture_process_start_id",
            return_value="test-restart-3",
        ):
            third = self.components()
        third._lease_service._prior_owner_process_probe = lambda *args, **kwargs: (
            LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
        )
        third._channel_factory = channel_factory
        recovered = third.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        recovery_results = []
        original_reconcile = production_module.reconcile_pending_external_effects

        def capture_recovery(*args, **kwargs):
            result = original_reconcile(*args, **kwargs)
            recovery_results.append(result)
            return result

        with (
            mock.patch.object(
                third,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
            mock.patch.object(third, "_retry_admitted", return_value=True),
            mock.patch.object(
                production_module,
                "reconcile_pending_external_effects",
                side_effect=capture_recovery,
            ),
        ):
            active = third.acquire_lease(recovered)

        self.assertIsNotNone(
            active,
            {
                "recovery_results": recovery_results,
                "pending": third._recover().pending_external_effects,
                "events": tuple(
                    (event.event_type.value, event.identity.to_primitive())
                    for event in third._read_verified_events()
                ),
                "channel_records": tuple(state.channel_records),
            },
        )
        self.assertTrue(
            any(
                event.event_type is JournalEventType.EFFECT_RECONCILED
                and event.identity.correlation_id == cancel_operation_id
                for event in third._read_verified_events()
            )
        )
        event_types = tuple(
            event.event_type for event in third._read_verified_events()
        )
        reconciled_index = event_types.index(JournalEventType.EFFECT_RECONCILED)
        self.assertIs(
            JournalEventType.LEASE_RENEWED,
            event_types[reconciled_index + 1],
        )
        self.assertEqual(1, len(active.snapshot.attempts))
        self.assertEqual(identity.attempt, active.snapshot.attempts[0].attempt)
        self.assertEqual(3, active.snapshot.attempts[0].coordinator_epoch)
        self.assertIs(RuntimeState.RESERVED, active.snapshot.attempts[0].state)
        self.assertTrue(state.channel_records[cancel_operation_id].applied)
        self.assertEqual([cancel_operation_id], cancel_calls)

        reconciled_cursor = recovery_results[0].cursor
        self.assertIsNotNone(reconciled_cursor)
        assert reconciled_cursor is not None
        journal_events = third._read_verified_events()
        request_event = next(
            event
            for event in journal_events
            if event.event_type is JournalEventType.DISPATCH_REQUESTED
            and event.identity == identity
        )
        observation_event = next(
            event
            for event in journal_events
            if event.event_type is JournalEventType.DISPATCH_OBSERVED
            and event.identity == identity
        )
        recovered_event = next(
            event
            for event in journal_events
            if event.event_type is JournalEventType.EFFECT_RECONCILED
            and event.identity.correlation_id == cancel_operation_id
        )
        recovered_event_index = journal_events.index(recovered_event)
        recovered_prefix = journal_events[: recovered_event_index + 1]
        cancel_request = next(
            event
            for event in recovered_prefix
            if event.event_type is JournalEventType.EFFECT_REQUESTED
            and event.identity.correlation_id == cancel_operation_id
        )
        cancel_request_index = recovered_prefix.index(cancel_request)

        def rechain_from(
            replacement,
        ):
            rebuilt = list(recovered_prefix[:cancel_request_index]) + [replacement]
            for original in recovered_prefix[cancel_request_index + 1 :]:
                rebuilt.append(
                    type(original).create(
                        sequence=original.sequence,
                        event_id=original.event_id,
                        event_type=original.event_type,
                        identity=original.identity,
                        actor_type=original.actor_type,
                        actor_id=original.actor_id,
                        recorded_at=original.recorded_at,
                        previous_event_hash=rebuilt[-1].event_hash,
                        payload=original.payload,
                        reason_code=original.reason_code,
                    )
                )
            return tuple(rebuilt)

        self.assertTrue(
            _recovered_cancel_authority_matches(
                recovered_event,
                recovered_prefix,
                manifest_digest=self.manifest.canonical_sha256(),
            )
        )
        self.assertFalse(
            _recovered_cancel_authority_matches(
                recovered_event,
                recovered_prefix,
                manifest_digest="sha256:" + "0" * 64,
            )
        )
        wrong_actor = type(recovered_event).create(
            sequence=recovered_event.sequence,
            event_id=recovered_event.event_id,
            event_type=recovered_event.event_type,
            identity=recovered_event.identity,
            actor_type=recovered_event.actor_type,
            actor_id="different-coordinator",
            recorded_at=recovered_event.recorded_at,
            previous_event_hash=recovered_event.previous_event_hash,
            payload=recovered_event.payload,
            reason_code=recovered_event.reason_code,
        )
        self.assertFalse(
            _recovered_cancel_authority_matches(
                wrong_actor,
                recovered_prefix[:-1] + (wrong_actor,),
                manifest_digest=self.manifest.canonical_sha256(),
            )
        )
        wrong_request_actor = type(cancel_request).create(
            sequence=cancel_request.sequence,
            event_id=cancel_request.event_id,
            event_type=cancel_request.event_type,
            identity=cancel_request.identity,
            actor_type=cancel_request.actor_type,
            actor_id="different-coordinator",
            recorded_at=cancel_request.recorded_at,
            previous_event_hash=cancel_request.previous_event_hash,
            payload=cancel_request.payload,
            reason_code=cancel_request.reason_code,
        )
        wrong_request_actor_prefix = rechain_from(wrong_request_actor)
        self.assertFalse(
            _recovered_cancel_authority_matches(
                wrong_request_actor_prefix[-1],
                wrong_request_actor_prefix,
                manifest_digest=self.manifest.canonical_sha256(),
            )
        )
        late_request = type(cancel_request).create(
            sequence=cancel_request.sequence,
            event_id=cancel_request.event_id,
            event_type=cancel_request.event_type,
            identity=cancel_request.identity,
            actor_type=cancel_request.actor_type,
            actor_id=cancel_request.actor_id,
            recorded_at="2099-01-01T00:00:00.000000Z",
            previous_event_hash=cancel_request.previous_event_hash,
            payload=cancel_request.payload,
            reason_code=cancel_request.reason_code,
        )
        late_request_prefix = rechain_from(late_request)
        self.assertFalse(
            _recovered_cancel_authority_matches(
                late_request_prefix[-1],
                late_request_prefix,
                manifest_digest=self.manifest.canonical_sha256(),
            )
        )
        recovered_turn = channel_factory(identity).inspect_turn(cancel_operation_id)
        for label, invalid_prefix in {
            "request-actor": wrong_request_actor_prefix,
            "request-time": late_request_prefix,
        }.items():
            with self.subTest(label=label):
                invalid_authority = third.coordinator(
                    reconciled_cursor
                ).reclaim_cancelled_dispatch(
                    request_event,
                    observation_event,
                    owned_path_changes=(),
                    recovered_cancellation=(invalid_prefix[-1], recovered_turn),
                    recovered_cancellation_history=invalid_prefix,
                )
                self.assertIs(
                    CoordinatorStatus.REJECTED,
                    invalid_authority.status,
                )
                self.assertIs(
                    CoordinatorReason.RECOVERY_PROOF_INVALID,
                    invalid_authority.reason,
                )
                self.assertEqual((), invalid_authority.events)
                self.assertEqual(journal_events, third._read_verified_events())
        request_payload_tampering = {
            "request-payload-hash": dataclasses.replace(
                cancel_request.payload,
                request_payload_hash="sha256:" + "f" * 64,
            ),
            "expected-sequence": dataclasses.replace(
                cancel_request.payload,
                expected_sequence=cancel_request.payload.expected_sequence - 1,
            ),
            "base-hash": dataclasses.replace(
                cancel_request.payload,
                base_hash="sha256:" + "f" * 64,
            ),
            "head-hash": dataclasses.replace(
                cancel_request.payload,
                head_hash="sha256:" + "f" * 64,
            ),
        }
        for label, wrong_request_payload in request_payload_tampering.items():
            with self.subTest(label=label):
                wrong_request = type(cancel_request).create(
                    sequence=cancel_request.sequence,
                    event_id=cancel_request.event_id,
                    event_type=cancel_request.event_type,
                    identity=cancel_request.identity,
                    actor_type=cancel_request.actor_type,
                    actor_id=cancel_request.actor_id,
                    recorded_at=cancel_request.recorded_at,
                    previous_event_hash=cancel_request.previous_event_hash,
                    payload=wrong_request_payload,
                    reason_code=cancel_request.reason_code,
                )
                wrong_request_prefix = rechain_from(wrong_request)
                invalid_request = third.coordinator(
                    reconciled_cursor
                ).reclaim_cancelled_dispatch(
                    request_event,
                    observation_event,
                    owned_path_changes=(),
                    recovered_cancellation=(
                        wrong_request_prefix[-1],
                        recovered_turn,
                    ),
                    recovered_cancellation_history=wrong_request_prefix,
                )
                self.assertIs(CoordinatorStatus.REJECTED, invalid_request.status)
                self.assertIs(
                    CoordinatorReason.RECOVERY_PROOF_INVALID,
                    invalid_request.reason,
                )
                self.assertEqual((), invalid_request.events)
                self.assertEqual(journal_events, third._read_verified_events())
        wrong_epoch = type(recovered_event).create(
            sequence=recovered_event.sequence,
            event_id=recovered_event.event_id,
            event_type=recovered_event.event_type,
            identity=dataclasses.replace(
                recovered_event.identity,
                coordinator_epoch=recovered_event.identity.coordinator_epoch + 1,
            ),
            actor_type=recovered_event.actor_type,
            actor_id=recovered_event.actor_id,
            recorded_at=recovered_event.recorded_at,
            previous_event_hash=recovered_event.previous_event_hash,
            payload=recovered_event.payload,
            reason_code=recovered_event.reason_code,
        )
        invalid = third.coordinator(reconciled_cursor).reclaim_cancelled_dispatch(
            request_event,
            observation_event,
            owned_path_changes=(),
            recovered_cancellation=(wrong_epoch, recovered_turn),
        )
        self.assertIs(CoordinatorStatus.REJECTED, invalid.status)
        self.assertIs(CoordinatorReason.RECOVERY_PROOF_INVALID, invalid.reason)
        self.assertEqual((), invalid.events)
        self.assertEqual(journal_events, third._read_verified_events())

        with mock.patch.object(
            third._evidence_store,
            "verify_existing",
            side_effect=RuntimeError("evidence unavailable"),
        ) as verified:
            self.assertIsNone(third._recover_cancelled_dispatch(reconciled_cursor))
        verified.assert_called_once()
        self.assertEqual(journal_events, third._read_verified_events())

    def test_takeover_consumes_cancel_reconciled_by_prior_lease_holder(self) -> None:
        state = FakeExternalState()
        cancel_calls: list[str] = []

        class CountingBackendChannel(FakeBackendChannelPort):
            def cancel(self, effect):
                cancel_calls.append(effect.command.operation_id)
                return super().cancel(effect)

        first = self.components()
        capabilities = first._config.channel_capabilities
        bootstrapped = bootstrap_gate_b(
            gate_b_material(
                self.manifest,
                workspace_hash=first._workspace.workspace_hash,
            ),
            (),
            first._journal,
        )
        self.assertTrue(bootstrapped.admitted)

        def channel_factory(_attempt):
            return CountingBackendChannel(
                capabilities,
                state=state,
                send_state=TurnState.RUNNING,
            )

        first._channel_factory = channel_factory
        initial = first.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(initial)
        active = first.acquire_lease(initial)
        self.assertIsNotNone(active)
        reserved = first.coordinator(active).reserve_ready()
        identity = reserved.reserved[0]
        prepared = first.workflow(reserved.cursor).prepare_attempt(identity)
        self.assertIsNotNone(prepared.attempt)
        cancel_suffix = production_module.canonical_sha256(
            {
                "fencing_token": 2,
                "identity": identity.to_primitive(),
                "operation": EffectOperation.CANCEL_TURN.value,
            }
        )[:48].upper()
        cancel_operation_id = f"CANCEL-{cancel_suffix}"
        lifecycle = production_module._LifecycleProjection(
            True,
            CoordinatorReason.NONE,
            (),
        )
        with (
            mock.patch.object(
                first,
                "_project_prepare_lifecycle",
                return_value=lifecycle,
            ),
            mock.patch.object(
                first,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
        ):
            dispatched = first.coordinator(prepared.cursor).dispatch_reserved(identity)
        self.assertIs(CoordinatorStatus.PROGRESSED, dispatched.status)
        first.close()

        self.authority_clock._value += timedelta(seconds=1_000)
        with mock.patch.object(
            production_module,
            "capture_process_start_id",
            return_value="test-prior-holder-2",
        ):
            second = self.components()
        second._lease_service._prior_owner_process_probe = lambda *args, **kwargs: (
            LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
        )
        second._channel_factory = channel_factory
        recovered = second.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        original_trigger = BackendDispatchEffectService._trigger

        def crash_after_request(service, point, operation_id):
            if point == "after_request_append" and operation_id == cancel_operation_id:
                raise BackendDispatchEffectCrash(operation_id)
            return original_trigger(service, point, operation_id)

        with (
            mock.patch.object(
                second,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
            mock.patch.object(
                BackendDispatchEffectService,
                "_trigger",
                new=crash_after_request,
            ),
        ):
            self.assertIsNone(second.acquire_lease(recovered))
        second.close()

        self.authority_clock._value += timedelta(seconds=1_000)
        with mock.patch.object(
            production_module,
            "capture_process_start_id",
            return_value="test-prior-holder-3",
        ):
            third = self.components()
        third._lease_service._prior_owner_process_probe = lambda *args, **kwargs: (
            LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
        )
        third._channel_factory = channel_factory
        recovered = third.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        original_reconcile = production_module.reconcile_pending_external_effects
        reconciliation_results = []

        def reconcile_then_crash(*args, **kwargs):
            result = original_reconcile(*args, **kwargs)
            reconciliation_results.append(result)
            raise RuntimeError("simulated coordinator crash after reconciliation")

        with (
            mock.patch.object(
                third,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
            mock.patch.object(third, "_retry_admitted", return_value=True),
            mock.patch.object(
                production_module,
                "reconcile_pending_external_effects",
                side_effect=reconcile_then_crash,
            ),
        ):
            self.assertIsNone(third.acquire_lease(recovered))
        self.assertEqual(1, len(reconciliation_results))
        reconciled_event = next(
            event
            for event in third._read_verified_events()
            if event.event_type is JournalEventType.EFFECT_RECONCILED
            and event.identity.correlation_id == cancel_operation_id
        )
        self.assertEqual(3, reconciled_event.identity.coordinator_epoch)
        prior_actor_id = reconciled_event.actor_id
        third.close()

        self.authority_clock._value += timedelta(seconds=1_000)
        with mock.patch.object(
            production_module,
            "capture_process_start_id",
            return_value="test-prior-holder-4",
        ):
            fourth = self.components()
        fourth._lease_service._prior_owner_process_probe = lambda *args, **kwargs: (
            LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
        )
        fourth._channel_factory = channel_factory
        recovered = fourth.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        with (
            mock.patch.object(
                fourth,
                "_dispatch_runtime_admitted",
                return_value=True,
            ),
            mock.patch.object(fourth, "_retry_admitted", return_value=True),
        ):
            active = fourth.acquire_lease(recovered)

        self.assertIsNotNone(active)
        assert active is not None
        self.assertNotEqual(prior_actor_id, fourth._coordinator_id)
        self.assertEqual(1, len(active.snapshot.attempts))
        self.assertEqual(identity.attempt, active.snapshot.attempts[0].attempt)
        self.assertEqual(4, active.snapshot.attempts[0].coordinator_epoch)
        self.assertIs(RuntimeState.RESERVED, active.snapshot.attempts[0].state)
        self.assertEqual([cancel_operation_id], cancel_calls)

    def test_lease_append_is_followed_by_graph_revalidation(self) -> None:
        built = self.components()
        initial = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(initial)

        with mock.patch.object(
            built,
            "_live_graph_admitted",
            side_effect=(True, False),
        ) as admitted:
            active = built.acquire_lease(initial)

        self.assertIsNone(active)
        self.assertEqual(2, admitted.call_count)
        recovered = built._recover()
        self.assertTrue(recovered.lease_state.active)
        self.assertIsNotNone(recovered.last_lease_event)

    def test_graph_drift_before_lease_does_not_append_a_lease(self) -> None:
        built = self.components()
        initial = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(initial)
        before = built._read_verified_events()

        with mock.patch.object(
            built,
            "_live_graph_admitted",
            return_value=False,
        ) as admitted:
            active = built.acquire_lease(initial)

        self.assertIsNone(active)
        admitted.assert_called_once_with()
        self.assertEqual(before, built._read_verified_events())
        self.assertFalse(
            any(
                event.event_type is JournalEventType.LEASE_ACQUIRED
                for event in built._read_verified_events()
            )
        )

    def test_graph_drift_before_reserve_does_not_append_task_events(self) -> None:
        built, active = self.acquired_components()
        built._lifecycle_factory = mock.Mock(
            side_effect=AssertionError("lifecycle must not be invoked")
        )
        before = active.head
        with mock.patch.object(
            built,
            "_live_graph_admitted",
            return_value=False,
        ) as admitted:
            coordinator = built.coordinator(active)
            result = coordinator.reserve_ready()

        self.assertIs(CoordinatorStatus.BLOCKED, result.status)
        self.assertIs(
            CoordinatorReason.GRAPH_SNAPSHOT_NOT_ADMITTED,
            result.reason,
        )
        self.assertEqual((), result.events)
        self.assertEqual(before, result.cursor.head)
        admitted.assert_called_once_with()

    def test_real_attempt_is_routed_to_monitor_without_starting_a_worker(self) -> None:
        built, active = self.acquired_components()
        identity = self.identity()
        attempt = attempt_worktree(self.root / "prepared-attempt", identity)
        expected = WorkerBatchResult(True, cursor=active)

        with mock.patch.object(
            production_module,
            "BackendWorkerTurnMonitor",
        ) as monitor_type:
            monitor_type.return_value.run.return_value = expected
            actual = built.run_workers(
                (PreparedForegroundAttempt(identity, attempt),),
                active,
            )

        self.assertIs(expected, actual)
        monitor_type.return_value.run.assert_called_once()
        self.assertEqual(identity, built.acceptance._identities[identity.task_id])
        self.assertEqual((), built._routes_for_cursor(active))

    def identity(self) -> ExecutionIdentity:
        return ExecutionIdentity(
            self.manifest.run_id,
            1,
            self.manifest.tasks[0].id,
            1,
            "DISPATCH-PRODUCTION-COMPOSITION",
        )

    def test_checkpoint_and_worker_lease_methods_validate_and_fail_closed(self) -> None:
        built, active = self.acquired_components()
        with self.assertRaisesRegex(TypeError, "cursor"):
            built.publish_checkpoint(object(), ())
        for events in ((), [], (object(),)):
            with self.subTest(events=events):
                blocked = built.publish_checkpoint(active, events)
                self.assertIs(blocked.status, ExecutionCheckpointStatus.BLOCKED)
                self.assertIs(blocked.reason, ExecutionCheckpointReason.STATE_MISMATCH)

        journal_events = built._read_verified_events()
        expected = mock.sentinel.checkpoint_result
        with mock.patch.object(
            built._checkpoint_publisher,
            "observe",
            return_value=expected,
        ) as observe:
            result = built.publish_checkpoint(active, journal_events)
        self.assertIs(expected, result)
        observe.assert_called_once_with(
            active.snapshot,
            active.graph_index,
            active.head,
            journal_events[-1],
        )

        renewed = built._renew_worker_lease(active)
        self.assertTrue(renewed.succeeded)
        self.assertIsNotNone(renewed.cursor)
        self.assertIsNotNone(renewed.event)
        with self.assertRaisesRegex(TypeError, "cursor"):
            built._require_active_cursor(object())
        built.close()
        self.assertFalse(built._renew_worker_lease(renewed.cursor).succeeded)

    def test_retry_admission_short_circuits_and_swallows_boundary_errors(self) -> None:
        built = self.components()
        built._lifecycle_factory = mock.Mock(
            side_effect=AssertionError("lifecycle must not be invoked")
        )
        admitted = mock.Mock(admitted=True)
        with (
            mock.patch.object(production_module, "admit_backend", return_value=admitted),
            mock.patch.object(built, "protect_control_root", return_value=True),
            mock.patch.object(built, "verify_workspace_identity", return_value=True),
        ):
            self.assertTrue(built._retry_admitted())
        with (
            mock.patch.object(production_module, "admit_backend", return_value=admitted),
            mock.patch.object(built, "protect_control_root", return_value=True),
            mock.patch.object(built, "verify_workspace_identity", return_value=True),
            mock.patch.object(
                built,
                "_live_graph_admitted",
                return_value=False,
            ) as graph_admitted,
        ):
            self.assertFalse(built._retry_admitted())
        graph_admitted.assert_called_once_with()
        with mock.patch.object(
            production_module,
            "admit_backend",
            side_effect=RuntimeError("admission probe failed"),
        ):
            self.assertFalse(built._retry_admitted())

    def test_acquire_and_recovery_reject_wrong_types_and_untrusted_results(self) -> None:
        built = self.components()
        initial = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(initial)
        with self.assertRaisesRegex(TypeError, "cursor"):
            built.acquire_lease(object())
        with mock.patch.object(built._lease_service, "acquire", return_value=object()):
            self.assertIsNone(built.acquire_lease(initial))
        with mock.patch.object(built, "_recover", return_value=object()):
            self.assertIsNone(built.recover_verified_cursor(self.manifest))
        self.assertIsNone(built._cursor_from_recovery(object()))

        with self.assertRaisesRegex(ValueError, "attempt identity"):
            built._attempt_key(ExecutionIdentity(self.manifest.run_id, 1))

    def test_cleanup_validates_types_and_delegates_a_fenced_candidate(self) -> None:
        built, active = self.acquired_components()
        workflow = built.workflow(active)
        identity = self.identity()
        attempt = attempt_worktree(self.root / "cleanup-attempt", identity)
        promotion = PromotionRecord(
            identity.task_id,
            0,
            "1" * 40,
            "2" * 40,
            "3" * 40,
            "4" * 40,
            "sha256:" + "a" * 64,
            (),
        )

        with self.assertRaisesRegex(TypeError, "workflow"):
            built.cleanup_attempt(object(), attempt, promotion)
        with self.assertRaisesRegex(TypeError, "attempt"):
            built.cleanup_attempt(workflow, object(), promotion)
        with self.assertRaisesRegex(TypeError, "promotion"):
            built.cleanup_attempt(workflow, attempt, object())

        expected = mock.sentinel.cleanup_result
        with mock.patch.object(
            workflow,
            "cleanup_attempt",
            return_value=expected,
        ) as cleanup:
            actual = built.cleanup_attempt(workflow, attempt, promotion)
        self.assertIs(expected, actual)
        service, candidate = cleanup.call_args.args
        self.assertIs(built._cleanup, service)
        self.assertIs(attempt, candidate.attempt)
        self.assertEqual(promotion.source_commit_sha, candidate.expected_head_sha)
        self.assertEqual(
            "CLEANUP-TASK-001-0001-EPOCH-0001",
            cleanup.call_args.kwargs["operation_id"],
        )

    def test_terminal_finalizer_receives_recovered_release_only_at_the_same_head(
        self,
    ) -> None:
        built, active = self.acquired_components()
        with mock.patch.object(
            production_module,
            "ProductionTerminalFinalizer",
        ) as finalizer:
            finalizer.return_value.finish.return_value = mock.sentinel.active_terminal
            self.assertIs(mock.sentinel.active_terminal, built.finish(active))
        self.assertIsNone(
            finalizer.call_args.kwargs["recovered_terminal_event"]
        )

        released = built._lease_service.release(event_id="EVENT-LEASE-RELEASED-TEST")
        self.assertIsNotNone(released.lease_state)
        recovery = built._recover()
        released_cursor = built._cursor_from_recovery(recovery)
        self.assertIsNotNone(released_cursor)
        built._last_recovery = recovery
        with mock.patch.object(
            production_module,
            "ProductionTerminalFinalizer",
        ) as finalizer:
            finalizer.return_value.finish.return_value = mock.sentinel.released_terminal
            self.assertIs(
                mock.sentinel.released_terminal,
                built.finish(released_cursor),
            )
        self.assertIs(
            recovery.last_lease_event,
            finalizer.call_args.kwargs["recovered_terminal_event"],
        )

    def test_verified_event_reader_handles_absent_and_empty_segment_directories(self) -> None:
        built = self.components()
        self.assertEqual((), built._read_verified_events())
        segments = built._config.layout.journal_root / "segments"
        segments.mkdir(parents=True)
        self.assertEqual((), built._read_verified_events())

    def test_verified_event_reader_rejects_invalid_and_noncontiguous_layouts(self) -> None:
        built = self.components()
        segments = built._config.layout.journal_root / "segments"
        segments.mkdir(parents=True)
        (segments / "unexpected.txt").write_text("invalid\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "layout is invalid"):
            built._read_verified_events()

        (segments / "unexpected.txt").unlink()
        (segments / "segment-00000002.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "non-contiguous"):
            built._read_verified_events()

    def test_verified_event_reader_rejects_empty_and_malformed_frames(self) -> None:
        built = self.components()
        segment = built._config.layout.journal_root / "segments" / "segment-00000001.jsonl"
        segment.parent.mkdir(parents=True)
        segment.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "segment is empty"):
            built._read_verified_events()

        segment.write_bytes(b"{}\n")
        with self.assertRaisesRegex(ValueError, "strict decoding"):
            built._read_verified_events()

    def test_verified_event_reader_enforces_event_budget_and_hash_chain(self) -> None:
        built, _ = self.acquired_components()
        with mock.patch.object(production_module, "_MAX_ADMISSION_EVENTS", 0):
            with self.assertRaisesRegex(ValueError, "event limit"):
                built._read_verified_events()

        fake_event = mock.Mock()
        fake_event.identity.run_id = "different-run"
        fake_event.sequence = 1
        fake_event.previous_event_hash = production_module.GENESIS_HEAD.event_hash
        fake_event.canonical_json_bytes.return_value = b""
        decoded = mock.Mock(ok=True, value=fake_event)
        with mock.patch.object(
            production_module,
            "decode_journal_event_bytes",
            return_value=decoded,
        ):
            with self.assertRaisesRegex(ValueError, "hash chain"):
                built._read_verified_events()

    def test_verified_event_reader_rechecks_control_root_after_read(self) -> None:
        built, _ = self.acquired_components()
        with mock.patch.object(
            built,
            "protect_control_root",
            side_effect=(True, False),
        ):
            with self.assertRaisesRegex(ValueError, "control_root_drift"):
                built._read_verified_events()

    def test_close_is_idempotent_and_blocks_further_recovery(self) -> None:
        built = self.components()

        built.close()
        built.close()

        self.assertFalse(built.protect_control_root())
        self.assertIsNone(built.recover_verified_cursor(self.manifest))

    def test_managed_workspace_drift_blocks_identity_and_recovery(self) -> None:
        built = self.components()
        drift = self.repository / "src" / "req-001" / "drift.txt"
        drift.write_text("changed\n", encoding="utf-8")

        self.assertFalse(built.verify_workspace_identity(self.manifest))
        self.assertIsNone(built.recover_verified_cursor(self.manifest))

    def test_replaced_control_root_blocks_validation_and_recovery(self) -> None:
        built = self.components()
        layout = ProductionRuntimeLayout.for_run(
            self.repository,
            self.runtime_root,
            self.manifest.run_id,
        )
        displaced = layout.control_root.with_name("control-displaced")
        layout.control_root.rename(displaced)
        layout.control_root.mkdir()

        self.assertFalse(built.protect_control_root())
        self.assertIsNone(built.recover_verified_cursor(self.manifest))


if __name__ == "__main__":
    unittest.main()
