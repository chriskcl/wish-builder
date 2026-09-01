"""Focused fail-closed branch coverage for external-effect services."""

from __future__ import annotations

from wish_builder.adapters.fakes import FakeBackendChannelPort

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.services import test_backend_effects as _backend_effect_fixtures
from tests.services import test_external_recovery as _external_recovery_fixtures
from tests.processes.test_coordinator import CoordinatorHarness
from tests.services.test_execution_admission import WORKSPACE_HASH, admitted_events
from tests.services.test_backend_effects import (
    capabilities,
    plan,
)
from tests.services.test_external_recovery import (
    RecordingChannel,
    reserve_command,
)
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts.compatibility import Platform
from wish_builder.contracts.runtime import (
    EffectOperation,
    EffectStatus,
    JournalEventType,
)
from wish_builder.services.backend_admission import (
    BackendAdmissionReason,
    BackendAdmissionResult,
    admit_backend,
)
from wish_builder.services.checkpoints import CheckpointPolicy, CheckpointStore
from wish_builder.services.execution_admission import (
    ExecutionAdmissionReason,
    ExecutionAdmissionResult,
    admit_execution_snapshot,
)
from wish_builder.services.execution_checkpoints import (
    ExecutionCheckpointPublisher,
    ExecutionCheckpointReason,
    ExecutionCheckpointResult,
    ExecutionCheckpointStatus,
)
from wish_builder.services.journal import GENESIS_HEAD
from wish_builder.services.backend_effects import (
    BackendDispatchEffectReason,
    BackendDispatchResult,
    BackendDispatchEffectStatus,
)
from wish_builder.services.external_recovery import (
    BackendEffectRecoveryService,
    ExternalEffectRecoveryReason,
    ExternalEffectRecoveryResult,
    ExternalEffectRecoveryStatus,
)
from wish_builder.services.ports import ChannelObservation


class _Clock:
    def __init__(self, value: object = 0.0) -> None:
        self.value = value

    def __call__(self) -> object:
        return self.value


class BackendEffectsFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _backend_effect_fixtures.BackendDispatchEffectServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_parent_and_cancel_validation_block_before_adapter_calls(self) -> None:
        service = self.fixture.service()
        with self.assertRaisesRegex(ValueError, "same dispatch channel"):
            # The plan contract itself refuses cross-dispatch child operations.
            dataclasses.replace(
                plan(), send=dataclasses.replace(plan().send, dispatch_id="OTHER")
            )

        stale_parent = dataclasses.replace(
            self.fixture.parent.event.identity, coordinator_epoch=2
        )
        object.__setattr__(self.fixture.parent.event, "identity", stale_parent)
        result = service.dispatch(self.fixture.parent, plan())
        self.assertIs(result.status, BackendDispatchEffectStatus.BLOCKED)
        self.assertIs(
            result.reason, BackendDispatchEffectReason.PARENT_REQUEST_INVALID
        )

    def test_absent_child_effect_is_explicitly_blocked(self) -> None:
        dispatch_plan = plan()
        channel = FakeBackendChannelPort(capabilities())
        channel.reserve = (  # type: ignore[method-assign]
            lambda effect: ChannelObservation(
            effect.operation_id, EffectStatus.ABSENT, "2026-08-19T00:00:00Z"
            )
        )
        result = self.fixture.service(channel).dispatch(
            self.fixture.parent, dispatch_plan
        )
        self.assertIs(result.status, BackendDispatchEffectStatus.BLOCKED)
        self.assertIs(result.reason, BackendDispatchEffectReason.EFFECT_ABSENT)
        self.assertIsNotNone(result.receipt)

    def test_result_contract_rejects_incoherent_statuses(self) -> None:
        head = self.fixture.parent.append_result.head
        assert head is not None
        cases = (
            ("applied", BackendDispatchEffectReason.NONE),
            (BackendDispatchEffectStatus.BLOCKED, BackendDispatchEffectReason.NONE),
        )
        for status, reason in cases:
            with (
                self.subTest(status=status),
                self.assertRaises((TypeError, ValueError)),
            ):
                BackendDispatchResult(status, reason, head)  # type: ignore[arg-type]


class ExternalRecoveryFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _external_recovery_fixtures.BackendDispatchRecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_missing_retry_command_and_invalid_inspection_are_blocked(
        self,
    ) -> None:
        journal, evidence = self.fixture.fixture("required")
        command = reserve_command("RESERVE-REQUIRED")
        pending, head = self.fixture.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL
        )
        channel = RecordingChannel()
        required = self.fixture.backend_service(
            journal, channel, evidence
        ).reconcile(
            pending, expected_head=head
        )
        self.assertIs(required.reason, ExternalEffectRecoveryReason.RETRY_COMMAND_REQUIRED)

        journal, evidence = self.fixture.fixture("invalid")
        command = reserve_command("RESERVE-INVALID")
        pending, head = self.fixture.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL
        )
        channel = RecordingChannel()
        channel.inspect_reservation = (  # type: ignore[method-assign]
            lambda _operation_id: object()
        )
        invalid = self.fixture.backend_service(
            journal, channel, evidence
        ).reconcile(
            pending, expected_head=head, retry_command=command
        )
        self.assertIs(invalid.reason, ExternalEffectRecoveryReason.OBSERVATION_INVALID)

    def test_recovery_result_contract_rejects_success_without_evidence(self) -> None:
        with self.assertRaises(ValueError):
            ExternalEffectRecoveryResult(
                ExternalEffectRecoveryStatus.RECONCILED,
                ExternalEffectRecoveryReason.NONE,
                GENESIS_HEAD,
            )

    def test_constructor_rejects_non_callable_admission(self) -> None:
        journal, evidence = self.fixture.fixture("constructor")
        with self.assertRaises(TypeError):
            BackendEffectRecoveryService(
                journal,
                RecordingChannel(),
                evidence,
                coordinator_id="coordinator-001",
                fencing_token=1,
                retry_admitted=True,  # type: ignore[arg-type]
            )


class ExecutionCheckpointFailClosedTests(unittest.TestCase):
    def test_constructor_and_clock_failures_are_rejected_or_blocked(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = CoordinatorHarness(root)
            store = CheckpointStore(root / "checkpoints")
            with self.assertRaises(ValueError):
                ExecutionCheckpointPublisher(
                    harness.manifest,
                    harness.journal,
                    store,
                    monotonic_clock=_Clock(float("nan")),
                )
            publisher = ExecutionCheckpointPublisher(
                harness.manifest,
                harness.journal,
                store,
                policy=CheckpointPolicy(event_interval=1),
                previous_sequence=harness.coordinator.cursor.head.sequence,
                monotonic_clock=_Clock(0.0),
            )
            step = harness.coordinator.dispatch_ready()
            publisher._monotonic_clock = _Clock(  # type: ignore[assignment]
                "not-a-number"
            )
            result = publisher.observe(
                step.cursor.snapshot,
                step.cursor.graph_index,
                step.cursor.head,
                step.events[-1],
            )
            self.assertIs(result.status, ExecutionCheckpointStatus.BLOCKED)
            self.assertIs(result.reason, ExecutionCheckpointReason.PUBLISH_FAILED)

    def test_result_contract_rejects_wrong_state_combinations(self) -> None:
        for status, reason in (
            ("blocked", ExecutionCheckpointReason.STATE_MISMATCH),
            (ExecutionCheckpointStatus.SKIPPED, ExecutionCheckpointReason.NONE),
            (ExecutionCheckpointStatus.BLOCKED, ExecutionCheckpointReason.NOT_DUE),
        ):
            with (
                self.subTest(status=status),
                self.assertRaises((TypeError, ValueError)),
            ):
                ExecutionCheckpointResult(status, reason)  # type: ignore[arg-type]


class AdmissionFailClosedTests(unittest.TestCase):
    def test_backend_type_scheduler_and_provider_guards(self) -> None:
        bundle = load_bundled_compatibility()
        manifest, _ = admitted_events()
        with self.assertRaises(TypeError):
            admit_backend(
                object(), bundle=bundle, platform=Platform.WINDOWS
            )  # type: ignore[arg-type]
        object.__setattr__(manifest, "scheduler_mode", object())
        self.assertIs(
            admit_backend(manifest, bundle=bundle, platform=Platform.WINDOWS).reason,
            BackendAdmissionReason.SCHEDULER_MISMATCH,
        )
        manifest, _ = admitted_events()
        with mock.patch("wish_builder.services.backend_admission._PROVIDERS", {}):
            self.assertIs(
                admit_backend(
                    manifest, bundle=bundle, platform=Platform.WINDOWS
                ).reason,
                BackendAdmissionReason.PROVIDER_MISMATCH,
            )
        with self.assertRaises((TypeError, ValueError)):
            BackendAdmissionResult(True, BackendAdmissionReason.NONE)

    def test_execution_admission_type_missing_and_link_guards(
        self,
    ) -> None:
        manifest, events = admitted_events()
        with self.assertRaises(TypeError):
            admit_execution_snapshot(
                object(), tuple(events), workspace_hash=WORKSPACE_HASH
            )  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            admit_execution_snapshot(manifest, tuple(events), workspace_hash="bad")
        request_index = next(
            index
            for index, event in enumerate(events)
            if event.event_type is JournalEventType.DECISION_REQUESTED
        )
        without_request = tuple(events[:request_index])
        without_decision = tuple(events[: request_index + 1])
        self.assertIs(
            admit_execution_snapshot(
                manifest, without_request, workspace_hash=WORKSPACE_HASH
            ).reason,
            ExecutionAdmissionReason.GATE_B_REQUEST_MISSING,
        )
        self.assertIs(
            admit_execution_snapshot(
                manifest, without_decision, workspace_hash=WORKSPACE_HASH
            ).reason,
            ExecutionAdmissionReason.GATE_B_DECISION_MISSING,
        )
        with self.assertRaises((TypeError, ValueError)):
            ExecutionAdmissionResult(True, ExecutionAdmissionReason.NONE)


if __name__ == "__main__":
    unittest.main()
