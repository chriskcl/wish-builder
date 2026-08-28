from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from wish_builder.adapters.providers import jsonl_rpc as rpc
from wish_builder.contracts import canonical_json_bytes
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


if __name__ == "__main__":
    unittest.main()
