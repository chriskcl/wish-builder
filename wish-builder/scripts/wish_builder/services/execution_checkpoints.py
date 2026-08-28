"""Checkpoint publication for normal foreground execution."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from wish_builder.contracts.execution import (
    ExecutionManifestModel,
    is_execution_manifest_model,
)
from wish_builder.contracts.runtime import JournalEvent
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.kernel.state import KernelSnapshot

from .checkpoints import (
    CheckpointPersistenceFault,
    CheckpointPolicy,
    CheckpointStore,
    JournalPosition,
    VerifiedCheckpoint,
)
from .journal import DurableJournal, JournalHead, PersistenceFault


class ExecutionCheckpointStatus(StrEnum):
    SKIPPED = "skipped"
    PUBLISHED = "published"
    BLOCKED = "blocked"


class ExecutionCheckpointReason(StrEnum):
    NONE = "none"
    NOT_DUE = "not_due"
    STATE_MISMATCH = "state_mismatch"
    POSITION_FAILED = "position_failed"
    PUBLISH_FAILED = "publish_failed"
    ALREADY_BLOCKED = "already_blocked"


@dataclass(frozen=True, slots=True)
class ExecutionCheckpointResult:
    status: ExecutionCheckpointStatus
    reason: ExecutionCheckpointReason
    checkpoint: VerifiedCheckpoint | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not ExecutionCheckpointStatus:
            raise TypeError("status must be an ExecutionCheckpointStatus")
        if type(self.reason) is not ExecutionCheckpointReason:
            raise TypeError("reason must be an ExecutionCheckpointReason")
        if self.status is ExecutionCheckpointStatus.PUBLISHED:
            if (
                self.reason is not ExecutionCheckpointReason.NONE
                or type(self.checkpoint) is not VerifiedCheckpoint
            ):
                raise ValueError("published results require one verified checkpoint")
        elif self.checkpoint is not None:
            raise ValueError("non-published results cannot contain a checkpoint")
        elif self.status is ExecutionCheckpointStatus.SKIPPED:
            if self.reason is not ExecutionCheckpointReason.NOT_DUE:
                raise ValueError("skipped results require not_due")
        elif self.reason in {
            ExecutionCheckpointReason.NONE,
            ExecutionCheckpointReason.NOT_DUE,
        }:
            raise ValueError("blocked results require a failure reason")


class ExecutionCheckpointPublisher:
    """Publish derived checkpoints after durable foreground Journal events."""

    def __init__(
        self,
        manifest: ExecutionManifestModel,
        journal: DurableJournal,
        store: CheckpointStore,
        *,
        policy: CheckpointPolicy = CheckpointPolicy(),
        previous_sequence: int = 0,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not is_execution_manifest_model(manifest):
            raise TypeError("manifest must be an ExecutionManifest model")
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        if type(store) is not CheckpointStore:
            raise TypeError("store must be a CheckpointStore")
        if type(policy) is not CheckpointPolicy:
            raise TypeError("policy must be a CheckpointPolicy")
        if type(previous_sequence) is not int or previous_sequence < 0:
            raise ValueError("previous_sequence must be non-negative")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        started_at = monotonic_clock()
        if (
            type(started_at) not in {int, float}
            or type(started_at) is bool
            or not math.isfinite(started_at)
        ):
            raise ValueError("monotonic_clock must return a finite number")

        self._manifest = manifest
        self._journal = journal
        self._store = store
        self._policy = policy
        self._previous_sequence = previous_sequence
        self._monotonic_clock = monotonic_clock
        self._last_published_at = float(started_at)
        self._blocked = False

    @property
    def blocked(self) -> bool:
        return self._blocked

    @property
    def previous_sequence(self) -> int:
        return self._previous_sequence

    def observe(
        self,
        snapshot: KernelSnapshot,
        graph_index: GraphIndex,
        head: JournalHead,
        event: JournalEvent,
    ) -> ExecutionCheckpointResult:
        """Publish when policy is due; any ambiguity permanently blocks this instance."""

        if self._blocked:
            return ExecutionCheckpointResult(
                ExecutionCheckpointStatus.BLOCKED,
                ExecutionCheckpointReason.ALREADY_BLOCKED,
            )
        if (
            type(snapshot) is not KernelSnapshot
            or type(graph_index) is not GraphIndex
            or type(head) is not JournalHead
            or type(event) is not JournalEvent
        ):
            raise TypeError("observe requires snapshot, graph_index, head, and event")
        if not self._state_matches(snapshot, graph_index, head, event):
            return self._block(ExecutionCheckpointReason.STATE_MISMATCH)
        now = self._read_clock()
        if now is None:
            return self._block(ExecutionCheckpointReason.PUBLISH_FAILED)
        elapsed = now - self._last_published_at
        if elapsed < 0:
            return self._block(ExecutionCheckpointReason.PUBLISH_FAILED)
        if not self._policy.should_publish(
            previous_sequence=self._previous_sequence,
            current_sequence=snapshot.last_sequence,
            last_event_type=event.event_type,
            elapsed_seconds=elapsed,
        ):
            return ExecutionCheckpointResult(
                ExecutionCheckpointStatus.SKIPPED,
                ExecutionCheckpointReason.NOT_DUE,
            )
        try:
            segment, offset = self._journal.current_position(expected_head=head)
        except (PersistenceFault, OSError, TypeError, ValueError):
            return self._block(ExecutionCheckpointReason.POSITION_FAILED)
        try:
            checkpoint = self._store.publish(
                self._manifest,
                snapshot,
                graph_index,
                JournalPosition(segment, offset),
            )
        except (CheckpointPersistenceFault, OSError, TypeError, ValueError):
            return self._block(ExecutionCheckpointReason.PUBLISH_FAILED)
        self._previous_sequence = snapshot.last_sequence
        self._last_published_at = now
        return ExecutionCheckpointResult(
            ExecutionCheckpointStatus.PUBLISHED,
            ExecutionCheckpointReason.NONE,
            checkpoint,
        )

    def _state_matches(
        self,
        snapshot: KernelSnapshot,
        graph_index: GraphIndex,
        head: JournalHead,
        event: JournalEvent,
    ) -> bool:
        return (
            snapshot.run_id == self._manifest.run_id
            and event.identity.run_id == self._manifest.run_id
            and snapshot.last_sequence == head.sequence == event.sequence
            and snapshot.last_event_hash == head.event_hash == event.event_hash
            and self._previous_sequence <= snapshot.last_sequence
            and graph_index.verify(self._manifest, snapshot)
        )

    def _read_clock(self) -> float | None:
        try:
            value = self._monotonic_clock()
        except Exception:
            return None
        if (
            type(value) not in {int, float}
            or type(value) is bool
            or not math.isfinite(value)
        ):
            return None
        return float(value)

    def _block(
        self,
        reason: ExecutionCheckpointReason,
    ) -> ExecutionCheckpointResult:
        self._blocked = True
        return ExecutionCheckpointResult(
            ExecutionCheckpointStatus.BLOCKED,
            reason,
        )


__all__ = [
    "ExecutionCheckpointPublisher",
    "ExecutionCheckpointReason",
    "ExecutionCheckpointResult",
    "ExecutionCheckpointStatus",
]
