from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.adapters.test_trellis_graph_import import settings, snapshot
from wish_builder.adapters.trellis import import_trellis_snapshot
from wish_builder.contracts import ExecutionManifestV2, RuntimeState
from wish_builder.kernel import KernelSnapshot, TaskDag
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.services.checkpoints import CheckpointStore, JournalPosition
from wish_builder.services.replay import ReplayStatus, replay_journal


class ManifestV2ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = import_trellis_snapshot(snapshot(), settings()).manifest
        self.assertIs(type(self.manifest), ExecutionManifestV2)

    def test_trellis_manifest_uses_the_existing_dag_and_graph_index(self) -> None:
        dag = TaskDag.compile(self.manifest)
        self.assertEqual(("TASK-001", "TASK-002"), dag.topological_order)

        frozen_index = GraphIndex.compile(self.manifest)
        self.assertEqual(
            {"TASK-001": RuntimeState.APPROVED, "TASK-002": RuntimeState.APPROVED},
            dict(frozen_index.task_states),
        )

        snapshot_value = KernelSnapshot.initial(self.manifest.run_id, 1, dag)
        index = GraphIndex.compile(self.manifest, snapshot_value)
        self.assertEqual(
            {"TASK-001": RuntimeState.PROPOSED, "TASK-002": RuntimeState.PROPOSED},
            dict(index.task_states),
        )
        self.assertTrue(index.verify(self.manifest, snapshot_value))

    def test_replay_and_checkpoint_accept_the_immutable_v2_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            recovered = replay_journal(
                root / "journal",
                self.manifest,
                coordinator_epoch=1,
                repair_derived=False,
            )
            self.assertEqual(ReplayStatus.RECOVERED, recovered.status)
            self.assertEqual(0, recovered.head.sequence)
            self.assertTrue(
                recovered.graph_index.verify(self.manifest, recovered.snapshot)
            )

            store = CheckpointStore(root / "checkpoints")
            published = store.publish(
                self.manifest,
                recovered.snapshot,
                recovered.graph_index,
                JournalPosition(1, 0),
            )
            loaded = store.load(self.manifest, coordinator_epoch=1)
            self.assertEqual(published, loaded.checkpoint)

    def test_unadmitted_lookalikes_remain_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "ExecutionManifest"):
            TaskDag.compile(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "ExecutionManifest"):
            GraphIndex.compile(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
