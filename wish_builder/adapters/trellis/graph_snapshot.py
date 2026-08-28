"""Read-only adapter for Wish Builder graph snapshots derived from Trellis tasks."""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.services.ports.trellis import (
    MAX_GRAPH_SNAPSHOT_BYTES,
    TrellisGraphPort,
    TrellisGraphSnapshot,
)

from .graph import SUPPORTED_TRELLIS_EXPORT_VERSION
from .projection import (
    _BridgeTransportError,
    _directory,
    _environment,
    _invoke_bridge,
    _limit,
)

BRIDGE_PROTOCOL_VERSION = 1
SUPPORTED_TRELLIS_VERSION = "0.6.15"
DEFAULT_GRAPH_TIMEOUT_SECONDS = 30.0
MAX_GRAPH_TIMEOUT_SECONDS = 300.0
DEFAULT_GRAPH_REQUEST_BYTES = 64 * 1024
DEFAULT_GRAPH_OUTPUT_BYTES = 12 * 1024 * 1024
DEFAULT_GRAPH_STDERR_BYTES = 64 * 1024


class TrellisGraphAdapterError(RuntimeError):
    """The bridge could not produce a trustworthy complete graph snapshot."""

    def __init__(self, reason: str) -> None:
        self.reason = _reason(reason)
        super().__init__(self.reason)


class TrellisCoreGraphPort(TrellisGraphPort):
    """Derive immutable Wish Builder snapshots from official Trellis task records."""

    def __init__(
        self,
        *,
        bridge_command: Sequence[str],
        checkout_root: str | os.PathLike[str],
        working_directory: str | os.PathLike[str],
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_GRAPH_TIMEOUT_SECONDS,
        max_request_bytes: int = DEFAULT_GRAPH_REQUEST_BYTES,
        max_stdout_bytes: int = DEFAULT_GRAPH_OUTPUT_BYTES,
        max_stderr_bytes: int = DEFAULT_GRAPH_STDERR_BYTES,
        clock: Callable[[], str] = lambda: _utc_now(),
    ) -> None:
        self._bridge_command = _command(bridge_command)
        self._checkout_root = _directory(checkout_root, "checkout_root")
        self._working_directory = _directory(working_directory, "working_directory")
        self._environment = _environment(environment)
        if (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= MAX_GRAPH_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds is outside the supported range")
        self._timeout_seconds = float(timeout_seconds)
        self._max_request_bytes = _limit(
            max_request_bytes, "max_request_bytes", DEFAULT_GRAPH_REQUEST_BYTES * 8
        )
        self._max_stdout_bytes = _limit(
            max_stdout_bytes, "max_stdout_bytes", DEFAULT_GRAPH_OUTPUT_BYTES * 2
        )
        self._max_stderr_bytes = _limit(
            max_stderr_bytes, "max_stderr_bytes", DEFAULT_GRAPH_STDERR_BYTES * 8
        )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock

    def export_snapshot(self, parent_task_id: str) -> TrellisGraphSnapshot:
        if type(parent_task_id) is not str or not parent_task_id:
            raise ValueError("parent_task_id must be non-empty")
        observed_at = self._clock()
        if type(observed_at) is not str or not observed_at:
            raise TrellisGraphAdapterError("graph_clock_invalid")
        request = {
            "protocolVersion": BRIDGE_PROTOCOL_VERSION,
            "action": "graph_snapshot",
            "checkoutRoot": self._checkout_root,
            "parentTaskId": parent_task_id,
            "observedAt": observed_at,
        }
        response = self._call(request)
        self._validate_bridge(response["bridge"])
        return _snapshot(response["snapshot"], parent_task_id, observed_at)

    def _call(self, request: dict[str, object]) -> dict[str, object]:
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
            raise TrellisGraphAdapterError(f"graph_{exc.reason}") from exc
        if document.get("ok") is False:
            _raise_bridge_error(document, return_code)
        if return_code != 0:
            raise TrellisGraphAdapterError("graph_exit_failure")
        if set(document) != {
            "protocolVersion",
            "ok",
            "action",
            "snapshot",
            "bridge",
        }:
            raise TrellisGraphAdapterError("graph_response_schema")
        if (
            document["protocolVersion"] != BRIDGE_PROTOCOL_VERSION
            or document["ok"] is not True
            or document["action"] != "graph_snapshot"
        ):
            raise TrellisGraphAdapterError("graph_response_identity")
        return document

    @staticmethod
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
            raise TrellisGraphAdapterError("graph_bridge_schema")
        if (
            value["bridgeProtocolVersion"] != BRIDGE_PROTOCOL_VERSION
            or value["corePackageName"] != "@mindfoldhq/trellis-core"
            or value["corePackageVersion"] != SUPPORTED_TRELLIS_VERSION
            or value["coreArchiveVerified"] is not True
            or value["operationSchemaVersion"] is not None
            or value["capabilitySchemaVersion"] is not None
            or value["operationKinds"] != []
        ):
            raise TrellisGraphAdapterError("graph_bridge_identity")
        _digest(value["coreArchiveSha256"], "coreArchiveSha256")
        _digest(value["corePackageTreeSha256"], "corePackageTreeSha256")


def _snapshot(
    value: object, parent_task_id: str, observed_at: str
) -> TrellisGraphSnapshot:
    if type(value) is not dict or set(value) != {
        "exportVersion",
        "trellisVersion",
        "parentTaskId",
        "revision",
        "observedAt",
        "snapshotBase64",
        "sourceSha256",
        "byteLength",
        "complete",
    }:
        raise TrellisGraphAdapterError("graph_snapshot_schema")
    if (
        value["exportVersion"] != SUPPORTED_TRELLIS_EXPORT_VERSION
        or value["trellisVersion"] != SUPPORTED_TRELLIS_VERSION
        or value["parentTaskId"] != parent_task_id
        or value["observedAt"] != observed_at
        or value["complete"] is not True
    ):
        raise TrellisGraphAdapterError("graph_snapshot_identity")
    encoded = value["snapshotBase64"]
    if type(encoded) is not str:
        raise TrellisGraphAdapterError("graph_snapshot_encoding")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TrellisGraphAdapterError("graph_snapshot_encoding") from exc
    byte_length = value["byteLength"]
    if (
        type(byte_length) is not int
        or byte_length != len(raw)
        or not 0 < byte_length <= MAX_GRAPH_SNAPSHOT_BYTES
    ):
        raise TrellisGraphAdapterError("graph_snapshot_length")
    revision = value["revision"]
    if type(revision) is not str or not revision:
        raise TrellisGraphAdapterError("graph_snapshot_revision")
    try:
        return TrellisGraphSnapshot(
            export_version=value["exportVersion"],
            trellis_version=value["trellisVersion"],
            parent_task_id=value["parentTaskId"],
            revision=revision,
            observed_at=value["observedAt"],
            snapshot_bytes=raw,
            source_sha256=_digest(value["sourceSha256"], "sourceSha256"),
            complete=True,
        )
    except (TypeError, ValueError) as exc:
        raise TrellisGraphAdapterError("graph_snapshot_invalid") from exc


def _raise_bridge_error(document: dict[str, object], return_code: int | None) -> None:
    if set(document) != {"protocolVersion", "ok", "action", "error"}:
        raise TrellisGraphAdapterError("graph_error_schema")
    if (
        document["protocolVersion"] != BRIDGE_PROTOCOL_VERSION
        or document["ok"] is not False
        or document["action"] not in {None, "graph_snapshot"}
        or return_code in {None, 0}
    ):
        raise TrellisGraphAdapterError("graph_error_identity")
    error = document["error"]
    if type(error) is not dict or set(error) != {"code", "message", "details"}:
        raise TrellisGraphAdapterError("graph_error_schema")
    details = error["details"]
    reason = details.get("reason") if type(details) is dict else error["code"]
    raise TrellisGraphAdapterError(_reason(reason))


def _command(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or len(value) != 2:
        raise ValueError("bridge_command must contain Node and the bridge script")
    result: list[str] = []
    for item in value:
        if type(item) is not str or not item or "\x00" in item:
            raise ValueError("bridge_command contains an invalid argument")
        path = Path(item)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("bridge_command entries must be absolute files")
        result.append(str(path.resolve(strict=True)))
    return tuple(result)


def _digest(value: object, field: str) -> str:
    if type(value) is not str or len(value) != 71 or not value.startswith("sha256:"):
        raise TrellisGraphAdapterError(f"graph_{field}_invalid")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise TrellisGraphAdapterError(f"graph_{field}_invalid")
    return value


def _reason(value: object) -> str:
    if type(value) is not str or not value:
        return "graph_unavailable"
    normalized = "".join(
        character if character.isalnum() or character in "_-." else "_"
        for character in value.lower()
    )
    return normalized[:128] or "graph_unavailable"


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "DEFAULT_GRAPH_OUTPUT_BYTES",
    "DEFAULT_GRAPH_REQUEST_BYTES",
    "DEFAULT_GRAPH_STDERR_BYTES",
    "DEFAULT_GRAPH_TIMEOUT_SECONDS",
    "SUPPORTED_TRELLIS_VERSION",
    "TrellisCoreGraphPort",
    "TrellisGraphAdapterError",
]
