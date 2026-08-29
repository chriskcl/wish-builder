"""Durable terminal lifecycle composition for a production foreground run."""

from __future__ import annotations

from wish_builder.contracts.manifest_v2 import ExecutionManifestV2
from wish_builder.contracts.runtime import (
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel.state import apply_journal_event
from wish_builder.processes.coordinator import CoordinatorCursor
from wish_builder.processes.foreground import ForegroundTerminalResult
from wish_builder.services.execution_checkpoints import (
    ExecutionCheckpointPublisher,
    ExecutionCheckpointStatus,
)
from wish_builder.services.journal import AppendStatus, DurableJournal, JournalEventDraft
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    LeaseMutationStatus,
)


class ProductionTerminalFinalizer:
    """Append the ordered terminal prefix, release its lease, and checkpoint it."""

    def __init__(
        self,
        manifest: ExecutionManifestV2,
        journal: DurableJournal,
        lease_service: CoordinatorLeaseService,
        checkpoint_publisher: ExecutionCheckpointPublisher,
        *,
        coordinator_id: str,
        fencing_token: int,
        recovered_terminal_event: JournalEvent | None = None,
    ) -> None:
        if type(manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        if type(lease_service) is not CoordinatorLeaseService:
            raise TypeError("lease_service must be a CoordinatorLeaseService")
        if type(checkpoint_publisher) is not ExecutionCheckpointPublisher:
            raise TypeError(
                "checkpoint_publisher must be an ExecutionCheckpointPublisher"
            )
        if type(coordinator_id) is not str or not coordinator_id:
            raise ValueError("coordinator_id must be non-empty")
        if type(fencing_token) is not int or fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if recovered_terminal_event is not None and (
            type(recovered_terminal_event) is not JournalEvent
            or recovered_terminal_event.event_type
            is not JournalEventType.LEASE_RELEASED
        ):
            raise ValueError(
                "recovered_terminal_event must be a LEASE_RELEASED event or null"
            )
        self._manifest = manifest
        self._journal = journal
        self._lease_service = lease_service
        self._checkpoint_publisher = checkpoint_publisher
        self._coordinator_id = coordinator_id
        self._fencing_token = fencing_token
        self._recovered_terminal_event = recovered_terminal_event

    def finish(self, cursor: CoordinatorCursor) -> ForegroundTerminalResult:
        if type(cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        if (
            cursor.snapshot.run_id != self._manifest.run_id
            or cursor.snapshot.coordinator_epoch != self._fencing_token
            or not cursor.graph_index.verify(self._manifest, cursor.snapshot)
        ):
            return ForegroundTerminalResult(False, cursor)

        events: list[JournalEvent] = []
        current = cursor
        transitions = (
            (
                JournalEventType.EXECUTION_COMPLETED,
                RuntimeState.EXECUTING,
                RuntimeState.INTEGRATION,
            ),
            (
                JournalEventType.INTEGRATION_VERIFIED,
                RuntimeState.INTEGRATION,
                RuntimeState.QUALITY_DOCS,
            ),
            (
                JournalEventType.QUALITY_DOCS_VERIFIED,
                RuntimeState.QUALITY_DOCS,
                RuntimeState.COMPLETE,
            ),
        )
        phase_positions = {
            RuntimeState.EXECUTING: 0,
            RuntimeState.INTEGRATION: 1,
            RuntimeState.QUALITY_DOCS: 2,
            RuntimeState.COMPLETE: 3,
        }
        position = phase_positions.get(current.snapshot.phase)
        if position is None:
            return ForegroundTerminalResult(False, current)
        if not current.lease_state.active:
            return self._finish_recovered_release(current)

        for event_type, from_state, to_state in transitions[position:]:
            appended = self._journal.append_draft(
                JournalEventDraft(
                    event_id=(
                        "EVENT-PRODUCTION-"
                        f"{event_type.value.replace('_', '-').upper()}-"
                        f"{current.head.sequence + 1:08d}"
                    ),
                    event_type=event_type,
                    identity=ExecutionIdentity(
                        self._manifest.run_id,
                        self._fencing_token,
                    ),
                    actor_type=ActorType.COORDINATOR,
                    actor_id=self._coordinator_id,
                    payload=TransitionPayload(
                        TransitionSubject.RUN,
                        from_state,
                        to_state,
                    ),
                ),
                expected_head=current.head,
            )
            if (
                appended.status
                not in {AppendStatus.COMMITTED, AppendStatus.IDEMPOTENT}
                or appended.event is None
            ):
                return ForegroundTerminalResult(False, current, tuple(events))
            advanced = self._advance(current, appended.event)
            if advanced is None:
                return ForegroundTerminalResult(False, current, tuple(events))
            current = advanced
            events.append(appended.event)

        released = self._lease_service.release(
            event_id=(
                "EVENT-PRODUCTION-LEASE-RELEASED-"
                f"{current.head.sequence + 1:08d}"
            )
        )
        if (
            released.status
            not in {LeaseMutationStatus.COMMITTED, LeaseMutationStatus.IDEMPOTENT}
            or released.append_result is None
            or released.append_result.event is None
        ):
            return ForegroundTerminalResult(False, current, tuple(events))
        advanced = self._advance(current, released.append_result.event)
        if advanced is None or released.lease_state != advanced.lease_state:
            return ForegroundTerminalResult(False, current, tuple(events))
        current = advanced
        events.append(released.append_result.event)

        checkpoint = self._checkpoint_publisher.observe(
            current.snapshot,
            current.graph_index,
            current.head,
            events[-1],
        )
        if checkpoint.status is not ExecutionCheckpointStatus.PUBLISHED:
            return ForegroundTerminalResult(False, current, tuple(events))
        return ForegroundTerminalResult(True, current, tuple(events))

    def _finish_recovered_release(
        self,
        cursor: CoordinatorCursor,
    ) -> ForegroundTerminalResult:
        event = self._recovered_terminal_event
        if (
            cursor.snapshot.phase is not RuntimeState.COMPLETE
            or event is None
            or event.sequence != cursor.head.sequence
            or event.event_hash != cursor.head.event_hash
        ):
            return ForegroundTerminalResult(False, cursor)
        checkpoint = self._checkpoint_publisher.observe(
            cursor.snapshot,
            cursor.graph_index,
            cursor.head,
            event,
        )
        if checkpoint.status not in {
            ExecutionCheckpointStatus.PUBLISHED,
            ExecutionCheckpointStatus.SKIPPED,
        }:
            return ForegroundTerminalResult(False, cursor, (event,))
        return ForegroundTerminalResult(True, cursor, (event,))

    @staticmethod
    def _advance(
        cursor: CoordinatorCursor,
        event: JournalEvent,
    ) -> CoordinatorCursor | None:
        try:
            applied = apply_journal_event(cursor.snapshot, event)
            if not applied.accepted:
                return None
            return CoordinatorCursor(
                applied.snapshot,
                cursor.graph_index.advance(cursor.snapshot, applied.snapshot),
                cursor.lease_state.advance(event),
                cursor.dispatch_recoveries,
            )
        except (TypeError, ValueError):
            return None


__all__ = ["ProductionTerminalFinalizer"]
