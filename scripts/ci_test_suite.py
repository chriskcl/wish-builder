#!/usr/bin/env python3
"""Run repository unittest packages with performance tests isolated on demand."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = REPOSITORY_ROOT / "tests"
_PACKAGE_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
_REVISION_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def discover_suite(
    test_root: Path = TEST_ROOT,
    *,
    exclude_packages: frozenset[str] = frozenset(),
    only_package: str | None = None,
) -> unittest.TestSuite:
    """Discover immediate test packages without importing excluded packages."""
    test_root = test_root.resolve(strict=True)
    repository_root = test_root.parent
    if only_package is not None and exclude_packages:
        raise ValueError("only_package and exclude_packages are mutually exclusive")
    requested = set(exclude_packages)
    if only_package is not None:
        requested.add(only_package)
    for name in requested:
        if not _PACKAGE_RE.fullmatch(name):
            raise ValueError(f"invalid test package name: {name!r}")
        package = test_root / name
        if not package.is_dir() or not (package / "__init__.py").is_file():
            raise ValueError(f"unknown test package: {name}")

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    packages = tuple(
        path
        for path in sorted(test_root.iterdir(), key=lambda item: item.name)
        if path.is_dir()
        and (path / "__init__.py").is_file()
        and (
            path.name == only_package
            if only_package is not None
            else path.name not in exclude_packages
        )
    )
    for package in packages:
        suite.addTests(
            loader.discover(
                start_dir=str(package),
                pattern="test_*.py",
                top_level_dir=str(repository_root),
            )
        )

    if only_package is None:
        for test_file in sorted(test_root.glob("test_*.py")):
            suite.addTests(
                loader.loadTestsFromName(f"tests.{test_file.stem}")
            )
    if loader.errors:
        raise RuntimeError("test discovery failed:\n" + "\n".join(loader.errors))
    return suite


def discover_test_ids(suite: unittest.TestSuite) -> tuple[str, ...]:
    """Return the exact canonical test IDs represented by a discovered suite."""
    values: list[str] = []

    def visit(item: unittest.TestSuite | unittest.case.TestCase) -> None:
        if isinstance(item, unittest.TestSuite):
            for child in item:
                visit(child)
            return
        test_id = item.id()
        if type(test_id) is not str or not test_id:
            raise ValueError("discovered test has no stable test id")
        values.append(test_id)

    visit(suite)
    if not values:
        raise ValueError("discovered test ids are empty")
    return tuple(sorted(values))


def test_id_digest(test_ids: tuple[str, ...]) -> str:
    """Content-address the complete unittest ID multiset, including duplicates."""
    if (
        not test_ids
        or any(type(item) is not str or not item for item in test_ids)
    ):
        raise ValueError("test ids are empty or invalid")
    encoded = json.dumps(
        sorted(test_ids),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class EvidenceTextTestResult(unittest.TextTestResult):
    """Text result that records exactly which discovered tests actually started."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.executed_test_ids: list[str] = []

    def startTest(self, test: unittest.case.TestCase) -> None:  # noqa: N802
        test_id = test.id()
        if type(test_id) is not str or not test_id:
            raise ValueError("executed test has no stable test id")
        self.executed_test_ids.append(test_id)
        super().startTest(test)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--exclude-package",
        action="append",
        default=[],
        help="immediate tests package to exclude; repeatable",
    )
    mode.add_argument(
        "--only-package",
        help="run only one immediate tests package and no top-level tests",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--summary-output",
        type=Path,
        help="write a canonical machine-readable test-cell summary",
    )
    parser.add_argument(
        "--revision",
        help="candidate Git revision; defaults to GITHUB_SHA when writing a summary",
    )
    parser.add_argument("--cell-id", help="matrix cell identifier for the summary")
    parser.add_argument("--platform", help="runner platform label for the summary")
    parser.add_argument("--python-version", help="matrix Python version for the summary")
    return parser


def _require_revision(value: str | None) -> str:
    revision = value if value is not None else os.environ.get("GITHUB_SHA")
    if type(revision) is not str or _REVISION_RE.fullmatch(revision) is None:
        raise ValueError("summary revision must be a lowercase 40- or 64-character commit id")
    return revision


def _write_summary(path: Path, summary: dict[str, object]) -> None:
    encoded = (
        json.dumps(
            summary,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _summary_for_result(
    result: unittest.TestResult,
    *,
    discovered_test_ids: tuple[str, ...],
    revision: str,
    cell_id: str | None,
    platform: str | None,
    python_version: str | None,
) -> dict[str, object]:
    selected_platform = platform or os.environ.get("CI_PLATFORM")
    selected_version = python_version or os.environ.get("CI_PYTHON_VERSION")
    selected_cell = cell_id or os.environ.get("CI_CELL_ID")
    actual_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if (
        type(selected_platform) is not str
        or not selected_platform
        or "\x00" in selected_platform
    ):
        raise ValueError("summary platform is required")
    if selected_version != actual_version:
        raise ValueError(
            f"summary Python version {selected_version!r} does not match "
            f"interpreter {actual_version}"
        )
    expected_cell = f"{selected_platform}-py{selected_version}"
    if selected_cell != expected_cell:
        raise ValueError("summary cell id does not match its platform and Python version")
    executed_test_ids = tuple(getattr(result, "executed_test_ids", ()))
    if not executed_test_ids or len(executed_test_ids) != result.testsRun:
        raise ValueError("executed test ids do not match the unittest result")
    discovered_digest = test_id_digest(discovered_test_ids)
    executed_digest = test_id_digest(executed_test_ids)
    skipped_tests = sorted(
        (
            {"reason": reason, "test_id": test.id()}
            for test, reason in getattr(result, "skipped", ())
        ),
        key=lambda item: (str(item["test_id"]), str(item["reason"])),
    )
    if any(
        type(item["test_id"]) is not str
        or not item["test_id"]
        or type(item["reason"]) is not str
        or not item["reason"]
        for item in skipped_tests
    ) or len({str(item["test_id"]) for item in skipped_tests}) != len(skipped_tests):
        raise ValueError("skipped test ids and reasons must be nonempty and unique")
    return {
        "cell_id": selected_cell,
        "discovered_test_count": len(discovered_test_ids),
        "discovered_test_ids_digest": discovered_digest,
        "errors": len(result.errors),
        "executed_test_count": len(executed_test_ids),
        "executed_test_ids_digest": executed_digest,
        "failures": len(result.failures),
        "github_sha": revision,
        "platform": selected_platform,
        "python_version": selected_version,
        "revision": revision,
        "schema_version": 2,
        "skipped": len(skipped_tests),
        "skipped_tests": skipped_tests,
        "status": "passed" if result.wasSuccessful() else "failed",
        "tests_run": result.testsRun,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    summary_revision: str | None = None
    if arguments.summary_output is not None:
        try:
            summary_revision = _require_revision(arguments.revision)
        except ValueError as exc:
            print(f"test suite summary configuration error: {exc}", file=sys.stderr)
            return 2
    try:
        suite = discover_suite(
            exclude_packages=frozenset(arguments.exclude_package),
            only_package=arguments.only_package,
        )
        discovered_test_ids = discover_test_ids(suite)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"test suite configuration error: {exc}", file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(
        verbosity=1 if arguments.quiet else 2,
        resultclass=EvidenceTextTestResult,
    ).run(suite)
    if arguments.summary_output is not None:
        assert summary_revision is not None
        try:
            summary = _summary_for_result(
                result,
                discovered_test_ids=discovered_test_ids,
                revision=summary_revision,
                cell_id=arguments.cell_id,
                platform=arguments.platform,
                python_version=arguments.python_version,
            )
            _write_summary(arguments.summary_output, summary)
        except (OSError, ValueError) as exc:
            print(f"cannot write test suite summary: {exc}", file=sys.stderr)
            return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
