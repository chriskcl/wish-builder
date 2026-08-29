from __future__ import annotations

import dataclasses
import hashlib
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from tests.services.test_attempts import graph_index
from tests.services.test_decisions import (
    RUN_ID,
    decision,
    decision_request,
    seed_request,
)
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    ActorType,
    AdapterKind,
    DecisionAdmissionReason,
    DecisionChoice,
    DecisionEvaluation,
    EffectObjectType,
    EffectOperation,
    EffectReceipt,
    EffectRequestPayload,
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
    MAX_EVIDENCE_REFS,
    RuntimeReasonCode,
    WorkerProvider,
    canonical_json_bytes,
)
from wish_builder.kernel.gates import evaluate_decision
from wish_builder.services.cleanup import (
    CleanupBoundaryError,
    CleanupCandidate,
    CleanupCommand,
    CleanupDisposition,
    CleanupInspection,
    CleanupObservation,
    CleanupPlan,
    CleanupReport,
    CleanupService,
)
from wish_builder.services.decisions import DecisionCommitResult, commit_decision
from wish_builder.services.journal import (
    AppendResult,
    AppendStatus,
    DurableJournal,
    JournalFaultCode,
    JournalHead,
)
from wish_builder.services.ports import PreparedEffect
from wish_builder.services.ports import trellis as trellis_contracts
from wish_builder.services.ports.backend import (
    ChannelObservation,
    ReserveChannel,
    SendTaskPacket,
    TurnObservation,
    TurnState,
    BackendCapabilities as BackendCapabilities,
)
from wish_builder.services.ports.trellis import (
    AttemptObservation,
    CheckObservation,
    FinishObservation,
    TrellisGraphSnapshot,
    TrellisLifecycleState,
)
from wish_builder.services.promotion import (
    PromotionBoundaryError,
    PromotionCommand,
    PromotionDisposition,
    PromotionObservation,
    PromotionPlan,
    PromotionRecord,
    PromotionService,
)

NOW = "2026-08-19T05:00:00Z"
ZERO_HASH = "sha256:" + "0" * 64
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
GIT_A = "1" * 40
GIT_B = "2" * 40
GIT_C = "3" * 40
GIT_D = "4" * 40


def evidence_ref(ordinal: int = 1) -> EvidenceRef:
    raw = f"service-edge-evidence-{ordinal}".encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    identity = ExecutionIdentity(
        RUN_ID,
        1,
        "TASK-001",
        1,
        f"EVIDENCE-{ordinal:03d}",
    )
    return EvidenceRef(
        1,
        digest,
        len(raw),
        EvidenceType.GIT,
        EvidenceProducer(identity, external_object_id=f"artifact-{ordinal:03d}"),
        NOW,
        EvidenceSensitivity.INTERNAL,
        EvidenceRenderPolicy.METADATA_ONLY,
        EvidenceRole.REQUIRED,
        digest,
    )


def effect_receipt(
    operation: EffectOperation,
    status: EffectStatus,
    *,
    identity: ExecutionIdentity | None = None,
    evidence: tuple[EvidenceRef, ...] = (),
) -> EffectReceipt:
    return EffectReceipt(
        1,
        identity or ExecutionIdentity(RUN_ID, 1, "TASK-001", 1, "OPERATION-001"),
        operation,
        status,
        NOW,
        effect_hash=SHA_A if status is EffectStatus.APPLIED else None,
        external_object_id="artifact-001" if status is EffectStatus.APPLIED else None,
        evidence=evidence,
    )


def prepared_effect(
    command: CleanupCommand | PromotionCommand,
    *,
    event_type: JournalEventType,
    operation: EffectOperation,
    adapter: AdapterKind,
    object_type: EffectObjectType,
    target_hash: str,
    identity: ExecutionIdentity | None = None,
) -> PreparedEffect:
    command_hash = (
        "sha256:"
        + hashlib.sha256(canonical_json_bytes(command.to_primitive())).hexdigest()
    )
    request_identity = identity or ExecutionIdentity(
        command.run_id,
        command.coordinator_epoch,
        command.task_id,
        command.attempt,
        command.operation_id,
    )
    event = JournalEvent.create(
        sequence=1,
        event_id=f"EVENT-{command.operation_id}",
        event_type=event_type,
        identity=request_identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at=NOW,
        previous_event_hash=ZERO_HASH,
        payload=EffectRequestPayload(
            operation,
            adapter,
            object_type,
            target_hash,
            command_hash,
            0,
            request_identity.coordinator_epoch,
        ),
    )
    appended = AppendResult(
        AppendStatus.COMMITTED,
        JournalHead(event.sequence, event.event_hash),
        event,
    )
    return PreparedEffect.from_append_result(appended, command)


@dataclass(frozen=True)
class AttemptStub:
    run_id: str = RUN_ID
    task_id: str = "TASK-001"
    attempt_number: int = 1
    local_repository_id: str = SHA_A
    external_object_id: str = "attempt-001"
    path: str = "C:/attempts/attempt-001"


@dataclass(frozen=True)
class StagedStub:
    run_id: str = RUN_ID
    task_id: str = "TASK-001"
    attempt: int = 1
    staged_ref: str = "refs/wish-builder/staged/TASK-001"
    result_commit_sha: str = GIT_B
    result_tree_sha: str = GIT_C
    result_manifest_hash: str = SHA_B
    local_repository_id: str = SHA_A


def cleanup_candidate(**changes: object) -> CleanupCandidate:
    values: dict[str, object] = {
        "attempt": AttemptStub(),
        "expected_head_sha": GIT_A,
        "evidence": (evidence_ref(),),
        "reconciliation_complete": True,
        "process_tree_terminated": True,
        "outcome_known": True,
    }
    values.update(changes)
    return CleanupCandidate(**values)  # type: ignore[arg-type]


def cleanup_inspection(**changes: object) -> CleanupInspection:
    values: dict[str, object] = {
        "exists": True,
        "identity_ok": True,
        "clean": True,
        "observed_head_sha": GIT_A,
        "target_workspace_hash": SHA_B,
        "state_hash": SHA_C,
        "details": (),
    }
    values.update(changes)
    return CleanupInspection(**values)  # type: ignore[arg-type]


def cleanup_command(
    *,
    candidate: CleanupCandidate | None = None,
    inspection: CleanupInspection | None = None,
    remove_allowed: bool = True,
    operation_id: str = "CLEANUP-001",
) -> CleanupCommand:
    selected_candidate = candidate or cleanup_candidate()
    selected_inspection = inspection or cleanup_inspection()
    attempt = selected_candidate.attempt
    return CleanupCommand(
        operation_id,
        attempt.run_id,
        1,
        attempt.task_id,
        attempt.attempt_number,
        attempt.local_repository_id,
        selected_inspection.target_workspace_hash,
        attempt.external_object_id,
        selected_candidate.expected_head_sha,
        selected_inspection.state_hash,
        tuple(sorted(item.digest for item in selected_candidate.evidence)),
        remove_allowed,
    )


def cleanup_plan() -> CleanupPlan:
    candidate = cleanup_candidate()
    inspection = cleanup_inspection()
    return CleanupPlan(
        cleanup_command(candidate=candidate, inspection=inspection),
        candidate,
        inspection,
    )


def promotion_source(**changes: object) -> StagedStub:
    return dataclasses.replace(StagedStub(), **changes)


def promotion_command(
    *,
    source: StagedStub | None = None,
    operation_id: str = "PROMOTE-001",
) -> PromotionCommand:
    selected = source or promotion_source()
    return PromotionCommand(
        operation_id,
        selected.run_id,
        1,
        selected.task_id,
        selected.attempt,
        0,
        selected.local_repository_id,
        SHA_C,
        GIT_A,
        selected.staged_ref,
        selected.result_manifest_hash,
        selected.result_commit_sha,
        selected.result_tree_sha,
        GIT_C,
        GIT_D,
    )


def promotion_plan() -> PromotionPlan:
    source = promotion_source()
    return PromotionPlan(promotion_command(source=source), source)


def bound_promotion_plan() -> PromotionPlan:
    return PromotionService.bind_acceptance(
        promotion_plan(),
        (evidence_ref(),),
    )


def promotion_record(
    acceptance_evidence: tuple[EvidenceRef, ...] = (evidence_ref(),),
) -> PromotionRecord:
    return PromotionRecord(
        "TASK-001",
        0,
        GIT_A,
        GIT_C,
        GIT_D,
        GIT_B,
        SHA_B,
        acceptance_evidence,
    )


class CleanupRepository:
    def __init__(
        self,
        inspection: CleanupInspection | None = None,
        disposition: CleanupDisposition = CleanupDisposition.REMOVED,
    ) -> None:
        self.inspection = inspection or cleanup_inspection()
        self.disposition = disposition
        self.calls: list[str] = []

    def inspect_cleanup(self, candidate: CleanupCandidate) -> CleanupInspection:
        self.calls.append(f"inspect:{candidate.attempt.external_object_id}")
        return self.inspection

    def apply_cleanup(
        self,
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
    ) -> CleanupObservation:
        self.calls.append(f"apply:{plan.command.external_object_id}")
        status = {
            CleanupDisposition.REMOVED: EffectStatus.APPLIED,
            CleanupDisposition.ALREADY_ABSENT: EffectStatus.APPLIED,
            CleanupDisposition.QUARANTINED: EffectStatus.ABSENT,
            CleanupDisposition.UNKNOWN: EffectStatus.UNKNOWN,
        }[self.disposition]
        evidence = plan.candidate.evidence
        receipt = effect_receipt(
            EffectOperation.CLEANUP,
            status,
            identity=effect.request.identity,
            evidence=evidence,
        )
        reason = (
            None
            if self.disposition
            in {CleanupDisposition.REMOVED, CleanupDisposition.ALREADY_ABSENT}
            else RuntimeReasonCode.CLEANUP_INCOMPLETE
        )
        return CleanupObservation(
            receipt,
            self.disposition,
            plan.command.external_object_id,
            evidence,
            reason,
        )


class PromotionRepository:
    def __init__(
        self,
        plan: PromotionPlan | None = None,
        disposition: PromotionDisposition = PromotionDisposition.APPLIED,
    ) -> None:
        self.plan = plan or promotion_plan()
        self.disposition = disposition
        self.calls: list[str] = []

    def prepare_promotion(
        self,
        source: StagedStub,
        *,
        expected_target_sha: str,
        topological_position: int,
        operation_id: str,
        coordinator_epoch: int,
    ) -> PromotionPlan:
        self.calls.append(f"prepare:{source.task_id}")
        return self.plan

    def apply_promotion(
        self,
        effect: PreparedEffect[PromotionCommand],
        plan: PromotionPlan,
    ) -> PromotionObservation:
        self.calls.append(f"apply:{plan.command.task_id}")
        status = EffectStatus(self.disposition.value)
        evidence = (
            (*plan.command.acceptance_evidence, evidence_ref(2))
            if status is EffectStatus.UNKNOWN
            else plan.command.acceptance_evidence
        )
        receipt = effect_receipt(
            EffectOperation.RESULT_PROMOTION,
            status,
            identity=effect.request.identity,
            evidence=evidence,
        )
        return PromotionObservation(
            receipt,
            self.disposition,
            plan.candidate_record()
            if self.disposition is PromotionDisposition.APPLIED
            else None,
            None
            if self.disposition is PromotionDisposition.APPLIED
            else RuntimeReasonCode.GIT_STATE_CONFLICT,
        )

    def inspect_promotion(self, plan: PromotionPlan) -> PromotionObservation:
        self.calls.append(f"inspect:{plan.command.task_id}")
        identity = ExecutionIdentity(
            plan.command.run_id,
            plan.command.coordinator_epoch,
            plan.command.task_id,
            plan.command.attempt,
            plan.command.operation_id,
        )
        return self.apply_promotion(
            prepared_effect(
                plan.command,
                event_type=JournalEventType.PROMOTION_REQUESTED,
                operation=EffectOperation.RESULT_PROMOTION,
                adapter=AdapterKind.GIT,
                object_type=EffectObjectType.GIT_REF,
                target_hash=plan.command.target_workspace_hash,
                identity=identity,
            ),
            plan,
        )

    def materialize_promotion_candidate(self, plan: PromotionPlan):
        self.calls.append(f"materialize:{plan.command.task_id}")
        return nullcontext(Path("C:/attempts/promotion-candidate"))


class DecisionEdgeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"
        self.head = seed_request(self.root)
        self.evaluation = evaluate_decision(
            decision_request(),
            decision(DecisionChoice.APPROVE, "EDGE"),
            current_sequence=1,
            current_workspace_hash=SHA_B,
        )
        self.journal = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(self.root, RUN_ID),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_result_rejects_impossible_persistence_combinations(self) -> None:
        committed = commit_decision(
            self.evaluation,
            self.journal,
            expected_head=self.head,
            identity=ExecutionIdentity(RUN_ID, 1),
            event_id="EVENT-DECISION-EDGE",
        )
        assert committed.append_result is not None
        assert committed.event is not None
        rejected = DecisionEvaluation(False, DecisionAdmissionReason.CHANNEL_DENIED)
        conflict = AppendResult(AppendStatus.CONFLICT, committed.append_result.head)

        invalid = (
            ("evaluation", ("bad", None, None), TypeError),
            ("append", (rejected, "bad", None), TypeError),
            ("event", (rejected, None, "bad"), TypeError),
            ("unpersisted_event", (rejected, None, committed.event), ValueError),
            ("accepted_without_append", (self.evaluation, None, None), ValueError),
            ("append_without_event", (rejected, conflict, None), ValueError),
            (
                "admission_mismatch",
                (self.evaluation, conflict, committed.event),
                ValueError,
            ),
        )
        for name, arguments, error in invalid:
            with self.subTest(name=name), self.assertRaises(error):
                DecisionCommitResult(*arguments)  # type: ignore[arg-type]

        other = JournalEvent.create(
            sequence=committed.event.sequence,
            event_id="EVENT-DECISION-OTHER",
            event_type=committed.event.event_type,
            identity=committed.event.identity,
            actor_type=committed.event.actor_type,
            actor_id=committed.event.actor_id,
            recorded_at=committed.event.recorded_at,
            previous_event_hash=committed.event.previous_event_hash,
            payload=committed.event.payload,
        )
        with self.assertRaisesRegex(ValueError, "identify the decision event"):
            DecisionCommitResult(self.evaluation, committed.append_result, other)

    def test_commit_validates_inputs_and_fail_closed_results(self) -> None:
        arguments = {
            "evaluation": self.evaluation,
            "journal": self.journal,
            "expected_head": self.head,
            "identity": ExecutionIdentity(RUN_ID, 1),
            "event_id": "EVENT-DECISION-VALIDATE",
        }
        invalid = (
            ("evaluation", "bad"),
            ("journal", object()),
            ("expected_head", object()),
            ("identity", object()),
            ("identity", ExecutionIdentity(RUN_ID, 1, "TASK-001")),
            ("event_id", 1),
        )
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(TypeError):
                commit_decision(**(arguments | {field: value}))  # type: ignore[arg-type]

        malformed = object.__new__(DecisionEvaluation)
        object.__setattr__(malformed, "accepted", True)
        object.__setattr__(malformed, "reason", DecisionAdmissionReason.ACCEPTED)
        object.__setattr__(malformed, "observation", None)
        object.__setattr__(malformed, "idempotent", False)
        with self.assertRaisesRegex(ValueError, "carry its observation"):
            commit_decision(**(arguments | {"evaluation": malformed}))

        with self.assertRaisesRegex(ValueError, "immediately follow"):
            commit_decision(
                self.evaluation,
                self.journal,
                expected_head=JournalHead(0, ZERO_HASH),
                identity=ExecutionIdentity(RUN_ID, 1),
                event_id="EVENT-SEQUENCE-MISMATCH",
            )

        conflict = AppendResult(AppendStatus.CONFLICT, self.head)
        failed = AppendResult(
            AppendStatus.PERSISTENCE_FAILED,
            self.head,
            fault_code=JournalFaultCode.WRITE_FAILED,
        )
        for appended, reason in (
            (conflict, DecisionAdmissionReason.DECISION_CONFLICT),
            (failed, DecisionAdmissionReason.PERSISTENCE_FAILED),
        ):
            with mock.patch.object(DurableJournal, "append", return_value=appended):
                result = commit_decision(**arguments)
            self.assertFalse(result.durable)
            self.assertIs(result.evaluation.reason, reason)

        replay = dataclasses.replace(
            self.evaluation,
            reason=DecisionAdmissionReason.IDEMPOTENT_REPLAY,
            idempotent=True,
        )
        with self.assertRaisesRegex(RuntimeError, "cannot be newly committed"):
            commit_decision(**(arguments | {"evaluation": replay}))


class CleanupModelCoverageTests(unittest.TestCase):
    def test_candidate_and_inspection_guards(self) -> None:
        candidate = cleanup_candidate()
        candidate_cases = (
            {"attempt": object()},
            {"expected_head_sha": "bad"},
            {"evidence": [evidence_ref()]},
            {"evidence": (object(),)},
            {"evidence": (evidence_ref(), evidence_ref())},
            {"reconciliation_complete": 1},
            {"process_tree_terminated": 1},
            {"outcome_known": 1},
        )
        for changes in candidate_cases:
            with (
                self.subTest(candidate=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(candidate, **changes)

        inspection = cleanup_inspection()
        inspection_cases = (
            {"exists": 1},
            {"identity_ok": 1},
            {"clean": 1},
            {"observed_head_sha": "bad"},
            {"target_workspace_hash": "bad"},
            {"state_hash": "bad"},
            {"details": ["detail"]},
            {"details": (object(),)},
            {"exists": False, "observed_head_sha": GIT_A},
        )
        for changes in inspection_cases:
            with (
                self.subTest(inspection=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(inspection, **changes)

    def test_command_plan_observation_and_report_guards(self) -> None:
        candidate = cleanup_candidate()
        inspection = cleanup_inspection()
        command = cleanup_command(candidate=candidate, inspection=inspection)
        command_cases = (
            {"operation_id": ""},
            {"run_id": ""},
            {"task_id": ""},
            {"external_object_id": ""},
            {"coordinator_epoch": 0},
            {"coordinator_epoch": True},
            {"attempt": 0},
            {"local_repository_id": "bad"},
            {"target_workspace_hash": "bad"},
            {"expected_head_sha": "bad"},
            {"observed_state_hash": "bad"},
            {"evidence_digests": [evidence_ref().digest]},
            {"evidence_digests": ("bad",)},
            {"evidence_digests": (evidence_ref().digest,) * 2},
            {"remove_allowed": 1},
        )
        for changes in command_cases:
            with (
                self.subTest(command=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(command, **changes)
        self.assertEqual("cleanup", command.to_primitive()["operation"])

        plan = CleanupPlan(command, candidate, inspection)
        plan_cases = (
            {"command": object()},
            {"candidate": object()},
            {"inspection": object()},
            {"command": dataclasses.replace(command, task_id="TASK-002")},
            {"command": dataclasses.replace(command, remove_allowed=False)},
            {
                "command": dataclasses.replace(command, remove_allowed=False),
                "quarantine_reason": "bad",
            },
        )
        for changes in plan_cases:
            with self.subTest(plan=changes), self.assertRaises((TypeError, ValueError)):
                dataclasses.replace(plan, **changes)

        applied_receipt = effect_receipt(
            EffectOperation.CLEANUP,
            EffectStatus.APPLIED,
        )
        observation = CleanupObservation(
            applied_receipt,
            CleanupDisposition.REMOVED,
            "attempt-001",
            (evidence_ref(),),
        )
        wrong_operation = effect_receipt(
            EffectOperation.RESULT_PROMOTION,
            EffectStatus.APPLIED,
        )
        absent_receipt = effect_receipt(
            EffectOperation.CLEANUP,
            EffectStatus.ABSENT,
        )
        observation_cases = (
            {"receipt": object()},
            {"receipt": wrong_operation},
            {"disposition": "removed"},
            {"receipt": absent_receipt},
            {"external_object_id": ""},
            {"evidence": [evidence_ref()]},
            {"reason_code": RuntimeReasonCode.CLEANUP_INCOMPLETE},
            {
                "receipt": absent_receipt,
                "disposition": CleanupDisposition.QUARANTINED,
            },
            {"details": ["detail"]},
        )
        for changes in observation_cases:
            with (
                self.subTest(observation=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(observation, **changes)

        report = CleanupReport((observation,), (), False, 1024)
        report_cases = (
            {"observations": [observation]},
            {"observations": (object(),)},
            {"retained_object_ids": []},
            {"dispatch_blocked": 1},
            {"available_bytes": -1},
            {"available_bytes": True},
        )
        for changes in report_cases:
            with (
                self.subTest(report=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(report, **changes)

    def test_cleanup_reason_precedence_and_service_constructor(self) -> None:
        invalid_services = (
            (object(), lambda: 1, 0, lambda: NOW),
            (CleanupRepository(), 1, 0, lambda: NOW),
            (CleanupRepository(), lambda: 1, -1, lambda: NOW),
            (CleanupRepository(), lambda: 1, True, lambda: NOW),
            (CleanupRepository(), lambda: 1, 0, 1),
        )
        for arguments in invalid_services:
            with (
                self.subTest(arguments=arguments),
                self.assertRaises((TypeError, ValueError)),
            ):
                CleanupService(
                    arguments[0],  # type: ignore[arg-type]
                    available_bytes=arguments[1],  # type: ignore[arg-type]
                    minimum_free_bytes=arguments[2],  # type: ignore[arg-type]
                    clock=arguments[3],  # type: ignore[arg-type]
                )

        cases = (
            (
                cleanup_candidate(evidence=()),
                cleanup_inspection(),
                RuntimeReasonCode.EVIDENCE_MISSING,
            ),
            (
                cleanup_candidate(reconciliation_complete=False),
                cleanup_inspection(),
                RuntimeReasonCode.WORKER_OUTCOME_UNKNOWN,
            ),
            (
                cleanup_candidate(outcome_known=False),
                cleanup_inspection(),
                RuntimeReasonCode.WORKER_OUTCOME_UNKNOWN,
            ),
            (
                cleanup_candidate(process_tree_terminated=False),
                cleanup_inspection(),
                RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
            ),
            (
                cleanup_candidate(),
                cleanup_inspection(identity_ok=False),
                RuntimeReasonCode.CLEANUP_INCOMPLETE,
            ),
            (
                cleanup_candidate(),
                cleanup_inspection(clean=False),
                RuntimeReasonCode.GIT_STATE_CONFLICT,
            ),
            (
                cleanup_candidate(),
                cleanup_inspection(observed_head_sha=GIT_B),
                RuntimeReasonCode.GIT_STATE_CONFLICT,
            ),
        )
        for candidate, inspection, expected in cases:
            repository = CleanupRepository(inspection)
            service = CleanupService(
                repository,
                available_bytes=lambda: 1024,
                minimum_free_bytes=0,
            )
            plan = service.plan(
                candidate, operation_id="CLEANUP-EDGE", coordinator_epoch=1
            )
            self.assertIs(plan.quarantine_reason, expected)
            effect = prepared_effect(
                plan.command,
                event_type=JournalEventType.CLEANUP_REQUESTED,
                operation=EffectOperation.CLEANUP,
                adapter=AdapterKind.GIT,
                object_type=EffectObjectType.CLEANUP_ITEM,
                target_hash=plan.command.target_workspace_hash,
            )
            observation = service.apply(effect, plan)
            self.assertIs(observation.disposition, CleanupDisposition.QUARANTINED)


class CleanupServiceCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = cleanup_plan()
        self.effect = prepared_effect(
            self.plan.command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            adapter=AdapterKind.GIT,
            object_type=EffectObjectType.CLEANUP_ITEM,
            target_hash=self.plan.command.target_workspace_hash,
        )

    def service(
        self,
        disposition: CleanupDisposition = CleanupDisposition.REMOVED,
        *,
        available: object = 1024,
    ) -> CleanupService:
        return CleanupService(
            CleanupRepository(disposition=disposition),
            available_bytes=lambda: available,  # type: ignore[return-value]
            minimum_free_bytes=512,
            clock=lambda: NOW,
        )

    def test_apply_many_stops_after_unknown_and_checks_storage(self) -> None:
        unknown = self.service(CleanupDisposition.UNKNOWN)
        second_plan = cleanup_plan()
        second_command = dataclasses.replace(
            second_plan.command,
            operation_id="CLEANUP-002",
            external_object_id="attempt-002",
        )
        second_attempt = dataclasses.replace(
            second_plan.candidate.attempt,
            external_object_id="attempt-002",
        )
        second_candidate = dataclasses.replace(
            second_plan.candidate, attempt=second_attempt
        )
        second_plan = CleanupPlan(
            second_command, second_candidate, second_plan.inspection
        )
        second_effect = prepared_effect(
            second_command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            adapter=AdapterKind.GIT,
            object_type=EffectObjectType.CLEANUP_ITEM,
            target_hash=second_command.target_workspace_hash,
        )
        report = unknown.apply_many(
            ((second_effect, second_plan), (self.effect, self.plan))
        )
        self.assertTrue(report.dispatch_blocked)
        self.assertEqual(("attempt-001", "attempt-002"), report.retained_object_ids)
        self.assertTrue(unknown.blocked_on_unknown)
        with self.assertRaisesRegex(CleanupBoundaryError, "cleanup_outcome_unknown"):
            unknown.apply(second_effect, second_plan)

        invalid = self.service(available="unknown")
        with self.assertRaisesRegex(
            CleanupBoundaryError, "invalid_storage_observation"
        ):
            invalid.apply_many(())

    def test_apply_many_and_validation_reject_malformed_boundaries(self) -> None:
        service = self.service()
        for operations in ([()], ((self.effect,),), ((object(), self.plan),)):
            with self.subTest(operations=operations), self.assertRaises(TypeError):
                service.apply_many(operations)  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            service.plan(object(), operation_id="CLEANUP", coordinator_epoch=1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            service.apply(object(), self.plan)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            service.apply(self.effect, object())  # type: ignore[arg-type]

        other_command = dataclasses.replace(
            self.plan.command, operation_id="CLEANUP-OTHER"
        )
        other_effect = prepared_effect(
            other_command,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            operation=EffectOperation.CLEANUP,
            adapter=AdapterKind.GIT,
            object_type=EffectObjectType.CLEANUP_ITEM,
            target_hash=other_command.target_workspace_hash,
        )
        with self.assertRaisesRegex(ValueError, "does not bind"):
            service.apply(other_effect, self.plan)

        boundary_cases = (
            {
                "event_type": JournalEventType.EFFECT_REQUESTED,
                "operation": EffectOperation.CLEANUP,
                "adapter": AdapterKind.GIT,
                "object_type": EffectObjectType.CLEANUP_ITEM,
                "target_hash": self.plan.command.target_workspace_hash,
            },
            {
                "event_type": JournalEventType.CLEANUP_REQUESTED,
                "operation": EffectOperation.TASK_EXECUTION,
                "adapter": AdapterKind.TASK,
                "object_type": EffectObjectType.WORKER,
                "target_hash": self.plan.command.target_workspace_hash,
            },
            {
                "event_type": JournalEventType.CLEANUP_REQUESTED,
                "operation": EffectOperation.CLEANUP,
                "adapter": AdapterKind.GIT,
                "object_type": EffectObjectType.CLEANUP_ITEM,
                "target_hash": SHA_A,
            },
            {
                "event_type": JournalEventType.CLEANUP_REQUESTED,
                "operation": EffectOperation.CLEANUP,
                "adapter": AdapterKind.GIT,
                "object_type": EffectObjectType.CLEANUP_ITEM,
                "target_hash": self.plan.command.target_workspace_hash,
                "identity": ExecutionIdentity(
                    "WISH-OTHER",
                    1,
                    self.plan.command.task_id,
                    self.plan.command.attempt,
                    self.plan.command.operation_id,
                ),
            },
        )
        for values in boundary_cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                service.apply(prepared_effect(self.plan.command, **values), self.plan)


class PromotionModelCoverageTests(unittest.TestCase):
    def test_command_plan_record_and_observation_guards(self) -> None:
        source = promotion_source()
        command = promotion_command(source=source)
        command_cases = (
            {"operation_id": ""},
            {"run_id": ""},
            {"task_id": ""},
            {"staged_ref": ""},
            {"coordinator_epoch": 0},
            {"attempt": 0},
            {"topological_position": -1},
            {"local_repository_id": "bad"},
            {"target_workspace_hash": "bad"},
            {"result_manifest_hash": "bad"},
            {"expected_target_sha": "bad"},
            {"source_commit_sha": "bad"},
            {"source_tree_sha": "bad"},
            {"candidate_commit_sha": "bad"},
            {"candidate_tree_sha": "bad"},
        )
        for changes in command_cases:
            with self.subTest(command=changes), self.assertRaises(ValueError):
                dataclasses.replace(command, **changes)
        with self.assertRaises(TypeError):
            dataclasses.replace(command, acceptance_evidence=[])
        with self.assertRaisesRegex(ValueError, "repeat"):
            dataclasses.replace(
                command,
                acceptance_evidence=(evidence_ref(), evidence_ref()),
            )
        with self.assertRaisesRegex(ValueError, "promotion-safe maximum"):
            dataclasses.replace(
                command,
                acceptance_evidence=tuple(
                    evidence_ref(ordinal)
                    for ordinal in range(100, 100 + MAX_EVIDENCE_REFS)
                ),
            )
        wrong_evidence = dataclasses.replace(
            evidence_ref(),
            producer=EvidenceProducer(
                ExecutionIdentity(RUN_ID, 1, "TASK-002", 1, "EVIDENCE-WRONG"),
                external_object_id="artifact-wrong",
            ),
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            dataclasses.replace(command, acceptance_evidence=(wrong_evidence,))
        self.assertEqual("result_promotion", command.to_primitive()["operation"])

        plan = PromotionPlan(command, source)
        for changes in (
            {"command": object()},
            {"source": object()},
            {"command": dataclasses.replace(command, task_id="TASK-002")},
        ):
            with self.subTest(plan=changes), self.assertRaises((TypeError, ValueError)):
                dataclasses.replace(plan, **changes)

        record = promotion_record()
        record_cases = (
            {"task_id": ""},
            {"topological_position": -1},
            {"previous_target_sha": "bad"},
            {"promoted_commit_sha": "bad"},
            {"promoted_tree_sha": "bad"},
            {"source_commit_sha": "bad"},
            {"result_manifest_hash": "bad"},
        )
        for changes in record_cases:
            with self.subTest(record=changes), self.assertRaises(ValueError):
                dataclasses.replace(record, **changes)
        with self.assertRaises(TypeError):
            dataclasses.replace(record, acceptance_evidence=[])
        with self.assertRaisesRegex(ValueError, "repeat"):
            dataclasses.replace(
                record,
                acceptance_evidence=(evidence_ref(), evidence_ref()),
            )
        with self.assertRaisesRegex(ValueError, "promotion-safe maximum"):
            dataclasses.replace(
                record,
                acceptance_evidence=tuple(
                    evidence_ref(ordinal)
                    for ordinal in range(200, 200 + MAX_EVIDENCE_REFS)
                ),
            )
        with self.assertRaisesRegex(ValueError, "identity"):
            dataclasses.replace(record, acceptance_evidence=(wrong_evidence,))
        self.assertEqual("TASK-001", record.to_primitive()["task_id"])

        receipt = effect_receipt(
            EffectOperation.RESULT_PROMOTION,
            EffectStatus.APPLIED,
            evidence=record.acceptance_evidence,
        )
        observation = PromotionObservation(
            receipt,
            PromotionDisposition.APPLIED,
            record,
        )
        absent = effect_receipt(
            EffectOperation.RESULT_PROMOTION,
            EffectStatus.ABSENT,
        )
        wrong_operation = effect_receipt(EffectOperation.CLEANUP, EffectStatus.APPLIED)
        observation_cases = (
            {"receipt": object()},
            {"receipt": wrong_operation},
            {"disposition": "applied"},
            {"receipt": absent},
            {"record": None},
            {"reason_code": RuntimeReasonCode.GIT_STATE_CONFLICT},
            {
                "receipt": effect_receipt(
                    EffectOperation.RESULT_PROMOTION,
                    EffectStatus.APPLIED,
                )
            },
            {
                "receipt": absent,
                "disposition": PromotionDisposition.ABSENT,
                "record": record,
                "reason_code": RuntimeReasonCode.GIT_STATE_CONFLICT,
            },
            {
                "receipt": absent,
                "disposition": PromotionDisposition.ABSENT,
                "record": None,
                "reason_code": None,
            },
            {"details": ["detail"]},
        )
        for changes in observation_cases:
            with (
                self.subTest(observation=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(observation, **changes)


class PromotionServiceCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.unbound_plan = promotion_plan()
        self.plan = PromotionService.bind_acceptance(
            self.unbound_plan,
            (evidence_ref(),),
        )
        self.index = graph_index()
        self.effect = prepared_effect(
            self.plan.command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            adapter=AdapterKind.GIT,
            object_type=EffectObjectType.GIT_REF,
            target_hash=self.plan.command.target_workspace_hash,
        )
        self.assertEqual(
            self.effect.request.payload.request_payload_hash,
            self.effect.command_hash,
        )
        self.assertNotEqual(
            canonical_json_bytes(self.unbound_plan.command.to_primitive()),
            canonical_json_bytes(self.plan.command.to_primitive()),
        )

    def test_constructor_ordering_and_selection_fail_closed(self) -> None:
        with self.assertRaises(TypeError):
            PromotionService(object(), self.index)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            PromotionService(PromotionRepository(), object())  # type: ignore[arg-type]

        service = PromotionService(PromotionRepository(), self.index)
        self.assertFalse(service.blocked_on_unknown)
        invalid_sources = (
            [promotion_source()],
            (object(),),
        )
        for sources in invalid_sources:
            with self.subTest(sources=sources), self.assertRaises(TypeError):
                service.canonical_order(sources)  # type: ignore[arg-type]
        with self.assertRaisesRegex(PromotionBoundaryError, "duplicate_staged_task"):
            service.canonical_order((promotion_source(), promotion_source()))
        with self.assertRaisesRegex(PromotionBoundaryError, "unknown_graph_task"):
            service.canonical_order((promotion_source(task_id="TASK-999"),))
        with self.assertRaisesRegex(PromotionBoundaryError, "run_identity_mismatch"):
            service.canonical_order((promotion_source(run_id="WISH-OTHER"),))
        with self.assertRaisesRegex(PromotionBoundaryError, "no_staged_result"):
            service.plan_next(
                (),
                expected_target_sha=GIT_A,
                operation_id="PROMOTE-NONE",
                coordinator_epoch=1,
            )

        ordered = service.canonical_order(
            (
                promotion_source(task_id="TASK-002"),
                promotion_source(task_id="TASK-001"),
            )
        )
        self.assertEqual(
            ("TASK-001", "TASK-002"), tuple(item.task_id for item in ordered)
        )
        selected = service.plan_next(
            (promotion_source(),),
            expected_target_sha=GIT_A,
            operation_id="PROMOTE-NEXT",
            coordinator_epoch=1,
        )
        self.assertIs(selected, service._repository.plan)

    def test_apply_reconcile_and_effect_validation(self) -> None:
        unknown_repository = PromotionRepository(
            plan=self.plan,
            disposition=PromotionDisposition.UNKNOWN
        )
        service = PromotionService(unknown_repository, self.index)
        observation = service.apply(self.effect, self.plan)
        self.assertIs(observation.disposition, PromotionDisposition.UNKNOWN)
        self.assertTrue(service.blocked_on_unknown)
        with self.assertRaisesRegex(
            PromotionBoundaryError, "promotion_outcome_unknown"
        ):
            service.plan_next(
                (promotion_source(),),
                expected_target_sha=GIT_A,
                operation_id="PROMOTE-BLOCKED",
                coordinator_epoch=1,
            )
        with self.assertRaisesRegex(
            PromotionBoundaryError, "promotion_outcome_unknown"
        ):
            service.apply(self.effect, self.plan)

        other_plan = bound_promotion_plan()
        other_plan = PromotionPlan(
            dataclasses.replace(other_plan.command, operation_id="PROMOTE-OTHER"),
            other_plan.source,
        )
        with self.assertRaisesRegex(PromotionBoundaryError, "different_promotion"):
            service.reconcile(other_plan)
        with self.assertRaises(TypeError):
            service.reconcile(object())  # type: ignore[arg-type]

        unknown_repository.disposition = PromotionDisposition.ABSENT
        reconciled = service.reconcile(self.plan)
        self.assertIs(reconciled.disposition, PromotionDisposition.ABSENT)
        self.assertFalse(service.blocked_on_unknown)

        valid_service = PromotionService(PromotionRepository(), self.index)
        with self.assertRaises(TypeError):
            valid_service.materialize_candidate(object())
        with self.assertRaises(TypeError):
            valid_service.bind_acceptance(object(), (evidence_ref(),))
        with self.assertRaisesRegex(PromotionBoundaryError, "already_bound"):
            valid_service.bind_acceptance(self.plan, (evidence_ref(),))
        unbound_effect = prepared_effect(
            self.unbound_plan.command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            adapter=AdapterKind.GIT,
            object_type=EffectObjectType.GIT_REF,
            target_hash=self.unbound_plan.command.target_workspace_hash,
        )
        with self.assertRaisesRegex(ValueError, "acceptance evidence"):
            valid_service.apply(unbound_effect, self.unbound_plan)
        with self.assertRaisesRegex(ValueError, "acceptance evidence"):
            valid_service.reconcile(self.unbound_plan)
        with valid_service.materialize_candidate(self.unbound_plan) as candidate:
            self.assertEqual(Path("C:/attempts/promotion-candidate"), candidate)
        with self.assertRaisesRegex(PromotionBoundaryError, "already_bound"):
            valid_service.materialize_candidate(self.plan)
        with self.assertRaisesRegex(PromotionBoundaryError, "evidence_absent"):
            valid_service.bind_acceptance(self.unbound_plan, ())
        with self.assertRaises(TypeError):
            valid_service.apply(object(), self.plan)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            valid_service.apply(self.effect, object())  # type: ignore[arg-type]
        other_command = dataclasses.replace(self.plan.command, operation_id="PROMOTE-X")
        other_effect = prepared_effect(
            other_command,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            operation=EffectOperation.RESULT_PROMOTION,
            adapter=AdapterKind.GIT,
            object_type=EffectObjectType.GIT_REF,
            target_hash=other_command.target_workspace_hash,
        )
        with self.assertRaisesRegex(ValueError, "does not bind"):
            valid_service.apply(other_effect, self.plan)

        boundary_cases = (
            {
                "event_type": JournalEventType.EFFECT_REQUESTED,
                "operation": EffectOperation.RESULT_PROMOTION,
                "adapter": AdapterKind.GIT,
                "object_type": EffectObjectType.GIT_REF,
                "target_hash": self.plan.command.target_workspace_hash,
            },
            {
                "event_type": JournalEventType.PROMOTION_REQUESTED,
                "operation": EffectOperation.TASK_EXECUTION,
                "adapter": AdapterKind.TASK,
                "object_type": EffectObjectType.WORKER,
                "target_hash": self.plan.command.target_workspace_hash,
            },
            {
                "event_type": JournalEventType.PROMOTION_REQUESTED,
                "operation": EffectOperation.RESULT_PROMOTION,
                "adapter": AdapterKind.GIT,
                "object_type": EffectObjectType.GIT_REF,
                "target_hash": self.plan.command.target_workspace_hash,
                "identity": ExecutionIdentity(
                    "WISH-OTHER",
                    1,
                    self.plan.command.task_id,
                    self.plan.command.attempt,
                    self.plan.command.operation_id,
                ),
            },
        )
        for values in boundary_cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                valid_service.apply(
                    prepared_effect(self.plan.command, **values), self.plan
                )


class TrellisContractCoverageTests(unittest.TestCase):
    def test_primitive_validators_reject_ambiguous_values(self) -> None:
        calls = (
            (trellis_contracts._text, (1, "field"), TypeError),
            (trellis_contracts._text, (" ", "field"), ValueError),
            (
                trellis_contracts._text,
                ("abc", "field"),
                ValueError,
                {"max_length": 2},
            ),
            (trellis_contracts._text, ("\ud800", "field"), ValueError),
            (trellis_contracts._text, ("bad\x00", "field"), ValueError),
            (trellis_contracts._token, ("has spaces", "field"), ValueError),
            (trellis_contracts._digest, ("bad", "field"), ValueError),
            (trellis_contracts._positive, (0, "field"), ValueError),
            (trellis_contracts._timestamp, ("not-time", "field"), ValueError),
            (
                trellis_contracts._timestamp,
                ("2026-99-99T00:00:00Z", "field"),
                ValueError,
            ),
            (trellis_contracts._commit, ("BAD", "field"), ValueError),
            (trellis_contracts._evidence, (["one"],), TypeError),
            (
                trellis_contracts._evidence,
                (tuple(f"item-{item}" for item in range(33)),),
                ValueError,
            ),
            (trellis_contracts._evidence, (("same", "same"),), ValueError),
        )
        for item in calls:
            function, arguments, error, *options = item
            kwargs = options[0] if options else {}
            with (
                self.subTest(function=function.__name__, arguments=arguments),
                self.assertRaises(error),
            ):
                function(*arguments, **kwargs)

    def test_snapshot_commands_and_capability_guards(self) -> None:
        raw = b'{"complete":true,"tasks":[]}\n'
        snapshot = TrellisGraphSnapshot(
            "wish-builder.trellis-graph.v1",
            "0.6.15",
            "parent-001",
            None,
            NOW,
            raw,
            "sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(len(raw), snapshot.byte_length)
        snapshot_cases = (
            {"snapshot_bytes": "bad"},
            {"snapshot_bytes": b""},
            {"snapshot_bytes": b"x" * (trellis_contracts.MAX_GRAPH_SNAPSHOT_BYTES + 1)},
            {"source_sha256": SHA_A},
            {"complete": 1},
        )
        for changes in snapshot_cases:
            with (
                self.subTest(snapshot=tuple(changes)),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(snapshot, **changes)

        reserve = ReserveChannel(
            "RESERVE-001",
            "attempt-001",
            "DISPATCH-001",
            "channel-001",
            WorkerProvider.CODEX,
            SHA_A,
            SHA_B,
            SHA_C,
        )
        with self.assertRaises(TypeError):
            dataclasses.replace(reserve, provider="codex")
        self.assertEqual("codex", reserve.to_primitive()["provider"])

        packet = "implement TASK-001"
        sent = SendTaskPacket(
            "SEND-001",
            "attempt-001",
            "DISPATCH-001",
            "channel-001",
            "message-001",
            "turn-001",
            packet,
            "sha256:" + hashlib.sha256(packet.encode()).hexdigest(),
        )
        with self.assertRaisesRegex(ValueError, "byte limit"):
            dataclasses.replace(sent, task_packet="\u00e9" * 600_000)
        with self.assertRaisesRegex(ValueError, "does not match"):
            dataclasses.replace(sent, task_packet_digest=SHA_A)
        self.assertEqual("send_task_packet", sent.to_primitive()["command_type"])

        capabilities = BackendCapabilities(
            WorkerProvider.CODEX,
            "windows-x86_64",
            SHA_A,
            SHA_B,
            SHA_C,
            4096,
        )
        with self.assertRaises(TypeError):
            dataclasses.replace(capabilities, provider="codex")
        with self.assertRaises(TypeError):
            dataclasses.replace(capabilities, fresh_session_per_attempt=1)
        self.assertTrue(capabilities.to_primitive()["caller_supplied_ids"])

    def test_common_observation_status_rules(self) -> None:
        invalid = (
            {"status": "applied"},
            {"status": EffectStatus.ABSENT, "effect_digest": SHA_A},
            {"status": EffectStatus.APPLIED, "effect_digest": None},
            {"status": EffectStatus.UNKNOWN, "effect_digest": None, "evidence": ()},
        )
        for values in invalid:
            arguments = {
                "operation_id": "OBSERVE-001",
                "status": EffectStatus.ABSENT,
                "observed_at": NOW,
                "effect_digest": None,
                "evidence": (),
            } | values
            with (
                self.subTest(values=values),
                self.assertRaises((TypeError, ValueError)),
            ):
                trellis_contracts._observation_common(**arguments)

    def test_attempt_check_and_finish_observation_guards(self) -> None:
        attempt = AttemptObservation(
            "ATTEMPT-OBS-001",
            EffectStatus.APPLIED,
            NOW,
            TrellisLifecycleState.PREPARED,
            SHA_A,
            "attempt-001",
            "trellis-task-001",
            "worktree-001",
            "C:/attempts/001",
            GIT_A,
        )
        attempt_cases = (
            {"lifecycle_state": "prepared"},
            {
                "status": EffectStatus.ABSENT,
                "effect_digest": None,
                "lifecycle_state": TrellisLifecycleState.PREPARED,
            },
            {
                "status": EffectStatus.UNKNOWN,
                "effect_digest": None,
                "evidence": ("uncertain",),
                "lifecycle_state": TrellisLifecycleState.PREPARED,
            },
            {"lifecycle_state": TrellisLifecycleState.ABSENT},
            {"attempt_id": None},
            {"worktree_id": None},
        )
        for changes in attempt_cases:
            with (
                self.subTest(attempt=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(attempt, **changes)
        self.assertEqual("prepared", attempt.to_primitive()["lifecycle_state"])

        check = CheckObservation(
            "CHECK-OBS-001",
            EffectStatus.APPLIED,
            NOW,
            SHA_A,
            "attempt-001",
            True,
            GIT_A,
            SHA_B,
        )
        check_cases = (
            {"passed": 1},
            {"attempt_id": None},
            {
                "status": EffectStatus.ABSENT,
                "effect_digest": None,
            },
        )
        for changes in check_cases:
            with (
                self.subTest(check=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(check, **changes)
        self.assertTrue(check.to_primitive()["passed"])

        finish = FinishObservation(
            "FINISH-OBS-001",
            EffectStatus.APPLIED,
            NOW,
            SHA_A,
            "attempt-001",
            True,
            GIT_A,
            SHA_B,
        )
        finish_cases = (
            {"finished": 1},
            {"attempt_id": None},
            {
                "status": EffectStatus.ABSENT,
                "effect_digest": None,
            },
        )
        for changes in finish_cases:
            with (
                self.subTest(finish=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(finish, **changes)
        self.assertTrue(finish.to_primitive()["finished"])

    def test_channel_and_turn_observation_guards(self) -> None:
        channel = ChannelObservation(
            "CHANNEL-OBS-001",
            EffectStatus.APPLIED,
            NOW,
            SHA_A,
            "attempt-001",
            "channel-001",
            WorkerProvider.CODEX,
            "provider-session-001",
        )
        channel_cases = (
            {"provider": "codex"},
            {"attempt_id": None},
            {"status": EffectStatus.ABSENT, "effect_digest": None},
        )
        for changes in channel_cases:
            with (
                self.subTest(channel=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(channel, **changes)
        self.assertEqual("codex", channel.to_primitive()["provider"])

        turn = TurnObservation(
            "TURN-OBS-001",
            EffectStatus.APPLIED,
            NOW,
            TurnState.DONE,
            SHA_A,
            "attempt-001",
            "channel-001",
            "message-001",
            "turn-001",
            SHA_B,
        )
        turn_cases = (
            {"state": "done"},
            {
                "status": EffectStatus.ABSENT,
                "effect_digest": None,
                "state": TurnState.DONE,
            },
            {
                "status": EffectStatus.UNKNOWN,
                "effect_digest": None,
                "evidence": ("uncertain",),
                "state": TurnState.RUNNING,
            },
            {"state": TurnState.ABSENT},
            {"attempt_id": None},
            {"result_digest": None},
        )
        for changes in turn_cases:
            with self.subTest(turn=changes), self.assertRaises((TypeError, ValueError)):
                dataclasses.replace(turn, **changes)
        self.assertEqual("done", turn.to_primitive()["state"])


if __name__ == "__main__":
    unittest.main()
