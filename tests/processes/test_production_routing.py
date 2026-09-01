from __future__ import annotations

from wish_builder.adapters.fakes import FakeBackendChannelPort

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.contracts.test_compatibility_v2 import _enabled_v2_primitive
from wish_builder.adapters.git_identity import capture_filesystem_identity
from wish_builder.adapters.git_worktree import AttemptWorktree
from wish_builder.adapters.trellis import (
    FakeTrellisLifecyclePort,
    FakeExternalState,
)
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts import (
    WorkerProvider,
    canonical_json_bytes,
    canonical_sha256,
    decode_compatibility_bundle_primitive,
)
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.contracts.manifest_v2 import PathCaseMode
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
)
from wish_builder.processes.production_routing import (
    AttemptChannelRoute,
    AttemptOperationRoute,
    AttemptBackendChannelRouter,
    BackendDispatchUnavailable,
    WishBuilderBackendAttemptChannelFactory,
)
from wish_builder.services.dispatch_recovery import PendingExternalEffect
from wish_builder.services.journal import AppendResult, AppendStatus, JournalHead
from wish_builder.services.ports import (
    AttemptObservation,
    CancelTurn,
    BackendCapabilities,
    ChannelObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PrepareAttempt,
    PreparedEffect,
    ReserveChannel,
    SendTaskPacket,
    BackendChannelPort,
    TrellisLifecyclePort,
    TrellisLifecycleState,
    TurnState,
)
from wish_builder.services.backend_effects import BackendDispatchPlan

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
GENESIS_HASH = "sha256:" + "0" * 64
OBSERVED_AT = "2026-08-19T10:00:00Z"


def capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        provider=WorkerProvider.CODEX,
        platform="windows",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest=HASH_C,
        max_task_packet_bytes=4096,
    )


def attempt_identity(number: int) -> ExecutionIdentity:
    return ExecutionIdentity(
        "RUN-PRODUCTION-ROUTING",
        7,
        f"TASK-{number:03d}",
        number,
        f"DISPATCH-{number:03d}",
    )


def dispatch_plan(identity: ExecutionIdentity) -> BackendDispatchPlan:
    assert identity.task_id is not None
    assert identity.correlation_id is not None
    assert identity.attempt is not None
    suffix = f"{identity.attempt:03d}"
    packet = '{"task_id":"%s"}' % identity.task_id
    return BackendDispatchPlan(
        ReserveChannel(
            operation_id=f"RESERVE-{suffix}",
            attempt_id=f"ATTEMPT-{suffix}",
            dispatch_id=identity.correlation_id,
            channel_id=f"CHANNEL-{suffix}",
            provider=WorkerProvider.CODEX,
            capability_digest=HASH_A,
            launch_profile_digest=HASH_B,
            policy_digest=HASH_C,
        ),
        SendTaskPacket(
            operation_id=f"SEND-{suffix}",
            attempt_id=f"ATTEMPT-{suffix}",
            dispatch_id=identity.correlation_id,
            channel_id=f"CHANNEL-{suffix}",
            message_id=f"MESSAGE-{suffix}",
            turn_id=f"TURN-{suffix}",
            task_packet=packet,
            task_packet_digest=(
                "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()
            ),
        ),
    )


def lifecycle_dispatch_plan(identity: ExecutionIdentity) -> BackendDispatchPlan:
    plan = dispatch_plan(identity)
    assert identity.task_id is not None
    assert identity.correlation_id is not None
    packet = canonical_json_bytes(
        {
            "execution": {
                "dispatch_id": identity.correlation_id,
                "identity": identity.to_primitive(),
                "manifest_digest": HASH_A,
                "run_id": identity.run_id,
            },
            "kind": "wish_builder_task_packet",
            "task": {
                "id": identity.task_id,
                "trellis_task_id": f"TRELLIS-{identity.task_id}",
            },
            "trellis": {
                "graph_digest": HASH_B,
                "parent_task_id": "TRELLIS-PARENT-001",
            },
        }
    ).decode("utf-8")
    return BackendDispatchPlan(
        plan.reserve,
        replace(
            plan.send,
            task_packet=packet,
            task_packet_digest=(
                "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()
            ),
        ),
    )


def prepared_effect(
    attempt: ExecutionIdentity,
    command: (
        PrepareAttempt
        | ReserveChannel
        | SendTaskPacket
        | CancelTurn
        | CheckAttempt
        | FinishAttempt
    ),
    operation: EffectOperation,
    *,
    event_number: int,
) -> PreparedEffect:
    object_type = {
        EffectOperation.PREPARE_ATTEMPT: EffectObjectType.ATTEMPT,
        EffectOperation.RESERVE_CHANNEL: EffectObjectType.CHANNEL,
        EffectOperation.SEND_TASK_PACKET: EffectObjectType.TASK_PACKET,
        EffectOperation.CANCEL_TURN: EffectObjectType.TURN,
        EffectOperation.CHECK_ATTEMPT: EffectObjectType.ATTEMPT,
        EffectOperation.FINISH_ATTEMPT: EffectObjectType.ATTEMPT,
    }[operation]
    identity = replace(attempt, correlation_id=command.operation_id)
    event = JournalEvent.create(
        sequence=1,
        event_id=f"EVENT-ROUTING-{event_number:03d}",
        event_type=JournalEventType.EFFECT_REQUESTED,
        identity=identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-routing",
        recorded_at=OBSERVED_AT,
        previous_event_hash=GENESIS_HASH,
        payload=EffectRequestPayload(
            operation,
            (
                AdapterKind.TRELLIS
                if operation
                in {
                    EffectOperation.PREPARE_ATTEMPT,
                    EffectOperation.CHECK_ATTEMPT,
                    EffectOperation.FINISH_ATTEMPT,
                }
                else AdapterKind.BACKEND
            ),
            object_type,
            "sha256:" + canonical_sha256(identity.to_primitive()),
            command.canonical_sha256(),
            0,
            identity.coordinator_epoch,
        ),
    )
    result = AppendResult(
        AppendStatus.COMMITTED,
        JournalHead(event.sequence, event.event_hash),
        event,
    )
    return PreparedEffect.from_append_result(result, command)


def attempt_worktree(root: Path, identity: ExecutionIdentity) -> AttemptWorktree:
    root.mkdir()
    resolved_root = root.resolve(strict=True)
    git_dir = resolved_root / ".attempt-git"
    git_dir.mkdir()
    return AttemptWorktree(
        identity=identity,
        path=str(resolved_root),
        external_object_id=f"attempt-{identity.attempt}",
        local_repository_id=HASH_A,
        target_workspace_hash=HASH_B,
        worktree_root=capture_filesystem_identity(resolved_root),
        git_dir=capture_filesystem_identity(git_dir),
        base_commit_sha="1" * 40,
        base_tree_sha="2" * 40,
        owned_paths=(f"src/{identity.task_id}/**",),
        allowed_auxiliary_paths=(),
        protected_paths=(".wish-builder/**",),
        path_case_mode=PathCaseMode.SENSITIVE,
    )


class _RecordingChannel:
    def __init__(
        self,
        delegate: FakeBackendChannelPort,
        path: str,
        calls: list[tuple[str, str]],
    ) -> None:
        self.delegate = delegate
        self.lifecycle = FakeTrellisLifecyclePort(
            state=delegate.state,
            worktree_path=path,
        )
        self.path = path
        self.calls = calls

    def probe(self):
        self.calls.append(("probe", self.path))
        return self.delegate.probe()

    def reserve(self, effect):
        self.calls.append(("reserve", self.path))
        return self.delegate.reserve(effect)

    def send(self, effect):
        self.calls.append(("send", self.path))
        return self.delegate.send(effect)

    def cancel(self, effect):
        self.calls.append(("cancel", self.path))
        return self.delegate.cancel(effect)

    def inspect_reservation(self, operation_id):
        self.calls.append(("inspect_reservation", self.path))
        return self.delegate.inspect_reservation(operation_id)

    def inspect_turn(self, operation_id):
        self.calls.append(("inspect_turn", self.path))
        return self.delegate.inspect_turn(operation_id)

    def prepare_attempt(self, effect):
        self.calls.append(("prepare_attempt", self.path))
        return self.lifecycle.prepare_attempt(effect)

    def check_attempt(self, effect):
        self.calls.append(("check_attempt", self.path))
        return self.lifecycle.check_attempt(effect)

    def finish_attempt(self, effect):
        self.calls.append(("finish_attempt", self.path))
        return self.lifecycle.finish_attempt(effect)

    def inspect_attempt(self, operation_id, **keywords):
        self.calls.append(("inspect_attempt", self.path))
        return self.lifecycle.inspect_attempt(operation_id, **keywords)

    def inspect_check(self, operation_id, **keywords):
        self.calls.append(("inspect_check", self.path))
        return self.lifecycle.inspect_check(operation_id, **keywords)

    def inspect_finish(self, operation_id, **keywords):
        self.calls.append(("inspect_finish", self.path))
        return self.lifecycle.inspect_finish(operation_id, **keywords)


class _RecordingLifecycle:
    def __init__(
        self,
        delegate: FakeTrellisLifecyclePort,
        path: str,
        calls: list[tuple[str, str]],
    ) -> None:
        self.delegate = delegate
        self.path = path
        self.calls = calls

    def prepare_attempt(self, effect):
        self.calls.append(("prepare_attempt", self.path))
        return self.delegate.prepare_attempt(effect)

    def check_attempt(self, effect):
        self.calls.append(("check_attempt", self.path))
        return self.delegate.check_attempt(effect)

    def finish_attempt(self, effect):
        self.calls.append(("finish_attempt", self.path))
        return self.delegate.finish_attempt(effect)

    def inspect_attempt(self, operation_id, **keywords):
        self.calls.append(("inspect_attempt", self.path))
        return self.delegate.inspect_attempt(operation_id, **keywords)

    def inspect_check(self, operation_id, **keywords):
        self.calls.append(("inspect_check", self.path))
        return self.delegate.inspect_check(operation_id, **keywords)

    def inspect_finish(self, operation_id, **keywords):
        self.calls.append(("inspect_finish", self.path))
        return self.delegate.inspect_finish(operation_id, **keywords)


class _ChannelFactory:
    def __init__(
        self,
        *,
        states: dict[str, FakeExternalState] | None = None,
        selected_capabilities: BackendCapabilities | None = None,
        send_state: TurnState = TurnState.DONE,
    ) -> None:
        self.states = {} if states is None else states
        self.capabilities = selected_capabilities or capabilities()
        self.send_state = send_state
        self.created_for: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def __call__(self, attempt: AttemptWorktree):
        self.created_for.append(attempt.path)
        state = self.states.setdefault(attempt.path, FakeExternalState())
        return _RecordingChannel(
            FakeBackendChannelPort(
                self.capabilities,
                state=state,
                send_state=self.send_state,
            ),
            attempt.path,
            self.calls,
        )

    def lifecycle_for(self, attempt: AttemptWorktree):
        state = self.states.setdefault(attempt.path, FakeExternalState())
        return _RecordingLifecycle(
            FakeTrellisLifecyclePort(state=state, worktree_path=attempt.path),
            attempt.path,
            self.calls,
        )


class AttemptBackendChannelRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identities = (attempt_identity(1), attempt_identity(2))
        self.attempts = tuple(
            attempt_worktree(self.root / f"attempt-{index}", identity)
            for index, identity in enumerate(self.identities, start=1)
        )
        self.plans = tuple(dispatch_plan(identity) for identity in self.identities)
        self.routes = tuple(
            AttemptChannelRoute(attempt, plan)
            for attempt, plan in zip(self.attempts, self.plans, strict=True)
        )

    def router(self, factory: _ChannelFactory, routes=None):
        return AttemptBackendChannelRouter(
            self.routes if routes is None else routes,
            expected_capabilities=capabilities(),
            channel_factory=factory,
            lifecycle_factory=getattr(factory, "lifecycle_for", None),
            clock=lambda: OBSERVED_AT,
        )

    def test_each_attempt_dispatches_from_its_own_git_worktree(self) -> None:
        factory = _ChannelFactory()
        router = self.router(factory)

        self.assertIsInstance(router, BackendChannelPort)
        self.assertEqual(capabilities(), router.probe())
        self.assertEqual([], factory.created_for)

        event_number = 1
        for identity, attempt, plan in zip(
            self.identities,
            self.attempts,
            self.plans,
            strict=True,
        ):
            reserved = router.reserve(
                prepared_effect(
                    identity,
                    plan.reserve,
                    EffectOperation.RESERVE_CHANNEL,
                    event_number=event_number,
                )
            )
            event_number += 1
            sent = router.send(
                prepared_effect(
                    identity,
                    plan.send,
                    EffectOperation.SEND_TASK_PACKET,
                    event_number=event_number,
                )
            )
            event_number += 1
            self.assertIs(reserved.status, EffectStatus.APPLIED)
            self.assertIs(sent.status, EffectStatus.APPLIED)
            self.assertEqual(plan.send.turn_id, sent.turn_id)
            self.assertEqual(attempt.path, factory.created_for[-1])

        self.assertEqual(
            [attempt.path for attempt in self.attempts],
            factory.created_for,
        )
        for attempt in self.attempts:
            self.assertIn(("reserve", attempt.path), factory.calls)
            self.assertIn(("send", attempt.path), factory.calls)
        self.assertEqual(
            self.plans[0].send.turn_id,
            router.inspect_turn(self.plans[0].send.operation_id).turn_id,
        )

    def test_lifecycle_operations_bind_the_frozen_attempt_route_and_recover(self) -> None:
        identity = self.identities[0]
        attempt = self.attempts[0]
        plan = lifecycle_dispatch_plan(identity)
        route = AttemptChannelRoute(attempt, plan)
        states: dict[str, FakeExternalState] = {}
        factory = _ChannelFactory(states=states)
        router = self.router(factory, (route,))
        self.assertIsInstance(router, TrellisLifecyclePort)
        trellis_task_id = f"TRELLIS-{identity.task_id}"

        prepare = PrepareAttempt(
            plan.reserve.attempt_id,
            identity.run_id,
            "TRELLIS-PARENT-001",
            trellis_task_id,
            identity.task_id,
            identity.attempt,
            identity.correlation_id,
            HASH_A,
            HASH_B,
            attempt.base_commit_sha,
        )
        prepare_effect = prepared_effect(
            identity, prepare, EffectOperation.PREPARE_ATTEMPT, event_number=1
        )
        prepared_attempt = router.prepare_attempt(prepare_effect)
        self.assertIs(prepared_attempt.status, EffectStatus.APPLIED)
        self.assertEqual(plan.reserve.attempt_id, prepared_attempt.attempt_id)

        check = CheckAttempt(
            "CHECK-001",
            plan.reserve.attempt_id,
            trellis_task_id,
            identity.task_id,
            plan.send.task_packet_digest,
            "3" * 40,
        )
        check_effect = prepared_effect(
            identity, check, EffectOperation.CHECK_ATTEMPT, event_number=2
        )
        checked = router.check_attempt(check_effect)
        self.assertIs(checked.status, EffectStatus.APPLIED)

        finish = FinishAttempt(
            "FINISH-001",
            plan.reserve.attempt_id,
            trellis_task_id,
            identity.task_id,
            "3" * 40,
            HASH_C,
        )
        finish_effect = prepared_effect(
            identity, finish, EffectOperation.FINISH_ATTEMPT, event_number=3
        )
        finished = router.finish_attempt(finish_effect)
        self.assertIs(finished.status, EffectStatus.APPLIED)

        recovered_route = AttemptChannelRoute(
            attempt,
            plan,
            tuple(
                AttemptOperationRoute.from_pending(
                    PendingExternalEffect(effect.request.event)
                )
                for effect in (prepare_effect, check_effect, finish_effect)
            ),
        )
        restarted_factory = _ChannelFactory(states=states)
        restarted = self.router(restarted_factory, (recovered_route,))
        self.assertIs(
            restarted.inspect_attempt(prepare.operation_id).status,
            EffectStatus.APPLIED,
        )
        self.assertIs(
            restarted.inspect_check(check.operation_id).status,
            EffectStatus.APPLIED,
        )
        self.assertIs(
            restarted.inspect_finish(finish.operation_id).status,
            EffectStatus.APPLIED,
        )

        mismatched_hash = replace(check, expected_head_commit="4" * 40)
        rejected = restarted.check_attempt(
            prepared_effect(
                identity,
                mismatched_hash,
                EffectOperation.CHECK_ATTEMPT,
                event_number=4,
            )
        )
        self.assertIs(rejected.status, EffectStatus.UNKNOWN)
        self.assertNotEqual("4" * 40, rejected.head_commit)

    def test_lifecycle_route_rejects_wrong_task_packet_and_prepare_identity(self) -> None:
        identity = self.identities[0]
        attempt = self.attempts[0]
        plan = lifecycle_dispatch_plan(identity)
        router = self.router(_ChannelFactory(), (AttemptChannelRoute(attempt, plan),))
        trellis_task_id = f"TRELLIS-{identity.task_id}"

        wrong_prepare = PrepareAttempt(
            "PREPARE-WRONG",
            identity.run_id,
            "TRELLIS-PARENT-001",
            trellis_task_id,
            identity.task_id,
            identity.attempt,
            identity.correlation_id,
            HASH_A,
            HASH_B,
            attempt.base_commit_sha,
        )
        wrong_packet = CheckAttempt(
            "CHECK-WRONG",
            plan.reserve.attempt_id,
            trellis_task_id,
            identity.task_id,
            HASH_C,
            "3" * 40,
        )

        self.assertIs(
            router.prepare_attempt(
                prepared_effect(
                    identity,
                    wrong_prepare,
                    EffectOperation.PREPARE_ATTEMPT,
                    event_number=1,
                )
            ).status,
            EffectStatus.UNKNOWN,
        )
        self.assertIs(
            router.check_attempt(
                prepared_effect(
                    identity,
                    wrong_packet,
                    EffectOperation.CHECK_ATTEMPT,
                    event_number=2,
                )
            ).status,
            EffectStatus.UNKNOWN,
        )

    def test_lifecycle_inspection_rejects_wrong_path_and_unsuccessful_results(
        self,
    ) -> None:
        identity = self.identities[0]
        attempt = self.attempts[0]
        plan = lifecycle_dispatch_plan(identity)
        route = AttemptChannelRoute(attempt, plan)
        router = self.router(_ChannelFactory(), (route,))
        trellis_task_id = f"TRELLIS-{identity.task_id}"

        wrong_path = AttemptObservation(
            operation_id=plan.reserve.attempt_id,
            status=EffectStatus.APPLIED,
            observed_at=OBSERVED_AT,
            lifecycle_state=TrellisLifecycleState.PREPARED,
            effect_digest=HASH_A,
            attempt_id=plan.reserve.attempt_id,
            trellis_task_id=trellis_task_id,
            worktree_id="WORKTREE-001",
            worktree_path=str(Path(attempt.path).parent / "wrong-attempt"),
            base_commit=attempt.base_commit_sha,
        )
        failed_check = CheckObservation(
            "CHECK-001",
            EffectStatus.APPLIED,
            OBSERVED_AT,
            HASH_A,
            plan.reserve.attempt_id,
            False,
            "3" * 40,
            HASH_B,
        )
        unfinished = FinishObservation(
            "FINISH-001",
            EffectStatus.APPLIED,
            OBSERVED_AT,
            HASH_A,
            plan.reserve.attempt_id,
            False,
            "3" * 40,
            HASH_B,
        )

        self.assertFalse(
            router._valid_lifecycle_observation(
                route,
                EffectOperation.PREPARE_ATTEMPT,
                wrong_path.operation_id,
                wrong_path,
            )
        )
        self.assertFalse(
            router._valid_lifecycle_observation(
                route,
                EffectOperation.CHECK_ATTEMPT,
                failed_check.operation_id,
                failed_check,
            )
        )
        self.assertFalse(
            router._valid_lifecycle_observation(
                route,
                EffectOperation.FINISH_ATTEMPT,
                unfinished.operation_id,
                unfinished,
            )
        )

    def test_empty_router_is_inert_until_an_attempt_route_exists(self) -> None:
        factory = _ChannelFactory()
        router = self.router(factory, ())

        self.assertEqual(capabilities(), router.probe())
        self.assertIs(
            router.inspect_turn("SEND-NOT-ROUTED").status,
            EffectStatus.UNKNOWN,
        )
        self.assertIs(
            router.reserve(
                prepared_effect(
                    self.identities[0],
                    self.plans[0].reserve,
                    EffectOperation.RESERVE_CHANNEL,
                    event_number=1,
                )
            ).status,
            EffectStatus.UNKNOWN,
        )
        self.assertEqual([], factory.created_for)
        self.assertEqual([], factory.calls)

    def test_backend_factory_ignores_legacy_enable_bit_but_requires_sdk_root(
        self,
    ) -> None:
        cell = load_bundled_compatibility().platform(
            Provider.CODEX,
            Platform.LINUX,
        )
        self.assertFalse(cell.qualification.enabled_for_dispatch)
        factory = WishBuilderBackendAttemptChannelFactory(
            compatibility_cell=cell,
        )

        with self.assertRaisesRegex(
            BackendDispatchUnavailable,
            "codex/linux: no Wish Builder provider adapter is installed",
        ):
            factory(self.attempts[0])

    def test_backend_factory_never_falls_back_to_trellis_for_enabled_cell(
        self,
    ) -> None:
        decoded = decode_compatibility_bundle_primitive(_enabled_v2_primitive())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None
        cell = decoded.value.platform(Provider.CODEX, Platform.LINUX)
        self.assertTrue(cell.qualification.enabled_for_dispatch)
        factory = WishBuilderBackendAttemptChannelFactory(
            compatibility_cell=cell,
        )

        with self.assertRaisesRegex(
            BackendDispatchUnavailable,
            "codex/linux: no Wish Builder provider adapter is installed",
        ):
            factory(self.attempts[0])

    def test_cancel_route_rebuilds_deterministically_from_pending_journal_identity(
        self,
    ) -> None:
        states: dict[str, FakeExternalState] = {}
        first_factory = _ChannelFactory(states=states)
        first = self.router(first_factory, (self.routes[0],))
        identity = self.identities[0]
        plan = self.plans[0]
        first.reserve(
            prepared_effect(
                identity,
                plan.reserve,
                EffectOperation.RESERVE_CHANNEL,
                event_number=1,
            )
        )
        first.send(
            prepared_effect(
                identity,
                plan.send,
                EffectOperation.SEND_TASK_PACKET,
                event_number=2,
            )
        )
        cancel = CancelTurn(
            operation_id="CANCEL-001",
            attempt_id=plan.send.attempt_id,
            channel_id=plan.send.channel_id,
            turn_id=plan.send.turn_id,
            reason_code="attempt-timeout",
        )
        cancel_effect = prepared_effect(
            identity,
            cancel,
            EffectOperation.CANCEL_TURN,
            event_number=3,
        )
        cancelled = first.cancel(cancel_effect)
        self.assertIs(cancelled.status, EffectStatus.APPLIED)
        conflicting_cancel = replace(cancel, reason_code="different-reason")
        conflict = first.cancel(
            prepared_effect(
                identity,
                conflicting_cancel,
                EffectOperation.CANCEL_TURN,
                event_number=4,
            )
        )
        self.assertIs(conflict.status, EffectStatus.UNKNOWN)
        self.assertEqual(
            1,
            sum(name == "cancel" for name, _ in first_factory.calls),
        )

        pending = PendingExternalEffect(cancel_effect.request.event)
        recovered_operation = AttemptOperationRoute.from_pending(pending)
        self.assertEqual(
            recovered_operation,
            AttemptOperationRoute.from_pending(pending),
        )
        recovered_route = AttemptChannelRoute(
            self.attempts[0],
            plan,
            (recovered_operation,),
        )
        restarted_factory = _ChannelFactory(states=states)
        restarted = self.router(restarted_factory, (recovered_route,))

        inspected = restarted.inspect_turn(cancel.operation_id)

        self.assertIs(inspected.status, EffectStatus.APPLIED)
        self.assertEqual(cancel.operation_id, inspected.operation_id)
        self.assertEqual(plan.send.turn_id, inspected.turn_id)
        self.assertEqual([self.attempts[0].path], restarted_factory.created_for)

    def test_takeover_cancel_routes_current_epoch_to_the_old_attempt_state(self) -> None:
        factory = _ChannelFactory(send_state=TurnState.RUNNING)
        router = self.router(factory, (self.routes[0],))
        identity = self.identities[0]
        plan = self.plans[0]
        router.reserve(
            prepared_effect(
                identity,
                plan.reserve,
                EffectOperation.RESERVE_CHANNEL,
                event_number=1,
            )
        )
        router.send(
            prepared_effect(
                identity,
                plan.send,
                EffectOperation.SEND_TASK_PACKET,
                event_number=2,
            )
        )
        cancel = CancelTurn(
            operation_id="CANCEL-TAKEOVER-001",
            attempt_id=plan.send.attempt_id,
            channel_id=plan.send.channel_id,
            turn_id=plan.send.turn_id,
            reason_code="lease_lost_takeover",
        )

        observation = router.cancel(
            prepared_effect(
                replace(identity, coordinator_epoch=8),
                cancel,
                EffectOperation.CANCEL_TURN,
                event_number=3,
            )
        )

        self.assertIs(observation.status, EffectStatus.APPLIED)
        self.assertIs(observation.state, TurnState.CANCELLED)
        self.assertEqual(plan.send.turn_id, observation.turn_id)
        self.assertEqual([self.attempts[0].path], factory.created_for)

    def test_unknown_or_inconsistent_identity_never_reaches_a_channel(self) -> None:
        factory = _ChannelFactory()
        router = self.router(factory)

        unknown = router.inspect_turn("SEND-UNKNOWN")
        wrong_kind = router.inspect_turn(self.plans[0].reserve.operation_id)
        mismatched = router.reserve(
            prepared_effect(
                self.identities[1],
                self.plans[0].reserve,
                EffectOperation.RESERVE_CHANNEL,
                event_number=1,
            )
        )

        self.assertIs(unknown.status, EffectStatus.UNKNOWN)
        self.assertIs(unknown.state, TurnState.UNKNOWN)
        self.assertIs(wrong_kind.status, EffectStatus.UNKNOWN)
        self.assertIs(mismatched.status, EffectStatus.UNKNOWN)
        self.assertEqual([], factory.created_for)
        self.assertEqual([], factory.calls)

        foreign_operation = AttemptOperationRoute(
            replace(self.identities[1], correlation_id="CANCEL-FOREIGN"),
            EffectOperation.CANCEL_TURN,
            HASH_A,
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            AttemptChannelRoute(
                self.attempts[0],
                self.plans[0],
                (foreign_operation,),
            )

    def test_replaced_worktree_and_capability_drift_fail_closed(self) -> None:
        route = self.routes[0]
        original = Path(route.attempt.path)
        displaced = original.with_name(original.name + "-displaced")
        original.rename(displaced)
        original.mkdir()
        factory = _ChannelFactory()
        router = self.router(factory, (route,))

        replaced = router.inspect_reservation(route.plan.reserve.operation_id)

        self.assertIs(replaced.status, EffectStatus.UNKNOWN)
        self.assertEqual([], factory.created_for)

        original.rmdir()
        displaced.rename(original)
        git_dir = Path(route.attempt.git_dir.canonical_path)
        displaced_git_dir = git_dir.with_name(git_dir.name + "-displaced")
        git_dir.rename(displaced_git_dir)
        git_dir.mkdir()
        git_drift_factory = _ChannelFactory()
        git_drift = self.router(git_drift_factory, (route,))
        git_rejected = git_drift.inspect_reservation(
            route.plan.reserve.operation_id
        )
        self.assertIs(git_rejected.status, EffectStatus.UNKNOWN)
        self.assertEqual([], git_drift_factory.created_for)
        git_dir.rmdir()
        displaced_git_dir.rename(git_dir)

        wrong_capabilities = replace(capabilities(), policy_digest=HASH_A)
        drifting_factory = _ChannelFactory(
            selected_capabilities=wrong_capabilities,
        )
        drifting = self.router(drifting_factory, (route,))
        rejected = drifting.reserve(
            prepared_effect(
                self.identities[0],
                route.plan.reserve,
                EffectOperation.RESERVE_CHANNEL,
                event_number=2,
            )
        )

        self.assertIs(rejected.status, EffectStatus.UNKNOWN)
        self.assertEqual([route.attempt.path], drifting_factory.created_for)
        self.assertNotIn(("reserve", route.attempt.path), drifting_factory.calls)

    def test_duplicate_attempt_or_operation_routes_are_rejected(self) -> None:
        factory = _ChannelFactory()
        with self.assertRaisesRegex(ValueError, "identity is duplicated"):
            self.router(factory, (self.routes[0], self.routes[0]))

        colliding_plan = replace(
            self.plans[1],
            reserve=replace(
                self.plans[1].reserve,
                operation_id=self.plans[0].reserve.operation_id,
            ),
        )
        with self.assertRaisesRegex(ValueError, "operation route"):
            self.router(
                factory,
                (
                    self.routes[0],
                    AttemptChannelRoute(self.attempts[1], colliding_plan),
                ),
            )

    def test_route_contracts_reject_corrupt_recovery_metadata(self) -> None:
        identity = self.identities[0]
        attempt = self.attempts[0]
        plan = self.plans[0]

        with self.assertRaisesRegex(ValueError, "complete correlated attempt"):
            AttemptOperationRoute(
                replace(identity, correlation_id=None),
                EffectOperation.CANCEL_TURN,
                HASH_A,
            )
        with self.assertRaisesRegex(TypeError, "PendingExternalEffect"):
            AttemptOperationRoute.from_pending(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "AttemptWorktree"):
            AttemptChannelRoute(object(), plan)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "BackendDispatchPlan"):
            AttemptChannelRoute(attempt, object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "recovery_operations"):
            AttemptChannelRoute(attempt, plan, [])  # type: ignore[arg-type]

        duplicate = AttemptOperationRoute(
            replace(identity, correlation_id="CANCEL-DUPLICATE"),
            EffectOperation.CANCEL_TURN,
            HASH_A,
        )
        with self.assertRaisesRegex(ValueError, "duplicates"):
            AttemptChannelRoute(attempt, plan, (duplicate, duplicate))

        foreign_dispatch = "DISPATCH-FOREIGN"
        mismatched_plan = replace(
            plan,
            reserve=replace(plan.reserve, dispatch_id=foreign_dispatch),
            send=replace(plan.send, dispatch_id=foreign_dispatch),
        )
        with self.assertRaisesRegex(ValueError, "Git attempt identity"):
            AttemptChannelRoute(attempt, mismatched_plan)

        invalid_recoveries = (
            (
                AttemptOperationRoute(
                    replace(identity, correlation_id="PREPARE-FOREIGN"),
                    EffectOperation.PREPARE_ATTEMPT,
                    HASH_A,
                ),
                "planned operation",
            ),
            (
                AttemptOperationRoute(
                    replace(identity, correlation_id=plan.reserve.operation_id),
                    EffectOperation.RESERVE_CHANNEL,
                    HASH_A,
                ),
                "payload does not match",
            ),
            (
                AttemptOperationRoute(
                    replace(identity, correlation_id=plan.reserve.operation_id),
                    EffectOperation.CANCEL_TURN,
                    HASH_A,
                ),
                "cancel operation collides",
            ),
            (
                AttemptOperationRoute(
                    replace(identity, correlation_id=plan.send.operation_id),
                    EffectOperation.CHECK_ATTEMPT,
                    HASH_A,
                ),
                "lifecycle operation collides",
            ),
        )
        for recovery, message in invalid_recoveries:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    AttemptChannelRoute(attempt, plan, (recovery,))

    def test_router_rejects_duplicate_paths_ids_and_oversized_packets(self) -> None:
        same_path_attempt = replace(
            self.attempts[1],
            path=self.attempts[0].path,
            worktree_root=self.attempts[0].worktree_root,
        )
        with self.assertRaisesRegex(ValueError, "worktree path is duplicated"):
            self.router(
                _ChannelFactory(),
                (
                    self.routes[0],
                    AttemptChannelRoute(same_path_attempt, self.plans[1]),
                ),
            )

        duplicated_attempt_id = self.plans[0].reserve.attempt_id
        colliding_plan = replace(
            self.plans[1],
            reserve=replace(
                self.plans[1].reserve,
                attempt_id=duplicated_attempt_id,
            ),
            send=replace(
                self.plans[1].send,
                attempt_id=duplicated_attempt_id,
            ),
        )
        with self.assertRaisesRegex(ValueError, "attempt_id is duplicated"):
            self.router(
                _ChannelFactory(),
                (
                    self.routes[0],
                    AttemptChannelRoute(self.attempts[1], colliding_plan),
                ),
            )

        too_small = replace(capabilities(), max_task_packet_bytes=1)
        with self.assertRaisesRegex(ValueError, "admitted capabilities"):
            AttemptBackendChannelRouter(
                (self.routes[0],),
                expected_capabilities=too_small,
                channel_factory=_ChannelFactory(),
                clock=lambda: OBSERVED_AT,
            )

    def test_lifecycle_identity_parser_rejects_malformed_task_packets(self) -> None:
        identity = self.identities[0]
        attempt = self.attempts[0]
        plan = lifecycle_dispatch_plan(identity)
        valid_packet = {
            "execution": {
                "dispatch_id": identity.correlation_id,
                "identity": identity.to_primitive(),
                "manifest_digest": HASH_A,
                "run_id": identity.run_id,
            },
            "kind": "wish_builder_task_packet",
            "task": {
                "id": identity.task_id,
                "trellis_task_id": f"TRELLIS-{identity.task_id}",
            },
            "trellis": {
                "graph_digest": HASH_B,
                "parent_task_id": "TRELLIS-PARENT-001",
            },
        }
        malformed_packets: tuple[str | dict[str, object], ...] = (
            {**valid_packet, "execution": []},
            {
                **valid_packet,
                "execution": {
                    **valid_packet["execution"],  # type: ignore[arg-type]
                    "identity": [],
                },
            },
            {
                **valid_packet,
                "execution": {
                    "dispatch_id": identity.correlation_id,
                },
            },
            {
                **valid_packet,
                "execution": {
                    **valid_packet["execution"],  # type: ignore[arg-type]
                    "manifest_digest": "not-a-digest",
                },
            },
            "{not-json",
        )

        for packet_value in malformed_packets:
            with self.subTest(packet=packet_value):
                packet = (
                    packet_value
                    if type(packet_value) is str
                    else canonical_json_bytes(packet_value).decode("utf-8")
                )
                malformed_plan = replace(
                    plan,
                    send=replace(
                        plan.send,
                        task_packet=packet,
                        task_packet_digest=(
                            "sha256:"
                            + hashlib.sha256(packet.encode("utf-8")).hexdigest()
                        ),
                    ),
                )
                router = self.router(
                    _ChannelFactory(),
                    (AttemptChannelRoute(attempt, malformed_plan),),
                )
                self.assertIs(
                    router.inspect_attempt(plan.reserve.attempt_id).status,
                    EffectStatus.UNKNOWN,
                )

    def test_router_defensive_helpers_fail_closed(self) -> None:
        route = self.routes[0]
        router = self.router(_ChannelFactory(), (route,))

        with self.assertRaisesRegex(LookupError, "route unknown"):
            router._operation_hash("NOT-BOUND")
        with self.assertRaisesRegex(TypeError, "operation_id"):
            router.inspect_turn(1)  # type: ignore[arg-type]
        reserve_effect = prepared_effect(
            self.identities[0],
            route.plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
            event_number=1,
        )
        with self.assertRaisesRegex(TypeError, "effect command"):
            router._require_effect(reserve_effect, SendTaskPacket)
        with mock.patch(
            "wish_builder.processes.production_routing.capture_filesystem_identity",
            side_effect=OSError("identity unavailable"),
        ):
            self.assertFalse(router._worktree_matches(route.attempt))

        unknown = ChannelObservation(
            route.plan.reserve.operation_id,
            EffectStatus.UNKNOWN,
            OBSERVED_AT,
            evidence=("not observed",),
        )
        wrong_id = ChannelObservation(
            "RESERVE-OTHER",
            EffectStatus.UNKNOWN,
            OBSERVED_AT,
            evidence=("not observed",),
        )
        self.assertFalse(
            router._valid_observation(
                route,
                EffectOperation.RESERVE_CHANNEL,
                route.plan.reserve.operation_id,
                object(),
            )
        )
        self.assertFalse(
            router._valid_observation(
                route,
                EffectOperation.RESERVE_CHANNEL,
                route.plan.reserve.operation_id,
                wrong_id,
            )
        )
        self.assertFalse(
            router._valid_observation(
                route,
                EffectOperation.RESERVE_CHANNEL,
                wrong_id.operation_id,
                wrong_id,
            )
        )
        self.assertTrue(
            router._valid_observation(
                route,
                EffectOperation.RESERVE_CHANNEL,
                route.plan.reserve.operation_id,
                unknown,
            )
        )
        self.assertFalse(
            router._valid_lifecycle_observation(
                route,
                EffectOperation.PREPARE_ATTEMPT,
                route.plan.reserve.attempt_id,
                object(),
            )
        )

        attempt_observation = AttemptObservation(
            operation_id=route.plan.reserve.attempt_id,
            status=EffectStatus.APPLIED,
            observed_at=OBSERVED_AT,
            lifecycle_state=TrellisLifecycleState.PREPARED,
            effect_digest=HASH_A,
            attempt_id=route.plan.reserve.attempt_id,
            trellis_task_id=f"TRELLIS-{self.identities[0].task_id}",
            worktree_id="WORKTREE-001",
            worktree_path=route.attempt.path,
            base_commit=route.attempt.base_commit_sha,
        )
        self.assertFalse(
            router._valid_lifecycle_observation(
                route,
                EffectOperation.PREPARE_ATTEMPT,
                route.plan.reserve.attempt_id,
                attempt_observation,
            )
        )

    def test_adapter_instances_cannot_cross_attempt_boundaries(self) -> None:
        invalid_factory_router = self.router(lambda _attempt: object())
        invalid = invalid_factory_router.reserve(
            prepared_effect(
                self.identities[0],
                self.plans[0].reserve,
                EffectOperation.RESERVE_CHANNEL,
                event_number=1,
            )
        )
        self.assertIs(invalid.status, EffectStatus.UNKNOWN)

        calls: list[tuple[str, str]] = []
        shared_channel = _RecordingChannel(
            FakeBackendChannelPort(capabilities()),
            self.attempts[0].path,
            calls,
        )
        shared_router = self.router(lambda _attempt: shared_channel)
        first = shared_router.reserve(
            prepared_effect(
                self.identities[0],
                self.plans[0].reserve,
                EffectOperation.RESERVE_CHANNEL,
                event_number=2,
            )
        )
        second = shared_router.reserve(
            prepared_effect(
                self.identities[1],
                self.plans[1].reserve,
                EffectOperation.RESERVE_CHANNEL,
                event_number=3,
            )
        )
        self.assertIs(first.status, EffectStatus.APPLIED)
        self.assertIs(second.status, EffectStatus.UNKNOWN)

        identity = self.identities[0]
        lifecycle_plan = lifecycle_dispatch_plan(identity)
        lifecycle_route = AttemptChannelRoute(self.attempts[0], lifecycle_plan)
        channel_only_router = self.router(
            lambda _attempt: FakeBackendChannelPort(capabilities()),
            (lifecycle_route,),
        )
        prepare = PrepareAttempt(
            lifecycle_plan.reserve.attempt_id,
            identity.run_id,
            "TRELLIS-PARENT-001",
            f"TRELLIS-{identity.task_id}",
            identity.task_id,
            identity.attempt,
            identity.correlation_id,
            HASH_A,
            HASH_B,
            self.attempts[0].base_commit_sha,
        )
        rejected = channel_only_router.prepare_attempt(
            prepared_effect(
                identity,
                prepare,
                EffectOperation.PREPARE_ATTEMPT,
                event_number=4,
            )
        )
        self.assertIs(rejected.status, EffectStatus.UNKNOWN)

    def test_unrouted_lifecycle_and_send_operations_are_inert(self) -> None:
        router = self.router(_ChannelFactory(), ())
        plan = self.plans[0]
        identity = self.identities[0]

        sent = router.send(
            prepared_effect(
                identity,
                plan.send,
                EffectOperation.SEND_TASK_PACKET,
                event_number=1,
            )
        )
        self.assertIs(sent.status, EffectStatus.UNKNOWN)
        self.assertIs(
            router.inspect_attempt("PREPARE-UNKNOWN").status,
            EffectStatus.UNKNOWN,
        )
        self.assertIs(
            router.inspect_check("CHECK-UNKNOWN").status,
            EffectStatus.UNKNOWN,
        )
        self.assertIs(
            router.inspect_finish("FINISH-UNKNOWN").status,
            EffectStatus.UNKNOWN,
        )


if __name__ == "__main__":
    unittest.main()
