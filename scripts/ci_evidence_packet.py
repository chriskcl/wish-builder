#!/usr/bin/env python3
"""Assemble one fail-closed active-M1 packet from current-workflow CI artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.ci_coverage_gate import (
    DEFAULT_SOURCE_ROOT,
    GROUPS as COVERAGE_GROUPS,
    CoverageInputError,
    discover_grouped_sources,
    discover_safety_sources,
    evaluate_report,
)
from scripts.ci_distribution_evidence import (
    DistributionEvidenceError,
    validate_distribution_artifacts,
)
from scripts.ci_mutation_gate import (
    DEFAULT_MUTATIONS,
    MINIMUM_MUTATION_SCORE,
    REPORT_SCHEMA_VERSION as MUTATION_SCHEMA_VERSION,
)
from scripts.ci_safety_evidence import (
    ACTIVE_M1_SAFETY_PATHS,
    ChangedLinesInputError,
    collect_changed_lines,
    evaluate_safety_evidence,
)
from scripts.ci_test_suite import discover_suite, discover_test_ids, test_id_digest
from scripts.ci_trellis_integration import (
    EXPECTED_NODE_TEST_COUNT,
    EXPECTED_PYTHON_TEST_COUNT,
    NODE_TEST_FILES,
    PYTHON_TEST_MODULES,
    SCHEMA_VERSION as TRELLIS_INTEGRATION_SCHEMA_VERSION,
    discover_python_test_ids,
    integration_source_digest,
)
from tests.performance.evidence import (
    evaluate_gate as evaluate_performance_gate,
    read_evidence as read_performance_evidence,
    validate_evidence as validate_performance_evidence,
)
from wish_builder.compatibility import load_bundled_trellis_compatibility


SCHEMA_VERSION = 4
EXPECTED_PLATFORMS = ("ubuntu-latest", "windows-latest")
EXPECTED_PYTHON_VERSIONS = ("3.11", "3.12", "3.13")
REQUIRED_JOBS = (
    "coverage",
    "mutation",
    "performance-evidence",
    "python",
    "python-distribution",
    "python-distribution-install",
    "safety-evidence",
    "trellis-official-integration",
)
RAW_EVIDENCE_FILES = {
    "coverage": frozenset({"coverage.json", "coverage-gate.json"}),
    "mutation": frozenset({"mutation-report.json"}),
    "performance": frozenset(
        {
            "performance-evidence.json",
            "performance-gate.json",
            "performance-gate.raw.json",
        }
    ),
    "safety": frozenset({"changed-lines.json", "safety-evidence.json"}),
}
_REVISION_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

_REQUIRED_PLATFORM_SKIPS = {
    "ubuntu-latest": frozenset(
        {
            (
                "tests.adapters.test_git_identity.GitIdentityTests."
                "test_control_root_reparse_replacement_is_detected",
                "junction test is Windows-specific",
            ),
            (
                "tests.adapters.test_git_identity.GitIdentityTests."
                "test_workspace_junction_replacement_is_detected_without_user_edits",
                "junction test is Windows-specific",
            ),
            (
                "tests.processes.test_containment.ContainmentTests."
                "test_windows_job_creation_failures_are_unavailable",
                "Windows API failure mapping",
            ),
            (
                "tests.processes.test_containment.ContainmentTests."
                "test_windows_resume_thread_failures_are_unknown",
                "Windows API failure mapping",
            ),
            (
                "tests.processes.test_containment.ContainmentTests."
                "test_windows_session_api_failures_are_unknown",
                "Windows API failure mapping",
            ),
            (
                "tests.processes.test_containment.ContainmentTests."
                "test_windows_backend_uses_suspended_launch",
                "Windows-specific suspended launch",
            ),
            (
                "tests.processes.test_runner.ProcessRunnerTests."
                "test_windows_job_contains_a_breakaway_request",
                "Windows Job breakaway test",
            ),
        }
    ),
    "windows-latest": frozenset(
        {
            (
                "tests.processes.test_containment.ContainmentTests."
                "test_posix_backend_uses_a_new_session",
                "POSIX-specific process group",
            )
        }
    ),
}
_TRELLIS_RUNTIME_SKIPS = frozenset(
    {
        (
            "tests.adapters.test_trellis_graph_snapshot."
            "TrellisGraphSnapshotAdapterIntegrationTests."
            "test_incomplete_parent_membership_fails_closed",
            "official Trellis 0.6.15 fixture is unavailable",
        ),
        (
            "tests.adapters.test_trellis_graph_snapshot."
            "TrellisGraphSnapshotAdapterIntegrationTests."
            "test_official_records_flow_through_port_and_manifest_import",
            "official Trellis 0.6.15 fixture is unavailable",
        ),
        (
            "tests.adapters.test_trellis_projection."
            "OfficialTrellisLifecycleIntegrationTests."
            "test_official_records_run_through_graph_e2e_recovery_and_projection",
            "official Trellis 0.6.15 fixture is unavailable",
        ),
        (
            "tests.adapters.test_trellis_projection."
            "TrellisProjectionAdapterIntegrationTests."
            "test_existing_projection_writer_lock_fails_closed_without_writing",
            "pinned local Trellis runtime is unavailable",
        ),
        (
            "tests.adapters.test_trellis_projection."
            "TrellisProjectionAdapterIntegrationTests."
            "test_authoritative_projection_updates_task_and_replay_is_idempotent",
            "pinned local Trellis runtime is unavailable",
        ),
        (
            "tests.adapters.test_trellis_projection."
            "TrellisProjectionAdapterIntegrationTests."
            "test_authoritative_projection_revision_conflict_does_not_overwrite",
            "pinned local Trellis runtime is unavailable",
        ),
        (
            "tests.adapters.test_trellis_projection."
            "TrellisProjectionAdapterIntegrationTests."
            "test_authoritative_projection_manual_status_conflict_does_not_overwrite",
            "pinned local Trellis runtime is unavailable",
        )
    }
)
_PLATFORM_SKIP_ALLOWLIST = {
    platform: skips | _TRELLIS_RUNTIME_SKIPS
    for platform, skips in _REQUIRED_PLATFORM_SKIPS.items()
}
_OPTIONAL_SKIP_REASON_PATTERNS = {
    "ubuntu-latest": {
        (
            "tests.adapters.test_git_identity.GitIdentityTests."
            "test_sha256_repository_identity_is_supported_when_git_supports_it",
            re.compile(r"installed Git does not support SHA-256 repositories\Z"),
        ),
    },
    "windows-latest": {
        (
            "tests.adapters.test_git_identity.GitIdentityTests."
            "test_sha256_repository_identity_is_supported_when_git_supports_it",
            re.compile(r"installed Git does not support SHA-256 repositories\Z"),
        ),
        (
            "tests.adapters.test_git_identity.GitIdentityTests."
            "test_untracked_symlink_hashes_the_link_not_its_target",
            re.compile(r"symlink creation is unavailable: [^\r\n\x00]+\Z"),
        ),
    },
}


def _skip_is_allowed(platform: str, test_id: str, reason: str) -> bool:
    if (test_id, reason) in _PLATFORM_SKIP_ALLOWLIST[platform]:
        return True
    return any(
        test_id == allowed_id and pattern.fullmatch(reason) is not None
        for allowed_id, pattern in _OPTIONAL_SKIP_REASON_PATTERNS[platform]
    )


class EvidencePacketError(ValueError):
    """An input cannot prove that every required gate passed for one revision."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidencePacketError(
                "duplicate_json_key", f"JSON object contains duplicate key: {key}"
            )
        result[key] = value
    return result


def _decode_json_bytes(value: bytes, *, label: str) -> object:
    def reject_constant(constant: str) -> object:
        raise EvidencePacketError(
            "invalid_json", f"{label} contains non-standard number {constant}"
        )

    try:
        text = value.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=reject_constant,
        )
    except EvidencePacketError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvidencePacketError("invalid_json", f"{label} is not strict UTF-8 JSON") from exc


def _read_json(path: Path, *, label: str) -> object:
    try:
        return _decode_json_bytes(path.read_bytes(), label=label)
    except OSError as exc:
        raise EvidencePacketError("input_read_failed", f"cannot read {label}") from exc


def _require_object(value: object, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise EvidencePacketError("malformed_input", f"{label} must be an object")
    return value


def _require_exact_fields(
    value: Mapping[str, object], fields: set[str], *, label: str
) -> None:
    if set(value) != fields:
        raise EvidencePacketError(
            "malformed_input", f"{label} fields do not match schema"
        )


def _require_revision(value: object, *, label: str = "revision") -> str:
    if type(value) is not str or _REVISION_RE.fullmatch(value) is None:
        raise EvidencePacketError(
            "revision_invalid",
            f"{label} must be a lowercase 40- or 64-character commit id",
        )
    return value


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise EvidencePacketError("digest_invalid", f"{label} is not a SHA-256 digest")
    return value


def _require_nonnegative_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise EvidencePacketError("malformed_input", f"{label} must be nonnegative")
    return value


def _regular_root(path: Path) -> Path:
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise EvidencePacketError("evidence_root_missing", "evidence root is missing") from exc
    if path.is_symlink() or not root.is_dir():
        raise EvidencePacketError(
            "evidence_root_invalid", "evidence root must be a non-symlink directory"
        )
    return root


def _is_regular_under(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        relative = path.relative_to(root)
    except (OSError, ValueError):
        return False
    if not resolved.is_file() or path.is_symlink() or not resolved.is_relative_to(root):
        return False
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            return False
    return True


def _discover(root: Path, filename: str, *, expected: int) -> tuple[Path, ...]:
    matches = tuple(
        sorted(
            (path for path in root.rglob(filename) if _is_regular_under(path, root)),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    )
    if len(matches) != expected:
        raise EvidencePacketError(
            "evidence_count_invalid",
            f"expected {expected} {filename} artifact(s), found {len(matches)}",
        )
    return matches


def _validate_test_summary(
    value: object,
    candidate: str,
    raw_path: Path,
    *,
    expected_test_ids: tuple[str, ...],
    expected_test_ids_digest: str,
) -> dict[str, object]:
    summary = _require_object(value, label="test summary")
    _require_exact_fields(
        summary,
        {
            "cell_id",
            "discovered_test_count",
            "discovered_test_ids_digest",
            "errors",
            "executed_test_count",
            "executed_test_ids_digest",
            "failures",
            "github_sha",
            "platform",
            "python_version",
            "revision",
            "schema_version",
            "skipped",
            "skipped_tests",
            "status",
            "tests_run",
        },
        label="test summary",
    )
    if summary["schema_version"] != 2 or summary["status"] != "passed":
        raise EvidencePacketError("test_cell_not_passed", "test summary did not pass")
    revision = _require_revision(summary["revision"], label="test summary revision")
    if revision != candidate or summary["github_sha"] != candidate:
        raise EvidencePacketError(
            "revision_mismatch", "test summary is not bound to the candidate revision"
        )
    platform = summary["platform"]
    version = summary["python_version"]
    if platform not in EXPECTED_PLATFORMS or version not in EXPECTED_PYTHON_VERSIONS:
        raise EvidencePacketError("test_cell_unknown", "test summary identifies an unknown cell")
    expected_id = f"{platform}-py{version}"
    if summary["cell_id"] != expected_id:
        raise EvidencePacketError("test_cell_invalid", "test summary cell id is inconsistent")
    tests_run = _require_nonnegative_int(summary["tests_run"], label="tests_run")
    discovered_count = _require_nonnegative_int(
        summary["discovered_test_count"], label="discovered_test_count"
    )
    executed_count = _require_nonnegative_int(
        summary["executed_test_count"], label="executed_test_count"
    )
    discovered_digest = _require_digest(
        summary["discovered_test_ids_digest"], label="discovered test ids digest"
    )
    executed_digest = _require_digest(
        summary["executed_test_ids_digest"], label="executed test ids digest"
    )
    failures = _require_nonnegative_int(summary["failures"], label="failures")
    errors = _require_nonnegative_int(summary["errors"], label="errors")
    skipped = _require_nonnegative_int(summary["skipped"], label="skipped")
    expected_count = len(expected_test_ids)
    if (
        tests_run != expected_count
        or discovered_count != expected_count
        or executed_count != expected_count
        or discovered_digest != expected_test_ids_digest
        or executed_digest != expected_test_ids_digest
        or failures != 0
        or errors != 0
        or skipped > tests_run
    ):
        raise EvidencePacketError(
            "test_cell_counts_invalid", "test summary counters cannot prove a passing run"
        )
    skipped_tests = summary["skipped_tests"]
    if type(skipped_tests) is not list or skipped != len(skipped_tests):
        raise EvidencePacketError(
            "test_cell_skips_invalid", "test summary skip details are incomplete"
        )
    normalized_skips: list[dict[str, str]] = []
    skip_pairs: list[tuple[str, str]] = []
    expected_ids = set(expected_test_ids)
    for item in skipped_tests:
        if type(item) is not dict or set(item) != {"reason", "test_id"}:
            raise EvidencePacketError(
                "test_cell_skips_invalid", "test summary skip fields are invalid"
            )
        test_id = item["test_id"]
        reason = item["reason"]
        if (
            type(test_id) is not str
            or test_id not in expected_ids
            or type(reason) is not str
            or not reason
        ):
            raise EvidencePacketError(
                "test_cell_skips_invalid", "test summary skip identity is invalid"
            )
        skip_pairs.append((test_id, reason))
        normalized_skips.append({"reason": reason, "test_id": test_id})
    observed_skips = frozenset(skip_pairs)
    if len(observed_skips) != len(skip_pairs):
        raise EvidencePacketError(
            "test_cell_skips_invalid", "test summary skip identities are duplicated"
        )
    required_skips = _REQUIRED_PLATFORM_SKIPS[str(platform)]
    if not required_skips.issubset(observed_skips) or any(
        not _skip_is_allowed(str(platform), test_id, reason)
        for test_id, reason in observed_skips
    ):
        raise EvidencePacketError(
            "test_cell_skip_not_allowed",
            "test summary contains missing or unapproved platform skips",
        )
    return {
        "cell_id": expected_id,
        "discovered_test_count": discovered_count,
        "discovered_test_ids_digest": discovered_digest,
        "errors": errors,
        "executed_test_count": executed_count,
        "executed_test_ids_digest": executed_digest,
        "failures": failures,
        "platform": platform,
        "python_version": version,
        "raw_sha256": _sha256_file(raw_path),
        "raw_size_bytes": raw_path.stat().st_size,
        "skipped": skipped,
        "skipped_tests": sorted(
            normalized_skips,
            key=lambda item: (item["test_id"], item["reason"]),
        ),
        "tests_run": tests_run,
    }


def _validate_python_matrix(root: Path, candidate: str) -> list[dict[str, object]]:
    try:
        expected_test_ids = discover_test_ids(
            discover_suite(exclude_packages=frozenset({"performance"}))
        )
        expected_test_ids_digest = test_id_digest(expected_test_ids)
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidencePacketError(
            "test_discovery_failed", "cannot independently discover the matrix suite"
        ) from exc
    expected_count = len(EXPECTED_PLATFORMS) * len(EXPECTED_PYTHON_VERSIONS)
    summaries = [
        _validate_test_summary(
            _read_json(path, label="test summary"),
            candidate,
            path,
            expected_test_ids=expected_test_ids,
            expected_test_ids_digest=expected_test_ids_digest,
        )
        for path in _discover(root, "ci-summary.json", expected=expected_count)
    ]
    ids = [str(item["cell_id"]) for item in summaries]
    expected_ids = {
        f"{platform}-py{version}"
        for platform in EXPECTED_PLATFORMS
        for version in EXPECTED_PYTHON_VERSIONS
    }
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise EvidencePacketError(
            "test_matrix_incomplete", "test matrix has duplicate or missing cells"
        )
    return sorted(summaries, key=lambda item: str(item["cell_id"]))


def _expected_trellis_packages() -> tuple[str, list[dict[str, object]]]:
    compatibility = load_bundled_trellis_compatibility()
    return compatibility.compatibility_digest, [
        {
            "name": package.name,
            "npm_integrity": package.integrity,
            "npm_shasum": package.shasum,
            "sha256": package.sha256,
            "version": package.version,
        }
        for package in compatibility.packages
    ]


def _validate_trellis_summary(
    value: object,
    candidate: str,
    raw_path: Path,
    *,
    expected_python_ids_digest: str,
    expected_source_digest: str,
) -> dict[str, object]:
    summary = _require_object(value, label="official Trellis integration summary")
    _require_exact_fields(
        summary,
        {
            "compatibility_digest",
            "github_sha",
            "node",
            "packages",
            "platform",
            "python",
            "revision",
            "schema_version",
            "source_digest",
            "status",
            "summary_digest",
            "trellis_version",
        },
        label="official Trellis integration summary",
    )
    if (
        summary["schema_version"] != TRELLIS_INTEGRATION_SCHEMA_VERSION
        or summary["status"] != "passed"
        or summary["trellis_version"] != "0.6.15"
    ):
        raise EvidencePacketError(
            "trellis_integration_not_passed",
            "official Trellis integration summary did not pass",
        )
    revision = _require_revision(summary["revision"], label="Trellis revision")
    if revision != candidate or summary["github_sha"] != candidate:
        raise EvidencePacketError(
            "revision_mismatch",
            "official Trellis integration is not bound to the candidate",
        )
    platform = summary["platform"]
    if platform not in EXPECTED_PLATFORMS:
        raise EvidencePacketError(
            "trellis_integration_platform_invalid",
            "official Trellis integration platform is unsupported",
        )
    expected_compatibility, expected_packages = _expected_trellis_packages()
    if (
        summary["compatibility_digest"] != expected_compatibility
        or summary["packages"] != expected_packages
    ):
        raise EvidencePacketError(
            "trellis_integration_pin_mismatch",
            "official Trellis package pins differ from repository compatibility",
        )
    if summary["source_digest"] != expected_source_digest:
        raise EvidencePacketError(
            "trellis_integration_source_mismatch",
            "official Trellis integration sources differ from the candidate",
        )

    node = _require_object(summary["node"], label="official Node result")
    expected_node = {
        "cancelled": 0,
        "failed": 0,
        "passed": EXPECTED_NODE_TEST_COUNT,
        "skipped": 0,
        "test_files": list(NODE_TEST_FILES),
        "tests_run": EXPECTED_NODE_TEST_COUNT,
        "todo": 0,
    }
    if node != expected_node:
        raise EvidencePacketError(
            "trellis_node_result_invalid",
            "official Node integration result is incomplete",
        )

    python = _require_object(summary["python"], label="official Python result")
    expected_python = {
        "errors": 0,
        "failures": 0,
        "skipped": 0,
        "test_ids_digest": expected_python_ids_digest,
        "test_modules": list(PYTHON_TEST_MODULES),
        "tests_run": EXPECTED_PYTHON_TEST_COUNT,
    }
    if python != expected_python:
        raise EvidencePacketError(
            "trellis_python_result_invalid",
            "official Python integration result is incomplete",
        )

    declared_digest = _require_digest(
        summary["summary_digest"], label="Trellis summary digest"
    )
    digest_input = dict(summary)
    digest_input.pop("summary_digest")
    expected_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(digest_input)
    ).hexdigest()
    if declared_digest != expected_digest:
        raise EvidencePacketError(
            "trellis_integration_digest_mismatch",
            "official Trellis integration digest is inconsistent",
        )
    return {
        "compatibility_digest": expected_compatibility,
        "node_tests_run": EXPECTED_NODE_TEST_COUNT,
        "platform": platform,
        "python_test_ids_digest": expected_python_ids_digest,
        "python_tests_run": EXPECTED_PYTHON_TEST_COUNT,
        "raw_sha256": _sha256_file(raw_path),
        "raw_size_bytes": raw_path.stat().st_size,
        "source_digest": expected_source_digest,
        "summary_digest": declared_digest,
    }


def _validate_trellis_matrix(root: Path, candidate: str) -> list[dict[str, object]]:
    try:
        expected_python_ids_digest = test_id_digest(discover_python_test_ids())
        expected_source_digest = integration_source_digest()
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidencePacketError(
            "trellis_integration_discovery_failed",
            "cannot independently reconstruct official Trellis integration inputs",
        ) from exc
    summaries = [
        _validate_trellis_summary(
            _read_json(path, label="official Trellis integration summary"),
            candidate,
            path,
            expected_python_ids_digest=expected_python_ids_digest,
            expected_source_digest=expected_source_digest,
        )
        for path in _discover(
            root,
            "trellis-integration-summary.json",
            expected=len(EXPECTED_PLATFORMS),
        )
    ]
    platforms = [str(item["platform"]) for item in summaries]
    if len(platforms) != len(set(platforms)) or set(platforms) != set(
        EXPECTED_PLATFORMS
    ):
        raise EvidencePacketError(
            "trellis_integration_matrix_incomplete",
            "official Trellis integration has duplicate or missing platforms",
        )
    return sorted(summaries, key=lambda item: str(item["platform"]))


def _validate_coverage(value: object, raw_coverage: object) -> dict[str, object]:
    report = _require_object(value, label="coverage gate")
    if report.get("schema_version") != 1 or report.get("status") != "pass":
        raise EvidencePacketError("coverage_not_passed", "coverage gate did not pass")
    if report.get("errors") != []:
        raise EvidencePacketError("coverage_malformed", "coverage gate contains errors")
    groups = report.get("groups")
    if type(groups) is not list:
        raise EvidencePacketError("coverage_malformed", "coverage groups are missing")
    names: list[str] = []
    for group in groups:
        if type(group) is not dict or group.get("status") != "pass":
            raise EvidencePacketError("coverage_not_passed", "a coverage group did not pass")
        name = group.get("group")
        if type(name) is not str:
            raise EvidencePacketError("coverage_malformed", "coverage group name is invalid")
        names.append(name)
    expected = {group.name for group in COVERAGE_GROUPS}
    if len(names) != len(set(names)) or set(names) != expected:
        raise EvidencePacketError(
            "coverage_malformed", "coverage groups are duplicate or incomplete"
        )

    expected_safety_files = discover_safety_sources(DEFAULT_SOURCE_ROOT)
    if report.get("safety_files") != list(expected_safety_files):
        raise EvidencePacketError(
            "coverage_registry_mismatch",
            "coverage safety files differ from the repository registry",
        )
    try:
        recomputed = evaluate_report(
            raw_coverage,
            discover_grouped_sources(DEFAULT_SOURCE_ROOT),
            expected_safety_files,
        )
    except CoverageInputError as exc:
        raise EvidencePacketError(
            "coverage_raw_invalid", f"raw coverage cannot be validated: {exc}"
        ) from exc
    if recomputed != report:
        raise EvidencePacketError(
            "coverage_gate_mismatch",
            "coverage gate is not the deterministic result of raw coverage",
        )
    return {"groups": sorted(names), "schema_version": 1, "status": "pass"}


_TEST_RUN_FIELDS = {
    "error_test_ids",
    "errors",
    "failed_test_ids",
    "failures",
    "infrastructure_error",
    "skipped",
    "successful",
    "tests_run",
}


def _validate_mutation_test_run(
    value: object,
    *,
    expected_test_ids: Sequence[str],
    successful: bool,
) -> None:
    run = _require_object(value, label="mutation test run")
    _require_exact_fields(run, _TEST_RUN_FIELDS, label="mutation test run")
    integer_fields = ("errors", "failures", "skipped", "tests_run")
    if any(type(run[field]) is not int or run[field] < 0 for field in integer_fields):
        raise EvidencePacketError("mutation_malformed", "mutation test counts are invalid")
    failed_ids = run["failed_test_ids"]
    error_ids = run["error_test_ids"]
    if (
        type(failed_ids) is not list
        or type(error_ids) is not list
        or any(type(item) is not str or not item for item in (*failed_ids, *error_ids))
        or len(failed_ids) != len(set(failed_ids))
        or len(error_ids) != len(set(error_ids))
    ):
        raise EvidencePacketError("mutation_malformed", "mutation test ids are invalid")
    if (
        run["successful"] is not successful
        or run["tests_run"] != len(expected_test_ids)
        or run["skipped"] != 0
        or run["infrastructure_error"] is not None
        or run["failures"] != len(failed_ids)
        or run["errors"] != len(error_ids)
    ):
        raise EvidencePacketError("mutation_malformed", "mutation test run is inconsistent")
    if successful:
        if run["failures"] != 0 or run["errors"] != 0:
            raise EvidencePacketError("mutation_malformed", "mutation baseline did not pass cleanly")
        return
    if run["failures"] <= 0 or run["errors"] != 0:
        raise EvidencePacketError(
            "mutation_malformed", "killed mutation lacks a direct assertion failure"
        )

    def belongs_to_declared_test(observed: str) -> bool:
        return any(
            observed == test_id or observed.startswith(test_id + " (")
            for test_id in expected_test_ids
        )

    if any(not belongs_to_declared_test(item) for item in failed_ids):
        raise EvidencePacketError(
            "mutation_registry_mismatch",
            "mutation failure came from an undeclared direct test",
        )
    if any(
        not any(
            observed == test_id or observed.startswith(test_id + " (")
            for observed in failed_ids
        )
        for test_id in expected_test_ids
    ):
        raise EvidencePacketError(
            "mutation_registry_mismatch",
            "a declared direct test did not kill the mutation",
        )


def _validate_mutation(value: object) -> dict[str, object]:
    report = _require_object(value, label="mutation gate")
    _require_exact_fields(
        report,
        {
            "baseline",
            "minimum_score",
            "mutation_count",
            "policy",
            "results",
            "schema_version",
            "status",
        },
        label="mutation gate",
    )
    if (
        report["schema_version"] != MUTATION_SCHEMA_VERSION
        or report["status"] != "passed"
    ):
        raise EvidencePacketError("mutation_not_passed", "mutation gate did not pass")
    if type(report["minimum_score"]) is not float or report["minimum_score"] != float(
        MINIMUM_MUTATION_SCORE
    ):
        raise EvidencePacketError("mutation_malformed", "mutation score threshold drifted")

    policy = _require_object(report["policy"], label="mutation policy")
    _require_exact_fields(
        policy,
        {
            "errors",
            "killed",
            "passed",
            "reasons",
            "score",
            "survived",
            "surviving_safety_mutations",
        },
        label="mutation policy",
    )
    results = report["results"]
    count = report["mutation_count"]
    if (
        type(results) is not list
        or type(count) is not int
        or count <= 0
        or len(results) != count
        or type(policy["errors"]) is not int
        or type(policy["killed"]) is not int
        or type(policy["survived"]) is not int
        or type(policy["score"]) is not float
        or type(policy["reasons"]) is not list
        or type(policy["surviving_safety_mutations"]) is not list
        or policy
        != {
            "errors": 0,
            "killed": count,
            "passed": True,
            "reasons": [],
            "score": 100.0,
            "survived": 0,
            "surviving_safety_mutations": [],
        }
    ):
        raise EvidencePacketError("mutation_malformed", "mutation evidence is incomplete")

    expected_by_id = {spec.mutation_id: spec for spec in DEFAULT_MUTATIONS}
    expected_baseline_tests = tuple(
        dict.fromkeys(test_id for spec in DEFAULT_MUTATIONS for test_id in spec.test_ids)
    )
    _validate_mutation_test_run(
        report["baseline"],
        expected_test_ids=expected_baseline_tests,
        successful=True,
    )
    ids: list[str] = []
    for result in results:
        if type(result) is not dict:
            raise EvidencePacketError("mutation_malformed", "mutation result is invalid")
        _require_exact_fields(
            result,
            {
                "invariant",
                "mutation_id",
                "safety_invariant",
                "source_path",
                "status",
                "test_ids",
                "test_run",
            },
            label="mutation result",
        )
        if result["status"] != "killed":
            raise EvidencePacketError("mutation_not_passed", "a mutation was not killed")
        mutation_id = result["mutation_id"]
        if type(mutation_id) is not str or not mutation_id:
            raise EvidencePacketError("mutation_malformed", "mutation id is invalid")
        spec = expected_by_id.get(mutation_id)
        if spec is None:
            raise EvidencePacketError(
                "mutation_registry_mismatch",
                "mutation evidence contains an unregistered mutation",
            )
        if (
            result.get("invariant") != spec.invariant
            or result.get("safety_invariant") is not spec.safety_invariant
            or result.get("source_path") != spec.source_path
            or result.get("test_ids") != list(spec.test_ids)
        ):
            raise EvidencePacketError(
                "mutation_registry_mismatch",
                "mutation evidence differs from the fixed registry",
            )
        _validate_mutation_test_run(
            result["test_run"],
            expected_test_ids=spec.test_ids,
            successful=False,
        )
        ids.append(mutation_id)
    if len(ids) != len(set(ids)):
        raise EvidencePacketError("mutation_malformed", "mutation ids are duplicated")
    if count != len(expected_by_id) or set(ids) != set(expected_by_id):
        raise EvidencePacketError(
            "mutation_registry_mismatch",
            "mutation evidence is missing fixed registry entries",
        )
    return {
        "mutation_count": count,
        "mutation_ids": sorted(ids),
        "schema_version": MUTATION_SCHEMA_VERSION,
        "status": "passed",
    }


def _validate_safety(
    value: object,
    recomputed_value: object,
    candidate: str,
    trusted_base_ref: str,
) -> dict[str, object]:
    report = _require_object(value, label="safety evidence")
    recomputed = _require_object(recomputed_value, label="recomputed safety evidence")
    if recomputed.get("status") != "pass" or recomputed.get("errors") != []:
        raise EvidencePacketError(
            "safety_not_passed", "raw safety inputs do not satisfy the safety policy"
        )
    if report != recomputed:
        raise EvidencePacketError(
            "safety_gate_mismatch",
            "safety evidence is not the deterministic result of raw inputs",
        )
    if report.get("schema_version") != 2 or report.get("status") != "pass":
        raise EvidencePacketError("safety_not_passed", "safety evidence did not pass")
    if report.get("errors") != []:
        raise EvidencePacketError("safety_malformed", "safety evidence contains errors")
    provenance = report.get("provenance")
    if type(provenance) is not dict:
        raise EvidencePacketError("safety_malformed", "safety provenance is missing")
    head = _require_revision(provenance.get("head"), label="safety provenance head")
    merge_base = _require_revision(
        provenance.get("merge_base"), label="safety provenance merge base"
    )
    base_ref = provenance.get("base_ref")
    if type(base_ref) is not str or not base_ref or "\x00" in base_ref:
        raise EvidencePacketError("safety_malformed", "safety base ref is missing")
    if base_ref != trusted_base_ref:
        raise EvidencePacketError(
            "safety_base_mismatch",
            "safety evidence does not use the trusted workflow comparison base",
        )
    if head != candidate:
        raise EvidencePacketError(
            "safety_revision_mismatch", "safety provenance head is not the candidate"
        )
    if merge_base == head:
        raise EvidencePacketError(
            "safety_comparison_empty", "safety merge base must differ from candidate head"
        )
    invariants = report.get("invariants")
    invariant_count = report.get("invariant_count")
    if (
        type(invariants) is not list
        or type(invariant_count) is not int
        or invariant_count <= 0
        or invariant_count != len(invariants)
    ):
        raise EvidencePacketError("safety_malformed", "safety invariant set is incomplete")
    mutation_ids: list[str] = []
    for invariant in invariants:
        if type(invariant) is not dict:
            raise EvidencePacketError("safety_malformed", "safety invariant is invalid")
        mutation_id = invariant.get("mutation_id")
        if type(mutation_id) is not str or not mutation_id:
            raise EvidencePacketError("safety_malformed", "safety mutation id is invalid")
        mutation_ids.append(mutation_id)
    if len(mutation_ids) != len(set(mutation_ids)):
        raise EvidencePacketError("safety_malformed", "safety mutation ids are duplicated")
    digest = _require_digest(report.get("evidence_digest"), label="safety evidence digest")
    changed_branches = report.get("changed_branches")
    changed_files = report.get("changed_files")
    changed_branch_count = report.get("changed_branch_count")
    if (
        type(changed_branches) is not list
        or type(changed_files) is not list
        or type(changed_branch_count) is not int
        or changed_branch_count != len(changed_branches)
    ):
        raise EvidencePacketError("safety_malformed", "changed safety evidence is invalid")
    digest_input = {
        "changed_branches": changed_branches,
        "changed_files": changed_files,
        "invariants": invariants,
        "provenance": provenance,
    }
    digest_bytes = json.dumps(
        digest_input,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if digest != "sha256:" + hashlib.sha256(digest_bytes).hexdigest():
        raise EvidencePacketError(
            "safety_digest_mismatch", "safety evidence digest is inconsistent"
        )
    return {
        "evidence_digest": digest,
        "invariant_count": invariant_count,
        "mutation_ids": sorted(mutation_ids),
        "provenance": {
            "base_ref": base_ref,
            "head": head,
            "merge_base": merge_base,
        },
        "schema_version": 2,
        "status": "pass",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise EvidencePacketError("evidence_read_failed", "cannot hash evidence file") from exc
    return "sha256:" + digest.hexdigest()


def record_raw_evidence(
    paths: Sequence[Path],
    *,
    kind: str,
    revision: str,
    cell_id: str | None = None,
) -> dict[str, object]:
    """Content-address the exact gate inputs emitted by one CI job."""
    candidate = _require_revision(revision)
    expected = RAW_EVIDENCE_FILES.get(kind)
    if expected is None:
        raise EvidencePacketError("raw_evidence_kind_invalid", "raw evidence kind is invalid")
    if kind == "performance":
        if cell_id not in EXPECTED_PLATFORMS:
            raise EvidencePacketError(
                "raw_evidence_cell_invalid", "performance raw evidence needs a platform cell"
            )
    elif cell_id is not None:
        raise EvidencePacketError(
            "raw_evidence_cell_invalid", "non-matrix raw evidence cannot have a cell id"
        )

    resolved: list[Path] = []
    names: list[str] = []
    for path in paths:
        try:
            current = path.resolve(strict=True)
        except OSError as exc:
            raise EvidencePacketError(
                "raw_evidence_file_missing", f"raw evidence file is missing: {path.name}"
            ) from exc
        if path.is_symlink() or not current.is_file():
            raise EvidencePacketError(
                "raw_evidence_file_invalid", "raw evidence must use regular files"
            )
        if path.name != PurePosixPath(path.name).name or "\\" in path.name:
            raise EvidencePacketError(
                "raw_evidence_path_invalid", "raw evidence filename is invalid"
            )
        resolved.append(current)
        names.append(path.name)
    if (
        len(resolved) != len(set(resolved))
        or len(names) != len(set(names))
        or set(names) != set(expected)
    ):
        raise EvidencePacketError(
            "raw_evidence_set_invalid", f"{kind} raw evidence set is duplicate or incomplete"
        )
    if len({path.parent for path in resolved}) != 1:
        raise EvidencePacketError(
            "raw_evidence_path_invalid", "raw evidence files must share one directory"
        )
    files = [
        {
            "path": path.name,
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(resolved, key=lambda item: item.name)
    ]
    if any(item["size_bytes"] == 0 for item in files):
        raise EvidencePacketError(
            "raw_evidence_file_invalid", "raw evidence files must be non-empty"
        )
    body: dict[str, object] = {
        "cell_id": cell_id,
        "file_count": len(files),
        "files": files,
        "github_sha": candidate,
        "kind": kind,
        "revision": candidate,
        "schema_version": 1,
        "status": "recorded",
    }
    body["inventory_digest"] = "sha256:" + hashlib.sha256(
        canonical_json_bytes(body)
    ).hexdigest()
    return body


def _validate_raw_inventory(
    value: object,
    manifest_path: Path,
    *,
    candidate: str,
    expected_kind: str,
) -> tuple[dict[str, object], dict[str, Path]]:
    manifest = _require_object(value, label=f"{expected_kind} raw inventory")
    _require_exact_fields(
        manifest,
        {
            "cell_id",
            "file_count",
            "files",
            "github_sha",
            "inventory_digest",
            "kind",
            "revision",
            "schema_version",
            "status",
        },
        label=f"{expected_kind} raw inventory",
    )
    if (
        manifest["schema_version"] != 1
        or manifest["status"] != "recorded"
        or manifest["kind"] != expected_kind
    ):
        raise EvidencePacketError(
            "raw_evidence_malformed", f"{expected_kind} raw inventory is invalid"
        )
    revision = _require_revision(
        manifest["revision"], label=f"{expected_kind} raw inventory revision"
    )
    if revision != candidate or manifest["github_sha"] != candidate:
        raise EvidencePacketError(
            "revision_mismatch", f"{expected_kind} raw inventory has another revision"
        )
    cell_id = manifest["cell_id"]
    if expected_kind == "performance":
        if cell_id not in EXPECTED_PLATFORMS:
            raise EvidencePacketError(
                "raw_evidence_cell_invalid", "performance inventory cell is invalid"
            )
    elif cell_id is not None:
        raise EvidencePacketError(
            "raw_evidence_cell_invalid", "non-matrix inventory has a cell id"
        )

    declared_inventory_digest = _require_digest(
        manifest["inventory_digest"], label=f"{expected_kind} inventory digest"
    )
    digest_input = dict(manifest)
    digest_input.pop("inventory_digest")
    expected_inventory_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(digest_input)
    ).hexdigest()
    if declared_inventory_digest != expected_inventory_digest:
        raise EvidencePacketError(
            "raw_evidence_inventory_digest_mismatch",
            f"{expected_kind} inventory digest is inconsistent",
        )

    files = manifest["files"]
    count = manifest["file_count"]
    expected_names = RAW_EVIDENCE_FILES[expected_kind]
    if type(files) is not list or count != len(files) or count != len(expected_names):
        raise EvidencePacketError(
            "raw_evidence_set_invalid", f"{expected_kind} inventory is incomplete"
        )
    directory = manifest_path.parent.resolve(strict=True)
    normalized: list[dict[str, object]] = []
    paths: dict[str, Path] = {}
    for raw_file in files:
        item = _require_object(raw_file, label="raw evidence file")
        _require_exact_fields(
            item, {"path", "sha256", "size_bytes"}, label="raw evidence file"
        )
        filename = item["path"]
        if (
            type(filename) is not str
            or not filename
            or filename != PurePosixPath(filename).name
            or "\\" in filename
            or "\x00" in filename
        ):
            raise EvidencePacketError(
                "raw_evidence_path_invalid", "raw evidence path is invalid"
            )
        declared_digest = _require_digest(
            item["sha256"], label=f"{filename} raw digest"
        )
        declared_size = _require_nonnegative_int(
            item["size_bytes"], label=f"{filename} raw size"
        )
        raw_path = manifest_path.parent / filename
        if not _is_regular_under(raw_path, directory):
            raise EvidencePacketError(
                "raw_evidence_file_missing", f"raw evidence file is unavailable: {filename}"
            )
        if (
            declared_size == 0
            or raw_path.stat().st_size != declared_size
            or _sha256_file(raw_path) != declared_digest
        ):
            raise EvidencePacketError(
                "raw_evidence_hash_mismatch",
                f"raw evidence file does not match inventory: {filename}",
            )
        if filename in paths:
            raise EvidencePacketError(
                "raw_evidence_set_invalid", "raw evidence paths are duplicated"
            )
        paths[filename] = raw_path
        normalized.append(
            {"path": filename, "sha256": declared_digest, "size_bytes": declared_size}
        )
    if set(paths) != set(expected_names):
        raise EvidencePacketError(
            "raw_evidence_set_invalid", f"{expected_kind} raw evidence set is incomplete"
        )
    normalized_manifest = {
        "cell_id": cell_id,
        "files": sorted(normalized, key=lambda item: str(item["path"])),
        "inventory_digest": declared_inventory_digest,
        "manifest_sha256": _sha256_file(manifest_path),
        "manifest_size_bytes": manifest_path.stat().st_size,
    }
    return normalized_manifest, paths


def _resolve_raw_artifact(root: Path, filename: str) -> Path:
    if (
        type(filename) is not str
        or not filename
        or filename != PurePosixPath(filename).name
        or "\\" in filename
        or "\x00" in filename
    ):
        raise EvidencePacketError("distribution_path_invalid", "raw artifact path is invalid")
    matches = tuple(
        path
        for path in root.rglob(filename)
        if path.name == filename and _is_regular_under(path, root)
    )
    if len(matches) != 1:
        raise EvidencePacketError(
            "distribution_file_ambiguous",
            f"expected one raw distribution file named {filename}, found {len(matches)}",
        )
    return matches[0]


def _validate_distribution(
    value: object, evidence_path: Path, root: Path, candidate: str
) -> dict[str, object]:
    report = _require_object(value, label="distribution evidence")
    _require_exact_fields(
        report,
        {
            "artifact_count",
            "artifacts",
            "evidence_digest",
            "github_sha",
            "revision",
            "schema_version",
            "skill_zip_deterministic",
            "status",
        },
        label="distribution evidence",
    )
    if (
        report["schema_version"] != 1
        or report["status"] != "passed"
        or report["skill_zip_deterministic"] is not True
    ):
        raise EvidencePacketError(
            "distribution_not_passed", "distribution evidence did not pass"
        )
    revision = _require_revision(report["revision"], label="distribution revision")
    if revision != candidate or report["github_sha"] != candidate:
        raise EvidencePacketError(
            "revision_mismatch", "distribution evidence is not bound to the candidate"
        )
    evidence_digest = _require_digest(
        report["evidence_digest"], label="distribution evidence digest"
    )
    digest_input = dict(report)
    digest_input.pop("evidence_digest")
    expected_evidence_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(digest_input)
    ).hexdigest()
    if evidence_digest != expected_evidence_digest:
        raise EvidencePacketError(
            "distribution_evidence_digest_mismatch",
            "distribution evidence digest is inconsistent",
        )

    artifacts = report["artifacts"]
    count = report["artifact_count"]
    if type(artifacts) is not list or count != 4 or len(artifacts) != 4:
        raise EvidencePacketError(
            "distribution_malformed", "distribution artifact set is incomplete"
        )
    expected_kinds = {"wheel", "sdist", "skill_zip", "skill_zip_repeat"}
    normalized: list[dict[str, object]] = []
    kinds: list[str] = []
    filenames: list[str] = []
    raw_paths: dict[str, Path] = {}
    for artifact in artifacts:
        artifact_object = _require_object(artifact, label="distribution artifact")
        _require_exact_fields(
            artifact_object,
            {"kind", "path", "sha256", "size_bytes"},
            label="distribution artifact",
        )
        kind = artifact_object["kind"]
        filename = artifact_object["path"]
        if type(kind) is not str or kind not in expected_kinds:
            raise EvidencePacketError("distribution_malformed", "artifact kind is invalid")
        if type(filename) is not str:
            raise EvidencePacketError("distribution_path_invalid", "artifact path is invalid")
        declared_digest = _require_digest(
            artifact_object["sha256"], label=f"{kind} raw digest"
        )
        size = _require_nonnegative_int(
            artifact_object["size_bytes"], label=f"{kind} size"
        )
        expected_suffix = {
            "wheel": ".whl",
            "sdist": ".tar.gz",
            "skill_zip": ".zip",
            "skill_zip_repeat": ".zip",
        }[kind]
        if not filename.endswith(expected_suffix) or size == 0:
            raise EvidencePacketError(
                "distribution_malformed", f"{kind} filename or size is invalid"
            )
        raw = _resolve_raw_artifact(root, filename)
        if raw.stat().st_size != size or _sha256_file(raw) != declared_digest:
            raise EvidencePacketError(
                "distribution_hash_mismatch", f"raw {kind} file does not match evidence"
            )
        kinds.append(kind)
        filenames.append(filename)
        raw_paths[kind] = raw
        normalized.append(
            {"kind": kind, "path": filename, "sha256": declared_digest, "size_bytes": size}
        )
    if (
        len(kinds) != len(set(kinds))
        or set(kinds) != expected_kinds
        or len(filenames) != len(set(filenames))
    ):
        raise EvidencePacketError(
            "distribution_malformed", "distribution roles or paths are duplicated"
        )
    by_kind = {str(item["kind"]): item for item in normalized}
    if by_kind["skill_zip"]["sha256"] != by_kind["skill_zip_repeat"]["sha256"]:
        raise EvidencePacketError(
            "skill_zip_not_deterministic", "Skill ZIP raw hashes do not match"
        )
    try:
        validate_distribution_artifacts(
            raw_paths["wheel"],
            raw_paths["sdist"],
            raw_paths["skill_zip"],
            raw_paths["skill_zip_repeat"],
        )
    except DistributionEvidenceError as exc:
        raise EvidencePacketError(
            "distribution_content_invalid",
            f"raw distribution archive validation failed: {exc}",
        ) from exc
    return {
        "artifacts": sorted(normalized, key=lambda item: str(item["kind"])),
        "evidence_digest": evidence_digest,
        "evidence_sha256": _sha256_file(evidence_path),
        "evidence_size_bytes": evidence_path.stat().st_size,
        "schema_version": 1,
        "status": "passed",
    }


def _validate_distribution_install_cell(
    value: object,
    evidence_path: Path,
    *,
    candidate: str,
    distribution: Mapping[str, object],
    distribution_evidence_sha256: str,
) -> dict[str, object]:
    report = _require_object(value, label="distribution clean-install evidence")
    _require_exact_fields(
        report,
        {
            "artifacts",
            "cell_id",
            "distribution_evidence_digest",
            "distribution_evidence_sha256",
            "evidence_digest",
            "github_sha",
            "installations",
            "platform",
            "python_version",
            "revision",
            "runtime",
            "schema_version",
            "status",
        },
        label="distribution clean-install evidence",
    )
    if report["schema_version"] != 1 or report["status"] != "passed":
        raise EvidencePacketError(
            "distribution_install_not_passed",
            "distribution clean-install evidence did not pass",
        )
    revision = _require_revision(
        report["revision"], label="distribution clean-install revision"
    )
    if revision != candidate or report["github_sha"] != candidate:
        raise EvidencePacketError(
            "revision_mismatch",
            "distribution clean-install evidence has another revision",
        )
    platform = report["platform"]
    python_version = report["python_version"]
    if platform not in EXPECTED_PLATFORMS or python_version not in EXPECTED_PYTHON_VERSIONS:
        raise EvidencePacketError(
            "distribution_install_cell_invalid",
            "distribution clean-install cell is unsupported",
        )
    expected_cell_id = f"{platform}-py{python_version}"
    if report["cell_id"] != expected_cell_id:
        raise EvidencePacketError(
            "distribution_install_cell_invalid",
            "distribution clean-install cell id is inconsistent",
        )

    evidence_digest = _require_digest(
        report["evidence_digest"], label="distribution clean-install evidence digest"
    )
    digest_input = dict(report)
    digest_input.pop("evidence_digest")
    if evidence_digest != "sha256:" + hashlib.sha256(
        canonical_json_bytes(digest_input)
    ).hexdigest():
        raise EvidencePacketError(
            "distribution_install_digest_mismatch",
            "distribution clean-install evidence digest is inconsistent",
        )
    if report["distribution_evidence_digest"] != distribution["evidence_digest"]:
        raise EvidencePacketError(
            "distribution_install_source_mismatch",
            "clean-install cell cites another distribution evidence digest",
        )
    if report["distribution_evidence_sha256"] != distribution_evidence_sha256:
        raise EvidencePacketError(
            "distribution_install_source_mismatch",
            "clean-install cell cites another raw distribution evidence file",
        )

    runtime = _require_object(report["runtime"], label="clean-install runtime")
    _require_exact_fields(
        runtime,
        {"implementation", "python_full_version", "sys_platform"},
        label="clean-install runtime",
    )
    full_version = runtime["python_full_version"]
    expected_sys_platform = {
        "ubuntu-latest": "linux",
        "windows-latest": "win32",
    }[str(platform)]
    if (
        runtime["implementation"] != "cpython"
        or type(full_version) is not str
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", full_version) is None
        or ".".join(full_version.split(".")[:2]) != python_version
        or runtime["sys_platform"] != expected_sys_platform
    ):
        raise EvidencePacketError(
            "distribution_install_runtime_mismatch",
            "clean-install runtime does not match its declared matrix cell",
        )

    canonical_by_kind = {
        str(item["kind"]): item
        for item in distribution["artifacts"]  # type: ignore[union-attr]
        if item["kind"] in {"wheel", "sdist"}
    }
    artifacts = report["artifacts"]
    if type(artifacts) is not list or len(artifacts) != 2:
        raise EvidencePacketError(
            "distribution_install_artifacts_invalid",
            "clean-install cell must identify exactly one wheel and one sdist",
        )
    normalized_artifacts: list[dict[str, object]] = []
    for artifact in artifacts:
        artifact_object = _require_object(
            artifact, label="distribution clean-install artifact"
        )
        _require_exact_fields(
            artifact_object,
            {"kind", "path", "sha256", "size_bytes"},
            label="distribution clean-install artifact",
        )
        kind = artifact_object["kind"]
        if type(kind) is not str or kind not in canonical_by_kind:
            raise EvidencePacketError(
                "distribution_install_artifacts_invalid",
                "clean-install artifact kind is invalid",
            )
        if artifact_object != canonical_by_kind[kind]:
            raise EvidencePacketError(
                "distribution_install_artifact_mismatch",
                "clean-install cell did not consume the canonical distribution bytes",
            )
        normalized_artifacts.append(dict(artifact_object))
    artifact_kinds = [str(item["kind"]) for item in normalized_artifacts]
    if len(set(artifact_kinds)) != 2 or set(artifact_kinds) != {"wheel", "sdist"}:
        raise EvidencePacketError(
            "distribution_install_artifacts_invalid",
            "clean-install artifact roles are duplicate or incomplete",
        )

    installations = report["installations"]
    if type(installations) is not list or len(installations) != 2:
        raise EvidencePacketError(
            "distribution_install_result_invalid",
            "clean-install cell must pass both distribution roles",
        )
    normalized_installations: list[dict[str, str]] = []
    for installation in installations:
        item = _require_object(installation, label="clean-install result")
        _require_exact_fields(
            item,
            {"artifact_kind", "artifact_sha256", "status"},
            label="clean-install result",
        )
        kind = item["artifact_kind"]
        if (
            type(kind) is not str
            or kind not in canonical_by_kind
            or item["status"] != "passed"
            or item["artifact_sha256"] != canonical_by_kind[kind]["sha256"]
        ):
            raise EvidencePacketError(
                "distribution_install_result_invalid",
                "clean-install result does not prove the canonical artifact passed",
            )
        normalized_installations.append(
            {
                "artifact_kind": kind,
                "artifact_sha256": str(item["artifact_sha256"]),
                "status": "passed",
            }
        )
    installed_kinds = [item["artifact_kind"] for item in normalized_installations]
    if len(set(installed_kinds)) != 2 or set(installed_kinds) != {"wheel", "sdist"}:
        raise EvidencePacketError(
            "distribution_install_result_invalid",
            "clean-install results are duplicate or incomplete",
        )
    return {
        "artifacts": sorted(normalized_artifacts, key=lambda item: str(item["kind"])),
        "cell_id": expected_cell_id,
        "distribution_evidence_digest": report["distribution_evidence_digest"],
        "distribution_evidence_sha256": distribution_evidence_sha256,
        "evidence_digest": evidence_digest,
        "evidence_sha256": _sha256_file(evidence_path),
        "evidence_size_bytes": evidence_path.stat().st_size,
        "installations": sorted(
            normalized_installations, key=lambda item: item["artifact_kind"]
        ),
        "platform": platform,
        "python_version": python_version,
        "runtime": dict(runtime),
        "schema_version": 1,
        "status": "passed",
    }


def _validate_distribution_install_matrix(
    root: Path,
    candidate: str,
    distribution: Mapping[str, object],
    distribution_evidence_path: Path,
) -> list[dict[str, object]]:
    evidence_paths = _discover(
        root, "distribution-install-evidence.json", expected=6
    )
    distribution_evidence_sha256 = _sha256_file(distribution_evidence_path)
    values = [
        _validate_distribution_install_cell(
            _read_json(path, label="distribution clean-install evidence"),
            path,
            candidate=candidate,
            distribution=distribution,
            distribution_evidence_sha256=distribution_evidence_sha256,
        )
        for path in evidence_paths
    ]
    expected_ids = {
        f"{platform}-py{version}"
        for platform in EXPECTED_PLATFORMS
        for version in EXPECTED_PYTHON_VERSIONS
    }
    cell_ids = [str(item["cell_id"]) for item in values]
    if len(cell_ids) != len(set(cell_ids)) or set(cell_ids) != expected_ids:
        raise EvidencePacketError(
            "distribution_install_matrix_incomplete",
            "distribution clean-install evidence has duplicate or missing cells",
        )
    return sorted(values, key=lambda item: str(item["cell_id"]))


def stamp_performance_gate(
    gate: object, *, platform: str, revision: str
) -> dict[str, object]:
    """Bind an already-written performance gate report to its CI matrix cell."""
    candidate = _require_revision(revision)
    if platform not in EXPECTED_PLATFORMS:
        raise EvidencePacketError("performance_platform_invalid", "platform is unsupported")
    report = _require_object(gate, label="performance gate")
    gate_digest = "sha256:" + hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    return {
        "gate": report,
        "gate_digest": gate_digest,
        "github_sha": candidate,
        "platform": platform,
        "revision": candidate,
        "schema_version": 1,
        "status": "passed" if report.get("passed") is True else "failed",
    }


def _validate_performance_evidence(
    value: object, gate: Mapping[str, object]
) -> None:
    evidence = _require_object(value, label="performance raw evidence")
    validation_errors = validate_performance_evidence(evidence)
    if validation_errors:
        raise EvidencePacketError(
            "performance_raw_invalid",
            "performance evidence failed canonical validation: "
            + ", ".join(validation_errors),
        )

    report = evaluate_performance_gate(
        evidence,
        None,
        controlled=False,
        require_baseline=False,
    )
    workloads = evidence["workloads"]
    replay = workloads["replay_100000_events"]
    graph = workloads["graph_64_tasks_512_edges"]
    expected_gate = {
        **report.to_primitive(),
        "environment_digest": evidence["environment"]["identity_digest"],
        "evidence": gate.get("evidence"),
        "summaries": {
            "checkpoint_tail": replay["summaries"]["checkpoint_tail"],
            "cold_replay": replay["summaries"]["cold_replay"],
            "graph_batch": graph["summaries"]["batch"],
            "peak_rss_bytes": replay["measurements"]["peak_rss_bytes"],
        },
    }
    evidence_path = gate.get("evidence")
    if (
        type(evidence_path) is not str
        or evidence_path.replace("\\", "/").rsplit("/", 1)[-1]
        != "performance-evidence.json"
    ):
        raise EvidencePacketError(
            "performance_malformed", "performance evidence path is not the canonical filename"
        )
    if gate != expected_gate:
        raise EvidencePacketError(
            "performance_gate_mismatch",
            "performance gate is not the diagnostic result of canonical raw evidence",
        )


def _validate_performance(
    value: object,
    raw_gate_value: object,
    raw_evidence_value: object,
    candidate: str,
) -> dict[str, object]:
    wrapper = _require_object(value, label="performance evidence")
    _require_exact_fields(
        wrapper,
        {
            "gate",
            "gate_digest",
            "github_sha",
            "platform",
            "revision",
            "schema_version",
            "status",
        },
        label="performance evidence",
    )
    if wrapper["schema_version"] != 1 or wrapper["status"] != "passed":
        raise EvidencePacketError("performance_not_passed", "performance gate did not pass")
    revision = _require_revision(wrapper["revision"], label="performance revision")
    if revision != candidate or wrapper["github_sha"] != candidate:
        raise EvidencePacketError(
            "revision_mismatch", "performance evidence is not bound to the candidate"
        )
    platform = wrapper["platform"]
    if platform not in EXPECTED_PLATFORMS:
        raise EvidencePacketError(
            "performance_platform_invalid", "performance platform is unsupported"
        )
    gate = _require_object(wrapper["gate"], label="performance gate")
    _require_exact_fields(
        gate,
        {"environment_digest", "evidence", "findings", "passed", "summaries", "warnings"},
        label="performance gate",
    )
    if gate["passed"] is not True or gate["findings"] != []:
        raise EvidencePacketError("performance_not_passed", "performance gate has findings")
    if type(gate["warnings"]) is not list or any(
        type(item) is not str for item in gate["warnings"]
    ):
        raise EvidencePacketError("performance_malformed", "performance warnings are invalid")
    if type(gate["evidence"]) is not str or not gate["evidence"]:
        raise EvidencePacketError("performance_malformed", "performance evidence path is absent")
    _require_digest(gate["environment_digest"], label="performance environment digest")
    summaries = gate["summaries"]
    if type(summaries) is not dict or set(summaries) != {
        "checkpoint_tail",
        "cold_replay",
        "graph_batch",
        "peak_rss_bytes",
    }:
        raise EvidencePacketError("performance_malformed", "performance summaries are incomplete")
    for name in ("checkpoint_tail", "cold_replay", "graph_batch"):
        summary = summaries[name]
        if type(summary) is not dict or set(summary) != {
            "minimum_ms",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "sample_count",
        }:
            raise EvidencePacketError(
                "performance_malformed", f"{name} summary fields are invalid"
            )
        ordered = [summary[key] for key in ("minimum_ms", "p50_ms", "p95_ms", "p99_ms")]
        if (
            any(type(item) is not int or item < 0 for item in ordered)
            or ordered != sorted(ordered)
            or type(summary["sample_count"]) is not int
            or summary["sample_count"] <= 0
        ):
            raise EvidencePacketError(
                "performance_malformed", f"{name} summary values are invalid"
            )
    if type(summaries["peak_rss_bytes"]) is not int or summaries["peak_rss_bytes"] <= 0:
        raise EvidencePacketError(
            "performance_malformed", "performance peak RSS is invalid"
        )
    declared_gate_digest = _require_digest(
        wrapper["gate_digest"], label="performance gate digest"
    )
    expected_gate_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(gate)
    ).hexdigest()
    if declared_gate_digest != expected_gate_digest:
        raise EvidencePacketError(
            "performance_digest_mismatch", "performance gate digest is inconsistent"
        )
    raw_gate = _require_object(raw_gate_value, label="raw performance gate")
    if raw_gate != gate:
        raise EvidencePacketError(
            "performance_gate_mismatch", "stamped gate differs from raw gate report"
        )
    _validate_performance_evidence(raw_evidence_value, gate)
    return {
        "environment_digest": gate["environment_digest"],
        "gate_digest": declared_gate_digest,
        "mode": "diagnostic",
        "platform": platform,
        "warnings": list(gate["warnings"]),
    }


def _validate_performance_matrix(
    root: Path, candidate: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    values: list[dict[str, object]] = []
    inventories: list[dict[str, object]] = []
    for manifest_path in _discover(
        root, "performance-raw-evidence.json", expected=len(EXPECTED_PLATFORMS)
    ):
        inventory, paths = _validate_raw_inventory(
            _read_json(manifest_path, label="performance raw inventory"),
            manifest_path,
            candidate=candidate,
            expected_kind="performance",
        )
        try:
            raw_evidence = read_performance_evidence(paths["performance-evidence.json"])
        except (OSError, ValueError) as exc:
            raise EvidencePacketError(
                "performance_raw_invalid",
                "performance evidence is not canonical or schema-valid",
            ) from exc
        performance = _validate_performance(
            _read_json(paths["performance-gate.json"], label="performance evidence"),
            _read_json(paths["performance-gate.raw.json"], label="raw performance gate"),
            raw_evidence,
            candidate,
        )
        if inventory["cell_id"] != performance["platform"]:
            raise EvidencePacketError(
                "performance_platform_mismatch",
                "performance inventory and gate identify different cells",
            )
        values.append(performance)
        inventories.append(inventory)
    platforms = [str(item["platform"]) for item in values]
    if len(platforms) != len(set(platforms)) or set(platforms) != set(EXPECTED_PLATFORMS):
        raise EvidencePacketError(
            "performance_matrix_incomplete",
            "performance evidence has duplicate or missing platforms",
        )
    return (
        sorted(values, key=lambda item: str(item["platform"])),
        sorted(inventories, key=lambda item: str(item["cell_id"])),
    )


def _validate_needs(value: object) -> dict[str, str]:
    needs = _require_object(value, label="GitHub needs context")
    if set(needs) != set(REQUIRED_JOBS):
        raise EvidencePacketError(
            "job_set_incomplete", "GitHub needs context is duplicate or incomplete"
        )
    normalized: dict[str, str] = {}
    for job in REQUIRED_JOBS:
        state = needs[job]
        if type(state) is not dict or state.get("result") != "success":
            raise EvidencePacketError("job_not_passed", f"required job did not pass: {job}")
        normalized[job] = "success"
    return normalized


def build_evidence_packet(
    evidence_root: Path,
    *,
    candidate_revision: str,
    needs: object,
    workflow_run_id: str,
    workflow_run_attempt: str,
    safety_base_ref: str,
) -> dict[str, object]:
    """Validate current-workflow artifacts and return a normalized packet."""
    root = _regular_root(evidence_root)
    candidate = _require_revision(candidate_revision, label="candidate revision")
    if type(workflow_run_id) is not str or not workflow_run_id.isdigit():
        raise EvidencePacketError("workflow_identity_invalid", "workflow run id is invalid")
    if type(workflow_run_attempt) is not str or not workflow_run_attempt.isdigit():
        raise EvidencePacketError("workflow_identity_invalid", "workflow attempt is invalid")
    if (
        type(safety_base_ref) is not str
        or not safety_base_ref.strip()
        or "\x00" in safety_base_ref
    ):
        raise EvidencePacketError(
            "safety_base_invalid", "trusted safety comparison base is invalid"
        )

    job_results = _validate_needs(needs)
    python_matrix = _validate_python_matrix(root, candidate)
    trellis_matrix = _validate_trellis_matrix(root, candidate)
    raw_inventories: dict[str, object] = {}

    coverage_manifest = _discover(root, "coverage-raw-evidence.json", expected=1)[0]
    coverage_inventory, coverage_paths = _validate_raw_inventory(
        _read_json(coverage_manifest, label="coverage raw inventory"),
        coverage_manifest,
        candidate=candidate,
        expected_kind="coverage",
    )
    raw_inventories["coverage"] = coverage_inventory
    raw_coverage = _read_json(coverage_paths["coverage.json"], label="raw coverage")
    coverage = _validate_coverage(
        _read_json(coverage_paths["coverage-gate.json"], label="coverage gate"),
        raw_coverage,
    )

    mutation_manifest = _discover(root, "mutation-raw-evidence.json", expected=1)[0]
    mutation_inventory, mutation_paths = _validate_raw_inventory(
        _read_json(mutation_manifest, label="mutation raw inventory"),
        mutation_manifest,
        candidate=candidate,
        expected_kind="mutation",
    )
    raw_inventories["mutation"] = mutation_inventory
    raw_mutation = _read_json(
        mutation_paths["mutation-report.json"], label="mutation gate"
    )
    mutation = _validate_mutation(raw_mutation)

    safety_manifest = _discover(root, "safety-raw-evidence.json", expected=1)[0]
    safety_inventory, safety_paths = _validate_raw_inventory(
        _read_json(safety_manifest, label="safety raw inventory"),
        safety_manifest,
        candidate=candidate,
        expected_kind="safety",
    )
    raw_inventories["safety"] = safety_inventory
    raw_changed_lines = _read_json(
        safety_paths["changed-lines.json"], label="changed-lines evidence"
    )
    try:
        trusted_changed_lines = collect_changed_lines(
            REPOSITORY_ROOT,
            safety_base_ref,
            governed_paths=ACTIVE_M1_SAFETY_PATHS.git_pathspecs,
        )
    except ChangedLinesInputError as exc:
        raise EvidencePacketError(
            "safety_base_unavailable",
            "cannot independently collect changed safety lines from the trusted base",
        ) from exc
    if trusted_changed_lines.get("head") != candidate:
        raise EvidencePacketError(
            "safety_revision_mismatch",
            "checked-out repository HEAD is not the candidate revision",
        )
    if raw_changed_lines != trusted_changed_lines:
        raise EvidencePacketError(
            "safety_changed_lines_mismatch",
            "changed-lines artifact differs from the trusted Git comparison",
        )
    recomputed_safety = evaluate_safety_evidence(
        raw_coverage,
        raw_mutation,
        raw_changed_lines,
        trusted_changed_lines_report=trusted_changed_lines,
    )
    safety = _validate_safety(
        _read_json(safety_paths["safety-evidence.json"], label="safety evidence"),
        recomputed_safety,
        candidate,
        safety_base_ref,
    )
    if mutation["mutation_ids"] != safety["mutation_ids"]:
        raise EvidencePacketError(
            "safety_mutation_set_mismatch",
            "mutation report and safety evidence identify different mutation sets",
        )

    distribution_path = _discover(root, "distribution-evidence.json", expected=1)[0]
    distribution = _validate_distribution(
        _read_json(
            distribution_path,
            label="distribution evidence",
        ),
        distribution_path,
        root,
        candidate,
    )
    distribution_matrix = _validate_distribution_install_matrix(
        root,
        candidate,
        distribution,
        distribution_path,
    )
    performance, performance_inventories = _validate_performance_matrix(root, candidate)
    raw_inventories["performance"] = performance_inventories
    packet: dict[str, object] = {
        "candidate_revision": candidate,
        "distribution": distribution,
        "distribution_matrix": distribution_matrix,
        "gates": {
            "coverage": coverage,
            "mutation": mutation,
            "performance": performance,
            "safety": safety,
        },
        "job_results": job_results,
        "python_matrix": python_matrix,
        "raw_evidence": raw_inventories,
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "trellis_matrix": trellis_matrix,
        "workflow": {
            "run_attempt": int(workflow_run_attempt),
            "run_id": int(workflow_run_id),
        },
    }
    packet["packet_digest"] = "sha256:" + hashlib.sha256(
        canonical_json_bytes(packet)
    ).hexdigest()
    return packet


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


def _write_packet_and_digest(
    output: Path, digest_output: Path, packet: Mapping[str, object]
) -> None:
    encoded = canonical_json_bytes(packet)
    _write_atomic(output, encoded)
    raw_digest = "sha256:" + hashlib.sha256(encoded).hexdigest() + "\n"
    _write_atomic(digest_output, raw_digest.encode("ascii"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    record = commands.add_parser(
        "record-files", help="content-address the raw inputs emitted by one gate job"
    )
    record.add_argument("--kind", choices=sorted(RAW_EVIDENCE_FILES), required=True)
    record.add_argument("--cell-id")
    record.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    record.add_argument("--file", type=Path, action="append", required=True)
    record.add_argument("--output", type=Path, required=True)

    stamp = commands.add_parser(
        "stamp-performance", help="bind a performance gate to one matrix cell"
    )
    stamp.add_argument("--gate-input", type=Path, required=True)
    stamp.add_argument("--platform", required=True)
    stamp.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    stamp.add_argument("--output", type=Path, required=True)

    build = commands.add_parser("build", help="assemble the final evidence packet")
    build.add_argument("--evidence-root", type=Path, required=True)
    build.add_argument("--candidate-revision", default=os.environ.get("GITHUB_SHA"))
    build.add_argument("--needs-json", required=True)
    build.add_argument("--workflow-run-id", required=True)
    build.add_argument("--workflow-run-attempt", required=True)
    build.add_argument(
        "--safety-base-ref",
        required=True,
        help="trusted workflow comparison base independently resolved in this job",
    )
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--digest-output", type=Path, required=True)
    return parser


def _failure_packet(
    error: EvidencePacketError, candidate_revision: object
) -> dict[str, object]:
    packet: dict[str, object] = {
        "candidate_revision": candidate_revision,
        "errors": [{"code": error.code, "message": str(error)}],
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
    }
    packet["packet_digest"] = "sha256:" + hashlib.sha256(
        canonical_json_bytes(packet)
    ).hexdigest()
    return packet


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "record-files":
        try:
            result = record_raw_evidence(
                arguments.file,
                kind=arguments.kind,
                revision=arguments.revision,
                cell_id=arguments.cell_id,
            )
            encoded = canonical_json_bytes(result)
            _write_atomic(arguments.output, encoded)
        except (EvidencePacketError, OSError) as exc:
            print(f"cannot record raw evidence: {exc}", file=sys.stderr)
            return 2
        sys.stdout.buffer.write(encoded)
        return 0
    if arguments.command == "stamp-performance":
        try:
            result = stamp_performance_gate(
                _read_json(arguments.gate_input, label="performance gate"),
                platform=arguments.platform,
                revision=arguments.revision,
            )
            encoded = canonical_json_bytes(result)
            _write_atomic(arguments.output, encoded)
        except (EvidencePacketError, OSError) as exc:
            print(f"cannot stamp performance gate: {exc}", file=sys.stderr)
            return 2
        sys.stdout.buffer.write(encoded)
        return 0 if result["status"] == "passed" else 1

    try:
        needs = _decode_json_bytes(arguments.needs_json.encode("utf-8"), label="needs")
        packet = build_evidence_packet(
            arguments.evidence_root,
            candidate_revision=arguments.candidate_revision,
            needs=needs,
            workflow_run_id=arguments.workflow_run_id,
            workflow_run_attempt=arguments.workflow_run_attempt,
            safety_base_ref=arguments.safety_base_ref,
        )
        exit_code = 0
    except EvidencePacketError as exc:
        packet = _failure_packet(exc, arguments.candidate_revision)
        exit_code = 1
    except (OSError, ValueError) as exc:
        packet = _failure_packet(
            EvidencePacketError(
                "input_error", str(exc) or type(exc).__name__
            ),
            arguments.candidate_revision,
        )
        exit_code = 1
    except Exception as exc:
        packet = _failure_packet(
            EvidencePacketError(
                "unexpected_error", str(exc) or type(exc).__name__
            ),
            arguments.candidate_revision,
        )
        exit_code = 1
    try:
        _write_packet_and_digest(arguments.output, arguments.digest_output, packet)
    except OSError as exc:
        print(f"cannot write evidence packet: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(packet))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
