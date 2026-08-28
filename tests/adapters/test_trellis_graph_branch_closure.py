from __future__ import annotations

import copy
import dataclasses
import unittest
from unittest.mock import patch

from tests.adapters.test_trellis_graph_import import (
    assert_import_error,
    payload,
    settings,
    snapshot,
)
from wish_builder.adapters.trellis import graph
from wish_builder.contracts.models import MAX_COLLECTION_ITEMS, MAX_PATH_LENGTH


class TrellisGraphBranchClosureTests(unittest.TestCase):
    def test_import_boundary_rejects_wrong_runtime_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "snapshot must be"):
            graph.import_trellis_snapshot(object(), settings())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "settings must be"):
            graph.import_trellis_snapshot(snapshot(), object())  # type: ignore[arg-type]

    def test_import_settings_validate_all_nominal_types(self) -> None:
        valid = settings()
        cases = (
            ("gate_a", None, "gate_a"),
            ("provider", "codex", "provider"),
            ("execution_budget", None, "execution_budget"),
            ("path_case_mode", "insensitive", "path_case_mode"),
            ("max_concurrency", True, "max_concurrency"),
            ("protected_paths", ["src/**"], "protected_paths"),
            ("protected_paths", ("src/**", 1), "protected_paths"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value), self.assertRaisesRegex(
                TypeError, message
            ):
                dataclasses.replace(valid, **{field: value})

    def test_import_result_validates_manifest_digest_and_flag(self) -> None:
        imported = graph.import_trellis_snapshot(snapshot(), settings())
        with self.assertRaisesRegex(TypeError, "manifest"):
            graph.TrellisImportResult(None, imported.trellis_graph_digest, False)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "full sha256"):
            graph.TrellisImportResult(imported.manifest, "short", False)
        with self.assertRaisesRegex(TypeError, "boolean"):
            graph.TrellisImportResult(
                imported.manifest,
                imported.trellis_graph_digest,
                0,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "digests must match"):
            graph.TrellisImportResult(
                imported.manifest,
                "sha256:" + "f" * 64,
                False,
            )

    def test_empty_and_oversized_root_collections_are_rejected(self) -> None:
        empty_tasks = payload()
        empty_tasks["tasks"] = []
        assert_import_error(self, "empty_tasks", snapshot(empty_tasks))

        empty_requirements = payload()
        empty_requirements["requirements"] = []
        assert_import_error(self, "empty_requirements", snapshot(empty_requirements))

        too_many_requirements = payload()
        too_many_requirements["requirements"] = [None] * (MAX_COLLECTION_ITEMS + 1)
        assert_import_error(
            self,
            "requirement_limit_exceeded",
            snapshot(too_many_requirements),
        )

    def test_closed_container_and_primitive_helpers_fail_closed(self) -> None:
        cases = (
            (
                lambda: graph._closed_object(
                    [], path=("root",), allowed=set(), required=frozenset()
                ),
                "wrong_container_type",
            ),
            (
                lambda: graph._closed_object(
                    {},
                    path=("root",),
                    allowed={"required"},
                    required=frozenset({"required"}),
                ),
                "missing_field",
            ),
            (lambda: graph._list_value({}, ("items",)), "wrong_container_type"),
            (lambda: graph._exact_int(True, ("count",)), "wrong_primitive_type"),
            (lambda: graph._text(1, ("text",)), "wrong_primitive_type"),
            (lambda: graph._text("   ", ("text",)), "empty_string"),
            (
                lambda: graph._text("x" * (MAX_PATH_LENGTH + 1), ("text",), limit=MAX_PATH_LENGTH),
                "string_limit_exceeded",
            ),
            (lambda: graph._text("\ud800", ("text",)), "invalid_unicode_scalar"),
            (lambda: graph._text("bad\x00value", ("text",)), "disallowed_contract_control"),
        )
        for invoke, code in cases:
            with self.subTest(code=code), self.assertRaises(graph.TrellisGraphImportError) as raised:
                invoke()
            self.assertEqual(code, raised.exception.code)

    def test_requirement_and_dependency_duplicates_are_rejected(self) -> None:
        duplicate_requirement = payload()
        duplicate_requirement["requirements"][1]["id"] = "REQ-001"
        assert_import_error(
            self,
            "duplicate_requirement_id",
            snapshot(duplicate_requirement),
        )

        duplicate_dependency = payload()
        duplicate_dependency["tasks"][0]["depends_on"] = [
            "trellis/task-alpha",
            "trellis/task-alpha",
        ]
        assert_import_error(
            self,
            "duplicate_dependency",
            snapshot(duplicate_dependency),
        )

    def test_non_string_requirement_status_reaches_manifest_admission(self) -> None:
        value = payload()
        value["requirements"][0]["status"] = None
        assert_import_error(self, "wrong_primitive_type", snapshot(value))

    def test_manifest_decoder_rejection_is_reported_as_import_error(self) -> None:
        value = payload()
        value["tasks"][0]["title"] = 7
        assert_import_error(self, "wrong_primitive_type", snapshot(value))

    def test_manifest_digest_drift_after_admission_is_rejected(self) -> None:
        admit_manifest = graph._admit_manifest

        def drift_digest(candidate: dict[str, object]):
            manifest = admit_manifest(candidate)
            if candidate["trellis_graph_digest"] != graph._ZERO_DIGEST:
                return dataclasses.replace(
                    manifest,
                    trellis_graph_digest=graph._ZERO_DIGEST,
                )
            return manifest

        with patch.object(graph, "_admit_manifest", side_effect=drift_digest):
            with self.assertRaises(graph.TrellisGraphImportError) as raised:
                graph.import_trellis_snapshot(snapshot(), settings())

        self.assertEqual("graph_digest_mismatch", raised.exception.code)

    def test_task_number_falls_back_for_external_identifiers(self) -> None:
        self.assertEqual(7, graph._task_number("TASK-007"))
        self.assertEqual(2**31, graph._task_number("TASK"))
        self.assertEqual(2**31, graph._task_number("TASK-not-a-number"))

    def test_dependency_is_not_ready_until_all_predecessors_are_visited(self) -> None:
        graph._validate_dependency_cycle(
            (
                {"id": "TASK-001", "depends_on": []},
                {"id": "TASK-002", "depends_on": []},
                {"id": "TASK-003", "depends_on": ["TASK-001", "TASK-002"]},
            )
        )

    def test_nullable_text_and_lifecycle_status_paths(self) -> None:
        self.assertIsNone(graph._nullable_text(None, ("revision",)))
        self.assertEqual("revision", graph._nullable_text("revision", ("revision",)))

        value = copy.deepcopy(payload())
        value["requirements"][0]["status"] = "implemented"
        imported = graph.import_trellis_snapshot(snapshot(value), settings())
        self.assertEqual("approved", imported.manifest.requirements[0].status.value)


if __name__ == "__main__":
    unittest.main()
