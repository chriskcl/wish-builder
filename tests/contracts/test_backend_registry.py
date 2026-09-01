from __future__ import annotations

import dataclasses
import unittest

from wish_builder.compatibility import (
    BACKEND_VERSION_REGISTRY_DIGEST,
    admit_bundled_backend_version_registry_bytes,
    bundled_backend_version_registry_bytes,
    load_bundled_backend_version_registry,
)
from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.backend_registry import (
    BackendVersionRegistry,
    BackendVersionStatus,
)
from wish_builder.contracts.backend_registry_decoder import (
    MAX_BACKEND_REGISTRY_BYTES,
    BackendRegistryDecodeError,
    decode_backend_version_registry_bytes,
)
from wish_builder.contracts.compatibility import Platform, Provider


class BackendRegistryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_bundled_backend_version_registry()

    def test_bundled_registry_is_canonical_and_pinned(self) -> None:
        raw = bundled_backend_version_registry_bytes()

        admitted = admit_bundled_backend_version_registry_bytes(raw)

        self.assertEqual(BACKEND_VERSION_REGISTRY_DIGEST, admitted.registry_digest)
        self.assertEqual(raw, admitted.canonical_json_bytes())
        self.assertEqual(3, len(admitted.profiles))
        self.assertEqual(6, len(admitted.records))
        enabled = tuple(item for item in admitted.records if item.enabled_for_dispatch)
        self.assertEqual(1, len(enabled))
        self.assertEqual(
            (Provider.CODEX, Platform.WINDOWS, "0.149.0", 2),
            (
                enabled[0].provider,
                enabled[0].platform,
                enabled[0].backend_version,
                enabled[0].max_concurrency,
            ),
        )

    def test_decoder_rejects_duplicate_keys_oversize_and_non_finite_numbers(self) -> None:
        with self.assertRaisesRegex(BackendRegistryDecodeError, "duplicate JSON key"):
            decode_backend_version_registry_bytes(
                b'{"profiles":[],"profiles":[],"records":[],"registryDigest":"x",'
                b'"schemaVersion":1}'
            )
        with self.assertRaisesRegex(BackendRegistryDecodeError, "byte limit"):
            decode_backend_version_registry_bytes(b" " * (MAX_BACKEND_REGISTRY_BYTES + 1))
        with self.assertRaisesRegex(BackendRegistryDecodeError, "non-finite"):
            decode_backend_version_registry_bytes(b'{"value":NaN}')

    def test_registry_digest_and_closed_fields_fail_closed(self) -> None:
        primitive = self.registry.to_primitive()
        primitive["unexpected"] = True
        from wish_builder.contracts.serialization import canonical_json_bytes

        with self.assertRaisesRegex(BackendRegistryDecodeError, "invalid field set"):
            decode_backend_version_registry_bytes(canonical_json_bytes(primitive))

        primitive.pop("unexpected")
        primitive["registryDigest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(BackendRegistryDecodeError, "does not match"):
            decode_backend_version_registry_bytes(canonical_json_bytes(primitive))

    def test_more_than_two_qualified_versions_per_backend_os_is_rejected(self) -> None:
        template = self.registry.record(Provider.CODEX, Platform.WINDOWS, "0.149.0")
        assert template is not None
        records = self.registry.records + (
            dataclasses.replace(template, backend_version="0.150.0"),
            dataclasses.replace(template, backend_version="0.151.0"),
        )
        records = tuple(
            sorted(
                records,
                key=lambda item: (
                    item.provider.value,
                    item.platform.value,
                    item.backend_version,
                ),
            )
        )
        body = {
            "profiles": [item.to_primitive() for item in self.registry.profiles],
            "records": [item.to_primitive() for item in records],
            "schemaVersion": self.registry.schema_version,
        }

        with self.assertRaisesRegex(ValueError, "at most two"):
            BackendVersionRegistry(
                schema_version=self.registry.schema_version,
                profiles=self.registry.profiles,
                records=records,
                registry_digest="sha256:" + canonical_sha256(body),
            )

    def test_provider_protocol_identity_is_unambiguous(self) -> None:
        source = self.registry.profile("codex-app-server-v1")
        profiles = tuple(
            sorted(
                (
                    *self.registry.profiles,
                    dataclasses.replace(source, profile_id="codex-app-server-v2"),
                ),
                key=lambda item: item.profile_id,
            )
        )
        body = {
            "profiles": [item.to_primitive() for item in profiles],
            "records": [item.to_primitive() for item in self.registry.records],
            "schemaVersion": self.registry.schema_version,
        }

        with self.assertRaisesRegex(ValueError, "provider/protocol identities"):
            BackendVersionRegistry(
                schema_version=self.registry.schema_version,
                profiles=profiles,
                records=self.registry.records,
                registry_digest="sha256:" + canonical_sha256(body),
            )

    def test_candidate_and_quarantine_cannot_authorize_dispatch(self) -> None:
        for status in (
            BackendVersionStatus.CANDIDATE,
            BackendVersionStatus.QUARANTINED,
        ):
            with self.subTest(status=status.value):
                source = self.registry.record(
                    Provider.CODEX, Platform.WINDOWS, "0.149.0"
                )
                assert source is not None
                values = {
                    "status": status,
                    "max_concurrency": 0,
                    "review_reference": (
                        None
                        if status is BackendVersionStatus.CANDIDATE
                        else "security-review:quarantine"
                    ),
                }
                if status is BackendVersionStatus.CANDIDATE:
                    values.update(
                        evidence_digest=None,
                        publication_receipt_digest=None,
                    )
                record = dataclasses.replace(source, **values)
                self.assertFalse(record.enabled_for_dispatch)

    def test_profile_record_and_registry_boundaries_fail_closed(self) -> None:
        profile = self.registry.profile("codex-app-server-v1")
        profile_cases = (
            ("schema_version", 2),
            ("profile_id", "Not-Lowercase"),
            ("provider", "codex"),
            ("adapter", "codex_app_server"),
            ("protocol", "Not-Lowercase"),
            ("package_name", "unscoped-package"),
            ("bin_name", "Not-Lowercase"),
            ("entrypoint", "/absolute/entrypoint.js"),
            ("entrypoint", "bin//codex.js"),
            ("runtime", "python"),
            ("version_probe", "floating-version-probe"),
        )
        for field_name, value in profile_cases:
            with self.subTest(profile_field=field_name, value=value):
                with self.assertRaises((TypeError, ValueError)):
                    dataclasses.replace(profile, **{field_name: value})

        record = self.registry.record(Provider.CODEX, Platform.WINDOWS, "0.149.0")
        assert record is not None
        record_cases = (
            {"provider": "codex"},
            {"platform": "windows"},
            {"backend_version": "latest"},
            {"protocol_profile": "Not-Lowercase"},
            {"launch_profile_digest": "not-a-digest"},
            {"package_shasum": "0" * 39},
            {"package_integrity": "sha512-not-valid"},
            {"status": "qualified"},
            {"max_concurrency": True},
            {"max_concurrency": 65},
            {"evidence_digest": "not-a-digest"},
            {"publication_receipt_digest": "not-a-digest"},
            {"max_concurrency": 0},
            {"evidence_digest": None},
            {
                "status": BackendVersionStatus.QUARANTINED,
                "max_concurrency": 1,
            },
            {
                "status": BackendVersionStatus.QUARANTINED,
                "max_concurrency": 0,
                "review_reference": None,
            },
            {
                "status": BackendVersionStatus.CANDIDATE,
                "max_concurrency": 1,
            },
        )
        for values in record_cases:
            with self.subTest(record_values=values):
                with self.assertRaises((TypeError, ValueError)):
                    dataclasses.replace(record, **values)

        duplicate_profiles = (
            self.registry.profiles[0],
            self.registry.profiles[0],
            *self.registry.profiles[1:],
        )
        duplicate_records = (
            self.registry.records[0],
            self.registry.records[0],
            *self.registry.records[1:],
        )
        mismatched_record = dataclasses.replace(
            self.registry.records[0],
            protocol_profile="pi-jsonl-v1",
        )
        registry_cases = (
            {"schema_version": 2},
            {"profiles": list(self.registry.profiles)},
            {"profiles": ()},
            {"records": list(self.registry.records)},
            {"records": ()},
            {"profiles": tuple(reversed(self.registry.profiles))},
            {"profiles": duplicate_profiles},
            {"records": tuple(reversed(self.registry.records))},
            {"records": duplicate_records},
            {"records": (mismatched_record, *self.registry.records[1:])},
        )
        for values in registry_cases:
            with self.subTest(registry_values=values):
                with self.assertRaises((TypeError, ValueError)):
                    dataclasses.replace(self.registry, **values)

        with self.assertRaises(KeyError):
            self.registry.profile_for_protocol(Provider.CODEX, "missing-protocol")


if __name__ == "__main__":
    unittest.main()
