"""Journal-owned child effects for Wish Builder backend dispatch operations."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable

from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectRequestPayload,
    EffectStatus,
    EvidenceRef,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
)
from wish_builder.services.journal import (
    AppendResult,
    AppendStatus,
    DurableJournal,
    JournalEventDraft,
    JournalHead,
)
from wish_builder.services.ports import (
    BackendChannelPort,
    CancelTurn,
    ChannelObservation,
    PreparedEffect,
    PersistedEffectRequest,
    ReserveChannel,
    SendTaskPacket,
    TurnObservation,
)


class BackendDispatchEffectStatus(StrEnum):
    APPLIED = "applied"
    BLOCKED = "blocked"


class BackendDispatchEffectReason(StrEnum):
    NONE = "none"
    PARENT_REQUEST_INVALID = "parent_request_invalid"
    JOURNAL_CONFLICT = "journal_conflict"
    PERSISTENCE_FAILED = "persistence_failed"
    OBSERVATION_INVALID = "observation_invalid"
    EVIDENCE_NOT_DURABLE = "evidence_not_durable"
    EFFECT_ABSENT = "effect_absent"
    EFFECT_OUTCOME_UNKNOWN = "effect_outcome_unknown"


class BackendDispatchEffectCrash(RuntimeError):
    """Deliberate test-only interruption at a named child-effect boundary."""


@runtime_checkable
class BackendObservationStorePort(Protocol):
    def put(
        self,
        observation: ChannelObservation | TurnObservation,
        *,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> EvidenceRef: ...


class BackendDispatchEffectFailpoint(Protocol):
    def __call__(self, point: str, operation_id: str) -> None: ...


class BackendDispatchEffectAdmitter(Protocol):
    def __call__(self, head: JournalHead, identity: ExecutionIdentity) -> bool: ...


@dataclass(frozen=True, slots=True)
class BackendDispatchPlan:
    reserve: ReserveChannel
    send: SendTaskPacket

    def __post_init__(self) -> None:
        if type(self.reserve) is not ReserveChannel:
            raise TypeError("reserve must be a ReserveChannel")
        if type(self.send) is not SendTaskPacket:
            raise TypeError("send must be a SendTaskPacket")
        if self.reserve.operation_id == self.send.operation_id:
            raise ValueError("child operation IDs must be unique")
        if (
            self.reserve.attempt_id != self.send.attempt_id
            or self.reserve.dispatch_id != self.send.dispatch_id
            or self.reserve.channel_id != self.send.channel_id
        ):
            raise ValueError("reserve and send must bind the same dispatch channel")


@dataclass(frozen=True, slots=True)
class BackendDispatchResult:
    status: BackendDispatchEffectStatus
    reason: BackendDispatchEffectReason
    head: JournalHead
    events: tuple[JournalEvent, ...] = ()
    receipt: EffectReceipt | None = None
    reservation: ChannelObservation | None = None
    turn: TurnObservation | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not BackendDispatchEffectStatus:
            raise TypeError("status must be a BackendDispatchEffectStatus")
        if type(self.reason) is not BackendDispatchEffectReason:
            raise TypeError("reason must be a BackendDispatchEffectReason")
        if type(self.head) is not JournalHead:
            raise TypeError("head must be a JournalHead")
        if type(self.events) is not tuple or not all(
            type(event) is JournalEvent for event in self.events
        ):
            raise TypeError("events must contain JournalEvent values")
        if self.receipt is not None and type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt or null")
        if self.reservation is not None and type(self.reservation) is not ChannelObservation:
            raise TypeError("reservation must be a ChannelObservation or null")
        if self.turn is not None and type(self.turn) is not TurnObservation:
            raise TypeError("turn must be a TurnObservation or null")
        if self.events and (
            self.events[-1].sequence != self.head.sequence
            or self.events[-1].event_hash != self.head.event_hash
            or any(
                current.sequence != previous.sequence + 1
                or current.previous_event_hash != previous.event_hash
                for previous, current in zip(self.events, self.events[1:])
            )
        ):
            raise ValueError("events must form one chain ending at head")
        if self.status is BackendDispatchEffectStatus.APPLIED:
            if (
                self.reason is not BackendDispatchEffectReason.NONE
                or self.receipt is None
                or self.receipt.status is not EffectStatus.APPLIED
                or self.turn is None
            ):
                raise ValueError("applied child effects require an applied turn observation")
            if self.receipt.operation is EffectOperation.WORKER_DISPATCH:
                if self.reservation is None:
                    raise ValueError(
                        "applied dispatches require both child observations"
                    )
            elif self.receipt.operation is EffectOperation.CANCEL_TURN:
                if self.reservation is not None:
                    raise ValueError(
                        "applied cancellations must not contain a reservation observation"
                    )
            else:
                raise ValueError("applied result has an unsupported operation")
        elif self.reason is BackendDispatchEffectReason.NONE:
            raise ValueError("blocked child effects require a reason")


@runtime_checkable
class BackendDispatchPort(Protocol):
    def dispatch(
        self,
        parent: PersistedEffectRequest,
        plan: BackendDispatchPlan,
    ) -> BackendDispatchResult: ...

    def cancel(
        self,
        parent: PersistedEffectRequest,
        command: CancelTurn,
        *,
        expected_head: JournalHead,
    ) -> BackendDispatchResult: ...


@dataclass(frozen=True, slots=True)
class _ChildResult:
    head: JournalHead
    events: tuple[JournalEvent, ...]
    receipt: EffectReceipt | None
    observation: ChannelObservation | TurnObservation | None
    reason: BackendDispatchEffectReason


class BackendDispatchEffectService:
    """Persist each backend operation intent before invoking the provider port."""

    def __init__(
        self,
        journal: DurableJournal,
        channel: BackendChannelPort,
        evidence_store: BackendObservationStorePort,
        *,
        coordinator_id: str,
        fencing_token: int,
        effect_admitter: BackendDispatchEffectAdmitter | None = None,
        failpoint: BackendDispatchEffectFailpoint | None = None,
    ) -> None:
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        if not isinstance(channel, BackendChannelPort):
            raise TypeError("channel must implement BackendChannelPort")
        if not isinstance(evidence_store, BackendObservationStorePort):
            raise TypeError("evidence_store must implement BackendObservationStorePort")
        if type(coordinator_id) is not str or not coordinator_id:
            raise ValueError("coordinator_id must be non-empty")
        if type(fencing_token) is not int or fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if effect_admitter is not None and not callable(effect_admitter):
            raise TypeError("effect_admitter must be callable or null")
        if failpoint is not None and not callable(failpoint):
            raise TypeError("failpoint must be callable or null")
        self._journal = journal
        self._channel = channel
        self._evidence_store = evidence_store
        self._coordinator_id = coordinator_id
        self._fencing_token = fencing_token
        self._effect_admitter = effect_admitter
        self._failpoint = failpoint

    def dispatch(
        self,
        parent: PersistedEffectRequest,
        plan: BackendDispatchPlan,
    ) -> BackendDispatchResult:
        if type(parent) is not PersistedEffectRequest:
            raise TypeError("parent must be a PersistedEffectRequest")
        if type(plan) is not BackendDispatchPlan:
            raise TypeError("plan must be a BackendDispatchPlan")
        if not self._valid_parent(parent, plan):
            assert parent.append_result.head is not None
            return BackendDispatchResult(
                BackendDispatchEffectStatus.BLOCKED,
                BackendDispatchEffectReason.PARENT_REQUEST_INVALID,
                parent.append_result.head,
            )
        assert parent.append_result.head is not None
        reserve = self._apply_child(
            parent.identity,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
            EffectObjectType.CHANNEL,
            parent.append_result.head,
        )
        events = list(reserve.events)
        reservation = (
            reserve.observation
            if type(reserve.observation) is ChannelObservation
            else None
        )
        if reserve.receipt is None:
            return BackendDispatchResult(
                BackendDispatchEffectStatus.BLOCKED,
                reserve.reason,
                reserve.head,
                tuple(events),
                reservation=reservation,
            )
        if reserve.receipt.status is not EffectStatus.APPLIED:
            receipt = self._parent_receipt(parent.identity, reserve.receipt)
            return BackendDispatchResult(
                BackendDispatchEffectStatus.BLOCKED,
                self._status_reason(receipt.status),
                reserve.head,
                tuple(events),
                receipt,
                reservation,
            )

        sent = self._apply_child(
            parent.identity,
            plan.send,
            EffectOperation.SEND_TASK_PACKET,
            EffectObjectType.TASK_PACKET,
            reserve.head,
        )
        events.extend(sent.events)
        turn = sent.observation if type(sent.observation) is TurnObservation else None
        if sent.receipt is None:
            return BackendDispatchResult(
                BackendDispatchEffectStatus.BLOCKED,
                sent.reason,
                sent.head,
                tuple(events),
                reservation=reservation,
                turn=turn,
            )
        receipt = self._parent_receipt(parent.identity, reserve.receipt, sent.receipt)
        status = (
            BackendDispatchEffectStatus.APPLIED
            if receipt.status is EffectStatus.APPLIED
            else BackendDispatchEffectStatus.BLOCKED
        )
        reason = (
            BackendDispatchEffectReason.NONE
            if status is BackendDispatchEffectStatus.APPLIED
            else self._status_reason(receipt.status)
        )
        return BackendDispatchResult(
            status,
            reason,
            sent.head,
            tuple(events),
            receipt,
            reservation,
            turn,
        )

    def cancel(
        self,
        parent: PersistedEffectRequest,
        command: CancelTurn,
        *,
        expected_head: JournalHead,
    ) -> BackendDispatchResult:
        """Persist and observe one cancellation independently of dispatch."""

        if type(parent) is not PersistedEffectRequest:
            raise TypeError("parent must be a PersistedEffectRequest")
        if type(command) is not CancelTurn:
            raise TypeError("command must be a CancelTurn")
        if type(expected_head) is not JournalHead:
            raise TypeError("expected_head must be a JournalHead")
        if (
            not self._valid_cancel_parent(parent)
            or command.operation_id == parent.identity.correlation_id
            or expected_head.sequence < parent.event.sequence
        ):
            return BackendDispatchResult(
                BackendDispatchEffectStatus.BLOCKED,
                BackendDispatchEffectReason.PARENT_REQUEST_INVALID,
                expected_head,
            )

        cancelled = self._apply_child(
            parent.identity,
            command,
            EffectOperation.CANCEL_TURN,
            EffectObjectType.TURN,
            expected_head,
            request_identity=replace(
                parent.identity,
                coordinator_epoch=self._fencing_token,
                correlation_id=command.operation_id,
            ),
        )
        turn = (
            cancelled.observation
            if type(cancelled.observation) is TurnObservation
            else None
        )
        if cancelled.receipt is None:
            return BackendDispatchResult(
                BackendDispatchEffectStatus.BLOCKED,
                cancelled.reason,
                cancelled.head,
                cancelled.events,
                turn=turn,
            )
        status = (
            BackendDispatchEffectStatus.APPLIED
            if cancelled.receipt.status is EffectStatus.APPLIED
            else BackendDispatchEffectStatus.BLOCKED
        )
        reason = (
            BackendDispatchEffectReason.NONE
            if status is BackendDispatchEffectStatus.APPLIED
            else self._status_reason(cancelled.receipt.status)
        )
        return BackendDispatchResult(
            status,
            reason,
            cancelled.head,
            cancelled.events,
            cancelled.receipt,
            turn=turn,
        )

    def _apply_child(
        self,
        parent_identity: ExecutionIdentity,
        command: ReserveChannel | SendTaskPacket | CancelTurn,
        operation: EffectOperation,
        object_type: EffectObjectType,
        expected_head: JournalHead,
        *,
        request_identity: ExecutionIdentity | None = None,
    ) -> _ChildResult:
        identity = request_identity or replace(
            parent_identity,
            correlation_id=command.operation_id,
        )
        request = EffectRequestPayload(
            operation,
            AdapterKind.BACKEND,
            object_type,
            self._target_hash(parent_identity, command, operation),
            command.canonical_sha256(),
            expected_head.sequence,
            self._fencing_token,
        )
        appended = self._append(
            JournalEventType.EFFECT_REQUESTED,
            identity,
            request,
            expected_head,
            ActorType.COORDINATOR,
            self._coordinator_id,
        )
        if not appended.durable or appended.event is None or appended.head is None:
            return _ChildResult(
                expected_head, (), None, None, self._append_reason(appended)
            )
        events = [appended.event]
        self._trigger("after_request_append", command.operation_id)
        if self._effect_admitter is not None:
            try:
                effect_admitted = self._effect_admitter(appended.head, identity)
            except Exception:  # noqa: BLE001 - admission failures stop the effect
                effect_admitted = False
            if effect_admitted is not True:
                return _ChildResult(
                    appended.head,
                    tuple(events),
                    None,
                    None,
                    BackendDispatchEffectReason.EFFECT_ABSENT,
                )
        effect = PreparedEffect.from_append_result(appended, command)
        observation: ChannelObservation | TurnObservation
        if type(command) is ReserveChannel:
            observation = self._channel.reserve(effect)
        elif type(command) is SendTaskPacket:
            observation = self._channel.send(effect)
        else:
            observation = self._channel.cancel(effect)
        self._trigger("after_adapter_call", command.operation_id)
        expected_type = (
            ChannelObservation if type(command) is ReserveChannel else TurnObservation
        )
        if (
            type(observation) is not expected_type
            or observation.operation_id != command.operation_id
        ):
            return _ChildResult(
                appended.head,
                tuple(events),
                None,
                None,
                BackendDispatchEffectReason.OBSERVATION_INVALID,
            )
        evidence = self._evidence_store.put(
            observation,
            identity=identity,
            operation=operation,
        )
        if type(evidence) is not EvidenceRef:
            return _ChildResult(
                appended.head,
                tuple(events),
                None,
                observation,
                BackendDispatchEffectReason.EVIDENCE_NOT_DURABLE,
            )
        self._trigger("after_evidence_store", command.operation_id)
        receipt = self._child_receipt(identity, operation, observation, evidence)
        observed = self._append(
            JournalEventType.EFFECT_OBSERVED,
            identity,
            EffectObservationPayload(AdapterKind.BACKEND, receipt),
            appended.head,
            ActorType.ADAPTER,
            "backend-channel-adapter",
        )
        if not observed.durable or observed.event is None or observed.head is None:
            return _ChildResult(
                appended.head,
                tuple(events),
                None,
                observation,
                self._append_reason(observed),
            )
        events.append(observed.event)
        self._trigger("after_observation_append", command.operation_id)
        return _ChildResult(
            observed.head,
            tuple(events),
            receipt,
            observation,
            BackendDispatchEffectReason.NONE,
        )

    def _append(
        self,
        event_type: JournalEventType,
        identity: ExecutionIdentity,
        payload: EffectRequestPayload | EffectObservationPayload,
        expected_head: JournalHead,
        actor_type: ActorType,
        actor_id: str,
    ) -> AppendResult:
        sequence = expected_head.sequence + 1
        draft = JournalEventDraft(
            event_id=(
                f"EVENT-{event_type.value.replace('_', '-').upper()}-"
                f"{sequence:08d}"
            ),
            event_type=event_type,
            identity=identity,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload,
        )
        return self._journal.append_draft(draft, expected_head=expected_head)

    def _valid_parent(
        self,
        parent: PersistedEffectRequest,
        plan: BackendDispatchPlan,
    ) -> bool:
        return (
            self._valid_parent_request(parent)
            and plan.reserve.dispatch_id == parent.identity.correlation_id
            and plan.send.dispatch_id == parent.identity.correlation_id
        )

    def _valid_parent_request(self, parent: PersistedEffectRequest) -> bool:
        payload = parent.payload
        identity = parent.identity
        return (
            parent.event.event_type is JournalEventType.DISPATCH_REQUESTED
            and payload.operation is EffectOperation.WORKER_DISPATCH
            and payload.adapter is AdapterKind.TASK
            and payload.object_type is EffectObjectType.WORKER
            and identity.is_attempt
            and identity.coordinator_epoch == self._fencing_token
            and identity.correlation_id is not None
        )

    def _valid_cancel_parent(self, parent: PersistedEffectRequest) -> bool:
        payload = parent.payload
        identity = parent.identity
        return (
            parent.event.event_type is JournalEventType.DISPATCH_REQUESTED
            and payload.operation is EffectOperation.WORKER_DISPATCH
            and payload.adapter is AdapterKind.TASK
            and payload.object_type is EffectObjectType.WORKER
            and identity.is_attempt
            and 0 < identity.coordinator_epoch <= self._fencing_token
            and payload.fencing_token == identity.coordinator_epoch
            and identity.correlation_id is not None
        )

    @staticmethod
    def _target_hash(
        parent_identity: ExecutionIdentity,
        command: ReserveChannel | SendTaskPacket | CancelTurn,
        operation: EffectOperation,
    ) -> str:
        dispatch_id = (
            parent_identity.correlation_id
            if type(command) is CancelTurn
            else command.dispatch_id
        )
        assert dispatch_id is not None
        target: dict[str, object] = {
            "adapter": AdapterKind.BACKEND.value,
            "attempt_id": command.attempt_id,
            "channel_id": command.channel_id,
            "operation": operation.value,
            "run_id": parent_identity.run_id,
            "task_id": parent_identity.task_id,
            "dispatch_id": dispatch_id,
        }
        if type(command) is SendTaskPacket:
            target.update(
                {
                    "message_id": command.message_id,
                    "turn_id": command.turn_id,
                }
            )
        elif type(command) is CancelTurn:
            target["turn_id"] = command.turn_id
        return "sha256:" + canonical_sha256(target)

    @staticmethod
    def _child_receipt(
        identity: ExecutionIdentity,
        operation: EffectOperation,
        observation: ChannelObservation | TurnObservation,
        evidence: EvidenceRef,
    ) -> EffectReceipt:
        external_id = None
        if observation.status is EffectStatus.APPLIED:
            external_id = (
                observation.channel_id
                if type(observation) is ChannelObservation
                else observation.turn_id
            )
        return EffectReceipt(
            1,
            identity,
            operation,
            observation.status,
            observation.observed_at,
            effect_hash=observation.effect_digest,
            external_object_id=external_id,
            evidence=(evidence,),
        )

    @staticmethod
    def _parent_receipt(
        identity: ExecutionIdentity,
        *children: EffectReceipt,
    ) -> EffectReceipt:
        terminal = children[-1]
        status = terminal.status
        evidence = tuple(
            sorted(
                {
                    item.digest: item
                    for child in children
                    for item in child.evidence
                }.values(),
                key=lambda item: item.digest,
            )
        )
        return EffectReceipt(
            1,
            identity,
            EffectOperation.WORKER_DISPATCH,
            status,
            terminal.observed_at,
            effect_hash=(
                "sha256:"
                + canonical_sha256([child.to_primitive() for child in children])
                if status is EffectStatus.APPLIED
                else None
            ),
            external_object_id=(
                terminal.external_object_id if status is EffectStatus.APPLIED else None
            ),
            evidence=evidence,
        )

    @staticmethod
    def _append_reason(result: AppendResult) -> BackendDispatchEffectReason:
        return (
            BackendDispatchEffectReason.JOURNAL_CONFLICT
            if result.status is AppendStatus.CONFLICT
            else BackendDispatchEffectReason.PERSISTENCE_FAILED
        )

    @staticmethod
    def _status_reason(status: EffectStatus) -> BackendDispatchEffectReason:
        return (
            BackendDispatchEffectReason.EFFECT_OUTCOME_UNKNOWN
            if status is EffectStatus.UNKNOWN
            else BackendDispatchEffectReason.EFFECT_ABSENT
        )

    def _trigger(self, point: str, operation_id: str) -> None:
        if self._failpoint is not None:
            self._failpoint(point, operation_id)


__all__ = [
    "BackendDispatchEffectCrash",
    "BackendDispatchEffectAdmitter",
    "BackendDispatchPort",
    "BackendDispatchEffectReason",
    "BackendDispatchResult",
    "BackendDispatchEffectService",
    "BackendDispatchEffectStatus",
    "BackendDispatchPlan",
    "BackendObservationStorePort",
]
