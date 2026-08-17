"""Immutable diagnostics shared by contract decoders and kernel validation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar


MAX_REPORT_ISSUES = 200
MAX_DIAGNOSTIC_MESSAGE = 512
MAX_DIAGNOSTIC_RULE_ID = 128
MAX_DIAGNOSTIC_PATH_SEGMENTS = 64
MAX_DIAGNOSTIC_PATH_SEGMENT_LENGTH = 256
MAX_DIAGNOSTIC_RELATED_PATHS = 64
_RULE_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LIMIT_RULE_ID = "validation.issue_limit"
_LIMIT_MESSAGE_RE = re.compile(
    r"^Validation omitted ([1-9][0-9]*) additional issue\(s\)\.$"
)
_LIMIT_MESSAGE_OVERHEAD = len("Validation omitted ") + len(" additional issue(s).")
# Keep the generated marker within the diagnostic message bound even if many
# already-capped reports are combined.  Ordinary M1 reports are far below this
# limit; the bound only prevents an unbounded caller-controlled integer from
# making serialization fail.
_MAX_OMITTED_COUNT = 10 ** (MAX_DIAGNOSTIC_MESSAGE - _LIMIT_MESSAGE_OVERHEAD) - 1

PathSegment = str | int
DiagnosticPath = tuple[PathSegment, ...]


class ValidationStage(StrEnum):
    """Closed validation stages in admission order."""

    BOUNDARY = "boundary"
    LOCAL = "local"
    REFERENTIAL = "referential"
    POLICY = "policy"
    LIFECYCLE = "lifecycle"
    CAPABILITY = "capability"


_STAGE_ORDER = {stage: index for index, stage in enumerate(ValidationStage)}


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ReasonCode(StrEnum):
    BASE_BRANCH_REUSE = "base_branch_reuse"
    BYTE_LIMIT_EXCEEDED = "byte_limit_exceeded"
    CONTRACT_CHANGE_OUTSIDE_WAVE_ZERO = "contract_change_outside_wave_zero"
    CYCLIC_INPUT = "cyclic_input"
    DEEP_DEPENDENCY_GRAPH = "deep_dependency_graph"
    DEPENDENCY_CYCLE = "dependency_cycle"
    DEPTH_LIMIT_EXCEEDED = "depth_limit_exceeded"
    DISALLOWED_CONTRACT_CONTROL = "disallowed_contract_control"
    DUPLICATE_IDENTIFIER = "duplicate_identifier"
    DUPLICATE_ITEM = "duplicate_item"
    DUPLICATE_OBJECT_KEY = "duplicate_object_key"
    EMPTY_COLLECTION = "empty_collection"
    EMPTY_STRING = "empty_string"
    FLOAT_NOT_ALLOWED = "float_not_allowed"
    GATE_APPROVAL_MISSING = "gate_approval_missing"
    INTEGER_OUT_OF_RANGE = "integer_out_of_range"
    INVALID_GATE_APPROVAL = "invalid_gate_approval"
    INVALID_HASH = "invalid_hash"
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_JSON = "invalid_json"
    INVALID_MANIFEST = "invalid_manifest"
    INVALID_OWNERSHIP_SCOPE = "invalid_ownership_scope"
    INVALID_TASK = "invalid_task"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_UNICODE_SCALAR = "invalid_unicode_scalar"
    INVALID_UTF8 = "invalid_utf8"
    INVALID_WAVE = "invalid_wave"
    ITEM_LIMIT_EXCEEDED = "item_limit_exceeded"
    LATER_WAVE_DEPENDENCY = "later_wave_dependency"
    MISSING_ACTIVE_OWNER = "missing_active_owner"
    MISSING_EXECUTION_IDENTITY = "missing_execution_identity"
    MISSING_FIELD = "missing_field"
    MISSING_MERGE_IDENTITY = "missing_merge_identity"
    MISSING_PR_IDENTITY = "missing_pr_identity"
    NON_FINITE_NUMBER = "non_finite_number"
    NORMALIZED_KEY_COLLISION = "normalized_key_collision"
    OBJECT_KEY_TYPE = "object_key_type"
    PARALLEL_OWNERSHIP_CONFLICT = "parallel_ownership_conflict"
    SELF_DEPENDENCY = "self_dependency"
    SHARED_BRANCH_IDENTITY = "shared_branch_identity"
    SHARED_ISSUE_IDENTITY = "shared_issue_identity"
    SHARED_PR_IDENTITY = "shared_pr_identity"
    STRING_LIMIT_EXCEEDED = "string_limit_exceeded"
    TASK_NOT_APPROVED = "task_not_approved"
    UNCOVERED_REQUIREMENT = "uncovered_requirement"
    UNFINISHED_TASK = "unfinished_task"
    UNIMPLEMENTED_REQUIREMENT = "unimplemented_requirement"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    UNKNOWN_ENUM_VALUE = "unknown_enum_value"
    UNKNOWN_FIELD = "unknown_field"
    UNKNOWN_REQUIREMENT = "unknown_requirement"
    UNORDERED_SERIAL_WAVE = "unordered_serial_wave"
    UNSUPPORTED_JSON_SHAPE = "unsupported_json_shape"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    VALIDATION_ISSUE_LIMIT = "validation_issue_limit"
    WAVE_BARRIER_BYPASS = "wave_barrier_bypass"
    WRONG_CONTAINER_TYPE = "wrong_container_type"
    WRONG_PRIMITIVE_TYPE = "wrong_primitive_type"


def _canonical_diagnostic_string(value: str, field_name: str) -> str:
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} strings must be valid Unicode") from exc
    return normalized


def _validate_path(path: DiagnosticPath, field_name: str) -> DiagnosticPath:
    if type(path) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if len(path) > MAX_DIAGNOSTIC_PATH_SEGMENTS:
        raise ValueError(f"{field_name} exceeds the path segment limit")
    normalized: list[PathSegment] = []
    for segment in path:
        if type(segment) not in (str, int):
            raise TypeError(f"{field_name} segments must be strings or integers")
        if type(segment) is int and not -(2**63) <= segment <= 2**63 - 1:
            raise ValueError(f"{field_name} integers must fit the signed 64-bit range")
        if type(segment) is str:
            segment = _canonical_diagnostic_string(segment, field_name)
            if len(segment) > MAX_DIAGNOSTIC_PATH_SEGMENT_LENGTH:
                raise ValueError(f"{field_name} contains an oversized string segment")
        normalized.append(segment)
    return tuple(normalized)


def _path_sort_key(path: DiagnosticPath) -> tuple[tuple[str, str], ...]:
    return tuple(
        ("i", f"{segment:+020d}") if type(segment) is int else ("s", segment)
        for segment in path
    )


def _render_safe_text(value: str) -> str:
    safe = []
    for character in value:
        if character == "\n":
            safe.append("\\n")
        elif character == "\r":
            safe.append("\\r")
        elif character == "\t":
            safe.append("\\t")
        elif unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}:
            codepoint = ord(character)
            escape = "\\u{0:04x}" if codepoint <= 0xFFFF else "\\U{0:08x}"
            safe.append(escape.format(codepoint))
        else:
            safe.append(character)
    return "".join(safe)


def _path_text(path: DiagnosticPath) -> str:
    if not path:
        return "$"
    encoded = [
        _render_safe_text(str(segment).replace("~", "~0").replace("/", "~1"))
        for segment in path
    ]
    return "/" + "/".join(encoded)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One stable machine-readable validation finding."""

    stage: ValidationStage
    rule_id: str
    severity: Severity
    path: DiagnosticPath
    reason_code: ReasonCode
    message: str
    related_paths: tuple[DiagnosticPath, ...] = ()

    def __post_init__(self) -> None:
        if type(self.stage) is not ValidationStage:
            raise TypeError("stage must be a ValidationStage")
        if type(self.severity) is not Severity:
            raise TypeError("severity must be a Severity")
        if type(self.reason_code) is not ReasonCode:
            raise TypeError("reason_code must be a ReasonCode")
        if (
            type(self.rule_id) is not str
            or len(self.rule_id) > MAX_DIAGNOSTIC_RULE_ID
            or not _RULE_RE.fullmatch(self.rule_id)
        ):
            raise ValueError("rule_id is not a stable rule identifier")
        if type(self.message) is not str or not self.message:
            raise ValueError("message must be a non-empty string")
        normalized_message = _canonical_diagnostic_string(self.message, "message")
        if len(normalized_message) > MAX_DIAGNOSTIC_MESSAGE:
            raise ValueError("message exceeds the diagnostic limit")
        object.__setattr__(self, "message", normalized_message)
        object.__setattr__(self, "path", _validate_path(self.path, "path"))
        if type(self.related_paths) is not tuple:
            raise TypeError("related_paths must be a tuple")
        if len(self.related_paths) > MAX_DIAGNOSTIC_RELATED_PATHS:
            raise ValueError("related_paths exceeds the path count limit")
        normalized_related_paths = tuple(
            _validate_path(path, "related_paths") for path in self.related_paths
        )
        object.__setattr__(
            self,
            "related_paths",
            tuple(sorted(set(normalized_related_paths), key=_path_sort_key)),
        )

    @property
    def rule(self) -> str:
        """Compatibility spelling for callers that refer to a rule."""

        return self.rule_id

    @property
    def path_text(self) -> str:
        return _path_text(self.path)

    def identity_key(self) -> tuple[object, ...]:
        """Return the identity of one exact fact.

        A rule/path pair is not sufficient: the same check may intentionally
        produce independent findings at different stages, with different
        severities, reasons, messages, or related paths.  Those facts must all
        survive report construction; only byte-identical normalized facts are
        deduplicated.
        """

        return (
            self.stage.value,
            self.rule_id,
            self.severity.value,
            _path_sort_key(self.path),
            self.reason_code.value,
            self.message,
            tuple(_path_sort_key(path) for path in self.related_paths),
        )

    def sort_key(self) -> tuple[object, ...]:
        return (
            _STAGE_ORDER[self.stage],
            _path_sort_key(self.path),
            self.rule_id,
            tuple(_path_sort_key(path) for path in self.related_paths),
            self.reason_code,
            self.severity.value,
            self.message,
        )

    def _dedup_preference(self) -> tuple[object, ...]:
        """Compatibility ordering retained for callers of the old helper."""

        return self.sort_key()

    def to_primitive(self) -> dict[str, object]:
        return {
            "message": self.message,
            "path": list(self.path),
            "reason_code": self.reason_code.value,
            "related_paths": [list(path) for path in self.related_paths],
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "stage": self.stage.value,
        }


Diagnostic = ValidationIssue


def _limit_count(issue: ValidationIssue) -> int:
    """Read only our canonical marker spelling, failing closed otherwise."""

    if (
        issue.stage is not ValidationStage.CAPABILITY
        or issue.severity is not Severity.ERROR
        or issue.path
        or issue.reason_code is not ReasonCode.VALIDATION_ISSUE_LIMIT
        or issue.related_paths
    ):
        return 1
    match = _LIMIT_MESSAGE_RE.fullmatch(issue.message)
    if match is None:
        # A caller may construct a forged marker with arbitrary stage, severity,
        # path, or prose.  Its unknown omitted facts are conservatively counted
        # as one and replaced with a canonical marker by the report boundary.
        return 1
    count = int(match.group(1))
    return min(count, _MAX_OMITTED_COUNT)


def _canonical_limit_issue(omitted: int) -> ValidationIssue:
    bounded = max(1, min(int(omitted), _MAX_OMITTED_COUNT))
    return ValidationIssue(
        stage=ValidationStage.CAPABILITY,
        rule_id=_LIMIT_RULE_ID,
        severity=Severity.ERROR,
        path=(),
        reason_code=ReasonCode.VALIDATION_ISSUE_LIMIT,
        message=f"Validation omitted {bounded} additional issue(s).",
    )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """A deterministically ordered, bounded collection of findings."""

    issues: tuple[ValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        if type(self.issues) is not tuple:
            raise TypeError("issues must be a tuple")
        if not all(type(issue) is ValidationIssue for issue in self.issues):
            raise TypeError("issues must contain only ValidationIssue values")

        # Limit markers are aggregate metadata rather than ordinary facts.  Do
        # not deduplicate them: combining two independently capped reports must
        # add both omitted counts, even when their canonical marker bytes match.
        carried_omitted = 0
        representatives: dict[tuple[object, ...], ValidationIssue] = {}
        for issue in self.issues:
            if issue.rule_id == _LIMIT_RULE_ID:
                carried_omitted += _limit_count(issue)
                continue
            identity = issue.identity_key()
            representatives.setdefault(identity, issue)

        detailed = list(representatives.values())
        detailed.sort(key=ValidationIssue.sort_key)

        # The cap includes the synthetic limit finding.  Once a marker exists,
        # reserve its one slot even when the current batch itself has only 200
        # details.  Retention is severity-first so a later blocking error cannot
        # be evicted by an earlier warning; final presentation returns to the
        # normative stage/path/rule order below.
        needs_marker = carried_omitted > 0 or len(detailed) > MAX_REPORT_ISSUES
        if needs_marker:
            detail_capacity = MAX_REPORT_ISSUES - 1
            retained = sorted(
                detailed,
                key=lambda issue: (
                    0 if issue.severity is Severity.ERROR else 1,
                    issue.sort_key(),
                ),
            )[:detail_capacity]
            omitted = carried_omitted + (len(detailed) - len(retained))
            limit_issue = _canonical_limit_issue(omitted)
            retained.sort(key=ValidationIssue.sort_key)
            unique = retained + [limit_issue]
        else:
            unique = detailed

        # Retention order is an admission detail; serialized/rendered order is
        # always the normative canonical stage/path/rule order.
        unique.sort(key=ValidationIssue.sort_key)

        object.__setattr__(self, "issues", tuple(unique))

    @property
    def diagnostics(self) -> tuple[ValidationIssue, ...]:
        return self.issues

    @property
    def ok(self) -> bool:
        return not self.has_errors

    @property
    def has_errors(self) -> bool:
        return any(issue.severity is Severity.ERROR for issue in self.issues)

    def to_primitive(self) -> dict[str, object]:
        return {
            "issues": [issue.to_primitive() for issue in self.issues],
            "ok": self.ok,
            "schema_version": 1,
        }

    def to_json_bytes(self) -> bytes:
        from .serialization import canonical_json_bytes

        return canonical_json_bytes(self.to_primitive())

    def sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    def render_text(self) -> str:
        lines = [
            (
                f"{issue.severity.value.upper()} {issue.stage.value} "
                f"{issue.rule_id} {issue.path_text}: "
                f"{_render_safe_text(issue.message)} "
                f"[{issue.reason_code.value}]"
            )
            for issue in self.issues
        ]
        if not lines:
            lines.append("OK")
        return "\n".join(lines) + "\n"


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class DecodeResult(Generic[T]):
    """Typed result for a contract boundary decode or admission."""

    value: T | None
    report: ValidationReport
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.report) is not ValidationReport:
            raise TypeError("report must be a ValidationReport")
        if self.source_sha256 is not None and (
            type(self.source_sha256) is not str
            or not _SHA256_RE.fullmatch(self.source_sha256)
        ):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        if self.report.has_errors and self.value is not None:
            raise ValueError("an invalid decode result cannot expose a value")
        if self.report.ok and self.value is None:
            raise ValueError("a successful decode result must expose a value")

    @property
    def ok(self) -> bool:
        return self.report.ok

    @property
    def issues(self) -> tuple[ValidationIssue, ...]:
        return self.report.issues

    @property
    def diagnostics(self) -> tuple[ValidationIssue, ...]:
        return self.report.issues

    def diagnostic_bytes(self) -> bytes:
        return self.report.to_json_bytes()

    def diagnostic_sha256(self) -> str:
        return self.report.sha256()


def combine_reports(*reports: ValidationReport) -> ValidationReport:
    issues = tuple(issue for report in reports for issue in report.issues)
    return ValidationReport(issues)
