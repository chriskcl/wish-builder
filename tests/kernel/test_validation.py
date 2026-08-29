from __future__ import annotations

import copy
import hashlib
import itertools
import json
import random
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wish_builder.contracts import (
    ReasonCode,
    Severity,
    ValidationIssue,
    ValidationPhase,
    ValidationReport,
    ValidationStage,
    decode_manifest_primitive,
)
from wish_builder.kernel.validation import (
    _analyze_ownership_scope,
    _ownership_summaries_overlap,
    _patterns_overlap,
    _precompute_reachability,
    _summarize_ownership,
    admit_manifest_bytes,
    diagnostics_bytes,
    diagnostics_sha256,
    validate_manifest,
    validate_manifest_bytes,
    validate_manifest_shape,
)


def _task(
    task_id: str,
    requirement_id: str,
    *,
    depends_on: list[str],
    owned_path: str,
    wave: int,
    issue_id: int,
    status: str = "approved",
    pr_id: int | None = None,
    squash_commit: str | None = None,
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": task_id.replace("-", " "),
        "requirement_ids": [requirement_id],
        "depends_on": depends_on,
        "owned_paths": [owned_path],
        "allowed_auxiliary_paths": [f".trellis/tasks/{task_id.lower()}/**"],
        "acceptance_criteria": [f"{task_id} passes"],
        "regression_commands": [f"python -m unittest {task_id}"],
        "rollback": "Revert the squash commit",
        "documentation": [],
        "wave": wave,
        "risk": "medium",
        "may_change_contracts": wave == 0,
        "issue_id": issue_id,
        "branch": f"feat/{issue_id}-{task_id.lower()}",
        "pr_id": pr_id,
        "squash_commit": squash_commit,
        "agent_owner": None,
        "status": status,
    }


def valid_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "WISH-001",
        "goal": "Ship the active M1 slice",
        "base_branch": "main",
        "max_concurrency": 2,
        "protected_paths": ["wish_builder/contracts/**"],
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
            {"id": "REQ-001", "text": "Foundation", "status": "implemented"},
            {"id": "REQ-002", "text": "Feature A", "status": "approved"},
            {"id": "REQ-003", "text": "Feature B", "status": "approved"},
            {"id": "REQ-004", "text": "Integration", "status": "approved"},
        ],
        "tasks": [
            _task(
                "TASK-001",
                "REQ-001",
                depends_on=[],
                owned_path="wish_builder/contracts/**",
                wave=0,
                issue_id=1,
                status="merged",
                pr_id=11,
                squash_commit="abc123",
            ),
            _task(
                "TASK-002",
                "REQ-002",
                depends_on=["TASK-001"],
                owned_path="src/a/**",
                wave=1,
                issue_id=2,
            ),
            _task(
                "TASK-003",
                "REQ-003",
                depends_on=["TASK-001"],
                owned_path="src/b/**",
                wave=1,
                issue_id=3,
            ),
            _task(
                "TASK-004",
                "REQ-004",
                depends_on=["TASK-002", "TASK-003"],
                owned_path="tests/e2e/**",
                wave=2,
                issue_id=4,
            ),
        ],
    }


def _raw(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _shuffle_object_keys(value: object, randomizer: random.Random) -> object:
    if type(value) is dict:
        keys = list(value)
        randomizer.shuffle(keys)
        return {
            key: _shuffle_object_keys(value[key], randomizer)
            for key in keys
        }
    if type(value) is list:
        return [_shuffle_object_keys(item, randomizer) for item in value]
    return value


class KernelValidationTests(unittest.TestCase):
    def test_valid_execution_model_has_no_findings(self) -> None:
        decoded = decode_manifest_primitive(valid_manifest())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        report = validate_manifest(decoded.value, ValidationPhase.EXECUTION)
        self.assertTrue(report.ok, report.render_text())

    def test_core_rejects_raw_mappings_at_the_trust_boundary(self) -> None:
        with self.assertRaises(TypeError):
            validate_manifest(valid_manifest())  # type: ignore[arg-type]

    def test_referential_and_cycle_rules_are_typed(self) -> None:
        value = valid_manifest()
        value["tasks"][0]["depends_on"] = ["TASK-004"]
        value["tasks"][1]["requirement_ids"] = ["REQ-999"]
        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())

        report = validate_manifest(decoded.value)
        rules = {issue.rule_id for issue in report.issues}
        self.assertIn("manifest.dependency_cycle", rules)
        self.assertIn("manifest.requirement_reference", rules)

    def test_unrelated_unknown_dependency_does_not_hide_wave_barrier(self) -> None:
        value = valid_manifest()
        value["tasks"][1]["depends_on"] = ["TASK-999"]
        value["tasks"][3]["depends_on"] = []
        report = validate_manifest_shape(value)
        rules_by_path = {(issue.rule_id, issue.path) for issue in report.issues}
        self.assertIn(
            (
                "manifest.dependency_reference",
                ("tasks", "TASK-002", "depends_on", "TASK-999"),
            ),
            rules_by_path,
        )
        self.assertIn(
            ("manifest.wave_barrier", ("tasks", "TASK-004", "depends_on")),
            rules_by_path,
        )
        self.assertNotIn(
            ("manifest.wave_barrier", ("tasks", "TASK-002", "depends_on")),
            rules_by_path,
        )

    def test_changed_safety_rules_have_direct_evidence(self) -> None:
        cases = (
            (
                "unknown dependency",
                lambda value: value["tasks"][1].update(depends_on=["TASK-999"]),
                ValidationPhase.PLANNING,
                "manifest.dependency_reference",
                ReasonCode.UNKNOWN_DEPENDENCY,
            ),
            (
                "later-wave dependency",
                lambda value: value["tasks"][0].update(depends_on=["TASK-002"]),
                ValidationPhase.PLANNING,
                "manifest.later_wave_dependency",
                ReasonCode.LATER_WAVE_DEPENDENCY,
            ),
            (
                "contract change outside wave zero",
                lambda value: value["tasks"][1].update(may_change_contracts=True),
                ValidationPhase.PLANNING,
                "manifest.contract_change_wave",
                ReasonCode.CONTRACT_CHANGE_OUTSIDE_WAVE_ZERO,
            ),
            (
                "shared issue",
                lambda value: value["tasks"][2].update(issue_id=2),
                ValidationPhase.EXECUTION,
                "manifest.unique_issue",
                ReasonCode.SHARED_ISSUE_IDENTITY,
            ),
            (
                "shared branch",
                lambda value: value["tasks"][2].update(branch="feat/2-task-002"),
                ValidationPhase.EXECUTION,
                "manifest.unique_branch",
                ReasonCode.SHARED_BRANCH_IDENTITY,
            ),
            (
                "shared PR",
                lambda value: (
                    value["tasks"][1].update(status="pr_open", pr_id=22),
                    value["tasks"][2].update(status="pr_open", pr_id=22),
                ),
                ValidationPhase.EXECUTION,
                "manifest.unique_pr",
                ReasonCode.SHARED_PR_IDENTITY,
            ),
            (
                "missing issue",
                lambda value: value["tasks"][1].update(issue_id=None),
                ValidationPhase.EXECUTION,
                "manifest.execution_issue",
                ReasonCode.MISSING_EXECUTION_IDENTITY,
            ),
            (
                "missing branch",
                lambda value: value["tasks"][1].update(branch=None),
                ValidationPhase.EXECUTION,
                "manifest.execution_branch",
                ReasonCode.MISSING_EXECUTION_IDENTITY,
            ),
            (
                "missing Gate A",
                lambda value: value["approved"].pop("gate_a"),
                ValidationPhase.PLANNING,
                "manifest.gate_a_approval",
                ReasonCode.GATE_APPROVAL_MISSING,
            ),
            (
                "missing Gate B",
                lambda value: value["approved"].pop("gate_b"),
                ValidationPhase.EXECUTION,
                "manifest.gate_b_approval",
                ReasonCode.GATE_APPROVAL_MISSING,
            ),
        )
        for name, mutate, phase, expected_rule, expected_reason in cases:
            with self.subTest(name=name):
                value = valid_manifest()
                mutate(value)
                report = validate_manifest_shape(value, phase)
                matching = [
                    issue
                    for issue in report.issues
                    if issue.rule_id == expected_rule
                    and issue.reason_code is expected_reason
                ]
                self.assertTrue(matching, report.render_text())

    def test_parallel_ownership_rule_is_typed(self) -> None:
        value = valid_manifest()
        value["tasks"][2]["owned_paths"] = ["SRC/A/components/**"]
        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())

        report = validate_manifest(decoded.value)
        rules = {issue.rule_id for issue in report.issues}
        self.assertIn("manifest.parallel_ownership", rules)

    def test_parallel_ownership_rejects_intersecting_glob_languages(self) -> None:
        value = valid_manifest()
        value["tasks"][1]["owned_paths"] = ["src/ab*cd"]
        value["tasks"][2]["owned_paths"] = ["src/abef*"]
        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())

        report = validate_manifest(decoded.value)
        self.assertIn(
            "manifest.parallel_ownership",
            {issue.rule_id for issue in report.issues},
        )

    def test_parallel_ownership_allows_provably_disjoint_literal_prefixes(self) -> None:
        cases = (
            ("src/a/**", "src/ab/**"),
            ("src/x/../a/**", "src/b/**"),
            (r"src\a\**", "src/b/**"),
        )
        for left_path, right_path in cases:
            with self.subTest(left_path=left_path, right_path=right_path):
                value = valid_manifest()
                value["tasks"][1]["owned_paths"] = [left_path]
                value["tasks"][2]["owned_paths"] = [right_path]
                report = validate_manifest_shape(value)
                self.assertNotIn(
                    "manifest.parallel_ownership",
                    {issue.rule_id for issue in report.issues},
                )

    def test_parallel_ownership_normalizes_equivalent_scope_spellings(self) -> None:
        equivalent = ("src/a/**", "src//a/**", r"src\a\**", "src/x/../a/**")
        for spelling in equivalent:
            with self.subTest(spelling=spelling):
                value = valid_manifest()
                value["tasks"][1]["owned_paths"] = [spelling]
                value["tasks"][2]["owned_paths"] = ["src/a/**"]
                report = validate_manifest_shape(value)
                self.assertIn(
                    "manifest.parallel_ownership",
                    {issue.rule_id for issue in report.issues},
                )

    def test_parallel_ownership_denies_unsafe_parent_traversal(self) -> None:
        for spelling in (
            "../src/a/**",
            "src/../../a/**",
            "src/*/../a/**",
            r"C:\outside\a\**",
            r"\\server\share\**",
            ".",
            "///",
        ):
            with self.subTest(spelling=spelling):
                value = valid_manifest()
                value["tasks"][1]["owned_paths"] = [spelling]
                value["tasks"][2]["owned_paths"] = ["src/b/**"]
                report = validate_manifest_shape(value)
                self.assertIn(
                    "manifest.ownership_scope",
                    {issue.rule_id for issue in report.issues},
                )
                self.assertNotIn(
                    "manifest.parallel_ownership",
                    {issue.rule_id for issue in report.issues},
                )

    def test_every_ownership_scope_is_admitted_independently_in_every_wave(self) -> None:
        single = valid_manifest()
        single["requirements"] = [single["requirements"][0]]
        single["tasks"] = [single["tasks"][0]]
        single["protected_paths"] = ["../protected"]
        single["tasks"][0]["owned_paths"] = ["../owned"]
        single["tasks"][0]["allowed_auxiliary_paths"] = ["../auxiliary"]
        single_report = validate_manifest_shape(single)
        single_issues = [
            issue
            for issue in single_report.issues
            if issue.rule_id == "manifest.ownership_scope"
        ]
        self.assertEqual(3, len(single_issues))
        self.assertEqual(
            {
                ("protected_paths", 0),
                ("tasks", "TASK-001", "owned_paths", 0),
                ("tasks", "TASK-001", "allowed_auxiliary_paths", 0),
            },
            {issue.path for issue in single_issues},
        )
        self.assertEqual(
            {ReasonCode.INVALID_OWNERSHIP_SCOPE},
            {issue.reason_code for issue in single_issues},
        )

        for task_index in (0, 1, 3):
            for field_name in ("owned_paths", "allowed_auxiliary_paths"):
                with self.subTest(
                    wave=valid_manifest()["tasks"][task_index]["wave"],
                    field_name=field_name,
                ):
                    value = valid_manifest()
                    task = value["tasks"][task_index]
                    task[field_name] = ["src/*/../escape"]
                    report = validate_manifest_shape(value)
                    self.assertIn(
                        (
                            "tasks",
                            task["id"],
                            field_name,
                            0,
                        ),
                        {
                            issue.path
                            for issue in report.issues
                            if issue.rule_id == "manifest.ownership_scope"
                        },
                    )

    def test_active_m1_windows_collision_profile_is_fail_closed(self) -> None:
        unsafe_scopes = (
            "../escape",
            "src/../../escape",
            "src/*/../escape",
            "/absolute/path",
            r"\absolute\path",
            r"C:\outside\file",
            "./C:/outside/file",
            "x/../C:/outside/file",
            "src/C:/../safe/**",
            "src/file:stream/**",
            r"\\server\share\path",
            r"\\?\C:\outside\path",
            "src/name./file",
            "src/name /file",
            "src/bad<name/file",
            "src/bad>name/file",
            'src/bad"name/file',
            "src/bad|name/file",
            "src/con/file",
            "src/PrN.txt/file",
            "src/AUX.log/file",
            "src/NUL/file",
            "src/CLOCK$.txt/file",
            "src/CONIN$/file",
            "src/CONOUT$.log/file",
            "src/COM1/file",
            "src/com9.txt/file",
            "src/LPT1/file",
            "src/lpt9.log/file",
            "src/COM\u00b9.txt/file",
            "src/com\u00b2/file",
            "src/LPT\u00b3.log/file",
        )
        for scope in unsafe_scopes:
            with self.subTest(scope=scope):
                self.assertTrue(_analyze_ownership_scope(scope).unsafe)
                value = valid_manifest()
                value["tasks"][0]["owned_paths"] = [scope]
                report = validate_manifest_shape(value)
                issues = [
                    issue
                    for issue in report.issues
                    if issue.rule_id == "manifest.ownership_scope"
                ]
                self.assertEqual(1, len(issues), report.render_text())
                self.assertEqual(
                    ReasonCode.INVALID_OWNERSHIP_SCOPE,
                    issues[0].reason_code,
                )

        for scope in ("src/\x00/file", "src/\x1f/file", "src/\x7f/file"):
            with self.subTest(control=repr(scope)):
                self.assertTrue(_analyze_ownership_scope(scope).unsafe)

        for scope in (
            "src/?/file",
            "src/*/file",
            "src/[ab]/file",
            " src/a/**",
            "src/ a/**",
            "src//a/./file",
            r"src\a\file",
            "src/x/../a/file",
        ):
            with self.subTest(valid_scope=scope):
                self.assertFalse(_analyze_ownership_scope(scope).unsafe)

    def test_ownership_summary_matches_pairwise_reference_for_generated_sets(self) -> None:
        patterns = (
            "src/a",
            "src/a/b",
            "src/ab",
            "src/b",
            "src/a/**",
            "src/ab/**",
            "src/a*cd",
            "src/aef*",
            "tests/?/x",
            "docs/[ab]/**",
        )
        scope_sets = [
            combination
            for size in range(4)
            for combination in itertools.combinations(patterns, size)
        ]
        summaries = {
            scopes: _summarize_ownership(scopes)
            for scopes in scope_sets
        }
        for left in scope_sets:
            for right in scope_sets:
                expected = any(
                    _patterns_overlap(left_scope, right_scope)
                    for left_scope in left
                    for right_scope in right
                )
                self.assertEqual(
                    expected,
                    _ownership_summaries_overlap(
                        summaries[left],
                        summaries[right],
                    ),
                    (left, right),
                )

    def test_default_envelope_ownership_uses_linear_set_scans(self) -> None:
        value = valid_manifest()
        value["max_concurrency"] = 64
        value["requirements"] = [
            {
                "id": f"REQ-{index:03d}",
                "text": f"Requirement {index}",
                "status": "implemented",
            }
            for index in range(64)
        ]
        tasks = []
        for index in range(64):
            task = _task(
                f"TASK-{index:03d}",
                f"REQ-{index:03d}",
                depends_on=[] if index == 0 else ["TASK-000"],
                owned_path=f"src/task-{index:03d}/part-000/**",
                wave=0 if index == 0 else 1,
                issue_id=index + 1,
            )
            task["owned_paths"] = [
                f"src/task-{index:03d}/part-{path_index:03d}/**"
                for path_index in range(100)
            ]
            tasks.append(task)
        value["tasks"] = tasks

        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())
        from wish_builder.kernel import validation as validation_module

        with patch(
            "wish_builder.kernel.validation._prefixes_compatible",
            wraps=validation_module._prefixes_compatible,
        ) as comparisons:
            report = validate_manifest(decoded.value)
        self.assertTrue(report.ok, report.render_text())
        # All four owned/auxiliary field combinations are checked while the
        # compiled summaries keep the admitted envelope below the scan bound.
        self.assertEqual(402_318, comparisons.call_count)
        self.assertLess(comparisons.call_count, 500_000)

    def test_wave_barriers_cannot_be_bypassed(self) -> None:
        cases = ((1, []), (3, []))
        for task_index, dependencies in cases:
            with self.subTest(task_id=valid_manifest()["tasks"][task_index]["id"]):
                value = valid_manifest()
                value["tasks"][task_index]["depends_on"] = dependencies
                report = validate_manifest_shape(value, ValidationPhase.EXECUTION)
                self.assertIn(
                    "manifest.wave_barrier",
                    {issue.rule_id for issue in report.issues},
                )

    def test_stale_failed_task_status_is_not_an_active_m1_contract_value(self) -> None:
        value = valid_manifest()
        value["tasks"][1]["status"] = "failed"
        decoded = decode_manifest_primitive(value)
        self.assertFalse(decoded.ok)
        self.assertIn("schema.enum_value", {issue.rule_id for issue in decoded.issues})

    def test_finish_requires_verified_or_archived_tasks(self) -> None:
        value = valid_manifest()
        for requirement in value["requirements"]:
            requirement["status"] = "implemented"
        for index, task in enumerate(value["tasks"]):
            task.update(
                status="merged",
                pr_id=100 + index,
                squash_commit=f"commit-{index}",
            )

        merged_report = validate_manifest_shape(value, ValidationPhase.FINISH)
        self.assertEqual(
            4,
            sum(
                issue.rule_id == "manifest.finished_task"
                for issue in merged_report.issues
            ),
        )

        for task in value["tasks"]:
            task["status"] = "verified"
        verified_report = validate_manifest_shape(value, ValidationPhase.FINISH)
        self.assertTrue(verified_report.ok, verified_report.render_text())

    def test_shape_validation_is_total_for_invalid_json_compatible_values(self) -> None:
        values = (
            None,
            True,
            17,
            "manifest",
            [],
            {},
            {"schema_version": True, "tasks": {}},
            {"schema_version": 1, "unknown": [{"nested": [None]}]},
        )
        for value in values:
            with self.subTest(value=type(value).__name__):
                report = validate_manifest_shape(value)
                self.assertFalse(report.ok)
                self.assertTrue(report.issues)
                self.assertEqual(report.to_json_bytes(), report.to_json_bytes())

    def test_equivalent_shuffled_inputs_have_identical_diagnostic_bytes_and_hash(self) -> None:
        value = valid_manifest()
        value["tasks"][1]["requirement_ids"] = ["REQ-999"]
        value["tasks"][2]["owned_paths"] = ["src/a/components/**"]

        left = copy.deepcopy(value)
        right = copy.deepcopy(value)
        random.Random(11).shuffle(left["requirements"])
        random.Random(12).shuffle(left["tasks"])
        random.Random(21).shuffle(right["requirements"])
        random.Random(22).shuffle(right["tasks"])
        left = _shuffle_object_keys(left, random.Random(31))
        right = _shuffle_object_keys(right, random.Random(32))

        first = validate_manifest_bytes(_raw(left))
        second = validate_manifest_bytes(_raw(right))
        self.assertEqual(diagnostics_bytes(first), diagnostics_bytes(second))
        self.assertEqual(diagnostics_sha256(first), diagnostics_sha256(second))

    def test_diagnostics_are_sorted_and_use_normalized_lf(self) -> None:
        value = valid_manifest()
        value["tasks"][1]["requirement_ids"] = ["REQ-999"]
        value["tasks"][2]["owned_paths"] = ["src/a/components/**"]
        report = validate_manifest_shape(value)
        keys = [issue.sort_key() for issue in report.issues]
        self.assertEqual(sorted(keys), keys)
        encoded = diagnostics_bytes(report)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertNotIn(b"\r\n", encoded)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), diagnostics_sha256(report))

    def test_diagnostic_order_is_asserted_independently(self) -> None:
        def issue(
            stage: ValidationStage,
            rule_id: str,
            path: tuple[str, ...],
        ) -> ValidationIssue:
            return ValidationIssue(
                stage=stage,
                rule_id=rule_id,
                severity=Severity.ERROR,
                path=path,
                reason_code=ReasonCode.INVALID_JSON,
                message=f"{stage.value}:{rule_id}:{'/'.join(path)}",
            )

        unordered = (
            issue(ValidationStage.LOCAL, "a.rule", ("a",)),
            issue(ValidationStage.BOUNDARY, "a.rule", ("z",)),
            issue(ValidationStage.BOUNDARY, "z.rule", ("a",)),
        )
        report = ValidationReport(tuple(reversed(unordered)))
        self.assertEqual(
            [
                (ValidationStage.BOUNDARY, "z.rule", ("a",)),
                (ValidationStage.BOUNDARY, "a.rule", ("z",)),
                (ValidationStage.LOCAL, "a.rule", ("a",)),
            ],
            [(item.stage, item.rule_id, item.path) for item in report.issues],
        )

    def test_distinct_warning_and_error_facts_both_survive_deduplication(self) -> None:
        warning = ValidationIssue(
            stage=ValidationStage.BOUNDARY,
            rule_id="manifest.shared_fact",
            severity=Severity.WARNING,
            path=("tasks", "TASK-001"),
            reason_code=ReasonCode.INVALID_TASK,
            message="boundary warning",
        )
        error = ValidationIssue(
            stage=ValidationStage.LOCAL,
            rule_id="manifest.shared_fact",
            severity=Severity.ERROR,
            path=("tasks", "TASK-001"),
            reason_code=ReasonCode.INVALID_TASK,
            message="local error",
        )

        first = ValidationReport((warning, error))
        second = ValidationReport((error, warning))
        self.assertEqual(2, len(first.issues))
        self.assertEqual({Severity.WARNING, Severity.ERROR}, {item.severity for item in first.issues})
        self.assertFalse(first.ok)
        self.assertEqual(first.to_json_bytes(), second.to_json_bytes())

    def test_same_rule_path_different_fact_fields_are_not_collapsed(self) -> None:
        base = dict(
            stage=ValidationStage.LOCAL,
            rule_id="manifest.same_fact",
            severity=Severity.ERROR,
            path=("tasks", "TASK-001"),
            reason_code=ReasonCode.INVALID_TASK,
            message="first reason",
        )
        variants = (
            ValidationIssue(**base),
            ValidationIssue(**{**base, "stage": ValidationStage.REFERENTIAL}),
            ValidationIssue(**{**base, "reason_code": ReasonCode.UNKNOWN_DEPENDENCY}),
            ValidationIssue(**{**base, "message": "second reason"}),
            ValidationIssue(**{**base, "related_paths": (("tasks", "TASK-002"),)}),
            ValidationIssue(**{**base, "severity": Severity.WARNING}),
        )
        report = ValidationReport(tuple(reversed(variants)) + (variants[0],))
        self.assertEqual(len(variants), len(report.issues))
        self.assertFalse(report.ok)
        self.assertEqual(
            report.to_json_bytes(),
            ValidationReport(tuple(variants)).to_json_bytes(),
        )

    def test_dense_dag_uses_bounded_precomputed_reachability(self) -> None:
        # 64 nodes and exactly 512 edges, matching the admitted M1 envelope.
        tasks = {}
        for index in range(64):
            width = min(index, 8)
            if 9 <= index <= 44:
                width = 9
            dependencies = tuple(
                f"TASK-{index - offset:03d}" for offset in range(1, width + 1)
            )
            tasks[f"TASK-{index:03d}"] = SimpleNamespace(depends_on=dependencies)

        edge_count = sum(len(task.depends_on) for task in tasks.values())
        self.assertEqual(512, edge_count)
        reachability = _precompute_reachability(tasks)  # type: ignore[arg-type]
        self.assertEqual(0, len(reachability["TASK-000"]))
        self.assertEqual(63, len(reachability["TASK-063"]))
        self.assertIn("TASK-000", reachability["TASK-063"])

        # Compare every source against an independent bounded BFS reference;
        # this guards complete transitive wave barriers without relying on a
        # noisy wall-clock threshold.
        for source in sorted(tasks):
            expected: set[str] = set()
            pending = list(tasks[source].depends_on)
            while pending:
                dependency = pending.pop()
                if dependency in expected:
                    continue
                expected.add(dependency)
                pending.extend(tasks[dependency].depends_on)
            self.assertEqual(frozenset(expected), reachability[source])

        # Exercise the full validator with the same dense graph.  It must use
        # the compiled map rather than re-entering the legacy recursive helper
        # once per pairwise policy check.
        value = valid_manifest()
        value["max_concurrency"] = 64
        value["requirements"] = [
            {
                "id": f"REQ-{index:03d}",
                "text": f"Requirement {index}",
                "status": "implemented",
            }
            for index in range(64)
        ]
        value["tasks"] = [
            _task(
                f"TASK-{index:03d}",
                f"REQ-{index:03d}",
                depends_on=[
                    f"TASK-{index - offset:03d}" for offset in range(1, width + 1)
                ],
                owned_path=f"src/task-{index:03d}/**",
                wave=0 if index == 0 else 1,
                issue_id=index + 1,
            )
            for index in range(64)
            for width in (min(index, 8) + (1 if 9 <= index <= 44 else 0),)
        ]
        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())
        with patch(
            "wish_builder.kernel.validation._depends_on",
            side_effect=AssertionError("repeated dependency walk"),
        ):
            report = validate_manifest(decoded.value)
        self.assertTrue(report.ok, report.render_text())
        self.assertIn(
            "manifest.dependency_depth",
            {issue.rule_id for issue in report.issues},
        )

    def test_maximum_task_cycle_retains_typed_bounded_diagnostics(self) -> None:
        value = valid_manifest()
        value["max_concurrency"] = 64
        value["requirements"] = [
            {
                "id": f"REQ-{index:03d}",
                "text": f"Requirement {index}",
                "status": "implemented",
            }
            for index in range(64)
        ]
        value["tasks"] = [
            _task(
                f"TASK-{index:03d}",
                f"REQ-{index:03d}",
                depends_on=[f"TASK-{(index + 1) % 64:03d}"],
                owned_path=f"src/task-{index:03d}/**",
                wave=0,
                issue_id=index + 1,
            )
            for index in range(64)
        ]
        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())

        report = validate_manifest(decoded.value)
        cycle_issues = [
            issue
            for issue in report.issues
            if issue.rule_id == "manifest.dependency_cycle"
        ]
        self.assertEqual(1, len(cycle_issues), report.render_text())
        self.assertLessEqual(len(cycle_issues[0].message), 512)
        self.assertEqual(63, len(cycle_issues[0].related_paths))

    def test_rendered_diagnostic_messages_are_one_physical_line(self) -> None:
        issue = ValidationIssue(
            stage=ValidationStage.BOUNDARY,
            rule_id="json.hostile_message",
            severity=Severity.ERROR,
            path=("safe",),
            reason_code=ReasonCode.INVALID_JSON,
            message="hostile\r\n\t\x1b\u202etext",
        )
        rendered = ValidationReport((issue,)).render_text()
        self.assertNotIn("\r", rendered)
        self.assertEqual(1, len(rendered.splitlines()))
        self.assertIn(r"hostile\n\t\u001b\u202etext", rendered)

    def test_ownership_rechecks_resolved_drive_and_windows_aliases(self) -> None:
        for spelling in (
            "./C:/outside/file",
            "x/../C:/outside/file",
            "src/C:/../safe/**",
            "src/file:stream/**",
            "src/CON.txt/**",
            "src/CON/../safe/**",
            "src/ /../safe/**",
        ):
            with self.subTest(spelling=spelling):
                value = valid_manifest()
                value["tasks"][1]["owned_paths"] = [spelling]
                value["tasks"][2]["owned_paths"] = ["src/b/**"]
                report = validate_manifest_shape(value)
                self.assertIn(
                    "manifest.ownership_scope",
                    {issue.rule_id for issue in report.issues},
                )
                self.assertNotIn(
                    "manifest.parallel_ownership",
                    {issue.rule_id for issue in report.issues},
                )

    def test_ownership_trailing_dot_space_aliases_overlap_but_leading_space_does_not(self) -> None:
        for spelling in ("src/a./**", "src/a /**"):
            with self.subTest(spelling=spelling):
                value = valid_manifest()
                value["tasks"][1]["owned_paths"] = [spelling]
                value["tasks"][2]["owned_paths"] = ["src/a/**"]
                report = validate_manifest_shape(value)
                self.assertIn(
                    "manifest.ownership_scope",
                    {issue.rule_id for issue in report.issues},
                )
                self.assertNotIn(
                    "manifest.parallel_ownership",
                    {issue.rule_id for issue in report.issues},
                )

        for spelling in (" src/a/**", "src/ a/**"):
            with self.subTest(leading_space=spelling):
                value = valid_manifest()
                value["tasks"][1]["owned_paths"] = [spelling]
                value["tasks"][2]["owned_paths"] = ["src/a/**"]
                report = validate_manifest_shape(value)
                self.assertNotIn(
                    "manifest.parallel_ownership",
                    {issue.rule_id for issue in report.issues},
                )
                self.assertNotIn(
                    "manifest.ownership_scope",
                    {issue.rule_id for issue in report.issues},
                )

    def test_admission_withholds_a_semantically_invalid_model(self) -> None:
        value = valid_manifest()
        value["tasks"][1]["requirement_ids"] = ["REQ-999"]
        result = admit_manifest_bytes(_raw(value), ValidationPhase.EXECUTION)
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertIn(
            "manifest.requirement_reference",
            {issue.rule_id for issue in result.issues},
        )

    def test_phase_specific_failures_are_deterministic(self) -> None:
        value = valid_manifest()
        value["tasks"][1]["branch"] = None
        report = validate_manifest_shape(value, ValidationPhase.EXECUTION)
        self.assertIn(
            "manifest.execution_branch",
            {issue.rule_id for issue in report.issues},
        )


if __name__ == "__main__":
    unittest.main()
