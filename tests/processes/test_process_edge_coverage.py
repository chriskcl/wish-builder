from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.processes.test_coordinator import (
    BASE_TIME,
    COORDINATOR_ID,
    CoordinatorHarness,
    lease_owner,
    sibling_manifest,
)
from wish_builder.adapters.fake import FakeModelPort
from wish_builder.adapters.git_worktree import GitWorktreeAdapter, StagedResult
from wish_builder.contracts.runtime import (
    AdapterKind,
    ExecutionIdentity,
    RuntimeReasonCode,
)
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.processes.coordinator import (
    CoordinatorCursor,
    CoordinatorReason,
    CoordinatorStatus,
    CoordinatorStepResult,
    ForegroundCoordinator,
    WorkerResultProposal,
)
from wish_builder.processes.workflow import (
    AcceptanceResult,
    LocalExecutionWorkflow,
)
from wish_builder.services.journal import CoordinatorLeaseState


class _TaskPortWithoutDispatch:
    @property
    def adapter_kind(self):
        return AdapterKind.TASK

    @property
    def operations(self):
        return frozenset()

    def apply(self, request):
        raise AssertionError("apply must not be reached")

    def lookup(self, identity, operation):
        raise AssertionError("lookup must not be reached")


class _Acceptance:
    def verify(self, task, repository, promotion):
        raise AssertionError("verify must not be reached")


class ProcessBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.harness = CoordinatorHarness(self.root / "control")
        self.cursor = self.harness.coordinator.cursor
        self.repository = object.__new__(GitWorktreeAdapter)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def coordinator(self, **overrides) -> ForegroundCoordinator:
        values = {
            "manifest": self.harness.manifest,
            "cursor": self.cursor,
            "journal": self.harness.journal,
            "task_port": self.harness.port,
            "coordinator_id": COORDINATOR_ID,
            "owner": self.harness.owner,
            "fencing_token": 1,
            "authority_clock": lambda: BASE_TIME,
        }
        values.update(overrides)
        return ForegroundCoordinator(**values)

    def workflow(self, **overrides) -> LocalExecutionWorkflow:
        values = {
            "manifest": self.harness.manifest,
            "cursor": self.cursor,
            "journal": self.harness.journal,
            "repository": self.repository,
            "coordinator_id": COORDINATOR_ID,
            "owner": self.harness.owner,
            "fencing_token": 1,
            "authority_clock": lambda: BASE_TIME,
        }
        values.update(overrides)
        return LocalExecutionWorkflow(**values)

    def test_coordinator_cursor_rejects_wrong_components_and_head_drift(self) -> None:
        cases = (
            (object(), self.cursor.graph_index, self.cursor.lease_state, TypeError),
            (self.cursor.snapshot, object(), self.cursor.lease_state, TypeError),
            (self.cursor.snapshot, self.cursor.graph_index, object(), TypeError),
            (
                self.cursor.snapshot,
                self.cursor.graph_index,
                CoordinatorLeaseState.initial(),
                ValueError,
            ),
        )
        for snapshot, graph_index, lease_state, error in cases:
            with self.subTest(
                error=error,
                value=type(snapshot).__name__,
            ), self.assertRaises(error):
                CoordinatorCursor(snapshot, graph_index, lease_state)

        self.assertEqual(self.cursor.lease_state.head, self.cursor.head)

    def test_worker_result_proposal_rejects_every_invalid_shape(self) -> None:
        run_id = self.harness.manifest.run_id
        complete = ExecutionIdentity(run_id, 1, "TASK-001", 1, "CORRELATION-001")
        without_correlation = ExecutionIdentity(run_id, 1, "TASK-001", 1)
        cases = (
            ({"identity": object()}, ValueError),
            ({"identity": ExecutionIdentity(run_id, 1)}, ValueError),
            ({"identity": without_correlation}, ValueError),
            ({"actor_id": 1}, ValueError),
            ({"actor_id": ""}, ValueError),
            ({"succeeded": 1}, TypeError),
            ({"reason_code": "bad"}, TypeError),
            ({"reason_code": RuntimeReasonCode.INVARIANT_VIOLATION}, ValueError),
            ({"succeeded": False}, ValueError),
            ({"evidence": []}, TypeError),
            ({"evidence": (object(),)}, TypeError),
        )
        defaults = {
            "identity": complete,
            "actor_id": "worker-001",
            "succeeded": True,
            "reason_code": None,
            "evidence": (),
        }
        for changes, error in cases:
            values = defaults | changes
            with self.subTest(changes=changes), self.assertRaises(error):
                WorkerResultProposal(**values)

        failed = WorkerResultProposal(
            complete,
            "worker-001",
            False,
            RuntimeReasonCode.INVARIANT_VIOLATION,
        )
        self.assertFalse(failed.succeeded)

    def test_step_result_rejects_invalid_types_and_status_reason_pairs(self) -> None:
        valid = {
            "status": CoordinatorStatus.IDLE,
            "reason": CoordinatorReason.NO_READY_TASKS,
            "cursor": self.cursor,
            "events": (),
            "dispatched": (),
            "receipt": None,
        }
        cases = (
            ({"status": "idle"}, TypeError),
            ({"reason": "no_ready_tasks"}, TypeError),
            ({"cursor": object()}, TypeError),
            ({"events": []}, TypeError),
            ({"events": (object(),)}, TypeError),
            ({"dispatched": []}, TypeError),
            ({"dispatched": (object(),)}, TypeError),
            ({"receipt": object()}, TypeError),
            (
                {
                    "status": CoordinatorStatus.PROGRESSED,
                    "reason": CoordinatorReason.NO_READY_TASKS,
                },
                ValueError,
            ),
            ({"status": CoordinatorStatus.IDLE, "reason": CoordinatorReason.NONE}, ValueError),
        )
        for changes, error in cases:
            with self.subTest(changes=changes), self.assertRaises(error):
                CoordinatorStepResult(**(valid | changes))

    def test_coordinator_constructor_fails_closed_for_every_dependency(self) -> None:
        other_graph = GraphIndex.compile(sibling_manifest())
        mismatched_graph_cursor = CoordinatorCursor(
            self.cursor.snapshot,
            other_graph,
            self.cursor.lease_state,
        )
        wrong_run_cursor = CoordinatorCursor(
            replace(self.cursor.snapshot, run_id="WISH-OTHER"),
            self.cursor.graph_index,
            self.cursor.lease_state,
        )
        wrong_epoch_cursor = CoordinatorCursor(
            replace(self.cursor.snapshot, coordinator_epoch=2),
            self.cursor.graph_index,
            self.cursor.lease_state,
        )
        cases = (
            ({"manifest": object()}, TypeError),
            ({"cursor": object()}, TypeError),
            ({"journal": object()}, TypeError),
            ({"task_port": object()}, TypeError),
            ({"task_port": FakeModelPort(self.root / "model")}, ValueError),
            ({"task_port": _TaskPortWithoutDispatch()}, ValueError),
            ({"coordinator_id": 1}, ValueError),
            ({"coordinator_id": ""}, ValueError),
            ({"owner": object()}, TypeError),
            ({"owner": lease_owner("other-coordinator")}, ValueError),
            ({"fencing_token": "1"}, ValueError),
            ({"fencing_token": 0}, ValueError),
            ({"authority_clock": None}, TypeError),
            ({"event_id_factory": None}, TypeError),
            ({"correlation_id_factory": None}, TypeError),
            ({"cursor": wrong_run_cursor}, ValueError),
            ({"cursor": wrong_epoch_cursor}, ValueError),
            ({"cursor": mismatched_graph_cursor}, ValueError),
        )
        for changes, error in cases:
            with self.subTest(changes=tuple(changes)), self.assertRaises(error):
                self.coordinator(**changes)

    def test_coordinator_public_input_guards_and_clock_contract(self) -> None:
        coordinator = self.coordinator()
        self.assertEqual(self.harness.manifest.canonical_sha256(), coordinator.manifest_digest)
        for limit in (0, -1, "1", True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                coordinator.dispatch_ready(limit=limit)
        for method in (coordinator.dispatch_task, coordinator.resume_dispatch):
            for task_id in (None, "", 1):
                with self.subTest(
                    method=method.__name__,
                    task_id=task_id,
                ), self.assertRaises(ValueError):
                    method(task_id)
        with self.assertRaises(TypeError):
            coordinator.reconcile_dispatch(object())
        with self.assertRaises(TypeError):
            coordinator.accept_worker_result(object())

        for clock in (
            lambda: "not-a-datetime",
            lambda: BASE_TIME.replace(tzinfo=None),
        ):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                self.coordinator(authority_clock=clock).dispatch_ready(limit=1)

    def test_acceptance_result_rejects_inconsistent_outcomes(self) -> None:
        cases = (
            ({"accepted": 1, "evidence": ()}, TypeError),
            ({"accepted": True, "evidence": []}, TypeError),
            ({"accepted": True, "evidence": (object(),)}, TypeError),
            ({"accepted": True, "evidence": ()}, ValueError),
            (
                {
                    "accepted": True,
                    "evidence": (),
                    "reason_code": RuntimeReasonCode.INVARIANT_VIOLATION,
                },
                ValueError,
            ),
            ({"accepted": False, "evidence": (), "reason_code": None}, ValueError),
            ({"accepted": False, "evidence": (), "reason_code": "bad"}, ValueError),
        )
        for values, error in cases:
            with self.subTest(values=values), self.assertRaises(error):
                AcceptanceResult(**values)

        rejected = AcceptanceResult(
            False,
            (),
            RuntimeReasonCode.INVARIANT_VIOLATION,
        )
        self.assertFalse(rejected.accepted)

    def test_workflow_constructor_fails_closed_for_every_dependency(self) -> None:
        other_graph = GraphIndex.compile(sibling_manifest())
        mismatched_graph_cursor = CoordinatorCursor(
            self.cursor.snapshot,
            other_graph,
            self.cursor.lease_state,
        )
        wrong_run_cursor = CoordinatorCursor(
            replace(self.cursor.snapshot, run_id="WISH-OTHER"),
            self.cursor.graph_index,
            self.cursor.lease_state,
        )
        wrong_epoch_cursor = CoordinatorCursor(
            replace(self.cursor.snapshot, coordinator_epoch=2),
            self.cursor.graph_index,
            self.cursor.lease_state,
        )
        cases = (
            ({"manifest": object()}, TypeError),
            ({"cursor": object()}, TypeError),
            ({"journal": object()}, TypeError),
            ({"repository": object()}, TypeError),
            ({"coordinator_id": 1}, ValueError),
            ({"coordinator_id": ""}, ValueError),
            ({"owner": object()}, ValueError),
            ({"owner": lease_owner("other-coordinator")}, ValueError),
            ({"fencing_token": "1"}, ValueError),
            ({"fencing_token": 0}, ValueError),
            ({"authority_clock": None}, TypeError),
            ({"cursor": wrong_run_cursor}, ValueError),
            ({"cursor": wrong_epoch_cursor}, ValueError),
            ({"cursor": mismatched_graph_cursor}, ValueError),
        )
        for changes, error in cases:
            with self.subTest(changes=tuple(changes)), self.assertRaises(error):
                self.workflow(**changes)

    def test_workflow_public_guards_and_private_fail_closed_helpers(self) -> None:
        workflow = self.workflow()
        with self.assertRaises(TypeError):
            workflow.stage_attempt_result(object())
        for sources in ([], (object(),)):
            with self.subTest(sources=sources), self.assertRaises(TypeError):
                workflow.promote_staged(sources, _Acceptance())
        with self.assertRaises(TypeError):
            workflow.promote_staged((), object())

        self.assertFalse(workflow._current_attempt(object(), self.cursor.snapshot.status))
        self.assertFalse(
            workflow._current_attempt(
                ExecutionIdentity(self.harness.manifest.run_id, 1),
                self.cursor.snapshot.status,
            )
        )
        wrong_epoch = ExecutionIdentity(
            self.harness.manifest.run_id,
            2,
            "TASK-001",
            1,
            "CORRELATION-001",
        )
        self.assertFalse(workflow._current_attempt(wrong_epoch, self.cursor.snapshot.status))
        with self.assertRaises(ValueError):
            workflow._task(None)

        with self.assertRaises(ValueError):
            self.workflow(authority_clock=lambda: "bad").prepare_attempt(
                ExecutionIdentity(self.harness.manifest.run_id, 1)
            )

    def test_workflow_rejects_invalid_staged_tuple_before_repository_access(self) -> None:
        workflow = self.workflow()
        staged = object.__new__(StagedResult)
        with self.assertRaises((AttributeError, TypeError, ValueError)):
            workflow.promote_staged((staged,), _Acceptance())


if __name__ == "__main__":
    unittest.main()
