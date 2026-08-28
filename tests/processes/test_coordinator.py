from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.adapters.test_trellis_graph_import import (
    payload as trellis_payload,
)
from tests.adapters.test_trellis_graph_import import (
    settings,
)
from tests.adapters.test_trellis_graph_import import (
    snapshot as trellis_snapshot,
)
from tests.adapters.test_trellis_graph_import import (
    task as trellis_task,
)
from wish_builder.adapters.fake import FakeEffectCrash, FakeTaskPort
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.adapters.trellis import import_trellis_snapshot
from wish_builder.contracts import SchedulerMode
from wish_builder.contracts.runtime import (
    ActorIdentity,
    ActorType,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionCommand,
    DecisionObservedPayload,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    EffectStatus,
    ExecutionIdentity,
    JournalEventType,
    LeaseDraftPayload,
    LeaseOwner,
    RuntimeReasonCode,
    RuntimeState,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel import KernelSnapshot, TaskDag, apply_journal_event
from wish_builder.kernel.gates import evaluate_decision
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.processes.coordinator import (
    CoordinatorCursor,
    CoordinatorReason,
    CoordinatorStatus,
    ForegroundCoordinator,
    WorkerResultProposal,
)
from wish_builder.services.journal import (
    AppendStatus,
    CoordinatorLeaseState,
    DurableJournal,
    JournalEventDraft,
)
from wish_builder.services.recovery import (
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)

BASE_TIME = datetime(2026, 8, 19, tzinfo=UTC)
COORDINATOR_ID = "coordinator-001"


def digest(character: str) -> str:
    return "sha256:" + character * 64


def lease_owner(actor_id: str = COORDINATOR_ID) -> LeaseOwner:
    return LeaseOwner(
        ActorIdentity(
            ActorType.COORDINATOR,
            actor_id,
            "host-001",
            4123,
            "process-start-001",
        ),
        digest("1"),
        digest("2"),
        digest("3"),
        digest("4"),
    )


def one_task_manifest():
    value = trellis_payload()
    value["requirements"] = [value["requirements"][0]]
    value["tasks"] = [trellis_task("trellis/only", "REQ-001")]
    return import_trellis_snapshot(
        trellis_snapshot(value),
        settings(),
    ).manifest


def sibling_manifest():
    value = trellis_payload()
    foundation_id = "trellis/foundation"
    left = trellis_task(
        "trellis/left",
        "REQ-002",
        depends_on=[foundation_id],
        wave=1,
    )
    right = trellis_task(
        "trellis/right",
        "REQ-002",
        depends_on=[foundation_id],
        wave=1,
    )
    right["owned_paths"] = ["src/req-002-right/**"]
    right["allowed_auxiliary_paths"] = ["tests/req-002-right/**"]
    value["tasks"] = [
        trellis_task(foundation_id, "REQ-001"),
        left,
        right,
    ]
    return import_trellis_snapshot(
        trellis_snapshot(value),
        settings(),
    ).manifest


class CoordinatorHarness:
    def __init__(
        self,
        root: Path,
        *,
        manifest=None,
        port: FakeTaskPort | None = None,
        coordinator_clock=None,
        execution_snapshot_admitter=None,
        gate_b_admission: bool = False,
    ) -> None:
        self.manifest = manifest or one_task_manifest()
        self.owner = lease_owner()
        self.storage = FilesystemJournalStorage(
            root / "journal",
            self.manifest.run_id,
            authority_clock=lambda: BASE_TIME,
        )
        self.journal = DurableJournal(self.manifest.run_id, self.storage)
        dag = TaskDag.compile(self.manifest)
        kernel_snapshot = KernelSnapshot.initial(self.manifest.run_id, 1, dag)
        lease_state = CoordinatorLeaseState.initial()
        phase_steps = (
            (
                JournalEventType.RUN_INITIALIZED,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
            (
                JournalEventType.PREFLIGHT_COMPLETED,
                RuntimeState.PREFLIGHT,
                RuntimeState.DISCOVERY,
            ),
            (
                JournalEventType.DISCOVERY_COMPLETED,
                RuntimeState.DISCOVERY,
                RuntimeState.GATE_A_PENDING,
            ),
            (
                JournalEventType.GATE_APPROVED,
                RuntimeState.GATE_A_PENDING,
                RuntimeState.TRELLIS_PREPARATION,
            ),
            (
                JournalEventType.TRELLIS_GRAPH_IMPORTED,
                RuntimeState.TRELLIS_PREPARATION,
                RuntimeState.GATE_B_PENDING,
            ),
            (
                JournalEventType.TASK_GRAPH_FROZEN,
                RuntimeState.GATE_B_PENDING,
                RuntimeState.EXECUTING,
            ),
        )
        for event_type, from_state, to_state in phase_steps:
            if (
                gate_b_admission
                and event_type is JournalEventType.TASK_GRAPH_FROZEN
            ):
                request_sequence = lease_state.head.sequence + 1
                request = DecisionRequest(
                    CommandIdentity(
                        1,
                        "COMMAND-GATE-B-FINISH-001",
                        "REQUEST-GATE-B-FINISH-001",
                        CommandKind.DECIDE,
                        request_sequence,
                        "nonce-gate-b-finish-001",
                        self.owner.actor,
                        SourceChannel.COORDINATOR,
                        "2026-08-19T00:00:00Z",
                    ),
                    DecisionType.GATE_B,
                    self.manifest.canonical_sha256(),
                    self.owner.workspace_hash,
                    "local-account-001",
                    (DecisionChoice.APPROVE, DecisionChoice.REJECT),
                )
                requested = self.journal.append_draft(
                    JournalEventDraft(
                        "EVENT-GATE-B-REQUEST-FINISH-001",
                        JournalEventType.DECISION_REQUESTED,
                        ExecutionIdentity(self.manifest.run_id, 1),
                        ActorType.COORDINATOR,
                        self.owner.actor.actor_id,
                        DecisionRequestPayload(request),
                    ),
                    expected_head=lease_state.head,
                )
                if (
                    requested.status is not AppendStatus.COMMITTED
                    or requested.event is None
                ):
                    raise AssertionError(requested)
                kernel_snapshot = replace(
                    kernel_snapshot,
                    last_sequence=requested.event.sequence,
                    last_event_id=requested.event.event_id,
                    last_event_hash=requested.event.event_hash,
                )
                lease_state = lease_state.advance(requested.event)

                command = DecisionCommand(
                    "DECISION-GATE-B-FINISH-001",
                    request,
                    DecisionChoice.APPROVE,
                    ActorIdentity(
                        ActorType.HUMAN,
                        "local-account-001",
                        "host-001",
                        5123,
                        "process-start-human-001",
                    ),
                    SourceChannel.DIRECT_CLI,
                    "2026-08-19T00:00:00Z",
                )
                evaluation = evaluate_decision(
                    request,
                    command,
                    current_sequence=request_sequence,
                    current_workspace_hash=self.owner.workspace_hash,
                )
                if not evaluation.accepted or evaluation.observation is None:
                    raise AssertionError(evaluation)
                observed = self.journal.append_draft(
                    JournalEventDraft(
                        "EVENT-GATE-B-OBSERVED-FINISH-001",
                        JournalEventType.DECISION_OBSERVED,
                        ExecutionIdentity(self.manifest.run_id, 1),
                        ActorType.HUMAN,
                        "local-account-001",
                        DecisionObservedPayload(evaluation.observation),
                    ),
                    expected_head=lease_state.head,
                )
                if (
                    observed.status is not AppendStatus.COMMITTED
                    or observed.event is None
                ):
                    raise AssertionError(observed)
                kernel_snapshot = replace(
                    kernel_snapshot,
                    last_sequence=observed.event.sequence,
                    last_event_id=observed.event.event_id,
                    last_event_hash=observed.event.event_hash,
                )
                lease_state = lease_state.advance(observed.event)
            sequence = lease_state.head.sequence + 1
            appended = self.journal.append_draft(
                JournalEventDraft(
                    f"EVENT-SEED-{sequence:04d}",
                    event_type,
                    ExecutionIdentity(self.manifest.run_id, 1),
                    ActorType.SYSTEM,
                    "test-bootstrap",
                    TransitionPayload(
                        TransitionSubject.RUN,
                        from_state,
                        to_state,
                    ),
                ),
                expected_head=lease_state.head,
            )
            if appended.status is not AppendStatus.COMMITTED or appended.event is None:
                raise AssertionError(appended)
            applied_phase = apply_journal_event(kernel_snapshot, appended.event)
            if not applied_phase.accepted:
                raise AssertionError(applied_phase.reason)
            kernel_snapshot = applied_phase.snapshot
            lease_state = lease_state.advance(appended.event)

        lease = self.journal.append_draft(
            JournalEventDraft(
                "EVENT-LEASE-ACQUIRED-00000001",
                JournalEventType.LEASE_ACQUIRED,
                ExecutionIdentity(self.manifest.run_id, 1),
                ActorType.COORDINATOR,
                COORDINATOR_ID,
                LeaseDraftPayload(
                    "LEASE-001",
                    COORDINATOR_ID,
                    self.owner,
                    SchedulerMode.WISH_BUILDER,
                    1,
                    self.manifest.canonical_sha256(),
                    300,
                    10,
                ),
            ),
            expected_head=lease_state.head,
            lease_state=lease_state,
        )
        if lease.status is not AppendStatus.COMMITTED or lease.event is None:
            raise AssertionError(lease)
        lease_state = lease_state.advance(lease.event)
        applied = apply_journal_event(kernel_snapshot, lease.event)
        if not applied.accepted:
            raise AssertionError(applied.reason)
        graph = GraphIndex.compile(self.manifest, applied.snapshot)
        self.port = port or FakeTaskPort(
            root / "effects",
            clock=lambda: "2026-08-19T00:00:10Z",
        )
        self.coordinator = ForegroundCoordinator(
            self.manifest,
            CoordinatorCursor(applied.snapshot, graph, lease_state),
            self.journal,
            self.port,
            coordinator_id=COORDINATOR_ID,
            owner=self.owner,
            fencing_token=1,
            authority_clock=coordinator_clock
            or (lambda: BASE_TIME + timedelta(seconds=10)),
            execution_snapshot_admitter=execution_snapshot_admitter
            or (lambda: True),
        )


class ForegroundCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reservation_is_effect_free_until_dispatch_reserved(self) -> None:
        harness = CoordinatorHarness(self.root)

        reserved = harness.coordinator.reserve_ready()

        self.assertEqual(CoordinatorStatus.PROGRESSED, reserved.status)
        self.assertEqual(1, len(reserved.reserved))
        self.assertFalse(harness.port.effects.exists())
        self.assertEqual(RuntimeState.LEASED, reserved.cursor.snapshot.tasks[0].state)
        self.assertEqual(
            RuntimeState.RESERVED,
            reserved.cursor.snapshot.attempts[0].state,
        )
        self.assertEqual(
            [
                JournalEventType.TASK_READY,
                JournalEventType.LEASE_ACQUIRED,
                JournalEventType.ATTEMPT_RESERVED,
            ],
            [event.event_type for event in reserved.events],
        )

        dispatched = harness.coordinator.dispatch_reserved(reserved.reserved[0])

        self.assertEqual(CoordinatorStatus.PROGRESSED, dispatched.status)
        self.assertEqual(reserved.reserved, dispatched.dispatched)
        self.assertEqual(1, len(tuple(harness.port.effects.glob("*.json"))))
        self.assertEqual(
            RuntimeState.RUNNING,
            dispatched.cursor.snapshot.attempts[0].state,
        )

    def test_reserve_ready_reuses_current_reserved_attempt_after_restart_boundary(
        self,
    ) -> None:
        harness = CoordinatorHarness(self.root)
        first = harness.coordinator.reserve_ready()
        head = first.cursor.head

        resumed = harness.coordinator.reserve_ready()

        self.assertEqual(CoordinatorStatus.PROGRESSED, resumed.status)
        self.assertEqual(first.reserved, resumed.reserved)
        self.assertEqual((), resumed.events)
        self.assertEqual(head, resumed.cursor.head)
        self.assertFalse(harness.port.effects.exists())

    def test_dispatch_persists_request_before_effect_and_observation_after(
        self,
    ) -> None:
        saw_durable_request = False

        def failpoint(point: str, path: Path) -> None:
            nonlocal saw_durable_request
            del path
            if point == "before_effect":
                raw = next(
                    (self.root / "journal" / "segments").glob("*.jsonl")
                ).read_bytes()
                saw_durable_request = b'"event_type":"dispatch_requested"' in raw

        port = FakeTaskPort(
            self.root / "effects",
            clock=lambda: "2026-08-19T00:00:10Z",
            failpoint=failpoint,
        )
        harness = CoordinatorHarness(self.root, port=port)

        result = harness.coordinator.dispatch_ready()

        self.assertEqual(CoordinatorStatus.PROGRESSED, result.status)
        self.assertTrue(saw_durable_request)
        self.assertEqual(
            [
                JournalEventType.TASK_READY,
                JournalEventType.LEASE_ACQUIRED,
                JournalEventType.ATTEMPT_RESERVED,
                JournalEventType.DISPATCH_REQUESTED,
                JournalEventType.DISPATCH_OBSERVED,
            ],
            [event.event_type for event in result.events],
        )
        self.assertEqual(RuntimeState.DISPATCHED, result.cursor.snapshot.tasks[0].state)
        self.assertEqual(RuntimeState.RUNNING, result.cursor.snapshot.attempts[0].state)
        self.assertEqual(EffectStatus.APPLIED, result.receipt.status)  # type: ignore[union-attr]

    def test_graph_drift_immediately_before_request_writes_no_dispatch_event(
        self,
    ) -> None:
        admissions = iter((True,) * 6 + (False,))
        harness = CoordinatorHarness(
            self.root,
            execution_snapshot_admitter=lambda: next(admissions),
        )

        reserved = harness.coordinator.reserve_ready()
        self.assertEqual(CoordinatorStatus.PROGRESSED, reserved.status)
        head = reserved.cursor.head

        result = harness.coordinator.dispatch_reserved(reserved.reserved[0])

        self.assertEqual(CoordinatorStatus.BLOCKED, result.status)
        self.assertEqual(
            CoordinatorReason.GRAPH_SNAPSHOT_NOT_ADMITTED,
            result.reason,
        )
        self.assertEqual((), result.events)
        self.assertEqual(head, result.cursor.head)
        self.assertFalse(harness.port.effects.exists())

    def test_graph_drift_after_request_writes_no_external_effect(self) -> None:
        admissions = iter((True,) * 8 + (False,))
        harness = CoordinatorHarness(
            self.root,
            execution_snapshot_admitter=lambda: next(admissions),
        )

        reserved = harness.coordinator.reserve_ready()
        self.assertEqual(CoordinatorStatus.PROGRESSED, reserved.status)

        result = harness.coordinator.dispatch_reserved(reserved.reserved[0])

        self.assertEqual(CoordinatorStatus.BLOCKED, result.status)
        self.assertEqual(
            CoordinatorReason.GRAPH_SNAPSHOT_NOT_ADMITTED,
            result.reason,
        )
        self.assertEqual(
            (JournalEventType.DISPATCH_REQUESTED,),
            tuple(event.event_type for event in result.events),
        )
        self.assertFalse(harness.port.effects.exists())

    def test_ready_set_dispatches_two_non_conflicting_siblings(self) -> None:
        harness = CoordinatorHarness(self.root, manifest=sibling_manifest())

        foundation = harness.coordinator.dispatch_task("TASK-001")
        self.assertEqual(CoordinatorStatus.PROGRESSED, foundation.status)
        completed = harness.coordinator.accept_worker_result(
            WorkerResultProposal(
                foundation.dispatched[0],
                "worker-foundation",
                True,
            )
        )
        self.assertEqual(CoordinatorStatus.PROGRESSED, completed.status)
        foundation_identity = ExecutionIdentity(
            harness.manifest.run_id,
            1,
            "TASK-001",
        )
        for event_type, from_state, to_state in (
            (
                JournalEventType.PR_OBSERVED,
                RuntimeState.DISPATCHED,
                RuntimeState.PR_OPEN,
            ),
            (
                JournalEventType.MERGE_OBSERVED,
                RuntimeState.PR_OPEN,
                RuntimeState.MERGED,
            ),
            (
                JournalEventType.TASK_VERIFIED,
                RuntimeState.MERGED,
                RuntimeState.VERIFIED,
            ),
        ):
            advanced = harness.coordinator._append_transition(
                event_type,
                foundation_identity,
                TransitionSubject.TASK,
                from_state,
                to_state,
            )
            self.assertIsNotNone(advanced.event)

        ready_siblings = ("TASK-002", "TASK-003")
        self.assertEqual(
            ready_siblings,
            harness.coordinator.cursor.graph_index.ready_tasks,
        )

        result = harness.coordinator.dispatch_ready()

        self.assertEqual(CoordinatorStatus.PROGRESSED, result.status)
        self.assertEqual(2, len(result.dispatched))
        self.assertEqual(
            ready_siblings,
            tuple(identity.task_id for identity in result.dispatched),
        )
        self.assertEqual((), result.cursor.graph_index.ready_tasks)

    def test_failed_worker_result_blocks_its_task_and_the_run(self) -> None:
        harness = CoordinatorHarness(self.root)
        dispatched = harness.coordinator.dispatch_ready()

        result = harness.coordinator.accept_worker_result(
            WorkerResultProposal(
                dispatched.dispatched[0],
                "worker-failed",
                False,
                RuntimeReasonCode.CHECK_FAILED,
            )
        )

        self.assertEqual(CoordinatorStatus.PROGRESSED, result.status)
        self.assertEqual(
            RuntimeState.FAILED,
            result.cursor.snapshot.attempts[0].state,
        )
        self.assertEqual(
            RuntimeReasonCode.CHECK_FAILED,
            result.cursor.snapshot.attempts[0].reason_code,
        )
        self.assertEqual(RuntimeState.BLOCKED, result.cursor.snapshot.tasks[0].state)
        self.assertEqual(RuntimeState.BLOCKED, result.cursor.snapshot.status)
        self.assertEqual(
            [
                JournalEventType.ATTEMPT_FAILED,
                JournalEventType.TASK_BLOCKED,
                JournalEventType.RUN_BLOCKED,
            ],
            [event.event_type for event in result.events],
        )

    def test_unknown_dispatch_observation_blocks_the_entire_run(self) -> None:
        def failpoint(point: str, path: Path) -> None:
            del path
            if point == "before_effect":
                raise OSError("effect store unavailable")

        port = FakeTaskPort(
            self.root / "effects",
            clock=lambda: "2026-08-19T00:00:10Z",
            failpoint=failpoint,
        )
        harness = CoordinatorHarness(self.root, port=port)

        result = harness.coordinator.dispatch_ready()

        self.assertEqual(CoordinatorStatus.BLOCKED, result.status)
        self.assertEqual(CoordinatorReason.EFFECT_OUTCOME_UNKNOWN, result.reason)
        self.assertEqual(EffectStatus.UNKNOWN, result.receipt.status)  # type: ignore[union-attr]
        self.assertEqual(RuntimeState.BLOCKED, result.cursor.snapshot.status)
        self.assertEqual(RuntimeState.BLOCKED, result.cursor.snapshot.tasks[0].state)
        self.assertEqual(
            RuntimeState.OUTCOME_UNKNOWN,
            result.cursor.snapshot.attempts[0].state,
        )
        self.assertEqual((), result.cursor.graph_index.ready_tasks)
        self.assertEqual(
            [
                JournalEventType.DISPATCH_OBSERVED,
                JournalEventType.ATTEMPT_OUTCOME_UNKNOWN,
                JournalEventType.TASK_BLOCKED,
                JournalEventType.RUN_BLOCKED,
            ],
            [event.event_type for event in result.events[-4:]],
        )
        recovered = recover_coordinator_lease(
            harness.storage.root,
            harness.manifest,
            coordinator_epoch=1,
            repair_derived=False,
        )
        self.assertEqual(LeaseRecoveryStatus.RECOVERED, recovered.status)
        self.assertEqual(RuntimeState.BLOCKED, recovered.replay.snapshot.status)
        self.assertEqual(
            RuntimeState.OUTCOME_UNKNOWN,
            recovered.replay.snapshot.attempts[0].state,
        )

    def test_recovery_retries_only_after_lookup_proves_effect_absent(self) -> None:
        crash_once = True

        def failpoint(point: str, path: Path) -> None:
            nonlocal crash_once
            del path
            if point == "before_effect" and crash_once:
                crash_once = False
                raise FakeEffectCrash("before_effect")

        port = FakeTaskPort(
            self.root / "effects",
            clock=lambda: "2026-08-19T00:00:10Z",
            failpoint=failpoint,
        )
        harness = CoordinatorHarness(self.root, port=port)
        with self.assertRaises(FakeEffectCrash):
            harness.coordinator.dispatch_ready()
        pending = harness.coordinator.cursor.snapshot.attempts[0]
        self.assertEqual(RuntimeState.DISPATCH_REQUESTED, pending.state)
        request_event = self._last_journal_event(harness)
        self.assertEqual(JournalEventType.DISPATCH_REQUESTED, request_event.event_type)

        result = harness.coordinator.reconcile_dispatch(request_event)

        self.assertEqual(CoordinatorStatus.PROGRESSED, result.status)
        self.assertEqual(EffectStatus.APPLIED, result.receipt.status)  # type: ignore[union-attr]
        self.assertEqual(1, len(tuple(port.effects.glob("*.json"))))

    def test_unknown_lookup_never_redispatches(self) -> None:
        apply_calls = 0

        def failpoint(point: str, path: Path) -> None:
            nonlocal apply_calls
            if point == "before_effect":
                apply_calls += 1
                if apply_calls == 1:
                    raise FakeEffectCrash("before_effect")
            del path

        port = FakeTaskPort(
            self.root / "effects",
            clock=lambda: "2026-08-19T00:00:10Z",
            failpoint=failpoint,
        )
        harness = CoordinatorHarness(self.root, port=port)
        with self.assertRaises(FakeEffectCrash):
            harness.coordinator.dispatch_ready()
        request_event = self._last_journal_event(harness)
        correlation = request_event.identity.correlation_id
        assert correlation is not None
        key = hashlib.sha256(correlation.encode("ascii")).hexdigest()
        port.receipts.mkdir(parents=True, exist_ok=True)
        (port.receipts / f"{key}.json").write_bytes(b"not-a-receipt")

        result = harness.coordinator.reconcile_dispatch(request_event)

        self.assertEqual(CoordinatorStatus.BLOCKED, result.status)
        self.assertEqual(CoordinatorReason.EFFECT_OUTCOME_UNKNOWN, result.reason)
        self.assertEqual(1, apply_calls)
        self.assertEqual(0, len(tuple(port.effects.glob("*.json"))))

    def test_expired_or_wrong_owner_lease_closes_admission_before_effect(self) -> None:
        expired = CoordinatorHarness(
            self.root / "expired",
            coordinator_clock=lambda: BASE_TIME + timedelta(seconds=291),
        )
        expired_result = expired.coordinator.dispatch_ready()
        self.assertEqual(CoordinatorStatus.BLOCKED, expired_result.status)
        self.assertEqual(CoordinatorReason.LEASE_NOT_ADMITTED, expired_result.reason)
        self.assertFalse(expired.port.effects.exists())

        valid = CoordinatorHarness(self.root / "owner")
        wrong_owner = replace(
            valid.owner,
            actor=replace(
                valid.owner.actor,
                process_id=9999,
                process_start_id="process-start-reused",
            ),
        )
        coordinator = ForegroundCoordinator(
            valid.manifest,
            valid.coordinator.cursor,
            valid.journal,
            valid.port,
            coordinator_id=COORDINATOR_ID,
            owner=wrong_owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
        )
        owner_result = coordinator.dispatch_ready()
        self.assertEqual(CoordinatorReason.LEASE_NOT_ADMITTED, owner_result.reason)
        self.assertFalse(valid.port.effects.exists())

    def test_stale_and_duplicate_worker_results_never_append(self) -> None:
        harness = CoordinatorHarness(self.root)
        dispatched = harness.coordinator.dispatch_ready()
        identity = dispatched.dispatched[0]
        head_before_stale = harness.coordinator.cursor.head
        stale_identity = ExecutionIdentity(
            identity.run_id,
            identity.coordinator_epoch + 1,
            identity.task_id,
            identity.attempt,
            identity.correlation_id,
        )
        stale = harness.coordinator.accept_worker_result(
            WorkerResultProposal(stale_identity, "worker-001", True)
        )
        self.assertEqual(CoordinatorStatus.REJECTED, stale.status)
        self.assertEqual(CoordinatorReason.STALE_RESULT, stale.reason)
        self.assertEqual(head_before_stale, harness.coordinator.cursor.head)

        accepted = harness.coordinator.accept_worker_result(
            WorkerResultProposal(identity, "worker-001", True)
        )
        self.assertEqual(CoordinatorStatus.PROGRESSED, accepted.status)
        head_after_success = harness.coordinator.cursor.head
        duplicate = harness.coordinator.accept_worker_result(
            WorkerResultProposal(identity, "worker-001", True)
        )
        self.assertEqual(CoordinatorStatus.REJECTED, duplicate.status)
        self.assertEqual(CoordinatorReason.DUPLICATE_RESULT, duplicate.reason)
        self.assertEqual(head_after_success, harness.coordinator.cursor.head)

    def test_manifest_digest_and_epoch_are_bound_at_construction_and_admission(
        self,
    ) -> None:
        harness = CoordinatorHarness(self.root)
        with self.assertRaisesRegex(ValueError, "snapshot epoch"):
            ForegroundCoordinator(
                harness.manifest,
                harness.coordinator.cursor,
                harness.journal,
                harness.port,
                coordinator_id=COORDINATOR_ID,
                owner=harness.owner,
                fencing_token=2,
                authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
            )

        lease = harness.coordinator.cursor.lease_state.lease
        assert lease is not None
        changed_lease_state = CoordinatorLeaseState(
            harness.coordinator.cursor.head,
            harness.coordinator.cursor.lease_state.event_type,
            replace(lease, manifest_digest=digest("9")),
            harness.coordinator.cursor.lease_state.max_fencing_token,
        )
        changed = ForegroundCoordinator(
            harness.manifest,
            CoordinatorCursor(
                harness.coordinator.cursor.snapshot,
                harness.coordinator.cursor.graph_index,
                changed_lease_state,
            ),
            harness.journal,
            harness.port,
            coordinator_id=COORDINATOR_ID,
            owner=harness.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
        )
        result = changed.dispatch_ready()
        self.assertEqual(CoordinatorReason.LEASE_NOT_ADMITTED, result.reason)
        self.assertFalse(harness.port.effects.exists())

    def test_verified_recovery_result_reconstructs_a_live_coordinator(self) -> None:
        harness = CoordinatorHarness(self.root)
        dispatched = harness.coordinator.dispatch_ready()
        self.assertEqual(CoordinatorStatus.PROGRESSED, dispatched.status)

        recovered = recover_coordinator_lease(
            harness.storage.root,
            harness.manifest,
            coordinator_epoch=1,
            repair_derived=False,
        )
        self.assertEqual(LeaseRecoveryStatus.RECOVERED, recovered.status)
        self.assertEqual((), recovered.pending_dispatch_requests)
        assert recovered.lease_state is not None
        restarted = ForegroundCoordinator(
            harness.manifest,
            CoordinatorCursor(
                recovered.replay.snapshot,
                recovered.replay.graph_index,
                recovered.lease_state,
            ),
            harness.journal,
            harness.port,
            coordinator_id=COORDINATOR_ID,
            owner=harness.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
        )

        accepted = restarted.accept_worker_result(
            WorkerResultProposal(dispatched.dispatched[0], "worker-001", True)
        )

        self.assertEqual(CoordinatorStatus.PROGRESSED, accepted.status)
        self.assertEqual(
            RuntimeState.SUCCEEDED,
            accepted.cursor.snapshot.attempts[0].state,
        )

    def test_scheduler_mode_is_fail_closed_even_for_a_forged_model(self) -> None:
        harness = CoordinatorHarness(self.root)
        forged = object.__new__(type(harness.manifest))
        for field in fields(harness.manifest):
            object.__setattr__(
                forged, field.name, getattr(harness.manifest, field.name)
            )
        object.__setattr__(forged, "scheduler_mode", "trellis")

        with self.assertRaisesRegex(ValueError, "scheduler_mode"):
            ForegroundCoordinator(
                forged,
                harness.coordinator.cursor,
                harness.journal,
                harness.port,
                coordinator_id=COORDINATOR_ID,
                owner=harness.owner,
                fencing_token=1,
                authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
            )

    def test_injected_clock_stops_after_request_and_ids_are_deterministic(self) -> None:
        harness = CoordinatorHarness(self.root)
        calls = 0

        def clock():
            nonlocal calls
            calls += 1
            if calls >= 7:
                return BASE_TIME + timedelta(seconds=291)
            return BASE_TIME + timedelta(seconds=10)

        coordinator = ForegroundCoordinator(
            harness.manifest,
            harness.coordinator.cursor,
            harness.journal,
            harness.port,
            coordinator_id=COORDINATOR_ID,
            owner=harness.owner,
            fencing_token=1,
            authority_clock=clock,
            event_id_factory=lambda event_type, sequence, identity: (
                f"EVENT-INJECTED-{sequence:04d}"
            ),
            correlation_id_factory=lambda task_id, attempt, epoch: (
                f"CORRELATION-INJECTED-{attempt:04d}"
            ),
        )

        result = coordinator.dispatch_ready()

        self.assertEqual(CoordinatorStatus.BLOCKED, result.status)
        self.assertEqual(CoordinatorReason.LEASE_NOT_ADMITTED, result.reason)
        self.assertEqual(
            JournalEventType.DISPATCH_REQUESTED,
            result.events[-1].event_type,
        )
        self.assertEqual(
            "EVENT-INJECTED-0011",
            result.events[-1].event_id,
        )
        self.assertEqual(
            "CORRELATION-INJECTED-0001",
            result.events[-1].identity.correlation_id,
        )
        self.assertFalse(harness.port.effects.exists())
        recovered = recover_coordinator_lease(
            harness.storage.root,
            harness.manifest,
            coordinator_epoch=1,
            repair_derived=False,
        )
        self.assertEqual(LeaseRecoveryStatus.RECOVERED, recovered.status)
        self.assertEqual((result.events[-1],), recovered.pending_dispatch_requests)

    def _last_journal_event(self, harness: CoordinatorHarness):
        from wish_builder.contracts.runtime_decoder import decode_journal_event_bytes

        segment = next((harness.storage.root / "segments").glob("*.jsonl"))
        raw = segment.read_bytes().splitlines(keepends=True)[-1]
        decoded = decode_journal_event_bytes(raw)
        self.assertTrue(decoded.ok, decoded.issues)
        assert decoded.value is not None
        return decoded.value


if __name__ == "__main__":
    unittest.main()
