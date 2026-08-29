"""Deterministic, bounded trace projections over a verified Journal stream."""

from __future__ import annotations

import hashlib
import html
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from wish_builder.contracts.execution import (
    ExecutionManifestModel,
    is_execution_manifest_model,
)
from wish_builder.contracts.manifest_v2 import ExecutionManifestV2, ManifestTask
from wish_builder.contracts.models import Task
from wish_builder.contracts.runtime import (
    DecisionObservedPayload,
    DecisionRequestPayload,
    EffectObservationPayload,
    EffectRequestPayload,
    EvidenceRef,
    JournalEvent,
    RecoveryPayload,
    TransitionPayload,
)
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.kernel.graph_index import GraphIndex, GraphIndexError
from wish_builder.kernel.state import GENESIS_HASH, KernelSnapshot

TRACE_SCHEMA_VERSION = 1
DEFAULT_MAX_TRACE_EVENTS = 100_000
DEFAULT_MAX_TRACE_COMMANDS = 10_000
DEFAULT_MAX_TRACE_EVIDENCE = 10_000
DEFAULT_MAX_TRACE_OUTPUT_BYTES = 16 * 1024 * 1024

JsonObject: TypeAlias = dict[str, object]


class TraceExportFault(StrEnum):
    """Closed failure domain for deterministic trace construction."""

    INVALID_INPUT = "invalid_input"
    GRAPH_MISMATCH = "graph_mismatch"
    EVENT_CHAIN_MISMATCH = "event_chain_mismatch"
    EVENT_LIMIT_EXCEEDED = "event_limit_exceeded"
    COMMAND_LIMIT_EXCEEDED = "command_limit_exceeded"
    EVIDENCE_LIMIT_EXCEEDED = "evidence_limit_exceeded"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"


TraceFaultCode = TraceExportFault
TraceExportReason = TraceExportFault


class TraceExportError(ValueError):
    """A stable trace/export failure safe for CLI or service translation."""

    def __init__(
        self,
        fault: TraceExportFault,
        detail: str,
        *,
        sequence: int | None = None,
    ) -> None:
        if type(fault) is not TraceExportFault:
            raise TypeError("fault must be a TraceExportFault")
        self.fault = fault
        self.code = fault
        self.reason = fault.value
        self.detail = detail
        self.sequence = sequence
        location = "" if sequence is None else f" at sequence {sequence}"
        super().__init__(f"{fault.value}{location}: {detail}")


@dataclass(frozen=True, slots=True)
class TraceLimits:
    """Hard caps that keep Journal projection and rendering bounded."""

    max_events: int = DEFAULT_MAX_TRACE_EVENTS
    max_commands: int = DEFAULT_MAX_TRACE_COMMANDS
    max_evidence: int = DEFAULT_MAX_TRACE_EVIDENCE
    max_output_bytes: int = DEFAULT_MAX_TRACE_OUTPUT_BYTES

    def __post_init__(self) -> None:
        for name in ("max_events", "max_commands", "max_evidence"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.max_output_bytes) is not int or self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")


DEFAULT_TRACE_LIMITS = TraceLimits()


@dataclass(frozen=True, slots=True)
class TraceProjection:
    """Immutable canonical projection shared by every trace renderer."""

    _canonical_bytes: bytes
    max_output_bytes: int = DEFAULT_MAX_TRACE_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if type(self._canonical_bytes) is not bytes:
            raise TypeError("_canonical_bytes must be bytes")
        if type(self.max_output_bytes) is not int or self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        if len(self._canonical_bytes) > self.max_output_bytes:
            raise TraceExportError(
                TraceExportFault.OUTPUT_LIMIT_EXCEEDED,
                "canonical JSON exceeds the configured output byte cap",
            )
        try:
            value = json.loads(self._canonical_bytes.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "projection bytes must contain canonical UTF-8 JSON"
            ) from exc
        if (
            type(value) is not dict
            or value.get("schema_version") != TRACE_SCHEMA_VERSION
        ):
            raise ValueError("projection bytes contain an unsupported trace schema")
        if canonical_json_bytes(value) != self._canonical_bytes:
            raise ValueError("projection bytes are not canonical JSON")

    @classmethod
    def _from_primitive(
        cls,
        value: JsonObject,
        *,
        max_output_bytes: int,
    ) -> TraceProjection:
        try:
            raw = canonical_json_bytes(value)
        except (TypeError, ValueError) as exc:
            raise TraceExportError(
                TraceExportFault.INVALID_INPUT,
                "trace projection cannot be serialized canonically",
            ) from exc
        if len(raw) > max_output_bytes:
            raise TraceExportError(
                TraceExportFault.OUTPUT_LIMIT_EXCEEDED,
                "canonical JSON exceeds the configured output byte cap",
            )
        return cls(raw, max_output_bytes)

    def to_primitive(self) -> JsonObject:
        value = json.loads(self._canonical_bytes.decode("utf-8", errors="strict"))
        if type(value) is not dict:  # Defensive; __post_init__ proves this invariant.
            raise AssertionError("trace projection root is not an object")
        return value

    def canonical_json_bytes(self) -> bytes:
        return self._canonical_bytes

    def canonical_sha256(self) -> str:
        return _sha256_bytes(self._canonical_bytes)

    @property
    def digest(self) -> str:
        return self.canonical_sha256()


@dataclass(frozen=True, slots=True)
class TraceArtifacts:
    """The two deterministic renderings and their independent byte hashes."""

    projection: TraceProjection
    json_bytes: bytes
    markdown_bytes: bytes
    json_sha256: str
    markdown_sha256: str

    def __post_init__(self) -> None:
        if type(self.projection) is not TraceProjection:
            raise TypeError("projection must be a TraceProjection")
        if self.json_bytes != self.projection.canonical_json_bytes():
            raise ValueError("json_bytes must match the canonical projection")
        if self.json_sha256 != _sha256_bytes(self.json_bytes):
            raise ValueError("json_sha256 does not match json_bytes")
        if self.markdown_sha256 != _sha256_bytes(self.markdown_bytes):
            raise ValueError("markdown_sha256 does not match markdown_bytes")


TraceExport = TraceArtifacts


def build_trace_projection(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
    journal_events: Iterable[JournalEvent],
    *,
    limits: TraceLimits = DEFAULT_TRACE_LIMITS,
) -> TraceProjection:
    """Validate one Journal stream and compile its bounded trace projection."""

    _require_typed_inputs(manifest, snapshot, graph_index, limits)
    _require_graph_match(manifest, snapshot, graph_index)
    commands, evidence, journal = _scan_journal(
        manifest.run_id,
        snapshot,
        journal_events,
        limits,
    )

    snapshot_value = _snapshot_primitive(snapshot, graph_index)
    task_values = _task_projections(manifest, snapshot, graph_index)
    requirement_values = _requirement_projections(manifest, graph_index)
    attempt_values = _attempt_projections(snapshot, graph_index)
    source = _manifest_source(manifest)
    component_digests = {
        "commands": _sha256_value(commands),
        "evidence": _sha256_value(evidence),
        "graph_index": graph_index.digest,
        "manifest": _sha256_bytes(manifest.canonical_json_bytes()),
        "snapshot": _sha256_value(snapshot_value),
    }
    run = {
        "coordinator_epoch": snapshot.coordinator_epoch,
        "event_count": journal["event_count"],
        "goal": manifest.goal,
        "last_event_hash": snapshot.last_event_hash,
        "last_event_id": snapshot.last_event_id,
        "last_event_type": journal["last_event_type"],
        "last_recorded_at": journal["last_recorded_at"],
        "last_sequence": snapshot.last_sequence,
        "manifest_schema_version": manifest.schema_version,
        "phase": snapshot.phase.value,
        "reason_code": (
            None if snapshot.run_reason_code is None else snapshot.run_reason_code.value
        ),
        "run_id": manifest.run_id,
        "source": source,
        "status": snapshot.status.value,
    }
    primitive: JsonObject = {
        "attempts": attempt_values,
        "commands": commands,
        "component_digests": component_digests,
        "evidence": evidence,
        "requirements": requirement_values,
        "run": run,
        "schema_version": TRACE_SCHEMA_VERSION,
        "tasks": task_values,
    }
    return TraceProjection._from_primitive(
        primitive,
        max_output_bytes=limits.max_output_bytes,
    )


compile_trace = build_trace_projection


def render_trace_json(
    projection: TraceProjection,
    *,
    max_output_bytes: int | None = None,
) -> bytes:
    """Return the canonical JSON rendering without reparsing the Journal."""

    _require_projection(projection)
    cap = _render_cap(projection, max_output_bytes)
    raw = projection.canonical_json_bytes()
    _require_output_size(raw, cap, "canonical JSON")
    return raw


def render_trace_markdown(
    projection: TraceProjection,
    *,
    max_output_bytes: int | None = None,
) -> bytes:
    """Render a safe, stable Markdown view from the canonical projection."""

    _require_projection(projection)
    cap = _render_cap(projection, max_output_bytes)
    value = projection.to_primitive()
    writer = _MarkdownWriter(cap)

    writer.line("# Wish Builder Execution Trace")
    writer.line()
    writer.line("## Run")
    writer.line()
    writer.line("| Field | Value |")
    writer.line("| --- | --- |")
    run = _object(value["run"])
    for key in (
        "run_id",
        "goal",
        "manifest_schema_version",
        "phase",
        "status",
        "reason_code",
        "coordinator_epoch",
        "event_count",
        "last_sequence",
        "last_event_id",
        "last_event_type",
        "last_event_hash",
        "last_recorded_at",
    ):
        writer.line(f"| {_md_cell(key)} | {_md_cell(run.get(key))} |")
    source = _object(run["source"])
    for key in sorted(source):
        writer.line(f"| {_md_cell('source.' + key)} | {_md_cell(source[key])} |")

    writer.line()
    writer.line("## Component Digests")
    writer.line()
    writer.line("| Component | SHA-256 |")
    writer.line("| --- | --- |")
    digests = _object(value["component_digests"])
    for key in sorted(digests):
        writer.line(f"| {_md_cell(key)} | {_md_cell(digests[key])} |")

    writer.line()
    writer.line("## Requirements")
    writer.line()
    writer.line("| ID | Status | Decision | Tasks | Requirement |")
    writer.line("| --- | --- | --- | --- | --- |")
    requirements = _objects(value["requirements"])
    for requirement in requirements:
        writer.line(
            "| {id} | {status} | {decision} | {tasks} | {text} |".format(
                id=_md_cell(requirement["id"]),
                status=_md_cell(requirement["status"]),
                decision=_md_cell(requirement["decision_ref"]),
                tasks=_md_list(requirement["task_ids"]),
                text=_md_cell(requirement["text"]),
            )
        )

    writer.line()
    writer.line("## Tasks")
    writer.line()
    writer.line(
        "| Pos | Task | Trellis task | Wave | State | Reason | Dependencies | "
        "Requirements | Title |"
    )
    writer.line("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    tasks = _objects(value["tasks"])
    for task in tasks:
        writer.line(
            "| {position} | {id} | {trellis_id} | {wave} | {state} | {reason} | "
            "{deps} | {requirements} | {title} |".format(
                position=_md_cell(task["topological_position"]),
                id=_md_cell(task["id"]),
                trellis_id=_md_cell(task["trellis_task_id"]),
                wave=_md_cell(task["wave"]),
                state=_md_cell(task["state"]),
                reason=_md_cell(task["reason_code"]),
                deps=_md_list(task["depends_on"]),
                requirements=_md_list(task["requirement_ids"]),
                title=_md_cell(task["title"]),
            )
        )

    writer.line()
    writer.line("### Task Evidence")
    writer.line()
    writer.line(
        "| Task | Owned paths | Auxiliary paths | Acceptance | Regression | "
        "Definition digest |"
    )
    writer.line("| --- | --- | --- | --- | --- | --- |")
    for task in tasks:
        writer.line(
            "| {id} | {owned} | {auxiliary} | {acceptance} | {regression} | "
            "{digest} |".format(
                id=_md_cell(task["id"]),
                owned=_md_list(task["owned_paths"]),
                auxiliary=_md_list(task["allowed_auxiliary_paths"]),
                acceptance=_md_list(task["acceptance_criteria"]),
                regression=_md_list(task["regression_commands"]),
                digest=_md_cell(task["definition_digest"]),
            )
        )

    writer.line()
    writer.line("## Attempts")
    writer.line()
    attempts = _objects(value["attempts"])
    if attempts:
        writer.line("| Task | Attempt | Epoch | State | Reason | Correlation |")
        writer.line("| --- | --- | --- | --- | --- | --- |")
        for attempt in attempts:
            writer.line(
                "| {task} | {attempt} | {epoch} | {state} | {reason} | "
                "{correlation} |".format(
                    task=_md_cell(attempt["task_id"]),
                    attempt=_md_cell(attempt["attempt"]),
                    epoch=_md_cell(attempt["coordinator_epoch"]),
                    state=_md_cell(attempt["state"]),
                    reason=_md_cell(attempt["reason_code"]),
                    correlation=_md_cell(attempt["correlation_id"]),
                )
            )
    else:
        writer.line("None.")

    writer.line()
    writer.line("## Commands")
    writer.line()
    commands = _objects(value["commands"])
    if commands:
        writer.line("| Seq | Event | Type | Task | Attempt | Payload |")
        writer.line("| --- | --- | --- | --- | --- | --- |")
        for command in commands:
            identity = _object(command["identity"])
            writer.line(
                "| {sequence} | {event} | {kind} | {task} | {attempt} | "
                "{payload} |".format(
                    sequence=_md_cell(command["sequence"]),
                    event=_md_cell(command["event_id"]),
                    kind=_md_cell(command["event_type"]),
                    task=_md_cell(identity["task_id"]),
                    attempt=_md_cell(identity["attempt"]),
                    payload=_md_cell(command["payload"]),
                )
            )
    else:
        writer.line("None.")

    writer.line()
    writer.line("## Evidence")
    writer.line()
    evidence = _objects(value["evidence"])
    if evidence:
        writer.line(
            "| Digest | Type | Role | Sensitivity | Render policy | Bytes | Producer |"
        )
        writer.line("| --- | --- | --- | --- | --- | --- | --- |")
        for item in evidence:
            writer.line(
                "| {digest} | {kind} | {role} | {sensitivity} | {render} | "
                "{size} | {producer} |".format(
                    digest=_md_cell(item["digest"]),
                    kind=_md_cell(item["evidence_type"]),
                    role=_md_cell(item["role"]),
                    sensitivity=_md_cell(item["sensitivity"]),
                    render=_md_cell(item["render_policy"]),
                    size=_md_cell(item["byte_length"]),
                    producer=_md_cell(item["producer"]),
                )
            )
    else:
        writer.line("None.")

    return writer.finish()


def export_trace(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
    journal_events: Iterable[JournalEvent],
    *,
    limits: TraceLimits = DEFAULT_TRACE_LIMITS,
) -> TraceArtifacts:
    """Consume the Journal once and return both deterministic artifacts."""

    projection = build_trace_projection(
        manifest,
        snapshot,
        graph_index,
        journal_events,
        limits=limits,
    )
    json_bytes = render_trace_json(projection)
    markdown_bytes = render_trace_markdown(projection)
    return TraceArtifacts(
        projection=projection,
        json_bytes=json_bytes,
        markdown_bytes=markdown_bytes,
        json_sha256=_sha256_bytes(json_bytes),
        markdown_sha256=_sha256_bytes(markdown_bytes),
    )


def trace_json_bytes(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
    journal_events: Iterable[JournalEvent],
    *,
    limits: TraceLimits = DEFAULT_TRACE_LIMITS,
) -> bytes:
    projection = build_trace_projection(
        manifest, snapshot, graph_index, journal_events, limits=limits
    )
    return render_trace_json(projection)


def trace_markdown_bytes(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
    journal_events: Iterable[JournalEvent],
    *,
    limits: TraceLimits = DEFAULT_TRACE_LIMITS,
) -> bytes:
    projection = build_trace_projection(
        manifest, snapshot, graph_index, journal_events, limits=limits
    )
    return render_trace_markdown(projection)


def trace_json_sha256(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
    journal_events: Iterable[JournalEvent],
    *,
    limits: TraceLimits = DEFAULT_TRACE_LIMITS,
) -> str:
    return _sha256_bytes(
        trace_json_bytes(manifest, snapshot, graph_index, journal_events, limits=limits)
    )


def trace_markdown_sha256(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
    journal_events: Iterable[JournalEvent],
    *,
    limits: TraceLimits = DEFAULT_TRACE_LIMITS,
) -> str:
    return _sha256_bytes(
        trace_markdown_bytes(
            manifest, snapshot, graph_index, journal_events, limits=limits
        )
    )


def _require_typed_inputs(
    manifest: object,
    snapshot: object,
    graph_index: object,
    limits: object,
) -> None:
    if not is_execution_manifest_model(manifest):
        raise TraceExportError(
            TraceExportFault.INVALID_INPUT,
            "manifest must be a decoded ExecutionManifest model",
        )
    if type(snapshot) is not KernelSnapshot:
        raise TraceExportError(
            TraceExportFault.INVALID_INPUT,
            "snapshot must be a KernelSnapshot",
        )
    if type(graph_index) is not GraphIndex:
        raise TraceExportError(
            TraceExportFault.INVALID_INPUT,
            "graph_index must be a GraphIndex",
        )
    if type(limits) is not TraceLimits:
        raise TraceExportError(
            TraceExportFault.INVALID_INPUT,
            "limits must be TraceLimits",
        )


def _require_graph_match(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
) -> None:
    try:
        graph_index.require_match(manifest, snapshot)
    except (GraphIndexError, TypeError, ValueError) as exc:
        raise TraceExportError(
            TraceExportFault.GRAPH_MISMATCH,
            "manifest, snapshot, and GraphIndex do not describe one state",
        ) from exc


def _scan_journal(
    run_id: str,
    snapshot: KernelSnapshot,
    journal_events: Iterable[JournalEvent],
    limits: TraceLimits,
) -> tuple[list[JsonObject], list[JsonObject], JsonObject]:
    try:
        iterator = iter(journal_events)
    except TypeError as exc:
        raise TraceExportError(
            TraceExportFault.INVALID_INPUT,
            "journal_events must be an iterable of JournalEvent values",
        ) from exc

    commands: list[JsonObject] = []
    evidence_by_digest: dict[str, JsonObject] = {}
    expected_sequence = 1
    expected_previous_hash = GENESIS_HASH
    last_event_id: str | None = None
    last_event_type: str | None = None
    last_recorded_at: str | None = None
    event_count = 0

    while True:
        try:
            event = next(iterator)
        except StopIteration:
            break
        except Exception as exc:
            raise TraceExportError(
                TraceExportFault.INVALID_INPUT,
                "Journal event stream could not be read",
                sequence=event_count + 1,
            ) from exc
        event_count += 1
        if event_count > limits.max_events:
            raise TraceExportError(
                TraceExportFault.EVENT_LIMIT_EXCEEDED,
                "Journal event count exceeds the configured cap",
                sequence=event_count,
            )
        if type(event) is not JournalEvent:
            raise TraceExportError(
                TraceExportFault.INVALID_INPUT,
                "journal_events must contain only JournalEvent values",
                sequence=event_count,
            )
        _verify_event(
            event,
            run_id=run_id,
            expected_sequence=expected_sequence,
            expected_previous_hash=expected_previous_hash,
        )

        if type(event.payload) in {
            DecisionRequestPayload,
            DecisionObservedPayload,
            EffectRequestPayload,
        }:
            if len(commands) >= limits.max_commands:
                raise TraceExportError(
                    TraceExportFault.COMMAND_LIMIT_EXCEEDED,
                    "command projection count exceeds the configured cap",
                    sequence=event.sequence,
                )
            commands.append(_command_primitive(event))

        for evidence in _event_evidence(event):
            primitive = evidence.to_primitive()
            existing = evidence_by_digest.get(evidence.digest)
            if existing is not None:
                if existing != primitive:
                    raise TraceExportError(
                        TraceExportFault.INVALID_INPUT,
                        "one evidence digest has conflicting metadata",
                        sequence=event.sequence,
                    )
                continue
            if len(evidence_by_digest) >= limits.max_evidence:
                raise TraceExportError(
                    TraceExportFault.EVIDENCE_LIMIT_EXCEEDED,
                    "evidence projection count exceeds the configured cap",
                    sequence=event.sequence,
                )
            evidence_by_digest[evidence.digest] = primitive

        expected_sequence += 1
        expected_previous_hash = event.event_hash
        last_event_id = event.event_id
        last_event_type = event.event_type.value
        last_recorded_at = event.recorded_at

    if (
        event_count != snapshot.last_sequence
        or last_event_id != snapshot.last_event_id
        or expected_previous_hash != snapshot.last_event_hash
    ):
        raise TraceExportError(
            TraceExportFault.EVENT_CHAIN_MISMATCH,
            "Journal terminal head does not match the KernelSnapshot",
            sequence=event_count,
        )

    evidence = [evidence_by_digest[digest] for digest in sorted(evidence_by_digest)]
    return (
        commands,
        evidence,
        {
            "event_count": event_count,
            "last_event_type": last_event_type,
            "last_recorded_at": last_recorded_at,
        },
    )


def _verify_event(
    event: JournalEvent,
    *,
    run_id: str,
    expected_sequence: int,
    expected_previous_hash: str,
) -> None:
    if event.identity.run_id != run_id:
        raise TraceExportError(
            TraceExportFault.EVENT_CHAIN_MISMATCH,
            "Journal event belongs to another run",
            sequence=event.sequence,
        )
    if event.sequence != expected_sequence:
        raise TraceExportError(
            TraceExportFault.EVENT_CHAIN_MISMATCH,
            "Journal sequence is not contiguous from genesis",
            sequence=event.sequence,
        )
    if event.previous_event_hash != expected_previous_hash:
        raise TraceExportError(
            TraceExportFault.EVENT_CHAIN_MISMATCH,
            "Journal previous hash does not extend the verified head",
            sequence=event.sequence,
        )
    try:
        payload_matches = event.payload_hash == event.computed_payload_hash()
        event_matches = event.event_hash == event.computed_event_hash()
    except (TypeError, ValueError, AttributeError) as exc:
        raise TraceExportError(
            TraceExportFault.EVENT_CHAIN_MISMATCH,
            "Journal event cannot be hashed canonically",
            sequence=event.sequence,
        ) from exc
    if not payload_matches or not event_matches:
        raise TraceExportError(
            TraceExportFault.EVENT_CHAIN_MISMATCH,
            "Journal event hash does not match its canonical content",
            sequence=event.sequence,
        )


def _command_primitive(event: JournalEvent) -> JsonObject:
    return {
        "actor_id": event.actor_id,
        "actor_type": event.actor_type.value,
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "identity": event.identity.to_primitive(),
        "payload": event.payload.to_primitive(),
        "payload_hash": event.payload_hash,
        "reason_code": (None if event.reason_code is None else event.reason_code.value),
        "recorded_at": event.recorded_at,
        "sequence": event.sequence,
    }


def _event_evidence(event: JournalEvent) -> tuple[EvidenceRef, ...]:
    payload = event.payload
    if type(payload) is TransitionPayload:
        return payload.evidence
    if type(payload) is EffectObservationPayload:
        return payload.receipt.evidence
    if type(payload) is RecoveryPayload:
        return payload.evidence
    return ()


def _snapshot_primitive(
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
) -> JsonObject:
    task_states = {task.task_id: task for task in snapshot.tasks}
    positions = {
        task_id: position
        for position, task_id in enumerate(graph_index.topological_order)
    }
    return {
        "attempts": [
            {
                "attempt": attempt.attempt,
                "coordinator_epoch": attempt.coordinator_epoch,
                "correlation_id": attempt.correlation_id,
                "reason_code": (
                    None if attempt.reason_code is None else attempt.reason_code.value
                ),
                "state": attempt.state.value,
                "task_id": attempt.task_id,
            }
            for attempt in sorted(
                snapshot.attempts,
                key=lambda item: (
                    positions[item.task_id],
                    item.task_id,
                    item.attempt,
                ),
            )
        ],
        "coordinator_epoch": snapshot.coordinator_epoch,
        "last_event_hash": snapshot.last_event_hash,
        "last_event_id": snapshot.last_event_id,
        "last_sequence": snapshot.last_sequence,
        "phase": snapshot.phase.value,
        "projection_version": 1,
        "run_id": snapshot.run_id,
        "run_reason_code": (
            None if snapshot.run_reason_code is None else snapshot.run_reason_code.value
        ),
        "status": snapshot.status.value,
        "tasks": [
            {
                "reason_code": (
                    None
                    if task_states[task_id].reason_code is None
                    else task_states[task_id].reason_code.value
                ),
                "state": task_states[task_id].state.value,
                "task_id": task_id,
            }
            for task_id in graph_index.topological_order
        ],
    }


def _manifest_source(manifest: ExecutionManifestModel) -> JsonObject:
    if type(manifest) is ExecutionManifestV2:
        return {
            "imported_at": manifest.imported_at,
            "kind": "trellis",
            "trellis_graph_digest": manifest.trellis_graph_digest,
            "trellis_parent_task_id": manifest.trellis_parent_task_id,
            "trellis_revision": manifest.trellis_revision,
        }
    return {
        "imported_at": None,
        "kind": "legacy",
        "trellis_graph_digest": None,
        "trellis_parent_task_id": None,
        "trellis_revision": None,
    }


def _requirement_projections(
    manifest: ExecutionManifestModel,
    graph_index: GraphIndex,
) -> list[JsonObject]:
    positions = {
        task_id: position
        for position, task_id in enumerate(graph_index.topological_order)
    }
    result: list[JsonObject] = []
    for requirement in sorted(manifest.requirements, key=lambda item: item.id):
        task_ids = sorted(
            (
                task.id
                for task in manifest.tasks
                if requirement.id in task.requirement_ids
            ),
            key=lambda task_id: (positions[task_id], task_id),
        )
        result.append(
            {
                "decision_ref": getattr(requirement, "decision_ref", None),
                "id": requirement.id,
                "status": requirement.status.value,
                "task_ids": task_ids,
                "text": requirement.text,
            }
        )
    return result


def _task_projections(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
) -> list[JsonObject]:
    tasks = {task.id: task for task in manifest.tasks}
    states = {task.task_id: task for task in snapshot.tasks}
    trellis_task_ids = (
        {item.task_id: item.trellis_task_id for item in manifest.task_id_mapping}
        if type(manifest) is ExecutionManifestV2
        else {}
    )
    positions = {
        task_id: position
        for position, task_id in enumerate(graph_index.topological_order)
    }
    nodes = sorted(
        graph_index.nodes,
        key=lambda node: (node.topological_position, node.task_id),
    )
    result: list[JsonObject] = []
    for node in nodes:
        task = tasks[node.task_id]
        state = states[node.task_id]
        result.append(
            {
                "acceptance_criteria": list(task.acceptance_criteria),
                "allowed_auxiliary_paths": list(task.allowed_auxiliary_paths),
                "approved_document_digests": list(
                    getattr(task, "approved_document_digests", ())
                ),
                "definition_digest": _sha256_value(task.to_primitive()),
                "delivery": _delivery_primitive(task),
                "depends_on": sorted(
                    task.depends_on,
                    key=lambda task_id: (positions[task_id], task_id),
                ),
                "documentation": list(task.documentation),
                "id": task.id,
                "instruction_context_digest": getattr(
                    task, "instruction_context_digest", None
                ),
                "may_change_contracts": task.may_change_contracts,
                "owned_paths": list(task.owned_paths),
                "reason_code": (
                    None if state.reason_code is None else state.reason_code.value
                ),
                "regression_commands": _regression_primitives(task),
                "requirement_ids": list(task.requirement_ids),
                "risk": task.risk.value,
                "rollback": task.rollback,
                "state": state.state.value,
                "task_packet_template_digest": getattr(
                    task, "task_packet_template_digest", None
                ),
                "title": task.title,
                "topological_position": node.topological_position,
                "trellis_task_id": trellis_task_ids.get(task.id),
                "wave": node.wave,
            }
        )
    return result


def _delivery_primitive(task: Task | ManifestTask) -> JsonObject:
    if type(task) is Task:
        return {
            "agent_owner": task.agent_owner,
            "branch": task.branch,
            "issue_id": task.issue_id,
            "pr_id": task.pr_id,
            "squash_commit": task.squash_commit,
        }
    return {
        "agent_owner": None,
        "branch": None,
        "issue_id": None,
        "pr_id": None,
        "squash_commit": None,
    }


def _regression_primitives(task: Task | ManifestTask) -> list[object]:
    if type(task) is Task:
        return list(task.regression_commands)
    return [command.to_primitive() for command in task.regression_commands]


def _attempt_projections(
    snapshot: KernelSnapshot,
    graph_index: GraphIndex,
) -> list[JsonObject]:
    positions = {
        task_id: position
        for position, task_id in enumerate(graph_index.topological_order)
    }
    return [
        {
            "attempt": attempt.attempt,
            "coordinator_epoch": attempt.coordinator_epoch,
            "correlation_id": attempt.correlation_id,
            "reason_code": (
                None if attempt.reason_code is None else attempt.reason_code.value
            ),
            "state": attempt.state.value,
            "task_id": attempt.task_id,
            "topological_position": positions[attempt.task_id],
        }
        for attempt in sorted(
            snapshot.attempts,
            key=lambda item: (
                positions[item.task_id],
                item.task_id,
                item.attempt,
            ),
        )
    ]


def _sha256_value(value: object) -> str:
    return _sha256_bytes(canonical_json_bytes(value))


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _require_projection(projection: object) -> None:
    if type(projection) is not TraceProjection:
        raise TraceExportError(
            TraceExportFault.INVALID_INPUT,
            "projection must be a TraceProjection",
        )


def _render_cap(
    projection: TraceProjection,
    value: int | None,
) -> int:
    if value is None:
        return projection.max_output_bytes
    if type(value) is not int or value <= 0:
        raise TraceExportError(
            TraceExportFault.INVALID_INPUT,
            "max_output_bytes must be a positive integer",
        )
    return value


def _require_output_size(raw: bytes, cap: int, label: str) -> None:
    if len(raw) > cap:
        raise TraceExportError(
            TraceExportFault.OUTPUT_LIMIT_EXCEEDED,
            f"{label} exceeds the configured output byte cap",
        )


class _MarkdownWriter:
    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._size = 0
        self._parts: list[bytes] = []

    def line(self, value: str = "") -> None:
        raw = (value + "\n").encode("utf-8", errors="strict")
        if self._size + len(raw) > self._cap:
            raise TraceExportError(
                TraceExportFault.OUTPUT_LIMIT_EXCEEDED,
                "Markdown exceeds the configured output byte cap",
            )
        self._parts.append(raw)
        self._size += len(raw)

    def finish(self) -> bytes:
        return b"".join(self._parts)


def _md_cell(value: object) -> str:
    if value is None or value == "":
        return "-"
    if type(value) in (dict, list):
        value = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    text = html.escape(str(value), quote=True)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("\r\n", "<br>")
        .replace("\r", "<br>")
        .replace("\n", "<br>")
    )


def _md_list(value: object) -> str:
    if type(value) is not list:
        raise AssertionError("trace list projection is malformed")
    if not value:
        return "-"
    return "<br>".join(_md_cell(item) for item in value)


def _object(value: object) -> JsonObject:
    if type(value) is not dict:
        raise AssertionError("trace object projection is malformed")
    return value


def _objects(value: object) -> list[JsonObject]:
    if type(value) is not list or not all(type(item) is dict for item in value):
        raise AssertionError("trace object-list projection is malformed")
    return value


__all__ = [
    "DEFAULT_TRACE_LIMITS",
    "TRACE_SCHEMA_VERSION",
    "TraceArtifacts",
    "TraceExport",
    "TraceExportError",
    "TraceExportFault",
    "TraceExportReason",
    "TraceFaultCode",
    "TraceLimits",
    "TraceProjection",
    "build_trace_projection",
    "compile_trace",
    "export_trace",
    "render_trace_json",
    "render_trace_markdown",
    "trace_json_bytes",
    "trace_json_sha256",
    "trace_markdown_bytes",
    "trace_markdown_sha256",
]
