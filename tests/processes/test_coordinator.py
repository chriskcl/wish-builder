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
from wish_builder.adapters import FilesystemExternalEvidenceStore
from wish_builder.adapters.fake import FakeEffectCrash, FakeTaskPort
from wish_builder.adapters.fakes import FakeBackendChannelPort
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.adapters.trellis import import_trellis_snapshot
from wish_builder.contracts import SchedulerMode
from wish_builder.contracts.runtime import (
    ActorIdentity,
    ActorType,
    AdapterKind,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionCommand,
    DecisionObservedPayload,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectRequestPayload,
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
from wish_builder.services.backend_effects import (
    BackendDispatchEffectService,
    BackendDispatchPlan,
)
from wish_builder.services.gate_b_bootstrap import (
    gate_b_artifact_nonce,
    graph_projection_bytes,
)
from wish_builder.services.recovery import (
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)
from wish_builder.services.ports import (
    BackendCapabilities,
    ReserveChannel,
    SendTaskPacket,
    TurnState,
)

BASE_TIME = datetime(2026, 8, 19, tzinfo=UTC)
COORDINATOR_ID = "coordinator-001"
GATE_B_ARTIFACT_HASH = "sha256:" + "e" * 64


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


def contract_evidence(
    manifest,
    evidence_digest: str,
    byte_length: int,
    external_object_id: str,
) -> EvidenceRef:
    return EvidenceRef(
        1,
        evidence_digest,
        byte_length,
        EvidenceType.CONTRACT,
        EvidenceProducer(
            ExecutionIdentity(manifest.run_id, 1),
            external_object_id=external_object_id,
        ),
        "2026-08-19T00:00:00Z",
        EvidenceSensitivity.INTERNAL,
        EvidenceRenderPolicy.METADATA_ONLY,
        EvidenceRole.REQUIRED,
        evidence_digest,
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
                        gate_b_artifact_nonce(GATE_B_ARTIFACT_HASH),
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
            evidence = ()
            if event_type is JournalEventType.TRELLIS_GRAPH_IMPORTED:
                evidence = (
                    contract_evidence(
                        self.manifest,
                        self.manifest.trellis_graph_digest,
                        len(graph_projection_bytes(self.manifest)),
                        "trellis-material-graph",
                    ),
                )
            elif event_type is JournalEventType.TASK_GRAPH_FROZEN:
                evidence = (
                    contract_evidence(
                        self.manifest,
                        GATE_B_ARTIFACT_HASH,
                        1024,
                        "gate-b-approved-artifact",
                    ),
                    contract_evidence(
                        self.manifest,
                        self.manifest.trellis_graph_digest,
                        len(graph_projection_bytes(self.manifest)),
                        "trellis-material-graph",
                    ),
                    contract_evidence(
                        self.manifest,
                        self.manifest.canonical_sha256(),
                        len(self.manifest.canonical_json_bytes()),
                        "execution-manifest-v2",
                    ),
                )
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
                        evidence,
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

    def test_admission_samples_lease_clock_after_slow_graph_check(self) -> None:
        authority_time = [BASE_TIME + timedelta(seconds=10)]

        def slow_graph_admission():
            authority_time[0] = BASE_TIME + timedelta(seconds=400)
            return True

        harness = CoordinatorHarness(
            self.root,
            coordinator_clock=lambda: authority_time[0],
            execution_snapshot_admitter=slow_graph_admission,
        )

        result = harness.coordinator.reserve_ready()

        self.assertIs(CoordinatorStatus.BLOCKED, result.status)
        self.assertIs(CoordinatorReason.LEASE_NOT_ADMITTED, result.reason)
        self.assertEqual((), result.events)

    def _takeover_with_reserved_attempt(
        self,
        root: Path,
    ) -> tuple[CoordinatorHarness, ForegroundCoordinator]:
        harness = CoordinatorHarness(root)
        reserved = harness.coordinator.reserve_ready()
        cursor = reserved.cursor
        lease = cursor.lease_state.lease
        self.assertIsNotNone(lease)
        assert lease is not None

        def append_lease(event_type, event_id, token):
            nonlocal cursor
            appended = harness.journal.append_draft(
                JournalEventDraft(
                    event_id,
                    event_type,
                    ExecutionIdentity(harness.manifest.run_id, token),
                    ActorType.COORDINATOR,
                    COORDINATOR_ID,
                    LeaseDraftPayload(
                        "LEASE-001" if token == 1 else "LEASE-002",
                        COORDINATOR_ID,
                        harness.owner,
                        SchedulerMode.WISH_BUILDER,
                        token,
                        harness.manifest.canonical_sha256(),
                        300,
                        10,
                    ),
                ),
                expected_head=cursor.head,
                lease_state=cursor.lease_state,
            )
            self.assertIs(AppendStatus.COMMITTED, appended.status)
            self.assertIsNotNone(appended.event)
            assert appended.event is not None
            applied = apply_journal_event(cursor.snapshot, appended.event)
            self.assertTrue(applied.accepted)
            cursor = CoordinatorCursor(
                applied.snapshot,
                cursor.graph_index.advance(cursor.snapshot, applied.snapshot),
                cursor.lease_state.advance(appended.event),
                cursor.dispatch_recoveries,
            )

        append_lease(
            JournalEventType.LEASE_RELEASED,
            "EVENT-LEASE-RELEASED-TAKEOVER",
            1,
        )
        append_lease(
            JournalEventType.LEASE_ACQUIRED,
            "EVENT-LEASE-ACQUIRED-TAKEOVER",
            2,
        )
        return harness, ForegroundCoordinator(
            harness.manifest,
            cursor,
            harness.journal,
            harness.port,
            coordinator_id=COORDINATOR_ID,
            owner=harness.owner,
            fencing_token=2,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
            execution_snapshot_admitter=lambda: True,
        )

    def _takeover_with_running_backend_attempt(
        self,
        root: Path,
        *,
        send_state: TurnState = TurnState.RUNNING,
    ):
        harness = CoordinatorHarness(root)
        capabilities = BackendCapabilities(
            provider=harness.manifest.provider,
            platform="windows",
            capability_digest=harness.manifest.capability_digest,
            launch_profile_digest=harness.manifest.launch_profile_digest,
            policy_digest=harness.manifest.policy_digest,
            max_task_packet_bytes=4096,
        )
        channel = FakeBackendChannelPort(capabilities, send_state=send_state)
        evidence = FilesystemExternalEvidenceStore(root / "backend-evidence")

        def plan_factory(identity: ExecutionIdentity) -> BackendDispatchPlan:
            assert identity.correlation_id is not None
            suffix = hashlib.sha256(
                repr(identity.to_primitive()).encode("utf-8")
            ).hexdigest()[:24].upper()
            packet = '{"task_id":"%s"}' % identity.task_id
            return BackendDispatchPlan(
                ReserveChannel(
                    f"RESERVE-{suffix}",
                    f"ATTEMPT-{suffix}",
                    identity.correlation_id,
                    f"CHANNEL-{suffix}",
                    harness.manifest.provider,
                    capabilities.capability_digest,
                    capabilities.launch_profile_digest,
                    capabilities.policy_digest,
                ),
                SendTaskPacket(
                    f"SEND-{suffix}",
                    f"ATTEMPT-{suffix}",
                    identity.correlation_id,
                    f"CHANNEL-{suffix}",
                    f"MESSAGE-{suffix}",
                    f"TURN-{suffix}",
                    packet,
                    "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest(),
                ),
            )

        old_effects = BackendDispatchEffectService(
            harness.journal,
            channel,
            evidence,
            coordinator_id=COORDINATOR_ID,
            fencing_token=1,
        )
        old = ForegroundCoordinator(
            harness.manifest,
            harness.coordinator.cursor,
            harness.journal,
            None,
            backend_effects=old_effects,
            backend_plan_factory=plan_factory,
            coordinator_id=COORDINATOR_ID,
            owner=harness.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
            execution_snapshot_admitter=lambda: True,
        )
        reserved = old.reserve_ready()
        dispatched = old.dispatch_reserved(reserved.reserved[0])
        request = next(
            event
            for event in dispatched.events
            if event.event_type is JournalEventType.DISPATCH_REQUESTED
        )
        observation = next(
            event
            for event in dispatched.events
            if event.event_type is JournalEventType.DISPATCH_OBSERVED
        )
        cursor = dispatched.cursor

        for event_type, event_id, token in (
            (JournalEventType.LEASE_RELEASED, "EVENT-LEASE-RELEASED-RUNNING", 1),
            (JournalEventType.LEASE_ACQUIRED, "EVENT-LEASE-ACQUIRED-RUNNING", 2),
        ):
            appended = harness.journal.append_draft(
                JournalEventDraft(
                    event_id,
                    event_type,
                    ExecutionIdentity(harness.manifest.run_id, token),
                    ActorType.COORDINATOR,
                    COORDINATOR_ID,
                    LeaseDraftPayload(
                        "LEASE-001" if token == 1 else "LEASE-002",
                        COORDINATOR_ID,
                        harness.owner,
                        SchedulerMode.WISH_BUILDER,
                        token,
                        harness.manifest.canonical_sha256(),
                        300,
                        10,
                    ),
                ),
                expected_head=cursor.head,
                lease_state=cursor.lease_state,
            )
            self.assertIs(AppendStatus.COMMITTED, appended.status)
            assert appended.event is not None
            applied = apply_journal_event(cursor.snapshot, appended.event)
            self.assertTrue(applied.accepted, applied.reason)
            cursor = CoordinatorCursor(
                applied.snapshot,
                cursor.graph_index.advance(cursor.snapshot, applied.snapshot),
                cursor.lease_state.advance(appended.event),
                cursor.dispatch_recoveries,
            )

        takeover_effects = BackendDispatchEffectService(
            harness.journal,
            channel,
            evidence,
            coordinator_id=COORDINATOR_ID,
            fencing_token=2,
        )
        takeover = ForegroundCoordinator(
            harness.manifest,
            cursor,
            harness.journal,
            None,
            backend_effects=takeover_effects,
            backend_plan_factory=plan_factory,
            coordinator_id=COORDINATOR_ID,
            owner=harness.owner,
            fencing_token=2,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
            execution_snapshot_admitter=lambda: True,
        )
        return takeover, request, observation, reserved.reserved[0]

    def test_takeover_cancels_and_refences_the_same_running_attempt(self) -> None:
        takeover, request, observation, old_identity = (
            self._takeover_with_running_backend_attempt(self.root / "running")
        )

        reclaimed = takeover.reclaim_cancelled_dispatch(
            request,
            observation,
            owned_path_changes=(),
        )

        self.assertIs(CoordinatorStatus.PROGRESSED, reclaimed.status)
        self.assertEqual(
            (
                JournalEventType.EFFECT_REQUESTED,
                JournalEventType.EFFECT_OBSERVED,
                JournalEventType.CANCEL_REQUESTED,
                JournalEventType.ATTEMPT_TERMINATED,
                JournalEventType.TASK_BLOCKED,
                JournalEventType.TASK_RETRY_SCHEDULED,
                JournalEventType.LEASE_ACQUIRED,
                JournalEventType.ATTEMPT_RESERVED,
            ),
            tuple(event.event_type for event in reclaimed.events),
        )
        self.assertEqual(1, len(reclaimed.cursor.snapshot.attempts))
        self.assertEqual(old_identity.attempt, reclaimed.reserved[0].attempt)
        self.assertEqual(2, reclaimed.reserved[0].coordinator_epoch)
        self.assertIs(
            RuntimeState.RESERVED,
            reclaimed.cursor.snapshot.attempts[0].state,
        )
        stale = takeover.accept_worker_result(
            WorkerResultProposal(old_identity, "old-worker", True)
        )
        self.assertIs(CoordinatorStatus.REJECTED, stale.status)
        self.assertIs(CoordinatorReason.STALE_RESULT, stale.reason)

        replayed = takeover.reclaim_cancelled_dispatch(
            request,
            observation,
            owned_path_changes=(),
        )
        self.assertIs(CoordinatorStatus.PROGRESSED, replayed.status)
        self.assertEqual(reclaimed.reserved, replayed.reserved)
        self.assertEqual((), replayed.events)

    def test_takeover_blocks_owned_changes_and_non_cancelled_provider_results(self) -> None:
        changed, request, observation, _ = self._takeover_with_running_backend_attempt(
            self.root / "owned-change"
        )
        rejected = changed.reclaim_cancelled_dispatch(
            request,
            observation,
            owned_path_changes=("src/owned.txt",),
        )
        self.assertIs(CoordinatorStatus.REJECTED, rejected.status)
        self.assertEqual((), rejected.events)

        completed, request, observation, _ = self._takeover_with_running_backend_attempt(
            self.root / "completed-provider",
            send_state=TurnState.DONE,
        )
        blocked = completed.reclaim_cancelled_dispatch(
            request,
            observation,
            owned_path_changes=(),
        )
        self.assertIs(CoordinatorStatus.BLOCKED, blocked.status)
        self.assertIs(CoordinatorReason.PORT_OUTCOME_INVALID, blocked.reason)
        self.assertEqual(
            (JournalEventType.EFFECT_REQUESTED, JournalEventType.EFFECT_OBSERVED),
            tuple(event.event_type for event in blocked.events),
        )

    def test_takeover_rejects_missing_or_mismatched_evidence(self) -> None:
        takeover, request, observation, _ = self._takeover_with_running_backend_attempt(
            self.root / "invalid-evidence"
        )
        unknown_paths = takeover.reclaim_cancelled_dispatch(
            request,
            observation,
            owned_path_changes=None,
        )
        mismatched = takeover.reclaim_cancelled_dispatch(
            request,
            request,
            owned_path_changes=(),
        )

        self.assertIs(CoordinatorStatus.REJECTED, unknown_paths.status)
        self.assertIs(CoordinatorStatus.REJECTED, mismatched.status)
        self.assertEqual((), unknown_paths.events)
        self.assertEqual((), mismatched.events)

    def test_takeover_reclaims_reserved_attempt_without_incrementing_attempt(self) -> None:
        _, takeover = self._takeover_with_reserved_attempt(self.root)

        reclaimed = takeover.reclaim_stale_reservations()

        self.assertIs(CoordinatorStatus.PROGRESSED, reclaimed.status)
        self.assertEqual(1, len(reclaimed.reserved))
        self.assertEqual(1, reclaimed.reserved[0].attempt)
        self.assertEqual(2, reclaimed.reserved[0].coordinator_epoch)
        self.assertEqual(
            (
                JournalEventType.ATTEMPT_RELEASED,
                JournalEventType.ATTEMPT_RESERVED,
            ),
            tuple(event.event_type for event in reclaimed.events),
        )
        self.assertEqual(1, len(reclaimed.cursor.snapshot.attempts))
        replayed = takeover.reserve_ready()
        self.assertEqual(reclaimed.reserved, replayed.reserved)
        self.assertEqual((), replayed.events)

    def test_takeover_resumes_after_crash_between_release_and_reservation(self) -> None:
        _, takeover = self._takeover_with_reserved_attempt(
            self.root / "partial-reclaim"
        )
        attempt = takeover.cursor.snapshot.attempts[0]
        released = takeover._append_transition(
            JournalEventType.ATTEMPT_RELEASED,
            ExecutionIdentity(
                takeover.cursor.snapshot.run_id,
                takeover.cursor.snapshot.coordinator_epoch,
                attempt.task_id,
                attempt.attempt,
                attempt.correlation_id,
            ),
            TransitionSubject.ATTEMPT,
            RuntimeState.RESERVED,
            RuntimeState.TERMINATED,
            reason_code=RuntimeReasonCode.LEASE_LOST,
            allow_recovery=True,
        )
        self.assertIsNotNone(released.event)

        reclaimed = takeover.reclaim_stale_reservations()

        self.assertIs(CoordinatorStatus.PROGRESSED, reclaimed.status)
        self.assertEqual((JournalEventType.ATTEMPT_RESERVED,), tuple(
            event.event_type for event in reclaimed.events
        ))
        self.assertEqual(1, len(reclaimed.reserved))
        self.assertEqual(1, reclaimed.reserved[0].attempt)
        self.assertEqual(2, reclaimed.reserved[0].coordinator_epoch)
        self.assertIs(
            RuntimeState.RESERVED,
            reclaimed.cursor.snapshot.attempts[0].state,
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

    def test_absent_preparation_retry_releases_attempt_and_reopens_run(self) -> None:
        harness = CoordinatorHarness(self.root)
        request, observation = self._block_absent_preparation(harness)

        retried = harness.coordinator.retry_absent_preparation(
            request,
            observation,
        )

        self.assertEqual(CoordinatorStatus.PROGRESSED, retried.status)
        self.assertEqual(
            [
                JournalEventType.ATTEMPT_RELEASED,
                JournalEventType.TASK_RETRY_SCHEDULED,
                JournalEventType.RUN_RESUMED,
            ],
            [event.event_type for event in retried.events],
        )
        self.assertEqual(RuntimeState.TERMINATED, retried.cursor.snapshot.attempts[0].state)
        self.assertEqual(RuntimeState.READY, retried.cursor.snapshot.tasks[0].state)
        self.assertEqual(RuntimeState.RUNNING, retried.cursor.snapshot.status)

        idempotent = harness.coordinator.retry_absent_preparation(
            request,
            observation,
        )
        self.assertEqual(CoordinatorStatus.PROGRESSED, idempotent.status)
        self.assertEqual((), idempotent.events)
        next_attempt = harness.coordinator.reserve_ready()
        self.assertEqual(CoordinatorStatus.PROGRESSED, next_attempt.status)
        self.assertEqual(2, next_attempt.reserved[0].attempt)

    def test_absent_preparation_retry_continues_crash_prefixes(self) -> None:
        for prefix in ("released", "retried"):
            with self.subTest(prefix=prefix):
                harness = CoordinatorHarness(self.root / prefix)
                request, observation = self._block_absent_preparation(harness)
                identity = request.identity
                released = harness.coordinator._append_transition(
                    JournalEventType.ATTEMPT_RELEASED,
                    ExecutionIdentity(
                        identity.run_id,
                        identity.coordinator_epoch,
                        identity.task_id,
                        identity.attempt,
                        identity.correlation_id,
                    ),
                    TransitionSubject.ATTEMPT,
                    RuntimeState.RESERVED,
                    RuntimeState.TERMINATED,
                    allow_recovery=True,
                )
                self.assertIsNotNone(released.event)
                if prefix == "retried":
                    task = harness.coordinator._append_transition(
                        JournalEventType.TASK_RETRY_SCHEDULED,
                        ExecutionIdentity(
                            identity.run_id,
                            identity.coordinator_epoch,
                            identity.task_id,
                        ),
                        TransitionSubject.TASK,
                        RuntimeState.BLOCKED,
                        RuntimeState.READY,
                        allow_recovery=True,
                    )
                    self.assertIsNotNone(task.event)

                resumed = harness.coordinator.retry_absent_preparation(
                    request,
                    observation,
                )

                expected = (
                    [JournalEventType.RUN_RESUMED]
                    if prefix == "retried"
                    else [
                        JournalEventType.TASK_RETRY_SCHEDULED,
                        JournalEventType.RUN_RESUMED,
                    ]
                )
                self.assertEqual(expected, [event.event_type for event in resumed.events])

    def test_absent_preparation_retry_rejects_wrong_observation_event(self) -> None:
        harness = CoordinatorHarness(self.root)
        request, _observation = self._block_absent_preparation(harness)

        result = harness.coordinator.retry_absent_preparation(request, request)

        self.assertEqual(CoordinatorStatus.REJECTED, result.status)
        self.assertEqual(CoordinatorReason.RECOVERY_PROOF_INVALID, result.reason)
        self.assertEqual((), result.events)

    def test_absent_preparation_retry_rejects_exhausted_attempt_budget(self) -> None:
        manifest = one_task_manifest()
        manifest = replace(
            manifest,
            execution_budget=replace(
                manifest.execution_budget,
                max_attempts_per_task=1,
                max_attempts_per_run=1,
            ),
        )
        harness = CoordinatorHarness(self.root, manifest=manifest)
        request, observation = self._block_absent_preparation(harness)

        result = harness.coordinator.retry_absent_preparation(
            request,
            observation,
        )

        self.assertEqual(CoordinatorStatus.REJECTED, result.status)
        self.assertEqual(CoordinatorReason.RECOVERY_PROOF_INVALID, result.reason)
        self.assertEqual((), result.events)

        released = harness.coordinator._append_transition(
            JournalEventType.ATTEMPT_RELEASED,
            request.identity,
            TransitionSubject.ATTEMPT,
            RuntimeState.RESERVED,
            RuntimeState.TERMINATED,
            allow_recovery=True,
        )
        self.assertIsNotNone(released.event)

        resumed = harness.coordinator.retry_absent_preparation(
            request,
            observation,
        )

        self.assertEqual(CoordinatorStatus.REJECTED, resumed.status)
        self.assertEqual(CoordinatorReason.RECOVERY_PROOF_INVALID, resumed.reason)
        self.assertEqual((), resumed.events)

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

    def _block_absent_preparation(self, harness: CoordinatorHarness):
        reserved = harness.coordinator.reserve_ready(limit=1)
        self.assertEqual(CoordinatorStatus.PROGRESSED, reserved.status)
        identity = reserved.reserved[0]
        request = harness.coordinator._append_payload(
            JournalEventType.EFFECT_REQUESTED,
            identity,
            EffectRequestPayload(
                EffectOperation.REPOSITORY_UPDATE,
                AdapterKind.GIT,
                EffectObjectType.WORKTREE,
                digest("7"),
                digest("8"),
                harness.coordinator.cursor.head.sequence,
                identity.coordinator_epoch,
            ),
        )
        self.assertIsNotNone(request.event)
        receipt = EffectReceipt(
            1,
            identity,
            EffectOperation.REPOSITORY_UPDATE,
            EffectStatus.ABSENT,
            "2026-08-19T00:00:10Z",
        )
        observed = harness.coordinator._append_payload(
            JournalEventType.EFFECT_OBSERVED,
            identity,
            EffectObservationPayload(AdapterKind.GIT, receipt),
            actor_type=ActorType.ADAPTER,
            actor_id="git-worktree-adapter",
        )
        self.assertIsNotNone(observed.event)
        task = harness.coordinator._append_transition(
            JournalEventType.TASK_BLOCKED,
            ExecutionIdentity(identity.run_id, identity.coordinator_epoch, identity.task_id),
            TransitionSubject.TASK,
            RuntimeState.LEASED,
            RuntimeState.BLOCKED,
            reason_code=RuntimeReasonCode.GIT_STATE_CONFLICT,
        )
        self.assertIsNotNone(task.event)
        run = harness.coordinator._append_transition(
            JournalEventType.RUN_BLOCKED,
            ExecutionIdentity(identity.run_id, identity.coordinator_epoch),
            TransitionSubject.RUN,
            RuntimeState.RUNNING,
            RuntimeState.BLOCKED,
            reason_code=RuntimeReasonCode.GIT_STATE_CONFLICT,
        )
        self.assertIsNotNone(run.event)
        assert request.event is not None and observed.event is not None
        return request.event, observed.event


if __name__ == "__main__":
    unittest.main()
