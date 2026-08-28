from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _normalized_section(path: Path, heading: str) -> str:
    document = path.read_text(encoding="utf-8")
    _, separator, tail = document.partition(heading)
    if not separator:
        raise AssertionError(f"missing contract heading: {heading}")
    section, _, _ = tail.partition("\n## ")
    return " ".join(section.split())


def _rule(section: str, number: int) -> str:
    marker = f"**GQ-{number} -"
    _, separator, tail = section.partition(marker)
    if not separator:
        raise AssertionError(f"missing contract rule: GQ-{number}")
    if number == 7:
        return tail
    rule, next_separator, _ = tail.partition(f"**GQ-{number + 1} -")
    if not next_separator:
        raise AssertionError(f"missing contract rule: GQ-{number + 1}")
    return rule


class GstackQuestionHandoffContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = _normalized(REPOSITORY_ROOT / "wish-builder" / "SKILL.md")
        cls.bridge = _normalized_section(
            REPOSITORY_ROOT / "wish-builder" / "references" / "tool-bridges.md",
            "### gstack Question Handoff",
        )
        cls.rules = {number: _rule(cls.bridge, number) for number in range(1, 8)}

    def test_gq1_every_review_runs_in_a_non_interactive_child_session(self) -> None:
        self.assertIn("GQ-1 - Isolate every review.", self.skill)
        self.assertIn(
            "Run each review in its own non-interactive child session.", self.skill
        )
        self.assertIn(
            "Run every gstack review in its own non-interactive child session.",
            self.rules[1],
        )
        self.assertIn("must not own the user conversation", self.rules[1])

    def test_gq2_raw_gstack_questions_never_reach_the_user(self) -> None:
        self.assertIn("GQ-2 - Hide raw questions.", self.skill)
        self.assertIn(
            "Never display, quote, or relay a raw gstack question to the user.",
            self.skill,
        )
        self.assertIn("not copied from the child prompt", self.rules[2])

    def test_gq3_each_decision_has_the_complete_transfer_schema(self) -> None:
        self.assertIn("GQ-3 - Transfer complete decisions.", self.skill)
        for field in (
            "`practical_outcome`",
            "`alternatives`",
            "`recommendation_and_reason`",
            "`changeability`",
            "`decision_class`",
            "`original_technical_explanation`",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.rules[3])
        for decision_class in (
            "`product`",
            "`architecture`",
            "`cost`",
            "`security`",
            "`external_action`",
        ):
            with self.subTest(decision_class=decision_class):
                self.assertIn(decision_class, self.rules[3])
        self.assertIn(
            "temporarily choose only the answer explicitly marked recommended",
            self.rules[3],
        )
        self.assertIn("one decision transfer per choice", self.rules[3])
        self.assertIn("a yes/no flag for each of", self.rules[3])
        self.assertIn(
            "all five may be false for a purely engineering choice",
            self.rules[3],
        )

    def test_gq4_reclassifies_both_automatic_and_human_owned_choices(self) -> None:
        self.assertIn("GQ-4 - Reclassify centrally.", self.skill)
        self.assertIn("easy, reversible engineering choice", self.rules[4])
        self.assertIn(
            "record the adoption and rationale in the decision log",
            self.rules[4],
        )
        for escalation_condition in (
            "material",
            "difficult to reverse",
            "disputed",
            "product",
            "architecture",
            "cost",
            "security",
        ):
            with self.subTest(escalation_condition=escalation_condition):
                self.assertRegex(
                    self.rules[4],
                    re.compile(
                        rf"If a choice is .*?{re.escape(escalation_condition)}.*?Gate A",
                        re.IGNORECASE,
                    ),
                )
        self.assertIn(
            "rewrite it in plain language and queue it for Gate A",
            self.rules[4],
        )

    def test_gq5_automatic_choices_are_advice_not_human_approval(self) -> None:
        self.assertIn("GQ-5 - Preserve approval authority.", self.skill)
        self.assertIn("Automatic gstack choices are advice only.", self.rules[5])
        self.assertIn("They are never human approval", self.rules[5])

    def test_gq6_human_decisions_are_consolidated_at_gate_a(self) -> None:
        self.assertIn("GQ-6 - Batch human decisions.", self.skill)
        self.assertIn(
            "Do not interrupt the user decision by decision.",
            self.rules[6],
        )
        self.assertIn(
            "Consolidate all human-owned review choices into one Gate A decision packet.",
            self.rules[6],
        )
        self.assertIn(
            "never creates a per-question user interruption",
            self.rules[6],
        )

    def test_gq7_direct_questions_or_incomplete_data_fail_closed(self) -> None:
        self.assertIn("GQ-7 - Fail closed.", self.skill)
        for failure_trigger in (
            "directly asks the user",
            "exposes an interactive question",
            "lacks an explicitly recommended answer",
            "omits any required GQ-3 field",
        ):
            with self.subTest(failure_trigger=failure_trigger):
                self.assertIn(failure_trigger, self.rules[7])
        self.assertIn("stop that child review immediately", self.rules[7])
        self.assertIn("`integration_capability_failure`", self.rules[7])
        self.assertIn("do not treat the review as complete", self.rules[7])
        self.assertIn("Never relay the raw question", self.rules[7])
        self.assertIn("or invent the missing decision data", self.rules[7])


if __name__ == "__main__":
    unittest.main()
