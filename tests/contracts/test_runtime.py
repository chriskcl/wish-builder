from __future__ import annotations

import copy
import dataclasses
import hashlib
import unittest
from unittest.mock import patch

import wish_builder.contracts as public_contracts
from wish_builder.contracts import runtime_decoder
from wish_builder.contracts.decoder import DecodeLimits
from wish_builder.contracts.runtime import (
    JOURNAL_EVENT_VERSION,
    MAX_AFFECTED_IDENTITIES,
    MAX_EVIDENCE_REFS,
    RUNTIME_SCHEMA_VERSION,
    Acknowledgement,
    ActorIdentity,
    ActorType,
    AdapterKind,
    BudgetCharge,
    BudgetDimension,
    BudgetDisposition,
    CanonicalRuntimeContract,
    CommandIdentity,
    CommandKind,
    DecisionAdmissionReason,
    DecisionChoice,
    DecisionCommand,
    DecisionEvaluation,
    DecisionObservation,
    DecisionObservedPayload,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectReceiptValue,
    EffectRequestPayload,
    EffectStatus,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceSet,
    EvidenceType,
    ExecutionIdentity,
    IdentityObservation,
    JournalEvent,
    JournalEventType,
    JournalPosition,
    OperationOutcome,
    OutcomeKind,
    RecoveryPayload,
    RetryMetadata,
    RuntimeReasonCode,
    RuntimeState,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.contracts.runtime_decoder import (
    decode_actor_identity_bytes,
    decode_actor_identity_primitive,
    decode_command_identity_bytes,
    decode_decision_command_bytes,
    decode_decision_observation_bytes,
    decode_decision_request_bytes,
    decode_decision_request_primitive,
    decode_effect_receipt_bytes,
    decode_evidence_ref_bytes,
    decode_evidence_ref_primitive,
    decode_execution_identity_bytes,
    decode_journal_event_bytes,
    decode_journal_event_primitive,
    decode_operation_outcome_bytes,
    decode_operation_outcome_primitive,
)
from wish_builder.contracts.serialization import canonical_json_bytes, canonical_sha256

ZERO_HASH = "sha256:" + "0" * 64
NOW = "2026-08-18T03:00:00Z"


def hash_ref(number: int) -> str:
    return "sha256:" + f"{number:064x}"


def attempt_identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        run_id="RUN-001",
        coordinator_epoch=3,
        task_id="TASK-001",
        attempt=2,
        correlation_id="CORRELATION-001",
    )


def coordinator_actor() -> ActorIdentity:
    return ActorIdentity(
        ActorType.COORDINATOR,
        "coordinator-001",
        "host-001",
        100,
        "process-start-100",
    )


def human_actor() -> ActorIdentity:
    return ActorIdentity(
        ActorType.HUMAN,
        "local-account-001",
        "host-001",
        200,
        "process-start-200",
    )


def decision_request() -> DecisionRequest:
    return DecisionRequest(
        command=CommandIdentity(
            schema_version=1,
            command_id="COMMAND-001",
            request_id="REQUEST-001",
            kind=CommandKind.DECIDE,
            expected_sequence=10,
            request_nonce="nonce-001",
            actor=coordinator_actor(),
            source_channel=SourceChannel.COORDINATOR,
            submitted_at=NOW,
        ),
        decision_type=DecisionType.GATE_B,
        candidate_hash=hash_ref(1),
        workspace_hash=hash_ref(2),
        expected_actor_id="local-account-001",
        options=(DecisionChoice.APPROVE, DecisionChoice.REVISE, DecisionChoice.REJECT),
    )


def decision_command() -> DecisionCommand:
    return DecisionCommand(
        decision_id="DECISION-001",
        request=decision_request(),
        choice=DecisionChoice.APPROVE,
        actor=human_actor(),
        source_channel=SourceChannel.DIRECT_CLI,
        decided_at=NOW,
    )


def decision_observation() -> DecisionObservation:
    command = decision_command()
    return DecisionObservation(
        command,
        event_sequence=11,
        submission_hash="sha256:" + canonical_sha256(command.to_primitive()),
    )


def evidence_ref(number: int = 10, *, sensitivity: EvidenceSensitivity = EvidenceSensitivity.INTERNAL) -> EvidenceRef:
    return EvidenceRef(
        schema_version=1,
        digest=hash_ref(number),
        byte_length=number,
        evidence_type=EvidenceType.RESULT,
        producer=EvidenceProducer(
            attempt_identity(),
            event_id="EVENT-010",
            external_object_id="worker-result-10",
        ),
        created_at=NOW,
        sensitivity=sensitivity,
        render_policy=(
            EvidenceRenderPolicy.METADATA_ONLY
            if sensitivity is EvidenceSensitivity.SENSITIVE
            else EvidenceRenderPolicy.TEXT
        ),
        role=EvidenceRole.REQUIRED,
        structured_subject_hash=hash_ref(number + 1000),
    )


def applied_receipt() -> EffectReceipt:
    return EffectReceipt(
        schema_version=1,
        identity=attempt_identity(),
        operation=EffectOperation.WORKER_DISPATCH,
        status=EffectStatus.APPLIED,
        observed_at=NOW,
        effect_hash=hash_ref(20),
        external_object_id="worker-001",
        evidence=(evidence_ref(),),
    )


def transition_event() -> JournalEvent:
    return JournalEvent.create(
        sequence=1,
        event_id="EVENT-001",
        event_type=JournalEventType.RUN_INITIALIZED,
        identity=ExecutionIdentity("RUN-001", 0),
        actor_type=ActorType.SYSTEM,
        actor_id="wishctl",
        recorded_at=NOW,
        previous_event_hash=ZERO_HASH,
        payload=TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.NONE,
            RuntimeState.PREFLIGHT,
        ),
    )


class RuntimeContractTests(unittest.TestCase):
    def assert_round_trip(self, value: object, decoder: object) -> None:
        raw = value.canonical_json_bytes()  # type: ignore[attr-defined]
        result = decoder(raw)  # type: ignore[operator]
        self.assertTrue(result.ok, result.report.render_text())
        self.assertEqual(value, result.value)
        self.assertEqual(raw, result.value.canonical_json_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result.source_sha256)

    def test_runtime_contracts_and_decoders_are_public(self) -> None:
        for name in (
            "ExecutionIdentity",
            "DecisionCommand",
            "EvidenceRef",
            "EffectReceipt",
            "OperationOutcome",
            "JournalEvent",
            "decode_journal_event_bytes",
            "decode_operation_outcome_bytes",
        ):
            with self.subTest(name=name):
                self.assertIn(name, public_contracts.__all__)
                self.assertTrue(hasattr(public_contracts, name))

    def test_pr_merge_and_revert_task_events_round_trip(self) -> None:
        task_identity = ExecutionIdentity(
            run_id="RUN-001",
            coordinator_epoch=3,
            task_id="TASK-001",
        )
        transitions = (
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
                JournalEventType.REVERT_OBSERVED,
                RuntimeState.MERGED,
                RuntimeState.REVERTED,
            ),
        )
        previous_hash = ZERO_HASH
        for sequence, (event_type, from_state, to_state) in enumerate(
            transitions,
            start=1,
        ):
            event = JournalEvent.create(
                sequence=sequence,
                event_id=f"EVENT-{sequence:03d}",
                event_type=event_type,
                identity=task_identity,
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=NOW,
                previous_event_hash=previous_hash,
                payload=TransitionPayload(
                    TransitionSubject.TASK,
                    from_state,
                    to_state,
                ),
            )
            self.assert_round_trip(event, decode_journal_event_bytes)
            previous_hash = event.event_hash

    def test_execution_command_and_decision_identity_round_trip(self) -> None:
        self.assert_round_trip(attempt_identity(), decode_execution_identity_bytes)
        self.assert_round_trip(decision_request().command, decode_command_identity_bytes)
        self.assert_round_trip(decision_command(), decode_decision_command_bytes)
        self.assert_round_trip(decision_observation(), decode_decision_observation_bytes)

    def test_runtime_contracts_are_frozen_slotted_and_reject_untyped_values(self) -> None:
        identity = attempt_identity()
        self.assertFalse(hasattr(identity, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            identity.attempt = 3  # type: ignore[misc]
        with self.assertRaises(TypeError):
            OperationOutcome(1, OutcomeKind.SUCCESS, value={"unsafe": True})  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            JournalEvent.create(
                sequence=1,
                event_id="EVENT-001",
                event_type=JournalEventType.RUN_INITIALIZED,
                identity=ExecutionIdentity("RUN-001", 0),
                actor_type=ActorType.SYSTEM,
                actor_id="wishctl",
                recorded_at=NOW,
                previous_event_hash=ZERO_HASH,
                payload={"payload_type": "transition"},  # type: ignore[arg-type]
            )

    def test_identity_combinations_and_integer_boundaries_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            ExecutionIdentity("run-001", 1)
        with self.assertRaises(ValueError):
            ExecutionIdentity("RUN-001", 1, attempt=1)
        with self.assertRaises(ValueError):
            ExecutionIdentity("RUN-001", 1, "TASK-001", correlation_id="CORRELATION-001")
        with self.assertRaises(ValueError):
            ExecutionIdentity("RUN-001", 0, "TASK-001", 1, "CORRELATION-001")
        accepted = ExecutionIdentity("RUN-001", 2**63 - 1)
        self.assertEqual(2**63 - 1, accepted.coordinator_epoch)
        with self.assertRaises(ValueError):
            ExecutionIdentity("RUN-001", 2**63)

    def test_decision_actor_choice_and_submission_hash_are_bound(self) -> None:
        with self.assertRaises(ValueError):
            dataclasses.replace(
                decision_command(),
                actor=dataclasses.replace(human_actor(), actor_id="other-account"),
            )
        with self.assertRaises(ValueError):
            dataclasses.replace(decision_command(), choice=DecisionChoice.ABORT)
        with self.assertRaises(ValueError):
            dataclasses.replace(decision_observation(), submission_hash=hash_ref(999))

    def test_evidence_reference_round_trip_and_sensitivity_policy(self) -> None:
        self.assert_round_trip(evidence_ref(), decode_evidence_ref_bytes)
        sensitive = evidence_ref(11, sensitivity=EvidenceSensitivity.SENSITIVE)
        self.assertEqual(EvidenceRenderPolicy.METADATA_ONLY, sensitive.render_policy)
        with self.assertRaises(ValueError):
            dataclasses.replace(sensitive, render_policy=EvidenceRenderPolicy.DOWNLOAD)

    def test_effect_receipt_has_closed_absent_applied_unknown_semantics(self) -> None:
        self.assert_round_trip(applied_receipt(), decode_effect_receipt_bytes)
        absent = dataclasses.replace(
            applied_receipt(),
            status=EffectStatus.ABSENT,
            effect_hash=None,
            external_object_id=None,
            evidence=(),
        )
        self.assertEqual(EffectStatus.ABSENT, absent.status)
        unknown = dataclasses.replace(
            applied_receipt(),
            status=EffectStatus.UNKNOWN,
            effect_hash=None,
            external_object_id=None,
        )
        self.assertEqual(EffectStatus.UNKNOWN, unknown.status)
        with self.assertRaises(ValueError):
            dataclasses.replace(absent, status=EffectStatus.APPLIED)
        with self.assertRaises(ValueError):
            dataclasses.replace(absent, status=EffectStatus.UNKNOWN)
        with self.assertRaises(ValueError):
            dataclasses.replace(absent, effect_hash=hash_ref(1))

    def test_operation_outcome_variants_and_combinations(self) -> None:
        success_values = (
            None,
            Acknowledgement(),
            IdentityObservation("worker-001"),
            JournalPosition(1, "EVENT-001", hash_ref(1)),
            EffectReceiptValue(applied_receipt()),
            EvidenceSet((evidence_ref(),)),
        )
        for value in success_values:
            with self.subTest(value=type(value).__name__):
                outcome = OperationOutcome(1, OutcomeKind.SUCCESS, value=value)
                self.assert_round_trip(outcome, decode_operation_outcome_bytes)

        retryable = OperationOutcome(
            1,
            OutcomeKind.RETRYABLE,
            reason_code=RuntimeReasonCode.RATE_LIMITED,
            retry=RetryMetadata(1, 3, NOW),
            budget_charge=BudgetCharge(
                BudgetDimension.ATTEMPTS, 1, BudgetDisposition.RESERVED
            ),
            user_message_key="adapter.rate_limited",
        )
        blocked = OperationOutcome(
            1,
            OutcomeKind.BLOCKED,
            reason_code=RuntimeReasonCode.EXTERNAL_OUTCOME_UNKNOWN,
            evidence=(evidence_ref(),),
            user_message_key="adapter.outcome_unknown",
        )
        terminal = OperationOutcome(
            1,
            OutcomeKind.TERMINAL,
            reason_code=RuntimeReasonCode.POLICY_DENIED,
            user_message_key="adapter.policy_denied",
        )
        for outcome in (retryable, blocked, terminal):
            self.assert_round_trip(outcome, decode_operation_outcome_bytes)

        invalid = (
            {"kind": OutcomeKind.SUCCESS, "reason_code": RuntimeReasonCode.POLICY_DENIED},
            {
                "kind": OutcomeKind.RETRYABLE,
                "reason_code": RuntimeReasonCode.RATE_LIMITED,
                "user_message_key": "retry",
            },
            {
                "kind": OutcomeKind.BLOCKED,
                "reason_code": RuntimeReasonCode.POLICY_DENIED,
                "user_message_key": "blocked",
            },
            {
                "kind": OutcomeKind.TERMINAL,
                "value": Acknowledgement(),
                "reason_code": RuntimeReasonCode.POLICY_DENIED,
                "user_message_key": "terminal",
            },
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                OperationOutcome(1, **arguments)

    def test_journal_event_factory_hashes_and_round_trips_deterministically(self) -> None:
        event = transition_event()
        self.assertEqual(JOURNAL_EVENT_VERSION, event.event_version)
        self.assertEqual(event.computed_payload_hash(), event.payload_hash)
        self.assertEqual(event.computed_event_hash(), event.event_hash)
        self.assertTrue(event.canonical_json_bytes().endswith(b"\n"))
        self.assert_round_trip(event, decode_journal_event_bytes)
        rebuilt = transition_event()
        self.assertEqual(event.canonical_json_bytes(), rebuilt.canonical_json_bytes())

    def test_new_writes_use_trellis_terms_and_reject_legacy_decomposition_terms(self) -> None:
        event = JournalEvent.create(
            sequence=4,
            event_id="EVENT-TRELLIS-004",
            event_type=JournalEventType.TRELLIS_GRAPH_IMPORTED,
            identity=ExecutionIdentity("RUN-001", 1),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-001",
            recorded_at=NOW,
            previous_event_hash=hash_ref(3),
            payload=TransitionPayload(
                TransitionSubject.RUN,
                RuntimeState.TRELLIS_PREPARATION,
                RuntimeState.GATE_B_PENDING,
            ),
        )
        primitive = event.to_primitive()
        payload_primitive = primitive["payload"]
        self.assertIs(type(payload_primitive), dict)
        assert type(payload_primitive) is dict
        self.assertEqual("trellis_graph_imported", primitive["event_type"])
        self.assertEqual("trellis_preparation", payload_primitive["from_state"])
        self.assertEqual(
            "trellis_graph_incomplete",
            RuntimeReasonCode.TRELLIS_GRAPH_INCOMPLETE.value,
        )
        self.assert_round_trip(event, decode_journal_event_bytes)

        with self.assertRaisesRegex(ValueError, "event_type is replay-only"):
            JournalEvent.create(
                sequence=4,
                event_id="EVENT-LEGACY-TYPE",
                event_type=JournalEventType.DECOMPOSITION_COMPLETED,
                identity=ExecutionIdentity("RUN-001", 1),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=NOW,
                previous_event_hash=hash_ref(3),
                payload=TransitionPayload(
                    TransitionSubject.RUN,
                    RuntimeState.TRELLIS_PREPARATION,
                    RuntimeState.GATE_B_PENDING,
                ),
            )
        with self.assertRaisesRegex(ValueError, "state is replay-only"):
            JournalEvent.create(
                sequence=4,
                event_id="EVENT-LEGACY-STATE",
                event_type=JournalEventType.TRELLIS_GRAPH_IMPORTED,
                identity=ExecutionIdentity("RUN-001", 1),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=NOW,
                previous_event_hash=hash_ref(3),
                payload=TransitionPayload(
                    TransitionSubject.RUN,
                    RuntimeState.DECOMPOSITION,
                    RuntimeState.GATE_B_PENDING,
                ),
            )
        with self.assertRaisesRegex(ValueError, "reason_code is replay-only"):
            JournalEvent.create(
                sequence=4,
                event_id="EVENT-LEGACY-REASON",
                event_type=JournalEventType.RUN_BLOCKED,
                identity=ExecutionIdentity("RUN-001", 1),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=NOW,
                previous_event_hash=hash_ref(3),
                payload=TransitionPayload(
                    TransitionSubject.RUN,
                    RuntimeState.RUNNING,
                    RuntimeState.BLOCKED,
                ),
                reason_code=RuntimeReasonCode.DECOMPOSITION_INCOMPLETE,
            )

    def test_all_journal_payload_variants_are_typed_and_round_trip(self) -> None:
        request_event = JournalEvent.create(
            sequence=10,
            event_id="EVENT-010",
            event_type=JournalEventType.DECISION_REQUESTED,
            identity=ExecutionIdentity("RUN-001", 0),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-001",
            recorded_at=NOW,
            previous_event_hash=transition_event().event_hash,
            payload=DecisionRequestPayload(decision_request()),
        )
        observation_event = JournalEvent.create(
            sequence=11,
            event_id="EVENT-011",
            event_type=JournalEventType.DECISION_OBSERVED,
            identity=ExecutionIdentity("RUN-001", 0),
            actor_type=ActorType.HUMAN,
            actor_id="local-account-001",
            recorded_at=NOW,
            previous_event_hash=hash_ref(50),
            payload=DecisionObservedPayload(decision_observation()),
        )
        request_payload = EffectRequestPayload(
            EffectOperation.WORKER_DISPATCH,
            AdapterKind.TASK,
            EffectObjectType.WORKER,
            hash_ref(30),
            hash_ref(31),
            20,
            3,
        )
        effect_request_event = JournalEvent.create(
            sequence=21,
            event_id="EVENT-021",
            event_type=JournalEventType.DISPATCH_REQUESTED,
            identity=attempt_identity(),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-001",
            recorded_at=NOW,
            previous_event_hash=hash_ref(51),
            payload=request_payload,
        )
        effect_observation_event = JournalEvent.create(
            sequence=22,
            event_id="EVENT-022",
            event_type=JournalEventType.DISPATCH_OBSERVED,
            identity=attempt_identity(),
            actor_type=ActorType.ADAPTER,
            actor_id="fake-task-adapter",
            recorded_at=NOW,
            previous_event_hash=effect_request_event.event_hash,
            payload=EffectObservationPayload(AdapterKind.TASK, applied_receipt()),
        )
        recovery_event = JournalEvent.create(
            sequence=23,
            event_id="EVENT-023",
            event_type=JournalEventType.RECOVERY_STARTED,
            identity=ExecutionIdentity("RUN-001", 4),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-002",
            recorded_at=NOW,
            previous_event_hash=effect_observation_event.event_hash,
            payload=RecoveryPayload(
                22,
                effect_observation_event.event_hash,
                ("TASK-001", "EVENT-021"),
                (evidence_ref(),),
            ),
        )
        for event in (
            request_event,
            observation_event,
            effect_request_event,
            effect_observation_event,
            recovery_event,
        ):
            with self.subTest(event_type=event.event_type):
                self.assert_round_trip(event, decode_journal_event_bytes)

    def test_journal_event_rejects_payload_type_identity_and_reason_mismatches(self) -> None:
        with self.assertRaises(TypeError):
            JournalEvent.create(
                sequence=2,
                event_id="EVENT-002",
                event_type=JournalEventType.DECISION_REQUESTED,
                identity=ExecutionIdentity("RUN-001", 0),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=NOW,
                previous_event_hash=hash_ref(1),
                payload=TransitionPayload(
                    TransitionSubject.RUN, RuntimeState.DISCOVERY, RuntimeState.GATE_A_PENDING
                ),
            )
        with self.assertRaises(ValueError):
            JournalEvent.create(
                sequence=2,
                event_id="EVENT-002",
                event_type=JournalEventType.DISPATCH_REQUESTED,
                identity=ExecutionIdentity("RUN-001", 1),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=NOW,
                previous_event_hash=hash_ref(1),
                payload=EffectRequestPayload(
                    EffectOperation.WORKER_DISPATCH,
                    AdapterKind.TASK,
                    EffectObjectType.WORKER,
                    hash_ref(2),
                    hash_ref(3),
                    1,
                    1,
                ),
            )
        with self.assertRaises(ValueError):
            JournalEvent.create(
                sequence=2,
                event_id="EVENT-002",
                event_type=JournalEventType.RUN_BLOCKED,
                identity=ExecutionIdentity("RUN-001", 1),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=NOW,
                previous_event_hash=hash_ref(1),
                payload=TransitionPayload(
                    TransitionSubject.RUN, RuntimeState.RUNNING, RuntimeState.BLOCKED
                ),
            )

    def test_tampered_payload_and_event_hashes_are_rejected_at_exact_fields(self) -> None:
        primitive = transition_event().to_primitive()
        primitive["payload"]["to_state"] = "discovery"  # type: ignore[index]
        result = decode_journal_event_primitive(primitive)
        self.assertFalse(result.ok)
        self.assertIn("/payload_hash", {issue.path_text for issue in result.issues})

        primitive = transition_event().to_primitive()
        primitive["event_hash"] = hash_ref(999)
        result = decode_journal_event_primitive(primitive)
        self.assertFalse(result.ok)
        self.assertIn("/event_hash", {issue.path_text for issue in result.issues})

    def test_unknown_missing_wrong_type_and_invalid_scalar_fields_aggregate(self) -> None:
        primitive = transition_event().to_primitive()
        primitive["unknown"] = True
        primitive["event_version"] = "2.0"
        primitive["event_type"] = "invented"
        primitive["event_id"] = "bad"
        primitive["recorded_at"] = "2026-02-30T00:00:00Z"
        primitive["previous_event_hash"] = "sha256:ABC"
        primitive["sequence"] = True
        del primitive["actor_id"]
        first = decode_journal_event_primitive(primitive)
        second = decode_journal_event_primitive(copy.deepcopy(primitive))
        self.assertFalse(first.ok)
        reasons = {issue.reason_code.value for issue in first.issues}
        self.assertTrue(
            {
                "unknown_field",
                "unsupported_schema_version",
                "unknown_enum_value",
                "invalid_identifier",
                "invalid_timestamp",
                "invalid_hash",
                "wrong_primitive_type",
                "missing_field",
            }.issubset(reasons)
        )
        self.assertEqual(first.diagnostic_bytes(), second.diagnostic_bytes())

    def test_raw_hostile_json_is_rejected_before_contract_admission(self) -> None:
        valid = transition_event().canonical_json_bytes()
        hostile_cases = (
            (b"\xff", "json.invalid_utf8"),
            (b"{", "json.invalid_syntax"),
            (b'{"sequence":NaN}', "json.non_finite_number"),
            (b'{"sequence":9223372036854775808}', "json.integer_range"),
            (valid.replace(b'"payload_type":"transition"', b'"payload_type":"transition","payload_type":"transition"'), "json.duplicate_key"),
        )
        for raw, expected_rule in hostile_cases:
            with self.subTest(rule=expected_rule):
                result = decode_journal_event_bytes(raw)
                self.assertFalse(result.ok)
                self.assertIn(expected_rule, {issue.rule_id for issue in result.issues})
        duplicate = decode_journal_event_bytes(hostile_cases[-1][0])
        self.assertIn("/payload/payload_type", {issue.path_text for issue in duplicate.issues})

    def test_raw_and_primitive_resource_limits_are_enforced(self) -> None:
        raw = transition_event().canonical_json_bytes()
        result = decode_journal_event_bytes(raw, limits=DecodeLimits(max_bytes=len(raw) - 1))
        self.assertEqual("json.byte_limit", result.issues[0].rule_id)
        result = decode_journal_event_bytes(
            raw,
            limits=DecodeLimits(max_depth=2, max_items=10_000, max_string_length=16_384),
        )
        self.assertIn("json.depth_limit", {issue.rule_id for issue in result.issues})
        at_limit = decode_journal_event_bytes(raw, limits=DecodeLimits(max_bytes=len(raw)))
        self.assertTrue(at_limit.ok, at_limit.report.render_text())

    def test_evidence_and_recovery_collection_limits_are_inclusive(self) -> None:
        refs = tuple(evidence_ref(index + 1) for index in range(MAX_EVIDENCE_REFS))
        admitted = EvidenceSet(tuple(reversed(refs)))
        self.assertEqual(tuple(sorted(refs, key=lambda item: item.digest)), admitted.evidence)
        with self.assertRaises(ValueError):
            EvidenceSet(refs + (evidence_ref(MAX_EVIDENCE_REFS + 1),))

        identities = tuple(f"TASK-{index:03d}" for index in range(MAX_AFFECTED_IDENTITIES))
        admitted_recovery = RecoveryPayload(0, ZERO_HASH, identities, ())
        self.assertEqual(MAX_AFFECTED_IDENTITIES, len(admitted_recovery.affected_identities))
        with self.assertRaises(ValueError):
            RecoveryPayload(0, ZERO_HASH, identities + ("TASK-999",), ())

    def test_set_like_evidence_has_stable_bytes_and_diagnostics(self) -> None:
        refs = (evidence_ref(1), evidence_ref(2), evidence_ref(3))
        left = OperationOutcome(
            1,
            OutcomeKind.BLOCKED,
            reason_code=RuntimeReasonCode.EVIDENCE_INVALID,
            evidence=refs,
            user_message_key="evidence.invalid",
        )
        right = dataclasses.replace(left, evidence=tuple(reversed(refs)))
        self.assertEqual(left.canonical_json_bytes(), right.canonical_json_bytes())

        bad_left = left.to_primitive()
        bad_left["evidence"][0]["created_at"] = "bad"  # type: ignore[index]
        bad_right = copy.deepcopy(bad_left)
        bad_right["evidence"].reverse()  # type: ignore[union-attr]
        first = decode_operation_outcome_primitive(bad_left)
        second = decode_operation_outcome_primitive(bad_right)
        self.assertEqual(first.diagnostic_bytes(), second.diagnostic_bytes())

    def test_decoder_never_aliases_caller_owned_primitive(self) -> None:
        source = applied_receipt().to_primitive()
        result = decode_effect_receipt_bytes(canonical_json_bytes(source))
        self.assertTrue(result.ok, result.report.render_text())
        source["identity"]["task_id"] = "TASK-999"  # type: ignore[index]
        source["evidence"].clear()  # type: ignore[union-attr]
        self.assertEqual("TASK-001", result.value.identity.task_id)
        self.assertEqual(1, len(result.value.evidence))

    def test_schema_version_and_hash_boundaries(self) -> None:
        self.assertEqual(1, RUNTIME_SCHEMA_VERSION)
        primitive = evidence_ref().to_primitive()
        primitive["schema_version"] = 2
        result = decode_evidence_ref_bytes(canonical_json_bytes(primitive))
        self.assertEqual(
            ["schema.runtime_version"],
            [issue.rule_id for issue in result.issues],
        )
        primitive = applied_receipt().to_primitive()
        primitive["effect_hash"] = "sha256:" + "A" * 64
        result = decode_effect_receipt_bytes(canonical_json_bytes(primitive))
        self.assertIn("value.sha256_reference", {issue.rule_id for issue in result.issues})

    def test_direct_constructor_defenses_cover_every_shared_scalar_boundary(self) -> None:
        identity = attempt_identity()
        actor = human_actor()
        command = decision_request().command
        reference = evidence_ref()

        cases = (
            (lambda: dataclasses.replace(identity, run_id=17), TypeError),
            (lambda: dataclasses.replace(identity, run_id=" "), ValueError),
            (lambda: dataclasses.replace(identity, run_id="R" * 65), ValueError),
            (lambda: dataclasses.replace(identity, run_id="RUN-\ud800"), ValueError),
            (lambda: dataclasses.replace(identity, run_id="RUN-\u202eX"), ValueError),
            (lambda: dataclasses.replace(actor, actor_id="bad token!"), ValueError),
            (lambda: dataclasses.replace(actor, actor_type="human"), TypeError),
            (lambda: dataclasses.replace(actor, process_id=True), ValueError),
            (lambda: dataclasses.replace(command, schema_version=2), ValueError),
            (lambda: dataclasses.replace(command, kind="decide"), TypeError),
            (lambda: dataclasses.replace(command, actor=object()), TypeError),
            (lambda: dataclasses.replace(command, source_channel="direct_cli"), TypeError),
            (lambda: dataclasses.replace(command, submitted_at="not-time"), ValueError),
            (lambda: dataclasses.replace(command, submitted_at="2026-02-30T00:00:00Z"), ValueError),
            (lambda: dataclasses.replace(reference, digest="bad"), ValueError),
            (lambda: dataclasses.replace(reference, evidence_type="result"), TypeError),
            (lambda: dataclasses.replace(reference, producer=object()), TypeError),
            (lambda: dataclasses.replace(reference, sensitivity="internal"), TypeError),
            (lambda: dataclasses.replace(reference, render_policy="text"), TypeError),
            (lambda: dataclasses.replace(reference, role="required"), TypeError),
        )
        for build, error in cases:
            with self.subTest(build=repr(build)), self.assertRaises(error):
                build()

        base = CanonicalRuntimeContract()
        with self.assertRaises(NotImplementedError):
            base.to_primitive()
        self.assertEqual(reference.canonical_sha256(), reference.canonical_sha256())

    def test_direct_decision_and_collection_defenses_are_closed(self) -> None:
        request = decision_request()
        command = decision_command()
        observation = decision_observation()

        bad_command = dataclasses.replace(request.command, kind=CommandKind.PAUSE)
        request_cases = (
            lambda: dataclasses.replace(request, command=bad_command),
            lambda: dataclasses.replace(request, command=object()),
            lambda: dataclasses.replace(request, decision_type="gate_b"),
            lambda: dataclasses.replace(request, options=[]),
            lambda: dataclasses.replace(request, options=()),
            lambda: dataclasses.replace(
                request,
                options=tuple(DecisionChoice.APPROVE for _ in range(17)),
            ),
            lambda: dataclasses.replace(request, options=("approve",)),
            lambda: dataclasses.replace(
                request,
                options=(DecisionChoice.APPROVE, DecisionChoice.APPROVE),
            ),
        )
        for build in request_cases:
            with self.subTest(build=repr(build)), self.assertRaises((TypeError, ValueError)):
                build()

        command_cases = (
            lambda: dataclasses.replace(command, request=object()),
            lambda: dataclasses.replace(command, choice="approve"),
            lambda: dataclasses.replace(command, actor=object()),
            lambda: dataclasses.replace(command, source_channel="direct_cli"),
            lambda: dataclasses.replace(observation, decision=object()),
        )
        for build in command_cases:
            with self.subTest(build=repr(build)), self.assertRaises((TypeError, ValueError)):
                build()

        accepted = DecisionEvaluation(
            True,
            DecisionAdmissionReason.ACCEPTED,
            observation,
        )
        replay = DecisionEvaluation(
            True,
            DecisionAdmissionReason.IDEMPOTENT_REPLAY,
            observation,
            idempotent=True,
        )
        self.assertTrue(accepted.accepted)
        self.assertTrue(replay.idempotent)
        invalid_evaluations = (
            lambda: DecisionEvaluation(1, DecisionAdmissionReason.ACCEPTED, observation),
            lambda: DecisionEvaluation(True, "accepted", observation),
            lambda: DecisionEvaluation(True, DecisionAdmissionReason.ACCEPTED),
            lambda: DecisionEvaluation(
                True,
                DecisionAdmissionReason.ACCEPTED,
                observation,
                idempotent=True,
            ),
        )
        for build in invalid_evaluations:
            with self.assertRaises((TypeError, ValueError)):
                build()

        with self.assertRaises(TypeError):
            EvidenceSet([evidence_ref()])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            EvidenceSet((object(),))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            EvidenceSet((evidence_ref(), evidence_ref()))

    def test_direct_effect_outcome_and_payload_defenses_are_closed(self) -> None:
        reference = evidence_ref()
        producer = reference.producer
        receipt = applied_receipt()
        effect_request = EffectRequestPayload(
            EffectOperation.WORKER_DISPATCH,
            AdapterKind.TASK,
            EffectObjectType.WORKER,
            hash_ref(1),
            hash_ref(2),
            1,
            1,
        )
        transition = TransitionPayload(
            TransitionSubject.RUN, RuntimeState.NONE, RuntimeState.PREFLIGHT
        )

        cases = (
            lambda: EvidenceProducer(object(), "EVENT-001"),
            lambda: EvidenceProducer(attempt_identity()),
            lambda: RetryMetadata(3, 2, NOW),
            lambda: BudgetCharge("attempts", 1, BudgetDisposition.RESERVED),
            lambda: BudgetCharge(BudgetDimension.ATTEMPTS, 1, "reserved"),
            lambda: dataclasses.replace(receipt, identity=ExecutionIdentity("RUN-001", 1)),
            lambda: dataclasses.replace(
                receipt,
                identity=ExecutionIdentity("RUN-001", 1, "TASK-001", 1),
            ),
            lambda: dataclasses.replace(receipt, operation="worker_dispatch"),
            lambda: dataclasses.replace(receipt, status="applied"),
            lambda: EffectReceiptValue(object()),
            lambda: EvidenceSet(()),
            lambda: OperationOutcome(1, "success"),
            lambda: OperationOutcome(1, OutcomeKind.SUCCESS, reason_code="policy_denied"),
            lambda: OperationOutcome(1, OutcomeKind.SUCCESS, retry=object()),
            lambda: OperationOutcome(1, OutcomeKind.SUCCESS, budget_charge=object()),
            lambda: OperationOutcome(1, OutcomeKind.TERMINAL),
            lambda: OperationOutcome(
                1,
                OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.POLICY_DENIED,
                retry=RetryMetadata(1, 2, NOW),
                user_message_key="terminal",
            ),
            lambda: OperationOutcome(
                1,
                OutcomeKind.TERMINAL,
                reason_code=RuntimeReasonCode.POLICY_DENIED,
                user_message_key="Bad Key",
            ),
            lambda: dataclasses.replace(transition, subject="run"),
            lambda: dataclasses.replace(transition, from_state="none"),
            lambda: dataclasses.replace(transition, to_state=RuntimeState.NONE),
            lambda: DecisionRequestPayload(object()),
            lambda: DecisionObservedPayload(object()),
            lambda: dataclasses.replace(effect_request, operation="worker_dispatch"),
            lambda: dataclasses.replace(effect_request, adapter="task"),
            lambda: dataclasses.replace(effect_request, object_type="worker"),
            lambda: EffectObservationPayload("task", receipt),
            lambda: EffectObservationPayload(AdapterKind.TASK, object()),
            lambda: RecoveryPayload(0, ZERO_HASH, [], ()),
            lambda: RecoveryPayload(0, ZERO_HASH, ("TASK-001", "TASK-001"), ()),
        )
        self.assertEqual("EVENT-010", producer.event_id)
        for build in cases:
            with self.subTest(build=repr(build)), self.assertRaises((TypeError, ValueError)):
                build()

    def test_journal_identity_binding_rejects_stale_cross_entity_data(self) -> None:
        base = transition_event()
        receipt = applied_receipt()
        with self.assertRaises(ValueError):
            dataclasses.replace(base, previous_event_hash=hash_ref(1))

        def event_for(
            event_type: JournalEventType,
            identity: ExecutionIdentity,
            payload: object,
            *,
            sequence: int = 2,
        ) -> JournalEvent:
            return JournalEvent.create(
                sequence=sequence,
                event_id="EVENT-099",
                event_type=event_type,
                identity=identity,
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at=NOW,
                previous_event_hash=hash_ref(1),
                payload=payload,  # type: ignore[arg-type]
            )

        invalid = (
            lambda: event_for(
                JournalEventType.RUN_PAUSED,
                ExecutionIdentity("RUN-001", 1),
                TransitionPayload(
                    TransitionSubject.TASK, RuntimeState.PAUSING, RuntimeState.PAUSED
                ),
            ),
            lambda: event_for(
                JournalEventType.RUN_PAUSED,
                ExecutionIdentity("RUN-001", 1, "TASK-001"),
                TransitionPayload(
                    TransitionSubject.RUN, RuntimeState.PAUSING, RuntimeState.PAUSED
                ),
            ),
            lambda: event_for(
                JournalEventType.TASK_READY,
                ExecutionIdentity("RUN-001", 1),
                TransitionPayload(
                    TransitionSubject.TASK, RuntimeState.APPROVED, RuntimeState.READY
                ),
            ),
            lambda: event_for(
                JournalEventType.TASK_READY,
                attempt_identity(),
                TransitionPayload(
                    TransitionSubject.TASK, RuntimeState.APPROVED, RuntimeState.READY
                ),
            ),
            lambda: event_for(
                JournalEventType.ATTEMPT_RESERVED,
                ExecutionIdentity("RUN-001", 1, "TASK-001"),
                TransitionPayload(
                    TransitionSubject.ATTEMPT, RuntimeState.PLANNED, RuntimeState.RESERVED
                ),
            ),
            lambda: event_for(
                JournalEventType.DECISION_REQUESTED,
                ExecutionIdentity("RUN-001", 1),
                DecisionRequestPayload(decision_request()),
                sequence=9,
            ),
            lambda: event_for(
                JournalEventType.DECISION_OBSERVED,
                ExecutionIdentity("RUN-001", 1),
                DecisionObservedPayload(decision_observation()),
                sequence=10,
            ),
            lambda: event_for(
                JournalEventType.DISPATCH_OBSERVED,
                dataclasses.replace(attempt_identity(), correlation_id="CORRELATION-999"),
                EffectObservationPayload(AdapterKind.TASK, receipt),
            ),
        )
        for build in invalid:
            with self.subTest(build=repr(build)), self.assertRaises((TypeError, ValueError)):
                build()

    def test_decoder_boundary_matrix_returns_stable_typed_diagnostics(self) -> None:
        command = decision_request().command.to_primitive()
        request = decision_request().to_primitive()
        reference = evidence_ref().to_primitive()
        outcome = OperationOutcome(1, OutcomeKind.SUCCESS, Acknowledgement()).to_primitive()
        cases = (
            (decode_actor_identity_bytes, human_actor().to_primitive(), "actor_type", 1),
            (decode_command_identity_bytes, command, "request_nonce", "bad token!"),
            (decode_decision_request_bytes, request, "options", []),
            (decode_evidence_ref_bytes, reference, "byte_length", -1),
            (decode_operation_outcome_bytes, outcome, "kind", "invented"),
        )
        for decoder, primitive, field, invalid in cases:
            with self.subTest(field=field):
                candidate = copy.deepcopy(primitive)
                candidate[field] = invalid
                result = decoder(canonical_json_bytes(candidate))
                self.assertFalse(result.ok)
                self.assertIsNone(result.value)
                self.assertEqual(result.diagnostic_bytes(), decoder(canonical_json_bytes(candidate)).diagnostic_bytes())

        wrong_root = decode_evidence_ref_primitive([])
        self.assertFalse(wrong_root.ok)
        self.assertEqual("schema.object_type", wrong_root.issues[0].rule_id)
        wrong_raw = decode_evidence_ref_bytes("{}")  # type: ignore[arg-type]
        self.assertFalse(wrong_raw.ok)
        self.assertIsNone(wrong_raw.source_sha256)
        with self.assertRaises(TypeError):
            decode_evidence_ref_primitive({}, limits=None)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            decode_evidence_ref_bytes(b"{}", limits=None)  # type: ignore[arg-type]
        with patch(
            "wish_builder.contracts.runtime_decoder.json.loads",
            side_effect=RecursionError,
        ):
            recursion = decode_evidence_ref_bytes(b"{}")
        self.assertEqual("json.depth_limit", recursion.issues[0].rule_id)

    def test_decoder_semantic_invariant_matrix_rejects_valid_looking_shapes(self) -> None:
        def rejected(decoder: object, primitive: object) -> None:
            result = decoder(primitive)  # type: ignore[operator]
            self.assertFalse(result.ok)
            self.assertIsNone(result.value)

        identity = attempt_identity().to_primitive()
        identity["task_id"] = None
        rejected(decode_execution_identity_bytes, canonical_json_bytes(identity))

        request = decision_request().to_primitive()
        request["command"]["kind"] = "pause"  # type: ignore[index]
        rejected(decode_decision_request_primitive, request)

        command = decision_command().to_primitive()
        command["actor"]["actor_id"] = "other-account"  # type: ignore[index]
        rejected(decode_decision_command_bytes, canonical_json_bytes(command))

        observation = decision_observation().to_primitive()
        observation["submission_hash"] = hash_ref(999)
        rejected(decode_decision_observation_bytes, canonical_json_bytes(observation))

        reference = evidence_ref(
            sensitivity=EvidenceSensitivity.SENSITIVE
        ).to_primitive()
        reference["render_policy"] = "text"
        rejected(decode_evidence_ref_primitive, reference)

        receipt_cases = []
        absent_with_effect = applied_receipt().to_primitive()
        absent_with_effect["status"] = "absent"
        receipt_cases.append(absent_with_effect)
        unknown_without_evidence = applied_receipt().to_primitive()
        unknown_without_evidence.update(
            status="unknown", effect_hash=None, external_object_id=None, evidence=[]
        )
        receipt_cases.append(unknown_without_evidence)
        no_correlation = applied_receipt().to_primitive()
        no_correlation["identity"]["correlation_id"] = None  # type: ignore[index]
        receipt_cases.append(no_correlation)
        for primitive in receipt_cases:
            rejected(decode_effect_receipt_bytes, canonical_json_bytes(primitive))

        retry_over_ceiling = OperationOutcome(
            1,
            OutcomeKind.RETRYABLE,
            reason_code=RuntimeReasonCode.RATE_LIMITED,
            retry=RetryMetadata(1, 2, NOW),
            user_message_key="retry.later",
        ).to_primitive()
        retry_over_ceiling["retry"]["attempt"] = 3  # type: ignore[index]
        rejected(decode_operation_outcome_primitive, retry_over_ceiling)

        terminal_missing_reason = OperationOutcome(
            1,
            OutcomeKind.TERMINAL,
            reason_code=RuntimeReasonCode.POLICY_DENIED,
            user_message_key="terminal.policy",
        ).to_primitive()
        terminal_missing_reason["reason_code"] = None
        rejected(decode_operation_outcome_primitive, terminal_missing_reason)

        same_state = OperationOutcome(1, OutcomeKind.SUCCESS).to_primitive()
        same_state["value"] = {
            "type": "journal_position",
            "sequence": 0,
            "event_id": "bad",
            "event_hash": "bad",
        }
        rejected(decode_operation_outcome_primitive, same_state)

    def test_decoder_closed_collection_and_variant_matrix(self) -> None:
        def outcome_with(value: object) -> dict[str, object]:
            primitive = OperationOutcome(1, OutcomeKind.SUCCESS).to_primitive()
            primitive["value"] = value
            return primitive

        invalid_values = (
            [],
            {},
            {"type": "invented"},
            {"type": "acknowledgement", "extra": True},
            {"type": "identity"},
            {"type": "identity", "identifier": "bad token!"},
            {
                "type": "journal_position",
                "sequence": 0,
                "event_id": "EVENT-001",
                "event_hash": hash_ref(1),
            },
            {"type": "effect_receipt", "receipt": []},
            {"type": "evidence_set", "evidence": []},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                result = decode_operation_outcome_primitive(outcome_with(value))
                self.assertFalse(result.ok)

        retryable = OperationOutcome(
            1,
            OutcomeKind.RETRYABLE,
            reason_code=RuntimeReasonCode.RATE_LIMITED,
            retry=RetryMetadata(1, 2, NOW),
            budget_charge=BudgetCharge(
                BudgetDimension.ATTEMPTS, 1, BudgetDisposition.RESERVED
            ),
            user_message_key="retry.later",
        ).to_primitive()
        nested_invalid = (
            ("evidence", {}),
            ("retry", []),
            ("budget_charge", []),
            ("user_message_key", "Bad Key"),
        )
        for field, invalid in nested_invalid:
            with self.subTest(field=field):
                candidate = copy.deepcopy(retryable)
                candidate[field] = invalid
                self.assertFalse(decode_operation_outcome_primitive(candidate).ok)

        oversized = copy.deepcopy(retryable)
        oversized["evidence"] = [
            evidence_ref(index + 1).to_primitive()
            for index in range(MAX_EVIDENCE_REFS + 1)
        ]
        self.assertFalse(decode_operation_outcome_primitive(oversized).ok)
        duplicate = copy.deepcopy(retryable)
        duplicate["evidence"] = [evidence_ref().to_primitive()] * 2
        result = decode_operation_outcome_primitive(duplicate)
        self.assertIn(
            "value.duplicate_evidence_digest",
            {issue.rule_id for issue in result.issues},
        )

    def test_decoder_decision_option_and_recovery_collection_matrix(self) -> None:
        request = decision_request().to_primitive()
        option_cases = (
            None,
            {},
            [],
            ["approve", "approve"],
            ["invented"],
            ["approve"] * 17,
        )
        for options in option_cases:
            with self.subTest(options=options):
                candidate = copy.deepcopy(request)
                if options is None:
                    del candidate["options"]
                else:
                    candidate["options"] = options
                self.assertFalse(decode_decision_request_primitive(candidate).ok)

        event = JournalEvent.create(
            sequence=2,
            event_id="EVENT-002",
            event_type=JournalEventType.RECOVERY_STARTED,
            identity=ExecutionIdentity("RUN-001", 4),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-002",
            recorded_at=NOW,
            previous_event_hash=hash_ref(1),
            payload=RecoveryPayload(1, hash_ref(1), ("TASK-001",), ()),
        ).to_primitive()
        affected_cases = (
            {},
            ["bad"],
            ["TASK-001", "TASK-001"],
            [f"TASK-{index:03d}" for index in range(MAX_AFFECTED_IDENTITIES + 1)],
        )
        for affected in affected_cases:
            with self.subTest(affected_type=type(affected).__name__):
                candidate = copy.deepcopy(event)
                candidate["payload"]["affected_identities"] = affected  # type: ignore[index]
                self.assertFalse(decode_journal_event_primitive(candidate).ok)

    def test_decoder_payload_dispatch_rejects_every_untrusted_shape(self) -> None:
        event = transition_event().to_primitive()
        payload_cases = (
            [],
            {},
            {"payload_type": 1},
            {"payload_type": "invented"},
            {"payload_type": "transition"},
            {"payload_type": "effect_request"},
            {"payload_type": "effect_observation"},
            {"payload_type": "recovery"},
            {"payload_type": "decision_request", "request": []},
            {"payload_type": "decision_observed", "observation": []},
        )
        for payload in payload_cases:
            with self.subTest(payload=payload):
                candidate = copy.deepcopy(event)
                candidate["payload"] = payload
                self.assertFalse(decode_journal_event_primitive(candidate).ok)

        transition = transition_event().to_primitive()
        transition["payload"]["to_state"] = "none"  # type: ignore[index]
        transition["payload_hash"] = "sha256:" + canonical_sha256(transition["payload"])
        unsigned = {key: value for key, value in transition.items() if key != "event_hash"}
        transition["event_hash"] = "sha256:" + canonical_sha256(unsigned)
        result = decode_journal_event_primitive(transition)
        self.assertFalse(result.ok)
        self.assertIn("value.runtime_contract", {issue.rule_id for issue in result.issues})

    def test_every_public_decoder_entry_point_is_exercised(self) -> None:
        primitives_and_decoders = (
            (
                human_actor(),
                decode_actor_identity_primitive,
                decode_actor_identity_bytes,
            ),
            (
                decision_request(),
                decode_decision_request_primitive,
                decode_decision_request_bytes,
            ),
        )
        for value, primitive_decoder, byte_decoder in primitives_and_decoders:
            with self.subTest(value=type(value).__name__):
                self.assertTrue(primitive_decoder(value.to_primitive()).ok)
                self.assertTrue(byte_decoder(value.canonical_json_bytes()).ok)

        cyclic: list[object] = []
        cyclic.append(cyclic)
        result = decode_actor_identity_primitive(cyclic)
        self.assertFalse(result.ok)
        self.assertIn("json.cyclic_shape", {issue.rule_id for issue in result.issues})

        self.assertEqual(7, runtime_decoder._record_segment(object(), 7))
        self.assertRegex(
            str(runtime_decoder._record_segment({"other": "value"}, 0)),
            r"^@[0-9a-f]{24}$",
        )
        issues: list[object] = []
        self.assertIsNone(
            runtime_decoder._enum_value(
                runtime_decoder._MISSING,
                ActorType,
                (),
                issues,
            )
        )


if __name__ == "__main__":
    unittest.main()
