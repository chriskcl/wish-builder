"""Immutable, rebuildable scheduling index for a validated Gate-B task graph."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from wish_builder.contracts.execution import (
    ExecutionManifestModel,
    is_execution_manifest_model,
)
from wish_builder.contracts.manifest_v2 import ExecutionManifestV2
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import RuntimeState
from wish_builder.contracts.serialization import canonical_json_bytes

from .dag import TaskDag
from .state import KernelSnapshot

GRAPH_INDEX_VERSION = 1
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


class GraphIndexError(ValueError):
    """A derived graph index does not match its authoritative inputs."""


@dataclass(frozen=True, slots=True)
class GraphIndexNode:
    task_id: str
    wave: int
    topological_position: int
    dependencies: tuple[str, ...]
    dependents: tuple[str, ...]
    ownership_conflicts: tuple[str, ...]
    remaining_dependencies: int

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id:
            raise ValueError("task_id must be non-empty")
        if type(self.wave) is not int or self.wave < 0:
            raise ValueError("wave must be a non-negative integer")
        if type(self.topological_position) is not int or self.topological_position < 0:
            raise ValueError("topological_position must be non-negative")
        for name in ("dependencies", "dependents", "ownership_conflicts"):
            values = getattr(self, name)
            if type(values) is not tuple or not all(
                type(item) is str for item in values
            ):
                raise TypeError(f"{name} must be a tuple of strings")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
        if type(
            self.remaining_dependencies
        ) is not int or not 0 <= self.remaining_dependencies <= len(self.dependencies):
            raise ValueError("remaining_dependencies is outside the dependency count")

    def to_primitive(self) -> dict[str, object]:
        return {
            "dependencies": list(self.dependencies),
            "dependents": list(self.dependents),
            "ownership_conflicts": list(self.ownership_conflicts),
            "remaining_dependencies": self.remaining_dependencies,
            "task_id": self.task_id,
            "topological_position": self.topological_position,
            "wave": self.wave,
        }


@dataclass(frozen=True, slots=True)
class GraphIndex:
    """Static graph data plus replay-derived dependency counters and ready set."""

    schema_version: int
    run_id: str
    manifest_hash: str
    graph_hash: str
    nodes: tuple[GraphIndexNode, ...]
    topological_order: tuple[str, ...]
    edge_count: int
    max_concurrency: int
    task_states: tuple[tuple[str, RuntimeState], ...]
    phase: RuntimeState
    status: RuntimeState
    active_task_ids: tuple[str, ...]
    ready_set: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != GRAPH_INDEX_VERSION:
            raise ValueError(f"schema_version must be {GRAPH_INDEX_VERSION}")
        if type(self.run_id) is not str or not self.run_id:
            raise ValueError("run_id must be non-empty")
        for value, name in (
            (self.manifest_hash, "manifest_hash"),
            (self.graph_hash, "graph_hash"),
        ):
            if type(value) is not str or not HASH_RE.fullmatch(value):
                raise ValueError(f"{name} must be a full sha256 reference")
        if type(self.nodes) is not tuple or not all(
            type(node) is GraphIndexNode for node in self.nodes
        ):
            raise TypeError("nodes must be a tuple of GraphIndexNode values")
        if type(self.topological_order) is not tuple or not all(
            type(task_id) is str for task_id in self.topological_order
        ):
            raise TypeError("topological_order must be a tuple of strings")
        node_ids = tuple(node.task_id for node in self.nodes)
        if node_ids != self.topological_order or len(node_ids) != len(set(node_ids)):
            raise ValueError("nodes must exactly follow a unique topological order")
        if tuple(node.topological_position for node in self.nodes) != tuple(
            range(len(self.nodes))
        ):
            raise ValueError("node positions must match topological order")
        known = set(node_ids)
        for node in self.nodes:
            if (
                not set(node.dependencies + node.dependents + node.ownership_conflicts)
                <= known
            ):
                raise ValueError("graph adjacency references an unknown task")
        if type(self.edge_count) is not int or self.edge_count < 0:
            raise ValueError("edge_count must be non-negative")
        if self.edge_count != sum(len(node.dependencies) for node in self.nodes):
            raise ValueError("edge_count does not match the graph")
        if type(self.max_concurrency) is not int or self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        if type(self.task_states) is not tuple or not all(
            type(item) is tuple
            and len(item) == 2
            and type(item[0]) is str
            and type(item[1]) is RuntimeState
            for item in self.task_states
        ):
            raise TypeError("task_states must contain task and RuntimeState pairs")
        if tuple(task_id for task_id, _ in self.task_states) != self.topological_order:
            raise ValueError("task_states must exactly follow topological order")
        if (
            type(self.phase) is not RuntimeState
            or type(self.status) is not RuntimeState
        ):
            raise TypeError("phase and status must be RuntimeState values")
        for values, name in (
            (self.active_task_ids, "active_task_ids"),
            (self.ready_set, "ready_set"),
        ):
            if type(values) is not tuple or not all(
                type(item) is str for item in values
            ):
                raise TypeError(f"{name} must be a tuple of strings")
            if len(values) != len(set(values)) or not set(values) <= known:
                raise ValueError(f"{name} must contain unique known tasks")
        expected_hash = _sha256_ref(_static_graph_primitive(self))
        if self.graph_hash != expected_hash:
            raise ValueError("graph_hash does not match the static graph")

    @classmethod
    def compile(
        cls,
        manifest: ExecutionManifestModel,
        snapshot: KernelSnapshot | None = None,
    ) -> GraphIndex:
        if not is_execution_manifest_model(manifest):
            raise TypeError("manifest must be an ExecutionManifest model")
        dag = TaskDag.compile(manifest)
        if snapshot is None:
            states = (
                {task.id: RuntimeState.APPROVED for task in manifest.tasks}
                if type(manifest) is ExecutionManifestV2
                else {
                    task.id: RuntimeState(task.status.value) for task in manifest.tasks
                }
            )
            phase = RuntimeState.EXECUTING
            status = RuntimeState.RUNNING
        else:
            _validate_snapshot(manifest, snapshot, dag)
            states = snapshot.task_states()
            phase = snapshot.phase
            status = snapshot.status

        nodes = tuple(
            GraphIndexNode(
                task_id=node.task_id,
                wave=node.wave,
                topological_position=node.topological_position,
                dependencies=node.dependencies,
                dependents=node.dependents,
                ownership_conflicts=node.ownership_conflicts,
                remaining_dependencies=sum(
                    states[dependency] not in _DEPENDENCY_COMPLETE
                    for dependency in node.dependencies
                ),
            )
            for node in dag.nodes
        )
        task_states = tuple(
            (task_id, states[task_id]) for task_id in dag.topological_order
        )
        active = _active_tasks(dag.topological_order, states)
        ready = _ready_tasks(nodes, states, phase, status, active, dag.max_concurrency)
        manifest_hash = _sha256_bytes(manifest.canonical_json_bytes())
        graph_hash = _sha256_ref(
            _static_graph_fields(
                manifest.run_id,
                manifest_hash,
                nodes,
                dag.topological_order,
                dag.edge_count,
                dag.max_concurrency,
            )
        )
        return cls(
            schema_version=GRAPH_INDEX_VERSION,
            run_id=manifest.run_id,
            manifest_hash=manifest_hash,
            graph_hash=graph_hash,
            nodes=nodes,
            topological_order=dag.topological_order,
            edge_count=dag.edge_count,
            max_concurrency=dag.max_concurrency,
            task_states=task_states,
            phase=phase,
            status=status,
            active_task_ids=active,
            ready_set=ready,
        )

    @classmethod
    def rebuild(
        cls, manifest: ExecutionManifestModel, snapshot: KernelSnapshot
    ) -> GraphIndex:
        return cls.compile(manifest, snapshot)

    def advance(
        self,
        previous: KernelSnapshot,
        current: KernelSnapshot,
    ) -> GraphIndex:
        """Advance counters from one accepted journal fold without recompiling the DAG."""

        if type(previous) is not KernelSnapshot or type(current) is not KernelSnapshot:
            raise TypeError("previous and current must be KernelSnapshot values")
        if previous.run_id != self.run_id or current.run_id != self.run_id:
            raise GraphIndexError("run_mismatch")
        previous_state_pairs = tuple(
            (task.task_id, task.state) for task in previous.tasks
        )
        current_state_pairs = tuple(
            (task.task_id, task.state) for task in current.tasks
        )
        if (
            tuple(task_id for task_id, _ in previous_state_pairs)
            != self.topological_order
            or tuple(task_id for task_id, _ in current_state_pairs)
            != self.topological_order
        ):
            raise GraphIndexError("task_set_mismatch")
        if previous_state_pairs != self.task_states:
            raise GraphIndexError("previous_state_mismatch")
        if current.last_sequence != previous.last_sequence + 1:
            raise GraphIndexError("sequence_mismatch")

        state_changes = tuple(
            (position, before, after)
            for position, ((_, before), (_, after)) in enumerate(
                zip(previous_state_pairs, current_state_pairs, strict=True)
            )
            if before is not after
        )
        nodes = self.nodes
        completion_changes = tuple(
            (position, before, after)
            for position, before, after in state_changes
            if (before in _DEPENDENCY_COMPLETE) != (after in _DEPENDENCY_COMPLETE)
        )
        if completion_changes:
            positions = {
                task_id: position
                for position, task_id in enumerate(self.topological_order)
            }
            updated_nodes = list(self.nodes)
            for position, before, after in completion_changes:
                adjustment = -1 if after in _DEPENDENCY_COMPLETE else 1
                for dependent in self.nodes[position].dependents:
                    dependent_position = positions[dependent]
                    node = updated_nodes[dependent_position]
                    remaining = node.remaining_dependencies + adjustment
                    if not 0 <= remaining <= len(node.dependencies):
                        raise GraphIndexError("remaining_dependency_mismatch")
                    updated_nodes[dependent_position] = replace(
                        node,
                        remaining_dependencies=remaining,
                    )
            nodes = tuple(updated_nodes)

        if not state_changes:
            task_states = self.task_states
            active = self.active_task_ids
        else:
            task_states = current_state_pairs
            current_states = dict(current_state_pairs)
            active = _active_tasks(self.topological_order, current_states)

        if (
            not state_changes
            and current.phase is self.phase
            and current.status is self.status
        ):
            return self

        if (
            current.phase is RuntimeState.EXECUTING
            and current.status is RuntimeState.RUNNING
        ):
            current_states = dict(current_state_pairs)
            ready = _ready_tasks(
                nodes,
                current_states,
                current.phase,
                current.status,
                active,
                self.max_concurrency,
            )
        else:
            ready = ()
        return self._replace_dynamic(
            nodes=nodes,
            task_states=task_states,
            phase=current.phase,
            status=current.status,
            active_task_ids=active,
            ready_set=ready,
        )

    def _replace_dynamic(
        self,
        *,
        nodes: tuple[GraphIndexNode, ...],
        task_states: tuple[tuple[str, RuntimeState], ...],
        phase: RuntimeState,
        status: RuntimeState,
        active_task_ids: tuple[str, ...],
        ready_set: tuple[str, ...],
    ) -> GraphIndex:
        """Build from trusted derived fields without rehashing the static graph."""

        advanced = object.__new__(type(self))
        for name in (
            "schema_version",
            "run_id",
            "manifest_hash",
            "graph_hash",
            "topological_order",
            "edge_count",
            "max_concurrency",
        ):
            object.__setattr__(advanced, name, getattr(self, name))
        object.__setattr__(advanced, "nodes", nodes)
        object.__setattr__(advanced, "task_states", task_states)
        object.__setattr__(advanced, "phase", phase)
        object.__setattr__(advanced, "status", status)
        object.__setattr__(advanced, "active_task_ids", active_task_ids)
        object.__setattr__(advanced, "ready_set", ready_set)
        return advanced

    def verify(
        self, manifest: ExecutionManifestModel, snapshot: KernelSnapshot
    ) -> bool:
        return self == type(self).rebuild(manifest, snapshot)

    def require_match(
        self, manifest: ExecutionManifestModel, snapshot: KernelSnapshot
    ) -> None:
        if not self.verify(manifest, snapshot):
            raise GraphIndexError("index_mismatch")

    def node(self, task_id: str) -> GraphIndexNode:
        for node in self.nodes:
            if node.task_id == task_id:
                return node
        raise KeyError(task_id)

    def to_primitive(self) -> dict[str, object]:
        return {
            "active_task_ids": list(self.active_task_ids),
            "edge_count": self.edge_count,
            "graph_hash": self.graph_hash,
            "manifest_hash": self.manifest_hash,
            "max_concurrency": self.max_concurrency,
            "nodes": [node.to_primitive() for node in self.nodes],
            "phase": self.phase.value,
            "ready_set": list(self.ready_set),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "task_states": [
                {"state": state.value, "task_id": task_id}
                for task_id, state in self.task_states
            ],
            "topological_order": list(self.topological_order),
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    @property
    def digest(self) -> str:
        return _sha256_bytes(self.canonical_json_bytes())

    @property
    def index_hash(self) -> str:
        """Checkpoint-facing spelling for the canonical derived-index digest."""

        return self.digest

    @property
    def ready_tasks(self) -> tuple[str, ...]:
        """Scheduler-facing spelling retained by replay consumers."""

        return self.ready_set

    def remaining_for(self, task_id: str) -> int:
        return self.node(task_id).remaining_dependencies

    @classmethod
    def from_primitive(cls, value: object) -> GraphIndex:
        """Strictly decode a checkpoint copy; callers must still verify it."""

        if type(value) is not dict:
            raise GraphIndexError("index_schema")
        expected = {
            "active_task_ids",
            "edge_count",
            "graph_hash",
            "manifest_hash",
            "max_concurrency",
            "nodes",
            "phase",
            "ready_set",
            "run_id",
            "schema_version",
            "status",
            "task_states",
            "topological_order",
        }
        if set(value) != expected or value.get("schema_version") != GRAPH_INDEX_VERSION:
            raise GraphIndexError("index_schema")

        def strings(name: str) -> tuple[str, ...]:
            raw = value[name]
            if type(raw) is not list or not all(type(item) is str for item in raw):
                raise GraphIndexError("index_schema")
            return tuple(raw)

        raw_nodes = value["nodes"]
        if type(raw_nodes) is not list:
            raise GraphIndexError("index_schema")
        node_keys = {
            "dependencies",
            "dependents",
            "ownership_conflicts",
            "remaining_dependencies",
            "task_id",
            "topological_position",
            "wave",
        }
        nodes: list[GraphIndexNode] = []
        for raw in raw_nodes:
            if type(raw) is not dict or set(raw) != node_keys:
                raise GraphIndexError("index_schema")
            try:
                nodes.append(
                    GraphIndexNode(
                        task_id=raw["task_id"],
                        wave=raw["wave"],
                        topological_position=raw["topological_position"],
                        dependencies=_strict_string_list(raw["dependencies"]),
                        dependents=_strict_string_list(raw["dependents"]),
                        ownership_conflicts=_strict_string_list(
                            raw["ownership_conflicts"]
                        ),
                        remaining_dependencies=raw["remaining_dependencies"],
                    )
                )
            except (TypeError, ValueError) as exc:
                raise GraphIndexError("index_schema") from exc

        raw_states = value["task_states"]
        if type(raw_states) is not list:
            raise GraphIndexError("index_schema")
        states: list[tuple[str, RuntimeState]] = []
        for raw in raw_states:
            if type(raw) is not dict or set(raw) != {"state", "task_id"}:
                raise GraphIndexError("index_schema")
            if type(raw["task_id"]) is not str or type(raw["state"]) is not str:
                raise GraphIndexError("index_schema")
            try:
                states.append((raw["task_id"], RuntimeState(raw["state"])))
            except ValueError as exc:
                raise GraphIndexError("index_schema") from exc
        try:
            return cls(
                schema_version=value["schema_version"],
                run_id=value["run_id"],
                manifest_hash=value["manifest_hash"],
                graph_hash=value["graph_hash"],
                nodes=tuple(nodes),
                topological_order=strings("topological_order"),
                edge_count=value["edge_count"],
                max_concurrency=value["max_concurrency"],
                task_states=tuple(states),
                phase=RuntimeState(value["phase"]),
                status=RuntimeState(value["status"]),
                active_task_ids=strings("active_task_ids"),
                ready_set=strings("ready_set"),
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, GraphIndexError):
                raise
            raise GraphIndexError("index_schema") from exc


def _validate_snapshot(
    manifest: ExecutionManifestModel,
    snapshot: KernelSnapshot,
    dag: TaskDag,
) -> None:
    if type(snapshot) is not KernelSnapshot:
        raise TypeError("snapshot must be a KernelSnapshot")
    if snapshot.run_id != manifest.run_id:
        raise GraphIndexError("run_mismatch")
    if tuple(snapshot.task_states()) != dag.topological_order:
        raise GraphIndexError("task_set_mismatch")


def _active_tasks(
    order: tuple[str, ...], states: dict[str, RuntimeState]
) -> tuple[str, ...]:
    return tuple(task_id for task_id in order if states[task_id] in _ACTIVE_TASK_STATES)


def _ready_tasks(
    nodes: tuple[GraphIndexNode, ...],
    states: dict[str, RuntimeState],
    phase: RuntimeState,
    status: RuntimeState,
    active: tuple[str, ...],
    max_concurrency: int,
) -> tuple[str, ...]:
    if phase is not RuntimeState.EXECUTING or status is not RuntimeState.RUNNING:
        return ()
    capacity = max(0, max_concurrency - len(active))
    active_set = set(active)
    selected: list[str] = []
    for node in nodes:
        if len(selected) >= capacity:
            break
        if states[node.task_id] not in {RuntimeState.APPROVED, RuntimeState.READY}:
            continue
        if node.remaining_dependencies:
            continue
        conflicts = set(node.ownership_conflicts)
        if conflicts.intersection(active_set) or conflicts.intersection(selected):
            continue
        selected.append(node.task_id)
    return tuple(selected)


def _static_graph_primitive(index: GraphIndex) -> dict[str, object]:
    return _static_graph_fields(
        index.run_id,
        index.manifest_hash,
        index.nodes,
        index.topological_order,
        index.edge_count,
        index.max_concurrency,
    )


def _static_graph_fields(
    run_id: str,
    manifest_hash: str,
    nodes: tuple[GraphIndexNode, ...],
    topological_order: tuple[str, ...],
    edge_count: int,
    max_concurrency: int,
) -> dict[str, object]:
    return {
        "edge_count": edge_count,
        "manifest_hash": manifest_hash,
        "max_concurrency": max_concurrency,
        "nodes": [
            {
                "dependencies": list(node.dependencies),
                "dependents": list(node.dependents),
                "ownership_conflicts": list(node.ownership_conflicts),
                "task_id": node.task_id,
                "topological_position": node.topological_position,
                "wave": node.wave,
            }
            for node in nodes
        ],
        "run_id": run_id,
        "schema_version": GRAPH_INDEX_VERSION,
        "topological_order": list(topological_order),
    }


def _sha256_ref(value: object) -> str:
    return _sha256_bytes(canonical_json_bytes(value))


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _strict_string_list(value: object) -> tuple[str, ...]:
    if type(value) is not list or not all(type(item) is str for item in value):
        raise GraphIndexError("index_schema")
    return tuple(value)


GraphIndexMismatch = GraphIndexError


__all__ = [
    "GRAPH_INDEX_VERSION",
    "GraphIndex",
    "GraphIndexError",
    "GraphIndexMismatch",
    "GraphIndexNode",
]
