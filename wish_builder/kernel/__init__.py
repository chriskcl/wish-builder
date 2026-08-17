"""Pure deterministic Wish Builder kernel."""

from .validation import (
    admit_manifest_bytes,
    admit_manifest_primitive,
    diagnostics_bytes,
    diagnostics_sha256,
    render_diagnostics,
    validate_manifest,
    validate_manifest_bytes,
    validate_manifest_shape,
)


__all__ = [
    "admit_manifest_bytes",
    "admit_manifest_primitive",
    "diagnostics_bytes",
    "diagnostics_sha256",
    "render_diagnostics",
    "validate_manifest",
    "validate_manifest_bytes",
    "validate_manifest_shape",
]
