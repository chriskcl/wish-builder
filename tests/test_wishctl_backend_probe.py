from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from wish_builder.cli import wishctl
from wish_builder.services.backend_admission import current_platform


CODEX_INTEGRITY = (
    "sha512-i4dryj2Y1j+00Mb5n+0n71EYnTK9/KDc2cdFo/dXD0d1oTog2bhUssKDEIOnKmn"
    "Ef51P0Z/HJTWvTKw/UHyOvQ=="
)
ALT_INTEGRITY = (
    "sha512-l4E+B7hgXKWddRo8bC/eSue2aWZjEgJ9xIpf5p0Og+lq8a2TArCwJ0HCoCPCgaBP/"
    "tN4zbYH/wOwvx9pJpeLCA=="
)


def invoke(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = wishctl.main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class WishCtlBackendProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.sdk_root = self.root / "sdk"

    def write_codex(self, version: str = "0.149.0") -> None:
        package_name = "@openai/codex"
        package_root = self.sdk_root / "node_modules" / Path(package_name)
        entrypoint = package_root / "bin" / "codex.js"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        (self.sdk_root / "package.json").write_text(
            json.dumps({"dependencies": {package_name: version}}),
            encoding="utf-8",
        )
        (package_root / "package.json").write_text(
            json.dumps(
                {
                    "bin": {"codex": "bin/codex.js"},
                    "name": package_name,
                    "version": version,
                }
            ),
            encoding="utf-8",
        )
        (self.sdk_root / "package-lock.json").write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"dependencies": {package_name: version}},
                        "node_modules/@openai/codex": {
                            "integrity": CODEX_INTEGRITY,
                            "version": version,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    def probe(self) -> tuple[int, str, str]:
        return invoke(
            [
                "backend-probe",
                "--provider",
                "codex",
                "--provider-sdk-root",
                str(self.sdk_root),
            ]
        )

    def test_qualified_exact_version_reports_structured_identity(self) -> None:
        self.write_codex()

        code, stdout, stderr = self.probe()

        expected_code = 0 if current_platform().value == "windows" else 1
        self.assertEqual((expected_code, ""), (code, stderr))
        result = json.loads(stdout)
        self.assertEqual("0.149.0", result["backendVersion"])
        self.assertEqual("codex-app-server-v1", result["protocolProfile"])
        self.assertEqual("codex-app-server-jsonl-stdio", result["protocol"])
        self.assertEqual(expected_code == 0, result["enabledForDispatch"])
        self.assertEqual("qualified" if expected_code == 0 else "candidate", result["status"])

    def test_unknown_version_is_reported_and_cannot_dispatch(self) -> None:
        self.write_codex("0.150.0")

        code, stdout, stderr = self.probe()

        self.assertEqual((1, ""), (code, stderr))
        result = json.loads(stdout)
        self.assertEqual("unknown", result["status"])
        self.assertFalse(result["enabledForDispatch"])
        self.assertIn("no qualification record", result["reason"])

    def test_integrity_drift_is_reported_without_launching_the_backend(self) -> None:
        self.write_codex()
        lock_path = self.sdk_root / "package-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["packages"]["node_modules/@openai/codex"]["integrity"] = ALT_INTEGRITY
        lock_path.write_text(json.dumps(lock), encoding="utf-8")

        code, stdout, stderr = self.probe()

        self.assertEqual((1, ""), (code, stderr))
        result = json.loads(stdout)
        self.assertEqual("drift", result["status"])
        self.assertFalse(result["enabledForDispatch"])
        self.assertIn("integrity", result["reason"])

    def test_relative_root_and_duplicate_package_json_fail_closed(self) -> None:
        code, stdout, stderr = invoke(
            [
                "backend-probe",
                "--provider",
                "codex",
                "--provider-sdk-root",
                "relative-sdk",
            ]
        )
        self.assertEqual((2, ""), (code, stdout))
        self.assertIn("absolute path", stderr)

        self.write_codex()
        package = self.sdk_root / "node_modules" / "@openai" / "codex" / "package.json"
        package.write_text(
            '{"name":"@openai/codex","name":"@openai/codex","version":"0.149.0"}',
            encoding="utf-8",
        )
        code, stdout, stderr = self.probe()
        self.assertEqual((2, ""), (code, stdout))
        self.assertIn("duplicate JSON key", stderr)


if __name__ == "__main__":
    unittest.main()
