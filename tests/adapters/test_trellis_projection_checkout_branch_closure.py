from __future__ import annotations

import dataclasses
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wish_builder.adapters.git_identity import (
    FilesystemIdentity,
    IdentityComparison,
    WorkspaceIdentity,
)
from wish_builder.adapters.trellis import projection_checkout as checkout


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _identity(path: Path, *, link: bool = False) -> FilesystemIdentity:
    value = str(path.resolve())
    return FilesystemIdentity(value, value, 1, 2, 1, 2, link, _hash("a"))


def _workspace(repository: Path) -> WorkspaceIdentity:
    return WorkspaceIdentity(
        _hash("a"),
        _hash("b"),
        _identity(repository),
        _identity(repository),
        _identity(repository),
        "refs/heads/main",
        "c" * 40,
        (".trellis/tasks/**",),
        _hash("d"),
    )


class _ChunkStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


class _Popen:
    def __init__(self, chunks: list[bytes], return_code: int = 0) -> None:
        self.stdout = _ChunkStream(chunks)
        self.return_code = return_code
        self.killed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: int | None = None) -> int:
        return self.return_code


class TrellisAuthoritativeProjectionBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.workspace = _workspace(self.repository)

    def provider_shell(self) -> checkout.TrellisAuthoritativeProjectionProvider:
        provider = object.__new__(checkout.TrellisAuthoritativeProjectionProvider)
        provider.repository = self.repository
        provider.expected_workspace = self.workspace
        return provider

    def test_error_target_and_provider_validate_their_boundaries(self) -> None:
        error = checkout.TrellisProjectionCheckoutError("reason", "x" * 2048)
        self.assertEqual(1024, len(error.detail))
        self.assertIn(":", str(error))
        self.assertEqual(
            "reason",
            str(checkout.TrellisProjectionCheckoutError("reason")),
        )

        target_cases = (
            (Path("relative"), self.workspace, ValueError),
            (self.repository, object(), TypeError),
            (self.root, self.workspace, ValueError),
        )
        for path, workspace, exception in target_cases:
            with self.subTest(path=path), self.assertRaises(exception):
                checkout.TrellisAuthoritativeProjectionTarget(  # type: ignore[arg-type]
                    path,
                    workspace,
                )

        with self.assertRaises(TypeError):
            checkout.TrellisAuthoritativeProjectionProvider(  # type: ignore[arg-type]
                self.repository,
                object(),
            )
        with self.assertRaisesRegex(RuntimeError, "root_unavailable"):
            checkout.TrellisAuthoritativeProjectionProvider(
                self.root / "missing",
                self.workspace,
            )
        with self.assertRaisesRegex(RuntimeError, "identity_mismatch"):
            checkout.TrellisAuthoritativeProjectionProvider(
                self.root,
                self.workspace,
            )
        unsafe = dataclasses.replace(
            self.workspace,
            worktree_root=_identity(self.repository, link=True),
        )
        with self.assertRaisesRegex(RuntimeError, "root_unsafe"):
            checkout.TrellisAuthoritativeProjectionProvider(
                self.repository,
                unsafe,
            )

    def test_provider_revalidates_identity_but_allows_projection_and_head_drift(
        self,
    ) -> None:
        provider = self.provider_shell()
        for run_id in (None, "", "x" * 257):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                provider.ensure(run_id)  # type: ignore[arg-type]

        unavailable = IdentityComparison(
            False,
            "workspace_drift",
            ("git_probe_failed",),
            None,
        )
        with mock.patch.object(
            checkout,
            "revalidate_workspace_identity",
            return_value=unavailable,
        ), self.assertRaisesRegex(RuntimeError, "workspace_drift"):
            provider.ensure("WISH-2026-001")

        branch_drift = IdentityComparison(
            False,
            "workspace_drift",
            ("target_full_ref",),
            self.workspace,
        )
        with mock.patch.object(
            checkout,
            "revalidate_workspace_identity",
            return_value=branch_drift,
        ), self.assertRaisesRegex(RuntimeError, "target_full_ref"):
            provider.ensure("WISH-2026-001")

        advanced = dataclasses.replace(
            self.workspace,
            base_commit_sha="e" * 40,
            index_dirty_fingerprint=_hash("f"),
        )
        comparison = IdentityComparison(
            False,
            "workspace_drift",
            ("base_commit_sha", "index_dirty_fingerprint"),
            advanced,
        )
        with (
            mock.patch.object(
                checkout,
                "revalidate_workspace_identity",
                return_value=comparison,
            ),
            mock.patch.object(checkout, "_guard_authoritative_task_store"),
            mock.patch.object(
                checkout,
                "_changed_paths",
                side_effect=((), ("README.md",)),
            ),
        ):
            target = provider.ensure("WISH-2026-001")
            self.assertEqual(advanced, target.workspace)
            with self.assertRaisesRegex(RuntimeError, "workspace_dirty"):
                provider.ensure("WISH-2026-001")

        wrong_root = dataclasses.replace(
            self.workspace,
            worktree_root=_identity(self.root),
        )
        with (
            mock.patch.object(
                checkout,
                "revalidate_workspace_identity",
                return_value=IdentityComparison(True, None, (), wrong_root),
            ),
            self.assertRaisesRegex(RuntimeError, "worktree_root"),
        ):
            provider.ensure("WISH-2026-001")

    def test_authoritative_task_store_rejects_missing_unreadable_and_unsafe(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "store_missing"):
            checkout._guard_authoritative_task_store(self.repository)

        trellis = self.repository / ".trellis"
        trellis.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "store_unsafe"):
            checkout._guard_authoritative_task_store(self.repository)
        trellis.unlink()
        tasks = trellis / "tasks"
        tasks.mkdir(parents=True)
        checkout._guard_authoritative_task_store(self.repository)

        with mock.patch.object(checkout.os, "lstat", side_effect=PermissionError):
            with self.assertRaisesRegex(RuntimeError, "store_unreadable"):
                checkout._guard_authoritative_task_store(self.repository)

        fake_link = mock.Mock(st_mode=stat.S_IFLNK, st_file_attributes=0)
        with mock.patch.object(
            checkout.os,
            "lstat",
            side_effect=(os.lstat(trellis), fake_link),
        ):
            with self.assertRaisesRegex(RuntimeError, "store_unsafe"):
                checkout._guard_authoritative_task_store(self.repository)

    def test_changed_path_and_projection_path_guards_are_strict(self) -> None:
        with mock.patch.object(
            checkout,
            "_git",
            side_effect=(b"b\\path\0", b"a/path\0", b"a/path\0"),
        ):
            self.assertEqual(
                ("a/path", "b/path"),
                checkout._changed_paths(self.repository),
            )
        for raw in (b"\xff\0", b"/absolute\0", b"a/../escape\0"):
            with (
                self.subTest(raw=raw),
                mock.patch.object(checkout, "_git", side_effect=(raw, b"", b"")),
                self.assertRaises(checkout.TrellisProjectionCheckoutError),
            ):
                checkout._changed_paths(self.repository)

        self.assertTrue(
            checkout._is_projection_task_path(
                ".trellis/tasks/TASK-1/task.json"
            )
        )
        for value in (
            "task.json",
            "other/tasks/TASK-1/task.json",
            ".trellis/other/TASK-1/task.json",
            ".trellis/tasks//task.json",
            ".trellis/tasks/archive/task.json",
            ".trellis/tasks/TASK-1/other.json",
        ):
            with self.subTest(value=value):
                self.assertFalse(checkout._is_projection_task_path(value))

    def test_git_runner_wraps_unavailable_failure_and_output_limit(self) -> None:
        with mock.patch.object(
            checkout.subprocess,
            "Popen",
            side_effect=OSError("missing"),
        ):
            with self.assertRaisesRegex(RuntimeError, "git_unavailable"):
                checkout._git(self.repository, "status")

        process = _Popen([b"too large"])
        with (
            mock.patch.object(checkout, "_MAX_GIT_OUTPUT", 1),
            mock.patch.object(checkout.subprocess, "Popen", return_value=process),
            self.assertRaisesRegex(RuntimeError, "git_output_limit"),
        ):
            checkout._git(self.repository, "status")
        self.assertTrue(process.killed)

        failed = _Popen([], return_code=1)
        with (
            mock.patch.object(checkout.subprocess, "Popen", return_value=failed),
            self.assertRaisesRegex(RuntimeError, "git_command_failed"),
        ):
            checkout._git(self.repository, "status")

    def test_link_detection_covers_symlink_and_reparse_point(self) -> None:
        directory_stat = os.lstat(self.repository)
        self.assertFalse(checkout._is_link(directory_stat))
        symlink_stat = list(directory_stat)
        symlink_stat[0] = stat.S_IFLNK
        self.assertTrue(checkout._is_link(os.stat_result(symlink_stat)))
        reparse = mock.Mock(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        self.assertTrue(checkout._is_link(reparse))


if __name__ == "__main__":
    unittest.main()
