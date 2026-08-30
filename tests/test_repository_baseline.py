from __future__ import annotations

import tomllib
import unittest
import zipfile
from pathlib import Path

import wish_builder
from scripts.build_skill_zip import archive_bytes, distributable_files


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "wish-builder"


class RepositoryBaselineTests(unittest.TestCase):
    def test_python_version_has_one_value(self) -> None:
        pyproject = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(pyproject["project"]["version"], wish_builder.__version__)
        uv_lock = tomllib.loads(
            (REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8")
        )
        self.assertEqual(">=3.11", uv_lock["requires-python"])
        self.assertEqual(
            [("wish-builder", wish_builder.__version__)],
            [
                (package["name"], package["version"])
                for package in uv_lock["package"]
            ],
        )

    def test_skill_and_package_use_the_same_wishctl_source(self) -> None:
        package_source = (
            REPOSITORY_ROOT / "wish_builder" / "cli" / "wishctl.py"
        ).read_bytes()
        skill_source = (SKILL_ROOT / "scripts" / "wishctl.py").read_bytes()
        self.assertEqual(package_source, skill_source)

    def test_skill_zip_exactly_matches_distributable_files(self) -> None:
        expected = {
            path.relative_to(REPOSITORY_ROOT).as_posix(): archive_bytes(path)
            for path in distributable_files(SKILL_ROOT)
        }
        with zipfile.ZipFile(
            REPOSITORY_ROOT / "wish-builder-skill.zip"
        ) as archive:
            names = {
                name
                for name in archive.namelist()
                if not name.endswith("/") and "__pycache__" not in name
            }
            self.assertEqual(set(expected), names)
            for name, content in expected.items():
                self.assertEqual(content, archive.read(name), name)


if __name__ == "__main__":
    unittest.main()
