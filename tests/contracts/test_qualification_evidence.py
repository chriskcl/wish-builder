from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest

from wish_builder.contracts import (
    QUALIFICATION_EVENT_GENESIS_DIGEST,
    QUALIFICATION_PROVENANCE_SUBJECT_ROLE_ORDER,
    QUALIFICATION_SCENARIO_ORDER,
    AttemptPreparedPayload,
    CancelObservedPayload,
    CancelRequestedPayload,
    ChannelReservedPayload,
    CleanupObservedPayload,
    CleanupRequestedPayload,
    CrashInjectedPayload,
    DecodeLimits,
    Platform,
    PrepareRequestedPayload,
    ProcessRestartedPayload,
    Provider,
    QualificationEffectStatus,
    QualificationEvent,
    QualificationEventSource,
    QualificationEventType,
    QualificationEvidenceArtifact,
    QualificationEvidenceInventory,
    QualificationEvidenceRole,
    QualificationEvidenceScenario,
    QualificationHarnessDescriptor,
    QualificationProvenance,
    QualificationProvenanceKind,
    QualificationProvenanceSubject,
    QualificationRunOutcome,
    QualificationTurnState,
    QualificationTurnTerminalState,
    ReconcileInspectedPayload,
    ReconcileRequestedPayload,
    ReserveRequestedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    SendRequestedPayload,
    TaskPacketSentPayload,
    TurnStartedPayload,
    TurnTerminalPayload,
    canonical_json_bytes,
    canonical_sha256,
    decode_qualification_event_bytes,
    decode_qualification_event_log_bytes,
    decode_qualification_evidence_inventory_bytes,
    decode_qualification_harness_descriptor_bytes,
    decode_qualification_provenance_bytes,
    qualification_event_digest,
    qualification_event_log_bytes,
    validate_qualification_provenance_binding,
)
from wish_builder.contracts.diagnostics import ReasonCode


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REVISION = "c" * 40
TIMESTAMP = "2026-08-20T12:34:56Z"


def _payloads() -> tuple[object, ...]:
    task_packet = '{"kind":"qualification-fixture"}\n'
    task_packet_digest = "sha256:" + hashlib.sha256(
        task_packet.encode("utf-8")
    ).hexdigest()
    turn = {
        "dispatch_id": "dispatch-1",
        "attempt_id": "attempt-1",
        "task_id": "TASK-1",
        "channel_id": "channel-1",
        "provider_session_id": "session-1",
        "provider_message_id": "message-1",
        "provider_turn_id": "turn-1",
    }
    return (
        RunStartedPayload(
            source_revision=REVISION,
            harness_digest=DIGEST_A,
            harness_version="1.0.0",
            trellis_version="0.6.15",
            trellis_compatibility_digest=DIGEST_A,
            policy_digest=DIGEST_A,
            launch_profile_digest=DIGEST_A,
            capability_digest=DIGEST_A,
            manifest_digest=DIGEST_A,
            trellis_snapshot_digest=DIGEST_A,
            sdk_name="provider-sdk",
            sdk_version="1.2.3",
            sdk_shasum="d" * 40,
        ),
        PrepareRequestedPayload(
            operation_id="operation-prepare",
            dispatch_id="dispatch-1",
            attempt_id="attempt-1",
            task_id="TASK-1",
            trellis_task_id="trellis-task-1",
            worktree_id="worktree-1",
            base_commit=REVISION,
            owned_paths=("src/one.py",),
        ),
        AttemptPreparedPayload(
            operation_id="operation-prepare",
            dispatch_id="dispatch-1",
            attempt_id="attempt-1",
            task_id="TASK-1",
            trellis_task_id="trellis-task-1",
            worktree_id="worktree-1",
            base_commit=REVISION,
            owned_paths=("src/one.py",),
        ),
        ReserveRequestedPayload(
            operation_id="operation-reserve",
            dispatch_id="dispatch-1",
            attempt_id="attempt-1",
            task_id="TASK-1",
            channel_id="channel-1",
        ),
        ChannelReservedPayload(
            operation_id="operation-reserve",
            dispatch_id="dispatch-1",
            attempt_id="attempt-1",
            task_id="TASK-1",
            channel_id="channel-1",
            provider_session_id="session-1",
        ),
        SendRequestedPayload(
            operation_id="operation-send",
            dispatch_id="dispatch-1",
            attempt_id="attempt-1",
            task_id="TASK-1",
            channel_id="channel-1",
            task_packet=task_packet,
            task_packet_digest=task_packet_digest,
        ),
        TaskPacketSentPayload(
            operation_id="operation-send",
            dispatch_id="dispatch-1",
            attempt_id="attempt-1",
            task_id="TASK-1",
            channel_id="channel-1",
            provider_session_id="session-1",
            provider_message_id="message-1",
            task_packet=task_packet,
            task_packet_digest=task_packet_digest,
        ),
        TurnStartedPayload(**turn),
        TurnTerminalPayload(
            **turn,
            terminal_state=QualificationTurnTerminalState.DONE,
            result_digest=DIGEST_B,
        ),
        CancelRequestedPayload(operation_id="operation-cancel", **turn),
        CancelObservedPayload(
            operation_id="operation-cancel",
            **turn,
            effect_status=QualificationEffectStatus.APPLIED,
        ),
        CrashInjectedPayload(
            failpoint="after-send",
            operation_id="operation-send",
            **turn,
        ),
        ProcessRestartedPayload(
            previous_process_identity="process-1",
            recovery_id="recovery-1",
        ),
        ReconcileRequestedPayload(
            operation_id="operation-send",
            request_digest=DIGEST_A,
            **turn,
        ),
        ReconcileInspectedPayload(
            operation_id="operation-send",
            request_digest=DIGEST_A,
            **turn,
            effect_status=QualificationEffectStatus.APPLIED,
            turn_state=QualificationTurnState.DONE,
            result_digest=DIGEST_B,
        ),
        CleanupRequestedPayload(
            operation_id="operation-cleanup",
            dispatch_id="dispatch-1",
            attempt_id="attempt-1",
            task_id="TASK-1",
            channel_id="channel-1",
            provider_session_id="session-1",
            worktree_id="worktree-1",
            process_tree_ids=("process-provider-1", "process-provider-child-1"),
        ),
        CleanupObservedPayload(
            operation_id="operation-cleanup",
            dispatch_id="dispatch-1",
            attempt_id="attempt-1",
            task_id="TASK-1",
            channel_id="channel-1",
            provider_session_id="session-1",
            worktree_id="worktree-1",
            process_tree_ids=("process-provider-1", "process-provider-child-1"),
            resources_before=(
                "channel:channel-1",
                "channel:channel-sibling",
                "process:process-provider-1",
                "process:process-provider-child-1",
                "provider_session:session-1",
                "provider_session:session-sibling",
                "worktree:worktree-1",
                "worktree:worktree-sibling",
            ),
            resources_after=(
                "channel:channel-sibling",
                "provider_session:session-sibling",
                "worktree:worktree-sibling",
            ),
        ),
        RunFinishedPayload(outcome=QualificationRunOutcome.COMPLETED),
    )


def _events() -> tuple[QualificationEvent, ...]:
    result: list[QualificationEvent] = []
    previous = QUALIFICATION_EVENT_GENESIS_DIGEST
    for sequence, payload in enumerate(_payloads(), start=1):
        event = QualificationEvent.create(
            sequence=sequence,
            qualification_run_id="qualification-run-1",
            scenario=QualificationEvidenceScenario.FULL_TURN,
            provider=Provider.CODEX,
            platform=Platform.WINDOWS,
            source=QualificationEventSource.RUNNER,
            event_type=payload.EVENT_TYPE,
            recorded_at=TIMESTAMP,
            monotonic_ns=sequence * 100,
            host_boot_id="boot-1",
            process_identity="process-1" if sequence < 13 else "process-2",
            payload=payload,
            previous_event_digest=previous,
        )
        result.append(event)
        previous = event.event_digest
    return tuple(result)


def _evidence_objects() -> tuple[
    QualificationEvidenceInventory,
    QualificationHarnessDescriptor,
    QualificationProvenance,
]:
    harness = QualificationHarnessDescriptor(
        schema_version=1,
        harness_version="1.0.0",
        source_revision=REVISION,
        entrypoint="scripts/backend_qualification.py",
        event_schema_version=1,
        scenarios=QUALIFICATION_SCENARIO_ORDER,
    )
    raw_by_role = {
        QualificationEvidenceRole.EVENT_LOG: qualification_event_log_bytes(_events()),
        QualificationEvidenceRole.HARNESS_DESCRIPTOR: harness.canonical_json_bytes(),
        QualificationEvidenceRole.EXECUTION_MANIFEST: b'{"manifest":"fixture"}\n',
        QualificationEvidenceRole.TRELLIS_SNAPSHOT: b'{"snapshot":"fixture"}\n',
    }
    subject_artifacts = tuple(
        QualificationEvidenceArtifact(
            role=role,
            path=f"evidence/{role.value}.jsonl"
            if role is QualificationEvidenceRole.EVENT_LOG
            else f"evidence/{role.value}.json",
            digest="sha256:" + canonical_sha256({"raw": raw_by_role[role].hex()}),
            byte_length=len(raw_by_role[role]),
            media_type="application/x-ndjson"
            if role is QualificationEvidenceRole.EVENT_LOG
            else "application/json",
        )
        for role in QUALIFICATION_PROVENANCE_SUBJECT_ROLE_ORDER
    )
    provenance = QualificationProvenance(
        schema_version=1,
        kind=QualificationProvenanceKind.GITHUB_ACTIONS,
        issuer="https://token.actions.githubusercontent.com",
        reference="https://github.com/example/wish-builder/actions/runs/1",
        identity="example/wish-builder/.github/workflows/qualification.yml@refs/heads/main",
        source_revision=REVISION,
        subjects=tuple(
            QualificationProvenanceSubject.from_artifact(item)
            for item in subject_artifacts
        ),
    )
    artifacts = subject_artifacts + (
        QualificationEvidenceArtifact(
            role=QualificationEvidenceRole.PROVENANCE,
            path="evidence/provenance.json",
            digest=provenance.digest(),
            byte_length=len(provenance.canonical_json_bytes()),
            media_type="application/json",
        ),
    )
    inventory = QualificationEvidenceInventory(
        schema_version=1,
        qualification_run_id="qualification-run-1",
        provider=Provider.CODEX,
        platform=Platform.WINDOWS,
        artifacts=artifacts,
    )
    return inventory, harness, provenance


class QualificationEventContractTests(unittest.TestCase):
    def test_every_closed_payload_round_trips_through_canonical_jsonl(self) -> None:
        events = _events()
        self.assertEqual(tuple(QualificationEventType), tuple(event.event_type for event in events))

        raw = qualification_event_log_bytes(events)
        decoded = decode_qualification_event_log_bytes(raw)

        self.assertTrue(decoded.ok, decoded.report.render_text())
        self.assertEqual(events, decoded.value)
        self.assertEqual(raw, b"".join(event.canonical_json_bytes() for event in events))
        for event in events:
            self.assertEqual(event.event_digest, qualification_event_digest(event))

    def test_event_decoder_rejects_unknown_fields_wrong_types_and_schema(self) -> None:
        primitive = _events()[0].to_primitive()
        cases = []
        unknown = dict(primitive)
        unknown["claimPassed"] = True
        cases.append((unknown, ReasonCode.UNKNOWN_FIELD))
        wrong_type = dict(primitive)
        wrong_type["sequence"] = True
        wrong_type["eventDigest"] = qualification_event_digest(wrong_type)
        cases.append((wrong_type, ReasonCode.WRONG_PRIMITIVE_TYPE))
        old_schema = dict(primitive)
        old_schema["schemaVersion"] = 2
        old_schema["eventDigest"] = qualification_event_digest(old_schema)
        cases.append((old_schema, ReasonCode.UNSUPPORTED_SCHEMA_VERSION))

        for value, reason in cases:
            with self.subTest(reason=reason):
                result = decode_qualification_event_bytes(canonical_json_bytes(value))
                self.assertFalse(result.ok)
                self.assertEqual(reason, result.issues[0].reason_code)

    def test_event_payload_is_closed_and_bound_to_event_type(self) -> None:
        primitive = _events()[1].to_primitive()
        primitive["payload"]["unexpected"] = "value"
        primitive["eventDigest"] = qualification_event_digest(primitive)
        result = decode_qualification_event_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.UNKNOWN_FIELD, result.issues[0].reason_code)

        primitive = _events()[1].to_primitive()
        primitive["eventType"] = "run_finished"
        primitive["eventDigest"] = qualification_event_digest(primitive)
        result = decode_qualification_event_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.UNKNOWN_FIELD, result.issues[0].reason_code)

    def test_duplicate_keys_and_noncanonical_event_lines_are_rejected(self) -> None:
        raw = _events()[0].canonical_json_bytes()
        digest = _events()[0].event_digest.encode()
        duplicate = raw.replace(
            b'"eventDigest":' + json.dumps(_events()[0].event_digest).encode(),
            b'"eventDigest":"' + digest + b'","eventDigest":' + json.dumps(_events()[0].event_digest).encode(),
            1,
        )
        result = decode_qualification_event_log_bytes(duplicate)
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.DUPLICATE_OBJECT_KEY, result.issues[0].reason_code)

        noncanonical = json.dumps(_events()[0].to_primitive(), sort_keys=True).encode() + b"\n"
        result = decode_qualification_event_log_bytes(noncanonical)
        self.assertFalse(result.ok)
        self.assertIn("noncanonical_bytes", result.issues[0].rule_id)

    def test_event_and_chain_digest_tampering_fail_closed(self) -> None:
        events = _events()
        primitive = events[0].to_primitive()
        primitive["payload"]["harnessVersion"] = "1.0.1"
        result = decode_qualification_event_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.INVALID_HASH, result.issues[0].reason_code)

        second = QualificationEvent.create(
            sequence=2,
            qualification_run_id=events[0].qualification_run_id,
            scenario=events[0].scenario,
            provider=events[0].provider,
            platform=events[0].platform,
            source=events[0].source,
            event_type=events[1].event_type,
            recorded_at=events[1].recorded_at,
            monotonic_ns=events[1].monotonic_ns,
            host_boot_id=events[1].host_boot_id,
            process_identity=events[1].process_identity,
            payload=events[1].payload,
            previous_event_digest=DIGEST_B,
        )
        result = decode_qualification_event_log_bytes(
            events[0].canonical_json_bytes() + second.canonical_json_bytes()
        )
        self.assertFalse(result.ok)
        self.assertIn("hash_chain", result.issues[0].rule_id)

    def test_sequence_identity_monotonic_and_terminal_newline_are_enforced(self) -> None:
        first = _events()[0]
        gap = QualificationEvent.create(
            sequence=3,
            qualification_run_id=first.qualification_run_id,
            scenario=first.scenario,
            provider=first.provider,
            platform=first.platform,
            source=first.source,
            event_type=_events()[1].event_type,
            recorded_at=TIMESTAMP,
            monotonic_ns=200,
            host_boot_id=first.host_boot_id,
            process_identity=first.process_identity,
            payload=_events()[1].payload,
            previous_event_digest=first.event_digest,
        )
        result = decode_qualification_event_log_bytes(
            first.canonical_json_bytes() + gap.canonical_json_bytes()
        )
        self.assertFalse(result.ok)
        self.assertIn("event_sequence", result.issues[0].rule_id)

        drift = QualificationEvent.create(
            sequence=2,
            qualification_run_id="qualification-run-2",
            scenario=first.scenario,
            provider=first.provider,
            platform=first.platform,
            source=first.source,
            event_type=_events()[1].event_type,
            recorded_at=TIMESTAMP,
            monotonic_ns=50,
            host_boot_id=first.host_boot_id,
            process_identity=first.process_identity,
            payload=_events()[1].payload,
            previous_event_digest=first.event_digest,
        )
        result = decode_qualification_event_log_bytes(
            first.canonical_json_bytes() + drift.canonical_json_bytes()
        )
        self.assertFalse(result.ok)
        self.assertIn("event_log_identity", result.issues[0].rule_id)

        same_clock = QualificationEvent.create(
            sequence=2,
            qualification_run_id=first.qualification_run_id,
            scenario=QualificationEvidenceScenario.CLEANUP,
            provider=first.provider,
            platform=first.platform,
            source=first.source,
            event_type=_events()[1].event_type,
            recorded_at=TIMESTAMP,
            monotonic_ns=first.monotonic_ns,
            host_boot_id=first.host_boot_id,
            process_identity=first.process_identity,
            payload=_events()[1].payload,
            previous_event_digest=first.event_digest,
        )
        result = decode_qualification_event_log_bytes(
            first.canonical_json_bytes() + same_clock.canonical_json_bytes()
        )
        self.assertFalse(result.ok)
        self.assertIn("monotonic_order", result.issues[0].rule_id)

        next_scenario = QualificationEvent.create(
            sequence=2,
            qualification_run_id=first.qualification_run_id,
            scenario=QualificationEvidenceScenario.CLEANUP,
            provider=first.provider,
            platform=first.platform,
            source=first.source,
            event_type=_events()[1].event_type,
            recorded_at=TIMESTAMP,
            monotonic_ns=first.monotonic_ns + 1,
            host_boot_id=first.host_boot_id,
            process_identity=first.process_identity,
            payload=_events()[1].payload,
            previous_event_digest=first.event_digest,
        )
        result = decode_qualification_event_log_bytes(
            first.canonical_json_bytes() + next_scenario.canonical_json_bytes()
        )
        self.assertTrue(result.ok, result.report.render_text())

        result = decode_qualification_event_log_bytes(first.canonical_json_bytes()[:-1])
        self.assertFalse(result.ok)
        self.assertIn("terminal_newline", result.issues[0].rule_id)

    def test_jsonl_limits_blank_lines_and_floats_are_rejected(self) -> None:
        raw = _events()[0].canonical_json_bytes()
        result = decode_qualification_event_log_bytes(
            raw,
            limits=DecodeLimits(max_bytes=len(raw) - 1),
        )
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.BYTE_LIMIT_EXCEEDED, result.issues[0].reason_code)

        result = decode_qualification_event_log_bytes(raw + b"\n")
        self.assertFalse(result.ok)
        self.assertIn("blank_line", result.issues[0].rule_id)

        floating = raw.replace(b'"monotonicNs":100', b'"monotonicNs":1.0')
        result = decode_qualification_event_log_bytes(floating)
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.FLOAT_NOT_ALLOWED, result.issues[0].reason_code)


class QualificationEvidenceDocumentTests(unittest.TestCase):
    def test_inventory_harness_and_detached_provenance_round_trip(self) -> None:
        inventory, harness, provenance = _evidence_objects()
        decoded_inventory = decode_qualification_evidence_inventory_bytes(
            inventory.canonical_json_bytes()
        )
        decoded_harness = decode_qualification_harness_descriptor_bytes(
            harness.canonical_json_bytes()
        )
        decoded_provenance = decode_qualification_provenance_bytes(
            provenance.canonical_json_bytes()
        )

        self.assertTrue(decoded_inventory.ok, decoded_inventory.report.render_text())
        self.assertTrue(decoded_harness.ok, decoded_harness.report.render_text())
        self.assertTrue(decoded_provenance.ok, decoded_provenance.report.render_text())
        self.assertEqual(inventory, decoded_inventory.value)
        self.assertEqual(harness, decoded_harness.value)
        self.assertEqual(provenance, decoded_provenance.value)
        binding = validate_qualification_provenance_binding(inventory, provenance)
        self.assertTrue(binding.ok, binding.report.render_text())

    def test_inventory_requires_exact_roles_order_and_closed_entries(self) -> None:
        inventory, _, _ = _evidence_objects()
        primitive = inventory.to_primitive()
        primitive["artifacts"] = list(reversed(primitive["artifacts"]))
        result = decode_qualification_evidence_inventory_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.INVALID_MANIFEST, result.issues[0].reason_code)

        primitive = inventory.to_primitive()
        primitive["artifacts"][0]["claim"] = True
        result = decode_qualification_evidence_inventory_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.UNKNOWN_FIELD, result.issues[0].reason_code)

    def test_provenance_requires_https_and_exact_four_subject_roles(self) -> None:
        _, _, provenance = _evidence_objects()
        primitive = provenance.to_primitive()
        primitive["issuer"] = "http://token.actions.githubusercontent.com"
        result = decode_qualification_provenance_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)

        primitive = provenance.to_primitive()
        primitive["subjects"][3]["role"] = "provenance"
        result = decode_qualification_provenance_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)

    def test_provenance_subject_digest_and_inventory_digest_must_match(self) -> None:
        inventory, _, provenance = _evidence_objects()
        subjects = list(provenance.subjects)
        subjects[0] = dataclasses.replace(subjects[0], digest=DIGEST_B)
        drifted = dataclasses.replace(provenance, subjects=tuple(subjects))
        result = validate_qualification_provenance_binding(inventory, drifted)
        self.assertFalse(result.ok)
        self.assertIn("subject_binding", result.issues[0].rule_id)

        artifacts = list(inventory.artifacts)
        artifacts[-1] = dataclasses.replace(artifacts[-1], digest=DIGEST_B)
        wrong_inventory = dataclasses.replace(inventory, artifacts=tuple(artifacts))
        result = validate_qualification_provenance_binding(wrong_inventory, provenance)
        self.assertFalse(result.ok)
        self.assertIn("inventory_digest", result.issues[0].rule_id)

    def test_document_decoders_reject_duplicates_unknown_fields_and_bounds(self) -> None:
        inventory, _, provenance = _evidence_objects()
        raw = inventory.canonical_json_bytes()
        duplicate = raw.replace(
            b'"platform":"windows"',
            b'"platform":"windows","platform":"windows"',
        )
        result = decode_qualification_evidence_inventory_bytes(duplicate)
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.DUPLICATE_OBJECT_KEY, result.issues[0].reason_code)

        primitive = provenance.to_primitive()
        primitive["verified"] = True
        result = decode_qualification_provenance_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.UNKNOWN_FIELD, result.issues[0].reason_code)

        result = decode_qualification_provenance_bytes(
            provenance.canonical_json_bytes(),
            limits=DecodeLimits(max_bytes=8),
        )
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.BYTE_LIMIT_EXCEEDED, result.issues[0].reason_code)


if __name__ == "__main__":
    unittest.main()
