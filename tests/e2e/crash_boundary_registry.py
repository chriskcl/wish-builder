from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EvidenceKind = Literal["subprocess_e2e", "integration", "unit"]

UNPROVEN_PREFIX = "Required but not proven: "


@dataclass(frozen=True, slots=True)
class CrashBoundary:
    boundary_id: str
    subsystem: str
    failpoint_name: str
    semantic_boundary: str
    evidence_kind: EvidenceKind | None
    test_id: str | None
    expected_recovery: str
    proven: bool


CRASH_BOUNDARIES: tuple[CrashBoundary, ...] = (
    CrashBoundary(
        boundary_id="fake-before-write",
        subsystem="fake_effect",
        failpoint_name="before_write",
        semantic_boundary="Before an atomic effect or receipt file write starts.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_crash_boundary_registry."
            "CrashBoundaryDirectEvidenceTests."
            "test_before_write_process_crash_recovers_one_effect"
        ),
        expected_recovery=(
            "Recovery inspects both durable files, proves the effect absent, and creates "
            "exactly one effect and one receipt in a fresh process."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="fake-after-write",
        subsystem="fake_effect",
        failpoint_name="after_write",
        semantic_boundary=(
            "After an effect or receipt file is atomically replaced and its parent "
            "directory is synchronized."
        ),
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_crash_boundary_registry."
            "CrashBoundaryDirectEvidenceTests."
            "test_after_write_process_crash_reconciles_one_effect"
        ),
        expected_recovery=(
            "Recovery observes the durable effect file, rebuilds the receipt in a fresh "
            "process, and does not create a second effect."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="fake-before-effect",
        subsystem="fake_effect",
        failpoint_name="before_effect",
        semantic_boundary="After the request is durable but before the external effect.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_m1_workflow.ActiveM1WorkflowE2ETests."
            "test_restart_retries_only_when_before_effect_is_proven_absent"
        ),
        expected_recovery=(
            "Recovery proves absence, re-executes the request, and finishes with exactly "
            "one effect and one receipt."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="fake-after-effect-before-receipt",
        subsystem="fake_effect",
        failpoint_name="after_effect_before_receipt",
        semantic_boundary=(
            "After the external effect is durable but before its receipt is published."
        ),
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_after_effect_before_receipt_process_exit_reconciles_once"
        ),
        expected_recovery=(
            "A fresh process acquires the next epoch, observes the applied old-epoch "
            "effect, reconstructs its receipt, and does not repeat the effect."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="fake-after-receipt",
        subsystem="fake_effect",
        failpoint_name="after_receipt",
        semantic_boundary=(
            "After the durable receipt exists but before the observation is journaled."
        ),
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_after_receipt_process_exit_reconciles_without_redispatch"
        ),
        expected_recovery=(
            "A fresh process acquires the next epoch and uses the durable old-epoch "
            "receipt to append the missing observation without repeating the effect."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="git-before-attempt-create",
        subsystem="git_worktree",
        failpoint_name="before_attempt_create",
        semantic_boundary="Before Git creates the isolated attempt worktree.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_git_before_attempt_create_process_exit_creates_once"
        ),
        expected_recovery=(
            "A fresh adapter proves the worktree absent, creates it once, and leaves the "
            "target worktree unchanged."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="git-after-attempt-create",
        subsystem="git_worktree",
        failpoint_name="after_attempt_create",
        semantic_boundary="After Git creates the isolated attempt worktree.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_git_after_attempt_create_process_exit_reconciles_once"
        ),
        expected_recovery=(
            "A fresh process verifies the existing worktree identity and does not create "
            "a second worktree or move the target."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="git-before-result-stage",
        subsystem="git_worktree",
        failpoint_name="before_result_stage",
        semantic_boundary="Before Git creates the immutable staged-result ref.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_git_before_result_stage_process_exit_stages_once"
        ),
        expected_recovery=(
            "A fresh adapter proves the staged ref absent, creates it once, and leaves the "
            "target ref unchanged."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="git-after-result-stage",
        subsystem="git_worktree",
        failpoint_name="after_result_stage",
        semantic_boundary="After Git creates the immutable staged-result ref.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_git_after_result_stage_process_exit_reconciles_once"
        ),
        expected_recovery=(
            "A fresh adapter verifies the exact staged ref and does not stage the result "
            "again or advance the target."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="git-before-target-promotion",
        subsystem="git_worktree",
        failpoint_name="before_target_promotion",
        semantic_boundary="Before Git advances the target worktree to the candidate commit.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_git_before_target_promotion_process_exit_promotes_once"
        ),
        expected_recovery=(
            "A fresh adapter proves the target remains at the expected commit, promotes "
            "once, and preserves the planned ancestry."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="git-after-target-promotion",
        subsystem="git_worktree",
        failpoint_name="after_target_promotion",
        semantic_boundary="After Git advances the target to the candidate commit.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_git_after_target_promotion_process_exit_reconciles_once"
        ),
        expected_recovery=(
            "Recovery verifies the target and staged source, reports the promotion as "
            "applied, and does not advance the target again."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="git-before-attempt-remove",
        subsystem="git_worktree",
        failpoint_name="before_attempt_remove",
        semantic_boundary="Before Git removes a reconciled attempt worktree.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_git_before_attempt_remove_process_exit_removes_only_target"
        ),
        expected_recovery=(
            "A fresh adapter repeats cleanup safety checks, removes only the planned "
            "attempt, and preserves the target and sibling worktree."
        ),
        proven=True,
    ),
    CrashBoundary(
        boundary_id="git-after-attempt-remove",
        subsystem="git_worktree",
        failpoint_name="after_attempt_remove",
        semantic_boundary="After Git removes a reconciled attempt worktree.",
        evidence_kind="subprocess_e2e",
        test_id=(
            "tests.e2e.test_process_exit_recovery.ProcessExitRecoveryE2ETests."
            "test_git_after_attempt_remove_process_exit_reconciles_only_target_absent"
        ),
        expected_recovery=(
            "A fresh adapter reconciles the verified absence and does not remove the "
            "target or sibling worktree."
        ),
        proven=True,
    ),
)
