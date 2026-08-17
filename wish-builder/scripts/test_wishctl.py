#!/usr/bin/env python3

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import wishctl


def valid_manifest():
    return {
        "schema_version": 1,
        "run_id": "WISH-001",
        "goal": "Ship an observable outcome",
        "base_branch": "main",
        "max_concurrency": 3,
        "protected_paths": ["src/contracts/**"],
        "approved": {
            "gate_a": {
                "approved_by": "architect",
                "approved_at": "2026-08-16T10:00:00Z",
                "artifact_hash": "sha256:" + "a" * 64,
            },
            "gate_b": {
                "approved_by": "architect",
                "approved_at": "2026-08-16T11:00:00Z",
                "artifact_hash": "sha256:" + "b" * 64,
            },
        },
        "requirements": [
            {"id": "REQ-001", "text": "Foundation exists", "status": "implemented"},
            {"id": "REQ-002", "text": "Feature A works", "status": "approved"},
            {"id": "REQ-003", "text": "Feature B works", "status": "approved"},
            {"id": "REQ-004", "text": "Product integrates", "status": "approved"},
        ],
        "tasks": [
            {
                "id": "TASK-001",
                "title": "Foundation",
                "requirement_ids": ["REQ-001"],
                "depends_on": [],
                "owned_paths": ["src/contracts/**"],
                "allowed_auxiliary_paths": [".trellis/tasks/foundation/**"],
                "acceptance_criteria": ["Contract passes"],
                "regression_commands": ["npm test -- contract"],
                "rollback": "Revert squash commit",
                "documentation": ["docs/contracts.md"],
                "wave": 0,
                "risk": "medium",
                "may_change_contracts": True,
                "issue_id": 1,
                "branch": "feat/1-foundation",
                "pr_id": 11,
                "squash_commit": "a1b2c3d",
                "agent_owner": None,
                "status": "merged",
            },
            {
                "id": "TASK-002",
                "title": "Feature A",
                "requirement_ids": ["REQ-002"],
                "depends_on": ["TASK-001"],
                "owned_paths": ["src/a/**"],
                "allowed_auxiliary_paths": [".trellis/tasks/feature-a/**"],
                "acceptance_criteria": ["Feature A passes"],
                "regression_commands": ["npm test -- feature-a"],
                "rollback": "Revert squash commit",
                "documentation": [],
                "wave": 1,
                "risk": "low",
                "may_change_contracts": False,
                "issue_id": 2,
                "branch": "feat/2-a",
                "pr_id": None,
                "squash_commit": None,
                "agent_owner": None,
                "status": "approved",
            },
            {
                "id": "TASK-003",
                "title": "Feature B",
                "requirement_ids": ["REQ-003"],
                "depends_on": ["TASK-001"],
                "owned_paths": ["src/b/**"],
                "allowed_auxiliary_paths": [".trellis/tasks/feature-b/**"],
                "acceptance_criteria": ["Feature B passes"],
                "regression_commands": ["npm test -- feature-b"],
                "rollback": "Revert squash commit",
                "documentation": [],
                "wave": 1,
                "risk": "low",
                "may_change_contracts": False,
                "issue_id": 3,
                "branch": "feat/3-b",
                "pr_id": None,
                "squash_commit": None,
                "agent_owner": None,
                "status": "approved",
            },
            {
                "id": "TASK-004",
                "title": "Integration",
                "requirement_ids": ["REQ-004"],
                "depends_on": ["TASK-002", "TASK-003"],
                "owned_paths": ["tests/e2e/**"],
                "allowed_auxiliary_paths": [".trellis/tasks/integration/**"],
                "acceptance_criteria": ["End-to-end passes"],
                "regression_commands": ["npm test -- e2e"],
                "rollback": "Revert release toggle",
                "documentation": ["docs/product.md"],
                "wave": 2,
                "risk": "medium",
                "may_change_contracts": False,
                "issue_id": 4,
                "branch": "feat/4-integration",
                "pr_id": None,
                "squash_commit": None,
                "agent_owner": None,
                "status": "approved",
            },
        ],
    }


class WishCtlTests(unittest.TestCase):
    def test_valid_execution_manifest(self):
        errors, warnings = wishctl.validate_manifest(valid_manifest(), "execution")
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

    def test_cycle_is_rejected(self):
        manifest = valid_manifest()
        manifest["tasks"][0]["depends_on"] = ["TASK-004"]
        errors, _ = wishctl.validate_manifest(manifest, "planning")
        self.assertTrue(any("dependency cycle" in error for error in errors))

    def test_parallel_path_overlap_is_rejected(self):
        manifest = valid_manifest()
        manifest["tasks"][2]["owned_paths"] = ["SRC/A/components/**"]
        errors, _ = wishctl.validate_manifest(manifest, "planning")
        self.assertTrue(any("parallel ownership overlap" in error for error in errors))

    def test_ready_returns_parallel_wave(self):
        result = wishctl.ready_tasks(valid_manifest())
        self.assertEqual(1, result["wave"])
        self.assertEqual(["TASK-002", "TASK-003"], result["task_ids"])

    def test_drift_rejects_outside_and_protected_paths(self):
        report = wishctl.drift_report(
            valid_manifest(), "TASK-002", ["src/a/view.ts", "src/contracts/api.ts"]
        )
        self.assertFalse(report["ok"])
        self.assertEqual(["src/contracts/api.ts"], report["outside_owned_paths"])
        self.assertEqual(["src/contracts/api.ts"], report["protected_path_changes"])

    def test_glob_does_not_over_allow_static_prefix(self):
        self.assertTrue(wishctl.path_matches("src/a/view.ts", "src/a/*.ts"))
        self.assertTrue(wishctl.path_matches("SRC/A/VIEW.TS", "src/a/*.ts"))
        self.assertFalse(wishctl.path_matches("src/a/view.css", "src/a/*.ts"))

    def test_drift_protected_paths_are_case_insensitive(self):
        report = wishctl.drift_report(
            valid_manifest(), "TASK-002", ["SRC/CONTRACTS/api.ts"]
        )
        self.assertFalse(report["ok"])
        self.assertEqual(["SRC/CONTRACTS/api.ts"], report["protected_path_changes"])

    def test_gate_requires_full_sha256(self):
        manifest = valid_manifest()
        manifest["approved"]["gate_a"]["artifact_hash"] = "sha256:short"
        errors, _ = wishctl.validate_manifest(manifest, "execution")
        self.assertIn("gate_a approval evidence is incomplete", errors)

    def test_planning_requires_gate_a_but_not_gate_b(self):
        manifest = valid_manifest()
        manifest["approved"]["gate_a"] = {}
        manifest["approved"]["gate_b"] = {}
        errors, _ = wishctl.validate_manifest(manifest, "planning")
        self.assertIn("gate_a approval evidence is incomplete", errors)
        self.assertNotIn("gate_b approval evidence is incomplete", errors)

    def test_duplicate_issue_is_rejected(self):
        manifest = valid_manifest()
        manifest["tasks"][2]["issue_id"] = manifest["tasks"][1]["issue_id"]
        errors, _ = wishctl.validate_manifest(manifest, "execution")
        self.assertTrue(any("Issue 2 is shared" in error for error in errors))

    def test_trace_contains_requirement_and_test(self):
        output = wishctl.trace_markdown(valid_manifest())
        self.assertIn("REQ-002", output)
        self.assertIn("npm test -- feature-a", output)

    def test_finish_requires_implemented_requirements(self):
        manifest = valid_manifest()
        for task in manifest["tasks"]:
            task["status"] = "merged"
            task["pr_id"] = task["pr_id"] or 99
            task["squash_commit"] = task["squash_commit"] or "f00baa"
        errors, _ = wishctl.validate_manifest(manifest, "finish")
        self.assertTrue(any("remain unimplemented" in error for error in errors))

    def test_cli_validate_ready_and_trace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            trace_path = Path(temporary_directory) / "trace.md"
            manifest_path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(
                    0,
                    wishctl.main(
                        ["validate", str(manifest_path), "--stage", "execution"]
                    ),
                )
                self.assertEqual(0, wishctl.main(["ready", str(manifest_path)]))
                self.assertEqual(
                    0,
                    wishctl.main(
                        ["trace", str(manifest_path), "--output", str(trace_path)]
                    ),
                )
                self.assertEqual(0, wishctl.main(["hash", str(trace_path)]))
            self.assertIn("TASK-002", output.getvalue())
            self.assertIn("sha256:", output.getvalue())
            self.assertIn("REQ-002", trace_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
