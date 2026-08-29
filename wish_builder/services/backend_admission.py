"""Fail-closed backend admission for one frozen execution manifest."""

from __future__ import annotations

import platform as host_platform
from dataclasses import dataclass, replace
from enum import StrEnum

from wish_builder.compatibility import (
    load_bundled_compatibility,
    load_bundled_trellis_compatibility,
)
from wish_builder.contracts.compatibility import (
    CompatibilityBundle,
    DisjointSiblingOverlapEvidence,
    Platform,
    PlatformCompatibility,
    Provider,
    Qualification,
    QualificationArtifact,
    QualificationScenarioEvidence,
)
from wish_builder.contracts.manifest_v2 import (
    ExecutionManifestV2,
    SchedulerMode,
    WorkerProvider,
)


class BackendAdmissionReason(StrEnum):
    NONE = "none"
    UNSUPPORTED_HOST = "unsupported_host"
    SCHEDULER_MISMATCH = "scheduler_mismatch"
    PROVIDER_MISMATCH = "provider_mismatch"
    POLICY_MISMATCH = "policy_mismatch"
    CAPABILITY_MISMATCH = "capability_mismatch"
    LAUNCH_PROFILE_MISMATCH = "launch_profile_mismatch"
    DISPATCH_NOT_QUALIFIED = "dispatch_not_qualified"
    QUALIFICATION_EVIDENCE_MISMATCH = "qualification_evidence_mismatch"
    CONCURRENCY_NOT_QUALIFIED = "concurrency_not_qualified"


@dataclass(frozen=True, slots=True)
class BackendAdmissionResult:
    admitted: bool
    reason: BackendAdmissionReason
    cell: PlatformCompatibility | None = None

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise TypeError("admitted must be a bool")
        if type(self.reason) is not BackendAdmissionReason:
            raise TypeError("reason must be a BackendAdmissionReason")
        if self.cell is not None and type(self.cell) is not PlatformCompatibility:
            raise TypeError("cell must be a PlatformCompatibility or null")
        if self.admitted:
            if self.reason is not BackendAdmissionReason.NONE or self.cell is None:
                raise ValueError("admitted results require a compatibility cell")
        elif self.reason is BackendAdmissionReason.NONE:
            raise ValueError("rejected results require a reason")


_PROVIDERS = {
    WorkerProvider.CODEX: Provider.CODEX,
    WorkerProvider.OH_MY_PI: Provider.OMP,
    WorkerProvider.PI: Provider.PI,
}


def current_platform() -> Platform | None:
    name = host_platform.system().casefold()
    if name == "windows":
        return Platform.WINDOWS
    if name == "linux":
        return Platform.LINUX
    return None


def _revalidate_artifact(
    artifact: object,
) -> QualificationArtifact | None:
    """Re-run immutable artifact invariants at the final admission boundary."""

    if type(artifact) is not QualificationArtifact:
        return None
    try:
        scenarios = tuple(
            replace(item)
            if type(item) is QualificationScenarioEvidence
            else None
            for item in artifact.scenarios
        )
        if any(item is None for item in scenarios):
            return None
        overlap = artifact.disjoint_sibling_overlap
        if overlap is not None:
            if type(overlap) is not DisjointSiblingOverlapEvidence:
                return None
            overlap = replace(overlap)
        return replace(
            artifact,
            scenarios=scenarios,
            disjoint_sibling_overlap=overlap,
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _revalidate_qualification(value: object) -> Qualification | None:
    """Re-run the parent qualification invariants before trusting its enable bit."""

    if type(value) is not Qualification:
        return None
    try:
        source_artifact = value.artifact
        artifact = (
            None
            if source_artifact is None
            else _revalidate_artifact(source_artifact)
        )
        if source_artifact is not None and artifact is None:
            return None
        return replace(value, artifact=artifact)
    except (AttributeError, TypeError, ValueError):
        return None


def _artifact_matches_cell(
    artifact: QualificationArtifact,
    bundle: CompatibilityBundle,
    cell: PlatformCompatibility,
    provider: Provider,
    platform: Platform,
) -> bool:
    provider_entry = next(
        (item for item in bundle.providers if item.provider is provider),
        None,
    )
    return bool(
        provider_entry is not None
        and artifact.provider is provider
        and artifact.platform is platform
        and artifact.trellis_compatibility_digest
        == bundle.trellis_compatibility_digest
        and artifact.sdk == provider_entry.sdk
        and artifact.policy_digest == bundle.policy_digest
        and artifact.launch_profile_digest == cell.launch_profile_digest
        and artifact.capability_digest == cell.capabilities.capability_digest
    )


def admit_backend(
    manifest: ExecutionManifestV2,
    *,
    bundle: CompatibilityBundle | None = None,
    platform: Platform | None = None,
) -> BackendAdmissionResult:
    """Admit exactly one qualified provider/platform cell with matching digests."""

    if type(manifest) is not ExecutionManifestV2:
        raise TypeError("manifest must be an ExecutionManifestV2")
    if bundle is not None and type(bundle) is not CompatibilityBundle:
        raise TypeError("bundle must be a CompatibilityBundle or null")
    if platform is not None and type(platform) is not Platform:
        raise TypeError("platform must be a Platform or null")
    selected_platform = current_platform() if platform is None else platform
    if selected_platform is None:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.UNSUPPORTED_HOST,
        )
    if manifest.scheduler_mode is not SchedulerMode.WISH_BUILDER:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.SCHEDULER_MISMATCH,
        )
    selected_bundle = bundle or load_bundled_compatibility()
    provider = _PROVIDERS.get(manifest.provider)
    if provider is None:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.PROVIDER_MISMATCH,
        )
    cell = selected_bundle.platform(provider, selected_platform)
    if manifest.policy_digest != selected_bundle.policy_digest:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.POLICY_MISMATCH,
            cell,
        )
    if manifest.capability_digest != cell.capabilities.capability_digest:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.CAPABILITY_MISMATCH,
            cell,
        )
    if manifest.launch_profile_digest != cell.launch_profile_digest:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.LAUNCH_PROFILE_MISMATCH,
            cell,
        )
    if not selected_bundle.published:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.DISPATCH_NOT_QUALIFIED,
            cell,
        )
    qualification = _revalidate_qualification(cell.qualification)
    if qualification is None:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            cell,
        )
    if not qualification.enabled_for_dispatch:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.DISPATCH_NOT_QUALIFIED,
            cell,
        )
    artifact = qualification.artifact
    if artifact is None or not _artifact_matches_cell(
        artifact,
        selected_bundle,
        cell,
        provider,
        selected_platform,
    ):
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            cell,
        )
    overlap = artifact.disjoint_sibling_overlap
    if (
        manifest.max_concurrency > artifact.max_concurrent_turns
        or manifest.max_concurrency > artifact.observed_max_concurrent_turns
        or (
            manifest.max_concurrency > 1
            and (
                overlap is None
                or overlap.observed_concurrent_turns < manifest.max_concurrency
            )
        )
    ):
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.CONCURRENCY_NOT_QUALIFIED,
            cell,
        )
    trellis = load_bundled_trellis_compatibility()
    if trellis.compatibility_digest != selected_bundle.trellis_compatibility_digest:
        return BackendAdmissionResult(
            False,
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            cell,
        )
    return BackendAdmissionResult(True, BackendAdmissionReason.NONE, cell)


__all__ = [
    "BackendAdmissionReason",
    "BackendAdmissionResult",
    "admit_backend",
    "current_platform",
]
