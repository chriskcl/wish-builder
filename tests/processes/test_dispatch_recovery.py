from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from tests.processes.test_coordinator import (
    BASE_TIME,
    CoordinatorHarness,
    digest,
    lease_owner,
)
from wish_builder.adapters.fake import FakeTaskPort
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    ActorIdentity,
    ActorType,
    CommandIdentity,
    CommandKind,
    DispatchRecoveryPayload,
    EffectOperation,
    EffectReceipt,
    EffectStatus,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseDraftPayload,
    RuntimeReasonCode,
    RuntimeState,
    SchedulerMode,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel import apply_journal_event
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.processes.coordinator import (
    CoordinatorCursor,
    CoordinatorReason,
    CoordinatorStatus,
    ForegroundCoordinator,
)
from wish_builder.services.journal import (
    AppendStatus,
    DurableJournal,
    JournalEventDraft,
)
from wish_builder.services.recovery import (
    LeaseRecoveryFaultCode,
    LeaseRecoveryStatus,
    advance_dispatch_recoveries,
    recover_coordinator_lease,
)


def recovery_evidence(identity: ExecutionIdentity) -> tuple[EvidenceRef, ...]:
    common = {
        "schema_version": 1,
        "byte_length": 64,
        "created_at": "2026-08-19T00:00:10Z",
        "sensitivity": EvidenceSensitivity.INTERNAL,
        "render_policy": EvidenceRenderPolicy.METADATA_ONLY,
        "role": EvidenceRole.REQUIRED,
    }
    return (
        EvidenceRef(
            digest=digest("c"),
            evidence_type=EvidenceType.EFFECT_RECEIPT,
            producer=EvidenceProducer(identity, event_id="EVENT-RECEIPT-PROOF-001"),
            **common,
        ),
        EvidenceRef(
            digest=digest("d"),
            evidence_type=EvidenceType.PROCESS,
            producer=EvidenceProducer(
                identity,
                external_object_id="process-tree-proof-001",
            ),
            **common,
        ),
    )


def recovery_proof(
    coordinator: ForegroundCoordinator,
    request: JournalEvent,
) -> DispatchRecoveryPayload:
    actor = ActorIdentity(
        ActorType.HUMAN,
        "local-account-001",
        "host-001",
        5001,
        "process-start-human-001",
    )
    head = coordinator.cursor.head
    return DispatchRecoveryPayload(
        "RECOVERY-DISPATCH-001",
        CommandIdentity(
            1,
            "COMMAND-RECONCILE-001",
            "REQUEST-RECONCILE-001",
            CommandKind.RECONCILE,
            head.sequence,
            "nonce-reconcile-001",
            actor,
            SourceChannel.DIRECT_CLI,
            "2026-08-19T00:00:10Z",
        ),
        request.identity,
        request.event_id,
        request.sequence,
        request.event_hash,
        EffectReceipt(
            1,
            request.identity,
            EffectOperation.WORKER_DISPATCH,
            EffectStatus.ABSENT,
            "2026-08-19T00:00:10Z",
        ),
        True,
        head.sequence,
        head.event_hash,
        recovery_evidence(request.identity),
    )


def take_over(
    harness: CoordinatorHarness,
    coordinator: ForegroundCoordinator | None = None,
    *,
    new_epoch: int = 2,
) -> ForegroundCoordinator:
    source = coordinator or harness.coordinator
    cursor = source.cursor
    lease = cursor.lease_state.lease
    assert lease is not None
    lost = harness.journal.append_draft(
        JournalEventDraft(
            f"EVENT-LEASE-LOST-RECOVERY-{lease.fencing_token:03d}",
            JournalEventType.LEASE_LOST,
            ExecutionIdentity(harness.manifest.run_id, lease.fencing_token),
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
    self_event = lost.event
    if lost.status is not AppendStatus.COMMITTED or self_event is None:
        raise AssertionError(lost)
    applied = apply_journal_event(cursor.snapshot, self_event)
    if not applied.accepted:
        raise AssertionError(applied.reason)
    lease_state = cursor.lease_state.advance(self_event)
    graph = cursor.graph_index.advance(cursor.snapshot, applied.snapshot)
    recoveries = advance_dispatch_recoveries(cursor.dispatch_recoveries, self_event)

    coordinator_id = f"coordinator-{new_epoch:03d}"
    new_owner = lease_owner(coordinator_id)
    acquired = harness.journal.append_draft(
        JournalEventDraft(
            f"EVENT-LEASE-ACQUIRED-RECOVERY-{new_epoch:03d}",
            JournalEventType.LEASE_ACQUIRED,
            ExecutionIdentity(harness.manifest.run_id, new_epoch),
            ActorType.COORDINATOR,
            coordinator_id,
            LeaseDraftPayload(
                f"LEASE-RECOVERY-{new_epoch:03d}",
                coordinator_id,
                new_owner,
                SchedulerMode.WISH_BUILDER,
                new_epoch,
                harness.manifest.canonical_sha256(),
                300,
                10,
            ),
        ),
        expected_head=lease_state.head,
        lease_state=lease_state,
    )
    acquired_event = acquired.event
    if acquired.status is not AppendStatus.COMMITTED or acquired_event is None:
        raise AssertionError(acquired)
    acquired_state = apply_journal_event(applied.snapshot, acquired_event)
    if not acquired_state.accepted:
        raise AssertionError(acquired_state.reason)
    lease_state = lease_state.advance(acquired_event)
    graph = graph.advance(applied.snapshot, acquired_state.snapshot)
    recoveries = advance_dispatch_recoveries(recoveries, acquired_event)
    return ForegroundCoordinator(
        harness.manifest,
        CoordinatorCursor(
            acquired_state.snapshot,
            graph,
            lease_state,
            recoveries,
        ),
        harness.journal,
        harness.port,
        coordinator_id=coordinator_id,
        owner=new_owner,
        fencing_token=new_epoch,
        authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
    )


class UnknownDispatchRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_human_proof_reopens_task_then_run_and_preserves_old_attempt(self) -> None:
        def failpoint(point: str, path: Path) -> None:
            del path
            if point == "before_effect":
                raise OSError("effect outcome unavailable")

        harness = CoordinatorHarness(
            self.root,
            port=FakeTaskPort(
                self.root / "effects",
                clock=lambda: "2026-08-19T00:00:10Z",
                failpoint=failpoint,
            ),
        )
        blocked = harness.coordinator.dispatch_ready()
        request = next(
            event
            for event in blocked.events
            if event.event_type is JournalEventType.DISPATCH_REQUESTED
        )
        coordinator = take_over(harness)
        proof = recovery_proof(coordinator, request)

        recovered = coordinator.resume_unknown_dispatch(request, proof)

        self.assertEqual(CoordinatorStatus.PROGRESSED, recovered.status)
        self.assertEqual(
            [
                JournalEventType.RECOVERY_COMPLETED,
                JournalEventType.TASK_RETRY_SCHEDULED,
                JournalEventType.RUN_RESUMED,
            ],
            [event.event_type for event in recovered.events],
        )
        self.assertEqual(RuntimeState.RUNNING, recovered.cursor.snapshot.status)
        self.assertEqual(RuntimeState.READY, recovered.cursor.snapshot.tasks[0].state)
        self.assertEqual(
            RuntimeState.OUTCOME_UNKNOWN,
            recovered.cursor.snapshot.attempts[0].state,
        )
        exact_retry = coordinator.resume_unknown_dispatch(request, proof)
        self.assertEqual(CoordinatorStatus.PROGRESSED, exact_retry.status)
        self.assertEqual((), exact_retry.events)

    def test_crash_prefixes_restart_without_opening_dispatch_early(self) -> None:
        for crash_after_retry in (False, True):
            with self.subTest(crash_after_retry=crash_after_retry):
                root = self.root / ("after-retry" if crash_after_retry else "after-proof")
                harness, request = self._blocked_harness(root)
                coordinator = take_over(harness)
                proof = recovery_proof(coordinator, request)
                proof_result = coordinator._append_payload(
                    JournalEventType.RECOVERY_COMPLETED,
                    ExecutionIdentity(harness.manifest.run_id, 2),
                    proof,
                    actor_type=ActorType.HUMAN,
                    actor_id="local-account-001",
                    allow_recovery=True,
                )
                self.assertIsNotNone(proof_result.event)
                if crash_after_retry:
                    retry_result = coordinator._append_transition(
                        JournalEventType.TASK_RETRY_SCHEDULED,
                        ExecutionIdentity(
                            harness.manifest.run_id,
                            2,
                            request.identity.task_id,
                        ),
                        TransitionSubject.TASK,
                        RuntimeState.BLOCKED,
                        RuntimeState.READY,
                        actor_type=ActorType.HUMAN,
                        actor_id="local-account-001",
                        evidence=proof.evidence,
                        allow_recovery=True,
                    )
                    self.assertIsNotNone(retry_result.event)

                restarted = self._restart(harness)
                self.assertEqual(RuntimeState.BLOCKED, restarted.cursor.snapshot.status)
                self.assertEqual((), restarted.cursor.graph_index.ready_tasks)
                expected_task = (
                    RuntimeState.READY if crash_after_retry else RuntimeState.BLOCKED
                )
                self.assertEqual(expected_task, restarted.cursor.snapshot.tasks[0].state)

                resumed = restarted.resume_unknown_dispatch(request, proof)
                expected = (
                    [JournalEventType.RUN_RESUMED]
                    if crash_after_retry
                    else [
                        JournalEventType.TASK_RETRY_SCHEDULED,
                        JournalEventType.RUN_RESUMED,
                    ]
                )
                self.assertEqual(expected, [event.event_type for event in resumed.events])

    def test_cross_epoch_takeover_continues_existing_proof(self) -> None:
        harness, request = self._blocked_harness(self.root / "cross-epoch")
        epoch_two = take_over(harness)
        proof = recovery_proof(epoch_two, request)
        appended = epoch_two._append_payload(
            JournalEventType.RECOVERY_COMPLETED,
            ExecutionIdentity(harness.manifest.run_id, 2),
            proof,
            actor_type=ActorType.HUMAN,
            actor_id="local-account-001",
            allow_recovery=True,
        )
        self.assertIsNotNone(appended.event)

        epoch_three = take_over(harness, epoch_two, new_epoch=3)
        resumed = epoch_three.resume_unknown_dispatch(request, proof)

        self.assertEqual(CoordinatorStatus.PROGRESSED, resumed.status)
        self.assertEqual(
            [3, 3],
            [event.identity.coordinator_epoch for event in resumed.events],
        )
        self.assertEqual(
            [JournalEventType.TASK_RETRY_SCHEDULED, JournalEventType.RUN_RESUMED],
            [event.event_type for event in resumed.events],
        )

    def test_recovery_replay_blocks_an_out_of_order_prefix(self) -> None:
        harness, request = self._blocked_harness(self.root / "out-of-order")
        coordinator = take_over(harness)
        proof = recovery_proof(coordinator, request)
        appended = coordinator._append_payload(
            JournalEventType.RECOVERY_COMPLETED,
            ExecutionIdentity(harness.manifest.run_id, 2),
            proof,
            actor_type=ActorType.HUMAN,
            actor_id="local-account-001",
            allow_recovery=True,
        )
        self.assertIsNotNone(appended.event)
        cursor = coordinator.cursor
        invalid = harness.journal.append_draft(
            JournalEventDraft(
                "EVENT-RUN-RESUMED-OUT-OF-ORDER",
                JournalEventType.RUN_RESUMED,
                ExecutionIdentity(harness.manifest.run_id, 2),
                ActorType.HUMAN,
                "local-account-001",
                TransitionPayload(
                    TransitionSubject.RUN,
                    RuntimeState.BLOCKED,
                    RuntimeState.RUNNING,
                    proof.evidence,
                ),
            ),
            expected_head=cursor.head,
        )
        self.assertEqual(AppendStatus.COMMITTED, invalid.status)

        recovered = recover_coordinator_lease(
            harness.storage.root,
            harness.manifest,
            coordinator_epoch=1,
            repair_derived=False,
        )

        self.assertEqual(LeaseRecoveryStatus.BLOCKED, recovered.status)
        assert recovered.fault is not None
        self.assertEqual(
            LeaseRecoveryFaultCode.RECOVERY_PREFIX_INVALID,
            recovered.fault.code,
        )

    def test_coordinator_rejects_stale_forged_and_conflicting_recovery(self) -> None:
        harness, request = self._blocked_harness(self.root / "rejections")
        coordinator = take_over(harness)
        proof = recovery_proof(coordinator, request)

        with self.assertRaises(TypeError):
            coordinator.resume_unknown_dispatch(object(), proof)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            coordinator.resume_unknown_dispatch(request, object())  # type: ignore[arg-type]

        different_request = JournalEvent.create(
            sequence=request.sequence,
            event_id="EVENT-DISPATCH-REQUESTED-DIFFERENT",
            event_type=request.event_type,
            identity=request.identity,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            recorded_at=request.recorded_at,
            previous_event_hash=request.previous_event_hash,
            payload=request.payload,
        )
        rejected = coordinator.resume_unknown_dispatch(different_request, proof)
        self.assertEqual(CoordinatorReason.RECOVERY_PROOF_INVALID, rejected.reason)

        stale = replace(
            proof,
            command=replace(
                proof.command,
                expected_sequence=proof.last_valid_sequence - 1,
            ),
            last_valid_sequence=proof.last_valid_sequence - 1,
        )
        rejected = coordinator.resume_unknown_dispatch(request, stale)
        self.assertEqual(CoordinatorReason.RECOVERY_PROOF_INVALID, rejected.reason)

        appended = coordinator._append_payload(
            JournalEventType.RECOVERY_COMPLETED,
            ExecutionIdentity(harness.manifest.run_id, 2),
            proof,
            actor_type=ActorType.HUMAN,
            actor_id="local-account-001",
            allow_recovery=True,
        )
        self.assertIsNotNone(appended.event)
        blocked_dispatch = coordinator.dispatch_ready()
        self.assertEqual(CoordinatorReason.RECOVERY_IN_PROGRESS, blocked_dispatch.reason)

        changed = replace(
            proof,
            command=replace(proof.command, request_nonce="nonce-reconcile-changed"),
        )
        conflicted = coordinator.resume_unknown_dispatch(request, changed)
        self.assertEqual(CoordinatorReason.RECOVERY_CONFLICT, conflicted.reason)

        second = replace(proof, recovery_id="RECOVERY-DISPATCH-SECOND")
        conflicted = coordinator.resume_unknown_dispatch(request, second)
        self.assertEqual(CoordinatorReason.RECOVERY_CONFLICT, conflicted.reason)

    def test_exactly_one_unresolved_unknown_and_prefix_state_are_required(self) -> None:
        harness, request = self._blocked_harness(self.root / "state-guards")
        coordinator = take_over(harness)
        proof = recovery_proof(coordinator, request)
        cursor = coordinator.cursor
        unknown = cursor.snapshot.attempts[0]
        extra = replace(
            unknown,
            attempt=unknown.attempt + 1,
            correlation_id="CORRELATION-TASK-001-0002-EPOCH-0001",
        )
        multiple_snapshot = replace(
            cursor.snapshot,
            attempts=(*cursor.snapshot.attempts, extra),
        )
        multiple = self._with_snapshot(harness, coordinator, multiple_snapshot)
        rejected = multiple.resume_unknown_dispatch(request, proof)
        self.assertEqual(CoordinatorReason.RECOVERY_PROOF_INVALID, rejected.reason)

        appended = coordinator._append_payload(
            JournalEventType.RECOVERY_COMPLETED,
            ExecutionIdentity(harness.manifest.run_id, 2),
            proof,
            actor_type=ActorType.HUMAN,
            actor_id="local-account-001",
            allow_recovery=True,
        )
        self.assertIsNotNone(appended.event)
        cursor = coordinator.cursor
        forged_task = replace(
            cursor.snapshot.tasks[0],
            state=RuntimeState.READY,
            reason_code=None,
        )
        forged_snapshot = replace(cursor.snapshot, tasks=(forged_task,))
        forged = self._with_snapshot(harness, coordinator, forged_snapshot)
        conflicted = forged.resume_unknown_dispatch(request, proof)
        self.assertEqual(CoordinatorReason.RECOVERY_CONFLICT, conflicted.reason)

    def _blocked_harness(
        self,
        root: Path,
    ) -> tuple[CoordinatorHarness, JournalEvent]:
        def failpoint(point: str, path: Path) -> None:
            del path
            if point == "before_effect":
                raise OSError("effect outcome unavailable")

        harness = CoordinatorHarness(
            root,
            port=FakeTaskPort(
                root / "effects",
                clock=lambda: "2026-08-19T00:00:10Z",
                failpoint=failpoint,
            ),
        )
        blocked = harness.coordinator.dispatch_ready()
        request = next(
            event
            for event in blocked.events
            if event.event_type is JournalEventType.DISPATCH_REQUESTED
        )
        return harness, request

    def _restart(self, harness: CoordinatorHarness) -> ForegroundCoordinator:
        recovered = recover_coordinator_lease(
            harness.storage.root,
            harness.manifest,
            coordinator_epoch=1,
            repair_derived=False,
        )
        self.assertEqual(LeaseRecoveryStatus.RECOVERED, recovered.status)
        assert recovered.lease_state is not None
        lease = recovered.lease_state.lease
        assert lease is not None
        journal = DurableJournal(
            harness.manifest.run_id,
            FilesystemJournalStorage(
                harness.storage.root,
                harness.manifest.run_id,
                authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
            ),
        )
        return ForegroundCoordinator(
            harness.manifest,
            CoordinatorCursor(
                recovered.replay.snapshot,
                recovered.replay.graph_index,
                recovered.lease_state,
                recovered.dispatch_recoveries,
            ),
            journal,
            harness.port,
            coordinator_id=lease.coordinator_id,
            owner=lease.owner,
            fencing_token=lease.fencing_token,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
        )

    def _with_snapshot(
        self,
        harness: CoordinatorHarness,
        coordinator: ForegroundCoordinator,
        snapshot,
    ) -> ForegroundCoordinator:
        cursor = coordinator.cursor
        lease = cursor.lease_state.lease
        assert lease is not None
        return ForegroundCoordinator(
            harness.manifest,
            CoordinatorCursor(
                snapshot,
                GraphIndex.rebuild(harness.manifest, snapshot),
                cursor.lease_state,
                cursor.dispatch_recoveries,
            ),
            harness.journal,
            harness.port,
            coordinator_id=lease.coordinator_id,
            owner=lease.owner,
            fencing_token=lease.fencing_token,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
        )


if __name__ == "__main__":
    unittest.main()
