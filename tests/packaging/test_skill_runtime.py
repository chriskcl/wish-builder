from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_skill_zip import (
    REPOSITORY_ROOT,
    RuntimeDriftError,
    archive_bytes,
    assert_runtime_current,
    build,
    runtime_file_map,
    runtime_manifest_bytes,
    sync_runtime,
)
from wish_builder.contracts import decode_manifest_v2_bytes


class SkillRuntimePackagingTests(unittest.TestCase):
    def _fixture_repository(self, root: Path) -> None:
        files = {
            "wish_builder/__init__.py": b'__version__ = "test"\n',
            "wish_builder/__main__.py": (
                b"from wish_builder.cli.wishctl import main\n"
                b"raise SystemExit(main())\n"
            ),
            "wish_builder/cli/__init__.py": b"",
            "wish_builder/cli/wishctl.py": (
                b"def main():\n"
                b"    print('fixture runtime')\n"
                b"    return 0\n"
            ),
            "wish_builder/contracts/__init__.py": b"CONTRACT = 'owned'\n",
            "wish_builder/contracts/schema.json": b'{"schema":1}\n',
            "scripts/ci_backend_qualification.py": b"print('fixture qualification')\n",
            "wish-builder/SKILL.md": b"---\nname: fixture\ndescription: fixture\n---\n",
            "wish-builder/LICENSE": b"fixture license\n",
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def _run_clean_python(
        self, cwd: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-S", *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_generated_runtime_is_a_complete_source_identical_snapshot(self) -> None:
        assert_runtime_current(REPOSITORY_ROOT)
        manifest_path = (
            REPOSITORY_ROOT
            / "wish-builder"
            / "scripts"
            / "runtime-manifest.json"
        )
        self.assertEqual(runtime_manifest_bytes(), manifest_path.read_bytes())

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = {entry["destination"]: entry for entry in manifest["files"]}
        expected = {
            destination.as_posix(): source
            for source, destination in runtime_file_map()
        }
        package_sources = {
            path.relative_to(REPOSITORY_ROOT).as_posix()
            for path in (REPOSITORY_ROOT / "wish_builder").rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        }
        package_sources.add("scripts/ci_backend_qualification.py")
        self.assertEqual(set(expected), set(entries))
        manifest_sources = {entry["source"] for entry in entries.values()}
        self.assertEqual(package_sources, manifest_sources)
        self.assertIn("scripts/wish_builder/contracts/decoder.py", entries)
        self.assertIn(
            "scripts/wish_builder/compatibility/trellis-0.6.15.json",
            entries,
        )
        self.assertIn(
            "scripts/wish_builder/compatibility/backend-qualification-0.6.15.json",
            entries,
        )
        self.assertIn("scripts/wish_builder/kernel/validation.py", entries)
        self.assertIn("scripts/ci_backend_qualification.py", entries)

        skill_root = REPOSITORY_ROOT / "wish-builder"
        for destination, source in expected.items():
            source_bytes = source.read_bytes()
            canonical_source_bytes = archive_bytes(source)
            self.assertEqual(source_bytes, (skill_root / destination).read_bytes())
            self.assertEqual(
                source.relative_to(REPOSITORY_ROOT).as_posix(),
                entries[destination]["source"],
            )
            self.assertEqual(
                len(canonical_source_bytes), entries[destination]["source_size"]
            )
            self.assertEqual(
                hashlib.sha256(canonical_source_bytes).hexdigest(),
                entries[destination]["source_sha256"],
            )

    def test_manifest_and_zip_are_stable_across_text_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            lf_root = temporary_root / "lf"
            crlf_root = temporary_root / "crlf"
            self._fixture_repository(lf_root)
            self._fixture_repository(crlf_root)

            for path in crlf_root.rglob("*"):
                if path.is_file():
                    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

            sync_runtime(lf_root)
            sync_runtime(crlf_root)
            self.assertEqual(
                runtime_manifest_bytes(lf_root),
                runtime_manifest_bytes(crlf_root),
            )

            lf_zip = temporary_root / "lf.zip"
            crlf_zip = temporary_root / "crlf.zip"
            self.assertEqual(
                build(lf_zip, lf_root),
                build(crlf_zip, crlf_root),
            )
            self.assertEqual(lf_zip.read_bytes(), crlf_zip.read_bytes())

    def test_documented_manifest_v2_example_matches_the_closed_decoder(self) -> None:
        contracts = (
            REPOSITORY_ROOT
            / "wish-builder"
            / "references"
            / "artifact-contracts.md"
        ).read_text(encoding="utf-8")
        section = contracts.split("## Execution Manifest", 1)[1]
        match = re.search(r"```json\n(?P<manifest>.*?)\n```", section, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None

        raw = match.group("manifest").encode("utf-8") + b"\n"
        decoded = decode_manifest_v2_bytes(raw)
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None
        self.assertEqual(json.loads(raw), decoded.value.to_primitive())

    def test_build_fails_closed_for_stale_generated_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._fixture_repository(root)
            sync_runtime(root)

            generated = (
                root
                / "wish-builder"
                / "scripts"
                / "wish_builder"
                / "contracts"
                / "__init__.py"
            )
            generated.write_text("CONTRACT = 'stale'\n", encoding="utf-8")
            output = root / "stale.zip"
            with self.assertRaisesRegex(
                RuntimeDriftError, "stale generated runtime file"
            ):
                build(output, root)
            self.assertFalse(output.exists())

            sync_runtime(root)
            (root / "wish_builder" / "contracts" / "new.py").write_text(
                "NEW = True\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                RuntimeDriftError, "missing generated runtime file"
            ):
                build(output, root)
            self.assertFalse(output.exists())

    def test_sync_and_build_reject_npm_tarballs_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._fixture_repository(root)
            source_tarball = root / "wish_builder" / "vendor" / "trellis.TGZ"
            source_tarball.parent.mkdir(parents=True)
            source_tarball.write_bytes(b"not-a-package")
            with self.assertRaisesRegex(
                RuntimeDriftError, "authoritative runtime must not contain npm tarballs"
            ):
                sync_runtime(root)

            source_tarball.unlink()
            sync_runtime(root)
            skill_tarball = root / "wish-builder" / "vendor" / "trellis.tgz"
            skill_tarball.parent.mkdir(parents=True)
            skill_tarball.write_bytes(b"not-a-package")
            output = root / "skill.zip"
            with self.assertRaisesRegex(
                RuntimeDriftError, "Skill distribution must not contain npm tarballs"
            ):
                build(output, root)
            self.assertFalse(output.exists())

    def test_check_rejects_manifest_tampering_and_unexpected_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self._fixture_repository(root)
            sync_runtime(root)

            manifest = (
                root
                / "wish-builder"
                / "scripts"
                / "runtime-manifest.json"
            )
            manifest.write_bytes(manifest.read_bytes() + b" ")
            with self.assertRaisesRegex(RuntimeDriftError, "stale generated runtime manifest"):
                assert_runtime_current(root)

            sync_runtime(root)
            unexpected = (
                root
                / "wish-builder"
                / "scripts"
                / "wish_builder"
                / "unexpected.py"
            )
            unexpected.write_text("UNEXPECTED = True\n", encoding="utf-8")
            with self.assertRaisesRegex(
                RuntimeDriftError, "unexpected generated runtime file"
            ):
                assert_runtime_current(root)

    def test_zip_build_is_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.zip"
            second = root / "second.zip"
            first_digest = build(first)
            second_digest = build(second)
            self.assertEqual(first_digest, second_digest)
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_committed_zip_matches_a_temporary_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            rebuilt = Path(temporary_directory) / "wish-builder-skill.zip"
            build(rebuilt)
            committed = REPOSITORY_ROOT / "wish-builder-skill.zip"
            self.assertEqual(
                committed.read_bytes(),
                rebuilt.read_bytes(),
                "run scripts/build_skill_zip.py and commit the exact rebuilt artifact",
            )

    def test_clean_extraction_executes_vendored_cli_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            archive_path = root / "skill.zip"
            build(archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(root / "extracted")

            skill_root = root / "extracted" / "wish-builder"
            scripts_root = skill_root / "scripts"
            manifest = json.loads(
                (scripts_root / "runtime-manifest.json").read_text(encoding="utf-8")
            )
            for entry in manifest["files"]:
                runtime_bytes = (skill_root / entry["destination"]).read_bytes()
                self.assertEqual(entry["runtime_size"], len(runtime_bytes))
                self.assertEqual(
                    entry["runtime_sha256"],
                    hashlib.sha256(runtime_bytes).hexdigest(),
                )

            direct = self._run_clean_python(
                root,
                str(scripts_root / "wishctl.py"),
                "--help",
            )
            self.assertEqual(0, direct.returncode, direct.stderr)
            self.assertIn("Validate and inspect wish-builder", direct.stdout)

            package_cli = self._run_clean_python(
                scripts_root, "-m", "wish_builder", "--help"
            )
            self.assertEqual(0, package_cli.returncode, package_cli.stderr)
            self.assertEqual(direct.stdout, package_cli.stdout)

            qualification_cli = self._run_clean_python(
                root,
                str(scripts_root / "ci_backend_qualification.py"),
                "--help",
            )
            self.assertEqual(0, qualification_cli.returncode, qualification_cli.stderr)
            self.assertIn(
                "untrusted qualification candidate",
                " ".join(qualification_cli.stdout.split()),
            )

            probe = self._run_clean_python(
                scripts_root,
                "-c",
                (
                    "import json, pathlib, wish_builder; "
                    "import wish_builder.contracts.decoder; "
                    "import wish_builder.contracts.runtime; "
                    "import wish_builder.kernel.validation; "
                    "print(json.dumps([str(pathlib.Path(wish_builder.__file__).resolve()), "
                    "str(pathlib.Path(wish_builder.contracts.decoder.__file__).resolve()), "
                    "str(pathlib.Path(wish_builder.contracts.runtime.__file__).resolve()), "
                    "str(pathlib.Path(wish_builder.kernel.validation.__file__).resolve())]))"
                ),
            )
            self.assertEqual(0, probe.returncode, probe.stderr)
            module_paths = [Path(value) for value in json.loads(probe.stdout)]
            for module_path in module_paths:
                self.assertTrue(module_path.is_relative_to(scripts_root), module_path)
                self.assertNotIn(str(REPOSITORY_ROOT), str(module_path))


if __name__ == "__main__":
    unittest.main()
