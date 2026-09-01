"""Fail-closed reconciliation for interrupted backend and Trellis effects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import threading
from typing import TypeVar, cast

from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectStatus,
    EvidenceRef,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
)
from wish_builder.services.dispatch_recovery import PendingExternalEffect
from wish_builder.services.journal import (
    AppendResult,
    AppendStatus,
    DurableJournal,
    JournalEventDraft,
    JournalHead,
)
from wish_builder.services.ports import (
    AttemptObservation,
    CancelTurn,
    ChannelObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PreparedEffect,
    PrepareAttempt,
    ReserveChannel,
    SendTaskPacket,
    BackendChannelPort,
    TrellisLifecyclePort,
    TurnObservation,
)
from wish_builder.services.backend_effects import BackendObservationStorePort


class ExternalEffectRecoveryStatus(StrEnum):
    RECONCILED = "reconciled"
    RETRIED = "retried"
    BLOCKED = "blocked"


class ExternalEffectRecoveryReason(StrEnum):
    NONE = "none"
    ADAPTER_MISMATCH = "adapter_mismatch"
    OBSERVATION_UNKNOWN = "observation_unknown"
    OBSERVATION_INVALID = "observation_invalid"
    STALE_EPOCH = "stale_epoch"
    RETRY_NOT_ADMITTED = "retry_not_admitted"
    RETRY_COMMAND_REQUIRED = "retry_command_required"
    RETRY_COMMAND_MISMATCH = "retry_command_mismatch"
    RETRY_EFFECT_ABSENT = "retry_effect_absent"
    EVIDENCE_NOT_DURABLE = "evidence_not_durable"
    JOURNAL_CONFLICT = "journal_conflict"
    PERSISTENCE_FAILED = "persistence_failed"


BackendRecoveryCommand = ReserveChannel | SendTaskPacket | CancelTurn
TrellisLifecycleRecoveryCommand = PrepareAttempt | CheckAttempt | FinishAttempt
ExternalEffectRetryCommand = BackendRecoveryCommand | TrellisLifecycleRecoveryCommand
ExternalEffectObservation = (
    AttemptObservation
    | ChannelObservation
    | TurnObservation
    | CheckObservation
    | FinishObservation
)
_CommandT = TypeVar(
    "_CommandT",
    ReserveChannel,
    SendTaskPacket,
    CancelTurn,
    PrepareAttempt,
    CheckAttempt,
    FinishAttempt,
)
_ObservationT = TypeVar(
    "_ObservationT",
    ChannelObservation,
    TurnObservation,
    AttemptObservation,
    CheckObservation,
    FinishObservation,
)


class _RecoveryExclusionEntry:
    __slots__ = ("lock", "terminal_result", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.terminal_result: ExternalEffectRecoveryResult | None = None
        self.users = 0


_RECOVERY_EXCLUSION_GUARD = threading.Lock()
# Retain terminal attempts for the coordinator process lifetime. A second component may
# have loaded stale provider state before it reaches this exclusion.
_RECOVERY_EXCLUSIONS: dict[tuple[str, str, str], _RecoveryExclusionEntry] = {}


def _acquire_recovery_exclusion(
    journal_scope: str,
    run_id: str,
    operation_id: str,
) -> _RecoveryExclusionEntry:
    key = (journal_scope, run_id, operation_id)
    with _RECOVERY_EXCLUSION_GUARD:
        entry = _RECOVERY_EXCLUSIONS.get(key)
        if entry is None:
            entry = _RecoveryExclusionEntry()
            _RECOVERY_EXCLUSIONS[key] = entry
        entry.users += 1
    try:
        entry.lock.acquire()
    except BaseException:
        with _RECOVERY_EXCLUSION_GUARD:
            entry.users -= 1
            if entry.users == 0 and entry.terminal_result is None:
                _RECOVERY_EXCLUSIONS.pop(key, None)
        raise
    return entry


def _release_recovery_exclusion(
    journal_scope: str,
    run_id: str,
    operation_id: str,
    entry: _RecoveryExclusionEntry,
) -> None:
    key = (journal_scope, run_id, operation_id)
    entry.lock.release()
    with _RECOVERY_EXCLUSION_GUARD:
        entry.users -= 1
        if entry.users == 0 and entry.terminal_result is None:
            _RECOVERY_EXCLUSIONS.pop(key, None)


@dataclass(frozen=True, slots=True)
class ExternalEffectRecoveryResult:
    status: ExternalEffectRecoveryStatus
    reason: ExternalEffectRecoveryReason
    head: JournalHead
    observation: ExternalEffectObservation | None = None
    receipt: EffectReceipt | None = None
    event: JournalEvent | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not ExternalEffectRecoveryStatus:
            raise TypeError("status must be an ExternalEffectRecoveryStatus")
        if type(self.reason) is not ExternalEffectRecoveryReason:
            raise TypeError("reason must be an ExternalEffectRecoveryReason")
        if type(self.head) is not JournalHead:
            raise TypeError("head must be a JournalHead")
        if self.observation is not None and type(self.observation) not in {
            AttemptObservation,
            ChannelObservation,
            TurnObservation,
            CheckObservation,
            FinishObservation,
        }:
            raise TypeError("observation must be an external observation or null")
        if self.receipt is not None and type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt or null")
        if self.event is not None and type(self.event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent or null")
        if self.status in {
            ExternalEffectRecoveryStatus.RECONCILED,
            ExternalEffectRecoveryStatus.RETRIED,
        } and (
            self.reason is not ExternalEffectRecoveryReason.NONE
            or self.observation is None
            or self.observation.status is not EffectStatus.APPLIED
            or self.receipt is None
            or self.receipt.status is not EffectStatus.APPLIED
            or self.event is None
            or self.event.event_type is not JournalEventType.EFFECT_RECONCILED
            or self.event.sequence != self.head.sequence
            or self.event.event_hash != self.head.event_hash
        ):
            raise ValueError("successful external recovery requires a reconciled APPLIED event")
        if self.status is ExternalEffectRecoveryStatus.BLOCKED and self.reason is ExternalEffectRecoveryReason.NONE:
            raise ValueError("blocked external recovery requires a reason")


class _ChildEffectRecoveryCore:
    """Adapter-neutral journal, evidence, fencing, and retry rules."""

    def __init__(
        self,
        journal: DurableJournal,
        evidence_store: BackendObservationStorePort,
        *,
        coordinator_id: str,
        fencing_token: int,
        retry_admitted: Callable[[], bool],
    ) -> None:
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        if not isinstance(evidence_store, BackendObservationStorePort):
            raise TypeError("evidence_store must implement BackendObservationStorePort")
        if type(coordinator_id) is not str or not coordinator_id:
            raise ValueError("coordinator_id must be non-empty")
        if type(fencing_token) is not int or fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if not callable(retry_admitted):
            raise TypeError("retry_admitted must be callable")
        self._journal = journal
        self._evidence_store = evidence_store
        self._coordinator_id = coordinator_id
        self._fencing_token = fencing_token
        self._retry_admitted = retry_admitted

    def reconcile_typed(
        self,
        pending: PendingExternalEffect,
        *,
        expected_head: JournalHead,
        retry_command: object,
        command_type: type[_CommandT],
        observation_type: type[_ObservationT],
        inspect: Callable[[str], _ObservationT],
        retry: Callable[[PreparedEffect[_CommandT]], _ObservationT],
        allow_stale_retry: bool = False,
    ) -> ExternalEffectRecoveryResult:
        if type(allow_stale_retry) is not bool:
            raise TypeError("allow_stale_retry must be a bool")
        request = pending.request_event
        run_id = request.identity.run_id
        operation_id = pending.operation_id
        journal_scope = self._journal.recovery_scope
        entry = _acquire_recovery_exclusion(
            journal_scope,
            run_id,
            operation_id,
        )
        try:
            if entry.terminal_result is not None:
                return entry.terminal_result

            retry_attempted = False

            def marked_retry(
                prepared: PreparedEffect[_CommandT],
            ) -> _ObservationT:
                nonlocal retry_attempted
                retry_attempted = True
                return retry(prepared)

            result = self._reconcile_typed_once(
                pending,
                expected_head=expected_head,
                retry_command=retry_command,
                command_type=command_type,
                observation_type=observation_type,
                inspect=inspect,
                retry=marked_retry,
                allow_stale_retry=allow_stale_retry,
            )
            if (
                result.observation is not None
                and result.observation.status is EffectStatus.APPLIED
            ) or (
                retry_attempted
                and result.reason
                is not ExternalEffectRecoveryReason.RETRY_EFFECT_ABSENT
            ):
                entry.terminal_result = result
            return result
        finally:
            _release_recovery_exclusion(
                journal_scope,
                run_id,
                operation_id,
                entry,
            )

    def _reconcile_typed_once(
        self,
        pending: PendingExternalEffect,
        *,
        expected_head: JournalHead,
        retry_command: object,
        command_type: type[_CommandT],
        observation_type: type[_ObservationT],
        inspect: Callable[[str], _ObservationT],
        retry: Callable[[PreparedEffect[_CommandT]], _ObservationT],
        allow_stale_retry: bool,
    ) -> ExternalEffectRecoveryResult:
        request = pending.request_event
        operation_id = pending.operation_id
        try:
            observation = inspect(operation_id)
        except Exception:
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN,
            )
        if not self._valid_observation(
            operation_id,
            observation,
            observation_type,
        ):
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.OBSERVATION_INVALID,
            )
        if observation.status is EffectStatus.UNKNOWN:
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN,
                observation,
            )
        if observation.status is EffectStatus.APPLIED:
            return self._persist_applied(
                pending,
                observation,
                expected_head,
                ExternalEffectRecoveryStatus.RECONCILED,
            )

        if request.identity.coordinator_epoch != self._fencing_token and not (
            allow_stale_retry
            and request.identity.coordinator_epoch < self._fencing_token
        ):
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.STALE_EPOCH,
                observation,
            )
        if not self._retry_is_admitted():
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.RETRY_NOT_ADMITTED,
                observation,
            )
        if retry_command is None:
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.RETRY_COMMAND_REQUIRED,
                observation,
            )
        if type(retry_command) is not command_type:
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.RETRY_COMMAND_MISMATCH,
                observation,
            )
        typed_retry_command = cast(_CommandT, retry_command)
        try:
            prepared = PreparedEffect.from_append_result(
                AppendResult(
                    AppendStatus.IDEMPOTENT,
                    JournalHead(request.sequence, request.event_hash),
                    request,
                ),
                typed_retry_command,
            )
        except (TypeError, ValueError):
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.RETRY_COMMAND_MISMATCH,
                observation,
            )
        if not self._retry_is_admitted():
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.RETRY_NOT_ADMITTED,
                observation,
            )
        try:
            retried = retry(prepared)
        except Exception:
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN,
                observation,
            )
        if not self._valid_observation(
            operation_id,
            retried,
            observation_type,
        ):
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.OBSERVATION_INVALID,
            )
        if retried.status is not EffectStatus.APPLIED:
            return self._blocked(
                expected_head,
                (
                    ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN
                    if retried.status is EffectStatus.UNKNOWN
                    else ExternalEffectRecoveryReason.RETRY_EFFECT_ABSENT
                ),
                retried,
            )
        return self._persist_applied(
            pending,
            retried,
            expected_head,
            ExternalEffectRecoveryStatus.RETRIED,
        )

    def _persist_applied(
        self,
        pending: PendingExternalEffect,
        observation: ExternalEffectObservation,
        expected_head: JournalHead,
        status: ExternalEffectRecoveryStatus,
    ) -> ExternalEffectRecoveryResult:
        try:
            evidence = self._evidence_store.put(
                observation,
                identity=pending.request_event.identity,
                operation=pending.operation,
            )
        except Exception:
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.EVIDENCE_NOT_DURABLE,
                observation,
            )
        if type(evidence) is not EvidenceRef:
            return self._blocked(
                expected_head,
                ExternalEffectRecoveryReason.EVIDENCE_NOT_DURABLE,
                observation,
            )
        receipt = EffectReceipt(
            1,
            pending.request_event.identity,
            pending.operation,
            EffectStatus.APPLIED,
            observation.observed_at,
            effect_hash=observation.effect_digest,
            external_object_id=(
                observation.channel_id
                if type(observation) is ChannelObservation
                else observation.turn_id
                if type(observation) is TurnObservation
                else observation.attempt_id
            ),
            evidence=(evidence,),
        )
        result = self._journal.append_draft(
            JournalEventDraft(
                event_id=(
                    "EVENT-EFFECT-RECONCILED-"
                    f"{expected_head.sequence + 1:08d}"
                ),
                event_type=JournalEventType.EFFECT_RECONCILED,
                identity=self._observation_identity(pending.request_event.identity),
                actor_type=ActorType.COORDINATOR,
                actor_id=self._coordinator_id,
                payload=EffectObservationPayload(pending.adapter, receipt),
            ),
            expected_head=expected_head,
        )
        if not result.durable or result.event is None or result.head is None:
            reason = (
                ExternalEffectRecoveryReason.JOURNAL_CONFLICT
                if result.status is AppendStatus.CONFLICT
                else ExternalEffectRecoveryReason.PERSISTENCE_FAILED
            )
            return self._blocked(expected_head, reason, observation)
        return ExternalEffectRecoveryResult(
            status,
            ExternalEffectRecoveryReason.NONE,
            result.head,
            observation,
            receipt,
            result.event,
        )

    def _observation_identity(
        self,
        request_identity: ExecutionIdentity,
    ) -> ExecutionIdentity:
        return ExecutionIdentity(
            request_identity.run_id,
            self._fencing_token,
            request_identity.task_id,
            request_identity.attempt,
            request_identity.correlation_id,
        )

    @staticmethod
    def _valid_observation(
        operation_id: str,
        observation: object,
        observation_type: type[_ObservationT],
    ) -> bool:
        return (
            type(observation) is observation_type
            and observation.operation_id == operation_id
        )

    def _retry_is_admitted(self) -> bool:
        try:
            return self._retry_admitted() is True
        except Exception:
            return False

    @staticmethod
    def _blocked(
        head: JournalHead,
        reason: ExternalEffectRecoveryReason,
        observation: ExternalEffectObservation | None = None,
    ) -> ExternalEffectRecoveryResult:
        return ExternalEffectRecoveryResult(
            ExternalEffectRecoveryStatus.BLOCKED,
            reason,
            head,
            observation,
        )


class BackendEffectRecoveryService:
    """Recover only backend reserve, send, and cancellation operations."""

    def __init__(
        self,
        journal: DurableJournal,
        backend_channel: BackendChannelPort,
        evidence_store: BackendObservationStorePort,
        *,
        coordinator_id: str,
        fencing_token: int,
        retry_admitted: Callable[[], bool],
        stale_cancel_retry_operation_ids: frozenset[str] = frozenset(),
    ) -> None:
        if not isinstance(backend_channel, BackendChannelPort):
            raise TypeError("backend_channel must implement BackendChannelPort")
        if type(stale_cancel_retry_operation_ids) is not frozenset or not all(
            type(operation_id) is str and operation_id
            for operation_id in stale_cancel_retry_operation_ids
        ):
            raise TypeError(
                "stale_cancel_retry_operation_ids must contain non-empty strings"
            )
        self._backend_channel = backend_channel
        self._stale_cancel_retry_operation_ids = stale_cancel_retry_operation_ids
        self._core = _ChildEffectRecoveryCore(
            journal,
            evidence_store,
            coordinator_id=coordinator_id,
            fencing_token=fencing_token,
            retry_admitted=retry_admitted,
        )

    def reconcile(
        self,
        pending: PendingExternalEffect,
        *,
        expected_head: JournalHead,
        retry_command: BackendRecoveryCommand | None = None,
    ) -> ExternalEffectRecoveryResult:
        _require_recovery_inputs(pending, expected_head)
        if pending.adapter is not AdapterKind.BACKEND:
            return self._core._blocked(
                expected_head,
                ExternalEffectRecoveryReason.ADAPTER_MISMATCH,
            )
        if pending.operation is EffectOperation.RESERVE_CHANNEL:
            return self._core.reconcile_typed(
                pending,
                expected_head=expected_head,
                retry_command=retry_command,
                command_type=ReserveChannel,
                observation_type=ChannelObservation,
                inspect=self._backend_channel.inspect_reservation,
                retry=self._backend_channel.reserve,
            )
        if pending.operation is EffectOperation.SEND_TASK_PACKET:
            return self._core.reconcile_typed(
                pending,
                expected_head=expected_head,
                retry_command=retry_command,
                command_type=SendTaskPacket,
                observation_type=TurnObservation,
                inspect=self._backend_channel.inspect_turn,
                retry=self._backend_channel.send,
            )
        if pending.operation is EffectOperation.CANCEL_TURN:
            return self._core.reconcile_typed(
                pending,
                expected_head=expected_head,
                retry_command=retry_command,
                command_type=CancelTurn,
                observation_type=TurnObservation,
                inspect=self._backend_channel.inspect_turn,
                retry=self._backend_channel.cancel,
                allow_stale_retry=(
                    pending.operation_id in self._stale_cancel_retry_operation_ids
                ),
            )
        return self._core._blocked(
            expected_head,
            ExternalEffectRecoveryReason.ADAPTER_MISMATCH,
        )


class TrellisLifecycleEffectRecoveryService:
    """Recover only Trellis prepare, check, and finish lifecycle operations."""

    def __init__(
        self,
        journal: DurableJournal,
        trellis_lifecycle: TrellisLifecyclePort,
        evidence_store: BackendObservationStorePort,
        *,
        coordinator_id: str,
        fencing_token: int,
        retry_admitted: Callable[[], bool],
    ) -> None:
        if not isinstance(trellis_lifecycle, TrellisLifecyclePort):
            raise TypeError("trellis_lifecycle must implement TrellisLifecyclePort")
        self._trellis_lifecycle = trellis_lifecycle
        self._core = _ChildEffectRecoveryCore(
            journal,
            evidence_store,
            coordinator_id=coordinator_id,
            fencing_token=fencing_token,
            retry_admitted=retry_admitted,
        )

    def reconcile(
        self,
        pending: PendingExternalEffect,
        *,
        expected_head: JournalHead,
        retry_command: TrellisLifecycleRecoveryCommand | None = None,
    ) -> ExternalEffectRecoveryResult:
        _require_recovery_inputs(pending, expected_head)
        if pending.adapter is not AdapterKind.TRELLIS:
            return self._core._blocked(
                expected_head,
                ExternalEffectRecoveryReason.ADAPTER_MISMATCH,
            )
        if pending.operation is EffectOperation.PREPARE_ATTEMPT:
            return self._core.reconcile_typed(
                pending,
                expected_head=expected_head,
                retry_command=retry_command,
                command_type=PrepareAttempt,
                observation_type=AttemptObservation,
                inspect=self._trellis_lifecycle.inspect_attempt,
                retry=self._trellis_lifecycle.prepare_attempt,
            )
        if pending.operation is EffectOperation.CHECK_ATTEMPT:
            return self._core.reconcile_typed(
                pending,
                expected_head=expected_head,
                retry_command=retry_command,
                command_type=CheckAttempt,
                observation_type=CheckObservation,
                inspect=self._trellis_lifecycle.inspect_check,
                retry=self._trellis_lifecycle.check_attempt,
            )
        if pending.operation is EffectOperation.FINISH_ATTEMPT:
            return self._core.reconcile_typed(
                pending,
                expected_head=expected_head,
                retry_command=retry_command,
                command_type=FinishAttempt,
                observation_type=FinishObservation,
                inspect=self._trellis_lifecycle.inspect_finish,
                retry=self._trellis_lifecycle.finish_attempt,
            )
        return self._core._blocked(
            expected_head,
            ExternalEffectRecoveryReason.ADAPTER_MISMATCH,
        )


def _require_recovery_inputs(
    pending: object,
    expected_head: object,
) -> None:
    if type(pending) is not PendingExternalEffect:
        raise TypeError("pending must be a PendingExternalEffect")
    if type(expected_head) is not JournalHead:
        raise TypeError("expected_head must be a JournalHead")


__all__ = [
    "BackendEffectRecoveryService",
    "ExternalEffectObservation",
    "ExternalEffectRetryCommand",
    "BackendRecoveryCommand",
    "TrellisLifecycleEffectRecoveryService",
    "TrellisLifecycleRecoveryCommand",
    "ExternalEffectRecoveryReason",
    "ExternalEffectRecoveryResult",
    "ExternalEffectRecoveryStatus",
]
