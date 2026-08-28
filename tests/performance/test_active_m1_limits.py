from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from tests.services.test_replay import EPOCH, admitted_manifest
from wish_builder.contracts import DEFAULT_DECODE_LIMITS
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.services.checkpoints import CheckpointPolicy, CheckpointStore
from wish_builder.services.replay import ReplayStatus, replay_journal

from .helpers import append_status_events, envelope_manifest, write_large_journal

TOTAL_EVENTS = 100_000
CHECKPOINT_SEQUENCE = TOTAL_EVENTS - 10


class ReplayCheckpointPerformanceTests(unittest.TestCase):
    def test_100k_event_checkpoint_replays_only_bounded_tail_deterministically(
        self,
    ) -> None:
        manifest = admitted_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "journal"
            checkpoint_event = write_large_journal(
                journal,
                through_sequence=CHECKPOINT_SEQUENCE,
            )

            cold = replay_journal(
                journal,
                manifest,
                coordinator_epoch=EPOCH,
                repair_derived=False,
            )

            self.assertEqual(ReplayStatus.RECOVERED, cold.status)
            self.assertEqual(CHECKPOINT_SEQUENCE, cold.events_replayed)
            self.assertEqual(CHECKPOINT_SEQUENCE, cold.snapshot.last_sequence)
            self.assertEqual(checkpoint_event.event_hash, cold.head.event_hash)

            store = CheckpointStore(root / "checkpoints")
            store.publish(
                manifest,
                cold.snapshot,
                cold.graph_index,
                cold.journal_position,
            )
            final_event = append_status_events(
                journal,
                first_sequence=CHECKPOINT_SEQUENCE + 1,
                through_sequence=TOTAL_EVENTS,
                previous_hash=checkpoint_event.event_hash,
            )

            results = []
            for _ in range(2):
                result = replay_journal(
                    journal,
                    manifest,
                    coordinator_epoch=EPOCH,
                    checkpoint_store=store,
                    checkpoint_policy=CheckpointPolicy(
                        event_interval=TOTAL_EVENTS + 1,
                    ),
                    repair_derived=False,
                )
                results.append(result)

            first, second = results
            self.assertEqual(ReplayStatus.RECOVERED, first.status)
            self.assertTrue(first.checkpoint_used)
            self.assertEqual(10, first.events_replayed)
            self.assertEqual(TOTAL_EVENTS, first.snapshot.last_sequence)
            self.assertEqual(final_event.event_hash, first.head.event_hash)
            self.assertLess(first.max_frame_bytes, DEFAULT_DECODE_LIMITS.max_bytes)
            self.assertEqual(first.snapshot, second.snapshot)
            self.assertEqual(first.graph_index, second.graph_index)
            self.assertEqual(first.head, second.head)
            self.assertEqual(first.journal_position, second.journal_position)
            self.assertEqual(first.events_replayed, second.events_replayed)


class GraphEnvelopePerformanceTests(unittest.TestCase):
    def test_64_task_512_edge_dag_and_graph_index_are_deterministic(self) -> None:
        manifest = envelope_manifest(admitted_manifest())
        reordered = dataclasses.replace(
            manifest,
            tasks=tuple(reversed(manifest.tasks)),
        )

        first_dag = TaskDag.compile(manifest)
        first_index = GraphIndex.compile(manifest)
        second_dag = TaskDag.compile(reordered)
        second_index = GraphIndex.compile(reordered)

        self.assertEqual(64, len(manifest.tasks))
        self.assertEqual(512, first_dag.edge_count)
        self.assertEqual(first_dag, second_dag)
        self.assertEqual(first_index, second_index)
        self.assertEqual(first_index.digest, second_index.digest)
        self.assertEqual(
            first_index.canonical_json_bytes(),
            second_index.canonical_json_bytes(),
        )
        self.assertEqual(("TASK-001",), first_index.ready_set)


if __name__ == "__main__":
    unittest.main()
