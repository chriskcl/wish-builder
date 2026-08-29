from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.adapters.test_trellis_graph_snapshot import (
    _task_record as official_task_record,
    _task_spec as official_task_spec,
    _write_record as write_official_record,
)
from tests.e2e.support import E2EHarness
from wish_builder.adapters.git_identity import capture_workspace_identity
from wish_builder.adapters.trellis import (
    TrellisAuthoritativeProjectionProvider,
    TrellisCoreGraphPort,
    TrellisCoreProjectionPort,
    import_trellis_snapshot,
)
from wish_builder.contracts.runtime import JournalEventType
from wish_builder.services.ports import (
    TrellisProjection,
    TrellisProjectionApplyRequest,
    TrellisProjectionDisposition,
    TrellisProjectionReason,
)
from wish_builder.services.trellis_projection import (
    TrellisProjectionService,
    TrellisProjectionSyncStatus,
)
from wish_builder.services.ports import TrellisGraphSnapshot
from tests.adapters.test_trellis_graph_import import settings


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORK_ROOT = REPOSITORY_ROOT.parent / "work"
CORE_ROOT = WORK_ROOT / "tools" / "trellis-core-0.6.15" / "package"
CORE_ARCHIVE = (
    WORK_ROOT
    / "artifacts"
    / "trellis-0.6.15"
    / "mindfoldhq-trellis-core-0.6.15.tgz"
)
BRIDGE = (
    REPOSITORY_ROOT
    / "wish_builder"
    / "bridges"
    / "trellis_core"
    / "bridge.mjs"
)


class TrellisProjectionAdapterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        _git(self.repository, "init", "-b", "main")
        _git(self.repository, "config", "user.email", "wish-builder@example.invalid")
        _git(self.repository, "config", "user.name", "Wish Builder Test")
        self.task_file = (
            self.repository / ".trellis" / "tasks" / "08-19-task-a" / "task.json"
        )
        self.task_file.parent.mkdir(parents=True)
        self.task_file.write_text(
            json.dumps(_task_record(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (self.repository / "README.md").write_text(
            "# Projection fixture\n",
            encoding="utf-8",
        )
        _git(self.repository, "add", ".trellis/tasks/08-19-task-a/task.json")
        _git(self.repository, "add", "README.md")
        _git(self.repository, "commit", "-m", "seed Trellis task")
        self.source_bytes = self.task_file.read_bytes()
        self.workspace = capture_workspace_identity(
            self.repository, ("README.md",)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _runtime(self):
        node = shutil.which("node")
        if (
            node is None
            or not CORE_ROOT.is_dir()
            or not CORE_ARCHIVE.is_file()
            or not BRIDGE.is_file()
        ):
            self.skipTest("pinned local Trellis runtime is unavailable")
        port = TrellisCoreProjectionPort(
            bridge_command=(str(Path(node).resolve()), str(BRIDGE)),
            working_directory=self.repository,
            environment={
                "WISH_BUILDER_TRELLIS_CORE_ROOT": str(CORE_ROOT),
                "WISH_BUILDER_TRELLIS_CORE_ARCHIVE": str(CORE_ARCHIVE),
            },
        )
        return port

    def test_existing_projection_writer_lock_fails_closed_without_writing(self) -> None:
        port = self._runtime()
        target = TrellisAuthoritativeProjectionProvider(
            self.repository,
            self.workspace,
        ).ensure("WISH-2026-001")
        initial = port.inspect(target.path, "trellis/task-a")
        self.assertIsNotNone(initial.record_revision)
        before = self.task_file.read_bytes()
        lock = target.path / ".trellis" / ".wish-builder-projection-writer.lock"
        lock.write_text("another-writer", encoding="utf-8")

        observed = port.apply(
            TrellisProjectionApplyRequest(
                target.path,
                "trellis/task-a",
                initial.record_revision or "",
                _projection(sequence=10),
            )
        )

        self.assertIs(observed.disposition, TrellisProjectionDisposition.UNAVAILABLE)
        self.assertIs(observed.reason, TrellisProjectionReason.UNAVAILABLE)
        self.assertEqual(before, self.task_file.read_bytes())
        self.assertEqual("another-writer", lock.read_text(encoding="utf-8"))

    def test_authoritative_provider_revalidates_workspace_identity(self) -> None:
        provider = TrellisAuthoritativeProjectionProvider(
            self.repository,
            self.workspace,
        )
        target = provider.ensure("WISH-2026-001")
        self.assertEqual(self.repository.resolve(), target.path)
        self.assertEqual(
            self.workspace.local_repository_id,
            target.workspace.local_repository_id,
        )

        _git(self.repository, "checkout", "-b", "unapproved-target")
        with self.assertRaisesRegex(
            RuntimeError,
            "projection_authoritative_workspace_drift",
        ):
            provider.ensure("WISH-2026-001")

    def test_authoritative_projection_updates_task_and_replay_is_idempotent(
        self,
    ) -> None:
        port = self._runtime()
        provider = TrellisAuthoritativeProjectionProvider(
            self.repository,
            self.workspace,
        )
        target = provider.ensure("WISH-2026-001")
        initial = port.inspect(target.path, "trellis/task-a")
        projection = _projection(sequence=10)

        applied = port.apply(
            TrellisProjectionApplyRequest(
                target.path,
                "trellis/task-a",
                initial.record_revision or "",
                projection,
            )
        )

        self.assertIs(applied.disposition, TrellisProjectionDisposition.APPLIED)
        self.assertEqual("in_progress", applied.task_status)
        self.assertNotEqual(self.source_bytes, self.task_file.read_bytes())
        task_record = json.loads(self.task_file.read_text(encoding="utf-8"))
        self.assertEqual("in_progress", task_record["status"])
        self.assertEqual(
            "WISH-2026-001",
            task_record["meta"]["wish_builder_projection"]["runId"],
        )

        before_replay = self.task_file.read_bytes()
        replay_target = provider.ensure("WISH-2026-001")
        inspected = port.inspect(replay_target.path, "trellis/task-a")
        replayed = port.apply(
            TrellisProjectionApplyRequest(
                replay_target.path,
                "trellis/task-a",
                inspected.record_revision or "",
                projection,
            )
        )

        self.assertIs(
            replayed.disposition,
            TrellisProjectionDisposition.IDEMPOTENT,
        )
        self.assertEqual(projection, replayed.projection)
        self.assertEqual(before_replay, self.task_file.read_bytes())

    def test_authoritative_projection_revision_conflict_does_not_overwrite(
        self,
    ) -> None:
        port = self._runtime()
        provider = TrellisAuthoritativeProjectionProvider(
            self.repository,
            self.workspace,
        )
        target = provider.ensure("WISH-2026-001")
        initial = port.inspect(target.path, "trellis/task-a")
        record = json.loads(self.task_file.read_text(encoding="utf-8"))
        record["title"] = "Manual authoritative edit"
        self.task_file.write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        manual_bytes = self.task_file.read_bytes()

        observed = port.apply(
            TrellisProjectionApplyRequest(
                target.path,
                "trellis/task-a",
                initial.record_revision or "",
                _projection(sequence=10),
            )
        )

        self.assertIs(observed.disposition, TrellisProjectionDisposition.CONFLICT)
        self.assertIs(observed.reason, TrellisProjectionReason.REVISION_CONFLICT)
        self.assertEqual(manual_bytes, self.task_file.read_bytes())


class OfficialTrellisLifecycleIntegrationTests(unittest.TestCase):
    """Run the M1 lifecycle from official 0.6.15 records through recovery."""

    def setUp(self) -> None:
        self.node = shutil.which("node")
        if (
            self.node is None
            or not CORE_ROOT.is_dir()
            or not CORE_ARCHIVE.is_file()
            or not BRIDGE.is_file()
        ):
            self.skipTest("official Trellis 0.6.15 fixture is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.trellis_root = self.root / "trellis"
        self.trellis_root.mkdir()
        _git(self.trellis_root, "init", "-b", "main")
        _git(self.trellis_root, "config", "user.email", "wish-builder@example.invalid")
        _git(self.trellis_root, "config", "user.name", "Wish Builder Test")
        self.parent_id = "official/parent"
        child_id = "official/child"
        requirements = (
            {
                "id": "REQ-001",
                "text": "Run the official lifecycle",
                "status": "approved",
                "decision_ref": None,
            },
            {
                "id": "REQ-002",
                "text": "Recover and project the result",
                "status": "approved",
                "decision_ref": None,
            },
        )
        child_spec = official_task_spec("REQ-001")
        child_spec["requirement_ids"] = ["REQ-001", "REQ-002"]
        write_official_record(
            self.trellis_root,
            "08-21-parent",
            official_task_record(
                task_id=self.parent_id,
                title="Official lifecycle parent",
                children=(child_id,),
                meta={
                    "wish_builder": {
                        "schemaVersion": 1,
                        "requirements": list(requirements),
                    }
                },
            ),
        )
        write_official_record(
            self.trellis_root,
            "08-21-child",
            official_task_record(
                task_id=child_id,
                title="Official lifecycle task",
                parent=self.parent_id,
                meta={
                    "wish_builder": {
                        "schemaVersion": 1,
                        "task": child_spec,
                    }
                },
            ),
        )
        # Bind projection to a known Git checkout. The task record is the
        # only expected dirty path after the projection writer runs.
        (self.trellis_root / "README.md").write_text(
            "# Official Trellis projection fixture\n",
            encoding="utf-8",
        )
        _git(self.trellis_root, "add", ".")
        _git(self.trellis_root, "commit", "-m", "seed official Trellis tasks")
        self.workspace = capture_workspace_identity(
            self.trellis_root,
            ("README.md",),
        )
        self.environment = {
            "WISH_BUILDER_TRELLIS_CORE_ROOT": str(CORE_ROOT),
            "WISH_BUILDER_TRELLIS_CORE_ARCHIVE": str(CORE_ARCHIVE),
        }
        self.graph_port = TrellisCoreGraphPort(
            bridge_command=(str(Path(self.node).resolve()), str(BRIDGE.resolve())),
            checkout_root=self.trellis_root,
            working_directory=self.trellis_root,
            environment=self.environment,
            clock=lambda: "2026-08-21T01:00:00.000Z",
        )

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def test_official_records_run_through_graph_e2e_recovery_and_projection(self) -> None:
        snapshot = self.graph_port.export_snapshot(self.parent_id)
        imported = import_trellis_snapshot(snapshot, settings())
        self.assertIsInstance(snapshot, TrellisGraphSnapshot)
        self.assertEqual("0.6.15", snapshot.trellis_version)
        self.assertEqual(self.parent_id, imported.manifest.trellis_parent_task_id)
        self.assertEqual(
            {"official/child": "TASK-001"},
            imported.manifest.to_primitive()["task_id_mapping"],
        )

        harness = E2EHarness(self.root / "execution", imported_manifest=imported.manifest)
        try:
            projection_port = TrellisCoreProjectionPort(
                bridge_command=(
                    str(Path(self.node).resolve()),
                    str(BRIDGE.resolve()),
                ),
                working_directory=self.trellis_root,
                environment=self.environment,
            )
            checkout = TrellisAuthoritativeProjectionProvider(
                self.trellis_root,
                self.workspace,
            )
            service = TrellisProjectionService(
                imported.manifest,
                harness.journal,
                checkout,
                projection_port,
            )
            lock = self.trellis_root / ".trellis" / ".wish-builder-projection-writer.lock"
            lock.write_text("simulated interrupted writer", encoding="utf-8")

            cursor, task_id = _complete_imported_task(harness)
            self.assertEqual("TASK-001", task_id)

            recovered, recovered_cursor = harness.recover()
            self.assertEqual((), recovered.pending_dispatch_requests)
            self.assertEqual(cursor.snapshot, recovered_cursor.snapshot)
            events = harness.events()
            self.assertTrue(events)
            self.assertEqual(
                1,
                sum(
                    event.event_type is JournalEventType.DISPATCH_REQUESTED
                    for event in events
                ),
            )
            self.assertEqual(
                1,
                sum(
                    event.event_type is JournalEventType.DISPATCH_OBSERVED
                    for event in events
                ),
            )
            self.assertEqual(
                1,
                sum(
                    event.event_type is JournalEventType.PROMOTION_OBSERVED
                    for event in events
                ),
            )

            delayed = service.reconcile_verified_events(
                events,
                verified_head=recovered.replay.head,
            )
            self.assertTrue(delayed)
            self.assertTrue(
                all(item.status is TrellisProjectionSyncStatus.DELAYED for item in delayed)
            )
            lock.unlink()

            projected = service.reconcile_verified_events(
                events,
                verified_head=recovered.replay.head,
            )
            self.assertTrue(projected)
            self.assertTrue(
                all(item.status is TrellisProjectionSyncStatus.APPLIED for item in projected)
            )
            self.assertEqual(events, harness.events())
            self.assertEqual(
                1,
                sum(
                    event.event_type is JournalEventType.DISPATCH_REQUESTED
                    for event in harness.events()
                ),
            )
            record = json.loads(
                (
                    self.trellis_root
                    / ".trellis"
                    / "tasks"
                    / "08-21-child"
                    / "task.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual("completed", record["status"])
            self.assertEqual(
                imported.manifest.run_id,
                record["meta"]["wish_builder_projection"]["runId"],
            )
        finally:
            harness_root = harness.root
            if harness_root.exists():
                # E2EHarness owns no external resources beyond its temporary tree.
                import shutil as _shutil

                _shutil.rmtree(harness_root, ignore_errors=True)

def _complete_imported_task(harness: E2EHarness):
    reserved = harness.coordinator().reserve_ready(limit=1)
    if len(reserved.reserved) != 1:
        raise AssertionError(reserved)
    identity = reserved.reserved[0]
    prepared = harness.workflow(reserved.cursor).prepare_attempt(identity)
    if prepared.attempt is None:
        raise AssertionError(prepared)
    dispatched = harness.coordinator(prepared.cursor).dispatch_reserved(identity)
    relative_path = harness.result_path(identity.task_id, harness.manifest)
    marker = harness.root / "markers" / f"{identity.task_id}.json"
    outcome = harness.run_worker(
        prepared.attempt,
        relative_path,
        f"implemented {identity.task_id}",
        marker,
    )
    if outcome.status.value != "success":
        raise AssertionError(outcome)
    harness.commit_attempt(prepared.attempt, relative_path)
    from wish_builder.processes import WorkerResultProposal

    accepted = harness.coordinator(dispatched.cursor).accept_worker_result(
        WorkerResultProposal(identity, f"worker-{identity.task_id}", True)
    )
    staged = harness.workflow(accepted.cursor).stage_attempt_result(prepared.attempt)
    if staged.staged is None:
        raise AssertionError(staged)
    from tests.e2e.support import E2EAcceptance

    promoted = harness.workflow(staged.cursor).promote_staged(
        (staged.staged,),
        E2EAcceptance((staged.staged,)),
    )
    return promoted.cursor, identity.task_id

def _projection(*, sequence: int) -> TrellisProjection:
    return TrellisProjection(
        schema_version=1,
        operation_id=f"projection-{sequence}",
        run_id="WISH-2026-001",
        task_id="TASK-001",
        trellis_task_id="trellis/task-a",
        manifest_digest="sha256:" + "1" * 64,
        trellis_graph_digest="sha256:" + "a" * 64,
        canonical_sequence=sequence,
        canonical_event_hash="sha256:" + f"{sequence:x}".rjust(64, "0"),
        canonical_state="dispatched",
        target_status="in_progress",
        evidence_digests=("sha256:" + "2" * 64,),
        summary="Canonical task transition recorded.",
    )


def _task_record() -> dict[str, object]:
    return {
        "id": "trellis/task-a",
        "name": "task-a",
        "title": "Projection integration fixture",
        "description": "A tracked Trellis task used by the projection test.",
        "status": "planning",
        "dev_type": None,
        "scope": None,
        "package": None,
        "priority": "P1",
        "creator": "wish-builder-test",
        "assignee": "wish-builder-test",
        "createdAt": "2026-08-19",
        "completedAt": None,
        "branch": None,
        "base_branch": "main",
        "worktree_path": None,
        "commit": None,
        "pr_url": None,
        "subtasks": [],
        "children": [],
        "parent": None,
        "relatedFiles": [],
        "notes": "",
        "meta": {},
    }


def _git(repository: Path, *arguments: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)


if __name__ == "__main__":
    unittest.main()
