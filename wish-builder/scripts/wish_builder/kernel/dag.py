"""Immutable task DAG used by the active-M1 state kernel."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from wish_builder.contracts.execution import (
    ExecutionManifestModel,
    is_execution_manifest_model,
)
from wish_builder.contracts.manifest_v2 import ManifestTask
from wish_builder.contracts.models import MAX_TASKS, Task
from wish_builder.contracts.runtime import RuntimeState

from .validation import _summarize_writable_set, _writable_sets_overlap

MAX_GRAPH_TASKS = MAX_TASKS
MAX_GRAPH_EDGES = 4_096
_DEPENDENCY_COMPLETE = frozenset({RuntimeState.VERIFIED, RuntimeState.ARCHIVED})
_ACTIVE_TASK_STATES = frozenset(
    {
        RuntimeState.LEASED,
        RuntimeState.DISPATCHED,
        RuntimeState.PR_OPEN,
        RuntimeState.STAGED,
        RuntimeState.PROMOTED,
    }
)


class DagError(ValueError):
    def __init__(self, reason: str, task_ids: Iterable[str] = ()) -> None:
        self.reason = reason
        self.task_ids = tuple(sorted(task_ids))
        detail = "" if not self.task_ids else ": " + ", ".join(self.task_ids)
        super().__init__(reason + detail)


@dataclass(frozen=True, slots=True)
class DagNode:
    task_id: str
    wave: int
    dependencies: tuple[str, ...]
    dependents: tuple[str, ...]
    ownership_conflicts: tuple[str, ...]
    topological_position: int


@dataclass(frozen=True, slots=True)
class TaskDag:
    nodes: tuple[DagNode, ...]
    topological_order: tuple[str, ...]
    edge_count: int
    max_concurrency: int

    @classmethod
    def compile(cls, manifest: ExecutionManifestModel) -> TaskDag:
        if not is_execution_manifest_model(manifest):
            raise TypeError("manifest must be an ExecutionManifest model")
        tasks = manifest.tasks
        if not tasks or len(tasks) > MAX_GRAPH_TASKS:
            raise DagError("task_limit_exceeded")
        by_id = {task.id: task for task in tasks}
        missing: set[str] = set()
        self_dependencies: set[str] = set()
        edge_count = 0
        reverse: dict[str, list[str]] = {task.id: [] for task in tasks}
        remaining: dict[str, int] = {}
        for task in tasks:
            edge_count += len(task.depends_on)
            remaining[task.id] = len(task.depends_on)
            for dependency in task.depends_on:
                if dependency == task.id:
                    self_dependencies.add(task.id)
                elif dependency not in by_id:
                    missing.add(dependency)
                else:
                    reverse[dependency].append(task.id)
        if edge_count > MAX_GRAPH_EDGES:
            raise DagError("edge_limit_exceeded")
        if self_dependencies:
            raise DagError("self_dependency", self_dependencies)
        if missing:
            raise DagError("missing_dependency", missing)

        ready = [task_id for task_id, count in remaining.items() if count == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            task_id = heapq.heappop(ready)
            order.append(task_id)
            for dependent in sorted(reverse[task_id]):
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    heapq.heappush(ready, dependent)
        if len(order) != len(tasks):
            cyclic = (task_id for task_id, count in remaining.items() if count > 0)
            raise DagError("dependency_cycle", cyclic)

        positions = {task_id: index for index, task_id in enumerate(order)}
        conflicts: dict[str, set[str]] = {task.id: set() for task in tasks}
        summaries = {task.id: _summarize_writable_set(task) for task in tasks}
        descendants = _descendant_sets(tuple(tasks), reverse)
        for index, left in enumerate(tasks):
            for right in tasks[index + 1 :]:
                if right.id in descendants[left.id] or left.id in descendants[right.id]:
                    continue
                if (
                    _writable_sets_overlap(summaries[left.id], summaries[right.id])
                    is not None
                ):
                    conflicts[left.id].add(right.id)
                    conflicts[right.id].add(left.id)

        nodes = tuple(
            DagNode(
                task_id=task_id,
                wave=by_id[task_id].wave,
                dependencies=by_id[task_id].depends_on,
                dependents=tuple(sorted(reverse[task_id])),
                ownership_conflicts=tuple(sorted(conflicts[task_id])),
                topological_position=positions[task_id],
            )
            for task_id in order
        )
        return cls(nodes, tuple(order), edge_count, manifest.max_concurrency)

    def node(self, task_id: str) -> DagNode:
        for node in self.nodes:
            if node.task_id == task_id:
                return node
        raise KeyError(task_id)

    def ready(
        self,
        states: Mapping[str, RuntimeState],
        *,
        active_task_ids: Iterable[str] = (),
    ) -> tuple[str, ...]:
        if not isinstance(states, Mapping):
            raise TypeError("states must be a mapping")
        if set(states) != set(self.topological_order):
            raise DagError("state_task_set_mismatch")
        if not all(type(state) is RuntimeState for state in states.values()):
            raise TypeError("states must contain RuntimeState values")
        active = set(active_task_ids)
        unknown_active = active.difference(self.topological_order)
        if unknown_active:
            raise DagError("unknown_active_task", unknown_active)
        capacity = max(0, self.max_concurrency - len(active))
        selected: list[str] = []
        for node in self.nodes:
            if len(selected) >= capacity:
                break
            if states[node.task_id] not in {RuntimeState.APPROVED, RuntimeState.READY}:
                continue
            if any(
                states[dependency] not in _DEPENDENCY_COMPLETE
                for dependency in node.dependencies
            ):
                continue
            conflicts = set(node.ownership_conflicts)
            if conflicts.intersection(active) or conflicts.intersection(selected):
                continue
            selected.append(node.task_id)
        return tuple(selected)

    def active_task_ids(self, states: Mapping[str, RuntimeState]) -> tuple[str, ...]:
        return tuple(
            task_id
            for task_id in self.topological_order
            if states[task_id] in _ACTIVE_TASK_STATES
        )

    def blocked_by(
        self, task_id: str, states: Mapping[str, RuntimeState]
    ) -> tuple[str, ...]:
        self.node(task_id)
        blockers: set[str] = set()
        pending = list(self.node(task_id).dependencies)
        while pending:
            dependency = pending.pop()
            if dependency in blockers:
                continue
            if states[dependency] not in _DEPENDENCY_COMPLETE:
                blockers.add(dependency)
            pending.extend(self.node(dependency).dependencies)
        return tuple(sorted(blockers, key=self.topological_order.index))

    def descendants(self, task_id: str) -> tuple[str, ...]:
        self.node(task_id)
        found: set[str] = set()
        pending = list(self.node(task_id).dependents)
        while pending:
            candidate = pending.pop()
            if candidate in found:
                continue
            found.add(candidate)
            pending.extend(self.node(candidate).dependents)
        return tuple(sorted(found, key=self.topological_order.index))


def _descendant_sets(
    tasks: tuple[Task | ManifestTask, ...], reverse: Mapping[str, list[str]]
) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    for task in tasks:
        found: set[str] = set()
        pending = list(reverse[task.id])
        while pending:
            candidate = pending.pop()
            if candidate in found:
                continue
            found.add(candidate)
            pending.extend(reverse[candidate])
        result[task.id] = frozenset(found)
    return result


__all__ = [
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_TASKS",
    "DagError",
    "DagNode",
    "TaskDag",
]
