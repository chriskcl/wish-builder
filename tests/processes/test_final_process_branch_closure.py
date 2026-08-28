from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.processes.test_coordinator import CoordinatorHarness
from tests.processes import test_production_terminal as production_terminal_tests
from tests.processes.test_backend_workers import (
    _Clock,
    _RecordingStore,
    _ScriptedChannel,
    identity,
    plan_for,
    turn,
)
from wish_builder.contracts.runtime import RuntimeState
from wish_builder.processes.foreground import PreparedForegroundAttempt
from wish_builder.processes.production_terminal import ProductionTerminalFinalizer
from wish_builder.processes.backend_workers import BackendWorkerTurnMonitor, _ExpectedTurn
from wish_builder.services.checkpoints import CheckpointStore
from wish_builder.services.execution_checkpoints import ExecutionCheckpointStatus
from wish_builder.services.ports import TurnState


class ProductionTerminalFinalBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.helper = production_terminal_tests.ProductionTerminalFinalizerTests()
        self.helper.root = self.root
        self.harness = CoordinatorHarness(self.root / "control")
        self.cursor = self.helper._verified_cursor(self.harness)

    def finalizer(self, **overrides: object) -> ProductionTerminalFinalizer:
        return self.helper._finalizer(
            self.harness,
            self.harness.journal,
            self.cursor,
            **overrides,
        )

    def test_unknown_terminal_phase_and_failed_transition_are_rejected(self) -> None:
        invalid_snapshot = dataclasses.replace(
            self.cursor.snapshot,
            phase=RuntimeState.GATE_B_PENDING,
        )
        invalid_cursor = type(self.cursor)(
            invalid_snapshot,
            self.cursor.graph_index,
            self.cursor.lease_state,
            self.cursor.dispatch_recoveries,
        )
        with mock.patch.object(
            type(self.cursor.graph_index),
            "verify",
            return_value=True,
        ):
            result = self.finalizer().finish(invalid_cursor)
        self.assertFalse(result.completed)

        with mock.patch.object(
            ProductionTerminalFinalizer,
            "_advance",
            return_value=None,
        ):
            result = self.finalizer().finish(self.cursor)
        self.assertFalse(result.completed)
        self.assertEqual((), result.events)

    def test_recovered_release_checkpoint_failure_remains_incomplete(self) -> None:
        blocked_store = CheckpointStore(
            self.root / "blocked-checkpoints",
            control_root_validator=lambda: False,
        )
        first = self.finalizer(store=blocked_store).finish(self.cursor)
        self.assertFalse(first.completed)
        release = first.events[-1]

        finalizer = self.helper._finalizer(
            self.harness,
            self.harness.journal,
            first.cursor,
            store=blocked_store,
            recovered_terminal_event=release,
        )
        with mock.patch.object(
            finalizer._checkpoint_publisher,
            "observe",
            return_value=mock.Mock(status=ExecutionCheckpointStatus.BLOCKED),
        ):
            resumed = finalizer.finish(first.cursor)
        self.assertFalse(resumed.completed)
        self.assertEqual((release,), resumed.events)

    def test_advance_rejects_unaccepted_and_exceptional_state_events(self) -> None:
        event = self.finalizer().finish(self.cursor).events[0]
        with mock.patch(
            "wish_builder.processes.production_terminal.apply_journal_event",
            return_value=mock.Mock(accepted=False),
        ):
            self.assertIsNone(ProductionTerminalFinalizer._advance(self.cursor, event))
        with mock.patch(
            "wish_builder.processes.production_terminal.apply_journal_event",
            side_effect=ValueError("invalid transition"),
        ):
            self.assertIsNone(ProductionTerminalFinalizer._advance(self.cursor, event))

    def test_release_projection_mismatch_is_rejected(self) -> None:
        finalizer = self.finalizer()
        release = finalizer._lease_service.release

        def mismatched_release(*, event_id: str) -> object:
            mutation = release(event_id=event_id)
            return mock.Mock(
                status=mutation.status,
                append_result=mutation.append_result,
                lease_state=self.cursor.lease_state,
            )

        with mock.patch.object(
            finalizer._lease_service,
            "release",
            side_effect=mismatched_release,
        ):
            result = finalizer.finish(self.cursor)
        self.assertFalse(result.completed)
        self.assertEqual(3, len(result.events))


class BackendWorkerFinalBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.worker_identity = identity()
        self.plan = plan_for(self.worker_identity)
        self.prepared = PreparedForegroundAttempt(self.worker_identity, object())

    def monitor(self, channel: object, **overrides: object) -> BackendWorkerTurnMonitor:
        values: dict[str, object] = {
            "monotonic": _Clock().monotonic,
            "sleeper": lambda seconds: None,
        }
        values.update(overrides)
        return BackendWorkerTurnMonitor(
            channel,  # type: ignore[arg-type]
            _RecordingStore(self.root / "evidence"),
            lambda value: self.plan,
            timeout_seconds=5,
            poll_interval_seconds=1,
            **values,
        )

    def test_clock_exception_and_rollback_fail_closed(self) -> None:
        channel = _ScriptedChannel(
            {self.plan.send.operation_id: [turn(self.plan, TurnState.RUNNING)]}
        )
        exceptional = self.monitor(
            channel,
            monotonic=lambda: (_ for _ in ()).throw(OSError("clock failed")),
        )
        self.assertFalse(exceptional.run((self.prepared,)).outcomes_known)

        rollback_clock = iter((2.0, 1.0))
        rollback = self.monitor(channel, monotonic=lambda: next(rollback_clock))
        self.assertFalse(rollback.run((self.prepared,)).outcomes_known)

    def test_private_worker_guards_reject_missing_observation_and_inactive_lease(self) -> None:
        channel = _ScriptedChannel({})
        monitor = self.monitor(channel)
        expected = (_ExpectedTurn(self.prepared, self.plan),)
        self.assertIsNone(monitor._build_proposals(expected, {}))

        inactive = mock.Mock()
        inactive.lease_state.active = False
        inactive.lease_state.lease = None
        self.assertIsNone(monitor._renewal_interval(inactive))


if __name__ == "__main__":
    unittest.main()
