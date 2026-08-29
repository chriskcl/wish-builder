"""Typed Trellis graph and lifecycle service boundaries."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from wish_builder.contracts import canonical_json_bytes, canonical_sha256
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import EffectStatus
from wish_builder.contracts.serialization import MAX_CANONICAL_INTEGER

from .effects import PreparedEffect

TRELLIS_PORT_SCHEMA_VERSION = 1
MAX_GRAPH_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_TRELLIS_EVIDENCE = 32

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


def _text(
    value: object,
    field_name: str,
    *,
    max_length: int = 256,
    multiline: bool = False,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    if not normalized or not normalized.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds the string limit")
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain valid Unicode") from exc
    allowed = {"\n", "\t"} if multiline else set()
    if any(
        (ord(character) < 32 or ord(character) == 127)
        and character not in allowed
        for character in normalized
    ):
        raise ValueError(f"{field_name} contains a disallowed control character")
    return normalized


def _token(value: object, field_name: str) -> str:
    normalized = _text(value, field_name)
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} is not a stable token")
    return normalized


def _digest(value: object, field_name: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return value


def _optional_digest(value: object, field_name: str) -> str | None:
    return None if value is None else _digest(value, field_name)


def _positive(value: object, field_name: str) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > MAX_CANONICAL_INTEGER
    ):
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _timestamp(value: object, field_name: str) -> str:
    normalized = _text(value, field_name, max_length=32)
    if not _TIMESTAMP_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a UTC timestamp")
    try:
        datetime.fromisoformat(normalized[:-1])
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UTC timestamp") from exc
    return normalized


def _commit(value: object, field_name: str) -> str:
    normalized = _text(value, field_name, max_length=64)
    if len(normalized) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{field_name} must be a lowercase Git object ID")
    return normalized


def _evidence(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError("evidence must be a tuple")
    if len(value) > MAX_TRELLIS_EVIDENCE:
        raise ValueError("evidence exceeds the item limit")
    normalized = tuple(
        _text(item, "evidence item", max_length=512) for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError("evidence must not contain duplicates")
    return tuple(sorted(normalized, key=lambda item: item.encode("utf-8")))


class CanonicalTrellisContract:
    __slots__ = ()

    def to_primitive(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    def canonical_sha256(self) -> str:
        return "sha256:" + canonical_sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class TrellisGraphSnapshot:
    """One complete Wish Builder-derived envelope over Trellis task data."""

    export_version: str
    trellis_version: str
    parent_task_id: str
    revision: str | None
    observed_at: str
    snapshot_bytes: bytes
    source_sha256: str
    complete: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "export_version", _token(self.export_version, "export_version")
        )
        object.__setattr__(
            self, "trellis_version", _token(self.trellis_version, "trellis_version")
        )
        object.__setattr__(
            self,
            "parent_task_id",
            _text(self.parent_task_id, "parent_task_id", max_length=512),
        )
        object.__setattr__(self, "revision", _optional_digest(self.revision, "revision"))
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        if type(self.snapshot_bytes) is not bytes:
            raise TypeError("snapshot_bytes must be bytes")
        if not self.snapshot_bytes:
            raise ValueError("snapshot_bytes must not be empty")
        if len(self.snapshot_bytes) > MAX_GRAPH_SNAPSHOT_BYTES:
            raise ValueError("snapshot_bytes exceeds the byte limit")
        object.__setattr__(
            self, "source_sha256", _digest(self.source_sha256, "source_sha256")
        )
        expected = "sha256:" + hashlib.sha256(self.snapshot_bytes).hexdigest()
        if self.source_sha256 != expected:
            raise ValueError("source_sha256 does not match snapshot_bytes")
        if type(self.complete) is not bool:
            raise TypeError("complete must be a boolean")

    @property
    def byte_length(self) -> int:
        return len(self.snapshot_bytes)


@dataclass(frozen=True, slots=True)
class PrepareAttempt(CanonicalTrellisContract):
    operation_id: str
    run_id: str
    parent_task_id: str
    trellis_task_id: str
    task_id: str
    attempt: int
    dispatch_id: str
    manifest_digest: str
    trellis_graph_digest: str
    expected_base_commit: str
    command_type = "prepare_attempt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _token(self.operation_id, "operation_id"))
        object.__setattr__(self, "run_id", _token(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "parent_task_id",
            _text(self.parent_task_id, "parent_task_id", max_length=512),
        )
        object.__setattr__(
            self,
            "trellis_task_id",
            _text(self.trellis_task_id, "trellis_task_id", max_length=512),
        )
        object.__setattr__(self, "task_id", _token(self.task_id, "task_id"))
        object.__setattr__(self, "attempt", _positive(self.attempt, "attempt"))
        object.__setattr__(self, "dispatch_id", _token(self.dispatch_id, "dispatch_id"))
        object.__setattr__(
            self, "manifest_digest", _digest(self.manifest_digest, "manifest_digest")
        )
        object.__setattr__(
            self,
            "trellis_graph_digest",
            _digest(self.trellis_graph_digest, "trellis_graph_digest"),
        )
        object.__setattr__(
            self,
            "expected_base_commit",
            _commit(self.expected_base_commit, "expected_base_commit"),
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "command_type": self.command_type,
            "dispatch_id": self.dispatch_id,
            "expected_base_commit": self.expected_base_commit,
            "manifest_digest": self.manifest_digest,
            "operation_id": self.operation_id,
            "parent_task_id": self.parent_task_id,
            "run_id": self.run_id,
            "schema_version": TRELLIS_PORT_SCHEMA_VERSION,
            "task_id": self.task_id,
            "trellis_graph_digest": self.trellis_graph_digest,
            "trellis_task_id": self.trellis_task_id,
        }


@dataclass(frozen=True, slots=True)
class CheckAttempt(CanonicalTrellisContract):
    operation_id: str
    attempt_id: str
    trellis_task_id: str
    task_id: str
    task_packet_digest: str
    expected_head_commit: str
    command_type = "check_attempt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _token(self.operation_id, "operation_id"))
        object.__setattr__(self, "attempt_id", _token(self.attempt_id, "attempt_id"))
        object.__setattr__(
            self,
            "trellis_task_id",
            _text(self.trellis_task_id, "trellis_task_id", max_length=512),
        )
        object.__setattr__(self, "task_id", _token(self.task_id, "task_id"))
        object.__setattr__(
            self,
            "task_packet_digest",
            _digest(self.task_packet_digest, "task_packet_digest"),
        )
        object.__setattr__(
            self,
            "expected_head_commit",
            _commit(self.expected_head_commit, "expected_head_commit"),
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "command_type": self.command_type,
            "expected_head_commit": self.expected_head_commit,
            "operation_id": self.operation_id,
            "schema_version": TRELLIS_PORT_SCHEMA_VERSION,
            "task_id": self.task_id,
            "task_packet_digest": self.task_packet_digest,
            "trellis_task_id": self.trellis_task_id,
        }


@dataclass(frozen=True, slots=True)
class FinishAttempt(CanonicalTrellisContract):
    operation_id: str
    attempt_id: str
    trellis_task_id: str
    task_id: str
    delivered_commit: str
    delivery_evidence_digest: str
    command_type = "finish_attempt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _token(self.operation_id, "operation_id"))
        object.__setattr__(self, "attempt_id", _token(self.attempt_id, "attempt_id"))
        object.__setattr__(
            self,
            "trellis_task_id",
            _text(self.trellis_task_id, "trellis_task_id", max_length=512),
        )
        object.__setattr__(self, "task_id", _token(self.task_id, "task_id"))
        object.__setattr__(
            self, "delivered_commit", _commit(self.delivered_commit, "delivered_commit")
        )
        object.__setattr__(
            self,
            "delivery_evidence_digest",
            _digest(self.delivery_evidence_digest, "delivery_evidence_digest"),
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "command_type": self.command_type,
            "delivered_commit": self.delivered_commit,
            "delivery_evidence_digest": self.delivery_evidence_digest,
            "operation_id": self.operation_id,
            "schema_version": TRELLIS_PORT_SCHEMA_VERSION,
            "task_id": self.task_id,
            "trellis_task_id": self.trellis_task_id,
        }


class TrellisLifecycleState(StrEnum):
    ABSENT = "absent"
    PREPARED = "prepared"
    CHECKED = "checked"
    FINISHED = "finished"
    UNKNOWN = "unknown"


def _observation_common(
    operation_id: object,
    status: object,
    observed_at: object,
    effect_digest: object,
    evidence: object,
) -> tuple[str, EffectStatus, str, str | None, tuple[str, ...]]:
    normalized_id = _token(operation_id, "operation_id")
    if type(status) is not EffectStatus:
        raise TypeError("status must be an EffectStatus")
    normalized_time = _timestamp(observed_at, "observed_at")
    normalized_digest = _optional_digest(effect_digest, "effect_digest")
    normalized_evidence = _evidence(evidence)
    if status is EffectStatus.ABSENT and normalized_digest is not None:
        raise ValueError("an absent observation cannot claim an effect digest")
    if status is EffectStatus.APPLIED and normalized_digest is None:
        raise ValueError("an applied observation requires an effect digest")
    if status is EffectStatus.UNKNOWN and not normalized_evidence:
        raise ValueError("an unknown observation requires evidence")
    return (
        normalized_id,
        status,
        normalized_time,
        normalized_digest,
        normalized_evidence,
    )


@dataclass(frozen=True, slots=True)
class AttemptObservation(CanonicalTrellisContract):
    operation_id: str
    status: EffectStatus
    observed_at: str
    lifecycle_state: TrellisLifecycleState
    effect_digest: str | None = None
    attempt_id: str | None = None
    trellis_task_id: str | None = None
    worktree_id: str | None = None
    worktree_path: str | None = None
    base_commit: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = _observation_common(
            self.operation_id,
            self.status,
            self.observed_at,
            self.effect_digest,
            self.evidence,
        )
        for field_name, value in zip(
            ("operation_id", "status", "observed_at", "effect_digest", "evidence"),
            values,
            strict=True,
        ):
            object.__setattr__(self, field_name, value)
        if type(self.lifecycle_state) is not TrellisLifecycleState:
            raise TypeError("lifecycle_state must be a TrellisLifecycleState")
        for field_name in ("attempt_id", "worktree_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _token(value, field_name))
        if self.trellis_task_id is not None:
            object.__setattr__(
                self,
                "trellis_task_id",
                _text(self.trellis_task_id, "trellis_task_id", max_length=512),
            )
        if self.worktree_path is not None:
            object.__setattr__(
                self,
                "worktree_path",
                _text(self.worktree_path, "worktree_path", max_length=1024),
            )
        if self.base_commit is not None:
            object.__setattr__(self, "base_commit", _commit(self.base_commit, "base_commit"))
        identity_values = (
            self.attempt_id,
            self.trellis_task_id,
            self.worktree_id,
            self.worktree_path,
            self.base_commit,
        )
        if self.status is EffectStatus.ABSENT:
            if self.lifecycle_state is not TrellisLifecycleState.ABSENT or any(
                value is not None for value in identity_values
            ):
                raise ValueError("an absent attempt observation cannot claim attempt state")
        elif self.status is EffectStatus.UNKNOWN:
            if self.lifecycle_state is not TrellisLifecycleState.UNKNOWN:
                raise ValueError("an unknown attempt observation requires unknown state")
        elif self.lifecycle_state in {
            TrellisLifecycleState.ABSENT,
            TrellisLifecycleState.UNKNOWN,
        }:
            raise ValueError("an applied attempt requires a concrete lifecycle state")
        elif self.attempt_id is None or self.trellis_task_id is None:
            raise ValueError("an applied attempt requires attempt and task identities")
        if self.lifecycle_state is TrellisLifecycleState.PREPARED and any(
            value is None for value in (self.worktree_id, self.worktree_path, self.base_commit)
        ):
            raise ValueError("a prepared attempt requires complete worktree identity")

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "base_commit": self.base_commit,
            "effect_digest": self.effect_digest,
            "evidence": list(self.evidence),
            "lifecycle_state": self.lifecycle_state.value,
            "observed_at": self.observed_at,
            "operation_id": self.operation_id,
            "schema_version": TRELLIS_PORT_SCHEMA_VERSION,
            "status": self.status.value,
            "trellis_task_id": self.trellis_task_id,
            "worktree_id": self.worktree_id,
            "worktree_path": self.worktree_path,
        }


@dataclass(frozen=True, slots=True)
class CheckObservation(CanonicalTrellisContract):
    operation_id: str
    status: EffectStatus
    observed_at: str
    effect_digest: str | None = None
    attempt_id: str | None = None
    passed: bool | None = None
    head_commit: str | None = None
    check_digest: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = _observation_common(
            self.operation_id,
            self.status,
            self.observed_at,
            self.effect_digest,
            self.evidence,
        )
        for field_name, value in zip(
            ("operation_id", "status", "observed_at", "effect_digest", "evidence"),
            values,
            strict=True,
        ):
            object.__setattr__(self, field_name, value)
        if self.attempt_id is not None:
            object.__setattr__(self, "attempt_id", _token(self.attempt_id, "attempt_id"))
        if self.passed is not None and type(self.passed) is not bool:
            raise TypeError("passed must be a boolean or null")
        if self.head_commit is not None:
            object.__setattr__(self, "head_commit", _commit(self.head_commit, "head_commit"))
        object.__setattr__(
            self, "check_digest", _optional_digest(self.check_digest, "check_digest")
        )
        facts = (self.attempt_id, self.passed, self.head_commit, self.check_digest)
        if self.status is EffectStatus.APPLIED and any(value is None for value in facts):
            raise ValueError("an applied check requires complete check facts")
        if self.status is EffectStatus.ABSENT and any(value is not None for value in facts):
            raise ValueError("an absent check cannot claim check facts")

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "check_digest": self.check_digest,
            "effect_digest": self.effect_digest,
            "evidence": list(self.evidence),
            "head_commit": self.head_commit,
            "observed_at": self.observed_at,
            "operation_id": self.operation_id,
            "passed": self.passed,
            "schema_version": TRELLIS_PORT_SCHEMA_VERSION,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class FinishObservation(CanonicalTrellisContract):
    operation_id: str
    status: EffectStatus
    observed_at: str
    effect_digest: str | None = None
    attempt_id: str | None = None
    finished: bool | None = None
    delivered_commit: str | None = None
    finish_digest: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = _observation_common(
            self.operation_id,
            self.status,
            self.observed_at,
            self.effect_digest,
            self.evidence,
        )
        for field_name, value in zip(
            ("operation_id", "status", "observed_at", "effect_digest", "evidence"),
            values,
            strict=True,
        ):
            object.__setattr__(self, field_name, value)
        if self.attempt_id is not None:
            object.__setattr__(self, "attempt_id", _token(self.attempt_id, "attempt_id"))
        if self.finished is not None and type(self.finished) is not bool:
            raise TypeError("finished must be a boolean or null")
        if self.delivered_commit is not None:
            object.__setattr__(
                self,
                "delivered_commit",
                _commit(self.delivered_commit, "delivered_commit"),
            )
        object.__setattr__(
            self, "finish_digest", _optional_digest(self.finish_digest, "finish_digest")
        )
        facts = (
            self.attempt_id,
            self.finished,
            self.delivered_commit,
            self.finish_digest,
        )
        if self.status is EffectStatus.APPLIED and any(value is None for value in facts):
            raise ValueError("an applied finish requires complete finish facts")
        if self.status is EffectStatus.ABSENT and any(value is not None for value in facts):
            raise ValueError("an absent finish cannot claim finish facts")

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "delivered_commit": self.delivered_commit,
            "effect_digest": self.effect_digest,
            "evidence": list(self.evidence),
            "finish_digest": self.finish_digest,
            "finished": self.finished,
            "observed_at": self.observed_at,
            "operation_id": self.operation_id,
            "schema_version": TRELLIS_PORT_SCHEMA_VERSION,
            "status": self.status.value,
        }


@runtime_checkable
class TrellisGraphPort(Protocol):
    def export_snapshot(self, parent_task_id: str) -> TrellisGraphSnapshot: ...


@runtime_checkable
class TrellisLifecyclePort(Protocol):
    def prepare_attempt(
        self, effect: PreparedEffect[PrepareAttempt]
    ) -> AttemptObservation: ...

    def check_attempt(
        self, effect: PreparedEffect[CheckAttempt]
    ) -> CheckObservation: ...

    def finish_attempt(
        self, effect: PreparedEffect[FinishAttempt]
    ) -> FinishObservation: ...

    def inspect_attempt(
        self,
        operation_id: str,
        *,
        expected_request_payload_hash: str | None = None,
    ) -> AttemptObservation: ...

    def inspect_check(
        self,
        operation_id: str,
        *,
        expected_request_payload_hash: str | None = None,
    ) -> CheckObservation: ...

    def inspect_finish(
        self,
        operation_id: str,
        *,
        expected_request_payload_hash: str | None = None,
    ) -> FinishObservation: ...


__all__ = [
    "AttemptObservation",
    "CanonicalTrellisContract",
    "CheckAttempt",
    "CheckObservation",
    "FinishAttempt",
    "FinishObservation",
    "MAX_GRAPH_SNAPSHOT_BYTES",
    "PrepareAttempt",
    "TRELLIS_PORT_SCHEMA_VERSION",
    "TrellisGraphPort",
    "TrellisGraphSnapshot",
    "TrellisLifecyclePort",
    "TrellisLifecycleState",
]
