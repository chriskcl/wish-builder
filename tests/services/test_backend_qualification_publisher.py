from __future__ import annotations

import dataclasses
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import publish_backend_qualification as publication_cli

from tests.services.test_backend_qualification_builder import (
    SOURCE_REVISION,
    _EvidenceOptions,
    _write_evidence_root,
)
from wish_builder.compatibility import load_bundled_backend_qualification
from wish_builder.contracts import canonical_json_bytes, canonical_sha256
from wish_builder.contracts.compatibility import (
    BackendQualificationBundle,
    Platform,
    Provider,
)
from wish_builder.contracts.compatibility_decoder import (
    decode_backend_qualification_bundle_bytes,
)
from wish_builder.contracts.qualification_evidence import (
    QualificationEvidenceArtifact,
    QualificationEvidenceRole,
    QualificationProvenance,
    QualificationProvenanceKind,
)
from wish_builder.contracts.qualification_evidence_decoder import (
    decode_qualification_evidence_inventory_bytes,
    decode_qualification_provenance_bytes,
)
from wish_builder.services.backend_qualification_builder import (
    build_backend_qualification_candidate,
)
from wish_builder.services.backend_qualification_publisher import (
    BackendQualificationPublicationError,
    prepare_backend_qualification_publication,
    publish_backend_qualification,
    qualification_pin_module_bytes,
)


def _disabled_bundle() -> BackendQualificationBundle:
    primitive = load_bundled_backend_qualification().to_primitive()
    primitive["published"] = False
    for provider in primitive["providers"]:
        if provider["provider"] != Provider.CODEX.value:
            continue
        for cell in provider["platforms"]:
            if cell["platform"] == Platform.WINDOWS.value:
                cell["qualification"] = {
                    "artifact": None,
                    "enabledForDispatch": False,
                    "evidence": ["fixture:codex-app-server", "ci:windows-required"],
                    "evidenceScope": "deterministic_fixture_and_ci",
                    "live": False,
                    "note": (
                        "The pinned Codex 0.149.0 adapter still requires a full "
                        "live qualification run on this platform."
                    ),
                    "status": "fixture_ci_only",
                }
    primitive.pop("bundleDigest", None)
    primitive["bundleDigest"] = "sha256:" + canonical_sha256(primitive)
    decoded = decode_backend_qualification_bundle_bytes(canonical_json_bytes(primitive))
    if not decoded.ok or decoded.value is None:  # pragma: no cover - fixture guard
        raise AssertionError(decoded.report.render_text())
    return decoded.value


def _convert_to_provider_provenance(evidence: Path) -> None:
    provenance_path = evidence / "provenance.json"
    inventory_path = evidence / "inventory.json"
    provenance_result = decode_qualification_provenance_bytes(provenance_path.read_bytes())
    inventory_result = decode_qualification_evidence_inventory_bytes(
        inventory_path.read_bytes()
    )
    assert provenance_result.ok and provenance_result.value is not None
    assert inventory_result.ok and inventory_result.value is not None
    current = provenance_result.value
    provenance = QualificationProvenance(
        schema_version=current.schema_version,
        kind=QualificationProvenanceKind.PROVIDER,
        issuer="https://provider.example.invalid",
        reference="https://provider.example.invalid/local-run",
        identity="local-test-provider",
        source_revision=current.source_revision,
        subjects=current.subjects,
    )
    raw = provenance.canonical_json_bytes()
    artifacts = []
    for artifact in inventory_result.value.artifacts:
        if artifact.role is QualificationEvidenceRole.PROVENANCE:
            artifacts.append(
                QualificationEvidenceArtifact(
                    role=artifact.role,
                    path=artifact.path,
                    digest=provenance.digest(),
                    byte_length=len(raw),
                    media_type=artifact.media_type,
                )
            )
        else:
            artifacts.append(artifact)
    inventory = dataclasses.replace(
        inventory_result.value,
        artifacts=tuple(artifacts),
    )
    provenance_path.write_bytes(raw)
    inventory_path.write_bytes(inventory.canonical_json_bytes())


class BackendQualificationPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.base_bundle = _disabled_bundle()
        evidence = self.root / "raw"
        _write_evidence_root(evidence)
        _convert_to_provider_provenance(evidence)
        self.candidate_root = self.root / "candidate"
        self.candidate = build_backend_qualification_candidate(
            evidence,
            self.candidate_root,
            bundle=self.base_bundle,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def publication(self, **overrides: object):
        values: dict[str, object] = {
            "expected_source_revision": SOURCE_REVISION,
            "expected_artifact_digest": self.candidate.artifact.artifact_digest,
            "reviewer": "independent-test-reviewer",
            "review_reference": "local-review:publisher-tests",
            "human_approver": "local-test-user",
            "human_approval_reference": "local-approval:publisher-tests",
            "review_test_count": 52,
            "review_skip_count": 1,
            "accept_detached_provider_provenance": True,
            "base_bundle": self.base_bundle,
        }
        values.update(overrides)
        return prepare_backend_qualification_publication(
            self.candidate_root,
            **values,
        )

    def test_publication_is_canonical_scoped_and_idempotent(self) -> None:
        publication = self.publication()
        self.assertEqual(
            "compatibility/q/"
            + self.candidate.artifact.artifact_digest.removeprefix("sha256:")[:32],
            publication.evidence_relative,
        )
        record = (self.root / "compatibility" / "record.json").resolve()
        pin = (self.root / "compatibility" / "pin.py").resolve()
        evidence = (self.root / "compatibility" / "published-evidence").resolve()
        record.parent.mkdir(parents=True)
        record.write_bytes(self.base_bundle.canonical_json_bytes())
        pin.write_bytes(
            qualification_pin_module_bytes("0.6.15", self.base_bundle.bundle_digest)
        )

        self.assertTrue(
            publish_backend_qualification(
                publication,
                record_path=record,
                pin_path=pin,
                evidence_root=evidence,
                base_bundle=self.base_bundle,
            )
        )
        decoded = decode_backend_qualification_bundle_bytes(record.read_bytes())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None
        self.assertTrue(decoded.value.published)
        target = decoded.value.platform(Provider.CODEX, Platform.WINDOWS)
        self.assertTrue(target.qualification.enabled_for_dispatch)
        self.assertEqual(
            self.candidate.artifact,
            target.qualification.artifact,
        )
        for provider in self.base_bundle.providers:
            for base_cell in provider.platforms:
                if (provider.provider, base_cell.platform) == (
                    Provider.CODEX,
                    Platform.WINDOWS,
                ):
                    continue
                self.assertEqual(
                    base_cell,
                    decoded.value.platform(provider.provider, base_cell.platform),
                )
        self.assertFalse(
            publish_backend_qualification(
                publication,
                record_path=record,
                pin_path=pin,
                evidence_root=evidence,
                base_bundle=self.base_bundle,
            )
        )

    def test_publication_accepts_a_valid_omp_linux_cell(self) -> None:
        evidence = self.root / "omp-linux-raw"
        _write_evidence_root(
            evidence,
            _EvidenceOptions(
                target_provider=Provider.OMP,
                target_platform=Platform.LINUX,
                inventory_provider=Provider.OMP,
                inventory_platform=Platform.LINUX,
                event_provider=Provider.OMP,
                event_platform=Platform.LINUX,
            ),
        )
        _convert_to_provider_provenance(evidence)
        candidate_root = self.root / "omp-linux-candidate"
        candidate = build_backend_qualification_candidate(
            evidence,
            candidate_root,
            bundle=self.base_bundle,
        )

        publication = prepare_backend_qualification_publication(
            candidate_root,
            expected_source_revision=SOURCE_REVISION,
            expected_artifact_digest=candidate.artifact.artifact_digest,
            reviewer="independent-test-reviewer",
            review_reference="local-review:omp-linux-publisher-test",
            human_approver="local-test-user",
            human_approval_reference="local-approval:omp-linux-publisher-test",
            review_test_count=1,
            review_skip_count=0,
            accept_detached_provider_provenance=True,
            base_bundle=self.base_bundle,
        )

        self.assertIs(publication.provider, Provider.OMP)
        self.assertIs(publication.platform, Platform.LINUX)
        target = publication.bundle.platform(Provider.OMP, Platform.LINUX)
        self.assertTrue(target.qualification.enabled_for_dispatch)
        self.assertEqual(candidate.artifact, target.qualification.artifact)

    def test_detached_provenance_and_approved_identity_fail_closed(self) -> None:
        with self.assertRaises(BackendQualificationPublicationError) as detached:
            self.publication(accept_detached_provider_provenance=False)
        self.assertEqual("detached_provenance_not_accepted", detached.exception.code)

        with self.assertRaises(BackendQualificationPublicationError) as revision:
            self.publication(expected_source_revision="f" * 40)
        self.assertEqual("source_revision_mismatch", revision.exception.code)

        with self.assertRaises(BackendQualificationPublicationError) as digest:
            self.publication(expected_artifact_digest="sha256:" + "f" * 64)
        self.assertEqual("artifact_digest_mismatch", digest.exception.code)

    def test_candidate_tamper_and_record_or_pin_drift_fail_closed(self) -> None:
        artifact_path = self.candidate_root / "candidate-artifact.json"
        original = artifact_path.read_bytes()
        artifact_path.write_bytes(original + b"\n")
        with self.assertRaises(BackendQualificationPublicationError) as tamper:
            self.publication()
        self.assertEqual("candidate_bytes_mismatch", tamper.exception.code)
        artifact_path.write_bytes(original)

        publication = self.publication()
        record = (self.root / "drift" / "record.json").resolve()
        pin = (self.root / "drift" / "pin.py").resolve()
        evidence = (self.root / "drift" / "evidence").resolve()
        record.parent.mkdir(parents=True)
        record.write_bytes(self.base_bundle.canonical_json_bytes() + b"\n")
        pin.write_bytes(
            qualification_pin_module_bytes("0.6.15", self.base_bundle.bundle_digest)
        )
        with self.assertRaises(BackendQualificationPublicationError) as record_drift:
            publish_backend_qualification(
                publication,
                record_path=record,
                pin_path=pin,
                evidence_root=evidence,
                base_bundle=self.base_bundle,
            )
        self.assertEqual("record_drift", record_drift.exception.code)

        record.write_bytes(self.base_bundle.canonical_json_bytes())
        pin.write_bytes(b"not the compiled pin\n")
        with self.assertRaises(BackendQualificationPublicationError) as pin_drift:
            publish_backend_qualification(
                publication,
                record_path=record,
                pin_path=pin,
                evidence_root=evidence,
                base_bundle=self.base_bundle,
            )
        self.assertEqual("pin_drift", pin_drift.exception.code)

    def test_partial_publication_rolls_back_and_can_be_retried(self) -> None:
        publication = self.publication()
        base_record = self.base_bundle.canonical_json_bytes()
        base_pin = qualification_pin_module_bytes(
            "0.6.15", self.base_bundle.bundle_digest
        )
        real_replace = os.replace

        for failure_point in (2, 3):
            with self.subTest(failure_point=failure_point):
                root = self.root / f"rollback-{failure_point}"
                record = (root / "record.json").resolve()
                pin = (root / "pin.py").resolve()
                evidence = (root / "evidence").resolve()
                record.parent.mkdir(parents=True)
                record.write_bytes(base_record)
                pin.write_bytes(base_pin)
                replace_count = 0

                def fail_during_publication(
                    source: object,
                    destination: object,
                ) -> None:
                    nonlocal replace_count
                    replace_count += 1
                    if replace_count == failure_point:
                        raise OSError(
                            f"injected replacement failure {failure_point}"
                        )
                    real_replace(source, destination)

                with mock.patch(
                    "wish_builder.services.backend_qualification_publisher.os.replace",
                    side_effect=fail_during_publication,
                ):
                    with self.assertRaises(
                        BackendQualificationPublicationError
                    ) as failure:
                        publish_backend_qualification(
                            publication,
                            record_path=record,
                            pin_path=pin,
                            evidence_root=evidence,
                            base_bundle=self.base_bundle,
                        )
                self.assertEqual("publication_write_failed", failure.exception.code)
                self.assertEqual(base_record, record.read_bytes())
                self.assertEqual(base_pin, pin.read_bytes())
                self.assertFalse(evidence.exists())

                self.assertTrue(
                    publish_backend_qualification(
                        publication,
                        record_path=record,
                        pin_path=pin,
                        evidence_root=evidence,
                        base_bundle=self.base_bundle,
                    )
                )

    def test_conflicting_existing_evidence_is_never_overwritten(self) -> None:
        publication = self.publication()
        record = (self.root / "conflict" / "record.json").resolve()
        pin = (self.root / "conflict" / "pin.py").resolve()
        evidence = (self.root / "conflict" / "evidence").resolve()
        record.parent.mkdir(parents=True)
        record.write_bytes(self.base_bundle.canonical_json_bytes())
        pin.write_bytes(
            qualification_pin_module_bytes("0.6.15", self.base_bundle.bundle_digest)
        )
        evidence.mkdir(parents=True)
        marker = evidence / "unrelated.json"
        marker.write_bytes(b"{}\n")

        with self.assertRaises(BackendQualificationPublicationError) as conflict:
            publish_backend_qualification(
                publication,
                record_path=record,
                pin_path=pin,
                evidence_root=evidence,
                base_bundle=self.base_bundle,
            )
        self.assertEqual("evidence_output_exists", conflict.exception.code)
        self.assertEqual(b"{}\n", marker.read_bytes())

    def test_cli_reports_publish_replay_and_failure_states(self) -> None:
        publication = self.publication()
        arguments = [
            str(self.candidate_root),
            "--expected-source-revision",
            SOURCE_REVISION,
            "--expected-artifact-digest",
            self.candidate.artifact.artifact_digest,
            "--reviewer",
            "independent-test-reviewer",
            "--review-reference",
            "local-review:publisher-tests",
            "--human-approver",
            "local-test-user",
            "--human-approval-reference",
            "local-approval:publisher-tests",
            "--review-test-count",
            "52",
            "--review-skip-count",
            "1",
            "--accept-detached-provider-provenance",
            "--repository-root",
            str(self.root),
        ]
        for changed, state in ((True, "published"), (False, "already_published")):
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    publication_cli,
                    "load_bundled_backend_qualification",
                    return_value=self.base_bundle,
                ),
                mock.patch.object(
                    publication_cli,
                    "prepare_backend_qualification_publication",
                    return_value=publication,
                ),
                mock.patch.object(
                    publication_cli,
                    "publish_backend_qualification",
                    return_value=changed,
                ),
                mock.patch("sys.stdout", stdout),
            ):
                self.assertEqual(0, publication_cli.main(arguments))
            self.assertIn(f"publicationState={state}\n", stdout.getvalue())

        stderr = io.StringIO()
        with (
            mock.patch.object(
                publication_cli,
                "load_bundled_backend_qualification",
                side_effect=ValueError("bad qualification record"),
            ),
            mock.patch("sys.stderr", stderr),
        ):
            self.assertEqual(1, publication_cli.main(arguments))
        self.assertEqual("ERROR: bad qualification record\n", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
