"""Production adapter for the pinned Trellis 0.6.15 task-record lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from wish_builder.contracts import canonical_json_bytes
from wish_builder.contracts.runtime import EffectStatus
from wish_builder.services.ports import (
    AttemptObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PrepareAttempt,
    PreparedEffect,
    TrellisLifecyclePort,
    TrellisLifecycleState,
)

from .projection import (
    _BridgeTransportError,
    _command,
    _directory,
    _environment,
    _invoke_bridge,
    _limit,
)

BRIDGE_PROTOCOL_VERSION = 1
SUPPORTED_TRELLIS_VERSION = "0.6.15"
DEFAULT_LIFECYCLE_TIMEOUT_SECONDS = 30.0
MAX_LIFECYCLE_TIMEOUT_SECONDS = 300.0
DEFAULT_LIFECYCLE_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_LIFECYCLE_OUTPUT_BYTES = 2 * 1024 * 1024
DEFAULT_LIFECYCLE_STDERR_BYTES = 64 * 1024


class TrellisLifecycleAdapterError(RuntimeError):
    """The lifecycle bridge could not produce a trustworthy observation."""

    def __init__(self, reason: str) -> None:
        self.reason = _reason(reason)
        super().__init__(self.reason)


class TrellisCoreLifecyclePort(TrellisLifecyclePort):
    """Call the official Core task-record API through the pinned Node bridge.

    A port is scoped to one attempt.  Trellis task records are the durable
    lifecycle store; the adapter only writes its namespaced metadata and never
    changes the Wish Builder graph metadata or task status.
    """

    def __init__(
        self,
        *,
        bridge_command: Sequence[str],
        checkout_root: str | os.PathLike[str],
        working_directory: str | os.PathLike[str],
        trellis_task_id: str,
        worktree_path: str | os.PathLike[str],
        worktree_id: str,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_LIFECYCLE_TIMEOUT_SECONDS,
        max_request_bytes: int = DEFAULT_LIFECYCLE_REQUEST_BYTES,
        max_stdout_bytes: int = DEFAULT_LIFECYCLE_OUTPUT_BYTES,
        max_stderr_bytes: int = DEFAULT_LIFECYCLE_STDERR_BYTES,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._bridge_command = _command(bridge_command)
        self._checkout_root = _directory(checkout_root, "checkout_root")
        self._working_directory = _directory(working_directory, "working_directory")
        if type(trellis_task_id) is not str or not trellis_task_id or len(trellis_task_id) > 512:
            raise ValueError("trellis_task_id must be a non-empty string")
        self._trellis_task_id = trellis_task_id
        path = Path(worktree_path)
        if not path.is_absolute() or not str(path):
            raise ValueError("worktree_path must be an absolute path")
        self._worktree_path = str(path)
        if type(worktree_id) is not str or not worktree_id:
            raise ValueError("worktree_id must be a non-empty string")
        self._worktree_id = worktree_id
        self._environment = _environment(environment)
        if (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= MAX_LIFECYCLE_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds is outside the supported range")
        self._timeout_seconds = float(timeout_seconds)
        self._max_request_bytes = _limit(
            max_request_bytes, "max_request_bytes", DEFAULT_LIFECYCLE_REQUEST_BYTES * 8
        )
        self._max_stdout_bytes = _limit(
            max_stdout_bytes, "max_stdout_bytes", DEFAULT_LIFECYCLE_OUTPUT_BYTES * 2
        )
        self._max_stderr_bytes = _limit(
            max_stderr_bytes, "max_stderr_bytes", DEFAULT_LIFECYCLE_STDERR_BYTES * 8
        )
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or null")
        self._clock = clock or _utc_now

    def prepare_attempt(self, effect: PreparedEffect[PrepareAttempt]) -> AttemptObservation:
        typed = _require_effect(effect, PrepareAttempt)
        command = typed.command
        if command.trellis_task_id != self._trellis_task_id:
            return self._unknown_attempt(command.operation_id, "trellis_task_identity_mismatch")
        return self._apply(
            "lifecycle_prepare",
            "prepare_attempt",
            typed,
            _decode_attempt,
            worktree_path=self._worktree_path,
            worktree_id=self._worktree_id,
        )

    def check_attempt(self, effect: PreparedEffect[CheckAttempt]) -> CheckObservation:
        typed = _require_effect(effect, CheckAttempt)
        command = typed.command
        if command.trellis_task_id != self._trellis_task_id:
            return self._unknown_check(command.operation_id, "trellis_task_identity_mismatch")
        return self._apply("lifecycle_check", "check_attempt", typed, _decode_check)

    def finish_attempt(self, effect: PreparedEffect[FinishAttempt]) -> FinishObservation:
        typed = _require_effect(effect, FinishAttempt)
        command = typed.command
        if command.trellis_task_id != self._trellis_task_id:
            return self._unknown_finish(command.operation_id, "trellis_task_identity_mismatch")
        return self._apply("lifecycle_finish", "finish_attempt", typed, _decode_finish)

    def inspect_attempt(
        self, operation_id: str, *, expected_request_payload_hash: str | None = None
    ) -> AttemptObservation:
        return self._inspect("prepare_attempt", operation_id, _decode_attempt, expected_request_payload_hash)

    def inspect_check(
        self, operation_id: str, *, expected_request_payload_hash: str | None = None
    ) -> CheckObservation:
        return self._inspect("check_attempt", operation_id, _decode_check, expected_request_payload_hash)

    def inspect_finish(
        self, operation_id: str, *, expected_request_payload_hash: str | None = None
    ) -> FinishObservation:
        return self._inspect("finish_attempt", operation_id, _decode_finish, expected_request_payload_hash)

    def _apply(self, action, kind, effect, decoder, **extra):
        request = {
            "protocolVersion": BRIDGE_PROTOCOL_VERSION,
            "action": action,
            "checkoutRoot": self._checkout_root,
            "operationKind": kind,
            "commandHash": effect.command_hash,
            "command": effect.command.to_primitive(),
            "worktreePath": extra.get("worktree_path"),
            "worktreeId": extra.get("worktree_id"),
        }
        try:
            document = self._call(request, action)
            return decoder(document["observation"])
        except Exception as exc:  # noqa: BLE001 - effects fail closed as UNKNOWN
            return self._unknown(kind, effect.operation_id, _failure_reason(exc), decoder)

    def _inspect(self, kind, operation_id, decoder, expected_hash):
        if type(operation_id) is not str or not operation_id:
            raise TypeError("operation_id must be a non-empty string")
        if expected_hash is not None and (
            type(expected_hash) is not str or not _is_digest(expected_hash)
        ):
            raise ValueError("expected_request_payload_hash must be a sha256 reference")
        request = {
            "protocolVersion": BRIDGE_PROTOCOL_VERSION,
            "action": "lifecycle_inspect",
            "checkoutRoot": self._checkout_root,
            "trellisTaskId": self._trellis_task_id,
            "operationKind": kind,
            "operationId": operation_id,
            "expectedRequestPayloadHash": expected_hash,
        }
        try:
            document = self._call(request, "lifecycle_inspect")
            return decoder(document["observation"])
        except Exception as exc:  # noqa: BLE001 - reconciliation is fail closed
            return self._unknown(kind, operation_id, _failure_reason(exc), decoder)

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
            raise TrellisLifecycleAdapterError(f"lifecycle_{exc.reason}") from exc
        if document.get("ok") is False:
            _raise_bridge_error(document, action, return_code)
        if return_code != 0 or set(document) != {
            "protocolVersion", "ok", "action", "observation", "bridge"
        }:
            raise TrellisLifecycleAdapterError("lifecycle_response_schema")
        if (
            document["protocolVersion"] != BRIDGE_PROTOCOL_VERSION
            or document["ok"] is not True
            or document["action"] != action
        ):
            raise TrellisLifecycleAdapterError("lifecycle_response_identity")
        _validate_bridge(document["bridge"])
        return document

    def _unknown(self, kind, operation_id, reason, decoder):
        return decoder({
            "operationId": operation_id,
            "status": "unknown",
            "observedAt": self._clock(),
            **_unknown_fields(kind),
            "effectDigest": None,
            "evidence": (reason,),
        })

    def _unknown_attempt(self, operation_id, reason):
        return self._unknown("prepare_attempt", operation_id, reason, _decode_attempt)

    def _unknown_check(self, operation_id, reason):
        return self._unknown("check_attempt", operation_id, reason, _decode_check)

    def _unknown_finish(self, operation_id, reason):
        return self._unknown("finish_attempt", operation_id, reason, _decode_finish)


def _require_effect(effect, command_type):
    if type(effect) is not PreparedEffect:
        raise TypeError("effect must be a PreparedEffect")
    if type(effect.command) is not command_type:
        raise TypeError(f"effect command must be {command_type.__name__}")
    return effect


def _decode_attempt(value: object) -> AttemptObservation:
    source = _object(
        value,
        "attempt observation",
        {
            "operationId", "status", "observedAt", "lifecycleState", "effectDigest",
            "attemptId", "trellisTaskId", "worktreeId", "worktreePath", "baseCommit",
            "evidence",
        },
    )
    return AttemptObservation(
        operation_id=source["operationId"],
        status=EffectStatus(source["status"]),
        observed_at=source["observedAt"],
        lifecycle_state=TrellisLifecycleState(source["lifecycleState"]),
        effect_digest=source.get("effectDigest"),
        attempt_id=source.get("attemptId"),
        trellis_task_id=source.get("trellisTaskId"),
        worktree_id=source.get("worktreeId"),
        worktree_path=source.get("worktreePath"),
        base_commit=source.get("baseCommit"),
        evidence=tuple(source.get("evidence", ())),
    )


def _decode_check(value: object) -> CheckObservation:
    source = _object(
        value,
        "check observation",
        {
            "operationId", "status", "observedAt", "effectDigest", "attemptId",
            "passed", "headCommit", "checkDigest", "evidence",
        },
    )
    return CheckObservation(
        operation_id=source["operationId"],
        status=EffectStatus(source["status"]),
        observed_at=source["observedAt"],
        effect_digest=source.get("effectDigest"),
        attempt_id=source.get("attemptId"),
        passed=source.get("passed"),
        head_commit=source.get("headCommit"),
        check_digest=source.get("checkDigest"),
        evidence=tuple(source.get("evidence", ())),
    )


def _decode_finish(value: object) -> FinishObservation:
    source = _object(
        value,
        "finish observation",
        {
            "operationId", "status", "observedAt", "effectDigest", "attemptId",
            "finished", "deliveredCommit", "finishDigest", "evidence",
        },
    )
    return FinishObservation(
        operation_id=source["operationId"],
        status=EffectStatus(source["status"]),
        observed_at=source["observedAt"],
        effect_digest=source.get("effectDigest"),
        attempt_id=source.get("attemptId"),
        finished=source.get("finished"),
        delivered_commit=source.get("deliveredCommit"),
        finish_digest=source.get("finishDigest"),
        evidence=tuple(source.get("evidence", ())),
    )


def _object(value: object, field: str, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{field} must be an object")
    return value


def _unknown_fields(kind: str) -> dict[str, object]:
    if kind == "prepare_attempt":
        return {
            "lifecycleState": "unknown", "attemptId": None, "trellisTaskId": None,
            "worktreeId": None, "worktreePath": None, "baseCommit": None,
        }
    if kind == "check_attempt":
        return {"attemptId": None, "passed": None, "headCommit": None, "checkDigest": None}
    return {"attemptId": None, "finished": None, "deliveredCommit": None, "finishDigest": None}


def _validate_bridge(value: object) -> None:
    if type(value) is not dict or value.get("bridgeProtocolVersion") != 1:
        raise TrellisLifecycleAdapterError("lifecycle_bridge_schema")
    if (
        value.get("corePackageName") != "@mindfoldhq/trellis-core"
        or value.get("corePackageVersion") != SUPPORTED_TRELLIS_VERSION
        or value.get("coreArchiveVerified") is not True
        or value.get("operationSchemaVersion") is not None
        or value.get("capabilitySchemaVersion") is not None
        or value.get("operationKinds") != []
        or not _is_digest(value.get("coreArchiveSha256"))
        or not _is_digest(value.get("corePackageTreeSha256"))
    ):
        raise TrellisLifecycleAdapterError("lifecycle_bridge_identity")


def _raise_bridge_error(document: dict[str, object], action: str, return_code: int | None) -> None:
    if return_code in {None, 0} or set(document) != {"protocolVersion", "ok", "action", "error"}:
        raise TrellisLifecycleAdapterError("lifecycle_error_schema")
    error = document["error"]
    if type(error) is not dict:
        raise TrellisLifecycleAdapterError("lifecycle_error_schema")
    details = error.get("details")
    reason = details.get("reason") if type(details) is dict else error.get("code")
    raise TrellisLifecycleAdapterError(_reason(reason or "lifecycle_failure"))


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, TrellisLifecycleAdapterError):
        return exc.reason
    return "lifecycle_response_invalid"


def _reason(value: object) -> str:
    if type(value) is not str or not value:
        return "lifecycle_unavailable"
    normalized = "".join(ch if ch.isalnum() or ch in "_-." else "_" for ch in value.lower())
    return normalized[:128] or "lifecycle_unavailable"


def _is_digest(value: object) -> bool:
    return type(value) is str and len(value) == 71 and value.startswith("sha256:") and all(ch in "0123456789abcdef" for ch in value[7:])


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "DEFAULT_LIFECYCLE_OUTPUT_BYTES",
    "DEFAULT_LIFECYCLE_REQUEST_BYTES",
    "DEFAULT_LIFECYCLE_STDERR_BYTES",
    "DEFAULT_LIFECYCLE_TIMEOUT_SECONDS",
    "SUPPORTED_TRELLIS_VERSION",
    "TrellisCoreLifecyclePort",
    "TrellisLifecycleAdapterError",
]
