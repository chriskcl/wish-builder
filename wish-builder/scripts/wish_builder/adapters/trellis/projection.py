"""Subprocess adapter for the pinned Trellis task projection bridge."""

from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.services.ports.projection import (
    TrellisProjection,
    TrellisProjectionApplyRequest,
    TrellisProjectionDisposition,
    TrellisProjectionObservation,
    TrellisProjectionPort,
    TrellisProjectionReason,
)

BRIDGE_PROTOCOL_VERSION = 1
DEFAULT_PROJECTION_TIMEOUT_SECONDS = 30.0
MAX_PROJECTION_TIMEOUT_SECONDS = 300.0
DEFAULT_PROJECTION_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_PROJECTION_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_PROJECTION_STDERR_BYTES = 64 * 1024
SUPPORTED_TRELLIS_VERSION = "0.6.15"


class TrellisProjectionAdapterError(RuntimeError):
    """The bridge could not produce a trustworthy projection observation."""

    def __init__(self, reason: str) -> None:
        self.reason = _reason_text(reason)
        super().__init__(self.reason)


class _BridgeTransportError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class TrellisCoreProjectionPort(TrellisProjectionPort):
    """Call the pinned Node bridge for guarded projection inspect/write operations.

    The port never shells a checkout path.  Paths and the projection payload
    are encoded as one bounded JSON request. Official Trellis 0.6.15 has no
    cross-process CAS, so the bridge uses one projection writer plus pre/post
    digest checks and turns every uncertain result into an unavailable
    observation. Canonical Wish Builder state always remains intact.
    """

    def __init__(
        self,
        *,
        bridge_command: Sequence[str],
        working_directory: str | os.PathLike[str],
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_PROJECTION_TIMEOUT_SECONDS,
        max_request_bytes: int = DEFAULT_PROJECTION_REQUEST_BYTES,
        max_stdout_bytes: int = DEFAULT_PROJECTION_OUTPUT_BYTES,
        max_stderr_bytes: int = DEFAULT_PROJECTION_STDERR_BYTES,
    ) -> None:
        self._bridge_command = _command(bridge_command)
        self._working_directory = _directory(working_directory, "working_directory")
        self._environment = _environment(environment)
        if (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= MAX_PROJECTION_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds is outside the supported range")
        self._timeout_seconds = float(timeout_seconds)
        self._max_request_bytes = _limit(
            max_request_bytes, "max_request_bytes", DEFAULT_PROJECTION_REQUEST_BYTES * 8
        )
        self._max_stdout_bytes = _limit(
            max_stdout_bytes, "max_stdout_bytes", DEFAULT_PROJECTION_OUTPUT_BYTES * 8
        )
        self._max_stderr_bytes = _limit(
            max_stderr_bytes, "max_stderr_bytes", DEFAULT_PROJECTION_STDERR_BYTES * 8
        )

    def inspect(
        self, checkout_root: Path, trellis_task_id: str
    ) -> TrellisProjectionObservation:
        request = {
            "protocolVersion": BRIDGE_PROTOCOL_VERSION,
            "action": "projection_inspect",
            "checkoutRoot": _checkout(checkout_root),
            "trellisTaskId": trellis_task_id,
        }
        try:
            response = self._call(request, "projection_inspect")
            projection = _observation(response["projection"])
            return projection
        except (TrellisProjectionAdapterError, TypeError, ValueError, KeyError) as exc:
            return _unavailable(_failure_reason(exc))

    def apply(
        self, request: TrellisProjectionApplyRequest
    ) -> TrellisProjectionObservation:
        if type(request) is not TrellisProjectionApplyRequest:
            raise TypeError("request must be a TrellisProjectionApplyRequest")
        body = {
            "protocolVersion": BRIDGE_PROTOCOL_VERSION,
            "action": "projection_apply",
            "checkoutRoot": _checkout(request.checkout_root),
            "trellisTaskId": request.trellis_task_id,
            "expectedRevision": request.expected_revision,
            "projection": request.projection.to_primitive(),
        }
        try:
            response = self._call(body, "projection_apply")
            return _observation(response["projection"])
        except (TrellisProjectionAdapterError, TypeError, ValueError, KeyError) as exc:
            return _unavailable(_failure_reason(exc))

    def _call(self, request: dict[str, object], action: str) -> dict[str, object]:
        raw = canonical_json_bytes(request)
        try:
            document, return_code = _invoke_bridge(
                raw=raw,
                bridge_command=self._bridge_command,
                working_directory=self._working_directory,
                environment=self._environment,
                timeout_seconds=self._timeout_seconds,
                max_request_bytes=self._max_request_bytes,
                max_stdout_bytes=self._max_stdout_bytes,
                max_stderr_bytes=self._max_stderr_bytes,
            )
        except _BridgeTransportError as exc:
            raise TrellisProjectionAdapterError(f"projection_{exc.reason}") from exc
        if document.get("ok") is False:
            _raise_bridge_error(document, action, return_code)
        if return_code != 0:
            raise TrellisProjectionAdapterError("projection_exit_failure")
        expected = {
            "protocolVersion",
            "ok",
            "action",
            "projection",
            "bridge",
        }
        if set(document) != expected:
            raise TrellisProjectionAdapterError("projection_response_schema")
        if (
            document["protocolVersion"] != BRIDGE_PROTOCOL_VERSION
            or document["ok"] is not True
            or document["action"] != action
        ):
            raise TrellisProjectionAdapterError("projection_response_identity")
        _validate_bridge(document["bridge"])
        return document


def _observation(value: object) -> TrellisProjectionObservation:
    if not isinstance(value, dict):
        raise ValueError("projection response must be an object")
    required = {
        "disposition",
        "reason",
        "recordRevision",
        "byteLength",
        "taskStatus",
        "projection",
    }
    if set(value) != required:
        raise ValueError("projection observation schema mismatch")
    try:
        disposition = TrellisProjectionDisposition(value["disposition"])
    except (TypeError, ValueError) as exc:
        raise ValueError("projection disposition is invalid") from exc
    reason_value = value["reason"]
    try:
        reason = TrellisProjectionReason(reason_value)
    except (TypeError, ValueError):
        reason = TrellisProjectionReason.UNAVAILABLE
    revision = value["recordRevision"]
    if revision is not None:
        _digest(revision, "recordRevision")
    byte_length = value["byteLength"]
    if type(byte_length) is not int or byte_length < 0:
        raise ValueError("projection byteLength is invalid")
    task_status = value["taskStatus"]
    if task_status is not None and (type(task_status) is not str or not task_status):
        raise ValueError("projection taskStatus is invalid")
    projection_value = value["projection"]
    projection = None if projection_value is None else _decode_projection(projection_value)
    if disposition is TrellisProjectionDisposition.UNAVAILABLE:
        raise ValueError("bridge cannot return unavailable as a success response")
    return TrellisProjectionObservation(
        disposition,
        reason,
        revision,
        byte_length,
        task_status,
        projection,
    )


def _decode_projection(value: object) -> TrellisProjection:
    if not isinstance(value, dict):
        raise ValueError("projection payload must be an object")
    expected = {
        "schemaVersion",
        "operationId",
        "runId",
        "taskId",
        "trellisTaskId",
        "manifestDigest",
        "trellisGraphDigest",
        "canonicalSequence",
        "canonicalEventHash",
        "canonicalState",
        "targetStatus",
        "evidenceDigests",
        "summary",
    }
    if set(value) != expected | {"projectionDigest"}:
        raise ValueError("projection payload schema mismatch")
    projection = TrellisProjection(
        schema_version=value["schemaVersion"],
        operation_id=value["operationId"],
        run_id=value["runId"],
        task_id=value["taskId"],
        trellis_task_id=value["trellisTaskId"],
        manifest_digest=value["manifestDigest"],
        trellis_graph_digest=value["trellisGraphDigest"],
        canonical_sequence=value["canonicalSequence"],
        canonical_event_hash=value["canonicalEventHash"],
        canonical_state=value["canonicalState"],
        target_status=value["targetStatus"],
        evidence_digests=tuple(value["evidenceDigests"]),
        summary=value["summary"],
    )
    stored_digest = _digest(value["projectionDigest"], "projectionDigest")
    encoded = json.dumps(
        projection.to_primitive(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", errors="strict")
    expected_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if stored_digest != expected_digest:
        raise ValueError("projection digest does not match its payload")
    return projection


def _raise_bridge_error(
    document: dict[str, object], action: str, return_code: int | None
) -> None:
    if set(document) != {"protocolVersion", "ok", "action", "error"}:
        raise TrellisProjectionAdapterError("projection_error_schema")
    if document["protocolVersion"] != BRIDGE_PROTOCOL_VERSION or document["ok"] is not False:
        raise TrellisProjectionAdapterError("projection_error_identity")
    if document["action"] not in {None, action}:
        raise TrellisProjectionAdapterError("projection_error_action")
    error = document["error"]
    if not isinstance(error, dict) or set(error) != {"code", "message", "details"}:
        raise TrellisProjectionAdapterError("projection_error_schema")
    details = error["details"]
    reason = None
    if isinstance(details, dict) and isinstance(details.get("reason"), str):
        reason = details["reason"]
    if reason is None and isinstance(error.get("code"), str):
        reason = error["code"]
    raise TrellisProjectionAdapterError(_reason_text(reason or "projection_failure"))


def _validate_bridge(value: object) -> None:
    if type(value) is not dict or set(value) != {
        "bridgeProtocolVersion",
        "corePackageName",
        "corePackageVersion",
        "coreArchiveSha256",
        "coreArchiveVerified",
        "corePackageTreeSha256",
        "operationSchemaVersion",
        "capabilitySchemaVersion",
        "operationKinds",
    }:
        raise TrellisProjectionAdapterError("projection_bridge_schema")
    if (
        value["bridgeProtocolVersion"] != BRIDGE_PROTOCOL_VERSION
        or value["corePackageName"] != "@mindfoldhq/trellis-core"
        or value["corePackageVersion"] != SUPPORTED_TRELLIS_VERSION
        or value["coreArchiveVerified"] is not True
        or value["operationSchemaVersion"] is not None
        or value["capabilitySchemaVersion"] is not None
        or value["operationKinds"] != []
    ):
        raise TrellisProjectionAdapterError("projection_bridge_identity")
    try:
        _digest(value["coreArchiveSha256"], "coreArchiveSha256")
        _digest(value["corePackageTreeSha256"], "corePackageTreeSha256")
    except ValueError as exc:
        raise TrellisProjectionAdapterError("projection_bridge_identity") from exc


def _unavailable(reason: str) -> TrellisProjectionObservation:
    return TrellisProjectionObservation(
        TrellisProjectionDisposition.UNAVAILABLE,
        _reason_enum(reason),
    )


def _reason_enum(value: str) -> TrellisProjectionReason:
    try:
        return TrellisProjectionReason(value)
    except ValueError:
        return TrellisProjectionReason.UNAVAILABLE


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, TrellisProjectionAdapterError):
        return exc.reason
    return "projection_response_invalid"


def _reason_text(value: object) -> str:
    if type(value) is not str or not value:
        return "projection_unavailable"
    normalized = "".join(
        character if character.isalnum() or character in "_-." else "_"
        for character in value.lower()
    )
    return normalized[:128] or "projection_unavailable"


def _invoke_bridge(
    *,
    raw: bytes,
    bridge_command: tuple[str, ...],
    working_directory: str,
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_request_bytes: int,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> tuple[dict[str, object], int | None]:
    if len(raw) > max_request_bytes:
        raise _BridgeTransportError("request_limit")
    try:
        process = subprocess.Popen(
            bridge_command,
            cwd=working_directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except (OSError, ValueError) as exc:
        raise _BridgeTransportError("spawn_failed") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture = _BoundedCapture(max_stdout_bytes)
    stderr_capture = _BoundedCapture(max_stderr_bytes)
    threads = (
        threading.Thread(target=_write_request, args=(process.stdin, raw), daemon=True),
        threading.Thread(target=stdout_capture.drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr_capture.drain, args=(process.stderr,), daemon=True),
    )
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout_seconds
    failure: str | None = None
    while process.poll() is None:
        if stdout_capture.overflow.is_set():
            failure = "stdout_limit"
            break
        if stderr_capture.overflow.is_set():
            failure = "stderr_limit"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = "timeout"
            break
        time.sleep(min(0.01, remaining))
    if failure is not None:
        _terminate(process)
    else:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            failure = "timeout"
            _terminate(process)
    for thread in threads:
        thread.join(timeout=1)
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    if any(thread.is_alive() for thread in threads):
        failure = failure or "pipe_not_closed"
    if stdout_capture.overflow.is_set():
        failure = "stdout_limit"
    if stderr_capture.overflow.is_set():
        failure = "stderr_limit"
    if failure is not None:
        raise _BridgeTransportError(failure)
    if stderr_capture.data:
        raise _BridgeTransportError("unexpected_stderr")
    try:
        document = _strict_object(bytes(stdout_capture.data), max_stdout_bytes)
    except TrellisProjectionAdapterError as exc:
        reason = exc.reason.removeprefix("projection_")
        raise _BridgeTransportError(reason) from exc
    return document, process.returncode


def _strict_object(raw: bytes, maximum: int | None = None) -> dict[str, object]:
    limit = DEFAULT_PROJECTION_OUTPUT_BYTES * 8 if maximum is None else maximum
    if not raw or len(raw) > limit:
        raise TrellisProjectionAdapterError("projection_stdout_limit")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TrellisProjectionAdapterError("projection_json_invalid") from exc
    if type(value) is not dict:
        raise TrellisProjectionAdapterError("projection_response_not_object")
    return value


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _command(value: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise ValueError("bridge_command must be a non-empty sequence")
    result = tuple(value)
    if any(type(item) is not str or not item for item in result):
        raise ValueError("bridge_command contains an invalid argument")
    if not Path(result[0]).is_absolute():
        raise ValueError("bridge executable must be absolute")
    return result


def _directory(value: str | os.PathLike[str], field: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError(f"{field} must be an existing non-link directory")
    return str(path.resolve(strict=True))


def _checkout(value: Path) -> str:
    if not isinstance(value, Path):
        raise TypeError("checkout_root must be a Path")
    if not value.is_absolute() or "\x00" in str(value):
        raise ValueError("checkout_root must be absolute")
    return str(value)


def _environment(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is not None and not isinstance(value, Mapping):
        raise TypeError("environment must be a mapping")
    allowed = {
        key: os.environ[key]
        for key in (
            "PATH",
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        )
        if key in os.environ
    }
    if value is not None:
        for key, item in value.items():
            if type(key) is not str or type(item) is not str or not key or "\x00" in item:
                raise ValueError("environment contains an invalid value")
            allowed[key] = item
    return allowed


def _limit(value: int, field: str, maximum: int) -> int:
    if type(value) is not int or value <= 0 or value > maximum:
        raise ValueError(f"{field} is outside the supported range")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError(f"{field} must be a sha256 reference")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise ValueError(f"{field} must be a sha256 reference")
    return value


def _terminate(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


class _BoundedCapture:
    __slots__ = ("data", "limit", "overflow")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.overflow = threading.Event()

    def drain(self, stream: Any) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.overflow.set()
        except (OSError, ValueError):
            self.overflow.set()


def _write_request(stream: Any, raw: bytes) -> None:
    try:
        stream.write(raw)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "DEFAULT_PROJECTION_REQUEST_BYTES",
    "DEFAULT_PROJECTION_STDERR_BYTES",
    "DEFAULT_PROJECTION_OUTPUT_BYTES",
    "DEFAULT_PROJECTION_TIMEOUT_SECONDS",
    "TrellisCoreProjectionPort",
    "TrellisProjectionAdapterError",
]
