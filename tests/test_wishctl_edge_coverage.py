from __future__ import annotations

import argparse
import contextlib
import copy
import dataclasses
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.test_wishctl import (
    direct_decision_request,
    invoke_cli,
    valid_import_settings,
    valid_manifest,
    valid_trellis_snapshot,
    write_import_inputs,
)
from wish_builder.cli import wishctl
from wish_builder.contracts import (
    ActorType,
    DecisionRequestPayload,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
)
from wish_builder.services.journal import GENESIS_HEAD


def _journal_event(
    sequence: int,
    previous_event_hash: str,
    *,
    run_id: str = "WISH-CLI-EDGE",
) -> JournalEvent:
    request = direct_decision_request()
    request = dataclasses.replace(
        request,
        command=dataclasses.replace(
            request.command,
            command_id=f"COMMAND-EDGE-{sequence:04d}",
            request_id=f"REQUEST-EDGE-{sequence:04d}",
            expected_sequence=sequence,
            request_nonce=f"nonce-edge-{sequence:04d}",
        ),
    )
    return JournalEvent.create(
        sequence=sequence,
        event_id=f"EVENT-EDGE-{sequence:04d}-{run_id}",
        event_type=JournalEventType.DECISION_REQUESTED,
        identity=ExecutionIdentity(run_id, 1),
        actor_type=ActorType.COORDINATOR,
        actor_id="coordinator-001",
        recorded_at=f"2026-08-19T01:00:{sequence:02d}Z",
        previous_event_hash=previous_event_hash,
        payload=DecisionRequestPayload(request),
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")


class WishCtlDecodeAndIoEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_settings_decoder_rejects_wrong_missing_constant_and_syntax(self) -> None:
        source = self.root / "settings.json"
        missing = valid_import_settings()
        missing.pop("provider")
        cases = (
            (b"[]", "wrong_container_type"),
            (json.dumps(missing).encode(), "missing_field"),
            (b"NaN", "invalid_constant"),
            (b'{"broken":', "invalid_syntax"),
        )
        for raw, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(
                wishctl.ManifestError,
                code,
            ):
                wishctl._decode_import_settings(raw, source)

    def test_trellis_import_reports_missing_files_and_invalid_settings_values(self) -> None:
        missing = self.root / "missing.json"
        settings = self.root / "settings.json"
        snapshot_path = self.root / "snapshot.json"
        _write_json(settings, valid_import_settings())
        _write_json(snapshot_path, valid_trellis_snapshot())

        with self.assertRaisesRegex(wishctl.ManifestError, "snapshot not found"):
            wishctl._load_trellis_import(missing, settings)
        with self.assertRaisesRegex(wishctl.ManifestError, "settings not found"):
            wishctl._load_trellis_import(snapshot_path, missing)

        bad_paths = valid_import_settings()
        bad_paths["protected_paths"] = ["src/**", 1]
        _write_json(settings, bad_paths)
        with self.assertRaisesRegex(wishctl.ManifestError, "protected_paths"):
            wishctl._load_trellis_import(snapshot_path, settings)

        bad_provider = valid_import_settings()
        bad_provider["provider"] = "unsupported-provider"
        _write_json(settings, bad_provider)
        with self.assertRaisesRegex(wishctl.ManifestError, "invalid_value"):
            wishctl._load_trellis_import(snapshot_path, settings)

        bad_lease = valid_import_settings()
        bad_lease["lease_clock_skew_seconds"] = 23
        _write_json(settings, bad_lease)
        with self.assertRaisesRegex(wishctl.ManifestError, "one-quarter TTL"):
            wishctl._load_trellis_import(snapshot_path, settings)

    def test_atomic_write_detects_a_late_collision_and_cleans_best_effort(self) -> None:
        target = self.root / "manifest.json"
        with (
            mock.patch.object(Path, "exists", side_effect=(False, True)),
            mock.patch.object(Path, "unlink", side_effect=FileNotFoundError),
            self.assertRaisesRegex(wishctl.ManifestError, "output exists"),
        ):
            wishctl._atomic_write_bytes(target, b"payload", force=False)

    def test_stdout_binary_path_preserves_exact_bytes(self) -> None:
        stream = SimpleNamespace(buffer=io.BytesIO())
        with mock.patch.object(wishctl.sys, "stdout", stream):
            wishctl._write_stdout_bytes(b'{"ok":true}\n')
        self.assertEqual(b'{"ok":true}\n', stream.buffer.getvalue())

    def test_decision_request_loader_reports_missing_oversized_and_invalid_data(self) -> None:
        missing = self.root / "missing-request.json"
        with self.assertRaisesRegex(wishctl.DecisionCliError, "not found"):
            wishctl._load_decision_request(missing)

        oversized = self.root / "oversized-request.json"
        oversized.write_bytes(b"x" * (wishctl.MAX_DECISION_REQUEST_BYTES + 1))
        with self.assertRaisesRegex(wishctl.DecisionCliError, "exceeds"):
            wishctl._load_decision_request(oversized)

        invalid = self.root / "invalid-request.json"
        invalid.write_bytes(b"{}")
        with self.assertRaisesRegex(wishctl.DecisionCliError, "request rejected"):
            wishctl._load_decision_request(invalid)

    def test_manifest_loader_reports_missing_and_invalid_data(self) -> None:
        with self.assertRaisesRegex(wishctl.ManifestError, "manifest not found"):
            wishctl.load_manifest(self.root / "missing-manifest.json")
        invalid = self.root / "invalid-manifest.json"
        invalid.write_bytes(b"{}")
        with self.assertRaisesRegex(wishctl.ManifestError, "manifest rejected"):
            wishctl.load_manifest(invalid)

    def test_manifest_schema_probe_fails_closed_on_parser_limits(self) -> None:
        for parser_error in (RecursionError(), ValueError("integer too large")):
            with self.subTest(error=type(parser_error).__name__), mock.patch.object(
                wishctl.json,
                "loads",
                side_effect=parser_error,
            ):
                self.assertIsNone(
                    wishctl._manifest_schema_version(b'{"schema_version":2}')
                )


class WishCtlJournalEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def segment(self, number: int, raw: bytes) -> Path:
        segments = self.root / "segments"
        segments.mkdir(exist_ok=True)
        path = segments / f"segment-{number:08d}.jsonl"
        path.write_bytes(raw)
        return path

    def test_journal_reader_rejects_missing_noncontiguous_and_empty_layouts(self) -> None:
        with self.assertRaisesRegex(wishctl.DecisionCliError, "segments not found"):
            wishctl._read_verified_journal(self.root)

        self.segment(2, b"invalid\n")
        with self.assertRaisesRegex(wishctl.DecisionCliError, "non-contiguous"):
            wishctl._read_verified_journal(self.root)

        (self.root / "segments" / "segment-00000002.jsonl").unlink()
        self.segment(1, b"")
        with self.assertRaisesRegex(wishctl.DecisionCliError, "segment is empty"):
            wishctl._read_verified_journal(self.root)

    def test_journal_reader_rejects_unreadable_incomplete_and_invalid_frames(self) -> None:
        segment = self.segment(1, b"invalid\n")
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=OSError("denied"),
        ), self.assertRaisesRegex(wishctl.DecisionCliError, "unreadable"):
            wishctl._read_verified_journal(self.root)

        segment.write_bytes(b"invalid")
        with self.assertRaisesRegex(wishctl.DecisionCliError, "incomplete final frame"):
            wishctl._read_verified_journal(self.root)

        segment.write_bytes(b"{}\n")
        with self.assertRaisesRegex(wishctl.DecisionCliError, "event rejected"):
            wishctl._read_verified_journal(self.root)

    def test_journal_reader_rejects_bad_chain_and_mixed_run_identity(self) -> None:
        bad_sequence = _journal_event(2, GENESIS_HEAD.event_hash)
        self.segment(1, bad_sequence.canonical_json_bytes())
        with self.assertRaisesRegex(wishctl.DecisionCliError, "hash chain"):
            wishctl._read_verified_journal(self.root)

        first = _journal_event(1, GENESIS_HEAD.event_hash)
        second = _journal_event(2, first.event_hash, run_id="WISH-OTHER")
        self.segment(1, first.canonical_json_bytes() + second.canonical_json_bytes())
        with self.assertRaisesRegex(wishctl.DecisionCliError, "multiple run identities"):
            wishctl._read_verified_journal(self.root)


class WishCtlLegacyValidationEdgeTests(unittest.TestCase):
    def test_path_and_graph_helpers_cover_conservative_boundaries(self) -> None:
        self.assertEqual("src/file", wishctl._normalize_path("././src\\file/"))
        self.assertEqual("src", wishctl._static_prefix("src/*/file.py"))
        self.assertTrue(wishctl.patterns_overlap("SRC/A/**", "src/a/**"))
        self.assertTrue(wishctl.patterns_overlap("*", "docs/**"))
        self.assertFalse(wishctl.patterns_overlap("src/a/**", "src/b/**"))
        self.assertTrue(wishctl.path_matches("src/a", "src/**"))
        self.assertFalse(wishctl.path_matches("src/a/file.txt", "src/*.py"))
        self.assertTrue(wishctl.path_matches("src/a/file.txt", "src"))

        self.assertEqual({}, wishctl._task_map({"tasks": "wrong"}))
        tasks = {
            "TASK-A": {"depends_on": ["TASK-B"]},
            "TASK-B": {"depends_on": ["TASK-C", "TASK-MISSING"]},
            "TASK-C": {"depends_on": []},
        }
        self.assertTrue(wishctl._depends_on("TASK-A", "TASK-C", tasks))
        self.assertFalse(wishctl._depends_on("TASK-MISSING", "TASK-C", tasks))
        self.assertEqual([], wishctl._find_cycles(tasks))
        cyclic = copy.deepcopy(tasks)
        cyclic["TASK-C"]["depends_on"] = ["TASK-A"]
        self.assertTrue(wishctl._find_cycles(cyclic))
        self.assertEqual(3, wishctl._max_depth(cyclic))

    def test_validate_manifest_reports_malformed_root_requirements_and_tasks(self) -> None:
        manifest = valid_manifest()
        manifest.update(
            schema_version=0,
            run_id="",
            goal=1,
            base_branch=" ",
            max_concurrency=0,
            protected_paths=[1],
        )
        manifest["requirements"].extend(
            [
                None,
                {"id": "bad", "text": "bad", "status": "approved"},
                {"id": "REQ-001", "text": "", "status": "unknown"},
            ]
        )
        manifest["tasks"].extend([None, {"id": "bad"}])

        task = manifest["tasks"][0]
        task.update(
            title="",
            requirement_ids=["REQ-999"],
            depends_on=["TASK-001", "TASK-999"],
            owned_paths=[],
            allowed_auxiliary_paths=[""],
            acceptance_criteria=[],
            regression_commands=[],
            rollback="",
            documentation=[1],
            wave=1,
            risk="extreme",
            may_change_contracts=True,
            issue_id=None,
            branch="main",
            status="proposed",
        )
        errors, _ = wishctl.validate_manifest(manifest, "execution")
        expected = (
            "schema_version",
            "run_id",
            "max_concurrency",
            "protected_paths",
            "duplicate requirement id",
            "must be an object",
            "unknown requirements",
            "depends on itself",
            "unknown dependencies",
            "still proposed",
        )
        for fragment in expected:
            with self.subTest(fragment=fragment):
                self.assertTrue(any(fragment in error for error in errors), errors)

    def test_validate_manifest_reports_execution_identity_collisions(self) -> None:
        manifest = valid_manifest()
        for task in manifest["tasks"][1:3]:
            task.update(
                status="pr_open",
                agent_owner="worker-001",
                issue_id=99,
                branch="feat/shared",
                pr_id=199,
            )
        manifest["tasks"][0]["squash_commit"] = None
        errors, _ = wishctl.validate_manifest(manifest, "execution")
        for fragment in (
            "Issue 99 is shared",
            "branch 'feat/shared' is shared",
            "PR 199 is shared",
            "squash_commit is required",
        ):
            with self.subTest(fragment=fragment):
                self.assertTrue(any(fragment in error for error in errors), errors)

    def test_validate_manifest_warns_for_deep_dependency_chain(self) -> None:
        manifest = valid_manifest()
        template = copy.deepcopy(manifest["tasks"][1])
        tasks = []
        for index in range(6):
            task = copy.deepcopy(template)
            task.update(
                id=f"TASK-10{index}",
                title=f"Step {index}",
                requirement_ids=["REQ-001"],
                depends_on=[] if index == 0 else [f"TASK-10{index - 1}"],
                owned_paths=[f"src/deep/{index}/**"],
                wave=1,
                issue_id=100 + index,
                branch=f"feat/deep-{index}",
            )
            tasks.append(task)
        manifest["tasks"] = tasks
        _, warnings = wishctl.validate_manifest(manifest, "planning")
        self.assertTrue(any("dependency depth" in warning for warning in warnings))

    def test_ready_tasks_covers_complete_busy_dependency_and_overlap_states(self) -> None:
        complete = valid_manifest()
        for task in complete["tasks"]:
            task["status"] = "merged"
        self.assertTrue(wishctl.ready_tasks(complete)["complete"])

        busy = valid_manifest()
        busy["max_concurrency"] = 1
        busy["tasks"][1].update(status="dispatched", agent_owner="worker-001")
        self.assertEqual(0, wishctl.ready_tasks(busy)["capacity"])

        blocked = valid_manifest()
        blocked["tasks"][1]["status"] = "blocked"
        blocked["tasks"][2]["depends_on"] = ["TASK-002"]
        self.assertEqual([], wishctl.ready_tasks(blocked)["task_ids"])

        overlap = valid_manifest()
        overlap["max_concurrency"] = 2
        overlap["tasks"][1].update(status="dispatched", agent_owner="worker-001")
        overlap["tasks"][2]["owned_paths"] = overlap["tasks"][1]["owned_paths"]
        self.assertEqual([], wishctl.ready_tasks(overlap)["task_ids"])
        self.assertEqual(1, len(wishctl.ready_tasks(valid_manifest(), limit=1)["task_ids"]))

    def test_drift_trace_print_and_changed_file_helpers(self) -> None:
        with self.assertRaisesRegex(wishctl.ManifestError, "unknown task"):
            wishctl.drift_report(valid_manifest(), "TASK-MISSING", ())

        manifest = valid_manifest()
        manifest["requirements"][0]["text"] = "contains | pipe\nand newline"
        traced = wishctl.trace_markdown(manifest)
        self.assertIn("# Requirement Traceability", traced)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            wishctl._print_validation(["bad"], ["careful"])
        self.assertIn("WARNING: careful", output.getvalue())
        self.assertIn("ERROR: bad", output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            wishctl._print_validation([], [])
        self.assertIn("OK: manifest valid", output.getvalue())

        changed_file = Path(tempfile.gettempdir()) / "wish-builder-changed-files.txt"
        self.addCleanup(changed_file.unlink, missing_ok=True)
        changed_file.write_text("src/a.py\nsrc/b.py\n", encoding="utf-8")
        args = argparse.Namespace(changed_file=["src/direct.py"], changed_files=str(changed_file))
        self.assertEqual(
            ["src/direct.py", "src/a.py", "src/b.py"],
            wishctl._read_changed_files(args),
        )

        stdin_args = argparse.Namespace(changed_file=None, changed_files=None)
        with mock.patch.object(wishctl.sys, "stdin", io.StringIO("src/stdin.py\n")):
            self.assertEqual(["src/stdin.py"], wishctl._read_changed_files(stdin_args))
        with mock.patch.object(
            wishctl.sys,
            "stdin",
            io.StringIO("\n"),
        ), self.assertRaisesRegex(wishctl.ManifestError, "no changed files"):
            wishctl._read_changed_files(stdin_args)


class WishCtlManifestV2EdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        snapshot_path, settings_path = write_import_inputs(self.root)
        self.manifest_path = self.root / "manifest-v2.json"
        code, _, stderr = invoke_cli(
            [
                "import-trellis",
                str(snapshot_path),
                str(settings_path),
                "--output",
                str(self.manifest_path),
            ]
        )
        if code != 0:
            raise AssertionError(stderr)
        self.manifest = wishctl.load_manifest(self.manifest_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_v2_validation_rejects_cycles_orphans_and_finish_without_state(self) -> None:
        first, second = self.manifest.tasks
        cyclic = dataclasses.replace(
            self.manifest,
            tasks=(dataclasses.replace(first, depends_on=(second.id,)), second),
        )
        errors, _ = wishctl._validate_manifest_v2(cyclic, "planning")
        self.assertTrue(any("Dependency cycle" in error for error in errors))

        extra_requirement = dataclasses.replace(
            self.manifest.requirements[0],
            id="REQ-099",
        )
        orphaned = dataclasses.replace(
            self.manifest,
            requirements=(*self.manifest.requirements, extra_requirement),
        )
        errors, _ = wishctl._validate_manifest_v2(orphaned, "planning")
        self.assertTrue(any("REQ-099" in error for error in errors))

        errors, _ = wishctl._validate_admitted_manifest(self.manifest, "finish")
        self.assertIn("Gate B approval evidence is incomplete.", errors)
        self.assertFalse(any("replayed runtime state" in error for error in errors))

    def test_v2_ready_caps_preview_and_trace_formats_command_fallbacks(self) -> None:
        ready = wishctl._ready_manifest_v2(self.manifest, limit=99)
        self.assertEqual(self.manifest.max_concurrency, ready["capacity"])
        self.assertFalse(ready["execution_admitted"])

        primitive = self.manifest.to_primitive()
        command = primitive["tasks"][0]["regression_commands"][0]
        command["display_text"] = None
        trace = wishctl.trace_markdown(primitive)
        self.assertIn("python -m unittest tests.adapters", trace)
        primitive["tasks"][0]["regression_commands"] = [{}]
        self.assertIn("{}", wishctl.trace_markdown(primitive))


class WishCtlCommandDispatchEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest_path = self.root / "manifest.json"
        _write_json(self.manifest_path, valid_manifest())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_hash_ready_drift_and_trace_output_commands(self) -> None:
        artifact = self.root / "artifact.bin"
        artifact.write_bytes(b"artifact")
        code, stdout, stderr = invoke_cli(["hash", str(artifact)])
        self.assertEqual((0, ""), (code, stderr))
        self.assertTrue(stdout.startswith("sha256:"))

        code, _, stderr = invoke_cli(["ready", str(self.manifest_path), "--limit", "0"])
        self.assertEqual(2, code)
        self.assertIn("positive integer", stderr)

        code, stdout, stderr = invoke_cli(
            [
                "drift",
                str(self.manifest_path),
                "--task",
                "TASK-002",
                "--changed-file",
                "src/a/file.py",
            ]
        )
        self.assertEqual((0, ""), (code, stderr))
        self.assertIn('"ok": true', stdout)

        trace_path = self.root / "trace.md"
        code, stdout, stderr = invoke_cli(
            ["trace", str(self.manifest_path), "--output", str(trace_path)]
        )
        self.assertEqual((0, "", ""), (code, stdout, stderr))
        self.assertIn("# Requirement Traceability", trace_path.read_text(encoding="utf-8"))

    def test_package_main_module_dispatches_to_cli(self) -> None:
        artifact = self.root / "artifact.bin"
        artifact.write_bytes(b"artifact")
        stdout = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["wish_builder", "hash", str(artifact)]),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_module("wish_builder", run_name="__main__")
        self.assertEqual(0, raised.exception.code)
        self.assertTrue(stdout.getvalue().startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
