from __future__ import annotations

from wish_builder.adapters.fakes import FakeBackendChannelPort

import dataclasses
import hashlib
import unittest

from tests.ports.trellis_helpers import (
    BASE_COMMIT,
    FIXED_TIME,
    GENESIS_HASH,
    HASH_A,
    HASH_B,
    prepared,
)
from wish_builder.adapters.trellis import (
    FakeTrellisGraphPort,
    FakeTrellisLifecyclePort,
)
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
)
from wish_builder.contracts import WorkerProvider
from wish_builder.services.journal import AppendResult, AppendStatus, JournalHead
from wish_builder.services.ports import (
    BackendCapabilities,
    PrepareAttempt,
    PreparedEffect,
    ReserveChannel,
    SendTaskPacket,
    BackendChannelPort,
    TrellisGraphPort,
    TrellisGraphSnapshot,
    TrellisLifecyclePort,
)


def prepare_command(operation_id: str = "OP-PREPARE-001") -> PrepareAttempt:
    return PrepareAttempt(
        operation_id=operation_id,
        run_id="WISH-001",
        parent_task_id="parent-001",
        trellis_task_id="child-alpha",
        task_id="TASK-001",
        attempt=1,
        dispatch_id="DISPATCH-001",
        manifest_digest=HASH_A,
        trellis_graph_digest=HASH_B,
        expected_base_commit=BASE_COMMIT,
    )


def capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        provider=WorkerProvider.CODEX,
        platform="windows-x86_64",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest="sha256:" + "c" * 64,
        max_task_packet_bytes=4096,
    )


class PreparedEffectTests(unittest.TestCase):
    def test_exact_command_is_bound_to_the_durable_request(self) -> None:
        command = prepare_command()
        effect = prepared(command)

        self.assertEqual(command, effect.command)
        self.assertEqual(command.operation_id, effect.operation_id)
        self.assertEqual(command.canonical_sha256(), effect.command_hash)
        self.assertEqual(
            effect.request.payload.request_payload_hash,
            effect.command_hash,
        )

    def test_command_hash_and_operation_identity_mismatches_fail_closed(self) -> None:
        original = prepare_command()
        valid = prepared(original)
        changed = dataclasses.replace(original, task_id="TASK-002")
        with self.assertRaisesRegex(ValueError, "canonical hash"):
            PreparedEffect(valid.request, changed)

        other_operation = dataclasses.replace(original, operation_id="OP-PREPARE-002")
        with self.assertRaisesRegex(ValueError, "operation_id"):
            PreparedEffect(valid.request, other_operation)

    def test_non_durable_and_untyped_values_are_rejected(self) -> None:
        command = prepare_command()
        effect = prepared(command)
        with self.assertRaisesRegex(TypeError, "PersistedEffectRequest"):
            PreparedEffect(effect.request.event, command)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "canonical typed command"):
            PreparedEffect(  # type: ignore[arg-type]
                effect.request, {"operation_id": command.operation_id}
            )

        conflict = AppendResult(
            AppendStatus.CONFLICT,
            JournalHead(1, "sha256:" + "f" * 64),
        )
        with self.assertRaisesRegex(ValueError, "durable event"):
            PreparedEffect.from_append_result(conflict, command)

    def test_request_hash_cannot_be_forged_for_another_command(self) -> None:
        command = prepare_command()
        identity = ExecutionIdentity(
            "WISH-001", 1, "TASK-001", 1, command.operation_id
        )
        event = JournalEvent.create(
            sequence=1,
            event_id="EVENT-TRELLIS-FORGED",
            event_type=JournalEventType.EFFECT_REQUESTED,
            identity=identity,
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-001",
            recorded_at=FIXED_TIME,
            previous_event_hash=GENESIS_HASH,
            payload=EffectRequestPayload(
                EffectOperation.TASK_EXECUTION,
                AdapterKind.TASK,
                EffectObjectType.WORKER,
                HASH_A,
                HASH_B,
                0,
                1,
            ),
        )
        result = AppendResult(
            AppendStatus.COMMITTED,
            JournalHead(event.sequence, event.event_hash),
            event,
        )
        with self.assertRaisesRegex(ValueError, "request_payload_hash"):
            PreparedEffect.from_append_result(result, command)

    def test_mutable_structural_command_cannot_change_after_preparation(self) -> None:
        class MutableCommand:
            def __init__(self) -> None:
                self.operation_id = "OP-MUTABLE-001"
                self.value = "first"

            def to_primitive(self) -> dict[str, object]:
                return {
                    "operation_id": self.operation_id,
                    "value": self.value,
                }

            def canonical_sha256(self) -> str:
                from wish_builder.contracts import canonical_sha256

                return "sha256:" + canonical_sha256(self.to_primitive())

        command = MutableCommand()
        effect = prepared(command, event_number=9)
        command.value = "changed"
        with self.assertRaisesRegex(ValueError, "changed after preparation"):
            _ = effect.command_hash


class TrellisPortContractTests(unittest.TestCase):
    def test_runtime_protocols_accept_only_the_narrow_fake_surfaces(self) -> None:
        raw = b'{"complete":true,"tasks":[]}\n'
        snapshot = TrellisGraphSnapshot(
            export_version="wish-builder.trellis-graph.v1",
            trellis_version="0.6.15",
            parent_task_id="parent-001",
            revision=HASH_A,
            observed_at=FIXED_TIME,
            snapshot_bytes=raw,
            source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        self.assertIsInstance(FakeTrellisGraphPort(snapshot), TrellisGraphPort)
        self.assertIsInstance(FakeTrellisLifecyclePort(), TrellisLifecyclePort)
        self.assertIsInstance(
            FakeBackendChannelPort(capabilities()), BackendChannelPort
        )

    def test_graph_snapshot_is_hash_verified_and_fake_export_is_repeatable(self) -> None:
        raw = b'{"complete":true,"tasks":[]}\n'
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        snapshot = TrellisGraphSnapshot(
            export_version="wish-builder.trellis-graph.v1",
            trellis_version="0.6.15",
            parent_task_id="parent-001",
            revision=None,
            observed_at=FIXED_TIME,
            snapshot_bytes=raw,
            source_sha256=digest,
        )
        port = FakeTrellisGraphPort(snapshot)
        self.assertIs(snapshot, port.export_snapshot("parent-001"))
        self.assertIs(snapshot, port.export_snapshot("parent-001"))
        self.assertEqual(("parent-001", "parent-001"), port.calls)
        with self.assertRaises(LookupError):
            port.export_snapshot("parent-unknown")
        with self.assertRaisesRegex(ValueError, "does not match"):
            dataclasses.replace(snapshot, source_sha256=HASH_A)

    def test_command_and_capability_bytes_are_canonical_and_stable(self) -> None:
        command = prepare_command()
        self.assertEqual(command.canonical_json_bytes(), command.canonical_json_bytes())
        self.assertTrue(command.canonical_json_bytes().endswith(b"\n"))
        reserve = ReserveChannel(
            operation_id="OP-RESERVE-001",
            attempt_id="attempt-001",
            dispatch_id="DISPATCH-001",
            channel_id="channel-001",
            provider=WorkerProvider.CODEX,
            capability_digest=HASH_A,
            launch_profile_digest=HASH_B,
            policy_digest="sha256:" + "c" * 64,
        )
        self.assertEqual(reserve.canonical_sha256(), reserve.canonical_sha256())
        self.assertIn(b'"command_type":"reserve_channel"', reserve.canonical_json_bytes())
        backend_capabilities = capabilities()
        self.assertIn(b'"provider":"codex"', backend_capabilities.canonical_json_bytes())
        self.assertNotIn("trellis_version", backend_capabilities.to_primitive())
        self.assertFalse(hasattr(backend_capabilities, "trellis_version"))

    def test_channel_dispatch_identity_is_canonical_and_hash_bound(self) -> None:
        reserve = ReserveChannel(
            operation_id="OP-RESERVE-001",
            attempt_id="attempt-001",
            dispatch_id="DISPATCH-001",
            channel_id="channel-001",
            provider=WorkerProvider.CODEX,
            capability_digest=HASH_A,
            launch_profile_digest=HASH_B,
            policy_digest="sha256:" + "c" * 64,
        )
        packet = '{"task":"TASK-001"}'
        send = SendTaskPacket(
            operation_id="OP-SEND-001",
            attempt_id="attempt-001",
            dispatch_id="DISPATCH-001",
            channel_id="channel-001",
            message_id="message-001",
            turn_id="turn-001",
            task_packet=packet,
            task_packet_digest="sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest(),
        )

        for event_number, command in enumerate((reserve, send), start=1):
            with self.subTest(command=command.command_type):
                self.assertEqual("DISPATCH-001", command.to_primitive()["dispatch_id"])
                changed = dataclasses.replace(command, dispatch_id="DISPATCH-002")
                self.assertNotEqual(command.canonical_sha256(), changed.canonical_sha256())
                effect = prepared(command, event_number=event_number)
                with self.assertRaisesRegex(ValueError, "canonical hash"):
                    PreparedEffect(effect.request, changed)
                with self.assertRaisesRegex(ValueError, "dispatch_id.*stable token"):
                    dataclasses.replace(command, dispatch_id="contains spaces")

    def test_invalid_contract_values_are_rejected_at_construction(self) -> None:
        with self.assertRaisesRegex(ValueError, "Git object ID"):
            dataclasses.replace(prepare_command(), expected_base_commit="not-a-commit")
        with self.assertRaisesRegex(ValueError, "stable token"):
            dataclasses.replace(prepare_command(), operation_id="contains spaces")
        with self.assertRaisesRegex(ValueError, "positive"):
            dataclasses.replace(prepare_command(), attempt=0)
        with self.assertRaisesRegex(TypeError, "boolean"):
            dataclasses.replace(capabilities(), caller_supplied_ids=1)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
