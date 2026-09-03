from __future__ import annotations

import copy
import dataclasses
import unittest
from collections.abc import Callable

from wish_builder.compatibility import (
    load_bundled_compatibility,
    load_bundled_trellis_compatibility,
)
from wish_builder.contracts import (
    DecodeLimits,
    EvidenceScope,
    IntegrationCapabilities,
    OperationGuarantee,
    Platform,
    PlatformCompatibility,
    Provider,
    Qualification,
    QualificationStatus,
    ReasonCode,
    canonical_sha256,
    decode_compatibility_bundle_primitive,
)
from wish_builder.contracts import compatibility as compatibility_contract


def _digest(value: object) -> str:
    return "sha256:" + canonical_sha256(value)


def _rehash_capability(
    capability: IntegrationCapabilities, **changes: object
) -> IntegrationCapabilities:
    body = capability.body_primitive()
    primitive_names = {
        "launch_profile_digest": "launchProfileDigest",
        "platform": "platform",
        "policy_digest": "policyDigest",
        "provider": "provider",
    }
    for field_name, value in changes.items():
        primitive_name = primitive_names[field_name]
        body[primitive_name] = value.value if hasattr(value, "value") else value
    return dataclasses.replace(
        capability,
        capability_digest=_digest(body),
        **changes,
    )


def _rehash_cell_profile(
    cell: PlatformCompatibility, **profile_changes: object
) -> PlatformCompatibility:
    profile = dataclasses.replace(cell.launch_profile, **profile_changes)
    profile_digest = _digest(profile.to_primitive())
    capability = _rehash_capability(
        cell.capabilities,
        launch_profile_digest=profile_digest,
    )
    qualification = cell.qualification
    if qualification.artifact is not None:
        artifact_body = qualification.artifact.body_primitive()
        artifact_body["capabilityDigest"] = capability.capability_digest
        artifact_body["launchProfileDigest"] = profile_digest
        qualification = dataclasses.replace(
            qualification,
            artifact=dataclasses.replace(
                qualification.artifact,
                capability_digest=capability.capability_digest,
                artifact_digest=_digest(artifact_body),
                launch_profile_digest=profile_digest,
            ),
        )
    return dataclasses.replace(
        cell,
        capabilities=capability,
        launch_profile=profile,
        launch_profile_digest=profile_digest,
        qualification=qualification,
    )


class CompatibilityModelInvariantTests(unittest.TestCase):
    def test_graph_evidence_uses_official_core_adapter_path(self) -> None:
        evidence = compatibility_contract.GRAPH_ADAPTER_EVIDENCE

        self.assertEqual(
            (
                "wish_builder/bridges/trellis_core/graph-snapshot.mjs",
                "wish_builder/adapters/trellis/graph_snapshot.py",
                "wish_builder/adapters/trellis/graph.py",
                "wish_builder/services/trellis_graph_admission.py",
                "tests/node/trellis-graph-snapshot.test.mjs",
                "tests/adapters/test_trellis_graph_snapshot.py",
                "tests/adapters/test_trellis_graph_import.py",
            ),
            evidence,
        )
        self.assertNotIn(
            "tests/services/test_trellis_graph_admission.py",
            evidence,
        )
        self.assertEqual(len(evidence), len(set(evidence)))

    def assert_rejected(
        self,
        cases: tuple[tuple[str, Callable[[], object], type[BaseException], str], ...],
    ) -> None:
        for name, operation, error_type, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(error_type, message):
                operation()

    def test_scalar_collection_and_launch_boundaries_reject_lookalikes(self) -> None:
        bundle = load_bundled_compatibility()
        package = load_bundled_trellis_compatibility().packages[0]
        sdk = bundle.providers[0].sdk
        profile = bundle.providers[0].platforms[0].launch_profile

        self.assert_rejected(
            (
                (
                    "sha256 type",
                    lambda: dataclasses.replace(package, sha256=17),
                    ValueError,
                    "full sha256",
                ),
                (
                    "size bool",
                    lambda: dataclasses.replace(package, size=True),
                    TypeError,
                    "integer",
                ),
                (
                    "size zero",
                    lambda: dataclasses.replace(package, size=0),
                    ValueError,
                    "positive safe",
                ),
                (
                    "unsafe size",
                    lambda: dataclasses.replace(
                        package,
                        size=compatibility_contract.MAX_SAFE_JSON_INTEGER + 1,
                    ),
                    ValueError,
                    "positive safe",
                ),
                (
                    "non-semver package",
                    lambda: dataclasses.replace(package, version="01.0.0"),
                    ValueError,
                    "semantic version",
                ),
                (
                    "uppercase sha1",
                    lambda: dataclasses.replace(sdk, shasum="A" * 40),
                    ValueError,
                    "lowercase SHA-1",
                ),
                (
                    "argv list",
                    lambda: dataclasses.replace(profile, args=["app-server"]),
                    TypeError,
                    "tuple",
                ),
                (
                    "empty argv",
                    lambda: dataclasses.replace(profile, args=()),
                    ValueError,
                    "must not be empty",
                ),
                (
                    "oversized argv",
                    lambda: dataclasses.replace(
                        profile, args=tuple(f"arg-{index}" for index in range(17))
                    ),
                    ValueError,
                    "item limit",
                ),
                (
                    "platform string",
                    lambda: dataclasses.replace(profile, platform="linux"),
                    TypeError,
                    "Platform",
                ),
                (
                    "fresh session integer",
                    lambda: dataclasses.replace(profile, fresh_session=1),
                    TypeError,
                    "bool",
                ),
                (
                    "resumed session",
                    lambda: dataclasses.replace(profile, resume=True),
                    ValueError,
                    "fresh non-resumed",
                ),
                (
                    "package without shasum",
                    lambda: dataclasses.replace(profile, package="sdk@1.0.0"),
                    ValueError,
                    "present together",
                ),
                (
                    "shasum without package",
                    lambda: dataclasses.replace(profile, package_shasum="a" * 40),
                    ValueError,
                    "present together",
                ),
            )
        )

    def test_trellis_adapter_qualification_is_exact_and_digest_bound(self) -> None:
        trellis = load_bundled_trellis_compatibility()
        adapter = trellis.adapter_qualification
        graph = adapter.graph
        projection = adapter.projection

        self.assert_rejected(
            (
                (
                    "graph format",
                    lambda: dataclasses.replace(graph, derived_format="graph.v2"),
                    ValueError,
                    "derived_format",
                ),
                (
                    "read-only snapshot",
                    lambda: dataclasses.replace(graph, read_only_snapshot=False),
                    ValueError,
                    "read_only_snapshot",
                ),
                (
                    "deterministic snapshot type",
                    lambda: dataclasses.replace(graph, deterministic_snapshot=1),
                    TypeError,
                    "bool",
                ),
                (
                    "graph evidence container",
                    lambda: dataclasses.replace(graph, evidence=list(graph.evidence)),
                    TypeError,
                    "tuple",
                ),
                (
                    "graph evidence order",
                    lambda: dataclasses.replace(
                        graph, evidence=tuple(reversed(graph.evidence))
                    ),
                    ValueError,
                    "canonical evidence",
                ),
                (
                    "cross-process CAS",
                    lambda: dataclasses.replace(projection, cross_process_cas=True),
                    ValueError,
                    "cross_process_cas must be False",
                ),
                (
                    "concurrent projection writers",
                    lambda: dataclasses.replace(
                        projection, concurrent_projection_writers_safe=True
                    ),
                    ValueError,
                    "concurrent_projection_writers_safe must be False",
                ),
                (
                    "required projection guard",
                    lambda: dataclasses.replace(projection, digest_guarded=False),
                    ValueError,
                    "digest_guarded",
                ),
                (
                    "projection evidence order",
                    lambda: dataclasses.replace(
                        projection, evidence=tuple(reversed(projection.evidence))
                    ),
                    ValueError,
                    "canonical evidence",
                ),
                (
                    "adapter graph type",
                    lambda: dataclasses.replace(adapter, graph=object()),
                    TypeError,
                    "TrellisGraphAdapterQualification",
                ),
                (
                    "adapter projection type",
                    lambda: dataclasses.replace(adapter, projection=object()),
                    TypeError,
                    "TrellisProjectionAdapterQualification",
                ),
                (
                    "adapter schema",
                    lambda: dataclasses.replace(adapter, schema_version=2),
                    ValueError,
                    "schema_version",
                ),
                (
                    "adapter version",
                    lambda: dataclasses.replace(adapter, trellis_version="0.6.14"),
                    ValueError,
                    "unsupported Trellis",
                ),
                (
                    "adapter digest",
                    lambda: dataclasses.replace(
                        adapter, qualification_digest="sha256:" + "0" * 64
                    ),
                    ValueError,
                    "qualification_digest",
                ),
                (
                    "root adapter type",
                    lambda: dataclasses.replace(
                        trellis, adapter_qualification=object()
                    ),
                    TypeError,
                    "TrellisAdapterQualification",
                ),
            )
        )

    def test_policy_features_and_operation_guarantees_are_exact_booleans(self) -> None:
        bundle = load_bundled_compatibility()
        policy = bundle.policy
        capability = bundle.providers[0].platforms[0].capabilities

        policy_cases: list[
            tuple[str, Callable[[], object], type[BaseException], str]
        ] = [
            (
                "schema",
                lambda: dataclasses.replace(policy, schema_version=2),
                ValueError,
                "schema_version",
            ),
            (
                "scheduler",
                lambda: dataclasses.replace(policy, scheduler_mode="trellis"),
                ValueError,
                "scheduler_mode",
            ),
            (
                "credential owner",
                lambda: dataclasses.replace(
                    policy, credentials_managed_by="coordinator"
                ),
                ValueError,
                "credentials_managed_by",
            ),
            (
                "packet limit",
                lambda: dataclasses.replace(policy, max_task_packet_bytes=1),
                ValueError,
                "max_task_packet_bytes",
            ),
            (
                "boolean lookalike",
                lambda: dataclasses.replace(policy, fresh_attempt_worktree=1),
                TypeError,
                "bool",
            ),
        ]
        for field_name in (
            "fresh_attempt_worktree",
            "fresh_provider_session",
            "one_provider_per_run",
        ):
            policy_cases.append(
                (
                    field_name,
                    lambda field_name=field_name: dataclasses.replace(
                        policy, **{field_name: False}
                    ),
                    ValueError,
                    field_name,
                )
            )
        for field_name in ("provider_fallback", "provider_native_sibling_scheduling"):
            policy_cases.append(
                (
                    field_name,
                    lambda field_name=field_name: dataclasses.replace(
                        policy, **{field_name: True}
                    ),
                    ValueError,
                    field_name,
                )
            )
        self.assert_rejected(tuple(policy_cases))

        for field_name in dataclasses.asdict(capability.features):
            with (
                self.subTest(feature=field_name),
                self.assertRaisesRegex(ValueError, field_name),
            ):
                dataclasses.replace(
                    capability.features,
                    **{field_name: False},
                )
        with self.assertRaisesRegex(TypeError, "bool"):
            dataclasses.replace(capability.features, fresh_provider_sessions=1)
        with self.assertRaisesRegex(ValueError, "idempotent"):
            OperationGuarantee(idempotent=False, inspectable=True)
        with self.assertRaisesRegex(ValueError, "inspectable"):
            OperationGuarantee(idempotent=True, inspectable=False)
        with self.assertRaisesRegex(TypeError, "bool"):
            OperationGuarantee(idempotent=1, inspectable=True)

    def test_qualification_state_matrix_blocks_inadequate_dispatch_evidence(
        self,
    ) -> None:
        valid = {
            "artifact": None,
            "enabled_for_dispatch": False,
            "evidence": ("fixture:verified",),
            "evidence_scope": EvidenceScope.DETERMINISTIC_FIXTURE_AND_CI,
            "live": False,
            "note": "Deterministic fixture evidence.",
            "status": QualificationStatus.FIXTURE_CI_ONLY,
        }

        def qualification(**changes: object) -> Qualification:
            return Qualification(**(valid | changes))  # type: ignore[arg-type]

        self.assert_rejected(
            (
                (
                    "enabled lookalike",
                    lambda: qualification(enabled_for_dispatch=1),
                    TypeError,
                    "bool",
                ),
                (
                    "evidence list",
                    lambda: qualification(evidence=["fixture:verified"]),
                    TypeError,
                    "tuple",
                ),
                (
                    "empty evidence",
                    lambda: qualification(evidence=()),
                    ValueError,
                    "must not be empty",
                ),
                (
                    "too much evidence",
                    lambda: qualification(
                        evidence=tuple(
                            f"fixture:{index}"
                            for index in range(
                                compatibility_contract.MAX_COMPATIBILITY_EVIDENCE + 1
                            )
                        )
                    ),
                    ValueError,
                    "item limit",
                ),
                (
                    "duplicate evidence",
                    lambda: qualification(evidence=("same", "same")),
                    ValueError,
                    "duplicates",
                ),
                (
                    "scope string",
                    lambda: qualification(
                        evidence_scope="deterministic_fixture_and_ci"
                    ),
                    TypeError,
                    "EvidenceScope",
                ),
                (
                    "status string",
                    lambda: qualification(status="fixture_ci_only"),
                    TypeError,
                    "QualificationStatus",
                ),
                (
                    "passed without live evidence",
                    lambda: qualification(
                        status=QualificationStatus.PASSED,
                        evidence_scope=EvidenceScope.STARTUP_AND_HANDSHAKE,
                    ),
                    ValueError,
                    "must contain live evidence",
                ),
                (
                    "passed with fixture scope",
                    lambda: qualification(
                        status=QualificationStatus.PASSED,
                        live=True,
                    ),
                    ValueError,
                    "invalid evidence scope",
                ),
                (
                    "fixture status marked live",
                    lambda: qualification(live=True),
                    ValueError,
                    "only passed",
                ),
                (
                    "blocked credentials with CI scope",
                    lambda: qualification(
                        status=QualificationStatus.BLOCKED_CREDENTIALS
                    ),
                    ValueError,
                    "fixture-only evidence",
                ),
                (
                    "fixture CI with fixture-only scope",
                    lambda: qualification(
                        evidence_scope=EvidenceScope.DETERMINISTIC_FIXTURE_ONLY
                    ),
                    ValueError,
                    "fixture-and-CI evidence",
                ),
                (
                    "dispatch with handshake only",
                    lambda: qualification(
                        enabled_for_dispatch=True,
                        evidence_scope=EvidenceScope.STARTUP_AND_HANDSHAKE,
                        live=True,
                        status=QualificationStatus.PASSED,
                    ),
                    ValueError,
                    "full-turn-and-cancellation",
                ),
            )
        )
        with self.assertRaisesRegex(ValueError, "qualification artifact"):
            qualification(
                enabled_for_dispatch=True,
                evidence_scope=EvidenceScope.FULL_TURN_AND_CANCELLATION,
                live=True,
                status=QualificationStatus.PASSED,
            )

    def test_capability_rejects_wrong_types_sets_and_digest_bindings(self) -> None:
        capability = load_bundled_compatibility().providers[0].platforms[0].capabilities
        self.assert_rejected(
            (
                (
                    "feature type",
                    lambda: dataclasses.replace(capability, features=object()),
                    TypeError,
                    "CapabilityFeatures",
                ),
                (
                    "packet limit",
                    lambda: dataclasses.replace(capability, max_task_packet_bytes=1),
                    ValueError,
                    "max_task_packet_bytes",
                ),
                (
                    "operation list",
                    lambda: dataclasses.replace(
                        capability, operations=list(capability.operations)
                    ),
                    TypeError,
                    "tuple",
                ),
                (
                    "malformed operation",
                    lambda: dataclasses.replace(capability, operations=((),)),
                    TypeError,
                    "invalid entry",
                ),
                (
                    "operation order",
                    lambda: dataclasses.replace(
                        capability, operations=tuple(reversed(capability.operations))
                    ),
                    ValueError,
                    "exact backend operation set",
                ),
                (
                    "platform string",
                    lambda: dataclasses.replace(capability, platform="linux"),
                    TypeError,
                    "Platform",
                ),
                (
                    "provider string",
                    lambda: dataclasses.replace(capability, provider="codex"),
                    TypeError,
                    "Provider",
                ),
                (
                    "schema",
                    lambda: dataclasses.replace(capability, schema_version=True),
                    ValueError,
                    "schema_version",
                ),
                (
                    "launch digest shape",
                    lambda: dataclasses.replace(
                        capability, launch_profile_digest="sha256:short"
                    ),
                    ValueError,
                    "full sha256",
                ),
                (
                    "policy digest shape",
                    lambda: dataclasses.replace(
                        capability, policy_digest="sha256:short"
                    ),
                    ValueError,
                    "full sha256",
                ),
                (
                    "body digest drift",
                    lambda: dataclasses.replace(
                        capability, capability_digest="sha256:" + "0" * 64
                    ),
                    ValueError,
                    "does not match",
                ),
            )
        )

    def test_platform_cell_binds_types_platform_and_both_digest_layers(self) -> None:
        bundle = load_bundled_compatibility()
        cell = bundle.providers[0].platforms[0]
        windows = Platform.WINDOWS
        windows_profile = dataclasses.replace(cell.launch_profile, platform=windows)
        wrong_platform_capability = _rehash_capability(
            cell.capabilities,
            launch_profile_digest=_digest(windows_profile.to_primitive()),
        )
        wrong_capability_digest = _rehash_capability(
            cell.capabilities,
            launch_profile_digest="sha256:" + "f" * 64,
        )

        self.assert_rejected(
            (
                (
                    "capability type",
                    lambda: dataclasses.replace(cell, capabilities=object()),
                    TypeError,
                    "IntegrationCapabilities",
                ),
                (
                    "profile type",
                    lambda: dataclasses.replace(cell, launch_profile=object()),
                    TypeError,
                    "LaunchProfile",
                ),
                (
                    "qualification type",
                    lambda: dataclasses.replace(cell, qualification=object()),
                    TypeError,
                    "Qualification",
                ),
                (
                    "platform string",
                    lambda: dataclasses.replace(cell, platform="linux"),
                    TypeError,
                    "Platform",
                ),
                (
                    "profile platform",
                    lambda: dataclasses.replace(cell, platform=windows),
                    ValueError,
                    "launch profile platform",
                ),
                (
                    "capability platform",
                    lambda: dataclasses.replace(
                        cell,
                        platform=windows,
                        launch_profile=windows_profile,
                        launch_profile_digest=_digest(windows_profile.to_primitive()),
                        capabilities=wrong_platform_capability,
                    ),
                    ValueError,
                    "capability platform",
                ),
                (
                    "profile digest body",
                    lambda: dataclasses.replace(
                        cell, launch_profile_digest="sha256:" + "0" * 64
                    ),
                    ValueError,
                    "does not match the launch profile",
                ),
                (
                    "capability profile digest",
                    lambda: dataclasses.replace(
                        cell, capabilities=wrong_capability_digest
                    ),
                    ValueError,
                    "capability launch profile digest",
                ),
            )
        )

    def test_provider_binds_cells_sdk_command_protocol_and_package(self) -> None:
        bundle = load_bundled_compatibility()
        provider = bundle.providers[0]
        cell = provider.platforms[0]
        mismatched_capability = _rehash_capability(
            cell.capabilities, provider=Provider.OMP
        )
        mismatched_cell = dataclasses.replace(cell, capabilities=mismatched_capability)
        command_cell = _rehash_cell_profile(cell, command="unexpected")
        protocol_cell = _rehash_cell_profile(cell, protocol="unexpected")
        metadata_cell = _rehash_cell_profile(
            cell,
            package="@openai/codex@1.0.0",
            package_shasum="a" * 40,
        )

        self.assert_rejected(
            (
                (
                    "platform list",
                    lambda: dataclasses.replace(
                        provider, platforms=list(provider.platforms)
                    ),
                    TypeError,
                    "PlatformCompatibility",
                ),
                (
                    "provider string",
                    lambda: dataclasses.replace(provider, provider="codex"),
                    TypeError,
                    "Provider",
                ),
                (
                    "sdk type",
                    lambda: dataclasses.replace(provider, sdk=object()),
                    TypeError,
                    "SdkPin",
                ),
                (
                    "platform order",
                    lambda: dataclasses.replace(
                        provider, platforms=tuple(reversed(provider.platforms))
                    ),
                    ValueError,
                    "canonical order",
                ),
                (
                    "sdk name",
                    lambda: dataclasses.replace(
                        provider,
                        sdk=dataclasses.replace(provider.sdk, name="wrong-sdk"),
                    ),
                    ValueError,
                    "SDK package name",
                ),
                (
                    "capability provider",
                    lambda: dataclasses.replace(
                        provider,
                        platforms=(mismatched_cell, provider.platforms[1]),
                    ),
                    ValueError,
                    "capability provider",
                ),
                (
                    "launch command",
                    lambda: dataclasses.replace(
                        provider, platforms=(command_cell, provider.platforms[1])
                    ),
                    ValueError,
                    "launch command",
                ),
                (
                    "launch protocol",
                    lambda: dataclasses.replace(
                        provider, platforms=(protocol_cell, provider.platforms[1])
                    ),
                    ValueError,
                    "launch protocol",
                ),
                (
                    "codex duplicate package metadata",
                    lambda: dataclasses.replace(
                        provider, platforms=(metadata_cell, provider.platforms[1])
                    ),
                    ValueError,
                    "must not duplicate SDK metadata",
                ),
            )
        )

        omp = bundle.providers[1]
        omp_cell = _rehash_cell_profile(
            omp.platforms[0], package="@oh-my-pi/pi-coding-agent@0.0.0"
        )
        with self.assertRaisesRegex(ValueError, "provider SDK pin"):
            dataclasses.replace(
                omp,
                platforms=(omp_cell, omp.platforms[1]),
            )

    def test_bundle_binds_exact_matrix_versions_policy_and_capabilities(self) -> None:
        bundle = load_bundled_compatibility()
        provider = bundle.providers[0]
        cell = provider.platforms[0]

        wrong_policy_capability = _rehash_capability(
            cell.capabilities, policy_digest="sha256:" + "e" * 64
        )
        wrong_policy_cell = dataclasses.replace(
            cell, capabilities=wrong_policy_capability
        )
        wrong_policy_provider = dataclasses.replace(
            provider,
            platforms=(wrong_policy_cell, provider.platforms[1]),
        )

        self.assert_rejected(
            (
                (
                    "policy type",
                    lambda: dataclasses.replace(bundle, policy=object()),
                    TypeError,
                    "CompatibilityPolicy",
                ),
                (
                    "provider list",
                    lambda: dataclasses.replace(
                        bundle, providers=list(bundle.providers)
                    ),
                    TypeError,
                    "ProviderCompatibility",
                ),
                (
                    "provider order",
                    lambda: dataclasses.replace(
                        bundle, providers=tuple(reversed(bundle.providers))
                    ),
                    ValueError,
                    "canonical order",
                ),
                (
                    "published bool",
                    lambda: dataclasses.replace(bundle, published=1),
                    TypeError,
                    "bool",
                ),
                (
                    "schema",
                    lambda: dataclasses.replace(bundle, schema_version=1),
                    ValueError,
                    "schema_version",
                ),
                (
                    "Trellis compatibility digest shape",
                    lambda: dataclasses.replace(
                        bundle, trellis_compatibility_digest="short"
                    ),
                    ValueError,
                    "full sha256",
                ),
                (
                    "policy digest shape",
                    lambda: dataclasses.replace(bundle, policy_digest="short"),
                    ValueError,
                    "full sha256",
                ),
                (
                    "policy digest binding",
                    lambda: dataclasses.replace(
                        bundle, policy_digest="sha256:" + "0" * 64
                    ),
                    ValueError,
                    "policy_digest",
                ),
                (
                    "capability policy digest",
                    lambda: dataclasses.replace(
                        bundle,
                        providers=(wrong_policy_provider, *bundle.providers[1:]),
                    ),
                    ValueError,
                    "capability policy digest",
                ),
                (
                    "bundle body digest",
                    lambda: dataclasses.replace(
                        bundle, bundle_digest="sha256:" + "0" * 64
                    ),
                    ValueError,
                    "bundle_digest",
                ),
            )
        )

        corrupted = copy.deepcopy(bundle)
        object.__setattr__(
            corrupted.providers[0].platforms[0].capabilities,
            "max_task_packet_bytes",
            compatibility_contract.MAX_TASK_PACKET_BYTES + 1,
        )
        with self.assertRaisesRegex(ValueError, "task packet limit"):
            dataclasses.replace(corrupted)


class CompatibilityDecoderInvariantTests(unittest.TestCase):
    def test_public_decoder_rejects_every_scalar_and_container_boundary(self) -> None:
        from tests.contracts.test_compatibility import _primitive

        cases: list[tuple[str, object, ReasonCode]] = []
        cases.append(("root object", [], ReasonCode.WRONG_CONTAINER_TYPE))

        missing = _primitive()
        missing.pop("bundleDigest")
        cases.append(("missing field", missing, ReasonCode.MISSING_FIELD))

        empty = _primitive()
        empty["providers"] = []
        cases.append(("empty array", empty, ReasonCode.EMPTY_COLLECTION))

        oversized = _primitive()
        oversized["providers"] = oversized["providers"] * 2
        cases.append(("array limit", oversized, ReasonCode.ITEM_LIMIT_EXCEEDED))

        mutations = (
            (
                "string type",
                lambda value: value["providers"][0]["sdk"].__setitem__("name", 17),
                ReasonCode.WRONG_PRIMITIVE_TYPE,
            ),
            (
                "empty string",
                lambda value: value["providers"][0]["sdk"].__setitem__("name", " "),
                ReasonCode.EMPTY_STRING,
            ),
            (
                "string limit",
                lambda value: value["providers"][0]["sdk"].__setitem__(
                    "name", "x" * 129
                ),
                ReasonCode.STRING_LIMIT_EXCEEDED,
            ),
            (
                "boolean type",
                lambda value: value.__setitem__("published", 0),
                ReasonCode.WRONG_PRIMITIVE_TYPE,
            ),
            (
                "integer type",
                lambda value: value["policy"].__setitem__("maxTaskPacketBytes", True),
                ReasonCode.WRONG_PRIMITIVE_TYPE,
            ),
            (
                "integer range",
                lambda value: value["policy"].__setitem__("maxTaskPacketBytes", 0),
                ReasonCode.INTEGER_OUT_OF_RANGE,
            ),
            (
                "schema type",
                lambda value: value.__setitem__("schemaVersion", "1"),
                ReasonCode.WRONG_PRIMITIVE_TYPE,
            ),
            (
                "schema version",
                lambda value: value.__setitem__("schemaVersion", 2),
                ReasonCode.UNSUPPORTED_SCHEMA_VERSION,
            ),
            (
                "sha256",
                lambda value: value.__setitem__("bundleDigest", "sha256:short"),
                ReasonCode.INVALID_HASH,
            ),
            (
                "sha1",
                lambda value: value["providers"][0]["sdk"].__setitem__(
                    "shasum", "G" * 40
                ),
                ReasonCode.INVALID_HASH,
            ),
        )
        for name, mutate, reason in mutations:
            value = _primitive()
            mutate(value)
            cases.append((name, value, reason))

        for name, value, reason in cases:
            with self.subTest(name=name):
                result = decode_compatibility_bundle_primitive(value)
                self.assertFalse(result.ok)
                self.assertEqual(reason, result.issues[0].reason_code)

    def test_shape_audit_and_limits_fail_before_model_construction(self) -> None:
        from tests.contracts.test_compatibility import _primitive

        result = decode_compatibility_bundle_primitive(
            _primitive(), limits=DecodeLimits(max_items=1)
        )
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.ITEM_LIMIT_EXCEEDED, result.issues[0].reason_code)
        with self.assertRaisesRegex(TypeError, "DecodeLimits"):
            decode_compatibility_bundle_primitive({}, limits=None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
