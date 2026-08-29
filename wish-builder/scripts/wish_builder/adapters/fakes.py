"""Deterministic backend and Trellis fakes for contract tests."""

from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.runtime import EffectStatus
from wish_builder.services.ports import (
    AttemptObservation,
    CancelTurn,
    BackendCapabilities,
    ChannelObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PreparedEffect,
    PrepareAttempt,
    ReserveChannel,
    SendTaskPacket,
    TrellisGraphSnapshot,
    TrellisLifecycleState,
    TurnObservation,
    TurnState,
)

FIXED_FAKE_TIME = "2026-08-18T00:00:00Z"


def _effect_digest(kind: str, command_hash: str) -> str:
    return "sha256:" + canonical_sha256(
        {"command_hash": command_hash, "fake_effect": kind}
    )


def _derived_token(prefix: str, command_hash: str) -> str:
    return f"{prefix}-{command_hash.removeprefix('sha256:')[:24]}"


@dataclass(frozen=True, slots=True)
class _LifecycleRecord:
    command_type: type[object]
    command_hash: str
    response: AttemptObservation | CheckObservation | FinishObservation
    inspection: AttemptObservation | None
    applied: bool


@dataclass(frozen=True, slots=True)
class _ChannelRecord:
    command_type: type[object]
    command_hash: str
    response: ChannelObservation | TurnObservation
    applied: bool


class FakeExternalState:
    """Shareable external state used to simulate adapter restart and races."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.lifecycle_records: dict[str, _LifecycleRecord] = {}
        self.channel_records: dict[str, _ChannelRecord] = {}
        self.prepared_attempts: dict[str, AttemptObservation] = {}
        self.checked_attempts: set[str] = set()
        self.channel_reservations: dict[str, str] = {}
        self.attempt_channels: dict[str, str] = {}
        self.turn_operations: dict[str, str] = {}
        self.message_operations: dict[str, str] = {}
        self.turn_cancellations: dict[str, str] = {}
        self.lifecycle_conflicts: set[str] = set()
        self.channel_conflicts: set[str] = set()


class FakeTrellisGraphPort:
    def __init__(
        self,
        snapshots: Mapping[str, TrellisGraphSnapshot] | TrellisGraphSnapshot,
    ) -> None:
        if type(snapshots) is TrellisGraphSnapshot:
            values = {snapshots.parent_task_id: snapshots}
        else:
            values = dict(snapshots)
        if not values:
            raise ValueError("at least one graph snapshot is required")
        for parent_task_id, snapshot in values.items():
            if type(parent_task_id) is not str or type(snapshot) is not TrellisGraphSnapshot:
                raise TypeError("snapshots must map parent IDs to TrellisGraphSnapshot values")
            if parent_task_id != snapshot.parent_task_id:
                raise ValueError("snapshot parent ID does not match its lookup key")
        self._snapshots = values
        self._lock = threading.Lock()
        self._calls: list[str] = []

    @property
    def calls(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._calls)

    def export_snapshot(self, parent_task_id: str) -> TrellisGraphSnapshot:
        if type(parent_task_id) is not str:
            raise TypeError("parent_task_id must be a string")
        with self._lock:
            self._calls.append(parent_task_id)
            try:
                return self._snapshots[parent_task_id]
            except KeyError as exc:
                raise LookupError(
                    f"no fake Trellis snapshot for parent {parent_task_id!r}"
                ) from exc


class FakeTrellisLifecyclePort:
    def __init__(
        self,
        *,
        state: FakeExternalState | None = None,
        clock: Callable[[], str] = lambda: FIXED_FAKE_TIME,
        unknown_operation_ids: Collection[str] = (),
        worktree_path: str | None = None,
    ) -> None:
        if state is not None and type(state) is not FakeExternalState:
            raise TypeError("state must be a FakeExternalState")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if worktree_path is not None and (
            type(worktree_path) is not str or not worktree_path
        ):
            raise ValueError("worktree_path must be a non-empty string or null")
        self.state = FakeExternalState() if state is None else state
        self._clock = clock
        self._unknown_operation_ids = frozenset(unknown_operation_ids)
        self._worktree_path = worktree_path

    @property
    def applied_operation_ids(self) -> tuple[str, ...]:
        with self.state.lock:
            return tuple(
                sorted(
                    operation_id
                    for operation_id, record in self.state.lifecycle_records.items()
                    if record.applied
                )
            )

    @property
    def effect_count(self) -> int:
        return len(self.applied_operation_ids)

    def _require_effect(
        self,
        effect: object,
        command_type: type[PrepareAttempt]
        | type[CheckAttempt]
        | type[FinishAttempt],
    ) -> (
        PreparedEffect[PrepareAttempt]
        | PreparedEffect[CheckAttempt]
        | PreparedEffect[FinishAttempt]
    ):
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        if type(effect.command) is not command_type:
            raise TypeError(f"effect command must be {command_type.__name__}")
        return effect

    def _unknown_attempt(self, operation_id: str, reason: str) -> AttemptObservation:
        return AttemptObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            lifecycle_state=TrellisLifecycleState.UNKNOWN,
            evidence=(reason,),
        )

    def _unknown_check(self, operation_id: str, reason: str) -> CheckObservation:
        return CheckObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            evidence=(reason,),
        )

    def _unknown_finish(self, operation_id: str, reason: str) -> FinishObservation:
        return FinishObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            evidence=(reason,),
        )

    def _existing(
        self,
        effect: PreparedEffect[object],
        response_type: type[AttemptObservation]
        | type[CheckObservation]
        | type[FinishObservation],
    ) -> AttemptObservation | CheckObservation | FinishObservation | None:
        operation_id = effect.operation_id
        if operation_id in self.state.lifecycle_conflicts:
            reason = f"operation_id_collision:{operation_id}"
            if response_type is CheckObservation:
                return self._unknown_check(operation_id, reason)
            if response_type is FinishObservation:
                return self._unknown_finish(operation_id, reason)
            return self._unknown_attempt(operation_id, reason)
        if operation_id in self.state.channel_records:
            self.state.lifecycle_conflicts.add(operation_id)
            self.state.channel_conflicts.add(operation_id)
            reason = f"operation_id_collision:{operation_id}"
            if response_type is CheckObservation:
                return self._unknown_check(operation_id, reason)
            if response_type is FinishObservation:
                return self._unknown_finish(operation_id, reason)
            return self._unknown_attempt(operation_id, reason)
        existing = self.state.lifecycle_records.get(operation_id)
        if existing is None:
            return None
        if (
            existing.command_type is type(effect.command)
            and existing.command_hash == effect.command_hash
            and type(existing.response) is response_type
        ):
            return existing.response
        self.state.lifecycle_conflicts.add(operation_id)
        reason = f"operation_id_collision:{operation_id}"
        if response_type is CheckObservation:
            return self._unknown_check(operation_id, reason)
        if response_type is FinishObservation:
            return self._unknown_finish(operation_id, reason)
        return self._unknown_attempt(operation_id, reason)

    def prepare_attempt(
        self, effect: PreparedEffect[PrepareAttempt]
    ) -> AttemptObservation:
        typed = self._require_effect(effect, PrepareAttempt)
        command = typed.command
        assert type(command) is PrepareAttempt
        with self.state.lock:
            existing = self._existing(typed, AttemptObservation)
            if existing is not None:
                assert type(existing) is AttemptObservation
                return existing
            if command.operation_id in self._unknown_operation_ids:
                response = self._unknown_attempt(
                    command.operation_id,
                    f"scripted_unknown:{command.operation_id}",
                )
                self.state.lifecycle_records[command.operation_id] = _LifecycleRecord(
                    PrepareAttempt, typed.command_hash, response, response, False
                )
                return response
            effect_digest = _effect_digest("prepare_attempt", typed.command_hash)
            attempt_id = command.operation_id
            worktree_id = _derived_token("worktree", typed.command_hash)
            response = AttemptObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                lifecycle_state=TrellisLifecycleState.PREPARED,
                effect_digest=effect_digest,
                attempt_id=attempt_id,
                trellis_task_id=command.trellis_task_id,
                worktree_id=worktree_id,
                worktree_path=(
                    self._worktree_path
                    or f".wish-builder/fake-trellis/{worktree_id}"
                ),
                base_commit=command.expected_base_commit,
            )
            self.state.lifecycle_records[command.operation_id] = _LifecycleRecord(
                PrepareAttempt, typed.command_hash, response, response, True
            )
            self.state.prepared_attempts[attempt_id] = response
            return response

    def check_attempt(
        self, effect: PreparedEffect[CheckAttempt]
    ) -> CheckObservation:
        typed = self._require_effect(effect, CheckAttempt)
        command = typed.command
        assert type(command) is CheckAttempt
        with self.state.lock:
            existing = self._existing(typed, CheckObservation)
            if existing is not None:
                assert type(existing) is CheckObservation
                return existing
            prepared = self.state.prepared_attempts.get(command.attempt_id)
            if command.operation_id in self._unknown_operation_ids or prepared is None:
                reason = (
                    f"scripted_unknown:{command.operation_id}"
                    if command.operation_id in self._unknown_operation_ids
                    else f"attempt_not_prepared:{command.attempt_id}"
                )
                response = self._unknown_check(command.operation_id, reason)
                inspection = self._unknown_attempt(command.operation_id, reason)
                self.state.lifecycle_records[command.operation_id] = _LifecycleRecord(
                    CheckAttempt, typed.command_hash, response, inspection, False
                )
                return response
            effect_digest = _effect_digest("check_attempt", typed.command_hash)
            check_digest = _effect_digest("check_result", typed.command_hash)
            response = CheckObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                effect_digest=effect_digest,
                attempt_id=command.attempt_id,
                passed=True,
                head_commit=command.expected_head_commit,
                check_digest=check_digest,
            )
            inspection = AttemptObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                lifecycle_state=TrellisLifecycleState.CHECKED,
                effect_digest=effect_digest,
                attempt_id=command.attempt_id,
                trellis_task_id=command.trellis_task_id,
            )
            self.state.lifecycle_records[command.operation_id] = _LifecycleRecord(
                CheckAttempt, typed.command_hash, response, inspection, True
            )
            self.state.checked_attempts.add(command.attempt_id)
            return response

    def finish_attempt(
        self, effect: PreparedEffect[FinishAttempt]
    ) -> FinishObservation:
        typed = self._require_effect(effect, FinishAttempt)
        command = typed.command
        assert type(command) is FinishAttempt
        with self.state.lock:
            existing = self._existing(typed, FinishObservation)
            if existing is not None:
                assert type(existing) is FinishObservation
                return existing
            checked = command.attempt_id in self.state.checked_attempts
            if command.operation_id in self._unknown_operation_ids or not checked:
                reason = (
                    f"scripted_unknown:{command.operation_id}"
                    if command.operation_id in self._unknown_operation_ids
                    else f"attempt_not_checked:{command.attempt_id}"
                )
                response = self._unknown_finish(command.operation_id, reason)
                inspection = self._unknown_attempt(command.operation_id, reason)
                self.state.lifecycle_records[command.operation_id] = _LifecycleRecord(
                    FinishAttempt, typed.command_hash, response, inspection, False
                )
                return response
            effect_digest = _effect_digest("finish_attempt", typed.command_hash)
            finish_digest = _effect_digest("finish_result", typed.command_hash)
            response = FinishObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                effect_digest=effect_digest,
                attempt_id=command.attempt_id,
                finished=True,
                delivered_commit=command.delivered_commit,
                finish_digest=finish_digest,
            )
            inspection = AttemptObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                lifecycle_state=TrellisLifecycleState.FINISHED,
                effect_digest=effect_digest,
                attempt_id=command.attempt_id,
                trellis_task_id=command.trellis_task_id,
            )
            self.state.lifecycle_records[command.operation_id] = _LifecycleRecord(
                FinishAttempt, typed.command_hash, response, inspection, True
            )
            return response

    def inspect_attempt(
        self,
        operation_id: str,
        *,
        expected_request_payload_hash: str | None = None,
    ) -> AttemptObservation:
        if type(operation_id) is not str:
            raise TypeError("operation_id must be a string")
        with self.state.lock:
            if operation_id in self.state.lifecycle_conflicts:
                return self._unknown_attempt(
                    operation_id, f"operation_id_collision:{operation_id}"
                )
            record = self.state.lifecycle_records.get(operation_id)
            if record is not None and record.command_type is PrepareAttempt:
                if (
                    expected_request_payload_hash is not None
                    and record.command_hash != expected_request_payload_hash
                ):
                    return self._unknown_attempt(
                        operation_id, f"request_hash_mismatch:{operation_id}"
                    )
                assert record.inspection is not None
                return record.inspection
            if record is not None:
                return self._unknown_attempt(
                    operation_id, f"operation_kind_mismatch:{operation_id}"
                )
            return AttemptObservation(
                operation_id=operation_id,
                status=EffectStatus.ABSENT,
                observed_at=self._clock(),
                lifecycle_state=TrellisLifecycleState.ABSENT,
            )

    def inspect_check(
        self,
        operation_id: str,
        *,
        expected_request_payload_hash: str | None = None,
    ) -> CheckObservation:
        if type(operation_id) is not str:
            raise TypeError("operation_id must be a string")
        with self.state.lock:
            if operation_id in self.state.lifecycle_conflicts:
                return self._unknown_check(
                    operation_id, f"operation_id_collision:{operation_id}"
                )
            record = self.state.lifecycle_records.get(operation_id)
            if record is not None and record.command_type is CheckAttempt:
                if (
                    expected_request_payload_hash is not None
                    and record.command_hash != expected_request_payload_hash
                ):
                    return self._unknown_check(
                        operation_id, f"request_hash_mismatch:{operation_id}"
                    )
                assert type(record.response) is CheckObservation
                return record.response
            if record is not None:
                return self._unknown_check(
                    operation_id, f"operation_kind_mismatch:{operation_id}"
                )
            return CheckObservation(
                operation_id=operation_id,
                status=EffectStatus.ABSENT,
                observed_at=self._clock(),
            )

    def inspect_finish(
        self,
        operation_id: str,
        *,
        expected_request_payload_hash: str | None = None,
    ) -> FinishObservation:
        if type(operation_id) is not str:
            raise TypeError("operation_id must be a string")
        with self.state.lock:
            if operation_id in self.state.lifecycle_conflicts:
                return self._unknown_finish(
                    operation_id, f"operation_id_collision:{operation_id}"
                )
            record = self.state.lifecycle_records.get(operation_id)
            if record is not None and record.command_type is FinishAttempt:
                if (
                    expected_request_payload_hash is not None
                    and record.command_hash != expected_request_payload_hash
                ):
                    return self._unknown_finish(
                        operation_id, f"request_hash_mismatch:{operation_id}"
                    )
                assert type(record.response) is FinishObservation
                return record.response
            if record is not None:
                return self._unknown_finish(
                    operation_id, f"operation_kind_mismatch:{operation_id}"
                )
            return FinishObservation(
                operation_id=operation_id,
                status=EffectStatus.ABSENT,
                observed_at=self._clock(),
            )


class FakeBackendChannelPort:
    def __init__(
        self,
        capabilities: BackendCapabilities,
        *,
        state: FakeExternalState | None = None,
        clock: Callable[[], str] = lambda: FIXED_FAKE_TIME,
        unknown_operation_ids: Collection[str] = (),
        send_state: TurnState = TurnState.DONE,
    ) -> None:
        if type(capabilities) is not BackendCapabilities:
            raise TypeError("capabilities must be BackendCapabilities")
        if state is not None and type(state) is not FakeExternalState:
            raise TypeError("state must be a FakeExternalState")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if type(send_state) is not TurnState or send_state in {
            TurnState.ABSENT,
            TurnState.UNKNOWN,
            TurnState.CANCELLED,
        }:
            raise ValueError("send_state must be a send-result state")
        self._capabilities = capabilities
        self.state = FakeExternalState() if state is None else state
        self._clock = clock
        self._unknown_operation_ids = frozenset(unknown_operation_ids)
        self._send_state = send_state

    @property
    def applied_operation_ids(self) -> tuple[str, ...]:
        with self.state.lock:
            return tuple(
                sorted(
                    operation_id
                    for operation_id, record in self.state.channel_records.items()
                    if record.applied
                )
            )

    @property
    def effect_count(self) -> int:
        return len(self.applied_operation_ids)

    def probe(self) -> BackendCapabilities:
        return self._capabilities

    def _require_effect(
        self,
        effect: object,
        command_type: type[ReserveChannel]
        | type[SendTaskPacket]
        | type[CancelTurn],
    ) -> (
        PreparedEffect[ReserveChannel]
        | PreparedEffect[SendTaskPacket]
        | PreparedEffect[CancelTurn]
    ):
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        if type(effect.command) is not command_type:
            raise TypeError(f"effect command must be {command_type.__name__}")
        return effect

    def _unknown_channel(self, operation_id: str, reason: str) -> ChannelObservation:
        return ChannelObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            evidence=(reason,),
        )

    def _unknown_turn(self, operation_id: str, reason: str) -> TurnObservation:
        return TurnObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            state=TurnState.UNKNOWN,
            evidence=(reason,),
        )

    def _existing(
        self,
        effect: PreparedEffect[object],
        response_type: type[ChannelObservation] | type[TurnObservation],
    ) -> ChannelObservation | TurnObservation | None:
        operation_id = effect.operation_id
        if operation_id in self.state.channel_conflicts:
            reason = f"operation_id_collision:{operation_id}"
            if response_type is ChannelObservation:
                return self._unknown_channel(operation_id, reason)
            return self._unknown_turn(operation_id, reason)
        if operation_id in self.state.lifecycle_records:
            self.state.channel_conflicts.add(operation_id)
            self.state.lifecycle_conflicts.add(operation_id)
            reason = f"operation_id_collision:{operation_id}"
            if response_type is ChannelObservation:
                return self._unknown_channel(operation_id, reason)
            return self._unknown_turn(operation_id, reason)
        existing = self.state.channel_records.get(operation_id)
        if existing is None:
            return None
        if (
            existing.command_type is type(effect.command)
            and existing.command_hash == effect.command_hash
            and type(existing.response) is response_type
        ):
            return existing.response
        self.state.channel_conflicts.add(operation_id)
        reason = f"operation_id_collision:{operation_id}"
        if response_type is ChannelObservation:
            return self._unknown_channel(operation_id, reason)
        return self._unknown_turn(operation_id, reason)

    def reserve(
        self, effect: PreparedEffect[ReserveChannel]
    ) -> ChannelObservation:
        typed = self._require_effect(effect, ReserveChannel)
        command = typed.command
        assert type(command) is ReserveChannel
        with self.state.lock:
            existing = self._existing(typed, ChannelObservation)
            if existing is not None:
                assert type(existing) is ChannelObservation
                return existing
            digests_match = (
                command.provider == self._capabilities.provider
                and command.capability_digest == self._capabilities.capability_digest
                and command.launch_profile_digest
                == self._capabilities.launch_profile_digest
                and command.policy_digest == self._capabilities.policy_digest
            )
            reserved_for = self.state.channel_reservations.get(command.channel_id)
            channel_for_attempt = self.state.attempt_channels.get(command.attempt_id)
            collision = reserved_for is not None or channel_for_attempt is not None
            if (
                command.operation_id in self._unknown_operation_ids
                or not digests_match
                or collision
            ):
                if command.operation_id in self._unknown_operation_ids:
                    reason = f"scripted_unknown:{command.operation_id}"
                elif collision:
                    reason = f"channel_id_collision:{command.channel_id}"
                else:
                    reason = "channel_capability_mismatch"
                response = self._unknown_channel(command.operation_id, reason)
                self.state.channel_records[command.operation_id] = _ChannelRecord(
                    ReserveChannel, typed.command_hash, response, False
                )
                return response
            response = ChannelObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                effect_digest=_effect_digest("reserve_channel", typed.command_hash),
                attempt_id=command.attempt_id,
                channel_id=command.channel_id,
                provider=command.provider,
                provider_session_id=_derived_token("session", typed.command_hash),
            )
            self.state.channel_records[command.operation_id] = _ChannelRecord(
                ReserveChannel, typed.command_hash, response, True
            )
            self.state.channel_reservations[command.channel_id] = command.attempt_id
            self.state.attempt_channels[command.attempt_id] = command.channel_id
            return response

    def send(self, effect: PreparedEffect[SendTaskPacket]) -> TurnObservation:
        typed = self._require_effect(effect, SendTaskPacket)
        command = typed.command
        assert type(command) is SendTaskPacket
        with self.state.lock:
            existing = self._existing(typed, TurnObservation)
            if existing is not None:
                assert type(existing) is TurnObservation
                return existing
            reserved_for = self.state.channel_reservations.get(command.channel_id)
            oversized = (
                len(command.task_packet.encode("utf-8"))
                > self._capabilities.max_task_packet_bytes
            )
            turn_owner = self.state.turn_operations.get(command.turn_id)
            message_owner = self.state.message_operations.get(command.message_id)
            collision = turn_owner is not None or message_owner is not None
            if (
                command.operation_id in self._unknown_operation_ids
                or reserved_for != command.attempt_id
                or oversized
                or collision
            ):
                if command.operation_id in self._unknown_operation_ids:
                    reason = f"scripted_unknown:{command.operation_id}"
                elif collision:
                    reason = (
                        f"turn_id_collision:{command.turn_id}"
                        if turn_owner is not None
                        else f"message_id_collision:{command.message_id}"
                    )
                elif oversized:
                    reason = "task_packet_exceeds_capability"
                else:
                    reason = f"channel_not_reserved:{command.channel_id}"
                response = self._unknown_turn(command.operation_id, reason)
                self.state.channel_records[command.operation_id] = _ChannelRecord(
                    SendTaskPacket, typed.command_hash, response, False
                )
                return response
            result_digest = (
                _effect_digest("turn_result", typed.command_hash)
                if self._send_state is TurnState.DONE
                else None
            )
            response = TurnObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                state=self._send_state,
                effect_digest=_effect_digest("send_task_packet", typed.command_hash),
                attempt_id=command.attempt_id,
                channel_id=command.channel_id,
                message_id=command.message_id,
                turn_id=command.turn_id,
                result_digest=result_digest,
            )
            self.state.channel_records[command.operation_id] = _ChannelRecord(
                SendTaskPacket, typed.command_hash, response, True
            )
            self.state.turn_operations[command.turn_id] = command.operation_id
            self.state.message_operations[command.message_id] = command.operation_id
            return response

    def inspect_reservation(self, operation_id: str) -> ChannelObservation:
        if type(operation_id) is not str:
            raise TypeError("operation_id must be a string")
        with self.state.lock:
            if operation_id in self.state.channel_conflicts:
                return self._unknown_channel(
                    operation_id, f"operation_id_collision:{operation_id}"
                )
            record = self.state.channel_records.get(operation_id)
            if record is not None and type(record.response) is ChannelObservation:
                return record.response
            if record is not None:
                return self._unknown_channel(
                    operation_id, f"operation_is_not_a_reservation:{operation_id}"
                )
            return ChannelObservation(
                operation_id=operation_id,
                status=EffectStatus.ABSENT,
                observed_at=self._clock(),
            )

    def inspect_turn(self, operation_id: str) -> TurnObservation:
        if type(operation_id) is not str:
            raise TypeError("operation_id must be a string")
        with self.state.lock:
            if operation_id in self.state.channel_conflicts:
                return self._unknown_turn(
                    operation_id, f"operation_id_collision:{operation_id}"
                )
            record = self.state.channel_records.get(operation_id)
            if record is not None and type(record.response) is TurnObservation:
                return record.response
            if record is not None:
                return self._unknown_turn(
                    operation_id, f"operation_is_not_a_turn:{operation_id}"
                )
            return TurnObservation(
                operation_id=operation_id,
                status=EffectStatus.ABSENT,
                observed_at=self._clock(),
                state=TurnState.ABSENT,
            )

    def cancel(self, effect: PreparedEffect[CancelTurn]) -> TurnObservation:
        typed = self._require_effect(effect, CancelTurn)
        command = typed.command
        assert type(command) is CancelTurn
        with self.state.lock:
            existing = self._existing(typed, TurnObservation)
            if existing is not None:
                assert type(existing) is TurnObservation
                return existing
            source_operation = self.state.turn_operations.get(command.turn_id)
            source = (
                None
                if source_operation is None
                else self.state.channel_records.get(source_operation)
            )
            source_response = None if source is None else source.response
            existing_cancellation = self.state.turn_cancellations.get(command.turn_id)
            source_matches = (
                type(source_response) is TurnObservation
                and source_response.attempt_id == command.attempt_id
                and source_response.channel_id == command.channel_id
                and existing_cancellation is None
            )
            if command.operation_id in self._unknown_operation_ids or not source_matches:
                reason = (
                    f"scripted_unknown:{command.operation_id}"
                    if command.operation_id in self._unknown_operation_ids
                    else f"turn_not_found:{command.turn_id}"
                )
                response = self._unknown_turn(command.operation_id, reason)
                self.state.channel_records[command.operation_id] = _ChannelRecord(
                    CancelTurn, typed.command_hash, response, False
                )
                return response
            assert type(source_response) is TurnObservation
            terminal = source_response.state in {
                TurnState.DONE,
                TurnState.FAILED,
                TurnState.CANCELLED,
            }
            response = TurnObservation(
                operation_id=command.operation_id,
                status=EffectStatus.APPLIED,
                observed_at=self._clock(),
                state=(source_response.state if terminal else TurnState.CANCELLED),
                effect_digest=_effect_digest("cancel_turn", typed.command_hash),
                attempt_id=command.attempt_id,
                channel_id=command.channel_id,
                message_id=source_response.message_id,
                turn_id=command.turn_id,
                result_digest=source_response.result_digest,
            )
            self.state.channel_records[command.operation_id] = _ChannelRecord(
                CancelTurn, typed.command_hash, response, True
            )
            self.state.turn_cancellations[command.turn_id] = command.operation_id
            return response


__all__ = [
    "FIXED_FAKE_TIME",
    "FakeBackendChannelPort",
    "FakeTrellisGraphPort",
    "FakeTrellisLifecyclePort",
    "FakeExternalState",
]
