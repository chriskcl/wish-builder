from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.processes.test_coordinator import BASE_TIME, COORDINATOR_ID, one_task_manifest
from tests.processes.test_dispatch_recovery import recovery_proof
from wish_builder.adapters.fake import FakeTaskPort
from wish_builder.adapters.git_identity import (
    capture_filesystem_identity,
    capture_workspace_identity,
)
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.cli import wishctl
from wish_builder.contracts import (
    ActorIdentity,
    ActorType,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseDraftPayload,
    LeaseOwner,
    RuntimeReasonCode,
    RuntimeState,
    SchedulerMode,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
    canonical_json_bytes,
)
from wish_builder.kernel import KernelSnapshot, TaskDag, apply_journal_event
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.processes import CoordinatorCursor, ForegroundCoordinator
from wish_builder.services.journal import (
    AppendStatus,
    CoordinatorLeaseState,
    DurableJournal,
    JournalEventDraft,
)


def digest(character: str) -> str:
    return "sha256:" + character * 64


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
    return completed.stdout.decode("utf-8", errors="strict").strip()


def initialize_repository(repository: Path) -> None:
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Wish Builder Tests")
    git(repository, "config", "user.email", "tests@wish-builder.invalid")
    git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("baseline\n", encoding="utf-8", newline="\n")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "baseline")


def invoke_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = wishctl.main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class DecisionFixture:
    def __init__(self, root: Path, *, workspace_hash: str) -> None:
        self.journal_root = root / "control" / "journal"
        self.request_path = root / "decision-request.json"
        request = DecisionRequest(
            CommandIdentity(
                1,
                "COMMAND-GATE-B-LIVE",
                "REQUEST-GATE-B-LIVE",
                CommandKind.DECIDE,
                1,
                "nonce-gate-b-live",
                ActorIdentity(
                    ActorType.COORDINATOR,
                    "coordinator-live",
                    "host-live",
                    4321,
                    "process-start-live",
                ),
                SourceChannel.COORDINATOR,
                "2026-08-19T01:00:00Z",
            ),
            DecisionType.GATE_B,
            digest("a"),
            workspace_hash,
            "local-account-live",
            (DecisionChoice.APPROVE, DecisionChoice.REJECT),
        )
        self.request = request
        self.request_path.write_bytes(canonical_json_bytes(request.to_primitive()))
        event = JournalEvent.create(
            sequence=1,
            event_id="EVENT-DECISION-REQUEST-LIVE",
            event_type=JournalEventType.DECISION_REQUESTED,
            identity=ExecutionIdentity("WISH-LIVE-001", 1),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-live",
            recorded_at="2026-08-19T01:00:00Z",
            previous_event_hash=wishctl.GENESIS_HEAD.event_hash,
            payload=DecisionRequestPayload(request),
        )
        appended = DurableJournal(
            "WISH-LIVE-001",
            FilesystemJournalStorage(self.journal_root, "WISH-LIVE-001"),
        ).append(event, expected_head=wishctl.GENESIS_HEAD)
        if not appended.durable:
            raise AssertionError(appended)

    def arguments(self, repository: Path, workspace_hash: str) -> list[str]:
        return [
            "decide",
            str(self.request_path),
            "--journal-root",
            str(self.journal_root),
            "--workspace-root",
            str(repository),
            "--workspace-scope",
            "README.md",
            "--workspace-hash",
            workspace_hash,
            "--choice",
            "approve",
            "--actor-id",
            self.request.expected_actor_id,
            "--host-id",
            "host-live",
        ]


class LiveResumeHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repository = root / "repository"
        self.control_root = root / "control"
        self.journal_root = self.control_root / "journal"
        self.effects_root = root / "effects"
        initialize_repository(self.repository)
        self.control_root.mkdir()
        self.manifest = one_task_manifest()
        workspace = capture_workspace_identity(
            self.repository,
            wishctl._manifest_workspace_scopes(self.manifest),
        )
        control = capture_filesystem_identity(self.control_root)
        self.owner = LeaseOwner(
            ActorIdentity(
                ActorType.COORDINATOR,
                COORDINATOR_ID,
                "host-live",
                os.getpid(),
                "process-start-live",
            ),
            workspace.local_repository_id,
            workspace.local_worktree_id,
            workspace.workspace_hash,
            control.identity_hash,
        )
        self.storage = FilesystemJournalStorage(
            self.journal_root,
            self.manifest.run_id,
            authority_clock=lambda: BASE_TIME,
        )
        self.journal = DurableJournal(self.manifest.run_id, self.storage)
        snapshot = KernelSnapshot.initial(
            self.manifest.run_id,
            1,
            TaskDag.compile(self.manifest),
        )
        lease_state = CoordinatorLeaseState.initial()
        for event_type, from_state, to_state in (
            (JournalEventType.RUN_INITIALIZED, RuntimeState.NONE, RuntimeState.PREFLIGHT),
            (
                JournalEventType.PREFLIGHT_COMPLETED,
                RuntimeState.PREFLIGHT,
                RuntimeState.DISCOVERY,
            ),
            (
                JournalEventType.DISCOVERY_COMPLETED,
                RuntimeState.DISCOVERY,
                RuntimeState.GATE_A_PENDING,
            ),
            (
                JournalEventType.GATE_APPROVED,
                RuntimeState.GATE_A_PENDING,
                RuntimeState.TRELLIS_PREPARATION,
            ),
            (
                JournalEventType.TRELLIS_GRAPH_IMPORTED,
                RuntimeState.TRELLIS_PREPARATION,
                RuntimeState.GATE_B_PENDING,
            ),
            (
                JournalEventType.TASK_GRAPH_FROZEN,
                RuntimeState.GATE_B_PENDING,
                RuntimeState.EXECUTING,
            ),
        ):
            sequence = lease_state.head.sequence + 1
            appended = self.journal.append_draft(
                JournalEventDraft(
                    f"EVENT-LIVE-SEED-{sequence:04d}",
                    event_type,
                    ExecutionIdentity(self.manifest.run_id, 1),
                    ActorType.SYSTEM,
                    "live-bootstrap",
                    TransitionPayload(TransitionSubject.RUN, from_state, to_state),
                ),
                expected_head=lease_state.head,
            )
            if appended.status is not AppendStatus.COMMITTED or appended.event is None:
                raise AssertionError(appended)
            applied = apply_journal_event(snapshot, appended.event)
            if not applied.accepted:
                raise AssertionError(applied.reason)
            snapshot = applied.snapshot
            lease_state = lease_state.advance(appended.event)

        lease = self.journal.append_draft(
            JournalEventDraft(
                "EVENT-LIVE-LEASE-ACQUIRED-0001",
                JournalEventType.LEASE_ACQUIRED,
                ExecutionIdentity(self.manifest.run_id, 1),
                ActorType.COORDINATOR,
                COORDINATOR_ID,
                LeaseDraftPayload(
                    "LEASE-LIVE-001",
                    COORDINATOR_ID,
                    self.owner,
                    SchedulerMode.WISH_BUILDER,
                    1,
                    self.manifest.canonical_sha256(),
                    300,
                    10,
                ),
            ),
            expected_head=lease_state.head,
            lease_state=lease_state,
        )
        if lease.status is not AppendStatus.COMMITTED or lease.event is None:
            raise AssertionError(lease)
        applied = apply_journal_event(snapshot, lease.event)
        if not applied.accepted:
            raise AssertionError(applied.reason)
        snapshot = applied.snapshot
        lease_state = lease_state.advance(lease.event)
        graph = GraphIndex.compile(self.manifest, snapshot)
        self.port = FakeTaskPort(
            self.effects_root,
            clock=lambda: "2026-08-19T00:00:10Z",
            failpoint=lambda point, path: (_ for _ in ()).throw(OSError("blocked"))
            if point == "before_effect"
            else None,
        )
        self.coordinator = ForegroundCoordinator(
            self.manifest,
            CoordinatorCursor(snapshot, graph, lease_state),
            self.journal,
            self.port,
            coordinator_id=COORDINATOR_ID,
            owner=self.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
        )

    def take_over(self) -> ForegroundCoordinator:
        cursor = self.coordinator.cursor
        lease = cursor.lease_state.lease
        assert lease is not None
        lost = self.journal.append_draft(
            JournalEventDraft(
                "EVENT-LIVE-LEASE-LOST-0001",
                JournalEventType.LEASE_LOST,
                ExecutionIdentity(self.manifest.run_id, 1),
                ActorType.SYSTEM,
                "recovery",
                LeaseDraftPayload(
                    lease.lease_id,
                    lease.coordinator_id,
                    lease.owner,
                    lease.scheduler_mode,
                    lease.fencing_token,
                    lease.manifest_digest,
                    lease.lease_ttl_seconds,
                    lease.lease_clock_skew_seconds,
                ),
                RuntimeReasonCode.LEASE_LOST,
            ),
            expected_head=cursor.head,
            lease_state=cursor.lease_state,
        )
        if lost.status is not AppendStatus.COMMITTED or lost.event is None:
            raise AssertionError(lost)
        applied = apply_journal_event(cursor.snapshot, lost.event)
        if not applied.accepted:
            raise AssertionError(applied.reason)
        lease_state = cursor.lease_state.advance(lost.event)
        graph = cursor.graph_index.advance(cursor.snapshot, applied.snapshot)
        new_owner = replace(
            self.owner,
            actor=replace(
                self.owner.actor,
                actor_id="coordinator-live-002",
                process_id=os.getpid(),
                process_start_id="process-start-live-002",
            ),
        )
        acquired = self.journal.append_draft(
            JournalEventDraft(
                "EVENT-LIVE-LEASE-ACQUIRED-0002",
                JournalEventType.LEASE_ACQUIRED,
                ExecutionIdentity(self.manifest.run_id, 2),
                ActorType.COORDINATOR,
                "coordinator-live-002",
                LeaseDraftPayload(
                    "LEASE-LIVE-002",
                    "coordinator-live-002",
                    new_owner,
                    SchedulerMode.WISH_BUILDER,
                    2,
                    self.manifest.canonical_sha256(),
                    300,
                    10,
                ),
            ),
            expected_head=lease_state.head,
            lease_state=lease_state,
        )
        if acquired.status is not AppendStatus.COMMITTED or acquired.event is None:
            raise AssertionError(acquired)
        acquired_state = apply_journal_event(applied.snapshot, acquired.event)
        if not acquired_state.accepted:
            raise AssertionError(acquired_state.reason)
        lease_state = lease_state.advance(acquired.event)
        graph = graph.advance(applied.snapshot, acquired_state.snapshot)
        return ForegroundCoordinator(
            self.manifest,
            CoordinatorCursor(acquired_state.snapshot, graph, lease_state),
            self.journal,
            self.port,
            coordinator_id="coordinator-live-002",
            owner=new_owner,
            fencing_token=2,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=10),
        )


class WishCtlLiveIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_decide_rejects_request_and_argument_hash_spoof(self) -> None:
        repository = self.root / "repo"
        initialize_repository(repository)
        fake_hash = digest("f")
        fixture = DecisionFixture(self.root, workspace_hash=fake_hash)

        code, stdout, stderr = invoke_cli(fixture.arguments(repository, fake_hash))

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("workspace_drift", stderr)
        segment = fixture.journal_root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(1, len(segment.read_bytes().splitlines()))

    def test_decide_rejects_live_workspace_drift_without_append(self) -> None:
        repository = self.root / "repo"
        initialize_repository(repository)
        workspace = capture_workspace_identity(repository, ("README.md",))
        fixture = DecisionFixture(self.root, workspace_hash=workspace.workspace_hash)
        (repository / "README.md").write_text(
            "drifted\n",
            encoding="utf-8",
            newline="\n",
        )

        code, stdout, stderr = invoke_cli(
            fixture.arguments(repository, workspace.workspace_hash)
        )

        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertIn("workspace_drift", stderr)
        segment = fixture.journal_root / "segments" / "segment-00000001.jsonl"
        self.assertEqual(1, len(segment.read_bytes().splitlines()))

    def test_resume_rejects_replaced_control_root_before_append(self) -> None:
        harness = LiveResumeHarness(self.root)
        blocked = harness.coordinator.dispatch_ready()
        request = next(
            event
            for event in blocked.events
            if event.event_type is JournalEventType.DISPATCH_REQUESTED
        )
        active = harness.take_over()
        proof = recovery_proof(active, request)
        manifest_path = self.root / "execution-manifest.json"
        proof_path = self.root / "dispatch-recovery.json"
        manifest_path.write_bytes(harness.manifest.canonical_json_bytes())
        proof_path.write_bytes(proof.canonical_json_bytes())
        original_control = self.root / "control-original"
        shutil.move(str(harness.control_root), str(original_control))
        harness.control_root.mkdir()
        shutil.copytree(original_control / "journal", harness.journal_root)

        with mock.patch.object(
            wishctl,
            "_authority_now",
            return_value=BASE_TIME + timedelta(seconds=10),
        ):
            code, stdout, stderr = invoke_cli(
                [
                    "resume",
                    str(manifest_path),
                    str(proof_path),
                    "--journal-root",
                    str(harness.journal_root),
                    "--workspace-root",
                    str(harness.repository),
                ]
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("control_root_drift", stderr)
        events = wishctl._read_verified_journal(harness.journal_root)
        self.assertEqual(active.cursor.head.sequence, events[-1].sequence)

    def test_resume_rejects_live_workspace_drift_before_append(self) -> None:
        harness = LiveResumeHarness(self.root)
        blocked = harness.coordinator.dispatch_ready()
        request = next(
            event
            for event in blocked.events
            if event.event_type is JournalEventType.DISPATCH_REQUESTED
        )
        active = harness.take_over()
        proof = recovery_proof(active, request)
        manifest_path = self.root / "execution-manifest.json"
        proof_path = self.root / "dispatch-recovery.json"
        manifest_path.write_bytes(harness.manifest.canonical_json_bytes())
        proof_path.write_bytes(proof.canonical_json_bytes())
        drifted = harness.repository / "src" / "req-001" / "drift.txt"
        drifted.parent.mkdir(parents=True)
        drifted.write_text("drifted\n", encoding="utf-8", newline="\n")

        with mock.patch.object(
            wishctl,
            "_authority_now",
            return_value=BASE_TIME + timedelta(seconds=10),
        ):
            code, stdout, stderr = invoke_cli(
                [
                    "resume",
                    str(manifest_path),
                    str(proof_path),
                    "--journal-root",
                    str(harness.journal_root),
                    "--workspace-root",
                    str(harness.repository),
                ]
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("workspace_drift", stderr)
        events = wishctl._read_verified_journal(harness.journal_root)
        self.assertEqual(active.cursor.head.sequence, events[-1].sequence)


if __name__ == "__main__":
    unittest.main()
