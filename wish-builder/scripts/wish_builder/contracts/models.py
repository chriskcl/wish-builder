"""Frozen, closed-domain models used at the M1 trust boundary."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias


ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9][A-Z0-9-]*$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)

MAX_ID_LENGTH = 64
MAX_TEXT_LENGTH = 4096
MAX_PATH_LENGTH = 1024
MAX_COLLECTION_ITEMS = 256
MAX_TASKS = 64

_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x206A),
    }
)

Identifier: TypeAlias = int | str


def _has_disallowed_contract_control(value: str) -> bool:
    """Reject control syntax without excluding ordinary international text."""

    for character in value:
        codepoint = ord(character)
        if codepoint < 0x20 and character not in {"\t", "\n"}:
            return True
        if 0x7F <= codepoint <= 0x9F or codepoint in _BIDI_CONTROL_CODEPOINTS:
            return True
    return False


class RequirementStatus(StrEnum):
    APPROVED = "approved"
    IMPLEMENTED = "implemented"
    DEFERRED = "deferred"
    OUT_OF_SCOPE = "out_of_scope"


class TaskStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    READY = "ready"
    LEASED = "leased"
    DISPATCHED = "dispatched"
    PR_OPEN = "pr_open"
    MERGED = "merged"
    VERIFIED = "verified"
    ARCHIVED = "archived"
    BLOCKED = "blocked"
    REVERTED = "reverted"
    INVALIDATED = "invalidated"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ValidationPhase(StrEnum):
    PLANNING = "planning"
    EXECUTION = "execution"
    FINISH = "finish"


def _nonempty(
    value: object,
    field_name: str,
    limit: int = MAX_TEXT_LENGTH,
) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    if not normalized.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(normalized) > limit:
        raise ValueError(f"{field_name} exceeds the string limit")
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain valid Unicode") from exc
    if _has_disallowed_contract_control(normalized):
        raise ValueError(f"{field_name} contains a disallowed contract control")
    return normalized


def _stable_id(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, MAX_ID_LENGTH)
    if not ID_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} is not a stable uppercase ID")
    return normalized


def _tuple_of_strings(
    value: object,
    field_name: str,
    *,
    nonempty: bool,
    limit: int = MAX_TEXT_LENGTH,
    max_items: int = MAX_COLLECTION_ITEMS,
    unique: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if len(value) > max_items:
        raise ValueError(f"{field_name} exceeds the item limit")
    if nonempty and not value:
        raise ValueError(f"{field_name} must not be empty")
    normalized = tuple(
        _nonempty(item, f"{field_name}[{index}]", limit)
        for index, item in enumerate(value)
    )
    if unique and len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _optional_identifier(value: object, field_name: str) -> Identifier | None:
    if value is None:
        return None
    if type(value) is int:
        if value <= 0 or value > 2**63 - 1:
            raise ValueError(f"{field_name} must be a positive integer")
        return value
    if type(value) is str:
        return _nonempty(value, field_name, MAX_ID_LENGTH)
    raise TypeError(f"{field_name} must be an integer, string, or null")


def _timestamp(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, 32)
    if not TIMESTAMP_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a UTC timestamp")
    try:
        datetime.fromisoformat(normalized[:-1])
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid timestamp") from exc
    return normalized


@dataclass(frozen=True, slots=True)
class GateApproval:
    approved_by: str
    approved_at: str
    artifact_hash: str

    def __post_init__(self) -> None:
        approved_by = _nonempty(self.approved_by, "approved_by", MAX_ID_LENGTH)
        approved_at = _timestamp(self.approved_at, "approved_at")
        if type(self.artifact_hash) is not str or not HASH_RE.fullmatch(
            self.artifact_hash
        ):
            raise ValueError("artifact_hash must be a full sha256 reference")
        object.__setattr__(self, "approved_by", approved_by)
        object.__setattr__(self, "approved_at", approved_at)

    def to_primitive(self) -> dict[str, object]:
        return {
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "artifact_hash": self.artifact_hash,
        }


@dataclass(frozen=True, slots=True)
class ApprovalSet:
    gate_a: GateApproval | None = None
    gate_b: GateApproval | None = None

    def __post_init__(self) -> None:
        if self.gate_a is not None and type(self.gate_a) is not GateApproval:
            raise TypeError("gate_a must be a GateApproval or null")
        if self.gate_b is not None and type(self.gate_b) is not GateApproval:
            raise TypeError("gate_b must be a GateApproval or null")

    def to_primitive(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.gate_a is not None:
            result["gate_a"] = self.gate_a.to_primitive()
        if self.gate_b is not None:
            result["gate_b"] = self.gate_b.to_primitive()
        return result


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    text: str
    status: RequirementStatus

    def __post_init__(self) -> None:
        requirement_id = _stable_id(self.id, "id")
        text = _nonempty(self.text, "text")
        if type(self.status) is not RequirementStatus:
            raise TypeError("status must be a RequirementStatus")
        object.__setattr__(self, "id", requirement_id)
        object.__setattr__(self, "text", text)

    def to_primitive(self) -> dict[str, object]:
        return {"id": self.id, "status": self.status.value, "text": self.text}


@dataclass(frozen=True, slots=True)
class Task:
    """Legacy schema-v1 task retained for strict compatibility.

    New execution snapshots use ``ManifestTask`` / ``TaskDefinition``. Runtime
    lifecycle facts belong to kernel projections, the Journal, and Trellis.
    """

    id: str
    title: str
    requirement_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    owned_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    regression_commands: tuple[str, ...]
    rollback: str
    wave: int
    risk: RiskLevel
    status: TaskStatus
    allowed_auxiliary_paths: tuple[str, ...] = ()
    documentation: tuple[str, ...] = ()
    may_change_contracts: bool = False
    issue_id: Identifier | None = None
    branch: str | None = None
    pr_id: Identifier | None = None
    squash_commit: str | None = None
    agent_owner: str | None = None

    def __post_init__(self) -> None:
        task_id = _stable_id(self.id, "id")
        title = _nonempty(self.title, "title")
        requirement_ids = _tuple_of_strings(
            self.requirement_ids,
            "requirement_ids",
            nonempty=True,
            limit=MAX_ID_LENGTH,
            unique=True,
        )
        requirement_ids = tuple(
            _stable_id(value, "requirement_id") for value in requirement_ids
        )
        depends_on = _tuple_of_strings(
            self.depends_on,
            "depends_on",
            nonempty=False,
            limit=MAX_ID_LENGTH,
            unique=True,
        )
        depends_on = tuple(
            _stable_id(value, "dependency_id") for value in depends_on
        )
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
        )
        regression_commands = _tuple_of_strings(
            self.regression_commands,
            "regression_commands",
            nonempty=True,
        )
        rollback = _nonempty(self.rollback, "rollback")
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
        if type(self.status) is not TaskStatus:
            raise TypeError("status must be a TaskStatus")
        if type(self.may_change_contracts) is not bool:
            raise TypeError("may_change_contracts must be a bool")
        issue_id = _optional_identifier(self.issue_id, "issue_id")
        pr_id = _optional_identifier(self.pr_id, "pr_id")
        optional_strings: dict[str, str | None] = {}
        for value, field_name in (
            (self.branch, "branch"),
            (self.squash_commit, "squash_commit"),
            (self.agent_owner, "agent_owner"),
        ):
            optional_strings[field_name] = (
                None
                if value is None
                else _nonempty(value, field_name, MAX_TEXT_LENGTH)
            )

        # These fields are sets in the manifest contract. Canonicalize them once
        # so shuffled equivalent inputs produce the same immutable value.
        object.__setattr__(self, "id", task_id)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "requirement_ids", tuple(sorted(requirement_ids)))
        object.__setattr__(self, "depends_on", tuple(sorted(depends_on)))
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
        object.__setattr__(self, "issue_id", issue_id)
        object.__setattr__(self, "pr_id", pr_id)
        object.__setattr__(self, "branch", optional_strings["branch"])
        object.__setattr__(self, "squash_commit", optional_strings["squash_commit"])
        object.__setattr__(self, "agent_owner", optional_strings["agent_owner"])

    def to_primitive(self) -> dict[str, object]:
        return {
            "acceptance_criteria": list(self.acceptance_criteria),
            "agent_owner": self.agent_owner,
            "allowed_auxiliary_paths": list(self.allowed_auxiliary_paths),
            "branch": self.branch,
            "depends_on": list(self.depends_on),
            "documentation": list(self.documentation),
            "id": self.id,
            "issue_id": self.issue_id,
            "may_change_contracts": self.may_change_contracts,
            "pr_id": self.pr_id,
            "regression_commands": list(self.regression_commands),
            "requirement_ids": list(self.requirement_ids),
            "risk": self.risk.value,
            "rollback": self.rollback,
            "squash_commit": self.squash_commit,
            "status": self.status.value,
            "title": self.title,
            "owned_paths": list(self.owned_paths),
            "wave": self.wave,
        }


@dataclass(frozen=True, slots=True)
class ExecutionManifest:
    """Legacy schema-v1 manifest retained for strict compatibility."""

    schema_version: int
    run_id: str
    goal: str
    base_branch: str
    requirements: tuple[Requirement, ...]
    tasks: tuple[Task, ...]
    max_concurrency: int = 3
    protected_paths: tuple[str, ...] = ()
    approvals: ApprovalSet = ApprovalSet()

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        run_id = _stable_id(self.run_id, "run_id")
        goal = _nonempty(self.goal, "goal")
        base_branch = _nonempty(self.base_branch, "base_branch", MAX_PATH_LENGTH)
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 64:
            raise ValueError("max_concurrency must be between 1 and 64")
        protected_paths = _tuple_of_strings(
            self.protected_paths,
            "protected_paths",
            nonempty=False,
            limit=MAX_PATH_LENGTH,
            unique=True,
        )
        if type(self.approvals) is not ApprovalSet:
            raise TypeError("approvals must be an ApprovalSet")
        if type(self.requirements) is not tuple or not self.requirements:
            raise ValueError("requirements must be a non-empty tuple")
        if type(self.tasks) is not tuple or not self.tasks:
            raise ValueError("tasks must be a non-empty tuple")
        if len(self.requirements) > MAX_COLLECTION_ITEMS:
            raise ValueError("requirements exceeds the item limit")
        if len(self.tasks) > MAX_TASKS:
            raise ValueError("tasks exceeds the task limit")
        if not all(type(item) is Requirement for item in self.requirements):
            raise TypeError("requirements must contain only Requirement values")
        if not all(type(item) is Task for item in self.tasks):
            raise TypeError("tasks must contain only Task values")
        if len({item.id for item in self.requirements}) != len(self.requirements):
            raise ValueError("requirement IDs must be unique")
        if len({item.id for item in self.tasks}) != len(self.tasks):
            raise ValueError("task IDs must be unique")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "base_branch", base_branch)
        object.__setattr__(
            self,
            "requirements",
            tuple(sorted(self.requirements, key=lambda item: item.id)),
        )
        object.__setattr__(self, "tasks", tuple(sorted(self.tasks, key=lambda item: item.id)))
        object.__setattr__(self, "protected_paths", tuple(sorted(protected_paths)))

    @property
    def approved(self) -> ApprovalSet:
        """Legacy field spelling retained for callers of the manifest schema."""

        return self.approvals

    def to_primitive(self) -> dict[str, object]:
        return {
            "approved": self.approvals.to_primitive(),
            "base_branch": self.base_branch,
            "goal": self.goal,
            "max_concurrency": self.max_concurrency,
            "protected_paths": list(self.protected_paths),
            "requirements": [item.to_primitive() for item in self.requirements],
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "tasks": [item.to_primitive() for item in self.tasks],
        }

    def canonical_json_bytes(self) -> bytes:
        from .serialization import canonical_json_bytes

        return canonical_json_bytes(self.to_primitive())


Manifest = ExecutionManifest
