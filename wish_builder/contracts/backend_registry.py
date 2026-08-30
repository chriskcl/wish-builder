"""Versioned backend protocol profiles and dispatch qualification records.

The registry is deliberately independent from Trellis compatibility.  It records
which exact backend package, OS and protocol profile may be dispatched.  Backend
adapters consume these records; the execution kernel only sees the stable Channel
port.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from .compatibility import (
    MAX_QUALIFICATION_CONCURRENT_TURNS,
    NPM_INTEGRITY_RE,
    Platform,
    Provider,
    VERSION_RE,
)
from .models import HASH_RE, MAX_PATH_LENGTH, MAX_TEXT_LENGTH, _nonempty
from .serialization import canonical_json_bytes, canonical_sha256


BACKEND_VERSION_REGISTRY_SCHEMA_VERSION = 1
BACKEND_PROTOCOL_PROFILE_SCHEMA_VERSION = 1
MAX_BACKEND_PROTOCOL_PROFILES = 16
MAX_BACKEND_VERSION_RECORDS = 128
MAX_QUALIFIED_VERSIONS_PER_CELL = 2

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PACKAGE_RE = re.compile(r"^@[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class BackendVersionStatus(StrEnum):
    CANDIDATE = "candidate"
    QUALIFIED = "qualified"
    QUARANTINED = "quarantined"


class BackendAdapterKind(StrEnum):
    CODEX_APP_SERVER = "codex_app_server"
    JSONL_RPC = "jsonl_rpc"


def _token(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, 128)
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a stable lowercase token")
    return normalized


def _version(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, 128)
    if not VERSION_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an exact semantic version")
    return normalized


def _digest(value: object, field_name: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return value


def _optional_digest(value: object, field_name: str) -> str | None:
    return None if value is None else _digest(value, field_name)


def _sha1(value: object, field_name: str) -> str:
    if type(value) is not str or not _SHA1_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-1 digest")
    return value


def _npm_integrity(value: object, field_name: str) -> str:
    if type(value) is not str or not NPM_INTEGRITY_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be an npm sha512 integrity string")
    try:
        decoded = base64.b64decode(value.removeprefix("sha512-"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be an npm sha512 integrity string"
        ) from exc
    if len(decoded) != 64:
        raise ValueError(f"{field_name} must contain a full SHA-512 digest")
    return value


def _relative_path(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, MAX_PATH_LENGTH).replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{field_name} must be a normalized relative path")
    if normalized != path.as_posix():
        raise ValueError(f"{field_name} must be a normalized relative path")
    return normalized


@dataclass(frozen=True, slots=True)
class BackendProtocolProfile:
    schema_version: int
    profile_id: str
    provider: Provider
    adapter: BackendAdapterKind
    protocol: str
    package_name: str
    bin_name: str
    entrypoint: str
    runtime: str
    version_probe: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != BACKEND_PROTOCOL_PROFILE_SCHEMA_VERSION
        ):
            raise ValueError("backend protocol profile schema_version must be 1")
        object.__setattr__(self, "profile_id", _token(self.profile_id, "profile_id"))
        if type(self.provider) is not Provider:
            raise TypeError("provider must be a Provider")
        if type(self.adapter) is not BackendAdapterKind:
            raise TypeError("adapter must be a BackendAdapterKind")
        object.__setattr__(self, "protocol", _token(self.protocol, "protocol"))
        package_name = _nonempty(self.package_name, "package_name", 128)
        if not _PACKAGE_RE.fullmatch(package_name):
            raise ValueError("package_name must be an exact scoped npm package")
        object.__setattr__(self, "package_name", package_name)
        object.__setattr__(self, "bin_name", _token(self.bin_name, "bin_name"))
        object.__setattr__(self, "entrypoint", _relative_path(self.entrypoint, "entrypoint"))
        runtime = _token(self.runtime, "runtime")
        if runtime not in {"bun", "node"}:
            raise ValueError("runtime must be bun or node")
        object.__setattr__(self, "runtime", runtime)
        if self.version_probe != "npm-package-json-v1":
            raise ValueError("version_probe must be npm-package-json-v1")

    def to_primitive(self) -> dict[str, object]:
        return {
            "adapter": self.adapter.value,
            "binName": self.bin_name,
            "entrypoint": self.entrypoint,
            "packageName": self.package_name,
            "profileId": self.profile_id,
            "protocol": self.protocol,
            "provider": self.provider.value,
            "runtime": self.runtime,
            "schemaVersion": self.schema_version,
            "versionProbe": self.version_probe,
        }


@dataclass(frozen=True, slots=True)
class BackendVersionQualificationRecord:
    provider: Provider
    platform: Platform
    backend_version: str
    protocol_profile: str
    launch_profile_digest: str
    package_shasum: str
    package_integrity: str
    status: BackendVersionStatus
    max_concurrency: int
    evidence_digest: str | None
    publication_receipt_digest: str | None
    review_reference: str | None
    note: str

    def __post_init__(self) -> None:
        if type(self.provider) is not Provider:
            raise TypeError("provider must be a Provider")
        if type(self.platform) is not Platform:
            raise TypeError("platform must be a Platform")
        object.__setattr__(
            self, "backend_version", _version(self.backend_version, "backend_version")
        )
        object.__setattr__(
            self,
            "protocol_profile",
            _token(self.protocol_profile, "protocol_profile"),
        )
        object.__setattr__(
            self,
            "launch_profile_digest",
            _digest(self.launch_profile_digest, "launch_profile_digest"),
        )
        object.__setattr__(
            self, "package_shasum", _sha1(self.package_shasum, "package_shasum")
        )
        object.__setattr__(
            self,
            "package_integrity",
            _npm_integrity(self.package_integrity, "package_integrity"),
        )
        if type(self.status) is not BackendVersionStatus:
            raise TypeError("status must be a BackendVersionStatus")
        if type(self.max_concurrency) is not int or isinstance(
            self.max_concurrency, bool
        ):
            raise TypeError("max_concurrency must be an integer")
        if not 0 <= self.max_concurrency <= MAX_QUALIFICATION_CONCURRENT_TURNS:
            raise ValueError("max_concurrency is outside the qualification limit")
        object.__setattr__(
            self,
            "evidence_digest",
            _optional_digest(self.evidence_digest, "evidence_digest"),
        )
        object.__setattr__(
            self,
            "publication_receipt_digest",
            _optional_digest(
                self.publication_receipt_digest, "publication_receipt_digest"
            ),
        )
        review_reference = (
            None
            if self.review_reference is None
            else _nonempty(self.review_reference, "review_reference", MAX_PATH_LENGTH)
        )
        object.__setattr__(self, "review_reference", review_reference)
        object.__setattr__(self, "note", _nonempty(self.note, "note", MAX_TEXT_LENGTH))

        if self.status is BackendVersionStatus.QUALIFIED:
            if self.max_concurrency < 1:
                raise ValueError("qualified versions require positive concurrency")
            if (
                self.evidence_digest is None
                or self.publication_receipt_digest is None
                or self.review_reference is None
            ):
                raise ValueError(
                    "qualified versions require evidence, publication receipt and review"
                )
        elif self.status is BackendVersionStatus.QUARANTINED:
            if self.max_concurrency != 0:
                raise ValueError("quarantined versions must have zero concurrency")
            if self.review_reference is None:
                raise ValueError("quarantined versions require a review reference")
        elif self.max_concurrency != 0:
            raise ValueError("candidate versions cannot authorize concurrency")

    @property
    def enabled_for_dispatch(self) -> bool:
        return self.status is BackendVersionStatus.QUALIFIED

    @property
    def key(self) -> tuple[Provider, Platform, str]:
        return (self.provider, self.platform, self.backend_version)

    def to_primitive(self) -> dict[str, object]:
        return {
            "backendVersion": self.backend_version,
            "enabledForDispatch": self.enabled_for_dispatch,
            "evidenceDigest": self.evidence_digest,
            "launchProfileDigest": self.launch_profile_digest,
            "maxConcurrency": self.max_concurrency,
            "note": self.note,
            "npmIntegrity": self.package_integrity,
            "npmShasum": self.package_shasum,
            "platform": self.platform.value,
            "protocolProfile": self.protocol_profile,
            "provider": self.provider.value,
            "publicationReceiptDigest": self.publication_receipt_digest,
            "reviewReference": self.review_reference,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class BackendVersionRegistry:
    schema_version: int
    profiles: tuple[BackendProtocolProfile, ...]
    records: tuple[BackendVersionQualificationRecord, ...]
    registry_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != BACKEND_VERSION_REGISTRY_SCHEMA_VERSION
        ):
            raise ValueError("backend version registry schema_version must be 1")
        if type(self.profiles) is not tuple or not all(
            type(item) is BackendProtocolProfile for item in self.profiles
        ):
            raise TypeError("profiles must contain BackendProtocolProfile values")
        if not 1 <= len(self.profiles) <= MAX_BACKEND_PROTOCOL_PROFILES:
            raise ValueError("profiles must contain a bounded non-empty set")
        if type(self.records) is not tuple or not all(
            type(item) is BackendVersionQualificationRecord for item in self.records
        ):
            raise TypeError(
                "records must contain BackendVersionQualificationRecord values"
            )
        if not 1 <= len(self.records) <= MAX_BACKEND_VERSION_RECORDS:
            raise ValueError("records must contain a bounded non-empty set")

        profile_order = tuple(item.profile_id for item in self.profiles)
        if profile_order != tuple(sorted(profile_order)):
            raise ValueError("profiles must be in canonical profile_id order")
        if len(set(profile_order)) != len(profile_order):
            raise ValueError("protocol profile IDs must be unique")
        profile_by_id = {item.profile_id: item for item in self.profiles}
        protocol_identities = tuple(
            (item.provider.value, item.protocol) for item in self.profiles
        )
        if len(set(protocol_identities)) != len(protocol_identities):
            raise ValueError("provider/protocol identities must be unique")

        record_order = tuple(
            (
                item.provider.value,
                item.platform.value,
                item.backend_version,
            )
            for item in self.records
        )
        if record_order != tuple(sorted(record_order)):
            raise ValueError("records must be in canonical identity order")
        if len(set(record_order)) != len(record_order):
            raise ValueError("backend version record identities must be unique")
        for record in self.records:
            profile = profile_by_id.get(record.protocol_profile)
            if profile is None or profile.provider is not record.provider:
                raise ValueError("record protocol profile does not match its provider")

        for provider in Provider:
            for platform in Platform:
                qualified = sum(
                    item.status is BackendVersionStatus.QUALIFIED
                    for item in self.records
                    if item.provider is provider and item.platform is platform
                )
                if qualified > MAX_QUALIFIED_VERSIONS_PER_CELL:
                    raise ValueError(
                        "a backend/OS cell may keep at most two qualified versions"
                    )

        object.__setattr__(
            self, "registry_digest", _digest(self.registry_digest, "registry_digest")
        )
        expected = "sha256:" + canonical_sha256(self.body_primitive())
        if self.registry_digest != expected:
            raise ValueError("registry_digest does not match the registry body")

    def body_primitive(self) -> dict[str, object]:
        return {
            "profiles": [item.to_primitive() for item in self.profiles],
            "records": [item.to_primitive() for item in self.records],
            "schemaVersion": self.schema_version,
        }

    def to_primitive(self) -> dict[str, object]:
        value = self.body_primitive()
        value["registryDigest"] = self.registry_digest
        return value

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    def profile(self, profile_id: str) -> BackendProtocolProfile:
        selected = next(
            (item for item in self.profiles if item.profile_id == profile_id), None
        )
        if selected is None:
            raise KeyError(profile_id)
        return selected

    def profile_for_protocol(
        self, provider: Provider, protocol: str
    ) -> BackendProtocolProfile:
        selected = next(
            (
                item
                for item in self.profiles
                if item.provider is provider and item.protocol == protocol
            ),
            None,
        )
        if selected is None:
            raise KeyError((provider.value, protocol))
        return selected

    def record(
        self, provider: Provider, platform: Platform, backend_version: str
    ) -> BackendVersionQualificationRecord | None:
        return next(
            (
                item
                for item in self.records
                if item.provider is provider
                and item.platform is platform
                and item.backend_version == backend_version
            ),
            None,
        )


__all__ = [
    "BACKEND_PROTOCOL_PROFILE_SCHEMA_VERSION",
    "BACKEND_VERSION_REGISTRY_SCHEMA_VERSION",
    "MAX_BACKEND_PROTOCOL_PROFILES",
    "MAX_BACKEND_VERSION_RECORDS",
    "MAX_QUALIFIED_VERSIONS_PER_CELL",
    "BackendAdapterKind",
    "BackendProtocolProfile",
    "BackendVersionQualificationRecord",
    "BackendVersionRegistry",
    "BackendVersionStatus",
]
