from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wish_builder.adapters import git_identity
from wish_builder.adapters.git_identity import (
    GitIdentityError,
    ProtectedControlRoot,
    capture_filesystem_identity,
    capture_workspace_identity,
    compare_workspace_identity,
    reconstruct_pristine_workspace_identity,
    revalidate_control_root,
    revalidate_workspace_identity,
)


def repository_snapshot(repository: Path) -> tuple[dict[str, bytes], bytes | None]:
    files = {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    index = repository / ".git" / "index"
    return files, index.read_bytes() if index.is_file() else None


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        shell=False,
    )
    return result.stdout.decode("utf-8", errors="strict").strip()


class GitIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repository"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Wish Builder Tests")
        git(self.root, "config", "user.email", "wish-builder@local.invalid")
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        (self.root / ".github").mkdir()
        (self.root / "src" / "owned.txt").write_text("owned\n", encoding="utf-8")
        (self.root / "docs" / "outside.txt").write_text("outside\n", encoding="utf-8")
        (self.root / ".github" / "protected.txt").write_text(
            "protected\n", encoding="utf-8"
        )
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "baseline")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stable_probe_is_read_only_and_revalidates(self) -> None:
        before = repository_snapshot(self.root)
        expected = capture_workspace_identity(self.root, ("src/**",))
        result = revalidate_workspace_identity(expected)
        after = repository_snapshot(self.root)
        self.assertTrue(result.ok, result)
        self.assertEqual(before, after)
        self.assertEqual("refs/heads/main", expected.target_full_ref)
        self.assertTrue(expected.local_repository_id.startswith("sha256:"))
        self.assertTrue(expected.local_worktree_id.startswith("sha256:"))

    def test_owned_dirty_and_index_changes_are_detected(self) -> None:
        expected = capture_workspace_identity(self.root, ("src/**",))
        (self.root / "src" / "owned.txt").write_text("changed\n", encoding="utf-8")
        dirty_before = repository_snapshot(self.root)
        dirty = revalidate_workspace_identity(expected)
        self.assertFalse(dirty.ok)
        self.assertIn("index_dirty_fingerprint", dirty.mismatches)
        self.assertEqual(dirty_before, repository_snapshot(self.root))

        git(self.root, "add", "src/owned.txt")
        staged_before = repository_snapshot(self.root)
        staged = revalidate_workspace_identity(expected)
        self.assertFalse(staged.ok)
        self.assertIn("index_dirty_fingerprint", staged.mismatches)
        self.assertEqual(staged_before, repository_snapshot(self.root))

    def test_pristine_reconstruction_ignores_only_unstaged_worktree_bytes(
        self,
    ) -> None:
        with self.assertRaises(TypeError):
            reconstruct_pristine_workspace_identity(object())  # type: ignore[arg-type]

        baseline = capture_workspace_identity(self.root, ("src/**",))
        source = self.root / "src" / "owned.txt"
        source.write_text("derived projection\n", encoding="utf-8")
        dirty = capture_workspace_identity(self.root, ("src/**",))

        reconstructed = reconstruct_pristine_workspace_identity(dirty)

        self.assertEqual(baseline, reconstructed)
        git(self.root, "add", "src/owned.txt")
        staged = capture_workspace_identity(self.root, ("src/**",))
        self.assertNotEqual(
            baseline.workspace_hash,
            reconstruct_pristine_workspace_identity(staged).workspace_hash,
        )

    def test_pristine_reconstruction_retains_head_identity(self) -> None:
        baseline = capture_workspace_identity(self.root, ("src/**",))
        source = self.root / "src" / "owned.txt"
        source.write_text("next commit\n", encoding="utf-8")
        git(self.root, "add", "src/owned.txt")
        git(self.root, "commit", "-m", "next")
        advanced = capture_workspace_identity(self.root, ("src/**",))

        self.assertNotEqual(
            baseline.workspace_hash,
            reconstruct_pristine_workspace_identity(advanced).workspace_hash,
        )

    def test_unrelated_dirty_file_does_not_change_scoped_fingerprint(self) -> None:
        expected = capture_workspace_identity(self.root, ("src/**",))
        (self.root / "docs" / "outside.txt").write_text(
            "user change\n", encoding="utf-8"
        )
        result = revalidate_workspace_identity(expected)
        self.assertTrue(result.ok, result)

    def test_fingerprint_is_independent_of_pipe_read_boundaries(self) -> None:
        (self.root / "src" / "owned.txt").write_text(
            "changed across several chunks\n", encoding="utf-8"
        )
        with mock.patch.object(git_identity, "_READ_CHUNK", 1):
            bytewise = capture_workspace_identity(self.root, ("src/**",))
        with mock.patch.object(git_identity, "_READ_CHUNK", 64 * 1024):
            buffered = capture_workspace_identity(self.root, ("src/**",))
        self.assertEqual(
            bytewise.index_dirty_fingerprint,
            buffered.index_dirty_fingerprint,
        )

    def test_untracked_file_disappearing_during_probe_fails_closed(self) -> None:
        candidate = self.root / "src" / "racing.txt"
        candidate.write_text("temporary\n", encoding="utf-8")
        original_run_git = git_identity._run_git
        removed = False

        def run_and_remove(*args: object, **kwargs: object) -> bytes:
            nonlocal removed
            result = original_run_git(*args, **kwargs)  # type: ignore[arg-type]
            arguments = tuple(args[1])
            if arguments[:2] == ("ls-files", "--others") and not removed:
                candidate.unlink()
                removed = True
            return result

        with (
            mock.patch.object(
                git_identity,
                "_run_git",
                side_effect=run_and_remove,
            ),
            self.assertRaisesRegex(GitIdentityError, "workspace_probe_race"),
        ):
            capture_workspace_identity(self.root, ("src/**",))

    def test_absolute_parent_and_git_magic_scopes_are_rejected(self) -> None:
        for scope in (
            "/src/**",
            "\\src\\**",
            "C:/src/**",
            "../src/**",
            "src/../docs/**",
            ":(exclude)docs/**",
        ):
            with self.subTest(scope=scope), self.assertRaises(GitIdentityError):
                capture_workspace_identity(self.root, (scope,))

    def test_untracked_symlink_hashes_the_link_not_its_target(self) -> None:
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_text("outside version one\n", encoding="utf-8")
        link = self.root / "src" / "link.txt"
        try:
            os.symlink(outside, link)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        expected = capture_workspace_identity(self.root, ("src/**",))
        outside.unlink()
        dangling = capture_workspace_identity(self.root, ("src/**",))
        self.assertEqual(
            expected.index_dirty_fingerprint,
            dangling.index_dirty_fingerprint,
        )

        link.unlink()
        os.symlink(Path(self.temporary.name) / "different-target.txt", link)
        changed = capture_workspace_identity(self.root, ("src/**",))
        self.assertNotEqual(
            expected.index_dirty_fingerprint,
            changed.index_dirty_fingerprint,
        )

    def test_ref_and_head_drift_are_detected(self) -> None:
        expected = capture_workspace_identity(self.root, ("src/**",))
        git(self.root, "switch", "-c", "other")
        result = revalidate_workspace_identity(expected)
        self.assertFalse(result.ok)
        self.assertIn("target_full_ref", result.mismatches)

        git(self.root, "switch", "main")
        (self.root / "src" / "owned.txt").write_text("next\n", encoding="utf-8")
        git(self.root, "add", "src/owned.txt")
        git(self.root, "commit", "-m", "next")
        commit_result = revalidate_workspace_identity(expected)
        self.assertFalse(commit_result.ok)
        self.assertIn("base_commit_sha", commit_result.mismatches)

    def test_sibling_worktree_has_same_repository_but_distinct_worktree(self) -> None:
        primary = capture_workspace_identity(self.root, ("src/**",))
        sibling = Path(self.temporary.name) / "sibling"
        git(self.root, "worktree", "add", "-b", "sibling", str(sibling), "HEAD")
        other = capture_workspace_identity(sibling, ("src/**",))
        self.assertEqual(primary.local_repository_id, other.local_repository_id)
        self.assertNotEqual(primary.local_worktree_id, other.local_worktree_id)
        comparison = compare_workspace_identity(primary, other)
        self.assertFalse(comparison.ok)
        self.assertIn("local_worktree_id", comparison.mismatches)

    def test_moved_and_replaced_worktree_keeps_original_user_files_untouched(
        self,
    ) -> None:
        expected = capture_workspace_identity(self.root, (".github/**", "src/**"))
        before = repository_snapshot(self.root)
        moved = Path(self.temporary.name) / "repository-moved"
        self.root.rename(moved)
        self.root.mkdir()

        result = revalidate_workspace_identity(expected)
        self.assertFalse(result.ok)
        self.assertEqual("workspace_drift", result.reason)
        self.assertEqual(before, repository_snapshot(moved))

    def test_replaced_common_dir_keeps_user_files_and_index_untouched(self) -> None:
        expected = capture_workspace_identity(self.root, ("src/**",))
        before = repository_snapshot(self.root)
        common_dir = Path(expected.common_dir.lexical_path)
        moved = common_dir.with_name(".git-original")
        common_dir.rename(moved)
        common_dir.mkdir()
        try:
            result = revalidate_workspace_identity(expected)
            self.assertFalse(result.ok)
            self.assertEqual("workspace_drift", result.reason)
            current_files = {
                path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
                and ".git" not in path.parts
                and ".git-original" not in path.parts
            }
            self.assertEqual(before[0], current_files)
            self.assertEqual(before[1], (moved / "index").read_bytes())
        finally:
            common_dir.rmdir()
            moved.rename(common_dir)

    def test_changed_worktree_git_indirection_is_detected_without_user_edits(
        self,
    ) -> None:
        sibling = Path(self.temporary.name) / "sibling-indirection"
        git(self.root, "worktree", "add", "-b", "indirection", str(sibling), "HEAD")
        expected = capture_workspace_identity(sibling, ("src/**",))
        before = repository_snapshot(sibling)
        git_file = sibling / ".git"
        original = git_file.read_bytes()
        original_mode = git_file.stat().st_mode
        original_attributes = None
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            original_attributes = kernel32.GetFileAttributesW(str(git_file))
            if not kernel32.SetFileAttributesW(str(git_file), 0x80):
                self.fail(f"cannot clear .git attributes: {ctypes.get_last_error()}")
        else:
            os.chmod(git_file, stat.S_IWRITE)
        git_file.write_text(
            f"gitdir: {(self.root / '.git').as_posix()}\n",
            encoding="utf-8",
        )
        try:
            result = revalidate_workspace_identity(expected)
            self.assertFalse(result.ok)
            self.assertEqual("workspace_drift", result.reason)
            self.assertEqual(before, repository_snapshot(sibling))
        finally:
            git_file.write_bytes(original)
            if original_attributes is not None:
                kernel32.SetFileAttributesW(str(git_file), original_attributes)
            else:
                os.chmod(git_file, original_mode)

    def test_sha256_repository_identity_is_supported_when_git_supports_it(self) -> None:
        repository = Path(self.temporary.name) / "sha256-repository"
        initialized = subprocess.run(
            ["git", "init", "--object-format=sha256", "-b", "main", str(repository)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            shell=False,
        )
        if initialized.returncode != 0:
            self.skipTest("installed Git does not support SHA-256 repositories")
        git(repository, "config", "user.name", "Wish Builder Tests")
        git(repository, "config", "user.email", "wish-builder@local.invalid")
        (repository / "src").mkdir()
        (repository / "src" / "owned.txt").write_text("owned\n", encoding="utf-8")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "baseline")
        identity = capture_workspace_identity(repository, ("src/**",))
        self.assertEqual(64, len(identity.base_commit_sha))

    def test_control_root_replacement_is_detected(self) -> None:
        control_root = Path(self.temporary.name) / "control"
        control_root.mkdir()
        expected = capture_filesystem_identity(control_root)
        moved = Path(self.temporary.name) / "control-old"
        control_root.rename(moved)
        control_root.mkdir()
        result = revalidate_control_root(expected)
        self.assertFalse(result.ok)
        self.assertEqual("control_root_drift", result.reason)

    def test_protected_control_root_binds_path_to_a_live_handle(self) -> None:
        control_root = Path(self.temporary.name) / "protected-control"
        control_root.mkdir()
        moved = Path(self.temporary.name) / "protected-control-old"
        with ProtectedControlRoot.open(control_root) as protected:
            self.assertFalse(protected.closed)
            self.assertTrue(protected.revalidate().ok)
            control_root.rename(moved)
            control_root.mkdir()
            drift = protected.revalidate()
            self.assertFalse(drift.ok)
            self.assertEqual("control_root_drift", drift.reason)
        self.assertTrue(protected.closed)
        self.assertFalse(protected.revalidate().ok)
        protected.close()

        with self.assertRaisesRegex(TypeError, "FilesystemIdentity"):
            revalidate_control_root(object())  # type: ignore[arg-type]

    def test_control_root_access_control_drift_is_detected(self) -> None:
        control_root = Path(self.temporary.name) / "control-acl"
        control_root.mkdir()
        expected = capture_filesystem_identity(control_root)
        changed_hash = "sha256:" + "f" * 64
        if changed_hash == expected.access_control_hash:
            changed_hash = "sha256:" + "e" * 64
        with mock.patch.object(
            git_identity,
            "_access_control_hash",
            return_value=changed_hash,
        ):
            result = revalidate_control_root(expected)
        self.assertFalse(result.ok)
        self.assertEqual("control_root_drift", result.reason)

    @unittest.skipUnless(os.name == "nt", "junction test is Windows-specific")
    def test_workspace_junction_replacement_is_detected_without_user_edits(
        self,
    ) -> None:
        expected = capture_workspace_identity(self.root, ("src/**",))
        before = repository_snapshot(self.root)
        moved = Path(self.temporary.name) / "repository-junction-original"
        replacement = Path(self.temporary.name) / "repository-junction-replacement"
        self.root.rename(moved)
        git(moved, "clone", "--no-hardlinks", str(moved), str(replacement))
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.root), str(replacement)],
            capture_output=True,
            check=False,
            shell=False,
        )
        if created.returncode != 0:
            moved.rename(self.root)
            self.skipTest(created.stderr.decode(errors="replace"))
        try:
            result = revalidate_workspace_identity(expected)
            self.assertFalse(result.ok)
            self.assertEqual("workspace_drift", result.reason)
            self.assertEqual(before, repository_snapshot(moved))
        finally:
            os.rmdir(self.root)
            moved.rename(self.root)

    @unittest.skipUnless(os.name == "nt", "junction test is Windows-specific")
    def test_control_root_reparse_replacement_is_detected(self) -> None:
        control_root = Path(self.temporary.name) / "control"
        replacement = Path(self.temporary.name) / "replacement"
        control_root.mkdir()
        replacement.mkdir()
        expected = capture_filesystem_identity(control_root)
        control_root.rmdir()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(control_root), str(replacement)],
            capture_output=True,
            check=True,
            shell=False,
        )
        result = revalidate_control_root(expected)
        self.assertFalse(result.ok)
        self.assertEqual("control_root_drift", result.reason)
        if control_root.exists():
            os.rmdir(control_root)


if __name__ == "__main__":
    unittest.main()
