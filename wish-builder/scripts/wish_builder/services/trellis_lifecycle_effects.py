"""Journal-owned Trellis attempt lifecycle effects."""

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
    AttemptObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PreparedEffect,
    PrepareAttempt,
    TrellisLifecyclePort,
)

LifecycleCommand = PrepareAttempt | CheckAttempt | FinishAttempt
LifecycleObservation = AttemptObservation | CheckObservation | FinishObservation


def lifecycle_target_object_hash(
    identity: ExecutionIdentity,
    command: LifecycleCommand,
    operation: EffectOperation,
) -> str:
    """Return the immutable Trellis attempt target bound into the Journal."""

    if type(identity) is not ExecutionIdentity or not identity.is_attempt:
        raise ValueError("identity must be a complete attempt identity")
    if type(command) not in {PrepareAttempt, CheckAttempt, FinishAttempt}:
        raise TypeError("command must be a Trellis lifecycle command")
    expected_operation = {
        PrepareAttempt: EffectOperation.PREPARE_ATTEMPT,
        CheckAttempt: EffectOperation.CHECK_ATTEMPT,
        FinishAttempt: EffectOperation.FINISH_ATTEMPT,
    }[type(command)]
    if operation is not expected_operation:
        raise ValueError("operation does not match the lifecycle command")
    if identity.task_id != command.task_id:
        raise ValueError("command task does not match the attempt identity")

    target: dict[str, object] = {
        "adapter": AdapterKind.TRELLIS.value,
        "attempt": identity.attempt,
        "operation": operation.value,
        "run_id": identity.run_id,
        "task_id": identity.task_id,
        "trellis_task_id": command.trellis_task_id,
    }
    if type(command) is PrepareAttempt:
        if identity.run_id != command.run_id or identity.attempt != command.attempt:
            raise ValueError("prepare command does not match the attempt identity")
        target["dispatch_id"] = command.dispatch_id
        target["parent_task_id"] = command.parent_task_id
    else:
        target["attempt_id"] = command.attempt_id
    return "sha256:" + canonical_sha256(target)


class TrellisLifecycleEffectStatus(StrEnum):
    APPLIED = "applied"
    BLOCKED = "blocked"


class TrellisLifecycleEffectReason(StrEnum):
    NONE = "none"
    REQUEST_INVALID = "request_invalid"
    JOURNAL_CONFLICT = "journal_conflict"
    PERSISTENCE_FAILED = "persistence_failed"
    OBSERVATION_INVALID = "observation_invalid"
    EVIDENCE_NOT_DURABLE = "evidence_not_durable"
    EFFECT_ABSENT = "effect_absent"
    EFFECT_OUTCOME_UNKNOWN = "effect_outcome_unknown"


class TrellisLifecycleEffectCrash(RuntimeError):
    """Deliberate test-only interruption at a lifecycle effect boundary."""


@runtime_checkable
class TrellisLifecycleObservationStorePort(Protocol):
    def put(
        self,
        observation: LifecycleObservation,
        *,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> EvidenceRef: ...


class TrellisLifecycleEffectFailpoint(Protocol):
    def __call__(self, point: str, operation_id: str) -> None: ...


def _validate_result(
    *,
    status: TrellisLifecycleEffectStatus,
    reason: TrellisLifecycleEffectReason,
    head: JournalHead,
    events: tuple[JournalEvent, ...],
    receipt: EffectReceipt | None,
    observation: LifecycleObservation | None,
    operation: EffectOperation,
    observation_type: type[LifecycleObservation],
) -> None:
    if type(status) is not TrellisLifecycleEffectStatus:
        raise TypeError("status must be a TrellisLifecycleEffectStatus")
    if type(reason) is not TrellisLifecycleEffectReason:
        raise TypeError("reason must be a TrellisLifecycleEffectReason")
    if type(head) is not JournalHead:
        raise TypeError("head must be a JournalHead")
    if type(events) is not tuple or not all(
        type(event) is JournalEvent for event in events
    ):
        raise TypeError("events must contain JournalEvent values")
    if receipt is not None and type(receipt) is not EffectReceipt:
        raise TypeError("receipt must be an EffectReceipt or null")
    if observation is not None and type(observation) is not observation_type:
        raise TypeError(
            f"observation must be a {observation_type.__name__} or null"
        )
    if events and (
        events[-1].sequence != head.sequence
        or events[-1].event_hash != head.event_hash
        or any(
            current.sequence != previous.sequence + 1
            or current.previous_event_hash != previous.event_hash
            for previous, current in zip(events, events[1:])
        )
    ):
        raise ValueError("events must form one chain ending at head")
    if receipt is not None:
        if receipt.operation is not operation:
            raise ValueError("receipt operation does not match result type")
        if observation is None:
            raise ValueError("a receipt requires its typed observation")
        if (
            receipt.identity.correlation_id != observation.operation_id
            or receipt.status is not observation.status
            or receipt.effect_hash != observation.effect_digest
            or not receipt.evidence
        ):
            raise ValueError("receipt does not match the lifecycle observation")
    if status is TrellisLifecycleEffectStatus.APPLIED:
        if (
            reason is not TrellisLifecycleEffectReason.NONE
            or receipt is None
            or receipt.status is not EffectStatus.APPLIED
            or observation is None
        ):
            raise ValueError("applied lifecycle effects require an applied observation")
        return
    if reason is TrellisLifecycleEffectReason.NONE:
        raise ValueError("blocked lifecycle effects require a reason")
    if reason in {
        TrellisLifecycleEffectReason.EFFECT_ABSENT,
        TrellisLifecycleEffectReason.EFFECT_OUTCOME_UNKNOWN,
    }:
        expected_status = (
            EffectStatus.UNKNOWN
            if reason is TrellisLifecycleEffectReason.EFFECT_OUTCOME_UNKNOWN
            else EffectStatus.ABSENT
        )
        if receipt is None or receipt.status is not expected_status:
            raise ValueError("known blocked outcomes require their durable receipt")
    elif receipt is not None:
        raise ValueError("an unobserved blocked effect cannot expose a receipt")


@dataclass(frozen=True, slots=True)
class PrepareAttemptEffectResult:
    status: TrellisLifecycleEffectStatus
    reason: TrellisLifecycleEffectReason
    head: JournalHead
    events: tuple[JournalEvent, ...] = ()
    receipt: EffectReceipt | None = None
    observation: AttemptObservation | None = None

    def __post_init__(self) -> None:
        _validate_result(
            status=self.status,
            reason=self.reason,
            head=self.head,
            events=self.events,
            receipt=self.receipt,
            observation=self.observation,
            operation=EffectOperation.PREPARE_ATTEMPT,
            observation_type=AttemptObservation,
        )


@dataclass(frozen=True, slots=True)
class CheckAttemptEffectResult:
    status: TrellisLifecycleEffectStatus
    reason: TrellisLifecycleEffectReason
    head: JournalHead
    events: tuple[JournalEvent, ...] = ()
    receipt: EffectReceipt | None = None
    observation: CheckObservation | None = None

    def __post_init__(self) -> None:
        _validate_result(
            status=self.status,
            reason=self.reason,
            head=self.head,
            events=self.events,
            receipt=self.receipt,
            observation=self.observation,
            operation=EffectOperation.CHECK_ATTEMPT,
            observation_type=CheckObservation,
        )


@dataclass(frozen=True, slots=True)
class FinishAttemptEffectResult:
    status: TrellisLifecycleEffectStatus
    reason: TrellisLifecycleEffectReason
    head: JournalHead
    events: tuple[JournalEvent, ...] = ()
    receipt: EffectReceipt | None = None
    observation: FinishObservation | None = None

    def __post_init__(self) -> None:
        _validate_result(
            status=self.status,
            reason=self.reason,
            head=self.head,
            events=self.events,
            receipt=self.receipt,
            observation=self.observation,
            operation=EffectOperation.FINISH_ATTEMPT,
            observation_type=FinishObservation,
        )


@dataclass(frozen=True, slots=True)
class _LifecycleApplication:
    head: JournalHead
    events: tuple[JournalEvent, ...]
    receipt: EffectReceipt | None
    observation: LifecycleObservation | None
    reason: TrellisLifecycleEffectReason


class TrellisLifecycleEffectService:
    """Persist intent and evidence around each Trellis lifecycle mutation."""

    def __init__(
        self,
        journal: DurableJournal,
        lifecycle: TrellisLifecyclePort,
        evidence_store: TrellisLifecycleObservationStorePort,
        *,
        coordinator_id: str,
        fencing_token: int,
        failpoint: TrellisLifecycleEffectFailpoint | None = None,
    ) -> None:
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        if not isinstance(lifecycle, TrellisLifecyclePort):
            raise TypeError("lifecycle must implement TrellisLifecyclePort")
        if not isinstance(evidence_store, TrellisLifecycleObservationStorePort):
            raise TypeError(
                "evidence_store must implement TrellisLifecycleObservationStorePort"
            )
        if type(coordinator_id) is not str or not coordinator_id:
            raise ValueError("coordinator_id must be non-empty")
        if type(fencing_token) is not int or fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if failpoint is not None and not callable(failpoint):
            raise TypeError("failpoint must be callable or null")
        self._journal = journal
        self._lifecycle = lifecycle
        self._evidence_store = evidence_store
        self._coordinator_id = coordinator_id
        self._fencing_token = fencing_token
        self._failpoint = failpoint

    def prepare(
        self,
        identity: ExecutionIdentity,
        command: PrepareAttempt,
        *,
        expected_head: JournalHead,
    ) -> PrepareAttemptEffectResult:
        self._require_arguments(identity, command, PrepareAttempt, expected_head)
        if not self._valid_binding(identity, command):
            return PrepareAttemptEffectResult(
                TrellisLifecycleEffectStatus.BLOCKED,
                TrellisLifecycleEffectReason.REQUEST_INVALID,
                expected_head,
            )
        applied = self._apply(
            identity,
            command,
            EffectOperation.PREPARE_ATTEMPT,
            AttemptObservation,
            expected_head,
        )
        observation = (
            applied.observation
            if type(applied.observation) is AttemptObservation
            else None
        )
        return PrepareAttemptEffectResult(
            self._result_status(applied.receipt),
            self._result_reason(applied),
            applied.head,
            applied.events,
            applied.receipt,
            observation,
        )

    def check(
        self,
        identity: ExecutionIdentity,
        command: CheckAttempt,
        *,
        expected_head: JournalHead,
    ) -> CheckAttemptEffectResult:
        self._require_arguments(identity, command, CheckAttempt, expected_head)
        if not self._valid_binding(identity, command):
            return CheckAttemptEffectResult(
                TrellisLifecycleEffectStatus.BLOCKED,
                TrellisLifecycleEffectReason.REQUEST_INVALID,
                expected_head,
            )
        applied = self._apply(
            identity,
            command,
            EffectOperation.CHECK_ATTEMPT,
            CheckObservation,
            expected_head,
        )
        observation = (
            applied.observation
            if type(applied.observation) is CheckObservation
            else None
        )
        return CheckAttemptEffectResult(
            self._result_status(applied.receipt),
            self._result_reason(applied),
            applied.head,
            applied.events,
            applied.receipt,
            observation,
        )

    def finish(
        self,
        identity: ExecutionIdentity,
        command: FinishAttempt,
        *,
        expected_head: JournalHead,
    ) -> FinishAttemptEffectResult:
        self._require_arguments(identity, command, FinishAttempt, expected_head)
        if not self._valid_binding(identity, command):
            return FinishAttemptEffectResult(
                TrellisLifecycleEffectStatus.BLOCKED,
                TrellisLifecycleEffectReason.REQUEST_INVALID,
                expected_head,
            )
        applied = self._apply(
            identity,
            command,
            EffectOperation.FINISH_ATTEMPT,
            FinishObservation,
            expected_head,
        )
        observation = (
            applied.observation
            if type(applied.observation) is FinishObservation
            else None
        )
        return FinishAttemptEffectResult(
            self._result_status(applied.receipt),
            self._result_reason(applied),
            applied.head,
            applied.events,
            applied.receipt,
            observation,
        )

    @staticmethod
    def _require_arguments(
        identity: ExecutionIdentity,
        command: object,
        command_type: type[LifecycleCommand],
        expected_head: JournalHead,
    ) -> None:
        if type(identity) is not ExecutionIdentity:
            raise TypeError("identity must be an ExecutionIdentity")
        if type(command) is not command_type:
            raise TypeError(f"command must be a {command_type.__name__}")
        if type(expected_head) is not JournalHead:
            raise TypeError("expected_head must be a JournalHead")

    def _valid_binding(
        self,
        identity: ExecutionIdentity,
        command: LifecycleCommand,
    ) -> bool:
        if (
            not identity.is_attempt
            or identity.coordinator_epoch != self._fencing_token
            or identity.task_id != command.task_id
        ):
            return False
        if type(command) is PrepareAttempt:
            return (
                identity.run_id == command.run_id
                and identity.attempt == command.attempt
                and identity.correlation_id == command.dispatch_id
            )
        return True

    def _apply(
        self,
        parent_identity: ExecutionIdentity,
        command: LifecycleCommand,
        operation: EffectOperation,
        observation_type: type[LifecycleObservation],
        expected_head: JournalHead,
    ) -> _LifecycleApplication:
        identity = replace(parent_identity, correlation_id=command.operation_id)
        request = EffectRequestPayload(
            operation,
            AdapterKind.TRELLIS,
            EffectObjectType.ATTEMPT,
            lifecycle_target_object_hash(parent_identity, command, operation),
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
            return _LifecycleApplication(
                expected_head,
                (),
                None,
                None,
                self._append_reason(appended),
            )
        events = [appended.event]
        self._trigger("after_request_append", command.operation_id)
        effect = PreparedEffect.from_append_result(appended, command)
        observation: LifecycleObservation
        if type(command) is PrepareAttempt:
            observation = self._lifecycle.prepare_attempt(effect)
        elif type(command) is CheckAttempt:
            observation = self._lifecycle.check_attempt(effect)
        else:
            observation = self._lifecycle.finish_attempt(effect)
        self._trigger("after_adapter_call", command.operation_id)
        if (
            type(observation) is not observation_type
            or observation.operation_id != command.operation_id
            or not self._observation_matches(command, observation)
        ):
            return _LifecycleApplication(
                appended.head,
                tuple(events),
                None,
                None,
                TrellisLifecycleEffectReason.OBSERVATION_INVALID,
            )
        evidence = self._evidence_store.put(
            observation,
            identity=identity,
            operation=operation,
        )
        if type(evidence) is not EvidenceRef:
            return _LifecycleApplication(
                appended.head,
                tuple(events),
                None,
                observation,
                TrellisLifecycleEffectReason.EVIDENCE_NOT_DURABLE,
            )
        self._trigger("after_evidence_store", command.operation_id)
        receipt = self._receipt(identity, operation, observation, evidence)
        observed = self._append(
            JournalEventType.EFFECT_OBSERVED,
            identity,
            EffectObservationPayload(AdapterKind.TRELLIS, receipt),
            appended.head,
            ActorType.ADAPTER,
            "trellis-lifecycle-adapter",
        )
        if not observed.durable or observed.event is None or observed.head is None:
            return _LifecycleApplication(
                appended.head,
                tuple(events),
                None,
                observation,
                self._append_reason(observed),
            )
        events.append(observed.event)
        self._trigger("after_observation_append", command.operation_id)
        return _LifecycleApplication(
            observed.head,
            tuple(events),
            receipt,
            observation,
            TrellisLifecycleEffectReason.NONE,
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

    @staticmethod
    def _observation_matches(
        command: LifecycleCommand,
        observation: LifecycleObservation,
    ) -> bool:
        if type(command) is PrepareAttempt:
            assert type(observation) is AttemptObservation
            return (
                observation.trellis_task_id in {None, command.trellis_task_id}
                and observation.base_commit in {None, command.expected_base_commit}
            )
        if type(command) is CheckAttempt:
            assert type(observation) is CheckObservation
            return (
                observation.attempt_id in {None, command.attempt_id}
                and observation.head_commit in {None, command.expected_head_commit}
            )
        assert type(command) is FinishAttempt
        assert type(observation) is FinishObservation
        return (
            observation.attempt_id in {None, command.attempt_id}
            and observation.delivered_commit in {None, command.delivered_commit}
        )

    @staticmethod
    def _receipt(
        identity: ExecutionIdentity,
        operation: EffectOperation,
        observation: LifecycleObservation,
        evidence: EvidenceRef,
    ) -> EffectReceipt:
        external_id = None
        if observation.status is EffectStatus.APPLIED:
            external_id = observation.attempt_id
            assert external_id is not None
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
    def _append_reason(result: AppendResult) -> TrellisLifecycleEffectReason:
        return (
            TrellisLifecycleEffectReason.JOURNAL_CONFLICT
            if result.status is AppendStatus.CONFLICT
            else TrellisLifecycleEffectReason.PERSISTENCE_FAILED
        )

    @staticmethod
    def _result_status(
        receipt: EffectReceipt | None,
    ) -> TrellisLifecycleEffectStatus:
        return (
            TrellisLifecycleEffectStatus.APPLIED
            if receipt is not None and receipt.status is EffectStatus.APPLIED
            else TrellisLifecycleEffectStatus.BLOCKED
        )

    @staticmethod
    def _result_reason(
        applied: _LifecycleApplication,
    ) -> TrellisLifecycleEffectReason:
        if applied.receipt is None:
            return applied.reason
        if applied.receipt.status is EffectStatus.APPLIED:
            return TrellisLifecycleEffectReason.NONE
        return (
            TrellisLifecycleEffectReason.EFFECT_OUTCOME_UNKNOWN
            if applied.receipt.status is EffectStatus.UNKNOWN
            else TrellisLifecycleEffectReason.EFFECT_ABSENT
        )

    def _trigger(self, point: str, operation_id: str) -> None:
        if self._failpoint is not None:
            self._failpoint(point, operation_id)


__all__ = [
    "CheckAttemptEffectResult",
    "FinishAttemptEffectResult",
    "PrepareAttemptEffectResult",
    "TrellisLifecycleEffectCrash",
    "TrellisLifecycleEffectFailpoint",
    "TrellisLifecycleEffectReason",
    "TrellisLifecycleEffectService",
    "TrellisLifecycleEffectStatus",
    "TrellisLifecycleObservationStorePort",
    "lifecycle_target_object_hash",
]
