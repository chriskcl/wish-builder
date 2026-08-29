from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from wish_builder.processes import containment as containment_module
from wish_builder.processes.containment import (
    AttachResult,
    ContainmentCapability,
    ContainmentStatus,
    KillResult,
    ProcessIdentity,
    TreeState,
    UnavailableContainment,
    create_containment_session,
    posix,
    windows,
)


class ContainmentTests(unittest.TestCase):
    def test_platform_backend_attaches_and_proves_tree_empty(self) -> None:
        session = create_containment_session()
        self.assertEqual(ContainmentStatus.PROVEN, session.capability.status)
        options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "close_fds": True,
        }
        if session.creation_flags:
            options["creationflags"] = session.creation_flags
        if session.start_new_session:
            options["start_new_session"] = True
        process = subprocess.Popen(  # type: ignore[call-overload]
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **options,
        )
        try:
            attached = session.attach(process)
            self.assertEqual(ContainmentStatus.PROVEN, attached.status, attached)
            assert attached.identity is not None
            self.assertEqual(process.pid, attached.identity.pid)
            self.assertEqual(TreeState.ACTIVE, session.tree_state())
            killed = session.kill_tree()
            self.assertIn(killed.state, {TreeState.ACTIVE, TreeState.EMPTY})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                process.poll()
                if session.tree_state() is TreeState.EMPTY:
                    break
                time.sleep(0.01)
            process.wait(timeout=1)
            self.assertIsNotNone(process.poll())
            self.assertEqual(TreeState.EMPTY, session.tree_state())
        finally:
            session.abort_start(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            session.close()

    def test_unavailable_backend_is_non_launching_and_unknown(self) -> None:
        session = UnavailableContainment(
            ContainmentStatus.UNSUPPORTED,
            "test",
            "not present",
        )
        self.assertEqual(ContainmentStatus.UNSUPPORTED, session.capability.status)
        self.assertEqual(0, session.creation_flags)
        self.assertFalse(session.start_new_session)
        self.assertEqual(TreeState.UNKNOWN, session.tree_state())
        self.assertEqual(TreeState.UNKNOWN, session.kill_tree().state)
        process = SimpleNamespace(kill=mock.Mock())
        self.assertEqual(ContainmentStatus.UNSUPPORTED, session.attach(process).status)
        session.abort_start(process)  # type: ignore[arg-type]
        process.kill.assert_called_once_with()
        session.close()

    def test_posix_session_state_and_kill_results_are_fail_closed(self) -> None:
        session = posix.PosixProcessGroupSession()
        process = SimpleNamespace(pid=1234, kill=mock.Mock())
        with (
            mock.patch.object(posix, "_linux_process_start_id", return_value="start:1"),
            mock.patch.object(posix.os, "getpgid", return_value=1234, create=True),
        ):
            attached = session.attach(process)  # type: ignore[arg-type]
        self.assertEqual(ContainmentStatus.PROVEN, attached.status)
        self.assertEqual(1234, attached.identity.pid if attached.identity else None)

        with mock.patch.object(posix.signal, "SIGKILL", 9, create=True):
            with mock.patch.object(posix.os, "killpg", return_value=None, create=True):
                self.assertEqual(TreeState.ACTIVE, session.tree_state())
                self.assertEqual(TreeState.ACTIVE, session.kill_tree().state)
            with mock.patch.object(
                posix.os,
                "killpg",
                side_effect=ProcessLookupError,
                create=True,
            ):
                self.assertEqual(TreeState.EMPTY, session.tree_state())
                self.assertEqual(TreeState.EMPTY, session.kill_tree().state)
            with mock.patch.object(
                posix.os,
                "killpg",
                side_effect=PermissionError("denied"),
                create=True,
            ):
                self.assertEqual(TreeState.UNKNOWN, session.tree_state())
                self.assertEqual(TreeState.UNKNOWN, session.kill_tree().state)
            with mock.patch.object(
                posix.os,
                "killpg",
                side_effect=OSError(errno.ESRCH, "gone"),
                create=True,
            ):
                self.assertEqual(TreeState.EMPTY, session.tree_state())
                self.assertEqual(TreeState.EMPTY, session.kill_tree().state)
            with mock.patch.object(
                posix.os,
                "killpg",
                side_effect=OSError(errno.EIO, "io"),
                create=True,
            ):
                self.assertEqual(TreeState.UNKNOWN, session.tree_state())
                self.assertEqual(TreeState.UNKNOWN, session.kill_tree().state)

    def test_posix_attach_and_abort_failure_paths(self) -> None:
        process = SimpleNamespace(pid=4321, kill=mock.Mock())
        for failure in (ProcessLookupError(), OSError("probe"), UnicodeError("text")):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(
                    posix,
                    "_linux_process_start_id",
                    side_effect=failure,
                ),
            ):
                result = posix.PosixProcessGroupSession().attach(  # type: ignore[arg-type]
                    process
                )
                self.assertEqual(ContainmentStatus.UNKNOWN, result.status)

        with (
            mock.patch.object(posix, "_linux_process_start_id", return_value="start:2"),
            mock.patch.object(posix.os, "getpgid", return_value=9999, create=True),
        ):
            wrong_group = posix.PosixProcessGroupSession().attach(  # type: ignore[arg-type]
                process
            )
        self.assertEqual(ContainmentStatus.UNKNOWN, wrong_group.status)

        unattached = posix.PosixProcessGroupSession()
        self.assertEqual(TreeState.UNKNOWN, unattached.tree_state())
        self.assertEqual(TreeState.UNKNOWN, unattached.kill_tree().state)
        with mock.patch.object(
            unattached,
            "kill_tree",
            return_value=KillResult(TreeState.UNKNOWN),
        ):
            unattached.abort_start(process)  # type: ignore[arg-type]
        process.kill.assert_called_once_with()
        process.kill.side_effect = OSError("gone")
        with mock.patch.object(
            unattached,
            "kill_tree",
            return_value=KillResult(TreeState.UNKNOWN),
        ):
            unattached.abort_start(process)  # type: ignore[arg-type]
        unattached.close()

    def test_posix_capability_probe_can_fail_closed(self) -> None:
        created = posix.PosixProcessGroupSession.create()
        if os.name == "nt":
            self.assertEqual(ContainmentStatus.UNSUPPORTED, created.capability.status)
        else:
            self.assertEqual(ContainmentStatus.PROVEN, created.capability.status)

    def test_linux_process_start_parser_is_strict(self) -> None:
        valid = b"1 (worker name) " + b" ".join([b"S", *([b"0"] * 18), b"12345"])
        cases = (
            (valid, "linux-proc-start:12345"),
            (b"missing close", OSError),
            (b"1 (x) S 0", OSError),
            (
                b"1 (x) " + b" ".join([b"S", *([b"0"] * 18), b"invalid"]),
                OSError,
            ),
        )
        for raw, expected in cases:
            with (
                self.subTest(raw=raw),
                mock.patch.object(
                    posix.Path,
                    "read_bytes",
                    return_value=raw,
                ),
            ):
                if isinstance(expected, str):
                    self.assertEqual(expected, posix._linux_process_start_id(1))
                else:
                    with self.assertRaises(expected):
                        posix._linux_process_start_id(1)

    def test_containment_selector_unknown_platform_is_unsupported(self) -> None:
        with mock.patch.object(
            containment_module,
            "os",
            SimpleNamespace(name="unknown-os"),
        ):
            selected = containment_module.create_containment_session()
        self.assertEqual(ContainmentStatus.UNSUPPORTED, selected.capability.status)
        self.assertEqual("none", selected.capability.backend)

    @unittest.skipUnless(os.name == "nt", "Windows API failure mapping")
    def test_windows_job_creation_failures_are_unavailable(self) -> None:
        def kernel(**overrides):
            api = SimpleNamespace(
                CreateJobObjectW=mock.Mock(return_value=123),
                SetInformationJobObject=mock.Mock(return_value=True),
                QueryInformationJobObject=mock.Mock(return_value=True),
                CloseHandle=mock.Mock(return_value=True),
            )
            for name, value in overrides.items():
                setattr(api, name, value)
            return api

        cases = (
            kernel(CreateJobObjectW=mock.Mock(return_value=0)),
            kernel(SetInformationJobObject=mock.Mock(return_value=False)),
            kernel(QueryInformationJobObject=mock.Mock(return_value=False)),
        )
        for api in cases:
            with (
                self.subTest(api=api),
                mock.patch.object(windows.ctypes, "WinDLL", return_value=api),
                mock.patch.object(windows.WindowsJobSession, "_configure_signatures"),
            ):
                result = windows.WindowsJobSession.create()
                self.assertEqual(ContainmentStatus.UNKNOWN, result.capability.status)

        def query_bad_flags(job, info_class, pointer, size, returned):
            pointer._obj.BasicLimitInformation.LimitFlags = (  # type: ignore[attr-defined]
                windows.JOB_OBJECT_LIMIT_BREAKAWAY_OK
            )
            return True

        api = kernel(QueryInformationJobObject=mock.Mock(side_effect=query_bad_flags))
        with (
            mock.patch.object(windows.ctypes, "WinDLL", return_value=api),
            mock.patch.object(windows.WindowsJobSession, "_configure_signatures"),
        ):
            invalid_limits = windows.WindowsJobSession.create()
        self.assertEqual(ContainmentStatus.UNKNOWN, invalid_limits.capability.status)

        with mock.patch.object(
            windows.ctypes, "WinDLL", side_effect=OSError("missing")
        ):
            unsupported = windows.WindowsJobSession.create()
        self.assertEqual(ContainmentStatus.UNSUPPORTED, unsupported.capability.status)

    @unittest.skipUnless(os.name == "nt", "Windows API failure mapping")
    def test_windows_session_api_failures_are_unknown(self) -> None:
        api = SimpleNamespace(
            AssignProcessToJobObject=mock.Mock(return_value=False),
            IsProcessInJob=mock.Mock(return_value=False),
            QueryInformationJobObject=mock.Mock(return_value=False),
            TerminateJobObject=mock.Mock(return_value=False),
            GetProcessTimes=mock.Mock(return_value=False),
            CloseHandle=mock.Mock(return_value=True),
        )
        process = SimpleNamespace(pid=123, _handle=456, kill=mock.Mock())
        session = windows.WindowsJobSession(api, 1)
        with mock.patch.object(windows.ctypes, "get_last_error", return_value=5):
            denied = session.attach(process)  # type: ignore[arg-type]
        self.assertEqual(ContainmentStatus.UNSUPPORTED, denied.status)
        with mock.patch.object(windows.ctypes, "get_last_error", return_value=87):
            unknown = session.attach(process)  # type: ignore[arg-type]
        self.assertEqual(ContainmentStatus.UNKNOWN, unknown.status)
        self.assertEqual(TreeState.UNKNOWN, session.tree_state())
        self.assertEqual(TreeState.UNKNOWN, session.kill_tree().state)
        session.abort_start(process)  # type: ignore[arg-type]
        process.kill.assert_called_once_with()
        process.kill.side_effect = OSError("gone")
        session.abort_start(process)  # type: ignore[arg-type]

        closed = windows.WindowsJobSession(api, 1)
        closed.close()
        self.assertEqual(
            ContainmentStatus.UNKNOWN,
            closed.attach(process).status,  # type: ignore[arg-type]
        )
        self.assertEqual(TreeState.UNKNOWN, closed.tree_state())
        self.assertEqual(TreeState.UNKNOWN, closed.kill_tree().state)
        closed.close()

        membership = windows.WindowsJobSession(api, 1)
        api.AssignProcessToJobObject.return_value = True
        self.assertEqual(
            ContainmentStatus.UNKNOWN,
            membership.attach(process).status,  # type: ignore[arg-type]
        )
        self.assertEqual(TreeState.UNKNOWN, membership.tree_state())
        self.assertEqual(TreeState.UNKNOWN, membership.kill_tree().state)
        with self.assertRaises(OSError):
            membership._creation_time(456)

    @unittest.skipUnless(os.name == "nt", "Windows API failure mapping")
    def test_windows_resume_thread_failures_are_unknown(self) -> None:
        def api(**overrides):
            value = SimpleNamespace(
                CreateToolhelp32Snapshot=mock.Mock(return_value=1),
                Thread32First=mock.Mock(return_value=False),
                Thread32Next=mock.Mock(return_value=False),
                OpenThread=mock.Mock(return_value=0),
                ResumeThread=mock.Mock(return_value=0),
                CloseHandle=mock.Mock(return_value=True),
            )
            for name, item in overrides.items():
                setattr(value, name, item)
            return value

        with self.assertRaises(OSError):
            windows.WindowsJobSession(
                api(CreateToolhelp32Snapshot=mock.Mock(return_value=0)), 1
            )._resume_initial_thread(123)
        with self.assertRaises(OSError):
            windows.WindowsJobSession(api(), 1)._resume_initial_thread(123)

        def first(snapshot, pointer):
            pointer._obj.th32OwnerProcessID = 123  # type: ignore[attr-defined]
            pointer._obj.th32ThreadID = 456  # type: ignore[attr-defined]
            return True

        one_thread = {
            "Thread32First": mock.Mock(side_effect=first),
            "Thread32Next": mock.Mock(return_value=False),
        }
        with self.assertRaises(OSError):
            windows.WindowsJobSession(api(**one_thread), 1)._resume_initial_thread(123)
        with self.assertRaises(OSError):
            windows.WindowsJobSession(
                api(
                    **one_thread,
                    OpenThread=mock.Mock(return_value=2),
                    ResumeThread=mock.Mock(return_value=0),
                ),
                1,
            )._resume_initial_thread(123)

    def test_contracts_reject_false_proof(self) -> None:
        with self.assertRaises(ValueError):
            ContainmentCapability(ContainmentStatus.PROVEN, "")
        with self.assertRaises(ValueError):
            ProcessIdentity(0, "start", "container")
        with self.assertRaises(ValueError):
            AttachResult(ContainmentStatus.PROVEN)
        with self.assertRaises(ValueError):
            AttachResult(
                ContainmentStatus.UNKNOWN,
                ProcessIdentity(1, "start", "container"),
            )
        with self.assertRaises(TypeError):
            KillResult("empty")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            UnavailableContainment(ContainmentStatus.PROVEN, "test", "bad")

    @unittest.skipUnless(os.name == "nt", "Windows-specific suspended launch")
    def test_windows_backend_uses_suspended_launch(self) -> None:
        session = create_containment_session()
        try:
            self.assertNotEqual(0, session.creation_flags & 0x4)
            self.assertEqual("windows_job_object", session.capability.backend)
        finally:
            session.close()

    @unittest.skipUnless(os.name == "posix", "POSIX-specific process group")
    def test_posix_backend_uses_a_new_session(self) -> None:
        session = create_containment_session()
        try:
            self.assertTrue(session.start_new_session)
            self.assertEqual("posix_process_group", session.capability.backend)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
