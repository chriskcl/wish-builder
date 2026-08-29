from __future__ import annotations

import gzip
import hashlib
import io
import os
import subprocess
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from scripts.build_distributions import (
    CANONICAL_EPOCH,
    DistributionBuildError,
    _SourceFile,
    _copy_source_file,
    _exclusive_output_lock,
    _portable_path_key,
    _prepare_output_directory,
    _promote,
    _promote_distribution_set,
    _run_raw_build,
    _worktree_files,
    build_distributions,
    canonicalize_sdist,
    canonicalize_wheel,
)


class ReproducibleDistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_raw_build_uses_the_preinstalled_hash_locked_toolchain(self) -> None:
        output = self.root / "raw-dist"
        with patch("scripts.build_distributions.subprocess.run") as run:
            _run_raw_build(self.root, output)

        arguments = run.call_args.args[0]
        self.assertIn("--no-isolation", arguments)
        self.assertEqual(self.root, run.call_args.kwargs["cwd"])
        self.assertTrue(run.call_args.kwargs["check"])

    @staticmethod
    def _wheel(
        path: Path,
        timestamp: tuple[int, int, int, int, int, int],
        *,
        reverse: bool = False,
        mode: int = 0o100644,
        marker: bytes = b"",
    ) -> None:
        members = [
            ("wish_builder/__init__.py", b"__version__ = '0.1'\n" + marker),
            ("wish_builder-0.1.dist-info/METADATA", b"Name: wish-builder\n"),
        ]
        if reverse:
            members.reverse()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members:
                info = zipfile.ZipInfo(name, timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = mode << 16
                archive.writestr(info, content)

    @staticmethod
    def _sdist(
        path: Path,
        timestamp: int,
        marker: str,
        *,
        reverse: bool = False,
        file_mode: int = 0o644,
    ) -> None:
        with path.open("wb") as output:
            with gzip.GzipFile(
                filename=marker,
                mode="wb",
                fileobj=output,
                mtime=timestamp,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    directory = tarfile.TarInfo("wish_builder-0.1")
                    directory.type = tarfile.DIRTYPE
                    directory.mode = 0o755
                    directory.mtime = timestamp
                    directory.uid = timestamp
                    directory.pax_headers = {"atime": f"{timestamp}.1"}
                    content = b"Name: wish-builder\n"
                    member = tarfile.TarInfo("wish_builder-0.1/PKG-INFO")
                    member.mode = file_mode
                    member.mtime = timestamp
                    member.uid = timestamp
                    member.gid = timestamp
                    member.uname = marker
                    member.gname = marker
                    member.pax_headers = {"ctime": f"{timestamp}.2"}
                    member.size = len(content)
                    entries = [
                        (directory, None),
                        (member, io.BytesIO(content)),
                    ]
                    if reverse:
                        entries.reverse()
                    for info, handle in entries:
                        archive.addfile(info, handle)

    @staticmethod
    def _wheel_members(path: Path, names: tuple[str, ...]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                info = zipfile.ZipInfo(name, (2025, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                archive.writestr(info, b"content")
        archive_bytes = path.read_bytes()
        for name in names:
            if "\\" in name:
                archive_bytes = archive_bytes.replace(
                    name.replace("\\", "/").encode("utf-8"),
                    name.encode("utf-8"),
                )
        path.write_bytes(archive_bytes)

    @staticmethod
    def _sdist_members(path: Path, names: tuple[str, ...]) -> None:
        with tarfile.open(path, "w:gz") as archive:
            for name in names:
                content = b"content"
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

    def test_wheel_metadata_is_canonical_across_raw_build_times(self) -> None:
        first_raw = self.root / "first.whl"
        second_raw = self.root / "second.whl"
        first = self.root / "first-canonical.whl"
        second = self.root / "second-canonical.whl"
        self._wheel(first_raw, (2025, 1, 2, 3, 4, 6))
        self._wheel(
            second_raw,
            (2026, 7, 8, 9, 10, 12),
            reverse=True,
            mode=0o100600,
        )

        canonicalize_wheel(first_raw, first)
        canonicalize_wheel(second_raw, second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertTrue(
                all(
                    member.date_time == (1980, 1, 1, 0, 0, 0)
                    for member in archive.infolist()
                )
            )
            self.assertTrue(
                all(
                    member.compress_type == zipfile.ZIP_STORED
                    for member in archive.infolist()
                )
            )
            self.assertTrue(
                all(
                    (member.external_attr >> 16) == 0o100644
                    for member in archive.infolist()
                )
            )

    def test_sdist_metadata_is_canonical_across_raw_build_times(self) -> None:
        first_raw = self.root / "first.tar.gz"
        second_raw = self.root / "second.tar.gz"
        first = self.root / "first-canonical.tar.gz"
        second = self.root / "second-canonical.tar.gz"
        self._sdist(first_raw, 1_700_000_000, "first")
        self._sdist(
            second_raw,
            1_800_000_000,
            "second",
            reverse=True,
            file_mode=0o600,
        )

        canonicalize_sdist(first_raw, first)
        canonicalize_sdist(second_raw, second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        with tarfile.open(first, "r:gz") as archive:
            for member in archive.getmembers():
                self.assertEqual(CANONICAL_EPOCH, member.mtime)
                self.assertEqual(0, member.uid)
                self.assertEqual(0, member.gid)
                self.assertEqual({}, member.pax_headers)
                self.assertEqual(0o755 if member.isdir() else 0o644, member.mode)

    def test_orchestrator_builds_twice_before_promotion(self) -> None:
        calls = 0

        def fake_stage(_repository_root: Path, destination: Path) -> None:
            destination.mkdir(parents=True)

        def fake_build(_source_root: Path, output: Path) -> None:
            nonlocal calls
            calls += 1
            output.mkdir(parents=True)
            self._wheel(
                output / "wish_builder-0.1-py3-none-any.whl",
                (2024 + calls, 1, 1, 0, 0, 0),
            )
            self._sdist(
                output / "wish_builder-0.1.tar.gz", 1_700_000_000 + calls, str(calls)
            )

        output = self.root / "dist"
        with (
            patch(
                "scripts.build_distributions._stage_worktree", side_effect=fake_stage
            ),
            patch("scripts.build_distributions._run_raw_build", side_effect=fake_build),
        ):
            result = build_distributions(
                output,
                repository_root=self.root,
                verify_repeat=True,
            )

        self.assertEqual(2, calls)
        self.assertEqual("passed", result["status"])
        self.assertTrue(result["repeat_verified"])
        self.assertEqual(
            {"wish_builder-0.1-py3-none-any.whl", "wish_builder-0.1.tar.gz"},
            {path.name for path in output.iterdir()},
        )

    def test_output_symlink_is_rejected_before_resolution(self) -> None:
        output = MagicMock(spec=Path)
        output.is_symlink.return_value = True

        with self.assertRaisesRegex(DistributionBuildError, "must not be a symlink"):
            _prepare_output_directory(output)

        output.mkdir.assert_not_called()
        output.resolve.assert_not_called()

    def test_output_junction_is_rejected_before_resolution(self) -> None:
        output = MagicMock(spec=Path)
        output.is_symlink.return_value = False

        with patch(
            "scripts.build_distributions._path_is_junction",
            return_value=True,
        ):
            with self.assertRaisesRegex(
                DistributionBuildError, "must not be a symlink"
            ):
                _prepare_output_directory(output)

        output.mkdir.assert_not_called()
        output.resolve.assert_not_called()

    def test_portable_path_key_normalizes_unicode_and_case(self) -> None:
        composed = Path("Package") / "Caf\u00e9.py"
        decomposed = Path("package") / "Cafe\u0301.py"

        self.assertEqual(
            _portable_path_key(composed),
            _portable_path_key(decomposed),
        )

    def test_worktree_rejects_unicode_normalized_path_collision(self) -> None:
        composed = Path("Package") / "Caf\u00e9.py"
        decomposed = Path("package") / "Cafe\u0301.py"
        for relative in (composed, decomposed):
            source = self.root / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("content", encoding="utf-8")
        git_output = (
            b"\0".join(
                relative.as_posix().encode("utf-8")
                for relative in (composed, decomposed)
            )
            + b"\0"
        )

        with patch("scripts.build_distributions.subprocess.run") as run:
            run.side_effect = (
                subprocess.CompletedProcess([], 0, stdout=b""),
                subprocess.CompletedProcess([], 0, stdout=git_output),
            )
            with self.assertRaisesRegex(DistributionBuildError, "collide portably"):
                _worktree_files(self.root)

    def test_git_index_symlink_is_rejected_even_when_checkout_is_regular(self) -> None:
        source = self.root / "link.txt"
        source.write_text("target.txt", encoding="utf-8")
        tracked = b"120000 " + (b"a" * 40) + b" 0\tlink.txt\0"

        with patch("scripts.build_distributions.subprocess.run") as run:
            run.side_effect = (
                subprocess.CompletedProcess([], 0, stdout=tracked),
                subprocess.CompletedProcess([], 0, stdout=b""),
            )
            with self.assertRaisesRegex(DistributionBuildError, "Git symlink"):
                _worktree_files(self.root)

    def test_source_swap_between_check_and_open_is_rejected(self) -> None:
        relative = Path("source.txt")
        source = self.root / relative
        source.write_bytes(b"first")
        replacement = self.root / "replacement.txt"
        replacement.write_bytes(b"second")
        destination = self.root / "staged"
        destination.mkdir()
        record = _SourceFile(relative, 0o100644, True)
        real_open = os.open
        swapped = False

        def swap_before_open(path: object, flags: int, mode: int = 0o777) -> int:
            nonlocal swapped
            if Path(path) == source and not swapped:
                swapped = True
                source.unlink()
                replacement.replace(source)
            return real_open(path, flags, mode)

        with patch("scripts.build_distributions.os.open", side_effect=swap_before_open):
            with self.assertRaisesRegex(DistributionBuildError, "changed before copy"):
                _copy_source_file(self.root, destination, record)

        self.assertFalse((destination / relative).exists())

    def test_canonicalizers_reject_unsafe_and_portably_duplicate_members(self) -> None:
        unsafe_names = ("../escape", "/absolute", "C:\\drive", "a\\b")
        for index, name in enumerate(unsafe_names):
            with self.subTest(format="wheel", name=name):
                raw = self.root / f"unsafe-{index}.whl"
                self._wheel_members(raw, (name,))
                with self.assertRaisesRegex(DistributionBuildError, "unsafe wheel"):
                    canonicalize_wheel(raw, self.root / f"unsafe-{index}.canonical.whl")
            with self.subTest(format="sdist", name=name):
                raw = self.root / f"unsafe-{index}.tar.gz"
                self._sdist_members(raw, (name,))
                with self.assertRaisesRegex(DistributionBuildError, "unsafe sdist"):
                    canonicalize_sdist(
                        raw, self.root / f"unsafe-{index}.canonical.tar.gz"
                    )

        collisions = (
            ("pkg/A.py", "pkg/a.py"),
            ("pkg/Caf\u00e9.py", "pkg/Cafe\u0301.py"),
        )
        for index, names in enumerate(collisions):
            with self.subTest(format="wheel", names=names):
                raw = self.root / f"collision-{index}.whl"
                self._wheel_members(raw, names)
                with self.assertRaisesRegex(DistributionBuildError, "duplicate member"):
                    canonicalize_wheel(
                        raw, self.root / f"collision-{index}.canonical.whl"
                    )
            with self.subTest(format="sdist", names=names):
                raw = self.root / f"collision-{index}.tar.gz"
                self._sdist_members(raw, names)
                with self.assertRaisesRegex(DistributionBuildError, "duplicate member"):
                    canonicalize_sdist(
                        raw, self.root / f"collision-{index}.canonical.tar.gz"
                    )

    def test_promotion_ignores_predictable_legacy_temp_name(self) -> None:
        source = self.root / "source.whl"
        destination = self.root / "result.whl"
        legacy_temp = destination.with_suffix(destination.suffix + ".tmp")
        source.write_bytes(b"new")
        legacy_temp.write_bytes(b"sentinel")

        _promote(source, destination)

        self.assertEqual(b"new", destination.read_bytes())
        self.assertEqual(b"sentinel", legacy_temp.read_bytes())

    def test_output_lock_rejects_a_competing_writer_and_cleans_up(self) -> None:
        output = _prepare_output_directory(self.root / "dist-lock")
        with _exclusive_output_lock(output):
            with self.assertRaisesRegex(DistributionBuildError, "promotion is active"):
                with _exclusive_output_lock(output):
                    self.fail("a competing writer acquired the output lock")

        self.assertFalse((output.resolved / ".wish-builder-build.lock").exists())

    def test_distribution_set_rolls_back_when_second_promotion_fails(self) -> None:
        output = _prepare_output_directory(self.root / "dist-rollback")
        wheel = self.root / "new.whl"
        sdist = self.root / "new.tar.gz"
        wheel.write_bytes(b"new-wheel")
        sdist.write_bytes(b"new-sdist")
        wheel_destination = output.resolved / wheel.name
        sdist_destination = output.resolved / sdist.name
        wheel_destination.write_bytes(b"old-wheel")
        sdist_destination.write_bytes(b"old-sdist")
        real_replace = os.replace
        failed = False

        def fail_second(source: object, destination: object) -> None:
            nonlocal failed
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                destination_path == sdist_destination
                and source_path.suffix == ".tmp"
                and not failed
            ):
                failed = True
                raise OSError("injected second promotion failure")
            real_replace(source, destination)

        with patch("scripts.build_distributions.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected second promotion"):
                _promote_distribution_set({"wheel": wheel, "sdist": sdist}, output)

        self.assertEqual(b"old-wheel", wheel_destination.read_bytes())
        self.assertEqual(b"old-sdist", sdist_destination.read_bytes())
        self.assertEqual([], list(output.resolved.glob(".*.tmp")))
        self.assertEqual([], list(output.resolved.glob(".*.bak")))

    def test_committed_pair_survives_partial_backup_cleanup_failure(self) -> None:
        output = _prepare_output_directory(self.root / "dist-cleanup-failure")
        wheel = self.root / "new.whl"
        sdist = self.root / "new.tar.gz"
        wheel.write_bytes(b"new-wheel")
        sdist.write_bytes(b"new-sdist")
        wheel_destination = output.resolved / wheel.name
        sdist_destination = output.resolved / sdist.name
        wheel_destination.write_bytes(b"old-wheel")
        sdist_destination.write_bytes(b"old-sdist")
        real_unlink = Path.unlink
        backup_unlinks = 0

        def fail_second_backup_cleanup(path: Path, missing_ok: bool = False) -> None:
            nonlocal backup_unlinks
            if path.suffix == ".bak":
                backup_unlinks += 1
                if backup_unlinks == 2:
                    raise PermissionError("injected backup cleanup failure")
            real_unlink(path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", new=fail_second_backup_cleanup):
            with self.assertRaisesRegex(
                DistributionBuildError, "promotion committed.*cleanup was incomplete"
            ):
                _promote_distribution_set({"wheel": wheel, "sdist": sdist}, output)

        self.assertEqual(b"new-wheel", wheel_destination.read_bytes())
        self.assertEqual(b"new-sdist", sdist_destination.read_bytes())
        retained = list(output.resolved.glob(".*.bak"))
        self.assertEqual(1, len(retained))
        self.assertEqual(b"old-sdist", retained[0].read_bytes())

    def test_failed_rollback_retains_the_recoverable_backup(self) -> None:
        output = _prepare_output_directory(self.root / "dist-restore-failure")
        wheel = self.root / "new.whl"
        sdist = self.root / "new.tar.gz"
        wheel.write_bytes(b"new-wheel")
        sdist.write_bytes(b"new-sdist")
        wheel_destination = output.resolved / wheel.name
        sdist_destination = output.resolved / sdist.name
        wheel_destination.write_bytes(b"old-wheel")
        sdist_destination.write_bytes(b"old-sdist")
        real_replace = os.replace

        def fail_promotion_and_wheel_restore(
            source: object, destination: object
        ) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if source_path.suffix == ".tmp" and destination_path == sdist_destination:
                raise OSError("injected second promotion failure")
            if source_path.suffix == ".bak" and destination_path == wheel_destination:
                raise OSError("injected wheel restore failure")
            real_replace(source, destination)

        with patch(
            "scripts.build_distributions.os.replace",
            side_effect=fail_promotion_and_wheel_restore,
        ):
            with self.assertRaisesRegex(
                DistributionBuildError, "rollback was incomplete.*restore wheel"
            ):
                _promote_distribution_set({"wheel": wheel, "sdist": sdist}, output)

        self.assertEqual(b"new-wheel", wheel_destination.read_bytes())
        self.assertEqual(b"old-sdist", sdist_destination.read_bytes())
        retained = list(output.resolved.glob(".*.bak"))
        self.assertEqual(1, len(retained))
        self.assertEqual(b"old-wheel", retained[0].read_bytes())

    def test_artifact_metadata_is_read_while_promotion_lock_is_held(self) -> None:
        output = _prepare_output_directory(self.root / "dist-metadata-lock")
        wheel = self.root / "new.whl"
        sdist = self.root / "new.tar.gz"
        wheel.write_bytes(b"new-wheel")
        sdist.write_bytes(b"new-sdist")

        def hash_while_asserting_lock(path: Path) -> str:
            with self.assertRaisesRegex(DistributionBuildError, "promotion is active"):
                with _exclusive_output_lock(output):
                    self.fail("metadata hashing ran after the lock was released")
            return hashlib.sha256(path.read_bytes()).hexdigest()

        with patch(
            "scripts.build_distributions._sha256",
            side_effect=hash_while_asserting_lock,
        ):
            promoted = _promote_distribution_set(
                {"wheel": wheel, "sdist": sdist}, output
            )

        self.assertEqual(
            hashlib.sha256(b"new-wheel").hexdigest(),
            promoted["wheel"].sha256,
        )
        self.assertEqual(len(b"new-sdist"), promoted["sdist"].size_bytes)

    def test_lock_cleanup_does_not_mask_the_promotion_failure(self) -> None:
        output = _prepare_output_directory(self.root / "dist-lock-cleanup")
        wheel = self.root / "new.whl"
        sdist = self.root / "new.tar.gz"
        wheel.write_bytes(b"new-wheel")
        sdist.write_bytes(b"new-sdist")
        sdist_destination = output.resolved / sdist.name
        lock_path = output.resolved / ".wish-builder-build.lock"
        real_replace = os.replace
        real_unlink = Path.unlink

        def fail_second_promotion(source: object, destination: object) -> None:
            if Path(source).suffix == ".tmp" and Path(destination) == sdist_destination:
                raise OSError("injected second promotion failure")
            real_replace(source, destination)

        def fail_lock_cleanup(path: Path, missing_ok: bool = False) -> None:
            if path == lock_path:
                raise PermissionError("injected lock cleanup failure")
            real_unlink(path, missing_ok=missing_ok)

        with (
            patch(
                "scripts.build_distributions.os.replace",
                side_effect=fail_second_promotion,
            ),
            patch.object(Path, "unlink", new=fail_lock_cleanup),
        ):
            with self.assertRaisesRegex(
                OSError, "injected second promotion failure"
            ) as caught:
                _promote_distribution_set({"wheel": wheel, "sdist": sdist}, output)

        notes = getattr(caught.exception, "__notes__", ())
        self.assertTrue(
            any("lock could not be removed" in note for note in notes),
            notes,
        )
        self.assertTrue(lock_path.exists())

    def test_repeat_mismatch_refuses_every_promotion(self) -> None:
        calls = 0

        def fake_stage(_repository_root: Path, destination: Path) -> None:
            destination.mkdir(parents=True)

        def fake_build(_source_root: Path, output: Path) -> None:
            nonlocal calls
            calls += 1
            output.mkdir(parents=True)
            self._wheel(
                output / "wish_builder-0.1-py3-none-any.whl",
                (2025, 1, 1, 0, 0, 0),
                marker=str(calls).encode("ascii"),
            )
            self._sdist(output / "wish_builder-0.1.tar.gz", 1_700_000_000, "same")

        output = self.root / "dist-mismatch"
        with (
            patch(
                "scripts.build_distributions._stage_worktree", side_effect=fake_stage
            ),
            patch("scripts.build_distributions._run_raw_build", side_effect=fake_build),
        ):
            with self.assertRaisesRegex(DistributionBuildError, "different bytes"):
                build_distributions(output, repository_root=self.root)

        self.assertEqual([], list(output.iterdir()))


if __name__ == "__main__":
    unittest.main()
