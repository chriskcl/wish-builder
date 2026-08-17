#!/usr/bin/env python3
"""Validate and inspect wish-builder execution manifests."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
REQUIREMENT_STATUSES = {"approved", "implemented", "deferred", "out_of_scope"}
TASK_STATUSES = {
    "proposed",
    "approved",
    "ready",
    "dispatched",
    "pr_open",
    "merged",
    "verified",
    "archived",
    "blocked",
    "failed",
}
DONE_STATUSES = {"merged", "verified", "archived"}
ACTIVE_STATUSES = {"dispatched", "pr_open"}
DISPATCHABLE_STATUSES = {"approved", "ready"}
RISK_LEVELS = {"low", "medium", "high"}
ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9][A-Z0-9-]*$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ManifestError(Exception):
    """Raised for an unreadable manifest."""


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"invalid JSON at {manifest_path}:{exc.lineno}:{exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be a JSON object")
    return data


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_id(value: Any) -> bool:
    if isinstance(value, int):
        return value > 0
    return _nonempty_string(value)


def _string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_nonempty_string(item) for item in value)
    )


def _gate_is_approved(manifest: dict[str, Any], gate_name: str) -> bool:
    gate = manifest.get("approved", {}).get(gate_name, {})
    return (
        isinstance(gate, dict)
        and _nonempty_string(gate.get("approved_by"))
        and _nonempty_string(gate.get("approved_at"))
        and isinstance(gate.get("artifact_hash"), str)
        and bool(HASH_RE.match(gate["artifact_hash"]))
    )


def _normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _static_prefix(pattern: str) -> str:
    normalized = _normalize_path(pattern)
    glob_positions = [
        position for token in "*?[" if (position := normalized.find(token)) >= 0
    ]
    if glob_positions:
        normalized = normalized[: min(glob_positions)]
    return normalized.rstrip("/")


def _contains_path(prefix: str, path: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def patterns_overlap(left: str, right: str) -> bool:
    """Conservatively detect whether two repository path patterns may overlap."""
    left_normalized = _normalize_path(left).casefold()
    right_normalized = _normalize_path(right).casefold()
    if left_normalized == right_normalized:
        return True

    left_prefix = _static_prefix(left_normalized)
    right_prefix = _static_prefix(right_normalized)
    if not left_prefix or not right_prefix:
        return True
    return _contains_path(left_prefix, right_prefix) or _contains_path(
        right_prefix, left_prefix
    )


def path_matches(path: str, pattern: str) -> bool:
    normalized_path = _normalize_path(path).casefold()
    normalized_pattern = _normalize_path(pattern).casefold()
    if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
        return True
    has_glob = any(token in normalized_pattern for token in "*?[")
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return bool(prefix) and _contains_path(prefix, normalized_path)
    if has_glob:
        return False
    return _contains_path(normalized_pattern, normalized_path)


def _task_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = manifest.get("tasks", [])
    if not isinstance(tasks, list):
        return {}
    return {
        task["id"]: task
        for task in tasks
        if isinstance(task, dict) and _nonempty_string(task.get("id"))
    }


def _depends_on(
    task_id: str,
    dependency_id: str,
    tasks: dict[str, dict[str, Any]],
    seen: set[str] | None = None,
) -> bool:
    if seen is None:
        seen = set()
    if task_id in seen or task_id not in tasks:
        return False
    seen.add(task_id)
    dependencies = tasks[task_id].get("depends_on", [])
    if dependency_id in dependencies:
        return True
    return any(
        _depends_on(parent, dependency_id, tasks, seen.copy())
        for parent in dependencies
        if parent in tasks
    )


def _find_cycles(tasks: dict[str, dict[str, Any]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(task_id: str) -> None:
        state[task_id] = 1
        stack.append(task_id)
        for dependency in tasks[task_id].get("depends_on", []):
            if dependency not in tasks:
                continue
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                cycle = stack[start:] + [dependency]
                if cycle not in cycles:
                    cycles.append(cycle)
        stack.pop()
        state[task_id] = 2

    for task_id in tasks:
        if state.get(task_id, 0) == 0:
            visit(task_id)
    return cycles


def _max_depth(tasks: dict[str, dict[str, Any]]) -> int:
    memo: dict[str, int] = {}

    def depth(task_id: str, visiting: set[str]) -> int:
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting:
            return 0
        dependencies = [
            dependency
            for dependency in tasks[task_id].get("depends_on", [])
            if dependency in tasks
        ]
        result = 0 if not dependencies else 1 + max(
            depth(dependency, visiting | {task_id}) for dependency in dependencies
        )
        memo[task_id] = result
        return result

    return max((depth(task_id, set()) for task_id in tasks), default=0)


def validate_manifest(
    manifest: dict[str, Any], stage: str = "planning"
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION}, got {manifest.get('schema_version')!r}"
        )
    for field in ("run_id", "goal", "base_branch"):
        if not _nonempty_string(manifest.get(field)):
            errors.append(f"{field} must be a non-empty string")

    max_concurrency = manifest.get("max_concurrency", 3)
    if not isinstance(max_concurrency, int) or max_concurrency < 1:
        errors.append("max_concurrency must be a positive integer")

    protected_paths = manifest.get("protected_paths", [])
    if not isinstance(protected_paths, list) or not all(
        _nonempty_string(path) for path in protected_paths
    ):
        errors.append("protected_paths must be a list of non-empty strings")

    requirements = manifest.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append("requirements must be a non-empty list")
        requirements = []

    requirement_ids: set[str] = set()
    active_requirement_ids: set[str] = set()
    for index, requirement in enumerate(requirements):
        label = f"requirements[{index}]"
        if not isinstance(requirement, dict):
            errors.append(f"{label} must be an object")
            continue
        requirement_id = requirement.get("id")
        if not _nonempty_string(requirement_id) or not ID_RE.match(requirement_id):
            errors.append(f"{label}.id must be a stable uppercase ID such as REQ-001")
            continue
        if requirement_id in requirement_ids:
            errors.append(f"duplicate requirement id: {requirement_id}")
        requirement_ids.add(requirement_id)
        if not _nonempty_string(requirement.get("text")):
            errors.append(f"{requirement_id}.text must be non-empty")
        status = requirement.get("status")
        if status not in REQUIREMENT_STATUSES:
            errors.append(
                f"{requirement_id}.status must be one of {sorted(REQUIREMENT_STATUSES)}"
            )
        if status in {"approved", "implemented"}:
            active_requirement_ids.add(requirement_id)

    raw_tasks = manifest.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        errors.append("tasks must be a non-empty list")
        raw_tasks = []

    task_ids: set[str] = set()
    tasks: dict[str, dict[str, Any]] = {}
    issue_owners: dict[str, str] = {}
    branch_owners: dict[str, str] = {}
    pr_owners: dict[str, str] = {}
    for index, task in enumerate(raw_tasks):
        label = f"tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{label} must be an object")
            continue
        task_id = task.get("id")
        if not _nonempty_string(task_id) or not ID_RE.match(task_id):
            errors.append(f"{label}.id must be a stable uppercase ID such as TASK-001")
            continue
        if task_id in task_ids:
            errors.append(f"duplicate task id: {task_id}")
            continue
        task_ids.add(task_id)
        tasks[task_id] = task

        if not _nonempty_string(task.get("title")):
            errors.append(f"{task_id}.title must be non-empty")
        if not _string_list(task.get("requirement_ids")):
            errors.append(f"{task_id}.requirement_ids must be a non-empty string list")
        else:
            unknown = set(task["requirement_ids"]) - requirement_ids
            if unknown:
                errors.append(f"{task_id} references unknown requirements: {sorted(unknown)}")
        if not isinstance(task.get("depends_on"), list) or not all(
            _nonempty_string(value) for value in task.get("depends_on", [])
        ):
            errors.append(f"{task_id}.depends_on must be a string list")
        if not _string_list(task.get("owned_paths")):
            errors.append(f"{task_id}.owned_paths must be a non-empty string list")
        auxiliary = task.get("allowed_auxiliary_paths", [])
        if not isinstance(auxiliary, list) or not all(
            _nonempty_string(value) for value in auxiliary
        ):
            errors.append(f"{task_id}.allowed_auxiliary_paths must be a string list")
        if not _string_list(task.get("acceptance_criteria")):
            errors.append(f"{task_id}.acceptance_criteria must be a non-empty string list")
        if not _string_list(task.get("regression_commands")):
            errors.append(f"{task_id}.regression_commands must be a non-empty string list")
        if not _nonempty_string(task.get("rollback")):
            errors.append(f"{task_id}.rollback must be a non-empty string")
        docs = task.get("documentation", [])
        if not isinstance(docs, list) or not all(_nonempty_string(value) for value in docs):
            errors.append(f"{task_id}.documentation must be a string list")
        if task.get("wave") not in {0, 1, 2}:
            errors.append(f"{task_id}.wave must be 0, 1, or 2")
        if task.get("risk") not in RISK_LEVELS:
            errors.append(f"{task_id}.risk must be one of {sorted(RISK_LEVELS)}")
        if task.get("status") not in TASK_STATUSES:
            errors.append(f"{task_id}.status must be one of {sorted(TASK_STATUSES)}")
        if task.get("may_change_contracts", False) and task.get("wave") != 0:
            errors.append(f"{task_id} may change protected contracts only in Wave 0")

        if stage in {"execution", "finish"}:
            if not _nonempty_id(task.get("issue_id")):
                errors.append(f"{task_id}.issue_id is required for {stage}")
            else:
                issue_key = str(task["issue_id"])
                if issue_key in issue_owners:
                    errors.append(
                        f"Issue {issue_key} is shared by {issue_owners[issue_key]} and {task_id}"
                    )
                issue_owners[issue_key] = task_id
            if not _nonempty_string(task.get("branch")):
                errors.append(f"{task_id}.branch is required for {stage}")
            else:
                branch = task["branch"]
                if branch == manifest.get("base_branch"):
                    errors.append(f"{task_id}.branch must differ from base_branch")
                if branch in branch_owners:
                    errors.append(
                        f"branch {branch!r} is shared by {branch_owners[branch]} and {task_id}"
                    )
                branch_owners[branch] = task_id
            if task.get("status") == "proposed":
                errors.append(f"{task_id} is still proposed after Gate B")
        if task.get("status") in ACTIVE_STATUSES and not _nonempty_string(
            task.get("agent_owner")
        ):
            errors.append(f"{task_id}.agent_owner is required while work is active")
        if task.get("status") in {"pr_open", *DONE_STATUSES}:
            if not _nonempty_id(task.get("pr_id")):
                errors.append(f"{task_id}.pr_id is required for status {task.get('status')}")
            else:
                pr_key = str(task["pr_id"])
                if pr_key in pr_owners:
                    errors.append(
                        f"PR {pr_key} is shared by {pr_owners[pr_key]} and {task_id}"
                    )
                pr_owners[pr_key] = task_id
        if task.get("status") in DONE_STATUSES and not _nonempty_string(
            task.get("squash_commit")
        ):
            errors.append(f"{task_id}.squash_commit is required after merge")
        if stage == "finish":
            if not _nonempty_id(task.get("pr_id")):
                errors.append(f"{task_id}.pr_id is required for finish")
            if task.get("status") not in DONE_STATUSES:
                errors.append(f"{task_id} is not finished: {task.get('status')!r}")

    for task_id, task in tasks.items():
        dependencies = task.get("depends_on", [])
        if task_id in dependencies:
            errors.append(f"{task_id} depends on itself")
        unknown = set(dependencies) - task_ids
        if unknown:
            errors.append(f"{task_id} references unknown dependencies: {sorted(unknown)}")
        for dependency in dependencies:
            if dependency in tasks and isinstance(task.get("wave"), int):
                dependency_wave = tasks[dependency].get("wave")
                if isinstance(dependency_wave, int) and dependency_wave > task["wave"]:
                    errors.append(
                        f"{task_id} in Wave {task['wave']} depends on later-wave {dependency}"
                    )

    cycles = _find_cycles(tasks)
    for cycle in cycles:
        errors.append("dependency cycle: " + " -> ".join(cycle))

    covered_requirements = {
        requirement_id
        for task in tasks.values()
        for requirement_id in task.get("requirement_ids", [])
    }
    uncovered = active_requirement_ids - covered_requirements
    if uncovered:
        errors.append(f"active requirements without a task: {sorted(uncovered)}")

    if not cycles:
        depth = _max_depth(tasks)
        if depth > 4:
            warnings.append(f"dependency depth is {depth}; explain why it exceeds four")

    for wave in (0, 2):
        serial_tasks = [task_id for task_id, task in tasks.items() if task.get("wave") == wave]
        for index, left in enumerate(serial_tasks):
            for right in serial_tasks[index + 1 :]:
                if not _depends_on(left, right, tasks) and not _depends_on(right, left, tasks):
                    errors.append(
                        f"Wave {wave} must be serial, but {left} and {right} are unordered"
                    )

    parallel_tasks = [task_id for task_id, task in tasks.items() if task.get("wave") == 1]
    for index, left in enumerate(parallel_tasks):
        for right in parallel_tasks[index + 1 :]:
            if _depends_on(left, right, tasks) or _depends_on(right, left, tasks):
                continue
            overlaps = [
                (left_path, right_path)
                for left_path in tasks[left].get("owned_paths", [])
                for right_path in tasks[right].get("owned_paths", [])
                if patterns_overlap(left_path, right_path)
            ]
            if overlaps:
                errors.append(
                    f"parallel ownership overlap between {left} and {right}: {overlaps}"
                )

    required_gates = ["gate_a"]
    if stage in {"execution", "finish"}:
        required_gates.append("gate_b")
    for gate_name in required_gates:
        if not _gate_is_approved(manifest, gate_name):
            errors.append(f"{gate_name} approval evidence is incomplete")
    if stage == "finish":
        unfinished_requirements = [
            requirement.get("id")
            for requirement in requirements
            if isinstance(requirement, dict) and requirement.get("status") == "approved"
        ]
        if unfinished_requirements:
            errors.append(
                f"approved requirements remain unimplemented: {unfinished_requirements}"
            )

    return errors, warnings


def ready_tasks(manifest: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    tasks = _task_map(manifest)
    unfinished = [
        task for task in tasks.values() if task.get("status") not in DONE_STATUSES
    ]
    if not unfinished:
        return {"wave": None, "task_ids": [], "capacity": 0, "complete": True}

    current_wave = min(task.get("wave", 99) for task in unfinished)
    wave_tasks = [task for task in unfinished if task.get("wave") == current_wave]
    active = [task for task in wave_tasks if task.get("status") in ACTIVE_STATUSES]

    configured_limit = limit if limit is not None else manifest.get("max_concurrency", 3)
    concurrency = 1 if current_wave in {0, 2} else configured_limit
    capacity = max(0, concurrency - len(active))
    if capacity == 0:
        return {
            "wave": current_wave,
            "task_ids": [],
            "capacity": 0,
            "complete": False,
        }

    selected: list[dict[str, Any]] = []
    for task in sorted(wave_tasks, key=lambda item: item["id"]):
        if task.get("status") not in DISPATCHABLE_STATUSES:
            continue
        dependencies = task.get("depends_on", [])
        if any(tasks[dependency].get("status") not in DONE_STATUSES for dependency in dependencies):
            continue
        occupied = active + selected
        if any(
            patterns_overlap(candidate_path, occupied_path)
            for candidate_path in task.get("owned_paths", [])
            for other in occupied
            for occupied_path in other.get("owned_paths", [])
        ):
            continue
        selected.append(task)
        if len(selected) >= capacity:
            break

    return {
        "wave": current_wave,
        "task_ids": [task["id"] for task in selected],
        "capacity": capacity,
        "complete": False,
    }


def drift_report(
    manifest: dict[str, Any], task_id: str, changed_files: Iterable[str]
) -> dict[str, Any]:
    tasks = _task_map(manifest)
    if task_id not in tasks:
        raise ManifestError(f"unknown task id: {task_id}")
    task = tasks[task_id]
    allowed = task.get("owned_paths", []) + task.get("allowed_auxiliary_paths", [])
    protected = manifest.get("protected_paths", [])
    changed = sorted({_normalize_path(path) for path in changed_files if path.strip()})
    outside = [
        path for path in changed if not any(path_matches(path, pattern) for pattern in allowed)
    ]
    protected_changes = [
        path
        for path in changed
        if any(path_matches(path, pattern) for pattern in protected)
        and not task.get("may_change_contracts", False)
    ]
    return {
        "task_id": task_id,
        "changed_files": changed,
        "outside_owned_paths": outside,
        "protected_path_changes": protected_changes,
        "ok": not outside and not protected_changes,
    }


def trace_markdown(manifest: dict[str, Any]) -> str:
    tasks = _task_map(manifest)
    rows = [
        "# Requirement Traceability",
        "",
        "| Requirement | Status | Tasks | Issues | PRs | Commits | Regression commands |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def escape(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    for requirement in manifest.get("requirements", []):
        requirement_id = requirement.get("id", "")
        mapped = [
            task
            for task in tasks.values()
            if requirement_id in task.get("requirement_ids", [])
        ]
        rows.append(
            "| {req} | {status} | {tasks} | {issues} | {prs} | {commits} | {commands} |".format(
                req=escape(requirement_id),
                status=escape(requirement.get("status", "")),
                tasks=escape(", ".join(task["id"] for task in mapped) or "-"),
                issues=escape(", ".join(str(task.get("issue_id") or "-") for task in mapped) or "-"),
                prs=escape(", ".join(str(task.get("pr_id") or "-") for task in mapped) or "-"),
                commits=escape(
                    ", ".join(str(task.get("squash_commit") or "-") for task in mapped)
                    or "-"
                ),
                commands=escape(
                    "; ".join(
                        command
                        for task in mapped
                        for command in task.get("regression_commands", [])
                    )
                    or "-"
                ),
            )
        )
    rows.extend(["", f"Generated for `{manifest.get('run_id', 'unknown')}`.", ""])
    return "\n".join(rows)


def _print_validation(errors: list[str], warnings: list[str]) -> None:
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print(f"OK: manifest valid ({len(warnings)} warning(s))")


def _read_changed_files(args: argparse.Namespace) -> list[str]:
    changed = list(args.changed_file or [])
    if args.changed_files:
        changed.extend(
            Path(args.changed_files).read_text(encoding="utf-8").splitlines()
        )
    if not changed and not sys.stdin.isatty():
        changed.extend(sys.stdin.read().splitlines())
    if not any(path.strip() for path in changed):
        raise ManifestError("no changed files provided")
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wishctl",
        description="Validate and inspect wish-builder execution manifests."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a manifest")
    validate_parser.add_argument("manifest")
    validate_parser.add_argument(
        "--stage", choices=("planning", "execution", "finish"), default="planning"
    )

    ready_parser = subparsers.add_parser("ready", help="print dispatchable task IDs")
    ready_parser.add_argument("manifest")
    ready_parser.add_argument("--limit", type=int)

    drift_parser = subparsers.add_parser("drift", help="check changed files against ownership")
    drift_parser.add_argument("manifest")
    drift_parser.add_argument("--task", required=True)
    drift_parser.add_argument("--changed-file", action="append")
    drift_parser.add_argument("--changed-files", help="newline-delimited file list")

    trace_parser = subparsers.add_parser("trace", help="render requirement trace Markdown")
    trace_parser.add_argument("manifest")
    trace_parser.add_argument("--output")

    hash_parser = subparsers.add_parser("hash", help="print a gate artifact SHA-256")
    hash_parser.add_argument("artifact")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "hash":
            digest = hashlib.sha256(Path(args.artifact).read_bytes()).hexdigest()
            print(f"sha256:{digest}")
            return 0
        manifest = load_manifest(args.manifest)
        if args.command == "validate":
            errors, warnings = validate_manifest(manifest, args.stage)
            _print_validation(errors, warnings)
            return 1 if errors else 0
        if args.command == "ready":
            errors, warnings = validate_manifest(manifest, "execution")
            if errors:
                _print_validation(errors, warnings)
                return 1
            if args.limit is not None and args.limit < 1:
                raise ManifestError("--limit must be a positive integer")
            print(json.dumps(ready_tasks(manifest, args.limit), indent=2))
            return 0
        if args.command == "drift":
            report = drift_report(manifest, args.task, _read_changed_files(args))
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        if args.command == "trace":
            output = trace_markdown(manifest)
            if args.output:
                Path(args.output).write_text(output, encoding="utf-8", newline="\n")
            else:
                print(output, end="")
            return 0
    except (ManifestError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
