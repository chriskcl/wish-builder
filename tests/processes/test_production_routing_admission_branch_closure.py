from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.processes.test_production_routing import _ChannelFactory, capabilities
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts import WorkerProvider
from wish_builder.contracts.compatibility import Platform, Provider, SdkPin
from wish_builder.processes.production_routing import (
    AttemptBackendChannelRouter,
    ProviderSdkUnavailable,
    WishBuilderBackendAttemptChannelFactory,
    _package_lock_entry,
    _PROVIDER_SDK_SPECS,
    _read_json_object,
    _safe_absolute_directory,
    resolve_provider_sdk,
)


class ProductionRoutingAdmissionBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.cell = load_bundled_compatibility().platform(
            Provider.CODEX,
            Platform.WINDOWS,
        )
        self.spec = _PROVIDER_SDK_SPECS[WorkerProvider.CODEX]
        self.runtime = self.root / "node.exe"
        self.runtime.write_bytes(b"runtime")

    def _write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _sdk_tree(self, name: str) -> tuple[Path, Path]:
        sdk_root = self.root / name
        package_root = sdk_root / "node_modules" / Path(self.spec.package_name)
        entrypoint = package_root / self.spec.entrypoint
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        self._write_json(
            sdk_root / "package.json",
            {"dependencies": {self.spec.package_name: self.spec.version}},
        )
        self._write_json(
            package_root / "package.json",
            {
                "name": self.spec.package_name,
                "version": self.spec.version,
                "bin": {self.spec.bin_name: self.spec.entrypoint},
            },
        )
        self._write_json(
            sdk_root / "package-lock.json",
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {
                        "dependencies": {
                            self.spec.package_name: self.spec.version,
                        }
                    },
                    f"node_modules/{self.spec.package_name}": {
                        "version": self.spec.version,
                        "integrity": self.spec.integrity,
                    },
                },
            },
        )
        return sdk_root, package_root

    def test_directory_and_json_admission_guards(self) -> None:
        with self.assertRaisesRegex(ProviderSdkUnavailable, "absolute directory"):
            _safe_absolute_directory("relative-sdk", "sdk")

        missing = self.root / "missing.json"
        with self.assertRaisesRegex(ProviderSdkUnavailable, "missing or is a link"):
            _read_json_object(missing, "fixture")

        invalid = self.root / "invalid.json"
        invalid.write_text("{not-json", encoding="utf-8")
        with self.assertRaisesRegex(ProviderSdkUnavailable, "not valid JSON"):
            _read_json_object(invalid, "fixture")

        array = self.root / "array.json"
        self._write_json(array, [])
        with self.assertRaisesRegex(ProviderSdkUnavailable, "JSON object"):
            _read_json_object(array, "fixture")

    def test_package_lock_v1_and_v3_shapes_fail_closed(self) -> None:
        package_root = self.root / "sdk" / "node_modules" / Path(self.spec.package_name)
        lock_root = self.root / "sdk"

        self.assertIsNone(
            _package_lock_entry(
                {},
                self.spec.package_name,
                package_root,
                lock_root,
            )
        )

        direct = {"version": self.spec.version}
        self.assertIs(
            direct,
            _package_lock_entry(
                {
                    "packages": {},
                    "dependencies": {self.spec.package_name: direct},
                },
                self.spec.package_name,
                package_root,
                lock_root,
            ),
        )

        scoped = {"version": self.spec.version}
        self.assertIs(
            scoped,
            _package_lock_entry(
                {
                    "dependencies": {
                        "openai": {"codex": scoped},
                    }
                },
                self.spec.package_name,
                package_root,
                lock_root,
            ),
        )

        self.assertIsNone(
            _package_lock_entry(
                {
                    "packages": [],
                    "dependencies": {
                        "openai": {"codex": []},
                        self.spec.package_name: [],
                    },
                },
                self.spec.package_name,
                package_root,
                lock_root,
            )
        )

    def test_sdk_pin_and_package_presence_guards(self) -> None:
        with self.assertRaisesRegex(TypeError, "PlatformCompatibility"):
            resolve_provider_sdk(object(), self.root)  # type: ignore[arg-type]

        with self.assertRaisesRegex(TypeError, "SdkPin"):
            resolve_provider_sdk(
                self.cell,
                self.root,
                sdk_pin=object(),  # type: ignore[arg-type]
            )

        mismatched = SdkPin(
            self.spec.package_name,
            self.spec.shasum,
            "0.149.1",
        )
        with self.assertRaisesRegex(ProviderSdkUnavailable, "admitted M1 pin"):
            resolve_provider_sdk(self.cell, self.root, sdk_pin=mismatched)

        exact = SdkPin(
            self.spec.package_name,
            self.spec.shasum,
            self.spec.version,
        )
        with self.assertRaisesRegex(ProviderSdkUnavailable, "not installed"):
            resolve_provider_sdk(self.cell, self.root, sdk_pin=exact)

    def test_package_manifest_entrypoint_guards(self) -> None:
        wrong_name_root, wrong_name_package = self._sdk_tree("wrong-name")
        self._write_json(
            wrong_name_package / "package.json",
            {
                "name": "@example/not-codex",
                "version": self.spec.version,
                "bin": {self.spec.bin_name: self.spec.entrypoint},
            },
        )
        with self.assertRaisesRegex(ProviderSdkUnavailable, "package name"):
            resolve_provider_sdk(
                self.cell,
                wrong_name_root,
                runtime_executable=self.runtime,
            )

        invalid_bin_root, invalid_bin_package = self._sdk_tree("invalid-bin")
        self._write_json(
            invalid_bin_package / "package.json",
            {
                "name": self.spec.package_name,
                "version": self.spec.version,
                "bin": 1,
            },
        )
        with self.assertRaisesRegex(ProviderSdkUnavailable, "entrypoint"):
            resolve_provider_sdk(
                self.cell,
                invalid_bin_root,
                runtime_executable=self.runtime,
            )

        string_bin_root, string_bin_package = self._sdk_tree("string-bin")
        self._write_json(
            string_bin_package / "package.json",
            {
                "name": self.spec.package_name,
                "version": self.spec.version,
                "bin": self.spec.entrypoint,
            },
        )
        (string_bin_package / self.spec.entrypoint).unlink()
        with self.assertRaisesRegex(ProviderSdkUnavailable, "missing or is a link"):
            resolve_provider_sdk(
                self.cell,
                string_bin_root,
                runtime_executable=self.runtime,
            )

    def test_lockfile_and_runtime_guards(self) -> None:
        no_entry_root, _ = self._sdk_tree("no-lock-entry")
        self._write_json(
            no_entry_root / "package-lock.json",
            {
                "packages": {
                    "": {
                        "dependencies": {
                            self.spec.package_name: self.spec.version,
                        }
                    }
                }
            },
        )
        with self.assertRaisesRegex(ProviderSdkUnavailable, "no entry"):
            resolve_provider_sdk(
                self.cell,
                no_entry_root,
                runtime_executable=self.runtime,
            )

        wrong_version_root, _ = self._sdk_tree("wrong-lock-version")
        self._write_json(
            wrong_version_root / "package-lock.json",
            {
                "packages": {
                    "": {
                        "dependencies": {
                            self.spec.package_name: self.spec.version,
                        }
                    },
                    f"node_modules/{self.spec.package_name}": {
                        "version": "0.149.1",
                        "integrity": self.spec.integrity,
                    },
                }
            },
        )
        with self.assertRaisesRegex(ProviderSdkUnavailable, "provider version"):
            resolve_provider_sdk(
                self.cell,
                wrong_version_root,
                runtime_executable=self.runtime,
            )

        root_drift_root, _ = self._sdk_tree("root-lock-drift")
        self._write_json(
            root_drift_root / "package-lock.json",
            {
                "packages": {
                    "": {
                        "dependencies": {
                            self.spec.package_name: "@latest",
                        }
                    },
                    f"node_modules/{self.spec.package_name}": {
                        "version": self.spec.version,
                        "integrity": self.spec.integrity,
                    },
                }
            },
        )
        with self.assertRaisesRegex(ProviderSdkUnavailable, "root dependency"):
            resolve_provider_sdk(
                self.cell,
                root_drift_root,
                runtime_executable=self.runtime,
            )

        no_root_entry, _ = self._sdk_tree("no-root-lock-entry")
        self._write_json(
            no_root_entry / "package-lock.json",
            {
                "packages": {
                    f"node_modules/{self.spec.package_name}": {
                        "version": self.spec.version,
                        "integrity": self.spec.integrity,
                    }
                }
            },
        )
        with self.assertRaisesRegex(ProviderSdkUnavailable, "not installed"):
            resolve_provider_sdk(
                self.cell,
                no_root_entry,
                runtime_executable="",
            )

        relative_runtime_root, _ = self._sdk_tree("relative-runtime")
        with self.assertRaisesRegex(ProviderSdkUnavailable, "absolute executable"):
            resolve_provider_sdk(
                self.cell,
                relative_runtime_root,
                runtime_executable="node.exe",
            )

        wrong_runtime = self.root / "wrong.exe"
        wrong_runtime.write_bytes(b"runtime")
        wrong_runtime_root, _ = self._sdk_tree("wrong-runtime")
        with self.assertRaisesRegex(ProviderSdkUnavailable, "exact node runtime"):
            resolve_provider_sdk(
                self.cell,
                wrong_runtime_root,
                runtime_executable=wrong_runtime,
            )

        valid_root, valid_package = self._sdk_tree("valid")
        resolution = resolve_provider_sdk(
            self.cell,
            valid_root,
            runtime_executable=self.runtime,
        )
        self.assertEqual(valid_package, resolution.package_root)
        self.assertEqual(self.runtime, resolution.runtime)

    def test_factory_and_router_constructor_guards(self) -> None:
        with self.assertRaisesRegex(ValueError, "aliases"):
            WishBuilderBackendAttemptChannelFactory(
                compatibility_cell=self.cell,
                provider_sdk_root=self.root,
                sdk_root=self.root,
            )
        with self.assertRaisesRegex(ValueError, "state_root"):
            WishBuilderBackendAttemptChannelFactory(
                compatibility_cell=self.cell,
                state_root="relative-state",
            )
        with self.assertRaisesRegex(TypeError, "mapping"):
            WishBuilderBackendAttemptChannelFactory(
                compatibility_cell=self.cell,
                channel_constructors=object(),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(TypeError, "lifecycle_factory"):
            AttemptBackendChannelRouter(
                (),
                expected_capabilities=capabilities(),
                channel_factory=_ChannelFactory(),
                lifecycle_factory=object(),  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
