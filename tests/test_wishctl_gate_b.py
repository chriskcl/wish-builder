from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.e2e.support import git, initialize_repository
from tests.test_wishctl import (
    import_digest,
    invoke_cli,
    valid_trellis_snapshot,
    write_import_inputs,
)
from wish_builder.adapters.trellis import import_trellis_snapshot
from wish_builder.cli import wishctl
from wish_builder.processes.production import ProductionRuntimeLayout


class WishCtlGateBAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.runtime_root = self.root / "runtime"
        initialize_repository(self.repository)

        self.snapshot_path, self.settings_path = write_import_inputs(self.root)
        settings_value = json.loads(self.settings_path.read_text(encoding="utf-8"))
        settings_value["protected_paths"].append(".trellis/tasks/**")
        self.settings_path.write_text(
            json.dumps(settings_value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
            newline="\n",
        )
        snapshot, settings = wishctl._load_trellis_import(
            self.snapshot_path,
            self.settings_path,
        )
        self.manifest = import_trellis_snapshot(snapshot, settings).manifest
        self.manifest_path = self.root / "execution-manifest.json"
        self.manifest_path.write_bytes(self.manifest.canonical_json_bytes())
        self.manifest_bytes = self.manifest_path.read_bytes()

        artifact_bytes = b"# Approved Gate B\n"
        self.artifact_hash = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
        self.artifact_path = (
            self.root
            / f"gate-b-{self.artifact_hash.removeprefix('sha256:')}.md"
        )
        self.artifact_path.write_bytes(artifact_bytes)
        live_value = valid_trellis_snapshot()
        live_value["revision"] = import_digest("0")
        live_bytes = json.dumps(
            live_value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.live_snapshot = replace(
            snapshot,
            revision=import_digest("0"),
            snapshot_bytes=live_bytes,
            source_sha256="sha256:" + hashlib.sha256(live_bytes).hexdigest(),
        )

    def _arguments(self) -> list[str]:
        return [
            "admit-gate-b",
            str(self.manifest_path),
            str(self.artifact_path),
            str(self.settings_path),
            "--approved-artifact-hash",
            self.artifact_hash,
            "--runtime-root",
            str(self.runtime_root),
            "--workspace-root",
            str(self.repository),
            "--actor-id",
            "local-account-001",
            "--coordinator-id",
            "coordinator-001",
            "--host-id",
            "host-001",
        ]

    def _admit(self, snapshot=None):
        port = mock.Mock()
        port.export_snapshot.return_value = snapshot or self.live_snapshot
        with (
            mock.patch.object(wishctl, "_trellis_graph_port", return_value=port),
            mock.patch.object(
                wishctl,
                "_utc_now",
                return_value="2026-08-31T01:00:00Z",
            ),
            mock.patch.object(wishctl.time, "time_ns", return_value=123456789),
        ):
            result = invoke_cli(self._arguments())
        return result

    def _layout(self) -> ProductionRuntimeLayout:
        return ProductionRuntimeLayout.for_run(
            self.repository,
            self.runtime_root,
            self.manifest.run_id,
        )

    def _validate_execution(self):
        return invoke_cli(
            [
                "validate",
                str(self.manifest_path),
                "--stage",
                "execution",
                "--journal-root",
                str(self._layout().journal_root),
                "--workspace-root",
                str(self.repository),
            ]
        )

    def _seed_trellis_task(self) -> Path:
        task_file = self.repository / ".trellis" / "tasks" / "task-a" / "task.json"
        task_file.parent.mkdir(parents=True)
        task_file.write_text('{"status":"planning"}\n', encoding="utf-8")
        git(self.repository, "add", ".trellis/tasks/task-a/task.json")
        git(self.repository, "commit", "-m", "add Trellis task")
        return task_file

    def test_revision_only_drift_is_admitted_and_execution_validates(self) -> None:
        code, stdout, stderr = self._admit()

        self.assertEqual((0, ""), (code, stderr))
        summary = json.loads(stdout)
        self.assertEqual(8, summary["appended_count"])
        self.assertEqual(self.live_snapshot.revision, summary["trellis_revision"])
        self.assertEqual(self.manifest_bytes, self.manifest_path.read_bytes())
        self.assertEqual(
            {
                "approved_by": None,
                "approved_at": None,
                "artifact_hash": None,
            },
            self.manifest.to_primitive()["approved"]["gate_b"],
        )

        layout = self._layout()
        validate_code, validate_stdout, validate_stderr = invoke_cli(
            [
                "validate",
                str(self.manifest_path),
                "--stage",
                "execution",
                "--journal-root",
                str(layout.journal_root),
                "--workspace-root",
                str(self.repository),
            ]
        )
        self.assertEqual((0, ""), (validate_code, validate_stderr))
        self.assertIn("OK: manifest valid", validate_stdout)

        replay_code, replay_stdout, replay_stderr = self._admit()
        self.assertEqual((0, ""), (replay_code, replay_stderr))
        replay = json.loads(replay_stdout)
        self.assertEqual(0, replay["appended_count"])
        self.assertEqual("already_admitted", replay["reason"])
        self.assertEqual(self.manifest_bytes, self.manifest_path.read_bytes())

    def test_artifact_manifest_and_material_graph_drift_fail_closed(self) -> None:
        self.artifact_path.write_bytes(b"changed artifact\n")
        artifact_result = self._admit()
        self.assertEqual(2, artifact_result[0])
        self.assertIn("artifact hash does not match", artifact_result[2])

        self.artifact_path.write_bytes(b"# Approved Gate B\n")
        self.manifest_path.write_bytes(self.manifest_bytes + b" ")
        manifest_result = self._admit()
        self.assertEqual(2, manifest_result[0])
        self.assertIn("not the canonical immutable snapshot", manifest_result[2])

        self.manifest_path.write_bytes(self.manifest_bytes)
        changed = valid_trellis_snapshot()
        changed["revision"] = self.live_snapshot.revision
        changed["tasks"][0]["title"] = "Materially changed task"
        changed_bytes = json.dumps(
            changed,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        graph_snapshot = replace(
            self.live_snapshot,
            snapshot_bytes=changed_bytes,
            source_sha256=(
                "sha256:" + hashlib.sha256(changed_bytes).hexdigest()
            ),
        )
        graph_result = self._admit(graph_snapshot)
        self.assertEqual(2, graph_result[0])
        self.assertIn("live material graph changed", graph_result[2])
        self.assertFalse(self._layout().journal_root.exists())

    def test_execution_validation_requires_and_verifies_runtime_roots(self) -> None:
        code, stdout, stderr = invoke_cli(
            ["validate", str(self.manifest_path), "--stage", "execution"]
        )
        self.assertEqual((1, ""), (code, stderr))
        self.assertIn("--journal-root and --workspace-root", stdout)

        admitted_code, _, admitted_stderr = self._admit()
        self.assertEqual((0, ""), (admitted_code, admitted_stderr))
        layout = self._layout()
        segment = next((layout.journal_root / "segments").glob("segment-*.jsonl"))
        segment.write_bytes(segment.read_bytes() + b"{}\n")

        corrupt_code, corrupt_stdout, corrupt_stderr = invoke_cli(
            [
                "validate",
                str(self.manifest_path),
                "--stage",
                "execution",
                "--journal-root",
                str(layout.journal_root),
                "--workspace-root",
                str(self.repository),
            ]
        )
        self.assertEqual((1, ""), (corrupt_code, corrupt_stderr))
        self.assertIn("runtime evidence could not be verified", corrupt_stdout)

    def test_execution_validation_only_normalizes_unstaged_task_projection(self) -> None:
        task_file = self._seed_trellis_task()
        admitted_code, _, admitted_stderr = self._admit()
        self.assertEqual((0, ""), (admitted_code, admitted_stderr))

        task_file.write_text('{"status":"in_progress"}\n', encoding="utf-8")
        projected_code, projected_stdout, projected_stderr = self._validate_execution()
        self.assertEqual((0, ""), (projected_code, projected_stderr))
        self.assertIn("OK: manifest valid", projected_stdout)

        git(self.repository, "add", ".trellis/tasks/task-a/task.json")
        staged_code, staged_stdout, staged_stderr = self._validate_execution()
        self.assertEqual((1, ""), (staged_code, staged_stderr))
        self.assertIn("workspace_drift", staged_stdout)
        git(self.repository, "restore", "--staged", ".trellis/tasks/task-a/task.json")

        source_file = self.repository / "src" / "req-001" / "unexpected.kt"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("source drift\n", encoding="utf-8")
        source_code, source_stdout, source_stderr = self._validate_execution()
        self.assertEqual((1, ""), (source_code, source_stderr))
        self.assertIn("projection workspace could not be verified", source_stdout)
        source_file.unlink()

        git(self.repository, "checkout", "-b", "unapproved-target")
        branch_code, branch_stdout, branch_stderr = self._validate_execution()
        self.assertEqual((1, ""), (branch_code, branch_stderr))
        self.assertIn("workspace_drift", branch_stdout)
        git(self.repository, "checkout", "main")

        readme = self.repository / "README.md"
        readme.write_text("# Advanced target\n", encoding="utf-8")
        git(self.repository, "add", "README.md")
        git(self.repository, "commit", "-m", "advance target")
        head_code, head_stdout, head_stderr = self._validate_execution()
        self.assertEqual((1, ""), (head_code, head_stderr))
        self.assertIn("workspace_drift", head_stdout)

        malformed_provider = mock.Mock()
        malformed_provider.ensure.return_value = object()
        with mock.patch.object(
            wishctl,
            "TrellisAuthoritativeProjectionProvider",
            return_value=malformed_provider,
        ):
            malformed_code, malformed_stdout, malformed_stderr = (
                self._validate_execution()
            )
        self.assertEqual((1, ""), (malformed_code, malformed_stderr))
        self.assertIn("projection workspace could not be verified", malformed_stdout)


if __name__ == "__main__":
    unittest.main()
