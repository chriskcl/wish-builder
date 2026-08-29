from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.adapters.test_trellis_graph_import import settings
from wish_builder.adapters.trellis import (
    SUPPORTED_TRELLIS_EXPORT_VERSION,
    TrellisCoreGraphPort,
    import_trellis_snapshot,
)


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
OBSERVED_AT = "2026-08-20T01:00:00.000Z"


class TrellisGraphSnapshotAdapterIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        node = shutil.which("node")
        if (
            node is None
            or not CORE_ROOT.is_dir()
            or not CORE_ARCHIVE.is_file()
            or not BRIDGE.is_file()
        ):
            self.skipTest("official Trellis 0.6.15 fixture is unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".git").mkdir()
        self.parent_id = "parent-wish"
        _write_record(
            self.root,
            "08-20-parent",
            _task_record(
                task_id=self.parent_id,
                title="Parent wish",
                children=("child-a", "child-b"),
                meta={
                    "wish_builder": {
                        "schemaVersion": 1,
                        "requirements": [
                            {
                                "id": "REQ-001",
                                "text": "Build the shared contract",
                                "status": "approved",
                                "decision_ref": None,
                            },
                            {
                                "id": "REQ-002",
                                "text": "Build the independent feature",
                                "status": "approved",
                                "decision_ref": None,
                            },
                        ],
                    }
                },
            ),
        )
        _write_record(
            self.root,
            "08-20-child-a",
            _task_record(
                task_id="child-a",
                title="Task child-a",
                parent=self.parent_id,
                meta={"wish_builder": {"schemaVersion": 1, "task": _task_spec("REQ-001")}},
            ),
        )
        _write_record(
            self.root,
            "08-20-child-b",
            _task_record(
                task_id="child-b",
                title="Task child-b",
                parent=self.parent_id,
                meta={
                    "wish_builder": {
                        "schemaVersion": 1,
                        "task": _task_spec("REQ-002", depends_on=("child-a",), wave=1),
                    }
                },
            ),
        )
        self.port = TrellisCoreGraphPort(
            bridge_command=(str(Path(node).resolve()), str(BRIDGE.resolve())),
            checkout_root=self.root,
            working_directory=self.root,
            environment={
                "WISH_BUILDER_TRELLIS_CORE_ROOT": str(CORE_ROOT),
                "WISH_BUILDER_TRELLIS_CORE_ARCHIVE": str(CORE_ARCHIVE),
            },
            clock=lambda: OBSERVED_AT,
        )

    def tearDown(self) -> None:
        if hasattr(self, "temporary"):
            self.temporary.cleanup()

    def test_official_records_flow_through_port_and_manifest_import(self) -> None:
        first = self.port.export_snapshot(self.parent_id)
        second = self.port.export_snapshot(self.parent_id)

        self.assertEqual(SUPPORTED_TRELLIS_EXPORT_VERSION, first.export_version)
        self.assertEqual("0.6.15", first.trellis_version)
        self.assertEqual(first, second)
        imported = import_trellis_snapshot(first, settings(imported_at=OBSERVED_AT))
        self.assertEqual(self.parent_id, imported.manifest.trellis_parent_task_id)
        self.assertEqual(first.revision, imported.manifest.trellis_revision)
        self.assertEqual(
            {"child-a": "TASK-001", "child-b": "TASK-002"},
            imported.manifest.to_primitive()["task_id_mapping"],
        )

    def test_incomplete_parent_membership_fails_closed(self) -> None:
        parent_file = self.root / ".trellis" / "tasks" / "08-20-parent" / "task.json"
        parent = json.loads(parent_file.read_text(encoding="utf-8"))
        parent["children"] = ["child-a"]
        parent_file.write_text(json.dumps(parent), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "graph_membership_incomplete"):
            self.port.export_snapshot(self.parent_id)


def _task_spec(
    requirement_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    wave: int = 0,
) -> dict[str, object]:
    digest = "sha256:" + "1" * 64
    return {
        "requirement_ids": [requirement_id],
        "depends_on": list(depends_on),
        "owned_paths": [f"src/{requirement_id.lower()}/**"],
        "allowed_auxiliary_paths": [f"tests/{requirement_id.lower()}/**"],
        "acceptance_criteria": [f"{requirement_id} passes acceptance"],
        "regression_commands": [
            {
                "executable_profile": "python",
                "executable_identity_digest": digest,
                "argv": ["python", "-m", "unittest", "discover"],
                "working_directory": ".",
                "timeout_seconds": 300,
                "stdout_limit_bytes": 1048576,
                "stderr_limit_bytes": 1048576,
                "result_limit_bytes": 262144,
                "environment_allowlist": [],
                "network_policy": "denied",
                "display_text": "Run repository tests",
            }
        ],
        "rollback": "Revert the task commit.",
        "documentation": [f"docs/{requirement_id.lower()}.md"],
        "wave": wave,
        "risk": "low",
        "may_change_contracts": wave == 0,
        "instruction_context_digest": None,
        "approved_document_digests": [],
        "task_packet_template_digest": "sha256:" + "5" * 64,
    }


def _task_record(
    *,
    task_id: str,
    title: str,
    parent: str | None = None,
    children: tuple[str, ...] = (),
    meta: dict[str, object],
) -> dict[str, object]:
    return {
        "id": task_id,
        "name": task_id,
        "title": title,
        "description": title,
        "status": "planning",
        "dev_type": None,
        "scope": None,
        "package": None,
        "priority": "P1",
        "creator": "architect",
        "assignee": "worker",
        "createdAt": "2026-08-20",
        "completedAt": None,
        "branch": None,
        "base_branch": "main",
        "worktree_path": None,
        "commit": None,
        "pr_url": None,
        "subtasks": [],
        "children": list(children),
        "parent": parent,
        "relatedFiles": [],
        "notes": "",
        "meta": meta,
    }


def _write_record(root: Path, directory: str, record: dict[str, object]) -> None:
    task_file = root / ".trellis" / "tasks" / directory / "task.json"
    task_file.parent.mkdir(parents=True)
    task_file.write_text(json.dumps(record), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
