from __future__ import annotations

import dataclasses
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.processes import test_production_terminal as production_terminal_tests
from tests.processes.test_coordinator import CoordinatorHarness
from tests.processes.test_production_routing import (
    HASH_A,
    OBSERVED_AT,
    _ChannelFactory,
    attempt_identity,
    attempt_worktree,
    capabilities,
    dispatch_plan,
    prepared_effect,
)
from tests.processes.test_backend_workers import (
    _Clock,
    _RecordingStore,
    _ScriptedChannel,
    identity,
    plan_for,
    turn,
)
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.contracts.runtime import (
    EffectOperation,
    EffectStatus,
)
from wish_builder.processes.foreground import PreparedForegroundAttempt
from wish_builder.processes.production_routing import (
    AttemptChannelRoute,
    AttemptOperationRoute,
    AttemptBackendChannelRouter,
    WishBuilderBackendAttemptChannelFactory,
)
from wish_builder.processes.production_terminal import ProductionTerminalFinalizer
from wish_builder.processes.backend_workers import BackendWorkerTurnMonitor
from wish_builder.services.checkpoints import CheckpointStore
from wish_builder.services.ports import TurnState


class ProductionRoutingBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identity = attempt_identity(1)
        self.attempt = attempt_worktree(self.root / "attempt", self.identity)
        self.plan = dispatch_plan(self.identity)
        self.route = AttemptChannelRoute(self.attempt, self.plan)

    def router(self, factory=None) -> AttemptBackendChannelRouter:
        return AttemptBackendChannelRouter(
            (self.route,),
            expected_capabilities=capabilities(),
            channel_factory=_ChannelFactory() if factory is None else factory,
            clock=lambda: OBSERVED_AT,
        )

    def test_constructor_guards_reject_invalid_route_inputs(self) -> None:
        with self.assertRaisesRegex(TypeError, "compatibility_cell"):
            WishBuilderBackendAttemptChannelFactory(compatibility_cell=object())

        factory = WishBuilderBackendAttemptChannelFactory(
            compatibility_cell=load_bundled_compatibility().platform(
                Provider.CODEX,
                Platform.WINDOWS,
            )
        )
        with self.assertRaises(TypeError):
            factory(object())
        with self.assertRaises(ValueError):
            AttemptOperationRoute(self.identity, EffectOperation.TASK_EXECUTION, HASH_A)
        with self.assertRaises(ValueError):
            AttemptOperationRoute(self.identity, EffectOperation.CANCEL_TURN, "bad")

        for changes in (
            {"routes": []},
            {"expected_capabilities": object()},
            {"channel_factory": object()},
            {"clock": object()},
        ):
            values = {
                "routes": (self.route,),
                "expected_capabilities": capabilities(),
                "channel_factory": _ChannelFactory(),
                "clock": lambda: OBSERVED_AT,
            } | changes
            with self.subTest(changes=changes), self.assertRaises(TypeError):
                AttemptBackendChannelRouter(**values)

    def test_adapter_faults_and_wrong_observations_remain_unknown(self) -> None:
        reserve = prepared_effect(
            self.identity,
            self.plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
            event_number=1,
        )
        send = prepared_effect(
            self.identity,
            self.plan.send,
            EffectOperation.SEND_TASK_PACKET,
            event_number=2,
        )

        router = self.router()
        with mock.patch.object(router, "_channel", side_effect=OSError("offline")):
            observed = router.reserve(reserve)
        self.assertIs(observed.status, EffectStatus.UNKNOWN)
        self.assertEqual(("attempt channel adapter failed",), observed.evidence)

        router = self.router()
        channel = mock.Mock()
        channel.send.return_value = object()
        with mock.patch.object(router, "_channel", return_value=channel):
            observed_turn = router.send(send)
        self.assertIs(observed_turn.status, EffectStatus.UNKNOWN)
        self.assertIs(observed_turn.state, TurnState.UNKNOWN)
        self.assertEqual(("attempt channel observation mismatch",), observed_turn.evidence)

        with self.assertRaises(TypeError):
            router.reserve(object())
        with self.assertRaises(ValueError):
            router.inspect_turn("")


class ProductionTerminalBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.helper = production_terminal_tests.ProductionTerminalFinalizerTests()
        self.helper.root = self.root
        self.harness = CoordinatorHarness(self.root / "control")
        self.cursor = self.helper._verified_cursor(self.harness)

    def finalizer(self, **overrides) -> ProductionTerminalFinalizer:
        return self.helper._finalizer(
            self.harness,
            self.harness.journal,
            self.cursor,
            **overrides,
        )

    def test_constructor_and_cursor_guards_fail_closed(self) -> None:
        baseline = {
            "manifest": self.harness.manifest,
            "journal": self.harness.journal,
            "lease_service": self.helper._lease_service(
                self.harness,
                self.harness.journal,
                self.root / "control" / "journal",
            ),
            "checkpoint_publisher": self.finalizer()._checkpoint_publisher,
            "coordinator_id": self.harness.owner.actor.actor_id,
            "fencing_token": 1,
        }
        invalid = (
            {"manifest": object()},
            {"journal": object()},
            {"lease_service": object()},
            {"checkpoint_publisher": object()},
            {"coordinator_id": ""},
            {"fencing_token": 0},
            {"recovered_terminal_event": object()},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                ProductionTerminalFinalizer(**(baseline | changes))

        finalizer = self.finalizer()
        with self.assertRaises(TypeError):
            finalizer.finish(object())
        wrong_run = dataclasses.replace(self.cursor.snapshot, run_id="RUN-OTHER")
        mismatched = type(self.cursor)(
            wrong_run,
            self.cursor.graph_index,
            self.cursor.lease_state,
            self.cursor.dispatch_recoveries,
        )
        self.assertFalse(finalizer.finish(mismatched).completed)

    def test_inactive_lease_requires_a_matching_recovered_release(self) -> None:
        blocked = self.finalizer(
            store=CheckpointStore(
                self.root / "blocked-checkpoints",
                control_root_validator=lambda: False,
            ),
        )
        first = blocked.finish(self.cursor)
        self.assertFalse(first.completed)
        self.assertFalse(first.cursor.lease_state.active)

        resumed = self.helper._finalizer(
            self.harness,
            self.harness.journal,
            first.cursor,
        ).finish(first.cursor)
        self.assertFalse(resumed.completed)


class BackendWorkerBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def monitor(self, channel, plans, **overrides) -> BackendWorkerTurnMonitor:
        values = {
            "timeout_seconds": 5,
            "poll_interval_seconds": 1,
            "monotonic": _Clock().monotonic,
            "sleeper": lambda seconds: None,
        } | overrides
        return BackendWorkerTurnMonitor(
            channel,
            _RecordingStore(self.root / "evidence"),
            plans.__getitem__,
            **values,
        )

    def test_constructor_and_clock_failures_are_rejected(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        channel = _ScriptedChannel({plan.send.operation_id: [turn(plan, TurnState.DONE)]})
        store = _RecordingStore(self.root / "constructor-evidence")
        base = {"channel": channel, "observation_store": store, "plan_factory": lambda _: plan}
        for changes in (
            {"channel": object()},
            {"observation_store": object()},
            {"plan_factory": object()},
            {"timeout_seconds": math.inf},
            {"poll_interval_seconds": True},
            {"monotonic": object()},
            {"sleeper": object()},
            {"lease_renewal": object()},
        ):
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                BackendWorkerTurnMonitor(**(base | changes))

        invalid_clock = self.monitor(channel, {worker_identity: plan}, monotonic=lambda: math.nan)
        result = invalid_clock.run((PreparedForegroundAttempt(worker_identity, object()),))
        self.assertFalse(result.outcomes_known)
        with self.assertRaises(TypeError):
            invalid_clock.run((PreparedForegroundAttempt(worker_identity, object()),), object())

    def test_plan_factory_and_sleeper_failures_are_contained(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        attempt = PreparedForegroundAttempt(worker_identity, object())
        channel = _ScriptedChannel({plan.send.operation_id: [turn(plan, TurnState.RUNNING)]})

        unavailable = BackendWorkerTurnMonitor(
            channel,
            _RecordingStore(self.root / "unavailable"),
            lambda _: (_ for _ in ()).throw(OSError("plan unavailable")),
            monotonic=lambda: 0.0,
            sleeper=lambda seconds: None,
        ).run((attempt,))
        self.assertFalse(unavailable.outcomes_known)
        self.assertEqual([], channel.inspect_calls)

        sleeping_channel = _ScriptedChannel(
            {plan.send.operation_id: [turn(plan, TurnState.RUNNING)]}
        )
        monitor = self.monitor(
            sleeping_channel,
            {worker_identity: plan},
            sleeper=lambda seconds: (_ for _ in ()).throw(OSError("sleep failed")),
        )
        result = monitor.run((attempt,))
        self.assertFalse(result.outcomes_known)
        self.assertEqual([plan.send.operation_id], sleeping_channel.inspect_calls)


if __name__ == "__main__":
    unittest.main()
