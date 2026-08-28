from __future__ import annotations

import copy
import hashlib
import json
import unittest

from wish_builder.adapters.trellis import (
    TrellisGraphImportError,
    TrellisImportSettings,
    import_trellis_snapshot,
)
from wish_builder.contracts import (
    BillingPosture,
    ExecutionBudgetPolicy,
    GateApproval,
    PathCaseMode,
    RequirementStatus,
    WorkerProvider,
)
from wish_builder.services.ports import TrellisGraphSnapshot

FIXED_TIME = "2026-08-18T05:00:00Z"
PARENT_ID = "parent/wish-001"
REVISION = "sha256:" + "f" * 64


def digest(character: str) -> str:
    return "sha256:" + character * 64


def command() -> dict[str, object]:
    return {
        "executable_profile": "python",
        "executable_identity_digest": digest("1"),
        "argv": ["python", "-m", "unittest", "tests.adapters"],
        "working_directory": ".",
        "timeout_seconds": 120,
        "stdout_limit_bytes": 1_048_576,
        "stderr_limit_bytes": 1_048_576,
        "result_limit_bytes": 262_144,
        "environment_allowlist": ["PATH", "PYTHONUTF8"],
        "network_policy": "denied",
        "display_text": "Run adapter tests",
    }


def task(
    source_id: str,
    requirement_id: str,
    *,
    depends_on: list[str] | None = None,
    wave: int = 0,
    template: bool = False,
) -> dict[str, object]:
    return {
        "id": source_id,
        "title": f"Implement {source_id}",
        "requirement_ids": [requirement_id],
        "depends_on": list(depends_on or []),
        "owned_paths": [f"src/{requirement_id.lower()}/**"],
        "allowed_auxiliary_paths": [f"tests/{requirement_id.lower()}/**"],
        "acceptance_criteria": [f"{requirement_id} is covered"],
        "regression_commands": [command()],
        "rollback": "Revert the squash commit",
        "documentation": [f"docs/{requirement_id.lower()}.md"],
        "wave": wave,
        "risk": "medium",
        "may_change_contracts": wave == 0,
        "instruction_context_digest": None if template else digest("2"),
        "approved_document_digests": [] if template else [digest("3"), digest("4")],
        "task_packet_template_digest": digest("5") if template else None,
    }


def payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "parent_task_id": PARENT_ID,
        "revision": REVISION,
        "requirements": [
            {
                "id": "REQ-001",
                "text": "Freeze the shared contract",
                "status": "approved",
                "decision_ref": None,
            },
            {
                "id": "REQ-002",
                "text": "Import Trellis tasks",
                "status": "approved",
                "decision_ref": None,
            },
        ],
        "tasks": [
            task("trellis/task-zeta", "REQ-002", depends_on=["trellis/task-alpha"], wave=1, template=True),
            task("trellis/task-alpha", "REQ-001"),
        ],
    }


def settings(
    *,
    imported_at: str = FIXED_TIME,
    provider: WorkerProvider = WorkerProvider.CODEX,
    lease_ttl_seconds: int = 90,
    lease_clock_skew_seconds: int = 2,
) -> TrellisImportSettings:
    budget = ExecutionBudgetPolicy(
        max_attempts_per_task=2,
        max_attempts_per_run=4,
        attempt_deadline_seconds=1_800,
        total_worker_seconds=7_200,
        max_output_bytes=8_388_608,
        max_retained_evidence_bytes=16_777_216,
        max_concurrent_workers=2,
        billing_posture=BillingPosture.PREAPPROVED,
    )
    return TrellisImportSettings(
        run_id="WISH-2026-001",
        goal="Deliver a Trellis-backed workflow",
        base_branch="main",
        imported_at=imported_at,
        gate_a=GateApproval(
            approved_by="architect",
            approved_at="2026-08-18T04:00:00Z",
            artifact_hash=digest("a"),
        ),
        provider=provider,
        capability_digest=digest("b"),
        launch_profile_digest=digest("c"),
        policy_digest=digest("d"),
        execution_budget=budget,
        max_concurrency=2,
        lease_ttl_seconds=lease_ttl_seconds,
        lease_clock_skew_seconds=lease_clock_skew_seconds,
        path_case_mode=PathCaseMode.INSENSITIVE,
        protected_paths=("db/schema/**", "src/contracts/**"),
    )


def snapshot(
    value: object | None = None,
    *,
    raw: bytes | None = None,
    export_version: str = "wish-builder.trellis-graph.v1",
    complete: bool = True,
    parent_task_id: str = PARENT_ID,
    revision: str | None = REVISION,
    trellis_version: str = "0.6.15",
) -> TrellisGraphSnapshot:
    if raw is None:
        raw = json.dumps(
            payload() if value is None else value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    return TrellisGraphSnapshot(
        export_version=export_version,
        trellis_version=trellis_version,
        parent_task_id=parent_task_id,
        revision=revision,
        observed_at=FIXED_TIME,
        snapshot_bytes=raw,
        source_sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        complete=complete,
    )


def assert_import_error(
    case: unittest.TestCase,
    expected_code: str,
    candidate: TrellisGraphSnapshot,
) -> TrellisGraphImportError:
    with case.assertRaises(TrellisGraphImportError) as raised:
        import_trellis_snapshot(candidate, settings())
    case.assertEqual(expected_code, raised.exception.code)
    return raised.exception


class TrellisGraphImportTests(unittest.TestCase):
    def test_same_inputs_produce_identical_manifest_mapping_and_digest(self) -> None:
        candidate = snapshot()
        first = import_trellis_snapshot(candidate, settings())
        second = import_trellis_snapshot(candidate, settings())

        self.assertEqual(first, second)
        self.assertEqual(
            first.manifest.canonical_json_bytes(),
            second.manifest.canonical_json_bytes(),
        )
        self.assertEqual(
            {
                "trellis/task-alpha": "TASK-001",
                "trellis/task-zeta": "TASK-002",
            },
            first.manifest.to_primitive()["task_id_mapping"],
        )
        self.assertEqual(first.trellis_graph_digest, first.manifest.trellis_graph_digest)
        self.assertEqual(90, first.manifest.lease_ttl_seconds)
        self.assertEqual(2, first.manifest.lease_clock_skew_seconds)
        self.assertFalse(first.gate_b_invalidated)
        self.assertEqual(
            {
                "approved_by": None,
                "approved_at": None,
                "artifact_hash": None,
            },
            first.manifest.to_primitive()["approved"]["gate_b"],
        )

    def test_record_and_set_like_input_order_do_not_change_output(self) -> None:
        left = payload()
        right = copy.deepcopy(left)
        right["requirements"].reverse()
        right["tasks"].reverse()
        for item in right["tasks"]:
            item["owned_paths"].reverse()
            item["allowed_auxiliary_paths"].reverse()
            item["documentation"].reverse()
            item["approved_document_digests"].reverse()
            item["regression_commands"][0]["environment_allowlist"].reverse()

        first = import_trellis_snapshot(snapshot(left), settings())
        second = import_trellis_snapshot(snapshot(right), settings())
        self.assertEqual(first.trellis_graph_digest, second.trellis_graph_digest)
        self.assertEqual(
            first.manifest.canonical_json_bytes(),
            second.manifest.canonical_json_bytes(),
        )

    def test_task_ids_use_nfc_and_unsigned_utf8_byte_order(self) -> None:
        value = payload()
        value["tasks"] = [
            task("trellis/e\u0301", "REQ-002", depends_on=["trellis/z"]),
            task("trellis/z", "REQ-001"),
        ]
        result = import_trellis_snapshot(snapshot(value), settings())
        self.assertEqual(
            {
                "trellis/z": "TASK-001",
                "trellis/\u00e9": "TASK-002",
            },
            result.manifest.to_primitive()["task_id_mapping"],
        )
        self.assertEqual(("TASK-001",), result.manifest.tasks[1].depends_on)

    def test_canonically_equivalent_task_ids_collide(self) -> None:
        value = payload()
        value["tasks"] = [
            task("trellis/caf\u00e9", "REQ-001"),
            task("trellis/cafe\u0301", "REQ-002"),
        ]
        error = assert_import_error(self, "duplicate_task_id", snapshot(value))
        self.assertEqual(("tasks", "trellis/caf\u00e9", "id"), error.path)

    def test_dependency_failures_are_rejected_deterministically(self) -> None:
        missing = payload()
        missing["tasks"][0]["depends_on"] = ["trellis/missing"]
        assert_import_error(self, "missing_dependency", snapshot(missing))

        self_edge = payload()
        self_edge["tasks"][0]["depends_on"] = ["trellis/task-zeta"]
        assert_import_error(self, "self_dependency", snapshot(self_edge))

        cycle = payload()
        cycle["tasks"][1]["depends_on"] = ["trellis/task-zeta"]
        assert_import_error(self, "dependency_cycle", snapshot(cycle))

    def test_shared_kernel_policy_rejects_invalid_projected_graphs(self) -> None:
        cases: list[tuple[str, str, dict[str, object]]] = []

        wave_barrier = payload()
        wave_barrier["tasks"][0]["depends_on"] = []
        cases.append(("wave barrier", "wave_barrier_bypass", wave_barrier))

        unordered_wave_zero = payload()
        unordered_wave_zero["tasks"][0].update(
            depends_on=[],
            wave=0,
            may_change_contracts=True,
        )
        cases.append(
            ("unordered Wave 0", "unordered_serial_wave", unordered_wave_zero)
        )

        unordered_wave_two = payload()
        unordered_wave_two["tasks"] = [
            task("trellis/task-alpha", "REQ-001"),
            task(
                "trellis/task-beta",
                "REQ-002",
                depends_on=["trellis/task-alpha"],
                wave=1,
            ),
            task(
                "trellis/task-gamma",
                "REQ-002",
                depends_on=["trellis/task-beta"],
                wave=2,
            ),
            task(
                "trellis/task-delta",
                "REQ-002",
                depends_on=["trellis/task-beta"],
                wave=2,
            ),
        ]
        cases.append(
            ("unordered Wave 2", "unordered_serial_wave", unordered_wave_two)
        )

        writable_overlap = payload()
        overlapping_task = task(
            "trellis/task-beta",
            "REQ-002",
            depends_on=["trellis/task-alpha"],
            wave=1,
        )
        overlapping_task["owned_paths"] = ["src/independent/**"]
        overlapping_task["allowed_auxiliary_paths"] = ["src/req-002/**"]
        writable_overlap["tasks"].append(overlapping_task)
        cases.append(
            (
                "complete writable-set overlap",
                "parallel_ownership_conflict",
                writable_overlap,
            )
        )

        unsafe_path = payload()
        unsafe_path["tasks"][1]["owned_paths"] = ["../outside/**"]
        cases.append(("unsafe path", "invalid_ownership_scope", unsafe_path))

        later_contract_change = payload()
        later_contract_change["tasks"][0]["may_change_contracts"] = True
        cases.append(
            (
                "contract change outside Wave 0",
                "contract_change_outside_wave_zero",
                later_contract_change,
            )
        )

        for name, expected_code, value in cases:
            with self.subTest(name=name):
                assert_import_error(self, expected_code, snapshot(value))

    def test_orphan_approved_requirement_and_task_are_rejected(self) -> None:
        orphan_requirement = payload()
        orphan_requirement["tasks"][1]["requirement_ids"] = ["REQ-002"]
        assert_import_error(
            self,
            "orphan_requirement",
            snapshot(orphan_requirement),
        )

        orphan_task = payload()
        orphan_task["requirements"][1].update(
            status="deferred",
            decision_ref="DECISION-7",
        )
        assert_import_error(self, "orphan_task", snapshot(orphan_task))

    def test_more_than_64_tasks_fails_before_record_validation(self) -> None:
        value = payload()
        value["tasks"] = [None] * 65
        value["requirements"] = "not-an-array"
        error = assert_import_error(self, "task_limit_exceeded", snapshot(value))
        self.assertEqual(("tasks",), error.path)

    def test_task_identity_and_topology_fail_before_requirement_validation(self) -> None:
        duplicate = payload()
        duplicate["tasks"][1]["id"] = duplicate["tasks"][0]["id"]
        duplicate["requirements"] = "not-an-array"
        assert_import_error(self, "duplicate_task_id", snapshot(duplicate))

        cycle = payload()
        cycle["tasks"][1]["depends_on"] = ["trellis/task-zeta"]
        cycle["requirements"] = "not-an-array"
        assert_import_error(self, "dependency_cycle", snapshot(cycle))

    def test_export_envelope_and_payload_versions_are_strict(self) -> None:
        assert_import_error(
            self,
            "unsupported_export_version",
            snapshot(export_version="2.0"),
        )
        assert_import_error(self, "incomplete_export", snapshot(complete=False))

        value = payload()
        value["schema_version"] = 2
        assert_import_error(self, "unsupported_schema_version", snapshot(value))

        error = assert_import_error(
            self,
            "unsupported_trellis_version",
            snapshot(trellis_version="0.7.0-dev.2"),
        )
        self.assertEqual(("trellis_version",), error.path)

    def test_payload_parent_and_revision_must_match_envelope(self) -> None:
        wrong_parent = payload()
        wrong_parent["parent_task_id"] = "parent/other"
        assert_import_error(
            self,
            "envelope_payload_mismatch",
            snapshot(wrong_parent),
        )

        wrong_revision = payload()
        wrong_revision["revision"] = "revision-other"
        error = assert_import_error(
            self,
            "envelope_payload_mismatch",
            snapshot(wrong_revision),
        )
        self.assertEqual(("revision",), error.path)

    def test_hostile_json_and_unknown_fields_fail_closed(self) -> None:
        cases = (
            (
                "duplicate_object_key",
                b'{"schema_version":1,"schema_version":1}',
            ),
            ("invalid_utf8", b'{"schema_version":1,"value":"\xff"}'),
            ("invalid_json", b"\xef\xbb\xbf{}"),
            ("wrong_container_type", b"[]"),
            (
                "normalized_key_collision",
                br'{"schema_version":1,"\u00e9":1,"e\u0301":2}',
            ),
        )
        for code, raw in cases:
            with self.subTest(code=code):
                assert_import_error(self, code, snapshot(raw=raw))

        unknown_root = payload()
        unknown_root["scheduler"] = "hidden"
        assert_import_error(self, "unknown_field", snapshot(unknown_root))

        unknown_requirement = payload()
        unknown_requirement["requirements"][0]["owner"] = "hidden"
        assert_import_error(self, "unknown_field", snapshot(unknown_requirement))

        unknown_task = payload()
        unknown_task["tasks"][0]["priority"] = "hidden"
        assert_import_error(self, "unknown_field", snapshot(unknown_task))

        float_in_ignored_lifecycle = payload()
        float_in_ignored_lifecycle["tasks"][0]["progress"] = 0.5
        assert_import_error(
            self,
            "float_not_allowed",
            snapshot(float_in_ignored_lifecycle),
        )

    def test_task_ids_reject_controls_even_when_general_text_allows_newlines(self) -> None:
        value = payload()
        value["tasks"][0]["id"] = "trellis/task\nalpha"
        assert_import_error(
            self,
            "disallowed_contract_control",
            snapshot(value),
        )

    def test_lifecycle_only_changes_preserve_graph_and_manifest_bytes(self) -> None:
        original = payload()
        progressed = copy.deepcopy(original)
        progressed.update(
            status="running",
            progress={"done": 1},
            history=["started"],
            presentation={"color": "green"},
            lifecycle={"attempt": 2},
        )
        progressed["requirements"][0].update(
            status="implemented",
            progress=100,
            implemented_at="2026-08-18T05:30:00Z",
            history=["accepted"],
        )
        progressed["tasks"][0].update(
            status="finished",
            progress=100,
            branch="task/zeta",
            pr_id=42,
            squash_commit="f" * 40,
            agent_owner="worker-7",
            history=["implemented", "checked"],
            lifecycle={"turn_id": "turn-7"},
            presentation={"collapsed": True},
        )

        first = import_trellis_snapshot(snapshot(original), settings())
        second = import_trellis_snapshot(snapshot(progressed), settings())
        self.assertEqual(first.trellis_graph_digest, second.trellis_graph_digest)
        self.assertEqual(
            first.manifest.canonical_json_bytes(),
            second.manifest.canonical_json_bytes(),
        )
        self.assertEqual(
            RequirementStatus.APPROVED,
            second.manifest.requirements[0].status,
        )

    def test_projected_change_alters_digest_and_invalidates_gate_b(self) -> None:
        original = import_trellis_snapshot(snapshot(), settings())
        unchanged = import_trellis_snapshot(
            snapshot(),
            settings(),
            approved_graph_digest=original.trellis_graph_digest,
        )
        self.assertFalse(unchanged.gate_b_invalidated)

        changed_payload = payload()
        changed_payload["tasks"][0]["title"] = "A materially different task"
        changed = import_trellis_snapshot(
            snapshot(changed_payload),
            settings(),
            approved_graph_digest=original.trellis_graph_digest,
        )
        self.assertNotEqual(original.trellis_graph_digest, changed.trellis_graph_digest)
        self.assertTrue(changed.gate_b_invalidated)

    def test_import_time_and_provider_do_not_change_graph_digest(self) -> None:
        original = import_trellis_snapshot(snapshot(), settings())
        later = import_trellis_snapshot(
            snapshot(),
            settings(imported_at="2026-08-18T06:00:00Z"),
        )
        pi = import_trellis_snapshot(
            snapshot(),
            settings(provider=WorkerProvider.PI),
        )

        self.assertEqual(original.trellis_graph_digest, later.trellis_graph_digest)
        self.assertEqual(original.trellis_graph_digest, pi.trellis_graph_digest)
        self.assertNotEqual(
            original.manifest.canonical_json_bytes(),
            later.manifest.canonical_json_bytes(),
        )
        self.assertNotEqual(
            original.manifest.canonical_json_bytes(),
            pi.manifest.canonical_json_bytes(),
        )

    def test_lease_timing_changes_manifest_but_not_trellis_graph_digest(self) -> None:
        original = import_trellis_snapshot(snapshot(), settings())
        changed = import_trellis_snapshot(
            snapshot(),
            settings(lease_ttl_seconds=120, lease_clock_skew_seconds=3),
        )

        self.assertEqual(original.trellis_graph_digest, changed.trellis_graph_digest)
        self.assertNotEqual(
            original.manifest.canonical_sha256(),
            changed.manifest.canonical_sha256(),
        )

    def test_import_settings_reject_invalid_lease_timing(self) -> None:
        for ttl, skew in ((29, 0), (3_601, 0), (30, -1), (30, 8)):
            with self.subTest(ttl=ttl, skew=skew), self.assertRaises(ValueError):
                settings(
                    lease_ttl_seconds=ttl,
                    lease_clock_skew_seconds=skew,
                )

    def test_revision_is_provenance_while_parent_identity_is_graph_material(self) -> None:
        original = import_trellis_snapshot(snapshot(), settings())

        revised_payload = payload()
        revised_payload["revision"] = digest("e")
        revised = import_trellis_snapshot(
            snapshot(revised_payload, revision=digest("e")),
            settings(),
        )
        self.assertEqual(original.trellis_graph_digest, revised.trellis_graph_digest)
        self.assertNotEqual(
            original.manifest.canonical_json_bytes(),
            revised.manifest.canonical_json_bytes(),
        )

        other_parent_payload = payload()
        other_parent_payload["parent_task_id"] = "parent/wish-002"
        other_parent = import_trellis_snapshot(
            snapshot(other_parent_payload, parent_task_id="parent/wish-002"),
            settings(),
        )
        self.assertNotEqual(
            original.trellis_graph_digest,
            other_parent.trellis_graph_digest,
        )

        revisionless_payload = payload()
        revisionless_payload["revision"] = None
        revisionless = import_trellis_snapshot(
            snapshot(revisionless_payload, revision=None),
            settings(),
        )
        self.assertIsNone(revisionless.manifest.trellis_revision)

    def test_frozen_inputs_and_command_specs_project_exactly(self) -> None:
        result = import_trellis_snapshot(snapshot(), settings())
        first, second = result.manifest.tasks

        self.assertEqual(digest("2"), first.instruction_context_digest)
        self.assertEqual((digest("3"), digest("4")), first.approved_document_digests)
        self.assertIsNone(first.task_packet_template_digest)
        self.assertIsNone(second.instruction_context_digest)
        self.assertEqual((), second.approved_document_digests)
        self.assertEqual(digest("5"), second.task_packet_template_digest)
        self.assertEqual(tuple(command()["argv"]), first.regression_commands[0].argv)
        self.assertEqual(
            tuple(sorted(command()["environment_allowlist"])),
            first.regression_commands[0].environment_allowlist,
        )

    def test_invalid_approved_graph_digest_is_rejected_without_gate_mutation(self) -> None:
        with self.assertRaises(TrellisGraphImportError) as raised:
            import_trellis_snapshot(
                snapshot(),
                settings(),
                approved_graph_digest="sha256:short",
            )
        self.assertEqual("invalid_approved_graph_digest", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
