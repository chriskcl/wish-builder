"""Fail-closed publication of independently reviewed backend evidence."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wish_builder.compatibility import load_bundled_backend_qualification
from wish_builder.contracts import canonical_json_bytes, canonical_sha256
from wish_builder.contracts.compatibility import (
    BackendQualificationBundle,
    EvidenceScope,
    Platform,
    Provider,
    QualificationStatus,
)
from wish_builder.contracts.compatibility_decoder import (
    decode_backend_qualification_bundle_bytes,
)
from wish_builder.contracts.qualification_evidence import (
    QualificationEvidenceRole,
    QualificationProvenanceKind,
)
from wish_builder.contracts.qualification_evidence_decoder import (
    decode_qualification_harness_descriptor_bytes,
    decode_qualification_provenance_bytes,
)
from wish_builder.services.backend_qualification_builder import (
    BackendQualificationCandidate,
    verify_backend_qualification_candidate,
)


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_PIN_HEADER = '"""Generated backend qualification trust pins."""\n\n'


class BackendQualificationPublicationError(ValueError):
    """Stable failure raised before an unsafe qualification publication."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class BackendQualificationPublication:
    """Canonical bytes prepared for one local publication."""

    bundle: BackendQualificationBundle
    bundle_bytes: bytes
    evidence_files: tuple[tuple[str, bytes], ...]
    evidence_relative: str
    pin_bytes: bytes
    platform: Platform
    provider: Provider
    receipt_bytes: bytes
    receipt_digest: str
    source_revision: str

    @property
    def report_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "bundleDigest": self.bundle.bundle_digest,
                "enabledForDispatch": True,
                "evidenceReference": self.evidence_relative,
                "platform": self.platform.value,
                "provenanceAssurance": (
                    "detached_provider_reference_human_accepted"
                ),
                "provider": self.provider.value,
                "publicationMode": "local",
                "published": True,
                "receiptDigest": self.receipt_digest,
                "schemaVersion": 1,
                "sourceRevision": self.source_revision,
            }
        )


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _required_text(value: str, field: str, limit: int = 1024) -> str:
    if type(value) is not str or not value or len(value) > limit:
        raise BackendQualificationPublicationError(
            "invalid_publication_input", f"{field} must be a non-empty bounded string."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BackendQualificationPublicationError(
            "invalid_publication_input", f"{field} contains control characters."
        )
    return value


def _read_regular(path: Path, code: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise BackendQualificationPublicationError(code, f"Unsafe or missing file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BackendQualificationPublicationError(code, str(exc)) from exc


def _decode_value(result: object, code: str) -> object:
    if not getattr(result, "ok", False) or getattr(result, "value", None) is None:
        report = getattr(result, "report", None)
        detail = report.render_text().rstrip() if report is not None else code
        raise BackendQualificationPublicationError(code, detail)
    return result.value


def _candidate_files(candidate: BackendQualificationCandidate) -> dict[str, bytes]:
    result = {
        "candidate-artifact.json": canonical_json_bytes(candidate.artifact.to_primitive()),
        "verification-report.json": candidate.report_bytes,
    }
    for relative, raw in candidate.evidence_objects:
        result[f"evidence/{relative}"] = raw
    for digest, raw in candidate.derived_objects:
        result[f"derived/sha256/{digest.removeprefix('sha256:')}.json"] = raw
    return result


def _verify_candidate_directory(
    root: Path,
    candidate: BackendQualificationCandidate,
) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        raise BackendQualificationPublicationError(
            "candidate_root_invalid", "Candidate root must be a real directory."
        )
    expected = _candidate_files(candidate)
    actual: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackendQualificationPublicationError(
                "candidate_symlink", "Candidate directories and files cannot be symlinks."
            )
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = path.read_bytes()
    if set(actual) != set(expected):
        raise BackendQualificationPublicationError(
            "candidate_file_set_mismatch",
            "Candidate files do not match the verifier-derived file set.",
        )
    mismatch = next(
        (relative for relative in sorted(expected) if actual[relative] != expected[relative]),
        None,
    )
    if mismatch is not None:
        raise BackendQualificationPublicationError(
            "candidate_bytes_mismatch",
            f"Candidate file differs from verifier output: {mismatch}",
        )
    return expected


def qualification_pin_module_bytes(version: str, digest: str) -> bytes:
    """Render the complete generated trust-pin module."""

    version = _required_text(version, "version", 64)
    if not _DIGEST.fullmatch(digest):
        raise BackendQualificationPublicationError(
            "invalid_publication_input", "digest must be a canonical SHA-256 value."
        )
    return (
        _PIN_HEADER
        + "BACKEND_QUALIFICATION_DIGESTS = {\n"
        + f'    "{version}": "{digest}",\n'
        + "}\n\n"
        + '__all__ = ["BACKEND_QUALIFICATION_DIGESTS"]\n'
    ).encode("ascii")


def _publication_receipt(
    *,
    candidate: BackendQualificationCandidate,
    source_revision: str,
    evidence_relative: str,
    provenance_reference: str,
    reviewer: str,
    review_reference: str,
    human_approver: str,
    human_approval_reference: str,
    review_test_count: int,
    review_skip_count: int,
) -> tuple[bytes, str]:
    if type(review_test_count) is not int or review_test_count < 1:
        raise BackendQualificationPublicationError(
            "invalid_publication_input", "review_test_count must be positive."
        )
    if type(review_skip_count) is not int or review_skip_count < 0:
        raise BackendQualificationPublicationError(
            "invalid_publication_input", "review_skip_count cannot be negative."
        )
    event_log_digest = candidate.inventory.artifact(
        QualificationEvidenceRole.EVENT_LOG
    ).digest
    body = {
        "candidateArtifactDigest": candidate.artifact.artifact_digest,
        "evidenceInventoryDigest": candidate.inventory.digest(),
        "evidenceReference": evidence_relative,
        "eventLogDigest": event_log_digest,
        "humanApprovalReference": human_approval_reference,
        "humanApprover": human_approver,
        "officialProviderAttestation": False,
        "platform": candidate.inventory.platform.value,
        "provenanceAssurance": "detached_provider_reference_human_accepted",
        "provenanceReference": provenance_reference,
        "provider": candidate.inventory.provider.value,
        "qualificationRunId": candidate.inventory.qualification_run_id,
        "reviewReference": review_reference,
        "reviewSkipCount": review_skip_count,
        "reviewTestCount": review_test_count,
        "reviewer": reviewer,
        "schemaVersion": 1,
        "sourceRevision": source_revision,
        "verificationMode": "local",
    }
    receipt_digest = "sha256:" + canonical_sha256(body)
    receipt = dict(body)
    receipt["receiptDigest"] = receipt_digest
    return canonical_json_bytes(receipt), receipt_digest


def prepare_backend_qualification_publication(
    candidate_root: Path,
    *,
    expected_source_revision: str,
    expected_artifact_digest: str,
    reviewer: str,
    review_reference: str,
    human_approver: str,
    human_approval_reference: str,
    review_test_count: int,
    review_skip_count: int,
    accept_detached_provider_provenance: bool,
    trellis_version: str = "0.6.15",
    base_bundle: BackendQualificationBundle | None = None,
) -> BackendQualificationPublication:
    """Reverify and prepare one exact backend/OS publication."""

    if not isinstance(candidate_root, Path):
        raise TypeError("candidate_root must be a Path")
    if not _REVISION.fullmatch(expected_source_revision):
        raise BackendQualificationPublicationError(
            "source_revision_invalid", "Expected source revision must be a full Git SHA."
        )
    if not _DIGEST.fullmatch(expected_artifact_digest):
        raise BackendQualificationPublicationError(
            "artifact_digest_invalid", "Expected artifact digest must be SHA-256."
        )
    reviewer = _required_text(reviewer, "reviewer")
    review_reference = _required_text(review_reference, "review_reference")
    human_approver = _required_text(human_approver, "human_approver")
    human_approval_reference = _required_text(
        human_approval_reference, "human_approval_reference"
    )
    if type(accept_detached_provider_provenance) is not bool:
        raise TypeError("accept_detached_provider_provenance must be a bool")
    selected_bundle = base_bundle or load_bundled_backend_qualification(trellis_version)
    if type(selected_bundle) is not BackendQualificationBundle:
        raise TypeError("base_bundle must be a BackendQualificationBundle or null")
    evidence_root = candidate_root / "evidence"
    candidate = verify_backend_qualification_candidate(
        evidence_root,
        bundle=selected_bundle,
    )
    files = _verify_candidate_directory(candidate_root, candidate)
    if candidate.artifact.artifact_digest != expected_artifact_digest:
        raise BackendQualificationPublicationError(
            "artifact_digest_mismatch", "Candidate artifact digest was not approved."
        )
    harness = _decode_value(
        decode_qualification_harness_descriptor_bytes(files["evidence/harness.json"]),
        "harness_invalid",
    )
    provenance = _decode_value(
        decode_qualification_provenance_bytes(files["evidence/provenance.json"]),
        "provenance_invalid",
    )
    if (
        harness.source_revision != expected_source_revision
        or provenance.source_revision != expected_source_revision
    ):
        raise BackendQualificationPublicationError(
            "source_revision_mismatch",
            "Harness and provenance must match the approved source revision.",
        )
    if provenance.kind is not QualificationProvenanceKind.PROVIDER:
        raise BackendQualificationPublicationError(
            "provenance_kind_mismatch", "This local publication requires provider provenance."
        )
    if not accept_detached_provider_provenance:
        raise BackendQualificationPublicationError(
            "detached_provenance_not_accepted",
            "Detached provider provenance requires explicit human acceptance.",
        )

    evidence_key = candidate.artifact.artifact_digest.removeprefix("sha256:")[:32]
    evidence_relative = f"compatibility/q/{evidence_key}"
    receipt_bytes, receipt_digest = _publication_receipt(
        candidate=candidate,
        source_revision=expected_source_revision,
        evidence_relative=evidence_relative,
        provenance_reference=provenance.reference,
        reviewer=reviewer,
        review_reference=review_reference,
        human_approver=human_approver,
        human_approval_reference=human_approval_reference,
        review_test_count=review_test_count,
        review_skip_count=review_skip_count,
    )
    published_files = dict(files)
    published_files["publication-receipt.json"] = receipt_bytes

    primitive = selected_bundle.to_primitive()
    target_key = (
        candidate.inventory.provider.value,
        candidate.inventory.platform.value,
    )
    target_label = f"{target_key[0]}/{target_key[1]}"
    original_cells: dict[tuple[str, str], object] = {}
    target: dict[str, object] | None = None
    for provider in primitive["providers"]:
        provider_name = provider["provider"]
        for cell in provider["platforms"]:
            key = (provider_name, cell["platform"])
            original_cells[key] = canonical_json_bytes(cell)
            if key == target_key:
                target = cell
    if target is None:
        raise BackendQualificationPublicationError(
            "target_cell_missing", f"{target_label} is absent from the base bundle."
        )
    desired_qualification = {
        "artifact": candidate.artifact.to_primitive(),
        "enabledForDispatch": True,
        "evidence": [
            f"bundled-evidence:{evidence_relative}",
            f"candidate-artifact:{candidate.artifact.artifact_digest}",
            f"evidence-inventory:{candidate.inventory.digest()}",
            (
                "event-log:"
                + candidate.inventory.artifact(QualificationEvidenceRole.EVENT_LOG).digest
            ),
            f"publication-receipt:{receipt_digest}",
        ],
        "evidenceScope": EvidenceScope.FULL_TURN_AND_CANCELLATION.value,
        "live": True,
        "note": (
            "Locally published from independently reviewed live "
            f"{candidate.artifact.sdk.name}@{candidate.artifact.sdk.version} evidence. "
            "Detached provider provenance was human-accepted; this is not a "
            "provider-signed attestation."
        ),
        "status": QualificationStatus.PASSED.value,
    }
    current_qualification = target["qualification"]
    if current_qualification.get("enabledForDispatch") is True:
        if current_qualification != desired_qualification or primitive["published"] is not True:
            raise BackendQualificationPublicationError(
                "cell_already_qualified",
                f"{target_label} is already qualified with different publication evidence.",
            )
    else:
        if current_qualification.get("artifact") is not None:
            raise BackendQualificationPublicationError(
                "disabled_cell_has_artifact", "Disabled target cell contains an artifact."
            )
        target["qualification"] = desired_qualification
    primitive["published"] = True
    primitive.pop("bundleDigest", None)
    primitive["bundleDigest"] = "sha256:" + canonical_sha256(primitive)
    bundle_bytes = canonical_json_bytes(primitive)
    decoded = decode_backend_qualification_bundle_bytes(bundle_bytes)
    if not decoded.ok or decoded.value is None:
        raise BackendQualificationPublicationError(
            "published_bundle_invalid", decoded.report.render_text().rstrip()
        )
    published_bundle = decoded.value
    for provider in published_bundle.to_primitive()["providers"]:
        provider_name = provider["provider"]
        for cell in provider["platforms"]:
            key = (provider_name, cell["platform"])
            if key != target_key:
                if canonical_json_bytes(cell) != original_cells[key]:
                    raise BackendQualificationPublicationError(
                        "unapproved_cell_changed", f"Publication changed {key[0]}/{key[1]}.",
                    )
    pin_bytes = qualification_pin_module_bytes(
        trellis_version, published_bundle.bundle_digest
    )
    return BackendQualificationPublication(
        bundle=published_bundle,
        bundle_bytes=bundle_bytes,
        evidence_files=tuple(sorted(published_files.items())),
        evidence_relative=evidence_relative,
        pin_bytes=pin_bytes,
        platform=candidate.inventory.platform,
        provider=candidate.inventory.provider,
        receipt_bytes=receipt_bytes,
        receipt_digest=receipt_digest,
        source_revision=expected_source_revision,
    )


def _write_fsync(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _read_evidence_tree(root: Path, code: str) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink():
        raise BackendQualificationPublicationError(
            code, "Publication evidence must be a real directory."
        )
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise BackendQualificationPublicationError(
                code, "Publication evidence cannot contain symlinks."
            )
        if path.is_file():
            files[path.relative_to(root).as_posix()] = _read_regular(path, code)
        elif not path.is_dir():
            raise BackendQualificationPublicationError(
                code, f"Publication evidence contains an unsafe path: {path}"
            )
    return files


def publish_backend_qualification(
    publication: BackendQualificationPublication,
    *,
    record_path: Path,
    pin_path: Path,
    evidence_root: Path,
    base_bundle: BackendQualificationBundle,
    trellis_version: str = "0.6.15",
) -> bool:
    """Publish evidence, bundle, and pin; return False for an exact replay."""

    if type(publication) is not BackendQualificationPublication:
        raise TypeError("publication must be a BackendQualificationPublication")
    if type(base_bundle) is not BackendQualificationBundle:
        raise TypeError("base_bundle must be a BackendQualificationBundle")
    for value, field in (
        (record_path, "record_path"),
        (pin_path, "pin_path"),
        (evidence_root, "evidence_root"),
    ):
        if not isinstance(value, Path) or not value.is_absolute():
            raise ValueError(f"{field} must be an absolute Path")
    expected_evidence = dict(publication.evidence_files)
    if record_path.exists() and pin_path.exists() and evidence_root.is_dir():
        current_files = _read_evidence_tree(evidence_root, "evidence_output_invalid")
        if (
            _read_regular(record_path, "record_invalid") == publication.bundle_bytes
            and _read_regular(pin_path, "pin_invalid") == publication.pin_bytes
            and current_files == expected_evidence
        ):
            return False
    if evidence_root.exists() or evidence_root.is_symlink():
        raise BackendQualificationPublicationError(
            "evidence_output_exists", "Publication evidence output already exists."
        )
    base_record_bytes = _read_regular(record_path, "record_invalid")
    if base_record_bytes != base_bundle.canonical_json_bytes():
        raise BackendQualificationPublicationError(
            "record_drift", "Bundled qualification bytes changed before publication."
        )
    expected_pin = qualification_pin_module_bytes(
        trellis_version, base_bundle.bundle_digest
    )
    base_pin_bytes = _read_regular(pin_path, "pin_invalid")
    if base_pin_bytes != expected_pin:
        raise BackendQualificationPublicationError(
            "pin_drift", "Compiled qualification pin changed before publication."
        )
    record_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".qualification-publish-", dir=record_path.parent))
    staged_evidence = temporary / "evidence"
    staged_record = temporary / "record.json"
    staged_pin = temporary / "pin.py"
    backup_record = temporary / "record.backup.json"
    backup_pin = temporary / "pin.backup.py"
    evidence_installed = False
    record_installed = False
    pin_installed = False
    try:
        for relative, raw in publication.evidence_files:
            _write_fsync(staged_evidence.joinpath(*relative.split("/")), raw)
        _write_fsync(staged_record, publication.bundle_bytes)
        _write_fsync(staged_pin, publication.pin_bytes)
        _write_fsync(backup_record, base_record_bytes)
        _write_fsync(backup_pin, base_pin_bytes)
        try:
            os.replace(staged_evidence, evidence_root)
            evidence_installed = True
            os.replace(staged_record, record_path)
            record_installed = True
            os.replace(staged_pin, pin_path)
            pin_installed = True
            if (
                _read_regular(record_path, "post_publish_record_invalid")
                != publication.bundle_bytes
                or _read_regular(pin_path, "post_publish_pin_invalid")
                != publication.pin_bytes
                or _read_evidence_tree(
                    evidence_root, "post_publish_evidence_invalid"
                )
                != expected_evidence
            ):
                raise BackendQualificationPublicationError(
                    "post_publish_mismatch",
                    "Published evidence, record, or pin does not match prepared bytes.",
                )
        except (BackendQualificationPublicationError, OSError) as exc:
            rollback_errors: list[str] = []
            if pin_installed:
                try:
                    os.replace(backup_pin, pin_path)
                except OSError as rollback_exc:
                    rollback_errors.append(f"pin: {rollback_exc}")
            if record_installed:
                try:
                    os.replace(backup_record, record_path)
                except OSError as rollback_exc:
                    rollback_errors.append(f"record: {rollback_exc}")
            if evidence_installed:
                try:
                    shutil.rmtree(evidence_root)
                except OSError as rollback_exc:
                    rollback_errors.append(f"evidence: {rollback_exc}")
            if rollback_errors:
                raise BackendQualificationPublicationError(
                    "publication_rollback_failed",
                    "Publication failed and rollback was incomplete: "
                    + "; ".join(rollback_errors),
                ) from exc
            if isinstance(exc, BackendQualificationPublicationError):
                raise
            raise BackendQualificationPublicationError(
                "publication_write_failed", str(exc)
            ) from exc
    except OSError as exc:
        raise BackendQualificationPublicationError(
            "publication_write_failed", str(exc)
        ) from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return True


__all__ = [
    "BackendQualificationPublication",
    "BackendQualificationPublicationError",
    "prepare_backend_qualification_publication",
    "publish_backend_qualification",
    "qualification_pin_module_bytes",
]
