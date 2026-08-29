from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
    decode_journal_event_bytes,
)
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.services.journal import (
    GENESIS_HEAD,
    AppendResult,
    AppendStatus,
    DurableJournal,
    JournalFaultCode,
    JournalHead,
    PersistenceFault,
    SegmentPolicy,
)


NOW = "2026-08-18T06:00:00Z"
RUN_ID = "RUN-001"


def journal_event(
    sequence: int,
    previous_hash: str,
    *,
    variant: int = 0,
    run_id: str = RUN_ID,
) -> JournalEvent:
    if sequence == 1:
        event_type = JournalEventType.RUN_INITIALIZED
        from_state = RuntimeState.NONE
        to_state = RuntimeState.PREFLIGHT
    else:
        event_type = JournalEventType.RUN_PAUSED
        from_state = RuntimeState.RUNNING
        to_state = RuntimeState.PAUSED
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-{sequence:03d}-{variant:03d}",
        event_type=event_type,
        identity=ExecutionIdentity(run_id, 0),
        actor_type=ActorType.SYSTEM,
        actor_id=f"wishctl-{variant:03d}",
        recorded_at=NOW,
        previous_event_hash=previous_hash,
        payload=TransitionPayload(
            TransitionSubject.RUN,
            from_state,
            to_state,
        ),
    )


def _process_append(
    root: str,
    event: JournalEvent,
    barrier: object,
    queue: object,
) -> None:
    storage = FilesystemJournalStorage(root, RUN_ID, lock_timeout_seconds=10)
    journal = DurableJournal(RUN_ID, storage)
    barrier.wait(timeout=10)  # type: ignore[attr-defined]
    result = journal.append(event, expected_head=GENESIS_HEAD)
    queue.put(  # type: ignore[attr-defined]
        (
            result.status.value,
            None if result.head is None else result.head.sequence,
            None if result.head is None else result.head.event_hash,
        )
    )


class JournalAppendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_journal(
        self,
        *,
        policy: SegmentPolicy = SegmentPolicy(),
    ) -> DurableJournal:
        return DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(self.root, RUN_ID),
            policy=policy,
        )

    def test_first_event_is_validated_hash_linked_and_fsynced_as_exact_jsonl(self) -> None:
        event = journal_event(1, GENESIS_HEAD.event_hash)
        result = self.make_journal().append(event, expected_head=GENESIS_HEAD)

        self.assertEqual(AppendStatus.COMMITTED, result.status)
        self.assertTrue(result.durable)
        self.assertEqual(JournalHead(1, event.event_hash), result.head)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(event.canonical_json_bytes(), segment.read_bytes())
        decoded = decode_journal_event_bytes(segment.read_bytes())
        self.assertTrue(decoded.ok, decoded.issues)
        self.assertEqual(event, decoded.value)
        self.assertFalse((self.root / "index.json").exists())

    def test_exact_retry_is_idempotent_and_conflicting_same_sequence_is_denied(self) -> None:
        accepted = journal_event(1, GENESIS_HEAD.event_hash, variant=1)
        conflict = journal_event(1, GENESIS_HEAD.event_hash, variant=2)
        first = self.make_journal().append(accepted, expected_head=GENESIS_HEAD)

        replay = self.make_journal().append(accepted, expected_head=GENESIS_HEAD)
        rejected = self.make_journal().append(conflict, expected_head=GENESIS_HEAD)

        self.assertEqual(AppendStatus.COMMITTED, first.status)
        self.assertEqual(AppendStatus.IDEMPOTENT, replay.status)
        self.assertEqual(AppendStatus.CONFLICT, rejected.status)
        self.assertEqual(first.head, replay.head)
        self.assertEqual(first.head, rejected.head)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(1, len(segment.read_bytes().splitlines()))

    def test_rotation_publishes_canonical_index_only_at_the_boundary(self) -> None:
        policy = SegmentPolicy(max_events=2, max_bytes=1024 * 1024)
        journal = self.make_journal(policy=policy)
        first = journal_event(1, GENESIS_HEAD.event_hash)
        first_result = journal.append(first, expected_head=GENESIS_HEAD)
        assert first_result.head is not None
        second = journal_event(2, first.event_hash)
        second_result = journal.append(second, expected_head=first_result.head)
        assert second_result.head is not None

        index_path = self.root / "index.json"
        self.assertFalse(index_path.exists())
        third = journal_event(3, second.event_hash)
        third_result = journal.append(third, expected_head=second_result.head)
        self.assertEqual(AppendStatus.COMMITTED, third_result.status)

        first_segment = self.root / "segments" / "segment-00000001.jsonl"
        second_segment = self.root / "segments" / "segment-00000002.jsonl"
        self.assertEqual(
            first.canonical_json_bytes() + second.canonical_json_bytes(),
            first_segment.read_bytes(),
        )
        self.assertEqual(third.canonical_json_bytes(), second_segment.read_bytes())
        index_bytes = index_path.read_bytes()
        index = json.loads(index_bytes)
        self.assertEqual(index_bytes, canonical_json_bytes(index))
        self.assertEqual(1, index["sealed_segment"])
        self.assertEqual(2, index["active_segment"])
        self.assertEqual(2, index["last_sequence"])
        self.assertEqual(second.event_hash, index["last_event_hash"])

        assert third_result.head is not None
        fourth = journal_event(4, third.event_hash)
        fourth_result = journal.append(fourth, expected_head=third_result.head)
        self.assertEqual(AppendStatus.COMMITTED, fourth_result.status)
        self.assertEqual(index_bytes, index_path.read_bytes())

    def test_multiple_rotations_and_restarts_continue_from_the_latest_verified_index(self) -> None:
        policy = SegmentPolicy(max_events=2)
        head = GENESIS_HEAD
        events: list[JournalEvent] = []
        for sequence in range(1, 8):
            current = journal_event(sequence, head.event_hash)
            result = self.make_journal(policy=policy).append(
                current,
                expected_head=head,
            )
            self.assertEqual(AppendStatus.COMMITTED, result.status)
            assert result.head is not None
            head = result.head
            events.append(current)

        segments = sorted((self.root / "segments").glob("*.jsonl"))
        self.assertEqual(4, len(segments))
        self.assertEqual(
            [2, 2, 2, 1],
            [len(path.read_bytes().splitlines()) for path in segments],
        )
        index = json.loads((self.root / "index.json").read_bytes())
        self.assertEqual(3, index["sealed_segment"])
        self.assertEqual(4, index["active_segment"])
        self.assertEqual(6, index["last_sequence"])
        self.assertEqual(events[5].event_hash, index["last_event_hash"])

    def test_segment_byte_limit_rotates_before_the_frame_that_would_exceed_it(self) -> None:
        first = journal_event(1, GENESIS_HEAD.event_hash)
        policy = SegmentPolicy(
            max_events=100,
            max_bytes=len(first.canonical_json_bytes()) + 1,
        )
        journal = self.make_journal(policy=policy)
        first_result = journal.append(first, expected_head=GENESIS_HEAD)
        assert first_result.head is not None
        second = journal_event(2, first.event_hash)
        second_result = journal.append(second, expected_head=first_result.head)

        self.assertEqual(AppendStatus.COMMITTED, second_result.status)
        first_segment = self.root / "segments" / "segment-00000001.jsonl"
        second_segment = self.root / "segments" / "segment-00000002.jsonl"
        self.assertEqual(first.canonical_json_bytes(), first_segment.read_bytes())
        self.assertEqual(second.canonical_json_bytes(), second_segment.read_bytes())
        self.assertTrue((self.root / "index.json").exists())

    def test_two_processes_compare_and_append_one_conflicting_gate_slot(self) -> None:
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        queue = context.Queue()
        events = (
            journal_event(1, GENESIS_HEAD.event_hash, variant=10),
            journal_event(1, GENESIS_HEAD.event_hash, variant=20),
        )
        processes = [
            context.Process(
                target=_process_append,
                args=(str(self.root), event, barrier, queue),
            )
            for event in events
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            self.assertFalse(process.is_alive())
            self.assertEqual(0, process.exitcode)
        results = [queue.get(timeout=2), queue.get(timeout=2)]

        self.assertEqual(
            [AppendStatus.COMMITTED.value, AppendStatus.CONFLICT.value],
            sorted(result[0] for result in results),
        )
        self.assertEqual({1}, {result[1] for result in results})
        self.assertEqual(1, len({result[2] for result in results}))
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(1, len(segment.read_bytes().splitlines()))

    def test_service_rejects_wrong_run_sequence_or_previous_hash_before_storage(self) -> None:
        journal = self.make_journal()
        wrong_run = journal_event(
            1,
            GENESIS_HEAD.event_hash,
            run_id="RUN-OTHER",
        )
        with self.assertRaisesRegex(ValueError, "run_id"):
            journal.append(wrong_run, expected_head=GENESIS_HEAD)

        wrong_sequence = journal_event(2, GENESIS_HEAD.event_hash)
        with self.assertRaisesRegex(ValueError, "sequence"):
            journal.append(wrong_sequence, expected_head=GENESIS_HEAD)

        expected = JournalHead(1, "sha256:" + "1" * 64)
        wrong_previous = journal_event(2, "sha256:" + "2" * 64)
        with self.assertRaisesRegex(ValueError, "previous hash"):
            journal.append(wrong_previous, expected_head=expected)

    def test_service_models_reject_impossible_states(self) -> None:
        hash_one = "sha256:" + "1" * 64
        first = journal_event(1, GENESIS_HEAD.event_hash)
        invalid_heads = (
            (-1, hash_one),
            (True, hash_one),
            (1, "bad"),
            (0, hash_one),
        )
        for arguments in invalid_heads:
            with self.subTest(head=arguments), self.assertRaises(ValueError):
                JournalHead(*arguments)  # type: ignore[arg-type]

        for arguments in ((0, 1), (True, 1), (1, 0), (1, True)):
            with self.subTest(policy=arguments), self.assertRaises(ValueError):
                SegmentPolicy(*arguments)

        invalid_results = (
            lambda: AppendResult("committed", GENESIS_HEAD),
            lambda: AppendResult(AppendStatus.CONFLICT, object()),
            lambda: AppendResult(AppendStatus.COMMITTED, GENESIS_HEAD, object()),
            lambda: AppendResult(
                AppendStatus.PERSISTENCE_FAILED,
                None,
                fault_code="disk_full",
            ),
            lambda: AppendResult(AppendStatus.COMMITTED, None),
            lambda: AppendResult(AppendStatus.COMMITTED, GENESIS_HEAD, first),
            lambda: AppendResult(AppendStatus.CONFLICT, None),
            lambda: AppendResult(
                AppendStatus.PERSISTENCE_FAILED,
                None,
                first,
                JournalFaultCode.DISK_FULL,
            ),
            lambda: AppendResult(AppendStatus.PERSISTENCE_FAILED, None),
        )
        for index, build in enumerate(invalid_results):
            with self.subTest(result=index), self.assertRaises((TypeError, ValueError)):
                build()

        invalid_faults = (
            lambda: PersistenceFault("disk_full", "write"),
            lambda: PersistenceFault(JournalFaultCode.DISK_FULL, ""),
            lambda: PersistenceFault(
                JournalFaultCode.DISK_FULL,
                "write",
                last_committed_head=object(),
            ),
            lambda: PersistenceFault(
                JournalFaultCode.DISK_FULL,
                "write",
                os_error=-1,
            ),
        )
        for index, build in enumerate(invalid_faults):
            with self.subTest(fault=index), self.assertRaises((TypeError, ValueError)):
                build()

    def test_service_validates_boundary_types_decoder_and_storage_result(self) -> None:
        class InvalidStorage:
            def compare_and_append(self, **_: object) -> object:
                return object()

        first = journal_event(1, GENESIS_HEAD.event_hash)
        with self.assertRaises(ValueError):
            DurableJournal("", InvalidStorage())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            DurableJournal(RUN_ID, InvalidStorage(), policy=object())  # type: ignore[arg-type]

        journal = DurableJournal(RUN_ID, InvalidStorage())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            journal.append(object(), expected_head=GENESIS_HEAD)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            journal.append(first, expected_head=object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            journal.append(first, expected_head=GENESIS_HEAD)

        with mock.patch(
            "wish_builder.services.journal.decode_journal_event_bytes",
            return_value=SimpleNamespace(ok=False, value=None),
        ):
            with self.assertRaisesRegex(ValueError, "strict"):
                self.make_journal().append(first, expected_head=GENESIS_HEAD)
        with (
            mock.patch(
                "wish_builder.services.journal.decode_journal_event_bytes",
                return_value=SimpleNamespace(ok=True, value=first),
            ),
            mock.patch.object(
                JournalEvent,
                "canonical_json_bytes",
                return_value=b"{}\n\n",
            ),
        ):
            with self.assertRaisesRegex(ValueError, "delimiter"):
                self.make_journal().append(first, expected_head=GENESIS_HEAD)


if __name__ == "__main__":
    unittest.main()
