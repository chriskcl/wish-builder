from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.ci_mutation_gate import DEFAULT_MUTATIONS

REGISTRY = Path(__file__).with_name("failure-witnesses.json")


class FailureWitnessRegistryTests(unittest.TestCase):
    def test_all_23_failure_families_have_loadable_executable_witnesses(self) -> None:
        document = json.loads(REGISTRY.read_text(encoding="utf-8"))
        self.assertEqual(1, document["schema_version"])
        families = document["families"]
        self.assertEqual(23, len(families))
        self.assertEqual(23, len({item["id"] for item in families}))
        self.assertEqual(23, len({item["codepath"] for item in families}))

        for family in families:
            with self.subTest(family=family["id"]):
                self.assertEqual(
                    {
                        "asserted_outcome",
                        "codepath",
                        "failure_trigger",
                        "id",
                        "test_ids",
                    },
                    set(family),
                )
                self.assertTrue(family["failure_trigger"].strip())
                self.assertTrue(family["asserted_outcome"].strip())
                self.assertTrue(family["test_ids"])
                for test_id in family["test_ids"]:
                    loader = unittest.TestLoader()
                    suite = loader.loadTestsFromName(test_id)
                    self.assertEqual([], loader.errors)
                    self.assertGreater(suite.countTestCases(), 0)

    def test_every_safety_mutation_names_direct_executable_evidence(self) -> None:
        mutation_ids = [mutation.mutation_id for mutation in DEFAULT_MUTATIONS]
        self.assertEqual(len(mutation_ids), len(set(mutation_ids)))
        self.assertTrue(mutation_ids)

        for mutation in DEFAULT_MUTATIONS:
            with self.subTest(mutation=mutation.mutation_id):
                self.assertTrue(mutation.safety_invariant)
                self.assertTrue(mutation.test_ids)
                for test_id in mutation.test_ids:
                    loader = unittest.TestLoader()
                    suite = loader.loadTestsFromName(test_id)
                    self.assertEqual([], loader.errors)
                    self.assertGreater(suite.countTestCases(), 0)


if __name__ == "__main__":
    unittest.main()
