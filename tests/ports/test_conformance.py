from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from wish_builder.adapters.fake import (
    FakeModelPort,
    FakeRepositoryPort,
    FakeTaskPort,
)
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectReceiptValue,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    OperationOutcome,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.services.journal import (
    AppendResult,
    AppendStatus,
    JournalHead,
)
from wish_builder.services.ports import EffectPort, PersistedEffectRequest

GENESIS_HASH = "sha256:" + "0" * 64
FIXED_TIME = "2026-08-18T05:00:00Z"


def persisted_request(
    adapter: AdapterKind,
    operation: EffectOperation,
    object_type: EffectObjectType,
    event_type: JournalEventType,
    *,
    correlation_id: str = "CORRELATION-001",
    request_hash_number: int = 2,
) -> PersistedEffectRequest:
    identity = ExecutionIdentity("WISH-001", 1, "TASK-001", 1, correlation_id)
    event = JournalEvent.create(
        sequence=1,
        event_id=f"EVENT-{adapter.value.upper()}-{request_hash_number:03d}",
        event_type=event_type,
        identity=identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at=FIXED_TIME,
        previous_event_hash=GENESIS_HASH,
        payload=EffectRequestPayload(
            operation,
            adapter,
            object_type,
            "sha256:" + f"{1:064x}",
            "sha256:" + f"{request_hash_number:064x}",
            0,
            1,
        ),
    )
    result = AppendResult(
        AppendStatus.COMMITTED,
        JournalHead(event.sequence, event.event_hash),
        event,
    )
    return PersistedEffectRequest.from_append_result(result)


def receipt_from(outcome: OperationOutcome):
    if type(outcome.value) is not EffectReceiptValue:
        raise AssertionError(outcome)
    return outcome.value.receipt


class FakePortConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cases(self):
        return (
            (
                FakeTaskPort,
                AdapterKind.TASK,
                EffectOperation.TASK_EXECUTION,
                EffectObjectType.WORKER,
                JournalEventType.EFFECT_REQUESTED,
            ),
            (
                FakeModelPort,
                AdapterKind.MODEL,
                EffectOperation.MODEL_INFERENCE,
                EffectObjectType.RESULT_BUNDLE,
                JournalEventType.EFFECT_REQUESTED,
            ),
            (
                FakeRepositoryPort,
                AdapterKind.REPOSITORY,
                EffectOperation.REPOSITORY_UPDATE,
                EffectObjectType.WORKTREE,
                JournalEventType.EFFECT_REQUESTED,
            ),
        )

    def test_every_port_has_absent_apply_applied_and_idempotent_lookup(self) -> None:
        for factory, adapter, operation, object_type, event_type in self.cases():
            with self.subTest(adapter=adapter):
                port = factory(self.root, clock=lambda: FIXED_TIME)
                self.assertIsInstance(port, EffectPort)
                request = persisted_request(adapter, operation, object_type, event_type)

                absent = receipt_from(port.lookup(request.identity, operation))
                self.assertEqual(EffectStatus.ABSENT, absent.status)

                first = receipt_from(port.apply(request))
                second = receipt_from(port.apply(request))
                looked_up = receipt_from(port.lookup(request.identity, operation))
                self.assertEqual(EffectStatus.APPLIED, first.status)
                self.assertEqual(first, second)
                self.assertEqual(first, looked_up)
                self.assertEqual(1, len(tuple(port.effects.glob("*.json"))))
                self.assertEqual(1, len(tuple(port.receipts.glob("*.json"))))

    def test_raw_or_non_durable_requests_never_reach_an_effect(self) -> None:
        request = persisted_request(
            AdapterKind.TASK,
            EffectOperation.TASK_EXECUTION,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
        )
        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        with self.assertRaisesRegex(TypeError, "PersistedEffectRequest"):
            port.apply(request.event)  # type: ignore[arg-type]

        conflict = AppendResult(
            AppendStatus.CONFLICT,
            JournalHead(1, "sha256:" + f"{99:064x}"),
        )
        with self.assertRaisesRegex(ValueError, "durable event"):
            PersistedEffectRequest.from_append_result(conflict)
        self.assertFalse(port.effects.exists())

    def test_identity_adapter_operation_event_and_object_mismatches_are_closed(
        self,
    ) -> None:
        request = persisted_request(
            AdapterKind.TASK,
            EffectOperation.TASK_EXECUTION,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
        )
        model = FakeModelPort(self.root, clock=lambda: FIXED_TIME)
        with self.assertRaisesRegex(ValueError, "another adapter"):
            model.apply(request)
        with self.assertRaisesRegex(ValueError, "operation"):
            model.lookup(request.identity, EffectOperation.TASK_EXECUTION)
        with self.assertRaisesRegex(ValueError, "complete attempt"):
            model.lookup(
                ExecutionIdentity("WISH-001", 1),
                EffectOperation.MODEL_INFERENCE,
            )

        wrong_event = persisted_request(
            AdapterKind.TASK,
            EffectOperation.WORKER_DISPATCH,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
        )
        with self.assertRaisesRegex(ValueError, "event_type"):
            FakeTaskPort(self.root, clock=lambda: FIXED_TIME).apply(wrong_event)

        wrong_object = persisted_request(
            AdapterKind.REPOSITORY,
            EffectOperation.REPOSITORY_UPDATE,
            EffectObjectType.RESULT_BUNDLE,
            JournalEventType.EFFECT_REQUESTED,
        )
        with self.assertRaisesRegex(ValueError, "object_type"):
            FakeRepositoryPort(self.root, clock=lambda: FIXED_TIME).apply(wrong_object)

    def test_same_correlation_with_a_different_request_becomes_unknown(self) -> None:
        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        first = persisted_request(
            AdapterKind.TASK,
            EffectOperation.TASK_EXECUTION,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
            request_hash_number=2,
        )
        conflict = persisted_request(
            AdapterKind.TASK,
            EffectOperation.TASK_EXECUTION,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
            request_hash_number=3,
        )
        self.assertEqual(EffectStatus.APPLIED, receipt_from(port.apply(first)).status)
        unknown = receipt_from(port.apply(conflict))
        self.assertEqual(EffectStatus.UNKNOWN, unknown.status)
        self.assertTrue(unknown.evidence)
        self.assertEqual(1, len(tuple(port.effects.glob("*.json"))))

    def test_persisted_request_rejects_forged_durability_sequence_and_fence(
        self,
    ) -> None:
        valid = persisted_request(
            AdapterKind.TASK,
            EffectOperation.TASK_EXECUTION,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
        )
        other = persisted_request(
            AdapterKind.TASK,
            EffectOperation.TASK_EXECUTION,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
            request_hash_number=3,
        )
        with self.assertRaisesRegex(TypeError, "JournalEvent"):
            PersistedEffectRequest(object(), valid.append_result)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "AppendResult"):
            PersistedEffectRequest(valid.event, object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "AppendResult"):
            PersistedEffectRequest.from_append_result(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "exact durable"):
            PersistedEffectRequest(valid.event, other.append_result)

        transition_event = JournalEvent.create(
            sequence=1,
            event_id="EVENT-NOT-EFFECT",
            event_type=JournalEventType.RUN_INITIALIZED,
            identity=ExecutionIdentity("WISH-001", 1),
            actor_type=ActorType.SYSTEM,
            actor_id="test",
            recorded_at=FIXED_TIME,
            previous_event_hash=GENESIS_HASH,
            payload=TransitionPayload(
                TransitionSubject.RUN,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
        )
        transition_result = AppendResult(
            AppendStatus.COMMITTED,
            JournalHead(1, transition_event.event_hash),
            transition_event,
        )
        with self.assertRaisesRegex(ValueError, "not an effect request"):
            PersistedEffectRequest(transition_event, transition_result)

        forged_payload = object.__new__(JournalEvent)
        for field in dataclasses.fields(transition_event):
            object.__setattr__(
                forged_payload,
                field.name,
                getattr(transition_event, field.name),
            )
        object.__setattr__(
            forged_payload,
            "event_type",
            JournalEventType.EFFECT_REQUESTED,
        )
        forged_result = AppendResult(
            AppendStatus.COMMITTED,
            JournalHead(1, forged_payload.event_hash),
            forged_payload,
        )
        with self.assertRaisesRegex(TypeError, "payload type"):
            PersistedEffectRequest(forged_payload, forged_result)

        def invalid_request_event(
            *,
            sequence: int,
            expected_sequence: int,
            epoch: int,
            fencing_token: int,
            event_id: str,
        ) -> JournalEvent:
            return JournalEvent.create(
                sequence=sequence,
                event_id=event_id,
                event_type=JournalEventType.EFFECT_REQUESTED,
                identity=ExecutionIdentity(
                    "WISH-001", epoch, "TASK-001", 1, "CORRELATION-001"
                ),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=FIXED_TIME,
                previous_event_hash=(
                    GENESIS_HASH if sequence == 1 else "sha256:" + f"{9:064x}"
                ),
                payload=EffectRequestPayload(
                    EffectOperation.TASK_EXECUTION,
                    AdapterKind.TASK,
                    EffectObjectType.WORKER,
                    "sha256:" + f"{1:064x}",
                    "sha256:" + f"{2:064x}",
                    expected_sequence,
                    fencing_token,
                ),
            )

        invalid_events = (
            (
                "journal-adjacent",
                invalid_request_event(
                    sequence=2,
                    expected_sequence=0,
                    epoch=1,
                    fencing_token=1,
                    event_id="EVENT-SEQUENCE-NOT-ADJACENT",
                ),
            ),
            (
                "fencing token",
                invalid_request_event(
                    sequence=1,
                    expected_sequence=0,
                    epoch=2,
                    fencing_token=1,
                    event_id="EVENT-FENCE-MISMATCH",
                ),
            ),
        )
        for message, event in invalid_events:
            result = AppendResult(
                AppendStatus.COMMITTED,
                JournalHead(event.sequence, event.event_hash),
                event,
            )
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                PersistedEffectRequest(event, result)


if __name__ == "__main__":
    unittest.main()
