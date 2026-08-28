from __future__ import annotations

import dataclasses
import unittest

from wish_builder.contracts import ActorType, CommandKind, DecisionChoice
from wish_builder.kernel.gates import (
    CommandIdentity,
    DecisionActor,
    DecisionChannel,
    DecisionReason,
    DecisionRequest,
    DecisionSubmission,
    DecisionType,
    GateMaterial,
    evaluate_decision,
    revalidate_gate_material,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
NOW = "2026-08-18T03:00:00Z"


def coordinator_actor() -> DecisionActor:
    return DecisionActor(
        ActorType.COORDINATOR,
        "coordinator-001",
        "host-001",
        4321,
        "process-start-coordinator",
    )


def human_actor(actor_id: str = "local-account-001") -> DecisionActor:
    return DecisionActor(
        ActorType.HUMAN,
        actor_id,
        "host-001",
        1234,
        "process-start-human",
    )


def request() -> DecisionRequest:
    return DecisionRequest(
        command=CommandIdentity(
            schema_version=1,
            command_id="COMMAND-001",
            request_id="REQUEST-001",
            kind=CommandKind.DECIDE,
            expected_sequence=41,
            request_nonce="nonce-001",
            actor=coordinator_actor(),
            source_channel=DecisionChannel.COORDINATOR,
            submitted_at=NOW,
        ),
        decision_type=DecisionType.GATE_B,
        candidate_hash=HASH_A,
        workspace_hash=HASH_B,
        expected_actor_id="local-account-001",
        options=(
            DecisionChoice.APPROVE,
            DecisionChoice.REVISE,
            DecisionChoice.REJECT,
        ),
    )


def submission() -> DecisionSubmission:
    return DecisionSubmission(
        decision_id="DECISION-001",
        request=request(),
        choice=DecisionChoice.APPROVE,
        actor=human_actor(),
        source_channel=DecisionChannel.DIRECT_CLI,
        decided_at=NOW,
    )


class GateDecisionTests(unittest.TestCase):
    def test_exact_direct_cli_decision_is_proposed_then_committed(self) -> None:
        evaluation = evaluate_decision(
            request(),
            submission(),
            current_sequence=41,
            current_workspace_hash=HASH_B,
        )
        self.assertTrue(evaluation.accepted)
        self.assertEqual(DecisionReason.ACCEPTED, evaluation.reason)
        self.assertEqual(42, evaluation.observation.event_sequence)

    def test_full_decision_mismatch_matrix_fails_closed(self) -> None:
        different_request = dataclasses.replace(
            request(), decision_type=DecisionType.GATE_A
        )
        different_candidate = dataclasses.replace(request(), candidate_hash=HASH_C)
        different_nonce = dataclasses.replace(
            request(),
            command=dataclasses.replace(request().command, request_nonce="nonce-999"),
        )
        different_sequence = dataclasses.replace(
            request(),
            command=dataclasses.replace(request().command, expected_sequence=40),
        )
        different_workspace = dataclasses.replace(request(), workspace_hash=HASH_C)
        different_actor = dataclasses.replace(
            request(), expected_actor_id="local-account-999"
        )
        unsupported_choice = dataclasses.replace(
            request(), options=(DecisionChoice.ABORT,)
        )
        cases = (
            (
                "request",
                dataclasses.replace(submission(), request=different_request),
                41,
                HASH_B,
                DecisionReason.REQUEST_MISMATCH,
            ),
            (
                "candidate",
                dataclasses.replace(submission(), request=different_candidate),
                41,
                HASH_B,
                DecisionReason.STALE_CANDIDATE,
            ),
            (
                "nonce",
                dataclasses.replace(submission(), request=different_nonce),
                41,
                HASH_B,
                DecisionReason.STALE_NONCE,
            ),
            (
                "submission sequence",
                dataclasses.replace(submission(), request=different_sequence),
                41,
                HASH_B,
                DecisionReason.STALE_SEQUENCE,
            ),
            (
                "current sequence",
                submission(),
                42,
                HASH_B,
                DecisionReason.STALE_SEQUENCE,
            ),
            (
                "submission workspace",
                dataclasses.replace(submission(), request=different_workspace),
                41,
                HASH_B,
                DecisionReason.WORKSPACE_DRIFT,
            ),
            (
                "current workspace",
                submission(),
                41,
                HASH_C,
                DecisionReason.WORKSPACE_DRIFT,
            ),
            (
                "actor",
                dataclasses.replace(
                    submission(),
                    request=different_actor,
                    actor=human_actor("local-account-999"),
                ),
                41,
                HASH_B,
                DecisionReason.ACTOR_MISMATCH,
            ),
            (
                "choice",
                dataclasses.replace(
                    submission(),
                    request=unsupported_choice,
                    choice=DecisionChoice.ABORT,
                ),
                41,
                HASH_B,
                DecisionReason.INVALID_CHOICE,
            ),
        )
        for name, candidate, sequence, workspace_hash, expected in cases:
            with self.subTest(name=name):
                result = evaluate_decision(
                    request(),
                    candidate,
                    current_sequence=sequence,
                    current_workspace_hash=workspace_hash,
                )
                self.assertFalse(result.accepted)
                self.assertEqual(expected, result.reason)
                self.assertIsNone(result.observation)

    def test_chat_relay_is_denied_in_active_m1(self) -> None:
        candidate = dataclasses.replace(
            submission(), source_channel=DecisionChannel.CODEX_RELAY
        )
        result = evaluate_decision(
            request(),
            candidate,
            current_sequence=41,
            current_workspace_hash=HASH_B,
        )
        self.assertEqual(DecisionReason.CHANNEL_DENIED, result.reason)

    def test_exact_replay_is_idempotent_but_conflict_is_rejected(self) -> None:
        first = evaluate_decision(
            request(),
            submission(),
            current_sequence=41,
            current_workspace_hash=HASH_B,
        )
        replay = evaluate_decision(
            request(),
            submission(),
            current_sequence=42,
            current_workspace_hash=HASH_B,
            observed=first.observation,
        )
        conflict = evaluate_decision(
            request(),
            dataclasses.replace(submission(), choice=DecisionChoice.REJECT),
            current_sequence=42,
            current_workspace_hash=HASH_B,
            observed=first.observation,
        )
        self.assertTrue(replay.accepted)
        self.assertTrue(replay.idempotent)
        self.assertEqual(first.observation, replay.observation)
        self.assertFalse(conflict.accepted)
        self.assertEqual(DecisionReason.DECISION_CONFLICT, conflict.reason)

    def test_exact_replay_is_rejected_after_workspace_drift(self) -> None:
        first = evaluate_decision(
            request(),
            submission(),
            current_sequence=41,
            current_workspace_hash=HASH_B,
        )
        replay = evaluate_decision(
            request(),
            submission(),
            current_sequence=42,
            current_workspace_hash=HASH_C,
            observed=first.observation,
        )
        self.assertFalse(replay.accepted)
        self.assertEqual(DecisionReason.WORKSPACE_DRIFT, replay.reason)

    def test_only_human_actor_type_may_decide(self) -> None:
        for actor_type in ActorType:
            if actor_type is ActorType.HUMAN:
                continue
            with self.subTest(actor_type=actor_type):
                candidate = dataclasses.replace(
                    submission(),
                    actor=dataclasses.replace(
                        submission().actor,
                        actor_type=actor_type,
                    ),
                )
                result = evaluate_decision(
                    request(),
                    candidate,
                    current_sequence=41,
                    current_workspace_hash=HASH_B,
                )
                self.assertFalse(result.accepted)
                self.assertEqual(DecisionReason.ACTOR_MISMATCH, result.reason)

    def test_material_or_workspace_drift_invalidates_gate(self) -> None:
        approved = GateMaterial(HASH_A, HASH_B)
        self.assertEqual(
            DecisionReason.ACCEPTED,
            revalidate_gate_material(approved, GateMaterial(HASH_A, HASH_B)),
        )
        self.assertEqual(
            DecisionReason.MATERIAL_DRIFT,
            revalidate_gate_material(approved, GateMaterial(HASH_C, HASH_B)),
        )
        self.assertEqual(
            DecisionReason.WORKSPACE_DRIFT,
            revalidate_gate_material(approved, GateMaterial(HASH_A, HASH_C)),
        )


if __name__ == "__main__":
    unittest.main()
