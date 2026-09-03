from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from scripts import ci_live_backend_qualification as live_harness
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.contracts.qualification_evidence import (
    QualificationEvidenceScenario,
)
from wish_builder.contracts.qualification_evidence_decoder import (
    decode_qualification_event_log_bytes,
)
from wish_builder.services.backend_qualification_builder import (
    verify_backend_qualification_candidate,
)
from wish_builder.services.ports import TurnState


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPOSITORY_ROOT / "scripts" / "ci_live_backend_qualification.py"
SDK_NAME = "@earendil-works/pi-coding-agent"
SDK_VERSION = "0.84.2"
SDK_SHASUM = "e4d4c1e769963c816959f5cea02a0a10ccc0495a"
SDK_INTEGRITY = "sha512-l4E+B7hgXKWddRo8bC/eSue2aWZjEgJ9xIpf5p0Og+lq8a2TArCwJ0HCoCPCgaBP/tN4zbYH/wOwvx9pJpeLCA=="
PROVIDER_RUNTIME = Path(getattr(sys, "_base_executable", sys.executable)).resolve()


FAKE_PROVIDER = r'''
import json
import os
import sys
import threading
import time
from pathlib import Path

def argument(name):
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return None

log_root = Path(os.environ["FAKE_LIVE_LOG"])
barrier = Path(os.environ["FAKE_LIVE_BARRIER"])
pid_root = Path(os.environ["FAKE_LIVE_PIDS"])
pid_root.mkdir(parents=True, exist_ok=True)
(pid_root / str(os.getpid())).write_text("live", encoding="ascii")
session_root = Path(argument("--session-dir") or ".")
session_root.mkdir(parents=True, exist_ok=True)
session_arg = argument("--session") or argument("--resume")
session_path = Path(session_arg) if session_arg else session_root / "session.jsonl"
write_lock = threading.Lock()
session_lock = threading.Lock()
abort = threading.Event()
streaming = threading.Event()

def emit(value):
    with write_lock:
        sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
        sys.stdout.flush()

def append_message(message):
    with session_lock:
        with session_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"type": "message", "message": message}, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

def finish(dispatch_id):
    normalized_dispatch_id = dispatch_id.lower()
    if "active-turn-cancellation" in normalized_dispatch_id:
        cancelled = abort.wait(15)
    else:
        cancelled = False
        if "sibling-overlap" in normalized_dispatch_id:
            barrier.mkdir(parents=True, exist_ok=True)
            (barrier / str(os.getpid())).write_text("ready", encoding="ascii")
            deadline = time.monotonic() + 10
            while len(tuple(barrier.iterdir())) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)
        else:
            time.sleep(0.05)
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "cancelled" if cancelled else "done"}],
        "stopReason": "aborted" if cancelled else "stop",
    }
    append_message(assistant)
    emit({"type": "agent_end", "messages": [assistant], "willRetry": False})
    emit({"type": "agent_settled"})
    streaming.clear()

for raw in sys.stdin.buffer:
    command = json.loads(raw.decode("utf-8"))
    kind = command.get("type")
    request_id = command.get("id")
    if kind == "get_state":
        emit({
            "id": request_id,
            "type": "response",
            "command": kind,
            "success": True,
            "data": {
                "sessionId": "fake-session-" + str(os.getpid()),
                "sessionFile": str(session_path.resolve()),
                "isStreaming": streaming.is_set(),
                "messageCount": 0,
            },
        })
    elif kind == "prompt":
        packet = json.loads(command["message"])
        dispatch_id = packet["execution"]["dispatch_id"]
        recorded_at = time.monotonic_ns()
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / (str(os.getpid()) + "-" + str(recorded_at) + ".json")
        with log_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"dispatchId": dispatch_id, "pid": os.getpid(), "at": recorded_at, "argv": sys.argv[1:]}, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        append_message({"role": "user", "content": [{"type": "text", "text": command["message"]}]})
        abort.clear()
        streaming.set()
        emit({"id": request_id, "type": "response", "command": kind, "success": True})
        emit({"type": "agent_start"})
        threading.Thread(target=finish, args=(dispatch_id,), daemon=True).start()
    elif kind == "abort":
        abort.set()
        emit({"id": request_id, "type": "response", "command": kind, "success": True})
    else:
        emit({"id": request_id, "type": "response", "command": kind, "success": False, "error": "unsupported"})
'''


def _run(command: tuple[str, ...], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ("tasklist", "/FI", f"PID eq {pid}", "/NH"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return str(pid) in result.stdout.decode("utf-8", errors="replace")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class LiveBackendQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "repo"
        self.workspace.mkdir()
        (self.workspace / "seed.txt").write_text("qualification\n", encoding="ascii")
        for command in (
            ("git", "init", "-q"),
            ("git", "config", "user.email", "qualification@example.invalid"),
            ("git", "config", "user.name", "Qualification Test"),
            ("git", "add", "seed.txt"),
            ("git", "commit", "-qm", "seed"),
        ):
            completed = _run(command, cwd=self.workspace)
            self.assertEqual(0, completed.returncode, completed.stderr.decode())
        revision = _run(("git", "rev-parse", "HEAD"), cwd=self.workspace)
        self.revision = revision.stdout.decode("ascii").strip()

        self.providers = self.root / "providers"
        package = self.providers / "node_modules" / "@earendil-works" / "pi-coding-agent"
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps({"name": SDK_NAME, "version": SDK_VERSION}),
            encoding="utf-8",
        )
        (package / "dist" / "cli.js").write_text(
            textwrap.dedent(FAKE_PROVIDER), encoding="utf-8"
        )
        (self.providers / "package-lock.json").write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "node_modules/" + SDK_NAME: {
                            "version": SDK_VERSION,
                            "integrity": SDK_INTEGRITY,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.log = self.root / "provider-prompts"
        self.barrier = self.root / "overlap-barrier"
        self.pids = self.root / "provider-pids"
        os.environ["FAKE_LIVE_LOG"] = str(self.log)
        os.environ["FAKE_LIVE_BARRIER"] = str(self.barrier)
        os.environ["FAKE_LIVE_PIDS"] = str(self.pids)
        self.addCleanup(os.environ.pop, "FAKE_LIVE_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_LIVE_BARRIER", None)
        self.addCleanup(os.environ.pop, "FAKE_LIVE_PIDS", None)

    def command(self, output: Path) -> tuple[str, ...]:
        current_platform = Platform.WINDOWS if os.name == "nt" else Platform.LINUX
        return (
            str(Path(sys.executable).resolve()),
            str(HARNESS),
            "--provider", Provider.PI.value,
            "--platform", current_platform.value,
            "--run-id", "QUAL-PI-LIVE-TEST",
            "--sdk-name", SDK_NAME,
            "--sdk-version", SDK_VERSION,
            "--sdk-shasum", SDK_SHASUM,
            "--sdk-integrity", SDK_INTEGRITY,
            "--source-revision", self.revision,
            "--workspace", str(self.workspace),
            "--providers-root", str(self.providers),
            "--runtime", str(PROVIDER_RUNTIME),
            "--provider-env", "FAKE_LIVE_LOG",
            "--provider-env", "FAKE_LIVE_BARRIER",
            "--provider-env", "FAKE_LIVE_PIDS",
            "--provider-model", "provider/test-model",
            "--output", str(output),
            "--timeout-seconds", "20",
            "--provenance-kind", "provider",
            "--provenance-issuer", "https://provider.example.invalid",
            "--provenance-reference", "https://provider.example.invalid/runs/qual-pi-live-test",
            "--provenance-identity", "provider:test:pi",
        )

    def test_live_harness_requires_turns_overlap_recovery_and_cleanup(self) -> None:
        output = self.root / "evidence"
        completed = _run(self.command(output), cwd=REPOSITORY_ROOT)
        self.assertEqual(
            0,
            completed.returncode,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        report = json.loads(completed.stdout)
        self.assertEqual("candidate_unverified", report["status"])
        candidate = verify_backend_qualification_candidate(output)
        self.assertEqual(2, candidate.artifact.observed_max_concurrent_turns)

        prompts = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.log.glob("*.json"))
        ]
        self.assertEqual(6, len(prompts), "a handshake-only probe must not qualify")
        current_platform = Platform.WINDOWS if os.name == "nt" else Platform.LINUX
        profile_args = load_bundled_compatibility().platform(
            Provider.PI, current_platform
        ).launch_profile.args
        for prompt in prompts:
            argv = prompt["argv"]
            self.assertEqual(list(profile_args), argv[: len(profile_args)])
            self.assertEqual("--session-dir", argv[len(profile_args)])
            self.assertTrue(Path(argv[len(profile_args) + 1]).is_absolute())
            self.assertEqual(
                ["--model", "provider/test-model"],
                argv[len(profile_args) + 2 :],
            )
        crash = [
            item
            for item in prompts
            if "crash-reconcile" in item["dispatchId"].lower()
        ]
        self.assertEqual(1, len(crash), "restart must inspect without resending")

        decoded = decode_qualification_event_log_bytes((output / "events.jsonl").read_bytes())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None
        overlap = [
            item
            for item in decoded.value
            if item.scenario is QualificationEvidenceScenario.SIBLING_OVERLAP
        ]
        starts = [item.monotonic_ns for item in overlap if item.event_type.value == "turn_started"]
        ends = [item.monotonic_ns for item in overlap if item.event_type.value == "turn_terminal"]
        self.assertEqual(2, len(starts))
        self.assertEqual(2, len(ends))
        self.assertLess(max(starts), min(ends), "sibling intervals must truly overlap")

        recorded_pids = [int(item.name) for item in self.pids.iterdir()]
        self.assertTrue(recorded_pids)
        self.assertFalse(
            [pid for pid in recorded_pids if _pid_exists(pid)],
            "cleanup must terminate every provider process tree",
        )

    def test_missing_explicit_environment_fails_without_output(self) -> None:
        output = self.root / "missing-env-evidence"
        command = (*self.command(output), "--provider-env", "MISSING_LIVE_SECRET")
        completed = _run(command, cwd=REPOSITORY_ROOT)
        self.assertNotEqual(0, completed.returncode)
        self.assertFalse(output.exists())
        self.assertIn(b"provider_environment_missing", completed.stderr)

    def test_provider_argument_override_is_not_exposed(self) -> None:
        output = self.root / "provider-argument-evidence"
        command = (*self.command(output), "--provider-arg=--unsafe")
        completed = _run(command, cwd=REPOSITORY_ROOT)
        self.assertNotEqual(0, completed.returncode)
        self.assertFalse(output.exists())
        self.assertIn(b"unrecognized arguments", completed.stderr)

    def test_provider_model_selector_is_bounded_and_jsonl_only(self) -> None:
        self.assertEqual(
            "openai/gpt-5.5",
            live_harness._provider_model("openai/gpt-5.5", Provider.OMP),
        )
        for value in ("--unsafe", "openai/model/extra", "provider/"):
            with self.subTest(value=value), self.assertRaises(
                live_harness.LiveQualificationError
            ) as raised:
                live_harness._provider_model(value, Provider.OMP)
            self.assertEqual("invalid_provider_model", raised.exception.code)
        with self.assertRaises(live_harness.LiveQualificationError) as raised:
            live_harness._provider_model("openai/gpt-5.5", Provider.CODEX)
        self.assertEqual("provider_model_not_supported", raised.exception.code)

    def test_run_id_uses_the_runtime_stable_id_contract(self) -> None:
        self.assertEqual(
            "QUAL-CODEX-001",
            live_harness._validate_run_id("QUAL-CODEX-001"),
        )
        for value in ("qual-codex-001", "QUAL.CODEX.001", "A" * 65):
            with self.subTest(value=value), self.assertRaises(
                live_harness.LiveQualificationError
            ) as raised:
                live_harness._validate_run_id(value)
            self.assertEqual("invalid_run_id", raised.exception.code)

    def test_terminal_mismatch_names_scenario_and_states(self) -> None:
        with self.assertRaises(live_harness.LiveQualificationError) as raised:
            live_harness._finish_attempt(
                None,
                SimpleNamespace(
                    scenario=QualificationEvidenceScenario.SIBLING_OVERLAP
                ),
                None,
                SimpleNamespace(state=TurnState.FAILED),
                "qualifier-main",
                1.0,
            )

        self.assertEqual("turn_terminal_state_mismatch", raised.exception.code)
        self.assertEqual(
            "scenario=sibling_overlap expected=done observed=failed",
            raised.exception.message,
        )

    def test_windows_worktree_cleanup_retries_transient_sharing_locks(self) -> None:
        locked = PermissionError(13, "locked", str(self.root))
        locked.winerror = 32
        with (
            mock.patch.object(live_harness.os, "name", "nt"),
            mock.patch.object(
                live_harness.shutil, "rmtree", side_effect=(locked, None)
            ) as remove,
            mock.patch.object(
                live_harness.time, "monotonic", side_effect=(0.0, 0.1)
            ),
            mock.patch.object(live_harness.time, "sleep") as sleep,
        ):
            live_harness._remove_tree(self.root)
        self.assertEqual(2, remove.call_count)
        sleep.assert_called_once()

    def test_windows_worktree_cleanup_fails_after_the_retry_deadline(self) -> None:
        locked = PermissionError(13, "locked", str(self.root))
        locked.winerror = 32
        with (
            mock.patch.object(live_harness.os, "name", "nt"),
            mock.patch.object(
                live_harness.shutil, "rmtree", side_effect=locked
            ) as remove,
            mock.patch.object(
                live_harness.time, "monotonic", side_effect=(0.0, 4.9, 5.0)
            ),
            mock.patch.object(live_harness.time, "sleep"),
            self.assertRaises(PermissionError),
        ):
            live_harness._remove_tree(self.root)
        self.assertEqual(2, remove.call_count)


if __name__ == "__main__":
    unittest.main()
