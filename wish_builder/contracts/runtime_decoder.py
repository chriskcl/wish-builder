"""Strict raw-byte and primitive decoders for active-M1 runtime contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TypeVar

from .decoder import (
    _MISSING,
    DEFAULT_DECODE_LIMITS,
    DecodeLimits,
    _audit_shape,
    _bounded_integer,
    _closed_object,
    _has_deferred_integer_range,
    _integer,
    _issue,
    _materialize_pairs,
    _NonFiniteNumber,
    _pairs_object,
    _preflight_limits,
    _reject_constant,
    _string,
)
from .diagnostics import (
    DecodeResult,
    DiagnosticPath,
    ReasonCode,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
)
from .manifest_v2 import SchedulerMode
from .runtime import (
    _MESSAGE_KEY_RE,
    _TIMESTAMP_RE,
    _TOKEN_RE,
    JOURNAL_EVENT_VERSION,
    MAX_AFFECTED_IDENTITIES,
    MAX_DECISION_OPTIONS,
    MAX_EVIDENCE_REFS,
    MAX_RUNTIME_ID_LENGTH,
    MAX_RUNTIME_TOKEN_LENGTH,
    RUNTIME_SCHEMA_VERSION,
    Acknowledgement,
    ActorIdentity,
    ActorType,
    AdapterKind,
    BudgetCharge,
    BudgetDimension,
    BudgetDisposition,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionCommand,
    DecisionObservation,
    DecisionObservedPayload,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    DispatchRecoveryPayload,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectReceiptValue,
    EffectRequestPayload,
    EffectStatus,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceSet,
    EvidenceType,
    ExecutionIdentity,
    IdentityObservation,
    JournalEvent,
    JournalEventType,
    JournalPosition,
    LeaseOwner,
    LeasePayload,
    OperationOutcome,
    OutcomeKind,
    OutcomeValue,
    OutcomeValueType,
    RecoveryPayload,
    RetryMetadata,
    RuntimeReasonCode,
    RuntimeState,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from .serialization import MAX_CANONICAL_INTEGER, canonical_json_bytes, canonical_sha256

T = TypeVar("T")
PrimitiveDecoder = Callable[[object, DecodeLimits], DecodeResult[T]]


def _enum_value(
    value: object,
    enum_type: type[T],
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> T | None:
    """Decode a closed enum without allowing a large enum to overflow diagnostics."""

    if value is _MISSING:
        return None
    if type(value) is not str:
        issues.append(
            _issue(
                "schema.enum_type",
                path,
                ReasonCode.WRONG_PRIMITIVE_TYPE,
                "Expected a string enum value.",
            )
        )
        return None
    try:
        return enum_type(value)
    except ValueError:
        allowed = sorted(item.value for item in enum_type)  # type: ignore[attr-defined]
        rendered = "Expected one of: " + ", ".join(allowed) + "."
        if len(rendered) > 480:
            rendered = (
                f"Expected one of the {len(allowed)} closed "
                f"{enum_type.__name__} values."
            )
        issues.append(
            _issue(
                "schema.enum_value",
                path,
                ReasonCode.UNKNOWN_ENUM_VALUE,
                rendered,
            )
        )
        return None


def _invalid_contract(path: DiagnosticPath, name: str) -> ValidationIssue:
    return _issue(
        "value.runtime_contract",
        path,
        ReasonCode.INVALID_MANIFEST,
        f"{name} fields are not jointly valid.",
        stage=ValidationStage.LOCAL,
    )


def _stable_id_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    return _string(
        value,
        path,
        issues,
        limit=MAX_RUNTIME_ID_LENGTH,
        stable_id=True,
    )


def _token_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    result = _string(value, path, issues, limit=MAX_RUNTIME_TOKEN_LENGTH)
    if result is not None and not _TOKEN_RE.fullmatch(result):
        issues.append(
            _issue(
                "value.runtime_token",
                path,
                ReasonCode.INVALID_IDENTIFIER,
                "Expected a stable runtime token.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return result


def _message_key_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    result = _string(value, path, issues, limit=MAX_RUNTIME_TOKEN_LENGTH)
    if result is not None and not _MESSAGE_KEY_RE.fullmatch(result):
        issues.append(
            _issue(
                "value.message_key",
                path,
                ReasonCode.INVALID_IDENTIFIER,
                "Expected a lowercase stable message key.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return result


def _hash_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    result = _string(value, path, issues, limit=71)
    if result is not None:
        from .models import HASH_RE

        if not HASH_RE.fullmatch(result):
            issues.append(
                _issue(
                    "value.sha256_reference",
                    path,
                    ReasonCode.INVALID_HASH,
                    "Expected sha256 followed by 64 lowercase hexadecimal digits.",
                    stage=ValidationStage.LOCAL,
                )
            )
            return None
    return result


def _optional_hash_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    return None if value is None else _hash_value(value, path, issues)


def _timestamp_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> str | None:
    result = _string(value, path, issues, limit=32)
    valid = False
    if result is not None and _TIMESTAMP_RE.fullmatch(result):
        from datetime import datetime

        try:
            datetime.fromisoformat(result[:-1])
            valid = True
        except ValueError:
            pass
    if result is not None and not valid:
        issues.append(
            _issue(
                "value.utc_timestamp",
                path,
                ReasonCode.INVALID_TIMESTAMP,
                "Expected a valid UTC timestamp ending in Z.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return result


def _bounded_integer_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    positive: bool,
) -> int | None:
    result = _integer(value, path, issues)
    lower = 1 if positive else 0
    if result is not None and not lower <= result <= MAX_CANONICAL_INTEGER:
        issues.append(
            _issue(
                "value.positive_integer" if positive else "value.nonnegative_integer",
                path,
                ReasonCode.INTEGER_OUT_OF_RANGE,
                (
                    "Expected a positive signed 64-bit integer."
                    if positive
                    else "Expected a non-negative signed 64-bit integer."
                ),
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return result


def _schema_version_value(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> int | None:
    result = _integer(value, path, issues)
    if result is not None and result != RUNTIME_SCHEMA_VERSION:
        issues.append(
            _issue(
                "schema.runtime_version",
                path,
                ReasonCode.UNSUPPORTED_SCHEMA_VERSION,
                f"Only runtime schema version {RUNTIME_SCHEMA_VERSION} is supported.",
            )
        )
        return None
    return result


def _nullable_enum(
    value: object,
    enum_type: type[T],
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> T | None:
    return None if value is None else _enum_value(value, enum_type, path, issues)


def _record_segment(value: object, index: int) -> str | int:
    if type(value) is dict:
        for key in (
            "digest",
            "event_id",
            "decision_id",
            "correlation_id",
            "identifier",
        ):
            candidate = value.get(key)
            if type(candidate) is str and candidate:
                return candidate[:48]
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError):
        return index
    return "@" + hashlib.sha256(encoded).hexdigest()[:24]


def _decode_execution_identity_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> ExecutionIdentity | None:
    obj = _closed_object(
        value,
        path=path,
        allowed={"run_id", "coordinator_epoch", "task_id", "attempt", "correlation_id"},
        required={
            "run_id",
            "coordinator_epoch",
            "task_id",
            "attempt",
            "correlation_id",
        },
        issues=issues,
    )
    if obj is None:
        return None
    start = len(issues)
    run_id = _stable_id_value(obj.get("run_id", _MISSING), path + ("run_id",), issues)
    epoch = _bounded_integer_value(
        obj.get("coordinator_epoch", _MISSING),
        path + ("coordinator_epoch",),
        issues,
        positive=False,
    )
    task_raw = obj.get("task_id", _MISSING)
    task_id = (
        None
        if task_raw is None
        else _stable_id_value(task_raw, path + ("task_id",), issues)
    )
    attempt_raw = obj.get("attempt", _MISSING)
    attempt = (
        None
        if attempt_raw is None
        else _bounded_integer_value(
            attempt_raw, path + ("attempt",), issues, positive=True
        )
    )
    correlation_raw = obj.get("correlation_id", _MISSING)
    correlation_id = (
        None
        if correlation_raw is None
        else _stable_id_value(correlation_raw, path + ("correlation_id",), issues)
    )
    if len(issues) != start:
        return None
    try:
        return ExecutionIdentity(run_id, epoch, task_id, attempt, correlation_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Execution identity"))
        return None


def _decode_actor_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> ActorIdentity | None:
    obj = _closed_object(
        value,
        path=path,
        allowed={"actor_type", "actor_id", "host_id", "process_id", "process_start_id"},
        required={
            "actor_type",
            "actor_id",
            "host_id",
            "process_id",
            "process_start_id",
        },
        issues=issues,
    )
    if obj is None:
        return None
    start = len(issues)
    actor_type = _enum_value(
        obj.get("actor_type", _MISSING), ActorType, path + ("actor_type",), issues
    )
    actor_id = _token_value(obj.get("actor_id", _MISSING), path + ("actor_id",), issues)
    host_id = _token_value(obj.get("host_id", _MISSING), path + ("host_id",), issues)
    process_id = _bounded_integer_value(
        obj.get("process_id", _MISSING), path + ("process_id",), issues, positive=True
    )
    process_start_id = _token_value(
        obj.get("process_start_id", _MISSING), path + ("process_start_id",), issues
    )
    if len(issues) != start:
        return None
    try:
        return ActorIdentity(
            actor_type, actor_id, host_id, process_id, process_start_id
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Actor identity"))
        return None


def _decode_command_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> CommandIdentity | None:
    fields = {
        "schema_version",
        "command_id",
        "request_id",
        "kind",
        "expected_sequence",
        "request_nonce",
        "actor",
        "source_channel",
        "submitted_at",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    schema_version = _schema_version_value(
        obj.get("schema_version", _MISSING), path + ("schema_version",), issues
    )
    command_id = _stable_id_value(
        obj.get("command_id", _MISSING), path + ("command_id",), issues
    )
    request_id = _stable_id_value(
        obj.get("request_id", _MISSING), path + ("request_id",), issues
    )
    kind = _enum_value(obj.get("kind", _MISSING), CommandKind, path + ("kind",), issues)
    expected_sequence = _bounded_integer_value(
        obj.get("expected_sequence", _MISSING),
        path + ("expected_sequence",),
        issues,
        positive=False,
    )
    request_nonce = _token_value(
        obj.get("request_nonce", _MISSING), path + ("request_nonce",), issues
    )
    actor = _decode_actor_shape(obj.get("actor", _MISSING), path + ("actor",), issues)
    source_channel = _enum_value(
        obj.get("source_channel", _MISSING),
        SourceChannel,
        path + ("source_channel",),
        issues,
    )
    submitted_at = _timestamp_value(
        obj.get("submitted_at", _MISSING), path + ("submitted_at",), issues
    )
    if len(issues) != start:
        return None
    try:
        return CommandIdentity(
            schema_version,
            command_id,
            request_id,
            kind,
            expected_sequence,
            request_nonce,
            actor,
            source_channel,
            submitted_at,
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Command identity"))
        return None


def _decode_decision_request_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> DecisionRequest | None:
    fields = {
        "command",
        "decision_type",
        "candidate_hash",
        "workspace_hash",
        "expected_actor_id",
        "options",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    command = _decode_command_shape(
        obj.get("command", _MISSING), path + ("command",), issues
    )
    decision_type = _enum_value(
        obj.get("decision_type", _MISSING),
        DecisionType,
        path + ("decision_type",),
        issues,
    )
    candidate_hash = _hash_value(
        obj.get("candidate_hash", _MISSING), path + ("candidate_hash",), issues
    )
    workspace_hash = _hash_value(
        obj.get("workspace_hash", _MISSING), path + ("workspace_hash",), issues
    )
    expected_actor_id = _token_value(
        obj.get("expected_actor_id", _MISSING), path + ("expected_actor_id",), issues
    )
    options_raw = obj.get("options", _MISSING)
    options: list[DecisionChoice] = []
    if type(options_raw) is not list:
        if options_raw is not _MISSING:
            issues.append(
                _issue(
                    "schema.array_type",
                    path + ("options",),
                    ReasonCode.WRONG_CONTAINER_TYPE,
                    "Expected a JSON array.",
                )
            )
    else:
        if not options_raw:
            issues.append(
                _issue(
                    "value.nonempty_array",
                    path + ("options",),
                    ReasonCode.EMPTY_COLLECTION,
                    "The array must not be empty.",
                    stage=ValidationStage.LOCAL,
                )
            )
        if len(options_raw) > MAX_DECISION_OPTIONS:
            issues.append(
                _issue(
                    "value.collection_limit",
                    path + ("options",),
                    ReasonCode.ITEM_LIMIT_EXCEEDED,
                    f"The array exceeds {MAX_DECISION_OPTIONS} entries.",
                    stage=ValidationStage.LOCAL,
                )
            )
        for index, item in enumerate(options_raw):
            decoded = _enum_value(
                item, DecisionChoice, path + ("options", index), issues
            )
            if decoded is not None:
                options.append(decoded)
        if len(set(options)) != len(options):
            issues.append(
                _issue(
                    "value.duplicate_array_item",
                    path + ("options",),
                    ReasonCode.DUPLICATE_ITEM,
                    "The array must not contain duplicate values.",
                    stage=ValidationStage.LOCAL,
                )
            )
    if len(issues) != start:
        return None
    try:
        return DecisionRequest(
            command,
            decision_type,
            candidate_hash,
            workspace_hash,
            expected_actor_id,
            tuple(options),
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Decision request"))
        return None


def _decode_decision_command_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> DecisionCommand | None:
    fields = {
        "decision_id",
        "request",
        "choice",
        "actor",
        "source_channel",
        "decided_at",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    decision_id = _stable_id_value(
        obj.get("decision_id", _MISSING), path + ("decision_id",), issues
    )
    request = _decode_decision_request_shape(
        obj.get("request", _MISSING), path + ("request",), issues
    )
    choice = _enum_value(
        obj.get("choice", _MISSING), DecisionChoice, path + ("choice",), issues
    )
    actor = _decode_actor_shape(obj.get("actor", _MISSING), path + ("actor",), issues)
    source_channel = _enum_value(
        obj.get("source_channel", _MISSING),
        SourceChannel,
        path + ("source_channel",),
        issues,
    )
    decided_at = _timestamp_value(
        obj.get("decided_at", _MISSING), path + ("decided_at",), issues
    )
    if len(issues) != start:
        return None
    try:
        return DecisionCommand(
            decision_id, request, choice, actor, source_channel, decided_at
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Decision command"))
        return None


def _decode_decision_observation_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> DecisionObservation | None:
    fields = {"decision", "event_sequence", "submission_hash"}
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    decision = _decode_decision_command_shape(
        obj.get("decision", _MISSING), path + ("decision",), issues
    )
    event_sequence = _bounded_integer_value(
        obj.get("event_sequence", _MISSING),
        path + ("event_sequence",),
        issues,
        positive=True,
    )
    submission_hash = _hash_value(
        obj.get("submission_hash", _MISSING), path + ("submission_hash",), issues
    )
    if len(issues) != start:
        return None
    try:
        return DecisionObservation(decision, event_sequence, submission_hash)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Decision observation"))
        return None


def _decode_evidence_producer_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> EvidenceProducer | None:
    fields = {"identity", "event_id", "external_object_id"}
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    identity = _decode_execution_identity_shape(
        obj.get("identity", _MISSING), path + ("identity",), issues
    )
    event_raw = obj.get("event_id", _MISSING)
    event_id = (
        None
        if event_raw is None
        else _stable_id_value(event_raw, path + ("event_id",), issues)
    )
    external_raw = obj.get("external_object_id", _MISSING)
    external_object_id = (
        None
        if external_raw is None
        else _token_value(external_raw, path + ("external_object_id",), issues)
    )
    if len(issues) != start:
        return None
    try:
        return EvidenceProducer(identity, event_id, external_object_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Evidence producer"))
        return None


def _decode_evidence_ref_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> EvidenceRef | None:
    fields = {
        "schema_version",
        "digest",
        "byte_length",
        "evidence_type",
        "producer",
        "created_at",
        "sensitivity",
        "render_policy",
        "role",
        "structured_subject_hash",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    schema_version = _schema_version_value(
        obj.get("schema_version", _MISSING), path + ("schema_version",), issues
    )
    digest = _hash_value(obj.get("digest", _MISSING), path + ("digest",), issues)
    byte_length = _bounded_integer_value(
        obj.get("byte_length", _MISSING),
        path + ("byte_length",),
        issues,
        positive=False,
    )
    evidence_type = _enum_value(
        obj.get("evidence_type", _MISSING),
        EvidenceType,
        path + ("evidence_type",),
        issues,
    )
    producer = _decode_evidence_producer_shape(
        obj.get("producer", _MISSING), path + ("producer",), issues
    )
    created_at = _timestamp_value(
        obj.get("created_at", _MISSING), path + ("created_at",), issues
    )
    sensitivity = _enum_value(
        obj.get("sensitivity", _MISSING),
        EvidenceSensitivity,
        path + ("sensitivity",),
        issues,
    )
    render_policy = _enum_value(
        obj.get("render_policy", _MISSING),
        EvidenceRenderPolicy,
        path + ("render_policy",),
        issues,
    )
    role = _enum_value(
        obj.get("role", _MISSING), EvidenceRole, path + ("role",), issues
    )
    structured_raw = obj.get("structured_subject_hash", _MISSING)
    structured_hash = (
        None
        if structured_raw is None
        else _hash_value(structured_raw, path + ("structured_subject_hash",), issues)
    )
    if len(issues) != start:
        return None
    try:
        return EvidenceRef(
            schema_version,
            digest,
            byte_length,
            evidence_type,
            producer,
            created_at,
            sensitivity,
            render_policy,
            role,
            structured_hash,
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Evidence reference"))
        return None


def _decode_evidence_list(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    nonempty: bool = False,
) -> tuple[EvidenceRef, ...] | None:
    if type(value) is not list:
        issues.append(
            _issue(
                "schema.array_type",
                path,
                ReasonCode.WRONG_CONTAINER_TYPE,
                "Expected a JSON array.",
            )
        )
        return None
    if nonempty and not value:
        issues.append(
            _issue(
                "value.nonempty_array",
                path,
                ReasonCode.EMPTY_COLLECTION,
                "The array must not be empty.",
                stage=ValidationStage.LOCAL,
            )
        )
    if len(value) > MAX_EVIDENCE_REFS:
        issues.append(
            _issue(
                "value.collection_limit",
                path,
                ReasonCode.ITEM_LIMIT_EXCEEDED,
                f"The array exceeds {MAX_EVIDENCE_REFS} entries.",
                stage=ValidationStage.LOCAL,
            )
        )
    result: list[EvidenceRef] = []
    start = len(issues)
    for index, item in enumerate(value):
        decoded = _decode_evidence_ref_shape(
            item, path + (_record_segment(item, index),), issues
        )
        if decoded is not None:
            result.append(decoded)
    if len({item.digest for item in result}) != len(result):
        issues.append(
            _issue(
                "value.duplicate_evidence_digest",
                path,
                ReasonCode.DUPLICATE_ITEM,
                "Evidence references must not repeat a digest.",
                stage=ValidationStage.LOCAL,
            )
        )
    if len(issues) != start or (nonempty and not result):
        return None
    return tuple(result)


def _decode_effect_receipt_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> EffectReceipt | None:
    fields = {
        "schema_version",
        "identity",
        "operation",
        "status",
        "observed_at",
        "effect_hash",
        "external_object_id",
        "evidence",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    schema_version = _schema_version_value(
        obj.get("schema_version", _MISSING), path + ("schema_version",), issues
    )
    identity = _decode_execution_identity_shape(
        obj.get("identity", _MISSING), path + ("identity",), issues
    )
    operation = _enum_value(
        obj.get("operation", _MISSING), EffectOperation, path + ("operation",), issues
    )
    status = _enum_value(
        obj.get("status", _MISSING), EffectStatus, path + ("status",), issues
    )
    observed_at = _timestamp_value(
        obj.get("observed_at", _MISSING), path + ("observed_at",), issues
    )
    effect_hash = _optional_hash_value(
        obj.get("effect_hash", _MISSING), path + ("effect_hash",), issues
    )
    external_raw = obj.get("external_object_id", _MISSING)
    external_object_id = (
        None
        if external_raw is None
        else _token_value(external_raw, path + ("external_object_id",), issues)
    )
    evidence = _decode_evidence_list(
        obj.get("evidence", _MISSING), path + ("evidence",), issues
    )
    if len(issues) != start:
        return None
    try:
        return EffectReceipt(
            schema_version,
            identity,
            operation,
            status,
            observed_at,
            effect_hash,
            external_object_id,
            evidence,
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Effect receipt"))
        return None


def _decode_retry_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> RetryMetadata | None:
    fields = {"attempt", "ceiling", "retry_at"}
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    attempt = _bounded_integer_value(
        obj.get("attempt", _MISSING), path + ("attempt",), issues, positive=True
    )
    ceiling = _bounded_integer_value(
        obj.get("ceiling", _MISSING), path + ("ceiling",), issues, positive=True
    )
    retry_at = _timestamp_value(
        obj.get("retry_at", _MISSING), path + ("retry_at",), issues
    )
    if len(issues) != start:
        return None
    try:
        return RetryMetadata(attempt, ceiling, retry_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Retry metadata"))
        return None


def _decode_budget_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> BudgetCharge | None:
    fields = {"dimension", "amount", "disposition"}
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    dimension = _enum_value(
        obj.get("dimension", _MISSING), BudgetDimension, path + ("dimension",), issues
    )
    amount = _bounded_integer_value(
        obj.get("amount", _MISSING), path + ("amount",), issues, positive=False
    )
    disposition = _enum_value(
        obj.get("disposition", _MISSING),
        BudgetDisposition,
        path + ("disposition",),
        issues,
    )
    if len(issues) != start:
        return None
    try:
        return BudgetCharge(dimension, amount, disposition)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Budget charge"))
        return None


def _decode_outcome_value_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> OutcomeValue | None:
    if type(value) is not dict:
        issues.append(
            _issue(
                "schema.object_type",
                path,
                ReasonCode.WRONG_CONTAINER_TYPE,
                "Expected a JSON object.",
            )
        )
        return None
    if "type" not in value:
        issues.append(
            _issue(
                "schema.required_field",
                path + ("type",),
                ReasonCode.MISSING_FIELD,
                "A required field is missing.",
            )
        )
        return None
    value_type = _enum_value(
        value.get("type", _MISSING), OutcomeValueType, path + ("type",), issues
    )
    if value_type is None:
        return None
    if value_type is OutcomeValueType.ACKNOWLEDGEMENT:
        obj = _closed_object(
            value, path=path, allowed={"type"}, required={"type"}, issues=issues
        )
        return None if obj is None else Acknowledgement()
    if value_type is OutcomeValueType.IDENTITY:
        fields = {"type", "identifier"}
        obj = _closed_object(
            value, path=path, allowed=fields, required=fields, issues=issues
        )
        if obj is None:
            return None
        identifier = _token_value(
            obj.get("identifier", _MISSING), path + ("identifier",), issues
        )
        if identifier is None:
            return None
        return IdentityObservation(identifier)
    if value_type is OutcomeValueType.JOURNAL_POSITION:
        fields = {"type", "sequence", "event_id", "event_hash"}
        obj = _closed_object(
            value, path=path, allowed=fields, required=fields, issues=issues
        )
        if obj is None:
            return None
        start = len(issues)
        sequence = _bounded_integer_value(
            obj.get("sequence", _MISSING), path + ("sequence",), issues, positive=True
        )
        event_id = _stable_id_value(
            obj.get("event_id", _MISSING), path + ("event_id",), issues
        )
        event_hash = _hash_value(
            obj.get("event_hash", _MISSING), path + ("event_hash",), issues
        )
        if len(issues) != start:
            return None
        return JournalPosition(sequence, event_id, event_hash)  # type: ignore[arg-type]
    if value_type is OutcomeValueType.EFFECT_RECEIPT:
        fields = {"type", "receipt"}
        obj = _closed_object(
            value, path=path, allowed=fields, required=fields, issues=issues
        )
        if obj is None:
            return None
        receipt = _decode_effect_receipt_shape(
            obj.get("receipt", _MISSING), path + ("receipt",), issues
        )
        return None if receipt is None else EffectReceiptValue(receipt)
    fields = {"type", "evidence"}
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    evidence = _decode_evidence_list(
        obj.get("evidence", _MISSING), path + ("evidence",), issues, nonempty=True
    )
    return None if evidence is None else EvidenceSet(evidence)


def _decode_outcome_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> OperationOutcome | None:
    fields = {
        "schema_version",
        "kind",
        "value",
        "reason_code",
        "evidence",
        "retry",
        "budget_charge",
        "user_message_key",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    schema_version = _schema_version_value(
        obj.get("schema_version", _MISSING), path + ("schema_version",), issues
    )
    kind = _enum_value(obj.get("kind", _MISSING), OutcomeKind, path + ("kind",), issues)
    value_raw = obj.get("value", _MISSING)
    outcome_value = (
        None
        if value_raw is None
        else _decode_outcome_value_shape(value_raw, path + ("value",), issues)
    )
    reason_code = _nullable_enum(
        obj.get("reason_code", _MISSING),
        RuntimeReasonCode,
        path + ("reason_code",),
        issues,
    )
    evidence = _decode_evidence_list(
        obj.get("evidence", _MISSING), path + ("evidence",), issues
    )
    retry_raw = obj.get("retry", _MISSING)
    retry = (
        None
        if retry_raw is None
        else _decode_retry_shape(retry_raw, path + ("retry",), issues)
    )
    budget_raw = obj.get("budget_charge", _MISSING)
    budget = (
        None
        if budget_raw is None
        else _decode_budget_shape(budget_raw, path + ("budget_charge",), issues)
    )
    message_raw = obj.get("user_message_key", _MISSING)
    user_message_key = (
        None
        if message_raw is None
        else _message_key_value(message_raw, path + ("user_message_key",), issues)
    )
    if len(issues) != start:
        return None
    try:
        return OperationOutcome(
            schema_version,
            kind,
            outcome_value,
            reason_code,
            evidence,
            retry,
            budget,
            user_message_key,
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Operation outcome"))
        return None


def _decode_transition_payload(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> TransitionPayload | None:
    fields = {"payload_type", "subject", "from_state", "to_state", "evidence"}
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    subject = _enum_value(
        obj.get("subject", _MISSING), TransitionSubject, path + ("subject",), issues
    )
    from_state = _enum_value(
        obj.get("from_state", _MISSING), RuntimeState, path + ("from_state",), issues
    )
    to_state = _enum_value(
        obj.get("to_state", _MISSING), RuntimeState, path + ("to_state",), issues
    )
    evidence = _decode_evidence_list(
        obj.get("evidence", _MISSING), path + ("evidence",), issues
    )
    if len(issues) != start:
        return None
    try:
        return TransitionPayload(subject, from_state, to_state, evidence)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Transition payload"))
        return None


def _decode_effect_request_payload(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> EffectRequestPayload | None:
    fields = {
        "payload_type",
        "operation",
        "adapter",
        "object_type",
        "normalized_target_hash",
        "request_payload_hash",
        "expected_sequence",
        "fencing_token",
        "base_hash",
        "head_hash",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    operation = _enum_value(
        obj.get("operation", _MISSING), EffectOperation, path + ("operation",), issues
    )
    adapter = _enum_value(
        obj.get("adapter", _MISSING), AdapterKind, path + ("adapter",), issues
    )
    object_type = _enum_value(
        obj.get("object_type", _MISSING),
        EffectObjectType,
        path + ("object_type",),
        issues,
    )
    target_hash = _hash_value(
        obj.get("normalized_target_hash", _MISSING),
        path + ("normalized_target_hash",),
        issues,
    )
    request_hash = _hash_value(
        obj.get("request_payload_hash", _MISSING),
        path + ("request_payload_hash",),
        issues,
    )
    expected_sequence = _bounded_integer_value(
        obj.get("expected_sequence", _MISSING),
        path + ("expected_sequence",),
        issues,
        positive=False,
    )
    fencing_token = _bounded_integer_value(
        obj.get("fencing_token", _MISSING),
        path + ("fencing_token",),
        issues,
        positive=True,
    )
    base_hash = _optional_hash_value(
        obj.get("base_hash", _MISSING), path + ("base_hash",), issues
    )
    head_hash = _optional_hash_value(
        obj.get("head_hash", _MISSING), path + ("head_hash",), issues
    )
    if len(issues) != start:
        return None
    try:
        return EffectRequestPayload(
            operation,
            adapter,
            object_type,
            target_hash,
            request_hash,
            expected_sequence,
            fencing_token,
            base_hash,
            head_hash,
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Effect request payload"))
        return None


def _decode_effect_observation_payload(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> EffectObservationPayload | None:
    fields = {"payload_type", "adapter", "receipt"}
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    adapter = _enum_value(
        obj.get("adapter", _MISSING), AdapterKind, path + ("adapter",), issues
    )
    receipt = _decode_effect_receipt_shape(
        obj.get("receipt", _MISSING), path + ("receipt",), issues
    )
    if len(issues) != start:
        return None
    try:
        return EffectObservationPayload(adapter, receipt)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Effect observation payload"))
        return None


def _decode_recovery_payload(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> RecoveryPayload | None:
    fields = {
        "payload_type",
        "last_valid_sequence",
        "last_valid_event_hash",
        "affected_identities",
        "evidence",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    last_sequence = _bounded_integer_value(
        obj.get("last_valid_sequence", _MISSING),
        path + ("last_valid_sequence",),
        issues,
        positive=False,
    )
    last_hash = _hash_value(
        obj.get("last_valid_event_hash", _MISSING),
        path + ("last_valid_event_hash",),
        issues,
    )
    affected_raw = obj.get("affected_identities", _MISSING)
    affected: list[str] = []
    if type(affected_raw) is not list:
        if affected_raw is not _MISSING:
            issues.append(
                _issue(
                    "schema.array_type",
                    path + ("affected_identities",),
                    ReasonCode.WRONG_CONTAINER_TYPE,
                    "Expected a JSON array.",
                )
            )
    else:
        if len(affected_raw) > MAX_AFFECTED_IDENTITIES:
            issues.append(
                _issue(
                    "value.collection_limit",
                    path + ("affected_identities",),
                    ReasonCode.ITEM_LIMIT_EXCEEDED,
                    f"The array exceeds {MAX_AFFECTED_IDENTITIES} entries.",
                    stage=ValidationStage.LOCAL,
                )
            )
        for index, item in enumerate(affected_raw):
            decoded = _stable_id_value(
                item,
                path + ("affected_identities", _record_segment(item, index)),
                issues,
            )
            if decoded is not None:
                affected.append(decoded)
        if len(set(affected)) != len(affected):
            issues.append(
                _issue(
                    "value.duplicate_array_item",
                    path + ("affected_identities",),
                    ReasonCode.DUPLICATE_ITEM,
                    "The array must not contain duplicate values.",
                    stage=ValidationStage.LOCAL,
                )
            )
    evidence = _decode_evidence_list(
        obj.get("evidence", _MISSING), path + ("evidence",), issues
    )
    if len(issues) != start:
        return None
    try:
        return RecoveryPayload(last_sequence, last_hash, tuple(affected), evidence)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Recovery payload"))
        return None


def _decode_dispatch_recovery_payload(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> DispatchRecoveryPayload | None:
    fields = {
        "payload_type",
        "recovery_id",
        "command",
        "subject_identity",
        "request_event_id",
        "request_sequence",
        "request_event_hash",
        "receipt",
        "process_tree_termination_proven",
        "last_valid_sequence",
        "last_valid_event_hash",
        "evidence",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    recovery_id = _stable_id_value(
        obj.get("recovery_id", _MISSING), path + ("recovery_id",), issues
    )
    command = _decode_command_shape(
        obj.get("command", _MISSING), path + ("command",), issues
    )
    subject_identity = _decode_execution_identity_shape(
        obj.get("subject_identity", _MISSING),
        path + ("subject_identity",),
        issues,
    )
    request_event_id = _stable_id_value(
        obj.get("request_event_id", _MISSING),
        path + ("request_event_id",),
        issues,
    )
    request_sequence = _bounded_integer_value(
        obj.get("request_sequence", _MISSING),
        path + ("request_sequence",),
        issues,
        positive=True,
    )
    request_event_hash = _hash_value(
        obj.get("request_event_hash", _MISSING),
        path + ("request_event_hash",),
        issues,
    )
    receipt = _decode_effect_receipt_shape(
        obj.get("receipt", _MISSING), path + ("receipt",), issues
    )
    termination = obj.get("process_tree_termination_proven", _MISSING)
    if termination is _MISSING:
        issues.append(
            _issue(
                "schema.required_field",
                path + ("process_tree_termination_proven",),
                ReasonCode.MISSING_FIELD,
                "A required field is missing.",
            )
        )
    elif type(termination) is not bool:
        issues.append(
            _issue(
                "schema.boolean_type",
                path + ("process_tree_termination_proven",),
                ReasonCode.WRONG_PRIMITIVE_TYPE,
                "Expected a boolean value.",
            )
        )
    last_valid_sequence = _bounded_integer_value(
        obj.get("last_valid_sequence", _MISSING),
        path + ("last_valid_sequence",),
        issues,
        positive=False,
    )
    last_valid_event_hash = _hash_value(
        obj.get("last_valid_event_hash", _MISSING),
        path + ("last_valid_event_hash",),
        issues,
    )
    evidence = _decode_evidence_list(
        obj.get("evidence", _MISSING), path + ("evidence",), issues
    )
    if len(issues) != start:
        return None
    try:
        return DispatchRecoveryPayload(
            recovery_id,
            command,
            subject_identity,
            request_event_id,
            request_sequence,
            request_event_hash,
            receipt,
            termination,
            last_valid_sequence,
            last_valid_event_hash,
            evidence,
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Dispatch recovery payload"))
        return None


def _decode_lease_payload(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> LeasePayload | None:
    fields = {
        "payload_type",
        "lease_id",
        "coordinator_id",
        "owner",
        "scheduler_mode",
        "fencing_token",
        "manifest_digest",
        "lease_ttl_seconds",
        "lease_clock_skew_seconds",
        "committed_at",
        "expires_at",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    lease_id = _stable_id_value(
        obj.get("lease_id", _MISSING), path + ("lease_id",), issues
    )
    coordinator_id = _token_value(
        obj.get("coordinator_id", _MISSING),
        path + ("coordinator_id",),
        issues,
    )
    owner_value = obj.get("owner", _MISSING)
    owner: LeaseOwner | None = None
    owner_obj = _closed_object(
        owner_value,
        path=path + ("owner",),
        allowed={
            "actor",
            "control_root_id",
            "local_repository_id",
            "local_worktree_id",
            "workspace_hash",
        },
        required={
            "actor",
            "control_root_id",
            "local_repository_id",
            "local_worktree_id",
            "workspace_hash",
        },
        issues=issues,
    )
    if owner_obj is not None:
        owner_start = len(issues)
        owner_actor = _decode_actor_shape(
            owner_obj.get("actor", _MISSING),
            path + ("owner", "actor"),
            issues,
        )
        control_root_id = _hash_value(
            owner_obj.get("control_root_id", _MISSING),
            path + ("owner", "control_root_id"),
            issues,
        )
        local_repository_id = _hash_value(
            owner_obj.get("local_repository_id", _MISSING),
            path + ("owner", "local_repository_id"),
            issues,
        )
        local_worktree_id = _hash_value(
            owner_obj.get("local_worktree_id", _MISSING),
            path + ("owner", "local_worktree_id"),
            issues,
        )
        workspace_hash = _hash_value(
            owner_obj.get("workspace_hash", _MISSING),
            path + ("owner", "workspace_hash"),
            issues,
        )
        if len(issues) == owner_start:
            try:
                owner = LeaseOwner(
                    owner_actor,
                    local_repository_id,
                    local_worktree_id,
                    workspace_hash,
                    control_root_id,
                )  # type: ignore[arg-type]
            except (TypeError, ValueError):
                issues.append(_invalid_contract(path + ("owner",), "Lease owner"))
    scheduler_mode = _enum_value(
        obj.get("scheduler_mode", _MISSING),
        SchedulerMode,
        path + ("scheduler_mode",),
        issues,
    )
    fencing_token = _bounded_integer_value(
        obj.get("fencing_token", _MISSING),
        path + ("fencing_token",),
        issues,
        positive=True,
    )
    manifest_digest = _hash_value(
        obj.get("manifest_digest", _MISSING),
        path + ("manifest_digest",),
        issues,
    )
    ttl = _bounded_integer_value(
        obj.get("lease_ttl_seconds", _MISSING),
        path + ("lease_ttl_seconds",),
        issues,
        positive=True,
    )
    skew = _bounded_integer_value(
        obj.get("lease_clock_skew_seconds", _MISSING),
        path + ("lease_clock_skew_seconds",),
        issues,
        positive=False,
    )
    committed_at = _timestamp_value(
        obj.get("committed_at", _MISSING),
        path + ("committed_at",),
        issues,
    )
    expires_at = _timestamp_value(
        obj.get("expires_at", _MISSING),
        path + ("expires_at",),
        issues,
    )
    if len(issues) != start:
        return None
    try:
        return LeasePayload(
            lease_id=lease_id,
            coordinator_id=coordinator_id,
            owner=owner,
            scheduler_mode=scheduler_mode,
            fencing_token=fencing_token,
            manifest_digest=manifest_digest,
            lease_ttl_seconds=ttl,
            lease_clock_skew_seconds=skew,
            committed_at=committed_at,
            expires_at=expires_at,
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Lease payload"))
        return None


def _decode_payload_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> object | None:
    if type(value) is not dict:
        issues.append(
            _issue(
                "schema.object_type",
                path,
                ReasonCode.WRONG_CONTAINER_TYPE,
                "Expected a JSON object.",
            )
        )
        return None
    if "payload_type" not in value:
        issues.append(
            _issue(
                "schema.required_field",
                path + ("payload_type",),
                ReasonCode.MISSING_FIELD,
                "A required field is missing.",
            )
        )
        return None
    payload_type = value.get("payload_type", _MISSING)
    if type(payload_type) is not str:
        _string(payload_type, path + ("payload_type",), issues, limit=32)
        return None
    decoders = {
        "transition": _decode_transition_payload,
        "effect_request": _decode_effect_request_payload,
        "effect_observation": _decode_effect_observation_payload,
        "lease": _decode_lease_payload,
        "recovery": _decode_recovery_payload,
        "dispatch_recovery": _decode_dispatch_recovery_payload,
    }
    if payload_type == "decision_request":
        fields = {"payload_type", "request"}
        obj = _closed_object(
            value, path=path, allowed=fields, required=fields, issues=issues
        )
        if obj is None:
            return None
        request = _decode_decision_request_shape(
            obj.get("request", _MISSING), path + ("request",), issues
        )
        return None if request is None else DecisionRequestPayload(request)
    if payload_type == "decision_observed":
        fields = {"payload_type", "observation"}
        obj = _closed_object(
            value, path=path, allowed=fields, required=fields, issues=issues
        )
        if obj is None:
            return None
        observation = _decode_decision_observation_shape(
            obj.get("observation", _MISSING), path + ("observation",), issues
        )
        return None if observation is None else DecisionObservedPayload(observation)
    decoder = decoders.get(payload_type)
    if decoder is None:
        issues.append(
            _issue(
                "schema.enum_value",
                path + ("payload_type",),
                ReasonCode.UNKNOWN_ENUM_VALUE,
                "Expected one of: decision_observed, decision_request, dispatch_recovery, effect_observation, effect_request, lease, recovery, transition.",
            )
        )
        return None
    return decoder(value, path, issues)


def _decode_journal_event_shape(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> JournalEvent | None:
    fields = {
        "event_version",
        "sequence",
        "event_id",
        "event_type",
        "run_id",
        "task_id",
        "attempt",
        "coordinator_epoch",
        "correlation_id",
        "actor_type",
        "actor_id",
        "recorded_at",
        "reason_code",
        "previous_event_hash",
        "payload_hash",
        "payload",
        "event_hash",
    }
    obj = _closed_object(
        value, path=path, allowed=fields, required=fields, issues=issues
    )
    if obj is None:
        return None
    start = len(issues)
    event_version = _string(
        obj.get("event_version", _MISSING), path + ("event_version",), issues, limit=16
    )
    if event_version is not None and event_version != JOURNAL_EVENT_VERSION:
        issues.append(
            _issue(
                "schema.journal_event_version",
                path + ("event_version",),
                ReasonCode.UNSUPPORTED_SCHEMA_VERSION,
                f"Only journal event version {JOURNAL_EVENT_VERSION} is supported.",
            )
        )
        event_version = None
    sequence = _bounded_integer_value(
        obj.get("sequence", _MISSING), path + ("sequence",), issues, positive=True
    )
    event_id = _stable_id_value(
        obj.get("event_id", _MISSING), path + ("event_id",), issues
    )
    event_type = _enum_value(
        obj.get("event_type", _MISSING),
        JournalEventType,
        path + ("event_type",),
        issues,
    )
    identity = _decode_execution_identity_shape(
        {
            "run_id": obj.get("run_id", _MISSING),
            "task_id": obj.get("task_id", _MISSING),
            "attempt": obj.get("attempt", _MISSING),
            "coordinator_epoch": obj.get("coordinator_epoch", _MISSING),
            "correlation_id": obj.get("correlation_id", _MISSING),
        },
        path,
        issues,
    )
    actor_type = _enum_value(
        obj.get("actor_type", _MISSING), ActorType, path + ("actor_type",), issues
    )
    actor_id = _token_value(obj.get("actor_id", _MISSING), path + ("actor_id",), issues)
    recorded_at = _timestamp_value(
        obj.get("recorded_at", _MISSING), path + ("recorded_at",), issues
    )
    reason_code = _nullable_enum(
        obj.get("reason_code", _MISSING),
        RuntimeReasonCode,
        path + ("reason_code",),
        issues,
    )
    previous_hash = _hash_value(
        obj.get("previous_event_hash", _MISSING),
        path + ("previous_event_hash",),
        issues,
    )
    payload_hash = _hash_value(
        obj.get("payload_hash", _MISSING), path + ("payload_hash",), issues
    )
    payload = _decode_payload_shape(
        obj.get("payload", _MISSING), path + ("payload",), issues
    )
    event_hash = _hash_value(
        obj.get("event_hash", _MISSING), path + ("event_hash",), issues
    )

    if payload is not None and payload_hash is not None:
        expected_payload_hash = "sha256:" + canonical_sha256(payload.to_primitive())
        if payload_hash != expected_payload_hash:
            issues.append(
                _issue(
                    "value.payload_hash_match",
                    path + ("payload_hash",),
                    ReasonCode.INVALID_HASH,
                    "payload_hash does not match the canonical payload.",
                    stage=ValidationStage.LOCAL,
                )
            )
    if event_hash is not None and len(issues) == start:
        unsigned = {key: item for key, item in obj.items() if key != "event_hash"}
        expected_event_hash = "sha256:" + canonical_sha256(unsigned)
        if event_hash != expected_event_hash:
            issues.append(
                _issue(
                    "value.event_hash_match",
                    path + ("event_hash",),
                    ReasonCode.INVALID_HASH,
                    "event_hash does not match the canonical event.",
                    stage=ValidationStage.LOCAL,
                )
            )
    if len(issues) != start:
        return None
    try:
        return JournalEvent(
            event_version,
            sequence,
            event_id,
            event_type,
            identity,
            actor_type,
            actor_id,
            recorded_at,
            reason_code,
            previous_hash,
            payload_hash,
            payload,
            event_hash,
        )  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issues.append(_invalid_contract(path, "Journal event"))
        return None


def _decode_primitive(
    value: object,
    limits: DecodeLimits,
    decoder: Callable[[object, DiagnosticPath, list[ValidationIssue]], T | None],
) -> DecodeResult[T]:
    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    shape_issues = _audit_shape(value, limits)
    if shape_issues:
        return DecodeResult(None, ValidationReport(shape_issues))
    issues: list[ValidationIssue] = []
    decoded = decoder(value, (), issues)
    report = ValidationReport(tuple(issues))
    return DecodeResult(decoded if report.ok else None, report)


def _decode_bytes(
    raw: bytes,
    limits: DecodeLimits,
    primitive_decoder: PrimitiveDecoder[T],
) -> DecodeResult[T]:
    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    if type(raw) is not bytes:
        return DecodeResult(
            None,
            ValidationReport(
                (
                    _issue(
                        "json.raw_type",
                        (),
                        ReasonCode.WRONG_PRIMITIVE_TYPE,
                        "The strict JSON boundary accepts bytes only.",
                    ),
                )
            ),
        )
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if len(raw) > limits.max_bytes:
        return DecodeResult(
            None,
            ValidationReport(
                (
                    _issue(
                        "json.byte_limit",
                        (),
                        ReasonCode.BYTE_LIMIT_EXCEEDED,
                        f"Input exceeds the configured byte limit of {limits.max_bytes}.",
                    ),
                )
            ),
            source_sha256,
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return DecodeResult(
            None,
            ValidationReport(
                (
                    _issue(
                        "json.invalid_utf8",
                        (),
                        ReasonCode.INVALID_UTF8,
                        "Input is not valid UTF-8.",
                    ),
                )
            ),
            source_sha256,
        )
    preflight = _preflight_limits(text, limits)
    if preflight:
        return DecodeResult(None, ValidationReport(preflight), source_sha256)
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_pairs_object,
            parse_constant=_reject_constant,
            parse_int=_bounded_integer,
        )
    except _NonFiniteNumber:
        return DecodeResult(
            None,
            ValidationReport(
                (
                    _issue(
                        "json.non_finite_number",
                        (),
                        ReasonCode.NON_FINITE_NUMBER,
                        "NaN and Infinity are not valid contract numbers.",
                    ),
                )
            ),
            source_sha256,
        )
    except RecursionError:
        return DecodeResult(
            None,
            ValidationReport(
                (
                    _issue(
                        "json.depth_limit",
                        (),
                        ReasonCode.DEPTH_LIMIT_EXCEEDED,
                        "JSON nesting exceeds the parser safety boundary.",
                    ),
                )
            ),
            source_sha256,
        )
    except json.JSONDecodeError:
        return DecodeResult(
            None,
            ValidationReport(
                (
                    _issue(
                        "json.invalid_syntax",
                        (),
                        ReasonCode.INVALID_JSON,
                        "Input is not syntactically valid JSON.",
                    ),
                )
            ),
            source_sha256,
        )
    materialized, duplicate_issues = _materialize_pairs(parsed)
    if duplicate_issues:
        return DecodeResult(None, ValidationReport(duplicate_issues), source_sha256)
    if _has_deferred_integer_range(materialized):
        return DecodeResult(
            None,
            ValidationReport(
                (
                    _issue(
                        "json.integer_range",
                        (),
                        ReasonCode.INTEGER_OUT_OF_RANGE,
                        "JSON integers must fit the signed 64-bit contract range.",
                    ),
                )
            ),
            source_sha256,
        )
    decoded = primitive_decoder(materialized, limits)
    return DecodeResult(decoded.value, decoded.report, source_sha256)


def decode_execution_identity_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[ExecutionIdentity]:
    return _decode_primitive(value, limits, _decode_execution_identity_shape)


def decode_actor_identity_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[ActorIdentity]:
    return _decode_primitive(value, limits, _decode_actor_shape)


def decode_command_identity_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[CommandIdentity]:
    return _decode_primitive(value, limits, _decode_command_shape)


def decode_decision_request_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[DecisionRequest]:
    return _decode_primitive(value, limits, _decode_decision_request_shape)


def decode_decision_command_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[DecisionCommand]:
    return _decode_primitive(value, limits, _decode_decision_command_shape)


def decode_decision_observation_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[DecisionObservation]:
    return _decode_primitive(value, limits, _decode_decision_observation_shape)


def decode_evidence_ref_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[EvidenceRef]:
    return _decode_primitive(value, limits, _decode_evidence_ref_shape)


def decode_effect_receipt_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[EffectReceipt]:
    return _decode_primitive(value, limits, _decode_effect_receipt_shape)


def decode_operation_outcome_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[OperationOutcome]:
    return _decode_primitive(value, limits, _decode_outcome_shape)


def decode_dispatch_recovery_payload_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[DispatchRecoveryPayload]:
    return _decode_primitive(value, limits, _decode_dispatch_recovery_payload)


def decode_journal_event_primitive(
    value: object, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[JournalEvent]:
    return _decode_primitive(value, limits, _decode_journal_event_shape)


def decode_execution_identity_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[ExecutionIdentity]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_execution_identity_primitive(value, limits=active),
    )


def decode_actor_identity_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[ActorIdentity]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_actor_identity_primitive(value, limits=active),
    )


def decode_command_identity_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[CommandIdentity]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_command_identity_primitive(value, limits=active),
    )


def decode_decision_request_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[DecisionRequest]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_decision_request_primitive(value, limits=active),
    )


def decode_decision_command_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[DecisionCommand]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_decision_command_primitive(value, limits=active),
    )


def decode_decision_observation_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[DecisionObservation]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_decision_observation_primitive(
            value, limits=active
        ),
    )


def decode_evidence_ref_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[EvidenceRef]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_evidence_ref_primitive(value, limits=active),
    )


def decode_effect_receipt_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[EffectReceipt]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_effect_receipt_primitive(value, limits=active),
    )


def decode_operation_outcome_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[OperationOutcome]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_operation_outcome_primitive(value, limits=active),
    )


def decode_dispatch_recovery_payload_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[DispatchRecoveryPayload]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_dispatch_recovery_payload_primitive(
            value, limits=active
        ),
    )


def decode_journal_event_bytes(
    raw: bytes, *, limits: DecodeLimits = DEFAULT_DECODE_LIMITS
) -> DecodeResult[JournalEvent]:
    return _decode_bytes(
        raw,
        limits,
        lambda value, active: decode_journal_event_primitive(value, limits=active),
    )


strict_decode_journal_event = decode_journal_event_bytes


__all__ = [
    "decode_actor_identity_bytes",
    "decode_actor_identity_primitive",
    "decode_command_identity_bytes",
    "decode_command_identity_primitive",
    "decode_decision_command_bytes",
    "decode_decision_command_primitive",
    "decode_decision_observation_bytes",
    "decode_decision_observation_primitive",
    "decode_decision_request_bytes",
    "decode_decision_request_primitive",
    "decode_dispatch_recovery_payload_bytes",
    "decode_dispatch_recovery_payload_primitive",
    "decode_effect_receipt_bytes",
    "decode_effect_receipt_primitive",
    "decode_evidence_ref_bytes",
    "decode_evidence_ref_primitive",
    "decode_execution_identity_bytes",
    "decode_execution_identity_primitive",
    "decode_journal_event_bytes",
    "decode_journal_event_primitive",
    "decode_operation_outcome_bytes",
    "decode_operation_outcome_primitive",
    "strict_decode_journal_event",
]
