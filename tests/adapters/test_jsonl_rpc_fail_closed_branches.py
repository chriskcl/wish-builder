from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.adapters.test_jsonl_rpc_provider import prepared_effect
from wish_builder.adapters.providers import jsonl_rpc as rpc
from wish_builder.contracts import canonical_json_bytes
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
OBSERVED_AT = "2026-08-21T05:00:00Z"


def capabilities(provider: WorkerProvider) -> BackendCapabilities:
    return BackendCapabilities(
        provider=provider,
        platform="windows",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest=HASH_C,
        max_task_packet_bytes=4096,
    )


class JsonlRpcFailClosedBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.worktree = self.root / "attempt"
        self.worktree.mkdir()

    def launch(self, provider: WorkerProvider = WorkerProvider.PI) -> rpc.JsonlRpcLaunch:
        return rpc.JsonlRpcLaunch(
            provider=provider,
            protocol=(
                rpc.JsonlRpcProtocol.PI
                if provider is WorkerProvider.PI
                else rpc.JsonlRpcProtocol.OH_MY_PI_V2
            ),
            command_prefix=(str(Path(sys.executable).resolve()),),
            sdk_name="pi-sdk",
            sdk_version="1.2.3",
        )

    def config(
        self,
        state_name: str = "state",
        provider: WorkerProvider = WorkerProvider.PI,
    ) -> rpc.JsonlRpcBackendConfig:
        return rpc.JsonlRpcBackendConfig(
            capabilities(provider),
            self.launch(provider),
            self.worktree,
            self.root / state_name,
        )

    def client(self, provider: WorkerProvider = WorkerProvider.PI) -> rpc.JsonlRpcClient:
        return rpc.JsonlRpcClient(
            self.launch(provider),
            working_directory=self.worktree,
            session_directory=self.root / "session",
            handshake_timeout_seconds=0.01,
            response_timeout_seconds=0.01,
        )

    def commands(
        self,
        suffix: str = "001",
        provider: WorkerProvider = WorkerProvider.PI,
    ) -> tuple[ReserveChannel, SendTaskPacket, CancelTurn]:
        packet = '{"task":"frozen"}'
        reserve = ReserveChannel(
            operation_id=f"RESERVE-{suffix}",
            attempt_id="ATTEMPT-001",
            dispatch_id="DISPATCH-001",
            channel_id="CHANNEL-001",
            provider=provider,
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
            task_packet_digest=rpc._sha256(packet.encode()),
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

    def assert_rpc_error(self, code: str, action) -> None:
        with self.assertRaises(rpc.JsonlRpcError) as captured:
            action()
        self.assertEqual(code, captured.exception.code)

    def test_launch_validation_and_provider_specific_resume_arguments(self) -> None:
        pi = self.launch()
        omp = self.launch(WorkerProvider.OH_MY_PI)
        session_directory = self.root / "sessions"

        self.assertIn("--session-dir", pi.argv(session_directory))
        self.assertIn("--session", pi.argv(session_directory, "pi.jsonl"))
        self.assertIn("--resume", omp.argv(session_directory, "omp.jsonl"))

        invalid = (
            (ValueError, lambda: replace(pi, provider=WorkerProvider.CODEX)),
            (
                ValueError,
                lambda: replace(pi, protocol=rpc.JsonlRpcProtocol.OH_MY_PI_V2),
            ),
            (TypeError, lambda: replace(pi, command_prefix=())),
            (
                TypeError,
                lambda: replace(
                    pi,
                    command_prefix=(str(Path(sys.executable).resolve()), ""),
                ),
            ),
            (ValueError, lambda: replace(pi, command_prefix=("relative-provider",))),
            (ValueError, lambda: replace(pi, sdk_name="")),
            (ValueError, lambda: replace(pi, sdk_version="")),
            (TypeError, lambda: replace(pi, extra_args=["--flag"])),
            (TypeError, lambda: replace(pi, extra_args=("",))),
        )
        for expected, action in invalid:
            with self.subTest(expected=expected.__name__, action=action):
                with self.assertRaises(expected):
                    action()

    def test_client_and_config_reject_invalid_boundaries(self) -> None:
        launch = self.launch()
        session = self.root / "session"
        client_cases = (
            (
                TypeError,
                lambda: rpc.JsonlRpcClient(
                    object(),
                    working_directory=self.worktree,
                    session_directory=session,
                ),
            ),
            (
                ValueError,
                lambda: rpc.JsonlRpcClient(
                    launch,
                    working_directory=Path("relative"),
                    session_directory=session,
                ),
            ),
            (
                ValueError,
                lambda: rpc.JsonlRpcClient(
                    launch,
                    working_directory=self.root / "missing",
                    session_directory=session,
                ),
            ),
            (
                TypeError,
                lambda: rpc.JsonlRpcClient(
                    launch,
                    working_directory=self.worktree,
                    session_directory=session,
                    environment={1: "bad"},
                ),
            ),
            (
                TypeError,
                lambda: rpc.JsonlRpcClient(
                    launch,
                    working_directory=self.worktree,
                    session_directory=session,
                    frame_callback=1,
                ),
            ),
            (
                ValueError,
                lambda: rpc.JsonlRpcClient(
                    launch,
                    working_directory=self.worktree,
                    session_directory=session,
                    handshake_timeout_seconds=True,
                ),
            ),
            (
                ValueError,
                lambda: rpc.JsonlRpcClient(
                    launch,
                    working_directory=self.worktree,
                    session_directory=session,
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
            (TypeError, lambda: replace(valid, launch=object())),
            (
                ValueError,
                lambda: replace(valid, capabilities=capabilities(WorkerProvider.OH_MY_PI)),
            ),
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
            rpc.JsonlRpcBackendChannel(object())
        with self.assertRaises(TypeError):
            rpc.JsonlRpcBackendChannel(valid, clock=1)

    def _chunk(
        self,
        *,
        index: int,
        count: int,
        byte_length: int,
        raw: bytes,
        chunk_id: str = "chunk-1",
    ) -> dict[str, object]:
        return {
            "type": "rpc_chunk",
            "chunkId": chunk_id,
            "index": index,
            "count": count,
            "byteLength": byte_length,
            "data": base64.b64encode(raw).decode("ascii"),
        }

    def _decode_frame(self, raw: bytes) -> dict[str, object]:
        decoder = rpc._OmpFrameDecoder()
        chunks = [
            raw[index : index + rpc._MAX_CHUNK_BYTES]
            for index in range(0, len(raw), rpc._MAX_CHUNK_BYTES)
        ]
        result = None
        for index, chunk in enumerate(chunks):
            result = decoder.push(
                self._chunk(
                    index=index,
                    count=len(chunks),
                    byte_length=len(raw),
                    raw=chunk,
                )
            )
            if index + 1 < len(chunks):
                self.assertIsNone(result)
        assert result is not None
        return result

    def test_omp_chunk_decoder_reassembles_and_rejects_ambiguous_streams(self) -> None:
        decoder = rpc._OmpFrameDecoder()
        ordinary = {"type": "response", "id": "one"}
        self.assertIs(ordinary, decoder.push(ordinary))

        invalid_metadata = {"type": "rpc_chunk", "chunkId": "missing-fields"}
        self.assert_rpc_error(
            "invalid_rpc_chunk_metadata", lambda: decoder.push(invalid_metadata)
        )

        metadata = self._chunk(
            index=0,
            count=2,
            byte_length=rpc._MAX_PHYSICAL_FRAME_BYTES,
            raw=b"x",
        )
        bad_data = dict(metadata, data="not-base64!")
        self.assert_rpc_error("invalid_rpc_chunk_data", lambda: decoder.push(bad_data))
        empty_data = dict(metadata, data="")
        self.assert_rpc_error("invalid_rpc_chunk_data", lambda: decoder.push(empty_data))

        start_at_one = self._chunk(
            index=1,
            count=2,
            byte_length=rpc._MAX_PHYSICAL_FRAME_BYTES,
            raw=b"x",
        )
        self.assert_rpc_error(
            "rpc_chunk_sequence_start",
            lambda: rpc._OmpFrameDecoder().push(start_at_one),
        )

        interrupted = rpc._OmpFrameDecoder()
        self.assertIsNone(interrupted.push(metadata))
        self.assert_rpc_error(
            "rpc_chunk_interrupted", lambda: interrupted.push(ordinary)
        )

        mismatched = rpc._OmpFrameDecoder()
        self.assertIsNone(mismatched.push(metadata))
        repeated_first = dict(metadata)
        self.assert_rpc_error(
            "rpc_chunk_sequence_mismatch", lambda: mismatched.push(repeated_first)
        )

        exceeded = rpc._OmpFrameDecoder()
        full = b"x" * rpc._MAX_CHUNK_BYTES
        for index in range(4):
            self.assertIsNone(
                exceeded.push(
                    self._chunk(
                        index=index,
                        count=5,
                        byte_length=rpc._MAX_PHYSICAL_FRAME_BYTES,
                        raw=full,
                    )
                )
            )
        self.assert_rpc_error(
            "rpc_chunk_length_exceeded",
            lambda: exceeded.push(
                self._chunk(
                    index=4,
                    count=5,
                    byte_length=rpc._MAX_PHYSICAL_FRAME_BYTES,
                    raw=b"x",
                )
            ),
        )

        short = rpc._OmpFrameDecoder()
        self.assertIsNone(short.push(metadata))
        self.assert_rpc_error(
            "rpc_chunk_length_mismatch",
            lambda: short.push(
                self._chunk(
                    index=1,
                    count=2,
                    byte_length=rpc._MAX_PHYSICAL_FRAME_BYTES,
                    raw=b"y",
                )
            ),
        )

        invalid_json = b"x" * rpc._MAX_PHYSICAL_FRAME_BYTES
        self.assert_rpc_error(
            "invalid_reassembled_rpc_frame", lambda: self._decode_frame(invalid_json)
        )
        scalar = b"0" + b" " * (rpc._MAX_PHYSICAL_FRAME_BYTES - 1)
        self.assert_rpc_error("rpc_frame_not_object", lambda: self._decode_frame(scalar))

        text = "x" * rpc._MAX_PHYSICAL_FRAME_BYTES
        encoded = json.dumps({"payload": text}, separators=(",", ":")).encode("utf-8")
        self.assertEqual({"payload": text}, self._decode_frame(encoded))

    def test_transport_reader_and_dispatch_fail_closed(self) -> None:
        cases = (
            (b"{}", "rpc_physical_frame_too_large"),
            (b"\n", "rpc_blank_frame"),
            (b"{\n", "rpc_invalid_json"),
            (b"[]\n", "rpc_frame_not_object"),
        )
        for raw, code in cases:
            client = self.client()
            client._read_stdout(io.BytesIO(raw))
            self.assertIsNotNone(client._fatal)
            self.assertEqual(code, client._fatal.code)

        client = self.client()
        frame = {
            "type": "response",
            "id": "request-1",
            "command": "get_state",
            "success": True,
        }
        wire = json.dumps(frame, separators=(",", ":")).encode() + b"\r\n"
        client._read_stdout(io.BytesIO(wire))
        self.assertEqual(frame, client._responses["request-1"])

        client = self.client(WorkerProvider.OH_MY_PI)
        client._dispatch_frame({"type": "ready"})
        client._dispatch_frame({"type": "ready"})
        self.assertEqual("rpc_duplicate_ready", client._fatal.code)

        client = self.client()
        response = {"type": "response", "id": "duplicate"}
        client._dispatch_frame(response)
        client._dispatch_frame(response)
        self.assertEqual("rpc_duplicate_response", client._fatal.code)

        client = rpc.JsonlRpcClient(
            self.launch(),
            working_directory=self.worktree,
            session_directory=self.root / "callback-session",
            frame_callback=lambda _frame: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        client._dispatch_frame({"type": "agent_start"})
        self.assertEqual("rpc_frame_callback_failed", client._fatal.code)

    def test_request_and_write_boundaries_are_fail_closed(self) -> None:
        client = self.client()
        with self.assertRaises(ValueError):
            client.request("")
        self.assert_rpc_error("rpc_process_not_started", lambda: client.request("get_state"))
        self.assert_rpc_error(
            "rpc_command_frame_too_large",
            lambda: client._write_frame({"value": "x" * rpc._MAX_PHYSICAL_FRAME_BYTES}),
        )

        process = Mock()
        process.poll.return_value = None
        process.stdin = io.BytesIO()
        client._process = process
        client._fatal = rpc.JsonlRpcError("reader_failed")
        self.assert_rpc_error("reader_failed", lambda: client.request("get_state"))

        client = self.client()
        process = Mock()
        process.poll.return_value = 9
        process.returncode = 9
        process.stdin = io.BytesIO()
        client._process = process
        self.assert_rpc_error("rpc_process_exited", lambda: client.request("get_state"))

        client = self.client()
        process = Mock()
        process.poll.return_value = None
        process.stdin = io.BytesIO()
        client._process = process
        client._responses["wish-builder-00000001"] = {
            "type": "response",
            "id": "wish-builder-00000001",
            "command": "get_state",
            "success": False,
            "error": "denied",
        }
        self.assert_rpc_error("rpc_command_failed", lambda: client.request("get_state"))

    def test_session_transcripts_are_bounded_and_identity_checked(self) -> None:
        missing = self.root / "missing.jsonl"
        self.assert_rpc_error(
            "provider_session_missing",
            lambda: rpc.JsonlRpcBackendChannel._session_result(missing, HASH_A),
        )

        oversized = self.root / "oversized.jsonl"
        with oversized.open("wb") as stream:
            stream.seek(rpc._MAX_SESSION_BYTES)
            stream.write(b"x")
        self.assert_rpc_error(
            "provider_session_too_large",
            lambda: rpc.JsonlRpcBackendChannel._session_result(oversized, HASH_A),
        )

        too_many = self.root / "too-many.jsonl"
        too_many.write_bytes(b"{}\n" * (rpc._MAX_SESSION_LINES + 1))
        self.assert_rpc_error(
            "provider_session_too_many_lines",
            lambda: rpc.JsonlRpcBackendChannel._session_result(too_many, HASH_A),
        )

        invalid = self.root / "invalid.jsonl"
        invalid.write_bytes(b"\xff\n")
        self.assert_rpc_error(
            "provider_session_invalid_json",
            lambda: rpc.JsonlRpcBackendChannel._session_result(invalid, HASH_A),
        )

        with patch.object(Path, "open", side_effect=OSError("denied")):
            self.assert_rpc_error(
                "provider_session_read_failed",
                lambda: rpc.JsonlRpcBackendChannel._session_result(invalid, HASH_A),
            )

        task = "frozen task"
        digest = rpc._sha256(task.encode())
        transcript = self.root / "valid.jsonl"
        entries = (
            [],
            {"type": "event"},
            {"type": "message", "message": []},
            {
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "frozen "},
                        {"type": "ignored", "text": "not included"},
                        {"type": "text", "text": "task"},
                    ],
                },
            },
            {
                "type": "message",
                "message": {"role": "assistant", "stopReason": "error", "content": "failed"},
            },
        )
        transcript.write_bytes(b"".join(canonical_json_bytes(entry) for entry in entries))
        assistant, found_user = rpc.JsonlRpcBackendChannel._session_result(
            transcript, digest
        )
        self.assertTrue(found_user)
        self.assertEqual("assistant", assistant["role"])
        self.assertEqual(TurnState.FAILED, rpc.JsonlRpcBackendChannel._terminal_state(assistant))

        self.assertEqual("text", rpc.JsonlRpcBackendChannel._message_text({"content": "text"}))
        self.assertIsNone(rpc.JsonlRpcBackendChannel._message_text({"content": []}))
        self.assertIsNone(rpc.JsonlRpcBackendChannel._message_text({"content": 1}))
        self.assertEqual(
            TurnState.CANCELLED,
            rpc.JsonlRpcBackendChannel._terminal_state(
                {"nested": [{"stop_reason": "ABORTED"}]}
            ),
        )
        self.assertEqual(TurnState.DONE, rpc.JsonlRpcBackendChannel._terminal_state({}))

    def test_persisted_state_and_observation_helpers_reject_ambiguity(self) -> None:
        nonregular = self.root / "nonregular"
        (nonregular / "channel-state.json").mkdir(parents=True)
        self.assert_rpc_error(
            "provider_state_not_regular",
            lambda: rpc.JsonlRpcBackendChannel(self.config("nonregular")),
        )

        invalid_state = self.root / "invalid-state"
        invalid_state.mkdir()
        (invalid_state / "channel-state.json").write_text("{", encoding="utf-8")
        self.assert_rpc_error(
            "provider_state_invalid",
            lambda: rpc.JsonlRpcBackendChannel(self.config("invalid-state")),
        )

        noncanonical = self.root / "noncanonical"
        noncanonical.mkdir()
        value = {"schema_version": 1, "provider": "pi", "operations": {}}
        (noncanonical / "channel-state.json").write_text(json.dumps(value), encoding="utf-8")
        self.assert_rpc_error(
            "provider_state_invalid",
            lambda: rpc.JsonlRpcBackendChannel(self.config("noncanonical")),
        )

        valid_state = self.root / "valid-state"
        valid_state.mkdir()
        (valid_state / "channel-state.json").write_bytes(canonical_json_bytes(value))
        channel = rpc.JsonlRpcBackendChannel(
            self.config("valid-state"), clock=lambda: OBSERVED_AT
        )
        self.addCleanup(channel.close)
        self.assertEqual(EffectStatus.ABSENT, channel.inspect_reservation("missing").status)
        self.assertEqual(EffectStatus.ABSENT, channel.inspect_turn("missing").status)
        self.assertIsNone(channel._reservation_observation())
        self.assertIsNone(channel.process_id)

        channel._state["operations"]["wrong-kind"] = {"kind": "reservation"}
        self.assertEqual(
            EffectStatus.UNKNOWN, channel.inspect_turn("wrong-kind").status
        )
        channel._state["operations"]["wrong-kind"] = {"kind": "send"}
        self.assertEqual(
            EffectStatus.UNKNOWN, channel.inspect_reservation("wrong-kind").status
        )

        channel._state["operations"]["collision"] = {
            "kind": "send",
            "command_hash": HASH_A,
        }
        collision = channel._existing("collision", HASH_B, "reservation")
        self.assertEqual(
            EffectStatus.UNKNOWN,
            rpc.JsonlRpcBackendChannel._channel_observation(collision).status,
        )
        collision = channel._existing("collision", HASH_B, "send")
        self.assertEqual(
            EffectStatus.UNKNOWN,
            rpc.JsonlRpcBackendChannel._turn_observation(collision).status,
        )
        self.assertIs(
            channel._state["operations"]["collision"],
            channel._existing("collision", HASH_A, "send"),
        )

        with self.assertRaises(rpc.JsonlRpcError):
            channel._operation("absent")
        with self.assertRaises(rpc.JsonlRpcError):
            channel._channel_observation({})
        with self.assertRaises(rpc.JsonlRpcError):
            channel._turn_observation({})
        with self.assertRaises(TypeError):
            channel._validate_operation_id("")
        with self.assertRaises(TypeError):
            channel._require_effect(None, ReserveChannel)

        shell = object.__new__(PreparedEffect)
        object.__setattr__(shell, "command", object())
        with self.assertRaises(TypeError):
            channel._require_effect(shell, ReserveChannel)

        cleanup_state = self.root / "cleanup-state"
        cleanup_state.mkdir()
        cleanup = rpc.JsonlRpcBackendChannel(self.config("cleanup-state"))
        self.assertEqual(
            ("provider_process_tree", "provider_session_state"),
            cleanup.cleanup(remove_durable_state=True),
        )
        self.assertFalse(cleanup_state.exists())

    def test_client_lifecycle_handshake_and_transport_fail_closed(self) -> None:
        self.assertRegex(rpc._utc_now(), r"Z$")
        client = self.client()
        self.assertIsNone(client.returncode)
        client._stderr.extend(b"bad\xff")
        self.assertEqual("bad\ufffd", client.stderr_text)

        process = Mock()
        process.poll.return_value = None
        process.pid = 123
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO()
        process.stderr = io.BytesIO()
        thread = Mock()
        with (
            patch.object(rpc.os, "name", "posix"),
            patch.object(rpc.subprocess, "Popen", return_value=process),
            patch.object(rpc.threading, "Thread", return_value=thread),
            patch.object(
                client,
                "request",
                return_value={"data": {"sessionId": "native-1"}},
            ),
        ):
            self.assertEqual("native-1", client.start()["sessionId"])
            self.assert_rpc_error("rpc_process_already_started", client.start)

        failed = self.client()
        with patch.object(rpc.subprocess, "Popen", side_effect=OSError("denied")):
            self.assert_rpc_error("rpc_process_start_failed", failed.start)

        def start_case(
            name: str,
            provider: WorkerProvider,
            *,
            ready: dict[str, object] | None = None,
            responses: tuple[dict[str, object], ...] = (),
            code: str,
        ) -> None:
            candidate = self.client(provider)
            candidate_process = Mock()
            candidate_process.poll.return_value = 0
            candidate_process.stdin = io.BytesIO()
            candidate_process.stdout = io.BytesIO()
            candidate_process.stderr = io.BytesIO()
            with (
                self.subTest(name=name),
                patch.object(rpc.subprocess, "Popen", return_value=candidate_process),
                patch.object(rpc.threading, "Thread", return_value=Mock()),
                patch.object(candidate, "_wait_ready", return_value=ready),
                patch.object(candidate, "request", side_effect=responses),
            ):
                self.assert_rpc_error(code, candidate.start)

        start_case(
            "unsupported",
            WorkerProvider.OH_MY_PI,
            ready={"protocolVersion": 1, "supportedProtocolVersions": [1]},
            code="omp_rpc_v2_not_supported",
        )
        supported = {
            "protocolVersion": 1,
            "supportedProtocolVersions": [1, 2],
            "maxFrameBytes": rpc._MAX_PHYSICAL_FRAME_BYTES,
            "maxReassembledFrameBytes": rpc._MAX_REASSEMBLED_FRAME_BYTES,
        }
        start_case(
            "negotiation",
            WorkerProvider.OH_MY_PI,
            ready=supported,
            responses=({"data": {}},),
            code="omp_rpc_v2_negotiation_failed",
        )
        start_case(
            "state",
            WorkerProvider.PI,
            responses=({"data": {}},),
            code="rpc_get_state_invalid",
        )

        timeout = self.client()
        live = Mock()
        live.poll.return_value = None
        live.stdin = io.BytesIO()
        timeout._process = live
        timeout._response_timeout = 0
        with patch.object(timeout, "_write_frame"):
            self.assert_rpc_error(
                "rpc_response_timeout", lambda: timeout.request("get_state")
            )

        wait = self.client(WorkerProvider.OH_MY_PI)
        wait._fatal = rpc.JsonlRpcError("reader_failed")
        self.assert_rpc_error("reader_failed", wait._wait_ready)
        wait._fatal = None
        wait._process = Mock(poll=Mock(return_value=1))
        self.assert_rpc_error("rpc_process_exited_before_ready", wait._wait_ready)
        wait._process = Mock(poll=Mock(return_value=None))
        wait._handshake_timeout = 0
        self.assert_rpc_error("rpc_ready_timeout", wait._wait_ready)

        unavailable = self.client()
        self.assert_rpc_error(
            "rpc_stdin_unavailable", lambda: unavailable._write_frame({"ok": True})
        )
        unavailable._process = Mock(poll=Mock(return_value=None))
        unavailable._process.stdin = Mock()
        unavailable._process.stdin.write.side_effect = BrokenPipeError("closed")
        self.assert_rpc_error(
            "rpc_write_failed", lambda: unavailable._write_frame({"ok": True})
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

    def test_client_reader_and_process_termination_paths_are_bounded(self) -> None:
        omp = self.client(WorkerProvider.OH_MY_PI)
        metadata = self._chunk(
            index=0,
            count=2,
            byte_length=rpc._MAX_PHYSICAL_FRAME_BYTES,
            raw=b"x",
        )
        omp._read_stdout(
            io.BytesIO(json.dumps(metadata, separators=(",", ":")).encode() + b"\n")
        )
        self.assertIsNone(omp._fatal)

        closed_reader = self.client()
        closed_reader._closed = True
        closed_reader._read_stdout(io.BytesIO(b"{\n"))
        self.assertIsNone(closed_reader._fatal)

        callback = rpc.JsonlRpcClient(
            self.launch(),
            working_directory=self.worktree,
            session_directory=self.root / "callback-preset",
            frame_callback=lambda _frame: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        callback._fatal = rpc.JsonlRpcError("first_failure")
        callback._dispatch_frame({"type": "agent_start"})
        self.assertEqual("first_failure", callback._fatal.code)

        failing_reader = Mock()
        failing_reader.read.side_effect = OSError("denied")
        omp._read_stderr(failing_reader)
        captured = self.client()
        captured._read_stderr(io.BytesIO(b"stderr"))
        self.assertEqual("stderr", captured.stderr_text)
        bounded = self.client()
        bounded._stderr.extend(b"x" * rpc._STDERR_LIMIT)
        bounded._read_stderr(io.BytesIO(b"ignored"))
        self.assertEqual(rpc._STDERR_LIMIT, len(bounded._stderr))

        exited = Mock(poll=Mock(return_value=0))
        rpc.JsonlRpcClient._terminate_process_tree(exited)
        normal = Mock(poll=Mock(return_value=None), pid=42)
        with (
            patch.object(rpc.os, "name", "posix"),
            patch.object(rpc.os, "killpg", create=True) as killpg,
        ):
            rpc.JsonlRpcClient._terminate_process_tree(normal)
        killpg.assert_called_once_with(42, rpc.signal.SIGTERM)

        fallback = Mock(poll=Mock(return_value=None), pid=43)
        fallback.wait.side_effect = (OSError("first"), None)
        with patch.object(rpc.os, "name", "nt"):
            rpc.JsonlRpcClient._terminate_process_tree(fallback)
        fallback.kill.assert_called_once_with()

        abandoned = Mock(poll=Mock(return_value=None), pid=44)
        abandoned.wait.side_effect = OSError("wait")
        with (
            patch.object(rpc.os, "name", "posix"),
            patch.object(rpc.os, "killpg", side_effect=OSError("signal"), create=True),
            patch.object(rpc.signal, "SIGKILL", 9, create=True),
        ):
            rpc.JsonlRpcClient._terminate_process_tree(abandoned)

    def test_channel_admission_send_and_cancel_edges_fail_closed(self) -> None:
        reserve, send, cancel = self.commands()
        reserve_effect = self.effect(reserve, EffectOperation.RESERVE_CHANNEL)

        mismatch = rpc.JsonlRpcBackendChannel(
            self.config("mismatch"), clock=lambda: OBSERVED_AT
        )
        self.addCleanup(mismatch.close)
        wrong_reserve = replace(reserve, provider=WorkerProvider.OH_MY_PI)
        wrong_effect = self.effect(
            wrong_reserve, EffectOperation.RESERVE_CHANNEL, 2
        )
        observed = mismatch.reserve(wrong_effect)
        self.assertIn("channel_capability_mismatch", observed.evidence)
        self.assertEqual(observed, mismatch.reserve(wrong_effect))

        occupied = rpc.JsonlRpcBackendChannel(
            self.config("occupied"), clock=lambda: OBSERVED_AT
        )
        self.addCleanup(occupied.close)
        occupied._state["reservation"] = "OTHER"
        self.assertIn(
            "attempt_already_has_channel", occupied.reserve(reserve_effect).evidence
        )

        failing_client = self.client()
        with patch.object(
            failing_client, "start", side_effect=rpc.JsonlRpcError("start_failed")
        ):
            failed = rpc.JsonlRpcBackendChannel(
                self.config("failed"),
                clock=lambda: OBSERVED_AT,
                client_factory=lambda *_args, **_kwargs: failing_client,
            )
            self.addCleanup(failed.close)
            self.assertIn("start_failed", failed.reserve(reserve_effect).evidence)

        invalid = rpc.JsonlRpcBackendChannel(
            self.config("invalid-factory"),
            client_factory=lambda *_args, **_kwargs: object(),
        )
        self.addCleanup(invalid.close)
        with self.assertRaises(TypeError):
            invalid._ensure_client_locked()

        provider = self.client()
        absolute_session = self.root / "provider-session.jsonl"
        absolute_session.write_text("", encoding="utf-8")
        with patch.object(
            provider,
            "start",
            return_value={
                "sessionId": "native-1",
                "sessionFile": str(absolute_session),
            },
        ):
            channel = rpc.JsonlRpcBackendChannel(
                self.config("live"),
                clock=lambda: OBSERVED_AT,
                client_factory=lambda *_args, **_kwargs: provider,
            )
            self.addCleanup(channel.close)
            self.assertEqual(EffectStatus.APPLIED, channel.reserve(reserve_effect).status)
        self.assertEqual(capabilities(WorkerProvider.PI), channel.probe())
        self.assertEqual(channel.state_path, channel.state_path)

        unreserved = rpc.JsonlRpcBackendChannel(
            self.config("unreserved"), clock=lambda: OBSERVED_AT
        )
        self.addCleanup(unreserved.close)
        send_effect = self.effect(send, EffectOperation.SEND_TASK_PACKET, 3)
        self.assertIn("channel_not_reserved", unreserved.send(send_effect).evidence)

        oversized_packet = "x" * (capabilities(WorkerProvider.PI).max_task_packet_bytes + 1)
        oversized = replace(
            send,
            operation_id="SEND-OVERSIZED",
            task_packet=oversized_packet,
            task_packet_digest=rpc._sha256(oversized_packet.encode()),
        )
        self.assertIn(
            "task_packet_exceeds_capability",
            channel.send(
                self.effect(oversized, EffectOperation.SEND_TASK_PACKET, 4)
            ).evidence,
        )

        channel._state["active_send"] = "OTHER"
        active = replace(send, operation_id="SEND-ACTIVE")
        self.assertIn(
            "attempt_already_has_turn",
            channel.send(self.effect(active, EffectOperation.SEND_TASK_PACKET, 5)).evidence,
        )
        channel._state.pop("active_send")

        provider._process = Mock(poll=Mock(return_value=None), pid=99)

        def request(command: str, **_payload):
            if command == "get_state":
                return {"data": {"sessionId": "native-1"}}
            raise rpc.JsonlRpcError("prompt_failed")

        with patch.object(provider, "request", side_effect=request):
            sent = channel.send(send_effect)
        self.assertIn("provider_send_ambiguous:prompt_failed", sent.evidence)
        self.assertEqual(EffectStatus.UNKNOWN, channel.send(send_effect).status)
        channel._operation(send.operation_id)["observation"] = channel._applied_turn(
            send.operation_id,
            TurnState.RUNNING,
            send_effect.command_hash,
        ).to_primitive()
        self.assertEqual(TurnState.RUNNING, channel.send(send_effect).state)

        channel._state.pop("active_send", None)
        channel._client = None
        ensure_send = replace(send, operation_id="SEND-ENSURE")
        ensure_effect = self.effect(
            ensure_send, EffectOperation.SEND_TASK_PACKET, 6
        )
        with patch.object(
            provider, "start", side_effect=rpc.JsonlRpcError("restart_failed")
        ):
            ambiguous = channel.send(ensure_effect)
        self.assertIn("provider_send_ambiguous:restart_failed", ambiguous.evidence)

        no_turn = rpc.JsonlRpcBackendChannel(
            self.config("no-turn"), clock=lambda: OBSERVED_AT
        )
        self.addCleanup(no_turn.close)
        cancel_effect = self.effect(cancel, EffectOperation.CANCEL_TURN, 7)
        missing = no_turn.cancel(cancel_effect)
        self.assertIn("turn_not_found", missing.evidence)
        self.assertEqual(missing, no_turn.cancel(cancel_effect))

        invalid_cancel = replace(cancel, operation_id="CANCEL-INVALID")
        invalid_cancel_effect = self.effect(
            invalid_cancel, EffectOperation.CANCEL_TURN, 8
        )
        channel._state["active_send"] = send.operation_id
        channel._operation(send.operation_id)["command"] = {}
        self.assertIn("turn_not_found", channel.cancel(invalid_cancel_effect).evidence)

        cancel_channel = rpc.JsonlRpcBackendChannel(
            self.config("cancel-paths"), clock=lambda: OBSERVED_AT
        )
        self.addCleanup(cancel_channel.close)
        cancel_channel._put_operation(
            send.operation_id,
            "send",
            send_effect.command_hash,
            TurnObservation(
                operation_id=send.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=OBSERVED_AT,
                state=TurnState.RUNNING,
                effect_digest=HASH_A,
                attempt_id=send.attempt_id,
                channel_id=send.channel_id,
                message_id=send.message_id,
                turn_id=send.turn_id,
            ).to_primitive(),
            command={
                "attempt_id": send.attempt_id,
                "channel_id": send.channel_id,
                "message_id": send.message_id,
                "turn_id": send.turn_id,
            },
        )
        cancel_channel._state["active_send"] = send.operation_id
        offline_cancel = replace(cancel, operation_id="CANCEL-OFFLINE")
        offline_effect = self.effect(
            offline_cancel, EffectOperation.CANCEL_TURN, 9
        )
        terminal = cancel_channel._applied_turn(
            send.operation_id,
            TurnState.FAILED,
            send_effect.command_hash,
        )
        with patch.object(
            cancel_channel, "_reconcile_session_locked", return_value=terminal
        ):
            self.assertEqual(TurnState.FAILED, cancel_channel.cancel(offline_effect).state)

        live_provider = self.client()
        live_provider._process = Mock(poll=Mock(return_value=None), pid=101)
        cancel_channel._client = live_provider
        failing_cancel = replace(cancel, operation_id="CANCEL-FAILING")
        failing_effect = self.effect(
            failing_cancel, EffectOperation.CANCEL_TURN, 10
        )
        with patch.object(
            live_provider,
            "request",
            side_effect=rpc.JsonlRpcError("abort_failed"),
        ):
            failed_cancel = cancel_channel.cancel(failing_effect)
        self.assertIn("provider_cancel_ambiguous:abort_failed", failed_cancel.evidence)

        accepted_cancel = replace(cancel, operation_id="CANCEL-ACCEPTED")
        accepted_effect = self.effect(
            accepted_cancel, EffectOperation.CANCEL_TURN, 11
        )
        with patch.object(live_provider, "request", return_value={}):
            self.assertEqual(
                TurnState.CANCELLED,
                cancel_channel.cancel(accepted_effect).state,
            )
        live_provider._process.poll.return_value = 0

    def test_channel_reconciliation_and_frame_guards_are_deterministic(self) -> None:
        channel = rpc.JsonlRpcBackendChannel(
            self.config("reconcile"), clock=lambda: OBSERVED_AT
        )
        self.addCleanup(channel.close)
        channel._on_frame({"type": "agent_start"})
        channel._state["active_send"] = "MISSING"
        channel._on_frame({"type": "agent_start"})

        _, send, _ = self.commands("RECONCILE")
        pending = TurnObservation(
            operation_id=send.operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=OBSERVED_AT,
            state=TurnState.UNKNOWN,
            evidence=("pending",),
        )
        channel._put_operation(
            send.operation_id,
            "send",
            HASH_A,
            pending.to_primitive(),
            command={
                "attempt_id": send.attempt_id,
                "channel_id": send.channel_id,
                "message_id": send.message_id,
                "turn_id": send.turn_id,
            },
        )
        channel._state["active_send"] = send.operation_id
        channel._on_frame({"type": "agent_start"})
        self.assertEqual(TurnState.RUNNING, channel.inspect_turn(send.operation_id).state)
        done = channel._applied_turn(send.operation_id, TurnState.DONE, HASH_A)
        self.assertIsNotNone(done.result_digest)
        channel._operation(send.operation_id)["observation"] = done.to_primitive()
        self.assertEqual(done, channel._reconcile_session_locked(send.operation_id))

        channel._operation(send.operation_id)["observation"] = pending.to_primitive()
        channel._state.pop("session_file", None)
        channel._state.pop("task_packet_text_digest", None)
        self.assertEqual(
            EffectStatus.UNKNOWN,
            channel._reconcile_session_locked(send.operation_id).status,
        )

        missing = self.root / "missing-session.jsonl"
        channel._state["session_file"] = str(missing)
        channel._state["task_packet_text_digest"] = HASH_A
        failed = channel._reconcile_session_locked(send.operation_id)
        self.assertIn("session_reconcile_failed:provider_session_missing", failed.evidence)

        leading_assistant = self.root / "leading-assistant.jsonl"
        leading_assistant.write_bytes(
            canonical_json_bytes(
                {
                    "type": "message",
                    "message": {"role": "assistant", "content": "early"},
                }
            )
        )
        assistant, found_user = channel._session_result(leading_assistant, HASH_A)
        self.assertIsNone(assistant)
        self.assertFalse(found_user)

        live = self.client()
        live._process = Mock(poll=Mock(return_value=None), pid=102)
        channel._client = live
        with patch.object(live, "request", return_value={"data": []}):
            self.assert_rpc_error("rpc_get_state_invalid", channel._ensure_client_locked)
        live._process.poll.return_value = 0

        resumable_file = self.root / "resumable.jsonl"
        resumable_file.write_text("", encoding="utf-8")
        resumed_client = self.client()
        resumed = rpc.JsonlRpcBackendChannel(
            self.config("resumed"),
            clock=lambda: OBSERVED_AT,
            client_factory=lambda *_args, **_kwargs: resumed_client,
        )
        self.addCleanup(resumed.close)
        resumed._state["session_file"] = str(resumable_file)
        with patch.object(
            resumed_client,
            "start",
            return_value={"sessionId": "resumed-native"},
        ) as start:
            data = resumed._ensure_client_locked()
        self.assertEqual("resumed-native", data["sessionId"])
        start.assert_called_once_with(session_file=str(resumable_file))

        with patch.object(Path, "unlink", side_effect=OSError("denied")):
            channel._save_state()


if __name__ == "__main__":
    unittest.main()
