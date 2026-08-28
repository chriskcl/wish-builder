from __future__ import annotations

import errno
import sys
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.adapters.test_storage_faults import RUN_ID, event
from wish_builder.adapters.storage import filesystem
from wish_builder.adapters.storage.filesystem import FilesystemJournalStorage
from wish_builder.contracts import (
    ActorType,
    ExecutionIdentity,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.services.journal import (
    GENESIS_HEAD,
    JournalEventDraft,
    JournalFaultCode,
    JournalHead,
    PersistenceFault,
    SegmentPolicy,
)


def run_draft(run_id: str = RUN_ID) -> JournalEventDraft:
    return JournalEventDraft(
        event_id="EVENT-DRAFT-COVERAGE",
        event_type=JournalEventType.RUN_INITIALIZED,
        identity=ExecutionIdentity(run_id, 0),
        actor_type=ActorType.SYSTEM,
        actor_id="coverage-test",
        payload=TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.NONE,
            RuntimeState.PREFLIGHT,
        ),
    )


def descriptor(
    path: Path,
    *,
    number: int = 1,
    event_count: int = 0,
) -> filesystem._SegmentDescriptor:
    return filesystem._SegmentDescriptor(
        number=number,
        path=path,
        start_head=GENESIS_HEAD,
        head=GENESIS_HEAD,
        event_count=event_count,
        byte_count=0,
        content_hash="sha256:" + "0" * 64,
        last_event=None,
    )


def index(*, sealed: int, active: int) -> filesystem._IndexRecord:
    return filesystem._IndexRecord(
        sealed_segment=sealed,
        active_segment=active,
        start_sequence=1,
        previous_event_hash=GENESIS_HEAD.event_hash,
        last_sequence=1,
        last_event_hash="sha256:" + "1" * 64,
        event_count=1,
        byte_count=1,
        segment_hash="sha256:" + "2" * 64,
    )


class FilesystemBranchBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"
        self.storage = FilesystemJournalStorage(self.root, RUN_ID)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_constructor_and_draft_interfaces_reject_untrusted_values(self) -> None:
        with self.assertRaisesRegex(TypeError, "authority_clock"):
            FilesystemJournalStorage(self.root, RUN_ID, authority_clock=object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "control_root"):
            FilesystemJournalStorage(self.root, RUN_ID, control_root=object())  # type: ignore[arg-type]

        valid = {
            "draft": run_draft(),
            "expected_head": GENESIS_HEAD,
            "policy": SegmentPolicy(),
            "validate_event": None,
        }
        invalid = (
            ({"draft": object()}, TypeError),
            ({"expected_head": object()}, TypeError),
            ({"policy": object()}, TypeError),
            ({"validate_event": object()}, TypeError),
            ({"draft": run_draft("RUN-OTHER")}, ValueError),
        )
        for changes, error_type in invalid:
            arguments = dict(valid)
            arguments.update(changes)
            with self.subTest(changes=changes), self.assertRaises(error_type):
                self.storage.compare_and_append_draft(**arguments)  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            self.storage.current_position(
                expected_head=object(),  # type: ignore[arg-type]
                policy=SegmentPolicy(),
            )
        with self.assertRaises(TypeError):
            self.storage.current_position(
                expected_head=GENESIS_HEAD,
                policy=object(),  # type: ignore[arg-type]
            )

    def test_authority_clock_errors_fail_closed_before_materialization(self) -> None:
        active = descriptor(self.root / "segment.jsonl")
        clocks = (
            lambda: (_ for _ in ()).throw(RuntimeError("clock failed")),
            lambda: "2026-08-20T00:00:00Z",
            lambda: datetime(2026, 8, 20),
        )
        for clock in clocks:
            storage = FilesystemJournalStorage(
                self.root,
                RUN_ID,
                authority_clock=clock,
            )
            with self.subTest(clock=clock), self.assertRaises(
                PersistenceFault
            ) as raised:
                storage._read_authority_time(active)
            self.assertEqual(JournalFaultCode.CLOCK_ROLLBACK, raised.exception.code)

    def test_lock_open_failure_closes_an_unowned_descriptor(self) -> None:
        close = mock.Mock()
        with (
            mock.patch.object(filesystem.os, "open", return_value=101),
            mock.patch.object(
                filesystem.os,
                "fdopen",
                side_effect=OSError(errno.EACCES, "denied"),
            ),
            mock.patch.object(filesystem.os, "close", close),
            self.assertRaises(PersistenceFault) as raised,
        ):
            with self.storage._append_lock():
                self.fail("the lock body must not run")
        close.assert_called_once_with(101)
        self.assertEqual(JournalFaultCode.PERMISSION_DENIED, raised.exception.code)

        close.reset_mock()
        with (
            mock.patch.object(
                filesystem.os,
                "open",
                side_effect=OSError(errno.EACCES, "denied"),
            ),
            mock.patch.object(filesystem.os, "close", close),
            self.assertRaises(PersistenceFault),
        ):
            with self.storage._append_lock():
                self.fail("the lock body must not run")
        close.assert_not_called()

    def test_short_lock_initialization_closes_the_open_handle(self) -> None:
        handle = mock.MagicMock()
        handle.fileno.return_value = 102
        handle.write.return_value = 0
        with (
            mock.patch.object(filesystem.os, "open", return_value=102),
            mock.patch.object(filesystem.os, "fdopen", return_value=handle),
            mock.patch.object(
                filesystem.os,
                "fstat",
                return_value=SimpleNamespace(st_size=0),
            ),
            self.assertRaises(PersistenceFault) as raised,
        ):
            with self.storage._append_lock():
                self.fail("the lock body must not run")
        handle.close.assert_called_once_with()
        self.assertEqual(JournalFaultCode.SHORT_WRITE, raised.exception.code)

    def test_segment_number_gaps_are_rejected(self) -> None:
        self.storage.segments.mkdir(parents=True)
        (self.storage.segments / "segment-00000001.jsonl").touch()
        (self.storage.segments / "segment-00000003.jsonl").touch()
        with self.assertRaises(PersistenceFault) as raised:
            self.storage._segment_paths()
        self.assertEqual(JournalFaultCode.INVALID_LAYOUT, raised.exception.code)
        self.assertIn("segment_sequence", str(raised.exception))

    def test_segment_listing_ignores_unrelated_files(self) -> None:
        self.storage.segments.mkdir(parents=True)
        expected = self.storage.segments / "segment-00000001.jsonl"
        expected.touch()
        (self.storage.segments / "README.txt").touch()
        self.assertEqual([(1, expected)], self.storage._segment_paths())

    def test_compare_and_append_checks_decoder_size_and_rotation(self) -> None:
        active = descriptor(self.root / "active.jsonl")

        def common_context() -> ExitStack:
            stack = ExitStack()
            stack.enter_context(
                mock.patch.object(
                    self.storage,
                    "_append_lock",
                    return_value=nullcontext(),
                )
            )
            stack.enter_context(mock.patch.object(self.storage, "_guard_control_root"))
            stack.enter_context(
                mock.patch.object(
                    self.storage,
                    "_load_active_segment",
                    return_value=active,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    self.storage,
                    "_read_authority_time",
                    return_value=datetime(2026, 8, 20, tzinfo=timezone.utc),
                )
            )
            return stack

        with (
            common_context(),
            mock.patch.object(
                filesystem,
                "decode_journal_event_bytes",
                return_value=SimpleNamespace(ok=False, value=None),
            ),
            self.assertRaisesRegex(ValueError, "strict JournalEvent decoder"),
        ):
            self.storage.compare_and_append_draft(
                draft=run_draft(),
                expected_head=GENESIS_HEAD,
                policy=SegmentPolicy(),
            )

        with (
            common_context(),
            self.assertRaises(PersistenceFault) as too_large,
        ):
            self.storage.compare_and_append_draft(
                draft=run_draft(),
                expected_head=GENESIS_HEAD,
                policy=SegmentPolicy(max_bytes=1),
            )
        self.assertEqual(JournalFaultCode.EVENT_TOO_LARGE, too_large.exception.code)

        rotated = descriptor(self.root / "rotated.jsonl", number=2)
        with (
            common_context(),
            mock.patch.object(self.storage, "_rotation_required", return_value=True),
            mock.patch.object(self.storage, "_rotate", return_value=rotated) as rotate,
            mock.patch.object(self.storage, "_append_frame") as append,
        ):
            result = self.storage.compare_and_append_draft(
                draft=run_draft(),
                expected_head=GENESIS_HEAD,
                policy=SegmentPolicy(),
            )
        self.assertEqual("committed", result.status.value)
        rotate.assert_called_once_with(active, SegmentPolicy())
        self.assertEqual(rotated.path, append.call_args.args[0])

    def test_lock_helpers_cover_both_platforms_and_retry_outcomes(self) -> None:
        handle = mock.MagicMock()
        handle.fileno.return_value = 17

        windows = SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=mock.Mock(side_effect=[OSError("busy"), None]),
        )
        with (
            mock.patch.dict(sys.modules, {"msvcrt": windows}),
            mock.patch.object(filesystem.os, "name", "nt"),
            mock.patch.object(filesystem.time, "monotonic", side_effect=[0.0, 0.0]),
            mock.patch.object(filesystem.time, "sleep") as sleep,
        ):
            self.storage._lock_handle(handle)
        sleep.assert_called_once_with(0.01)

        windows.locking = mock.Mock(side_effect=OSError("busy"))
        with (
            mock.patch.dict(sys.modules, {"msvcrt": windows}),
            mock.patch.object(filesystem.os, "name", "nt"),
            mock.patch.object(filesystem.time, "monotonic", side_effect=[0.0, 6.0]),
            self.assertRaises(OSError),
        ):
            self.storage._lock_handle(handle)

        posix = SimpleNamespace(
            LOCK_EX=1,
            LOCK_NB=2,
            LOCK_UN=4,
            flock=mock.Mock(side_effect=[BlockingIOError(), None]),
        )
        with (
            mock.patch.dict(sys.modules, {"fcntl": posix}),
            mock.patch.object(filesystem.os, "name", "posix"),
            mock.patch.object(filesystem.time, "monotonic", side_effect=[0.0, 0.0]),
            mock.patch.object(filesystem.time, "sleep") as sleep,
        ):
            self.storage._lock_handle(handle)
        sleep.assert_called_once_with(0.01)

        posix.flock = mock.Mock(side_effect=BlockingIOError())
        with (
            mock.patch.dict(sys.modules, {"fcntl": posix}),
            mock.patch.object(filesystem.os, "name", "posix"),
            mock.patch.object(filesystem.time, "monotonic", side_effect=[0.0, 6.0]),
            self.assertRaises(OSError),
        ):
            self.storage._lock_handle(handle)

        windows.locking = mock.Mock()
        with (
            mock.patch.dict(sys.modules, {"msvcrt": windows}),
            mock.patch.object(filesystem.os, "name", "nt"),
        ):
            self.storage._unlock_handle(handle)
        windows.locking.assert_called_once_with(17, windows.LK_UNLCK, 1)

        posix.flock = mock.Mock()
        with (
            mock.patch.dict(sys.modules, {"fcntl": posix}),
            mock.patch.object(filesystem.os, "name", "posix"),
        ):
            self.storage._unlock_handle(handle)
        posix.flock.assert_called_once_with(17, posix.LOCK_UN)

    def test_directory_sync_covers_posix_and_windows_failures(self) -> None:
        with (
            mock.patch.object(filesystem.os, "name", "posix"),
            mock.patch.object(filesystem.os, "open", return_value=21) as opened,
            mock.patch.object(filesystem.os, "fsync") as fsync,
            mock.patch.object(filesystem.os, "close") as close,
        ):
            self.storage._sync_directory(self.root, "sync", None)
        opened.assert_called_once()
        fsync.assert_called_once_with(21)
        close.assert_called_once_with(21)

        invalid_handle = filesystem.ctypes.c_void_p(-1).value
        for create_result, flush_result in (
            (invalid_handle, True),
            (22, False),
            (22, True),
        ):
            create_file = mock.Mock(return_value=create_result)
            flush = mock.Mock(return_value=flush_result)
            close_handle = mock.Mock(return_value=True)
            kernel32 = SimpleNamespace(
                CreateFileW=create_file,
                FlushFileBuffers=flush,
                CloseHandle=close_handle,
            )
            with (
                self.subTest(create_result=create_result, flush_result=flush_result),
                mock.patch.object(filesystem.ctypes, "WinDLL", return_value=kernel32, create=True),
                mock.patch.object(
                    filesystem.ctypes,
                    "get_last_error",
                    return_value=5,
                    create=True,
                ),
                mock.patch.object(
                    filesystem.ctypes,
                    "WinError",
                    side_effect=OSError(errno.EACCES, "denied"),
                    create=True,
                ),
            ):
                if create_result == invalid_handle or not flush_result:
                    with self.assertRaises(OSError):
                        self.storage._sync_windows_directory(self.root)
                else:
                    self.storage._sync_windows_directory(self.root)
            if create_result != invalid_handle:
                close_handle.assert_called_once_with(22)

    def test_active_segment_index_rejects_all_ambiguous_layouts(self) -> None:
        one = self.root / "segment-1"
        two = self.root / "segment-2"
        three = self.root / "segment-3"
        cases = (
            ([(1, one), (2, two), (3, three)], index(sealed=1, active=3)),
            ([(1, one), (3, three)], index(sealed=2, active=3)),
            ([(1, one), (2, two), (3, three)], index(sealed=1, active=2)),
        )
        for paths, record in cases:
            with (
                self.subTest(paths=paths, record=record),
                mock.patch.object(self.storage, "_segment_paths", return_value=paths),
                mock.patch.object(self.storage, "_read_index", return_value=record),
                self.assertRaises(PersistenceFault) as raised,
            ):
                self.storage._load_active_segment(SegmentPolicy())
            self.assertEqual(JournalFaultCode.INVALID_LAYOUT, raised.exception.code)

    def test_empty_sealed_segments_cannot_be_verified_or_rotated(self) -> None:
        empty = descriptor(self.root / "empty.jsonl")
        with self.assertRaises(PersistenceFault) as verified:
            self.storage._verify_index(index(sealed=1, active=2), empty)
        self.assertEqual(JournalFaultCode.JOURNAL_CORRUPT, verified.exception.code)

        with self.assertRaises(PersistenceFault) as rotated:
            self.storage._rotate(empty, SegmentPolicy())
        self.assertEqual(JournalFaultCode.ROTATION_FAILED, rotated.exception.code)

    def test_index_read_and_segment_limits_preserve_named_faults(self) -> None:
        self.root.mkdir(parents=True)
        self.storage.index_path.touch()
        with (
            mock.patch.object(
                Path,
                "read_bytes",
                side_effect=OSError(errno.EACCES, "denied"),
            ),
            self.assertRaises(PersistenceFault) as index_fault,
        ):
            self.storage._read_index()
        self.assertEqual(JournalFaultCode.PERMISSION_DENIED, index_fault.exception.code)

        first = event(1, GENESIS_HEAD.event_hash)
        second = event(2, first.event_hash)
        segment = self.root / "two-events.jsonl"
        segment.write_bytes(first.canonical_json_bytes() + second.canonical_json_bytes())
        with self.assertRaises(PersistenceFault) as segment_fault:
            self.storage._scan_segment(
                1,
                segment,
                GENESIS_HEAD,
                SegmentPolicy(max_events=1),
            )
        self.assertEqual(JournalFaultCode.JOURNAL_CORRUPT, segment_fault.exception.code)
        self.assertIn("segment_event_limit", str(segment_fault.exception))


if __name__ == "__main__":
    unittest.main()
