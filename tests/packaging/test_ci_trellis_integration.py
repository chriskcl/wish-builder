from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.ci_test_suite import test_id_digest
from scripts.ci_trellis_integration import (
    EXPECTED_NODE_TEST_COUNT,
    EXPECTED_PYTHON_TEST_COUNT,
    NODE_TEST_FILES,
    PYTHON_TEST_MODULES,
    TrellisIntegrationError,
    build_summary,
    canonical_json_bytes,
    discover_python_test_ids,
    integration_source_digest,
    integration_source_paths,
    main,
    parse_node_tap,
)


def passing_node_result() -> dict[str, object]:
    return {
        "cancelled": 0,
        "failed": 0,
        "passed": EXPECTED_NODE_TEST_COUNT,
        "skipped": 0,
        "test_files": list(NODE_TEST_FILES),
        "tests_run": EXPECTED_NODE_TEST_COUNT,
        "todo": 0,
    }


def passing_python_result() -> dict[str, object]:
    test_ids = discover_python_test_ids()
    return {
        "errors": 0,
        "failures": 0,
        "skipped": 0,
        "test_ids_digest": test_id_digest(test_ids),
        "test_modules": list(PYTHON_TEST_MODULES),
        "tests_run": EXPECTED_PYTHON_TEST_COUNT,
    }


class TrellisIntegrationEvidenceTests(unittest.TestCase):
    def assert_failed_summary(
        self, output: Path, *, error_contains: str
    ) -> dict[str, object]:
        summary = json.loads(output.read_text(encoding="ascii"))
        digest_input = dict(summary)
        declared_digest = digest_input.pop("summary_digest")
        self.assertEqual("failed", summary["status"])
        self.assertIn(error_contains, summary["errors"][0])
        self.assertEqual(
            "sha256:" + hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest(),
            declared_digest,
        )
        return summary

    def test_fixed_integration_set_is_unique_complete_and_content_addressed(self) -> None:
        test_ids = discover_python_test_ids()

        self.assertEqual(EXPECTED_PYTHON_TEST_COUNT, len(test_ids))
        self.assertEqual(len(test_ids), len(set(test_ids)))
        self.assertEqual(3, len(PYTHON_TEST_MODULES))
        self.assertIn(
            "tests.adapters.test_trellis_lifecycle",
            PYTHON_TEST_MODULES,
        )
        self.assertEqual(5, len(NODE_TEST_FILES))
        self.assertIn(
            "tests/node/trellis-lifecycle-bridge.test.mjs",
            NODE_TEST_FILES,
        )
        self.assertRegex(integration_source_digest(), r"^sha256:[0-9a-f]{64}$")

    def test_source_digest_binds_runtime_bridges_pins_and_compatibility(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        source_paths = integration_source_paths(repository_root)
        required = {
            ".github/workflows/ci.yml",
            "scripts/ci_trellis_integration.py",
            "scripts/ci_evidence_packet.py",
            "wish_builder/adapters/trellis/graph_snapshot.py",
            "wish_builder/adapters/trellis/lifecycle.py",
            "wish_builder/adapters/trellis/projection.py",
            "wish_builder/bridges/trellis_core/graph-snapshot.mjs",
            "wish_builder/bridges/trellis_core/lifecycle.mjs",
            "wish_builder/bridges/trellis_core/projection.mjs",
            "wish_builder/bridges/trellis_core/cli-pins.json",
            "wish_builder/bridges/trellis_core/pins.json",
            "wish_builder/compatibility/trellis-0.6.15.json",
            "tests/packaging/test_ci_evidence_packet.py",
            "tests/node/trellis-lifecycle-bridge.test.mjs",
            "tests/adapters/test_trellis_lifecycle.py",
        }
        self.assertTrue(required.issubset(source_paths))

        with TemporaryDirectory() as raw_root:
            copied_root = Path(raw_root)
            for relative in source_paths:
                destination = copied_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(repository_root / relative, destination)
            baseline = integration_source_digest(copied_root)

            for relative in sorted(required - {"scripts/ci_trellis_integration.py"}):
                with self.subTest(relative=relative):
                    target = copied_root / relative
                    original = target.read_bytes()
                    target.write_bytes(original + b"\n")
                    self.assertNotEqual(baseline, integration_source_digest(copied_root))
                    target.write_bytes(original)

    def test_node_tap_requires_one_exact_skip_free_plan(self) -> None:
        passing = (
            "TAP version 13\n"
            f"1..{EXPECTED_NODE_TEST_COUNT}\n"
            f"# tests {EXPECTED_NODE_TEST_COUNT}\n"
            "# suites 0\n"
            f"# pass {EXPECTED_NODE_TEST_COUNT}\n"
            "# fail 0\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
        ).encode("ascii")
        self.assertEqual(EXPECTED_NODE_TEST_COUNT, parse_node_tap(passing)["pass"])

        for replacement in (
            passing.replace(b"# skipped 0", b"# skipped 1"),
            passing.replace(
                f"1..{EXPECTED_NODE_TEST_COUNT}".encode(),
                f"1..{EXPECTED_NODE_TEST_COUNT - 1}".encode(),
            ),
            passing + f"# pass {EXPECTED_NODE_TEST_COUNT}\n".encode(),
            b"\xff",
        ):
            with self.subTest(raw=replacement[-24:]):
                with self.assertRaises(TrellisIntegrationError):
                    parse_node_tap(replacement)

    def test_summary_binds_revision_platform_sources_and_official_pins(self) -> None:
        summary = build_summary(
            revision="a" * 40,
            platform="windows-latest",
            node=passing_node_result(),
            python=passing_python_result(),
        )
        digest_input = dict(summary)
        declared = digest_input.pop("summary_digest")

        self.assertEqual("passed", summary["status"])
        self.assertEqual("0.6.15", summary["trellis_version"])
        self.assertEqual(
            ["@mindfoldhq/trellis", "@mindfoldhq/trellis-core"],
            [item["name"] for item in summary["packages"]],
        )
        self.assertEqual(
            "sha256:" + hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest(),
            declared,
        )

    def test_cli_writes_failure_evidence_instead_of_a_false_pass(self) -> None:
        with TemporaryDirectory() as raw_root:
            output = Path(raw_root) / "summary.json"
            with (
                patch(
                    "scripts.ci_trellis_integration._run_node",
                    return_value=passing_node_result(),
                ),
                patch(
                    "scripts.ci_trellis_integration._run_python",
                    return_value=passing_python_result(),
                ),
            ):
                exit_code = main(
                    [
                        "--revision",
                        "HEAD",
                        "--platform",
                        "ubuntu-latest",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assert_failed_summary(output, error_contains="revision must be")

    def test_cli_rejects_non_finite_timeouts_before_running_node(self) -> None:
        with TemporaryDirectory() as raw_root:
            for timeout in ("nan", "inf", "-inf"):
                with self.subTest(timeout=timeout):
                    output = Path(raw_root) / f"summary-{timeout}.json"
                    with patch(
                        "scripts.ci_trellis_integration._run_node"
                    ) as run_node:
                        exit_code = main(
                            [
                                "--revision",
                                "a" * 40,
                                "--platform",
                                "ubuntu-latest",
                                f"--timeout-seconds={timeout}",
                                "--output",
                                str(output),
                            ]
                        )

                    self.assertEqual(1, exit_code)
                    run_node.assert_not_called()
                    self.assert_failed_summary(
                        output, error_contains="timeout must be finite and positive"
                    )

    def test_cli_writes_failure_evidence_for_compatibility_loader_error(self) -> None:
        with TemporaryDirectory() as raw_root:
            output = Path(raw_root) / "summary.json"
            with (
                patch(
                    "scripts.ci_trellis_integration._run_node",
                    return_value=passing_node_result(),
                ),
                patch(
                    "scripts.ci_trellis_integration._run_python",
                    return_value=passing_python_result(),
                ),
                patch(
                    "scripts.ci_trellis_integration.load_bundled_trellis_compatibility",
                    side_effect=RuntimeError("compatibility loader failed"),
                ),
            ):
                exit_code = main(
                    [
                        "--revision",
                        "a" * 40,
                        "--platform",
                        "windows-latest",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assert_failed_summary(
                output, error_contains="compatibility loader failed"
            )

    def test_cli_writes_failure_evidence_for_subprocess_error(self) -> None:
        with TemporaryDirectory() as raw_root:
            output = Path(raw_root) / "summary.json"
            with patch(
                "scripts.ci_trellis_integration.subprocess.run",
                side_effect=subprocess.SubprocessError("Node runner failed"),
            ):
                exit_code = main(
                    [
                        "--revision",
                        "a" * 40,
                        "--platform",
                        "ubuntu-latest",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assert_failed_summary(output, error_contains="Node runner failed")

    def test_cli_does_not_catch_base_exceptions(self) -> None:
        with TemporaryDirectory() as raw_root:
            for name, failure in (
                ("keyboard-interrupt", KeyboardInterrupt()),
                ("system-exit", SystemExit(7)),
            ):
                with self.subTest(failure=name):
                    output = Path(raw_root) / f"summary-{name}.json"
                    with patch(
                        "scripts.ci_trellis_integration._run_node",
                        side_effect=failure,
                    ):
                        with self.assertRaises(type(failure)):
                            main(
                                [
                                    "--revision",
                                    "a" * 40,
                                    "--platform",
                                    "ubuntu-latest",
                                    "--output",
                                    str(output),
                                ]
                            )
                    self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
