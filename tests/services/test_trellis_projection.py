from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.contracts.test_manifest_v2 import valid_manifest_v2
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    AdapterKind,
    ActorType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectStatus,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
    decode_manifest_v2_primitive,
)
from wish_builder.services import DurableJournal, GENESIS_HEAD
from wish_builder.services.ports import (
    TrellisProjectionApplyRequest,
    TrellisProjectionDisposition,
    TrellisProjectionObservation,
    TrellisProjectionReason,
)
from wish_builder.services.trellis_projection import (
    TrellisProjectionService,
    TrellisProjectionSyncStatus,
)


REVISION_A = "sha256:" + "1" * 64
REVISION_B = "sha256:" + "2" * 64


class _CheckoutProvider:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0

    def ensure(self, run_id: str) -> Path:
        self.calls += 1
        return self.path


class _ProjectionPort:
    def __init__(self) -> None:
        self.revision = REVISION_A
        self.projection = None
        self.task_status = "planning"
        self.unavailable = False
        self.conflict_reason = None
        self.race_once = False
        self.apply_calls: list[TrellisProjectionApplyRequest] = []

    def inspect(self, checkout_root: Path, trellis_task_id: str):
        if self.unavailable:
            return TrellisProjectionObservation(
                TrellisProjectionDisposition.UNAVAILABLE,
                TrellisProjectionReason.UNAVAILABLE,
            )
        return TrellisProjectionObservation(
            TrellisProjectionDisposition.INSPECTED,
            TrellisProjectionReason.NONE,
            self.revision,
            512,
            self.task_status,
            self.projection,
        )

    def apply(self, request: TrellisProjectionApplyRequest):
        self.apply_calls.append(request)
        if self.race_once:
            self.race_once = False
            self.revision = REVISION_B
            return TrellisProjectionObservation(
                TrellisProjectionDisposition.CONFLICT,
                TrellisProjectionReason.REVISION_CONFLICT,
                REVISION_B,
                512,
                self.task_status,
                self.projection,
            )
        if self.conflict_reason is not None:
            return TrellisProjectionObservation(
                TrellisProjectionDisposition.CONFLICT,
                self.conflict_reason,
                self.revision,
                512,
                self.task_status,
                self.projection,
            )
        if self.projection == request.projection:
            return TrellisProjectionObservation(
                TrellisProjectionDisposition.IDEMPOTENT,
                TrellisProjectionReason.NONE,
                self.revision,
                512,
                self.task_status,
                self.projection,
            )
        self.projection = request.projection
        self.task_status = request.projection.target_status
        self.revision = REVISION_B
        return TrellisProjectionObservation(
            TrellisProjectionDisposition.APPLIED,
            TrellisProjectionReason.NONE,
            self.revision,
            768,
            self.task_status,
            self.projection,
        )


class TrellisProjectionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        decoded = decode_manifest_v2_primitive(valid_manifest_v2())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        self.manifest = decoded.value
        assert self.manifest is not None
        self.journal = DurableJournal(
            self.manifest.run_id,
            FilesystemJournalStorage(
                self.root / "journal",
                self.manifest.run_id,
            ),
        )
        self.checkout = _CheckoutProvider(self.root)
        self.port = _ProjectionPort()
        self.service = TrellisProjectionService(
            self.manifest,
            self.journal,
            self.checkout,
            self.port,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_projection_is_denied_until_the_canonical_event_is_durable(self) -> None:
        event = _task_event(self.manifest.run_id, 1, GENESIS_HEAD.event_hash)
        delayed = self.service.project_committed_event(event)
        self.assertIs(delayed.status, TrellisProjectionSyncStatus.DELAYED)
        self.assertIs(
            delayed.reason,
            TrellisProjectionReason.CANONICAL_NOT_DURABLE,
        )
        self.assertEqual([], self.port.apply_calls)

        appended = self.journal.append(event, expected_head=GENESIS_HEAD)
        self.assertTrue(appended.durable)
        applied = self.service.project_committed_event(event)
        self.assertIs(applied.status, TrellisProjectionSyncStatus.APPLIED)
        self.assertEqual("planning", self.port.task_status)
        self.assertEqual(event.sequence, self.port.projection.canonical_sequence)
        self.assertEqual(event.event_hash, self.port.projection.canonical_event_hash)

    def test_outage_after_local_durability_delays_only_the_projection(self) -> None:
        event = _task_event(self.manifest.run_id, 1, GENESIS_HEAD.event_hash)
        appended = self.journal.append(event, expected_head=GENESIS_HEAD)
        self.assertTrue(appended.durable)
        self.port.unavailable = True
        delayed = self.service.project_committed_event(event)
        self.assertIs(delayed.status, TrellisProjectionSyncStatus.DELAYED)
        self.assertIs(delayed.reason, TrellisProjectionReason.UNAVAILABLE)
        self.assertEqual(event.event_hash, appended.head.event_hash)
        self.assertEqual([], self.port.apply_calls)

    def test_durable_dispatch_observation_projects_task_in_progress(self) -> None:
        event = _dispatch_observed_event(
            self.manifest.run_id,
            1,
            GENESIS_HEAD.event_hash,
        )
        appended = self.journal.append(event, expected_head=GENESIS_HEAD)
        self.assertTrue(appended.durable)

        result = self.service.project_committed_event(event)

        self.assertIs(result.status, TrellisProjectionSyncStatus.APPLIED)
        self.assertEqual("in_progress", self.port.task_status)
        self.assertEqual("dispatched", self.port.projection.canonical_state)
        self.assertEqual("in_progress", self.port.projection.target_status)
        self.assertEqual(event.sequence, self.port.projection.canonical_sequence)
        self.assertEqual(event.event_hash, self.port.projection.canonical_event_hash)

    def test_durable_task_verification_projects_task_completed(self) -> None:
        event = _task_verified_event(
            self.manifest.run_id,
            1,
            GENESIS_HEAD.event_hash,
        )
        appended = self.journal.append(event, expected_head=GENESIS_HEAD)
        self.assertTrue(appended.durable)

        result = self.service.project_committed_event(event)

        self.assertIs(result.status, TrellisProjectionSyncStatus.APPLIED)
        self.assertEqual("completed", self.port.task_status)
        self.assertEqual("verified", self.port.projection.canonical_state)
        self.assertEqual("completed", self.port.projection.target_status)
        self.assertEqual(event.sequence, self.port.projection.canonical_sequence)
        self.assertEqual(event.event_hash, self.port.projection.canonical_event_hash)

    def test_revision_race_fails_closed_without_reinspection(self) -> None:
        event = _task_event(self.manifest.run_id, 1, GENESIS_HEAD.event_hash)
        self.journal.append(event, expected_head=GENESIS_HEAD)
        self.port.race_once = True
        result = self.service.project_committed_event(event)
        self.assertIs(result.status, TrellisProjectionSyncStatus.CONFLICT)
        self.assertIs(result.reason, TrellisProjectionReason.REVISION_CONFLICT)
        self.assertEqual(1, len(self.port.apply_calls))
        self.assertEqual(REVISION_A, self.port.apply_calls[0].expected_revision)
        self.assertEqual("planning", self.port.task_status)
        self.assertIsNone(self.port.projection)

    def test_ahead_or_manually_drifted_projection_never_rolls_back_canonical_state(self) -> None:
        for reason in (
            TrellisProjectionReason.AHEAD,
            TrellisProjectionReason.DIGEST_MISMATCH,
            TrellisProjectionReason.STATUS_MISMATCH,
        ):
            with self.subTest(reason=reason):
                root = self.root / reason.value
                journal = DurableJournal(
                    self.manifest.run_id,
                    FilesystemJournalStorage(root, self.manifest.run_id),
                )
                event = _task_event(self.manifest.run_id, 1, GENESIS_HEAD.event_hash)
                appended = journal.append(event, expected_head=GENESIS_HEAD)
                port = _ProjectionPort()
                port.conflict_reason = reason
                service = TrellisProjectionService(
                    self.manifest,
                    journal,
                    self.checkout,
                    port,
                )
                result = service.project_committed_event(event)
                self.assertIs(result.status, TrellisProjectionSyncStatus.CONFLICT)
                self.assertIs(result.reason, reason)
                self.assertEqual(event.event_hash, appended.head.event_hash)

    def test_restart_replays_the_exact_projection_idempotently(self) -> None:
        event = _task_event(self.manifest.run_id, 1, GENESIS_HEAD.event_hash)
        self.journal.append(event, expected_head=GENESIS_HEAD)
        first = self.service.project_committed_event(event)
        restarted = TrellisProjectionService(
            self.manifest,
            self.journal,
            self.checkout,
            self.port,
        )
        second = restarted.project_committed_event(event)
        self.assertIs(first.status, TrellisProjectionSyncStatus.APPLIED)
        self.assertIs(second.status, TrellisProjectionSyncStatus.APPLIED)
        self.assertIs(
            second.observation.disposition,
            TrellisProjectionDisposition.IDEMPOTENT,
        )
        self.assertEqual(
            self.port.apply_calls[0].projection,
            self.port.apply_calls[1].projection,
        )


def _task_event(run_id: str, sequence: int, previous_hash: str) -> JournalEvent:
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-TASK-READY-{sequence:08d}",
        event_type=JournalEventType.TASK_READY,
        identity=ExecutionIdentity(run_id, 1, "TASK-001"),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-test",
        recorded_at="2026-08-19T05:00:00Z",
        previous_event_hash=previous_hash,
        payload=TransitionPayload(
            TransitionSubject.TASK,
            RuntimeState.APPROVED,
            RuntimeState.READY,
        ),
    )


def _dispatch_observed_event(
    run_id: str,
    sequence: int,
    previous_hash: str,
) -> JournalEvent:
    identity = ExecutionIdentity(run_id, 1, "TASK-001", 1, "DISPATCH-001")
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-DISPATCH-OBSERVED-{sequence:08d}",
        event_type=JournalEventType.DISPATCH_OBSERVED,
        identity=identity,
        actor_type=ActorType.ADAPTER,
        actor_id="task-adapter",
        recorded_at="2026-08-19T05:00:00Z",
        previous_event_hash=previous_hash,
        payload=EffectObservationPayload(
            AdapterKind.TASK,
            EffectReceipt(
                schema_version=1,
                identity=identity,
                operation=EffectOperation.WORKER_DISPATCH,
                status=EffectStatus.APPLIED,
                observed_at="2026-08-19T05:00:00Z",
                effect_hash="sha256:" + "3" * 64,
                external_object_id="worker-turn-001",
            ),
        ),
    )


def _task_verified_event(
    run_id: str,
    sequence: int,
    previous_hash: str,
) -> JournalEvent:
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-TASK-VERIFIED-{sequence:08d}",
        event_type=JournalEventType.TASK_VERIFIED,
        identity=ExecutionIdentity(run_id, 1, "TASK-001"),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-test",
        recorded_at="2026-08-19T05:00:00Z",
        previous_event_hash=previous_hash,
        payload=TransitionPayload(
            TransitionSubject.TASK,
            RuntimeState.PROMOTED,
            RuntimeState.VERIFIED,
        ),
    )


if __name__ == "__main__":
    unittest.main()
