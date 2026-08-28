"""Wish Builder-owned coding-agent backend contracts.

Trellis owns task and lifecycle records. These contracts describe only the
admitted provider channel used to execute one frozen Wish Builder snapshot.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from wish_builder.contracts import WorkerProvider, canonical_json_bytes, canonical_sha256
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import EffectStatus

from .effects import PreparedEffect


BACKEND_PORT_SCHEMA_VERSION = 1
MAX_TASK_PACKET_BYTES = 1024 * 1024
MAX_BACKEND_EVIDENCE = 32

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


def _text(
    value: object,
    field_name: str,
    *,
    max_length: int = 256,
    multiline: bool = False,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    if not normalized or not normalized.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds the string limit")
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain valid Unicode") from exc
    allowed = {"\n", "\t"} if multiline else set()
    if any(
        (ord(character) < 32 or ord(character) == 127)
        and character not in allowed
        for character in normalized
    ):
        raise ValueError(f"{field_name} contains a disallowed control character")
    return normalized


def _token(value: object, field_name: str) -> str:
    normalized = _text(value, field_name)
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} is not a stable token")
    return normalized


def _digest(value: object, field_name: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return value


def _optional_digest(value: object, field_name: str) -> str | None:
    return None if value is None else _digest(value, field_name)


def _timestamp(value: object, field_name: str) -> str:
    normalized = _text(value, field_name, max_length=32)
    if not _TIMESTAMP_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a UTC timestamp")
    try:
        datetime.fromisoformat(normalized[:-1])
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UTC timestamp") from exc
    return normalized


def _evidence(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError("evidence must be a tuple")
    if len(value) > MAX_BACKEND_EVIDENCE:
        raise ValueError("evidence exceeds the item limit")
    normalized = tuple(
        _text(item, "evidence item", max_length=512) for item in value
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError("evidence must not contain duplicates")
    return tuple(sorted(normalized, key=lambda item: item.encode("utf-8")))


class CanonicalBackendContract:
    __slots__ = ()

    def to_primitive(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    def canonical_sha256(self) -> str:
        return "sha256:" + canonical_sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class BackendCapabilities(CanonicalBackendContract):
    """The immutable backend capability projection admitted for a run."""

    provider: WorkerProvider
    platform: str
    capability_digest: str
    launch_profile_digest: str
    policy_digest: str
    max_task_packet_bytes: int
    caller_supplied_ids: bool = True
    idempotent_operations: bool = True
    inspect_operations: bool = True
    fresh_session_per_attempt: bool = True

    def __post_init__(self) -> None:
        if type(self.provider) is not WorkerProvider:
            raise TypeError("provider must be a WorkerProvider")
        object.__setattr__(self, "platform", _token(self.platform, "platform"))
        for field_name in (
            "capability_digest",
            "launch_profile_digest",
            "policy_digest",
        ):
            object.__setattr__(
                self, field_name, _digest(getattr(self, field_name), field_name)
            )
        if (
            type(self.max_task_packet_bytes) is not int
            or isinstance(self.max_task_packet_bytes, bool)
            or self.max_task_packet_bytes <= 0
        ):
            raise ValueError("max_task_packet_bytes must be a positive integer")
        for field_name in (
            "caller_supplied_ids",
            "idempotent_operations",
            "inspect_operations",
            "fresh_session_per_attempt",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be a boolean")

    def to_primitive(self) -> dict[str, object]:
        return {
            "caller_supplied_ids": self.caller_supplied_ids,
            "capability_digest": self.capability_digest,
            "fresh_session_per_attempt": self.fresh_session_per_attempt,
            "idempotent_operations": self.idempotent_operations,
            "inspect_operations": self.inspect_operations,
            "launch_profile_digest": self.launch_profile_digest,
            "max_task_packet_bytes": self.max_task_packet_bytes,
            "platform": self.platform,
            "policy_digest": self.policy_digest,
            "provider": self.provider.value,
            "schema_version": BACKEND_PORT_SCHEMA_VERSION,
        }


@dataclass(frozen=True, slots=True)
class ReserveChannel(CanonicalBackendContract):
    operation_id: str
    attempt_id: str
    dispatch_id: str
    channel_id: str
    provider: WorkerProvider
    capability_digest: str
    launch_profile_digest: str
    policy_digest: str
    command_type = "reserve_channel"

    def __post_init__(self) -> None:
        for field_name in ("operation_id", "attempt_id", "dispatch_id", "channel_id"):
            object.__setattr__(
                self, field_name, _token(getattr(self, field_name), field_name)
            )
        if type(self.provider) is not WorkerProvider:
            raise TypeError("provider must be a WorkerProvider")
        for field_name in (
            "capability_digest",
            "launch_profile_digest",
            "policy_digest",
        ):
            object.__setattr__(
                self, field_name, _digest(getattr(self, field_name), field_name)
            )

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "capability_digest": self.capability_digest,
            "channel_id": self.channel_id,
            "command_type": self.command_type,
            "dispatch_id": self.dispatch_id,
            "launch_profile_digest": self.launch_profile_digest,
            "operation_id": self.operation_id,
            "policy_digest": self.policy_digest,
            "provider": self.provider.value,
            "schema_version": BACKEND_PORT_SCHEMA_VERSION,
        }


@dataclass(frozen=True, slots=True)
class SendTaskPacket(CanonicalBackendContract):
    operation_id: str
    attempt_id: str
    dispatch_id: str
    channel_id: str
    message_id: str
    turn_id: str
    task_packet: str
    task_packet_digest: str
    command_type = "send_task_packet"

    def __post_init__(self) -> None:
        for field_name in (
            "operation_id",
            "attempt_id",
            "dispatch_id",
            "channel_id",
            "message_id",
            "turn_id",
        ):
            object.__setattr__(
                self, field_name, _token(getattr(self, field_name), field_name)
            )
        packet = _text(
            self.task_packet,
            "task_packet",
            max_length=MAX_TASK_PACKET_BYTES,
            multiline=True,
        )
        if len(packet.encode("utf-8")) > MAX_TASK_PACKET_BYTES:
            raise ValueError("task_packet exceeds the byte limit")
        object.__setattr__(self, "task_packet", packet)
        object.__setattr__(
            self,
            "task_packet_digest",
            _digest(self.task_packet_digest, "task_packet_digest"),
        )
        expected = "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()
        if self.task_packet_digest != expected:
            raise ValueError("task_packet_digest does not match task_packet")

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "channel_id": self.channel_id,
            "command_type": self.command_type,
            "dispatch_id": self.dispatch_id,
            "message_id": self.message_id,
            "operation_id": self.operation_id,
            "schema_version": BACKEND_PORT_SCHEMA_VERSION,
            "task_packet": self.task_packet,
            "task_packet_digest": self.task_packet_digest,
            "turn_id": self.turn_id,
        }


@dataclass(frozen=True, slots=True)
class CancelTurn(CanonicalBackendContract):
    operation_id: str
    attempt_id: str
    channel_id: str
    turn_id: str
    reason_code: str
    command_type = "cancel_turn"

    def __post_init__(self) -> None:
        for field_name in (
            "operation_id",
            "attempt_id",
            "channel_id",
            "turn_id",
            "reason_code",
        ):
            object.__setattr__(
                self, field_name, _token(getattr(self, field_name), field_name)
            )

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "channel_id": self.channel_id,
            "command_type": self.command_type,
            "operation_id": self.operation_id,
            "reason_code": self.reason_code,
            "schema_version": BACKEND_PORT_SCHEMA_VERSION,
            "turn_id": self.turn_id,
        }


class TurnState(StrEnum):
    ABSENT = "absent"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def _observation_common(
    operation_id: object,
    status: object,
    observed_at: object,
    effect_digest: object,
    evidence: object,
) -> tuple[str, EffectStatus, str, str | None, tuple[str, ...]]:
    normalized_id = _token(operation_id, "operation_id")
    if type(status) is not EffectStatus:
        raise TypeError("status must be an EffectStatus")
    normalized_time = _timestamp(observed_at, "observed_at")
    normalized_digest = _optional_digest(effect_digest, "effect_digest")
    normalized_evidence = _evidence(evidence)
    if status is EffectStatus.ABSENT and normalized_digest is not None:
        raise ValueError("an absent observation cannot claim an effect digest")
    if status is EffectStatus.APPLIED and normalized_digest is None:
        raise ValueError("an applied observation requires an effect digest")
    if status is EffectStatus.UNKNOWN and not normalized_evidence:
        raise ValueError("an unknown observation requires evidence")
    return (
        normalized_id,
        status,
        normalized_time,
        normalized_digest,
        normalized_evidence,
    )


@dataclass(frozen=True, slots=True)
class ChannelObservation(CanonicalBackendContract):
    operation_id: str
    status: EffectStatus
    observed_at: str
    effect_digest: str | None = None
    attempt_id: str | None = None
    channel_id: str | None = None
    provider: WorkerProvider | None = None
    provider_session_id: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = _observation_common(
            self.operation_id,
            self.status,
            self.observed_at,
            self.effect_digest,
            self.evidence,
        )
        for field_name, value in zip(
            ("operation_id", "status", "observed_at", "effect_digest", "evidence"),
            values,
            strict=True,
        ):
            object.__setattr__(self, field_name, value)
        for field_name in ("attempt_id", "channel_id", "provider_session_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _token(value, field_name))
        if self.provider is not None and type(self.provider) is not WorkerProvider:
            raise TypeError("provider must be a WorkerProvider or null")
        facts = (
            self.attempt_id,
            self.channel_id,
            self.provider,
            self.provider_session_id,
        )
        if self.status is EffectStatus.APPLIED and any(value is None for value in facts):
            raise ValueError("an applied channel reservation requires complete identities")
        if self.status is EffectStatus.ABSENT and any(value is not None for value in facts):
            raise ValueError("an absent channel observation cannot claim identities")

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "channel_id": self.channel_id,
            "effect_digest": self.effect_digest,
            "evidence": list(self.evidence),
            "observed_at": self.observed_at,
            "operation_id": self.operation_id,
            "provider": None if self.provider is None else self.provider.value,
            "provider_session_id": self.provider_session_id,
            "schema_version": BACKEND_PORT_SCHEMA_VERSION,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class TurnObservation(CanonicalBackendContract):
    operation_id: str
    status: EffectStatus
    observed_at: str
    state: TurnState
    effect_digest: str | None = None
    attempt_id: str | None = None
    channel_id: str | None = None
    message_id: str | None = None
    turn_id: str | None = None
    result_digest: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = _observation_common(
            self.operation_id,
            self.status,
            self.observed_at,
            self.effect_digest,
            self.evidence,
        )
        for field_name, value in zip(
            ("operation_id", "status", "observed_at", "effect_digest", "evidence"),
            values,
            strict=True,
        ):
            object.__setattr__(self, field_name, value)
        if type(self.state) is not TurnState:
            raise TypeError("state must be a TurnState")
        for field_name in ("attempt_id", "channel_id", "message_id", "turn_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _token(value, field_name))
        object.__setattr__(
            self, "result_digest", _optional_digest(self.result_digest, "result_digest")
        )
        identities = (self.attempt_id, self.channel_id, self.message_id, self.turn_id)
        if self.status is EffectStatus.ABSENT:
            if self.state is not TurnState.ABSENT or any(
                value is not None for value in (*identities, self.result_digest)
            ):
                raise ValueError("an absent turn observation cannot claim turn state")
        elif self.status is EffectStatus.UNKNOWN:
            if self.state is not TurnState.UNKNOWN:
                raise ValueError("an unknown turn observation requires unknown state")
        elif self.state in {TurnState.ABSENT, TurnState.UNKNOWN}:
            raise ValueError("an applied turn requires a concrete state")
        elif any(value is None for value in identities):
            raise ValueError("an applied turn requires complete identities")
        if self.state is TurnState.DONE and self.result_digest is None:
            raise ValueError("a completed turn requires result_digest")

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "channel_id": self.channel_id,
            "effect_digest": self.effect_digest,
            "evidence": list(self.evidence),
            "message_id": self.message_id,
            "observed_at": self.observed_at,
            "operation_id": self.operation_id,
            "result_digest": self.result_digest,
            "schema_version": BACKEND_PORT_SCHEMA_VERSION,
            "state": self.state.value,
            "status": self.status.value,
            "turn_id": self.turn_id,
        }


@runtime_checkable
class BackendChannelPort(Protocol):
    def probe(self) -> BackendCapabilities: ...

    def reserve(
        self, effect: PreparedEffect[ReserveChannel]
    ) -> ChannelObservation: ...

    def send(self, effect: PreparedEffect[SendTaskPacket]) -> TurnObservation: ...

    def inspect_reservation(self, operation_id: str) -> ChannelObservation: ...

    def inspect_turn(self, operation_id: str) -> TurnObservation: ...

    def cancel(self, effect: PreparedEffect[CancelTurn]) -> TurnObservation: ...


__all__ = [
    "BACKEND_PORT_SCHEMA_VERSION",
    "MAX_BACKEND_EVIDENCE",
    "MAX_TASK_PACKET_BYTES",
    "BackendCapabilities",
    "BackendChannelPort",
    "CancelTurn",
    "CanonicalBackendContract",
    "ChannelObservation",
    "ReserveChannel",
    "SendTaskPacket",
    "TurnObservation",
    "TurnState",
]
