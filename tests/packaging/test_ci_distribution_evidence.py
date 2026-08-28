from __future__ import annotations

import hashlib
import io
import stat
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import scripts.ci_distribution_evidence as distribution_evidence
from scripts.ci_distribution_evidence import (
    DistributionEvidenceError,
    REPOSITORY_ROOT,
    _INSTALLED_PACKAGE_CHECK,
    build_clean_install_evidence,
    build_distribution_evidence,
    canonical_json_bytes,
)


class DistributionEvidenceTests(unittest.TestCase):
    def test_install_smoke_keeps_trellis_version_out_of_backend_record(self) -> None:
        self.assertIn("trellis.trellis_version == expected", _INSTALLED_PACKAGE_CHECK)
        self.assertIn(
            "package_version('wish-builder') == expected_version",
            _INSTALLED_PACKAGE_CHECK,
        )
        self.assertIn(
            "backend.trellis_compatibility_digest == trellis.compatibility_digest",
            _INSTALLED_PACKAGE_CHECK,
        )
        self.assertNotIn("backend.trellis_version", _INSTALLED_PACKAGE_CHECK)

    @staticmethod
    def _zip_bytes(members: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, content in members.items():
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        return output.getvalue()

    @staticmethod
    def _sdist_bytes(members: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, content in members.items():
                info = tarfile.TarInfo(name)
                info.mtime = 0
                info.mode = 0o644
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dist = self.root / "dist"
        self.dist.mkdir()
        self.wheel = self.dist / "wish_builder-0.1-py3-none-any.whl"
        self.sdist = self.dist / "wish_builder-0.1.tar.gz"
        self.skill = self.dist / "wish-builder-skill.zip"
        self.repeat = self.dist / "wish-builder-skill.repeat.zip"
        self.revision = "a" * 40
        self.build_evidence = self.root / "distribution-evidence.json"
        self._write_valid_archives()

    @staticmethod
    def _metadata_bytes() -> bytes:
        return (
            b"Metadata-Version: 2.4\n"
            b"Name: wish-builder\n"
            b"Version: 0.1\n"
            b"License-Expression: GPL-3.0-only\n"
            b"License-File: LICENSE\n\n"
        )

    @staticmethod
    def _license_bytes() -> bytes:
        return (REPOSITORY_ROOT / "LICENSE").read_bytes()

    def _valid_members(self, role: str) -> dict[str, bytes]:
        warning = (
            b"Trellis 0.7.0-dev.2 was withdrawn. "
            b"Never install or resolve @latest."
        )
        if role == "wheel":
            return {
                "wish_builder/__init__.py": b"__version__ = '0.1'\n",
                "wish_builder-0.1.dist-info/METADATA": self._metadata_bytes(),
                "wish_builder-0.1.dist-info/licenses/LICENSE": self._license_bytes(),
            }
        if role == "sdist":
            return {
                "wish_builder-0.1/README.md": warning,
                "wish_builder-0.1/PKG-INFO": self._metadata_bytes(),
                "wish_builder-0.1/LICENSE": self._license_bytes(),
            }
        return {
            "wish-builder/SKILL.md": warning,
            "wish-builder/LICENSE": self._license_bytes(),
        }

    def _write_valid_archives(self) -> None:
        self.wheel_bytes = self._zip_bytes(self._valid_members("wheel"))
        self.sdist_bytes = self._sdist_bytes(self._valid_members("sdist"))
        self.skill_bytes = self._zip_bytes(self._valid_members("skill_zip"))
        self.wheel.write_bytes(self.wheel_bytes)
        self.sdist.write_bytes(self.sdist_bytes)
        self.skill.write_bytes(self.skill_bytes)
        self.repeat.write_bytes(self.skill_bytes)

    def _write_archive(
        self,
        role: str,
        members: dict[str, bytes],
        *,
        include_required: bool = True,
    ) -> None:
        path = {
            "wheel": self.wheel,
            "sdist": self.sdist,
            "skill_zip": self.skill,
            "skill_zip_repeat": self.repeat,
        }[role]
        archive_members = self._valid_members(role) if include_required else {}
        archive_members.update(members)
        content = (
            self._sdist_bytes(archive_members)
            if role == "sdist"
            else self._zip_bytes(archive_members)
        )
        path.write_bytes(content)

    def _write_archive_with_symlink(self, role: str) -> None:
        path = {
            "wheel": self.wheel,
            "sdist": self.sdist,
            "skill_zip": self.skill,
            "skill_zip_repeat": self.repeat,
        }[role]
        members = self._valid_members(role)
        if role == "sdist":
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                for name, content in members.items():
                    info = tarfile.TarInfo(name)
                    info.mtime = 0
                    info.mode = 0o644
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                link = tarfile.TarInfo("wish_builder-0.1/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "../../outside"
                archive.addfile(link)
            path.write_bytes(output.getvalue())
            return

        output = io.BytesIO()
        with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
            link = zipfile.ZipInfo("wish-builder/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, b"../../outside")
        path.write_bytes(output.getvalue())

    def _write_build_evidence(self) -> dict[str, object]:
        evidence = build_distribution_evidence(
            self.dist,
            self.skill,
            self.repeat,
            revision=self.revision,
        )
        self.build_evidence.write_bytes(canonical_json_bytes(evidence))
        return evidence

    def test_records_raw_hashes_and_revision(self) -> None:
        result = build_distribution_evidence(
            self.dist,
            self.skill,
            self.repeat,
            revision=self.revision,
        )

        artifacts = {item["kind"]: item for item in result["artifacts"]}
        expected = "sha256:" + hashlib.sha256(self.skill_bytes).hexdigest()
        self.assertEqual("passed", result["status"])
        self.assertEqual(self.revision, result["github_sha"])
        self.assertEqual(expected, artifacts["skill_zip"]["sha256"])
        self.assertEqual(expected, artifacts["skill_zip_repeat"]["sha256"])
        digest_input = dict(result)
        actual_digest = digest_input.pop("evidence_digest")
        self.assertEqual(
            "sha256:" + hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest(),
            actual_digest,
        )

    def test_missing_or_duplicate_distribution_role_fails_closed(self) -> None:
        (self.dist / "wish_builder-extra.whl").write_bytes(b"other")
        with self.assertRaisesRegex(DistributionEvidenceError, "exactly one wheel"):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.repeat,
                revision=self.revision,
            )

        (self.dist / "wish_builder-extra.whl").unlink()
        (self.dist / "wish_builder-0.1.tar.gz").unlink()
        with self.assertRaisesRegex(DistributionEvidenceError, "exactly one sdist"):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.repeat,
                revision=self.revision,
            )

    def test_non_deterministic_skill_zip_and_invalid_revision_are_rejected(self) -> None:
        repeat_members = self._valid_members("skill_zip_repeat")
        repeat_members["wish-builder/SKILL.md"] = b"different"
        self.repeat.write_bytes(self._zip_bytes(repeat_members))
        with self.assertRaisesRegex(DistributionEvidenceError, "different raw bytes"):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.repeat,
                revision=self.revision,
            )
        with self.assertRaisesRegex(DistributionEvidenceError, "commit id"):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.skill,
                revision="HEAD",
            )

    def test_rejects_tgz_members_in_every_archive_role(self) -> None:
        for role in ("wheel", "sdist", "skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                self._write_archive(
                    role,
                    {"vendor/mindfoldhq-trellis-0.7.0-dev.2.tgz": b"fixture"},
                )
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive contains a forbidden \.tgz member",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_rejects_unsafe_member_paths_in_every_archive_role(self) -> None:
        unsafe_names = (
            "../outside.txt",
            "/absolute.txt",
            "C:\\absolute.txt",
            "C:drive-relative.txt",
            "safe\\noncanonical.txt",
            "safe/../../outside.txt",
            "wish-builder/NUL",
            "wish-builder/con.txt",
            "wish-builder/alternate:stream",
            "wish-builder/trailing.",
            "wish-builder/trailing ",
            "wish-builder/question?.txt",
            "wish-builder/control\x1f.txt",
        )
        for role in ("wheel", "sdist", "skill_zip", "skill_zip_repeat"):
            for name in unsafe_names:
                with self.subTest(role=role, name=name):
                    self._write_valid_archives()
                    self._write_archive(role, {name: b"unsafe"})
                    if "\\" in name and role != "sdist":
                        path = {
                            "wheel": self.wheel,
                            "skill_zip": self.skill,
                            "skill_zip_repeat": self.repeat,
                        }[role]
                        canonical = name.replace("\\", "/").encode("ascii")
                        noncanonical = name.encode("ascii")
                        raw = path.read_bytes()
                        self.assertIn(canonical, raw)
                        path.write_bytes(raw.replace(canonical, noncanonical))
                    with self.assertRaisesRegex(
                        DistributionEvidenceError,
                        rf"{role} archive contains an unsafe member path",
                    ):
                        build_distribution_evidence(
                            self.dist,
                            self.skill,
                            self.repeat,
                            revision=self.revision,
                        )

    def test_rejects_link_members_in_every_archive_role(self) -> None:
        for role in ("wheel", "sdist", "skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                self._write_archive_with_symlink(role)
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive contains a (?:symbolic-link|non-regular) member",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_rejects_unicode_normalized_archive_path_aliases(self) -> None:
        aliases = {
            "wish-builder/caf\u00e9.txt": b"first",
            "wish-builder/cafe\u0301.txt": b"second",
        }
        for role in ("wheel", "sdist", "skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                self._write_archive(role, aliases)
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive contains a duplicate member path",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_rejects_case_normalized_duplicate_members_in_every_role(self) -> None:
        for role in ("wheel", "sdist", "skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                path = {
                    "wheel": self.wheel,
                    "sdist": self.sdist,
                    "skill_zip": self.skill,
                    "skill_zip_repeat": self.repeat,
                }[role]
                members = self._valid_members(role)
                first_name = next(iter(members))
                duplicate_name = first_name.swapcase()
                if role == "sdist":
                    output = io.BytesIO()
                    with tarfile.open(fileobj=output, mode="w:gz") as archive:
                        for name, content in members.items():
                            info = tarfile.TarInfo(name)
                            info.mtime = 0
                            info.mode = 0o644
                            info.size = len(content)
                            archive.addfile(info, io.BytesIO(content))
                        duplicate = tarfile.TarInfo(duplicate_name)
                        duplicate.mtime = 0
                        duplicate.mode = 0o644
                        duplicate.size = len(members[first_name])
                        archive.addfile(duplicate, io.BytesIO(members[first_name]))
                    path.write_bytes(output.getvalue())
                else:
                    output = io.BytesIO()
                    with zipfile.ZipFile(
                        output,
                        mode="w",
                        compression=zipfile.ZIP_DEFLATED,
                    ) as archive:
                        for name, content in members.items():
                            archive.writestr(name, content)
                        archive.writestr(duplicate_name, members[first_name])
                    path.write_bytes(output.getvalue())

                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive contains a duplicate member path",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_rejects_non_regular_zip_members(self) -> None:
        for role in ("wheel", "skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                path = {
                    "wheel": self.wheel,
                    "skill_zip": self.skill,
                    "skill_zip_repeat": self.repeat,
                }[role]
                with zipfile.ZipFile(path, mode="a") as archive:
                    device = zipfile.ZipInfo("wish-builder/device")
                    device.create_system = 3
                    device.external_attr = (stat.S_IFIFO | 0o600) << 16
                    archive.writestr(device, b"")
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive contains a non-regular member",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_archive_member_count_limits_zip_and_sdist(self) -> None:
        with patch.object(distribution_evidence, "MAX_ARCHIVE_MEMBERS", 2):
            with self.assertRaisesRegex(
                DistributionEvidenceError, r"wheel archive exceeds the 2-member limit"
            ):
                build_distribution_evidence(
                    self.dist,
                    self.skill,
                    self.repeat,
                    revision=self.revision,
                )

        self._write_valid_archives()
        self._write_archive("sdist", {"wish_builder-0.1/extra.txt": b"extra"})
        with patch.object(distribution_evidence, "MAX_ARCHIVE_MEMBERS", 3):
            with self.assertRaisesRegex(
                DistributionEvidenceError, r"sdist archive exceeds the 3-member limit"
            ):
                build_distribution_evidence(
                    self.dist,
                    self.skill,
                    self.repeat,
                    revision=self.revision,
                )

    def test_archive_uncompressed_size_limits_zip_and_sdist(self) -> None:
        with patch.object(
            distribution_evidence, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 1024
        ):
            with self.assertRaisesRegex(
                DistributionEvidenceError, r"wheel archive exceeds .*total uncompressed"
            ):
                build_distribution_evidence(
                    self.dist,
                    self.skill,
                    self.repeat,
                    revision=self.revision,
                )

        self._write_valid_archives()
        incompressible = b"".join(
            hashlib.sha256(index.to_bytes(4, "big")).digest()
            for index in range(2048)
        )
        self._write_archive(
            "sdist", {"wish_builder-0.1/incompressible.bin": incompressible}
        )
        with patch.object(
            distribution_evidence, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 64 * 1024
        ):
            with self.assertRaisesRegex(
                DistributionEvidenceError, r"sdist archive exceeds .*total uncompressed"
            ):
                build_distribution_evidence(
                    self.dist,
                    self.skill,
                    self.repeat,
                    revision=self.revision,
                )

    def test_archive_single_member_uncompressed_size_is_bounded(self) -> None:
        with patch.object(distribution_evidence, "MAX_ARCHIVE_MEMBER_BYTES", 1024):
            with self.assertRaisesRegex(
                DistributionEvidenceError,
                r"wheel archive member exceeds the 1024-byte uncompressed limit",
            ):
                build_distribution_evidence(
                    self.dist,
                    self.skill,
                    self.repeat,
                    revision=self.revision,
                )

    def test_archive_compression_ratio_limits_zip_and_sdist(self) -> None:
        self._write_archive(
            "wheel", {"wish_builder/compression-bomb.txt": b"A" * (64 * 1024)}
        )
        with patch.object(distribution_evidence, "MAX_ARCHIVE_COMPRESSION_RATIO", 10):
            with self.assertRaisesRegex(
                DistributionEvidenceError,
                r"wheel archive member exceeds the 10:1 compression-ratio limit",
            ):
                build_distribution_evidence(
                    self.dist,
                    self.skill,
                    self.repeat,
                    revision=self.revision,
                )

        self._write_valid_archives()
        self._write_archive(
            "sdist", {"wish_builder-0.1/compression-bomb.txt": b"A" * (512 * 1024)}
        )
        with patch.object(distribution_evidence, "MAX_ARCHIVE_COMPRESSION_RATIO", 10):
            with self.assertRaisesRegex(
                DistributionEvidenceError,
                r"sdist archive exceeds the 10:1 compression-ratio limit",
            ):
                build_distribution_evidence(
                    self.dist,
                    self.skill,
                    self.repeat,
                    revision=self.revision,
                )

    def test_archive_metadata_reads_are_bounded_for_wheel_and_sdist(self) -> None:
        metadata_limit = len(self._license_bytes()) + 1024
        for role in ("wheel", "sdist"):
            with self.subTest(role=role):
                self._write_valid_archives()
                members = self._valid_members(role)
                metadata_name = next(
                    name
                    for name in members
                    if name.endswith("METADATA") or name.endswith("PKG-INFO")
                )
                members[metadata_name] = self._metadata_bytes() + b"X" * metadata_limit
                self._write_archive(role, members, include_required=False)
                with patch.object(
                    distribution_evidence,
                    "MAX_ARCHIVE_METADATA_BYTES",
                    metadata_limit,
                ):
                    with self.assertRaisesRegex(
                        DistributionEvidenceError,
                        rf"{role} archive metadata member exceeds the .*read limit",
                    ):
                        build_distribution_evidence(
                            self.dist,
                            self.skill,
                            self.repeat,
                            revision=self.revision,
                        )

    def test_skill_archives_reject_members_outside_the_skill_root(self) -> None:
        for role in ("skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                self._write_archive(role, {"outside.txt": b"unexpected"})
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive member is outside the wish-builder root",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_rejects_extra_top_level_tgz_in_build_and_install_evidence(self) -> None:
        tarball = self.dist / "mindfoldhq-trellis-0.6.15.TGZ"
        tarball.write_bytes(b"official-package-input-must-stay-outside-dist")
        with self.assertRaisesRegex(
            DistributionEvidenceError,
            r"distribution directory contains forbidden top-level \.tgz files",
        ):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.repeat,
                revision=self.revision,
            )

        tarball.unlink()
        self._write_build_evidence()
        tarball.write_bytes(b"appeared-after-build")
        with self.assertRaisesRegex(
            DistributionEvidenceError,
            r"distribution directory contains forbidden top-level \.tgz files",
        ):
            build_clean_install_evidence(
                self.dist,
                self.build_evidence,
                revision=self.revision,
                platform_name=(
                    "windows-latest" if sys.platform == "win32" else "ubuntu-latest"
                ),
                python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
                cell_id=(
                    "windows-latest" if sys.platform == "win32" else "ubuntu-latest"
                )
                + f"-py{sys.version_info.major}.{sys.version_info.minor}",
            )

    def test_requires_canonical_gpl_license_in_every_archive_role(self) -> None:
        for role in ("wheel", "sdist", "skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                members = self._valid_members(role)
                for name in tuple(members):
                    if name.endswith("LICENSE"):
                        members.pop(name)
                self._write_archive(role, members, include_required=False)
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive must contain exactly one canonical LICENSE",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

                members = self._valid_members(role)
                for name in tuple(members):
                    if name.endswith("LICENSE"):
                        members[name] = b"not the canonical GPL text\n"
                self._write_archive(role, members, include_required=False)
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive LICENSE does not match",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_requires_gpl_expression_and_license_file_metadata(self) -> None:
        for role in ("wheel", "sdist"):
            with self.subTest(role=role):
                self._write_valid_archives()
                members = self._valid_members(role)
                metadata_name = next(
                    name
                    for name in members
                    if name.endswith("METADATA") or name.endswith("PKG-INFO")
                )
                members[metadata_name] = self._metadata_bytes().replace(
                    b"GPL-3.0-only", b"MIT"
                )
                self._write_archive(role, members, include_required=False)
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} metadata must declare License-Expression",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_requires_exact_package_name_in_wheel_and_sdist_metadata(self) -> None:
        for role in ("wheel", "sdist"):
            with self.subTest(role=role):
                self._write_valid_archives()
                members = self._valid_members(role)
                metadata_name = next(
                    name
                    for name in members
                    if name.endswith("METADATA") or name.endswith("PKG-INFO")
                )
                members[metadata_name] = self._metadata_bytes().replace(
                    b"Name: wish-builder", b"Name: other-project"
                )
                self._write_archive(role, members, include_required=False)

                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} metadata must declare exactly one Name: wish-builder",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_requires_canonical_distribution_filenames(self) -> None:
        renamed_wheel = self.wheel.with_name("wish-builder-0.1-py3-none-any.whl")
        self.wheel.rename(renamed_wheel)
        self.wheel = renamed_wheel
        with self.assertRaisesRegex(
            DistributionEvidenceError, "wheel filename must be canonical"
        ):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.repeat,
                revision=self.revision,
            )

        self.wheel.rename(self.dist / "wish_builder-0.1-py3-none-any.whl")
        self.wheel = self.dist / "wish_builder-0.1-py3-none-any.whl"
        renamed_sdist = self.sdist.with_name("wish-builder-0.1.tar.gz")
        self.sdist.rename(renamed_sdist)
        self.sdist = renamed_sdist
        with self.assertRaisesRegex(
            DistributionEvidenceError, "sdist filename must be canonical"
        ):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.repeat,
                revision=self.revision,
            )

    def test_expected_release_version_must_match_both_archives(self) -> None:
        with self.assertRaisesRegex(
            DistributionEvidenceError,
            "wheel metadata Version must equal 0.1.0.dev0",
        ):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.repeat,
                revision=self.revision,
                expected_version="0.1.0.dev0",
            )

    def test_rejects_forbidden_package_qualified_install_specs(self) -> None:
        forbidden = (
            b"npm install @mindfoldhq/trellis@latest",
            b"npm install @mindfoldhq/trellis-core@latest",
            b"npm pack @mindfoldhq/trellis@0.7.0-dev.2",
            b"npm pack @mindfoldhq/trellis-core@0.7.0-beta.3",
            b"npm install @mindfoldhq/trellis@0.6.14",
            b"npm install @mindfoldhq/trellis-core@0.6.16",
            b"npm install @mindfoldhq/trellis@0.7.0",
            b"npm install @mindfoldhq/trellis@^0.6.15",
            b"npm install @mindfoldhq/trellis-core@~0.6.15",
            b"npm install @mindfoldhq/trellis-core@file:trellis-core.tgz",
            b"npm pack @mindfoldhq/trellis",
            b"npm install @mindfoldhq/trellis-core",
        )
        for install_spec in forbidden:
            with self.subTest(install_spec=install_spec):
                self._write_valid_archives()
                self._write_archive(
                    "wheel",
                    {"wish_builder/install.txt": install_spec},
                )
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    "wheel archive contains a forbidden Trellis install spec",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_rejects_bare_install_after_package_manager_global_options(self) -> None:
        forbidden = (
            b"npm --prefix sandbox install @mindfoldhq/trellis",
            b"pnpm --dir sandbox add @mindfoldhq/trellis-core",
            b"yarn --cwd sandbox add @mindfoldhq/trellis",
        )
        for install_spec in forbidden:
            with self.subTest(install_spec=install_spec):
                self._write_valid_archives()
                self._write_archive(
                    "wheel", {"wish_builder/install.txt": install_spec}
                )
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    "wheel archive contains a forbidden Trellis install spec",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_allows_only_exact_official_package_qualified_pins(self) -> None:
        exact = (
            b"npm install -g @mindfoldhq/trellis@0.6.15",
            b"npm pack '@mindfoldhq/trellis-core@0.6.15'",
        )
        self._write_archive(
            "wheel",
            {"wish_builder/install.txt": b"\n".join(exact)},
        )

        result = build_distribution_evidence(
            self.dist,
            self.skill,
            self.repeat,
            revision=self.revision,
        )

        self.assertEqual("passed", result["status"])

    def test_rejects_a_later_bad_pin_after_an_exact_supported_pin(self) -> None:
        install_specs = (
            b"npm install @mindfoldhq/trellis@0.6.15\n"
            b"npm install @mindfoldhq/trellis-core@0.6.16\n"
        )
        self._write_archive(
            "wheel",
            {"wish_builder/install.txt": install_specs},
        )

        with self.assertRaisesRegex(
            DistributionEvidenceError,
            "wheel archive contains a forbidden Trellis install spec",
        ):
            build_distribution_evidence(
                self.dist,
                self.skill,
                self.repeat,
                revision=self.revision,
            )

    def test_scans_install_specs_in_every_archive_role(self) -> None:
        install_spec = b"npm install @mindfoldhq/trellis@0.7.0-dev.9"
        for role in ("wheel", "sdist", "skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                member_name = (
                    "wish-builder/docs/install.txt"
                    if role in {"skill_zip", "skill_zip_repeat"}
                    else "docs/install.txt"
                )
                self._write_archive(role, {member_name: install_spec})
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive contains a forbidden Trellis install spec",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_allows_withdrawal_and_unqualified_latest_warnings(self) -> None:
        warning = (
            b"0.7.0-dev.2 was withdrawn and is not supported. "
            b"Do not install or resolve @latest."
        )
        self._write_archive(
            "wheel",
            {"wish_builder/warning.txt": warning},
        )

        result = build_distribution_evidence(
            self.dist,
            self.skill,
            self.repeat,
            revision=self.revision,
        )

        self.assertEqual("passed", result["status"])

    def test_unreadable_or_wrong_archive_format_fails_closed(self) -> None:
        for role in ("wheel", "sdist", "skill_zip", "skill_zip_repeat"):
            with self.subTest(role=role):
                self._write_valid_archives()
                path = {
                    "wheel": self.wheel,
                    "sdist": self.sdist,
                    "skill_zip": self.skill,
                    "skill_zip_repeat": self.repeat,
                }[role]
                path.write_bytes(b"not an archive")
                with self.assertRaisesRegex(
                    DistributionEvidenceError,
                    rf"{role} archive cannot be inspected",
                ):
                    build_distribution_evidence(
                        self.dist,
                        self.skill,
                        self.repeat,
                        revision=self.revision,
                    )

    def test_clean_install_cell_is_bound_to_identity_and_canonical_artifacts(self) -> None:
        build = self._write_build_evidence()
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        platform_name = "windows-latest" if sys.platform == "win32" else "ubuntu-latest"
        cell_id = f"{platform_name}-py{python_version}"

        with patch(
            "scripts.ci_distribution_evidence._smoke_install"
        ) as smoke_install:
            result = build_clean_install_evidence(
                self.dist,
                self.build_evidence,
                revision=self.revision,
                platform_name=platform_name,
                python_version=python_version,
                cell_id=cell_id,
            )

        self.assertEqual("passed", result["status"])
        self.assertEqual(cell_id, result["cell_id"])
        self.assertEqual(self.revision, result["revision"])
        self.assertEqual(build["evidence_digest"], result["distribution_evidence_digest"])
        self.assertEqual(
            "sha256:" + hashlib.sha256(self.build_evidence.read_bytes()).hexdigest(),
            result["distribution_evidence_sha256"],
        )
        self.assertEqual(["sdist", "wheel"], [item["kind"] for item in result["artifacts"]])
        self.assertEqual(2, smoke_install.call_count)
        self.assertEqual(
            ["0.1", "0.1"],
            [call.kwargs["expected_version"] for call in smoke_install.call_args_list],
        )
        digest_input = dict(result)
        actual_digest = digest_input.pop("evidence_digest")
        self.assertEqual(
            "sha256:" + hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest(),
            actual_digest,
        )

    def test_clean_install_rejects_wrong_cell_and_changed_distribution(self) -> None:
        self._write_build_evidence()
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        platform_name = "windows-latest" if sys.platform == "win32" else "ubuntu-latest"

        with self.assertRaisesRegex(DistributionEvidenceError, "cell id"):
            build_clean_install_evidence(
                self.dist,
                self.build_evidence,
                revision=self.revision,
                platform_name=platform_name,
                python_version=python_version,
                cell_id="wrong-cell",
            )

        (self.dist / "wish_builder-0.1-py3-none-any.whl").write_bytes(b"changed")
        with self.assertRaisesRegex(DistributionEvidenceError, "does not match"):
            build_clean_install_evidence(
                self.dist,
                self.build_evidence,
                revision=self.revision,
                platform_name=platform_name,
                python_version=python_version,
                cell_id=f"{platform_name}-py{python_version}",
            )

    def test_clean_install_rejects_rewritten_or_ambiguous_build_evidence(self) -> None:
        build = self._write_build_evidence()
        build["github_sha"] = "b" * 40
        self.build_evidence.write_bytes(canonical_json_bytes(build))
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        platform_name = "windows-latest" if sys.platform == "win32" else "ubuntu-latest"
        with self.assertRaisesRegex(DistributionEvidenceError, "another revision"):
            build_clean_install_evidence(
                self.dist,
                self.build_evidence,
                revision=self.revision,
                platform_name=platform_name,
                python_version=python_version,
                cell_id=f"{platform_name}-py{python_version}",
            )

        self.build_evidence.write_text(
            '{"schema_version":1,"schema_version":1}', encoding="utf-8"
        )
        with self.assertRaisesRegex(DistributionEvidenceError, "duplicate JSON key"):
            build_clean_install_evidence(
                self.dist,
                self.build_evidence,
                revision=self.revision,
                platform_name=platform_name,
                python_version=python_version,
                cell_id=f"{platform_name}-py{python_version}",
            )

    def test_clean_install_rejects_artifact_changed_while_installing(self) -> None:
        self._write_build_evidence()
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        platform_name = "windows-latest" if sys.platform == "win32" else "ubuntu-latest"

        def mutate_first_artifact(
            artifact: Path,
            *,
            kind: str,
            environment: Path,
            expected_version: str,
        ) -> None:
            del environment, expected_version
            if kind == "sdist":
                artifact.write_bytes(b"changed during install")

        with (
            patch(
                "scripts.ci_distribution_evidence._smoke_install",
                side_effect=mutate_first_artifact,
            ),
            self.assertRaisesRegex(DistributionEvidenceError, "changed during"),
        ):
            build_clean_install_evidence(
                self.dist,
                self.build_evidence,
                revision=self.revision,
                platform_name=platform_name,
                python_version=python_version,
                cell_id=f"{platform_name}-py{python_version}",
            )


if __name__ == "__main__":
    unittest.main()
