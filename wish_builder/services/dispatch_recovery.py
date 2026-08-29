"""Derived validation for crash-resumable unknown-dispatch recovery prefixes."""

from __future__ import annotations

from dataclasses import dataclass, replace

from wish_builder.contracts import (
    AdapterKind,
    DispatchRecoveryPayload,
    EffectObservationPayload,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    JournalEvent,
    JournalEventType,
    LeasePayload,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)


class DispatchRecoveryProjectionError(ValueError):
    """A Journal event conflicts with the recovery prefix protocol."""


_BACKEND_OPERATIONS = frozenset(
    {
        EffectOperation.RESERVE_CHANNEL,
        EffectOperation.SEND_TASK_PACKET,
        EffectOperation.CANCEL_TURN,
    }
)
_TRELLIS_OPERATIONS = frozenset(
    {
        EffectOperation.PREPARE_ATTEMPT,
        EffectOperation.CHECK_ATTEMPT,
        EffectOperation.FINISH_ATTEMPT,
    }
)
_CHILD_EFFECT_ADAPTER = {
    **{operation: AdapterKind.BACKEND for operation in _BACKEND_OPERATIONS},
    **{operation: AdapterKind.TRELLIS for operation in _TRELLIS_OPERATIONS},
}

_TRELLIS_OBJECT_TYPES = {
    EffectOperation.PREPARE_ATTEMPT: EffectObjectType.ATTEMPT,
    EffectOperation.RESERVE_CHANNEL: EffectObjectType.CHANNEL,
    EffectOperation.SEND_TASK_PACKET: EffectObjectType.TASK_PACKET,
    EffectOperation.CANCEL_TURN: EffectObjectType.TURN,
    EffectOperation.CHECK_ATTEMPT: EffectObjectType.ATTEMPT,
    EffectOperation.FINISH_ATTEMPT: EffectObjectType.ATTEMPT,
}


@dataclass(frozen=True, slots=True)
class PendingExternalEffect:
    """One durable backend or Trellis request without an applied observation."""

    request_event: JournalEvent

    def __post_init__(self) -> None:
        if (
            type(self.request_event) is not JournalEvent
            or self.request_event.event_type is not JournalEventType.EFFECT_REQUESTED
            or type(self.request_event.payload) is not EffectRequestPayload
            or self.request_event.payload.operation not in _CHILD_EFFECT_ADAPTER
            or self.request_event.payload.adapter
            is not _CHILD_EFFECT_ADAPTER[self.request_event.payload.operation]
            or self.request_event.identity.correlation_id is None
        ):
            raise ValueError(
                "request_event must be a complete typed child effect request"
            )
        payload = self.request_event.payload
        assert type(payload) is EffectRequestPayload
        if (
            payload.fencing_token != self.request_event.identity.coordinator_epoch
            or payload.object_type is not _TRELLIS_OBJECT_TYPES[payload.operation]
        ):
            raise ValueError(
                "external request fencing or object type is inconsistent"
            )

    @property
    def operation(self) -> EffectOperation:
        payload = self.request_event.payload
        assert type(payload) is EffectRequestPayload
        return payload.operation

    @property
    def adapter(self) -> AdapterKind:
        payload = self.request_event.payload
        assert type(payload) is EffectRequestPayload
        return payload.adapter

    @property
    def operation_id(self) -> str:
        operation_id = self.request_event.identity.correlation_id
        assert operation_id is not None
        return operation_id


@dataclass(frozen=True, slots=True)
class ExternalEffectProjection:
    """Incremental pending-effect projection over one contiguous Journal chain."""

    previous_sequence: int = 0
    previous_hash: str | None = None
    pending: tuple[PendingExternalEffect, ...] = ()
    completed_keys: frozenset[tuple[object, ...]] = frozenset()

    def __post_init__(self) -> None:
        if type(self.previous_sequence) is not int or self.previous_sequence < 0:
            raise ValueError("previous_sequence must be non-negative")
        if self.previous_hash is not None and type(self.previous_hash) is not str:
            raise TypeError("previous_hash must be a string or null")
        if type(self.pending) is not tuple or not all(
            type(item) is PendingExternalEffect for item in self.pending
        ):
            raise TypeError("pending must contain PendingExternalEffect values")
        if type(self.completed_keys) is not frozenset:
            raise TypeError("completed_keys must be a frozenset")


def advance_external_effect_projection(
    projection: ExternalEffectProjection,
    event: JournalEvent,
) -> ExternalEffectProjection:
    """Advance the derived external-effect projection by one Journal event."""

    if type(projection) is not ExternalEffectProjection:
        raise TypeError("projection must be a ExternalEffectProjection")
    if type(event) is not JournalEvent:
        raise TypeError("event must be a JournalEvent")
    if event.sequence <= projection.previous_sequence:
        raise DispatchRecoveryProjectionError(
            "Journal events must be in strictly increasing sequence order"
        )
    if (
        projection.previous_hash is not None
        and event.previous_event_hash != projection.previous_hash
    ):
        raise DispatchRecoveryProjectionError(
            "Journal events do not form one contiguous hash chain"
        )

    pending = {
        _external_effect_key(item.request_event): item for item in projection.pending
    }
    completed = set(projection.completed_keys)
    key = _external_effect_key(event)
    if key is not None:
        if event.event_type is JournalEventType.EFFECT_REQUESTED:
            payload = event.payload
            assert type(payload) is EffectRequestPayload
            if payload.expected_sequence != event.sequence - 1:
                raise DispatchRecoveryProjectionError(
                    "external request is not bound to its Journal predecessor"
                )
            if key in pending or key in completed:
                raise DispatchRecoveryProjectionError(
                    "external operation identity is reused"
                )
            pending[key] = PendingExternalEffect(event)
        else:
            request = pending.get(key)
            if request is None:
                raise DispatchRecoveryProjectionError(
                    "external observation has no matching request"
                )
            payload = event.payload
            assert type(payload) is EffectObservationPayload
            if payload.receipt.operation is not request.operation:
                raise DispatchRecoveryProjectionError(
                    "external observation operation does not match its request"
                )
            if payload.receipt.status is EffectStatus.APPLIED:
                pending.pop(key)
                completed.add(key)

    return ExternalEffectProjection(
        event.sequence,
        event.event_hash,
        tuple(sorted(pending.values(), key=lambda item: item.request_event.sequence)),
        frozenset(completed),
    )


def project_pending_external_effects(
    events: tuple[JournalEvent, ...],
) -> tuple[PendingExternalEffect, ...]:
    """Pair external requests with observed/reconciled events in Journal order."""

    if type(events) is not tuple or not all(type(event) is JournalEvent for event in events):
        raise TypeError("events must contain JournalEvent values")
    projection = ExternalEffectProjection()
    for event in events:
        projection = advance_external_effect_projection(projection, event)
    return projection.pending


def _external_effect_key(event: JournalEvent) -> tuple[object, ...] | None:
    if event.event_type is JournalEventType.EFFECT_REQUESTED:
        payload = event.payload
        if (
            type(payload) is not EffectRequestPayload
            or payload.operation not in _CHILD_EFFECT_ADAPTER
            or payload.adapter is not _CHILD_EFFECT_ADAPTER[payload.operation]
        ):
            return None
    elif event.event_type in {
        JournalEventType.EFFECT_OBSERVED,
        JournalEventType.EFFECT_RECONCILED,
    }:
        payload = event.payload
        if (
            type(payload) is not EffectObservationPayload
            or payload.receipt.operation not in _CHILD_EFFECT_ADAPTER
            or payload.adapter
            is not _CHILD_EFFECT_ADAPTER[payload.receipt.operation]
        ):
            return None
    else:
        return None
    identity = (
        event.identity
        if event.event_type is JournalEventType.EFFECT_REQUESTED
        else event.payload.receipt.identity
    )
    return (
        identity.run_id,
        identity.coordinator_epoch,
        identity.task_id,
        identity.attempt,
        identity.correlation_id,
    )


@dataclass(frozen=True, slots=True)
class DispatchRecoveryRecord:
    recovery_id: str
    proof_event: JournalEvent
    task_retry_event: JournalEvent | None = None
    run_resumed_event: JournalEvent | None = None

    def __post_init__(self) -> None:
        if type(self.recovery_id) is not str or not self.recovery_id:
            raise ValueError("recovery_id must be non-empty")
        if (
            type(self.proof_event) is not JournalEvent
            or self.proof_event.event_type is not JournalEventType.RECOVERY_COMPLETED
            or type(self.proof_event.payload) is not DispatchRecoveryPayload
            or self.proof_event.payload.recovery_id != self.recovery_id
        ):
            raise ValueError("proof_event must contain this dispatch recovery")
        if self.task_retry_event is not None and (
            type(self.task_retry_event) is not JournalEvent
            or self.task_retry_event.event_type
            is not JournalEventType.TASK_RETRY_SCHEDULED
        ):
            raise ValueError("task_retry_event must be a task retry event or null")
        if self.run_resumed_event is not None and (
            type(self.run_resumed_event) is not JournalEvent
            or self.run_resumed_event.event_type is not JournalEventType.RUN_RESUMED
        ):
            raise ValueError("run_resumed_event must be a run resumed event or null")
        if self.run_resumed_event is not None and self.task_retry_event is None:
            raise ValueError("run resume requires a prior task retry")
        if self.task_retry_event is not None:
            if not _matches_transition(
                self.task_retry_event,
                self,
                event_type=JournalEventType.TASK_RETRY_SCHEDULED,
                subject=TransitionSubject.TASK,
                from_state=RuntimeState.BLOCKED,
                to_state=RuntimeState.READY,
            ):
                raise ValueError("task_retry_event does not match its recovery proof")
            if self.task_retry_event.sequence <= self.proof_event.sequence:
                raise ValueError("task retry must follow its recovery proof")
        if self.run_resumed_event is not None:
            if not _matches_transition(
                self.run_resumed_event,
                self,
                event_type=JournalEventType.RUN_RESUMED,
                subject=TransitionSubject.RUN,
                from_state=RuntimeState.BLOCKED,
                to_state=RuntimeState.RUNNING,
            ):
                raise ValueError("run_resumed_event does not match its recovery proof")
            assert self.task_retry_event is not None
            if self.run_resumed_event.sequence <= self.task_retry_event.sequence:
                raise ValueError("run resume must follow its task retry")

    @property
    def payload(self) -> DispatchRecoveryPayload:
        payload = self.proof_event.payload
        assert type(payload) is DispatchRecoveryPayload
        return payload

    @property
    def complete(self) -> bool:
        return self.run_resumed_event is not None


def _matches_transition(
    event: JournalEvent,
    record: DispatchRecoveryRecord,
    *,
    event_type: JournalEventType,
    subject: TransitionSubject,
    from_state: RuntimeState,
    to_state: RuntimeState,
) -> bool:
    payload = record.payload
    transition = event.payload
    task_id = payload.subject_identity.task_id
    return (
        event.event_type is event_type
        and type(transition) is TransitionPayload
        and transition.subject is subject
        and transition.from_state is from_state
        and transition.to_state is to_state
        and transition.evidence == payload.evidence
        and event.identity.run_id == payload.subject_identity.run_id
        and event.identity.coordinator_epoch
        >= record.proof_event.identity.coordinator_epoch
        and (
            event.identity.task_id == task_id
            if subject is TransitionSubject.TASK
            else event.identity.task_id is None
        )
        and event.identity.attempt is None
        and event.identity.correlation_id is None
        and event.actor_type is payload.command.actor.actor_type
        and event.actor_id == payload.command.actor.actor_id
    )


def advance_dispatch_recoveries(
    records: tuple[DispatchRecoveryRecord, ...],
    event: JournalEvent,
) -> tuple[DispatchRecoveryRecord, ...]:
    """Advance the one-at-a-time recovery protocol from a verified event."""

    if type(records) is not tuple or not all(
        type(record) is DispatchRecoveryRecord for record in records
    ):
        raise TypeError("records must contain DispatchRecoveryRecord values")
    if type(event) is not JournalEvent:
        raise TypeError("event must be a JournalEvent")
    incomplete = tuple(record for record in records if not record.complete)
    if len(incomplete) > 1:
        raise DispatchRecoveryProjectionError(
            "multiple dispatch recovery prefixes are incomplete"
        )

    if type(event.payload) is DispatchRecoveryPayload:
        recovery_id = event.payload.recovery_id
        existing = next(
            (record for record in records if record.recovery_id == recovery_id),
            None,
        )
        if existing is not None:
            if existing.proof_event == event:
                return records
            raise DispatchRecoveryProjectionError(
                "recovery_id identifies a different proof event"
            )
        if incomplete:
            raise DispatchRecoveryProjectionError(
                "a second recovery proof cannot interrupt an incomplete prefix"
            )
        return (*records, DispatchRecoveryRecord(recovery_id, event))

    if not incomplete:
        if any(
            record.complete
            and type(event.payload) is TransitionPayload
            and event.payload.evidence == record.payload.evidence
            and event.event_type
            in {
                JournalEventType.TASK_RETRY_SCHEDULED,
                JournalEventType.RUN_RESUMED,
            }
            and event.identity.run_id == record.payload.subject_identity.run_id
            for record in records
        ):
            raise DispatchRecoveryProjectionError(
                "a completed recovery prefix cannot be replayed out of order"
            )
        return records
    record = incomplete[0]
    if type(event.payload) is LeasePayload:
        return records
    index = records.index(record)
    if record.task_retry_event is None:
        if not _matches_transition(
            event,
            record,
            event_type=JournalEventType.TASK_RETRY_SCHEDULED,
            subject=TransitionSubject.TASK,
            from_state=RuntimeState.BLOCKED,
            to_state=RuntimeState.READY,
        ):
            raise DispatchRecoveryProjectionError(
                "recovery proof must be followed by its task retry"
            )
        updated = replace(record, task_retry_event=event)
    else:
        if not _matches_transition(
            event,
            record,
            event_type=JournalEventType.RUN_RESUMED,
            subject=TransitionSubject.RUN,
            from_state=RuntimeState.BLOCKED,
            to_state=RuntimeState.RUNNING,
        ):
            raise DispatchRecoveryProjectionError(
                "recovery task retry must be followed by its run resume"
            )
        updated = replace(record, run_resumed_event=event)
    return (*records[:index], updated, *records[index + 1 :])


__all__ = [
    "DispatchRecoveryProjectionError",
    "DispatchRecoveryRecord",
    "PendingExternalEffect",
    "ExternalEffectProjection",
    "advance_external_effect_projection",
    "advance_dispatch_recoveries",
    "project_pending_external_effects",
]
