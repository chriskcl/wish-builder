from __future__ import annotations

import dataclasses
import unittest

from tests.e2e.support import serial_parallel_manifest
from wish_builder.compatibility import (
    load_bundled_backend_version_registry,
    load_bundled_compatibility,
)
from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.backend_registry import (
    BackendVersionRegistry,
    BackendVersionStatus,
)
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.contracts.manifest_v2 import WorkerProvider
from wish_builder.services.backend_admission import (
    BackendAdmissionReason,
    admit_backend,
)


class BackendVersionAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_bundled_compatibility()
        cls.registry = load_bundled_backend_version_registry()

    def manifest(
        self,
        provider: WorkerProvider,
        platform: Platform,
        concurrency: int = 1,
    ):
        compatibility_provider = {
            WorkerProvider.CODEX: Provider.CODEX,
            WorkerProvider.OH_MY_PI: Provider.OMP,
            WorkerProvider.PI: Provider.PI,
        }[provider]
        cell = self.bundle.platform(compatibility_provider, platform)
        base = serial_parallel_manifest()
        return dataclasses.replace(
            base,
            provider=provider,
            capability_digest=cell.capabilities.capability_digest,
            launch_profile_digest=cell.launch_profile_digest,
            policy_digest=self.bundle.policy_digest,
            execution_budget=dataclasses.replace(
                base.execution_budget,
                max_concurrent_workers=concurrency,
            ),
            max_concurrency=concurrency,
        )

    def qualified_pi_windows_registry(self) -> BackendVersionRegistry:
        records = tuple(
            dataclasses.replace(
                item,
                status=BackendVersionStatus.QUALIFIED,
                max_concurrency=2,
                evidence_digest="sha256:" + "1" * 64,
                publication_receipt_digest="sha256:" + "2" * 64,
                review_reference="review:pi-windows",
                note="Locally qualified for admission tests.",
            )
            if item.provider is Provider.PI and item.platform is Platform.WINDOWS
            else item
            for item in self.registry.records
        )
        body = {
            "profiles": [item.to_primitive() for item in self.registry.profiles],
            "records": [item.to_primitive() for item in records],
            "schemaVersion": self.registry.schema_version,
        }
        return BackendVersionRegistry(
            schema_version=self.registry.schema_version,
            profiles=self.registry.profiles,
            records=records,
            registry_digest="sha256:" + canonical_sha256(body),
        )

    def test_registry_can_enable_a_cell_without_the_legacy_cell_enable_bit(self) -> None:
        registry = self.qualified_pi_windows_registry()
        cell = self.bundle.platform(Provider.PI, Platform.WINDOWS)
        self.assertFalse(cell.qualification.enabled_for_dispatch)

        result = admit_backend(
            self.manifest(WorkerProvider.PI, Platform.WINDOWS, 2),
            bundle=self.bundle,
            platform=Platform.WINDOWS,
            registry=registry,
            backend_version="0.84.2",
            package_integrity=registry.record(
                Provider.PI, Platform.WINDOWS, "0.84.2"
            ).package_integrity,
            protocol_profile="pi-jsonl-rpc-v1",
        )

        self.assertTrue(result.admitted)
        self.assertEqual(BackendAdmissionReason.NONE, result.reason)

    def test_candidate_unknown_quarantined_and_identity_drift_fail_closed(self) -> None:
        manifest = self.manifest(WorkerProvider.PI, Platform.WINDOWS)
        candidate = self.registry.record(Provider.PI, Platform.WINDOWS, "0.84.2")
        assert candidate is not None
        cases = (
            (
                self.registry,
                "0.84.2",
                candidate.package_integrity,
                "pi-jsonl-rpc-v1",
            ),
            (
                self.registry,
                "0.85.0",
                candidate.package_integrity,
                "pi-jsonl-rpc-v1",
            ),
            (
                self.qualified_pi_windows_registry(),
                "0.84.2",
                "sha512-" + "A" * 88,
                "pi-jsonl-rpc-v1",
            ),
            (
                self.qualified_pi_windows_registry(),
                "0.84.2",
                candidate.package_integrity,
                "pi-jsonl-rpc-v9",
            ),
        )
        for registry, version, integrity, profile in cases:
            with self.subTest(version=version, profile=profile):
                result = admit_backend(
                    manifest,
                    bundle=self.bundle,
                    platform=Platform.WINDOWS,
                    registry=registry,
                    backend_version=version,
                    package_integrity=integrity,
                    protocol_profile=profile,
                )
                self.assertFalse(result.admitted)
                self.assertEqual(
                    BackendAdmissionReason.DISPATCH_NOT_QUALIFIED,
                    result.reason,
                )

        qualified = self.qualified_pi_windows_registry()
        source = qualified.record(Provider.PI, Platform.WINDOWS, "0.84.2")
        assert source is not None
        quarantined_records = tuple(
            dataclasses.replace(
                item,
                status=BackendVersionStatus.QUARANTINED,
                max_concurrency=0,
                review_reference="incident:pi",
                note="Quarantined.",
            )
            if item.key == source.key
            else item
            for item in qualified.records
        )
        body = {
            "profiles": [item.to_primitive() for item in qualified.profiles],
            "records": [item.to_primitive() for item in quarantined_records],
            "schemaVersion": qualified.schema_version,
        }
        quarantined = BackendVersionRegistry(
            schema_version=qualified.schema_version,
            profiles=qualified.profiles,
            records=quarantined_records,
            registry_digest="sha256:" + canonical_sha256(body),
        )
        result = admit_backend(
            manifest,
            bundle=self.bundle,
            platform=Platform.WINDOWS,
            registry=quarantined,
            backend_version="0.84.2",
            package_integrity=source.package_integrity,
            protocol_profile="pi-jsonl-rpc-v1",
        )
        self.assertEqual(BackendAdmissionReason.DISPATCH_NOT_QUALIFIED, result.reason)

    def test_exact_version_concurrency_limit_is_enforced(self) -> None:
        registry = self.qualified_pi_windows_registry()
        record = registry.record(Provider.PI, Platform.WINDOWS, "0.84.2")
        assert record is not None

        result = admit_backend(
            self.manifest(WorkerProvider.PI, Platform.WINDOWS, 3),
            bundle=self.bundle,
            platform=Platform.WINDOWS,
            registry=registry,
            backend_version=record.backend_version,
            package_integrity=record.package_integrity,
            protocol_profile=record.protocol_profile,
        )

        self.assertEqual(BackendAdmissionReason.CONCURRENCY_NOT_QUALIFIED, result.reason)


if __name__ == "__main__":
    unittest.main()
