from __future__ import annotations

import unittest
from dataclasses import replace

from tests.processes.test_coordinator import one_task_manifest
from wish_builder.contracts import (
    ActorIdentity,
    ActorType,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionCommand,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeState,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel.gates import evaluate_decision
from wish_builder.services.execution_admission import (
    ExecutionAdmissionReason,
    admit_execution_snapshot,
)
from wish_builder.services.journal import GENESIS_HEAD


NOW = "2026-08-19T01:00:00Z"
WORKSPACE_HASH = "sha256:" + "b" * 64


def _event(
    events: list[JournalEvent],
    event_type: JournalEventType,
    payload,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: str = "bootstrap",
) -> JournalEvent:
    previous = GENESIS_HEAD.event_hash if not events else events[-1].event_hash
    event = JournalEvent.create(
        sequence=len(events) + 1,
        event_id=f"EVENT-ADMISSION-{len(events) + 1:04d}",
        event_type=event_type,
        identity=ExecutionIdentity("WISH-2026-001", 1),
        actor_type=actor_type,
        actor_id=actor_id,
        recorded_at=NOW,
        previous_event_hash=previous,
        payload=payload,
    )
    events.append(event)
    return event


def admitted_events(
    *,
    choice: DecisionChoice = DecisionChoice.APPROVE,
    include_import: bool = True,
):
    manifest = one_task_manifest()
    events: list[JournalEvent] = []
    if include_import:
        _event(
            events,
            JournalEventType.TRELLIS_GRAPH_IMPORTED,
            TransitionPayload(
                TransitionSubject.RUN,
                RuntimeState.TRELLIS_PREPARATION,
                RuntimeState.GATE_B_PENDING,
            ),
        )
    coordinator = ActorIdentity(
        ActorType.COORDINATOR,
        "coordinator-001",
        "host-001",
        1234,
        "process-start-coordinator",
    )
    request = DecisionRequest(
        CommandIdentity(
            1,
            "COMMAND-GATE-B-001",
            "REQUEST-GATE-B-001",
            CommandKind.DECIDE,
            len(events) + 1,
            "nonce-gate-b-001",
            coordinator,
            SourceChannel.COORDINATOR,
            NOW,
        ),
        DecisionType.GATE_B,
        manifest.canonical_sha256(),
        WORKSPACE_HASH,
        "local-account-001",
        (DecisionChoice.APPROVE, DecisionChoice.REJECT),
    )
    _event(
        events,
        JournalEventType.DECISION_REQUESTED,
        DecisionRequestPayload(request),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
    )
    command = DecisionCommand(
        "DECISION-GATE-B-001",
        request,
        choice,
        ActorIdentity(
            ActorType.HUMAN,
            "local-account-001",
            "host-001",
            5678,
            "process-start-human",
        ),
        SourceChannel.DIRECT_CLI,
        NOW,
    )
    evaluation = evaluate_decision(
        request,
        command,
        current_sequence=request.command.expected_sequence,
        current_workspace_hash=WORKSPACE_HASH,
    )
    assert evaluation.observation is not None
    from wish_builder.contracts import DecisionObservedPayload

    _event(
        events,
        JournalEventType.DECISION_OBSERVED,
        DecisionObservedPayload(evaluation.observation),
        actor_type=ActorType.HUMAN,
        actor_id="local-account-001",
    )
    _event(
        events,
        JournalEventType.TASK_GRAPH_FROZEN,
        TransitionPayload(
            TransitionSubject.RUN,
            RuntimeState.GATE_B_PENDING,
            RuntimeState.EXECUTING,
        ),
    )
    return manifest, events


class ExecutionSnapshotAdmissionTests(unittest.TestCase):
    def test_exact_gate_b_snapshot_is_admitted(self) -> None:
        manifest, events = admitted_events()

        result = admit_execution_snapshot(
            manifest,
            tuple(events),
            workspace_hash=WORKSPACE_HASH,
        )

        self.assertTrue(result.admitted)
        self.assertIs(result.reason, ExecutionAdmissionReason.NONE)
        self.assertEqual(2, result.request_event.sequence)
        self.assertEqual(3, result.decision_event.sequence)
        self.assertEqual(4, result.frozen_event.sequence)

    def test_empty_broken_or_wrong_run_journal_is_rejected(self) -> None:
        manifest, events = admitted_events()
        broken = (events[0], events[2], events[1], events[3])
        wrong_run_manifest = replace(manifest, run_id="WISH-OTHER-001")
        cases = (
            (manifest, (), ExecutionAdmissionReason.JOURNAL_EMPTY),
            (manifest, broken, ExecutionAdmissionReason.JOURNAL_CHAIN_INVALID),
            (
                wrong_run_manifest,
                tuple(events),
                ExecutionAdmissionReason.JOURNAL_RUN_MISMATCH,
            ),
        )
        for candidate_manifest, candidate_events, reason in cases:
            with self.subTest(reason=reason):
                self.assertIs(
                    admit_execution_snapshot(
                        candidate_manifest,
                        candidate_events,
                        workspace_hash=WORKSPACE_HASH,
                    ).reason,
                    reason,
                )

    def test_rejection_manifest_drift_and_workspace_drift_fail_closed(self) -> None:
        manifest, rejected = admitted_events(choice=DecisionChoice.REJECT)
        approved_manifest, approved = admitted_events()
        cases = (
            (
                manifest,
                tuple(rejected),
                WORKSPACE_HASH,
                ExecutionAdmissionReason.GATE_B_NOT_APPROVED,
            ),
            (
                replace(approved_manifest, goal="A different frozen goal"),
                tuple(approved),
                WORKSPACE_HASH,
                ExecutionAdmissionReason.MANIFEST_DIGEST_MISMATCH,
            ),
            (
                approved_manifest,
                tuple(approved),
                "sha256:" + "c" * 64,
                ExecutionAdmissionReason.WORKSPACE_DRIFT,
            ),
        )
        for candidate, events, workspace_hash, reason in cases:
            with self.subTest(reason=reason):
                result = admit_execution_snapshot(
                    candidate,
                    events,
                    workspace_hash=workspace_hash,
                )
                self.assertFalse(result.admitted)
                self.assertIs(result.reason, reason)

    def test_new_request_or_graph_import_invalidates_the_old_approval(self) -> None:
        manifest, events = admitted_events()
        request = events[1].payload.request
        pending = list(events)
        pending_event = JournalEvent.create(
            sequence=5,
            event_id="EVENT-ADMISSION-PENDING-0005",
            event_type=JournalEventType.DECISION_REQUESTED,
            identity=ExecutionIdentity(manifest.run_id, 1),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-001",
            recorded_at=NOW,
            previous_event_hash=pending[-1].event_hash,
            payload=DecisionRequestPayload(
                replace(
                    request,
                    command=replace(
                        request.command,
                        command_id="COMMAND-GATE-B-002",
                        request_id="REQUEST-GATE-B-002",
                        expected_sequence=5,
                        request_nonce="nonce-gate-b-002",
                    ),
                )
            ),
        )
        pending.append(pending_event)
        changed = list(events)
        _event(
            changed,
            JournalEventType.TRELLIS_GRAPH_IMPORTED,
            TransitionPayload(
                TransitionSubject.RUN,
                RuntimeState.TRELLIS_PREPARATION,
                RuntimeState.GATE_B_PENDING,
            ),
        )

        self.assertIs(
            admit_execution_snapshot(
                manifest,
                tuple(pending),
                workspace_hash=WORKSPACE_HASH,
            ).reason,
            ExecutionAdmissionReason.GATE_B_DECISION_PENDING,
        )
        self.assertIs(
            admit_execution_snapshot(
                manifest,
                tuple(changed),
                workspace_hash=WORKSPACE_HASH,
            ).reason,
            ExecutionAdmissionReason.TRELLIS_GRAPH_CHANGED,
        )

    def test_missing_import_or_freeze_is_rejected(self) -> None:
        manifest, events = admitted_events()
        missing_import_manifest, without_import = admitted_events(
            include_import=False
        )
        without_freeze = tuple(events[:-1])
        self.assertIs(
            admit_execution_snapshot(
                missing_import_manifest,
                tuple(without_import),
                workspace_hash=WORKSPACE_HASH,
            ).reason,
            ExecutionAdmissionReason.TRELLIS_GRAPH_IMPORT_MISSING,
        )
        self.assertIs(
            admit_execution_snapshot(
                manifest,
                without_freeze,
                workspace_hash=WORKSPACE_HASH,
            ).reason,
            ExecutionAdmissionReason.TASK_GRAPH_NOT_FROZEN,
        )


if __name__ == "__main__":
    unittest.main()
