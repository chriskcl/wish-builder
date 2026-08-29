from __future__ import annotations

from wish_builder.adapters import FilesystemExternalEvidenceStore

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.processes.test_coordinator import (
    COORDINATOR_ID,
    CoordinatorHarness,
    lease_owner,
    sibling_manifest,
)
from wish_builder.contracts import SchedulerMode, WorkerProvider
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEventType,
    LeaseDraftPayload,
    RuntimeReasonCode,
)
from wish_builder.kernel.state import ApplyReason, apply_journal_event
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.processes.coordinator import CoordinatorCursor
from wish_builder.processes.production_recovery import (
    ProductionExternalEffectRecoveryReason,
    ProductionExternalEffectRecoveryResult,
    reconcile_pending_external_effects,
    resolve_external_recovery_command,
)
from wish_builder.services.dispatch_recovery import PendingExternalEffect
from wish_builder.services.journal import (
    AppendStatus,
    JournalEventDraft,
)
from wish_builder.services.recovery import (
    LeaseRecoveryResult,
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)
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
    TurnObservation,
    TurnState,
    TrellisLifecycleState,
)
from wish_builder.services.backend_effects import BackendDispatchPlan
from wish_builder.services.trellis_lifecycle_effects import (
    lifecycle_target_object_hash,
)
from wish_builder.services.external_recovery import ExternalEffectRecoveryReason


FIXED_TIME = "2026-08-19T00:00:10Z"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


class RecordingChannel:
    def __init__(self) -> None:
        self.reservations: dict[str, ChannelObservation] = {}
        self.turns: dict[str, TurnObservation] = {}
        self.calls: list[tuple[str, str]] = []

    def probe(self) -> BackendCapabilities:
        return BackendCapabilities(
            WorkerProvider.CODEX,
            "windows",
            HASH_A,
            HASH_B,
            HASH_C,
            4096,
        )

    def inspect_reservation(self, operation_id: str) -> ChannelObservation:
        self.calls.append(("inspect_reservation", operation_id))
        return self.reservations.get(
            operation_id,
            ChannelObservation(operation_id, EffectStatus.ABSENT, FIXED_TIME),
        )

    def inspect_turn(self, operation_id: str) -> TurnObservation:
        self.calls.append(("inspect_turn", operation_id))
        return self.turns.get(
            operation_id,
            TurnObservation(
                operation_id,
                EffectStatus.ABSENT,
                FIXED_TIME,
                TurnState.ABSENT,
            ),
        )

    def reserve(
        self,
        effect: PreparedEffect[ReserveChannel],
    ) -> ChannelObservation:
        command = effect.command
        self.calls.append(("reserve", command.operation_id))
        observation = ChannelObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            HASH_A,
            command.attempt_id,
            command.channel_id,
            command.provider,
            "SESSION-001",
        )
        self.reservations[command.operation_id] = observation
        return observation

    def send(
        self,
        effect: PreparedEffect[SendTaskPacket],
    ) -> TurnObservation:
        command = effect.command
        self.calls.append(("send", command.operation_id))
        observation = TurnObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            TurnState.DONE,
            HASH_A,
            command.attempt_id,
            command.channel_id,
            command.message_id,
            command.turn_id,
            HASH_B,
        )
        self.turns[command.operation_id] = observation
        return observation

    def cancel(
        self,
        effect: PreparedEffect[CancelTurn],
    ) -> TurnObservation:
        command = effect.command
        self.calls.append(("cancel", command.operation_id))
        observation = TurnObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            TurnState.CANCELLED,
            HASH_A,
            command.attempt_id,
            command.channel_id,
            "MESSAGE-001",
            command.turn_id,
        )
        self.turns[command.operation_id] = observation
        return observation


class RecordingLifecycle:
    def __init__(self) -> None:
        self.attempts: dict[str, AttemptObservation] = {}
        self.checks: dict[str, CheckObservation] = {}
        self.finishes: dict[str, FinishObservation] = {}
        self.calls: list[tuple[str, str]] = []

    def inspect_attempt(self, operation_id: str) -> AttemptObservation:
        self.calls.append(("inspect_attempt", operation_id))
        return self.attempts.get(
            operation_id,
            AttemptObservation(
                operation_id,
                EffectStatus.ABSENT,
                FIXED_TIME,
                TrellisLifecycleState.ABSENT,
            ),
        )

    def inspect_check(self, operation_id: str) -> CheckObservation:
        self.calls.append(("inspect_check", operation_id))
        return self.checks.get(
            operation_id,
            CheckObservation(operation_id, EffectStatus.ABSENT, FIXED_TIME),
        )

    def inspect_finish(self, operation_id: str) -> FinishObservation:
        self.calls.append(("inspect_finish", operation_id))
        return self.finishes.get(
            operation_id,
            FinishObservation(operation_id, EffectStatus.ABSENT, FIXED_TIME),
        )

    def prepare_attempt(
        self, effect: PreparedEffect[PrepareAttempt]
    ) -> AttemptObservation:
        command = effect.command
        self.calls.append(("prepare_attempt", command.operation_id))
        observation = AttemptObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            TrellisLifecycleState.PREPARED,
            HASH_A,
            command.operation_id,
            command.trellis_task_id,
            "WORKTREE-001",
            "C:/worktree",
            command.expected_base_commit,
        )
        self.attempts[command.operation_id] = observation
        return observation

    def check_attempt(
        self, effect: PreparedEffect[CheckAttempt]
    ) -> CheckObservation:
        command = effect.command
        self.calls.append(("check_attempt", command.operation_id))
        observation = CheckObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            HASH_A,
            command.attempt_id,
            True,
            command.expected_head_commit,
            HASH_B,
        )
        self.checks[command.operation_id] = observation
        return observation

    def finish_attempt(
        self, effect: PreparedEffect[FinishAttempt]
    ) -> FinishObservation:
        command = effect.command
        self.calls.append(("finish_attempt", command.operation_id))
        observation = FinishObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            HASH_A,
            command.attempt_id,
            True,
            command.delivered_commit,
            HASH_B,
        )
        self.finishes[command.operation_id] = observation
        return observation


class ProductionExternalRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.harness = CoordinatorHarness(self.root / "control")
        self.cursor = self.harness.coordinator.cursor
        self.backend = RecordingChannel()
        self.lifecycle = RecordingLifecycle()
        self.evidence = FilesystemExternalEvidenceStore(self.root / "evidence")

    def plan(self, label: str = "001") -> BackendDispatchPlan:
        packet = '{"task_id":"TASK-001"}'
        return BackendDispatchPlan(
            ReserveChannel(
                f"RESERVE-{label}",
                f"ATTEMPT-{label}",
                f"DISPATCH-{label}",
                f"CHANNEL-{label}",
                WorkerProvider.CODEX,
                HASH_A,
                HASH_B,
                HASH_C,
            ),
            SendTaskPacket(
                f"SEND-{label}",
                f"ATTEMPT-{label}",
                f"DISPATCH-{label}",
                f"CHANNEL-{label}",
                f"MESSAGE-{label}",
                f"TURN-{label}",
                packet,
                "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest(),
            ),
        )

    def append_pending(
        self,
        cursor: CoordinatorCursor,
        command: (
            PrepareAttempt
            | ReserveChannel
            | SendTaskPacket
            | CancelTurn
            | CheckAttempt
            | FinishAttempt
        ),
        operation: EffectOperation,
        *,
        target_hash: str | None = None,
    ) -> tuple[PendingExternalEffect, CoordinatorCursor]:
        object_type = {
            EffectOperation.PREPARE_ATTEMPT: EffectObjectType.ATTEMPT,
            EffectOperation.RESERVE_CHANNEL: EffectObjectType.CHANNEL,
            EffectOperation.SEND_TASK_PACKET: EffectObjectType.TASK_PACKET,
            EffectOperation.CANCEL_TURN: EffectObjectType.TURN,
            EffectOperation.CHECK_ATTEMPT: EffectObjectType.ATTEMPT,
            EffectOperation.FINISH_ATTEMPT: EffectObjectType.ATTEMPT,
        }[operation]
        identity = ExecutionIdentity(
            self.harness.manifest.run_id,
            cursor.snapshot.coordinator_epoch,
            "TASK-001",
            1,
            command.operation_id,
        )
        if target_hash is None:
            target_hash = (
                lifecycle_target_object_hash(identity, command, operation)
                if type(command) in {PrepareAttempt, CheckAttempt, FinishAttempt}
                else HASH_A
            )
        appended = self.harness.journal.append_draft(
            JournalEventDraft(
                f"EVENT-EFFECT-REQUESTED-{command.operation_id}",
                JournalEventType.EFFECT_REQUESTED,
                identity,
                ActorType.COORDINATOR,
                COORDINATOR_ID,
                EffectRequestPayload(
                    operation,
                    (
                        AdapterKind.TRELLIS
                        if operation
                        in {
                            EffectOperation.PREPARE_ATTEMPT,
                            EffectOperation.CHECK_ATTEMPT,
                            EffectOperation.FINISH_ATTEMPT,
                        }
                        else AdapterKind.BACKEND
                    ),
                    object_type,
                    target_hash,
                    command.canonical_sha256(),
                    cursor.head.sequence,
                    identity.coordinator_epoch,
                ),
            ),
            expected_head=cursor.head,
        )
        self.assertIs(appended.status, AppendStatus.COMMITTED)
        assert appended.event is not None
        return PendingExternalEffect(appended.event), self.adopt(
            cursor,
            appended.event,
        )

    def adopt(self, cursor: CoordinatorCursor, event) -> CoordinatorCursor:
        applied = apply_journal_event(cursor.snapshot, event)
        if applied.accepted:
            snapshot = applied.snapshot
        else:
            self.assertIs(applied.reason, ApplyReason.UNSUPPORTED_EVENT)
            snapshot = replace(
                cursor.snapshot,
                last_sequence=event.sequence,
                last_event_id=event.event_id,
                last_event_hash=event.event_hash,
            )
        return CoordinatorCursor(
            snapshot,
            cursor.graph_index.advance(cursor.snapshot, snapshot),
            cursor.lease_state.advance(event),
            cursor.dispatch_recoveries,
        )

    def recover(
        self,
        pending: tuple[PendingExternalEffect, ...],
        cursor: CoordinatorCursor,
        *,
        plan_factory=None,
        retry_admitted=lambda: True,
        command_resolver=None,
        lease_recovery: LeaseRecoveryResult | None = None,
        manifest=None,
    ):
        if lease_recovery is None:
            lease_recovery = self.authoritative_recovery(cursor)
        keywords = {
            "lease_recovery": lease_recovery,
            "manifest": manifest or self.harness.manifest,
            "journal": self.harness.journal,
            "backend_channel": self.backend,
            "trellis_lifecycle": self.lifecycle,
            "evidence_store": self.evidence,
            "cursor": cursor,
            "plan_factory": plan_factory,
            "retry_admitted": retry_admitted,
        }
        if command_resolver is not None:
            keywords["command_resolver"] = command_resolver
        return reconcile_pending_external_effects(pending, **keywords)

    def authoritative_recovery(
        self,
        cursor: CoordinatorCursor,
    ) -> LeaseRecoveryResult:
        recovered = recover_coordinator_lease(
            self.harness.storage.root,
            self.harness.manifest,
            coordinator_epoch=self.cursor.snapshot.coordinator_epoch,
            repair_derived=False,
        )
        self.assertIs(recovered.status, LeaseRecoveryStatus.RECOVERED)
        self.assertEqual(cursor.head, recovered.replay.head)
        return recovered

    def test_applied_effect_is_reconciled_without_plan_or_retry(self) -> None:
        plan = self.plan()
        pending, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )
        self.backend.reservations[plan.reserve.operation_id] = ChannelObservation(
            plan.reserve.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            HASH_A,
            plan.reserve.attempt_id,
            plan.reserve.channel_id,
            plan.reserve.provider,
            "SESSION-001",
        )

        result = self.recover(
            (pending,),
            cursor,
            retry_admitted=lambda: self.fail("APPLIED must not retry"),
        )

        self.assertTrue(result.success)
        self.assertEqual((plan.reserve.operation_id,), result.completed_operation_ids)
        self.assertEqual(1, len(result.events))
        self.assertEqual(result.events[-1].event_hash, result.cursor.head.event_hash)
        self.assertEqual(
            [
                ("inspect_reservation", plan.reserve.operation_id),
                ("inspect_reservation", plan.reserve.operation_id),
            ],
            self.backend.calls,
        )

    def test_absent_effect_retries_after_deterministic_reconstruction(self) -> None:
        plan = self.plan()
        pending, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )
        admissions = 0

        def admitted() -> bool:
            nonlocal admissions
            admissions += 1
            return True

        result = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: plan,
            retry_admitted=admitted,
        )

        self.assertTrue(result.success)
        self.assertEqual(2, admissions)
        self.assertEqual(
            [
                ("inspect_reservation", plan.reserve.operation_id),
                ("inspect_reservation", plan.reserve.operation_id),
                ("reserve", plan.reserve.operation_id),
            ],
            self.backend.calls,
        )

    def test_absent_lifecycle_effect_uses_typed_resolver_without_dispatch_plan(self) -> None:
        plan = self.plan()
        trellis_task_id = next(
            item.trellis_task_id
            for item in self.harness.manifest.task_id_mapping
            if item.task_id == "TASK-001"
        )
        command = PrepareAttempt(
            operation_id=plan.reserve.attempt_id,
            run_id=self.harness.manifest.run_id,
            parent_task_id=self.harness.manifest.trellis_parent_task_id,
            trellis_task_id=trellis_task_id,
            task_id="TASK-001",
            attempt=1,
            dispatch_id=plan.reserve.dispatch_id,
            manifest_digest=self.harness.manifest.canonical_sha256(),
            trellis_graph_digest=self.harness.manifest.trellis_graph_digest,
            expected_base_commit="1" * 40,
        )
        pending, cursor = self.append_pending(
            self.cursor,
            command,
            EffectOperation.PREPARE_ATTEMPT,
        )

        result = self.recover(
            (pending,),
            cursor,
            command_resolver=lambda item, selected_plan: (
                command if selected_plan is None else None
            ),
        )

        self.assertTrue(result.success)
        self.assertEqual((command.operation_id,), result.completed_operation_ids)
        self.assertEqual(
            [
                ("inspect_attempt", command.operation_id),
                ("inspect_attempt", command.operation_id),
                ("prepare_attempt", command.operation_id),
            ],
            self.lifecycle.calls,
        )

    def test_lifecycle_target_hash_mismatch_blocks_before_retry(self) -> None:
        plan = self.plan()
        trellis_task_id = next(
            item.trellis_task_id
            for item in self.harness.manifest.task_id_mapping
            if item.task_id == "TASK-001"
        )
        command = PrepareAttempt(
            operation_id=plan.reserve.attempt_id,
            run_id=self.harness.manifest.run_id,
            parent_task_id=self.harness.manifest.trellis_parent_task_id,
            trellis_task_id=trellis_task_id,
            task_id="TASK-001",
            attempt=1,
            dispatch_id=plan.reserve.dispatch_id,
            manifest_digest=self.harness.manifest.canonical_sha256(),
            trellis_graph_digest=self.harness.manifest.trellis_graph_digest,
            expected_base_commit="1" * 40,
        )
        pending, cursor = self.append_pending(
            self.cursor,
            command,
            EffectOperation.PREPARE_ATTEMPT,
            target_hash=HASH_A,
        )

        result = self.recover(
            (pending,),
            cursor,
            command_resolver=lambda item, selected_plan: command,
            retry_admitted=lambda: self.fail("invalid target must not retry"),
        )

        self.assertFalse(result.success)
        self.assertIs(result.reason, ProductionExternalEffectRecoveryReason.COMMAND_REQUIRED)
        self.assertNotIn(
            ("prepare_attempt", command.operation_id),
            self.lifecycle.calls,
        )

    def test_unknown_effect_fails_closed_without_plan_or_journal_write(self) -> None:
        plan = self.plan()
        pending, cursor = self.append_pending(
            self.cursor,
            plan.send,
            EffectOperation.SEND_TASK_PACKET,
        )
        self.backend.turns[plan.send.operation_id] = TurnObservation(
            plan.send.operation_id,
            EffectStatus.UNKNOWN,
            FIXED_TIME,
            TurnState.UNKNOWN,
            evidence=("inspection_timeout",),
        )

        result = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: self.fail("UNKNOWN must not need a plan"),
        )

        self.assertFalse(result.success)
        self.assertIs(
            result.reason,
            ProductionExternalEffectRecoveryReason.CHILD_RECOVERY_BLOCKED,
        )
        assert result.child_result is not None
        self.assertIs(
            result.child_result.reason,
            ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN,
        )
        self.assertEqual(cursor, result.cursor)
        self.assertEqual((), result.events)
        self.assertEqual(
            [("inspect_turn", plan.send.operation_id)],
            self.backend.calls,
        )

    def test_absent_effect_from_stale_epoch_is_never_retried(self) -> None:
        plan = self.plan()
        pending, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )
        cursor = self.take_over(cursor)

        result = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: self.fail("stale effect must not need a plan"),
            retry_admitted=lambda: self.fail("stale effect must not retry"),
        )

        self.assertFalse(result.success)
        assert result.child_result is not None
        self.assertIs(result.child_result.reason, ExternalEffectRecoveryReason.STALE_EPOCH)
        self.assertEqual(
            [("inspect_reservation", plan.reserve.operation_id)],
            self.backend.calls,
        )

    def test_cursor_journal_mismatch_blocks_before_inspection(self) -> None:
        plan = self.plan()
        pending, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )
        other = self.plan("002")
        _, advanced = self.append_pending(
            cursor,
            other.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )

        result = self.recover(
            (pending,),
            cursor,
            lease_recovery=self.authoritative_recovery(advanced),
        )

        self.assertFalse(result.success)
        self.assertIs(
            result.reason,
            ProductionExternalEffectRecoveryReason.CURSOR_HEAD_MISMATCH,
        )
        self.assertEqual([], self.backend.calls)
        self.assertEqual([], self.lifecycle.calls)

    def test_unknown_in_batch_blocks_before_reconciliation_or_retry(self) -> None:
        plan = self.plan()
        reserve, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )
        sent, cursor = self.append_pending(
            cursor,
            plan.send,
            EffectOperation.SEND_TASK_PACKET,
        )
        self.backend.turns[plan.send.operation_id] = TurnObservation(
            plan.send.operation_id,
            EffectStatus.UNKNOWN,
            FIXED_TIME,
            TurnState.UNKNOWN,
            evidence=("inspection_timeout",),
        )

        result = self.recover(
            # Reversed input proves the helper uses durable Journal order.
            (sent, reserve),
            cursor,
            plan_factory=lambda item: self.fail("UNKNOWN batch must not retry"),
        )

        self.assertFalse(result.success)
        self.assertEqual((), result.completed_operation_ids)
        self.assertEqual(plan.send.operation_id, result.blocked_operation_id)
        self.assertEqual((), result.events)
        self.assertEqual(cursor, result.cursor)
        assert result.child_result is not None
        self.assertIs(
            result.child_result.reason,
            ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN,
        )
        self.assertEqual(
            [
                ("inspect_reservation", plan.reserve.operation_id),
                ("inspect_turn", plan.send.operation_id),
            ],
            self.backend.calls,
        )

    def test_invalid_later_command_blocks_the_whole_batch_before_retry(self) -> None:
        plan = self.plan()
        reserve, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )
        sent, cursor = self.append_pending(
            cursor,
            plan.send,
            EffectOperation.SEND_TASK_PACKET,
        )
        wrong_packet = '{"task_id":"TASK-TAMPERED"}'
        wrong_send = replace(
            plan.send,
            task_packet=wrong_packet,
            task_packet_digest=(
                "sha256:" + hashlib.sha256(wrong_packet.encode("utf-8")).hexdigest()
            ),
        )

        result = self.recover(
            (reserve, sent),
            cursor,
            plan_factory=lambda item: plan,
            command_resolver=lambda item, candidate: (
                plan.reserve
                if item.operation is EffectOperation.RESERVE_CHANNEL
                else wrong_send
            ),
            retry_admitted=lambda: self.fail("invalid batch must not retry"),
        )

        self.assertFalse(result.success)
        self.assertIs(result.reason, ProductionExternalEffectRecoveryReason.COMMAND_REQUIRED)
        self.assertEqual((), result.events)
        self.assertEqual((), result.completed_operation_ids)
        self.assertEqual(
            [
                ("inspect_reservation", plan.reserve.operation_id),
                ("inspect_turn", plan.send.operation_id),
            ],
            self.backend.calls,
        )

    def test_cancel_retry_requires_a_deterministic_custom_resolver(self) -> None:
        plan = self.plan()
        command = CancelTurn(
            "CANCEL-001",
            plan.send.attempt_id,
            plan.send.channel_id,
            plan.send.turn_id,
            "recovery_cancel",
        )
        pending, cursor = self.append_pending(
            self.cursor,
            command,
            EffectOperation.CANCEL_TURN,
        )

        blocked = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: plan,
        )
        self.assertFalse(blocked.success)
        self.assertIs(
            blocked.reason,
            ProductionExternalEffectRecoveryReason.COMMAND_REQUIRED,
        )
        self.assertNotIn(("cancel", command.operation_id), self.backend.calls)

        recovered = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: plan,
            command_resolver=lambda item, candidate: command,
        )
        self.assertTrue(recovered.success)
        self.assertIn(("cancel", command.operation_id), self.backend.calls)

    def test_result_and_public_inputs_reject_invalid_shapes(self) -> None:
        valid = {
            "success": False,
            "reason": ProductionExternalEffectRecoveryReason.INVALID_INPUT,
            "cursor": self.cursor,
            "events": (),
            "completed_operation_ids": (),
            "blocked_operation_id": None,
            "child_result": None,
        }
        result_cases = (
            ({"success": 1}, TypeError),
            ({"reason": "invalid_input"}, TypeError),
            ({"cursor": object()}, TypeError),
            ({"events": []}, TypeError),
            ({"events": (object(),)}, TypeError),
            ({"completed_operation_ids": []}, TypeError),
            ({"completed_operation_ids": ("",)}, TypeError),
            ({"blocked_operation_id": ""}, TypeError),
            ({"blocked_operation_id": 1}, TypeError),
            ({"child_result": object()}, TypeError),
            (
                {
                    "success": True,
                    "reason": ProductionExternalEffectRecoveryReason.INVALID_INPUT,
                },
                ValueError,
            ),
            (
                {
                    "success": False,
                    "reason": ProductionExternalEffectRecoveryReason.NONE,
                },
                ValueError,
            ),
        )
        for changes, error in result_cases:
            with self.subTest(result_changes=changes), self.assertRaises(error):
                ProductionExternalEffectRecoveryResult(**(valid | changes))

        keywords = {
            "lease_recovery": self.authoritative_recovery(self.cursor),
            "manifest": self.harness.manifest,
            "journal": self.harness.journal,
            "backend_channel": self.backend,
            "trellis_lifecycle": self.lifecycle,
            "evidence_store": self.evidence,
            "cursor": self.cursor,
            "plan_factory": None,
            "retry_admitted": lambda: True,
        }
        input_cases = (
            ({"cursor": object()}, TypeError),
            ({"lease_recovery": object()}, TypeError),
            ({"manifest": object()}, TypeError),
            ({"journal": object()}, TypeError),
            ({"backend_channel": object()}, TypeError),
            ({"trellis_lifecycle": object()}, TypeError),
            ({"evidence_store": object()}, TypeError),
            ({"plan_factory": object()}, TypeError),
            ({"retry_admitted": object()}, TypeError),
            ({"command_resolver": object()}, TypeError),
        )
        for changes, error in input_cases:
            with self.subTest(input_changes=changes), self.assertRaises(error):
                reconcile_pending_external_effects((), **(keywords | changes))

        invalid = reconcile_pending_external_effects([], **keywords)
        self.assertIs(invalid.reason, ProductionExternalEffectRecoveryReason.INVALID_INPUT)

        plan = self.plan()
        pending, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )
        duplicate = self.recover((pending, pending), cursor)
        self.assertIs(
            duplicate.reason,
            ProductionExternalEffectRecoveryReason.PENDING_ORDER_INVALID,
        )
        mismatched_graph = CoordinatorCursor(
            cursor.snapshot,
            GraphIndex.compile(sibling_manifest()),
            cursor.lease_state,
        )
        invalid_graph = self.recover((pending,), mismatched_graph)
        self.assertIs(
            invalid_graph.reason,
            ProductionExternalEffectRecoveryReason.INVALID_INPUT,
        )
        inactive = CoordinatorCursor(
            cursor.snapshot,
            cursor.graph_index,
            replace(cursor.lease_state, event_type=JournalEventType.LEASE_LOST),
        )
        invalid_lease = self.recover((pending,), inactive)
        self.assertIs(
            invalid_lease.reason,
            ProductionExternalEffectRecoveryReason.INVALID_INPUT,
        )

        with self.assertRaises(TypeError):
            resolve_external_recovery_command(object(), plan)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            resolve_external_recovery_command(
                pending,
                object(),  # type: ignore[arg-type]
            )

    def test_recovered_authority_must_match_cursor_manifest_and_pending_batch(
        self,
    ) -> None:
        stale_recovery = self.authoritative_recovery(self.cursor)
        plan = self.plan()
        pending, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )
        current_recovery = self.authoritative_recovery(cursor)

        stale = self.recover(
            (pending,),
            cursor,
            lease_recovery=stale_recovery,
        )
        self.assertIs(
            stale.reason,
            ProductionExternalEffectRecoveryReason.CURSOR_HEAD_MISMATCH,
        )

        wrong_manifest = self.recover(
            (pending,),
            cursor,
            lease_recovery=current_recovery,
            manifest=sibling_manifest(),
        )
        self.assertIs(
            wrong_manifest.reason,
            ProductionExternalEffectRecoveryReason.INVALID_INPUT,
        )

        incomplete_batch = self.recover(
            (),
            cursor,
            lease_recovery=current_recovery,
        )
        self.assertIs(
            incomplete_batch.reason,
            ProductionExternalEffectRecoveryReason.INVALID_INPUT,
        )
        self.assertEqual([], self.backend.calls)
        self.assertEqual([], self.lifecycle.calls)

    def test_absent_reconstruction_and_admission_fail_closed(self) -> None:
        plan = self.plan()
        pending, cursor = self.append_pending(
            self.cursor,
            plan.reserve,
            EffectOperation.RESERVE_CHANNEL,
        )

        missing = self.recover((pending,), cursor)
        self.assertIs(missing.reason, ProductionExternalEffectRecoveryReason.PLAN_REQUIRED)

        raised_plan = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: (_ for _ in ()).throw(RuntimeError("lost")),
        )
        self.assertIs(
            raised_plan.reason,
            ProductionExternalEffectRecoveryReason.PLAN_REQUIRED,
        )
        invalid_plan = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: object(),
        )
        self.assertIs(
            invalid_plan.reason,
            ProductionExternalEffectRecoveryReason.PLAN_REQUIRED,
        )
        raised_resolver = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: plan,
            command_resolver=lambda item, candidate: (_ for _ in ()).throw(
                RuntimeError("lost")
            ),
        )
        self.assertIs(
            raised_resolver.reason,
            ProductionExternalEffectRecoveryReason.COMMAND_REQUIRED,
        )
        wrong_mapping = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: self.plan("OTHER"),
        )
        self.assertIs(
            wrong_mapping.reason,
            ProductionExternalEffectRecoveryReason.COMMAND_REQUIRED,
        )

        denied = self.recover(
            (pending,),
            cursor,
            plan_factory=lambda item: plan,
            retry_admitted=lambda: False,
        )
        self.assertIs(
            denied.reason,
            ProductionExternalEffectRecoveryReason.CHILD_RECOVERY_BLOCKED,
        )
        assert denied.child_result is not None
        self.assertIs(
            denied.child_result.reason,
            ExternalEffectRecoveryReason.RETRY_NOT_ADMITTED,
        )

        send_pending, send_cursor = self.append_pending(
            cursor,
            plan.send,
            EffectOperation.SEND_TASK_PACKET,
        )
        sent = self.recover(
            (pending, send_pending),
            send_cursor,
            plan_factory=lambda item: plan,
        )
        self.assertTrue(sent.success)

    def take_over(self, cursor: CoordinatorCursor) -> CoordinatorCursor:
        lease = cursor.lease_state.lease
        assert lease is not None
        lost = self.harness.journal.append_draft(
            JournalEventDraft(
                "EVENT-LEASE-LOST-PRODUCTION-RECOVERY",
                JournalEventType.LEASE_LOST,
                ExecutionIdentity(self.harness.manifest.run_id, 1),
                ActorType.SYSTEM,
                "recovery",
                LeaseDraftPayload(
                    lease.lease_id,
                    lease.coordinator_id,
                    lease.owner,
                    lease.scheduler_mode,
                    lease.fencing_token,
                    lease.manifest_digest,
                    lease.lease_ttl_seconds,
                    lease.lease_clock_skew_seconds,
                ),
                RuntimeReasonCode.LEASE_LOST,
            ),
            expected_head=cursor.head,
            lease_state=cursor.lease_state,
        )
        self.assertIs(lost.status, AppendStatus.COMMITTED)
        assert lost.event is not None
        cursor = self.adopt(cursor, lost.event)

        coordinator_id = "coordinator-002"
        owner = lease_owner(coordinator_id)
        acquired = self.harness.journal.append_draft(
            JournalEventDraft(
                "EVENT-LEASE-ACQUIRED-PRODUCTION-RECOVERY",
                JournalEventType.LEASE_ACQUIRED,
                ExecutionIdentity(self.harness.manifest.run_id, 2),
                ActorType.COORDINATOR,
                coordinator_id,
                LeaseDraftPayload(
                    "LEASE-002",
                    coordinator_id,
                    owner,
                    SchedulerMode.WISH_BUILDER,
                    2,
                    self.harness.manifest.canonical_sha256(),
                    300,
                    10,
                ),
            ),
            expected_head=cursor.head,
            lease_state=cursor.lease_state,
        )
        self.assertIs(acquired.status, AppendStatus.COMMITTED)
        assert acquired.event is not None
        return self.adopt(cursor, acquired.event)


if __name__ == "__main__":
    unittest.main()
