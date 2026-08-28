from __future__ import annotations

import dataclasses
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from wish_builder.adapters.storage import FilesystemJournalStorage
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
    SourceChannel,
)
from wish_builder.kernel.gates import evaluate_decision
from wish_builder.services.decisions import commit_decision
from wish_builder.services.journal import (
    GENESIS_HEAD,
    AppendStatus,
    DurableJournal,
    JournalHead,
)


RUN_ID = "WISH-001"
NOW = "2026-08-18T07:00:00Z"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def coordinator_actor() -> ActorIdentity:
    return ActorIdentity(
        ActorType.COORDINATOR,
        "coordinator-001",
        "host-001",
        4321,
        "process-start-coordinator",
    )


def human_actor() -> ActorIdentity:
    return ActorIdentity(
        ActorType.HUMAN,
        "local-account-001",
        "host-001",
        1234,
        "process-start-human",
    )


def decision_request() -> DecisionRequest:
    return DecisionRequest(
        CommandIdentity(
            1,
            "COMMAND-001",
            "REQUEST-001",
            CommandKind.DECIDE,
            1,
            "nonce-001",
            coordinator_actor(),
            SourceChannel.COORDINATOR,
            NOW,
        ),
        DecisionType.GATE_B,
        HASH_A,
        HASH_B,
        "local-account-001",
        (DecisionChoice.APPROVE, DecisionChoice.REJECT),
    )


def decision(choice: DecisionChoice, suffix: str) -> DecisionCommand:
    return DecisionCommand(
        f"DECISION-{suffix}",
        decision_request(),
        choice,
        human_actor(),
        SourceChannel.DIRECT_CLI,
        NOW,
    )


def seed_request(root: Path) -> JournalHead:
    request = decision_request()
    event = JournalEvent.create(
        sequence=1,
        event_id="EVENT-REQUEST-001",
        event_type=JournalEventType.DECISION_REQUESTED,
        identity=ExecutionIdentity(RUN_ID, 1),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at=NOW,
        previous_event_hash=GENESIS_HEAD.event_hash,
        payload=DecisionRequestPayload(request),
    )
    result = DurableJournal(
        RUN_ID,
        FilesystemJournalStorage(root, RUN_ID),
    ).append(event, expected_head=GENESIS_HEAD)
    if result.head is None:
        raise AssertionError(result)
    return result.head


def _commit_worker(
    root: str,
    command: DecisionCommand,
    expected_head: JournalHead,
    barrier: object,
    queue: object,
) -> None:
    evaluation = evaluate_decision(
        decision_request(),
        command,
        current_sequence=1,
        current_workspace_hash=HASH_B,
    )
    barrier.wait(timeout=10)  # type: ignore[attr-defined]
    result = commit_decision(
        evaluation,
        DurableJournal(
            RUN_ID,
            FilesystemJournalStorage(root, RUN_ID, lock_timeout_seconds=10),
        ),
        expected_head=expected_head,
        identity=ExecutionIdentity(RUN_ID, 1),
        event_id=f"EVENT-{command.decision_id}",
    )
    queue.put(  # type: ignore[attr-defined]
        (
            result.evaluation.accepted,
            result.evaluation.reason.value,
            None if result.append_result is None else result.append_result.status.value,
        )
    )


class DecisionPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "journal"
        self.expected_head = seed_request(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def race(self, commands: tuple[DecisionCommand, DecisionCommand]):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        queue = context.Queue()
        processes = [
            context.Process(
                target=_commit_worker,
                args=(
                    str(self.root),
                    command,
                    self.expected_head,
                    barrier,
                    queue,
                ),
            )
            for command in commands
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            self.assertFalse(process.is_alive())
            self.assertEqual(0, process.exitcode)
        return [queue.get(timeout=2), queue.get(timeout=2)]

    def test_conflicting_concurrent_decisions_have_one_durable_winner(self) -> None:
        results = self.race(
            (
                decision(DecisionChoice.APPROVE, "APPROVE"),
                decision(DecisionChoice.REJECT, "REJECT"),
            )
        )
        self.assertEqual([False, True], sorted(item[0] for item in results))
        self.assertEqual(
            [AppendStatus.COMMITTED.value, AppendStatus.CONFLICT.value],
            sorted(item[2] for item in results),
        )
        self.assertEqual(
            ["accepted", "decision_conflict"],
            sorted(item[1] for item in results),
        )
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(2, len(segment.read_bytes().splitlines()))

    def test_exact_concurrent_decision_is_committed_once_then_idempotent(self) -> None:
        command = decision(DecisionChoice.APPROVE, "APPROVE")
        results = self.race((command, command))
        self.assertTrue(all(item[0] for item in results))
        self.assertEqual(
            [AppendStatus.COMMITTED.value, AppendStatus.IDEMPOTENT.value],
            sorted(item[2] for item in results),
        )
        self.assertEqual(
            ["accepted", "idempotent_replay"],
            sorted(item[1] for item in results),
        )
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(2, len(segment.read_bytes().splitlines()))

    def test_rejected_evaluation_never_calls_journal(self) -> None:
        command = dataclasses.replace(
            decision(DecisionChoice.APPROVE, "DENIED"),
            source_channel=SourceChannel.CODEX_RELAY,
        )
        evaluation = evaluate_decision(
            decision_request(),
            command,
            current_sequence=1,
            current_workspace_hash=HASH_B,
        )
        result = commit_decision(
            evaluation,
            DurableJournal(RUN_ID, FilesystemJournalStorage(self.root, RUN_ID)),
            expected_head=self.expected_head,
            identity=ExecutionIdentity(RUN_ID, 1),
            event_id="EVENT-DENIED",
        )
        self.assertFalse(result.evaluation.accepted)
        self.assertIsNone(result.append_result)
        segment = self.root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(1, len(segment.read_bytes().splitlines()))


if __name__ == "__main__":
    unittest.main()
