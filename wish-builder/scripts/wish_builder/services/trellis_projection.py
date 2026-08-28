"""Repairable Trellis task projections derived from durable Journal events."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from wish_builder.contracts.manifest_v2 import ExecutionManifestV2
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObservationPayload,
    EffectOperation,
    EffectStatus,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.services.journal import (
    GENESIS_HEAD,
    DurableJournal,
    JournalHead,
)
from wish_builder.services.ports.projection import (
    TrellisProjection,
    TrellisProjectionApplyRequest,
    TrellisProjectionDisposition,
    TrellisProjectionObservation,
    TrellisProjectionPort,
    TrellisProjectionReason,
)


class TrellisProjectionSyncStatus(StrEnum):
    SKIPPED = "skipped"
    APPLIED = "applied"
    DELAYED = "delayed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class TrellisProjectionSyncResult:
    status: TrellisProjectionSyncStatus
    reason: TrellisProjectionReason
    canonical_head: JournalHead
    observation: TrellisProjectionObservation | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not TrellisProjectionSyncStatus:
            raise TypeError("status must be a TrellisProjectionSyncStatus")
        if type(self.reason) is not TrellisProjectionReason:
            raise TypeError("reason must be a TrellisProjectionReason")
        if type(self.canonical_head) is not JournalHead:
            raise TypeError("canonical_head must be a JournalHead")
        if self.observation is not None and type(
            self.observation
        ) is not TrellisProjectionObservation:
            raise TypeError("observation must be a TrellisProjectionObservation")
        if self.status in {
            TrellisProjectionSyncStatus.SKIPPED,
            TrellisProjectionSyncStatus.APPLIED,
        } and self.reason is not TrellisProjectionReason.NONE:
            raise ValueError("successful projection results require reason=none")
        if self.status in {
            TrellisProjectionSyncStatus.DELAYED,
            TrellisProjectionSyncStatus.CONFLICT,
        } and self.reason is TrellisProjectionReason.NONE:
            raise ValueError("blocked projection results require a reason")


@runtime_checkable
class TrellisProjectionCheckoutProvider(Protocol):
    def ensure(self, run_id: str) -> object: ...


class TrellisProjectionService:
    """Project only events proven to be the current durable Journal head."""

    def __init__(
        self,
        manifest: ExecutionManifestV2,
        journal: DurableJournal,
        checkout_provider: TrellisProjectionCheckoutProvider,
        port: TrellisProjectionPort,
    ) -> None:
        if type(manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        if not isinstance(checkout_provider, TrellisProjectionCheckoutProvider):
            raise TypeError("checkout_provider must provide a projection workspace")
        if not isinstance(port, TrellisProjectionPort):
            raise TypeError("port must implement TrellisProjectionPort")
        self._manifest = manifest
        self._journal = journal
        self._checkout_provider = checkout_provider
        self._port = port
        self._trellis_task_ids = {
            mapping.task_id: mapping.trellis_task_id
            for mapping in manifest.task_id_mapping
        }

    def project_committed_event(
        self, event: JournalEvent
    ) -> TrellisProjectionSyncResult:
        if type(event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent")
        head = JournalHead(event.sequence, event.event_hash)
        candidate = self._projection(event)
        if candidate is None:
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.SKIPPED,
                TrellisProjectionReason.NONE,
                head,
            )
        return self._sync_projection(event, candidate, head)

    def reconcile_verified_events(
        self,
        events: tuple[JournalEvent, ...],
        *,
        verified_head: JournalHead,
    ) -> tuple[TrellisProjectionSyncResult, ...]:
        """Repair each task from the latest event in one verified Journal chain."""

        if type(events) is not tuple or not all(
            type(event) is JournalEvent for event in events
        ):
            raise TypeError("events must contain JournalEvent values")
        if type(verified_head) is not JournalHead:
            raise TypeError("verified_head must be a JournalHead")

        head = GENESIS_HEAD
        candidates: dict[str, tuple[JournalEvent, TrellisProjection]] = {}
        for event in events:
            if (
                event.identity.run_id != self._manifest.run_id
                or event.sequence != head.sequence + 1
                or event.previous_event_hash != head.event_hash
            ):
                raise ValueError("events do not form the manifest Journal chain")
            head = JournalHead(event.sequence, event.event_hash)
            candidate = self._projection(event)
            if candidate is not None:
                candidates[candidate.task_id] = (event, candidate)
        if head != verified_head:
            raise ValueError("verified_head does not match the supplied Journal chain")

        return tuple(
            self._sync_projection(event, candidate, verified_head)
            for event, candidate in sorted(
                candidates.values(),
                key=lambda item: (
                    item[0].sequence,
                    item[1].task_id.encode("utf-8", errors="strict"),
                ),
            )
        )

    def _sync_projection(
        self,
        event: JournalEvent,
        candidate: TrellisProjection,
        durability_head: JournalHead,
    ) -> TrellisProjectionSyncResult:
        candidate_head = JournalHead(event.sequence, event.event_hash)
        try:
            self._journal.current_position(expected_head=durability_head)
        except Exception:  # noqa: BLE001 - derived projection must not affect authority
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.DELAYED,
                TrellisProjectionReason.CANONICAL_NOT_DURABLE,
                candidate_head,
            )
        try:
            checkout = self._checkout_provider.ensure(self._manifest.run_id)
            checkout_root = _checkout_path(checkout)
        except Exception:  # noqa: BLE001 - checkout is a repairable derived boundary
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.DELAYED,
                TrellisProjectionReason.CHECKOUT_UNAVAILABLE,
                candidate_head,
            )
        try:
            inspected = self._port.inspect(checkout_root, candidate.trellis_task_id)
        except Exception:  # noqa: BLE001 - adapter crashes cannot roll back the Journal
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.DELAYED,
                TrellisProjectionReason.UNAVAILABLE,
                candidate_head,
            )
        if type(inspected) is not TrellisProjectionObservation:
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.DELAYED,
                TrellisProjectionReason.INVALID,
                candidate_head,
            )
        if inspected.disposition is TrellisProjectionDisposition.UNAVAILABLE:
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.DELAYED,
                inspected.reason,
                candidate_head,
                inspected,
            )
        if inspected.record_revision is None:
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.DELAYED,
                TrellisProjectionReason.INVALID,
                candidate_head,
                inspected,
            )
        try:
            applied = self._port.apply(
                TrellisProjectionApplyRequest(
                    checkout_root,
                    candidate.trellis_task_id,
                    inspected.record_revision,
                    candidate,
                )
            )
        except Exception:  # noqa: BLE001 - adapter crashes cannot roll back the Journal
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.DELAYED,
                TrellisProjectionReason.UNAVAILABLE,
                candidate_head,
            )
        if type(applied) is not TrellisProjectionObservation:
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.DELAYED,
                TrellisProjectionReason.INVALID,
                candidate_head,
            )
        if applied.disposition in {
            TrellisProjectionDisposition.APPLIED,
            TrellisProjectionDisposition.IDEMPOTENT,
        }:
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.APPLIED,
                TrellisProjectionReason.NONE,
                candidate_head,
                applied,
            )
        if applied.disposition is TrellisProjectionDisposition.CONFLICT:
            return TrellisProjectionSyncResult(
                TrellisProjectionSyncStatus.CONFLICT,
                applied.reason,
                candidate_head,
                applied,
            )
        return TrellisProjectionSyncResult(
            TrellisProjectionSyncStatus.DELAYED,
            (
                applied.reason
                if applied.reason is not TrellisProjectionReason.NONE
                else TrellisProjectionReason.UNAVAILABLE
            ),
            candidate_head,
            applied,
        )

    def _projection(self, event: JournalEvent) -> TrellisProjection | None:
        if event.identity.run_id != self._manifest.run_id:
            raise ValueError("event run identity does not match the manifest")
        task_id = event.identity.task_id
        if task_id is None:
            return None
        state_and_evidence = _task_state_and_evidence(event)
        if state_and_evidence is None:
            return None
        state, evidence = state_and_evidence
        trellis_task_id = self._trellis_task_ids.get(task_id)
        if trellis_task_id is None:
            raise ValueError("event task is not mapped by the approved manifest")
        operation_key = hashlib.sha256(
            f"{event.identity.run_id}\0{task_id}\0{event.sequence}\0{event.event_hash}".encode(
                "utf-8", errors="strict"
            )
        ).hexdigest()[:40]
        return TrellisProjection(
            schema_version=1,
            operation_id=f"projection-{operation_key}",
            run_id=event.identity.run_id,
            task_id=task_id,
            trellis_task_id=trellis_task_id,
            manifest_digest=self._manifest.canonical_sha256(),
            trellis_graph_digest=self._manifest.trellis_graph_digest,
            canonical_sequence=event.sequence,
            canonical_event_hash=event.event_hash,
            canonical_state=state.value,
            target_status=_target_status(state),
            evidence_digests=tuple(item.digest for item in evidence),
            summary=(
                f"Canonical task state is {state.value} at Journal sequence "
                f"{event.sequence}."
            ),
        )


def _task_state_and_evidence(event: JournalEvent):
    payload = event.payload
    if (
        type(payload) is TransitionPayload
        and payload.subject is TransitionSubject.TASK
    ):
        return payload.to_state, payload.evidence
    if (
        event.event_type is JournalEventType.DISPATCH_OBSERVED
        and type(payload) is EffectObservationPayload
        and payload.adapter is AdapterKind.TASK
        and payload.receipt.operation is EffectOperation.WORKER_DISPATCH
        and payload.receipt.status is EffectStatus.APPLIED
    ):
        return RuntimeState.DISPATCHED, payload.receipt.evidence
    if (
        event.event_type is JournalEventType.PROMOTION_OBSERVED
        and type(payload) is EffectObservationPayload
        and payload.adapter is AdapterKind.GIT
        and payload.receipt.operation is EffectOperation.RESULT_PROMOTION
        and payload.receipt.status is EffectStatus.APPLIED
    ):
        return RuntimeState.PROMOTED, payload.receipt.evidence
    return None


def _target_status(state: RuntimeState) -> str:
    if state in {
        RuntimeState.APPROVED,
        RuntimeState.READY,
        RuntimeState.LEASED,
    }:
        return "planning"
    if state in {RuntimeState.VERIFIED, RuntimeState.ARCHIVED}:
        return "completed"
    return "in_progress"


def _checkout_path(value: object) -> Path:
    path = getattr(value, "path", value)
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("checkout provider returned an invalid path")
    return path




__all__ = [
    "TrellisProjectionCheckoutProvider",
    "TrellisProjectionService",
    "TrellisProjectionSyncResult",
    "TrellisProjectionSyncStatus",
]
