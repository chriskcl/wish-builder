"""Pure replay-stable run, task, and attempt transition kernel."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TypeVar

from wish_builder.contracts.models import HASH_RE, ID_RE
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeasePayload,
    RuntimeReasonCode,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)

from .dag import TaskDag


GENESIS_HASH = "sha256:" + "0" * 64
_T = TypeVar("_T")


class ApplyReason(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    SEQUENCE_CONFLICT = "sequence_conflict"
    STALE_SEQUENCE = "stale_sequence"
    SEQUENCE_GAP = "sequence_gap"
    HASH_CHAIN_MISMATCH = "hash_chain_mismatch"
    RUN_MISMATCH = "run_mismatch"
    STALE_EPOCH = "stale_epoch"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED_EVENT = "unsupported_event"
    ILLEGAL_TRANSITION = "illegal_transition"
    STATE_MISMATCH = "state_mismatch"
    ACTIVE_ATTEMPT_EXISTS = "active_attempt_exists"
    STALE_ATTEMPT = "stale_attempt"
    STALE_CORRELATION = "stale_correlation"


_RUN_PHASE_TRANSITIONS = {
    JournalEventType.RUN_INITIALIZED: {(RuntimeState.NONE, RuntimeState.PREFLIGHT)},
    JournalEventType.PREFLIGHT_COMPLETED: {
        (RuntimeState.PREFLIGHT, RuntimeState.DISCOVERY)
    },
    JournalEventType.DISCOVERY_COMPLETED: {
        (RuntimeState.DISCOVERY, RuntimeState.GATE_A_PENDING)
    },
    JournalEventType.GATE_APPROVED: {
        (RuntimeState.GATE_A_PENDING, RuntimeState.TRELLIS_PREPARATION),
        # Legacy Journal replay path.
        (RuntimeState.GATE_A_PENDING, RuntimeState.DECOMPOSITION)
    },
    JournalEventType.TRELLIS_GRAPH_IMPORTED: {
        (RuntimeState.TRELLIS_PREPARATION, RuntimeState.GATE_B_PENDING)
    },
    JournalEventType.DECOMPOSITION_COMPLETED: {
        # Legacy Journal replay path.
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

_NONTERMINAL_RUN_STATUSES = frozenset(
    {
        RuntimeState.RUNNING,
        RuntimeState.PAUSING,
        RuntimeState.PAUSED,
        RuntimeState.BLOCKED,
        RuntimeState.ESCALATED,
        RuntimeState.CANCELLING,
    }
)
_RUN_STATUS_TRANSITIONS = {
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

_TASK_TRANSITIONS = {
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

_ATTEMPT_TRANSITIONS = {
    JournalEventType.ATTEMPT_RESERVED: {
        (RuntimeState.PLANNED, RuntimeState.RESERVED),
        (RuntimeState.TERMINATED, RuntimeState.RESERVED),
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

_TERMINAL_ATTEMPT_STATES = frozenset(
    {
        RuntimeState.SUCCEEDED,
        RuntimeState.FAILED,
        RuntimeState.TERMINATED,
        RuntimeState.OUTCOME_UNKNOWN,
    }
)
_REASON_REQUIRED_STATES = frozenset(
    {
        RuntimeState.PAUSING,
        RuntimeState.BLOCKED,
        RuntimeState.ESCALATED,
        RuntimeState.CANCELLING,
        RuntimeState.FAILED,
        RuntimeState.INVALIDATED,
        RuntimeState.OUTCOME_UNKNOWN,
    }
)
_RUN_PHASE_STATES = frozenset(
    {
        RuntimeState.NONE,
        RuntimeState.PREFLIGHT,
        RuntimeState.DISCOVERY,
        RuntimeState.GATE_A_PENDING,
        RuntimeState.TRELLIS_PREPARATION,
        RuntimeState.DECOMPOSITION,
        RuntimeState.GATE_B_PENDING,
        RuntimeState.EXECUTING,
        RuntimeState.INTEGRATION,
        RuntimeState.QUALITY_DOCS,
        RuntimeState.COMPLETE,
    }
)
_RUN_STATUS_STATES = frozenset(
    {
        RuntimeState.RUNNING,
        RuntimeState.PAUSING,
        RuntimeState.PAUSED,
        RuntimeState.BLOCKED,
        RuntimeState.ESCALATED,
        RuntimeState.CANCELLING,
        RuntimeState.CANCELLED,
        RuntimeState.FAILED,
        RuntimeState.ARCHIVED,
    }
)
_TASK_STATES = frozenset(
    {
        RuntimeState.PROPOSED,
        RuntimeState.APPROVED,
        RuntimeState.READY,
        RuntimeState.LEASED,
        RuntimeState.DISPATCHED,
        RuntimeState.PR_OPEN,
        RuntimeState.MERGED,
        RuntimeState.STAGED,
        RuntimeState.PROMOTED,
        RuntimeState.VERIFIED,
        RuntimeState.ARCHIVED,
        RuntimeState.BLOCKED,
        RuntimeState.REVERTED,
        RuntimeState.INVALIDATED,
    }
)
_ATTEMPT_STATES = frozenset(
    {
        RuntimeState.PLANNED,
        RuntimeState.RESERVED,
        RuntimeState.DISPATCH_REQUESTED,
        RuntimeState.RUNNING,
        RuntimeState.CANCEL_REQUESTED,
        RuntimeState.SUCCEEDED,
        RuntimeState.FAILED,
        RuntimeState.TERMINATED,
        RuntimeState.OUTCOME_UNKNOWN,
    }
)


class StateTransitionError(ValueError):
    def __init__(self, reason: ApplyReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class StateTransition:
    sequence: int
    event_id: str
    event_hash: str
    previous_event_hash: str
    event_type: JournalEventType
    subject: TransitionSubject
    from_state: RuntimeState
    to_state: RuntimeState
    identity: ExecutionIdentity
    reason_code: RuntimeReasonCode | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence <= 0:
            raise ValueError("sequence must be positive")
        if type(self.event_id) is not str or not ID_RE.fullmatch(self.event_id):
            raise ValueError("event_id must be a stable uppercase ID")
        for value, name in (
            (self.event_hash, "event_hash"),
            (self.previous_event_hash, "previous_event_hash"),
        ):
            if type(value) is not str or not HASH_RE.fullmatch(value):
                raise ValueError(f"{name} must be a full sha256 reference")
        if type(self.event_type) is not JournalEventType:
            raise TypeError("event_type must be a JournalEventType")
        if type(self.subject) is not TransitionSubject:
            raise TypeError("subject must be a TransitionSubject")
        if type(self.from_state) is not RuntimeState or type(self.to_state) is not RuntimeState:
            raise TypeError("states must be RuntimeState values")
        if type(self.identity) is not ExecutionIdentity:
            raise TypeError("identity must be an ExecutionIdentity")
        if self.reason_code is not None and type(self.reason_code) is not RuntimeReasonCode:
            raise TypeError("reason_code must be a RuntimeReasonCode or null")
        if self.to_state in _REASON_REQUIRED_STATES and self.reason_code is None:
            raise ValueError("blocking or failure transitions require a reason_code")

    @classmethod
    def from_journal_event(cls, event: JournalEvent) -> StateTransition:
        if type(event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent")
        if type(event.payload) is not TransitionPayload:
            raise StateTransitionError(ApplyReason.UNSUPPORTED_EVENT)
        return cls(
            sequence=event.sequence,
            event_id=event.event_id,
            event_hash=event.event_hash,
            previous_event_hash=event.previous_event_hash,
            event_type=event.event_type,
            subject=event.payload.subject,
            from_state=event.payload.from_state,
            to_state=event.payload.to_state,
            identity=event.identity,
            reason_code=event.reason_code,
        )


@dataclass(frozen=True, slots=True)
class TaskProjection:
    task_id: str
    state: RuntimeState = RuntimeState.PROPOSED
    reason_code: RuntimeReasonCode | None = None

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not ID_RE.fullmatch(self.task_id):
            raise ValueError("task_id must be a stable uppercase ID")
        if type(self.state) is not RuntimeState or self.state not in _TASK_STATES:
            raise ValueError("state must be a task RuntimeState")
        if self.reason_code is not None and type(self.reason_code) is not RuntimeReasonCode:
            raise TypeError("reason_code must be a RuntimeReasonCode or null")
        if self.state in {RuntimeState.BLOCKED, RuntimeState.INVALIDATED} and self.reason_code is None:
            raise ValueError("blocked or invalidated tasks require a reason_code")
        if self.state not in {
            RuntimeState.BLOCKED,
            RuntimeState.INVALIDATED,
            RuntimeState.REVERTED,
        } and self.reason_code is not None:
            raise ValueError("this task state cannot retain a reason_code")


@dataclass(frozen=True, slots=True)
class AttemptProjection:
    task_id: str
    attempt: int
    correlation_id: str
    coordinator_epoch: int
    state: RuntimeState
    reason_code: RuntimeReasonCode | None = None

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not ID_RE.fullmatch(self.task_id):
            raise ValueError("task_id must be a stable uppercase ID")
        if type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("attempt must be positive")
        if type(self.correlation_id) is not str or not ID_RE.fullmatch(
            self.correlation_id
        ):
            raise ValueError("correlation_id must be a stable uppercase ID")
        if type(self.coordinator_epoch) is not int or self.coordinator_epoch <= 0:
            raise ValueError("coordinator_epoch must be positive")
        if type(self.state) is not RuntimeState or self.state not in _ATTEMPT_STATES:
            raise ValueError("state must be an attempt RuntimeState")
        if self.reason_code is not None and type(self.reason_code) is not RuntimeReasonCode:
            raise TypeError("reason_code must be a RuntimeReasonCode or null")
        if self.state in {RuntimeState.FAILED, RuntimeState.OUTCOME_UNKNOWN}:
            if self.reason_code is None:
                raise ValueError("failed or unknown attempts require a reason_code")
        elif self.reason_code is not None and self.state not in {
            RuntimeState.CANCEL_REQUESTED,
            RuntimeState.TERMINATED,
        }:
            raise ValueError("this attempt state cannot retain a reason_code")


@dataclass(frozen=True, slots=True)
class KernelSnapshot:
    run_id: str
    coordinator_epoch: int
    phase: RuntimeState
    status: RuntimeState
    run_reason_code: RuntimeReasonCode | None
    tasks: tuple[TaskProjection, ...]
    attempts: tuple[AttemptProjection, ...]
    last_sequence: int
    last_event_id: str | None
    last_event_hash: str

    def __post_init__(self) -> None:
        identity = ExecutionIdentity(self.run_id, self.coordinator_epoch)
        if identity.run_id != self.run_id or identity.coordinator_epoch != self.coordinator_epoch:
            raise ValueError("run identity is not canonical")
        if type(self.phase) is not RuntimeState or self.phase not in _RUN_PHASE_STATES:
            raise ValueError("phase must be a run-phase RuntimeState")
        if type(self.status) is not RuntimeState or self.status not in _RUN_STATUS_STATES:
            raise ValueError("status must be a run-status RuntimeState")
        if self.run_reason_code is not None and type(self.run_reason_code) is not RuntimeReasonCode:
            raise TypeError("run_reason_code must be a RuntimeReasonCode or null")
        if self.status in {
            RuntimeState.PAUSING,
            RuntimeState.BLOCKED,
            RuntimeState.ESCALATED,
            RuntimeState.CANCELLING,
            RuntimeState.CANCELLED,
            RuntimeState.FAILED,
        } and self.run_reason_code is None:
            raise ValueError("this run status requires a reason_code")
        if self.status in {
            RuntimeState.RUNNING,
            RuntimeState.PAUSED,
            RuntimeState.ARCHIVED,
        } and self.run_reason_code is not None:
            raise ValueError("this run status cannot retain a reason_code")
        if type(self.tasks) is not tuple or not all(
            type(task) is TaskProjection for task in self.tasks
        ):
            raise TypeError("tasks must be a tuple of TaskProjection values")
        task_ids = tuple(task.task_id for task in self.tasks)
        if not task_ids or len(set(task_ids)) != len(task_ids):
            raise ValueError("task projections must be non-empty and unique")
        if type(self.attempts) is not tuple or not all(
            type(attempt) is AttemptProjection for attempt in self.attempts
        ):
            raise TypeError("attempts must be a tuple of AttemptProjection values")
        attempt_ids = tuple((attempt.task_id, attempt.attempt) for attempt in self.attempts)
        if len(set(attempt_ids)) != len(attempt_ids):
            raise ValueError("attempt projections must be unique")
        if any(attempt.task_id not in task_ids for attempt in self.attempts):
            raise ValueError("attempt projection references an unknown task")
        if type(self.last_sequence) is not int or self.last_sequence < 0:
            raise ValueError("last_sequence must be non-negative")
        if type(self.last_event_hash) is not str or not HASH_RE.fullmatch(
            self.last_event_hash
        ):
            raise ValueError("last_event_hash must be a full sha256 reference")
        if self.last_sequence == 0:
            if self.last_event_id is not None or self.last_event_hash != GENESIS_HASH:
                raise ValueError("an empty projection must use the genesis position")
        elif type(self.last_event_id) is not str or not ID_RE.fullmatch(
            self.last_event_id
        ):
            raise ValueError("a non-empty projection requires a stable last_event_id")

    @classmethod
    def initial(
        cls, run_id: str, coordinator_epoch: int, dag: TaskDag
    ) -> KernelSnapshot:
        if type(dag) is not TaskDag:
            raise TypeError("dag must be a TaskDag")
        identity = ExecutionIdentity(run_id, coordinator_epoch)
        return cls(
            run_id=identity.run_id,
            coordinator_epoch=identity.coordinator_epoch,
            phase=RuntimeState.NONE,
            status=RuntimeState.RUNNING,
            run_reason_code=None,
            tasks=tuple(TaskProjection(task_id) for task_id in dag.topological_order),
            attempts=(),
            last_sequence=0,
            last_event_id=None,
            last_event_hash=GENESIS_HASH,
        )

    def task_states(self) -> dict[str, RuntimeState]:
        return {task.task_id: task.state for task in self.tasks}

    def ready(self, dag: TaskDag) -> tuple[str, ...]:
        if self.phase is not RuntimeState.EXECUTING or self.status is not RuntimeState.RUNNING:
            return ()
        states = self.task_states()
        return dag.ready(states, active_task_ids=dag.active_task_ids(states))


@dataclass(frozen=True, slots=True)
class ApplyResult:
    accepted: bool
    reason: ApplyReason
    snapshot: KernelSnapshot

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a bool")
        if type(self.reason) is not ApplyReason:
            raise TypeError("reason must be an ApplyReason")
        if type(self.snapshot) is not KernelSnapshot:
            raise TypeError("snapshot must be a KernelSnapshot")
        expected = self.reason in {ApplyReason.APPLIED, ApplyReason.IDEMPOTENT_REPLAY}
        if self.accepted != expected:
            raise ValueError("accepted is inconsistent with the apply reason")


def validate_transition(transition: StateTransition) -> None:
    if type(transition) is not StateTransition:
        raise TypeError("transition must be a StateTransition")
    table = (
        _RUN_PHASE_TRANSITIONS
        if transition.subject is TransitionSubject.RUN
        and transition.event_type in _RUN_PHASE_TRANSITIONS
        else _RUN_STATUS_TRANSITIONS
        if transition.subject is TransitionSubject.RUN
        else _TASK_TRANSITIONS
        if transition.subject is TransitionSubject.TASK
        else _ATTEMPT_TRANSITIONS
    )
    if (transition.from_state, transition.to_state) not in table.get(
        transition.event_type, set()
    ):
        raise StateTransitionError(ApplyReason.ILLEGAL_TRANSITION)


def apply_transition(
    snapshot: KernelSnapshot, transition: StateTransition
) -> ApplyResult:
    if type(snapshot) is not KernelSnapshot:
        raise TypeError("snapshot must be a KernelSnapshot")
    if type(transition) is not StateTransition:
        raise TypeError("transition must be a StateTransition")
    def rejected(reason: ApplyReason) -> ApplyResult:
        return ApplyResult(False, reason, snapshot)
    if transition.sequence == snapshot.last_sequence:
        if (
            transition.event_id == snapshot.last_event_id
            and transition.event_hash == snapshot.last_event_hash
        ):
            return ApplyResult(True, ApplyReason.IDEMPOTENT_REPLAY, snapshot)
        return rejected(ApplyReason.SEQUENCE_CONFLICT)
    if transition.sequence < snapshot.last_sequence:
        return rejected(ApplyReason.STALE_SEQUENCE)
    if transition.sequence != snapshot.last_sequence + 1:
        return rejected(ApplyReason.SEQUENCE_GAP)
    if transition.previous_event_hash != snapshot.last_event_hash:
        return rejected(ApplyReason.HASH_CHAIN_MISMATCH)
    if transition.identity.run_id != snapshot.run_id:
        return rejected(ApplyReason.RUN_MISMATCH)
    if transition.identity.coordinator_epoch != snapshot.coordinator_epoch:
        return rejected(ApplyReason.STALE_EPOCH)
    try:
        validate_transition(transition)
    except StateTransitionError as exc:
        return rejected(exc.reason)

    updated = snapshot
    if transition.subject is TransitionSubject.RUN:
        if transition.identity.task_id is not None:
            return rejected(ApplyReason.IDENTITY_MISMATCH)
        current = (
            snapshot.phase
            if transition.event_type in _RUN_PHASE_TRANSITIONS
            else snapshot.status
        )
        if current is not transition.from_state:
            return rejected(ApplyReason.STATE_MISMATCH)
        if (
            transition.event_type is JournalEventType.RUN_ARCHIVED
            and transition.from_state is RuntimeState.RUNNING
            and snapshot.phase is not RuntimeState.COMPLETE
        ):
            return rejected(ApplyReason.STATE_MISMATCH)
        if transition.event_type in _RUN_PHASE_TRANSITIONS:
            updated = replace(snapshot, phase=transition.to_state)
            if transition.event_type is JournalEventType.TASK_GRAPH_FROZEN:
                if any(task.state is not RuntimeState.PROPOSED for task in snapshot.tasks):
                    return rejected(ApplyReason.STATE_MISMATCH)
                updated = replace(
                    updated,
                    tasks=tuple(
                        replace(task, state=RuntimeState.APPROVED)
                        for task in snapshot.tasks
                    ),
                )
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
        if transition.identity.task_id is None or transition.identity.attempt is not None:
            return rejected(ApplyReason.IDENTITY_MISMATCH)
        task_index = _task_index(snapshot.tasks, transition.identity.task_id)
        if task_index is None:
            return rejected(ApplyReason.IDENTITY_MISMATCH)
        task = snapshot.tasks[task_index]
        if task.state is not transition.from_state:
            return rejected(ApplyReason.STATE_MISMATCH)
        replacement = replace(
            task,
            state=transition.to_state,
            reason_code=(
                transition.reason_code
                if transition.to_state
                in {RuntimeState.BLOCKED, RuntimeState.INVALIDATED, RuntimeState.REVERTED}
                else None
            ),
        )
        updated = replace(snapshot, tasks=_replace_at(snapshot.tasks, task_index, replacement))
    else:
        if (
            not transition.identity.is_attempt
            or transition.identity.task_id is None
            or transition.identity.correlation_id is None
        ):
            return rejected(ApplyReason.IDENTITY_MISMATCH)
        task_index = _task_index(snapshot.tasks, transition.identity.task_id)
        if task_index is None:
            return rejected(ApplyReason.IDENTITY_MISMATCH)
        attempt_index = _attempt_index(
            snapshot.attempts,
            transition.identity.task_id,
            transition.identity.attempt,
        )
        if attempt_index is None:
            if transition.from_state is not RuntimeState.PLANNED:
                return rejected(ApplyReason.STALE_ATTEMPT)
            if any(
                attempt.task_id == transition.identity.task_id
                and attempt.state not in _TERMINAL_ATTEMPT_STATES
                for attempt in snapshot.attempts
            ):
                return rejected(ApplyReason.ACTIVE_ATTEMPT_EXISTS)
            previous_attempts = [
                attempt.attempt
                for attempt in snapshot.attempts
                if attempt.task_id == transition.identity.task_id
            ]
            if previous_attempts and transition.identity.attempt <= max(previous_attempts):
                return rejected(ApplyReason.STALE_ATTEMPT)
            attempt = AttemptProjection(
                transition.identity.task_id,
                transition.identity.attempt,
                transition.identity.correlation_id,
                transition.identity.coordinator_epoch,
                transition.to_state,
                transition.reason_code,
            )
            updated = replace(snapshot, attempts=(*snapshot.attempts, attempt))
        else:
            attempt = snapshot.attempts[attempt_index]
            if attempt.correlation_id != transition.identity.correlation_id and not (
                transition.event_type is JournalEventType.ATTEMPT_RESERVED
                and transition.from_state is RuntimeState.TERMINATED
                and transition.to_state is RuntimeState.RESERVED
            ):
                return rejected(ApplyReason.STALE_CORRELATION)
            reclaiming = (
                transition.event_type is JournalEventType.ATTEMPT_RESERVED
                and transition.from_state is RuntimeState.TERMINATED
                and transition.to_state is RuntimeState.RESERVED
            )
            if (
                reclaiming
                and transition.identity.coordinator_epoch <= attempt.coordinator_epoch
            ):
                return rejected(ApplyReason.STALE_EPOCH)
            if attempt.state is not transition.from_state:
                return rejected(ApplyReason.STATE_MISMATCH)
            replacement = replace(
                attempt,
                correlation_id=(
                    transition.identity.correlation_id
                    if reclaiming
                    else attempt.correlation_id
                ),
                coordinator_epoch=(
                    transition.identity.coordinator_epoch
                    if reclaiming
                    else attempt.coordinator_epoch
                ),
                state=transition.to_state,
                reason_code=transition.reason_code,
            )
            updated = replace(
                snapshot,
                attempts=_replace_at(snapshot.attempts, attempt_index, replacement),
            )

    updated = replace(
        updated,
        last_sequence=transition.sequence,
        last_event_id=transition.event_id,
        last_event_hash=transition.event_hash,
    )
    return ApplyResult(True, ApplyReason.APPLIED, updated)


def apply_journal_event(snapshot: KernelSnapshot, event: JournalEvent) -> ApplyResult:
    """Fold one verified Journal event, including atomic dispatch side effects."""

    if type(snapshot) is not KernelSnapshot:
        raise TypeError("snapshot must be a KernelSnapshot")
    if type(event) is not JournalEvent:
        raise TypeError("event must be a JournalEvent")
    if type(event.payload) is TransitionPayload:
        return apply_transition(snapshot, StateTransition.from_journal_event(event))

    def rejected(reason: ApplyReason) -> ApplyResult:
        return ApplyResult(False, reason, snapshot)

    if event.sequence == snapshot.last_sequence:
        if event.event_id == snapshot.last_event_id and event.event_hash == snapshot.last_event_hash:
            return ApplyResult(True, ApplyReason.IDEMPOTENT_REPLAY, snapshot)
        return rejected(ApplyReason.SEQUENCE_CONFLICT)
    if event.sequence < snapshot.last_sequence:
        return rejected(ApplyReason.STALE_SEQUENCE)
    if event.sequence != snapshot.last_sequence + 1:
        return rejected(ApplyReason.SEQUENCE_GAP)
    if event.previous_event_hash != snapshot.last_event_hash:
        return rejected(ApplyReason.HASH_CHAIN_MISMATCH)
    if event.identity.run_id != snapshot.run_id:
        return rejected(ApplyReason.RUN_MISMATCH)
    if type(event.payload) is LeasePayload:
        if event.event_type not in {
            JournalEventType.LEASE_ACQUIRED,
            JournalEventType.LEASE_RENEWED,
            JournalEventType.LEASE_RELEASED,
            JournalEventType.LEASE_LOST,
        }:
            return rejected(ApplyReason.UNSUPPORTED_EVENT)
        if event.event_type is JournalEventType.LEASE_ACQUIRED:
            if event.identity.coordinator_epoch < snapshot.coordinator_epoch:
                return rejected(ApplyReason.STALE_EPOCH)
        elif event.identity.coordinator_epoch != snapshot.coordinator_epoch:
            return rejected(ApplyReason.STALE_EPOCH)
        return ApplyResult(
            True,
            ApplyReason.APPLIED,
            replace(
                snapshot,
                coordinator_epoch=event.payload.fencing_token,
                last_sequence=event.sequence,
                last_event_id=event.event_id,
                last_event_hash=event.event_hash,
            ),
        )
    if event.identity.coordinator_epoch != snapshot.coordinator_epoch:
        return rejected(ApplyReason.STALE_EPOCH)

    if (
        event.event_type is JournalEventType.DISPATCH_REQUESTED
        and type(event.payload) is EffectRequestPayload
    ):
        payload = event.payload
        if (
            payload.operation is not EffectOperation.WORKER_DISPATCH
            or payload.adapter is not AdapterKind.TASK
            or payload.object_type is not EffectObjectType.WORKER
            or payload.expected_sequence != snapshot.last_sequence
        ):
            return rejected(ApplyReason.UNSUPPORTED_EVENT)
        transition = StateTransition(
            event.sequence,
            event.event_id,
            event.event_hash,
            event.previous_event_hash,
            event.event_type,
            TransitionSubject.ATTEMPT,
            RuntimeState.RESERVED,
            RuntimeState.DISPATCH_REQUESTED,
            event.identity,
            event.reason_code,
        )
        return apply_transition(snapshot, transition)

    if (
        event.event_type is JournalEventType.PROMOTION_OBSERVED
        and type(event.payload) is EffectObservationPayload
        and event.payload.adapter is AdapterKind.GIT
        and event.payload.receipt.operation is EffectOperation.RESULT_PROMOTION
        and event.payload.receipt.status is EffectStatus.APPLIED
    ):
        identity = event.identity
        if not identity.is_attempt or identity.task_id is None:
            return rejected(ApplyReason.IDENTITY_MISMATCH)
        if event.payload.receipt.identity != identity:
            return rejected(ApplyReason.IDENTITY_MISMATCH)
        task_index = _task_index(snapshot.tasks, identity.task_id)
        if task_index is None:
            return rejected(ApplyReason.IDENTITY_MISMATCH)
        task = snapshot.tasks[task_index]
        if task.state is not RuntimeState.STAGED:
            return rejected(ApplyReason.STATE_MISMATCH)
        updated = replace(
            snapshot,
            tasks=_replace_at(
                snapshot.tasks,
                task_index,
                replace(task, state=RuntimeState.PROMOTED),
            ),
            last_sequence=event.sequence,
            last_event_id=event.event_id,
            last_event_hash=event.event_hash,
        )
        return ApplyResult(True, ApplyReason.APPLIED, updated)

    if (
        event.event_type is not JournalEventType.DISPATCH_OBSERVED
        or type(event.payload) is not EffectObservationPayload
        or event.payload.adapter is not AdapterKind.TASK
        or event.payload.receipt.operation is not EffectOperation.WORKER_DISPATCH
        or event.payload.receipt.status is not EffectStatus.APPLIED
    ):
        return rejected(ApplyReason.UNSUPPORTED_EVENT)
    identity = event.identity
    receipt_identity = event.payload.receipt.identity
    if not identity.is_attempt or identity.task_id is None or identity.correlation_id is None:
        return rejected(ApplyReason.IDENTITY_MISMATCH)
    if (
        receipt_identity.run_id != identity.run_id
        or receipt_identity.task_id != identity.task_id
        or receipt_identity.attempt != identity.attempt
        or receipt_identity.correlation_id != identity.correlation_id
        or receipt_identity.coordinator_epoch > identity.coordinator_epoch
    ):
        return rejected(ApplyReason.IDENTITY_MISMATCH)
    task_index = _task_index(snapshot.tasks, receipt_identity.task_id)
    attempt_index = _attempt_index(
        snapshot.attempts,
        receipt_identity.task_id,
        receipt_identity.attempt,
    )
    if task_index is None or attempt_index is None:
        return rejected(ApplyReason.IDENTITY_MISMATCH)
    task = snapshot.tasks[task_index]
    attempt = snapshot.attempts[attempt_index]
    if (
        attempt.correlation_id != receipt_identity.correlation_id
        or attempt.coordinator_epoch != receipt_identity.coordinator_epoch
    ):
        return rejected(ApplyReason.STALE_CORRELATION)
    if task.state is not RuntimeState.LEASED or attempt.state is not RuntimeState.DISPATCH_REQUESTED:
        return rejected(ApplyReason.STATE_MISMATCH)

    updated_task = replace(task, state=RuntimeState.DISPATCHED)
    updated_attempt = replace(attempt, state=RuntimeState.RUNNING)
    updated = replace(
        snapshot,
        tasks=_replace_at(snapshot.tasks, task_index, updated_task),
        attempts=_replace_at(snapshot.attempts, attempt_index, updated_attempt),
        last_sequence=event.sequence,
        last_event_id=event.event_id,
        last_event_hash=event.event_hash,
    )
    return ApplyResult(True, ApplyReason.APPLIED, updated)


def replay(
    initial: KernelSnapshot, transitions: tuple[StateTransition, ...]
) -> ApplyResult:
    if type(initial) is not KernelSnapshot:
        raise TypeError("initial must be a KernelSnapshot")
    if type(transitions) is not tuple or not all(
        type(transition) is StateTransition for transition in transitions
    ):
        raise TypeError("transitions must be a tuple of StateTransition values")
    snapshot = initial
    for transition in transitions:
        result = apply_transition(snapshot, transition)
        if not result.accepted:
            return result
        snapshot = result.snapshot
    return ApplyResult(True, ApplyReason.APPLIED, snapshot)


def replay_journal_events(
    initial: KernelSnapshot,
    events: tuple[JournalEvent, ...],
) -> ApplyResult:
    if type(initial) is not KernelSnapshot:
        raise TypeError("initial must be a KernelSnapshot")
    if type(events) is not tuple or not all(type(event) is JournalEvent for event in events):
        raise TypeError("events must be a tuple of JournalEvent values")
    snapshot = initial
    for event in events:
        result = apply_journal_event(snapshot, event)
        if not result.accepted:
            return result
        snapshot = result.snapshot
    return ApplyResult(True, ApplyReason.APPLIED, snapshot)


def _task_index(tasks: tuple[TaskProjection, ...], task_id: str) -> int | None:
    for index, task in enumerate(tasks):
        if task.task_id == task_id:
            return index
    return None


def _attempt_index(
    attempts: tuple[AttemptProjection, ...], task_id: str, attempt: int | None
) -> int | None:
    for index, value in enumerate(attempts):
        if value.task_id == task_id and value.attempt == attempt:
            return index
    return None


def _replace_at(values: tuple[_T, ...], index: int, value: _T) -> tuple[_T, ...]:
    return (*values[:index], value, *values[index + 1 :])


__all__ = [
    "GENESIS_HASH",
    "ApplyReason",
    "ApplyResult",
    "AttemptProjection",
    "KernelSnapshot",
    "StateTransition",
    "StateTransitionError",
    "TaskProjection",
    "apply_journal_event",
    "apply_transition",
    "replay",
    "replay_journal_events",
    "validate_transition",
]
