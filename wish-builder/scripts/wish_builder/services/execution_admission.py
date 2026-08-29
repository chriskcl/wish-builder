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
    JournalEvent,
    JournalEventType,
    SourceChannel,
)
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
    MANIFEST_DIGEST_MISMATCH = "manifest_digest_mismatch"
    WORKSPACE_DRIFT = "workspace_drift"
    TRELLIS_GRAPH_IMPORT_MISSING = "trellis_graph_import_missing"
    TRELLIS_GRAPH_CHANGED = "trellis_graph_changed"
    TASK_GRAPH_NOT_FROZEN = "task_graph_not_frozen"


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
        if (
            event.sequence != head.sequence + 1
            or event.previous_event_hash != head.event_hash
        ):
            return False
        head = type(head)(event.sequence, event.event_hash)
    return True


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

    decision_event = decisions[-1]
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

    matching_requests = tuple(
        event
        for event in requests
        if event.payload.request == request
    )
    if len(matching_requests) != 1:
        return _rejected(ExecutionAdmissionReason.GATE_B_LINK_INVALID)
    request_event = matching_requests[0]
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
    frozen = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.TASK_GRAPH_FROZEN
        and event.sequence > decision_event.sequence
    )
    if not frozen:
        return _rejected(ExecutionAdmissionReason.TASK_GRAPH_NOT_FROZEN)
    frozen_event = frozen[-1]
    if any(
        event.sequence > decision_event.sequence
        for event in graph_imports
    ):
        return _rejected(ExecutionAdmissionReason.TRELLIS_GRAPH_CHANGED)
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
