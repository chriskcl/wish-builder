from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.processes.test_coordinator import one_task_manifest
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    ActorIdentity,
    ActorType,
    ExecutionIdentity,
    JournalEventType,
    RuntimeReasonCode,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.services.execution_admission import admit_execution_snapshot
from wish_builder.services.gate_b_bootstrap import (
    GateBBootstrapMaterial,
    GateBBootstrapReason,
    bootstrap_gate_b,
    gate_b_artifact_hash_from_nonce,
    gate_b_artifact_nonce,
    graph_projection_bytes,
)
from wish_builder.services.journal import (
    AppendStatus,
    DurableJournal,
    GENESIS_HEAD,
    JournalEventDraft,
    JournalHead,
)


NOW = "2026-08-31T04:00:00Z"
WORKSPACE_HASH = "sha256:" + "b" * 64
ARTIFACT_HASH = "sha256:" + "e" * 64
SOURCE_HASH = "sha256:" + "f" * 64


def material(
    manifest=None,
    *,
    workspace_hash: str = WORKSPACE_HASH,
    artifact_hash: str = ARTIFACT_HASH,
):
    selected = manifest or one_task_manifest()
    return GateBBootstrapMaterial(
        manifest=selected,
        workspace_hash=workspace_hash,
        gate_b_artifact_hash=artifact_hash,
        gate_b_artifact_byte_length=2048,
        trellis_snapshot_hash=SOURCE_HASH,
        trellis_snapshot_byte_length=4096,
        trellis_observed_at=NOW,
        coordinator=ActorIdentity(
            ActorType.COORDINATOR,
            "coordinator-001",
            "host-001",
            4100,
            "process-start-coordinator",
        ),
        approver=ActorIdentity(
            ActorType.HUMAN,
            "local-account-001",
            "host-001",
            4200,
            "process-start-human",
        ),
        requested_at=NOW,
        decided_at=NOW,
    )


def journal(root: Path, run_id: str) -> DurableJournal:
    return DurableJournal(
        run_id,
        FilesystemJournalStorage(root / "journal", run_id),
    )


class GateBBootstrapTests(unittest.TestCase):
    def test_artifact_nonce_and_graph_projection_are_content_bound(self) -> None:
        manifest = one_task_manifest()
        nonce = gate_b_artifact_nonce(ARTIFACT_HASH)

        self.assertEqual(ARTIFACT_HASH, gate_b_artifact_hash_from_nonce(nonce))
        self.assertIsNone(gate_b_artifact_hash_from_nonce("nonce-gate-b-001"))
        self.assertEqual(
            manifest.trellis_graph_digest,
            "sha256:" + hashlib.sha256(graph_projection_bytes(manifest)).hexdigest(),
        )

    def test_bootstrap_is_admitted_and_an_exact_replay_is_idempotent(self) -> None:
        selected = material()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = bootstrap_gate_b(
                selected,
                (),
                journal(root, selected.manifest.run_id),
            )
            second = bootstrap_gate_b(
                selected,
                first.events,
                journal(root, selected.manifest.run_id),
            )

        self.assertTrue(first.admitted)
        self.assertIs(first.reason, GateBBootstrapReason.NONE)
        self.assertEqual((8, 8), (len(first.events), first.appended_count))
        self.assertTrue(second.admitted)
        self.assertIs(second.reason, GateBBootstrapReason.ALREADY_ADMITTED)
        self.assertEqual(0, second.appended_count)
        self.assertTrue(
            admit_execution_snapshot(
                selected.manifest,
                first.events,
                workspace_hash=WORKSPACE_HASH,
            ).admitted
        )

    def test_admitted_replay_preserves_later_runtime_events(self) -> None:
        selected = material()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = journal(root, selected.manifest.run_id)
            first = bootstrap_gate_b(selected, (), target)
            head = JournalHead(first.events[-1].sequence, first.events[-1].event_hash)
            later = target.append_draft(
                JournalEventDraft(
                    "EVENT-RUN-PAUSED-00000009",
                    JournalEventType.RUN_PAUSED,
                    ExecutionIdentity(selected.manifest.run_id, 1),
                    ActorType.SYSTEM,
                    "test-runtime",
                    TransitionPayload(
                        TransitionSubject.RUN,
                        RuntimeState.EXECUTING,
                        RuntimeState.PAUSED,
                    ),
                    RuntimeReasonCode.PAUSE_REQUESTED,
                ),
                expected_head=head,
            )
            self.assertIs(later.status, AppendStatus.COMMITTED)
            assert later.event is not None
            replay = bootstrap_gate_b(
                selected,
                (*first.events, later.event),
                target,
            )

        self.assertTrue(replay.admitted)
        self.assertIs(replay.reason, GateBBootstrapReason.ALREADY_ADMITTED)
        self.assertEqual(0, replay.appended_count)
        self.assertEqual(9, len(replay.events))

    def test_admitted_replay_rejects_a_different_gate_b_artifact(self) -> None:
        selected = material()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = bootstrap_gate_b(
                selected,
                (),
                journal(root, selected.manifest.run_id),
            )
            replay = bootstrap_gate_b(
                material(artifact_hash="sha256:" + "a" * 64),
                complete.events,
                journal(root, selected.manifest.run_id),
            )

        self.assertFalse(replay.admitted)
        self.assertIs(
            replay.reason,
            GateBBootstrapReason.JOURNAL_PREFIX_INVALID,
        )
        self.assertEqual(0, replay.appended_count)

    def test_every_crash_prefix_resumes_without_duplicate_events(self) -> None:
        selected = material()
        with tempfile.TemporaryDirectory() as template_directory:
            template = bootstrap_gate_b(
                selected,
                (),
                journal(Path(template_directory), selected.manifest.run_id),
            ).events

        for prefix_length in range(9):
            with self.subTest(prefix_length=prefix_length):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    target = journal(root, selected.manifest.run_id)
                    head = GENESIS_HEAD
                    for event in template[:prefix_length]:
                        appended = target.append(event, expected_head=head)
                        self.assertTrue(appended.durable)
                        head = JournalHead(event.sequence, event.event_hash)

                    result = bootstrap_gate_b(
                        selected,
                        template[:prefix_length],
                        target,
                    )

                self.assertTrue(result.admitted, result)
                self.assertEqual(8, len(result.events))
                self.assertEqual(8 - prefix_length, result.appended_count)

    def test_partial_journal_rejects_workspace_manifest_and_graph_drift(self) -> None:
        selected = material()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = bootstrap_gate_b(
                selected,
                (),
                journal(root, selected.manifest.run_id),
            ).events

        cases = (
            material(workspace_hash="sha256:" + "c" * 64),
            material(replace(selected.manifest, goal="Different approved goal")),
            material(
                replace(
                    selected.manifest,
                    trellis_graph_digest="sha256:" + "d" * 64,
                )
            ),
        )
        for drifted in cases:
            with self.subTest(drift=drifted):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    target = journal(root, selected.manifest.run_id)
                    head = GENESIS_HEAD
                    for event in complete[:6]:
                        appended = target.append(event, expected_head=head)
                        self.assertTrue(appended.durable)
                        head = JournalHead(event.sequence, event.event_hash)
                    result = bootstrap_gate_b(
                        drifted,
                        complete[:6],
                        target,
                    )
                self.assertFalse(result.admitted)
                self.assertIs(
                    result.reason,
                    GateBBootstrapReason.JOURNAL_PREFIX_INVALID,
                )

    def test_corrupt_prefix_is_rejected_before_append(self) -> None:
        selected = material()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = bootstrap_gate_b(
                selected,
                (),
                journal(root, selected.manifest.run_id),
            ).events
        corrupt = list(complete[:4])
        object.__setattr__(corrupt[-1], "previous_event_hash", "sha256:" + "9" * 64)

        with tempfile.TemporaryDirectory() as temporary:
            result = bootstrap_gate_b(
                selected,
                tuple(corrupt),
                journal(Path(temporary), selected.manifest.run_id),
            )

        self.assertFalse(result.admitted)
        self.assertIs(result.reason, GateBBootstrapReason.JOURNAL_PREFIX_INVALID)
        self.assertEqual(0, result.appended_count)


if __name__ == "__main__":
    unittest.main()
