"""Service-owned contracts for request-before-effect adapter calls."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar, runtime_checkable

from wish_builder.contracts import canonical_json_bytes
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectOperation,
    EffectRequestPayload,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    OperationOutcome,
)
from wish_builder.services.journal import AppendResult

_REQUEST_EVENT_TYPES = frozenset(
    {
        JournalEventType.DISPATCH_REQUESTED,
        JournalEventType.EFFECT_REQUESTED,
        JournalEventType.PROMOTION_REQUESTED,
        JournalEventType.CLEANUP_REQUESTED,
    }
)


@dataclass(frozen=True, slots=True)
class PersistedEffectRequest:
    """An effect request proven to be the exact event durably appended."""

    event: JournalEvent
    append_result: AppendResult

    def __post_init__(self) -> None:
        if type(self.event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent")
        if type(self.append_result) is not AppendResult:
            raise TypeError("append_result must be an AppendResult")
        if not self.append_result.durable or self.append_result.event != self.event:
            raise ValueError(
                "effect execution requires the exact durable request event"
            )
        if self.event.event_type not in _REQUEST_EVENT_TYPES:
            raise ValueError("event is not an effect request")
        if type(self.event.payload) is not EffectRequestPayload:
            raise TypeError("effect request event has the wrong payload type")
        if self.event.payload.expected_sequence != self.event.sequence - 1:
            raise ValueError("effect request expected_sequence is not journal-adjacent")
        if self.event.payload.fencing_token != self.event.identity.coordinator_epoch:
            raise ValueError("effect request fencing token does not match its epoch")

    @classmethod
    def from_append_result(cls, result: AppendResult) -> PersistedEffectRequest:
        if type(result) is not AppendResult:
            raise TypeError("result must be an AppendResult")
        if result.event is None:
            raise ValueError("append result does not contain a durable event")
        return cls(result.event, result)

    @property
    def payload(self) -> EffectRequestPayload:
        payload = self.event.payload
        assert type(payload) is EffectRequestPayload
        return payload

    @property
    def identity(self) -> ExecutionIdentity:
        return self.event.identity


@runtime_checkable
class PreparedCommand(Protocol):
    """Canonical typed command that can be bound to a durable request."""

    operation_id: str

    def to_primitive(self) -> dict[str, object]: ...


PreparedCommandT = TypeVar("PreparedCommandT", bound=PreparedCommand)


@dataclass(frozen=True, slots=True)
class PreparedEffect(Generic[PreparedCommandT]):
    """Exact durable request paired with the command its hash authorizes."""

    request: PersistedEffectRequest
    command: PreparedCommandT
    _operation_id: str = field(init=False, repr=False, compare=False)
    _command_bytes: bytes = field(init=False, repr=False, compare=False)
    _command_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.request) is not PersistedEffectRequest:
            raise TypeError("request must be a PersistedEffectRequest")
        if not isinstance(self.command, PreparedCommand):
            raise TypeError("command must be a canonical typed command")
        operation_id = self.command.operation_id
        if type(operation_id) is not str:
            raise TypeError("command operation_id must be a string")
        if operation_id != self.request.identity.correlation_id:
            raise ValueError(
                "command operation_id does not match the durable request identity"
            )
        command_bytes = canonical_json_bytes(self.command.to_primitive())
        command_hash = "sha256:" + hashlib.sha256(command_bytes).hexdigest()
        if command_hash != self.request.payload.request_payload_hash:
            raise ValueError(
                "command canonical hash does not match request_payload_hash"
            )
        object.__setattr__(self, "_operation_id", operation_id)
        object.__setattr__(self, "_command_bytes", command_bytes)
        object.__setattr__(self, "_command_hash", command_hash)

    def _verify_unchanged(self) -> None:
        if self.command.operation_id != self._operation_id:
            raise ValueError("prepared command operation_id changed after preparation")
        if canonical_json_bytes(self.command.to_primitive()) != self._command_bytes:
            raise ValueError("prepared command changed after preparation")

    @property
    def operation_id(self) -> str:
        self._verify_unchanged()
        return self._operation_id

    @property
    def command_hash(self) -> str:
        self._verify_unchanged()
        return self._command_hash

    @classmethod
    def from_append_result(
        cls,
        result: AppendResult,
        command: PreparedCommandT,
    ) -> PreparedEffect[PreparedCommandT]:
        return cls(PersistedEffectRequest.from_append_result(result), command)


@runtime_checkable
class EffectPort(Protocol):
    """Closed mutation/reconciliation surface shared by every fake port."""

    @property
    def adapter_kind(self) -> AdapterKind: ...

    @property
    def operations(self) -> frozenset[EffectOperation]: ...

    def apply(self, request: PersistedEffectRequest) -> OperationOutcome:
        """Apply or identify one durable request without blind retry."""

    def lookup(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> OperationOutcome:
        """Return an explicit absent, applied, or unknown effect receipt."""


@runtime_checkable
class TaskPort(EffectPort, Protocol):
    pass


@runtime_checkable
class ModelPort(EffectPort, Protocol):
    pass


@runtime_checkable
class RepositoryPort(EffectPort, Protocol):
    pass


__all__ = [
    "EffectPort",
    "ModelPort",
    "PreparedCommand",
    "PreparedEffect",
    "PersistedEffectRequest",
    "RepositoryPort",
    "TaskPort",
]
