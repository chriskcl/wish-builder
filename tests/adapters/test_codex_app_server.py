from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from wish_builder.adapters.providers import (
    CodexAppServerChannel,
    CodexAppServerClient,
    CodexAppServerConfig,
    CodexAppServerError,
    CodexAppServerLaunch,
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
OBSERVED_AT = "2026-08-21T04:00:00Z"

FAKE_SERVER = r'''
import json
import os
import subprocess
import sys
import time
from pathlib import Path

scenario = os.environ.get("CODEX_FAKE_SCENARIO", "normal")
state_path = Path(os.environ["CODEX_FAKE_STATE"])
log_path = Path(os.environ["CODEX_FAKE_LOG"])
thread_id = os.environ.get("CODEX_FAKE_THREAD", "codex-thread-001")
child_path = os.environ.get("CODEX_FAKE_CHILD")

def emit(value, *, crlf=False):
    ending = "\r\n" if crlf else "\n"
    sys.stdout.buffer.write((json.dumps(value, separators=(",", ":")) + ending).encode())
    sys.stdout.buffer.flush()

def load_state():
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {"id": thread_id, "turns": []}

def save_state(value):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")

def log(value):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")

def result_text(valid=True):
    value = {
        "status": "completed",
        "summary": "implemented",
        "changed_files": ["src/example.py"],
        "checks": [{"name": "unit", "status": "passed", "details": "ok"}],
    }
    if not valid:
        value.pop("checks")
    return json.dumps(value, separators=(",", ":"))

def complete_turn(turn, status="completed", valid=True):
    if status == "completed":
        turn["items"].append({
            "id": "agent-final-" + turn["id"],
            "type": "agentMessage",
            "phase": "final_answer",
            "text": result_text(valid),
        })
    turn["status"] = status
    state = load_state()
    state["turns"] = [turn if item["id"] == turn["id"] else item for item in state["turns"]]
    save_state(state)

if child_path:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path(child_path).write_text(str(child.pid), encoding="ascii")

for raw in sys.stdin.buffer:
    try:
        message = json.loads(raw.decode("utf-8"))
    except Exception:
        sys.exit(31)
    log(message)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params", {})
    if method == "initialize":
        if scenario == "crlf":
            emit({"id": request_id, "result": {}}, crlf=True)
        elif scenario == "oversized":
            sys.stdout.buffer.write(b'{"id":1,"result":{"x":"' + b'x' * 1048576 + b'"}}\n')
            sys.stdout.buffer.flush()
        else:
            emit({"method": "account/rateLimits/updated", "params": {"ok": True}})
            emit({"id": request_id, "result": {}})
    elif method == "initialized":
        if scenario == "server_request":
            emit({"method": "item/commandExecution/requestApproval", "id": 900, "params": {}})
    elif method == "thread/start":
        state = load_state()
        state["id"] = thread_id
        save_state(state)
        emit({"id": request_id, "result": {"thread": {"id": thread_id, "turns": []}}})
    elif method == "turn/start":
        message_id = params["clientUserMessageId"]
        turn_id = "provider-turn-" + message_id
        user = {
            "id": "user-" + message_id,
            "type": "userMessage",
            "clientId": message_id,
            "content": params["input"],
        }
        turn = {"id": turn_id, "status": "inProgress", "items": [user]}
        state = load_state()
        state["turns"].append(turn)
        save_state(state)
        if scenario == "overlap":
            barrier = Path(os.environ["CODEX_FAKE_BARRIER"])
            barrier.mkdir(parents=True, exist_ok=True)
            (barrier / thread_id).write_text("ready", encoding="ascii")
            deadline = time.monotonic() + 10
            while len(list(barrier.iterdir())) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        emit({"method": "item/started", "params": {
            "threadId": thread_id, "turnId": turn_id,
            "item": {"id": "cmd-1", "type": "commandExecution", "status": "inProgress"},
        }})
        emit({"id": request_id, "result": {"turn": turn}})
        if scenario in {"crash_done", "crash_in_progress"}:
            if scenario == "crash_done":
                complete_turn(turn)
            sys.stdout.buffer.flush()
            os._exit(19)
        if scenario not in {"cancel", "cancel_ack_only"}:
            valid = scenario != "bad_result"
            complete_turn(turn, valid=valid)
            agent = turn["items"][-1]
            emit({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id, "item": agent,
            }})
            emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
    elif method == "turn/interrupt":
        state = load_state()
        turn = next(item for item in state["turns"] if item["id"] == params["turnId"])
        emit({"id": request_id, "result": {}})
        if scenario != "cancel_ack_only":
            complete_turn(turn, status="interrupted")
            emit({"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
    elif method == "thread/read":
        state = load_state()
        emit({"id": request_id, "result": {"thread": state}})
    else:
        emit({"id": request_id, "error": {"code": -32601, "message": method}})
'''


def capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        provider=WorkerProvider.CODEX,
        platform="windows" if os.name == "nt" else "linux",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest=HASH_C,
        max_task_packet_bytes=4096,
    )


def packet_digest(packet: str) -> str:
    return "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()


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
        event_id=f"EVENT-CODEX-{sequence:03d}",
        event_type=JournalEventType.EFFECT_REQUESTED,
        identity=request_identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-codex-test",
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


class CodexFixture:
    def __init__(self, root: Path, *, scenario: str = "normal", name: str = "a") -> None:
        self.root = root
        self.worktree = root / f"worktree-{name}"
        self.control = root / f"control-{name}"
        self.script = root / "fake-codex.py"
        self.provider_state = root / f"provider-{name}.json"
        self.log = root / f"wire-{name}.jsonl"
        self.worktree.mkdir(parents=True)
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.environment = (
            ("CODEX_FAKE_SCENARIO", scenario),
            ("CODEX_FAKE_STATE", str(self.provider_state)),
            ("CODEX_FAKE_LOG", str(self.log)),
            ("CODEX_FAKE_THREAD", f"codex-thread-{name}"),
        )

    def launch(self) -> CodexAppServerLaunch:
        return CodexAppServerLaunch(
            command_prefix=(str(Path(sys.executable).resolve()), str(self.script.resolve())),
            sdk_version="0.149.0",
            sdk_shasum="2e38d3859f52f288a86596d0c22366a10154437b",
            sdk_integrity="sha512-i4dryj2Y1j+00Mb5n+0n71EYnTK9/KDc2cdFo/dXD0d1oTog2bhUssKDEIOnKmnEf51P0Z/HJTWvTKw/UHyOvQ==",
        )

    def config(self, extra_environment: tuple[tuple[str, str], ...] = ()) -> CodexAppServerConfig:
        return CodexAppServerConfig(
            capabilities=capabilities(),
            launch=self.launch(),
            working_directory=self.worktree.resolve(),
            state_directory=self.control.resolve(),
            environment=(*self.environment, *extra_environment),
            response_timeout_seconds=5,
        )

    def client(self) -> CodexAppServerClient:
        return CodexAppServerClient(
            self.launch(),
            working_directory=self.worktree.resolve(),
            environment=dict(self.environment),
            response_timeout_seconds=5,
        )

    def wire(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


class CodexAppServerClientTests(unittest.TestCase):
    def test_exact_handshake_and_thread_start_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CodexFixture(Path(directory))
            client = fixture.client()
            try:
                client.connect()
                self.assertEqual("codex-thread-a", client.start_thread())
            finally:
                client.close()
            methods = [frame["method"] for frame in fixture.wire()]
            self.assertEqual(["initialize", "initialized", "thread/start"], methods)
            initialize = fixture.wire()[0]
            self.assertNotIn("jsonrpc", initialize)
            self.assertEqual("wish_builder", initialize["params"]["clientInfo"]["name"])

    def test_strict_lf_and_frame_limit_fail_closed(self) -> None:
        for scenario, code in (
            ("crlf", "codex_non_lf_frame"),
            ("oversized", "codex_physical_frame_too_large"),
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                client = CodexFixture(Path(directory), scenario=scenario).client()
                try:
                    with self.assertRaises(CodexAppServerError) as raised:
                        client.connect()
                    self.assertEqual(code, raised.exception.code)
                finally:
                    client.close()

    def test_unexpected_server_request_is_rejected_without_deadlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CodexFixture(Path(directory), scenario="server_request")
            client = fixture.client()
            try:
                client.connect()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and len(fixture.wire()) < 3:
                    time.sleep(0.01)
                with self.assertRaises(CodexAppServerError) as raised:
                    client.start_thread()
                self.assertEqual("codex_server_request_rejected", raised.exception.code)
            finally:
                client.close()


class CodexAppServerChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = ExecutionIdentity("WISH-001", 1, "TASK-001", 1, "DISPATCH-001")

    def commands(self) -> tuple[ReserveChannel, SendTaskPacket, CancelTurn]:
        reserve = ReserveChannel(
            operation_id="RESERVE-001",
            attempt_id="ATTEMPT-001",
            dispatch_id="DISPATCH-001",
            channel_id="CHANNEL-001",
            provider=WorkerProvider.CODEX,
            capability_digest=HASH_A,
            launch_profile_digest=HASH_B,
            policy_digest=HASH_C,
        )
        packet = '{"kind":"wish_builder_task_packet"}'
        send = SendTaskPacket(
            operation_id="SEND-001",
            attempt_id="ATTEMPT-001",
            dispatch_id="DISPATCH-001",
            channel_id="CHANNEL-001",
            message_id="MESSAGE-001",
            turn_id="LOGICAL-TURN-001",
            task_packet=packet,
            task_packet_digest=packet_digest(packet),
        )
        cancel = CancelTurn(
            operation_id="CANCEL-001",
            attempt_id="ATTEMPT-001",
            channel_id="CHANNEL-001",
            turn_id="LOGICAL-TURN-001",
            reason_code="operator_cancelled",
        )
        return reserve, send, cancel

    def reserve_and_send(
        self, fixture: CodexFixture
    ) -> tuple[CodexAppServerChannel, object, object]:
        channel = CodexAppServerChannel(fixture.config(), clock=lambda: OBSERVED_AT)
        reserve, send, _ = self.commands()
        reserved = channel.reserve(
            prepared_effect(self.identity, reserve, EffectOperation.RESERVE_CHANNEL, 1)
        )
        sent = channel.send(
            prepared_effect(self.identity, send, EffectOperation.SEND_TASK_PACKET, 2)
        )
        return channel, reserved, sent

    def test_interleaved_notifications_and_structured_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CodexFixture(Path(directory))
            channel, reserved, sent = self.reserve_and_send(fixture)
            try:
                self.assertEqual(EffectStatus.APPLIED, reserved.status)
                self.assertEqual("codex-thread-a", reserved.provider_session_id)
                deadline = time.monotonic() + 3
                while sent.state is TurnState.RUNNING and time.monotonic() < deadline:
                    sent = channel.inspect_turn("SEND-001")
                    time.sleep(0.01)
                self.assertEqual(TurnState.DONE, sent.state)
                self.assertIsNotNone(sent.result_digest)
                wire = fixture.wire()
                turn = next(frame for frame in wire if frame["method"] == "turn/start")
                self.assertEqual("MESSAGE-001", turn["params"]["clientUserMessageId"])
                self.assertFalse(turn["params"]["outputSchema"]["additionalProperties"])
                state = json.loads(
                    (fixture.control / "channel-state.json").read_text(encoding="utf-8")
                )
                self.assertGreater(
                    state["operations"]["SEND-001"]["provider_event_position"], 0
                )
            finally:
                channel.close()

    def test_structured_result_is_validated_locally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CodexFixture(Path(directory), scenario="bad_result")
            channel, _, sent = self.reserve_and_send(fixture)
            try:
                deadline = time.monotonic() + 3
                while sent.state is TurnState.RUNNING and time.monotonic() < deadline:
                    sent = channel.inspect_turn("SEND-001")
                    time.sleep(0.01)
                self.assertEqual(TurnState.FAILED, sent.state)
                self.assertIsNone(sent.result_digest)
                self.assertIn("codex_structured_result_schema_mismatch", sent.evidence)
            finally:
                channel.close()

    def test_cancel_waits_for_terminal_interrupted_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CodexFixture(Path(directory), scenario="cancel")
            channel, _, sent = self.reserve_and_send(fixture)
            try:
                self.assertEqual(TurnState.RUNNING, sent.state)
                _, _, cancel = self.commands()
                cancelled = channel.cancel(
                    prepared_effect(self.identity, cancel, EffectOperation.CANCEL_TURN, 3)
                )
                self.assertEqual(EffectStatus.APPLIED, cancelled.status)
                self.assertEqual(TurnState.CANCELLED, cancelled.state)
                methods = [frame["method"] for frame in fixture.wire()]
                self.assertIn("turn/interrupt", methods)
            finally:
                channel.close()

    def test_interrupt_ack_without_terminal_notification_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CodexFixture(Path(directory), scenario="cancel_ack_only")
            config = replace(fixture.config(), response_timeout_seconds=0.2)
            channel = CodexAppServerChannel(config, clock=lambda: OBSERVED_AT)
            reserve, send, cancel = self.commands()
            try:
                channel.reserve(
                    prepared_effect(
                        self.identity, reserve, EffectOperation.RESERVE_CHANNEL, 1
                    )
                )
                channel.send(
                    prepared_effect(
                        self.identity, send, EffectOperation.SEND_TASK_PACKET, 2
                    )
                )
                observation = channel.cancel(
                    prepared_effect(
                        self.identity, cancel, EffectOperation.CANCEL_TURN, 3
                    )
                )
                self.assertEqual(EffectStatus.UNKNOWN, observation.status)
                self.assertEqual(TurnState.UNKNOWN, observation.state)
                self.assertTrue(
                    any("codex_turn_terminal_timeout" in item for item in observation.evidence)
                )
            finally:
                channel.close()

    def test_crash_reconciles_with_thread_read_without_second_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CodexFixture(Path(directory), scenario="crash_done")
            channel, _, sent = self.reserve_and_send(fixture)
            try:
                deadline = time.monotonic() + 3
                while channel._client is not None and channel._client.is_alive and time.monotonic() < deadline:
                    time.sleep(0.01)
                reconciled = channel.inspect_turn("SEND-001")
                self.assertEqual(TurnState.DONE, reconciled.state)
                methods = [frame["method"] for frame in fixture.wire()]
                self.assertEqual(1, methods.count("turn/start"))
                self.assertEqual(1, methods.count("thread/read"))
                self.assertIn(sent.state, {TurnState.RUNNING, TurnState.UNKNOWN})
            finally:
                channel.close()

    def test_in_progress_recovery_is_unknown_and_never_resent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = CodexFixture(Path(directory), scenario="crash_in_progress")
            channel, _, _ = self.reserve_and_send(fixture)
            try:
                deadline = time.monotonic() + 3
                while channel._client is not None and channel._client.is_alive and time.monotonic() < deadline:
                    time.sleep(0.01)
                observation = channel.inspect_turn("SEND-001")
                self.assertEqual(EffectStatus.UNKNOWN, observation.status)
                self.assertEqual(TurnState.UNKNOWN, observation.state)
                methods = [frame["method"] for frame in fixture.wire()]
                self.assertEqual(1, methods.count("turn/start"))
            finally:
                channel.close()

    def test_cleanup_terminates_provider_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_file = root / "child.pid"
            fixture = CodexFixture(root)
            config = fixture.config((("CODEX_FAKE_CHILD", str(child_file)),))
            channel = CodexAppServerChannel(config, clock=lambda: OBSERVED_AT)
            reserve, _, _ = self.commands()
            channel.reserve(
                prepared_effect(self.identity, reserve, EffectOperation.RESERVE_CHANNEL, 1)
            )
            deadline = time.monotonic() + 3
            while not child_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(child_file.exists())
            child_pid = int(child_file.read_text(encoding="ascii"))
            self.assertEqual(("provider_process_tree",), channel.cleanup())
            deadline = time.monotonic() + 5
            while _pid_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_pid_exists(child_pid))

    def test_sibling_attempts_use_distinct_overlapping_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            barrier = root / "barrier"
            fixture_a = CodexFixture(root, scenario="overlap", name="a")
            fixture_b = CodexFixture(root, scenario="overlap", name="b")
            extra = (("CODEX_FAKE_BARRIER", str(barrier)),)
            channel_a = CodexAppServerChannel(fixture_a.config(extra), clock=lambda: OBSERVED_AT)
            channel_b = CodexAppServerChannel(fixture_b.config(extra), clock=lambda: OBSERVED_AT)
            identity_b = ExecutionIdentity("WISH-001", 1, "TASK-002", 1, "DISPATCH-002")
            reserve_a, send_a, _ = self.commands()
            reserve_b = replace(
                reserve_a,
                operation_id="RESERVE-002",
                attempt_id="ATTEMPT-002",
                dispatch_id="DISPATCH-002",
                channel_id="CHANNEL-002",
            )
            packet = send_a.task_packet
            send_b = replace(
                send_a,
                operation_id="SEND-002",
                attempt_id="ATTEMPT-002",
                dispatch_id="DISPATCH-002",
                channel_id="CHANNEL-002",
                message_id="MESSAGE-002",
                turn_id="LOGICAL-TURN-002",
                task_packet_digest=packet_digest(packet),
            )
            try:
                channel_a.reserve(prepared_effect(self.identity, reserve_a, EffectOperation.RESERVE_CHANNEL, 1))
                channel_b.reserve(prepared_effect(identity_b, reserve_b, EffectOperation.RESERVE_CHANNEL, 2))
                pids = {channel_a._client.process_id, channel_b._client.process_id}
                self.assertEqual(2, len(pids))
                results: list[object] = []

                def dispatch(channel, identity, command, number):
                    results.append(channel.send(prepared_effect(identity, command, EffectOperation.SEND_TASK_PACKET, number)))

                first = threading.Thread(target=dispatch, args=(channel_a, self.identity, send_a, 3))
                second = threading.Thread(target=dispatch, args=(channel_b, identity_b, send_b, 4))
                first.start()
                second.start()
                first.join(timeout=10)
                second.join(timeout=10)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(2, len(results))
                self.assertEqual(2, len(list(barrier.iterdir())))
            finally:
                channel_a.close()
                channel_b.close()


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ("tasklist", "/FI", f"PID eq {pid}", "/NH"),
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
