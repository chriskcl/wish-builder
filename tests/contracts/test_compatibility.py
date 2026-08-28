from __future__ import annotations

import copy
import dataclasses
import json
import unittest

from wish_builder.compatibility import (
    BUNDLED_BACKEND_QUALIFICATION_DIGESTS,
    BUNDLED_TRELLIS_COMPATIBILITY_DIGESTS,
    BundledCompatibilityError,
    admit_backend_qualification_for_trellis,
    admit_bundled_backend_qualification_bytes,
    admit_bundled_trellis_compatibility_bytes,
    bundled_backend_qualification_bytes,
    bundled_trellis_compatibility_bytes,
    load_bundled_compatibility,
    load_bundled_trellis_compatibility,
)
from wish_builder.contracts import (
    EvidenceScope,
    Platform,
    Provider,
    QualificationStatus,
    canonical_json_bytes,
    canonical_sha256,
    decode_backend_qualification_bundle_bytes,
    decode_backend_qualification_bundle_primitive,
    decode_trellis_compatibility_bytes,
    decode_trellis_compatibility_primitive,
)
from wish_builder.contracts.diagnostics import ReasonCode


def _digest(value: object) -> str:
    return "sha256:" + canonical_sha256(value)


def _with_root_digest(value: dict[str, object]) -> dict[str, object]:
    body = {key: item for key, item in value.items() if key != "bundleDigest"}
    value["bundleDigest"] = _digest(body)
    return value


def _with_trellis_digest(value: dict[str, object]) -> dict[str, object]:
    body = {key: item for key, item in value.items() if key != "compatibilityDigest"}
    value["compatibilityDigest"] = _digest(body)
    return value


def _with_adapter_and_trellis_digests(
    value: dict[str, object],
) -> dict[str, object]:
    adapter = value["adapterQualification"]
    adapter_body = {
        key: item for key, item in adapter.items() if key != "qualificationDigest"
    }
    adapter["qualificationDigest"] = _digest(adapter_body)
    return _with_trellis_digest(value)


def _primitive() -> dict[str, object]:
    return json.loads(bundled_backend_qualification_bytes())


def _trellis_primitive() -> dict[str, object]:
    return json.loads(bundled_trellis_compatibility_bytes())


class CompatibilityContractTests(unittest.TestCase):
    def test_official_trellis_0615_is_canonical_and_digest_pinned(self) -> None:
        raw = bundled_trellis_compatibility_bytes()
        compatibility = load_bundled_trellis_compatibility()

        self.assertEqual("0.6.15", compatibility.trellis_version)
        self.assertEqual(raw, compatibility.canonical_json_bytes())
        self.assertEqual(
            BUNDLED_TRELLIS_COMPATIBILITY_DIGESTS["0.6.15"],
            compatibility.compatibility_digest,
        )
        self.assertEqual(
            ("@mindfoldhq/trellis", "@mindfoldhq/trellis-core"),
            tuple(package.name for package in compatibility.packages),
        )
        self.assertEqual(
            (
                "sha512-grbF8PToesHojsaWkoG4+Aupih7eZHkXH5y33uzPrWQXwIRewwlM1AoeJEttcXAia9nLZzF/ezuR338PWCKv+A==",
                "sha512-UYMVMM47Zyr/ns39U/f620cs7XaFKX2yez91QMV40Eah+uxxEdGwYHgNjDPZxwMhlr/0TIsZuMM+KF6lcbxg9w==",
            ),
            tuple(package.integrity for package in compatibility.packages),
        )
        self.assertTrue(
            all(
                package.provenance.predicate_type == "https://slsa.dev/provenance/v1"
                for package in compatibility.packages
            )
        )
        adapter = compatibility.adapter_qualification
        self.assertEqual(1, adapter.schema_version)
        self.assertEqual("0.6.15", adapter.trellis_version)
        self.assertEqual("wish-builder.trellis-graph.v1", adapter.graph.derived_format)
        self.assertTrue(adapter.graph.read_only_snapshot)
        self.assertTrue(adapter.graph.deterministic_snapshot)
        self.assertTrue(adapter.graph.strict_import)
        self.assertTrue(adapter.projection.single_writer)
        self.assertTrue(adapter.projection.digest_guarded)
        self.assertTrue(adapter.projection.expected_revision_guarded)
        self.assertTrue(adapter.projection.post_write_verified)
        self.assertFalse(adapter.projection.isolated_checkout)
        self.assertFalse(adapter.projection.cross_process_cas)
        self.assertFalse(adapter.projection.concurrent_projection_writers_safe)

    def test_backend_qualification_is_independent_and_references_trellis(self) -> None:
        raw = bundled_backend_qualification_bytes()
        bundle = load_bundled_compatibility()
        trellis = load_bundled_trellis_compatibility()

        self.assertEqual(raw, bundle.canonical_json_bytes())
        self.assertEqual(
            BUNDLED_BACKEND_QUALIFICATION_DIGESTS["0.6.15"],
            bundle.bundle_digest,
        )
        self.assertEqual(
            trellis.compatibility_digest,
            bundle.trellis_compatibility_digest,
        )
        primitive = bundle.to_primitive()
        self.assertNotIn("packages", primitive)
        self.assertNotIn("sourcePackageVersion", primitive)
        self.assertNotIn("trellisVersion", primitive)
        self.assertNotIn("trellisPackages", json.dumps(primitive))
        self.assertEqual(
            (Provider.CODEX, Provider.OMP, Provider.PI),
            tuple(provider.provider for provider in bundle.providers),
        )
        self.assertEqual(
            {status for status in QualificationStatus},
            {
                cell.qualification.status
                for provider in bundle.providers
                for cell in provider.platforms
            },
        )
        for provider in bundle.providers:
            self.assertEqual(
                (Platform.LINUX, Platform.WINDOWS),
                tuple(cell.platform for cell in provider.platforms),
            )
            for cell in provider.platforms:
                self.assertFalse(cell.qualification.enabled_for_dispatch)
                capability = cell.capabilities.to_primitive()
                self.assertEqual(2, capability["schemaVersion"])
                self.assertNotIn("trellisVersion", capability)
                self.assertNotIn("graphExportVersion", capability)
                self.assertNotIn("completeGraphExport", capability["features"])
                self.assertNotIn("freshAttemptWorktrees", capability["features"])
                self.assertEqual(
                    {"cancel_turn", "reserve_channel", "send_task_packet"},
                    set(capability["operations"]),
                )

        pi_windows = bundle.platform(Provider.PI, Platform.WINDOWS)
        self.assertTrue(pi_windows.qualification.live)
        self.assertEqual(
            EvidenceScope.STARTUP_AND_HANDSHAKE,
            pi_windows.qualification.evidence_scope,
        )

        omp_windows = bundle.platform(Provider.OMP, Platform.WINDOWS)
        self.assertEqual(
            QualificationStatus.BLOCKED_CREDENTIALS,
            omp_windows.qualification.status,
        )
        self.assertEqual(
            EvidenceScope.DETERMINISTIC_FIXTURE_ONLY,
            omp_windows.qualification.evidence_scope,
        )
        self.assertFalse(omp_windows.qualification.live)
        self.assertEqual(("fixture:omp-rpc-v2",), omp_windows.qualification.evidence)

    def test_trellis_decoder_rejects_unknown_fields_versions_and_order(self) -> None:
        cases = {
            "unknown": lambda value: value.update({"surprise": True}),
            "wrong package version": lambda value: value["packages"][0].__setitem__(
                "version", "0.6.14"
            ),
            "wrong root version": lambda value: value.__setitem__(
                "trellisVersion", "0.6.14"
            ),
            "reordered packages": lambda value: value.__setitem__(
                "packages", list(reversed(value["packages"]))
            ),
            "malformed integrity": lambda value: value["packages"][0].__setitem__(
                "npmIntegrity", "sha512-not-a-full-digest"
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                value = _trellis_primitive()
                mutate(value)
                result = decode_trellis_compatibility_primitive(
                    _with_trellis_digest(value)
                )
                self.assertFalse(result.ok)

        missing = _trellis_primitive()
        del missing["packages"][0]["npmIntegrity"]
        result = decode_trellis_compatibility_primitive(_with_trellis_digest(missing))
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.MISSING_FIELD, result.issues[0].reason_code)

    def test_trellis_decoder_fails_closed_at_limits_shape_and_bytes_boundaries(self) -> None:
        with self.assertRaisesRegex(TypeError, "DecodeLimits"):
            decode_trellis_compatibility_primitive(
                _trellis_primitive(), limits=None  # type: ignore[arg-type]
            )

        wrong_shape = decode_trellis_compatibility_primitive([])
        self.assertFalse(wrong_shape.ok)
        self.assertEqual(
            ReasonCode.WRONG_CONTAINER_TYPE,
            wrong_shape.issues[0].reason_code,
        )

        invalid_utf8 = decode_trellis_compatibility_bytes(b"\xff")
        self.assertFalse(invalid_utf8.ok)
        self.assertEqual(
            ReasonCode.INVALID_UTF8,
            invalid_utf8.issues[0].reason_code,
        )

    def test_trellis_integrity_is_digest_bound_and_compiled_pin_rejects_rehash(
        self,
    ) -> None:
        value = _trellis_primitive()
        value["packages"][0]["npmIntegrity"] = "sha512-" + "A" * 86 + "=="
        raw = canonical_json_bytes(_with_trellis_digest(value))

        generic = decode_trellis_compatibility_bytes(raw)
        self.assertTrue(generic.ok, generic.report.render_text())
        with self.assertRaisesRegex(BundledCompatibilityError, "compiled trust pin"):
            admit_bundled_trellis_compatibility_bytes(raw)

    def test_adapter_qualification_decoder_is_closed_and_digest_bound(self) -> None:
        cases = {
            "unknown graph field": lambda value: value["adapterQualification"][
                "graph"
            ].update({"surprise": True}),
            "wrong adapter version": lambda value: value[
                "adapterQualification"
            ].__setitem__("trellisVersion", "0.6.14"),
            "wrong graph format": lambda value: value["adapterQualification"][
                "graph"
            ].__setitem__("derivedFormat", "wish-builder.trellis-graph.v2"),
            "missing graph evidence": lambda value: value["adapterQualification"][
                "graph"
            ]["evidence"].pop(),
            "reordered projection evidence": lambda value: value[
                "adapterQualification"
            ]["projection"].__setitem__(
                "evidence",
                list(reversed(value["adapterQualification"]["projection"]["evidence"])),
            ),
            "unproven cross-process CAS": lambda value: value["adapterQualification"][
                "projection"
            ].__setitem__("crossProcessCas", True),
            "unproven concurrent projection writers": lambda value: value["adapterQualification"][
                "projection"
            ].__setitem__("concurrentProjectionWritersSafe", True),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                value = _trellis_primitive()
                mutate(value)
                result = decode_trellis_compatibility_primitive(
                    _with_adapter_and_trellis_digests(value)
                )
                self.assertFalse(result.ok)

        drifted = _trellis_primitive()
        drifted["adapterQualification"]["qualificationDigest"] = "sha256:" + "0" * 64
        result = decode_trellis_compatibility_primitive(_with_trellis_digest(drifted))
        self.assertFalse(result.ok)
        self.assertIn("qualification_digest", result.report.render_text())

        missing = _trellis_primitive()
        del missing["adapterQualification"]["projection"]["singleWriter"]
        result = decode_trellis_compatibility_primitive(
            _with_adapter_and_trellis_digests(missing)
        )
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.MISSING_FIELD, result.issues[0].reason_code)

    def test_backend_qualification_can_reference_trellis_without_owning_projection(
        self,
    ) -> None:
        from tests.contracts.test_compatibility_v2 import _enabled_v2_primitive

        decoded = decode_backend_qualification_bundle_primitive(_enabled_v2_primitive())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None
        self.assertIs(
            decoded.value,
            admit_backend_qualification_for_trellis(
                decoded.value,
                load_bundled_trellis_compatibility(),
            ),
        )

    def test_backend_decoder_rejects_unknown_fields_and_wrong_types(self) -> None:
        raw = bundled_backend_qualification_bytes()
        duplicate = b'{"bundleDigest":"sha256:' + (b"0" * 64) + b'",' + raw[1:]
        duplicate_result = decode_backend_qualification_bundle_bytes(duplicate)
        self.assertFalse(duplicate_result.ok)
        self.assertEqual(
            ReasonCode.DUPLICATE_OBJECT_KEY,
            duplicate_result.issues[0].reason_code,
        )

        cases = (
            ("unknown", lambda value: value.update({"surprise": True})),
            ("wrong-root-type", lambda value: value.__setitem__("published", 0)),
            (
                "wrong-nested-type",
                lambda value: value["providers"][0].__setitem__("platforms", {}),
            ),
            (
                "unknown-enum",
                lambda value: value["providers"][0]["platforms"][0][
                    "qualification"
                ].__setitem__("status", "almost_passed"),
            ),
        )
        for label, mutate in cases:
            with self.subTest(label=label):
                value = _primitive()
                mutate(value)
                result = decode_backend_qualification_bundle_primitive(value)
                self.assertFalse(result.ok)

    def test_backend_digest_layers_fail_closed_on_drift(self) -> None:
        mutations = {
            "bundle": lambda value: value.__setitem__(
                "bundleDigest", "sha256:" + "0" * 64
            ),
            "Trellis reference": lambda value: value.__setitem__(
                "trellisCompatibilityDigest", "sha256:" + "1" * 64
            ),
            "policy": lambda value: value["policy"].__setitem__(
                "freshAttemptWorktree", False
            ),
            "launch profile": lambda value: value["providers"][0]["platforms"][0][
                "launchProfile"
            ].__setitem__("command", "untrusted-command"),
            "capability": lambda value: value["providers"][0]["platforms"][0][
                "capabilities"
            ].__setitem__("maxTaskPacketBytes", 1),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                value = _primitive()
                mutate(value)
                result = decode_backend_qualification_bundle_bytes(
                    canonical_json_bytes(value)
                )
                self.assertFalse(result.ok)

    def test_backend_compiled_pin_rejects_coherent_rehash(self) -> None:
        value = _primitive()
        value["providers"][0]["platforms"][0]["qualification"]["note"] += " changed"
        raw = canonical_json_bytes(_with_root_digest(value))

        generic = decode_backend_qualification_bundle_bytes(raw)
        self.assertTrue(generic.ok, generic.report.render_text())
        with self.assertRaisesRegex(BundledCompatibilityError, "compiled trust pin"):
            admit_bundled_backend_qualification_bytes(raw)

    def test_noncanonical_bytes_are_rejected_by_both_bundled_loaders(self) -> None:
        cases = (
            (
                bundled_trellis_compatibility_bytes(),
                decode_trellis_compatibility_bytes,
                admit_bundled_trellis_compatibility_bytes,
            ),
            (
                bundled_backend_qualification_bytes(),
                decode_backend_qualification_bundle_bytes,
                admit_bundled_backend_qualification_bytes,
            ),
        )
        for raw, decoder, admit in cases:
            with self.subTest(admit=admit.__name__):
                generic = decoder(b" " + raw)
                self.assertTrue(generic.ok, generic.report.render_text())
                with self.assertRaisesRegex(
                    BundledCompatibilityError, "canonical form"
                ):
                    admit(b" " + raw)

    def test_models_are_frozen_and_backend_lookup_uses_closed_enums(self) -> None:
        trellis = load_bundled_trellis_compatibility()
        bundle = load_bundled_compatibility()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            trellis.published = False  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            trellis.adapter_qualification.graph.read_only_snapshot = False  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            trellis.adapter_qualification.projection.single_writer = False  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            bundle.published = True  # type: ignore[misc]
        with self.assertRaises(TypeError):
            bundle.platform("codex", Platform.WINDOWS)  # type: ignore[arg-type]

    def test_backend_decoder_diagnostics_are_stable(self) -> None:
        value = _primitive()
        value["providers"][1]["platforms"][1]["qualification"]["evidence"] = [
            "fixture:omp-rpc-v1",
            "fixture:omp-rpc-v1",
        ]
        first = decode_backend_qualification_bundle_primitive(copy.deepcopy(value))
        second = decode_backend_qualification_bundle_primitive(copy.deepcopy(value))
        self.assertFalse(first.ok)
        self.assertEqual(first.diagnostic_bytes(), second.diagnostic_bytes())
        self.assertEqual(ReasonCode.DUPLICATE_ITEM, first.issues[0].reason_code)


if __name__ == "__main__":
    unittest.main()
