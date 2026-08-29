"""Content-addressed local evidence for backend and Trellis observations."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from wish_builder.contracts import canonical_json_bytes
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectOperation,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
)
from wish_builder.services.ports import (
    AttemptObservation,
    ChannelObservation,
    CheckObservation,
    FinishObservation,
    TurnObservation,
)


ExternalEvidenceObservation = (
    AttemptObservation
    | ChannelObservation
    | CheckObservation
    | FinishObservation
    | TurnObservation
)
_EXTERNAL_EVIDENCE_OBSERVATION_TYPES = {
    AttemptObservation,
    ChannelObservation,
    CheckObservation,
    FinishObservation,
    TurnObservation,
}
_EXTERNAL_OPERATION_ADAPTERS = {
    EffectOperation.PREPARE_ATTEMPT: AdapterKind.TRELLIS,
    EffectOperation.RESERVE_CHANNEL: AdapterKind.BACKEND,
    EffectOperation.SEND_TASK_PACKET: AdapterKind.BACKEND,
    EffectOperation.CANCEL_TURN: AdapterKind.BACKEND,
    EffectOperation.CHECK_ATTEMPT: AdapterKind.TRELLIS,
    EffectOperation.FINISH_ATTEMPT: AdapterKind.TRELLIS,
}


class ExternalEvidenceStoreError(RuntimeError):
    """The observation bytes could not be durably stored or verified."""


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        # Python cannot fsync a Windows directory handle. Atomic publication is
        # still used; the surrounding protected control root owns volume probes.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FilesystemExternalEvidenceStore:
    """Store exact canonical observation bytes under their SHA-256 digest."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        path = Path(root).expanduser().absolute()
        if path == path.parent:
            raise ValueError("evidence root must not be a filesystem root")
        self.root = path
        self.objects = path / "objects" / "sha256"

    def put(
        self,
        observation: ExternalEvidenceObservation,
        *,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> EvidenceRef:
        raw, reference = self._reference(
            observation,
            identity=identity,
            operation=operation,
        )
        self.objects.mkdir(parents=True, exist_ok=True)
        target = self.objects / f"{reference.digest.removeprefix('sha256:')}.json"
        self._publish(target, raw)
        return reference

    def verify_existing(
        self,
        observation: ExternalEvidenceObservation,
        *,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> EvidenceRef:
        """Return the reference only when the exact object already exists."""

        raw, reference = self._reference(
            observation,
            identity=identity,
            operation=operation,
        )
        if self.read(reference.digest) != raw:
            raise ExternalEvidenceStoreError("external_evidence_content_mismatch")
        return reference

    @staticmethod
    def _reference(
        observation: ExternalEvidenceObservation,
        *,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> tuple[bytes, EvidenceRef]:
        if type(observation) not in _EXTERNAL_EVIDENCE_OBSERVATION_TYPES:
            raise TypeError("observation must be a typed external effect observation")
        if type(identity) is not ExecutionIdentity or not identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        if identity.correlation_id != observation.operation_id:
            raise ValueError("observation operation_id does not match identity")
        if type(operation) is not EffectOperation:
            raise TypeError("operation must be an EffectOperation")
        adapter = _EXTERNAL_OPERATION_ADAPTERS.get(operation)
        if adapter is None:
            raise ValueError("operation is not a supported external effect operation")

        raw = observation.canonical_json_bytes()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        subject = canonical_json_bytes(
            {
                "adapter": adapter.value,
                "identity": identity.to_primitive(),
                "operation": operation.value,
            }
        )
        return raw, EvidenceRef(
            1,
            digest,
            len(raw),
            EvidenceType.EFFECT_RECEIPT,
            EvidenceProducer(
                identity,
                external_object_id="external-observation-store",
            ),
            observation.observed_at,
            EvidenceSensitivity.INTERNAL,
            EvidenceRenderPolicy.METADATA_ONLY,
            EvidenceRole.REQUIRED,
            "sha256:" + hashlib.sha256(subject).hexdigest(),
        )

    def read(self, digest: str) -> bytes:
        if type(digest) is not str or not HASH_RE.fullmatch(digest):
            raise ValueError("digest must be a full sha256 reference")
        path = self.objects / f"{digest.removeprefix('sha256:')}.json"
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ExternalEvidenceStoreError("external_evidence_read_failed") from exc
        if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
            raise ExternalEvidenceStoreError("external_evidence_digest_mismatch")
        return raw

    @staticmethod
    def _publish(target: Path, raw: bytes) -> None:
        try:
            existing = target.read_bytes()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise ExternalEvidenceStoreError("external_evidence_read_failed") from exc
        if existing is not None:
            if existing != raw:
                raise ExternalEvidenceStoreError("external_evidence_collision")
            return

        # Keep the temporary name independent of the 64-character object name.
        # Long runtime roots otherwise cross legacy MAX_PATH on Windows before
        # the content-addressed object can be published.
        temporary = target.parent / f".tmp-{uuid.uuid4().hex}"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            written = 0
            while written < len(raw):
                count = os.write(descriptor, raw[written:])
                if count <= 0:
                    raise OSError("short write while publishing external evidence")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.read_bytes() != raw:
                    raise ExternalEvidenceStoreError("external_evidence_collision")
            _sync_directory(target.parent)
        except ExternalEvidenceStoreError:
            raise
        except OSError as exc:
            raise ExternalEvidenceStoreError("external_evidence_write_failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = [
    "FilesystemExternalEvidenceStore",
    "ExternalEvidenceStoreError",
]
