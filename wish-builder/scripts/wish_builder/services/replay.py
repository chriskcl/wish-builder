"""Bounded-memory replay and recovery for canonical segmented Journal JSONL."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from wish_builder.contracts import (
    DEFAULT_DECODE_LIMITS,
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeReasonCode,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
    canonical_json_bytes,
    decode_journal_event_bytes,
)
from wish_builder.contracts.execution import (
    ExecutionManifestModel,
    is_execution_manifest_model,
)
from wish_builder.contracts.models import HASH_RE
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.graph_index import GraphIndex, GraphIndexError
from wish_builder.kernel.state import ApplyReason, KernelSnapshot, apply_journal_event

from .checkpoints import (
    CheckpointFault,
    CheckpointLoadStatus,
    CheckpointPersistenceFault,
    CheckpointPolicy,
    CheckpointStore,
    JournalPosition,
    VerifiedCheckpoint,
)
from .journal import GENESIS_HEAD, JournalHead

_SEGMENT_RE = re.compile(r"segment-([0-9]{8})\.jsonl\Z")
_INDEX_MAX_BYTES = 16 * 1024
_INDEX_VERSION = 1
_DEFAULT_CHECKPOINT_POLICY = CheckpointPolicy()
_MIN_SIGNED_64 = -(2**63)
_MAX_SIGNED_64 = 2**63 - 1
_EVENT_FIELDS = frozenset(
    {
        "actor_id",
        "actor_type",
        "attempt",
        "coordinator_epoch",
        "correlation_id",
        "event_hash",
        "event_id",
        "event_type",
        "event_version",
        "payload",
        "payload_hash",
        "previous_event_hash",
        "reason_code",
        "recorded_at",
        "run_id",
        "sequence",
        "task_id",
    }
)
_TRANSITION_FIELDS = frozenset(
    {"evidence", "from_state", "payload_type", "subject", "to_state"}
)
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }
)


class ReplayStatus(StrEnum):
    RECOVERED = "recovered"
    BLOCKED = "blocked"


class ReplayFaultCode(StrEnum):
    CONTROL_ROOT_DRIFT = "control_root_drift"
    INVALID_LAYOUT = "invalid_layout"
    SEGMENT_IO_FAILED = "segment_io_failed"
    SEGMENT_REPLACED = "segment_replaced"
    FRAME_TOO_LARGE = "frame_too_large"
    MID_SEGMENT_INCOMPLETE = "mid_segment_incomplete"
    EVENT_DECODE_FAILED = "event_decode_failed"
    EVENT_NONCANONICAL = "event_noncanonical"
    RUN_MISMATCH = "run_mismatch"
    SEQUENCE_MISMATCH = "sequence_mismatch"
    HASH_CHAIN_MISMATCH = "hash_chain_mismatch"
    STATE_REJECTED = "state_rejected"
    GRAPH_INDEX_MISMATCH = "graph_index_mismatch"
    JOURNAL_TRUNCATED = "journal_truncated"
    QUARANTINE_FAILED = "quarantine_failed"
    DERIVED_PUBLISH_FAILED = "derived_publish_failed"


@dataclass(frozen=True, slots=True)
class ReplayFault:
    code: ReplayFaultCode
    detail: str
    segment: int | None
    byte_offset: int | None
    previous_event_hash: str
    raw_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not ReplayFaultCode:
            raise TypeError("code must be a ReplayFaultCode")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("detail must be non-empty")
        if self.segment is not None and (
            type(self.segment) is not int or self.segment <= 0
        ):
            raise ValueError("segment must be positive or null")
        if self.byte_offset is not None and (
            type(self.byte_offset) is not int or self.byte_offset < 0
        ):
            raise ValueError("byte_offset must be non-negative or null")
        if type(self.previous_event_hash) is not str or not HASH_RE.fullmatch(
            self.previous_event_hash
        ):
            raise ValueError("previous_event_hash must be a full sha256 reference")
        if self.raw_sha256 is not None and (
            type(self.raw_sha256) is not str or not HASH_RE.fullmatch(self.raw_sha256)
        ):
            raise ValueError("raw_sha256 must be a full sha256 reference or null")


@dataclass(frozen=True, slots=True)
class QuarantinedTail:
    segment: int
    byte_offset: int
    byte_count: int
    raw_sha256: str
    quarantine_path: str
    reason: str = "uncommitted_tail"

    def __post_init__(self) -> None:
        if type(self.segment) is not int or self.segment <= 0:
            raise ValueError("segment must be positive")
        if type(self.byte_offset) is not int or self.byte_offset < 0:
            raise ValueError("byte_offset must be non-negative")
        if type(self.byte_count) is not int or self.byte_count <= 0:
            raise ValueError("byte_count must be positive")
        if type(self.raw_sha256) is not str or not HASH_RE.fullmatch(self.raw_sha256):
            raise ValueError("raw_sha256 must be a full sha256 reference")
        if type(self.quarantine_path) is not str or not self.quarantine_path:
            raise ValueError("quarantine_path must be non-empty")
        if self.reason != "uncommitted_tail":
            raise ValueError("reason must be uncommitted_tail")


@dataclass(frozen=True, slots=True)
class DerivedDataFault:
    source: str
    detail: str
    raw_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.source) is not str or not self.source:
            raise ValueError("source must be non-empty")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("detail must be non-empty")
        if self.raw_sha256 is not None and (
            type(self.raw_sha256) is not str or not HASH_RE.fullmatch(self.raw_sha256)
        ):
            raise ValueError("raw_sha256 must be a full sha256 reference or null")


@dataclass(frozen=True, slots=True)
class ReplayResult:
    status: ReplayStatus
    snapshot: KernelSnapshot
    graph_index: GraphIndex
    head: JournalHead
    journal_position: JournalPosition
    events_replayed: int
    max_frame_bytes: int
    checkpoint_used: bool
    quarantined_tail: QuarantinedTail | None = None
    fault: ReplayFault | None = None
    derived_faults: tuple[DerivedDataFault, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not ReplayStatus:
            raise TypeError("status must be a ReplayStatus")
        if type(self.snapshot) is not KernelSnapshot:
            raise TypeError("snapshot must be a KernelSnapshot")
        if type(self.graph_index) is not GraphIndex:
            raise TypeError("graph_index must be a GraphIndex")
        if type(self.head) is not JournalHead:
            raise TypeError("head must be a JournalHead")
        if type(self.journal_position) is not JournalPosition:
            raise TypeError("journal_position must be a JournalPosition")
        if type(self.events_replayed) is not int or self.events_replayed < 0:
            raise ValueError("events_replayed must be non-negative")
        if type(self.max_frame_bytes) is not int or self.max_frame_bytes < 0:
            raise ValueError("max_frame_bytes must be non-negative")
        if type(self.checkpoint_used) is not bool:
            raise TypeError("checkpoint_used must be a bool")
        if self.quarantined_tail is not None and (
            type(self.quarantined_tail) is not QuarantinedTail
        ):
            raise TypeError("quarantined_tail must be a QuarantinedTail or null")
        if type(self.derived_faults) is not tuple or not all(
            type(fault) is DerivedDataFault for fault in self.derived_faults
        ):
            raise TypeError("derived_faults must be a tuple of DerivedDataFault values")
        if self.status is ReplayStatus.BLOCKED:
            if type(self.fault) is not ReplayFault:
                raise ValueError("a blocked replay requires a fault")
        elif self.fault is not None:
            raise ValueError("a recovered replay cannot contain a blocking fault")


@dataclass(frozen=True, slots=True)
class _SegmentDescriptor:
    number: int
    start_head: JournalHead
    head: JournalHead
    event_count: int
    byte_count: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class _AttemptResult:
    result: ReplayResult
    descriptors: tuple[_SegmentDescriptor, ...]
    last_event_type: JournalEventType | None
    pointer_anchor_seen: bool


class _SegmentReplaced(RuntimeError):
    pass


class _FastDecodeFallback(ValueError):
    pass


def _decode_replay_event(raw: bytes) -> tuple[JournalEvent | None, bool]:
    """Decode common transitions once, retaining the public decoder as fallback."""

    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_number,
            parse_float=_reject_json_number,
            parse_int=_signed_64_json_integer,
        )
        if type(value) is not dict or set(value) != _EVENT_FIELDS:
            raise _FastDecodeFallback
        payload_value = value["payload"]
        if (
            type(payload_value) is not dict
            or set(payload_value) != _TRANSITION_FIELDS
            or payload_value.get("payload_type") != "transition"
            or type(payload_value.get("evidence")) is not list
            or payload_value["evidence"]
        ):
            raise _FastDecodeFallback
        _validate_replay_json_shape(value)
        canonical = raw == canonical_json_bytes(value)
        reason_value = value["reason_code"]
        event = JournalEvent(
            event_version=value["event_version"],
            sequence=value["sequence"],
            event_id=value["event_id"],
            event_type=JournalEventType(value["event_type"]),
            identity=ExecutionIdentity(
                value["run_id"],
                value["coordinator_epoch"],
                value["task_id"],
                value["attempt"],
                value["correlation_id"],
            ),
            actor_type=ActorType(value["actor_type"]),
            actor_id=value["actor_id"],
            recorded_at=value["recorded_at"],
            reason_code=(
                None if reason_value is None else RuntimeReasonCode(reason_value)
            ),
            previous_event_hash=value["previous_event_hash"],
            payload_hash=value["payload_hash"],
            payload=TransitionPayload(
                TransitionSubject(payload_value["subject"]),
                RuntimeState(payload_value["from_state"]),
                RuntimeState(payload_value["to_state"]),
            ),
            event_hash=value["event_hash"],
        )
        return event, canonical
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        decoded = decode_journal_event_bytes(raw)
        if not decoded.ok or decoded.value is None:
            return None, False
        event = decoded.value
        return event, raw == event.canonical_json_bytes()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _FastDecodeFallback("duplicate object key")
        result[key] = value
    return result


def _reject_json_number(token: str) -> object:
    raise _FastDecodeFallback(f"unsupported JSON number: {token}")


def _signed_64_json_integer(token: str) -> int:
    negative = token.startswith("-")
    digits = (token[1:] if negative else token).lstrip("0") or "0"
    boundary = "9223372036854775808" if negative else "9223372036854775807"
    if len(digits) > len(boundary) or (
        len(digits) == len(boundary) and digits > boundary
    ):
        raise _FastDecodeFallback("JSON integer is outside signed 64-bit range")
    value = int(token)
    if not _MIN_SIGNED_64 <= value <= _MAX_SIGNED_64:
        raise _FastDecodeFallback("JSON integer is outside signed 64-bit range")
    return value


def _validate_replay_json_shape(value: object) -> None:
    limits = DEFAULT_DECODE_LIMITS
    item_count = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, parent_depth = stack.pop()
        if current is None or type(current) in (bool, int):
            continue
        if type(current) is str:
            _validate_replay_json_string(current)
            continue
        if type(current) not in (dict, list):
            raise _FastDecodeFallback("unsupported JSON shape")
        depth = parent_depth + 1
        if depth > limits.max_depth:
            raise _FastDecodeFallback("JSON depth limit exceeded")
        item_count += len(current)
        if item_count > limits.max_items:
            raise _FastDecodeFallback("JSON item limit exceeded")
        if type(current) is list:
            stack.extend((item, depth) for item in current)
            continue
        for key, item in current.items():
            _validate_replay_json_string(key)
            stack.append((item, depth))


def _validate_replay_json_string(value: str) -> None:
    if len(value) > DEFAULT_DECODE_LIMITS.max_string_length:
        raise _FastDecodeFallback("JSON string limit exceeded")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _FastDecodeFallback("invalid Unicode scalar") from exc
    for character in value:
        codepoint = ord(character)
        if (
            (codepoint < 0x20 and character not in {"\t", "\n"})
            or 0x7F <= codepoint <= 0x9F
            or codepoint in _BIDI_CONTROL_CODEPOINTS
        ):
            raise _FastDecodeFallback("disallowed contract control")


def replay_journal(
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
) -> ReplayResult:
    """Recover canonical state without retaining a Journal event history."""

    if not is_execution_manifest_model(manifest):
        raise TypeError("manifest must be an ExecutionManifest model")
    if type(coordinator_epoch) is not int or coordinator_epoch <= 0:
        raise ValueError("coordinator_epoch must be positive")
    if type(checkpoint_policy) is not CheckpointPolicy:
        raise TypeError("checkpoint_policy must be a CheckpointPolicy")
    if checkpoint_store is not None and type(checkpoint_store) is not CheckpointStore:
        raise TypeError("checkpoint_store must be a CheckpointStore or null")
    if type(repair_derived) is not bool:
        raise TypeError("repair_derived must be a bool")
    if control_root_validator is not None and expected_control_root is not None:
        raise ValueError(
            "provide control_root_validator or expected_control_root, not both"
        )
    validator = control_root_validator or (
        None
        if expected_control_root is None
        else _filesystem_identity_validator(expected_control_root)
    )
    root = Path(journal_root)
    store = checkpoint_store or CheckpointStore(
        root.parent / "checkpoints",
        control_root_validator=validator,
    )
    derived_faults: list[DerivedDataFault] = []
    if not _control_root_valid(validator):
        initial, graph = _initial_state(manifest, coordinator_epoch)
        return _blocked(
            initial,
            graph,
            GENESIS_HEAD,
            JournalPosition(1, 0),
            ReplayFaultCode.CONTROL_ROOT_DRIFT,
            "control_root_drift",
            None,
            None,
            0,
            0,
            False,
            (),
        )
    load = store.load(manifest, coordinator_epoch=coordinator_epoch)
    checkpoint = load.checkpoint if load.status is CheckpointLoadStatus.LOADED else None
    if load.status is CheckpointLoadStatus.DISCARDED and load.fault is not None:
        derived_faults.append(_checkpoint_derived_fault(load.fault))

    try:
        segments = _segment_paths(root)
    except (OSError, ValueError) as exc:
        initial, graph = _initial_state(manifest, coordinator_epoch)
        return _blocked(
            initial,
            graph,
            GENESIS_HEAD,
            JournalPosition(1, 0),
            ReplayFaultCode.INVALID_LAYOUT,
            _detail(exc),
            None,
            None,
            0,
            0,
            False,
            tuple(derived_faults),
        )

    index_valid, index_detail = _journal_index_status(root, segments, manifest.run_id)
    if not index_valid:
        derived_faults.append(DerivedDataFault("journal_index", index_detail))

    use_checkpoint = checkpoint is not None and index_valid
    if use_checkpoint and not _checkpoint_position_valid(segments, checkpoint):
        derived_faults.append(
            DerivedDataFault("checkpoint", "journal_position_invalid")
        )
        use_checkpoint = False

    attempt = _replay_attempt(
        root,
        segments,
        manifest,
        coordinator_epoch,
        checkpoint if use_checkpoint else None,
        tuple(derived_faults),
        pointer_checkpoint=None if use_checkpoint else checkpoint,
        control_root_validator=validator,
    )
    if use_checkpoint and attempt.result.status is ReplayStatus.BLOCKED:
        derived_faults.append(
            DerivedDataFault("checkpoint", "tail_verification_fell_back_to_genesis")
        )
        attempt = _replay_attempt(
            root,
            segments,
            manifest,
            coordinator_epoch,
            None,
            tuple(derived_faults),
            pointer_checkpoint=checkpoint,
            control_root_validator=validator,
        )
    if attempt.result.status is ReplayStatus.BLOCKED:
        return attempt.result

    if checkpoint is not None and not attempt.pointer_anchor_seen:
        return _blocked_from_result(
            attempt.result,
            ReplayFaultCode.JOURNAL_TRUNCATED,
            "checkpoint pointer names a journal event that is no longer present",
        )

    result = attempt.result
    if repair_derived and not index_valid:
        try:
            if not _control_root_valid(validator):
                raise ValueError("control_root_drift")
            if attempt.descriptors:
                _rebuild_journal_index(root, attempt.descriptors)
            elif (root / "index.json").exists():
                _quarantine_derived_index(root)
        except (OSError, ValueError) as exc:
            return _blocked_from_result(
                result,
                ReplayFaultCode.DERIVED_PUBLISH_FAILED,
                f"journal_index:{_detail(exc)}",
            )

    previous_checkpoint_sequence = (
        0 if checkpoint is None else checkpoint.snapshot.last_sequence
    )
    rebuild_checkpoint = load.status is CheckpointLoadStatus.DISCARDED
    should_publish = checkpoint_policy.should_publish(
        previous_sequence=previous_checkpoint_sequence,
        current_sequence=result.snapshot.last_sequence,
        last_event_type=attempt.last_event_type,
        elapsed_seconds=elapsed_since_checkpoint_seconds,
    )
    if (
        repair_derived
        and result.snapshot.last_sequence > 0
        and (rebuild_checkpoint or should_publish)
    ):
        try:
            store.publish(
                manifest,
                result.snapshot,
                result.graph_index,
                result.journal_position,
            )
        except (CheckpointPersistenceFault, OSError, TypeError, ValueError) as exc:
            return _blocked_from_result(
                result,
                ReplayFaultCode.DERIVED_PUBLISH_FAILED,
                f"checkpoint:{_detail(exc)}",
            )
    return result


def _replay_attempt(
    root: Path,
    segments: tuple[tuple[int, Path], ...],
    manifest: ExecutionManifestModel,
    coordinator_epoch: int,
    checkpoint: VerifiedCheckpoint | None,
    derived_faults: tuple[DerivedDataFault, ...],
    *,
    pointer_checkpoint: VerifiedCheckpoint | None = None,
    control_root_validator: Callable[[], bool] | None = None,
) -> _AttemptResult:
    if checkpoint is None:
        snapshot, graph = _initial_state(manifest, coordinator_epoch)
        start = JournalPosition(1, 0)
        checkpoint_used = False
    else:
        snapshot = checkpoint.snapshot
        graph = checkpoint.graph_index
        start = checkpoint.journal_position
        checkpoint_used = True
    pointer = checkpoint or pointer_checkpoint
    pointer_anchor_seen = pointer is None or (
        pointer.journal_through_sequence == snapshot.last_sequence
        and pointer.journal_through_event_hash == snapshot.last_event_hash
    )
    events_replayed = 0
    max_frame_bytes = 0
    descriptors: list[_SegmentDescriptor] = []
    last_event_type: JournalEventType | None = None
    final_position = start

    if not segments:
        if pointer is not None and pointer.journal_through_sequence > 0:
            result = _blocked(
                snapshot,
                graph,
                JournalHead(snapshot.last_sequence, snapshot.last_event_hash),
                JournalPosition(1, 0),
                ReplayFaultCode.JOURNAL_TRUNCATED,
                "checkpoint exists but the authoritative journal is absent",
                None,
                None,
                0,
                0,
                checkpoint_used,
                derived_faults,
            )
            return _AttemptResult(result, (), None, False)
        result = ReplayResult(
            ReplayStatus.RECOVERED,
            snapshot,
            graph,
            JournalHead(snapshot.last_sequence, snapshot.last_event_hash),
            JournalPosition(1, 0),
            0,
            0,
            checkpoint_used,
            derived_faults=derived_faults,
        )
        return _AttemptResult(result, (), None, pointer_anchor_seen)

    last_segment = segments[-1][0]
    for number, path in segments:
        if number < start.segment:
            continue
        offset = start.offset if number == start.segment else 0
        segment_start = JournalHead(snapshot.last_sequence, snapshot.last_event_hash)
        event_count = 0
        byte_count = 0
        digest = hashlib.sha256()
        if not _control_root_valid(control_root_validator):
            result = _blocked(
                snapshot,
                graph,
                JournalHead(snapshot.last_sequence, snapshot.last_event_hash),
                JournalPosition(number, offset),
                ReplayFaultCode.CONTROL_ROOT_DRIFT,
                "control_root_drift",
                number,
                offset,
                events_replayed,
                max_frame_bytes,
                checkpoint_used,
                derived_faults,
            )
            return _AttemptResult(
                result,
                tuple(descriptors),
                last_event_type,
                pointer_anchor_seen,
            )
        try:
            link_stat = os.lstat(path)
            if (
                not stat.S_ISREG(link_stat.st_mode)
                or _is_link_or_junction(path)
                or link_stat.st_nlink != 1
            ):
                raise _SegmentReplaced("segment is not a protected regular file")
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (
                    link_stat.st_dev,
                    link_stat.st_ino,
                ):
                    raise _SegmentReplaced("segment identity changed before read")
                opened_size = opened.st_size
                handle.seek(offset)
                while True:
                    frame_offset = handle.tell()
                    frame = handle.readline(DEFAULT_DECODE_LIMITS.max_bytes + 1)
                    if not frame:
                        break
                    max_frame_bytes = max(max_frame_bytes, len(frame))
                    if len(frame) > DEFAULT_DECODE_LIMITS.max_bytes:
                        return _failed_attempt(
                            snapshot,
                            graph,
                            number,
                            frame_offset,
                            frame,
                            ReplayFaultCode.FRAME_TOO_LARGE,
                            "frame exceeds the strict decoder byte limit",
                            events_replayed,
                            max_frame_bytes,
                            checkpoint_used,
                            derived_faults,
                            descriptors,
                            last_event_type,
                            pointer_anchor_seen,
                        )
                    if not frame.endswith(b"\n"):
                        if number != last_segment:
                            return _failed_attempt(
                                snapshot,
                                graph,
                                number,
                                frame_offset,
                                frame,
                                ReplayFaultCode.MID_SEGMENT_INCOMPLETE,
                                "a non-final segment ends without a commit delimiter",
                                events_replayed,
                                max_frame_bytes,
                                checkpoint_used,
                                derived_faults,
                                descriptors,
                                last_event_type,
                                pointer_anchor_seen,
                            )
                        try:
                            quarantined = _quarantine_tail(
                                root,
                                path,
                                number,
                                frame_offset,
                                frame,
                                control_root_validator,
                            )
                        except (OSError, ValueError) as exc:
                            return _failed_attempt(
                                snapshot,
                                graph,
                                number,
                                frame_offset,
                                frame,
                                ReplayFaultCode.QUARANTINE_FAILED,
                                _detail(exc),
                                events_replayed,
                                max_frame_bytes,
                                checkpoint_used,
                                derived_faults,
                                descriptors,
                                last_event_type,
                                pointer_anchor_seen,
                            )
                        if not graph.verify(manifest, snapshot):
                            return _failed_attempt(
                                snapshot,
                                graph,
                                number,
                                frame_offset,
                                frame,
                                ReplayFaultCode.GRAPH_INDEX_MISMATCH,
                                "incremental index differs from a verified rebuild",
                                events_replayed,
                                max_frame_bytes,
                                checkpoint_used,
                                derived_faults,
                                descriptors,
                                last_event_type,
                                pointer_anchor_seen,
                            )
                        final_position = JournalPosition(number, frame_offset)
                        result = ReplayResult(
                            ReplayStatus.RECOVERED,
                            snapshot,
                            graph,
                            JournalHead(
                                snapshot.last_sequence, snapshot.last_event_hash
                            ),
                            final_position,
                            events_replayed,
                            max_frame_bytes,
                            checkpoint_used,
                            quarantined_tail=quarantined,
                            derived_faults=derived_faults,
                        )
                        if not checkpoint_used:
                            descriptors.append(
                                _SegmentDescriptor(
                                    number,
                                    segment_start,
                                    JournalHead(
                                        snapshot.last_sequence, snapshot.last_event_hash
                                    ),
                                    event_count,
                                    byte_count,
                                    "sha256:" + digest.hexdigest(),
                                )
                            )
                        return _AttemptResult(
                            result,
                            tuple(descriptors),
                            last_event_type,
                            pointer_anchor_seen,
                        )
                    event, canonical = _decode_replay_event(frame)
                    if event is None:
                        return _failed_attempt(
                            snapshot,
                            graph,
                            number,
                            frame_offset,
                            frame,
                            ReplayFaultCode.EVENT_DECODE_FAILED,
                            "complete frame failed strict event decoding",
                            events_replayed,
                            max_frame_bytes,
                            checkpoint_used,
                            derived_faults,
                            descriptors,
                            last_event_type,
                            pointer_anchor_seen,
                        )
                    if not canonical:
                        return _failed_attempt(
                            snapshot,
                            graph,
                            number,
                            frame_offset,
                            frame,
                            ReplayFaultCode.EVENT_NONCANONICAL,
                            "complete event bytes are not canonical JSONL",
                            events_replayed,
                            max_frame_bytes,
                            checkpoint_used,
                            derived_faults,
                            descriptors,
                            last_event_type,
                            pointer_anchor_seen,
                        )
                    if event.identity.run_id != manifest.run_id:
                        return _failed_attempt(
                            snapshot,
                            graph,
                            number,
                            frame_offset,
                            frame,
                            ReplayFaultCode.RUN_MISMATCH,
                            "event run_id differs from the approved manifest",
                            events_replayed,
                            max_frame_bytes,
                            checkpoint_used,
                            derived_faults,
                            descriptors,
                            last_event_type,
                            pointer_anchor_seen,
                        )
                    if event.sequence != snapshot.last_sequence + 1:
                        return _failed_attempt(
                            snapshot,
                            graph,
                            number,
                            frame_offset,
                            frame,
                            ReplayFaultCode.SEQUENCE_MISMATCH,
                            "event sequence is not contiguous",
                            events_replayed,
                            max_frame_bytes,
                            checkpoint_used,
                            derived_faults,
                            descriptors,
                            last_event_type,
                            pointer_anchor_seen,
                        )
                    if event.previous_event_hash != snapshot.last_event_hash:
                        return _failed_attempt(
                            snapshot,
                            graph,
                            number,
                            frame_offset,
                            frame,
                            ReplayFaultCode.HASH_CHAIN_MISMATCH,
                            "event previous hash breaks the journal chain",
                            events_replayed,
                            max_frame_bytes,
                            checkpoint_used,
                            derived_faults,
                            descriptors,
                            last_event_type,
                            pointer_anchor_seen,
                        )
                    previous = snapshot
                    applied = apply_journal_event(previous, event)
                    if not applied.accepted:
                        if applied.reason is ApplyReason.UNSUPPORTED_EVENT:
                            snapshot = replace(
                                previous,
                                last_sequence=event.sequence,
                                last_event_id=event.event_id,
                                last_event_hash=event.event_hash,
                            )
                        else:
                            return _failed_attempt(
                                snapshot,
                                graph,
                                number,
                                frame_offset,
                                frame,
                                ReplayFaultCode.STATE_REJECTED,
                                applied.reason.value,
                                events_replayed,
                                max_frame_bytes,
                                checkpoint_used,
                                derived_faults,
                                descriptors,
                                last_event_type,
                                pointer_anchor_seen,
                            )
                    else:
                        snapshot = applied.snapshot
                    try:
                        graph = graph.advance(previous, snapshot)
                    except (GraphIndexError, TypeError, ValueError) as exc:
                        return _failed_attempt(
                            previous,
                            graph,
                            number,
                            frame_offset,
                            frame,
                            ReplayFaultCode.GRAPH_INDEX_MISMATCH,
                            _detail(exc),
                            events_replayed,
                            max_frame_bytes,
                            checkpoint_used,
                            derived_faults,
                            descriptors,
                            last_event_type,
                            pointer_anchor_seen,
                        )
                    digest.update(frame)
                    byte_count += len(frame)
                    event_count += 1
                    events_replayed += 1
                    last_event_type = event.event_type
                    if (
                        pointer is not None
                        and event.sequence == pointer.journal_through_sequence
                    ):
                        if event.event_hash != pointer.journal_through_event_hash:
                            return _failed_attempt(
                                snapshot,
                                graph,
                                number,
                                frame_offset,
                                frame,
                                ReplayFaultCode.HASH_CHAIN_MISMATCH,
                                "pointer journal hash does not match the authoritative event",
                                events_replayed,
                                max_frame_bytes,
                                checkpoint_used,
                                derived_faults,
                                descriptors,
                                last_event_type,
                                False,
                            )
                        pointer_anchor_seen = True
                observed = os.fstat(handle.fileno())
                if (observed.st_dev, observed.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ) or observed.st_size != opened_size:
                    raise _SegmentReplaced("segment changed during replay")
                final_position = JournalPosition(number, handle.tell())
        except _SegmentReplaced as exc:
            return _failed_attempt(
                snapshot,
                graph,
                number,
                offset,
                b"",
                ReplayFaultCode.SEGMENT_REPLACED,
                _detail(exc),
                events_replayed,
                max_frame_bytes,
                checkpoint_used,
                derived_faults,
                descriptors,
                last_event_type,
                pointer_anchor_seen,
            )
        except OSError as exc:
            return _failed_attempt(
                snapshot,
                graph,
                number,
                offset,
                b"",
                ReplayFaultCode.SEGMENT_IO_FAILED,
                _detail(exc),
                events_replayed,
                max_frame_bytes,
                checkpoint_used,
                derived_faults,
                descriptors,
                last_event_type,
                pointer_anchor_seen,
            )
        if number != last_segment and offset == 0 and event_count == 0:
            return _failed_attempt(
                snapshot,
                graph,
                number,
                0,
                b"",
                ReplayFaultCode.INVALID_LAYOUT,
                "a sealed segment cannot be empty",
                events_replayed,
                max_frame_bytes,
                checkpoint_used,
                derived_faults,
                descriptors,
                last_event_type,
                pointer_anchor_seen,
            )
        if not checkpoint_used:
            descriptors.append(
                _SegmentDescriptor(
                    number,
                    segment_start,
                    JournalHead(snapshot.last_sequence, snapshot.last_event_hash),
                    event_count,
                    byte_count,
                    "sha256:" + digest.hexdigest(),
                )
            )

    if not graph.verify(manifest, snapshot):
        result = _blocked(
            snapshot,
            graph,
            JournalHead(snapshot.last_sequence, snapshot.last_event_hash),
            final_position,
            ReplayFaultCode.GRAPH_INDEX_MISMATCH,
            "incremental index differs from a verified rebuild",
            None,
            None,
            events_replayed,
            max_frame_bytes,
            checkpoint_used,
            derived_faults,
        )
        return _AttemptResult(
            result,
            tuple(descriptors),
            last_event_type,
            pointer_anchor_seen,
        )
    if not _control_root_valid(control_root_validator):
        result = _blocked(
            snapshot,
            graph,
            JournalHead(snapshot.last_sequence, snapshot.last_event_hash),
            final_position,
            ReplayFaultCode.CONTROL_ROOT_DRIFT,
            "control_root_drift",
            None,
            None,
            events_replayed,
            max_frame_bytes,
            checkpoint_used,
            derived_faults,
        )
        return _AttemptResult(
            result,
            tuple(descriptors),
            last_event_type,
            pointer_anchor_seen,
        )
    head = JournalHead(snapshot.last_sequence, snapshot.last_event_hash)
    result = ReplayResult(
        ReplayStatus.RECOVERED,
        snapshot,
        graph,
        head,
        final_position,
        events_replayed,
        max_frame_bytes,
        checkpoint_used,
        derived_faults=derived_faults,
    )
    return _AttemptResult(
        result,
        tuple(descriptors),
        last_event_type,
        pointer_anchor_seen,
    )


def _failed_attempt(
    snapshot: KernelSnapshot,
    graph: GraphIndex,
    segment: int,
    offset: int,
    raw: bytes,
    code: ReplayFaultCode,
    detail: str,
    events_replayed: int,
    max_frame_bytes: int,
    checkpoint_used: bool,
    derived_faults: tuple[DerivedDataFault, ...],
    descriptors: list[_SegmentDescriptor],
    last_event_type: JournalEventType | None,
    pointer_anchor_seen: bool,
) -> _AttemptResult:
    position = JournalPosition(segment, offset)
    result = _blocked(
        snapshot,
        graph,
        JournalHead(snapshot.last_sequence, snapshot.last_event_hash),
        position,
        code,
        detail,
        segment,
        offset,
        events_replayed,
        max_frame_bytes,
        checkpoint_used,
        derived_faults,
        raw,
    )
    return _AttemptResult(
        result,
        tuple(descriptors),
        last_event_type,
        pointer_anchor_seen,
    )


def _blocked(
    snapshot: KernelSnapshot,
    graph: GraphIndex,
    head: JournalHead,
    position: JournalPosition,
    code: ReplayFaultCode,
    detail: str,
    segment: int | None,
    offset: int | None,
    events_replayed: int,
    max_frame_bytes: int,
    checkpoint_used: bool,
    derived_faults: tuple[DerivedDataFault, ...],
    raw: bytes = b"",
) -> ReplayResult:
    return ReplayResult(
        ReplayStatus.BLOCKED,
        snapshot,
        graph,
        head,
        position,
        events_replayed,
        max_frame_bytes,
        checkpoint_used,
        fault=ReplayFault(
            code,
            detail,
            segment,
            offset,
            snapshot.last_event_hash,
            None if not raw else _sha256_bytes(raw),
        ),
        derived_faults=derived_faults,
    )


def _blocked_from_result(
    result: ReplayResult,
    code: ReplayFaultCode,
    detail: str,
) -> ReplayResult:
    return _blocked(
        result.snapshot,
        result.graph_index,
        result.head,
        result.journal_position,
        code,
        detail,
        None,
        None,
        result.events_replayed,
        result.max_frame_bytes,
        result.checkpoint_used,
        result.derived_faults,
    )


def _initial_state(
    manifest: ExecutionManifestModel,
    coordinator_epoch: int,
) -> tuple[KernelSnapshot, GraphIndex]:
    dag = TaskDag.compile(manifest)
    snapshot = KernelSnapshot.initial(manifest.run_id, coordinator_epoch, dag)
    return snapshot, GraphIndex.rebuild(manifest, snapshot)


def _segment_paths(root: Path) -> tuple[tuple[int, Path], ...]:
    directory = root / "segments"
    if not directory.exists():
        return ()
    if _is_link_or_junction(directory) or not directory.is_dir():
        raise ValueError("segments_directory_replaced")
    entries = list(directory.iterdir())
    for path in entries:
        if _SEGMENT_RE.fullmatch(path.name) is not None and _is_link_or_junction(path):
            raise ValueError("segment_link_or_reparse_point")
    unexpected = [
        path.name
        for path in entries
        if path.is_file()
        and path.suffix == ".jsonl"
        and _SEGMENT_RE.fullmatch(path.name) is None
    ]
    if unexpected:
        raise ValueError("unexpected_segment_name")
    result = sorted(
        (int(match.group(1)), path)
        for path in entries
        if path.is_file() and (match := _SEGMENT_RE.fullmatch(path.name)) is not None
    )
    if result and [number for number, _ in result] != list(range(1, result[-1][0] + 1)):
        raise ValueError("segment_sequence_gap")
    return tuple(result)


def _checkpoint_position_valid(
    segments: tuple[tuple[int, Path], ...],
    checkpoint: VerifiedCheckpoint,
) -> bool:
    by_number = dict(segments)
    number = checkpoint.journal_position.segment
    path = by_number.get(number)
    if path is None:
        return False
    try:
        link_stat = os.lstat(path)
        if (
            not stat.S_ISREG(link_stat.st_mode)
            or _is_link_or_junction(path)
            or link_stat.st_nlink != 1
        ):
            return False
        size = link_stat.st_size
        offset = checkpoint.journal_position.offset
        if offset > size:
            return False
        if checkpoint.snapshot.last_sequence == 0:
            return number == 1 and offset == 0
        if offset == 0:
            number -= 1
            path = by_number.get(number)
            if path is None:
                return False
            link_stat = os.lstat(path)
            if (
                not stat.S_ISREG(link_stat.st_mode)
                or _is_link_or_junction(path)
                or link_stat.st_nlink != 1
            ):
                return False
            offset = link_stat.st_size
        if offset <= 0:
            return False
        start = max(0, offset - DEFAULT_DECODE_LIMITS.max_bytes - 1)
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (
                link_stat.st_dev,
                link_stat.st_ino,
            ):
                return False
            handle.seek(start)
            suffix = handle.read(offset - start)
            observed = os.fstat(handle.fileno())
            if (observed.st_dev, observed.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ) or observed.st_size != opened.st_size:
                return False
        if not suffix.endswith(b"\n"):
            return False
        prior_delimiter = suffix.rfind(b"\n", 0, len(suffix) - 1)
        frame = suffix[prior_delimiter + 1 :] if prior_delimiter >= 0 else suffix
        if len(frame) > DEFAULT_DECODE_LIMITS.max_bytes:
            return False
        decoded = decode_journal_event_bytes(frame)
        if not decoded.ok or decoded.value is None:
            return False
        event = decoded.value
        if frame != event.canonical_json_bytes():
            return False
        if (
            event.sequence != checkpoint.snapshot.last_sequence
            or event.event_hash != checkpoint.snapshot.last_event_hash
        ):
            return False
    except OSError:
        return False
    return True


def _journal_index_status(
    root: Path,
    segments: tuple[tuple[int, Path], ...],
    run_id: str,
) -> tuple[bool, str]:
    path = root / "index.json"
    try:
        if not path.exists():
            return (len(segments) <= 1, "missing")
        if _is_link_or_junction(path):
            return False, "index_link_or_reparse_point"
        raw = _read_limited(path, _INDEX_MAX_BYTES)
        value = _canonical_object(raw)
        expected = {
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
        if set(value) != expected or value["index_version"] != _INDEX_VERSION:
            raise ValueError("schema")
        integer_fields = (
            "active_segment",
            "byte_count",
            "event_count",
            "last_sequence",
            "sealed_segment",
            "start_sequence",
        )
        if any(
            type(value[name]) is not int or value[name] <= 0 for name in integer_fields
        ):
            raise ValueError("integer")
        if any(
            type(value[name]) is not str
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value[name])
            for name in ("last_event_hash", "previous_event_hash", "segment_hash")
        ):
            raise ValueError("hash")
        if not segments:
            raise ValueError("index_without_segments")
        if value["active_segment"] != segments[-1][0]:
            raise ValueError("stale_active_segment")
        if value["sealed_segment"] + 1 != value["active_segment"]:
            raise ValueError("segment_order")
        sealed_path = dict(segments).get(value["sealed_segment"])
        if sealed_path is None:
            raise ValueError("sealed_segment_missing")
        descriptor = _scan_indexed_segment(
            sealed_path,
            value["sealed_segment"],
            JournalHead(value["start_sequence"] - 1, value["previous_event_hash"]),
            run_id,
        )
        observed = (
            descriptor.head.sequence,
            descriptor.head.event_hash,
            descriptor.event_count,
            descriptor.byte_count,
            descriptor.content_hash,
        )
        expected_values = (
            value["last_sequence"],
            value["last_event_hash"],
            value["event_count"],
            value["byte_count"],
            value["segment_hash"],
        )
        if observed != expected_values:
            raise ValueError("mismatch")
    except (OSError, TypeError, ValueError, _SegmentReplaced) as exc:
        return False, _detail(exc)
    return True, "verified"


def _scan_indexed_segment(
    path: Path,
    number: int,
    start: JournalHead,
    run_id: str,
) -> _SegmentDescriptor:
    head = start
    count = 0
    size = 0
    digest = hashlib.sha256()
    link_stat = os.lstat(path)
    if (
        not stat.S_ISREG(link_stat.st_mode)
        or _is_link_or_junction(path)
        or link_stat.st_nlink != 1
    ):
        raise _SegmentReplaced("indexed segment is not a protected regular file")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (link_stat.st_dev, link_stat.st_ino):
            raise _SegmentReplaced("indexed segment identity changed before read")
        while frame := handle.readline(DEFAULT_DECODE_LIMITS.max_bytes + 1):
            if len(frame) > DEFAULT_DECODE_LIMITS.max_bytes or not frame.endswith(
                b"\n"
            ):
                raise ValueError("indexed_segment_frame")
            decoded = decode_journal_event_bytes(frame)
            if not decoded.ok or decoded.value is None:
                raise ValueError("indexed_segment_decode")
            event = decoded.value
            if (
                frame != event.canonical_json_bytes()
                or event.identity.run_id != run_id
                or event.sequence != head.sequence + 1
                or event.previous_event_hash != head.event_hash
            ):
                raise ValueError("indexed_segment_chain")
            head = JournalHead(event.sequence, event.event_hash)
            count += 1
            size += len(frame)
            digest.update(frame)
        observed = os.fstat(handle.fileno())
        if (observed.st_dev, observed.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or observed.st_size != opened.st_size:
            raise _SegmentReplaced("indexed segment changed during verification")
    if count == 0:
        raise ValueError("indexed_segment_empty")
    return _SegmentDescriptor(
        number,
        start,
        head,
        count,
        size,
        "sha256:" + digest.hexdigest(),
    )


def _rebuild_journal_index(
    root: Path,
    descriptors: tuple[_SegmentDescriptor, ...],
) -> None:
    path = root / "index.json"
    if len(descriptors) <= 1:
        if path.exists():
            _quarantine_derived_index(root)
        return
    sealed = descriptors[-2]
    active = descriptors[-1]
    if active.number != sealed.number + 1 or sealed.event_count == 0:
        raise ValueError("cannot publish an index for an invalid segment boundary")
    value = {
        "active_segment": active.number,
        "byte_count": sealed.byte_count,
        "event_count": sealed.event_count,
        "index_version": _INDEX_VERSION,
        "last_event_hash": sealed.head.event_hash,
        "last_sequence": sealed.head.sequence,
        "previous_event_hash": sealed.start_head.event_hash,
        "sealed_segment": sealed.number,
        "segment_hash": sealed.content_hash,
        "start_sequence": sealed.start_head.sequence + 1,
    }
    _atomic_publish(path, canonical_json_bytes(value))
    _sync_file_parent(path)


def _quarantine_tail(
    root: Path,
    segment_path: Path,
    segment: int,
    offset: int,
    raw: bytes,
    control_root_validator: Callable[[], bool] | None = None,
) -> QuarantinedTail:
    if not _control_root_valid(control_root_validator):
        raise ValueError("control_root_drift")
    digest = _sha256_bytes(raw)
    before = os.lstat(segment_path)
    if (
        not stat.S_ISREG(before.st_mode)
        or _is_link_or_junction(segment_path)
        or before.st_nlink != 1
    ):
        raise ValueError("segment_replaced")
    directory = root.parent / "quarantine"
    directory.mkdir(parents=True, exist_ok=True)
    name = f"uncommitted-tail-{segment:08d}-{offset:016d}-{digest[7:23]}.bin"
    target = directory / name
    metadata_path = directory / f"{name[:-4]}.json"
    metadata = canonical_json_bytes(
        {
            "byte_count": len(raw),
            "byte_offset": offset,
            "raw_sha256": digest,
            "reason": "uncommitted_tail",
            "segment": segment,
        }
    )
    _write_quarantine_immutable(target, raw)
    _write_quarantine_immutable(metadata_path, metadata)
    _sync_file_parent(target)
    if not _control_root_valid(control_root_validator):
        raise ValueError("control_root_drift")
    with segment_path.open("r+b", buffering=0) as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ) or opened.st_size != offset + len(raw):
            raise ValueError("segment_replaced")
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_file_parent(segment_path)
    if not _control_root_valid(control_root_validator):
        raise ValueError("control_root_drift")
    return QuarantinedTail(
        segment,
        offset,
        len(raw),
        digest,
        target.relative_to(root.parent).as_posix(),
    )


def _quarantine_derived_index(root: Path) -> None:
    source = root / "index.json"
    if not source.exists():
        return
    raw = _read_limited(source, _INDEX_MAX_BYTES)
    directory = root.parent / "quarantine"
    directory.mkdir(parents=True, exist_ok=True)
    digest = _sha256_bytes(raw)
    target = directory / f"derived-index-{digest[7:31]}.json"
    if target.exists():
        if _read_limited(target, _INDEX_MAX_BYTES) != raw:
            raise ValueError("derived index quarantine collision")
        source.unlink()
    else:
        os.replace(source, target)
    _sync_file_parent(target)
    _sync_file_parent(source)


def _atomic_publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    replaced = False
    try:
        with temporary.open("xb", buffering=0) as handle:
            written = handle.write(payload)
            if written != len(payload):
                raise OSError("short derived write")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
    finally:
        if not replaced:
            temporary.unlink(missing_ok=True)


def _canonical_object(raw: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=_reject_json_number,
            parse_float=_reject_json_number,
            parse_int=_signed_64_json_integer,
        )
    except RecursionError as exc:
        raise ValueError("depth_limit") from exc
    _validate_replay_json_shape(value)
    if type(value) is not dict or canonical_json_bytes(value) != raw:
        raise ValueError("noncanonical")
    return value


def _read_limited(path: Path, limit: int) -> bytes:
    link_stat = os.lstat(path)
    if (
        not stat.S_ISREG(link_stat.st_mode)
        or _is_link_or_junction(path)
        or link_stat.st_nlink != 1
    ):
        raise ValueError("protected file identity is invalid")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (
            link_stat.st_dev,
            link_stat.st_ino,
        ):
            raise ValueError("protected file changed before read")
        raw = handle.read(limit + 1)
        observed = os.fstat(handle.fileno())
        if (observed.st_dev, observed.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or observed.st_size != opened.st_size:
            raise ValueError("protected file changed during read")
    if len(raw) > limit:
        raise ValueError("size_limit")
    return raw


def _sync_file_parent(path: Path) -> None:
    if os.name == "nt":
        from wish_builder.adapters.storage.filesystem import FilesystemJournalStorage

        FilesystemJournalStorage._sync_windows_directory(path.parent)
        return
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_quarantine_immutable(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb", buffering=0) as handle:
            written = handle.write(payload)
            if written != len(payload):
                raise OSError("short quarantine write")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if _read_limited(path, DEFAULT_DECODE_LIMITS.max_bytes) != payload:
            raise ValueError("quarantine collision")


def _checkpoint_derived_fault(fault: CheckpointFault) -> DerivedDataFault:
    return DerivedDataFault(
        "checkpoint",
        f"{fault.code.value}:{fault.operation}:{fault.detail}",
        fault.raw_sha256,
    )


def _control_root_valid(validator: Callable[[], bool] | None) -> bool:
    if validator is None:
        return True
    try:
        return validator() is True
    # A caller-supplied validator is an untrusted safety boundary.
    except Exception:  # noqa: BLE001
        return False


def _filesystem_identity_validator(expected: object) -> Callable[[], bool]:
    def validate() -> bool:
        from wish_builder.adapters.git_identity import (
            FilesystemIdentity,
            revalidate_control_root,
        )

        if type(expected) is not FilesystemIdentity:
            return False
        return revalidate_control_root(expected).ok

    return validate


def _is_link_or_junction(path: Path) -> bool:
    link_stat = os.lstat(path)
    attributes = getattr(link_stat, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(link_stat.st_mode) or bool(attributes & reparse_attribute)


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _detail(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:160] if text else type(exc).__name__


__all__ = [
    "DerivedDataFault",
    "QuarantinedTail",
    "ReplayFault",
    "ReplayFaultCode",
    "ReplayResult",
    "ReplayStatus",
    "replay_journal",
]
