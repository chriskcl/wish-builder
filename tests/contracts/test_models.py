from __future__ import annotations

import copy
import dataclasses
import unittest
from pathlib import Path

from wish_builder.contracts import (
    ReasonCode,
    Requirement,
    RequirementStatus,
    Severity,
    TaskStatus,
    ValidationIssue,
    ValidationReport,
    ValidationStage,
    canonical_json_bytes,
    canonical_sha256,
    decode_manifest_primitive,
)

from .test_decoder import valid_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ContractModelTests(unittest.TestCase):
    def test_contract_aggregates_are_frozen_slotted_and_tuple_backed(self) -> None:
        result = decode_manifest_primitive(valid_manifest())
        self.assertTrue(result.ok, result.report.render_text())
        manifest = result.value
        self.assertFalse(hasattr(manifest, "__dict__"))
        self.assertIs(type(manifest.tasks), tuple)
        self.assertIs(type(manifest.tasks[0].owned_paths), tuple)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.goal = "mutated"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.tasks[0].title = "mutated"  # type: ignore[misc]

    def test_direct_construction_requires_closed_enum_and_tuple_types(self) -> None:
        with self.assertRaises(TypeError):
            Requirement("REQ-001", "text", "approved")  # type: ignore[arg-type]
        requirement = Requirement(
            "REQ-001", "text", RequirementStatus.APPROVED
        )
        self.assertEqual("approved", requirement.status.value)
        with self.assertRaises(ValueError):
            TaskStatus("failed")

    def test_direct_construction_normalizes_and_rejects_set_collisions(self) -> None:
        decoded = decode_manifest_primitive(valid_manifest())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        manifest = decoded.value

        normalized = dataclasses.replace(manifest, goal="line one\r\nline two\r")
        self.assertEqual("line one\nline two\n", normalized.goal)
        with self.assertRaises(ValueError):
            dataclasses.replace(
                manifest.tasks[0],
                owned_paths=("src/line\r\nbreak", "src/line\nbreak"),
            )
        with self.assertRaises(ValueError):
            dataclasses.replace(
                manifest.tasks[0],
                owned_paths=("src/caf\u00e9", "src/cafe\u0301"),
            )
        with self.assertRaises(ValueError):
            dataclasses.replace(
                manifest.tasks[0],
                requirement_ids=("REQ-001", "REQ-001"),
            )
        with self.assertRaises(ValueError):
            dataclasses.replace(
                manifest,
                protected_paths=("src/line\r\nbreak", "src/line\nbreak"),
            )

    def test_direct_models_reject_controls_but_accept_international_text(self) -> None:
        decoded = decode_manifest_primitive(valid_manifest())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        manifest = decoded.value

        for character in ("\x00", "\u202e"):
            with self.subTest(codepoint=f"U+{ord(character):04X}"):
                with self.assertRaisesRegex(ValueError, "disallowed contract control"):
                    dataclasses.replace(
                        manifest,
                        goal=f"before{character}after",
                    )

        admitted = dataclasses.replace(
            manifest,
            goal="\u4ea4\u4ed8 caf\u00e9\tline one\r\nline two",
        )
        self.assertEqual(
            "\u4ea4\u4ed8 caf\u00e9\tline one\nline two",
            admitted.goal,
        )

    def test_admitted_model_and_report_primitives_are_bounded_trees(self) -> None:
        decoded = decode_manifest_primitive(valid_manifest())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        report = ValidationReport(
            (
                ValidationIssue(
                    stage=ValidationStage.LOCAL,
                    rule_id="value.example",
                    severity=Severity.ERROR,
                    path=("tasks", "TASK-001"),
                    reason_code=ReasonCode.INVALID_TASK,
                    message="Example finding.",
                    related_paths=(("requirements", "REQ-001"),),
                ),
            )
        )

        def container_count(value: object) -> int:
            pending = [value]
            identities: set[int] = set()
            while pending:
                current = pending.pop()
                if type(current) is dict:
                    self.assertNotIn(id(current), identities)
                    identities.add(id(current))
                    pending.extend(current.values())
                elif type(current) is list:
                    self.assertNotIn(id(current), identities)
                    identities.add(id(current))
                    pending.extend(current)
            return len(identities)

        for primitive in (decoded.value.to_primitive(), report.to_primitive()):
            self.assertLess(container_count(primitive), 1_000)
            self.assertTrue(canonical_json_bytes(primitive).endswith(b"\n"))

        base = dict(
            stage=ValidationStage.LOCAL,
            rule_id="value.example",
            severity=Severity.ERROR,
            path=(),
            reason_code=ReasonCode.INVALID_TASK,
            message="Example finding.",
        )
        for override in (
            {"rule_id": "r" * 129},
            {"path": tuple("segment" for _ in range(65))},
            {"path": ("x" * 257,)},
            {"related_paths": tuple((index,) for index in range(65))},
        ):
            with self.subTest(field=next(iter(override))):
                with self.assertRaises(ValueError):
                    ValidationIssue(**{**base, **override})

    def test_canonical_bytes_ignore_order_for_set_like_manifest_fields(self) -> None:
        left = valid_manifest()
        left["protected_paths"] = ["z/**", "a/**"]
        left["tasks"][0]["owned_paths"] = ["z/**", "a/**"]
        left["tasks"][0]["documentation"] = ["z.md", "a.md"]
        right = copy.deepcopy(left)
        right["protected_paths"].reverse()
        right["tasks"][0]["owned_paths"].reverse()
        right["tasks"][0]["documentation"].reverse()

        first = decode_manifest_primitive(left)
        second = decode_manifest_primitive(right)
        self.assertTrue(first.ok, first.report.render_text())
        self.assertTrue(second.ok, second.report.render_text())
        self.assertEqual(
            first.value.canonical_json_bytes(),
            second.value.canonical_json_bytes(),
        )

    def test_canonical_serializer_normalizes_lf_and_rejects_floats(self) -> None:
        crlf_value = {
            "outer\r\nkey": ["one\r\ntwo", {"line": "three\rfour"}],
            "value": 1,
        }
        lf_value = {
            "outer\nkey": ["one\ntwo", {"line": "three\nfour"}],
            "value": 1,
        }
        encoded = canonical_json_bytes(crlf_value)
        self.assertEqual(canonical_json_bytes(lf_value), encoded)
        self.assertEqual(canonical_sha256(lf_value), canonical_sha256(crlf_value))
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertNotIn(b"\r\n", encoded)
        with self.assertRaises(TypeError):
            canonical_json_bytes({"value": 1.5})

    def test_manifest_hashes_match_for_crlf_and_lf_forms(self) -> None:
        crlf = valid_manifest()
        crlf["goal"] = "one\r\ntwo\r"
        lf = valid_manifest()
        lf["goal"] = "one\ntwo\n"
        first = decode_manifest_primitive(crlf)
        second = decode_manifest_primitive(lf)
        self.assertTrue(first.ok, first.report.render_text())
        self.assertTrue(second.ok, second.report.render_text())
        self.assertEqual(first.value, second.value)
        self.assertEqual(
            first.value.canonical_json_bytes(),
            second.value.canonical_json_bytes(),
        )

    def test_legacy_skill_cli_remains_byte_identical_to_package_source(self) -> None:
        package_source = REPOSITORY_ROOT / "wish_builder" / "cli" / "wishctl.py"
        skill_source = REPOSITORY_ROOT / "wish-builder" / "scripts" / "wishctl.py"
        self.assertEqual(package_source.read_bytes(), skill_source.read_bytes())


if __name__ == "__main__":
    unittest.main()
