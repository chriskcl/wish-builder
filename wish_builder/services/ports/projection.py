"""Typed boundary for repairable Trellis task projections.

The projection is deliberately a derived view.  A caller must provide a
durable canonical Journal position before this port can be asked to write
anything to Trellis.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from wish_builder.contracts.models import HASH_RE

TRELLIS_PROJECTION_SCHEMA_VERSION = 1
MAX_PROJECTION_EVIDENCE = 32
MAX_PROJECTION_SUMMARY = 1024
MAX_PROJECTION_TASK_ID = 512
MAX_PROJECTION_TOKEN = 256

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")


class TrellisProjectionDisposition(StrEnum):
    INSPECTED = "inspected"
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class TrellisProjectionReason(StrEnum):
    NONE = "none"
    REVISION_CONFLICT = "revision_conflict"
    AHEAD = "ahead"
    DIGEST_MISMATCH = "digest_mismatch"
    IDENTITY_MISMATCH = "identity_mismatch"
    STATUS_MISMATCH = "status_mismatch"
    TASK_MISSING = "projection_task_missing"
    CHECKOUT_MISSING = "projection_checkout_missing"
    CHECKOUT_UNSAFE = "projection_checkout_unsafe"
    CHECKOUT_NOT_GIT = "projection_checkout_not_git"
    CHECKOUT_UNAVAILABLE = "projection_checkout_unavailable"
    UNAVAILABLE = "projection_unavailable"
    CANONICAL_NOT_DURABLE = "canonical_not_durable"
    INVALID = "projection_invalid"


def _text(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError(f"{field} is invalid")
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    if normalized != value or not value.strip():
        raise ValueError(f"{field} is not stable text")
    if any(
        (ord(character) < 32 or 0x7F <= ord(character) <= 0x9F)
        for character in value
    ):
        raise ValueError(f"{field} contains a control character")
    return value


def _token(value: object, field: str) -> str:
    result = _text(value, field, MAX_PROJECTION_TOKEN)
    if not _TOKEN_RE.fullmatch(result):
        raise ValueError(f"{field} is not a stable token")
    return result


def _digest(value: object, field: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field} must be a sha256 reference")
    return value


@dataclass(frozen=True, slots=True)
class TrellisProjection:
    """One deterministic projection derived from a canonical transition."""

    schema_version: int
    operation_id: str
    run_id: str
    task_id: str
    trellis_task_id: str
    manifest_digest: str
    trellis_graph_digest: str
    canonical_sequence: int
    canonical_event_hash: str
    canonical_state: str
    target_status: str
    evidence_digests: tuple[str, ...]
    summary: str

    def __post_init__(self) -> None:
        if self.schema_version != TRELLIS_PROJECTION_SCHEMA_VERSION:
            raise ValueError("unsupported Trellis projection schema")
        for field in ("operation_id", "run_id", "task_id"):
            object.__setattr__(self, field, _token(getattr(self, field), field))
        object.__setattr__(
            self,
            "trellis_task_id",
            _text(self.trellis_task_id, "trellis_task_id", MAX_PROJECTION_TASK_ID),
        )
        for field in (
            "manifest_digest",
            "trellis_graph_digest",
            "canonical_event_hash",
        ):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        if type(self.canonical_sequence) is not int or self.canonical_sequence < 1:
            raise ValueError("canonical_sequence must be positive")
        for field in ("canonical_state", "target_status"):
            object.__setattr__(self, field, _token(getattr(self, field), field))
        if type(self.evidence_digests) is not tuple:
            raise TypeError("evidence_digests must be a tuple")
        if len(self.evidence_digests) > MAX_PROJECTION_EVIDENCE:
            raise ValueError("evidence_digests exceeds the limit")
        evidence = tuple(
            sorted(
                (_digest(value, "evidence_digest") for value in self.evidence_digests),
                key=lambda value: value.encode("utf-8"),
            )
        )
        if len(set(evidence)) != len(evidence):
            raise ValueError("evidence_digests must be unique")
        object.__setattr__(self, "evidence_digests", evidence)
        object.__setattr__(
            self, "summary", _text(self.summary, "summary", MAX_PROJECTION_SUMMARY)
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "operationId": self.operation_id,
            "runId": self.run_id,
            "taskId": self.task_id,
            "trellisTaskId": self.trellis_task_id,
            "manifestDigest": self.manifest_digest,
            "trellisGraphDigest": self.trellis_graph_digest,
            "canonicalSequence": self.canonical_sequence,
            "canonicalEventHash": self.canonical_event_hash,
            "canonicalState": self.canonical_state,
            "targetStatus": self.target_status,
            "evidenceDigests": list(self.evidence_digests),
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class TrellisProjectionObservation:
    disposition: TrellisProjectionDisposition
    reason: TrellisProjectionReason
    record_revision: str | None = None
    byte_length: int | None = None
    task_status: str | None = None
    projection: TrellisProjection | None = None

    def __post_init__(self) -> None:
        if type(self.disposition) is not TrellisProjectionDisposition:
            raise TypeError("disposition must be a TrellisProjectionDisposition")
        if type(self.reason) is not TrellisProjectionReason:
            raise TypeError("reason must be a TrellisProjectionReason")
        if self.record_revision is not None:
            object.__setattr__(
                self,
                "record_revision",
                _digest(self.record_revision, "record_revision"),
            )
        if self.byte_length is not None and (
            type(self.byte_length) is not int or self.byte_length < 0
        ):
            raise ValueError("byte_length must be non-negative")
        if self.task_status is not None:
            object.__setattr__(self, "task_status", _text(self.task_status, "task_status", 256))
        if self.projection is not None and type(self.projection) is not TrellisProjection:
            raise TypeError("projection must be a TrellisProjection")
        if self.disposition in {
            TrellisProjectionDisposition.APPLIED,
            TrellisProjectionDisposition.IDEMPOTENT,
            TrellisProjectionDisposition.INSPECTED,
        } and self.reason is not TrellisProjectionReason.NONE:
            raise ValueError("successful projection observations require reason=none")
        if self.disposition is TrellisProjectionDisposition.UNAVAILABLE:
            if self.reason is TrellisProjectionReason.NONE:
                raise ValueError("unavailable projection requires a reason")


@dataclass(frozen=True, slots=True)
class TrellisProjectionApplyRequest:
    checkout_root: Path
    trellis_task_id: str
    expected_revision: str
    projection: TrellisProjection

    def __post_init__(self) -> None:
        if not isinstance(self.checkout_root, Path) or not self.checkout_root.is_absolute():
            raise ValueError("checkout_root must be an absolute Path")
        object.__setattr__(
            self,
            "trellis_task_id",
            _text(self.trellis_task_id, "trellis_task_id", MAX_PROJECTION_TASK_ID),
        )
        object.__setattr__(
            self,
            "expected_revision",
            _digest(self.expected_revision, "expected_revision"),
        )
        if type(self.projection) is not TrellisProjection:
            raise TypeError("projection must be a TrellisProjection")
        if self.projection.trellis_task_id != self.trellis_task_id:
            raise ValueError("projection task identity does not match request")


@runtime_checkable
class TrellisProjectionPort(Protocol):
    def inspect(
        self, checkout_root: Path, trellis_task_id: str
    ) -> TrellisProjectionObservation: ...

    def apply(
        self, request: TrellisProjectionApplyRequest
    ) -> TrellisProjectionObservation: ...


__all__ = [
    "MAX_PROJECTION_EVIDENCE",
    "MAX_PROJECTION_SUMMARY",
    "TrellisProjection",
    "TrellisProjectionApplyRequest",
    "TrellisProjectionDisposition",
    "TrellisProjectionObservation",
    "TrellisProjectionPort",
    "TrellisProjectionReason",
    "TRELLIS_PROJECTION_SCHEMA_VERSION",
]
