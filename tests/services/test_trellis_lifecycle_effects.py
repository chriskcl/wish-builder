from __future__ import annotations

from wish_builder.adapters import FilesystemExternalEvidenceStore

import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.adapters.trellis import (
    FakeTrellisLifecyclePort,
    FakeExternalState,
)
from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEventType,
)
from wish_builder.services import (
    GENESIS_HEAD,
    CheckAttemptEffectResult,
    DurableJournal,
    FinishAttemptEffectResult,
    PrepareAttemptEffectResult,
    TrellisLifecycleEffectCrash,
    TrellisLifecycleEffectReason,
    TrellisLifecycleEffectService,
    TrellisLifecycleEffectStatus,
    project_pending_external_effects,
)
from wish_builder.services import trellis_lifecycle_effects as lifecycle_module
from wish_builder.services.journal import (
    AppendResult,
    AppendStatus,
    JournalFaultCode,
    JournalHead,
)
from wish_builder.services.ports import (
    AttemptObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PrepareAttempt,
    TrellisLifecycleState,
)


RUN_ID = "RUN-TRELLIS-LIFECYCLE"
TASK_ID = "TASK-001"
TRELLIS_TASK_ID = "trellis-task-001"
DISPATCH_ID = "DISPATCH-001"
BASE_COMMIT = "a" * 40
HEAD_COMMIT = "b" * 40
DELIVERED_COMMIT = "c" * 40
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def identity(*, epoch: int = 7) -> ExecutionIdentity:
    return ExecutionIdentity(RUN_ID, epoch, TASK_ID, 1, DISPATCH_ID)


def prepare_command(
    *,
    operation_id: str = "TRELLIS-PREPARE-001",
    expected_base_commit: str = BASE_COMMIT,
) -> PrepareAttempt:
    return PrepareAttempt(
        operation_id=operation_id,
        run_id=RUN_ID,
        parent_task_id="trellis-parent-001",
        trellis_task_id=TRELLIS_TASK_ID,
        task_id=TASK_ID,
        attempt=1,
        dispatch_id=DISPATCH_ID,
        manifest_digest=HASH_A,
        trellis_graph_digest=HASH_B,
        expected_base_commit=expected_base_commit,
    )


def check_command(attempt_id: str) -> CheckAttempt:
    return CheckAttempt(
        operation_id="TRELLIS-CHECK-001",
        attempt_id=attempt_id,
        trellis_task_id=TRELLIS_TASK_ID,
        task_id=TASK_ID,
        task_packet_digest=HASH_A,
        expected_head_commit=HEAD_COMMIT,
    )


def finish_command(attempt_id: str) -> FinishAttempt:
    return FinishAttempt(
        operation_id="TRELLIS-FINISH-001",
        attempt_id=attempt_id,
        trellis_task_id=TRELLIS_TASK_ID,
        task_id=TASK_ID,
        delivered_commit=DELIVERED_COMMIT,
        delivery_evidence_digest=HASH_B,
    )


class _WrongPrepareObservationPort:
    def __init__(self) -> None:
        self.delegate = FakeTrellisLifecyclePort()

    def prepare_attempt(self, effect):
        return AttemptObservation(
            operation_id="TRELLIS-PREPARE-WRONG",
            status=EffectStatus.UNKNOWN,
            observed_at="2026-08-19T00:00:00Z",
            lifecycle_state=TrellisLifecycleState.UNKNOWN,
            evidence=("wrong_operation_id",),
        )

    def check_attempt(self, effect):
        return self.delegate.check_attempt(effect)

    def finish_attempt(self, effect):
        return self.delegate.finish_attempt(effect)

    def inspect_attempt(self, operation_id):
        return self.delegate.inspect_attempt(operation_id)

    def inspect_check(self, operation_id):
        return self.delegate.inspect_check(operation_id)

    def inspect_finish(self, operation_id):
        return self.delegate.inspect_finish(operation_id)


class _NonDurableEvidenceStore:
    def put(self, observation, *, identity, operation):
        return object()


class _InvalidCheckObservationPort:
    def __init__(self) -> None:
        self.delegate = FakeTrellisLifecyclePort()

    def prepare_attempt(self, effect):
        return self.delegate.prepare_attempt(effect)

    def check_attempt(self, effect):
        return CheckObservation(
            operation_id="TRELLIS-CHECK-WRONG",
            status=EffectStatus.UNKNOWN,
            observed_at="2026-08-19T00:00:00Z",
            evidence=("wrong_operation_id",),
        )

    def finish_attempt(self, effect):
        return self.delegate.finish_attempt(effect)

    def inspect_attempt(self, operation_id):
        return self.delegate.inspect_attempt(operation_id)

    def inspect_check(self, operation_id):
        return self.delegate.inspect_check(operation_id)

    def inspect_finish(self, operation_id):
        return self.delegate.inspect_finish(operation_id)


class _AbsentPreparePort(FakeTrellisLifecyclePort):
    def prepare_attempt(self, effect):
        return AttemptObservation(
            operation_id=effect.operation_id,
            status=EffectStatus.ABSENT,
            observed_at="2026-08-19T00:00:00Z",
            lifecycle_state=TrellisLifecycleState.ABSENT,
        )


class TrellisLifecycleEffectServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(self.root / "journal", RUN_ID),
        )
        self.evidence = FilesystemExternalEvidenceStore(self.root / "evidence")
        self.state = FakeExternalState()

    def service(self, lifecycle=None, *, evidence=None, failpoint=None):
        return TrellisLifecycleEffectService(
            self.journal,
            lifecycle or FakeTrellisLifecyclePort(state=self.state),
            evidence or self.evidence,
            coordinator_id="coordinator-001",
            fencing_token=7,
            failpoint=failpoint,
        )

    def test_vocabulary_and_full_lifecycle_are_durable_and_typed(self) -> None:
        self.assertEqual("prepare_attempt", EffectOperation.PREPARE_ATTEMPT.value)
        self.assertEqual("check_attempt", EffectOperation.CHECK_ATTEMPT.value)
        self.assertEqual("finish_attempt", EffectOperation.FINISH_ATTEMPT.value)
        self.assertEqual("attempt", EffectObjectType.ATTEMPT.value)

        prepared = self.service().prepare(
            identity(), prepare_command(), expected_head=GENESIS_HEAD
        )
        self.assertIs(type(prepared), PrepareAttemptEffectResult)
        self.assertIs(prepared.status, TrellisLifecycleEffectStatus.APPLIED)
        self.assertIs(prepared.reason, TrellisLifecycleEffectReason.NONE)
        assert prepared.observation is not None
        attempt_id = prepared.observation.attempt_id
        assert attempt_id is not None

        checked = self.service().check(
            identity(), check_command(attempt_id), expected_head=prepared.head
        )
        self.assertIs(type(checked), CheckAttemptEffectResult)
        self.assertIs(checked.status, TrellisLifecycleEffectStatus.APPLIED)
        assert checked.observation is not None
        self.assertTrue(checked.observation.passed)

        finished = self.service().finish(
            identity(), finish_command(attempt_id), expected_head=checked.head
        )
        self.assertIs(type(finished), FinishAttemptEffectResult)
        self.assertIs(finished.status, TrellisLifecycleEffectStatus.APPLIED)
        assert finished.observation is not None
        self.assertTrue(finished.observation.finished)
        self.assertEqual(3, len(self.state.lifecycle_records))

        all_events = prepared.events + checked.events + finished.events
        self.assertEqual(
            (JournalEventType.EFFECT_REQUESTED, JournalEventType.EFFECT_OBSERVED) * 3,
            tuple(event.event_type for event in all_events),
        )
        requests = all_events[::2]
        observations = all_events[1::2]
        self.assertEqual(
            (
                EffectOperation.PREPARE_ATTEMPT,
                EffectOperation.CHECK_ATTEMPT,
                EffectOperation.FINISH_ATTEMPT,
            ),
            tuple(event.payload.operation for event in requests),
        )
        for request in requests:
            self.assertIs(type(request.payload), EffectRequestPayload)
            self.assertIs(request.payload.adapter, AdapterKind.TRELLIS)
            self.assertIs(request.payload.object_type, EffectObjectType.ATTEMPT)
            self.assertEqual(7, request.payload.fencing_token)
        for observed in observations:
            self.assertIs(type(observed.payload), EffectObservationPayload)
            receipt = observed.payload.receipt
            self.assertEqual(observed.identity.correlation_id, receipt.identity.correlation_id)
            self.assertEqual(1, len(receipt.evidence))
            evidence_ref = receipt.evidence[0]
            raw = self.evidence.read(evidence_ref.digest)
            self.assertEqual(
                evidence_ref.digest,
                "sha256:" + hashlib.sha256(raw).hexdigest(),
            )
        self.assertEqual((), project_pending_external_effects(all_events))

    def test_request_hash_and_normalized_target_bind_the_caller_command(self) -> None:
        command = prepare_command()
        result = self.service().prepare(
            identity(), command, expected_head=GENESIS_HEAD
        )

        request = result.events[0]
        assert type(request.payload) is EffectRequestPayload
        self.assertEqual(command.operation_id, request.identity.correlation_id)
        self.assertEqual(command.canonical_sha256(), request.payload.request_payload_hash)
        self.assertEqual(
            "sha256:"
            + canonical_sha256(
                {
                    "adapter": "trellis",
                    "attempt": 1,
                    "dispatch_id": DISPATCH_ID,
                    "operation": "prepare_attempt",
                    "parent_task_id": "trellis-parent-001",
                    "run_id": RUN_ID,
                    "task_id": TASK_ID,
                    "trellis_task_id": TRELLIS_TASK_ID,
                }
            ),
            request.payload.normalized_target_hash,
        )

    def test_unknown_is_durably_observed_and_remains_blocked(self) -> None:
        command = prepare_command()
        lifecycle = FakeTrellisLifecyclePort(
            state=self.state,
            unknown_operation_ids={command.operation_id},
        )
        result = self.service(lifecycle).prepare(
            identity(), command, expected_head=GENESIS_HEAD
        )

        self.assertIs(result.status, TrellisLifecycleEffectStatus.BLOCKED)
        self.assertIs(
            result.reason,
            TrellisLifecycleEffectReason.EFFECT_OUTCOME_UNKNOWN,
        )
        self.assertEqual(2, len(result.events))
        assert result.receipt is not None
        self.assertIs(result.receipt.status, EffectStatus.UNKNOWN)
        self.assertTrue(result.receipt.evidence)
        self.assertIsNotNone(result.observation)
        self.assertEqual(
            (result.events[0],),
            tuple(
                pending.request_event
                for pending in project_pending_external_effects(result.events)
            ),
        )

    def test_absent_lifecycle_observation_does_not_clear_pending_request(self) -> None:
        result = self.service(_AbsentPreparePort()).prepare(
            identity(), prepare_command(), expected_head=GENESIS_HEAD
        )

        self.assertIs(result.status, TrellisLifecycleEffectStatus.BLOCKED)
        self.assertIs(result.reason, TrellisLifecycleEffectReason.EFFECT_ABSENT)
        self.assertEqual(2, len(result.events))
        self.assertEqual(
            (result.events[0],),
            tuple(
                pending.request_event
                for pending in project_pending_external_effects(result.events)
            ),
        )

    def test_crash_after_adapter_call_retries_the_same_operation_exactly_once(self) -> None:
        command = prepare_command()

        def failpoint(point: str, operation_id: str) -> None:
            if point == "after_adapter_call" and operation_id == command.operation_id:
                raise TrellisLifecycleEffectCrash(point)

        with self.assertRaises(TrellisLifecycleEffectCrash):
            self.service(failpoint=failpoint).prepare(
                identity(), command, expected_head=GENESIS_HEAD
            )
        self.assertEqual(1, len(self.state.lifecycle_records))

        recovered = self.service().prepare(
            identity(), command, expected_head=GENESIS_HEAD
        )

        self.assertIs(recovered.status, TrellisLifecycleEffectStatus.APPLIED)
        self.assertEqual(1, len(self.state.lifecycle_records))
        self.assertEqual(2, len(recovered.events))

    def test_operation_id_collision_is_durable_unknown(self) -> None:
        original = prepare_command()
        first = self.service().prepare(
            identity(), original, expected_head=GENESIS_HEAD
        )
        changed = dataclasses.replace(
            original,
            expected_base_commit="d" * 40,
        )
        collision = self.service().prepare(
            identity(), changed, expected_head=first.head
        )

        self.assertIs(collision.status, TrellisLifecycleEffectStatus.BLOCKED)
        self.assertIs(
            collision.reason,
            TrellisLifecycleEffectReason.EFFECT_OUTCOME_UNKNOWN,
        )
        self.assertEqual(2, len(collision.events))
        assert collision.observation is not None
        self.assertIn("operation_id_collision", collision.observation.evidence[0])

    def test_stale_fence_and_cross_task_command_stop_before_journal(self) -> None:
        stale = self.service().prepare(
            identity(epoch=6), prepare_command(), expected_head=GENESIS_HEAD
        )
        wrong_task = dataclasses.replace(prepare_command(), task_id="TASK-OTHER")
        mismatched = self.service().prepare(
            identity(), wrong_task, expected_head=GENESIS_HEAD
        )

        for result in (stale, mismatched):
            self.assertIs(result.status, TrellisLifecycleEffectStatus.BLOCKED)
            self.assertIs(result.reason, TrellisLifecycleEffectReason.REQUEST_INVALID)
            self.assertEqual(GENESIS_HEAD, result.head)
            self.assertFalse(result.events)
        self.assertFalse((self.root / "journal" / "segments").exists())

    def test_wrong_observation_identity_is_not_saved_or_journaled(self) -> None:
        result = self.service(_WrongPrepareObservationPort()).prepare(
            identity(), prepare_command(), expected_head=GENESIS_HEAD
        )

        self.assertIs(result.status, TrellisLifecycleEffectStatus.BLOCKED)
        self.assertIs(
            result.reason,
            TrellisLifecycleEffectReason.OBSERVATION_INVALID,
        )
        self.assertEqual(1, len(result.events))
        self.assertIsNone(result.observation)
        self.assertFalse((self.root / "evidence" / "objects").exists())

    def test_non_durable_evidence_stops_before_effect_observation(self) -> None:
        result = self.service(evidence=_NonDurableEvidenceStore()).prepare(
            identity(), prepare_command(), expected_head=GENESIS_HEAD
        )

        self.assertIs(result.status, TrellisLifecycleEffectStatus.BLOCKED)
        self.assertIs(
            result.reason,
            TrellisLifecycleEffectReason.EVIDENCE_NOT_DURABLE,
        )
        self.assertEqual(1, len(result.events))
        self.assertIsNotNone(result.observation)
        self.assertIsNone(result.receipt)

    def test_target_hash_rejects_incomplete_or_cross_boundary_inputs(self) -> None:
        command = prepare_command()
        invalid_calls = (
            lambda: lifecycle_module.lifecycle_target_object_hash(
                ExecutionIdentity(RUN_ID, 7),
                command,
                EffectOperation.PREPARE_ATTEMPT,
            ),
            lambda: lifecycle_module.lifecycle_target_object_hash(
                identity(), object(), EffectOperation.PREPARE_ATTEMPT
            ),
            lambda: lifecycle_module.lifecycle_target_object_hash(
                identity(), command, EffectOperation.CHECK_ATTEMPT
            ),
            lambda: lifecycle_module.lifecycle_target_object_hash(
                identity(),
                dataclasses.replace(command, task_id="TASK-OTHER"),
                EffectOperation.PREPARE_ATTEMPT,
            ),
            lambda: lifecycle_module.lifecycle_target_object_hash(
                identity(),
                dataclasses.replace(command, run_id="RUN-OTHER"),
                EffectOperation.PREPARE_ATTEMPT,
            ),
        )
        for invalid_call in invalid_calls:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(
                (TypeError, ValueError)
            ):
                invalid_call()

    def test_result_contract_rejects_incoherent_status_receipt_and_chain(self) -> None:
        valid = self.service().prepare(
            identity(), prepare_command(), expected_head=GENESIS_HEAD
        )
        assert valid.receipt is not None
        assert valid.observation is not None
        wrong_operation = dataclasses.replace(
            valid.receipt, operation=EffectOperation.CHECK_ATTEMPT
        )
        wrong_identity = dataclasses.replace(
            valid.receipt,
            identity=dataclasses.replace(
                valid.receipt.identity, correlation_id="TRELLIS-PREPARE-OTHER"
            ),
        )

        invalid_results = (
            lambda: dataclasses.replace(valid, status=object()),
            lambda: dataclasses.replace(valid, reason=object()),
            lambda: dataclasses.replace(valid, head=object()),
            lambda: dataclasses.replace(valid, events=[]),
            lambda: dataclasses.replace(valid, receipt=object()),
            lambda: dataclasses.replace(
                valid, events=(valid.events[0],), head=valid.head
            ),
            lambda: dataclasses.replace(valid, receipt=wrong_operation),
            lambda: dataclasses.replace(valid, observation=None),
            lambda: dataclasses.replace(valid, receipt=wrong_identity),
            lambda: PrepareAttemptEffectResult(
                TrellisLifecycleEffectStatus.APPLIED,
                TrellisLifecycleEffectReason.NONE,
                GENESIS_HEAD,
            ),
            lambda: PrepareAttemptEffectResult(
                TrellisLifecycleEffectStatus.BLOCKED,
                TrellisLifecycleEffectReason.NONE,
                GENESIS_HEAD,
            ),
            lambda: PrepareAttemptEffectResult(
                TrellisLifecycleEffectStatus.BLOCKED,
                TrellisLifecycleEffectReason.EFFECT_ABSENT,
                GENESIS_HEAD,
            ),
            lambda: dataclasses.replace(
                valid,
                status=TrellisLifecycleEffectStatus.BLOCKED,
                reason=TrellisLifecycleEffectReason.EFFECT_ABSENT,
            ),
            lambda: dataclasses.replace(
                valid,
                status=TrellisLifecycleEffectStatus.BLOCKED,
                reason=TrellisLifecycleEffectReason.JOURNAL_CONFLICT,
            ),
        )
        for invalid_result in invalid_results:
            with self.subTest(invalid_result=invalid_result), self.assertRaises(
                (TypeError, ValueError)
            ):
                invalid_result()

    def test_constructor_arguments_and_phase_bindings_fail_closed(self) -> None:
        lifecycle = FakeTrellisLifecyclePort(state=self.state)
        constructor_cases = (
            (object(), lifecycle, self.evidence, "coordinator-001", 7, None),
            (self.journal, object(), self.evidence, "coordinator-001", 7, None),
            (self.journal, lifecycle, object(), "coordinator-001", 7, None),
            (self.journal, lifecycle, self.evidence, "", 7, None),
            (self.journal, lifecycle, self.evidence, "coordinator-001", 0, None),
            (self.journal, lifecycle, self.evidence, "coordinator-001", 7, object()),
        )
        for journal, adapter, evidence, coordinator_id, token, failpoint in constructor_cases:
            with self.subTest(coordinator_id=coordinator_id, token=token), self.assertRaises(
                (TypeError, ValueError)
            ):
                TrellisLifecycleEffectService(
                    journal,
                    adapter,
                    evidence,
                    coordinator_id=coordinator_id,
                    fencing_token=token,
                    failpoint=failpoint,
                )

        service = self.service()
        argument_cases = (
            lambda: service.prepare(
                object(), prepare_command(), expected_head=GENESIS_HEAD
            ),
            lambda: service.prepare(
                identity(), object(), expected_head=GENESIS_HEAD
            ),
            lambda: service.prepare(
                identity(), prepare_command(), expected_head=object()
            ),
        )
        for invalid_call in argument_cases:
            with self.subTest(invalid_call=invalid_call), self.assertRaises(TypeError):
                invalid_call()

        check = service.check(
            identity(),
            dataclasses.replace(check_command("ATTEMPT-001"), task_id="TASK-OTHER"),
            expected_head=GENESIS_HEAD,
        )
        finish = service.finish(
            identity(),
            dataclasses.replace(finish_command("ATTEMPT-001"), task_id="TASK-OTHER"),
            expected_head=GENESIS_HEAD,
        )
        for result in (check, finish):
            self.assertIs(result.status, TrellisLifecycleEffectStatus.BLOCKED)
            self.assertIs(result.reason, TrellisLifecycleEffectReason.REQUEST_INVALID)
            self.assertFalse(result.events)

    def test_journal_failures_block_at_each_append_boundary(self) -> None:
        conflict_head = JournalHead(1, "sha256:" + "d" * 64)
        with mock.patch.object(
            self.journal,
            "append_draft",
            return_value=AppendResult(AppendStatus.CONFLICT, conflict_head),
        ):
            request_conflict = self.service().prepare(
                identity(), prepare_command(), expected_head=GENESIS_HEAD
            )
        self.assertIs(
            request_conflict.reason, TrellisLifecycleEffectReason.JOURNAL_CONFLICT
        )
        self.assertFalse(request_conflict.events)

        original_append = self.journal.append_draft
        append_count = 0

        def fail_observation_append(draft, *, expected_head, lease_state=None):
            nonlocal append_count
            append_count += 1
            if append_count == 1:
                return original_append(
                    draft,
                    expected_head=expected_head,
                    lease_state=lease_state,
                )
            return AppendResult(
                AppendStatus.PERSISTENCE_FAILED,
                None,
                fault_code=JournalFaultCode.WRITE_FAILED,
            )

        with mock.patch.object(
            self.journal, "append_draft", side_effect=fail_observation_append
        ):
            observation_failure = self.service().prepare(
                identity(),
                prepare_command(operation_id="TRELLIS-PREPARE-PERSISTENCE"),
                expected_head=GENESIS_HEAD,
            )
        self.assertIs(
            observation_failure.reason,
            TrellisLifecycleEffectReason.PERSISTENCE_FAILED,
        )
        self.assertEqual(1, len(observation_failure.events))
        self.assertIsNotNone(observation_failure.observation)
        self.assertIsNone(observation_failure.receipt)

    def test_result_types_reject_cross_phase_observations(self) -> None:
        observation = AttemptObservation(
            operation_id="TRELLIS-PREPARE-001",
            status=EffectStatus.UNKNOWN,
            observed_at="2026-08-19T00:00:00Z",
            lifecycle_state=TrellisLifecycleState.UNKNOWN,
            evidence=("unknown",),
        )
        with self.assertRaisesRegex(TypeError, "CheckObservation"):
            CheckAttemptEffectResult(
                TrellisLifecycleEffectStatus.BLOCKED,
                TrellisLifecycleEffectReason.OBSERVATION_INVALID,
                GENESIS_HEAD,
                observation=observation,  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
