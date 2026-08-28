"""Standard-library filesystem implementation of the Journal storage port."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Protocol

from wish_builder.adapters.git_identity import ProtectedControlRoot
from wish_builder.contracts import (
    DEFAULT_DECODE_LIMITS,
    JournalEvent,
    canonical_json_bytes,
    decode_journal_event_bytes,
)
from wish_builder.services.journal import (
    GENESIS_HEAD,
    AppendResult,
    AppendStatus,
    JournalFaultCode,
    JournalEventDraft,
    JournalHead,
    PersistenceFault,
    SegmentPolicy,
)

_INDEX_VERSION = 1
_INDEX_MAX_BYTES = 16 * 1024
_SEGMENT_RE = re.compile(r"segment-([0-9]{8})\.jsonl\Z")
_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class StorageFailpoint(Protocol):
    def __call__(
        self,
        point: str,
        requested_bytes: int | None = None,
    ) -> int | None:
        """Raise an OSError or return a write limit at one named boundary."""


@dataclass(frozen=True, slots=True)
class _SegmentDescriptor:
    number: int
    path: Path
    start_head: JournalHead
    head: JournalHead
    event_count: int
    byte_count: int
    content_hash: str
    last_event: JournalEvent | None


@dataclass(frozen=True, slots=True)
class _IndexRecord:
    sealed_segment: int
    active_segment: int
    start_sequence: int
    previous_event_hash: str
    last_sequence: int
    last_event_hash: str
    event_count: int
    byte_count: int
    segment_hash: str

    def to_primitive(self) -> dict[str, object]:
        return {
            "active_segment": self.active_segment,
            "byte_count": self.byte_count,
            "event_count": self.event_count,
            "index_version": _INDEX_VERSION,
            "last_event_hash": self.last_event_hash,
            "last_sequence": self.last_sequence,
            "previous_event_hash": self.previous_event_hash,
            "sealed_segment": self.sealed_segment,
            "segment_hash": self.segment_hash,
            "start_sequence": self.start_sequence,
        }


def _pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate index key")
        result[key] = value
    return result


class FilesystemJournalStorage:
    """Append canonical JSONL frames under a short cross-process lock."""

    def __init__(
        self,
        journal_root: str | os.PathLike[str],
        run_id: str,
        *,
        fault_injector: StorageFailpoint | None = None,
        lock_timeout_seconds: float = 5.0,
        control_root: ProtectedControlRoot | None = None,
        authority_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(run_id) is not str or not run_id:
            raise ValueError("run_id must be a non-empty string")
        if (
            type(lock_timeout_seconds) not in {int, float}
            or not math.isfinite(lock_timeout_seconds)
            or lock_timeout_seconds <= 0
        ):
            raise ValueError("lock_timeout_seconds must be positive")
        self.root = Path(journal_root).expanduser().absolute()
        self.segments = self.root / "segments"
        self.index_path = self.root / "index.json"
        self.lock_path = self.root / "append.lock"
        self.run_id = run_id
        self.recovery_scope = os.path.normcase(os.path.abspath(self.root))
        self._fault_injector = fault_injector
        self._lock_timeout_seconds = float(lock_timeout_seconds)
        if authority_clock is not None and not callable(authority_clock):
            raise TypeError("authority_clock must be callable or null")
        self._authority_clock = authority_clock or (lambda: datetime.now(timezone.utc))
        if control_root is not None and type(control_root) is not ProtectedControlRoot:
            raise TypeError("control_root must be a ProtectedControlRoot or null")
        if control_root is not None:
            guarded_path = os.path.normcase(
                os.path.abspath(control_root.expected.lexical_path)
            )
            journal_path = os.path.normcase(os.path.abspath(self.root))
            try:
                common_path = os.path.commonpath((guarded_path, journal_path))
            except ValueError as exc:
                raise ValueError("journal_root must be inside control_root") from exc
            if common_path != guarded_path:
                raise ValueError("journal_root must be inside control_root")
        self._control_root = control_root

    def compare_and_append(
        self,
        *,
        event: JournalEvent,
        frame: bytes,
        expected_head: JournalHead,
        policy: SegmentPolicy,
    ) -> AppendResult:
        self._validate_request(event, frame, expected_head, policy)
        self._guard_control_root("compare_and_append", expected_head)
        self._ensure_directories()
        with self._append_lock():
            self._guard_control_root("append_lock_acquired", expected_head)
            active = self._load_active_segment(policy)
            current_head = active.head

            if (
                current_head.sequence == event.sequence
                and current_head.event_hash == event.event_hash
            ):
                self._durabilize_observed_head(active.path, current_head)
                return AppendResult(AppendStatus.IDEMPOTENT, current_head, event)

            if current_head != expected_head:
                self._durabilize_observed_head(active.path, current_head)
                return AppendResult(AppendStatus.CONFLICT, current_head)

            if len(frame) > policy.max_bytes:
                raise PersistenceFault(
                    JournalFaultCode.EVENT_TOO_LARGE,
                    "segment_write",
                    last_committed_head=active.head,
                )
            if self._rotation_required(active, len(frame), policy):
                active = self._rotate(active, policy)

            self._append_frame(active.path, frame, active.head)
            committed_head = JournalHead(event.sequence, event.event_hash)
            return AppendResult(AppendStatus.COMMITTED, committed_head, event)

    def compare_and_append_draft(
        self,
        *,
        draft: JournalEventDraft,
        expected_head: JournalHead,
        policy: SegmentPolicy,
        validate_event: Callable[[JournalEvent], None] | None = None,
    ) -> AppendResult:
        if type(draft) is not JournalEventDraft:
            raise TypeError("draft must be a JournalEventDraft")
        if type(expected_head) is not JournalHead:
            raise TypeError("expected_head must be a JournalHead")
        if type(policy) is not SegmentPolicy:
            raise TypeError("policy must be a SegmentPolicy")
        if validate_event is not None and not callable(validate_event):
            raise TypeError("validate_event must be callable or null")
        if draft.identity.run_id != self.run_id:
            raise ValueError("draft run_id does not match the storage run")

        self._guard_control_root("compare_and_append_draft", expected_head)
        self._ensure_directories()
        with self._append_lock():
            self._guard_control_root("append_lock_acquired", expected_head)
            active = self._load_active_segment(policy)
            current_head = active.head

            if current_head != expected_head:
                if active.last_event is not None and draft.matches_event(active.last_event):
                    self._durabilize_observed_head(active.path, current_head)
                    return AppendResult(
                        AppendStatus.IDEMPOTENT,
                        current_head,
                        active.last_event,
                    )
                self._durabilize_observed_head(active.path, current_head)
                return AppendResult(AppendStatus.CONFLICT, current_head)

            authority_time = self._read_authority_time(active)
            event = draft.materialize(
                sequence=current_head.sequence + 1,
                previous_event_hash=current_head.event_hash,
                authority_time=authority_time,
            )
            frame = event.canonical_json_bytes()
            decoded = decode_journal_event_bytes(frame)
            if not decoded.ok or decoded.value != event:
                raise ValueError("materialized draft must pass the strict JournalEvent decoder")
            if validate_event is not None:
                validate_event(event)

            if len(frame) > policy.max_bytes:
                raise PersistenceFault(
                    JournalFaultCode.EVENT_TOO_LARGE,
                    "segment_write",
                    last_committed_head=active.head,
                )
            if self._rotation_required(active, len(frame), policy):
                active = self._rotate(active, policy)

            self._append_frame(active.path, frame, active.head)
            committed_head = JournalHead(event.sequence, event.event_hash)
            return AppendResult(AppendStatus.COMMITTED, committed_head, event)

    def current_position(
        self,
        *,
        expected_head: JournalHead,
        policy: SegmentPolicy,
    ) -> tuple[int, int]:
        """Read the active segment position under the append lock."""

        if type(expected_head) is not JournalHead:
            raise TypeError("expected_head must be a JournalHead")
        if type(policy) is not SegmentPolicy:
            raise TypeError("policy must be a SegmentPolicy")
        self._guard_control_root("position_observe", expected_head)
        self._ensure_directories()
        with self._append_lock():
            self._guard_control_root("position_lock_acquired", expected_head)
            active = self._load_active_segment(policy)
            if active.head != expected_head:
                raise PersistenceFault(
                    JournalFaultCode.JOURNAL_CORRUPT,
                    "position_head_mismatch",
                    last_committed_head=active.head,
                )
            return active.number, active.byte_count

    def _read_authority_time(self, active: _SegmentDescriptor) -> datetime:
        try:
            value = self._authority_clock()
        except Exception as exc:
            raise PersistenceFault(
                JournalFaultCode.CLOCK_ROLLBACK,
                "authority_clock",
                last_committed_head=active.head,
            ) from exc
        if type(value) is not datetime or value.tzinfo is None:
            raise PersistenceFault(
                JournalFaultCode.CLOCK_ROLLBACK,
                "authority_clock",
                last_committed_head=active.head,
            )
        current = value.astimezone(timezone.utc)
        if active.last_event is not None:
            previous = datetime.fromisoformat(
                active.last_event.recorded_at[:-1] + "+00:00"
            )
            if current < previous:
                raise PersistenceFault(
                    JournalFaultCode.CLOCK_ROLLBACK,
                    "authority_clock_rollback",
                    last_committed_head=active.head,
                )
        return current

    def _validate_request(
        self,
        event: JournalEvent,
        frame: bytes,
        expected_head: JournalHead,
        policy: SegmentPolicy,
    ) -> None:
        if type(event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent")
        if type(frame) is not bytes:
            raise TypeError("frame must be bytes")
        if type(expected_head) is not JournalHead:
            raise TypeError("expected_head must be a JournalHead")
        if type(policy) is not SegmentPolicy:
            raise TypeError("policy must be a SegmentPolicy")
        if event.identity.run_id != self.run_id:
            raise ValueError("event run_id does not match the storage run")
        if event.sequence != expected_head.sequence + 1:
            raise ValueError("event sequence must immediately follow expected_head")
        if event.previous_event_hash != expected_head.event_hash:
            raise ValueError("event previous hash must match expected_head")
        if frame != event.canonical_json_bytes():
            raise ValueError("frame must be the event's canonical JSON bytes")
        decoded = decode_journal_event_bytes(frame)
        if not decoded.ok or decoded.value != event:
            raise ValueError("frame must decode as the exact JournalEvent")

    def _trigger(
        self,
        point: str,
        *,
        requested_bytes: int | None = None,
        default_code: JournalFaultCode,
        operation: str,
        head: JournalHead | None,
    ) -> int | None:
        result: int | None = None
        if self._fault_injector is not None:
            try:
                result = self._fault_injector(point, requested_bytes)
            except OSError as exc:
                self._raise_os_fault(exc, default_code, operation, head)
        self._guard_control_root(operation, head)
        if result is not None and (
            type(result) is not int
            or requested_bytes is None
            or result < 0
            or result > requested_bytes
        ):
            raise TypeError("a storage failpoint returned an invalid write limit")
        return result

    def _guard_control_root(
        self,
        operation: str,
        head: JournalHead | None,
    ) -> None:
        if self._control_root is None:
            return
        try:
            valid = self._control_root.revalidate().ok
        except (OSError, RuntimeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise PersistenceFault(
                JournalFaultCode.CONTROL_ROOT_DRIFT,
                operation,
                last_committed_head=head,
            )

    @staticmethod
    def _fault_code_for_os_error(
        exc: OSError,
        default_code: JournalFaultCode,
    ) -> JournalFaultCode:
        disk_codes = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
        permission_codes = {
            errno.EACCES,
            errno.EPERM,
            getattr(errno, "EROFS", errno.EACCES),
        }
        if exc.errno in disk_codes:
            return JournalFaultCode.DISK_FULL
        if exc.errno in permission_codes or isinstance(exc, PermissionError):
            return JournalFaultCode.PERMISSION_DENIED
        return default_code

    def _raise_os_fault(
        self,
        exc: OSError,
        default_code: JournalFaultCode,
        operation: str,
        head: JournalHead | None,
    ) -> None:
        raise PersistenceFault(
            self._fault_code_for_os_error(exc, default_code),
            operation,
            last_committed_head=head,
            os_error=exc.errno if type(exc.errno) is int and exc.errno >= 0 else None,
        ) from exc

    def _ensure_directories(self) -> None:
        root_existed = self.root.is_dir()
        segments_existed = self.segments.is_dir()
        self._trigger(
            "layout_open",
            default_code=JournalFaultCode.SEGMENT_OPEN_FAILED,
            operation="layout_open",
            head=None,
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.segments.mkdir(exist_ok=True)
        except OSError as exc:
            self._raise_os_fault(
                exc,
                JournalFaultCode.INVALID_LAYOUT,
                "layout_open",
                None,
            )
        if not root_existed:
            self._sync_directory(self.root.parent, "journal_parent_sync", None)
        if not segments_existed:
            self._sync_directory(self.root, "layout_parent_sync", None)

    @contextmanager
    def _append_lock(self) -> Iterator[None]:
        self._trigger(
            "lock_open",
            default_code=JournalFaultCode.LOCK_OPEN_FAILED,
            operation="lock_open",
            head=None,
        )
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            handle = os.fdopen(descriptor, "r+b", buffering=0)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            self._raise_os_fault(
                exc,
                JournalFaultCode.LOCK_OPEN_FAILED,
                "lock_open",
                None,
            )
        acquired = False
        try:
            try:
                if os.fstat(handle.fileno()).st_size == 0:
                    written = handle.write(b"\0")
                    if written != 1:
                        raise PersistenceFault(
                            JournalFaultCode.SHORT_WRITE,
                            "lock_initialize",
                        )
                    self._flush_file(handle, "lock_flush", None)
                    self._fsync_file(handle, "lock_fsync", None)
                self._trigger(
                    "lock_acquire",
                    default_code=JournalFaultCode.LOCK_ACQUIRE_FAILED,
                    operation="lock_acquire",
                    head=None,
                )
                self._lock_handle(handle)
                acquired = True
            except OSError as exc:
                self._raise_os_fault(
                    exc,
                    JournalFaultCode.LOCK_ACQUIRE_FAILED,
                    "lock_acquire",
                    None,
                )
            yield
        finally:
            if acquired:
                self._unlock_handle(handle)
            handle.close()

    def _lock_handle(self, handle: BinaryIO) -> None:
        deadline = time.monotonic() + self._lock_timeout_seconds
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError(errno.EBUSY, "journal append lock is busy")
                    time.sleep(0.01)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OSError(errno.EBUSY, "journal append lock is busy")
                    time.sleep(0.01)

    @staticmethod
    def _unlock_handle(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _segment_paths(self) -> list[tuple[int, Path]]:
        try:
            entries = list(self.segments.iterdir())
        except OSError as exc:
            self._raise_os_fault(
                exc,
                JournalFaultCode.SEGMENT_OPEN_FAILED,
                "segment_list",
                None,
            )
        result: list[tuple[int, Path]] = []
        for path in entries:
            match = _SEGMENT_RE.fullmatch(path.name)
            if match is not None:
                result.append((int(match.group(1)), path))
        result.sort(key=lambda item: item[0])
        if result and [number for number, _ in result] != list(
            range(1, result[-1][0] + 1)
        ):
            raise PersistenceFault(JournalFaultCode.INVALID_LAYOUT, "segment_sequence")
        return result

    def _load_active_segment(self, policy: SegmentPolicy) -> _SegmentDescriptor:
        segment_paths = self._segment_paths()
        if not segment_paths:
            path = self._create_segment(1, GENESIS_HEAD, rotation=False)
            return self._scan_segment(1, path, GENESIS_HEAD, policy)

        index = self._read_index()
        if index is None:
            if len(segment_paths) != 1 or segment_paths[0][0] != 1:
                raise PersistenceFault(JournalFaultCode.INVALID_LAYOUT, "index_missing")
            return self._scan_segment(1, segment_paths[0][1], GENESIS_HEAD, policy)

        if index.active_segment != index.sealed_segment + 1:
            raise PersistenceFault(
                JournalFaultCode.INVALID_LAYOUT, "index_segment_order"
            )
        by_number = dict(segment_paths)
        if index.sealed_segment not in by_number:
            raise PersistenceFault(
                JournalFaultCode.INVALID_LAYOUT, "sealed_segment_missing"
            )
        if set(by_number) - set(range(1, index.active_segment + 1)):
            raise PersistenceFault(
                JournalFaultCode.INVALID_LAYOUT, "unexpected_segment"
            )

        sealed_start = JournalHead(
            index.start_sequence - 1,
            index.previous_event_hash,
        )
        sealed = self._scan_segment(
            index.sealed_segment,
            by_number[index.sealed_segment],
            sealed_start,
            policy,
        )
        self._verify_index(index, sealed)

        active_path = by_number.get(index.active_segment)
        if active_path is None:
            raise PersistenceFault(
                JournalFaultCode.INVALID_LAYOUT,
                "active_segment_missing",
                last_committed_head=sealed.head,
            )
        return self._scan_segment(
            index.active_segment,
            active_path,
            sealed.head,
            policy,
            sealed.last_event,
        )

    def _read_index(self) -> _IndexRecord | None:
        if not self.index_path.exists():
            return None
        self._trigger(
            "index_open",
            default_code=JournalFaultCode.SEGMENT_OPEN_FAILED,
            operation="index_open",
            head=None,
        )
        try:
            raw = self.index_path.read_bytes()
        except OSError as exc:
            self._raise_os_fault(
                exc,
                JournalFaultCode.SEGMENT_OPEN_FAILED,
                "index_open",
                None,
            )
        if len(raw) > _INDEX_MAX_BYTES:
            raise PersistenceFault(JournalFaultCode.JOURNAL_CORRUPT, "index_size")
        try:
            primitive = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_pairs_without_duplicates,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PersistenceFault(
                JournalFaultCode.JOURNAL_CORRUPT, "index_decode"
            ) from exc
        try:
            canonical = canonical_json_bytes(primitive)
        except (TypeError, ValueError) as exc:
            raise PersistenceFault(
                JournalFaultCode.JOURNAL_CORRUPT, "index_value"
            ) from exc
        if type(primitive) is not dict or canonical != raw:
            raise PersistenceFault(JournalFaultCode.JOURNAL_CORRUPT, "index_canonical")
        expected_keys = {
            "active_segment",
            "byte_count",
            "event_count",
            "index_version",
            "last_event_hash",
            "last_sequence",
            "previous_event_hash",
            "sealed_segment",
            "segment_hash",
            "start_sequence",
        }
        if (
            set(primitive) != expected_keys
            or type(primitive.get("index_version")) is not int
            or primitive.get("index_version") != _INDEX_VERSION
        ):
            raise PersistenceFault(JournalFaultCode.JOURNAL_CORRUPT, "index_schema")
        integer_fields = (
            "active_segment",
            "byte_count",
            "event_count",
            "last_sequence",
            "sealed_segment",
            "start_sequence",
        )
        if any(
            type(primitive[name]) is not int or primitive[name] <= 0
            for name in integer_fields
        ):
            raise PersistenceFault(JournalFaultCode.JOURNAL_CORRUPT, "index_integer")
        hash_fields = ("last_event_hash", "previous_event_hash", "segment_hash")
        if any(
            type(primitive[name]) is not str or not _HASH_RE.fullmatch(primitive[name])
            for name in hash_fields
        ):
            raise PersistenceFault(JournalFaultCode.JOURNAL_CORRUPT, "index_hash")
        return _IndexRecord(
            sealed_segment=primitive["sealed_segment"],
            active_segment=primitive["active_segment"],
            start_sequence=primitive["start_sequence"],
            previous_event_hash=primitive["previous_event_hash"],
            last_sequence=primitive["last_sequence"],
            last_event_hash=primitive["last_event_hash"],
            event_count=primitive["event_count"],
            byte_count=primitive["byte_count"],
            segment_hash=primitive["segment_hash"],
        )

    @staticmethod
    def _verify_index(index: _IndexRecord, segment: _SegmentDescriptor) -> None:
        if segment.event_count == 0:
            raise PersistenceFault(
                JournalFaultCode.JOURNAL_CORRUPT, "sealed_segment_empty"
            )
        observed = (
            segment.number,
            segment.start_head.sequence + 1,
            segment.start_head.event_hash,
            segment.head.sequence,
            segment.head.event_hash,
            segment.event_count,
            segment.byte_count,
            segment.content_hash,
        )
        expected = (
            index.sealed_segment,
            index.start_sequence,
            index.previous_event_hash,
            index.last_sequence,
            index.last_event_hash,
            index.event_count,
            index.byte_count,
            index.segment_hash,
        )
        if observed != expected:
            raise PersistenceFault(JournalFaultCode.JOURNAL_CORRUPT, "index_mismatch")

    def _scan_segment(
        self,
        number: int,
        path: Path,
        start_head: JournalHead,
        policy: SegmentPolicy,
        start_event: JournalEvent | None = None,
    ) -> _SegmentDescriptor:
        self._trigger(
            "segment_read",
            default_code=JournalFaultCode.SEGMENT_OPEN_FAILED,
            operation="segment_read",
            head=start_head,
        )
        head = start_head
        last_event = start_event
        event_count = 0
        byte_count = 0
        digest = hashlib.sha256()
        frame_limit = min(DEFAULT_DECODE_LIMITS.max_bytes, policy.max_bytes)
        try:
            with path.open("rb") as handle:
                while frame := handle.readline(frame_limit + 1):
                    byte_count += len(frame)
                    digest.update(frame)
                    if len(frame) > frame_limit or byte_count > policy.max_bytes:
                        raise PersistenceFault(
                            JournalFaultCode.JOURNAL_CORRUPT,
                            "segment_frame_too_large",
                            last_committed_head=head,
                        )
                    if event_count >= policy.max_events:
                        raise PersistenceFault(
                            JournalFaultCode.JOURNAL_CORRUPT,
                            "segment_event_limit",
                            last_committed_head=head,
                        )
                    if not frame.endswith(b"\n"):
                        raise PersistenceFault(
                            JournalFaultCode.JOURNAL_CORRUPT,
                            "segment_incomplete_frame",
                            last_committed_head=head,
                        )
                    decoded = decode_journal_event_bytes(frame)
                    if not decoded.ok or decoded.value is None:
                        raise PersistenceFault(
                            JournalFaultCode.JOURNAL_CORRUPT,
                            "segment_decode",
                            last_committed_head=head,
                        )
                    event = decoded.value
                    if frame != event.canonical_json_bytes():
                        raise PersistenceFault(
                            JournalFaultCode.JOURNAL_CORRUPT,
                            "segment_noncanonical",
                            last_committed_head=head,
                        )
                    if event.identity.run_id != self.run_id:
                        raise PersistenceFault(
                            JournalFaultCode.JOURNAL_CORRUPT,
                            "segment_run_id",
                            last_committed_head=head,
                        )
                    if (
                        event.sequence != head.sequence + 1
                        or event.previous_event_hash != head.event_hash
                    ):
                        raise PersistenceFault(
                            JournalFaultCode.JOURNAL_CORRUPT,
                            "segment_hash_chain",
                            last_committed_head=head,
                        )
                    head = JournalHead(event.sequence, event.event_hash)
                    last_event = event
                    event_count += 1
        except PersistenceFault:
            raise
        except OSError as exc:
            self._raise_os_fault(
                exc,
                JournalFaultCode.SEGMENT_OPEN_FAILED,
                "segment_read",
                head,
            )
        return _SegmentDescriptor(
            number,
            path,
            start_head,
            head,
            event_count,
            byte_count,
            "sha256:" + digest.hexdigest(),
            last_event,
        )

    @staticmethod
    def _rotation_required(
        segment: _SegmentDescriptor,
        incoming_bytes: int,
        policy: SegmentPolicy,
    ) -> bool:
        return segment.event_count >= policy.max_events or (
            segment.event_count > 0
            and segment.byte_count + incoming_bytes > policy.max_bytes
        )

    def _rotate(
        self,
        sealed: _SegmentDescriptor,
        policy: SegmentPolicy,
    ) -> _SegmentDescriptor:
        if sealed.event_count == 0:
            raise PersistenceFault(
                JournalFaultCode.ROTATION_FAILED,
                "rotate_empty_segment",
                last_committed_head=sealed.head,
            )
        active_number = sealed.number + 1
        active_path = self._create_segment(active_number, sealed.head, rotation=True)
        self._publish_index(sealed, active_number)
        return self._scan_segment(
            active_number,
            active_path,
            sealed.head,
            policy,
            sealed.last_event,
        )

    def _create_segment(
        self,
        number: int,
        head: JournalHead,
        *,
        rotation: bool,
    ) -> Path:
        path = self.segments / f"segment-{number:08d}.jsonl"
        point = "rotation_create" if rotation else "segment_create"
        code = (
            JournalFaultCode.ROTATION_FAILED
            if rotation
            else JournalFaultCode.SEGMENT_OPEN_FAILED
        )
        self._trigger(point, default_code=code, operation=point, head=head)
        try:
            with path.open("xb", buffering=0) as handle:
                self._flush_file(handle, f"{point}_flush", head)
                self._fsync_file(handle, f"{point}_fsync", head)
        except OSError as exc:
            self._raise_os_fault(exc, code, point, head)
        self._sync_directory(self.segments, "segment_parent_sync", head)
        return path

    def _publish_index(
        self,
        sealed: _SegmentDescriptor,
        active_number: int,
    ) -> None:
        record = _IndexRecord(
            sealed_segment=sealed.number,
            active_segment=active_number,
            start_sequence=sealed.start_head.sequence + 1,
            previous_event_hash=sealed.start_head.event_hash,
            last_sequence=sealed.head.sequence,
            last_event_hash=sealed.head.event_hash,
            event_count=sealed.event_count,
            byte_count=sealed.byte_count,
            segment_hash=sealed.content_hash,
        )
        payload = canonical_json_bytes(record.to_primitive())
        temporary = self.root / f".index-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        replaced = False
        try:
            self._trigger(
                "index_open",
                default_code=JournalFaultCode.ATOMIC_PUBLISH_FAILED,
                operation="index_open",
                head=sealed.head,
            )
            with temporary.open("xb", buffering=0) as handle:
                self._write_exact(
                    handle,
                    payload,
                    point="index_write",
                    head=sealed.head,
                )
                self._flush_file(handle, "index_flush", sealed.head)
                self._fsync_file(handle, "index_fsync", sealed.head)
            self._trigger(
                "index_replace",
                default_code=JournalFaultCode.ATOMIC_PUBLISH_FAILED,
                operation="index_replace",
                head=sealed.head,
            )
            os.replace(temporary, self.index_path)
            replaced = True
            self._sync_directory(self.root, "index_parent_sync", sealed.head)
        except PersistenceFault:
            raise
        except OSError as exc:
            self._raise_os_fault(
                exc,
                JournalFaultCode.ATOMIC_PUBLISH_FAILED,
                "index_publish",
                sealed.head,
            )
        finally:
            if not replaced:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _append_frame(
        self,
        path: Path,
        frame: bytes,
        head: JournalHead,
    ) -> None:
        self._trigger(
            "segment_open",
            default_code=JournalFaultCode.SEGMENT_OPEN_FAILED,
            operation="segment_open",
            head=head,
        )
        try:
            with path.open("ab", buffering=0) as handle:
                self._write_exact(handle, frame, point="segment_write", head=head)
                self._flush_file(handle, "segment_flush", head)
                self._fsync_file(handle, "segment_fsync", head)
        except PersistenceFault:
            raise
        except OSError as exc:
            self._raise_os_fault(
                exc,
                JournalFaultCode.SEGMENT_OPEN_FAILED,
                "segment_append",
                head,
            )

    def _write_exact(
        self,
        handle: BinaryIO,
        payload: bytes,
        *,
        point: str,
        head: JournalHead,
    ) -> None:
        limit = self._trigger(
            point,
            requested_bytes=len(payload),
            default_code=JournalFaultCode.SHORT_WRITE,
            operation=point,
            head=head,
        )
        attempted = payload if limit is None else payload[:limit]
        try:
            written = handle.write(attempted)
        except OSError as exc:
            self._raise_os_fault(exc, JournalFaultCode.WRITE_FAILED, point, head)
        if written != len(payload):
            raise PersistenceFault(
                JournalFaultCode.SHORT_WRITE,
                point,
                last_committed_head=head,
            )

    def _durabilize_observed_head(self, path: Path, head: JournalHead) -> None:
        self._trigger(
            "observed_head_open",
            default_code=JournalFaultCode.SEGMENT_OPEN_FAILED,
            operation="observed_head_open",
            head=head,
        )
        try:
            handle = path.open("r+b", buffering=0)
        except OSError as exc:
            self._raise_os_fault(
                exc,
                JournalFaultCode.SEGMENT_OPEN_FAILED,
                "observed_head_open",
                head,
            )
        with handle:
            self._fsync_file(handle, "observed_head_fsync", head)

    def _flush_file(
        self,
        handle: BinaryIO,
        point: str,
        head: JournalHead | None,
    ) -> None:
        self._trigger(
            point,
            default_code=JournalFaultCode.FLUSH_FAILED,
            operation=point,
            head=head,
        )
        try:
            handle.flush()
        except OSError as exc:
            self._raise_os_fault(exc, JournalFaultCode.FLUSH_FAILED, point, head)

    def _fsync_file(
        self,
        handle: BinaryIO,
        point: str,
        head: JournalHead | None,
    ) -> None:
        self._trigger(
            point,
            default_code=JournalFaultCode.FSYNC_FAILED,
            operation=point,
            head=head,
        )
        try:
            os.fsync(handle.fileno())
        except OSError as exc:
            self._raise_os_fault(exc, JournalFaultCode.FSYNC_FAILED, point, head)

    def _sync_directory(
        self,
        path: Path,
        point: str,
        head: JournalHead | None,
    ) -> None:
        self._trigger(
            point,
            default_code=JournalFaultCode.DIRECTORY_SYNC_FAILED,
            operation=point,
            head=head,
        )
        try:
            if os.name == "nt":
                self._sync_windows_directory(path)
            else:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                descriptor = os.open(path, flags)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as exc:
            self._raise_os_fault(
                exc,
                JournalFaultCode.DIRECTORY_SYNC_FAILED,
                point,
                head,
            )

    @staticmethod
    def _sync_windows_directory(path: Path) -> None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = (wintypes.HANDLE,)
        flush_file_buffers.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        generic_write = 0x40000000
        share_all = 0x00000001 | 0x00000002 | 0x00000004
        open_existing = 3
        backup_semantics = 0x02000000
        invalid_handle = ctypes.c_void_p(-1).value
        handle = create_file(
            str(path),
            generic_write,
            share_all,
            None,
            open_existing,
            backup_semantics,
            None,
        )
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not flush_file_buffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close_handle(handle)


__all__ = ["FilesystemJournalStorage", "StorageFailpoint"]
