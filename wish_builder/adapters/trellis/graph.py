"""Deterministic projection of a Wish Builder snapshot derived from Trellis tasks.

Trellis remains the editable graph authority.  This module only admits one
complete derived snapshot, validates its topology, and derives the immutable manifest
that Wish Builder can submit to Gate B.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Final

from wish_builder.contracts import (
    GRAPH_PROJECTION_VERSION,
    ExecutionBudgetPolicy,
    ExecutionManifestV2,
    GateApproval,
    ManifestRequirement,
    ManifestTask,
    NullGateApproval,
    PathCaseMode,
    RequirementStatus,
    SchedulerMode,
    Severity,
    SUPPORTED_TRELLIS_VERSION,
    TaskIdMapping,
    ValidationReport,
    WorkerProvider,
    canonical_json_bytes,
    decode_manifest_v2_primitive,
    validate_lease_timing,
)
from wish_builder.contracts.decoder import (
    DecodeLimits,
    _audit_shape,
    _decode_json_bytes,
)
from wish_builder.contracts.models import (
    HASH_RE,
    MAX_COLLECTION_ITEMS,
    MAX_ID_LENGTH,
    MAX_PATH_LENGTH,
    MAX_TASKS,
    MAX_TEXT_LENGTH,
    _has_disallowed_contract_control,
)
from wish_builder.kernel.validation import validate_manifest
from wish_builder.services.ports.trellis import (
    MAX_GRAPH_SNAPSHOT_BYTES,
    TrellisGraphSnapshot,
)

SUPPORTED_TRELLIS_EXPORT_VERSION: Final[str] = "wish-builder.trellis-graph.v1"
TRELLIS_GRAPH_PAYLOAD_SCHEMA_VERSION: Final[int] = 1
MAX_IMPORT_SNAPSHOT_BYTES: Final[int] = MAX_GRAPH_SNAPSHOT_BYTES

# The strict decoder is shared with manifest-v2.  The larger byte/string
# limits are needed because a complete Trellis export may contain structured
# argv values, while the final manifest contract still enforces field limits.
_IMPORT_LIMITS = DecodeLimits(
    max_bytes=MAX_IMPORT_SNAPSHOT_BYTES,
    max_depth=64,
    max_items=1_000_000,
    max_string_length=128 * 1024,
)

_ROOT_FIELDS = {
    "schema_version",
    "parent_task_id",
    "revision",
    "requirements",
    "tasks",
}
_ROOT_LIFECYCLE_FIELDS = {
    "history",
    "lifecycle",
    "presentation",
    "progress",
    "status",
}
_REQUIREMENT_FIELDS = {"id", "text", "status", "decision_ref"}
_REQUIREMENT_LIFECYCLE_FIELDS = {
    "history",
    "lifecycle",
    "presentation",
    "progress",
    "implemented_at",
    "updated_at",
}
_TASK_FIELDS = {
    "id",
    "title",
    "requirement_ids",
    "depends_on",
    "owned_paths",
    "allowed_auxiliary_paths",
    "acceptance_criteria",
    "regression_commands",
    "rollback",
    "documentation",
    "wave",
    "risk",
    "may_change_contracts",
    "instruction_context_digest",
    "approved_document_digests",
    "task_packet_template_digest",
}
_TASK_LIFECYCLE_FIELDS = {
    "status",
    "history",
    "lifecycle",
    "presentation",
    "progress",
    "issue_id",
    "branch",
    "pr_id",
    "squash_commit",
    "agent_owner",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
    "archived_at",
}
_REQUIRED_ROOT_FIELDS = frozenset(_ROOT_FIELDS)
_REQUIRED_REQUIREMENT_FIELDS = frozenset(_REQUIREMENT_FIELDS)
_REQUIRED_TASK_FIELDS = frozenset(_TASK_FIELDS)
_ZERO_DIGEST = "sha256:" + "0" * 64


class TrellisGraphImportError(ValueError):
    """A stable reason for rejecting a Wish Builder-derived Trellis snapshot."""

    def __init__(
        self,
        code: str,
        path: tuple[str | int, ...] = (),
        message: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.message = message or code
        super().__init__(f"{code} at {self._path_text(path)}: {self.message}")

    @staticmethod
    def _path_text(path: tuple[str | int, ...]) -> str:
        if not path:
            return "$"
        return "/" + "/".join(
            str(item).replace("~", "~0").replace("/", "~1")
            for item in path
        )


@dataclass(frozen=True, slots=True)
class TrellisImportSettings:
    """Caller-owned fields that are not part of the editable Trellis graph."""

    run_id: str
    goal: str
    base_branch: str
    imported_at: str
    gate_a: GateApproval
    provider: WorkerProvider
    capability_digest: str
    launch_profile_digest: str
    policy_digest: str
    execution_budget: ExecutionBudgetPolicy
    max_concurrency: int
    lease_ttl_seconds: int
    lease_clock_skew_seconds: int
    path_case_mode: PathCaseMode
    protected_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.gate_a) is not GateApproval:
            raise TypeError("gate_a must be a GateApproval")
        if type(self.provider) is not WorkerProvider:
            raise TypeError("provider must be a WorkerProvider")
        if type(self.execution_budget) is not ExecutionBudgetPolicy:
            raise TypeError("execution_budget must be an ExecutionBudgetPolicy")
        if type(self.path_case_mode) is not PathCaseMode:
            raise TypeError("path_case_mode must be a PathCaseMode")
        if type(self.max_concurrency) is not int:
            raise TypeError("max_concurrency must be an integer")
        validate_lease_timing(
            self.lease_ttl_seconds,
            self.lease_clock_skew_seconds,
        )
        if type(self.protected_paths) is not tuple or not all(
            type(item) is str for item in self.protected_paths
        ):
            raise TypeError("protected_paths must be a tuple of strings")


@dataclass(frozen=True, slots=True)
class TrellisImportResult:
    manifest: ExecutionManifestV2
    trellis_graph_digest: str
    gate_b_invalidated: bool

    def __post_init__(self) -> None:
        if type(self.manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if type(self.trellis_graph_digest) is not str or not HASH_RE.fullmatch(
            self.trellis_graph_digest
        ):
            raise ValueError("trellis_graph_digest must be a full sha256 reference")
        if type(self.gate_b_invalidated) is not bool:
            raise TypeError("gate_b_invalidated must be a boolean")
        if self.manifest.trellis_graph_digest != self.trellis_graph_digest:
            raise ValueError("manifest and result graph digests must match")


def import_trellis_snapshot(
    snapshot: TrellisGraphSnapshot,
    settings: TrellisImportSettings,
    *,
    approved_graph_digest: str | None = None,
) -> TrellisImportResult:
    """Import one complete snapshot and return a deterministic manifest.

    ``approved_graph_digest`` is deliberately a comparison input only.  Gate
    state is owned by the caller's Journal and is never mutated here.
    """

    if type(snapshot) is not TrellisGraphSnapshot:
        raise TypeError("snapshot must be a TrellisGraphSnapshot")
    if type(settings) is not TrellisImportSettings:
        raise TypeError("settings must be a TrellisImportSettings")
    if approved_graph_digest is not None and (
        type(approved_graph_digest) is not str
        or not HASH_RE.fullmatch(approved_graph_digest)
    ):
        raise TrellisGraphImportError(
            "invalid_approved_graph_digest",
            ("approved_graph_digest",),
            "Expected a full sha256 reference or null.",
        )

    if snapshot.export_version != SUPPORTED_TRELLIS_EXPORT_VERSION:
        raise TrellisGraphImportError(
            "unsupported_export_version",
            ("export_version",),
            f"Only derived snapshot format {SUPPORTED_TRELLIS_EXPORT_VERSION} is supported.",
        )
    if snapshot.trellis_version != SUPPORTED_TRELLIS_VERSION:
        raise TrellisGraphImportError(
            "unsupported_trellis_version",
            ("trellis_version",),
            f"Only official Trellis {SUPPORTED_TRELLIS_VERSION} is supported.",
        )
    if not snapshot.complete:
        raise TrellisGraphImportError(
            "incomplete_export",
            ("complete",),
            "The importer requires one complete Wish Builder-derived task snapshot.",
        )

    payload = _decode_snapshot_bytes(snapshot.snapshot_bytes)
    root = _closed_object(
        payload,
        path=(),
        allowed=_ROOT_FIELDS | _ROOT_LIFECYCLE_FIELDS,
        required=_REQUIRED_ROOT_FIELDS,
    )
    schema_version = _exact_int(root["schema_version"], ("schema_version",))
    if schema_version != TRELLIS_GRAPH_PAYLOAD_SCHEMA_VERSION:
        raise TrellisGraphImportError(
            "unsupported_schema_version",
            ("schema_version",),
            f"Only Trellis graph payload schema {TRELLIS_GRAPH_PAYLOAD_SCHEMA_VERSION} is supported.",
        )

    parent_task_id = _text(
        root["parent_task_id"],
        ("parent_task_id",),
        limit=MAX_PATH_LENGTH,
    )
    revision = _nullable_text(root["revision"], ("revision",), limit=MAX_PATH_LENGTH)
    if parent_task_id != snapshot.parent_task_id:
        raise TrellisGraphImportError(
            "envelope_payload_mismatch",
            ("parent_task_id",),
            "Payload parent_task_id does not match the snapshot envelope.",
        )
    if revision != snapshot.revision:
        raise TrellisGraphImportError(
            "envelope_payload_mismatch",
            ("revision",),
            "Payload revision does not match the snapshot envelope.",
        )

    tasks_raw = _list_value(root["tasks"], ("tasks",))
    # This check intentionally precedes ID/dependency pairwise work.  It is a
    # hard admission bound and keeps hostile oversized graphs cheap to reject.
    if len(tasks_raw) > MAX_TASKS:
        raise TrellisGraphImportError(
            "task_limit_exceeded",
            ("tasks",),
            f"Tasks exceed the active limit of {MAX_TASKS}.",
        )
    if not tasks_raw:
        raise TrellisGraphImportError(
            "empty_tasks",
            ("tasks",),
            "A Trellis graph must contain at least one task.",
        )

    tasks = _project_tasks(tasks_raw)
    source_ids = tuple(task["_source_id"] for task in tasks)
    source_id_set = set(source_ids)
    mapping = {
        source_id: f"TASK-{index:03d}"
        for index, source_id in enumerate(
            sorted(source_ids, key=_utf8_key),
            start=1,
        )
    }
    projected_tasks = _rewrite_tasks(tasks, mapping, source_id_set)
    _validate_dependency_cycle(projected_tasks)

    requirements_raw = _list_value(root["requirements"], ("requirements",))
    if not requirements_raw:
        raise TrellisGraphImportError(
            "empty_requirements",
            ("requirements",),
            "A Trellis graph must contain at least one requirement.",
        )
    if len(requirements_raw) > MAX_COLLECTION_ITEMS:
        raise TrellisGraphImportError(
            "requirement_limit_exceeded",
            ("requirements",),
            f"Requirements exceed {MAX_COLLECTION_ITEMS} entries.",
        )
    requirements = _project_requirements(requirements_raw)
    projected_requirements = tuple(
        sorted(requirements, key=lambda item: _utf8_key(item["id"]))
    )

    candidate = _manifest_primitive(
        settings=settings,
        parent_task_id=parent_task_id,
        revision=revision,
        mapping=mapping,
        requirements=projected_requirements,
        tasks=projected_tasks,
        graph_digest=_ZERO_DIGEST,
    )
    admitted = _admit_manifest(candidate)
    _validate_requirement_coverage(admitted)
    _validate_execution_policy(admitted)

    graph_digest = _graph_digest(
        parent_task_id=parent_task_id,
        mapping=admitted.task_id_mapping,
        requirements=admitted.requirements,
        tasks=admitted.tasks,
    )
    candidate["trellis_graph_digest"] = graph_digest
    manifest = _admit_manifest(candidate)
    if manifest.trellis_graph_digest != graph_digest:  # pragma: no cover - contract guard
        raise TrellisGraphImportError(
            "graph_digest_mismatch",
            ("trellis_graph_digest",),
            "Manifest admission changed the computed graph digest.",
        )
    return TrellisImportResult(
        manifest=manifest,
        trellis_graph_digest=graph_digest,
        gate_b_invalidated=(
            approved_graph_digest is not None
            and approved_graph_digest != graph_digest
        ),
    )


def _decode_snapshot_bytes(raw: bytes) -> object:
    decoded = _decode_json_bytes(raw, limits=_IMPORT_LIMITS)
    if not decoded.ok:
        issue = decoded.issues[0]
        raise TrellisGraphImportError(
            issue.reason_code.value,
            issue.path,
            issue.message,
        )
    # _decode_json_bytes returns a small private wrapper so domain decoders can
    # share the strict parser.  Keep that wrapper inside this adapter boundary.
    assert decoded.value is not None
    value = decoded.value.value  # type: ignore[union-attr]
    report = ValidationReport(_audit_shape(value, _IMPORT_LIMITS))
    if not report.ok:
        issue = report.issues[0]
        raise TrellisGraphImportError(
            issue.reason_code.value,
            issue.path,
            issue.message,
        )
    return value


def _closed_object(
    value: object,
    *,
    path: tuple[str | int, ...],
    allowed: set[str],
    required: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        raise TrellisGraphImportError(
            "wrong_container_type",
            path,
            "Expected a JSON object.",
        )
    unknown = sorted(set(value) - allowed, key=_utf8_key)
    if unknown:
        raise TrellisGraphImportError(
            "unknown_field",
            path + (unknown[0],),
            "Unknown fields are not admitted by the derived graph schema.",
        )
    missing = sorted(required - set(value), key=_utf8_key)
    if missing:
        raise TrellisGraphImportError(
            "missing_field",
            path + (missing[0],),
            "A required field is missing.",
        )
    return value


def _list_value(value: object, path: tuple[str | int, ...]) -> list[object]:
    if type(value) is not list:
        raise TrellisGraphImportError(
            "wrong_container_type",
            path,
            "Expected a JSON array.",
        )
    return value


def _exact_int(value: object, path: tuple[str | int, ...]) -> int:
    if type(value) is not int:
        raise TrellisGraphImportError(
            "wrong_primitive_type",
            path,
            "Expected an integer.",
        )
    return value


def _text(
    value: object,
    path: tuple[str | int, ...],
    *,
    limit: int = MAX_TEXT_LENGTH,
) -> str:
    if type(value) is not str:
        raise TrellisGraphImportError(
            "wrong_primitive_type",
            path,
            "Expected a string.",
        )
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    if not normalized.strip():
        raise TrellisGraphImportError(
            "empty_string",
            path,
            "The string must not be empty.",
        )
    if len(normalized) > limit:
        raise TrellisGraphImportError(
            "string_limit_exceeded",
            path,
            f"The string exceeds {limit} characters.",
        )
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise TrellisGraphImportError(
            "invalid_unicode_scalar",
            path,
            "The string must contain valid Unicode scalar values.",
        ) from exc
    if _has_disallowed_contract_control(normalized):
        raise TrellisGraphImportError(
            "disallowed_contract_control",
            path,
            "The string contains a disallowed control character.",
        )
    return normalized


def _nullable_text(
    value: object,
    path: tuple[str | int, ...],
    *,
    limit: int = MAX_TEXT_LENGTH,
) -> str | None:
    return None if value is None else _text(value, path, limit=limit)


def _identifier_text(
    value: object,
    path: tuple[str | int, ...],
    *,
    limit: int,
) -> str:
    normalized = _text(value, path, limit=limit)
    if any(ord(character) < 32 for character in normalized):
        raise TrellisGraphImportError(
            "disallowed_contract_control",
            path,
            "Identifiers must not contain control characters.",
        )
    return normalized


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8", errors="strict")


def _project_requirements(records: list[object]) -> tuple[dict[str, object], ...]:
    projected: list[dict[str, object]] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(records):
        path = ("requirements", index)
        record = _closed_object(
            raw,
            path=path,
            allowed=_REQUIREMENT_FIELDS | _REQUIREMENT_LIFECYCLE_FIELDS,
            required=_REQUIRED_REQUIREMENT_FIELDS,
        )
        identifier = _identifier_text(
            record["id"],
            path + ("id",),
            limit=MAX_ID_LENGTH,
        )
        if identifier in seen:
            raise TrellisGraphImportError(
                "duplicate_requirement_id",
                ("requirements", identifier, "id"),
                "Requirement IDs collide after NFC normalization.",
            )
        seen[identifier] = index
        text = _text(record["text"], path + ("text",))
        status = record["status"]
        if status == RequirementStatus.IMPLEMENTED.value:
            # Trellis may expose completion as lifecycle progress.  The frozen
            # graph retains the approved requirement and ignores that progress.
            status = RequirementStatus.APPROVED.value
        elif type(status) is str:
            status = _text(status, path + ("status",), limit=MAX_ID_LENGTH)
        decision_ref = record["decision_ref"]
        if decision_ref is not None:
            decision_ref = _text(
                decision_ref,
                path + ("decision_ref",),
                limit=MAX_TEXT_LENGTH,
            )
        projected.append(
            {
                "id": identifier,
                "text": text,
                "status": status,
                "decision_ref": decision_ref,
            }
        )
    return tuple(projected)


def _project_tasks(records: list[object]) -> tuple[dict[str, object], ...]:
    projected: list[dict[str, object]] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(records):
        path = ("tasks", index)
        record = _closed_object(
            raw,
            path=path,
            allowed=_TASK_FIELDS | _TASK_LIFECYCLE_FIELDS,
            required=_REQUIRED_TASK_FIELDS,
        )
        source_id = _identifier_text(
            record["id"],
            path + ("id",),
            limit=MAX_TEXT_LENGTH,
        )
        if source_id in seen:
            raise TrellisGraphImportError(
                "duplicate_task_id",
                ("tasks", source_id, "id"),
                "Task IDs collide after NFC normalization.",
            )
        seen[source_id] = index
        requirement_ids = _string_list(
            record["requirement_ids"],
            path + ("requirement_ids",),
            limit=MAX_ID_LENGTH,
        )
        depends_on = _string_list(
            record["depends_on"],
            path + ("depends_on",),
            limit=MAX_TEXT_LENGTH,
        )
        normalized_dependencies = tuple(
            _identifier_text(
                item,
                path + ("depends_on", ordinal),
                limit=MAX_TEXT_LENGTH,
            )
            for ordinal, item in enumerate(depends_on)
        )
        if len(set(normalized_dependencies)) != len(normalized_dependencies):
            raise TrellisGraphImportError(
                "duplicate_dependency",
                ("tasks", source_id, "depends_on"),
                "A task dependency may appear only once.",
            )
        normalized_requirement_ids = tuple(
            _identifier_text(
                item,
                path + ("requirement_ids", ordinal),
                limit=MAX_ID_LENGTH,
            )
            for ordinal, item in enumerate(requirement_ids)
        )
        projected.append(
            {
                "_source_id": source_id,
                "title": record["title"],
                "requirement_ids": list(normalized_requirement_ids),
                "depends_on": list(normalized_dependencies),
                "owned_paths": record["owned_paths"],
                "allowed_auxiliary_paths": record["allowed_auxiliary_paths"],
                "acceptance_criteria": record["acceptance_criteria"],
                "regression_commands": record["regression_commands"],
                "rollback": record["rollback"],
                "documentation": record["documentation"],
                "wave": record["wave"],
                "risk": record["risk"],
                "may_change_contracts": record["may_change_contracts"],
                "instruction_context_digest": record["instruction_context_digest"],
                "approved_document_digests": record["approved_document_digests"],
                "task_packet_template_digest": record["task_packet_template_digest"],
            }
        )
    return tuple(projected)


def _string_list(
    value: object,
    path: tuple[str | int, ...],
    *,
    limit: int,
) -> tuple[str, ...]:
    values = _list_value(value, path)
    result: list[str] = []
    for index, item in enumerate(values):
        result.append(_text(item, path + (index,), limit=limit))
    return tuple(result)


def _rewrite_tasks(
    tasks: tuple[dict[str, object], ...],
    mapping: dict[str, str],
    source_id_set: set[str],
) -> tuple[dict[str, object], ...]:
    result: list[dict[str, object]] = []
    for task in sorted(tasks, key=lambda item: _utf8_key(str(item["_source_id"]))):
        source_id = str(task["_source_id"])
        mapped_id = mapping[source_id]
        dependencies = tuple(task["depends_on"])  # type: ignore[arg-type]
        for dependency in dependencies:
            if dependency not in source_id_set:
                raise TrellisGraphImportError(
                    "missing_dependency",
                    ("tasks", source_id, "depends_on", dependency),
                    "The dependency does not identify an exported task.",
                )
            if dependency == source_id:
                raise TrellisGraphImportError(
                    "self_dependency",
                    ("tasks", source_id, "depends_on", dependency),
                    "A task cannot depend on itself.",
                )
        rewritten = dict(task)
        rewritten.pop("_source_id")
        rewritten["id"] = mapped_id
        rewritten["depends_on"] = [mapping[item] for item in dependencies]
        result.append(rewritten)
    return tuple(result)


def _manifest_primitive(
    *,
    settings: TrellisImportSettings,
    parent_task_id: str,
    revision: str | None,
    mapping: dict[str, str],
    requirements: tuple[dict[str, object], ...],
    tasks: tuple[dict[str, object], ...],
    graph_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "graph_projection_version": GRAPH_PROJECTION_VERSION,
        "run_id": settings.run_id,
        "goal": settings.goal,
        "base_branch": settings.base_branch,
        "trellis_parent_task_id": parent_task_id,
        "trellis_revision": revision,
        "trellis_graph_digest": graph_digest,
        "task_id_mapping": dict(mapping),
        "imported_at": settings.imported_at,
        "approved": {
            "gate_a": settings.gate_a.to_primitive(),
            "gate_b": NullGateApproval().to_primitive(),
        },
        "provider": settings.provider.value,
        "capability_digest": settings.capability_digest,
        "launch_profile_digest": settings.launch_profile_digest,
        "policy_digest": settings.policy_digest,
        "scheduler_mode": SchedulerMode.WISH_BUILDER.value,
        "execution_budget": settings.execution_budget.to_primitive(),
        "max_concurrency": settings.max_concurrency,
        "lease_ttl_seconds": settings.lease_ttl_seconds,
        "lease_clock_skew_seconds": settings.lease_clock_skew_seconds,
        "path_case_mode": settings.path_case_mode.value,
        "protected_paths": list(settings.protected_paths),
        "requirements": list(requirements),
        "tasks": list(tasks),
    }


def _admit_manifest(value: dict[str, object]) -> ExecutionManifestV2:
    decoded = decode_manifest_v2_primitive(value, limits=_IMPORT_LIMITS)
    if not decoded.ok:
        issue = decoded.issues[0]
        raise TrellisGraphImportError(issue.reason_code.value, issue.path, issue.message)
    assert decoded.value is not None
    return decoded.value


def _validate_dependency_cycle(tasks: tuple[dict[str, object], ...]) -> None:
    task_ids = tuple(str(task["id"]) for task in tasks)
    remaining = {
        str(task["id"]): len(task["depends_on"])  # type: ignore[arg-type]
        for task in tasks
    }
    dependents = {task_id: [] for task_id in task_ids}
    for task in tasks:
        task_id = str(task["id"])
        for dependency in task["depends_on"]:  # type: ignore[union-attr]
            dependents[str(dependency)].append(task_id)

    ready = sorted(
        (task_id for task_id, count in remaining.items() if count == 0),
        key=_task_number,
    )
    visited: list[str] = []
    while ready:
        task_id = ready.pop(0)
        visited.append(task_id)
        for dependent in sorted(dependents[task_id], key=_task_number):
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=_task_number)
    if len(visited) != len(task_ids):
        cyclic = sorted(
            (task_id for task_id, count in remaining.items() if count > 0),
            key=_task_number,
        )
        raise TrellisGraphImportError(
            "dependency_cycle",
            ("tasks", cyclic[0], "depends_on"),
            "The Trellis task graph contains a dependency cycle.",
        )


def _validate_requirement_coverage(manifest: ExecutionManifestV2) -> None:
    requirement_by_id = {requirement.id: requirement for requirement in manifest.requirements}
    referenced = {
        requirement_id
        for task in manifest.tasks
        for requirement_id in task.requirement_ids
    }
    orphan_approved = sorted(
        (
            requirement_id
            for requirement_id, requirement in requirement_by_id.items()
            if requirement.status is RequirementStatus.APPROVED
            and requirement_id not in referenced
        ),
        key=_task_number,
    )
    if orphan_approved:
        raise TrellisGraphImportError(
            "orphan_requirement",
            ("requirements", orphan_approved[0]),
            "An approved requirement is not covered by any task.",
        )

    approved_ids = {
        requirement.id
        for requirement in manifest.requirements
        if requirement.status is RequirementStatus.APPROVED
    }
    for task in sorted(manifest.tasks, key=lambda item: _task_number(item.id)):
        if not approved_ids.intersection(task.requirement_ids):
            raise TrellisGraphImportError(
                "orphan_task",
                ("tasks", task.id, "requirement_ids"),
                "Every task must cover at least one approved requirement.",
            )


def _validate_execution_policy(manifest: ExecutionManifestV2) -> None:
    """Apply the shared kernel policy before a Trellis graph can be frozen."""

    report = validate_manifest(manifest)
    errors = tuple(
        issue for issue in report.issues if issue.severity is Severity.ERROR
    )
    if errors:
        issue = errors[0]
        raise TrellisGraphImportError(
            issue.reason_code.value,
            issue.path,
            issue.message,
        )


def _task_number(identifier: str) -> int:
    try:
        return int(identifier.split("-", 1)[1])
    except (IndexError, ValueError):
        return 2**31


def _graph_digest(
    *,
    parent_task_id: str,
    mapping: tuple[TaskIdMapping, ...],
    requirements: tuple[ManifestRequirement, ...],
    tasks: tuple[ManifestTask, ...],
) -> str:
    projection = {
        "graph_projection_version": GRAPH_PROJECTION_VERSION,
        "trellis_parent_task_id": parent_task_id,
        "task_id_mapping": {
            item.trellis_task_id: item.task_id for item in mapping
        },
        "requirements": [item.to_primitive() for item in requirements],
        "tasks": [item.to_primitive() for item in tasks],
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


__all__ = [
    "MAX_IMPORT_SNAPSHOT_BYTES",
    "SUPPORTED_TRELLIS_EXPORT_VERSION",
    "TRELLIS_GRAPH_PAYLOAD_SCHEMA_VERSION",
    "TrellisGraphImportError",
    "TrellisImportResult",
    "TrellisImportSettings",
    "import_trellis_snapshot",
]
