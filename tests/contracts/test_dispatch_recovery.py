from __future__ import annotations

import copy
import unittest
from dataclasses import replace

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
    SourceChannel,
    canonical_json_bytes,
    decode_dispatch_recovery_payload_bytes,
    decode_journal_event_bytes,
)

NOW = "2026-08-19T00:00:10Z"


def digest(character: str) -> str:
    return "sha256:" + character * 64


def subject() -> ExecutionIdentity:
    return ExecutionIdentity(
        "RUN-RECOVERY",
        1,
        "TASK-001",
        1,
        "CORRELATION-TASK-001-0001-EPOCH-0001",
    )


def human() -> ActorIdentity:
    return ActorIdentity(
        ActorType.HUMAN,
        "local-account-001",
        "host-001",
        4100,
        "process-start-human-001",
    )


def evidence(
    evidence_type: EvidenceType,
    character: str,
) -> EvidenceRef:
    identity = subject()
    return EvidenceRef(
        1,
        digest(character),
        64,
        evidence_type,
        EvidenceProducer(
            identity,
            event_id=(
                "EVENT-DISPATCH-REQUESTED-0001"
                if evidence_type is EvidenceType.EFFECT_RECEIPT
                else None
            ),
            external_object_id=(
                "process-tree-proof-001"
                if evidence_type is EvidenceType.PROCESS
                else None
            ),
        ),
        NOW,
        EvidenceSensitivity.INTERNAL,
        EvidenceRenderPolicy.METADATA_ONLY,
        EvidenceRole.REQUIRED,
    )


def recovery_payload(*, last_sequence: int = 40) -> DispatchRecoveryPayload:
    identity = subject()
    return DispatchRecoveryPayload(
        recovery_id="RECOVERY-DISPATCH-001",
        command=CommandIdentity(
            1,
            "COMMAND-RECONCILE-001",
            "REQUEST-RECONCILE-001",
            CommandKind.RECONCILE,
            last_sequence,
            "nonce-reconcile-001",
            human(),
            SourceChannel.DIRECT_CLI,
            NOW,
        ),
        subject_identity=identity,
        request_event_id="EVENT-DISPATCH-REQUESTED-0001",
        request_sequence=12,
        request_event_hash=digest("a"),
        receipt=EffectReceipt(
            1,
            identity,
            EffectOperation.WORKER_DISPATCH,
            EffectStatus.ABSENT,
            NOW,
        ),
        process_tree_termination_proven=True,
        last_valid_sequence=last_sequence,
        last_valid_event_hash=digest("b"),
        evidence=(
            evidence(EvidenceType.PROCESS, "d"),
            evidence(EvidenceType.EFFECT_RECEIPT, "c"),
        ),
    )


class DispatchRecoveryContractTests(unittest.TestCase):
    def test_payload_and_completed_event_round_trip_strictly(self) -> None:
        payload = recovery_payload()
        decoded_payload = decode_dispatch_recovery_payload_bytes(
            payload.canonical_json_bytes()
        )
        self.assertTrue(decoded_payload.ok, decoded_payload.report.render_text())
        self.assertEqual(payload, decoded_payload.value)

        event = JournalEvent.create(
            sequence=41,
            event_id="EVENT-RECOVERY-COMPLETED-0041",
            event_type=JournalEventType.RECOVERY_COMPLETED,
            identity=ExecutionIdentity("RUN-RECOVERY", 2),
            actor_type=ActorType.HUMAN,
            actor_id="local-account-001",
            recorded_at=NOW,
            previous_event_hash=digest("b"),
            payload=payload,
        )
        decoded_event = decode_journal_event_bytes(event.canonical_json_bytes())
        self.assertTrue(decoded_event.ok, decoded_event.report.render_text())
        self.assertEqual(event, decoded_event.value)

    def test_safe_retry_proof_is_strict_and_subject_bound(self) -> None:
        valid = recovery_payload()
        with self.assertRaises(TypeError):
            replace(valid, command=object())
        with self.assertRaises(ValueError):
            replace(valid, command=replace(valid.command, kind=CommandKind.RETRY))
        with self.assertRaises(ValueError):
            replace(
                valid,
                receipt=replace(
                    valid.receipt,
                    status=EffectStatus.APPLIED,
                    effect_hash=digest("e"),
                ),
            )
        with self.assertRaises(ValueError):
            replace(valid, process_tree_termination_proven=False)
        with self.assertRaises(ValueError):
            replace(
                valid,
                subject_identity=ExecutionIdentity("RUN-RECOVERY", 1, "TASK-001"),
            )
        with self.assertRaises(TypeError):
            replace(valid, receipt=object())
        with self.assertRaises(ValueError):
            replace(
                valid,
                receipt=replace(
                    valid.receipt,
                    operation=EffectOperation.PROCESS_TERMINATION,
                ),
            )
        with self.assertRaises(ValueError):
            replace(valid, evidence=(evidence(EvidenceType.PROCESS, "e"),))
        with self.assertRaises(ValueError):
            replace(valid, evidence=())
        with self.assertRaises(ValueError):
            replace(
                valid,
                command=replace(valid.command, source_channel=SourceChannel.CODEX_RELAY),
            )
        with self.assertRaises(ValueError):
            replace(
                valid,
                command=replace(
                    valid.command,
                    actor=replace(human(), actor_type=ActorType.COORDINATOR),
                ),
            )
        with self.assertRaises(ValueError):
            replace(
                valid,
                receipt=replace(
                    valid.receipt,
                    identity=replace(subject(), attempt=2),
                ),
            )
        with self.assertRaises(ValueError):
            replace(valid, request_sequence=valid.last_valid_sequence + 1)
        with self.assertRaises(ValueError):
            replace(
                valid,
                command=replace(
                    valid.command,
                    expected_sequence=valid.last_valid_sequence - 1,
                ),
            )
        wrong_producer = replace(
            valid.evidence[0],
            producer=replace(
                valid.evidence[0].producer,
                identity=replace(subject(), attempt=2),
            ),
        )
        with self.assertRaises(ValueError):
            replace(valid, evidence=(wrong_producer, valid.evidence[1]))

    def test_completed_event_requires_new_epoch_run_identity_and_head_anchor(self) -> None:
        payload = recovery_payload()
        common = {
            "sequence": 41,
            "event_id": "EVENT-RECOVERY-COMPLETED-0041",
            "event_type": JournalEventType.RECOVERY_COMPLETED,
            "actor_type": ActorType.HUMAN,
            "actor_id": "local-account-001",
            "recorded_at": NOW,
            "previous_event_hash": digest("b"),
            "payload": payload,
        }
        for identity in (
            ExecutionIdentity("RUN-RECOVERY", 1),
            ExecutionIdentity("RUN-RECOVERY", 2, "TASK-001"),
        ):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                JournalEvent.create(identity=identity, **common)
        with self.assertRaises(ValueError):
            JournalEvent.create(
                identity=ExecutionIdentity("RUN-RECOVERY", 2),
                **{**common, "previous_event_hash": digest("f")},
            )
        with self.assertRaises(ValueError):
            JournalEvent.create(
                identity=ExecutionIdentity("RUN-OTHER", 2),
                **common,
            )
        with self.assertRaises(ValueError):
            JournalEvent.create(
                identity=ExecutionIdentity("RUN-RECOVERY", 2),
                **{
                    **common,
                    "actor_type": ActorType.COORDINATOR,
                    "actor_id": "coordinator-002",
                },
            )
        with self.assertRaises(ValueError):
            JournalEvent.create(
                identity=ExecutionIdentity("RUN-RECOVERY", 2),
                **{**common, "sequence": 42},
            )

    def test_decoder_rejects_unknown_and_wrong_boolean_fields(self) -> None:
        primitive = recovery_payload().to_primitive()
        primitive["unknown"] = True
        primitive["process_tree_termination_proven"] = 1
        result = decode_dispatch_recovery_payload_bytes(canonical_json_bytes(primitive))
        self.assertFalse(result.ok)
        self.assertIn("/unknown", {issue.path_text for issue in result.issues})
        self.assertIn(
            "/process_tree_termination_proven",
            {issue.path_text for issue in result.issues},
        )

        missing = copy.deepcopy(recovery_payload().to_primitive())
        del missing["receipt"]
        result = decode_dispatch_recovery_payload_bytes(canonical_json_bytes(missing))
        self.assertFalse(result.ok)
        self.assertIn("/receipt", {issue.path_text for issue in result.issues})

        missing_termination = recovery_payload().to_primitive()
        del missing_termination["process_tree_termination_proven"]
        result = decode_dispatch_recovery_payload_bytes(
            canonical_json_bytes(missing_termination)
        )
        self.assertFalse(result.ok)
        self.assertIn(
            "/process_tree_termination_proven",
            {issue.path_text for issue in result.issues},
        )

        semantic = recovery_payload().to_primitive()
        semantic["command"]["kind"] = "retry"  # type: ignore[index]
        result = decode_dispatch_recovery_payload_bytes(canonical_json_bytes(semantic))
        self.assertFalse(result.ok)
        self.assertIn(
            "value.runtime_contract",
            {issue.rule_id for issue in result.issues},
        )


if __name__ == "__main__":
    unittest.main()
