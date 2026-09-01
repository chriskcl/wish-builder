"""Gate-B and frozen-snapshot admission for foreground execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from wish_builder.contracts.manifest_v2 import ExecutionManifestV2
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import (
    ActorType,
    DecisionChoice,
    DecisionObservedPayload,
    DecisionRequestPayload,
    DecisionType,
    EvidenceRole,
    EvidenceType,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.contracts.runtime_decoder import decode_journal_event_bytes
from wish_builder.services.gate_b_bootstrap import gate_b_artifact_hash_from_nonce
from wish_builder.services.journal import GENESIS_HEAD


class ExecutionAdmissionReason(StrEnum):
    NONE = "none"
    JOURNAL_EMPTY = "journal_empty"
    JOURNAL_CHAIN_INVALID = "journal_chain_invalid"
    JOURNAL_RUN_MISMATCH = "journal_run_mismatch"
    GATE_B_REQUEST_MISSING = "gate_b_request_missing"
    GATE_B_DECISION_MISSING = "gate_b_decision_missing"
    GATE_B_DECISION_PENDING = "gate_b_decision_pending"
    GATE_B_NOT_APPROVED = "gate_b_not_approved"
    GATE_B_LINK_INVALID = "gate_b_link_invalid"
    GATE_B_ARTIFACT_INVALID = "gate_b_artifact_invalid"
    MANIFEST_DIGEST_MISMATCH = "manifest_digest_mismatch"
    WORKSPACE_DRIFT = "workspace_drift"
    LIFECYCLE_ORDER_INVALID = "lifecycle_order_invalid"
    TRELLIS_GRAPH_IMPORT_MISSING = "trellis_graph_import_missing"
    TRELLIS_GRAPH_EVIDENCE_INVALID = "trellis_graph_evidence_invalid"
    TRELLIS_GRAPH_CHANGED = "trellis_graph_changed"
    TASK_GRAPH_NOT_FROZEN = "task_graph_not_frozen"
    TASK_GRAPH_FREEZE_EVIDENCE_INVALID = "task_graph_freeze_evidence_invalid"


@dataclass(frozen=True, slots=True)
class ExecutionAdmissionResult:
    admitted: bool
    reason: ExecutionAdmissionReason
    request_event: JournalEvent | None = None
    decision_event: JournalEvent | None = None
    frozen_event: JournalEvent | None = None

    def __post_init__(self) -> None:
        if type(self.admitted) is not bool:
            raise TypeError("admitted must be a bool")
        if type(self.reason) is not ExecutionAdmissionReason:
            raise TypeError("reason must be an ExecutionAdmissionReason")
        for field_name in ("request_event", "decision_event", "frozen_event"):
            value = getattr(self, field_name)
            if value is not None and type(value) is not JournalEvent:
                raise TypeError(f"{field_name} must be a JournalEvent or null")
        if self.admitted:
            if self.reason is not ExecutionAdmissionReason.NONE or any(
                value is None
                for value in (
                    self.request_event,
                    self.decision_event,
                    self.frozen_event,
                )
            ):
                raise ValueError("admitted execution requires complete Gate B evidence")
        elif self.reason is ExecutionAdmissionReason.NONE:
            raise ValueError("rejected execution requires a reason")


def _rejected(reason: ExecutionAdmissionReason) -> ExecutionAdmissionResult:
    return ExecutionAdmissionResult(False, reason)


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
        head = type(head)(event.sequence, event.event_hash)
    return True


_RUN_TRANSITIONS = {
    JournalEventType.RUN_INITIALIZED: (RuntimeState.NONE, RuntimeState.PREFLIGHT),
    JournalEventType.PREFLIGHT_COMPLETED: (
        RuntimeState.PREFLIGHT,
        RuntimeState.DISCOVERY,
    ),
    JournalEventType.DISCOVERY_COMPLETED: (
        RuntimeState.DISCOVERY,
        RuntimeState.GATE_A_PENDING,
    ),
    JournalEventType.GATE_APPROVED: (
        RuntimeState.GATE_A_PENDING,
        RuntimeState.TRELLIS_PREPARATION,
    ),
    JournalEventType.TRELLIS_GRAPH_IMPORTED: (
        RuntimeState.TRELLIS_PREPARATION,
        RuntimeState.GATE_B_PENDING,
    ),
    JournalEventType.TASK_GRAPH_FROZEN: (
        RuntimeState.GATE_B_PENDING,
        RuntimeState.EXECUTING,
    ),
}


def _transition_matches(event: JournalEvent) -> bool:
    states = _RUN_TRANSITIONS.get(event.event_type)
    payload = event.payload
    return (
        states is not None
        and type(payload) is TransitionPayload
        and payload.subject is TransitionSubject.RUN
        and (payload.from_state, payload.to_state) == states
        and event.identity.task_id is None
        and event.identity.attempt is None
        and event.identity.correlation_id is None
    )


def _single_event(
    events: tuple[JournalEvent, ...],
    event_type: JournalEventType,
) -> JournalEvent | None:
    matches = tuple(event for event in events if event.event_type is event_type)
    return matches[0] if len(matches) == 1 else None


def _required_contract_evidence(event: JournalEvent, digest: str) -> bool:
    payload = event.payload
    if type(payload) is not TransitionPayload:
        return False
    return any(
        evidence.digest == digest
        and evidence.byte_length > 0
        and evidence.evidence_type is EvidenceType.CONTRACT
        and evidence.role is EvidenceRole.REQUIRED
        and evidence.structured_subject_hash == digest
        and evidence.producer.identity.run_id == event.identity.run_id
        for evidence in payload.evidence
    )


def admit_execution_snapshot(
    manifest: ExecutionManifestV2,
    events: tuple[JournalEvent, ...],
    *,
    workspace_hash: str,
) -> ExecutionAdmissionResult:
    """Admit only the latest direct-CLI Gate-B approval and its frozen graph."""

    if type(manifest) is not ExecutionManifestV2:
        raise TypeError("manifest must be an ExecutionManifestV2")
    if type(events) is not tuple or not all(type(event) is JournalEvent for event in events):
        raise TypeError("events must be a tuple of JournalEvent values")
    if type(workspace_hash) is not str or HASH_RE.fullmatch(workspace_hash) is None:
        raise ValueError("workspace_hash must be a full sha256 reference")
    if not events:
        return _rejected(ExecutionAdmissionReason.JOURNAL_EMPTY)
    if not _journal_chain_valid(events):
        return _rejected(ExecutionAdmissionReason.JOURNAL_CHAIN_INVALID)
    if any(event.identity.run_id != manifest.run_id for event in events):
        return _rejected(ExecutionAdmissionReason.JOURNAL_RUN_MISMATCH)

    requests = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.DECISION_REQUESTED
        and type(event.payload) is DecisionRequestPayload
        and event.payload.request.decision_type is DecisionType.GATE_B
    )
    if not requests:
        return _rejected(ExecutionAdmissionReason.GATE_B_REQUEST_MISSING)
    decisions = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.DECISION_OBSERVED
        and type(event.payload) is DecisionObservedPayload
        and event.payload.observation.decision.request.decision_type
        is DecisionType.GATE_B
    )
    if not decisions:
        return _rejected(ExecutionAdmissionReason.GATE_B_DECISION_MISSING)

    if len(requests) != 1 or len(decisions) != 1:
        if any(event.sequence > decisions[-1].sequence for event in requests):
            return _rejected(ExecutionAdmissionReason.GATE_B_DECISION_PENDING)
        return _rejected(ExecutionAdmissionReason.GATE_B_LINK_INVALID)

    decision_event = decisions[0]
    if any(event.sequence > decision_event.sequence for event in requests):
        return _rejected(ExecutionAdmissionReason.GATE_B_DECISION_PENDING)
    observation = decision_event.payload.observation
    decision = observation.decision
    request = decision.request
    if decision.choice is not DecisionChoice.APPROVE:
        return _rejected(ExecutionAdmissionReason.GATE_B_NOT_APPROVED)
    if request.candidate_hash != manifest.canonical_sha256():
        return _rejected(ExecutionAdmissionReason.MANIFEST_DIGEST_MISMATCH)
    if request.workspace_hash != workspace_hash:
        return _rejected(ExecutionAdmissionReason.WORKSPACE_DRIFT)

    request_event = requests[0]
    if request_event.payload.request != request:
        return _rejected(ExecutionAdmissionReason.GATE_B_LINK_INVALID)
    if (
        request.command.expected_sequence != request_event.sequence
        or observation.event_sequence != decision_event.sequence
        or decision_event.sequence != request_event.sequence + 1
        or decision.source_channel is not SourceChannel.DIRECT_CLI
        or decision.actor.actor_type is not ActorType.HUMAN
        or decision.actor.actor_id != request.expected_actor_id
        or decision_event.actor_type is not ActorType.HUMAN
        or decision_event.actor_id != decision.actor.actor_id
    ):
        return _rejected(ExecutionAdmissionReason.GATE_B_LINK_INVALID)

    gate_b_artifact_hash = gate_b_artifact_hash_from_nonce(
        request.command.request_nonce
    )
    if gate_b_artifact_hash is None:
        return _rejected(ExecutionAdmissionReason.GATE_B_ARTIFACT_INVALID)

    graph_imports = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.TRELLIS_GRAPH_IMPORTED
    )
    if not graph_imports or graph_imports[-1].sequence > request_event.sequence:
        reason = (
            ExecutionAdmissionReason.TRELLIS_GRAPH_IMPORT_MISSING
            if not graph_imports
            else ExecutionAdmissionReason.TRELLIS_GRAPH_CHANGED
        )
        return _rejected(reason)
    if len(graph_imports) != 1:
        return _rejected(ExecutionAdmissionReason.TRELLIS_GRAPH_CHANGED)
    graph_event = graph_imports[0]
    if not _required_contract_evidence(
        graph_event,
        manifest.trellis_graph_digest,
    ):
        return _rejected(ExecutionAdmissionReason.TRELLIS_GRAPH_EVIDENCE_INVALID)
    frozen = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.TASK_GRAPH_FROZEN
        and event.sequence > decision_event.sequence
    )
    if not frozen:
        return _rejected(ExecutionAdmissionReason.TASK_GRAPH_NOT_FROZEN)
    if len(frozen) != 1:
        return _rejected(ExecutionAdmissionReason.LIFECYCLE_ORDER_INVALID)
    frozen_event = frozen[0]
    if any(
        event.sequence > decision_event.sequence
        for event in graph_imports
    ):
        return _rejected(ExecutionAdmissionReason.TRELLIS_GRAPH_CHANGED)
    if not all(
        _required_contract_evidence(frozen_event, digest)
        for digest in (
            gate_b_artifact_hash,
            manifest.trellis_graph_digest,
            manifest.canonical_sha256(),
        )
    ):
        return _rejected(
            ExecutionAdmissionReason.TASK_GRAPH_FREEZE_EVIDENCE_INVALID
        )

    lifecycle = tuple(
        _single_event(events, event_type)
        for event_type in (
            JournalEventType.RUN_INITIALIZED,
            JournalEventType.PREFLIGHT_COMPLETED,
            JournalEventType.DISCOVERY_COMPLETED,
            JournalEventType.GATE_APPROVED,
            JournalEventType.TRELLIS_GRAPH_IMPORTED,
            JournalEventType.TASK_GRAPH_FROZEN,
        )
    )
    if any(event is None for event in lifecycle):
        return _rejected(ExecutionAdmissionReason.LIFECYCLE_ORDER_INVALID)
    run_initialized, preflight, discovery, gate_approved, imported, frozen_check = lifecycle
    assert all(event is not None for event in lifecycle)
    if (
        not all(_transition_matches(event) for event in lifecycle if event is not None)
        or run_initialized.sequence != 1
        or preflight.sequence != 2
        or discovery.sequence != 3
        or not (
            discovery.sequence
            < gate_approved.sequence
            < imported.sequence
            < request_event.sequence
            < decision_event.sequence
            < frozen_check.sequence
        )
        or imported.sequence != gate_approved.sequence + 1
        or request_event.sequence != imported.sequence + 1
        or decision_event.sequence != request_event.sequence + 1
        or frozen_check.sequence != decision_event.sequence + 1
    ):
        return _rejected(ExecutionAdmissionReason.LIFECYCLE_ORDER_INVALID)
    return ExecutionAdmissionResult(
        True,
        ExecutionAdmissionReason.NONE,
        request_event,
        decision_event,
        frozen_event,
    )


__all__ = [
    "ExecutionAdmissionReason",
    "ExecutionAdmissionResult",
    "admit_execution_snapshot",
]
