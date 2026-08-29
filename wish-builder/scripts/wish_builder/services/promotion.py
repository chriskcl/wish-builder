"""Deterministic, journal-authorized promotion of validated attempt results.

This module deliberately does not schedule work.  It consumes staged results and
the immutable topological positions already compiled by ``GraphIndex``.  The
repository port owns Git I/O and the single cross-process mutation lock.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectReceipt,
    EffectStatus,
    EvidenceRef,
    JournalEventType,
    MAX_EVIDENCE_REFS,
    RuntimeReasonCode,
)
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.services.ports import PreparedEffect

_MAX_ACCEPTANCE_EVIDENCE_REFS = MAX_EVIDENCE_REFS - 1


class PromotionDisposition(StrEnum):
    APPLIED = "applied"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class PromotionBoundaryError(RuntimeError):
    """A stable promotion admission or reconciliation failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _nonempty(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _git_oid(value: object, field_name: str) -> str:
    text = _nonempty(value, field_name)
    if len(text) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{field_name} must be a lowercase Git object ID")
    return text


def _sha256(value: object, field_name: str) -> str:
    text = _nonempty(value, field_name)
    if len(text) != 71 or not text.startswith("sha256:") or any(
        character not in "0123456789abcdef" for character in text[7:]
    ):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return text


def _acceptance_evidence(
    value: object,
    *,
    run_id: str,
    coordinator_epoch: int,
    task_id: str,
    attempt: int,
) -> tuple[EvidenceRef, ...]:
    if type(value) is not tuple or not all(type(item) is EvidenceRef for item in value):
        raise TypeError("acceptance_evidence must be a tuple of EvidenceRef values")
    evidence = tuple(sorted(value, key=lambda item: item.digest))
    if len(evidence) > _MAX_ACCEPTANCE_EVIDENCE_REFS:
        raise ValueError(
            "acceptance_evidence exceeds the promotion-safe maximum of "
            f"{_MAX_ACCEPTANCE_EVIDENCE_REFS}"
        )
    if len({item.digest for item in evidence}) != len(evidence):
        raise ValueError("acceptance_evidence must not repeat a digest")
    for item in evidence:
        identity = item.producer.identity
        if (
            identity.run_id != run_id
            or identity.coordinator_epoch != coordinator_epoch
            or identity.task_id != task_id
            or identity.attempt != attempt
        ):
            raise ValueError("acceptance evidence identity does not match promotion")
    return evidence


def _record_acceptance_evidence(
    value: object,
    *,
    task_id: str,
) -> tuple[EvidenceRef, ...]:
    if type(value) is not tuple or not all(type(item) is EvidenceRef for item in value):
        raise TypeError("acceptance_evidence must be a tuple of EvidenceRef values")
    evidence = tuple(sorted(value, key=lambda item: item.digest))
    if len(evidence) > _MAX_ACCEPTANCE_EVIDENCE_REFS:
        raise ValueError(
            "acceptance_evidence exceeds the promotion-safe maximum of "
            f"{_MAX_ACCEPTANCE_EVIDENCE_REFS}"
        )
    if len({item.digest for item in evidence}) != len(evidence):
        raise ValueError("acceptance_evidence must not repeat a digest")
    identities = {
        (
            item.producer.identity.run_id,
            item.producer.identity.task_id,
            item.producer.identity.attempt,
        )
        for item in evidence
    }
    if evidence and (
        len(identities) != 1 or next(iter(identities))[1] != task_id
    ):
        raise ValueError("acceptance evidence identity does not match promotion")
    return evidence


@runtime_checkable
class StagedResultView(Protocol):
    """Provider-neutral view of a staged, validated Git result."""

    @property
    def run_id(self) -> str: ...

    @property
    def task_id(self) -> str: ...

    @property
    def attempt(self) -> int: ...

    @property
    def staged_ref(self) -> str: ...

    @property
    def result_commit_sha(self) -> str: ...

    @property
    def result_tree_sha(self) -> str: ...

    @property
    def result_manifest_hash(self) -> str: ...

    @property
    def local_repository_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class PromotionCommand:
    """Exact command whose canonical hash is stored in promotion_requested."""

    operation_id: str
    run_id: str
    coordinator_epoch: int
    task_id: str
    attempt: int
    topological_position: int
    local_repository_id: str
    target_workspace_hash: str
    expected_target_sha: str
    staged_ref: str
    result_manifest_hash: str
    source_commit_sha: str
    source_tree_sha: str
    candidate_commit_sha: str
    candidate_tree_sha: str
    acceptance_evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        for name in ("operation_id", "run_id", "task_id", "staged_ref"):
            _nonempty(getattr(self, name), name)
        if type(self.coordinator_epoch) is not int or self.coordinator_epoch <= 0:
            raise ValueError("coordinator_epoch must be positive")
        if type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("attempt must be positive")
        if (
            type(self.topological_position) is not int
            or self.topological_position < 0
        ):
            raise ValueError("topological_position must be non-negative")
        _sha256(self.local_repository_id, "local_repository_id")
        _sha256(self.target_workspace_hash, "target_workspace_hash")
        _sha256(self.result_manifest_hash, "result_manifest_hash")
        for name in (
            "expected_target_sha",
            "source_commit_sha",
            "source_tree_sha",
            "candidate_commit_sha",
            "candidate_tree_sha",
        ):
            _git_oid(getattr(self, name), name)
        object.__setattr__(
            self,
            "acceptance_evidence",
            _acceptance_evidence(
                self.acceptance_evidence,
                run_id=self.run_id,
                coordinator_epoch=self.coordinator_epoch,
                task_id=self.task_id,
                attempt=self.attempt,
            ),
        )

    @property
    def acceptance_bound(self) -> bool:
        return bool(self.acceptance_evidence)

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "acceptance_evidence": [
                item.to_primitive() for item in self.acceptance_evidence
            ],
            "candidate_commit_sha": self.candidate_commit_sha,
            "candidate_tree_sha": self.candidate_tree_sha,
            "coordinator_epoch": self.coordinator_epoch,
            "expected_target_sha": self.expected_target_sha,
            "local_repository_id": self.local_repository_id,
            "operation": EffectOperation.RESULT_PROMOTION.value,
            "operation_id": self.operation_id,
            "result_manifest_hash": self.result_manifest_hash,
            "run_id": self.run_id,
            "source_commit_sha": self.source_commit_sha,
            "source_tree_sha": self.source_tree_sha,
            "staged_ref": self.staged_ref,
            "target_workspace_hash": self.target_workspace_hash,
            "task_id": self.task_id,
            "topological_position": self.topological_position,
        }


@dataclass(frozen=True, slots=True)
class PromotionPlan:
    command: PromotionCommand
    source: StagedResultView

    def __post_init__(self) -> None:
        if type(self.command) is not PromotionCommand:
            raise TypeError("command must be a PromotionCommand")
        if not isinstance(self.source, StagedResultView):
            raise TypeError("source must be a staged result view")
        command = self.command
        source = self.source
        if (
            command.run_id != source.run_id
            or command.task_id != source.task_id
            or command.attempt != source.attempt
            or command.local_repository_id != source.local_repository_id
            or command.staged_ref != source.staged_ref
            or command.result_manifest_hash != source.result_manifest_hash
            or command.source_commit_sha != source.result_commit_sha
            or command.source_tree_sha != source.result_tree_sha
        ):
            raise ValueError("promotion command does not bind the staged result")

    def candidate_record(self) -> PromotionRecord:
        command = self.command
        return PromotionRecord(
            task_id=command.task_id,
            topological_position=command.topological_position,
            previous_target_sha=command.expected_target_sha,
            promoted_commit_sha=command.candidate_commit_sha,
            promoted_tree_sha=command.candidate_tree_sha,
            source_commit_sha=command.source_commit_sha,
            result_manifest_hash=command.result_manifest_hash,
            acceptance_evidence=command.acceptance_evidence,
        )


@dataclass(frozen=True, slots=True)
class PromotionRecord:
    task_id: str
    topological_position: int
    previous_target_sha: str
    promoted_commit_sha: str
    promoted_tree_sha: str
    source_commit_sha: str
    result_manifest_hash: str
    acceptance_evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.task_id, "task_id")
        if (
            type(self.topological_position) is not int
            or self.topological_position < 0
        ):
            raise ValueError("topological_position must be non-negative")
        for name in (
            "previous_target_sha",
            "promoted_commit_sha",
            "promoted_tree_sha",
            "source_commit_sha",
        ):
            _git_oid(getattr(self, name), name)
        _sha256(self.result_manifest_hash, "result_manifest_hash")
        object.__setattr__(
            self,
            "acceptance_evidence",
            _record_acceptance_evidence(
                self.acceptance_evidence,
                task_id=self.task_id,
            ),
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "acceptance_evidence": [
                item.to_primitive() for item in self.acceptance_evidence
            ],
            "previous_target_sha": self.previous_target_sha,
            "promoted_commit_sha": self.promoted_commit_sha,
            "promoted_tree_sha": self.promoted_tree_sha,
            "result_manifest_hash": self.result_manifest_hash,
            "source_commit_sha": self.source_commit_sha,
            "task_id": self.task_id,
            "topological_position": self.topological_position,
        }


@dataclass(frozen=True, slots=True)
class PromotionObservation:
    receipt: EffectReceipt
    disposition: PromotionDisposition
    record: PromotionRecord | None = None
    reason_code: RuntimeReasonCode | None = None
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt")
        if self.receipt.operation is not EffectOperation.RESULT_PROMOTION:
            raise ValueError("receipt must observe result_promotion")
        if type(self.disposition) is not PromotionDisposition:
            raise TypeError("disposition must be a PromotionDisposition")
        expected_status = EffectStatus(self.disposition.value)
        if self.receipt.status is not expected_status:
            raise ValueError("receipt status does not match disposition")
        if self.disposition is PromotionDisposition.APPLIED:
            if type(self.record) is not PromotionRecord or self.reason_code is not None:
                raise ValueError("an applied promotion requires only its record")
            if (
                not self.record.acceptance_evidence
                or self.receipt.evidence != self.record.acceptance_evidence
            ):
                raise ValueError(
                    "an applied promotion must durably bind acceptance evidence"
                )
        elif self.record is not None or self.reason_code is None:
            raise ValueError("a non-applied promotion requires a reason")
        if type(self.details) is not tuple or not all(
            type(item) is str for item in self.details
        ):
            raise TypeError("details must be a tuple of strings")


@runtime_checkable
class PromotionRepositoryPort(Protocol):
    """Narrow Git mutation surface used by the promotion use-case."""

    def prepare_promotion(
        self,
        source: StagedResultView,
        *,
        expected_target_sha: str,
        topological_position: int,
        operation_id: str,
        coordinator_epoch: int,
    ) -> PromotionPlan: ...

    def apply_promotion(
        self,
        effect: PreparedEffect[PromotionCommand],
        plan: PromotionPlan,
    ) -> PromotionObservation: ...

    def inspect_promotion(self, plan: PromotionPlan) -> PromotionObservation: ...

    def materialize_promotion_candidate(
        self,
        plan: PromotionPlan,
    ) -> AbstractContextManager[Path]: ...


class PromotionService:
    """Select and promote one already-eligible result in frozen graph order."""

    def __init__(
        self,
        repository: PromotionRepositoryPort,
        graph_index: GraphIndex,
    ) -> None:
        if not isinstance(repository, PromotionRepositoryPort):
            raise TypeError("repository must implement PromotionRepositoryPort")
        if type(graph_index) is not GraphIndex:
            raise TypeError("graph_index must be a GraphIndex")
        self._repository = repository
        self._graph_index = graph_index
        self._positions = {
            node.task_id: node.topological_position for node in graph_index.nodes
        }
        self._unknown_plan: PromotionPlan | None = None

    @property
    def blocked_on_unknown(self) -> bool:
        return self._unknown_plan is not None

    def canonical_order(
        self, sources: tuple[StagedResultView, ...]
    ) -> tuple[StagedResultView, ...]:
        if type(sources) is not tuple or not all(
            isinstance(source, StagedResultView) for source in sources
        ):
            raise TypeError("sources must be a tuple of staged result views")
        task_ids = [source.task_id for source in sources]
        if len(task_ids) != len(set(task_ids)):
            raise PromotionBoundaryError("duplicate_staged_task")
        unknown = tuple(sorted(set(task_ids) - set(self._positions)))
        if unknown:
            raise PromotionBoundaryError("unknown_graph_task", ",".join(unknown))
        for source in sources:
            if source.run_id != self._graph_index.run_id:
                raise PromotionBoundaryError("run_identity_mismatch", source.run_id)
        return tuple(
            sorted(
                sources,
                key=lambda source: (
                    self._positions[source.task_id],
                    source.task_id,
                ),
            )
        )

    def plan_next(
        self,
        sources: tuple[StagedResultView, ...],
        *,
        expected_target_sha: str,
        operation_id: str,
        coordinator_epoch: int,
    ) -> PromotionPlan:
        if self._unknown_plan is not None:
            raise PromotionBoundaryError(
                "promotion_outcome_unknown",
                self._unknown_plan.command.task_id,
            )
        ordered = self.canonical_order(sources)
        if not ordered:
            raise PromotionBoundaryError("no_staged_result")
        selected = ordered[0]
        return self._repository.prepare_promotion(
            selected,
            expected_target_sha=expected_target_sha,
            topological_position=self._positions[selected.task_id],
            operation_id=operation_id,
            coordinator_epoch=coordinator_epoch,
        )

    def apply(
        self,
        effect: PreparedEffect[PromotionCommand],
        plan: PromotionPlan,
    ) -> PromotionObservation:
        if self._unknown_plan is not None:
            raise PromotionBoundaryError(
                "promotion_outcome_unknown",
                self._unknown_plan.command.task_id,
            )
        self._validate_effect(effect, plan)
        observation = self._repository.apply_promotion(effect, plan)
        if observation.disposition is PromotionDisposition.UNKNOWN:
            self._unknown_plan = plan
        return observation

    def materialize_candidate(
        self,
        plan: PromotionPlan,
    ) -> AbstractContextManager[Path]:
        if type(plan) is not PromotionPlan:
            raise TypeError("plan must be a PromotionPlan")
        if plan.command.acceptance_bound:
            raise PromotionBoundaryError("acceptance_already_bound")
        return self._repository.materialize_promotion_candidate(plan)

    @staticmethod
    def bind_acceptance(
        plan: PromotionPlan,
        evidence: tuple[EvidenceRef, ...],
    ) -> PromotionPlan:
        if type(plan) is not PromotionPlan:
            raise TypeError("plan must be a PromotionPlan")
        if plan.command.acceptance_bound:
            raise PromotionBoundaryError("acceptance_already_bound")
        normalized = _acceptance_evidence(
            evidence,
            run_id=plan.command.run_id,
            coordinator_epoch=plan.command.coordinator_epoch,
            task_id=plan.command.task_id,
            attempt=plan.command.attempt,
        )
        if not normalized:
            raise PromotionBoundaryError("acceptance_evidence_absent")
        return PromotionPlan(
            replace(plan.command, acceptance_evidence=normalized),
            plan.source,
        )

    def reconcile(self, plan: PromotionPlan) -> PromotionObservation:
        if type(plan) is not PromotionPlan:
            raise TypeError("plan must be a PromotionPlan")
        if not plan.command.acceptance_bound:
            raise ValueError("promotion requires bound acceptance evidence")
        if self._unknown_plan is not None and self._unknown_plan.command != plan.command:
            raise PromotionBoundaryError("different_promotion_is_unknown")
        observation = self._repository.inspect_promotion(plan)
        if observation.disposition is not PromotionDisposition.UNKNOWN:
            self._unknown_plan = None
        else:
            self._unknown_plan = plan
        return observation

    @staticmethod
    def _validate_effect(
        effect: PreparedEffect[PromotionCommand],
        plan: PromotionPlan,
    ) -> None:
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        if type(plan) is not PromotionPlan:
            raise TypeError("plan must be a PromotionPlan")
        if type(effect.command) is not PromotionCommand or effect.command != plan.command:
            raise ValueError("durable effect does not bind the promotion plan")
        payload = effect.request.payload
        identity = effect.request.identity
        command = plan.command
        if not command.acceptance_bound:
            raise ValueError("promotion requires bound acceptance evidence")
        if effect.request.event.event_type is not JournalEventType.PROMOTION_REQUESTED:
            raise ValueError("promotion requires promotion_requested")
        if (
            payload.operation is not EffectOperation.RESULT_PROMOTION
            or payload.adapter is not AdapterKind.GIT
            or payload.object_type is not EffectObjectType.GIT_REF
        ):
            raise ValueError("promotion request has the wrong effect boundary")
        if (
            identity.run_id != command.run_id
            or identity.coordinator_epoch != command.coordinator_epoch
            or identity.task_id != command.task_id
            or identity.attempt != command.attempt
            or identity.correlation_id != command.operation_id
        ):
            raise ValueError("promotion request identity does not match its command")


__all__ = [
    "PromotionBoundaryError",
    "PromotionCommand",
    "PromotionDisposition",
    "PromotionObservation",
    "PromotionPlan",
    "PromotionRecord",
    "PromotionRepositoryPort",
    "PromotionService",
    "StagedResultView",
]
