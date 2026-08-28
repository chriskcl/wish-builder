from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.e2e.process_crash_fixture import CRASH_EXIT_CODE
from tests.e2e.support import serial_parallel_manifest
from wish_builder.adapters.process_identity import (
    LeaseOwnerProcessState,
    probe_lease_owner_process,
)
from wish_builder.services.recovery import (
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ProcessExitRecoveryE2ETests(unittest.TestCase):
    def _wait_for_prior_owner_dead(self, root: Path) -> None:
        recovered = recover_coordinator_lease(
            root / "control/journal",
            serial_parallel_manifest(),
            coordinator_epoch=1,
            repair_derived=False,
        )
        self.assertEqual(LeaseRecoveryStatus.RECOVERED, recovered.status, recovered)
        self.assertIsNotNone(recovered.lease_state)
        assert recovered.lease_state is not None
        self.assertIsNotNone(recovered.lease_state.lease)
        assert recovered.lease_state.lease is not None
        owner = recovered.lease_state.lease.owner
        deadline = time.monotonic() + 5
        while True:
            observed = probe_lease_owner_process(
                owner,
                local_host_id=owner.actor.host_id,
            )
            if observed.state is LeaseOwnerProcessState.DEAD:
                return
            if time.monotonic() >= deadline:
                self.fail(f"crashed lease owner did not become DEAD: {observed}")
            time.sleep(0.01)

    def _run_fixture(
        self,
        *arguments: object,
        expected_exit: int,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(REPOSITORY_ROOT) + (
            os.pathsep + existing if existing else ""
        )
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "tests.e2e.process_crash_fixture",
                *(str(argument) for argument in arguments),
            ),
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(
            expected_exit,
            completed.returncode,
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        return completed

    def _dispatch_process_exit(
        self,
        point: str,
        *,
        effects_at_crash: int,
        receipts_at_crash: int,
        corrupt_receipt: bool = False,
        final_effects: int = 1,
        final_receipts: int = 1,
    ) -> tuple[dict[str, object], dict[str, object]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "recovery-result.json"
            self._run_fixture(
                "crash-dispatch",
                root,
                point,
                expected_exit=CRASH_EXIT_CODE,
            )
            self._wait_for_prior_owner_dead(root)
            marker = json.loads(
                (root / "dispatch-crash.json").read_text(encoding="utf-8")
            )
            if corrupt_receipt:
                receipts = tuple(
                    (root / "control/effects/task/receipts").glob("*.json")
                )
                self.assertEqual(1, len(receipts))
                with receipts[0].open("wb") as handle:
                    handle.write(b"not-a-canonical-effect-receipt")
                    handle.flush()
                    os.fsync(handle.fileno())
            recovery_arguments: list[object] = [
                "recover-dispatch",
                root,
                result_path,
            ]
            if corrupt_receipt:
                recovery_arguments.append("--corrupt-receipt")
            self._run_fixture(*recovery_arguments, expected_exit=0)
            result = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertEqual(point, marker["point"])
            self.assertEqual("dispatch_requested", marker["last_event_type"])
            self.assertEqual(effects_at_crash, marker["effects"])
            self.assertEqual(receipts_at_crash, marker["receipts"])
            self.assertNotEqual(marker["crash_pid"], result["restart_pid"])
            self.assertEqual(marker["crash_pid"], result["crash_pid"])
            self.assertNotEqual(
                marker["crash_process_start_id"],
                result["restart_process_start_id"],
            )
            self.assertEqual(marker["boundary_head"], result["first_recovery_head"])
            self.assertEqual(1, result["first_recovery_pending_dispatches"])
            self.assertEqual("dead", result["prior_process_probe"])
            self.assertEqual("exact_alive", result["new_process_probe"])
            self.assertEqual(2, result["lease_epoch"])
            self.assertTrue(result["lease_owner_matches_restart"])
            self.assertEqual(result["request_identity"], result["receipt_identity"])
            self.assertEqual(
                result["request_identity"],
                result["observation_receipt_identity"],
            )
            observation_identity = result["observation_identity"]
            request_identity = result["request_identity"]
            self.assertEqual(
                result["lease_epoch"],
                observation_identity["coordinator_epoch"],
            )
            for field in ("run_id", "task_id", "attempt", "correlation_id"):
                self.assertEqual(request_identity[field], observation_identity[field])
            self.assertEqual(1, result["dispatch_requested_events"])
            self.assertEqual(1, result["dispatch_observed_events"])
            self.assertEqual(final_effects, result["effect_files"])
            self.assertEqual(final_receipts, result["receipt_files"])
            self.assertEqual(marker["effect_sha256"], result["effect_sha256"])
            self.assertEqual(marker["repository_head"], result["target_head"])
            self.assertTrue(result["target_clean"])
            self.assertEqual(0, result["final_pending_dispatches"])
            self.assertTrue(result["final_replay_matches_cursor"])
            self.assertEqual("rejected", result["replayed_status"])
            self.assertEqual("dispatch_not_pending", result["replayed_reason"])
            self.assertGreater(
                result["final_head"]["sequence"],
                result["first_recovery_head"]["sequence"],
            )
            self.assertLess(
                request_identity["coordinator_epoch"],
                result["lease_epoch"],
            )
            return marker, result

    def test_before_effect_process_exit_never_redispatches_old_epoch(self) -> None:
        _, result = self._dispatch_process_exit(
            "before_effect",
            effects_at_crash=0,
            receipts_at_crash=0,
            final_effects=0,
            final_receipts=0,
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("effect_absent_after_apply", result["reason"])
        self.assertEqual("absent", result["receipt_status"])
        self.assertEqual("absent", result["final_lookup_status"])
        self.assertEqual("blocked", result["run_state"])
        self.assertEqual("blocked", result["task_state"])
        self.assertIn("outcome_unknown", result["attempt_states"])

    def test_after_effect_before_receipt_process_exit_reconciles_once(self) -> None:
        _, result = self._dispatch_process_exit(
            "after_effect_before_receipt",
            effects_at_crash=1,
            receipts_at_crash=0,
        )
        self.assertEqual("progressed", result["status"])
        self.assertEqual("none", result["reason"])
        self.assertEqual("applied", result["receipt_status"])
        self.assertEqual("applied", result["final_lookup_status"])
        self.assertEqual("dispatched", result["task_state"])
        self.assertIn("running", result["attempt_states"])

    def test_after_receipt_process_exit_reconciles_without_redispatch(self) -> None:
        _, result = self._dispatch_process_exit(
            "after_receipt",
            effects_at_crash=1,
            receipts_at_crash=1,
        )
        self.assertEqual("progressed", result["status"])
        self.assertEqual("none", result["reason"])
        self.assertEqual("applied", result["receipt_status"])
        self.assertEqual("applied", result["final_lookup_status"])
        self.assertEqual("dispatched", result["task_state"])

    def test_corrupt_receipt_after_process_exit_fails_closed_unknown(self) -> None:
        _, result = self._dispatch_process_exit(
            "after_receipt",
            effects_at_crash=1,
            receipts_at_crash=1,
            corrupt_receipt=True,
        )
        self.assertEqual("blocked", result["status"])
        self.assertEqual("effect_outcome_unknown", result["reason"])
        self.assertEqual("unknown", result["receipt_status"])
        self.assertEqual("unknown", result["final_lookup_status"])
        self.assertGreater(result["receipt_evidence_count"], 0)
        self.assertEqual("blocked", result["run_state"])
        self.assertEqual("blocked", result["task_state"])
        self.assertIn("outcome_unknown", result["attempt_states"])

    def _git_boundary_process_exit(
        self,
        point: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "git-recovery-result.json"
            self._run_fixture(
                "crash-git-boundary",
                root,
                point,
                expected_exit=CRASH_EXIT_CODE,
            )
            marker = json.loads((root / "git-crash.json").read_text(encoding="utf-8"))
            self._run_fixture(
                "recover-git-boundary",
                root,
                point,
                result_path,
                expected_exit=0,
            )
            result = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertEqual(point, marker["point"])
            self.assertEqual(point, result["point"])
            self.assertNotEqual(marker["crash_pid"], result["restart_pid"])
            self.assertEqual(marker["crash_pid"], result["crash_pid"])
            self.assertNotEqual(
                marker["crash_process_start_id"],
                result["restart_process_start_id"],
            )
            self.assertTrue(result["target_clean"])
            return marker, result

    def _assert_git_attempt_process_exit(
        self,
        point: str,
        *,
        exists_at_crash: bool,
    ) -> None:
        marker, result = self._git_boundary_process_exit(point)
        self.assertEqual(exists_at_crash, marker["destination_exists"])
        self.assertEqual(1 + int(exists_at_crash), marker["worktree_count"])
        self.assertEqual(
            "applied" if exists_at_crash else "absent",
            result["observed_disposition"],
        )
        self.assertEqual(marker["destination"], result["destination"])
        self.assertTrue(result["destination_exists"])
        self.assertEqual("applied", result["recovered_disposition"])
        self.assertEqual("applied", result["replayed_disposition"])
        self.assertTrue(result["exact_replay"])
        self.assertEqual(2, result["worktree_count"])
        self.assertEqual(result["base_head"], result["target_head"])
        self.assertEqual(
            marker["initial_target_reflog_count"],
            result["target_reflog_count"],
        )

    def test_git_before_attempt_create_process_exit_creates_once(self) -> None:
        self._assert_git_attempt_process_exit(
            "before_attempt_create",
            exists_at_crash=False,
        )

    def test_git_after_attempt_create_process_exit_reconciles_once(self) -> None:
        self._assert_git_attempt_process_exit(
            "after_attempt_create",
            exists_at_crash=True,
        )

    def _assert_git_stage_process_exit(
        self,
        point: str,
        *,
        staged_at_crash: bool,
    ) -> None:
        marker, result = self._git_boundary_process_exit(point)
        self.assertEqual(staged_at_crash, marker["staged_ref_exists"])
        self.assertEqual(2, marker["worktree_count"])
        self.assertEqual(
            "applied" if staged_at_crash else "absent",
            result["observed_disposition"],
        )
        self.assertEqual("applied", result["recovered_disposition"])
        self.assertEqual("applied", result["replayed_disposition"])
        self.assertTrue(result["exact_replay"])
        self.assertEqual(1, result["staged_ref_count"])
        self.assertEqual(marker["staged_ref"], result["staged_ref"])
        self.assertEqual(marker["result_commit_sha"], result["staged_commit_sha"])
        self.assertEqual(result["base_head"], result["target_head"])
        self.assertEqual(2, result["worktree_count"])
        self.assertEqual(
            marker["initial_target_reflog_count"],
            result["target_reflog_count"],
        )

    def test_git_before_result_stage_process_exit_stages_once(self) -> None:
        self._assert_git_stage_process_exit(
            "before_result_stage",
            staged_at_crash=False,
        )

    def test_git_after_result_stage_process_exit_reconciles_once(self) -> None:
        self._assert_git_stage_process_exit(
            "after_result_stage",
            staged_at_crash=True,
        )

    def _assert_git_promotion_process_exit(
        self,
        point: str,
        *,
        promoted_at_crash: bool,
    ) -> None:
        marker, result = self._git_boundary_process_exit(point)
        self.assertEqual(
            marker["candidate_commit_sha"]
            if promoted_at_crash
            else marker["base_head"],
            marker["target_head"],
        )
        self.assertEqual(
            marker["initial_target_reflog_count"] + int(promoted_at_crash),
            marker["target_reflog_count"],
        )
        self.assertEqual(
            "applied" if promoted_at_crash else "absent",
            result["reconciled_disposition"],
        )
        self.assertEqual("applied", result["recovered_disposition"])
        self.assertEqual("applied", result["replayed_disposition"])
        self.assertTrue(result["exact_replay"])
        self.assertEqual(marker["candidate_commit_sha"], result["target_head"])
        self.assertEqual(marker["base_head"], result["candidate_parent_sha"])
        self.assertEqual(marker["source_commit_sha"], result["staged_source_sha"])
        self.assertEqual(
            marker["initial_target_reflog_count"] + 1,
            result["target_reflog_count"],
        )
        self.assertEqual(2, result["worktree_count"])

    def test_git_before_target_promotion_process_exit_promotes_once(self) -> None:
        self._assert_git_promotion_process_exit(
            "before_target_promotion",
            promoted_at_crash=False,
        )

    def test_git_after_target_promotion_process_exit_reconciles_once(self) -> None:
        self._assert_git_promotion_process_exit(
            "after_target_promotion",
            promoted_at_crash=True,
        )

    def _assert_git_cleanup_process_exit(
        self,
        point: str,
        *,
        removed_at_crash: bool,
    ) -> None:
        marker, result = self._git_boundary_process_exit(point)
        self.assertEqual(not removed_at_crash, marker["destination_exists"])
        self.assertEqual(
            marker["initial_worktree_count"] + 2 - int(removed_at_crash),
            marker["worktree_count"],
        )
        self.assertEqual(not removed_at_crash, result["inspection_exists"])
        self.assertEqual(
            "already_absent" if removed_at_crash else "removed",
            result["recovered_disposition"],
        )
        self.assertEqual("already_absent", result["replayed_disposition"])
        self.assertFalse(result["cleanup_attempt_exists"])
        self.assertTrue(result["sibling_exists"])
        self.assertEqual(marker["sibling_head"], result["sibling_head"])
        self.assertEqual(
            marker["initial_worktree_count"] + 1,
            result["worktree_count"],
        )
        self.assertEqual(marker["base_head"], result["target_head"])
        self.assertEqual(
            marker["initial_target_reflog_count"],
            result["target_reflog_count"],
        )

    def test_git_before_attempt_remove_process_exit_removes_only_target(self) -> None:
        self._assert_git_cleanup_process_exit(
            "before_attempt_remove",
            removed_at_crash=False,
        )

    def test_git_after_attempt_remove_process_exit_reconciles_only_target_absent(
        self,
    ) -> None:
        self._assert_git_cleanup_process_exit(
            "after_attempt_remove",
            removed_at_crash=True,
        )


if __name__ == "__main__":
    unittest.main()
