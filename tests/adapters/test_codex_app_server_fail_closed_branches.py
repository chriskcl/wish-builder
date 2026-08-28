from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from wish_builder.adapters.providers import codex_app_server as codex
from wish_builder.contracts.manifest_v2 import WorkerProvider
from wish_builder.contracts.runtime import EffectStatus
from wish_builder.services.ports import (
    BackendCapabilities,
    PreparedEffect,
    ReserveChannel,
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


if __name__ == "__main__":
    unittest.main()
