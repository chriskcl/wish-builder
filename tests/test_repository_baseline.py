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
        self.assertEqual(128, len(support["sdk_archive"]["sha512"]))
        self.assertEqual("pass", support["baseline_qualification"]["status"])
        self.assertEqual(2, support["baseline_qualification"]["tests_passed"])
        self.assertEqual(
            "06a44ce4c1067383b65690fd8d5b1699e6b501e26fcf76afeb88c24107c60fa8",
            support["baseline_qualification"]["repeat_publish_sha256"],
        )

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

        package_props = ElementTree.parse(
            REPOSITORY_ROOT / "Directory.Packages.props"
        ).getroot()
        versions = {
            item.attrib["Include"]: item.attrib["Version"]
            for group in package_props.findall("ItemGroup")
            for item in group.findall("PackageVersion")
        }
        self.assertEqual(
            {
                "Microsoft.NET.Test.Sdk": "[18.9.0]",
                "MSTest.TestAdapter": "[4.3.3]",
                "MSTest.TestFramework": "[4.3.3]",
            },
            versions,
        )

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

        service_lock = json.loads(
            (
                REPOSITORY_ROOT
                / "src"
                / "WishBuilder.CredentialService"
                / "packages.lock.json"
            ).read_text(encoding="utf-8")
        )
        service_packages = service_lock["dependencies"]["net10.0-windows7.0"]
        self.assertEqual(
            "10.0.11",
            service_packages["Microsoft.NET.ILLink.Tasks"]["resolved"],
        )

        test_lock = json.loads(
            (
                REPOSITORY_ROOT
                / "tests"
                / "dotnet"
                / "WishBuilder.CredentialService.Tests"
                / "packages.lock.json"
            ).read_text(encoding="utf-8")
        )
        test_packages = test_lock["dependencies"]["net10.0-windows7.0"]
        self.assertEqual(
            {
                "Microsoft.NET.Test.Sdk": ("[18.9.0, 18.9.0]", "18.9.0"),
                "MSTest.TestAdapter": ("[4.3.3, 4.3.3]", "4.3.3"),
                "MSTest.TestFramework": ("[4.3.3, 4.3.3]", "4.3.3"),
            },
            {
                name: (
                    test_packages[name]["requested"],
                    test_packages[name]["resolved"],
                )
                for name in (
                    "Microsoft.NET.Test.Sdk",
                    "MSTest.TestAdapter",
                    "MSTest.TestFramework",
                )
            },
        )

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
