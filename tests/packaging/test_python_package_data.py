from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from importlib.resources import files
from pathlib import Path, PurePosixPath

from scripts.build_skill_zip import REPOSITORY_ROOT
from wish_builder.compatibility import (
    load_bundled_backend_qualification,
    load_bundled_compatibility,
    load_bundled_trellis_compatibility,
)
from wish_builder.contracts import canonical_json_bytes, canonical_sha256
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.contracts.qualification_evidence_decoder import (
    decode_qualification_evidence_inventory_bytes,
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
        self.assertIn(
            "compatibility/q/*/derived/sha256/*.json",
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

    def test_built_wheel_and_sdist_include_all_qualification_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            distribution = Path(temporary) / "dist"
            build_backend = Path(temporary) / "build-backend"
            source.mkdir()
            distribution.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(REPOSITORY_ROOT / name, source / name)
            shutil.copytree(REPOSITORY_ROOT / "wish_builder", source / "wish_builder")
            evidence_root = source / "wish_builder" / "compatibility" / "q"
            evidence_files = {
                path.relative_to(source).as_posix()
                for path in evidence_root.rglob("*")
                if path.is_file()
            }
            self.assertTrue(evidence_files)

            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--target",
                    str(build_backend),
                    "-r",
                    str(REPOSITORY_ROOT / "requirements" / "build.txt"),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                0,
                install.returncode,
                install.stdout + install.stderr,
            )

            build_script = """
import json
import sys
import setuptools
from setuptools.build_meta import build_sdist, build_wheel

distribution = sys.argv[1]
wheel_stage = sys.argv[2]
wheel = build_wheel(
    distribution,
    {"--build-option": [f"--bdist-dir={wheel_stage}"]},
)
sdist = build_sdist(distribution)
print(
    json.dumps(
        {
            "setuptools": setuptools.__version__,
            "wheel": wheel,
            "sdist": sdist,
        },
        sort_keys=True,
    )
)
"""
            build_environment = os.environ.copy()
            build_environment["PYTHONPATH"] = str(build_backend)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    build_script,
                    str(distribution),
                    str(Path(temporary) / "wheel-stage"),
                ],
                cwd=source,
                env=build_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                0,
                completed.returncode,
                completed.stdout + completed.stderr,
            )
            build_result = json.loads(completed.stdout.splitlines()[-1])
            self.assertEqual("83.0.0", build_result["setuptools"])
            wheel_name = build_result["wheel"]
            sdist_name = build_result["sdist"]

            with zipfile.ZipFile(distribution / wheel_name) as archive:
                wheel_members = set(archive.namelist())
            self.assertTrue(evidence_files.issubset(wheel_members))

            with tarfile.open(distribution / sdist_name, mode="r:gz") as archive:
                sdist_members = set(archive.getnames())
            sdist_root = sdist_name.removesuffix(".tar.gz")
            expected_sdist = {f"{sdist_root}/{path}" for path in evidence_files}
            self.assertTrue(expected_sdist.issubset(sdist_members))

    def test_published_backend_qualification_evidence_chain_is_complete(self) -> None:
        backend = load_bundled_backend_qualification()
        qualification = backend.platform(
            Provider.CODEX,
            Platform.WINDOWS,
        ).qualification
        self.assertIsNotNone(qualification.artifact)
        artifact = qualification.artifact
        assert artifact is not None

        references = dict(item.split(":", 1) for item in qualification.evidence)
        self.assertEqual(
            {
                "bundled-evidence",
                "candidate-artifact",
                "evidence-inventory",
                "event-log",
                "publication-receipt",
            },
            set(references),
        )
        evidence_reference = PurePosixPath(references["bundled-evidence"])
        evidence_root = files("wish_builder").joinpath(*evidence_reference.parts)

        candidate_raw = evidence_root.joinpath("candidate-artifact.json").read_bytes()
        self.assertEqual(canonical_json_bytes(artifact.to_primitive()), candidate_raw)
        self.assertEqual(artifact.artifact_digest, references["candidate-artifact"])

        inventory_raw = evidence_root.joinpath(
            "evidence", "inventory.json"
        ).read_bytes()
        inventory_result = decode_qualification_evidence_inventory_bytes(
            inventory_raw
        )
        self.assertTrue(inventory_result.ok, inventory_result.report.render_text())
        inventory = inventory_result.value
        assert inventory is not None
        self.assertEqual(inventory.digest(), references["evidence-inventory"])

        event_raw = evidence_root.joinpath("evidence", "events.jsonl").read_bytes()
        event_digest = "sha256:" + hashlib.sha256(event_raw).hexdigest()
        self.assertEqual(event_digest, references["event-log"])

        receipt_raw = evidence_root.joinpath("publication-receipt.json").read_bytes()
        receipt = json.loads(receipt_raw)
        self.assertEqual(canonical_json_bytes(receipt), receipt_raw)
        receipt_body = dict(receipt)
        stored_receipt_digest = receipt_body.pop("receiptDigest")
        receipt_digest = "sha256:" + canonical_sha256(receipt_body)
        self.assertEqual(stored_receipt_digest, receipt_digest)
        self.assertEqual(receipt_digest, references["publication-receipt"])
        self.assertEqual(evidence_reference.as_posix(), receipt["evidenceReference"])
        self.assertEqual(artifact.artifact_digest, receipt["candidateArtifactDigest"])
        self.assertEqual(inventory.digest(), receipt["evidenceInventoryDigest"])
        self.assertEqual(event_digest, receipt["eventLogDigest"])
        self.assertEqual(inventory.qualification_run_id, receipt["qualificationRunId"])
        self.assertEqual(artifact.provider.value, receipt["provider"])
        self.assertEqual(artifact.platform.value, receipt["platform"])

        provenance = json.loads(
            evidence_root.joinpath("evidence", "provenance.json").read_bytes()
        )
        self.assertEqual(provenance["sourceRevision"], receipt["sourceRevision"])

        derived_digests = {
            item.evidence_digest for item in artifact.scenarios
        }
        overlap = artifact.disjoint_sibling_overlap
        self.assertIsNotNone(overlap)
        assert overlap is not None
        derived_digests.add(overlap.evidence_digest)
        for digest in derived_digests:
            with self.subTest(digest=digest):
                derived_raw = evidence_root.joinpath(
                    "derived", "sha256", f"{digest.removeprefix('sha256:')}.json"
                ).read_bytes()
                self.assertEqual(
                    digest,
                    "sha256:" + hashlib.sha256(derived_raw).hexdigest(),
                )
                self.assertEqual(
                    canonical_json_bytes(json.loads(derived_raw)),
                    derived_raw,
                )


if __name__ == "__main__":
    unittest.main()
