from __future__ import annotations

import hashlib
import unittest
from dataclasses import FrozenInstanceError, replace

from wish_builder.contracts import (
    EffectOperation,
    EffectReceipt,
    EffectStatus,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
)
from wish_builder.services.attempt_deadlines import (
    AttemptClockContinuity,
    AttemptClockSample,
    AttemptDeadline,
    AttemptDeadlineReason,
    AttemptDeadlineState,
    AttemptReconciliationConflict,
    AttemptReconciliationEvidence,
    AttemptReconciliationReason,
    AttemptReconciliationStatus,
    evaluate_attempt_deadline,
    reconcile_attempt_deadline,
)


RECORDED_AT = "2026-08-19T01:00:00Z"
DIAGNOSTIC_DEADLINE = "2026-08-19T01:05:00Z"


def sha256(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def identity(*, correlation_id: str = "OP-001") -> ExecutionIdentity:
    return ExecutionIdentity("RUN-001", 1, "TASK-001", 1, correlation_id)


def continuity(
    *,
    boot_id: str = "boot-001",
    pid: int = 1001,
    process_start_id: str = "process-start-001",
) -> AttemptClockContinuity:
    return AttemptClockContinuity(boot_id, pid, process_start_id)


def deadline(
    *,
    diagnostic_deadline_at_utc: str = DIAGNOSTIC_DEADLINE,
) -> AttemptDeadline:
    return AttemptDeadline(
        identity(),
        continuity(),
        100.0,
        200.0,
        RECORDED_AT,
        diagnostic_deadline_at_utc,
    )


def sample(
    monotonic_value: float,
    *,
    clock_continuity: AttemptClockContinuity | None = None,
    observed_at_utc: str = RECORDED_AT,
) -> AttemptClockSample:
    return AttemptClockSample(
        clock_continuity or continuity(),
        monotonic_value,
        observed_at_utc,
    )


def evidence_ref(
    evidence_type: EvidenceType,
    ordinal: int,
    *,
    subject: ExecutionIdentity | None = None,
    role: EvidenceRole = EvidenceRole.REQUIRED,
) -> EvidenceRef:
    attempt = subject or identity()
    digest = sha256(f"{evidence_type.value}-{ordinal}-{attempt.correlation_id}")
    return EvidenceRef(
        1,
        digest,
        ordinal,
        evidence_type,
        EvidenceProducer(attempt, external_object_id=f"proof-{ordinal}"),
        RECORDED_AT,
        EvidenceSensitivity.INTERNAL,
        EvidenceRenderPolicy.METADATA_ONLY,
        role,
        digest,
    )


def receipt(
    status: EffectStatus,
    *,
    operation: EffectOperation = EffectOperation.TASK_EXECUTION,
    subject: ExecutionIdentity | None = None,
    ordinal: int = 1,
) -> EffectReceipt:
    attempt = subject or identity()
    receipt_evidence: tuple[EvidenceRef, ...] = ()
    if status is EffectStatus.UNKNOWN:
        receipt_evidence = (
            evidence_ref(EvidenceType.DIAGNOSTIC, 100 + ordinal, subject=attempt),
        )
    return EffectReceipt(
        1,
        attempt,
        operation,
        status,
        RECORDED_AT,
        sha256(f"effect-{ordinal}") if status is EffectStatus.APPLIED else None,
        f"object-{ordinal}" if status is EffectStatus.APPLIED else None,
        receipt_evidence,
    )


def termination_receipt(
    *,
    subject: ExecutionIdentity | None = None,
    ordinal: int = 1,
) -> EffectReceipt:
    return receipt(
        EffectStatus.APPLIED,
        operation=EffectOperation.PROCESS_TERMINATION,
        subject=subject,
        ordinal=ordinal,
    )


def reconciliation_evidence(
    status: EffectStatus = EffectStatus.ABSENT,
    *,
    outcome_receipts: tuple[EffectReceipt, ...] | None = None,
    termination_receipts: tuple[EffectReceipt, ...] | None = None,
    process_tree_termination_proven: bool = True,
    evidence: tuple[EvidenceRef, ...] | None = None,
) -> AttemptReconciliationEvidence:
    return AttemptReconciliationEvidence(
        identity(),
        EffectOperation.TASK_EXECUTION,
        outcome_receipts
        if outcome_receipts is not None
        else (receipt(status),),
        termination_receipts
        if termination_receipts is not None
        else (termination_receipt(),),
        process_tree_termination_proven,
        evidence
        if evidence is not None
        else (
            evidence_ref(EvidenceType.EFFECT_RECEIPT, 1),
            evidence_ref(EvidenceType.PROCESS, 2),
        ),
    )


class AttemptDeadlineTests(unittest.TestCase):
    def test_deadline_values_are_immutable_and_strictly_typed(self) -> None:
        value = deadline()
        with self.assertRaises(FrozenInstanceError):
            value.deadline_monotonic = 300.0  # type: ignore[misc]
        with self.assertRaises(ValueError):
            AttemptClockContinuity(
                "boot-001", True, "start-001"  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            replace(value, deadline_monotonic=value.recorded_monotonic)
        with self.assertRaises(ValueError):
            replace(value, identity=ExecutionIdentity("RUN-001", 1))

    def test_same_continuity_before_deadline_is_active(self) -> None:
        result = evaluate_attempt_deadline(deadline(), sample(199.999))

        self.assertIs(result.state, AttemptDeadlineState.ACTIVE)
        self.assertIs(result.reason, AttemptDeadlineReason.BEFORE_DEADLINE)
        self.assertFalse(result.reconciliation_required)
        self.assertFalse(result.permissions.any_allowed)

    def test_expired_deadline_authorizes_nothing(self) -> None:
        result = evaluate_attempt_deadline(deadline(), sample(200.0))

        self.assertIs(result.state, AttemptDeadlineState.EXPIRED)
        self.assertIs(result.reason, AttemptDeadlineReason.DEADLINE_REACHED)
        self.assertTrue(result.reconciliation_required)
        self.assertFalse(result.permissions.retry_allowed)
        self.assertFalse(result.permissions.takeover_allowed)
        self.assertFalse(result.permissions.cleanup_allowed)
        self.assertFalse(result.permissions.dispatch_allowed)

    def test_utc_metadata_never_drives_expiry(self) -> None:
        past_diagnostic = deadline(
            diagnostic_deadline_at_utc="2000-01-01T00:00:00Z"
        )
        active_with_future_wall_clock = evaluate_attempt_deadline(
            past_diagnostic,
            sample(150.0, observed_at_utc="2100-01-01T00:00:00Z"),
        )
        future_diagnostic = deadline(
            diagnostic_deadline_at_utc="2100-01-01T00:00:00Z"
        )
        expired_with_past_wall_clock = evaluate_attempt_deadline(
            future_diagnostic,
            sample(250.0, observed_at_utc="2000-01-01T00:00:00Z"),
        )

        self.assertIs(active_with_future_wall_clock.state, AttemptDeadlineState.ACTIVE)
        self.assertIs(expired_with_past_wall_clock.state, AttemptDeadlineState.EXPIRED)

    def test_boot_change_requires_reconciliation(self) -> None:
        result = evaluate_attempt_deadline(
            deadline(),
            sample(250.0, clock_continuity=continuity(boot_id="boot-002")),
        )

        self.assertIs(result.state, AttemptDeadlineState.RECONCILIATION_REQUIRED)
        self.assertIs(result.reason, AttemptDeadlineReason.BOOT_CHANGED)
        self.assertFalse(result.permissions.any_allowed)

    def test_pid_or_process_start_change_requires_reconciliation(self) -> None:
        cases = (
            continuity(pid=1002),
            continuity(process_start_id="process-start-002"),
        )
        for changed in cases:
            with self.subTest(changed=changed):
                result = evaluate_attempt_deadline(
                    deadline(), sample(250.0, clock_continuity=changed)
                )
                self.assertIs(
                    result.state, AttemptDeadlineState.RECONCILIATION_REQUIRED
                )
                self.assertIs(
                    result.reason,
                    AttemptDeadlineReason.PROCESS_IDENTITY_CHANGED,
                )
                self.assertFalse(result.permissions.any_allowed)

    def test_monotonic_rollback_requires_reconciliation(self) -> None:
        result = evaluate_attempt_deadline(deadline(), sample(99.999))

        self.assertIs(result.state, AttemptDeadlineState.RECONCILIATION_REQUIRED)
        self.assertIs(result.reason, AttemptDeadlineReason.MONOTONIC_ROLLBACK)
        self.assertFalse(result.permissions.any_allowed)

    def test_each_safe_observation_advances_an_immutable_high_water_mark(self) -> None:
        original = deadline()
        first = evaluate_attempt_deadline(original, sample(190.0))

        self.assertEqual(original.last_observed_monotonic, 100.0)
        self.assertEqual(first.updated_deadline.last_observed_monotonic, 190.0)

        rolled_back = evaluate_attempt_deadline(
            first.updated_deadline,
            sample(150.0),
        )
        self.assertIs(
            rolled_back.state,
            AttemptDeadlineState.RECONCILIATION_REQUIRED,
        )
        self.assertIs(
            rolled_back.reason,
            AttemptDeadlineReason.MONOTONIC_ROLLBACK,
        )
        self.assertEqual(
            rolled_back.updated_deadline.last_observed_monotonic,
            190.0,
        )

    def test_invalid_clock_samples_are_rejected(self) -> None:
        for value in (True, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sample(value)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            sample(150.0, observed_at_utc="2026-08-19T01:00:00")


class AttemptReconciliationTests(unittest.TestCase):
    def test_absent_outcome_and_complete_tree_allow_rerun_and_cleanup(self) -> None:
        result = reconcile_attempt_deadline(reconciliation_evidence())

        self.assertIs(result.status, AttemptReconciliationStatus.COMPLETED)
        self.assertIs(result.reason, AttemptReconciliationReason.ABSENT_CONFIRMED)
        self.assertIs(result.outcome_status, EffectStatus.ABSENT)
        self.assertTrue(result.permissions.retry_allowed)
        self.assertTrue(result.permissions.dispatch_allowed)
        self.assertTrue(result.permissions.cleanup_allowed)
        self.assertFalse(result.permissions.takeover_allowed)

    def test_applied_outcome_and_complete_tree_allow_cleanup_only(self) -> None:
        result = reconcile_attempt_deadline(
            reconciliation_evidence(EffectStatus.APPLIED)
        )

        self.assertIs(result.status, AttemptReconciliationStatus.COMPLETED)
        self.assertIs(result.reason, AttemptReconciliationReason.APPLIED_CONFIRMED)
        self.assertFalse(result.permissions.retry_allowed)
        self.assertFalse(result.permissions.dispatch_allowed)
        self.assertTrue(result.permissions.cleanup_allowed)
        self.assertFalse(result.permissions.takeover_allowed)

    def test_unknown_outcome_fails_closed_even_with_complete_termination(self) -> None:
        result = reconcile_attempt_deadline(
            reconciliation_evidence(EffectStatus.UNKNOWN)
        )

        self.assertIs(result.status, AttemptReconciliationStatus.BLOCKED)
        self.assertIs(result.reason, AttemptReconciliationReason.OUTCOME_UNKNOWN)
        self.assertFalse(result.permissions.any_allowed)

    def test_missing_outcome_or_termination_fails_closed(self) -> None:
        cases = (
            (
                reconciliation_evidence(outcome_receipts=()),
                AttemptReconciliationReason.OUTCOME_MISSING,
            ),
            (
                reconciliation_evidence(termination_receipts=()),
                AttemptReconciliationReason.TERMINATION_MISSING,
            ),
        )
        for bundle, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                result = reconcile_attempt_deadline(bundle)
                self.assertIs(result.status, AttemptReconciliationStatus.BLOCKED)
                self.assertIs(result.reason, expected_reason)
                self.assertFalse(result.permissions.any_allowed)

    def test_incomplete_process_tree_fails_closed(self) -> None:
        result = reconcile_attempt_deadline(
            reconciliation_evidence(process_tree_termination_proven=False)
        )

        self.assertIs(result.status, AttemptReconciliationStatus.BLOCKED)
        self.assertIs(
            result.reason, AttemptReconciliationReason.TERMINATION_INCOMPLETE
        )
        self.assertFalse(result.permissions.any_allowed)

    def test_non_applied_termination_receipt_fails_closed(self) -> None:
        result = reconcile_attempt_deadline(
            reconciliation_evidence(
                termination_receipts=(
                    receipt(
                        EffectStatus.ABSENT,
                        operation=EffectOperation.PROCESS_TERMINATION,
                    ),
                )
            )
        )

        self.assertIs(result.status, AttemptReconciliationStatus.BLOCKED)
        self.assertIs(
            result.reason, AttemptReconciliationReason.TERMINATION_INCOMPLETE
        )

    def test_required_receipt_and_process_evidence_are_both_mandatory(self) -> None:
        cases = (
            (),
            (evidence_ref(EvidenceType.EFFECT_RECEIPT, 1),),
            (evidence_ref(EvidenceType.PROCESS, 2),),
            (
                evidence_ref(
                    EvidenceType.EFFECT_RECEIPT,
                    1,
                    role=EvidenceRole.OPTIONAL,
                ),
                evidence_ref(EvidenceType.PROCESS, 2),
            ),
        )
        for refs in cases:
            with self.subTest(refs=refs):
                result = reconcile_attempt_deadline(
                    reconciliation_evidence(evidence=refs)
                )
                self.assertIs(result.status, AttemptReconciliationStatus.BLOCKED)
                self.assertIs(
                    result.reason, AttemptReconciliationReason.EVIDENCE_MISSING
                )
                self.assertFalse(result.permissions.any_allowed)

    def test_every_evidence_reference_must_bind_to_exact_attempt(self) -> None:
        other = identity(correlation_id="OP-002")
        result = reconcile_attempt_deadline(
            reconciliation_evidence(
                evidence=(
                    evidence_ref(EvidenceType.EFFECT_RECEIPT, 1),
                    evidence_ref(EvidenceType.PROCESS, 2, subject=other),
                )
            )
        )

        self.assertIs(result.status, AttemptReconciliationStatus.BLOCKED)
        self.assertIs(result.reason, AttemptReconciliationReason.EVIDENCE_MISSING)

    def test_receipts_must_bind_to_exact_attempt_and_operation(self) -> None:
        other = identity(correlation_id="OP-002")
        cases = (
            (
                reconciliation_evidence(
                    outcome_receipts=(receipt(EffectStatus.ABSENT, subject=other),)
                ),
                AttemptReconciliationReason.IDENTITY_MISMATCH,
            ),
            (
                reconciliation_evidence(
                    outcome_receipts=(
                        receipt(
                            EffectStatus.ABSENT,
                            operation=EffectOperation.MODEL_INFERENCE,
                        ),
                    )
                ),
                AttemptReconciliationReason.OPERATION_MISMATCH,
            ),
            (
                reconciliation_evidence(
                    termination_receipts=(termination_receipt(subject=other),)
                ),
                AttemptReconciliationReason.IDENTITY_MISMATCH,
            ),
        )
        for bundle, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                result = reconcile_attempt_deadline(bundle)
                self.assertIs(result.status, AttemptReconciliationStatus.BLOCKED)
                self.assertIs(result.reason, expected_reason)
                self.assertFalse(result.permissions.any_allowed)

    def test_conflicting_outcome_or_termination_receipts_are_rejected(self) -> None:
        cases = (
            reconciliation_evidence(
                outcome_receipts=(
                    receipt(EffectStatus.ABSENT),
                    receipt(EffectStatus.APPLIED),
                )
            ),
            reconciliation_evidence(
                termination_receipts=(
                    termination_receipt(ordinal=1),
                    termination_receipt(ordinal=2),
                )
            ),
        )
        for bundle in cases:
            with self.subTest(bundle=bundle):
                result = reconcile_attempt_deadline(bundle)
                self.assertIs(result.status, AttemptReconciliationStatus.BLOCKED)
                self.assertIs(
                    result.reason,
                    AttemptReconciliationReason.CONFLICTING_EVIDENCE,
                )
                self.assertFalse(result.permissions.any_allowed)

    def test_exact_completed_result_replay_is_idempotent(self) -> None:
        bundle = reconciliation_evidence()
        completed = reconcile_attempt_deadline(bundle)
        reordered = replace(
            bundle,
            outcome_receipts=tuple(reversed(bundle.outcome_receipts)),
            termination_receipts=tuple(reversed(bundle.termination_receipts)),
            evidence=tuple(reversed(bundle.evidence)),
        )

        replayed = reconcile_attempt_deadline(reordered, previous=completed)

        self.assertTrue(replayed.idempotent)
        self.assertEqual(replayed.evidence_fingerprint, completed.evidence_fingerprint)
        self.assertEqual(replayed.permissions, completed.permissions)

    def test_completed_result_rejects_conflicting_new_evidence(self) -> None:
        completed = reconcile_attempt_deadline(reconciliation_evidence())

        with self.assertRaises(AttemptReconciliationConflict):
            reconcile_attempt_deadline(
                reconciliation_evidence(EffectStatus.APPLIED),
                previous=completed,
            )

    def test_blocked_result_can_complete_when_evidence_arrives(self) -> None:
        incomplete = reconciliation_evidence(evidence=())
        blocked = reconcile_attempt_deadline(incomplete)

        completed = reconcile_attempt_deadline(
            reconciliation_evidence(), previous=blocked
        )

        self.assertIs(blocked.status, AttemptReconciliationStatus.BLOCKED)
        self.assertIs(completed.status, AttemptReconciliationStatus.COMPLETED)
        self.assertFalse(completed.idempotent)


if __name__ == "__main__":
    unittest.main()
