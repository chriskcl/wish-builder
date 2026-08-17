from __future__ import annotations

import json
import tomllib
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import wish_builder


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "wish-builder"


class RepositoryBaselineTests(unittest.TestCase):
    def test_python_version_has_one_value(self) -> None:
        pyproject = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(pyproject["project"]["version"], wish_builder.__version__)

    def test_dotnet_sdk_and_support_evidence_are_coherent(self) -> None:
        global_json = json.loads(
            (REPOSITORY_ROOT / "global.json").read_text(encoding="utf-8")
        )
        support = json.loads(
            (
                REPOSITORY_ROOT
                / "release"
                / "provenance"
                / "dotnet-support.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("10.0.400", global_json["sdk"]["version"])
        self.assertEqual("disable", global_json["sdk"]["rollForward"])
        self.assertIs(False, global_json["sdk"]["allowPrerelease"])
        self.assertEqual(global_json["sdk"]["version"], support["selected_sdk"])
        self.assertEqual("active", support["support_phase"])
        self.assertEqual("2028-11-14", support["eol_date"])

    def test_dotnet_projects_inherit_the_locked_baseline(self) -> None:
        build_props = ElementTree.parse(
            REPOSITORY_ROOT / "Directory.Build.props"
        ).getroot()
        values = {
            child.tag: child.text
            for group in build_props.findall("PropertyGroup")
            for child in group
        }
        self.assertEqual("net10.0-windows", values["TargetFramework"])
        self.assertEqual("true", values["TreatWarningsAsErrors"])
        self.assertEqual("true", values["RestorePackagesWithLockFile"])

        service = ElementTree.parse(
            REPOSITORY_ROOT
            / "src"
            / "WishBuilder.CredentialService"
            / "WishBuilder.CredentialService.csproj"
        ).getroot()
        service_values = {
            child.tag: child.text
            for group in service.findall("PropertyGroup")
            for child in group
        }
        self.assertEqual("win-x64", service_values["RuntimeIdentifier"])
        self.assertEqual("true", service_values["SelfContained"])
        self.assertEqual("true", service_values["PublishSingleFile"])
        self.assertEqual("false", service_values["PublishTrimmed"])

    def test_skill_and_package_use_the_same_wishctl_source(self) -> None:
        package_source = (
            REPOSITORY_ROOT / "wish_builder" / "cli" / "wishctl.py"
        ).read_bytes()
        skill_source = (SKILL_ROOT / "scripts" / "wishctl.py").read_bytes()
        self.assertEqual(package_source, skill_source)

    def test_skill_zip_exactly_matches_distributable_files(self) -> None:
        expected = {
            path.relative_to(REPOSITORY_ROOT).as_posix(): path.read_bytes()
            for path in SKILL_ROOT.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
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
