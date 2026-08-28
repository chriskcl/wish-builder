from __future__ import annotations

import copy
import dataclasses
import unittest
from types import SimpleNamespace

from wish_builder.contracts import (
    ActorType,
    CommandKind,
    DecisionChoice,
    ValidationPhase,
    decode_manifest_primitive,
)
from wish_builder.kernel.gates import (
    CommandIdentity,
    DecisionActor,
    DecisionChannel,
    DecisionEvaluation,
    DecisionObserved,
    DecisionReason,
    DecisionRequest,
    DecisionSubmission,
    DecisionType,
    evaluate_decision,
)
from wish_builder.kernel.validation import (
    _canonical_cycle,
    _canonicalize_path,
    _contains_path,
    _dependency_depth,
    _depends_on,
    _normalize_path,
    _ownership_summaries_overlap,
    _patterns_overlap,
    _phase,
    _static_prefix,
    _summarize_ownership,
    _windows_alias_path,
    admit_manifest_bytes,
    admit_manifest_primitive,
    diagnostics_bytes,
    diagnostics_sha256,
    render_diagnostics,
    validate_manifest,
    validate_manifest_bytes,
)

from .test_validation import _raw, valid_manifest


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
NOW = "2026-08-18T03:00:00Z"


def _actor() -> DecisionActor:
    return DecisionActor(ActorType.HUMAN, "account-1", "host-1", 7, "start-1")


def _coordinator_actor() -> DecisionActor:
    return DecisionActor(
        ActorType.COORDINATOR,
        "coordinator-1",
        "host-1",
        8,
        "start-2",
    )


def _request() -> DecisionRequest:
    return DecisionRequest(
        command=CommandIdentity(
            1,
            "COMMAND-1",
            "REQUEST-1",
            CommandKind.DECIDE,
            4,
            "nonce-1",
            _coordinator_actor(),
            DecisionChannel.COORDINATOR,
            NOW,
        ),
        decision_type=DecisionType.GATE_B,
        candidate_hash=HASH_A,
        workspace_hash=HASH_B,
        expected_actor_id="account-1",
        options=(DecisionChoice.APPROVE, DecisionChoice.REJECT),
    )


def _submission() -> DecisionSubmission:
    return DecisionSubmission(
        decision_id="DECISION-1",
        request=_request(),
        choice=DecisionChoice.APPROVE,
        actor=_actor(),
        source_channel=DecisionChannel.DIRECT_CLI,
        decided_at=NOW,
    )


class GateInvariantEdgeTests(unittest.TestCase):
    def test_actor_request_and_submission_reject_lookalike_values(self) -> None:
        actor_cases = (
            ((ActorType.HUMAN, "bad token", "host-1", 7, "start-1"), ValueError),
            ((ActorType.HUMAN, "account-1", "host-1", 0, "start-1"), ValueError),
        )
        for arguments, error in actor_cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(error):
                    DecisionActor(*arguments)

        request = _request()
        request_cases = (
            ({"decision_type": "gate_b"}, TypeError),
            ({"candidate_hash": "sha256:BAD"}, ValueError),
            ({"command": object()}, ValueError),
            ({"options": []}, TypeError),
            (
                {"options": (DecisionChoice.APPROVE, DecisionChoice.APPROVE)},
                ValueError,
            ),
        )
        for updates, error in request_cases:
            with self.subTest(request_field=tuple(updates)):
                with self.assertRaises(error):
                    dataclasses.replace(request, **updates)

        with self.assertRaisesRegex(ValueError, "expected_sequence"):
            dataclasses.replace(request.command, expected_sequence=True)

        submission = _submission()
        submission_cases = (
            ({"choice": "approve"}, TypeError),
            ({"actor": object()}, TypeError),
            ({"source_channel": "direct_cli"}, TypeError),
            ({"decided_at": "2026-08-18 03:00:00"}, ValueError),
        )
        for updates, error in submission_cases:
            with self.subTest(submission_field=tuple(updates)):
                with self.assertRaises(error):
                    dataclasses.replace(submission, **updates)

        self.assertEqual("gate_b", request.to_primitive()["decision_type"])

    def test_evaluation_model_rejects_impossible_states(self) -> None:
        cases = (
            ((1, DecisionReason.ACCEPTED), TypeError),
            ((False, "accepted"), TypeError),
            ((True, DecisionReason.ACCEPTED), ValueError),
            ((False, DecisionReason.ACCEPTED, None, True), ValueError),
        )
        for arguments, error in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(error):
                    DecisionEvaluation(*arguments)  # type: ignore[arg-type]

    def test_observation_primitive_and_invalid_current_sequence(self) -> None:
        evaluation = evaluate_decision(
            _request(),
            _submission(),
            current_sequence=4,
            current_workspace_hash=HASH_B,
        )
        assert evaluation.observation is not None
        primitive = evaluation.observation.to_primitive()
        self.assertEqual(5, primitive["event_sequence"])
        self.assertEqual(
            "gate_b",
            primitive["decision"]["request"]["decision_type"],
        )
        for invalid in (-1, True):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "current_sequence"):
                    evaluate_decision(
                        _request(),
                        _submission(),
                        current_sequence=invalid,
                        current_workspace_hash=HASH_B,
                    )

class KernelValidationEdgeTests(unittest.TestCase):
    def test_phase_boundary_accepts_exact_values_and_rejects_lookalikes(self) -> None:
        self.assertIs(ValidationPhase.FINISH, _phase("finish"))
        with self.assertRaisesRegex(ValueError, "planning, execution, or finish"):
            _phase("done")
        with self.assertRaisesRegex(TypeError, "ValidationPhase"):
            _phase(1)  # type: ignore[arg-type]

    def test_path_compatibility_helpers_are_conservative(self) -> None:
        with self.assertRaisesRegex(TypeError, "ownership scope"):
            _canonicalize_path(1)  # type: ignore[arg-type]
        self.assertEqual(("src/a", False), _canonicalize_path("src/./b/../a"))
        self.assertEqual("src/a", _normalize_path("src/./a"))
        self.assertEqual(("", False), _windows_alias_path(""))
        self.assertEqual(("src/a", True), _windows_alias_path("src/a. "))
        self.assertEqual("", _static_prefix("../outside"))
        self.assertTrue(_contains_path("src", "src"))
        self.assertTrue(_contains_path("src", "src/a"))
        self.assertFalse(_contains_path("src", "src2/a"))

    def test_ownership_overlap_fails_closed_for_invalid_and_match_all_scopes(self) -> None:
        self.assertTrue(_patterns_overlap("../outside", "src/a"))
        self.assertTrue(_patterns_overlap("**", "unrelated/path"))
        self.assertTrue(
            _ownership_summaries_overlap(
                _summarize_ownership(("**",)),
                _summarize_ownership(("src/a",)),
            )
        )
        self.assertFalse(
            _ownership_summaries_overlap(
                _summarize_ownership(()),
                _summarize_ownership(("src/a",)),
            )
        )

    def test_iterative_dependency_helper_handles_missing_seen_cycle_and_hit(self) -> None:
        tasks = {
            "A": SimpleNamespace(depends_on=("B", "MISSING")),
            "B": SimpleNamespace(depends_on=("C",)),
            "C": SimpleNamespace(depends_on=("A",)),
        }
        self.assertFalse(_depends_on("MISSING", "A", tasks))  # type: ignore[arg-type]
        self.assertFalse(_depends_on("A", "C", tasks, frozenset({"A"})))  # type: ignore[arg-type]
        self.assertTrue(_depends_on("A", "C", tasks))  # type: ignore[arg-type]
        self.assertFalse(_depends_on("A", "D", tasks))  # type: ignore[arg-type]
        self.assertEqual(0, _dependency_depth(tasks))  # type: ignore[arg-type]
        self.assertEqual((), _canonical_cycle([]))

    def test_lifecycle_and_policy_matrix_emits_each_blocking_fact(self) -> None:
        value = valid_manifest()
        value["tasks"][0]["depends_on"] = ["TASK-001"]
        value["tasks"][1]["branch"] = "main"
        value["tasks"][1]["status"] = "proposed"
        value["tasks"][2]["status"] = "pr_open"
        value["tasks"][2]["pr_id"] = None
        value["tasks"][3]["status"] = "merged"
        value["tasks"][3]["squash_commit"] = None
        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None

        report = validate_manifest(decoded.value, ValidationPhase.EXECUTION)
        rules = {issue.rule_id for issue in report.issues}
        self.assertTrue(
            {
                "manifest.self_dependency",
                "manifest.base_branch_reuse",
                "manifest.proposed_after_gate_b",
                "manifest.pr_identity",
                "manifest.squash_commit",
            }.issubset(rules),
            report.render_text(),
        )

    def test_unordered_serial_wave_and_finish_requirement_are_reported(self) -> None:
        value = valid_manifest()
        second = copy.deepcopy(value["tasks"][0])
        second["id"] = "TASK-005"
        second["requirement_ids"] = ["REQ-002"]
        second["owned_paths"] = ["src/serial/**"]
        second["issue_id"] = 5
        second["branch"] = "feat/5-serial"
        second["may_change_contracts"] = False
        value["tasks"].append(second)
        decoded = decode_manifest_primitive(value)
        self.assertTrue(decoded.ok, decoded.report.render_text())
        assert decoded.value is not None

        report = validate_manifest(decoded.value, ValidationPhase.FINISH)
        rules = {issue.rule_id for issue in report.issues}
        self.assertIn("manifest.serial_wave_order", rules)
        self.assertIn("manifest.implemented_requirement", rules)

    def test_boundary_wrappers_fail_closed_and_validate_argument_types(self) -> None:
        invalid_raw = b"not json"
        self.assertFalse(validate_manifest_bytes(invalid_raw).ok)
        self.assertFalse(admit_manifest_bytes(invalid_raw).ok)
        self.assertFalse(admit_manifest_primitive({}).ok)

        valid = valid_manifest()
        admitted = admit_manifest_primitive(valid)
        self.assertTrue(admitted.ok, admitted.report.render_text())
        self.assertTrue(admit_manifest_bytes(_raw(valid)).ok)

        for function in (diagnostics_bytes, diagnostics_sha256, render_diagnostics):
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(TypeError, "ValidationReport"):
                    function(object())  # type: ignore[arg-type]
        self.assertEqual("OK\n", render_diagnostics(admitted.report))


if __name__ == "__main__":
    unittest.main()
