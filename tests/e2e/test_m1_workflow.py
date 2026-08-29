from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tests.e2e.failpoints import CrashOnce
from tests.e2e.support import E2EAcceptance, E2EHarness
from wish_builder.adapters.fake import FakeEffectCrash, FakeTaskPort
from wish_builder.contracts.runtime import JournalEventType, RuntimeState
from wish_builder.presentation import export_trace
from wish_builder.processes import (
    CoordinatorStatus,
    ProcessOutcomeStatus,
    WorkerResultProposal,
    WorkflowStatus,
)
from wish_builder.processes.acceptance import ProcessAcceptancePort


class ActiveM1WorkflowE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.harness = E2EHarness(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _complete_single(
        self,
        cursor,
        *,
        expected_wave: int,
        process_acceptance: bool = False,
    ):
        reserved = self.harness.coordinator(cursor).reserve_ready(limit=1)
        self.assertEqual(CoordinatorStatus.PROGRESSED, reserved.status)
        self.assertEqual(1, len(reserved.reserved))
        identity = reserved.reserved[0]
        task = next(
            task for task in self.harness.manifest.tasks if task.id == identity.task_id
        )
        self.assertEqual(expected_wave, task.wave)

        prepared = self.harness.workflow(reserved.cursor).prepare_attempt(identity)
        self.assertEqual(WorkflowStatus.PROGRESSED, prepared.status, prepared)
        self.assertIsNotNone(prepared.attempt)
        assert prepared.attempt is not None and identity.task_id is not None
        dispatched = self.harness.coordinator(prepared.cursor).dispatch_reserved(identity)
        self.assertEqual(CoordinatorStatus.PROGRESSED, dispatched.status)
        relative_path = self.harness.result_path(
            identity.task_id, self.harness.manifest
        )
        marker = self.root / "markers" / f"{identity.task_id}.json"
        outcome = self.harness.run_worker(
            prepared.attempt,
            relative_path,
            f"implemented {identity.task_id}",
            marker,
        )
        self.assertEqual(ProcessOutcomeStatus.SUCCESS, outcome.status, outcome)
        self.harness.commit_attempt(prepared.attempt, relative_path)

        accepted = self.harness.coordinator(dispatched.cursor).accept_worker_result(
            WorkerResultProposal(identity, f"worker-{identity.task_id}", True)
        )
        self.assertEqual(CoordinatorStatus.PROGRESSED, accepted.status, accepted)
        staged = self.harness.workflow(accepted.cursor).stage_attempt_result(
            prepared.attempt
        )
        self.assertEqual(WorkflowStatus.PROGRESSED, staged.status, staged)
        self.assertIsNotNone(staged.staged)
        assert staged.staged is not None
        acceptance = (
            ProcessAcceptancePort(
                identity=prepared.attempt.identity,
                executable_profiles={"python": sys.executable},
                clock=lambda: "2026-08-19T00:00:40Z",
            )
            if process_acceptance
            else E2EAcceptance((staged.staged,))
        )
        promoted = self.harness.workflow(staged.cursor).promote_staged(
            (staged.staged,),
            acceptance,
        )
        self.assertEqual(WorkflowStatus.PROGRESSED, promoted.status, promoted)
        return promoted.cursor, identity.task_id

    def test_real_project_acceptance_command_runs_in_promotion_candidate(self) -> None:
        self.harness = E2EHarness(
            self.root / "process-acceptance",
            acceptance_executable=sys.executable,
        )

        cursor, task_id = self._complete_single(
            self.harness.cursor,
            expected_wave=0,
            process_acceptance=True,
        )

        self.assertEqual("TASK-001", task_id)
        self.assertEqual(
            "implemented TASK-001\n",
            (self.harness.repository / "src/req-001/result.txt").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual(
            RuntimeState.VERIFIED,
            dict(cursor.graph_index.task_states)[task_id],
        )

    def _assert_dispatch_crash_recovery(
        self,
        point: str,
        *,
        effects_before_recovery: int,
        receipts_before_recovery: int,
    ) -> None:
        failpoint = CrashOnce(point)
        self.harness.port = FakeTaskPort(
            self.harness.control_root / "effects",
            clock=lambda: "2026-08-19T00:00:30Z",
            failpoint=failpoint,
        )
        with self.assertRaises(FakeEffectCrash):
            self.harness.coordinator().dispatch_ready(limit=1)

        effects_root = self.harness.control_root / "effects"
        effect_files = tuple((effects_root / "task" / "effects").glob("*.json"))
        receipt_files = tuple((effects_root / "task" / "receipts").glob("*.json"))
        self.assertEqual(effects_before_recovery, len(effect_files))
        self.assertEqual(receipts_before_recovery, len(receipt_files))

        recovered, cursor = self.harness.recover()
        self.assertEqual(1, len(recovered.pending_dispatch_requests))
        request = recovered.pending_dispatch_requests[0]
        self.assertEqual(JournalEventType.DISPATCH_REQUESTED, request.event_type)

        self.harness.port = FakeTaskPort(
            effects_root,
            clock=lambda: "2026-08-19T00:00:30Z",
        )
        reconciled = self.harness.coordinator(cursor).reconcile_dispatch(request)
        self.assertEqual(CoordinatorStatus.PROGRESSED, reconciled.status, reconciled)
        self.assertEqual(1, len(reconciled.dispatched))

        recovered_again, replayed_cursor = self.harness.recover()
        self.assertEqual((), recovered_again.pending_dispatch_requests)
        self.assertEqual(reconciled.cursor.snapshot, replayed_cursor.snapshot)
        effect_files = tuple((effects_root / "task" / "effects").glob("*.json"))
        receipt_files = tuple((effects_root / "task" / "receipts").glob("*.json"))
        self.assertEqual(1, len(effect_files))
        self.assertEqual(1, len(receipt_files))
        self.assertEqual(
            1,
            sum(
                event.event_type is JournalEventType.DISPATCH_REQUESTED
                for event in self.harness.events()
            ),
        )

    def test_gate_to_serial_parallel_serial_real_process_workflow(self) -> None:
        cursor, foundation_id = self._complete_single(
            self.harness.cursor,
            expected_wave=0,
        )
        self.assertEqual(("TASK-003", "TASK-004"), cursor.graph_index.ready_set)

        reserved = self.harness.coordinator(cursor).reserve_ready()
        self.assertEqual(CoordinatorStatus.PROGRESSED, reserved.status)
        self.assertEqual(
            ("TASK-003", "TASK-004"),
            tuple(identity.task_id for identity in reserved.reserved),
        )

        preparations = {}
        cursor = reserved.cursor
        for identity in reserved.reserved:
            prepared = self.harness.workflow(cursor).prepare_attempt(identity)
            self.assertEqual(WorkflowStatus.PROGRESSED, prepared.status, prepared)
            self.assertIsNotNone(prepared.attempt)
            assert prepared.attempt is not None and identity.task_id is not None
            preparations[identity.task_id] = prepared
            cursor = prepared.cursor

        for identity in reserved.reserved:
            dispatched = self.harness.coordinator(cursor).dispatch_reserved(identity)
            self.assertEqual(CoordinatorStatus.PROGRESSED, dispatched.status)
            cursor = dispatched.cursor

        completion_order = []
        outcomes = {}
        delays = {"TASK-003": 0.30, "TASK-004": 0.05}
        barrier = self.root / "markers" / "parallel-barrier"
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}
            for task_id, prepared in preparations.items():
                relative_path = self.harness.result_path(task_id, self.harness.manifest)
                marker = self.root / "markers" / f"{task_id}.json"
                future = executor.submit(
                    self.harness.run_worker,
                    prepared.attempt,
                    relative_path,
                    f"implemented {task_id}",
                    marker,
                    delay=delays[task_id],
                    barrier=barrier,
                    barrier_count=2,
                )
                futures[future] = task_id
            for future in as_completed(futures):
                task_id = futures[future]
                completion_order.append(task_id)
                outcomes[task_id] = future.result()

        self.assertEqual(["TASK-004", "TASK-003"], completion_order)
        markers = {
            task_id: json.loads(
                (self.root / "markers" / f"{task_id}.json").read_text(encoding="utf-8")
            )
            for task_id in preparations
        }
        self.assertLess(
            max(value["started_ns"] for value in markers.values()),
            min(value["finished_ns"] for value in markers.values()),
        )
        self.assertEqual(2, len({value["pid"] for value in markers.values()}))

        staged_by_task = {}
        for task_id in completion_order:
            outcome = outcomes[task_id]
            self.assertEqual(ProcessOutcomeStatus.SUCCESS, outcome.status, outcome)
            prepared = preparations[task_id]
            relative_path = self.harness.result_path(task_id, self.harness.manifest)
            self.harness.commit_attempt(prepared.attempt, relative_path)
            identity = prepared.attempt.identity
            accepted = self.harness.coordinator(cursor).accept_worker_result(
                WorkerResultProposal(identity, f"worker-{task_id}", True)
            )
            self.assertEqual(CoordinatorStatus.PROGRESSED, accepted.status, accepted)
            staged = self.harness.workflow(accepted.cursor).stage_attempt_result(
                prepared.attempt
            )
            self.assertEqual(WorkflowStatus.PROGRESSED, staged.status, staged)
            self.assertIsNotNone(staged.staged)
            assert staged.staged is not None
            staged_by_task[task_id] = staged.staged
            cursor = staged.cursor

        self.assertEqual((), cursor.graph_index.ready_set)
        reversed_sources = tuple(
            staged_by_task[task_id] for task_id in completion_order
        )
        promoted = self.harness.workflow(cursor).promote_staged(
            reversed_sources,
            E2EAcceptance(reversed_sources),
        )
        self.assertEqual(WorkflowStatus.PROGRESSED, promoted.status, promoted)
        self.assertEqual(
            ("TASK-003", "TASK-004"),
            tuple(record.task_id for record in promoted.promoted),
        )
        self.assertEqual(("TASK-002",), promoted.cursor.graph_index.ready_set)

        cursor, integration_id = self._complete_single(
            promoted.cursor,
            expected_wave=2,
        )
        self.assertEqual("TASK-001", foundation_id)
        self.assertEqual("TASK-002", integration_id)
        self.assertEqual((), cursor.graph_index.ready_set)
        self.assertTrue(
            all(
                state is RuntimeState.VERIFIED
                for _, state in cursor.graph_index.task_states
            )
        )

        recovered, recovered_cursor = self.harness.recover()
        self.assertEqual((), recovered.pending_dispatch_requests)
        self.assertEqual(cursor.snapshot, recovered_cursor.snapshot)
        self.assertEqual(cursor.graph_index, recovered_cursor.graph_index)
        events = self.harness.events()
        trace_one = export_trace(
            self.harness.manifest,
            recovered_cursor.snapshot,
            recovered_cursor.graph_index,
            events,
        )
        trace_two = export_trace(
            self.harness.manifest,
            recovered_cursor.snapshot,
            recovered_cursor.graph_index,
            iter(events),
        )
        self.assertEqual(trace_one.json_bytes, trace_two.json_bytes)
        self.assertEqual(trace_one.markdown_bytes, trace_two.markdown_bytes)
        self.assertEqual(2, len(self.harness.decision_summaries))

    def test_restart_reconciles_applied_dispatch_without_duplicate_effect(self) -> None:
        self._assert_dispatch_crash_recovery(
            "after_effect_before_receipt",
            effects_before_recovery=1,
            receipts_before_recovery=0,
        )

    def test_restart_retries_only_when_before_effect_is_proven_absent(self) -> None:
        self._assert_dispatch_crash_recovery(
            "before_effect",
            effects_before_recovery=0,
            receipts_before_recovery=0,
        )

    def test_restart_reconciles_receipt_before_observation_without_second_effect(
        self,
    ) -> None:
        self._assert_dispatch_crash_recovery(
            "after_receipt",
            effects_before_recovery=1,
            receipts_before_recovery=1,
        )

    def test_restart_after_observation_rebuilds_projection_without_redispatch(
        self,
    ) -> None:
        dispatched = self.harness.coordinator().dispatch_ready(limit=1)
        self.assertEqual(CoordinatorStatus.PROGRESSED, dispatched.status)
        self.assertEqual(1, len(dispatched.dispatched))

        effects_root = self.harness.control_root / "effects"
        effect_files = tuple((effects_root / "task" / "effects").glob("*.json"))
        receipt_files = tuple((effects_root / "task" / "receipts").glob("*.json"))
        self.assertEqual(1, len(effect_files))
        self.assertEqual(1, len(receipt_files))

        recovered, cursor = self.harness.recover()
        self.assertEqual((), recovered.pending_dispatch_requests)
        self.assertEqual(dispatched.cursor.snapshot, cursor.snapshot)
        self.assertEqual(
            1,
            sum(
                event.event_type is JournalEventType.DISPATCH_REQUESTED
                for event in self.harness.events()
            ),
        )


if __name__ == "__main__":
    unittest.main()
