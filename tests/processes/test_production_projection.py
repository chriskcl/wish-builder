from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.processes import test_production_lifecycle as lifecycle_tests
from tests.processes.test_coordinator import one_task_manifest
from tests.processes.test_production import (
    git,
    initialize_repository,
    one_task_graph_snapshot,
)
from tests.processes.test_workflow import PassingAcceptance
from wish_builder.adapters.trellis import FakeTrellisGraphPort
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts.compatibility import Provider
from wish_builder.contracts.runtime import JournalEventType, RuntimeState
from wish_builder.processes import CoordinatorStatus
from wish_builder.processes import production as production_module
from wish_builder.processes.foreground import PreparedForegroundAttempt
from wish_builder.processes.production import ProductionForegroundRunComponents
from wish_builder.processes.workflow import WorkflowStatus
from wish_builder.services.backend_admission import current_platform
from wish_builder.services.ports import (
    TrellisProjectionApplyRequest,
    TrellisProjectionDisposition,
    TrellisProjectionObservation,
    TrellisProjectionReason,
)
from wish_builder.services.trellis_projection import TrellisProjectionSyncStatus


def _revision(value: int) -> str:
    return f"sha256:{value:064x}"


class _ProjectionCheckout:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def ensure(self, run_id: str) -> Path:
        self.calls += 1
        return self.path


class _ProjectionPort:
    def __init__(self) -> None:
        self.revision_number = 1
        self.projection = None
        self.task_status = "planning"
        self.unavailable = False
        self.conflict_reason: TrellisProjectionReason | None = None
        self.race_once = False
        self.inspect_calls: list[tuple[Path, str]] = []
        self.apply_calls: list[TrellisProjectionApplyRequest] = []

    @property
    def revision(self) -> str:
        return _revision(self.revision_number)

    def inspect(self, checkout_root: Path, trellis_task_id: str):
        self.inspect_calls.append((checkout_root, trellis_task_id))
        if self.unavailable:
            return TrellisProjectionObservation(
                TrellisProjectionDisposition.UNAVAILABLE,
                TrellisProjectionReason.UNAVAILABLE,
            )
        return TrellisProjectionObservation(
            TrellisProjectionDisposition.INSPECTED,
            TrellisProjectionReason.NONE,
            self.revision,
            512,
            self.task_status,
            self.projection,
        )

    def apply(self, request: TrellisProjectionApplyRequest):
        self.apply_calls.append(request)
        if self.race_once:
            self.race_once = False
            self.revision_number += 1
            return TrellisProjectionObservation(
                TrellisProjectionDisposition.CONFLICT,
                TrellisProjectionReason.REVISION_CONFLICT,
                self.revision,
                512,
                self.task_status,
                self.projection,
            )
        if self.conflict_reason is not None:
            return TrellisProjectionObservation(
                TrellisProjectionDisposition.CONFLICT,
                self.conflict_reason,
                self.revision,
                512,
                self.task_status,
                self.projection,
            )
        if self.projection == request.projection:
            return TrellisProjectionObservation(
                TrellisProjectionDisposition.IDEMPOTENT,
                TrellisProjectionReason.NONE,
                self.revision,
                512,
                self.task_status,
                self.projection,
            )
        self.projection = request.projection
        self.task_status = request.projection.target_status
        self.revision_number += 1
        return TrellisProjectionObservation(
            TrellisProjectionDisposition.APPLIED,
            TrellisProjectionReason.NONE,
            self.revision,
            768,
            self.task_status,
            self.projection,
        )


class ProductionTrellisProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.component_index = 0
        bundle = load_bundled_compatibility()
        self.cell = bundle.platform(Provider.CODEX, current_platform())
        self.manifest = dataclasses.replace(
            one_task_manifest(),
            capability_digest=self.cell.capabilities.capability_digest,
            launch_profile_digest=self.cell.launch_profile_digest,
            policy_digest=self.cell.capabilities.policy_digest,
        )

    def _new_runtime(self):
        self.component_index += 1
        repository = self.root / f"repository-{self.component_index}"
        runtime_root = self.root / f"runtime-{self.component_index}"
        initialize_repository(repository)
        factory = lifecycle_tests._SeparatedTrellisFactories(
            production_module.channel_capabilities_from_compatibility(self.cell)
        )
        checkout = _ProjectionCheckout(repository)
        port = _ProjectionPort()
        built = self._build(repository, runtime_root, factory, checkout, port)
        lifecycle_tests.ProductionLifecycleIntegrationTests._seed_executing_graph(
            self,
            built,
        )
        recovered = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        acquired = built.acquire_lease(recovered)
        self.assertIsNotNone(acquired)
        built._test_active_cursor = acquired
        return built, factory, checkout, port, repository, runtime_root

    def _build(
        self,
        repository: Path,
        runtime_root: Path,
        factory: lifecycle_tests._SeparatedTrellisFactories,
        checkout: _ProjectionCheckout,
        port: _ProjectionPort,
    ) -> ProductionForegroundRunComponents:
        command = (
            str((self.root / "node.exe").absolute()),
            str((self.root / "bridge.mjs").absolute()),
        )
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
                return_value=factory,
            ),
            mock.patch.object(
                production_module,
                "TrellisAuthoritativeProjectionProvider",
                return_value=checkout,
            ),
            mock.patch.object(
                production_module,
                "TrellisCoreProjectionPort",
                return_value=port,
            ),
            mock.patch.object(
                production_module,
                "TrellisCoreGraphPort",
                return_value=FakeTrellisGraphPort(one_task_graph_snapshot()),
            ),
        ):
            built = ProductionForegroundRunComponents.from_runtime_inputs(
                self.manifest,
                runtime_root=runtime_root,
                workspace_root=repository,
            )
        built._lifecycle_factory = factory.lifecycle_for
        self.addCleanup(built.close)
        return built

    def _dispatch(self, built):
        return lifecycle_tests.ProductionLifecycleIntegrationTests.reserve_prepare_dispatch(
            self,
            built,
        )

    @staticmethod
    def _commit_result(prepared) -> None:
        attempt_root = Path(prepared.attempt.path)
        result = attempt_root / "src" / "req-001" / "result.txt"
        result.write_text("implemented\n", encoding="utf-8")
        git(attempt_root, "add", "--", "src/req-001/result.txt")
        git(attempt_root, "commit", "-m", "implement projected task")

    def test_durable_dispatch_projects_in_progress_through_production(self) -> None:
        built, _factory, _checkout, port, repository, _runtime = self._new_runtime()

        _identity, _prepared, dispatched = self._dispatch(built)

        observed = next(
            event
            for event in dispatched.events
            if event.event_type is JournalEventType.DISPATCH_OBSERVED
        )
        self.assertIs(dispatched.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual(dispatched.cursor.head.sequence, observed.sequence)
        self.assertEqual(dispatched.cursor.head.event_hash, observed.event_hash)
        self.assertEqual("in_progress", port.task_status)
        self.assertEqual("dispatched", port.projection.canonical_state)
        self.assertEqual(observed.sequence, port.projection.canonical_sequence)
        self.assertIs(
            built.projection_results[-1].status,
            TrellisProjectionSyncStatus.APPLIED,
        )
        self.assertTrue(
            all(root == repository for root, _task_id in port.inspect_calls)
        )
        self.assertTrue(
            all(request.checkout_root == repository for request in port.apply_calls)
        )
        built._journal.current_position(expected_head=dispatched.cursor.head)

    def test_durable_task_verification_projects_completed_through_production(
        self,
    ) -> None:
        built, _factory, _checkout, port, _repository, _runtime = self._new_runtime()
        identity, prepared, dispatched = self._dispatch(built)
        self._commit_result(prepared)
        batch = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            dispatched.cursor,
        )
        accepted = built.coordinator(batch.cursor).accept_worker_result(
            batch.proposals[0]
        )
        staged = built.workflow(accepted.cursor).stage_attempt_result(prepared.attempt)
        self.assertIs(staged.status, WorkflowStatus.PROGRESSED)
        self.assertIsNotNone(staged.staged)
        promoted = built.workflow(staged.cursor).promote_staged(
            (staged.staged,),
            PassingAcceptance((staged.staged,)),
        )

        verified = next(
            event
            for event in promoted.events
            if event.event_type is JournalEventType.TASK_VERIFIED
        )
        self.assertIs(promoted.status, WorkflowStatus.PROGRESSED)
        self.assertEqual(RuntimeState.VERIFIED, promoted.cursor.snapshot.tasks[0].state)
        self.assertEqual(promoted.cursor.head.sequence, verified.sequence)
        self.assertEqual(promoted.cursor.head.event_hash, verified.event_hash)
        self.assertEqual("completed", port.task_status)
        self.assertEqual("verified", port.projection.canonical_state)
        self.assertEqual(verified.sequence, port.projection.canonical_sequence)
        self.assertIs(
            built.projection_results[-1].status,
            TrellisProjectionSyncStatus.APPLIED,
        )
        built._journal.current_position(expected_head=promoted.cursor.head)

    def test_projection_outage_or_conflict_never_rolls_back_dispatch(self) -> None:
        cases = (
            (
                "outage",
                TrellisProjectionSyncStatus.DELAYED,
                TrellisProjectionReason.UNAVAILABLE,
            ),
            (
                "conflict",
                TrellisProjectionSyncStatus.CONFLICT,
                TrellisProjectionReason.STATUS_MISMATCH,
            ),
        )
        for mode, expected_status, expected_reason in cases:
            with self.subTest(mode=mode):
                built, _factory, _checkout, port, _repository, _runtime = (
                    self._new_runtime()
                )
                if mode == "outage":
                    port.unavailable = True
                else:
                    port.conflict_reason = expected_reason

                _identity, _prepared, dispatched = self._dispatch(built)

                self.assertIs(dispatched.status, CoordinatorStatus.PROGRESSED)
                self.assertIs(
                    dispatched.cursor.snapshot.tasks[0].state,
                    RuntimeState.DISPATCHED,
                )
                projection = built.projection_results[-1]
                self.assertIs(projection.status, expected_status)
                self.assertIs(projection.reason, expected_reason)
                built._journal.current_position(expected_head=dispatched.cursor.head)

    def test_recovery_repairs_lagging_projection_without_worker_redispatch(
        self,
    ) -> None:
        built, factory, checkout, port, repository, runtime_root = self._new_runtime()
        port.unavailable = True
        _identity, _prepared, dispatched = self._dispatch(built)
        self.assertIs(
            built.projection_results[-1].status,
            TrellisProjectionSyncStatus.DELAYED,
        )
        worker_effects = tuple(factory.channel_state.channel_records)
        self.assertEqual(2, len(worker_effects))
        built.close()

        port.unavailable = False
        restarted = self._build(
            repository,
            runtime_root,
            factory,
            checkout,
            port,
        )
        recovered = restarted.recover_verified_cursor(self.manifest)

        self.assertIsNotNone(recovered)
        self.assertEqual(dispatched.cursor.head, recovered.head)
        self.assertEqual(worker_effects, tuple(factory.channel_state.channel_records))
        self.assertEqual("in_progress", port.task_status)
        self.assertEqual("dispatched", port.projection.canonical_state)
        self.assertIs(
            restarted.projection_results[-1].status,
            TrellisProjectionSyncStatus.APPLIED,
        )

    def test_revision_race_fails_closed_without_worker_redispatch(self) -> None:
        built, factory, _checkout, port, _repository, _runtime = self._new_runtime()
        port.race_once = True

        _identity, _prepared, dispatched = self._dispatch(built)

        self.assertIs(dispatched.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual(2, len(factory.channel_state.channel_records))
        self.assertEqual(1, len(port.apply_calls))
        self.assertEqual(_revision(1), port.apply_calls[0].expected_revision)
        self.assertEqual("planning", port.task_status)
        self.assertIsNone(port.projection)
        self.assertIs(
            built.projection_results[-1].status,
            TrellisProjectionSyncStatus.CONFLICT,
        )
        self.assertIs(
            built.projection_results[-1].reason,
            TrellisProjectionReason.REVISION_CONFLICT,
        )


if __name__ == "__main__":
    unittest.main()
