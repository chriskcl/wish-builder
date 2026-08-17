from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_HELP = REPOSITORY_ROOT / "tests" / "golden" / "wishctl-help.txt"


class LauncherTests(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_compatibility_launcher_help_matches_golden(self) -> None:
        result = self._run("scripts/wishctl.py", "--help")
        expected = GOLDEN_HELP.read_text(encoding="utf-8")
        self.assertEqual(0, result.returncode)
        self.assertEqual(expected, result.stdout.replace("\r\n", "\n"))
        self.assertEqual("", result.stderr)

    def test_module_help_matches_compatibility_launcher(self) -> None:
        launcher = self._run("scripts/wishctl.py", "--help")
        module = self._run("-m", "wish_builder", "--help")
        self.assertEqual(0, module.returncode)
        self.assertEqual(launcher.stdout, module.stdout)
        self.assertEqual("", module.stderr)

    def test_missing_command_uses_argparse_exit_code(self) -> None:
        result = self._run("scripts/wishctl.py")
        self.assertEqual(2, result.returncode)
        self.assertIn("the following arguments are required: command", result.stderr)


if __name__ == "__main__":
    unittest.main()
