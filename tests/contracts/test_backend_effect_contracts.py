from __future__ import annotations

import copy
import unittest

import wish_builder.contracts as public_contracts
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
)
from wish_builder.contracts.runtime_decoder import (
    decode_journal_event_bytes,
    decode_journal_event_primitive,
)


NOW = "2026-08-19T00:00:00Z"
PREVIOUS_HASH = "sha256:" + "0" * 63 + "1"


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _identity(correlation_id: str) -> ExecutionIdentity:
    return ExecutionIdentity(
        run_id="RUN-BACKEND",
        coordinator_epoch=3,
        task_id="TASK-001",
        attempt=1,
        correlation_id=correlation_id,
    )


def _request_event(
    operation: EffectOperation,
    object_type: EffectObjectType,
) -> JournalEvent:
    operation_id = operation.name.replace("_", "-")
    identity = _identity(f"OP-{operation_id}")
    return JournalEvent.create(
        sequence=7,
        event_id=f"EVENT-{operation_id}",
        event_type=JournalEventType.EFFECT_REQUESTED,
        identity=identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at=NOW,
        previous_event_hash=PREVIOUS_HASH,
        payload=EffectRequestPayload(
            operation=operation,
            adapter=AdapterKind.BACKEND,
            object_type=object_type,
            normalized_target_hash=_hash("a"),
            request_payload_hash=_hash("b"),
            expected_sequence=7,
            fencing_token=3,
        ),
    )


def _observation_event(
    operation: EffectOperation,
    object_type: EffectObjectType,
) -> JournalEvent:
    request = _request_event(operation, object_type)
    receipt = EffectReceipt(
        schema_version=1,
        identity=request.identity,
        operation=operation,
        status=EffectStatus.APPLIED,
        observed_at=NOW,
        effect_hash=_hash("c"),
        external_object_id=f"trellis-{object_type.value}-001",
    )
    return JournalEvent.create(
        sequence=8,
        event_id=f"EVENT-{operation.name.replace('_', '-')}-OBSERVED",
        event_type=JournalEventType.EFFECT_OBSERVED,
        identity=request.identity,
        actor_type=ActorType.ADAPTER,
        actor_id="backend-channel-adapter",
        recorded_at=NOW,
        previous_event_hash=request.event_hash,
        payload=EffectObservationPayload(AdapterKind.BACKEND, receipt),
    )


BACKEND_EFFECTS = (
    (EffectOperation.RESERVE_CHANNEL, EffectObjectType.CHANNEL),
    (EffectOperation.SEND_TASK_PACKET, EffectObjectType.TASK_PACKET),
    (EffectOperation.CANCEL_TURN, EffectObjectType.TURN),
)


class BackendEffectContractTests(unittest.TestCase):
    def test_backend_effect_vocabulary_is_public_and_stable(self) -> None:
        self.assertIs(public_contracts.AdapterKind.BACKEND, AdapterKind.BACKEND)
        self.assertEqual("backend", AdapterKind.BACKEND.value)
        self.assertEqual("reserve_channel", EffectOperation.RESERVE_CHANNEL.value)
        self.assertEqual("send_task_packet", EffectOperation.SEND_TASK_PACKET.value)
        self.assertEqual("cancel_turn", EffectOperation.CANCEL_TURN.value)
        self.assertEqual("channel", EffectObjectType.CHANNEL.value)
        self.assertEqual("task_packet", EffectObjectType.TASK_PACKET.value)
        self.assertEqual("turn", EffectObjectType.TURN.value)

    def test_backend_effect_requests_and_observations_round_trip(self) -> None:
        for operation, object_type in BACKEND_EFFECTS:
            for event in (
                _request_event(operation, object_type),
                _observation_event(operation, object_type),
            ):
                with self.subTest(operation=operation, event_type=event.event_type):
                    result = decode_journal_event_bytes(event.canonical_json_bytes())
                    self.assertTrue(result.ok, result.report.render_text())
                    self.assertEqual(event, result.value)
                    self.assertEqual(
                        event.canonical_json_bytes(),
                        result.value.canonical_json_bytes(),
                    )

    def test_backend_effect_constructors_reject_untyped_tokens(self) -> None:
        payload = _request_event(
            EffectOperation.RESERVE_CHANNEL,
            EffectObjectType.CHANNEL,
        ).payload
        cases = (
            {"operation": "reserve_channel"},
            {"adapter": "backend"},
            {"object_type": "channel"},
        )
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(TypeError):
                EffectRequestPayload(
                    operation=updates.get("operation", payload.operation),  # type: ignore[arg-type]
                    adapter=updates.get("adapter", payload.adapter),  # type: ignore[arg-type]
                    object_type=updates.get("object_type", payload.object_type),  # type: ignore[arg-type]
                    normalized_target_hash=payload.normalized_target_hash,
                    request_payload_hash=payload.request_payload_hash,
                    expected_sequence=payload.expected_sequence,
                    fencing_token=payload.fencing_token,
                )

    def test_strict_decoder_rejects_unknown_backend_effect_tokens(self) -> None:
        primitive = _request_event(
            EffectOperation.RESERVE_CHANNEL,
            EffectObjectType.CHANNEL,
        ).to_primitive()
        cases = (
            ("operation", "reserve_channel_v2"),
            ("adapter", "backend_v2"),
            ("object_type", "channel_v2"),
        )
        for field, invalid_value in cases:
            tampered = copy.deepcopy(primitive)
            tampered["payload"][field] = invalid_value  # type: ignore[index]
            with self.subTest(field=field):
                result = decode_journal_event_primitive(tampered)
                self.assertFalse(result.ok)
                self.assertIn(
                    f"/payload/{field}",
                    {issue.path_text for issue in result.issues},
                )


if __name__ == "__main__":
    unittest.main()
