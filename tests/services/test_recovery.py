from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.kernel.test_validation import valid_manifest
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    AdapterKind,
    ActorIdentity,
    ActorType,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    ExecutionIdentity,
    JournalEventType,
    LeaseDraftPayload,
    LeaseOwner,
    RuntimeReasonCode,
    SchedulerMode,
    decode_manifest_primitive,
)
from wish_builder.services.journal import (
    GENESIS_HEAD,
    AppendResult,
    AppendStatus,
    DurableJournal,
    JournalEventDraft,
    JournalHead,
    LeaseStateCode,
)
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    LeaseAction,
    LeaseMutationStatus,
    LeaseRecoveryFaultCode,
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)

RUN_ID = "RUN-RECOVERY"
MANIFEST_DIGEST = "sha256:" + "a" * 64
BASE_TIME = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        if not self.values:
            raise RuntimeError("clock exhausted")
        return self.values.pop(0)


def manifest():
    primitive = valid_manifest()
    primitive["run_id"] = RUN_ID
    decoded = decode_manifest_primitive(primitive)
    if not decoded.ok or decoded.value is None:
        raise AssertionError(decoded.report.render_text())
    return decoded.value


def owner(
    coordinator_id: str,
    *,
    process_id: int = 100,
    process_start_id: str | None = None,
    workspace: str = "3",
) -> LeaseOwner:
    return LeaseOwner(
        ActorIdentity(
            ActorType.COORDINATOR,
            coordinator_id,
            "host-test",
            process_id,
            process_start_id or f"process-start-{coordinator_id}",
        ),
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + workspace * 64,
        "sha256:" + "4" * 64,
    )


def lease_draft(
    event_type: JournalEventType,
    *,
    event_id: str,
    lease_id: str,
    lease_owner: LeaseOwner,
    token: int,
) -> JournalEventDraft:
    lost = event_type is JournalEventType.LEASE_LOST
    return JournalEventDraft(
        event_id,
        event_type,
        ExecutionIdentity(RUN_ID, token),
        ActorType.SYSTEM if lost else ActorType.COORDINATOR,
        "recovery" if lost else lease_owner.actor.actor_id,
        LeaseDraftPayload(
            lease_id,
            lease_owner.actor.actor_id,
            lease_owner,
            SchedulerMode.WISH_BUILDER,
            token,
            MANIFEST_DIGEST,
            90,
            2,
        ),
        RuntimeReasonCode.LEASE_LOST if lost else None,
    )


def recovery(root: str | Path):
    return recover_coordinator_lease(
        root,
        manifest(),
        coordinator_epoch=1,
        repair_derived=False,
    )


def service(
    root: str | Path,
    lease_owner: LeaseOwner,
    clock: Clock,
    *,
    max_conflict_retries: int = 4,
) -> CoordinatorLeaseService:
    return CoordinatorLeaseService(
        DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(
                root,
                RUN_ID,
                authority_clock=clock,
                lock_timeout_seconds=10,
            ),
        ),
        lambda: recovery(root),
        run_id=RUN_ID,
        owner=lease_owner,
        manifest_digest=MANIFEST_DIGEST,
        lease_ttl_seconds=90,
        lease_clock_skew_seconds=2,
        max_conflict_retries=max_conflict_retries,
    )


def _acquire_worker(root: str, contender: int, barrier: object, queue: object) -> None:
    contender_owner = owner(f"coordinator-{contender}")
    contender_service = service(
        root,
        contender_owner,
        Clock(BASE_TIME),
    )
    barrier.wait(timeout=10)  # type: ignore[attr-defined]
    result = contender_service.acquire(
        event_id=f"EVENT-ACQUIRE-{contender}",
        lease_id=f"LEASE-{contender}",
    )
    queue.put(  # type: ignore[attr-defined]
        (
            result.status.value,
            None if result.lease_state_code is None else result.lease_state_code.value,
            None if result.lease_state is None else result.lease_state.head.sequence,
        )
    )


class AlwaysConflictJournal:
    def __init__(self) -> None:
        self.calls = 0

    def append_draft(
        self,
        draft: JournalEventDraft,
        *,
        expected_head: JournalHead,
        lease_state=None,
    ) -> AppendResult:
        del draft, lease_state
        self.calls += 1
        return AppendResult(
            AppendStatus.CONFLICT,
            JournalHead(expected_head.sequence + 1, "sha256:" + "f" * 64),
        )


class RecoveryCounter:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.result


class CoordinatorLeaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def segment_lines(self) -> list[bytes]:
        segment = self.root / "segments" / "segment-00000001.jsonl"
        return [] if not segment.exists() else segment.read_bytes().splitlines()

    def test_empty_verified_journal_recovers_initial_lease_state(self) -> None:
        result = recovery(self.root)

        self.assertEqual(LeaseRecoveryStatus.RECOVERED, result.status)
        self.assertIsNone(result.fault)
        self.assertIsNone(result.last_lease_event)
        assert result.lease_state is not None
        self.assertEqual(GENESIS_HEAD, result.lease_state.head)
        self.assertFalse(result.lease_state.active)
        self.assertEqual(0, result.lease_state.max_fencing_token)

    def test_recovery_exposes_pending_backend_effects(self) -> None:
        journal = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(self.root, RUN_ID),
        )
        appended = journal.append_draft(
            JournalEventDraft(
                "EVENT-BACKEND-EFFECT-REQUESTED",
                JournalEventType.EFFECT_REQUESTED,
                ExecutionIdentity(RUN_ID, 1, "TASK-001", 1, "RESERVE-CHANNEL-001"),
                ActorType.COORDINATOR,
                "coordinator-a",
                EffectRequestPayload(
                    EffectOperation.RESERVE_CHANNEL,
                    AdapterKind.BACKEND,
                    EffectObjectType.CHANNEL,
                    "sha256:" + "5" * 64,
                    "sha256:" + "6" * 64,
                    0,
                    1,
                ),
            ),
            expected_head=GENESIS_HEAD,
        )
        assert appended.event is not None

        result = recovery(self.root)

        self.assertEqual(LeaseRecoveryStatus.RECOVERED, result.status)
        self.assertEqual(1, len(result.pending_external_effects))
        self.assertEqual(
            appended.event,
            result.pending_external_effects[0].request_event,
        )

    def test_streaming_recovery_projects_acquire_renew_and_release(self) -> None:
        holder = owner("coordinator-a")
        controller = service(
            self.root,
            holder,
            Clock(
                BASE_TIME,
                BASE_TIME + timedelta(seconds=20),
                BASE_TIME + timedelta(seconds=30),
            ),
        )

        acquired = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        renewed = controller.renew(event_id="EVENT-RENEW-A")
        released = controller.release(event_id="EVENT-RELEASE-A")
        recovered = recovery(self.root)

        self.assertEqual(LeaseMutationStatus.COMMITTED, acquired.status)
        self.assertEqual(LeaseMutationStatus.COMMITTED, renewed.status)
        self.assertEqual(LeaseMutationStatus.COMMITTED, released.status)
        self.assertEqual(LeaseRecoveryStatus.RECOVERED, recovered.status)
        assert recovered.lease_state is not None
        self.assertFalse(recovered.lease_state.active)
        self.assertEqual(1, recovered.lease_state.max_fencing_token)
        self.assertEqual(3, recovered.lease_state.head.sequence)
        assert recovered.last_lease_event is not None
        self.assertEqual(
            JournalEventType.LEASE_RELEASED,
            recovered.last_lease_event.event_type,
        )

    def test_exact_retry_is_reconciled_from_durable_last_lease_event(self) -> None:
        clock = Clock(BASE_TIME, BASE_TIME + timedelta(seconds=10))
        controller = service(self.root, owner("coordinator-a"), clock)
        first = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        retry = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        release = controller.release(event_id="EVENT-RELEASE-A")
        release_retry = controller.release(event_id="EVENT-RELEASE-A")

        self.assertEqual(LeaseMutationStatus.COMMITTED, first.status)
        self.assertEqual(LeaseMutationStatus.IDEMPOTENT, retry.status)
        self.assertEqual(LeaseMutationStatus.COMMITTED, release.status)
        self.assertEqual(LeaseMutationStatus.IDEMPOTENT, release_retry.status)
        self.assertEqual(2, clock.calls)
        self.assertEqual(2, len(self.segment_lines()))

    def test_different_acquire_for_live_exact_holder_uses_authority_clock(self) -> None:
        clock = Clock(BASE_TIME, BASE_TIME + timedelta(seconds=1))
        controller = service(self.root, owner("coordinator-a"), clock)
        controller.acquire(event_id="EVENT-ACQUIRE-A", lease_id="LEASE-A")

        rejected = controller.acquire(
            event_id="EVENT-ACQUIRE-A-AGAIN",
            lease_id="LEASE-A-AGAIN",
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, rejected.status)
        self.assertEqual(
            LeaseStateCode.LIVE_LEASE_CONFLICT,
            rejected.lease_state_code,
        )
        self.assertFalse(rejected.succeeded)
        self.assertEqual(2, clock.calls)
        self.assertEqual(1, len(self.segment_lines()))

    def test_two_process_contenders_commit_one_lease_and_loser_rereads(self) -> None:
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        queue = context.Queue()
        processes = [
            context.Process(
                target=_acquire_worker,
                args=(str(self.root), contender, barrier, queue),
            )
            for contender in (1, 2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            self.assertFalse(process.is_alive())
            self.assertEqual(0, process.exitcode)
        results = [queue.get(timeout=2), queue.get(timeout=2)]

        self.assertEqual(
            [LeaseMutationStatus.COMMITTED.value, LeaseMutationStatus.REJECTED.value],
            sorted(item[0] for item in results),
        )
        self.assertEqual(
            [None, LeaseStateCode.LIVE_LEASE_CONFLICT.value],
            sorted((item[1] for item in results), key=lambda value: value or ""),
        )
        self.assertEqual({1}, {item[2] for item in results})
        self.assertEqual(1, len(self.segment_lines()))

    def test_repeated_cas_conflicts_reread_then_fail_closed_at_bound(self) -> None:
        recovered = recovery(self.root)
        recover_counter = RecoveryCounter(recovered)
        journal = AlwaysConflictJournal()
        controller = CoordinatorLeaseService(
            journal,
            recover_counter,
            run_id=RUN_ID,
            owner=owner("coordinator-a"),
            manifest_digest=MANIFEST_DIGEST,
            lease_ttl_seconds=90,
            lease_clock_skew_seconds=2,
            max_conflict_retries=2,
        )

        result = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )

        self.assertEqual(LeaseMutationStatus.BLOCKED, result.status)
        self.assertIn("retry limit", result.detail or "")
        self.assertEqual(3, journal.calls)
        self.assertEqual(3, recover_counter.calls)
        self.assertEqual([], self.segment_lines())

    def test_expired_lease_takeover_advances_epoch(self) -> None:
        first = service(self.root, owner("coordinator-a"), Clock(BASE_TIME))
        first.acquire(event_id="EVENT-ACQUIRE-A", lease_id="LEASE-A")
        contender = service(
            self.root,
            owner("coordinator-b"),
            Clock(BASE_TIME + timedelta(seconds=93)),
        )

        takeover = contender.acquire(
            event_id="EVENT-ACQUIRE-B",
            lease_id="LEASE-B",
        )

        self.assertEqual(LeaseMutationStatus.COMMITTED, takeover.status)
        assert (
            takeover.lease_state is not None and takeover.lease_state.lease is not None
        )
        self.assertEqual(2, takeover.lease_state.max_fencing_token)
        self.assertEqual("coordinator-b", takeover.lease_state.lease.coordinator_id)

    def test_expired_exact_owner_reacquires_instead_of_claiming_live_admission(
        self,
    ) -> None:
        controller = service(
            self.root,
            owner("coordinator-a"),
            Clock(BASE_TIME, BASE_TIME + timedelta(seconds=93)),
        )
        controller.acquire(event_id="EVENT-ACQUIRE-A", lease_id="LEASE-A")

        reacquired = controller.acquire(
            event_id="EVENT-REACQUIRE-A",
            lease_id="LEASE-A-SECOND",
        )

        self.assertEqual(LeaseMutationStatus.COMMITTED, reacquired.status)
        assert reacquired.lease_state is not None
        self.assertEqual(2, reacquired.lease_state.max_fencing_token)
        self.assertEqual(2, len(self.segment_lines()))

    def test_live_contender_is_rejected_without_appending(self) -> None:
        service(self.root, owner("coordinator-a"), Clock(BASE_TIME)).acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        contender_clock = Clock(BASE_TIME + timedelta(seconds=92))
        contender = service(self.root, owner("coordinator-b"), contender_clock)

        denied = contender.acquire(
            event_id="EVENT-ACQUIRE-B",
            lease_id="LEASE-B",
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, denied.status)
        self.assertEqual(LeaseStateCode.LIVE_LEASE_CONFLICT, denied.lease_state_code)
        self.assertEqual(1, contender_clock.calls)
        self.assertEqual(1, len(self.segment_lines()))

    def test_pid_reuse_and_workspace_mismatch_cannot_renew_or_release(self) -> None:
        original = owner(
            "coordinator-a",
            process_id=4321,
            process_start_id="process-start-original",
        )
        service(self.root, original, Clock(BASE_TIME)).acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        reused_pid = owner(
            "coordinator-a",
            process_id=4321,
            process_start_id="process-start-reused",
        )
        reused_clock = Clock(BASE_TIME + timedelta(seconds=10))
        reused = service(self.root, reused_pid, reused_clock)

        renew = reused.renew(event_id="EVENT-RENEW-REUSED")
        release = reused.release(event_id="EVENT-RELEASE-REUSED")

        self.assertEqual(LeaseStateCode.LEASE_IDENTITY_MISMATCH, renew.lease_state_code)
        self.assertEqual(
            LeaseStateCode.LEASE_IDENTITY_MISMATCH, release.lease_state_code
        )
        self.assertEqual(0, reused_clock.calls)
        self.assertEqual(1, len(self.segment_lines()))

        wrong_workspace = service(
            self.root,
            owner(
                "coordinator-a",
                process_id=4321,
                process_start_id="process-start-original",
                workspace="9",
            ),
            Clock(BASE_TIME + timedelta(seconds=11)),
        ).renew(event_id="EVENT-RENEW-WRONG-WORKSPACE")
        self.assertEqual(
            LeaseStateCode.LEASE_IDENTITY_MISMATCH,
            wrong_workspace.lease_state_code,
        )
        self.assertEqual(1, len(self.segment_lines()))

    def test_lost_requires_the_exact_recovered_lease_and_is_idempotent(self) -> None:
        clock = Clock(BASE_TIME, BASE_TIME + timedelta(seconds=10))
        controller = service(self.root, owner("coordinator-a"), clock)
        acquired = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        assert (
            acquired.lease_state is not None and acquired.lease_state.lease is not None
        )
        expected = acquired.lease_state.lease

        lost = controller.lost(event_id="EVENT-LOST-A", expected_lease=expected)
        retry = controller.lost(event_id="EVENT-LOST-A", expected_lease=expected)

        self.assertEqual(LeaseAction.LOST, lost.action)
        self.assertEqual(LeaseMutationStatus.COMMITTED, lost.status)
        self.assertEqual(LeaseMutationStatus.IDEMPOTENT, retry.status)
        assert lost.lease_state is not None
        self.assertFalse(lost.lease_state.active)
        assert lost.append_result is not None and lost.append_result.event is not None
        self.assertEqual(ActorType.SYSTEM, lost.append_result.event.actor_type)
        self.assertEqual(
            RuntimeReasonCode.LEASE_LOST, lost.append_result.event.reason_code
        )
        self.assertEqual(2, len(self.segment_lines()))

    def test_lost_rejects_stale_lease_observation(self) -> None:
        controller = service(
            self.root,
            owner("coordinator-a"),
            Clock(BASE_TIME, BASE_TIME + timedelta(seconds=20)),
        )
        acquired = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        assert (
            acquired.lease_state is not None and acquired.lease_state.lease is not None
        )
        stale = acquired.lease_state.lease
        controller.renew(event_id="EVENT-RENEW-A")

        rejected = controller.lost(
            event_id="EVENT-LOST-STALE",
            expected_lease=stale,
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, rejected.status)
        self.assertEqual(
            LeaseStateCode.LEASE_IDENTITY_MISMATCH,
            rejected.lease_state_code,
        )
        self.assertEqual(2, len(self.segment_lines()))

    def test_corrupt_verified_replay_blocks_acquisition_without_writing(self) -> None:
        segments = self.root / "segments"
        segments.mkdir(parents=True)
        segment = segments / "segment-00000001.jsonl"
        segment.write_bytes(b"{}\n")
        clock = Clock(BASE_TIME)
        controller = service(self.root, owner("coordinator-a"), clock)

        result = controller.acquire(
            event_id="EVENT-MUST-NOT-APPEND",
            lease_id="LEASE-MUST-NOT-APPEND",
        )

        self.assertEqual(LeaseMutationStatus.BLOCKED, result.status)
        self.assertEqual(LeaseRecoveryStatus.BLOCKED, result.recovery.status)
        assert result.recovery.fault is not None
        self.assertEqual(
            LeaseRecoveryFaultCode.REPLAY_BLOCKED,
            result.recovery.fault.code,
        )
        self.assertEqual(0, clock.calls)
        self.assertEqual(b"{}\n", segment.read_bytes())

    def test_schema_valid_but_invalid_lease_history_blocks_recovery(self) -> None:
        first_draft = lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
            lease_owner=owner("coordinator-a"),
            token=1,
        )
        first = first_draft.materialize(
            sequence=1,
            previous_event_hash=GENESIS_HEAD.event_hash,
            authority_time=BASE_TIME,
        )
        second_draft = lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            event_id="EVENT-ACQUIRE-BAD",
            lease_id="LEASE-BAD",
            lease_owner=owner("coordinator-b"),
            token=1,
        )
        second = second_draft.materialize(
            sequence=2,
            previous_event_hash=first.event_hash,
            authority_time=BASE_TIME + timedelta(seconds=93),
        )
        segments = self.root / "segments"
        segments.mkdir(parents=True)
        (segments / "segment-00000001.jsonl").write_bytes(
            first.canonical_json_bytes() + second.canonical_json_bytes()
        )

        result = recovery(self.root)

        self.assertEqual(LeaseRecoveryStatus.BLOCKED, result.status)
        self.assertIsNone(result.lease_state)
        assert result.fault is not None
        self.assertEqual(LeaseRecoveryFaultCode.LEASE_STATE_INVALID, result.fault.code)
        self.assertIn(
            LeaseStateCode.FENCING_TOKEN_NOT_ADVANCED.value,
            result.fault.detail,
        )

    def test_manifest_or_owner_mismatch_is_not_silently_admitted(self) -> None:
        holder = owner("coordinator-a")
        controller = service(self.root, holder, Clock(BASE_TIME))
        acquired = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        assert (
            acquired.lease_state is not None and acquired.lease_state.lease is not None
        )
        unexpected = replace(
            acquired.lease_state.lease,
            manifest_digest="sha256:" + "b" * 64,
        )

        rejected = controller.lost(
            event_id="EVENT-LOST-WRONG-MANIFEST",
            expected_lease=unexpected,
        )

        self.assertEqual(LeaseMutationStatus.REJECTED, rejected.status)
        self.assertEqual(
            LeaseStateCode.LEASE_IDENTITY_MISMATCH,
            rejected.lease_state_code,
        )
        self.assertEqual(1, len(self.segment_lines()))


if __name__ == "__main__":
    unittest.main()
