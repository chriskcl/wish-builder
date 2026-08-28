#!/usr/bin/env python3
"""Enforce active-M1 branch-enabled coverage floors from coverage.py JSON."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.ci_safety_registry import ACTIVE_M1_SAFETY_PATHS

DEFAULT_SOURCE_ROOT = REPOSITORY_ROOT / "wish_builder"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CoverageGroup:
    name: str
    threshold_percent: int


GROUPS = (
    CoverageGroup("contracts_kernel", 95),
    CoverageGroup("services", 90),
    CoverageGroup("adapters_processes_cli", 85),
)


class CoverageInputError(ValueError):
    """Raised when the coverage report cannot prove a safe gate result."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FileCoverage:
    path: str
    group: str
    covered_lines: int
    num_statements: int
    covered_branches: int
    num_branches: int


def _canonical_package_path(value: str) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    parts = [
        part for part in value.replace("\\", "/").split("/") if part not in {"", "."}
    ]
    if ".." in parts:
        return None
    package_positions = [
        position for position, part in enumerate(parts) if part == "wish_builder"
    ]
    if len(package_positions) != 1:
        return None
    return "/".join(parts[package_positions[0] :])


def classify_path(path: str) -> str | None:
    """Return the active-M1 coverage group for a canonical source path."""
    canonical = _canonical_package_path(path)
    if canonical is None:
        return None
    if canonical.startswith(("wish_builder/contracts/", "wish_builder/kernel/")):
        return "contracts_kernel"
    if canonical.startswith("wish_builder/services/"):
        return "services"
    if canonical.startswith(("wish_builder/processes/", "wish_builder/cli/")):
        return "adapters_processes_cli"
    if canonical == "wish_builder/__main__.py":
        return "adapters_processes_cli"
    if canonical.startswith("wish_builder/adapters/"):
        return "adapters_processes_cli"
    return None


def discover_grouped_sources(source_root: Path) -> tuple[str, ...]:
    """Discover every current Python source governed by a coverage floor."""
    if not source_root.is_dir():
        raise CoverageInputError(
            "source_root_missing",
            f"source root is not a directory: {source_root.as_posix()}",
        )
    paths: list[str] = []
    for source in source_root.rglob("*.py"):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root).as_posix()
        canonical = f"wish_builder/{relative}"
        if classify_path(canonical) is not None:
            paths.append(canonical)
    return tuple(sorted(paths))


def discover_safety_sources(source_root: Path) -> tuple[str, ...]:
    """Discover branch-bearing source files governed by changed-safety."""
    return tuple(
        path
        for path in discover_grouped_sources(source_root)
        if ACTIVE_M1_SAFETY_PATHS.governs(path)
        and PurePosixPath(path).name != "__init__.py"
    )


def _required_int(summary: Mapping[str, Any], field: str, path: str) -> int:
    value = summary.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CoverageInputError(
            "invalid_file_summary",
            f"{path}: {field} must be a non-negative integer",
        )
    return value


def _parse_file_coverage(path: str, group: str, payload: Any) -> FileCoverage:
    if not isinstance(payload, Mapping):
        raise CoverageInputError(
            "invalid_file_data", f"{path}: file coverage must be an object"
        )
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        raise CoverageInputError(
            "missing_file_summary", f"{path}: summary is missing or invalid"
        )
    num_statements = _required_int(summary, "num_statements", path)
    covered_lines = _required_int(summary, "covered_lines", path)
    missing_lines = _required_int(summary, "missing_lines", path)
    if covered_lines + missing_lines != num_statements:
        raise CoverageInputError(
            "inconsistent_line_counts",
            f"{path}: covered_lines + missing_lines must equal num_statements",
        )
    num_branches = _required_int(summary, "num_branches", path)
    covered_branches = _required_int(summary, "covered_branches", path)
    missing_branches = _required_int(summary, "missing_branches", path)
    if covered_branches + missing_branches != num_branches:
        raise CoverageInputError(
            "inconsistent_branch_counts",
            (f"{path}: covered_branches + missing_branches must equal num_branches"),
        )
    return FileCoverage(
        path,
        group,
        covered_lines,
        num_statements,
        covered_branches,
        num_branches,
    )


def _format_percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        raise ValueError("coverage percentage requires a positive denominator")
    scale = 1_000_000
    scaled = (numerator * 100 * scale * 2 + denominator) // (2 * denominator)
    return f"{scaled // scale}.{scaled % scale:06d}"


def _error(code: str, message: str, **details: str) -> dict[str, str]:
    return {"code": code, **details, "message": message}


def evaluate_report(
    report: Any,
    expected_paths: Iterable[str],
    safety_files: Iterable[str] = (),
) -> dict[str, Any]:
    """Validate and evaluate one coverage.py JSON report deterministically."""
    if not isinstance(report, Mapping):
        raise CoverageInputError("invalid_report", "coverage report must be an object")
    meta = report.get("meta")
    if not isinstance(meta, Mapping) or meta.get("branch_coverage") is not True:
        raise CoverageInputError(
            "branch_coverage_required",
            "coverage report must have meta.branch_coverage=true",
        )
    raw_files = report.get("files")
    if not isinstance(raw_files, Mapping):
        raise CoverageInputError(
            "invalid_files", "coverage report files must be an object"
        )

    expected = tuple(sorted(set(expected_paths)))
    invalid_expected = [path for path in expected if classify_path(path) is None]
    if invalid_expected:
        raise CoverageInputError(
            "unclassified_expected_source",
            f"expected source cannot be classified: {invalid_expected[0]}",
        )

    grouped: dict[str, FileCoverage] = {}
    for raw_path in sorted(raw_files, key=str):
        canonical = _canonical_package_path(raw_path)
        if canonical is None:
            continue
        group = classify_path(canonical)
        if group is None:
            continue
        if canonical in grouped:
            raise CoverageInputError(
                "duplicate_file_data",
                f"multiple report entries normalize to: {canonical}",
            )
        grouped[canonical] = _parse_file_coverage(canonical, group, raw_files[raw_path])

    errors: list[dict[str, str]] = []
    expected_set = set(expected)
    for path in sorted(expected_set - grouped.keys()):
        errors.append(
            _error(
                "missing_file_data",
                "current governed source is missing from the coverage report",
                path=path,
            )
        )
    for path in sorted(grouped.keys() - expected_set):
        errors.append(
            _error(
                "unexpected_file_data",
                "coverage report contains a governed source absent from the source tree",
                path=path,
            )
        )

    normalized_safety_files: set[str] = set()
    for raw_path in safety_files:
        canonical = _canonical_package_path(raw_path)
        if canonical is None or classify_path(canonical) is None:
            errors.append(
                _error(
                    "unclassified_safety_file",
                    "designated safety file is outside the governed coverage groups",
                    path=str(raw_path).replace("\\", "/"),
                )
            )
            continue
        normalized_safety_files.add(canonical)

    for path in sorted(normalized_safety_files):
        if path not in expected_set:
            errors.append(
                _error(
                    "safety_file_not_in_source",
                    "designated safety file is absent from the current source tree",
                    path=path,
                )
            )
            continue
        coverage = grouped.get(path)
        if coverage is None:
            errors.append(
                _error(
                    "safety_file_missing_data",
                    "designated safety file is missing from the coverage report",
                    path=path,
                )
            )
        elif coverage.num_branches == 0:
            errors.append(
                _error(
                    "safety_file_empty_denominator",
                    "designated safety file has no measurable branches",
                    path=path,
                )
            )

    files_output: list[dict[str, Any]] = []
    for path in sorted(grouped):
        coverage = grouped[path]
        covered_opportunities = coverage.covered_lines + coverage.covered_branches
        num_opportunities = coverage.num_statements + coverage.num_branches
        files_output.append(
            {
                "covered_branches": coverage.covered_branches,
                "branch_coverage_percent": (
                    _format_percent(coverage.covered_branches, coverage.num_branches)
                    if coverage.num_branches
                    else None
                ),
                "covered_lines": coverage.covered_lines,
                "covered_opportunities": covered_opportunities,
                "combined_coverage_percent": (
                    _format_percent(covered_opportunities, num_opportunities)
                    if num_opportunities
                    else None
                ),
                "group": coverage.group,
                "line_coverage_percent": (
                    _format_percent(coverage.covered_lines, coverage.num_statements)
                    if coverage.num_statements
                    else None
                ),
                "num_branches": coverage.num_branches,
                "num_opportunities": num_opportunities,
                "num_statements": coverage.num_statements,
                "path": coverage.path,
            }
        )

    groups_output: list[dict[str, Any]] = []
    floor_failed = False
    for group in GROUPS:
        members = [item for item in grouped.values() if item.group == group.name]
        covered_lines = sum(item.covered_lines for item in members)
        num_statements = sum(item.num_statements for item in members)
        covered_branches = sum(item.covered_branches for item in members)
        num_branches = sum(item.num_branches for item in members)
        covered = covered_lines + covered_branches
        total = num_statements + num_branches
        if total == 0 or num_branches == 0:
            errors.append(
                _error(
                    "group_empty_denominator",
                    "coverage group has no measurable branches",
                    group=group.name,
                )
            )
            groups_output.append(
                {
                    "branch_coverage_percent": None,
                    "covered_branches": covered_branches,
                    "covered_lines": covered_lines,
                    "covered_opportunities": covered,
                    "combined_coverage_percent": None,
                    "file_count": len(members),
                    "group": group.name,
                    "line_coverage_percent": (
                        _format_percent(covered_lines, num_statements)
                        if num_statements
                        else None
                    ),
                    "num_branches": num_branches,
                    "num_opportunities": total,
                    "num_statements": num_statements,
                    "percentage_point_shortfall": None,
                    "required_additional_covered_branches": None,
                    "status": "error",
                    "threshold_percent": f"{group.threshold_percent}.000000",
                }
            )
            continue
        required = (group.threshold_percent * num_branches + 100 - 1) // 100
        shortfall = max(0, required - covered_branches)
        passed = covered_branches * 100 >= group.threshold_percent * num_branches
        floor_failed = floor_failed or not passed
        actual_percent = _format_percent(covered_branches, num_branches)
        combined_percent = _format_percent(covered, total)
        actual_scaled = int(actual_percent.replace(".", ""))
        threshold_scaled = group.threshold_percent * 1_000_000
        shortfall_percent = max(0, threshold_scaled - actual_scaled)
        groups_output.append(
            {
                "branch_coverage_percent": actual_percent,
                "covered_branches": covered_branches,
                "covered_lines": covered_lines,
                "covered_opportunities": covered,
                "combined_coverage_percent": combined_percent,
                "file_count": len(members),
                "group": group.name,
                "line_coverage_percent": (
                    _format_percent(covered_lines, num_statements)
                    if num_statements
                    else None
                ),
                "num_branches": num_branches,
                "num_opportunities": total,
                "num_statements": num_statements,
                "percentage_point_shortfall": (
                    f"{shortfall_percent // 1_000_000}."
                    f"{shortfall_percent % 1_000_000:06d}"
                ),
                "required_additional_covered_branches": shortfall,
                "status": "pass" if passed else "fail",
                "threshold_percent": f"{group.threshold_percent}.000000",
            }
        )

    errors.sort(
        key=lambda item: (
            item["code"],
            item.get("group", ""),
            item.get("path", ""),
            item["message"],
        )
    )
    status = "error" if errors else "fail" if floor_failed else "pass"
    return {
        "errors": errors,
        "files": files_output,
        "groups": groups_output,
        "metric": "covered_branches / num_branches",
        "safety_files": sorted(normalized_safety_files),
        "schema_version": SCHEMA_VERSION,
        "status": status,
    }


def _error_result(error: CoverageInputError) -> dict[str, Any]:
    return {
        "errors": [_error(error.code, error.message)],
        "files": [],
        "groups": [],
        "metric": "covered_branches / num_branches",
        "safety_files": [],
        "schema_version": SCHEMA_VERSION,
        "status": "error",
    }


def _write_result(result: Mapping[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enforce active-M1 layer branch-coverage floors from coverage.py JSON."
        )
    )
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help="current wish_builder package root (default: repository package)",
    )
    parser.add_argument(
        "--safety-file",
        action="append",
        default=[],
        help="governed safety file that must have non-empty branch data; repeatable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        raw = arguments.coverage_json.read_text(encoding="utf-8")
        report = json.loads(raw)
        expected = discover_grouped_sources(arguments.source_root)
        safety_files = tuple(
            sorted(
                {
                    *discover_safety_sources(arguments.source_root),
                    *arguments.safety_file,
                }
            )
        )
        result = evaluate_report(report, expected, safety_files)
    except OSError:
        result = _error_result(
            CoverageInputError(
                "report_read_failed",
                f"cannot read coverage report: {arguments.coverage_json.as_posix()}",
            )
        )
    except json.JSONDecodeError as error:
        result = _error_result(
            CoverageInputError(
                "invalid_json",
                f"coverage report is not valid JSON at line {error.lineno} column {error.colno}",
            )
        )
    except CoverageInputError as error:
        result = _error_result(error)
    _write_result(result)
    if result["status"] == "pass":
        return 0
    if result["status"] == "fail":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
