from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.contracts.test_branch_floor_closure import _hash, _lease_event, _lease_payload
from tests.kernel.test_state import freeze_graph, hash_ref, manifest_from
from wish_builder.contracts.runtime import (
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
)
from wish_builder.kernel import state as state_kernel
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.state import ApplyReason, KernelSnapshot, apply_journal_event


def forged_event(event: JournalEvent, **changes: object) -> JournalEvent:
    forged = object.__new__(JournalEvent)
    for field in dataclasses.fields(event):
        object.__setattr__(
            forged,
            field.name,
            changes.get(field.name, getattr(event, field.name)),
        )
    return forged


class StateChangedBranchCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dag = TaskDag.compile(manifest_from())

    def test_snapshot_rejects_an_identity_constructor_that_changes_values(self) -> None:
        with (
            mock.patch.object(
                state_kernel,
                "ExecutionIdentity",
                return_value=SimpleNamespace(
                    run_id="WISH-OTHER",
                    coordinator_epoch=1,
                ),
            ),
            self.assertRaisesRegex(ValueError, "not canonical"),
        ):
            KernelSnapshot(
                run_id="WISH-001",
                coordinator_epoch=1,
                phase=RuntimeState.NONE,
                status=RuntimeState.RUNNING,
                run_reason_code=None,
                tasks=(),
                attempts=(),
                last_sequence=0,
                last_event_id=None,
                last_event_hash=state_kernel.GENESIS_HASH,
            )

    def test_lease_events_reject_wrong_kind_and_stale_epochs(self) -> None:
        initial = KernelSnapshot.initial("RUN-LEASE", 4, self.dag)
        snapshot = dataclasses.replace(
            initial,
            last_sequence=1,
            last_event_id="EVENT-LEASE-PREFIX",
            last_event_hash=_hash("b"),
        )
        valid_acquired = _lease_event(
            JournalEventType.LEASE_ACQUIRED,
            _lease_payload(fencing_token=4),
            identity=ExecutionIdentity("RUN-LEASE", 4),
        )
        acquired = forged_event(
            valid_acquired,
            identity=ExecutionIdentity("RUN-LEASE", 3),
        )
        wrong_kind = forged_event(
            acquired,
            event_type=JournalEventType.RUN_INITIALIZED,
        )
        self.assertEqual(
            ApplyReason.UNSUPPORTED_EVENT,
            apply_journal_event(snapshot, wrong_kind).reason,
        )
        self.assertEqual(
            ApplyReason.STALE_EPOCH,
            apply_journal_event(snapshot, acquired).reason,
        )

        valid_renewed = _lease_event(
            JournalEventType.LEASE_RENEWED,
            _lease_payload(fencing_token=4),
            identity=ExecutionIdentity("RUN-LEASE", 4),
        )
        renewed = forged_event(
            valid_renewed,
            identity=ExecutionIdentity("RUN-LEASE", 3),
        )
        self.assertEqual(
            ApplyReason.STALE_EPOCH,
            apply_journal_event(snapshot, renewed).reason,
        )

    def _promotion_event(
        self,
        snapshot: KernelSnapshot,
        identity: ExecutionIdentity,
    ) -> JournalEvent:
        receipt = EffectReceipt(
            1,
            identity,
            EffectOperation.RESULT_PROMOTION,
            EffectStatus.APPLIED,
            "2026-08-20T00:00:00Z",
            effect_hash=hash_ref(90),
            external_object_id="promotion-coverage",
        )
        return JournalEvent.create(
            sequence=snapshot.last_sequence + 1,
            event_id="EVENT-PROMOTION-COVERAGE",
            event_type=JournalEventType.PROMOTION_OBSERVED,
            identity=identity,
            actor_type=ActorType.ADAPTER,
            actor_id="git-worktree-adapter",
            recorded_at="2026-08-20T00:00:00Z",
            previous_event_hash=snapshot.last_event_hash,
            payload=EffectObservationPayload(AdapterKind.GIT, receipt),
        )

    def test_promotion_rejects_each_identity_and_state_mismatch(self) -> None:
        initial = KernelSnapshot.initial("WISH-001", 1, self.dag)
        frozen, _ = freeze_graph(initial)
        staged = dataclasses.replace(
            frozen,
            tasks=(
                dataclasses.replace(frozen.tasks[0], state=RuntimeState.STAGED),
                *frozen.tasks[1:],
            ),
        )
        identity = ExecutionIdentity(
            "WISH-001",
            1,
            "TASK-001",
            1,
            "PROMOTION-COVERAGE",
        )
        valid = self._promotion_event(staged, identity)

        missing_attempt = forged_event(
            valid,
            identity=ExecutionIdentity("WISH-001", 1),
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_journal_event(staged, missing_attempt).reason,
        )

        mismatched_receipt = dataclasses.replace(
            valid.payload.receipt,
            identity=ExecutionIdentity(
                "WISH-001",
                1,
                "TASK-001",
                1,
                "PROMOTION-OTHER",
            ),
        )
        wrong_receipt = forged_event(
            valid,
            payload=EffectObservationPayload(AdapterKind.GIT, mismatched_receipt),
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_journal_event(staged, wrong_receipt).reason,
        )

        unknown_identity = ExecutionIdentity(
            "WISH-001",
            1,
            "TASK-999",
            1,
            "PROMOTION-UNKNOWN",
        )
        unknown_task = self._promotion_event(staged, unknown_identity)
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_journal_event(staged, unknown_task).reason,
        )

        wrong_state = dataclasses.replace(
            staged,
            tasks=(
                dataclasses.replace(staged.tasks[0], state=RuntimeState.DISPATCHED),
                *staged.tasks[1:],
            ),
        )
        self.assertEqual(
            ApplyReason.STATE_MISMATCH,
            apply_journal_event(wrong_state, valid).reason,
        )

    def test_dispatch_rejects_a_receipt_identity_that_is_ahead(self) -> None:
        initial = KernelSnapshot.initial("WISH-001", 1, self.dag)
        frozen, _ = freeze_graph(initial)
        identity = ExecutionIdentity(
            "WISH-001",
            1,
            "TASK-001",
            1,
            "DISPATCH-COVERAGE",
        )
        receipt = EffectReceipt(
            1,
            identity,
            EffectOperation.WORKER_DISPATCH,
            EffectStatus.APPLIED,
            "2026-08-20T00:00:00Z",
            effect_hash=hash_ref(91),
            external_object_id="worker-coverage",
        )
        event = JournalEvent.create(
            sequence=frozen.last_sequence + 1,
            event_id="EVENT-DISPATCH-COVERAGE",
            event_type=JournalEventType.DISPATCH_OBSERVED,
            identity=identity,
            actor_type=ActorType.ADAPTER,
            actor_id="task-adapter",
            recorded_at="2026-08-20T00:00:00Z",
            previous_event_hash=frozen.last_event_hash,
            payload=EffectObservationPayload(AdapterKind.TASK, receipt),
        )
        ahead_receipt = dataclasses.replace(
            receipt,
            identity=dataclasses.replace(identity, coordinator_epoch=2),
        )
        forged = forged_event(
            event,
            payload=EffectObservationPayload(AdapterKind.TASK, ahead_receipt),
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_journal_event(frozen, forged).reason,
        )


if __name__ == "__main__":
    unittest.main()
