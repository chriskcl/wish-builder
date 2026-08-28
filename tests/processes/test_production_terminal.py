from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from tests.processes.test_coordinator import BASE_TIME, CoordinatorHarness
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts.runtime import JournalEventType, RuntimeState
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.processes.coordinator import CoordinatorCursor
from wish_builder.processes.production_terminal import ProductionTerminalFinalizer
from wish_builder.services.checkpoints import (
    CheckpointLoadStatus,
    CheckpointStore,
)
from wish_builder.services.execution_checkpoints import ExecutionCheckpointPublisher
from wish_builder.services.journal import DurableJournal
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    recover_coordinator_lease,
)


class _FailAtWrite:
    def __init__(self, selected: int) -> None:
        self._selected = selected
        self._writes = 0

    def __call__(self, point: str, requested_bytes: int | None = None) -> None:
        del requested_bytes
        if point != "segment_write":
            return
        self._writes += 1
        if self._writes == self._selected:
            raise OSError("injected terminal append failure")


class ProductionTerminalFinalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def _verified_cursor(harness: CoordinatorHarness) -> CoordinatorCursor:
        previous = harness.coordinator.cursor
        snapshot = dataclasses.replace(
            previous.snapshot,
            tasks=tuple(
                dataclasses.replace(
                    task,
                    state=RuntimeState.VERIFIED,
                    reason_code=None,
                )
                for task in previous.snapshot.tasks
            ),
        )
        return CoordinatorCursor(
            snapshot,
            GraphIndex.compile(harness.manifest, snapshot),
            previous.lease_state,
            previous.dispatch_recoveries,
        )

    @staticmethod
    def _lease_service(
        harness: CoordinatorHarness,
        journal: DurableJournal,
        journal_root: Path,
    ) -> CoordinatorLeaseService:
        return CoordinatorLeaseService(
            journal,
            lambda: recover_coordinator_lease(
                journal_root,
                harness.manifest,
                coordinator_epoch=1,
                repair_derived=False,
            ),
            run_id=harness.manifest.run_id,
            owner=harness.owner,
            manifest_digest=harness.manifest.canonical_sha256(),
            lease_ttl_seconds=harness.manifest.lease_ttl_seconds,
            lease_clock_skew_seconds=harness.manifest.lease_clock_skew_seconds,
        )

    def _finalizer(
        self,
        harness: CoordinatorHarness,
        journal: DurableJournal,
        cursor: CoordinatorCursor,
        *,
        store: CheckpointStore | None = None,
        recovered_terminal_event=None,
    ) -> ProductionTerminalFinalizer:
        journal_root = self.root / "control" / "journal"
        selected_store = store or CheckpointStore(self.root / "checkpoints")
        return ProductionTerminalFinalizer(
            harness.manifest,
            journal,
            self._lease_service(harness, journal, journal_root),
            ExecutionCheckpointPublisher(
                harness.manifest,
                journal,
                selected_store,
                previous_sequence=cursor.head.sequence,
            ),
            coordinator_id=harness.owner.actor.actor_id,
            fencing_token=cursor.snapshot.coordinator_epoch,
            recovered_terminal_event=recovered_terminal_event,
        )

    def test_writes_terminal_suffix_releases_lease_and_publishes_checkpoint(self) -> None:
        harness = CoordinatorHarness(self.root / "control")
        cursor = self._verified_cursor(harness)

        result = self._finalizer(harness, harness.journal, cursor).finish(cursor)

        self.assertTrue(result.completed)
        self.assertEqual(
            (
                JournalEventType.EXECUTION_COMPLETED,
                JournalEventType.INTEGRATION_VERIFIED,
                JournalEventType.QUALITY_DOCS_VERIFIED,
                JournalEventType.LEASE_RELEASED,
            ),
            tuple(event.event_type for event in result.events),
        )
        self.assertIs(result.cursor.snapshot.phase, RuntimeState.COMPLETE)
        self.assertFalse(result.cursor.lease_state.active)
        self.assertTrue(
            result.cursor.graph_index.verify(
                harness.manifest,
                result.cursor.snapshot,
            )
        )
        loaded = CheckpointStore(self.root / "checkpoints").load(
            harness.manifest,
            coordinator_epoch=1,
        )
        self.assertIs(loaded.status, CheckpointLoadStatus.LOADED)
        assert loaded.checkpoint is not None
        self.assertEqual(result.cursor.snapshot, loaded.checkpoint.snapshot)

    def test_each_append_failure_preserves_only_the_durable_prefix(self) -> None:
        expected_phases = (
            RuntimeState.EXECUTING,
            RuntimeState.INTEGRATION,
            RuntimeState.QUALITY_DOCS,
            RuntimeState.COMPLETE,
        )
        for failure_number in range(1, 5):
            with self.subTest(failure_number=failure_number):
                case_root = self.root / f"case-{failure_number}"
                harness = CoordinatorHarness(case_root / "control")
                cursor = self._verified_cursor(harness)
                journal_root = case_root / "control" / "journal"
                storage = FilesystemJournalStorage(
                    journal_root,
                    harness.manifest.run_id,
                    authority_clock=lambda: BASE_TIME,
                    fault_injector=_FailAtWrite(failure_number),
                )
                journal = DurableJournal(harness.manifest.run_id, storage)
                publisher = ExecutionCheckpointPublisher(
                    harness.manifest,
                    journal,
                    CheckpointStore(case_root / "checkpoints"),
                    previous_sequence=cursor.head.sequence,
                )
                finalizer = ProductionTerminalFinalizer(
                    harness.manifest,
                    journal,
                    self._lease_service(harness, journal, journal_root),
                    publisher,
                    coordinator_id=harness.owner.actor.actor_id,
                    fencing_token=cursor.snapshot.coordinator_epoch,
                )

                result = finalizer.finish(cursor)

                self.assertFalse(result.completed)
                self.assertEqual(failure_number - 1, len(result.events))
                self.assertIs(
                    result.cursor.snapshot.phase,
                    expected_phases[failure_number - 1],
                )
                self.assertTrue(result.cursor.lease_state.active)
                self.assertFalse((case_root / "checkpoints" / "current.json").exists())

    def test_restarts_from_a_partial_terminal_prefix(self) -> None:
        harness = CoordinatorHarness(self.root / "control")
        cursor = self._verified_cursor(harness)
        journal_root = self.root / "control" / "journal"
        failing_journal = DurableJournal(
            harness.manifest.run_id,
            FilesystemJournalStorage(
                journal_root,
                harness.manifest.run_id,
                authority_clock=lambda: BASE_TIME,
                fault_injector=_FailAtWrite(2),
            ),
        )
        first = self._finalizer(harness, failing_journal, cursor).finish(cursor)
        self.assertIs(first.cursor.snapshot.phase, RuntimeState.INTEGRATION)

        resumed_journal = DurableJournal(
            harness.manifest.run_id,
            FilesystemJournalStorage(
                journal_root,
                harness.manifest.run_id,
                authority_clock=lambda: BASE_TIME,
            ),
        )
        resumed = self._finalizer(
            harness,
            resumed_journal,
            first.cursor,
        ).finish(first.cursor)

        self.assertTrue(resumed.completed)
        self.assertEqual(
            (
                JournalEventType.INTEGRATION_VERIFIED,
                JournalEventType.QUALITY_DOCS_VERIFIED,
                JournalEventType.LEASE_RELEASED,
            ),
            tuple(event.event_type for event in resumed.events),
        )

    def test_retries_checkpoint_after_the_lease_was_already_released(self) -> None:
        harness = CoordinatorHarness(self.root / "control")
        cursor = self._verified_cursor(harness)
        blocked_store = CheckpointStore(
            self.root / "blocked-checkpoints",
            control_root_validator=lambda: False,
        )
        first = self._finalizer(
            harness,
            harness.journal,
            cursor,
            store=blocked_store,
        ).finish(cursor)
        self.assertFalse(first.completed)
        self.assertFalse(first.cursor.lease_state.active)
        terminal_event = first.events[-1]

        resumed = self._finalizer(
            harness,
            harness.journal,
            first.cursor,
            recovered_terminal_event=terminal_event,
        ).finish(first.cursor)

        self.assertTrue(resumed.completed)
        self.assertEqual((terminal_event,), resumed.events)


if __name__ == "__main__":
    unittest.main()
