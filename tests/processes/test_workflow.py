from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.processes.test_coordinator import (
    BASE_TIME,
    COORDINATOR_ID,
    CoordinatorHarness,
)
from wish_builder.adapters.git_identity import capture_workspace_identity
from wish_builder.adapters.git_worktree import GitWorktreeAdapter
from wish_builder.contracts.runtime import (
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    JournalEventType,
    RuntimeState,
)
from wish_builder.processes import (
    AcceptanceResult,
    CoordinatorStatus,
    ForegroundCoordinator,
    LocalExecutionWorkflow,
    WorkerResultProposal,
    WorkflowReason,
    WorkflowStatus,
)


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout.decode("utf-8", errors="strict").strip()


def initialize_repository(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Wish Builder Tests")
    git(path, "config", "user.email", "tests@wish-builder.invalid")
    git(path, "config", "core.autocrlf", "false")
    source = path / "src" / "req-001" / "base.txt"
    source.parent.mkdir(parents=True)
    source.write_text("base\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")


class PassingAcceptance:
    def verify(self, task, repository, promotion):
        result = repository / "src" / "req-001" / "result.txt"
        if task.id != promotion.task_id or result.read_text() != "implemented\n":
            raise AssertionError("promotion candidate is not visible to acceptance")
        raw = f"{task.id}:{promotion.promoted_commit_sha}".encode()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        identity = next(
            item.manifest.identity
            for item in self.sources
            if item.task_id == task.id
        )
        return AcceptanceResult(
            True,
            (
                EvidenceRef(
                    1,
                    digest,
                    len(raw),
                    EvidenceType.RESULT,
                    EvidenceProducer(identity, external_object_id="local-acceptance"),
                    "2026-08-19T00:00:20Z",
                    EvidenceSensitivity.INTERNAL,
                    EvidenceRenderPolicy.METADATA_ONLY,
                    EvidenceRole.REQUIRED,
                    digest,
                ),
            ),
        )

    def __init__(self, sources):
        self.sources = sources


class FailingAcceptance:
    def __init__(self, target: Path, expected_target_sha: str, *, raises: bool) -> None:
        self.target = target
        self.expected_target_sha = expected_target_sha
        self.raises = raises
        self.candidate_repository: Path | None = None

    def verify(self, task, repository, promotion):
        self.candidate_repository = repository
        if repository.resolve() == self.target.resolve():
            raise AssertionError("acceptance must not run in the target worktree")
        if git(self.target, "rev-parse", "HEAD") != self.expected_target_sha:
            raise AssertionError("target advanced before acceptance completed")
        if git(repository, "rev-parse", "HEAD") != promotion.promoted_commit_sha:
            raise AssertionError("acceptance candidate does not match promotion")
        if self.raises:
            raise RuntimeError("acceptance adapter crashed")
        return AcceptanceResult(
            False,
            (),
            RuntimeReasonCode.CHECK_FAILED,
        )


class LocalExecutionWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.harness = CoordinatorHarness(self.root / "control")
        self.repository = self.root / "repository"
        self.attempts = self.root / "attempts"
        initialize_repository(self.repository)
        self.attempts.mkdir()
        scopes = tuple(
            sorted(
                {
                    *self.harness.manifest.protected_paths,
                    *(
                        path
                        for task in self.harness.manifest.tasks
                        for path in (
                            *task.owned_paths,
                            *task.allowed_auxiliary_paths,
                        )
                    ),
                }
            )
        )
        expected = capture_workspace_identity(self.repository, scopes)
        self.repository_adapter = GitWorktreeAdapter(
            self.repository,
            self.attempts,
            expected,
            clock=lambda: "2026-08-19T00:00:20Z",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def workflow(self, cursor):
        return LocalExecutionWorkflow(
            self.harness.manifest,
            cursor,
            self.harness.journal,
            self.repository_adapter,
            coordinator_id=COORDINATOR_ID,
            owner=self.harness.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=20),
        )

    def coordinator(self, cursor):
        return ForegroundCoordinator(
            self.harness.manifest,
            cursor,
            self.harness.journal,
            self.harness.port,
            coordinator_id=COORDINATOR_ID,
            owner=self.harness.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=20),
        )

    def stage_result(self):
        reserved = self.harness.coordinator.reserve_ready(limit=1)
        identity = reserved.reserved[0]
        preparation = self.workflow(reserved.cursor).prepare_attempt(identity)
        assert preparation.attempt is not None
        dispatched = self.coordinator(preparation.cursor).dispatch_reserved(identity)
        assert dispatched.status is CoordinatorStatus.PROGRESSED
        attempt_path = Path(preparation.attempt.path)
        result_path = attempt_path / "src" / "req-001" / "result.txt"
        result_path.write_text("implemented\n", encoding="utf-8")
        git(attempt_path, "add", "--", "src/req-001/result.txt")
        git(attempt_path, "commit", "-m", "implement req 001")
        accepted = self.coordinator(dispatched.cursor).accept_worker_result(
            WorkerResultProposal(identity, "worker-001", True)
        )
        staged = self.workflow(accepted.cursor).stage_attempt_result(
            preparation.attempt
        )
        assert staged.staged is not None
        return staged

    def assert_no_durable_promotion_events(self) -> None:
        journal = b"".join(
            path.read_bytes()
            for path in sorted(self.harness.storage.root.rglob("*.jsonl"))
        )
        self.assertNotIn(b'"event_type":"promotion_requested"', journal)
        self.assertNotIn(b'"event_type":"promotion_observed"', journal)

    def test_succeeded_worker_is_staged_promoted_and_verified(self) -> None:
        reserved = self.harness.coordinator.reserve_ready(limit=1)
        self.assertEqual(CoordinatorStatus.PROGRESSED, reserved.status)
        identity = reserved.reserved[0]
        self.assertEqual(RuntimeState.LEASED, reserved.cursor.snapshot.tasks[0].state)
        self.assertEqual(RuntimeState.RESERVED, reserved.cursor.snapshot.attempts[0].state)

        preparation = self.workflow(reserved.cursor).prepare_attempt(identity)
        self.assertEqual(WorkflowStatus.PROGRESSED, preparation.status)
        self.assertIsNotNone(preparation.attempt)
        assert preparation.attempt is not None

        dispatched = self.coordinator(preparation.cursor).dispatch_reserved(identity)
        self.assertEqual(CoordinatorStatus.PROGRESSED, dispatched.status)
        self.assertEqual(RuntimeState.DISPATCHED, dispatched.cursor.snapshot.tasks[0].state)
        self.assertEqual(RuntimeState.RUNNING, dispatched.cursor.snapshot.attempts[0].state)

        attempt_path = Path(preparation.attempt.path)
        result_path = attempt_path / "src" / "req-001" / "result.txt"
        result_path.write_text("implemented\n", encoding="utf-8")
        git(attempt_path, "add", "--", "src/req-001/result.txt")
        git(attempt_path, "commit", "-m", "implement req 001")
        self.assertEqual("", git(attempt_path, "status", "--porcelain=v1", "-z"))
        self.assertEqual(
            "",
            git(
                attempt_path,
                "status",
                "--porcelain=v2",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ),
        )
        accepted = self.coordinator(dispatched.cursor).accept_worker_result(
            WorkerResultProposal(identity, "worker-001", True)
        )
        self.assertEqual(RuntimeState.SUCCEEDED, accepted.cursor.snapshot.attempts[0].state)

        workflow = self.workflow(accepted.cursor)
        staged = workflow.stage_attempt_result(preparation.attempt)
        self.assertEqual(WorkflowStatus.PROGRESSED, staged.status, staged)
        self.assertIsNotNone(staged.staged)
        assert staged.staged is not None
        self.assertEqual(RuntimeState.STAGED, staged.cursor.snapshot.tasks[0].state)

        promoted = workflow.promote_staged(
            (staged.staged,),
            PassingAcceptance((staged.staged,)),
        )
        self.assertEqual(WorkflowStatus.PROGRESSED, promoted.status)
        self.assertEqual(RuntimeState.VERIFIED, promoted.cursor.snapshot.tasks[0].state)
        self.assertEqual("implemented\n", (self.repository / "src/req-001/result.txt").read_text())
        self.assertEqual(
            [
                JournalEventType.PROMOTION_REQUESTED,
                JournalEventType.PROMOTION_OBSERVED,
                JournalEventType.TASK_VERIFIED,
            ],
            [event.event_type for event in promoted.events],
        )
        observation = next(
            event
            for event in promoted.events
            if event.event_type is JournalEventType.PROMOTION_OBSERVED
        )
        verified = next(
            event
            for event in promoted.events
            if event.event_type is JournalEventType.TASK_VERIFIED
        )
        record = promoted.promoted[0]
        self.assertTrue(record.acceptance_evidence)
        self.assertEqual(
            record.acceptance_evidence,
            observation.payload.receipt.evidence,
        )
        self.assertEqual(record.acceptance_evidence, verified.payload.evidence)

    def test_workflow_advances_dispatch_recovery_projection_with_journal_event(self) -> None:
        reserved = self.harness.coordinator.reserve_ready(limit=1)
        identity = reserved.reserved[0]

        with mock.patch(
            "wish_builder.processes.workflow.advance_dispatch_recoveries",
            return_value=reserved.cursor.dispatch_recoveries,
        ) as advance:
            preparation = self.workflow(reserved.cursor).prepare_attempt(identity)

        self.assertIs(preparation.status, WorkflowStatus.PROGRESSED)
        self.assertGreaterEqual(advance.call_count, 2)
        first_event = advance.call_args_list[0].args[1]
        self.assertIs(first_event.event_type, JournalEventType.EFFECT_REQUESTED)

    def test_prepare_attempt_rejects_after_worker_dispatch(self) -> None:
        dispatched = self.harness.coordinator.dispatch_ready(limit=1)
        self.assertEqual(CoordinatorStatus.PROGRESSED, dispatched.status)
        identity = dispatched.dispatched[0]

        preparation = self.workflow(dispatched.cursor).prepare_attempt(identity)

        self.assertEqual(WorkflowStatus.REJECTED, preparation.status)
        self.assertEqual(WorkflowReason.ATTEMPT_NOT_CURRENT, preparation.reason)
        self.assertIsNone(preparation.attempt)
        self.assertEqual((), preparation.events)
        self.assertEqual((), tuple(self.attempts.iterdir()))

    def test_rejected_acceptance_does_not_advance_or_request_target_promotion(self) -> None:
        staged = self.stage_result()
        target_sha = git(self.repository, "rev-parse", "HEAD")
        acceptance = FailingAcceptance(
            self.repository,
            target_sha,
            raises=False,
        )

        result = self.workflow(staged.cursor).promote_staged(
            (staged.staged,),
            acceptance,
        )

        self.assertEqual(WorkflowStatus.BLOCKED, result.status)
        self.assertEqual(target_sha, git(self.repository, "rev-parse", "HEAD"))
        self.assertFalse((self.repository / "src/req-001/result.txt").exists())
        self.assertIsNotNone(acceptance.candidate_repository)
        assert acceptance.candidate_repository is not None
        self.assertFalse(acceptance.candidate_repository.exists())
        self.assertFalse(
            {
                JournalEventType.PROMOTION_REQUESTED,
                JournalEventType.PROMOTION_OBSERVED,
            }
            & {event.event_type for event in result.events}
        )
        self.assert_no_durable_promotion_events()

    def test_acceptance_exception_does_not_advance_or_request_target_promotion(self) -> None:
        staged = self.stage_result()
        target_sha = git(self.repository, "rev-parse", "HEAD")
        acceptance = FailingAcceptance(
            self.repository,
            target_sha,
            raises=True,
        )

        result = self.workflow(staged.cursor).promote_staged(
            (staged.staged,),
            acceptance,
        )

        self.assertEqual(WorkflowStatus.BLOCKED, result.status)
        self.assertEqual(target_sha, git(self.repository, "rev-parse", "HEAD"))
        self.assertFalse((self.repository / "src/req-001/result.txt").exists())
        self.assertIsNotNone(acceptance.candidate_repository)
        assert acceptance.candidate_repository is not None
        self.assertFalse(acceptance.candidate_repository.exists())
        self.assertFalse(
            {
                JournalEventType.PROMOTION_REQUESTED,
                JournalEventType.PROMOTION_OBSERVED,
            }
            & {event.event_type for event in result.events}
        )
        self.assert_no_durable_promotion_events()


if __name__ == "__main__":
    unittest.main()
