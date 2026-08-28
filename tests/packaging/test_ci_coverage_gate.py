from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.ci_coverage_gate import CoverageInputError, evaluate_report, main


def coverage_file(
    covered: int,
    total: int,
    *,
    covered_lines: int | None = None,
    num_statements: int | None = None,
) -> dict[str, object]:
    line_covered = covered if covered_lines is None else covered_lines
    line_total = total if num_statements is None else num_statements
    return {
        "summary": {
            "covered_branches": covered,
            "covered_lines": line_covered,
            "missing_branches": total - covered,
            "missing_lines": line_total - line_covered,
            "num_branches": total,
            "num_statements": line_total,
        }
    }


def report(files: dict[str, dict[str, object]]) -> dict[str, object]:
    return {"meta": {"branch_coverage": True}, "files": files}


class CoverageGateTests(unittest.TestCase):
    expected = (
        "wish_builder/contracts/models.py",
        "wish_builder/kernel/dag.py",
        "wish_builder/services/journal.py",
        "wish_builder/adapters/fake/effects.py",
        "wish_builder/adapters/git_worktree.py",
        "wish_builder/adapters/storage/filesystem.py",
        "wish_builder/adapters/fakes.py",
        "wish_builder/processes/runner.py",
        "wish_builder/cli/wishctl.py",
        "wish_builder/__main__.py",
    )

    def passing_files(self) -> dict[str, dict[str, object]]:
        return {
            "wish_builder/contracts/models.py": coverage_file(95, 100),
            "wish_builder/kernel/dag.py": coverage_file(95, 100),
            "wish_builder/services/journal.py": coverage_file(90, 100),
            "wish_builder/adapters/fake/effects.py": coverage_file(85, 100),
            "wish_builder/adapters/git_worktree.py": coverage_file(85, 100),
            "wish_builder/adapters/storage/filesystem.py": coverage_file(85, 100),
            "wish_builder/adapters/fakes.py": coverage_file(0, 0),
            "wish_builder/processes/runner.py": coverage_file(85, 100),
            "wish_builder/cli/wishctl.py": coverage_file(85, 100),
            "wish_builder/__main__.py": coverage_file(0, 0),
            "wish_builder/presentation/trace.py": coverage_file(0, 10),
        }

    def test_groups_each_file_and_passes_exact_floors_deterministically(self) -> None:
        files = self.passing_files()
        first = evaluate_report(report(files), self.expected)
        reversed_files = dict(reversed(tuple(files.items())))
        second = evaluate_report(report(reversed_files), reversed(self.expected))

        self.assertEqual(first, second)
        self.assertEqual("pass", first["status"])
        self.assertEqual(
            [
                "contracts_kernel",
                "services",
                "adapters_processes_cli",
            ],
            [group["group"] for group in first["groups"]],
        )
        self.assertEqual(
            ["95.000000", "90.000000", "85.000000"],
            [group["branch_coverage_percent"] for group in first["groups"]],
        )
        self.assertEqual(
            sorted(self.expected), [item["path"] for item in first["files"]]
        )
        external_fakes = next(
            item
            for item in first["files"]
            if item["path"] == "wish_builder/adapters/fakes.py"
        )
        self.assertIsNone(external_fakes["branch_coverage_percent"])

    def test_production_adapter_branches_contribute_to_the_aggregate_floor(self) -> None:
        files = self.passing_files()
        files["wish_builder/adapters/git_worktree.py"] = coverage_file(84, 100)

        result = evaluate_report(report(files), self.expected)

        adapters = next(
            group
            for group in result["groups"]
            if group["group"] == "adapters_processes_cli"
        )
        git_adapter = next(
            item
            for item in result["files"]
            if item["path"] == "wish_builder/adapters/git_worktree.py"
        )
        self.assertEqual("fail", result["status"])
        self.assertEqual("84.800000", adapters["branch_coverage_percent"])
        self.assertEqual(1, adapters["required_additional_covered_branches"])
        self.assertEqual("adapters_processes_cli", git_adapter["group"])

    def test_branch_floor_cannot_be_masked_by_complete_line_coverage(self) -> None:
        files = self.passing_files()
        files["wish_builder/services/journal.py"] = coverage_file(
            80,
            100,
            covered_lines=100,
            num_statements=100,
        )
        result = evaluate_report(report(files), self.expected)

        services = next(
            group for group in result["groups"] if group["group"] == "services"
        )
        self.assertEqual("fail", result["status"])
        self.assertEqual("100.000000", services["line_coverage_percent"])
        self.assertEqual("80.000000", services["branch_coverage_percent"])
        self.assertEqual("90.000000", services["combined_coverage_percent"])
        self.assertEqual(10, services["required_additional_covered_branches"])

    def test_below_floor_reports_exact_aggregate_shortfall_and_exit_one(self) -> None:
        files = self.passing_files()
        files["wish_builder/services/journal.py"] = coverage_file(88, 100)
        result = evaluate_report(report(files), self.expected)

        self.assertEqual("fail", result["status"])
        services = next(
            group for group in result["groups"] if group["group"] == "services"
        )
        self.assertEqual("88.000000", services["branch_coverage_percent"])
        self.assertEqual(2, services["required_additional_covered_branches"])
        self.assertEqual("2.000000", services["percentage_point_shortfall"])
        self.assertEqual("fail", services["status"])

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "wish_builder"
            for path in self.expected:
                source = source_root / Path(path).relative_to("wish_builder")
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("pass\n", encoding="utf-8")
            report_path = root / "coverage.json"
            report_path.write_text(json.dumps(report(files)), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        str(report_path),
                        "--source-root",
                        str(source_root),
                    ]
                )
            self.assertEqual(1, exit_code)
            cli_result = json.loads(stdout.getvalue())
            self.assertEqual("fail", cli_result["status"])
            self.assertEqual(result["groups"], cli_result["groups"])
            self.assertEqual(
                [
                    "wish_builder/adapters/git_worktree.py",
                    "wish_builder/adapters/storage/filesystem.py",
                ],
                cli_result["safety_files"],
            )

    def test_missing_and_stale_governed_files_fail_closed(self) -> None:
        files = self.passing_files()
        del files["wish_builder/kernel/dag.py"]
        files["wish_builder/kernel/removed.py"] = coverage_file(1, 1)
        result = evaluate_report(report(files), self.expected)

        self.assertEqual("error", result["status"])
        self.assertEqual(
            ["missing_file_data", "unexpected_file_data"],
            [error["code"] for error in result["errors"]],
        )

    def test_requires_branch_report_and_consistent_counts(self) -> None:
        files = self.passing_files()
        with self.assertRaisesRegex(CoverageInputError, "branch_coverage"):
            evaluate_report(
                {"meta": {"branch_coverage": False}, "files": files},
                self.expected,
            )

        files["wish_builder/services/journal.py"] = {
            "summary": {
                "covered_branches": 90,
                "covered_lines": 90,
                "missing_branches": 9,
                "missing_lines": 10,
                "num_branches": 100,
                "num_statements": 100,
            }
        }
        with self.assertRaisesRegex(CoverageInputError, "must equal"):
            evaluate_report(report(files), self.expected)

        files = self.passing_files()
        summary = files["wish_builder/kernel/dag.py"]["summary"]
        assert isinstance(summary, dict)
        del summary["covered_lines"]
        with self.assertRaisesRegex(CoverageInputError, "covered_lines"):
            evaluate_report(report(files), self.expected)

    def test_missing_report_returns_deterministic_fail_closed_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "wish_builder"
            source_root.mkdir()
            report_path = root / "absent.json"

            outputs: list[str] = []
            for _ in range(2):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(
                        [
                            str(report_path),
                            "--source-root",
                            str(source_root),
                        ]
                    )
                self.assertEqual(2, exit_code)
                outputs.append(stdout.getvalue())

            self.assertEqual(outputs[0], outputs[1])
            result = json.loads(outputs[0])
            self.assertEqual("error", result["status"])
            self.assertEqual("report_read_failed", result["errors"][0]["code"])

    def test_group_with_empty_denominator_fails_closed(self) -> None:
        files = self.passing_files()
        files["wish_builder/services/journal.py"] = coverage_file(0, 0)
        result = evaluate_report(report(files), self.expected)

        self.assertEqual("error", result["status"])
        self.assertIn(
            {
                "code": "group_empty_denominator",
                "group": "services",
                "message": "coverage group has no measurable branches",
            },
            result["errors"],
        )

    def test_designated_safety_files_must_be_classified_present_and_nonempty(
        self,
    ) -> None:
        files = self.passing_files()
        result = evaluate_report(
            report(files),
            self.expected,
            (
                "wish_builder/presentation/trace.py",
                "wish_builder/kernel/missing.py",
                "wish_builder/adapters/fakes.py",
            ),
        )

        self.assertEqual("error", result["status"])
        self.assertEqual(
            [
                "safety_file_empty_denominator",
                "safety_file_not_in_source",
                "unclassified_safety_file",
            ],
            [error["code"] for error in result["errors"]],
        )

    def test_backslash_paths_are_normalized_before_classification(self) -> None:
        files = {
            path.replace("/", "\\"): value
            for path, value in self.passing_files().items()
        }
        result = evaluate_report(report(files), self.expected)

        self.assertEqual("pass", result["status"])
        self.assertTrue(all("\\" not in item["path"] for item in result["files"]))


if __name__ == "__main__":
    unittest.main()
