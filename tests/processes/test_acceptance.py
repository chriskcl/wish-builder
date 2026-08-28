from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from wish_builder.contracts import RiskLevel
from wish_builder.contracts.manifest_v2 import CommandSpec, ManifestTask, NetworkPolicy
from wish_builder.contracts.runtime import ExecutionIdentity, RuntimeReasonCode
from wish_builder.processes.acceptance import ProcessAcceptancePort
from wish_builder.processes.runner import capture_executable_identity
from wish_builder.services.promotion import PromotionRecord

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


def digest(character: str) -> str:
    return "sha256:" + character * 64


def executable_digest() -> str:
    return "sha256:" + capture_executable_identity(sys.executable).sha256


def command(code: str, *, timeout: int = 5) -> CommandSpec:
    return CommandSpec(
        "python",
        executable_digest(),
        ("python", "-c", RESULT_WRITER + "\n" + code),
        ".",
        timeout,
        65_536,
        65_536,
        65_536,
        (),
        NetworkPolicy.DENIED,
        "Run acceptance command",
    )


def ordinary_command(code: str, *, timeout: int = 5) -> CommandSpec:
    return replace(
        command(""),
        argv=("python", "-c", code),
        timeout_seconds=timeout,
    )


def task(*commands: CommandSpec, task_id: str = "TASK-001") -> ManifestTask:
    return ManifestTask(
        task_id,
        "Accept the result",
        ("REQ-001",),
        (),
        ("src/**",),
        (),
        ("Acceptance commands pass",),
        commands,
        "Revert the result",
        (),
        0,
        RiskLevel.MEDIUM,
        False,
        digest("2"),
        (digest("3"),),
        None,
    )


def promotion(task_id: str = "TASK-001") -> PromotionRecord:
    return PromotionRecord(
        task_id,
        0,
        "a" * 40,
        "b" * 40,
        "c" * 40,
        "d" * 40,
        digest("4"),
    )


class ProcessAcceptancePortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name)
        self.identity = ExecutionIdentity(
            "WISH-2026-001",
            1,
            "TASK-001",
            1,
            "CORRELATION-TASK-001-0001-EPOCH-0001",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def port(self, *, identity: ExecutionIdentity | None = None) -> ProcessAcceptancePort:
        return ProcessAcceptancePort(
            identity=self.identity if identity is None else identity,
            executable_profiles={"python": sys.executable},
            clock=lambda: "2026-08-19T00:00:20Z",
        )

    def test_accepts_after_running_every_typed_command_in_candidate_repository(self) -> None:
        first = command(
            "from pathlib import Path\n"
            "Path('first.txt').write_text('one\\n', encoding='utf-8')\n"
            "emit_result(b'{\"command\":1}')\n"
        )
        second = command(
            "from pathlib import Path\n"
            "Path('second.txt').write_text('two\\n', encoding='utf-8')\n"
            "emit_result(b'{\"command\":2}')\n"
        )

        result = self.port().verify(task(first, second), self.repository, promotion())

        self.assertTrue(result.accepted, result)
        self.assertIsNone(result.reason_code)
        self.assertEqual(1, len(result.evidence))
        evidence = result.evidence[0]
        self.assertEqual(self.identity, evidence.producer.identity)
        self.assertEqual("acceptance-TASK-001", evidence.producer.external_object_id)
        self.assertGreater(evidence.byte_length, 0)
        self.assertEqual("one\n", (self.repository / "first.txt").read_text())
        self.assertEqual("two\n", (self.repository / "second.txt").read_text())

    def test_shell_tokens_are_passed_as_argv_not_interpreted(self) -> None:
        marker = self.repository / "shell-must-not-run"
        code = (
            "import sys\n"
            "from pathlib import Path\n"
            "Path('argv.txt').write_text(sys.argv[1], encoding='utf-8')\n"
            "emit_result(b'{\"argv\":\"literal\"}')\n"
        )
        spec = replace(
            command(code),
            argv=("python", "-c", RESULT_WRITER + "\n" + code, f";touch {marker}"),
        )

        result = self.port().verify(task(spec), self.repository, promotion())

        self.assertTrue(result.accepted, result)
        self.assertEqual(f";touch {marker}", (self.repository / "argv.txt").read_text())
        self.assertFalse(marker.exists())

    def test_accepts_ordinary_command_without_result_writer(self) -> None:
        spec = ordinary_command(
            "import sys\n"
            "from pathlib import Path\n"
            "Path('ordinary.txt').write_text('passed\\n', encoding='utf-8')\n"
            "print('ordinary stdout')\n"
            "print('ordinary stderr', file=sys.stderr)\n"
        )

        result = self.port().verify(task(spec), self.repository, promotion())

        self.assertTrue(result.accepted, result)
        self.assertIsNone(result.reason_code)
        self.assertEqual(1, len(result.evidence))
        self.assertEqual("passed\n", (self.repository / "ordinary.txt").read_text())

    def test_nonzero_exit_fails_closed_with_check_failed(self) -> None:
        spec = command("raise SystemExit(17)\n")

        result = self.port().verify(task(spec), self.repository, promotion())

        self.assertFalse(result.accepted)
        self.assertEqual(RuntimeReasonCode.CHECK_FAILED, result.reason_code)
        self.assertEqual((), result.evidence)

    def test_timeout_fails_closed_with_timeout_reason(self) -> None:
        spec = command("import time\ntime.sleep(30)\n", timeout=1)

        result = self.port().verify(task(spec), self.repository, promotion())

        self.assertFalse(result.accepted)
        self.assertEqual(RuntimeReasonCode.EXTERNAL_TIMEOUT, result.reason_code)
        self.assertEqual((), result.evidence)

    def test_invalid_result_channel_output_fails_closed(self) -> None:
        spec = command("emit_result(b'not-json')\n")

        result = self.port().verify(task(spec), self.repository, promotion())

        self.assertFalse(result.accepted)
        self.assertEqual(RuntimeReasonCode.EVIDENCE_INVALID, result.reason_code)
        self.assertEqual((), result.evidence)

    def test_executable_identity_mismatch_fails_before_launch(self) -> None:
        marker = self.repository / "must-not-exist"
        spec = replace(
            command(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                "emit_result(b'{\"ok\":true}')\n"
            ),
            executable_identity_digest=digest("f"),
        )

        result = self.port().verify(task(spec), self.repository, promotion())

        self.assertFalse(result.accepted)
        self.assertEqual(RuntimeReasonCode.PROCESS_START_FAILED, result.reason_code)
        self.assertFalse(marker.exists())

    def test_task_promotion_identity_mismatch_fails_closed(self) -> None:
        spec = command("emit_result(b'{\"ok\":true}')\n")

        result = self.port().verify(
            task(spec, task_id="TASK-001"),
            self.repository,
            promotion("TASK-002"),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(RuntimeReasonCode.INVARIANT_VIOLATION, result.reason_code)
        self.assertEqual((), result.evidence)

    def test_missing_acceptance_identity_fails_closed(self) -> None:
        spec = command("emit_result(b'{\"ok\":true}')\n")
        port = ProcessAcceptancePort(
            executable_profiles={"python": sys.executable},
            clock=lambda: "2026-08-19T00:00:20Z",
        )

        result = port.verify(task(spec), self.repository, promotion())

        self.assertFalse(result.accepted)
        self.assertEqual(RuntimeReasonCode.INVARIANT_VIOLATION, result.reason_code)


if __name__ == "__main__":
    unittest.main()
