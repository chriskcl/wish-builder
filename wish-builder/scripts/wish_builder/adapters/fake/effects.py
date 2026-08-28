"""Durable local fake ports used by the active-M1 recovery workflow."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from wish_builder.contracts import (
    DEFAULT_DECODE_LIMITS,
    canonical_json_bytes,
    canonical_sha256,
)
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectReceipt,
    EffectReceiptValue,
    EffectStatus,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    JournalEventType,
    OperationOutcome,
    OutcomeKind,
)
from wish_builder.contracts.runtime_decoder import decode_effect_receipt_bytes
from wish_builder.services.ports import PersistedEffectRequest


class FakeEffectCrash(RuntimeError):
    """Deliberate crash boundary used by subprocess recovery tests."""


class FakeEffectFailpoint(Protocol):
    def __call__(self, point: str, path: Path) -> None:
        """Raise at a named boundary; FakeEffectCrash is allowed to escape."""


class _ReadState(StrEnum):
    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class _ReceiptRead:
    state: _ReadState
    receipt: EffectReceipt | None
    raw: bytes


@dataclass(frozen=True, slots=True)
class _PortContract:
    adapter: AdapterKind
    operations: frozenset[EffectOperation]
    event_types: dict[EffectOperation, frozenset[JournalEventType]]
    object_types: dict[EffectOperation, frozenset[EffectObjectType]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush_file_buffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


class FilesystemFakeEffectPort:
    """One fake external system with an independent durable receipt store."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        contract: _PortContract,
        *,
        clock: Callable[[], str] = _utc_now,
        failpoint: FakeEffectFailpoint | None = None,
    ) -> None:
        if type(contract) is not _PortContract:
            raise TypeError("contract must be a _PortContract")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.root = Path(root).expanduser().absolute() / contract.adapter.value
        self.effects = self.root / "effects"
        self.receipts = self.root / "receipts"
        self.lock_path = self.root / "effects.lock"
        self._contract = contract
        self._clock = clock
        self._failpoint = failpoint

    @property
    def adapter_kind(self) -> AdapterKind:
        return self._contract.adapter

    @property
    def operations(self) -> frozenset[EffectOperation]:
        return self._contract.operations

    def _trigger(self, point: str, path: Path) -> None:
        if self._failpoint is not None:
            self._failpoint(point, path)

    def _ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.effects.mkdir(exist_ok=True)
        self.receipts.mkdir(exist_ok=True)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self._ensure_layout()
        with self.lock_path.open("a+b", buffering=0) as handle:
            if os.name == "nt":
                import msvcrt

                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _key(identity: ExecutionIdentity) -> str:
        assert identity.correlation_id is not None
        return hashlib.sha256(identity.correlation_id.encode("ascii")).hexdigest()

    def _paths(self, identity: ExecutionIdentity) -> tuple[Path, Path]:
        key = self._key(identity)
        return self.effects / f"{key}.json", self.receipts / f"{key}.json"

    @staticmethod
    def _read(path: Path) -> _ReceiptRead:
        try:
            with path.open("rb") as handle:
                raw = handle.read(DEFAULT_DECODE_LIMITS.max_bytes + 1)
        except FileNotFoundError:
            return _ReceiptRead(_ReadState.ABSENT, None, b"")
        except OSError as exc:
            return _ReceiptRead(
                _ReadState.INVALID,
                None,
                f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace"),
            )
        if len(raw) > DEFAULT_DECODE_LIMITS.max_bytes:
            return _ReceiptRead(_ReadState.INVALID, None, raw)
        decoded = decode_effect_receipt_bytes(raw)
        if (
            not decoded.ok
            or decoded.value is None
            or decoded.value.canonical_json_bytes() != raw
        ):
            return _ReceiptRead(_ReadState.INVALID, None, raw)
        return _ReceiptRead(_ReadState.VALID, decoded.value, raw)

    def _write_atomic(self, path: Path, value: bytes) -> None:
        self._trigger("before_write", path)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            written = 0
            while written < len(value):
                count = os.write(descriptor, value[written:])
                if count <= 0:
                    raise OSError("short write while publishing fake effect")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if path.exists():
                raise FileExistsError(str(path))
            os.replace(temporary, path)
            _sync_directory(path.parent)
            self._trigger("after_write", path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _evidence(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
        raw: bytes,
        observed_at: str,
    ) -> EvidenceRef:
        subject = canonical_json_bytes(
            {
                "adapter": self.adapter_kind.value,
                "identity": identity.to_primitive(),
                "operation": operation.value,
            }
        )
        return EvidenceRef(
            1,
            "sha256:" + hashlib.sha256(raw or b"unknown").hexdigest(),
            len(raw),
            EvidenceType.EFFECT_RECEIPT,
            EvidenceProducer(
                identity,
                external_object_id=f"fake-{self.adapter_kind.value}-receipt-store",
            ),
            observed_at,
            EvidenceSensitivity.INTERNAL,
            EvidenceRenderPolicy.METADATA_ONLY,
            EvidenceRole.REQUIRED,
            "sha256:" + hashlib.sha256(subject).hexdigest(),
        )

    def _receipt_outcome(self, receipt: EffectReceipt) -> OperationOutcome:
        return OperationOutcome(
            1,
            OutcomeKind.SUCCESS,
            value=EffectReceiptValue(receipt),
        )

    def _unknown(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
        *raw_values: bytes,
    ) -> OperationOutcome:
        observed_at = self._clock()
        raw = b"\0".join(raw_values)
        evidence = self._evidence(identity, operation, raw, observed_at)
        return self._receipt_outcome(
            EffectReceipt(
                1,
                identity,
                operation,
                EffectStatus.UNKNOWN,
                observed_at,
                evidence=(evidence,),
            )
        )

    def _absent(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> OperationOutcome:
        return self._receipt_outcome(
            EffectReceipt(
                1,
                identity,
                operation,
                EffectStatus.ABSENT,
                self._clock(),
            )
        )

    @staticmethod
    def _matches(
        receipt: EffectReceipt,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> bool:
        return receipt.identity == identity and receipt.operation is operation

    def _observe_locked(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
        effect_path: Path,
        receipt_path: Path,
    ) -> OperationOutcome:
        effect = self._read(effect_path)
        receipt = self._read(receipt_path)
        if effect.state is _ReadState.ABSENT:
            if receipt.state is _ReadState.ABSENT:
                return self._absent(identity, operation)
            return self._unknown(identity, operation, effect.raw, receipt.raw)
        if (
            effect.state is not _ReadState.VALID
            or effect.receipt is None
            or not self._matches(effect.receipt, identity, operation)
        ):
            return self._unknown(identity, operation, effect.raw, receipt.raw)
        if receipt.state is _ReadState.VALID:
            if receipt.receipt != effect.receipt:
                return self._unknown(identity, operation, effect.raw, receipt.raw)
        elif receipt.state is _ReadState.ABSENT:
            try:
                self._write_atomic(receipt_path, effect.raw)
            except OSError:
                pass
        else:
            return self._unknown(identity, operation, effect.raw, receipt.raw)
        return self._receipt_outcome(effect.receipt)

    @staticmethod
    def _validate_identity(identity: ExecutionIdentity) -> None:
        if type(identity) is not ExecutionIdentity or not identity.is_attempt:
            raise ValueError("fake effect lookup requires complete attempt identity")
        if identity.correlation_id is None:
            raise ValueError("fake effect lookup requires correlation identity")

    def lookup(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> OperationOutcome:
        self._validate_identity(identity)
        if type(operation) is not EffectOperation:
            raise TypeError("operation must be an EffectOperation")
        if operation not in self.operations:
            raise ValueError("operation is not supported by this fake port")
        effect_path, receipt_path = self._paths(identity)
        try:
            with self._lock():
                return self._observe_locked(
                    identity,
                    operation,
                    effect_path,
                    receipt_path,
                )
        except OSError as exc:
            return self._unknown(
                identity,
                operation,
                f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace"),
            )

    def _validate_request(self, request: PersistedEffectRequest) -> None:
        if type(request) is not PersistedEffectRequest:
            raise TypeError("request must be a PersistedEffectRequest")
        payload = request.payload
        if payload.adapter is not self.adapter_kind:
            raise ValueError("effect request targets another adapter")
        if payload.operation not in self.operations:
            raise ValueError("effect operation is not supported by this fake port")
        if (
            request.event.event_type
            not in self._contract.event_types[payload.operation]
        ):
            raise ValueError("effect request event_type does not match its operation")
        if payload.object_type not in self._contract.object_types[payload.operation]:
            raise ValueError("effect object_type does not match its operation")

    def _expected_receipt(self, request: PersistedEffectRequest) -> EffectReceipt:
        payload = request.payload
        effect_hash = "sha256:" + canonical_sha256(
            {
                "adapter": self.adapter_kind.value,
                "correlation_id": request.identity.correlation_id,
                "event_hash": request.event.event_hash,
                "operation": payload.operation.value,
                "request_payload_hash": payload.request_payload_hash,
            }
        )
        return EffectReceipt(
            1,
            request.identity,
            payload.operation,
            EffectStatus.APPLIED,
            self._clock(),
            effect_hash=effect_hash,
            external_object_id=(
                f"fake-{self.adapter_kind.value}-{effect_hash.removeprefix('sha256:')[:24]}"
            ),
        )

    def apply(self, request: PersistedEffectRequest) -> OperationOutcome:
        self._validate_request(request)
        expected = self._expected_receipt(request)
        effect_path, receipt_path = self._paths(request.identity)
        try:
            with self._lock():
                effect = self._read(effect_path)
                receipt = self._read(receipt_path)
                if effect.state is not _ReadState.ABSENT:
                    observed = self._observe_locked(
                        request.identity,
                        request.payload.operation,
                        effect_path,
                        receipt_path,
                    )
                    value = observed.value
                    if (
                        type(value) is EffectReceiptValue
                        and value.receipt.status is EffectStatus.APPLIED
                        and value.receipt.effect_hash != expected.effect_hash
                    ):
                        return self._unknown(
                            request.identity,
                            request.payload.operation,
                            effect.raw,
                            receipt.raw,
                        )
                    return observed
                if receipt.state is not _ReadState.ABSENT:
                    return self._unknown(
                        request.identity,
                        request.payload.operation,
                        effect.raw,
                        receipt.raw,
                    )

                self._trigger("before_effect", effect_path)
                self._write_atomic(effect_path, expected.canonical_json_bytes())
                self._trigger("after_effect_before_receipt", effect_path)
                self._write_atomic(receipt_path, expected.canonical_json_bytes())
                self._trigger("after_receipt", receipt_path)
                return self._receipt_outcome(expected)
        except FakeEffectCrash:
            raise
        except OSError as exc:
            observed = self.lookup(request.identity, request.payload.operation)
            value = observed.value
            if (
                type(value) is EffectReceiptValue
                and value.receipt.status is EffectStatus.APPLIED
                and value.receipt.effect_hash == expected.effect_hash
            ):
                return observed
            return self._unknown(
                request.identity,
                request.payload.operation,
                f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace"),
            )


_TASK_CONTRACT = _PortContract(
    AdapterKind.TASK,
    frozenset({EffectOperation.TASK_EXECUTION, EffectOperation.WORKER_DISPATCH}),
    {
        EffectOperation.TASK_EXECUTION: frozenset({JournalEventType.EFFECT_REQUESTED}),
        EffectOperation.WORKER_DISPATCH: frozenset(
            {JournalEventType.DISPATCH_REQUESTED}
        ),
    },
    {
        EffectOperation.TASK_EXECUTION: frozenset({EffectObjectType.WORKER}),
        EffectOperation.WORKER_DISPATCH: frozenset({EffectObjectType.WORKER}),
    },
)

_MODEL_CONTRACT = _PortContract(
    AdapterKind.MODEL,
    frozenset({EffectOperation.MODEL_INFERENCE}),
    {EffectOperation.MODEL_INFERENCE: frozenset({JournalEventType.EFFECT_REQUESTED})},
    {EffectOperation.MODEL_INFERENCE: frozenset({EffectObjectType.RESULT_BUNDLE})},
)

_REPOSITORY_CONTRACT = _PortContract(
    AdapterKind.REPOSITORY,
    frozenset(
        {
            EffectOperation.REPOSITORY_UPDATE,
            EffectOperation.RESULT_STAGE,
            EffectOperation.RESULT_PROMOTION,
        }
    ),
    {
        EffectOperation.REPOSITORY_UPDATE: frozenset(
            {JournalEventType.EFFECT_REQUESTED}
        ),
        EffectOperation.RESULT_STAGE: frozenset({JournalEventType.EFFECT_REQUESTED}),
        EffectOperation.RESULT_PROMOTION: frozenset(
            {JournalEventType.PROMOTION_REQUESTED}
        ),
    },
    {
        EffectOperation.REPOSITORY_UPDATE: frozenset(
            {EffectObjectType.WORKTREE, EffectObjectType.GIT_REF}
        ),
        EffectOperation.RESULT_STAGE: frozenset({EffectObjectType.RESULT_BUNDLE}),
        EffectOperation.RESULT_PROMOTION: frozenset({EffectObjectType.GIT_REF}),
    },
)


class FakeTaskPort(FilesystemFakeEffectPort):
    def __init__(self, root: str | os.PathLike[str], **kwargs: object) -> None:
        super().__init__(root, _TASK_CONTRACT, **kwargs)  # type: ignore[arg-type]


class FakeModelPort(FilesystemFakeEffectPort):
    def __init__(self, root: str | os.PathLike[str], **kwargs: object) -> None:
        super().__init__(root, _MODEL_CONTRACT, **kwargs)  # type: ignore[arg-type]


class FakeRepositoryPort(FilesystemFakeEffectPort):
    def __init__(self, root: str | os.PathLike[str], **kwargs: object) -> None:
        super().__init__(root, _REPOSITORY_CONTRACT, **kwargs)  # type: ignore[arg-type]


__all__ = [
    "FakeEffectCrash",
    "FakeEffectFailpoint",
    "FakeModelPort",
    "FakeRepositoryPort",
    "FakeTaskPort",
    "FilesystemFakeEffectPort",
]
