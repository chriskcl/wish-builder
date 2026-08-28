from __future__ import annotations

import dataclasses
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.services.test_replay import (
    EPOCH,
    RUN_ID,
    admitted_manifest,
    folded_snapshot,
    graph_freeze_events,
)
from wish_builder.adapters.git_identity import FilesystemIdentity
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.services import replay as replay_module
from wish_builder.services.checkpoints import JournalPosition, VerifiedCheckpoint
from wish_builder.services.journal import GENESIS_HEAD, JournalHead
from wish_builder.services.replay import ReplayFaultCode, ReplayStatus


HASH = "sha256:" + "a" * 64


def _changed_stat(
    value: os.stat_result,
    *,
    inode_delta: int = 0,
    size_delta: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=value.st_dev,
        st_ino=value.st_ino + inode_delta,
        st_size=value.st_size + size_delta,
    )


def _verified_checkpoint(
    snapshot,
    graph: GraphIndex,
    position: JournalPosition,
) -> VerifiedCheckpoint:
    return VerifiedCheckpoint(
        "CHECKPOINT-001",
        HASH,
        snapshot,
        graph,
        position,
        snapshot.last_sequence,
        snapshot.last_event_hash,
    )


class ReplayDecoderBranchClosureTests(unittest.TestCase):
    def test_strict_shape_and_integer_secondary_guards(self) -> None:
        with mock.patch.object(replay_module, "_MIN_SIGNED_64", 0):
            with self.assertRaisesRegex(ValueError, "signed 64-bit"):
                replay_module._signed_64_json_integer("-1")

        with self.assertRaisesRegex(ValueError, "unsupported JSON shape"):
            replay_module._validate_replay_json_shape(1.5)

        with self.assertRaisesRegex(ValueError, "item limit"):
            replay_module._validate_replay_json_shape(
                [None] * (replay_module.DEFAULT_DECODE_LIMITS.max_items + 1)
            )

    def test_canonical_object_rejects_duplicate_keys_and_non_objects(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate_key"):
            replay_module._canonical_object(b'{"key":1,"key":2}')
        with self.assertRaisesRegex(ValueError, "noncanonical"):
            replay_module._canonical_object(b"[]")


class ReplayAttemptBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.journal = self.root / "journal"
        self.segments = self.journal / "segments"
        self.segments.mkdir(parents=True)
        self.manifest = admitted_manifest()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _segment(self, raw: bytes) -> Path:
        path = self.segments / "segment-00000001.jsonl"
        path.write_bytes(raw)
        return path

    def test_empty_invalid_index_without_index_file_needs_no_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = Path(temporary) / "journal"
            with mock.patch.object(
                replay_module,
                "_journal_index_status",
                return_value=(False, "forced_invalid"),
            ):
                result = replay_module.replay_journal(
                    journal,
                    self.manifest,
                    coordinator_epoch=EPOCH,
                )

        self.assertEqual(ReplayStatus.RECOVERED, result.status)

    def test_segment_identity_change_before_open_is_blocked(self) -> None:
        path = self._segment(graph_freeze_events()[0].canonical_json_bytes())
        linked = os.lstat(path)
        with mock.patch.object(
            replay_module.os,
            "fstat",
            return_value=_changed_stat(linked, inode_delta=1),
        ):
            attempt = replay_module._replay_attempt(
                self.journal,
                ((1, path),),
                self.manifest,
                EPOCH,
                None,
                (),
            )

        self.assertEqual(ReplayFaultCode.SEGMENT_REPLACED, attempt.result.fault.code)

    def test_segment_identity_change_after_read_is_blocked(self) -> None:
        path = self._segment(graph_freeze_events()[0].canonical_json_bytes())
        real_fstat = os.fstat
        calls = 0

        def changing_fstat(descriptor: int):
            nonlocal calls
            calls += 1
            observed = real_fstat(descriptor)
            return (
                observed
                if calls == 1
                else _changed_stat(observed, size_delta=1)
            )

        with mock.patch.object(replay_module.os, "fstat", side_effect=changing_fstat):
            attempt = replay_module._replay_attempt(
                self.journal,
                ((1, path),),
                self.manifest,
                EPOCH,
                None,
                (),
            )

        self.assertEqual(ReplayFaultCode.SEGMENT_REPLACED, attempt.result.fault.code)

    def test_quarantined_tail_rejects_graph_mismatch(self) -> None:
        path = self._segment(b'{"partial":true}')
        with mock.patch.object(GraphIndex, "verify", return_value=False):
            attempt = replay_module._replay_attempt(
                self.journal,
                ((1, path),),
                self.manifest,
                EPOCH,
                None,
                (),
            )

        self.assertEqual(
            ReplayFaultCode.GRAPH_INDEX_MISMATCH,
            attempt.result.fault.code,
        )

    def test_checkpoint_tail_quarantine_does_not_build_descriptors(self) -> None:
        event = graph_freeze_events()[0]
        committed = event.canonical_json_bytes()
        path = self._segment(committed + b'{"partial":true}')
        snapshot = folded_snapshot([event])
        graph = GraphIndex.rebuild(self.manifest, snapshot)
        checkpoint = _verified_checkpoint(
            snapshot,
            graph,
            JournalPosition(1, len(committed)),
        )

        attempt = replay_module._replay_attempt(
            self.journal,
            ((1, path),),
            self.manifest,
            EPOCH,
            checkpoint,
            (),
        )

        self.assertEqual(ReplayStatus.RECOVERED, attempt.result.status)
        self.assertTrue(attempt.result.checkpoint_used)
        self.assertEqual((), attempt.descriptors)


class CheckpointPositionBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.mkdir(exist_ok=True)
        self.manifest = admitted_manifest()
        self.event = graph_freeze_events()[0]
        self.raw = self.event.canonical_json_bytes()
        self.snapshot = folded_snapshot([self.event])
        self.graph = GraphIndex.rebuild(self.manifest, self.snapshot)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def checkpoint(self, segment: int, offset: int) -> VerifiedCheckpoint:
        return _verified_checkpoint(
            self.snapshot,
            self.graph,
            JournalPosition(segment, offset),
        )

    def test_rejects_invalid_current_and_previous_segment_identities(self) -> None:
        first = self.root / "segment-00000001.jsonl"
        second = self.root / "segment-00000002.jsonl"
        first.write_bytes(self.raw)
        second.write_bytes(b"")

        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            return_value=True,
        ):
            self.assertFalse(
                replay_module._checkpoint_position_valid(
                    ((1, first),), self.checkpoint(1, len(self.raw))
                )
            )

        def previous_is_link(path: Path) -> bool:
            return path == first

        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            side_effect=previous_is_link,
        ):
            self.assertFalse(
                replay_module._checkpoint_position_valid(
                    ((1, first), (2, second)), self.checkpoint(2, 0)
                )
            )

    def test_rejects_missing_or_empty_previous_segment(self) -> None:
        first = self.root / "segment-00000001.jsonl"
        second = self.root / "segment-00000002.jsonl"
        first.write_bytes(b"")
        second.write_bytes(b"")

        self.assertFalse(
            replay_module._checkpoint_position_valid(
                ((1, first),), self.checkpoint(1, 0)
            )
        )
        self.assertFalse(
            replay_module._checkpoint_position_valid(
                ((1, first), (2, second)), self.checkpoint(2, 0)
            )
        )

    def test_rejects_identity_change_before_and_after_read(self) -> None:
        path = self.root / "segment-00000001.jsonl"
        path.write_bytes(self.raw)
        linked = os.lstat(path)

        with mock.patch.object(
            replay_module.os,
            "fstat",
            return_value=_changed_stat(linked, inode_delta=1),
        ):
            self.assertFalse(
                replay_module._checkpoint_position_valid(
                    ((1, path),), self.checkpoint(1, len(self.raw))
                )
            )

        real_fstat = os.fstat
        calls = 0

        def changing_fstat(descriptor: int):
            nonlocal calls
            calls += 1
            observed = real_fstat(descriptor)
            return (
                observed
                if calls == 1
                else _changed_stat(observed, inode_delta=1)
            )

        with mock.patch.object(replay_module.os, "fstat", side_effect=changing_fstat):
            self.assertFalse(
                replay_module._checkpoint_position_valid(
                    ((1, path),), self.checkpoint(1, len(self.raw))
                )
            )

    def test_rejects_incomplete_large_invalid_and_noncanonical_frames(self) -> None:
        path = self.root / "segment-00000001.jsonl"

        path.write_bytes(b"incomplete")
        self.assertFalse(
            replay_module._checkpoint_position_valid(
                ((1, path),), self.checkpoint(1, len(b"incomplete"))
            )
        )

        path.write_bytes(b"xxxxxx\n")
        limits = SimpleNamespace(max_bytes=4)
        with mock.patch.object(replay_module, "DEFAULT_DECODE_LIMITS", limits):
            self.assertFalse(
                replay_module._checkpoint_position_valid(
                    ((1, path),), self.checkpoint(1, 7)
                )
            )

        path.write_bytes(b"{}\n")
        self.assertFalse(
            replay_module._checkpoint_position_valid(
                ((1, path),), self.checkpoint(1, 3)
            )
        )

        noncanonical = json.dumps(
            json.loads(self.raw),
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        path.write_bytes(noncanonical)
        self.assertFalse(
            replay_module._checkpoint_position_valid(
                ((1, path),), self.checkpoint(1, len(noncanonical))
            )
        )

    def test_rejects_event_that_does_not_match_checkpoint_head(self) -> None:
        path = self.root / "segment-00000001.jsonl"
        path.write_bytes(self.raw)
        mismatched = dataclasses.replace(
            self.snapshot,
            last_sequence=2,
            last_event_id="EVENT-000002",
            last_event_hash=HASH,
        )
        checkpoint = _verified_checkpoint(
            mismatched,
            self.graph,
            JournalPosition(1, len(self.raw)),
        )

        self.assertFalse(
            replay_module._checkpoint_position_valid(((1, path),), checkpoint)
        )


class ReplayIndexBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.mkdir(exist_ok=True)
        self.event = graph_freeze_events()[0]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_index_link_and_missing_sealed_segment_are_invalid(self) -> None:
        index = self.root / "index.json"
        index.write_bytes(b"{}")
        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            return_value=True,
        ):
            valid, detail = replay_module._journal_index_status(
                self.root,
                (),
                RUN_ID,
            )
        self.assertFalse(valid)
        self.assertEqual("index_link_or_reparse_point", detail)

        value = {
            "active_segment": 2,
            "byte_count": 1,
            "event_count": 1,
            "index_version": replay_module._INDEX_VERSION,
            "last_event_hash": HASH,
            "last_sequence": 1,
            "previous_event_hash": GENESIS_HEAD.event_hash,
            "sealed_segment": 1,
            "segment_hash": HASH,
            "start_sequence": 1,
        }
        index.write_bytes(canonical_json_bytes(value))
        active = self.root / "segment-00000002.jsonl"
        valid, detail = replay_module._journal_index_status(
            self.root,
            ((2, active),),
            RUN_ID,
        )
        self.assertFalse(valid)
        self.assertEqual("sealed_segment_missing", detail)

    def test_indexed_segment_rejects_link_and_identity_changes(self) -> None:
        path = self.root / "segment-00000001.jsonl"
        path.write_bytes(self.event.canonical_json_bytes())

        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            return_value=True,
        ):
            with self.assertRaises(replay_module._SegmentReplaced):
                replay_module._scan_indexed_segment(
                    path,
                    1,
                    GENESIS_HEAD,
                    RUN_ID,
                )

        linked = os.lstat(path)
        with mock.patch.object(
            replay_module.os,
            "fstat",
            return_value=_changed_stat(linked, inode_delta=1),
        ):
            with self.assertRaises(replay_module._SegmentReplaced):
                replay_module._scan_indexed_segment(
                    path,
                    1,
                    GENESIS_HEAD,
                    RUN_ID,
                )

        real_fstat = os.fstat
        calls = 0

        def changing_fstat(descriptor: int):
            nonlocal calls
            calls += 1
            observed = real_fstat(descriptor)
            return (
                observed
                if calls == 1
                else _changed_stat(observed, size_delta=1)
            )

        with mock.patch.object(replay_module.os, "fstat", side_effect=changing_fstat):
            with self.assertRaises(replay_module._SegmentReplaced):
                replay_module._scan_indexed_segment(
                    path,
                    1,
                    GENESIS_HEAD,
                    RUN_ID,
                )

    def test_rebuild_handles_absent_present_and_invalid_boundaries(self) -> None:
        replay_module._rebuild_journal_index(self.root, ())

        index = self.root / "index.json"
        index.write_bytes(b"stale")
        with mock.patch.object(replay_module, "_quarantine_derived_index") as quarantine:
            replay_module._rebuild_journal_index(self.root, ())
        quarantine.assert_called_once_with(self.root)

        start = JournalHead(0, GENESIS_HEAD.event_hash)
        end = JournalHead(1, self.event.event_hash)
        sealed = replay_module._SegmentDescriptor(1, start, end, 1, 1, HASH)
        active = replay_module._SegmentDescriptor(3, end, end, 0, 0, HASH)
        with self.assertRaisesRegex(ValueError, "invalid segment boundary"):
            replay_module._rebuild_journal_index(self.root, (sealed, active))


class ReplayFilesystemBranchClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_quarantine_tail_guards_each_identity_boundary(self) -> None:
        journal = self.root / "journal"
        journal.mkdir()
        segment = journal / "segment-00000001.jsonl"
        raw = b"partial"
        segment.write_bytes(raw)

        with self.assertRaisesRegex(ValueError, "control_root_drift"):
            replay_module._quarantine_tail(
                journal,
                segment,
                1,
                0,
                raw,
                lambda: False,
            )

        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            return_value=True,
        ):
            with self.assertRaisesRegex(ValueError, "segment_replaced"):
                replay_module._quarantine_tail(journal, segment, 1, 0, raw)

        linked = os.lstat(segment)
        with (
            mock.patch.object(replay_module, "_write_quarantine_immutable"),
            mock.patch.object(replay_module, "_sync_file_parent"),
            mock.patch.object(
                replay_module.os,
                "fstat",
                return_value=_changed_stat(linked, inode_delta=1),
            ),
            self.assertRaisesRegex(ValueError, "segment_replaced"),
        ):
            replay_module._quarantine_tail(journal, segment, 1, 0, raw)

        validations = iter((True, True, False))
        with (
            mock.patch.object(replay_module, "_sync_file_parent"),
            self.assertRaisesRegex(ValueError, "control_root_drift"),
        ):
            replay_module._quarantine_tail(
                journal,
                segment,
                1,
                0,
                raw,
                lambda: next(validations),
            )

    def test_derived_index_absence_and_collision(self) -> None:
        journal = self.root / "journal"
        journal.mkdir()
        replay_module._quarantine_derived_index(journal)

        raw = b"stale-index"
        source = journal / "index.json"
        source.write_bytes(raw)
        digest = replay_module._sha256_bytes(raw)
        quarantine = self.root / "quarantine"
        quarantine.mkdir()
        target = quarantine / f"derived-index-{digest[7:31]}.json"
        target.write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "collision"):
            replay_module._quarantine_derived_index(journal)

    def test_atomic_and_quarantine_writes_reject_short_writes(self) -> None:
        for operation in (
            lambda path: replay_module._atomic_publish(path, b"payload"),
            lambda path: replay_module._write_quarantine_immutable(path, b"payload"),
        ):
            with self.subTest(operation=operation):
                context = mock.MagicMock()
                context.__enter__.return_value.write.return_value = 1
                with (
                    mock.patch.object(Path, "open", return_value=context),
                    self.assertRaisesRegex(OSError, "short"),
                ):
                    operation(self.root / "target.bin")

    def test_immutable_quarantine_reuses_equal_bytes_and_rejects_collision(self) -> None:
        path = self.root / "quarantine.bin"
        path.write_bytes(b"same")
        replay_module._write_quarantine_immutable(path, b"same")
        with self.assertRaisesRegex(ValueError, "quarantine collision"):
            replay_module._write_quarantine_immutable(path, b"different")

    def test_protected_read_rejects_link_and_identity_changes(self) -> None:
        path = self.root / "protected.bin"
        path.write_bytes(b"payload")

        with mock.patch.object(
            replay_module,
            "_is_link_or_junction",
            return_value=True,
        ):
            with self.assertRaisesRegex(ValueError, "identity is invalid"):
                replay_module._read_limited(path, 100)

        linked = os.lstat(path)
        with mock.patch.object(
            replay_module.os,
            "fstat",
            return_value=_changed_stat(linked, inode_delta=1),
        ):
            with self.assertRaisesRegex(ValueError, "before read"):
                replay_module._read_limited(path, 100)

        real_fstat = os.fstat
        calls = 0

        def changing_fstat(descriptor: int):
            nonlocal calls
            calls += 1
            observed = real_fstat(descriptor)
            return (
                observed
                if calls == 1
                else _changed_stat(observed, size_delta=1)
            )

        with mock.patch.object(replay_module.os, "fstat", side_effect=changing_fstat):
            with self.assertRaisesRegex(ValueError, "during read"):
                replay_module._read_limited(path, 100)

    def test_posix_parent_sync_closes_descriptor(self) -> None:
        path = self.root / "file.bin"
        with (
            mock.patch.object(replay_module.os, "name", "posix"),
            mock.patch.object(replay_module.os, "open", return_value=17) as opened,
            mock.patch.object(replay_module.os, "fsync") as synced,
            mock.patch.object(replay_module.os, "close") as closed,
        ):
            replay_module._sync_file_parent(path)

        opened.assert_called_once()
        synced.assert_called_once_with(17)
        closed.assert_called_once_with(17)

    def test_valid_filesystem_identity_delegates_revalidation(self) -> None:
        identity = FilesystemIdentity(
            "root",
            "root",
            1,
            2,
            1,
            2,
            False,
            HASH,
        )
        with mock.patch(
            "wish_builder.adapters.git_identity.revalidate_control_root",
            return_value=SimpleNamespace(ok=True),
        ) as revalidate:
            self.assertTrue(replay_module._filesystem_identity_validator(identity)())
        revalidate.assert_called_once_with(identity)


if __name__ == "__main__":
    unittest.main()
