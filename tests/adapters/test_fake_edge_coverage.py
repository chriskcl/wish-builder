from __future__ import annotations

from wish_builder.adapters.fakes import FakeBackendChannelPort

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.adapters.test_trellis_fakes import (
    capabilities,
    prepare_command,
    reserve_command,
    send_command,
)
from tests.adapters.test_trellis_graph_import import snapshot
from tests.ports.test_conformance import FIXED_TIME, persisted_request, receipt_from
from tests.ports.trellis_helpers import HASH_A, HEAD_COMMIT, prepared
from wish_builder.adapters.fake import FakeTaskPort
from wish_builder.adapters.fake import effects as fake_effects
from wish_builder.adapters.trellis import (
    FakeTrellisGraphPort,
    FakeTrellisLifecyclePort,
    FakeExternalState,
)
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectStatus,
    ExecutionIdentity,
    JournalEventType,
)
from wish_builder.services.ports import (
    CancelTurn,
    CheckAttempt,
    FinishAttempt,
    TurnState,
)


def check_command(operation_id: str, *, attempt_id: str = "attempt-missing") -> CheckAttempt:
    return CheckAttempt(
        operation_id=operation_id,
        attempt_id=attempt_id,
        trellis_task_id="child-alpha",
        task_id="TASK-001",
        task_packet_digest=HASH_A,
        expected_head_commit=HEAD_COMMIT,
    )


def finish_command(operation_id: str, *, attempt_id: str = "attempt-missing") -> FinishAttempt:
    return FinishAttempt(
        operation_id=operation_id,
        attempt_id=attempt_id,
        trellis_task_id="child-alpha",
        task_id="TASK-001",
        delivered_commit=HEAD_COMMIT,
        delivery_evidence_digest=HASH_A,
    )


def cancel_command(operation_id: str, *, turn_id: str = "turn-missing") -> CancelTurn:
    return CancelTurn(
        operation_id=operation_id,
        attempt_id="attempt-001",
        channel_id="channel-001",
        turn_id=turn_id,
        reason_code="operator_requested",
    )


class FakeTrellisBoundaryTests(unittest.TestCase):
    def test_graph_port_validates_mapping_and_lookup_identity(self) -> None:
        candidate = snapshot()
        for value, error in (
            ({}, ValueError),
            ({1: candidate}, TypeError),
            ({candidate.parent_task_id: object()}, TypeError),
            ({"different-parent": candidate}, ValueError),
        ):
            with self.subTest(value=value), self.assertRaises(error):
                FakeTrellisGraphPort(value)

        port = FakeTrellisGraphPort(candidate)
        with self.assertRaises(TypeError):
            port.export_snapshot(1)
        self.assertEqual(candidate, port.export_snapshot(candidate.parent_task_id))
        self.assertEqual((candidate.parent_task_id,), port.calls)

    def test_lifecycle_constructor_and_effect_type_guards(self) -> None:
        for kwargs in ({"state": object()}, {"clock": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TypeError):
                FakeTrellisLifecyclePort(**kwargs)

        port = FakeTrellisLifecyclePort()
        with self.assertRaises(TypeError):
            port.prepare_attempt(object())
        with self.assertRaises(TypeError):
            port.prepare_attempt(prepared(check_command("OP-WRONG-TYPE")))
        with self.assertRaises(TypeError):
            port.inspect_attempt(1)

    def test_lifecycle_conflicts_preserve_response_shape(self) -> None:
        state = FakeExternalState()
        lifecycle = FakeTrellisLifecyclePort(state=state)
        state.lifecycle_conflicts.update({"OP-CHECK-CONFLICT", "OP-FINISH-CONFLICT"})

        checked = lifecycle.check_attempt(prepared(check_command("OP-CHECK-CONFLICT")))
        finished = lifecycle.finish_attempt(
            prepared(finish_command("OP-FINISH-CONFLICT"), event_number=2)
        )
        self.assertEqual(EffectStatus.UNKNOWN, checked.status)
        self.assertEqual(EffectStatus.UNKNOWN, finished.status)

        channel = FakeBackendChannelPort(capabilities(), state=state)
        channel.reserve(
            prepared(reserve_command("OP-CROSS-CHECK"), event_number=3)
        )
        channel.reserve(
            prepared(
                reserve_command(
                    "OP-CROSS-FINISH",
                    attempt_id="attempt-002",
                    channel_id="channel-002",
                ),
                event_number=4,
            )
        )
        self.assertEqual(
            EffectStatus.UNKNOWN,
            lifecycle.check_attempt(
                prepared(check_command("OP-CROSS-CHECK"), event_number=5)
            ).status,
        )
        self.assertEqual(
            EffectStatus.UNKNOWN,
            lifecycle.finish_attempt(
                prepared(finish_command("OP-CROSS-FINISH"), event_number=6)
            ).status,
        )

    def test_lifecycle_missing_and_changed_check_or_finish_are_unknown(self) -> None:
        port = FakeTrellisLifecyclePort()
        original_check = check_command("OP-CHECK-MISSING")
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.check_attempt(prepared(original_check, event_number=1)).status,
        )
        changed_check = dataclasses.replace(original_check, expected_head_commit="b" * 40)
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.check_attempt(prepared(changed_check, event_number=2)).status,
        )

        original_finish = finish_command("OP-FINISH-MISSING")
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.finish_attempt(prepared(original_finish, event_number=3)).status,
        )
        changed_finish = dataclasses.replace(original_finish, delivered_commit="c" * 40)
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.finish_attempt(prepared(changed_finish, event_number=4)).status,
        )

    def test_channel_constructor_and_effect_type_guards(self) -> None:
        for args, kwargs, error in (
            ((object(),), {}, TypeError),
            ((capabilities(),), {"state": object()}, TypeError),
            ((capabilities(),), {"clock": None}, TypeError),
            ((capabilities(),), {"send_state": object()}, ValueError),
            ((capabilities(),), {"send_state": TurnState.ABSENT}, ValueError),
        ):
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(error):
                FakeBackendChannelPort(*args, **kwargs)

        port = FakeBackendChannelPort(capabilities())
        with self.assertRaises(TypeError):
            port.reserve(object())
        with self.assertRaises(TypeError):
            port.reserve(prepared(send_command("OP-WRONG-RESERVE")))
        with self.assertRaises(TypeError):
            port.inspect_turn(1)

    def test_channel_collision_reasons_and_inspection_are_explicit(self) -> None:
        state = FakeExternalState()
        port = FakeBackendChannelPort(capabilities(), state=state)
        state.channel_conflicts.update({"OP-RESERVE-CONFLICT", "OP-SEND-CONFLICT"})
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.reserve(
                prepared(reserve_command("OP-RESERVE-CONFLICT"), event_number=1)
            ).status,
        )
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.send(prepared(send_command("OP-SEND-CONFLICT"), event_number=2)).status,
        )

        lifecycle = FakeTrellisLifecyclePort(state=state)
        lifecycle.prepare_attempt(
            prepared(prepare_command("OP-LIFECYCLE-SEND"), event_number=3)
        )
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.send(
                prepared(send_command("OP-LIFECYCLE-SEND"), event_number=4)
            ).status,
        )

        original = reserve_command("OP-RESERVE-CHANGED")
        port.reserve(prepared(original, event_number=5))
        changed = dataclasses.replace(original, attempt_id="attempt-changed")
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.reserve(prepared(changed, event_number=6)).status,
        )
        self.assertEqual(
            EffectStatus.UNKNOWN,
            port.inspect_reservation(original.operation_id).status,
        )

    def test_channel_reports_reservation_packet_and_identifier_failures(self) -> None:
        port = FakeBackendChannelPort(capabilities())
        port.reserve(prepared(reserve_command(), event_number=1))
        collision = port.reserve(
            prepared(
                reserve_command(
                    "OP-RESERVE-COLLISION",
                    attempt_id="attempt-002",
                    channel_id="channel-001",
                ),
                event_number=2,
            )
        )
        self.assertIn("channel_id_collision", collision.evidence[0])
        self.assertEqual(
            EffectStatus.APPLIED,
            port.inspect_reservation("OP-RESERVE-001").status,
        )

        first = port.send(prepared(send_command(), event_number=3))
        self.assertEqual(EffectStatus.APPLIED, first.status)
        message_collision = port.send(
            prepared(
                send_command("OP-SEND-MESSAGE", turn_id="turn-002"),
                event_number=4,
            )
        )
        self.assertIn("message_id_collision", message_collision.evidence[0])

        small = FakeBackendChannelPort(capabilities(max_packet_bytes=16))
        small.reserve(prepared(reserve_command(), event_number=5))
        oversized = small.send(prepared(send_command(), event_number=6))
        self.assertIn("task_packet_exceeds_capability", oversized.evidence[0])

        scripted = FakeBackendChannelPort(
            capabilities(), unknown_operation_ids={"OP-SEND-SCRIPTED"}
        )
        scripted.reserve(prepared(reserve_command(), event_number=7))
        unknown = scripted.send(
            prepared(send_command("OP-SEND-SCRIPTED"), event_number=8)
        )
        self.assertIn("scripted_unknown", unknown.evidence[0])

    def test_cancel_unknown_missing_and_terminal_turns_are_stable(self) -> None:
        scripted = FakeBackendChannelPort(
            capabilities(), unknown_operation_ids={"OP-CANCEL-SCRIPTED"}
        )
        unknown = scripted.cancel(
            prepared(cancel_command("OP-CANCEL-SCRIPTED"), event_number=1)
        )
        self.assertIn("scripted_unknown", unknown.evidence[0])

        port = FakeBackendChannelPort(capabilities())
        missing = port.cancel(
            prepared(cancel_command("OP-CANCEL-MISSING"), event_number=2)
        )
        self.assertIn("turn_not_found", missing.evidence[0])

        port.reserve(prepared(reserve_command(), event_number=3))
        done = port.send(prepared(send_command(), event_number=4))
        cancelled = port.cancel(
            prepared(cancel_command("OP-CANCEL-DONE", turn_id="turn-001"), event_number=5)
        )
        self.assertEqual(TurnState.DONE, done.state)
        self.assertEqual(TurnState.DONE, cancelled.state)


class FilesystemFakeBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.request = persisted_request(
            AdapterKind.TASK,
            EffectOperation.TASK_EXECUTION,
            EffectObjectType.WORKER,
            JournalEventType.EFFECT_REQUESTED,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_constructor_identity_and_operation_guards(self) -> None:
        with self.assertRaises(TypeError):
            fake_effects.FilesystemFakeEffectPort(self.root, object())
        with self.assertRaises(TypeError):
            fake_effects.FilesystemFakeEffectPort(
                self.root,
                fake_effects._TASK_CONTRACT,
                clock=None,
            )

        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        run_id = self.request.identity.run_id
        for identity in (
            object(),
            ExecutionIdentity(run_id, 1),
            ExecutionIdentity(run_id, 1, "TASK-001", 1),
        ):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                port.lookup(identity, EffectOperation.TASK_EXECUTION)
        with self.assertRaises(TypeError):
            port.lookup(self.request.identity, "task_execution")
        with self.assertRaises(ValueError):
            port.lookup(self.request.identity, EffectOperation.MODEL_INFERENCE)

    def test_read_and_atomic_write_storage_faults_are_bounded(self) -> None:
        with mock.patch.object(Path, "open", side_effect=PermissionError("denied")):
            read = fake_effects.FilesystemFakeEffectPort._read(self.root / "denied.json")
        self.assertEqual(fake_effects._ReadState.INVALID, read.state)
        self.assertIn(b"PermissionError", read.raw)

        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        port._ensure_layout()
        target = port.effects / "existing.json"
        target.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            port._write_atomic(target, b"new")

        short_target = port.effects / "short.json"
        with mock.patch.object(
            fake_effects.os,
            "write",
            return_value=0,
        ), self.assertRaisesRegex(OSError, "short write"):
            port._write_atomic(short_target, b"payload")

    def test_posix_directory_sync_closes_the_descriptor(self) -> None:
        with (
            mock.patch.object(fake_effects.os, "name", "posix"),
            mock.patch.object(fake_effects.os, "open", return_value=7) as opened,
            mock.patch.object(fake_effects.os, "fsync") as synced,
            mock.patch.object(fake_effects.os, "close") as closed,
        ):
            fake_effects._sync_directory(self.root)
        opened.assert_called_once()
        synced.assert_called_once_with(7)
        closed.assert_called_once_with(7)

    def test_lookup_turns_lock_failure_into_unknown_receipt(self) -> None:
        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        with mock.patch.object(port, "_lock", side_effect=OSError("lock failed")):
            outcome = receipt_from(
                port.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
        )
        self.assertEqual(EffectStatus.UNKNOWN, outcome.status)
        self.assertTrue(outcome.evidence)
        self.assertGreater(outcome.evidence[0].byte_length, 0)

    def test_mismatched_receipt_and_failed_receipt_repair_are_safe(self) -> None:
        port = FakeTaskPort(self.root, clock=lambda: FIXED_TIME)
        applied = receipt_from(port.apply(self.request))
        receipt_path = next(port.receipts.glob("*.json"))
        different = dataclasses.replace(applied, observed_at="2026-08-19T00:00:01Z")
        receipt_path.write_bytes(different.canonical_json_bytes())
        mismatch = receipt_from(
            port.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
        )
        self.assertEqual(EffectStatus.UNKNOWN, mismatch.status)

        receipt_path.unlink()
        with mock.patch.object(port, "_write_atomic", side_effect=OSError("read-only")):
            repaired = receipt_from(
                port.lookup(self.request.identity, EffectOperation.TASK_EXECUTION)
            )
        self.assertEqual(EffectStatus.APPLIED, repaired.status)
        self.assertFalse(receipt_path.exists())

    def test_orphan_receipt_and_after_receipt_fault_do_not_duplicate_effect(self) -> None:
        port = FakeTaskPort(self.root / "orphan", clock=lambda: FIXED_TIME)
        receipt_from(port.apply(self.request))
        next(port.effects.glob("*.json")).unlink()
        orphan = receipt_from(port.apply(self.request))
        self.assertEqual(EffectStatus.UNKNOWN, orphan.status)

        def failpoint(point: str, path: Path) -> None:
            del path
            if point == "after_receipt":
                raise OSError("ack failed")

        recovered_port = FakeTaskPort(
            self.root / "recover",
            clock=lambda: FIXED_TIME,
            failpoint=failpoint,
        )
        recovered = receipt_from(recovered_port.apply(self.request))
        self.assertEqual(EffectStatus.APPLIED, recovered.status)
        self.assertEqual(1, len(tuple(recovered_port.effects.glob("*.json"))))
        self.assertEqual(1, len(tuple(recovered_port.receipts.glob("*.json"))))

    def test_default_clock_is_utc_text(self) -> None:
        self.assertTrue(fake_effects._utc_now().endswith("Z"))


if __name__ == "__main__":
    unittest.main()
