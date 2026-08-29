"""Strict JSON and closed-schema decoding for execution manifests."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import TypeVar

from .diagnostics import (
    DecodeResult,
    DiagnosticPath,
    MAX_DIAGNOSTIC_PATH_SEGMENTS,
    ReasonCode,
    Severity,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
)
from .models import (
    HASH_RE,
    ID_RE,
    MAX_COLLECTION_ITEMS,
    MAX_ID_LENGTH,
    MAX_PATH_LENGTH,
    MAX_TASKS,
    MAX_TEXT_LENGTH,
    ApprovalSet,
    ExecutionManifest,
    GateApproval,
    Requirement,
    RequirementStatus,
    RiskLevel,
    Task,
    TaskStatus,
    _has_disallowed_contract_control,
)
from .serialization import (
    MAX_CANONICAL_INTEGER,
    MIN_CANONICAL_INTEGER,
    canonical_json_bytes,
)


@dataclass(frozen=True, slots=True)
class DecodeLimits:
    """Resource limits enforced before a model can enter the kernel."""

    max_bytes: int = 1_048_576
    max_depth: int = 32
    max_items: int = 10_000
    max_string_length: int = 16_384

    def __post_init__(self) -> None:
        for field_name in (
            "max_bytes",
            "max_depth",
            "max_items",
            "max_string_length",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_depth > MAX_DIAGNOSTIC_PATH_SEGMENTS:
            raise ValueError(
                "max_depth exceeds the diagnostic path safety boundary of "
                f"{MAX_DIAGNOSTIC_PATH_SEGMENTS}"
            )


DEFAULT_DECODE_LIMITS = DecodeLimits()
_MISSING = object()


class _NonFiniteNumber(ValueError):
    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(token)


class _DeferredIntegerRange:
    __slots__ = ()


_DEFERRED_INTEGER_RANGE = _DeferredIntegerRange()


def _string_digest(value: str) -> str:
    return hashlib.sha256(
        value.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def _normalized_contract_string(value: str) -> str:
    return unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )


def _bounded_segment(value: str) -> str:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "<invalid-unicode~" + _string_digest(value)[:16] + ">"
    if len(value) <= 80:
        return value
    digest = _string_digest(value)[:16]
    return value[:48] + "~" + digest


def _shape_segment(value: object, fallback: int | None = None) -> str | int:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is str and key == "id" and type(item) is str and item:
                return _bounded_segment(item)
        if fallback is not None:
            return fallback
        token = f"dict:{len(value)}".encode("ascii")
        return "@" + hashlib.sha256(token).hexdigest()[:16]
    if value is None:
        token = b"null"
    elif type(value) is bool:
        token = b"bool:1" if value else b"bool:0"
    elif type(value) is int:
        magnitude = abs(value)
        bits = magnitude.bit_length()
        low = magnitude & ((1 << 128) - 1)
        high = magnitude >> max(0, bits - 128)
        token = f"int:{value < 0}:{bits}:{high:x}:{low:x}".encode("ascii")
    elif type(value) is float:
        token = f"float:{value.hex()}".encode("ascii")
    elif type(value) is str:
        token = (
            f"str:{len(value)}:{_string_digest(value)}"
        ).encode("ascii")
    elif type(value) is list:
        if fallback is not None:
            return fallback
        token = f"list:{len(value)}".encode("ascii")
    else:
        return fallback if fallback is not None else "@unsupported"
    return "@" + hashlib.sha256(token).hexdigest()[:16]


def _record_segments(records: list[object]) -> tuple[str, ...]:
    """Return order-independent paths for records that form a semantic set."""

    entries: list[tuple[str, str, bool]] = []
    for value in records:
        if type(value) is dict:
            record_id = value.get("id")
            if type(record_id) is str and ID_RE.fullmatch(record_id):
                entries.append((_bounded_segment(record_id), record_id, True))
                continue
        encoded = canonical_json_bytes(value)
        digest = hashlib.sha256(encoded).hexdigest()
        entries.append(("@" + digest[:24], digest + ":" + encoded.hex(), False))

    grouped: dict[str, list[int]] = {}
    for index, (base, _, stable_id) in enumerate(entries):
        if stable_id:
            continue
        grouped.setdefault(base, []).append(index)

    result = [entry[0] for entry in entries]
    for base, indexes in grouped.items():
        if len(indexes) == 1:
            continue
        for ordinal, index in enumerate(
            sorted(indexes, key=lambda item: entries[item][1]),
            start=1,
        ):
            result[index] = f"{base}~{ordinal}"
    return tuple(result)


def _issue(
    rule_id: str,
    path: DiagnosticPath,
    reason_code: ReasonCode | str,
    message: str,
    *,
    stage: ValidationStage = ValidationStage.BOUNDARY,
) -> ValidationIssue:
    return ValidationIssue(
        stage=stage,
        rule_id=rule_id,
        severity=Severity.ERROR,
        path=path,
        reason_code=ReasonCode(reason_code),
        message=message,
    )


def _scan_string(text: str, start: int) -> tuple[int, int]:
    """Return the closing offset and decoded-character count for one JSON string."""

    index = start + 1
    length = 0
    while index < len(text):
        character = text[index]
        if character == '"':
            return index + 1, length
        if character != "\\":
            length += 1
            index += 1
            continue
        if index + 1 >= len(text):
            return len(text), length
        escape = text[index + 1]
        if escape != "u":
            length += 1
            index += 2
            continue
        if index + 6 > len(text):
            return len(text), length + 1
        first_text = text[index + 2 : index + 6]
        try:
            first = int(first_text, 16)
        except ValueError:
            first = -1
        length += 1
        index += 6
        if 0xD800 <= first <= 0xDBFF and text[index : index + 2] == "\\u":
            second_text = text[index + 2 : index + 6]
            try:
                second = int(second_text, 16)
            except ValueError:
                second = -1
            if 0xDC00 <= second <= 0xDFFF:
                index += 6
    return len(text), length


def _preflight_limits(text: str, limits: DecodeLimits) -> tuple[ValidationIssue, ...]:
    """Bound nesting, entries, and strings without materializing the JSON tree."""

    # Frames are [kind, state]. The parser below only tracks enough grammar to
    # count valid JSON. The standard-library decoder remains syntax authority.
    frames: list[list[str]] = []
    root_state = "value"
    item_count = 0
    index = 0

    def start_value() -> bool:
        nonlocal root_state, item_count
        if not frames:
            if root_state != "value":
                return False
            root_state = "done"
            return True
        frame = frames[-1]
        if frame[0] == "array" and frame[1] in ("value", "value_or_end"):
            frame[1] = "comma_or_end"
            item_count += 1
            return True
        if frame[0] == "object" and frame[1] == "value":
            frame[1] = "comma_or_end"
            item_count += 1
            return True
        return False

    while index < len(text):
        character = text[index]
        if character in " \t\r\n":
            index += 1
            continue
        if character == '"':
            end, string_length = _scan_string(text, index)
            if string_length > limits.max_string_length:
                return (
                    _issue(
                        "json.string_limit",
                        (),
                        "string_limit_exceeded",
                        f"A JSON string exceeds {limits.max_string_length} characters.",
                    ),
                )
            if frames and frames[-1][0] == "object" and frames[-1][1] in (
                "key",
                "key_or_end",
            ):
                frames[-1][1] = "colon"
            else:
                start_value()
            index = end
        elif character in "{[":
            start_value()
            frames.append(
                ["object", "key_or_end"]
                if character == "{"
                else ["array", "value_or_end"]
            )
            if len(frames) > limits.max_depth:
                return (
                    _issue(
                        "json.depth_limit",
                        (),
                        "depth_limit_exceeded",
                        f"JSON nesting exceeds the configured depth of {limits.max_depth}.",
                    ),
                )
            index += 1
        elif character in "}]":
            expected = "object" if character == "}" else "array"
            if frames and frames[-1][0] == expected:
                frames.pop()
            index += 1
        elif character == ":":
            if frames and frames[-1][0] == "object" and frames[-1][1] == "colon":
                frames[-1][1] = "value"
            index += 1
        elif character == ",":
            if frames and frames[-1][1] == "comma_or_end":
                frames[-1][1] = "key" if frames[-1][0] == "object" else "value"
            index += 1
        else:
            start_value()
            while index < len(text) and text[index] not in " \t\r\n,]}:":
                index += 1

        if item_count > limits.max_items:
            return (
                _issue(
                    "json.item_limit",
                    (),
                    "item_limit_exceeded",
                    f"JSON container entries exceed the configured limit of {limits.max_items}.",
                ),
            )
    return ()


def _audit_shape(value: object, limits: DecodeLimits) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    active: set[int] = set()
    item_count = 0
    stack: list[tuple[str, object, DiagnosticPath, int]] = [
        ("enter", value, (), 0)
    ]

    while stack:
        action, current, path, parent_depth = stack.pop()
        if action == "exit":
            active.remove(id(current))
            continue
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if not MIN_CANONICAL_INTEGER <= current <= MAX_CANONICAL_INTEGER:
                issues.append(
                    _issue(
                        "json.integer_range",
                        path,
                        "integer_out_of_range",
                        "JSON integers must fit the signed 64-bit contract range.",
                    )
                )
            continue
        if type(current) is float:
            if not math.isfinite(current):
                issues.append(
                    _issue(
                        "json.non_finite_number",
                        path,
                        "non_finite_number",
                        "NaN and Infinity are not valid contract numbers.",
                    )
                )
            else:
                issues.append(
                    _issue(
                        "json.float_not_allowed",
                        path,
                        "float_not_allowed",
                        "Contract JSON does not admit floating-point numbers.",
                    )
                )
            continue
        if type(current) is str:
            if len(current) > limits.max_string_length:
                return (
                    _issue(
                        "json.string_limit",
                        path,
                        "string_limit_exceeded",
                        f"A JSON string exceeds {limits.max_string_length} characters.",
                    ),
                )
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                issues.append(
                    _issue(
                        "json.invalid_unicode_scalar",
                        path,
                        "invalid_unicode_scalar",
                        "Strings must contain valid Unicode scalar values.",
                    )
                )
            else:
                if _has_disallowed_contract_control(
                    _normalized_contract_string(current)
                ):
                    issues.append(
                        _issue(
                            "value.contract_control",
                            path,
                            "disallowed_contract_control",
                            (
                                "Contract strings must not contain disallowed "
                                "control or bidi characters."
                            ),
                            stage=ValidationStage.LOCAL,
                        )
                    )
            continue
        if type(current) not in (list, dict):
            issues.append(
                _issue(
                    "json.shape_type",
                    path,
                    "unsupported_json_shape",
                    "Values must use exact JSON-compatible Python types.",
                )
            )
            continue

        identity = id(current)
        if identity in active:
            issues.append(
                _issue(
                    "json.cyclic_shape",
                    path,
                    "cyclic_input",
                    "JSON-compatible input cannot contain a cycle.",
                )
            )
            continue
        depth = parent_depth + 1
        if depth > limits.max_depth:
            return (
                _issue(
                    "json.depth_limit",
                    (),
                    "depth_limit_exceeded",
                    f"JSON nesting exceeds the configured depth of {limits.max_depth}.",
                ),
            )
        item_count += len(current)
        if item_count > limits.max_items:
            return (
                _issue(
                    "json.item_limit",
                    (),
                    "item_limit_exceeded",
                    f"JSON container entries exceed the configured limit of {limits.max_items}.",
                ),
            )
        active.add(identity)
        stack.append(("exit", current, path, parent_depth))

        if type(current) is list:
            for index in range(len(current) - 1, -1, -1):
                stack.append(("enter", current[index], path + (index,), depth))
            continue

        normalized_keys: dict[str, str] = {}
        string_items: list[tuple[str, object]] = []
        has_non_string_key = False
        for key, item in current.items():
            if type(key) is not str:
                has_non_string_key = True
                continue
            if len(key) > limits.max_string_length:
                return (
                    _issue(
                        "json.string_limit",
                        path + ("<oversized-key>",),
                        "string_limit_exceeded",
                        f"A JSON string exceeds {limits.max_string_length} characters.",
                    ),
                )
            string_items.append((key, item))
        if has_non_string_key:
            issues.append(
                _issue(
                    "json.object_key_type",
                    path,
                    "object_key_type",
                    "JSON object keys must be strings.",
                )
            )

        for key, item in reversed(sorted(string_items, key=lambda pair: pair[0])):
            key_segment = _bounded_segment(key)
            try:
                key.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                issues.append(
                    _issue(
                        "json.invalid_unicode_scalar",
                        path + (key_segment,),
                        "invalid_unicode_scalar",
                        "Object keys must contain valid Unicode scalar values.",
                    )
                )
            else:
                if _has_disallowed_contract_control(
                    _normalized_contract_string(key)
                ):
                    issues.append(
                        _issue(
                            "value.contract_control",
                            path + (key_segment,),
                            "disallowed_contract_control",
                            (
                                "Contract strings must not contain disallowed "
                                "control or bidi characters."
                            ),
                            stage=ValidationStage.LOCAL,
                        )
                    )
            normalized = _normalized_contract_string(key)
            if normalized in normalized_keys and normalized_keys[normalized] != key:
                issues.append(
                    _issue(
                        "json.normalized_key_collision",
                        path + (_bounded_segment(normalized),),
                        "normalized_key_collision",
                        "Object keys collide after Unicode normalization.",
                    )
                )
            normalized_keys[normalized] = key
            stack.append(("enter", item, path + (key_segment,), depth))
    return tuple(issues)


class _PairsObject(tuple[tuple[str, object], ...]):
    """Temporary JSON object retaining duplicate keys until paths are known."""


def _pairs_object(pairs: list[tuple[str, object]]) -> _PairsObject:
    return _PairsObject(pairs)


def _raw_record_token(value: object) -> bytes:
    """Encode duplicate-preserving JSON shapes for order-independent path IDs."""

    def frame(kind: bytes, parts: list[bytes]) -> bytes:
        return kind + b"".join(len(part).to_bytes(8, "big") + part for part in parts)

    if type(value) is _PairsObject:
        pairs = sorted(
            (
                key.encode("utf-8", errors="surrogatepass"),
                _raw_record_token(item),
            )
            for key, item in value
        )
        return frame(b"O", [frame(b"P", [key, item]) for key, item in pairs])
    if type(value) is list:
        return frame(b"A", [_raw_record_token(item) for item in value])
    if value is None:
        return b"N"
    if type(value) is bool:
        return b"B1" if value else b"B0"
    if type(value) is int:
        return b"I" + str(value).encode("ascii")
    if type(value) is float:
        return b"F" + value.hex().encode("ascii")
    if type(value) is str:
        return frame(b"S", [value.encode("utf-8", errors="surrogatepass")])
    if value is _DEFERRED_INTEGER_RANGE:
        return b"R"
    return frame(b"U", [type(value).__name__.encode("ascii", errors="backslashreplace")])


def _raw_record_segments(records: list[object]) -> tuple[str, ...]:
    entries: list[tuple[str, bytes, bool]] = []
    for value in records:
        if type(value) is _PairsObject:
            identifiers = [
                item
                for key, item in value
                if key == "id" and type(item) is str and ID_RE.fullmatch(item)
            ]
            if len(identifiers) == 1:
                entries.append((_bounded_segment(identifiers[0]), b"", True))
                continue
        token = _raw_record_token(value)
        digest = hashlib.sha256(token).hexdigest()
        entries.append(("@" + digest[:24], token, False))

    grouped: dict[str, list[int]] = {}
    for index, (base, _, stable_id) in enumerate(entries):
        if not stable_id:
            grouped.setdefault(base, []).append(index)
    result = [entry[0] for entry in entries]
    for base, indexes in grouped.items():
        if len(indexes) > 1:
            for ordinal, index in enumerate(
                sorted(indexes, key=lambda item: entries[item][1]),
                start=1,
            ):
                result[index] = f"{base}~{ordinal}"
    return tuple(result)


def _materialize_pairs(
    value: object,
    path: DiagnosticPath = (),
) -> tuple[object, tuple[ValidationIssue, ...]]:
    issues: list[ValidationIssue] = []
    if type(value) is _PairsObject:
        result: dict[str, object] = {}
        for key, item in value:
            key_segment = _bounded_segment(key)
            decoded, nested_issues = _materialize_pairs(
                item,
                path + (key_segment,),
            )
            issues.extend(nested_issues)
            if key in result:
                issues.append(
                    _issue(
                        "json.duplicate_key",
                        path + (key_segment,),
                        "duplicate_object_key",
                        "Duplicate JSON object keys are not admitted.",
                    )
                )
                continue
            result[key] = decoded
        return result, tuple(issues)
    if type(value) is list:
        result_list: list[object] = []
        stable_segments = (
            _raw_record_segments(value)
            if path in {("requirements",), ("tasks",)}
            else None
        )
        for index, item in enumerate(value):
            segment = index if stable_segments is None else stable_segments[index]
            decoded, nested_issues = _materialize_pairs(item, path + (segment,))
            result_list.append(decoded)
            issues.extend(nested_issues)
        return result_list, tuple(issues)
    return value, ()


def _reject_constant(token: str) -> object:
    raise _NonFiniteNumber(token)


def _bounded_integer(token: str) -> int | _DeferredIntegerRange:
    negative = token.startswith("-")
    digits = token[1:] if negative else token
    digits = digits.lstrip("0") or "0"
    boundary = "9223372036854775808" if negative else "9223372036854775807"
    if len(digits) > len(boundary) or (
        len(digits) == len(boundary) and digits > boundary
    ):
        # The JSON scanner still owns syntax classification.  Returning a
        # sentinel lets it inspect trailing junk, a dot, or an unclosed
        # container before we report a valid-but-out-of-range integer token.
        return _DEFERRED_INTEGER_RANGE
    return int(token)


def _has_deferred_integer_range(value: object) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if current is _DEFERRED_INTEGER_RANGE:
            return True
        if type(current) is list:
            pending.extend(current)
        elif type(current) is dict:
            pending.extend(current.values())
    return False


def _closed_object(
    value: object,
    *,
    path: DiagnosticPath,
    allowed: set[str],
    required: set[str],
    issues: list[ValidationIssue],
) -> dict[str, object] | None:
    if value is _MISSING:
        return None
    if type(value) is not dict:
        issues.append(
            _issue(
                "schema.object_type",
                path,
                "wrong_container_type",
                "Expected a JSON object.",
            )
        )
        return None
    for field in sorted(set(value) - allowed):
        issues.append(
            _issue(
                "schema.unknown_field",
                path + (_bounded_segment(field),),
                "unknown_field",
                "Unknown fields are not admitted by this schema.",
            )
        )
    for field in sorted(required - set(value)):
        issues.append(
            _issue(
                "schema.required_field",
                path + (field,),
                "missing_field",
                "A required field is missing.",
            )
        )
    return value


def _string(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    limit: int = MAX_TEXT_LENGTH,
    stable_id: bool = False,
) -> str | None:
    if value is _MISSING:
        return None
    if type(value) is not str:
        issues.append(
            _issue(
                "schema.string_type",
                path,
                "wrong_primitive_type",
                "Expected a string.",
            )
        )
        return None
    if not value.strip():
        issues.append(
            _issue(
                "value.nonempty_string",
                path,
                "empty_string",
                "The string must not be empty.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    normalized = _normalized_contract_string(value)
    if len(normalized) > limit:
        issues.append(
            _issue(
                "value.string_length",
                path,
                "string_limit_exceeded",
                f"The string exceeds the field limit of {limit} characters.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    if stable_id and not ID_RE.fullmatch(normalized):
        issues.append(
            _issue(
                "value.stable_id",
                path,
                "invalid_identifier",
                "Expected a stable uppercase identifier such as TASK-001.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    return normalized


def _integer(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> int | None:
    if value is _MISSING:
        return None
    if type(value) is not int:
        issues.append(
            _issue(
                "schema.integer_type",
                path,
                "wrong_primitive_type",
                "Expected an integer; booleans are not integers at this boundary.",
            )
        )
        return None
    return value


def _boolean(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> bool | None:
    if value is _MISSING:
        return None
    if type(value) is not bool:
        issues.append(
            _issue(
                "schema.boolean_type",
                path,
                "wrong_primitive_type",
                "Expected a boolean.",
            )
        )
        return None
    return value


E = TypeVar("E")


def _enum_value(
    value: object,
    enum_type: type[E],
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> E | None:
    if value is _MISSING:
        return None
    if type(value) is not str:
        issues.append(
            _issue(
                "schema.enum_type",
                path,
                "wrong_primitive_type",
                "Expected a string enum value.",
            )
        )
        return None
    try:
        return enum_type(value)
    except ValueError:
        allowed = sorted(item.value for item in enum_type)  # type: ignore[attr-defined]
        issues.append(
            _issue(
                "schema.enum_value",
                path,
                "unknown_enum_value",
                "Expected one of: " + ", ".join(allowed) + ".",
            )
        )
        return None


def _string_list(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
    *,
    nonempty: bool,
    limit: int = MAX_TEXT_LENGTH,
    stable_ids: bool = False,
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
    item_segments = _raw_record_segments(value)
    for index, item in enumerate(value):
        decoded = _string(
            item,
            path + (item_segments[index],),
            issues,
            limit=limit,
            stable_id=stable_ids,
        )
        if decoded is not None:
            result.append(decoded)
    if len(result) != len(value) or (nonempty and not result):
        return None
    if len(set(result)) != len(result):
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


def _optional_identifier(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> int | str | None:
    if value is _MISSING:
        return None
    if value is None:
        return None
    if type(value) is int:
        if 1 <= value <= MAX_CANONICAL_INTEGER:
            return value
        issues.append(
            _issue(
                "value.positive_identifier",
                path,
                "invalid_identifier",
                "Integer identifiers must be positive.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None
    if type(value) is str:
        return _string(value, path, issues, limit=MAX_ID_LENGTH)
    issues.append(
        _issue(
            "schema.identifier_type",
            path,
            "wrong_primitive_type",
            "Expected a positive integer, non-empty string, or null.",
        )
    )
    return None


def _decode_approval(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> GateApproval | None:
    data = _closed_object(
        value,
        path=path,
        allowed={"approved_by", "approved_at", "artifact_hash"},
        required={"approved_by", "approved_at", "artifact_hash"},
        issues=issues,
    )
    if data is None:
        return None
    before = len(issues)
    approved_by = _string(data.get("approved_by", _MISSING), path + ("approved_by",), issues, limit=MAX_ID_LENGTH)
    approved_at = _string(data.get("approved_at", _MISSING), path + ("approved_at",), issues, limit=32)
    artifact_hash = _string(data.get("artifact_hash", _MISSING), path + ("artifact_hash",), issues, limit=71)
    if approved_at is not None:
        try:
            GateApproval("actor", approved_at, "sha256:" + "0" * 64)
        except ValueError:
            issues.append(
                _issue(
                    "value.utc_timestamp",
                    path + ("approved_at",),
                    "invalid_timestamp",
                    "Expected an ISO-8601 UTC timestamp with a trailing Z.",
                    stage=ValidationStage.LOCAL,
                )
            )
    if artifact_hash is not None and not HASH_RE.fullmatch(artifact_hash):
        issues.append(
            _issue(
                "value.sha256_reference",
                path + ("artifact_hash",),
                "invalid_hash",
                "Expected sha256 followed by 64 lowercase hexadecimal digits.",
                stage=ValidationStage.LOCAL,
            )
        )
    if len(issues) != before or None in (approved_by, approved_at, artifact_hash):
        return None
    try:
        return GateApproval(approved_by, approved_at, artifact_hash)
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "value.gate_approval",
                path,
                "invalid_gate_approval",
                "Gate approval fields are not jointly valid.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None


def _decode_requirement(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> Requirement | None:
    data = _closed_object(
        value,
        path=path,
        allowed={"id", "text", "status"},
        required={"id", "text", "status"},
        issues=issues,
    )
    if data is None:
        return None
    before = len(issues)
    requirement_id = _string(data.get("id", _MISSING), path + ("id",), issues, limit=MAX_ID_LENGTH, stable_id=True)
    text = _string(data.get("text", _MISSING), path + ("text",), issues)
    status = _enum_value(data.get("status", _MISSING), RequirementStatus, path + ("status",), issues)
    if len(issues) != before or None in (requirement_id, text, status):
        return None
    return Requirement(requirement_id, text, status)


_TASK_REQUIRED = {
    "id",
    "title",
    "requirement_ids",
    "depends_on",
    "owned_paths",
    "acceptance_criteria",
    "regression_commands",
    "rollback",
    "wave",
    "risk",
    "status",
}
_TASK_OPTIONAL = {
    "allowed_auxiliary_paths",
    "documentation",
    "may_change_contracts",
    "issue_id",
    "branch",
    "pr_id",
    "squash_commit",
    "agent_owner",
}


def _decode_task(
    value: object,
    path: DiagnosticPath,
    issues: list[ValidationIssue],
) -> Task | None:
    data = _closed_object(
        value,
        path=path,
        allowed=_TASK_REQUIRED | _TASK_OPTIONAL,
        required=_TASK_REQUIRED,
        issues=issues,
    )
    if data is None:
        return None
    before = len(issues)
    task_id = _string(data.get("id", _MISSING), path + ("id",), issues, limit=MAX_ID_LENGTH, stable_id=True)
    title = _string(data.get("title", _MISSING), path + ("title",), issues)
    requirement_ids = _string_list(
        data.get("requirement_ids", _MISSING),
        path + ("requirement_ids",),
        issues,
        nonempty=True,
        limit=MAX_ID_LENGTH,
        stable_ids=True,
    )
    depends_on = _string_list(
        data.get("depends_on", _MISSING),
        path + ("depends_on",),
        issues,
        nonempty=False,
        limit=MAX_ID_LENGTH,
        stable_ids=True,
    )
    owned_paths = _string_list(
        data.get("owned_paths", _MISSING),
        path + ("owned_paths",),
        issues,
        nonempty=True,
        limit=MAX_PATH_LENGTH,
    )
    auxiliary = _string_list(
        data.get("allowed_auxiliary_paths", []),
        path + ("allowed_auxiliary_paths",),
        issues,
        nonempty=False,
        limit=MAX_PATH_LENGTH,
    )
    criteria = _string_list(
        data.get("acceptance_criteria", _MISSING),
        path + ("acceptance_criteria",),
        issues,
        nonempty=True,
    )
    commands = _string_list(
        data.get("regression_commands", _MISSING),
        path + ("regression_commands",),
        issues,
        nonempty=True,
    )
    rollback = _string(data.get("rollback", _MISSING), path + ("rollback",), issues)
    documentation = _string_list(
        data.get("documentation", []),
        path + ("documentation",),
        issues,
        nonempty=False,
        limit=MAX_PATH_LENGTH,
    )
    wave = _integer(data.get("wave", _MISSING), path + ("wave",), issues)
    if wave is not None and wave not in (0, 1, 2):
        issues.append(
            _issue(
                "value.task_wave",
                path + ("wave",),
                "invalid_wave",
                "Task wave must be 0, 1, or 2.",
                stage=ValidationStage.LOCAL,
            )
        )
    risk = _enum_value(data.get("risk", _MISSING), RiskLevel, path + ("risk",), issues)
    status = _enum_value(data.get("status", _MISSING), TaskStatus, path + ("status",), issues)
    may_change = _boolean(
        data.get("may_change_contracts", False),
        path + ("may_change_contracts",),
        issues,
    )
    issue_id = _optional_identifier(data.get("issue_id"), path + ("issue_id",), issues)
    pr_id = _optional_identifier(data.get("pr_id"), path + ("pr_id",), issues)

    optional_strings: dict[str, str | None] = {}
    for field in ("branch", "squash_commit", "agent_owner"):
        raw = data.get(field)
        optional_strings[field] = (
            None if raw is None else _string(raw, path + (field,), issues)
        )

    required_values = (
        task_id,
        title,
        requirement_ids,
        depends_on,
        owned_paths,
        auxiliary,
        criteria,
        commands,
        rollback,
        documentation,
        wave,
        risk,
        status,
        may_change,
    )
    if len(issues) != before or any(value is None for value in required_values):
        return None
    try:
        return Task(
            id=task_id,
            title=title,
            requirement_ids=requirement_ids,
            depends_on=depends_on,
            owned_paths=owned_paths,
            allowed_auxiliary_paths=auxiliary,
            acceptance_criteria=criteria,
            regression_commands=commands,
            rollback=rollback,
            documentation=documentation,
            wave=wave,
            risk=risk,
            may_change_contracts=may_change,
            issue_id=issue_id,
            branch=optional_strings["branch"],
            pr_id=pr_id,
            squash_commit=optional_strings["squash_commit"],
            agent_owner=optional_strings["agent_owner"],
            status=status,
        )
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "value.task_contract",
                path,
                "invalid_task",
                "Task fields are not jointly valid.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None


_MANIFEST_REQUIRED = {
    "schema_version",
    "run_id",
    "goal",
    "base_branch",
    "approved",
    "requirements",
    "tasks",
}
_MANIFEST_OPTIONAL = {"max_concurrency", "protected_paths"}


def _decode_manifest_shape(value: object) -> tuple[ExecutionManifest | None, tuple[ValidationIssue, ...]]:
    issues: list[ValidationIssue] = []
    data = _closed_object(
        value,
        path=(),
        allowed=_MANIFEST_REQUIRED | _MANIFEST_OPTIONAL,
        required=_MANIFEST_REQUIRED,
        issues=issues,
    )
    if data is None:
        return None, tuple(issues)

    schema_version = _integer(data.get("schema_version", _MISSING), ("schema_version",), issues)
    if schema_version is not None and schema_version != 1:
        issues.append(
            _issue(
                "value.schema_version",
                ("schema_version",),
                "unsupported_schema_version",
                "Only execution manifest schema version 1 is supported.",
                stage=ValidationStage.LOCAL,
            )
        )
    run_id = _string(data.get("run_id", _MISSING), ("run_id",), issues, limit=MAX_ID_LENGTH, stable_id=True)
    goal = _string(data.get("goal", _MISSING), ("goal",), issues)
    base_branch = _string(data.get("base_branch", _MISSING), ("base_branch",), issues, limit=MAX_PATH_LENGTH)
    max_concurrency = _integer(data.get("max_concurrency", 3), ("max_concurrency",), issues)
    if max_concurrency is not None and not 1 <= max_concurrency <= 64:
        issues.append(
            _issue(
                "value.max_concurrency",
                ("max_concurrency",),
                "integer_out_of_range",
                "max_concurrency must be between 1 and 64.",
                stage=ValidationStage.LOCAL,
            )
        )
    protected_paths = _string_list(
        data.get("protected_paths", []),
        ("protected_paths",),
        issues,
        nonempty=False,
        limit=MAX_PATH_LENGTH,
    )

    approvals_data = _closed_object(
        data.get("approved", _MISSING),
        path=("approved",),
        allowed={"gate_a", "gate_b"},
        required=set(),
        issues=issues,
    )
    gate_a = None
    gate_b = None
    if approvals_data is not None:
        if "gate_a" in approvals_data:
            gate_a = _decode_approval(approvals_data["gate_a"], ("approved", "gate_a"), issues)
        if "gate_b" in approvals_data:
            gate_b = _decode_approval(approvals_data["gate_b"], ("approved", "gate_b"), issues)

    requirements_raw = data.get("requirements", _MISSING)
    requirements: list[Requirement] = []
    if requirements_raw is _MISSING:
        pass
    elif type(requirements_raw) is not list:
        issues.append(
            _issue(
                "schema.array_type",
                ("requirements",),
                "wrong_container_type",
                "Expected a JSON array.",
            )
        )
    else:
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
        requirement_segments = _record_segments(requirements_raw)
        for index, item in enumerate(requirements_raw):
            decoded = _decode_requirement(
                item,
                ("requirements", requirement_segments[index]),
                issues,
            )
            if decoded is not None:
                requirements.append(decoded)

    tasks_raw = data.get("tasks", _MISSING)
    tasks: list[Task] = []
    if tasks_raw is _MISSING:
        pass
    elif type(tasks_raw) is not list:
        issues.append(
            _issue(
                "schema.array_type",
                ("tasks",),
                "wrong_container_type",
                "Expected a JSON array.",
            )
        )
    else:
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
                    f"Tasks exceed the active-M1 limit of {MAX_TASKS}.",
                    stage=ValidationStage.LOCAL,
                )
            )
        task_segments = _record_segments(tasks_raw)
        for index, item in enumerate(tasks_raw):
            decoded = _decode_task(
                item,
                ("tasks", task_segments[index]),
                issues,
            )
            if decoded is not None:
                tasks.append(decoded)

    for kind, records in (("requirement", requirements), ("task", tasks)):
        counts = Counter(record.id for record in records)
        for record_id in sorted(identifier for identifier, count in counts.items() if count > 1):
            issues.append(
                _issue(
                    f"value.duplicate_{kind}_id",
                    (kind + "s", record_id, "id"),
                    "duplicate_identifier",
                    f"The {kind} identifier is duplicated.",
                    stage=ValidationStage.LOCAL,
                )
            )

    required_values = (
        schema_version,
        run_id,
        goal,
        base_branch,
        max_concurrency,
        protected_paths,
        approvals_data,
    )
    if issues or any(item is None for item in required_values):
        return None, tuple(issues)
    try:
        manifest = ExecutionManifest(
            schema_version=schema_version,
            run_id=run_id,
            goal=goal,
            base_branch=base_branch,
            max_concurrency=max_concurrency,
            protected_paths=protected_paths,
            approvals=ApprovalSet(gate_a=gate_a, gate_b=gate_b),
            requirements=tuple(requirements),
            tasks=tuple(tasks),
        )
    except (TypeError, ValueError):
        issues.append(
            _issue(
                "value.manifest_contract",
                (),
                "invalid_manifest",
                "Manifest fields are not jointly valid.",
                stage=ValidationStage.LOCAL,
            )
        )
        return None, tuple(issues)
    return manifest, ()


def decode_manifest_primitive(
    value: object,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[ExecutionManifest]:
    """Total boundary validation for a Python JSON-compatible shape."""

    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    shape_issues = _audit_shape(value, limits)
    if shape_issues:
        return DecodeResult(None, ValidationReport(shape_issues))
    manifest, schema_issues = _decode_manifest_shape(value)
    report = ValidationReport(schema_issues)
    return DecodeResult(manifest if report.ok else None, report)


@dataclass(frozen=True, slots=True)
class _DecodedJson:
    value: object


def _decode_json_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[_DecodedJson]:
    """Parse strict untrusted JSON once for domain-specific contract decoders."""

    if type(limits) is not DecodeLimits:
        raise TypeError("limits must be a DecodeLimits value")
    if type(raw) is not bytes:
        report = ValidationReport(
            (
                _issue(
                    "json.raw_type",
                    (),
                    "wrong_primitive_type",
                    "The strict JSON boundary accepts bytes only.",
                ),
            )
        )
        return DecodeResult(None, report)

    source_sha256 = hashlib.sha256(raw).hexdigest()
    if len(raw) > limits.max_bytes:
        report = ValidationReport(
            (
                _issue(
                    "json.byte_limit",
                    (),
                    "byte_limit_exceeded",
                    f"Input exceeds the configured byte limit of {limits.max_bytes}.",
                ),
            )
        )
        return DecodeResult(None, report, source_sha256)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        report = ValidationReport(
            (
                _issue(
                    "json.invalid_utf8",
                    (),
                    "invalid_utf8",
                    "Input is not valid UTF-8.",
                ),
            )
        )
        return DecodeResult(None, report, source_sha256)

    preflight_issues = _preflight_limits(text, limits)
    if preflight_issues:
        return DecodeResult(
            None,
            ValidationReport(preflight_issues),
            source_sha256,
        )
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_pairs_object,
            parse_constant=_reject_constant,
            parse_int=_bounded_integer,
        )
    except _NonFiniteNumber:
        report = ValidationReport(
            (
                _issue(
                    "json.non_finite_number",
                    (),
                    "non_finite_number",
                    "NaN and Infinity are not valid contract numbers.",
                ),
            )
        )
        return DecodeResult(None, report, source_sha256)
    except RecursionError:
        report = ValidationReport(
            (
                _issue(
                    "json.depth_limit",
                    (),
                    "depth_limit_exceeded",
                    "JSON nesting exceeds the parser safety boundary.",
                ),
            )
        )
        return DecodeResult(None, report, source_sha256)
    except json.JSONDecodeError:
        report = ValidationReport(
            (
                _issue(
                    "json.invalid_syntax",
                    (),
                    "invalid_json",
                    "Input is not syntactically valid JSON.",
                ),
            )
        )
        return DecodeResult(None, report, source_sha256)

    value, duplicate_issues = _materialize_pairs(parsed)
    if duplicate_issues:
        return DecodeResult(
            None,
            ValidationReport(duplicate_issues),
            source_sha256,
        )

    if _has_deferred_integer_range(value):
        report = ValidationReport(
            (
                _issue(
                    "json.integer_range",
                    (),
                    "integer_out_of_range",
                    "JSON integers must fit the signed 64-bit contract range.",
                ),
            )
        )
        return DecodeResult(None, report, source_sha256)

    return DecodeResult(_DecodedJson(value), ValidationReport(()), source_sha256)


def decode_manifest_bytes(
    raw: bytes,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[ExecutionManifest]:
    """Decode untrusted UTF-8 JSON bytes through the v1 manifest boundary."""

    decoded_json = _decode_json_bytes(raw, limits=limits)
    if not decoded_json.ok:
        return DecodeResult(None, decoded_json.report, decoded_json.source_sha256)
    decoded = decode_manifest_primitive(decoded_json.value.value, limits=limits)
    return DecodeResult(decoded.value, decoded.report, decoded_json.source_sha256)


strict_decode_manifest = decode_manifest_bytes
