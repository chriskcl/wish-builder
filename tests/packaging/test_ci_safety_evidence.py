from __future__ import annotations

import ast
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.ci_mutation_gate import MutationSpec
from scripts.ci_safety_registry import ACTIVE_M1_SAFETY_PATHS, SafetyPathRegistry
from scripts.ci_safety_evidence import (
    BranchProjection,
    CHANGED_LINES_SCHEMA_VERSION,
    ChangedLinesInputError,
    _allowed_static_exclusion_lines,
    _branch_projections,
    _git_blob_oid,
    _line_evidence_span,
    _parse_unified_added_ranges,
    _parse_unified_hunks,
    _projection_refs,
    _source_branch_arc_universe,
    _source_sha256,
    collect_changed_lines,
    evaluate_safety_evidence as _evaluate_safety_evidence,
    main,
)

ADAPTER_PATH = "wish_builder/adapters/git_worktree.py"
EXAMPLE_SOURCE = (
    "def decide(allowed):\n"
    "    if allowed:\n"
    "        return True\n"
    "    else:\n"
    "        return False\n"
    "\n"
    "def audit(flag):\n"
    "    if flag:\n"
    "        return 'yes'\n"
    "    return 'no'\n"
)
ADAPTER_SOURCE = (
    "def promote(allowed):\n"
    "    if allowed:\n"
    "        return 'promoted'\n"
    "    return 'blocked'\n"
)

SPEC = MutationSpec(
    "TEST-SAFETY-BRANCH",
    "The safety decision remains fail closed.",
    "wish_builder/kernel/example.py",
    "    if allowed:\n",
    "    if False and allowed:\n",
    ("tests.example.ExampleTests.test_denied",),
)


def coverage_file(
    *,
    executed_branches: list[list[int]],
    executed_lines: list[int],
    missing_branches: list[list[int]] | None = None,
    missing_lines: list[int] | None = None,
    excluded_lines: list[int] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "excluded_lines": list(excluded_lines or []),
        "executed_branches": executed_branches,
        "executed_lines": executed_lines,
        "missing_branches": list(missing_branches or []),
        "missing_lines": list(missing_lines or []),
    }
    refresh_branch_summary(payload)
    return payload


def refresh_branch_summary(payload: dict[str, object]) -> None:
    executed = {tuple(item) for item in payload["executed_branches"]}
    missing = {tuple(item) for item in payload["missing_branches"]}
    payload["summary"] = {
        "covered_branches": len(executed),
        "missing_branches": len(missing),
        "num_branches": len(executed | missing),
    }


def coverage_report() -> dict[str, object]:
    return {
        "files": {
            "wish_builder/kernel/example.py": coverage_file(
                executed_branches=[[2, 3], [2, 5]],
                executed_lines=[1, 2, 3, 5],
            )
        },
        "meta": {"branch_coverage": True},
    }


def mutation_report() -> dict[str, object]:
    return {
        "baseline": {"successful": True},
        "policy": {"passed": True},
        "results": [
            {
                "invariant": SPEC.invariant,
                "mutation_id": SPEC.mutation_id,
                "safety_invariant": True,
                "source_path": SPEC.source_path,
                "status": "killed",
                "test_ids": list(SPEC.test_ids),
                "test_run": {
                    "errors": 0,
                    "failures": 1,
                    "infrastructure_error": None,
                    "successful": False,
                    "tests_run": 1,
                },
            }
        ],
        "status": "passed",
    }


def changed_lines(
    *ranges: tuple[int, int],
    path: str = "wish_builder/kernel/example.py",
    status: str = "M",
    old_source: str | None = None,
    new_source: str | None = None,
) -> dict[str, object]:
    default_source = ADAPTER_SOURCE if path == ADAPTER_PATH else EXAMPLE_SOURCE
    if old_source is None and status != "A":
        old_source = default_source
    if new_source is None and status != "D":
        new_source = default_source
    if status == "M" and old_source == new_source and old_source is not None and ranges:
        old_lines = old_source.splitlines(keepends=True)
        for first, last in ranges:
            for index in range(first - 1, last):
                line = old_lines[index]
                if line.endswith("\n"):
                    old_lines[index] = line[:-1] + " \n"
                else:
                    old_lines[index] = line + " "
        old_source = "".join(old_lines)
    old_path: str | None = path
    new_path: str | None = path
    if status == "A":
        old_path = None
    elif status == "D":
        new_path = None
    old_control_flow = () if old_source is None else _branch_projections(old_source)
    new_control_flow = () if new_source is None else _branch_projections(new_source)
    hunks = []
    for first, last in ranges:
        count = last - first + 1
        old_first = 0 if status == "A" else first
        old_count = 0 if status == "A" else count
        new_first = 0 if status == "D" else first
        new_count = 0 if status == "D" else count
        hunks.append(
            {
                "new_branch_refs": list(
                    _projection_refs(new_control_flow, new_first, new_count)
                ),
                "new_count": new_count,
                "new_first": new_first,
                "old_branch_refs": list(
                    _projection_refs(old_control_flow, old_first, old_count)
                ),
                "old_count": old_count,
                "old_first": old_first,
            }
        )
    return {
        "base_ref": "refs/heads/main",
        "files": [
            {
                "hunks": hunks,
                "new_blob_oid": (
                    None if new_source is None else _git_blob_oid(new_source)
                ),
                "new_control_flow": [
                    projection.to_primitive() for projection in new_control_flow
                ],
                "new_path": new_path,
                "new_source": new_source,
                "new_source_sha256": (
                    None if new_source is None else _source_sha256(new_source)
                ),
                "old_blob_oid": (
                    None if old_source is None else _git_blob_oid(old_source)
                ),
                "old_control_flow": [
                    projection.to_primitive() for projection in old_control_flow
                ],
                "old_path": old_path,
                "old_source": old_source,
                "old_source_sha256": (
                    None if old_source is None else _source_sha256(old_source)
                ),
                "status": status,
            }
        ],
        "head": "b" * 40,
        "merge_base": "a" * 40,
        "schema_version": CHANGED_LINES_SCHEMA_VERSION,
    }


def evaluate_safety_evidence(
    coverage: object,
    mutations: object,
    changed: object,
    **kwargs: object,
) -> dict[str, object]:
    """Evaluate synthetic fixtures only after explicitly trusting their artifact."""
    return _evaluate_safety_evidence(
        coverage,
        mutations,
        changed,
        trusted_changed_lines_report=changed,
        **kwargs,
    )


class SafetyEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / SPEC.source_path
        source.parent.mkdir(parents=True)
        source.write_text(EXAMPLE_SOURCE, encoding="utf-8")
        adapter = self.root / ADAPTER_PATH
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text(ADAPTER_SOURCE, encoding="utf-8")
        self.registry = SafetyPathRegistry(
            exact_paths=(SPEC.source_path,),
            recursive_python_roots=("wish_builder/adapters",),
        )

    def test_cli_writes_the_exact_changed_lines_input_atomically(self) -> None:
        coverage_path = self.root / "coverage.json"
        mutation_path = self.root / "mutation.json"
        changed_path = self.root / "changed-input.json"
        changed_output = self.root / "changed-output.json"
        safety_output = self.root / "safety.json"
        artifact = changed_lines((2, 2))
        for path, value in (
            (coverage_path, {}),
            (mutation_path, {}),
            (changed_path, artifact),
        ):
            path.write_text(json.dumps(value), encoding="utf-8")

        captured = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        with patch(
            "scripts.ci_safety_evidence.evaluate_safety_evidence",
            return_value={"status": "pass"},
        ), patch(
            "scripts.ci_safety_evidence.collect_changed_lines",
            return_value=artifact,
        ), patch("sys.stdout", captured):
            exit_code = main(
                [
                    str(coverage_path),
                    str(mutation_path),
                    "--base-ref",
                    "refs/heads/main",
                    "--changed-lines",
                    str(changed_path),
                    "--changed-lines-output",
                    str(changed_output),
                    "--output",
                    str(safety_output),
                ]
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(artifact, json.loads(changed_output.read_text(encoding="utf-8")))
        self.assertFalse(any(self.root.glob(".changed-output.json.*.tmp")))

    def test_cli_replay_recollects_complete_git_provenance(self) -> None:
        coverage_path = self.root / "coverage.json"
        mutation_path = self.root / "mutation.json"
        changed_path = self.root / "changed-input.json"
        trusted = changed_lines((2, 2))
        forged = dict(trusted)
        forged["files"] = []
        for path, value in (
            (coverage_path, coverage_report()),
            (mutation_path, mutation_report()),
            (changed_path, forged),
        ):
            path.write_text(json.dumps(value), encoding="utf-8")

        raw_stdout = io.BytesIO()
        captured = io.TextIOWrapper(raw_stdout, encoding="utf-8")
        with patch(
            "scripts.ci_safety_evidence.collect_changed_lines",
            return_value=trusted,
        ), patch(
            "scripts.ci_safety_evidence.evaluate_safety_evidence"
        ) as evaluate, patch("sys.stdout", captured):
            exit_code = main(
                [
                    str(coverage_path),
                    str(mutation_path),
                    "--base-ref",
                    "refs/heads/main",
                    "--changed-lines",
                    str(changed_path),
                ]
            )
        captured.flush()
        result = json.loads(raw_stdout.getvalue())

        self.assertEqual(2, exit_code)
        self.assertEqual("error", result["status"])
        self.assertEqual(
            "changed_lines_provenance_mismatch", result["errors"][0]["code"]
        )
        evaluate.assert_not_called()

    def test_direct_evaluator_cannot_authorize_untrusted_empty_inventory(
        self,
    ) -> None:
        artifact = changed_lines((2, 2))
        artifact["files"] = []

        with patch("scripts.ci_safety_evidence.collect_changed_lines") as collector:
            result = _evaluate_safety_evidence(
                coverage_report(),
                mutation_report(),
                artifact,
                source_root=self.root,
                specs=(SPEC,),
                path_registry=self.registry,
            )

        self.assertEqual("fail", result["status"])
        self.assertIn(
            "changed_lines_provenance_unverified",
            {error["code"] for error in result["errors"]},
        )
        collector.assert_not_called()

    def test_direct_evaluator_rejects_an_explicit_trusted_report_mismatch(
        self,
    ) -> None:
        trusted = changed_lines((2, 2))
        forged = changed_lines((2, 2))
        forged["files"] = []

        result = _evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            forged,
            trusted_changed_lines_report=trusted,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("fail", result["status"])
        self.assertIn(
            "changed_lines_provenance_mismatch",
            {error["code"] for error in result["errors"]},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_covered_anchor_and_killed_mutation_form_direct_evidence(self) -> None:
        first = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            changed_lines((2, 2)),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        second = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            changed_lines((2, 2)),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", first["status"])
        self.assertEqual(first, second)
        self.assertEqual(1, first["invariant_count"])
        self.assertEqual(1, first["changed_branch_count"])
        self.assertEqual([SPEC.mutation_id], first["changed_branches"][0]["mutation_ids"])
        self.assertEqual(list(SPEC.test_ids), first["changed_branches"][0]["test_ids"])
        self.assertEqual([], first["errors"])

    def test_uncovered_surviving_or_drifted_invariant_fails_closed(self) -> None:
        coverage = coverage_report()
        coverage["files"][SPEC.source_path]["executed_branches"] = [[2, 3]]
        coverage["files"][SPEC.source_path]["missing_branches"] = [[2, 5]]
        refresh_branch_summary(coverage["files"][SPEC.source_path])
        mutations = mutation_report()
        mutations["results"][0]["status"] = "survived"
        mutations["results"][0]["test_run"]["successful"] = True
        mutations["results"][0]["test_run"]["failures"] = 0

        result = evaluate_safety_evidence(
            coverage,
            mutations,
            changed_lines((2, 2)),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        codes = {error["code"] for error in result["errors"]}
        self.assertEqual("fail", result["status"])
        self.assertIn("source_anchor_branch_uncovered", codes)
        self.assertIn("direct_mutation_evidence_invalid", codes)

        (self.root / SPEC.source_path).write_text(
            "def decide(allowed):\n    return bool(allowed)\n",
            encoding="utf-8",
        )
        drifted = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            changed_lines((2, 2)),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        self.assertIn(
            "source_anchor_drift",
            {error["code"] for error in drifted["errors"]},
        )

    def test_uncovered_changed_branch_fails_closed(self) -> None:
        coverage = coverage_report()
        coverage["files"][SPEC.source_path]["executed_branches"] = [[2, 3]]
        coverage["files"][SPEC.source_path]["missing_branches"] = [[2, 5]]
        refresh_branch_summary(coverage["files"][SPEC.source_path])

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines((2, 2)),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("fail", result["status"])
        self.assertIn(
            "changed_branch_uncovered",
            {error["code"] for error in result["errors"]},
        )

    def test_changed_branch_outside_registered_anchor_uses_coverage_evidence(self) -> None:
        coverage = coverage_report()
        file_coverage = coverage["files"][SPEC.source_path]
        file_coverage["executed_lines"].extend([7, 8, 9, 10])
        file_coverage["executed_branches"].extend([[8, 9], [8, 10]])
        refresh_branch_summary(file_coverage)

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines((8, 8)),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])
        self.assertEqual([], result["errors"])
        self.assertEqual([], result["changed_branches"][0]["mutation_ids"])

    def test_production_adapter_branch_uses_direct_coverage_without_mutation(self) -> None:
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines((2, 2), path=ADAPTER_PATH),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertTrue(ACTIVE_M1_SAFETY_PATHS.governs(ADAPTER_PATH))
        self.assertEqual("pass", result["status"])
        self.assertEqual([], result["errors"])
        self.assertEqual([], result["changed_branches"][0]["mutation_ids"])

    def test_non_branch_changed_line_does_not_require_a_fake_mutation(self) -> None:
        old_source = EXAMPLE_SOURCE.replace("\n\ndef audit", "\ndef audit")
        artifact = changed_lines(old_source=old_source, new_source=EXAMPLE_SOURCE)
        artifact["files"][0]["hunks"] = [
            {
                "new_branch_refs": [],
                "new_count": 1,
                "new_first": 6,
                "old_branch_refs": [],
                "old_count": 0,
                "old_first": 5,
            }
        ]
        result = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])
        self.assertEqual([], result["changed_branches"])

    def test_changed_lines_artifact_requires_exact_base_provenance_and_shape(self) -> None:
        missing_base = changed_lines((2, 2))
        del missing_base["base_ref"]
        malformed_range = changed_lines((2, 2))
        malformed_range["files"][0]["hunks"][0]["new_count"] = -1
        unhashable_status = changed_lines((2, 2))
        unhashable_status["files"][0]["status"] = []

        first = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            missing_base,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        second = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            malformed_range,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        third = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            unhashable_status,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("fail", first["status"])
        self.assertIn(
            "changed_base_ref_missing",
            {error["code"] for error in first["errors"]},
        )
        self.assertEqual("fail", second["status"])
        self.assertIn(
            "changed_hunks_invalid",
            {error["code"] for error in second["errors"]},
        )
        self.assertIn(
            "changed_file_status_invalid",
            {error["code"] for error in third["errors"]},
        )

    def test_changed_path_is_normalized_before_branch_matching(self) -> None:
        artifact = changed_lines(
            (2, 2), path=r"wish_builder\kernel\example.py"
        )

        result = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])
        self.assertEqual(SPEC.source_path, result["changed_branches"][0]["path"])
        self.assertEqual(SPEC.source_path, result["changed_files"][0]["new_path"])

    def test_deleted_or_renamed_governed_source_fails_closed(self) -> None:
        deleted = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            changed_lines(status="D"),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        renamed_artifact = changed_lines(status="M")
        renamed_artifact["files"][0].update(
            {
                "new_path": "wish_builder/kernel/replacement.py",
                "status": "R",
            }
        )
        renamed = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            renamed_artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "governed_safety_source_deleted",
            {error["code"] for error in deleted["errors"]},
        )
        self.assertIn(
            "governed_safety_source_renamed",
            {error["code"] for error in renamed["errors"]},
        )

    def test_malformed_unified_diff_hunk_is_rejected(self) -> None:
        with self.assertRaises(ChangedLinesInputError) as raised:
            _parse_unified_added_ranges(b"@@ malformed @@\n")
        self.assertEqual("git_diff_invalid", raised.exception.code)

    def test_deletion_hunks_are_preserved_and_fail_closed(self) -> None:
        raw = (
            b"diff --git a/wish_builder/kernel/example.py "
            b"b/wish_builder/kernel/example.py\n"
            b"@@ -2,3 +2,0 @@\n"
        )
        hunks = _parse_unified_hunks(raw)
        self.assertEqual((2, 3, 2, 0), (
            hunks[0].old_first,
            hunks[0].old_count,
            hunks[0].new_first,
            hunks[0].new_count,
        ))
        artifact = changed_lines((2, 2))
        artifact["files"][0]["hunks"][0].update(
            {"new_branch_refs": [], "new_count": 0, "old_count": 3}
        )

        result = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("fail", result["status"])
        self.assertIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_modified_safety_source_without_hunks_fails_closed(self) -> None:
        artifact = changed_lines((2, 2))
        artifact["files"][0]["hunks"] = []

        result = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("fail", result["status"])
        self.assertIn(
            "changed_safety_change_unmapped",
            {error["code"] for error in result["errors"]},
        )

    def test_deletion_hunk_cannot_be_hidden_by_a_covered_branch(self) -> None:
        artifact = changed_lines((2, 2))
        artifact["files"][0]["hunks"].append(
            {
                "new_branch_refs": [],
                "new_count": 0,
                "new_first": 8,
                "old_branch_refs": list(
                    _projection_refs(_branch_projections(EXAMPLE_SOURCE), 8, 1)
                ),
                "old_count": 1,
                "old_first": 8,
            }
        )

        result = evaluate_safety_evidence(
            coverage_report(),
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual(1, result["changed_branch_count"])
        self.assertIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_multiline_condition_maps_back_to_its_branch_origin(self) -> None:
        adapter = self.root / ADAPTER_PATH
        multiline_source = (
            "def promote(allowed):\n"
            "    if (\n"
            "        allowed\n"
            "    ):\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        old_multiline_source = multiline_source.replace(
            "        allowed\n", "        allowed is True\n"
        )
        adapter.write_text(multiline_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 5], [2, 6]],
            executed_lines=[1, 2, 3, 4, 5, 6],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (3, 3),
                path=ADAPTER_PATH,
                old_source=old_multiline_source,
                new_source=multiline_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])
        self.assertEqual(2, result["changed_branches"][0]["line"])

    def test_replacement_without_current_branch_evidence_fails_closed(self) -> None:
        adapter = self.root / ADAPTER_PATH
        branchless_source = "def promote(allowed):\n    return bool(allowed)\n"
        adapter.write_text(branchless_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[],
            executed_lines=[1, 2],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (2, 2), path=ADAPTER_PATH, new_source=branchless_source
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("fail", result["status"])
        self.assertIn(
            "changed_safety_change_unmapped",
            {error["code"] for error in result["errors"]},
        )

    def test_replacement_cannot_borrow_evidence_from_an_outer_branch_body(self) -> None:
        adapter = self.root / ADAPTER_PATH
        adapter.write_text(
            "def promote(allowed):\n"
            "    if allowed:\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n",
            encoding="utf-8",
        )
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )
        artifact = changed_lines((3, 3), path=ADAPTER_PATH)
        artifact["files"][0]["hunks"][0].update(
            {"old_count": 2, "old_first": 3}
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        codes = {error["code"] for error in result["errors"]}
        self.assertIn("changed_safety_deletion_unproven", codes)
        self.assertIn("changed_safety_change_unmapped", codes)

    def test_changed_coverage_suppression_and_denominator_drift_fail_closed(self) -> None:
        adapter = self.root / ADAPTER_PATH
        excluded_source = (
            "def promote(allowed):\n"
            "    if allowed:  # pragma: no cover\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        adapter.write_text(excluded_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[],
            executed_lines=[1, 4],
            excluded_lines=[2, 3],
        )
        artifact = changed_lines(
            (2, 3),
            path=ADAPTER_PATH,
            status="A",
            new_source=excluded_source,
        )

        excluded = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        excluded_codes = {error["code"] for error in excluded["errors"]}
        self.assertIn("changed_branch_coverage_suppressed", excluded_codes)
        self.assertIn("changed_branch_coverage_missing", excluded_codes)

        suppressed_source = (
            "def promote(allowed):\n"
            "    if allowed:  # pragma: no branch\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        adapter.write_text(suppressed_source, encoding="utf-8")
        partial = coverage_file(
            executed_branches=[[2, 3]],
            executed_lines=[1, 2, 3],
        )
        partial["summary"].update(
            {"covered_branches": 2, "num_branches": 2}
        )
        coverage["files"][ADAPTER_PATH] = partial
        denominator = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (2, 2),
                path=ADAPTER_PATH,
                status="A",
                new_source=suppressed_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        self.assertIn(
            "changed_branch_coverage_invalid",
            {error["code"] for error in denominator["errors"]},
        )

        refresh_branch_summary(partial)
        synchronized = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (2, 2),
                path=ADAPTER_PATH,
                status="A",
                new_source=suppressed_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )
        self.assertIn(
            "changed_branch_coverage_suppressed",
            {error["code"] for error in synchronized["errors"]},
        )

    def test_multiline_match_guard_maps_to_case_branch_origin(self) -> None:
        adapter = self.root / ADAPTER_PATH
        match_source = (
            "def promote(value, extra):\n"
            "    match value:\n"
            "        case (\n"
            "            'go'\n"
            "        ) if (\n"
            "            extra\n"
            "        ):\n"
            "            return 'promoted'\n"
            "        case _:\n"
            "            return 'blocked'\n"
        )
        adapter.write_text(match_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[3, 8]],
            executed_lines=list(range(1, 11)),
            missing_branches=[[3, 9]],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (6, 6),
                path=ADAPTER_PATH,
                status="A",
                new_source=match_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_branch_uncovered",
            {error["code"] for error in result["errors"]},
        )
        self.assertEqual(3, result["changed_branches"][0]["line"])

    def test_changed_branch_cannot_forge_a_one_arc_denominator(self) -> None:
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3]],
            executed_lines=[1, 2, 3],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines((2, 2), path=ADAPTER_PATH),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_branch_coverage_invalid",
            {error["code"] for error in result["errors"]},
        )

    def test_phantom_and_out_of_range_coverage_arcs_fail_closed(self) -> None:
        old_signature = ADAPTER_SOURCE
        new_signature = old_signature.replace(
            "def promote(allowed):", "def promote(allowed, audit=False):"
        )
        old_condition = ADAPTER_SOURCE
        new_condition = old_condition.replace(
            "if allowed:", "if allowed is True:"
        )
        cases = (
            (
                old_signature,
                new_signature,
                (1, 1),
                [[1, 2], [1, 3]],
                [1, 2, 3, 4],
            ),
            (
                old_condition,
                new_condition,
                (2, 2),
                [[2, 999], [2, 1000]],
                [1, 2, 3, 4],
            ),
            (
                old_condition,
                new_condition,
                (2, 2),
                [[2, 1], [2, 3]],
                [1, 2, 3, 4],
            ),
        )
        for old_source, new_source, changed_range, arcs, lines in cases:
            with self.subTest(arcs=arcs):
                (self.root / ADAPTER_PATH).write_text(
                    new_source, encoding="utf-8"
                )
                coverage = coverage_report()
                coverage["files"][ADAPTER_PATH] = coverage_file(
                    executed_branches=arcs,
                    executed_lines=lines,
                )
                result = evaluate_safety_evidence(
                    coverage,
                    mutation_report(),
                    changed_lines(
                        changed_range,
                        path=ADAPTER_PATH,
                        old_source=old_source,
                        new_source=new_source,
                    ),
                    source_root=self.root,
                    specs=(SPEC,),
                    path_registry=self.registry,
                )
                self.assertIn(
                    "changed_branch_coverage_invalid",
                    {error["code"] for error in result["errors"]},
                )

    def test_same_span_unobservable_decision_uses_line_evidence(self) -> None:
        old_source = (
            "def promote(allowed, ready):\n"
            "    if allowed:\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        new_source = (
            "def promote(allowed, ready):\n"
            "    if allowed and ready:\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (1, 2),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=new_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])
        self.assertEqual([], result["errors"])
        self.assertEqual(2, result["changed_branch_count"])
        line_evidence = [
            item
            for item in result["changed_branches"]
            if item.get("evidence_kind") == "executed_line"
        ]
        self.assertEqual(["BoolOpSlot"], [item["kind"] for item in line_evidence])
        self.assertEqual([[2]], [item["executed_lines"] for item in line_evidence])

    def test_unobservable_decision_without_line_execution_fails_closed(self) -> None:
        source = (
            "def promote(allowed):\n"
            "    return 'promoted' if allowed else 'blocked'\n"
        )
        (self.root / ADAPTER_PATH).write_text(source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[],
            executed_lines=[1],
            missing_lines=[2],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (2, 2),
                path=ADAPTER_PATH,
                status="A",
                new_source=source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_branch_line_uncovered",
            {error["code"] for error in result["errors"]},
        )

    def test_multiline_unobservable_decision_uses_statement_line_evidence(
        self,
    ) -> None:
        source = (
            "def promote(allowed):\n"
            "    return (\n"
            "        'promoted'\n"
            "        if allowed\n"
            "        else 'blocked'\n"
            "    )\n"
        )
        old_source = source.replace("if allowed", "if bool(allowed)")
        (self.root / ADAPTER_PATH).write_text(source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[],
            executed_lines=[1, 2],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (4, 4),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])
        self.assertEqual([], result["errors"])
        self.assertEqual(
            [[2]],
            [
                item["executed_lines"]
                for item in result["changed_branches"]
                if item.get("evidence_kind") == "executed_line"
            ],
        )

    def test_constant_loop_and_finally_branch_use_line_evidence(self) -> None:
        cases = (
            (
                "def poll():\n"
                "    while True:\n"
                "        return 'done'\n",
                [1, 2, 3],
                1,
            ),
            (
                "def close(resource):\n"
                "    try:\n"
                "        use(resource)\n"
                "    finally:\n"
                "        if resource:\n"
                "            resource.close()\n",
                [1, 3, 5, 6],
                2,
            ),
        )
        for source, executed_lines, expected_count in cases:
            with self.subTest(source=source):
                (self.root / ADAPTER_PATH).write_text(source, encoding="utf-8")
                coverage = coverage_report()
                coverage["files"][ADAPTER_PATH] = coverage_file(
                    executed_branches=[],
                    executed_lines=executed_lines,
                )

                result = evaluate_safety_evidence(
                    coverage,
                    mutation_report(),
                    changed_lines(
                        (1, len(source.splitlines())),
                        path=ADAPTER_PATH,
                        status="A",
                        new_source=source,
                    ),
                    source_root=self.root,
                    specs=(SPEC,),
                    path_registry=self.registry,
                )

                self.assertEqual([], result["errors"])
                self.assertEqual("pass", result["status"])
                self.assertEqual(expected_count, result["changed_branch_count"])
                self.assertTrue(
                    all(
                        item.get("evidence_kind") == "executed_line"
                        for item in result["changed_branches"]
                    )
                )

    def test_protocol_ellipsis_exclusions_are_structural_not_suppression(self) -> None:
        source = (
            "from typing import Protocol\n"
            "class Port(Protocol):\n"
            "    def close(self) -> None: ...\n"
        )
        (self.root / ADAPTER_PATH).write_text(source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[],
            executed_lines=[1, 2],
            excluded_lines=[3],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (1, 3),
                path=ADAPTER_PATH,
                status="A",
                new_source=source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual([], result["errors"])
        self.assertEqual("pass", result["status"])

    def test_line_evidence_and_protocol_exclusion_helper_boundaries(self) -> None:
        expression = BranchProjection(
            "branch-expression",
            "IfExp",
            4,
            4,
            "module.body[0].decorator_list[0]",
            "body",
            "decision",
            "module",
        )
        handler = BranchProjection(
            "branch-handler",
            "ExceptHandler",
            7,
            9,
            "module.body[0].handlers[0]",
            "body",
            "decision",
            "module/function:run",
        )

        self.assertEqual((4, 4), _line_evidence_span(expression, ()))
        self.assertEqual((7, 9), _line_evidence_span(handler, ((1, 12),)))

        source = (
            "from typing import Protocol\n"
            "class Port(Protocol):\n"
            "    name: str\n"
            "    def close(self) -> None:\n"
            "        return None\n"
            "    async def wait(self) -> None: ...\n"
        )
        allowed = _allowed_static_exclusion_lines(
            ast.parse(source),
            source.splitlines(),
        )

        self.assertNotIn(3, allowed)
        self.assertNotIn(4, allowed)
        self.assertNotIn(5, allowed)
        self.assertIn(6, allowed)

    def test_source_arc_universe_models_decorators_and_finally(self) -> None:
        source = (
            "@decorator\n"
            "def run(flag, recovery):\n"
            "    try:\n"
            "        fail()\n"
            "    except OSError:\n"
            "        if flag:\n"
            "            recover()\n"
            "    finally:\n"
            "        cleanup()\n"
            "    if recovery:\n"
            "        recover()\n"
        )

        universe = _source_branch_arc_universe(source)

        self.assertEqual(frozenset({7, 9}), universe[6])
        self.assertEqual(frozenset({-1, 11}), universe[10])

    def test_snapshot_hunks_cannot_hide_the_actual_source_diff(self) -> None:
        old_source = (
            "def promote(allowed, ready):\n"
            "    if allowed:\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        new_source = old_source.replace("if allowed:", "if allowed and ready:")
        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        artifact = changed_lines(
            path=ADAPTER_PATH,
            old_source=old_source,
            new_source=new_source,
        )
        artifact["files"][0]["hunks"] = [
            {
                "new_branch_refs": [],
                "new_count": 1,
                "new_first": 1,
                "old_branch_refs": [],
                "old_count": 0,
                "old_first": 1,
            }
        ]
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_hunks_snapshot_mismatch",
            {error["code"] for error in result["errors"]},
        )

    def test_equal_count_git_replacement_cannot_delete_an_inner_guard(self) -> None:
        repository = self.root / "git-equal-count-fixture"
        repository.mkdir()

        def git(*arguments: str) -> str:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            return completed.stdout.strip()

        git("init", "--quiet")
        git("config", "user.name", "Safety Evidence Test")
        git("config", "user.email", "safety-evidence@example.invalid")
        invariant_source = repository / SPEC.source_path
        invariant_source.parent.mkdir(parents=True)
        invariant_source.write_text(EXAMPLE_SOURCE, encoding="utf-8")
        adapter = repository / ADAPTER_PATH
        adapter.parent.mkdir(parents=True)
        adapter.write_text(
            "def promote(outer, allowed, ready):\n"
            "    if outer:\n"
            "        if allowed:\n"
            "            promote_now()\n"
            "    return 'blocked'\n",
            encoding="utf-8",
        )
        git("add", SPEC.source_path, ADAPTER_PATH)
        git("commit", "--quiet", "-m", "base")
        base = git("rev-parse", "HEAD")
        adapter.write_text(
            "def promote(outer, allowed, ready):\n"
            "    if outer and ready:\n"
            "        promote_now()\n"
            "        audit()\n"
            "    return 'blocked'\n",
            encoding="utf-8",
        )
        git("add", ADAPTER_PATH)
        git("commit", "--quiet", "-m", "remove inner guard")

        artifact = collect_changed_lines(
            repository,
            base,
            governed_paths=(ADAPTER_PATH,),
        )
        hunk = artifact["files"][0]["hunks"][0]
        self.assertEqual((3, 3), (hunk["old_count"], hunk["new_count"]))
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 5]],
            executed_lines=[1, 2, 3, 4, 5],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            artifact,
            source_root=repository,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("fail", result["status"])
        self.assertIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_cross_function_branch_relocation_cannot_prove_a_deleted_guard(
        self,
    ) -> None:
        repository = self.root / "git-cross-function-fixture"
        repository.mkdir()

        def git(*arguments: str) -> str:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            return completed.stdout.strip()

        old_source = (
            "def promote(allowed):\n"
            "    if allowed:\n"
            "        promote_now()\n"
            "    return 'blocked'\n"
            "\n"
            "def audit(flag):\n"
            "    return 'done'\n"
        )
        new_source = (
            "def promote(allowed):\n"
            "    return 'blocked'\n"
            "\n"
            "def audit(flag):\n"
            "    if flag:\n"
            "        promote_now()\n"
            "    return 'done'\n"
        )
        git("init", "--quiet")
        git("config", "user.name", "Safety Evidence Test")
        git("config", "user.email", "safety-evidence@example.invalid")
        invariant_source = repository / SPEC.source_path
        invariant_source.parent.mkdir(parents=True)
        invariant_source.write_text(EXAMPLE_SOURCE, encoding="utf-8")
        adapter = repository / ADAPTER_PATH
        adapter.parent.mkdir(parents=True)
        adapter.write_text(old_source, encoding="utf-8")
        git("add", SPEC.source_path, ADAPTER_PATH)
        git("commit", "--quiet", "-m", "base")
        base = git("rev-parse", "HEAD")
        adapter.write_text(new_source, encoding="utf-8")
        git("add", ADAPTER_PATH)
        git("commit", "--quiet", "-m", "move branch across functions")

        artifact = collect_changed_lines(
            repository,
            base,
            governed_paths=(ADAPTER_PATH,),
        )
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[5, 6], [5, 7]],
            executed_lines=[1, 2, 4, 5, 6, 7],
        )
        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            artifact,
            source_root=repository,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_same_ast_slot_in_a_different_owner_is_not_a_correspondence(
        self,
    ) -> None:
        old_source = (
            "def promote(allowed):\n"
            "    if allowed:\n"
            "        promote_now()\n"
            "    return 'blocked'\n"
        )
        new_source = old_source.replace("def promote", "def audit")
        old_branch = _branch_projections(old_source)[0]
        new_branch = _branch_projections(new_source)[0]
        self.assertEqual(old_branch.ast_path, new_branch.ast_path)
        self.assertNotEqual(old_branch.owner_key, new_branch.owner_key)
        self.assertNotEqual(old_branch.structural_key, new_branch.structural_key)

        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )
        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (1, 1),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=new_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_one_to_one_condition_change_keeps_its_branch_correspondence(self) -> None:
        old_source = ADAPTER_SOURCE
        new_source = ADAPTER_SOURCE.replace("if allowed:", "if allowed is True:")
        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (2, 2),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=new_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])

    def test_nested_condition_change_does_not_invalidate_its_outer_branch(self) -> None:
        old_source = (
            "def promote(outer, allowed):\n"
            "    if outer:\n"
            "        if allowed:\n"
            "            return 'promoted'\n"
            "    return 'blocked'\n"
        )
        new_source = old_source.replace("if allowed:", "if allowed is True:")
        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 5], [3, 4], [3, 5]],
            executed_lines=[1, 2, 3, 4, 5],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (3, 3),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=new_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])

    def test_control_flow_inventory_preserves_same_span_multiplicity(self) -> None:
        source = (
            "def promote(allowed, ready):\n"
            "    if allowed and ready:\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        projections = _branch_projections(source)

        self.assertEqual(2, len(projections))
        self.assertEqual(1, len({item.owner_key for item in projections}))
        self.assertTrue(projections[0].owner_key.startswith("module/function:promote:"))
        self.assertEqual(
            [(2, 2), (2, 2)],
            [(item.first_line, item.last_line) for item in projections],
        )
        self.assertEqual(2, len({item.branch_id for item in projections}))

    def test_control_flow_inventory_counts_each_boolean_short_circuit(self) -> None:
        old_source = (
            "def promote(allowed, ready, healthy):\n"
            "    if allowed and ready and healthy:\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        new_source = old_source.replace(
            "allowed and ready and healthy", "allowed and ready"
        )
        old_projections = _branch_projections(old_source)
        new_projections = _branch_projections(new_source)

        self.assertEqual(
            ["BoolOpSlot", "BoolOpSlot", "If"],
            sorted(item.kind for item in old_projections),
        )
        self.assertEqual(
            ["BoolOpSlot", "If"],
            sorted(item.kind for item in new_projections),
        )

        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )
        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (2, 2),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=new_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_match_guard_and_comprehension_filter_removal_fail_closed(self) -> None:
        match_old = (
            "def promote(value, ready):\n"
            "    match value:\n"
            "        case 'go' if ready:\n"
            "            return 'promoted'\n"
            "        case _:\n"
            "            return 'blocked'\n"
        )
        match_new = match_old.replace("case 'go' if ready:", "case 'go':")
        comprehension_old = (
            "def promote(values):\n"
            "    return [value for value in values if value if value > 1]\n"
        )
        comprehension_new = comprehension_old.replace(
            " if value if value > 1", " if value"
        )

        for old_source, new_source, changed_range in (
            (match_old, match_new, (3, 3)),
            (comprehension_old, comprehension_new, (2, 2)),
        ):
            (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
            coverage = coverage_report()
            coverage["files"][ADAPTER_PATH] = coverage_file(
                executed_branches=[],
                executed_lines=list(range(1, len(new_source.splitlines()) + 1)),
            )
            result = evaluate_safety_evidence(
                coverage,
                mutation_report(),
                changed_lines(
                    changed_range,
                    path=ADAPTER_PATH,
                    old_source=old_source,
                    new_source=new_source,
                ),
                source_root=self.root,
                specs=(SPEC,),
                path_registry=self.registry,
            )

            self.assertIn(
                "changed_safety_deletion_unproven",
                {error["code"] for error in result["errors"]},
            )

    def test_decision_inventory_detects_short_circuit_filter_and_guard_removal(
        self,
    ) -> None:
        sources = (
            (
                "def promote(allowed, ready, audited):\n"
                "    if allowed and ready and audited:\n"
                "        return 'promoted'\n"
                "    return 'blocked'\n",
                "def promote(allowed, ready, audited):\n"
                "    if allowed and ready:\n"
                "        return 'promoted'\n"
                "    return 'blocked'\n",
                [[2, 3], [2, 4]],
                [1, 2, 3, 4],
                2,
            ),
            (
                "def promote(values):\n"
                "    return [value for value in values if value > 0 if value < 10]\n",
                "def promote(values):\n"
                "    return [value for value in values if value > 0]\n",
                [[2, -1], [2, 2]],
                [1, 2],
                2,
            ),
            (
                "def promote(value, allowed):\n"
                "    match value:\n"
                "        case 'go' if allowed:\n"
                "            return 'promoted'\n"
                "        case _:\n"
                "            return 'blocked'\n",
                "def promote(value, allowed):\n"
                "    match value:\n"
                "        case 'go':\n"
                "            return 'promoted'\n"
                "        case _:\n"
                "            return 'blocked'\n",
                [[3, 4], [3, 5]],
                [1, 2, 3, 4, 5, 6],
                3,
            ),
        )
        for old_source, new_source, branches, lines, changed_line in sources:
            with self.subTest(old_source=old_source):
                (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
                coverage = coverage_report()
                coverage["files"][ADAPTER_PATH] = coverage_file(
                    executed_branches=branches,
                    executed_lines=lines,
                )
                result = evaluate_safety_evidence(
                    coverage,
                    mutation_report(),
                    changed_lines(
                        (changed_line, changed_line),
                        path=ADAPTER_PATH,
                        old_source=old_source,
                        new_source=new_source,
                    ),
                    source_root=self.root,
                    specs=(SPEC,),
                    path_registry=self.registry,
                )
                self.assertIn(
                    "changed_safety_deletion_unproven",
                    {error["code"] for error in result["errors"]},
                )

    def test_unrelated_short_circuit_cannot_offset_a_removed_decision(self) -> None:
        old_source = (
            "def promote(allowed, ready, healthy, extra, audited):\n"
            "    if allowed and ready and healthy:\n"
            "        return 'promoted'\n"
            "    if extra:\n"
            "        audit()\n"
            "    return 'blocked'\n"
        )
        new_source = (
            "def promote(allowed, ready, healthy, extra, audited):\n"
            "    if allowed and ready:\n"
            "        return 'promoted'\n"
            "    if extra and audited:\n"
            "        audit()\n"
            "    return 'blocked'\n"
        )
        self.assertEqual(
            len(_branch_projections(old_source)),
            len(_branch_projections(new_source)),
        )
        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4], [4, 5], [4, 6]],
            executed_lines=[1, 2, 3, 4, 5, 6],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (2, 2),
                (4, 4),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=new_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_branch_body_edit_keeps_the_same_structural_correspondence(self) -> None:
        old_source = ADAPTER_SOURCE
        new_source = old_source.replace(
            "        return 'promoted'\n", "        return promote_now()\n"
        )
        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (3, 3),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=new_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertNotIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_nested_if_expression_inventory_reduction_fails_closed(self) -> None:
        old_source = (
            "def promote(first, second):\n"
            "    return 'a' if first else ('b' if second else 'blocked')\n"
        )
        new_source = (
            "def promote(first, second):\n"
            "    return 'a' if first else 'blocked'\n"
        )
        self.assertEqual(2, len(_branch_projections(old_source)))
        self.assertEqual(1, len(_branch_projections(new_source)))
        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[],
            executed_lines=[1, 2],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            changed_lines(
                (2, 2),
                path=ADAPTER_PATH,
                old_source=old_source,
                new_source=new_source,
            ),
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_safety_deletion_unproven",
            {error["code"] for error in result["errors"]},
        )

    def test_branch_move_across_hunks_keeps_file_level_correspondence(self) -> None:
        old_source = (
            "def promote(allowed):\n"
            "    if allowed:\n"
            "        return 'promoted'\n"
            "    marker_one()\n"
            "    marker_two()\n"
            "    return 'blocked'\n"
        )
        new_source = (
            "def promote(allowed):\n"
            "    marker_one()\n"
            "    marker_two()\n"
            "    if allowed:\n"
            "        return 'promoted'\n"
            "    return 'blocked'\n"
        )
        old_flow = _branch_projections(old_source)
        new_flow = _branch_projections(new_source)
        artifact = changed_lines(
            path=ADAPTER_PATH,
            old_source=old_source,
            new_source=new_source,
        )
        artifact["files"][0]["hunks"] = [
            {
                "new_branch_refs": [],
                "new_count": 0,
                "new_first": 1,
                "old_branch_refs": list(_projection_refs(old_flow, 2, 2)),
                "old_count": 2,
                "old_first": 2,
            },
            {
                "new_branch_refs": list(_projection_refs(new_flow, 4, 2)),
                "new_count": 2,
                "new_first": 4,
                "old_branch_refs": [],
                "old_count": 0,
                "old_first": 5,
            },
        ]
        (self.root / ADAPTER_PATH).write_text(new_source, encoding="utf-8")
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[4, 5], [4, 6]],
            executed_lines=[1, 2, 3, 4, 5, 6],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertEqual("pass", result["status"])

    def test_changed_lines_rejects_snapshot_projection_and_ref_tampering(self) -> None:
        projection_tamper = changed_lines((2, 2))
        projection_tamper["files"][0]["old_control_flow"].pop()
        ref_tamper = changed_lines((2, 2))
        ref_tamper["files"][0]["hunks"][0]["new_branch_refs"] = []
        digest_tamper = changed_lines((2, 2))
        digest_tamper["files"][0]["new_source_sha256"] = "sha256:" + "0" * 64

        results = [
            evaluate_safety_evidence(
                coverage_report(),
                mutation_report(),
                artifact,
                source_root=self.root,
                specs=(SPEC,),
                path_registry=self.registry,
            )
            for artifact in (projection_tamper, ref_tamper, digest_tamper)
        ]

        self.assertIn(
            "changed_control_flow_invalid",
            {error["code"] for error in results[0]["errors"]},
        )
        self.assertIn(
            "changed_hunks_invalid",
            {error["code"] for error in results[1]["errors"]},
        )
        self.assertIn(
            "changed_file_snapshot_digest_mismatch",
            {error["code"] for error in results[2]["errors"]},
        )

    def test_evaluator_rejects_current_source_drift_after_snapshot(self) -> None:
        artifact = changed_lines((2, 2), path=ADAPTER_PATH)
        (self.root / ADAPTER_PATH).write_text(
            ADAPTER_SOURCE.replace("if allowed:", "if allowed is True:"),
            encoding="utf-8",
        )
        coverage = coverage_report()
        coverage["files"][ADAPTER_PATH] = coverage_file(
            executed_branches=[[2, 3], [2, 4]],
            executed_lines=[1, 2, 3, 4],
        )

        result = evaluate_safety_evidence(
            coverage,
            mutation_report(),
            artifact,
            source_root=self.root,
            specs=(SPEC,),
            path_registry=self.registry,
        )

        self.assertIn(
            "changed_safety_source_mismatch",
            {error["code"] for error in result["errors"]},
        )

    def test_git_collector_uses_exact_merge_base_added_ranges(self) -> None:
        repository = self.root / "git-fixture"
        repository.mkdir()

        def git(*arguments: str) -> str:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            return completed.stdout.strip()

        git("init", "--quiet")
        git("config", "user.name", "Safety Evidence Test")
        git("config", "user.email", "safety-evidence@example.invalid")
        source = repository / SPEC.source_path
        source.parent.mkdir(parents=True)
        source.write_text(
            "def decide(allowed):\n"
            "    if allowed:\n"
            "        return True\n"
            "    return False\n",
            encoding="utf-8",
        )
        git("add", SPEC.source_path)
        git("commit", "--quiet", "-m", "base")
        base = git("rev-parse", "HEAD")
        source.write_text(
            "def decide(allowed):\n"
            "    if allowed is True:\n"
            "        return True\n"
            "    return False\n",
            encoding="utf-8",
        )
        git("add", SPEC.source_path)
        git("commit", "--quiet", "-m", "change branch")

        artifact = collect_changed_lines(
            repository,
            base,
            governed_paths=(SPEC.source_path,),
        )

        self.assertEqual(base, artifact["merge_base"])
        self.assertEqual(
            [
                {
                    "new_branch_refs": ["branch-000000"],
                    "new_count": 1,
                    "new_first": 2,
                    "old_branch_refs": ["branch-000000"],
                    "old_count": 1,
                    "old_first": 2,
                }
            ],
            artifact["files"][0]["hunks"],
        )
        self.assertEqual("M", artifact["files"][0]["status"])
        self.assertEqual(CHANGED_LINES_SCHEMA_VERSION, artifact["schema_version"])
        self.assertEqual(
            _source_sha256(artifact["files"][0]["old_source"]),
            artifact["files"][0]["old_source_sha256"],
        )
        self.assertEqual(
            _git_blob_oid(artifact["files"][0]["new_source"]),
            artifact["files"][0]["new_blob_oid"],
        )
        with self.assertRaises(ChangedLinesInputError) as raised:
            collect_changed_lines(
                repository,
                "refs/heads/does-not-exist",
                governed_paths=(SPEC.source_path,),
            )
        self.assertEqual("base_ref_unavailable", raised.exception.code)

        adapter_base = git("rev-parse", "HEAD")
        direct_adapter = repository / ADAPTER_PATH
        direct_adapter.parent.mkdir(parents=True, exist_ok=True)
        direct_adapter.write_text("DIRECT = True\n", encoding="utf-8")
        nested_path = "wish_builder/adapters/storage/filesystem.py"
        nested_adapter = repository / nested_path
        nested_adapter.parent.mkdir(parents=True, exist_ok=True)
        nested_adapter.write_text("NESTED = True\n", encoding="utf-8")
        git("add", ADAPTER_PATH, nested_path)
        git("commit", "--quiet", "-m", "add adapters")

        adapter_artifact = collect_changed_lines(
            repository,
            adapter_base,
            governed_paths=ACTIVE_M1_SAFETY_PATHS.git_pathspecs,
        )

        self.assertEqual(
            [ADAPTER_PATH, nested_path],
            [item["new_path"] for item in adapter_artifact["files"]],
        )

    def test_git_collector_rejects_an_ambiguous_merge_base(self) -> None:
        first = "c" * 40
        second = "d" * 40
        with patch(
            "scripts.ci_safety_evidence._resolve_commit",
            side_effect=("a" * 40, "b" * 40),
        ), patch(
            "scripts.ci_safety_evidence._git_output",
            return_value=f"{first}\n{second}\n".encode("ascii"),
        ) as git_output:
            with self.assertRaises(ChangedLinesInputError) as raised:
                collect_changed_lines(
                    self.root,
                    "refs/heads/main",
                    governed_paths=(SPEC.source_path,),
                )

        self.assertEqual("merge_base_unavailable", raised.exception.code)
        self.assertEqual(
            ("merge-base", "--all", "a" * 40, "b" * 40),
            git_output.call_args.args[1],
        )

    def test_git_collector_retains_a_guard_deletion(self) -> None:
        repository = self.root / "git-deletion-fixture"
        repository.mkdir()

        def git(*arguments: str) -> str:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            return completed.stdout.strip()

        git("init", "--quiet")
        git("config", "user.name", "Safety Evidence Test")
        git("config", "user.email", "safety-evidence@example.invalid")
        source = repository / SPEC.source_path
        source.parent.mkdir(parents=True)
        source.write_text(
            "def decide(allowed):\n"
            "    if allowed:\n"
            "        return True\n"
            "    return False\n",
            encoding="utf-8",
        )
        git("add", SPEC.source_path)
        git("commit", "--quiet", "-m", "base")
        base = git("rev-parse", "HEAD")
        source.write_text(
            "def decide(allowed):\n    return False\n",
            encoding="utf-8",
        )
        git("add", SPEC.source_path)
        git("commit", "--quiet", "-m", "remove guard")

        artifact = collect_changed_lines(
            repository,
            base,
            governed_paths=(SPEC.source_path,),
        )

        self.assertEqual("M", artifact["files"][0]["status"])
        self.assertTrue(
            any(
                hunk["old_count"] > 0 and hunk["new_count"] == 0
                for hunk in artifact["files"][0]["hunks"]
            )
        )

    def test_git_collector_treats_each_changed_filename_as_literal(self) -> None:
        repository = self.root / "git-literal-path-fixture"
        repository.mkdir()

        def git(*arguments: str) -> str:
            completed = subprocess.run(
                ["git", *arguments],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            return completed.stdout.strip()

        git("init", "--quiet")
        git("config", "user.name", "Safety Evidence Test")
        git("config", "user.email", "safety-evidence@example.invalid")
        plain_path = "wish_builder/adapters/x1.py"
        special_path = "wish_builder/adapters/x[1].py"
        for path in (plain_path, special_path):
            source = repository / path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                "".join(f"VALUE_{line} = {line}\n" for line in range(1, 11)),
                encoding="utf-8",
            )
        git("add", plain_path, special_path)
        git("commit", "--quiet", "-m", "base")
        base = git("rev-parse", "HEAD")
        (repository / plain_path).write_text(
            "VALUE_1 = 100\n"
            + "".join(f"VALUE_{line} = {line}\n" for line in range(2, 11)),
            encoding="utf-8",
        )
        (repository / special_path).write_text(
            "".join(f"VALUE_{line} = {line}\n" for line in range(1, 10))
            + "VALUE_10 = 1000\n",
            encoding="utf-8",
        )
        git("add", plain_path, special_path)
        git("commit", "--quiet", "-m", "change both")

        artifact = collect_changed_lines(
            repository,
            base,
            governed_paths=(plain_path, special_path),
        )
        by_path = {item["new_path"]: item for item in artifact["files"]}

        self.assertEqual(1, by_path[plain_path]["hunks"][0]["new_first"])
        self.assertEqual(10, by_path[special_path]["hunks"][0]["new_first"])
        self.assertEqual(1, len(by_path[special_path]["hunks"]))

    def test_fake_adapters_are_not_changed_safety_authorities(self) -> None:
        self.assertFalse(
            ACTIVE_M1_SAFETY_PATHS.governs(
                "wish_builder/adapters/fake/effects.py"
            )
        )
        self.assertFalse(
            ACTIVE_M1_SAFETY_PATHS.governs(
                "wish_builder/adapters/fakes.py"
            )
        )
        self.assertIn(
            ":(exclude,glob)wish_builder/adapters/fake/**/*.py",
            ACTIVE_M1_SAFETY_PATHS.git_pathspecs,
        )


if __name__ == "__main__":
    unittest.main()
