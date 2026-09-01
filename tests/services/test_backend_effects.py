from __future__ import annotations

from wish_builder.adapters import FilesystemExternalEvidenceStore

from wish_builder.adapters.fakes import FakeBackendChannelPort

import dataclasses
import hashlib
import tempfile
import unittest
from pathlib import Path

from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import canonical_sha256
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEventType,
)
from wish_builder.services import (
    GENESIS_HEAD,
    DurableJournal,
    JournalEventDraft,
    BackendDispatchEffectCrash,
    BackendDispatchEffectReason,
    BackendDispatchEffectService,
    BackendDispatchEffectStatus,
    BackendDispatchPlan,
)
from wish_builder.services.ports import (
    CancelTurn,
    BackendCapabilities,
    PersistedEffectRequest,
    ReserveChannel,
    SendTaskPacket,
    TurnState,
)
from wish_builder.contracts import WorkerProvider


RUN_ID = "RUN-BACKEND-EFFECTS"
TASK_ID = "TASK-001"
DISPATCH_ID = "DISPATCH-001"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def capabilities() -> BackendCapabilities:
    return BackendCapabilities(
        provider=WorkerProvider.CODEX,
        platform="windows",
        capability_digest=HASH_A,
        launch_profile_digest=HASH_B,
        policy_digest=HASH_C,
        max_task_packet_bytes=4096,
    )


def plan() -> BackendDispatchPlan:
    packet = '{"task_id":"TASK-001"}'
    return BackendDispatchPlan(
        ReserveChannel(
            operation_id="BACKEND-RESERVE-001",
            attempt_id="ATTEMPT-001",
            dispatch_id=DISPATCH_ID,
            channel_id="CHANNEL-001",
            provider=WorkerProvider.CODEX,
            capability_digest=HASH_A,
            launch_profile_digest=HASH_B,
            policy_digest=HASH_C,
        ),
        SendTaskPacket(
            operation_id="BACKEND-SEND-001",
            attempt_id="ATTEMPT-001",
            dispatch_id=DISPATCH_ID,
            channel_id="CHANNEL-001",
            message_id="MESSAGE-001",
            turn_id="TURN-001",
            task_packet=packet,
            task_packet_digest=(
                "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()
            ),
        ),
    )


def cancel_command() -> CancelTurn:
    return CancelTurn(
        operation_id="BACKEND-CANCEL-001",
        attempt_id="ATTEMPT-001",
        channel_id="CHANNEL-001",
        turn_id="TURN-001",
        reason_code="attempt-timeout",
    )


class BackendDispatchEffectServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(self.root / "journal", RUN_ID),
        )
        self.parent = self._append_parent_request()
        self.evidence = FilesystemExternalEvidenceStore(self.root / "evidence")

    def _append_parent_request(self) -> PersistedEffectRequest:
        identity = ExecutionIdentity(RUN_ID, 1, TASK_ID, 1, DISPATCH_ID)
        target_hash = "sha256:" + canonical_sha256(
            {"run_id": RUN_ID, "task_id": TASK_ID}
        )
        request_hash = "sha256:" + canonical_sha256(
            {"identity": identity.to_primitive()}
        )
        result = self.journal.append_draft(
            JournalEventDraft(
                event_id="EVENT-DISPATCH-REQUESTED-00000001",
                event_type=JournalEventType.DISPATCH_REQUESTED,
                identity=identity,
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                payload=EffectRequestPayload(
                    EffectOperation.WORKER_DISPATCH,
                    AdapterKind.TASK,
                    EffectObjectType.WORKER,
                    target_hash,
                    request_hash,
                    0,
                    1,
                ),
            ),
            expected_head=GENESIS_HEAD,
        )
        return PersistedEffectRequest.from_append_result(result)

    def service(self, channel=None, *, failpoint=None, fencing_token=1):
        return BackendDispatchEffectService(
            self.journal,
            channel or FakeBackendChannelPort(capabilities()),
            self.evidence,
            coordinator_id="coordinator-001",
            fencing_token=fencing_token,
            failpoint=failpoint,
        )

    def test_dispatch_persists_two_exact_child_requests_and_observations(self) -> None:
        dispatch_plan = plan()
        result = self.service().dispatch(self.parent, dispatch_plan)

        self.assertIs(result.status, BackendDispatchEffectStatus.APPLIED)
        self.assertIs(result.reason, BackendDispatchEffectReason.NONE)
        self.assertEqual(
            (
                JournalEventType.EFFECT_REQUESTED,
                JournalEventType.EFFECT_OBSERVED,
                JournalEventType.EFFECT_REQUESTED,
                JournalEventType.EFFECT_OBSERVED,
            ),
            tuple(event.event_type for event in result.events),
        )
        requests = result.events[::2]
        observations = result.events[1::2]
        self.assertEqual(
            (dispatch_plan.reserve.operation_id, dispatch_plan.send.operation_id),
            tuple(event.identity.correlation_id for event in requests),
        )
        self.assertEqual(
            (
                dispatch_plan.reserve.canonical_sha256(),
                dispatch_plan.send.canonical_sha256(),
            ),
            tuple(event.payload.request_payload_hash for event in requests),
        )
        self.assertEqual(
            (EffectOperation.RESERVE_CHANNEL, EffectOperation.SEND_TASK_PACKET),
            tuple(event.payload.operation for event in requests),
        )
        self.assertTrue(
            all(
                type(event.payload) is EffectObservationPayload
                and event.payload.adapter is AdapterKind.BACKEND
                for event in observations
            )
        )
        assert result.receipt is not None
        self.assertIs(result.receipt.status, EffectStatus.APPLIED)
        self.assertEqual(2, len(result.receipt.evidence))
        for evidence in result.receipt.evidence:
            self.assertEqual(evidence.digest, "sha256:" + hashlib.sha256(
                self.evidence.read(evidence.digest)
            ).hexdigest())

    def test_unknown_send_is_durable_and_never_reported_as_applied(self) -> None:
        dispatch_plan = plan()
        channel = FakeBackendChannelPort(
            capabilities(),
            unknown_operation_ids={dispatch_plan.send.operation_id},
        )
        result = self.service(channel).dispatch(self.parent, dispatch_plan)

        self.assertIs(result.status, BackendDispatchEffectStatus.BLOCKED)
        self.assertIs(
            result.reason, BackendDispatchEffectReason.EFFECT_OUTCOME_UNKNOWN
        )
        self.assertEqual(4, len(result.events))
        assert result.receipt is not None
        self.assertIs(result.receipt.status, EffectStatus.UNKNOWN)
        self.assertTrue(result.receipt.evidence)

    def test_cancel_persists_its_own_request_and_observation(self) -> None:
        dispatch_plan = plan()
        command = cancel_command()
        channel = FakeBackendChannelPort(
            capabilities(),
            send_state=TurnState.RUNNING,
        )
        service = self.service(channel)
        dispatched = service.dispatch(self.parent, dispatch_plan)

        result = service.cancel(
            self.parent,
            command,
            expected_head=dispatched.head,
        )

        self.assertIs(result.status, BackendDispatchEffectStatus.APPLIED)
        self.assertIs(result.reason, BackendDispatchEffectReason.NONE)
        self.assertEqual(
            (JournalEventType.EFFECT_REQUESTED, JournalEventType.EFFECT_OBSERVED),
            tuple(event.event_type for event in result.events),
        )
        request, observed = result.events
        self.assertEqual(command.operation_id, request.identity.correlation_id)
        self.assertIs(request.payload.operation, EffectOperation.CANCEL_TURN)
        self.assertIs(request.payload.adapter, AdapterKind.BACKEND)
        self.assertIs(request.payload.object_type, EffectObjectType.TURN)
        self.assertEqual(command.canonical_sha256(), request.payload.request_payload_hash)
        self.assertEqual(
            "sha256:"
            + canonical_sha256(
                {
                    "adapter": AdapterKind.BACKEND.value,
                    "attempt_id": command.attempt_id,
                    "channel_id": command.channel_id,
                    "dispatch_id": DISPATCH_ID,
                    "operation": EffectOperation.CANCEL_TURN.value,
                    "run_id": RUN_ID,
                    "task_id": TASK_ID,
                    "turn_id": command.turn_id,
                }
            ),
            request.payload.normalized_target_hash,
        )
        self.assertIsInstance(observed.payload, EffectObservationPayload)
        assert result.receipt is not None
        assert result.turn is not None
        self.assertIs(result.receipt.operation, EffectOperation.CANCEL_TURN)
        self.assertIs(result.receipt.status, EffectStatus.APPLIED)
        self.assertEqual(command.turn_id, result.receipt.external_object_id)
        self.assertIs(result.turn.state, TurnState.CANCELLED)
        self.assertIsNone(result.reservation)
        self.assertEqual(3, channel.effect_count)
        self.assertEqual(1, len(result.receipt.evidence))
        evidence = result.receipt.evidence[0]
        self.assertEqual(
            evidence.digest,
            "sha256:" + hashlib.sha256(self.evidence.read(evidence.digest)).hexdigest(),
        )

    def test_unknown_cancel_is_durable_and_never_reported_as_applied(self) -> None:
        dispatch_plan = plan()
        command = cancel_command()
        channel = FakeBackendChannelPort(
            capabilities(),
            unknown_operation_ids={command.operation_id},
            send_state=TurnState.RUNNING,
        )
        service = self.service(channel)
        dispatched = service.dispatch(self.parent, dispatch_plan)

        result = service.cancel(
            self.parent,
            command,
            expected_head=dispatched.head,
        )

        self.assertIs(result.status, BackendDispatchEffectStatus.BLOCKED)
        self.assertIs(
            result.reason,
            BackendDispatchEffectReason.EFFECT_OUTCOME_UNKNOWN,
        )
        self.assertEqual(2, len(result.events))
        assert result.receipt is not None
        self.assertIs(result.receipt.operation, EffectOperation.CANCEL_TURN)
        self.assertIs(result.receipt.status, EffectStatus.UNKNOWN)
        self.assertTrue(result.receipt.evidence)

    def test_takeover_epoch_can_cancel_but_cannot_dispatch_an_old_parent(self) -> None:
        dispatch_plan = plan()
        command = cancel_command()
        channel = FakeBackendChannelPort(
            capabilities(),
            send_state=TurnState.RUNNING,
        )
        dispatched = self.service(channel).dispatch(self.parent, dispatch_plan)
        takeover = self.service(channel, fencing_token=2)

        blocked = takeover.dispatch(self.parent, dispatch_plan)
        cancelled = takeover.cancel(
            self.parent,
            command,
            expected_head=dispatched.head,
        )

        self.assertIs(blocked.status, BackendDispatchEffectStatus.BLOCKED)
        self.assertIs(
            blocked.reason,
            BackendDispatchEffectReason.PARENT_REQUEST_INVALID,
        )
        self.assertEqual(self.parent.append_result.head, blocked.head)
        self.assertEqual((), blocked.events)
        self.assertIs(cancelled.status, BackendDispatchEffectStatus.APPLIED)
        self.assertEqual(2, len(cancelled.events))
        request, observation = cancelled.events
        self.assertEqual(2, request.identity.coordinator_epoch)
        self.assertEqual(2, request.payload.fencing_token)
        self.assertEqual(2, observation.identity.coordinator_epoch)
        assert cancelled.receipt is not None
        self.assertEqual(2, cancelled.receipt.identity.coordinator_epoch)

    def test_cancel_evidence_is_durable_before_journal_observation(self) -> None:
        dispatch_plan = plan()
        command = cancel_command()

        def failpoint(point: str, operation_id: str) -> None:
            if point == "after_evidence_store" and operation_id == command.operation_id:
                raise BackendDispatchEffectCrash(point)

        channel = FakeBackendChannelPort(
            capabilities(),
            send_state=TurnState.RUNNING,
        )
        service = self.service(channel, failpoint=failpoint)
        dispatched = service.dispatch(self.parent, dispatch_plan)

        with self.assertRaises(BackendDispatchEffectCrash):
            service.cancel(
                self.parent,
                command,
                expected_head=dispatched.head,
            )

        segment = self.root / "journal" / "segments" / "segment-00000001.jsonl"
        self.assertEqual(6, len(segment.read_bytes().splitlines()))
        evidence_objects = tuple((self.root / "evidence" / "objects" / "sha256").glob("*.json"))
        self.assertEqual(3, len(evidence_objects))

    def test_crash_after_reservation_effect_leaves_one_pending_child_request(self) -> None:
        dispatch_plan = plan()

        def failpoint(point: str, operation_id: str) -> None:
            if point == "after_adapter_call" and operation_id == dispatch_plan.reserve.operation_id:
                raise BackendDispatchEffectCrash(point)

        with self.assertRaises(BackendDispatchEffectCrash):
            self.service(failpoint=failpoint).dispatch(self.parent, dispatch_plan)

        segment = self.root / "journal" / "segments" / "segment-00000001.jsonl"
        self.assertEqual(2, len(segment.read_bytes().splitlines()))

    def test_dispatch_plan_requires_one_parent_dispatch_identity(self) -> None:
        dispatch_plan = plan()
        with self.assertRaisesRegex(ValueError, "same dispatch channel"):
            BackendDispatchPlan(
                dispatch_plan.reserve,
                dataclasses.replace(
                    dispatch_plan.send,
                    dispatch_id="DISPATCH-OTHER",
                ),
            )


if __name__ == "__main__":
    unittest.main()
