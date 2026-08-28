from __future__ import annotations

import hashlib

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
from wish_builder.services.journal import AppendResult, AppendStatus, JournalHead
from wish_builder.services.ports import PreparedCommand, PreparedEffect

FIXED_TIME = "2026-08-18T05:00:00Z"
GENESIS_HASH = "sha256:" + "0" * 64
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
BASE_COMMIT = "1" * 40
HEAD_COMMIT = "2" * 40


def packet_digest(packet: str) -> str:
    return "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()


def prepared(
    command: PreparedCommand,
    *,
    event_number: int = 1,
) -> PreparedEffect[PreparedCommand]:
    identity = ExecutionIdentity(
        "WISH-001",
        1,
        "TASK-001",
        1,
        command.operation_id,
    )
    event = JournalEvent.create(
        sequence=1,
        event_id=f"EVENT-TRELLIS-{event_number:03d}",
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
            command.canonical_sha256(),  # type: ignore[attr-defined]
            0,
            1,
        ),
    )
    result = AppendResult(
        AppendStatus.COMMITTED,
        JournalHead(event.sequence, event.event_hash),
        event,
    )
    return PreparedEffect.from_append_result(result, command)
