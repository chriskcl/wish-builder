"""Durable Gate decision admission backed by the canonical Journal CAS."""

from __future__ import annotations

from dataclasses import dataclass

from wish_builder.contracts import (
    DecisionAdmissionReason,
    DecisionEvaluation,
    DecisionObservedPayload,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
)

from .journal import (
    AppendResult,
    AppendStatus,
    DurableJournal,
    JournalHead,
)


@dataclass(frozen=True, slots=True)
class DecisionCommitResult:
    evaluation: DecisionEvaluation
    append_result: AppendResult | None
    event: JournalEvent | None

    def __post_init__(self) -> None:
        if type(self.evaluation) is not DecisionEvaluation:
            raise TypeError("evaluation must be a DecisionEvaluation")
        if self.append_result is not None and type(self.append_result) is not AppendResult:
            raise TypeError("append_result must be an AppendResult or null")
        if self.event is not None and type(self.event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent or null")
        if self.append_result is None:
            if self.event is not None or self.evaluation.accepted:
                raise ValueError("a non-persisted decision must be rejected and have no event")
            return
        if self.event is None:
            raise ValueError("a persistence attempt requires its exact event")
        if self.evaluation.accepted != self.append_result.durable:
            raise ValueError("decision admission must match durable append evidence")
        if self.append_result.durable and self.append_result.event != self.event:
            raise ValueError("durable append evidence must identify the decision event")

    @property
    def durable(self) -> bool:
        return self.evaluation.accepted and self.append_result is not None and self.append_result.durable


def commit_decision(
    evaluation: DecisionEvaluation,
    journal: DurableJournal,
    *,
    expected_head: JournalHead,
    identity: ExecutionIdentity,
    event_id: str,
) -> DecisionCommitResult:
    """Persist the exact admitted observation before exposing the decision."""

    if type(evaluation) is not DecisionEvaluation:
        raise TypeError("evaluation must be a DecisionEvaluation")
    if type(journal) is not DurableJournal:
        raise TypeError("journal must be a DurableJournal")
    if type(expected_head) is not JournalHead:
        raise TypeError("expected_head must be a JournalHead")
    if type(identity) is not ExecutionIdentity or identity.task_id is not None:
        raise TypeError("identity must be a run-only ExecutionIdentity")
    if type(event_id) is not str:
        raise TypeError("event_id must be a string")
    if not evaluation.accepted:
        return DecisionCommitResult(evaluation, None, None)

    observation = evaluation.observation
    if observation is None:
        raise ValueError("an accepted evaluation must carry its observation")
    if observation.event_sequence != expected_head.sequence + 1:
        raise ValueError("decision observation must immediately follow expected_head")
    decision = observation.decision
    event = JournalEvent.create(
        sequence=observation.event_sequence,
        event_id=event_id,
        event_type=JournalEventType.DECISION_OBSERVED,
        identity=identity,
        actor_type=decision.actor.actor_type,
        actor_id=decision.actor.actor_id,
        recorded_at=decision.decided_at,
        previous_event_hash=expected_head.event_hash,
        payload=DecisionObservedPayload(observation),
    )
    append_result = journal.append(event, expected_head=expected_head)

    if append_result.status is AppendStatus.COMMITTED:
        if evaluation.idempotent:
            raise RuntimeError("an observed decision cannot be newly committed")
        return DecisionCommitResult(evaluation, append_result, event)
    if append_result.status is AppendStatus.IDEMPOTENT:
        replay = DecisionEvaluation(
            True,
            DecisionAdmissionReason.IDEMPOTENT_REPLAY,
            observation,
            idempotent=True,
        )
        return DecisionCommitResult(replay, append_result, event)
    reason = (
        DecisionAdmissionReason.DECISION_CONFLICT
        if append_result.status is AppendStatus.CONFLICT
        else DecisionAdmissionReason.PERSISTENCE_FAILED
    )
    return DecisionCommitResult(
        DecisionEvaluation(False, reason),
        append_result,
        event,
    )


__all__ = ["DecisionCommitResult", "commit_decision"]
