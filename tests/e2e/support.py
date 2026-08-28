from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.adapters.test_trellis_graph_import import (
    payload as trellis_payload,
)
from tests.adapters.test_trellis_graph_import import settings, snapshot, task
from wish_builder.adapters.fake import FakeTaskPort
from wish_builder.adapters.git_identity import capture_workspace_identity
from wish_builder.adapters.git_worktree import (
    AttemptWorktree,
    GitWorktreeAdapter,
    StagedResult,
)
from wish_builder.adapters.process_identity import capture_process_start_id
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.adapters.trellis import import_trellis_snapshot
from wish_builder.cli import wishctl
from wish_builder.contracts import canonical_json_bytes, decode_journal_event_bytes
from wish_builder.contracts.runtime import (
    ActorIdentity,
    ActorType,
    CommandIdentity,
    CommandKind,
    DecisionChoice,
    DecisionRequest,
    DecisionRequestPayload,
    DecisionType,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseDraftPayload,
    LeaseOwner,
    RuntimeState,
    SchedulerMode,
    SourceChannel,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.contracts.manifest_v2 import ExecutionManifestV2
from wish_builder.kernel import KernelSnapshot, TaskDag, apply_journal_event
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.processes import (
    AcceptanceResult,
    CoordinatorCursor,
    EnvironmentVariable,
    ForegroundCoordinator,
    LocalExecutionWorkflow,
    ProcessOutcome,
    ProcessRequest,
    ProcessRunner,
)
from wish_builder.processes.runner import capture_executable_identity
from wish_builder.services.journal import (
    AppendStatus,
    CoordinatorLeaseState,
    DurableJournal,
    JournalEventDraft,
)
from wish_builder.services.recovery import (
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)

BASE_TIME = datetime(2026, 8, 19, tzinfo=UTC)
COORDINATOR_ID = "coordinator-e2e-001"
HUMAN_ID = "local-account-e2e"


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


def serial_parallel_manifest(
    *,
    acceptance_executable: str | os.PathLike[str] | None = None,
):
    value = trellis_payload()
    value["requirements"] = [
        {
            "id": f"REQ-{ordinal:03d}",
            "text": text,
            "status": "approved",
            "decision_ref": None,
        }
        for ordinal, text in enumerate(
            (
                "Foundation is frozen",
                "Left module works",
                "Right module works",
                "Integrated product works",
            ),
            start=1,
        )
    ]
    value["tasks"] = [
        task("trellis/foundation", "REQ-001", wave=0),
        task(
            "trellis/left",
            "REQ-002",
            depends_on=["trellis/foundation"],
            wave=1,
        ),
        task(
            "trellis/right",
            "REQ-003",
            depends_on=["trellis/foundation"],
            wave=1,
        ),
        task(
            "trellis/integration",
            "REQ-004",
            depends_on=["trellis/left", "trellis/right"],
            wave=2,
        ),
    ]
    if acceptance_executable is not None:
        executable = capture_executable_identity(acceptance_executable)
        task_ids = {
            "trellis/foundation": "task_001",
            "trellis/integration": "task_002",
            "trellis/left": "task_003",
            "trellis/right": "task_004",
        }
        for task_value in value["tasks"]:
            source_id = task_value["id"]
            task_value["regression_commands"] = [
                {
                    "executable_profile": "python",
                    "executable_identity_digest": f"sha256:{executable.sha256}",
                    "argv": [
                        "python",
                        "-m",
                        "unittest",
                        "-q",
                        f"project_tests.test_{task_ids[source_id]}",
                    ],
                    "working_directory": ".",
                    "timeout_seconds": 10,
                    "stdout_limit_bytes": 65_536,
                    "stderr_limit_bytes": 65_536,
                    "result_limit_bytes": 65_536,
                    "environment_allowlist": [],
                    "network_policy": "denied",
                    "display_text": f"Run acceptance for {source_id}",
                }
            ]
    return import_trellis_snapshot(snapshot(value), settings()).manifest


def initialize_repository(path: Path, *, include_project_tests: bool = False) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Wish Builder E2E")
    git(path, "config", "user.email", "e2e@wish-builder.invalid")
    git(path, "config", "core.autocrlf", "false")
    readme = path / "README.md"
    readme.write_text("# Active M1 fixture\n", encoding="utf-8", newline="\n")
    if include_project_tests:
        project_tests = path / "project_tests"
        project_tests.mkdir()
        (project_tests / "__init__.py").write_text("", encoding="utf-8")
        cases = {
            "task_001": ("src/req-001/result.txt", "implemented TASK-001\n"),
            "task_002": ("src/req-004/result.txt", "implemented TASK-002\n"),
            "task_003": ("src/req-002/result.txt", "implemented TASK-003\n"),
            "task_004": ("src/req-003/result.txt", "implemented TASK-004\n"),
        }
        for module, (relative_path, expected) in cases.items():
            source = (
                "from pathlib import Path\n"
                "import unittest\n\n"
                "class CandidateResultTests(unittest.TestCase):\n"
                "    def test_promoted_result_is_visible(self):\n"
                f"        result = Path({relative_path!r})\n"
                "        self.assertTrue(result.is_file())\n"
                f"        self.assertEqual({expected!r}, result.read_text(encoding='utf-8'))\n"
            )
            (project_tests / f"test_{module}.py").write_text(
                source,
                encoding="utf-8",
                newline="\n",
            )
    git(path, "add", ".")
    git(path, "commit", "-m", "baseline")


def _fold_event(snapshot_value, event):
    applied = apply_journal_event(snapshot_value, event)
    if applied.accepted:
        return applied.snapshot
    if applied.reason.value == "unsupported_event":
        return replace(
            snapshot_value,
            last_sequence=event.sequence,
            last_event_id=event.event_id,
            last_event_hash=event.event_hash,
        )
    raise AssertionError(applied.reason)


def _last_event(journal_root: Path):
    segment = max((journal_root / "segments").glob("segment-*.jsonl"))
    raw = segment.read_bytes().splitlines(keepends=True)[-1]
    decoded = decode_journal_event_bytes(raw)
    if not decoded.ok or decoded.value is None:
        raise AssertionError(decoded.report.render_text())
    return decoded.value


@dataclass
class E2EHarness:
    root: Path
    acceptance_executable: str | os.PathLike[str] | None = None
    imported_manifest: ExecutionManifestV2 | None = None

    def __post_init__(self) -> None:
        self._clock_tick = -1
        if self.imported_manifest is not None and type(
            self.imported_manifest
        ) is not ExecutionManifestV2:
            raise TypeError("imported_manifest must be an ExecutionManifestV2")
        self.manifest = self.imported_manifest or serial_parallel_manifest(
            acceptance_executable=self.acceptance_executable
        )
        self.repository = self.root / "repository"
        self.attempts_root = self.root / "attempts"
        self.control_root = self.root / "control"
        self.journal_root = self.control_root / "journal"
        initialize_repository(
            self.repository,
            include_project_tests=self.acceptance_executable is not None,
        )
        self.attempts_root.mkdir()
        self.control_root.mkdir()
        self.workspace_scopes = tuple(
            sorted(
                {
                    *self.manifest.protected_paths,
                    *(
                        path
                        for manifest_task in self.manifest.tasks
                        for path in (
                            *manifest_task.owned_paths,
                            *manifest_task.allowed_auxiliary_paths,
                        )
                    ),
                }
            )
        )
        workspace = capture_workspace_identity(
            self.repository,
            self.workspace_scopes,
        )
        self.repository_adapter = GitWorktreeAdapter(
            self.repository,
            self.attempts_root,
            workspace,
            clock=lambda: "2026-08-19T00:00:30Z",
        )
        self.owner = LeaseOwner(
            ActorIdentity(
                ActorType.COORDINATOR,
                COORDINATOR_ID,
                "host-e2e",
                os.getpid(),
                capture_process_start_id(),
            ),
            workspace.local_repository_id,
            workspace.local_worktree_id,
            workspace.workspace_hash,
            digest("9"),
        )
        self.storage = FilesystemJournalStorage(
            self.journal_root,
            self.manifest.run_id,
            authority_clock=self._next_time,
        )
        self.journal = DurableJournal(self.manifest.run_id, self.storage)
        self.snapshot = KernelSnapshot.initial(
            self.manifest.run_id,
            1,
            TaskDag.compile(self.manifest),
        )
        self.lease_state = CoordinatorLeaseState.initial()
        self.decision_summaries: list[dict[str, object]] = []
        self._bootstrap(workspace.workspace_hash)
        self.port = FakeTaskPort(
            self.control_root / "effects",
            clock=lambda: "2026-08-19T00:00:30Z",
        )
        graph = GraphIndex.compile(self.manifest, self.snapshot)
        self.cursor = CoordinatorCursor(self.snapshot, graph, self.lease_state)

    def _next_time(self) -> datetime:
        self._clock_tick += 1
        return BASE_TIME + timedelta(seconds=self._clock_tick)

    def _next_timestamp(self) -> str:
        return self._next_time().isoformat(timespec="seconds").replace("+00:00", "Z")

    def _append_transition(
        self,
        event_type: JournalEventType,
        from_state: RuntimeState,
        to_state: RuntimeState,
    ) -> None:
        sequence = self.lease_state.head.sequence + 1
        appended = self.journal.append_draft(
            JournalEventDraft(
                f"EVENT-BOOTSTRAP-{sequence:04d}",
                event_type,
                ExecutionIdentity(self.manifest.run_id, 1),
                ActorType.SYSTEM,
                "e2e-bootstrap",
                TransitionPayload(
                    TransitionSubject.RUN,
                    from_state,
                    to_state,
                ),
            ),
            expected_head=self.lease_state.head,
        )
        if appended.status is not AppendStatus.COMMITTED or appended.event is None:
            raise AssertionError(appended)
        self.snapshot = _fold_event(self.snapshot, appended.event)
        self.lease_state = self.lease_state.advance(appended.event)

    def _approve_gate(
        self,
        decision_type: DecisionType,
        candidate_hash: str,
        workspace_hash: str,
    ) -> None:
        label = decision_type.value.replace("_", "-").upper()
        sequence = self.lease_state.head.sequence + 1
        request = DecisionRequest(
            CommandIdentity(
                1,
                f"COMMAND-{label}-001",
                f"REQUEST-{label}-001",
                CommandKind.DECIDE,
                sequence,
                f"nonce-{decision_type.value}-001",
                self.owner.actor,
                SourceChannel.COORDINATOR,
                "2026-08-19T00:00:00Z",
            ),
            decision_type,
            candidate_hash,
            workspace_hash,
            HUMAN_ID,
            (DecisionChoice.APPROVE, DecisionChoice.REJECT),
        )
        event = JournalEvent.create(
            sequence=sequence,
            event_id=f"EVENT-{label}-REQUEST-001",
            event_type=JournalEventType.DECISION_REQUESTED,
            identity=ExecutionIdentity(self.manifest.run_id, 1),
            actor_type=ActorType.COORDINATOR,
            actor_id=COORDINATOR_ID,
            recorded_at=self._next_timestamp(),
            previous_event_hash=self.lease_state.head.event_hash,
            payload=DecisionRequestPayload(request),
        )
        appended = self.journal.append(event, expected_head=self.lease_state.head)
        if not appended.durable:
            raise AssertionError(appended)
        self.snapshot = _fold_event(self.snapshot, event)
        self.lease_state = self.lease_state.advance(event)

        request_path = self.control_root / f"{decision_type.value}-request.json"
        request_path.write_bytes(canonical_json_bytes(request.to_primitive()))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                wishctl,
                "_utc_now",
                return_value=self._next_timestamp(),
            ),
            mock.patch.object(wishctl.time, "time_ns", return_value=123456789),
        ):
            arguments = [
                "decide",
                str(request_path),
                "--journal-root",
                str(self.journal_root),
                "--workspace-hash",
                workspace_hash,
                "--workspace-root",
                str(self.repository),
            ]
            for scope in self.workspace_scopes:
                arguments.extend(("--workspace-scope", scope))
            arguments.extend(
                (
                    "--choice",
                    "approve",
                    "--actor-id",
                    HUMAN_ID,
                    "--host-id",
                    "host-e2e",
                )
            )
            code = wishctl.main(arguments)
        if code != 0 or stderr.getvalue():
            raise AssertionError((code, stdout.getvalue(), stderr.getvalue()))
        self.decision_summaries.append(json.loads(stdout.getvalue()))
        decision_event = _last_event(self.journal_root)
        self.snapshot = _fold_event(self.snapshot, decision_event)
        self.lease_state = self.lease_state.advance(decision_event)

    def _bootstrap(self, workspace_hash: str) -> None:
        for event_type, from_state, to_state in (
            (
                JournalEventType.RUN_INITIALIZED,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
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
        ):
            self._append_transition(event_type, from_state, to_state)
        self._approve_gate(
            DecisionType.GATE_A,
            self.manifest.approvals.gate_a.artifact_hash,
            workspace_hash,
        )
        self._append_transition(
            JournalEventType.GATE_APPROVED,
            RuntimeState.GATE_A_PENDING,
            RuntimeState.TRELLIS_PREPARATION,
        )
        self._append_transition(
            JournalEventType.TRELLIS_GRAPH_IMPORTED,
            RuntimeState.TRELLIS_PREPARATION,
            RuntimeState.GATE_B_PENDING,
        )
        self._approve_gate(
            DecisionType.GATE_B,
            self.manifest.canonical_sha256(),
            workspace_hash,
        )
        self._append_transition(
            JournalEventType.TASK_GRAPH_FROZEN,
            RuntimeState.GATE_B_PENDING,
            RuntimeState.EXECUTING,
        )
        lease = self.journal.append_draft(
            JournalEventDraft(
                "EVENT-LEASE-ACQUIRED-E2E-001",
                JournalEventType.LEASE_ACQUIRED,
                ExecutionIdentity(self.manifest.run_id, 1),
                ActorType.COORDINATOR,
                COORDINATOR_ID,
                LeaseDraftPayload(
                    "LEASE-E2E-001",
                    COORDINATOR_ID,
                    self.owner,
                    SchedulerMode.WISH_BUILDER,
                    1,
                    self.manifest.canonical_sha256(),
                    300,
                    10,
                ),
            ),
            expected_head=self.lease_state.head,
            lease_state=self.lease_state,
        )
        if lease.status is not AppendStatus.COMMITTED or lease.event is None:
            raise AssertionError(lease)
        self.snapshot = _fold_event(self.snapshot, lease.event)
        self.lease_state = self.lease_state.advance(lease.event)

    def coordinator(self, cursor: CoordinatorCursor | None = None):
        return ForegroundCoordinator(
            self.manifest,
            cursor or self.cursor,
            self.journal,
            self.port,
            coordinator_id=COORDINATOR_ID,
            owner=self.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=30),
        )

    def workflow(self, cursor: CoordinatorCursor) -> LocalExecutionWorkflow:
        return LocalExecutionWorkflow(
            self.manifest,
            cursor,
            self.journal,
            self.repository_adapter,
            coordinator_id=COORDINATOR_ID,
            owner=self.owner,
            fencing_token=1,
            authority_clock=lambda: BASE_TIME + timedelta(seconds=30),
        )

    def recover(self):
        recovered = recover_coordinator_lease(
            self.journal_root,
            self.manifest,
            coordinator_epoch=1,
            repair_derived=False,
        )
        if recovered.status is not LeaseRecoveryStatus.RECOVERED:
            raise AssertionError(recovered)
        assert recovered.lease_state is not None
        return recovered, CoordinatorCursor(
            recovered.replay.snapshot,
            recovered.replay.graph_index,
            recovered.lease_state,
        )

    def events(self) -> tuple[JournalEvent, ...]:
        events: list[JournalEvent] = []
        for segment in sorted((self.journal_root / "segments").glob("segment-*.jsonl")):
            for raw in segment.read_bytes().splitlines(keepends=True):
                decoded = decode_journal_event_bytes(raw)
                if not decoded.ok or decoded.value is None:
                    raise AssertionError(decoded.report.render_text())
                events.append(decoded.value)
        return tuple(events)

    @staticmethod
    def result_path(task_id: str, manifest) -> str:
        task_value = next(task for task in manifest.tasks if task.id == task_id)
        root = task_value.owned_paths[0].removesuffix("/**")
        return f"{root}/result.txt"

    @staticmethod
    def commit_attempt(attempt: AttemptWorktree, relative_path: str) -> None:
        attempt_path = Path(attempt.path)
        git(attempt_path, "add", "--", relative_path)
        git(attempt_path, "commit", "-m", f"implement {attempt.task_id}")
        if git(attempt_path, "status", "--porcelain=v1", "-z"):
            raise AssertionError("attempt worktree is dirty after commit")

    def run_worker(
        self,
        attempt: AttemptWorktree,
        relative_path: str,
        content: str,
        marker: Path,
        *,
        delay: float = 0.0,
    ) -> ProcessOutcome:
        repository_root = Path(__file__).resolve().parents[2]
        environment = [EnvironmentVariable("PYTHONPATH", str(repository_root))]
        if os.name == "nt" and "SYSTEMROOT" in os.environ:
            environment.append(
                EnvironmentVariable("SYSTEMROOT", os.environ["SYSTEMROOT"])
            )
        names = tuple(item.name for item in environment)
        request = ProcessRequest.create(
            executable=sys.executable,
            arguments=(
                "-m",
                "tests.e2e.worker_fixture",
                relative_path,
                content,
                str(marker),
                "--delay",
                str(delay),
            ),
            cwd=attempt.path,
            environment=tuple(environment),
            timeout_seconds=10,
        )
        return ProcessRunner(environment_allowlist=names).run(request)


class E2EAcceptance:
    def __init__(self, sources: tuple[StagedResult, ...]) -> None:
        self._sources = {source.task_id: source for source in sources}

    def verify(self, task_value, repository: Path, promotion) -> AcceptanceResult:
        source = self._sources[task_value.id]
        if len(source.manifest.changed_paths) != 1:
            raise AssertionError("E2E fixture expects exactly one changed path")
        relative_path = source.manifest.changed_paths[0].path
        result = repository / relative_path
        expected = f"implemented {task_value.id}\n"
        if (
            task_value.id != promotion.task_id
            or not result.is_file()
            or result.read_text(encoding="utf-8") != expected
        ):
            raise AssertionError("promotion candidate is not visible to acceptance")
        raw = f"{task_value.id}:{promotion.promoted_commit_sha}".encode("ascii")
        evidence_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        return AcceptanceResult(
            True,
            (
                EvidenceRef(
                    1,
                    evidence_hash,
                    len(raw),
                    EvidenceType.RESULT,
                    EvidenceProducer(
                        source.manifest.identity,
                        external_object_id="local-e2e-acceptance",
                    ),
                    "2026-08-19T00:00:40Z",
                    EvidenceSensitivity.INTERNAL,
                    EvidenceRenderPolicy.METADATA_ONLY,
                    EvidenceRole.REQUIRED,
                    evidence_hash,
                ),
            ),
        )
