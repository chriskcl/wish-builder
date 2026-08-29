from __future__ import annotations

import dataclasses
import unittest

import wish_builder.contracts.manifest_v2_decoder as manifest_decoder
import wish_builder.contracts.qualification_evidence as evidence_contract
from tests.contracts.test_manifest_v2 import valid_manifest_v2
from tests.contracts.test_qualification_evidence import (
    DIGEST_A,
    DIGEST_B,
    _events,
    _evidence_objects,
)
from wish_builder.contracts import (
    ExecutionIdentity,
    QualificationEvidenceRole,
    QualificationEvidenceScenario,
    QualificationProvenanceSubject,
    decode_manifest_v2_primitive,
)
from wish_builder.contracts.task_packet import generated_task_packet_bytes


class QualificationEvidenceBoundaryCoverageTests(unittest.TestCase):
    def test_scalar_and_collection_helpers_fail_closed(self) -> None:
        invalid_calls = (
            (lambda: evidence_contract._token("bad token", "value"), ValueError),
            (lambda: evidence_contract._digest("bad", "value"), ValueError),
            (lambda: evidence_contract._revision("ABC", "value"), ValueError),
            (lambda: evidence_contract._sha1("bad", "value"), ValueError),
            (lambda: evidence_contract._semver("v1", "value"), ValueError),
            (lambda: evidence_contract._timestamp("not-a-time", "value"), ValueError),
            (
                lambda: evidence_contract._timestamp(
                    "2026-02-30T00:00:00Z", "value"
                ),
                ValueError,
            ),
            (lambda: evidence_contract._relative_path("/root", "value"), ValueError),
            (lambda: evidence_contract._relative_path("a/../b", "value"), ValueError),
            (lambda: evidence_contract._owned_paths([], "value"), TypeError),
            (lambda: evidence_contract._owned_paths((), "value"), ValueError),
            (
                lambda: evidence_contract._owned_paths(
                    tuple(f"p-{index}" for index in range(257)), "value"
                ),
                ValueError,
            ),
            (
                lambda: evidence_contract._owned_paths(("same", "same"), "value"),
                ValueError,
            ),
            (lambda: evidence_contract._resource_ids([], "value"), TypeError),
            (lambda: evidence_contract._resource_ids((), "value"), ValueError),
            (
                lambda: evidence_contract._resource_ids(
                    tuple(f"r-{index}" for index in range(257)), "value"
                ),
                ValueError,
            ),
            (
                lambda: evidence_contract._resource_ids(("same", "same"), "value"),
                ValueError,
            ),
            (lambda: evidence_contract._https_url("http://example.com", "value"), ValueError),
            (lambda: evidence_contract._https_url("https:///path", "value"), ValueError),
            (
                lambda: evidence_contract._https_url(
                    "https://user@example.com", "value"
                ),
                ValueError,
            ),
            (
                lambda: evidence_contract._https_url(
                    "https://example.com/#fragment", "value"
                ),
                ValueError,
            ),
            (lambda: evidence_contract._positive_integer(0, "value"), ValueError),
            (lambda: evidence_contract._nonnegative_integer(-1, "value"), ValueError),
            (lambda: evidence_contract._boolean(1, "value"), TypeError),
            (
                lambda: evidence_contract._enum(
                    "full_turn", QualificationEvidenceScenario, "value"
                ),
                TypeError,
            ),
        )
        for call, error in invalid_calls:
            with self.subTest(call=call), self.assertRaises(error):
                call()

        self.assertEqual(
            (), evidence_contract._resource_ids((), "value", allow_empty=True)
        )

    def test_event_and_event_log_constructor_guards(self) -> None:
        event = _events()[0]
        for changes, error in (
            ({"schema_version": 2}, ValueError),
            ({"payload": _events()[-1].payload}, TypeError),
            ({"previous_event_digest": DIGEST_B}, ValueError),
            ({"event_digest": DIGEST_B}, ValueError),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                dataclasses.replace(event, **changes)

        with self.assertRaises(TypeError):
            evidence_contract.qualification_event_digest(object())
        with self.assertRaises(ValueError):
            evidence_contract.qualification_event_digest({})
        for events, error in (
            ([], ValueError),
            ((), ValueError),
            ((object(),), TypeError),
            ((_events()[1],), ValueError),
            ((_events()[0],) * (evidence_contract.MAX_QUALIFICATION_EVENTS + 1), ValueError),
        ):
            with self.subTest(events_type=type(events).__name__), self.assertRaises(error):
                evidence_contract.qualification_event_log_bytes(events)  # type: ignore[arg-type]

    def test_evidence_inventory_harness_and_provenance_guards(self) -> None:
        inventory, harness, provenance = _evidence_objects()
        first = inventory.artifacts[0]
        for changes, error in (
            ({"byte_length": evidence_contract.MAX_QUALIFICATION_ARTIFACT_BYTES + 1}, ValueError),
            ({"media_type": "not a media type"}, ValueError),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                dataclasses.replace(first, **changes)

        duplicate_path = dataclasses.replace(
            inventory.artifacts[-1], path=inventory.artifacts[0].path
        )
        for changes, error in (
            ({"schema_version": 2}, ValueError),
            ({"artifacts": list(inventory.artifacts)}, TypeError),
            ({"artifacts": inventory.artifacts[:-1]}, ValueError),
            ({"artifacts": inventory.artifacts[:-1] + (duplicate_path,)}, ValueError),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                dataclasses.replace(inventory, **changes)
        with self.assertRaises(TypeError):
            inventory.artifact("event_log")  # type: ignore[arg-type]

        for changes, error in (
            ({"schema_version": 2}, ValueError),
            ({"event_schema_version": 2}, ValueError),
            ({"scenarios": list(harness.scenarios)}, TypeError),
            ({"scenarios": tuple(reversed(harness.scenarios))}, ValueError),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                dataclasses.replace(harness, **changes)

        provenance_artifact = inventory.artifact(QualificationEvidenceRole.PROVENANCE)
        with self.assertRaises(ValueError):
            QualificationProvenanceSubject.from_artifact(provenance_artifact)
        with self.assertRaises(TypeError):
            QualificationProvenanceSubject.from_artifact(object())  # type: ignore[arg-type]
        for changes, error in (
            ({"schema_version": 2}, ValueError),
            ({"subjects": list(provenance.subjects)}, TypeError),
            ({"subjects": tuple(reversed(provenance.subjects))}, ValueError),
        ):
            with self.subTest(changes=changes), self.assertRaises(error):
                dataclasses.replace(provenance, **changes)
        with self.assertRaises(TypeError):
            provenance.binds_inventory(object())  # type: ignore[arg-type]


class ManifestDecoderBoundaryCoverageTests(unittest.TestCase):
    def test_scalar_array_and_argument_helpers_report_every_boundary(self) -> None:
        issues: list[object] = []
        self.assertIsNone(manifest_decoder._timestamp_value(object(), ("x",), issues))
        self.assertIsNone(manifest_decoder._snapshot_id_value(object(), "TASK", ("x",), issues))

        array_cases = (
            (manifest_decoder._MISSING, True, True, True),
            ({}, True, True, True),
            ([], True, True, True),
            (["x"] * 257, False, False, False),
            ([1], False, False, True),
            (["same", "same"], False, True, True),
        )
        for value, nonempty, unique, expect_none in array_cases:
            with self.subTest(value_type=type(value).__name__, nonempty=nonempty):
                local: list[object] = []
                result = manifest_decoder._string_array(
                    value,
                    ("x",),
                    local,
                    nonempty=nonempty,
                    unique=unique,
                )
                self.assertEqual(expect_none, result is None)
                self.assertTrue(local or value is manifest_decoder._MISSING)

        argument_cases = (
            (manifest_decoder._MISSING, True),
            ({}, True),
            ([], True),
            (["x"] * 4097, False),
            ([1], True),
            (["bad\ud800"], True),
        )
        for value, expect_none in argument_cases:
            with self.subTest(argument_type=type(value).__name__):
                local = []
                result = manifest_decoder._argument_array(
                    value, ("argv",), local
                )
                self.assertEqual(expect_none, result is None)
                self.assertTrue(local or value is manifest_decoder._MISSING)

        local = []
        self.assertIsNone(manifest_decoder._hash_array(manifest_decoder._MISSING, ("x",), local))
        local = []
        self.assertIsNone(manifest_decoder._hash_array(["bad"], ("x",), local))
        local = []
        self.assertIsNone(
            manifest_decoder._snapshot_id_array(
                ["bad"], "TASK", ("x",), local, nonempty=True
            )
        )

    def test_nested_decoder_short_circuits_and_limits(self) -> None:
        helpers = (
            lambda issues: manifest_decoder._decode_null_gate_approval(
                object(), ("gate",), issues
            ),
            lambda issues: manifest_decoder._decode_command_list(
                manifest_decoder._MISSING, ("commands",), issues
            ),
            lambda issues: manifest_decoder._decode_command_list(
                object(), ("commands",), issues
            ),
            lambda issues: manifest_decoder._decode_command_list(
                [], ("commands",), issues
            ),
            lambda issues: manifest_decoder._decode_budget(
                object(), ("budget",), issues
            ),
            lambda issues: manifest_decoder._decode_requirement(
                object(), ("requirement",), issues
            ),
            lambda issues: manifest_decoder._decode_task(
                object(), ("task",), issues
            ),
            lambda issues: manifest_decoder._decode_mapping(
                manifest_decoder._MISSING, ("mapping",), issues
            ),
            lambda issues: manifest_decoder._decode_mapping(
                object(), ("mapping",), issues
            ),
            lambda issues: manifest_decoder._decode_mapping(
                {}, ("mapping",), issues
            ),
        )
        for helper in helpers:
            local: list[object] = []
            with self.subTest(helper=helper):
                self.assertIsNone(helper(local))

        budget = valid_manifest_v2()["execution_budget"].copy()
        budget["max_concurrent_workers"] = 65
        issues: list[object] = []
        self.assertIsNone(
            manifest_decoder._decode_budget(budget, ("budget",), issues)
        )
        self.assertTrue(issues)


class TaskPacketBoundaryCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        decoded = decode_manifest_v2_primitive(valid_manifest_v2())
        if not decoded.ok or decoded.value is None:
            raise AssertionError(decoded.report.render_text())
        cls.manifest = decoded.value
        cls.task = cls.manifest.tasks[0]
        cls.trellis_task_id = next(
            item.trellis_task_id
            for item in cls.manifest.task_id_mapping
            if item.task_id == cls.task.id
        )
        cls.identity = ExecutionIdentity(
            cls.manifest.run_id,
            1,
            cls.task.id,
            1,
            "DISPATCH-BOUNDARY",
        )

    def test_packet_rejects_every_mismatched_boundary(self) -> None:
        other_task = dataclasses.replace(self.task, title="Another task")
        cases = (
            (object(), self.task, self.trellis_task_id, self.identity, TypeError),
            (self.manifest, other_task, self.trellis_task_id, self.identity, ValueError),
            (self.manifest, self.task, "", self.identity, ValueError),
            (self.manifest, self.task, self.trellis_task_id, object(), ValueError),
            (
                self.manifest,
                self.task,
                self.trellis_task_id,
                ExecutionIdentity(self.manifest.run_id, 1),
                ValueError,
            ),
            (
                self.manifest,
                self.task,
                self.trellis_task_id,
                ExecutionIdentity("OTHER-RUN", 1, self.task.id, 1, "DISPATCH-X"),
                ValueError,
            ),
            (self.manifest, self.task, "wrong-task", self.identity, ValueError),
        )
        for manifest, task, trellis_id, identity, error in cases:
            with self.subTest(error=error), self.assertRaises(error):
                generated_task_packet_bytes(
                    manifest, task, trellis_id, identity  # type: ignore[arg-type]
                )


if __name__ == "__main__":
    unittest.main()
