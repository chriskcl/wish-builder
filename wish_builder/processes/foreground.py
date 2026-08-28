"""Bounded foreground composition for one frozen Trellis execution snapshot.

The coordinator remains the only scheduler.  This module orders already-built
M1 boundaries and deliberately has no provider fallback: an unqualified
backend is rejected before any injected worker or Git component is reached.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from inspect import getattr_static
from typing import Protocol, runtime_checkable

from wish_builder.contracts.manifest_v2 import ExecutionManifestV2
from wish_builder.contracts.runtime import (
    DecisionObservedPayload,
    DecisionRequestPayload,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
)
from wish_builder.kernel.state import apply_journal_event
from wish_builder.processes.coordinator import (
    CoordinatorCursor,
    CoordinatorReservationResult,
    CoordinatorStatus,
    CoordinatorStepResult,
    WorkerResultProposal,
)
from wish_builder.processes.workflow import (
    AcceptancePort,
    AttemptPreparationResult,
    CleanupStepResult,
    PromotionBatchResult,
    ResultStageResult,
    WorkflowReason,
    WorkflowStatus,
)
from wish_builder.services.backend_admission import (
    BackendAdmissionReason,
    BackendAdmissionResult,
    admit_backend,
)
from wish_builder.services.execution_checkpoints import (
    ExecutionCheckpointResult,
    ExecutionCheckpointStatus,
)
from wish_builder.services.execution_admission import ExecutionAdmissionResult


class ForegroundRunStatus(StrEnum):
    """Terminal result of one bounded foreground invocation."""

    COMPLETED = "completed"
    REJECTED = "rejected"
    BLOCKED = "blocked"


class ForegroundRunStage(StrEnum):
    """Last lifecycle boundary reached by a foreground invocation."""

    BACKEND_ADMISSION = "backend_admission"
    MANIFEST_VALIDATION = "manifest_validation"
    CONTROL_ROOT = "control_root"
    WORKSPACE_IDENTITY = "workspace_identity"
    RECOVERY = "recovery"
    LEASE = "lease"
    COORDINATOR = "coordinator"
    ATTEMPT_WORKFLOW = "attempt_workflow"
    WORKER = "worker"
    ACCEPTANCE = "acceptance"
    CLEANUP = "cleanup"
    CHECKPOINT = "checkpoint"
    TERMINAL_LIFECYCLE = "terminal_lifecycle"


class ForegroundRunReason(StrEnum):
    """Closed, CLI-stable reasons emitted by foreground composition."""

    NONE = "none"
    DISPATCH_NOT_QUALIFIED = "dispatch_not_qualified"
    BACKEND_NOT_ADMITTED = "backend_not_admitted"
    COMPOSITION_UNAVAILABLE = "composition_unavailable"
    MANIFEST_NOT_ADMITTED = "manifest_not_admitted"
    CONTROL_ROOT_NOT_PROTECTED = "control_root_not_protected"
    WORKSPACE_IDENTITY_NOT_VERIFIED = "workspace_identity_not_verified"
    RECOVERY_NOT_VERIFIED = "recovery_not_verified"
    LEASE_NOT_ADMITTED = "lease_not_admitted"
    COORDINATOR_BLOCKED = "coordinator_blocked"
    COORDINATOR_IDLE = "coordinator_idle"
    WORKFLOW_BLOCKED = "workflow_blocked"
    WORKER_OUTCOME_UNKNOWN = "worker_outcome_unknown"
    WORKER_RESULT_INVALID = "worker_result_invalid"
    WORKER_FAILED = "worker_failed"
    ACCEPTANCE_FAILED = "acceptance_failed"
    CLEANUP_BLOCKED = "cleanup_blocked"
    CHECKPOINT_BLOCKED = "checkpoint_blocked"
    TERMINAL_LIFECYCLE_BLOCKED = "terminal_lifecycle_blocked"
    BOUNDED_RUN_EXHAUSTED = "bounded_run_exhausted"
    INVARIANT_VIOLATION = "invariant_violation"


@dataclass(frozen=True, slots=True)
class PreparedForegroundAttempt:
    """A coordinator-selected identity paired with its prepared Git attempt."""

    identity: ExecutionIdentity
    attempt: object

    def __post_init__(self) -> None:
        if type(self.identity) is not ExecutionIdentity or not self.identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        if self.attempt is None:
            raise ValueError("attempt must not be null")


@dataclass(frozen=True, slots=True)
class WorkerBatchResult:
    """All-known worker results, or an explicit fail-closed unknown batch."""

    outcomes_known: bool
    proposals: tuple[WorkerResultProposal, ...] = ()
    cursor: CoordinatorCursor | None = None
    events: tuple[JournalEvent, ...] = ()

    def __post_init__(self) -> None:
        if type(self.outcomes_known) is not bool:
            raise TypeError("outcomes_known must be a bool")
        if type(self.proposals) is not tuple or not all(
            type(item) is WorkerResultProposal for item in self.proposals
        ):
            raise TypeError("proposals must contain WorkerResultProposal values")
        if self.cursor is not None and type(self.cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor or null")
        if type(self.events) is not tuple or not all(
            type(event) is JournalEvent for event in self.events
        ):
            raise TypeError("events must contain JournalEvent values")
        if not self.outcomes_known and self.proposals:
            raise ValueError("unknown batches cannot expose partially trusted results")
        if self.events and self.cursor is None:
            raise ValueError("worker events require their resulting cursor")
        if any(
            event.event_type is not JournalEventType.LEASE_RENEWED
            for event in self.events
        ):
            raise ValueError("worker batches may only expose lease renewal events")
        if any(
            current.sequence != previous.sequence + 1
            or current.previous_event_hash != previous.event_hash
            for previous, current in zip(self.events, self.events[1:], strict=False)
        ):
            raise ValueError("worker lease renewal events must form one Journal chain")
        if self.events and (
            self.events[-1].sequence != self.cursor.head.sequence
            or self.events[-1].event_hash != self.cursor.head.event_hash
        ):
            raise ValueError("worker cursor must end at the last renewal event")


@dataclass(frozen=True, slots=True)
class WorkerLeaseRenewalResult:
    """One durable lease renewal returned by an injected worker callback."""

    succeeded: bool
    cursor: CoordinatorCursor | None = None
    event: JournalEvent | None = None

    def __post_init__(self) -> None:
        if type(self.succeeded) is not bool:
            raise TypeError("succeeded must be a bool")
        if self.succeeded:
            if type(self.cursor) is not CoordinatorCursor:
                raise TypeError("successful renewals require a CoordinatorCursor")
            if type(self.event) is not JournalEvent:
                raise TypeError("successful renewals require a JournalEvent")
            if self.event.event_type is not JournalEventType.LEASE_RENEWED:
                raise ValueError("renewal event must be LEASE_RENEWED")
            if (
                self.event.sequence != self.cursor.head.sequence
                or self.event.event_hash != self.cursor.head.event_hash
            ):
                raise ValueError("renewal cursor must end at the renewal event")
        elif self.cursor is not None or self.event is not None:
            raise ValueError("failed renewals cannot expose a cursor or event")


@dataclass(frozen=True, slots=True)
class ForegroundTerminalResult:
    """Result of durable run-finalization and lease release composition."""

    completed: bool
    cursor: CoordinatorCursor
    events: tuple[JournalEvent, ...] = ()

    def __post_init__(self) -> None:
        if type(self.completed) is not bool:
            raise TypeError("completed must be a bool")
        if type(self.cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        if type(self.events) is not tuple or not all(
            type(event) is JournalEvent for event in self.events
        ):
            raise TypeError("events must contain JournalEvent values")
        if self.completed:
            canonical = (
                JournalEventType.EXECUTION_COMPLETED,
                JournalEventType.INTEGRATION_VERIFIED,
                JournalEventType.QUALITY_DOCS_VERIFIED,
                JournalEventType.LEASE_RELEASED,
            )
            if (
                not self.events
                or self.events[-1].event_type is not JournalEventType.LEASE_RELEASED
                or any(event.event_type not in canonical for event in self.events)
                or tuple(canonical.index(event.event_type) for event in self.events)
                != tuple(
                    sorted(canonical.index(event.event_type) for event in self.events)
                )
                or len({event.event_type for event in self.events}) != len(self.events)
                or any(
                    current.sequence != previous.sequence + 1
                    or current.previous_event_hash != previous.event_hash
                    for previous, current in zip(
                        self.events,
                        self.events[1:],
                        strict=False,
                    )
                )
                or self.events[-1].sequence != self.cursor.head.sequence
                or self.events[-1].event_hash != self.cursor.head.event_hash
                or self.cursor.snapshot.phase is not RuntimeState.COMPLETE
                or self.cursor.lease_state.active
                or any(
                    state not in {RuntimeState.VERIFIED, RuntimeState.ARCHIVED}
                    for _, state in self.cursor.graph_index.task_states
                )
            ):
                raise ValueError(
                    "completed terminal results require verified tasks, a valid "
                    "terminal suffix, and a released lease"
                )


@dataclass(frozen=True, slots=True)
class ForegroundRunResult:
    """Stable result consumed by the CLI and recovery-aware callers."""

    status: ForegroundRunStatus
    reason: ForegroundRunReason
    stage: ForegroundRunStage
    backend_admission: BackendAdmissionResult
    cursor: CoordinatorCursor | None = None
    completed_task_ids: tuple[str, ...] = ()
    batch_count: int = 0

    def __post_init__(self) -> None:
        if type(self.status) is not ForegroundRunStatus:
            raise TypeError("status must be a ForegroundRunStatus")
        if type(self.reason) is not ForegroundRunReason:
            raise TypeError("reason must be a ForegroundRunReason")
        if type(self.stage) is not ForegroundRunStage:
            raise TypeError("stage must be a ForegroundRunStage")
        if type(self.backend_admission) is not BackendAdmissionResult:
            raise TypeError("backend_admission must be a BackendAdmissionResult")
        if self.cursor is not None and type(self.cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor or null")
        if type(self.completed_task_ids) is not tuple or not all(
            type(task_id) is str and task_id for task_id in self.completed_task_ids
        ):
            raise TypeError("completed_task_ids must contain non-empty strings")
        if len(set(self.completed_task_ids)) != len(self.completed_task_ids):
            raise ValueError("completed_task_ids must be unique")
        if type(self.batch_count) is not int or self.batch_count < 0:
            raise ValueError("batch_count must be non-negative")
        if self.status is ForegroundRunStatus.COMPLETED:
            if self.reason is not ForegroundRunReason.NONE or self.cursor is None:
                raise ValueError(
                    "completed runs require a cursor and no failure reason"
                )
        elif self.reason is ForegroundRunReason.NONE:
            raise ValueError("non-completed runs require a failure reason")


@runtime_checkable
class ForegroundCoordinatorPort(Protocol):
    def reserve_ready(
        self,
        *,
        limit: int | None = None,
    ) -> CoordinatorReservationResult: ...

    def dispatch_reserved(
        self,
        identity: ExecutionIdentity,
    ) -> CoordinatorStepResult: ...

    def accept_worker_result(
        self,
        proposal: WorkerResultProposal,
    ) -> CoordinatorStepResult: ...


@runtime_checkable
class ForegroundWorkflowPort(Protocol):
    def prepare_attempt(
        self,
        identity: ExecutionIdentity,
    ) -> AttemptPreparationResult: ...

    def stage_attempt_result(self, attempt: object) -> ResultStageResult: ...

    def promote_staged(
        self,
        sources: tuple[object, ...],
        acceptance: AcceptancePort,
    ) -> PromotionBatchResult: ...


@runtime_checkable
class ForegroundRunComponents(Protocol):
    """Narrow composition surface around the existing M1 components.

    Implementations may wrap ``CoordinatorLeaseService``,
    ``ForegroundCoordinator``, ``LocalExecutionWorkflow``,
    ``ExecutionCheckpointPublisher`` and the qualified provider adapter.  The
    service never supplies a fake implementation on their behalf.
    """

    @property
    def acceptance(self) -> AcceptancePort: ...

    def validate_execution(
        self,
        manifest: ExecutionManifestV2,
    ) -> ExecutionAdmissionResult: ...

    def protect_control_root(self) -> bool: ...

    def verify_workspace_identity(self, manifest: ExecutionManifestV2) -> bool: ...

    def recover_verified_cursor(
        self,
        manifest: ExecutionManifestV2,
    ) -> CoordinatorCursor | None: ...

    def acquire_lease(self, cursor: CoordinatorCursor) -> CoordinatorCursor | None: ...

    def coordinator(self, cursor: CoordinatorCursor) -> ForegroundCoordinatorPort: ...

    def workflow(self, cursor: CoordinatorCursor) -> ForegroundWorkflowPort: ...

    def run_workers(
        self,
        attempts: tuple[PreparedForegroundAttempt, ...],
        cursor: CoordinatorCursor,
    ) -> WorkerBatchResult: ...

    def cleanup_attempt(
        self,
        workflow: ForegroundWorkflowPort,
        attempt: object,
        promotion: object,
    ) -> CleanupStepResult: ...

    def publish_checkpoint(
        self,
        cursor: CoordinatorCursor,
        events: tuple[JournalEvent, ...],
    ) -> ExecutionCheckpointResult: ...

    def finish(self, cursor: CoordinatorCursor) -> ForegroundTerminalResult: ...


ForegroundRunComponentsFactory = Callable[[], ForegroundRunComponents]

_FOREGROUND_COMPONENT_METHODS = (
    "validate_execution",
    "protect_control_root",
    "verify_workspace_identity",
    "recover_verified_cursor",
    "acquire_lease",
    "coordinator",
    "workflow",
    "run_workers",
    "cleanup_attempt",
    "publish_checkpoint",
    "finish",
)


def _is_foreground_run_components(candidate: object) -> bool:
    """Check the composition shape without evaluating descriptors."""

    try:
        getattr_static(candidate, "acceptance")
        for name in _FOREGROUND_COMPONENT_METHODS:
            member = getattr_static(candidate, name)
            if isinstance(member, (classmethod, staticmethod)):
                member = member.__func__
            if not callable(member):
                return False
    except AttributeError:
        return False
    return True


class ForegroundRunService:
    """Run complete coordinator-selected batches behind one backend admission."""

    def __init__(
        self,
        manifest: ExecutionManifestV2,
        *,
        components: ForegroundRunComponents | None = None,
        components_factory: ForegroundRunComponentsFactory | None = None,
        backend_admitter=admit_backend,
        max_batches: int | None = None,
    ) -> None:
        if type(manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if components is not None and not _is_foreground_run_components(components):
            raise TypeError(
                "components must implement ForegroundRunComponents or be null"
            )
        if components_factory is not None and not callable(components_factory):
            raise TypeError("components_factory must be callable or null")
        if components is not None and components_factory is not None:
            raise ValueError("components and components_factory are mutually exclusive")
        if not callable(backend_admitter):
            raise TypeError("backend_admitter must be callable")
        if max_batches is not None and (
            type(max_batches) is not int or max_batches <= 0
        ):
            raise ValueError("max_batches must be a positive integer or null")
        self._manifest = manifest
        self._components = components
        self._components_factory = components_factory
        self._backend_admitter = backend_admitter
        self._max_batches = len(manifest.tasks) if max_batches is None else max_batches

    def run(self) -> ForegroundRunResult:
        """Run once and release only components created by this service."""

        owned_component: object | None = None

        def tracked_factory() -> ForegroundRunComponents:
            nonlocal owned_component
            assert self._components_factory is not None
            owned_component = self._components_factory()
            return owned_component  # type: ignore[return-value]

        factory = tracked_factory if self._components_factory is not None else None
        try:
            return self._run_impl(factory)
        finally:
            close = getattr(owned_component, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    # The foreground result already records the durable boundary
                    # reached by the run. Resource release must not mask it.
                    pass

    def _run_impl(
        self,
        components_factory: ForegroundRunComponentsFactory | None,
    ) -> ForegroundRunResult:
        """Execute a bounded foreground lifecycle, failing closed at every boundary.

        Backend qualification is the first, side-effect-free rejection.  An
        admitted path then runs execution evidence, control-root and workspace
        checks before recovery, lease/fencing, coordinator-selected batches,
        Git/acceptance, cleanup, checkpoint publication and terminal Journal
        finalization in that order.
        """

        try:
            admission = self._backend_admitter(self._manifest)
        except Exception:
            admission = BackendAdmissionResult(
                False,
                BackendAdmissionReason.CAPABILITY_MISMATCH,
            )
        if type(admission) is not BackendAdmissionResult:
            admission = BackendAdmissionResult(
                False,
                BackendAdmissionReason.CAPABILITY_MISMATCH,
            )
        if not admission.admitted:
            reason = (
                ForegroundRunReason.DISPATCH_NOT_QUALIFIED
                if admission.reason is BackendAdmissionReason.DISPATCH_NOT_QUALIFIED
                else ForegroundRunReason.BACKEND_NOT_ADMITTED
            )
            return self._result(
                ForegroundRunStatus.REJECTED,
                reason,
                ForegroundRunStage.BACKEND_ADMISSION,
                admission,
            )

        components = self._components
        if components is None and components_factory is not None:
            try:
                candidate = components_factory()
            except Exception:
                candidate = None
            components = (
                candidate
                if candidate is not None and _is_foreground_run_components(candidate)
                else None
            )
        if components is None:
            return self._result(
                ForegroundRunStatus.BLOCKED,
                ForegroundRunReason.COMPOSITION_UNAVAILABLE,
                ForegroundRunStage.MANIFEST_VALIDATION,
                admission,
            )
        try:
            execution_admission = components.validate_execution(self._manifest)
        except Exception:
            execution_admission = None
        if not self._execution_admission_valid(execution_admission):
            return self._blocked(
                ForegroundRunReason.MANIFEST_NOT_ADMITTED,
                ForegroundRunStage.MANIFEST_VALIDATION,
                admission,
            )
        try:
            control_root_protected = components.protect_control_root()
        except Exception:
            control_root_protected = False
        if control_root_protected is not True:
            return self._blocked(
                ForegroundRunReason.CONTROL_ROOT_NOT_PROTECTED,
                ForegroundRunStage.CONTROL_ROOT,
                admission,
            )
        try:
            workspace_verified = components.verify_workspace_identity(self._manifest)
        except Exception:
            workspace_verified = False
        if workspace_verified is not True:
            return self._blocked(
                ForegroundRunReason.WORKSPACE_IDENTITY_NOT_VERIFIED,
                ForegroundRunStage.WORKSPACE_IDENTITY,
                admission,
            )
        try:
            cursor = components.recover_verified_cursor(self._manifest)
        except Exception:
            cursor = None
        if not self._cursor_valid(cursor):
            return self._blocked(
                ForegroundRunReason.RECOVERY_NOT_VERIFIED,
                ForegroundRunStage.RECOVERY,
                admission,
            )
        assert cursor is not None
        known_task_ids = set(cursor.graph_index.topological_order)
        completed = self._completed_task_ids(cursor)
        if (
            set(completed) == known_task_ids
            and cursor.snapshot.phase is RuntimeState.COMPLETE
            and not cursor.lease_state.active
            and cursor.lease_state.event_type is JournalEventType.LEASE_RELEASED
        ):
            return self._finish_completed(
                components,
                cursor,
                admission,
                completed,
                batch_count=0,
            )
        try:
            cursor = components.acquire_lease(cursor)
        except Exception:
            cursor = None
        if not self._lease_cursor_valid(cursor):
            return self._blocked(
                ForegroundRunReason.LEASE_NOT_ADMITTED,
                ForegroundRunStage.LEASE,
                admission,
            )
        assert cursor is not None

        completed = self._completed_task_ids(cursor)
        if set(completed) == known_task_ids:
            return self._finish_completed(
                components,
                cursor,
                admission,
                completed,
                batch_count=0,
            )
        for batch_count in range(1, self._max_batches + 1):
            try:
                reservation = components.coordinator(cursor).reserve_ready()
            except Exception:
                return self._blocked(
                    ForegroundRunReason.COORDINATOR_BLOCKED,
                    ForegroundRunStage.COORDINATOR,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            if type(
                reservation
            ) is not CoordinatorReservationResult or not self._cursor_valid(
                reservation.cursor
            ):
                return self._blocked(
                    ForegroundRunReason.INVARIANT_VIOLATION,
                    ForegroundRunStage.COORDINATOR,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            cursor = reservation.cursor
            if reservation.status is not CoordinatorStatus.PROGRESSED:
                reason = (
                    ForegroundRunReason.COORDINATOR_IDLE
                    if reservation.status is CoordinatorStatus.IDLE
                    else ForegroundRunReason.COORDINATOR_BLOCKED
                )
                return self._blocked(
                    reason,
                    ForegroundRunStage.COORDINATOR,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            identities = reservation.reserved
            identity_task_ids = tuple(identity.task_id for identity in identities)
            if (
                not identities
                or any(not identity.is_attempt for identity in identities)
                or any(task_id not in known_task_ids for task_id in identity_task_ids)
                or len(set(identity_task_ids)) != len(identity_task_ids)
                or any(task_id in completed for task_id in identity_task_ids)
            ):
                return self._blocked(
                    ForegroundRunReason.INVARIANT_VIOLATION,
                    ForegroundRunStage.COORDINATOR,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )

            prepared: list[PreparedForegroundAttempt] = []
            batch_events = list(reservation.events)
            for identity in identities:
                try:
                    preparation = components.workflow(cursor).prepare_attempt(identity)
                except Exception:
                    preparation = None
                if (
                    type(preparation) is not AttemptPreparationResult
                    or preparation.status is not WorkflowStatus.PROGRESSED
                    or preparation.attempt is None
                    or not self._cursor_valid(preparation.cursor)
                ):
                    return self._blocked(
                        ForegroundRunReason.WORKFLOW_BLOCKED,
                        ForegroundRunStage.ATTEMPT_WORKFLOW,
                        admission,
                        cursor,
                        completed,
                        batch_count - 1,
                    )
                cursor = preparation.cursor
                batch_events.extend(preparation.events)
                prepared.append(
                    PreparedForegroundAttempt(identity, preparation.attempt)
                )

            for identity in identities:
                try:
                    dispatched = components.coordinator(cursor).dispatch_reserved(
                        identity
                    )
                except Exception:
                    dispatched = None
                if (
                    type(dispatched) is not CoordinatorStepResult
                    or dispatched.status is not CoordinatorStatus.PROGRESSED
                    or dispatched.dispatched != (identity,)
                    or not self._cursor_valid(dispatched.cursor)
                ):
                    return self._blocked(
                        ForegroundRunReason.COORDINATOR_BLOCKED,
                        ForegroundRunStage.WORKER,
                        admission,
                        cursor,
                        completed,
                        batch_count - 1,
                    )
                cursor = dispatched.cursor
                batch_events.extend(dispatched.events)

            try:
                worker_batch = components.run_workers(tuple(prepared), cursor)
            except Exception:
                worker_batch = None
            if type(worker_batch) is not WorkerBatchResult:
                return self._blocked(
                    ForegroundRunReason.WORKER_OUTCOME_UNKNOWN,
                    ForegroundRunStage.WORKER,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            worker_cursor = self._worker_batch_cursor(cursor, worker_batch)
            if worker_cursor is None:
                return self._blocked(
                    ForegroundRunReason.WORKER_RESULT_INVALID,
                    ForegroundRunStage.WORKER,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            cursor = worker_cursor
            batch_events.extend(worker_batch.events)
            if not worker_batch.outcomes_known:
                return self._blocked(
                    ForegroundRunReason.WORKER_OUTCOME_UNKNOWN,
                    ForegroundRunStage.WORKER,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            proposals = {
                proposal.identity: proposal for proposal in worker_batch.proposals
            }
            if len(proposals) != len(worker_batch.proposals) or set(proposals) != set(
                identities
            ):
                return self._blocked(
                    ForegroundRunReason.WORKER_RESULT_INVALID,
                    ForegroundRunStage.WORKER,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            prepared_by_identity = {item.identity: item for item in prepared}
            successful_prepared: list[PreparedForegroundAttempt] = []
            worker_failed = False
            for identity in identities:
                proposal = proposals[identity]
                try:
                    accepted = components.coordinator(cursor).accept_worker_result(
                        proposal
                    )
                except Exception:
                    accepted = None
                if (
                    type(accepted) is not CoordinatorStepResult
                    or accepted.status is not CoordinatorStatus.PROGRESSED
                    or not self._cursor_valid(accepted.cursor)
                ):
                    return self._blocked(
                        ForegroundRunReason.COORDINATOR_BLOCKED,
                        ForegroundRunStage.WORKER,
                        admission,
                        cursor,
                        completed,
                        batch_count - 1,
                    )
                cursor = accepted.cursor
                batch_events.extend(accepted.events)
                if proposal.succeeded:
                    successful_prepared.append(prepared_by_identity[identity])
                else:
                    worker_failed = True
            if worker_failed and not successful_prepared:
                return self._blocked(
                    ForegroundRunReason.WORKER_FAILED,
                    ForegroundRunStage.WORKER,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )

            successful_task_ids = tuple(
                item.identity.task_id for item in successful_prepared
            )
            assert all(task_id is not None for task_id in successful_task_ids)
            staged: list[object] = []
            for item in successful_prepared:
                try:
                    stage_result = components.workflow(cursor).stage_attempt_result(
                        item.attempt
                    )
                except Exception:
                    stage_result = None
                if (
                    type(stage_result) is not ResultStageResult
                    or stage_result.status is not WorkflowStatus.PROGRESSED
                    or stage_result.staged is None
                    or not self._cursor_valid(stage_result.cursor)
                ):
                    return self._blocked(
                        ForegroundRunReason.WORKFLOW_BLOCKED,
                        ForegroundRunStage.ATTEMPT_WORKFLOW,
                        admission,
                        cursor,
                        completed,
                        batch_count - 1,
                    )
                cursor = stage_result.cursor
                batch_events.extend(stage_result.events)
                staged.append(stage_result.staged)

            try:
                promotion = components.workflow(cursor).promote_staged(
                    tuple(staged),
                    components.acceptance,
                )
            except Exception:
                promotion = None
            if (
                type(promotion) is not PromotionBatchResult
                or promotion.status is not WorkflowStatus.PROGRESSED
                or len(promotion.promoted) != len(successful_prepared)
                or not self._cursor_valid(promotion.cursor)
            ):
                reason = (
                    ForegroundRunReason.ACCEPTANCE_FAILED
                    if type(promotion) is PromotionBatchResult
                    and promotion.reason is WorkflowReason.ACCEPTANCE_FAILED
                    else ForegroundRunReason.WORKFLOW_BLOCKED
                )
                return self._blocked(
                    reason,
                    ForegroundRunStage.ACCEPTANCE,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            cursor = promotion.cursor
            batch_events.extend(promotion.events)
            promotions = {
                getattr(item, "task_id", None): item for item in promotion.promoted
            }
            if set(promotions) != set(successful_task_ids):
                return self._blocked(
                    ForegroundRunReason.INVARIANT_VIOLATION,
                    ForegroundRunStage.ACCEPTANCE,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )

            for item in successful_prepared:
                assert item.identity.task_id is not None
                try:
                    workflow = components.workflow(cursor)
                    cleanup = components.cleanup_attempt(
                        workflow,
                        item.attempt,
                        promotions[item.identity.task_id],
                    )
                except Exception:
                    cleanup = None
                if (
                    type(cleanup) is not CleanupStepResult
                    or cleanup.status is not WorkflowStatus.PROGRESSED
                    or not self._cursor_valid(cleanup.cursor)
                ):
                    return self._blocked(
                        ForegroundRunReason.CLEANUP_BLOCKED,
                        ForegroundRunStage.CLEANUP,
                        admission,
                        cursor,
                        completed,
                        batch_count - 1,
                    )
                cursor = cleanup.cursor
                batch_events.extend(cleanup.events)

            try:
                checkpoint = components.publish_checkpoint(cursor, tuple(batch_events))
            except Exception:
                checkpoint = None
            if (
                type(checkpoint) is not ExecutionCheckpointResult
                or checkpoint.status is ExecutionCheckpointStatus.BLOCKED
            ):
                return self._blocked(
                    ForegroundRunReason.CHECKPOINT_BLOCKED,
                    ForegroundRunStage.CHECKPOINT,
                    admission,
                    cursor,
                    completed,
                    batch_count - 1,
                )
            completed.extend(
                task_id for task_id in successful_task_ids if task_id is not None
            )
            if worker_failed:
                return self._blocked(
                    ForegroundRunReason.WORKER_FAILED,
                    ForegroundRunStage.WORKER,
                    admission,
                    cursor,
                    completed,
                    batch_count,
                )
            if set(completed) == known_task_ids:
                return self._finish_completed(
                    components,
                    cursor,
                    admission,
                    completed,
                    batch_count=batch_count,
                )

        return self._blocked(
            ForegroundRunReason.BOUNDED_RUN_EXHAUSTED,
            ForegroundRunStage.COORDINATOR,
            admission,
            cursor,
            completed,
            self._max_batches,
        )

    def _cursor_valid(self, cursor: object) -> bool:
        if type(cursor) is not CoordinatorCursor:
            return False
        try:
            return (
                cursor.snapshot.run_id == self._manifest.run_id
                and cursor.graph_index.verify(self._manifest, cursor.snapshot)
            )
        except (TypeError, ValueError):
            return False

    def _execution_admission_valid(self, value: object) -> bool:
        if type(value) is not ExecutionAdmissionResult or not value.admitted:
            return False
        request_event = value.request_event
        decision_event = value.decision_event
        frozen_event = value.frozen_event
        if (
            request_event is None
            or decision_event is None
            or frozen_event is None
            or type(request_event.payload) is not DecisionRequestPayload
            or type(decision_event.payload) is not DecisionObservedPayload
        ):
            return False
        request = request_event.payload.request
        decision_request = decision_event.payload.observation.decision.request
        return (
            request_event.identity.run_id
            == decision_event.identity.run_id
            == frozen_event.identity.run_id
            == self._manifest.run_id
            and request.candidate_hash == self._manifest.canonical_sha256()
            and decision_request == request
            and request_event.sequence < decision_event.sequence < frozen_event.sequence
            and frozen_event.event_type is JournalEventType.TASK_GRAPH_FROZEN
        )

    def _lease_cursor_valid(self, cursor: object) -> bool:
        if not self._cursor_valid(cursor):
            return False
        assert type(cursor) is CoordinatorCursor
        lease = cursor.lease_state.lease
        return bool(
            cursor.lease_state.active
            and lease is not None
            and lease.fencing_token == cursor.snapshot.coordinator_epoch
            and lease.manifest_digest == self._manifest.canonical_sha256()
            and lease.scheduler_mode is self._manifest.scheduler_mode
        )

    @staticmethod
    def _completed_task_ids(cursor: CoordinatorCursor) -> list[str]:
        return [
            task_id
            for task_id, state in cursor.graph_index.task_states
            if state in {RuntimeState.VERIFIED, RuntimeState.ARCHIVED}
        ]

    def _finish_completed(
        self,
        components: ForegroundRunComponents,
        cursor: CoordinatorCursor,
        admission: BackendAdmissionResult,
        completed: list[str],
        *,
        batch_count: int,
    ) -> ForegroundRunResult:
        pre_terminal_cursor = cursor
        try:
            terminal = components.finish(cursor)
        except Exception:
            terminal = None
        if not self._terminal_result_valid(pre_terminal_cursor, terminal):
            return self._blocked(
                ForegroundRunReason.TERMINAL_LIFECYCLE_BLOCKED,
                ForegroundRunStage.TERMINAL_LIFECYCLE,
                admission,
                cursor,
                completed,
                batch_count,
            )
        assert type(terminal) is ForegroundTerminalResult
        return self._result(
            ForegroundRunStatus.COMPLETED,
            ForegroundRunReason.NONE,
            ForegroundRunStage.TERMINAL_LIFECYCLE,
            admission,
            terminal.cursor,
            completed,
            batch_count,
        )

    def _terminal_result_valid(
        self,
        before: CoordinatorCursor,
        terminal: object,
    ) -> bool:
        if (
            type(terminal) is not ForegroundTerminalResult
            or not terminal.completed
            or not self._cursor_valid(terminal.cursor)
            or not terminal.events
        ):
            return False
        first = terminal.events[0]
        if (
            first.sequence == before.head.sequence + 1
            and first.previous_event_hash == before.head.event_hash
        ):
            return True
        return bool(
            len(terminal.events) == 1
            and first.event_type is JournalEventType.LEASE_RELEASED
            and first.sequence == before.head.sequence
            and first.event_hash == before.head.event_hash
            and terminal.cursor == before
        )

    def _worker_batch_cursor(
        self,
        current: CoordinatorCursor,
        worker_batch: WorkerBatchResult,
    ) -> CoordinatorCursor | None:
        """Rebuild the only cursor a worker-side renewal chain may produce."""

        if worker_batch.cursor is None:
            return current if not worker_batch.events else None
        if not worker_batch.events:
            return current if worker_batch.cursor == current else None

        expected = current
        try:
            for event in worker_batch.events:
                applied = apply_journal_event(expected.snapshot, event)
                if not applied.accepted:
                    return None
                expected = CoordinatorCursor(
                    applied.snapshot,
                    expected.graph_index.advance(expected.snapshot, applied.snapshot),
                    expected.lease_state.advance(event),
                    expected.dispatch_recoveries,
                )
        except (TypeError, ValueError):
            return None
        if worker_batch.cursor != expected or not self._lease_cursor_valid(expected):
            return None
        return expected

    @staticmethod
    def _result(
        status: ForegroundRunStatus,
        reason: ForegroundRunReason,
        stage: ForegroundRunStage,
        admission: BackendAdmissionResult,
        cursor: CoordinatorCursor | None = None,
        completed: list[str] | tuple[str, ...] = (),
        batch_count: int = 0,
    ) -> ForegroundRunResult:
        return ForegroundRunResult(
            status,
            reason,
            stage,
            admission,
            cursor,
            tuple(completed),
            batch_count,
        )

    def _blocked(
        self,
        reason: ForegroundRunReason,
        stage: ForegroundRunStage,
        admission: BackendAdmissionResult,
        cursor: CoordinatorCursor | None = None,
        completed: list[str] | tuple[str, ...] = (),
        batch_count: int = 0,
    ) -> ForegroundRunResult:
        return self._result(
            ForegroundRunStatus.BLOCKED,
            reason,
            stage,
            admission,
            cursor,
            completed,
            batch_count,
        )


__all__ = [
    "ForegroundCoordinatorPort",
    "ForegroundRunComponents",
    "ForegroundRunComponentsFactory",
    "ForegroundRunReason",
    "ForegroundRunResult",
    "ForegroundRunService",
    "ForegroundRunStage",
    "ForegroundRunStatus",
    "ForegroundTerminalResult",
    "ForegroundWorkflowPort",
    "PreparedForegroundAttempt",
    "WorkerBatchResult",
    "WorkerLeaseRenewalResult",
]
