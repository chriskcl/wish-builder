"""Evidence-preserving, fail-closed cleanup for attempt worktrees."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol, runtime_checkable

from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectReceipt,
    EffectStatus,
    EvidenceRef,
    JournalEventType,
    RuntimeReasonCode,
)
from wish_builder.services.ports import PreparedEffect


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


class CleanupDisposition(StrEnum):
    REMOVED = "removed"
    ALREADY_ABSENT = "already_absent"
    QUARANTINED = "quarantined"
    UNKNOWN = "unknown"


class CleanupBoundaryError(RuntimeError):
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


@runtime_checkable
class CleanupAttemptView(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def task_id(self) -> str: ...

    @property
    def attempt_number(self) -> int: ...

    @property
    def local_repository_id(self) -> str: ...

    @property
    def external_object_id(self) -> str: ...

    @property
    def path(self) -> str: ...


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    attempt: CleanupAttemptView
    expected_head_sha: str
    evidence: tuple[EvidenceRef, ...]
    reconciliation_complete: bool
    process_tree_terminated: bool
    outcome_known: bool

    def __post_init__(self) -> None:
        if not isinstance(self.attempt, CleanupAttemptView):
            raise TypeError("attempt must implement CleanupAttemptView")
        _git_oid(self.expected_head_sha, "expected_head_sha")
        if type(self.evidence) is not tuple or not all(
            type(item) is EvidenceRef for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of EvidenceRef values")
        if len({item.digest for item in self.evidence}) != len(self.evidence):
            raise ValueError("evidence must not repeat a digest")
        for name in (
            "reconciliation_complete",
            "process_tree_terminated",
            "outcome_known",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")


@dataclass(frozen=True, slots=True)
class CleanupInspection:
    exists: bool
    identity_ok: bool
    clean: bool
    observed_head_sha: str | None
    target_workspace_hash: str
    state_hash: str
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("exists", "identity_ok", "clean"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        if self.observed_head_sha is not None:
            _git_oid(self.observed_head_sha, "observed_head_sha")
        _sha256(self.target_workspace_hash, "target_workspace_hash")
        _sha256(self.state_hash, "state_hash")
        if type(self.details) is not tuple or not all(
            type(item) is str for item in self.details
        ):
            raise TypeError("details must be a tuple of strings")
        if not self.exists and self.observed_head_sha is not None:
            raise ValueError("an absent attempt cannot expose a head")


def _cleanup_quarantine_reason(
    candidate: CleanupCandidate,
    inspection: CleanupInspection,
) -> RuntimeReasonCode | None:
    if not candidate.evidence:
        return RuntimeReasonCode.EVIDENCE_MISSING
    if not candidate.reconciliation_complete or not candidate.outcome_known:
        return RuntimeReasonCode.WORKER_OUTCOME_UNKNOWN
    if not candidate.process_tree_terminated:
        return RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN
    if not inspection.identity_ok:
        return RuntimeReasonCode.CLEANUP_INCOMPLETE
    if inspection.exists and (
        not inspection.clean
        or inspection.observed_head_sha != candidate.expected_head_sha
    ):
        return RuntimeReasonCode.GIT_STATE_CONFLICT
    return None


@dataclass(frozen=True, slots=True)
class CleanupCommand:
    operation_id: str
    run_id: str
    coordinator_epoch: int
    task_id: str
    attempt: int
    local_repository_id: str
    target_workspace_hash: str
    external_object_id: str
    expected_head_sha: str
    observed_state_hash: str
    evidence_digests: tuple[str, ...]
    remove_allowed: bool

    def __post_init__(self) -> None:
        for name in (
            "operation_id",
            "run_id",
            "task_id",
            "external_object_id",
        ):
            _nonempty(getattr(self, name), name)
        if type(self.coordinator_epoch) is not int or self.coordinator_epoch <= 0:
            raise ValueError("coordinator_epoch must be positive")
        if type(self.attempt) is not int or self.attempt <= 0:
            raise ValueError("attempt must be positive")
        _sha256(self.local_repository_id, "local_repository_id")
        _sha256(self.target_workspace_hash, "target_workspace_hash")
        _git_oid(self.expected_head_sha, "expected_head_sha")
        _sha256(self.observed_state_hash, "observed_state_hash")
        if type(self.evidence_digests) is not tuple:
            raise TypeError("evidence_digests must be a tuple")
        for digest in self.evidence_digests:
            _sha256(digest, "evidence_digest")
        if len(set(self.evidence_digests)) != len(self.evidence_digests):
            raise ValueError("evidence_digests must not contain duplicates")
        if type(self.remove_allowed) is not bool:
            raise TypeError("remove_allowed must be a bool")

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "coordinator_epoch": self.coordinator_epoch,
            "evidence_digests": list(self.evidence_digests),
            "expected_head_sha": self.expected_head_sha,
            "external_object_id": self.external_object_id,
            "local_repository_id": self.local_repository_id,
            "observed_state_hash": self.observed_state_hash,
            "operation": EffectOperation.CLEANUP.value,
            "operation_id": self.operation_id,
            "remove_allowed": self.remove_allowed,
            "run_id": self.run_id,
            "target_workspace_hash": self.target_workspace_hash,
            "task_id": self.task_id,
        }


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    command: CleanupCommand
    candidate: CleanupCandidate
    inspection: CleanupInspection
    quarantine_reason: RuntimeReasonCode | None = None

    def __post_init__(self) -> None:
        if type(self.command) is not CleanupCommand:
            raise TypeError("command must be a CleanupCommand")
        if type(self.candidate) is not CleanupCandidate:
            raise TypeError("candidate must be a CleanupCandidate")
        if type(self.inspection) is not CleanupInspection:
            raise TypeError("inspection must be a CleanupInspection")
        attempt = self.candidate.attempt
        command = self.command
        if (
            command.run_id != attempt.run_id
            or command.task_id != attempt.task_id
            or command.attempt != attempt.attempt_number
            or command.local_repository_id != attempt.local_repository_id
            or command.target_workspace_hash
            != self.inspection.target_workspace_hash
            or command.external_object_id != attempt.external_object_id
            or command.expected_head_sha != self.candidate.expected_head_sha
            or command.observed_state_hash != self.inspection.state_hash
            or command.evidence_digests
            != tuple(sorted(item.digest for item in self.candidate.evidence))
        ):
            raise ValueError("cleanup command does not bind its candidate")
        if command.remove_allowed != (self.quarantine_reason is None):
            raise ValueError("cleanup admission and quarantine reason disagree")
        if self.quarantine_reason is not None and type(
            self.quarantine_reason
        ) is not RuntimeReasonCode:
            raise TypeError("quarantine_reason must be a RuntimeReasonCode or null")
        expected_reason = _cleanup_quarantine_reason(self.candidate, self.inspection)
        if self.quarantine_reason is not expected_reason:
            raise ValueError("cleanup plan does not match its safety evidence")


@dataclass(frozen=True, slots=True)
class CleanupObservation:
    receipt: EffectReceipt
    disposition: CleanupDisposition
    external_object_id: str
    evidence: tuple[EvidenceRef, ...]
    reason_code: RuntimeReasonCode | None = None
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt")
        if self.receipt.operation is not EffectOperation.CLEANUP:
            raise ValueError("receipt must observe cleanup")
        if type(self.disposition) is not CleanupDisposition:
            raise TypeError("disposition must be a CleanupDisposition")
        expected_status = {
            CleanupDisposition.REMOVED: EffectStatus.APPLIED,
            CleanupDisposition.ALREADY_ABSENT: EffectStatus.APPLIED,
            CleanupDisposition.QUARANTINED: EffectStatus.ABSENT,
            CleanupDisposition.UNKNOWN: EffectStatus.UNKNOWN,
        }[self.disposition]
        if self.receipt.status is not expected_status:
            raise ValueError("receipt status does not match cleanup disposition")
        _nonempty(self.external_object_id, "external_object_id")
        if type(self.evidence) is not tuple or not all(
            type(item) is EvidenceRef for item in self.evidence
        ):
            raise TypeError("evidence must be a tuple of EvidenceRef values")
        if self.disposition in {
            CleanupDisposition.REMOVED,
            CleanupDisposition.ALREADY_ABSENT,
        }:
            if self.reason_code is not None:
                raise ValueError("successful cleanup cannot carry a reason")
        elif self.reason_code is None:
            raise ValueError("retained cleanup requires a reason")
        if type(self.details) is not tuple or not all(
            type(item) is str for item in self.details
        ):
            raise TypeError("details must be a tuple of strings")


@dataclass(frozen=True, slots=True)
class CleanupReport:
    observations: tuple[CleanupObservation, ...]
    retained_object_ids: tuple[str, ...]
    dispatch_blocked: bool
    available_bytes: int

    def __post_init__(self) -> None:
        if type(self.observations) is not tuple or not all(
            type(item) is CleanupObservation for item in self.observations
        ):
            raise TypeError("observations must be CleanupObservation values")
        if type(self.retained_object_ids) is not tuple:
            raise TypeError("retained_object_ids must be a tuple")
        if type(self.dispatch_blocked) is not bool:
            raise TypeError("dispatch_blocked must be a bool")
        if type(self.available_bytes) is not int or self.available_bytes < 0:
            raise ValueError("available_bytes must be non-negative")


@runtime_checkable
class CleanupRepositoryPort(Protocol):
    def inspect_cleanup(self, candidate: CleanupCandidate) -> CleanupInspection: ...

    def apply_cleanup(
        self,
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
    ) -> CleanupObservation: ...


class CleanupService:
    """Plan and apply one journaled cleanup decision at a time."""

    def __init__(
        self,
        repository: CleanupRepositoryPort,
        *,
        available_bytes: Callable[[], int],
        minimum_free_bytes: int,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        if not isinstance(repository, CleanupRepositoryPort):
            raise TypeError("repository must implement CleanupRepositoryPort")
        if not callable(available_bytes):
            raise TypeError("available_bytes must be callable")
        if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes must be non-negative")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._repository = repository
        self._available_bytes = available_bytes
        self._minimum_free_bytes = minimum_free_bytes
        self._clock = clock
        self._unknown_plan: CleanupPlan | None = None

    @property
    def blocked_on_unknown(self) -> bool:
        return self._unknown_plan is not None

    @property
    def dispatch_blocked(self) -> bool:
        """Fail closed when cleanup is unknown or storage pressure is unsafe."""

        if self._unknown_plan is not None:
            return True
        try:
            available = self._available_bytes()
        except Exception:
            return True
        return (
            type(available) is not int
            or available < 0
            or available < self._minimum_free_bytes
        )

    def plan(
        self,
        candidate: CleanupCandidate,
        *,
        operation_id: str,
        coordinator_epoch: int,
    ) -> CleanupPlan:
        if type(candidate) is not CleanupCandidate:
            raise TypeError("candidate must be a CleanupCandidate")
        inspection = self._repository.inspect_cleanup(candidate)
        reason = _cleanup_quarantine_reason(candidate, inspection)
        attempt = candidate.attempt
        command = CleanupCommand(
            operation_id=operation_id,
            run_id=attempt.run_id,
            coordinator_epoch=coordinator_epoch,
            task_id=attempt.task_id,
            attempt=attempt.attempt_number,
            local_repository_id=attempt.local_repository_id,
            target_workspace_hash=inspection.target_workspace_hash,
            external_object_id=attempt.external_object_id,
            expected_head_sha=candidate.expected_head_sha,
            observed_state_hash=inspection.state_hash,
            evidence_digests=tuple(
                sorted(item.digest for item in candidate.evidence)
            ),
            remove_allowed=reason is None,
        )
        return CleanupPlan(command, candidate, inspection, reason)

    def apply(
        self,
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
    ) -> CleanupObservation:
        self._validate_effect(effect, plan)
        if (
            self._unknown_plan is not None
            and self._unknown_plan.command != plan.command
        ):
            raise CleanupBoundaryError(
                "cleanup_outcome_unknown",
                self._unknown_plan.command.external_object_id,
            )
        if plan.quarantine_reason is not None:
            receipt = EffectReceipt(
                1,
                effect.request.identity,
                EffectOperation.CLEANUP,
                EffectStatus.ABSENT,
                self._clock(),
                evidence=plan.candidate.evidence,
            )
            observation = CleanupObservation(
                receipt=receipt,
                disposition=CleanupDisposition.QUARANTINED,
                external_object_id=plan.command.external_object_id,
                evidence=plan.candidate.evidence,
                reason_code=plan.quarantine_reason,
                details=plan.inspection.details,
            )
        else:
            observation = self._repository.apply_cleanup(effect, plan)
        if observation.disposition is CleanupDisposition.UNKNOWN:
            self._unknown_plan = plan
        elif self._unknown_plan is not None:
            self._unknown_plan = None
        return observation

    def apply_many(
        self,
        operations: tuple[
            tuple[PreparedEffect[CleanupCommand], CleanupPlan], ...
        ],
    ) -> CleanupReport:
        if type(operations) is not tuple:
            raise TypeError("operations must be a tuple")
        if not all(
            type(item) is tuple
            and len(item) == 2
            and type(item[0]) is PreparedEffect
            and type(item[1]) is CleanupPlan
            for item in operations
        ):
            raise TypeError("operations must contain prepared cleanup plan pairs")
        ordered = tuple(
            sorted(
                operations,
                key=lambda item: (
                    item[1].command.run_id,
                    item[1].command.task_id,
                    item[1].command.attempt,
                    item[1].command.external_object_id,
                ),
            )
        )
        observations_list: list[CleanupObservation] = []
        unattempted_object_ids: list[str] = []
        for position, (effect, plan) in enumerate(ordered):
            observation = self.apply(effect, plan)
            observations_list.append(observation)
            if observation.disposition is CleanupDisposition.UNKNOWN:
                unattempted_object_ids.extend(
                    pending_plan.command.external_object_id
                    for _, pending_plan in ordered[position + 1 :]
                )
                break
        observations = tuple(observations_list)
        retained = tuple(
            sorted(
                {
                    observation.external_object_id
                    for observation in observations
                    if observation.disposition
                    in {CleanupDisposition.QUARANTINED, CleanupDisposition.UNKNOWN}
                }
                | set(unattempted_object_ids)
            )
        )
        available = self._available_bytes()
        if type(available) is not int or available < 0:
            raise CleanupBoundaryError("invalid_storage_observation")
        unknown = any(
            observation.disposition is CleanupDisposition.UNKNOWN
            for observation in observations
        )
        return CleanupReport(
            observations=observations,
            retained_object_ids=retained,
            dispatch_blocked=unknown or available < self._minimum_free_bytes,
            available_bytes=available,
        )

    @staticmethod
    def _validate_effect(
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
    ) -> None:
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        if type(plan) is not CleanupPlan:
            raise TypeError("plan must be a CleanupPlan")
        if type(effect.command) is not CleanupCommand or effect.command != plan.command:
            raise ValueError("durable effect does not bind the cleanup plan")
        payload = effect.request.payload
        identity = effect.request.identity
        command = plan.command
        if effect.request.event.event_type is not JournalEventType.CLEANUP_REQUESTED:
            raise ValueError("cleanup requires cleanup_requested")
        if (
            payload.operation is not EffectOperation.CLEANUP
            or payload.adapter is not AdapterKind.GIT
            or payload.object_type is not EffectObjectType.CLEANUP_ITEM
        ):
            raise ValueError("cleanup request has the wrong effect boundary")
        if payload.normalized_target_hash != command.target_workspace_hash:
            raise ValueError("cleanup request target hash does not match")
        if (
            identity.run_id != command.run_id
            or identity.coordinator_epoch != command.coordinator_epoch
            or identity.task_id != command.task_id
            or identity.attempt != command.attempt
            or identity.correlation_id != command.operation_id
        ):
            raise ValueError("cleanup request identity does not match its command")


__all__ = [
    "CleanupAttemptView",
    "CleanupBoundaryError",
    "CleanupCandidate",
    "CleanupCommand",
    "CleanupDisposition",
    "CleanupInspection",
    "CleanupObservation",
    "CleanupPlan",
    "CleanupReport",
    "CleanupRepositoryPort",
    "CleanupService",
]
