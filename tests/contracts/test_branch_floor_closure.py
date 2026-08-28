from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, datetime

from wish_builder.contracts.runtime import (
    ActorIdentity,
    ActorType,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionCommand,
    DecisionObservation,
    DecisionObservedPayload,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    ExecutionIdentity,
    JOURNAL_EVENT_VERSION,
    JournalEvent,
    JournalEventType,
    LeaseDraftPayload,
    LeaseOwner,
    LeasePayload,
    RuntimeReasonCode,
    RuntimeState,
    SchedulerMode,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.contracts.serialization import canonical_sha256


NOW = "2026-08-19T00:00:00Z"
LATER = "2026-08-19T00:01:30Z"
ZERO_HASH = "sha256:" + "0" * 64


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _actor(
    actor_type: ActorType = ActorType.COORDINATOR,
    actor_id: str = "coordinator-a",
) -> ActorIdentity:
    return ActorIdentity(
        actor_type,
        actor_id,
        "host-a",
        101,
        f"process-start-{actor_id}",
    )


def _owner(
    actor_type: ActorType = ActorType.COORDINATOR,
    actor_id: str = "coordinator-a",
) -> LeaseOwner:
    return LeaseOwner(
        actor=_actor(actor_type, actor_id),
        local_repository_id=_hash("1"),
        local_worktree_id=_hash("2"),
        workspace_hash=_hash("3"),
        control_root_id=_hash("4"),
    )


def _lease_payload(
    *,
    coordinator_id: str = "coordinator-a",
    owner: object | None = None,
    scheduler_mode: object = SchedulerMode.WISH_BUILDER,
    fencing_token: int = 3,
    lease_ttl_seconds: int = 90,
    lease_clock_skew_seconds: int = 2,
    committed_at: str = NOW,
    expires_at: str = LATER,
) -> LeasePayload:
    return LeasePayload(
        lease_id="LEASE-003",
        coordinator_id=coordinator_id,
        owner=_owner() if owner is None else owner,  # type: ignore[arg-type]
        scheduler_mode=scheduler_mode,  # type: ignore[arg-type]
        fencing_token=fencing_token,
        manifest_digest=_hash("a"),
        lease_ttl_seconds=lease_ttl_seconds,
        lease_clock_skew_seconds=lease_clock_skew_seconds,
        committed_at=committed_at,
        expires_at=expires_at,
    )


def _transition_event() -> JournalEvent:
    return JournalEvent.create(
        sequence=1,
        event_id="EVENT-001",
        event_type=JournalEventType.RUN_INITIALIZED,
        identity=ExecutionIdentity("RUN-001", 0),
        actor_type=ActorType.SYSTEM,
        actor_id="wishctl",
        recorded_at=NOW,
        previous_event_hash=ZERO_HASH,
        payload=TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.NONE,
            RuntimeState.PREFLIGHT,
        ),
    )


def _lease_event(
    event_type: JournalEventType,
    payload: LeasePayload,
    *,
    identity: ExecutionIdentity | None = None,
    actor_type: ActorType = ActorType.COORDINATOR,
    actor_id: str = "coordinator-a",
    recorded_at: str = NOW,
    reason_code: RuntimeReasonCode | None = None,
) -> JournalEvent:
    return JournalEvent.create(
        sequence=2,
        event_id="EVENT-LEASE-002",
        event_type=event_type,
        identity=identity or ExecutionIdentity("RUN-LEASE", 3),
        actor_type=actor_type,
        actor_id=actor_id,
        recorded_at=recorded_at,
        previous_event_hash=_hash("b"),
        payload=payload,
        reason_code=reason_code,
    )


def _decision_request(expected_sequence: int = 10) -> DecisionRequest:
    return DecisionRequest(
        command=CommandIdentity(
            schema_version=1,
            command_id="COMMAND-001",
            request_id="REQUEST-001",
            kind=CommandKind.DECIDE,
            expected_sequence=expected_sequence,
            request_nonce="nonce-001",
            actor=_actor(),
            source_channel=SourceChannel.COORDINATOR,
            submitted_at=NOW,
        ),
        decision_type=DecisionType.GATE_B,
        candidate_hash=_hash("c"),
        workspace_hash=_hash("d"),
        expected_actor_id="human-a",
        options=(DecisionChoice.APPROVE, DecisionChoice.REJECT),
    )


def _decision_observation(expected_sequence: int = 10) -> DecisionObservation:
    decision = DecisionCommand(
        decision_id="DECISION-001",
        request=_decision_request(expected_sequence),
        choice=DecisionChoice.APPROVE,
        actor=_actor(ActorType.HUMAN, "human-a"),
        source_channel=SourceChannel.DIRECT_CLI,
        decided_at=NOW,
    )
    return DecisionObservation(
        decision=decision,
        event_sequence=11,
        submission_hash="sha256:" + canonical_sha256(decision.to_primitive()),
    )


class LeaseBoundaryBranchTests(unittest.TestCase):
    def test_lease_timing_rejects_out_of_policy_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 30 and 3600"):
            _lease_payload(lease_ttl_seconds=29)
        with self.assertRaisesRegex(ValueError, "less than one-quarter TTL"):
            _lease_payload(lease_ttl_seconds=30, lease_clock_skew_seconds=8)

    def test_lease_owner_requires_a_coordinator_actor(self) -> None:
        owner_fields = {
            "local_repository_id": _hash("1"),
            "local_worktree_id": _hash("2"),
            "workspace_hash": _hash("3"),
            "control_root_id": _hash("4"),
        }
        with self.assertRaisesRegex(TypeError, "ActorIdentity"):
            LeaseOwner(actor=object(), **owner_fields)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "must be a coordinator"):
            LeaseOwner(actor=_actor(ActorType.HUMAN, "human-a"), **owner_fields)

    def test_lease_payload_binds_owner_scheduler_and_expiry(self) -> None:
        cases = (
            ({"owner": object()}, TypeError, "LeaseOwner"),
            (
                {"coordinator_id": "coordinator-b", "owner": _owner()},
                ValueError,
                "actor_id must match",
            ),
            ({"scheduler_mode": "wish_builder"}, TypeError, "SchedulerMode"),
            (
                {"committed_at": LATER, "expires_at": NOW},
                ValueError,
                "must not precede",
            ),
        )
        for updates, error, message in cases:
            with self.subTest(updates=updates), self.assertRaisesRegex(error, message):
                _lease_payload(**updates)  # type: ignore[arg-type]

    def test_lease_draft_materialization_rejects_ambiguous_inputs(self) -> None:
        draft = LeaseDraftPayload(
            lease_id="LEASE-003",
            coordinator_id="coordinator-a",
            owner=_owner(),
            scheduler_mode=SchedulerMode.WISH_BUILDER,
            fencing_token=3,
            manifest_digest=_hash("a"),
            lease_ttl_seconds=90,
            lease_clock_skew_seconds=2,
        )
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            draft.materialize(datetime(2026, 8, 19), terminal=False)
        with self.assertRaisesRegex(TypeError, "terminal must be a bool"):
            draft.materialize(datetime(2026, 8, 19, tzinfo=UTC), terminal=1)  # type: ignore[arg-type]


class JournalBoundaryBranchTests(unittest.TestCase):
    def test_scalar_and_hash_tampering_is_rejected_at_the_exact_boundary(self) -> None:
        event = _transition_event()
        cases = (
            ({"event_version": "2.0"}, ValueError, JOURNAL_EVENT_VERSION),
            ({"event_type": "run_initialized"}, TypeError, "JournalEventType"),
            ({"identity": object()}, TypeError, "ExecutionIdentity"),
            ({"actor_type": "system"}, TypeError, "ActorType"),
            ({"reason_code": "policy_denied"}, TypeError, "RuntimeReasonCode"),
            ({"payload_hash": _hash("f")}, ValueError, "payload_hash"),
        )
        for updates, error, message in cases:
            with self.subTest(updates=updates), self.assertRaisesRegex(error, message):
                dataclasses.replace(event, **updates)

        with self.assertRaisesRegex(ValueError, "genesis previous hash"):
            JournalEvent.create(
                sequence=1,
                event_id="EVENT-INVALID-GENESIS",
                event_type=JournalEventType.RUN_INITIALIZED,
                identity=ExecutionIdentity("RUN-001", 0),
                actor_type=ActorType.SYSTEM,
                actor_id="wishctl",
                recorded_at=NOW,
                previous_event_hash=_hash("e"),
                payload=event.payload,
            )

    def test_lease_events_bind_identity_fencing_time_and_actor(self) -> None:
        active = _lease_payload()
        cases = (
            (
                {
                    "event_type": JournalEventType.LEASE_ACQUIRED,
                    "payload": active,
                    "identity": ExecutionIdentity("RUN-LEASE", 3, "TASK-001"),
                },
                "run-only identity",
            ),
            (
                {
                    "event_type": JournalEventType.LEASE_ACQUIRED,
                    "payload": active,
                    "identity": ExecutionIdentity("RUN-LEASE", 4),
                },
                "fencing token",
            ),
            (
                {
                    "event_type": JournalEventType.LEASE_ACQUIRED,
                    "payload": active,
                    "recorded_at": "2026-08-19T00:00:01Z",
                },
                "committed_at must match",
            ),
            (
                {
                    "event_type": JournalEventType.LEASE_RELEASED,
                    "payload": active,
                },
                "terminal lease events",
            ),
            (
                {
                    "event_type": JournalEventType.LEASE_RENEWED,
                    "payload": _lease_payload(expires_at=NOW),
                },
                "future expiry",
            ),
            (
                {
                    "event_type": JournalEventType.LEASE_ACQUIRED,
                    "payload": active,
                    "actor_type": ActorType.SYSTEM,
                    "actor_id": "recovery",
                },
                "lease holder",
            ),
        )
        for arguments, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                _lease_event(**arguments)  # type: ignore[arg-type]

    def test_lease_loss_and_non_loss_reason_codes_are_closed(self) -> None:
        terminal = _lease_payload(expires_at=NOW)
        with self.assertRaisesRegex(ValueError, "lease_lost reason code"):
            _lease_event(
                JournalEventType.LEASE_LOST,
                terminal,
                actor_type=ActorType.SYSTEM,
                actor_id="recovery",
            )
        with self.assertRaisesRegex(ValueError, "cannot carry a reason code"):
            _lease_event(
                JournalEventType.LEASE_RENEWED,
                _lease_payload(),
                reason_code=RuntimeReasonCode.POLICY_DENIED,
            )

    def test_decision_events_bind_sequence_and_actor_to_the_payload(self) -> None:
        with self.assertRaisesRegex(ValueError, "request actor"):
            JournalEvent.create(
                sequence=10,
                event_id="EVENT-DECISION-REQUESTED-010",
                event_type=JournalEventType.DECISION_REQUESTED,
                identity=ExecutionIdentity("RUN-001", 0),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-b",
                recorded_at=NOW,
                previous_event_hash=_hash("8"),
                payload=DecisionRequestPayload(_decision_request()),
            )

        with self.assertRaisesRegex(ValueError, "next expected event"):
            JournalEvent.create(
                sequence=11,
                event_id="EVENT-DECISION-OBSERVED-011",
                event_type=JournalEventType.DECISION_OBSERVED,
                identity=ExecutionIdentity("RUN-001", 0),
                actor_type=ActorType.HUMAN,
                actor_id="human-a",
                recorded_at=NOW,
                previous_event_hash=_hash("9"),
                payload=DecisionObservedPayload(_decision_observation(9)),
            )

        with self.assertRaisesRegex(ValueError, "decision actor"):
            JournalEvent.create(
                sequence=11,
                event_id="EVENT-DECISION-OBSERVED-011",
                event_type=JournalEventType.DECISION_OBSERVED,
                identity=ExecutionIdentity("RUN-001", 0),
                actor_type=ActorType.HUMAN,
                actor_id="human-b",
                recorded_at=NOW,
                previous_event_hash=_hash("9"),
                payload=DecisionObservedPayload(_decision_observation()),
            )


if __name__ == "__main__":
    unittest.main()
