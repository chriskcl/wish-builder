from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.ci_test_suite import (
    EvidenceTextTestResult,
    _require_revision,
    _summary_for_result,
    _write_summary,
    discover_suite,
    discover_test_ids,
    test_id_digest,
)


def _test_ids(suite: unittest.TestSuite) -> tuple[str, ...]:
    values: list[str] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            values.extend(_test_ids(item))
        else:
            values.append(item.id())
    return tuple(values)


class CiTestSuiteTests(unittest.TestCase):
    def test_performance_package_is_excluded_before_discovery(self) -> None:
        ids = _test_ids(discover_suite(exclude_packages=frozenset({"performance"})))

        self.assertTrue(ids)
        self.assertTrue(any("tests.contracts." in item for item in ids))
        self.assertTrue(any("tests.test_wishctl." in item for item in ids))
        self.assertFalse(any("tests.performance." in item for item in ids))

    def test_performance_package_can_be_selected_alone(self) -> None:
        ids = _test_ids(discover_suite(only_package="performance"))

        self.assertTrue(ids)
        self.assertTrue(all("tests.performance." in item for item in ids))

    def test_invalid_or_conflicting_selection_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            discover_suite(
                exclude_packages=frozenset({"performance"}),
                only_package="contracts",
            )
        with self.assertRaisesRegex(ValueError, "invalid test package"):
            discover_suite(exclude_packages=frozenset({"../performance"}))
        with self.assertRaisesRegex(ValueError, "unknown test package"):
            discover_suite(exclude_packages=frozenset({"missing"}))

    def test_revision_bound_summary_is_canonical_and_complete(self) -> None:
        suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
        discovered = discover_test_ids(suite)
        result = unittest.TextTestRunner(
            stream=io.StringIO(), resultclass=EvidenceTextTestResult
        ).run(suite)
        revision = "a" * 40
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        summary = _summary_for_result(
            result,
            discovered_test_ids=discovered,
            revision=revision,
            cell_id=f"ubuntu-latest-py{python_version}",
            platform="ubuntu-latest",
            python_version=python_version,
        )

        with TemporaryDirectory() as raw:
            output = Path(raw) / "summary.json"
            _write_summary(output, summary)
            encoded = output.read_bytes()

        self.assertEqual(b"{", encoded[:1])
        self.assertEqual(b"\n", encoded[-1:])
        self.assertEqual(revision, summary["github_sha"])
        self.assertEqual(revision, summary["revision"])
        self.assertEqual("passed", summary["status"])
        self.assertEqual(1, summary["tests_run"])
        self.assertEqual(1, summary["discovered_test_count"])
        self.assertEqual(1, summary["executed_test_count"])
        self.assertEqual(test_id_digest(discovered), summary["discovered_test_ids_digest"])
        self.assertEqual(test_id_digest(discovered), summary["executed_test_ids_digest"])
        self.assertEqual([], summary["skipped_tests"])

    def test_test_id_digest_rejects_hollow_and_preserves_duplicate_executions(self) -> None:
        with self.assertRaises(ValueError):
            test_id_digest(())
        self.assertNotEqual(
            test_id_digest(("tests.example",)),
            test_id_digest(("tests.example", "tests.example")),
        )

    def test_summary_revision_rejects_symbolic_or_uppercase_refs(self) -> None:
        for invalid in ("HEAD", "A" * 40, "a" * 39, "a" * 41):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _require_revision(invalid)
        with patch.dict("os.environ", {"GITHUB_SHA": ""}):
            with self.assertRaises(ValueError):
                _require_revision(None)


if __name__ == "__main__":
    unittest.main()
