from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from wish_builder.contracts import OutcomeKind, RuntimeReasonCode
from wish_builder.processes import (
    ContainmentStatus,
    EnvironmentVariable,
    ProcessConfigurationError,
    ProcessFailure,
    ProcessLimits,
    ProcessOutcomeStatus,
    ProcessRequest,
    ProcessRunner,
    StreamLimits,
    UnavailableContainment,
    capture_directory_identity,
    capture_executable_identity,
)
from wish_builder.processes import runner as runner_module
from wish_builder.processes.containment import (
    AttachResult,
    KillResult,
    TreeState,
    create_containment_session,
)

RESULT_WRITER = r"""
def emit_result(payload):
    import os
    if os.name == "nt":
        import msvcrt
        raw = os.environ.pop("WISH_BUILDER_RESULT_HANDLE")
        descriptor = msvcrt.open_osfhandle(
            int(raw), os.O_WRONLY | getattr(os, "O_BINARY", 0)
        )
    else:
        descriptor = int(os.environ.pop("WISH_BUILDER_RESULT_FD"))
    os.write(descriptor, payload)
    os.close(descriptor)
"""


def process_is_active(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
            exit_code.value == still_active
        )
    finally:
        kernel32.CloseHandle(handle)


class _KillProofFailureSession:
    def __init__(self) -> None:
        self.delegate = create_containment_session()

    @property
    def capability(self):
        return self.delegate.capability

    @property
    def creation_flags(self):
        return self.delegate.creation_flags

    @property
    def start_new_session(self):
        return self.delegate.start_new_session

    def attach(self, process):
        return self.delegate.attach(process)

    def tree_state(self):
        return self.delegate.tree_state()

    def kill_tree(self):
        self.delegate.kill_tree()
        return KillResult(TreeState.UNKNOWN, "injected kill proof failure")

    def abort_start(self, process):
        self.delegate.abort_start(process)

    def close(self):
        self.delegate.close()


class _AttachFailureSession:
    def __init__(self, status: ContainmentStatus) -> None:
        self.delegate = create_containment_session()
        self.status = status

    @property
    def capability(self):
        return self.delegate.capability

    @property
    def creation_flags(self):
        return self.delegate.creation_flags

    @property
    def start_new_session(self):
        return self.delegate.start_new_session

    def attach(self, process):
        return AttachResult(self.status, detail="injected attach failure")

    def tree_state(self):
        return self.delegate.tree_state()

    def kill_tree(self):
        return self.delegate.kill_tree()

    def abort_start(self, process):
        self.delegate.abort_start(process)

    def close(self):
        self.delegate.close()


class _UnknownAfterExitSession(_KillProofFailureSession):
    def tree_state(self):
        state = self.delegate.tree_state()
        return TreeState.UNKNOWN if state is TreeState.EMPTY else state


class _RaisingAttachSession(_KillProofFailureSession):
    def attach(self, process):
        raise RuntimeError("injected attach crash")


class _RaisingKillSession(_KillProofFailureSession):
    def kill_tree(self):
        self.delegate.kill_tree()
        raise RuntimeError("injected kill crash")


class _RaisingCloseSession:
    def __init__(self) -> None:
        self.delegate = UnavailableContainment(
            ContainmentStatus.UNSUPPORTED,
            "test",
            "unavailable",
        )

    @property
    def capability(self):
        return self.delegate.capability

    def close(self):
        raise RuntimeError("injected close crash")


class ProcessRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        code: str,
        *,
        arguments: tuple[str, ...] = (),
        environment: tuple[EnvironmentVariable, ...] = (),
        timeout: float = 5.0,
        limits: ProcessLimits | None = None,
        cwd: Path | None = None,
    ) -> ProcessRequest:
        return ProcessRequest.create(
            executable=sys.executable,
            arguments=("-c", code, *arguments),
            cwd=self.root if cwd is None else cwd,
            environment=environment,
            timeout_seconds=timeout,
            limits=limits,
        )

    def test_exact_argv_environment_cwd_stdin_and_result_channel(self) -> None:
        code = (
            RESULT_WRITER
            + r"""
import json
import os
import sys
stdin_bytes = sys.stdin.buffer.read()
payload = {
    "argv": sys.argv[1:],
    "cwd": os.path.normcase(os.path.realpath(os.getcwd())),
    "environment": dict(os.environ),
    "stdin": stdin_bytes.decode("ascii"),
}
print(json.dumps(payload, sort_keys=True), flush=True)
sys.stderr.buffer.write(b"stderr-frame\r\n")
sys.stderr.buffer.flush()
emit_result(b'{"result":"ok"}\n')
"""
        )
        arguments = ("two words", ";shell-token", "$(not-executed)")
        request = self.request(
            code,
            arguments=arguments,
            environment=(EnvironmentVariable("WISH_TEST_VALUE", "exact"),),
        )
        outcome = ProcessRunner(environment_allowlist=("WISH_TEST_VALUE",)).run(request)
        self.assertEqual(ProcessOutcomeStatus.SUCCESS, outcome.status, outcome)
        payload = json.loads(outcome.stdout.data)
        self.assertEqual(list(arguments), payload["argv"])
        self.assertEqual(
            os.path.normcase(str(self.root.resolve())),
            payload["cwd"],
        )
        observed_environment = payload["environment"]
        result_keys = {
            key
            for key in observed_environment
            if key.startswith("WISH_BUILDER_RESULT_")
        }
        self.assertEqual(1, len(result_keys))
        for key in result_keys:
            observed_environment.pop(key)
        if observed_environment.get("LC_CTYPE") == "C.UTF-8":
            observed_environment.pop("LC_CTYPE")
        self.assertEqual({"WISH_TEST_VALUE": "exact"}, observed_environment)
        self.assertEqual("", payload["stdin"])
        self.assertEqual(b"stderr-frame\r\n", outcome.stderr.data)
        self.assertEqual(b'{"result":"ok"}\n', outcome.admitted_result)
        self.assertIsNotNone(outcome.process_identity)
        self.assertEqual(ContainmentStatus.PROVEN, outcome.containment.status)
        self.assertTrue(outcome.termination_proven)
        self.assertFalse(outcome.termination_attempted)

    def test_public_result_channel_helper(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        code = r"""
from wish_builder.processes import open_result_channel
with open_result_channel() as channel:
    channel.write(b"one-result-frame\n")
"""
        outcome = ProcessRunner().run(self.request(code, cwd=repository))
        self.assertEqual(ProcessOutcomeStatus.SUCCESS, outcome.status, outcome)
        self.assertEqual(b"one-result-frame\n", outcome.result.data)
        self.assertEqual(1, outcome.result.frame_count)

    def test_nonzero_exit_is_a_named_terminal_outcome(self) -> None:
        outcome = ProcessRunner().run(self.request("raise SystemExit(17)"))
        self.assertEqual(ProcessOutcomeStatus.EXIT_FAILURE, outcome.status)
        self.assertEqual(OutcomeKind.TERMINAL, outcome.kind)
        self.assertEqual(RuntimeReasonCode.CHECK_FAILED, outcome.reason_code)
        self.assertEqual(ProcessFailure.NONZERO_EXIT, outcome.failure)
        self.assertEqual(17, outcome.exit_code)
        self.assertTrue(outcome.termination_proven)

    def test_environment_outside_allowlist_is_denied_before_launch(self) -> None:
        marker = self.root / "must-not-exist"
        request = self.request(
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
            environment=(EnvironmentVariable("DENIED_VALUE", "secret"),),
        )
        outcome = ProcessRunner().run(request)
        self.assertEqual(ProcessOutcomeStatus.START_FAILED, outcome.status)
        self.assertEqual(ProcessFailure.ENVIRONMENT_DENIED, outcome.failure)
        self.assertEqual(RuntimeReasonCode.PROCESS_START_FAILED, outcome.reason_code)
        self.assertFalse(marker.exists())

    def test_executable_deletion_and_replacement_are_named(self) -> None:
        copied = self.root / ("python-copy.exe" if os.name == "nt" else "python-copy")
        shutil.copy2(sys.executable, copied)
        if os.name == "posix":
            copied.chmod(copied.stat().st_mode | stat.S_IXUSR)
        identity = capture_executable_identity(copied)
        cwd = capture_directory_identity(self.root)
        request = ProcessRequest(
            identity,
            cwd,
            (identity.canonical_path,),
            (),
            1.0,
            ProcessLimits.defaults(),
        )
        copied.unlink()
        missing = ProcessRunner().run(request)
        self.assertEqual(ProcessFailure.EXECUTABLE_UNAVAILABLE, missing.failure)

        shutil.copy2(sys.executable, copied)
        if os.name == "posix":
            copied.chmod(copied.stat().st_mode | stat.S_IXUSR)
        identity = capture_executable_identity(copied)
        changed_request = ProcessRequest(
            identity,
            cwd,
            (identity.canonical_path,),
            (),
            1.0,
            ProcessLimits.defaults(),
        )
        with copied.open("ab") as handle:
            handle.write(b"identity-change")
        changed = ProcessRunner().run(changed_request)
        self.assertEqual(ProcessFailure.EXECUTABLE_IDENTITY_MISMATCH, changed.failure)

    def test_cwd_deletion_and_replacement_are_named(self) -> None:
        cwd = self.root / "attempt"
        cwd.mkdir()
        request = ProcessRequest.create(executable=sys.executable, cwd=cwd)
        cwd.rmdir()
        missing = ProcessRunner().run(request)
        self.assertEqual(ProcessFailure.CWD_UNAVAILABLE, missing.failure)

        cwd.mkdir()
        request = ProcessRequest.create(executable=sys.executable, cwd=cwd)
        moved = self.root / "attempt-old"
        cwd.rename(moved)
        cwd.mkdir()
        replaced = ProcessRunner().run(request)
        self.assertEqual(ProcessFailure.CWD_IDENTITY_MISMATCH, replaced.failure)

    def test_stdio_and_launch_failures_are_named(self) -> None:
        request = self.request("pass")
        with mock.patch.object(runner_module.os, "pipe", side_effect=OSError("pipe")):
            stdio = ProcessRunner().run(request)
        self.assertEqual(ProcessFailure.STDIO_SETUP_FAILED, stdio.failure)
        with mock.patch.object(
            runner_module.subprocess,
            "Popen",
            side_effect=OSError("launch"),
        ):
            launch = ProcessRunner().run(request)
        self.assertEqual(ProcessFailure.LAUNCH_FAILED, launch.failure)
        self.assertEqual(ProcessOutcomeStatus.START_FAILED, launch.status)

    def test_unsupported_and_unknown_containment_never_launch(self) -> None:
        marker = self.root / "must-not-launch"
        request = self.request(
            f"from pathlib import Path; Path({str(marker)!r}).touch()"
        )
        cases = (
            (
                ContainmentStatus.UNSUPPORTED,
                ProcessOutcomeStatus.CONTAINMENT_UNSUPPORTED,
            ),
            (ContainmentStatus.UNKNOWN, ProcessOutcomeStatus.CONTAINMENT_UNKNOWN),
        )
        for containment_status, outcome_status in cases:
            with self.subTest(status=containment_status):
                runner = ProcessRunner(
                    containment_factory=lambda status=containment_status: (
                        UnavailableContainment(status, "test", "injected")
                    )
                )
                outcome = runner.run(request)
                self.assertEqual(outcome_status, outcome.status)
                self.assertEqual(OutcomeKind.BLOCKED, outcome.kind)
                self.assertEqual(
                    RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
                    outcome.reason_code,
                )
                self.assertFalse(marker.exists())

    def test_attachment_failures_are_blocked_before_process_execution(self) -> None:
        marker = self.root / "not-executed"
        request = self.request(
            f"from pathlib import Path; Path({str(marker)!r}).touch()"
        )
        for status, expected in (
            (
                ContainmentStatus.UNSUPPORTED,
                ProcessOutcomeStatus.CONTAINMENT_UNSUPPORTED,
            ),
            (ContainmentStatus.UNKNOWN, ProcessOutcomeStatus.CONTAINMENT_UNKNOWN),
        ):
            with self.subTest(status=status):
                outcome = ProcessRunner(
                    containment_factory=lambda status=status: _AttachFailureSession(
                        status
                    ),
                    termination_grace_seconds=0.05,
                    poll_interval_seconds=0.005,
                ).run(request)
                self.assertEqual(expected, outcome.status)
                self.assertEqual(status, outcome.containment.status)
                self.assertTrue(outcome.termination_attempted)
                self.assertFalse(marker.exists())

    def test_unknown_tree_state_after_exit_is_blocked(self) -> None:
        outcome = ProcessRunner(
            containment_factory=_UnknownAfterExitSession,
            termination_grace_seconds=0.05,
            poll_interval_seconds=0.005,
        ).run(self.request("pass"))
        self.assertEqual(ProcessOutcomeStatus.CONTAINMENT_UNKNOWN, outcome.status)
        self.assertEqual(ProcessFailure.TREE_TERMINATION_UNKNOWN, outcome.failure)
        self.assertEqual(ContainmentStatus.UNKNOWN, outcome.containment.status)

    def test_capture_error_is_blocked_and_never_admitted(self) -> None:
        def fail_capture(collector) -> None:
            with collector._lock:
                collector.error = "OSError: injected capture failure"
                collector.complete = False
            collector.activity.set()

        with mock.patch.object(
            runner_module._StreamCollector,
            "_read",
            new=fail_capture,
        ):
            outcome = ProcessRunner(
                termination_grace_seconds=0.05,
                poll_interval_seconds=0.005,
            ).run(self.request("import time; time.sleep(30)"))
        self.assertEqual(ProcessOutcomeStatus.CONTAINMENT_UNKNOWN, outcome.status)
        self.assertIn(
            outcome.failure,
            {
                ProcessFailure.CAPTURE_INCOMPLETE,
                ProcessFailure.TREE_TERMINATION_UNKNOWN,
            },
        )
        self.assertIsNone(outcome.admitted_result)

    def test_containment_factory_and_close_crashes_are_named(self) -> None:
        request = self.request("pass")

        def broken_factory():
            raise RuntimeError("injected factory crash")

        factory = ProcessRunner(containment_factory=broken_factory).run(request)
        self.assertEqual(ProcessOutcomeStatus.CONTAINMENT_UNKNOWN, factory.status)
        self.assertEqual(ContainmentStatus.UNKNOWN, factory.containment.status)
        closed = ProcessRunner(containment_factory=_RaisingCloseSession).run(request)
        self.assertEqual(ProcessOutcomeStatus.CONTAINMENT_UNSUPPORTED, closed.status)

    def test_attach_and_kill_crashes_are_blocked(self) -> None:
        attach = ProcessRunner(
            containment_factory=_RaisingAttachSession,
            termination_grace_seconds=0.05,
            poll_interval_seconds=0.005,
        ).run(self.request("pass"))
        self.assertEqual(ProcessOutcomeStatus.CONTAINMENT_UNKNOWN, attach.status)
        self.assertIsNone(attach.admitted_result)

        killed = ProcessRunner(
            containment_factory=_RaisingKillSession,
            termination_grace_seconds=0.1,
            poll_interval_seconds=0.005,
        ).run(self.request("import time; time.sleep(30)", timeout=0.05))
        self.assertEqual(ProcessOutcomeStatus.CONTAINMENT_UNKNOWN, killed.status)
        self.assertEqual(ProcessFailure.TREE_TERMINATION_UNKNOWN, killed.failure)
        self.assertFalse(killed.termination_proven)

    def test_post_launch_stdio_and_thread_setup_crashes_are_named(self) -> None:
        request = self.request("import time; time.sleep(30)")
        with mock.patch.object(
            runner_module.os,
            "fdopen",
            side_effect=OSError("injected fdopen failure"),
        ):
            fdopen = ProcessRunner(
                termination_grace_seconds=0.1,
                poll_interval_seconds=0.005,
            ).run(request)
        self.assertEqual(ProcessOutcomeStatus.START_FAILED, fdopen.status)
        self.assertEqual(ProcessFailure.STDIO_SETUP_FAILED, fdopen.failure)
        self.assertTrue(fdopen.termination_proven)

        with mock.patch.object(
            runner_module._StreamCollector,
            "start",
            side_effect=RuntimeError("injected thread failure"),
        ):
            thread = ProcessRunner(
                termination_grace_seconds=0.1,
                poll_interval_seconds=0.005,
            ).run(request)
        self.assertEqual(ProcessOutcomeStatus.START_FAILED, thread.status)
        self.assertEqual(ProcessFailure.STDIO_SETUP_FAILED, thread.failure)
        self.assertTrue(thread.termination_proven)

    def test_result_channel_inheritance_setup_failure_is_named(self) -> None:
        request = self.request("pass")
        with mock.patch.object(
            runner_module.os,
            "set_inheritable",
            side_effect=OSError("injected inheritance failure"),
        ):
            outcome = ProcessRunner().run(request)
        self.assertEqual(ProcessOutcomeStatus.START_FAILED, outcome.status)
        self.assertEqual(ProcessFailure.STDIO_SETUP_FAILED, outcome.failure)

    def test_stdout_byte_flood_is_bounded_and_kills_tree(self) -> None:
        limits = ProcessLimits(
            StreamLimits(1024, 10_000, 128),
            StreamLimits(4096, 100),
            StreamLimits(4096, 100),
        )
        code = "import os,time; os.write(1, b'x' * 100000); time.sleep(30)"
        outcome = ProcessRunner().run(self.request(code, limits=limits))
        self.assertEqual(ProcessOutcomeStatus.OUTPUT_LIMIT_EXCEEDED, outcome.status)
        self.assertEqual(ProcessFailure.STDOUT_LIMIT_EXCEEDED, outcome.failure)
        self.assertEqual(RuntimeReasonCode.OUTPUT_LIMIT_EXCEEDED, outcome.reason_code)
        self.assertGreater(outcome.stdout.total_bytes, 1024)
        self.assertEqual(1024, len(outcome.stdout.data))
        self.assertLessEqual(len(outcome.stdout.diagnostic_tail), 128)
        self.assertTrue(outcome.termination_proven)
        self.assertIsNone(outcome.admitted_result)

    def test_stderr_frame_flood_has_an_independent_cap(self) -> None:
        limits = ProcessLimits(
            StreamLimits(100_000, 1000),
            StreamLimits(100_000, 3, 32),
            StreamLimits(100_000, 1000),
        )
        code = "import os,time; os.write(2, b'line\\n' * 20); time.sleep(30)"
        outcome = ProcessRunner().run(self.request(code, limits=limits))
        self.assertEqual(ProcessFailure.STDERR_LIMIT_EXCEEDED, outcome.failure)
        self.assertGreater(outcome.stderr.frame_count, 3)
        self.assertFalse(outcome.stdout.limit_exceeded)
        self.assertFalse(outcome.result.limit_exceeded)

    def test_result_flood_has_an_independent_cap_and_is_not_admitted(self) -> None:
        limits = ProcessLimits(
            StreamLimits(100_000, 1000),
            StreamLimits(100_000, 1000),
            StreamLimits(64, 1000, 16),
        )
        code = (
            RESULT_WRITER + "\nemit_result(b'r' * 10000)\nimport time; time.sleep(30)"
        )
        outcome = ProcessRunner().run(self.request(code, limits=limits))
        self.assertEqual(ProcessFailure.RESULT_LIMIT_EXCEEDED, outcome.failure)
        self.assertEqual(64, len(outcome.result.data))
        self.assertEqual(b"r" * 16, outcome.result.diagnostic_tail)
        self.assertIsNone(outcome.admitted_result)

    def test_monotonic_timeout_terminates_the_root(self) -> None:
        started = time.monotonic()
        outcome = ProcessRunner(termination_grace_seconds=2).run(
            self.request("import time; time.sleep(30)", timeout=0.15)
        )
        elapsed = time.monotonic() - started
        self.assertEqual(ProcessOutcomeStatus.TIMED_OUT, outcome.status, outcome)
        self.assertEqual(ProcessFailure.TIMED_OUT, outcome.failure)
        self.assertEqual(RuntimeReasonCode.EXTERNAL_TIMEOUT, outcome.reason_code)
        self.assertTrue(outcome.termination_attempted)
        self.assertTrue(outcome.termination_proven)
        self.assertLess(elapsed, 5)
        assert outcome.process_identity is not None
        self.assertFalse(process_is_active(outcome.process_identity.pid))

    def test_expired_prelaunch_deadline_has_no_process_effect(self) -> None:
        marker = self.root / "not-launched-after-deadline"
        samples = iter((0.0, 2.0, 2.0))
        runner = ProcessRunner(monotonic=lambda: next(samples, 2.0))
        outcome = runner.run(
            self.request(
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
                timeout=1,
            )
        )
        self.assertEqual(ProcessOutcomeStatus.TIMED_OUT, outcome.status)
        self.assertEqual(ProcessFailure.TIMED_OUT, outcome.failure)
        self.assertIsNone(outcome.process_identity)
        self.assertTrue(outcome.termination_proven)
        self.assertFalse(marker.exists())

    def test_child_and_grandchild_are_gone_after_timeout(self) -> None:
        leaf = "import time; time.sleep(30)"
        middle = (
            "import subprocess,sys,time; "
            f"p=subprocess.Popen([sys.executable,'-c',{leaf!r}]); "
            "print('grandchild='+str(p.pid), flush=True); time.sleep(30)"
        )
        root = (
            "import subprocess,sys,time; "
            f"p=subprocess.Popen([sys.executable,'-c',{middle!r}]); "
            "print('child='+str(p.pid), flush=True); time.sleep(30)"
        )
        outcome = ProcessRunner(termination_grace_seconds=3).run(
            self.request(root, timeout=0.5)
        )
        self.assertEqual(ProcessOutcomeStatus.TIMED_OUT, outcome.status, outcome)
        self.assertTrue(outcome.termination_proven)
        pids = {
            key: int(value)
            for key, value in (
                line.split("=", 1)
                for line in outcome.stdout.data.decode().splitlines()
                if "=" in line
            )
        }
        self.assertEqual({"child", "grandchild"}, set(pids), outcome.stdout.data)
        for pid in pids.values():
            self.assertFalse(process_is_active(pid), f"process {pid} survived")

    def test_root_exit_waits_for_late_descendant_output(self) -> None:
        child = (
            "import sys,time; time.sleep(0.15); "
            "print('late-output', flush=True); time.sleep(0.05)"
        )
        root = (
            "import subprocess,sys; "
            f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
            "print('root-output', flush=True)"
        )
        started = time.monotonic()
        outcome = ProcessRunner().run(self.request(root, timeout=2))
        elapsed = time.monotonic() - started
        self.assertEqual(ProcessOutcomeStatus.SUCCESS, outcome.status, outcome)
        self.assertIn(b"root-output", outcome.stdout.data)
        self.assertIn(b"late-output", outcome.stdout.data)
        self.assertGreaterEqual(elapsed, 0.1)
        self.assertTrue(outcome.stdout.complete)

    def test_kill_proof_failure_is_blocked_even_when_cleanup_succeeds(self) -> None:
        outcome = ProcessRunner(
            containment_factory=_KillProofFailureSession,
            termination_grace_seconds=1,
        ).run(self.request("import time; time.sleep(30)", timeout=0.1))
        self.assertEqual(ProcessOutcomeStatus.CONTAINMENT_UNKNOWN, outcome.status)
        self.assertEqual(ProcessFailure.TREE_TERMINATION_UNKNOWN, outcome.failure)
        self.assertEqual(OutcomeKind.BLOCKED, outcome.kind)
        self.assertEqual(
            RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
            outcome.reason_code,
        )
        self.assertFalse(outcome.termination_proven)
        self.assertEqual(ContainmentStatus.UNKNOWN, outcome.containment.status)
        self.assertIsNone(outcome.admitted_result)

    @unittest.skipUnless(os.name == "nt", "Windows Job breakaway test")
    def test_windows_job_contains_a_breakaway_request(self) -> None:
        code = r"""
import subprocess
import sys
try:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=0x01000000,
    )
except OSError:
    print("breakaway-denied", flush=True)
else:
    print("breakaway-started=" + str(child.pid), flush=True)
"""
        outcome = ProcessRunner().run(self.request(code, timeout=0.5))
        try:
            if b"breakaway-denied" in outcome.stdout.data:
                self.assertEqual(ProcessOutcomeStatus.SUCCESS, outcome.status, outcome)
            else:
                self.assertEqual(
                    ProcessOutcomeStatus.TIMED_OUT, outcome.status, outcome
                )
                pid = int(outcome.stdout.data.split(b"=", 1)[1])
                self.assertFalse(process_is_active(pid))
        finally:
            if b"breakaway-started=" in outcome.stdout.data:
                pid = int(outcome.stdout.data.split(b"=", 1)[1])
                import ctypes

                handle = ctypes.WinDLL("kernel32", use_last_error=True).OpenProcess(
                    1, False, pid
                )
                if handle:
                    ctypes.WinDLL("kernel32", use_last_error=True).TerminateProcess(
                        handle, 1
                    )

    def test_request_rejects_untyped_or_ambiguous_inputs(self) -> None:
        executable = capture_executable_identity(sys.executable)
        cwd = capture_directory_identity(self.root)
        with self.assertRaisesRegex(
            ProcessConfigurationError, "executable_argv_mismatch"
        ):
            ProcessRequest(
                executable,
                cwd,
                ("different",),
                (),
                1,
                ProcessLimits.defaults(),
            )
        with self.assertRaisesRegex(ProcessConfigurationError, "duplicate_environment"):
            ProcessRequest(
                executable,
                cwd,
                (executable.canonical_path,),
                (
                    EnvironmentVariable("DUPLICATE", "one"),
                    EnvironmentVariable("DUPLICATE", "two"),
                ),
                1,
                ProcessLimits.defaults(),
            )
        with self.assertRaisesRegex(ProcessConfigurationError, "reserved_environment"):
            EnvironmentVariable("WISH_BUILDER_RESULT_FD", "4")
        for timeout in (0, -1, float("inf"), float("nan")):
            with (
                self.subTest(timeout=timeout),
                self.assertRaisesRegex(ProcessConfigurationError, "invalid_timeout"),
            ):
                ProcessRequest(
                    executable,
                    cwd,
                    (executable.canonical_path,),
                    (),
                    timeout,
                    ProcessLimits.defaults(),
                )

    def test_typed_contract_validation_rejects_invalid_values(self) -> None:
        executable = capture_executable_identity(sys.executable)
        cwd = capture_directory_identity(self.root)
        for change in (
            {"lexical_path": ""},
            {"canonical_path": ""},
            {"link_inode": -1},
            {"sha256": "not-a-digest"},
        ):
            with self.subTest(executable=change), self.assertRaises(ValueError):
                replace(executable, **change)
        for change in (
            {"lexical_path": ""},
            {"canonical_path": ""},
            {"target_inode": -1},
        ):
            with self.subTest(directory=change), self.assertRaises(ValueError):
                replace(cwd, **change)
        for name, value in (
            ("1INVALID", "value"),
            ("VALID", "value\x00tail"),
            ("VALID", "x" * (1024 * 1024 + 1)),
        ):
            with (
                self.subTest(environment=name),
                self.assertRaises(ProcessConfigurationError),
            ):
                EnvironmentVariable(name, value)
        for values in ((0, 1, 1), (1, 0, 1), (1, 1, 0)):
            with (
                self.subTest(limits=values),
                self.assertRaises(ProcessConfigurationError),
            ):
                StreamLimits(*values)
        with self.assertRaises(TypeError):
            ProcessLimits("bad", StreamLimits(1, 1), StreamLimits(1, 1))  # type: ignore[arg-type]

    def test_request_and_runner_reject_wrong_container_types(self) -> None:
        executable = capture_executable_identity(sys.executable)
        cwd = capture_directory_identity(self.root)
        limits = ProcessLimits.defaults()
        invalid_requests = (
            ("bad", cwd, (executable.canonical_path,), (), 1, limits),
            (executable, "bad", (executable.canonical_path,), (), 1, limits),
            (executable, cwd, [], (), 1, limits),
            (executable, cwd, (), (), 1, limits),
            (executable, cwd, (executable.canonical_path, None), (), 1, limits),
            (executable, cwd, (executable.canonical_path,), [], 1, limits),
            (executable, cwd, (executable.canonical_path,), (), 1, "bad"),
        )
        for values in invalid_requests:
            with (
                self.subTest(values=values[:2]),
                self.assertRaises((TypeError, ProcessConfigurationError)),
            ):
                ProcessRequest(*values)  # type: ignore[arg-type]
        with self.assertRaises(ProcessConfigurationError):
            ProcessRequest.create(
                executable=sys.executable,
                arguments=[],  # type: ignore[arg-type]
                cwd=self.root,
            )
        with self.assertRaises(TypeError):
            ProcessRunner().run("bad")  # type: ignore[arg-type]
        for kwargs in (
            {"environment_allowlist": []},
            {"environment_allowlist": ("1BAD",)},
            {"environment_allowlist": ("WISH_BUILDER_RESULT_FD",)},
            {"environment_allowlist": ("DUP", "DUP")},
            {"termination_grace_seconds": 0},
            {"termination_grace_seconds": float("inf")},
            {"poll_interval_seconds": 0},
            {"poll_interval_seconds": float("nan")},
        ):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaises(ProcessConfigurationError),
            ):
                ProcessRunner(**kwargs)  # type: ignore[arg-type]

    def test_path_capture_rejects_wrong_kinds_and_read_failures(self) -> None:
        regular_file = self.root / "regular.txt"
        regular_file.write_text("content", encoding="utf-8")
        with self.assertRaisesRegex(
            ProcessConfigurationError, "executable_not_regular"
        ):
            capture_executable_identity(self.root)
        with self.assertRaisesRegex(ProcessConfigurationError, "cwd_not_directory"):
            capture_directory_identity(regular_file)
        with self.assertRaisesRegex(ProcessConfigurationError, "invalid_path"):
            capture_directory_identity(object())  # type: ignore[arg-type]
        with (
            mock.patch.object(runner_module.os, "open", side_effect=OSError("denied")),
            self.assertRaisesRegex(ProcessConfigurationError, "executable_unreadable"),
        ):
            capture_executable_identity(sys.executable)

    def test_outcome_contract_rejects_false_success_and_negative_duration(self) -> None:
        success = ProcessRunner().run(self.request("pass"))
        with self.assertRaises(ValueError):
            replace(success, kind=OutcomeKind.TERMINAL)
        with self.assertRaises(ValueError):
            replace(success, exit_code=1)
        with self.assertRaises(ValueError):
            replace(success, status=ProcessOutcomeStatus.EXIT_FAILURE)
        with self.assertRaises(ValueError):
            replace(success, duration_seconds=-1)

    def test_open_result_channel_rejects_missing_and_invalid_identity(self) -> None:
        environment = os.environ.copy()
        try:
            os.environ.pop("WISH_BUILDER_RESULT_HANDLE", None)
            os.environ.pop("WISH_BUILDER_RESULT_FD", None)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                runner_module.open_result_channel()
            key = (
                "WISH_BUILDER_RESULT_HANDLE"
                if os.name == "nt"
                else "WISH_BUILDER_RESULT_FD"
            )
            os.environ[key] = "not-an-integer"
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                runner_module.open_result_channel()
        finally:
            os.environ.clear()
            os.environ.update(environment)


if __name__ == "__main__":
    unittest.main()
