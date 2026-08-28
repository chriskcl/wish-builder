from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tests.processes.test_coordinator import CoordinatorHarness
from wish_builder.cli import wishctl
from wish_builder.contracts.runtime import (
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel.state import apply_journal_event
from wish_builder.processes.coordinator import (
    CoordinatorCursor,
    CoordinatorStatus,
    WorkerResultProposal,
)
from wish_builder.processes.production_terminal import ProductionTerminalFinalizer
from wish_builder.services.checkpoints import CheckpointStore
from wish_builder.services.execution_checkpoints import ExecutionCheckpointPublisher
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    recover_coordinator_lease,
)


class WishCtlFinishValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.control_root = self.root / "control"
        self.journal_root = self.control_root / "journal"
        self.checkpoint_root = self.control_root / "checkpoints"
        self.harness = CoordinatorHarness(
            self.control_root,
            gate_b_admission=True,
        )
        self.manifest_path = self.root / "execution-manifest.json"
        self.manifest_path.write_bytes(self.harness.manifest.canonical_json_bytes())

    def _verified_cursor(self) -> CoordinatorCursor:
        dispatched = self.harness.coordinator.dispatch_ready(limit=1)
        self.assertIs(dispatched.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual(1, len(dispatched.dispatched))
        identity = dispatched.dispatched[0]
        accepted = self.harness.coordinator.accept_worker_result(
            WorkerResultProposal(identity, "finish-fixture-worker", True)
        )
        self.assertIs(accepted.status, CoordinatorStatus.PROGRESSED)
        cursor = accepted.cursor
        transitions = (
            (
                JournalEventType.PR_OBSERVED,
                RuntimeState.DISPATCHED,
                RuntimeState.PR_OPEN,
            ),
            (
                JournalEventType.MERGE_OBSERVED,
                RuntimeState.PR_OPEN,
                RuntimeState.MERGED,
            ),
            (
                JournalEventType.TASK_VERIFIED,
                RuntimeState.MERGED,
                RuntimeState.VERIFIED,
            ),
        )
        for event_type, from_state, to_state in transitions:
            event = JournalEvent.create(
                sequence=cursor.head.sequence + 1,
                event_id=f"EVENT-FINISH-FIXTURE-{cursor.head.sequence + 1:08d}",
                event_type=event_type,
                identity=ExecutionIdentity(
                    self.harness.manifest.run_id,
                    1,
                    self.harness.manifest.tasks[0].id,
                ),
                actor_type=ActorType.COORDINATOR,
                actor_id=self.harness.owner.actor.actor_id,
                recorded_at="2026-08-19T00:00:00Z",
                previous_event_hash=cursor.head.event_hash,
                payload=TransitionPayload(
                    TransitionSubject.TASK,
                    from_state,
                    to_state,
                ),
            )
            appended = self.harness.journal.append(event, expected_head=cursor.head)
            self.assertTrue(appended.durable)
            applied = apply_journal_event(cursor.snapshot, event)
            self.assertTrue(applied.accepted, applied.reason)
            cursor = CoordinatorCursor(
                applied.snapshot,
                cursor.graph_index.advance(cursor.snapshot, applied.snapshot),
                cursor.lease_state.advance(event),
                cursor.dispatch_recoveries,
            )
        return cursor

    def _finish(self) -> CoordinatorCursor:
        cursor = self._verified_cursor()
        lease = cursor.lease_state.lease
        self.assertIsNotNone(lease)
        assert lease is not None
        lease_service = CoordinatorLeaseService(
            self.harness.journal,
            lambda: recover_coordinator_lease(
                self.journal_root,
                self.harness.manifest,
                coordinator_epoch=1,
                repair_derived=False,
            ),
            run_id=self.harness.manifest.run_id,
            owner=self.harness.owner,
            manifest_digest=self.harness.manifest.canonical_sha256(),
            lease_ttl_seconds=lease.lease_ttl_seconds,
            lease_clock_skew_seconds=lease.lease_clock_skew_seconds,
        )
        finalizer = ProductionTerminalFinalizer(
            self.harness.manifest,
            self.harness.journal,
            lease_service,
            ExecutionCheckpointPublisher(
                self.harness.manifest,
                self.harness.journal,
                CheckpointStore(self.checkpoint_root),
                previous_sequence=cursor.head.sequence,
            ),
            coordinator_id=self.harness.owner.actor.actor_id,
            fencing_token=1,
        )
        result = finalizer.finish(cursor)
        self.assertTrue(result.completed, result)
        return result.cursor

    def _invoke(self, *extra: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        arguments = [
            "validate",
            str(self.manifest_path),
            "--stage",
            "finish",
            *extra,
        ]
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = wishctl.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def _finish_arguments(self) -> tuple[str, ...]:
        return (
            "--journal-root",
            str(self.journal_root),
            "--checkpoint-root",
            str(self.checkpoint_root),
        )

    def test_finish_accepts_only_replayed_terminal_state_and_checkpoint(self) -> None:
        terminal = self._finish()

        code, stdout, stderr = self._invoke(*self._finish_arguments())

        self.assertEqual((0, ""), (code, stderr))
        self.assertIn("OK: manifest valid", stdout)
        self.assertIs(terminal.snapshot.phase, RuntimeState.COMPLETE)

    def test_finish_requires_both_runtime_evidence_roots(self) -> None:
        code, stdout, stderr = self._invoke()

        self.assertEqual((1, ""), (code, stderr))
        self.assertIn("--journal-root and --checkpoint-root", stdout)

    def test_finish_rejects_incomplete_runtime_even_with_valid_manifest(self) -> None:
        code, stdout, stderr = self._invoke(*self._finish_arguments())

        self.assertEqual((1, ""), (code, stderr))
        self.assertIn("not 'complete'", stdout)
        self.assertIn("not verified or archived", stdout)
        self.assertIn("not terminally released", stdout)
        self.assertIn("terminal checkpoint is not verified", stdout)

    def test_finish_rejects_terminal_runtime_without_gate_b_admission(self) -> None:
        self.control_root = self.root / "control-without-gate-b"
        self.journal_root = self.control_root / "journal"
        self.checkpoint_root = self.control_root / "checkpoints"
        self.harness = CoordinatorHarness(self.control_root)
        self.manifest_path.write_bytes(self.harness.manifest.canonical_json_bytes())
        self._finish()

        code, stdout, stderr = self._invoke(*self._finish_arguments())

        self.assertEqual((1, ""), (code, stderr))
        self.assertIn("Gate B execution admission failed", stdout)
        self.assertIn("gate_b_request_missing", stdout)

    def test_finish_rejects_corrupt_checkpoint_and_journal(self) -> None:
        self._finish()
        self.checkpoint_root.joinpath("current.json").write_bytes(b"{}")
        code, stdout, stderr = self._invoke(*self._finish_arguments())
        self.assertEqual((1, ""), (code, stderr))
        self.assertIn("checkpoint", stdout.lower())

        segment = max((self.journal_root / "segments").glob("segment-*.jsonl"))
        with segment.open("ab") as handle:
            handle.write(b"{")
        code, stdout, stderr = self._invoke(*self._finish_arguments())
        self.assertEqual((1, ""), (code, stderr))
        self.assertIn("runtime evidence could not be verified", stdout)


if __name__ == "__main__":
    unittest.main()
