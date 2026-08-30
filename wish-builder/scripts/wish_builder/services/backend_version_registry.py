"""Fail-closed maintenance for the backend version qualification registry."""

from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from pathlib import Path

from wish_builder.compatibility import (
    load_bundled_compatibility,
)
from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.backend_registry import (
    BackendVersionQualificationRecord,
    BackendVersionRegistry,
    BackendVersionStatus,
)
from wish_builder.contracts.backend_registry_decoder import (
    BackendRegistryDecodeError,
    decode_backend_version_registry_bytes,
)
from wish_builder.contracts.compatibility import CompatibilityBundle, Platform, Provider


class BackendVersionRegistryUpdateError(ValueError):
    """Stable failure raised before a registry update can be published."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _require_registry(value: object) -> BackendVersionRegistry:
    if type(value) is not BackendVersionRegistry:
        raise TypeError("registry must be a BackendVersionRegistry")
    return value


def _require_expected_digest(
    registry: BackendVersionRegistry,
    expected_registry_digest: str,
) -> None:
    if registry.registry_digest != expected_registry_digest:
        raise BackendVersionRegistryUpdateError(
            "registry_digest_conflict",
            "The registry changed after this update was prepared.",
        )


def _compatibility_cell(
    provider: Provider,
    platform: Platform,
    bundle: CompatibilityBundle | None,
):
    selected = bundle or load_bundled_compatibility()
    if type(selected) is not CompatibilityBundle:
        raise TypeError("bundle must be a CompatibilityBundle or null")
    return selected, selected.platform(provider, platform)


def _rebuilt_registry(
    registry: BackendVersionRegistry,
    records: tuple[BackendVersionQualificationRecord, ...],
) -> BackendVersionRegistry:
    ordered = tuple(
        sorted(
            records,
            key=lambda item: (
                item.provider.value,
                item.platform.value,
                item.backend_version,
            ),
        )
    )
    body = {
        "profiles": [item.to_primitive() for item in registry.profiles],
        "records": [item.to_primitive() for item in ordered],
        "schemaVersion": registry.schema_version,
    }
    try:
        return BackendVersionRegistry(
            schema_version=registry.schema_version,
            profiles=registry.profiles,
            records=ordered,
            registry_digest="sha256:" + canonical_sha256(body),
        )
    except (TypeError, ValueError) as exc:
        raise BackendVersionRegistryUpdateError(
            "registry_policy_violation", str(exc)
        ) from exc


def prepare_backend_version_candidate(
    registry: BackendVersionRegistry,
    *,
    expected_registry_digest: str,
    provider: Provider,
    platform: Platform,
    backend_version: str,
    protocol_profile: str,
    package_shasum: str,
    package_integrity: str,
    note: str,
    bundle: CompatibilityBundle | None = None,
) -> BackendVersionRegistry:
    """Add one exact version as a non-dispatchable candidate."""

    registry = _require_registry(registry)
    _require_expected_digest(registry, expected_registry_digest)
    if type(provider) is not Provider or type(platform) is not Platform:
        raise TypeError("provider and platform must use compatibility enums")
    selected_bundle, cell = _compatibility_cell(provider, platform, bundle)
    try:
        profile = registry.profile(protocol_profile)
    except KeyError as exc:
        raise BackendVersionRegistryUpdateError(
            "protocol_profile_unknown", "The protocol profile is not registered."
        ) from exc
    provider_entry = next(
        item for item in selected_bundle.providers if item.provider is provider
    )
    if (
        profile.provider is not provider
        or profile.protocol != cell.launch_profile.protocol
        or profile.package_name != provider_entry.sdk.name
    ):
        raise BackendVersionRegistryUpdateError(
            "protocol_profile_mismatch",
            "The protocol profile does not match the backend compatibility cell.",
        )
    candidate = BackendVersionQualificationRecord(
        provider=provider,
        platform=platform,
        backend_version=backend_version,
        protocol_profile=profile.profile_id,
        launch_profile_digest=cell.launch_profile_digest,
        package_shasum=package_shasum,
        package_integrity=package_integrity,
        status=BackendVersionStatus.CANDIDATE,
        max_concurrency=0,
        evidence_digest=None,
        publication_receipt_digest=None,
        review_reference=None,
        note=note,
    )
    existing = registry.record(provider, platform, candidate.backend_version)
    if existing is not None:
        if existing == candidate:
            return registry
        raise BackendVersionRegistryUpdateError(
            "backend_version_exists",
            "The backend/OS/version identity already has a different record.",
        )
    return _rebuilt_registry(registry, (*registry.records, candidate))


def prepare_backend_version_qualification(
    registry: BackendVersionRegistry,
    *,
    expected_registry_digest: str,
    provider: Provider,
    platform: Platform,
    backend_version: str,
    max_concurrency: int,
    evidence_digest: str,
    publication_receipt_digest: str,
    review_reference: str,
    note: str,
) -> BackendVersionRegistry:
    """Promote one reviewed candidate to an exact qualified version."""

    registry = _require_registry(registry)
    _require_expected_digest(registry, expected_registry_digest)
    existing = registry.record(provider, platform, backend_version)
    if existing is None:
        raise BackendVersionRegistryUpdateError(
            "candidate_missing", "Qualification requires an existing candidate record."
        )
    desired = replace(
        existing,
        status=BackendVersionStatus.QUALIFIED,
        max_concurrency=max_concurrency,
        evidence_digest=evidence_digest,
        publication_receipt_digest=publication_receipt_digest,
        review_reference=review_reference,
        note=note,
    )
    if existing == desired:
        return registry
    if existing.status is not BackendVersionStatus.CANDIDATE:
        raise BackendVersionRegistryUpdateError(
            "candidate_not_promotable",
            "Only a candidate record can be promoted to qualified.",
        )
    return _rebuilt_registry(
        registry,
        tuple(desired if item.key == existing.key else item for item in registry.records),
    )


def prepare_backend_version_quarantine(
    registry: BackendVersionRegistry,
    *,
    expected_registry_digest: str,
    provider: Provider,
    platform: Platform,
    backend_version: str,
    review_reference: str,
    note: str,
) -> BackendVersionRegistry:
    """Disable one known version without changing adapter or kernel code."""

    registry = _require_registry(registry)
    _require_expected_digest(registry, expected_registry_digest)
    existing = registry.record(provider, platform, backend_version)
    if existing is None:
        raise BackendVersionRegistryUpdateError(
            "backend_version_missing", "Only a known backend version can be quarantined."
        )
    desired = replace(
        existing,
        status=BackendVersionStatus.QUARANTINED,
        max_concurrency=0,
        review_reference=review_reference,
        note=note,
    )
    if existing == desired:
        return registry
    return _rebuilt_registry(
        registry,
        tuple(desired if item.key == existing.key else item for item in registry.records),
    )


def backend_version_registry_pin_bytes(digest: str) -> bytes:
    """Render the complete generated trust-pin module."""

    if (
        type(digest) is not str
        or len(digest) != 71
        or not digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise BackendVersionRegistryUpdateError(
            "registry_digest_invalid", "The registry digest must be canonical SHA-256."
        )
    return (
        '"""Generated trust pin for the independently published backend version registry."""\n\n'
        "BACKEND_VERSION_REGISTRY_DIGEST = (\n"
        f'    "{digest}"\n'
        ")\n\n"
        '__all__ = ["BACKEND_VERSION_REGISTRY_DIGEST"]\n'
    ).encode("ascii")


def _write_staged(path: Path, raw: bytes) -> Path:
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _replace_bytes(path: Path, raw: bytes) -> None:
    staged = _write_staged(path, raw)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def publish_backend_version_registry(
    registry: BackendVersionRegistry,
    *,
    record_path: Path,
    pin_path: Path,
    expected_current_digest: str,
) -> bool:
    """Publish canonical registry bytes and its pin; return False for a replay.

    Each replacement is atomic. If the second replacement fails, the first is
    restored. A process crash between replacements remains fail closed because
    the bundled loader requires the JSON digest and compiled pin to agree.
    """

    registry = _require_registry(registry)
    for value, name in ((record_path, "record_path"), (pin_path, "pin_path")):
        if not isinstance(value, Path) or not value.is_absolute():
            raise ValueError(f"{name} must be an absolute Path")
        if value.parent != record_path.parent:
            raise ValueError("registry and pin must be in the same directory")
        if value.is_symlink() or not value.is_file():
            raise BackendVersionRegistryUpdateError(
                "publication_target_invalid", f"Unsafe or missing publication target: {value}"
            )

    target_record = registry.canonical_json_bytes()
    target_pin = backend_version_registry_pin_bytes(registry.registry_digest)
    current_record = record_path.read_bytes()
    current_pin = pin_path.read_bytes()
    if current_record == target_record and current_pin == target_pin:
        return False

    try:
        current = decode_backend_version_registry_bytes(current_record)
    except BackendRegistryDecodeError as exc:
        raise BackendVersionRegistryUpdateError(
            "current_registry_invalid",
            "The current registry is not a canonical valid record.",
        ) from exc
    if current_record != current.canonical_json_bytes():
        raise BackendVersionRegistryUpdateError(
            "current_registry_invalid",
            "The current registry bytes are not canonical.",
        )
    if current.registry_digest != expected_current_digest:
        raise BackendVersionRegistryUpdateError(
            "registry_digest_conflict",
            "The registry changed before publication.",
        )
    expected_pin = backend_version_registry_pin_bytes(expected_current_digest)
    if current_pin != expected_pin:
        raise BackendVersionRegistryUpdateError(
            "current_pin_invalid", "The current trust pin bytes are not canonical."
        )

    staged_record = _write_staged(record_path, target_record)
    try:
        staged_pin = _write_staged(pin_path, target_pin)
    except Exception as exc:
        try:
            staged_record.unlink(missing_ok=True)
        except OSError:
            pass
        raise BackendVersionRegistryUpdateError(
            "publication_failed", "Registry publication staging did not complete."
        ) from exc
    record_replaced = False
    pin_replaced = False
    try:
        os.replace(staged_record, record_path)
        record_replaced = True
        os.replace(staged_pin, pin_path)
        pin_replaced = True
    except Exception as exc:
        try:
            if pin_replaced:
                _replace_bytes(pin_path, current_pin)
            if record_replaced:
                _replace_bytes(record_path, current_record)
        except Exception as rollback_exc:
            raise BackendVersionRegistryUpdateError(
                "publication_rollback_failed",
                "Registry publication failed and the previous pair could not be restored.",
            ) from rollback_exc
        raise BackendVersionRegistryUpdateError(
            "publication_failed", "Registry publication did not complete."
        ) from exc
    finally:
        staged_record.unlink(missing_ok=True)
        staged_pin.unlink(missing_ok=True)
    return True


__all__ = [
    "BackendVersionRegistryUpdateError",
    "backend_version_registry_pin_bytes",
    "prepare_backend_version_candidate",
    "prepare_backend_version_qualification",
    "prepare_backend_version_quarantine",
    "publish_backend_version_registry",
]
