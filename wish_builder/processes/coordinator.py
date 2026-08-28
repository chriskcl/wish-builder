"""Foreground coordinator for one approved Wish Builder execution snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.manifest_v2 import ExecutionManifestV2, SchedulerMode
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    DispatchRecoveryPayload,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectReceiptValue,
    EffectRequestPayload,
    EffectStatus,
    EvidenceRef,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseOwner,
    OperationOutcome,
    OutcomeKind,
    RuntimeReasonCode,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.kernel.state import (
    ApplyReason,
    AttemptProjection,
    KernelSnapshot,
    apply_journal_event,
)
from wish_builder.services.dispatch_recovery import (
    DispatchRecoveryProjectionError,
    DispatchRecoveryRecord,
    advance_dispatch_recoveries,
)
from wish_builder.services.journal import (
    AppendResult,
    AppendStatus,
    CoordinatorLeaseState,
    DurableJournal,
    JournalEventDraft,
    JournalHead,
)
from wish_builder.services.ports import PersistedEffectRequest, TaskPort
from wish_builder.services.backend_effects import (
    BackendDispatchPort,
    BackendDispatchResult,
    BackendDispatchEffectStatus,
    BackendDispatchPlan,
)


class CoordinatorStatus(StrEnum):
    """Outcome of one bounded foreground coordinator operation."""

    PROGRESSED = "progressed"
    IDLE = "idle"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class CoordinatorReason(StrEnum):
    NONE = "none"
    NO_READY_TASKS = "no_ready_tasks"
    TASK_NOT_READY = "task_not_ready"
    LEASE_NOT_ADMITTED = "lease_not_admitted"
    JOURNAL_CONFLICT = "journal_conflict"
    PERSISTENCE_FAILED = "persistence_failed"
    INDEX_MISMATCH = "index_mismatch"
    STATE_REJECTED = "state_rejected"
    PORT_OUTCOME_INVALID = "port_outcome_invalid"
    EFFECT_ABSENT_AFTER_APPLY = "effect_absent_after_apply"
    EFFECT_OUTCOME_UNKNOWN = "effect_outcome_unknown"
    DISPATCH_NOT_PENDING = "dispatch_not_pending"
    DISPATCH_RECONCILIATION_REQUIRED = "dispatch_reconciliation_required"
    STALE_RESULT = "stale_result"
    DUPLICATE_RESULT = "duplicate_result"
    RECOVERY_PROOF_INVALID = "recovery_proof_invalid"
    RECOVERY_CONFLICT = "recovery_conflict"
    RECOVERY_IN_PROGRESS = "recovery_in_progress"
    GRAPH_SNAPSHOT_NOT_ADMITTED = "graph_snapshot_not_admitted"


EventIdFactory = Callable[[JournalEventType, int, ExecutionIdentity], str]
CorrelationIdFactory = Callable[[str, int, int], str]
BackendDispatchPlanFactory = Callable[[ExecutionIdentity], BackendDispatchPlan]
AuthorityClock = Callable[[], datetime]
ExecutionSnapshotAdmitter = Callable[[], bool]


def _admit_frozen_snapshot() -> bool:
    return True


def _default_event_id(
    event_type: JournalEventType,
    sequence: int,
    identity: ExecutionIdentity,
) -> str:
    del identity
    label = event_type.value.replace("_", "-").upper()
    return f"EVENT-{label}-{sequence:08d}"


def _default_correlation_id(task_id: str, attempt: int, epoch: int) -> str:
    return f"CORRELATION-{task_id}-{attempt:04d}-EPOCH-{epoch:04d}"


@dataclass(frozen=True, slots=True)
class CoordinatorCursor:
    """Recovery-composable authoritative state plus its rebuildable graph index."""

    snapshot: KernelSnapshot
    graph_index: GraphIndex
    lease_state: CoordinatorLeaseState
    dispatch_recoveries: tuple[DispatchRecoveryRecord, ...] = ()

    def __post_init__(self) -> None:
        if type(self.snapshot) is not KernelSnapshot:
            raise TypeError("snapshot must be a KernelSnapshot")
        if type(self.graph_index) is not GraphIndex:
            raise TypeError("graph_index must be a GraphIndex")
        if type(self.lease_state) is not CoordinatorLeaseState:
            raise TypeError("lease_state must be a CoordinatorLeaseState")
        if type(self.dispatch_recoveries) is not tuple or not all(
            type(record) is DispatchRecoveryRecord
            for record in self.dispatch_recoveries
        ):
            raise TypeError(
                "dispatch_recoveries must contain DispatchRecoveryRecord values"
            )
        head = self.lease_state.head
        if (
            self.snapshot.last_sequence != head.sequence
            or self.snapshot.last_event_hash != head.event_hash
        ):
            raise ValueError("snapshot and lease_state must share one Journal head")

    @property
    def head(self) -> JournalHead:
        return self.lease_state.head


@dataclass(frozen=True, slots=True)
class WorkerResultProposal:
    """Untrusted worker result identity checked before any state transition."""

    identity: ExecutionIdentity
    actor_id: str
    succeeded: bool
    reason_code: RuntimeReasonCode | None = None
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if type(self.identity) is not ExecutionIdentity or not self.identity.is_attempt:
            raise ValueError("worker result requires complete attempt identity")
        if self.identity.correlation_id is None:
            raise ValueError("worker result requires correlation identity")
        if type(self.actor_id) is not str or not self.actor_id:
            raise ValueError("actor_id must be non-empty")
        if type(self.succeeded) is not bool:
            raise TypeError("succeeded must be a bool")
        if (
            self.reason_code is not None
            and type(self.reason_code) is not RuntimeReasonCode
        ):
            raise TypeError("reason_code must be a RuntimeReasonCode or null")
        if self.succeeded and self.reason_code is not None:
            raise ValueError("a successful result cannot carry a reason_code")
        if not self.succeeded and self.reason_code is None:
            raise ValueError("a failed result requires a reason_code")
        if type(self.evidence) is not tuple or not all(
            type(item) is EvidenceRef for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of EvidenceRef values")


@dataclass(frozen=True, slots=True)
class CoordinatorStepResult:
    status: CoordinatorStatus
    reason: CoordinatorReason
    cursor: CoordinatorCursor
    events: tuple[JournalEvent, ...] = ()
    dispatched: tuple[ExecutionIdentity, ...] = ()
    receipt: EffectReceipt | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not CoordinatorStatus:
            raise TypeError("status must be a CoordinatorStatus")
        if type(self.reason) is not CoordinatorReason:
            raise TypeError("reason must be a CoordinatorReason")
        if type(self.cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        if type(self.events) is not tuple or not all(
            type(event) is JournalEvent for event in self.events
        ):
            raise TypeError("events must be a tuple of JournalEvent values")
        if type(self.dispatched) is not tuple or not all(
            type(identity) is ExecutionIdentity for identity in self.dispatched
        ):
            raise TypeError("dispatched must be a tuple of ExecutionIdentity values")
        if self.receipt is not None and type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt or null")
        if self.status is CoordinatorStatus.PROGRESSED:
            if self.reason is not CoordinatorReason.NONE:
                raise ValueError("progressed results cannot carry a failure reason")
        elif self.reason is CoordinatorReason.NONE:
            raise ValueError("non-progressed results require a reason")


@dataclass(frozen=True, slots=True)
class CoordinatorReservationResult:
    """Attempts reserved for preparation before any worker dispatch effect."""

    status: CoordinatorStatus
    reason: CoordinatorReason
    cursor: CoordinatorCursor
    events: tuple[JournalEvent, ...] = ()
    reserved: tuple[ExecutionIdentity, ...] = ()

    def __post_init__(self) -> None:
        if type(self.status) is not CoordinatorStatus:
            raise TypeError("status must be a CoordinatorStatus")
        if type(self.reason) is not CoordinatorReason:
            raise TypeError("reason must be a CoordinatorReason")
        if type(self.cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        if type(self.events) is not tuple or not all(
            type(event) is JournalEvent for event in self.events
        ):
            raise TypeError("events must be a tuple of JournalEvent values")
        if type(self.reserved) is not tuple or not all(
            type(identity) is ExecutionIdentity and identity.is_attempt
            for identity in self.reserved
        ):
            raise TypeError("reserved must contain complete attempt identities")
        if len(set(self.reserved)) != len(self.reserved):
            raise ValueError("reserved identities must be unique")
        if self.status is CoordinatorStatus.PROGRESSED:
            if self.reason is not CoordinatorReason.NONE or not self.reserved:
                raise ValueError(
                    "progressed reservations require identities and no failure reason"
                )
        elif self.reason is CoordinatorReason.NONE:
            raise ValueError("non-progressed reservations require a reason")


@dataclass(frozen=True, slots=True)
class _AppendOutcome:
    event: JournalEvent | None
    reason: CoordinatorReason
    append_result: AppendResult | None = None


class ForegroundCoordinator:
    """Single-run foreground scheduler over an immutable Gate-B snapshot."""

    def __init__(
        self,
        manifest: ExecutionManifestV2,
        cursor: CoordinatorCursor,
        journal: DurableJournal,
        task_port: TaskPort | None,
        *,
        backend_effects: BackendDispatchPort | None = None,
        backend_plan_factory: BackendDispatchPlanFactory | None = None,
        coordinator_id: str,
        owner: LeaseOwner,
        fencing_token: int,
        authority_clock: AuthorityClock,
        event_id_factory: EventIdFactory = _default_event_id,
        correlation_id_factory: CorrelationIdFactory = _default_correlation_id,
        execution_snapshot_admitter: ExecutionSnapshotAdmitter = _admit_frozen_snapshot,
    ) -> None:
        if type(manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if manifest.scheduler_mode is not SchedulerMode.WISH_BUILDER:
            raise ValueError("scheduler_mode must be wish_builder")
        if type(cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        task_mode = isinstance(task_port, TaskPort)
        backend_mode = isinstance(backend_effects, BackendDispatchPort)
        if task_mode == backend_mode:
            raise TypeError(
                "exactly one task_port or backend_effects dispatcher is required"
            )
        if task_mode:
            assert task_port is not None
            if task_port.adapter_kind is not AdapterKind.TASK or (
                EffectOperation.WORKER_DISPATCH not in task_port.operations
            ):
                raise ValueError("task_port must support worker_dispatch")
            if backend_plan_factory is not None:
                raise ValueError("task_port mode cannot use a backend plan factory")
        elif not callable(backend_plan_factory):
            raise TypeError("backend_effects mode requires a backend plan factory")
        if type(coordinator_id) is not str or not coordinator_id:
            raise ValueError("coordinator_id must be non-empty")
        if type(owner) is not LeaseOwner:
            raise TypeError("owner must be a LeaseOwner")
        if owner.actor.actor_id != coordinator_id:
            raise ValueError("owner actor_id must match coordinator_id")
        if type(fencing_token) is not int or fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if not callable(authority_clock):
            raise TypeError("authority_clock must be callable")
        if not callable(event_id_factory) or not callable(correlation_id_factory):
            raise TypeError("ID factories must be callable")
        if not callable(execution_snapshot_admitter):
            raise TypeError("execution_snapshot_admitter must be callable")
        if cursor.snapshot.run_id != manifest.run_id:
            raise ValueError("snapshot run_id does not match manifest")
        if cursor.snapshot.coordinator_epoch != fencing_token:
            raise ValueError("snapshot epoch does not match fencing_token")
        if not cursor.graph_index.verify(manifest, cursor.snapshot):
            raise ValueError("graph_index does not match manifest and snapshot")

        self._manifest = manifest
        self._manifest_digest = manifest.canonical_sha256()
        self._cursor = cursor
        self._journal = journal
        self._task_port = task_port
        self._backend_effects = backend_effects
        self._backend_plan_factory = backend_plan_factory
        self._coordinator_id = coordinator_id
        self._owner = owner
        self._fencing_token = fencing_token
        self._authority_clock = authority_clock
        self._event_id_factory = event_id_factory
        self._correlation_id_factory = correlation_id_factory
        self._execution_snapshot_admitter = execution_snapshot_admitter

    @property
    def cursor(self) -> CoordinatorCursor:
        return self._cursor

    @property
    def manifest_digest(self) -> str:
        return self._manifest_digest

    def dispatch_ready(self, *, limit: int | None = None) -> CoordinatorStepResult:
        """Dispatch the current ready set in canonical GraphIndex order."""

        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("limit must be a positive integer or null")
        events: list[JournalEvent] = []
        dispatched: list[ExecutionIdentity] = []
        last_receipt: EffectReceipt | None = None
        remaining = limit
        while remaining is None or remaining > 0:
            ready = self._cursor.graph_index.ready_tasks
            if not ready:
                break
            result = self.dispatch_task(ready[0])
            events.extend(result.events)
            dispatched.extend(result.dispatched)
            if result.receipt is not None:
                last_receipt = result.receipt
            if result.status is not CoordinatorStatus.PROGRESSED:
                return CoordinatorStepResult(
                    result.status,
                    result.reason,
                    self._cursor,
                    tuple(events),
                    tuple(dispatched),
                    result.receipt,
                )
            if remaining is not None:
                remaining -= 1
        if dispatched:
            return CoordinatorStepResult(
                CoordinatorStatus.PROGRESSED,
                CoordinatorReason.NONE,
                self._cursor,
                tuple(events),
                tuple(dispatched),
                last_receipt,
            )
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        return self._result(
            CoordinatorStatus.IDLE,
            CoordinatorReason.NO_READY_TASKS,
        )

    def reserve_ready(
        self,
        *,
        limit: int | None = None,
    ) -> CoordinatorReservationResult:
        """Reserve ready attempts without invoking the worker dispatch port.

        The foreground composition uses this phase to create and verify the
        isolated attempt worktree before any provider process can start.  A
        current-epoch ``RESERVED`` attempt is returned first so a crash after
        reservation can resume preparation idempotently.
        """

        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("limit must be a positive integer or null")
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._reservation_result(CoordinatorStatus.BLOCKED, admission)

        capacity = self._cursor.graph_index.max_concurrency
        if limit is not None:
            capacity = min(capacity, limit)
        task_states = dict(self._cursor.graph_index.task_states)
        positions = {
            task_id: position
            for position, task_id in enumerate(
                self._cursor.graph_index.topological_order
            )
        }
        resumed = sorted(
            (
                ExecutionIdentity(
                    self._manifest.run_id,
                    attempt.coordinator_epoch,
                    attempt.task_id,
                    attempt.attempt,
                    attempt.correlation_id,
                )
                for attempt in self._cursor.snapshot.attempts
                if attempt.state is RuntimeState.RESERVED
                and attempt.coordinator_epoch == self._fencing_token
                and task_states.get(attempt.task_id) is RuntimeState.LEASED
            ),
            key=lambda identity: positions[identity.task_id or ""],
        )
        if resumed:
            return self._reservation_result(
                CoordinatorStatus.PROGRESSED,
                CoordinatorReason.NONE,
                reserved=tuple(resumed[:capacity]),
            )

        events: list[JournalEvent] = []
        reserved: list[ExecutionIdentity] = []
        while len(reserved) < capacity:
            ready = self._cursor.graph_index.ready_tasks
            if not ready:
                break
            result = self.reserve_task(ready[0])
            events.extend(result.events)
            reserved.extend(result.reserved)
            if result.status is not CoordinatorStatus.PROGRESSED:
                return self._reservation_result(
                    result.status,
                    result.reason,
                    events=tuple(events),
                    reserved=tuple(reserved),
                )
        if reserved:
            return self._reservation_result(
                CoordinatorStatus.PROGRESSED,
                CoordinatorReason.NONE,
                events=tuple(events),
                reserved=tuple(reserved),
            )
        return self._reservation_result(
            CoordinatorStatus.IDLE,
            CoordinatorReason.NO_READY_TASKS,
        )

    def reserve_task(self, task_id: str) -> CoordinatorReservationResult:
        """Reserve one ready task without producing a provider side effect."""

        if type(task_id) is not str or not task_id:
            raise ValueError("task_id must be non-empty")
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._reservation_result(CoordinatorStatus.BLOCKED, admission)
        if task_id not in self._cursor.graph_index.ready_tasks:
            return self._reservation_result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.TASK_NOT_READY,
            )

        events: list[JournalEvent] = []
        state = dict(self._cursor.graph_index.task_states)[task_id]
        identity = ExecutionIdentity(
            self._manifest.run_id, self._fencing_token, task_id
        )
        if state is RuntimeState.APPROVED:
            appended = self._append_transition(
                JournalEventType.TASK_READY,
                identity,
                TransitionSubject.TASK,
                RuntimeState.APPROVED,
                RuntimeState.READY,
            )
            if appended.event is None:
                return self._reservation_result(
                    CoordinatorStatus.BLOCKED,
                    appended.reason,
                    events=tuple(events),
                )
            events.append(appended.event)

        appended = self._append_transition(
            JournalEventType.LEASE_ACQUIRED,
            identity,
            TransitionSubject.TASK,
            RuntimeState.READY,
            RuntimeState.LEASED,
        )
        if appended.event is None:
            return self._reservation_result(
                CoordinatorStatus.BLOCKED,
                appended.reason,
                events=tuple(events),
            )
        events.append(appended.event)
        return self._reserve_attempt(task_id, events)

    def dispatch_reserved(
        self,
        identity: ExecutionIdentity,
    ) -> CoordinatorStepResult:
        """Dispatch an already-prepared reserved attempt exactly once."""

        if type(identity) is not ExecutionIdentity or not identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        attempt = self._matching_attempt(identity)
        task_state = dict(self._cursor.graph_index.task_states).get(
            identity.task_id or ""
        )
        if (
            attempt is None
            or identity.coordinator_epoch != self._fencing_token
            or attempt.state is not RuntimeState.RESERVED
            or task_state is not RuntimeState.LEASED
        ):
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.DISPATCH_NOT_PENDING,
            )
        return self._request_and_apply(identity, [])

    def dispatch_task(self, task_id: str) -> CoordinatorStepResult:
        """Admit and execute exactly one ready task dispatch."""

        if type(task_id) is not str or not task_id:
            raise ValueError("task_id must be non-empty")
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        if task_id not in self._cursor.graph_index.ready_tasks:
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.TASK_NOT_READY,
            )

        events: list[JournalEvent] = []
        state = dict(self._cursor.graph_index.task_states)[task_id]
        identity = ExecutionIdentity(
            self._manifest.run_id, self._fencing_token, task_id
        )
        if state is RuntimeState.APPROVED:
            failed = self._append_transition(
                JournalEventType.TASK_READY,
                identity,
                TransitionSubject.TASK,
                RuntimeState.APPROVED,
                RuntimeState.READY,
            )
            if failed.event is None:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    failed.reason,
                    events=tuple(events),
                )
            events.append(failed.event)

        failed = self._append_transition(
            JournalEventType.LEASE_ACQUIRED,
            identity,
            TransitionSubject.TASK,
            RuntimeState.READY,
            RuntimeState.LEASED,
        )
        if failed.event is None:
            return self._result(
                CoordinatorStatus.BLOCKED,
                failed.reason,
                events=tuple(events),
            )
        events.append(failed.event)

        return self._reserve_and_dispatch(task_id, events)

    def resume_dispatch(self, task_id: str) -> CoordinatorStepResult:
        """Resume a crash-interrupted dispatch that has no unresolved effect."""

        if type(task_id) is not str or not task_id:
            raise ValueError("task_id must be non-empty")
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        states = dict(self._cursor.graph_index.task_states)
        if task_id not in states:
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.TASK_NOT_READY,
            )
        if states[task_id] is RuntimeState.READY:
            return self.dispatch_task(task_id)
        if states[task_id] is not RuntimeState.LEASED:
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.TASK_NOT_READY,
            )
        attempts = tuple(
            attempt
            for attempt in self._cursor.snapshot.attempts
            if attempt.task_id == task_id
            and attempt.state
            not in {
                RuntimeState.SUCCEEDED,
                RuntimeState.FAILED,
                RuntimeState.TERMINATED,
                RuntimeState.OUTCOME_UNKNOWN,
            }
        )
        if not attempts:
            return self._reserve_and_dispatch(task_id, [])
        if len(attempts) != 1:
            return self._result(
                CoordinatorStatus.BLOCKED,
                CoordinatorReason.STATE_REJECTED,
            )
        attempt = attempts[0]
        if attempt.coordinator_epoch != self._fencing_token:
            return self._result(
                CoordinatorStatus.BLOCKED,
                CoordinatorReason.STATE_REJECTED,
            )
        identity = ExecutionIdentity(
            self._manifest.run_id,
            attempt.coordinator_epoch,
            attempt.task_id,
            attempt.attempt,
            attempt.correlation_id,
        )
        if attempt.state is RuntimeState.RESERVED:
            return self._request_and_apply(identity, [])
        if attempt.state is RuntimeState.DISPATCH_REQUESTED:
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.DISPATCH_RECONCILIATION_REQUIRED,
            )
        return self._result(
            CoordinatorStatus.REJECTED,
            CoordinatorReason.TASK_NOT_READY,
        )

    def _reserve_and_dispatch(
        self,
        task_id: str,
        events: list[JournalEvent],
    ) -> CoordinatorStepResult:
        reserved = self._reserve_attempt(task_id, events)
        if reserved.status is not CoordinatorStatus.PROGRESSED:
            return self._result(
                reserved.status,
                reserved.reason,
                events=reserved.events,
            )
        # The legacy one-step API already admitted immediately before
        # reservation. Preserve its historical clock boundary while the
        # foreground composition uses dispatch_reserved() after worktree prep.
        dispatched = self._request_and_apply(reserved.reserved[0], [])
        return self._result(
            dispatched.status,
            dispatched.reason,
            events=reserved.events + dispatched.events,
            dispatched=dispatched.dispatched,
            receipt=dispatched.receipt,
        )

    def _reserve_attempt(
        self,
        task_id: str,
        events: list[JournalEvent],
    ) -> CoordinatorReservationResult:
        attempt = self._next_attempt(task_id)
        correlation_id = self._correlation_id_factory(
            task_id,
            attempt,
            self._fencing_token,
        )
        attempt_identity = ExecutionIdentity(
            self._manifest.run_id,
            self._fencing_token,
            task_id,
            attempt,
            correlation_id,
        )
        failed = self._append_transition(
            JournalEventType.ATTEMPT_RESERVED,
            attempt_identity,
            TransitionSubject.ATTEMPT,
            RuntimeState.PLANNED,
            RuntimeState.RESERVED,
        )
        if failed.event is None:
            return self._reservation_result(
                CoordinatorStatus.BLOCKED,
                failed.reason,
                events=tuple(events),
            )
        events.append(failed.event)
        return self._reservation_result(
            CoordinatorStatus.PROGRESSED,
            CoordinatorReason.NONE,
            events=tuple(events),
            reserved=(attempt_identity,),
        )

    def _request_and_apply(
        self,
        attempt_identity: ExecutionIdentity,
        events: list[JournalEvent],
    ) -> CoordinatorStepResult:
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(
                CoordinatorStatus.BLOCKED,
                admission,
                events=tuple(events),
            )
        request = self._append_dispatch_request(attempt_identity)
        if request.event is None or request.append_result is None:
            return self._result(
                CoordinatorStatus.BLOCKED,
                request.reason,
                events=tuple(events),
            )
        events.append(request.event)
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(
                CoordinatorStatus.BLOCKED,
                admission,
                events=tuple(events),
            )

        persisted = PersistedEffectRequest.from_append_result(request.append_result)
        receipt: EffectReceipt | None
        if self._backend_effects is not None:
            assert self._backend_plan_factory is not None
            plan = self._backend_plan_factory(attempt_identity)
            if type(plan) is not BackendDispatchPlan:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    CoordinatorReason.PORT_OUTCOME_INVALID,
                    events=tuple(events),
                )
            child = self._backend_effects.dispatch(persisted, plan)
            if type(child) is not BackendDispatchResult:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    CoordinatorReason.PORT_OUTCOME_INVALID,
                    events=tuple(events),
                )
            adopted = self._adopt_committed_events(child.events)
            events.extend(child.events)
            if adopted is not CoordinatorReason.NONE:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    adopted,
                    events=tuple(events),
                    receipt=child.receipt,
                )
            receipt = child.receipt
            if (
                child.status is BackendDispatchEffectStatus.BLOCKED
                and receipt is None
            ):
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    CoordinatorReason.EFFECT_OUTCOME_UNKNOWN,
                    events=tuple(events),
                )
        else:
            assert self._task_port is not None
            outcome = self._task_port.apply(persisted)
            receipt = self._receipt_from_outcome(outcome, attempt_identity)
        if receipt is None:
            return self._result(
                CoordinatorStatus.BLOCKED,
                CoordinatorReason.PORT_OUTCOME_INVALID,
                events=tuple(events),
            )
        return self._complete_dispatch(receipt, events)

    def reconcile_dispatch(self, request_event: JournalEvent) -> CoordinatorStepResult:
        """Reconcile one pending dispatch and retry only after proven absence."""

        if type(request_event) is not JournalEvent:
            raise TypeError("request_event must be a JournalEvent")
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        if not self._is_pending_request(request_event):
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.DISPATCH_NOT_PENDING,
            )
        identity = request_event.identity
        takeover_reconciliation = identity.coordinator_epoch < self._fencing_token
        outcome = self._task_port.lookup(identity, EffectOperation.WORKER_DISPATCH)
        receipt = self._receipt_from_outcome(outcome, identity)
        if receipt is None:
            return self._result(
                CoordinatorStatus.BLOCKED,
                CoordinatorReason.PORT_OUTCOME_INVALID,
            )
        if receipt.status is EffectStatus.ABSENT and not takeover_reconciliation:
            admission = self._admission_reason()
            if admission is not CoordinatorReason.NONE:
                return self._result(CoordinatorStatus.BLOCKED, admission)
            persisted = PersistedEffectRequest.from_append_result(
                AppendResult(
                    AppendStatus.IDEMPOTENT,
                    JournalHead(request_event.sequence, request_event.event_hash),
                    request_event,
                )
            )
            outcome = self._task_port.apply(persisted)
            receipt = self._receipt_from_outcome(outcome, identity)
            if receipt is None:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    CoordinatorReason.PORT_OUTCOME_INVALID,
                )
        return self._complete_dispatch(
            receipt,
            [],
            observation_identity=self._current_epoch_identity(identity),
        )

    def resume_unknown_dispatch(
        self,
        request_event: JournalEvent,
        proof: DispatchRecoveryPayload,
    ) -> CoordinatorStepResult:
        """Resume one blocked unknown dispatch after human-supplied absence proof."""

        if type(request_event) is not JournalEvent:
            raise TypeError("request_event must be a JournalEvent")
        if type(proof) is not DispatchRecoveryPayload:
            raise TypeError("proof must be a DispatchRecoveryPayload")
        admission = self._admission_reason(allow_recovery=True)
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        if not self._proof_matches_request(proof, request_event):
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.RECOVERY_PROOF_INVALID,
            )

        matching = next(
            (
                record
                for record in self._cursor.dispatch_recoveries
                if record.recovery_id == proof.recovery_id
            ),
            None,
        )
        if matching is not None and matching.payload != proof:
            return self._result(
                CoordinatorStatus.BLOCKED,
                CoordinatorReason.RECOVERY_CONFLICT,
            )
        incomplete = tuple(
            record
            for record in self._cursor.dispatch_recoveries
            if not record.complete
        )
        if incomplete and matching not in incomplete:
            return self._result(
                CoordinatorStatus.BLOCKED,
                CoordinatorReason.RECOVERY_CONFLICT,
            )
        if matching is not None and matching.complete:
            return self._result(CoordinatorStatus.PROGRESSED, CoordinatorReason.NONE)

        if matching is None:
            if (
                proof.last_valid_sequence != self._cursor.head.sequence
                or proof.last_valid_event_hash != self._cursor.head.event_hash
            ):
                return self._result(
                    CoordinatorStatus.REJECTED,
                    CoordinatorReason.RECOVERY_PROOF_INVALID,
                )
            if not self._unknown_dispatch_state_matches(proof.subject_identity):
                return self._result(
                    CoordinatorStatus.REJECTED,
                    CoordinatorReason.RECOVERY_PROOF_INVALID,
                )
        elif not self._recovery_prefix_state_matches(matching):
            return self._result(
                CoordinatorStatus.BLOCKED,
                CoordinatorReason.RECOVERY_CONFLICT,
            )

        events: list[JournalEvent] = []
        if matching is None:
            appended = self._append_payload(
                JournalEventType.RECOVERY_COMPLETED,
                ExecutionIdentity(self._manifest.run_id, self._fencing_token),
                proof,
                actor_type=proof.command.actor.actor_type,
                actor_id=proof.command.actor.actor_id,
                allow_recovery=True,
            )
            if appended.event is None:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    appended.reason,
                    events=tuple(events),
                    receipt=proof.receipt,
                )
            events.append(appended.event)
            matching = next(
                record
                for record in self._cursor.dispatch_recoveries
                if record.recovery_id == proof.recovery_id
            )

        assert matching is not None
        if matching.task_retry_event is None:
            task_id = proof.subject_identity.task_id
            assert task_id is not None
            appended = self._append_transition(
                JournalEventType.TASK_RETRY_SCHEDULED,
                ExecutionIdentity(
                    self._manifest.run_id,
                    self._fencing_token,
                    task_id,
                ),
                TransitionSubject.TASK,
                RuntimeState.BLOCKED,
                RuntimeState.READY,
                actor_type=proof.command.actor.actor_type,
                actor_id=proof.command.actor.actor_id,
                evidence=proof.evidence,
                allow_recovery=True,
            )
            if appended.event is None:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    appended.reason,
                    events=tuple(events),
                    receipt=proof.receipt,
                )
            events.append(appended.event)
            matching = next(
                record
                for record in self._cursor.dispatch_recoveries
                if record.recovery_id == proof.recovery_id
            )

        if matching.run_resumed_event is None:
            appended = self._append_transition(
                JournalEventType.RUN_RESUMED,
                ExecutionIdentity(self._manifest.run_id, self._fencing_token),
                TransitionSubject.RUN,
                RuntimeState.BLOCKED,
                RuntimeState.RUNNING,
                actor_type=proof.command.actor.actor_type,
                actor_id=proof.command.actor.actor_id,
                evidence=proof.evidence,
                allow_recovery=True,
            )
            if appended.event is None:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    appended.reason,
                    events=tuple(events),
                    receipt=proof.receipt,
                )
            events.append(appended.event)
        return self._result(
            CoordinatorStatus.PROGRESSED,
            CoordinatorReason.NONE,
            events=tuple(events),
            receipt=proof.receipt,
        )

    def accept_worker_result(
        self,
        proposal: WorkerResultProposal,
    ) -> CoordinatorStepResult:
        """Persist a current worker result; stale and duplicate results are inert."""

        if type(proposal) is not WorkerResultProposal:
            raise TypeError("proposal must be a WorkerResultProposal")
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        attempt = self._matching_attempt(proposal.identity)
        if (
            attempt is None
            or proposal.identity.coordinator_epoch != self._fencing_token
        ):
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.STALE_RESULT,
            )
        if attempt.state is not RuntimeState.RUNNING:
            reason = (
                CoordinatorReason.DUPLICATE_RESULT
                if attempt.state
                in {
                    RuntimeState.SUCCEEDED,
                    RuntimeState.FAILED,
                    RuntimeState.TERMINATED,
                    RuntimeState.OUTCOME_UNKNOWN,
                }
                else CoordinatorReason.STALE_RESULT
            )
            return self._result(CoordinatorStatus.REJECTED, reason)
        task_state = dict(self._cursor.graph_index.task_states).get(
            proposal.identity.task_id or ""
        )
        if task_state is not RuntimeState.DISPATCHED:
            return self._result(
                CoordinatorStatus.REJECTED,
                CoordinatorReason.STALE_RESULT,
            )
        event_type = (
            JournalEventType.ATTEMPT_SUCCEEDED
            if proposal.succeeded
            else JournalEventType.ATTEMPT_FAILED
        )
        to_state = RuntimeState.SUCCEEDED if proposal.succeeded else RuntimeState.FAILED
        appended = self._append_transition(
            event_type,
            proposal.identity,
            TransitionSubject.ATTEMPT,
            RuntimeState.RUNNING,
            to_state,
            actor_type=ActorType.WORKER,
            actor_id=proposal.actor_id,
            reason_code=proposal.reason_code,
            evidence=proposal.evidence,
        )
        if appended.event is None:
            return self._result(CoordinatorStatus.BLOCKED, appended.reason)
        events = [appended.event]
        if not proposal.succeeded:
            assert proposal.identity.task_id is not None
            task = self._append_transition(
                JournalEventType.TASK_BLOCKED,
                ExecutionIdentity(
                    proposal.identity.run_id,
                    proposal.identity.coordinator_epoch,
                    proposal.identity.task_id,
                ),
                TransitionSubject.TASK,
                RuntimeState.DISPATCHED,
                RuntimeState.BLOCKED,
                reason_code=proposal.reason_code,
            )
            if task.event is None:
                return self._result(
                    CoordinatorStatus.BLOCKED,
                    task.reason,
                    events=tuple(events),
                )
            events.append(task.event)
            if self._cursor.snapshot.status is RuntimeState.RUNNING:
                run = self._append_transition(
                    JournalEventType.RUN_BLOCKED,
                    ExecutionIdentity(
                        proposal.identity.run_id,
                        proposal.identity.coordinator_epoch,
                    ),
                    TransitionSubject.RUN,
                    RuntimeState.RUNNING,
                    RuntimeState.BLOCKED,
                    reason_code=proposal.reason_code,
                )
                if run.event is None:
                    return self._result(
                        CoordinatorStatus.BLOCKED,
                        run.reason,
                        events=tuple(events),
                    )
                events.append(run.event)
        return self._result(
            CoordinatorStatus.PROGRESSED,
            CoordinatorReason.NONE,
            events=tuple(events),
        )

    def _complete_dispatch(
        self,
        receipt: EffectReceipt,
        events: list[JournalEvent],
        *,
        observation_identity: ExecutionIdentity | None = None,
    ) -> CoordinatorStepResult:
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(
                CoordinatorStatus.BLOCKED,
                admission,
                events=tuple(events),
                receipt=receipt,
            )
        observed_identity = observation_identity or receipt.identity
        observed = self._append_payload(
            JournalEventType.DISPATCH_OBSERVED,
            observed_identity,
            EffectObservationPayload(AdapterKind.TASK, receipt),
            actor_type=ActorType.ADAPTER,
            actor_id="task-adapter",
        )
        if observed.event is None:
            return self._result(
                CoordinatorStatus.BLOCKED,
                observed.reason,
                events=tuple(events),
                receipt=receipt,
            )
        events.append(observed.event)
        if receipt.status is EffectStatus.APPLIED:
            return self._result(
                CoordinatorStatus.PROGRESSED,
                CoordinatorReason.NONE,
                events=tuple(events),
                dispatched=(receipt.identity,),
                receipt=receipt,
            )
        reason = (
            CoordinatorReason.EFFECT_OUTCOME_UNKNOWN
            if receipt.status is EffectStatus.UNKNOWN
            else CoordinatorReason.EFFECT_ABSENT_AFTER_APPLY
        )
        runtime_reason = (
            RuntimeReasonCode.WORKER_OUTCOME_UNKNOWN
            if receipt.status is EffectStatus.UNKNOWN
            else RuntimeReasonCode.INVARIANT_VIOLATION
        )
        block_reason = self._block_unknown_dispatch(
            observed_identity,
            runtime_reason,
            events,
        )
        return self._result(
            CoordinatorStatus.BLOCKED,
            reason if block_reason is CoordinatorReason.NONE else block_reason,
            events=tuple(events),
            receipt=receipt,
        )

    def _block_unknown_dispatch(
        self,
        identity: ExecutionIdentity,
        reason_code: RuntimeReasonCode,
        events: list[JournalEvent],
    ) -> CoordinatorReason:
        attempt = self._append_transition(
            JournalEventType.ATTEMPT_OUTCOME_UNKNOWN,
            identity,
            TransitionSubject.ATTEMPT,
            RuntimeState.DISPATCH_REQUESTED,
            RuntimeState.OUTCOME_UNKNOWN,
            reason_code=reason_code,
        )
        if attempt.event is None:
            return attempt.reason
        events.append(attempt.event)
        task_identity = ExecutionIdentity(
            identity.run_id,
            identity.coordinator_epoch,
            identity.task_id,
        )
        task = self._append_transition(
            JournalEventType.TASK_BLOCKED,
            task_identity,
            TransitionSubject.TASK,
            RuntimeState.LEASED,
            RuntimeState.BLOCKED,
            reason_code=reason_code,
        )
        if task.event is None:
            return task.reason
        events.append(task.event)
        run = self._append_transition(
            JournalEventType.RUN_BLOCKED,
            ExecutionIdentity(identity.run_id, identity.coordinator_epoch),
            TransitionSubject.RUN,
            RuntimeState.RUNNING,
            RuntimeState.BLOCKED,
            reason_code=reason_code,
        )
        if run.event is not None:
            events.append(run.event)
            return CoordinatorReason.NONE
        return run.reason

    def _append_dispatch_request(
        self,
        identity: ExecutionIdentity,
    ) -> _AppendOutcome:
        assert identity.task_id is not None
        task = next(
            task for task in self._manifest.tasks if task.id == identity.task_id
        )
        target_hash = "sha256:" + canonical_sha256(
            {
                "adapter": AdapterKind.TASK.value,
                "operation": EffectOperation.WORKER_DISPATCH.value,
                "run_id": identity.run_id,
                "task_id": identity.task_id,
            }
        )
        request_hash = "sha256:" + canonical_sha256(
            {
                "capability_digest": self._manifest.capability_digest,
                "coordinator_epoch": identity.coordinator_epoch,
                "identity": identity.to_primitive(),
                "launch_profile_digest": self._manifest.launch_profile_digest,
                "manifest_digest": self._manifest_digest,
                "policy_digest": self._manifest.policy_digest,
                "provider": self._manifest.provider.value,
                "task": task.to_primitive(),
                "trellis_graph_digest": self._manifest.trellis_graph_digest,
            }
        )
        return self._append_payload(
            JournalEventType.DISPATCH_REQUESTED,
            identity,
            EffectRequestPayload(
                EffectOperation.WORKER_DISPATCH,
                AdapterKind.TASK,
                EffectObjectType.WORKER,
                target_hash,
                request_hash,
                self._cursor.head.sequence,
                self._fencing_token,
            ),
        )

    def _append_transition(
        self,
        event_type: JournalEventType,
        identity: ExecutionIdentity,
        subject: TransitionSubject,
        from_state: RuntimeState,
        to_state: RuntimeState,
        *,
        actor_type: ActorType = ActorType.COORDINATOR,
        actor_id: str | None = None,
        reason_code: RuntimeReasonCode | None = None,
        evidence: tuple[EvidenceRef, ...] = (),
        allow_recovery: bool = False,
    ) -> _AppendOutcome:
        return self._append_payload(
            event_type,
            identity,
            TransitionPayload(subject, from_state, to_state, evidence),
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code=reason_code,
            allow_recovery=allow_recovery,
        )

    def _append_payload(
        self,
        event_type: JournalEventType,
        identity: ExecutionIdentity,
        payload: (
            TransitionPayload
            | EffectRequestPayload
            | EffectObservationPayload
            | DispatchRecoveryPayload
        ),
        *,
        actor_type: ActorType = ActorType.COORDINATOR,
        actor_id: str | None = None,
        reason_code: RuntimeReasonCode | None = None,
        allow_recovery: bool = False,
    ) -> _AppendOutcome:
        admission = self._admission_reason(allow_recovery=allow_recovery)
        if admission is not CoordinatorReason.NONE:
            return _AppendOutcome(None, admission)
        sequence = self._cursor.head.sequence + 1
        draft = JournalEventDraft(
            event_id=self._event_id_factory(event_type, sequence, identity),
            event_type=event_type,
            identity=identity,
            actor_type=actor_type,
            actor_id=actor_id or self._coordinator_id,
            payload=payload,
            reason_code=reason_code,
        )
        append = self._journal.append_draft(draft, expected_head=self._cursor.head)
        if not append.durable or append.event is None:
            reason = (
                CoordinatorReason.JOURNAL_CONFLICT
                if append.status is AppendStatus.CONFLICT
                else CoordinatorReason.PERSISTENCE_FAILED
            )
            return _AppendOutcome(None, reason, append)
        event = append.event
        advanced = self._advance_committed_event(event)
        if advanced is not CoordinatorReason.NONE:
            return _AppendOutcome(None, advanced, append)
        return _AppendOutcome(event, CoordinatorReason.NONE, append)

    def _adopt_committed_events(
        self,
        events: tuple[JournalEvent, ...],
    ) -> CoordinatorReason:
        for event in events:
            advanced = self._advance_committed_event(event)
            if advanced is not CoordinatorReason.NONE:
                return advanced
        return CoordinatorReason.NONE

    def _advance_committed_event(self, event: JournalEvent) -> CoordinatorReason:
        if (
            type(event) is not JournalEvent
            or event.sequence != self._cursor.head.sequence + 1
            or event.previous_event_hash != self._cursor.head.event_hash
        ):
            return CoordinatorReason.STATE_REJECTED
        previous = self._cursor.snapshot
        applied = apply_journal_event(previous, event)
        if applied.accepted:
            current = applied.snapshot
        elif applied.reason is ApplyReason.UNSUPPORTED_EVENT:
            current = replace(
                previous,
                last_sequence=event.sequence,
                last_event_id=event.event_id,
                last_event_hash=event.event_hash,
            )
        else:
            return CoordinatorReason.STATE_REJECTED
        try:
            lease_state = self._cursor.lease_state.advance(event)
            graph_index = self._cursor.graph_index.advance(previous, current)
            dispatch_recoveries = advance_dispatch_recoveries(
                self._cursor.dispatch_recoveries,
                event,
            )
            self._cursor = CoordinatorCursor(
                current,
                graph_index,
                lease_state,
                dispatch_recoveries,
            )
        except DispatchRecoveryProjectionError:
            return CoordinatorReason.RECOVERY_CONFLICT
        except (TypeError, ValueError):
            return CoordinatorReason.INDEX_MISMATCH
        return CoordinatorReason.NONE

    def _admission_reason(
        self,
        *,
        allow_recovery: bool = False,
    ) -> CoordinatorReason:
        if self._journal.blocked:
            return CoordinatorReason.PERSISTENCE_FAILED
        if not self._cursor.graph_index.verify(self._manifest, self._cursor.snapshot):
            return CoordinatorReason.INDEX_MISMATCH
        if not allow_recovery and any(
            not record.complete for record in self._cursor.dispatch_recoveries
        ):
            return CoordinatorReason.RECOVERY_IN_PROGRESS
        authority_time = self._authority_clock()
        if type(authority_time) is not datetime or authority_time.tzinfo is None:
            raise ValueError("authority_clock must return a timezone-aware datetime")
        if not self._cursor.lease_state.allows_admission(
            authority_time=authority_time,
            coordinator_id=self._coordinator_id,
            owner=self._owner,
            fencing_token=self._fencing_token,
            manifest_digest=self._manifest_digest,
            scheduler_mode=SchedulerMode.WISH_BUILDER,
        ):
            return CoordinatorReason.LEASE_NOT_ADMITTED
        try:
            graph_admitted = self._execution_snapshot_admitter()
        except Exception:  # noqa: BLE001 - external admission failures stop dispatch
            graph_admitted = False
        if graph_admitted is not True:
            return CoordinatorReason.GRAPH_SNAPSHOT_NOT_ADMITTED
        return CoordinatorReason.NONE

    def _proof_matches_request(
        self,
        proof: DispatchRecoveryPayload,
        request_event: JournalEvent,
    ) -> bool:
        payload = request_event.payload
        return (
            request_event.event_type is JournalEventType.DISPATCH_REQUESTED
            and type(payload) is EffectRequestPayload
            and payload.adapter is AdapterKind.TASK
            and payload.operation is EffectOperation.WORKER_DISPATCH
            and payload.object_type is EffectObjectType.WORKER
            and request_event.identity == proof.subject_identity
            and request_event.event_id == proof.request_event_id
            and request_event.sequence == proof.request_sequence
            and request_event.event_hash == proof.request_event_hash
            and payload.fencing_token
            == proof.subject_identity.coordinator_epoch
            and self._fencing_token > proof.subject_identity.coordinator_epoch
        )

    def _unknown_dispatch_state_matches(
        self,
        identity: ExecutionIdentity,
    ) -> bool:
        resolved = {
            record.payload.subject_identity
            for record in self._cursor.dispatch_recoveries
            if record.complete
        }
        unknown = tuple(
            attempt
            for attempt in self._cursor.snapshot.attempts
            if attempt.state is RuntimeState.OUTCOME_UNKNOWN
            and ExecutionIdentity(
                self._manifest.run_id,
                attempt.coordinator_epoch,
                attempt.task_id,
                attempt.attempt,
                attempt.correlation_id,
            )
            not in resolved
        )
        if len(unknown) != 1:
            return False
        attempt = unknown[0]
        task_state = dict(self._cursor.graph_index.task_states).get(
            identity.task_id or ""
        )
        return (
            self._cursor.snapshot.status is RuntimeState.BLOCKED
            and task_state is RuntimeState.BLOCKED
            and attempt.task_id == identity.task_id
            and attempt.attempt == identity.attempt
            and attempt.correlation_id == identity.correlation_id
            and attempt.coordinator_epoch == identity.coordinator_epoch
        )

    def _recovery_prefix_state_matches(
        self,
        record: DispatchRecoveryRecord,
    ) -> bool:
        identity = record.payload.subject_identity
        task_id = identity.task_id or ""
        task_state = dict(self._cursor.graph_index.task_states).get(task_id)
        attempt = self._matching_attempt(identity)
        if attempt is None or attempt.state is not RuntimeState.OUTCOME_UNKNOWN:
            return False
        if record.task_retry_event is None:
            return (
                self._cursor.snapshot.status is RuntimeState.BLOCKED
                and task_state is RuntimeState.BLOCKED
            )
        return (
            self._cursor.snapshot.status is RuntimeState.BLOCKED
            and task_state is RuntimeState.READY
        )

    def _receipt_from_outcome(
        self,
        outcome: object,
        identity: ExecutionIdentity,
    ) -> EffectReceipt | None:
        if (
            type(outcome) is not OperationOutcome
            or outcome.kind is not OutcomeKind.SUCCESS
            or type(outcome.value) is not EffectReceiptValue
        ):
            return None
        receipt = outcome.value.receipt
        if (
            receipt.identity != identity
            or receipt.operation is not EffectOperation.WORKER_DISPATCH
        ):
            return None
        return receipt

    def _next_attempt(self, task_id: str) -> int:
        attempts = [
            attempt.attempt
            for attempt in self._cursor.snapshot.attempts
            if attempt.task_id == task_id
        ]
        return max(attempts, default=0) + 1

    def _matching_attempt(
        self,
        identity: ExecutionIdentity,
    ) -> AttemptProjection | None:
        for attempt in self._cursor.snapshot.attempts:
            if (
                attempt.task_id == identity.task_id
                and attempt.attempt == identity.attempt
                and attempt.correlation_id == identity.correlation_id
                and attempt.coordinator_epoch == identity.coordinator_epoch
            ):
                return attempt
        return None

    def _current_epoch_identity(
        self,
        identity: ExecutionIdentity,
    ) -> ExecutionIdentity:
        return ExecutionIdentity(
            identity.run_id,
            self._fencing_token,
            identity.task_id,
            identity.attempt,
            identity.correlation_id,
        )

    def _is_pending_request(self, event: JournalEvent) -> bool:
        if (
            event.event_type is not JournalEventType.DISPATCH_REQUESTED
            or type(event.payload) is not EffectRequestPayload
            or event.payload.adapter is not AdapterKind.TASK
            or event.payload.operation is not EffectOperation.WORKER_DISPATCH
            or event.payload.object_type is not EffectObjectType.WORKER
            or event.identity.coordinator_epoch > self._fencing_token
        ):
            return False
        attempt = self._matching_attempt(event.identity)
        return attempt is not None and attempt.state is RuntimeState.DISPATCH_REQUESTED

    def _result(
        self,
        status: CoordinatorStatus,
        reason: CoordinatorReason,
        *,
        events: tuple[JournalEvent, ...] = (),
        dispatched: tuple[ExecutionIdentity, ...] = (),
        receipt: EffectReceipt | None = None,
    ) -> CoordinatorStepResult:
        return CoordinatorStepResult(
            status,
            reason,
            self._cursor,
            events,
            dispatched,
            receipt,
        )

    def _reservation_result(
        self,
        status: CoordinatorStatus,
        reason: CoordinatorReason,
        *,
        events: tuple[JournalEvent, ...] = (),
        reserved: tuple[ExecutionIdentity, ...] = (),
    ) -> CoordinatorReservationResult:
        return CoordinatorReservationResult(
            status,
            reason,
            self._cursor,
            events,
            reserved,
        )


__all__ = [
    "AuthorityClock",
    "CoordinatorCursor",
    "CoordinatorReservationResult",
    "CoordinatorReason",
    "CoordinatorStatus",
    "CoordinatorStepResult",
    "CorrelationIdFactory",
    "EventIdFactory",
    "ForegroundCoordinator",
    "WorkerResultProposal",
]
