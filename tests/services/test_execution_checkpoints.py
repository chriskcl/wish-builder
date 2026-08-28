from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.processes.test_coordinator import CoordinatorHarness
from wish_builder.services.checkpoints import CheckpointPolicy, CheckpointStore
from wish_builder.services.execution_checkpoints import (
    ExecutionCheckpointPublisher,
    ExecutionCheckpointReason,
    ExecutionCheckpointStatus,
)


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class ExecutionCheckpointPublisherTests(unittest.TestCase):
    def test_event_threshold_publishes_exact_durable_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = CoordinatorHarness(root)
            clock = _Clock()
            publisher = ExecutionCheckpointPublisher(
                harness.manifest,
                harness.journal,
                CheckpointStore(root / "checkpoints"),
                policy=CheckpointPolicy(event_interval=1),
                previous_sequence=harness.coordinator.cursor.head.sequence,
                monotonic_clock=clock,
            )

            step = harness.coordinator.dispatch_ready()
            event = step.events[-1]
            result = publisher.observe(
                step.cursor.snapshot,
                step.cursor.graph_index,
                step.cursor.head,
                event,
            )

            self.assertEqual(ExecutionCheckpointStatus.PUBLISHED, result.status)
            self.assertEqual(ExecutionCheckpointReason.NONE, result.reason)
            self.assertEqual(step.cursor.head.sequence, publisher.previous_sequence)
            self.assertIsNotNone(result.checkpoint)
            assert result.checkpoint is not None
            self.assertGreater(result.checkpoint.journal_position.offset, 0)

    def test_not_due_does_not_write_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = CoordinatorHarness(root)
            clock = _Clock()
            publisher = ExecutionCheckpointPublisher(
                harness.manifest,
                harness.journal,
                CheckpointStore(root / "checkpoints"),
                policy=CheckpointPolicy(event_interval=100),
                previous_sequence=harness.coordinator.cursor.head.sequence,
                monotonic_clock=clock,
            )

            step = harness.coordinator.dispatch_ready()
            result = publisher.observe(
                step.cursor.snapshot,
                step.cursor.graph_index,
                step.cursor.head,
                step.events[-1],
            )

            self.assertEqual(ExecutionCheckpointStatus.SKIPPED, result.status)
            self.assertFalse((root / "checkpoints" / "current.json").exists())

    def test_elapsed_time_publishes_after_one_new_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = CoordinatorHarness(root)
            clock = _Clock()
            publisher = ExecutionCheckpointPublisher(
                harness.manifest,
                harness.journal,
                CheckpointStore(root / "checkpoints"),
                policy=CheckpointPolicy(
                    event_interval=100,
                    time_interval_seconds=10,
                ),
                previous_sequence=harness.coordinator.cursor.head.sequence,
                monotonic_clock=clock,
            )
            clock.value = 10.0

            step = harness.coordinator.dispatch_ready()
            result = publisher.observe(
                step.cursor.snapshot,
                step.cursor.graph_index,
                step.cursor.head,
                step.events[-1],
            )

            self.assertEqual(ExecutionCheckpointStatus.PUBLISHED, result.status)

    def test_state_mismatch_blocks_all_later_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = CoordinatorHarness(root)
            publisher = ExecutionCheckpointPublisher(
                harness.manifest,
                harness.journal,
                CheckpointStore(root / "checkpoints"),
                policy=CheckpointPolicy(event_interval=1),
            )
            previous = harness.coordinator.cursor
            step = harness.coordinator.dispatch_ready()

            first = publisher.observe(
                previous.snapshot,
                previous.graph_index,
                previous.head,
                step.events[-1],
            )
            second = publisher.observe(
                step.cursor.snapshot,
                step.cursor.graph_index,
                step.cursor.head,
                step.events[-1],
            )

            self.assertEqual(ExecutionCheckpointReason.STATE_MISMATCH, first.reason)
            self.assertEqual(ExecutionCheckpointReason.ALREADY_BLOCKED, second.reason)
            self.assertTrue(publisher.blocked)

    def test_control_root_failure_blocks_instead_of_skipping_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = CoordinatorHarness(root)
            publisher = ExecutionCheckpointPublisher(
                harness.manifest,
                harness.journal,
                CheckpointStore(
                    root / "checkpoints",
                    control_root_validator=lambda: False,
                ),
                policy=CheckpointPolicy(event_interval=1),
                previous_sequence=harness.coordinator.cursor.head.sequence,
            )
            step = harness.coordinator.dispatch_ready()

            result = publisher.observe(
                step.cursor.snapshot,
                step.cursor.graph_index,
                step.cursor.head,
                step.events[-1],
            )

            self.assertEqual(ExecutionCheckpointStatus.BLOCKED, result.status)
            self.assertEqual(ExecutionCheckpointReason.PUBLISH_FAILED, result.reason)
            self.assertTrue(publisher.blocked)


if __name__ == "__main__":
    unittest.main()
