"""Durable compare-and-append service for the active-M1 logical journal."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Callable, Protocol

from wish_builder.contracts import (
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    JournalPayload,
    LeaseDraftPayload,
    LeaseOwner,
    LeasePayload,
    RuntimeReasonCode,
    SchedulerMode,
    decode_journal_event_bytes,
)

_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ZERO_HASH = "sha256:" + "0" * 64


class AppendStatus(StrEnum):
    COMMITTED = "committed"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    PERSISTENCE_FAILED = "persistence_failed"


class JournalFaultCode(StrEnum):
    CONTROL_ROOT_DRIFT = "control_root_drift"
    LOCK_OPEN_FAILED = "lock_open_failed"
    LOCK_ACQUIRE_FAILED = "lock_acquire_failed"
    SEGMENT_OPEN_FAILED = "segment_open_failed"
    WRITE_FAILED = "write_failed"
    SHORT_WRITE = "short_write"
    FLUSH_FAILED = "flush_failed"
    FSYNC_FAILED = "fsync_failed"
    DISK_FULL = "disk_full"
    PERMISSION_DENIED = "permission_denied"
    ROTATION_FAILED = "rotation_failed"
    ATOMIC_PUBLISH_FAILED = "atomic_publish_failed"
    DIRECTORY_SYNC_FAILED = "directory_sync_failed"
    JOURNAL_CORRUPT = "journal_corrupt"
    INVALID_LAYOUT = "invalid_layout"
    EVENT_TOO_LARGE = "event_too_large"
    CLOCK_ROLLBACK = "clock_rollback"


@dataclass(frozen=True, slots=True)
class JournalHead:
    sequence: int
    event_hash: str

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("journal head sequence must be a non-negative integer")
        if type(self.event_hash) is not str or not _HASH_RE.fullmatch(self.event_hash):
            raise ValueError("journal head hash must be a full sha256 reference")
        if self.sequence == 0 and self.event_hash != _ZERO_HASH:
            raise ValueError("the genesis head must use the zero hash")


GENESIS_HEAD = JournalHead(0, _ZERO_HASH)


@dataclass(frozen=True, slots=True)
class SegmentPolicy:
    max_events: int = 100
    max_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if type(self.max_events) is not int or self.max_events <= 0:
            raise ValueError("max_events must be a positive integer")
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")


_COORDINATOR_LEASE_EVENTS = frozenset(
    {
        JournalEventType.LEASE_ACQUIRED,
        JournalEventType.LEASE_RENEWED,
        JournalEventType.LEASE_RELEASED,
        JournalEventType.LEASE_LOST,
    }
)
_ACTIVE_LEASE_EVENTS = frozenset(
    {JournalEventType.LEASE_ACQUIRED, JournalEventType.LEASE_RENEWED}
)


@dataclass(frozen=True, slots=True)
class JournalEventDraft:
    """Caller-controlled event fields before the Journal assigns authority data."""

    event_id: str
    event_type: JournalEventType
    identity: ExecutionIdentity
    actor_type: ActorType
    actor_id: str
    payload: JournalPayload | LeaseDraftPayload
    reason_code: RuntimeReasonCode | None = None

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or not self.event_id:
            raise ValueError("event_id must be a non-empty string")
        if type(self.event_type) is not JournalEventType:
            raise TypeError("event_type must be a JournalEventType")
        if type(self.identity) is not ExecutionIdentity:
            raise TypeError("identity must be an ExecutionIdentity")
        if type(self.actor_type) is not ActorType:
            raise TypeError("actor_type must be an ActorType")
        if type(self.actor_id) is not str or not self.actor_id:
            raise ValueError("actor_id must be a non-empty string")
        if (
            self.reason_code is not None
            and type(self.reason_code) is not RuntimeReasonCode
        ):
            raise TypeError("reason_code must be a RuntimeReasonCode or null")
        if type(self.payload) is LeaseDraftPayload:
            if self.event_type not in _COORDINATOR_LEASE_EVENTS:
                raise ValueError("a lease draft requires a coordinator lease event")
            if self.identity.coordinator_epoch != self.payload.fencing_token:
                raise ValueError("lease fencing token must match coordinator_epoch")
        elif self.event_type in {
            JournalEventType.LEASE_RENEWED,
            JournalEventType.LEASE_RELEASED,
            JournalEventType.LEASE_LOST,
        }:
            raise ValueError("coordinator lease events require a LeaseDraftPayload")

    @property
    def is_coordinator_lease(self) -> bool:
        return type(self.payload) is LeaseDraftPayload

    def materialize(
        self,
        *,
        sequence: int,
        previous_event_hash: str,
        authority_time: datetime,
    ) -> JournalEvent:
        if type(authority_time) is not datetime or authority_time.tzinfo is None:
            raise ValueError("authority_time must be a timezone-aware datetime")
        authority_time = authority_time.astimezone(timezone.utc)
        recorded_at = authority_time.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        payload: JournalPayload
        if type(self.payload) is LeaseDraftPayload:
            payload = self.payload.materialize(
                authority_time,
                terminal=self.event_type
                in {JournalEventType.LEASE_RELEASED, JournalEventType.LEASE_LOST},
            )
        else:
            payload = self.payload
        return JournalEvent.create(
            sequence=sequence,
            event_id=self.event_id,
            event_type=self.event_type,
            identity=self.identity,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            recorded_at=recorded_at,
            previous_event_hash=previous_event_hash,
            payload=payload,
            reason_code=self.reason_code,
        )

    def matches_event(self, event: JournalEvent) -> bool:
        if type(event) is not JournalEvent:
            return False
        if (
            self.event_id != event.event_id
            or self.event_type is not event.event_type
            or self.identity != event.identity
            or self.actor_type is not event.actor_type
            or self.actor_id != event.actor_id
            or self.reason_code is not event.reason_code
        ):
            return False
        if type(self.payload) is not LeaseDraftPayload:
            return self.payload == event.payload
        if type(event.payload) is not LeasePayload:
            return False
        return self.payload.to_primitive() == {
            key: value
            for key, value in event.payload.to_primitive().items()
            if key not in {"committed_at", "expires_at", "payload_type"}
        }


class LeaseStateCode(StrEnum):
    HEAD_MISMATCH = "head_mismatch"
    EVENT_MISMATCH = "event_mismatch"
    FENCING_TOKEN_NOT_ADVANCED = "fencing_token_not_advanced"
    LIVE_LEASE_CONFLICT = "live_lease_conflict"
    NO_ACTIVE_LEASE = "no_active_lease"
    LEASE_IDENTITY_MISMATCH = "lease_identity_mismatch"
    RENEWAL_TOO_LATE = "renewal_too_late"
    EXPIRY_NOT_EXTENDED = "expiry_not_extended"


class LeaseStateError(ValueError):
    def __init__(self, code: LeaseStateCode) -> None:
        if type(code) is not LeaseStateCode:
            raise TypeError("code must be a LeaseStateCode")
        self.code = code
        super().__init__(code.value)


def _utc_value(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _same_lease(left: LeasePayload, right: LeasePayload) -> bool:
    return (
        left.lease_id,
        left.coordinator_id,
        left.owner,
        left.scheduler_mode,
        left.fencing_token,
        left.manifest_digest,
        left.lease_ttl_seconds,
        left.lease_clock_skew_seconds,
    ) == (
        right.lease_id,
        right.coordinator_id,
        right.owner,
        right.scheduler_mode,
        right.fencing_token,
        right.manifest_digest,
        right.lease_ttl_seconds,
        right.lease_clock_skew_seconds,
    )


@dataclass(frozen=True, slots=True)
class CoordinatorLeaseState:
    """Replayable lease authority bound to one exact Journal head."""

    head: JournalHead
    event_type: JournalEventType | None = None
    lease: LeasePayload | None = None
    max_fencing_token: int = 0

    def __post_init__(self) -> None:
        if type(self.head) is not JournalHead:
            raise TypeError("head must be a JournalHead")
        if self.event_type is None:
            if self.lease is not None or self.max_fencing_token != 0:
                raise ValueError("an empty lease state cannot contain lease data")
            return
        if self.event_type not in _COORDINATOR_LEASE_EVENTS:
            raise ValueError("event_type must be a coordinator lease event or null")
        if type(self.lease) is not LeasePayload:
            raise TypeError("lease must be a LeasePayload")
        if (
            type(self.max_fencing_token) is not int
            or self.max_fencing_token != self.lease.fencing_token
        ):
            raise ValueError("max_fencing_token must identify the latest lease")

    @classmethod
    def initial(cls, head: JournalHead = GENESIS_HEAD) -> CoordinatorLeaseState:
        return cls(head)

    @property
    def active(self) -> bool:
        return self.event_type in _ACTIVE_LEASE_EVENTS

    def validate_next(self, event: JournalEvent) -> None:
        self.advance(event)

    def advance(self, event: JournalEvent) -> CoordinatorLeaseState:
        if type(event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent")
        event_head = JournalHead(event.sequence, event.event_hash)
        if event_head == self.head:
            return self
        if (
            event.sequence != self.head.sequence + 1
            or event.previous_event_hash != self.head.event_hash
        ):
            raise LeaseStateError(LeaseStateCode.HEAD_MISMATCH)
        if type(event.payload) is not LeasePayload:
            return CoordinatorLeaseState(
                event_head,
                self.event_type,
                self.lease,
                self.max_fencing_token,
            )
        if event.event_type not in _COORDINATOR_LEASE_EVENTS:
            raise LeaseStateError(LeaseStateCode.EVENT_MISMATCH)
        payload = event.payload
        if event.event_type is JournalEventType.LEASE_ACQUIRED:
            if payload.fencing_token <= self.max_fencing_token:
                raise LeaseStateError(LeaseStateCode.FENCING_TOKEN_NOT_ADVANCED)
            if self.active and self.lease is not None:
                takeover_after = _utc_value(self.lease.expires_at) + timedelta(
                    seconds=self.lease.lease_clock_skew_seconds
                )
                if _utc_value(payload.committed_at) <= takeover_after:
                    raise LeaseStateError(LeaseStateCode.LIVE_LEASE_CONFLICT)
        else:
            if not self.active or self.lease is None:
                raise LeaseStateError(LeaseStateCode.NO_ACTIVE_LEASE)
            if not _same_lease(self.lease, payload):
                raise LeaseStateError(LeaseStateCode.LEASE_IDENTITY_MISMATCH)
            if event.event_type is JournalEventType.LEASE_RENEWED:
                renew_by = _utc_value(self.lease.expires_at) - timedelta(
                    seconds=self.lease.lease_clock_skew_seconds
                )
                if _utc_value(payload.committed_at) >= renew_by:
                    raise LeaseStateError(LeaseStateCode.RENEWAL_TOO_LATE)
                if _utc_value(payload.expires_at) <= _utc_value(self.lease.expires_at):
                    raise LeaseStateError(LeaseStateCode.EXPIRY_NOT_EXTENDED)
        return CoordinatorLeaseState(
            event_head,
            event.event_type,
            payload,
            max(self.max_fencing_token, payload.fencing_token),
        )

    def allows_admission(
        self,
        *,
        authority_time: datetime,
        coordinator_id: str,
        owner: LeaseOwner,
        fencing_token: int,
        manifest_digest: str,
        scheduler_mode: SchedulerMode = SchedulerMode.WISH_BUILDER,
    ) -> bool:
        if type(authority_time) is not datetime or authority_time.tzinfo is None:
            raise ValueError("authority_time must be a timezone-aware datetime")
        if type(owner) is not LeaseOwner:
            raise TypeError("owner must be a LeaseOwner")
        if not self.active or self.lease is None:
            return False
        lease = self.lease
        safe_until = _utc_value(lease.expires_at) - timedelta(
            seconds=lease.lease_clock_skew_seconds
        )
        return (
            authority_time.astimezone(timezone.utc) < safe_until
            and coordinator_id == lease.coordinator_id
            and owner == lease.owner
            and fencing_token == lease.fencing_token
            and manifest_digest == lease.manifest_digest
            and scheduler_mode is lease.scheduler_mode
        )


@dataclass(frozen=True, slots=True)
class AppendResult:
    status: AppendStatus
    head: JournalHead | None
    event: JournalEvent | None = None
    fault_code: JournalFaultCode | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not AppendStatus:
            raise TypeError("status must be an AppendStatus")
        if self.head is not None and type(self.head) is not JournalHead:
            raise TypeError("head must be a JournalHead or null")
        if self.event is not None and type(self.event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent or null")
        if (
            self.fault_code is not None
            and type(self.fault_code) is not JournalFaultCode
        ):
            raise TypeError("fault_code must be a JournalFaultCode or null")

        if self.status in {AppendStatus.COMMITTED, AppendStatus.IDEMPOTENT}:
            if self.event is None or self.head is None or self.fault_code is not None:
                raise ValueError("a durable append result requires an event and head")
            if (
                self.head.sequence != self.event.sequence
                or self.head.event_hash != self.event.event_hash
            ):
                raise ValueError("the durable result head must identify its event")
        elif self.status is AppendStatus.CONFLICT:
            if (
                self.head is None
                or self.event is not None
                or self.fault_code is not None
            ):
                raise ValueError("a conflict requires only the observed journal head")
        elif self.status is AppendStatus.PERSISTENCE_FAILED:
            if self.event is not None or self.fault_code is None:
                raise ValueError(
                    "a persistence failure requires exactly one fault code"
                )

    @property
    def durable(self) -> bool:
        return self.status in {AppendStatus.COMMITTED, AppendStatus.IDEMPOTENT}


class PersistenceFault(RuntimeError):
    """A named persistence boundary failure with no invented commit outcome."""

    def __init__(
        self,
        code: JournalFaultCode,
        operation: str,
        *,
        last_committed_head: JournalHead | None = None,
        os_error: int | None = None,
    ) -> None:
        if type(code) is not JournalFaultCode:
            raise TypeError("code must be a JournalFaultCode")
        if type(operation) is not str or not operation:
            raise ValueError("operation must be a non-empty string")
        if (
            last_committed_head is not None
            and type(last_committed_head) is not JournalHead
        ):
            raise TypeError("last_committed_head must be a JournalHead or null")
        if os_error is not None and (type(os_error) is not int or os_error < 0):
            raise ValueError("os_error must be a non-negative integer or null")
        super().__init__(f"{code.value}:{operation}")
        self.code = code
        self.operation = operation
        self.last_committed_head = last_committed_head
        self.os_error = os_error


class JournalStorage(Protocol):
    @property
    def recovery_scope(self) -> str:
        """Identify one durable journal across independently constructed adapters."""

    def compare_and_append(
        self,
        *,
        event: JournalEvent,
        frame: bytes,
        expected_head: JournalHead,
        policy: SegmentPolicy,
    ) -> AppendResult:
        """Atomically compare the durable head and append one canonical frame."""

    def compare_and_append_draft(
        self,
        *,
        draft: JournalEventDraft,
        expected_head: JournalHead,
        policy: SegmentPolicy,
        validate_event: Callable[[JournalEvent], None] | None = None,
    ) -> AppendResult:
        """Materialize and append a draft under the same authority lock."""

    def current_position(
        self,
        *,
        expected_head: JournalHead,
        policy: SegmentPolicy,
    ) -> tuple[int, int]:
        """Return the verified active segment number and byte offset."""


class DurableJournal:
    """Validate journal events and fail closed after any persistence ambiguity."""

    def __init__(
        self,
        run_id: str,
        storage: JournalStorage,
        *,
        policy: SegmentPolicy = SegmentPolicy(),
    ) -> None:
        if type(run_id) is not str or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if type(policy) is not SegmentPolicy:
            raise TypeError("policy must be a SegmentPolicy")
        self._run_id = run_id
        self._storage = storage
        storage_scope = getattr(storage, "recovery_scope", None)
        self._recovery_scope = (
            storage_scope
            if type(storage_scope) is str and storage_scope
            else f"instance:{type(storage).__module__}.{type(storage).__qualname__}:{id(storage)}"
        )
        self._policy = policy
        self._call_lock = threading.Lock()
        self._blocked_fault: PersistenceFault | None = None

    @property
    def blocked(self) -> bool:
        return self._blocked_fault is not None

    @property
    def recovery_scope(self) -> str:
        return self._recovery_scope

    def current_position(self, *, expected_head: JournalHead) -> tuple[int, int]:
        """Observe the exact durable position bound to ``expected_head``."""

        if type(expected_head) is not JournalHead:
            raise TypeError("expected_head must be a JournalHead")
        with self._call_lock:
            if self._blocked_fault is not None:
                raise self._blocked_fault
            position = self._storage.current_position(
                expected_head=expected_head,
                policy=self._policy,
            )
            if (
                type(position) is not tuple
                or len(position) != 2
                or type(position[0]) is not int
                or position[0] <= 0
                or type(position[1]) is not int
                or position[1] < 0
            ):
                raise TypeError("journal storage returned an invalid position")
            return position

    def append(
        self,
        event: JournalEvent,
        *,
        expected_head: JournalHead,
    ) -> AppendResult:
        if type(event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent")
        if type(expected_head) is not JournalHead:
            raise TypeError("expected_head must be a JournalHead")
        if event.identity.run_id != self._run_id:
            raise ValueError("event run_id does not match the journal")
        if event.sequence != expected_head.sequence + 1:
            raise ValueError("event sequence must immediately follow expected_head")
        if event.previous_event_hash != expected_head.event_hash:
            raise ValueError("event previous hash must match expected_head")

        frame = event.canonical_json_bytes()
        decoded = decode_journal_event_bytes(frame)
        if not decoded.ok or decoded.value != event:
            raise ValueError("event must pass the strict JournalEvent decoder")
        if not frame.endswith(b"\n") or frame.count(b"\n") != 1:
            raise ValueError("a journal event must have one canonical JSONL delimiter")

        with self._call_lock:
            if self._blocked_fault is not None:
                return AppendResult(
                    AppendStatus.PERSISTENCE_FAILED,
                    self._blocked_fault.last_committed_head,
                    fault_code=self._blocked_fault.code,
                )
            try:
                result = self._storage.compare_and_append(
                    event=event,
                    frame=frame,
                    expected_head=expected_head,
                    policy=self._policy,
                )
            except PersistenceFault as fault:
                self._blocked_fault = fault
                return AppendResult(
                    AppendStatus.PERSISTENCE_FAILED,
                    fault.last_committed_head,
                    fault_code=fault.code,
                )
            if type(result) is not AppendResult:
                raise TypeError("journal storage returned an invalid append result")
            if result.durable and result.event != event:
                raise TypeError(
                    "journal storage returned a durable result for another event"
                )
            if result.status is AppendStatus.CONFLICT and result.head == expected_head:
                raise TypeError(
                    "journal storage returned a conflict without a head conflict"
                )
            return result

    def append_draft(
        self,
        draft: JournalEventDraft,
        *,
        expected_head: JournalHead,
        lease_state: CoordinatorLeaseState | None = None,
    ) -> AppendResult:
        """Append a storage-materialized event; use this path for all new writes."""

        if type(draft) is not JournalEventDraft:
            raise TypeError("draft must be a JournalEventDraft")
        if type(expected_head) is not JournalHead:
            raise TypeError("expected_head must be a JournalHead")
        if draft.identity.run_id != self._run_id:
            raise ValueError("draft run_id does not match the journal")
        validate_event: Callable[[JournalEvent], None] | None = None
        if draft.is_coordinator_lease:
            if type(lease_state) is not CoordinatorLeaseState:
                raise TypeError("coordinator lease drafts require a lease_state")
            if lease_state.head != expected_head:
                raise ValueError("lease_state must be bound to expected_head")
            validate_event = lease_state.validate_next
        elif lease_state is not None and type(lease_state) is not CoordinatorLeaseState:
            raise TypeError("lease_state must be a CoordinatorLeaseState or null")

        with self._call_lock:
            if self._blocked_fault is not None:
                return AppendResult(
                    AppendStatus.PERSISTENCE_FAILED,
                    self._blocked_fault.last_committed_head,
                    fault_code=self._blocked_fault.code,
                )
            try:
                result = self._storage.compare_and_append_draft(
                    draft=draft,
                    expected_head=expected_head,
                    policy=self._policy,
                    validate_event=validate_event,
                )
            except PersistenceFault as fault:
                self._blocked_fault = fault
                return AppendResult(
                    AppendStatus.PERSISTENCE_FAILED,
                    fault.last_committed_head,
                    fault_code=fault.code,
                )
            if type(result) is not AppendResult:
                raise TypeError("journal storage returned an invalid append result")
            if result.durable and (
                result.event is None or not draft.matches_event(result.event)
            ):
                raise TypeError(
                    "journal storage returned a durable result for another draft"
                )
            if result.status is AppendStatus.CONFLICT and result.head == expected_head:
                raise TypeError(
                    "journal storage returned a conflict without a head conflict"
                )
            return result


__all__ = [
    "GENESIS_HEAD",
    "AppendResult",
    "AppendStatus",
    "CoordinatorLeaseState",
    "DurableJournal",
    "JournalFaultCode",
    "JournalHead",
    "JournalEventDraft",
    "JournalStorage",
    "LeaseStateCode",
    "LeaseStateError",
    "PersistenceFault",
    "SegmentPolicy",
]
