"""Immutable checkpoint publication and verification for replay recovery."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from wish_builder.contracts import DEFAULT_DECODE_LIMITS
from wish_builder.contracts.execution import (
    ExecutionManifestModel,
    is_execution_manifest_model,
)
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import (
    JournalEventType,
    RuntimeReasonCode,
    RuntimeState,
)
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.kernel.state import (
    AttemptProjection,
    KernelSnapshot,
    TaskProjection,
)

CHECKPOINT_SCHEMA_VERSION = 1
POINTER_SCHEMA_VERSION = 1
CHECKPOINT_MAX_BYTES = 8 * 1024 * 1024
POINTER_MAX_BYTES = 64 * 1024
_MIN_SIGNED_64 = -(2**63)
_MAX_SIGNED_64 = 2**63 - 1
_CHECKPOINT_ID_RE = re.compile(r"CHECKPOINT-[0-9]{20}-[0-9A-F]{24}\Z")
_PHASE_BOUNDARY_EVENTS = frozenset(
    {
        JournalEventType.RUN_INITIALIZED,
        JournalEventType.PREFLIGHT_COMPLETED,
        JournalEventType.DISCOVERY_COMPLETED,
        JournalEventType.GATE_APPROVED,
        JournalEventType.TRELLIS_GRAPH_IMPORTED,
        JournalEventType.DECOMPOSITION_COMPLETED,
        JournalEventType.TASK_GRAPH_FROZEN,
        JournalEventType.EXECUTION_COMPLETED,
        JournalEventType.INTEGRATION_VERIFIED,
        JournalEventType.QUALITY_DOCS_VERIFIED,
        JournalEventType.LEASE_RELEASED,
        JournalEventType.RUN_ARCHIVED,
    }
)


class CheckpointLoadStatus(StrEnum):
    LOADED = "loaded"
    ABSENT = "absent"
    DISCARDED = "discarded"


class CheckpointFaultCode(StrEnum):
    POINTER_INVALID = "pointer_invalid"
    POINTER_IO_FAILED = "pointer_io_failed"
    CHECKPOINT_MISSING = "checkpoint_missing"
    CHECKPOINT_INVALID = "checkpoint_invalid"
    CHECKPOINT_IO_FAILED = "checkpoint_io_failed"
    MANIFEST_MISMATCH = "manifest_mismatch"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    GRAPH_INDEX_MISMATCH = "graph_index_mismatch"
    CONTROL_ROOT_DRIFT = "control_root_drift"
    PUBLISH_FAILED = "publish_failed"


@dataclass(frozen=True, slots=True)
class JournalPosition:
    segment: int
    offset: int

    def __post_init__(self) -> None:
        if type(self.segment) is not int or self.segment <= 0:
            raise ValueError("segment must be a positive integer")
        if type(self.offset) is not int or self.offset < 0:
            raise ValueError("offset must be a non-negative integer")

    def to_primitive(self) -> dict[str, int]:
        return {"offset": self.offset, "segment": self.segment}


@dataclass(frozen=True, slots=True)
class CheckpointPointer:
    checkpoint_id: str
    checkpoint_path: str
    checkpoint_sha256: str
    checkpoint_through_sequence: int
    checkpoint_through_event_hash: str
    journal_through_sequence: int
    journal_through_event_hash: str
    pointer_schema_version: int = POINTER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.pointer_schema_version != POINTER_SCHEMA_VERSION:
            raise ValueError(f"pointer_schema_version must be {POINTER_SCHEMA_VERSION}")
        if type(self.checkpoint_id) is not str or not _CHECKPOINT_ID_RE.fullmatch(
            self.checkpoint_id
        ):
            raise ValueError("checkpoint_id is invalid")
        _validate_relative_checkpoint_path(self.checkpoint_path)
        for value, name in (
            (self.checkpoint_sha256, "checkpoint_sha256"),
            (self.checkpoint_through_event_hash, "checkpoint_through_event_hash"),
            (self.journal_through_event_hash, "journal_through_event_hash"),
        ):
            if type(value) is not str or not HASH_RE.fullmatch(value):
                raise ValueError(f"{name} must be a full sha256 reference")
        for value, name in (
            (self.checkpoint_through_sequence, "checkpoint_through_sequence"),
            (self.journal_through_sequence, "journal_through_sequence"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.journal_through_sequence < self.checkpoint_through_sequence:
            raise ValueError("journal terminal position precedes the checkpoint")
        if (
            self.journal_through_sequence == self.checkpoint_through_sequence
            and self.journal_through_event_hash != self.checkpoint_through_event_hash
        ):
            raise ValueError("equal pointer positions must use the same event hash")

    def to_primitive(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_path": self.checkpoint_path,
            "checkpoint_sha256": self.checkpoint_sha256,
            "checkpoint_through_event_hash": self.checkpoint_through_event_hash,
            "checkpoint_through_sequence": self.checkpoint_through_sequence,
            "journal_through_event_hash": self.journal_through_event_hash,
            "journal_through_sequence": self.journal_through_sequence,
            "pointer_schema_version": self.pointer_schema_version,
        }


@dataclass(frozen=True, slots=True)
class VerifiedCheckpoint:
    checkpoint_id: str
    checkpoint_sha256: str
    snapshot: KernelSnapshot
    graph_index: GraphIndex
    journal_position: JournalPosition
    journal_through_sequence: int
    journal_through_event_hash: str


@dataclass(frozen=True, slots=True)
class CheckpointFault:
    code: CheckpointFaultCode
    operation: str
    detail: str
    raw_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not CheckpointFaultCode:
            raise TypeError("code must be a CheckpointFaultCode")
        if type(self.operation) is not str or not self.operation:
            raise ValueError("operation must be non-empty")
        if type(self.detail) is not str or not self.detail:
            raise ValueError("detail must be non-empty")
        if self.raw_sha256 is not None and not HASH_RE.fullmatch(self.raw_sha256):
            raise ValueError("raw_sha256 must be a full sha256 reference")


@dataclass(frozen=True, slots=True)
class CheckpointLoadResult:
    status: CheckpointLoadStatus
    checkpoint: VerifiedCheckpoint | None = None
    fault: CheckpointFault | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not CheckpointLoadStatus:
            raise TypeError("status must be a CheckpointLoadStatus")
        if self.status is CheckpointLoadStatus.LOADED:
            if (
                type(self.checkpoint) is not VerifiedCheckpoint
                or self.fault is not None
            ):
                raise ValueError("a loaded result requires only a verified checkpoint")
        elif self.status is CheckpointLoadStatus.DISCARDED:
            if self.checkpoint is not None or type(self.fault) is not CheckpointFault:
                raise ValueError("a discarded result requires only a fault")
        elif self.checkpoint is not None or self.fault is not None:
            raise ValueError("an absent result cannot contain checkpoint data")


@dataclass(frozen=True, slots=True)
class CheckpointPolicy:
    event_interval: int = 100
    time_interval_seconds: float = 600.0

    def __post_init__(self) -> None:
        if type(self.event_interval) is not int or self.event_interval <= 0:
            raise ValueError("event_interval must be positive")
        if (
            type(self.time_interval_seconds) not in {int, float}
            or type(self.time_interval_seconds) is bool
            or not math.isfinite(self.time_interval_seconds)
            or self.time_interval_seconds <= 0
        ):
            raise ValueError("time_interval_seconds must be positive")

    def should_publish(
        self,
        *,
        previous_sequence: int,
        current_sequence: int,
        last_event_type: JournalEventType | None = None,
        elapsed_seconds: float | None = None,
    ) -> bool:
        if type(previous_sequence) is not int or previous_sequence < 0:
            raise ValueError("previous_sequence must be non-negative")
        if type(current_sequence) is not int or current_sequence < previous_sequence:
            raise ValueError("current_sequence must not precede previous_sequence")
        if (
            last_event_type is not None
            and type(last_event_type) is not JournalEventType
        ):
            raise TypeError("last_event_type must be a JournalEventType or null")
        if elapsed_seconds is not None and (
            type(elapsed_seconds) not in {int, float}
            or type(elapsed_seconds) is bool
            or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0
        ):
            raise ValueError("elapsed_seconds must be non-negative")
        if current_sequence == previous_sequence:
            return False
        return (
            last_event_type in _PHASE_BOUNDARY_EVENTS
            or current_sequence - previous_sequence >= self.event_interval
            or (
                elapsed_seconds is not None
                and elapsed_seconds >= self.time_interval_seconds
            )
        )


class CheckpointPersistenceFault(RuntimeError):
    def __init__(self, operation: str, detail: str) -> None:
        self.code = CheckpointFaultCode.PUBLISH_FAILED
        self.operation = operation
        self.detail = detail
        super().__init__(f"{self.code.value}:{operation}:{detail}")


class CheckpointStore:
    """Filesystem-backed immutable checkpoint objects and atomic current pointer."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        control_root_validator: Callable[[], bool] | None = None,
        expected_control_root: object | None = None,
    ) -> None:
        if control_root_validator is not None and expected_control_root is not None:
            raise ValueError(
                "provide control_root_validator or expected_control_root, not both"
            )
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.current = self.root / "current.json"
        self._control_root_validator = control_root_validator or (
            None
            if expected_control_root is None
            else _filesystem_identity_validator(expected_control_root)
        )

    def publish(
        self,
        manifest: ExecutionManifestModel,
        snapshot: KernelSnapshot,
        graph_index: GraphIndex,
        journal_position: JournalPosition,
        *,
        journal_through_sequence: int | None = None,
        journal_through_event_hash: str | None = None,
    ) -> VerifiedCheckpoint:
        if not is_execution_manifest_model(manifest):
            raise TypeError("manifest must be an ExecutionManifest model")
        if type(snapshot) is not KernelSnapshot:
            raise TypeError("snapshot must be a KernelSnapshot")
        if type(graph_index) is not GraphIndex:
            raise TypeError("graph_index must be a GraphIndex")
        if type(journal_position) is not JournalPosition:
            raise TypeError("journal_position must be a JournalPosition")
        if not graph_index.verify(manifest, snapshot):
            raise ValueError("graph_index does not match manifest and snapshot")
        terminal_sequence = (
            snapshot.last_sequence
            if journal_through_sequence is None
            else journal_through_sequence
        )
        terminal_hash = (
            snapshot.last_event_hash
            if journal_through_event_hash is None
            else journal_through_event_hash
        )
        if (
            type(terminal_sequence) is not int
            or terminal_sequence < snapshot.last_sequence
        ):
            raise ValueError("journal terminal sequence precedes the checkpoint")
        if type(terminal_hash) is not str or not HASH_RE.fullmatch(terminal_hash):
            raise ValueError("journal terminal hash must be a full sha256 reference")
        if (
            terminal_sequence == snapshot.last_sequence
            and terminal_hash != snapshot.last_event_hash
        ):
            raise ValueError(
                "equal journal and checkpoint positions must use the same hash"
            )

        manifest_hash = _sha256_bytes(manifest.canonical_json_bytes())
        checkpoint_id = _checkpoint_id(
            snapshot.last_sequence,
            snapshot.last_event_hash,
            manifest_hash,
        )
        relative_path = f"objects/{checkpoint_id}.json"
        record = {
            "checkpoint_id": checkpoint_id,
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_through_event_hash": snapshot.last_event_hash,
            "checkpoint_through_sequence": snapshot.last_sequence,
            "graph_index": graph_index.to_primitive(),
            "journal_position": journal_position.to_primitive(),
            "manifest_hash": manifest_hash,
            "snapshot": _snapshot_to_primitive(snapshot),
        }
        checkpoint_bytes = canonical_json_bytes(record)
        checkpoint_sha256 = _sha256_bytes(checkpoint_bytes)
        pointer = CheckpointPointer(
            checkpoint_id=checkpoint_id,
            checkpoint_path=relative_path,
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_through_sequence=snapshot.last_sequence,
            checkpoint_through_event_hash=snapshot.last_event_hash,
            journal_through_sequence=terminal_sequence,
            journal_through_event_hash=terminal_hash,
        )
        try:
            self._guard_control_root()
            self._prepare_directories()
            self._guard_control_root()
            target = self.root / Path(*PurePosixPath(relative_path).parts)
            _write_immutable(target, checkpoint_bytes)
            _sync_directory(self.objects)
            self._guard_control_root()
            _atomic_publish(self.current, canonical_json_bytes(pointer.to_primitive()))
            _sync_directory(self.root)
            self._guard_control_root()
        except CheckpointPersistenceFault:
            raise
        except (OSError, ValueError) as exc:
            raise CheckpointPersistenceFault(
                "checkpoint_publish", type(exc).__name__
            ) from exc
        return VerifiedCheckpoint(
            checkpoint_id,
            checkpoint_sha256,
            snapshot,
            graph_index,
            journal_position,
            terminal_sequence,
            terminal_hash,
        )

    def load(
        self,
        manifest: ExecutionManifestModel,
        *,
        coordinator_epoch: int,
    ) -> CheckpointLoadResult:
        if not is_execution_manifest_model(manifest):
            raise TypeError("manifest must be an ExecutionManifest model")
        if type(coordinator_epoch) is not int or coordinator_epoch <= 0:
            raise ValueError("coordinator_epoch must be positive")
        try:
            self._guard_control_root()
        except CheckpointPersistenceFault:
            return _discarded(
                CheckpointFaultCode.CONTROL_ROOT_DRIFT,
                "control_root_revalidate",
                "control_root_drift",
            )
        try:
            pointer_exists = self.current.exists()
            pointer_is_link = pointer_exists and _is_link_or_junction(self.current)
        except OSError as exc:
            return _discarded(
                CheckpointFaultCode.POINTER_IO_FAILED,
                "pointer_identity",
                type(exc).__name__,
            )
        if not pointer_exists:
            return CheckpointLoadResult(CheckpointLoadStatus.ABSENT)
        if pointer_is_link:
            return _discarded(
                CheckpointFaultCode.POINTER_INVALID,
                "pointer_read",
                "link_or_reparse_point",
            )
        try:
            pointer_raw = _read_limited(self.current, POINTER_MAX_BYTES)
        except ValueError as exc:
            return _discarded(
                CheckpointFaultCode.POINTER_INVALID,
                "pointer_read",
                _fault_detail(exc),
            )
        except OSError as exc:
            return _discarded(
                CheckpointFaultCode.POINTER_IO_FAILED,
                "pointer_read",
                type(exc).__name__,
            )
        pointer_digest = _sha256_bytes(pointer_raw)
        try:
            pointer_value = _canonical_object(pointer_raw, "pointer")
            pointer = _pointer_from_primitive(pointer_value)
            target = self._resolve_pointer_path(pointer.checkpoint_path)
        except (TypeError, ValueError, _DecodeFault) as exc:
            return _discarded(
                CheckpointFaultCode.POINTER_INVALID,
                "pointer_decode",
                _fault_detail(exc),
                pointer_digest,
            )
        except OSError as exc:
            return _discarded(
                CheckpointFaultCode.POINTER_IO_FAILED,
                "pointer_resolve",
                type(exc).__name__,
                pointer_digest,
            )
        try:
            checkpoint_raw = _read_limited(target, CHECKPOINT_MAX_BYTES)
        except FileNotFoundError:
            return _discarded(
                CheckpointFaultCode.CHECKPOINT_MISSING,
                "checkpoint_read",
                "missing",
                pointer_digest,
            )
        except ValueError as exc:
            return _discarded(
                CheckpointFaultCode.CHECKPOINT_INVALID,
                "checkpoint_read",
                _fault_detail(exc),
                pointer_digest,
            )
        except OSError as exc:
            return _discarded(
                CheckpointFaultCode.CHECKPOINT_IO_FAILED,
                "checkpoint_read",
                type(exc).__name__,
                pointer_digest,
            )
        checkpoint_digest = _sha256_bytes(checkpoint_raw)
        if checkpoint_digest != pointer.checkpoint_sha256:
            return _discarded(
                CheckpointFaultCode.CHECKPOINT_INVALID,
                "checkpoint_hash",
                "hash_mismatch",
                checkpoint_digest,
            )
        try:
            record = _canonical_object(checkpoint_raw, "checkpoint")
            verified = _verified_checkpoint_from_record(
                record,
                pointer,
                manifest,
                coordinator_epoch,
            )
            self._guard_control_root()
        except CheckpointPersistenceFault:
            return _discarded(
                CheckpointFaultCode.CONTROL_ROOT_DRIFT,
                "control_root_revalidate",
                "control_root_drift",
                checkpoint_digest,
            )
        except _CheckpointMismatch as exc:
            return _discarded(exc.code, exc.operation, exc.detail, checkpoint_digest)
        except (TypeError, ValueError, _DecodeFault) as exc:
            return _discarded(
                CheckpointFaultCode.CHECKPOINT_INVALID,
                "checkpoint_decode",
                _fault_detail(exc),
                checkpoint_digest,
            )
        return CheckpointLoadResult(CheckpointLoadStatus.LOADED, verified)

    def _guard_control_root(self) -> None:
        if self._control_root_validator is None:
            return
        try:
            valid = self._control_root_validator()
        except Exception as exc:
            raise CheckpointPersistenceFault(
                "control_root_revalidate", "control_root_drift"
            ) from exc
        if valid is not True:
            raise CheckpointPersistenceFault(
                "control_root_revalidate", "control_root_drift"
            )

    def _prepare_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects.mkdir(exist_ok=True)
        if _is_link_or_junction(self.root) or _is_link_or_junction(self.objects):
            raise ValueError("checkpoint directories cannot be symbolic links")

    def _resolve_pointer_path(self, relative: str) -> Path:
        pure = _validate_relative_checkpoint_path(relative)
        root = self.root.resolve(strict=True)
        if _is_link_or_junction(self.root):
            raise ValueError("checkpoint root cannot be a symbolic link")
        if _is_link_or_junction(self.objects):
            raise ValueError("checkpoint objects directory cannot be a symbolic link")
        target = self.root.joinpath(*pure.parts)
        if target.exists() and _is_link_or_junction(target):
            raise ValueError("checkpoint object cannot be a symbolic link")
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise ValueError("checkpoint path escapes its root")
        return target


class _DecodeFault(ValueError):
    pass


class _CheckpointMismatch(ValueError):
    def __init__(
        self,
        code: CheckpointFaultCode,
        operation: str,
        detail: str,
    ) -> None:
        self.code = code
        self.operation = operation
        self.detail = detail
        super().__init__(detail)


def _verified_checkpoint_from_record(
    record: dict[str, object],
    pointer: CheckpointPointer,
    manifest: ExecutionManifestModel,
    coordinator_epoch: int,
) -> VerifiedCheckpoint:
    expected_keys = {
        "checkpoint_id",
        "checkpoint_schema_version",
        "checkpoint_through_event_hash",
        "checkpoint_through_sequence",
        "graph_index",
        "journal_position",
        "manifest_hash",
        "snapshot",
    }
    if (
        set(record) != expected_keys
        or record.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION
    ):
        raise _DecodeFault("checkpoint_schema")
    if record.get("checkpoint_id") != pointer.checkpoint_id:
        raise _DecodeFault("checkpoint_id_mismatch")
    manifest_hash = _sha256_bytes(manifest.canonical_json_bytes())
    if record.get("manifest_hash") != manifest_hash:
        raise _CheckpointMismatch(
            CheckpointFaultCode.MANIFEST_MISMATCH,
            "checkpoint_manifest",
            "manifest_hash_mismatch",
        )
    snapshot_value = record.get("snapshot")
    if type(snapshot_value) is not dict:
        raise _DecodeFault("snapshot_type")
    snapshot = _snapshot_from_primitive(snapshot_value)
    if (
        snapshot.run_id != manifest.run_id
        or snapshot.coordinator_epoch != coordinator_epoch
    ):
        raise _CheckpointMismatch(
            CheckpointFaultCode.SNAPSHOT_MISMATCH,
            "checkpoint_snapshot",
            "identity_mismatch",
        )
    if (
        record.get("checkpoint_through_sequence") != snapshot.last_sequence
        or record.get("checkpoint_through_event_hash") != snapshot.last_event_hash
        or pointer.checkpoint_through_sequence != snapshot.last_sequence
        or pointer.checkpoint_through_event_hash != snapshot.last_event_hash
    ):
        raise _CheckpointMismatch(
            CheckpointFaultCode.SNAPSHOT_MISMATCH,
            "checkpoint_snapshot",
            "terminal_position_mismatch",
        )
    expected_id = _checkpoint_id(
        snapshot.last_sequence, snapshot.last_event_hash, manifest_hash
    )
    if pointer.checkpoint_id != expected_id:
        raise _CheckpointMismatch(
            CheckpointFaultCode.SNAPSHOT_MISMATCH,
            "checkpoint_identity",
            "derived_id_mismatch",
        )
    graph_value = record.get("graph_index")
    if type(graph_value) is not dict:
        raise _DecodeFault("graph_index_type")
    rebuilt = GraphIndex.rebuild(manifest, snapshot)
    if graph_value != rebuilt.to_primitive():
        raise _CheckpointMismatch(
            CheckpointFaultCode.GRAPH_INDEX_MISMATCH,
            "checkpoint_graph_index",
            "derived_index_mismatch",
        )
    position_value = record.get("journal_position")
    if type(position_value) is not dict or set(position_value) != {"offset", "segment"}:
        raise _DecodeFault("journal_position_schema")
    position = JournalPosition(position_value["segment"], position_value["offset"])
    return VerifiedCheckpoint(
        pointer.checkpoint_id,
        pointer.checkpoint_sha256,
        snapshot,
        rebuilt,
        position,
        pointer.journal_through_sequence,
        pointer.journal_through_event_hash,
    )


def _snapshot_to_primitive(snapshot: KernelSnapshot) -> dict[str, object]:
    return {
        "attempts": [
            {
                "attempt": attempt.attempt,
                "coordinator_epoch": attempt.coordinator_epoch,
                "correlation_id": attempt.correlation_id,
                "reason_code": (
                    None if attempt.reason_code is None else attempt.reason_code.value
                ),
                "state": attempt.state.value,
                "task_id": attempt.task_id,
            }
            for attempt in snapshot.attempts
        ],
        "coordinator_epoch": snapshot.coordinator_epoch,
        "last_event_hash": snapshot.last_event_hash,
        "last_event_id": snapshot.last_event_id,
        "last_sequence": snapshot.last_sequence,
        "phase": snapshot.phase.value,
        "run_id": snapshot.run_id,
        "run_reason_code": (
            None if snapshot.run_reason_code is None else snapshot.run_reason_code.value
        ),
        "status": snapshot.status.value,
        "tasks": [
            {
                "reason_code": None
                if task.reason_code is None
                else task.reason_code.value,
                "state": task.state.value,
                "task_id": task.task_id,
            }
            for task in snapshot.tasks
        ],
    }


def _snapshot_from_primitive(value: dict[str, object]) -> KernelSnapshot:
    expected = {
        "attempts",
        "coordinator_epoch",
        "last_event_hash",
        "last_event_id",
        "last_sequence",
        "phase",
        "run_id",
        "run_reason_code",
        "status",
        "tasks",
    }
    if set(value) != expected:
        raise _DecodeFault("snapshot_schema")
    tasks_value = value["tasks"]
    attempts_value = value["attempts"]
    if type(tasks_value) is not list or type(attempts_value) is not list:
        raise _DecodeFault("snapshot_collection_type")
    tasks: list[TaskProjection] = []
    for item in tasks_value:
        if type(item) is not dict or set(item) != {"reason_code", "state", "task_id"}:
            raise _DecodeFault("task_projection_schema")
        tasks.append(
            TaskProjection(
                item["task_id"],
                _runtime_state(item["state"]),
                _reason_code(item["reason_code"]),
            )
        )
    attempts: list[AttemptProjection] = []
    attempt_keys = {
        "attempt",
        "coordinator_epoch",
        "correlation_id",
        "reason_code",
        "state",
        "task_id",
    }
    for item in attempts_value:
        if type(item) is not dict or set(item) != attempt_keys:
            raise _DecodeFault("attempt_projection_schema")
        attempts.append(
            AttemptProjection(
                item["task_id"],
                item["attempt"],
                item["correlation_id"],
                item["coordinator_epoch"],
                _runtime_state(item["state"]),
                _reason_code(item["reason_code"]),
            )
        )
    return KernelSnapshot(
        run_id=value["run_id"],
        coordinator_epoch=value["coordinator_epoch"],
        phase=_runtime_state(value["phase"]),
        status=_runtime_state(value["status"]),
        run_reason_code=_reason_code(value["run_reason_code"]),
        tasks=tuple(tasks),
        attempts=tuple(attempts),
        last_sequence=value["last_sequence"],
        last_event_id=value["last_event_id"],
        last_event_hash=value["last_event_hash"],
    )


def _runtime_state(value: object) -> RuntimeState:
    if type(value) is not str:
        raise _DecodeFault("runtime_state_type")
    try:
        return RuntimeState(value)
    except ValueError as exc:
        raise _DecodeFault("runtime_state_value") from exc


def _reason_code(value: object) -> RuntimeReasonCode | None:
    if value is None:
        return None
    if type(value) is not str:
        raise _DecodeFault("reason_code_type")
    try:
        return RuntimeReasonCode(value)
    except ValueError as exc:
        raise _DecodeFault("reason_code_value") from exc


def _pointer_from_primitive(value: dict[str, object]) -> CheckpointPointer:
    expected = {
        "checkpoint_id",
        "checkpoint_path",
        "checkpoint_sha256",
        "checkpoint_through_event_hash",
        "checkpoint_through_sequence",
        "journal_through_event_hash",
        "journal_through_sequence",
        "pointer_schema_version",
    }
    if set(value) != expected:
        raise _DecodeFault("pointer_schema")
    return CheckpointPointer(
        checkpoint_id=value["checkpoint_id"],
        checkpoint_path=value["checkpoint_path"],
        checkpoint_sha256=value["checkpoint_sha256"],
        checkpoint_through_sequence=value["checkpoint_through_sequence"],
        checkpoint_through_event_hash=value["checkpoint_through_event_hash"],
        journal_through_sequence=value["journal_through_sequence"],
        journal_through_event_hash=value["journal_through_event_hash"],
        pointer_schema_version=value["pointer_schema_version"],
    )


def _validate_relative_checkpoint_path(value: object) -> PurePosixPath:
    if type(value) is not str or not value or "\\" in value:
        raise ValueError("checkpoint_path must be a normalized relative POSIX path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or pure.parts[0:1] != ("objects",)
        or len(pure.parts) != 2
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.suffix != ".json"
    ):
        raise ValueError(
            "checkpoint_path must name one object beneath the checkpoint root"
        )
    return pure


def _canonical_object(raw: bytes, name: str) -> dict[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise _DecodeFault(f"{name}_duplicate_key")
            result[key] = item
        return result

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=no_duplicates,
            parse_constant=lambda token: _reject_json_number(token, name),
            parse_float=lambda token: _reject_json_number(token, name),
            parse_int=lambda token: _signed_64_json_integer(token, name),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _DecodeFault(f"{name}_json") from exc
    if type(value) is not dict:
        raise _DecodeFault(f"{name}_object")
    _validate_json_shape(value, name)
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise _DecodeFault(f"{name}_value") from exc
    if raw != canonical:
        raise _DecodeFault(f"{name}_noncanonical")
    return value


def _reject_json_number(token: str, name: str) -> object:
    raise _DecodeFault(f"{name}_number")


def _signed_64_json_integer(token: str, name: str) -> int:
    value = int(token)
    if not _MIN_SIGNED_64 <= value <= _MAX_SIGNED_64:
        raise _DecodeFault(f"{name}_integer_range")
    return value


def _validate_json_shape(value: object, name: str) -> None:
    item_count = 0
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, parent_depth = stack.pop()
        if current is None or type(current) in (bool, int):
            continue
        if type(current) is str:
            if len(current) > DEFAULT_DECODE_LIMITS.max_string_length:
                raise _DecodeFault(f"{name}_string_limit")
            continue
        if type(current) not in (dict, list):
            raise _DecodeFault(f"{name}_value")
        depth = parent_depth + 1
        if depth > DEFAULT_DECODE_LIMITS.max_depth:
            raise _DecodeFault(f"{name}_depth_limit")
        item_count += len(current)
        if item_count > DEFAULT_DECODE_LIMITS.max_items:
            raise _DecodeFault(f"{name}_item_limit")
        if type(current) is list:
            stack.extend((item, depth) for item in current)
            continue
        for key, item in current.items():
            if len(key) > DEFAULT_DECODE_LIMITS.max_string_length:
                raise _DecodeFault(f"{name}_string_limit")
            stack.append((item, depth))


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
        raise ValueError("file_size_limit")
    return raw


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError("immutable checkpoint cannot be a symbolic link")
    try:
        with path.open("xb", buffering=0) as handle:
            _write_all(handle, payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        existing = _read_limited(path, CHECKPOINT_MAX_BYTES)
        if existing == payload:
            return
        corrupt = path.parent.parent / "corrupt"
        corrupt.mkdir(exist_ok=True)
        digest = hashlib.sha256(existing).hexdigest()[:24]
        preserved = corrupt / f"{path.stem}-{digest}.json"
        if preserved.exists():
            if _read_limited(preserved, CHECKPOINT_MAX_BYTES) != existing:
                raise ValueError("corrupt checkpoint quarantine collision")
            path.unlink()
        else:
            os.replace(path, preserved)
        _sync_directory(corrupt)
        with path.open("xb", buffering=0) as handle:
            _write_all(handle, payload)
            handle.flush()
            os.fsync(handle.fileno())


def _atomic_publish(path: Path, payload: bytes) -> None:
    temporary = path.parent / f".{path.name}-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    replaced = False
    try:
        with temporary.open("xb", buffering=0) as handle:
            _write_all(handle, payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
    finally:
        if not replaced:
            temporary.unlink(missing_ok=True)


def _write_all(handle: object, payload: bytes) -> None:
    written = handle.write(payload)  # type: ignore[attr-defined]
    if written != len(payload):
        raise OSError("short write")


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
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
    flush = kernel32.FlushFileBuffers
    flush.argtypes = (wintypes.HANDLE,)
    flush.restype = wintypes.BOOL
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)


def _checkpoint_id(sequence: int, event_hash: str, manifest_hash: str) -> str:
    identity = (
        hashlib.sha256(f"{sequence}:{event_hash}:{manifest_hash}".encode("ascii"))
        .hexdigest()[:24]
        .upper()
    )
    return f"CHECKPOINT-{sequence:020d}-{identity}"


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _discarded(
    code: CheckpointFaultCode,
    operation: str,
    detail: str,
    raw_sha256: str | None = None,
) -> CheckpointLoadResult:
    return CheckpointLoadResult(
        CheckpointLoadStatus.DISCARDED,
        fault=CheckpointFault(code, operation, detail, raw_sha256),
    )


def _fault_detail(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:160] if text else type(exc).__name__


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
    path_stat = os.lstat(path)
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_attribute)


__all__ = [
    "CHECKPOINT_MAX_BYTES",
    "CHECKPOINT_SCHEMA_VERSION",
    "POINTER_SCHEMA_VERSION",
    "CheckpointFault",
    "CheckpointFaultCode",
    "CheckpointLoadResult",
    "CheckpointLoadStatus",
    "CheckpointPersistenceFault",
    "CheckpointPointer",
    "CheckpointPolicy",
    "CheckpointStore",
    "JournalPosition",
    "VerifiedCheckpoint",
]
