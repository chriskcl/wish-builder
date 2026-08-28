from __future__ import annotations

from wish_builder.adapters import FilesystemExternalEvidenceStore

import dataclasses
import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import WorkerProvider
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEventType,
)
from wish_builder.services import (
    BackendEffectRecoveryService,
    GENESIS_HEAD,
    AppendResult,
    AppendStatus,
    DispatchRecoveryProjectionError,
    DurableJournal,
    JournalEventDraft,
    JournalHead,
    PendingExternalEffect,
    TrellisLifecycleEffectRecoveryService,
    ExternalEffectRecoveryReason,
    ExternalEffectRecoveryStatus,
    project_pending_external_effects,
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


RUN_ID = "RUN-TRELLIS-RECOVERY"
TASK_ID = "TASK-001"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
FIXED_TIME = "2026-08-19T00:00:00Z"


def capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        provider=WorkerProvider.CODEX,
        platform="windows",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest=HASH_C,
        max_task_packet_bytes=4096,
    )


def reserve_command(operation_id: str) -> ReserveChannel:
    return ReserveChannel(
        operation_id,
        "ATTEMPT-001",
        "DISPATCH-001",
        "CHANNEL-001",
        WorkerProvider.CODEX,
        HASH_A,
        HASH_B,
        HASH_C,
    )


def send_command(operation_id: str) -> SendTaskPacket:
    packet = '{"task_id":"TASK-001"}'
    return SendTaskPacket(
        operation_id,
        "ATTEMPT-001",
        "DISPATCH-001",
        "CHANNEL-001",
        "MESSAGE-001",
        "TURN-001",
        packet,
        "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest(),
    )


def cancel_command(operation_id: str) -> CancelTurn:
    return CancelTurn(
        operation_id,
        "ATTEMPT-001",
        "CHANNEL-001",
        "TURN-001",
        "recovery_cancel",
    )


def prepare_command(operation_id: str) -> PrepareAttempt:
    return PrepareAttempt(
        operation_id,
        RUN_ID,
        "PARENT-001",
        "TRELLIS-TASK-001",
        TASK_ID,
        1,
        "DISPATCH-001",
        HASH_A,
        HASH_B,
        "1" * 40,
    )


def check_command(operation_id: str) -> CheckAttempt:
    return CheckAttempt(
        operation_id,
        "ATTEMPT-001",
        "TRELLIS-TASK-001",
        TASK_ID,
        HASH_A,
        "2" * 40,
    )


def finish_command(operation_id: str) -> FinishAttempt:
    return FinishAttempt(
        operation_id,
        "ATTEMPT-001",
        "TRELLIS-TASK-001",
        TASK_ID,
        "2" * 40,
        HASH_B,
    )


class RecordingChannel:
    def __init__(self) -> None:
        self.reservations: dict[str, ChannelObservation] = {}
        self.turns: dict[str, TurnObservation] = {}
        self.calls: list[tuple[str, str]] = []

    def probe(self) -> BackendCapabilities:
        return capabilities()

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
        self, effect: PreparedEffect[ReserveChannel]
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

    def send(self, effect: PreparedEffect[SendTaskPacket]) -> TurnObservation:
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

    def cancel(self, effect: PreparedEffect[CancelTurn]) -> TurnObservation:
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
        self.calls: list[tuple[str, str]] = []
        self.attempts: dict[str, AttemptObservation] = {}
        self.checks: dict[str, CheckObservation] = {}
        self.finishes: dict[str, FinishObservation] = {}

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
            "ATTEMPT-001",
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

class BackendDispatchRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self, name: str) -> tuple[DurableJournal, FilesystemExternalEvidenceStore]:
        root = self.root / name
        return (
            DurableJournal(
                RUN_ID,
                FilesystemJournalStorage(root / "journal", RUN_ID),
            ),
            FilesystemExternalEvidenceStore(root / "evidence"),
        )

    def append_request(
        self,
        journal: DurableJournal,
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
        epoch: int = 1,
    ) -> tuple[PendingExternalEffect, JournalHead]:
        object_type = {
            EffectOperation.PREPARE_ATTEMPT: EffectObjectType.ATTEMPT,
            EffectOperation.RESERVE_CHANNEL: EffectObjectType.CHANNEL,
            EffectOperation.SEND_TASK_PACKET: EffectObjectType.TASK_PACKET,
            EffectOperation.CANCEL_TURN: EffectObjectType.TURN,
            EffectOperation.CHECK_ATTEMPT: EffectObjectType.ATTEMPT,
            EffectOperation.FINISH_ATTEMPT: EffectObjectType.ATTEMPT,
        }[operation]
        identity = ExecutionIdentity(RUN_ID, epoch, TASK_ID, 1, command.operation_id)
        result = journal.append_draft(
            JournalEventDraft(
                event_id=f"EVENT-EFFECT-REQUESTED-{command.operation_id}",
                event_type=JournalEventType.EFFECT_REQUESTED,
                identity=identity,
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                payload=EffectRequestPayload(
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
                    HASH_A,
                    command.canonical_sha256(),
                    0,
                    epoch,
                ),
            ),
            expected_head=GENESIS_HEAD,
        )
        assert result.event is not None and result.head is not None
        return PendingExternalEffect(result.event), result.head

    def backend_service(
        self,
        journal: DurableJournal,
        channel: RecordingChannel,
        evidence: FilesystemExternalEvidenceStore,
        *,
        epoch: int = 1,
        admitted=lambda: True,
    ) -> BackendEffectRecoveryService:
        return BackendEffectRecoveryService(
            journal,
            channel,
            evidence,
            coordinator_id="coordinator-001",
            fencing_token=epoch,
            retry_admitted=admitted,
        )

    def lifecycle_service(
        self,
        journal: DurableJournal,
        lifecycle: RecordingLifecycle,
        evidence: FilesystemExternalEvidenceStore,
        *,
        epoch: int = 1,
        admitted=lambda: True,
    ) -> TrellisLifecycleEffectRecoveryService:
        return TrellisLifecycleEffectRecoveryService(
            journal,
            lifecycle,
            evidence,
            coordinator_id="coordinator-001",
            fencing_token=epoch,
            retry_admitted=admitted,
        )

    def test_projection_pairs_reconciled_child_effects(self) -> None:
        journal, evidence = self.fixture("projection")
        command = reserve_command("RESERVE-PROJECTION")
        pending, head = self.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL
        )
        self.assertEqual((pending,), project_pending_external_effects((pending.request_event,)))

        channel = RecordingChannel()
        channel.reserve(
            PreparedEffect.from_append_result(
                self._idempotent_append(pending.request_event), command
            )
        )
        result = self.backend_service(journal, channel, evidence).reconcile(
            pending, expected_head=head
        )
        self.assertIs(result.status, ExternalEffectRecoveryStatus.RECONCILED)
        assert result.event is not None
        self.assertEqual(
            (),
            project_pending_external_effects((pending.request_event, result.event)),
        )
        with self.assertRaisesRegex(
            DispatchRecoveryProjectionError, "no matching request"
        ):
            project_pending_external_effects((result.event,))

    def test_same_operation_concurrent_recovery_retries_only_once(self) -> None:
        journal, evidence = self.fixture("same-operation-concurrent")
        second_journal = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(
                self.root / "same-operation-concurrent" / "journal",
                RUN_ID,
            ),
        )
        command = reserve_command("RESERVE-SAME-OPERATION-CONCURRENT")
        pending, head = self.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL
        )
        retry_calls = 0
        retry_guard = threading.Lock()
        start = threading.Barrier(3)

        class StaleChannel(RecordingChannel):
            def reserve(
                self, effect: PreparedEffect[ReserveChannel]
            ) -> ChannelObservation:
                nonlocal retry_calls
                with retry_guard:
                    retry_calls += 1
                return super().reserve(effect)

        services = (
            self.backend_service(journal, StaleChannel(), evidence),
            self.backend_service(second_journal, StaleChannel(), evidence),
        )

        def recover(service: BackendEffectRecoveryService):
            start.wait(timeout=5)
            return service.reconcile(
                pending,
                expected_head=head,
                retry_command=command,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(executor.submit(recover, service) for service in services)
            start.wait(timeout=5)
            results = tuple(future.result(timeout=5) for future in futures)

        self.assertEqual(1, retry_calls)
        self.assertTrue(
            all(
                result.status is ExternalEffectRecoveryStatus.RETRIED
                for result in results
            )
        )
        self.assertEqual(results[0], results[1])

    def test_same_identifiers_in_distinct_journals_do_not_share_recovery(self) -> None:
        first_journal, first_evidence = self.fixture("same-identifiers-first")
        second_journal, second_evidence = self.fixture("same-identifiers-second")
        command = reserve_command("RESERVE-SAME-IDENTIFIERS")
        first_pending, first_head = self.append_request(
            first_journal,
            command,
            EffectOperation.RESERVE_CHANNEL,
        )
        second_pending, second_head = self.append_request(
            second_journal,
            command,
            EffectOperation.RESERVE_CHANNEL,
        )
        first_channel = RecordingChannel()
        second_channel = RecordingChannel()

        first = self.backend_service(
            first_journal,
            first_channel,
            first_evidence,
        ).reconcile(
            first_pending,
            expected_head=first_head,
            retry_command=command,
        )
        second = self.backend_service(
            second_journal,
            second_channel,
            second_evidence,
        ).reconcile(
            second_pending,
            expected_head=second_head,
            retry_command=command,
        )

        self.assertIs(first.status, ExternalEffectRecoveryStatus.RETRIED)
        self.assertIs(second.status, ExternalEffectRecoveryStatus.RETRIED)
        self.assertEqual(1, first_channel.calls.count(("reserve", command.operation_id)))
        self.assertEqual(1, second_channel.calls.count(("reserve", command.operation_id)))

    def test_unrelated_operations_recover_concurrently(self) -> None:
        first_journal, first_evidence = self.fixture("unrelated-first")
        second_journal, second_evidence = self.fixture("unrelated-second")
        first_command = reserve_command("RESERVE-UNRELATED-FIRST")
        second_command = reserve_command("RESERVE-UNRELATED-SECOND")
        first_pending, first_head = self.append_request(
            first_journal,
            first_command,
            EffectOperation.RESERVE_CHANNEL,
        )
        second_pending, second_head = self.append_request(
            second_journal,
            second_command,
            EffectOperation.RESERVE_CHANNEL,
        )
        retry_overlap = threading.Barrier(2)

        class OverlapChannel(RecordingChannel):
            def reserve(
                self, effect: PreparedEffect[ReserveChannel]
            ) -> ChannelObservation:
                retry_overlap.wait(timeout=5)
                return super().reserve(effect)

        calls = (
            (
                self.backend_service(
                    first_journal, OverlapChannel(), first_evidence
                ),
                first_pending,
                first_head,
                first_command,
            ),
            (
                self.backend_service(
                    second_journal, OverlapChannel(), second_evidence
                ),
                second_pending,
                second_head,
                second_command,
            ),
        )

        def recover(call: tuple[object, ...]):
            service, pending, head, command = call
            assert isinstance(service, BackendEffectRecoveryService)
            assert isinstance(pending, PendingExternalEffect)
            assert isinstance(head, JournalHead)
            assert isinstance(command, ReserveChannel)
            return service.reconcile(
                pending,
                expected_head=head,
                retry_command=command,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(recover, calls, timeout=10))

        self.assertTrue(
            all(
                result.status is ExternalEffectRecoveryStatus.RETRIED
                for result in results
            )
        )

    def test_applied_inspection_appends_effect_reconciled_without_retry(self) -> None:
        journal, evidence = self.fixture("applied")
        command = reserve_command("RESERVE-APPLIED")
        pending, head = self.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL
        )
        channel = RecordingChannel()
        channel.reservations[command.operation_id] = ChannelObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            HASH_A,
            command.attempt_id,
            command.channel_id,
            command.provider,
            "SESSION-001",
        )
        result = self.backend_service(
            journal,
            channel,
            evidence,
            admitted=lambda: self.fail("APPLIED recovery must not consult retry admission"),
        ).reconcile(pending, expected_head=head)

        self.assertIs(result.status, ExternalEffectRecoveryStatus.RECONCILED)
        self.assertIs(result.reason, ExternalEffectRecoveryReason.NONE)
        assert result.event is not None and result.receipt is not None
        self.assertIs(result.event.event_type, JournalEventType.EFFECT_RECONCILED)
        self.assertIs(result.receipt.status, EffectStatus.APPLIED)
        self.assertEqual(
            [("inspect_reservation", command.operation_id)], channel.calls
        )

    def test_unknown_inspection_fails_closed_without_journal_write(self) -> None:
        journal, evidence = self.fixture("unknown")
        command = send_command("SEND-UNKNOWN")
        pending, head = self.append_request(
            journal, command, EffectOperation.SEND_TASK_PACKET
        )
        channel = RecordingChannel()
        channel.turns[command.operation_id] = TurnObservation(
            command.operation_id,
            EffectStatus.UNKNOWN,
            FIXED_TIME,
            TurnState.UNKNOWN,
            evidence=("inspection_timeout",),
        )
        result = self.backend_service(journal, channel, evidence).reconcile(
            pending,
            expected_head=head,
            retry_command=command,
        )

        self.assertIs(result.status, ExternalEffectRecoveryStatus.BLOCKED)
        self.assertIs(result.reason, ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN)
        self.assertEqual(head, result.head)
        self.assertIsNone(result.event)
        self.assertEqual([("inspect_turn", command.operation_id)], channel.calls)

    def test_inspection_failure_and_non_boolean_admission_fail_closed(self) -> None:
        journal, evidence = self.fixture("inspect-failure")
        command = reserve_command("RESERVE-INSPECT-FAILURE")
        pending, head = self.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL
        )
        channel = RecordingChannel()

        def raise_inspection(_operation_id: str):
            raise OSError("simulated inspection loss")

        channel.inspect_reservation = raise_inspection  # type: ignore[method-assign]
        failed = self.backend_service(journal, channel, evidence).reconcile(
            pending, expected_head=head, retry_command=command
        )
        self.assertIs(failed.status, ExternalEffectRecoveryStatus.BLOCKED)
        self.assertIs(failed.reason, ExternalEffectRecoveryReason.OBSERVATION_UNKNOWN)

        other_journal, other_evidence = self.fixture("non-boolean-admission")
        other_command = reserve_command("RESERVE-NON-BOOLEAN")
        other_pending, other_head = self.append_request(
            other_journal, other_command, EffectOperation.RESERVE_CHANNEL
        )
        other_channel = RecordingChannel()
        rejected = self.backend_service(
            other_journal,
            other_channel,
            other_evidence,
            admitted=lambda: 1,
        ).reconcile(
            other_pending,
            expected_head=other_head,
            retry_command=other_command,
        )
        self.assertIs(rejected.status, ExternalEffectRecoveryStatus.BLOCKED)
        self.assertIs(rejected.reason, ExternalEffectRecoveryReason.RETRY_NOT_ADMITTED)
        self.assertEqual(
            [("inspect_reservation", other_command.operation_id)],
            other_channel.calls,
        )

    def test_absent_same_epoch_retries_reserve_send_and_cancel(self) -> None:
        cases = (
            ("reserve", reserve_command, EffectOperation.RESERVE_CHANNEL),
            ("send", send_command, EffectOperation.SEND_TASK_PACKET),
            ("cancel", cancel_command, EffectOperation.CANCEL_TURN),
        )
        for retry_method, factory, operation in cases:
            with self.subTest(operation=operation.value):
                journal, evidence = self.fixture(operation.value)
                command = factory(f"OP-{operation.value.upper().replace('_', '-')}")
                pending, head = self.append_request(
                    journal, command, operation
                )
                channel = RecordingChannel()
                admission_calls = 0

                def admitted() -> bool:
                    nonlocal admission_calls
                    admission_calls += 1
                    return True

                result = self.backend_service(
                    journal, channel, evidence, admitted=admitted
                ).reconcile(
                    pending,
                    expected_head=head,
                    retry_command=command,
                )
                self.assertIs(result.status, ExternalEffectRecoveryStatus.RETRIED)
                self.assertEqual(2, admission_calls)
                self.assertEqual(
                    [
                        (
                            "inspect_reservation"
                            if operation is EffectOperation.RESERVE_CHANNEL
                            else "inspect_turn",
                            command.operation_id,
                        ),
                        (retry_method, command.operation_id),
                    ],
                    channel.calls,
                )
                assert result.event is not None
                self.assertIs(
                    result.event.event_type, JournalEventType.EFFECT_RECONCILED
                )
                payload = result.event.payload
                assert type(payload) is EffectObservationPayload
                self.assertIs(payload.adapter, AdapterKind.BACKEND)

    def test_absent_same_epoch_retries_each_typed_lifecycle_operation(self) -> None:
        cases = (
            (
                "prepare_attempt",
                prepare_command("PREPARE-RECOVERY"),
                EffectOperation.PREPARE_ATTEMPT,
                "inspect_attempt",
            ),
            (
                "check_attempt",
                check_command("CHECK-RECOVERY"),
                EffectOperation.CHECK_ATTEMPT,
                "inspect_check",
            ),
            (
                "finish_attempt",
                finish_command("FINISH-RECOVERY"),
                EffectOperation.FINISH_ATTEMPT,
                "inspect_finish",
            ),
        )
        for execute_method, command, operation, inspect_method in cases:
            with self.subTest(operation=operation.value):
                journal, evidence = self.fixture(f"lifecycle-{operation.value}")
                pending, head = self.append_request(journal, command, operation)
                lifecycle = RecordingLifecycle()

                result = self.lifecycle_service(
                    journal, lifecycle, evidence
                ).reconcile(
                    pending,
                    expected_head=head,
                    retry_command=command,
                )

                self.assertIs(result.status, ExternalEffectRecoveryStatus.RETRIED)
                self.assertEqual(
                    [
                        (inspect_method, command.operation_id),
                        (execute_method, command.operation_id),
                    ],
                    lifecycle.calls,
                )
                assert result.event is not None
                payload = result.event.payload
                assert type(payload) is EffectObservationPayload
                self.assertIs(payload.adapter, AdapterKind.TRELLIS)
                self.assertEqual(
                    (),
                    project_pending_external_effects(
                        (pending.request_event, result.event)
                    ),
                )

    def test_applied_lifecycle_effect_reconciles_across_coordinator_epoch(self) -> None:
        journal, evidence = self.fixture("lifecycle-cross-epoch")
        command = prepare_command("PREPARE-CROSS-EPOCH")
        pending, head = self.append_request(
            journal,
            command,
            EffectOperation.PREPARE_ATTEMPT,
            epoch=1,
        )
        lifecycle = RecordingLifecycle()
        lifecycle.attempts[command.operation_id] = AttemptObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            TrellisLifecycleState.PREPARED,
            HASH_A,
            "ATTEMPT-001",
            command.trellis_task_id,
            "WORKTREE-001",
            "C:/worktree",
            command.expected_base_commit,
        )

        result = self.lifecycle_service(
            journal,
            lifecycle,
            evidence,
            epoch=2,
            admitted=lambda: self.fail("applied lifecycle effect must not retry"),
        ).reconcile(pending, expected_head=head)

        self.assertIs(result.status, ExternalEffectRecoveryStatus.RECONCILED)
        assert result.event is not None and result.receipt is not None
        self.assertEqual(2, result.event.identity.coordinator_epoch)
        self.assertEqual(1, result.receipt.identity.coordinator_epoch)

    def test_backend_and_lifecycle_services_reject_cross_routing(self) -> None:
        backend_journal, backend_evidence = self.fixture("backend-cross-route")
        backend_command = reserve_command("RESERVE-CROSS-ROUTE")
        backend_pending, backend_head = self.append_request(
            backend_journal,
            backend_command,
            EffectOperation.RESERVE_CHANNEL,
        )
        lifecycle = RecordingLifecycle()

        wrong_lifecycle = self.lifecycle_service(
            backend_journal,
            lifecycle,
            backend_evidence,
        ).reconcile(backend_pending, expected_head=backend_head)

        self.assertIs(wrong_lifecycle.status, ExternalEffectRecoveryStatus.BLOCKED)
        self.assertIs(
            wrong_lifecycle.reason,
            ExternalEffectRecoveryReason.ADAPTER_MISMATCH,
        )
        self.assertEqual([], lifecycle.calls)

        lifecycle_journal, lifecycle_evidence = self.fixture(
            "lifecycle-cross-route"
        )
        lifecycle_command = prepare_command("PREPARE-CROSS-ROUTE")
        lifecycle_pending, lifecycle_head = self.append_request(
            lifecycle_journal,
            lifecycle_command,
            EffectOperation.PREPARE_ATTEMPT,
        )
        backend = RecordingChannel()

        wrong_backend = self.backend_service(
            lifecycle_journal,
            backend,
            lifecycle_evidence,
        ).reconcile(lifecycle_pending, expected_head=lifecycle_head)

        self.assertIs(wrong_backend.status, ExternalEffectRecoveryStatus.BLOCKED)
        self.assertIs(
            wrong_backend.reason,
            ExternalEffectRecoveryReason.ADAPTER_MISMATCH,
        )
        self.assertEqual([], backend.calls)

    def test_recovery_services_require_their_narrow_ports(self) -> None:
        journal, evidence = self.fixture("narrow-ports")

        with self.assertRaisesRegex(TypeError, "BackendChannelPort"):
            BackendEffectRecoveryService(
                journal,
                RecordingLifecycle(),
                evidence,
                coordinator_id="coordinator-001",
                fencing_token=1,
                retry_admitted=lambda: True,
            )
        with self.assertRaisesRegex(TypeError, "TrellisLifecyclePort"):
            TrellisLifecycleEffectRecoveryService(
                journal,
                RecordingChannel(),
                evidence,
                coordinator_id="coordinator-001",
                fencing_token=1,
                retry_admitted=lambda: True,
            )

    def test_retry_rechecks_admission_immediately_before_effect(self) -> None:
        journal, evidence = self.fixture("revoked")
        command = reserve_command("RESERVE-REVOKED")
        pending, head = self.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL
        )
        channel = RecordingChannel()
        decisions = iter((True, False))
        result = self.backend_service(
            journal,
            channel,
            evidence,
            admitted=lambda: next(decisions),
        ).reconcile(pending, expected_head=head, retry_command=command)

        self.assertIs(result.status, ExternalEffectRecoveryStatus.BLOCKED)
        self.assertIs(result.reason, ExternalEffectRecoveryReason.RETRY_NOT_ADMITTED)
        self.assertEqual(
            [("inspect_reservation", command.operation_id)], channel.calls
        )

    def test_absent_old_epoch_is_never_redispatched(self) -> None:
        journal, evidence = self.fixture("stale")
        command = reserve_command("RESERVE-STALE")
        pending, head = self.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL, epoch=1
        )
        channel = RecordingChannel()
        result = self.backend_service(
            journal,
            channel,
            evidence,
            epoch=2,
            admitted=lambda: self.fail("stale effects must not consult retry admission"),
        ).reconcile(pending, expected_head=head, retry_command=command)

        self.assertIs(result.status, ExternalEffectRecoveryStatus.BLOCKED)
        self.assertIs(result.reason, ExternalEffectRecoveryReason.STALE_EPOCH)
        self.assertEqual(
            [("inspect_reservation", command.operation_id)], channel.calls
        )

    def test_applied_old_epoch_is_reconciled_but_never_redispatched(self) -> None:
        journal, evidence = self.fixture("stale-applied")
        command = reserve_command("RESERVE-STALE-APPLIED")
        pending, head = self.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL, epoch=1
        )
        channel = RecordingChannel()
        channel.reservations[command.operation_id] = ChannelObservation(
            command.operation_id,
            EffectStatus.APPLIED,
            FIXED_TIME,
            HASH_A,
            command.attempt_id,
            command.channel_id,
            command.provider,
            "SESSION-001",
        )
        result = self.backend_service(
            journal,
            channel,
            evidence,
            epoch=2,
            admitted=lambda: self.fail("APPLIED effects must not be redispatched"),
        ).reconcile(pending, expected_head=head, retry_command=command)

        self.assertIs(result.status, ExternalEffectRecoveryStatus.RECONCILED)
        assert result.event is not None and result.receipt is not None
        self.assertEqual(2, result.event.identity.coordinator_epoch)
        self.assertEqual(1, result.receipt.identity.coordinator_epoch)
        self.assertEqual(
            (),
            project_pending_external_effects(
                (pending.request_event, result.event)
            ),
        )
        self.assertEqual(
            [("inspect_reservation", command.operation_id)], channel.calls
        )

    def test_retry_command_must_match_original_canonical_hash(self) -> None:
        journal, evidence = self.fixture("mismatch")
        command = reserve_command("RESERVE-MISMATCH")
        pending, head = self.append_request(
            journal, command, EffectOperation.RESERVE_CHANNEL
        )
        mismatched = dataclasses.replace(command, channel_id="CHANNEL-OTHER")
        channel = RecordingChannel()
        result = self.backend_service(journal, channel, evidence).reconcile(
            pending,
            expected_head=head,
            retry_command=mismatched,
        )
        self.assertIs(result.status, ExternalEffectRecoveryStatus.BLOCKED)
        self.assertIs(result.reason, ExternalEffectRecoveryReason.RETRY_COMMAND_MISMATCH)
        self.assertEqual(
            [("inspect_reservation", command.operation_id)], channel.calls
        )

    @staticmethod
    def _idempotent_append(event) -> AppendResult:
        return AppendResult(
            AppendStatus.IDEMPOTENT,
            JournalHead(event.sequence, event.event_hash),
            event,
        )


if __name__ == "__main__":
    unittest.main()
