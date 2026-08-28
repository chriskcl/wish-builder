from __future__ import annotations

import copy
import ctypes
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.kernel.test_validation import valid_manifest
from wish_builder.contracts import DEFAULT_DECODE_LIMITS, decode_manifest_primitive
from wish_builder.contracts.runtime import (
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RecoveryPayload,
    RuntimeReasonCode,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.graph_index import GraphIndex, GraphIndexError, GraphIndexNode
from wish_builder.kernel.state import KernelSnapshot, apply_journal_event
from wish_builder.services import checkpoints as checkpoints_module
from wish_builder.services import replay as replay_module
from wish_builder.services.checkpoints import (
    CheckpointFault,
    CheckpointFaultCode,
    CheckpointLoadResult,
    CheckpointLoadStatus,
    CheckpointPersistenceFault,
    CheckpointPointer,
    CheckpointPolicy,
    CheckpointStore,
    JournalPosition,
)
from wish_builder.services.replay import (
    DerivedDataFault,
    QuarantinedTail,
    ReplayFault,
    ReplayFaultCode,
    ReplayResult,
    ReplayStatus,
    replay_journal,
)

RUN_ID = "WISH-001"
EPOCH = 1
NOW = "2026-08-18T08:00:00Z"
GENESIS_HASH = "sha256:" + "0" * 64


def admitted_manifest():
    decoded = decode_manifest_primitive(valid_manifest())
    if not decoded.ok or decoded.value is None:
        raise AssertionError(decoded.report.render_text())
    return decoded.value


def transition_event(
    sequence: int,
    previous_hash: str,
    event_type: JournalEventType,
    subject: TransitionSubject,
    from_state: RuntimeState,
    to_state: RuntimeState,
    *,
    task_id: str | None = None,
    reason_code: RuntimeReasonCode | None = None,
    run_id: str = RUN_ID,
    coordinator_epoch: int = EPOCH,
) -> JournalEvent:
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-{sequence:06d}",
        event_type=event_type,
        identity=ExecutionIdentity(run_id, coordinator_epoch, task_id),
        actor_type=ActorType.SYSTEM,
        actor_id="replay-test",
        recorded_at=NOW,
        previous_event_hash=previous_hash,
        payload=TransitionPayload(subject, from_state, to_state),
        reason_code=reason_code,
    )


def graph_freeze_events() -> list[JournalEvent]:
    steps = (
        (JournalEventType.RUN_INITIALIZED, RuntimeState.NONE, RuntimeState.PREFLIGHT),
        (
            JournalEventType.PREFLIGHT_COMPLETED,
            RuntimeState.PREFLIGHT,
            RuntimeState.DISCOVERY,
        ),
        (
            JournalEventType.DISCOVERY_COMPLETED,
            RuntimeState.DISCOVERY,
            RuntimeState.GATE_A_PENDING,
        ),
        (
            JournalEventType.GATE_APPROVED,
            RuntimeState.GATE_A_PENDING,
            RuntimeState.TRELLIS_PREPARATION,
        ),
        (
            JournalEventType.TRELLIS_GRAPH_IMPORTED,
            RuntimeState.TRELLIS_PREPARATION,
            RuntimeState.GATE_B_PENDING,
        ),
        (
            JournalEventType.TASK_GRAPH_FROZEN,
            RuntimeState.GATE_B_PENDING,
            RuntimeState.EXECUTING,
        ),
    )
    events: list[JournalEvent] = []
    previous = GENESIS_HASH
    for sequence, (event_type, from_state, to_state) in enumerate(steps, 1):
        event = transition_event(
            sequence,
            previous,
            event_type,
            TransitionSubject.RUN,
            from_state,
            to_state,
        )
        events.append(event)
        previous = event.event_hash
    return events


def write_segments(root: Path, groups: list[list[JournalEvent]]) -> None:
    segments = root / "segments"
    segments.mkdir(parents=True)
    for number, events in enumerate(groups, 1):
        (segments / f"segment-{number:08d}.jsonl").write_bytes(
            b"".join(event.canonical_json_bytes() for event in events)
        )


def folded_snapshot(events: list[JournalEvent]) -> KernelSnapshot:
    manifest = admitted_manifest()
    snapshot = KernelSnapshot.initial(RUN_ID, EPOCH, TaskDag.compile(manifest))
    for event in events:
        applied = apply_journal_event(snapshot, event)
        if not applied.accepted:
            raise AssertionError(applied.reason)
        snapshot = applied.snapshot
    return snapshot


def advanced_snapshot(
    snapshot: KernelSnapshot,
    *,
    tasks: tuple | None = None,
    phase: RuntimeState | None = None,
    status: RuntimeState | None = None,
) -> KernelSnapshot:
    sequence = snapshot.last_sequence + 1
    return dataclasses.replace(
        snapshot,
        tasks=snapshot.tasks if tasks is None else tasks,
        phase=snapshot.phase if phase is None else phase,
        status=snapshot.status if status is None else status,
        last_sequence=sequence,
        last_event_id=f"EVENT-{sequence:06d}",
        last_event_hash="sha256:" + hashlib.sha256(str(sequence).encode()).hexdigest(),
    )


class StreamingReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        self.journal = self.run_root / "journal"
        self.manifest = admitted_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def replay(self, **kwargs: object):
        return replay_journal(
            self.journal,
            self.manifest,
            coordinator_epoch=EPOCH,
            **kwargs,
        )

    def reset_run_root(self) -> None:
        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        self.journal = self.run_root / "journal"

    def test_cold_replay_repairs_stale_index_and_rebuilds_graph_index(self) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events[:3], events[3:]])
        (self.journal / "index.json").write_bytes(canonical_json_bytes({"stale": True}))

        result = self.replay(
            checkpoint_policy=CheckpointPolicy(event_interval=10_000),
        )

        self.assertEqual(ReplayStatus.RECOVERED, result.status)
        self.assertEqual(6, result.snapshot.last_sequence)
        self.assertEqual(("TASK-001",), result.graph_index.ready_set)
        self.assertTrue(result.graph_index.verify(self.manifest, result.snapshot))
        self.assertFalse(result.checkpoint_used)
        self.assertTrue(any(f.source == "journal_index" for f in result.derived_faults))
        index_raw = (self.journal / "index.json").read_bytes()
        self.assertEqual(index_raw, canonical_json_bytes(json.loads(index_raw)))
        index = json.loads(index_raw)
        self.assertEqual(1, index["sealed_segment"])
        self.assertEqual(2, index["active_segment"])

    def test_only_final_frame_without_delimiter_is_quarantined(self) -> None:
        first = graph_freeze_events()[0]
        write_segments(self.journal, [[first]])
        segment = self.journal / "segments" / "segment-00000001.jsonl"
        committed = segment.read_bytes()
        torn = b'{"event_version":"1.0","sequence":2'
        with segment.open("ab") as handle:
            handle.write(torn)

        result = self.replay(repair_derived=False)

        self.assertEqual(ReplayStatus.RECOVERED, result.status)
        self.assertEqual(1, result.snapshot.last_sequence)
        self.assertIsNotNone(result.quarantined_tail)
        assert result.quarantined_tail is not None
        self.assertEqual("uncommitted_tail", result.quarantined_tail.reason)
        self.assertEqual(
            torn, (self.run_root / result.quarantined_tail.quarantine_path).read_bytes()
        )
        self.assertEqual(committed, segment.read_bytes())

    def test_complete_invalid_final_event_blocks_without_quarantine(self) -> None:
        first = graph_freeze_events()[0]
        write_segments(self.journal, [[first]])
        segment = self.journal / "segments" / "segment-00000001.jsonl"
        with segment.open("ab") as handle:
            handle.write(b"{}\n")

        result = self.replay(repair_derived=False)

        self.assertEqual(ReplayStatus.BLOCKED, result.status)
        self.assertEqual(ReplayFaultCode.EVENT_DECODE_FAILED, result.fault.code)
        self.assertIsNone(result.quarantined_tail)
        self.assertTrue(segment.read_bytes().endswith(b"{}\n"))

    def test_complete_noncanonical_event_blocks(self) -> None:
        event = graph_freeze_events()[0]
        value = json.loads(event.canonical_json_bytes())
        noncanonical = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        segments = self.journal / "segments"
        segments.mkdir(parents=True)
        (segments / "segment-00000001.jsonl").write_bytes(noncanonical)

        result = self.replay(repair_derived=False)

        self.assertEqual(ReplayStatus.BLOCKED, result.status)
        self.assertEqual(ReplayFaultCode.EVENT_NONCANONICAL, result.fault.code)

    def test_incomplete_nonfinal_segment_and_hash_break_both_block(self) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events[:1], []])
        first_segment = self.journal / "segments" / "segment-00000001.jsonl"
        with first_segment.open("ab") as handle:
            handle.write(b'{"partial":true}')
        incomplete = self.replay(repair_derived=False)
        self.assertEqual(ReplayStatus.BLOCKED, incomplete.status)
        self.assertEqual(ReplayFaultCode.MID_SEGMENT_INCOMPLETE, incomplete.fault.code)

        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        self.journal = self.run_root / "journal"
        first = events[0]
        broken = transition_event(
            2,
            "sha256:" + "f" * 64,
            JournalEventType.PREFLIGHT_COMPLETED,
            TransitionSubject.RUN,
            RuntimeState.PREFLIGHT,
            RuntimeState.DISCOVERY,
        )
        write_segments(self.journal, [[first, broken]])
        mismatch = self.replay(repair_derived=False)
        self.assertEqual(ReplayStatus.BLOCKED, mismatch.status)
        self.assertEqual(ReplayFaultCode.HASH_CHAIN_MISMATCH, mismatch.fault.code)

    def test_checkpoint_tail_replay_does_not_hide_events_after_stale_pointer(
        self,
    ) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events])
        cold = self.replay(repair_derived=False)
        store = CheckpointStore(self.run_root / "checkpoints")
        store.publish(
            self.manifest,
            cold.snapshot,
            cold.graph_index,
            cold.journal_position,
        )
        pause = transition_event(
            7,
            events[-1].event_hash,
            JournalEventType.PAUSE_REQUESTED,
            TransitionSubject.RUN,
            RuntimeState.RUNNING,
            RuntimeState.PAUSING,
            reason_code=RuntimeReasonCode.PAUSE_REQUESTED,
        )
        segment = self.journal / "segments" / "segment-00000001.jsonl"
        with segment.open("ab") as handle:
            handle.write(pause.canonical_json_bytes())

        recovered = self.replay(
            checkpoint_store=store,
            checkpoint_policy=CheckpointPolicy(event_interval=10_000),
        )

        self.assertEqual(ReplayStatus.RECOVERED, recovered.status)
        self.assertTrue(recovered.checkpoint_used)
        self.assertEqual(1, recovered.events_replayed)
        self.assertEqual(7, recovered.snapshot.last_sequence)
        self.assertEqual(RuntimeState.PAUSING, recovered.snapshot.status)

    def test_corrupt_pointer_falls_back_to_genesis_and_is_rebuilt(self) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events])
        cold = self.replay(repair_derived=False)
        store = CheckpointStore(self.run_root / "checkpoints")
        store.publish(
            self.manifest,
            cold.snapshot,
            cold.graph_index,
            cold.journal_position,
        )
        store.current.write_bytes(b'{"broken":true}\n')

        recovered = self.replay(
            checkpoint_store=store,
            checkpoint_policy=CheckpointPolicy(event_interval=10_000),
        )

        self.assertEqual(ReplayStatus.RECOVERED, recovered.status)
        self.assertFalse(recovered.checkpoint_used)
        self.assertEqual(6, recovered.events_replayed)
        self.assertTrue(any(f.source == "checkpoint" for f in recovered.derived_faults))
        loaded = store.load(self.manifest, coordinator_epoch=EPOCH)
        self.assertEqual(CheckpointLoadStatus.LOADED, loaded.status)

    def test_corrupt_checkpoint_graph_is_discarded_preserved_and_rebuilt(self) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events])
        cold = self.replay(repair_derived=False)
        store = CheckpointStore(self.run_root / "checkpoints")
        store.publish(
            self.manifest,
            cold.snapshot,
            cold.graph_index,
            cold.journal_position,
        )
        pointer = json.loads(store.current.read_bytes())
        checkpoint_path = store.root / Path(pointer["checkpoint_path"])
        record = json.loads(checkpoint_path.read_bytes())
        record["graph_index"]["ready_set"] = []
        corrupt = canonical_json_bytes(record)
        checkpoint_path.write_bytes(corrupt)
        pointer["checkpoint_sha256"] = "sha256:" + hashlib.sha256(corrupt).hexdigest()
        store.current.write_bytes(canonical_json_bytes(pointer))

        recovered = self.replay(
            checkpoint_store=store,
            checkpoint_policy=CheckpointPolicy(event_interval=10_000),
        )

        self.assertEqual(ReplayStatus.RECOVERED, recovered.status)
        self.assertFalse(recovered.checkpoint_used)
        self.assertEqual(
            CheckpointLoadStatus.LOADED,
            store.load(self.manifest, coordinator_epoch=EPOCH).status,
        )
        preserved = list((store.root / "corrupt").glob("*.json"))
        self.assertEqual(1, len(preserved))
        self.assertEqual(corrupt, preserved[0].read_bytes())

    def test_valid_checkpoint_detects_authoritative_journal_truncation(self) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events])
        cold = self.replay(repair_derived=False)
        store = CheckpointStore(self.run_root / "checkpoints")
        store.publish(
            self.manifest,
            cold.snapshot,
            cold.graph_index,
            cold.journal_position,
        )
        segment = self.journal / "segments" / "segment-00000001.jsonl"
        segment.write_bytes(events[0].canonical_json_bytes())

        result = self.replay(checkpoint_store=store, repair_derived=False)

        self.assertEqual(ReplayStatus.BLOCKED, result.status)
        self.assertEqual(ReplayFaultCode.JOURNAL_TRUNCATED, result.fault.code)

    def test_oversized_pointer_is_typed_discarded_data(self) -> None:
        store = CheckpointStore(self.run_root / "checkpoints")
        store.root.mkdir(parents=True)
        store.current.write_bytes(b"x" * (64 * 1024 + 1))

        loaded = store.load(self.manifest, coordinator_epoch=EPOCH)

        self.assertEqual(CheckpointLoadStatus.DISCARDED, loaded.status)
        self.assertEqual("pointer_invalid", loaded.fault.code.value)

    def test_control_root_drift_blocks_before_reading(self) -> None:
        write_segments(self.journal, [graph_freeze_events()])

        result = self.replay(
            repair_derived=False,
            control_root_validator=lambda: False,
        )

        self.assertEqual(ReplayStatus.BLOCKED, result.status)
        self.assertEqual(ReplayFaultCode.CONTROL_ROOT_DRIFT, result.fault.code)
        self.assertEqual(0, result.events_replayed)

    def test_graph_index_incremental_update_matches_full_rebuild(self) -> None:
        dag = TaskDag.compile(self.manifest)
        previous = KernelSnapshot.initial(RUN_ID, EPOCH, dag)
        index = GraphIndex.rebuild(self.manifest, previous)
        for event in graph_freeze_events():
            applied = apply_journal_event(previous, event)
            self.assertTrue(applied.accepted, applied.reason)
            current = applied.snapshot
            index = index.advance(previous, current)
            self.assertEqual(GraphIndex.rebuild(self.manifest, current), index)
            previous = current

        task_states = list(previous.tasks)
        task_states[0] = dataclasses.replace(
            task_states[0], state=RuntimeState.VERIFIED
        )
        current = dataclasses.replace(
            previous,
            tasks=tuple(task_states),
            last_sequence=previous.last_sequence + 1,
            last_event_id="EVENT-000007",
            last_event_hash="sha256:" + "7" * 64,
        )
        index = index.advance(previous, current)
        self.assertEqual(0, index.node("TASK-002").remaining_dependencies)
        self.assertEqual(("TASK-002", "TASK-003"), index.ready_set)
        self.assertEqual(GraphIndex.rebuild(self.manifest, current), index)

    def test_empty_journal_and_orphaned_index_recover_deterministically(self) -> None:
        empty = self.replay(repair_derived=False)

        self.assertEqual(ReplayStatus.RECOVERED, empty.status)
        self.assertEqual(0, empty.events_replayed)
        self.assertEqual(0, empty.snapshot.last_sequence)
        self.assertEqual(
            (1, 0), (empty.journal_position.segment, empty.journal_position.offset)
        )

        self.journal.mkdir(parents=True)
        stale = canonical_json_bytes({"stale": True})
        (self.journal / "index.json").write_bytes(stale)
        repaired = self.replay()

        self.assertEqual(ReplayStatus.RECOVERED, repaired.status)
        self.assertFalse((self.journal / "index.json").exists())
        quarantined = list((self.run_root / "quarantine").glob("derived-index-*.json"))
        self.assertEqual(1, len(quarantined))
        self.assertEqual(stale, quarantined[0].read_bytes())

    def test_invalid_segment_layouts_and_oversized_frame_are_typed(self) -> None:
        cases: tuple[tuple[str, callable, ReplayFaultCode], ...] = (
            (
                "unexpected_name",
                lambda: (
                    (self.journal / "segments").mkdir(parents=True),
                    (self.journal / "segments" / "segment-1.jsonl").write_bytes(b""),
                ),
                ReplayFaultCode.INVALID_LAYOUT,
            ),
            (
                "sequence_gap",
                lambda: (
                    (self.journal / "segments").mkdir(parents=True),
                    (self.journal / "segments" / "segment-00000002.jsonl").write_bytes(
                        b""
                    ),
                ),
                ReplayFaultCode.INVALID_LAYOUT,
            ),
            (
                "empty_sealed_segment",
                lambda: write_segments(self.journal, [[], []]),
                ReplayFaultCode.INVALID_LAYOUT,
            ),
            (
                "oversized_frame",
                lambda: (
                    (self.journal / "segments").mkdir(parents=True),
                    (self.journal / "segments" / "segment-00000001.jsonl").write_bytes(
                        b"x" * (DEFAULT_DECODE_LIMITS.max_bytes + 1)
                    ),
                ),
                ReplayFaultCode.FRAME_TOO_LARGE,
            ),
        )
        for name, prepare, expected in cases:
            with self.subTest(name=name):
                prepare()
                result = self.replay(repair_derived=False)
                self.assertEqual(ReplayStatus.BLOCKED, result.status)
                self.assertEqual(expected, result.fault.code)
                self.reset_run_root()

    def test_identity_sequence_epoch_and_state_faults_are_distinct(self) -> None:
        cases = (
            (
                "run",
                transition_event(
                    1,
                    GENESIS_HASH,
                    JournalEventType.RUN_INITIALIZED,
                    TransitionSubject.RUN,
                    RuntimeState.NONE,
                    RuntimeState.PREFLIGHT,
                    run_id="OTHER-001",
                ),
                ReplayFaultCode.RUN_MISMATCH,
            ),
            (
                "sequence",
                transition_event(
                    2,
                    GENESIS_HASH,
                    JournalEventType.RUN_INITIALIZED,
                    TransitionSubject.RUN,
                    RuntimeState.NONE,
                    RuntimeState.PREFLIGHT,
                ),
                ReplayFaultCode.SEQUENCE_MISMATCH,
            ),
            (
                "epoch",
                transition_event(
                    1,
                    GENESIS_HASH,
                    JournalEventType.RUN_INITIALIZED,
                    TransitionSubject.RUN,
                    RuntimeState.NONE,
                    RuntimeState.PREFLIGHT,
                    coordinator_epoch=2,
                ),
                ReplayFaultCode.STATE_REJECTED,
            ),
            (
                "state",
                transition_event(
                    1,
                    GENESIS_HASH,
                    JournalEventType.PREFLIGHT_COMPLETED,
                    TransitionSubject.RUN,
                    RuntimeState.PREFLIGHT,
                    RuntimeState.DISCOVERY,
                ),
                ReplayFaultCode.STATE_REJECTED,
            ),
        )
        for name, event, expected in cases:
            with self.subTest(name=name):
                write_segments(self.journal, [[event]])
                result = self.replay(repair_derived=False)
                self.assertEqual(ReplayStatus.BLOCKED, result.status)
                self.assertEqual(expected, result.fault.code)
                self.assertEqual(0, result.events_replayed)
                self.reset_run_root()

    def test_deep_pointer_and_pointer_identity_race_are_discarded(self) -> None:
        store = CheckpointStore(self.run_root / "checkpoints")
        store.root.mkdir(parents=True)
        deep = b'{"x":' + b"[" * 1_100 + b"0" + b"]" * 1_100 + b"}"
        store.current.write_bytes(deep)

        loaded = store.load(self.manifest, coordinator_epoch=EPOCH)

        self.assertEqual(CheckpointLoadStatus.DISCARDED, loaded.status)
        self.assertEqual(CheckpointFaultCode.POINTER_INVALID, loaded.fault.code)

        with mock.patch.object(
            checkpoints_module,
            "_is_link_or_junction",
            side_effect=FileNotFoundError("raced"),
        ):
            raced = store.load(self.manifest, coordinator_epoch=EPOCH)
        self.assertEqual(CheckpointLoadStatus.DISCARDED, raced.status)
        self.assertEqual(CheckpointFaultCode.POINTER_IO_FAILED, raced.fault.code)

    def test_deep_index_and_index_identity_race_trigger_safe_rebuild(self) -> None:
        write_segments(self.journal, [graph_freeze_events()])
        index_path = self.journal / "index.json"
        index_path.write_bytes(b'{"x":' + b"[" * 1_100 + b"0" + b"]" * 1_100 + b"}")
        deep = self.replay(repair_derived=False)

        self.assertEqual(ReplayStatus.RECOVERED, deep.status)
        self.assertTrue(any(f.source == "journal_index" for f in deep.derived_faults))

        original = replay_module._is_link_or_junction

        def race_index(path: Path) -> bool:
            if path.name == "index.json":
                raise FileNotFoundError("raced")
            return original(path)

        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            side_effect=race_index,
        ):
            raced = self.replay(repair_derived=False)
        self.assertEqual(ReplayStatus.RECOVERED, raced.status)
        self.assertTrue(any(f.source == "journal_index" for f in raced.derived_faults))


class GraphIndexContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = admitted_manifest()
        self.dag = TaskDag.compile(self.manifest)
        self.initial = KernelSnapshot.initial(RUN_ID, EPOCH, self.dag)
        self.index = GraphIndex.rebuild(self.manifest, self.initial)

    def test_node_and_index_constructor_guards_reject_invalid_derived_data(
        self,
    ) -> None:
        node_cases = (
            {"task_id": ""},
            {"wave": -1},
            {"topological_position": -1},
            {"dependencies": []},
            {"dependencies": ("TASK-001", "TASK-001")},
            {"dependents": []},
            {"dependents": ("TASK-001", "TASK-001")},
            {"ownership_conflicts": []},
            {"ownership_conflicts": ("TASK-001", "TASK-001")},
            {"remaining_dependencies": 1},
        )
        base_node = GraphIndexNode("TASK-001", 0, 0, (), (), (), 0)
        self.assertEqual("TASK-001", base_node.to_primitive()["task_id"])
        for updates in node_cases:
            with self.subTest(node=updates), self.assertRaises((TypeError, ValueError)):
                dataclasses.replace(base_node, **updates)

        bad_position = dataclasses.replace(
            self.index.nodes[0],
            topological_position=1,
        )
        bad_adjacency = dataclasses.replace(
            self.index.nodes[0],
            dependents=("UNKNOWN",),
        )
        index_cases = (
            {"schema_version": 2},
            {"run_id": ""},
            {"manifest_hash": "bad"},
            {"graph_hash": "bad"},
            {"nodes": []},
            {"topological_order": []},
            {"topological_order": tuple(reversed(self.index.topological_order))},
            {"nodes": (bad_position, *self.index.nodes[1:])},
            {"nodes": (bad_adjacency, *self.index.nodes[1:])},
            {"edge_count": -1},
            {"edge_count": self.index.edge_count + 1},
            {"max_concurrency": 0},
            {"task_states": []},
            {"task_states": tuple(reversed(self.index.task_states))},
            {"phase": "bad"},
            {"status": "bad"},
            {"active_task_ids": []},
            {"active_task_ids": ("UNKNOWN",)},
            {"ready_set": []},
            {"ready_set": ("UNKNOWN",)},
            {"graph_hash": "sha256:" + "f" * 64},
        )
        for updates in index_cases:
            with (
                self.subTest(index=updates),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(self.index, **updates)

    def test_graph_index_strict_round_trip_and_schema_rejections(self) -> None:
        primitive = self.index.to_primitive()
        decoded = GraphIndex.from_primitive(primitive)

        self.assertEqual(self.index, decoded)
        self.assertEqual(self.index.digest, self.index.index_hash)
        self.assertEqual(
            self.index.canonical_json_bytes(), canonical_json_bytes(primitive)
        )
        self.assertEqual(self.index.ready_set, self.index.ready_tasks)
        self.assertEqual(
            self.index.node("TASK-002").remaining_dependencies,
            self.index.remaining_for("TASK-002"),
        )
        with self.assertRaises(KeyError):
            self.index.node("UNKNOWN")

        def missing_top_level(value: dict[str, object]) -> None:
            value.pop("run_id")

        def missing_node_field(value: dict[str, object]) -> None:
            value["nodes"][0].pop("wave")

        mutations = (
            lambda value: value.__setitem__("schema_version", 2),
            lambda value: value.__setitem__("topological_order", "bad"),
            lambda value: value.__setitem__("nodes", "bad"),
            missing_node_field,
            lambda value: value["nodes"][0].__setitem__("dependencies", "bad"),
            lambda value: value["nodes"][0].__setitem__("remaining_dependencies", -1),
            lambda value: value.__setitem__("task_states", "bad"),
            lambda value: value["task_states"][0].pop("state"),
            lambda value: value["task_states"][0].__setitem__("task_id", 1),
            lambda value: value["task_states"][0].__setitem__("state", "bad"),
            lambda value: value.__setitem__("phase", "bad"),
            lambda value: value.__setitem__("active_task_ids", "bad"),
            missing_top_level,
        )
        with self.assertRaises(GraphIndexError):
            GraphIndex.from_primitive(None)
        for mutate in mutations:
            value = copy.deepcopy(primitive)
            mutate(value)
            with self.assertRaises(GraphIndexError):
                GraphIndex.from_primitive(value)

    def test_compile_verify_and_advance_reject_mismatched_authority(self) -> None:
        compiled = GraphIndex.compile(self.manifest)
        self.assertEqual(self.manifest.run_id, compiled.run_id)
        with self.assertRaises(TypeError):
            GraphIndex.compile(object())
        with self.assertRaises(TypeError):
            GraphIndex.compile(self.manifest, object())

        wrong_run = dataclasses.replace(self.initial, run_id="OTHER-001")
        with self.assertRaises(GraphIndexError):
            GraphIndex.rebuild(self.manifest, wrong_run)
        reordered = dataclasses.replace(
            self.initial,
            tasks=tuple(reversed(self.initial.tasks)),
        )
        with self.assertRaises(GraphIndexError):
            GraphIndex.rebuild(self.manifest, reordered)

        unchanged = advanced_snapshot(self.initial)
        self.assertIs(self.index, self.index.advance(self.initial, unchanged))
        with self.assertRaises(TypeError):
            self.index.advance(object(), unchanged)
        with self.assertRaises(GraphIndexError):
            self.index.advance(self.initial, wrong_run)
        with self.assertRaises(GraphIndexError):
            self.index.advance(self.initial, reordered)
        with self.assertRaises(GraphIndexError):
            self.index.advance(self.initial, self.initial)

        changed_task = dataclasses.replace(
            self.initial.tasks[0],
            state=RuntimeState.APPROVED,
        )
        changed_previous = dataclasses.replace(
            self.initial,
            tasks=(changed_task, *self.initial.tasks[1:]),
        )
        with self.assertRaises(GraphIndexError):
            self.index.advance(changed_previous, advanced_snapshot(changed_previous))

        nonexecuting = advanced_snapshot(
            self.initial,
            phase=RuntimeState.PREFLIGHT,
        )
        advanced = self.index.advance(self.initial, nonexecuting)
        self.assertEqual((), advanced.ready_set)
        self.assertEqual(GraphIndex.rebuild(self.manifest, nonexecuting), advanced)

        with self.assertRaises(GraphIndexError):
            self.index.require_match(self.manifest, nonexecuting)
        self.index.require_match(self.manifest, self.initial)

    def test_dependency_counters_update_forward_reverse_and_detect_drift(self) -> None:
        executing = folded_snapshot(graph_freeze_events())
        index = GraphIndex.rebuild(self.manifest, executing)
        verified_task = dataclasses.replace(
            executing.tasks[0],
            state=RuntimeState.VERIFIED,
        )
        completed = advanced_snapshot(
            executing,
            tasks=(verified_task, *executing.tasks[1:]),
        )
        forward = index.advance(executing, completed)
        self.assertEqual(GraphIndex.rebuild(self.manifest, completed), forward)

        restored_task = dataclasses.replace(
            completed.tasks[0],
            state=RuntimeState.APPROVED,
        )
        restored = advanced_snapshot(
            completed,
            tasks=(restored_task, *completed.tasks[1:]),
        )
        reverse = forward.advance(completed, restored)
        self.assertEqual(GraphIndex.rebuild(self.manifest, restored), reverse)

        drifted_nodes = tuple(
            dataclasses.replace(node, remaining_dependencies=0)
            if node.task_id in {"TASK-002", "TASK-003"}
            else node
            for node in index.nodes
        )
        drifted = dataclasses.replace(index, nodes=drifted_nodes)
        with self.assertRaises(GraphIndexError):
            drifted.advance(executing, completed)

    def test_ready_selection_skips_parallel_ownership_conflicts(self) -> None:
        value = valid_manifest()
        value["tasks"][2]["owned_paths"] = list(value["tasks"][1]["owned_paths"])
        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())
        manifest = decoded.value
        assert manifest is not None
        dag = TaskDag.compile(manifest)
        initial = KernelSnapshot.initial(RUN_ID, EPOCH, dag)
        states = {
            "TASK-001": RuntimeState.VERIFIED,
            "TASK-002": RuntimeState.APPROVED,
            "TASK-003": RuntimeState.APPROVED,
            "TASK-004": RuntimeState.APPROVED,
        }
        snapshot = dataclasses.replace(
            initial,
            phase=RuntimeState.EXECUTING,
            tasks=tuple(
                dataclasses.replace(task, state=states[task.task_id])
                for task in initial.tasks
            ),
        )

        index = GraphIndex.rebuild(manifest, snapshot)

        self.assertEqual(("TASK-002",), index.ready_set)
        self.assertIn("TASK-003", index.node("TASK-002").ownership_conflicts)


class CheckpointContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = admitted_manifest()

    def published_checkpoint(self, root: Path):
        journal = root / "journal"
        write_segments(journal, [graph_freeze_events()])
        replayed = replay_journal(
            journal,
            self.manifest,
            coordinator_epoch=EPOCH,
            repair_derived=False,
        )
        self.assertEqual(ReplayStatus.RECOVERED, replayed.status)
        store = CheckpointStore(root / "checkpoints")
        verified = store.publish(
            self.manifest,
            replayed.snapshot,
            replayed.graph_index,
            replayed.journal_position,
        )
        return store, replayed, verified

    def checkpoint_files(self, store: CheckpointStore):
        pointer = json.loads(store.current.read_bytes())
        target = store.root / Path(pointer["checkpoint_path"])
        record = json.loads(target.read_bytes())
        return pointer, target, record

    def rewrite_checkpoint(self, store: CheckpointStore, mutate) -> None:
        pointer, target, record = self.checkpoint_files(store)
        mutate(record, pointer)
        raw = canonical_json_bytes(record)
        target.write_bytes(raw)
        pointer["checkpoint_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        store.current.write_bytes(canonical_json_bytes(pointer))

    def test_checkpoint_is_bound_to_the_publishing_coordinator_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))

            loaded = store.load(self.manifest, coordinator_epoch=EPOCH + 1)

        self.assertEqual(CheckpointLoadStatus.DISCARDED, loaded.status)
        self.assertEqual(CheckpointFaultCode.SNAPSHOT_MISMATCH, loaded.fault.code)

    def test_checkpoint_value_objects_and_policy_guards(self) -> None:
        with self.assertRaises(ValueError):
            JournalPosition(0, 0)
        with self.assertRaises(ValueError):
            JournalPosition(1, -1)
        self.assertEqual(
            {"offset": 2, "segment": 1}, JournalPosition(1, 2).to_primitive()
        )

        checkpoint_id = "CHECKPOINT-00000000000000000000-" + "A" * 24
        pointer = CheckpointPointer(
            checkpoint_id,
            f"objects/{checkpoint_id}.json",
            GENESIS_HASH,
            0,
            GENESIS_HASH,
            0,
            GENESIS_HASH,
        )
        self.assertEqual(checkpoint_id, pointer.to_primitive()["checkpoint_id"])
        pointer_cases = (
            {"pointer_schema_version": 2},
            {"checkpoint_id": "bad"},
            {"checkpoint_path": "../escape.json"},
            {"checkpoint_path": "objects\\bad.json"},
            {"checkpoint_sha256": "bad"},
            {"checkpoint_through_event_hash": "bad"},
            {"journal_through_event_hash": "bad"},
            {"checkpoint_through_sequence": -1},
            {"journal_through_sequence": -1},
            {"journal_through_sequence": 0, "checkpoint_through_sequence": 1},
            {
                "journal_through_sequence": 0,
                "checkpoint_through_sequence": 0,
                "journal_through_event_hash": "sha256:" + "f" * 64,
            },
        )
        for updates in pointer_cases:
            with self.subTest(pointer=updates), self.assertRaises(ValueError):
                dataclasses.replace(pointer, **updates)

        fault = CheckpointFault(
            CheckpointFaultCode.POINTER_INVALID,
            "pointer_decode",
            "invalid",
            GENESIS_HASH,
        )
        fault_cases = (
            {"code": "bad"},
            {"operation": ""},
            {"detail": ""},
            {"raw_sha256": "bad"},
        )
        for updates in fault_cases:
            with (
                self.subTest(fault=updates),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(fault, **updates)

        with tempfile.TemporaryDirectory() as temporary:
            _, _, verified = self.published_checkpoint(Path(temporary))
            self.assertEqual(
                CheckpointLoadStatus.LOADED,
                CheckpointLoadResult(CheckpointLoadStatus.LOADED, verified).status,
            )
        invalid_results = (
            lambda: CheckpointLoadResult("bad"),
            lambda: CheckpointLoadResult(CheckpointLoadStatus.LOADED),
            lambda: CheckpointLoadResult(CheckpointLoadStatus.DISCARDED),
            lambda: CheckpointLoadResult(CheckpointLoadStatus.ABSENT, fault=fault),
        )
        for build in invalid_results:
            with self.assertRaises((TypeError, ValueError)):
                build()

        invalid_policies = (
            {"event_interval": 0},
            {"event_interval": True},
            {"time_interval_seconds": 0},
            {"time_interval_seconds": True},
            {"time_interval_seconds": float("nan")},
            {"time_interval_seconds": float("inf")},
        )
        for values in invalid_policies:
            with self.subTest(policy=values), self.assertRaises(ValueError):
                CheckpointPolicy(**values)

        policy = CheckpointPolicy(event_interval=10, time_interval_seconds=20)
        self.assertFalse(policy.should_publish(previous_sequence=1, current_sequence=1))
        self.assertFalse(policy.should_publish(previous_sequence=1, current_sequence=2))
        self.assertTrue(
            policy.should_publish(
                previous_sequence=1,
                current_sequence=2,
                last_event_type=JournalEventType.RUN_INITIALIZED,
            )
        )
        self.assertTrue(
            policy.should_publish(
                previous_sequence=1,
                current_sequence=2,
                last_event_type=JournalEventType.TRELLIS_GRAPH_IMPORTED,
            )
        )
        self.assertTrue(policy.should_publish(previous_sequence=1, current_sequence=11))
        self.assertTrue(
            policy.should_publish(
                previous_sequence=1,
                current_sequence=2,
                elapsed_seconds=20,
            )
        )
        invalid_calls = (
            {"previous_sequence": -1, "current_sequence": 0},
            {"previous_sequence": 2, "current_sequence": 1},
            {"previous_sequence": 0, "current_sequence": 1, "last_event_type": "bad"},
            {"previous_sequence": 0, "current_sequence": 1, "elapsed_seconds": -1},
            {"previous_sequence": 0, "current_sequence": 1, "elapsed_seconds": True},
            {
                "previous_sequence": 0,
                "current_sequence": 1,
                "elapsed_seconds": float("nan"),
            },
        )
        for values in invalid_calls:
            with self.subTest(call=values), self.assertRaises((TypeError, ValueError)):
                policy.should_publish(**values)

        persistence = CheckpointPersistenceFault("publish", "disk")
        self.assertEqual(CheckpointFaultCode.PUBLISH_FAILED, persistence.code)

    def test_checkpoint_record_mismatch_matrix_is_typed(self) -> None:
        def manifest_mismatch(record, pointer) -> None:
            record["manifest_hash"] = "sha256:" + "e" * 64

        def identity_mismatch(record, pointer) -> None:
            record["snapshot"]["coordinator_epoch"] = EPOCH + 1

        def terminal_mismatch(record, pointer) -> None:
            record["checkpoint_through_sequence"] += 1

        def identity_id_mismatch(record, pointer) -> None:
            replacement = "CHECKPOINT-99999999999999999999-" + "A" * 24
            record["checkpoint_id"] = replacement
            pointer["checkpoint_id"] = replacement

        def graph_mismatch(record, pointer) -> None:
            record["graph_index"]["ready_set"] = []

        def position_schema(record, pointer) -> None:
            record["journal_position"] = {"segment": 1}

        def snapshot_type(record, pointer) -> None:
            record["snapshot"] = []

        def snapshot_schema(record, pointer) -> None:
            record["snapshot"].pop("phase")

        def checkpoint_schema(record, pointer) -> None:
            record.pop("checkpoint_schema_version")

        def checkpoint_id(record, pointer) -> None:
            record["checkpoint_id"] = "CHECKPOINT-99999999999999999999-" + "B" * 24

        def graph_type(record, pointer) -> None:
            record["graph_index"] = []

        def snapshot_collection(record, pointer) -> None:
            record["snapshot"]["tasks"] = {}

        def task_schema(record, pointer) -> None:
            record["snapshot"]["tasks"][0].pop("state")

        def runtime_state_type(record, pointer) -> None:
            record["snapshot"]["tasks"][0]["state"] = 1

        def runtime_state_value(record, pointer) -> None:
            record["snapshot"]["tasks"][0]["state"] = "unknown"

        def reason_type(record, pointer) -> None:
            record["snapshot"]["tasks"][0]["reason_code"] = 1

        def reason_value(record, pointer) -> None:
            record["snapshot"]["tasks"][0]["reason_code"] = "unknown"

        def attempt_schema(record, pointer) -> None:
            record["snapshot"]["attempts"] = [{}]

        cases = (
            (manifest_mismatch, CheckpointFaultCode.MANIFEST_MISMATCH),
            (identity_mismatch, CheckpointFaultCode.SNAPSHOT_MISMATCH),
            (terminal_mismatch, CheckpointFaultCode.SNAPSHOT_MISMATCH),
            (identity_id_mismatch, CheckpointFaultCode.SNAPSHOT_MISMATCH),
            (graph_mismatch, CheckpointFaultCode.GRAPH_INDEX_MISMATCH),
            (position_schema, CheckpointFaultCode.CHECKPOINT_INVALID),
            (snapshot_type, CheckpointFaultCode.CHECKPOINT_INVALID),
            (snapshot_schema, CheckpointFaultCode.CHECKPOINT_INVALID),
            (checkpoint_schema, CheckpointFaultCode.CHECKPOINT_INVALID),
            (checkpoint_id, CheckpointFaultCode.CHECKPOINT_INVALID),
            (graph_type, CheckpointFaultCode.CHECKPOINT_INVALID),
            (snapshot_collection, CheckpointFaultCode.CHECKPOINT_INVALID),
            (task_schema, CheckpointFaultCode.CHECKPOINT_INVALID),
            (runtime_state_type, CheckpointFaultCode.CHECKPOINT_INVALID),
            (runtime_state_value, CheckpointFaultCode.CHECKPOINT_INVALID),
            (reason_type, CheckpointFaultCode.CHECKPOINT_INVALID),
            (reason_value, CheckpointFaultCode.CHECKPOINT_INVALID),
            (attempt_schema, CheckpointFaultCode.CHECKPOINT_INVALID),
        )
        for mutate, expected in cases:
            with (
                self.subTest(expected=expected),
                tempfile.TemporaryDirectory() as temporary,
            ):
                store, _, _ = self.published_checkpoint(Path(temporary))
                self.rewrite_checkpoint(store, mutate)
                loaded = store.load(self.manifest, coordinator_epoch=EPOCH)
                self.assertEqual(CheckpointLoadStatus.DISCARDED, loaded.status)
                self.assertEqual(expected, loaded.fault.code)
                self.assertIsNotNone(loaded.fault.raw_sha256)

    def test_pointer_checkpoint_and_io_failures_are_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))
            _, target, _ = self.checkpoint_files(store)
            target.unlink()
            missing = store.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(CheckpointFaultCode.CHECKPOINT_MISSING, missing.fault.code)

        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))
            _, target, _ = self.checkpoint_files(store)
            target.write_bytes(b"{}\n")
            mismatched = store.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(
                CheckpointFaultCode.CHECKPOINT_INVALID, mismatched.fault.code
            )

        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))
            store.current.unlink()
            absent = store.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(CheckpointLoadStatus.ABSENT, absent.status)

        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))
            pointer = json.loads(store.current.read_bytes())
            pointer["checkpoint_path"] = "../escape.json"
            store.current.write_bytes(canonical_json_bytes(pointer))
            invalid = store.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(CheckpointFaultCode.POINTER_INVALID, invalid.fault.code)

        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))
            with mock.patch.object(
                checkpoints_module,
                "_is_link_or_junction",
                return_value=True,
            ):
                invalid = store.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(CheckpointFaultCode.POINTER_INVALID, invalid.fault.code)

        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))
            with mock.patch.object(
                checkpoints_module,
                "_read_limited",
                side_effect=PermissionError("denied"),
            ):
                failed = store.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(CheckpointFaultCode.POINTER_IO_FAILED, failed.fault.code)

        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))
            with mock.patch.object(
                store,
                "_resolve_pointer_path",
                side_effect=PermissionError("denied"),
            ):
                failed = store.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(CheckpointFaultCode.POINTER_IO_FAILED, failed.fault.code)

        with tempfile.TemporaryDirectory() as temporary:
            store, _, _ = self.published_checkpoint(Path(temporary))
            original = checkpoints_module._read_limited

            def fail_checkpoint(path: Path, limit: int) -> bytes:
                if path != store.current:
                    raise PermissionError("denied")
                return original(path, limit)

            with mock.patch.object(
                checkpoints_module,
                "_read_limited",
                side_effect=fail_checkpoint,
            ):
                failed = store.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(
                CheckpointFaultCode.CHECKPOINT_IO_FAILED, failed.fault.code
            )

    def test_checkpoint_decoder_enforces_depth_number_string_and_byte_limits(
        self,
    ) -> None:
        malformed_pointers = (
            b"\xff",
            b"[]",
            b'{"x":1,"x":2}',
            b'{"x":NaN}',
            b'{"x":1.5}',
            b'{"x":9223372036854775808}',
            canonical_json_bytes(
                {"x": "x" * (DEFAULT_DECODE_LIMITS.max_string_length + 1)}
            ),
            canonical_json_bytes({"x": [0] * (DEFAULT_DECODE_LIMITS.max_items + 1)}),
        )
        for raw in malformed_pointers:
            with (
                self.subTest(size=len(raw)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                store = CheckpointStore(Path(temporary) / "checkpoints")
                store.root.mkdir()
                store.current.write_bytes(raw)
                loaded = store.load(self.manifest, coordinator_epoch=EPOCH)
                self.assertEqual(CheckpointFaultCode.POINTER_INVALID, loaded.fault.code)

        malformed_checkpoints = (
            b'{"x":' + b"[" * 1_100 + b"0" + b"]" * 1_100 + b"}",
            b'{"x":1.5}',
            b"{} \n",
            b"x" * (checkpoints_module.CHECKPOINT_MAX_BYTES + 1),
        )
        for raw in malformed_checkpoints:
            with (
                self.subTest(size=len(raw)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                store, _, _ = self.published_checkpoint(Path(temporary))
                pointer, target, _ = self.checkpoint_files(store)
                target.write_bytes(raw)
                pointer["checkpoint_sha256"] = (
                    "sha256:" + hashlib.sha256(raw).hexdigest()
                )
                store.current.write_bytes(canonical_json_bytes(pointer))
                loaded = store.load(self.manifest, coordinator_epoch=EPOCH)
                self.assertEqual(
                    CheckpointFaultCode.CHECKPOINT_INVALID, loaded.fault.code
                )

    def test_publish_is_idempotent_and_stops_at_control_root_or_io_faults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, replayed, first = self.published_checkpoint(Path(temporary))
            second = store.publish(
                self.manifest,
                replayed.snapshot,
                replayed.graph_index,
                replayed.journal_position,
            )
            self.assertEqual(first, second)

            with (
                mock.patch.object(
                    checkpoints_module,
                    "_atomic_publish",
                    side_effect=OSError("disk"),
                ),
                self.assertRaises(CheckpointPersistenceFault),
            ):
                store.publish(
                    self.manifest,
                    replayed.snapshot,
                    replayed.graph_index,
                    replayed.journal_position,
                )

            future = store.publish(
                self.manifest,
                replayed.snapshot,
                replayed.graph_index,
                replayed.journal_position,
                journal_through_sequence=replayed.snapshot.last_sequence + 1,
                journal_through_event_hash="sha256:" + "f" * 64,
            )
            self.assertEqual(
                replayed.snapshot.last_sequence + 1, future.journal_through_sequence
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "journal"
            write_segments(journal, [graph_freeze_events()])
            replayed = replay_journal(
                journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                repair_derived=False,
            )
            denied = CheckpointStore(
                root / "denied", control_root_validator=lambda: False
            )
            with self.assertRaises(CheckpointPersistenceFault):
                denied.publish(
                    self.manifest,
                    replayed.snapshot,
                    replayed.graph_index,
                    replayed.journal_position,
                )
            self.assertFalse(denied.root.exists())

            validations = iter((True, True, False))
            drifting = CheckpointStore(
                root / "drifting",
                control_root_validator=lambda: next(validations),
            )
            with self.assertRaises(CheckpointPersistenceFault):
                drifting.publish(
                    self.manifest,
                    replayed.snapshot,
                    replayed.graph_index,
                    replayed.journal_position,
                )
            self.assertFalse(drifting.current.exists())
            self.assertEqual(1, len(list(drifting.objects.glob("*.json"))))

            short = CheckpointStore(root / "short")
            with (
                mock.patch.object(
                    checkpoints_module,
                    "_write_all",
                    side_effect=OSError("short"),
                ),
                self.assertRaises(CheckpointPersistenceFault),
            ):
                short.publish(
                    self.manifest,
                    replayed.snapshot,
                    replayed.graph_index,
                    replayed.journal_position,
                )

    def test_publish_input_guards_and_load_control_root_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, replayed, _ = self.published_checkpoint(Path(temporary))
            calls = (
                lambda: store.publish(
                    object(),
                    replayed.snapshot,
                    replayed.graph_index,
                    replayed.journal_position,
                ),
                lambda: store.publish(
                    self.manifest,
                    object(),
                    replayed.graph_index,
                    replayed.journal_position,
                ),
                lambda: store.publish(
                    self.manifest,
                    replayed.snapshot,
                    object(),
                    replayed.journal_position,
                ),
                lambda: store.publish(
                    self.manifest,
                    replayed.snapshot,
                    replayed.graph_index,
                    object(),
                ),
                lambda: store.publish(
                    self.manifest,
                    replayed.snapshot,
                    GraphIndex.rebuild(
                        self.manifest,
                        KernelSnapshot.initial(
                            RUN_ID, EPOCH, TaskDag.compile(self.manifest)
                        ),
                    ),
                    replayed.journal_position,
                ),
                lambda: store.publish(
                    self.manifest,
                    replayed.snapshot,
                    replayed.graph_index,
                    replayed.journal_position,
                    journal_through_sequence=replayed.snapshot.last_sequence - 1,
                ),
                lambda: store.publish(
                    self.manifest,
                    replayed.snapshot,
                    replayed.graph_index,
                    replayed.journal_position,
                    journal_through_event_hash="bad",
                ),
                lambda: store.publish(
                    self.manifest,
                    replayed.snapshot,
                    replayed.graph_index,
                    replayed.journal_position,
                    journal_through_event_hash="sha256:" + "f" * 64,
                ),
            )
            for call in calls:
                with self.assertRaises((TypeError, ValueError)):
                    call()

            invalid_loads = (
                CheckpointStore(store.root, control_root_validator=lambda: False),
                CheckpointStore(
                    store.root,
                    control_root_validator=lambda: (_ for _ in ()).throw(
                        OSError("drift")
                    ),
                ),
                CheckpointStore(store.root, expected_control_root=object()),
            )
            for guarded in invalid_loads:
                loaded = guarded.load(self.manifest, coordinator_epoch=EPOCH)
                self.assertEqual(
                    CheckpointFaultCode.CONTROL_ROOT_DRIFT, loaded.fault.code
                )

            validations = iter((True, False))
            changed = CheckpointStore(
                store.root,
                control_root_validator=lambda: next(validations),
            )
            loaded = changed.load(self.manifest, coordinator_epoch=EPOCH)
            self.assertEqual(CheckpointFaultCode.CONTROL_ROOT_DRIFT, loaded.fault.code)

            with self.assertRaises(TypeError):
                store.load(object(), coordinator_epoch=EPOCH)
            with self.assertRaises(ValueError):
                store.load(self.manifest, coordinator_epoch=0)

        with self.assertRaises(ValueError):
            CheckpointStore(
                "checkpoints",
                control_root_validator=lambda: True,
                expected_control_root=object(),
            )


class ReplayRecoveryEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_root = Path(self.temporary.name)
        self.journal = self.run_root / "journal"
        self.manifest = admitted_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def replay(self, **kwargs: object) -> ReplayResult:
        return replay_journal(
            self.journal,
            self.manifest,
            coordinator_epoch=EPOCH,
            **kwargs,
        )

    def test_replay_fault_tail_derived_and_result_guards(self) -> None:
        fault = ReplayFault(
            ReplayFaultCode.STATE_REJECTED,
            "state_mismatch",
            1,
            0,
            GENESIS_HASH,
            GENESIS_HASH,
        )
        fault_cases = (
            {"code": "bad"},
            {"detail": ""},
            {"segment": 0},
            {"segment": True},
            {"byte_offset": -1},
            {"byte_offset": True},
            {"previous_event_hash": "bad"},
            {"raw_sha256": "bad"},
        )
        for updates in fault_cases:
            with (
                self.subTest(fault=updates),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(fault, **updates)

        tail = QuarantinedTail(1, 0, 1, GENESIS_HASH, "quarantine/tail.bin")
        tail_cases = (
            {"segment": 0},
            {"byte_offset": -1},
            {"byte_count": 0},
            {"raw_sha256": "bad"},
            {"quarantine_path": ""},
            {"reason": "bad"},
        )
        for updates in tail_cases:
            with self.subTest(tail=updates), self.assertRaises(ValueError):
                dataclasses.replace(tail, **updates)

        derived = DerivedDataFault("checkpoint", "invalid", GENESIS_HASH)
        for updates in (
            {"source": ""},
            {"detail": ""},
            {"raw_sha256": "bad"},
        ):
            with self.subTest(derived=updates), self.assertRaises(ValueError):
                dataclasses.replace(derived, **updates)

        recovered = self.replay(repair_derived=False)
        self.assertEqual(ReplayStatus.RECOVERED, recovered.status)
        invalid_results = (
            {"status": "bad"},
            {"snapshot": object()},
            {"graph_index": object()},
            {"head": object()},
            {"journal_position": object()},
            {"events_replayed": -1},
            {"max_frame_bytes": -1},
            {"checkpoint_used": 1},
            {"quarantined_tail": object()},
            {"derived_faults": []},
            {"derived_faults": (object(),)},
            {"status": ReplayStatus.BLOCKED},
            {"fault": fault},
        )
        for updates in invalid_results:
            with (
                self.subTest(result=updates),
                self.assertRaises((TypeError, ValueError)),
            ):
                dataclasses.replace(recovered, **updates)
        blocked = dataclasses.replace(
            recovered,
            status=ReplayStatus.BLOCKED,
            fault=fault,
        )
        self.assertEqual(ReplayStatus.BLOCKED, blocked.status)

    def test_replay_argument_and_validator_guards(self) -> None:
        calls = (
            lambda: replay_journal(self.journal, object(), coordinator_epoch=EPOCH),
            lambda: replay_journal(self.journal, self.manifest, coordinator_epoch=0),
            lambda: replay_journal(self.journal, self.manifest, coordinator_epoch=True),
            lambda: replay_journal(
                self.journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                checkpoint_policy=object(),
            ),
            lambda: replay_journal(
                self.journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                checkpoint_store=object(),
            ),
            lambda: replay_journal(
                self.journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                repair_derived=1,
            ),
            lambda: replay_journal(
                self.journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                control_root_validator=lambda: True,
                expected_control_root=object(),
            ),
        )
        for call in calls:
            with self.assertRaises((TypeError, ValueError)):
                call()

        raised = self.replay(
            repair_derived=False,
            control_root_validator=lambda: (_ for _ in ()).throw(OSError("drift")),
        )
        self.assertEqual(ReplayFaultCode.CONTROL_ROOT_DRIFT, raised.fault.code)
        invalid_identity = self.replay(
            repair_derived=False,
            expected_control_root=object(),
        )
        self.assertEqual(
            ReplayFaultCode.CONTROL_ROOT_DRIFT, invalid_identity.fault.code
        )

    def test_non_transition_event_uses_strict_fallback_and_advances_chain(self) -> None:
        recovery = JournalEvent.create(
            sequence=1,
            event_id="EVENT-000001",
            event_type=JournalEventType.RECOVERY_STARTED,
            identity=ExecutionIdentity(RUN_ID, EPOCH),
            actor_type=ActorType.SYSTEM,
            actor_id="replay-test",
            recorded_at=NOW,
            previous_event_hash=GENESIS_HASH,
            payload=RecoveryPayload(0, GENESIS_HASH, (), ()),
        )
        write_segments(self.journal, [[recovery]])

        result = self.replay(repair_derived=False)

        self.assertEqual(ReplayStatus.RECOVERED, result.status)
        self.assertEqual(1, result.events_replayed)
        self.assertEqual(recovery.event_hash, result.snapshot.last_event_hash)

        malformed = (
            b'{"x":1,"x":2}\n',
            b'{"x":1.5}\n',
            b'{"x":9223372036854775808}\n',
            b"\xff\n",
        )
        for raw in malformed:
            with self.subTest(raw=raw):
                segment = self.journal / "segments" / "segment-00000001.jsonl"
                segment.write_bytes(raw)
                blocked = self.replay(repair_derived=False)
                self.assertEqual(
                    ReplayFaultCode.EVENT_DECODE_FAILED, blocked.fault.code
                )

    def test_valid_index_is_scanned_and_mismatch_falls_back_to_cold_replay(
        self,
    ) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events[:3], events[3:]])
        with mock.patch.object(CheckpointPolicy, "should_publish", return_value=False):
            built = self.replay()
        self.assertEqual(ReplayStatus.RECOVERED, built.status)
        index_path = self.journal / "index.json"
        self.assertTrue(index_path.exists())

        verified = self.replay(repair_derived=False)
        self.assertEqual(ReplayStatus.RECOVERED, verified.status)
        self.assertFalse(
            any(f.source == "journal_index" for f in verified.derived_faults)
        )

        value = json.loads(index_path.read_bytes())
        value["byte_count"] += 1
        index_path.write_bytes(canonical_json_bytes(value))
        mismatched = self.replay(repair_derived=False)
        self.assertEqual(ReplayStatus.RECOVERED, mismatched.status)
        self.assertTrue(
            any(f.source == "journal_index" for f in mismatched.derived_faults)
        )

    def test_future_checkpoint_anchor_mismatch_and_missing_journal_block(self) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events])
        cold = self.replay(repair_derived=False)
        store = CheckpointStore(self.run_root / "checkpoints")
        store.publish(
            self.manifest,
            cold.snapshot,
            cold.graph_index,
            cold.journal_position,
            journal_through_sequence=7,
            journal_through_event_hash="sha256:" + "f" * 64,
        )
        pause = transition_event(
            7,
            events[-1].event_hash,
            JournalEventType.PAUSE_REQUESTED,
            TransitionSubject.RUN,
            RuntimeState.RUNNING,
            RuntimeState.PAUSING,
            reason_code=RuntimeReasonCode.PAUSE_REQUESTED,
        )
        segment = self.journal / "segments" / "segment-00000001.jsonl"
        with segment.open("ab") as handle:
            handle.write(pause.canonical_json_bytes())

        mismatched = self.replay(checkpoint_store=store, repair_derived=False)
        self.assertEqual(ReplayStatus.BLOCKED, mismatched.status)
        self.assertEqual(ReplayFaultCode.HASH_CHAIN_MISMATCH, mismatched.fault.code)
        self.assertTrue(
            any(
                "fell_back_to_genesis" in fault.detail
                for fault in mismatched.derived_faults
            )
        )

        with mock.patch.object(replay_module, "_segment_paths", return_value=()):
            absent = self.replay(checkpoint_store=store, repair_derived=False)
        self.assertEqual(ReplayFaultCode.JOURNAL_TRUNCATED, absent.fault.code)

    def test_checkpoint_position_boundaries_choose_safe_fallback_or_tail(self) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events])
        cold = self.replay(repair_derived=False)
        invalid_store = CheckpointStore(self.run_root / "invalid-position")
        invalid_store.publish(
            self.manifest,
            cold.snapshot,
            cold.graph_index,
            JournalPosition(1, cold.journal_position.offset + 1),
        )
        fallback = self.replay(
            checkpoint_store=invalid_store,
            repair_derived=False,
        )
        self.assertEqual(ReplayStatus.RECOVERED, fallback.status)
        self.assertFalse(fallback.checkpoint_used)
        self.assertTrue(
            any(f.detail == "journal_position_invalid" for f in fallback.derived_faults)
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "journal"
            write_segments(journal, [[]])
            snapshot = KernelSnapshot.initial(
                RUN_ID,
                EPOCH,
                TaskDag.compile(self.manifest),
            )
            graph = GraphIndex.rebuild(self.manifest, snapshot)
            store = CheckpointStore(root / "checkpoints")
            store.publish(self.manifest, snapshot, graph, JournalPosition(1, 0))
            empty = replay_journal(
                journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                checkpoint_store=store,
                repair_derived=False,
            )
            self.assertTrue(empty.checkpoint_used)
            self.assertEqual(0, empty.events_replayed)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "journal"
            write_segments(journal, [events, []])
            with mock.patch.object(
                CheckpointPolicy, "should_publish", return_value=False
            ):
                replayed = replay_journal(
                    journal,
                    self.manifest,
                    coordinator_epoch=EPOCH,
                )
            store = CheckpointStore(root / "manual-checkpoint")
            store.publish(
                self.manifest,
                replayed.snapshot,
                replayed.graph_index,
                JournalPosition(2, 0),
            )
            tail = replay_journal(
                journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                checkpoint_store=store,
                repair_derived=False,
            )
            self.assertTrue(tail.checkpoint_used)
            self.assertEqual(0, tail.events_replayed)

    def test_segment_graph_and_control_root_races_are_typed(self) -> None:
        path = self.journal / "segments" / "segment-00000001.jsonl"
        with mock.patch.object(
            replay_module,
            "_segment_paths",
            return_value=((1, path),),
        ):
            missing = self.replay(repair_derived=False)
        self.assertEqual(ReplayFaultCode.SEGMENT_IO_FAILED, missing.fault.code)

        write_segments(self.journal, [graph_freeze_events()[:1]])
        segments = ((1, path),)
        with (
            mock.patch.object(replay_module, "_segment_paths", return_value=segments),
            mock.patch.object(replay_module.stat, "S_ISREG", return_value=False),
        ):
            replaced = self.replay(repair_derived=False)
        self.assertEqual(ReplayFaultCode.SEGMENT_REPLACED, replaced.fault.code)

        with mock.patch.object(GraphIndex, "verify", return_value=False):
            drifted = self.replay(repair_derived=False)
        self.assertEqual(ReplayFaultCode.GRAPH_INDEX_MISMATCH, drifted.fault.code)

        with mock.patch.object(
            GraphIndex,
            "advance",
            side_effect=GraphIndexError("drift"),
        ):
            failed_advance = self.replay(repair_derived=False)
        self.assertEqual(
            ReplayFaultCode.GRAPH_INDEX_MISMATCH, failed_advance.fault.code
        )

        validations = iter((True, True, False))
        before_read = self.replay(
            repair_derived=False,
            control_root_validator=lambda: next(validations, False),
        )
        self.assertEqual(ReplayFaultCode.CONTROL_ROOT_DRIFT, before_read.fault.code)

        validations = iter((True, True, True, False))
        after_read = self.replay(
            repair_derived=False,
            control_root_validator=lambda: next(validations, False),
        )
        self.assertEqual(ReplayFaultCode.CONTROL_ROOT_DRIFT, after_read.fault.code)
        self.assertEqual(1, after_read.events_replayed)

    def test_quarantine_and_derived_publish_failures_never_resume(self) -> None:
        first = graph_freeze_events()[0]
        write_segments(self.journal, [[first]])
        segment = self.journal / "segments" / "segment-00000001.jsonl"
        with segment.open("ab") as handle:
            handle.write(b'{"partial":true}')
        validations = iter((True, True, True, True, False))
        quarantine_failed = self.replay(
            repair_derived=False,
            control_root_validator=lambda: next(validations, False),
        )
        self.assertEqual(
            ReplayFaultCode.QUARANTINE_FAILED, quarantine_failed.fault.code
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "journal"
            events = graph_freeze_events()
            write_segments(journal, [events[:3], events[3:]])
            with mock.patch.object(
                replay_module,
                "_atomic_publish",
                side_effect=OSError("disk"),
            ):
                index_failed = replay_journal(
                    journal,
                    self.manifest,
                    coordinator_epoch=EPOCH,
                )
            self.assertEqual(
                ReplayFaultCode.DERIVED_PUBLISH_FAILED, index_failed.fault.code
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "journal"
            write_segments(journal, [graph_freeze_events()])
            store = CheckpointStore(root / "checkpoints")
            with mock.patch.object(
                store,
                "publish",
                side_effect=CheckpointPersistenceFault("publish", "disk"),
            ):
                checkpoint_failed = replay_journal(
                    journal,
                    self.manifest,
                    coordinator_epoch=EPOCH,
                    checkpoint_store=store,
                )
            self.assertEqual(
                ReplayFaultCode.DERIVED_PUBLISH_FAILED,
                checkpoint_failed.fault.code,
            )

    def test_repeated_stale_index_quarantine_is_idempotent(self) -> None:
        self.journal.mkdir(parents=True)
        stale = canonical_json_bytes({"stale": True})
        index = self.journal / "index.json"
        index.write_bytes(stale)
        first = self.replay()
        self.assertEqual(ReplayStatus.RECOVERED, first.status)
        index.write_bytes(stale)
        second = self.replay()
        self.assertEqual(ReplayStatus.RECOVERED, second.status)
        self.assertFalse(index.exists())
        self.assertEqual(1, len(list((self.run_root / "quarantine").glob("*.json"))))

    def test_invalid_index_fields_and_absent_segments_are_non_authoritative(
        self,
    ) -> None:
        events = graph_freeze_events()

        def built_index(root: Path) -> bytes:
            journal = root / "journal"
            write_segments(journal, [events[:3], events[3:]])
            with mock.patch.object(
                CheckpointPolicy, "should_publish", return_value=False
            ):
                result = replay_journal(
                    journal,
                    self.manifest,
                    coordinator_epoch=EPOCH,
                )
            self.assertEqual(ReplayStatus.RECOVERED, result.status)
            return (journal / "index.json").read_bytes()

        with tempfile.TemporaryDirectory() as temporary:
            base = built_index(Path(temporary))

        def integer(value: dict[str, object]) -> None:
            value["event_count"] = 0

        def invalid_hash(value: dict[str, object]) -> None:
            value["last_event_hash"] = "bad"

        def stale_active(value: dict[str, object]) -> None:
            value["active_segment"] = 3
            value["sealed_segment"] = 2

        def segment_order(value: dict[str, object]) -> None:
            value["sealed_segment"] = value["active_segment"]

        for mutate in (integer, invalid_hash, stale_active, segment_order):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                journal = root / "journal"
                write_segments(journal, [events[:3], events[3:]])
                value = json.loads(base)
                mutate(value)
                (journal / "index.json").write_bytes(canonical_json_bytes(value))
                result = replay_journal(
                    journal,
                    self.manifest,
                    coordinator_epoch=EPOCH,
                    repair_derived=False,
                )
                self.assertEqual(ReplayStatus.RECOVERED, result.status)
                self.assertTrue(
                    any(f.source == "journal_index" for f in result.derived_faults)
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "journal"
            journal.mkdir()
            (journal / "index.json").write_bytes(base)
            absent = replay_journal(
                journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                repair_derived=False,
            )
            self.assertEqual(ReplayStatus.RECOVERED, absent.status)
            self.assertTrue(
                any(f.source == "journal_index" for f in absent.derived_faults)
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "journal"
            write_segments(journal, [events])
            (journal / "index.json").write_bytes(b"x" * (16 * 1024 + 1))
            oversized = replay_journal(
                journal,
                self.manifest,
                coordinator_epoch=EPOCH,
                repair_derived=False,
            )
            self.assertEqual(ReplayStatus.RECOVERED, oversized.status)
            self.assertTrue(
                any(f.source == "journal_index" for f in oversized.derived_faults)
            )

    def test_corrupt_sealed_segment_invalidates_index_before_replay_blocks(
        self,
    ) -> None:
        events = graph_freeze_events()
        corruptions = (
            b"{}\n",
            events[1].canonical_json_bytes(),
            b'{"partial":true}',
            b"",
        )
        for corruption in corruptions:
            with (
                self.subTest(size=len(corruption)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                journal = root / "journal"
                write_segments(journal, [events[:3], events[3:]])
                with mock.patch.object(
                    CheckpointPolicy, "should_publish", return_value=False
                ):
                    replay_journal(
                        journal,
                        self.manifest,
                        coordinator_epoch=EPOCH,
                    )
                sealed = journal / "segments" / "segment-00000001.jsonl"
                sealed.write_bytes(corruption)
                result = replay_journal(
                    journal,
                    self.manifest,
                    coordinator_epoch=EPOCH,
                    repair_derived=False,
                )
                self.assertEqual(ReplayStatus.BLOCKED, result.status)
                self.assertTrue(
                    any(f.source == "journal_index" for f in result.derived_faults)
                )

    def test_link_like_layout_and_root_drift_before_repair_are_typed(self) -> None:
        events = graph_freeze_events()
        write_segments(self.journal, [events[:3], events[3:]])
        original = replay_module._is_link_or_junction

        def directory_link(path: Path) -> bool:
            return path.name == "segments" or original(path)

        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            side_effect=directory_link,
        ):
            directory = self.replay(repair_derived=False)
        self.assertEqual(ReplayFaultCode.INVALID_LAYOUT, directory.fault.code)

        def segment_link(path: Path) -> bool:
            return path.name == "segment-00000001.jsonl" or original(path)

        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            side_effect=segment_link,
        ):
            segment = self.replay(repair_derived=False)
        self.assertEqual(ReplayFaultCode.INVALID_LAYOUT, segment.fault.code)

        validations = iter((True, True, True, True, True, False))
        drifted = self.replay(
            control_root_validator=lambda: next(validations, False),
        )
        self.assertEqual(ReplayFaultCode.DERIVED_PUBLISH_FAILED, drifted.fault.code)
        self.assertIn("control_root_drift", drifted.fault.detail)


class ReplayScaleTests(unittest.TestCase):
    def test_one_hundred_thousand_event_replay_is_streaming(self) -> None:
        command = (
            sys.executable,
            "-c",
            (
                "import json; "
                "from tests.services.test_replay import _run_replay_scale_probe; "
                "print(json.dumps(_run_replay_scale_probe(), sort_keys=True))"
            ),
        )
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            timeout=180,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(100_000, result["events_replayed"])
        self.assertEqual(100_000, result["last_sequence"])
        self.assertTrue(result["graph_index_verified"])
        self.assertLess(result["peak_rss_bytes"], 512 * 1024 * 1024)
        self.assertLessEqual(result["max_frame_bytes"], 1_048_576)


def _run_replay_scale_probe() -> dict[str, object]:
    manifest = admitted_manifest()
    with tempfile.TemporaryDirectory() as temporary:
        journal = Path(temporary) / "journal"
        segments = journal / "segments"
        segments.mkdir(parents=True)
        path = segments / "segment-00000001.jsonl"
        prefix = graph_freeze_events()
        previous_hash = GENESIS_HASH
        status = RuntimeState.RUNNING
        with path.open("wb") as handle:
            for event in prefix:
                handle.write(event.canonical_json_bytes())
                previous_hash = event.event_hash
            for sequence in range(len(prefix) + 1, 100_001):
                if status is RuntimeState.RUNNING:
                    event_type = JournalEventType.PAUSE_REQUESTED
                    to_state = RuntimeState.PAUSING
                    reason = RuntimeReasonCode.PAUSE_REQUESTED
                elif status is RuntimeState.PAUSING:
                    event_type = JournalEventType.RUN_PAUSED
                    to_state = RuntimeState.PAUSED
                    reason = None
                else:
                    event_type = JournalEventType.RUN_RESUMED
                    to_state = RuntimeState.RUNNING
                    reason = None
                event = transition_event(
                    sequence,
                    previous_hash,
                    event_type,
                    TransitionSubject.RUN,
                    status,
                    to_state,
                    reason_code=reason,
                )
                handle.write(event.canonical_json_bytes())
                previous_hash = event.event_hash
                status = to_state
        result = replay_journal(
            journal,
            manifest,
            coordinator_epoch=EPOCH,
            repair_derived=False,
        )

        if result.status is not ReplayStatus.RECOVERED:
            raise AssertionError(f"unexpected replay status: {result.status.value}")
        if result.snapshot.last_event_hash != previous_hash:
            raise AssertionError("snapshot event hash does not match the journal head")
        if result.head.event_hash != previous_hash:
            raise AssertionError("replay head does not match the journal head")
        if result.snapshot.status is not status:
            raise AssertionError("snapshot state does not match the journal tail")
        if result.journal_position.offset != path.stat().st_size:
            raise AssertionError("replay offset does not match the journal size")
        return {
            "events_replayed": result.events_replayed,
            "graph_index_verified": result.graph_index.verify(manifest, result.snapshot),
            "last_sequence": result.snapshot.last_sequence,
            "max_frame_bytes": result.max_frame_bytes,
            "peak_rss_bytes": _peak_rss_bytes(),
        }


def _peak_rss_bytes() -> int:
    if os.name != "nt":
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if os.uname().sysname == "Darwin" else peak * 1024

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    get_process = ctypes.windll.kernel32.GetCurrentProcess
    get_process.argtypes = ()
    get_process.restype = ctypes.c_void_p
    get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
    get_memory.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    get_memory.restype = ctypes.c_int
    process = get_process()
    if not get_memory(
        process,
        ctypes.byref(counters),
        counters.cb,
    ):
        raise ctypes.WinError()
    return int(counters.PeakWorkingSetSize)


if __name__ == "__main__":
    unittest.main()
