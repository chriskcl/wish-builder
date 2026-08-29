"""Immutable execution-manifest v2 contracts.

Schema v2 is the frozen, Trellis-derived execution snapshot.  It deliberately
does not reuse the legacy manifest/task classes: those classes still contain
mutable lifecycle fields needed by the v1 compatibility surface.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from .models import (
    HASH_RE,
    MAX_COLLECTION_ITEMS,
    MAX_ID_LENGTH,
    MAX_PATH_LENGTH,
    MAX_TASKS,
    MAX_TEXT_LENGTH,
    GateApproval,
    RequirementStatus,
    RiskLevel,
    _has_disallowed_contract_control,
    _nonempty,
    _timestamp,
    _tuple_of_strings,
)


MANIFEST_V2_SCHEMA_VERSION = 2
GRAPH_PROJECTION_VERSION = 1
MAX_COMMAND_ARGUMENTS = 4_096
MAX_COMMAND_ARGUMENT_LENGTH = 128 * 1_024
MAX_COMMAND_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_BUDGET_VALUE = 2**63 - 1
MIN_LEASE_TTL_SECONDS = 30
MAX_LEASE_TTL_SECONDS = 3_600

_SNAPSHOT_ID_RE = re.compile(r"^(REQ|TASK)-([0-9]{3,})$")
_PROFILE_RE = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_ENVIRONMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SchedulerMode(StrEnum):
    """The sole v1 sibling scheduler selected by the reviewed design."""

    WISH_BUILDER = "wish_builder"


class WorkerProvider(StrEnum):
    PI = "pi"
    OH_MY_PI = "oh_my_pi"
    CODEX = "codex"


class PathCaseMode(StrEnum):
    SENSITIVE = "sensitive"
    INSENSITIVE = "insensitive"


class NetworkPolicy(StrEnum):
    DENIED = "denied"
    LOOPBACK_ONLY = "loopback_only"
    ALLOWED = "allowed"


class BillingPosture(StrEnum):
    PREAPPROVED = "preapproved"
    UNMETERED = "unmetered"
    OPERATOR_REQUIRED = "operator_required"


def _sha256_reference(value: object, field_name: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return value


def _snapshot_id(value: object, prefix: str, field_name: str) -> str:
    normalized = _nonempty(value, field_name, MAX_ID_LENGTH)
    match = _SNAPSHOT_ID_RE.fullmatch(normalized)
    if match is None or match.group(1) != prefix:
        raise ValueError(f"{field_name} must be a canonical {prefix}-NNN identifier")
    ordinal = int(match.group(2))
    if ordinal <= 0:
        raise ValueError(f"{field_name} ordinal must be positive")
    if match.group(2) != f"{ordinal:03d}":
        raise ValueError(f"{field_name} must use canonical ordinal padding")
    return normalized


def _snapshot_sort_key(value: str) -> int:
    match = _SNAPSHOT_ID_RE.fullmatch(value)
    if match is None:  # pragma: no cover - all callers admit IDs first
        raise ValueError("invalid snapshot identifier")
    return int(match.group(2))


def _optional_string(
    value: object,
    field_name: str,
    limit: int = MAX_TEXT_LENGTH,
) -> str | None:
    return None if value is None else _nonempty(value, field_name, limit)


def _bounded_positive(value: object, field_name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{field_name} must be between 1 and {maximum}")
    return value


def validate_lease_timing(
    lease_ttl_seconds: object,
    lease_clock_skew_seconds: object,
) -> tuple[int, int]:
    if type(lease_ttl_seconds) is not int or not (
        MIN_LEASE_TTL_SECONDS
        <= lease_ttl_seconds
        <= MAX_LEASE_TTL_SECONDS
    ):
        raise ValueError(
            "lease_ttl_seconds must be between "
            f"{MIN_LEASE_TTL_SECONDS} and {MAX_LEASE_TTL_SECONDS}"
        )
    if type(lease_clock_skew_seconds) is not int or lease_clock_skew_seconds < 0:
        raise ValueError("lease_clock_skew_seconds must be a non-negative integer")
    if lease_clock_skew_seconds * 4 >= lease_ttl_seconds:
        raise ValueError("lease_clock_skew_seconds must be less than one-quarter TTL")
    return lease_ttl_seconds, lease_clock_skew_seconds


def _argument(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    if len(normalized) > MAX_COMMAND_ARGUMENT_LENGTH:
        raise ValueError(f"{field_name} exceeds the argument limit")
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain valid Unicode") from exc
    if _has_disallowed_contract_control(normalized):
        raise ValueError(f"{field_name} contains a disallowed contract control")
    return normalized


def _repo_relative_directory(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, MAX_PATH_LENGTH)
    if normalized == ".":
        return normalized
    if (
        normalized.startswith(("/", "\\"))
        or "\\" in normalized
        or normalized.endswith("/")
        or ":" in normalized
    ):
        raise ValueError(f"{field_name} must be a repository-relative POSIX path")
    segments = normalized.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or any(character in segment for character in "*?[]")
        for segment in segments
    ):
        raise ValueError(f"{field_name} must not escape or glob the repository")
    return normalized


def _typed_tuple(
    value: object,
    field_name: str,
    item_type: type,
    *,
    nonempty: bool,
    max_items: int = MAX_COLLECTION_ITEMS,
) -> tuple:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if nonempty and not value:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > max_items:
        raise ValueError(f"{field_name} exceeds the item limit")
    if not all(type(item) is item_type for item in value):
        raise TypeError(f"{field_name} contains an invalid item type")
    return value


@dataclass(frozen=True, slots=True)
class NullGateApproval:
    """The self-referential Gate B approval slot fixed to JSON nulls."""

    approved_by: None = None
    approved_at: None = None
    artifact_hash: None = None

    def __post_init__(self) -> None:
        if any(
            value is not None
            for value in (self.approved_by, self.approved_at, self.artifact_hash)
        ):
            raise ValueError("Gate B fields must remain null in manifest v2")

    def to_primitive(self) -> dict[str, object]:
        return {
            "approved_at": None,
            "approved_by": None,
            "artifact_hash": None,
        }


@dataclass(frozen=True, slots=True)
class ManifestGateEvidence:
    gate_a: GateApproval
    gate_b: NullGateApproval = NullGateApproval()

    def __post_init__(self) -> None:
        if type(self.gate_a) is not GateApproval:
            raise TypeError("gate_a must be a GateApproval")
        if type(self.gate_b) is not NullGateApproval:
            raise TypeError("gate_b must be a NullGateApproval")

    def to_primitive(self) -> dict[str, object]:
        return {
            "gate_a": self.gate_a.to_primitive(),
            "gate_b": self.gate_b.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class TaskIdMapping:
    trellis_task_id: str
    task_id: str

    def __post_init__(self) -> None:
        trellis_task_id = _nonempty(
            self.trellis_task_id,
            "trellis_task_id",
            MAX_TEXT_LENGTH,
        )
        task_id = _snapshot_id(self.task_id, "TASK", "task_id")
        object.__setattr__(self, "trellis_task_id", trellis_task_id)
        object.__setattr__(self, "task_id", task_id)

    def to_primitive(self) -> tuple[str, str]:
        return self.trellis_task_id, self.task_id


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A closed, no-shell local acceptance command."""

    executable_profile: str
    executable_identity_digest: str
    argv: tuple[str, ...]
    working_directory: str
    timeout_seconds: int
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    result_limit_bytes: int
    environment_allowlist: tuple[str, ...]
    network_policy: NetworkPolicy
    display_text: str

    def __post_init__(self) -> None:
        executable_profile = _nonempty(
            self.executable_profile,
            "executable_profile",
            128,
        )
        if not _PROFILE_RE.fullmatch(executable_profile):
            raise ValueError("executable_profile must be a lowercase profile reference")
        executable_identity_digest = _sha256_reference(
            self.executable_identity_digest,
            "executable_identity_digest",
        )
        if type(self.argv) is not tuple or not self.argv:
            raise ValueError("argv must be a non-empty tuple")
        if len(self.argv) > MAX_COMMAND_ARGUMENTS:
            raise ValueError("argv exceeds the argument count limit")
        argv = tuple(
            _argument(item, f"argv[{index}]")
            for index, item in enumerate(self.argv)
        )
        working_directory = _repo_relative_directory(
            self.working_directory,
            "working_directory",
        )
        timeout_seconds = _bounded_positive(
            self.timeout_seconds,
            "timeout_seconds",
            MAX_COMMAND_TIMEOUT_SECONDS,
        )
        stdout_limit_bytes = _bounded_positive(
            self.stdout_limit_bytes,
            "stdout_limit_bytes",
            MAX_BUDGET_VALUE,
        )
        stderr_limit_bytes = _bounded_positive(
            self.stderr_limit_bytes,
            "stderr_limit_bytes",
            MAX_BUDGET_VALUE,
        )
        result_limit_bytes = _bounded_positive(
            self.result_limit_bytes,
            "result_limit_bytes",
            MAX_BUDGET_VALUE,
        )
        environment_allowlist = _tuple_of_strings(
            self.environment_allowlist,
            "environment_allowlist",
            nonempty=False,
            limit=128,
            unique=True,
        )
        for name in environment_allowlist:
            if not _ENVIRONMENT_RE.fullmatch(name):
                raise ValueError("environment_allowlist contains an invalid name")
        if len({name.casefold() for name in environment_allowlist}) != len(
            environment_allowlist
        ):
            raise ValueError(
                "environment_allowlist must be unique across supported platforms"
            )
        if type(self.network_policy) is not NetworkPolicy:
            raise TypeError("network_policy must be a NetworkPolicy")
        display_text = _nonempty(self.display_text, "display_text", MAX_TEXT_LENGTH)

        object.__setattr__(self, "executable_profile", executable_profile)
        object.__setattr__(
            self,
            "executable_identity_digest",
            executable_identity_digest,
        )
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "working_directory", working_directory)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "stdout_limit_bytes", stdout_limit_bytes)
        object.__setattr__(self, "stderr_limit_bytes", stderr_limit_bytes)
        object.__setattr__(self, "result_limit_bytes", result_limit_bytes)
        object.__setattr__(
            self,
            "environment_allowlist",
            tuple(sorted(environment_allowlist)),
        )
        object.__setattr__(self, "display_text", display_text)

    def to_primitive(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "display_text": self.display_text,
            "environment_allowlist": list(self.environment_allowlist),
            "executable_identity_digest": self.executable_identity_digest,
            "executable_profile": self.executable_profile,
            "network_policy": self.network_policy.value,
            "result_limit_bytes": self.result_limit_bytes,
            "stderr_limit_bytes": self.stderr_limit_bytes,
            "stdout_limit_bytes": self.stdout_limit_bytes,
            "timeout_seconds": self.timeout_seconds,
            "working_directory": self.working_directory,
        }


@dataclass(frozen=True, slots=True)
class ExecutionBudgetPolicy:
    max_attempts_per_task: int
    max_attempts_per_run: int
    attempt_deadline_seconds: int
    total_worker_seconds: int
    max_output_bytes: int
    max_retained_evidence_bytes: int
    max_concurrent_workers: int
    billing_posture: BillingPosture

    def __post_init__(self) -> None:
        max_attempts_per_task = _bounded_positive(
            self.max_attempts_per_task,
            "max_attempts_per_task",
            MAX_BUDGET_VALUE,
        )
        max_attempts_per_run = _bounded_positive(
            self.max_attempts_per_run,
            "max_attempts_per_run",
            MAX_BUDGET_VALUE,
        )
        if max_attempts_per_run < max_attempts_per_task:
            raise ValueError(
                "max_attempts_per_run must cover max_attempts_per_task"
            )
        attempt_deadline_seconds = _bounded_positive(
            self.attempt_deadline_seconds,
            "attempt_deadline_seconds",
            MAX_BUDGET_VALUE,
        )
        total_worker_seconds = _bounded_positive(
            self.total_worker_seconds,
            "total_worker_seconds",
            MAX_BUDGET_VALUE,
        )
        max_output_bytes = _bounded_positive(
            self.max_output_bytes,
            "max_output_bytes",
            MAX_BUDGET_VALUE,
        )
        max_retained_evidence_bytes = _bounded_positive(
            self.max_retained_evidence_bytes,
            "max_retained_evidence_bytes",
            MAX_BUDGET_VALUE,
        )
        max_concurrent_workers = _bounded_positive(
            self.max_concurrent_workers,
            "max_concurrent_workers",
            MAX_TASKS,
        )
        if type(self.billing_posture) is not BillingPosture:
            raise TypeError("billing_posture must be a BillingPosture")

        object.__setattr__(self, "max_attempts_per_task", max_attempts_per_task)
        object.__setattr__(self, "max_attempts_per_run", max_attempts_per_run)
        object.__setattr__(
            self,
            "attempt_deadline_seconds",
            attempt_deadline_seconds,
        )
        object.__setattr__(self, "total_worker_seconds", total_worker_seconds)
        object.__setattr__(self, "max_output_bytes", max_output_bytes)
        object.__setattr__(
            self,
            "max_retained_evidence_bytes",
            max_retained_evidence_bytes,
        )
        object.__setattr__(self, "max_concurrent_workers", max_concurrent_workers)

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_deadline_seconds": self.attempt_deadline_seconds,
            "billing_posture": self.billing_posture.value,
            "max_attempts_per_run": self.max_attempts_per_run,
            "max_attempts_per_task": self.max_attempts_per_task,
            "max_concurrent_workers": self.max_concurrent_workers,
            "max_output_bytes": self.max_output_bytes,
            "max_retained_evidence_bytes": self.max_retained_evidence_bytes,
            "total_worker_seconds": self.total_worker_seconds,
        }


@dataclass(frozen=True, slots=True)
class ManifestRequirement:
    id: str
    text: str
    status: RequirementStatus
    decision_ref: str | None = None

    def __post_init__(self) -> None:
        requirement_id = _snapshot_id(self.id, "REQ", "id")
        text = _nonempty(self.text, "text", MAX_TEXT_LENGTH)
        if type(self.status) is not RequirementStatus:
            raise TypeError("status must be a RequirementStatus")
        if self.status is RequirementStatus.IMPLEMENTED:
            raise ValueError("implemented is a runtime status, not a frozen requirement")
        decision_ref = _optional_string(self.decision_ref, "decision_ref")
        if self.status in {
            RequirementStatus.DEFERRED,
            RequirementStatus.OUT_OF_SCOPE,
        } and decision_ref is None:
            raise ValueError("deferred and out-of-scope requirements need decision_ref")
        object.__setattr__(self, "id", requirement_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "decision_ref", decision_ref)

    def to_primitive(self) -> dict[str, object]:
        return {
            "decision_ref": self.decision_ref,
            "id": self.id,
            "status": self.status.value,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class ManifestTask:
    id: str
    title: str
    requirement_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    owned_paths: tuple[str, ...]
    allowed_auxiliary_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    regression_commands: tuple[CommandSpec, ...]
    rollback: str
    documentation: tuple[str, ...]
    wave: int
    risk: RiskLevel
    may_change_contracts: bool
    instruction_context_digest: str | None
    approved_document_digests: tuple[str, ...]
    task_packet_template_digest: str | None

    def __post_init__(self) -> None:
        task_id = _snapshot_id(self.id, "TASK", "id")
        title = _nonempty(self.title, "title", MAX_TEXT_LENGTH)
        requirement_ids = tuple(
            _snapshot_id(item, "REQ", "requirement_id")
            for item in _tuple_of_strings(
                self.requirement_ids,
                "requirement_ids",
                nonempty=True,
                limit=MAX_ID_LENGTH,
                unique=True,
            )
        )
        depends_on = tuple(
            _snapshot_id(item, "TASK", "dependency_id")
            for item in _tuple_of_strings(
                self.depends_on,
                "depends_on",
                nonempty=False,
                limit=MAX_ID_LENGTH,
                unique=True,
            )
        )
        if task_id in depends_on:
            raise ValueError("a task cannot depend on itself")
        owned_paths = _tuple_of_strings(
            self.owned_paths,
            "owned_paths",
            nonempty=True,
            limit=MAX_PATH_LENGTH,
            unique=True,
        )
        allowed_auxiliary_paths = _tuple_of_strings(
            self.allowed_auxiliary_paths,
            "allowed_auxiliary_paths",
            nonempty=False,
            limit=MAX_PATH_LENGTH,
            unique=True,
        )
        acceptance_criteria = _tuple_of_strings(
            self.acceptance_criteria,
            "acceptance_criteria",
            nonempty=True,
            unique=False,
        )
        regression_commands = _typed_tuple(
            self.regression_commands,
            "regression_commands",
            CommandSpec,
            nonempty=True,
        )
        rollback = _nonempty(self.rollback, "rollback", MAX_TEXT_LENGTH)
        documentation = _tuple_of_strings(
            self.documentation,
            "documentation",
            nonempty=False,
            limit=MAX_PATH_LENGTH,
            unique=True,
        )
        if type(self.wave) is not int or self.wave not in (0, 1, 2):
            raise ValueError("wave must be 0, 1, or 2")
        if type(self.risk) is not RiskLevel:
            raise TypeError("risk must be a RiskLevel")
        if type(self.may_change_contracts) is not bool:
            raise TypeError("may_change_contracts must be a bool")

        instruction_context_digest = (
            None
            if self.instruction_context_digest is None
            else _sha256_reference(
                self.instruction_context_digest,
                "instruction_context_digest",
            )
        )
        approved_document_digests = _tuple_of_strings(
            self.approved_document_digests,
            "approved_document_digests",
            nonempty=False,
            limit=71,
            unique=True,
        )
        approved_document_digests = tuple(
            _sha256_reference(item, "approved_document_digest")
            for item in approved_document_digests
        )
        task_packet_template_digest = (
            None
            if self.task_packet_template_digest is None
            else _sha256_reference(
                self.task_packet_template_digest,
                "task_packet_template_digest",
            )
        )
        expanded_inputs = (
            instruction_context_digest is not None
            and bool(approved_document_digests)
            and task_packet_template_digest is None
        )
        template_input = (
            instruction_context_digest is None
            and not approved_document_digests
            and task_packet_template_digest is not None
        )
        if not (expanded_inputs or template_input):
            raise ValueError(
                "frozen worker inputs require context plus documents or one template digest"
            )

        object.__setattr__(self, "id", task_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(
            self,
            "requirement_ids",
            tuple(sorted(requirement_ids, key=_snapshot_sort_key)),
        )
        object.__setattr__(
            self,
            "depends_on",
            tuple(sorted(depends_on, key=_snapshot_sort_key)),
        )
        object.__setattr__(self, "owned_paths", tuple(sorted(owned_paths)))
        object.__setattr__(
            self,
            "allowed_auxiliary_paths",
            tuple(sorted(allowed_auxiliary_paths)),
        )
        object.__setattr__(self, "acceptance_criteria", acceptance_criteria)
        object.__setattr__(self, "regression_commands", regression_commands)
        object.__setattr__(self, "rollback", rollback)
        object.__setattr__(self, "documentation", tuple(sorted(documentation)))
        object.__setattr__(
            self,
            "instruction_context_digest",
            instruction_context_digest,
        )
        object.__setattr__(
            self,
            "approved_document_digests",
            tuple(sorted(approved_document_digests)),
        )
        object.__setattr__(
            self,
            "task_packet_template_digest",
            task_packet_template_digest,
        )

    @property
    def acceptance_commands(self) -> tuple[CommandSpec, ...]:
        return self.regression_commands

    def to_primitive(self) -> dict[str, object]:
        return {
            "acceptance_criteria": list(self.acceptance_criteria),
            "allowed_auxiliary_paths": list(self.allowed_auxiliary_paths),
            "approved_document_digests": list(self.approved_document_digests),
            "depends_on": list(self.depends_on),
            "documentation": list(self.documentation),
            "id": self.id,
            "instruction_context_digest": self.instruction_context_digest,
            "may_change_contracts": self.may_change_contracts,
            "owned_paths": list(self.owned_paths),
            "regression_commands": [
                item.to_primitive() for item in self.regression_commands
            ],
            "requirement_ids": list(self.requirement_ids),
            "risk": self.risk.value,
            "rollback": self.rollback,
            "task_packet_template_digest": self.task_packet_template_digest,
            "title": self.title,
            "wave": self.wave,
        }


@dataclass(frozen=True, slots=True)
class ExecutionManifestV2:
    schema_version: int
    graph_projection_version: int
    run_id: str
    goal: str
    base_branch: str
    trellis_parent_task_id: str
    trellis_revision: str | None
    trellis_graph_digest: str
    task_id_mapping: tuple[TaskIdMapping, ...]
    imported_at: str
    approvals: ManifestGateEvidence
    provider: WorkerProvider
    capability_digest: str
    launch_profile_digest: str
    policy_digest: str
    scheduler_mode: SchedulerMode
    execution_budget: ExecutionBudgetPolicy
    max_concurrency: int
    lease_ttl_seconds: int
    lease_clock_skew_seconds: int
    path_case_mode: PathCaseMode
    protected_paths: tuple[str, ...]
    requirements: tuple[ManifestRequirement, ...]
    tasks: tuple[ManifestTask, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("schema_version must be 2")
        if (
            type(self.graph_projection_version) is not int
            or self.graph_projection_version != GRAPH_PROJECTION_VERSION
        ):
            raise ValueError(
                f"graph_projection_version must be {GRAPH_PROJECTION_VERSION}"
            )
        run_id = _nonempty(self.run_id, "run_id", MAX_ID_LENGTH)
        goal = _nonempty(self.goal, "goal", MAX_TEXT_LENGTH)
        base_branch = _nonempty(self.base_branch, "base_branch", MAX_PATH_LENGTH)
        trellis_parent_task_id = _nonempty(
            self.trellis_parent_task_id,
            "trellis_parent_task_id",
            MAX_TEXT_LENGTH,
        )
        trellis_revision = (
            None
            if self.trellis_revision is None
            else _sha256_reference(self.trellis_revision, "trellis_revision")
        )
        trellis_graph_digest = _sha256_reference(
            self.trellis_graph_digest,
            "trellis_graph_digest",
        )
        imported_at = _timestamp(self.imported_at, "imported_at")
        mappings = _typed_tuple(
            self.task_id_mapping,
            "task_id_mapping",
            TaskIdMapping,
            nonempty=True,
            max_items=MAX_TASKS,
        )
        source_ids = [item.trellis_task_id for item in mappings]
        mapped_ids = [item.task_id for item in mappings]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("task_id_mapping source IDs must be unique")
        if len(set(mapped_ids)) != len(mapped_ids):
            raise ValueError("task_id_mapping target IDs must be unique")
        if type(self.approvals) is not ManifestGateEvidence:
            raise TypeError("approvals must be ManifestGateEvidence")
        if type(self.provider) is not WorkerProvider:
            raise TypeError("provider must be a WorkerProvider")
        capability_digest = _sha256_reference(
            self.capability_digest,
            "capability_digest",
        )
        launch_profile_digest = _sha256_reference(
            self.launch_profile_digest,
            "launch_profile_digest",
        )
        policy_digest = _sha256_reference(self.policy_digest, "policy_digest")
        if type(self.scheduler_mode) is not SchedulerMode:
            raise TypeError("scheduler_mode must be a SchedulerMode")
        if self.scheduler_mode is not SchedulerMode.WISH_BUILDER:
            raise ValueError("scheduler_mode must be wish_builder")
        if type(self.execution_budget) is not ExecutionBudgetPolicy:
            raise TypeError("execution_budget must be an ExecutionBudgetPolicy")
        max_concurrency = _bounded_positive(
            self.max_concurrency,
            "max_concurrency",
            MAX_TASKS,
        )
        if max_concurrency != self.execution_budget.max_concurrent_workers:
            raise ValueError(
                "max_concurrency must equal execution_budget.max_concurrent_workers"
            )
        lease_ttl_seconds, lease_clock_skew_seconds = validate_lease_timing(
            self.lease_ttl_seconds,
            self.lease_clock_skew_seconds,
        )
        if type(self.path_case_mode) is not PathCaseMode:
            raise TypeError("path_case_mode must be a PathCaseMode")
        protected_paths = _tuple_of_strings(
            self.protected_paths,
            "protected_paths",
            nonempty=False,
            limit=MAX_PATH_LENGTH,
            unique=True,
        )
        requirements = _typed_tuple(
            self.requirements,
            "requirements",
            ManifestRequirement,
            nonempty=True,
        )
        tasks = _typed_tuple(
            self.tasks,
            "tasks",
            ManifestTask,
            nonempty=True,
            max_items=MAX_TASKS,
        )
        requirement_ids = [item.id for item in requirements]
        task_ids = [item.id for item in tasks]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("requirement IDs must be unique")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("task IDs must be unique")
        if set(mapped_ids) != set(task_ids):
            raise ValueError("task_id_mapping must map bijectively onto every task")
        requirement_id_set = set(requirement_ids)
        task_id_set = set(task_ids)
        for task in tasks:
            if not set(task.requirement_ids) <= requirement_id_set:
                raise ValueError(f"{task.id} references an unknown requirement")
            if not set(task.depends_on) <= task_id_set:
                raise ValueError(f"{task.id} references an unknown dependency")

        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "base_branch", base_branch)
        object.__setattr__(
            self,
            "trellis_parent_task_id",
            trellis_parent_task_id,
        )
        object.__setattr__(self, "trellis_revision", trellis_revision)
        object.__setattr__(self, "trellis_graph_digest", trellis_graph_digest)
        object.__setattr__(
            self,
            "task_id_mapping",
            tuple(sorted(mappings, key=lambda item: item.trellis_task_id.encode("utf-8"))),
        )
        object.__setattr__(self, "imported_at", imported_at)
        object.__setattr__(
            self,
            "capability_digest",
            capability_digest,
        )
        object.__setattr__(
            self,
            "launch_profile_digest",
            launch_profile_digest,
        )
        object.__setattr__(self, "policy_digest", policy_digest)
        object.__setattr__(self, "max_concurrency", max_concurrency)
        object.__setattr__(self, "lease_ttl_seconds", lease_ttl_seconds)
        object.__setattr__(
            self,
            "lease_clock_skew_seconds",
            lease_clock_skew_seconds,
        )
        object.__setattr__(self, "protected_paths", tuple(sorted(protected_paths)))
        object.__setattr__(
            self,
            "requirements",
            tuple(sorted(requirements, key=lambda item: _snapshot_sort_key(item.id))),
        )
        object.__setattr__(
            self,
            "tasks",
            tuple(sorted(tasks, key=lambda item: _snapshot_sort_key(item.id))),
        )

    @property
    def approved(self) -> ManifestGateEvidence:
        return self.approvals

    @property
    def worker_backend(self) -> WorkerProvider:
        return self.provider

    @property
    def channel_capability_digest(self) -> str:
        """Descriptive alias for the Channel capability digest."""

        return self.capability_digest

    def to_primitive(self) -> dict[str, object]:
        return {
            "approved": self.approvals.to_primitive(),
            "base_branch": self.base_branch,
            "capability_digest": self.capability_digest,
            "execution_budget": self.execution_budget.to_primitive(),
            "goal": self.goal,
            "graph_projection_version": self.graph_projection_version,
            "imported_at": self.imported_at,
            "launch_profile_digest": self.launch_profile_digest,
            "lease_clock_skew_seconds": self.lease_clock_skew_seconds,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "max_concurrency": self.max_concurrency,
            "path_case_mode": self.path_case_mode.value,
            "policy_digest": self.policy_digest,
            "protected_paths": list(self.protected_paths),
            "provider": self.provider.value,
            "requirements": [item.to_primitive() for item in self.requirements],
            "run_id": self.run_id,
            "scheduler_mode": self.scheduler_mode.value,
            "schema_version": self.schema_version,
            "task_id_mapping": {
                item.trellis_task_id: item.task_id for item in self.task_id_mapping
            },
            "tasks": [item.to_primitive() for item in self.tasks],
            "trellis_graph_digest": self.trellis_graph_digest,
            "trellis_parent_task_id": self.trellis_parent_task_id,
            "trellis_revision": self.trellis_revision,
        }

    def canonical_json_bytes(self) -> bytes:
        from .serialization import canonical_json_bytes

        return canonical_json_bytes(self.to_primitive())

    def canonical_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json_bytes()).hexdigest()


ManifestV2 = ExecutionManifestV2
RequirementV2 = ManifestRequirement
TaskDefinition = ManifestTask
TaskV2 = ManifestTask


__all__ = [
    "GRAPH_PROJECTION_VERSION",
    "MANIFEST_V2_SCHEMA_VERSION",
    "MAX_LEASE_TTL_SECONDS",
    "MIN_LEASE_TTL_SECONDS",
    "BillingPosture",
    "CommandSpec",
    "ExecutionBudgetPolicy",
    "ExecutionManifestV2",
    "ManifestGateEvidence",
    "ManifestRequirement",
    "ManifestTask",
    "ManifestV2",
    "NetworkPolicy",
    "NullGateApproval",
    "PathCaseMode",
    "RequirementV2",
    "SchedulerMode",
    "TaskDefinition",
    "TaskIdMapping",
    "TaskV2",
    "WorkerProvider",
    "validate_lease_timing",
]
