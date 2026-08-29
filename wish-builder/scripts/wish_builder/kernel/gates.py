"""Pure Gate decision admission for the active-M1 control kernel."""

from __future__ import annotations

from dataclasses import dataclass

from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import (
    ActorIdentity,
    ActorType,
    CommandIdentity,
    DecisionAdmissionReason,
    DecisionChoice,
    DecisionCommand,
    DecisionEvaluation,
    DecisionObservation,
    DecisionRequest,
    DecisionType,
    SourceChannel,
)
from wish_builder.contracts.serialization import canonical_sha256


# Compatibility names remain at the kernel boundary while contracts own the
# underlying immutable models and enums.
DecisionActor = ActorIdentity
DecisionChannel = SourceChannel
DecisionReason = DecisionAdmissionReason
DecisionSubmission = DecisionCommand
DecisionObserved = DecisionObservation


def _hash(value: object, field_name: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return value


def _replay_matches(
    observed: DecisionObserved,
    request: DecisionRequest,
    submission: DecisionSubmission,
) -> bool:
    return observed.decision == submission and submission.request == request


def evaluate_decision(
    request: DecisionRequest,
    submission: DecisionSubmission,
    *,
    current_sequence: int,
    current_workspace_hash: str,
    observed: DecisionObserved | None = None,
) -> DecisionEvaluation:
    """Validate one direct-CLI decision without mutating canonical state."""

    if type(request) is not DecisionRequest:
        raise TypeError("request must be a DecisionRequest")
    if type(submission) is not DecisionSubmission:
        raise TypeError("submission must be a DecisionCommand")
    if type(current_sequence) is not int or current_sequence < 0:
        raise ValueError("current_sequence must be a non-negative integer")
    _hash(current_workspace_hash, "current_workspace_hash")
    if current_workspace_hash != request.workspace_hash:
        return DecisionEvaluation(False, DecisionReason.WORKSPACE_DRIFT)
    if observed is not None:
        if type(observed) is not DecisionObserved:
            raise TypeError("observed must be a DecisionObservation or null")
        if _replay_matches(observed, request, submission):
            return DecisionEvaluation(
                True,
                DecisionReason.IDEMPOTENT_REPLAY,
                observed,
                idempotent=True,
            )
        return DecisionEvaluation(False, DecisionReason.DECISION_CONFLICT)

    submitted_request = submission.request
    expected_command = request.command
    submitted_command = submitted_request.command
    if submitted_command.request_id != expected_command.request_id:
        return DecisionEvaluation(False, DecisionReason.REQUEST_MISMATCH)
    if submission.source_channel is not DecisionChannel.DIRECT_CLI:
        return DecisionEvaluation(False, DecisionReason.CHANNEL_DENIED)
    if submitted_request.candidate_hash != request.candidate_hash:
        return DecisionEvaluation(False, DecisionReason.STALE_CANDIDATE)
    if submitted_command.request_nonce != expected_command.request_nonce:
        return DecisionEvaluation(False, DecisionReason.STALE_NONCE)
    if submitted_command.expected_sequence != expected_command.expected_sequence:
        return DecisionEvaluation(False, DecisionReason.STALE_SEQUENCE)
    if current_sequence != expected_command.expected_sequence:
        return DecisionEvaluation(False, DecisionReason.STALE_SEQUENCE)
    if submitted_request.workspace_hash != request.workspace_hash:
        return DecisionEvaluation(False, DecisionReason.WORKSPACE_DRIFT)
    if submission.actor.actor_type is not ActorType.HUMAN:
        return DecisionEvaluation(False, DecisionReason.ACTOR_MISMATCH)
    if submission.actor.actor_id != request.expected_actor_id:
        return DecisionEvaluation(False, DecisionReason.ACTOR_MISMATCH)
    if submission.choice not in request.options:
        return DecisionEvaluation(False, DecisionReason.INVALID_CHOICE)
    if submitted_request != request:
        return DecisionEvaluation(False, DecisionReason.REQUEST_MISMATCH)

    observation = DecisionObserved(
        submission,
        event_sequence=expected_command.expected_sequence + 1,
        submission_hash="sha256:" + canonical_sha256(submission.to_primitive()),
    )
    return DecisionEvaluation(True, DecisionReason.ACCEPTED, observation)


@dataclass(frozen=True, slots=True)
class GateMaterial:
    candidate_hash: str
    workspace_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_hash",
            _hash(self.candidate_hash, "candidate_hash"),
        )
        object.__setattr__(
            self,
            "workspace_hash",
            _hash(self.workspace_hash, "workspace_hash"),
        )


def revalidate_gate_material(
    approved: GateMaterial,
    current: GateMaterial,
) -> DecisionReason:
    if type(approved) is not GateMaterial or type(current) is not GateMaterial:
        raise TypeError("gate material values must be GateMaterial")
    if approved.workspace_hash != current.workspace_hash:
        return DecisionReason.WORKSPACE_DRIFT
    if approved.candidate_hash != current.candidate_hash:
        return DecisionReason.MATERIAL_DRIFT
    return DecisionReason.ACCEPTED


__all__ = [
    "CommandIdentity",
    "DecisionActor",
    "DecisionChannel",
    "DecisionChoice",
    "DecisionEvaluation",
    "DecisionObserved",
    "DecisionReason",
    "DecisionRequest",
    "DecisionSubmission",
    "DecisionType",
    "GateMaterial",
    "evaluate_decision",
    "revalidate_gate_material",
]
