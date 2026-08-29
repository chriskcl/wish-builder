from __future__ import annotations

import errno
import multiprocessing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.kernel.test_validation import valid_manifest
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    ActorIdentity,
    ActorType,
    ExecutionIdentity,
    JournalEventType,
    LeaseDraftPayload,
    LeaseOwner,
    LeasePayload,
    RuntimeReasonCode,
    RuntimeState,
    SchedulerMode,
    TransitionPayload,
    TransitionSubject,
    decode_journal_event_bytes,
    decode_manifest_primitive,
)
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.state import ApplyReason, KernelSnapshot, apply_journal_event
from wish_builder.services.journal import (
    GENESIS_HEAD,
    AppendStatus,
    CoordinatorLeaseState,
    DurableJournal,
    JournalEventDraft,
    JournalFaultCode,
    LeaseStateCode,
    LeaseStateError,
)

RUN_ID = "RUN-LEASES"
MANIFEST_DIGEST = "sha256:" + "a" * 64
BASE_TIME = datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)


def lease_owner(coordinator_id: str) -> LeaseOwner:
    return LeaseOwner(
        actor=ActorIdentity(
            ActorType.COORDINATOR,
            coordinator_id,
            "host-test",
            100,
            f"process-start-{coordinator_id}",
        ),
        local_repository_id="sha256:" + "1" * 64,
        local_worktree_id="sha256:" + "2" * 64,
        workspace_hash="sha256:" + "3" * 64,
        control_root_id="sha256:" + "4" * 64,
    )


class Clock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if not self.values:
            raise RuntimeError("clock exhausted")
        return self.values.pop(0)


class Faults:
    def __init__(self) -> None:
        self.action: dict[str, BaseException] = {}
        self.timeline: list[str] = []

    def __call__(self, point: str, requested_bytes: int | None = None) -> None:
        self.timeline.append(point)
        action = self.action.pop(point, None)
        if action is not None:
            raise action


def run_draft(event_id: str = "EVENT-RUN-INITIALIZED") -> JournalEventDraft:
    return JournalEventDraft(
        event_id=event_id,
        event_type=JournalEventType.RUN_INITIALIZED,
        identity=ExecutionIdentity(RUN_ID, 0),
        actor_type=ActorType.SYSTEM,
        actor_id="wishctl",
        payload=TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.NONE,
            RuntimeState.PREFLIGHT,
        ),
    )


def lease_draft(
    event_type: JournalEventType,
    *,
    token: int,
    lease_id: str,
    coordinator_id: str,
) -> JournalEventDraft:
    lost = event_type is JournalEventType.LEASE_LOST
    return JournalEventDraft(
        event_id=f"EVENT-{event_type.value.upper().replace('_', '-')}-{token:03d}",
        event_type=event_type,
        identity=ExecutionIdentity(RUN_ID, token),
        actor_type=ActorType.SYSTEM if lost else ActorType.COORDINATOR,
        actor_id="recovery" if lost else coordinator_id,
        payload=LeaseDraftPayload(
            lease_id=lease_id,
            coordinator_id=coordinator_id,
            owner=lease_owner(coordinator_id),
            scheduler_mode=SchedulerMode.WISH_BUILDER,
            fencing_token=token,
            manifest_digest=MANIFEST_DIGEST,
            lease_ttl_seconds=90,
            lease_clock_skew_seconds=2,
        ),
        reason_code=RuntimeReasonCode.LEASE_LOST if lost else None,
    )


def _acquire_lease_worker(
    root: str,
    contender: int,
    barrier: object,
    queue: object,
) -> None:
    clock = Clock(BASE_TIME + timedelta(seconds=contender))
    journal = DurableJournal(
        RUN_ID,
        FilesystemJournalStorage(
            root,
            RUN_ID,
            authority_clock=clock,
            lock_timeout_seconds=10,
        ),
    )
    barrier.wait(timeout=10)  # type: ignore[attr-defined]
    result = journal.append_draft(
        lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            token=1,
            lease_id=f"LEASE-{contender:03d}",
            coordinator_id=f"coordinator-{contender}",
        ),
        expected_head=GENESIS_HEAD,
        lease_state=CoordinatorLeaseState.initial(),
    )
    queue.put(  # type: ignore[attr-defined]
        (
            result.status.value,
            None if result.head is None else result.head.sequence,
            None if result.head is None else result.head.event_hash,
            clock.calls,
        )
    )


class JournalLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def journal(self, clock: Clock, faults: Faults | None = None) -> DurableJournal:
        return DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(
                self.root,
                RUN_ID,
                authority_clock=clock,
                fault_injector=faults,
            ),
        )

    def test_storage_assigns_sequence_time_and_hash_chain_after_lock_acquisition(
        self,
    ) -> None:
        faults = Faults()

        def authority_clock() -> datetime:
            faults.timeline.append("authority_clock")
            return BASE_TIME

        journal = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(
                self.root,
                RUN_ID,
                authority_clock=authority_clock,
                fault_injector=faults,
            ),
        )
        result = journal.append_draft(run_draft(), expected_head=GENESIS_HEAD)

        self.assertEqual(AppendStatus.COMMITTED, result.status)
        assert result.event is not None
        self.assertEqual(1, result.event.sequence)
        self.assertEqual(GENESIS_HEAD.event_hash, result.event.previous_event_hash)
        self.assertEqual("2026-08-19T00:00:00.000000Z", result.event.recorded_at)
        self.assertLess(
            faults.timeline.index("lock_acquire"),
            faults.timeline.index("authority_clock"),
        )
        raw = (self.root / "segments" / "segment-00000001.jsonl").read_bytes()
        decoded = decode_journal_event_bytes(raw)
        self.assertTrue(decoded.ok, decoded.issues)
        self.assertEqual(result.event, decoded.value)

    def test_acquire_is_typed_authority_stamped_and_admits_only_the_holder(
        self,
    ) -> None:
        draft = lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            token=1,
            lease_id="LEASE-001",
            coordinator_id="coordinator-a",
        )
        initial = CoordinatorLeaseState.initial()
        result = self.journal(Clock(BASE_TIME)).append_draft(
            draft,
            expected_head=GENESIS_HEAD,
            lease_state=initial,
        )

        self.assertEqual(AppendStatus.COMMITTED, result.status)
        assert result.event is not None
        self.assertIsInstance(result.event.payload, LeasePayload)
        payload = result.event.payload
        assert isinstance(payload, LeasePayload)
        self.assertEqual("2026-08-19T00:01:30.000000Z", payload.expires_at)
        raw = (self.root / "segments" / "segment-00000001.jsonl").read_bytes()
        decoded = decode_journal_event_bytes(raw)
        self.assertTrue(decoded.ok, decoded.issues)
        self.assertEqual(result.event, decoded.value)
        state = initial.advance(result.event)
        self.assertTrue(state.active)
        self.assertTrue(
            state.allows_admission(
                authority_time=BASE_TIME + timedelta(seconds=10),
                coordinator_id="coordinator-a",
                owner=lease_owner("coordinator-a"),
                fencing_token=1,
                manifest_digest=MANIFEST_DIGEST,
            )
        )
        self.assertFalse(
            state.allows_admission(
                authority_time=BASE_TIME + timedelta(seconds=10),
                coordinator_id="coordinator-stale",
                owner=lease_owner("coordinator-stale"),
                fencing_token=1,
                manifest_digest=MANIFEST_DIGEST,
            )
        )

    def test_exact_draft_retry_returns_original_authority_event(self) -> None:
        draft = lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            token=1,
            lease_id="LEASE-001",
            coordinator_id="coordinator-a",
        )
        clock = Clock(BASE_TIME, BASE_TIME + timedelta(hours=1))
        journal = self.journal(clock)
        state = CoordinatorLeaseState.initial()
        first = journal.append_draft(
            draft,
            expected_head=GENESIS_HEAD,
            lease_state=state,
        )
        replay = journal.append_draft(
            draft,
            expected_head=GENESIS_HEAD,
            lease_state=state,
        )

        self.assertEqual(AppendStatus.IDEMPOTENT, replay.status)
        self.assertEqual(first.event, replay.event)
        self.assertEqual(1, clock.calls)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(1, len(segment.read_bytes().splitlines()))

    def test_cas_conflict_does_not_consume_authority_time(self) -> None:
        initial = CoordinatorLeaseState.initial()
        winner = self.journal(Clock(BASE_TIME)).append_draft(
            lease_draft(
                JournalEventType.LEASE_ACQUIRED,
                token=1,
                lease_id="LEASE-001",
                coordinator_id="coordinator-a",
            ),
            expected_head=GENESIS_HEAD,
            lease_state=initial,
        )
        loser_clock = Clock(BASE_TIME + timedelta(seconds=1))
        loser = self.journal(loser_clock).append_draft(
            lease_draft(
                JournalEventType.LEASE_ACQUIRED,
                token=1,
                lease_id="LEASE-002",
                coordinator_id="coordinator-b",
            ),
            expected_head=GENESIS_HEAD,
            lease_state=initial,
        )

        self.assertEqual(AppendStatus.COMMITTED, winner.status)
        self.assertEqual(AppendStatus.CONFLICT, loser.status)
        self.assertEqual(0, loser_clock.calls)

    def test_two_processes_racing_to_acquire_commit_exactly_one_lease(self) -> None:
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        queue = context.Queue()
        processes = [
            context.Process(
                target=_acquire_lease_worker,
                args=(str(self.root), contender, barrier, queue),
            )
            for contender in (1, 2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            self.assertFalse(process.is_alive())
            self.assertEqual(0, process.exitcode)
        results = [queue.get(timeout=2), queue.get(timeout=2)]

        self.assertEqual(
            [AppendStatus.COMMITTED.value, AppendStatus.CONFLICT.value],
            sorted(result[0] for result in results),
        )
        self.assertEqual({1}, {result[1] for result in results})
        self.assertEqual(1, len({result[2] for result in results}))
        self.assertEqual([0, 1], sorted(result[3] for result in results))
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(1, len(segment.read_bytes().splitlines()))

    def test_renewal_extends_expiry_and_late_renewal_is_not_persisted(self) -> None:
        state = CoordinatorLeaseState.initial()
        acquired = self.journal(Clock(BASE_TIME)).append_draft(
            lease_draft(
                JournalEventType.LEASE_ACQUIRED,
                token=1,
                lease_id="LEASE-001",
                coordinator_id="coordinator-a",
            ),
            expected_head=GENESIS_HEAD,
            lease_state=state,
        )
        assert acquired.event is not None and acquired.head is not None
        state = state.advance(acquired.event)
        renewed = self.journal(Clock(BASE_TIME + timedelta(seconds=20))).append_draft(
            lease_draft(
                JournalEventType.LEASE_RENEWED,
                token=1,
                lease_id="LEASE-001",
                coordinator_id="coordinator-a",
            ),
            expected_head=acquired.head,
            lease_state=state,
        )
        assert renewed.event is not None and renewed.head is not None
        state = state.advance(renewed.event)
        self.assertEqual("2026-08-19T00:01:50.000000Z", state.lease.expires_at)  # type: ignore[union-attr]

        with self.assertRaises(LeaseStateError) as context:
            self.journal(Clock(BASE_TIME + timedelta(seconds=109))).append_draft(
                lease_draft(
                    JournalEventType.LEASE_RENEWED,
                    token=1,
                    lease_id="LEASE-001",
                    coordinator_id="coordinator-a",
                ),
                expected_head=renewed.head,
                lease_state=state,
            )
        self.assertEqual(LeaseStateCode.RENEWAL_TOO_LATE, context.exception.code)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(2, len(segment.read_bytes().splitlines()))

    def test_expiry_takeover_requires_skew_and_advances_fencing(self) -> None:
        state = CoordinatorLeaseState.initial()
        acquired = self.journal(Clock(BASE_TIME)).append_draft(
            lease_draft(
                JournalEventType.LEASE_ACQUIRED,
                token=1,
                lease_id="LEASE-001",
                coordinator_id="coordinator-a",
            ),
            expected_head=GENESIS_HEAD,
            lease_state=state,
        )
        assert acquired.event is not None and acquired.head is not None
        state = state.advance(acquired.event)
        contender = lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            token=2,
            lease_id="LEASE-002",
            coordinator_id="coordinator-b",
        )

        with self.assertRaises(LeaseStateError) as context:
            self.journal(Clock(BASE_TIME + timedelta(seconds=92))).append_draft(
                contender,
                expected_head=acquired.head,
                lease_state=state,
            )
        self.assertEqual(LeaseStateCode.LIVE_LEASE_CONFLICT, context.exception.code)

        takeover = self.journal(Clock(BASE_TIME + timedelta(seconds=93))).append_draft(
            contender,
            expected_head=acquired.head,
            lease_state=state,
        )
        assert takeover.event is not None
        state = state.advance(takeover.event)
        self.assertEqual(2, state.max_fencing_token)
        self.assertFalse(
            state.allows_admission(
                authority_time=BASE_TIME + timedelta(seconds=94),
                coordinator_id="coordinator-a",
                owner=lease_owner("coordinator-a"),
                fencing_token=1,
                manifest_digest=MANIFEST_DIGEST,
            )
        )
        self.assertTrue(
            state.allows_admission(
                authority_time=BASE_TIME + timedelta(seconds=94),
                coordinator_id="coordinator-b",
                owner=lease_owner("coordinator-b"),
                fencing_token=2,
                manifest_digest=MANIFEST_DIGEST,
            )
        )

    def test_kernel_replay_advances_epoch_and_rejects_stale_fencing(self) -> None:
        manifest_result = decode_manifest_primitive(valid_manifest())
        self.assertTrue(manifest_result.ok, manifest_result.issues)
        assert manifest_result.value is not None
        snapshot = KernelSnapshot.initial(
            RUN_ID,
            1,
            TaskDag.compile(manifest_result.value),
        )
        lease_state = CoordinatorLeaseState.initial()
        acquired = self.journal(Clock(BASE_TIME)).append_draft(
            lease_draft(
                JournalEventType.LEASE_ACQUIRED,
                token=1,
                lease_id="LEASE-001",
                coordinator_id="coordinator-a",
            ),
            expected_head=GENESIS_HEAD,
            lease_state=lease_state,
        )
        assert acquired.event is not None and acquired.head is not None
        first_apply = apply_journal_event(snapshot, acquired.event)
        self.assertTrue(first_apply.accepted)
        lease_state = lease_state.advance(acquired.event)
        takeover = self.journal(Clock(BASE_TIME + timedelta(seconds=93))).append_draft(
            lease_draft(
                JournalEventType.LEASE_ACQUIRED,
                token=2,
                lease_id="LEASE-002",
                coordinator_id="coordinator-b",
            ),
            expected_head=acquired.head,
            lease_state=lease_state,
        )
        assert takeover.event is not None
        second_apply = apply_journal_event(first_apply.snapshot, takeover.event)
        self.assertTrue(second_apply.accepted)
        self.assertEqual(2, second_apply.snapshot.coordinator_epoch)

        stale = run_draft("EVENT-STALE-COORDINATOR").materialize(
            sequence=takeover.event.sequence + 1,
            previous_event_hash=takeover.event.event_hash,
            authority_time=BASE_TIME + timedelta(seconds=94),
        )
        stale_apply = apply_journal_event(second_apply.snapshot, stale)
        self.assertFalse(stale_apply.accepted)
        self.assertEqual(ApplyReason.STALE_EPOCH, stale_apply.reason)

    def test_release_and_loss_are_typed_terminal_events(self) -> None:
        for event_type in (
            JournalEventType.LEASE_RELEASED,
            JournalEventType.LEASE_LOST,
        ):
            with self.subTest(event_type=event_type):
                root = self.root / event_type.value
                initial = CoordinatorLeaseState.initial()
                acquired = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(
                        root, RUN_ID, authority_clock=Clock(BASE_TIME)
                    ),
                ).append_draft(
                    lease_draft(
                        JournalEventType.LEASE_ACQUIRED,
                        token=1,
                        lease_id="LEASE-001",
                        coordinator_id="coordinator-a",
                    ),
                    expected_head=GENESIS_HEAD,
                    lease_state=initial,
                )
                assert acquired.event is not None and acquired.head is not None
                state = initial.advance(acquired.event)
                terminal = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(
                        root,
                        RUN_ID,
                        authority_clock=Clock(BASE_TIME + timedelta(seconds=10)),
                    ),
                ).append_draft(
                    lease_draft(
                        event_type,
                        token=1,
                        lease_id="LEASE-001",
                        coordinator_id="coordinator-a",
                    ),
                    expected_head=acquired.head,
                    lease_state=state,
                )
                assert terminal.event is not None
                payload = terminal.event.payload
                assert isinstance(payload, LeasePayload)
                self.assertEqual(payload.committed_at, payload.expires_at)
                self.assertFalse(state.advance(terminal.event).active)

    def test_clock_rollback_after_restart_blocks_without_appending(self) -> None:
        first = self.journal(Clock(BASE_TIME)).append_draft(
            run_draft(), expected_head=GENESIS_HEAD
        )
        assert first.head is not None
        restarted = self.journal(Clock(BASE_TIME - timedelta(seconds=1)))
        failed = restarted.append_draft(
            run_draft("EVENT-RUN-SECOND"),
            expected_head=first.head,
        )

        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, failed.status)
        self.assertEqual(JournalFaultCode.CLOCK_ROLLBACK, failed.fault_code)
        self.assertTrue(restarted.blocked)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(1, len(segment.read_bytes().splitlines()))

    def test_persistence_failure_never_returns_or_advances_a_lease(self) -> None:
        faults = Faults()
        faults.action["segment_fsync"] = OSError(errno.EIO, "injected")
        journal = self.journal(Clock(BASE_TIME), faults)
        result = journal.append_draft(
            lease_draft(
                JournalEventType.LEASE_ACQUIRED,
                token=1,
                lease_id="LEASE-001",
                coordinator_id="coordinator-a",
            ),
            expected_head=GENESIS_HEAD,
            lease_state=CoordinatorLeaseState.initial(),
        )

        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
        self.assertEqual(JournalFaultCode.FSYNC_FAILED, result.fault_code)
        self.assertIsNone(result.event)
        self.assertTrue(journal.blocked)


if __name__ == "__main__":
    unittest.main()
