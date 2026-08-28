"""Canonical JSON serialization for contract and diagnostic hashes."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import TypeAlias


MIN_CANONICAL_INTEGER = -(2**63)
MAX_CANONICAL_INTEGER = 2**63 - 1
MAX_CANONICAL_DEPTH = 128

JsonScalar: TypeAlias = None | bool | int | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def _normalized_string(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("canonical strings must contain valid Unicode scalars") from exc
    return normalized


def _normalize(value: object, active: set[int], depth: int) -> JsonValue:
    if depth > MAX_CANONICAL_DEPTH:
        raise ValueError("canonical value exceeds the maximum depth")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not MIN_CANONICAL_INTEGER <= value <= MAX_CANONICAL_INTEGER:
            raise ValueError("canonical integer is outside the signed 64-bit range")
        return value
    if type(value) is str:
        return _normalized_string(value)
    if type(value) in (list, tuple):
        identity = id(value)
        if identity in active:
            raise ValueError("canonical values cannot contain cycles")
        active.add(identity)
        try:
            return [_normalize(item, active, depth + 1) for item in value]
        finally:
            active.remove(identity)
    if type(value) is dict:
        identity = id(value)
        if identity in active:
            raise ValueError("canonical values cannot contain cycles")
        active.add(identity)
        try:
            result: dict[str, JsonValue] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("canonical object keys must be strings")
                normalized_key = _normalized_string(key)
                if normalized_key in result:
                    raise ValueError("object keys collide after NFC normalization")
                result[normalized_key] = _normalize(item, active, depth + 1)
            return result
        finally:
            active.remove(identity)
    if type(value) is float:
        raise TypeError("floats are not canonical contract values")
    raise TypeError(
        "canonical values must use JSON primitives, lists, tuples, and dictionaries"
    )


def canonical_json_bytes(value: object) -> bytes:
    """Return the sole normalized byte representation used for contract hashes."""

    normalized = _normalize(value, set(), 0)
    text = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (text + "\n").encode("utf-8", errors="strict")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
