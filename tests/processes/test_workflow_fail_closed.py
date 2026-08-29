from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.e2e.support import E2EHarness, git
from wish_builder.adapters.git_worktree import AttemptEffectDisposition
from wish_builder.contracts.runtime import EffectStatus, RuntimeReasonCode
from wish_builder.processes import CoordinatorStatus, WorkerResultProposal
from wish_builder.processes.workflow import (
    LocalExecutionWorkflow,
    WorkflowReason,
    WorkflowStatus,
    _AppendOutcome,
)


class WorkflowFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.harness = E2EHarness(Path(self.temporary.name))

    def reserve_attempt(self):
        reserved = self.harness.coordinator().reserve_ready(limit=1)
        self.assertIs(reserved.status, CoordinatorStatus.PROGRESSED)
        identity = reserved.reserved[0]
        return identity, reserved, self.harness.workflow(reserved.cursor)

    def succeeded_attempt(self):
        identity, reserved, workflow = self.reserve_attempt()
        prepared = workflow.prepare_attempt(identity)
        self.assertIs(prepared.status, WorkflowStatus.PROGRESSED)
        assert prepared.attempt is not None
        dispatched = self.harness.coordinator(prepared.cursor).dispatch_reserved(identity)
        accepted = self.harness.coordinator(dispatched.cursor).accept_worker_result(
            WorkerResultProposal(identity, "worker-fail-closed", True)
        )
        return prepared.attempt, self.harness.workflow(accepted.cursor)

    def test_property_and_effect_reason_cover_both_receipt_outcomes(self) -> None:
        _, _, workflow = self.reserve_attempt()

        self.assertIs(workflow.cursor, workflow._cursor)
        self.assertIs(
            WorkflowReason.EFFECT_OUTCOME_UNKNOWN,
            workflow._effect_reason(EffectStatus.UNKNOWN),
        )
        self.assertIs(
            WorkflowReason.EFFECT_ABSENT,
            workflow._effect_reason(EffectStatus.ABSENT),
        )

    def test_prepare_attempt_stops_before_effect_when_admission_is_denied(self) -> None:
        identity, _, workflow = self.reserve_attempt()

        with mock.patch.object(
            workflow,
            "_admission_reason",
            return_value=WorkflowReason.LEASE_NOT_ADMITTED,
        ):
            result = workflow.prepare_attempt(identity)

        self.assertIs(result.status, WorkflowStatus.BLOCKED)
        self.assertIs(result.reason, WorkflowReason.LEASE_NOT_ADMITTED)
        self.assertEqual((), result.events)

    def test_prepare_attempt_rejects_reserved_attempt_when_task_was_advanced(self) -> None:
        identity, reserved, workflow = self.reserve_attempt()
        dispatched = self.harness.coordinator(reserved.cursor).dispatch_reserved(identity)

        workflow = self.harness.workflow(dispatched.cursor)
        with mock.patch.object(workflow, "_current_attempt", return_value=True):
            result = workflow.prepare_attempt(identity)

        self.assertIs(result.status, WorkflowStatus.REJECTED)
        self.assertIs(result.reason, WorkflowReason.ATTEMPT_NOT_CURRENT)

    def test_prepare_attempt_blocks_when_effect_request_cannot_be_persisted(self) -> None:
        identity, _, workflow = self.reserve_attempt()
        outcome = _AppendOutcome(None, None, WorkflowReason.JOURNAL_CONFLICT)

        with mock.patch.object(
            workflow,
            "_append_effect_request",
            return_value=outcome,
        ):
            result = workflow.prepare_attempt(identity)

        self.assertIs(result.status, WorkflowStatus.BLOCKED)
        self.assertIs(result.reason, WorkflowReason.JOURNAL_CONFLICT)

    def test_prepare_attempt_blocks_and_fences_run_for_unknown_effect_outcome(self) -> None:
        identity, _, workflow = self.reserve_attempt()
        effect = mock.Mock(
            receipt=mock.Mock(status=EffectStatus.UNKNOWN),
            disposition=AttemptEffectDisposition.UNKNOWN,
            reason_code=RuntimeReasonCode.GIT_STATE_CONFLICT,
            value=None,
        )
        requested = mock.Mock(
            event=mock.sentinel.requested,
            append_result=mock.sentinel.appended,
            reason=WorkflowReason.NONE,
        )
        observed = _AppendOutcome(
            mock.sentinel.observed,
            mock.sentinel.observed_append,
            WorkflowReason.NONE,
        )

        with (
            mock.patch.object(workflow, "_append_effect_request", return_value=requested),
            mock.patch(
                "wish_builder.processes.workflow.PreparedEffect.from_append_result",
                return_value=mock.sentinel.prepared,
            ),
            mock.patch.object(workflow._repository, "create_attempt", return_value=effect),
            mock.patch.object(workflow, "_append_observation", return_value=observed),
        ):
            result = workflow.prepare_attempt(identity)

        self.assertIs(result.status, WorkflowStatus.BLOCKED)
        self.assertIs(result.reason, WorkflowReason.EFFECT_OUTCOME_UNKNOWN)
        self.assertGreaterEqual(len(result.events), 2)

    def test_stage_attempt_blocks_before_validation_when_admission_is_denied(self) -> None:
        attempt, workflow = self.succeeded_attempt()

        with mock.patch.object(
            workflow,
            "_admission_reason",
            return_value=WorkflowReason.INDEX_MISMATCH,
        ):
            result = workflow.stage_attempt_result(attempt)

        self.assertIs(result.status, WorkflowStatus.BLOCKED)
        self.assertIs(result.reason, WorkflowReason.INDEX_MISMATCH)

    def test_stage_attempt_fences_run_when_validation_rejects_unmodified_attempt(self) -> None:
        attempt, workflow = self.succeeded_attempt()

        result = workflow.stage_attempt_result(attempt)

        self.assertIs(result.status, WorkflowStatus.BLOCKED)
        self.assertIs(result.reason, WorkflowReason.RESULT_REJECTED)
        self.assertTrue(result.events)

    def test_stage_attempt_fences_run_for_unknown_stage_effect(self) -> None:
        attempt, workflow = self.succeeded_attempt()
        validation = mock.Mock(accepted=True)
        effect = mock.Mock(
            receipt=mock.Mock(status=EffectStatus.UNKNOWN),
            disposition=AttemptEffectDisposition.UNKNOWN,
            reason_code=RuntimeReasonCode.GIT_STATE_CONFLICT,
            value=None,
        )
        requested = mock.Mock(
            event=mock.sentinel.requested,
            append_result=mock.sentinel.appended,
            reason=WorkflowReason.NONE,
        )
        observed = _AppendOutcome(
            mock.sentinel.observed,
            mock.sentinel.observed_append,
            WorkflowReason.NONE,
        )

        with (
            mock.patch.object(
                workflow._repository,
                "validate_result",
                return_value=validation,
            ),
            mock.patch.object(
                workflow._repository,
                "plan_stage",
                return_value=mock.sentinel.command,
            ),
            mock.patch.object(workflow, "_append_effect_request", return_value=requested),
            mock.patch(
                "wish_builder.processes.workflow.PreparedEffect.from_append_result",
                return_value=mock.sentinel.prepared,
            ),
            mock.patch.object(workflow._repository, "stage_result", return_value=effect),
            mock.patch.object(workflow, "_append_observation", return_value=observed),
        ):
            result = workflow.stage_attempt_result(attempt)

        self.assertIs(result.status, WorkflowStatus.BLOCKED)
        self.assertIs(result.reason, WorkflowReason.EFFECT_OUTCOME_UNKNOWN)

    def test_admission_reason_fails_closed_for_journal_index_and_lease_failures(self) -> None:
        _, _, workflow = self.reserve_attempt()

        workflow._journal = mock.Mock(blocked=True)
        self.assertIs(WorkflowReason.PERSISTENCE_FAILED, workflow._admission_reason())

        workflow._journal = self.harness.journal
        with mock.patch.object(
            type(workflow._cursor.graph_index),
            "verify",
            return_value=False,
        ):
            self.assertIs(WorkflowReason.INDEX_MISMATCH, workflow._admission_reason())
        with mock.patch.object(
            type(workflow._cursor.lease_state),
            "allows_admission",
            return_value=False,
        ):
            self.assertIs(WorkflowReason.LEASE_NOT_ADMITTED, workflow._admission_reason())

    def test_constructor_and_stage_input_validation_remain_strict(self) -> None:
        _, _, workflow = self.reserve_attempt()

        with self.assertRaisesRegex(TypeError, "attempt"):
            workflow.stage_attempt_result(object())
        with self.assertRaisesRegex(TypeError, "manifest"):
            LocalExecutionWorkflow(
                object(),
                workflow.cursor,
                self.harness.journal,
                self.harness.repository_adapter,
                coordinator_id=workflow._coordinator_id,
                owner=self.harness.owner,
                fencing_token=1,
                authority_clock=workflow._authority_clock,
            )

    def test_promotion_and_cleanup_reject_invalid_inputs_before_side_effects(self) -> None:
        attempt, workflow = self.succeeded_attempt()

        with self.assertRaisesRegex(TypeError, "sources"):
            workflow.promote_staged([], object())
        with self.assertRaisesRegex(TypeError, "acceptance"):
            workflow.promote_staged((), object())

        with self.assertRaisesRegex(TypeError, "cleanup"):
            workflow.cleanup_attempt(object(), object(), operation_id="cleanup")

        from wish_builder.services.cleanup import CleanupCandidate, CleanupService

        cleanup = CleanupService(
            self.harness.repository_adapter,
            available_bytes=lambda: 1_000_000,
            minimum_free_bytes=1,
        )
        with self.assertRaisesRegex(TypeError, "candidate"):
            workflow.cleanup_attempt(cleanup, object(), operation_id="cleanup")

        candidate = CleanupCandidate(
            attempt,
            git(Path(attempt.path), "rev-parse", "HEAD"),
            (),
            reconciliation_complete=True,
            process_tree_terminated=True,
            outcome_known=True,
        )
        with self.assertRaisesRegex(ValueError, "operation_id"):
            workflow.cleanup_attempt(cleanup, candidate, operation_id="")


if __name__ == "__main__":
    unittest.main()
