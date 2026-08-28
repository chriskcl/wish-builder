"""Strict admission decoder for execution-manifest schema v2."""

from __future__ import annotations

from collections import Counter

from .decoder import (
    DEFAULT_DECODE_LIMITS,
    DecodeLimits,
    _MISSING,
    _audit_shape,
    _boolean,
    _closed_object,
    _decode_approval,
    _decode_json_bytes,
    _enum_value,
    _integer,
    _issue,
    _normalized_contract_string,
    _record_segments,
    _string,
)
from .diagnostics import (
    DecodeResult,
    DiagnosticPath,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
)
from .manifest_v2 import (
    GRAPH_PROJECTION_VERSION,
    MAX_LEASE_TTL_SECONDS,
    MIN_LEASE_TTL_SECONDS,
    BillingPosture,
    CommandSpec,
    ExecutionBudgetPolicy,
    ExecutionManifestV2,
    ManifestGateEvidence,
    ManifestRequirement,
    ManifestTask,
    NetworkPolicy,
    NullGateApproval,
    PathCaseMode,
    SchedulerMode,
    TaskIdMapping,
    WorkerProvider,
    _argument,
    _snapshot_id,
)
from .models import (
    HASH_RE,
    MAX_COLLECTION_ITEMS,
    MAX_ID_LENGTH,
    MAX_PATH_LENGTH,
    MAX_TASKS,
    MAX_TEXT_LENGTH,
    RequirementStatus,
    RiskLevel,
    _timestamp,
)
from .serialization import MAX_CANONICAL_INTEGER


def _hash_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    decoded = _string(value, path, issues, limit=71)
    if decoded is not None and not HASH_RE.fullmatch(decoded):
        issues.append(
            _issue(
                "value.sha256_reference",
                path,
                "invalid_hash",
                "Expected sha256 followed by 64 lowercase hexadecimal characters.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return decoded


def _optional_string_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    limit: int = MAX_TEXT_LENGTH,
) -> str | None:
    if value is None:
        return None
    return _string(value, path, issues, limit=limit)


def _timestamp_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    decoded = _string(value, path, issues, limit=32)
    if decoded is None:
        return None
    try:
        return _timestamp(decoded, "timestamp")
    except ValueError:
        issues.append(
            _issue(
                "value.utc_timestamp",
                path,
                "invalid_timestamp",
                "Expected a valid UTC timestamp ending in Z.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None


def _snapshot_id_value(
    value: object,
    prefix: str,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    decoded = _string(value, path, issues, limit=MAX_ID_LENGTH)
    if decoded is None:
        return None
    try:
        return _snapshot_id(decoded, prefix, "identifier")
    except ValueError:
        issues.append(
            _issue(
                "value.snapshot_id",
                path,
                "invalid_identifier",
                f"Expected a canonical {prefix}-NNN snapshot identifier.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None


def _positive_integer(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    maximum: int = MAX_CANONICAL_INTEGER,
) -> int | None:
    decoded = _integer(value, path, issues)
    if decoded is not None and not 1 <= decoded <= maximum:
        issues.append(
            _issue(
                "value.positive_integer",
                path,
                "integer_out_of_range",
                f"Expected an integer from 1 through {maximum}.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return decoded


def _bounded_integer(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    decoded = _integer(value, path, issues)
    if decoded is not None and not minimum <= decoded <= maximum:
        issues.append(
            _issue(
                "value.bounded_integer",
                path,
                "integer_out_of_range",
                f"Expected an integer from {minimum} through {maximum}.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return decoded


def _string_array(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    nonempty: bool,
    unique: bool,
    limit: int = MAX_TEXT_LENGTH,
) -> tuple[str, ...] | None:
    if value is _MISSING:
        return None
    if type(value) is not list:
        issues.append(
            _issue(
                "schema.array_type",
                path,
                "wrong_container_type",
                "Expected a JSON array.",
            )
        )
        return None
    if nonempty and not value:
        issues.append(
            _issue(
                "value.nonempty_array",
                path,
                "empty_collection",
                "The array must not be empty.",
                stage=ValidationStage.LOCAL,
            )
        )
    if len(value) > MAX_COLLECTION_ITEMS:
        issues.append(
            _issue(
                "value.collection_limit",
                path,
                "item_limit_exceeded",
                f"The array exceeds {MAX_COLLECTION_ITEMS} entries.",
                stage=ValidationStage.LOCAL,
            )
        )
    result: list[str] = []
    segments = _record_segments(value)
    for index, item in enumerate(value):
        decoded = _string(
            item,
            path + (segments[index],),
            issues,
            limit=limit,
        )
        if decoded is not None:
            result.append(decoded)
    if len(result) != len(value) or (nonempty and not result):
        return None
    if unique and len(set(result)) != len(result):
        issues.append(
            _issue(
                "value.duplicate_array_item",
                path,
                "duplicate_item",
                "The array must not contain duplicate values.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return tuple(result)


def _argument_array(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> tuple[str, ...] | None:
    if value is _MISSING:
        return None
    if type(value) is not list:
        issues.append(
            _issue(
                "schema.array_type",
                path,
                "wrong_container_type",
                "Expected a JSON argv array.",
            )
        )
        return None
    if not value:
        issues.append(
            _issue(
                "value.nonempty_array",
                path,
                "empty_collection",
                "argv must not be empty.",
                stage=ValidationStage.LOCAL,
            )
        )
    if len(value) > 4_096:
        issues.append(
            _issue(
                "value.collection_limit",
                path,
                "item_limit_exceeded",
                "argv exceeds 4096 entries.",
                stage=ValidationStage.LOCAL,
            )
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if type(item) is not str:
            issues.append(
                _issue(
                    "schema.string_type",
                    path + (index,),
                    "wrong_primitive_type",
                    "Expected an argv string.",
                )
            )
            continue
        try:
            result.append(_argument(item, f"argv[{index}]"))
        except ValueError:
            issues.append(
                _issue(
                    "value.command_argument",
                    path + (index,),
                    "invalid_command_spec",
                    "The argv value is not a bounded Unicode argument.",
                    stage=ValidationStage.LOCAL,
                )
            )
    return tuple(result) if len(result) == len(value) and result else None


def _hash_array(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> tuple[str, ...] | None:
    decoded = _string_array(
        value,
        path,
        issues,
        nonempty=False,
        unique=True,
        limit=71,
    )
    if decoded is None:
        return None
    before = len(issues)
    segments = _record_segments(value)
    for index, item in enumerate(decoded):
        if not HASH_RE.fullmatch(item):
            issues.append(
                _issue(
                    "value.sha256_reference",
                    path + (segments[index],),
                    "invalid_hash",
                    "Expected sha256 followed by 64 lowercase hexadecimal characters.",
                    stage=ValidationStage.LOCAL,
                )
            )
    return None if len(issues) != before else decoded


def _snapshot_id_array(
    value: object,
    prefix: str,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    nonempty: bool,
) -> tuple[str, ...] | None:
    decoded = _string_array(
        value,
        path,
        issues,
        nonempty=nonempty,
        unique=True,
        limit=MAX_ID_LENGTH,
    )
    if decoded is None:
        return None
    before = len(issues)
    segments = _record_segments(value)
    admitted: list[str] = []
    for index, item in enumerate(decoded):
        try:
            admitted.append(_snapshot_id(item, prefix, "identifier"))
        except ValueError:
            issues.append(
                _issue(
                    "value.snapshot_id",
                    path + (segments[index],),
                    "invalid_identifier",
                    f"Expected a canonical {prefix}-NNN snapshot identifier.",
                    stage=ValidationStage.LOCAL,
                )
            )
    return None if len(issues) != before else tuple(admitted)


def _decode_null_gate_approval(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> NullGateApproval | None:
    fields = {"approved_by", "approved_at", "artifact_hash"}
    data = _closed_object(
        value,
        path=path,
        allowed=fields,
        required=fields,
        issues=issues,
    )
    if data is None:
        return None
    before = len(issues)
    for field in sorted(fields):
        if field in data and data[field] is not None:
            issues.append(
                _issue(
                    "value.gate_b_null",
                    path + (field,),
                    "invalid_gate_approval",
                    "Gate B evidence must remain null inside manifest v2.",
                    stage=ValidationStage.LOCAL,
                )
            )
    return None if len(issues) != before else NullGateApproval()


_COMMAND_FIELDS = {
    "executable_profile",
    "executable_identity_digest",
    "argv",
    "working_directory",
    "timeout_seconds",
    "stdout_limit_bytes",
    "stderr_limit_bytes",
    "result_limit_bytes",
    "environment_allowlist",
    "network_policy",
    "display_text",
}


def _decode_command_spec(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> CommandSpec | None:
    data = _closed_object(
        value,
        path=path,
        allowed=_COMMAND_FIELDS,
        required=_COMMAND_FIELDS,
        issues=issues,
    )
    if data is None:
        return None
    before = len(issues)
    executable_profile = _string(
        data.get("executable_profile", _MISSING),
        path + ("executable_profile",),
        issues,
        limit=128,
    )
    executable_identity_digest = _hash_value(
        data.get("executable_identity_digest", _MISSING),
        path + ("executable_identity_digest",),
        issues,
    )
    argv = _argument_array(
        data.get("argv", _MISSING),
        path + ("argv",),
        issues,
    )
    working_directory = _string(
        data.get("working_directory", _MISSING),
        path + ("working_directory",),
        issues,
        limit=MAX_PATH_LENGTH,
    )
    timeout_seconds = _positive_integer(
        data.get("timeout_seconds", _MISSING),
        path + ("timeout_seconds",),
        issues,
        maximum=24 * 60 * 60,
    )
    stdout_limit_bytes = _positive_integer(
        data.get("stdout_limit_bytes", _MISSING),
        path + ("stdout_limit_bytes",),
        issues,
    )
    stderr_limit_bytes = _positive_integer(
        data.get("stderr_limit_bytes", _MISSING),
        path + ("stderr_limit_bytes",),
        issues,
    )
    result_limit_bytes = _positive_integer(
        data.get("result_limit_bytes", _MISSING),
        path + ("result_limit_bytes",),
        issues,
    )
    environment_allowlist = _string_array(
        data.get("environment_allowlist", _MISSING),
        path + ("environment_allowlist",),
        issues,
        nonempty=False,
        unique=True,
        limit=128,
    )
    network_policy = _enum_value(
        data.get("network_policy", _MISSING),
        NetworkPolicy,
        path + ("network_policy",),
        issues,
    )
    display_text = _string(
        data.get("display_text", _MISSING),
        path + ("display_text",),
        issues,
    )
    required = (
        executable_profile,
        executable_identity_digest,
        argv,
        working_directory,
        timeout_seconds,
        stdout_limit_bytes,
        stderr_limit_bytes,
        result_limit_bytes,
        environment_allowlist,
        network_policy,
        display_text,
    )
    if len(issues) != before or any(item is None for item in required):
        return None
    try:
        return CommandSpec(
            executable_profile=executable_profile,
            executable_identity_digest=executable_identity_digest,
            argv=argv,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
            result_limit_bytes=result_limit_bytes,
            environment_allowlist=environment_allowlist,
            network_policy=network_policy,
            display_text=display_text,
        )
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "value.command_spec",
                path,
                "invalid_command_spec",
                "CommandSpec fields are not jointly valid.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None


def _decode_command_list(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> tuple[CommandSpec, ...] | None:
    if value is _MISSING:
        return None
    if type(value) is not list:
        issues.append(
            _issue(
                "schema.array_type",
                path,
                "wrong_container_type",
                "Expected an array of CommandSpec objects, never shell text.",
            )
        )
        return None
    if not value:
        issues.append(
            _issue(
                "value.nonempty_array",
                path,
                "empty_collection",
                "At least one acceptance CommandSpec is required.",
                stage=ValidationStage.LOCAL,
            )
        )
    if len(value) > MAX_COLLECTION_ITEMS:
        issues.append(
            _issue(
                "value.collection_limit",
                path,
                "item_limit_exceeded",
                f"The array exceeds {MAX_COLLECTION_ITEMS} entries.",
                stage=ValidationStage.LOCAL,
            )
        )
    result: list[CommandSpec] = []
    before = len(issues)
    for index, item in enumerate(value):
        decoded = _decode_command_spec(item, path + (index,), issues)
        if decoded is not None:
            result.append(decoded)
    if len(issues) != before or len(result) != len(value) or not result:
        return None
    return tuple(result)


_BUDGET_FIELDS = {
    "max_attempts_per_task",
    "max_attempts_per_run",
    "attempt_deadline_seconds",
    "total_worker_seconds",
    "max_output_bytes",
    "max_retained_evidence_bytes",
    "max_concurrent_workers",
    "billing_posture",
}


def _decode_budget(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> ExecutionBudgetPolicy | None:
    data = _closed_object(
        value,
        path=path,
        allowed=_BUDGET_FIELDS,
        required=_BUDGET_FIELDS,
        issues=issues,
    )
    if data is None:
        return None
    before = len(issues)
    integers = {
        field: _positive_integer(data.get(field, _MISSING), path + (field,), issues)
        for field in _BUDGET_FIELDS - {"billing_posture"}
    }
    if (
        integers["max_concurrent_workers"] is not None
        and integers["max_concurrent_workers"] > MAX_TASKS
    ):
        issues.append(
            _issue(
                "value.max_concurrent_workers",
                path + ("max_concurrent_workers",),
                "integer_out_of_range",
                f"Concurrency cannot exceed the manifest task limit of {MAX_TASKS}.",
                stage=ValidationStage.LOCAL,
            )
        )
    billing_posture = _enum_value(
        data.get("billing_posture", _MISSING),
        BillingPosture,
        path + ("billing_posture",),
        issues,
    )
    if (
        len(issues) != before
        or any(item is None for item in integers.values())
        or billing_posture is None
    ):
        return None
    try:
        return ExecutionBudgetPolicy(
            max_attempts_per_task=integers["max_attempts_per_task"],
            max_attempts_per_run=integers["max_attempts_per_run"],
            attempt_deadline_seconds=integers["attempt_deadline_seconds"],
            total_worker_seconds=integers["total_worker_seconds"],
            max_output_bytes=integers["max_output_bytes"],
            max_retained_evidence_bytes=integers["max_retained_evidence_bytes"],
            max_concurrent_workers=integers["max_concurrent_workers"],
            billing_posture=billing_posture,
        )
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "value.execution_budget",
                path,
                "invalid_execution_budget",
                "ExecutionBudgetPolicy fields are not jointly valid.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None


_REQUIREMENT_FIELDS = {"id", "text", "status", "decision_ref"}


def _decode_requirement(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> ManifestRequirement | None:
    data = _closed_object(
        value,
        path=path,
        allowed=_REQUIREMENT_FIELDS,
        required=_REQUIREMENT_FIELDS,
        issues=issues,
    )
    if data is None:
        return None
    before = len(issues)
    requirement_id = _snapshot_id_value(
        data.get("id", _MISSING),
        "REQ",
        path + ("id",),
        issues,
    )
    text = _string(data.get("text", _MISSING), path + ("text",), issues)
    status = _enum_value(
        data.get("status", _MISSING),
        RequirementStatus,
        path + ("status",),
        issues,
    )
    decision_ref = _optional_string_value(
        data.get("decision_ref", _MISSING),
        path + ("decision_ref",),
        issues,
    )
    if data.get("decision_ref", _MISSING) is _MISSING:
        decision_ref_marker: object = _MISSING
    else:
        decision_ref_marker = decision_ref
    required = (requirement_id, text, status, decision_ref_marker)
    if (
        len(issues) != before
        or any(item is _MISSING or item is None for item in required[:3])
        or decision_ref_marker is _MISSING
    ):
        return None
    try:
        return ManifestRequirement(requirement_id, text, status, decision_ref)
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "value.requirement_contract",
                path,
                "invalid_manifest",
                "Requirement fields are not jointly valid for manifest v2.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None


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


def _decode_task(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> ManifestTask | None:
    data = _closed_object(
        value,
        path=path,
        allowed=_TASK_FIELDS,
        required=_TASK_FIELDS,
        issues=issues,
    )
    if data is None:
        return None
    before = len(issues)
    task_id = _snapshot_id_value(
        data.get("id", _MISSING),
        "TASK",
        path + ("id",),
        issues,
    )
    title = _string(data.get("title", _MISSING), path + ("title",), issues)
    requirement_ids = _snapshot_id_array(
        data.get("requirement_ids", _MISSING),
        "REQ",
        path + ("requirement_ids",),
        issues,
        nonempty=True,
    )
    depends_on = _snapshot_id_array(
        data.get("depends_on", _MISSING),
        "TASK",
        path + ("depends_on",),
        issues,
        nonempty=False,
    )
    owned_paths = _string_array(
        data.get("owned_paths", _MISSING),
        path + ("owned_paths",),
        issues,
        nonempty=True,
        unique=True,
        limit=MAX_PATH_LENGTH,
    )
    auxiliary_paths = _string_array(
        data.get("allowed_auxiliary_paths", _MISSING),
        path + ("allowed_auxiliary_paths",),
        issues,
        nonempty=False,
        unique=True,
        limit=MAX_PATH_LENGTH,
    )
    acceptance_criteria = _string_array(
        data.get("acceptance_criteria", _MISSING),
        path + ("acceptance_criteria",),
        issues,
        nonempty=True,
        unique=False,
    )
    regression_commands = _decode_command_list(
        data.get("regression_commands", _MISSING),
        path + ("regression_commands",),
        issues,
    )
    rollback = _string(data.get("rollback", _MISSING), path + ("rollback",), issues)
    documentation = _string_array(
        data.get("documentation", _MISSING),
        path + ("documentation",),
        issues,
        nonempty=False,
        unique=True,
        limit=MAX_PATH_LENGTH,
    )
    wave = _integer(data.get("wave", _MISSING), path + ("wave",), issues)
    risk = _enum_value(data.get("risk", _MISSING), RiskLevel, path + ("risk",), issues)
    may_change_contracts = _boolean(
        data.get("may_change_contracts", _MISSING),
        path + ("may_change_contracts",),
        issues,
    )
    instruction_context_digest = (
        None
        if data.get("instruction_context_digest", _MISSING) is None
        else _hash_value(
            data.get("instruction_context_digest", _MISSING),
            path + ("instruction_context_digest",),
            issues,
        )
    )
    approved_document_digests = _hash_array(
        data.get("approved_document_digests", _MISSING),
        path + ("approved_document_digests",),
        issues,
    )
    task_packet_template_digest = (
        None
        if data.get("task_packet_template_digest", _MISSING) is None
        else _hash_value(
            data.get("task_packet_template_digest", _MISSING),
            path + ("task_packet_template_digest",),
            issues,
        )
    )
    expanded = (
        instruction_context_digest is not None
        and bool(approved_document_digests)
        and task_packet_template_digest is None
        and data.get("task_packet_template_digest") is None
    )
    template = (
        data.get("instruction_context_digest") is None
        and not approved_document_digests
        and task_packet_template_digest is not None
    )
    if (
        all(field in data for field in (
            "instruction_context_digest",
            "approved_document_digests",
            "task_packet_template_digest",
        ))
        and approved_document_digests is not None
        and not (expanded or template)
    ):
        issues.append(
            _issue(
                "value.frozen_worker_inputs",
                path,
                "invalid_frozen_inputs",
                "Provide context plus approved documents or one template digest, exclusively.",
                stage=ValidationStage.LOCAL,
            )
        )
    required = (
        task_id,
        title,
        requirement_ids,
        depends_on,
        owned_paths,
        auxiliary_paths,
        acceptance_criteria,
        regression_commands,
        rollback,
        documentation,
        wave,
        risk,
        may_change_contracts,
        approved_document_digests,
    )
    if len(issues) != before or any(item is None for item in required):
        return None
    try:
        return ManifestTask(
            id=task_id,
            title=title,
            requirement_ids=requirement_ids,
            depends_on=depends_on,
            owned_paths=owned_paths,
            allowed_auxiliary_paths=auxiliary_paths,
            acceptance_criteria=acceptance_criteria,
            regression_commands=regression_commands,
            rollback=rollback,
            documentation=documentation,
            wave=wave,
            risk=risk,
            may_change_contracts=may_change_contracts,
            instruction_context_digest=instruction_context_digest,
            approved_document_digests=approved_document_digests,
            task_packet_template_digest=task_packet_template_digest,
        )
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "value.task_contract",
                path,
                "invalid_task",
                "Manifest v2 task fields are not jointly valid.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None


def _decode_mapping(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> tuple[TaskIdMapping, ...] | None:
    if value is _MISSING:
        return None
    if type(value) is not dict:
        issues.append(
            _issue(
                "schema.object_type",
                path,
                "wrong_container_type",
                "Expected a Trellis-task-ID to TASK-NNN object.",
            )
        )
        return None
    if not value:
        issues.append(
            _issue(
                "value.nonempty_object",
                path,
                "empty_collection",
                "task_id_mapping must not be empty.",
                stage=ValidationStage.LOCAL,
            )
        )
    if len(value) > MAX_TASKS:
        issues.append(
            _issue(
                "value.task_limit",
                path,
                "item_limit_exceeded",
                f"task_id_mapping exceeds {MAX_TASKS} entries.",
                stage=ValidationStage.LOCAL,
            )
        )
    before = len(issues)
    result: list[TaskIdMapping] = []
    for source_id, raw_task_id in sorted(value.items(), key=lambda item: item[0].encode("utf-8")):
        normalized_source = _normalized_contract_string(source_id)
        if not normalized_source.strip() or len(normalized_source) > MAX_TEXT_LENGTH:
            issues.append(
                _issue(
                    "value.trellis_task_id",
                    path + (source_id[:256],),
                    "invalid_identifier",
                    "Trellis task IDs must be non-empty bounded NFC strings.",
                    stage=ValidationStage.LOCAL,
                )
            )
            continue
        task_id = _snapshot_id_value(
            raw_task_id,
            "TASK",
            path + (normalized_source,),
            issues,
        )
        if task_id is None:
            continue
        try:
            result.append(TaskIdMapping(normalized_source, task_id))
        except (TypeError, ValueError):
            issues.append(
                _issue(
                    "value.task_id_mapping_entry",
                    path + (normalized_source,),
                    "invalid_mapping",
                    "Mapping targets must be canonical TASK-NNN identifiers.",
                    stage=ValidationStage.LOCAL,
                )
            )
    if len(issues) != before or len(result) != len(value) or not result:
        return None
    if len({item.task_id for item in result}) != len(result):
        issues.append(
            _issue(
                "value.task_id_mapping_bijection",
                path,
                "invalid_mapping",
                "task_id_mapping targets must be unique.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return tuple(result)


_MANIFEST_FIELDS = {
    "schema_version",
    "graph_projection_version",
    "run_id",
    "goal",
    "base_branch",
    "trellis_parent_task_id",
    "trellis_revision",
    "trellis_graph_digest",
    "task_id_mapping",
    "imported_at",
    "approved",
    "provider",
    "capability_digest",
    "launch_profile_digest",
    "policy_digest",
    "scheduler_mode",
    "execution_budget",
    "max_concurrency",
    "lease_ttl_seconds",
    "lease_clock_skew_seconds",
    "path_case_mode",
    "protected_paths",
    "requirements",
    "tasks",
}


def _decode_manifest_v2_shape(
    value: object,
) -> tuple[ExecutionManifestV2 | None, tuple[ValidationIssue, ...]]:
    issues: list[ValidationIssue] = []
    data = _closed_object(
        value,
        path=(),
        allowed=_MANIFEST_FIELDS,
        required=_MANIFEST_FIELDS,
        issues=issues,
    )
    if data is None:
        return None, tuple(issues)
    schema_version = _integer(data.get("schema_version", _MISSING), ("schema_version",), issues)
    if schema_version is not None and schema_version != 2:
        issues.append(
            _issue(
                "value.schema_version",
                ("schema_version",),
                "unsupported_schema_version",
                "Only execution manifest schema version 2 is supported here.",
                stage=ValidationStage.LOCAL,
            )
        )
    graph_projection_version = _integer(
        data.get("graph_projection_version", _MISSING),
        ("graph_projection_version",),
        issues,
    )
    if (
        graph_projection_version is not None
        and graph_projection_version != GRAPH_PROJECTION_VERSION
    ):
        issues.append(
            _issue(
                "value.graph_projection_version",
                ("graph_projection_version",),
                "unsupported_schema_version",
                f"Only graph projection version {GRAPH_PROJECTION_VERSION} is supported.",
                stage=ValidationStage.LOCAL,
            )
        )
    run_id = _string(data.get("run_id", _MISSING), ("run_id",), issues, limit=MAX_ID_LENGTH)
    goal = _string(data.get("goal", _MISSING), ("goal",), issues)
    base_branch = _string(
        data.get("base_branch", _MISSING),
        ("base_branch",),
        issues,
        limit=MAX_PATH_LENGTH,
    )
    trellis_parent_task_id = _string(
        data.get("trellis_parent_task_id", _MISSING),
        ("trellis_parent_task_id",),
        issues,
    )
    trellis_revision_present = "trellis_revision" in data
    trellis_revision_raw = data.get("trellis_revision", _MISSING)
    trellis_revision = (
        None
        if trellis_revision_raw is None
        else _hash_value(trellis_revision_raw, ("trellis_revision",), issues)
    )
    trellis_graph_digest = _hash_value(
        data.get("trellis_graph_digest", _MISSING),
        ("trellis_graph_digest",),
        issues,
    )
    task_id_mapping = _decode_mapping(
        data.get("task_id_mapping", _MISSING),
        ("task_id_mapping",),
        issues,
    )
    imported_at = _timestamp_value(
        data.get("imported_at", _MISSING),
        ("imported_at",),
        issues,
    )

    approved_data = _closed_object(
        data.get("approved", _MISSING),
        path=("approved",),
        allowed={"gate_a", "gate_b"},
        required={"gate_a", "gate_b"},
        issues=issues,
    )
    gate_a = None
    gate_b = None
    if approved_data is not None:
        gate_a = _decode_approval(
            approved_data.get("gate_a", _MISSING),
            ("approved", "gate_a"),
            issues,
        )
        gate_b = _decode_null_gate_approval(
            approved_data.get("gate_b", _MISSING),
            ("approved", "gate_b"),
            issues,
        )

    provider = _enum_value(data.get("provider", _MISSING), WorkerProvider, ("provider",), issues)
    capability_digest = _hash_value(
        data.get("capability_digest", _MISSING),
        ("capability_digest",),
        issues,
    )
    launch_profile_digest = _hash_value(
        data.get("launch_profile_digest", _MISSING),
        ("launch_profile_digest",),
        issues,
    )
    policy_digest = _hash_value(data.get("policy_digest", _MISSING), ("policy_digest",), issues)
    scheduler_mode = _enum_value(
        data.get("scheduler_mode", _MISSING),
        SchedulerMode,
        ("scheduler_mode",),
        issues,
    )
    execution_budget = _decode_budget(
        data.get("execution_budget", _MISSING),
        ("execution_budget",),
        issues,
    )
    max_concurrency = _positive_integer(
        data.get("max_concurrency", _MISSING),
        ("max_concurrency",),
        issues,
        maximum=MAX_TASKS,
    )
    lease_ttl_seconds = _bounded_integer(
        data.get("lease_ttl_seconds", _MISSING),
        ("lease_ttl_seconds",),
        issues,
        minimum=MIN_LEASE_TTL_SECONDS,
        maximum=MAX_LEASE_TTL_SECONDS,
    )
    lease_clock_skew_seconds = _bounded_integer(
        data.get("lease_clock_skew_seconds", _MISSING),
        ("lease_clock_skew_seconds",),
        issues,
        minimum=0,
        maximum=MAX_CANONICAL_INTEGER,
    )
    if (
        lease_ttl_seconds is not None
        and lease_clock_skew_seconds is not None
        and lease_clock_skew_seconds * 4 >= lease_ttl_seconds
    ):
        issues.append(
            _issue(
                "value.lease_clock_skew",
                ("lease_clock_skew_seconds",),
                "integer_out_of_range",
                "Expected clock skew to be less than one-quarter of the lease TTL.",
                stage=ValidationStage.LOCAL,
            )
        )
        lease_clock_skew_seconds = None
    path_case_mode = _enum_value(
        data.get("path_case_mode", _MISSING),
        PathCaseMode,
        ("path_case_mode",),
        issues,
    )
    protected_paths = _string_array(
        data.get("protected_paths", _MISSING),
        ("protected_paths",),
        issues,
        nonempty=False,
        unique=True,
        limit=MAX_PATH_LENGTH,
    )

    requirements: list[ManifestRequirement] = []
    requirements_raw = data.get("requirements", _MISSING)
    if requirements_raw is not _MISSING and type(requirements_raw) is not list:
        issues.append(
            _issue(
                "schema.array_type",
                ("requirements",),
                "wrong_container_type",
                "Expected a JSON array.",
            )
        )
    elif type(requirements_raw) is list:
        if not requirements_raw:
            issues.append(
                _issue(
                    "value.nonempty_array",
                    ("requirements",),
                    "empty_collection",
                    "Requirements must not be empty.",
                    stage=ValidationStage.LOCAL,
                )
            )
        if len(requirements_raw) > MAX_COLLECTION_ITEMS:
            issues.append(
                _issue(
                    "value.requirement_limit",
                    ("requirements",),
                    "item_limit_exceeded",
                    f"Requirements exceed {MAX_COLLECTION_ITEMS} entries.",
                    stage=ValidationStage.LOCAL,
                )
            )
        segments = _record_segments(requirements_raw)
        for index, item in enumerate(requirements_raw):
            decoded = _decode_requirement(item, ("requirements", segments[index]), issues)
            if decoded is not None:
                requirements.append(decoded)

    tasks: list[ManifestTask] = []
    tasks_raw = data.get("tasks", _MISSING)
    if tasks_raw is not _MISSING and type(tasks_raw) is not list:
        issues.append(
            _issue(
                "schema.array_type",
                ("tasks",),
                "wrong_container_type",
                "Expected a JSON array.",
            )
        )
    elif type(tasks_raw) is list:
        if not tasks_raw:
            issues.append(
                _issue(
                    "value.nonempty_array",
                    ("tasks",),
                    "empty_collection",
                    "Tasks must not be empty.",
                    stage=ValidationStage.LOCAL,
                )
            )
        if len(tasks_raw) > MAX_TASKS:
            issues.append(
                _issue(
                    "value.task_limit",
                    ("tasks",),
                    "item_limit_exceeded",
                    f"Tasks exceed {MAX_TASKS} entries.",
                    stage=ValidationStage.LOCAL,
                )
            )
        segments = _record_segments(tasks_raw)
        for index, item in enumerate(tasks_raw):
            decoded = _decode_task(item, ("tasks", segments[index]), issues)
            if decoded is not None:
                tasks.append(decoded)

    for kind, records in (("requirement", requirements), ("task", tasks)):
        counts = Counter(item.id for item in records)
        for identifier in sorted(item for item, count in counts.items() if count > 1):
            issues.append(
                _issue(
                    f"value.duplicate_{kind}_id",
                    (kind + "s", identifier, "id"),
                    "duplicate_identifier",
                    f"The {kind} identifier is duplicated.",
                    stage=ValidationStage.LOCAL,
                )
            )

    required_values = (
        schema_version,
        graph_projection_version,
        run_id,
        goal,
        base_branch,
        trellis_parent_task_id,
        trellis_graph_digest,
        task_id_mapping,
        imported_at,
        gate_a,
        gate_b,
        provider,
        capability_digest,
        launch_profile_digest,
        policy_digest,
        scheduler_mode,
        execution_budget,
        max_concurrency,
        lease_ttl_seconds,
        lease_clock_skew_seconds,
        path_case_mode,
        protected_paths,
    )
    if issues or not trellis_revision_present or any(item is None for item in required_values):
        return None, tuple(issues)
    try:
        manifest = ExecutionManifestV2(
            schema_version=schema_version,
            graph_projection_version=graph_projection_version,
            run_id=run_id,
            goal=goal,
            base_branch=base_branch,
            trellis_parent_task_id=trellis_parent_task_id,
            trellis_revision=trellis_revision,
            trellis_graph_digest=trellis_graph_digest,
            task_id_mapping=task_id_mapping,
            imported_at=imported_at,
            approvals=ManifestGateEvidence(gate_a=gate_a, gate_b=gate_b),
            provider=provider,
            capability_digest=capability_digest,
            launch_profile_digest=launch_profile_digest,
            policy_digest=policy_digest,
            scheduler_mode=scheduler_mode,
            execution_budget=execution_budget,
            max_concurrency=max_concurrency,
            lease_ttl_seconds=lease_ttl_seconds,
            lease_clock_skew_seconds=lease_clock_skew_seconds,
            path_case_mode=path_case_mode,
            protected_paths=protected_paths,
            requirements=tuple(requirements),
            tasks=tuple(tasks),
        )
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "value.manifest_v2_contract",
                (),
                "invalid_manifest",
                "Manifest v2 fields are not jointly valid.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None, tuple(issues)
    return manifest, ()


def decode_manifest_v2_primitive(
    value: object,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[ExecutionManifestV2]:
    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    shape_issues = _audit_shape(value, limits)
    if shape_issues:
        return DecodeResult(None, ValidationReport(shape_issues))
    manifest, issues = _decode_manifest_v2_shape(value)
    report = ValidationReport(issues)
    return DecodeResult(manifest if report.ok else None, report)


def decode_manifest_v2_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[ExecutionManifestV2]:
    decoded_json = _decode_json_bytes(raw, limits=limits)
    if not decoded_json.ok:
        return DecodeResult(None, decoded_json.report, decoded_json.source_sha256)
    decoded = decode_manifest_v2_primitive(decoded_json.value.value, limits=limits)
    return DecodeResult(decoded.value, decoded.report, decoded_json.source_sha256)


strict_decode_manifest_v2 = decode_manifest_v2_bytes


__all__ = [
    "decode_manifest_v2_bytes",
    "decode_manifest_v2_primitive",
    "strict_decode_manifest_v2",
]
