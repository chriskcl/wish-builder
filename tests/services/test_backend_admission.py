from __future__ import annotations

import dataclasses
import unittest
from unittest import mock

from tests.adapters.test_trellis_graph_import import (
    payload,
    settings,
    snapshot,
)
from tests.contracts.test_compatibility_v2 import (
    _disabled_v2_primitive,
    _enabled_v2_primitive,
    _overlap,
    _rehash_root,
)
from wish_builder.adapters.trellis import import_trellis_snapshot
from wish_builder.compatibility import (
    load_bundled_compatibility,
    load_bundled_trellis_compatibility,
)
from wish_builder.contracts import decode_compatibility_bundle_primitive
from wish_builder.contracts.compatibility import (
    CompatibilityBundle,
    EvidenceScope,
    Platform,
    Provider,
    QualificationStatus,
)
from wish_builder.contracts.manifest_v2 import WorkerProvider
from wish_builder.contracts.serialization import canonical_sha256
from wish_builder.services import backend_admission as backend_admission_module
from wish_builder.services.backend_admission import (
    BackendAdmissionReason,
    admit_backend,
    current_platform,
)


class BackendAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = load_bundled_compatibility()

    def manifest(
        self,
        provider: WorkerProvider,
        platform: Platform,
        *,
        max_concurrency: int = 2,
        bundle: CompatibilityBundle | None = None,
    ):
        selected_bundle = self.bundle if bundle is None else bundle
        compatibility_provider = {
            WorkerProvider.CODEX: Provider.CODEX,
            WorkerProvider.OH_MY_PI: Provider.OMP,
            WorkerProvider.PI: Provider.PI,
        }[provider]
        cell = selected_bundle.platform(compatibility_provider, platform)
        base_settings = settings()
        value = dataclasses.replace(
            base_settings,
            provider=provider,
            capability_digest=cell.capabilities.capability_digest,
            launch_profile_digest=cell.launch_profile_digest,
            policy_digest=selected_bundle.policy_digest,
            execution_budget=dataclasses.replace(
                base_settings.execution_budget,
                max_concurrent_workers=max_concurrency,
            ),
            max_concurrency=max_concurrency,
        )
        return import_trellis_snapshot(snapshot(payload()), value).manifest

    def qualified_bundle(
        self,
        *,
        max_concurrent_turns: int = 1,
        observed_max_concurrent_turns: int = 1,
        published: bool = True,
    ) -> CompatibilityBundle:
        overlap = (
            None
            if max_concurrent_turns == observed_max_concurrent_turns == 1
            else _overlap(observed=observed_max_concurrent_turns)
        )
        decoded = decode_compatibility_bundle_primitive(
            _enabled_v2_primitive(
                max_concurrent_turns=max_concurrent_turns,
                observed_max_concurrent_turns=observed_max_concurrent_turns,
                overlap=overlap,
                published=published,
            )
        )
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None
        return decoded.value

    def test_only_published_codex_windows_cell_is_admitted(self) -> None:
        for provider in WorkerProvider:
            for platform in Platform:
                with self.subTest(provider=provider, platform=platform):
                    result = admit_backend(
                        self.manifest(provider, platform),
                        bundle=self.bundle,
                        platform=platform,
                    )
                    expected = (
                        provider is WorkerProvider.CODEX
                        and platform is Platform.WINDOWS
                    )
                    self.assertEqual(expected, result.admitted)
                    self.assertEqual(
                        (
                            BackendAdmissionReason.NONE
                            if expected
                            else BackendAdmissionReason.DISPATCH_NOT_QUALIFIED
                        ),
                        result.reason,
                    )

    def test_bundled_codex_windows_concurrency_limit_is_enforced(self) -> None:
        for max_concurrency, admitted, reason in (
            (1, True, BackendAdmissionReason.NONE),
            (2, True, BackendAdmissionReason.NONE),
            (3, False, BackendAdmissionReason.CONCURRENCY_NOT_QUALIFIED),
        ):
            with self.subTest(max_concurrency=max_concurrency):
                result = admit_backend(
                    self.manifest(
                        WorkerProvider.CODEX,
                        Platform.WINDOWS,
                        max_concurrency=max_concurrency,
                    ),
                    bundle=self.bundle,
                    platform=Platform.WINDOWS,
                )
                self.assertEqual(admitted, result.admitted)
                self.assertEqual(reason, result.reason)

    def test_final_boundary_revalidates_exact_nested_types(self) -> None:
        self.assertIsNone(backend_admission_module._revalidate_artifact(object()))
        self.assertIsNone(
            backend_admission_module._revalidate_qualification(object())
        )

        scenarios_bundle = self.qualified_bundle()
        scenarios_artifact = scenarios_bundle.platform(
            Provider.CODEX,
            Platform.LINUX,
        ).qualification.artifact
        assert scenarios_artifact is not None
        object.__setattr__(scenarios_artifact, "scenarios", (object(),))
        self.assertIsNone(
            backend_admission_module._revalidate_artifact(scenarios_artifact)
        )

        overlap_bundle = self.qualified_bundle(
            max_concurrent_turns=2,
            observed_max_concurrent_turns=2,
        )
        overlap_artifact = overlap_bundle.platform(
            Provider.CODEX,
            Platform.LINUX,
        ).qualification.artifact
        assert overlap_artifact is not None
        object.__setattr__(overlap_artifact, "disjoint_sibling_overlap", object())
        self.assertIsNone(
            backend_admission_module._revalidate_artifact(overlap_artifact)
        )

    def test_disabled_qualification_and_official_digest_mismatch_fail_closed(
        self,
    ) -> None:
        primitive = _disabled_v2_primitive()
        primitive["published"] = True
        decoded = decode_compatibility_bundle_primitive(_rehash_root(primitive))
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None
        disabled_bundle = decoded.value
        disabled = admit_backend(
            self.manifest(
                WorkerProvider.CODEX,
                Platform.LINUX,
                max_concurrency=1,
                bundle=disabled_bundle,
            ),
            bundle=disabled_bundle,
            platform=Platform.LINUX,
        )
        self.assertEqual(
            BackendAdmissionReason.DISPATCH_NOT_QUALIFIED,
            disabled.reason,
        )

        enabled_bundle = self.qualified_bundle()
        mismatched_trellis = mock.Mock(
            compatibility_digest="sha256:" + "9" * 64,
        )
        with mock.patch.object(
            backend_admission_module,
            "load_bundled_trellis_compatibility",
            return_value=mismatched_trellis,
        ):
            mismatched = admit_backend(
                self.manifest(
                    WorkerProvider.CODEX,
                    Platform.LINUX,
                    max_concurrency=1,
                    bundle=enabled_bundle,
                ),
                bundle=enabled_bundle,
                platform=Platform.LINUX,
            )
        self.assertEqual(
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            mismatched.reason,
        )

    def test_each_frozen_digest_is_checked_before_qualification(self) -> None:
        manifest = self.manifest(WorkerProvider.CODEX, Platform.WINDOWS)
        cases = (
            (
                dataclasses.replace(manifest, policy_digest="sha256:" + "1" * 64),
                BackendAdmissionReason.POLICY_MISMATCH,
            ),
            (
                dataclasses.replace(
                    manifest,
                    capability_digest="sha256:" + "2" * 64,
                ),
                BackendAdmissionReason.CAPABILITY_MISMATCH,
            ),
            (
                dataclasses.replace(
                    manifest,
                    launch_profile_digest="sha256:" + "3" * 64,
                ),
                BackendAdmissionReason.LAUNCH_PROFILE_MISMATCH,
            ),
        )
        for candidate, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    reason,
                    admit_backend(
                        candidate,
                        bundle=self.bundle,
                        platform=Platform.WINDOWS,
                    ).reason,
                )

    def test_unknown_host_is_rejected_before_bundle_selection(self) -> None:
        manifest = self.manifest(WorkerProvider.PI, Platform.LINUX)
        with mock.patch(
            "wish_builder.services.backend_admission.host_platform.system",
            return_value="Darwin",
        ):
            self.assertIsNone(current_platform())
            result = admit_backend(manifest, bundle=self.bundle)
        self.assertEqual(BackendAdmissionReason.UNSUPPORTED_HOST, result.reason)

    def test_serial_evidence_never_admits_a_parallel_manifest(self) -> None:
        bundle = self.qualified_bundle()
        manifest = self.manifest(
            WorkerProvider.CODEX,
            Platform.LINUX,
            max_concurrency=2,
            bundle=bundle,
        )

        result = admit_backend(
            manifest,
            bundle=bundle,
            platform=Platform.LINUX,
        )

        self.assertFalse(result.admitted)
        self.assertEqual(
            BackendAdmissionReason.CONCURRENCY_NOT_QUALIFIED,
            result.reason,
        )

    def test_unpublished_bundle_never_admits_complete_qualification(self) -> None:
        bundle = self.qualified_bundle(published=False)
        manifest = self.manifest(
            WorkerProvider.CODEX,
            Platform.LINUX,
            max_concurrency=1,
            bundle=bundle,
        )

        result = admit_backend(
            manifest,
            bundle=bundle,
            platform=Platform.LINUX,
        )

        self.assertFalse(result.admitted)
        self.assertEqual(
            BackendAdmissionReason.DISPATCH_NOT_QUALIFIED,
            result.reason,
        )

    def test_qualified_backend_workers_do_not_require_projection_cas(self) -> None:
        bundle = self.qualified_bundle(
            max_concurrent_turns=2,
            observed_max_concurrent_turns=2,
        )
        for max_concurrency in (1, 2):
            with self.subTest(max_concurrency=max_concurrency):
                result = admit_backend(
                    self.manifest(
                        WorkerProvider.CODEX,
                        Platform.LINUX,
                        max_concurrency=max_concurrency,
                        bundle=bundle,
                    ),
                    bundle=bundle,
                    platform=Platform.LINUX,
                )

                self.assertTrue(result.admitted)
                self.assertEqual(BackendAdmissionReason.NONE, result.reason)

    def test_backend_admission_binds_trellis_without_reading_projection_caps(
        self,
    ) -> None:
        bundle = self.qualified_bundle(
            max_concurrent_turns=2,
            observed_max_concurrent_turns=2,
        )
        trellis = load_bundled_trellis_compatibility()
        projection = trellis.adapter_qualification.projection
        digest_only_record = mock.Mock()
        digest_only_record.compatibility_digest = trellis.compatibility_digest
        type(digest_only_record).adapter_qualification = mock.PropertyMock(
            side_effect=AssertionError(
                "backend admission must not inspect projection capabilities"
            )
        )
        with mock.patch(
            "wish_builder.services.backend_admission."
            "load_bundled_trellis_compatibility",
            return_value=digest_only_record,
        ):
            result = admit_backend(
                self.manifest(
                    WorkerProvider.CODEX,
                    Platform.LINUX,
                    max_concurrency=2,
                    bundle=bundle,
                ),
                bundle=bundle,
                platform=Platform.LINUX,
            )

        self.assertTrue(result.admitted)
        self.assertEqual(BackendAdmissionReason.NONE, result.reason)
        self.assertTrue(projection.single_writer)
        self.assertFalse(projection.cross_process_cas)
        self.assertFalse(projection.concurrent_projection_writers_safe)

    def test_backend_binding_rejects_different_trellis_compatibility_digest(
        self,
    ) -> None:
        bundle = self.qualified_bundle()
        artifact = bundle.platform(
            Provider.CODEX,
            Platform.LINUX,
        ).qualification.artifact
        assert artifact is not None
        object.__setattr__(
            artifact,
            "trellis_compatibility_digest",
            "sha256:" + "9" * 64,
        )
        object.__setattr__(
            artifact,
            "artifact_digest",
            "sha256:" + canonical_sha256(artifact.body_primitive()),
        )

        result = admit_backend(
            self.manifest(
                WorkerProvider.CODEX,
                Platform.LINUX,
                max_concurrency=1,
                bundle=bundle,
            ),
            bundle=bundle,
            platform=Platform.LINUX,
        )

        self.assertFalse(result.admitted)
        self.assertEqual(
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            result.reason,
        )

    def test_backend_bundle_rejects_different_official_trellis_digest(self) -> None:
        bundle = self.qualified_bundle()
        object.__setattr__(
            bundle,
            "trellis_compatibility_digest",
            "sha256:" + "9" * 64,
        )

        result = admit_backend(
            self.manifest(
                WorkerProvider.CODEX,
                Platform.LINUX,
                max_concurrency=1,
                bundle=bundle,
            ),
            bundle=bundle,
            platform=Platform.LINUX,
        )

        self.assertFalse(result.admitted)
        self.assertEqual(
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            result.reason,
        )

    def test_concurrency_limit_is_enforced_independently_of_projection(self) -> None:
        bundle = self.qualified_bundle(
            max_concurrent_turns=2,
            observed_max_concurrent_turns=2,
        )
        rejected = admit_backend(
            self.manifest(
                WorkerProvider.CODEX,
                Platform.LINUX,
                max_concurrency=3,
                bundle=bundle,
            ),
            bundle=bundle,
            platform=Platform.LINUX,
        )

        self.assertFalse(rejected.admitted)
        self.assertEqual(
            BackendAdmissionReason.CONCURRENCY_NOT_QUALIFIED,
            rejected.reason,
        )

    def test_forged_artifact_is_revalidated_at_admission(self) -> None:
        bundle = self.qualified_bundle()
        artifact = bundle.platform(
            Provider.CODEX,
            Platform.LINUX,
        ).qualification.artifact
        assert artifact is not None
        object.__setattr__(artifact, "policy_digest", "sha256:" + "9" * 64)

        result = admit_backend(
            self.manifest(
                WorkerProvider.CODEX,
                Platform.LINUX,
                max_concurrency=1,
                bundle=bundle,
            ),
            bundle=bundle,
            platform=Platform.LINUX,
        )

        self.assertFalse(result.admitted)
        self.assertEqual(
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            result.reason,
        )

    def test_enabled_flag_without_artifact_still_fails_closed(self) -> None:
        primitive = _disabled_v2_primitive()
        primitive["published"] = True
        decoded = decode_compatibility_bundle_primitive(_rehash_root(primitive))
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None
        bundle = decoded.value
        qualification = bundle.platform(
            Provider.CODEX,
            Platform.LINUX,
        ).qualification
        object.__setattr__(qualification, "enabled_for_dispatch", True)

        result = admit_backend(
            self.manifest(
                WorkerProvider.CODEX,
                Platform.LINUX,
                max_concurrency=1,
                bundle=bundle,
            ),
            bundle=bundle,
            platform=Platform.LINUX,
        )

        self.assertFalse(result.admitted)
        self.assertEqual(
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            result.reason,
        )

    def test_tampered_parent_qualification_is_revalidated_at_admission(self) -> None:
        bundle = self.qualified_bundle()
        qualification = bundle.platform(
            Provider.CODEX,
            Platform.LINUX,
        ).qualification
        object.__setattr__(
            qualification,
            "status",
            QualificationStatus.FIXTURE_CI_ONLY,
        )
        object.__setattr__(
            qualification,
            "evidence_scope",
            EvidenceScope.DETERMINISTIC_FIXTURE_AND_CI,
        )
        object.__setattr__(qualification, "live", False)

        result = admit_backend(
            self.manifest(
                WorkerProvider.CODEX,
                Platform.LINUX,
                max_concurrency=1,
                bundle=bundle,
            ),
            bundle=bundle,
            platform=Platform.LINUX,
        )

        self.assertFalse(result.admitted)
        self.assertEqual(
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            result.reason,
        )

    def test_valid_artifact_from_another_cell_cannot_be_transplanted(self) -> None:
        bundle = self.qualified_bundle()
        linux_artifact = bundle.platform(
            Provider.CODEX,
            Platform.LINUX,
        ).qualification.artifact
        assert linux_artifact is not None
        windows_qualification = bundle.platform(
            Provider.CODEX,
            Platform.WINDOWS,
        ).qualification
        object.__setattr__(windows_qualification, "artifact", linux_artifact)
        object.__setattr__(windows_qualification, "enabled_for_dispatch", True)

        result = admit_backend(
            self.manifest(
                WorkerProvider.CODEX,
                Platform.WINDOWS,
                max_concurrency=1,
                bundle=bundle,
            ),
            bundle=bundle,
            platform=Platform.WINDOWS,
        )

        self.assertFalse(result.admitted)
        self.assertEqual(
            BackendAdmissionReason.QUALIFICATION_EVIDENCE_MISMATCH,
            result.reason,
        )


if __name__ == "__main__":
    unittest.main()
