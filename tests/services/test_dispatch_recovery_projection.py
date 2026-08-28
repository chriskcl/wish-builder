from __future__ import annotations

import unittest
from dataclasses import replace

from tests.contracts.test_dispatch_recovery import (
    NOW,
    digest,
    recovery_payload,
    subject,
)
from tests.services.test_recovery import BASE_TIME, lease_draft, owner
from wish_builder.contracts import (
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.services.recovery import (
    DispatchRecoveryProjectionError,
    DispatchRecoveryRecord,
    advance_dispatch_recoveries,
)


def proof_event() -> JournalEvent:
    return JournalEvent.create(
        sequence=41,
        event_id="EVENT-RECOVERY-COMPLETED-0041",
        event_type=JournalEventType.RECOVERY_COMPLETED,
        identity=ExecutionIdentity("RUN-RECOVERY", 2),
        actor_type=ActorType.HUMAN,
        actor_id="local-account-001",
        recorded_at=NOW,
        previous_event_hash=digest("b"),
        payload=recovery_payload(),
    )


def retry_event(previous: JournalEvent) -> JournalEvent:
    payload = recovery_payload()
    return JournalEvent.create(
        sequence=previous.sequence + 1,
        event_id="EVENT-TASK-RETRY-SCHEDULED-0042",
        event_type=JournalEventType.TASK_RETRY_SCHEDULED,
        identity=ExecutionIdentity("RUN-RECOVERY", 2, subject().task_id),
        actor_type=ActorType.HUMAN,
        actor_id="local-account-001",
        recorded_at=NOW,
        previous_event_hash=previous.event_hash,
        payload=TransitionPayload(
            TransitionSubject.TASK,
            RuntimeState.BLOCKED,
            RuntimeState.READY,
            payload.evidence,
        ),
    )


def resumed_event(previous: JournalEvent) -> JournalEvent:
    payload = recovery_payload()
    return JournalEvent.create(
        sequence=previous.sequence + 1,
        event_id="EVENT-RUN-RESUMED-0043",
        event_type=JournalEventType.RUN_RESUMED,
        identity=ExecutionIdentity("RUN-RECOVERY", 2),
        actor_type=ActorType.HUMAN,
        actor_id="local-account-001",
        recorded_at=NOW,
        previous_event_hash=previous.event_hash,
        payload=TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.BLOCKED,
            RuntimeState.RUNNING,
            payload.evidence,
        ),
    )


class DispatchRecoveryProjectionTests(unittest.TestCase):
    def test_exact_three_event_prefix_is_rebuildable(self) -> None:
        proof = proof_event()
        retry = retry_event(proof)
        resumed = resumed_event(retry)

        records = advance_dispatch_recoveries((), proof)
        self.assertEqual(1, len(records))
        self.assertFalse(records[0].complete)
        records = advance_dispatch_recoveries(records, retry)
        self.assertEqual(retry, records[0].task_retry_event)
        records = advance_dispatch_recoveries(records, resumed)
        self.assertEqual(resumed, records[0].run_resumed_event)
        self.assertTrue(records[0].complete)

    def test_incomplete_prefix_rejects_out_of_order_or_conflicting_events(self) -> None:
        proof = proof_event()
        records = advance_dispatch_recoveries((), proof)
        with self.assertRaises(DispatchRecoveryProjectionError):
            advance_dispatch_recoveries(records, resumed_event(proof))

        retry = retry_event(proof)
        wrong = JournalEvent.create(
            sequence=retry.sequence,
            event_id="EVENT-TASK-RETRY-SCHEDULED-WRONG",
            event_type=JournalEventType.TASK_RETRY_SCHEDULED,
            identity=ExecutionIdentity("RUN-RECOVERY", 2, "TASK-999"),
            actor_type=ActorType.HUMAN,
            actor_id="local-account-001",
            recorded_at=NOW,
            previous_event_hash=proof.event_hash,
            payload=retry.payload,
        )
        with self.assertRaises(DispatchRecoveryProjectionError):
            advance_dispatch_recoveries(records, wrong)

    def test_projection_guards_reject_forged_records_and_inputs(self) -> None:
        proof = proof_event()
        retry = retry_event(proof)
        resumed = resumed_event(retry)
        recovery_id = recovery_payload().recovery_id

        for args in (
            ("", proof),
            (recovery_id, retry),
            (recovery_id, proof, proof),
            (recovery_id, proof, retry, retry),
            (recovery_id, proof, None, resumed),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                DispatchRecoveryRecord(*args)

        wrong_retry = JournalEvent.create(
            sequence=42,
            event_id="EVENT-TASK-RETRY-SCHEDULED-FORGED",
            event_type=JournalEventType.TASK_RETRY_SCHEDULED,
            identity=ExecutionIdentity("RUN-RECOVERY", 2, "TASK-999"),
            actor_type=ActorType.HUMAN,
            actor_id="local-account-001",
            recorded_at=NOW,
            previous_event_hash=proof.event_hash,
            payload=retry.payload,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            DispatchRecoveryRecord(recovery_id, proof, wrong_retry)

        early_retry = JournalEvent.create(
            sequence=40,
            event_id="EVENT-TASK-RETRY-SCHEDULED-EARLY",
            event_type=JournalEventType.TASK_RETRY_SCHEDULED,
            identity=retry.identity,
            actor_type=retry.actor_type,
            actor_id=retry.actor_id,
            recorded_at=NOW,
            previous_event_hash=digest("e"),
            payload=retry.payload,
        )
        with self.assertRaisesRegex(ValueError, "must follow"):
            DispatchRecoveryRecord(recovery_id, proof, early_retry)

        wrong_resume = JournalEvent.create(
            sequence=43,
            event_id="EVENT-RUN-RESUMED-FORGED",
            event_type=JournalEventType.RUN_RESUMED,
            identity=resumed.identity,
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-002",
            recorded_at=NOW,
            previous_event_hash=retry.event_hash,
            payload=resumed.payload,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            DispatchRecoveryRecord(recovery_id, proof, retry, wrong_resume)

        early_resume = JournalEvent.create(
            sequence=41,
            event_id="EVENT-RUN-RESUMED-EARLY",
            event_type=JournalEventType.RUN_RESUMED,
            identity=resumed.identity,
            actor_type=resumed.actor_type,
            actor_id=resumed.actor_id,
            recorded_at=NOW,
            previous_event_hash=digest("f"),
            payload=resumed.payload,
        )
        with self.assertRaisesRegex(ValueError, "must follow"):
            DispatchRecoveryRecord(recovery_id, proof, retry, early_resume)

        for records, event in (([], proof), ((object(),), proof), ((), object())):
            with self.subTest(records=records, event=event), self.assertRaises(TypeError):
                advance_dispatch_recoveries(records, event)  # type: ignore[arg-type]

    def test_projection_handles_idempotency_lease_gaps_and_conflicts(self) -> None:
        proof = proof_event()
        records = advance_dispatch_recoveries((), proof)
        self.assertIs(records, advance_dispatch_recoveries(records, proof))

        different_event = JournalEvent.create(
            sequence=41,
            event_id="EVENT-RECOVERY-COMPLETED-DIFFERENT",
            event_type=JournalEventType.RECOVERY_COMPLETED,
            identity=proof.identity,
            actor_type=proof.actor_type,
            actor_id=proof.actor_id,
            recorded_at=NOW,
            previous_event_hash=proof.previous_event_hash,
            payload=proof.payload,
        )
        with self.assertRaises(DispatchRecoveryProjectionError):
            advance_dispatch_recoveries(records, different_event)

        second_payload = replace(
            recovery_payload(),
            recovery_id="RECOVERY-DISPATCH-002",
        )
        second_proof = JournalEvent.create(
            sequence=41,
            event_id="EVENT-RECOVERY-COMPLETED-SECOND",
            event_type=JournalEventType.RECOVERY_COMPLETED,
            identity=proof.identity,
            actor_type=proof.actor_type,
            actor_id=proof.actor_id,
            recorded_at=NOW,
            previous_event_hash=proof.previous_event_hash,
            payload=second_payload,
        )
        with self.assertRaises(DispatchRecoveryProjectionError):
            advance_dispatch_recoveries(records, second_proof)

        lease = lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            event_id="EVENT-LEASE-ACQUIRED-GAP",
            lease_id="LEASE-GAP",
            lease_owner=owner("coordinator-gap"),
            token=2,
        ).materialize(
            sequence=42,
            previous_event_hash=proof.event_hash,
            authority_time=BASE_TIME,
        )
        self.assertEqual(records, advance_dispatch_recoveries(records, lease))

        retry = retry_event(proof)
        complete = advance_dispatch_recoveries(
            advance_dispatch_recoveries(records, retry),
            resumed_event(retry),
        )
        with self.assertRaises(DispatchRecoveryProjectionError):
            advance_dispatch_recoveries(complete, retry)
        self.assertEqual((), advance_dispatch_recoveries((), retry))

        second_record = DispatchRecoveryRecord(
            second_payload.recovery_id,
            second_proof,
        )
        with self.assertRaises(DispatchRecoveryProjectionError):
            advance_dispatch_recoveries((*records, second_record), retry)

        after_retry = advance_dispatch_recoveries(records, retry)
        with self.assertRaises(DispatchRecoveryProjectionError):
            advance_dispatch_recoveries(after_retry, retry)


if __name__ == "__main__":
    unittest.main()
