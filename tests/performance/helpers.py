from __future__ import annotations

import dataclasses
from pathlib import Path

from tests.services.test_replay import graph_freeze_events, transition_event
from wish_builder.contracts.models import ExecutionManifest
from wish_builder.contracts.runtime import (
    JournalEvent,
    JournalEventType,
    RuntimeReasonCode,
    RuntimeState,
    TransitionSubject,
)


def append_status_events(
    journal: Path,
    *,
    first_sequence: int,
    through_sequence: int,
    previous_hash: str,
) -> JournalEvent:
    """Append a deterministic RUNNING/BLOCKED chain without retaining events."""

    segment = journal / "segments" / "segment-00000001.jsonl"
    segment.parent.mkdir(parents=True, exist_ok=True)
    last_event: JournalEvent | None = None
    with segment.open("ab") as handle:
        for sequence in range(first_sequence, through_sequence + 1):
            blocking = sequence % 2 == 1
            event = transition_event(
                sequence,
                previous_hash,
                (
                    JournalEventType.RUN_BLOCKED
                    if blocking
                    else JournalEventType.RUN_RESUMED
                ),
                TransitionSubject.RUN,
                RuntimeState.RUNNING if blocking else RuntimeState.BLOCKED,
                RuntimeState.BLOCKED if blocking else RuntimeState.RUNNING,
                reason_code=(
                    RuntimeReasonCode.CHECK_FAILED if blocking else None
                ),
            )
            handle.write(event.canonical_json_bytes())
            previous_hash = event.event_hash
            last_event = event
    if last_event is None:
        raise ValueError("the requested event range must be non-empty")
    return last_event


def write_large_journal(journal: Path, *, through_sequence: int) -> JournalEvent:
    """Write the normal graph-freeze prefix and a deterministic status tail."""

    prefix = graph_freeze_events()
    segment = journal / "segments" / "segment-00000001.jsonl"
    segment.parent.mkdir(parents=True, exist_ok=True)
    with segment.open("wb") as handle:
        for event in prefix:
            handle.write(event.canonical_json_bytes())
    if through_sequence == len(prefix):
        return prefix[-1]
    return append_status_events(
        journal,
        first_sequence=len(prefix) + 1,
        through_sequence=through_sequence,
        previous_hash=prefix[-1].event_hash,
    )


def envelope_manifest(base: ExecutionManifest) -> ExecutionManifest:
    """Build the 64-task, 512-edge accepted envelope from a real task model."""

    template = base.tasks[1]
    tasks = []
    for position in range(64):
        task_number = position + 1
        dependency_count = min(position, 8)
        if 9 <= position <= 44:
            dependency_count += 1
        dependencies = tuple(
            f"TASK-{prior + 1:03d}"
            for prior in range(position - dependency_count, position)
        )
        tasks.append(
            dataclasses.replace(
                template,
                id=f"TASK-{task_number:03d}",
                title=f"Envelope task {task_number:03d}",
                requirement_ids=("REQ-002",),
                depends_on=dependencies,
                owned_paths=(f"src/envelope/{task_number:03d}/**",),
                allowed_auxiliary_paths=(
                    f".trellis/tasks/envelope-{task_number:03d}/**",
                ),
                documentation=(),
                wave=0 if position == 0 else 2 if position == 63 else 1,
                may_change_contracts=position == 0,
                issue_id=task_number,
                branch=f"perf/envelope-{task_number:03d}",
                pr_id=None,
                squash_commit=None,
                agent_owner=None,
            )
        )
    return dataclasses.replace(base, tasks=tuple(tasks), max_concurrency=8)
