"""Additional fail-closed branch coverage for execution support services."""

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
from tests.services.test_attempt_deadlines import (
    deadline,
    evidence_ref,
    identity,
    reconciliation_evidence,
)
from tests.services.test_execution_admission import WORKSPACE_HASH, admitted_events
from tests.services.test_backend_effects import (
    capabilities,
    plan,
)
from tests.services.test_external_recovery import (
    HASH_A,
    RecordingChannel,
    reserve_command,
)
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts.compatibility import Platform
from wish_builder.contracts.runtime import (
    EffectOperation,
    EffectStatus,
    ExecutionIdentity,
)
from wish_builder.services.attempt_deadlines import (
    AttemptActionPermissions,
    AttemptClockContinuity,
    AttemptClockSample,
    AttemptDeadline,
    AttemptDeadlineAssessment,
    AttemptDeadlineReason,
    AttemptDeadlineState,
    AttemptReconciliationEvidence,
    AttemptReconciliationReason,
    AttemptReconciliationResult,
    AttemptReconciliationStatus,
    evaluate_attempt_deadline,
    reconcile_attempt_deadline,
)
from wish_builder.services.backend_admission import (
    BackendAdmissionReason,
    BackendAdmissionResult,
    admit_backend,
    current_platform,
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
from wish_builder.services.ports import ChannelObservation
from wish_builder.services.backend_effects import (
    BackendDispatchEffectReason,
    BackendDispatchResult,
    BackendDispatchEffectService,
    BackendDispatchEffectStatus,
    BackendDispatchPlan,
)
from wish_builder.services.external_recovery import (
    BackendEffectRecoveryService,
    ExternalEffectRecoveryReason,
    ExternalEffectRecoveryResult,
    ExternalEffectRecoveryStatus,
)


class _NonDurableEvidenceStore:
    def put(self, observation, *, identity, operation):  # type: ignore[no-untyped-def]
        return object()


class _Clock:
    def __init__(self, value: object = 0.0) -> None:
        self.value = value

    def __call__(self) -> object:
        return self.value


class AttemptDeadlineContractClosureTests(unittest.TestCase):
    def test_clock_deadline_and_assessment_reject_incoherent_inputs(self) -> None:
        valid = deadline()
        cases = (
            lambda: AttemptClockContinuity(" boot", 1, "process"),
            lambda: AttemptClockSample(object(), 1, "2026-08-19T00:00:00Z"),
            lambda: AttemptDeadline(
                identity(), object(), 1, 2, "2026-08-19T00:00:00Z", "2026-08-19T00:01:00Z"
            ),
            lambda: dataclasses.replace(valid, last_observed_monotonic=0),
            lambda: AttemptDeadlineAssessment(
                identity(), object(), AttemptDeadlineReason.BEFORE_DEADLINE, valid
            ),
            lambda: AttemptDeadlineAssessment(
                identity(), AttemptDeadlineState.ACTIVE, object(), valid
            ),
            lambda: AttemptDeadlineAssessment(
                ExecutionIdentity("RUN-OTHER", 1, "TASK-001", 1, "OP-001"),
                AttemptDeadlineState.ACTIVE,
                AttemptDeadlineReason.BEFORE_DEADLINE,
                valid,
            ),
            lambda: AttemptDeadlineAssessment(
                identity(),
                AttemptDeadlineState.ACTIVE,
                AttemptDeadlineReason.BEFORE_DEADLINE,
                valid,
                object(),
            ),
            lambda: AttemptDeadlineAssessment(
                identity(),
                AttemptDeadlineState.ACTIVE,
                AttemptDeadlineReason.BEFORE_DEADLINE,
                valid,
                AttemptActionPermissions(retry_allowed=True),
            ),
            lambda: AttemptDeadlineAssessment(
                identity(),
                AttemptDeadlineState.EXPIRED,
                AttemptDeadlineReason.BEFORE_DEADLINE,
                valid,
            ),
        )
        for make_value in cases:
            with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                make_value()

        with self.assertRaises(TypeError):
            evaluate_attempt_deadline(object(), AttemptClockSample(valid.continuity, 1, "2026-08-19T00:00:00Z"))
        with self.assertRaises(TypeError):
            evaluate_attempt_deadline(valid, object())

    def test_reconciliation_contracts_refuse_ambiguous_or_unsafe_results(self) -> None:
        evidence = reconciliation_evidence().evidence
        valid = reconciliation_evidence()
        invalid_evidence = (
            lambda: AttemptReconciliationEvidence(
                identity(), object(), (), (), True, ()
            ),
            lambda: AttemptReconciliationEvidence(
                identity(), EffectOperation.CLEANUP, (), (), True, ()
            ),
            lambda: AttemptReconciliationEvidence(
                identity(), EffectOperation.TASK_EXECUTION, [], (), True, ()
            ),
            lambda: AttemptReconciliationEvidence(
                identity(), EffectOperation.TASK_EXECUTION, (), (), 1, ()
            ),
            lambda: AttemptReconciliationEvidence(
                identity(), EffectOperation.TASK_EXECUTION, (), (), True, []
            ),
            lambda: AttemptReconciliationEvidence(
                identity(), EffectOperation.TASK_EXECUTION, (), (), True, (evidence[0], evidence[0])
            ),
        )
        for make_value in invalid_evidence:
            with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                make_value()

        fingerprint = "sha256:" + "a" * 64
        invalid_results = (
            lambda: AttemptReconciliationResult(identity(), object(), AttemptReconciliationReason.OUTCOME_MISSING, fingerprint),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.BLOCKED, object(), fingerprint),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.BLOCKED, AttemptReconciliationReason.OUTCOME_MISSING, "bad"),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.BLOCKED, AttemptReconciliationReason.OUTCOME_MISSING, fingerprint, object()),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.BLOCKED, AttemptReconciliationReason.OUTCOME_MISSING, fingerprint, idempotent=1),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.BLOCKED, AttemptReconciliationReason.OUTCOME_MISSING, fingerprint, idempotent=True),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.BLOCKED, AttemptReconciliationReason.OUTCOME_MISSING, fingerprint, permissions=AttemptActionPermissions(cleanup_allowed=True)),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.BLOCKED, AttemptReconciliationReason.ABSENT_CONFIRMED, fingerprint),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.COMPLETED, AttemptReconciliationReason.OUTCOME_MISSING, fingerprint),
            lambda: AttemptReconciliationResult(identity(), AttemptReconciliationStatus.COMPLETED, AttemptReconciliationReason.ABSENT_CONFIRMED, fingerprint),
        )
        for make_value in invalid_results:
            with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                make_value()
        with self.assertRaises(TypeError):
            reconcile_attempt_deadline(valid, previous=object())


class TrellisFailureClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.effects = _backend_effect_fixtures.BackendDispatchEffectServiceTests()
        self.effects.setUp()
        self.addCleanup(self.effects.doCleanups)
        self.recovery = _external_recovery_fixtures.BackendDispatchRecoveryTests()
        self.recovery.setUp()
        self.addCleanup(self.recovery.doCleanups)

    def test_dispatch_plan_and_result_contracts_reject_invalid_shapes(self) -> None:
        dispatch_plan = plan()
        head = self.effects.parent.append_result.head
        assert head is not None
        invalid_values = (
            lambda: BackendDispatchPlan(object(), dispatch_plan.send),
            lambda: BackendDispatchPlan(dispatch_plan.reserve, object()),
            lambda: BackendDispatchPlan(
                dispatch_plan.reserve,
                dataclasses.replace(dispatch_plan.send, operation_id=dispatch_plan.reserve.operation_id),
            ),
            lambda: BackendDispatchResult(BackendDispatchEffectStatus.BLOCKED, object(), head),
            lambda: BackendDispatchResult(BackendDispatchEffectStatus.BLOCKED, BackendDispatchEffectReason.JOURNAL_CONFLICT, object()),
            lambda: BackendDispatchResult(BackendDispatchEffectStatus.BLOCKED, BackendDispatchEffectReason.JOURNAL_CONFLICT, head, []),
            lambda: BackendDispatchResult(BackendDispatchEffectStatus.BLOCKED, BackendDispatchEffectReason.JOURNAL_CONFLICT, head, receipt=object()),
            lambda: BackendDispatchResult(BackendDispatchEffectStatus.BLOCKED, BackendDispatchEffectReason.JOURNAL_CONFLICT, head, reservation=object()),
            lambda: BackendDispatchResult(BackendDispatchEffectStatus.BLOCKED, BackendDispatchEffectReason.JOURNAL_CONFLICT, head, turn=object()),
        )
        for make_value in invalid_values:
            with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                make_value()

    def test_malformed_or_nondurable_child_results_block_before_send(self) -> None:
        dispatch_plan = plan()
        malformed_channel = FakeBackendChannelPort(capabilities())
        malformed_channel.reserve = lambda _effect: object()  # type: ignore[method-assign]
        malformed = self.effects.service(malformed_channel).dispatch(self.effects.parent, dispatch_plan)
        self.assertIs(malformed.reason, BackendDispatchEffectReason.OBSERVATION_INVALID)

        nondurable = BackendDispatchEffectService(
            self.effects.journal,
            FakeBackendChannelPort(capabilities()),
            _NonDurableEvidenceStore(),
            coordinator_id="coordinator-001",
            fencing_token=1,
        ).dispatch(self.effects.parent, dispatch_plan)
        self.assertIs(nondurable.reason, BackendDispatchEffectReason.EVIDENCE_NOT_DURABLE)
        self.assertIsNone(nondurable.receipt)

    def test_recovery_blocks_nondurable_evidence_retry_exceptions_and_invalid_arguments(self) -> None:
        journal, evidence = self.recovery.fixture("phase2")
        command = reserve_command("RESERVE-PHASE2")
        pending, head = self.recovery.append_request(journal, command, EffectOperation.RESERVE_CHANNEL)
        channel = RecordingChannel()
        channel.reservations[command.operation_id] = ChannelObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            "2026-08-19T00:00:00Z",
            HASH_A,
            command.attempt_id,
            command.channel_id,
            command.provider,
            "SESSION-PHASE2",
        )
        nondurable = BackendEffectRecoveryService(
            journal,
            channel,
            _NonDurableEvidenceStore(),
            coordinator_id="coordinator-001",
            fencing_token=1,
            retry_admitted=lambda: True,
        ).reconcile(pending, expected_head=head)
        self.assertIs(nondurable.reason, ExternalEffectRecoveryReason.EVIDENCE_NOT_DURABLE)

        retry_journal, retry_evidence = self.recovery.fixture("retry-error")
        retry_command = reserve_command("RESERVE-RETRY-ERROR")
        retry_pending, retry_head = self.recovery.append_request(
            retry_journal, retry_command, EffectOperation.RESERVE_CHANNEL
        )
        retry_channel = RecordingChannel()
        retry_channel.reserve = mock.Mock(side_effect=OSError("adapter lost"))  # type: ignore[method-assign]
        failed_retry = self.recovery.backend_service(
            retry_journal, retry_channel, retry_evidence
        ).reconcile(retry_pending, expected_head=retry_head, retry_command=retry_command)
        self.assertIs(failed_retry.reason, ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN)

        service = self.recovery.backend_service(journal, channel, evidence)
        with self.assertRaises(TypeError):
            service.reconcile(object(), expected_head=head)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            service.reconcile(pending, expected_head=object())  # type: ignore[arg-type]

    def test_recovery_constructor_and_result_contracts_fail_closed(self) -> None:
        journal, evidence = self.recovery.fixture("contracts")
        channel = RecordingChannel()
        for args in (
            (object(), channel, evidence, "coordinator-001", 1, lambda: True),
            (journal, object(), evidence, "coordinator-001", 1, lambda: True),
            (journal, channel, object(), "coordinator-001", 1, lambda: True),
            (journal, channel, evidence, "", 1, lambda: True),
            (journal, channel, evidence, "coordinator-001", 0, lambda: True),
        ):
            with self.subTest(args=args), self.assertRaises((TypeError, ValueError)):
                BackendEffectRecoveryService(
                    args[0], args[1], args[2], coordinator_id=args[3], fencing_token=args[4], retry_admitted=args[5]
                )
        invalid_results = (
            lambda: ExternalEffectRecoveryResult(object(), ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN, GENESIS_HEAD),
            lambda: ExternalEffectRecoveryResult(ExternalEffectRecoveryStatus.BLOCKED, object(), GENESIS_HEAD),
            lambda: ExternalEffectRecoveryResult(ExternalEffectRecoveryStatus.BLOCKED, ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN, object()),
            lambda: ExternalEffectRecoveryResult(ExternalEffectRecoveryStatus.BLOCKED, ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN, GENESIS_HEAD, observation=object()),
            lambda: ExternalEffectRecoveryResult(ExternalEffectRecoveryStatus.BLOCKED, ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN, GENESIS_HEAD, receipt=object()),
            lambda: ExternalEffectRecoveryResult(ExternalEffectRecoveryStatus.BLOCKED, ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN, GENESIS_HEAD, event=object()),
            lambda: ExternalEffectRecoveryResult(ExternalEffectRecoveryStatus.BLOCKED, ExternalEffectRecoveryReason.NONE, GENESIS_HEAD),
        )
        for make_value in invalid_results:
            with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                make_value()


class AdmissionAndCheckpointClosureTests(unittest.TestCase):
    def test_backend_platform_and_result_contracts_cover_host_and_type_guards(self) -> None:
        with mock.patch("wish_builder.services.backend_admission.host_platform.system", return_value="Windows"):
            self.assertIs(current_platform(), Platform.WINDOWS)
        with mock.patch("wish_builder.services.backend_admission.host_platform.system", return_value="Linux"):
            self.assertIs(current_platform(), Platform.LINUX)
        invalid_results = (
            lambda: BackendAdmissionResult(1, BackendAdmissionReason.UNSUPPORTED_HOST),
            lambda: BackendAdmissionResult(False, object()),
            lambda: BackendAdmissionResult(False, BackendAdmissionReason.UNSUPPORTED_HOST, object()),
            lambda: BackendAdmissionResult(False, BackendAdmissionReason.NONE),
        )
        for make_value in invalid_results:
            with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                make_value()
        manifest, _ = admitted_events()
        bundle = load_bundled_compatibility()
        with self.assertRaises(TypeError):
            admit_backend(manifest, bundle=object(), platform=Platform.WINDOWS)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            admit_backend(manifest, bundle=bundle, platform=object())  # type: ignore[arg-type]

    def test_execution_admission_and_checkpoint_contracts_reject_bad_evidence(self) -> None:
        manifest, events = admitted_events()
        with self.assertRaises(TypeError):
            admit_execution_snapshot(manifest, list(events), workspace_hash=WORKSPACE_HASH)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            admit_execution_snapshot(manifest, (object(),), workspace_hash=WORKSPACE_HASH)  # type: ignore[arg-type]
        invalid_admissions = (
            lambda: ExecutionAdmissionResult(1, ExecutionAdmissionReason.JOURNAL_EMPTY),
            lambda: ExecutionAdmissionResult(False, object()),
            lambda: ExecutionAdmissionResult(False, ExecutionAdmissionReason.JOURNAL_EMPTY, object()),
            lambda: ExecutionAdmissionResult(False, ExecutionAdmissionReason.NONE),
        )
        for make_value in invalid_admissions:
            with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                make_value()

        invalid_checkpoints = (
            lambda: ExecutionCheckpointResult(ExecutionCheckpointStatus.SKIPPED, object()),
            lambda: ExecutionCheckpointResult(ExecutionCheckpointStatus.PUBLISHED, ExecutionCheckpointReason.NONE),
            lambda: ExecutionCheckpointResult(ExecutionCheckpointStatus.BLOCKED, ExecutionCheckpointReason.STATE_MISMATCH, object()),
            lambda: ExecutionCheckpointResult(ExecutionCheckpointStatus.SKIPPED, ExecutionCheckpointReason.STATE_MISMATCH),
        )
        for make_value in invalid_checkpoints:
            with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                make_value()

    def test_checkpoint_constructor_and_rollback_clock_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = CoordinatorHarness(root)
            store = CheckpointStore(root / "checkpoints")
            invalid = (
                lambda: ExecutionCheckpointPublisher(object(), harness.journal, store),
                lambda: ExecutionCheckpointPublisher(harness.manifest, object(), store),
                lambda: ExecutionCheckpointPublisher(harness.manifest, harness.journal, object()),
                lambda: ExecutionCheckpointPublisher(harness.manifest, harness.journal, store, policy=object()),
                lambda: ExecutionCheckpointPublisher(harness.manifest, harness.journal, store, previous_sequence=-1),
                lambda: ExecutionCheckpointPublisher(harness.manifest, harness.journal, store, monotonic_clock=object()),
            )
            for make_value in invalid:
                with self.subTest(make_value=make_value), self.assertRaises((TypeError, ValueError)):
                    make_value()
            clock = _Clock(10.0)
            publisher = ExecutionCheckpointPublisher(
                harness.manifest,
                harness.journal,
                store,
                policy=CheckpointPolicy(event_interval=1),
                previous_sequence=harness.coordinator.cursor.head.sequence,
                monotonic_clock=clock,
            )
            clock.value = 9.0
            step = harness.coordinator.dispatch_ready()
            result = publisher.observe(
                step.cursor.snapshot, step.cursor.graph_index, step.cursor.head, step.events[-1]
            )
            self.assertIs(result.reason, ExecutionCheckpointReason.PUBLISH_FAILED)


if __name__ == "__main__":
    unittest.main()
