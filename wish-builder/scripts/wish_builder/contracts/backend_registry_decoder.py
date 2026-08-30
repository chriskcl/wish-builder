"""Strict decoder for the independently pinned backend version registry."""

from __future__ import annotations

import json
from typing import NoReturn

from .backend_registry import (
    BackendAdapterKind,
    BackendProtocolProfile,
    BackendVersionQualificationRecord,
    BackendVersionRegistry,
    BackendVersionStatus,
)
from .compatibility import Platform, Provider


MAX_BACKEND_REGISTRY_BYTES = 256 * 1024


class BackendRegistryDecodeError(ValueError):
    pass


def _reject_constant(value: str) -> NoReturn:
    raise BackendRegistryDecodeError(f"non-finite JSON number is not allowed: {value}")


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise BackendRegistryDecodeError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _dictionary(value: object, field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise BackendRegistryDecodeError(f"{field} must be an object")
    return value


def _array(value: object, field: str) -> list[object]:
    if type(value) is not list:
        raise BackendRegistryDecodeError(f"{field} must be an array")
    return value


def _keys(value: dict[str, object], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise BackendRegistryDecodeError(
            f"{field} has an invalid field set; missing={missing}, extra={extra}"
        )


def _enum(value: object, enum_type, field: str):
    if type(value) is not str:
        raise BackendRegistryDecodeError(f"{field} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise BackendRegistryDecodeError(f"{field} is unsupported") from exc


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise BackendRegistryDecodeError(f"{field} must be a string or null")
    return value


def _profile(value: object, index: int) -> BackendProtocolProfile:
    raw = _dictionary(value, f"profiles[{index}]")
    _keys(
        raw,
        {
            "adapter",
            "binName",
            "entrypoint",
            "packageName",
            "profileId",
            "protocol",
            "provider",
            "runtime",
            "schemaVersion",
            "versionProbe",
        },
        f"profiles[{index}]",
    )
    try:
        return BackendProtocolProfile(
            schema_version=raw["schemaVersion"],  # type: ignore[arg-type]
            profile_id=raw["profileId"],  # type: ignore[arg-type]
            provider=_enum(raw["provider"], Provider, "provider"),
            adapter=_enum(raw["adapter"], BackendAdapterKind, "adapter"),
            protocol=raw["protocol"],  # type: ignore[arg-type]
            package_name=raw["packageName"],  # type: ignore[arg-type]
            bin_name=raw["binName"],  # type: ignore[arg-type]
            entrypoint=raw["entrypoint"],  # type: ignore[arg-type]
            runtime=raw["runtime"],  # type: ignore[arg-type]
            version_probe=raw["versionProbe"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise BackendRegistryDecodeError(f"profiles[{index}]: {exc}") from exc


def _record(value: object, index: int) -> BackendVersionQualificationRecord:
    raw = _dictionary(value, f"records[{index}]")
    _keys(
        raw,
        {
            "backendVersion",
            "enabledForDispatch",
            "evidenceDigest",
            "launchProfileDigest",
            "maxConcurrency",
            "note",
            "npmIntegrity",
            "npmShasum",
            "platform",
            "protocolProfile",
            "provider",
            "publicationReceiptDigest",
            "reviewReference",
            "status",
        },
        f"records[{index}]",
    )
    status = _enum(raw["status"], BackendVersionStatus, "status")
    enabled = raw["enabledForDispatch"]
    if type(enabled) is not bool or enabled is not (
        status is BackendVersionStatus.QUALIFIED
    ):
        raise BackendRegistryDecodeError(
            f"records[{index}].enabledForDispatch contradicts status"
        )
    try:
        return BackendVersionQualificationRecord(
            provider=_enum(raw["provider"], Provider, "provider"),
            platform=_enum(raw["platform"], Platform, "platform"),
            backend_version=raw["backendVersion"],  # type: ignore[arg-type]
            protocol_profile=raw["protocolProfile"],  # type: ignore[arg-type]
            launch_profile_digest=raw["launchProfileDigest"],  # type: ignore[arg-type]
            package_shasum=raw["npmShasum"],  # type: ignore[arg-type]
            package_integrity=raw["npmIntegrity"],  # type: ignore[arg-type]
            status=status,
            max_concurrency=raw["maxConcurrency"],  # type: ignore[arg-type]
            evidence_digest=_optional_string(raw["evidenceDigest"], "evidenceDigest"),
            publication_receipt_digest=_optional_string(
                raw["publicationReceiptDigest"], "publicationReceiptDigest"
            ),
            review_reference=_optional_string(
                raw["reviewReference"], "reviewReference"
            ),
            note=raw["note"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise BackendRegistryDecodeError(f"records[{index}]: {exc}") from exc


def decode_backend_version_registry_primitive(value: object) -> BackendVersionRegistry:
    raw = _dictionary(value, "registry")
    _keys(raw, {"profiles", "records", "registryDigest", "schemaVersion"}, "registry")
    profiles = tuple(
        _profile(item, index)
        for index, item in enumerate(_array(raw["profiles"], "profiles"))
    )
    records = tuple(
        _record(item, index)
        for index, item in enumerate(_array(raw["records"], "records"))
    )
    try:
        return BackendVersionRegistry(
            schema_version=raw["schemaVersion"],  # type: ignore[arg-type]
            profiles=profiles,
            records=records,
            registry_digest=raw["registryDigest"],  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise BackendRegistryDecodeError(str(exc)) from exc


def decode_backend_version_registry_bytes(raw: bytes) -> BackendVersionRegistry:
    if type(raw) is not bytes:
        raise TypeError("raw must be bytes")
    if len(raw) > MAX_BACKEND_REGISTRY_BYTES:
        raise BackendRegistryDecodeError("backend registry exceeds the byte limit")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise BackendRegistryDecodeError("backend registry must be strict UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except BackendRegistryDecodeError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise BackendRegistryDecodeError("backend registry is not valid JSON") from exc
    return decode_backend_version_registry_primitive(value)


__all__ = [
    "MAX_BACKEND_REGISTRY_BYTES",
    "BackendRegistryDecodeError",
    "decode_backend_version_registry_bytes",
    "decode_backend_version_registry_primitive",
]
