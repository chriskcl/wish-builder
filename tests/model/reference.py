"""Independent full-scan models used to check the optimized kernel.

This module deliberately depends on contracts, not ``wish_builder.kernel``. It
keeps straightforward tables and immutable projections so a defect in the
production reducer is unlikely to be repeated through shared implementation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TypeVar

from wish_builder.contracts.models import ExecutionManifest
from wish_builder.contracts.runtime import (
    JournalEventType,
    RuntimeReasonCode,
    RuntimeState,
    TransitionSubject,
)


GENESIS_HASH = "sha256:" + "0" * 64
_T = TypeVar("_T")
COMPLETE = frozenset({RuntimeState.VERIFIED, RuntimeState.ARCHIVED})
TERMINAL_ATTEMPTS = frozenset(
    {
        RuntimeState.SUCCEEDED,
        RuntimeState.FAILED,
        RuntimeState.TERMINATED,
        RuntimeState.OUTCOME_UNKNOWN,
    }
)


class ReferenceReason(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    SEQUENCE_CONFLICT = "sequence_conflict"
    STALE_SEQUENCE = "stale_sequence"
    SEQUENCE_GAP = "sequence_gap"
    HASH_CHAIN_MISMATCH = "hash_chain_mismatch"
    RUN_MISMATCH = "run_mismatch"
    STALE_EPOCH = "stale_epoch"
    IDENTITY_MISMATCH = "identity_mismatch"
    ILLEGAL_TRANSITION = "illegal_transition"
    STATE_MISMATCH = "state_mismatch"
    ACTIVE_ATTEMPT_EXISTS = "active_attempt_exists"
    STALE_ATTEMPT = "stale_attempt"
    STALE_CORRELATION = "stale_correlation"


RUN_PHASE_TRANSITIONS = {
    JournalEventType.RUN_INITIALIZED: {(RuntimeState.NONE, RuntimeState.PREFLIGHT)},
    JournalEventType.PREFLIGHT_COMPLETED: {
        (RuntimeState.PREFLIGHT, RuntimeState.DISCOVERY)
    },
    JournalEventType.DISCOVERY_COMPLETED: {
        (RuntimeState.DISCOVERY, RuntimeState.GATE_A_PENDING)
    },
    JournalEventType.GATE_APPROVED: {
        (RuntimeState.GATE_A_PENDING, RuntimeState.TRELLIS_PREPARATION),
        (RuntimeState.GATE_A_PENDING, RuntimeState.DECOMPOSITION),
    },
    JournalEventType.TRELLIS_GRAPH_IMPORTED: {
        (RuntimeState.TRELLIS_PREPARATION, RuntimeState.GATE_B_PENDING)
    },
    JournalEventType.DECOMPOSITION_COMPLETED: {
        (RuntimeState.DECOMPOSITION, RuntimeState.GATE_B_PENDING)
    },
    JournalEventType.TASK_GRAPH_FROZEN: {
        (RuntimeState.GATE_B_PENDING, RuntimeState.EXECUTING)
    },
    JournalEventType.EXECUTION_COMPLETED: {
        (RuntimeState.EXECUTING, RuntimeState.INTEGRATION)
    },
    JournalEventType.INTEGRATION_VERIFIED: {
        (RuntimeState.INTEGRATION, RuntimeState.QUALITY_DOCS)
    },
    JournalEventType.QUALITY_DOCS_VERIFIED: {
        (RuntimeState.QUALITY_DOCS, RuntimeState.COMPLETE)
    },
}

_NONTERMINAL_RUN_STATUSES = (
    RuntimeState.RUNNING,
    RuntimeState.PAUSING,
    RuntimeState.PAUSED,
    RuntimeState.BLOCKED,
    RuntimeState.ESCALATED,
    RuntimeState.CANCELLING,
)
RUN_STATUS_TRANSITIONS = {
    JournalEventType.PAUSE_REQUESTED: {
        (state, RuntimeState.PAUSING)
        for state in (RuntimeState.RUNNING, RuntimeState.BLOCKED, RuntimeState.ESCALATED)
    },
    JournalEventType.RUN_PAUSED: {(RuntimeState.PAUSING, RuntimeState.PAUSED)},
    JournalEventType.RUN_RESUMED: {
        (state, RuntimeState.RUNNING)
        for state in (RuntimeState.PAUSED, RuntimeState.BLOCKED, RuntimeState.ESCALATED)
    },
    JournalEventType.RUN_BLOCKED: {
        (state, RuntimeState.BLOCKED)
        for state in (RuntimeState.RUNNING, RuntimeState.PAUSING)
    },
    JournalEventType.RUN_ESCALATED: {
        (state, RuntimeState.ESCALATED)
        for state in (RuntimeState.RUNNING, RuntimeState.BLOCKED)
    },
    JournalEventType.CANCEL_REQUESTED: {
        (state, RuntimeState.CANCELLING) for state in _NONTERMINAL_RUN_STATUSES
    },
    JournalEventType.RUN_CANCELLED: {
        (RuntimeState.CANCELLING, RuntimeState.CANCELLED)
    },
    JournalEventType.RUN_FAILED: {
        (state, RuntimeState.FAILED) for state in _NONTERMINAL_RUN_STATUSES
    },
    JournalEventType.RUN_ARCHIVED: {
        (state, RuntimeState.ARCHIVED)
        for state in (RuntimeState.RUNNING, RuntimeState.CANCELLED, RuntimeState.FAILED)
    },
}

TASK_TRANSITIONS = {
    JournalEventType.TASK_READY: {(RuntimeState.APPROVED, RuntimeState.READY)},
    JournalEventType.LEASE_ACQUIRED: {(RuntimeState.READY, RuntimeState.LEASED)},
    JournalEventType.DISPATCH_OBSERVED: {
        (RuntimeState.LEASED, RuntimeState.DISPATCHED)
    },
    JournalEventType.TASK_BLOCKED: {
        (state, RuntimeState.BLOCKED)
        for state in (
            RuntimeState.LEASED,
            RuntimeState.DISPATCHED,
            RuntimeState.PR_OPEN,
            RuntimeState.STAGED,
            RuntimeState.PROMOTED,
        )
    },
    JournalEventType.PR_OBSERVED: {
        (RuntimeState.DISPATCHED, RuntimeState.PR_OPEN),
        (RuntimeState.PROMOTED, RuntimeState.PR_OPEN),
    },
    JournalEventType.MERGE_OBSERVED: {
        (RuntimeState.PR_OPEN, RuntimeState.MERGED)
    },
    JournalEventType.TASK_VERIFIED: {
        (RuntimeState.MERGED, RuntimeState.VERIFIED),
        (RuntimeState.PROMOTED, RuntimeState.VERIFIED),
    },
    JournalEventType.TASK_ARCHIVED: {
        (RuntimeState.VERIFIED, RuntimeState.ARCHIVED)
    },
    JournalEventType.TASK_RETRY_SCHEDULED: {
        (RuntimeState.BLOCKED, RuntimeState.READY)
    },
    JournalEventType.REVERT_OBSERVED: {
        (RuntimeState.MERGED, RuntimeState.REVERTED),
        (RuntimeState.VERIFIED, RuntimeState.REVERTED),
    },
    JournalEventType.TASK_INVALIDATED: {
        (state, RuntimeState.INVALIDATED)
        for state in (
            RuntimeState.APPROVED,
            RuntimeState.READY,
            RuntimeState.LEASED,
            RuntimeState.DISPATCHED,
            RuntimeState.PR_OPEN,
            RuntimeState.MERGED,
            RuntimeState.VERIFIED,
        )
    },
    JournalEventType.REPAIR_SCHEDULED: {
        (RuntimeState.REVERTED, RuntimeState.READY)
    },
    JournalEventType.REWORK_SCHEDULED: {
        (RuntimeState.INVALIDATED, RuntimeState.READY)
    },
    JournalEventType.TASK_REVERIFIED: {
        (RuntimeState.INVALIDATED, RuntimeState.VERIFIED)
    },
    JournalEventType.RESULT_STAGED: {
        (RuntimeState.DISPATCHED, RuntimeState.STAGED)
    },
    JournalEventType.PROMOTION_OBSERVED: {
        (RuntimeState.STAGED, RuntimeState.PROMOTED)
    },
}

ATTEMPT_TRANSITIONS = {
    JournalEventType.ATTEMPT_RESERVED: {
        (RuntimeState.PLANNED, RuntimeState.RESERVED)
    },
    JournalEventType.DISPATCH_REQUESTED: {
        (RuntimeState.RESERVED, RuntimeState.DISPATCH_REQUESTED)
    },
    JournalEventType.ATTEMPT_RELEASED: {
        (RuntimeState.RESERVED, RuntimeState.TERMINATED)
    },
    JournalEventType.DISPATCH_OBSERVED: {
        (RuntimeState.DISPATCH_REQUESTED, RuntimeState.RUNNING)
    },
    JournalEventType.ATTEMPT_SUCCEEDED: {
        (RuntimeState.RUNNING, RuntimeState.SUCCEEDED)
    },
    JournalEventType.ATTEMPT_FAILED: {
        (RuntimeState.DISPATCH_REQUESTED, RuntimeState.FAILED),
        (RuntimeState.RUNNING, RuntimeState.FAILED),
    },
    JournalEventType.CANCEL_REQUESTED: {
        (RuntimeState.RUNNING, RuntimeState.CANCEL_REQUESTED)
    },
    JournalEventType.ATTEMPT_TERMINATED: {
        (RuntimeState.CANCEL_REQUESTED, RuntimeState.TERMINATED)
    },
    JournalEventType.ATTEMPT_OUTCOME_UNKNOWN: {
        (RuntimeState.DISPATCH_REQUESTED, RuntimeState.OUTCOME_UNKNOWN),
        (RuntimeState.CANCEL_REQUESTED, RuntimeState.OUTCOME_UNKNOWN),
    },
}


@dataclass(frozen=True, slots=True)
class ReferenceIdentity:
    run_id: str
    coordinator_epoch: int
    task_id: str | None = None
    attempt: int | None = None
    correlation_id: str | None = None

    @property
    def is_attempt(self) -> bool:
        return self.attempt is not None


@dataclass(frozen=True, slots=True)
class ReferenceTransition:
    sequence: int
    event_id: str
    event_hash: str
    previous_event_hash: str
    event_type: JournalEventType
    subject: TransitionSubject
    from_state: RuntimeState
    to_state: RuntimeState
    identity: ReferenceIdentity
    reason_code: RuntimeReasonCode | None = None


@dataclass(frozen=True, slots=True)
class ReferenceTask:
    task_id: str
    state: RuntimeState = RuntimeState.PROPOSED
    reason_code: RuntimeReasonCode | None = None


@dataclass(frozen=True, slots=True)
class ReferenceAttempt:
    task_id: str
    attempt: int
    correlation_id: str
    coordinator_epoch: int
    state: RuntimeState
    reason_code: RuntimeReasonCode | None = None


@dataclass(frozen=True, slots=True)
class ReferenceSnapshot:
    run_id: str
    coordinator_epoch: int
    phase: RuntimeState
    status: RuntimeState
    run_reason_code: RuntimeReasonCode | None
    tasks: tuple[ReferenceTask, ...]
    attempts: tuple[ReferenceAttempt, ...]
    last_sequence: int
    last_event_id: str | None
    last_event_hash: str

    @classmethod
    def initial(
        cls,
        manifest: ExecutionManifest,
        run_id: str | None = None,
        coordinator_epoch: int = 1,
    ) -> ReferenceSnapshot:
        return cls(
            run_id=manifest.run_id if run_id is None else run_id,
            coordinator_epoch=coordinator_epoch,
            phase=RuntimeState.NONE,
            status=RuntimeState.RUNNING,
            run_reason_code=None,
            tasks=tuple(ReferenceTask(task_id) for task_id in topological_order(manifest)),
            attempts=(),
            last_sequence=0,
            last_event_id=None,
            last_event_hash=GENESIS_HASH,
        )

    def task_states(self) -> dict[str, RuntimeState]:
        return {task.task_id: task.state for task in self.tasks}

    def ready(
        self,
        manifest: ExecutionManifest,
        conflicts: Mapping[str, frozenset[str]] | None = None,
    ) -> tuple[str, ...]:
        if self.phase is not RuntimeState.EXECUTING:
            return ()
        if self.status is not RuntimeState.RUNNING:
            return ()
        if conflicts is None:
            conflicts = {task.id: frozenset() for task in manifest.tasks}
        return ready_tasks(manifest, self.task_states(), conflicts)


@dataclass(frozen=True, slots=True)
class ReferenceResult:
    accepted: bool
    reason: ReferenceReason
    snapshot: ReferenceSnapshot


@dataclass(frozen=True, slots=True)
class GeneratedSequence:
    name: str
    task_order: tuple[str, ...]
    transitions: tuple[ReferenceTransition, ...]


@dataclass(frozen=True, slots=True)
class GeneratedFailure:
    name: str
    expected_reason: ReferenceReason
    transitions: tuple[ReferenceTransition, ...]


@dataclass(frozen=True, slots=True)
class SequenceFailure:
    index: int
    reason: ReferenceReason
    snapshot: ReferenceSnapshot


def topological_order(manifest: ExecutionManifest) -> tuple[str, ...]:
    dependencies = {task.id: set(task.depends_on) for task in manifest.tasks}
    order: list[str] = []
    while dependencies:
        ready = sorted(
            task_id for task_id, pending in dependencies.items() if not pending
        )
        if not ready:
            raise ValueError("dependency_cycle")
        for task_id in ready:
            order.append(task_id)
            dependencies.pop(task_id)
        for pending in dependencies.values():
            pending.difference_update(ready)
    return tuple(order)


def ready_tasks(
    manifest: ExecutionManifest,
    states: Mapping[str, RuntimeState],
    conflicts: Mapping[str, frozenset[str]],
) -> tuple[str, ...]:
    selected: list[str] = []
    active = {
        task_id
        for task_id, state in states.items()
        if state
        in {
            RuntimeState.LEASED,
            RuntimeState.DISPATCHED,
            RuntimeState.PR_OPEN,
            RuntimeState.STAGED,
            RuntimeState.PROMOTED,
        }
    }
    by_id = {task.id: task for task in manifest.tasks}
    for task_id in topological_order(manifest):
        if len(active) + len(selected) >= manifest.max_concurrency:
            break
        task = by_id[task_id]
        if states[task_id] not in {RuntimeState.APPROVED, RuntimeState.READY}:
            continue
        if any(states[dependency] not in COMPLETE for dependency in task.depends_on):
            continue
        if conflicts[task_id].intersection(active):
            continue
        if conflicts[task_id].intersection(selected):
            continue
        selected.append(task_id)
    return tuple(selected)


def reduce_transition(
    snapshot: ReferenceSnapshot,
    transition: ReferenceTransition,
) -> ReferenceResult:
    """Fold one transition without calling or importing the production kernel."""

    def rejected(reason: ReferenceReason) -> ReferenceResult:
        return ReferenceResult(False, reason, snapshot)

    if transition.sequence == snapshot.last_sequence:
        if (
            transition.event_id == snapshot.last_event_id
            and transition.event_hash == snapshot.last_event_hash
        ):
            return ReferenceResult(
                True,
                ReferenceReason.IDEMPOTENT_REPLAY,
                snapshot,
            )
        return rejected(ReferenceReason.SEQUENCE_CONFLICT)
    if transition.sequence < snapshot.last_sequence:
        return rejected(ReferenceReason.STALE_SEQUENCE)
    if transition.sequence != snapshot.last_sequence + 1:
        return rejected(ReferenceReason.SEQUENCE_GAP)
    if transition.previous_event_hash != snapshot.last_event_hash:
        return rejected(ReferenceReason.HASH_CHAIN_MISMATCH)
    if transition.identity.run_id != snapshot.run_id:
        return rejected(ReferenceReason.RUN_MISMATCH)
    if transition.identity.coordinator_epoch != snapshot.coordinator_epoch:
        return rejected(ReferenceReason.STALE_EPOCH)

    table = _transition_table(transition)
    pair = (transition.from_state, transition.to_state)
    if pair not in table.get(transition.event_type, set()):
        return rejected(ReferenceReason.ILLEGAL_TRANSITION)

    updated = snapshot
    if transition.subject is TransitionSubject.RUN:
        if transition.identity.task_id is not None:
            return rejected(ReferenceReason.IDENTITY_MISMATCH)
        is_phase = transition.event_type in RUN_PHASE_TRANSITIONS
        current = snapshot.phase if is_phase else snapshot.status
        if current is not transition.from_state:
            return rejected(ReferenceReason.STATE_MISMATCH)
        if (
            transition.event_type is JournalEventType.RUN_ARCHIVED
            and transition.from_state is RuntimeState.RUNNING
            and snapshot.phase is not RuntimeState.COMPLETE
        ):
            return rejected(ReferenceReason.STATE_MISMATCH)
        if is_phase:
            if transition.event_type is JournalEventType.TASK_GRAPH_FROZEN:
                if any(task.state is not RuntimeState.PROPOSED for task in snapshot.tasks):
                    return rejected(ReferenceReason.STATE_MISMATCH)
                tasks = tuple(
                    replace(task, state=RuntimeState.APPROVED)
                    for task in snapshot.tasks
                )
                updated = replace(snapshot, phase=transition.to_state, tasks=tasks)
            else:
                updated = replace(snapshot, phase=transition.to_state)
        else:
            if transition.to_state in {
                RuntimeState.RUNNING,
                RuntimeState.PAUSED,
                RuntimeState.ARCHIVED,
            }:
                reason = None
            elif transition.to_state is RuntimeState.CANCELLED:
                reason = transition.reason_code or snapshot.run_reason_code
            else:
                reason = transition.reason_code
            updated = replace(
                snapshot,
                status=transition.to_state,
                run_reason_code=reason,
            )
    elif transition.subject is TransitionSubject.TASK:
        identity = transition.identity
        if identity.task_id is None or identity.attempt is not None:
            return rejected(ReferenceReason.IDENTITY_MISMATCH)
        task_index = _task_index(snapshot, identity.task_id)
        if task_index is None:
            return rejected(ReferenceReason.IDENTITY_MISMATCH)
        task = snapshot.tasks[task_index]
        if task.state is not transition.from_state:
            return rejected(ReferenceReason.STATE_MISMATCH)
        reason = (
            transition.reason_code
            if transition.to_state
            in {RuntimeState.BLOCKED, RuntimeState.INVALIDATED, RuntimeState.REVERTED}
            else None
        )
        replacement = replace(task, state=transition.to_state, reason_code=reason)
        updated = replace(
            snapshot,
            tasks=_replace_at(snapshot.tasks, task_index, replacement),
        )
    else:
        identity = transition.identity
        if (
            not identity.is_attempt
            or identity.task_id is None
            or identity.correlation_id is None
        ):
            return rejected(ReferenceReason.IDENTITY_MISMATCH)
        if _task_index(snapshot, identity.task_id) is None:
            return rejected(ReferenceReason.IDENTITY_MISMATCH)
        attempt_index = _attempt_index(snapshot, identity.task_id, identity.attempt)
        if attempt_index is None:
            if transition.from_state is not RuntimeState.PLANNED:
                return rejected(ReferenceReason.STALE_ATTEMPT)
            if any(
                attempt.task_id == identity.task_id
                and attempt.state not in TERMINAL_ATTEMPTS
                for attempt in snapshot.attempts
            ):
                return rejected(ReferenceReason.ACTIVE_ATTEMPT_EXISTS)
            previous = [
                attempt.attempt
                for attempt in snapshot.attempts
                if attempt.task_id == identity.task_id
            ]
            if previous and identity.attempt is not None:
                if identity.attempt <= max(previous):
                    return rejected(ReferenceReason.STALE_ATTEMPT)
            if identity.attempt is None:
                return rejected(ReferenceReason.IDENTITY_MISMATCH)
            attempt = ReferenceAttempt(
                task_id=identity.task_id,
                attempt=identity.attempt,
                correlation_id=identity.correlation_id,
                coordinator_epoch=identity.coordinator_epoch,
                state=transition.to_state,
                reason_code=transition.reason_code,
            )
            updated = replace(snapshot, attempts=(*snapshot.attempts, attempt))
        else:
            attempt = snapshot.attempts[attempt_index]
            if attempt.correlation_id != identity.correlation_id:
                return rejected(ReferenceReason.STALE_CORRELATION)
            if attempt.state is not transition.from_state:
                return rejected(ReferenceReason.STATE_MISMATCH)
            replacement = replace(
                attempt,
                state=transition.to_state,
                reason_code=transition.reason_code,
            )
            updated = replace(
                snapshot,
                attempts=_replace_at(snapshot.attempts, attempt_index, replacement),
            )

    return ReferenceResult(
        True,
        ReferenceReason.APPLIED,
        replace(
            updated,
            last_sequence=transition.sequence,
            last_event_id=transition.event_id,
            last_event_hash=transition.event_hash,
        ),
    )


def replay_transitions(
    initial: ReferenceSnapshot,
    transitions: Sequence[ReferenceTransition],
) -> ReferenceResult:
    snapshot = initial
    for transition in transitions:
        result = reduce_transition(snapshot, transition)
        if not result.accepted:
            return result
        snapshot = result.snapshot
    return ReferenceResult(True, ReferenceReason.APPLIED, snapshot)


def first_failure(
    initial: ReferenceSnapshot,
    transitions: Sequence[ReferenceTransition],
) -> SequenceFailure | None:
    snapshot = initial
    for index, transition in enumerate(transitions):
        result = reduce_transition(snapshot, transition)
        if not result.accepted:
            return SequenceFailure(index, result.reason, result.snapshot)
        snapshot = result.snapshot
    return None


def shrink_failure(
    initial: ReferenceSnapshot,
    transitions: Sequence[ReferenceTransition],
    expected_reason: ReferenceReason,
) -> tuple[ReferenceTransition, ...]:
    """Return a deterministic 1-minimal witness preserving the failure reason."""

    failure = first_failure(initial, transitions)
    if failure is None or failure.reason is not expected_reason:
        raise ValueError("sequence does not fail with the expected reason")
    candidate = list(transitions[: failure.index + 1])
    changed = True
    while changed:
        changed = False
        for index in range(len(candidate)):
            trial = candidate[:index] + candidate[index + 1 :]
            trial_failure = first_failure(initial, trial)
            if trial_failure is not None and trial_failure.reason is expected_reason:
                candidate = trial
                changed = True
                break
    return tuple(candidate)


def generate_legal_sequences(
    manifest: ExecutionManifest,
    *,
    seed: int = 0x5EED_1200,
) -> tuple[GeneratedSequence, ...]:
    """Generate deterministic complete runs for distinct dependency tie orders."""

    orders = _boundary_topological_orders(manifest)
    chosen = [orders[0]]
    if orders[-1] != orders[0]:
        chosen.append(orders[-1])
    generated: list[GeneratedSequence] = []
    for case_index, task_order in enumerate(chosen):
        builder = _SequenceBuilder(
            ReferenceSnapshot.initial(manifest),
            seed + case_index,
            f"legal-{case_index + 1}",
        )
        _emit_freeze(builder)
        _emit_status_tour(builder)
        for task_id in task_order:
            if task_id not in builder.snapshot.ready(manifest):
                raise AssertionError(f"generator selected a task that is not ready: {task_id}")
            _emit_task_completion(builder, task_id)
        builder.emit(
            JournalEventType.EXECUTION_COMPLETED,
            TransitionSubject.RUN,
            RuntimeState.EXECUTING,
            RuntimeState.INTEGRATION,
        )
        builder.emit(
            JournalEventType.INTEGRATION_VERIFIED,
            TransitionSubject.RUN,
            RuntimeState.INTEGRATION,
            RuntimeState.QUALITY_DOCS,
        )
        builder.emit(
            JournalEventType.QUALITY_DOCS_VERIFIED,
            TransitionSubject.RUN,
            RuntimeState.QUALITY_DOCS,
            RuntimeState.COMPLETE,
        )
        builder.emit(
            JournalEventType.RUN_ARCHIVED,
            TransitionSubject.RUN,
            RuntimeState.RUNNING,
            RuntimeState.ARCHIVED,
        )
        generated.append(
            GeneratedSequence(
                name=f"complete-order-{case_index + 1}",
                task_order=task_order,
                transitions=tuple(builder.transitions),
            )
        )
    return tuple(generated)


def generate_invalid_sequences(
    manifest: ExecutionManifest,
    *,
    seed: int = 0x5EED_1200,
) -> tuple[GeneratedFailure, ...]:
    """Generate stable witnesses for every identity and Journal-order guard."""

    initial = ReferenceSnapshot.initial(manifest)
    first_task = topological_order(manifest)[0]
    first = _candidate(
        initial,
        seed,
        "base-first",
        JournalEventType.RUN_INITIALIZED,
        TransitionSubject.RUN,
        RuntimeState.NONE,
        RuntimeState.PREFLIGHT,
    )
    applied_first = reduce_transition(initial, first).snapshot
    second = _candidate(
        applied_first,
        seed,
        "base-second",
        JournalEventType.PREFLIGHT_COMPLETED,
        TransitionSubject.RUN,
        RuntimeState.PREFLIGHT,
        RuntimeState.DISCOVERY,
    )
    reserve = _candidate(
        initial,
        seed,
        "reserve-one",
        JournalEventType.ATTEMPT_RESERVED,
        TransitionSubject.ATTEMPT,
        RuntimeState.PLANNED,
        RuntimeState.RESERVED,
        task_id=first_task,
        attempt=1,
        correlation_id="CORRELATION-ONE",
    )
    reserved = reduce_transition(initial, reserve).snapshot

    return (
        GeneratedFailure(
            "sequence-conflict",
            ReferenceReason.SEQUENCE_CONFLICT,
            (
                first,
                replace(
                    first,
                    event_id="EVENT-REF-CONFLICT",
                    event_hash=_digest("sequence-conflict"),
                ),
            ),
        ),
        GeneratedFailure(
            "stale-sequence",
            ReferenceReason.STALE_SEQUENCE,
            (first, second, first),
        ),
        GeneratedFailure(
            "sequence-gap",
            ReferenceReason.SEQUENCE_GAP,
            (
                replace(
                    first,
                    sequence=3,
                    event_id="EVENT-REF-GAP",
                    event_hash=_digest("sequence-gap"),
                ),
            ),
        ),
        GeneratedFailure(
            "hash-chain-mismatch",
            ReferenceReason.HASH_CHAIN_MISMATCH,
            (replace(first, previous_event_hash=_digest("wrong-previous")),),
        ),
        GeneratedFailure(
            "run-mismatch",
            ReferenceReason.RUN_MISMATCH,
            (
                replace(
                    first,
                    identity=replace(first.identity, run_id="WISH-WRONG-RUN"),
                ),
            ),
        ),
        GeneratedFailure(
            "stale-epoch",
            ReferenceReason.STALE_EPOCH,
            (
                replace(
                    first,
                    identity=replace(
                        first.identity,
                        coordinator_epoch=first.identity.coordinator_epoch + 1,
                    ),
                ),
            ),
        ),
        GeneratedFailure(
            "identity-mismatch",
            ReferenceReason.IDENTITY_MISMATCH,
            (
                replace(
                    first,
                    identity=replace(first.identity, task_id=first_task),
                ),
            ),
        ),
        GeneratedFailure(
            "illegal-transition",
            ReferenceReason.ILLEGAL_TRANSITION,
            (
                _candidate(
                    initial,
                    seed,
                    "illegal-transition",
                    JournalEventType.TASK_VERIFIED,
                    TransitionSubject.TASK,
                    RuntimeState.APPROVED,
                    RuntimeState.VERIFIED,
                    task_id=first_task,
                ),
            ),
        ),
        GeneratedFailure(
            "state-mismatch",
            ReferenceReason.STATE_MISMATCH,
            (
                _candidate(
                    initial,
                    seed,
                    "state-mismatch",
                    JournalEventType.PREFLIGHT_COMPLETED,
                    TransitionSubject.RUN,
                    RuntimeState.PREFLIGHT,
                    RuntimeState.DISCOVERY,
                ),
            ),
        ),
        GeneratedFailure(
            "active-attempt-exists",
            ReferenceReason.ACTIVE_ATTEMPT_EXISTS,
            (
                reserve,
                _candidate(
                    reserved,
                    seed,
                    "reserve-two",
                    JournalEventType.ATTEMPT_RESERVED,
                    TransitionSubject.ATTEMPT,
                    RuntimeState.PLANNED,
                    RuntimeState.RESERVED,
                    task_id=first_task,
                    attempt=2,
                    correlation_id="CORRELATION-TWO",
                ),
            ),
        ),
        GeneratedFailure(
            "stale-attempt",
            ReferenceReason.STALE_ATTEMPT,
            (
                _candidate(
                    initial,
                    seed,
                    "stale-attempt",
                    JournalEventType.DISPATCH_REQUESTED,
                    TransitionSubject.ATTEMPT,
                    RuntimeState.RESERVED,
                    RuntimeState.DISPATCH_REQUESTED,
                    task_id=first_task,
                    attempt=99,
                    correlation_id="CORRELATION-STALE",
                ),
            ),
        ),
        GeneratedFailure(
            "stale-correlation",
            ReferenceReason.STALE_CORRELATION,
            (
                reserve,
                _candidate(
                    reserved,
                    seed,
                    "stale-correlation",
                    JournalEventType.ATTEMPT_RELEASED,
                    TransitionSubject.ATTEMPT,
                    RuntimeState.RESERVED,
                    RuntimeState.TERMINATED,
                    task_id=first_task,
                    attempt=1,
                    correlation_id="CORRELATION-WRONG",
                ),
            ),
        ),
    )


def transition_to_primitive(transition: ReferenceTransition) -> dict[str, object]:
    return {
        "attempt": transition.identity.attempt,
        "correlation_id": transition.identity.correlation_id,
        "coordinator_epoch": transition.identity.coordinator_epoch,
        "event_hash": transition.event_hash,
        "event_id": transition.event_id,
        "event_type": transition.event_type.value,
        "from_state": transition.from_state.value,
        "previous_event_hash": transition.previous_event_hash,
        "reason_code": (
            None if transition.reason_code is None else transition.reason_code.value
        ),
        "run_id": transition.identity.run_id,
        "sequence": transition.sequence,
        "subject": transition.subject.value,
        "task_id": transition.identity.task_id,
        "to_state": transition.to_state.value,
    }


def transition_from_primitive(value: Mapping[str, object]) -> ReferenceTransition:
    reason = value["reason_code"]
    return ReferenceTransition(
        sequence=int(value["sequence"]),
        event_id=str(value["event_id"]),
        event_hash=str(value["event_hash"]),
        previous_event_hash=str(value["previous_event_hash"]),
        event_type=JournalEventType(str(value["event_type"])),
        subject=TransitionSubject(str(value["subject"])),
        from_state=RuntimeState(str(value["from_state"])),
        to_state=RuntimeState(str(value["to_state"])),
        identity=ReferenceIdentity(
            run_id=str(value["run_id"]),
            coordinator_epoch=int(value["coordinator_epoch"]),
            task_id=None if value["task_id"] is None else str(value["task_id"]),
            attempt=None if value["attempt"] is None else int(value["attempt"]),
            correlation_id=(
                None
                if value["correlation_id"] is None
                else str(value["correlation_id"])
            ),
        ),
        reason_code=None if reason is None else RuntimeReasonCode(str(reason)),
    )


def transitions_digest(transitions: Sequence[ReferenceTransition]) -> str:
    primitive = [transition_to_primitive(item) for item in transitions]
    raw = json.dumps(
        primitive,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class _SequenceBuilder:
    def __init__(self, snapshot: ReferenceSnapshot, seed: int, name: str) -> None:
        self.snapshot = snapshot
        self.seed = seed
        self.name = name
        self.transitions: list[ReferenceTransition] = []

    def emit(
        self,
        event_type: JournalEventType,
        subject: TransitionSubject,
        from_state: RuntimeState,
        to_state: RuntimeState,
        *,
        task_id: str | None = None,
        attempt: int | None = None,
        correlation_id: str | None = None,
        reason_code: RuntimeReasonCode | None = None,
    ) -> ReferenceTransition:
        item = _candidate(
            self.snapshot,
            self.seed,
            f"{self.name}-{len(self.transitions) + 1}",
            event_type,
            subject,
            from_state,
            to_state,
            task_id=task_id,
            attempt=attempt,
            correlation_id=correlation_id,
            reason_code=reason_code,
        )
        result = reduce_transition(self.snapshot, item)
        if not result.accepted:
            raise AssertionError(f"generated illegal transition: {result.reason.value}")
        self.transitions.append(item)
        self.snapshot = result.snapshot
        return item


def _candidate(
    snapshot: ReferenceSnapshot,
    seed: int,
    label: str,
    event_type: JournalEventType,
    subject: TransitionSubject,
    from_state: RuntimeState,
    to_state: RuntimeState,
    *,
    task_id: str | None = None,
    attempt: int | None = None,
    correlation_id: str | None = None,
    reason_code: RuntimeReasonCode | None = None,
) -> ReferenceTransition:
    sequence = snapshot.last_sequence + 1
    return ReferenceTransition(
        sequence=sequence,
        event_id=f"EVENT-REF-{sequence:06d}",
        event_hash=_digest(f"{seed}:{label}:{sequence}"),
        previous_event_hash=snapshot.last_event_hash,
        event_type=event_type,
        subject=subject,
        from_state=from_state,
        to_state=to_state,
        identity=ReferenceIdentity(
            snapshot.run_id,
            snapshot.coordinator_epoch,
            task_id,
            attempt,
            correlation_id,
        ),
        reason_code=reason_code,
    )


def _emit_freeze(builder: _SequenceBuilder) -> None:
    for event_type, from_state, to_state in (
        (JournalEventType.RUN_INITIALIZED, RuntimeState.NONE, RuntimeState.PREFLIGHT),
        (
            JournalEventType.PREFLIGHT_COMPLETED,
            RuntimeState.PREFLIGHT,
            RuntimeState.DISCOVERY,
        ),
        (
            JournalEventType.DISCOVERY_COMPLETED,
            RuntimeState.DISCOVERY,
            RuntimeState.GATE_A_PENDING,
        ),
        (
            JournalEventType.GATE_APPROVED,
            RuntimeState.GATE_A_PENDING,
            RuntimeState.TRELLIS_PREPARATION,
        ),
        (
            JournalEventType.TRELLIS_GRAPH_IMPORTED,
            RuntimeState.TRELLIS_PREPARATION,
            RuntimeState.GATE_B_PENDING,
        ),
        (
            JournalEventType.TASK_GRAPH_FROZEN,
            RuntimeState.GATE_B_PENDING,
            RuntimeState.EXECUTING,
        ),
    ):
        builder.emit(event_type, TransitionSubject.RUN, from_state, to_state)


def _emit_status_tour(builder: _SequenceBuilder) -> None:
    builder.emit(
        JournalEventType.PAUSE_REQUESTED,
        TransitionSubject.RUN,
        RuntimeState.RUNNING,
        RuntimeState.PAUSING,
        reason_code=RuntimeReasonCode.PAUSE_REQUESTED,
    )
    builder.emit(
        JournalEventType.RUN_PAUSED,
        TransitionSubject.RUN,
        RuntimeState.PAUSING,
        RuntimeState.PAUSED,
    )
    builder.emit(
        JournalEventType.RUN_RESUMED,
        TransitionSubject.RUN,
        RuntimeState.PAUSED,
        RuntimeState.RUNNING,
    )
    builder.emit(
        JournalEventType.RUN_BLOCKED,
        TransitionSubject.RUN,
        RuntimeState.RUNNING,
        RuntimeState.BLOCKED,
        reason_code=RuntimeReasonCode.INVARIANT_VIOLATION,
    )
    builder.emit(
        JournalEventType.RUN_ESCALATED,
        TransitionSubject.RUN,
        RuntimeState.BLOCKED,
        RuntimeState.ESCALATED,
        reason_code=RuntimeReasonCode.INVARIANT_VIOLATION,
    )
    builder.emit(
        JournalEventType.RUN_RESUMED,
        TransitionSubject.RUN,
        RuntimeState.ESCALATED,
        RuntimeState.RUNNING,
    )


def _emit_task_completion(builder: _SequenceBuilder, task_id: str) -> None:
    correlation_id = f"CORRELATION-{task_id}-001"
    steps = (
        (
            JournalEventType.TASK_READY,
            TransitionSubject.TASK,
            RuntimeState.APPROVED,
            RuntimeState.READY,
            None,
            None,
        ),
        (
            JournalEventType.LEASE_ACQUIRED,
            TransitionSubject.TASK,
            RuntimeState.READY,
            RuntimeState.LEASED,
            None,
            None,
        ),
        (
            JournalEventType.ATTEMPT_RESERVED,
            TransitionSubject.ATTEMPT,
            RuntimeState.PLANNED,
            RuntimeState.RESERVED,
            1,
            correlation_id,
        ),
        (
            JournalEventType.DISPATCH_REQUESTED,
            TransitionSubject.ATTEMPT,
            RuntimeState.RESERVED,
            RuntimeState.DISPATCH_REQUESTED,
            1,
            correlation_id,
        ),
        (
            JournalEventType.DISPATCH_OBSERVED,
            TransitionSubject.ATTEMPT,
            RuntimeState.DISPATCH_REQUESTED,
            RuntimeState.RUNNING,
            1,
            correlation_id,
        ),
        (
            JournalEventType.DISPATCH_OBSERVED,
            TransitionSubject.TASK,
            RuntimeState.LEASED,
            RuntimeState.DISPATCHED,
            None,
            None,
        ),
        (
            JournalEventType.ATTEMPT_SUCCEEDED,
            TransitionSubject.ATTEMPT,
            RuntimeState.RUNNING,
            RuntimeState.SUCCEEDED,
            1,
            correlation_id,
        ),
        (
            JournalEventType.PR_OBSERVED,
            TransitionSubject.TASK,
            RuntimeState.DISPATCHED,
            RuntimeState.PR_OPEN,
            None,
            None,
        ),
        (
            JournalEventType.MERGE_OBSERVED,
            TransitionSubject.TASK,
            RuntimeState.PR_OPEN,
            RuntimeState.MERGED,
            None,
            None,
        ),
        (
            JournalEventType.TASK_VERIFIED,
            TransitionSubject.TASK,
            RuntimeState.MERGED,
            RuntimeState.VERIFIED,
            None,
            None,
        ),
    )
    for event_type, subject, from_state, to_state, attempt, correlation in steps:
        builder.emit(
            event_type,
            subject,
            from_state,
            to_state,
            task_id=task_id,
            attempt=attempt,
            correlation_id=correlation,
        )


def _transition_table(
    transition: ReferenceTransition,
) -> Mapping[JournalEventType, set[tuple[RuntimeState, RuntimeState]]]:
    if transition.subject is TransitionSubject.RUN:
        if transition.event_type in RUN_PHASE_TRANSITIONS:
            return RUN_PHASE_TRANSITIONS
        return RUN_STATUS_TRANSITIONS
    if transition.subject is TransitionSubject.TASK:
        return TASK_TRANSITIONS
    return ATTEMPT_TRANSITIONS


def _boundary_topological_orders(
    manifest: ExecutionManifest,
) -> tuple[tuple[str, ...], ...]:
    dependencies = {task.id: frozenset(task.depends_on) for task in manifest.tasks}

    def choose(*, reverse: bool) -> tuple[str, ...]:
        prefix: list[str] = []
        remaining = set(dependencies)
        while remaining:
            completed = frozenset(prefix)
            ready = sorted(
                (
                    task_id
                    for task_id in remaining
                    if dependencies[task_id].issubset(completed)
                ),
                reverse=reverse,
            )
            if not ready:
                raise ValueError("dependency_cycle")
            selected = ready[0]
            prefix.append(selected)
            remaining.remove(selected)
        return tuple(prefix)

    first = choose(reverse=False)
    last = choose(reverse=True)
    return (first,) if first == last else (first, last)


def _task_index(snapshot: ReferenceSnapshot, task_id: str) -> int | None:
    for index, task in enumerate(snapshot.tasks):
        if task.task_id == task_id:
            return index
    return None


def _attempt_index(
    snapshot: ReferenceSnapshot,
    task_id: str,
    attempt_number: int | None,
) -> int | None:
    for index, attempt in enumerate(snapshot.attempts):
        if attempt.task_id == task_id and attempt.attempt == attempt_number:
            return index
    return None


def _replace_at(values: tuple[_T, ...], index: int, value: _T) -> tuple[_T, ...]:
    return (*values[:index], value, *values[index + 1 :])


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "ATTEMPT_TRANSITIONS",
    "COMPLETE",
    "GENESIS_HASH",
    "GeneratedFailure",
    "GeneratedSequence",
    "ReferenceAttempt",
    "ReferenceIdentity",
    "ReferenceReason",
    "ReferenceResult",
    "ReferenceSnapshot",
    "ReferenceTask",
    "ReferenceTransition",
    "SequenceFailure",
    "first_failure",
    "generate_invalid_sequences",
    "generate_legal_sequences",
    "ready_tasks",
    "reduce_transition",
    "replay_transitions",
    "shrink_failure",
    "topological_order",
    "transition_from_primitive",
    "transition_to_primitive",
    "transitions_digest",
]
