from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wish_builder.compatibility import (
    bundled_backend_version_registry_bytes,
    load_bundled_backend_version_registry,
)
from wish_builder.contracts.backend_registry import BackendVersionStatus
from wish_builder.contracts.backend_registry_decoder import (
    decode_backend_version_registry_bytes,
)
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.services.backend_version_registry import (
    BackendVersionRegistryUpdateError,
    backend_version_registry_pin_bytes,
    prepare_backend_version_candidate,
    prepare_backend_version_qualification,
    prepare_backend_version_quarantine,
    publish_backend_version_registry,
)
from wish_builder.services import backend_version_registry as registry_service


INTEGRITY = (
    "sha512-i4dryj2Y1j+00Mb5n+0n71EYnTK9/KDc2cdFo/dXD0d1oTog2bhUssKDEIOnKmn"
    "Ef51P0Z/HJTWvTKw/UHyOvQ=="
)


class BackendVersionRegistryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.record_path = self.root / "backend-version-registry.json"
        self.pin_path = self.root / "_backend_version_registry_pin.py"
        self.registry = load_bundled_backend_version_registry()
        self.record_path.write_bytes(bundled_backend_version_registry_bytes())
        self.pin_path.write_bytes(
            backend_version_registry_pin_bytes(self.registry.registry_digest)
        )

    def candidate(self, registry=None, version: str = "0.150.0"):
        selected = self.registry if registry is None else registry
        return prepare_backend_version_candidate(
            selected,
            expected_registry_digest=selected.registry_digest,
            provider=Provider.CODEX,
            platform=Platform.WINDOWS,
            backend_version=version,
            protocol_profile="codex-app-server-v1",
            package_shasum="a" * 40,
            package_integrity=INTEGRITY,
            note="Detected locally; qualification is pending.",
        )

    def test_candidate_publish_qualification_and_quarantine_are_replayable(self) -> None:
        candidate = self.candidate()
        record = candidate.record(Provider.CODEX, Platform.WINDOWS, "0.150.0")
        assert record is not None
        self.assertEqual(BackendVersionStatus.CANDIDATE, record.status)
        self.assertFalse(record.enabled_for_dispatch)

        changed = publish_backend_version_registry(
            candidate,
            record_path=self.record_path,
            pin_path=self.pin_path,
            expected_current_digest=self.registry.registry_digest,
        )
        self.assertTrue(changed)
        self.assertFalse(
            publish_backend_version_registry(
                candidate,
                record_path=self.record_path,
                pin_path=self.pin_path,
                expected_current_digest=self.registry.registry_digest,
            )
        )

        qualified = prepare_backend_version_qualification(
            candidate,
            expected_registry_digest=candidate.registry_digest,
            provider=Provider.CODEX,
            platform=Platform.WINDOWS,
            backend_version="0.150.0",
            max_concurrency=2,
            evidence_digest="sha256:" + "1" * 64,
            publication_receipt_digest="sha256:" + "2" * 64,
            review_reference="independent-review:42",
            note="Locally qualified and independently reviewed.",
        )
        self.assertTrue(
            publish_backend_version_registry(
                qualified,
                record_path=self.record_path,
                pin_path=self.pin_path,
                expected_current_digest=candidate.registry_digest,
            )
        )
        decoded = decode_backend_version_registry_bytes(self.record_path.read_bytes())
        published = decoded.record(Provider.CODEX, Platform.WINDOWS, "0.150.0")
        assert published is not None
        self.assertTrue(published.enabled_for_dispatch)
        self.assertEqual(2, published.max_concurrency)

        quarantined = prepare_backend_version_quarantine(
            qualified,
            expected_registry_digest=qualified.registry_digest,
            provider=Provider.CODEX,
            platform=Platform.WINDOWS,
            backend_version="0.150.0",
            review_reference="incident:WB-7",
            note="Quarantined after an adapter regression.",
        )
        disabled = quarantined.record(
            Provider.CODEX, Platform.WINDOWS, "0.150.0"
        )
        assert disabled is not None
        self.assertEqual(BackendVersionStatus.QUARANTINED, disabled.status)
        self.assertEqual(0, disabled.max_concurrency)
        self.assertEqual("sha256:" + "1" * 64, disabled.evidence_digest)

    def test_stale_digest_unknown_profile_and_unknown_version_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            BackendVersionRegistryUpdateError, "registry_digest_conflict"
        ):
            prepare_backend_version_candidate(
                self.registry,
                expected_registry_digest="sha256:" + "0" * 64,
                provider=Provider.CODEX,
                platform=Platform.WINDOWS,
                backend_version="0.150.0",
                protocol_profile="codex-app-server-v1",
                package_shasum="a" * 40,
                package_integrity=INTEGRITY,
                note="Candidate.",
            )
        with self.assertRaisesRegex(
            BackendVersionRegistryUpdateError, "protocol_profile_unknown"
        ):
            prepare_backend_version_candidate(
                self.registry,
                expected_registry_digest=self.registry.registry_digest,
                provider=Provider.CODEX,
                platform=Platform.WINDOWS,
                backend_version="0.150.0",
                protocol_profile="codex-unknown-v9",
                package_shasum="a" * 40,
                package_integrity=INTEGRITY,
                note="Candidate.",
            )
        with self.assertRaisesRegex(
            BackendVersionRegistryUpdateError, "candidate_missing"
        ):
            prepare_backend_version_qualification(
                self.registry,
                expected_registry_digest=self.registry.registry_digest,
                provider=Provider.CODEX,
                platform=Platform.WINDOWS,
                backend_version="0.150.0",
                max_concurrency=1,
                evidence_digest="sha256:" + "1" * 64,
                publication_receipt_digest="sha256:" + "2" * 64,
                review_reference="review:missing",
                note="Missing candidate.",
            )

    def test_third_qualified_version_is_rejected(self) -> None:
        first = self.candidate(version="0.150.0")
        first = prepare_backend_version_qualification(
            first,
            expected_registry_digest=first.registry_digest,
            provider=Provider.CODEX,
            platform=Platform.WINDOWS,
            backend_version="0.150.0",
            max_concurrency=1,
            evidence_digest="sha256:" + "1" * 64,
            publication_receipt_digest="sha256:" + "2" * 64,
            review_reference="review:first",
            note="Qualified.",
        )
        third = self.candidate(first, version="0.151.0")

        with self.assertRaisesRegex(
            BackendVersionRegistryUpdateError,
            "registry_policy_violation: a backend/OS cell may keep at most two",
        ):
            prepare_backend_version_qualification(
                third,
                expected_registry_digest=third.registry_digest,
                provider=Provider.CODEX,
                platform=Platform.WINDOWS,
                backend_version="0.151.0",
                max_concurrency=1,
                evidence_digest="sha256:" + "3" * 64,
                publication_receipt_digest="sha256:" + "4" * 64,
                review_reference="review:third",
                note="Would exceed the support window.",
            )

    def test_publication_rejects_a_stale_or_noncanonical_pin(self) -> None:
        candidate = self.candidate()
        self.pin_path.write_text("not a trust pin\n", encoding="utf-8")

        with self.assertRaisesRegex(
            BackendVersionRegistryUpdateError, "current_pin_invalid"
        ):
            publish_backend_version_registry(
                candidate,
                record_path=self.record_path,
                pin_path=self.pin_path,
                expected_current_digest=self.registry.registry_digest,
            )

    def test_second_replace_failure_restores_the_previous_registry_pair(self) -> None:
        candidate = self.candidate()
        before_record = self.record_path.read_bytes()
        before_pin = self.pin_path.read_bytes()
        real_replace = registry_service.os.replace
        calls = 0

        def fail_second_replace(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated pin replacement failure")
            return real_replace(source, target)

        with (
            mock.patch.object(
                registry_service.os,
                "replace",
                side_effect=fail_second_replace,
            ),
            self.assertRaisesRegex(
                BackendVersionRegistryUpdateError,
                "publication_failed",
            ),
        ):
            publish_backend_version_registry(
                candidate,
                record_path=self.record_path,
                pin_path=self.pin_path,
                expected_current_digest=self.registry.registry_digest,
            )

        self.assertGreaterEqual(calls, 3)
        self.assertEqual(before_record, self.record_path.read_bytes())
        self.assertEqual(before_pin, self.pin_path.read_bytes())

    def test_second_staging_failure_removes_the_first_temporary_file(self) -> None:
        candidate = self.candidate()
        before_record = self.record_path.read_bytes()
        before_pin = self.pin_path.read_bytes()
        real_write_staged = registry_service._write_staged
        calls = 0

        def fail_second_stage(path, raw):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated pin staging failure")
            return real_write_staged(path, raw)

        with (
            mock.patch.object(
                registry_service,
                "_write_staged",
                side_effect=fail_second_stage,
            ),
            self.assertRaisesRegex(
                BackendVersionRegistryUpdateError,
                "publication_failed",
            ),
        ):
            publish_backend_version_registry(
                candidate,
                record_path=self.record_path,
                pin_path=self.pin_path,
                expected_current_digest=self.registry.registry_digest,
            )

        self.assertEqual(before_record, self.record_path.read_bytes())
        self.assertEqual(before_pin, self.pin_path.read_bytes())
        self.assertEqual(
            {self.record_path.name, self.pin_path.name},
            {item.name for item in self.root.iterdir()},
        )

    def test_transition_conflicts_fail_closed(self) -> None:
        candidate = self.candidate()
        self.assertIs(candidate, self.candidate(candidate))
        with self.assertRaisesRegex(
            BackendVersionRegistryUpdateError,
            "backend_version_exists",
        ):
            prepare_backend_version_candidate(
                candidate,
                expected_registry_digest=candidate.registry_digest,
                provider=Provider.CODEX,
                platform=Platform.WINDOWS,
                backend_version="0.150.0",
                protocol_profile="codex-app-server-v1",
                package_shasum="a" * 40,
                package_integrity=INTEGRITY,
                note="Conflicting candidate metadata.",
            )

        quarantined = prepare_backend_version_quarantine(
            self.registry,
            expected_registry_digest=self.registry.registry_digest,
            provider=Provider.CODEX,
            platform=Platform.WINDOWS,
            backend_version="0.149.0",
            review_reference="incident:test",
            note="Quarantined for transition tests.",
        )
        with self.assertRaisesRegex(
            BackendVersionRegistryUpdateError,
            "candidate_not_promotable",
        ):
            prepare_backend_version_qualification(
                quarantined,
                expected_registry_digest=quarantined.registry_digest,
                provider=Provider.CODEX,
                platform=Platform.WINDOWS,
                backend_version="0.149.0",
                max_concurrency=1,
                evidence_digest="sha256:" + "1" * 64,
                publication_receipt_digest="sha256:" + "2" * 64,
                review_reference="review:test",
                note="Must not re-enable a quarantined record directly.",
            )
        with self.assertRaisesRegex(
            BackendVersionRegistryUpdateError,
            "backend_version_missing",
        ):
            prepare_backend_version_quarantine(
                self.registry,
                expected_registry_digest=self.registry.registry_digest,
                provider=Provider.CODEX,
                platform=Platform.WINDOWS,
                backend_version="9.9.9",
                review_reference="incident:unknown",
                note="Unknown version.",
            )


if __name__ == "__main__":
    unittest.main()
