"""Crash-resumable Gate-B Journal bootstrap for an approved manifest v2."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

from wish_builder.contracts.manifest_v2 import ExecutionManifestV2
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import (
    ActorIdentity,
    ActorType,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionCommand,
    DecisionObservedPayload,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.contracts.runtime_decoder import decode_journal_event_bytes
from wish_builder.kernel.gates import evaluate_decision
from wish_builder.services.journal import (
    AppendStatus,
    DurableJournal,
    GENESIS_HEAD,
    JournalEventDraft,
    JournalHead,
)


GATE_B_ARTIFACT_NONCE_PREFIX = "gate-b-artifact-"


def gate_b_artifact_nonce(artifact_hash: str) -> str:
    """Encode the approved Gate-B artifact digest into a decision nonce."""

    if type(artifact_hash) is not str or HASH_RE.fullmatch(artifact_hash) is None:
        raise ValueError("artifact_hash must be a full sha256 reference")
    return GATE_B_ARTIFACT_NONCE_PREFIX + artifact_hash.removeprefix("sha256:")


def gate_b_artifact_hash_from_nonce(nonce: str) -> str | None:
    if type(nonce) is not str or not nonce.startswith(GATE_B_ARTIFACT_NONCE_PREFIX):
        return None
    artifact_hash = "sha256:" + nonce.removeprefix(GATE_B_ARTIFACT_NONCE_PREFIX)
    return artifact_hash if HASH_RE.fullmatch(artifact_hash) is not None else None


def graph_projection_bytes(manifest: ExecutionManifestV2) -> bytes:
    """Return the canonical material graph bytes covered by the manifest digest."""

    if type(manifest) is not ExecutionManifestV2:
        raise TypeError("manifest must be an ExecutionManifestV2")
    projection = {
        "graph_projection_version": manifest.graph_projection_version,
        "requirements": [item.to_primitive() for item in manifest.requirements],
        "task_id_mapping": {
            item.trellis_task_id: item.task_id for item in manifest.task_id_mapping
        },
        "tasks": [item.to_primitive() for item in manifest.tasks],
        "trellis_parent_task_id": manifest.trellis_parent_task_id,
    }
    payload = canonical_json_bytes(projection)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if digest != manifest.trellis_graph_digest:
        raise ValueError("manifest material graph digest does not match its projection")
    return payload


class GateBBootstrapReason(StrEnum):
    NONE = "none"
    ALREADY_ADMITTED = "already_admitted"
    JOURNAL_PREFIX_INVALID = "journal_prefix_invalid"
    JOURNAL_CONFLICT = "journal_conflict"
    PERSISTENCE_FAILED = "persistence_failed"
    POSTCONDITION_FAILED = "postcondition_failed"


@dataclass(frozen=True, slots=True)
class GateBBootstrapMaterial:
    manifest: ExecutionManifestV2
    workspace_hash: str
    gate_b_artifact_hash: str
    gate_b_artifact_byte_length: int
    trellis_snapshot_hash: str
    trellis_snapshot_byte_length: int
    trellis_observed_at: str
    coordinator: ActorIdentity
    approver: ActorIdentity
    requested_at: str
    decided_at: str

    def __post_init__(self) -> None:
        if type(self.manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        for field_name in (
            "workspace_hash",
            "gate_b_artifact_hash",
            "trellis_snapshot_hash",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or HASH_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a full sha256 reference")
        for field_name in (
            "gate_b_artifact_byte_length",
            "trellis_snapshot_byte_length",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if (
            type(self.coordinator) is not ActorIdentity
            or self.coordinator.actor_type is not ActorType.COORDINATOR
        ):
            raise ValueError("coordinator must be a coordinator ActorIdentity")
        if (
            type(self.approver) is not ActorIdentity
            or self.approver.actor_type is not ActorType.HUMAN
        ):
            raise ValueError("approver must be a human ActorIdentity")
        for field_name in ("trellis_observed_at", "requested_at", "decided_at"):
            if type(getattr(self, field_name)) is not str:
                raise TypeError(f"{field_name} must be a string")
        # These constructors apply the shared strict timestamp and token contracts.
        gate_b_artifact_nonce(self.gate_b_artifact_hash)
        _new_request(self, sequence=6)


@dataclass(frozen=True, slots=True)
class GateBBootstrapResult:
    admitted: bool
    reason: GateBBootstrapReason
    events: tuple[JournalEvent, ...]
    appended_count: int

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise TypeError("admitted must be a bool")
        if type(self.reason) is not GateBBootstrapReason:
            raise TypeError("reason must be a GateBBootstrapReason")
        if type(self.events) is not tuple or not all(
            type(event) is JournalEvent for event in self.events
        ):
            raise TypeError("events must be a tuple of JournalEvent values")
        if type(self.appended_count) is not int or self.appended_count < 0:
            raise ValueError("appended_count must be a non-negative integer")
        if self.admitted != (
            self.reason in {GateBBootstrapReason.NONE, GateBBootstrapReason.ALREADY_ADMITTED}
        ):
            raise ValueError("bootstrap admission does not match its reason")


def _event_ids(run_id: str) -> tuple[str, ...]:
    run_key = hashlib.sha256(run_id.encode("utf-8", errors="strict")).hexdigest()[:16]
    return tuple(f"EVENT-GATEB-{run_key.upper()}-{index:02d}" for index in range(1, 9))


def _producer(identity: ExecutionIdentity, external_object_id: str) -> EvidenceProducer:
    return EvidenceProducer(identity, external_object_id=external_object_id)


def _evidence(
    *,
    digest: str,
    byte_length: int,
    producer: EvidenceProducer,
    created_at: str,
    structured_subject_hash: str,
    render_policy: EvidenceRenderPolicy = EvidenceRenderPolicy.METADATA_ONLY,
) -> EvidenceRef:
    return EvidenceRef(
        schema_version=1,
        digest=digest,
        byte_length=byte_length,
        evidence_type=EvidenceType.CONTRACT,
        producer=producer,
        created_at=created_at,
        sensitivity=EvidenceSensitivity.INTERNAL,
        render_policy=render_policy,
        role=EvidenceRole.REQUIRED,
        structured_subject_hash=structured_subject_hash,
    )


def _graph_import_payload(material: GateBBootstrapMaterial) -> TransitionPayload:
    identity = ExecutionIdentity(material.manifest.run_id, 1)
    graph_bytes = graph_projection_bytes(material.manifest)
    return TransitionPayload(
        TransitionSubject.RUN,
        RuntimeState.TRELLIS_PREPARATION,
        RuntimeState.GATE_B_PENDING,
        (
            _evidence(
                digest=material.manifest.trellis_graph_digest,
                byte_length=len(graph_bytes),
                producer=_producer(identity, "trellis-material-graph"),
                created_at=material.trellis_observed_at,
                structured_subject_hash=material.manifest.trellis_graph_digest,
            ),
            _evidence(
                digest=material.trellis_snapshot_hash,
                byte_length=material.trellis_snapshot_byte_length,
                producer=_producer(identity, "trellis-stable-snapshot"),
                created_at=material.trellis_observed_at,
                structured_subject_hash=material.manifest.trellis_graph_digest,
            ),
        ),
    )


def _freeze_payload(material: GateBBootstrapMaterial) -> TransitionPayload:
    manifest = material.manifest
    identity = ExecutionIdentity(manifest.run_id, 1)
    manifest_bytes = manifest.canonical_json_bytes()
    graph_bytes = graph_projection_bytes(manifest)
    return TransitionPayload(
        TransitionSubject.RUN,
        RuntimeState.GATE_B_PENDING,
        RuntimeState.EXECUTING,
        (
            _evidence(
                digest=material.gate_b_artifact_hash,
                byte_length=material.gate_b_artifact_byte_length,
                producer=_producer(identity, "gate-b-approved-artifact"),
                created_at=material.decided_at,
                structured_subject_hash=material.gate_b_artifact_hash,
                render_policy=EvidenceRenderPolicy.TEXT,
            ),
            _evidence(
                digest=manifest.trellis_graph_digest,
                byte_length=len(graph_bytes),
                producer=_producer(identity, "trellis-material-graph"),
                created_at=material.trellis_observed_at,
                structured_subject_hash=manifest.trellis_graph_digest,
            ),
            _evidence(
                digest=manifest.canonical_sha256(),
                byte_length=len(manifest_bytes),
                producer=_producer(identity, "execution-manifest-v2"),
                created_at=material.decided_at,
                structured_subject_hash=manifest.canonical_sha256(),
            ),
        ),
    )


def _new_request(material: GateBBootstrapMaterial, *, sequence: int) -> DecisionRequest:
    run_key = hashlib.sha256(
        material.manifest.run_id.encode("utf-8", errors="strict")
    ).hexdigest()[:16].upper()
    return DecisionRequest(
        CommandIdentity(
            1,
            f"COMMAND-GATEB-{run_key}",
            f"REQUEST-GATEB-{run_key}",
            CommandKind.DECIDE,
            sequence,
            gate_b_artifact_nonce(material.gate_b_artifact_hash),
            material.coordinator,
            SourceChannel.COORDINATOR,
            material.requested_at,
        ),
        DecisionType.GATE_B,
        material.manifest.canonical_sha256(),
        material.workspace_hash,
        material.approver.actor_id,
        (DecisionChoice.APPROVE, DecisionChoice.REJECT),
    )


def _transition(
    event_type: JournalEventType,
    from_state: RuntimeState,
    to_state: RuntimeState,
) -> TransitionPayload:
    return TransitionPayload(TransitionSubject.RUN, from_state, to_state)


def _draft_for_index(
    material: GateBBootstrapMaterial,
    events: tuple[JournalEvent, ...],
    index: int,
) -> JournalEventDraft:
    manifest = material.manifest
    identity = ExecutionIdentity(manifest.run_id, 1)
    event_id = _event_ids(manifest.run_id)[index]
    transition_specs = {
        0: (
            JournalEventType.RUN_INITIALIZED,
            _transition(
                JournalEventType.RUN_INITIALIZED,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
        ),
        1: (
            JournalEventType.PREFLIGHT_COMPLETED,
            _transition(
                JournalEventType.PREFLIGHT_COMPLETED,
                RuntimeState.PREFLIGHT,
                RuntimeState.DISCOVERY,
            ),
        ),
        2: (
            JournalEventType.DISCOVERY_COMPLETED,
            _transition(
                JournalEventType.DISCOVERY_COMPLETED,
                RuntimeState.DISCOVERY,
                RuntimeState.GATE_A_PENDING,
            ),
        ),
        3: (
            JournalEventType.GATE_APPROVED,
            _transition(
                JournalEventType.GATE_APPROVED,
                RuntimeState.GATE_A_PENDING,
                RuntimeState.TRELLIS_PREPARATION,
            ),
        ),
    }
    if index in transition_specs:
        event_type, payload = transition_specs[index]
        return JournalEventDraft(
            event_id,
            event_type,
            identity,
            ActorType.SYSTEM,
            "gate-b-bootstrap",
            payload,
        )
    if index == 4:
        return JournalEventDraft(
            event_id,
            JournalEventType.TRELLIS_GRAPH_IMPORTED,
            identity,
            ActorType.SYSTEM,
            "gate-b-bootstrap",
            _graph_import_payload(material),
        )
    if index == 5:
        request = _new_request(material, sequence=len(events) + 1)
        return JournalEventDraft(
            event_id,
            JournalEventType.DECISION_REQUESTED,
            identity,
            ActorType.COORDINATOR,
            material.coordinator.actor_id,
            DecisionRequestPayload(request),
        )
    if index == 6:
        request_event = events[5]
        if (
            request_event.event_type is not JournalEventType.DECISION_REQUESTED
            or type(request_event.payload) is not DecisionRequestPayload
        ):
            raise ValueError("Gate B decision request is missing from the prefix")
        request = request_event.payload.request
        command = DecisionCommand(
            f"DECISION-{request.command.request_id}",
            request,
            DecisionChoice.APPROVE,
            material.approver,
            SourceChannel.DIRECT_CLI,
            material.decided_at,
        )
        evaluation = evaluate_decision(
            request,
            command,
            current_sequence=request_event.sequence,
            current_workspace_hash=material.workspace_hash,
        )
        if not evaluation.accepted or evaluation.observation is None:
            raise ValueError(f"Gate B decision was not admissible: {evaluation.reason.value}")
        return JournalEventDraft(
            event_id,
            JournalEventType.DECISION_OBSERVED,
            identity,
            ActorType.HUMAN,
            material.approver.actor_id,
            DecisionObservedPayload(evaluation.observation),
        )
    if index == 7:
        return JournalEventDraft(
            event_id,
            JournalEventType.TASK_GRAPH_FROZEN,
            identity,
            ActorType.SYSTEM,
            "gate-b-bootstrap",
            _freeze_payload(material),
        )
    raise ValueError("unsupported Gate B bootstrap event index")


def _evidence_matches(payload: TransitionPayload, digest: str) -> bool:
    return any(
        evidence.digest == digest
        and evidence.byte_length > 0
        and evidence.evidence_type is EvidenceType.CONTRACT
        and evidence.role is EvidenceRole.REQUIRED
        and evidence.structured_subject_hash == digest
        for evidence in payload.evidence
    )


def _prefix_event_matches(
    material: GateBBootstrapMaterial,
    events: tuple[JournalEvent, ...],
    index: int,
) -> bool:
    event = events[index]
    if event.event_id != _event_ids(material.manifest.run_id)[index]:
        return False
    if event.identity != ExecutionIdentity(material.manifest.run_id, 1):
        return False
    if index <= 3:
        expected = _draft_for_index(material, events[:index], index)
        return (
            event.event_type is expected.event_type
            and event.payload == expected.payload
            and event.actor_type is expected.actor_type
            and event.actor_id == expected.actor_id
        )
    if index == 4:
        payload = event.payload
        return (
            event.event_type is JournalEventType.TRELLIS_GRAPH_IMPORTED
            and type(payload) is TransitionPayload
            and payload.subject is TransitionSubject.RUN
            and payload.from_state is RuntimeState.TRELLIS_PREPARATION
            and payload.to_state is RuntimeState.GATE_B_PENDING
            and _evidence_matches(payload, material.manifest.trellis_graph_digest)
        )
    if index == 5:
        payload = event.payload
        if (
            event.event_type is not JournalEventType.DECISION_REQUESTED
            or type(payload) is not DecisionRequestPayload
        ):
            return False
        request = payload.request
        return (
            request.decision_type is DecisionType.GATE_B
            and request.candidate_hash == material.manifest.canonical_sha256()
            and request.workspace_hash == material.workspace_hash
            and request.command.expected_sequence == event.sequence
            and request.command.request_nonce
            == gate_b_artifact_nonce(material.gate_b_artifact_hash)
            and request.expected_actor_id == material.approver.actor_id
            and request.options == (DecisionChoice.APPROVE, DecisionChoice.REJECT)
            and request.command.source_channel is SourceChannel.COORDINATOR
            and request.command.actor.actor_type is ActorType.COORDINATOR
            and event.actor_type is ActorType.COORDINATOR
            and event.actor_id == request.command.actor.actor_id
        )
    if index == 6:
        payload = event.payload
        if (
            event.event_type is not JournalEventType.DECISION_OBSERVED
            or type(payload) is not DecisionObservedPayload
        ):
            return False
        decision = payload.observation.decision
        return (
            decision.request == events[5].payload.request
            and decision.choice is DecisionChoice.APPROVE
            and decision.source_channel is SourceChannel.DIRECT_CLI
            and decision.actor.actor_type is ActorType.HUMAN
            and decision.actor.actor_id == material.approver.actor_id
            and payload.observation.event_sequence == event.sequence
            and event.actor_type is ActorType.HUMAN
            and event.actor_id == decision.actor.actor_id
        )
    payload = event.payload
    return (
        event.event_type is JournalEventType.TASK_GRAPH_FROZEN
        and type(payload) is TransitionPayload
        and payload.subject is TransitionSubject.RUN
        and payload.from_state is RuntimeState.GATE_B_PENDING
        and payload.to_state is RuntimeState.EXECUTING
        and _evidence_matches(payload, material.gate_b_artifact_hash)
        and _evidence_matches(payload, material.manifest.trellis_graph_digest)
        and _evidence_matches(payload, material.manifest.canonical_sha256())
    )


def _journal_chain_valid(events: tuple[JournalEvent, ...]) -> bool:
    head = GENESIS_HEAD
    for event in events:
        decoded = decode_journal_event_bytes(event.canonical_json_bytes())
        if (
            not decoded.ok
            or decoded.value != event
            or event.sequence != head.sequence + 1
            or event.previous_event_hash != head.event_hash
        ):
            return False
        head = JournalHead(event.sequence, event.event_hash)
    return True


def bootstrap_gate_b(
    material: GateBBootstrapMaterial,
    events: tuple[JournalEvent, ...],
    journal: DurableJournal,
) -> GateBBootstrapResult:
    """Append or resume the one canonical Gate-B bootstrap prefix."""

    if type(material) is not GateBBootstrapMaterial:
        raise TypeError("material must be a GateBBootstrapMaterial")
    if type(events) is not tuple or not all(type(event) is JournalEvent for event in events):
        raise TypeError("events must be a tuple of JournalEvent values")
    if type(journal) is not DurableJournal:
        raise TypeError("journal must be a DurableJournal")

    from wish_builder.services.execution_admission import admit_execution_snapshot

    prior_admission = admit_execution_snapshot(
        material.manifest,
        events,
        workspace_hash=material.workspace_hash,
    )
    prefix_length = min(len(events), 8)
    if not _journal_chain_valid(events) or any(
        not _prefix_event_matches(material, events, index)
        for index in range(prefix_length)
    ) or (
        len(events) > 8 and not prior_admission.admitted
    ):
        return GateBBootstrapResult(
            False,
            GateBBootstrapReason.JOURNAL_PREFIX_INVALID,
            events,
            0,
        )
    if prior_admission.admitted:
        return GateBBootstrapResult(
            True,
            GateBBootstrapReason.ALREADY_ADMITTED,
            events,
            0,
        )

    appended_count = 0
    current_events = list(events)
    head = (
        GENESIS_HEAD
        if not current_events
        else JournalHead(current_events[-1].sequence, current_events[-1].event_hash)
    )
    while len(current_events) < 8:
        draft = _draft_for_index(material, tuple(current_events), len(current_events))
        result = journal.append_draft(draft, expected_head=head)
        if result.status not in {AppendStatus.COMMITTED, AppendStatus.IDEMPOTENT}:
            reason = (
                GateBBootstrapReason.JOURNAL_CONFLICT
                if result.status is AppendStatus.CONFLICT
                else GateBBootstrapReason.PERSISTENCE_FAILED
            )
            return GateBBootstrapResult(
                False,
                reason,
                tuple(current_events),
                appended_count,
            )
        if result.event is None:
            return GateBBootstrapResult(
                False,
                GateBBootstrapReason.PERSISTENCE_FAILED,
                tuple(current_events),
                appended_count,
            )
        current_events.append(result.event)
        head = result.head
        if result.status is AppendStatus.COMMITTED:
            appended_count += 1

    completed = tuple(current_events)
    admission = admit_execution_snapshot(
        material.manifest,
        completed,
        workspace_hash=material.workspace_hash,
    )
    if not admission.admitted:
        return GateBBootstrapResult(
            False,
            GateBBootstrapReason.POSTCONDITION_FAILED,
            completed,
            appended_count,
        )
    return GateBBootstrapResult(
        True,
        GateBBootstrapReason.NONE,
        completed,
        appended_count,
    )


__all__ = [
    "GATE_B_ARTIFACT_NONCE_PREFIX",
    "GateBBootstrapMaterial",
    "GateBBootstrapReason",
    "GateBBootstrapResult",
    "bootstrap_gate_b",
    "gate_b_artifact_hash_from_nonce",
    "gate_b_artifact_nonce",
    "graph_projection_bytes",
]
