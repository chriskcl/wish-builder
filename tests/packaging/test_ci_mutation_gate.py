from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from scripts.ci_mutation_gate import (
    DEFAULT_MUTATIONS,
    REPOSITORY_ROOT,
    MutationGateError,
    MutationResult,
    MutationSpec,
    MutationStatus,
    TestRunSummary as _TestRunSummary,
    evaluate_policy,
    run_mutation_gate,
    validate_mutation_specs,
)


def fixture_spec(*, safety_invariant: bool = True) -> MutationSpec:
    return MutationSpec(
        "FIXTURE-DENIAL-GUARD",
        "Denied input remains rejected.",
        "sample/guard.py",
        '    if value != "approved":\n',
        '    if False and value != "approved":\n',
        ("fixture_tests.test_guard.GuardTests.test_denied_input_is_rejected",),
        safety_invariant,
    )


def write_fixture(root: Path, *, passing: bool = True) -> None:
    files = {
        "sample/__init__.py": "",
        "sample/guard.py": (
            "def admit(value):\n"
            '    if value != "approved":\n'
            "        return False\n"
            "    return True\n"
        ),
        "fixture_tests/__init__.py": "",
        "fixture_tests/test_guard.py": (
            "import unittest\n"
            "from sample.guard import admit\n"
            "\n"
            "class GuardTests(unittest.TestCase):\n"
            "    def test_denied_input_is_rejected(self):\n"
            + (
                '        self.assertFalse(admit("denied"))\n'
                if passing
                else '        self.assertTrue(admit("denied"))\n'
            )
        ),
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def result(
    mutation_id: str,
    status: MutationStatus,
    *,
    safety_invariant: bool,
) -> MutationResult:
    failures = 1 if status is MutationStatus.KILLED else 0
    errors = 1 if status is MutationStatus.ERROR else 0
    return MutationResult(
        mutation_id,
        "fixture invariant",
        safety_invariant,
        status,
        ("fixture.test",),
        _TestRunSummary(
            status is MutationStatus.SURVIVED,
            1,
            failures,
            errors,
            0,
        ),
        "wish_builder/kernel/fixture.py",
    )


class MutationGateTests(unittest.TestCase):
    def test_canonical_serializer_sorts_arbitrary_mapping_keys(self) -> None:
        from wish_builder.contracts.serialization import canonical_json_bytes

        self.assertEqual(
            b'{"a":1,"z":2}\n',
            canonical_json_bytes({"z": 2, "a": 1}),
        )

    def test_default_registry_is_fixed_unique_and_safety_only(self) -> None:
        sources = validate_mutation_specs(REPOSITORY_ROOT, DEFAULT_MUTATIONS)

        self.assertGreaterEqual(len(DEFAULT_MUTATIONS), 10)
        self.assertEqual(
            len(DEFAULT_MUTATIONS),
            len({item.mutation_id for item in DEFAULT_MUTATIONS}),
        )
        self.assertTrue(all(item.safety_invariant for item in DEFAULT_MUTATIONS))
        self.assertEqual(
            {item.source_path for item in DEFAULT_MUTATIONS},
            set(sources),
        )

    def test_fixture_mutation_is_killed_without_editing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_fixture(root)
            source = root / "sample" / "guard.py"
            original = source.read_bytes()

            report = run_mutation_gate(root, (fixture_spec(),), timeout_seconds=30)
            repeated = run_mutation_gate(
                root,
                (fixture_spec(),),
                timeout_seconds=30,
            )

            self.assertTrue(report.baseline.successful)
            self.assertTrue(report.passed)
            self.assertEqual(100.0, report.policy.score)
            self.assertEqual(MutationStatus.KILLED, report.results[0].status)
            self.assertEqual(original, source.read_bytes())
            self.assertEqual(report.to_json_bytes(), repeated.to_json_bytes())

    def test_failing_baseline_closes_gate_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_fixture(root, passing=False)

            report = run_mutation_gate(root, (fixture_spec(),), timeout_seconds=30)

            self.assertFalse(report.passed)
            self.assertFalse(report.baseline.successful)
            self.assertEqual((), report.results)
            self.assertEqual(("baseline_tests_failed",), report.policy.reasons)

    def test_loader_error_and_timeout_are_baseline_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_fixture(root)
            missing_test = dataclasses.replace(
                fixture_spec(),
                test_ids=("fixture_tests.test_guard.MissingTests.test_missing",),
            )

            missing = run_mutation_gate(
                root,
                (missing_test,),
                timeout_seconds=30,
            )
            timed_out = run_mutation_gate(
                root,
                (fixture_spec(),),
                timeout_seconds=0.000001,
            )

            self.assertFalse(missing.passed)
            self.assertEqual(
                "test_loader_error",
                missing.baseline.infrastructure_error,
            )
            self.assertEqual((), missing.results)
            self.assertFalse(timed_out.passed)
            self.assertEqual(
                "test_process_timeout",
                timed_out.baseline.infrastructure_error,
            )
            self.assertEqual((), timed_out.results)

    def test_mutant_test_error_is_not_counted_as_killed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_fixture(root)
            erroring = dataclasses.replace(
                fixture_spec(),
                mutation_id="FIXTURE-TEST-ERROR",
                after=(
                    '    raise RuntimeError("mutant error")\n'
                    '    if value != "approved":\n'
                ),
            )

            report = run_mutation_gate(root, (erroring,), timeout_seconds=30)

            self.assertFalse(report.passed)
            self.assertEqual(MutationStatus.ERROR, report.results[0].status)
            self.assertEqual(1, report.policy.errors)
            self.assertIn("mutation_execution_error", report.policy.reasons)

    def test_score_and_safety_survivor_are_independent_blockers(self) -> None:
        ninety_percent = tuple(
            result(
                f"FIXTURE-KILLED-{index}",
                MutationStatus.KILLED,
                safety_invariant=True,
            )
            for index in range(9)
        ) + (
            result(
                "FIXTURE-SAFETY-SURVIVOR",
                MutationStatus.SURVIVED,
                safety_invariant=True,
            ),
        )
        safety_policy = evaluate_policy(ninety_percent)
        self.assertEqual(90.0, safety_policy.score)
        self.assertFalse(safety_policy.passed)
        self.assertEqual(
            ("FIXTURE-SAFETY-SURVIVOR",),
            safety_policy.surviving_safety_mutations,
        )
        self.assertIn("surviving_safety_mutation", safety_policy.reasons)

        below_minimum = tuple(
            result(
                f"FIXTURE-PASS-{index}",
                MutationStatus.KILLED,
                safety_invariant=False,
            )
            for index in range(8)
        ) + tuple(
            result(
                f"FIXTURE-SURVIVE-{index}",
                MutationStatus.SURVIVED,
                safety_invariant=False,
            )
            for index in range(2)
        )
        score_policy = evaluate_policy(below_minimum)
        self.assertEqual(80.0, score_policy.score)
        self.assertFalse(score_policy.passed)
        self.assertIn("mutation_score_below_minimum", score_policy.reasons)
        self.assertNotIn("surviving_safety_mutation", score_policy.reasons)

    def test_missing_or_ambiguous_anchor_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_fixture(root)
            source = root / "sample" / "guard.py"
            source.write_text(
                source.read_text(encoding="utf-8")
                + '\ndef duplicate(value):\n    if value != "approved":\n'
                + "        return False\n",
                encoding="utf-8",
                newline="\n",
            )

            with self.assertRaisesRegex(MutationGateError, "exactly once"):
                validate_mutation_specs(root, (fixture_spec(),))


if __name__ == "__main__":
    unittest.main()
