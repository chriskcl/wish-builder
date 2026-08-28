#!/usr/bin/env python3

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wish_builder.adapters.git_identity import capture_workspace_identity
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
    SourceChannel,
    canonical_json_bytes,
    decode_manifest_v2_bytes,
)
from wish_builder.services.journal import GENESIS_HEAD, DurableJournal
from wish_builder.services.ports import TrellisGraphSnapshot


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


def import_digest(character):
    return "sha256:" + character * 64


def direct_decision_request(*, workspace_hash=None):
    return DecisionRequest(
        CommandIdentity(
            1,
            "COMMAND-GATE-B-001",
            "REQUEST-GATE-B-001",
            CommandKind.DECIDE,
            1,
            "nonce-gate-b-001",
            ActorIdentity(
                ActorType.COORDINATOR,
                "coordinator-001",
                "host-001",
                4321,
                "process-start-coordinator",
            ),
            SourceChannel.COORDINATOR,
            "2026-08-19T01:00:00Z",
        ),
        DecisionType.GATE_B,
        import_digest("a"),
        workspace_hash or import_digest("b"),
        "local-account-001",
        (DecisionChoice.APPROVE, DecisionChoice.REJECT),
    )


def write_decision_fixture(root):
    root = Path(root)
    repository = root / "repository"
    repository.mkdir()
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Wish Builder Tests"),
        ("config", "user.email", "tests@wish-builder.invalid"),
        ("config", "core.autocrlf", "false"),
    ):
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            capture_output=True,
            check=False,
            shell=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr.decode(errors="replace"))
    (repository / "README.md").write_text(
        "baseline\n",
        encoding="utf-8",
        newline="\n",
    )
    for arguments in (("add", "README.md"), ("commit", "-m", "baseline")):
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            capture_output=True,
            check=False,
            shell=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr.decode(errors="replace"))
    workspace = capture_workspace_identity(repository, ("README.md",))
    journal_root = root / "journal"
    request_path = root / "decision-request.json"
    request = direct_decision_request(workspace_hash=workspace.workspace_hash)
    request_path.write_bytes(canonical_json_bytes(request.to_primitive()))
    event = JournalEvent.create(
        sequence=1,
        event_id="EVENT-DECISION-REQUEST-001",
        event_type=JournalEventType.DECISION_REQUESTED,
        identity=ExecutionIdentity("WISH-CLI-001", 1),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at="2026-08-19T01:00:00Z",
        previous_event_hash=GENESIS_HEAD.event_hash,
        payload=DecisionRequestPayload(request),
    )
    appended = DurableJournal(
        "WISH-CLI-001",
        FilesystemJournalStorage(journal_root, "WISH-CLI-001"),
    ).append(event, expected_head=GENESIS_HEAD)
    if not appended.durable:
        raise AssertionError(appended)
    return request_path, journal_root, request, repository


def import_command():
    return {
        "executable_profile": "python",
        "executable_identity_digest": import_digest("1"),
        "argv": ["python", "-m", "unittest", "tests.adapters"],
        "working_directory": ".",
        "timeout_seconds": 120,
        "stdout_limit_bytes": 1_048_576,
        "stderr_limit_bytes": 1_048_576,
        "result_limit_bytes": 262_144,
        "environment_allowlist": ["PATH", "PYTHONUTF8"],
        "network_policy": "denied",
        "display_text": "Run adapter tests",
    }


def import_task(source_id, requirement_id, *, depends_on=None, wave=0):
    return {
        "id": source_id,
        "title": f"Implement {source_id}",
        "requirement_ids": [requirement_id],
        "depends_on": list(depends_on or []),
        "owned_paths": [f"src/{requirement_id.lower()}/**"],
        "allowed_auxiliary_paths": [f"tests/{requirement_id.lower()}/**"],
        "acceptance_criteria": [f"{requirement_id} is covered"],
        "regression_commands": [import_command()],
        "rollback": "Revert the squash commit",
        "documentation": [f"docs/{requirement_id.lower()}.md"],
        "wave": wave,
        "risk": "medium",
        "may_change_contracts": wave == 0,
        "instruction_context_digest": import_digest("2"),
        "approved_document_digests": [import_digest("3")],
        "task_packet_template_digest": None,
    }


def valid_trellis_snapshot():
    return {
        "schema_version": 1,
        "parent_task_id": "parent/wish-001",
        "revision": import_digest("f"),
        "requirements": [
            {
                "id": "REQ-001",
                "text": "Freeze the shared contract",
                "status": "approved",
                "decision_ref": None,
            },
            {
                "id": "REQ-002",
                "text": "Import Trellis tasks",
                "status": "approved",
                "decision_ref": None,
            },
        ],
        "tasks": [
            import_task("trellis/task-alpha", "REQ-001"),
            import_task(
                "trellis/task-zeta",
                "REQ-002",
                depends_on=["trellis/task-alpha"],
                wave=1,
            ),
        ],
    }


def valid_import_settings():
    return {
        "export_version": "wish-builder.trellis-graph.v1",
        "trellis_version": "0.6.15",
        "parent_task_id": "parent/wish-001",
        "revision": import_digest("f"),
        "observed_at": "2026-08-18T05:00:00Z",
        "run_id": "WISH-2026-001",
        "goal": "Deliver a Trellis-backed workflow",
        "base_branch": "main",
        "imported_at": "2026-08-18T05:00:00Z",
        "gate_a": {
            "approved_by": "architect",
            "approved_at": "2026-08-18T04:00:00Z",
            "artifact_hash": import_digest("a"),
        },
        "provider": "codex",
        "capability_digest": import_digest("b"),
        "launch_profile_digest": import_digest("c"),
        "policy_digest": import_digest("d"),
        "execution_budget": {
            "max_attempts_per_task": 2,
            "max_attempts_per_run": 4,
            "attempt_deadline_seconds": 1_800,
            "total_worker_seconds": 7_200,
            "max_output_bytes": 8_388_608,
            "max_retained_evidence_bytes": 16_777_216,
            "max_concurrent_workers": 2,
            "billing_posture": "preapproved",
        },
        "max_concurrency": 2,
        "lease_ttl_seconds": 90,
        "lease_clock_skew_seconds": 2,
        "path_case_mode": "insensitive",
        "protected_paths": ["db/schema/**", "src/contracts/**"],
    }


def write_import_inputs(directory):
    root = Path(directory)
    snapshot_path = root / "trellis-snapshot.json"
    settings_path = root / "import-settings.json"
    snapshot_path.write_bytes(
        json.dumps(
            valid_trellis_snapshot(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    settings_path.write_bytes(
        json.dumps(
            valid_import_settings(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return snapshot_path, settings_path


def invoke_cli(arguments):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = wishctl.main(arguments)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class WishCtlTests(unittest.TestCase):
    def test_snapshot_trellis_writes_derived_bytes_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            core_root = root / "core" / "package"
            core_root.mkdir(parents=True)
            archive = root / "mindfoldhq-trellis-core-0.6.15.tgz"
            archive.write_bytes(b"official archive fixture")
            node = root / "node.exe"
            node.write_bytes(b"node fixture")
            output = root / "graph.json"
            raw = b'{"schema_version":1}'
            snapshot = TrellisGraphSnapshot(
                export_version="wish-builder.trellis-graph.v1",
                trellis_version="0.6.15",
                parent_task_id="parent-wish",
                revision="sha256:" + "a" * 64,
                observed_at="2026-08-20T01:00:00.000Z",
                snapshot_bytes=raw,
                source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
                complete=True,
            )
            port = mock.Mock()
            port.export_snapshot.return_value = snapshot
            with mock.patch.object(
                wishctl,
                "TrellisCoreGraphPort",
                return_value=port,
            ) as constructor:
                code, stdout, stderr = invoke_cli(
                    [
                        "snapshot-trellis",
                        "parent-wish",
                        "--checkout-root",
                        str(root),
                        "--core-root",
                        str(core_root),
                        "--core-archive",
                        str(archive),
                        "--node",
                        str(node),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertEqual(raw, output.read_bytes())
            summary = json.loads(stdout)
            self.assertEqual("0.6.15", summary["trellis_version"])
            self.assertEqual(
                "wish-builder.trellis-graph.v1",
                summary["export_version"],
            )
            self.assertEqual(snapshot.source_sha256, summary["source_sha256"])
            port.export_snapshot.assert_called_once_with("parent-wish")
            self.assertEqual(
                str(core_root),
                constructor.call_args.kwargs["environment"][
                    "WISH_BUILDER_TRELLIS_CORE_ROOT"
                ],
            )

    def test_snapshot_trellis_requires_verified_core_archive(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            core_root = root / "core"
            core_root.mkdir()
            code, stdout, stderr = invoke_cli(
                [
                    "snapshot-trellis",
                    "parent-wish",
                    "--checkout-root",
                    str(root),
                    "--core-root",
                    str(core_root),
                ]
            )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("--core-archive", stderr)
        self.assertNotIn("Traceback", stderr)

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

    def test_cli_uses_strict_raw_decoder_and_never_tracebacks_on_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "manifest.json"
            raw = json.dumps(valid_manifest(), separators=(",", ":")).encode("utf-8")
            duplicate = raw.replace(
                b'{"schema_version":1,',
                b'{"schema_version":1,"schema_version":1,"unknown_root":true,',
                1,
            )
            manifest_path.write_bytes(duplicate)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(2, wishctl.main(["validate", str(manifest_path)]))
            self.assertIn("json.duplicate_key", stderr.getvalue())

            manifest_path.write_bytes(b"{\"schema_version\":1,\"goal\":\xff}")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(2, wishctl.main(["validate", str(manifest_path)]))
            self.assertIn("json.invalid_utf8", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_import_trellis_stdout_is_canonical_and_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            arguments = [
                "import-trellis",
                str(snapshot_path),
                str(settings_path),
            ]

            first_code, first_stdout, first_stderr = invoke_cli(arguments)
            second_code, second_stdout, second_stderr = invoke_cli(arguments)

            self.assertEqual(0, first_code)
            self.assertEqual(0, second_code)
            self.assertEqual(first_stdout, second_stdout)
            self.assertEqual(first_stderr, second_stderr)
            decoded = decode_manifest_v2_bytes(first_stdout.encode("utf-8"))
            self.assertTrue(decoded.ok, decoded.report.render_text())
            self.assertIsNotNone(decoded.value)
            self.assertEqual(
                decoded.value.canonical_json_bytes(),
                first_stdout.encode("utf-8"),
            )
            summary = json.loads(first_stderr)
            self.assertEqual(
                decoded.value.trellis_graph_digest,
                summary["trellis_graph_digest"],
            )
            self.assertEqual(
                decoded.value.canonical_sha256(),
                summary["manifest_digest"],
            )
            self.assertFalse(summary["gate_b_invalidated"])

    def test_import_trellis_atomic_output_reports_digests(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            output_path = Path(temporary_directory) / "execution-manifest.json"

            code, stdout, stderr = invoke_cli(
                [
                    "import-trellis",
                    str(snapshot_path),
                    str(settings_path),
                    "--output",
                    str(output_path),
                ]
            )

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            summary = json.loads(stdout)
            decoded = decode_manifest_v2_bytes(output_path.read_bytes())
            self.assertTrue(decoded.ok, decoded.report.render_text())
            self.assertEqual(str(output_path), summary["output"])
            self.assertEqual(
                decoded.value.canonical_sha256(),
                summary["manifest_digest"],
            )
            self.assertEqual(
                decoded.value.trellis_graph_digest,
                summary["trellis_graph_digest"],
            )
            self.assertEqual([], list(output_path.parent.glob(".*.tmp")))

    def test_import_trellis_refuses_existing_output_without_force(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            output_path = Path(temporary_directory) / "execution-manifest.json"
            original = b"existing manifest\n"
            output_path.write_bytes(original)

            code, stdout, stderr = invoke_cli(
                [
                    "import-trellis",
                    str(snapshot_path),
                    str(settings_path),
                    "--output",
                    str(output_path),
                ]
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertIn("output exists", stderr)
            self.assertIn("--force", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(original, output_path.read_bytes())

    def test_import_trellis_force_replaces_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            output_path = Path(temporary_directory) / "execution-manifest.json"
            output_path.write_bytes(b"existing manifest\n")

            code, stdout, stderr = invoke_cli(
                [
                    "import-trellis",
                    str(snapshot_path),
                    str(settings_path),
                    "--output",
                    str(output_path),
                    "--force",
                ]
            )

            self.assertEqual(0, code)
            self.assertEqual("", stderr)
            self.assertEqual(str(output_path), json.loads(stdout)["output"])
            self.assertTrue(decode_manifest_v2_bytes(output_path.read_bytes()).ok)

    def test_import_trellis_rejects_hostile_settings_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            valid_raw = settings_path.read_bytes()
            unknown = valid_import_settings()
            unknown["unknown_field"] = True
            cases = {
                "duplicate": valid_raw.replace(
                    b"{",
                    b'{"run_id":"duplicate",',
                    1,
                ),
                "invalid_utf8": b'{"run_id":"\xff"}',
                "unknown": json.dumps(
                    unknown,
                    separators=(",", ":"),
                ).encode("utf-8"),
            }
            expected = {
                "duplicate": "json.duplicate_key",
                "invalid_utf8": "json.invalid_utf8",
                "unknown": "settings.unknown_field",
            }

            for name, raw in cases.items():
                with self.subTest(name=name):
                    settings_path.write_bytes(raw)
                    code, stdout, stderr = invoke_cli(
                        [
                            "import-trellis",
                            str(snapshot_path),
                            str(settings_path),
                        ]
                    )
                    self.assertEqual(2, code)
                    self.assertEqual("", stdout)
                    self.assertIn(expected[name], stderr)
                    self.assertNotIn("Traceback", stderr)

    def test_import_trellis_rejects_invalid_snapshot_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            snapshot_path.write_bytes(b'{"schema_version":1,"goal":"\xff"}')

            code, stdout, stderr = invoke_cli(
                ["import-trellis", str(snapshot_path), str(settings_path)]
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertIn("invalid_utf8", stderr)
            self.assertNotIn("Traceback", stderr)

    def test_import_trellis_output_failures_leave_no_partial_manifest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            root = Path(temporary_directory)

            missing_output = root / "missing" / "execution-manifest.json"
            code, _, stderr = invoke_cli(
                [
                    "import-trellis",
                    str(snapshot_path),
                    str(settings_path),
                    "--output",
                    str(missing_output),
                ]
            )
            self.assertEqual(2, code)
            self.assertFalse(missing_output.exists())
            self.assertNotIn("Traceback", stderr)

            for operation in ("fsync", "replace"):
                with self.subTest(operation=operation):
                    output_path = root / f"{operation}-manifest.json"
                    with mock.patch.object(
                        wishctl.os,
                        operation,
                        side_effect=OSError(f"{operation} failed"),
                    ):
                        code, _, stderr = invoke_cli(
                            [
                                "import-trellis",
                                str(snapshot_path),
                                str(settings_path),
                                "--output",
                                str(output_path),
                            ]
                        )
                    self.assertEqual(2, code)
                    self.assertFalse(output_path.exists())
                    self.assertIn(f"{operation} failed", stderr)
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual([], list(root.glob(f".{output_path.name}.*.tmp")))

    def test_import_trellis_reports_gate_b_invalidation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            base_arguments = [
                "import-trellis",
                str(snapshot_path),
                str(settings_path),
            ]
            code, _, stderr = invoke_cli(base_arguments)
            self.assertEqual(0, code)
            graph_digest = json.loads(stderr)["trellis_graph_digest"]

            code, _, stderr = invoke_cli(
                base_arguments + ["--approved-graph-digest", graph_digest]
            )
            self.assertEqual(0, code)
            self.assertFalse(json.loads(stderr)["gate_b_invalidated"])

            different_digest = import_digest("f")
            self.assertNotEqual(graph_digest, different_digest)
            code, _, stderr = invoke_cli(
                base_arguments + ["--approved-graph-digest", different_digest]
            )
            self.assertEqual(0, code)
            self.assertTrue(json.loads(stderr)["gate_b_invalidated"])

    def test_imported_manifest_v2_flows_through_validate_ready_and_trace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            root = Path(temporary_directory)
            manifest_path = root / "execution-manifest.json"
            trace_path = root / "trace.md"

            import_code, _, import_stderr = invoke_cli(
                [
                    "import-trellis",
                    str(snapshot_path),
                    str(settings_path),
                    "--output",
                    str(manifest_path),
                ]
            )
            planning_code, planning_stdout, planning_stderr = invoke_cli(
                ["validate", str(manifest_path), "--stage", "planning"]
            )
            execution_code, execution_stdout, execution_stderr = invoke_cli(
                ["validate", str(manifest_path), "--stage", "execution"]
            )
            ready_code, ready_stdout, ready_stderr = invoke_cli(
                ["ready", str(manifest_path)]
            )
            trace_code, trace_stdout, trace_stderr = invoke_cli(
                ["trace", str(manifest_path), "--output", str(trace_path)]
            )

            self.assertEqual(0, import_code, import_stderr)
            self.assertEqual(0, planning_code, planning_stderr)
            self.assertIn("OK: manifest valid", planning_stdout)
            self.assertEqual(1, execution_code, execution_stderr)
            self.assertIn("Gate B approval evidence is incomplete", execution_stdout)
            self.assertEqual(0, ready_code, ready_stderr)
            ready = json.loads(ready_stdout)
            self.assertEqual(["TASK-001"], ready["task_ids"])
            self.assertFalse(ready["execution_admitted"])
            self.assertEqual("gate_b_approval_required", ready["admission_blocker"])
            self.assertEqual(0, trace_code, trace_stderr)
            self.assertEqual("", trace_stdout)
            trace = trace_path.read_text(encoding="utf-8")
            self.assertIn("REQ-001", trace)
            self.assertIn("Run adapter tests", trace)

    def test_decide_commits_direct_cli_gate_and_replays_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            request_path, journal_root, request, repository = write_decision_fixture(
                temporary_directory
            )
            arguments = [
                "decide",
                str(request_path),
                "--journal-root",
                str(journal_root),
                "--workspace-hash",
                request.workspace_hash,
                "--workspace-root",
                str(repository),
                "--workspace-scope",
                "README.md",
                "--choice",
                "approve",
                "--actor-id",
                request.expected_actor_id,
                "--host-id",
                "host-001",
            ]
            with mock.patch.object(
                wishctl,
                "_utc_now",
                return_value="2026-08-19T01:00:01Z",
            ), mock.patch.object(wishctl.time, "time_ns", return_value=123456789):
                first_code, first_stdout, first_stderr = invoke_cli(arguments)
                second_code, second_stdout, second_stderr = invoke_cli(arguments)

            self.assertEqual(0, first_code)
            self.assertEqual("", first_stderr)
            first = json.loads(first_stdout)
            self.assertFalse(first["idempotent"])
            self.assertEqual("accepted", first["reason"])
            self.assertEqual(2, first["event_sequence"])

            self.assertEqual(0, second_code)
            self.assertEqual("", second_stderr)
            second = json.loads(second_stdout)
            self.assertTrue(second["idempotent"])
            self.assertEqual("idempotent_replay", second["reason"])
            self.assertEqual(first["event_hash"], second["event_hash"])
            segment = journal_root / "segments" / "segment-00000001.jsonl"
            self.assertEqual(2, len(segment.read_bytes().splitlines()))

    def test_decide_rejects_actor_workspace_and_journal_drift_without_append(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            request_path, journal_root, request, repository = write_decision_fixture(
                temporary_directory
            )
            base = [
                "decide",
                str(request_path),
                "--journal-root",
                str(journal_root),
                "--workspace-hash",
                request.workspace_hash,
                "--workspace-root",
                str(repository),
                "--workspace-scope",
                "README.md",
                "--choice",
                "approve",
                "--actor-id",
                request.expected_actor_id,
            ]

            actor_code, _, actor_stderr = invoke_cli(
                [*base[:-1], "different-account"]
            )
            self.assertEqual(1, actor_code)
            self.assertIn("actor_mismatch", actor_stderr)

            workspace_arguments = list(base)
            workspace_arguments[workspace_arguments.index(request.workspace_hash)] = (
                import_digest("c")
            )
            workspace_code, _, workspace_stderr = invoke_cli(workspace_arguments)
            self.assertEqual(1, workspace_code)
            self.assertIn("workspace_drift", workspace_stderr)

            segment = journal_root / "segments" / "segment-00000001.jsonl"
            self.assertEqual(1, len(segment.read_bytes().splitlines()))
            segment.write_bytes(segment.read_bytes() + b"not-json\n")
            corrupt_code, _, corrupt_stderr = invoke_cli(base)
            self.assertEqual(2, corrupt_code)
            self.assertIn("journal event rejected", corrupt_stderr)
            self.assertNotIn("Traceback", corrupt_stderr)

    def test_import_trellis_does_not_create_gate_or_journal_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_path, settings_path = write_import_inputs(temporary_directory)
            root = Path(temporary_directory)
            output_path = root / "execution-manifest.json"

            code, _, _ = invoke_cli(
                [
                    "import-trellis",
                    str(snapshot_path),
                    str(settings_path),
                    "--output",
                    str(output_path),
                ]
            )

            self.assertEqual(0, code)
            self.assertEqual(
                {
                    "execution-manifest.json",
                    "import-settings.json",
                    "trellis-snapshot.json",
                },
                {path.name for path in root.iterdir()},
            )

    def test_help_matches_golden(self):
        golden_path = Path(__file__).parent / "golden" / "wishctl-help.txt"
        self.assertEqual(
            golden_path.read_text(encoding="utf-8"),
            wishctl.build_parser().format_help(),
        )


if __name__ == "__main__":
    unittest.main()
