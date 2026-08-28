from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from wish_builder.adapters.providers import (
    JsonlRpcBackendChannel,
    JsonlRpcBackendConfig,
    JsonlRpcLaunch,
    JsonlRpcProtocol,
)
from wish_builder.contracts import WorkerProvider, canonical_sha256
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
from wish_builder.services.journal import AppendResult, AppendStatus, JournalHead
from wish_builder.services.ports import (
    BackendCapabilities,
    CancelTurn,
    PreparedEffect,
    ReserveChannel,
    SendTaskPacket,
    TurnState,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
GENESIS_HASH = "sha256:" + "0" * 64
OBSERVED_AT = "2026-08-21T03:00:00Z"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "jsonl_rpc_backend.py"


def capabilities(provider: WorkerProvider) -> BackendCapabilities:
    return BackendCapabilities(
        provider=provider,
        platform="windows" if os.name == "nt" else "linux",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest=HASH_C,
        max_task_packet_bytes=4096,
    )


def launch(provider: WorkerProvider) -> JsonlRpcLaunch:
    return JsonlRpcLaunch(
        provider=provider,
        protocol=(
            JsonlRpcProtocol.PI
            if provider is WorkerProvider.PI
            else JsonlRpcProtocol.OH_MY_PI_V2
        ),
        command_prefix=(str(Path(sys.executable).resolve()), str(FIXTURE.resolve())),
        sdk_name=(
            "@earendil-works/pi-coding-agent"
            if provider is WorkerProvider.PI
            else "@oh-my-pi/pi-coding-agent"
        ),
        sdk_version="0.84.2" if provider is WorkerProvider.PI else "17.4.0",
    )


def prepared_effect(
    identity: ExecutionIdentity,
    command: ReserveChannel | SendTaskPacket | CancelTurn,
    operation: EffectOperation,
    sequence: int,
) -> PreparedEffect:
    object_type = {
        EffectOperation.RESERVE_CHANNEL: EffectObjectType.CHANNEL,
        EffectOperation.SEND_TASK_PACKET: EffectObjectType.TASK_PACKET,
        EffectOperation.CANCEL_TURN: EffectObjectType.TURN,
    }[operation]
    request_identity = replace(identity, correlation_id=command.operation_id)
    event = JournalEvent.create(
        sequence=1,
        event_id=f"EVENT-PROVIDER-{sequence:03d}",
        event_type=JournalEventType.EFFECT_REQUESTED,
        identity=request_identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-provider-test",
        recorded_at=OBSERVED_AT,
        previous_event_hash=GENESIS_HASH,
        payload=EffectRequestPayload(
            operation,
            AdapterKind.BACKEND,
            object_type,
            "sha256:" + canonical_sha256(request_identity.to_primitive()),
            command.canonical_sha256(),
            0,
            request_identity.coordinator_epoch,
        ),
    )
    return PreparedEffect.from_append_result(
        AppendResult(
            AppendStatus.COMMITTED,
            JournalHead(event.sequence, event.event_hash),
            event,
        ),
        command,
    )


class JsonlRpcProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.worktree = self.root / "attempt"
        self.worktree.mkdir()
        self.identity = ExecutionIdentity("RUN-PROVIDER", 3, "TASK-001", 1, "DISPATCH-001")

    def channel(
        self,
        provider: WorkerProvider,
        name: str = "state",
        *,
        delay: str = "0.05",
        prompt_response_after_start: bool = False,
        abort_response_after_terminal: bool = False,
        barrier_directory: Path | None = None,
    ) -> JsonlRpcBackendChannel:
        config = JsonlRpcBackendConfig(
            capabilities(provider),
            launch(provider),
            self.worktree,
            self.root / name,
            environment=(
                ("FAKE_RPC_PROVIDER", "pi" if provider is WorkerProvider.PI else "omp"),
                ("FAKE_RPC_DELAY", delay),
                (
                    "FAKE_RPC_PROMPT_RESPONSE_AFTER_START",
                    "1" if prompt_response_after_start else "0",
                ),
                (
                    "FAKE_RPC_ABORT_RESPONSE_AFTER_TERMINAL",
                    "1" if abort_response_after_terminal else "0",
                ),
                (
                    "FAKE_RPC_BARRIER_DIRECTORY",
                    str(barrier_directory) if barrier_directory is not None else "",
                ),
            ),
            handshake_timeout_seconds=5,
            response_timeout_seconds=5,
        )
        channel = JsonlRpcBackendChannel(config, clock=lambda: OBSERVED_AT)
        self.addCleanup(channel.close)
        return channel

    def commands(
        self,
        provider: WorkerProvider,
        suffix: str = "001",
    ) -> tuple[ReserveChannel, SendTaskPacket, CancelTurn]:
        packet = json.dumps({"task": suffix}, separators=(",", ":"))
        reserve = ReserveChannel(
            operation_id=f"RESERVE-{suffix}",
            attempt_id=f"ATTEMPT-{suffix}",
            dispatch_id=f"DISPATCH-{suffix}",
            channel_id=f"CHANNEL-{suffix}",
            provider=provider,
            capability_digest=HASH_A,
            launch_profile_digest=HASH_B,
            policy_digest=HASH_C,
        )
        send = SendTaskPacket(
            operation_id=f"SEND-{suffix}",
            attempt_id=reserve.attempt_id,
            dispatch_id=reserve.dispatch_id,
            channel_id=reserve.channel_id,
            message_id=f"MESSAGE-{suffix}",
            turn_id=f"TURN-{suffix}",
            task_packet=packet,
            task_packet_digest="sha256:" + hashlib.sha256(packet.encode()).hexdigest(),
        )
        cancel = CancelTurn(
            operation_id=f"CANCEL-{suffix}",
            attempt_id=reserve.attempt_id,
            channel_id=reserve.channel_id,
            turn_id=send.turn_id,
            reason_code="operator_cancelled",
        )
        return reserve, send, cancel

    def dispatch(
        self,
        channel: JsonlRpcBackendChannel,
        provider: WorkerProvider,
        suffix: str = "001",
    ) -> tuple[ReserveChannel, SendTaskPacket, CancelTurn]:
        reserve, send, cancel = self.commands(provider, suffix)
        reserved = channel.reserve(
            prepared_effect(self.identity, reserve, EffectOperation.RESERVE_CHANNEL, 1)
        )
        self.assertEqual(EffectStatus.APPLIED, reserved.status)
        sent = channel.send(
            prepared_effect(self.identity, send, EffectOperation.SEND_TASK_PACKET, 2)
        )
        self.assertEqual(EffectStatus.APPLIED, sent.status)
        self.assertIn(sent.state, {TurnState.RUNNING, TurnState.DONE})
        return reserve, send, cancel

    def wait_terminal(
        self,
        channel: JsonlRpcBackendChannel,
        operation_id: str,
    ) -> TurnState:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = channel.inspect_turn(operation_id).state
            if state in {TurnState.DONE, TurnState.FAILED, TurnState.CANCELLED}:
                return state
            time.sleep(0.01)
        self.fail("provider fixture turn did not become terminal")

    def test_pi_and_oh_my_pi_complete_fresh_persistent_sessions(self) -> None:
        for index, provider in enumerate(
            (WorkerProvider.PI, WorkerProvider.OH_MY_PI), start=1
        ):
            with self.subTest(provider=provider):
                channel = self.channel(
                    provider,
                    f"state-{index}",
                    prompt_response_after_start=True,
                )
                reserve, send, _ = self.dispatch(channel, provider, f"00{index}")
                self.assertEqual(TurnState.DONE, self.wait_terminal(channel, send.operation_id))
                self.assertEqual(
                    channel.inspect_reservation(reserve.operation_id),
                    channel.inspect_reservation(reserve.operation_id),
                )
                session_files = tuple((self.root / f"state-{index}" / "provider-session").glob("*.jsonl"))
                self.assertEqual(1, len(session_files))

    def test_active_cancel_updates_send_and_cancel_observations(self) -> None:
        channel = self.channel(
            WorkerProvider.PI,
            delay="2",
            abort_response_after_terminal=True,
        )
        _, send, cancel = self.dispatch(channel, WorkerProvider.PI)
        cancelled = channel.cancel(
            prepared_effect(self.identity, cancel, EffectOperation.CANCEL_TURN, 3)
        )
        self.assertEqual(EffectStatus.APPLIED, cancelled.status)
        self.assertEqual(TurnState.CANCELLED, cancelled.state)
        self.assertEqual(TurnState.CANCELLED, channel.inspect_turn(send.operation_id).state)

    def test_cancel_after_terminal_turn_preserves_terminal_state(self) -> None:
        channel = self.channel(WorkerProvider.PI)
        _, send, cancel = self.dispatch(channel, WorkerProvider.PI)
        self.assertEqual(TurnState.DONE, self.wait_terminal(channel, send.operation_id))

        cancelled = channel.cancel(
            prepared_effect(self.identity, cancel, EffectOperation.CANCEL_TURN, 3)
        )

        self.assertEqual(EffectStatus.APPLIED, cancelled.status)
        self.assertEqual(TurnState.DONE, cancelled.state)
        self.assertEqual(TurnState.DONE, channel.inspect_turn(send.operation_id).state)

    def test_restart_reconciles_terminal_session_without_resending(self) -> None:
        first = self.channel(WorkerProvider.OH_MY_PI)
        _, send, _ = self.dispatch(first, WorkerProvider.OH_MY_PI)
        self.assertEqual(TurnState.DONE, self.wait_terminal(first, send.operation_id))
        first.close()

        state = json.loads(first.state_path.read_text(encoding="utf-8"))
        operation = state["operations"][send.operation_id]
        operation["observation"]["state"] = "running"
        operation["observation"]["result_digest"] = None
        first.state_path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="",
        )

        restarted = self.channel(WorkerProvider.OH_MY_PI)
        observed = restarted.inspect_turn(send.operation_id)
        self.assertEqual(EffectStatus.APPLIED, observed.status)
        self.assertEqual(TurnState.DONE, observed.state)
        self.assertIn("provider_session_reconciled", observed.evidence)
        self.assertIsNone(restarted.process_id)

    def test_interrupted_accepted_prompt_reconciles_as_failed(self) -> None:
        channel = self.channel(WorkerProvider.PI, delay="5")
        _, send, _ = self.dispatch(channel, WorkerProvider.PI)
        channel.close()
        restarted = self.channel(WorkerProvider.PI)
        observed = restarted.inspect_turn(send.operation_id)
        self.assertEqual(EffectStatus.APPLIED, observed.status)
        self.assertEqual(TurnState.FAILED, observed.state)
        self.assertIn("provider_session_interrupted_after_acceptance", observed.evidence)

    def test_sibling_channels_run_concurrently_and_cleanup_processes(self) -> None:
        barrier_directory = self.root / "sibling-barrier"
        channels = (
            self.channel(
                WorkerProvider.PI,
                "sibling-a",
                barrier_directory=barrier_directory,
            ),
            self.channel(
                WorkerProvider.PI,
                "sibling-b",
                barrier_directory=barrier_directory,
            ),
        )
        errors: list[BaseException] = []

        def run(index: int) -> None:
            try:
                _, send, _ = self.dispatch(channels[index], WorkerProvider.PI, f"10{index}")
                self.assertEqual(TurnState.DONE, self.wait_terminal(channels[index], send.operation_id))
            except BaseException as exc:  # pragma: no cover - relayed to test thread
                errors.append(exc)

        workers = tuple(threading.Thread(target=run, args=(index,)) for index in range(2))
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertFalse(errors, errors)
        self.assertEqual(2, len(tuple(barrier_directory.glob("*.ready"))))
        pids = tuple(channel.process_id for channel in channels)
        self.assertTrue(all(type(pid) is int for pid in pids))
        for channel in channels:
            self.assertEqual(("provider_process_tree",), channel.cleanup())
            self.assertIsNone(channel.process_id)

    def test_operation_id_collision_and_missing_operations_fail_closed(self) -> None:
        channel = self.channel(WorkerProvider.PI)
        reserve, _, _ = self.commands(WorkerProvider.PI)
        applied = channel.reserve(
            prepared_effect(self.identity, reserve, EffectOperation.RESERVE_CHANNEL, 1)
        )
        self.assertEqual(EffectStatus.APPLIED, applied.status)
        collision = replace(reserve, channel_id="CHANNEL-DIFFERENT")
        unknown = channel.reserve(
            prepared_effect(self.identity, collision, EffectOperation.RESERVE_CHANNEL, 2)
        )
        self.assertEqual(EffectStatus.UNKNOWN, unknown.status)
        self.assertEqual(
            EffectStatus.ABSENT,
            channel.inspect_turn("SEND-MISSING").status,
        )


if __name__ == "__main__":
    unittest.main()
