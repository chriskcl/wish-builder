from __future__ import annotations

import copy
import dataclasses
import json
import unittest

from wish_builder.compatibility import bundled_compatibility_bytes
from wish_builder.contracts import (
    COMPATIBILITY_SCHEMA_VERSION,
    QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
    Platform,
    Provider,
    QualificationArtifact,
    QualificationScenario,
    QualificationScenarioEvidence,
    QualificationStatus,
    canonical_json_bytes,
    canonical_sha256,
    decode_compatibility_bundle_bytes,
    decode_compatibility_bundle_primitive,
)
from wish_builder.contracts.diagnostics import ReasonCode

SCENARIOS = (
    "full_turn",
    "active_turn_cancellation",
    "crash_reconcile",
    "cleanup",
)


def _digest(value: object) -> str:
    return "sha256:" + canonical_sha256(value)


def _overlap(*, observed: int = 2) -> dict[str, object]:
    return {
        "evidenceDigest": _digest({"overlap": "siblings-a-b"}),
        "observedConcurrentTurns": observed,
        "siblingTaskIds": [f"task-{index + 1}" for index in range(max(2, observed))],
        "ownedPathsDisjoint": True,
        "overlapObserved": True,
    }


def _rehash_root(value: dict[str, object]) -> dict[str, object]:
    value["bundleDigest"] = _digest(
        {key: item for key, item in value.items() if key != "bundleDigest"}
    )
    return value


def _rehash_artifact(artifact: dict[str, object]) -> dict[str, object]:
    artifact["artifactDigest"] = _digest(
        {key: item for key, item in artifact.items() if key != "artifactDigest"}
    )
    return artifact


def _disabled_v2_primitive() -> dict[str, object]:
    value = json.loads(bundled_compatibility_bytes())
    value["schemaVersion"] = COMPATIBILITY_SCHEMA_VERSION
    for provider in value["providers"]:
        for cell in provider["platforms"]:
            cell["qualification"]["enabledForDispatch"] = False
            cell["qualification"]["artifact"] = None
    return _rehash_root(value)


def _enabled_v2_primitive(
    *,
    max_concurrent_turns: int = 1,
    observed_max_concurrent_turns: int = 1,
    overlap: dict[str, object] | None = None,
    published: bool = False,
) -> dict[str, object]:
    value = _disabled_v2_primitive()
    value["published"] = published
    provider = value["providers"][0]
    cell = provider["platforms"][0]
    qualification = cell["qualification"]
    qualification.update(
        {
            "enabledForDispatch": True,
            "evidenceScope": "full_turn_and_cancellation",
            "live": True,
            "status": "passed",
        }
    )
    artifact: dict[str, object] = {
        "artifactDigest": "sha256:" + "0" * 64,
        "capabilityDigest": cell["capabilities"]["capabilityDigest"],
        "disjointSiblingOverlap": overlap,
        "harnessDigest": _digest({"harness": "wish-builder-backend-qualification"}),
        "harnessVersion": "1.0.0",
        "launchProfileDigest": cell["launchProfileDigest"],
        "maxConcurrentTurns": max_concurrent_turns,
        "observedMaxConcurrentTurns": observed_max_concurrent_turns,
        "platform": cell["platform"],
        "policyDigest": value["policyDigest"],
        "provider": provider["provider"],
        "scenarios": {
            scenario: {
                "evidenceDigest": _digest({"scenario": scenario}),
                "live": True,
                "name": scenario,
                "status": "passed",
            }
            for scenario in SCENARIOS
        },
        "schemaVersion": QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
        "sdk": copy.deepcopy(provider["sdk"]),
        "trellisCompatibilityDigest": value["trellisCompatibilityDigest"],
    }
    qualification["artifact"] = _rehash_artifact(artifact)
    return _rehash_root(value)


class CompatibilityV2ContractTests(unittest.TestCase):
    def assert_invalid(self, value: dict[str, object], text: str) -> None:
        result = decode_compatibility_bundle_primitive(_rehash_root(value))
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertIn(text, result.report.render_text())

    def test_disabled_cells_admit_explicit_null_artifact(self) -> None:
        value = _disabled_v2_primitive()
        result = decode_compatibility_bundle_bytes(canonical_json_bytes(value))

        self.assertTrue(result.ok, result.report.render_text())
        assert result.value is not None
        self.assertEqual(COMPATIBILITY_SCHEMA_VERSION, result.value.schema_version)
        self.assertTrue(
            all(
                cell.qualification.artifact is None
                for provider in result.value.providers
                for cell in provider.platforms
            )
        )

    def test_enabled_cell_admits_digest_bound_artifact(self) -> None:
        value = _enabled_v2_primitive()
        result = decode_compatibility_bundle_bytes(canonical_json_bytes(value))

        self.assertTrue(result.ok, result.report.render_text())
        assert result.value is not None
        cell = result.value.platform(Provider.CODEX, Platform.LINUX)
        artifact = cell.qualification.artifact
        self.assertIsInstance(artifact, QualificationArtifact)
        assert artifact is not None
        self.assertEqual(
            tuple(QualificationScenario),
            tuple(item.name for item in artifact.scenarios),
        )
        self.assertEqual(1, artifact.max_concurrent_turns)
        self.assertEqual(1, artifact.observed_max_concurrent_turns)
        self.assertNotIn("trellisPackages", artifact.to_primitive())
        self.assertNotIn("trellisVersion", artifact.to_primitive())
        self.assertEqual(
            value["trellisCompatibilityDigest"],
            artifact.trellis_compatibility_digest,
        )
        self.assertEqual(value, result.value.to_primitive())

    def test_published_bundle_is_valid_and_publication_is_digest_bound(self) -> None:
        value = _enabled_v2_primitive(published=True)
        result = decode_compatibility_bundle_bytes(canonical_json_bytes(value))

        self.assertTrue(result.ok, result.report.render_text())
        assert result.value is not None
        self.assertTrue(result.value.published)

        value["published"] = False
        drifted = decode_compatibility_bundle_bytes(canonical_json_bytes(value))
        self.assertFalse(drifted.ok)
        self.assertIn("bundle_digest", drifted.report.render_text())

    def test_old_v1_bundle_fails_closed_at_root_schema(self) -> None:
        value = _disabled_v2_primitive()
        value["schemaVersion"] = 1
        result = decode_compatibility_bundle_primitive(_rehash_root(value))

        self.assertFalse(result.ok)
        self.assertEqual(
            ReasonCode.UNSUPPORTED_SCHEMA_VERSION,
            result.issues[0].reason_code,
        )
        self.assertEqual(("schemaVersion",), result.issues[0].path)

    def test_v4_trellis_fields_are_rejected_in_the_backend_v5_schema(self) -> None:
        cases = {
            "root Trellis version": lambda value: value.__setitem__(
                "trellisVersion", "0.6.15"
            ),
            "capability Trellis version": lambda value: value["providers"][0][
                "platforms"
            ][0]["capabilities"].__setitem__("trellisVersion", "0.6.15"),
            "graph export version": lambda value: value["providers"][0]["platforms"][
                0
            ]["capabilities"].__setitem__(
                "graphExportVersion", "wish-builder.trellis-graph.v1"
            ),
            "complete graph export": lambda value: value["providers"][0][
                "platforms"
            ][0]["capabilities"]["features"].__setitem__(
                "completeGraphExport", True
            ),
            "attempt worktrees": lambda value: value["providers"][0]["platforms"][
                0
            ]["capabilities"]["features"].__setitem__(
                "freshAttemptWorktrees", True
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                value = _disabled_v2_primitive()
                mutate(value)
                result = decode_compatibility_bundle_primitive(_rehash_root(value))
                self.assertFalse(result.ok)
                self.assertEqual(ReasonCode.UNKNOWN_FIELD, result.issues[0].reason_code)

    def test_qualification_requires_artifact_field_even_when_disabled(self) -> None:
        value = _disabled_v2_primitive()
        del value["providers"][0]["platforms"][0]["qualification"]["artifact"]
        result = decode_compatibility_bundle_primitive(_rehash_root(value))

        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.MISSING_FIELD, result.issues[0].reason_code)
        self.assertEqual("artifact", result.issues[0].path[-1])

    def test_enabled_qualification_rejects_null_artifact(self) -> None:
        value = _disabled_v2_primitive()
        qualification = value["providers"][0]["platforms"][0]["qualification"]
        qualification.update(
            {
                "enabledForDispatch": True,
                "evidenceScope": "full_turn_and_cancellation",
                "live": True,
                "status": "passed",
            }
        )
        self.assert_invalid(value, "requires a qualification artifact")

    def test_artifact_self_digest_rejects_body_drift(self) -> None:
        value = _enabled_v2_primitive()
        artifact = value["providers"][0]["platforms"][0]["qualification"]["artifact"]
        artifact["harnessVersion"] = "1.0.1"

        self.assert_invalid(value, "artifact_digest")

    def test_artifact_binds_every_outer_identity_and_pin(self) -> None:
        mutations = {
            "platform": lambda artifact: artifact.__setitem__("platform", "windows"),
            "provider": lambda artifact: artifact.__setitem__("provider", "omp"),
            "SDK pin": lambda artifact: artifact["sdk"].__setitem__("shasum", "f" * 40),
            "policy digest": lambda artifact: artifact.__setitem__(
                "policyDigest", "sha256:" + "1" * 64
            ),
            "launch profile digest": lambda artifact: artifact.__setitem__(
                "launchProfileDigest", "sha256:" + "2" * 64
            ),
            "capability digest": lambda artifact: artifact.__setitem__(
                "capabilityDigest", "sha256:" + "3" * 64
            ),
            "Trellis compatibility digest": lambda artifact: artifact.__setitem__(
                "trellisCompatibilityDigest", "sha256:" + "4" * 64
            ),
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected=expected):
                value = _enabled_v2_primitive()
                artifact = value["providers"][0]["platforms"][0]["qualification"][
                    "artifact"
                ]
                mutate(artifact)
                _rehash_artifact(artifact)
                self.assert_invalid(value, expected)

    def test_artifact_binds_exact_trellis_compatibility_digest(self) -> None:
        value = _enabled_v2_primitive()
        artifact = value["providers"][0]["platforms"][0]["qualification"]["artifact"]
        artifact["trellisCompatibilityDigest"] = "sha256:" + "5" * 64
        _rehash_artifact(artifact)

        self.assert_invalid(value, "Trellis compatibility digest")

    def test_artifact_requires_harness_pin(self) -> None:
        value = _enabled_v2_primitive()
        artifact = value["providers"][0]["platforms"][0]["qualification"]["artifact"]
        artifact["harnessDigest"] = "not-a-digest"
        result = decode_compatibility_bundle_primitive(_rehash_root(value))

        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.INVALID_HASH, result.issues[0].reason_code)

        value = _enabled_v2_primitive()
        artifact = value["providers"][0]["platforms"][0]["qualification"]["artifact"]
        del artifact["harnessVersion"]
        result = decode_compatibility_bundle_primitive(_rehash_root(value))
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.MISSING_FIELD, result.issues[0].reason_code)

    def test_artifact_requires_exact_four_proven_scenarios(self) -> None:
        cases = {
            "required field": lambda scenarios: scenarios.pop("cleanup"),
            "must have passed": lambda scenarios: scenarios["cleanup"].__setitem__(
                "status", "fixture_ci_only"
            ),
            "must be live": lambda scenarios: scenarios["crash_reconcile"].__setitem__(
                "live", False
            ),
            "scenario set": lambda scenarios: scenarios[
                "active_turn_cancellation"
            ].__setitem__("name", "full_turn"),
        }
        for expected, mutate in cases.items():
            with self.subTest(expected=expected):
                value = _enabled_v2_primitive()
                artifact = value["providers"][0]["platforms"][0]["qualification"][
                    "artifact"
                ]
                mutate(artifact["scenarios"])
                _rehash_artifact(artifact)
                self.assert_invalid(value, expected)

    def test_concurrency_is_bounded_by_observation_and_overlap_evidence(self) -> None:
        no_overlap = _enabled_v2_primitive(
            max_concurrent_turns=2,
            observed_max_concurrent_turns=2,
        )
        self.assert_invalid(no_overlap, "disjoint sibling overlap evidence")

        unobserved_limit = _enabled_v2_primitive(
            max_concurrent_turns=2,
            observed_max_concurrent_turns=1,
            overlap=_overlap(),
        )
        self.assert_invalid(unobserved_limit, "cannot exceed")

        qualified = _enabled_v2_primitive(
            max_concurrent_turns=2,
            observed_max_concurrent_turns=3,
            overlap=_overlap(),
        )
        result = decode_compatibility_bundle_primitive(qualified)
        self.assertTrue(result.ok, result.report.render_text())

    def test_overlap_evidence_is_structured_and_fail_closed(self) -> None:
        mutations = {
            "between 2 and 64": lambda overlap: overlap.__setitem__(
                "observedConcurrentTurns", 1
            ),
            "at least two": lambda overlap: overlap.__setitem__(
                "siblingTaskIds", ["task-a"]
            ),
            "duplicate": lambda overlap: overlap.__setitem__(
                "siblingTaskIds", ["task-a", "task-a"]
            ),
            "cover every observed": lambda overlap: (
                overlap.__setitem__("observedConcurrentTurns", 3),
                overlap.__setitem__("siblingTaskIds", ["task-a", "task-b"]),
            ),
            "disjoint owned paths": lambda overlap: overlap.__setitem__(
                "ownedPathsDisjoint", False
            ),
            "have been observed": lambda overlap: overlap.__setitem__(
                "overlapObserved", False
            ),
        }
        for expected, mutate in mutations.items():
            with self.subTest(expected=expected):
                overlap = _overlap()
                mutate(overlap)
                value = _enabled_v2_primitive(
                    max_concurrent_turns=2,
                    observed_max_concurrent_turns=2,
                    overlap=overlap,
                )
                self.assert_invalid(value, expected)

        serial_with_overlap = _enabled_v2_primitive(overlap=_overlap())
        self.assert_invalid(serial_with_overlap, "must not claim sibling overlap")

        above_limit = _enabled_v2_primitive(
            max_concurrent_turns=65,
            observed_max_concurrent_turns=65,
            overlap=_overlap(observed=64),
        )
        self.assert_invalid(above_limit, "cannot exceed 64")

    def test_artifact_models_are_frozen_and_strictly_typed(self) -> None:
        result = decode_compatibility_bundle_primitive(_enabled_v2_primitive())
        self.assertTrue(result.ok, result.report.render_text())
        assert result.value is not None
        artifact = result.value.providers[0].platforms[0].qualification.artifact
        assert artifact is not None

        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.max_concurrent_turns = 2  # type: ignore[misc]
        with self.assertRaisesRegex(TypeError, "QualificationScenarioEvidence"):
            dataclasses.replace(artifact, scenarios=(object(),))
        with self.assertRaisesRegex(TypeError, "QualificationScenario"):
            QualificationScenarioEvidence(
                evidence_digest=_digest({"scenario": "invalid"}),
                live=True,
                name="full_turn",  # type: ignore[arg-type]
                status=QualificationStatus.PASSED,
            )


if __name__ == "__main__":
    unittest.main()
