#!/usr/bin/env python3
"""Run the official Trellis integration cells and seal their exact results."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.ci_test_suite import (
    EvidenceTextTestResult,
    discover_test_ids,
    test_id_digest,
)
from wish_builder.compatibility import load_bundled_trellis_compatibility


SCHEMA_VERSION = 1
EXPECTED_PLATFORMS = ("ubuntu-latest", "windows-latest")
EXPECTED_NODE_TEST_COUNT = 24
EXPECTED_PYTHON_TEST_COUNT = 11
NODE_TEST_FILES = (
    "tests/node/trellis-core-bridge.test.mjs",
    "tests/node/trellis-cli-pin.test.mjs",
    "tests/node/trellis-graph-snapshot.test.mjs",
    "tests/node/trellis-lifecycle-bridge.test.mjs",
    "tests/node/trellis-projection-bridge.test.mjs",
)
PYTHON_TEST_MODULES = (
    "tests.adapters.test_trellis_graph_snapshot",
    "tests.adapters.test_trellis_lifecycle",
    "tests.adapters.test_trellis_projection",
)
PYTHON_TEST_SUPPORT_FILES = ("tests/e2e/support.py",)
INTEGRATION_POLICY_FILES = (
    ".github/workflows/ci.yml",
    "scripts/ci_test_suite.py",
    "scripts/ci_trellis_integration.py",
    "scripts/ci_evidence_packet.py",
    "tests/packaging/test_ci_evidence_packet.py",
    "tests/packaging/test_ci_trellis_integration.py",
    "tests/packaging/test_ci_workflow.py",
)
_REVISION_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_TAP_COUNTERS = ("tests", "pass", "fail", "cancelled", "skipped", "todo")


class TrellisIntegrationError(RuntimeError):
    """The official integration run cannot prove a passing result."""


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _require_revision(value: object) -> str:
    if type(value) is not str or _REVISION_RE.fullmatch(value) is None:
        raise TrellisIntegrationError(
            "revision must be a lowercase 40- or 64-character commit id"
        )
    return value


def _require_platform(value: object) -> str:
    if value not in EXPECTED_PLATFORMS:
        raise TrellisIntegrationError("platform is not an official integration cell")
    assert isinstance(value, str)
    return value


def _python_test_path(module: str) -> str:
    return module.replace(".", "/") + ".py"


def integration_source_paths(
    repository_root: Path = REPOSITORY_ROOT,
) -> tuple[str, ...]:
    """Return the complete runtime, test, and evidence-policy source inventory."""

    package_root = repository_root / "wish_builder"
    if (
        not package_root.is_dir()
        or package_root.is_symlink()
    ):
        raise TrellisIntegrationError(
            "integration runtime source is unavailable: wish_builder"
        )
    files = {
        *NODE_TEST_FILES,
        *(_python_test_path(item) for item in PYTHON_TEST_MODULES),
        *PYTHON_TEST_SUPPORT_FILES,
        *INTEGRATION_POLICY_FILES,
    }
    for path in package_root.rglob("*"):
        relative = path.relative_to(repository_root)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise TrellisIntegrationError(
                "integration runtime source must not be a symlink: "
                f"{relative.as_posix()}"
            )
        if path.is_file():
            files.add(relative.as_posix())
    return tuple(sorted(files))


def integration_source_digest(
    repository_root: Path = REPOSITORY_ROOT,
) -> str:
    """Hash all runtime, test, and evidence-policy sources represented by the cell."""

    inventory: dict[str, str] = {}
    for relative in integration_source_paths(repository_root):
        path = repository_root / relative
        if not path.is_file() or path.is_symlink():
            raise TrellisIntegrationError(
                f"integration source is unavailable: {relative}"
            )
        inventory[relative] = _sha256_file(path)
    return _sha256_bytes(canonical_json_bytes(inventory))


def discover_python_test_ids() -> tuple[str, ...]:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(PYTHON_TEST_MODULES)
    if loader.errors:
        raise TrellisIntegrationError("official Python integration discovery failed")
    test_ids = discover_test_ids(suite)
    if len(test_ids) != len(set(test_ids)):
        raise TrellisIntegrationError("official Python integration test ids are duplicated")
    if len(test_ids) != EXPECTED_PYTHON_TEST_COUNT:
        raise TrellisIntegrationError(
            "official Python integration test count changed without evidence-policy review"
        )
    return test_ids


def _single_tap_counter(text: str, name: str) -> int:
    values = re.findall(rf"^# {re.escape(name)} ([0-9]+)$", text, flags=re.MULTILINE)
    if len(values) != 1:
        raise TrellisIntegrationError(f"Node TAP output has no unique {name} counter")
    return int(values[0])


def parse_node_tap(raw: bytes) -> dict[str, int]:
    try:
        text = raw.decode("utf-8", errors="strict").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise TrellisIntegrationError("Node TAP output is not strict UTF-8") from exc
    counters = {name: _single_tap_counter(text, name) for name in _TAP_COUNTERS}
    plans = re.findall(r"^1\.\.([0-9]+)$", text, flags=re.MULTILINE)
    if len(plans) != 1 or int(plans[0]) != counters["tests"]:
        raise TrellisIntegrationError("Node TAP plan does not match its test counter")
    if counters != {
        "cancelled": 0,
        "fail": 0,
        "pass": EXPECTED_NODE_TEST_COUNT,
        "skipped": 0,
        "tests": EXPECTED_NODE_TEST_COUNT,
        "todo": 0,
    }:
        raise TrellisIntegrationError("official Node integration did not pass exactly")
    return counters


def _run_node(node: str, *, timeout_seconds: float) -> dict[str, object]:
    environment = os.environ.copy()
    environment["WISH_BUILDER_TEST_PYTHON"] = sys.executable
    try:
        completed = subprocess.run(
            [
                node,
                "--test",
                "--test-reporter=tap",
                *NODE_TEST_FILES,
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TrellisIntegrationError("official Node integration could not complete") from exc
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    if completed.returncode != 0:
        raise TrellisIntegrationError("official Node integration returned a failure")
    counters = parse_node_tap(completed.stdout)
    return {
        "cancelled": counters["cancelled"],
        "failed": counters["fail"],
        "passed": counters["pass"],
        "skipped": counters["skipped"],
        "test_files": list(NODE_TEST_FILES),
        "tests_run": counters["tests"],
        "todo": counters["todo"],
    }


def _run_python() -> dict[str, object]:
    expected_ids = discover_python_test_ids()
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(PYTHON_TEST_MODULES)
    if loader.errors:
        raise TrellisIntegrationError("official Python integration discovery failed")
    runner = unittest.TextTestRunner(
        stream=sys.stderr,
        verbosity=2,
        resultclass=EvidenceTextTestResult,
    )
    result = runner.run(suite)
    executed_ids = tuple(sorted(result.executed_test_ids))
    if executed_ids != expected_ids:
        raise TrellisIntegrationError("official Python integration execution set drifted")
    if (
        not result.wasSuccessful()
        or result.testsRun != EXPECTED_PYTHON_TEST_COUNT
        or result.failures
        or result.errors
        or result.skipped
    ):
        raise TrellisIntegrationError("official Python integration did not pass exactly")
    return {
        "errors": len(result.errors),
        "failures": len(result.failures),
        "skipped": len(result.skipped),
        "test_ids_digest": test_id_digest(expected_ids),
        "test_modules": list(PYTHON_TEST_MODULES),
        "tests_run": result.testsRun,
    }


def _package_pins() -> tuple[str, list[dict[str, object]]]:
    compatibility = load_bundled_trellis_compatibility()
    packages = [
        {
            "name": package.name,
            "npm_integrity": package.integrity,
            "npm_shasum": package.shasum,
            "sha256": package.sha256,
            "version": package.version,
        }
        for package in compatibility.packages
    ]
    return compatibility.compatibility_digest, packages


def build_summary(
    *,
    revision: str,
    platform: str,
    node: Mapping[str, object],
    python: Mapping[str, object],
) -> dict[str, object]:
    candidate = _require_revision(revision)
    selected_platform = _require_platform(platform)
    compatibility_digest, packages = _package_pins()
    summary: dict[str, object] = {
        "compatibility_digest": compatibility_digest,
        "github_sha": candidate,
        "node": dict(node),
        "packages": packages,
        "platform": selected_platform,
        "python": dict(python),
        "revision": candidate,
        "schema_version": SCHEMA_VERSION,
        "source_digest": integration_source_digest(),
        "status": "passed",
        "trellis_version": "0.6.15",
    }
    summary["summary_digest"] = _sha256_bytes(canonical_json_bytes(summary))
    return summary


def _write_atomic(path: Path, value: bytes) -> None:
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
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--platform", required=True, choices=EXPECTED_PLATFORMS)
    parser.add_argument("--node", default="node")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if not math.isfinite(arguments.timeout_seconds) or arguments.timeout_seconds <= 0:
            raise TrellisIntegrationError("timeout must be finite and positive")
        node = _run_node(arguments.node, timeout_seconds=arguments.timeout_seconds)
        python = _run_python()
        summary = build_summary(
            revision=arguments.revision,
            platform=arguments.platform,
            node=node,
            python=python,
        )
        exit_code = 0
    except Exception as exc:
        summary = {
            "errors": [str(exc) or type(exc).__name__],
            "github_sha": arguments.revision,
            "platform": arguments.platform,
            "revision": arguments.revision,
            "schema_version": SCHEMA_VERSION,
            "status": "failed",
        }
        summary["summary_digest"] = _sha256_bytes(canonical_json_bytes(summary))
        exit_code = 1
    encoded = canonical_json_bytes(summary)
    try:
        _write_atomic(arguments.output, encoded)
    except OSError as exc:
        print(f"cannot write Trellis integration evidence: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(encoded)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
