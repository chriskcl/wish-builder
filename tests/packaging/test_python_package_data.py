from __future__ import annotations

import json
import tomllib
import unittest
from importlib.resources import files

from scripts.build_skill_zip import REPOSITORY_ROOT
from wish_builder.compatibility import (
    load_bundled_backend_qualification,
    load_bundled_compatibility,
    load_bundled_trellis_compatibility,
)


class PythonPackageDataTests(unittest.TestCase):
    def test_release_metadata_and_skill_use_the_same_gpl_v3_license(self) -> None:
        configuration = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = configuration["project"]
        self.assertEqual("GPL-3.0-only", project["license"])
        self.assertEqual(["LICENSE"], project["license-files"])
        self.assertEqual(
            "https://github.com/chriskcl/wish-builder",
            project["urls"]["Repository"],
        )

        repository_license = (REPOSITORY_ROOT / "LICENSE").read_bytes()
        skill_license = (REPOSITORY_ROOT / "wish-builder" / "LICENSE").read_bytes()
        self.assertEqual(repository_license, skill_license)
        self.assertIn(b"GNU GENERAL PUBLIC LICENSE", repository_license)
        self.assertIn(b"Version 3, 29 June 2007", repository_license)

    def test_compatibility_json_is_declared_and_importable_package_data(self) -> None:
        configuration = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertIn(
            "compatibility/*.json",
            configuration["tool"]["setuptools"]["package-data"]["wish_builder"],
        )
        compatibility_root = files("wish_builder.compatibility")
        for name in (
            "trellis-0.6.15.json",
            "backend-qualification-0.6.15.json",
        ):
            self.assertTrue(compatibility_root.joinpath(name).is_file(), name)
        trellis = load_bundled_trellis_compatibility()
        backend = load_bundled_backend_qualification()
        self.assertEqual("0.6.15", trellis.trellis_version)
        self.assertEqual(
            trellis.compatibility_digest,
            backend.trellis_compatibility_digest,
        )
        self.assertEqual(backend, load_bundled_compatibility())

    def test_trellis_core_bridge_is_declared_as_package_data(self) -> None:
        configuration = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        package_data = configuration["tool"]["setuptools"]["package-data"][
            "wish_builder"
        ]
        self.assertIn("bridges/trellis_core/*.json", package_data)
        self.assertIn("bridges/trellis_core/*.mjs", package_data)

        bridge_root = files("wish_builder").joinpath("bridges", "trellis_core")
        expected = {
            "bridge.mjs",
            "cli-loader.mjs",
            "cli-pins.json",
            "core-loader.mjs",
            "graph-snapshot.mjs",
            "pins.json",
            "projection.mjs",
            "protocol.mjs",
            "strict-json.mjs",
        }
        for name in expected:
            self.assertTrue(bridge_root.joinpath(name).is_file(), name)
        for name in {
            "bundled-runtime-driver.mjs",
            "runtime-driver.mjs",
            "unknown-runtime-driver.mjs",
        }:
            self.assertFalse(bridge_root.joinpath(name).is_file(), name)

        pins = json.loads(bridge_root.joinpath("pins.json").read_text(encoding="utf-8"))
        self.assertEqual("@mindfoldhq/trellis-core", pins["packageName"])
        self.assertEqual("0.6.15", pins["packageVersion"])
        self.assertEqual(
            "sha256:3af3e71fbaba3b4e7f081ca7df39dc9d00f9c527d855dc159263f4a34cf8587a",
            pins["archiveSha256"],
        )
        self.assertEqual(
            "sha512-UYMVMM47Zyr/ns39U/f620cs7XaFKX2yez91QMV40Eah+uxxEdGwYHgNjDPZxwMhlr/0TIsZuMM+KF6lcbxg9w==",
            pins["npmIntegrity"],
        )
        self.assertEqual(
            "sha256:49602e2bbd8a9f172c63e0bcd341810b3a70fe592adf1860c15a213898c790af",
            pins["packageTreeSha256"],
        )

        cli_pins = json.loads(
            bridge_root.joinpath("cli-pins.json").read_text(encoding="utf-8")
        )
        self.assertEqual("@mindfoldhq/trellis", cli_pins["packageName"])
        self.assertEqual("0.6.15", cli_pins["packageVersion"])
        self.assertEqual(
            "sha256:7b97e4247f54e71f22ff80caa328d9e68fb81908f984f15d70a4d81cc2a0306c",
            cli_pins["archiveSha256"],
        )
        self.assertEqual(
            "sha512-grbF8PToesHojsaWkoG4+Aupih7eZHkXH5y33uzPrWQXwIRewwlM1AoeJEttcXAia9nLZzF/ezuR338PWCKv+A==",
            cli_pins["npmIntegrity"],
        )
        self.assertEqual(
            "sha256:f11904ad9d93e2e0dfdb7add3e4eb2caf7dd41d388a4a396aef5bf8305bdcceb",
            cli_pins["packageTreeSha256"],
        )


if __name__ == "__main__":
    unittest.main()
