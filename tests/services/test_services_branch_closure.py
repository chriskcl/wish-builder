from __future__ import annotations

import dataclasses
import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.services.test_journal_append import journal_event
from tests.services.test_journal_leases import (
    BASE_TIME,
    MANIFEST_DIGEST,
    RUN_ID as LEASE_RUN_ID,
    lease_draft,
    lease_owner,
    run_draft,
)
from tests.services.test_recovery import (
    AlwaysConflictJournal,
    owner as recovery_owner,
    recovery as recover_lease,
)
from tests.services.test_service_edge_coverage import (
    CleanupRepository,
    cleanup_plan,
    prepared_effect,
)
from wish_builder.contracts import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseDraftPayload,
    RuntimeReasonCode,
    RuntimeState,
    SchedulerMode,
    TransitionPayload,
    TransitionSubject,
    canonical_json_bytes,
)
from wish_builder.services.cleanup import CleanupDisposition, CleanupService
from wish_builder.services.journal import (
    GENESIS_HEAD,
    AppendResult,
    AppendStatus,
    CoordinatorLeaseState,
    DurableJournal,
    JournalEventDraft,
    JournalFaultCode,
    JournalHead,
    LeaseStateCode,
    LeaseStateError,
    PersistenceFault,
)
from wish_builder.services.ports import PersistedEffectRequest, PreparedEffect
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    LeaseAction,
    LeaseMutationResult,
    LeaseMutationStatus,
    LeaseRecoveryFault,
    LeaseRecoveryFaultCode,
    LeaseRecoveryResult,
    LeaseRecoveryStatus,
)


def _lease_event(
    event_type: JournalEventType,
    *,
    token: int,
    lease_id: str = "LEASE-001",
    coordinator_id: str = "coordinator-a",
    sequence: int = 1,
    previous_hash: str = GENESIS_HEAD.event_hash,
    authority_time: datetime = BASE_TIME,
) -> JournalEvent:
    return lease_draft(
        event_type,
        token=token,
        lease_id=lease_id,
        coordinator_id=coordinator_id,
    ).materialize(
        sequence=sequence,
        previous_event_hash=previous_hash,
        authority_time=authority_time,
    )


def _transition_event(
    sequence: int,
    previous_hash: str,
    *,
    run_id: str = LEASE_RUN_ID,
) -> JournalEvent:
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-TRANSITION-{sequence:03d}",
        event_type=JournalEventType.RUN_INITIALIZED,
        identity=ExecutionIdentity(run_id, 0),
        actor_type=ActorType.SYSTEM,
        actor_id="wishctl",
        recorded_at="2026-08-19T00:00:00Z",
        previous_event_hash=previous_hash,
        payload=TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.NONE,
            RuntimeState.PREFLIGHT,
        ),
    )


class JournalBranchClosureTests(unittest.TestCase):
    def test_draft_and_state_validation_edges_are_closed(self) -> None:
        draft = run_draft()
        lease = lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            token=1,
            lease_id="LEASE-001",
            coordinator_id="coordinator-a",
        )
        invalid_drafts = (
            lambda: dataclasses.replace(draft, event_id=""),
            lambda: dataclasses.replace(draft, event_type="bad"),
            lambda: dataclasses.replace(draft, identity=object()),
            lambda: dataclasses.replace(draft, actor_type="system"),
            lambda: dataclasses.replace(draft, actor_id=""),
            lambda: dataclasses.replace(draft, reason_code="pause"),
            lambda: dataclasses.replace(lease, event_type=JournalEventType.RUN_INITIALIZED),
            lambda: dataclasses.replace(lease, identity=ExecutionIdentity(LEASE_RUN_ID, 2)),
            lambda: dataclasses.replace(draft, event_type=JournalEventType.LEASE_RENEWED),
        )
        for build in invalid_drafts:
            with self.subTest(build=build), self.assertRaises((TypeError, ValueError)):
                build()

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            draft.materialize(
                sequence=1,
                previous_event_hash=GENESIS_HEAD.event_hash,
                authority_time=datetime(2026, 8, 19),
            )
        self.assertFalse(draft.matches_event(object()))

        acquired = _lease_event(JournalEventType.LEASE_ACQUIRED, token=1)
        lease_payload = acquired.payload
        mismatched_payload_event = object.__new__(JournalEvent)
        for field in dataclasses.fields(JournalEvent):
            object.__setattr__(
                mismatched_payload_event,
                field.name,
                getattr(acquired, field.name),
            )
        object.__setattr__(
            mismatched_payload_event,
            "payload",
            TransitionPayload(
                TransitionSubject.RUN,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
        )
        self.assertFalse(lease.matches_event(mismatched_payload_event))

        with self.assertRaises(TypeError):
            LeaseStateError("bad")  # type: ignore[arg-type]
        invalid_states = (
            lambda: CoordinatorLeaseState(object()),  # type: ignore[arg-type]
            lambda: CoordinatorLeaseState(GENESIS_HEAD, None, lease_payload, 0),
            lambda: CoordinatorLeaseState(
                GENESIS_HEAD,
                JournalEventType.RUN_INITIALIZED,
                lease_payload,
                1,
            ),
            lambda: CoordinatorLeaseState(
                GENESIS_HEAD,
                JournalEventType.LEASE_ACQUIRED,
                object(),  # type: ignore[arg-type]
                1,
            ),
            lambda: CoordinatorLeaseState(
                GENESIS_HEAD,
                JournalEventType.LEASE_ACQUIRED,
                lease_payload,
                2,
            ),
        )
        for build in invalid_states:
            with self.subTest(build=build), self.assertRaises((TypeError, ValueError)):
                build()

    def test_lease_state_transition_edges_are_closed(self) -> None:
        initial = CoordinatorLeaseState.initial()
        acquired = _lease_event(JournalEventType.LEASE_ACQUIRED, token=1)
        acquired_state = initial.advance(acquired)

        with self.assertRaises(TypeError):
            initial.advance(object())  # type: ignore[arg-type]
        self.assertIs(acquired_state.advance(acquired), acquired_state)
        with self.assertRaisesRegex(LeaseStateError, LeaseStateCode.HEAD_MISMATCH.value):
            initial.advance(_transition_event(2, GENESIS_HEAD.event_hash))

        unchanged = initial.advance(_transition_event(1, GENESIS_HEAD.event_hash))
        self.assertEqual(GENESIS_HEAD.sequence + 1, unchanged.head.sequence)

        non_lease_type = object.__new__(JournalEvent)
        for field in dataclasses.fields(JournalEvent):
            object.__setattr__(non_lease_type, field.name, getattr(acquired, field.name))
        object.__setattr__(non_lease_type, "event_type", JournalEventType.RUN_INITIALIZED)
        with self.assertRaisesRegex(LeaseStateError, LeaseStateCode.EVENT_MISMATCH.value):
            initial.advance(non_lease_type)

        renewed_without_active = _lease_event(JournalEventType.LEASE_RENEWED, token=1)
        with self.assertRaisesRegex(LeaseStateError, LeaseStateCode.NO_ACTIVE_LEASE.value):
            initial.advance(renewed_without_active)

        wrong_holder = _lease_event(
            JournalEventType.LEASE_RELEASED,
            token=1,
            coordinator_id="coordinator-b",
            sequence=2,
            previous_hash=acquired.event_hash,
            authority_time=BASE_TIME + timedelta(seconds=10),
        )
        with self.assertRaisesRegex(
            LeaseStateError,
            LeaseStateCode.LEASE_IDENTITY_MISMATCH.value,
        ):
            acquired_state.advance(wrong_holder)

        late_renewal = _lease_event(
            JournalEventType.LEASE_RENEWED,
            token=1,
            sequence=2,
            previous_hash=acquired.event_hash,
            authority_time=BASE_TIME + timedelta(seconds=88),
        )
        with self.assertRaisesRegex(LeaseStateError, LeaseStateCode.RENEWAL_TOO_LATE.value):
            acquired_state.advance(late_renewal)

        valid_renewal = _lease_event(
            JournalEventType.LEASE_RENEWED,
            token=1,
            sequence=2,
            previous_hash=acquired.event_hash,
            authority_time=BASE_TIME + timedelta(seconds=1),
        )
        stale_payload = dataclasses.replace(
            valid_renewal.payload,
            expires_at=acquired.payload.expires_at,
        )
        stale_renewal = object.__new__(JournalEvent)
        for field in dataclasses.fields(JournalEvent):
            object.__setattr__(
                stale_renewal,
                field.name,
                getattr(valid_renewal, field.name),
            )
        object.__setattr__(stale_renewal, "payload", stale_payload)
        with self.assertRaisesRegex(
            LeaseStateError,
            LeaseStateCode.EXPIRY_NOT_EXTENDED.value,
        ):
            acquired_state.advance(stale_renewal)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            acquired_state.allows_admission(
                authority_time=datetime(2026, 8, 19),
                coordinator_id="coordinator-a",
                owner=lease_owner("coordinator-a"),
                fencing_token=1,
                manifest_digest=MANIFEST_DIGEST,
            )
        with self.assertRaises(TypeError):
            acquired_state.allows_admission(
                authority_time=BASE_TIME,
                coordinator_id="coordinator-a",
                owner=object(),  # type: ignore[arg-type]
                fencing_token=1,
                manifest_digest=MANIFEST_DIGEST,
            )
        self.assertFalse(
            initial.allows_admission(
                authority_time=BASE_TIME,
                coordinator_id="coordinator-a",
                owner=lease_owner("coordinator-a"),
                fencing_token=1,
                manifest_digest=MANIFEST_DIGEST,
            )
        )

    def test_durable_journal_rejects_invalid_storage_boundaries(self) -> None:
        event = journal_event(1, GENESIS_HEAD.event_hash, run_id="RUN-001")
        other = journal_event(1, GENESIS_HEAD.event_hash, variant=99, run_id="RUN-001")
        draft = run_draft()

        class AppendStorage:
            def __init__(self, result: object | BaseException) -> None:
                self.result = result

            def compare_and_append(self, **_: object) -> object:
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

        invalid_append_results = (
            AppendResult(AppendStatus.COMMITTED, JournalHead(1, other.event_hash), other),
            AppendResult(AppendStatus.CONFLICT, GENESIS_HEAD),
        )
        for result in invalid_append_results:
            journal = DurableJournal("RUN-001", AppendStorage(result))  # type: ignore[arg-type]
            with self.subTest(result=result), self.assertRaises(TypeError):
                journal.append(event, expected_head=GENESIS_HEAD)

        faulting = DurableJournal(
            "RUN-001",
            AppendStorage(
                PersistenceFault(
                    JournalFaultCode.WRITE_FAILED,
                    "write",
                    last_committed_head=GENESIS_HEAD,
                )
            ),
        )  # type: ignore[arg-type]
        failed = faulting.append(event, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, failed.status)
        blocked = faulting.append(event, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, blocked.status)

        class DraftStorage:
            def __init__(self, result: object | BaseException) -> None:
                self.result = result

            def compare_and_append_draft(self, **_: object) -> object:
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

        with self.assertRaises(TypeError):
            DurableJournal(LEASE_RUN_ID, DraftStorage(object())).append_draft(  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                expected_head=GENESIS_HEAD,
            )
        with self.assertRaises(TypeError):
            DurableJournal(LEASE_RUN_ID, DraftStorage(object())).append_draft(  # type: ignore[arg-type]
                draft,
                expected_head=object(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "run_id"):
            DurableJournal("RUN-OTHER", DraftStorage(object())).append_draft(  # type: ignore[arg-type]
                draft,
                expected_head=GENESIS_HEAD,
            )

        lease = lease_draft(
            JournalEventType.LEASE_ACQUIRED,
            token=1,
            lease_id="LEASE-001",
            coordinator_id="coordinator-a",
        )
        with self.assertRaises(TypeError):
            DurableJournal(LEASE_RUN_ID, DraftStorage(object())).append_draft(  # type: ignore[arg-type]
                lease,
                expected_head=GENESIS_HEAD,
            )
        with self.assertRaisesRegex(ValueError, "lease_state"):
            DurableJournal(LEASE_RUN_ID, DraftStorage(object())).append_draft(  # type: ignore[arg-type]
                lease,
                expected_head=GENESIS_HEAD,
                lease_state=CoordinatorLeaseState(
                    JournalHead(1, "sha256:" + "1" * 64),
                ),
            )
        with self.assertRaises(TypeError):
            DurableJournal(LEASE_RUN_ID, DraftStorage(object())).append_draft(  # type: ignore[arg-type]
                draft,
                expected_head=GENESIS_HEAD,
                lease_state=object(),  # type: ignore[arg-type]
            )

        for result in (
            object(),
            AppendResult(
                AppendStatus.COMMITTED,
                JournalHead(1, other.event_hash),
                other,
            ),
            AppendResult(AppendStatus.CONFLICT, GENESIS_HEAD),
        ):
            journal = DurableJournal(LEASE_RUN_ID, DraftStorage(result))  # type: ignore[arg-type]
            with self.subTest(draft_result=result), self.assertRaises(TypeError):
                journal.append_draft(draft, expected_head=GENESIS_HEAD)

        faulting_draft = DurableJournal(
            LEASE_RUN_ID,
            DraftStorage(
                PersistenceFault(
                    JournalFaultCode.WRITE_FAILED,
                    "draft",
                    last_committed_head=GENESIS_HEAD,
                )
            ),
        )  # type: ignore[arg-type]
        self.assertEqual(
            AppendStatus.PERSISTENCE_FAILED,
            faulting_draft.append_draft(draft, expected_head=GENESIS_HEAD).status,
        )
        self.assertEqual(
            AppendStatus.PERSISTENCE_FAILED,
            faulting_draft.append_draft(draft, expected_head=GENESIS_HEAD).status,
        )


class RecoveryBranchClosureTests(unittest.TestCase):
    def test_recovery_models_and_service_constructor_close_guard_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recovered = recover_lease(Path(temporary) / "journal")
        assert recovered.lease_state is not None
        fault = LeaseRecoveryFault(
            LeaseRecoveryFaultCode.REPLAY_BLOCKED,
            "blocked",
        )

        invalid_faults = (
            lambda: LeaseRecoveryFault("blocked", "detail"),  # type: ignore[arg-type]
            lambda: LeaseRecoveryFault(LeaseRecoveryFaultCode.REPLAY_BLOCKED, ""),
            lambda: LeaseRecoveryFault(
                LeaseRecoveryFaultCode.REPLAY_BLOCKED,
                "detail",
                object(),  # type: ignore[arg-type]
            ),
        )
        for build in invalid_faults:
            with self.subTest(build=build), self.assertRaises((TypeError, ValueError)):
                build()

        invalid_results = (
            lambda: LeaseRecoveryResult("recovered", recovered.replay),  # type: ignore[arg-type]
            lambda: LeaseRecoveryResult(LeaseRecoveryStatus.RECOVERED, object()),  # type: ignore[arg-type]
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                recovered.replay,
                fault=fault,
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                recovered.replay,
                dataclasses.replace(
                    recovered.lease_state,
                    head=JournalHead(1, "sha256:" + "1" * 64),
                ),
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.BLOCKED,
                recovered.replay,
                recovered.lease_state,
                fault=fault,
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.RECOVERED,
                recovered.replay,
                recovered.lease_state,
                pending_dispatch_requests=(object(),),  # type: ignore[arg-type]
            ),
            lambda: LeaseRecoveryResult(
                LeaseRecoveryStatus.BLOCKED,
                recovered.replay,
                pending_dispatch_requests=(),
                fault=None,
            ),
        )
        for build in invalid_results:
            with self.subTest(build=build), self.assertRaises((TypeError, ValueError)):
                build()

        valid_owner = recovery_owner("coordinator-a")
        valid_args = {
            "journal": AlwaysConflictJournal(),
            "recover": lambda: recovered,
            "run_id": "RUN-RECOVERY",
            "owner": valid_owner,
            "manifest_digest": "sha256:" + "a" * 64,
            "lease_ttl_seconds": 90,
        }
        invalid_services = (
            {"journal": object()},
            {"recover": object()},
            {"run_id": ""},
            {"owner": object()},
            {"recovery_actor_id": ""},
            {"max_conflict_retries": -1},
        )
        for changes in invalid_services:
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                CoordinatorLeaseService(**(valid_args | changes))  # type: ignore[arg-type]

    def test_mutation_result_and_blocked_recovery_edges_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recovered = recover_lease(Path(temporary) / "journal")
        assert recovered.lease_state is not None
        event = _lease_event(JournalEventType.LEASE_ACQUIRED, token=1)
        append = AppendResult(AppendStatus.COMMITTED, JournalHead(1, event.event_hash), event)
        blocked_append = AppendResult(
            AppendStatus.PERSISTENCE_FAILED,
            GENESIS_HEAD,
            fault_code=JournalFaultCode.WRITE_FAILED,
        )
        invalid_mutations = (
            lambda: LeaseMutationResult("acquire", LeaseMutationStatus.BLOCKED, recovered, None),  # type: ignore[arg-type]
            lambda: LeaseMutationResult(LeaseAction.ACQUIRE, "blocked", recovered, None),  # type: ignore[arg-type]
            lambda: LeaseMutationResult(LeaseAction.ACQUIRE, LeaseMutationStatus.BLOCKED, object(), None),  # type: ignore[arg-type]
            lambda: LeaseMutationResult(LeaseAction.ACQUIRE, LeaseMutationStatus.BLOCKED, recovered, object()),  # type: ignore[arg-type]
            lambda: LeaseMutationResult(LeaseAction.ACQUIRE, LeaseMutationStatus.BLOCKED, recovered, None, object()),  # type: ignore[arg-type]
            lambda: LeaseMutationResult(
                LeaseAction.ACQUIRE,
                LeaseMutationStatus.BLOCKED,
                recovered,
                None,
                lease_state_code="no_active_lease",  # type: ignore[arg-type]
            ),
            lambda: LeaseMutationResult(
                LeaseAction.ACQUIRE,
                LeaseMutationStatus.BLOCKED,
                recovered,
                None,
                detail="",
            ),
            lambda: LeaseMutationResult(
                LeaseAction.ACQUIRE,
                LeaseMutationStatus.COMMITTED,
                recovered,
                recovered.lease_state,
            ),
            lambda: LeaseMutationResult(
                LeaseAction.ACQUIRE,
                LeaseMutationStatus.REJECTED,
                recovered,
                recovered.lease_state,
            ),
            lambda: LeaseMutationResult(
                LeaseAction.ACQUIRE,
                LeaseMutationStatus.BLOCKED,
                recovered,
                None,
                append_result=append,
            ),
        )
        for build in invalid_mutations:
            with self.subTest(build=build), self.assertRaises((TypeError, ValueError)):
                build()

        blocked = LeaseMutationResult(
            LeaseAction.ACQUIRE,
            LeaseMutationStatus.BLOCKED,
            recovered,
            None,
            append_result=blocked_append,
        )
        self.assertFalse(blocked.succeeded)

        service = CoordinatorLeaseService(
            AlwaysConflictJournal(),
            lambda: dataclasses.replace(recovered.replay.snapshot, run_id="RUN-OTHER"),
            run_id="RUN-RECOVERY",
            owner=recovery_owner("coordinator-a"),
            manifest_digest="sha256:" + "a" * 64,
            lease_ttl_seconds=90,
        )
        with self.assertRaises(TypeError):
            service.acquire(event_id="EVENT", lease_id="LEASE")


class EffectsAndCleanupBranchClosureTests(unittest.TestCase):
    def test_prepared_effect_revalidates_operation_identity(self) -> None:
        class MutableCommand:
            def __init__(self, operation_id: object) -> None:
                self.operation_id = operation_id
                self.payload = {"operation_id": operation_id}

            def to_primitive(self) -> dict[str, object]:
                return dict(self.payload)

        command = MutableCommand("OPERATION-001")
        request_hash = "sha256:" + hashlib.sha256(
            canonical_json_bytes(command.to_primitive())
        ).hexdigest()
        event = JournalEvent.create(
            sequence=1,
            event_id="EVENT-EFFECT-001",
            event_type=JournalEventType.EFFECT_REQUESTED,
            identity=ExecutionIdentity(
                "RUN-EFFECT",
                1,
                "TASK-001",
                1,
                "OPERATION-001",
            ),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-a",
            recorded_at="2026-08-19T00:00:00Z",
            previous_event_hash=GENESIS_HEAD.event_hash,
            payload=EffectRequestPayload(
                EffectOperation.CLEANUP,
                AdapterKind.GIT,
                EffectObjectType.CLEANUP_ITEM,
                "sha256:" + "1" * 64,
                request_hash,
                0,
                1,
            ),
        )
        append = AppendResult(AppendStatus.COMMITTED, JournalHead(1, event.event_hash), event)
        request = PersistedEffectRequest(event, append)

        with self.assertRaises(TypeError):
            PreparedEffect(request, MutableCommand(1))  # type: ignore[arg-type]

        prepared = PreparedEffect(request, command)
        command.operation_id = "OPERATION-CHANGED"
        with self.assertRaisesRegex(ValueError, "operation_id changed"):
            _ = prepared.operation_id

    def test_cleanup_unknown_plan_is_cleared_by_later_known_observation(self) -> None:
        repository = CleanupRepository(disposition=CleanupDisposition.UNKNOWN)
        service = CleanupService(
            repository,
            available_bytes=lambda: 1024,
            minimum_free_bytes=0,
        )
        plan = cleanup_plan()
        effect = prepared_effect(
            plan.command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            adapter=AdapterKind.GIT,
            object_type=EffectObjectType.CLEANUP_ITEM,
            target_hash=plan.command.target_workspace_hash,
        )

        unknown = service.apply(effect, plan)
        self.assertEqual(CleanupDisposition.UNKNOWN, unknown.disposition)
        self.assertTrue(service.blocked_on_unknown)

        repository.disposition = CleanupDisposition.REMOVED
        known = service.apply(effect, plan)
        self.assertEqual(CleanupDisposition.REMOVED, known.disposition)
        self.assertFalse(service.blocked_on_unknown)

    def test_checkpoint_and_recovery_control_root_validator_false_paths(self) -> None:
        from wish_builder.services import checkpoints as checkpoints_module
        from wish_builder.services import recovery as recovery_module

        self.assertFalse(checkpoints_module._filesystem_identity_validator(object())())
        self.assertFalse(recovery_module.replay_module._filesystem_identity_validator(object())())

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                checkpoints_module,
                "_is_link_or_junction",
                return_value=True,
            ):
                with self.assertRaisesRegex(ValueError, "symbolic links"):
                    store = checkpoints_module.CheckpointStore(Path(temporary))
                    store._prepare_directories()


if __name__ == "__main__":
    unittest.main()
