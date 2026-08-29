from __future__ import annotations

import dataclasses
import unittest

from wish_builder.kernel.gates import (
    DecisionReason,
    GateMaterial,
    evaluate_decision,
    revalidate_gate_material,
)

from .test_gates import HASH_B, request, submission


class GateInputBoundaryTests(unittest.TestCase):
    def test_gate_material_rejects_malformed_digest_references(self) -> None:
        for field_name in ("candidate_hash", "workspace_hash"):
            with self.subTest(field=field_name):
                values = {"candidate_hash": HASH_B, "workspace_hash": HASH_B}
                values[field_name] = "sha256:short"
                with self.assertRaisesRegex(ValueError, "full sha256"):
                    GateMaterial(**values)

    def test_decision_admission_rejects_untyped_trust_boundary_values(self) -> None:
        with self.assertRaisesRegex(TypeError, "DecisionRequest"):
            evaluate_decision(
                object(),
                submission(),
                current_sequence=41,
                current_workspace_hash=HASH_B,
            )
        with self.assertRaisesRegex(TypeError, "DecisionCommand"):
            evaluate_decision(
                request(),
                object(),
                current_sequence=41,
                current_workspace_hash=HASH_B,
            )

        accepted = evaluate_decision(
            request(),
            submission(),
            current_sequence=41,
            current_workspace_hash=HASH_B,
        )
        assert accepted.observation is not None
        with self.assertRaisesRegex(TypeError, "DecisionObservation"):
            evaluate_decision(
                request(),
                submission(),
                current_sequence=42,
                current_workspace_hash=HASH_B,
                observed=object(),
            )

    def test_request_id_mismatch_is_rejected_before_other_submission_fields(
        self,
    ) -> None:
        mismatched_request = dataclasses.replace(
            request(),
            command=dataclasses.replace(request().command, request_id="REQUEST-999"),
        )
        candidate = dataclasses.replace(submission(), request=mismatched_request)
        result = evaluate_decision(
            request(),
            candidate,
            current_sequence=41,
            current_workspace_hash=HASH_B,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(DecisionReason.REQUEST_MISMATCH, result.reason)

    def test_gate_revalidation_requires_typed_material_on_both_sides(self) -> None:
        material = GateMaterial(HASH_B, HASH_B)
        for approved, current in ((object(), material), (material, object())):
            with (
                self.subTest(
                    invalid="approved" if approved is not material else "current"
                ),
                self.assertRaisesRegex(TypeError, "GateMaterial"),
            ):
                revalidate_gate_material(approved, current)


if __name__ == "__main__":
    unittest.main()
