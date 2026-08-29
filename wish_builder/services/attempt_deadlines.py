"""Restart-safe attempt deadlines and evidence-bound reconciliation.

Monotonic values are meaningful only inside the process continuity that
captured them.  UTC timestamps in this module are diagnostic metadata and are
never consulted for expiry or recovery admission.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum

from wish_builder.contracts import canonical_json_bytes
from wish_builder.contracts.runtime import (
    EffectOperation,
    EffectReceipt,
    EffectStatus,
    EvidenceRef,
    EvidenceRole,
    EvidenceType,
    ExecutionIdentity,
)


def _token(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return value


def _positive(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _monotonic(value: object, field_name: str) -> float:
    if (
        type(value) not in {int, float}
        or type(value) is bool
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return float(value)


def _utc_timestamp(value: object, field_name: str) -> str:
    text = _token(value, field_name)
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _attempt_identity(value: object, field_name: str) -> ExecutionIdentity:
    if (
        type(value) is not ExecutionIdentity
        or not value.is_attempt
        or value.correlation_id is None
    ):
        raise ValueError(f"{field_name} must be a complete attempt identity")
    return value


@dataclass(frozen=True, slots=True)
class AttemptClockContinuity:
    """Identity of the one process timeline where monotonic values are comparable."""

    boot_id: str
    pid: int
    process_start_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "boot_id", _token(self.boot_id, "boot_id"))
        object.__setattr__(self, "pid", _positive(self.pid, "pid"))
        object.__setattr__(
            self,
            "process_start_id",
            _token(self.process_start_id, "process_start_id"),
        )


@dataclass(frozen=True, slots=True)
class AttemptClockSample:
    continuity: AttemptClockContinuity
    monotonic_value: float
    observed_at_utc: str

    def __post_init__(self) -> None:
        if type(self.continuity) is not AttemptClockContinuity:
            raise TypeError("continuity must be an AttemptClockContinuity")
        object.__setattr__(
            self,
            "monotonic_value",
            _monotonic(self.monotonic_value, "monotonic_value"),
        )
        object.__setattr__(
            self,
            "observed_at_utc",
            _utc_timestamp(self.observed_at_utc, "observed_at_utc"),
        )


@dataclass(frozen=True, slots=True)
class AttemptDeadline:
    identity: ExecutionIdentity
    continuity: AttemptClockContinuity
    recorded_monotonic: float
    deadline_monotonic: float
    recorded_at_utc: str
    diagnostic_deadline_at_utc: str
    last_observed_monotonic: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "identity", _attempt_identity(self.identity, "identity")
        )
        if type(self.continuity) is not AttemptClockContinuity:
            raise TypeError("continuity must be an AttemptClockContinuity")
        recorded = _monotonic(self.recorded_monotonic, "recorded_monotonic")
        deadline = _monotonic(self.deadline_monotonic, "deadline_monotonic")
        if deadline <= recorded:
            raise ValueError("deadline_monotonic must follow recorded_monotonic")
        object.__setattr__(self, "recorded_monotonic", recorded)
        object.__setattr__(self, "deadline_monotonic", deadline)
        high_water = (
            recorded
            if self.last_observed_monotonic is None
            else _monotonic(
                self.last_observed_monotonic,
                "last_observed_monotonic",
            )
        )
        if high_water < recorded:
            raise ValueError(
                "last_observed_monotonic cannot precede recorded_monotonic"
            )
        object.__setattr__(self, "last_observed_monotonic", high_water)
        object.__setattr__(
            self,
            "recorded_at_utc",
            _utc_timestamp(self.recorded_at_utc, "recorded_at_utc"),
        )
        object.__setattr__(
            self,
            "diagnostic_deadline_at_utc",
            _utc_timestamp(
                self.diagnostic_deadline_at_utc,
                "diagnostic_deadline_at_utc",
            ),
        )

    @property
    def monotonic_high_water(self) -> float:
        value = self.last_observed_monotonic
        assert value is not None
        return value


class AttemptDeadlineState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class AttemptDeadlineReason(StrEnum):
    BEFORE_DEADLINE = "before_deadline"
    DEADLINE_REACHED = "deadline_reached"
    BOOT_CHANGED = "boot_changed"
    PROCESS_IDENTITY_CHANGED = "process_identity_changed"
    MONOTONIC_ROLLBACK = "monotonic_rollback"


@dataclass(frozen=True, slots=True)
class AttemptActionPermissions:
    retry_allowed: bool = False
    takeover_allowed: bool = False
    cleanup_allowed: bool = False
    dispatch_allowed: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "retry_allowed",
            "takeover_allowed",
            "cleanup_allowed",
            "dispatch_allowed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be a bool")

    @property
    def any_allowed(self) -> bool:
        return any(
            (
                self.retry_allowed,
                self.takeover_allowed,
                self.cleanup_allowed,
                self.dispatch_allowed,
            )
        )


_NO_ACTIONS = AttemptActionPermissions()
_ABSENT_ACTIONS = AttemptActionPermissions(
    retry_allowed=True,
    cleanup_allowed=True,
    dispatch_allowed=True,
)
# Coordinator lease takeover remains owned by the lease recovery service.  An
# attempt outcome, even when fully reconciled, is not lease-owner death proof.
_APPLIED_ACTIONS = AttemptActionPermissions(cleanup_allowed=True)


@dataclass(frozen=True, slots=True)
class AttemptDeadlineAssessment:
    identity: ExecutionIdentity
    state: AttemptDeadlineState
    reason: AttemptDeadlineReason
    updated_deadline: AttemptDeadline
    permissions: AttemptActionPermissions = _NO_ACTIONS

    def __post_init__(self) -> None:
        _attempt_identity(self.identity, "identity")
        if type(self.state) is not AttemptDeadlineState:
            raise TypeError("state must be an AttemptDeadlineState")
        if type(self.reason) is not AttemptDeadlineReason:
            raise TypeError("reason must be an AttemptDeadlineReason")
        if (
            type(self.updated_deadline) is not AttemptDeadline
            or self.updated_deadline.identity != self.identity
        ):
            raise ValueError("updated_deadline must bind to identity")
        if type(self.permissions) is not AttemptActionPermissions:
            raise TypeError("permissions must be AttemptActionPermissions")
        if self.permissions.any_allowed:
            raise ValueError("deadline assessment cannot authorize an action")
        expected_state = {
            AttemptDeadlineReason.BEFORE_DEADLINE: AttemptDeadlineState.ACTIVE,
            AttemptDeadlineReason.DEADLINE_REACHED: AttemptDeadlineState.EXPIRED,
            AttemptDeadlineReason.BOOT_CHANGED: (
                AttemptDeadlineState.RECONCILIATION_REQUIRED
            ),
            AttemptDeadlineReason.PROCESS_IDENTITY_CHANGED: (
                AttemptDeadlineState.RECONCILIATION_REQUIRED
            ),
            AttemptDeadlineReason.MONOTONIC_ROLLBACK: (
                AttemptDeadlineState.RECONCILIATION_REQUIRED
            ),
        }[self.reason]
        if self.state is not expected_state:
            raise ValueError("deadline state does not match its reason")

    @property
    def reconciliation_required(self) -> bool:
        return self.state in {
            AttemptDeadlineState.EXPIRED,
            AttemptDeadlineState.RECONCILIATION_REQUIRED,
        }


def evaluate_attempt_deadline(
    deadline: AttemptDeadline,
    sample: AttemptClockSample,
) -> AttemptDeadlineAssessment:
    """Evaluate a deadline without granting recovery or mutation authority."""

    if type(deadline) is not AttemptDeadline:
        raise TypeError("deadline must be an AttemptDeadline")
    if type(sample) is not AttemptClockSample:
        raise TypeError("sample must be an AttemptClockSample")

    captured = deadline.continuity
    current = sample.continuity
    if current.boot_id != captured.boot_id:
        state = AttemptDeadlineState.RECONCILIATION_REQUIRED
        reason = AttemptDeadlineReason.BOOT_CHANGED
    elif (
        current.pid != captured.pid
        or current.process_start_id != captured.process_start_id
    ):
        state = AttemptDeadlineState.RECONCILIATION_REQUIRED
        reason = AttemptDeadlineReason.PROCESS_IDENTITY_CHANGED
    elif sample.monotonic_value < deadline.monotonic_high_water:
        state = AttemptDeadlineState.RECONCILIATION_REQUIRED
        reason = AttemptDeadlineReason.MONOTONIC_ROLLBACK
    elif sample.monotonic_value >= deadline.deadline_monotonic:
        state = AttemptDeadlineState.EXPIRED
        reason = AttemptDeadlineReason.DEADLINE_REACHED
    else:
        state = AttemptDeadlineState.ACTIVE
        reason = AttemptDeadlineReason.BEFORE_DEADLINE
    updated_deadline = deadline
    if state is not AttemptDeadlineState.RECONCILIATION_REQUIRED:
        updated_deadline = replace(
            deadline,
            last_observed_monotonic=sample.monotonic_value,
        )
    return AttemptDeadlineAssessment(
        deadline.identity,
        state,
        reason,
        updated_deadline,
    )


@dataclass(frozen=True, slots=True)
class AttemptReconciliationEvidence:
    """All observations required to resolve one exact timed-out attempt."""

    identity: ExecutionIdentity
    expected_operation: EffectOperation
    outcome_receipts: tuple[EffectReceipt, ...]
    termination_receipts: tuple[EffectReceipt, ...]
    process_tree_termination_proven: bool
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "identity", _attempt_identity(self.identity, "identity")
        )
        if type(self.expected_operation) is not EffectOperation:
            raise TypeError("expected_operation must be an EffectOperation")
        if self.expected_operation in {
            EffectOperation.PROCESS_TERMINATION,
            EffectOperation.CLEANUP,
        }:
            raise ValueError("expected_operation must describe the attempt outcome")
        for field_name in ("outcome_receipts", "termination_receipts"):
            values = getattr(self, field_name)
            if type(values) is not tuple or not all(
                type(item) is EffectReceipt for item in values
            ):
                raise TypeError(
                    f"{field_name} must be a tuple of EffectReceipt values"
                )
        if type(self.process_tree_termination_proven) is not bool:
            raise TypeError("process_tree_termination_proven must be a bool")
        if type(self.evidence) is not tuple or not all(
            type(item) is EvidenceRef for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of EvidenceRef values")
        if len({item.digest for item in self.evidence}) != len(self.evidence):
            raise ValueError("evidence must not repeat a digest")


class AttemptReconciliationStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"


class AttemptReconciliationReason(StrEnum):
    ABSENT_CONFIRMED = "absent_confirmed"
    APPLIED_CONFIRMED = "applied_confirmed"
    OUTCOME_MISSING = "outcome_missing"
    OUTCOME_UNKNOWN = "outcome_unknown"
    TERMINATION_MISSING = "termination_missing"
    TERMINATION_INCOMPLETE = "termination_incomplete"
    EVIDENCE_MISSING = "evidence_missing"
    IDENTITY_MISMATCH = "identity_mismatch"
    OPERATION_MISMATCH = "operation_mismatch"
    CONFLICTING_EVIDENCE = "conflicting_evidence"


@dataclass(frozen=True, slots=True)
class AttemptReconciliationResult:
    identity: ExecutionIdentity
    status: AttemptReconciliationStatus
    reason: AttemptReconciliationReason
    evidence_fingerprint: str
    outcome_status: EffectStatus | None = None
    permissions: AttemptActionPermissions = _NO_ACTIONS
    idempotent: bool = False

    def __post_init__(self) -> None:
        _attempt_identity(self.identity, "identity")
        if type(self.status) is not AttemptReconciliationStatus:
            raise TypeError("status must be an AttemptReconciliationStatus")
        if type(self.reason) is not AttemptReconciliationReason:
            raise TypeError("reason must be an AttemptReconciliationReason")
        if (
            type(self.evidence_fingerprint) is not str
            or len(self.evidence_fingerprint) != 71
            or not self.evidence_fingerprint.startswith("sha256:")
        ):
            raise ValueError("evidence_fingerprint must be a sha256 reference")
        if (
            self.outcome_status is not None
            and type(self.outcome_status) is not EffectStatus
        ):
            raise TypeError("outcome_status must be an EffectStatus or null")
        if type(self.permissions) is not AttemptActionPermissions:
            raise TypeError("permissions must be AttemptActionPermissions")
        if type(self.idempotent) is not bool:
            raise TypeError("idempotent must be a bool")

        completed = self.status is AttemptReconciliationStatus.COMPLETED
        if self.idempotent and not completed:
            raise ValueError("only a completed reconciliation may be idempotent")
        if not completed:
            if self.permissions.any_allowed:
                raise ValueError("blocked reconciliation cannot authorize an action")
            if self.reason in {
                AttemptReconciliationReason.ABSENT_CONFIRMED,
                AttemptReconciliationReason.APPLIED_CONFIRMED,
            }:
                raise ValueError("blocked reconciliation cannot confirm an outcome")
            return
        expected = {
            AttemptReconciliationReason.ABSENT_CONFIRMED: (
                EffectStatus.ABSENT,
                _ABSENT_ACTIONS,
            ),
            AttemptReconciliationReason.APPLIED_CONFIRMED: (
                EffectStatus.APPLIED,
                _APPLIED_ACTIONS,
            ),
        }
        if self.reason not in expected:
            raise ValueError("completed reconciliation requires a confirmed outcome")
        expected_status, expected_permissions = expected[self.reason]
        if (
            self.outcome_status is not expected_status
            or self.permissions != expected_permissions
        ):
            raise ValueError("completed reconciliation has unsafe permissions")


class AttemptReconciliationConflict(RuntimeError):
    """A completed reconciliation was presented with different evidence."""


def _receipt_primitives(receipts: tuple[EffectReceipt, ...]) -> list[dict[str, object]]:
    unique = {
        canonical_json_bytes(receipt.to_primitive()): receipt.to_primitive()
        for receipt in receipts
    }
    return [unique[key] for key in sorted(unique)]


def _fingerprint(bundle: AttemptReconciliationEvidence) -> str:
    primitive = {
        "evidence": [
            item.to_primitive()
            for item in sorted(bundle.evidence, key=lambda item: item.digest)
        ],
        "expected_operation": bundle.expected_operation.value,
        "identity": bundle.identity.to_primitive(),
        "outcome_receipts": _receipt_primitives(bundle.outcome_receipts),
        "process_tree_termination_proven": bundle.process_tree_termination_proven,
        "termination_receipts": _receipt_primitives(bundle.termination_receipts),
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(primitive)).hexdigest()


def _one_unique(
    receipts: tuple[EffectReceipt, ...],
) -> tuple[EffectReceipt | None, bool]:
    unique: dict[bytes, EffectReceipt] = {}
    for receipt in receipts:
        unique[canonical_json_bytes(receipt.to_primitive())] = receipt
    if not unique:
        return None, False
    if len(unique) != 1:
        return None, True
    return next(iter(unique.values())), False


def _blocked(
    bundle: AttemptReconciliationEvidence,
    fingerprint: str,
    reason: AttemptReconciliationReason,
    outcome_status: EffectStatus | None = None,
) -> AttemptReconciliationResult:
    return AttemptReconciliationResult(
        bundle.identity,
        AttemptReconciliationStatus.BLOCKED,
        reason,
        fingerprint,
        outcome_status,
    )


def reconcile_attempt_deadline(
    bundle: AttemptReconciliationEvidence,
    *,
    previous: AttemptReconciliationResult | None = None,
) -> AttemptReconciliationResult:
    """Admit recovery actions only from exact, complete, non-conflicting proof."""

    if type(bundle) is not AttemptReconciliationEvidence:
        raise TypeError("bundle must be AttemptReconciliationEvidence")
    if previous is not None and type(previous) is not AttemptReconciliationResult:
        raise TypeError("previous must be an AttemptReconciliationResult or null")
    fingerprint = _fingerprint(bundle)
    if (
        previous is not None
        and previous.status is AttemptReconciliationStatus.COMPLETED
    ):
        if (
            previous.identity != bundle.identity
            or previous.evidence_fingerprint != fingerprint
        ):
            raise AttemptReconciliationConflict(
                "completed reconciliation conflicts with new evidence"
            )
        return replace(previous, idempotent=True)

    outcome, outcome_conflict = _one_unique(bundle.outcome_receipts)
    if outcome_conflict:
        return _blocked(
            bundle, fingerprint, AttemptReconciliationReason.CONFLICTING_EVIDENCE
        )
    if outcome is None:
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.OUTCOME_MISSING,
        )
    if outcome.identity != bundle.identity:
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.IDENTITY_MISMATCH,
            outcome.status,
        )
    if outcome.operation is not bundle.expected_operation:
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.OPERATION_MISMATCH,
            outcome.status,
        )
    if outcome.status is EffectStatus.UNKNOWN:
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.OUTCOME_UNKNOWN,
            outcome.status,
        )

    termination, termination_conflict = _one_unique(bundle.termination_receipts)
    if termination_conflict:
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.CONFLICTING_EVIDENCE,
            outcome.status,
        )
    if termination is None:
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.TERMINATION_MISSING,
            outcome.status,
        )
    if termination.identity != bundle.identity:
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.IDENTITY_MISMATCH,
            outcome.status,
        )
    if (
        termination.operation is not EffectOperation.PROCESS_TERMINATION
        or termination.status is not EffectStatus.APPLIED
        or not bundle.process_tree_termination_proven
    ):
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.TERMINATION_INCOMPLETE,
            outcome.status,
        )

    if not bundle.evidence or any(
        item.producer.identity != bundle.identity for item in bundle.evidence
    ):
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.EVIDENCE_MISSING,
            outcome.status,
        )
    required_types = {
        item.evidence_type
        for item in bundle.evidence
        if item.role is EvidenceRole.REQUIRED
    }
    if not {EvidenceType.EFFECT_RECEIPT, EvidenceType.PROCESS}.issubset(
        required_types
    ):
        return _blocked(
            bundle,
            fingerprint,
            AttemptReconciliationReason.EVIDENCE_MISSING,
            outcome.status,
        )

    if outcome.status is EffectStatus.ABSENT:
        return AttemptReconciliationResult(
            bundle.identity,
            AttemptReconciliationStatus.COMPLETED,
            AttemptReconciliationReason.ABSENT_CONFIRMED,
            fingerprint,
            outcome.status,
            _ABSENT_ACTIONS,
        )
    return AttemptReconciliationResult(
        bundle.identity,
        AttemptReconciliationStatus.COMPLETED,
        AttemptReconciliationReason.APPLIED_CONFIRMED,
        fingerprint,
        outcome.status,
        _APPLIED_ACTIONS,
    )


__all__ = [
    "AttemptActionPermissions",
    "AttemptClockContinuity",
    "AttemptClockSample",
    "AttemptDeadline",
    "AttemptDeadlineAssessment",
    "AttemptDeadlineReason",
    "AttemptDeadlineState",
    "AttemptReconciliationConflict",
    "AttemptReconciliationEvidence",
    "AttemptReconciliationReason",
    "AttemptReconciliationResult",
    "AttemptReconciliationStatus",
    "evaluate_attempt_deadline",
    "reconcile_attempt_deadline",
]
