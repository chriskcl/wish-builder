from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import manage_backend_versions
from wish_builder.compatibility import (
    bundled_backend_version_registry_bytes,
    load_bundled_backend_version_registry,
)
from wish_builder.services.backend_version_registry import (
    backend_version_registry_pin_bytes,
)


INTEGRITY = (
    "sha512-i4dryj2Y1j+00Mb5n+0n71EYnTK9/KDc2cdFo/dXD0d1oTog2bhUssKDEIOnKmn"
    "Ef51P0Z/HJTWvTKw/UHyOvQ=="
)


class ManageBackendVersionsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        compatibility = self.root / "wish_builder" / "compatibility"
        compatibility.mkdir(parents=True)
        self.registry = load_bundled_backend_version_registry()
        (compatibility / "backend-version-registry.json").write_bytes(
            bundled_backend_version_registry_bytes()
        )
        (compatibility / "_backend_version_registry_pin.py").write_bytes(
            backend_version_registry_pin_bytes(self.registry.registry_digest)
        )

    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = manage_backend_versions.main(
                ["--repository-root", str(self.root), *arguments]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_candidate_command_updates_only_the_external_registry_pair(self) -> None:
        code, stdout, stderr = self.invoke(
            [
                "candidate",
                "--provider",
                "codex",
                "--platform",
                "windows",
                "--backend-version",
                "0.150.0",
                "--expected-registry-digest",
                self.registry.registry_digest,
                "--note",
                "Qualification pending.",
                "--protocol-profile",
                "codex-app-server-v1",
                "--npm-shasum",
                "a" * 40,
                "--npm-integrity",
                INTEGRITY,
            ]
        )

        self.assertEqual((0, ""), (code, stderr))
        report = json.loads(stdout)
        self.assertEqual("candidate", report["status"])
        self.assertEqual("published", report["publicationState"])
        self.assertNotEqual(self.registry.registry_digest, report["registryDigest"])
        self.assertEqual(
            {
                "_backend_version_registry_pin.py",
                "backend-version-registry.json",
            },
            {
                path.name
                for path in (self.root / "wish_builder" / "compatibility").iterdir()
            },
        )

    def test_stale_digest_exits_without_writing(self) -> None:
        before = (
            self.root
            / "wish_builder"
            / "compatibility"
            / "backend-version-registry.json"
        ).read_bytes()

        code, stdout, stderr = self.invoke(
            [
                "quarantine",
                "--provider",
                "codex",
                "--platform",
                "windows",
                "--backend-version",
                "0.149.0",
                "--expected-registry-digest",
                "sha256:" + "0" * 64,
                "--note",
                "Emergency quarantine.",
                "--review-reference",
                "incident:stale",
            ]
        )

        self.assertEqual((1, ""), (code, stdout))
        self.assertIn("registry_digest_conflict", stderr)
        self.assertEqual(
            before,
            (
                self.root
                / "wish_builder"
                / "compatibility"
                / "backend-version-registry.json"
            ).read_bytes(),
        )


if __name__ == "__main__":
    unittest.main()
