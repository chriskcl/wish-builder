"""Fail-closed admission against the current Trellis task graph."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from wish_builder.adapters.trellis.graph import (
    TrellisGraphImportError,
    TrellisImportSettings,
    import_trellis_snapshot,
)
from wish_builder.contracts.manifest_v2 import ExecutionManifestV2
from wish_builder.services.ports.trellis import TrellisGraphPort, TrellisGraphSnapshot


class TrellisGraphAdmissionReason(StrEnum):
    NONE = "none"
    GRAPH_UNAVAILABLE = "graph_unavailable"
    GRAPH_UNSTABLE = "graph_unstable"
    GRAPH_INVALID = "graph_invalid"
    GRAPH_CHANGED = "graph_changed"


@dataclass(frozen=True, slots=True)
class TrellisGraphAdmissionResult:
    admitted: bool
    reason: TrellisGraphAdmissionReason
    graph_digest: str | None = None
    revision: str | None = None

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise TypeError("admitted must be a boolean")
        if type(self.reason) is not TrellisGraphAdmissionReason:
            raise TypeError("reason must be a TrellisGraphAdmissionReason")
        if self.admitted != (self.reason is TrellisGraphAdmissionReason.NONE):
            raise ValueError("admission status and reason are inconsistent")
        if self.admitted and self.graph_digest is None:
            raise ValueError("admitted graph requires its derived digest")


class TrellisGraphAdmissionService:
    """Re-read Trellis and compare its derived graph with the Gate-B snapshot."""

    def __init__(
        self,
        manifest: ExecutionManifestV2,
        graph_port: TrellisGraphPort,
    ) -> None:
        if type(manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if not isinstance(graph_port, TrellisGraphPort):
            raise TypeError("graph_port must implement TrellisGraphPort")
        self._manifest = manifest
        self._graph_port = graph_port
        self._settings = TrellisImportSettings(
            run_id=manifest.run_id,
            goal=manifest.goal,
            base_branch=manifest.base_branch,
            imported_at=manifest.imported_at,
            gate_a=manifest.approvals.gate_a,
            provider=manifest.provider,
            capability_digest=manifest.capability_digest,
            launch_profile_digest=manifest.launch_profile_digest,
            policy_digest=manifest.policy_digest,
            execution_budget=manifest.execution_budget,
            max_concurrency=manifest.max_concurrency,
            lease_ttl_seconds=manifest.lease_ttl_seconds,
            lease_clock_skew_seconds=manifest.lease_clock_skew_seconds,
            path_case_mode=manifest.path_case_mode,
            protected_paths=manifest.protected_paths,
        )

    def admit(self) -> TrellisGraphAdmissionResult:
        """Perform two stable exports and recompile the Gate-B graph material."""

        try:
            first = self._graph_port.export_snapshot(
                self._manifest.trellis_parent_task_id
            )
            second = self._graph_port.export_snapshot(
                self._manifest.trellis_parent_task_id
            )
        except Exception:  # noqa: BLE001 - an external read is an admission boundary
            return TrellisGraphAdmissionResult(
                False,
                TrellisGraphAdmissionReason.GRAPH_UNAVAILABLE,
            )
        if type(first) is not TrellisGraphSnapshot or type(second) is not TrellisGraphSnapshot:
            return TrellisGraphAdmissionResult(
                False,
                TrellisGraphAdmissionReason.GRAPH_UNAVAILABLE,
            )
        if not _same_snapshot_material(first, second):
            return TrellisGraphAdmissionResult(
                False,
                TrellisGraphAdmissionReason.GRAPH_UNSTABLE,
            )
        try:
            imported = import_trellis_snapshot(
                second,
                self._settings,
                approved_graph_digest=self._manifest.trellis_graph_digest,
            )
        except (TrellisGraphImportError, TypeError, ValueError):
            return TrellisGraphAdmissionResult(
                False,
                TrellisGraphAdmissionReason.GRAPH_INVALID,
                revision=second.revision,
            )
        if (
            imported.gate_b_invalidated
            or imported.trellis_graph_digest != self._manifest.trellis_graph_digest
            or imported.manifest.task_id_mapping != self._manifest.task_id_mapping
        ):
            return TrellisGraphAdmissionResult(
                False,
                TrellisGraphAdmissionReason.GRAPH_CHANGED,
                imported.trellis_graph_digest,
                second.revision,
            )
        return TrellisGraphAdmissionResult(
            True,
            TrellisGraphAdmissionReason.NONE,
            imported.trellis_graph_digest,
            second.revision,
        )


def _same_snapshot_material(
    left: TrellisGraphSnapshot,
    right: TrellisGraphSnapshot,
) -> bool:
    # observed_at is intentionally excluded; it is the time of each read, not
    # part of the editable graph.
    return (
        left.export_version == right.export_version
        and left.trellis_version == right.trellis_version
        and left.parent_task_id == right.parent_task_id
        and left.revision == right.revision
        and left.snapshot_bytes == right.snapshot_bytes
        and left.source_sha256 == right.source_sha256
        and left.complete is right.complete
    )


__all__ = [
    "TrellisGraphAdmissionReason",
    "TrellisGraphAdmissionResult",
    "TrellisGraphAdmissionService",
]
