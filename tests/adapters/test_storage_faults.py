from __future__ import annotations

import errno
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wish_builder.adapters.git_identity import ProtectedControlRoot
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    DEFAULT_DECODE_LIMITS,
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.services.journal import (
    GENESIS_HEAD,
    AppendStatus,
    DurableJournal,
    JournalFaultCode,
    JournalHead,
    PersistenceFault,
    SegmentPolicy,
)

NOW = "2026-08-18T06:00:00Z"
RUN_ID = "RUN-FAULTS"


def event(sequence: int, previous_hash: str) -> JournalEvent:
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-{sequence:03d}",
        event_type=(
            JournalEventType.RUN_INITIALIZED
            if sequence == 1
            else JournalEventType.RUN_PAUSED
        ),
        identity=ExecutionIdentity(RUN_ID, 0),
        actor_type=ActorType.SYSTEM,
        actor_id="wishctl",
        recorded_at=NOW,
        previous_event_hash=previous_hash,
        payload=TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.NONE if sequence == 1 else RuntimeState.RUNNING,
            RuntimeState.PREFLIGHT if sequence == 1 else RuntimeState.PAUSED,
        ),
    )


class FaultScript:
    def __init__(self) -> None:
        self.actions: dict[str, object] = {}
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def set(self, point: str, action: object) -> None:
        with self._lock:
            self.actions[point] = action

    def __call__(self, point: str, requested_bytes: int | None = None) -> int | None:
        with self._lock:
            self.calls.append(point)
            action = self.actions.pop(point, None)
        if isinstance(action, BaseException):
            raise action
        if action is not None:
            return action  # type: ignore[return-value]
        return None


class StorageFaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"
        self.faults = FaultScript()
        self.storage = FilesystemJournalStorage(
            self.root,
            RUN_ID,
            fault_injector=self.faults,
        )
        self.journal = DurableJournal(RUN_ID, self.storage)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lock_open_and_acquire_faults_are_named(self) -> None:
        for point, expected in (
            ("lock_open", JournalFaultCode.LOCK_OPEN_FAILED),
            ("lock_acquire", JournalFaultCode.LOCK_ACQUIRE_FAILED),
        ):
            with self.subTest(point=point):
                root = Path(self.temporary.name) / point
                faults = FaultScript()
                faults.set(point, OSError(errno.EIO, "injected"))
                journal = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID, fault_injector=faults),
                )
                result = journal.append(
                    event(1, GENESIS_HEAD.event_hash),
                    expected_head=GENESIS_HEAD,
                )
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
                self.assertEqual(expected, result.fault_code)
                self.assertTrue(journal.blocked)

    def test_control_root_replacement_at_a_write_boundary_stops_all_writes(
        self,
    ) -> None:
        control_root = Path(self.temporary.name) / "control-root"
        moved = Path(self.temporary.name) / "control-root-original"
        control_root.mkdir()
        replaced = False

        def replace_root(point: str, requested_bytes: int | None = None) -> None:
            nonlocal replaced
            if point == "layout_open" and not replaced:
                replaced = True
                control_root.rename(moved)
                control_root.mkdir()

        with ProtectedControlRoot.open(control_root) as protected:
            guarded_storage = FilesystemJournalStorage(
                control_root / "journal",
                RUN_ID,
                fault_injector=replace_root,
                control_root=protected,
            )
            guarded_journal = DurableJournal(RUN_ID, guarded_storage)
            result = guarded_journal.append(
                event(1, GENESIS_HEAD.event_hash),
                expected_head=GENESIS_HEAD,
            )
            self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
            self.assertEqual(JournalFaultCode.CONTROL_ROOT_DRIFT, result.fault_code)
            self.assertTrue(guarded_journal.blocked)
            self.assertEqual([], list(control_root.iterdir()))
            self.assertEqual([], list(moved.iterdir()))

        outside = Path(self.temporary.name) / "outside"
        with (
            ProtectedControlRoot.open(control_root) as protected,
            self.assertRaisesRegex(ValueError, "inside control_root"),
        ):
            FilesystemJournalStorage(outside, RUN_ID, control_root=protected)

    def test_segment_open_permission_and_disk_full_are_distinct(self) -> None:
        cases = (
            (
                "segment_open",
                PermissionError(errno.EACCES, "injected"),
                JournalFaultCode.PERMISSION_DENIED,
            ),
            (
                "segment_write",
                OSError(errno.ENOSPC, "injected"),
                JournalFaultCode.DISK_FULL,
            ),
            (
                "segment_open",
                OSError(errno.EIO, "injected"),
                JournalFaultCode.SEGMENT_OPEN_FAILED,
            ),
        )
        for index, (point, fault, expected) in enumerate(cases):
            with self.subTest(point=point, expected=expected):
                root = Path(self.temporary.name) / f"case-{index}"
                faults = FaultScript()
                faults.set(point, fault)
                journal = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID, fault_injector=faults),
                )
                result = journal.append(
                    event(1, GENESIS_HEAD.event_hash),
                    expected_head=GENESIS_HEAD,
                )
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
                self.assertEqual(expected, result.fault_code)

    def test_short_write_is_not_retried_and_poisoned_tail_blocks_later_append(
        self,
    ) -> None:
        first = event(1, GENESIS_HEAD.event_hash)
        self.faults.set("segment_write", 1)
        result = self.journal.append(first, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
        self.assertEqual(JournalFaultCode.SHORT_WRITE, result.fault_code)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(first.canonical_json_bytes()[:1], segment.read_bytes())

        blocked = self.journal.append(first, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, blocked.status)
        self.assertEqual(JournalFaultCode.SHORT_WRITE, blocked.fault_code)
        recovered_instance = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(self.root, RUN_ID),
        )
        corrupt = recovered_instance.append(first, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, corrupt.status)
        self.assertEqual(JournalFaultCode.JOURNAL_CORRUPT, corrupt.fault_code)

    def test_flush_or_fsync_failure_closes_service_and_exact_retry_redurabilizes(
        self,
    ) -> None:
        for index, (point, expected) in enumerate(
            (
                ("segment_flush", JournalFaultCode.FLUSH_FAILED),
                ("segment_fsync", JournalFaultCode.FSYNC_FAILED),
            )
        ):
            with self.subTest(point=point):
                root = Path(self.temporary.name) / f"durability-{index}"
                faults = FaultScript()
                faults.set(point, OSError(errno.EIO, "injected"))
                first = event(1, GENESIS_HEAD.event_hash)
                journal = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID, fault_injector=faults),
                )
                failed = journal.append(first, expected_head=GENESIS_HEAD)
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, failed.status)
                self.assertEqual(expected, failed.fault_code)
                self.assertTrue(journal.blocked)

                retried = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID),
                ).append(first, expected_head=GENESIS_HEAD)
                self.assertEqual(AppendStatus.IDEMPOTENT, retried.status)
                self.assertTrue(retried.durable)

    def test_rotation_replace_and_parent_sync_fail_before_incoming_event(self) -> None:
        for index, (point, expected) in enumerate(
            (
                ("index_replace", JournalFaultCode.ATOMIC_PUBLISH_FAILED),
                ("index_parent_sync", JournalFaultCode.DIRECTORY_SYNC_FAILED),
                ("segment_parent_sync", JournalFaultCode.DIRECTORY_SYNC_FAILED),
            )
        ):
            with self.subTest(point=point):
                root = Path(self.temporary.name) / f"rotation-{index}"
                faults = FaultScript()
                storage = FilesystemJournalStorage(root, RUN_ID, fault_injector=faults)
                journal = DurableJournal(
                    RUN_ID,
                    storage,
                    policy=SegmentPolicy(max_events=1),
                )
                first = event(1, GENESIS_HEAD.event_hash)
                first_result = journal.append(first, expected_head=GENESIS_HEAD)
                self.assertEqual(AppendStatus.COMMITTED, first_result.status)
                faults.set(point, OSError(errno.EIO, "injected"))
                second = event(2, first.event_hash)
                failed = journal.append(
                    second,
                    expected_head=first_result.head,  # type: ignore[arg-type]
                )
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, failed.status)
                self.assertEqual(expected, failed.fault_code)
                second_segment = root / "segments" / "segment-00000002.jsonl"
                if second_segment.exists():
                    self.assertEqual(b"", second_segment.read_bytes())
                self.assertNotIn(
                    second.canonical_json_bytes(),
                    b"".join(
                        path.read_bytes()
                        for path in (root / "segments").glob("*.jsonl")
                    ),
                )

    def test_index_flush_and_fsync_failpoints_are_distinct(self) -> None:
        for index, (point, expected) in enumerate(
            (
                ("index_flush", JournalFaultCode.FLUSH_FAILED),
                ("index_fsync", JournalFaultCode.FSYNC_FAILED),
            )
        ):
            with self.subTest(point=point):
                root = Path(self.temporary.name) / f"index-{index}"
                faults = FaultScript()
                journal = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID, fault_injector=faults),
                    policy=SegmentPolicy(max_events=1),
                )
                first = event(1, GENESIS_HEAD.event_hash)
                committed = journal.append(first, expected_head=GENESIS_HEAD)
                faults.set(point, OSError(errno.EIO, "injected"))
                failed = journal.append(
                    event(2, first.event_hash),
                    expected_head=committed.head,  # type: ignore[arg-type]
                )
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, failed.status)
                self.assertEqual(expected, failed.fault_code)
                self.assertFalse((root / "index.json").exists())

    def test_complete_corruption_blocks_append_without_extending_the_file(self) -> None:
        first = event(1, GENESIS_HEAD.event_hash)
        committed = self.journal.append(first, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.COMMITTED, committed.status)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        with segment.open("ab") as handle:
            handle.write(b"{not-json}\n")
        before = segment.read_bytes()

        second = event(2, first.event_hash)
        result = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(self.root, RUN_ID),
        ).append(second, expected_head=committed.head)  # type: ignore[arg-type]
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
        self.assertEqual(JournalFaultCode.JOURNAL_CORRUPT, result.fault_code)
        self.assertEqual(before, segment.read_bytes())

    def test_one_committed_event_has_one_append_flush_and_fsync_without_index_publish(
        self,
    ) -> None:
        result = self.journal.append(
            event(1, GENESIS_HEAD.event_hash),
            expected_head=GENESIS_HEAD,
        )
        self.assertEqual(AppendStatus.COMMITTED, result.status)
        self.assertEqual(1, self.faults.calls.count("segment_write"))
        self.assertEqual(1, self.faults.calls.count("segment_flush"))
        self.assertEqual(1, self.faults.calls.count("segment_fsync"))
        self.assertNotIn("index_write", self.faults.calls)
        self.assertNotIn("index_replace", self.faults.calls)

    def test_constructor_request_and_failpoint_contracts_are_closed(self) -> None:
        with self.assertRaises(ValueError):
            FilesystemJournalStorage(self.root, "")
        for timeout in (0, -1, True, "1", float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                FilesystemJournalStorage(
                    self.root,
                    RUN_ID,
                    lock_timeout_seconds=timeout,  # type: ignore[arg-type]
                )

        first = event(1, GENESIS_HEAD.event_hash)
        frame = first.canonical_json_bytes()
        invalid = (
            {"event": object()},
            {"frame": "bytes"},
            {"expected_head": object()},
            {"policy": object()},
            {"frame": b"{}\n"},
        )
        for changes in invalid:
            arguments: dict[str, object] = {
                "event": first,
                "frame": frame,
                "expected_head": GENESIS_HEAD,
                "policy": SegmentPolicy(),
            }
            arguments.update(changes)
            with (
                self.subTest(changes=changes),
                self.assertRaises((TypeError, ValueError)),
            ):
                self.storage.compare_and_append(**arguments)  # type: ignore[arg-type]

        other_storage = FilesystemJournalStorage(self.root, "RUN-OTHER")
        with self.assertRaisesRegex(ValueError, "run"):
            other_storage.compare_and_append(
                event=first,
                frame=frame,
                expected_head=GENESIS_HEAD,
                policy=SegmentPolicy(),
            )
        second = event(2, GENESIS_HEAD.event_hash)
        with self.assertRaisesRegex(ValueError, "sequence"):
            self.storage.compare_and_append(
                event=second,
                frame=second.canonical_json_bytes(),
                expected_head=GENESIS_HEAD,
                policy=SegmentPolicy(),
            )
        wrong_expected = JournalHead(1, "sha256:" + "1" * 64)
        with self.assertRaisesRegex(ValueError, "previous hash"):
            self.storage.compare_and_append(
                event=second,
                frame=second.canonical_json_bytes(),
                expected_head=wrong_expected,
                policy=SegmentPolicy(),
            )

        faults = FaultScript()
        faults.set("layout_open", "invalid")
        invalid_failpoint = FilesystemJournalStorage(
            Path(self.temporary.name) / "invalid-failpoint",
            RUN_ID,
            fault_injector=faults,
        )
        with self.assertRaises(TypeError):
            DurableJournal(RUN_ID, invalid_failpoint).append(
                first,
                expected_head=GENESIS_HEAD,
            )

        with (
            mock.patch(
                "wish_builder.adapters.storage.filesystem.decode_journal_event_bytes",
                return_value=SimpleNamespace(ok=False, value=None),
            ),
            self.assertRaisesRegex(ValueError, "decode"),
        ):
            self.storage.compare_and_append(
                event=first,
                frame=frame,
                expected_head=GENESIS_HEAD,
                policy=SegmentPolicy(),
            )

    def test_real_write_flush_and_fsync_errors_use_the_same_named_faults(self) -> None:
        handle = mock.Mock()
        handle.write.side_effect = OSError(errno.EIO, "injected")
        with self.assertRaises(PersistenceFault) as write_context:
            self.storage._write_exact(
                handle,
                b"frame\n",
                point="segment_write",
                head=GENESIS_HEAD,
            )
        self.assertEqual(JournalFaultCode.WRITE_FAILED, write_context.exception.code)

        handle = mock.Mock()
        handle.flush.side_effect = OSError(errno.EIO, "injected")
        with self.assertRaises(PersistenceFault) as flush_context:
            self.storage._flush_file(handle, "segment_flush", GENESIS_HEAD)
        self.assertEqual(JournalFaultCode.FLUSH_FAILED, flush_context.exception.code)

        first = event(1, GENESIS_HEAD.event_hash)
        committed = self.journal.append(first, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.COMMITTED, committed.status)
        with mock.patch(
            "wish_builder.adapters.storage.filesystem.os.fsync",
            side_effect=OSError(errno.EIO, "injected"),
        ):
            failed = DurableJournal(
                RUN_ID,
                FilesystemJournalStorage(self.root, RUN_ID),
            ).append(
                event(2, first.event_hash),
                expected_head=committed.head,  # type: ignore[arg-type]
            )
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, failed.status)
        self.assertEqual(JournalFaultCode.FSYNC_FAILED, failed.fault_code)

    def test_oversized_event_and_layout_creation_fail_closed_before_append(
        self,
    ) -> None:
        first = event(1, GENESIS_HEAD.event_hash)
        oversized = DurableJournal(
            RUN_ID,
            self.storage,
            policy=SegmentPolicy(max_bytes=1),
        ).append(first, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, oversized.status)
        self.assertEqual(JournalFaultCode.EVENT_TOO_LARGE, oversized.fault_code)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(b"", segment.read_bytes())

        file_root = Path(self.temporary.name) / "not-a-directory"
        file_root.write_text("occupied", encoding="utf-8")
        invalid_layout = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(file_root, RUN_ID),
        ).append(first, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, invalid_layout.status)
        self.assertEqual(JournalFaultCode.INVALID_LAYOUT, invalid_layout.fault_code)

    def test_segment_creation_rotation_and_observed_head_faults_are_named(self) -> None:
        creation_cases = (
            ("segment_create", JournalFaultCode.SEGMENT_OPEN_FAILED),
            ("segment_create_flush", JournalFaultCode.FLUSH_FAILED),
            ("segment_create_fsync", JournalFaultCode.FSYNC_FAILED),
        )
        for index, (point, expected) in enumerate(creation_cases):
            with self.subTest(point=point):
                root = Path(self.temporary.name) / f"create-{index}"
                faults = FaultScript()
                faults.set(point, OSError(errno.EIO, "injected"))
                result = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID, fault_injector=faults),
                ).append(event(1, GENESIS_HEAD.event_hash), expected_head=GENESIS_HEAD)
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
                self.assertEqual(expected, result.fault_code)

        root = Path(self.temporary.name) / "rotation-create"
        faults = FaultScript()
        journal = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(root, RUN_ID, fault_injector=faults),
            policy=SegmentPolicy(max_events=1),
        )
        first = event(1, GENESIS_HEAD.event_hash)
        committed = journal.append(first, expected_head=GENESIS_HEAD)
        faults.set("rotation_create", OSError(errno.EIO, "injected"))
        failed = journal.append(
            event(2, first.event_hash),
            expected_head=committed.head,  # type: ignore[arg-type]
        )
        self.assertEqual(JournalFaultCode.ROTATION_FAILED, failed.fault_code)

        for index, point in enumerate(("observed_head_open", "observed_head_fsync")):
            with self.subTest(point=point):
                root = Path(self.temporary.name) / f"observed-{index}"
                first = event(1, GENESIS_HEAD.event_hash)
                base = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID),
                ).append(first, expected_head=GENESIS_HEAD)
                faults = FaultScript()
                faults.set(point, OSError(errno.EIO, "injected"))
                result = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID, fault_injector=faults),
                ).append(first, expected_head=GENESIS_HEAD)
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
                self.assertEqual(
                    JournalFaultCode.SEGMENT_OPEN_FAILED
                    if point == "observed_head_open"
                    else JournalFaultCode.FSYNC_FAILED,
                    result.fault_code,
                )
                self.assertEqual(base.head, result.head)

    def _build_rotated_journal(self, root: Path) -> tuple[JournalEvent, JournalEvent]:
        journal = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(root, RUN_ID),
            policy=SegmentPolicy(max_events=1),
        )
        first = event(1, GENESIS_HEAD.event_hash)
        first_result = journal.append(first, expected_head=GENESIS_HEAD)
        second = event(2, first.event_hash)
        second_result = journal.append(
            second,
            expected_head=first_result.head,  # type: ignore[arg-type]
        )
        self.assertEqual(AppendStatus.COMMITTED, second_result.status)
        return first, second

    def test_corrupt_index_shapes_and_mismatches_all_block_append(self) -> None:
        def mutate_duplicate(raw: bytes) -> bytes:
            return raw.replace(
                b'{"active_segment":', b'{"active_segment":2,"active_segment":', 1
            )

        def mutate_noncanonical(raw: bytes) -> bytes:
            return b" " + raw

        def mutate_missing(primitive: dict[str, object]) -> dict[str, object]:
            primitive.pop("event_count")
            return primitive

        def mutate_version(primitive: dict[str, object]) -> dict[str, object]:
            primitive["index_version"] = True
            return primitive

        def mutate_integer(primitive: dict[str, object]) -> dict[str, object]:
            primitive["event_count"] = 0
            return primitive

        def mutate_hash(primitive: dict[str, object]) -> dict[str, object]:
            primitive["last_event_hash"] = "sha256:BAD"
            return primitive

        def mutate_mismatch(primitive: dict[str, object]) -> dict[str, object]:
            primitive["last_event_hash"] = "sha256:" + "f" * 64
            return primitive

        raw_mutations = (mutate_duplicate, mutate_noncanonical)
        object_mutations = (
            mutate_missing,
            mutate_version,
            mutate_integer,
            mutate_hash,
            mutate_mismatch,
        )
        case = 0
        for mutation in (*raw_mutations, *object_mutations):
            with self.subTest(mutation=mutation.__name__):
                root = Path(self.temporary.name) / f"index-corrupt-{case}"
                case += 1
                _, second = self._build_rotated_journal(root)
                index_path = root / "index.json"
                raw = index_path.read_bytes()
                if mutation in raw_mutations:
                    changed = mutation(raw)  # type: ignore[arg-type]
                else:
                    changed = canonical_json_bytes(
                        mutation(json.loads(raw))  # type: ignore[arg-type]
                    )
                index_path.write_bytes(changed)  # type: ignore[arg-type]
                result = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID),
                ).append(
                    event(3, second.event_hash),
                    expected_head=type(GENESIS_HEAD)(2, second.event_hash),
                )
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
                self.assertEqual(JournalFaultCode.JOURNAL_CORRUPT, result.fault_code)

        root = Path(self.temporary.name) / "index-oversized"
        _, second = self._build_rotated_journal(root)
        (root / "index.json").write_bytes(b"x" * (16 * 1024 + 1))
        oversized = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(root, RUN_ID),
        ).append(
            event(3, second.event_hash),
            expected_head=type(GENESIS_HEAD)(2, second.event_hash),
        )
        self.assertEqual(JournalFaultCode.JOURNAL_CORRUPT, oversized.fault_code)

    def test_missing_or_unindexed_active_segment_never_discards_tail(self) -> None:
        root = Path(self.temporary.name) / "missing-active"
        _, second = self._build_rotated_journal(root)
        (root / "segments" / "segment-00000002.jsonl").unlink()
        missing = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(root, RUN_ID),
        ).append(
            event(3, second.event_hash),
            expected_head=type(GENESIS_HEAD)(2, second.event_hash),
        )
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, missing.status)
        self.assertEqual(JournalFaultCode.INVALID_LAYOUT, missing.fault_code)

        root = Path(self.temporary.name) / "missing-index"
        _, second = self._build_rotated_journal(root)
        (root / "index.json").unlink()
        unindexed = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(root, RUN_ID),
        ).append(
            event(3, second.event_hash),
            expected_head=type(GENESIS_HEAD)(2, second.event_hash),
        )
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, unindexed.status)
        self.assertEqual(JournalFaultCode.INVALID_LAYOUT, unindexed.fault_code)

    def test_oversized_noncanonical_wrong_run_and_broken_chain_segments_block(
        self,
    ) -> None:
        cases: list[tuple[str, bytes]] = []
        first = event(1, GENESIS_HEAD.event_hash)
        cases.append(("noncanonical", b" " + first.canonical_json_bytes()))
        wrong_run = JournalEvent.create(
            sequence=1,
            event_id="EVENT-WRONG-RUN",
            event_type=JournalEventType.RUN_INITIALIZED,
            identity=ExecutionIdentity("RUN-OTHER", 0),
            actor_type=ActorType.SYSTEM,
            actor_id="wishctl",
            recorded_at=NOW,
            previous_event_hash=GENESIS_HEAD.event_hash,
            payload=TransitionPayload(
                TransitionSubject.RUN,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
        )
        cases.append(("wrong-run", wrong_run.canonical_json_bytes()))
        broken = event(2, "sha256:" + "f" * 64)
        cases.append(("broken-chain", broken.canonical_json_bytes()))
        cases.append(("oversized", b"x" * (DEFAULT_DECODE_LIMITS.max_bytes + 1)))

        for index, (name, payload) in enumerate(cases):
            with self.subTest(name=name):
                root = Path(self.temporary.name) / f"segment-corrupt-{index}"
                segments = root / "segments"
                segments.mkdir(parents=True)
                (segments / "segment-00000001.jsonl").write_bytes(payload)
                result = DurableJournal(
                    RUN_ID,
                    FilesystemJournalStorage(root, RUN_ID),
                ).append(first, expected_head=GENESIS_HEAD)
                self.assertEqual(AppendStatus.PERSISTENCE_FAILED, result.status)
                self.assertEqual(JournalFaultCode.JOURNAL_CORRUPT, result.fault_code)

        root = Path(self.temporary.name) / "segment-event-limit"
        segments = root / "segments"
        segments.mkdir(parents=True)
        second = event(2, first.event_hash)
        (segments / "segment-00000001.jsonl").write_bytes(
            first.canonical_json_bytes() + second.canonical_json_bytes()
        )
        limited = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(root, RUN_ID),
            policy=SegmentPolicy(max_events=1),
        ).append(first, expected_head=GENESIS_HEAD)
        self.assertEqual(AppendStatus.PERSISTENCE_FAILED, limited.status)
        self.assertEqual(JournalFaultCode.JOURNAL_CORRUPT, limited.fault_code)


if __name__ == "__main__":
    unittest.main()
