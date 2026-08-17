"""Curated raw-byte failures for the strict JSON trust boundary."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostileJsonCase:
    name: str
    raw: bytes
    expected_rule: str


HOSTILE_RAW_BYTES = (
    HostileJsonCase(
        "duplicate root key",
        b'{"schema_version":1,"schema_version":1}',
        "json.duplicate_key",
    ),
    HostileJsonCase(
        "duplicate nested key",
        b'{"outer":{"value":1,"value":2}}',
        "json.duplicate_key",
    ),
    HostileJsonCase("nan", b'{"schema_version":NaN}', "json.non_finite_number"),
    HostileJsonCase(
        "positive infinity",
        b'{"schema_version":Infinity}',
        "json.non_finite_number",
    ),
    HostileJsonCase(
        "negative infinity",
        b'{"schema_version":-Infinity}',
        "json.non_finite_number",
    ),
    HostileJsonCase("invalid utf8", b'{"goal":"\xff"}', "json.invalid_utf8"),
    HostileJsonCase("malformed object", b'{"schema_version":1', "json.invalid_syntax"),
    HostileJsonCase("finite float", b'{"schema_version":1.5}', "json.float_not_allowed"),
    HostileJsonCase(
        "lone unicode surrogate",
        b'{"goal":"\\ud800"}',
        "json.invalid_unicode_scalar",
    ),
    HostileJsonCase(
        "normalized object key collision",
        b'{"\\u00e9":1,"e\\u0301":2}',
        "json.normalized_key_collision",
    ),
    HostileJsonCase(
        "line-ending object key collision",
        b'{"line\\r\\nbreak":1,"line\\nbreak":2}',
        "json.normalized_key_collision",
    ),
    HostileJsonCase(
        "signed 64-bit integer overflow",
        b'{"schema_version":9223372036854775808}',
        "json.integer_range",
    ),
)
