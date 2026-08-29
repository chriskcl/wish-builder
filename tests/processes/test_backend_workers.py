from __future__ import annotations

from wish_builder.adapters import FilesystemExternalEvidenceStore

import dataclasses
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tests.processes.test_coordinator import CoordinatorHarness
from wish_builder.contracts import WorkerProvider
from wish_builder.contracts.runtime import (
    ActorType,
    EffectOperation,
    EffectStatus,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseDraftPayload,
    RuntimeReasonCode,
)
from wish_builder.kernel.state import apply_journal_event
from wish_builder.processes import (
    PreparedForegroundAttempt,
    BackendWorkerTurnMonitor,
)
from wish_builder.processes.foreground import WorkerLeaseRenewalResult
from wish_builder.services import BackendDispatchPlan
from wish_builder.services.ports import (
    ReserveChannel,
    SendTaskPacket,
    TurnObservation,
    TurnState,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
OBSERVED_AT = "2026-08-19T08:00:00Z"


def identity(number: int = 1) -> ExecutionIdentity:
    suffix = f"{number:03d}"
    return ExecutionIdentity(
        "RUN-BACKEND-WORKERS",
        1,
        f"TASK-{suffix}",
        1,
        f"DISPATCH-{suffix}",
    )


def plan_for(
    value: ExecutionIdentity,
    *,
    operation_suffix: str | None = None,
    dispatch_id: str | None = None,
) -> BackendDispatchPlan:
    assert value.task_id is not None
    suffix = value.task_id.removeprefix("TASK-")
    operation_suffix = suffix if operation_suffix is None else operation_suffix
    dispatch_id = value.correlation_id if dispatch_id is None else dispatch_id
    assert dispatch_id is not None
    packet = '{"task_id":"%s"}' % value.task_id
    return BackendDispatchPlan(
        ReserveChannel(
            operation_id=f"BACKEND-RESERVE-{operation_suffix}",
            attempt_id=f"ATTEMPT-{suffix}",
            dispatch_id=dispatch_id,
            channel_id=f"CHANNEL-{suffix}",
            provider=WorkerProvider.CODEX,
            capability_digest=HASH_A,
            launch_profile_digest=HASH_B,
            policy_digest=HASH_C,
        ),
        SendTaskPacket(
            operation_id=f"BACKEND-SEND-{operation_suffix}",
            attempt_id=f"ATTEMPT-{suffix}",
            dispatch_id=dispatch_id,
            channel_id=f"CHANNEL-{suffix}",
            message_id=f"MESSAGE-{suffix}",
            turn_id=f"TURN-{suffix}",
            task_packet=packet,
            task_packet_digest=(
                "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()
            ),
        ),
    )


def turn(
    plan: BackendDispatchPlan,
    state: TurnState,
) -> TurnObservation:
    send = plan.send
    if state is TurnState.UNKNOWN:
        return TurnObservation(
            send.operation_id,
            EffectStatus.UNKNOWN,
            OBSERVED_AT,
            TurnState.UNKNOWN,
            evidence=("inspect outcome was ambiguous",),
        )
    if state is TurnState.ABSENT:
        return TurnObservation(
            send.operation_id,
            EffectStatus.ABSENT,
            OBSERVED_AT,
            TurnState.ABSENT,
        )
    return TurnObservation(
        send.operation_id,
        EffectStatus.APPLIED,
        OBSERVED_AT,
        state,
        effect_digest=HASH_A,
        attempt_id=send.attempt_id,
        channel_id=send.channel_id,
        message_id=send.message_id,
        turn_id=send.turn_id,
        result_digest=HASH_B if state is TurnState.DONE else None,
    )


class _ScriptedChannel:
    def __init__(self, scripts: dict[str, list[object]]) -> None:
        self.scripts = {key: list(values) for key, values in scripts.items()}
        self.inspect_calls: list[str] = []
        self.effect_calls: list[str] = []

    def inspect_turn(self, operation_id: str) -> object:
        self.inspect_calls.append(operation_id)
        values = self.scripts[operation_id]
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    def probe(self):
        self.effect_calls.append("probe")
        raise AssertionError("monitor must not probe providers")

    def reserve(self, effect):
        self.effect_calls.append("reserve")
        raise AssertionError("monitor must not reserve channels")

    def send(self, effect):
        self.effect_calls.append("send")
        raise AssertionError("monitor must not send task packets")

    def inspect_reservation(self, operation_id):
        self.effect_calls.append("inspect_reservation")
        raise AssertionError("monitor must only inspect turns")

    def cancel(self, effect):
        self.effect_calls.append("cancel")
        raise AssertionError("monitor must not cancel turns")


class _RecordingStore:
    def __init__(self, root: Path) -> None:
        self.backend = FilesystemExternalEvidenceStore(root)
        self.calls: list[tuple[TurnObservation, ExecutionIdentity, EffectOperation]] = []

    def put(self, observation, *, identity, operation):
        self.calls.append((observation, identity, operation))
        return self.backend.put(
            observation,
            identity=identity,
            operation=operation,
        )


class _BadStore:
    def __init__(self, *, raises: bool) -> None:
        self.raises = raises

    def put(self, observation, *, identity, operation):
        if self.raises:
            raise OSError("scripted evidence failure")
        return object()


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def renew_cursor(cursor) -> WorkerLeaseRenewalResult:
    lease = cursor.lease_state.lease
    assert lease is not None
    committed_at = datetime.fromisoformat(
        lease.committed_at.replace("Z", "+00:00")
    ) + timedelta(seconds=100)
    payload = LeaseDraftPayload(
        lease.lease_id,
        lease.coordinator_id,
        lease.owner,
        lease.scheduler_mode,
        lease.fencing_token,
        lease.manifest_digest,
        lease.lease_ttl_seconds,
        lease.lease_clock_skew_seconds,
    ).materialize(committed_at, terminal=False)
    event = JournalEvent.create(
        sequence=cursor.head.sequence + 1,
        event_id=f"EVENT-WORKER-LEASE-RENEWED-{cursor.head.sequence + 1:08d}",
        event_type=JournalEventType.LEASE_RENEWED,
        identity=ExecutionIdentity(cursor.snapshot.run_id, lease.fencing_token),
        actor_type=ActorType.COORDINATOR,
        actor_id=lease.coordinator_id,
        recorded_at=payload.committed_at,
        previous_event_hash=cursor.head.event_hash,
        payload=payload,
    )
    applied = apply_journal_event(cursor.snapshot, event)
    assert applied.accepted
    renewed = type(cursor)(
        applied.snapshot,
        cursor.graph_index.advance(cursor.snapshot, applied.snapshot),
        cursor.lease_state.advance(event),
        cursor.dispatch_recoveries,
    )
    return WorkerLeaseRenewalResult(True, renewed, event)


class BackendWorkerTurnMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def monitor(
        self,
        channel: _ScriptedChannel,
        plans: dict[ExecutionIdentity, BackendDispatchPlan],
        *,
        store=None,
        clock: _Clock | None = None,
        timeout_seconds: float = 10,
        poll_interval_seconds: float = 1,
        lease_renewal=None,
    ) -> BackendWorkerTurnMonitor:
        clock = _Clock() if clock is None else clock
        store = _RecordingStore(self.root / "evidence") if store is None else store
        return BackendWorkerTurnMonitor(
            channel,
            store,
            plans.__getitem__,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            lease_renewal=lease_renewal,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

    def test_done_turn_becomes_success_with_durable_terminal_evidence(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        observation = turn(plan, TurnState.DONE)
        channel = _ScriptedChannel({plan.send.operation_id: [observation]})
        store = _RecordingStore(self.root / "evidence")

        result = self.monitor(
            channel,
            {worker_identity: plan},
            store=store,
        ).run((PreparedForegroundAttempt(worker_identity, object()),))

        self.assertTrue(result.outcomes_known)
        self.assertEqual(1, len(result.proposals))
        proposal = result.proposals[0]
        self.assertEqual(worker_identity, proposal.identity)
        self.assertEqual("backend-turn:TURN-001", proposal.actor_id)
        self.assertTrue(proposal.succeeded)
        self.assertIsNone(proposal.reason_code)
        self.assertEqual(1, len(proposal.evidence))
        self.assertEqual(observation, store.calls[0][0])
        self.assertEqual(
            plan.send.operation_id,
            store.calls[0][1].correlation_id,
        )
        self.assertIs(store.calls[0][2], EffectOperation.SEND_TASK_PACKET)
        self.assertEqual(
            observation.canonical_json_bytes(),
            store.backend.read(proposal.evidence[0].digest),
        )
        self.assertEqual([], channel.effect_calls)

    def test_failed_and_cancelled_turns_use_closed_runtime_reasons(self) -> None:
        identities = (identity(1), identity(2))
        plans = {item: plan_for(item) for item in identities}
        channel = _ScriptedChannel(
            {
                plans[identities[0]].send.operation_id: [
                    turn(plans[identities[0]], TurnState.FAILED)
                ],
                plans[identities[1]].send.operation_id: [
                    turn(plans[identities[1]], TurnState.CANCELLED)
                ],
            }
        )

        result = self.monitor(channel, plans).run(
            tuple(PreparedForegroundAttempt(item, object()) for item in identities)
        )

        self.assertTrue(result.outcomes_known)
        self.assertEqual(identities, tuple(item.identity for item in result.proposals))
        self.assertEqual((False, False), tuple(item.succeeded for item in result.proposals))
        self.assertEqual(
            (
                RuntimeReasonCode.CHECK_FAILED,
                RuntimeReasonCode.CANCELLED_BY_USER,
            ),
            tuple(item.reason_code for item in result.proposals),
        )
        self.assertTrue(all(len(item.evidence) == 1 for item in result.proposals))

    def test_queued_and_running_turn_are_polled_with_injected_time(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        channel = _ScriptedChannel(
            {
                plan.send.operation_id: [
                    turn(plan, TurnState.QUEUED),
                    turn(plan, TurnState.RUNNING),
                    turn(plan, TurnState.DONE),
                ]
            }
        )
        clock = _Clock()

        result = self.monitor(
            channel,
            {worker_identity: plan},
            clock=clock,
        ).run((PreparedForegroundAttempt(worker_identity, object()),))

        self.assertTrue(result.outcomes_known)
        self.assertEqual([plan.send.operation_id] * 3, channel.inspect_calls)
        self.assertEqual([1.0, 1.0], clock.sleeps)
        self.assertEqual([], channel.effect_calls)

    def test_waiting_renews_from_cursor_ttl_and_returns_the_new_journal_head(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        channel = _ScriptedChannel(
            {
                plan.send.operation_id: [
                    turn(plan, TurnState.RUNNING),
                    turn(plan, TurnState.RUNNING),
                    turn(plan, TurnState.DONE),
                ]
            }
        )
        clock = _Clock()
        cursor = CoordinatorHarness(self.root / "coordinator").coordinator.cursor
        renewal_times = []

        def renew(current):
            renewal_times.append(clock.value)
            return renew_cursor(current)

        result = self.monitor(
            channel,
            {worker_identity: plan},
            clock=clock,
            timeout_seconds=250,
            poll_interval_seconds=60,
            lease_renewal=renew,
        ).run((PreparedForegroundAttempt(worker_identity, object()),), cursor)

        self.assertTrue(result.outcomes_known)
        self.assertEqual([0.0, 100.0], renewal_times)
        self.assertEqual([60.0, 40.0], clock.sleeps)
        self.assertEqual(2, len(result.events))
        self.assertTrue(
            all(
                event.event_type is JournalEventType.LEASE_RENEWED
                for event in result.events
            )
        )
        self.assertIsNotNone(result.cursor)
        assert result.cursor is not None
        self.assertEqual(result.events[-1].event_hash, result.cursor.head.event_hash)

    def test_renewal_failure_or_exception_fails_closed_when_waiting_begins(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        cursor = CoordinatorHarness(self.root / "coordinator").coordinator.cursor

        def raises(current):
            del current
            raise OSError("lease journal unavailable")

        callbacks = (
            lambda current: WorkerLeaseRenewalResult(False),
            raises,
        )
        for number, callback in enumerate(callbacks):
            with self.subTest(callback=number):
                channel = _ScriptedChannel(
                    {plan.send.operation_id: [turn(plan, TurnState.RUNNING)]}
                )
                clock = _Clock()
                store = _RecordingStore(self.root / f"renewal-failure-{number}")
                result = self.monitor(
                    channel,
                    {worker_identity: plan},
                    store=store,
                    clock=clock,
                    timeout_seconds=250,
                    poll_interval_seconds=60,
                    lease_renewal=callback,
                ).run((PreparedForegroundAttempt(worker_identity, object()),), cursor)

                self.assertFalse(result.outcomes_known)
                self.assertEqual(0.0, clock.value)
                self.assertEqual(cursor, result.cursor)
                self.assertEqual((), result.events)
                self.assertEqual([], store.calls)

    def test_later_renewal_failure_preserves_the_last_durable_cursor(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        channel = _ScriptedChannel(
            {plan.send.operation_id: [turn(plan, TurnState.RUNNING)]}
        )
        clock = _Clock()
        cursor = CoordinatorHarness(self.root / "coordinator").coordinator.cursor
        calls = 0

        def renew(current):
            nonlocal calls
            calls += 1
            return renew_cursor(current) if calls == 1 else WorkerLeaseRenewalResult(False)

        result = self.monitor(
            channel,
            {worker_identity: plan},
            clock=clock,
            timeout_seconds=250,
            poll_interval_seconds=60,
            lease_renewal=renew,
        ).run((PreparedForegroundAttempt(worker_identity, object()),), cursor)

        self.assertFalse(result.outcomes_known)
        self.assertEqual(2, calls)
        self.assertEqual(1, len(result.events))
        self.assertIsNotNone(result.cursor)
        assert result.cursor is not None
        self.assertEqual(result.events[0].event_hash, result.cursor.head.event_hash)

    def test_wait_past_ttl_third_without_renewal_callback_fails_closed(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        channel = _ScriptedChannel(
            {plan.send.operation_id: [turn(plan, TurnState.RUNNING)]}
        )
        clock = _Clock()
        cursor = CoordinatorHarness(self.root / "coordinator").coordinator.cursor

        result = self.monitor(
            channel,
            {worker_identity: plan},
            clock=clock,
            timeout_seconds=250,
            poll_interval_seconds=60,
        ).run((PreparedForegroundAttempt(worker_identity, object()),), cursor)

        self.assertFalse(result.outcomes_known)
        self.assertEqual(100.0, clock.value)
        self.assertEqual(cursor, result.cursor)

    def test_unknown_absent_or_wrong_observation_type_fails_closed(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        cases = (
            turn(plan, TurnState.UNKNOWN),
            turn(plan, TurnState.ABSENT),
            object(),
        )
        for number, observation in enumerate(cases):
            with self.subTest(observation=type(observation).__name__):
                channel = _ScriptedChannel(
                    {plan.send.operation_id: [observation]}
                )
                store = _RecordingStore(self.root / f"evidence-{number}")
                result = self.monitor(
                    channel,
                    {worker_identity: plan},
                    store=store,
                ).run((PreparedForegroundAttempt(worker_identity, object()),))
                self.assertFalse(result.outcomes_known)
                self.assertEqual((), result.proposals)
                self.assertEqual([], store.calls)

    def test_every_observed_turn_identity_must_match_the_dispatch_plan(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        valid = turn(plan, TurnState.DONE)
        mismatches = (
            dataclasses.replace(valid, attempt_id="ATTEMPT-WRONG"),
            dataclasses.replace(valid, channel_id="CHANNEL-WRONG"),
            dataclasses.replace(valid, message_id="MESSAGE-WRONG"),
            dataclasses.replace(valid, turn_id="TURN-WRONG"),
            dataclasses.replace(valid, operation_id="BACKEND-SEND-WRONG"),
        )
        for number, observation in enumerate(mismatches):
            with self.subTest(observation=observation):
                channel = _ScriptedChannel(
                    {plan.send.operation_id: [observation]}
                )
                store = _RecordingStore(self.root / f"mismatch-{number}")
                result = self.monitor(
                    channel,
                    {worker_identity: plan},
                    store=store,
                ).run((PreparedForegroundAttempt(worker_identity, object()),))
                self.assertFalse(result.outcomes_known)
                self.assertEqual([], store.calls)

    def test_timeout_fails_closed_without_retry_or_unjournaled_cancel(self) -> None:
        worker_identity = identity()
        plan = plan_for(worker_identity)
        channel = _ScriptedChannel(
            {plan.send.operation_id: [turn(plan, TurnState.RUNNING)]}
        )
        clock = _Clock()
        store = _RecordingStore(self.root / "timeout-evidence")

        result = self.monitor(
            channel,
            {worker_identity: plan},
            store=store,
            clock=clock,
            timeout_seconds=2,
        ).run((PreparedForegroundAttempt(worker_identity, object()),))

        self.assertFalse(result.outcomes_known)
        self.assertEqual((), result.proposals)
        self.assertEqual([plan.send.operation_id] * 2, channel.inspect_calls)
        self.assertEqual([1.0, 1.0], clock.sleeps)
        self.assertEqual([], channel.effect_calls)
        self.assertEqual([], store.calls)

    def test_invalid_plan_or_non_durable_evidence_fails_closed(self) -> None:
        worker_identity = identity()
        valid_plan = plan_for(worker_identity)
        channel = _ScriptedChannel(
            {valid_plan.send.operation_id: [turn(valid_plan, TurnState.DONE)]}
        )
        invalid_plan = plan_for(worker_identity, dispatch_id="DISPATCH-WRONG")
        invalid = self.monitor(channel, {worker_identity: invalid_plan}).run(
            (PreparedForegroundAttempt(worker_identity, object()),)
        )
        self.assertFalse(invalid.outcomes_known)
        self.assertEqual([], channel.inspect_calls)

        for raises in (False, True):
            with self.subTest(evidence_store_raises=raises):
                channel = _ScriptedChannel(
                    {
                        valid_plan.send.operation_id: [
                            turn(valid_plan, TurnState.DONE)
                        ]
                    }
                )
                result = self.monitor(
                    channel,
                    {worker_identity: valid_plan},
                    store=_BadStore(raises=raises),
                ).run((PreparedForegroundAttempt(worker_identity, object()),))
                self.assertFalse(result.outcomes_known)

    def test_duplicate_send_operation_ids_and_bad_inputs_are_rejected(self) -> None:
        first, second = identity(1), identity(2)
        plans = {
            first: plan_for(first, operation_suffix="SHARED"),
            second: plan_for(second, operation_suffix="SHARED"),
        }
        channel = _ScriptedChannel(
            {
                plans[first].send.operation_id: [
                    turn(plans[first], TurnState.DONE)
                ]
            }
        )
        monitor = self.monitor(channel, plans)

        result = monitor.run(
            (
                PreparedForegroundAttempt(first, object()),
                PreparedForegroundAttempt(second, object()),
            )
        )
        self.assertFalse(result.outcomes_known)
        self.assertEqual([], channel.inspect_calls)
        with self.assertRaises(TypeError):
            monitor.run([])  # type: ignore[arg-type]

    def test_empty_batch_is_known_without_touching_dependencies(self) -> None:
        channel = _ScriptedChannel({})
        monitor = self.monitor(channel, {})

        result = monitor.run(())

        self.assertTrue(result.outcomes_known)
        self.assertEqual((), result.proposals)
        self.assertEqual([], channel.inspect_calls)


if __name__ == "__main__":
    unittest.main()
