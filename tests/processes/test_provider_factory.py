from __future__ import annotations

from wish_builder.adapters.fakes import FakeBackendChannelPort

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from tests.contracts.test_compatibility_v2 import (
    SCENARIOS,
    _digest,
    _disabled_v2_primitive,
    _rehash_artifact,
    _rehash_root,
)
from tests.processes.test_production_routing import attempt_identity, attempt_worktree
from wish_builder.contracts import (
    BackendVersionRegistry,
    BackendVersionStatus,
    QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
    WorkerProvider,
    canonical_sha256,
    decode_compatibility_bundle_primitive,
)
from wish_builder.compatibility import load_bundled_backend_version_registry
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.processes.production_routing import (
    ProviderSdkUnavailable,
    WishBuilderBackendAttemptChannelFactory,
)


CODEX_INTEGRITY = (
    "sha512-i4dryj2Y1j+00Mb5n+0n71EYnTK9/KDc2cdFo/dXD0d1oTog2bhUssKDEIOnKmn"
    "Ef51P0Z/HJTWvTKw/UHyOvQ=="
)
PI_INTEGRITY = (
    "sha512-l4E+B7hgXKWddRo8bC/eSue2aWZjEgJ9xIpf5p0Og+lq8a2TArCwJ0HCoCPCgaBP/"
    "tN4zbYH/wOwvx9pJpeLCA=="
)
OMP_INTEGRITY = (
    "sha512-RMLu7DrF/W2lEPNgQECGR1Uw6jbhAKnDUVGGhhRXvVPp3ntx8CCwW48aC2kfp5QV/"
    "lDFYg0Rw6/CXMo/85jIBw=="
)
SDK_FIXTURES = {
    Provider.CODEX: (
        WorkerProvider.CODEX,
        "@openai/codex",
        "0.149.0",
        "codex",
        "bin/codex.js",
        CODEX_INTEGRITY,
    ),
    Provider.PI: (
        WorkerProvider.PI,
        "@earendil-works/pi-coding-agent",
        "0.84.2",
        "pi",
        "dist/cli.js",
        PI_INTEGRITY,
    ),
    Provider.OMP: (
        WorkerProvider.OH_MY_PI,
        "@oh-my-pi/pi-coding-agent",
        "17.4.0",
        "omp",
        "dist/cli.js",
        OMP_INTEGRITY,
    ),
}


def qualified_registry(selected_provider: Provider, platform: Platform):
    bundled = load_bundled_backend_version_registry()
    records = tuple(
        dataclasses.replace(
            item,
            status=BackendVersionStatus.QUALIFIED,
            max_concurrency=2,
            evidence_digest="sha256:" + "1" * 64,
            publication_receipt_digest="sha256:" + "2" * 64,
            review_reference="test-review:provider-factory",
            note="Test-only qualified backend version.",
        )
        if item.provider is selected_provider and item.platform is platform
        else item
        for item in bundled.records
    )
    body = {
        "profiles": [item.to_primitive() for item in bundled.profiles],
        "records": [item.to_primitive() for item in records],
        "schemaVersion": bundled.schema_version,
    }
    return BackendVersionRegistry(
        schema_version=bundled.schema_version,
        profiles=bundled.profiles,
        records=records,
        registry_digest="sha256:" + canonical_sha256(body),
    )


def enabled_cell(selected_provider: Provider):
    value = _disabled_v2_primitive()
    provider = next(
        item for item in value["providers"] if item["provider"] == selected_provider.value
    )
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
    qualification["artifact"] = _rehash_artifact(
        {
            "artifactDigest": "sha256:" + "0" * 64,
            "capabilityDigest": cell["capabilities"]["capabilityDigest"],
            "disjointSiblingOverlap": None,
            "harnessDigest": _digest({"harness": "provider-factory-test"}),
            "harnessVersion": "test",
            "launchProfileDigest": cell["launchProfileDigest"],
            "maxConcurrentTurns": 1,
            "observedMaxConcurrentTurns": 1,
            "platform": cell["platform"],
            "policyDigest": value["policyDigest"],
            "provider": provider["provider"],
            "scenarios": {
                scenario: {
                    "evidenceDigest": _digest(
                        {"provider": provider["provider"], "scenario": scenario}
                    ),
                    "live": True,
                    "name": scenario,
                    "status": "passed",
                }
                for scenario in SCENARIOS
            },
            "schemaVersion": QUALIFICATION_ARTIFACT_SCHEMA_VERSION,
            "sdk": provider["sdk"],
            "trellisCompatibilityDigest": value["trellisCompatibilityDigest"],
        }
    )
    decoded = decode_compatibility_bundle_primitive(_rehash_root(value))
    if not decoded.ok or decoded.value is None:  # pragma: no cover - fixture guard
        raise AssertionError(decoded.report.render_text())
    return decoded.value.platform(selected_provider, Platform.LINUX)


class ProviderFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.sdk_root = self.root / "sdk"
        self.state_root = self.root / "provider-state"
        self.node_runtime = self.root / "node.exe"
        self.bun_runtime = self.root / "bun.exe"
        self.node_runtime.write_bytes(b"runtime")
        self.bun_runtime.write_bytes(b"runtime")
        self.attempts = (
            attempt_worktree(self.root / "attempt-one", attempt_identity(1)),
            attempt_worktree(self.root / "attempt-two", attempt_identity(2)),
        )

    def write_sdk(
        self,
        provider: Provider = Provider.CODEX,
        *,
        version: str | None = None,
        integrity: str | None = None,
        dependency_version: str | None = None,
    ) -> Path:
        _, package_name, pinned_version, bin_name, bin_path, pinned_integrity = (
            SDK_FIXTURES[provider]
        )
        installed_version = pinned_version if version is None else version
        installed_integrity = pinned_integrity if integrity is None else integrity
        declared_version = (
            pinned_version if dependency_version is None else dependency_version
        )
        package_root = self.sdk_root / "node_modules" / Path(package_name)
        entrypoint = package_root / bin_path
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("#!/usr/bin/env runtime\n", encoding="utf-8")
        (self.sdk_root / "package.json").write_text(
            json.dumps({"dependencies": {package_name: declared_version}}),
            encoding="utf-8",
        )
        (package_root / "package.json").write_text(
            json.dumps(
                {
                    "name": package_name,
                    "version": installed_version,
                    "bin": {bin_name: bin_path},
                }
            ),
            encoding="utf-8",
        )
        (self.sdk_root / "package-lock.json").write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"dependencies": {package_name: declared_version}},
                        f"node_modules/{package_name}": {
                            "version": installed_version,
                            "integrity": installed_integrity,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        return package_root

    def factory(self, provider: Provider = Provider.CODEX, **overrides):
        runtime = self.bun_runtime if provider is Provider.OMP else self.node_runtime
        cell = enabled_cell(provider)
        values = {
            "compatibility_cell": cell,
            "provider_sdk_root": self.sdk_root,
            "state_root": self.state_root,
            "runtime_executable": runtime,
            "registry": qualified_registry(provider, cell.platform),
        }
        values.update(overrides)
        return WishBuilderBackendAttemptChannelFactory(**values)

    def test_missing_explicit_sdk_root_fails_closed(self) -> None:
        factory = self.factory(provider_sdk_root=self.root / "missing-sdk")

        with self.assertRaisesRegex(ProviderSdkUnavailable, "provider SDK root"):
            factory(self.attempts[0])

    def test_package_version_drift_is_rejected(self) -> None:
        self.write_sdk(version="0.149.1")

        with self.assertRaisesRegex(ProviderSdkUnavailable, "version drift"):
            self.factory()(self.attempts[0])

    def test_latest_dependency_is_rejected_even_when_installed_version_matches(self) -> None:
        self.write_sdk(dependency_version="@latest")

        with self.assertRaisesRegex(ProviderSdkUnavailable, "exact pinned version"):
            self.factory()(self.attempts[0])

    def test_missing_package_lock_is_rejected(self) -> None:
        self.write_sdk()
        (self.sdk_root / "package-lock.json").unlink()

        with self.assertRaisesRegex(ProviderSdkUnavailable, "package-lock.json"):
            self.factory()(self.attempts[0])

    def test_package_lock_integrity_drift_is_rejected(self) -> None:
        self.write_sdk(integrity="sha512-not-the-official-package")

        with self.assertRaisesRegex(ProviderSdkUnavailable, "integrity"):
            self.factory()(self.attempts[0])

    def test_attempt_state_paths_are_deterministic_disjoint_and_external(self) -> None:
        self.write_sdk()
        configs = []

        def construct(config):
            configs.append(config)
            return FakeBackendChannelPort(config.capabilities)

        factory = self.factory(
            channel_constructors={WorkerProvider.CODEX: construct}
        )

        factory(self.attempts[0])
        factory(self.attempts[1])

        first, second = (item.state_directory for item in configs)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_relative_to(self.state_root))
        self.assertTrue(second.is_relative_to(self.state_root))
        self.assertFalse(first.is_relative_to(Path(self.attempts[0].path)))
        self.assertFalse(second.is_relative_to(Path(self.attempts[1].path)))

    def test_constructs_codex_adapter_from_exact_entrypoint_without_model_call(self) -> None:
        package_root = self.write_sdk()
        configs = []

        def construct(config):
            configs.append(config)
            return FakeBackendChannelPort(config.capabilities)

        channel = self.factory(
            channel_constructors={WorkerProvider.CODEX: construct}
        )(self.attempts[0])

        self.assertIsInstance(channel, FakeBackendChannelPort)
        self.assertEqual(1, len(configs))
        config = configs[0]
        self.assertEqual(
            (
                str(self.node_runtime),
                str(package_root / "bin" / "codex.js"),
            ),
            config.launch.command_prefix,
        )
        self.assertEqual((), config.environment)
        self.assertFalse(config.state_directory.is_relative_to(config.working_directory))

    def test_explicit_package_directory_uses_ancestor_lock_for_integrity(self) -> None:
        package_root = self.write_sdk()

        channel = self.factory(provider_sdk_root=package_root)(self.attempts[0])

        from wish_builder.adapters.providers import CodexAppServerChannel

        self.assertIsInstance(channel, CodexAppServerChannel)

    def test_constructs_pi_and_omp_with_exact_node_and_bun_entrypoints(self) -> None:
        for provider in (Provider.PI, Provider.OMP):
            with self.subTest(provider=provider.value):
                # Each subcase receives a clean npm project root.
                if self.sdk_root.exists():
                    import shutil

                    shutil.rmtree(self.sdk_root)
                package_root = self.write_sdk(provider)
                worker, _, version, _, bin_path, _ = SDK_FIXTURES[provider]
                configs = []

                def construct(config):
                    configs.append(config)
                    return FakeBackendChannelPort(config.capabilities)

                channel = self.factory(
                    provider,
                    channel_constructors={worker: construct},
                )(self.attempts[0])

                self.assertIsInstance(channel, FakeBackendChannelPort)
                self.assertEqual(1, len(configs))
                config = configs[0]
                self.assertEqual(
                    (
                        str(
                            self.bun_runtime
                            if provider is Provider.OMP
                            else self.node_runtime
                        ),
                        str(package_root / bin_path),
                    ),
                    config.launch.command_prefix,
                )
                self.assertEqual(version, config.launch.sdk_version)
                self.assertEqual(worker, config.launch.provider)


if __name__ == "__main__":
    unittest.main()
