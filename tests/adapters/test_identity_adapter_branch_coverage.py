from __future__ import annotations

import ctypes
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wish_builder.adapters import git_identity, process_identity
from wish_builder.adapters.git_identity import (
    ControlRootComparison,
    FilesystemIdentity,
    GitIdentityError,
    ProtectedControlRoot,
)
from wish_builder.adapters.process_identity import (
    LeaseOwnerProcessProbeResult,
    LeaseOwnerProcessState,
)
from wish_builder.contracts import ActorIdentity, ActorType, LeaseOwner


def filesystem_identity(
    *,
    lexical_path: str = "control",
    is_link: bool = False,
) -> FilesystemIdentity:
    return FilesystemIdentity(
        lexical_path=lexical_path,
        canonical_path=lexical_path,
        link_device=1,
        link_inode=2,
        target_device=1,
        target_inode=2,
        is_link_or_reparse_point=is_link,
        access_control_hash="sha256:" + "a" * 64,
    )


def lease_owner(*, host_id: str = "host-local") -> LeaseOwner:
    return LeaseOwner(
        ActorIdentity(
            ActorType.COORDINATOR,
            "coordinator-test",
            host_id,
            123,
            "process-start-test",
        ),
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
    )


def fingerprint_run_git(payload: bytes):
    def run_git(
        repository: Path,
        arguments: object,
        *,
        stream_to: object | None = None,
    ) -> bytes:
        del repository, arguments
        if stream_to is not None:
            return b""
        return payload

    return run_git


class GitIdentityErrorBranchTests(unittest.TestCase):
    def test_path_and_filesystem_probe_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            with self.assertRaisesRegex(GitIdentityError, "path_unavailable"):
                git_identity._canonical_existing_path(missing)

            lexical = Path(temporary).absolute()
            resolved = lexical.resolve(strict=True)
            with (
                mock.patch.object(
                    git_identity,
                    "_canonical_existing_path",
                    return_value=(lexical, resolved),
                ),
                mock.patch.object(git_identity.os, "lstat", side_effect=OSError("race")),
                self.assertRaisesRegex(GitIdentityError, "filesystem_identity_failed"),
            ):
                git_identity.capture_filesystem_identity(temporary)

    def test_posix_access_control_hash_uses_mode_and_owner(self) -> None:
        path_stat = SimpleNamespace(st_gid=20, st_mode=stat.S_IFDIR | 0o750, st_uid=10)
        control_path = Path("control")
        with mock.patch.object(git_identity.os, "name", "posix"):
            observed = git_identity._access_control_hash(control_path, path_stat)
        self.assertRegex(observed, r"^sha256:[0-9a-f]{64}$")

    def test_windows_access_control_probe_rejects_query_failures(self) -> None:
        control_path = Path("control")
        get_security = mock.Mock(return_value=False)
        library = SimpleNamespace(GetFileSecurityW=get_security)
        with (
            mock.patch.object(git_identity.os, "name", "nt"),
            mock.patch.object(ctypes, "WinDLL", return_value=library, create=True),
            mock.patch.object(ctypes, "get_last_error", return_value=5, create=True),
            self.assertRaisesRegex(GitIdentityError, "filesystem_acl_failed: 5"),
        ):
            git_identity._access_control_hash(control_path, SimpleNamespace())

        def return_descriptor(
            path: object,
            security: object,
            descriptor: object,
            size: int,
            required: object,
        ) -> bool:
            del path, security
            if size == 0:
                required._obj.value = 4  # type: ignore[attr-defined]
                return False
            descriptor[0] = 1  # type: ignore[index]
            return True

        get_security = mock.Mock(side_effect=return_descriptor)
        library = SimpleNamespace(GetFileSecurityW=get_security)
        with (
            mock.patch.object(git_identity.os, "name", "nt"),
            mock.patch.object(ctypes, "WinDLL", return_value=library, create=True),
            mock.patch.object(ctypes, "get_last_error", return_value=122, create=True),
        ):
            observed = git_identity._access_control_hash(
                control_path,
                SimpleNamespace(),
            )
        self.assertRegex(observed, r"^sha256:[0-9a-f]{64}$")

        def fail_descriptor(
            path: object,
            security: object,
            descriptor: object,
            size: int,
            required: object,
        ) -> bool:
            del path, security, descriptor
            if size == 0:
                required._obj.value = 4  # type: ignore[attr-defined]
            return False

        get_security = mock.Mock(side_effect=fail_descriptor)
        library = SimpleNamespace(GetFileSecurityW=get_security)
        with (
            mock.patch.object(git_identity.os, "name", "nt"),
            mock.patch.object(ctypes, "WinDLL", return_value=library, create=True),
            mock.patch.object(
                ctypes,
                "get_last_error",
                side_effect=(122, 5),
                create=True,
            ),
            self.assertRaisesRegex(GitIdentityError, "filesystem_acl_failed: 5"),
        ):
            git_identity._access_control_hash(control_path, SimpleNamespace())

    def test_scope_normalization_covers_type_and_dot_prefixes(self) -> None:
        with self.assertRaisesRegex(GitIdentityError, "scope must be a string"):
            git_identity._normalize_scope(123)
        self.assertEqual("src/**", git_identity._normalize_scope("././src/**/"))

    def test_git_launch_and_utf8_errors_are_stable(self) -> None:
        with (
            mock.patch.object(git_identity.subprocess, "Popen", side_effect=OSError("no git")),
            self.assertRaisesRegex(GitIdentityError, "git_unavailable: git"),
        ):
            git_identity._run_git(Path("repository"), ("status",))

        with (
            mock.patch.object(git_identity, "_run_git", return_value=b"\xff"),
            self.assertRaisesRegex(GitIdentityError, "git_output_invalid_utf8"),
        ):
            git_identity._git_text(Path("repository"), "status")

    def test_untracked_git_paths_reject_invalid_bytes_and_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            for payload, reason in (
                (b"\xff\0", "git_path_invalid_utf8"),
                (b"../escape\0", "git_path_escape"),
                (b"missing/file.txt\0", "git_path_escape"),
            ):
                with (
                    self.subTest(payload=payload),
                    mock.patch.object(
                        git_identity,
                        "_run_git",
                        side_effect=fingerprint_run_git(payload),
                    ),
                    self.assertRaisesRegex(GitIdentityError, reason),
                ):
                    git_identity._scope_fingerprint(repository, ("src/**",))

    def test_untracked_symlink_is_hashed_and_identity_races_fail_closed(self) -> None:
        link_stat = SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o777,
            st_dev=10,
            st_ino=20,
        )
        changed_stat = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_dev=10,
            st_ino=21,
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve(strict=True)
            candidate = repository / "link.txt"
            real_lstat = os.lstat

            def stable_lstat(path):
                if Path(path) == candidate:
                    return link_stat
                return real_lstat(path)

            run_git = fingerprint_run_git(b"link.txt\0")
            with (
                mock.patch.object(git_identity, "_run_git", side_effect=run_git),
                mock.patch.object(git_identity.os, "lstat", side_effect=stable_lstat),
                mock.patch.object(git_identity.os, "readlink", return_value="target.txt"),
            ):
                digest = git_identity._scope_fingerprint(repository, ("src/**",))
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

            candidate_stats = iter((link_stat, changed_stat))

            def racing_lstat(path):
                if Path(path) == candidate:
                    return next(candidate_stats)
                return real_lstat(path)

            with (
                mock.patch.object(git_identity, "_run_git", side_effect=run_git),
                mock.patch.object(
                    git_identity.os,
                    "lstat",
                    side_effect=racing_lstat,
                ),
                mock.patch.object(git_identity.os, "readlink", return_value="target.txt"),
                self.assertRaisesRegex(GitIdentityError, "workspace_probe_race"),
            ):
                git_identity._scope_fingerprint(repository, ("src/**",))

    def test_untracked_regular_file_identity_race_and_special_file_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            candidate = repository / "dirty.txt"
            candidate.write_bytes(b"dirty")
            observed = os.lstat(candidate)
            changed = SimpleNamespace(
                st_mode=observed.st_mode,
                st_dev=observed.st_dev,
                st_ino=observed.st_ino + 1,
            )
            run_git = fingerprint_run_git(b"dirty.txt\0")
            with (
                mock.patch.object(git_identity, "_run_git", side_effect=run_git),
                mock.patch.object(git_identity.os, "fstat", return_value=changed),
                self.assertRaisesRegex(GitIdentityError, "workspace_probe_race"),
            ):
                git_identity._scope_fingerprint(repository, ("src/**",))

            special = repository / "special"
            special.mkdir()
            with (
                mock.patch.object(
                    git_identity,
                    "_run_git",
                    side_effect=fingerprint_run_git(b"special\0"),
                ),
                self.assertRaisesRegex(GitIdentityError, "unsupported_dirty_path"),
            ):
                git_identity._scope_fingerprint(repository, ("src/**",))

    def test_workspace_rejects_empty_scope_detached_head_and_invalid_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            with self.assertRaisesRegex(GitIdentityError, "at least one scope"):
                git_identity.capture_workspace_identity(repository, ())

            paths = (str(repository), str(repository), str(repository))
            with (
                mock.patch.object(
                    git_identity,
                    "_git_text",
                    side_effect=(*paths, "HEAD"),
                ),
                self.assertRaisesRegex(GitIdentityError, "unsupported_head"),
            ):
                git_identity.capture_workspace_identity(repository, ("src/**",))

            for commit in ("abc", "g" * 40):
                with (
                    self.subTest(commit=commit),
                    mock.patch.object(
                        git_identity,
                        "_git_text",
                        side_effect=(*paths, "refs/heads/main", commit),
                    ),
                    self.assertRaisesRegex(GitIdentityError, "invalid_commit_identity"),
                ):
                    git_identity.capture_workspace_identity(repository, ("src/**",))

    def test_control_root_revalidation_handles_probe_failure(self) -> None:
        expected = filesystem_identity()
        with mock.patch.object(
            git_identity,
            "capture_filesystem_identity",
            side_effect=GitIdentityError("path_unavailable"),
        ):
            result = git_identity.revalidate_control_root(expected)
        self.assertEqual(
            ControlRootComparison(False, "control_root_drift", None),
            result,
        )


class ProtectedControlRootBranchTests(unittest.TestCase):
    def test_windows_handle_helpers_fail_closed(self) -> None:
        get_information = mock.Mock(return_value=False)
        kernel32 = SimpleNamespace(GetFileInformationByHandle=get_information)
        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32, create=True),
            mock.patch.object(ctypes, "get_last_error", return_value=6, create=True),
            mock.patch.object(
                ctypes,
                "WinError",
                side_effect=lambda error: OSError(error, "Windows error"),
                create=True,
            ),
            self.assertRaises(OSError),
        ):
            git_identity._windows_directory_handle_identity(10)

        def report_file(handle: int, information: object) -> bool:
            del handle
            information._obj.dwFileAttributes = 0x80  # type: ignore[attr-defined]
            return True

        get_information = mock.Mock(side_effect=report_file)
        kernel32 = SimpleNamespace(GetFileInformationByHandle=get_information)
        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32, create=True),
            self.assertRaisesRegex(OSError, "non-reparse directory"),
        ):
            git_identity._windows_directory_handle_identity(10)

        create_file = mock.Mock(return_value=0)
        kernel32 = SimpleNamespace(CreateFileW=create_file)
        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32, create=True),
            mock.patch.object(ctypes, "get_last_error", return_value=5, create=True),
            mock.patch.object(
                ctypes,
                "WinError",
                side_effect=lambda error: OSError(error, "Windows error"),
                create=True,
            ),
            self.assertRaises(OSError),
        ):
            git_identity._open_windows_directory_handle("control")

        close_handle = mock.Mock(return_value=False)
        kernel32 = SimpleNamespace(CloseHandle=close_handle)
        with (
            mock.patch.object(ctypes, "WinDLL", return_value=kernel32, create=True),
            mock.patch.object(ctypes, "get_last_error", return_value=6, create=True),
            mock.patch.object(
                ctypes,
                "WinError",
                side_effect=lambda error: OSError(error, "Windows error"),
                create=True,
            ),
            self.assertRaises(OSError),
        ):
            git_identity._close_windows_handle(10)

    def test_windows_handle_helpers_and_protected_root_succeed(self) -> None:
        def report_directory(handle: int, information: object) -> bool:
            self.assertEqual(10, handle)
            information._obj.dwFileAttributes = 0x10  # type: ignore[attr-defined]
            information._obj.dwVolumeSerialNumber = 7  # type: ignore[attr-defined]
            information._obj.nFileIndexHigh = 0  # type: ignore[attr-defined]
            information._obj.nFileIndexLow = 9  # type: ignore[attr-defined]
            return True

        get_information = mock.Mock(side_effect=report_directory)
        create_file = mock.Mock(return_value=10)
        close_handle = mock.Mock(return_value=True)
        kernel32 = SimpleNamespace(
            CloseHandle=close_handle,
            CreateFileW=create_file,
            GetFileInformationByHandle=get_information,
        )
        with mock.patch.object(
            ctypes,
            "WinDLL",
            return_value=kernel32,
            create=True,
        ):
            self.assertEqual(
                (7, 9, 0x10),
                git_identity._windows_directory_handle_identity(10),
            )
            self.assertEqual(
                10,
                git_identity._open_windows_directory_handle("control"),
            )
            git_identity._close_windows_handle(10)
        create_file.assert_called_once()
        close_handle.assert_called_once_with(10)

        expected = filesystem_identity()
        observed = ControlRootComparison(True, None, expected)
        protected = ProtectedControlRoot(expected, 10, (7, 9, 0x10), windows=True)
        with (
            mock.patch.object(
                git_identity,
                "revalidate_control_root",
                return_value=observed,
            ),
            mock.patch.object(
                git_identity,
                "_windows_directory_handle_identity",
                return_value=(7, 9, 0x10),
            ),
        ):
            self.assertEqual(observed, protected.revalidate())
        with mock.patch.object(git_identity, "_close_windows_handle") as close:
            protected.close()
        close.assert_called_once_with(10)

    def test_constructor_rejects_invalid_runtime_values(self) -> None:
        expected = filesystem_identity()
        invalid_cases = (
            ((object(), 1, (1,), False), TypeError),
            ((expected, -1, (1,), False), ValueError),
            ((expected, 1, (), False), ValueError),
            ((expected, 1, (1,), 1), TypeError),
        )
        for arguments, error_type in invalid_cases:
            with self.subTest(arguments=arguments), self.assertRaises(error_type):
                ProtectedControlRoot(*arguments[:3], windows=arguments[3])  # type: ignore[arg-type]

    def test_open_denies_links_and_wraps_windows_open_failure(self) -> None:
        linked = filesystem_identity(is_link=True)
        with (
            mock.patch.object(git_identity, "capture_filesystem_identity", return_value=linked),
            self.assertRaisesRegex(GitIdentityError, "reparse roots are denied"),
        ):
            ProtectedControlRoot.open("control")

        expected = filesystem_identity()
        with (
            mock.patch.object(git_identity, "capture_filesystem_identity", return_value=expected),
            mock.patch.object(git_identity.os, "name", "nt"),
            mock.patch.object(
                git_identity,
                "_open_windows_directory_handle",
                side_effect=OSError("denied"),
            ),
            self.assertRaisesRegex(GitIdentityError, "control_root_handle_failed"),
        ):
            ProtectedControlRoot.open("control")

    def test_posix_open_failures_close_allocated_handles(self) -> None:
        expected = filesystem_identity()
        with (
            mock.patch.object(git_identity, "capture_filesystem_identity", return_value=expected),
            mock.patch.object(git_identity.os, "name", "posix"),
            mock.patch.object(git_identity.os, "open", side_effect=OSError("denied")),
            self.assertRaisesRegex(GitIdentityError, "control_root_handle_failed"),
        ):
            ProtectedControlRoot.open("control")

        close = mock.Mock(side_effect=OSError("close failed"))
        with (
            mock.patch.object(git_identity, "capture_filesystem_identity", return_value=expected),
            mock.patch.object(git_identity.os, "name", "posix"),
            mock.patch.object(git_identity.os, "open", return_value=12),
            mock.patch.object(git_identity.os, "fstat", side_effect=OSError("race")),
            mock.patch.object(git_identity.os, "close", close),
            self.assertRaisesRegex(GitIdentityError, "control_root_handle_failed"),
        ):
            ProtectedControlRoot.open("control")
        close.assert_called_once_with(12)

        close = mock.Mock()
        with (
            mock.patch.object(git_identity, "capture_filesystem_identity", return_value=expected),
            mock.patch.object(git_identity.os, "name", "posix"),
            mock.patch.object(git_identity.os, "open", return_value=13),
            mock.patch.object(
                git_identity.os,
                "fstat",
                return_value=SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o600,
                    st_dev=1,
                    st_ino=2,
                ),
            ),
            mock.patch.object(git_identity.os, "close", close),
            self.assertRaisesRegex(GitIdentityError, "control_root_handle_failed"),
        ):
            ProtectedControlRoot.open("control")
        close.assert_called_once_with(13)

    def test_posix_open_success_and_initial_revalidation_failure(self) -> None:
        expected = filesystem_identity()
        directory_stat = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_dev=1,
            st_ino=2,
        )
        ok = ControlRootComparison(True, None, expected)
        with (
            mock.patch.object(git_identity, "capture_filesystem_identity", return_value=expected),
            mock.patch.object(git_identity.os, "name", "posix"),
            mock.patch.object(git_identity.os, "open", return_value=12),
            mock.patch.object(git_identity.os, "fstat", return_value=directory_stat),
            mock.patch.object(ProtectedControlRoot, "revalidate", return_value=ok),
        ):
            protected = ProtectedControlRoot.open("control")
        with mock.patch.object(git_identity.os, "close") as close:
            protected.close()
        close.assert_called_once_with(12)

        drift = ControlRootComparison(False, "control_root_drift", expected)
        with (
            mock.patch.object(git_identity, "capture_filesystem_identity", return_value=expected),
            mock.patch.object(git_identity.os, "name", "posix"),
            mock.patch.object(git_identity.os, "open", return_value=13),
            mock.patch.object(git_identity.os, "fstat", return_value=directory_stat),
            mock.patch.object(ProtectedControlRoot, "revalidate", return_value=drift),
            mock.patch.object(ProtectedControlRoot, "close") as close,
            self.assertRaisesRegex(GitIdentityError, "control_root_drift"),
        ):
            ProtectedControlRoot.open("control")
        close.assert_called_once()

    def test_posix_revalidate_handles_type_and_identity_drift(self) -> None:
        expected = filesystem_identity()
        observed = ControlRootComparison(True, None, expected)
        protected = ProtectedControlRoot(expected, 12, (1, 2, stat.S_IFDIR | 0o700), windows=False)

        with (
            mock.patch.object(git_identity, "revalidate_control_root", return_value=observed),
            mock.patch.object(
                git_identity.os,
                "fstat",
                return_value=SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=2),
            ),
        ):
            self.assertFalse(protected.revalidate().ok)

        with (
            mock.patch.object(git_identity, "revalidate_control_root", return_value=observed),
            mock.patch.object(
                git_identity.os,
                "fstat",
                return_value=SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700,
                    st_dev=1,
                    st_ino=3,
                ),
            ),
        ):
            self.assertFalse(protected.revalidate().ok)

        with (
            mock.patch.object(git_identity, "revalidate_control_root", return_value=observed),
            mock.patch.object(
                git_identity.os,
                "fstat",
                return_value=SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700,
                    st_dev=1,
                    st_ino=2,
                ),
            ),
        ):
            self.assertEqual(observed, protected.revalidate())

    def test_closed_context_cannot_be_reentered(self) -> None:
        protected = ProtectedControlRoot(filesystem_identity(), 12, (1,), windows=False)
        with mock.patch.object(git_identity.os, "close"):
            protected.close()
        with self.assertRaisesRegex(RuntimeError, "is closed"):
            protected.__enter__()


class ProcessIdentityBranchTests(unittest.TestCase):
    def test_probe_result_rejects_invalid_state_and_detail(self) -> None:
        with self.assertRaisesRegex(TypeError, "LeaseOwnerProcessState"):
            LeaseOwnerProcessProbeResult("dead")  # type: ignore[arg-type]
        for detail in ("", 1):
            with self.subTest(detail=detail), self.assertRaisesRegex(ValueError, "detail"):
                LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD, detail)  # type: ignore[arg-type]

    def test_posix_capture_requires_proc_and_dispatches_to_linux_helper(self) -> None:
        path = mock.Mock()
        with (
            mock.patch.object(process_identity.os, "name", "posix"),
            mock.patch.object(process_identity, "Path", return_value=path),
        ):
            path.is_dir.return_value = False
            with self.assertRaisesRegex(OSError, "unavailable"):
                process_identity.capture_process_start_id(123)

            path.is_dir.return_value = True
            with mock.patch.object(
                process_identity,
                "_linux_process_start_id",
                return_value="linux-proc-start:1",
            ) as capture:
                self.assertEqual(
                    "linux-proc-start:1",
                    process_identity.capture_process_start_id(123),
                )
            capture.assert_called_once_with(123)

    def test_top_level_probe_validates_input_and_dispatches(self) -> None:
        owner = lease_owner()
        with self.assertRaisesRegex(TypeError, "LeaseOwner"):
            process_identity.probe_lease_owner_process(object(), local_host_id="host-local")  # type: ignore[arg-type]
        for host_id in ("", 1):
            with self.subTest(host_id=host_id), self.assertRaisesRegex(ValueError, "local_host_id"):
                process_identity.probe_lease_owner_process(owner, local_host_id=host_id)  # type: ignore[arg-type]

        remote = process_identity.probe_lease_owner_process(
            lease_owner(host_id="host-remote"),
            local_host_id="host-local",
        )
        self.assertEqual(LeaseOwnerProcessState.DIFFERENT_HOST, remote.state)

        expected = LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
        for host_name, helper in (
            ("posix", "_probe_posix_process"),
            ("nt", "_probe_windows_process"),
        ):
            with (
                self.subTest(host_name=host_name),
                mock.patch.object(process_identity.os, "name", host_name),
                mock.patch.object(process_identity, helper, return_value=expected) as probe,
            ):
                self.assertEqual(
                    expected,
                    process_identity.probe_lease_owner_process(
                        owner,
                        local_host_id="host-local",
                    ),
                )
            probe.assert_called_once_with(123, "process-start-test")

        with mock.patch.object(process_identity.os, "name", "unsupported"):
            unknown = process_identity.probe_lease_owner_process(
                owner,
                local_host_id="host-local",
            )
        self.assertEqual(LeaseOwnerProcessState.UNKNOWN, unknown.state)

    def test_posix_probe_covers_unavailable_dead_unknown_alive_and_reused(self) -> None:
        path = mock.Mock()
        with mock.patch.object(process_identity, "Path", return_value=path):
            path.is_dir.return_value = False
            self.assertEqual(
                LeaseOwnerProcessState.UNKNOWN,
                process_identity._probe_posix_process(123, "expected").state,
            )

            path.is_dir.return_value = True
            cases = (
                (FileNotFoundError(), LeaseOwnerProcessState.DEAD),
                (OSError("denied"), LeaseOwnerProcessState.UNKNOWN),
                (UnicodeError("bad"), LeaseOwnerProcessState.UNKNOWN),
                ("expected", LeaseOwnerProcessState.EXACT_ALIVE),
                ("other", LeaseOwnerProcessState.PID_REUSED),
            )
            for result, state in cases:
                kwargs = (
                    {"side_effect": result}
                    if isinstance(result, BaseException)
                    else {"return_value": result}
                )
                with (
                    self.subTest(state=state),
                    mock.patch.object(process_identity, "_linux_process_start_id", **kwargs),
                ):
                    self.assertEqual(
                        state,
                        process_identity._probe_posix_process(123, "expected").state,
                    )

    def test_linux_start_parser_rejects_malformed_values(self) -> None:
        path = mock.Mock()
        with mock.patch.object(process_identity, "Path", return_value=path):
            for raw, message in (
                (b"malformed", "malformed"),
                (b"1 (name) S 1 2", "incomplete"),
                (b"1 (name) " + b" ".join([b"S"] + [b"1"] * 18 + [b"x"]), "invalid"),
            ):
                path.read_bytes.return_value = raw
                with self.subTest(message=message), self.assertRaisesRegex(OSError, message):
                    process_identity._linux_process_start_id(123)

            path.read_bytes.return_value = (
                b"1 (name with parens) " + b" ".join([b"S"] + [b"1"] * 18 + [b"42"])
            )
            self.assertEqual(
                "linux-proc-start:42",
                process_identity._linux_process_start_id(123),
            )

    def test_windows_probe_covers_dead_unknown_alive_and_reused(self) -> None:
        cases = (
            (ProcessLookupError(), LeaseOwnerProcessState.DEAD),
            (OSError("denied"), LeaseOwnerProcessState.UNKNOWN),
            (TypeError("bad"), LeaseOwnerProcessState.UNKNOWN),
            (ValueError("bad"), LeaseOwnerProcessState.UNKNOWN),
            ("expected", LeaseOwnerProcessState.EXACT_ALIVE),
            ("other", LeaseOwnerProcessState.PID_REUSED),
        )
        for result, state in cases:
            kwargs = (
                {"side_effect": result}
                if isinstance(result, BaseException)
                else {"return_value": result}
            )
            with (
                self.subTest(state=state),
                mock.patch.object(process_identity, "_windows_process_start_id", **kwargs),
            ):
                self.assertEqual(
                    state,
                    process_identity._probe_windows_process(123, "expected").state,
                )

    def test_windows_start_probe_handles_open_and_time_failures(self) -> None:
        open_process = mock.Mock(return_value=0)
        kernel32 = SimpleNamespace(
            OpenProcess=open_process,
            GetProcessTimes=mock.Mock(),
            CloseHandle=mock.Mock(),
        )
        with (
            mock.patch.object(
                process_identity.ctypes,
                "WinDLL",
                return_value=kernel32,
                create=True,
            ),
            mock.patch.object(
                process_identity.ctypes,
                "get_last_error",
                return_value=87,
                create=True,
            ),
            self.assertRaises(ProcessLookupError),
        ):
            process_identity._windows_process_start_id(123)

        with (
            mock.patch.object(
                process_identity.ctypes,
                "WinDLL",
                return_value=kernel32,
                create=True,
            ),
            mock.patch.object(
                process_identity.ctypes,
                "get_last_error",
                return_value=5,
                create=True,
            ),
            self.assertRaisesRegex(OSError, "OpenProcess failed"),
        ):
            process_identity._windows_process_start_id(123)

        close_handle = mock.Mock(return_value=True)
        kernel32 = SimpleNamespace(
            OpenProcess=mock.Mock(return_value=10),
            GetProcessTimes=mock.Mock(return_value=False),
            CloseHandle=close_handle,
        )
        with (
            mock.patch.object(
                process_identity.ctypes,
                "WinDLL",
                return_value=kernel32,
                create=True,
            ),
            mock.patch.object(
                process_identity.ctypes,
                "get_last_error",
                return_value=6,
                create=True,
            ),
            self.assertRaisesRegex(OSError, "GetProcessTimes failed"),
        ):
            process_identity._windows_process_start_id(123)
        close_handle.assert_called_once_with(10)

    def test_windows_start_probe_treats_exited_process_with_open_handle_as_dead(
        self,
    ) -> None:
        def exited_process_times(
            _: object,
            creation: object,
            exit_time: object,
            __: object,
            ___: object,
        ) -> bool:
            creation._obj.dwLowDateTime = 123  # type: ignore[attr-defined]
            exit_time._obj.dwLowDateTime = 456  # type: ignore[attr-defined]
            return True

        close_handle = mock.Mock(return_value=True)
        kernel32 = SimpleNamespace(
            OpenProcess=mock.Mock(return_value=10),
            GetProcessTimes=mock.Mock(side_effect=exited_process_times),
            CloseHandle=close_handle,
        )
        with (
            mock.patch.object(
                process_identity.ctypes,
                "WinDLL",
                return_value=kernel32,
                create=True,
            ),
            self.assertRaises(ProcessLookupError),
        ):
            process_identity._windows_process_start_id(123)
        close_handle.assert_called_once_with(10)

    def test_windows_start_probe_returns_creation_time_for_live_process(self) -> None:
        def live_process_times(
            _: object,
            creation: object,
            exit_time: object,
            __: object,
            ___: object,
        ) -> bool:
            creation._obj.dwLowDateTime = 123  # type: ignore[attr-defined]
            exit_time._obj.dwLowDateTime = 0  # type: ignore[attr-defined]
            return True

        close_handle = mock.Mock(return_value=True)
        kernel32 = SimpleNamespace(
            OpenProcess=mock.Mock(return_value=10),
            GetProcessTimes=mock.Mock(side_effect=live_process_times),
            CloseHandle=close_handle,
        )
        with mock.patch.object(
            process_identity.ctypes,
            "WinDLL",
            return_value=kernel32,
            create=True,
        ):
            self.assertEqual(
                "windows-filetime:123",
                process_identity._windows_process_start_id(123),
            )
        close_handle.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
