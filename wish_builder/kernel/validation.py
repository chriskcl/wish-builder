"""Deterministic semantic validation over admitted immutable contracts."""

from __future__ import annotations

import unicodedata
from collections import deque
from dataclasses import dataclass

from wish_builder.contracts.decoder import (
    DEFAULT_DECODE_LIMITS,
    DecodeLimits,
    decode_manifest_bytes,
    decode_manifest_primitive,
)
from wish_builder.contracts.diagnostics import (
    DecodeResult,
    DiagnosticPath,
    MAX_DIAGNOSTIC_MESSAGE,
    ReasonCode,
    Severity,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
)
from wish_builder.contracts.models import (
    ExecutionManifest,
    RequirementStatus,
    Task,
    TaskStatus,
    ValidationPhase,
)


MERGED_OR_LATER_STATUSES = {
    TaskStatus.MERGED,
    TaskStatus.VERIFIED,
    TaskStatus.ARCHIVED,
}
FINISHED_STATUSES = {
    TaskStatus.VERIFIED,
    TaskStatus.ARCHIVED,
}
ACTIVE_STATUSES = {
    TaskStatus.LEASED,
    TaskStatus.DISPATCHED,
    TaskStatus.PR_OPEN,
}


# M1 treats Windows path aliases conservatively because ownership is eventually
# enforced on a Windows checkout.  A colon covers drive prefixes and alternate
# data streams (including embedded forms); reserved device names are denied as
# well.  This is deliberately a narrow active-M1 collision profile, not a claim
# to model every filesystem or Unicode normalization rule.
_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "clock$",
        "conin$",
        "conout$",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        *(f"com{index}" for index in "¹²³"),
        *(f"lpt{index}" for index in "¹²³"),
    }
)
_GLOB_TOKENS = "*?["
_INVALID_WINDOWS_COMPONENT_CHARACTERS = frozenset('<>"|')


def _unsafe_windows_component(segment: str) -> bool:
    if ":" in segment or any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or character in _INVALID_WINDOWS_COMPONENT_CHARACTERS
        for character in segment
    ):
        return True
    device_alias = segment.casefold().rstrip(" .")
    if segment.endswith((" ", ".")) or not device_alias:
        return True
    device_stem = device_alias.split(".", 1)[0]
    return (
        device_alias in _WINDOWS_DEVICE_NAMES
        or device_stem in _WINDOWS_DEVICE_NAMES
    )


def _issue(
    stage: ValidationStage,
    rule_id: str,
    path: DiagnosticPath,
    reason_code: ReasonCode | str,
    message: str,
    *,
    severity: Severity = Severity.ERROR,
    related_paths: tuple[DiagnosticPath, ...] = (),
) -> ValidationIssue:
    return ValidationIssue(
        stage=stage,
        rule_id=rule_id,
        severity=severity,
        path=path,
        reason_code=ReasonCode(reason_code),
        message=message,
        related_paths=related_paths,
    )


def _phase(value: ValidationPhase | str) -> ValidationPhase:
    if type(value) is ValidationPhase:
        return value
    if type(value) is str:
        try:
            return ValidationPhase(value)
        except ValueError as exc:
            raise ValueError("phase must be planning, execution, or finish") from exc
    raise TypeError("phase must be a ValidationPhase or its exact string value")


@dataclass(frozen=True, slots=True)
class _OwnershipPattern:
    normalized: str
    comparison_key: str
    unsafe: bool
    has_glob: bool
    static_prefix: str
    literal_components: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _OwnershipSummary:
    patterns: tuple[_OwnershipPattern, ...]
    has_invalid: bool
    matches_all: bool
    all_prefixes: tuple[str, ...]
    glob_prefixes: tuple[str, ...]
    literal_roots: tuple[tuple[str, ...], ...]


def _analyze_ownership_scope(value: str) -> _OwnershipPattern:
    """Compile one relative scope without interpreting its glob language."""

    if type(value) is not str:
        raise TypeError("ownership scope must be a string")
    slash_normalized = unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    ).replace("\\", "/")
    raw_segments = slash_normalized.split("/")
    unsafe = slash_normalized.startswith("/") or any(
        _unsafe_windows_component(segment)
        for segment in raw_segments
        if segment not in {"", ".", ".."}
    )
    segments: list[str] = []
    for segment in raw_segments:
        if not segment or segment == ".":
            continue
        if segment == "..":
            if not segments or any(token in segments[-1] for token in _GLOB_TOKENS):
                unsafe = True
                continue
            segments.pop()
            continue
        segments.append(segment)

    normalized = "/".join(segments)
    if not normalized:
        unsafe = True
    if any(_unsafe_windows_component(segment) for segment in segments):
        unsafe = True

    comparison_key = normalized.casefold()
    positions = [
        position
        for token in _GLOB_TOKENS
        if (position := comparison_key.find(token)) >= 0
    ]
    has_glob = bool(positions)
    static_prefix = (
        comparison_key[: min(positions)] if positions else comparison_key
    )
    literal_components = (
        () if has_glob else tuple(comparison_key.split("/"))
    )
    return _OwnershipPattern(
        normalized=normalized,
        comparison_key=comparison_key,
        unsafe=unsafe,
        has_glob=has_glob,
        static_prefix=static_prefix,
        literal_components=literal_components,
    )


def _canonicalize_path(value: str) -> tuple[str, bool]:
    pattern = _analyze_ownership_scope(value)
    return pattern.normalized, pattern.unsafe


def _normalize_path(value: str) -> str:
    """Return the canonical slash/dot-normalized spelling of a scope."""

    return _analyze_ownership_scope(value).normalized


def _windows_alias_path(normalized: str) -> tuple[str, bool]:
    """Compatibility helper for the active-M1 trailing-alias profile."""

    if not normalized:
        return "", False
    aliases = tuple(segment.rstrip(" .") for segment in normalized.split("/"))
    unsafe = any(
        segment.endswith((" ", ".")) or not alias
        for segment, alias in zip(normalized.split("/"), aliases)
    )
    return "/".join(aliases), unsafe


def _static_prefix(pattern: str) -> str:
    analyzed = _analyze_ownership_scope(pattern)
    return "" if analyzed.unsafe else analyzed.static_prefix


def _contains_path(prefix: str, path: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _prefixes_compatible(left: str, right: str) -> bool:
    return left.startswith(right) or right.startswith(left)


def _literal_components_overlap(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool:
    common = min(len(left), len(right))
    return left[:common] == right[:common]


def _ownership_patterns_overlap(
    left: _OwnershipPattern,
    right: _OwnershipPattern,
) -> bool:
    if left.unsafe or right.unsafe:
        return True
    if not left.comparison_key or not right.comparison_key:
        return True
    if not left.has_glob and not right.has_glob:
        return _literal_components_overlap(
            left.literal_components,
            right.literal_components,
        )
    return _prefixes_compatible(left.static_prefix, right.static_prefix)


def _patterns_overlap(left: str, right: str) -> bool:
    return _ownership_patterns_overlap(
        _analyze_ownership_scope(left),
        _analyze_ownership_scope(right),
    )


def _minimal_character_prefixes(values: list[str]) -> tuple[str, ...]:
    retained: list[str] = []
    for value in sorted(set(values)):
        if retained and value.startswith(retained[-1]):
            continue
        retained.append(value)
    return tuple(retained)


def _minimal_literal_roots(
    values: list[tuple[str, ...]],
) -> tuple[tuple[str, ...], ...]:
    retained: list[tuple[str, ...]] = []
    for value in sorted(set(values)):
        if retained and _literal_components_overlap(retained[-1], value):
            continue
        retained.append(value)
    return tuple(retained)


def _summarize_ownership(scopes: tuple[str, ...]) -> _OwnershipSummary:
    patterns = tuple(_analyze_ownership_scope(scope) for scope in scopes)
    has_invalid = any(pattern.unsafe for pattern in patterns)
    matches_all = any(
        pattern.unsafe
        or not pattern.comparison_key
        or (pattern.has_glob and not pattern.static_prefix)
        for pattern in patterns
    )
    valid = tuple(pattern for pattern in patterns if not pattern.unsafe)
    return _OwnershipSummary(
        patterns=patterns,
        has_invalid=has_invalid,
        matches_all=matches_all,
        all_prefixes=_minimal_character_prefixes(
            [pattern.static_prefix for pattern in valid]
        ),
        glob_prefixes=_minimal_character_prefixes(
            [pattern.static_prefix for pattern in valid if pattern.has_glob]
        ),
        literal_roots=_minimal_literal_roots(
            [pattern.literal_components for pattern in valid if not pattern.has_glob]
        ),
    )


def _prefix_sets_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    left_index = 0
    right_index = 0
    while left_index < len(left) and right_index < len(right):
        left_value = left[left_index]
        right_value = right[right_index]
        if _prefixes_compatible(left_value, right_value):
            return True
        if left_value < right_value:
            left_index += 1
        else:
            right_index += 1
    return False


def _literal_sets_overlap(
    left: tuple[tuple[str, ...], ...],
    right: tuple[tuple[str, ...], ...],
) -> bool:
    left_index = 0
    right_index = 0
    while left_index < len(left) and right_index < len(right):
        left_value = left[left_index]
        right_value = right[right_index]
        if _literal_components_overlap(left_value, right_value):
            return True
        if left_value < right_value:
            left_index += 1
        else:
            right_index += 1
    return False


def _ownership_summaries_overlap(
    left: _OwnershipSummary,
    right: _OwnershipSummary,
) -> bool:
    if not left.patterns or not right.patterns:
        return False
    if left.matches_all or right.matches_all:
        return True
    return (
        _prefix_sets_overlap(left.glob_prefixes, right.all_prefixes)
        or _prefix_sets_overlap(right.glob_prefixes, left.all_prefixes)
        or _literal_sets_overlap(left.literal_roots, right.literal_roots)
    )


def _precompute_reachability(
    tasks: dict[str, Task],
) -> dict[str, frozenset[str]]:
    """Build deterministic transitive dependency sets once per manifest.

    The admitted task envelope is small (64 tasks and 512 edges), so a bounded
    breadth-first walk from each sorted source is both simpler and safer than
    recursively re-walking the graph for every wave/order/ownership pair.
    Unknown references are omitted; their typed diagnostics are emitted by the
    caller and an unknown node cannot accidentally satisfy a barrier.
    """

    task_ids = tuple(sorted(tasks))
    direct = {
        task_id: tuple(
            sorted(
                dependency
                for dependency in tasks[task_id].depends_on
                if dependency in tasks
            )
        )
        for task_id in task_ids
    }
    reachability: dict[str, frozenset[str]] = {}
    for source in task_ids:
        discovered: set[str] = set()
        pending = deque(direct[source])
        while pending:
            dependency = pending.popleft()
            if dependency in discovered:
                continue
            discovered.add(dependency)
            pending.extend(direct[dependency])
        reachability[source] = frozenset(discovered)
    return reachability


def _depends_on(
    task_id: str,
    dependency_id: str,
    tasks: dict[str, Task],
    seen: frozenset[str] = frozenset(),
) -> bool:
    """Compatibility helper for callers outside the validator.

    Internal validation uses the precomputed reachability map.  Keep this
    private helper iterative for compatibility and to avoid recursion hazards
    when it is called directly with a hostile graph.
    """

    if task_id in seen or task_id not in tasks:
        return False
    pending = deque(
        dependency
        for dependency in tasks[task_id].depends_on
        if dependency not in seen and dependency in tasks
    )
    visited = set(seen)
    while pending:
        dependency = pending.popleft()
        if dependency in visited:
            continue
        if dependency == dependency_id:
            return True
        visited.add(dependency)
        pending.extend(
            parent
            for parent in tasks[dependency].depends_on
            if parent in tasks and parent not in visited
        )
    return False


def _canonical_cycle(nodes: list[str]) -> tuple[str, ...]:
    if not nodes:
        return ()
    rotations = [tuple(nodes[index:] + nodes[:index]) for index in range(len(nodes))]
    return min(rotations)


def _find_cycles(tasks: dict[str, Task]) -> tuple[tuple[str, ...], ...]:
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: set[tuple[str, ...]] = set()

    def visit(task_id: str) -> None:
        state[task_id] = 1
        stack.append(task_id)
        for dependency in sorted(tasks[task_id].depends_on):
            if dependency not in tasks:
                continue
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                cycles.add(_canonical_cycle(stack[start:]))
        stack.pop()
        state[task_id] = 2

    for task_id in sorted(tasks):
        if state.get(task_id, 0) == 0:
            visit(task_id)
    return tuple(sorted(cycles))


def _dependency_depth(tasks: dict[str, Task]) -> int:
    """Return the longest dependency chain using an iterative DAG fold."""

    task_ids = tuple(sorted(tasks))
    direct = {
        task_id: tuple(
            dependency
            for dependency in tasks[task_id].depends_on
            if dependency in tasks
        )
        for task_id in task_ids
    }
    dependents: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
    remaining = {task_id: len(direct[task_id]) for task_id in task_ids}
    depth = {task_id: 0 for task_id in task_ids}
    for task_id in task_ids:
        for dependency in direct[task_id]:
            dependents[dependency].append(task_id)

    ready = deque(task_id for task_id in task_ids if remaining[task_id] == 0)
    processed = 0
    while ready:
        dependency = ready.popleft()
        processed += 1
        for dependent in sorted(dependents[dependency]):
            depth[dependent] = max(depth[dependent], depth[dependency] + 1)
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)

    if processed != len(task_ids):
        # Validation skips the depth warning when a cycle is present.  Keep a
        # total, bounded result for direct callers instead of recursing forever.
        return max(depth.values(), default=0)
    return max(depth.values(), default=0)


def validate_manifest(
    manifest: ExecutionManifest,
    phase: ValidationPhase | str = ValidationPhase.PLANNING,
) -> ValidationReport:
    """Validate a decoded model; mappings are intentionally not accepted."""

    if type(manifest) is not ExecutionManifest:
        raise TypeError(
            "validate_manifest accepts only an admitted ExecutionManifest; "
            "use validate_manifest_shape or admit_manifest_bytes at a trust boundary"
        )
    selected_phase = _phase(phase)
    issues: list[ValidationIssue] = []
    requirements = {item.id: item for item in manifest.requirements}
    tasks = {item.id: item for item in manifest.tasks}
    # Compile dependency reachability once.  All later barrier, serial-order,
    # and ownership checks are constant-time membership tests over this map.
    reachability = _precompute_reachability(tasks)
    protected_summary = _summarize_ownership(manifest.protected_paths)
    owned_summaries = {
        task.id: _summarize_ownership(task.owned_paths)
        for task in manifest.tasks
    }
    auxiliary_summaries = {
        task.id: _summarize_ownership(task.allowed_auxiliary_paths)
        for task in manifest.tasks
    }

    issue_owners: dict[str, str] = {}
    branch_owners: dict[str, str] = {}
    pr_owners: dict[str, str] = {}

    for index, pattern in enumerate(protected_summary.patterns):
        if pattern.unsafe:
            issues.append(
                _issue(
                    ValidationStage.POLICY,
                    "manifest.ownership_scope",
                    ("protected_paths", index),
                    "invalid_ownership_scope",
                    (
                        "Protected path scopes must be safe normalized relative "
                        "patterns."
                    ),
                )
            )

    for task in manifest.tasks:
        task_path = ("tasks", task.id)
        for field_name, summary in (
            ("owned_paths", owned_summaries[task.id]),
            ("allowed_auxiliary_paths", auxiliary_summaries[task.id]),
        ):
            for index, pattern in enumerate(summary.patterns):
                if pattern.unsafe:
                    issues.append(
                        _issue(
                            ValidationStage.POLICY,
                            "manifest.ownership_scope",
                            task_path + (field_name, index),
                            "invalid_ownership_scope",
                            (
                                f"Task {task.id} {field_name} scopes must be safe "
                                "normalized relative patterns."
                            ),
                        )
                    )
        for requirement_id in sorted(set(task.requirement_ids) - set(requirements)):
            issues.append(
                _issue(
                    ValidationStage.REFERENTIAL,
                    "manifest.requirement_reference",
                    task_path + ("requirement_ids", requirement_id),
                    "unknown_requirement",
                    f"Task {task.id} references unknown requirement {requirement_id}.",
                )
            )
        for dependency_id in sorted(set(task.depends_on) - set(tasks)):
            issues.append(
                _issue(
                    ValidationStage.REFERENTIAL,
                    "manifest.dependency_reference",
                    task_path + ("depends_on", dependency_id),
                    "unknown_dependency",
                    f"Task {task.id} references unknown dependency {dependency_id}.",
                )
            )
        if task.id in task.depends_on:
            issues.append(
                _issue(
                    ValidationStage.REFERENTIAL,
                    "manifest.self_dependency",
                    task_path + ("depends_on", task.id),
                    "self_dependency",
                    f"Task {task.id} depends on itself.",
                )
            )
        for dependency_id in task.depends_on:
            dependency = tasks.get(dependency_id)
            if dependency is not None and dependency.wave > task.wave:
                issues.append(
                    _issue(
                        ValidationStage.POLICY,
                        "manifest.later_wave_dependency",
                        task_path + ("depends_on", dependency_id),
                        "later_wave_dependency",
                        f"Task {task.id} depends on later-wave task {dependency_id}.",
                    )
                )
        if task.may_change_contracts and task.wave != 0:
            issues.append(
                _issue(
                    ValidationStage.POLICY,
                    "manifest.contract_change_wave",
                    task_path + ("may_change_contracts",),
                    "contract_change_outside_wave_zero",
                    f"Task {task.id} may change contracts only in Wave 0.",
                )
            )

        if selected_phase in (ValidationPhase.EXECUTION, ValidationPhase.FINISH):
            if task.issue_id is None:
                issues.append(
                    _issue(
                        ValidationStage.LIFECYCLE,
                        "manifest.execution_issue",
                        task_path + ("issue_id",),
                        "missing_execution_identity",
                        f"Task {task.id} requires an issue identifier for execution.",
                    )
                )
            else:
                issue_key = str(task.issue_id)
                previous = issue_owners.get(issue_key)
                if previous is not None:
                    issues.append(
                        _issue(
                            ValidationStage.LIFECYCLE,
                            "manifest.unique_issue",
                            task_path + ("issue_id",),
                            "shared_issue_identity",
                            f"Tasks {previous} and {task.id} share issue {issue_key}.",
                            related_paths=(("tasks", previous, "issue_id"),),
                        )
                    )
                issue_owners[issue_key] = task.id
            if task.branch is None:
                issues.append(
                    _issue(
                        ValidationStage.LIFECYCLE,
                        "manifest.execution_branch",
                        task_path + ("branch",),
                        "missing_execution_identity",
                        f"Task {task.id} requires a branch for execution.",
                    )
                )
            else:
                if task.branch == manifest.base_branch:
                    issues.append(
                        _issue(
                            ValidationStage.LIFECYCLE,
                            "manifest.base_branch_reuse",
                            task_path + ("branch",),
                            "base_branch_reuse",
                            f"Task {task.id} must not use the manifest base branch.",
                        )
                    )
                previous = branch_owners.get(task.branch)
                if previous is not None:
                    issues.append(
                        _issue(
                            ValidationStage.LIFECYCLE,
                            "manifest.unique_branch",
                            task_path + ("branch",),
                            "shared_branch_identity",
                            f"Tasks {previous} and {task.id} share a branch.",
                            related_paths=(("tasks", previous, "branch"),),
                        )
                    )
                branch_owners[task.branch] = task.id
            if task.status is TaskStatus.PROPOSED:
                issues.append(
                    _issue(
                        ValidationStage.LIFECYCLE,
                        "manifest.proposed_after_gate_b",
                        task_path + ("status",),
                        "task_not_approved",
                        f"Task {task.id} remains proposed after Gate B.",
                    )
                )

        if task.status in ACTIVE_STATUSES and task.agent_owner is None:
            issues.append(
                _issue(
                    ValidationStage.LIFECYCLE,
                    "manifest.active_agent_owner",
                    task_path + ("agent_owner",),
                    "missing_active_owner",
                    f"Task {task.id} requires an agent owner while active.",
                )
            )
        if task.status is TaskStatus.PR_OPEN or task.status in MERGED_OR_LATER_STATUSES:
            if task.pr_id is None:
                issues.append(
                    _issue(
                        ValidationStage.LIFECYCLE,
                        "manifest.pr_identity",
                        task_path + ("pr_id",),
                        "missing_pr_identity",
                        f"Task {task.id} requires a PR identifier in status {task.status.value}.",
                    )
                )
            else:
                pr_key = str(task.pr_id)
                previous = pr_owners.get(pr_key)
                if previous is not None:
                    issues.append(
                        _issue(
                            ValidationStage.LIFECYCLE,
                            "manifest.unique_pr",
                            task_path + ("pr_id",),
                            "shared_pr_identity",
                            f"Tasks {previous} and {task.id} share PR {pr_key}.",
                            related_paths=(("tasks", previous, "pr_id"),),
                        )
                    )
                pr_owners[pr_key] = task.id
        if task.status in MERGED_OR_LATER_STATUSES and task.squash_commit is None:
            issues.append(
                _issue(
                    ValidationStage.LIFECYCLE,
                    "manifest.squash_commit",
                    task_path + ("squash_commit",),
                    "missing_merge_identity",
                    f"Task {task.id} requires a squash commit after merge.",
                )
            )
        if selected_phase is ValidationPhase.FINISH and task.status not in FINISHED_STATUSES:
            issues.append(
                _issue(
                    ValidationStage.LIFECYCLE,
                    "manifest.finished_task",
                    task_path + ("status",),
                    "unfinished_task",
                    f"Task {task.id} is not in a finished status.",
                )
            )

    cycles = _find_cycles(tasks)
    for cycle in cycles:
        first = cycle[0]
        cycle_message = "Dependency cycle: " + " -> ".join(cycle + (first,)) + "."
        if len(cycle_message) > MAX_DIAGNOSTIC_MESSAGE:
            cycle_message = (
                f"Dependency cycle contains {len(cycle)} tasks; canonical start "
                f"is {first}."
            )
        issues.append(
            _issue(
                ValidationStage.REFERENTIAL,
                "manifest.dependency_cycle",
                ("tasks", first, "depends_on"),
                "dependency_cycle",
                cycle_message,
                related_paths=tuple(("tasks", task_id, "depends_on") for task_id in cycle[1:]),
            )
        )

    dependencies_are_known = all(
        dependency_id in tasks
        for task in manifest.tasks
        for dependency_id in task.depends_on
    )
    if not cycles and dependencies_are_known:
        for wave in (1, 2):
            previous_wave = tuple(
                task for task in manifest.tasks if task.wave == wave - 1
            )
            current_wave = tuple(task for task in manifest.tasks if task.wave == wave)
            for task in current_wave:
                missing = tuple(
                    predecessor.id
                    for predecessor in previous_wave
                    if predecessor.id not in reachability.get(task.id, frozenset())
                )
                if previous_wave and not missing:
                    continue
                issues.append(
                    _issue(
                        ValidationStage.POLICY,
                        "manifest.wave_barrier",
                        ("tasks", task.id, "depends_on"),
                        "wave_barrier_bypass",
                        (
                            f"Wave {wave} task {task.id} must transitively depend on "
                            f"every Wave {wave - 1} task."
                        ),
                        related_paths=tuple(
                            ("tasks", predecessor_id, "wave")
                            for predecessor_id in missing
                        ),
                    )
                )

    covered = {
        requirement_id
        for task in manifest.tasks
        for requirement_id in task.requirement_ids
    }
    for requirement in manifest.requirements:
        if (
            requirement.status
            in (RequirementStatus.APPROVED, RequirementStatus.IMPLEMENTED)
            and requirement.id not in covered
        ):
            issues.append(
                _issue(
                    ValidationStage.REFERENTIAL,
                    "manifest.requirement_coverage",
                    ("requirements", requirement.id),
                    "uncovered_requirement",
                    f"Active requirement {requirement.id} has no task.",
                )
            )

    if not cycles:
        depth = _dependency_depth(tasks)
        if depth > 4:
            issues.append(
                _issue(
                    ValidationStage.POLICY,
                    "manifest.dependency_depth",
                    ("tasks",),
                    "deep_dependency_graph",
                    f"Dependency depth is {depth}; explain why it exceeds four.",
                    severity=Severity.WARNING,
                )
            )

    for wave in (0, 2):
        serial = [task.id for task in manifest.tasks if task.wave == wave]
        for index, left in enumerate(serial):
            for right in serial[index + 1 :]:
                if (
                    right not in reachability.get(left, frozenset())
                    and left not in reachability.get(right, frozenset())
                ):
                    issues.append(
                        _issue(
                            ValidationStage.POLICY,
                            "manifest.serial_wave_order",
                            ("tasks", left, "wave"),
                            "unordered_serial_wave",
                            f"Wave {wave} tasks {left} and {right} are unordered.",
                            related_paths=(("tasks", right, "wave"),),
                        )
                    )

    parallel = [task for task in manifest.tasks if task.wave == 1]
    for index, left in enumerate(parallel):
        for right in parallel[index + 1 :]:
            if (
                right.id in reachability.get(left.id, frozenset())
                or left.id in reachability.get(right.id, frozenset())
            ):
                continue
            left_summary = owned_summaries[left.id]
            right_summary = owned_summaries[right.id]
            if left_summary.has_invalid or right_summary.has_invalid:
                continue
            if _ownership_summaries_overlap(left_summary, right_summary):
                issues.append(
                    _issue(
                        ValidationStage.POLICY,
                        "manifest.parallel_ownership",
                        ("tasks", left.id, "owned_paths"),
                        "parallel_ownership_conflict",
                        f"Parallel tasks {left.id} and {right.id} have overlapping ownership.",
                        related_paths=(("tasks", right.id, "owned_paths"),),
                    )
                )

    if manifest.approvals.gate_a is None:
        issues.append(
            _issue(
                ValidationStage.LIFECYCLE,
                "manifest.gate_a_approval",
                ("approved", "gate_a"),
                "gate_approval_missing",
                "Gate A approval evidence is incomplete.",
            )
        )
    if (
        selected_phase in (ValidationPhase.EXECUTION, ValidationPhase.FINISH)
        and manifest.approvals.gate_b is None
    ):
        issues.append(
            _issue(
                ValidationStage.LIFECYCLE,
                "manifest.gate_b_approval",
                ("approved", "gate_b"),
                "gate_approval_missing",
                "Gate B approval evidence is incomplete.",
            )
        )
    if selected_phase is ValidationPhase.FINISH:
        for requirement in manifest.requirements:
            if requirement.status is RequirementStatus.APPROVED:
                issues.append(
                    _issue(
                        ValidationStage.LIFECYCLE,
                        "manifest.implemented_requirement",
                        ("requirements", requirement.id, "status"),
                        "unimplemented_requirement",
                        f"Requirement {requirement.id} remains approved, not implemented.",
                    )
                )

    return ValidationReport(tuple(issues))


def validate_manifest_shape(
    value: object,
    phase: ValidationPhase | str = ValidationPhase.PLANNING,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> ValidationReport:
    """Return typed diagnostics for every JSON-compatible Python shape."""

    decoded = decode_manifest_primitive(value, limits=limits)
    if not decoded.ok:
        return decoded.report
    assert decoded.value is not None
    return validate_manifest(decoded.value, phase)


def validate_manifest_bytes(
    raw: bytes,
    phase: ValidationPhase | str = ValidationPhase.PLANNING,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> ValidationReport:
    decoded = decode_manifest_bytes(raw, limits=limits)
    if not decoded.ok:
        return decoded.report
    assert decoded.value is not None
    return validate_manifest(decoded.value, phase)


def admit_manifest_primitive(
    value: object,
    phase: ValidationPhase | str = ValidationPhase.PLANNING,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[ExecutionManifest]:
    decoded = decode_manifest_primitive(value, limits=limits)
    if not decoded.ok:
        return decoded
    assert decoded.value is not None
    report = validate_manifest(decoded.value, phase)
    return DecodeResult(decoded.value if report.ok else None, report)


def admit_manifest_bytes(
    raw: bytes,
    phase: ValidationPhase | str = ValidationPhase.PLANNING,
    *,
    limits: DecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodeResult[ExecutionManifest]:
    decoded = decode_manifest_bytes(raw, limits=limits)
    if not decoded.ok:
        return decoded
    assert decoded.value is not None
    report = validate_manifest(decoded.value, phase)
    return DecodeResult(
        decoded.value if report.ok else None,
        report,
        decoded.source_sha256,
    )


def diagnostics_bytes(report: ValidationReport) -> bytes:
    if type(report) is not ValidationReport:
        raise TypeError("report must be a ValidationReport")
    return report.to_json_bytes()


def diagnostics_sha256(report: ValidationReport) -> str:
    if type(report) is not ValidationReport:
        raise TypeError("report must be a ValidationReport")
    return report.sha256()


def render_diagnostics(report: ValidationReport) -> str:
    if type(report) is not ValidationReport:
        raise TypeError("report must be a ValidationReport")
    return report.render_text()
