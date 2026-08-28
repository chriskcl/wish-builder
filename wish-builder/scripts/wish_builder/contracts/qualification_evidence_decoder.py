"""Strict decoders for raw backend qualification evidence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, TypeVar

from .compatibility import Platform, Provider
from .decoder import (
    DEFAULT_DECODE_LIMITS,
    DecodeLimits,
    _audit_shape,
    _decode_json_bytes,
    _issue,
    _normalized_contract_string,
)
from .diagnostics import (
    DecodeResult,
    DiagnosticPath,
    ReasonCode,
    Severity,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
)
from .models import HASH_RE
from .qualification_evidence import (
    MAX_QUALIFICATION_EVENTS,
    MAX_QUALIFICATION_TASK_PACKET_LENGTH,
    QUALIFICATION_EVENT_GENESIS_DIGEST,
    QUALIFICATION_EVENT_PAYLOAD_TYPES,
    QUALIFICATION_EVENT_SCHEMA_VERSION,
    QUALIFICATION_HARNESS_SCHEMA_VERSION,
    QUALIFICATION_INVENTORY_SCHEMA_VERSION,
    QUALIFICATION_PROVENANCE_SCHEMA_VERSION,
    AttemptPreparedPayload,
    CancelObservedPayload,
    CancelRequestedPayload,
    ChannelReservedPayload,
    CleanupObservedPayload,
    CleanupRequestedPayload,
    CrashInjectedPayload,
    PrepareRequestedPayload,
    ProcessRestartedPayload,
    QualificationEffectStatus,
    QualificationEvent,
    QualificationEventPayload,
    QualificationEventSource,
    QualificationEventType,
    QualificationEvidenceArtifact,
    QualificationEvidenceInventory,
    QualificationEvidenceRole,
    QualificationEvidenceScenario,
    QualificationHarnessDescriptor,
    QualificationProvenance,
    QualificationProvenanceKind,
    QualificationProvenanceSubject,
    QualificationRunOutcome,
    QualificationTurnState,
    QualificationTurnTerminalState,
    ReconcileInspectedPayload,
    ReconcileRequestedPayload,
    ReserveRequestedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    SendRequestedPayload,
    TaskPacketSentPayload,
    TurnStartedPayload,
    TurnTerminalPayload,
    qualification_event_digest,
)


T = TypeVar("T")
PrimitiveDecoder = Callable[[object], DecodeResult[T]]


@dataclass(frozen=True, slots=True)
class _EvidenceDecodeError(Exception):
    path: DiagnosticPath
    rule_id: str
    reason_code: ReasonCode
    message: str
    stage: ValidationStage = ValidationStage.BOUNDARY


def _fail(
    path: DiagnosticPath,
    rule_id: str,
    reason_code: ReasonCode,
    message: str,
    *,
    stage: ValidationStage = ValidationStage.BOUNDARY,
) -> None:
    raise _EvidenceDecodeError(path, rule_id, reason_code, message, stage)


def _error_issue(error: _EvidenceDecodeError) -> ValidationIssue:
    return _issue(
        error.rule_id,
        error.path,
        error.reason_code,
        error.message,
        stage=error.stage,
    )


def _object(
    value: object,
    path: DiagnosticPath,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        _fail(
            path,
            "schema.object_type",
            ReasonCode.WRONG_CONTAINER_TYPE,
            "Expected a JSON object.",
        )
    assert type(value) is dict
    unknown = sorted(set(value) - fields, key=lambda item: item.encode("utf-8"))
    if unknown:
        _fail(
            path + (unknown[0],),
            "schema.unknown_field",
            ReasonCode.UNKNOWN_FIELD,
            "Unknown fields are not admitted by the qualification evidence schema.",
        )
    missing = sorted(fields - set(value), key=lambda item: item.encode("utf-8"))
    if missing:
        _fail(
            path + (missing[0],),
            "schema.required_field",
            ReasonCode.MISSING_FIELD,
            "A required field is missing.",
        )
    return value


def _array(
    value: object,
    path: DiagnosticPath,
    *,
    minimum: int = 0,
    maximum: int,
) -> list[object]:
    if type(value) is not list:
        _fail(
            path,
            "schema.array_type",
            ReasonCode.WRONG_CONTAINER_TYPE,
            "Expected a JSON array.",
        )
    assert type(value) is list
    if len(value) < minimum:
        _fail(
            path,
            "value.nonempty_array",
            ReasonCode.EMPTY_COLLECTION,
            f"Expected at least {minimum} array entries.",
            stage=ValidationStage.LOCAL,
        )
    if len(value) > maximum:
        _fail(
            path,
            "value.collection_limit",
            ReasonCode.ITEM_LIMIT_EXCEEDED,
            f"The array exceeds {maximum} entries.",
            stage=ValidationStage.LOCAL,
        )
    return value


def _string(value: object, path: DiagnosticPath, *, limit: int = 4_096) -> str:
    if type(value) is not str:
        _fail(
            path,
            "schema.string_type",
            ReasonCode.WRONG_PRIMITIVE_TYPE,
            "Expected a string.",
        )
    assert type(value) is str
    normalized = _normalized_contract_string(value)
    if not normalized.strip():
        _fail(
            path,
            "value.nonempty_string",
            ReasonCode.EMPTY_STRING,
            "The string must not be empty.",
            stage=ValidationStage.LOCAL,
        )
    if len(normalized) > limit:
        _fail(
            path,
            "value.string_length",
            ReasonCode.STRING_LIMIT_EXCEEDED,
            f"The string exceeds the field limit of {limit} characters.",
            stage=ValidationStage.LOCAL,
        )
    return normalized


def _integer(
    value: object,
    path: DiagnosticPath,
    *,
    minimum: int,
    maximum: int = 2**63 - 1,
) -> int:
    if type(value) is not int:
        _fail(
            path,
            "schema.integer_type",
            ReasonCode.WRONG_PRIMITIVE_TYPE,
            "Expected an integer; booleans are not integers at this boundary.",
        )
    assert type(value) is int
    if not minimum <= value <= maximum:
        _fail(
            path,
            "value.integer_range",
            ReasonCode.INTEGER_OUT_OF_RANGE,
            f"Expected an integer between {minimum} and {maximum}.",
            stage=ValidationStage.LOCAL,
        )
    return value


def _boolean(value: object, path: DiagnosticPath) -> bool:
    if type(value) is not bool:
        _fail(
            path,
            "schema.boolean_type",
            ReasonCode.WRONG_PRIMITIVE_TYPE,
            "Expected a boolean.",
        )
    return value


def _enum_value(value: object, enum_type: type[T], path: DiagnosticPath) -> T:
    text = _string(value, path, limit=128)
    try:
        return enum_type(text)  # type: ignore[call-arg,return-value]
    except ValueError:
        allowed = ", ".join(item.value for item in enum_type)  # type: ignore[attr-defined]
        _fail(
            path,
            "schema.enum_value",
            ReasonCode.UNKNOWN_ENUM_VALUE,
            f"Expected one of: {allowed}.",
        )


def _schema_version(value: object, path: DiagnosticPath, expected: int, label: str) -> int:
    version = _integer(value, path, minimum=1)
    if version != expected:
        _fail(
            path,
            "schema.version",
            ReasonCode.UNSUPPORTED_SCHEMA_VERSION,
            f"Only {label} schema version {expected} is supported.",
        )
    return version


def _digest(value: object, path: DiagnosticPath) -> str:
    text = _string(value, path, limit=71)
    if not HASH_RE.fullmatch(text):
        _fail(
            path,
            "value.sha256_reference",
            ReasonCode.INVALID_HASH,
            "Expected sha256 followed by 64 lowercase hexadecimal digits.",
            stage=ValidationStage.LOCAL,
        )
    return text


def _optional_digest(value: object, path: DiagnosticPath) -> str | None:
    return None if value is None else _digest(value, path)


def _construct(
    model_type: type[T], diagnostic_path: DiagnosticPath, **values: object
) -> T:
    try:
        return model_type(**values)
    except (TypeError, ValueError):
        _fail(
            diagnostic_path,
            "value.qualification_evidence_contract",
            ReasonCode.INVALID_MANIFEST,
            f"{model_type.__name__} fields are not jointly valid.",
            stage=ValidationStage.LOCAL,
        )


_PAYLOAD_ENUM_FIELDS: dict[tuple[QualificationEventType, str], type[StrEnum]] = {
    (QualificationEventType.TURN_TERMINAL, "terminal_state"): QualificationTurnTerminalState,
    (QualificationEventType.CANCEL_OBSERVED, "effect_status"): QualificationEffectStatus,
    (QualificationEventType.RECONCILE_INSPECTED, "effect_status"): QualificationEffectStatus,
    (QualificationEventType.RECONCILE_INSPECTED, "turn_state"): QualificationTurnState,
    (QualificationEventType.RUN_FINISHED, "outcome"): QualificationRunOutcome,
}
_PAYLOAD_BOOLEAN_FIELDS: frozenset[tuple[QualificationEventType, str]] = frozenset()
_PAYLOAD_ARRAY_FIELDS = frozenset(
    {
        (QualificationEventType.PREPARE_REQUESTED, "owned_paths"),
        (QualificationEventType.ATTEMPT_PREPARED, "owned_paths"),
        (QualificationEventType.CLEANUP_REQUESTED, "process_tree_ids"),
        (QualificationEventType.CLEANUP_OBSERVED, "process_tree_ids"),
        (QualificationEventType.CLEANUP_OBSERVED, "resources_before"),
        (QualificationEventType.CLEANUP_OBSERVED, "resources_after"),
    }
)
_PAYLOAD_DIGEST_FIELDS = frozenset(
    {
        (QualificationEventType.RUN_STARTED, "harness_digest"),
        (QualificationEventType.RUN_STARTED, "trellis_compatibility_digest"),
        (QualificationEventType.RUN_STARTED, "policy_digest"),
        (QualificationEventType.RUN_STARTED, "launch_profile_digest"),
        (QualificationEventType.RUN_STARTED, "capability_digest"),
        (QualificationEventType.RUN_STARTED, "manifest_digest"),
        (QualificationEventType.RUN_STARTED, "trellis_snapshot_digest"),
        (QualificationEventType.SEND_REQUESTED, "task_packet_digest"),
        (QualificationEventType.TASK_PACKET_SENT, "task_packet_digest"),
        (QualificationEventType.RECONCILE_REQUESTED, "request_digest"),
        (QualificationEventType.RECONCILE_INSPECTED, "request_digest"),
    }
)
_PAYLOAD_OPTIONAL_DIGEST_FIELDS = frozenset(
    {
        (QualificationEventType.TURN_TERMINAL, "result_digest"),
        (QualificationEventType.RECONCILE_INSPECTED, "result_digest"),
    }
)
_PAYLOAD_TASK_PACKET_FIELDS = frozenset(
    {
        (QualificationEventType.SEND_REQUESTED, "task_packet"),
        (QualificationEventType.TASK_PACKET_SENT, "task_packet"),
    }
)


def _payload(
    value: object,
    event_type: QualificationEventType,
    path: DiagnosticPath,
) -> QualificationEventPayload:
    payload_type = QUALIFICATION_EVENT_PAYLOAD_TYPES[event_type]
    fields = frozenset(json_name for _, json_name in payload_type.JSON_FIELDS)
    item = _object(value, path, fields)
    values: dict[str, object] = {}
    for attribute, json_name in payload_type.JSON_FIELDS:
        field_path = path + (json_name,)
        key = (event_type, attribute)
        raw = item[json_name]
        enum_type = _PAYLOAD_ENUM_FIELDS.get(key)
        if enum_type is not None:
            values[attribute] = _enum_value(raw, enum_type, field_path)
        elif key in _PAYLOAD_BOOLEAN_FIELDS:
            values[attribute] = _boolean(raw, field_path)
        elif key in _PAYLOAD_ARRAY_FIELDS:
            entries = _array(raw, field_path, minimum=1, maximum=256)
            values[attribute] = tuple(
                _string(entry, field_path + (index,), limit=1_024)
                for index, entry in enumerate(entries)
            )
        elif key in _PAYLOAD_DIGEST_FIELDS:
            values[attribute] = _digest(raw, field_path)
        elif key in _PAYLOAD_OPTIONAL_DIGEST_FIELDS:
            values[attribute] = _optional_digest(raw, field_path)
        elif key in _PAYLOAD_TASK_PACKET_FIELDS:
            values[attribute] = _string(
                raw,
                field_path,
                limit=MAX_QUALIFICATION_TASK_PACKET_LENGTH,
            )
        else:
            values[attribute] = _string(raw, field_path, limit=1_024)
    return _construct(payload_type, path, **values)


_EVENT_FIELDS = frozenset(
    {
        "schemaVersion",
        "sequence",
        "qualificationRunId",
        "scenario",
        "provider",
        "platform",
        "source",
        "eventType",
        "recordedAt",
        "monotonicNs",
        "hostBootId",
        "processIdentity",
        "payload",
        "previousEventDigest",
        "eventDigest",
    }
)


def _decode_event_shape(value: object) -> QualificationEvent:
    item = _object(value, (), _EVENT_FIELDS)
    schema_version = _schema_version(
        item["schemaVersion"],
        ("schemaVersion",),
        QUALIFICATION_EVENT_SCHEMA_VERSION,
        "qualification event",
    )
    sequence = _integer(item["sequence"], ("sequence",), minimum=1)
    event_type = _enum_value(item["eventType"], QualificationEventType, ("eventType",))
    previous_event_digest = _digest(
        item["previousEventDigest"], ("previousEventDigest",)
    )
    event_digest = _digest(item["eventDigest"], ("eventDigest",))
    expected_digest = qualification_event_digest(item)
    if event_digest != expected_digest:
        _fail(
            ("eventDigest",),
            "value.event_digest",
            ReasonCode.INVALID_HASH,
            "eventDigest does not match the canonical event body.",
            stage=ValidationStage.LOCAL,
        )
    if sequence == 1 and previous_event_digest != QUALIFICATION_EVENT_GENESIS_DIGEST:
        _fail(
            ("previousEventDigest",),
            "value.event_genesis",
            ReasonCode.INVALID_HASH,
            "The first event must reference the fixed genesis digest.",
            stage=ValidationStage.REFERENTIAL,
        )
    return _construct(
        QualificationEvent,
        (),
        schema_version=schema_version,
        sequence=sequence,
        qualification_run_id=_string(item["qualificationRunId"], ("qualificationRunId",), limit=256),
        scenario=_enum_value(item["scenario"], QualificationEvidenceScenario, ("scenario",)),
        provider=_enum_value(item["provider"], Provider, ("provider",)),
        platform=_enum_value(item["platform"], Platform, ("platform",)),
        source=_enum_value(item["source"], QualificationEventSource, ("source",)),
        event_type=event_type,
        recorded_at=_string(item["recordedAt"], ("recordedAt",), limit=32),
        monotonic_ns=_integer(item["monotonicNs"], ("monotonicNs",), minimum=0),
        host_boot_id=_string(item["hostBootId"], ("hostBootId",), limit=256),
        process_identity=_string(item["processIdentity"], ("processIdentity",), limit=256),
        payload=_payload(item["payload"], event_type, ("payload",)),
        previous_event_digest=previous_event_digest,
        event_digest=event_digest,
    )


def decode_qualification_event_primitive(
    value: object,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[QualificationEvent]:
    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    issues = _audit_shape(value, limits)
    if issues:
        return DecodeResult(None, ValidationReport(issues))
    try:
        event = _decode_event_shape(value)
    except _EvidenceDecodeError as error:
        return DecodeResult(None, ValidationReport((_error_issue(error),)))
    return DecodeResult(event, ValidationReport(()))


def _decode_document_bytes(
    raw: bytes,
    limits: DecodeLimits,
    decoder: Callable[..., DecodeResult[T]],
) -> DecodeResult[T]:
    decoded_json = _decode_json_bytes(raw, limits=limits)
    if not decoded_json.ok:
        return DecodeResult(None, decoded_json.report, decoded_json.source_sha256)
    assert decoded_json.value is not None
    decoded = decoder(decoded_json.value.value, limits=limits)
    return DecodeResult(decoded.value, decoded.report, decoded_json.source_sha256)


def decode_qualification_event_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[QualificationEvent]:
    return _decode_document_bytes(raw, limits, decode_qualification_event_primitive)


def _prefix_issue(issue: ValidationIssue, prefix: DiagnosticPath) -> ValidationIssue:
    return ValidationIssue(
        stage=issue.stage,
        rule_id=issue.rule_id,
        severity=issue.severity,
        path=prefix + issue.path,
        reason_code=issue.reason_code,
        message=issue.message,
        related_paths=tuple(prefix + path for path in issue.related_paths),
    )


def _log_issue(
    rule_id: str,
    path: DiagnosticPath,
    reason_code: ReasonCode,
    message: str,
    *,
    stage: ValidationStage = ValidationStage.BOUNDARY,
) -> ValidationReport:
    return ValidationReport((_issue(rule_id, path, reason_code, message, stage=stage),))


def decode_qualification_event_log_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[tuple[QualificationEvent, ...]]:
    """Decode canonical JSONL and verify its contiguous event hash chain."""

    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    if type(raw) is not bytes:
        return DecodeResult(
            None,
            _log_issue(
                "jsonl.raw_type",
                (),
                ReasonCode.WRONG_PRIMITIVE_TYPE,
                "The qualification event-log boundary accepts bytes only.",
            ),
        )
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if len(raw) > limits.max_bytes:
        return DecodeResult(
            None,
            _log_issue(
                "jsonl.byte_limit",
                (),
                ReasonCode.BYTE_LIMIT_EXCEEDED,
                f"Input exceeds the configured byte limit of {limits.max_bytes}.",
            ),
            source_sha256,
        )
    if not raw:
        return DecodeResult(
            None,
            _log_issue(
                "jsonl.nonempty",
                (),
                ReasonCode.EMPTY_COLLECTION,
                "A qualification event log must contain at least one event.",
            ),
            source_sha256,
        )
    line_count = raw.count(b"\n")
    event_limit = min(MAX_QUALIFICATION_EVENTS, limits.max_items)
    if line_count > event_limit:
        return DecodeResult(
            None,
            _log_issue(
                "jsonl.event_limit",
                (),
                ReasonCode.ITEM_LIMIT_EXCEEDED,
                f"The event log exceeds {event_limit} events.",
            ),
            source_sha256,
        )
    lines = raw.splitlines(keepends=True)
    if len(lines) > event_limit:
        return DecodeResult(
            None,
            _log_issue(
                "jsonl.event_limit",
                (),
                ReasonCode.ITEM_LIMIT_EXCEEDED,
                f"The event log exceeds {event_limit} events.",
            ),
            source_sha256,
        )

    events: list[QualificationEvent] = []
    expected_previous = QUALIFICATION_EVENT_GENESIS_DIGEST
    stable_identity: tuple[str, Provider, Platform, str] | None = None
    previous_monotonic_ns: int | None = None
    for index, line in enumerate(lines):
        path = (index,)
        if line == b"\n":
            return DecodeResult(
                None,
                _log_issue(
                    "jsonl.blank_line",
                    path,
                    ReasonCode.INVALID_JSON,
                    "Blank lines are not admitted in canonical qualification JSONL.",
                ),
                source_sha256,
            )
        if not line.endswith(b"\n"):
            return DecodeResult(
                None,
                _log_issue(
                    "jsonl.terminal_newline",
                    path,
                    ReasonCode.INVALID_JSON,
                    "Every canonical JSONL event must end with one LF byte.",
                ),
                source_sha256,
            )
        decoded_json = _decode_json_bytes(line, limits=limits)
        if not decoded_json.ok:
            issues = tuple(_prefix_issue(issue, path) for issue in decoded_json.issues)
            return DecodeResult(None, ValidationReport(issues), source_sha256)
        assert decoded_json.value is not None
        decoded = decode_qualification_event_primitive(
            decoded_json.value.value,
            limits=limits,
        )
        if not decoded.ok:
            issues = tuple(_prefix_issue(issue, path) for issue in decoded.issues)
            return DecodeResult(None, ValidationReport(issues), source_sha256)
        assert decoded.value is not None
        event = decoded.value
        if event.canonical_json_bytes() != line:
            return DecodeResult(
                None,
                _log_issue(
                    "jsonl.noncanonical_bytes",
                    path,
                    ReasonCode.INVALID_JSON,
                    "Each event line must use the exact canonical JSON byte representation.",
                    stage=ValidationStage.LOCAL,
                ),
                source_sha256,
            )
        expected_sequence = index + 1
        if event.sequence != expected_sequence:
            return DecodeResult(
                None,
                _log_issue(
                    "value.event_sequence",
                    path + ("sequence",),
                    ReasonCode.INVALID_IDENTIFIER,
                    f"Expected contiguous event sequence {expected_sequence}.",
                    stage=ValidationStage.REFERENTIAL,
                ),
                source_sha256,
            )
        if event.previous_event_digest != expected_previous:
            return DecodeResult(
                None,
                _log_issue(
                    "value.event_hash_chain",
                    path + ("previousEventDigest",),
                    ReasonCode.INVALID_HASH,
                    "previousEventDigest does not extend the preceding event.",
                    stage=ValidationStage.REFERENTIAL,
                ),
                source_sha256,
            )
        identity = (
            event.qualification_run_id,
            event.provider,
            event.platform,
            event.host_boot_id,
        )
        if stable_identity is None:
            stable_identity = identity
        elif identity != stable_identity:
            return DecodeResult(
                None,
                _log_issue(
                    "value.event_log_identity",
                    path,
                    ReasonCode.INVALID_IDENTIFIER,
                    "All events must belong to one run, provider, platform, and host boot.",
                    stage=ValidationStage.REFERENTIAL,
                ),
                source_sha256,
            )
        if previous_monotonic_ns is not None and event.monotonic_ns <= previous_monotonic_ns:
            return DecodeResult(
                None,
                _log_issue(
                    "value.monotonic_order",
                    path + ("monotonicNs",),
                    ReasonCode.INTEGER_OUT_OF_RANGE,
                    "monotonicNs must strictly increase within one host boot.",
                    stage=ValidationStage.REFERENTIAL,
                ),
                source_sha256,
            )
        previous_monotonic_ns = event.monotonic_ns
        expected_previous = event.event_digest
        events.append(event)
    return DecodeResult(tuple(events), ValidationReport(()), source_sha256)


_ARTIFACT_FIELDS = frozenset({"role", "path", "digest", "byteLength", "mediaType"})


def _artifact(value: object, path: DiagnosticPath) -> QualificationEvidenceArtifact:
    item = _object(value, path, _ARTIFACT_FIELDS)
    return _construct(
        QualificationEvidenceArtifact,
        path,
        role=_enum_value(item["role"], QualificationEvidenceRole, path + ("role",)),
        path=_string(item["path"], path + ("path",), limit=1_024),
        digest=_digest(item["digest"], path + ("digest",)),
        byte_length=_integer(item["byteLength"], path + ("byteLength",), minimum=1),
        media_type=_string(item["mediaType"], path + ("mediaType",), limit=128),
    )


_INVENTORY_FIELDS = frozenset(
    {"schemaVersion", "qualificationRunId", "provider", "platform", "artifacts"}
)


def _decode_inventory_shape(value: object) -> QualificationEvidenceInventory:
    item = _object(value, (), _INVENTORY_FIELDS)
    artifacts = _array(item["artifacts"], ("artifacts",), minimum=5, maximum=5)
    return _construct(
        QualificationEvidenceInventory,
        (),
        schema_version=_schema_version(
            item["schemaVersion"],
            ("schemaVersion",),
            QUALIFICATION_INVENTORY_SCHEMA_VERSION,
            "qualification inventory",
        ),
        qualification_run_id=_string(item["qualificationRunId"], ("qualificationRunId",), limit=256),
        provider=_enum_value(item["provider"], Provider, ("provider",)),
        platform=_enum_value(item["platform"], Platform, ("platform",)),
        artifacts=tuple(
            _artifact(entry, ("artifacts", index))
            for index, entry in enumerate(artifacts)
        ),
    )


def decode_qualification_evidence_inventory_primitive(
    value: object,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[QualificationEvidenceInventory]:
    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    issues = _audit_shape(value, limits)
    if issues:
        return DecodeResult(None, ValidationReport(issues))
    try:
        inventory = _decode_inventory_shape(value)
    except _EvidenceDecodeError as error:
        return DecodeResult(None, ValidationReport((_error_issue(error),)))
    return DecodeResult(inventory, ValidationReport(()))


def decode_qualification_evidence_inventory_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[QualificationEvidenceInventory]:
    return _decode_document_bytes(raw, limits, decode_qualification_evidence_inventory_primitive)


_HARNESS_FIELDS = frozenset(
    {
        "schemaVersion",
        "harnessVersion",
        "sourceRevision",
        "entrypoint",
        "eventSchemaVersion",
        "scenarios",
    }
)


def _decode_harness_shape(value: object) -> QualificationHarnessDescriptor:
    item = _object(value, (), _HARNESS_FIELDS)
    scenarios = _array(item["scenarios"], ("scenarios",), minimum=5, maximum=5)
    return _construct(
        QualificationHarnessDescriptor,
        (),
        schema_version=_schema_version(
            item["schemaVersion"],
            ("schemaVersion",),
            QUALIFICATION_HARNESS_SCHEMA_VERSION,
            "qualification harness",
        ),
        harness_version=_string(item["harnessVersion"], ("harnessVersion",), limit=128),
        source_revision=_string(item["sourceRevision"], ("sourceRevision",), limit=64),
        entrypoint=_string(item["entrypoint"], ("entrypoint",), limit=1_024),
        event_schema_version=_schema_version(
            item["eventSchemaVersion"],
            ("eventSchemaVersion",),
            QUALIFICATION_EVENT_SCHEMA_VERSION,
            "qualification event",
        ),
        scenarios=tuple(
            _enum_value(entry, QualificationEvidenceScenario, ("scenarios", index))
            for index, entry in enumerate(scenarios)
        ),
    )


def decode_qualification_harness_descriptor_primitive(
    value: object,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[QualificationHarnessDescriptor]:
    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    issues = _audit_shape(value, limits)
    if issues:
        return DecodeResult(None, ValidationReport(issues))
    try:
        descriptor = _decode_harness_shape(value)
    except _EvidenceDecodeError as error:
        return DecodeResult(None, ValidationReport((_error_issue(error),)))
    return DecodeResult(descriptor, ValidationReport(()))


def decode_qualification_harness_descriptor_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[QualificationHarnessDescriptor]:
    return _decode_document_bytes(raw, limits, decode_qualification_harness_descriptor_primitive)


def _subject(value: object, path: DiagnosticPath) -> QualificationProvenanceSubject:
    item = _object(value, path, _ARTIFACT_FIELDS)
    return _construct(
        QualificationProvenanceSubject,
        path,
        role=_enum_value(item["role"], QualificationEvidenceRole, path + ("role",)),
        path=_string(item["path"], path + ("path",), limit=1_024),
        digest=_digest(item["digest"], path + ("digest",)),
        byte_length=_integer(item["byteLength"], path + ("byteLength",), minimum=1),
        media_type=_string(item["mediaType"], path + ("mediaType",), limit=128),
    )


_PROVENANCE_FIELDS = frozenset(
    {"schemaVersion", "kind", "issuer", "reference", "identity", "sourceRevision", "subjects"}
)


def _decode_provenance_shape(value: object) -> QualificationProvenance:
    item = _object(value, (), _PROVENANCE_FIELDS)
    subjects = _array(item["subjects"], ("subjects",), minimum=4, maximum=4)
    return _construct(
        QualificationProvenance,
        (),
        schema_version=_schema_version(
            item["schemaVersion"],
            ("schemaVersion",),
            QUALIFICATION_PROVENANCE_SCHEMA_VERSION,
            "qualification provenance",
        ),
        kind=_enum_value(item["kind"], QualificationProvenanceKind, ("kind",)),
        issuer=_string(item["issuer"], ("issuer",), limit=1_024),
        reference=_string(item["reference"], ("reference",), limit=1_024),
        identity=_string(item["identity"], ("identity",), limit=1_024),
        source_revision=_string(item["sourceRevision"], ("sourceRevision",), limit=64),
        subjects=tuple(
            _subject(entry, ("subjects", index))
            for index, entry in enumerate(subjects)
        ),
    )


def decode_qualification_provenance_primitive(
    value: object,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[QualificationProvenance]:
    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    issues = _audit_shape(value, limits)
    if issues:
        return DecodeResult(None, ValidationReport(issues))
    try:
        provenance = _decode_provenance_shape(value)
    except _EvidenceDecodeError as error:
        return DecodeResult(None, ValidationReport((_error_issue(error),)))
    return DecodeResult(provenance, ValidationReport(()))


def decode_qualification_provenance_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[QualificationProvenance]:
    return _decode_document_bytes(raw, limits, decode_qualification_provenance_primitive)


def validate_qualification_provenance_binding(
    inventory: QualificationEvidenceInventory,
    provenance: QualificationProvenance,
) -> DecodeResult[QualificationProvenance]:
    """Require detached provenance to bind the inventory's four subject roles."""

    if type(inventory) is not QualificationEvidenceInventory:
        raise TypeError("inventory must be a QualificationEvidenceInventory")
    if type(provenance) is not QualificationProvenance:
        raise TypeError("provenance must be a QualificationProvenance")
    if not provenance.binds_inventory(inventory):
        report = _log_issue(
            "value.provenance_subject_binding",
            ("subjects",),
            ReasonCode.INVALID_HASH,
            "Detached provenance subjects do not exactly bind the inventory artifacts.",
            stage=ValidationStage.REFERENTIAL,
        )
        return DecodeResult(None, report)
    provenance_artifact = inventory.artifact(QualificationEvidenceRole.PROVENANCE)
    if provenance_artifact.digest != provenance.digest():
        report = _log_issue(
            "value.provenance_inventory_digest",
            ("artifacts", QualificationEvidenceRole.PROVENANCE.value, "digest"),
            ReasonCode.INVALID_HASH,
            "The inventory provenance digest does not match the detached provenance bytes.",
            stage=ValidationStage.REFERENTIAL,
        )
        return DecodeResult(None, report)
    return DecodeResult(provenance, ValidationReport(()))


strict_decode_qualification_event = decode_qualification_event_bytes
strict_decode_qualification_event_log = decode_qualification_event_log_bytes
strict_decode_qualification_evidence_inventory = decode_qualification_evidence_inventory_bytes
strict_decode_qualification_harness_descriptor = decode_qualification_harness_descriptor_bytes
strict_decode_qualification_provenance = decode_qualification_provenance_bytes


__all__ = [
    "decode_qualification_event_bytes",
    "decode_qualification_event_log_bytes",
    "decode_qualification_event_primitive",
    "decode_qualification_evidence_inventory_bytes",
    "decode_qualification_evidence_inventory_primitive",
    "decode_qualification_harness_descriptor_bytes",
    "decode_qualification_harness_descriptor_primitive",
    "decode_qualification_provenance_bytes",
    "decode_qualification_provenance_primitive",
    "strict_decode_qualification_event",
    "strict_decode_qualification_event_log",
    "strict_decode_qualification_evidence_inventory",
    "strict_decode_qualification_harness_descriptor",
    "strict_decode_qualification_provenance",
    "validate_qualification_provenance_binding",
]
