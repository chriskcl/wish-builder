"""Fail-closed monitoring of already-dispatched backend worker turns."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from wish_builder.contracts.runtime import (
    EffectOperation,
    EffectStatus,
    EvidenceRef,
    ExecutionIdentity,
    JournalEvent,
    RuntimeReasonCode,
)
from wish_builder.kernel.state import apply_journal_event
from wish_builder.services.ports import BackendChannelPort, TurnObservation, TurnState
from wish_builder.services.backend_effects import (
    BackendDispatchPlan,
    BackendObservationStorePort,
)

from .coordinator import CoordinatorCursor, WorkerResultProposal
from .foreground import (
    PreparedForegroundAttempt,
    WorkerBatchResult,
    WorkerLeaseRenewalResult,
)


BackendDispatchPlanFactory = Callable[[ExecutionIdentity], BackendDispatchPlan]
MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]
WorkerLeaseRenewalCallback = Callable[
    [CoordinatorCursor],
    WorkerLeaseRenewalResult,
]


@dataclass(frozen=True, slots=True)
class _ExpectedTurn:
    prepared: PreparedForegroundAttempt
    plan: BackendDispatchPlan


class BackendWorkerTurnMonitor:
    """Collect terminal backend turn observations without dispatching effects.

    The plan factory must be the same deterministic factory used to dispatch
    the attempt.  The monitor only inspects the resulting send operation IDs;
    it never sends, retries, or cancels a turn.
    """

    def __init__(
        self,
        channel: BackendChannelPort,
        observation_store: BackendObservationStorePort,
        plan_factory: BackendDispatchPlanFactory,
        *,
        timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
        lease_renewal: WorkerLeaseRenewalCallback | None = None,
        monotonic: MonotonicClock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if not isinstance(channel, BackendChannelPort):
            raise TypeError("channel must implement BackendChannelPort")
        if not isinstance(observation_store, BackendObservationStorePort):
            raise TypeError(
                "observation_store must implement BackendObservationStorePort"
            )
        if not callable(plan_factory):
            raise TypeError("plan_factory must be callable")
        if (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(float(timeout_seconds))
            or float(timeout_seconds) <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if (
            type(poll_interval_seconds) not in {int, float}
            or isinstance(poll_interval_seconds, bool)
            or not math.isfinite(float(poll_interval_seconds))
            or float(poll_interval_seconds) <= 0
        ):
            raise ValueError(
                "poll_interval_seconds must be a positive finite number"
            )
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        if not callable(sleeper):
            raise TypeError("sleeper must be callable")
        if lease_renewal is not None and not callable(lease_renewal):
            raise TypeError("lease_renewal must be callable or null")

        self._channel = channel
        self._observation_store = observation_store
        self._plan_factory = plan_factory
        self._timeout_seconds = float(timeout_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._lease_renewal = lease_renewal
        self._monotonic = monotonic
        self._sleeper = sleeper

    def run(
        self,
        attempts: tuple[PreparedForegroundAttempt, ...],
        cursor: CoordinatorCursor | None = None,
    ) -> WorkerBatchResult:
        """Return proposals only when every observed worker outcome is known."""

        if type(attempts) is not tuple or not all(
            type(item) is PreparedForegroundAttempt for item in attempts
        ):
            raise TypeError(
                "attempts must be a tuple of PreparedForegroundAttempt values"
            )
        if cursor is not None and type(cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor or null")
        if not attempts:
            return WorkerBatchResult(True, cursor=cursor)

        current_cursor = cursor
        renewal_events: list[JournalEvent] = []
        renewal_interval = self._renewal_interval(current_cursor)
        if self._lease_renewal is not None and renewal_interval is None:
            return WorkerBatchResult(False)

        def result(
            outcomes_known: bool,
            proposals: tuple[WorkerResultProposal, ...] = (),
        ) -> WorkerBatchResult:
            return WorkerBatchResult(
                outcomes_known,
                proposals,
                current_cursor,
                tuple(renewal_events),
            )

        expected = self._expected_turns(attempts)
        if expected is None:
            return result(False)

        started_at = self._read_clock()
        if started_at is None:
            return result(False)
        deadline = started_at + self._timeout_seconds
        if not math.isfinite(deadline):
            return result(False)
        initial_renewal_pending = (
            renewal_interval is not None and self._lease_renewal is not None
        )
        next_renewal_at = None
        if renewal_interval is not None:
            # A worker may enter monitoring after the acquired lease has already
            # aged. Refresh as soon as waiting is confirmed, then use TTL/3.
            next_renewal_at = (
                started_at
                if initial_renewal_pending
                else started_at + renewal_interval
            )

        last_clock = started_at
        logical_elapsed = 0.0
        pending = {
            item.plan.send.operation_id: item
            for item in expected
        }
        terminal: dict[str, TurnObservation] = {}

        while pending:
            sampled_before_inspection = self._read_clock(last_clock)
            if (
                sampled_before_inspection is None
                or sampled_before_inspection >= deadline
            ):
                return result(False)
            last_clock = sampled_before_inspection
            if (
                not initial_renewal_pending
                and next_renewal_at is not None
                and sampled_before_inspection >= next_renewal_at
            ):
                renewed = self._renew(current_cursor)
                if renewed is None:
                    return result(False)
                current_cursor, event = renewed
                renewal_events.append(event)
                renewed_at = self._read_clock(last_clock)
                renewal_interval = self._renewal_interval(current_cursor)
                if (
                    renewed_at is None
                    or renewed_at >= deadline
                    or renewal_interval is None
                ):
                    return result(False)
                last_clock = renewed_at
                next_renewal_at = renewed_at + renewal_interval

            for operation_id, item in tuple(pending.items()):
                try:
                    observation = self._channel.inspect_turn(operation_id)
                except Exception:
                    return result(False)
                if not self._matches_plan(observation, item.plan):
                    return result(False)

                if observation.state in {TurnState.QUEUED, TurnState.RUNNING}:
                    continue
                if observation.state not in {
                    TurnState.DONE,
                    TurnState.FAILED,
                    TurnState.CANCELLED,
                }:
                    return result(False)
                terminal[operation_id] = observation
                del pending[operation_id]

            sampled_at = self._read_clock(last_clock)
            if sampled_at is None or sampled_at >= deadline:
                return result(False)
            last_clock = sampled_at

            if not pending:
                proposals = self._build_proposals(expected, terminal)
                return (
                    result(False)
                    if proposals is None
                    else result(True, proposals)
                )

            if initial_renewal_pending or (
                next_renewal_at is not None and sampled_at >= next_renewal_at
            ):
                renewed = self._renew(current_cursor)
                if renewed is None:
                    return result(False)
                current_cursor, event = renewed
                renewal_events.append(event)
                renewed_at = self._read_clock(last_clock)
                renewal_interval = self._renewal_interval(current_cursor)
                if (
                    renewed_at is None
                    or renewed_at >= deadline
                    or renewal_interval is None
                ):
                    return result(False)
                last_clock = renewed_at
                sampled_at = renewed_at
                next_renewal_at = renewed_at + renewal_interval
                initial_renewal_pending = False

            wall_remaining = deadline - sampled_at
            logical_remaining = self._timeout_seconds - logical_elapsed
            sleep_limits = [
                self._poll_interval_seconds,
                wall_remaining,
                logical_remaining,
            ]
            if next_renewal_at is not None:
                sleep_limits.append(next_renewal_at - sampled_at)
            sleep_for = min(sleep_limits)
            if sleep_for <= 0:
                return result(False)
            try:
                self._sleeper(sleep_for)
            except Exception:
                return result(False)
            logical_elapsed += sleep_for
            if logical_elapsed >= self._timeout_seconds:
                return result(False)

            sampled_after_sleep = self._read_clock(last_clock)
            if sampled_after_sleep is None or sampled_after_sleep >= deadline:
                return result(False)
            last_clock = sampled_after_sleep

        return result(False)

    def _renew(
        self,
        cursor: CoordinatorCursor | None,
    ) -> tuple[CoordinatorCursor, JournalEvent] | None:
        if cursor is None or self._lease_renewal is None:
            return None
        try:
            renewal = self._lease_renewal(cursor)
        except Exception:
            return None
        if type(renewal) is not WorkerLeaseRenewalResult or not renewal.succeeded:
            return None
        renewed_cursor = renewal.cursor
        event = renewal.event
        assert renewed_cursor is not None and event is not None
        try:
            applied = apply_journal_event(cursor.snapshot, event)
            if not applied.accepted:
                return None
            expected = CoordinatorCursor(
                applied.snapshot,
                cursor.graph_index.advance(cursor.snapshot, applied.snapshot),
                cursor.lease_state.advance(event),
                cursor.dispatch_recoveries,
            )
        except (TypeError, ValueError):
            return None
        if renewed_cursor != expected:
            return None
        return expected, event

    @staticmethod
    def _renewal_interval(cursor: CoordinatorCursor | None) -> float | None:
        if cursor is None:
            return None
        lease = cursor.lease_state.lease
        if not cursor.lease_state.active or lease is None:
            return None
        interval = float(lease.lease_ttl_seconds) / 3.0
        return interval if math.isfinite(interval) and interval > 0 else None

    def _expected_turns(
        self,
        attempts: tuple[PreparedForegroundAttempt, ...],
    ) -> tuple[_ExpectedTurn, ...] | None:
        expected: list[_ExpectedTurn] = []
        identities: set[ExecutionIdentity] = set()
        operation_ids: set[str] = set()
        for prepared in attempts:
            identity = prepared.identity
            if identity.correlation_id is None or identity in identities:
                return None
            try:
                plan = self._plan_factory(identity)
            except Exception:
                return None
            if (
                type(plan) is not BackendDispatchPlan
                or plan.send.dispatch_id != identity.correlation_id
                or plan.send.operation_id in operation_ids
            ):
                return None
            identities.add(identity)
            operation_ids.add(plan.send.operation_id)
            expected.append(_ExpectedTurn(prepared, plan))
        return tuple(expected)

    @staticmethod
    def _matches_plan(
        observation: object,
        plan: BackendDispatchPlan,
    ) -> bool:
        send = plan.send
        return (
            type(observation) is TurnObservation
            and observation.status is EffectStatus.APPLIED
            and observation.operation_id == send.operation_id
            and observation.attempt_id == send.attempt_id
            and observation.channel_id == send.channel_id
            and observation.message_id == send.message_id
            and observation.turn_id == send.turn_id
        )

    def _build_proposals(
        self,
        expected: tuple[_ExpectedTurn, ...],
        terminal: dict[str, TurnObservation],
    ) -> tuple[WorkerResultProposal, ...] | None:
        proposals: list[WorkerResultProposal] = []
        for item in expected:
            send = item.plan.send
            observation = terminal.get(send.operation_id)
            if observation is None:
                return None
            evidence_identity = replace(
                item.prepared.identity,
                correlation_id=send.operation_id,
            )
            try:
                evidence = self._observation_store.put(
                    observation,
                    identity=evidence_identity,
                    operation=EffectOperation.SEND_TASK_PACKET,
                )
            except Exception:
                return None
            if type(evidence) is not EvidenceRef:
                return None

            succeeded = observation.state is TurnState.DONE
            reason_code = None
            if observation.state is TurnState.FAILED:
                reason_code = RuntimeReasonCode.CHECK_FAILED
            elif observation.state is TurnState.CANCELLED:
                reason_code = RuntimeReasonCode.CANCELLED_BY_USER
            proposals.append(
                WorkerResultProposal(
                    item.prepared.identity,
                    f"backend-turn:{send.turn_id}",
                    succeeded,
                    reason_code,
                    (evidence,),
                )
            )
        return tuple(proposals)

    def _read_clock(self, previous: float | None = None) -> float | None:
        try:
            value = self._monotonic()
        except Exception:
            return None
        if (
            type(value) not in {int, float}
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            return None
        sampled = float(value)
        if previous is not None and sampled < previous:
            return None
        return sampled


__all__ = [
    "MonotonicClock",
    "Sleeper",
    "BackendDispatchPlanFactory",
    "BackendWorkerTurnMonitor",
    "WorkerLeaseRenewalCallback",
]
