"""Verified coordinator-lease recovery and compare-and-append mutations.

The Journal remains the only authority.  Recovery first delegates the complete
state replay to :mod:`wish_builder.services.replay`, then performs a bounded-
memory pass from genesis to project ``CoordinatorLeaseState``.  The second pass
rechecks the canonical chain and must end at the exact head verified by replay.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from wish_builder.adapters.process_identity import (
    LeaseOwnerProcessProbeResult,
    LeaseOwnerProcessState,
    probe_lease_owner_process,
)
from wish_builder.contracts import (
    DEFAULT_DECODE_LIMITS,
    ActorType,
    EffectObservationPayload,
    EffectOperation,
    EffectRequestPayload,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseDraftPayload,
    LeaseOwner,
    LeasePayload,
    RuntimeReasonCode,
    SchedulerMode,
)
from wish_builder.contracts.execution import ExecutionManifestModel

from . import replay as replay_module
from .checkpoints import CheckpointPolicy, CheckpointStore
from .dispatch_recovery import (
    DispatchRecoveryProjectionError,
    DispatchRecoveryRecord,
    PendingExternalEffect,
    ExternalEffectProjection,
    advance_dispatch_recoveries,
    advance_external_effect_projection,
)
from .journal import (
    AppendResult,
    AppendStatus,
    CoordinatorLeaseState,
    JournalEventDraft,
    JournalHead,
    LeaseStateCode,
    LeaseStateError,
)
from .replay import ReplayFault, ReplayResult, ReplayStatus, replay_journal

_DEFAULT_CHECKPOINT_POLICY = CheckpointPolicy()


class LeaseRecoveryStatus(StrEnum):
    RECOVERED = "recovered"
    RETRY_REQUIRED = "retry_required"
    BLOCKED = "blocked"


class LeaseRecoveryFaultCode(StrEnum):
    REPLAY_BLOCKED = "replay_blocked"
    CONTROL_ROOT_DRIFT = "control_root_drift"
    JOURNAL_CHANGED = "journal_changed"
    LEASE_STATE_INVALID = "lease_state_invalid"
    RECOVERY_PREFIX_INVALID = "recovery_prefix_invalid"


@dataclass(frozen=True, slots=True)
class LeaseRecoveryFault:
    code: LeaseRecoveryFaultCode
    detail: str
    replay_fault: ReplayFault | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not LeaseRecoveryFaultCode:
            raise TypeError("code must be a LeaseRecoveryFaultCode")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("detail must be a non-empty string")
        if self.replay_fault is not None and type(self.replay_fault) is not ReplayFault:
            raise TypeError("replay_fault must be a ReplayFault or null")


@dataclass(frozen=True, slots=True)
class LeaseRecoveryResult:
    status: LeaseRecoveryStatus
    replay: ReplayResult
    lease_state: CoordinatorLeaseState | None = None
    last_lease_event: JournalEvent | None = None
    pending_dispatch_requests: tuple[JournalEvent, ...] = ()
    dispatch_recoveries: tuple[DispatchRecoveryRecord, ...] = ()
    pending_external_effects: tuple[PendingExternalEffect, ...] = ()
    fault: LeaseRecoveryFault | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not LeaseRecoveryStatus:
            raise TypeError("status must be a LeaseRecoveryStatus")
        if type(self.replay) is not ReplayResult:
            raise TypeError("replay must be a ReplayResult")
        if self.status is LeaseRecoveryStatus.RECOVERED:
            if self.replay.status is not ReplayStatus.RECOVERED:
                raise ValueError("recovered lease state requires a recovered replay")
            if type(self.lease_state) is not CoordinatorLeaseState:
                raise ValueError("recovered lease state is required")
            if self.fault is not None:
                raise ValueError("recovered lease state cannot contain a fault")
            if self.lease_state.head != self.replay.head:
                raise ValueError("lease state must end at the verified replay head")
        elif self.lease_state is not None or type(self.fault) is not LeaseRecoveryFault:
            raise ValueError("non-recovered results require exactly one fault")
        if self.last_lease_event is None:
            if self.lease_state is not None and (
                self.lease_state.event_type is not None
                or self.lease_state.lease is not None
            ):
                raise ValueError("lease state requires its last lease event")
        else:
            if type(self.last_lease_event) is not JournalEvent or not isinstance(
                self.last_lease_event.payload, LeasePayload
            ):
                raise TypeError("last_lease_event must contain a LeasePayload")
            if self.status is not LeaseRecoveryStatus.RECOVERED:
                raise ValueError("a failed recovery cannot expose a lease event")
            assert self.lease_state is not None
            if (
                self.last_lease_event.identity.run_id != self.replay.snapshot.run_id
                or self.lease_state.event_type is not self.last_lease_event.event_type
                or self.lease_state.lease != self.last_lease_event.payload
            ):
                raise ValueError(
                    "last lease event does not match recovered lease state"
                )
        if type(self.pending_dispatch_requests) is not tuple or not all(
            type(event) is JournalEvent
            and event.event_type is JournalEventType.DISPATCH_REQUESTED
            and isinstance(event.payload, EffectRequestPayload)
            and event.payload.operation is EffectOperation.WORKER_DISPATCH
            for event in self.pending_dispatch_requests
        ):
            raise TypeError(
                "pending_dispatch_requests must contain dispatch request events"
            )
        if self.status is not LeaseRecoveryStatus.RECOVERED and (
            self.pending_dispatch_requests
        ):
            raise ValueError("failed recovery cannot expose pending dispatch requests")
        if tuple(event.sequence for event in self.pending_dispatch_requests) != tuple(
            sorted(event.sequence for event in self.pending_dispatch_requests)
        ):
            raise ValueError("pending dispatch requests must follow Journal order")
        if type(self.dispatch_recoveries) is not tuple or not all(
            type(record) is DispatchRecoveryRecord
            for record in self.dispatch_recoveries
        ):
            raise TypeError(
                "dispatch_recoveries must contain DispatchRecoveryRecord values"
            )
        if self.status is not LeaseRecoveryStatus.RECOVERED and (
            self.dispatch_recoveries
        ):
            raise ValueError("failed recovery cannot expose dispatch recoveries")
        if type(self.pending_external_effects) is not tuple or not all(
            type(item) is PendingExternalEffect
            for item in self.pending_external_effects
        ):
            raise TypeError(
                "pending_external_effects must contain PendingExternalEffect values"
            )
        if self.status is not LeaseRecoveryStatus.RECOVERED and (
            self.pending_external_effects
        ):
            raise ValueError("failed recovery cannot expose pending external effects")


@dataclass(frozen=True, slots=True)
class _LeaseProjection:
    state: CoordinatorLeaseState
    last_lease_event: JournalEvent | None
    pending_dispatch_requests: tuple[JournalEvent, ...]
    dispatch_recoveries: tuple[DispatchRecoveryRecord, ...]
    pending_external_effects: tuple[PendingExternalEffect, ...]


class _ProjectionFault(RuntimeError):
    def __init__(
        self,
        status: LeaseRecoveryStatus,
        code: LeaseRecoveryFaultCode,
        detail: str,
    ) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(f"{code.value}:{detail}")


def recover_coordinator_lease(
    journal_root: str | os.PathLike[str],
    manifest: ExecutionManifestModel,
    *,
    coordinator_epoch: int,
    checkpoint_store: CheckpointStore | None = None,
    checkpoint_policy: CheckpointPolicy = _DEFAULT_CHECKPOINT_POLICY,
    elapsed_since_checkpoint_seconds: float | None = None,
    repair_derived: bool = True,
    control_root_validator: Callable[[], bool] | None = None,
    expected_control_root: object | None = None,
) -> LeaseRecoveryResult:
    """Recover a lease projection only after the canonical replay succeeds."""

    replay = replay_journal(
        journal_root,
        manifest,
        coordinator_epoch=coordinator_epoch,
        checkpoint_store=checkpoint_store,
        checkpoint_policy=checkpoint_policy,
        elapsed_since_checkpoint_seconds=elapsed_since_checkpoint_seconds,
        repair_derived=repair_derived,
        control_root_validator=control_root_validator,
        expected_control_root=expected_control_root,
    )
    if replay.status is ReplayStatus.BLOCKED:
        detail = "verified journal replay blocked"
        if replay.fault is not None:
            detail = f"{replay.fault.code.value}:{replay.fault.detail}"
        return LeaseRecoveryResult(
            LeaseRecoveryStatus.BLOCKED,
            replay,
            fault=LeaseRecoveryFault(
                LeaseRecoveryFaultCode.REPLAY_BLOCKED,
                detail,
                replay.fault,
            ),
        )

    validator = control_root_validator
    if validator is None and expected_control_root is not None:
        validator = replay_module._filesystem_identity_validator(expected_control_root)
    try:
        projection = _stream_lease_projection(
            Path(journal_root),
            manifest.run_id,
            replay.head,
            validator,
        )
    except _ProjectionFault as fault:
        return LeaseRecoveryResult(
            fault.status,
            replay,
            fault=LeaseRecoveryFault(fault.code, fault.detail),
        )
    return LeaseRecoveryResult(
        LeaseRecoveryStatus.RECOVERED,
        replay,
        projection.state,
        projection.last_lease_event,
        projection.pending_dispatch_requests,
        projection.dispatch_recoveries,
        projection.pending_external_effects,
    )


def _stream_lease_projection(
    root: Path,
    run_id: str,
    verified_head: JournalHead,
    control_root_validator: Callable[[], bool] | None,
) -> _LeaseProjection:
    if not replay_module._control_root_valid(control_root_validator):
        raise _ProjectionFault(
            LeaseRecoveryStatus.BLOCKED,
            LeaseRecoveryFaultCode.CONTROL_ROOT_DRIFT,
            "control_root_drift",
        )
    try:
        segments = replay_module._segment_paths(root)
    except (OSError, ValueError) as exc:
        raise _changed(f"segment layout changed after replay: {_detail(exc)}") from exc

    state = CoordinatorLeaseState.initial()
    last_lease_event: JournalEvent | None = None
    pending_dispatches: dict[ExecutionIdentity, JournalEvent] = {}
    dispatch_recoveries: tuple[DispatchRecoveryRecord, ...] = ()
    backend_effects = ExternalEffectProjection()
    for _number, path in segments:
        if not replay_module._control_root_valid(control_root_validator):
            raise _ProjectionFault(
                LeaseRecoveryStatus.BLOCKED,
                LeaseRecoveryFaultCode.CONTROL_ROOT_DRIFT,
                "control_root_drift",
            )
        try:
            before = os.lstat(path)
            if (
                not stat.S_ISREG(before.st_mode)
                or replay_module._is_link_or_junction(path)
                or before.st_nlink != 1
            ):
                raise ValueError("segment is not a protected regular file")
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("segment identity changed before lease replay")
                opened_size = opened.st_size
                while frame := handle.readline(DEFAULT_DECODE_LIMITS.max_bytes + 1):
                    if len(frame) > DEFAULT_DECODE_LIMITS.max_bytes:
                        raise ValueError("journal frame exceeds the decoder limit")
                    if not frame.endswith(b"\n"):
                        raise ValueError("journal frame changed during lease replay")
                    event, canonical = replay_module._decode_replay_event(frame)
                    if event is None or not canonical:
                        raise ValueError("journal event changed after verified replay")
                    if event.identity.run_id != run_id:
                        raise ValueError("journal run changed after verified replay")
                    try:
                        state = state.advance(event)
                    except LeaseStateError as exc:
                        raise _ProjectionFault(
                            LeaseRecoveryStatus.BLOCKED,
                            LeaseRecoveryFaultCode.LEASE_STATE_INVALID,
                            f"{exc.code.value} at sequence {event.sequence}",
                        ) from exc
                    try:
                        dispatch_recoveries = advance_dispatch_recoveries(
                            dispatch_recoveries,
                            event,
                        )
                    except DispatchRecoveryProjectionError as exc:
                        raise _ProjectionFault(
                            LeaseRecoveryStatus.BLOCKED,
                            LeaseRecoveryFaultCode.RECOVERY_PREFIX_INVALID,
                            f"{exc} at sequence {event.sequence}",
                        ) from exc
                    try:
                        backend_effects = advance_external_effect_projection(
                            backend_effects,
                            event,
                        )
                    except DispatchRecoveryProjectionError as exc:
                        raise _ProjectionFault(
                            LeaseRecoveryStatus.BLOCKED,
                            LeaseRecoveryFaultCode.RECOVERY_PREFIX_INVALID,
                            f"external effect projection invalid: {exc} "
                            f"at sequence {event.sequence}",
                        ) from exc
                    if isinstance(event.payload, LeasePayload):
                        last_lease_event = event
                    if (
                        event.event_type is JournalEventType.DISPATCH_REQUESTED
                        and isinstance(event.payload, EffectRequestPayload)
                        and event.payload.operation is EffectOperation.WORKER_DISPATCH
                    ):
                        pending_dispatches[event.identity] = event
                    elif (
                        event.event_type is JournalEventType.DISPATCH_OBSERVED
                        and isinstance(event.payload, EffectObservationPayload)
                        and event.payload.receipt.operation
                        is EffectOperation.WORKER_DISPATCH
                    ):
                        pending_dispatches.pop(event.payload.receipt.identity, None)
                observed = os.fstat(handle.fileno())
                if (observed.st_dev, observed.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ) or observed.st_size != opened_size:
                    raise ValueError("segment changed during lease replay")
        except _ProjectionFault:
            raise
        except (OSError, ValueError) as exc:
            raise _changed(_detail(exc)) from exc

    if not replay_module._control_root_valid(control_root_validator):
        raise _ProjectionFault(
            LeaseRecoveryStatus.BLOCKED,
            LeaseRecoveryFaultCode.CONTROL_ROOT_DRIFT,
            "control_root_drift",
        )
    if state.head != verified_head:
        raise _changed(
            "lease replay head differs from the previously verified journal head"
        )
    return _LeaseProjection(
        state,
        last_lease_event,
        tuple(sorted(pending_dispatches.values(), key=lambda event: event.sequence)),
        dispatch_recoveries,
        backend_effects.pending,
    )


def _changed(detail: str) -> _ProjectionFault:
    return _ProjectionFault(
        LeaseRecoveryStatus.RETRY_REQUIRED,
        LeaseRecoveryFaultCode.JOURNAL_CHANGED,
        detail,
    )


def _detail(exc: BaseException) -> str:
    text = str(exc).strip()
    return type(exc).__name__ if not text else f"{type(exc).__name__}:{text}"


class LeaseAction(StrEnum):
    ACQUIRE = "acquire"
    RENEW = "renew"
    RELEASE = "release"
    LOST = "lost"


class LeaseMutationStatus(StrEnum):
    COMMITTED = "committed"
    IDEMPOTENT = "idempotent"
    REJECTED = "rejected"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class LeaseMutationResult:
    action: LeaseAction
    status: LeaseMutationStatus
    recovery: LeaseRecoveryResult
    lease_state: CoordinatorLeaseState | None
    append_result: AppendResult | None = None
    lease_state_code: LeaseStateCode | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.action) is not LeaseAction:
            raise TypeError("action must be a LeaseAction")
        if type(self.status) is not LeaseMutationStatus:
            raise TypeError("status must be a LeaseMutationStatus")
        if type(self.recovery) is not LeaseRecoveryResult:
            raise TypeError("recovery must be a LeaseRecoveryResult")
        if (
            self.lease_state is not None
            and type(self.lease_state) is not CoordinatorLeaseState
        ):
            raise TypeError("lease_state must be a CoordinatorLeaseState or null")
        if (
            self.append_result is not None
            and type(self.append_result) is not AppendResult
        ):
            raise TypeError("append_result must be an AppendResult or null")
        if (
            self.lease_state_code is not None
            and type(self.lease_state_code) is not LeaseStateCode
        ):
            raise TypeError("lease_state_code must be a LeaseStateCode or null")
        if self.detail is not None and (
            type(self.detail) is not str or not self.detail
        ):
            raise ValueError("detail must be a non-empty string or null")
        if self.status in {
            LeaseMutationStatus.COMMITTED,
            LeaseMutationStatus.IDEMPOTENT,
        }:
            if (
                self.append_result is None
                or not self.append_result.durable
                or self.lease_state is None
                or self.lease_state_code is not None
            ):
                raise ValueError("durable lease mutations require durable evidence")
        elif self.status is LeaseMutationStatus.REJECTED:
            if self.lease_state_code is None or self.append_result is not None:
                raise ValueError("rejected lease mutations require a lease state code")
        elif self.detail is None and (
            self.append_result is None
            or self.append_result.status is not AppendStatus.PERSISTENCE_FAILED
        ):
            raise ValueError("blocked lease mutations require a detail or fault")

    @property
    def succeeded(self) -> bool:
        return self.status in {
            LeaseMutationStatus.COMMITTED,
            LeaseMutationStatus.IDEMPOTENT,
        }


@runtime_checkable
class LeaseJournal(Protocol):
    def append_draft(
        self,
        draft: JournalEventDraft,
        *,
        expected_head: JournalHead,
        lease_state: CoordinatorLeaseState | None = None,
    ) -> AppendResult: ...


class LeaseOwnerProcessProbe(Protocol):
    def __call__(
        self,
        owner: LeaseOwner,
        *,
        local_host_id: str,
    ) -> LeaseOwnerProcessProbeResult: ...


class CoordinatorLeaseService:
    """Mutate one coordinator lease using recovered state and Journal CAS only."""

    def __init__(
        self,
        journal: LeaseJournal,
        recover: Callable[[], LeaseRecoveryResult],
        *,
        run_id: str,
        owner: LeaseOwner,
        manifest_digest: str,
        lease_ttl_seconds: int,
        lease_clock_skew_seconds: int = 0,
        recovery_actor_id: str = "recovery",
        max_conflict_retries: int = 4,
        prior_owner_process_probe: LeaseOwnerProcessProbe | None = None,
    ) -> None:
        if not isinstance(journal, LeaseJournal):
            raise TypeError("journal must implement LeaseJournal")
        if not callable(recover):
            raise TypeError("recover must be callable")
        if type(run_id) is not str or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if type(owner) is not LeaseOwner:
            raise TypeError("owner must be a LeaseOwner")
        if type(recovery_actor_id) is not str or not recovery_actor_id:
            raise ValueError("recovery_actor_id must be a non-empty string")
        if type(max_conflict_retries) is not int or max_conflict_retries < 0:
            raise ValueError("max_conflict_retries must be non-negative")
        if prior_owner_process_probe is not None and not callable(
            prior_owner_process_probe
        ):
            raise TypeError("prior_owner_process_probe must be callable or null")
        # Validate all caller-controlled lease fields once at construction.
        LeaseDraftPayload(
            "LEASE-VALIDATION",
            owner.actor.actor_id,
            owner,
            SchedulerMode.WISH_BUILDER,
            1,
            manifest_digest,
            lease_ttl_seconds,
            lease_clock_skew_seconds,
        )
        self._journal = journal
        self._recover = recover
        self._run_id = run_id
        self._owner = owner
        self._coordinator_id = owner.actor.actor_id
        self._manifest_digest = manifest_digest
        self._lease_ttl_seconds = lease_ttl_seconds
        self._lease_clock_skew_seconds = lease_clock_skew_seconds
        self._recovery_actor_id = recovery_actor_id
        self._max_conflict_retries = max_conflict_retries
        self._prior_owner_process_probe = (
            prior_owner_process_probe or probe_lease_owner_process
        )

    def acquire(self, *, event_id: str, lease_id: str) -> LeaseMutationResult:
        action = LeaseAction.ACQUIRE
        for recovery in self._recovery_attempts():
            blocked = self._blocked_recovery(action, recovery)
            if blocked is not None:
                return blocked
            state = recovery.lease_state
            assert state is not None
            if (
                state.active
                and state.lease is not None
                and state.lease.lease_id == lease_id
                and self._holds(state.lease)
            ):
                retry_draft = self._draft_from_lease(
                    JournalEventType.LEASE_ACQUIRED,
                    event_id,
                    state.lease,
                )
                replayed = self._idempotent_retry(action, retry_draft, recovery)
                if replayed is not None:
                    return replayed
            takeover_blocked = self._takeover_blocked(action, recovery)
            if takeover_blocked is not None:
                return takeover_blocked
            token = state.max_fencing_token + 1
            draft = self._draft(
                JournalEventType.LEASE_ACQUIRED,
                event_id,
                LeaseDraftPayload(
                    lease_id,
                    self._coordinator_id,
                    self._owner,
                    SchedulerMode.WISH_BUILDER,
                    token,
                    self._manifest_digest,
                    self._lease_ttl_seconds,
                    self._lease_clock_skew_seconds,
                ),
            )
            result = self._append(action, draft, recovery)
            if result is not None:
                return result
        return self._retry_exhausted(action, recovery)

    def renew(self, *, event_id: str) -> LeaseMutationResult:
        return self._holder_transition(LeaseAction.RENEW, event_id)

    def release(self, *, event_id: str) -> LeaseMutationResult:
        return self._holder_transition(LeaseAction.RELEASE, event_id)

    def lost(
        self,
        *,
        event_id: str,
        expected_lease: LeasePayload,
    ) -> LeaseMutationResult:
        if type(expected_lease) is not LeasePayload:
            raise TypeError("expected_lease must be a LeasePayload")
        action = LeaseAction.LOST
        for recovery in self._recovery_attempts():
            blocked = self._blocked_recovery(action, recovery)
            if blocked is not None:
                return blocked
            state = recovery.lease_state
            assert state is not None
            draft = self._draft_from_lease(
                JournalEventType.LEASE_LOST,
                event_id,
                expected_lease,
            )
            replayed = self._idempotent_retry(action, draft, recovery)
            if replayed is not None:
                return replayed
            if not state.active or state.lease is None:
                return self._rejected(action, recovery, LeaseStateCode.NO_ACTIVE_LEASE)
            if state.lease != expected_lease:
                return self._rejected(
                    action,
                    recovery,
                    LeaseStateCode.LEASE_IDENTITY_MISMATCH,
                )
            result = self._append(action, draft, recovery)
            if result is not None:
                return result
        return self._retry_exhausted(action, recovery)

    def _holder_transition(
        self,
        action: LeaseAction,
        event_id: str,
    ) -> LeaseMutationResult:
        event_type = {
            LeaseAction.RENEW: JournalEventType.LEASE_RENEWED,
            LeaseAction.RELEASE: JournalEventType.LEASE_RELEASED,
        }[action]
        for recovery in self._recovery_attempts():
            blocked = self._blocked_recovery(action, recovery)
            if blocked is not None:
                return blocked
            state = recovery.lease_state
            assert state is not None
            if state.lease is None:
                return self._rejected(action, recovery, LeaseStateCode.NO_ACTIVE_LEASE)
            draft = self._draft_from_lease(event_type, event_id, state.lease)
            replayed = self._idempotent_retry(action, draft, recovery)
            if replayed is not None:
                return replayed
            if not state.active:
                return self._rejected(action, recovery, LeaseStateCode.NO_ACTIVE_LEASE)
            if not self._holds(state.lease):
                return self._rejected(
                    action,
                    recovery,
                    LeaseStateCode.LEASE_IDENTITY_MISMATCH,
                )
            result = self._append(action, draft, recovery)
            if result is not None:
                return result
        return self._retry_exhausted(action, recovery)

    def _recovery_attempts(self):
        for attempt in range(self._max_conflict_retries + 1):
            recovery = self._recover()
            if type(recovery) is not LeaseRecoveryResult:
                raise TypeError("recover returned an invalid result")
            if (
                recovery.status is LeaseRecoveryStatus.RETRY_REQUIRED
                and attempt < self._max_conflict_retries
            ):
                continue
            yield recovery

    def _append(
        self,
        action: LeaseAction,
        draft: JournalEventDraft,
        recovery: LeaseRecoveryResult,
    ) -> LeaseMutationResult | None:
        state = recovery.lease_state
        assert state is not None
        try:
            appended = self._journal.append_draft(
                draft,
                expected_head=state.head,
                lease_state=state,
            )
        except LeaseStateError as exc:
            return self._rejected(action, recovery, exc.code)
        if appended.status is AppendStatus.CONFLICT:
            return None
        if appended.status is AppendStatus.PERSISTENCE_FAILED:
            return LeaseMutationResult(
                action,
                LeaseMutationStatus.BLOCKED,
                recovery,
                state,
                append_result=appended,
                detail=(
                    "persistence_failed"
                    if appended.fault_code is None
                    else appended.fault_code.value
                ),
            )
        if not appended.durable or appended.event is None:
            raise TypeError("lease journal returned an invalid append result")
        advanced = state.advance(appended.event)
        status = (
            LeaseMutationStatus.COMMITTED
            if appended.status is AppendStatus.COMMITTED
            else LeaseMutationStatus.IDEMPOTENT
        )
        return LeaseMutationResult(
            action,
            status,
            recovery,
            advanced,
            append_result=appended,
        )

    def _idempotent_retry(
        self,
        action: LeaseAction,
        draft: JournalEventDraft,
        recovery: LeaseRecoveryResult,
    ) -> LeaseMutationResult | None:
        event = recovery.last_lease_event
        state = recovery.lease_state
        if event is None or state is None or not draft.matches_event(event):
            return None
        append_result = AppendResult(
            AppendStatus.IDEMPOTENT,
            JournalHead(event.sequence, event.event_hash),
            event,
        )
        return LeaseMutationResult(
            action,
            LeaseMutationStatus.IDEMPOTENT,
            recovery,
            state,
            append_result=append_result,
        )

    def _draft_from_lease(
        self,
        event_type: JournalEventType,
        event_id: str,
        lease: LeasePayload,
    ) -> JournalEventDraft:
        return self._draft(
            event_type,
            event_id,
            LeaseDraftPayload(
                lease.lease_id,
                lease.coordinator_id,
                lease.owner,
                lease.scheduler_mode,
                lease.fencing_token,
                lease.manifest_digest,
                lease.lease_ttl_seconds,
                lease.lease_clock_skew_seconds,
            ),
        )

    def _draft(
        self,
        event_type: JournalEventType,
        event_id: str,
        payload: LeaseDraftPayload,
    ) -> JournalEventDraft:
        lost = event_type is JournalEventType.LEASE_LOST
        return JournalEventDraft(
            event_id,
            event_type,
            ExecutionIdentity(self._run_id, payload.fencing_token),
            ActorType.SYSTEM if lost else ActorType.COORDINATOR,
            self._recovery_actor_id if lost else payload.coordinator_id,
            payload,
            RuntimeReasonCode.LEASE_LOST if lost else None,
        )

    def _holds(self, lease: LeasePayload) -> bool:
        return (
            lease.coordinator_id == self._coordinator_id
            and lease.owner == self._owner
            and lease.scheduler_mode is SchedulerMode.WISH_BUILDER
            and lease.manifest_digest == self._manifest_digest
        )

    def _takeover_blocked(
        self,
        action: LeaseAction,
        recovery: LeaseRecoveryResult,
    ) -> LeaseMutationResult | None:
        state = recovery.lease_state
        assert state is not None
        if not state.active or state.lease is None or self._holds(state.lease):
            return None
        try:
            proof = self._prior_owner_process_probe(
                state.lease.owner,
                local_host_id=self._owner.actor.host_id,
            )
        except Exception as exc:
            return LeaseMutationResult(
                action,
                LeaseMutationStatus.REJECTED,
                recovery,
                state,
                lease_state_code=LeaseStateCode.LIVE_LEASE_CONFLICT,
                detail=f"prior_lease_owner_process_probe_error:{_detail(exc)}",
            )
        if type(proof) is not LeaseOwnerProcessProbeResult:
            raise TypeError("prior_owner_process_probe returned an invalid result")
        if proof.state is LeaseOwnerProcessState.DEAD:
            return None
        code = (
            LeaseStateCode.LEASE_IDENTITY_MISMATCH
            if proof.state is LeaseOwnerProcessState.PID_REUSED
            else LeaseStateCode.LIVE_LEASE_CONFLICT
        )
        return LeaseMutationResult(
            action,
            LeaseMutationStatus.REJECTED,
            recovery,
            state,
            lease_state_code=code,
            detail=f"prior_lease_owner_process_not_dead:{proof.state.value}",
        )

    def _blocked_recovery(
        self,
        action: LeaseAction,
        recovery: LeaseRecoveryResult,
    ) -> LeaseMutationResult | None:
        if (
            recovery.status is LeaseRecoveryStatus.RECOVERED
            and recovery.replay.snapshot.run_id == self._run_id
        ):
            return None
        detail = "lease recovery did not produce verified state"
        if recovery.replay.snapshot.run_id != self._run_id:
            detail = "recovered journal run_id does not match the lease service"
        elif recovery.fault is not None:
            detail = f"{recovery.fault.code.value}:{recovery.fault.detail}"
        return LeaseMutationResult(
            action,
            LeaseMutationStatus.BLOCKED,
            recovery,
            None,
            detail=detail,
        )

    @staticmethod
    def _rejected(
        action: LeaseAction,
        recovery: LeaseRecoveryResult,
        code: LeaseStateCode,
    ) -> LeaseMutationResult:
        return LeaseMutationResult(
            action,
            LeaseMutationStatus.REJECTED,
            recovery,
            recovery.lease_state,
            lease_state_code=code,
        )

    @staticmethod
    def _retry_exhausted(
        action: LeaseAction,
        recovery: LeaseRecoveryResult,
    ) -> LeaseMutationResult:
        return LeaseMutationResult(
            action,
            LeaseMutationStatus.BLOCKED,
            recovery,
            recovery.lease_state,
            detail="concurrent journal changes exceeded the recovery retry limit",
        )


__all__ = [
    "CoordinatorLeaseService",
    "DispatchRecoveryProjectionError",
    "DispatchRecoveryRecord",
    "LeaseAction",
    "LeaseJournal",
    "LeaseMutationResult",
    "LeaseMutationStatus",
    "LeaseOwnerProcessProbe",
    "LeaseRecoveryFault",
    "LeaseRecoveryFaultCode",
    "LeaseRecoveryResult",
    "LeaseRecoveryStatus",
    "advance_dispatch_recoveries",
    "recover_coordinator_lease",
]
