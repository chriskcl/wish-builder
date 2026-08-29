from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.processes.test_coordinator import BASE_TIME
from tests.processes.test_dispatch_recovery import recovery_proof
from tests.test_wishctl_live_identity import LiveResumeHarness
from wish_builder.cli import wishctl
from wish_builder.contracts import (
    EffectOperation,
    ExecutionIdentity,
    JournalEventType,
    RuntimeState,
)


class WishCtlResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

        self.harness = LiveResumeHarness(self.root)
        blocked = self.harness.coordinator.dispatch_ready()
        self.request = next(
            event
            for event in blocked.events
            if event.event_type is JournalEventType.DISPATCH_REQUESTED
        )
        self.active = self.harness.take_over()
        self.proof = recovery_proof(self.active, self.request)
        self.manifest_path = self.root / "execution-manifest.json"
        self.manifest_path.write_bytes(
            self.harness.manifest.canonical_json_bytes()
        )
        self.proof_path = self.root / "dispatch-recovery.json"
        self.proof_path.write_bytes(self.proof.canonical_json_bytes())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                wishctl,
                "_authority_now",
                return_value=BASE_TIME + timedelta(seconds=10),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = wishctl.main(
                [
                    "resume",
                    str(self.manifest_path),
                    str(self.proof_path),
                    "--journal-root",
                    str(self.harness.journal_root),
                    "--workspace-root",
                    str(self.harness.repository),
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_resume_writes_fixed_prefix_and_exact_retry_is_idempotent(self) -> None:
        code, stdout, stderr = self.invoke()

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertFalse(result["idempotent"])
        self.assertEqual(
            [
                "recovery_completed",
                "task_retry_scheduled",
                "run_resumed",
            ],
            [event["event_type"] for event in result["events"]],
        )
        events = wishctl._read_verified_journal(self.harness.journal_root)
        self.assertEqual(
            (
                JournalEventType.RECOVERY_COMPLETED,
                JournalEventType.TASK_RETRY_SCHEDULED,
                JournalEventType.RUN_RESUMED,
            ),
            tuple(event.event_type for event in events[-3:]),
        )

        code, stdout, stderr = self.invoke()

        self.assertEqual(0, code, stderr)
        replayed = json.loads(stdout)
        self.assertTrue(replayed["idempotent"])
        self.assertEqual([], replayed["events"])
        self.assertEqual(result["event_sequence"], replayed["event_sequence"])

    def test_resume_completes_a_crash_interrupted_prefix(self) -> None:
        appended = self.active._append_payload(
            JournalEventType.RECOVERY_COMPLETED,
            ExecutionIdentity(self.harness.manifest.run_id, 2),
            self.proof,
            actor_type=self.proof.command.actor.actor_type,
            actor_id=self.proof.command.actor.actor_id,
            allow_recovery=True,
        )
        self.assertIsNotNone(appended.event)

        code, stdout, stderr = self.invoke()

        self.assertEqual(0, code, stderr)
        result = json.loads(stdout)
        self.assertEqual(
            ["task_retry_scheduled", "run_resumed"],
            [event["event_type"] for event in result["events"]],
        )

    def test_resume_rejects_stale_or_mismatched_proof_without_appending(self) -> None:
        before = self.active.cursor.head
        stale = replace(
            self.proof,
            command=replace(
                self.proof.command,
                expected_sequence=self.proof.last_valid_sequence - 1,
            ),
            last_valid_sequence=self.proof.last_valid_sequence - 1,
        )
        self.proof_path.write_bytes(stale.canonical_json_bytes())

        code, _, stderr = self.invoke()

        self.assertEqual(1, code)
        self.assertIn("recovery_proof_invalid", stderr)
        events = wishctl._read_verified_journal(self.harness.journal_root)
        self.assertEqual(before.sequence, events[-1].sequence)

        mismatched = replace(
            self.proof,
            request_event_id="EVENT-DISPATCH-REQUESTED-DIFFERENT",
        )
        self.proof_path.write_bytes(mismatched.canonical_json_bytes())
        code, _, stderr = self.invoke()
        self.assertEqual(2, code)
        self.assertIn("exactly one dispatch request", stderr)

    def test_resume_strictly_rejects_hostile_or_insufficient_proof(self) -> None:
        admitted = self.proof.to_primitive()
        mutations = {
            "unknown field": lambda value: value.update(extra="rejected"),
            "wrong actor": lambda value: value["command"]["actor"].update(
                actor_type="coordinator"
            ),
            "wrong channel": lambda value: value["command"].update(
                source_channel="codex_chat"
            ),
            "applied outcome": lambda value: value["receipt"].update(
                status="applied"
            ),
            "unknown outcome": lambda value: value["receipt"].update(
                status="unknown"
            ),
            "missing process proof": lambda value: value.update(
                evidence=value["evidence"][:1]
            ),
            "unterminated process": lambda value: value.update(
                process_tree_termination_proven=False
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                candidate = copy.deepcopy(admitted)
                mutate(candidate)
                self.proof_path.write_text(
                    json.dumps(candidate, separators=(",", ":")),
                    encoding="utf-8",
                )
                code, _, stderr = self.invoke()
                self.assertEqual(2, code)
                self.assertIn("recovery proof rejected", stderr)
                self.assertEqual(
                    RuntimeState.BLOCKED,
                    self.active.cursor.snapshot.status,
                )

    def test_resume_detects_journal_change_between_read_and_replay(self) -> None:
        events = wishctl._read_verified_journal(self.harness.journal_root)
        with mock.patch.object(
            wishctl,
            "_read_verified_journal",
            return_value=events[:-1],
        ):
            code, _, stderr = self.invoke()

        self.assertEqual(2, code)
        self.assertIn("Journal changed during recovery", stderr)

    def test_recovery_proof_loader_is_bounded_and_recovery_port_is_inert(self) -> None:
        with self.assertRaisesRegex(wishctl.RecoveryCliError, "not found"):
            wishctl._load_dispatch_recovery_proof(self.root / "missing.json")
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b"x" * (wishctl.MAX_RECOVERY_PROOF_BYTES + 1))
        with self.assertRaisesRegex(wishctl.RecoveryCliError, "exceeds"):
            wishctl._load_dispatch_recovery_proof(oversized)

        port = wishctl._RecoveryOnlyTaskPort()
        self.assertEqual(
            frozenset({EffectOperation.WORKER_DISPATCH}),
            port.operations,
        )
        with self.assertRaisesRegex(wishctl.RecoveryCliError, "cannot execute"):
            port.apply(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(wishctl.RecoveryCliError, "cannot reconcile"):
            port.lookup(
                self.proof.subject_identity,
                EffectOperation.WORKER_DISPATCH,
            )


if __name__ == "__main__":
    unittest.main()
