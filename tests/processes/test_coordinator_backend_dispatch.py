from __future__ import annotations

from wish_builder.adapters import FilesystemExternalEvidenceStore

from wish_builder.adapters.fakes import FakeBackendChannelPort

import hashlib
import tempfile
import unittest
from pathlib import Path

from tests.processes.test_coordinator import (
    BASE_TIME,
    COORDINATOR_ID,
    CoordinatorHarness,
)
from wish_builder.contracts.runtime import (
    EffectOperation,
    JournalEventType,
    RuntimeState,
)
from wish_builder.processes import CoordinatorStatus, ForegroundCoordinator
from wish_builder.services import BackendDispatchEffectService, BackendDispatchPlan
from wish_builder.services.ports import (
    BackendCapabilities,
    ReserveChannel,
    SendTaskPacket,
)


class CoordinatorBackendDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.harness = CoordinatorHarness(self.root / "control")
        manifest = self.harness.manifest
        capabilities = BackendCapabilities(
            provider=manifest.provider,
            platform="windows",
            capability_digest=manifest.capability_digest,
            launch_profile_digest=manifest.launch_profile_digest,
            policy_digest=manifest.policy_digest,
            max_task_packet_bytes=4096,
        )
        self.channel = FakeBackendChannelPort(capabilities)
        effects = BackendDispatchEffectService(
            self.harness.journal,
            self.channel,
            FilesystemExternalEvidenceStore(self.root / "evidence"),
            coordinator_id=COORDINATOR_ID,
            fencing_token=1,
        )
        self.coordinator = ForegroundCoordinator(
            manifest,
            self.harness.coordinator.cursor,
            self.harness.journal,
            None,
            backend_effects=effects,
            backend_plan_factory=self._plan,
            coordinator_id=COORDINATOR_ID,
            owner=self.harness.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME,
        )

    def _plan(self, identity) -> BackendDispatchPlan:
        assert identity.correlation_id is not None
        packet = '{"run_id":"%s","task_id":"%s"}' % (
            identity.run_id,
            identity.task_id,
        )
        suffix = f"{identity.attempt:04d}"
        return BackendDispatchPlan(
            ReserveChannel(
                operation_id=f"BACKEND-RESERVE-{suffix}",
                attempt_id=f"ATTEMPT-{suffix}",
                dispatch_id=identity.correlation_id,
                channel_id=f"CHANNEL-{suffix}",
                provider=self.harness.manifest.provider,
                capability_digest=self.harness.manifest.capability_digest,
                launch_profile_digest=self.harness.manifest.launch_profile_digest,
                policy_digest=self.harness.manifest.policy_digest,
            ),
            SendTaskPacket(
                operation_id=f"BACKEND-SEND-{suffix}",
                attempt_id=f"ATTEMPT-{suffix}",
                dispatch_id=identity.correlation_id,
                channel_id=f"CHANNEL-{suffix}",
                message_id=f"MESSAGE-{suffix}",
                turn_id=f"TURN-{suffix}",
                task_packet=packet,
                task_packet_digest=(
                    "sha256:" + hashlib.sha256(packet.encode("utf-8")).hexdigest()
                ),
            ),
        )

    def test_parent_dispatch_is_observed_only_after_both_child_effects(self) -> None:
        reserved = self.coordinator.reserve_ready(limit=1)
        self.assertIs(reserved.status, CoordinatorStatus.PROGRESSED)
        identity = reserved.reserved[0]

        result = self.coordinator.dispatch_reserved(identity)

        self.assertIs(result.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual((identity,), result.dispatched)
        self.assertEqual(
            (
                JournalEventType.DISPATCH_REQUESTED,
                JournalEventType.EFFECT_REQUESTED,
                JournalEventType.EFFECT_OBSERVED,
                JournalEventType.EFFECT_REQUESTED,
                JournalEventType.EFFECT_OBSERVED,
                JournalEventType.DISPATCH_OBSERVED,
            ),
            tuple(event.event_type for event in result.events),
        )
        child_requests = tuple(
            event
            for event in result.events
            if event.event_type is JournalEventType.EFFECT_REQUESTED
        )
        self.assertEqual(
            (EffectOperation.RESERVE_CHANNEL, EffectOperation.SEND_TASK_PACKET),
            tuple(event.payload.operation for event in child_requests),
        )
        attempt = next(
            item
            for item in result.cursor.snapshot.attempts
            if item.task_id == identity.task_id
        )
        self.assertIs(attempt.state, RuntimeState.RUNNING)
        self.assertIs(
            dict(result.cursor.graph_index.task_states)[identity.task_id],
            RuntimeState.DISPATCHED,
        )
        self.assertEqual(2, self.channel.effect_count)


if __name__ == "__main__":
    unittest.main()
