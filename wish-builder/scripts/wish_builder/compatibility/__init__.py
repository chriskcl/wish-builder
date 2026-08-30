"""Load independently pinned Trellis and backend qualification contracts."""

from __future__ import annotations

from importlib.resources import files

from wish_builder.compatibility._backend_qualification_pin import (
    BACKEND_QUALIFICATION_DIGESTS,
)
from wish_builder.compatibility._backend_version_registry_pin import (
    BACKEND_VERSION_REGISTRY_DIGEST,
)
from wish_builder.contracts.backend_registry import BackendVersionRegistry
from wish_builder.contracts.backend_registry_decoder import (
    BackendRegistryDecodeError,
    decode_backend_version_registry_bytes,
)
from wish_builder.contracts.compatibility import (
    SUPPORTED_TRELLIS_VERSION,
    BackendQualificationBundle,
    CompatibilityBundle,
    TrellisCompatibility,
)
from wish_builder.contracts.compatibility_decoder import (
    decode_backend_qualification_bundle_bytes,
    decode_trellis_compatibility_bytes,
)

DEFAULT_TRELLIS_VERSION = SUPPORTED_TRELLIS_VERSION
BUNDLED_TRELLIS_COMPATIBILITY_DIGESTS = {
    DEFAULT_TRELLIS_VERSION: (
        "sha256:fd3601e3507f8e2befe914e94afff04c07dedfb55d30417d3b35370bbfacf235"
    )
}
BUNDLED_BACKEND_QUALIFICATION_DIGESTS = dict(BACKEND_QUALIFICATION_DIGESTS)

# Compatibility-facing name retained for services that consume the backend bundle.
BUNDLED_COMPATIBILITY_DIGESTS = BUNDLED_BACKEND_QUALIFICATION_DIGESTS


class BundledCompatibilityError(RuntimeError):
    """Raised when shipped compatibility data is missing, stale, or tampered."""


def _validated_version(trellis_version: str) -> str:
    if (
        type(trellis_version) is not str
        or trellis_version not in BUNDLED_TRELLIS_COMPATIBILITY_DIGESTS
        or trellis_version not in BUNDLED_BACKEND_QUALIFICATION_DIGESTS
    ):
        raise ValueError("trellis_version does not identify bundled compatibility data")
    return trellis_version


def _resource_bytes(filename: str, trellis_version: str) -> bytes:
    resource = files(__package__).joinpath(filename)
    try:
        return resource.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise BundledCompatibilityError(
            f"bundled compatibility data is unavailable for Trellis {trellis_version}"
        ) from exc


def bundled_trellis_compatibility_bytes(
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> bytes:
    version = _validated_version(trellis_version)
    return _resource_bytes(f"trellis-{version}.json", version)


def admit_bundled_trellis_compatibility_bytes(
    raw: bytes,
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> TrellisCompatibility:
    """Admit official package data only when schema, bytes, and pin agree."""

    version = _validated_version(trellis_version)
    decoded = decode_trellis_compatibility_bytes(raw)
    if not decoded.ok:
        raise BundledCompatibilityError(decoded.report.render_text().rstrip())
    assert decoded.value is not None
    compatibility = decoded.value
    if compatibility.trellis_version != version:
        raise BundledCompatibilityError(
            "bundled Trellis compatibility version does not match its resource name"
        )
    if (
        compatibility.compatibility_digest
        != BUNDLED_TRELLIS_COMPATIBILITY_DIGESTS[version]
    ):
        raise BundledCompatibilityError(
            "bundled Trellis compatibility digest does not match the compiled trust pin"
        )
    if raw != compatibility.canonical_json_bytes():
        raise BundledCompatibilityError(
            "bundled Trellis compatibility bytes are not in canonical form"
        )
    return compatibility


def load_bundled_trellis_compatibility(
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> TrellisCompatibility:
    return admit_bundled_trellis_compatibility_bytes(
        bundled_trellis_compatibility_bytes(trellis_version), trellis_version
    )


def bundled_backend_qualification_bytes(
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> bytes:
    version = _validated_version(trellis_version)
    return _resource_bytes(f"backend-qualification-{version}.json", version)


def admit_bundled_backend_qualification_bytes(
    raw: bytes,
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> BackendQualificationBundle:
    """Admit backend qualification only when schema, bytes, and pin agree."""

    version = _validated_version(trellis_version)
    decoded = decode_backend_qualification_bundle_bytes(raw)
    if not decoded.ok:
        raise BundledCompatibilityError(decoded.report.render_text().rstrip())
    assert decoded.value is not None
    bundle = decoded.value
    if bundle.bundle_digest != BUNDLED_BACKEND_QUALIFICATION_DIGESTS[version]:
        raise BundledCompatibilityError(
            "bundled backend qualification digest does not match the compiled trust pin"
        )
    if raw != bundle.canonical_json_bytes():
        raise BundledCompatibilityError(
            "bundled backend qualification bytes are not in canonical form"
        )
    return bundle


def load_bundled_backend_qualification(
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> BackendQualificationBundle:
    return admit_bundled_backend_qualification_bytes(
        bundled_backend_qualification_bytes(trellis_version), trellis_version
    )


def bundled_backend_version_registry_bytes() -> bytes:
    return _resource_bytes("backend-version-registry.json", "backend-version-registry")


def admit_bundled_backend_version_registry_bytes(
    raw: bytes,
) -> BackendVersionRegistry:
    """Admit the backend version matrix only when bytes and trust pin agree."""

    try:
        registry = decode_backend_version_registry_bytes(raw)
    except BackendRegistryDecodeError as exc:
        raise BundledCompatibilityError(str(exc)) from exc
    if registry.registry_digest != BACKEND_VERSION_REGISTRY_DIGEST:
        raise BundledCompatibilityError(
            "bundled backend version registry digest does not match the compiled trust pin"
        )
    if raw != registry.canonical_json_bytes():
        raise BundledCompatibilityError(
            "bundled backend version registry bytes are not in canonical form"
        )
    return registry


def load_bundled_backend_version_registry() -> BackendVersionRegistry:
    return admit_bundled_backend_version_registry_bytes(
        bundled_backend_version_registry_bytes()
    )


def bundled_compatibility_bytes(
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> bytes:
    return bundled_backend_qualification_bytes(trellis_version)


def admit_bundled_compatibility_bytes(
    raw: bytes,
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> CompatibilityBundle:
    bundle = admit_bundled_backend_qualification_bytes(raw, trellis_version)
    trellis = load_bundled_trellis_compatibility(trellis_version)
    return admit_backend_qualification_for_trellis(bundle, trellis)


def admit_backend_qualification_for_trellis(
    bundle: BackendQualificationBundle,
    trellis: TrellisCompatibility,
) -> BackendQualificationBundle:
    """Verify the backend record references the admitted Trellis contract."""

    if type(bundle) is not BackendQualificationBundle:
        raise TypeError("bundle must be a BackendQualificationBundle")
    if type(trellis) is not TrellisCompatibility:
        raise TypeError("trellis must be a TrellisCompatibility")
    if bundle.trellis_compatibility_digest != trellis.compatibility_digest:
        raise BundledCompatibilityError(
            "backend qualification does not reference bundled Trellis compatibility"
        )
    return bundle


def load_bundled_compatibility(
    trellis_version: str = DEFAULT_TRELLIS_VERSION,
) -> CompatibilityBundle:
    return admit_bundled_compatibility_bytes(
        bundled_compatibility_bytes(trellis_version), trellis_version
    )


__all__ = [
    "BUNDLED_BACKEND_QUALIFICATION_DIGESTS",
    "BUNDLED_COMPATIBILITY_DIGESTS",
    "BUNDLED_TRELLIS_COMPATIBILITY_DIGESTS",
    "BACKEND_VERSION_REGISTRY_DIGEST",
    "DEFAULT_TRELLIS_VERSION",
    "BundledCompatibilityError",
    "admit_backend_qualification_for_trellis",
    "admit_bundled_backend_qualification_bytes",
    "admit_bundled_backend_version_registry_bytes",
    "admit_bundled_compatibility_bytes",
    "admit_bundled_trellis_compatibility_bytes",
    "bundled_backend_qualification_bytes",
    "bundled_backend_version_registry_bytes",
    "bundled_compatibility_bytes",
    "bundled_trellis_compatibility_bytes",
    "load_bundled_backend_qualification",
    "load_bundled_backend_version_registry",
    "load_bundled_compatibility",
    "load_bundled_trellis_compatibility",
]
