from __future__ import annotations

from wish_builder.adapters.fakes import FakeBackendChannelPort

import dataclasses
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests.ports.trellis_helpers import (
    BASE_COMMIT,
    HASH_A,
    HASH_B,
    HEAD_COMMIT,
    packet_digest,
    prepared,
)
from wish_builder.adapters.trellis import (
    FakeTrellisLifecyclePort,
    FakeExternalState,
)
from wish_builder.contracts.runtime import EffectStatus
from wish_builder.contracts import WorkerProvider
from wish_builder.services.ports import (
    CancelTurn,
    BackendCapabilities,
    CheckAttempt,
    FinishAttempt,
    PrepareAttempt,
    ReserveChannel,
    SendTaskPacket,
    TrellisLifecycleState,
    TurnState,
)


def prepare_command(operation_id: str = "OP-PREPARE-001") -> PrepareAttempt:
    return PrepareAttempt(
        operation_id=operation_id,
        run_id="WISH-001",
        parent_task_id="parent-001",
        trellis_task_id="child-alpha",
        task_id="TASK-001",
        attempt=1,
        dispatch_id="DISPATCH-001",
        manifest_digest=HASH_A,
        trellis_graph_digest=HASH_B,
        expected_base_commit=BASE_COMMIT,
    )


def capabilities(*, max_packet_bytes: int = 4096) -> BackendCapabilities:
    return BackendCapabilities(
        provider=WorkerProvider.CODEX,
        platform="windows-x86_64",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest="sha256:" + "c" * 64,
        max_task_packet_bytes=max_packet_bytes,
    )


def reserve_command(
    operation_id: str = "OP-RESERVE-001",
    *,
    attempt_id: str = "attempt-001",
    dispatch_id: str = "DISPATCH-001",
    channel_id: str = "channel-001",
) -> ReserveChannel:
    value = capabilities()
    return ReserveChannel(
        operation_id=operation_id,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
        channel_id=channel_id,
        provider=value.provider,
        capability_digest=value.capability_digest,
        launch_profile_digest=value.launch_profile_digest,
        policy_digest=value.policy_digest,
    )


def send_command(
    operation_id: str = "OP-SEND-001",
    *,
    attempt_id: str = "attempt-001",
    dispatch_id: str = "DISPATCH-001",
    channel_id: str = "channel-001",
    turn_id: str = "turn-001",
    packet: str = '{"task":"TASK-001"}',
) -> SendTaskPacket:
    return SendTaskPacket(
        operation_id=operation_id,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
        channel_id=channel_id,
        message_id="message-001",
        turn_id=turn_id,
        task_packet=packet,
        task_packet_digest=packet_digest(packet),
    )


class FakeTrellisLifecycleTests(unittest.TestCase):
    def test_absent_prepare_duplicate_and_restart_share_one_effect(self) -> None:
        state = FakeExternalState()
        port = FakeTrellisLifecyclePort(state=state)
        absent = port.inspect_attempt("OP-PREPARE-001")
        self.assertEqual(EffectStatus.ABSENT, absent.status)
        self.assertEqual(TrellisLifecycleState.ABSENT, absent.lifecycle_state)

        effect = prepared(prepare_command())
        first = port.prepare_attempt(effect)
        second = port.prepare_attempt(effect)
        restarted = FakeTrellisLifecyclePort(state=state)
        inspected = restarted.inspect_attempt(effect.operation_id)

        self.assertEqual(EffectStatus.APPLIED, first.status)
        self.assertEqual(TrellisLifecycleState.PREPARED, first.lifecycle_state)
        self.assertEqual(first, second)
        self.assertEqual(first, inspected)
        self.assertEqual(1, restarted.effect_count)

    def test_duplicate_operation_id_with_changed_command_becomes_unknown(self) -> None:
        port = FakeTrellisLifecyclePort()
        original = prepare_command()
        first = port.prepare_attempt(prepared(original, event_number=1))
        changed = dataclasses.replace(original, trellis_task_id="child-beta")
        collision = port.prepare_attempt(prepared(changed, event_number=2))
        replay_after_collision = port.prepare_attempt(
            prepared(original, event_number=3)
        )

        self.assertEqual(EffectStatus.APPLIED, first.status)
        self.assertEqual(EffectStatus.UNKNOWN, collision.status)
        self.assertEqual(EffectStatus.UNKNOWN, replay_after_collision.status)
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.inspect_attempt(original.operation_id).status,
        )
        self.assertEqual(1, port.effect_count)

    def test_prepare_check_finish_chain_is_typed_and_idempotent(self) -> None:
        port = FakeTrellisLifecyclePort()
        prepared_attempt = port.prepare_attempt(prepared(prepare_command(), event_number=1))
        assert prepared_attempt.attempt_id is not None
        check = CheckAttempt(
            operation_id="OP-CHECK-001",
            attempt_id=prepared_attempt.attempt_id,
            trellis_task_id="child-alpha",
            task_id="TASK-001",
            task_packet_digest=HASH_A,
            expected_head_commit=HEAD_COMMIT,
        )
        checked = port.check_attempt(prepared(check, event_number=2))
        finish = FinishAttempt(
            operation_id="OP-FINISH-001",
            attempt_id=prepared_attempt.attempt_id,
            trellis_task_id="child-alpha",
            task_id="TASK-001",
            delivered_commit=HEAD_COMMIT,
            delivery_evidence_digest=HASH_B,
        )
        finish_effect = prepared(finish, event_number=3)
        finished = port.finish_attempt(finish_effect)

        self.assertTrue(checked.passed)
        self.assertTrue(finished.finished)
        self.assertEqual(finished, port.finish_attempt(finish_effect))
        self.assertEqual(
            checked,
            port.inspect_check(check.operation_id),
        )
        self.assertEqual(
            finished,
            port.inspect_finish(finish.operation_id),
        )
        self.assertIs(
            port.inspect_attempt(check.operation_id).status,
            EffectStatus.UNKNOWN,
        )
        self.assertEqual(3, port.effect_count)

    def test_check_without_prepare_and_scripted_unknown_never_apply(self) -> None:
        check = CheckAttempt(
            operation_id="OP-CHECK-ABSENT",
            attempt_id="attempt-missing",
            trellis_task_id="child-alpha",
            task_id="TASK-001",
            task_packet_digest=HASH_A,
            expected_head_commit=HEAD_COMMIT,
        )
        port = FakeTrellisLifecyclePort(
            unknown_operation_ids={"OP-PREPARE-UNKNOWN"}
        )
        missing = port.check_attempt(prepared(check, event_number=1))
        scripted = port.prepare_attempt(
            prepared(prepare_command("OP-PREPARE-UNKNOWN"), event_number=2)
        )

        self.assertEqual(EffectStatus.UNKNOWN, missing.status)
        self.assertEqual(EffectStatus.UNKNOWN, scripted.status)
        self.assertEqual(0, port.effect_count)

    def test_concurrent_duplicate_prepare_creates_one_exact_effect(self) -> None:
        port = FakeTrellisLifecyclePort()
        effect = prepared(prepare_command())
        with ThreadPoolExecutor(max_workers=8) as executor:
            observations = tuple(
                executor.map(lambda _: port.prepare_attempt(effect), range(32))
            )

        self.assertTrue(all(item == observations[0] for item in observations))
        self.assertEqual(EffectStatus.APPLIED, observations[0].status)
        self.assertEqual(1, port.effect_count)


class FakeBackendChannelTests(unittest.TestCase):
    def test_probe_absent_reserve_send_and_duplicate_are_deterministic(self) -> None:
        port = FakeBackendChannelPort(capabilities())
        self.assertEqual(capabilities(), port.probe())
        self.assertEqual(
            EffectStatus.ABSENT, port.inspect_turn("OP-SEND-001").status
        )

        reserve_effect = prepared(reserve_command(), event_number=1)
        first_reserve = port.reserve(reserve_effect)
        second_reserve = port.reserve(reserve_effect)
        send_effect = prepared(send_command(), event_number=2)
        first_turn = port.send(send_effect)
        second_turn = port.send(send_effect)

        self.assertEqual(EffectStatus.APPLIED, first_reserve.status)
        self.assertEqual(first_reserve, second_reserve)
        self.assertEqual(TurnState.DONE, first_turn.state)
        self.assertIsNotNone(first_turn.result_digest)
        self.assertEqual(first_turn, second_turn)
        self.assertEqual(first_turn, port.inspect_turn("OP-SEND-001"))
        self.assertEqual(first_reserve, port.inspect_reservation("OP-RESERVE-001"))
        self.assertEqual(2, port.effect_count)

    def test_operation_and_turn_id_collisions_are_unknown_without_second_effect(self) -> None:
        port = FakeBackendChannelPort(capabilities())
        port.reserve(prepared(reserve_command(), event_number=1))
        original = send_command()
        first = port.send(prepared(original, event_number=2))
        changed = dataclasses.replace(
            original,
            task_packet='{"task":"TASK-002"}',
            task_packet_digest=packet_digest('{"task":"TASK-002"}'),
        )
        collision = port.send(prepared(changed, event_number=3))
        second_turn_id_collision = port.send(
            prepared(
                send_command(
                    "OP-SEND-002",
                    turn_id=original.turn_id,
                ),
                event_number=4,
            )
        )

        self.assertEqual(EffectStatus.APPLIED, first.status)
        self.assertEqual(EffectStatus.UNKNOWN, collision.status)
        self.assertEqual(EffectStatus.UNKNOWN, second_turn_id_collision.status)
        self.assertEqual(
            EffectStatus.UNKNOWN, port.inspect_turn(original.operation_id).status
        )
        self.assertEqual(2, port.effect_count)

    def test_dispatch_identity_collisions_are_unknown_without_second_effect(self) -> None:
        reserve_port = FakeBackendChannelPort(capabilities())
        reserve = reserve_command()
        first_reserve = reserve_port.reserve(prepared(reserve, event_number=1))
        reserve_collision = reserve_port.reserve(
            prepared(
                dataclasses.replace(reserve, dispatch_id="DISPATCH-002"),
                event_number=2,
            )
        )

        self.assertEqual(EffectStatus.APPLIED, first_reserve.status)
        self.assertEqual(EffectStatus.UNKNOWN, reserve_collision.status)
        self.assertEqual(
            (f"operation_id_collision:{reserve.operation_id}",),
            reserve_collision.evidence,
        )
        self.assertEqual(
            EffectStatus.UNKNOWN,
            reserve_port.inspect_reservation(reserve.operation_id).status,
        )
        self.assertEqual(1, reserve_port.effect_count)

        send_port = FakeBackendChannelPort(capabilities())
        send_port.reserve(prepared(reserve_command(), event_number=3))
        sent = send_command()
        first_send = send_port.send(prepared(sent, event_number=4))
        send_collision = send_port.send(
            prepared(
                dataclasses.replace(sent, dispatch_id="DISPATCH-002"),
                event_number=5,
            )
        )

        self.assertEqual(EffectStatus.APPLIED, first_send.status)
        self.assertEqual(EffectStatus.UNKNOWN, send_collision.status)
        self.assertEqual(
            (f"operation_id_collision:{sent.operation_id}",),
            send_collision.evidence,
        )
        self.assertEqual(
            EffectStatus.UNKNOWN, send_port.inspect_turn(sent.operation_id).status
        )
        self.assertEqual(2, send_port.effect_count)

    def test_send_requires_reservation_and_matching_capabilities(self) -> None:
        port = FakeBackendChannelPort(capabilities())
        unreserved = port.send(prepared(send_command(), event_number=1))
        mismatched = dataclasses.replace(
            reserve_command("OP-RESERVE-MISMATCH"),
            capability_digest="sha256:" + "f" * 64,
        )
        reservation = port.reserve(prepared(mismatched, event_number=2))

        self.assertEqual(EffectStatus.UNKNOWN, unreserved.status)
        self.assertEqual(EffectStatus.UNKNOWN, reservation.status)
        self.assertEqual(0, port.effect_count)

    def test_running_turn_can_be_cancelled_once_and_inspected_after_restart(self) -> None:
        state = FakeExternalState()
        port = FakeBackendChannelPort(
            capabilities(), state=state, send_state=TurnState.RUNNING
        )
        port.reserve(prepared(reserve_command(), event_number=1))
        running = port.send(prepared(send_command(), event_number=2))
        cancel = CancelTurn(
            operation_id="OP-CANCEL-001",
            attempt_id="attempt-001",
            channel_id="channel-001",
            turn_id="turn-001",
            reason_code="operator_requested",
        )
        cancel_effect = prepared(cancel, event_number=3)
        cancelled = port.cancel(cancel_effect)
        restarted = FakeBackendChannelPort(capabilities(), state=state)

        self.assertEqual(TurnState.RUNNING, running.state)
        self.assertEqual(TurnState.CANCELLED, cancelled.state)
        self.assertEqual(cancelled, restarted.inspect_turn(cancel.operation_id))
        self.assertEqual(cancelled, restarted.cancel(cancel_effect))
        self.assertEqual(3, restarted.effect_count)

    def test_scripted_unknown_is_stable_across_duplicate_calls(self) -> None:
        port = FakeBackendChannelPort(
            capabilities(), unknown_operation_ids={"OP-RESERVE-UNKNOWN"}
        )
        effect = prepared(
            reserve_command("OP-RESERVE-UNKNOWN"),
            event_number=1,
        )
        first = port.reserve(effect)
        second = port.reserve(effect)
        self.assertEqual(EffectStatus.UNKNOWN, first.status)
        self.assertEqual(first, second)
        self.assertEqual(0, port.effect_count)

    def test_operation_ids_are_unique_across_lifecycle_and_channel_surfaces(self) -> None:
        state = FakeExternalState()
        lifecycle = FakeTrellisLifecyclePort(state=state)
        channel = FakeBackendChannelPort(capabilities(), state=state)
        lifecycle.prepare_attempt(
            prepared(prepare_command("OP-SHARED-001"), event_number=1)
        )
        collision = channel.reserve(
            prepared(reserve_command("OP-SHARED-001"), event_number=2)
        )

        self.assertEqual(EffectStatus.UNKNOWN, collision.status)
        self.assertEqual(
            EffectStatus.UNKNOWN,
            lifecycle.inspect_attempt("OP-SHARED-001").status,
        )
        self.assertEqual(1, lifecycle.effect_count)
        self.assertEqual(0, channel.effect_count)


if __name__ == "__main__":
    unittest.main()
