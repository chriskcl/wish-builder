from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.e2e.support import E2EAcceptance, E2EHarness, git
from wish_builder.contracts.runtime import JournalEventType
from wish_builder.processes import (
    CoordinatorStatus,
    WorkerResultProposal,
    WorkflowStatus,
)
from wish_builder.services.cleanup import (
    CleanupCandidate,
    CleanupDisposition,
    CleanupService,
)


class CleanupWorkflowTests(unittest.TestCase):
    def test_cleanup_is_journaled_before_and_after_attempt_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = E2EHarness(Path(temporary))
            reserved = harness.coordinator().reserve_ready(limit=1)
            self.assertEqual(CoordinatorStatus.PROGRESSED, reserved.status)
            identity = reserved.reserved[0]
            prepared = harness.workflow(reserved.cursor).prepare_attempt(identity)
            self.assertEqual(WorkflowStatus.PROGRESSED, prepared.status)
            assert prepared.attempt is not None and identity.task_id is not None
            dispatched = harness.coordinator(prepared.cursor).dispatch_reserved(identity)
            self.assertEqual(CoordinatorStatus.PROGRESSED, dispatched.status)
            relative_path = harness.result_path(identity.task_id, harness.manifest)
            result_path = Path(prepared.attempt.path) / relative_path
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                f"implemented {identity.task_id}\n",
                encoding="utf-8",
            )
            harness.commit_attempt(prepared.attempt, relative_path)
            accepted = harness.coordinator(dispatched.cursor).accept_worker_result(
                WorkerResultProposal(identity, "worker-cleanup", True)
            )
            staged = harness.workflow(accepted.cursor).stage_attempt_result(
                prepared.attempt
            )
            assert staged.staged is not None
            promoted = harness.workflow(staged.cursor).promote_staged(
                (staged.staged,),
                E2EAcceptance((staged.staged,)),
            )
            evidence = promoted.promoted[0].acceptance_evidence
            candidate = CleanupCandidate(
                prepared.attempt,
                git(Path(prepared.attempt.path), "rev-parse", "HEAD"),
                evidence,
                reconciliation_complete=True,
                process_tree_terminated=True,
                outcome_known=True,
            )
            cleanup = CleanupService(
                harness.repository_adapter,
                available_bytes=lambda: 1_000_000,
                minimum_free_bytes=1_000,
                clock=lambda: "2026-08-19T00:00:50Z",
            )

            result = harness.workflow(promoted.cursor).cleanup_attempt(
                cleanup,
                candidate,
                operation_id="CLEANUP-E2E-001",
            )

            self.assertEqual(WorkflowStatus.PROGRESSED, result.status, result)
            self.assertIsNotNone(result.observation)
            assert result.observation is not None
            self.assertEqual(CleanupDisposition.REMOVED, result.observation.disposition)
            self.assertEqual(
                [
                    JournalEventType.CLEANUP_REQUESTED,
                    JournalEventType.CLEANUP_OBSERVED,
                ],
                [event.event_type for event in result.events],
            )
            self.assertFalse(Path(prepared.attempt.path).exists())
            self.assertFalse(cleanup.dispatch_blocked)

    def test_dispatch_admission_fails_closed_on_space_probe_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = E2EHarness(Path(temporary))

            low_space = CleanupService(
                harness.repository_adapter,
                available_bytes=lambda: 99,
                minimum_free_bytes=100,
            )

            def failed_probe() -> int:
                raise OSError("probe failed")

            failed = CleanupService(
                harness.repository_adapter,
                available_bytes=failed_probe,
                minimum_free_bytes=100,
            )

            self.assertTrue(low_space.dispatch_blocked)
            self.assertTrue(failed.dispatch_blocked)


if __name__ == "__main__":
    unittest.main()
