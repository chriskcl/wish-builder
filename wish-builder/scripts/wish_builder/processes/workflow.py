"""Journaled local result preparation, promotion, and verification workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from wish_builder.adapters.git_worktree import (
    AttemptEffectDisposition,
    AttemptWorktree,
    GitBoundaryError,
    GitWorktreeAdapter,
    ResultValidation,
    StagedResult,
)
from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.manifest_v2 import ExecutionManifestV2, ManifestTask
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    EvidenceRef,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseOwner,
    RuntimeReasonCode,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel.state import ApplyReason, apply_journal_event
from wish_builder.services.journal import (
    AppendResult,
    AppendStatus,
    DurableJournal,
    JournalEventDraft,
)
from wish_builder.services.cleanup import (
    CleanupBoundaryError,
    CleanupCandidate,
    CleanupDisposition,
    CleanupObservation,
    CleanupService,
)
from wish_builder.services.dispatch_recovery import (
    DispatchRecoveryProjectionError,
    advance_dispatch_recoveries,
)
from wish_builder.services.ports import PreparedEffect
from wish_builder.services.promotion import (
    PromotionBoundaryError,
    PromotionDisposition,
    PromotionRecord,
    PromotionService,
)

from .coordinator import CoordinatorCursor


class WorkflowStatus(StrEnum):
    PROGRESSED = "progressed"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class WorkflowReason(StrEnum):
    NONE = "none"
    LEASE_NOT_ADMITTED = "lease_not_admitted"
    JOURNAL_CONFLICT = "journal_conflict"
    PERSISTENCE_FAILED = "persistence_failed"
    INDEX_MISMATCH = "index_mismatch"
    STATE_REJECTED = "state_rejected"
    ATTEMPT_NOT_CURRENT = "attempt_not_current"
    TASK_NOT_STAGED = "task_not_staged"
    RESULT_REJECTED = "result_rejected"
    EFFECT_ABSENT = "effect_absent"
    EFFECT_OUTCOME_UNKNOWN = "effect_outcome_unknown"
    ACCEPTANCE_FAILED = "acceptance_failed"
    PROMOTION_REJECTED = "promotion_rejected"
    CLEANUP_BLOCKED = "cleanup_blocked"
    CLEANUP_FAILED = "cleanup_failed"


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    accepted: bool
    evidence: tuple[EvidenceRef, ...]
    reason_code: RuntimeReasonCode | None = None

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a bool")
        if type(self.evidence) is not tuple or not all(
            type(item) is EvidenceRef for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of EvidenceRef values")
        if self.accepted:
            if self.reason_code is not None or not self.evidence:
                raise ValueError("accepted verification requires evidence and no reason")
        elif type(self.reason_code) is not RuntimeReasonCode:
            raise ValueError("rejected verification requires a RuntimeReasonCode")


@runtime_checkable
class AcceptancePort(Protocol):
    def verify(
        self,
        task: ManifestTask,
        repository: Path,
        promotion: PromotionRecord,
    ) -> AcceptanceResult: ...


@dataclass(frozen=True, slots=True)
class AttemptPreparationResult:
    status: WorkflowStatus
    reason: WorkflowReason
    cursor: CoordinatorCursor
    events: tuple[JournalEvent, ...]
    attempt: AttemptWorktree | None = None


@dataclass(frozen=True, slots=True)
class ResultStageResult:
    status: WorkflowStatus
    reason: WorkflowReason
    cursor: CoordinatorCursor
    events: tuple[JournalEvent, ...]
    validation: ResultValidation | None = None
    staged: StagedResult | None = None


@dataclass(frozen=True, slots=True)
class PromotionBatchResult:
    status: WorkflowStatus
    reason: WorkflowReason
    cursor: CoordinatorCursor
    events: tuple[JournalEvent, ...]
    promoted: tuple[PromotionRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class CleanupStepResult:
    status: WorkflowStatus
    reason: WorkflowReason
    cursor: CoordinatorCursor
    events: tuple[JournalEvent, ...]
    observation: CleanupObservation | None = None


@dataclass(frozen=True, slots=True)
class _AppendOutcome:
    event: JournalEvent | None
    append_result: AppendResult | None
    reason: WorkflowReason


class LocalExecutionWorkflow:
    """Compose the real local Git boundary without becoming a second scheduler."""

    def __init__(
        self,
        manifest: ExecutionManifestV2,
        cursor: CoordinatorCursor,
        journal: DurableJournal,
        repository: GitWorktreeAdapter,
        *,
        coordinator_id: str,
        owner: LeaseOwner,
        fencing_token: int,
        authority_clock,
    ) -> None:
        if type(manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if type(cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        if type(repository) is not GitWorktreeAdapter:
            raise TypeError("repository must be a GitWorktreeAdapter")
        if type(coordinator_id) is not str or not coordinator_id:
            raise ValueError("coordinator_id must be non-empty")
        if type(owner) is not LeaseOwner or owner.actor.actor_id != coordinator_id:
            raise ValueError("owner must identify the coordinator")
        if type(fencing_token) is not int or fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if not callable(authority_clock):
            raise TypeError("authority_clock must be callable")
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
        self._repository = repository
        self._coordinator_id = coordinator_id
        self._owner = owner
        self._fencing_token = fencing_token
        self._authority_clock = authority_clock
        self._promotion = PromotionService(repository, cursor.graph_index)

    @property
    def cursor(self) -> CoordinatorCursor:
        return self._cursor

    def prepare_attempt(
        self,
        identity: ExecutionIdentity,
    ) -> AttemptPreparationResult:
        """Materialize the isolated Git worktree behind a durable effect request."""

        events: list[JournalEvent] = []
        admission = self._admission_reason()
        if admission is not WorkflowReason.NONE:
            return AttemptPreparationResult(
                WorkflowStatus.BLOCKED, admission, self._cursor, ()
            )
        if not self._current_attempt(identity, RuntimeState.RESERVED):
            return AttemptPreparationResult(
                WorkflowStatus.REJECTED,
                WorkflowReason.ATTEMPT_NOT_CURRENT,
                self._cursor,
                (),
            )
        task_state = dict(self._cursor.graph_index.task_states).get(identity.task_id)
        if task_state is not RuntimeState.LEASED:
            return AttemptPreparationResult(
                WorkflowStatus.REJECTED,
                WorkflowReason.ATTEMPT_NOT_CURRENT,
                self._cursor,
                (),
            )
        task = self._task(identity.task_id)
        command = self._repository.plan_attempt(
            identity,
            owned_paths=task.owned_paths,
            protected_paths=self._manifest.protected_paths,
            allowed_auxiliary_paths=task.allowed_auxiliary_paths,
            path_case_mode=self._manifest.path_case_mode,
        )
        requested = self._append_effect_request(
            JournalEventType.EFFECT_REQUESTED,
            identity,
            command,
            EffectOperation.REPOSITORY_UPDATE,
            EffectObjectType.WORKTREE,
        )
        if requested.event is None or requested.append_result is None:
            return AttemptPreparationResult(
                WorkflowStatus.BLOCKED,
                requested.reason,
                self._cursor,
                tuple(events),
            )
        events.append(requested.event)
        effect = self._repository.create_attempt(
            PreparedEffect.from_append_result(requested.append_result, command)
        )
        observed = self._append_observation(
            JournalEventType.EFFECT_OBSERVED,
            effect.receipt,
        )
        if observed.event is None:
            return AttemptPreparationResult(
                WorkflowStatus.BLOCKED,
                observed.reason,
                self._cursor,
                tuple(events),
            )
        events.append(observed.event)
        if (
            effect.disposition is AttemptEffectDisposition.APPLIED
            and type(effect.value) is AttemptWorktree
        ):
            return AttemptPreparationResult(
                WorkflowStatus.PROGRESSED,
                WorkflowReason.NONE,
                self._cursor,
                tuple(events),
                effect.value,
            )
        reason = self._effect_reason(effect.receipt.status)
        self._block_run(identity, effect.reason_code, events)
        return AttemptPreparationResult(
            WorkflowStatus.BLOCKED,
            reason,
            self._cursor,
            tuple(events),
        )

    def stage_attempt_result(self, attempt: AttemptWorktree) -> ResultStageResult:
        """Validate and stage one succeeded attempt without touching the target ref."""

        if type(attempt) is not AttemptWorktree:
            raise TypeError("attempt must be an AttemptWorktree")
        events: list[JournalEvent] = []
        admission = self._admission_reason()
        if admission is not WorkflowReason.NONE:
            return ResultStageResult(
                WorkflowStatus.BLOCKED, admission, self._cursor, ()
            )
        identity = attempt.identity
        if not self._current_attempt(identity, RuntimeState.SUCCEEDED):
            return ResultStageResult(
                WorkflowStatus.REJECTED,
                WorkflowReason.ATTEMPT_NOT_CURRENT,
                self._cursor,
                (),
            )
        task_state = dict(self._cursor.graph_index.task_states).get(identity.task_id)
        if task_state is not RuntimeState.DISPATCHED:
            return ResultStageResult(
                WorkflowStatus.REJECTED,
                WorkflowReason.ATTEMPT_NOT_CURRENT,
                self._cursor,
                (),
            )
        validation = self._repository.validate_result(
            attempt,
            process_tree_terminated=True,
        )
        if not validation.accepted:
            self._block_run(identity, validation.reason_code, events)
            return ResultStageResult(
                WorkflowStatus.BLOCKED,
                WorkflowReason.RESULT_REJECTED,
                self._cursor,
                tuple(events),
                validation,
            )
        assert identity.task_id is not None and identity.attempt is not None
        operation_id = (
            f"STAGE-{identity.task_id}-{identity.attempt:04d}-"
            f"EPOCH-{identity.coordinator_epoch:04d}"
        )
        command = self._repository.plan_stage(
            validation,
            operation_id=operation_id,
        )
        request_identity = replace(identity, correlation_id=operation_id)
        requested = self._append_effect_request(
            JournalEventType.EFFECT_REQUESTED,
            request_identity,
            command,
            EffectOperation.RESULT_STAGE,
            EffectObjectType.RESULT_BUNDLE,
        )
        if requested.event is None or requested.append_result is None:
            return ResultStageResult(
                WorkflowStatus.BLOCKED,
                requested.reason,
                self._cursor,
                tuple(events),
                validation,
            )
        events.append(requested.event)
        effect = self._repository.stage_result(
            PreparedEffect.from_append_result(requested.append_result, command),
            validation,
        )
        observed = self._append_observation(
            JournalEventType.EFFECT_OBSERVED,
            effect.receipt,
        )
        if observed.event is None:
            return ResultStageResult(
                WorkflowStatus.BLOCKED,
                observed.reason,
                self._cursor,
                tuple(events),
                validation,
            )
        events.append(observed.event)
        if (
            effect.disposition is not AttemptEffectDisposition.APPLIED
            or type(effect.value) is not StagedResult
        ):
            reason = self._effect_reason(effect.receipt.status)
            self._block_run(identity, effect.reason_code, events)
            return ResultStageResult(
                WorkflowStatus.BLOCKED,
                reason,
                self._cursor,
                tuple(events),
                validation,
            )
        task_identity = ExecutionIdentity(
            identity.run_id,
            identity.coordinator_epoch,
            identity.task_id,
        )
        staged = self._append_transition(
            JournalEventType.RESULT_STAGED,
            task_identity,
            RuntimeState.DISPATCHED,
            RuntimeState.STAGED,
            evidence=effect.receipt.evidence,
        )
        if staged.event is None:
            return ResultStageResult(
                WorkflowStatus.BLOCKED,
                staged.reason,
                self._cursor,
                tuple(events),
                validation,
            )
        events.append(staged.event)
        return ResultStageResult(
            WorkflowStatus.PROGRESSED,
            WorkflowReason.NONE,
            self._cursor,
            tuple(events),
            validation,
            effect.value,
        )

    def promote_staged(
        self,
        sources: tuple[StagedResult, ...],
        acceptance: AcceptancePort,
    ) -> PromotionBatchResult:
        """Promote all staged siblings in frozen graph order and verify each one."""

        if type(sources) is not tuple or not all(
            type(source) is StagedResult for source in sources
        ):
            raise TypeError("sources must be a tuple of StagedResult values")
        if not isinstance(acceptance, AcceptancePort):
            raise TypeError("acceptance must implement AcceptancePort")
        events: list[JournalEvent] = []
        promoted: list[PromotionRecord] = []
        try:
            remaining = list(self._promotion.canonical_order(sources))
        except PromotionBoundaryError:
            return PromotionBatchResult(
                WorkflowStatus.REJECTED,
                WorkflowReason.PROMOTION_REJECTED,
                self._cursor,
                (),
            )
        while remaining:
            admission = self._admission_reason()
            if admission is not WorkflowReason.NONE:
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    admission,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            source = remaining[0]
            task_state = dict(self._cursor.graph_index.task_states).get(source.task_id)
            if task_state is not RuntimeState.STAGED:
                return PromotionBatchResult(
                    WorkflowStatus.REJECTED,
                    WorkflowReason.TASK_NOT_STAGED,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            operation_id = (
                f"PROMOTION-{source.task_id}-{source.attempt:04d}-"
                f"EPOCH-{self._fencing_token:04d}"
            )
            try:
                plan = self._promotion.plan_next(
                    tuple(remaining),
                    expected_target_sha=(
                        self._repository.expected_workspace.base_commit_sha
                    ),
                    operation_id=operation_id,
                    coordinator_epoch=self._fencing_token,
                )
            except (GitBoundaryError, PromotionBoundaryError):
                self._block_run(
                    source.manifest.identity,
                    RuntimeReasonCode.GIT_STATE_CONFLICT,
                    events,
                )
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    WorkflowReason.PROMOTION_REJECTED,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            task = self._task(source.task_id)
            try:
                candidate_record = plan.candidate_record()
                with self._promotion.materialize_candidate(plan) as candidate_repository:
                    acceptance_result = acceptance.verify(
                        task,
                        candidate_repository,
                        candidate_record,
                    )
            except (GitBoundaryError, PromotionBoundaryError):
                self._block_run(
                    source.manifest.identity,
                    RuntimeReasonCode.GIT_STATE_CONFLICT,
                    events,
                )
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    WorkflowReason.PROMOTION_REJECTED,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            except Exception:  # noqa: BLE001 - acceptance adapter crash barrier
                acceptance_result = AcceptanceResult(
                    False,
                    (),
                    RuntimeReasonCode.CHECK_FAILED,
                )
            if type(acceptance_result) is not AcceptanceResult:
                acceptance_result = AcceptanceResult(
                    False,
                    (),
                    RuntimeReasonCode.CHECK_FAILED,
                )
            if not acceptance_result.accepted:
                self._block_run(
                    source.manifest.identity,
                    acceptance_result.reason_code,
                    events,
                )
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    WorkflowReason.ACCEPTANCE_FAILED,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            try:
                plan = self._promotion.bind_acceptance(
                    plan,
                    acceptance_result.evidence,
                )
            except (TypeError, ValueError, PromotionBoundaryError):
                self._block_run(
                    source.manifest.identity,
                    RuntimeReasonCode.INVARIANT_VIOLATION,
                    events,
                )
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    WorkflowReason.PROMOTION_REJECTED,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            command = plan.command
            identity = ExecutionIdentity(
                command.run_id,
                command.coordinator_epoch,
                command.task_id,
                command.attempt,
                command.operation_id,
            )
            requested = self._append_effect_request(
                JournalEventType.PROMOTION_REQUESTED,
                identity,
                command,
                EffectOperation.RESULT_PROMOTION,
                EffectObjectType.GIT_REF,
            )
            if requested.event is None or requested.append_result is None:
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    requested.reason,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            events.append(requested.event)
            observation = self._promotion.apply(
                PreparedEffect.from_append_result(
                    requested.append_result,
                    command,
                ),
                plan,
            )
            observed = self._append_observation(
                JournalEventType.PROMOTION_OBSERVED,
                observation.receipt,
            )
            if observed.event is None:
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    observed.reason,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            events.append(observed.event)
            if (
                observation.disposition is not PromotionDisposition.APPLIED
                or observation.record is None
            ):
                self._block_run(identity, observation.reason_code, events)
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    self._effect_reason(observation.receipt.status),
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            task_identity = ExecutionIdentity(
                identity.run_id,
                identity.coordinator_epoch,
                identity.task_id,
            )
            verified = self._append_transition(
                JournalEventType.TASK_VERIFIED,
                task_identity,
                RuntimeState.PROMOTED,
                RuntimeState.VERIFIED,
                evidence=observation.record.acceptance_evidence,
            )
            if verified.event is None:
                return PromotionBatchResult(
                    WorkflowStatus.BLOCKED,
                    verified.reason,
                    self._cursor,
                    tuple(events),
                    tuple(promoted),
                )
            events.append(verified.event)
            promoted.append(observation.record)
            remaining.pop(0)
        return PromotionBatchResult(
            WorkflowStatus.PROGRESSED,
            WorkflowReason.NONE,
            self._cursor,
            tuple(events),
            tuple(promoted),
        )

    def cleanup_attempt(
        self,
        cleanup: CleanupService,
        candidate: CleanupCandidate,
        *,
        operation_id: str,
    ) -> CleanupStepResult:
        """Apply one evidence-bound cleanup behind durable request/observation events."""

        if type(cleanup) is not CleanupService:
            raise TypeError("cleanup must be a CleanupService")
        if type(candidate) is not CleanupCandidate:
            raise TypeError("candidate must be a CleanupCandidate")
        if type(operation_id) is not str or not operation_id:
            raise ValueError("operation_id must be non-empty")
        admission = self._admission_reason()
        if admission is not WorkflowReason.NONE:
            return CleanupStepResult(
                WorkflowStatus.BLOCKED,
                admission,
                self._cursor,
                (),
            )
        if cleanup.blocked_on_unknown:
            return CleanupStepResult(
                WorkflowStatus.BLOCKED,
                WorkflowReason.CLEANUP_BLOCKED,
                self._cursor,
                (),
            )
        try:
            plan = cleanup.plan(
                candidate,
                operation_id=operation_id,
                coordinator_epoch=self._fencing_token,
            )
        except CleanupBoundaryError:
            return CleanupStepResult(
                WorkflowStatus.BLOCKED,
                WorkflowReason.CLEANUP_BLOCKED,
                self._cursor,
                (),
            )
        command = plan.command
        identity = ExecutionIdentity(
            command.run_id,
            command.coordinator_epoch,
            command.task_id,
            command.attempt,
            command.operation_id,
        )
        requested = self._append_effect_request(
            JournalEventType.CLEANUP_REQUESTED,
            identity,
            command,
            EffectOperation.CLEANUP,
            EffectObjectType.CLEANUP_ITEM,
        )
        if requested.event is None or requested.append_result is None:
            return CleanupStepResult(
                WorkflowStatus.BLOCKED,
                requested.reason,
                self._cursor,
                (),
            )
        events = [requested.event]
        try:
            observation = cleanup.apply(
                PreparedEffect.from_append_result(requested.append_result, command),
                plan,
            )
        except CleanupBoundaryError:
            return CleanupStepResult(
                WorkflowStatus.BLOCKED,
                WorkflowReason.CLEANUP_BLOCKED,
                self._cursor,
                tuple(events),
            )
        observed = self._append_observation(
            JournalEventType.CLEANUP_OBSERVED,
            observation.receipt,
        )
        if observed.event is None:
            return CleanupStepResult(
                WorkflowStatus.BLOCKED,
                observed.reason,
                self._cursor,
                tuple(events),
                observation,
            )
        events.append(observed.event)
        if observation.disposition is CleanupDisposition.UNKNOWN:
            self._block_run(identity, observation.reason_code, events)
            return CleanupStepResult(
                WorkflowStatus.BLOCKED,
                WorkflowReason.CLEANUP_FAILED,
                self._cursor,
                tuple(events),
                observation,
            )
        return CleanupStepResult(
            WorkflowStatus.PROGRESSED,
            WorkflowReason.NONE,
            self._cursor,
            tuple(events),
            observation,
        )

    def _append_effect_request(
        self,
        event_type: JournalEventType,
        identity: ExecutionIdentity,
        command,
        operation: EffectOperation,
        object_type: EffectObjectType,
    ) -> _AppendOutcome:
        command_hash = "sha256:" + canonical_sha256(command.to_primitive())
        return self._append_payload(
            event_type,
            identity,
            EffectRequestPayload(
                operation,
                AdapterKind.GIT,
                object_type,
                command.target_workspace_hash,
                command_hash,
                self._cursor.head.sequence,
                self._fencing_token,
            ),
        )

    def _append_observation(
        self,
        event_type: JournalEventType,
        receipt,
    ) -> _AppendOutcome:
        return self._append_payload(
            event_type,
            receipt.identity,
            EffectObservationPayload(AdapterKind.GIT, receipt),
            actor_type=ActorType.ADAPTER,
            actor_id="git-worktree-adapter",
        )

    def _append_transition(
        self,
        event_type: JournalEventType,
        identity: ExecutionIdentity,
        from_state: RuntimeState,
        to_state: RuntimeState,
        *,
        evidence: tuple[EvidenceRef, ...] = (),
        reason_code: RuntimeReasonCode | None = None,
    ) -> _AppendOutcome:
        return self._append_payload(
            event_type,
            identity,
            TransitionPayload(
                TransitionSubject.TASK,
                from_state,
                to_state,
                evidence,
            ),
            reason_code=reason_code,
        )

    def _append_payload(
        self,
        event_type: JournalEventType,
        identity: ExecutionIdentity,
        payload,
        *,
        actor_type: ActorType = ActorType.COORDINATOR,
        actor_id: str | None = None,
        reason_code: RuntimeReasonCode | None = None,
    ) -> _AppendOutcome:
        admission = self._admission_reason()
        if admission is not WorkflowReason.NONE:
            return _AppendOutcome(None, None, admission)
        sequence = self._cursor.head.sequence + 1
        draft = JournalEventDraft(
            event_id=(
                f"EVENT-{event_type.value.replace('_', '-').upper()}-"
                f"{sequence:08d}"
            ),
            event_type=event_type,
            identity=identity,
            actor_type=actor_type,
            actor_id=actor_id or self._coordinator_id,
            payload=payload,
            reason_code=reason_code,
        )
        appended = self._journal.append_draft(
            draft,
            expected_head=self._cursor.head,
        )
        if not appended.durable or appended.event is None:
            reason = (
                WorkflowReason.JOURNAL_CONFLICT
                if appended.status is AppendStatus.CONFLICT
                else WorkflowReason.PERSISTENCE_FAILED
            )
            return _AppendOutcome(None, appended, reason)
        event = appended.event
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
            return _AppendOutcome(None, appended, WorkflowReason.STATE_REJECTED)
        try:
            self._cursor = CoordinatorCursor(
                current,
                self._cursor.graph_index.advance(previous, current),
                self._cursor.lease_state.advance(event),
                advance_dispatch_recoveries(
                    self._cursor.dispatch_recoveries,
                    event,
                ),
            )
        except DispatchRecoveryProjectionError:
            return _AppendOutcome(None, appended, WorkflowReason.INDEX_MISMATCH)
        except (TypeError, ValueError):
            return _AppendOutcome(None, appended, WorkflowReason.INDEX_MISMATCH)
        return _AppendOutcome(event, appended, WorkflowReason.NONE)

    def _block_run(
        self,
        identity: ExecutionIdentity,
        reason_code: RuntimeReasonCode | None,
        events: list[JournalEvent],
    ) -> None:
        runtime_reason = reason_code or RuntimeReasonCode.INVARIANT_VIOLATION
        task_id = identity.task_id
        if task_id is not None:
            task_state = dict(self._cursor.graph_index.task_states).get(task_id)
            if task_state in {
                RuntimeState.LEASED,
                RuntimeState.DISPATCHED,
                RuntimeState.STAGED,
                RuntimeState.PROMOTED,
            }:
                task = self._append_transition(
                    JournalEventType.TASK_BLOCKED,
                    ExecutionIdentity(identity.run_id, self._fencing_token, task_id),
                    task_state,
                    RuntimeState.BLOCKED,
                    reason_code=runtime_reason,
                )
                if task.event is not None:
                    events.append(task.event)
        if self._cursor.snapshot.status is RuntimeState.RUNNING:
            run = self._append_payload(
                JournalEventType.RUN_BLOCKED,
                ExecutionIdentity(identity.run_id, self._fencing_token),
                TransitionPayload(
                    TransitionSubject.RUN,
                    RuntimeState.RUNNING,
                    RuntimeState.BLOCKED,
                ),
                reason_code=runtime_reason,
            )
            if run.event is not None:
                events.append(run.event)

    def _admission_reason(self) -> WorkflowReason:
        if self._journal.blocked:
            return WorkflowReason.PERSISTENCE_FAILED
        if not self._cursor.graph_index.verify(self._manifest, self._cursor.snapshot):
            return WorkflowReason.INDEX_MISMATCH
        authority_time = self._authority_clock()
        if type(authority_time) is not datetime or authority_time.tzinfo is None:
            raise ValueError("authority_clock must return a timezone-aware datetime")
        if not self._cursor.lease_state.allows_admission(
            authority_time=authority_time,
            coordinator_id=self._coordinator_id,
            owner=self._owner,
            fencing_token=self._fencing_token,
            manifest_digest=self._manifest_digest,
            scheduler_mode=self._manifest.scheduler_mode,
        ):
            return WorkflowReason.LEASE_NOT_ADMITTED
        return WorkflowReason.NONE

    def _current_attempt(
        self,
        identity: ExecutionIdentity,
        state: RuntimeState,
    ) -> bool:
        if type(identity) is not ExecutionIdentity or not identity.is_attempt:
            return False
        if identity.coordinator_epoch != self._fencing_token:
            return False
        return any(
            attempt.task_id == identity.task_id
            and attempt.attempt == identity.attempt
            and attempt.correlation_id == identity.correlation_id
            and attempt.coordinator_epoch == identity.coordinator_epoch
            and attempt.state is state
            for attempt in self._cursor.snapshot.attempts
        )

    def _task(self, task_id: str | None) -> ManifestTask:
        if task_id is None:
            raise ValueError("task identity is incomplete")
        return next(task for task in self._manifest.tasks if task.id == task_id)

    @staticmethod
    def _effect_reason(status: EffectStatus) -> WorkflowReason:
        return (
            WorkflowReason.EFFECT_OUTCOME_UNKNOWN
            if status is EffectStatus.UNKNOWN
            else WorkflowReason.EFFECT_ABSENT
        )


__all__ = [
    "AcceptancePort",
    "AcceptanceResult",
    "AttemptPreparationResult",
    "CleanupStepResult",
    "LocalExecutionWorkflow",
    "PromotionBatchResult",
    "ResultStageResult",
    "WorkflowReason",
    "WorkflowStatus",
]
