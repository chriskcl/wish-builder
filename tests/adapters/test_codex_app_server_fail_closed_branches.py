from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.adapters.test_codex_app_server import prepared_effect
from wish_builder.adapters.providers import codex_app_server as codex
from wish_builder.contracts.manifest_v2 import WorkerProvider
from wish_builder.contracts.runtime import EffectOperation, EffectStatus, ExecutionIdentity
from wish_builder.services.ports import (
    BackendCapabilities,
    CancelTurn,
    PreparedEffect,
    ReserveChannel,
    SendTaskPacket,
    TurnObservation,
    TurnState,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
OBSERVED_AT = "2026-08-21T06:00:00Z"


def capabilities(provider: WorkerProvider = WorkerProvider.CODEX) -> BackendCapabilities:
    return BackendCapabilities(
        provider=provider,
        platform="windows",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest=HASH_C,
        max_task_packet_bytes=4096,
    )


class RecoveryClient:
    def __init__(
        self,
        thread: dict[str, object] | None = None,
        *,
        connect_error: BaseException | None = None,
    ) -> None:
        self.thread = {"id": "thread-1", "turns": []} if thread is None else thread
        self.connect_error = connect_error
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return False

    @property
    def event_position(self) -> int:
        return 0

    def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error

    def start_thread(self, *, on_started=None) -> str:
        raise AssertionError("recovery must not start a thread")

    def begin_turn(self, **_kwargs) -> str:
        raise AssertionError("recovery must not begin a turn")

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        raise AssertionError("recovery must not interrupt a turn")

    def read_thread(self, thread_id: str) -> dict[str, object]:
        return self.thread

    def wait_for_turn_completed(self, thread_id: str, turn_id: str, **_kwargs):
        raise AssertionError("recovery must not wait for live notifications")

    def wait_for_turn_active(self, thread_id: str, turn_id: str, **_kwargs):
        raise AssertionError("recovery must not wait for live notifications")

    def completed_notification(self, thread_id: str, turn_id: str):
        return None

    def item_notifications(self, thread_id: str, turn_id: str):
        return ()

    def close(self) -> None:
        self.closed = True


class CodexAppServerFailClosedBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.worktree = self.root / "attempt"
        self.worktree.mkdir()
        self._channel_number = 0

    def launch(self) -> codex.CodexAppServerLaunch:
        return codex.CodexAppServerLaunch(
            command_prefix=(str(Path(sys.executable).resolve()),),
            sdk_version="0.149.0",
            sdk_shasum="abc123",
            sdk_integrity="sha512-integrity",
        )

    def config(self, state_name: str = "state") -> codex.CodexAppServerConfig:
        return codex.CodexAppServerConfig(
            capabilities(),
            self.launch(),
            self.worktree,
            self.root / state_name,
        )

    def channel(self, client: RecoveryClient | None = None) -> codex.CodexAppServerChannel:
        self._channel_number += 1
        config = self.config(f"state-{self._channel_number}")
        factory = codex.CodexAppServerClient if client is None else lambda *_args, **_kwargs: client
        channel = codex.CodexAppServerChannel(
            config,
            clock=lambda: OBSERVED_AT,
            client_factory=factory,
        )
        self.addCleanup(channel.close)
        return channel

    def commands(
        self,
        suffix: str = "1",
    ) -> tuple[ReserveChannel, SendTaskPacket, CancelTurn]:
        packet = '{"task":"frozen"}'
        reserve = ReserveChannel(
            operation_id=f"RESERVE-{suffix}",
            attempt_id="ATTEMPT-001",
            dispatch_id="DISPATCH-001",
            channel_id="CHANNEL-001",
            provider=WorkerProvider.CODEX,
            capability_digest=HASH_A,
            launch_profile_digest=HASH_B,
            policy_digest=HASH_C,
        )
        send = SendTaskPacket(
            operation_id=f"SEND-{suffix}",
            attempt_id="ATTEMPT-001",
            dispatch_id="DISPATCH-001",
            channel_id="CHANNEL-001",
            message_id="MESSAGE-001",
            turn_id="TURN-001",
            task_packet=packet,
            task_packet_digest=codex._sha256(packet.encode()),
        )
        cancel = CancelTurn(
            operation_id=f"CANCEL-{suffix}",
            attempt_id="ATTEMPT-001",
            channel_id="CHANNEL-001",
            turn_id="TURN-001",
            reason_code="operator_cancelled",
        )
        return reserve, send, cancel

    @staticmethod
    def effect(command, operation: EffectOperation, sequence: int = 1):
        identity = ExecutionIdentity("WISH-001", 1, "TASK-001", 1, "DISPATCH-001")
        return prepared_effect(identity, command, operation, sequence)

    def client(self) -> codex.CodexAppServerClient:
        return codex.CodexAppServerClient(
            self.launch(),
            working_directory=self.worktree,
            response_timeout_seconds=0.01,
        )

    def assert_codex_error(self, code: str, action) -> None:
        with self.assertRaises(codex.CodexAppServerError) as captured:
            action()
        self.assertEqual(code, captured.exception.code)

    def test_launch_client_and_config_validation_reject_bad_boundaries(self) -> None:
        launch = self.launch()
        launch_cases = (
            (TypeError, lambda: replace(launch, command_prefix=())),
            (
                TypeError,
                lambda: replace(
                    launch,
                    command_prefix=(str(Path(sys.executable).resolve()), ""),
                ),
            ),
            (ValueError, lambda: replace(launch, command_prefix=("relative-codex",))),
            (ValueError, lambda: replace(launch, sdk_version="")),
            (ValueError, lambda: replace(launch, sdk_shasum="")),
            (ValueError, lambda: replace(launch, sdk_integrity="")),
            (TypeError, lambda: replace(launch, extra_args=["--flag"])),
            (TypeError, lambda: replace(launch, extra_args=("",))),
        )
        for expected, action in launch_cases:
            with self.subTest(boundary="launch", expected=expected.__name__):
                with self.assertRaises(expected):
                    action()

        client_cases = (
            (
                TypeError,
                lambda: codex.CodexAppServerClient(
                    object(), working_directory=self.worktree
                ),
            ),
            (
                ValueError,
                lambda: codex.CodexAppServerClient(
                    launch, working_directory=Path("relative")
                ),
            ),
            (
                ValueError,
                lambda: codex.CodexAppServerClient(
                    launch, working_directory=self.root / "missing"
                ),
            ),
            (
                TypeError,
                lambda: codex.CodexAppServerClient(
                    launch,
                    working_directory=self.worktree,
                    environment={1: "bad"},
                ),
            ),
            (
                TypeError,
                lambda: codex.CodexAppServerClient(
                    launch, working_directory=self.worktree, frame_callback=1
                ),
            ),
            (
                ValueError,
                lambda: codex.CodexAppServerClient(
                    launch,
                    working_directory=self.worktree,
                    response_timeout_seconds=True,
                ),
            ),
            (
                ValueError,
                lambda: codex.CodexAppServerClient(
                    launch,
                    working_directory=self.worktree,
                    response_timeout_seconds=0,
                ),
            ),
        )
        for expected, action in client_cases:
            with self.subTest(boundary="client", expected=expected.__name__):
                with self.assertRaises(expected):
                    action()

        valid = self.config()
        config_cases = (
            (TypeError, lambda: replace(valid, capabilities=object())),
            (
                ValueError,
                lambda: replace(valid, capabilities=capabilities(WorkerProvider.PI)),
            ),
            (TypeError, lambda: replace(valid, launch=object())),
            (ValueError, lambda: replace(valid, working_directory=Path("relative"))),
            (ValueError, lambda: replace(valid, state_directory=Path("relative"))),
            (ValueError, lambda: replace(valid, working_directory=self.root / "missing")),
            (ValueError, lambda: replace(valid, state_directory=self.worktree / "state")),
            (TypeError, lambda: replace(valid, environment=[("A", "B")])),
            (TypeError, lambda: replace(valid, environment=(("A", 1),))),
        )
        for expected, action in config_cases:
            with self.subTest(boundary="config", expected=expected.__name__):
                with self.assertRaises(expected):
                    action()

        with self.assertRaises(TypeError):
            codex.CodexAppServerChannel(object())
        with self.assertRaises(TypeError):
            codex.CodexAppServerChannel(valid, clock=1)

    def test_schema_definition_rejects_unbounded_or_open_shapes(self) -> None:
        closed = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        huge_key = "x" * (64 * 1024)
        huge = {
            "type": "object",
            "properties": {huge_key: {"type": "string"}},
            "required": [huge_key],
            "additionalProperties": False,
        }
        deep: dict[str, object] = {"type": "string"}
        for _ in range(34):
            deep = {"type": "array", "items": deep}
        deep_root = {
            "type": "object",
            "properties": {"value": deep},
            "required": ["value"],
            "additionalProperties": False,
        }
        invalid = (
            (TypeError, []),
            (ValueError, huge),
            (ValueError, {"type": "array", "items": {"type": "string"}}),
            (ValueError, deep_root),
            (ValueError, dict(closed, unsupported=True)),
            (
                ValueError,
                {
                    "type": "object",
                    "properties": {"value": {"type": "number"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            (
                ValueError,
                {
                    "type": "object",
                    "properties": {"value": {"type": "string", "enum": []}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            (
                ValueError,
                {
                    "type": "object",
                    "properties": [],
                    "required": [],
                    "additionalProperties": False,
                },
            ),
            (
                ValueError,
                {
                    "type": "object",
                    "properties": {},
                    "required": ["missing"],
                    "additionalProperties": False,
                },
            ),
            (
                ValueError,
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": True,
                },
            ),
            (
                ValueError,
                {
                    "type": "object",
                    "properties": {"value": []},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
            (
                ValueError,
                {
                    "type": "object",
                    "properties": {"value": {"type": "array", "items": []}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
        )
        for expected, schema in invalid:
            with self.subTest(expected=expected.__name__):
                with self.assertRaises(expected):
                    codex._validate_schema_definition(schema)

    def test_structured_results_enforce_every_closed_schema_boundary(self) -> None:
        string_schema = {"type": "string", "minLength": 2, "maxLength": 3}
        array_schema = {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "maxItems": 2,
        }
        object_schema = {
            "type": "object",
            "properties": {"name": string_schema, "values": array_schema},
            "required": ["name", "values"],
            "additionalProperties": False,
        }
        invalid = (
            (1, string_schema),
            ("z", {"type": "string", "enum": ["x"]}),
            ("x", string_schema),
            ("long", string_schema),
            ([], array_schema),
            ([1, 2, 3], array_schema),
            (["wrong"], array_schema),
            ({"name": "ok"}, object_schema),
            ({"name": "ok", "values": [1], "extra": True}, object_schema),
        )
        for value, schema in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    codex._validate_json_schema(value, schema, "$")
        codex._validate_json_schema({"name": "ok", "values": [1]}, object_schema, "$")

        self.assert_codex_error(
            "codex_structured_result_missing",
            lambda: codex._structured_result(
                [{"type": "agentMessage", "phase": "commentary", "text": "{}"}],
                codex.CODEX_COMPLETION_SCHEMA,
            ),
        )
        self.assert_codex_error(
            "codex_structured_result_too_large",
            lambda: codex._structured_result(
                [{"type": "agentMessage", "text": "x" * (codex._MAX_FRAME_BYTES + 1)}],
                codex.CODEX_COMPLETION_SCHEMA,
            ),
        )
        self.assert_codex_error(
            "codex_structured_result_invalid_json",
            lambda: codex._structured_result(
                [{"type": "agentMessage", "text": "{"}],
                codex.CODEX_COMPLETION_SCHEMA,
            ),
        )
        self.assert_codex_error(
            "codex_structured_result_not_object",
            lambda: codex._structured_result(
                [{"type": "agentMessage", "text": "[]"}],
                codex.CODEX_COMPLETION_SCHEMA,
            ),
        )
        self.assert_codex_error(
            "codex_structured_result_schema_mismatch",
            lambda: codex._structured_result(
                [{"type": "agentMessage", "text": "{}"}],
                codex.CODEX_COMPLETION_SCHEMA,
            ),
        )

    def test_client_frame_and_request_boundaries_fail_closed(self) -> None:
        client = self.client()
        with self.assertRaises(ValueError):
            client.request("", {})
        self.assert_codex_error(
            "codex_process_not_started", lambda: client.request("initialize", {})
        )
        self.assert_codex_error(
            "codex_not_initialized",
            lambda: client.start_thread(),
        )
        self.assert_codex_error(
            "codex_command_frame_too_large",
            lambda: client._write_frame({"value": "x" * codex._MAX_FRAME_BYTES}),
        )
        self.assert_codex_error(
            "codex_stdin_unavailable", lambda: client._write_frame({"ok": True})
        )

        cases = (
            (b"{}", "codex_physical_frame_too_large"),
            (b"\r\n", "codex_non_lf_frame"),
            (b"\n", "codex_non_lf_frame"),
            (b"{\n", "codex_invalid_json"),
            (b"[]\n", "codex_frame_not_object"),
        )
        for raw, code in cases:
            candidate = self.client()
            candidate._read_stdout(io.BytesIO(raw))
            self.assertEqual(code, candidate._fatal.code)

        client = self.client()
        client._dispatch_frame({"id": 1, "result": {}})
        client._dispatch_frame({"id": 1, "result": {}})
        self.assertEqual("codex_duplicate_response", client._fatal.code)

        client = self.client()
        client._dispatch_frame({"method": "turn/started"})
        self.assertEqual(1, len(client._notifications))
        client._dispatch_frame({"unexpected": True})
        self.assertEqual("codex_invalid_frame_shape", client._fatal.code)

        client = codex.CodexAppServerClient(
            self.launch(),
            working_directory=self.worktree,
            frame_callback=lambda _frame: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        client._dispatch_frame({"method": "turn/completed", "params": {}})
        self.assertEqual("codex_frame_callback_failed", client._fatal.code)

        client = self.client()
        process = Mock()
        process.poll.return_value = None
        process.stdin = io.BytesIO()
        client._process = process
        client._fatal = codex.CodexAppServerError("reader_failed")
        self.assert_codex_error(
            "reader_failed", lambda: client.request("initialize", {})
        )

        client = self.client()
        process = Mock()
        process.poll.return_value = None
        process.stdin = io.BytesIO()
        client._process = process
        client._responses[1] = {"id": 1, "error": {"code": 10}}
        self.assert_codex_error(
            "codex_rpc_error", lambda: client.request("initialize", {})
        )

        client = self.client()
        process = Mock()
        process.poll.return_value = None
        process.stdin = io.BytesIO()
        client._process = process
        client._responses[1] = {"id": 1, "result": []}
        self.assert_codex_error(
            "codex_response_invalid", lambda: client.request("initialize", {})
        )

    def test_turn_activation_requires_an_exact_started_notification(self) -> None:
        client = self.client()
        turn_started = {
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1"},
            },
        }
        item_started = {
            "method": "item/started",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"id": "item-1"},
            },
        }
        client._notifications.extend(
            (
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "other-thread",
                        "turn": {"id": "turn-1"},
                    },
                },
                turn_started,
                item_started,
            )
        )
        self.assertEqual(
            turn_started,
            client.wait_for_turn_active("thread-1", "turn-1"),
        )

        item_client = self.client()
        item_client._notifications.append(item_started)
        self.assertEqual(
            item_started,
            item_client.wait_for_turn_active("thread-1", "turn-1"),
        )

        timeout = self.client()
        process = Mock()
        process.poll.return_value = None
        timeout._process = process
        with patch.object(codex.time, "monotonic", side_effect=(0.0, 1.0)):
            self.assert_codex_error(
                "codex_turn_activation_timeout",
                lambda: timeout.wait_for_turn_active(
                    "thread-1", "turn-1", timeout_seconds=0.5
                ),
            )

    def test_state_loading_and_observation_helpers_reject_ambiguity(self) -> None:
        unsafe = self.root / "unsafe"
        unsafe.mkdir()
        unsafe_path = unsafe / "channel-state.json"
        with unsafe_path.open("wb") as stream:
            stream.seek(codex._MAX_STATE_BYTES)
            stream.write(b"x")
        self.assert_codex_error(
            "codex_state_unsafe",
            lambda: codex.CodexAppServerChannel(self.config("unsafe")),
        )

        invalid = self.root / "invalid"
        invalid.mkdir()
        (invalid / "channel-state.json").write_bytes(b"\xff")
        self.assert_codex_error(
            "codex_state_invalid",
            lambda: codex.CodexAppServerChannel(self.config("invalid")),
        )

        malformed = self.root / "malformed"
        malformed.mkdir()
        (malformed / "channel-state.json").write_text(
            json.dumps({"schema_version": 1, "operations": []}), encoding="utf-8"
        )
        self.assert_codex_error(
            "codex_state_invalid",
            lambda: codex.CodexAppServerChannel(self.config("malformed")),
        )

        persisted = self.root / "persisted"
        persisted.mkdir()
        (persisted / "channel-state.json").write_text(
            json.dumps({"schema_version": 1, "operations": {}}), encoding="utf-8"
        )
        channel = codex.CodexAppServerChannel(
            self.config("persisted"), clock=lambda: OBSERVED_AT
        )
        self.addCleanup(channel.close)
        self.assertEqual(EffectStatus.ABSENT, channel.inspect_reservation("missing").status)
        self.assertEqual(EffectStatus.ABSENT, channel.inspect_turn("missing").status)
        self.assertIsNone(channel._reservation_observation())
        self.assertIsNone(channel.process_id)

        channel._state["operations"]["wrong-kind"] = {"kind": "reservation"}
        self.assertEqual(EffectStatus.UNKNOWN, channel.inspect_turn("wrong-kind").status)
        channel._state["operations"]["wrong-kind"] = {"kind": "send"}
        self.assertEqual(
            EffectStatus.UNKNOWN, channel.inspect_reservation("wrong-kind").status
        )

        channel._state["operations"]["collision"] = {
            "kind": "send",
            "command_hash": HASH_A,
        }
        channel_collision = channel._existing("collision", HASH_B, "reservation")
        self.assertEqual(
            EffectStatus.UNKNOWN,
            channel._channel_observation(channel_collision).status,
        )
        turn_collision = channel._existing("collision", HASH_B, "send")
        self.assertEqual(
            EffectStatus.UNKNOWN, channel._turn_observation(turn_collision).status
        )
        self.assertIs(
            channel._state["operations"]["collision"],
            channel._existing("collision", HASH_A, "send"),
        )

        with self.assertRaises(codex.CodexAppServerError):
            channel._operation("absent")
        with self.assertRaises(codex.CodexAppServerError):
            channel._channel_observation({})
        with self.assertRaises(codex.CodexAppServerError):
            channel._turn_observation({})
        with self.assertRaises(ValueError):
            channel._validate_operation_id("")
        with self.assertRaises(TypeError):
            channel._require_effect(None, ReserveChannel)

        shell = object.__new__(PreparedEffect)
        object.__setattr__(shell, "command", object())
        with self.assertRaises(TypeError):
            channel._require_effect(shell, ReserveChannel)

        channel._state["oversized"] = "x" * codex._MAX_STATE_BYTES
        self.assert_codex_error("codex_state_too_large", channel._save_state)

        cleanup_state = self.root / "cleanup"
        cleanup_state.mkdir()
        cleanup = codex.CodexAppServerChannel(self.config("cleanup"))
        self.assertEqual(
            ("provider_process_tree", "provider_session_state"),
            cleanup.cleanup(remove_durable_state=True),
        )
        self.assertFalse(cleanup_state.exists())

    def _install_send(
        self,
        channel: codex.CodexAppServerChannel,
        *,
        status: EffectStatus = EffectStatus.UNKNOWN,
        state: TurnState = TurnState.UNKNOWN,
        provider_turn_id: object = "provider-turn-1",
        message_id: object = "message-1",
        include_thread: bool = True,
    ) -> None:
        operation: dict[str, object] = {
            "kind": "send",
            "command_hash": HASH_A,
            "observation": TurnObservation(
                operation_id="send-1",
                status=status,
                observed_at=OBSERVED_AT,
                state=state,
                effect_digest=HASH_B if status is EffectStatus.APPLIED else None,
                attempt_id="attempt-1" if status is EffectStatus.APPLIED else None,
                channel_id="channel-1" if status is EffectStatus.APPLIED else None,
                message_id="message-1" if status is EffectStatus.APPLIED else None,
                turn_id="turn-1" if status is EffectStatus.APPLIED else None,
                evidence=("persisted_state",),
            ).to_primitive(),
            "command": {
                "attempt_id": "attempt-1",
                "channel_id": "channel-1",
                "message_id": message_id,
                "logical_turn_id": "turn-1",
            },
        }
        if provider_turn_id is not None:
            operation["provider_turn_id"] = provider_turn_id
        channel._state["operations"]["send-1"] = operation
        if include_thread:
            channel._state["thread_id"] = "thread-1"

    @staticmethod
    def _turn(
        turn_id: object = "provider-turn-1",
        *,
        status: str = "failed",
        messages: int = 1,
    ) -> dict[str, object]:
        return {
            "id": turn_id,
            "status": status,
            "items": [
                {
                    "type": "userMessage",
                    "clientId": "message-1",
                }
                for _ in range(messages)
            ],
        }

    def test_reconciliation_fails_closed_for_missing_or_ambiguous_identity(self) -> None:
        terminal = self.channel()
        self._install_send(
            terminal, status=EffectStatus.APPLIED, state=TurnState.FAILED
        )
        self.assertEqual(TurnState.FAILED, terminal._reconcile_locked("send-1").state)

        missing_identity = self.channel()
        self._install_send(missing_identity, include_thread=False)
        observed = missing_identity._reconcile_locked("send-1")
        self.assertIn("codex_reconcile_identity_missing", observed.evidence)

        invalid_client = RecoveryClient({"id": "thread-1", "turns": "invalid"})
        invalid = self.channel(invalid_client)
        self._install_send(invalid)
        observed = invalid._reconcile_locked("send-1")
        self.assertIn("codex_reconcile_failed:codex_thread_turns_invalid", observed.evidence)
        self.assertTrue(invalid_client.closed)

        mismatched_client = RecoveryClient(
            {"id": "thread-1", "turns": [self._turn("different-turn")]}
        )
        mismatched = self.channel(mismatched_client)
        self._install_send(mismatched)
        observed = mismatched._reconcile_locked("send-1")
        self.assertIn("codex_reconcile_ambiguous", observed.evidence)

        duplicate_client = RecoveryClient(
            {"id": "thread-1", "turns": [self._turn(messages=2)]}
        )
        duplicate = self.channel(duplicate_client)
        self._install_send(duplicate)
        observed = duplicate._reconcile_locked("send-1")
        self.assertIn("codex_reconcile_ambiguous", observed.evidence)

        invalid_turn_id_client = RecoveryClient(
            {"id": "thread-1", "turns": [self._turn(1)]}
        )
        invalid_turn_id = self.channel(invalid_turn_id_client)
        self._install_send(invalid_turn_id, provider_turn_id=None)
        observed = invalid_turn_id._reconcile_locked("send-1")
        self.assertIn("codex_reconcile_ambiguous", observed.evidence)

        in_progress_client = RecoveryClient(
            {"id": "thread-1", "turns": [self._turn(status="inProgress")]}
        )
        in_progress = self.channel(in_progress_client)
        self._install_send(in_progress)
        observed = in_progress._reconcile_locked("send-1")
        self.assertIn("codex_turn_still_in_progress", observed.evidence)

        failed_client = RecoveryClient(
            {"id": "thread-1", "turns": [self._turn(status="failed")]}
        )
        failed = self.channel(failed_client)
        self._install_send(failed)
        observed = failed._reconcile_locked("send-1")
        self.assertEqual(EffectStatus.APPLIED, observed.status)
        self.assertEqual(TurnState.FAILED, observed.state)
        self.assertIn("codex_thread_read_reconciled", observed.evidence)

        connect_error_client = RecoveryClient(connect_error=OSError("offline"))
        connect_error = self.channel(connect_error_client)
        self._install_send(connect_error)
        observed = connect_error._reconcile_locked("send-1")
        self.assertIn("codex_reconcile_failed:OSError", observed.evidence)
        self.assertTrue(connect_error_client.closed)

    def test_terminal_frame_identity_mismatch_is_unknown(self) -> None:
        client = RecoveryClient()
        channel = self.channel(client)
        self._install_send(channel)
        mismatch = channel._terminal_from_frame(
            "send-1",
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "different-thread",
                    "turn": self._turn(),
                },
            },
            client,
        )
        self.assertEqual(EffectStatus.UNKNOWN, mismatch.status)
        self.assertIn("codex_terminal_identity_mismatch", mismatch.evidence)

        terminal = channel._terminal_from_frame(
            "send-1",
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": self._turn()},
            },
            client,
        )
        self.assertEqual(TurnState.FAILED, terminal.state)

    def test_client_lifecycle_and_rpc_errors_close_every_boundary(self) -> None:
        self.assertRegex(codex._utc_now(), r"Z$")
        client = self.client()
        client._stderr.extend(b"bad\xff")
        self.assertEqual("bad\ufffd", client.stderr_text)
        completed = {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": self._turn("provider-turn-1", status="failed"),
            },
        }
        client._notifications.extend(
            [{"method": "item/started", "params": {}}, completed]
        )
        self.assertEqual(
            completed,
            client.completed_notification("thread-1", "provider-turn-1"),
        )

        process = Mock()
        process.poll.return_value = None
        process.pid = 123
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        thread = Mock()
        with (
            patch.object(codex.os, "name", "posix"),
            patch.object(codex.subprocess, "Popen", return_value=process),
            patch.object(codex.threading, "Thread", return_value=thread),
            patch.object(client, "request", return_value={}),
            patch.object(client, "notify") as notify,
        ):
            client.connect()
            notify.assert_called_once_with("initialized", None)
            self.assert_codex_error("codex_process_already_started", client.connect)

        windows = self.client()
        popen = Mock(return_value=process)
        with (
            patch.object(codex.os, "name", "nt"),
            patch.object(
                codex.subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                512,
                create=True,
            ),
            patch.object(codex.subprocess, "Popen", popen),
            patch.object(codex.threading, "Thread", return_value=Mock()),
            patch.object(windows, "request", return_value={}),
            patch.object(windows, "notify"),
        ):
            windows.connect()
        self.assertEqual(512, popen.call_args.kwargs["creationflags"])
        self.assertNotIn("start_new_session", popen.call_args.kwargs)

        failed = self.client()
        with patch.object(codex.subprocess, "Popen", side_effect=OSError("denied")):
            self.assert_codex_error("codex_process_start_failed", failed.connect)

        invalid = self.client()
        invalid_process = Mock()
        invalid_process.poll.return_value = 0
        invalid_process.stdin = io.BytesIO()
        invalid_process.stdout = io.BytesIO()
        invalid_process.stderr = io.BytesIO()
        with (
            patch.object(codex.subprocess, "Popen", return_value=invalid_process),
            patch.object(codex.threading, "Thread", return_value=Mock()),
            patch.object(invalid, "request", return_value=[]),
        ):
            self.assert_codex_error("codex_initialize_invalid", invalid.connect)

        initialized = self.client()
        initialized._initialized = True
        live = Mock()
        live.poll.return_value = None
        live.stdin = io.BytesIO()
        initialized._process = live
        with patch.object(initialized, "request", return_value={"thread": {}}):
            self.assert_codex_error("codex_thread_start_invalid", initialized.start_thread)
        with self.assertRaises(ValueError):
            initialized.begin_turn(
                thread_id="",
                message_id="message",
                task_packet="packet",
                output_schema=codex.CODEX_COMPLETION_SCHEMA,
            )
        with patch.object(initialized, "request", return_value={"turn": {}}):
            self.assert_codex_error(
                "codex_turn_start_invalid",
                lambda: initialized.begin_turn(
                    thread_id="thread",
                    message_id="message",
                    task_packet="packet",
                    output_schema=codex.CODEX_COMPLETION_SCHEMA,
                ),
            )
        accepted: list[tuple[str, str]] = []
        with patch.object(
            initialized, "request", return_value={"turn": {"id": "provider-turn"}}
        ):
            self.assertEqual(
                "provider-turn",
                initialized.begin_turn(
                    thread_id="thread",
                    message_id="message",
                    task_packet="packet",
                    output_schema=codex.CODEX_COMPLETION_SCHEMA,
                    on_accepted=lambda *value: accepted.append(value),
                ),
            )
            self.assertEqual(
                "provider-turn",
                initialized.begin_turn(
                    thread_id="thread",
                    message_id="message",
                    task_packet="packet",
                    output_schema=codex.CODEX_COMPLETION_SCHEMA,
                ),
            )
        self.assertEqual([("thread", "provider-turn")], accepted)
        with patch.object(initialized, "request", return_value=[]):
            self.assert_codex_error(
                "codex_turn_interrupt_invalid",
                lambda: initialized.interrupt_turn(thread_id="thread", turn_id="turn"),
            )
        with patch.object(
            initialized, "request", return_value={"thread": {"id": "other"}}
        ):
            self.assert_codex_error(
                "codex_thread_read_invalid", lambda: initialized.read_thread("thread")
            )

        timeout = self.client()
        timeout._process = live
        timeout._response_timeout = 0
        with patch.object(timeout, "_write_frame"):
            self.assert_codex_error(
                "codex_response_timeout", lambda: timeout.request("method", {})
            )
        with patch.object(timeout, "_write_frame") as writer:
            timeout.notify("event", {"value": 1})
            writer.assert_called_once_with(
                {"method": "event", "params": {"value": 1}}
            )

        broken = self.client()
        broken._process = Mock(poll=Mock(return_value=None))
        broken._process.stdin = Mock()
        broken._process.stdin.write.side_effect = BrokenPipeError("closed")
        self.assert_codex_error(
            "codex_write_failed", lambda: broken._write_frame({"ok": True})
        )

        client._reader = Mock()
        client._stderr_reader = threading.current_thread()
        failing_stream = Mock()
        failing_stream.close.side_effect = OSError("closed")
        process.stdin = failing_stream
        process.stdout = None
        process.stderr = io.BytesIO()
        with patch.object(client, "_terminate_process_tree") as terminate:
            client.close()
            client.close()
        terminate.assert_called_once_with(process)
        client._reader.join.assert_called_once_with(timeout=2.0)
        self.client().close()

        unavailable = self.client()
        self.assert_codex_error("codex_process_exited", unavailable._raise_if_unavailable)

    def test_reader_and_process_termination_paths_are_bounded(self) -> None:
        client = self.client()
        client._output_bytes = codex._MAX_OUTPUT_BYTES
        client._read_stdout(io.BytesIO(b"{}\n"))
        self.assertEqual("codex_output_limit_exceeded", client._fatal.code)
        closed_reader = self.client()
        closed_reader._closed = True
        closed_reader._read_stdout(io.BytesIO(b"{\n"))
        self.assertIsNone(closed_reader._fatal)

        preset = self.client()
        preset._fatal = codex.CodexAppServerError("first_failure")
        with patch.object(preset, "_write_frame", side_effect=OSError("closed")):
            with self.assertRaises(OSError):
                preset._dispatch_frame({"method": "server/request", "id": 1})
        self.assertEqual("first_failure", preset._fatal.code)

        callback = codex.CodexAppServerClient(
            self.launch(),
            working_directory=self.worktree,
            frame_callback=lambda _frame: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        callback._fatal = codex.CodexAppServerError("first_failure")
        callback._dispatch_frame({"method": "turn/completed"})
        self.assertEqual("first_failure", callback._fatal.code)

        failing_reader = Mock()
        failing_reader.read.side_effect = OSError("denied")
        client._read_stderr(failing_reader)
        captured = self.client()
        captured._read_stderr(io.BytesIO(b"stderr"))
        self.assertEqual("stderr", captured.stderr_text)
        bounded = self.client()
        bounded._stderr.extend(b"x" * codex._STDERR_LIMIT)
        bounded._read_stderr(io.BytesIO(b"ignored"))
        self.assertEqual(codex._STDERR_LIMIT, len(bounded._stderr))

        exited = Mock()
        exited.poll.return_value = 0
        codex.CodexAppServerClient._terminate_process_tree(exited)

        normal = Mock()
        normal.poll.return_value = None
        normal.pid = 42
        with (
            patch.object(codex.os, "name", "posix"),
            patch.object(codex.os, "killpg", create=True) as killpg,
        ):
            codex.CodexAppServerClient._terminate_process_tree(normal)
        killpg.assert_called_once_with(42, codex.signal.SIGTERM)

        fallback = Mock()
        fallback.poll.return_value = None
        fallback.pid = 43
        fallback.wait.side_effect = (OSError("first"), None)
        with patch.object(codex.os, "name", "nt"):
            codex.CodexAppServerClient._terminate_process_tree(fallback)
        fallback.kill.assert_called_once_with()

        abandoned = Mock()
        abandoned.poll.return_value = None
        abandoned.pid = 44
        abandoned.wait.side_effect = OSError("wait")
        with (
            patch.object(codex.os, "name", "posix"),
            patch.object(
                codex.os,
                "killpg",
                side_effect=OSError("signal"),
                create=True,
            ),
            patch.object(codex.signal, "SIGKILL", 9, create=True),
        ):
            codex.CodexAppServerClient._terminate_process_tree(abandoned)

    def test_channel_admission_and_recovery_edges_fail_closed(self) -> None:
        reserve, send, cancel = self.commands()
        reserve_effect = self.effect(reserve, EffectOperation.RESERVE_CHANNEL)

        mismatch = self.channel()
        mismatch_reserve = replace(reserve, provider=WorkerProvider.PI)
        mismatch_effect = self.effect(
            mismatch_reserve, EffectOperation.RESERVE_CHANNEL, 2
        )
        first = mismatch.reserve(mismatch_effect)
        self.assertIn("capability_mismatch", first.evidence)
        self.assertEqual(first, mismatch.reserve(mismatch_effect))

        occupied = self.channel()
        occupied._state["reservation"] = "other"
        observed = occupied.reserve(reserve_effect)
        self.assertIn("attempt_already_reserved", observed.evidence)

        offline = self.channel(RecoveryClient(connect_error=OSError("offline")))
        observed = offline.reserve(reserve_effect)
        self.assertIn("OSError", observed.evidence)

        invalid_factory = codex.CodexAppServerChannel(
            self.config("invalid-factory"), client_factory=lambda *_args, **_kwargs: object()
        )
        self.addCleanup(invalid_factory.close)
        with self.assertRaises(TypeError):
            invalid_factory._new_client()
        self.assert_codex_error(
            "codex_live_process_unavailable", invalid_factory._ensure_live_client
        )

        unreserved = self.channel()
        send_effect = self.effect(send, EffectOperation.SEND_TASK_PACKET, 3)
        observed_turn = unreserved.send(send_effect)
        self.assertIn("channel_not_reserved", observed_turn.evidence)

        live_client = Mock(spec=codex.CodexClientPort)
        live_client.is_alive = True
        live_client.event_position = 5
        live_client.connect.return_value = None

        def start_thread(*, on_started=None):
            on_started("thread-1")
            return "thread-1"

        live_client.start_thread.side_effect = start_thread
        live_client.completed_notification.return_value = None
        live_client.item_notifications.return_value = ()
        live_channel = self.channel(live_client)
        self.assertEqual(EffectStatus.APPLIED, live_channel.reserve(reserve_effect).status)
        self.assertEqual(
            EffectStatus.APPLIED,
            live_channel.inspect_reservation(reserve.operation_id).status,
        )
        self.assertEqual(live_channel.state_path, live_channel.state_path)

        oversized_packet = "x" * (capabilities().max_task_packet_bytes + 1)
        oversized = replace(
            send,
            operation_id="SEND-OVERSIZED",
            task_packet=oversized_packet,
            task_packet_digest=codex._sha256(oversized_packet.encode()),
        )
        observed_turn = live_channel.send(
            self.effect(oversized, EffectOperation.SEND_TASK_PACKET, 4)
        )
        self.assertIn("task_packet_exceeds_capability", observed_turn.evidence)

        live_channel._state["active_send"] = "another"
        active = replace(send, operation_id="SEND-ACTIVE")
        observed_turn = live_channel.send(
            self.effect(active, EffectOperation.SEND_TASK_PACKET, 5)
        )
        self.assertIn("attempt_already_has_turn", observed_turn.evidence)
        live_channel._state.pop("active_send")

        def wrong_thread(**kwargs):
            kwargs["on_accepted"]("wrong-thread", "provider-turn")
            return "provider-turn"

        live_client.begin_turn.side_effect = wrong_thread
        observed_turn = live_channel.send(send_effect)
        self.assertIn("codex_send_ambiguous:codex_thread_identity_mismatch", observed_turn.evidence)

        existing = live_channel.send(send_effect)
        self.assertEqual(EffectStatus.UNKNOWN, existing.status)
        self.assertIn("codex_reconcile_failed:codex_thread_turns_invalid", existing.evidence)

        no_turn = self.channel()
        cancel_effect = self.effect(cancel, EffectOperation.CANCEL_TURN, 6)
        missing_cancel = no_turn.cancel(cancel_effect)
        self.assertIn("turn_not_found", missing_cancel.evidence)
        self.assertEqual(missing_cancel, no_turn.cancel(cancel_effect))

        live_channel._state["active_send"] = send.operation_id
        live_channel._operation(send.operation_id)["provider_turn_id"] = "provider-turn"
        live_client.begin_turn.side_effect = None
        live_client.interrupt_turn.return_value = None
        live_client.wait_for_turn_completed.return_value = {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": self._turn(status="failed")},
        }
        observed_cancel = live_channel.cancel(cancel_effect)
        self.assertIn("codex_cancel_ambiguous:codex_interrupt_not_cancelled", observed_cancel.evidence)

        invalid_cancel, invalid_send = self.commands("INVALID")[2], self.commands("INVALID")[1]
        invalid_effect = self.effect(
            invalid_cancel, EffectOperation.CANCEL_TURN, 7
        )
        invalid_channel = self.channel(live_client)
        invalid_channel._state["active_send"] = invalid_send.operation_id
        invalid_channel._put_operation(
            invalid_send.operation_id,
            "send",
            HASH_A,
            TurnObservation(
                operation_id=invalid_send.operation_id,
                status=EffectStatus.UNKNOWN,
                observed_at=OBSERVED_AT,
                state=TurnState.UNKNOWN,
                evidence=("pending",),
            ).to_primitive(),
            command={},
        )
        self.assertIn("turn_not_found", invalid_channel.cancel(invalid_effect).evidence)

    def test_channel_frame_and_helper_guards_ignore_unbound_events(self) -> None:
        channel = self.channel()
        channel._on_frame({"method": "turn/completed", "params": {}})
        channel._state["active_send"] = "missing"
        channel._on_frame(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "provider-turn"}},
            }
        )
        self._install_send(channel)
        channel._state["active_send"] = "send-1"
        channel._on_frame(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "other"}},
            }
        )
        channel._client = None
        channel._on_frame(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "provider-turn-1"}},
            }
        )
        channel._apply_live_terminal_locked("send-1", "provider-turn-1")

        alive = Mock(spec=codex.CodexClientPort)
        alive.is_alive = True
        alive.completed_notification.return_value = {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": self._turn("provider-turn-1", status="failed"),
            },
        }
        alive.item_notifications.return_value = ()
        channel._client = alive
        pending_observation = dict(channel._operation("send-1")["observation"])
        channel._apply_live_terminal_locked("send-1", "provider-turn-1")
        self.assertEqual(
            EffectStatus.APPLIED,
            channel._turn_observation(channel._operation("send-1")).status,
        )
        self.assertEqual(EffectStatus.APPLIED, channel.inspect_turn("send-1").status)

        channel._operation("send-1")["observation"] = pending_observation
        self.assertEqual(TurnState.FAILED, channel.inspect_turn("send-1").state)

        channel._operation("send-1")["observation"] = pending_observation
        channel._operation("send-1").pop("provider_turn_id")
        self.assertEqual(TurnState.UNKNOWN, channel.inspect_turn("send-1").state)

        no_expected = self.channel(
            RecoveryClient({"id": "thread-1", "turns": [self._turn()]})
        )
        self._install_send(no_expected, provider_turn_id=None)
        reconciled = no_expected._reconcile_locked("send-1")
        self.assertEqual(TurnState.FAILED, reconciled.state)
        self.assertEqual(
            "provider-turn-1",
            no_expected._operation("send-1")["provider_turn_id"],
        )
        unknown_terminal = no_expected._terminal_observation(
            "send-1",
            self._turn(status="unknown"),
            [],
            "test",
        )
        self.assertIn("codex_turn_not_terminal", unknown_terminal.evidence)

        identity_none = self.channel()
        self._install_send(identity_none, provider_turn_id=None, include_thread=False)
        terminal = identity_none._terminal_from_frame(
            "send-1",
            {
                "params": {
                    "threadId": None,
                    "turn": self._turn(None, status="failed"),
                }
            },
            RecoveryClient(),
        )
        self.assertEqual(TurnState.FAILED, terminal.state)

        self.assertEqual([], codex._turn_items({"items": "invalid"}))
        self.assertEqual(
            [{"type": "agentMessage"}],
            codex._items_from_notifications(
                (
                    {"params": {}},
                    {"params": {"item": {"type": "agentMessage"}}},
                )
            ),
        )
        self.assertEqual(0, codex._turn_client_message_count({}, None))
        with self.assertRaises(ValueError):
            codex._validate_schema_node(
                {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": True,
                },
                0,
            )


if __name__ == "__main__":
    unittest.main()
