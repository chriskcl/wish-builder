from __future__ import annotations

import os
import unittest
from unittest import mock

from wish_builder.adapters.process_identity import (
    LeaseOwnerProcessState,
    capture_process_start_id,
)


class ProcessIdentityCaptureTests(unittest.TestCase):
    def test_capture_uses_the_current_pid_by_default(self) -> None:
        helper = (
            "wish_builder.adapters.process_identity._windows_process_start_id"
            if os.name == "nt"
            else "wish_builder.adapters.process_identity._linux_process_start_id"
        )
        with mock.patch(helper, return_value="process-start-001") as capture:
            self.assertEqual("process-start-001", capture_process_start_id())
        capture.assert_called_once_with(os.getpid())

    def test_capture_rejects_non_positive_or_boolean_pids(self) -> None:
        for value in (True, 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                capture_process_start_id(value)  # type: ignore[arg-type]

    def test_capture_dispatches_to_windows_identity(self) -> None:
        with (
            mock.patch("wish_builder.adapters.process_identity.os.name", "nt"),
            mock.patch(
                "wish_builder.adapters.process_identity._windows_process_start_id",
                return_value="windows-filetime:123",
            ) as capture,
        ):
            self.assertEqual(
                "windows-filetime:123",
                capture_process_start_id(4321),
            )
        capture.assert_called_once_with(4321)

    def test_capture_fails_closed_on_an_unsupported_host(self) -> None:
        with (
            mock.patch("wish_builder.adapters.process_identity.os.name", "unsupported"),
            self.assertRaisesRegex(OSError, "unsupported"),
        ):
            capture_process_start_id(4321)

    def test_state_enum_retains_exact_dead_and_pid_reused_values(self) -> None:
        self.assertEqual("dead", LeaseOwnerProcessState.DEAD.value)
        self.assertEqual("pid_reused", LeaseOwnerProcessState.PID_REUSED.value)


if __name__ == "__main__":
    unittest.main()
