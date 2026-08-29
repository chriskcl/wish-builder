"""Shared type boundary for admitted execution-manifest models."""

from __future__ import annotations

from typing import TypeAlias, TypeGuard

from .manifest_v2 import ExecutionManifestV2
from .models import ExecutionManifest

ExecutionManifestModel: TypeAlias = ExecutionManifest | ExecutionManifestV2


def is_execution_manifest_model(value: object) -> TypeGuard[ExecutionManifestModel]:
    """Accept only the two immutable models produced by strict decoders."""

    return type(value) in {ExecutionManifest, ExecutionManifestV2}


__all__ = ["ExecutionManifestModel", "is_execution_manifest_model"]
