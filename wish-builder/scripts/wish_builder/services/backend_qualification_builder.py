"""Replay backend evidence and build a non-authorizing qualification candidate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wish_builder.adapters.trellis import (
    SUPPORTED_TRELLIS_EXPORT_VERSION,
    TrellisImportSettings,
    import_trellis_snapshot,
)
from wish_builder.adapters.trellis.graph import _decode_snapshot_bytes
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts import (
    ExecutionIdentity,
    ExecutionManifestV2,
    QualificationArtifact,
    QualificationScenario,
    QualificationScenarioEvidence,
    QualificationStatus,
    canonical_json_bytes,
    canonical_sha256,
    decode_manifest_v2_bytes,
    generated_task_packet_bytes,
)
from wish_builder.contracts.compatibility import (
    QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
    CompatibilityBundle,
    DisjointSiblingOverlapEvidence,
    Provider,
)
from wish_builder.contracts.manifest_v2 import WorkerProvider
from wish_builder.contracts.qualification_evidence import (
    QUALIFICATION_SCENARIO_ORDER,
    AttemptPreparedPayload,
    CancelObservedPayload,
    CancelRequestedPayload,
    ChannelReservedPayload,
    CleanupObservedPayload,
    CleanupRequestedPayload,
    CrashInjectedPayload,
    PrepareRequestedPayload,
    ProcessRestartedPayload,
    QualificationEffectStatus,
    QualificationEvent,
    QualificationEventSource,
    QualificationEventType,
    QualificationEvidenceInventory,
    QualificationEvidenceRole,
    QualificationEvidenceScenario,
    QualificationHarnessDescriptor,
    QualificationProvenance,
    QualificationRunOutcome,
    QualificationTurnState,
    QualificationTurnTerminalState,
    ReconcileInspectedPayload,
    ReconcileRequestedPayload,
    ReserveRequestedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    SendRequestedPayload,
    TaskPacketSentPayload,
    TurnStartedPayload,
    TurnTerminalPayload,
)
from wish_builder.contracts.qualification_evidence_decoder import (
    decode_qualification_event_log_bytes,
    decode_qualification_evidence_inventory_bytes,
    decode_qualification_harness_descriptor_bytes,
    decode_qualification_provenance_bytes,
    validate_qualification_provenance_binding,
)
from wish_builder.kernel.validation import _patterns_overlap
from wish_builder.services.ports.trellis import TrellisGraphSnapshot


CANDIDATE_REPORT_SCHEMA_VERSION = 1
DERIVED_SUMMARY_SCHEMA_VERSION = 1
_PROVIDER_BY_WORKER = {
    WorkerProvider.CODEX: Provider.CODEX,
    WorkerProvider.OH_MY_PI: Provider.OMP,
    WorkerProvider.PI: Provider.PI,
}
_SCENARIO_TO_ARTIFACT = {
    QualificationEvidenceScenario.FULL_TURN: QualificationScenario.FULL_TURN,
    QualificationEvidenceScenario.ACTIVE_TURN_CANCELLATION: (
        QualificationScenario.ACTIVE_TURN_CANCELLATION
    ),
    QualificationEvidenceScenario.CRASH_RECONCILE: (
        QualificationScenario.CRASH_RECONCILE
    ),
    QualificationEvidenceScenario.CLEANUP: QualificationScenario.CLEANUP,
}
_REQUEST_TYPES = (
    PrepareRequestedPayload,
    ReserveRequestedPayload,
    SendRequestedPayload,
    CancelRequestedPayload,
    ReconcileRequestedPayload,
    CleanupRequestedPayload,
)
_EXPECTED_EVENT_SOURCES = {
    QualificationEventType.RUN_STARTED: QualificationEventSource.RUNNER,
    QualificationEventType.PREPARE_REQUESTED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.ATTEMPT_PREPARED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.RESERVE_REQUESTED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.CHANNEL_RESERVED: QualificationEventSource.PROVIDER,
    QualificationEventType.SEND_REQUESTED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.TASK_PACKET_SENT: QualificationEventSource.PROVIDER,
    QualificationEventType.TURN_STARTED: QualificationEventSource.PROVIDER,
    QualificationEventType.TURN_TERMINAL: QualificationEventSource.PROVIDER,
    QualificationEventType.CANCEL_REQUESTED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.CANCEL_OBSERVED: QualificationEventSource.PROVIDER,
    QualificationEventType.CRASH_INJECTED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.PROCESS_RESTARTED: QualificationEventSource.RUNNER,
    QualificationEventType.RECONCILE_REQUESTED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.RECONCILE_INSPECTED: QualificationEventSource.PROVIDER,
    QualificationEventType.CLEANUP_REQUESTED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.CLEANUP_OBSERVED: QualificationEventSource.WISH_BUILDER,
    QualificationEventType.RUN_FINISHED: QualificationEventSource.RUNNER,
}


class BackendQualificationCandidateError(ValueError):
    """Stable fail-closed error for an invalid qualification evidence root."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class QualificationObjectStore:
    root: Path
    inventory: QualificationEvidenceInventory
    objects: tuple[tuple[QualificationEvidenceRole, bytes], ...]

    def bytes_for_role(self, role: QualificationEvidenceRole) -> bytes:
        for candidate, raw in self.objects:
            if candidate is role:
                return raw
        raise LookupError(role)  # pragma: no cover


@dataclass(frozen=True, slots=True)
class _AttemptTrace:
    prepare_requested: tuple[int, QualificationEvent, PrepareRequestedPayload]
    attempt_prepared: tuple[int, QualificationEvent, AttemptPreparedPayload]
    reserve_requested: tuple[int, QualificationEvent, ReserveRequestedPayload]
    channel_reserved: tuple[int, QualificationEvent, ChannelReservedPayload]
    send_requested: tuple[int, QualificationEvent, SendRequestedPayload]
    task_packet_sent: tuple[int, QualificationEvent, TaskPacketSentPayload]
    turn_started: tuple[int, QualificationEvent, TurnStartedPayload]
    turn_terminal: tuple[int, QualificationEvent, TurnTerminalPayload]

    @property
    def task_id(self) -> str:
        return self.prepare_requested[2].task_id

    @property
    def attempt_id(self) -> str:
        return self.prepare_requested[2].attempt_id


@dataclass(frozen=True, slots=True)
class _VerifiedScenario:
    scenario: QualificationEvidenceScenario
    summary_digest: str
    summary_bytes: bytes


@dataclass(frozen=True, slots=True)
class _VerifiedOverlap:
    summary_digest: str
    summary_bytes: bytes
    observed_concurrent_turns: int
    sibling_task_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackendQualificationCandidate:
    inventory: QualificationEvidenceInventory
    artifact: QualificationArtifact
    evidence_objects: tuple[tuple[str, bytes], ...]
    derived_objects: tuple[tuple[str, bytes], ...]
    report_bytes: bytes

    @property
    def report(self) -> dict[str, object]:
        return json.loads(self.report_bytes)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _safe_artifact_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*relative.split("/"))
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise BackendQualificationCandidateError(
            "artifact_path_escape", f"Artifact path escapes evidence root: {relative}"
        ) from exc
    return candidate


def load_qualification_object_store(root: Path) -> QualificationObjectStore:
    """Load one bounded, canonical, symlink-free evidence inventory."""

    if not isinstance(root, Path):
        raise TypeError("root must be a Path")
    if not root.is_dir() or root.is_symlink():
        raise BackendQualificationCandidateError(
            "invalid_root", "Evidence root must be a real directory."
        )
    inventory_path = root / "inventory.json"
    if not inventory_path.is_file() or inventory_path.is_symlink():
        raise BackendQualificationCandidateError(
            "inventory_missing", "inventory.json is missing or is a symlink."
        )
    raw_inventory = inventory_path.read_bytes()
    decoded = decode_qualification_evidence_inventory_bytes(raw_inventory)
    if not decoded.ok:
        raise BackendQualificationCandidateError(
            "inventory_invalid", decoded.report.render_text().strip()
        )
    assert decoded.value is not None
    inventory = decoded.value
    if raw_inventory != inventory.canonical_json_bytes():
        raise BackendQualificationCandidateError(
            "inventory_not_canonical", "inventory.json must use canonical JSON bytes."
        )

    expected_files = {"inventory.json"}
    loaded: list[tuple[QualificationEvidenceRole, bytes]] = []
    total_bytes = len(raw_inventory)
    for artifact in inventory.artifacts:
        path = _safe_artifact_path(root, artifact.path)
        expected_files.add(artifact.path)
        if path.is_symlink() or not path.is_file():
            raise BackendQualificationCandidateError(
                "artifact_missing", f"{artifact.role.value} is missing or is a symlink."
            )
        raw = path.read_bytes()
        total_bytes += len(raw)
        if total_bytes > 64 * 1_024 * 1_024:
            raise BackendQualificationCandidateError(
                "evidence_too_large", "Evidence root exceeds the 64 MiB input limit."
            )
        if len(raw) != artifact.byte_length:
            raise BackendQualificationCandidateError(
                "artifact_size_mismatch", f"{artifact.role.value} size does not match inventory."
            )
        if _digest(raw) != artifact.digest:
            raise BackendQualificationCandidateError(
                "artifact_digest_mismatch", f"{artifact.role.value} digest does not match its bytes."
            )
        loaded.append((artifact.role, raw))

    discovered_files: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackendQualificationCandidateError(
                "evidence_symlink", "Evidence root must not contain symlinks."
            )
        if path.is_file():
            discovered_files.add(path.relative_to(root).as_posix())
    if discovered_files != expected_files:
        raise BackendQualificationCandidateError(
            "artifact_inventory_mismatch", "Evidence root contains missing or unlisted files."
        )
    return QualificationObjectStore(root, inventory, tuple(loaded))


def _decode_harness(store: QualificationObjectStore) -> QualificationHarnessDescriptor:
    raw = store.bytes_for_role(QualificationEvidenceRole.HARNESS_DESCRIPTOR)
    decoded = decode_qualification_harness_descriptor_bytes(raw)
    if not decoded.ok:
        raise BackendQualificationCandidateError(
            "harness_invalid", decoded.report.render_text().strip()
        )
    assert decoded.value is not None
    if raw != decoded.value.canonical_json_bytes():
        raise BackendQualificationCandidateError(
            "harness_not_canonical", "Harness descriptor must use canonical JSON bytes."
        )
    return decoded.value


def _decode_provenance(store: QualificationObjectStore) -> QualificationProvenance:
    raw = store.bytes_for_role(QualificationEvidenceRole.PROVENANCE)
    decoded = decode_qualification_provenance_bytes(raw)
    if not decoded.ok:
        raise BackendQualificationCandidateError(
            "provenance_invalid", decoded.report.render_text().strip()
        )
    assert decoded.value is not None
    provenance = decoded.value
    if raw != provenance.canonical_json_bytes():
        raise BackendQualificationCandidateError(
            "provenance_not_canonical", "Detached provenance must use canonical JSON bytes."
        )
    binding = validate_qualification_provenance_binding(store.inventory, provenance)
    if not binding.ok:
        raise BackendQualificationCandidateError(
            "provenance_binding_invalid", binding.report.render_text().strip()
        )
    return provenance


def _decode_manifest(store: QualificationObjectStore) -> tuple[ExecutionManifestV2, bytes, str]:
    artifact = store.inventory.artifact(QualificationEvidenceRole.EXECUTION_MANIFEST)
    raw = store.bytes_for_role(QualificationEvidenceRole.EXECUTION_MANIFEST)
    decoded = decode_manifest_v2_bytes(raw)
    if not decoded.ok:
        raise BackendQualificationCandidateError(
            "manifest_invalid", decoded.report.render_text().strip()
        )
    assert decoded.value is not None
    if raw != decoded.value.canonical_json_bytes():
        raise BackendQualificationCandidateError(
            "manifest_not_canonical", "Execution manifest must use canonical JSON bytes."
        )
    return decoded.value, raw, artifact.digest


def _rederive_manifest(
    store: QualificationObjectStore,
    manifest: ExecutionManifestV2,
    manifest_bytes: bytes,
) -> str:
    artifact = store.inventory.artifact(QualificationEvidenceRole.TRELLIS_SNAPSHOT)
    raw = store.bytes_for_role(QualificationEvidenceRole.TRELLIS_SNAPSHOT)
    try:
        payload = _decode_snapshot_bytes(raw)
        if type(payload) is not dict or raw != canonical_json_bytes(payload):
            raise ValueError("Trellis snapshot must use canonical JSON bytes")
        snapshot = TrellisGraphSnapshot(
            export_version=SUPPORTED_TRELLIS_EXPORT_VERSION,
            trellis_version="0.6.15",
            parent_task_id=payload["parent_task_id"],  # type: ignore[arg-type]
            revision=payload["revision"],  # type: ignore[arg-type]
            observed_at=manifest.imported_at,
            snapshot_bytes=raw,
            source_sha256=artifact.digest,
            complete=True,
        )
        settings = TrellisImportSettings(
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
        rebuilt = import_trellis_snapshot(snapshot, settings).manifest
    except (KeyError, TypeError, ValueError) as exc:
        raise BackendQualificationCandidateError("snapshot_rebuild_failed", str(exc)) from exc
    if rebuilt.canonical_json_bytes() != manifest_bytes:
        raise BackendQualificationCandidateError(
            "snapshot_manifest_mismatch", "Trellis snapshot does not deterministically rebuild the manifest."
        )
    return artifact.digest


def _verify_cell(
    inventory: QualificationEvidenceInventory,
    manifest: ExecutionManifestV2,
    bundle: CompatibilityBundle,
) -> None:
    provider = _PROVIDER_BY_WORKER[manifest.provider]
    if provider is not inventory.provider:
        raise BackendQualificationCandidateError(
            "provider_mismatch", "Manifest and inventory providers differ."
        )
    cell = bundle.platform(provider, inventory.platform)
    if (
        manifest.policy_digest != bundle.policy_digest
        or manifest.launch_profile_digest != cell.launch_profile_digest
        or manifest.capability_digest != cell.capabilities.capability_digest
    ):
        raise BackendQualificationCandidateError(
            "manifest_pin_mismatch", "Manifest policy, launch profile, or capability does not match the bundled cell."
        )


def _decode_events(store: QualificationObjectStore) -> tuple[QualificationEvent, ...]:
    raw = store.bytes_for_role(QualificationEvidenceRole.EVENT_LOG)
    decoded = decode_qualification_event_log_bytes(raw)
    if not decoded.ok:
        raise BackendQualificationCandidateError(
            "event_log_invalid", decoded.report.render_text().strip()
        )
    assert decoded.value is not None
    for event in decoded.value:
        if event.source is not _EXPECTED_EVENT_SOURCES[event.event_type]:
            raise BackendQualificationCandidateError(
                "event_source_mismatch",
                f"{event.event_type.value} has an invalid evidence source.",
            )
    return decoded.value


def _split_scenarios(
    events: tuple[QualificationEvent, ...],
) -> dict[QualificationEvidenceScenario, tuple[QualificationEvent, ...]]:
    groups: dict[QualificationEvidenceScenario, list[QualificationEvent]] = {
        scenario: [] for scenario in QUALIFICATION_SCENARIO_ORDER
    }
    observed_order: list[QualificationEvidenceScenario] = []
    active: QualificationEvidenceScenario | None = None
    for event in events:
        if active is None:
            if event.event_type is not QualificationEventType.RUN_STARTED:
                raise BackendQualificationCandidateError(
                    "scenario_start_missing", "Every scenario segment must start with run_started."
                )
            active = event.scenario
            if active in observed_order:
                raise BackendQualificationCandidateError(
                    "scenario_reopened", "Scenario segments must be contiguous and unique."
                )
            observed_order.append(active)
        if event.scenario is not active:
            raise BackendQualificationCandidateError(
                "scenario_interleaved", "Scenario events cannot be interleaved."
            )
        groups[active].append(event)
        if event.event_type is QualificationEventType.RUN_FINISHED:
            active = None
    if active is not None or tuple(observed_order) != QUALIFICATION_SCENARIO_ORDER:
        raise BackendQualificationCandidateError(
            "scenario_set_invalid", "Event log must contain the five scenarios once in canonical order."
        )
    for scenario, group in groups.items():
        if (
            not group
            or sum(item.event_type is QualificationEventType.RUN_STARTED for item in group) != 1
            or sum(item.event_type is QualificationEventType.RUN_FINISHED for item in group) != 1
            or group[-1].event_type is not QualificationEventType.RUN_FINISHED
        ):
            raise BackendQualificationCandidateError(
                "scenario_boundary_invalid", f"{scenario.value} has invalid run boundaries."
            )
    return {scenario: tuple(group) for scenario, group in groups.items()}


def _verify_run_identity(
    groups: dict[QualificationEvidenceScenario, tuple[QualificationEvent, ...]],
    store: QualificationObjectStore,
    harness: QualificationHarnessDescriptor,
    provenance: QualificationProvenance,
    manifest_digest: str,
    snapshot_digest: str,
    bundle: CompatibilityBundle,
) -> None:
    if harness.source_revision != provenance.source_revision:
        raise BackendQualificationCandidateError(
            "source_revision_mismatch", "Harness and provenance source revisions differ."
        )
    first_event = next(iter(groups.values()))[0]
    if (
        first_event.qualification_run_id != store.inventory.qualification_run_id
        or first_event.provider is not store.inventory.provider
        or first_event.platform is not store.inventory.platform
    ):
        raise BackendQualificationCandidateError(
            "event_inventory_identity_mismatch",
            "Event-log run, provider, and platform must match the evidence inventory.",
        )
    provider_entry = next(item for item in bundle.providers if item.provider is store.inventory.provider)
    cell = bundle.platform(store.inventory.provider, store.inventory.platform)
    for scenario, events in groups.items():
        start = events[0].payload
        finish = events[-1].payload
        if type(start) is not RunStartedPayload or type(finish) is not RunFinishedPayload:
            raise BackendQualificationCandidateError(
                "scenario_payload_invalid", f"{scenario.value} has wrong boundary payloads."
            )
        expected = (
            start.source_revision == harness.source_revision
            and start.harness_digest
            == store.inventory.artifact(QualificationEvidenceRole.HARNESS_DESCRIPTOR).digest
            and start.harness_version == harness.harness_version
            and start.trellis_compatibility_digest == bundle.trellis_compatibility_digest
            and start.policy_digest == bundle.policy_digest
            and start.launch_profile_digest == cell.launch_profile_digest
            and start.capability_digest == cell.capabilities.capability_digest
            and start.manifest_digest == manifest_digest
            and start.trellis_snapshot_digest == snapshot_digest
            and start.sdk_name == provider_entry.sdk.name
            and start.sdk_version == provider_entry.sdk.version
            and start.sdk_shasum == provider_entry.sdk.shasum
            and finish.outcome is QualificationRunOutcome.COMPLETED
        )
        if not expected:
            raise BackendQualificationCandidateError(
                "run_identity_mismatch", f"{scenario.value} does not bind the admitted harness, pins, and artifacts."
            )


def _only(
    events: tuple[QualificationEvent, ...],
    payload_type: type,
    *,
    attempt_id: str | None = None,
) -> tuple[int, QualificationEvent, object]:
    matches = tuple(
        (index, event, event.payload)
        for index, event in enumerate(events)
        if type(event.payload) is payload_type
        and (attempt_id is None or getattr(event.payload, "attempt_id", None) == attempt_id)
    )
    if len(matches) != 1:
        raise BackendQualificationCandidateError(
            "event_cardinality", f"Expected one {payload_type.__name__}; found {len(matches)}."
        )
    return matches[0]


def _attempt_ids(events: tuple[QualificationEvent, ...]) -> tuple[str, ...]:
    values = tuple(
        event.payload.attempt_id
        for event in events
        if type(event.payload) is PrepareRequestedPayload
    )
    if not values or len(set(values)) != len(values):
        raise BackendQualificationCandidateError(
            "attempt_identity_invalid", "Prepare requests require unique attempt IDs."
        )
    return values


def _trace_attempt(events: tuple[QualificationEvent, ...], attempt_id: str) -> _AttemptTrace:
    entries = (
        _only(events, PrepareRequestedPayload, attempt_id=attempt_id),
        _only(events, AttemptPreparedPayload, attempt_id=attempt_id),
        _only(events, ReserveRequestedPayload, attempt_id=attempt_id),
        _only(events, ChannelReservedPayload, attempt_id=attempt_id),
        _only(events, SendRequestedPayload, attempt_id=attempt_id),
        _only(events, TaskPacketSentPayload, attempt_id=attempt_id),
        _only(events, TurnStartedPayload, attempt_id=attempt_id),
        _only(events, TurnTerminalPayload, attempt_id=attempt_id),
    )
    trace = _AttemptTrace(*entries)  # type: ignore[arg-type]
    if tuple(item[0] for item in entries) != tuple(sorted(item[0] for item in entries)):
        raise BackendQualificationCandidateError(
            "attempt_order_invalid", "Attempt prepare, reserve, send, and turn events are out of order."
        )
    prepare, prepared = trace.prepare_requested[2], trace.attempt_prepared[2]
    if prepare.to_primitive() != prepared.to_primitive():
        raise BackendQualificationCandidateError(
            "prepare_correlation_invalid", "attempt_prepared does not match prepare_requested."
        )
    reserve, reserved = trace.reserve_requested[2], trace.channel_reserved[2]
    reserve_fields = ("operation_id", "dispatch_id", "attempt_id", "task_id", "channel_id")
    if any(getattr(reserve, field) != getattr(reserved, field) for field in reserve_fields):
        raise BackendQualificationCandidateError(
            "reserve_correlation_invalid", "channel_reserved does not match reserve_requested."
        )
    send, sent = trace.send_requested[2], trace.task_packet_sent[2]
    send_fields = (
        "operation_id",
        "dispatch_id",
        "attempt_id",
        "task_id",
        "channel_id",
        "task_packet",
        "task_packet_digest",
    )
    if any(getattr(send, field) != getattr(sent, field) for field in send_fields):
        raise BackendQualificationCandidateError(
            "send_correlation_invalid", "task_packet_sent does not match send_requested."
        )
    packet_bytes = send.task_packet.encode("utf-8", errors="strict")
    if _digest(packet_bytes) != send.task_packet_digest:
        raise BackendQualificationCandidateError(
            "task_packet_digest_mismatch",
            "Task packet bytes do not match the recorded digest.",
        )
    started, terminal = trace.turn_started[2], trace.turn_terminal[2]
    turn_fields = (
        "dispatch_id",
        "attempt_id",
        "task_id",
        "channel_id",
        "provider_session_id",
        "provider_message_id",
        "provider_turn_id",
    )
    if any(getattr(started, field) != getattr(terminal, field) for field in turn_fields):
        raise BackendQualificationCandidateError(
            "turn_correlation_invalid", "turn_terminal identifies a different logical turn."
        )
    if (
        prepare.dispatch_id != reserve.dispatch_id
        or prepare.dispatch_id != send.dispatch_id
        or prepare.dispatch_id != started.dispatch_id
        or prepare.task_id != reserve.task_id
        or prepare.task_id != send.task_id
        or prepare.task_id != started.task_id
        or reserve.channel_id != send.channel_id
        or reserve.channel_id != started.channel_id
        or reserved.provider_session_id != sent.provider_session_id
        or reserved.provider_session_id != started.provider_session_id
    ):
        raise BackendQualificationCandidateError(
            "attempt_identity_changed", "Attempt identity changed across operations."
        )
    operation_ids = (prepare.operation_id, reserve.operation_id, send.operation_id)
    if len(set(operation_ids)) != len(operation_ids):
        raise BackendQualificationCandidateError(
            "operation_id_reused", "Prepare, reserve, and send require distinct operation IDs."
        )
    if trace.turn_started[1].monotonic_ns >= trace.turn_terminal[1].monotonic_ns:
        raise BackendQualificationCandidateError(
            "turn_interval_empty", "Turn interval must have positive monotonic duration."
        )
    return trace


def _reject_extra_attempt_events(
    events: tuple[QualificationEvent, ...],
    attempts: tuple[str, ...],
    allowed_types: tuple[type, ...],
) -> None:
    operation_ids: list[str] = []
    for event in events:
        payload = event.payload
        if type(payload) in _REQUEST_TYPES:
            operation_ids.append(payload.operation_id)  # type: ignore[attr-defined]
        attempt_id = getattr(payload, "attempt_id", None)
        if attempt_id is not None and attempt_id not in attempts:
            raise BackendQualificationCandidateError(
                "unknown_attempt", "Event references an unbound attempt."
            )
        if (
            event.event_type
            not in {QualificationEventType.RUN_STARTED, QualificationEventType.RUN_FINISHED}
            and type(payload) not in allowed_types
        ):
            raise BackendQualificationCandidateError(
                "scenario_event_extra", "Scenario contains an unexpected event type."
            )
    duplicates = {value for value in operation_ids if operation_ids.count(value) > 1}
    if duplicates:
        reconcile_ids = {
            event.payload.operation_id
            for event in events
            if type(event.payload) is ReconcileRequestedPayload
        }
        send_ids = {
            event.payload.operation_id
            for event in events
            if type(event.payload) is SendRequestedPayload
        }
        if duplicates != reconcile_ids or duplicates != send_ids:
            raise BackendQualificationCandidateError(
                "operation_id_reused", "Operation IDs were reused across logical effects."
            )


def _scenario_summary(
    scenario: QualificationEvidenceScenario,
    events: tuple[QualificationEvent, ...],
    inventory_digest: str,
    event_log_digest: str,
    details: dict[str, object],
) -> _VerifiedScenario:
    primitive = {
        "details": details,
        "eventDigestEnd": events[-1].event_digest,
        "eventDigestStart": events[0].event_digest,
        "eventLogDigest": event_log_digest,
        "evidenceInventoryDigest": inventory_digest,
        "qualificationRunId": events[0].qualification_run_id,
        "result": "passed",
        "scenario": scenario.value,
        "schemaVersion": DERIVED_SUMMARY_SCHEMA_VERSION,
    }
    raw = canonical_json_bytes(primitive)
    return _VerifiedScenario(scenario, _digest(raw), raw)


def _verify_attempt_manifest_binding(
    trace: _AttemptTrace,
    manifest: ExecutionManifestV2,
    max_task_packet_bytes: int,
) -> None:
    task_map = {item.id: item for item in manifest.tasks}
    mapping = {item.task_id: item.trellis_task_id for item in manifest.task_id_mapping}
    prepare = trace.prepare_requested[2]
    task = task_map.get(prepare.task_id)
    if task is None or mapping.get(prepare.task_id) != prepare.trellis_task_id:
        raise BackendQualificationCandidateError(
            "attempt_task_unbound",
            "Attempt task identity does not map to the frozen Trellis manifest.",
        )
    writable = tuple(sorted(task.owned_paths + task.allowed_auxiliary_paths))
    if tuple(sorted(prepare.owned_paths)) != writable:
        raise BackendQualificationCandidateError(
            "attempt_paths_unbound",
            "Attempt writable paths differ from the frozen manifest.",
        )
    send = trace.send_requested[2]
    packet_bytes = send.task_packet.encode("utf-8", errors="strict")
    if len(packet_bytes) > max_task_packet_bytes:
        raise BackendQualificationCandidateError(
            "task_packet_too_large",
            "Task packet exceeds the qualified backend capability.",
        )
    if task.task_packet_template_digest is not None:
        if send.task_packet_digest != task.task_packet_template_digest:
            raise BackendQualificationCandidateError(
                "task_packet_template_mismatch",
                "Task packet does not match the approved template digest.",
            )
        return
    try:
        primitive = json.loads(send.task_packet)
        if (
            type(primitive) is not dict
            or canonical_json_bytes(primitive) != packet_bytes
        ):
            raise ValueError("packet is not canonical JSON")
        execution = primitive["execution"]
        if type(execution) is not dict:
            raise ValueError("packet execution is not an object")
        identity_value = execution["identity"]
        if type(identity_value) is not dict or set(identity_value) != {
            "attempt",
            "coordinator_epoch",
            "correlation_id",
            "run_id",
            "task_id",
        }:
            raise ValueError("packet identity is not closed")
        identity = ExecutionIdentity(
            run_id=identity_value["run_id"],
            coordinator_epoch=identity_value["coordinator_epoch"],
            task_id=identity_value["task_id"],
            attempt=identity_value["attempt"],
            correlation_id=identity_value["correlation_id"],
        )
        if identity.correlation_id != send.dispatch_id:
            raise ValueError("packet dispatch identity changed")
        expected = generated_task_packet_bytes(
            manifest,
            task,
            mapping[task.id],
            identity,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BackendQualificationCandidateError(
            "task_packet_manifest_mismatch",
            "Task packet cannot be derived from the frozen manifest and attempt identity.",
        ) from exc
    if packet_bytes != expected:
        raise BackendQualificationCandidateError(
            "task_packet_manifest_mismatch",
            "Task packet differs from the deterministic frozen-manifest packet.",
        )


def _verify_single_scenario(
    scenario: QualificationEvidenceScenario,
    events: tuple[QualificationEvent, ...],
    inventory_digest: str,
    event_log_digest: str,
    manifest: ExecutionManifestV2,
    max_task_packet_bytes: int,
) -> _VerifiedScenario:
    attempts = _attempt_ids(events)
    if len(attempts) != 1:
        raise BackendQualificationCandidateError(
            "scenario_attempt_count", f"{scenario.value} requires exactly one attempt."
        )
    trace = _trace_attempt(events, attempts[0])
    _verify_attempt_manifest_binding(trace, manifest, max_task_packet_bytes)
    base_types = (
        PrepareRequestedPayload,
        AttemptPreparedPayload,
        ReserveRequestedPayload,
        ChannelReservedPayload,
        SendRequestedPayload,
        TaskPacketSentPayload,
        TurnStartedPayload,
        TurnTerminalPayload,
    )
    details: dict[str, object] = {
        "attemptId": trace.attempt_id,
        "taskId": trace.task_id,
        "terminalState": trace.turn_terminal[2].terminal_state.value,
    }
    if scenario is QualificationEvidenceScenario.FULL_TURN:
        _reject_extra_attempt_events(events, attempts, base_types)
        if trace.turn_terminal[2].terminal_state is not QualificationTurnTerminalState.DONE:
            raise BackendQualificationCandidateError(
                "full_turn_terminal", "Full turn must finish as done."
            )
    elif scenario is QualificationEvidenceScenario.ACTIVE_TURN_CANCELLATION:
        _reject_extra_attempt_events(
            events, attempts, base_types + (CancelRequestedPayload, CancelObservedPayload)
        )
        requested = _only(events, CancelRequestedPayload, attempt_id=attempts[0])
        observed = _only(events, CancelObservedPayload, attempt_id=attempts[0])
        request, observation = requested[2], observed[2]
        identity = (
            "operation_id", "dispatch_id", "attempt_id", "task_id", "channel_id",
            "provider_session_id", "provider_message_id", "provider_turn_id",
        )
        if (
            any(getattr(request, field) != getattr(observation, field) for field in identity)
            or observation.effect_status is not QualificationEffectStatus.APPLIED
            or not trace.turn_started[0] < requested[0] < observed[0] < trace.turn_terminal[0]
            or trace.turn_terminal[2].terminal_state is not QualificationTurnTerminalState.CANCELLED
            or request.operation_id in {
                trace.prepare_requested[2].operation_id,
                trace.reserve_requested[2].operation_id,
                trace.send_requested[2].operation_id,
            }
        ):
            raise BackendQualificationCandidateError(
                "cancellation_invalid", "Cancellation must correlate to the active turn and finish cancelled."
            )
        details["cancelOperationId"] = request.operation_id
    elif scenario is QualificationEvidenceScenario.CRASH_RECONCILE:
        _reject_extra_attempt_events(
            events,
            attempts,
            base_types
            + (
                CrashInjectedPayload,
                ProcessRestartedPayload,
                ReconcileRequestedPayload,
                ReconcileInspectedPayload,
            ),
        )
        crashed = _only(events, CrashInjectedPayload, attempt_id=attempts[0])
        restarted = _only(events, ProcessRestartedPayload)
        requested = _only(events, ReconcileRequestedPayload, attempt_id=attempts[0])
        inspected = _only(events, ReconcileInspectedPayload, attempt_id=attempts[0])
        crash, restart = crashed[2], restarted[2]
        reconcile, inspection = requested[2], inspected[2]
        turn_fields = (
            "dispatch_id", "attempt_id", "task_id", "channel_id",
            "provider_session_id", "provider_message_id", "provider_turn_id",
        )
        expected_request_digest = "sha256:" + canonical_sha256(
            trace.send_requested[2].to_primitive()
        )
        if (
            crash.operation_id != trace.send_requested[2].operation_id
            or reconcile.operation_id != crash.operation_id
            or inspection.operation_id != reconcile.operation_id
            or reconcile.request_digest != expected_request_digest
            or inspection.request_digest != reconcile.request_digest
            or any(getattr(crash, field) != getattr(reconcile, field) for field in turn_fields)
            or any(getattr(reconcile, field) != getattr(inspection, field) for field in turn_fields)
            or crash.failpoint != "after-send-before-journal"
            or crashed[1].process_identity != trace.task_packet_sent[1].process_identity
            or restart.previous_process_identity != crashed[1].process_identity
            or restarted[1].process_identity == crashed[1].process_identity
            or requested[1].process_identity != restarted[1].process_identity
            or inspected[1].process_identity != restarted[1].process_identity
            or trace.turn_terminal[1].process_identity != restarted[1].process_identity
            or inspection.effect_status is not QualificationEffectStatus.APPLIED
            or inspection.turn_state not in {QualificationTurnState.RUNNING, QualificationTurnState.DONE}
            or not trace.task_packet_sent[0] < crashed[0] < restarted[0] < requested[0] < inspected[0]
            or inspected[0] >= trace.turn_terminal[0]
            or trace.turn_terminal[2].terminal_state is not QualificationTurnTerminalState.DONE
        ):
            raise BackendQualificationCandidateError(
                "crash_reconcile_invalid", "Crash recovery must inspect the original send without a second logical turn."
            )
        if (
            inspection.turn_state is QualificationTurnState.DONE
            and inspection.result_digest != trace.turn_terminal[2].result_digest
        ):
            raise BackendQualificationCandidateError(
                "reconcile_result_mismatch", "Reconciled result digest changed."
            )
        details["recoveryId"] = restart.recovery_id
    elif scenario is QualificationEvidenceScenario.CLEANUP:
        _reject_extra_attempt_events(
            events, attempts, base_types + (CleanupRequestedPayload, CleanupObservedPayload)
        )
        requested = _only(events, CleanupRequestedPayload, attempt_id=attempts[0])
        observed = _only(events, CleanupObservedPayload, attempt_id=attempts[0])
        request, observation = requested[2], observed[2]
        identity = (
            "operation_id", "dispatch_id", "attempt_id", "task_id", "channel_id",
            "provider_session_id", "worktree_id", "process_tree_ids",
        )
        target_resources = {
            *(f"process:{item}" for item in request.process_tree_ids),
            f"channel:{request.channel_id}",
            f"provider_session:{request.provider_session_id}",
            f"worktree:{request.worktree_id}",
        }
        resources_before = set(observation.resources_before)
        resources_after = set(observation.resources_after)
        resource_transition_valid = (
            target_resources <= resources_before
            and resources_after == resources_before - target_resources
            and bool(resources_after)
        )
        if (
            any(getattr(request, field) != getattr(observation, field) for field in identity)
            or not resource_transition_valid
            or request.channel_id != trace.channel_reserved[2].channel_id
            or request.provider_session_id != trace.channel_reserved[2].provider_session_id
            or request.worktree_id != trace.attempt_prepared[2].worktree_id
            or request.operation_id in {
                trace.prepare_requested[2].operation_id,
                trace.reserve_requested[2].operation_id,
                trace.send_requested[2].operation_id,
            }
            or not trace.turn_terminal[0] < requested[0] < observed[0]
            or trace.turn_terminal[2].terminal_state is not QualificationTurnTerminalState.DONE
        ):
            raise BackendQualificationCandidateError(
                "cleanup_invalid", "Cleanup must remove the exact attempt resources and preserve siblings."
            )
        details["cleanupOperationId"] = request.operation_id
        details["preservedResourceIds"] = sorted(resources_after)
        details["removedResourceIds"] = sorted(target_resources)
    else:  # pragma: no cover
        raise BackendQualificationCandidateError("scenario_invalid", scenario.value)
    return _scenario_summary(scenario, events, inventory_digest, event_log_digest, details)


def _reachable(manifest: ExecutionManifestV2, start: str, target: str) -> bool:
    task_map = {item.id: item for item in manifest.tasks}
    pending = list(task_map[start].depends_on)
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current not in seen:
            seen.add(current)
            pending.extend(task_map[current].depends_on)
    return False


def _verify_overlap(
    events: tuple[QualificationEvent, ...],
    inventory_digest: str,
    event_log_digest: str,
    manifest: ExecutionManifestV2,
    max_task_packet_bytes: int,
) -> _VerifiedOverlap:
    attempts = _attempt_ids(events)
    if len(attempts) < 2:
        raise BackendQualificationCandidateError(
            "overlap_attempt_count", "Sibling overlap requires at least two attempts."
        )
    base_types = (
        PrepareRequestedPayload,
        AttemptPreparedPayload,
        ReserveRequestedPayload,
        ChannelReservedPayload,
        SendRequestedPayload,
        TaskPacketSentPayload,
        TurnStartedPayload,
        TurnTerminalPayload,
    )
    _reject_extra_attempt_events(events, attempts, base_types)
    traces = tuple(_trace_attempt(events, attempt_id) for attempt_id in attempts)
    for trace in traces:
        _verify_attempt_manifest_binding(trace, manifest, max_task_packet_bytes)
    task_map = {item.id: item for item in manifest.tasks}
    mapping = {item.task_id: item.trellis_task_id for item in manifest.task_id_mapping}
    task_ids: list[str] = []
    worktrees: set[str] = set()
    dispatches: set[str] = set()
    channels: set[str] = set()
    sessions: set[str] = set()
    messages: set[str] = set()
    turns: set[str] = set()
    intervals: list[dict[str, object]] = []
    for trace in traces:
        prepare = trace.prepare_requested[2]
        task = task_map.get(prepare.task_id)
        if task is None or mapping.get(prepare.task_id) != prepare.trellis_task_id:
            raise BackendQualificationCandidateError(
                "overlap_task_unknown", "Overlap attempt does not map to the frozen manifest."
            )
        writable = tuple(sorted(task.owned_paths + task.allowed_auxiliary_paths))
        if tuple(sorted(prepare.owned_paths)) != writable:
            raise BackendQualificationCandidateError(
                "overlap_paths_unbound", "Recorded writable paths differ from the manifest."
            )
        if trace.turn_terminal[2].terminal_state is not QualificationTurnTerminalState.DONE:
            raise BackendQualificationCandidateError(
                "overlap_turn_failed", "Every sibling overlap turn must finish done."
            )
        task_ids.append(task.id)
        dispatches.add(prepare.dispatch_id)
        channels.add(trace.channel_reserved[2].channel_id)
        worktrees.add(prepare.worktree_id)
        sessions.add(trace.channel_reserved[2].provider_session_id)
        messages.add(trace.task_packet_sent[2].provider_message_id)
        turns.add(trace.turn_started[2].provider_turn_id)
        intervals.append(
            {
                "endNs": trace.turn_terminal[1].monotonic_ns,
                "startNs": trace.turn_started[1].monotonic_ns,
                "taskId": task.id,
                "turnId": trace.turn_started[2].provider_turn_id,
            }
        )
    distinct_count = len(task_ids)
    if (
        len(set(task_ids)) != distinct_count
        or min(
            len(dispatches),
            len(channels),
            len(worktrees),
            len(sessions),
            len(messages),
            len(turns),
        )
        != distinct_count
    ):
        raise BackendQualificationCandidateError(
            "overlap_identity_reuse", "Sibling attempts require distinct dispatch, channel, task, worktree, session, message, and turn IDs."
        )
    for index, left_id in enumerate(task_ids):
        for right_id in task_ids[index + 1 :]:
            if _reachable(manifest, left_id, right_id) or _reachable(manifest, right_id, left_id):
                raise BackendQualificationCandidateError(
                    "overlap_not_siblings", "Dependency ancestors cannot qualify as siblings."
                )
            left = task_map[left_id]
            right = task_map[right_id]
            left_paths = left.owned_paths + left.allowed_auxiliary_paths
            right_paths = right.owned_paths + right.allowed_auxiliary_paths
            if any(_patterns_overlap(a, b) for a in left_paths for b in right_paths):
                raise BackendQualificationCandidateError(
                    "overlap_paths_conflict", "Sibling writable paths are not provably disjoint."
                )
    sweep = [
        point
        for interval in intervals
        for point in (
            (int(interval["startNs"]), 1),
            (int(interval["endNs"]), -1),
        )
    ]
    active = 0
    observed = 0
    for _, delta in sorted(sweep, key=lambda item: (item[0], item[1])):
        active += delta
        observed = max(observed, active)
    if observed < 2 or observed < manifest.max_concurrency:
        raise BackendQualificationCandidateError(
            "overlap_not_observed", "Turn intervals do not prove the manifest concurrency limit."
        )
    sibling_ids = tuple(sorted(task_ids))
    primitive = {
        "eventDigestEnd": events[-1].event_digest,
        "eventDigestStart": events[0].event_digest,
        "eventLogDigest": event_log_digest,
        "evidenceInventoryDigest": inventory_digest,
        "intervals": sorted(intervals, key=lambda item: str(item["taskId"])),
        "observedConcurrentTurns": observed,
        "ownedPaths": {
            task_id: list(task_map[task_id].owned_paths + task_map[task_id].allowed_auxiliary_paths)
            for task_id in sibling_ids
        },
        "ownedPathsDisjoint": True,
        "overlapObserved": True,
        "qualificationRunId": events[0].qualification_run_id,
        "scenario": QualificationEvidenceScenario.SIBLING_OVERLAP.value,
        "schemaVersion": DERIVED_SUMMARY_SCHEMA_VERSION,
        "siblingTaskIds": list(sibling_ids),
    }
    raw = canonical_json_bytes(primitive)
    return _VerifiedOverlap(_digest(raw), raw, observed, sibling_ids)


def _build_artifact(
    store: QualificationObjectStore,
    harness: QualificationHarnessDescriptor,
    manifest: ExecutionManifestV2,
    bundle: CompatibilityBundle,
    scenarios: dict[QualificationEvidenceScenario, _VerifiedScenario],
    overlap: _VerifiedOverlap,
) -> QualificationArtifact:
    scenario_values = tuple(
        QualificationScenarioEvidence(
            evidence_digest=scenarios[evidence_scenario].summary_digest,
            live=True,
            name=artifact_scenario,
            status=QualificationStatus.PASSED,
        )
        for evidence_scenario, artifact_scenario in _SCENARIO_TO_ARTIFACT.items()
    )
    overlap_value = DisjointSiblingOverlapEvidence(
        evidence_digest=overlap.summary_digest,
        observed_concurrent_turns=overlap.observed_concurrent_turns,
        sibling_task_ids=overlap.sibling_task_ids,
        owned_paths_disjoint=True,
        overlap_observed=True,
    )
    cell = bundle.platform(store.inventory.provider, store.inventory.platform)
    provider_entry = next(item for item in bundle.providers if item.provider is store.inventory.provider)
    harness_digest = store.inventory.artifact(QualificationEvidenceRole.HARNESS_DESCRIPTOR).digest
    body = {
        "capabilityDigest": cell.capabilities.capability_digest,
        "disjointSiblingOverlap": overlap_value.to_primitive(),
        "harnessDigest": harness_digest,
        "harnessVersion": harness.harness_version,
        "launchProfileDigest": cell.launch_profile_digest,
        "maxConcurrentTurns": manifest.max_concurrency,
        "observedMaxConcurrentTurns": overlap.observed_concurrent_turns,
        "platform": store.inventory.platform.value,
        "policyDigest": bundle.policy_digest,
        "provider": store.inventory.provider.value,
        "scenarios": {item.name.value: item.to_primitive() for item in scenario_values},
        "schemaVersion": QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
        "sdk": provider_entry.sdk.to_primitive(),
        "trellisCompatibilityDigest": bundle.trellis_compatibility_digest,
    }
    return QualificationArtifact(
        artifact_digest="sha256:" + canonical_sha256(body),
        capability_digest=cell.capabilities.capability_digest,
        disjoint_sibling_overlap=overlap_value,
        harness_digest=harness_digest,
        harness_version=harness.harness_version,
        launch_profile_digest=cell.launch_profile_digest,
        max_concurrent_turns=manifest.max_concurrency,
        observed_max_concurrent_turns=overlap.observed_concurrent_turns,
        platform=store.inventory.platform,
        policy_digest=bundle.policy_digest,
        provider=store.inventory.provider,
        scenarios=scenario_values,
        schema_version=QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
        sdk=provider_entry.sdk,
        trellis_compatibility_digest=bundle.trellis_compatibility_digest,
    )


def verify_backend_qualification_candidate(
    evidence_root: Path,
    *,
    bundle: CompatibilityBundle | None = None,
) -> BackendQualificationCandidate:
    """Verify a root and return a candidate without changing dispatch state."""

    store = load_qualification_object_store(evidence_root)
    harness = _decode_harness(store)
    provenance = _decode_provenance(store)
    manifest, manifest_bytes, manifest_digest = _decode_manifest(store)
    snapshot_digest = _rederive_manifest(store, manifest, manifest_bytes)
    selected_bundle = bundle or load_bundled_compatibility()
    _verify_cell(store.inventory, manifest, selected_bundle)
    selected_cell = selected_bundle.platform(store.inventory.provider, store.inventory.platform)
    events = _decode_events(store)
    groups = _split_scenarios(events)
    _verify_run_identity(
        groups, store, harness, provenance, manifest_digest, snapshot_digest, selected_bundle
    )
    inventory_digest = store.inventory.digest()
    event_log_digest = store.inventory.artifact(QualificationEvidenceRole.EVENT_LOG).digest
    verified = {
        scenario: _verify_single_scenario(
            scenario,
            groups[scenario],
            inventory_digest,
            event_log_digest,
            manifest,
            selected_cell.capabilities.max_task_packet_bytes,
        )
        for scenario in _SCENARIO_TO_ARTIFACT
    }
    overlap = _verify_overlap(
        groups[QualificationEvidenceScenario.SIBLING_OVERLAP],
        inventory_digest,
        event_log_digest,
        manifest,
        selected_cell.capabilities.max_task_packet_bytes,
    )
    artifact = _build_artifact(store, harness, manifest, selected_bundle, verified, overlap)
    derived = tuple(
        sorted(
            (
                *((item.summary_digest, item.summary_bytes) for item in verified.values()),
                (overlap.summary_digest, overlap.summary_bytes),
            ),
            key=lambda item: item[0],
        )
    )
    report = {
        "candidateArtifactDigest": artifact.artifact_digest,
        "dispatchAuthorized": False,
        "enabledForDispatch": False,
        "evidenceRoot": "evidence",
        "evidenceInventoryDigest": inventory_digest,
        "limitations": [
            "The candidate does not modify bundled qualification data or compiled trust pins.",
            "Detached provenance is structurally bound but must still be verified by its issuer and reviewed by a human.",
            "Dispatch remains disabled until this provider and platform evidence is independently verified and published.",
            "Trellis projection is a separate single-writer operation and is never performed by backend workers.",
        ],
        "platform": store.inventory.platform.value,
        "provenanceKind": provenance.kind.value,
        "provenanceReference": provenance.reference,
        "provenanceStatus": "detached_reference_structurally_bound",
        "provider": store.inventory.provider.value,
        "published": False,
        "qualificationRunId": store.inventory.qualification_run_id,
        "scenarioDigests": {
            scenario.value: verified[scenario].summary_digest
            for scenario in _SCENARIO_TO_ARTIFACT
        },
        "schemaVersion": CANDIDATE_REPORT_SCHEMA_VERSION,
        "siblingOverlapDigest": overlap.summary_digest,
        "status": "candidate_unverified",
    }
    return BackendQualificationCandidate(
        inventory=store.inventory,
        artifact=artifact,
        evidence_objects=(
            ("inventory.json", store.inventory.canonical_json_bytes()),
            *(
                (store.inventory.artifact(role).path, store.bytes_for_role(role))
                for role in QualificationEvidenceRole
            ),
        ),
        derived_objects=derived,
        report_bytes=canonical_json_bytes(report),
    )


def build_backend_qualification_candidate(
    evidence_root: Path,
    output_root: Path,
    *,
    bundle: CompatibilityBundle | None = None,
) -> BackendQualificationCandidate:
    """Atomically materialize a self-contained, non-authorizing candidate."""

    if not isinstance(output_root, Path):
        raise TypeError("output_root must be a Path")
    if output_root.exists() or output_root.is_symlink():
        raise BackendQualificationCandidateError(
            "output_exists", "Candidate output directory already exists."
        )
    candidate = verify_backend_qualification_candidate(evidence_root, bundle=bundle)
    output_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".qualification-candidate-", dir=output_root.parent)
    )
    try:
        for relative, raw in candidate.evidence_objects:
            target = (temporary / "evidence").joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        derived_root = temporary / "derived" / "sha256"
        derived_root.mkdir(parents=True, exist_ok=True)
        for digest, raw in candidate.derived_objects:
            (derived_root / f"{digest.removeprefix('sha256:')}.json").write_bytes(raw)
        (temporary / "candidate-artifact.json").write_bytes(
            canonical_json_bytes(candidate.artifact.to_primitive())
        )
        (temporary / "verification-report.json").write_bytes(candidate.report_bytes)
        os.replace(temporary, output_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return candidate


__all__ = [
    "BackendQualificationCandidate",
    "BackendQualificationCandidateError",
    "QualificationObjectStore",
    "build_backend_qualification_candidate",
    "load_qualification_object_store",
    "verify_backend_qualification_candidate",
]
