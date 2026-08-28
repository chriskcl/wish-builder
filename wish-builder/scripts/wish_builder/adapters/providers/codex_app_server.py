"""Attempt-scoped Codex app-server backend adapter.

Codex owns its native thread and turn identifiers. Wish Builder records those
identifiers beside its caller-controlled operation and message identifiers so a
restart can inspect the durable Codex thread without sending the task twice.
The app-server process is never shared between sibling attempts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

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
_MAX_FRAME_BYTES = 1_048_576
_MAX_OUTPUT_BYTES = 64 * 1_048_576
_MAX_STATE_BYTES = 4 * 1_048_576
_STDERR_LIMIT = 64 * 1_024

CODEX_COMPLETION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "failed"]},
        "summary": {"type": "string", "minLength": 1, "maxLength": 16_384},
        "changed_files": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 1_024},
            "maxItems": 10_000,
        },
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 1_024},
                    "status": {
                        "type": "string",
                        "enum": ["passed", "failed", "not_run"],
                    },
                    "details": {"type": "string", "maxLength": 16_384},
                },
                "required": ["name", "status", "details"],
                "additionalProperties": False,
            },
            "maxItems": 10_000,
        },
    },
    "required": ["status", "summary", "changed_files", "checks"],
    "additionalProperties": False,
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _effect_digest(operation: str, command_hash: str) -> str:
    return _sha256(
        canonical_json_bytes({"command_hash": command_hash, "operation": operation})
    )


def _is_object(value: object) -> bool:
    return type(value) is dict and all(type(key) is str for key in value)


class CodexAppServerError(RuntimeError):
    """Stable Codex process, protocol, or reconciliation error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class CodexAppServerLaunch:
    """Exact Codex executable and package identity admitted for one attempt."""

    command_prefix: tuple[str, ...]
    sdk_version: str
    sdk_shasum: str
    sdk_integrity: str
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.command_prefix) is not tuple
            or not self.command_prefix
            or not all(type(item) is str and item for item in self.command_prefix)
        ):
            raise TypeError("command_prefix must be a non-empty tuple of strings")
        if not Path(self.command_prefix[0]).is_absolute():
            raise ValueError("the Codex executable must be an absolute path")
        if not all(
            type(value) is str and value
            for value in (self.sdk_version, self.sdk_shasum, self.sdk_integrity)
        ):
            raise ValueError("Codex package identity must be complete")
        if type(self.extra_args) is not tuple or not all(
            type(item) is str and item for item in self.extra_args
        ):
            raise TypeError("extra_args must be a tuple of non-empty strings")

    @property
    def argv(self) -> tuple[str, ...]:
        return (*self.command_prefix, "app-server", "--stdio", *self.extra_args)


FrameCallback = Callable[[dict[str, object]], None]
ThreadStartedCallback = Callable[[str], None]
TurnAcceptedCallback = Callable[[str, str], None]


class CodexAppServerClient:
    """One bounded, duplex JSONL app-server subprocess."""

    def __init__(
        self,
        launch: CodexAppServerLaunch,
        *,
        working_directory: Path,
        environment: Mapping[str, str] | None = None,
        frame_callback: FrameCallback | None = None,
        response_timeout_seconds: float = 30.0,
    ) -> None:
        if type(launch) is not CodexAppServerLaunch:
            raise TypeError("launch must be a CodexAppServerLaunch")
        if not isinstance(working_directory, Path) or not working_directory.is_absolute():
            raise ValueError("working_directory must be an absolute Path")
        if not working_directory.is_dir():
            raise ValueError("working_directory must exist")
        if environment is not None and (
            not isinstance(environment, Mapping)
            or not all(type(key) is str and type(value) is str for key, value in environment.items())
        ):
            raise TypeError("environment must map strings to strings")
        if frame_callback is not None and not callable(frame_callback):
            raise TypeError("frame_callback must be callable or null")
        if (
            type(response_timeout_seconds) not in {int, float}
            or isinstance(response_timeout_seconds, bool)
            or float(response_timeout_seconds) <= 0
        ):
            raise ValueError("response_timeout_seconds must be positive")
        self.launch = launch
        self.working_directory = working_directory
        self._environment = None if environment is None else dict(environment)
        self._callback = frame_callback
        self._response_timeout = float(response_timeout_seconds)
        self._condition = threading.Condition(threading.RLock())
        self._write_lock = threading.Lock()
        self._responses: dict[int, dict[str, object]] = {}
        self._notifications: list[dict[str, object]] = []
        self._fatal: CodexAppServerError | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr = bytearray()
        self._next_id = 1
        self._event_position = 0
        self._output_bytes = 0
        self._closed = False
        self._initialized = False

    @property
    def process_id(self) -> int | None:
        process = self._process
        return None if process is None or process.poll() is not None else process.pid

    @property
    def is_alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None and not self._closed

    @property
    def stderr_text(self) -> str:
        with self._condition:
            return bytes(self._stderr).decode("utf-8", errors="replace")

    @property
    def event_position(self) -> int:
        with self._condition:
            return self._event_position

    def connect(self) -> None:
        with self._condition:
            if self._process is not None:
                raise CodexAppServerError("codex_process_already_started")
        environment = os.environ.copy()
        if self._environment is not None:
            environment.update(self._environment)
        options: dict[str, object] = {
            "cwd": str(self.working_directory),
            "env": environment,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(  # noqa: S603 - exact admitted command
                self.launch.argv,
                **options,
            )
        except OSError as exc:
            raise CodexAppServerError("codex_process_start_failed", str(exc)) from exc
        self._process = process
        assert process.stdout is not None and process.stderr is not None
        self._reader = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout,),
            name="wish-builder-codex-stdout",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr,),
            name="wish-builder-codex-stderr",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()
        response = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "wish_builder",
                    "title": "Wish Builder",
                    "version": "0.1.0.dev0",
                }
            },
        )
        if not _is_object(response):
            self.close()
            raise CodexAppServerError("codex_initialize_invalid")
        self.notify("initialized", None)
        self._initialized = True

    def start_thread(
        self,
        *,
        on_started: ThreadStartedCallback | None = None,
    ) -> str:
        self._require_initialized()
        response = self.request(
            "thread/start",
            {
                "cwd": str(self.working_directory),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "ephemeral": False,
                "config": {
                    "features": {
                        "multi_agent": False,
                        "multi_agent_v2": {"enabled": False},
                    }
                },
            },
        )
        thread = response.get("thread") if _is_object(response) else None
        thread_id = thread.get("id") if _is_object(thread) else None
        if type(thread_id) is not str or not thread_id:
            raise CodexAppServerError("codex_thread_start_invalid")
        if on_started is not None:
            on_started(thread_id)
        return thread_id

    def begin_turn(
        self,
        *,
        thread_id: str,
        message_id: str,
        task_packet: str,
        output_schema: dict[str, object],
        on_accepted: TurnAcceptedCallback | None = None,
    ) -> str:
        self._require_initialized()
        if not all(type(value) is str and value for value in (thread_id, message_id, task_packet)):
            raise ValueError("thread, message, and packet values must be non-empty strings")
        _validate_schema_definition(output_schema)
        response = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "clientUserMessageId": message_id,
                "input": [{"type": "text", "text": task_packet}],
                "outputSchema": output_schema,
            },
        )
        turn = response.get("turn") if _is_object(response) else None
        provider_turn_id = turn.get("id") if _is_object(turn) else None
        if type(provider_turn_id) is not str or not provider_turn_id:
            raise CodexAppServerError("codex_turn_start_invalid")
        if on_accepted is not None:
            on_accepted(thread_id, provider_turn_id)
        return provider_turn_id

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None:
        response = self.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )
        if not _is_object(response):
            raise CodexAppServerError("codex_turn_interrupt_invalid")

    def read_thread(self, thread_id: str) -> dict[str, object]:
        response = self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = response.get("thread") if _is_object(response) else None
        if not _is_object(thread) or thread.get("id") != thread_id:
            raise CodexAppServerError("codex_thread_read_invalid")
        return dict(thread)

    def wait_for_turn_completed(
        self, thread_id: str, turn_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, object]:
        timeout = self._response_timeout if timeout_seconds is None else float(timeout_seconds)
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for frame in self._notifications:
                    if _completed_frame_matches(frame, thread_id, turn_id):
                        return dict(frame)
                self._raise_if_unavailable()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexAppServerError("codex_turn_terminal_timeout")
                self._condition.wait(remaining)

    def completed_notification(
        self, thread_id: str, turn_id: str
    ) -> dict[str, object] | None:
        with self._condition:
            for frame in self._notifications:
                if _completed_frame_matches(frame, thread_id, turn_id):
                    return dict(frame)
        return None

    def item_notifications(self, thread_id: str, turn_id: str) -> tuple[dict[str, object], ...]:
        with self._condition:
            return tuple(
                dict(frame)
                for frame in self._notifications
                if frame.get("method") in {"item/started", "item/completed"}
                and _is_object(frame.get("params"))
                and frame["params"].get("threadId") == thread_id
                and frame["params"].get("turnId") == turn_id
            )

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if type(method) is not str or not method or not _is_object(params):
            raise ValueError("request method and params must be valid")
        with self._condition:
            if self._closed or self._process is None:
                raise CodexAppServerError("codex_process_not_started")
            request_id = self._next_id
            self._next_id += 1
        self._write_frame({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self._response_timeout
        with self._condition:
            while request_id not in self._responses:
                self._raise_if_unavailable()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexAppServerError("codex_response_timeout", method)
                self._condition.wait(remaining)
            frame = self._responses.pop(request_id)
        error = frame.get("error")
        if error is not None:
            raise CodexAppServerError("codex_rpc_error", json.dumps(error, sort_keys=True))
        result = frame.get("result")
        if not _is_object(result):
            raise CodexAppServerError("codex_response_invalid", method)
        return dict(result)

    def notify(self, method: str, params: dict[str, object] | None) -> None:
        frame: dict[str, object] = {"method": method}
        if params is not None:
            frame["params"] = params
        self._write_frame(frame)

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

    def _require_initialized(self) -> None:
        if not self._initialized or not self.is_alive:
            raise CodexAppServerError("codex_not_initialized")

    def _raise_if_unavailable(self) -> None:
        if self._fatal is not None:
            raise self._fatal
        process = self._process
        if process is None or process.poll() is not None:
            raise CodexAppServerError(
                "codex_process_exited",
                f"returncode={None if process is None else process.returncode}",
            )

    def _write_frame(self, frame: dict[str, object]) -> None:
        raw = canonical_json_bytes(frame)
        if len(raw) > _MAX_FRAME_BYTES:
            raise CodexAppServerError("codex_command_frame_too_large")
        process = self._process
        if process is None or process.stdin is None:
            raise CodexAppServerError("codex_stdin_unavailable")
        try:
            with self._write_lock:
                process.stdin.write(raw)
                process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexAppServerError("codex_write_failed", str(exc)) from exc

    def _read_stdout(self, stream: BinaryIO) -> None:
        try:
            while True:
                raw = stream.readline(_MAX_FRAME_BYTES + 1)
                if not raw:
                    break
                if len(raw) > _MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                    raise CodexAppServerError("codex_physical_frame_too_large")
                self._output_bytes += len(raw)
                if self._output_bytes > _MAX_OUTPUT_BYTES:
                    raise CodexAppServerError("codex_output_limit_exceeded")
                line = raw[:-1]
                if line.endswith(b"\r") or not line:
                    raise CodexAppServerError("codex_non_lf_frame")
                try:
                    frame = json.loads(line.decode("utf-8", errors="strict"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CodexAppServerError("codex_invalid_json") from exc
                if not _is_object(frame):
                    raise CodexAppServerError("codex_frame_not_object")
                self._dispatch_frame(frame)
        except CodexAppServerError as exc:
            with self._condition:
                if not self._closed and self._fatal is None:
                    self._fatal = exc
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._condition.notify_all()

    def _dispatch_frame(self, frame: dict[str, object]) -> None:
        method = frame.get("method")
        request_id = frame.get("id")
        callback = self._callback
        with self._condition:
            self._event_position += 1
        if type(method) is str and type(request_id) is int:
            try:
                self._write_frame(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": "headless Wish Builder rejects server requests",
                        },
                    }
                )
            finally:
                with self._condition:
                    if self._fatal is None:
                        self._fatal = CodexAppServerError(
                            "codex_server_request_rejected", method
                        )
                    self._condition.notify_all()
            return
        with self._condition:
            if type(request_id) is int and method is None:
                if request_id in self._responses:
                    self._fatal = CodexAppServerError(
                        "codex_duplicate_response", str(request_id)
                    )
                else:
                    self._responses[request_id] = dict(frame)
            elif type(method) is str and request_id is None:
                self._notifications.append(dict(frame))
            else:
                self._fatal = CodexAppServerError("codex_invalid_frame_shape")
            self._condition.notify_all()
        if callback is not None and type(method) is str and request_id is None:
            try:
                callback(dict(frame))
            except Exception:
                with self._condition:
                    if self._fatal is None:
                        self._fatal = CodexAppServerError("codex_frame_callback_failed")
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


@runtime_checkable
class CodexClientPort(Protocol):
    @property
    def is_alive(self) -> bool: ...

    @property
    def event_position(self) -> int: ...

    def connect(self) -> None: ...

    def start_thread(self, *, on_started: ThreadStartedCallback | None = None) -> str: ...

    def begin_turn(
        self,
        *,
        thread_id: str,
        message_id: str,
        task_packet: str,
        output_schema: dict[str, object],
        on_accepted: TurnAcceptedCallback | None = None,
    ) -> str: ...

    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None: ...

    def read_thread(self, thread_id: str) -> dict[str, object]: ...

    def wait_for_turn_completed(
        self, thread_id: str, turn_id: str, *, timeout_seconds: float | None = None
    ) -> dict[str, object]: ...

    def completed_notification(
        self, thread_id: str, turn_id: str
    ) -> dict[str, object] | None: ...

    def item_notifications(
        self, thread_id: str, turn_id: str
    ) -> tuple[dict[str, object], ...]: ...

    def close(self) -> None: ...


CodexClientFactory = Callable[..., CodexClientPort]


@dataclass(frozen=True, slots=True)
class CodexAppServerConfig:
    capabilities: BackendCapabilities
    launch: CodexAppServerLaunch
    working_directory: Path
    state_directory: Path
    environment: tuple[tuple[str, str], ...] = ()
    output_schema: dict[str, object] | None = None
    response_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if type(self.capabilities) is not BackendCapabilities:
            raise TypeError("capabilities must be BackendCapabilities")
        if self.capabilities.provider is not WorkerProvider.CODEX:
            raise ValueError("Codex capabilities are required")
        if type(self.launch) is not CodexAppServerLaunch:
            raise TypeError("launch must be a CodexAppServerLaunch")
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
        schema = CODEX_COMPLETION_SCHEMA if self.output_schema is None else self.output_schema
        _validate_schema_definition(schema)
        object.__setattr__(self, "output_schema", json.loads(json.dumps(schema)))


class CodexAppServerChannel:
    """Attempt-scoped implementation of the current backend Channel port."""

    def __init__(
        self,
        config: CodexAppServerConfig,
        *,
        clock: Callable[[], str] = _utc_now,
        client_factory: CodexClientFactory = CodexAppServerClient,
    ) -> None:
        if type(config) is not CodexAppServerConfig:
            raise TypeError("config must be a CodexAppServerConfig")
        if not callable(clock) or not callable(client_factory):
            raise TypeError("clock and client_factory must be callable")
        self._config = config
        self._clock = clock
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._client: CodexClientPort | None = None
        self._state_path = config.state_directory / "channel-state.json"
        self._state = self._load_state()

    def probe(self) -> BackendCapabilities:
        return self._config.capabilities

    @property
    def process_id(self) -> int | None:
        """Current provider PID for cleanup evidence; never an admission claim."""

        with self._lock:
            client = self._client
            value = None if client is None else getattr(client, "process_id", None)
            return value if type(value) is int and value > 0 else None

    @property
    def state_path(self) -> Path:
        """Exact durable channel-state path used by recovery qualification."""

        return self._state_path

    def reserve(self, effect: PreparedEffect[ReserveChannel]) -> ChannelObservation:
        typed = self._require_effect(effect, ReserveChannel)
        command = typed.command
        assert type(command) is ReserveChannel
        with self._lock:
            existing = self._existing(command.operation_id, typed.command_hash, "reservation")
            if existing is not None:
                return self._channel_observation(existing)
            if (
                command.provider is not WorkerProvider.CODEX
                or command.capability_digest != self._config.capabilities.capability_digest
                or command.launch_profile_digest != self._config.capabilities.launch_profile_digest
                or command.policy_digest != self._config.capabilities.policy_digest
            ):
                return self._unknown_channel(command.operation_id, typed.command_hash, "capability_mismatch")
            if self._state.get("reservation") is not None:
                return self._unknown_channel(command.operation_id, typed.command_hash, "attempt_already_reserved")
            self._put_operation(
                command.operation_id,
                "reservation",
                typed.command_hash,
                ChannelObservation(
                    operation_id=command.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    evidence=("codex_thread_not_yet_proven",),
                ).to_primitive(),
                command={
                    "attempt_id": command.attempt_id,
                    "channel_id": command.channel_id,
                },
            )
            self._state["reservation"] = command.operation_id
            self._save_state()
            try:
                client = self._new_client()
                client.connect()
                self._client = client

                def record_thread(thread_id: str) -> None:
                    with self._lock:
                        self._state["thread_id"] = thread_id
                        self._state["provider_event_position"] = client.event_position
                        self._save_state()

                thread_id = client.start_thread(on_started=record_thread)
            except (CodexAppServerError, OSError, ValueError) as exc:
                return self._unknown_channel(
                    command.operation_id,
                    typed.command_hash,
                    _error_code(exc),
                    replace=True,
                )
            observation = ChannelObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                effect_digest=_effect_digest("reserve_channel", typed.command_hash),
                attempt_id=command.attempt_id,
                channel_id=command.channel_id,
                provider=WorkerProvider.CODEX,
                provider_session_id=thread_id,
                evidence=(
                    f"sdk:@openai/codex@{self._config.launch.sdk_version}",
                    f"thread:{thread_id}",
                ),
            )
            self._operation(command.operation_id)["observation"] = observation.to_primitive()
            self._save_state()
            return observation

    def send(self, effect: PreparedEffect[SendTaskPacket]) -> TurnObservation:
        typed = self._require_effect(effect, SendTaskPacket)
        command = typed.command
        assert type(command) is SendTaskPacket
        with self._lock:
            existing = self._existing(command.operation_id, typed.command_hash, "send")
            if existing is not None:
                observation = self._turn_observation(existing)
                return self._reconcile_locked(command.operation_id) if observation.status is EffectStatus.UNKNOWN else observation
            reservation = self._reservation_observation()
            thread_id = self._state.get("thread_id")
            if (
                reservation is None
                or reservation.status is not EffectStatus.APPLIED
                or reservation.attempt_id != command.attempt_id
                or reservation.channel_id != command.channel_id
                or type(thread_id) is not str
            ):
                return self._unknown_turn(command.operation_id, typed.command_hash, "send", "channel_not_reserved")
            if len(command.task_packet.encode("utf-8")) > self._config.capabilities.max_task_packet_bytes:
                return self._unknown_turn(command.operation_id, typed.command_hash, "send", "task_packet_exceeds_capability")
            if self._state.get("active_send") is not None:
                return self._unknown_turn(command.operation_id, typed.command_hash, "send", "attempt_already_has_turn")
            self._put_operation(
                command.operation_id,
                "send",
                typed.command_hash,
                TurnObservation(
                    operation_id=command.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=("codex_turn_not_yet_proven",),
                ).to_primitive(),
                command={
                    "attempt_id": command.attempt_id,
                    "channel_id": command.channel_id,
                    "message_id": command.message_id,
                    "logical_turn_id": command.turn_id,
                    "task_packet_digest": command.task_packet_digest,
                },
            )
            self._state["active_send"] = command.operation_id
            self._save_state()
            try:
                client = self._ensure_live_client()

                def record_turn(observed_thread_id: str, provider_turn_id: str) -> None:
                    with self._lock:
                        if observed_thread_id != thread_id:
                            raise CodexAppServerError("codex_thread_identity_mismatch")
                        operation = self._operation(command.operation_id)
                        operation["provider_turn_id"] = provider_turn_id
                        operation["provider_event_position"] = client.event_position
                        operation["observation"] = self._applied_turn(
                            command.operation_id,
                            TurnState.RUNNING,
                            typed.command_hash,
                            evidence=("codex_turn_accepted",),
                        ).to_primitive()
                        self._save_state()

                provider_turn_id = client.begin_turn(
                    thread_id=thread_id,
                    message_id=command.message_id,
                    task_packet=command.task_packet,
                    output_schema=self._config.output_schema or CODEX_COMPLETION_SCHEMA,
                    on_accepted=record_turn,
                )
                self._apply_live_terminal_locked(command.operation_id, provider_turn_id)
            except (CodexAppServerError, OSError, ValueError) as exc:
                operation = self._operation(command.operation_id)
                operation["observation"] = TurnObservation(
                    operation_id=command.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=(f"codex_send_ambiguous:{_error_code(exc)}",),
                ).to_primitive()
                self._save_state()
            return self._turn_observation(self._operation(command.operation_id))

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
            if not _is_object(operation) or operation.get("kind") != "reservation":
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
            if not _is_object(operation) or operation.get("kind") not in {"send", "cancel"}:
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
                provider_turn_id = operation.get("provider_turn_id")
                if type(provider_turn_id) is str and self._client is not None and self._client.is_alive:
                    self._apply_live_terminal_locked(operation_id, provider_turn_id)
                    observation = self._turn_observation(operation)
                elif self._client is None or not self._client.is_alive:
                    observation = self._reconcile_locked(operation_id)
            return observation

    def cancel(self, effect: PreparedEffect[CancelTurn]) -> TurnObservation:
        typed = self._require_effect(effect, CancelTurn)
        command = typed.command
        assert type(command) is CancelTurn
        with self._lock:
            existing = self._existing(command.operation_id, typed.command_hash, "cancel")
            if existing is not None:
                return self._turn_observation(existing)
            send_id = self._state.get("active_send")
            if type(send_id) is not str:
                return self._unknown_turn(command.operation_id, typed.command_hash, "cancel", "turn_not_found")
            send = self._operation(send_id)
            send_command = send.get("command")
            provider_turn_id = send.get("provider_turn_id")
            thread_id = self._state.get("thread_id")
            if (
                not _is_object(send_command)
                or send_command.get("attempt_id") != command.attempt_id
                or send_command.get("channel_id") != command.channel_id
                or send_command.get("logical_turn_id") != command.turn_id
                or type(provider_turn_id) is not str
                or type(thread_id) is not str
            ):
                return self._unknown_turn(command.operation_id, typed.command_hash, "cancel", "turn_not_found")
            self._put_operation(
                command.operation_id,
                "cancel",
                typed.command_hash,
                TurnObservation(
                    operation_id=command.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=("codex_interrupt_not_yet_terminal",),
                ).to_primitive(),
                command={"send_operation_id": send_id},
            )
            self._save_state()
            try:
                client = self._ensure_live_client()
                client.interrupt_turn(thread_id=thread_id, turn_id=provider_turn_id)
                frame = client.wait_for_turn_completed(thread_id, provider_turn_id)
                source = self._terminal_from_frame(send_id, frame, client)
                send["observation"] = source.to_primitive()
                terminal_state = source.state
                if terminal_state is not TurnState.CANCELLED:
                    raise CodexAppServerError(
                        "codex_interrupt_not_cancelled", terminal_state.value
                    )
            except (CodexAppServerError, OSError, ValueError) as exc:
                operation = self._operation(command.operation_id)
                operation["observation"] = TurnObservation(
                    operation_id=command.operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=(f"codex_cancel_ambiguous:{_error_code(exc)}",),
                ).to_primitive()
                self._save_state()
                return self._turn_observation(operation)
            source = self._turn_observation(send)
            observation = TurnObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                state=TurnState.CANCELLED,
                effect_digest=_effect_digest("cancel_turn", typed.command_hash),
                attempt_id=command.attempt_id,
                channel_id=command.channel_id,
                message_id=source.message_id,
                turn_id=command.turn_id,
                evidence=("codex_turn_completed_interrupted",),
            )
            self._operation(command.operation_id)["observation"] = observation.to_primitive()
            self._save_state()
            return observation

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            client.close()

    def cleanup(self, *, remove_durable_state: bool = False) -> tuple[str, ...]:
        self.close()
        removed = ["provider_process_tree"]
        if remove_durable_state and self._config.state_directory.exists():
            shutil.rmtree(self._config.state_directory)
            removed.append("provider_session_state")
        return tuple(removed)

    def _new_client(self) -> CodexClientPort:
        client = self._client_factory(
            self._config.launch,
            working_directory=self._config.working_directory,
            environment=dict(self._config.environment),
            frame_callback=self._on_frame,
            response_timeout_seconds=self._config.response_timeout_seconds,
        )
        if not isinstance(client, CodexClientPort):
            raise TypeError("client_factory must return CodexClientPort")
        return client

    def _ensure_live_client(self) -> CodexClientPort:
        if self._client is None or not self._client.is_alive:
            raise CodexAppServerError("codex_live_process_unavailable")
        return self._client

    def _on_frame(self, frame: dict[str, object]) -> None:
        if frame.get("method") != "turn/completed":
            return
        params = frame.get("params")
        turn = params.get("turn") if _is_object(params) else None
        provider_turn_id = turn.get("id") if _is_object(turn) else None
        with self._lock:
            send_id = self._state.get("active_send")
            if type(send_id) is not str:
                return
            operation = self._state["operations"].get(send_id)
            if not _is_object(operation) or operation.get("provider_turn_id") != provider_turn_id:
                return
            client = self._client
            if client is None:
                return
            operation["observation"] = self._terminal_from_frame(
                send_id, frame, client
            ).to_primitive()
            operation["provider_event_position"] = client.event_position
            self._save_state()

    def _apply_live_terminal_locked(self, operation_id: str, provider_turn_id: str) -> None:
        client = self._client
        thread_id = self._state.get("thread_id")
        if client is None or type(thread_id) is not str:
            return
        frame = client.completed_notification(thread_id, provider_turn_id)
        if frame is not None:
            self._operation(operation_id)["observation"] = self._terminal_from_frame(
                operation_id, frame, client
            ).to_primitive()
            self._save_state()

    def _terminal_from_frame(
        self,
        operation_id: str,
        frame: dict[str, object],
        client: CodexClientPort,
    ) -> TurnObservation:
        params = frame.get("params")
        turn = params.get("turn") if _is_object(params) else None
        thread_id = params.get("threadId") if _is_object(params) else None
        operation = self._operation(operation_id)
        provider_turn_id = operation.get("provider_turn_id")
        if (
            not _is_object(turn)
            or turn.get("id") != provider_turn_id
            or thread_id != self._state.get("thread_id")
        ):
            return self._unknown_for_operation(operation_id, "codex_terminal_identity_mismatch")
        items = _turn_items(turn)
        if type(thread_id) is str and type(provider_turn_id) is str:
            items.extend(_items_from_notifications(client.item_notifications(thread_id, provider_turn_id)))
        return self._terminal_observation(operation_id, turn, items, "codex_live_terminal")

    def _reconcile_locked(self, operation_id: str) -> TurnObservation:
        operation = self._operation(operation_id)
        current = self._turn_observation(operation)
        if current.status is EffectStatus.APPLIED and current.state in {
            TurnState.DONE,
            TurnState.FAILED,
            TurnState.CANCELLED,
        }:
            return current
        thread_id = self._state.get("thread_id")
        command = operation.get("command")
        if type(thread_id) is not str or not _is_object(command):
            return self._unknown_for_operation(operation_id, "codex_reconcile_identity_missing")
        recovery: CodexClientPort | None = None
        try:
            recovery = self._new_client()
            recovery.connect()
            thread = recovery.read_thread(thread_id)
            turns = thread.get("turns")
            if type(turns) is not list or not all(_is_object(item) for item in turns):
                raise CodexAppServerError("codex_thread_turns_invalid")
            expected_turn = operation.get("provider_turn_id")
            expected_message = command.get("message_id")
            matches: list[dict[str, object]] = []
            for turn in turns:
                assert _is_object(turn)
                if type(expected_turn) is str and turn.get("id") != expected_turn:
                    continue
                if _turn_client_message_count(turn, expected_message) == 1:
                    matches.append(turn)
            if len(matches) != 1:
                return self._unknown_for_operation(operation_id, "codex_reconcile_ambiguous")
            turn = matches[0]
            if type(expected_turn) is not str:
                observed_turn = turn.get("id")
                if type(observed_turn) is not str:
                    return self._unknown_for_operation(operation_id, "codex_reconcile_ambiguous")
                operation["provider_turn_id"] = observed_turn
            status = turn.get("status")
            if status == "inProgress":
                return self._unknown_for_operation(operation_id, "codex_turn_still_in_progress")
            observation = self._terminal_observation(
                operation_id, turn, _turn_items(turn), "codex_thread_read_reconciled"
            )
            operation["observation"] = observation.to_primitive()
            self._save_state()
            return observation
        except (CodexAppServerError, OSError, ValueError) as exc:
            return self._unknown_for_operation(
                operation_id, f"codex_reconcile_failed:{_error_code(exc)}"
            )
        finally:
            if recovery is not None:
                recovery.close()

    def _terminal_observation(
        self,
        operation_id: str,
        turn: dict[str, object],
        items: list[dict[str, object]],
        evidence: str,
    ) -> TurnObservation:
        operation = self._operation(operation_id)
        command = operation.get("command")
        assert _is_object(command)
        status = turn.get("status")
        state = {
            "completed": TurnState.DONE,
            "interrupted": TurnState.CANCELLED,
            "failed": TurnState.FAILED,
        }.get(status)
        if state is None:
            return self._unknown_for_operation(operation_id, "codex_turn_not_terminal")
        result_digest: str | None = None
        result_evidence = evidence
        if state is TurnState.DONE:
            try:
                result = _structured_result(items, self._config.output_schema or CODEX_COMPLETION_SCHEMA)
            except CodexAppServerError as exc:
                state = TurnState.FAILED
                result_evidence = exc.code
            else:
                result_digest = _sha256(canonical_json_bytes(result))
                operation["structured_result"] = result
        return TurnObservation(
            operation_id=operation_id,
            status=EffectStatus.APPLIED,
            observed_at=self._clock(),
            state=state,
            effect_digest=_effect_digest("send_task_packet", operation["command_hash"]),
            attempt_id=command["attempt_id"],
            channel_id=command["channel_id"],
            message_id=command["message_id"],
            turn_id=command["logical_turn_id"],
            result_digest=result_digest,
            evidence=(result_evidence,),
        )

    def _applied_turn(
        self,
        operation_id: str,
        state: TurnState,
        command_hash: str,
        *,
        evidence: tuple[str, ...],
    ) -> TurnObservation:
        command = self._operation(operation_id).get("command")
        assert _is_object(command)
        return TurnObservation(
            operation_id=operation_id,
            status=EffectStatus.APPLIED,
            observed_at=self._clock(),
            state=state,
            effect_digest=_effect_digest("send_task_packet", command_hash),
            attempt_id=command["attempt_id"],
            channel_id=command["channel_id"],
            message_id=command["message_id"],
            turn_id=command["logical_turn_id"],
            evidence=evidence,
        )

    def _unknown_for_operation(self, operation_id: str, reason: str) -> TurnObservation:
        observation = TurnObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            state=TurnState.UNKNOWN,
            evidence=(reason,),
        )
        self._operation(operation_id)["observation"] = observation.to_primitive()
        self._save_state()
        return observation

    def _reservation_observation(self) -> ChannelObservation | None:
        operation_id = self._state.get("reservation")
        if type(operation_id) is not str:
            return None
        return self._channel_observation(self._operation(operation_id))

    def _existing(
        self, operation_id: str, command_hash: str, kind: str
    ) -> dict[str, object] | None:
        operation = self._state["operations"].get(operation_id)
        if operation is None:
            return None
        if (
            _is_object(operation)
            and operation.get("kind") == kind
            and operation.get("command_hash") == command_hash
        ):
            return operation
        return {
            "observation": (
                ChannelObservation(
                    operation_id=operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    evidence=("operation_id_collision",),
                ).to_primitive()
                if kind == "reservation"
                else TurnObservation(
                    operation_id=operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at=self._clock(),
                    state=TurnState.UNKNOWN,
                    evidence=("operation_id_collision",),
                ).to_primitive()
            )
        }

    def _unknown_channel(
        self,
        operation_id: str,
        command_hash: str,
        reason: str,
        *,
        replace: bool = False,
    ) -> ChannelObservation:
        observation = ChannelObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            evidence=(reason,),
        )
        if replace and operation_id in self._state["operations"]:
            self._operation(operation_id)["observation"] = observation.to_primitive()
        else:
            self._put_operation(operation_id, "reservation", command_hash, observation.to_primitive())
        self._save_state()
        return observation

    def _unknown_turn(
        self, operation_id: str, command_hash: str, kind: str, reason: str
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

    def _put_operation(
        self,
        operation_id: str,
        kind: str,
        command_hash: str,
        observation: dict[str, object],
        *,
        command: dict[str, object] | None = None,
    ) -> None:
        value: dict[str, object] = {
            "kind": kind,
            "command_hash": command_hash,
            "observation": observation,
        }
        if command is not None:
            value["command"] = command
        self._state["operations"][operation_id] = value

    def _operation(self, operation_id: str) -> dict[str, object]:
        operation = self._state["operations"].get(operation_id)
        if not _is_object(operation):
            raise CodexAppServerError("codex_operation_missing", operation_id)
        return operation

    @staticmethod
    def _require_effect(effect: object, command_type: type[object]) -> PreparedEffect:
        if type(effect) is not PreparedEffect or type(effect.command) is not command_type:
            raise TypeError(f"effect command must be {command_type.__name__}")
        return effect

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if type(operation_id) is not str or not operation_id:
            raise ValueError("operation_id must be non-empty")

    @staticmethod
    def _channel_observation(operation: dict[str, object]) -> ChannelObservation:
        value = operation.get("observation")
        if not _is_object(value):
            raise CodexAppServerError("codex_observation_invalid")
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
            raise CodexAppServerError("codex_observation_invalid")
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

    def _load_state(self) -> dict[str, object]:
        if not self._state_path.exists():
            return {"schema_version": _STATE_SCHEMA_VERSION, "operations": {}}
        if self._state_path.is_symlink() or self._state_path.stat().st_size > _MAX_STATE_BYTES:
            raise CodexAppServerError("codex_state_unsafe")
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CodexAppServerError("codex_state_invalid") from exc
        if (
            not _is_object(value)
            or value.get("schema_version") != _STATE_SCHEMA_VERSION
            or not _is_object(value.get("operations"))
        ):
            raise CodexAppServerError("codex_state_invalid")
        return value

    def _save_state(self) -> None:
        self._config.state_directory.mkdir(parents=True, exist_ok=True)
        raw = canonical_json_bytes(self._state)
        if len(raw) > _MAX_STATE_BYTES:
            raise CodexAppServerError("codex_state_too_large")
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, self._state_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _completed_frame_matches(
    frame: dict[str, object], thread_id: str, turn_id: str
) -> bool:
    if frame.get("method") != "turn/completed":
        return False
    params = frame.get("params")
    turn = params.get("turn") if _is_object(params) else None
    return (
        _is_object(params)
        and params.get("threadId") == thread_id
        and _is_object(turn)
        and turn.get("id") == turn_id
    )


def _turn_items(turn: dict[str, object]) -> list[dict[str, object]]:
    items = turn.get("items")
    if type(items) is not list:
        return []
    return [dict(item) for item in items if _is_object(item)]


def _items_from_notifications(
    frames: tuple[dict[str, object], ...]
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for frame in frames:
        params = frame.get("params")
        item = params.get("item") if _is_object(params) else None
        if _is_object(item):
            result.append(dict(item))
    return result


def _turn_client_message_count(turn: dict[str, object], message_id: object) -> int:
    if type(message_id) is not str:
        return 0
    return sum(
        1
        for item in _turn_items(turn)
        if item.get("type") == "userMessage" and item.get("clientId") == message_id
    )


def _structured_result(
    items: list[dict[str, object]], output_schema: dict[str, object]
) -> dict[str, object]:
    final_messages = [
        item.get("text")
        for item in items
        if item.get("type") == "agentMessage"
        and item.get("phase") != "commentary"
        and type(item.get("text")) is str
    ]
    if not final_messages:
        raise CodexAppServerError("codex_structured_result_missing")
    text = final_messages[-1]
    assert type(text) is str
    if len(text.encode("utf-8")) > _MAX_FRAME_BYTES:
        raise CodexAppServerError("codex_structured_result_too_large")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CodexAppServerError("codex_structured_result_invalid_json") from exc
    if not _is_object(value):
        raise CodexAppServerError("codex_structured_result_not_object")
    try:
        _validate_json_schema(value, output_schema, "$")
    except ValueError as exc:
        raise CodexAppServerError("codex_structured_result_schema_mismatch", str(exc)) from exc
    return value


def _validate_schema_definition(schema: object) -> None:
    if not _is_object(schema):
        raise TypeError("output_schema must be an object")
    raw = canonical_json_bytes(schema)
    if len(raw) > 64 * 1_024:
        raise ValueError("output_schema is too large")
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError("output_schema must be a closed object schema")
    _validate_schema_node(schema, 0)


def _validate_schema_node(schema: dict[str, object], depth: int) -> None:
    if depth > 32:
        raise ValueError("output_schema is too deep")
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
    if set(schema) - allowed:
        raise ValueError("output_schema uses unsupported keywords")
    value_type = schema.get("type")
    if value_type not in {"object", "array", "string", "integer", "boolean", "null"}:
        raise ValueError("output_schema has an unsupported type")
    enum = schema.get("enum")
    if enum is not None and (type(enum) is not list or not enum):
        raise ValueError("output_schema enum must be non-empty")
    if value_type == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not _is_object(properties) or type(required) is not list:
            raise ValueError("object schemas require properties and required")
        if not all(type(item) is str and item in properties for item in required):
            raise ValueError("object schema required fields must exist")
        if schema.get("additionalProperties") is not False:
            raise ValueError("object schemas must reject additional properties")
        for child in properties.values():
            if not _is_object(child):
                raise ValueError("property schemas must be objects")
            _validate_schema_node(child, depth + 1)
    elif value_type == "array":
        items = schema.get("items")
        if not _is_object(items):
            raise ValueError("array schemas require an item schema")
        _validate_schema_node(items, depth + 1)


def _validate_json_schema(value: object, schema: dict[str, object], path: str) -> None:
    value_type = schema["type"]
    valid = {
        "object": _is_object(value),
        "array": type(value) is list,
        "string": type(value) is str,
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }[value_type]
    if not valid:
        raise ValueError(f"{path} must be {value_type}")
    enum = schema.get("enum")
    if type(enum) is list and value not in enum:
        raise ValueError(f"{path} is outside the enum")
    if type(value) is str:
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if type(minimum) is int and len(value) < minimum:
            raise ValueError(f"{path} is too short")
        if type(maximum) is int and len(value) > maximum:
            raise ValueError(f"{path} is too long")
    if type(value) is list:
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if type(minimum) is int and len(value) < minimum:
            raise ValueError(f"{path} has too few items")
        if type(maximum) is int and len(value) > maximum:
            raise ValueError(f"{path} has too many items")
        item_schema = schema["items"]
        assert _is_object(item_schema)
        for index, item in enumerate(value):
            _validate_json_schema(item, item_schema, f"{path}[{index}]")
    if _is_object(value):
        properties = schema["properties"]
        required = schema["required"]
        assert _is_object(properties) and type(required) is list
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"{path} is missing {missing[0]}")
        extra = set(value) - set(properties)
        if extra:
            raise ValueError(f"{path} has additional properties")
        for key, item in value.items():
            child = properties[key]
            assert _is_object(child)
            _validate_json_schema(item, child, f"{path}.{key}")


def _error_code(exc: BaseException) -> str:
    return exc.code if isinstance(exc, CodexAppServerError) else type(exc).__name__


__all__ = [
    "CODEX_COMPLETION_SCHEMA",
    "CodexAppServerChannel",
    "CodexAppServerClient",
    "CodexAppServerConfig",
    "CodexAppServerError",
    "CodexAppServerLaunch",
    "CodexClientPort",
]
