"""Direct Pi and Oh My Pi JSONL-RPC backend adapters.

The provider process runs in one attempt worktree while all adapter metadata and
provider session files live in a separate control directory.  The adapter never
uses Trellis's operation runtime: Trellis owns task records, while this module
owns one Wish Builder worker channel.

Every mutation is already protected by a durable Wish Builder request before it
reaches this port.  Local state records the command hash before writing to the
provider so a coordinator restart can inspect the session transcript without
blindly sending the task again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from wish_builder.contracts import canonical_json_bytes
from wish_builder.contracts.manifest_v2 import WorkerProvider
from wish_builder.contracts.runtime import EffectStatus
from wish_builder.services.ports import (
    BackendCapabilities,
    CancelTurn,
    ChannelObservation,
    PreparedEffect,
    ReserveChannel,
    SendTaskPacket,
    TurnObservation,
    TurnState,
)

_STATE_SCHEMA_VERSION = 1
_MAX_PHYSICAL_FRAME_BYTES = 1_048_576
_MAX_REASSEMBLED_FRAME_BYTES = 64 * 1_048_576
_MAX_CHUNK_BYTES = 256 * 1_024
_MAX_SESSION_BYTES = 64 * 1_048_576
_MAX_SESSION_LINES = 100_000
_STDERR_LIMIT = 64 * 1_024


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _effect_digest(operation: str, command_hash: str) -> str:
    return _sha256(
        canonical_json_bytes(
            {"command_hash": command_hash, "operation": operation}
        )
    )


def _token_from_digest(prefix: str, digest: str) -> str:
    return f"{prefix}-{digest.removeprefix('sha256:')[:32]}"


def _is_object(value: object) -> bool:
    return type(value) is dict and all(type(key) is str for key in value)


class JsonlRpcError(RuntimeError):
    """Stable provider transport or state error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


class JsonlRpcProtocol(StrEnum):
    PI = "pi-rpc-jsonl-stdio"
    OH_MY_PI_V2 = "omp-rpc-v2-jsonl-stdio"


@dataclass(frozen=True, slots=True)
class JsonlRpcLaunch:
    """Exact executable prefix and provider-specific RPC behavior."""

    provider: WorkerProvider
    protocol: JsonlRpcProtocol
    command_prefix: tuple[str, ...]
    sdk_name: str
    sdk_version: str
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.provider not in {WorkerProvider.PI, WorkerProvider.OH_MY_PI}:
            raise ValueError("JSONL RPC supports only Pi and Oh My Pi")
        expected = {
            WorkerProvider.PI: JsonlRpcProtocol.PI,
            WorkerProvider.OH_MY_PI: JsonlRpcProtocol.OH_MY_PI_V2,
        }[self.provider]
        if self.protocol is not expected:
            raise ValueError("provider and JSONL protocol do not match")
        if (
            type(self.command_prefix) is not tuple
            or not self.command_prefix
            or not all(type(item) is str and item for item in self.command_prefix)
        ):
            raise TypeError("command_prefix must be a non-empty tuple of strings")
        if not Path(self.command_prefix[0]).is_absolute():
            raise ValueError("the provider executable must be an absolute path")
        if not all(type(value) is str and value for value in (self.sdk_name, self.sdk_version)):
            raise ValueError("SDK name and version must be non-empty")
        if type(self.extra_args) is not tuple or not all(
            type(item) is str and item for item in self.extra_args
        ):
            raise TypeError("extra_args must be a tuple of non-empty strings")

    def argv(self, session_directory: Path, session_file: str | None = None) -> tuple[str, ...]:
        args = [*self.command_prefix, "--mode", "rpc"]
        if session_file is None:
            args.extend(("--session-dir", str(session_directory)))
        elif self.provider is WorkerProvider.PI:
            args.extend(("--session", session_file, "--session-dir", str(session_directory)))
        else:
            args.extend(("--resume", session_file, "--session-dir", str(session_directory)))
        args.extend(self.extra_args)
        return tuple(args)


FrameCallback = Callable[[dict[str, object]], None]


@dataclass(slots=True)
class _PendingChunks:
    chunk_id: str
    count: int
    byte_length: int
    next_index: int
    chunks: list[bytes]
    received_bytes: int = 0


class _OmpFrameDecoder:
    """Strict reassembly for Oh My Pi RPC protocol v2."""

    def __init__(self) -> None:
        self._pending: _PendingChunks | None = None

    def push(self, value: dict[str, object]) -> dict[str, object] | None:
        if value.get("type") != "rpc_chunk":
            if self._pending is not None:
                raise JsonlRpcError("rpc_chunk_interrupted")
            return value
        chunk_id = value.get("chunkId")
        index = value.get("index")
        count = value.get("count")
        byte_length = value.get("byteLength")
        data = value.get("data")
        if (
            type(chunk_id) is not str
            or not 1 <= len(chunk_id) <= 128
            or type(index) is not int
            or type(count) is not int
            or type(byte_length) is not int
            or type(data) is not str
            or index < 0
            or count < 2
            or count > _MAX_REASSEMBLED_FRAME_BYTES // _MAX_CHUNK_BYTES
            or index >= count
            or byte_length < _MAX_PHYSICAL_FRAME_BYTES
            or byte_length > _MAX_REASSEMBLED_FRAME_BYTES
        ):
            raise JsonlRpcError("invalid_rpc_chunk_metadata")
        try:
            raw = base64.b64decode(data.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise JsonlRpcError("invalid_rpc_chunk_data") from exc
        if not raw or len(raw) > _MAX_CHUNK_BYTES:
            raise JsonlRpcError("invalid_rpc_chunk_data")
        if self._pending is None:
            if index != 0:
                raise JsonlRpcError("rpc_chunk_sequence_start")
            self._pending = _PendingChunks(chunk_id, count, byte_length, 0, [])
        pending = self._pending
        if (
            pending.chunk_id != chunk_id
            or pending.count != count
            or pending.byte_length != byte_length
            or pending.next_index != index
        ):
            raise JsonlRpcError("rpc_chunk_sequence_mismatch")
        pending.chunks.append(raw)
        pending.received_bytes += len(raw)
        pending.next_index += 1
        if pending.received_bytes > pending.byte_length:
            raise JsonlRpcError("rpc_chunk_length_exceeded")
        if pending.next_index < pending.count:
            return None
        self._pending = None
        if pending.received_bytes != pending.byte_length:
            raise JsonlRpcError("rpc_chunk_length_mismatch")
        try:
            decoded = b"".join(pending.chunks).decode("utf-8", errors="strict")
            result = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JsonlRpcError("invalid_reassembled_rpc_frame") from exc
        if not _is_object(result):
            raise JsonlRpcError("rpc_frame_not_object")
        return result


class JsonlRpcClient:
    """One bounded, correlated provider subprocess."""

    def __init__(
        self,
        launch: JsonlRpcLaunch,
        *,
        working_directory: Path,
        session_directory: Path,
        environment: Mapping[str, str] | None = None,
        frame_callback: FrameCallback | None = None,
        handshake_timeout_seconds: float = 30.0,
        response_timeout_seconds: float = 30.0,
    ) -> None:
        if type(launch) is not JsonlRpcLaunch:
            raise TypeError("launch must be a JsonlRpcLaunch")
        for path, name in (
            (working_directory, "working_directory"),
            (session_directory, "session_directory"),
        ):
            if not isinstance(path, Path) or not path.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        if not working_directory.is_dir():
            raise ValueError("working_directory must exist")
        if environment is not None and (
            not isinstance(environment, Mapping)
            or not all(type(k) is str and type(v) is str for k, v in environment.items())
        ):
            raise TypeError("environment must map strings to strings")
        if frame_callback is not None and not callable(frame_callback):
            raise TypeError("frame_callback must be callable or null")
        for value, name in (
            (handshake_timeout_seconds, "handshake_timeout_seconds"),
            (response_timeout_seconds, "response_timeout_seconds"),
        ):
            if type(value) not in {int, float} or isinstance(value, bool) or float(value) <= 0:
                raise ValueError(f"{name} must be positive")
        self.launch = launch
        self.working_directory = working_directory
        self.session_directory = session_directory
        self._environment = None if environment is None else dict(environment)
        self._callback = frame_callback
        self._handshake_timeout = float(handshake_timeout_seconds)
        self._response_timeout = float(response_timeout_seconds)
        self._condition = threading.Condition(threading.RLock())
        self._write_lock = threading.Lock()
        self._responses: dict[str, dict[str, object]] = {}
        self._ready: dict[str, object] | None = None
        self._fatal: JsonlRpcError | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr = bytearray()
        self._closed = False
        self._request_number = 0
        self._decoder = _OmpFrameDecoder()

    @property
    def process_id(self) -> int | None:
        process = self._process
        return None if process is None or process.poll() is not None else process.pid

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.poll()

    @property
    def stderr_text(self) -> str:
        with self._condition:
            return bytes(self._stderr).decode("utf-8", errors="replace")

    @property
    def is_alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None and not self._closed

    def start(self, *, session_file: str | None = None) -> dict[str, object]:
        with self._condition:
            if self._process is not None:
                raise JsonlRpcError("rpc_process_already_started")
        self.session_directory.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        if self._environment is not None:
            environment.update(self._environment)
        popen_options: dict[str, object] = {
            "cwd": str(self.working_directory),
            "env": environment,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
        }
        if os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_options["start_new_session"] = True
        try:
            process = subprocess.Popen(  # noqa: S603 - exact admitted command
                self.launch.argv(self.session_directory, session_file),
                **popen_options,
            )
        except OSError as exc:
            raise JsonlRpcError("rpc_process_start_failed", str(exc)) from exc
        self._process = process
        assert process.stdout is not None and process.stderr is not None
        self._reader = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout,),
            name=f"wish-builder-{self.launch.provider.value}-stdout",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr,),
            name=f"wish-builder-{self.launch.provider.value}-stderr",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()
        if self.launch.protocol is JsonlRpcProtocol.OH_MY_PI_V2:
            ready = self._wait_ready()
            supported = ready.get("supportedProtocolVersions")
            if (
                ready.get("protocolVersion") != 1
                or type(supported) is not list
                or 2 not in supported
                or ready.get("maxFrameBytes") != _MAX_PHYSICAL_FRAME_BYTES
                or ready.get("maxReassembledFrameBytes") != _MAX_REASSEMBLED_FRAME_BYTES
            ):
                self.close()
                raise JsonlRpcError("omp_rpc_v2_not_supported")
            negotiated = self.request(
                "negotiate_protocol", protocolVersion=2
            )
            data = negotiated.get("data")
            if not _is_object(data) or data.get("protocolVersion") != 2:
                self.close()
                raise JsonlRpcError("omp_rpc_v2_negotiation_failed")
        state = self.request("get_state")
        data = state.get("data")
        if not _is_object(data) or type(data.get("sessionId")) is not str:
            self.close()
            raise JsonlRpcError("rpc_get_state_invalid")
        return data

    def request(self, command: str, **payload: object) -> dict[str, object]:
        if type(command) is not str or not command:
            raise ValueError("command must be non-empty")
        with self._condition:
            if self._closed or self._process is None:
                raise JsonlRpcError("rpc_process_not_started")
            self._request_number += 1
            request_id = f"wish-builder-{self._request_number:08d}"
        frame = {"id": request_id, "type": command, **payload}
        self._write_frame(frame)
        deadline = time.monotonic() + self._response_timeout
        with self._condition:
            while request_id not in self._responses:
                if self._fatal is not None:
                    raise self._fatal
                process = self._process
                if process is None or process.poll() is not None:
                    raise JsonlRpcError(
                        "rpc_process_exited",
                        f"returncode={None if process is None else process.returncode}",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JsonlRpcError("rpc_response_timeout", command)
                self._condition.wait(remaining)
            response = self._responses.pop(request_id)
        if (
            response.get("type") != "response"
            or response.get("command") != command
            or response.get("success") is not True
        ):
            detail = response.get("error")
            raise JsonlRpcError(
                "rpc_command_failed",
                detail if type(detail) is str else command,
            )
        return response

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            process = self._process
            self._condition.notify_all()
        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _wait_ready(self) -> dict[str, object]:
        deadline = time.monotonic() + self._handshake_timeout
        with self._condition:
            while self._ready is None:
                if self._fatal is not None:
                    raise self._fatal
                process = self._process
                if process is None or process.poll() is not None:
                    raise JsonlRpcError("rpc_process_exited_before_ready")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JsonlRpcError("rpc_ready_timeout")
                self._condition.wait(remaining)
            return dict(self._ready)

    def _write_frame(self, frame: dict[str, object]) -> None:
        raw = canonical_json_bytes(frame)
        if len(raw) > _MAX_PHYSICAL_FRAME_BYTES:
            raise JsonlRpcError("rpc_command_frame_too_large")
        process = self._process
        if process is None or process.stdin is None:
            raise JsonlRpcError("rpc_stdin_unavailable")
        try:
            with self._write_lock:
                process.stdin.write(raw)
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise JsonlRpcError("rpc_write_failed", str(exc)) from exc

    def _read_stdout(self, stream: BinaryIO) -> None:
        try:
            while True:
                raw = stream.readline(_MAX_PHYSICAL_FRAME_BYTES + 1)
                if not raw:
                    break
                if len(raw) > _MAX_PHYSICAL_FRAME_BYTES or not raw.endswith(b"\n"):
                    raise JsonlRpcError("rpc_physical_frame_too_large")
                line = raw[:-1]
                if line.endswith(b"\r"):
                    line = line[:-1]
                if not line:
                    raise JsonlRpcError("rpc_blank_frame")
                try:
                    value = json.loads(line.decode("utf-8", errors="strict"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise JsonlRpcError("rpc_invalid_json") from exc
                if not _is_object(value):
                    raise JsonlRpcError("rpc_frame_not_object")
                frame = (
                    self._decoder.push(value)
                    if self.launch.protocol is JsonlRpcProtocol.OH_MY_PI_V2
                    else value
                )
                if frame is not None:
                    self._dispatch_frame(frame)
        except JsonlRpcError as exc:
            with self._condition:
                if not self._closed and self._fatal is None:
                    self._fatal = exc
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._condition.notify_all()

    def _dispatch_frame(self, frame: dict[str, object]) -> None:
        callback = self._callback
        with self._condition:
            if frame.get("type") == "ready":
                if self._ready is not None:
                    self._fatal = JsonlRpcError("rpc_duplicate_ready")
                else:
                    self._ready = dict(frame)
            elif frame.get("type") == "response" and type(frame.get("id")) is str:
                request_id = frame["id"]
                assert type(request_id) is str
                if request_id in self._responses:
                    self._fatal = JsonlRpcError("rpc_duplicate_response", request_id)
                else:
                    self._responses[request_id] = dict(frame)
            self._condition.notify_all()
        if callback is not None and frame.get("type") not in {"ready", "response"}:
            try:
                callback(dict(frame))
            except Exception:
                with self._condition:
                    if self._fatal is None:
                        self._fatal = JsonlRpcError("rpc_frame_callback_failed")
                        self._condition.notify_all()

    def _read_stderr(self, stream: BinaryIO) -> None:
        while True:
            try:
                raw = stream.read(4096)
            except OSError:
                return
            if not raw:
                return
            with self._condition:
                remaining = _STDERR_LIMIT - len(self._stderr)
                if remaining > 0:
                    self._stderr.extend(raw[:remaining])

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(  # noqa: S603,S607 - exact captured numeric PID
                    ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass


@dataclass(frozen=True, slots=True)
class JsonlRpcBackendConfig:
    capabilities: BackendCapabilities
    launch: JsonlRpcLaunch
    working_directory: Path
    state_directory: Path
    environment: tuple[tuple[str, str], ...] = ()
    handshake_timeout_seconds: float = 30.0
    response_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if type(self.capabilities) is not BackendCapabilities:
            raise TypeError("capabilities must be BackendCapabilities")
        if type(self.launch) is not JsonlRpcLaunch:
            raise TypeError("launch must be a JsonlRpcLaunch")
        if self.capabilities.provider is not self.launch.provider:
            raise ValueError("capabilities and launch provider do not match")
        for value, name in (
            (self.working_directory, "working_directory"),
            (self.state_directory, "state_directory"),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute Path")
        if not self.working_directory.is_dir():
            raise ValueError("working_directory must exist")
        try:
            self.state_directory.resolve(strict=False).relative_to(
                self.working_directory.resolve(strict=True)
            )
        except ValueError:
            pass
        else:
            raise ValueError("provider state must be outside the attempt worktree")
        if type(self.environment) is not tuple or not all(
            type(item) is tuple
            and len(item) == 2
            and all(type(part) is str for part in item)
            for item in self.environment
        ):
            raise TypeError("environment must contain string pairs")


class JsonlRpcBackendChannel:
    """Attempt-scoped Pi or Oh My Pi implementation of the Channel port."""

    def __init__(
        self,
        config: JsonlRpcBackendConfig,
        *,
        clock: Callable[[], str] = _utc_now,
        client_factory: Callable[..., JsonlRpcClient] = JsonlRpcClient,
    ) -> None:
        if type(config) is not JsonlRpcBackendConfig:
            raise TypeError("config must be a JsonlRpcBackendConfig")
        if not callable(clock) or not callable(client_factory):
            raise TypeError("clock and client_factory must be callable")
        self._config = config
        self._clock = clock
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._client: JsonlRpcClient | None = None
        self._last_agent_end: dict[str, object] | None = None
        self._state_path = config.state_directory / "channel-state.json"
        self._session_directory = config.state_directory / "provider-session"
        self._state = self._load_state()

    def probe(self) -> BackendCapabilities:
        return self._config.capabilities

    @property
    def process_id(self) -> int | None:
        """Current provider PID for cleanup evidence; never an admission claim."""

        with self._lock:
            return None if self._client is None else self._client.process_id

    @property
    def state_path(self) -> Path:
        return self._state_path

    def reserve(self, effect: PreparedEffect[ReserveChannel]) -> ChannelObservation:
        typed = self._require_effect(effect, ReserveChannel)
        command = typed.command
        assert type(command) is ReserveChannel
        with self._lock:
            existing = self._existing(typed.operation_id, typed.command_hash, "reservation")
            if existing is not None:
                return self._channel_observation(existing)
            if (
                command.provider is not self._config.capabilities.provider
                or command.capability_digest != self._config.capabilities.capability_digest
                or command.launch_profile_digest != self._config.capabilities.launch_profile_digest
                or command.policy_digest != self._config.capabilities.policy_digest
            ):
                return self._record_unknown_channel(
                    typed.operation_id,
                    typed.command_hash,
                    "channel_capability_mismatch",
                )
            reservation = self._state.get("reservation")
            if reservation is not None:
                return self._record_unknown_channel(
                    typed.operation_id,
                    typed.command_hash,
                    "attempt_already_has_channel",
                )
            try:
                native = self._ensure_client_locked()
            except JsonlRpcError as exc:
                return self._record_unknown_channel(
                    typed.operation_id,
                    typed.command_hash,
                    exc.code,
                )
            provider_session_id = _token_from_digest("session", typed.command_hash)
            observation = ChannelObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                effect_digest=_effect_digest("reserve_channel", typed.command_hash),
                attempt_id=command.attempt_id,
                channel_id=command.channel_id,
                provider=command.provider,
                provider_session_id=provider_session_id,
                evidence=(
                    f"native_session:{native['sessionId']}",
                    f"sdk:{self._config.launch.sdk_name}@{self._config.launch.sdk_version}",
                ),
            )
            self._state["reservation"] = command.operation_id
            self._state["provider_session_id"] = provider_session_id
            self._put_operation(
                command.operation_id,
                "reservation",
                typed.command_hash,
                observation.to_primitive(),
            )
            self._save_state()
            return observation

    def send(self, effect: PreparedEffect[SendTaskPacket]) -> TurnObservation:
        typed = self._require_effect(effect, SendTaskPacket)
        command = typed.command
        assert type(command) is SendTaskPacket
        with self._lock:
            existing = self._existing(typed.operation_id, typed.command_hash, "send")
            if existing is not None:
                observation = self._turn_observation(existing)
                if observation.status is EffectStatus.UNKNOWN:
                    return self._reconcile_session_locked(typed.operation_id)
                return observation
            reservation = self._reservation_observation()
            if (
                reservation is None
                or reservation.status is not EffectStatus.APPLIED
                or reservation.attempt_id != command.attempt_id
                or reservation.channel_id != command.channel_id
            ):
                return self._record_unknown_turn(
                    typed.operation_id,
                    typed.command_hash,
                    "send",
                    "channel_not_reserved",
                )
            if len(command.task_packet.encode("utf-8")) > self._config.capabilities.max_task_packet_bytes:
                return self._record_unknown_turn(
                    typed.operation_id,
                    typed.command_hash,
                    "send",
                    "task_packet_exceeds_capability",
                )
            if self._state.get("active_send") is not None:
                return self._record_unknown_turn(
                    typed.operation_id,
                    typed.command_hash,
                    "send",
                    "attempt_already_has_turn",
                )
            pending = TurnObservation(
                operation_id=command.operation_id,
                status=EffectStatus.UNKNOWN,
                observed_at=self._clock(),
                state=TurnState.UNKNOWN,
                evidence=("provider_send_not_yet_proven",),
            )
            self._state["active_send"] = command.operation_id
            self._state["task_packet_digest"] = command.task_packet_digest
            self._state["task_packet_text_digest"] = _sha256(command.task_packet.encode("utf-8"))
            self._put_operation(
                command.operation_id,
                "send",
                typed.command_hash,
                pending.to_primitive(),
                command={
                    "attempt_id": command.attempt_id,
                    "channel_id": command.channel_id,
                    "message_id": command.message_id,
                    "turn_id": command.turn_id,
                },
            )
            self._save_state()
            try:
                self._ensure_client_locked()
            except JsonlRpcError as exc:
                operation = self._operation(command.operation_id)
                operation["observation"] = TurnObservation(
                    operation_id=command.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=(f"provider_send_ambiguous:{exc.code}",),
                ).to_primitive()
                self._save_state()
                return self._turn_observation(operation)
            client = self._client
            assert client is not None
        try:
            client.request("prompt", message=command.task_packet)
        except JsonlRpcError as exc:
            with self._lock:
                operation = self._operation(command.operation_id)
                operation["observation"] = TurnObservation(
                    operation_id=command.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=(f"provider_send_ambiguous:{exc.code}",),
                ).to_primitive()
                self._save_state()
                return self._turn_observation(operation)
        with self._lock:
            operation = self._operation(command.operation_id)
            current = self._turn_observation(operation)
            if current.status is EffectStatus.UNKNOWN:
                current = self._applied_turn(
                    command.operation_id,
                    TurnState.RUNNING,
                    typed.command_hash,
                    evidence=("provider_prompt_accepted",),
                )
                operation["observation"] = current.to_primitive()
                self._save_state()
            return current

    def inspect_reservation(self, operation_id: str) -> ChannelObservation:
        self._validate_operation_id(operation_id)
        with self._lock:
            operation = self._state["operations"].get(operation_id)
            if operation is None:
                return ChannelObservation(
                    operation_id=operation_id,
                    status=EffectStatus.ABSENT,
                    observed_at=self._clock(),
                )
            if operation.get("kind") != "reservation":
                return ChannelObservation(
                    operation_id=operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    evidence=("operation_is_not_a_reservation",),
                )
            return self._channel_observation(operation)

    def inspect_turn(self, operation_id: str) -> TurnObservation:
        self._validate_operation_id(operation_id)
        with self._lock:
            operation = self._state["operations"].get(operation_id)
            if operation is None:
                return TurnObservation(
                    operation_id=operation_id,
                    status=EffectStatus.ABSENT,
                    observed_at=self._clock(),
                    state=TurnState.ABSENT,
                )
            if operation.get("kind") not in {"send", "cancel"}:
                return TurnObservation(
                    operation_id=operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=("operation_is_not_a_turn",),
                )
            observation = self._turn_observation(operation)
            if operation.get("kind") == "send" and observation.state in {
                TurnState.RUNNING,
                TurnState.QUEUED,
                TurnState.UNKNOWN,
            }:
                client = self._client
                if client is None or not client.is_alive:
                    observation = self._reconcile_session_locked(operation_id)
            return observation

    def cancel(self, effect: PreparedEffect[CancelTurn]) -> TurnObservation:
        typed = self._require_effect(effect, CancelTurn)
        command = typed.command
        assert type(command) is CancelTurn
        with self._lock:
            existing = self._existing(typed.operation_id, typed.command_hash, "cancel")
            if existing is not None:
                return self._turn_observation(existing)
            send_id = self._state.get("active_send")
            if type(send_id) is not str:
                return self._record_unknown_turn(
                    typed.operation_id,
                    typed.command_hash,
                    "cancel",
                    "turn_not_found",
                )
            send_operation = self._operation(send_id)
            send_command = send_operation.get("command")
            if (
                not _is_object(send_command)
                or send_command.get("attempt_id") != command.attempt_id
                or send_command.get("channel_id") != command.channel_id
                or send_command.get("turn_id") != command.turn_id
            ):
                return self._record_unknown_turn(
                    typed.operation_id,
                    typed.command_hash,
                    "cancel",
                    "turn_not_found",
                )
            source = self._turn_observation(send_operation)
            if source.state in {TurnState.DONE, TurnState.FAILED, TurnState.CANCELLED}:
                return self._complete_cancel_locked(
                    typed,
                    command,
                    send_id,
                    source.state,
                )
            else:
                intent = TurnObservation(
                    operation_id=typed.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=("provider_cancel_not_yet_proven",),
                )
                self._put_operation(
                    typed.operation_id,
                    "cancel",
                    typed.command_hash,
                    intent.to_primitive(),
                    command={"send_operation_id": send_id},
                )
                self._save_state()
                if self._client is None or not self._client.is_alive:
                    source = self._reconcile_session_locked(send_id)
                if source.state in {TurnState.DONE, TurnState.FAILED, TurnState.CANCELLED}:
                    return self._complete_cancel_locked(
                        typed,
                        command,
                        send_id,
                        source.state,
                    )
                client = self._client
                assert client is not None
        try:
            client.request("abort")
        except JsonlRpcError as exc:
            with self._lock:
                operation = self._operation(typed.operation_id)
                operation["observation"] = TurnObservation(
                    operation_id=typed.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=(f"provider_cancel_ambiguous:{exc.code}",),
                ).to_primitive()
                self._save_state()
                return self._turn_observation(operation)
        with self._lock:
            send_operation = self._operation(send_id)
            source = self._turn_observation(send_operation)
            if source.state in {TurnState.DONE, TurnState.FAILED, TurnState.CANCELLED}:
                terminal_state = source.state
            else:
                terminal_state = TurnState.CANCELLED
                send_operation["observation"] = self._applied_turn(
                    send_id,
                    terminal_state,
                    send_operation["command_hash"],
                    evidence=("provider_abort_accepted",),
                ).to_primitive()
            return self._complete_cancel_locked(
                typed,
                command,
                send_id,
                terminal_state,
            )

    def _complete_cancel_locked(
        self,
        effect: PreparedEffect[CancelTurn],
        command: CancelTurn,
        send_operation_id: str,
        terminal_state: TurnState,
    ) -> TurnObservation:
        source = self._turn_observation(self._operation(send_operation_id))
        observation = TurnObservation(
            operation_id=effect.operation_id,
            status=EffectStatus.APPLIED,
            observed_at=self._clock(),
            state=terminal_state,
            effect_digest=_effect_digest("cancel_turn", effect.command_hash),
            attempt_id=command.attempt_id,
            channel_id=command.channel_id,
            message_id=source.message_id,
            turn_id=command.turn_id,
            result_digest=source.result_digest,
            evidence=("provider_cancel_observed",),
        )
        self._put_operation(
            effect.operation_id,
            "cancel",
            effect.command_hash,
            observation.to_primitive(),
            command={"send_operation_id": send_operation_id},
        )
        self._save_state()
        return observation

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            client.close()

    def cleanup(self, *, remove_durable_state: bool = False) -> tuple[str, ...]:
        """Stop the provider tree and optionally remove this exact state root."""

        self.close()
        removed: list[str] = ["provider_process_tree"]
        if remove_durable_state and self._config.state_directory.exists():
            shutil.rmtree(self._config.state_directory)
            removed.append("provider_session_state")
        return tuple(removed)

    def _ensure_client_locked(self) -> dict[str, object]:
        if self._client is not None and self._client.is_alive:
            state = self._client.request("get_state")
            data = state.get("data")
            if _is_object(data):
                return data
            raise JsonlRpcError("rpc_get_state_invalid")
        client = self._client_factory(
            self._config.launch,
            working_directory=self._config.working_directory,
            session_directory=self._session_directory,
            environment=dict(self._config.environment),
            frame_callback=self._on_frame,
            handshake_timeout_seconds=self._config.handshake_timeout_seconds,
            response_timeout_seconds=self._config.response_timeout_seconds,
        )
        if not isinstance(client, JsonlRpcClient):
            raise TypeError("client_factory must return JsonlRpcClient")
        session_file = self._state.get("session_file")
        if type(session_file) is not str or not Path(session_file).is_file():
            session_file = None
        data = client.start(session_file=session_file)
        self._client = client
        self._state["native_session_id"] = data["sessionId"]
        observed_file = data.get("sessionFile")
        if type(observed_file) is str and Path(observed_file).is_absolute():
            self._state["session_file"] = observed_file
        self._state["process_id"] = client.process_id
        self._save_state()
        return data

    def _on_frame(self, frame: dict[str, object]) -> None:
        event_type = frame.get("type")
        if event_type == "agent_end":
            self._last_agent_end = dict(frame)
        terminal_frame: dict[str, object] | None = None
        if self._config.launch.provider is WorkerProvider.PI and event_type == "agent_settled":
            terminal_frame = self._last_agent_end or frame
        elif (
            self._config.launch.provider is WorkerProvider.OH_MY_PI
            and event_type == "agent_end"
            and frame.get("willContinue") is not True
        ):
            terminal_frame = frame
        with self._lock:
            send_id = self._state.get("active_send")
            if type(send_id) is not str:
                return
            operation = self._state["operations"].get(send_id)
            if not _is_object(operation) or operation.get("kind") != "send":
                return
            if event_type in {"agent_start", "turn_start"}:
                current = self._turn_observation(operation)
                if current.status is EffectStatus.UNKNOWN:
                    operation["observation"] = self._applied_turn(
                        send_id,
                        TurnState.RUNNING,
                        operation["command_hash"],
                        evidence=("provider_turn_started",),
                    ).to_primitive()
                    self._save_state()
            if terminal_frame is not None:
                state = self._terminal_state(terminal_frame)
                operation["observation"] = self._applied_turn(
                    send_id,
                    state,
                    operation["command_hash"],
                    result_digest=_sha256(canonical_json_bytes(terminal_frame)),
                    evidence=("provider_terminal_frame",),
                ).to_primitive()
                self._save_state()

    def _applied_turn(
        self,
        operation_id: str,
        state: TurnState,
        command_hash: str,
        *,
        result_digest: str | None = None,
        evidence: tuple[str, ...] = (),
    ) -> TurnObservation:
        operation = self._operation(operation_id)
        command = operation.get("command")
        assert _is_object(command)
        if state is TurnState.DONE and result_digest is None:
            result_digest = _sha256(canonical_json_bytes({"operation_id": operation_id, "state": state.value}))
        return TurnObservation(
            operation_id=operation_id,
            status=EffectStatus.APPLIED,
            observed_at=self._clock(),
            state=state,
            effect_digest=_effect_digest("send_task_packet", command_hash),
            attempt_id=command["attempt_id"],
            channel_id=command["channel_id"],
            message_id=command["message_id"],
            turn_id=command["turn_id"],
            result_digest=result_digest,
            evidence=evidence,
        )

    def _reconcile_session_locked(self, operation_id: str) -> TurnObservation:
        operation = self._operation(operation_id)
        current = self._turn_observation(operation)
        if current.status is EffectStatus.APPLIED and current.state in {
            TurnState.DONE,
            TurnState.FAILED,
            TurnState.CANCELLED,
        }:
            return current
        session_file = self._state.get("session_file")
        expected_digest = self._state.get("task_packet_text_digest")
        if type(session_file) is not str or type(expected_digest) is not str:
            return current
        try:
            assistant, found_user = self._session_result(Path(session_file), expected_digest)
        except JsonlRpcError as exc:
            operation["observation"] = TurnObservation(
                operation_id=operation_id,
                status=EffectStatus.UNKNOWN,
                observed_at=self._clock(),
                state=TurnState.UNKNOWN,
                evidence=(f"session_reconcile_failed:{exc.code}",),
            ).to_primitive()
            self._save_state()
            return self._turn_observation(operation)
        if assistant is not None:
            state = self._terminal_state(assistant)
            operation["observation"] = self._applied_turn(
                operation_id,
                state,
                operation["command_hash"],
                result_digest=_sha256(canonical_json_bytes(assistant)),
                evidence=("provider_session_reconciled",),
            ).to_primitive()
        elif found_user:
            operation["observation"] = self._applied_turn(
                operation_id,
                TurnState.FAILED,
                operation["command_hash"],
                evidence=("provider_session_interrupted_after_acceptance",),
            ).to_primitive()
        else:
            operation["observation"] = TurnObservation(
                operation_id=operation_id,
                status=EffectStatus.UNKNOWN,
                observed_at=self._clock(),
                state=TurnState.UNKNOWN,
                evidence=("provider_session_does_not_prove_send",),
            ).to_primitive()
        self._save_state()
        return self._turn_observation(operation)

    @staticmethod
    def _session_result(path: Path, expected_digest: str) -> tuple[dict[str, object] | None, bool]:
        if not path.is_file() or path.is_symlink():
            raise JsonlRpcError("provider_session_missing")
        try:
            if path.stat().st_size > _MAX_SESSION_BYTES:
                raise JsonlRpcError("provider_session_too_large")
            found_user = False
            assistant: dict[str, object] | None = None
            with path.open("rb") as stream:
                for index, raw in enumerate(stream, start=1):
                    if index > _MAX_SESSION_LINES:
                        raise JsonlRpcError("provider_session_too_many_lines")
                    try:
                        entry = json.loads(raw.decode("utf-8", errors="strict"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise JsonlRpcError("provider_session_invalid_json") from exc
                    if not _is_object(entry) or entry.get("type") != "message":
                        continue
                    message = entry.get("message")
                    if not _is_object(message):
                        continue
                    role = message.get("role")
                    if role == "user":
                        text = JsonlRpcBackendChannel._message_text(message)
                        found_user = text is not None and _sha256(text.encode("utf-8")) == expected_digest
                        assistant = None
                    elif role == "assistant" and found_user:
                        assistant = dict(message)
            return assistant, found_user
        except OSError as exc:
            raise JsonlRpcError("provider_session_read_failed", str(exc)) from exc

    @staticmethod
    def _message_text(message: dict[str, object]) -> str | None:
        content = message.get("content")
        if type(content) is str:
            return content
        if type(content) is list:
            parts: list[str] = []
            for item in content:
                if _is_object(item) and item.get("type") == "text" and type(item.get("text")) is str:
                    parts.append(item["text"])
            return "".join(parts) if parts else None
        return None

    @staticmethod
    def _terminal_state(frame: dict[str, object]) -> TurnState:
        stop_reasons: list[str] = []

        def visit(value: object) -> None:
            if type(value) is dict:
                for key, item in value.items():
                    if key in {"stopReason", "stop_reason"} and type(item) is str:
                        stop_reasons.append(item.lower())
                    else:
                        visit(item)
            elif type(value) is list:
                for item in value:
                    visit(item)

        visit(frame)
        if "aborted" in stop_reasons or "cancelled" in stop_reasons:
            return TurnState.CANCELLED
        if "error" in stop_reasons:
            return TurnState.FAILED
        return TurnState.DONE

    def _load_state(self) -> dict[str, object]:
        if not self._state_path.exists():
            return {
                "schema_version": _STATE_SCHEMA_VERSION,
                "provider": self._config.launch.provider.value,
                "operations": {},
            }
        if self._state_path.is_symlink() or not self._state_path.is_file():
            raise JsonlRpcError("provider_state_not_regular")
        try:
            raw = self._state_path.read_bytes()
            value = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise JsonlRpcError("provider_state_invalid") from exc
        if (
            not _is_object(value)
            or value.get("schema_version") != _STATE_SCHEMA_VERSION
            or value.get("provider") != self._config.launch.provider.value
            or not _is_object(value.get("operations"))
            or raw != canonical_json_bytes(value)
        ):
            raise JsonlRpcError("provider_state_invalid")
        return value

    def _save_state(self) -> None:
        self._config.state_directory.mkdir(parents=True, exist_ok=True)
        raw = canonical_json_bytes(self._state)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".channel-state-",
            suffix=".tmp",
            dir=self._config.state_directory,
        )
        path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, self._state_path)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _put_operation(
        self,
        operation_id: str,
        kind: str,
        command_hash: str,
        observation: dict[str, object],
        *,
        command: dict[str, object] | None = None,
    ) -> None:
        operations = self._state["operations"]
        assert type(operations) is dict
        operations[operation_id] = {
            "command_hash": command_hash,
            "kind": kind,
            "observation": observation,
            **({} if command is None else {"command": command}),
        }

    def _existing(
        self,
        operation_id: str,
        command_hash: str,
        kind: str,
    ) -> dict[str, object] | None:
        operation = self._state["operations"].get(operation_id)
        if operation is None:
            return None
        if (
            not _is_object(operation)
            or operation.get("kind") != kind
            or operation.get("command_hash") != command_hash
        ):
            if kind == "reservation":
                return {
                    "observation": ChannelObservation(
                        operation_id=operation_id,
                        status=EffectStatus.UNKNOWN,
                        observed_at=self._clock(),
                        evidence=("operation_id_collision",),
                    ).to_primitive()
                }
            return {
                "observation": TurnObservation(
                    operation_id=operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=("operation_id_collision",),
                ).to_primitive()
            }
        return operation

    def _operation(self, operation_id: str) -> dict[str, object]:
        operation = self._state["operations"].get(operation_id)
        if not _is_object(operation):
            raise JsonlRpcError("provider_operation_missing", operation_id)
        return operation

    def _reservation_observation(self) -> ChannelObservation | None:
        operation_id = self._state.get("reservation")
        if type(operation_id) is not str:
            return None
        return self._channel_observation(self._operation(operation_id))

    def _record_unknown_channel(
        self,
        operation_id: str,
        command_hash: str,
        reason: str,
    ) -> ChannelObservation:
        observation = ChannelObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            evidence=(reason,),
        )
        self._put_operation(operation_id, "reservation", command_hash, observation.to_primitive())
        self._save_state()
        return observation

    def _record_unknown_turn(
        self,
        operation_id: str,
        command_hash: str,
        kind: str,
        reason: str,
    ) -> TurnObservation:
        observation = TurnObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            state=TurnState.UNKNOWN,
            evidence=(reason,),
        )
        self._put_operation(operation_id, kind, command_hash, observation.to_primitive())
        self._save_state()
        return observation

    @staticmethod
    def _channel_observation(operation: dict[str, object]) -> ChannelObservation:
        value = operation.get("observation")
        if not _is_object(value):
            raise JsonlRpcError("provider_observation_invalid")
        provider = value.get("provider")
        return ChannelObservation(
            operation_id=value["operation_id"],
            status=EffectStatus(value["status"]),
            observed_at=value["observed_at"],
            effect_digest=value.get("effect_digest"),
            attempt_id=value.get("attempt_id"),
            channel_id=value.get("channel_id"),
            provider=None if provider is None else WorkerProvider(provider),
            provider_session_id=value.get("provider_session_id"),
            evidence=tuple(value.get("evidence", ())),
        )

    @staticmethod
    def _turn_observation(operation: dict[str, object]) -> TurnObservation:
        value = operation.get("observation")
        if not _is_object(value):
            raise JsonlRpcError("provider_observation_invalid")
        return TurnObservation(
            operation_id=value["operation_id"],
            status=EffectStatus(value["status"]),
            observed_at=value["observed_at"],
            state=TurnState(value["state"]),
            effect_digest=value.get("effect_digest"),
            attempt_id=value.get("attempt_id"),
            channel_id=value.get("channel_id"),
            message_id=value.get("message_id"),
            turn_id=value.get("turn_id"),
            result_digest=value.get("result_digest"),
            evidence=tuple(value.get("evidence", ())),
        )

    @staticmethod
    def _require_effect(
        effect: object,
        command_type: type[ReserveChannel] | type[SendTaskPacket] | type[CancelTurn],
    ) -> PreparedEffect:
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        if type(effect.command) is not command_type:
            raise TypeError(f"effect command must be {command_type.__name__}")
        return effect

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if type(operation_id) is not str or not operation_id:
            raise TypeError("operation_id must be a non-empty string")


__all__ = [
    "JsonlRpcBackendChannel",
    "JsonlRpcBackendConfig",
    "JsonlRpcClient",
    "JsonlRpcError",
    "JsonlRpcLaunch",
    "JsonlRpcProtocol",
]
