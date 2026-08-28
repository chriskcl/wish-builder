from __future__ import annotations

import copy
import dataclasses
import unittest
from unittest.mock import patch

from wish_builder.contracts import decoder
from wish_builder.contracts.decoder import DecodeLimits, decode_manifest_bytes, decode_manifest_primitive
from wish_builder.contracts.diagnostics import (
    DecodeResult,
    ReasonCode,
    Severity,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
    combine_reports,
)
from wish_builder.contracts.models import (
    ApprovalSet,
    ExecutionManifest,
    GateApproval,
    Requirement,
    RequirementStatus,
    RiskLevel,
    TaskStatus,
)
from wish_builder.contracts.serialization import canonical_json_bytes

from .test_decoder import valid_manifest


def _codes(result: DecodeResult[object]) -> set[ReasonCode]:
    return {issue.reason_code for issue in result.issues}


def _issue(**overrides: object) -> ValidationIssue:
    values: dict[str, object] = {
        "stage": ValidationStage.LOCAL,
        "rule_id": "contract.edge",
        "severity": Severity.ERROR,
        "path": ("field",),
        "reason_code": ReasonCode.INVALID_MANIFEST,
        "message": "Invalid contract edge.",
    }
    values.update(overrides)
    return ValidationIssue(**values)  # type: ignore[arg-type]


class DecoderBoundaryEdgeTests(unittest.TestCase):
    def test_decode_limits_reject_bool_zero_and_excessive_depth(self) -> None:
        for field_name in ("max_bytes", "max_depth", "max_items", "max_string_length"):
            for invalid in (0, True):
                with self.subTest(field=field_name, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        DecodeLimits(**{field_name: invalid})
        with self.assertRaisesRegex(ValueError, "diagnostic path safety boundary"):
            DecodeLimits(max_depth=65)

    def test_diagnostic_path_segments_are_bounded_for_hostile_shapes(self) -> None:
        surrogate = "bad\ud800key"
        self.assertRegex(decoder._bounded_segment(surrogate), r"^<invalid-unicode~[0-9a-f]{16}>$")
        self.assertEqual(65, len(decoder._bounded_segment("x" * 200)))

        self.assertEqual("TASK-001", decoder._shape_segment({"id": "TASK-001"}))
        self.assertEqual(7, decoder._shape_segment({}, 7))
        self.assertRegex(decoder._shape_segment({}), r"^@[0-9a-f]{16}$")
        for value in (None, False, -(2**300), 1.5, "text", []):
            with self.subTest(value_type=type(value).__name__):
                self.assertRegex(str(decoder._shape_segment(value)), r"^@[0-9a-f]{16}$")
        self.assertEqual(9, decoder._shape_segment([], 9))
        self.assertEqual("@unsupported", decoder._shape_segment(object()))
        self.assertEqual(11, decoder._shape_segment(object(), 11))

    def test_json_string_scanner_stays_bounded_on_truncated_escapes(self) -> None:
        cases = {
            '"trailing\\': (10, 8),
            '"\\u12': (5, 1),
            '"\\uZZZZ': (7, 1),
            '"\\uD800\\uZZZZ': (13, 2),
            '"\\uD800\\uDC00"': (14, 1),
            '"unterminated': (13, 12),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(expected, decoder._scan_string(text, 0))

    def test_oversized_object_key_is_rejected_before_schema_walk(self) -> None:
        result = decode_manifest_primitive(
            {"oversized": 1},
            limits=DecodeLimits(max_string_length=3),
        )
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.STRING_LIMIT_EXCEEDED, result.issues[0].reason_code)
        self.assertEqual(("<oversized-key>",), result.issues[0].path)

    def test_field_boundary_matrix_returns_typed_diagnostics(self) -> None:
        cases: tuple[tuple[str, object, ReasonCode], ...] = (
            ("goal", " ", ReasonCode.EMPTY_STRING),
            ("schema_version", 2, ReasonCode.UNSUPPORTED_SCHEMA_VERSION),
            ("max_concurrency", 0, ReasonCode.INTEGER_OUT_OF_RANGE),
            ("requirements", {}, ReasonCode.WRONG_CONTAINER_TYPE),
            ("requirements", [], ReasonCode.EMPTY_COLLECTION),
            ("tasks", {}, ReasonCode.WRONG_CONTAINER_TYPE),
            ("tasks", [], ReasonCode.EMPTY_COLLECTION),
        )
        for field, invalid, reason in cases:
            with self.subTest(field=field, invalid=invalid):
                value = valid_manifest()
                value[field] = invalid
                result = decode_manifest_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(reason, _codes(result))

        task_cases: tuple[tuple[str, object, ReasonCode], ...] = (
            ("wave", 3, ReasonCode.INVALID_WAVE),
            ("may_change_contracts", "yes", ReasonCode.WRONG_PRIMITIVE_TYPE),
            ("requirement_ids", None, ReasonCode.WRONG_CONTAINER_TYPE),
            ("requirement_ids", [], ReasonCode.EMPTY_COLLECTION),
            ("requirement_ids", ["REQ-001", "REQ-001"], ReasonCode.DUPLICATE_ITEM),
            ("issue_id", 0, ReasonCode.INVALID_IDENTIFIER),
            ("issue_id", [], ReasonCode.WRONG_PRIMITIVE_TYPE),
        )
        for field, invalid, reason in task_cases:
            with self.subTest(task_field=field, invalid=invalid):
                value = valid_manifest()
                value["tasks"][0][field] = invalid
                result = decode_manifest_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(reason, _codes(result))

    def test_collection_limits_and_duplicate_record_ids_fail_closed(self) -> None:
        value = valid_manifest()
        value["protected_paths"] = [f"path-{index}" for index in range(257)]
        result = decode_manifest_primitive(value)
        self.assertIn(ReasonCode.ITEM_LIMIT_EXCEEDED, _codes(result))

        for field in ("requirements", "tasks"):
            with self.subTest(field=field):
                value = valid_manifest()
                value[field] = [copy.deepcopy(value[field][0]), copy.deepcopy(value[field][0])]
                result = decode_manifest_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(ReasonCode.DUPLICATE_IDENTIFIER, _codes(result))

    def test_invalid_approval_shapes_and_hashes_are_rejected(self) -> None:
        cases = (
            ([], ReasonCode.WRONG_CONTAINER_TYPE),
            (
                {
                    "approved_by": "architect",
                    "approved_at": "2026-08-16T10:00:00Z",
                    "artifact_hash": "sha256:BAD",
                },
                ReasonCode.INVALID_HASH,
            ),
        )
        for approval, reason in cases:
            with self.subTest(reason=reason):
                value = valid_manifest()
                value["approved"]["gate_a"] = approval
                result = decode_manifest_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(reason, _codes(result))

    def test_model_constructor_defenses_become_diagnostics(self) -> None:
        value = valid_manifest()
        issues: list[ValidationIssue] = []
        with patch.object(decoder, "GateApproval", side_effect=(None, ValueError("joint invariant"))):
            self.assertIsNone(
                decoder._decode_approval(
                    value["approved"]["gate_a"],
                    ("approved", "gate_a"),
                    issues,
                )
            )
        self.assertEqual(ReasonCode.INVALID_GATE_APPROVAL, issues[-1].reason_code)

        issues = []
        with patch.object(decoder, "Task", side_effect=ValueError("joint invariant")):
            self.assertIsNone(decoder._decode_task(value["tasks"][0], ("tasks", "TASK-001"), issues))
        self.assertEqual(ReasonCode.INVALID_TASK, issues[-1].reason_code)

        with patch.object(decoder, "ExecutionManifest", side_effect=ValueError("joint invariant")):
            manifest, manifest_issues = decoder._decode_manifest_shape(value)
        self.assertIsNone(manifest)
        self.assertEqual(ReasonCode.INVALID_MANIFEST, manifest_issues[-1].reason_code)

    def test_parser_recursion_failure_is_reported_without_escaping(self) -> None:
        with patch.object(decoder.json, "loads", side_effect=RecursionError):
            result = decode_manifest_bytes(b"{}")
        self.assertFalse(result.ok)
        self.assertEqual(ReasonCode.DEPTH_LIMIT_EXCEEDED, result.issues[0].reason_code)

    def test_public_decoders_reject_invalid_limits_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "DecodeLimits"):
            decode_manifest_primitive({}, limits=None)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "DecodeLimits"):
            decode_manifest_bytes(b"{}", limits=None)  # type: ignore[arg-type]


class ModelInvariantEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        result = decode_manifest_primitive(valid_manifest())
        if not result.ok or result.value is None:
            raise AssertionError(result.report.render_text())
        cls.manifest = result.value

    def test_scalar_strings_reject_wrong_empty_oversized_and_surrogate_values(self) -> None:
        for invalid, message in (
            (17, "non-empty string"),
            (" \t", "non-empty string"),
            ("x" * 4097, "string limit"),
            ("bad\ud800", "valid Unicode"),
        ):
            with self.subTest(invalid_type=type(invalid).__name__, message=message):
                with self.assertRaisesRegex(ValueError, message):
                    dataclasses.replace(self.manifest, goal=invalid)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "stable uppercase ID"):
            dataclasses.replace(self.manifest, run_id="bad-id")

    def test_tuple_and_identifier_boundaries_are_enforced_directly(self) -> None:
        task = self.manifest.tasks[0]
        for updates, error, message in (
            ({"owned_paths": ["src/**"]}, TypeError, "must be a tuple"),
            ({"owned_paths": ()}, ValueError, "must not be empty"),
            ({"documentation": tuple(f"d-{i}" for i in range(257))}, ValueError, "item limit"),
            ({"issue_id": 0}, ValueError, "positive integer"),
            ({"issue_id": object()}, TypeError, "integer, string, or null"),
        ):
            with self.subTest(updates=tuple(updates)):
                with self.assertRaisesRegex(error, message):
                    dataclasses.replace(task, **updates)
        self.assertEqual("ISSUE-7", dataclasses.replace(task, issue_id="ISSUE-7").issue_id)

    def test_timestamp_hash_and_approval_types_are_closed(self) -> None:
        approval = self.manifest.approvals.gate_a
        assert approval is not None
        with self.assertRaisesRegex(ValueError, "valid timestamp"):
            dataclasses.replace(approval, approved_at="2026-02-30T10:00:00Z")
        with self.assertRaisesRegex(ValueError, "full sha256"):
            dataclasses.replace(approval, artifact_hash="sha256:BAD")
        with self.assertRaisesRegex(TypeError, "gate_a"):
            ApprovalSet(gate_a="approved")  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "gate_b"):
            ApprovalSet(gate_b="approved")  # type: ignore[arg-type]

    def test_task_closed_domain_fields_reject_lookalike_values(self) -> None:
        task = self.manifest.tasks[0]
        cases = (
            ({"wave": True}, ValueError, "wave"),
            ({"risk": "medium"}, TypeError, "RiskLevel"),
            ({"status": "approved"}, TypeError, "TaskStatus"),
            ({"may_change_contracts": 1}, TypeError, "bool"),
        )
        for updates, error, message in cases:
            with self.subTest(updates=tuple(updates)):
                with self.assertRaisesRegex(error, message):
                    dataclasses.replace(task, **updates)

    def test_manifest_aggregate_rejects_every_invalid_container_boundary(self) -> None:
        requirement = self.manifest.requirements[0]
        task = self.manifest.tasks[0]
        cases = (
            ({"schema_version": True}, ValueError, "schema_version"),
            ({"max_concurrency": 0}, ValueError, "max_concurrency"),
            ({"approvals": object()}, TypeError, "ApprovalSet"),
            ({"requirements": []}, ValueError, "non-empty tuple"),
            ({"tasks": []}, ValueError, "non-empty tuple"),
            ({"requirements": tuple(requirement for _ in range(257))}, ValueError, "item limit"),
            ({"tasks": tuple(task for _ in range(65))}, ValueError, "task limit"),
            ({"requirements": (object(),)}, TypeError, "Requirement values"),
            ({"tasks": (object(),)}, TypeError, "Task values"),
            ({"requirements": (requirement, requirement)}, ValueError, "requirement IDs"),
            ({"tasks": (task, task)}, ValueError, "task IDs"),
        )
        for updates, error, message in cases:
            with self.subTest(updates=tuple(updates)):
                with self.assertRaisesRegex(error, message):
                    dataclasses.replace(self.manifest, **updates)
        self.assertIs(self.manifest.approvals, self.manifest.approved)

    def test_valid_direct_models_cover_optional_closed_values(self) -> None:
        requirement = Requirement("REQ-002", "Implemented", RequirementStatus.IMPLEMENTED)
        self.assertEqual("REQ-002", requirement.id)
        task = dataclasses.replace(
            self.manifest.tasks[0],
            risk=RiskLevel.HIGH,
            status=TaskStatus.READY,
            issue_id="ISSUE-9",
        )
        rebuilt = ExecutionManifest(
            schema_version=1,
            run_id="WISH-002",
            goal="Rebuilt",
            base_branch="main",
            requirements=(requirement,),
            tasks=(task,),
        )
        self.assertEqual("ISSUE-9", rebuilt.tasks[0].issue_id)


class DiagnosticAndSerializationEdgeTests(unittest.TestCase):
    def test_validation_issue_rejects_malformed_metadata(self) -> None:
        cases = (
            ({"stage": "local"}, TypeError),
            ({"severity": "error"}, TypeError),
            ({"reason_code": "invalid_manifest"}, TypeError),
            ({"rule_id": "Bad Rule"}, ValueError),
            ({"message": ""}, ValueError),
            ({"message": "x" * 513}, ValueError),
            ({"message": "bad\ud800"}, ValueError),
            ({"path": ["field"]}, TypeError),
            ({"path": (object(),)}, TypeError),
            ({"path": (2**63,)}, ValueError),
            ({"related_paths": []}, TypeError),
        )
        for overrides, error in cases:
            with self.subTest(overrides=tuple(overrides)):
                with self.assertRaises(error):
                    _issue(**overrides)

    def test_diagnostic_compatibility_helpers_and_safe_rendering(self) -> None:
        issue = _issue(path=("a/b~c",), message="line one\rline two")
        self.assertEqual("contract.edge", issue.rule)
        self.assertEqual(issue.sort_key(), issue._dedup_preference())
        self.assertEqual("/a~1b~0c", issue.path_text)
        self.assertIn(r"line one\nline two", ValidationReport((issue,)).render_text())
        self.assertEqual("\\r", __import__("wish_builder.contracts.diagnostics", fromlist=["_render_safe_text"])._render_safe_text("\r"))

    def test_reports_reject_invalid_containers_and_canonicalize_forged_limits(self) -> None:
        with self.assertRaises(TypeError):
            ValidationReport([])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ValidationReport((object(),))  # type: ignore[arg-type]

        forged = _issue(
            stage=ValidationStage.CAPABILITY,
            rule_id="validation.issue_limit",
            path=(),
            reason_code=ReasonCode.VALIDATION_ISSUE_LIMIT,
            message="not a canonical marker",
        )
        report = ValidationReport((forged,))
        self.assertEqual("Validation omitted 1 additional issue(s).", report.issues[0].message)
        self.assertEqual(report.issues, report.diagnostics)

        combined = combine_reports(ValidationReport(), ValidationReport((_issue(),)))
        self.assertEqual(1, len(combined.issues))

    def test_decode_result_never_exposes_an_inconsistent_state(self) -> None:
        ok = ValidationReport()
        invalid = ValidationReport((_issue(),))
        with self.assertRaises(TypeError):
            DecodeResult(None, object())  # type: ignore[arg-type]
        for digest in ("ABC", "A" * 64, 17):
            with self.subTest(digest=digest):
                with self.assertRaises(ValueError):
                    DecodeResult("value", ok, digest)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "invalid decode"):
            DecodeResult("value", invalid)
        with self.assertRaisesRegex(ValueError, "successful decode"):
            DecodeResult(None, ok)

        result = DecodeResult("value", ok, "a" * 64)
        self.assertTrue(result.ok)
        self.assertEqual(result.issues, result.diagnostics)
        self.assertEqual(ok.to_json_bytes(), result.diagnostic_bytes())
        self.assertEqual(ok.sha256(), result.diagnostic_sha256())

    def test_canonical_serializer_rejects_unsafe_graphs_and_scalars(self) -> None:
        nested: object = None
        for _ in range(130):
            nested = [nested]
        cyclic_list: list[object] = []
        cyclic_list.append(cyclic_list)
        cyclic_dict: dict[str, object] = {}
        cyclic_dict["self"] = cyclic_dict
        cases = (
            ("invalid unicode", {"value": "bad\ud800"}, ValueError),
            ("depth", nested, ValueError),
            ("integer", 2**63, ValueError),
            ("list cycle", cyclic_list, ValueError),
            ("dict cycle", cyclic_dict, ValueError),
            ("key type", {1: "value"}, TypeError),
            ("normalized key collision", {"caf\u00e9": 1, "cafe\u0301": 2}, ValueError),
            ("unsupported", object(), TypeError),
        )
        for name, value, error in cases:
            with self.subTest(name=name):
                with self.assertRaises(error):
                    canonical_json_bytes(value)
        self.assertEqual(b'["tuple",true]\n', canonical_json_bytes(("tuple", True)))


if __name__ == "__main__":
    unittest.main()
