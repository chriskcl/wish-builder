from __future__ import annotations

import copy
import hashlib
import json
import random
import unittest

from wish_builder.contracts import (
    DecodeLimits,
    ExecutionManifest,
    ReasonCode,
    Severity,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
    decode_manifest_bytes,
    decode_manifest_primitive,
)

from .hostile_corpus import HOSTILE_RAW_BYTES


class ExplosiveKey:
    def __hash__(self) -> int:
        return hash("id")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("hostile key comparison must not run")

    def __repr__(self) -> str:
        raise AssertionError("hostile key repr must not run")


class ExplosiveValue:
    def __eq__(self, other: object) -> bool:
        raise AssertionError("hostile value comparison must not run")

    def __lt__(self, other: object) -> bool:
        raise AssertionError("hostile value ordering must not run")

    def __repr__(self) -> str:
        raise AssertionError("hostile value repr must not run")


def valid_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "WISH-001",
        "goal": "Ship an observable outcome",
        "base_branch": "main",
        "max_concurrency": 3,
        "protected_paths": ["src/contracts/**"],
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
            {"id": "REQ-001", "text": "Foundation exists", "status": "implemented"}
        ],
        "tasks": [
            {
                "id": "TASK-001",
                "title": "Foundation",
                "requirement_ids": ["REQ-001"],
                "depends_on": [],
                "owned_paths": ["src/contracts/**"],
                "allowed_auxiliary_paths": [".trellis/tasks/foundation/**"],
                "acceptance_criteria": ["Contract passes"],
                "regression_commands": ["python -m unittest"],
                "rollback": "Revert the change",
                "documentation": [],
                "wave": 0,
                "risk": "medium",
                "may_change_contracts": True,
                "issue_id": 1,
                "branch": "feat/1-foundation",
                "pr_id": None,
                "squash_commit": None,
                "agent_owner": None,
                "status": "approved",
            }
        ],
    }


def raw_manifest(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def generated_shape(randomizer: random.Random, depth: int) -> object:
    scalar_factories = (
        lambda: None,
        lambda: bool(randomizer.randrange(2)),
        lambda: randomizer.randint(-(2**65), 2**65),
        lambda: randomizer.choice((0.0, 1.25, float("nan"), float("inf"))),
        lambda: randomizer.choice(("", "text", "e\u0301", "TASK-001")),
    )
    if depth == 0 or randomizer.randrange(3) == 0:
        return randomizer.choice(scalar_factories)()
    if randomizer.randrange(2) == 0:
        return [
            generated_shape(randomizer, depth - 1)
            for _ in range(randomizer.randrange(4))
        ]
    result: dict[str, object] = {}
    for index in range(randomizer.randrange(4)):
        result[f"k{index}-{randomizer.randrange(4)}"] = generated_shape(
            randomizer, depth - 1
        )
    return result


class StrictDecoderTests(unittest.TestCase):
    def test_valid_bytes_produce_an_immutable_model_and_source_hash(self) -> None:
        raw = raw_manifest(valid_manifest())
        result = decode_manifest_bytes(raw)
        self.assertTrue(result.ok, result.report.render_text())
        self.assertIs(type(result.value), ExecutionManifest)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result.source_sha256)
        self.assertEqual(("TASK-001",), tuple(task.id for task in result.value.tasks))

    def test_curated_hostile_raw_byte_corpus_is_rejected(self) -> None:
        for case in HOSTILE_RAW_BYTES:
            with self.subTest(case.name):
                result = decode_manifest_bytes(case.raw)
                self.assertFalse(result.ok)
                self.assertIsNone(result.value)
                self.assertIn(case.expected_rule, {issue.rule_id for issue in result.issues})

    def test_raw_integer_parser_is_bounded_and_uses_stable_diagnostics(self) -> None:
        rejected = (
            b"9223372036854775808",
            b"-9223372036854775809",
            b"1" * 5_000,
        )
        for token in rejected:
            with self.subTest(length=len(token), negative=token.startswith(b"-")):
                result = decode_manifest_bytes(
                    b'{"schema_version":' + token + b"}"
                )
                self.assertFalse(result.ok)
                self.assertEqual(
                    ["json.integer_range"],
                    [issue.rule_id for issue in result.issues],
                )

        admitted_boundaries = decode_manifest_bytes(
            b"[9223372036854775807,-9223372036854775808]"
        )
        self.assertNotIn(
            "json.integer_range",
            {issue.rule_id for issue in admitted_boundaries.issues},
        )

    def test_oversized_integer_waits_for_complete_json_syntax_classification(self) -> None:
        huge = b"9" * 5_000
        valid_token = decode_manifest_bytes(
            b'{"schema_version":' + huge + b"}"
        )
        self.assertEqual(
            ["json.integer_range"],
            [issue.rule_id for issue in valid_token.issues],
        )

        malformed = (
            b'{"schema_version":' + huge + b"x}",
            b'{"schema_version":' + huge + b".}",
            b'{"schema_version":' + huge,
        )
        for raw in malformed:
            with self.subTest(suffix=raw[-2:]):
                result = decode_manifest_bytes(raw)
                self.assertEqual(
                    ["json.invalid_syntax"],
                    [issue.rule_id for issue in result.issues],
                )

    def test_unknown_fields_are_rejected_at_every_closed_object(self) -> None:
        root = valid_manifest()
        root["surprise"] = True
        nested = valid_manifest()
        nested["tasks"][0]["surprise"] = True

        root_result = decode_manifest_bytes(raw_manifest(root))
        nested_result = decode_manifest_bytes(raw_manifest(nested))

        self.assertIn("schema.unknown_field", {item.rule_id for item in root_result.issues})
        self.assertIn("schema.unknown_field", {item.rule_id for item in nested_result.issues})
        self.assertIn(("tasks", "TASK-001", "surprise"), {item.path for item in nested_result.issues})

    def test_wrong_primitives_and_containers_do_not_coerce(self) -> None:
        cases = []
        boolean_integer = valid_manifest()
        boolean_integer["schema_version"] = True
        cases.append((boolean_integer, "schema.integer_type"))
        object_array = valid_manifest()
        object_array["tasks"] = {}
        cases.append((object_array, "schema.array_type"))
        numeric_enum = valid_manifest()
        numeric_enum["tasks"][0]["status"] = 1
        cases.append((numeric_enum, "schema.enum_type"))

        for value, expected_rule in cases:
            with self.subTest(expected_rule):
                result = decode_manifest_bytes(raw_manifest(value))
                self.assertFalse(result.ok)
                self.assertIn(expected_rule, {issue.rule_id for issue in result.issues})

    def test_raw_boundary_accepts_exact_bytes_only(self) -> None:
        result = decode_manifest_bytes(bytearray(b"{}"))  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertEqual("json.raw_type", result.issues[0].rule_id)

    def test_contract_controls_are_rejected_in_raw_and_primitive_manifests(self) -> None:
        for character, escaped in (("\x00", b"\\u0000"), ("\u202e", b"\\u202e")):
            with self.subTest(codepoint=f"U+{ord(character):04X}"):
                value = valid_manifest()
                value["goal"] = f"before{character}after"

                raw = json.dumps(
                    value,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("ascii")
                self.assertIn(escaped, raw)
                for result in (
                    decode_manifest_bytes(raw),
                    decode_manifest_primitive(value),
                ):
                    self.assertFalse(result.ok)
                    self.assertIsNone(result.value)
                    self.assertEqual(
                        ["value.contract_control"],
                        [issue.rule_id for issue in result.issues],
                    )
                    self.assertEqual(
                        [ReasonCode.DISALLOWED_CONTRACT_CONTROL],
                        [issue.reason_code for issue in result.issues],
                    )

        value = valid_manifest()
        value["goal"] = "before\x00after"
        escaped_raw = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        unescaped_raw = escaped_raw.replace(b"\\u0000", b"\x00")
        syntax_result = decode_manifest_bytes(unescaped_raw)
        self.assertEqual(
            ["json.invalid_syntax"],
            [issue.rule_id for issue in syntax_result.issues],
        )

    def test_missing_fields_do_not_emit_derivative_type_noise(self) -> None:
        result = decode_manifest_bytes(b"{}")
        self.assertEqual(7, len(result.issues))
        self.assertEqual({"schema.required_field"}, {item.rule_id for item in result.issues})

    def test_invalid_python_unicode_still_produces_serializable_diagnostics(self) -> None:
        result = decode_manifest_primitive({"bad\ud800": "value\ud800"})
        self.assertFalse(result.ok)
        self.assertIn(
            "json.invalid_unicode_scalar",
            {item.rule_id for item in result.issues},
        )
        self.assertTrue(result.diagnostic_bytes().endswith(b"\n"))

    def test_shape_audit_is_total_for_cycles_aliases_and_hostile_objects(self) -> None:
        cycle: list[object] = []
        cycle.append(cycle)
        shared = {"value": 1.5}
        cases = (
            (cycle, {"json.cyclic_shape"}),
            ([shared, shared], {"json.float_not_allowed"}),
            ([{ExplosiveKey(): 1}], {"json.object_key_type"}),
            ([ExplosiveValue()], {"json.shape_type"}),
        )
        for shape, expected_rules in cases:
            with self.subTest(expected_rules=expected_rules):
                first = decode_manifest_primitive(shape)
                second = decode_manifest_primitive(shape)
                self.assertFalse(first.ok)
                self.assertTrue(
                    expected_rules.issubset({issue.rule_id for issue in first.issues})
                )
                self.assertEqual(first.diagnostic_bytes(), second.diagnostic_bytes())

        alias_result = decode_manifest_primitive([shared, shared])
        self.assertEqual(
            2,
            sum(
                issue.rule_id == "json.float_not_allowed"
                for issue in alias_result.issues
            ),
        )

    def test_container_limit_precedes_hostile_key_inspection(self) -> None:
        result = decode_manifest_primitive(
            [{ExplosiveKey(): 1}],
            limits=DecodeLimits(max_items=1),
        )
        self.assertEqual(["json.item_limit"], [issue.rule_id for issue in result.issues])

    def test_validation_issue_cap_is_exact_and_fail_closed(self) -> None:
        at_limit = decode_manifest_primitive([object() for _ in range(200)])
        self.assertEqual(200, len(at_limit.issues))
        self.assertNotIn(
            "validation.issue_limit",
            {issue.rule_id for issue in at_limit.issues},
        )

        result = decode_manifest_primitive([object() for _ in range(201)])
        self.assertFalse(result.ok)
        self.assertEqual(200, len(result.issues))
        limit_issues = [
            issue for issue in result.issues if issue.rule_id == "validation.issue_limit"
        ]
        self.assertEqual(1, len(limit_issues))
        self.assertEqual("Validation omitted 2 additional issue(s).", limit_issues[0].message)

    def test_validation_issue_boundaries_are_exact(self) -> None:
        def details(count: int) -> tuple[ValidationIssue, ...]:
            return tuple(
                ValidationIssue(
                    stage=ValidationStage.LOCAL,
                    rule_id=f"json.boundary_{index:03d}",
                    severity=Severity.ERROR,
                    path=(index,),
                    reason_code=ReasonCode.WRONG_PRIMITIVE_TYPE,
                    message=f"shape {index}",
                )
                for index in range(count)
            )

        for count, expected_issues, expected_marker in (
            (199, 199, None),
            (200, 200, None),
            (201, 200, "Validation omitted 2 additional issue(s)."),
        ):
            with self.subTest(count=count):
                report = ValidationReport(details(count))
                self.assertEqual(expected_issues, len(report.issues))
                markers = [
                    issue
                    for issue in report.issues
                    if issue.rule_id == "validation.issue_limit"
                ]
                self.assertEqual(0 if expected_marker is None else 1, len(markers))
                if expected_marker is not None:
                    self.assertEqual(expected_marker, markers[0].message)

    def test_combining_capped_reports_keeps_one_stable_limit_diagnostic(self) -> None:
        def detailed(offset: int) -> tuple[ValidationIssue, ...]:
            return tuple(
                ValidationIssue(
                    stage=ValidationStage.LOCAL,
                    rule_id=f"json.shape_{index:03d}",
                    severity=Severity.ERROR,
                    path=(offset + index,),
                    reason_code=ReasonCode.WRONG_PRIMITIVE_TYPE,
                    message=f"shape {offset + index}",
                )
                for index in range(201)
            )

        first = ValidationReport(detailed(0))
        second = ValidationReport(detailed(1_000))
        combined = ValidationReport(first.issues + second.issues)

        self.assertEqual(200, len(first.issues))
        self.assertEqual(200, len(second.issues))
        self.assertEqual(200, len(combined.issues))
        markers = [
            issue for issue in combined.issues if issue.rule_id == "validation.issue_limit"
        ]
        self.assertEqual(1, len(markers))
        self.assertEqual("Validation omitted 203 additional issue(s).", markers[0].message)
        self.assertEqual(
            combined.to_json_bytes(),
            ValidationReport(combined.issues).to_json_bytes(),
        )

    def test_forged_limit_marker_is_rewritten_to_one_canonical_error(self) -> None:
        forged = ValidationIssue(
            stage=ValidationStage.LOCAL,
            rule_id="validation.issue_limit",
            severity=Severity.WARNING,
            path=("forged",),
            reason_code=ReasonCode.INVALID_JSON,
            message="attacker\r\nnot a count",
            related_paths=(("other",),),
        )
        report = ValidationReport((forged,))
        self.assertEqual(1, len(report.issues))
        marker = report.issues[0]
        self.assertEqual(ValidationStage.CAPABILITY, marker.stage)
        self.assertEqual(Severity.ERROR, marker.severity)
        self.assertEqual((), marker.path)
        self.assertEqual(ReasonCode.VALIDATION_ISSUE_LIMIT, marker.reason_code)
        self.assertEqual("Validation omitted 1 additional issue(s).", marker.message)
        self.assertEqual(1, len(report.render_text().splitlines()))

    def test_cap_selection_is_error_first_and_output_order_is_canonical(self) -> None:
        warnings = tuple(
            ValidationIssue(
                stage=ValidationStage.LOCAL,
                rule_id=f"json.warning_{index:03d}",
                severity=Severity.WARNING,
                path=(index,),
                reason_code=ReasonCode.WRONG_PRIMITIVE_TYPE,
                message=f"warning {index}",
            )
            for index in range(201)
        )
        errors = (
            ValidationIssue(
                stage=ValidationStage.CAPABILITY,
                rule_id="json.late_error_a",
                severity=Severity.ERROR,
                path=("late", "a"),
                reason_code=ReasonCode.INVALID_JSON,
                message="blocking A",
            ),
            ValidationIssue(
                stage=ValidationStage.CAPABILITY,
                rule_id="json.late_error_b",
                severity=Severity.ERROR,
                path=("late", "b"),
                reason_code=ReasonCode.INVALID_JSON,
                message="blocking B",
            ),
        )
        report = ValidationReport(warnings + errors)
        self.assertEqual(200, len(report.issues))
        self.assertFalse(report.ok)
        self.assertEqual(
            {"json.late_error_a", "json.late_error_b"},
            {
                issue.rule_id
                for issue in report.issues
                if issue.severity is Severity.ERROR
                and issue.rule_id != "validation.issue_limit"
            },
        )
        self.assertEqual(
            "Validation omitted 4 additional issue(s).",
            next(issue.message for issue in report.issues if issue.rule_id == "validation.issue_limit"),
        )
        self.assertEqual(
            sorted(issue.sort_key() for issue in report.issues),
            [issue.sort_key() for issue in report.issues],
        )

    def test_diagnostic_nfc_aliases_collapse_before_cap_selection(self) -> None:
        aliases: list[ValidationIssue] = []
        for index in range(100):
            for spelling in ("caf\u00e9\r\nline", "cafe\u0301\nline"):
                aliases.append(
                    ValidationIssue(
                        stage=ValidationStage.LOCAL,
                        rule_id=f"value.alias_{index:03d}",
                        severity=Severity.ERROR,
                        path=("facts", spelling, index),
                        reason_code=ReasonCode.INVALID_MANIFEST,
                        message=f"{spelling} fact {index}",
                        related_paths=(("related", spelling, index),),
                    )
                )
        safety_error = ValidationIssue(
            stage=ValidationStage.CAPABILITY,
            rule_id="manifest.named_safety_error",
            severity=Severity.ERROR,
            path=("safety",),
            reason_code=ReasonCode.INVALID_MANIFEST,
            message="Named safety error.",
        )

        forward = ValidationReport(tuple(aliases) + (safety_error,))
        reverse = ValidationReport(tuple(reversed(aliases)) + (safety_error,))
        self.assertEqual(101, len(forward.issues))
        self.assertNotIn(
            "validation.issue_limit",
            {issue.rule_id for issue in forward.issues},
        )
        self.assertIn(
            "manifest.named_safety_error",
            {issue.rule_id for issue in forward.issues},
        )
        self.assertEqual(forward.to_json_bytes(), reverse.to_json_bytes())
        self.assertEqual(aliases[0].identity_key(), aliases[1].identity_key())

    def test_rendered_hostile_paths_stay_on_one_physical_line_per_issue(self) -> None:
        result = decode_manifest_primitive(
            {"hostile\r\n\t\x00\u202epath": 1}
        )
        rendered = result.report.render_text()
        self.assertNotIn("\r", rendered)
        self.assertEqual(len(result.issues), len(rendered.splitlines()))
        self.assertIn(r"\n\t\u0000\u202e", rendered)

    def test_configurable_limits_reject_before_model_admission(self) -> None:
        cases = (
            (
                b"{}",
                DecodeLimits(max_bytes=1),
                "json.byte_limit",
            ),
            (
                b"[[[]]]",
                DecodeLimits(max_depth=2),
                "json.depth_limit",
            ),
            (
                b'{"a":1,"b":2}',
                DecodeLimits(max_items=1),
                "json.item_limit",
            ),
            (
                b'{"abcd":1}',
                DecodeLimits(max_string_length=3),
                "json.string_limit",
            ),
        )
        for raw, limits, expected_rule in cases:
            with self.subTest(expected_rule):
                result = decode_manifest_bytes(raw, limits=limits)
                self.assertFalse(result.ok)
                self.assertEqual(expected_rule, result.issues[0].rule_id)

    def test_configured_depth_cannot_outgrow_bounded_diagnostics(self) -> None:
        with self.assertRaises(ValueError):
            DecodeLimits(max_depth=65)

        value: object = object()
        for _ in range(65):
            value = [value]
        result = decode_manifest_primitive(
            value,
            limits=DecodeLimits(max_depth=64, max_items=128),
        )
        self.assertEqual(
            ["json.depth_limit"],
            [issue.rule_id for issue in result.issues],
        )

    def test_limits_are_inclusive_at_the_boundary(self) -> None:
        result = decode_manifest_bytes(
            b'{"a":1}',
            limits=DecodeLimits(
                max_bytes=7,
                max_depth=1,
                max_items=1,
                max_string_length=1,
            ),
        )
        rules = {issue.rule_id for issue in result.issues}
        self.assertFalse(
            rules
            & {
                "json.byte_limit",
                "json.depth_limit",
                "json.item_limit",
                "json.string_limit",
            }
        )

    def test_python_shape_validation_is_total_and_deterministic(self) -> None:
        randomizer = random.Random(0x5EED_2101)
        shapes = [generated_shape(randomizer, 5) for _ in range(256)]
        shapes.append({"bad\ud800": "value\ud800"})
        shapes.append([10**10_000])
        shapes.append(["x" * 100_000])
        limits = DecodeLimits(max_depth=8, max_items=512, max_string_length=64)
        for index, shape in enumerate(shapes):
            with self.subTest(index=index):
                first = decode_manifest_primitive(shape, limits=limits)
                second = decode_manifest_primitive(copy.deepcopy(shape), limits=limits)
                self.assertEqual(first.diagnostic_bytes(), second.diagnostic_bytes())
                self.assertEqual(first.diagnostic_sha256(), second.diagnostic_sha256())

    def test_decoded_model_does_not_alias_the_source_mapping(self) -> None:
        source = valid_manifest()
        result = decode_manifest_primitive(source)
        self.assertTrue(result.ok, result.report.render_text())
        source["tasks"][0]["owned_paths"].append("outside/**")
        source["requirements"][0]["text"] = "Changed"
        self.assertEqual(("src/contracts/**",), result.value.tasks[0].owned_paths)
        self.assertEqual("Foundation exists", result.value.requirements[0].text)


if __name__ == "__main__":
    unittest.main()
