"""Curated raw-byte failures for the strict JSON trust boundary."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HostileJsonCase:
    name: str
    raw: bytes
    expected_report: dict[str, object]
    expected_source_sha256: str


def _report(
    rule_id: str,
    path: list[str | int],
    reason_code: str,
    message: str,
) -> dict[str, object]:
    return {
        "issues": [
            {
                "message": message,
                "path": path,
                "reason_code": reason_code,
                "related_paths": [],
                "rule_id": rule_id,
                "severity": "error",
                "stage": "boundary",
            }
        ],
        "ok": False,
        "schema_version": 1,
    }


HOSTILE_RAW_BYTES = (
    HostileJsonCase(
        "duplicate root key",
        b'{"schema_version":1,"schema_version":1}',
        _report(
            "json.duplicate_key",
            ["schema_version"],
            "duplicate_object_key",
            "Duplicate JSON object keys are not admitted.",
        ),
        "dfd205aeecbcf824d6da92dcf6cfd8102bc604002f14aa2356923cbfcaae789b",
    ),
    HostileJsonCase(
        "duplicate nested key",
        b'{"outer":{"value":1,"value":2}}',
        _report(
            "json.duplicate_key",
            ["outer", "value"],
            "duplicate_object_key",
            "Duplicate JSON object keys are not admitted.",
        ),
        "eea223bd439d315de3dc13896b1418a428f2d5b3860323b5a0e377207db0c360",
    ),
    HostileJsonCase(
        "nan",
        b'{"schema_version":NaN}',
        _report(
            "json.non_finite_number",
            [],
            "non_finite_number",
            "NaN and Infinity are not valid contract numbers.",
        ),
        "8a7ed23b7b7c48e38a73f0a0ef0e7f73fa38cbde7bc7d811d281c2b534ddd824",
    ),
    HostileJsonCase(
        "positive infinity",
        b'{"schema_version":Infinity}',
        _report(
            "json.non_finite_number",
            [],
            "non_finite_number",
            "NaN and Infinity are not valid contract numbers.",
        ),
        "26bd915b701ca90c585c38d1d60dc0c00f2b64343982a2bdea671b3e39a35949",
    ),
    HostileJsonCase(
        "negative infinity",
        b'{"schema_version":-Infinity}',
        _report(
            "json.non_finite_number",
            [],
            "non_finite_number",
            "NaN and Infinity are not valid contract numbers.",
        ),
        "a977a12f39071ba82f0fbade953dbd22f16b674d1a3ce8b6a0193750d358cf22",
    ),
    HostileJsonCase(
        "invalid utf8",
        b'{"goal":"\xff"}',
        _report(
            "json.invalid_utf8",
            [],
            "invalid_utf8",
            "Input is not valid UTF-8.",
        ),
        "a31171c555fd0774744374c4c8f6bafbdff57fd066efd228a9c9522ec89ebe15",
    ),
    HostileJsonCase(
        "malformed object",
        b'{"schema_version":1',
        _report(
            "json.invalid_syntax",
            [],
            "invalid_json",
            "Input is not syntactically valid JSON.",
        ),
        "7b3199a0944001af205a2bd932d500b18cfebebc376b362eb19ea1f55c4fbe3c",
    ),
    HostileJsonCase(
        "finite float",
        b'{"schema_version":1.5}',
        _report(
            "json.float_not_allowed",
            ["schema_version"],
            "float_not_allowed",
            "Contract JSON does not admit floating-point numbers.",
        ),
        "ceff91d0b57a7971630de28abe20f356bc9c8a8d187b4ad8a32bdec8464860c3",
    ),
    HostileJsonCase(
        "lone unicode surrogate",
        b'{"goal":"\\ud800"}',
        _report(
            "json.invalid_unicode_scalar",
            ["goal"],
            "invalid_unicode_scalar",
            "Strings must contain valid Unicode scalar values.",
        ),
        "4137ffbaaea4b32085a78e459eb700c20036b37d45c04c78bb02246c54c56082",
    ),
    HostileJsonCase(
        "normalized object key collision",
        b'{"\\u00e9":1,"e\\u0301":2}',
        _report(
            "json.normalized_key_collision",
            ["\u00e9"],
            "normalized_key_collision",
            "Object keys collide after Unicode normalization.",
        ),
        "30e2fc370c72a0a9f660cd87d6ee2487e62318b63897f4acac5609eb10843056",
    ),
    HostileJsonCase(
        "line-ending object key collision",
        b'{"line\\r\\nbreak":1,"line\\nbreak":2}',
        _report(
            "json.normalized_key_collision",
            ["line\nbreak"],
            "normalized_key_collision",
            "Object keys collide after Unicode normalization.",
        ),
        "4bfbef4dea518484d05898b49b73c2ad75a1a33cc60e65a37a743ba54a827bf0",
    ),
    HostileJsonCase(
        "signed 64-bit integer overflow",
        b'{"schema_version":9223372036854775808}',
        _report(
            "json.integer_range",
            [],
            "integer_out_of_range",
            "JSON integers must fit the signed 64-bit contract range.",
        ),
        "3632a88952da1ed329456ba1e950fc914e508a511d45ddb72de395a6b132d8d7",
    ),
)
