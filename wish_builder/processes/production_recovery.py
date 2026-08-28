"""Batch reconciliation for backend and Trellis effects recovered from the Journal."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

from wish_builder.contracts import SchedulerMode
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectOperation,
    EffectStatus,
    JournalEvent,
    JournalEventType,
)
from wish_builder.contracts.execution import (
    ExecutionManifestModel,
    is_execution_manifest_model,
)
from wish_builder.kernel.state import ApplyReason, apply_journal_event
from wish_builder.processes.coordinator import CoordinatorCursor
from wish_builder.services.dispatch_recovery import (
    DispatchRecoveryProjectionError,
    PendingExternalEffect,
    advance_dispatch_recoveries,
)
from wish_builder.services.journal import DurableJournal, JournalHead
from wish_builder.services.ports import (
    AttemptObservation,
    CancelTurn,
    ChannelObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PrepareAttempt,
    ReserveChannel,
    SendTaskPacket,
    BackendChannelPort,
    TrellisLifecyclePort,
    TurnObservation,
)
from wish_builder.services.recovery import (
    LeaseRecoveryResult,
    LeaseRecoveryStatus,
)
from wish_builder.services.backend_effects import (
    BackendDispatchPlan,
    BackendObservationStorePort,
)
from wish_builder.services.trellis_lifecycle_effects import (
    lifecycle_target_object_hash,
)
from wish_builder.services.external_recovery import (
    BackendEffectRecoveryService,
    BackendRecoveryCommand,
    TrellisLifecycleEffectRecoveryService,
    TrellisLifecycleRecoveryCommand,
    ExternalEffectRecoveryReason,
    ExternalEffectRecoveryResult,
    ExternalEffectRecoveryStatus,
)


ExternalRecoveryCommand = (
    PrepareAttempt
    | ReserveChannel
    | SendTaskPacket
    | CancelTurn
    | CheckAttempt
    | FinishAttempt
)
ExternalRecoveryPlanFactory = Callable[
    [PendingExternalEffect], BackendDispatchPlan
]
ExternalRecoveryCommandResolver = Callable[
    [PendingExternalEffect, BackendDispatchPlan | None],
    ExternalRecoveryCommand | None,
]
RetryAdmission = Callable[[], bool]
ExternalEffectObservation = (
    AttemptObservation
    | ChannelObservation
    | TurnObservation
    | CheckObservation
    | FinishObservation
)


class ProductionExternalEffectRecoveryReason(StrEnum):
    NONE = "none"
    INVALID_INPUT = "invalid_input"
    CURSOR_HEAD_MISMATCH = "cursor_head_mismatch"
    PENDING_ORDER_INVALID = "pending_order_invalid"
    PLAN_REQUIRED = "plan_required"
    COMMAND_REQUIRED = "command_required"
    CHILD_RECOVERY_BLOCKED = "child_recovery_blocked"
    CURSOR_ADVANCE_FAILED = "cursor_advance_failed"


@dataclass(frozen=True, slots=True)
class ProductionExternalEffectRecoveryResult:
    """Outcome of reconciling one Journal-ordered pending-effect batch."""

    success: bool
    reason: ProductionExternalEffectRecoveryReason
    cursor: CoordinatorCursor
    events: tuple[JournalEvent, ...] = ()
    completed_operation_ids: tuple[str, ...] = ()
    blocked_operation_id: str | None = None
    child_result: ExternalEffectRecoveryResult | None = None

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise TypeError("success must be a bool")
        if type(self.reason) is not ProductionExternalEffectRecoveryReason:
            raise TypeError("reason must be a ProductionExternalEffectRecoveryReason")
        if type(self.cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        if type(self.events) is not tuple or not all(
            type(event) is JournalEvent for event in self.events
        ):
            raise TypeError("events must contain JournalEvent values")
        if type(self.completed_operation_ids) is not tuple or not all(
            type(operation_id) is str and operation_id
            for operation_id in self.completed_operation_ids
        ):
            raise TypeError("completed_operation_ids must contain non-empty strings")
        if self.blocked_operation_id is not None and (
            type(self.blocked_operation_id) is not str
            or not self.blocked_operation_id
        ):
            raise TypeError("blocked_operation_id must be a non-empty string or null")
        if (
            self.child_result is not None
            and type(self.child_result) is not ExternalEffectRecoveryResult
        ):
            raise TypeError("child_result must be a ExternalEffectRecoveryResult or null")
        if len(self.events) != len(self.completed_operation_ids):
            raise ValueError("completed operations must have one reconciled event each")
        if any(
            event.event_type is not JournalEventType.EFFECT_RECONCILED
            for event in self.events
        ):
            raise ValueError("events must contain only EFFECT_RECONCILED events")
        if any(
            current.sequence != previous.sequence + 1
            or current.previous_event_hash != previous.event_hash
            for previous, current in zip(self.events, self.events[1:])
        ):
            raise ValueError("events must form one contiguous Journal chain")
        if self.events and (
            self.events[-1].sequence != self.cursor.head.sequence
            or self.events[-1].event_hash != self.cursor.head.event_hash
        ):
            raise ValueError("events must end at the returned cursor")
        if self.success:
            if (
                self.reason is not ProductionExternalEffectRecoveryReason.NONE
                or self.blocked_operation_id is not None
                or self.child_result is not None
            ):
                raise ValueError("successful recovery cannot contain a blocked result")
        elif self.reason is ProductionExternalEffectRecoveryReason.NONE:
            raise ValueError("failed recovery requires a reason")


def resolve_external_recovery_command(
    pending: PendingExternalEffect,
    plan: BackendDispatchPlan | None,
) -> ExternalRecoveryCommand | None:
    """Select a plan command only when its durable operation identity matches."""

    if type(pending) is not PendingExternalEffect:
        raise TypeError("pending must be a PendingExternalEffect")
    if plan is None:
        return None
    if type(plan) is not BackendDispatchPlan:
        raise TypeError("plan must be a BackendDispatchPlan")
    if (
        pending.operation is EffectOperation.RESERVE_CHANNEL
        and plan.reserve.operation_id == pending.operation_id
    ):
        return plan.reserve
    if (
        pending.operation is EffectOperation.SEND_TASK_PACKET
        and plan.send.operation_id == pending.operation_id
    ):
        return plan.send
    # A dispatch plan does not carry the original cancellation command. A caller
    # may supply a resolver backed by an immutable cancellation record.
    return None


def reconcile_pending_external_effects(
    pending_external_effects: tuple[PendingExternalEffect, ...],
    *,
    lease_recovery: LeaseRecoveryResult,
    manifest: ExecutionManifestModel,
    journal: DurableJournal,
    backend_channel: BackendChannelPort,
    trellis_lifecycle: TrellisLifecyclePort,
    evidence_store: BackendObservationStorePort,
    cursor: CoordinatorCursor,
    plan_factory: ExternalRecoveryPlanFactory | None,
    retry_admitted: RetryAdmission,
    command_resolver: ExternalRecoveryCommandResolver = (
        resolve_external_recovery_command
    ),
) -> ProductionExternalEffectRecoveryResult:
    """Reconcile every pending child effect before normal dispatch may resume.

    The whole recovered batch is inspected before any retry can reach the
    adapter. Commands for every absent effect are then reconstructed before
    reconciliation starts. Reconciliation inspects each effect again, closing
    the gap between preflight and retry.
    """

    if type(cursor) is not CoordinatorCursor:
        raise TypeError("cursor must be a CoordinatorCursor")
    if type(lease_recovery) is not LeaseRecoveryResult:
        raise TypeError("lease_recovery must be a LeaseRecoveryResult")
    if not is_execution_manifest_model(manifest):
        raise TypeError("manifest must be an execution manifest")
    if type(journal) is not DurableJournal:
        raise TypeError("journal must be a DurableJournal")
    if not isinstance(backend_channel, BackendChannelPort):
        raise TypeError("backend_channel must implement BackendChannelPort")
    if not isinstance(trellis_lifecycle, TrellisLifecyclePort):
        raise TypeError("trellis_lifecycle must implement TrellisLifecyclePort")
    if not isinstance(evidence_store, BackendObservationStorePort):
        raise TypeError("evidence_store must implement BackendObservationStorePort")
    if plan_factory is not None and not callable(plan_factory):
        raise TypeError("plan_factory must be callable or null")
    if not callable(retry_admitted):
        raise TypeError("retry_admitted must be callable")
    if not callable(command_resolver):
        raise TypeError("command_resolver must be callable")

    if (
        lease_recovery.status is not LeaseRecoveryStatus.RECOVERED
        or lease_recovery.lease_state is None
    ):
        return _blocked(ProductionExternalEffectRecoveryReason.INVALID_INPUT, cursor)
    if lease_recovery.replay.head != cursor.head:
        return _blocked(
            ProductionExternalEffectRecoveryReason.CURSOR_HEAD_MISMATCH,
            cursor,
        )
    invalid = _validate_pending_batch(
        pending_external_effects,
        cursor,
        lease_recovery,
        manifest,
    )
    if invalid is not None:
        return _blocked(invalid, cursor)
    ordered = tuple(
        sorted(
            pending_external_effects,
            key=lambda pending: pending.request_event.sequence,
        )
    )
    lease = cursor.lease_state.lease
    if (
        not cursor.lease_state.active
        or lease is None
        or lease.fencing_token != cursor.snapshot.coordinator_epoch
        or lease.coordinator_id == ""
    ):
        return _blocked(ProductionExternalEffectRecoveryReason.INVALID_INPUT, cursor)
    if not _journal_head_matches(journal, cursor):
        return _blocked(
            ProductionExternalEffectRecoveryReason.CURSOR_HEAD_MISMATCH,
            cursor,
        )

    preflight: list[tuple[PendingExternalEffect, ExternalEffectObservation]] = []
    for pending in ordered:
        if not _journal_head_matches(journal, cursor):
            return _blocked(
                ProductionExternalEffectRecoveryReason.CURSOR_HEAD_MISMATCH,
                cursor,
                operation_id=pending.operation_id,
            )
        observation, blocked = _inspect_for_preflight(
            backend_channel,
            trellis_lifecycle,
            pending,
            cursor.head,
            lease.fencing_token,
        )
        if blocked is not None or observation is None:
            return _blocked(
                ProductionExternalEffectRecoveryReason.CHILD_RECOVERY_BLOCKED,
                cursor,
                operation_id=pending.operation_id,
                child=blocked,
            )
        preflight.append((pending, observation))

    backend_retry_commands: dict[str, BackendRecoveryCommand] = {}
    lifecycle_retry_commands: dict[str, TrellisLifecycleRecoveryCommand] = {}
    for pending, observation in preflight:
        if observation.status is not EffectStatus.ABSENT:
            continue
        lifecycle = pending.operation in {
            EffectOperation.PREPARE_ATTEMPT,
            EffectOperation.CHECK_ATTEMPT,
            EffectOperation.FINISH_ATTEMPT,
        }
        if plan_factory is None and not lifecycle:
            return _blocked(
                ProductionExternalEffectRecoveryReason.PLAN_REQUIRED,
                cursor,
                operation_id=pending.operation_id,
            )
        try:
            plan = None if plan_factory is None else plan_factory(pending)
        except Exception:
            plan = None
        if plan_factory is not None and type(plan) is not BackendDispatchPlan:
            return _blocked(
                ProductionExternalEffectRecoveryReason.PLAN_REQUIRED,
                cursor,
                operation_id=pending.operation_id,
            )
        try:
            command = command_resolver(pending, plan)
        except Exception:
            command = None
        if not _valid_command(pending, command):
            return _blocked(
                ProductionExternalEffectRecoveryReason.COMMAND_REQUIRED,
                cursor,
                operation_id=pending.operation_id,
            )
        if pending.adapter is AdapterKind.BACKEND:
            assert type(command) in {
                ReserveChannel,
                SendTaskPacket,
                CancelTurn,
            }
            backend_retry_commands[pending.operation_id] = command
        else:
            assert type(command) in {
                PrepareAttempt,
                CheckAttempt,
                FinishAttempt,
            }
            lifecycle_retry_commands[pending.operation_id] = command

    current = cursor
    events: list[JournalEvent] = []
    completed: list[str] = []
    backend_recovery = BackendEffectRecoveryService(
        journal,
        backend_channel,
        evidence_store,
        coordinator_id=lease.coordinator_id,
        fencing_token=lease.fencing_token,
        retry_admitted=retry_admitted,
    )
    lifecycle_recovery = TrellisLifecycleEffectRecoveryService(
        journal,
        trellis_lifecycle,
        evidence_store,
        coordinator_id=lease.coordinator_id,
        fencing_token=lease.fencing_token,
        retry_admitted=retry_admitted,
    )
    for pending, observation in preflight:
        if not _journal_head_matches(journal, current):
            return _blocked(
                ProductionExternalEffectRecoveryReason.CURSOR_HEAD_MISMATCH,
                current,
                events,
                completed,
                pending.operation_id,
            )
        if pending.adapter is AdapterKind.BACKEND:
            command = (
                backend_retry_commands.get(pending.operation_id)
                if observation.status is EffectStatus.ABSENT
                else None
            )
            child = backend_recovery.reconcile(
                pending,
                expected_head=current.head,
                retry_command=command,
            )
        else:
            command = (
                lifecycle_retry_commands.get(pending.operation_id)
                if observation.status is EffectStatus.ABSENT
                else None
            )
            child = lifecycle_recovery.reconcile(
                pending,
                expected_head=current.head,
                retry_command=command,
            )

        if child.status not in {
            ExternalEffectRecoveryStatus.RECONCILED,
            ExternalEffectRecoveryStatus.RETRIED,
        } or child.event is None:
            return _blocked(
                ProductionExternalEffectRecoveryReason.CHILD_RECOVERY_BLOCKED,
                current,
                events,
                completed,
                pending.operation_id,
                child,
            )
        advanced = _advance_cursor(current, child.event)
        if advanced is None:
            return _blocked(
                ProductionExternalEffectRecoveryReason.CURSOR_ADVANCE_FAILED,
                current,
                events,
                completed,
                pending.operation_id,
                child,
            )
        current = advanced
        events.append(child.event)
        completed.append(pending.operation_id)

    return ProductionExternalEffectRecoveryResult(
        True,
        ProductionExternalEffectRecoveryReason.NONE,
        current,
        tuple(events),
        tuple(completed),
    )


def _validate_pending_batch(
    pending: object,
    cursor: CoordinatorCursor,
    lease_recovery: LeaseRecoveryResult,
    manifest: ExecutionManifestModel,
) -> ProductionExternalEffectRecoveryReason | None:
    if type(pending) is not tuple or not all(
        type(item) is PendingExternalEffect for item in pending
    ):
        return ProductionExternalEffectRecoveryReason.INVALID_INPUT
    requests = tuple(item.request_event for item in pending)
    sequences = tuple(event.sequence for event in requests)
    operation_ids = tuple(item.operation_id for item in pending)
    if len(set(sequences)) != len(sequences) or len(set(operation_ids)) != len(
        operation_ids
    ):
        return ProductionExternalEffectRecoveryReason.PENDING_ORDER_INVALID
    if any(
        event.identity.run_id != cursor.snapshot.run_id
        or event.sequence > cursor.head.sequence
        or event.payload.expected_sequence != event.sequence - 1
        for event in requests
    ):
        return ProductionExternalEffectRecoveryReason.INVALID_INPUT
    ordered = tuple(sorted(pending, key=lambda item: item.request_event.sequence))
    lease = cursor.lease_state.lease
    try:
        graph_matches = cursor.graph_index.verify(manifest, cursor.snapshot)
    except (TypeError, ValueError):
        graph_matches = False
    if (
        manifest.run_id != cursor.snapshot.run_id
        or lease_recovery.replay.snapshot != cursor.snapshot
        or lease_recovery.replay.graph_index != cursor.graph_index
        or lease_recovery.lease_state != cursor.lease_state
        or lease_recovery.dispatch_recoveries != cursor.dispatch_recoveries
        or lease_recovery.pending_external_effects != ordered
        or not graph_matches
        or lease is None
        or lease.scheduler_mode is not SchedulerMode.WISH_BUILDER
        or lease.manifest_digest != cursor.graph_index.manifest_hash
    ):
        return ProductionExternalEffectRecoveryReason.INVALID_INPUT
    return None


def _inspect_for_preflight(
    backend_channel: BackendChannelPort,
    trellis_lifecycle: TrellisLifecyclePort,
    pending: PendingExternalEffect,
    head: JournalHead,
    fencing_token: int,
) -> tuple[ExternalEffectObservation | None, ExternalEffectRecoveryResult | None]:
    try:
        if pending.adapter is AdapterKind.BACKEND:
            observation = _inspect_backend(backend_channel, pending)
        else:
            observation = _inspect_lifecycle(trellis_lifecycle, pending)
    except Exception:
        return None, ExternalEffectRecoveryResult(
            ExternalEffectRecoveryStatus.BLOCKED,
            ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN,
            head,
        )

    expected_type = {
        EffectOperation.PREPARE_ATTEMPT: AttemptObservation,
        EffectOperation.RESERVE_CHANNEL: ChannelObservation,
        EffectOperation.SEND_TASK_PACKET: TurnObservation,
        EffectOperation.CANCEL_TURN: TurnObservation,
        EffectOperation.CHECK_ATTEMPT: CheckObservation,
        EffectOperation.FINISH_ATTEMPT: FinishObservation,
    }[pending.operation]
    if (
        type(observation) is not expected_type
        or observation.operation_id != pending.operation_id
    ):
        return None, ExternalEffectRecoveryResult(
            ExternalEffectRecoveryStatus.BLOCKED,
            ExternalEffectRecoveryReason.OBSERVATION_INVALID,
            head,
        )
    if observation.status is EffectStatus.UNKNOWN:
        return None, ExternalEffectRecoveryResult(
            ExternalEffectRecoveryStatus.BLOCKED,
            ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN,
            head,
            observation,
        )
    if (
        observation.status is EffectStatus.ABSENT
        and pending.request_event.identity.coordinator_epoch != fencing_token
    ):
        return None, ExternalEffectRecoveryResult(
            ExternalEffectRecoveryStatus.BLOCKED,
            ExternalEffectRecoveryReason.STALE_EPOCH,
            head,
            observation,
        )
    return observation, None


def _inspect_backend(
    backend_channel: BackendChannelPort,
    pending: PendingExternalEffect,
) -> ChannelObservation | TurnObservation:
    if pending.operation is EffectOperation.RESERVE_CHANNEL:
        return backend_channel.inspect_reservation(pending.operation_id)
    if pending.operation in {
        EffectOperation.SEND_TASK_PACKET,
        EffectOperation.CANCEL_TURN,
    }:
        return backend_channel.inspect_turn(pending.operation_id)
    raise ValueError("backend recovery received a Trellis lifecycle operation")


def _inspect_lifecycle(
    trellis_lifecycle: TrellisLifecyclePort,
    pending: PendingExternalEffect,
) -> AttemptObservation | CheckObservation | FinishObservation:
    if pending.operation is EffectOperation.PREPARE_ATTEMPT:
        return trellis_lifecycle.inspect_attempt(pending.operation_id)
    if pending.operation is EffectOperation.CHECK_ATTEMPT:
        return trellis_lifecycle.inspect_check(pending.operation_id)
    if pending.operation is EffectOperation.FINISH_ATTEMPT:
        return trellis_lifecycle.inspect_finish(pending.operation_id)
    raise ValueError("Trellis lifecycle recovery received a backend operation")


def _journal_head_matches(journal: DurableJournal, cursor: CoordinatorCursor) -> bool:
    if journal.blocked:
        return False
    try:
        journal.current_position(expected_head=cursor.head)
    except Exception:
        return False
    return True


def _valid_command(
    pending: PendingExternalEffect,
    command: object,
) -> bool:
    expected_type = {
        EffectOperation.PREPARE_ATTEMPT: PrepareAttempt,
        EffectOperation.RESERVE_CHANNEL: ReserveChannel,
        EffectOperation.SEND_TASK_PACKET: SendTaskPacket,
        EffectOperation.CANCEL_TURN: CancelTurn,
        EffectOperation.CHECK_ATTEMPT: CheckAttempt,
        EffectOperation.FINISH_ATTEMPT: FinishAttempt,
    }[pending.operation]
    if type(command) is not expected_type or command.operation_id != pending.operation_id:
        return False
    payload = pending.request_event.payload
    try:
        if command.canonical_sha256() != payload.request_payload_hash:
            return False
        if type(command) in {PrepareAttempt, CheckAttempt, FinishAttempt}:
            return (
                lifecycle_target_object_hash(
                    pending.request_event.identity,
                    command,
                    pending.operation,
                )
                == payload.normalized_target_hash
            )
    except (TypeError, ValueError):
        return False
    return True


def _advance_cursor(
    cursor: CoordinatorCursor,
    event: JournalEvent,
) -> CoordinatorCursor | None:
    if (
        event.sequence != cursor.head.sequence + 1
        or event.previous_event_hash != cursor.head.event_hash
    ):
        return None
    previous = cursor.snapshot
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
        return None
    try:
        lease_state = cursor.lease_state.advance(event)
        graph_index = cursor.graph_index.advance(previous, current)
        dispatch_recoveries = advance_dispatch_recoveries(
            cursor.dispatch_recoveries,
            event,
        )
        return CoordinatorCursor(
            current,
            graph_index,
            lease_state,
            dispatch_recoveries,
        )
    except (DispatchRecoveryProjectionError, TypeError, ValueError):
        return None


def _blocked(
    reason: ProductionExternalEffectRecoveryReason,
    cursor: CoordinatorCursor,
    events: list[JournalEvent] | tuple[JournalEvent, ...] = (),
    completed: list[str] | tuple[str, ...] = (),
    operation_id: str | None = None,
    child: ExternalEffectRecoveryResult | None = None,
) -> ProductionExternalEffectRecoveryResult:
    return ProductionExternalEffectRecoveryResult(
        False,
        reason,
        cursor,
        tuple(events),
        tuple(completed),
        operation_id,
        child,
    )


__all__ = [
    "ProductionExternalEffectRecoveryReason",
    "ProductionExternalEffectRecoveryResult",
    "RetryAdmission",
    "ExternalRecoveryCommand",
    "ExternalRecoveryCommandResolver",
    "ExternalRecoveryPlanFactory",
    "reconcile_pending_external_effects",
    "resolve_external_recovery_command",
]
