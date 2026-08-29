from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.services.test_dispatch_recovery_projection import proof_event
from tests.services.test_recovery import (
    BASE_TIME,
    MANIFEST_DIGEST,
    RUN_ID,
    AlwaysConflictJournal,
    Clock,
    manifest,
    owner,
    recovery,
    service,
)
from wish_builder.contracts import (
    AdapterKind,
    ActorType,
    DispatchRecoveryPayload,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.services import recovery as recovery_module
from wish_builder.services.dispatch_recovery import (
    DispatchRecoveryProjectionError,
    DispatchRecoveryRecord,
    PendingExternalEffect,
)
from wish_builder.services.journal import (
    GENESIS_HEAD,
    AppendResult,
    AppendStatus,
    JournalHead,
    LeaseStateCode,
)
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    LeaseAction,
    LeaseRecoveryFault,
    LeaseRecoveryFaultCode,
    LeaseRecoveryResult,
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)
from wish_builder.services.replay import (
    ReplayFault,
    ReplayFaultCode,
    ReplayStatus,
)


def _run_event(*, run_id: str = RUN_ID) -> JournalEvent:
    return JournalEvent.create(
        sequence=1,
        event_id="EVENT-RUN-INITIALIZED",
        event_type=JournalEventType.RUN_INITIALIZED,
        identity=ExecutionIdentity(run_id, 1),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-a",
        recorded_at="2026-08-19T00:00:00Z",
        previous_event_hash=GENESIS_HEAD.event_hash,
        payload=TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.NONE,
            RuntimeState.PREFLIGHT,
        ),
    )


def _dispatch_request(sequence: int) -> JournalEvent:
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-DISPATCH-{sequence:04d}",
        event_type=JournalEventType.DISPATCH_REQUESTED,
        identity=ExecutionIdentity(
            RUN_ID,
            1,
            f"TASK-{sequence:03d}",
            1,
            f"DISPATCH-{sequence:03d}",
        ),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-a",
        recorded_at="2026-08-19T00:00:00Z",
        previous_event_hash=GENESIS_HEAD.event_hash,
        payload=EffectRequestPayload(
            EffectOperation.WORKER_DISPATCH,
            AdapterKind.TASK,
            EffectObjectType.WORKER,
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            sequence - 1,
            1,
        ),
    )


def _pending_external_effect() -> PendingExternalEffect:
    event = JournalEvent.create(
        sequence=1,
        event_id="EVENT-BACKEND-REQUEST",
        event_type=JournalEventType.EFFECT_REQUESTED,
        identity=ExecutionIdentity(
            RUN_ID,
            1,
            "TASK-001",
            1,
            "RESERVE-CHANNEL-001",
        ),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-a",
        recorded_at="2026-08-19T00:00:00Z",
        previous_event_hash=GENESIS_HEAD.event_hash,
        payload=EffectRequestPayload(
            EffectOperation.RESERVE_CHANNEL,
            AdapterKind.BACKEND,
            EffectObjectType.CHANNEL,
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
            0,
            1,
        ),
    )
    return PendingExternalEffect(event)


def _blocked_fault() -> LeaseRecoveryFault:
    return LeaseRecoveryFault(LeaseRecoveryFaultCode.REPLAY_BLOCKED, "blocked")


def _lease_service(
    recovered: LeaseRecoveryResult,
    journal: object,
    *,
    coordinator_id: str = "coordinator-a",
    max_conflict_retries: int = 1,
    probe: object | None = None,
) -> CoordinatorLeaseService:
    return CoordinatorLeaseService(
        journal,  # type: ignore[arg-type]
        lambda: recovered,
        run_id=RUN_ID,
        owner=owner(coordinator_id),
        manifest_digest=MANIFEST_DIGEST,
        lease_ttl_seconds=90,
        max_conflict_retries=max_conflict_retries,
        prior_owner_process_probe=probe,  # type: ignore[arg-type]
    )


class _ResultJournal:
    def __init__(self, result: AppendResult) -> None:
        self.result = result

    def append_draft(self, draft, *, expected_head, lease_state=None) -> AppendResult:
        del draft, expected_head, lease_state
        return self.result


class RecoveryResultContractBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"
        self.empty = recovery(self.root)
        controller = service(
            self.root,
            owner("coordinator-a"),
            Clock(BASE_TIME),
        )
        self.acquired = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        self.active = recovery(self.root)
        assert self.active.lease_state is not None
        assert self.active.last_lease_event is not None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_recovered_result_requires_matching_replay_state_and_evidence(self) -> None:
        replay_fault = ReplayFault(
            ReplayFaultCode.STATE_REJECTED,
            "blocked",
            None,
            None,
            GENESIS_HEAD.event_hash,
        )
        blocked_replay = replace(
            self.empty.replay,
            status=ReplayStatus.BLOCKED,
            fault=replay_fault,
        )
        mismatched_state = replace(
            self.active.lease_state,
            event_type=JournalEventType.LEASE_RENEWED,
        )

        invalid = (
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                blocked_replay,
                self.empty.lease_state,
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                self.empty.replay,
                self.empty.lease_state,
                fault=_blocked_fault(),
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                self.active.replay,
                self.active.lease_state,
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                self.empty.replay,
                self.empty.lease_state,
                last_lease_event=object(),  # type: ignore[arg-type]
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.BLOCKED,
                self.active.replay,
                last_lease_event=self.active.last_lease_event,
                fault=_blocked_fault(),
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                self.active.replay,
                mismatched_state,
                self.active.last_lease_event,
            ),
        )
        for build in invalid:
            with self.subTest(build=build), self.assertRaises((TypeError, ValueError)):
                build()

    def test_pending_collections_are_ordered_typed_and_recovered_only(self) -> None:
        first = _dispatch_request(1)
        second = _dispatch_request(2)
        proof = proof_event()
        assert type(proof.payload) is DispatchRecoveryPayload
        record = DispatchRecoveryRecord(proof.payload.recovery_id, proof)
        pending_effect = _pending_external_effect()
        invalid = (
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.BLOCKED,
                self.empty.replay,
                pending_dispatch_requests=(first,),
                fault=_blocked_fault(),
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                self.empty.replay,
                self.empty.lease_state,
                pending_dispatch_requests=(second, first),
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                self.empty.replay,
                self.empty.lease_state,
                dispatch_recoveries=(object(),),  # type: ignore[arg-type]
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.BLOCKED,
                self.empty.replay,
                dispatch_recoveries=(record,),
                fault=_blocked_fault(),
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                self.empty.replay,
                self.empty.lease_state,
                pending_external_effects=(object(),),  # type: ignore[arg-type]
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.BLOCKED,
                self.empty.replay,
                pending_external_effects=(pending_effect,),
                fault=_blocked_fault(),
            ),
        )
        for build in invalid:
            with self.subTest(build=build), self.assertRaises((TypeError, ValueError)):
                build()


class RecoveryProjectionRaceBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"
        self.segment = self.root / "segments" / "segment-00000001.jsonl"
        self.segment.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _project(self, head: JournalHead = GENESIS_HEAD, validator=None):
        return recovery_module._stream_lease_projection(
            self.root,
            RUN_ID,
            head,
            validator,
        )

    def test_control_root_is_checked_before_between_and_after_segments(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "control_root_drift"):
            self._project(validator=lambda: False)

        values = iter((True, False))
        with (
            mock.patch.object(
                recovery_module.replay_module,
                "_segment_paths",
                return_value=((1, self.segment),),
            ),
            self.assertRaisesRegex(RuntimeError, "control_root_drift"),
        ):
            self._project(validator=lambda: next(values))

        values = iter((True, False))
        with self.assertRaisesRegex(RuntimeError, "control_root_drift"):
            self._project(validator=lambda: next(values))

    def test_segment_must_remain_a_single_regular_file(self) -> None:
        self.segment.write_bytes(b"")
        with (
            mock.patch.object(
                recovery_module.replay_module,
                "_segment_paths",
                return_value=((1, self.segment),),
            ),
            mock.patch.object(
                recovery_module.replay_module,
                "_is_link_or_junction",
                return_value=True,
            ),
            self.assertRaisesRegex(RuntimeError, "protected regular file"),
        ):
            self._project()

        before = os.lstat(self.segment)
        replaced = SimpleNamespace(
            st_dev=before.st_dev,
            st_ino=before.st_ino + 1,
            st_size=before.st_size,
        )
        with (
            mock.patch.object(recovery_module.os, "fstat", return_value=replaced),
            self.assertRaisesRegex(RuntimeError, "identity changed"),
        ):
            self._project()

    def test_frames_are_bounded_complete_canonical_and_for_the_expected_run(self) -> None:
        cases = (
            (
                b"x" * (recovery_module.DEFAULT_DECODE_LIMITS.max_bytes + 1) + b"\n",
                "decoder limit",
            ),
            (b"{}", "frame changed"),
            (b"{}\n", "event changed"),
            (_run_event(run_id="RUN-OTHER").canonical_json_bytes(), "run changed"),
        )
        for raw, message in cases:
            with self.subTest(message=message):
                self.segment.write_bytes(raw)
                with self.assertRaisesRegex(RuntimeError, message):
                    self._project()

    def test_segment_size_and_verified_head_are_rechecked(self) -> None:
        event = _run_event()
        self.segment.write_bytes(event.canonical_json_bytes())
        opened = os.stat(self.segment)
        changed = SimpleNamespace(
            st_dev=opened.st_dev,
            st_ino=opened.st_ino,
            st_size=opened.st_size + 1,
        )
        with (
            mock.patch.object(
                recovery_module.os,
                "fstat",
                side_effect=(opened, changed),
            ),
            self.assertRaisesRegex(RuntimeError, "segment changed"),
        ):
            self._project(JournalHead(1, event.event_hash))

        self.segment.unlink()
        with self.assertRaisesRegex(RuntimeError, "head differs"):
            self._project(JournalHead(1, "sha256:" + "f" * 64))

    def test_layout_and_trellis_projection_errors_are_converted_to_recovery_faults(
        self,
    ) -> None:
        with (
            mock.patch.object(
                recovery_module.replay_module,
                "_segment_paths",
                side_effect=ValueError("layout changed"),
            ),
            self.assertRaisesRegex(RuntimeError, "segment layout changed"),
        ):
            self._project()

        event = _run_event()
        self.segment.write_bytes(event.canonical_json_bytes())
        with (
            mock.patch.object(
                recovery_module,
                "advance_external_effect_projection",
                side_effect=DispatchRecoveryProjectionError("invalid prefix"),
            ),
            self.assertRaisesRegex(RuntimeError, "external effect projection"),
        ):
            self._project(JournalHead(1, event.event_hash))

    def test_expected_control_root_builds_the_projection_validator(self) -> None:
        empty = recovery(self.root)
        validator = mock.Mock(return_value=True)
        with (
            mock.patch.object(
                recovery_module,
                "replay_journal",
                return_value=empty.replay,
            ),
            mock.patch.object(
                recovery_module.replay_module,
                "_filesystem_identity_validator",
                return_value=validator,
            ) as build_validator,
        ):
            result = recover_coordinator_lease(
                self.root,
                manifest(),
                coordinator_epoch=1,
                repair_derived=False,
                expected_control_root=object(),
            )

        self.assertEqual(LeaseRecoveryStatus.RECOVERED, result.status)
        build_validator.assert_called_once()
        self.assertGreaterEqual(validator.call_count, 2)

    def test_blocked_replay_without_fault_uses_the_defensive_fallback_detail(self) -> None:
        empty = recovery(self.root)
        replay_fault = ReplayFault(
            ReplayFaultCode.STATE_REJECTED,
            "blocked",
            None,
            None,
            GENESIS_HEAD.event_hash,
        )
        blocked = replace(
            empty.replay,
            status=ReplayStatus.BLOCKED,
            fault=replay_fault,
        )
        object.__setattr__(blocked, "fault", None)
        with mock.patch.object(recovery_module, "replay_journal", return_value=blocked):
            result = recover_coordinator_lease(
                self.root,
                manifest(),
                coordinator_epoch=1,
                repair_derived=False,
            )

        self.assertEqual(LeaseRecoveryStatus.BLOCKED, result.status)
        assert result.fault is not None
        self.assertEqual("verified journal replay blocked", result.fault.detail)


class CoordinatorLeaseServiceBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"
        controller = service(
            self.root,
            owner("coordinator-a"),
            Clock(BASE_TIME),
        )
        self.acquired = controller.acquire(
            event_id="EVENT-ACQUIRE-A",
            lease_id="LEASE-A",
        )
        self.active = recovery(self.root)
        self.empty = recovery(Path(self.temporary.name) / "empty")
        assert self.active.lease_state is not None
        assert self.active.lease_state.lease is not None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_constructor_and_public_lost_reject_invalid_collaborators(self) -> None:
        with self.assertRaisesRegex(TypeError, "prior_owner_process_probe"):
            _lease_service(
                self.active,
                AlwaysConflictJournal(),
                probe=object(),
            )
        with self.assertRaisesRegex(TypeError, "expected_lease"):
            _lease_service(self.active, AlwaysConflictJournal()).lost(
                event_id="EVENT-LOST",
                expected_lease=object(),  # type: ignore[arg-type]
            )

    def test_acquire_checks_nonmatching_retry_before_attempting_append(self) -> None:
        journal = AlwaysConflictJournal()
        result = _lease_service(
            self.active,
            journal,
            max_conflict_retries=0,
        ).acquire(event_id="EVENT-ACQUIRE-NEW", lease_id="LEASE-A")

        self.assertEqual(1, journal.calls)
        self.assertIn("retry limit", result.detail or "")

    def test_lost_and_holder_transitions_fail_closed_for_blocked_recovery(self) -> None:
        blocked = LeaseRecoveryResult(
            LeaseRecoveryStatus.BLOCKED,
            self.active.replay,
            fault=_blocked_fault(),
        )
        expected = self.active.lease_state.lease
        lost = _lease_service(blocked, AlwaysConflictJournal()).lost(
            event_id="EVENT-LOST",
            expected_lease=expected,
        )
        renewed = _lease_service(blocked, AlwaysConflictJournal()).renew(
            event_id="EVENT-RENEW"
        )

        self.assertEqual(LeaseRecoveryStatus.BLOCKED, lost.recovery.status)
        self.assertEqual(LeaseRecoveryStatus.BLOCKED, renewed.recovery.status)

    def test_lost_rejects_an_absent_lease_and_exhausts_conflict_retries(self) -> None:
        expected = self.active.lease_state.lease
        absent = _lease_service(self.empty, AlwaysConflictJournal()).lost(
            event_id="EVENT-LOST-ABSENT",
            expected_lease=expected,
        )
        journal = AlwaysConflictJournal()
        exhausted = _lease_service(self.active, journal).lost(
            event_id="EVENT-LOST-CONFLICT",
            expected_lease=expected,
        )

        self.assertEqual(LeaseStateCode.NO_ACTIVE_LEASE, absent.lease_state_code)
        self.assertEqual(2, journal.calls)
        self.assertIn("retry limit", exhausted.detail or "")

    def test_holder_transition_handles_absent_inactive_and_conflicting_leases(self) -> None:
        absent = _lease_service(self.empty, AlwaysConflictJournal()).renew(
            event_id="EVENT-RENEW-ABSENT"
        )

        real = service(
            self.root,
            owner("coordinator-a"),
            Clock(BASE_TIME),
        )
        real.release(event_id="EVENT-RELEASE-A")
        released = recovery(self.root)
        inactive = _lease_service(released, AlwaysConflictJournal()).renew(
            event_id="EVENT-RENEW-INACTIVE"
        )

        journal = AlwaysConflictJournal()
        exhausted = _lease_service(self.active, journal).renew(
            event_id="EVENT-RENEW-CONFLICT"
        )

        self.assertEqual(LeaseStateCode.NO_ACTIVE_LEASE, absent.lease_state_code)
        self.assertEqual(LeaseStateCode.NO_ACTIVE_LEASE, inactive.lease_state_code)
        self.assertEqual(2, journal.calls)
        self.assertIn("retry limit", exhausted.detail or "")

    def test_final_retry_required_recovery_is_yielded_and_blocked(self) -> None:
        retry = LeaseRecoveryResult(
            LeaseRecoveryStatus.RETRY_REQUIRED,
            self.empty.replay,
            fault=LeaseRecoveryFault(
                LeaseRecoveryFaultCode.JOURNAL_CHANGED,
                "changed",
            ),
        )
        result = _lease_service(
            retry,
            AlwaysConflictJournal(),
            max_conflict_retries=0,
        ).acquire(event_id="EVENT-ACQUIRE", lease_id="LEASE")

        self.assertEqual(LeaseRecoveryStatus.RETRY_REQUIRED, result.recovery.status)
        self.assertIn("journal_changed", result.detail or "")

    def test_retry_required_recovery_is_retried_while_budget_remains(self) -> None:
        retry = LeaseRecoveryResult(
            LeaseRecoveryStatus.RETRY_REQUIRED,
            self.empty.replay,
            fault=LeaseRecoveryFault(
                LeaseRecoveryFaultCode.JOURNAL_CHANGED,
                "changed",
            ),
        )
        recoveries = iter((retry, self.empty))
        journal = AlwaysConflictJournal()
        controller = CoordinatorLeaseService(
            journal,
            lambda: next(recoveries),
            run_id=RUN_ID,
            owner=owner("coordinator-a"),
            manifest_digest=MANIFEST_DIGEST,
            lease_ttl_seconds=90,
            max_conflict_retries=1,
        )

        result = controller.acquire(event_id="EVENT-ACQUIRE", lease_id="LEASE-001")

        self.assertEqual(1, journal.calls)
        self.assertIn("retry limit", result.detail or "")

    def test_invalid_append_result_from_journal_is_rejected(self) -> None:
        event = self.active.last_lease_event
        assert event is not None
        forged = AppendResult(
            AppendStatus.COMMITTED,
            JournalHead(event.sequence, event.event_hash),
            event,
        )
        object.__setattr__(forged, "event", None)
        with self.assertRaisesRegex(TypeError, "invalid append result"):
            _lease_service(self.active, _ResultJournal(forged)).acquire(
                event_id="EVENT-ACQUIRE-NEW",
                lease_id="LEASE-NEW",
            )

    def test_invalid_probe_result_and_recovered_run_id_fail_closed(self) -> None:
        with self.assertRaisesRegex(TypeError, "invalid result"):
            _lease_service(
                self.active,
                AlwaysConflictJournal(),
                coordinator_id="coordinator-b",
                probe=lambda *_args, **_kwargs: object(),
            ).acquire(event_id="EVENT-ACQUIRE-B", lease_id="LEASE-B")

        other_snapshot = replace(self.empty.replay.snapshot, run_id="RUN-OTHER")
        other_replay = replace(self.empty.replay, snapshot=other_snapshot)
        other = LeaseRecoveryResult(
            LeaseRecoveryStatus.RECOVERED,
            other_replay,
            self.empty.lease_state,
        )
        blocked = _lease_service(other, AlwaysConflictJournal()).acquire(
            event_id="EVENT-ACQUIRE",
            lease_id="LEASE",
        )
        self.assertIn("run_id", blocked.detail or "")

    def test_missing_fault_on_untrusted_recovery_uses_generic_detail(self) -> None:
        retry = LeaseRecoveryResult(
            LeaseRecoveryStatus.RETRY_REQUIRED,
            self.empty.replay,
            fault=LeaseRecoveryFault(
                LeaseRecoveryFaultCode.JOURNAL_CHANGED,
                "changed",
            ),
        )
        object.__setattr__(retry, "fault", None)
        blocked = _lease_service(
            retry,
            AlwaysConflictJournal(),
            max_conflict_retries=0,
        ).acquire(event_id="EVENT-ACQUIRE", lease_id="LEASE")

        self.assertEqual(
            "lease recovery did not produce verified state",
            blocked.detail,
        )


if __name__ == "__main__":
    unittest.main()
