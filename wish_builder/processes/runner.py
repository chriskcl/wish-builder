"""Typed, bounded, no-shell task process runner."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from wish_builder.contracts.runtime import OutcomeKind, RuntimeReasonCode
from wish_builder.contracts.serialization import canonical_json_bytes

from .containment import (
    AttachResult,
    ContainmentCapability,
    ContainmentSession,
    ContainmentStatus,
    KillResult,
    ProcessIdentity,
    TreeState,
    create_containment_session,
)

RESULT_FD_ENV = "WISH_BUILDER_RESULT_FD"
RESULT_HANDLE_ENV = "WISH_BUILDER_RESULT_HANDLE"
_RESERVED_ENVIRONMENT = frozenset({RESULT_FD_ENV, RESULT_HANDLE_ENV})
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_READ_SIZE = 64 * 1024
_MAX_ARGUMENTS = 4096
_MAX_ARGUMENT_LENGTH = 128 * 1024
_MAX_ENVIRONMENT_VALUE_LENGTH = 1024 * 1024
_MAX_TIMEOUT_SECONDS = 24 * 60 * 60


class ProcessConfigurationError(ValueError):
    """A stable error raised while creating a typed launch request."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ProcessOutcomeStatus(StrEnum):
    SUCCESS = "success"
    EXIT_FAILURE = "exit_failure"
    START_FAILED = "start_failed"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    CONTAINMENT_UNSUPPORTED = "containment_unsupported"
    CONTAINMENT_UNKNOWN = "containment_unknown"


class ProcessFailure(StrEnum):
    EXECUTABLE_UNAVAILABLE = "executable_unavailable"
    EXECUTABLE_IDENTITY_MISMATCH = "executable_identity_mismatch"
    CWD_UNAVAILABLE = "cwd_unavailable"
    CWD_IDENTITY_MISMATCH = "cwd_identity_mismatch"
    ENVIRONMENT_DENIED = "environment_denied"
    STDIO_SETUP_FAILED = "stdio_setup_failed"
    LAUNCH_FAILED = "launch_failed"
    CONTAINMENT_UNSUPPORTED = "containment_unsupported"
    CONTAINMENT_UNKNOWN = "containment_unknown"
    TREE_TERMINATION_UNKNOWN = "tree_termination_unknown"
    CAPTURE_INCOMPLETE = "capture_incomplete"
    TIMED_OUT = "timed_out"
    STDOUT_LIMIT_EXCEEDED = "stdout_limit_exceeded"
    STDERR_LIMIT_EXCEEDED = "stderr_limit_exceeded"
    RESULT_LIMIT_EXCEEDED = "result_limit_exceeded"
    NONZERO_EXIT = "nonzero_exit"


class StreamName(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    RESULT = "result"


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    lexical_path: str
    canonical_path: str
    link_device: int
    link_inode: int
    target_device: int
    target_inode: int
    target_mode: int
    byte_length: int
    modified_ns: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.lexical_path) is not str or not self.lexical_path:
            raise ValueError("lexical_path must be non-empty")
        if type(self.canonical_path) is not str or not self.canonical_path:
            raise ValueError("canonical_path must be non-empty")
        for name in (
            "link_device",
            "link_inode",
            "target_device",
            "target_inode",
            "target_mode",
            "byte_length",
            "modified_ns",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if (
            type(self.sha256) is not str
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    lexical_path: str
    canonical_path: str
    link_device: int
    link_inode: int
    target_device: int
    target_inode: int
    target_mode: int

    def __post_init__(self) -> None:
        if type(self.lexical_path) is not str or not self.lexical_path:
            raise ValueError("lexical_path must be non-empty")
        if type(self.canonical_path) is not str or not self.canonical_path:
            raise ValueError("canonical_path must be non-empty")
        for name in (
            "link_device",
            "link_inode",
            "target_device",
            "target_inode",
            "target_mode",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be non-negative")


def _absolute_lexical_path(path: str | os.PathLike[str]) -> Path:
    try:
        lexical = Path(path).expanduser().absolute()
    except (OSError, TypeError, ValueError) as exc:
        raise ProcessConfigurationError("invalid_path", str(path)) from exc
    return lexical


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path))


def capture_executable_identity(
    path: str | os.PathLike[str],
) -> ExecutableIdentity:
    lexical = _absolute_lexical_path(path)
    try:
        resolved = lexical.resolve(strict=True)
        link_stat = os.lstat(lexical)
        target_stat = os.stat(resolved)
    except (FileNotFoundError, OSError) as exc:
        raise ProcessConfigurationError("executable_unavailable", str(lexical)) from exc
    if not stat.S_ISREG(target_stat.st_mode):
        raise ProcessConfigurationError("executable_not_regular", str(resolved))
    if os.name == "posix" and not os.access(resolved, os.X_OK):
        raise ProcessConfigurationError("executable_not_executable", str(resolved))
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
        with open(descriptor, "rb", closefd=True) as handle:
            opened_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_stat.st_mode) or (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) != (target_stat.st_dev, target_stat.st_ino):
                raise ProcessConfigurationError(
                    "executable_identity_race",
                    str(resolved),
                )
            while chunk := handle.read(_READ_SIZE):
                digest.update(chunk)
    except ProcessConfigurationError:
        raise
    except OSError as exc:
        raise ProcessConfigurationError("executable_unreadable", str(resolved)) from exc
    return ExecutableIdentity(
        lexical_path=_normalized_path(lexical),
        canonical_path=_normalized_path(resolved),
        link_device=int(link_stat.st_dev),
        link_inode=int(link_stat.st_ino),
        target_device=int(target_stat.st_dev),
        target_inode=int(target_stat.st_ino),
        target_mode=stat.S_IMODE(target_stat.st_mode),
        byte_length=int(target_stat.st_size),
        modified_ns=int(target_stat.st_mtime_ns),
        sha256=digest.hexdigest(),
    )


def capture_directory_identity(path: str | os.PathLike[str]) -> DirectoryIdentity:
    lexical = _absolute_lexical_path(path)
    try:
        resolved = lexical.resolve(strict=True)
        link_stat = os.lstat(lexical)
        target_stat = os.stat(resolved)
    except (FileNotFoundError, OSError) as exc:
        raise ProcessConfigurationError("cwd_unavailable", str(lexical)) from exc
    if not stat.S_ISDIR(target_stat.st_mode):
        raise ProcessConfigurationError("cwd_not_directory", str(resolved))
    return DirectoryIdentity(
        lexical_path=_normalized_path(lexical),
        canonical_path=_normalized_path(resolved),
        link_device=int(link_stat.st_dev),
        link_inode=int(link_stat.st_ino),
        target_device=int(target_stat.st_dev),
        target_inode=int(target_stat.st_ino),
        target_mode=stat.S_IMODE(target_stat.st_mode),
    )


@dataclass(frozen=True, slots=True)
class EnvironmentVariable:
    name: str
    value: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or not _ENVIRONMENT_NAME.fullmatch(self.name):
            raise ProcessConfigurationError("invalid_environment_name", str(self.name))
        if self.name in _RESERVED_ENVIRONMENT:
            raise ProcessConfigurationError("reserved_environment_name", self.name)
        if type(self.value) is not str or "\x00" in self.value:
            raise ProcessConfigurationError("invalid_environment_value", self.name)
        if len(self.value) > _MAX_ENVIRONMENT_VALUE_LENGTH:
            raise ProcessConfigurationError("environment_value_too_large", self.name)


@dataclass(frozen=True, slots=True)
class StreamLimits:
    max_bytes: int
    max_frames: int
    diagnostic_tail_bytes: int = 4096

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_frames", "diagnostic_tail_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ProcessConfigurationError("invalid_stream_limit", name)


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    stdout: StreamLimits
    stderr: StreamLimits
    result: StreamLimits

    def __post_init__(self) -> None:
        if not all(
            type(value) is StreamLimits
            for value in (self.stdout, self.stderr, self.result)
        ):
            raise TypeError("all process limits must be StreamLimits")

    @classmethod
    def defaults(cls) -> ProcessLimits:
        return cls(
            stdout=StreamLimits(1024 * 1024, 10_000),
            stderr=StreamLimits(1024 * 1024, 10_000),
            result=StreamLimits(256 * 1024, 1024),
        )


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    executable: ExecutableIdentity
    cwd: DirectoryIdentity
    argv: tuple[str, ...]
    environment: tuple[EnvironmentVariable, ...]
    timeout_seconds: float
    limits: ProcessLimits

    def __post_init__(self) -> None:
        if type(self.executable) is not ExecutableIdentity:
            raise TypeError("executable must be an ExecutableIdentity")
        if type(self.cwd) is not DirectoryIdentity:
            raise TypeError("cwd must be a DirectoryIdentity")
        if type(self.argv) is not tuple or not self.argv:
            raise ProcessConfigurationError(
                "invalid_argv", "argv must be a non-empty tuple"
            )
        if len(self.argv) > _MAX_ARGUMENTS:
            raise ProcessConfigurationError("invalid_argv", "too many arguments")
        for argument in self.argv:
            if (
                type(argument) is not str
                or "\x00" in argument
                or len(argument) > _MAX_ARGUMENT_LENGTH
            ):
                raise ProcessConfigurationError("invalid_argv", "invalid argument")
        if os.path.normcase(self.argv[0]) != self.executable.canonical_path:
            raise ProcessConfigurationError(
                "executable_argv_mismatch",
                "argv[0] must be the captured canonical executable path",
            )
        if type(self.environment) is not tuple or not all(
            type(item) is EnvironmentVariable for item in self.environment
        ):
            raise TypeError("environment must be a tuple of EnvironmentVariable values")
        comparison_names = [
            item.name.casefold() if os.name == "nt" else item.name
            for item in self.environment
        ]
        if len(set(comparison_names)) != len(comparison_names):
            raise ProcessConfigurationError("duplicate_environment_name")
        object.__setattr__(
            self,
            "environment",
            tuple(sorted(self.environment, key=lambda item: item.name)),
        )
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(float(self.timeout_seconds))
            or not 0 < float(self.timeout_seconds) <= _MAX_TIMEOUT_SECONDS
        ):
            raise ProcessConfigurationError("invalid_timeout")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if type(self.limits) is not ProcessLimits:
            raise TypeError("limits must be ProcessLimits")

    @classmethod
    def create(
        cls,
        *,
        executable: str | os.PathLike[str],
        arguments: tuple[str, ...] = (),
        cwd: str | os.PathLike[str],
        environment: tuple[EnvironmentVariable, ...] = (),
        timeout_seconds: float = 60.0,
        limits: ProcessLimits | None = None,
    ) -> ProcessRequest:
        if type(arguments) is not tuple:
            raise ProcessConfigurationError("invalid_argv", "arguments must be a tuple")
        executable_identity = capture_executable_identity(executable)
        return cls(
            executable=executable_identity,
            cwd=capture_directory_identity(cwd),
            argv=(executable_identity.canonical_path, *arguments),
            environment=environment,
            timeout_seconds=timeout_seconds,
            limits=ProcessLimits.defaults() if limits is None else limits,
        )


@dataclass(frozen=True, slots=True)
class CapturedStream:
    stream: StreamName
    data: bytes
    diagnostic_tail: bytes
    total_bytes: int
    frame_count: int
    byte_limit_exceeded: bool
    frame_limit_exceeded: bool
    complete: bool

    @property
    def limit_exceeded(self) -> bool:
        return self.byte_limit_exceeded or self.frame_limit_exceeded


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    status: ProcessOutcomeStatus
    kind: OutcomeKind
    reason_code: RuntimeReasonCode | None
    failure: ProcessFailure | None
    message_key: str | None
    executable: ExecutableIdentity
    cwd: DirectoryIdentity
    argv_sha256: str
    environment_names: tuple[str, ...]
    containment: ContainmentCapability
    process_identity: ProcessIdentity | None
    exit_code: int | None
    stdout: CapturedStream
    stderr: CapturedStream
    result: CapturedStream
    duration_seconds: float
    termination_attempted: bool
    termination_proven: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status is ProcessOutcomeStatus.SUCCESS:
            if self.kind is not OutcomeKind.SUCCESS or self.reason_code is not None:
                raise ValueError("successful process outcomes cannot carry a reason")
            if self.exit_code != 0 or not self.termination_proven:
                raise ValueError(
                    "success requires exit zero and proven tree termination"
                )
        elif self.kind is OutcomeKind.SUCCESS or self.reason_code is None:
            raise ValueError("failed process outcomes require a kind and reason")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")

    @property
    def admitted_result(self) -> bytes | None:
        return self.result.data if self.status is ProcessOutcomeStatus.SUCCESS else None


class _StreamCollector:
    def __init__(
        self,
        stream: StreamName,
        handle: BinaryIO,
        limits: StreamLimits,
        activity: threading.Event,
    ) -> None:
        self.stream = stream
        self.handle = handle
        self.limits = limits
        self.activity = activity
        self.total_bytes = 0
        self.newline_count = 0
        self.last_byte: int | None = None
        self.prefix = bytearray()
        self.tail = bytearray()
        self.byte_limit_exceeded = False
        self.frame_limit_exceeded = False
        self.complete = False
        self.error: str | None = None
        self._lock = threading.Lock()
        self.thread = threading.Thread(
            target=self._read,
            name=f"wish-builder-{stream.value}-reader",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def _read(self) -> None:
        try:
            descriptor = self.handle.fileno()
            while chunk := os.read(descriptor, _READ_SIZE):
                with self._lock:
                    self.total_bytes += len(chunk)
                    self.newline_count += chunk.count(b"\n")
                    self.last_byte = chunk[-1]
                    remaining = self.limits.max_bytes - len(self.prefix)
                    if remaining > 0:
                        self.prefix.extend(chunk[:remaining])
                    self.tail.extend(chunk)
                    if len(self.tail) > self.limits.diagnostic_tail_bytes:
                        del self.tail[: -self.limits.diagnostic_tail_bytes]
                    self.byte_limit_exceeded = self.total_bytes > self.limits.max_bytes
                    self.frame_limit_exceeded = (
                        self._frame_count_unlocked() > self.limits.max_frames
                    )
                self.activity.set()
        except (OSError, ValueError) as exc:
            with self._lock:
                self.error = f"{type(exc).__name__}: {exc}"
            self.activity.set()
        finally:
            with self._lock:
                self.complete = self.error is None
            self.activity.set()

    def _frame_count_unlocked(self) -> int:
        return self.newline_count + (
            1 if self.total_bytes and self.last_byte != ord("\n") else 0
        )

    def snapshot(self) -> CapturedStream:
        with self._lock:
            return CapturedStream(
                stream=self.stream,
                data=bytes(self.prefix),
                diagnostic_tail=bytes(self.tail),
                total_bytes=self.total_bytes,
                frame_count=self._frame_count_unlocked(),
                byte_limit_exceeded=self.byte_limit_exceeded,
                frame_limit_exceeded=self.frame_limit_exceeded,
                complete=self.complete,
            )


def _empty_capture(stream: StreamName) -> CapturedStream:
    return CapturedStream(stream, b"", b"", 0, 0, False, False, True)


def _argv_hash(argv: tuple[str, ...]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(list(argv))).hexdigest()


class ProcessRunner:
    """Run one pre-identified task process and admit only proven outcomes."""

    def __init__(
        self,
        *,
        environment_allowlist: tuple[str, ...] = (),
        containment_factory: Callable[[], ContainmentSession] = (
            create_containment_session
        ),
        monotonic: Callable[[], float] = time.monotonic,
        termination_grace_seconds: float = 5.0,
        poll_interval_seconds: float = 0.01,
    ) -> None:
        if type(environment_allowlist) is not tuple:
            raise ProcessConfigurationError("invalid_environment_allowlist")
        for name in environment_allowlist:
            if type(name) is not str or not _ENVIRONMENT_NAME.fullmatch(name):
                raise ProcessConfigurationError(
                    "invalid_environment_allowlist", str(name)
                )
            if name in _RESERVED_ENVIRONMENT:
                raise ProcessConfigurationError("reserved_environment_name", name)
        normalized = [
            name.casefold() if os.name == "nt" else name
            for name in environment_allowlist
        ]
        if len(set(normalized)) != len(normalized):
            raise ProcessConfigurationError("duplicate_environment_allowlist")
        if (
            type(termination_grace_seconds) not in (int, float)
            or not math.isfinite(float(termination_grace_seconds))
            or termination_grace_seconds <= 0
        ):
            raise ProcessConfigurationError("invalid_termination_grace")
        if (
            type(poll_interval_seconds) not in (int, float)
            or not math.isfinite(float(poll_interval_seconds))
            or poll_interval_seconds <= 0
        ):
            raise ProcessConfigurationError("invalid_poll_interval")
        self._environment_allowlist = frozenset(normalized)
        self._containment_factory = containment_factory
        self._monotonic = monotonic
        self._termination_grace_seconds = float(termination_grace_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)

    def _environment(
        self, request: ProcessRequest
    ) -> tuple[dict[str, str] | None, str | None]:
        environment: dict[str, str] = {}
        for variable in request.environment:
            name = variable.name.casefold() if os.name == "nt" else variable.name
            if name not in self._environment_allowlist:
                return None, variable.name
            environment[variable.name] = variable.value
        return environment, None

    @staticmethod
    def _close_session(session: ContainmentSession) -> None:
        try:
            session.close()
        except Exception:  # noqa: BLE001 - adapter cleanup is a crash barrier
            # Closing is a final best effort.  Any path where containment
            # proof matters has already been classified before this call.
            return

    @staticmethod
    def _tree_state(session: ContainmentSession) -> TreeState:
        try:
            state = session.tree_state()
        except Exception:  # noqa: BLE001 - adapter probes fail closed
            return TreeState.UNKNOWN
        return state if type(state) is TreeState else TreeState.UNKNOWN

    def _terminate_started_process(
        self,
        session: ContainmentSession,
        process: subprocess.Popen[bytes],
        *,
        abort_start: bool = False,
    ) -> tuple[bool, str | None]:
        uncertain = False
        detail: str | None = None
        try:
            if abort_start:
                session.abort_start(process)
            else:
                kill_result = session.kill_tree()
                if type(kill_result) is not KillResult:
                    uncertain = True
                    detail = "containment backend returned an invalid kill result"
                elif kill_result.state is TreeState.UNKNOWN:
                    uncertain = True
                    detail = kill_result.detail
        except Exception as exc:  # noqa: BLE001 - adapter termination crash barrier
            uncertain = True
            detail = f"containment termination raised {type(exc).__name__}: {exc}"
            try:
                process.kill()
            except OSError:
                pass

        wait_event = threading.Event()
        kill_deadline = self._monotonic() + self._termination_grace_seconds
        while self._monotonic() < kill_deadline:
            root_exit = process.poll()
            tree_state = self._tree_state(session)
            if root_exit is not None and tree_state is TreeState.EMPTY:
                return not uncertain, detail
            wait_event.wait(self._poll_interval_seconds)
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
        return False, detail or "complete process-tree termination was not proven"

    def _base_outcome(
        self,
        request: ProcessRequest,
        *,
        status: ProcessOutcomeStatus,
        kind: OutcomeKind,
        reason_code: RuntimeReasonCode | None,
        failure: ProcessFailure | None,
        message_key: str | None,
        containment: ContainmentCapability,
        started_at: float,
        process_identity: ProcessIdentity | None = None,
        exit_code: int | None = None,
        stdout: CapturedStream | None = None,
        stderr: CapturedStream | None = None,
        result: CapturedStream | None = None,
        termination_attempted: bool = False,
        termination_proven: bool = False,
        detail: str | None = None,
    ) -> ProcessOutcome:
        return ProcessOutcome(
            status=status,
            kind=kind,
            reason_code=reason_code,
            failure=failure,
            message_key=message_key,
            executable=request.executable,
            cwd=request.cwd,
            argv_sha256=_argv_hash(request.argv),
            environment_names=tuple(item.name for item in request.environment),
            containment=containment,
            process_identity=process_identity,
            exit_code=exit_code,
            stdout=_empty_capture(StreamName.STDOUT) if stdout is None else stdout,
            stderr=_empty_capture(StreamName.STDERR) if stderr is None else stderr,
            result=_empty_capture(StreamName.RESULT) if result is None else result,
            duration_seconds=max(0.0, self._monotonic() - started_at),
            termination_attempted=termination_attempted,
            termination_proven=termination_proven,
            detail=None if detail is None else detail[:2048],
        )

    def _identity_failure(
        self,
        request: ProcessRequest,
        capability: ContainmentCapability,
        started_at: float,
    ) -> ProcessOutcome | None:
        try:
            executable = capture_executable_identity(request.executable.lexical_path)
        except ProcessConfigurationError as exc:
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.START_FAILED,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                failure=ProcessFailure.EXECUTABLE_UNAVAILABLE,
                message_key="process.executable_unavailable",
                containment=capability,
                started_at=started_at,
                detail=exc.code,
            )
        if executable != request.executable:
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.START_FAILED,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                failure=ProcessFailure.EXECUTABLE_IDENTITY_MISMATCH,
                message_key="process.executable_identity_mismatch",
                containment=capability,
                started_at=started_at,
            )
        try:
            cwd = capture_directory_identity(request.cwd.lexical_path)
        except ProcessConfigurationError as exc:
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.START_FAILED,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                failure=ProcessFailure.CWD_UNAVAILABLE,
                message_key="process.cwd_unavailable",
                containment=capability,
                started_at=started_at,
                detail=exc.code,
            )
        if cwd != request.cwd:
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.START_FAILED,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                failure=ProcessFailure.CWD_IDENTITY_MISMATCH,
                message_key="process.cwd_identity_mismatch",
                containment=capability,
                started_at=started_at,
            )
        return None

    def run(self, request: ProcessRequest) -> ProcessOutcome:
        if type(request) is not ProcessRequest:
            raise TypeError("request must be a ProcessRequest")
        started_at = self._monotonic()
        deadline = started_at + request.timeout_seconds
        session: ContainmentSession | None = None
        try:
            session = self._containment_factory()
            capability = session.capability
            if type(capability) is not ContainmentCapability:
                raise TypeError("containment capability has an invalid type")
        except Exception as exc:  # noqa: BLE001 - containment factory crash barrier
            if session is not None:
                self._close_session(session)
            capability = ContainmentCapability(
                ContainmentStatus.UNKNOWN,
                "containment_factory",
                f"containment setup raised {type(exc).__name__}: {exc}",
            )
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.CONTAINMENT_UNKNOWN,
                kind=OutcomeKind.BLOCKED,
                reason_code=RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
                failure=ProcessFailure.CONTAINMENT_UNKNOWN,
                message_key="process.containment_unknown",
                containment=capability,
                started_at=started_at,
                detail=capability.detail,
            )
        if capability.status is not ContainmentStatus.PROVEN:
            self._close_session(session)
            status = (
                ProcessOutcomeStatus.CONTAINMENT_UNSUPPORTED
                if capability.status is ContainmentStatus.UNSUPPORTED
                else ProcessOutcomeStatus.CONTAINMENT_UNKNOWN
            )
            failure = (
                ProcessFailure.CONTAINMENT_UNSUPPORTED
                if capability.status is ContainmentStatus.UNSUPPORTED
                else ProcessFailure.CONTAINMENT_UNKNOWN
            )
            return self._base_outcome(
                request,
                status=status,
                kind=OutcomeKind.BLOCKED,
                reason_code=RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
                failure=failure,
                message_key=f"process.{failure.value}",
                containment=capability,
                started_at=started_at,
                detail=capability.detail,
            )

        identity_failure = self._identity_failure(
            request,
            capability,
            started_at,
        )
        if identity_failure is not None:
            self._close_session(session)
            return identity_failure
        environment, denied_name = self._environment(request)
        if environment is None:
            self._close_session(session)
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.START_FAILED,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                failure=ProcessFailure.ENVIRONMENT_DENIED,
                message_key="process.environment_denied",
                containment=capability,
                started_at=started_at,
                detail=denied_name,
            )
        if self._monotonic() >= deadline:
            self._close_session(session)
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.TIMED_OUT,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.EXTERNAL_TIMEOUT,
                failure=ProcessFailure.TIMED_OUT,
                message_key="process.timed_out",
                containment=capability,
                started_at=started_at,
                termination_proven=True,
                detail="deadline expired before process launch",
            )

        result_read_fd = -1
        result_write_fd = -1
        try:
            result_read_fd, result_write_fd = os.pipe()
            os.set_inheritable(result_write_fd, True)
        except OSError as exc:
            for descriptor in (result_read_fd, result_write_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            self._close_session(session)
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.START_FAILED,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                failure=ProcessFailure.STDIO_SETUP_FAILED,
                message_key="process.stdio_setup_failed",
                containment=capability,
                started_at=started_at,
                detail=str(exc),
            )

        assert session is not None
        popen_options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": request.cwd.canonical_path,
            "env": environment,
            "shell": False,
            "close_fds": True,
        }
        try:
            if session.start_new_session:
                popen_options["start_new_session"] = True
            if session.creation_flags:
                popen_options["creationflags"] = session.creation_flags
            if os.name == "nt":
                import msvcrt

                result_handle = msvcrt.get_osfhandle(result_write_fd)
                os.set_handle_inheritable(result_handle, True)
                startup_info = subprocess.STARTUPINFO()
                startup_info.lpAttributeList = {"handle_list": [result_handle]}
                popen_options["startupinfo"] = startup_info
                environment[RESULT_HANDLE_ENV] = str(result_handle)
            else:
                popen_options["pass_fds"] = (result_write_fd,)
                environment[RESULT_FD_ENV] = str(result_write_fd)
        except Exception as exc:  # noqa: BLE001 - stdio setup crash barrier
            for descriptor in (result_read_fd, result_write_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._close_session(session)
            return self._base_outcome(
                request,
                status=ProcessOutcomeStatus.START_FAILED,
                kind=OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                failure=ProcessFailure.STDIO_SETUP_FAILED,
                message_key="process.stdio_setup_failed",
                containment=capability,
                started_at=started_at,
                detail=f"{type(exc).__name__}: {exc}",
            )

        process: subprocess.Popen[bytes] | None = None
        result_handle_file: BinaryIO | None = None
        try:
            try:
                process = subprocess.Popen(list(request.argv), **popen_options)  # type: ignore[arg-type]
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return self._base_outcome(
                    request,
                    status=ProcessOutcomeStatus.START_FAILED,
                    kind=OutcomeKind.TERMINAL,
                    reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                    failure=ProcessFailure.LAUNCH_FAILED,
                    message_key="process.launch_failed",
                    containment=capability,
                    started_at=started_at,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            try:
                attachment = session.attach(process)
                if type(attachment) is not AttachResult:
                    raise TypeError(
                        "containment backend returned an invalid attachment"
                    )
            except Exception as exc:  # noqa: BLE001 - containment adapter crash barrier
                termination_proven, termination_detail = (
                    self._terminate_started_process(
                        session,
                        process,
                        abort_start=True,
                    )
                )
                detail = f"containment attach raised {type(exc).__name__}: {exc}"
                if termination_detail:
                    detail = f"{detail}; {termination_detail}"
                return self._base_outcome(
                    request,
                    status=ProcessOutcomeStatus.CONTAINMENT_UNKNOWN,
                    kind=OutcomeKind.BLOCKED,
                    reason_code=RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
                    failure=ProcessFailure.CONTAINMENT_UNKNOWN,
                    message_key="process.containment_unknown",
                    containment=ContainmentCapability(
                        ContainmentStatus.UNKNOWN,
                        capability.backend,
                        detail,
                    ),
                    started_at=started_at,
                    exit_code=process.poll(),
                    termination_attempted=True,
                    termination_proven=termination_proven,
                    detail=detail,
                )
            if attachment.status is not ContainmentStatus.PROVEN:
                termination_proven, termination_detail = (
                    self._terminate_started_process(
                        session,
                        process,
                        abort_start=True,
                    )
                )
                status = (
                    ProcessOutcomeStatus.CONTAINMENT_UNSUPPORTED
                    if attachment.status is ContainmentStatus.UNSUPPORTED
                    else ProcessOutcomeStatus.CONTAINMENT_UNKNOWN
                )
                failure = (
                    ProcessFailure.CONTAINMENT_UNSUPPORTED
                    if attachment.status is ContainmentStatus.UNSUPPORTED
                    else ProcessFailure.CONTAINMENT_UNKNOWN
                )
                return self._base_outcome(
                    request,
                    status=status,
                    kind=OutcomeKind.BLOCKED,
                    reason_code=RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
                    failure=failure,
                    message_key=f"process.{failure.value}",
                    containment=ContainmentCapability(
                        attachment.status,
                        capability.backend,
                        attachment.detail or termination_detail,
                    ),
                    started_at=started_at,
                    exit_code=process.poll(),
                    termination_attempted=True,
                    termination_proven=termination_proven,
                    detail=attachment.detail or termination_detail,
                )

            assert attachment.identity is not None
            try:
                os.close(result_write_fd)
                result_write_fd = -1
            except OSError as exc:
                termination_proven, termination_detail = (
                    self._terminate_started_process(
                        session,
                        process,
                    )
                )
                detail = f"result channel close failed: {exc}"
                if termination_detail:
                    detail = f"{detail}; {termination_detail}"
                if termination_proven:
                    return self._base_outcome(
                        request,
                        status=ProcessOutcomeStatus.START_FAILED,
                        kind=OutcomeKind.TERMINAL,
                        reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                        failure=ProcessFailure.STDIO_SETUP_FAILED,
                        message_key="process.stdio_setup_failed",
                        containment=capability,
                        started_at=started_at,
                        process_identity=attachment.identity,
                        exit_code=process.poll(),
                        termination_attempted=True,
                        termination_proven=True,
                        detail=detail,
                    )
                return self._base_outcome(
                    request,
                    status=ProcessOutcomeStatus.CONTAINMENT_UNKNOWN,
                    kind=OutcomeKind.BLOCKED,
                    reason_code=RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
                    failure=ProcessFailure.TREE_TERMINATION_UNKNOWN,
                    message_key="process.tree_termination_unknown",
                    containment=ContainmentCapability(
                        ContainmentStatus.UNKNOWN,
                        capability.backend,
                        detail,
                    ),
                    started_at=started_at,
                    process_identity=attachment.identity,
                    exit_code=process.poll(),
                    termination_attempted=True,
                    termination_proven=False,
                    detail=detail,
                )
            collectors: tuple[_StreamCollector, ...] = ()
            try:
                if process.stdout is None or process.stderr is None:
                    raise OSError("subprocess stdio pipes are unavailable")
                result_handle_file = os.fdopen(result_read_fd, "rb", buffering=0)
                result_read_fd = -1
                activity = threading.Event()
                collectors = (
                    _StreamCollector(
                        StreamName.STDOUT,
                        process.stdout,
                        request.limits.stdout,
                        activity,
                    ),
                    _StreamCollector(
                        StreamName.STDERR,
                        process.stderr,
                        request.limits.stderr,
                        activity,
                    ),
                    _StreamCollector(
                        StreamName.RESULT,
                        result_handle_file,
                        request.limits.result,
                        activity,
                    ),
                )
                for collector in collectors:
                    collector.start()
            except Exception as exc:  # noqa: BLE001 - thread/stdio setup crash barrier
                termination_proven, termination_detail = (
                    self._terminate_started_process(
                        session,
                        process,
                    )
                )
                for collector in collectors:
                    if collector.thread.ident is not None:
                        collector.thread.join(timeout=self._poll_interval_seconds)
                detail = f"capture setup raised {type(exc).__name__}: {exc}"
                if termination_detail:
                    detail = f"{detail}; {termination_detail}"
                if termination_proven:
                    return self._base_outcome(
                        request,
                        status=ProcessOutcomeStatus.START_FAILED,
                        kind=OutcomeKind.TERMINAL,
                        reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
                        failure=ProcessFailure.STDIO_SETUP_FAILED,
                        message_key="process.stdio_setup_failed",
                        containment=capability,
                        started_at=started_at,
                        process_identity=attachment.identity,
                        exit_code=process.poll(),
                        termination_attempted=True,
                        termination_proven=True,
                        detail=detail,
                    )
                return self._base_outcome(
                    request,
                    status=ProcessOutcomeStatus.CONTAINMENT_UNKNOWN,
                    kind=OutcomeKind.BLOCKED,
                    reason_code=RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
                    failure=ProcessFailure.TREE_TERMINATION_UNKNOWN,
                    message_key="process.tree_termination_unknown",
                    containment=ContainmentCapability(
                        ContainmentStatus.UNKNOWN,
                        capability.backend,
                        detail,
                    ),
                    started_at=started_at,
                    process_identity=attachment.identity,
                    exit_code=process.poll(),
                    termination_attempted=True,
                    termination_proven=False,
                    detail=detail,
                )

            trigger: ProcessFailure | None = None
            trigger_status: ProcessOutcomeStatus | None = None
            trigger_reason: RuntimeReasonCode | None = None
            trigger_kind: OutcomeKind | None = None
            detail: str | None = None
            while True:
                snapshots = tuple(collector.snapshot() for collector in collectors)
                errors = tuple(
                    collector.error
                    for collector in collectors
                    if collector.error is not None
                )
                if errors:
                    trigger = ProcessFailure.CAPTURE_INCOMPLETE
                    trigger_status = ProcessOutcomeStatus.CONTAINMENT_UNKNOWN
                    trigger_reason = RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN
                    trigger_kind = OutcomeKind.BLOCKED
                    detail = errors[0]
                    break
                for snapshot, failure in zip(
                    snapshots,
                    (
                        ProcessFailure.STDOUT_LIMIT_EXCEEDED,
                        ProcessFailure.STDERR_LIMIT_EXCEEDED,
                        ProcessFailure.RESULT_LIMIT_EXCEEDED,
                    ),
                ):
                    if snapshot.limit_exceeded:
                        trigger = failure
                        trigger_status = ProcessOutcomeStatus.OUTPUT_LIMIT_EXCEEDED
                        trigger_reason = RuntimeReasonCode.OUTPUT_LIMIT_EXCEEDED
                        trigger_kind = OutcomeKind.BLOCKED
                        break
                if trigger is not None:
                    break

                root_exit = process.poll()
                all_complete = all(snapshot.complete for snapshot in snapshots)
                if root_exit is not None and all_complete:
                    tree_state = self._tree_state(session)
                    if tree_state is TreeState.EMPTY:
                        break
                    if tree_state is TreeState.UNKNOWN:
                        trigger = ProcessFailure.CONTAINMENT_UNKNOWN
                        trigger_status = ProcessOutcomeStatus.CONTAINMENT_UNKNOWN
                        trigger_reason = RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN
                        trigger_kind = OutcomeKind.BLOCKED
                        detail = "tree state became unknown after root exit"
                        break
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    trigger = ProcessFailure.TIMED_OUT
                    trigger_status = ProcessOutcomeStatus.TIMED_OUT
                    trigger_reason = RuntimeReasonCode.EXTERNAL_TIMEOUT
                    trigger_kind = OutcomeKind.TERMINAL
                    break
                activity.wait(min(self._poll_interval_seconds, remaining))
                activity.clear()

            termination_attempted = trigger is not None
            termination_proven = trigger is None
            kill_uncertain = False
            if trigger is not None:
                try:
                    kill_result = session.kill_tree()
                    if type(kill_result) is not KillResult:
                        raise TypeError(
                            "containment backend returned an invalid kill result"
                        )
                except Exception as exc:  # noqa: BLE001 - containment adapter crash barrier
                    kill_result = KillResult(
                        TreeState.UNKNOWN,
                        f"containment kill raised {type(exc).__name__}: {exc}",
                    )
                kill_uncertain = kill_result.state is TreeState.UNKNOWN
                if kill_result.detail:
                    detail = kill_result.detail
                kill_deadline = self._monotonic() + self._termination_grace_seconds
                while self._monotonic() < kill_deadline:
                    root_exit = process.poll()
                    tree_state = self._tree_state(session)
                    if (
                        root_exit is not None
                        and tree_state is TreeState.EMPTY
                        and all(
                            collector.snapshot().complete for collector in collectors
                        )
                    ):
                        termination_proven = not kill_uncertain
                        break
                    activity.wait(self._poll_interval_seconds)
                    activity.clear()
                if not termination_proven:
                    trigger = ProcessFailure.TREE_TERMINATION_UNKNOWN
                    trigger_status = ProcessOutcomeStatus.CONTAINMENT_UNKNOWN
                    trigger_reason = RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN
                    trigger_kind = OutcomeKind.BLOCKED
                    detail = (
                        detail or "complete process-tree termination was not proven"
                    )

            for collector in collectors:
                collector.thread.join(timeout=0)
            snapshots = tuple(collector.snapshot() for collector in collectors)
            if not all(snapshot.complete for snapshot in snapshots):
                trigger = ProcessFailure.CAPTURE_INCOMPLETE
                trigger_status = ProcessOutcomeStatus.CONTAINMENT_UNKNOWN
                trigger_reason = RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN
                trigger_kind = OutcomeKind.BLOCKED
                termination_proven = False
                detail = detail or "output readers did not reach EOF"
            exit_code = process.poll()
            if trigger is None:
                if exit_code == 0:
                    status = ProcessOutcomeStatus.SUCCESS
                    kind = OutcomeKind.SUCCESS
                    reason_code = None
                    failure = None
                    message_key = None
                else:
                    status = ProcessOutcomeStatus.EXIT_FAILURE
                    kind = OutcomeKind.TERMINAL
                    reason_code = RuntimeReasonCode.CHECK_FAILED
                    failure = ProcessFailure.NONZERO_EXIT
                    message_key = "process.nonzero_exit"
            else:
                assert trigger_status is not None
                assert trigger_reason is not None
                assert trigger_kind is not None
                status = trigger_status
                kind = trigger_kind
                reason_code = trigger_reason
                failure = trigger
                message_key = f"process.{trigger.value}"
            final_containment = capability
            if status is ProcessOutcomeStatus.CONTAINMENT_UNKNOWN:
                final_containment = ContainmentCapability(
                    ContainmentStatus.UNKNOWN,
                    capability.backend,
                    detail or "containment proof is incomplete",
                )
            return self._base_outcome(
                request,
                status=status,
                kind=kind,
                reason_code=reason_code,
                failure=failure,
                message_key=message_key,
                containment=final_containment,
                started_at=started_at,
                process_identity=attachment.identity,
                exit_code=exit_code,
                stdout=snapshots[0],
                stderr=snapshots[1],
                result=snapshots[2],
                termination_attempted=termination_attempted,
                termination_proven=termination_proven,
                detail=detail,
            )
        finally:
            for descriptor in (result_read_fd, result_write_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if process is not None:
                for handle in (process.stdout, process.stderr):
                    if handle is not None:
                        try:
                            handle.close()
                        except OSError:
                            pass
            if result_handle_file is not None:
                try:
                    result_handle_file.close()
                except OSError:
                    pass
            self._close_session(session)


def open_result_channel() -> BinaryIO:
    """Open the runner-provided result channel in a task child process."""

    if os.name == "nt":
        import msvcrt

        raw_handle = os.environ.get(RESULT_HANDLE_ENV)
        if raw_handle is None:
            raise RuntimeError("result channel is unavailable")
        try:
            handle = int(raw_handle, 10)
        except ValueError as exc:
            raise RuntimeError("result channel handle is invalid") from exc
        descriptor = msvcrt.open_osfhandle(
            handle,
            os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
    else:
        raw_descriptor = os.environ.get(RESULT_FD_ENV)
        if raw_descriptor is None:
            raise RuntimeError("result channel is unavailable")
        try:
            descriptor = int(raw_descriptor, 10)
        except ValueError as exc:
            raise RuntimeError("result channel descriptor is invalid") from exc
        if descriptor <= 2:
            raise RuntimeError("result channel descriptor is invalid")
    return os.fdopen(descriptor, "wb", buffering=0, closefd=True)


__all__ = [
    "RESULT_FD_ENV",
    "RESULT_HANDLE_ENV",
    "CapturedStream",
    "DirectoryIdentity",
    "EnvironmentVariable",
    "ExecutableIdentity",
    "ProcessConfigurationError",
    "ProcessFailure",
    "ProcessLimits",
    "ProcessOutcome",
    "ProcessOutcomeStatus",
    "ProcessRequest",
    "ProcessRunner",
    "StreamLimits",
    "StreamName",
    "capture_directory_identity",
    "capture_executable_identity",
    "open_result_channel",
]
