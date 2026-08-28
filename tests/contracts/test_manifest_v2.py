from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import unittest

from wish_builder.contracts import (
    BillingPosture,
    CommandSpec,
    ExecutionBudgetPolicy,
    ExecutionManifestV2,
    ManifestGateEvidence,
    ManifestRequirement,
    ManifestTask,
    NetworkPolicy,
    NullGateApproval,
    PathCaseMode,
    ReasonCode,
    RequirementStatus,
    RiskLevel,
    SchedulerMode,
    TaskIdMapping,
    WorkerProvider,
    decode_manifest_primitive,
    decode_manifest_v2_bytes,
    decode_manifest_v2_primitive,
)

from .hostile_corpus import HOSTILE_RAW_BYTES


def digest(character: str) -> str:
    return "sha256:" + character * 64


def valid_command() -> dict[str, object]:
    return {
        "executable_profile": "python",
        "executable_identity_digest": digest("1"),
        "argv": ["python", "-m", "unittest", "tests.contracts"],
        "working_directory": ".",
        "timeout_seconds": 120,
        "stdout_limit_bytes": 1_048_576,
        "stderr_limit_bytes": 1_048_576,
        "result_limit_bytes": 262_144,
        "environment_allowlist": ["PYTHONUTF8", "PATH"],
        "network_policy": "denied",
        "display_text": "Run contract tests",
    }


def valid_manifest_v2() -> dict[str, object]:
    first = {
        "id": "TASK-001",
        "title": "Freeze contracts",
        "requirement_ids": ["REQ-001"],
        "depends_on": [],
        "owned_paths": ["wish_builder/contracts/**"],
        "allowed_auxiliary_paths": ["tests/contracts/**"],
        "acceptance_criteria": ["Contract tests pass"],
        "regression_commands": [valid_command()],
        "rollback": "Revert the squash commit",
        "documentation": ["README.md"],
        "wave": 0,
        "risk": "medium",
        "may_change_contracts": True,
        "instruction_context_digest": digest("2"),
        "approved_document_digests": [digest("4"), digest("3")],
        "task_packet_template_digest": None,
    }
    second = {
        "id": "TASK-002",
        "title": "Use contracts",
        "requirement_ids": ["REQ-002"],
        "depends_on": ["TASK-001"],
        "owned_paths": ["wish_builder/adapters/**"],
        "allowed_auxiliary_paths": [],
        "acceptance_criteria": ["Adapter tests pass"],
        "regression_commands": [valid_command()],
        "rollback": "Revert the squash commit",
        "documentation": [],
        "wave": 1,
        "risk": "low",
        "may_change_contracts": False,
        "instruction_context_digest": None,
        "approved_document_digests": [],
        "task_packet_template_digest": digest("5"),
    }
    return {
        "schema_version": 2,
        "graph_projection_version": 1,
        "run_id": "WISH-2026-001",
        "goal": "Deliver a Trellis-backed workflow",
        "base_branch": "main",
        "trellis_parent_task_id": "2026-08-18-wish-001",
        "trellis_revision": digest("f"),
        "trellis_graph_digest": digest("a"),
        "task_id_mapping": {
            "trellis/task-b": "TASK-002",
            "trellis/task-a": "TASK-001",
        },
        "imported_at": "2026-08-18T04:00:00Z",
        "approved": {
            "gate_a": {
                "approved_by": "architect",
                "approved_at": "2026-08-18T03:00:00Z",
                "artifact_hash": digest("b"),
            },
            "gate_b": {
                "approved_by": None,
                "approved_at": None,
                "artifact_hash": None,
            },
        },
        "provider": "codex",
        "capability_digest": digest("c"),
        "launch_profile_digest": digest("d"),
        "policy_digest": digest("e"),
        "scheduler_mode": "wish_builder",
        "execution_budget": {
            "max_attempts_per_task": 2,
            "max_attempts_per_run": 4,
            "attempt_deadline_seconds": 1_800,
            "total_worker_seconds": 7_200,
            "max_output_bytes": 8_388_608,
            "max_retained_evidence_bytes": 16_777_216,
            "max_concurrent_workers": 2,
            "billing_posture": "preapproved",
        },
        "max_concurrency": 2,
        "lease_ttl_seconds": 90,
        "lease_clock_skew_seconds": 2,
        "path_case_mode": "insensitive",
        "protected_paths": ["src/contracts/**", "db/schema/**"],
        "requirements": [
            {
                "id": "REQ-001",
                "text": "Freeze worker instructions",
                "status": "approved",
                "decision_ref": None,
            },
            {
                "id": "REQ-002",
                "text": "Run through Trellis Channel",
                "status": "approved",
                "decision_ref": None,
            },
        ],
        "tasks": [first, second],
    }


class ManifestV2ContractTests(unittest.TestCase):
    def decode(self, value: dict[str, object]):
        result = decode_manifest_v2_primitive(value)
        self.assertTrue(result.ok, result.report.render_text())
        return result.value

    def test_v2_round_trip_is_frozen_slotted_and_complete(self) -> None:
        manifest = self.decode(valid_manifest_v2())
        self.assertIsInstance(manifest, ExecutionManifestV2)
        self.assertFalse(hasattr(manifest, "__dict__"))
        self.assertIs(type(manifest.tasks), tuple)
        self.assertIs(type(manifest.tasks[0].regression_commands[0]), CommandSpec)
        self.assertEqual(WorkerProvider.CODEX, manifest.provider)
        self.assertEqual(SchedulerMode.WISH_BUILDER, manifest.scheduler_mode)
        self.assertEqual(90, manifest.lease_ttl_seconds)
        self.assertEqual(2, manifest.lease_clock_skew_seconds)
        self.assertEqual(
            {
                "approved_by": None,
                "approved_at": None,
                "artifact_hash": None,
            },
            manifest.to_primitive()["approved"]["gate_b"],
        )
        self.assertTrue(manifest.canonical_json_bytes().endswith(b"\n"))
        self.assertRegex(manifest.canonical_sha256(), r"^sha256:[0-9a-f]{64}$")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            manifest.goal = "changed"  # type: ignore[misc]

    def test_all_v2_models_are_public_and_direct_construction_is_closed(self) -> None:
        manifest = self.decode(valid_manifest_v2())
        self.assertIsInstance(manifest.approvals, ManifestGateEvidence)
        self.assertIsInstance(manifest.approvals.gate_b, NullGateApproval)
        self.assertIsInstance(manifest.execution_budget, ExecutionBudgetPolicy)
        self.assertIsInstance(manifest.requirements[0], ManifestRequirement)
        self.assertIsInstance(manifest.tasks[0], ManifestTask)
        self.assertIsInstance(manifest.task_id_mapping[0], TaskIdMapping)
        self.assertEqual(PathCaseMode.INSENSITIVE, manifest.path_case_mode)
        self.assertEqual(BillingPosture.PREAPPROVED, manifest.execution_budget.billing_posture)
        self.assertEqual(
            NetworkPolicy.DENIED,
            manifest.tasks[0].regression_commands[0].network_policy,
        )
        with self.assertRaises(TypeError):
            dataclasses.replace(manifest, provider="codex")
        with self.assertRaises(TypeError):
            dataclasses.replace(manifest, task_id_mapping=[])
        with self.assertRaises(ValueError):
            NullGateApproval(approved_by="operator")  # type: ignore[arg-type]

    def test_canonical_bytes_ignore_all_set_like_input_order(self) -> None:
        left = valid_manifest_v2()
        right = copy.deepcopy(left)
        right["requirements"].reverse()
        right["tasks"].reverse()
        right["protected_paths"].reverse()
        right["tasks"][1]["approved_document_digests"].reverse()
        right["tasks"][1]["owned_paths"].append("a/**")
        left["tasks"][0]["owned_paths"].append("a/**")
        right["tasks"][1]["owned_paths"].reverse()
        right["task_id_mapping"] = dict(
            reversed(list(right["task_id_mapping"].items()))
        )
        right["tasks"][1]["regression_commands"][0][
            "environment_allowlist"
        ].reverse()

        first = self.decode(left)
        second = self.decode(right)
        self.assertEqual(first, second)
        self.assertEqual(first.canonical_json_bytes(), second.canonical_json_bytes())
        self.assertEqual(first.canonical_sha256(), second.canonical_sha256())

    def test_canonical_manifest_has_a_pinned_golden_digest(self) -> None:
        manifest = self.decode(valid_manifest_v2())
        self.assertEqual(
            "sha256:76e6e16421142bcb7ac92b58c4c6a882d5732ba6ce2d3648446b41a086280409",
            manifest.canonical_sha256(),
        )

    def test_gate_a_is_required_and_gate_b_is_always_null(self) -> None:
        for name, mutate, expected_path in (
            (
                "missing gate a",
                lambda value: value["approved"].pop("gate_a"),
                ("approved", "gate_a"),
            ),
            (
                "gate b actor",
                lambda value: value["approved"]["gate_b"].__setitem__(
                    "approved_by", "operator"
                ),
                ("approved", "gate_b", "approved_by"),
            ),
            (
                "gate b hash",
                lambda value: value["approved"]["gate_b"].__setitem__(
                    "artifact_hash", digest("9")
                ),
                ("approved", "gate_b", "artifact_hash"),
            ),
        ):
            with self.subTest(name=name):
                value = valid_manifest_v2()
                mutate(value)
                result = decode_manifest_v2_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(expected_path, {issue.path for issue in result.issues})

    def test_command_specs_reject_shell_text_escape_and_drift(self) -> None:
        cases = []
        free_form = valid_manifest_v2()
        free_form["tasks"][0]["regression_commands"] = ["python -m unittest"]
        cases.append(("free form", free_form, ReasonCode.WRONG_CONTAINER_TYPE))

        for cwd in ("..", "../tests", "/tmp", "C:/repo", "tests\\contracts"):
            value = valid_manifest_v2()
            value["tasks"][0]["regression_commands"][0]["working_directory"] = cwd
            cases.append((f"cwd {cwd}", value, ReasonCode.INVALID_COMMAND_SPEC))

        bad_profile = valid_manifest_v2()
        bad_profile["tasks"][0]["regression_commands"][0][
            "executable_profile"
        ] = "python && shell"
        cases.append(("profile", bad_profile, ReasonCode.INVALID_COMMAND_SPEC))

        bad_environment = valid_manifest_v2()
        bad_environment["tasks"][0]["regression_commands"][0][
            "environment_allowlist"
        ] = ["PATH", "Path"]
        cases.append(("environment", bad_environment, ReasonCode.INVALID_COMMAND_SPEC))

        bad_network = valid_manifest_v2()
        bad_network["tasks"][0]["regression_commands"][0][
            "network_policy"
        ] = "inherit"
        cases.append(("network", bad_network, ReasonCode.UNKNOWN_ENUM_VALUE))

        bad_timeout = valid_manifest_v2()
        bad_timeout["tasks"][0]["regression_commands"][0]["timeout_seconds"] = 0
        cases.append(("timeout", bad_timeout, ReasonCode.INTEGER_OUT_OF_RANGE))

        bad_identity = valid_manifest_v2()
        bad_identity["tasks"][0]["regression_commands"][0][
            "executable_identity_digest"
        ] = "sha256:short"
        cases.append(("identity", bad_identity, ReasonCode.INVALID_HASH))

        for name, value, reason in cases:
            with self.subTest(name=name):
                result = decode_manifest_v2_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(reason, {issue.reason_code for issue in result.issues})

        repeated_argument = valid_manifest_v2()
        repeated_argument["tasks"][0]["regression_commands"][0]["argv"] = [
            "python",
            "same",
            "same",
        ]
        self.assertTrue(decode_manifest_v2_primitive(repeated_argument).ok)

    def test_frozen_inputs_require_exactly_one_complete_representation(self) -> None:
        mutations = (
            lambda task: task.update(
                instruction_context_digest=None,
                approved_document_digests=[],
                task_packet_template_digest=None,
            ),
            lambda task: task.update(
                instruction_context_digest=digest("2"),
                approved_document_digests=[],
                task_packet_template_digest=None,
            ),
            lambda task: task.update(
                instruction_context_digest=None,
                approved_document_digests=[digest("3")],
                task_packet_template_digest=None,
            ),
            lambda task: task.update(
                instruction_context_digest=digest("2"),
                approved_document_digests=[digest("3")],
                task_packet_template_digest=digest("5"),
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = valid_manifest_v2()
                mutate(value["tasks"][0])
                result = decode_manifest_v2_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(
                    ReasonCode.INVALID_FROZEN_INPUTS,
                    {issue.reason_code for issue in result.issues},
                )

    def test_budget_provider_and_policy_are_digest_bound(self) -> None:
        cases = []
        mismatched_concurrency = valid_manifest_v2()
        mismatched_concurrency["max_concurrency"] = 1
        cases.append(
            ("concurrency", mismatched_concurrency, ReasonCode.INVALID_MANIFEST)
        )

        impossible_attempts = valid_manifest_v2()
        impossible_attempts["execution_budget"]["max_attempts_per_run"] = 1
        cases.append(
            (
                "attempts",
                impossible_attempts,
                ReasonCode.INVALID_EXECUTION_BUDGET,
            )
        )

        for field in (
            "capability_digest",
            "launch_profile_digest",
            "policy_digest",
        ):
            bad_digest = valid_manifest_v2()
            bad_digest[field] = "sha256:short"
            cases.append((field, bad_digest, ReasonCode.INVALID_HASH))

        bad_provider = valid_manifest_v2()
        bad_provider["provider"] = "claude"
        cases.append(("provider", bad_provider, ReasonCode.UNKNOWN_ENUM_VALUE))

        second_scheduler = valid_manifest_v2()
        second_scheduler["scheduler_mode"] = "trellis"
        cases.append(
            ("scheduler", second_scheduler, ReasonCode.UNKNOWN_ENUM_VALUE)
        )

        for name, value, reason in cases:
            with self.subTest(name=name):
                result = decode_manifest_v2_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(reason, {issue.reason_code for issue in result.issues})

    def test_lease_timing_is_required_bounded_and_digest_bound(self) -> None:
        for field in ("lease_ttl_seconds", "lease_clock_skew_seconds"):
            with self.subTest(missing=field):
                value = valid_manifest_v2()
                value.pop(field)
                result = decode_manifest_v2_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn((field,), {issue.path for issue in result.issues})

        for ttl in (29, 3_601, True):
            with self.subTest(ttl=ttl):
                value = valid_manifest_v2()
                value["lease_ttl_seconds"] = ttl
                result = decode_manifest_v2_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(
                    ("lease_ttl_seconds",),
                    {issue.path for issue in result.issues},
                )

        for skew in (-1, 23, True):
            with self.subTest(skew=skew):
                value = valid_manifest_v2()
                value["lease_clock_skew_seconds"] = skew
                result = decode_manifest_v2_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(
                    ("lease_clock_skew_seconds",),
                    {issue.path for issue in result.issues},
                )

        changed = valid_manifest_v2()
        changed["lease_ttl_seconds"] = 120
        self.assertNotEqual(
            self.decode(valid_manifest_v2()).canonical_sha256(),
            self.decode(changed).canonical_sha256(),
        )

        for ttl, skew in ((30, 0), (30, 7), (3_600, 899)):
            with self.subTest(valid=(ttl, skew)):
                value = valid_manifest_v2()
                value["lease_ttl_seconds"] = ttl
                value["lease_clock_skew_seconds"] = skew
                self.assertTrue(decode_manifest_v2_primitive(value).ok)

    def test_nullable_revision_timestamp_and_snapshot_ids_are_strict(self) -> None:
        revisionless = valid_manifest_v2()
        revisionless["trellis_revision"] = None
        self.assertTrue(decode_manifest_v2_primitive(revisionless).ok)

        invalid_revision = valid_manifest_v2()
        invalid_revision["trellis_revision"] = "graph-rev-0007"
        result = decode_manifest_v2_primitive(invalid_revision)
        self.assertIn(
            ("trellis_revision",),
            {issue.path for issue in result.issues},
        )

        invalid_timestamp = valid_manifest_v2()
        invalid_timestamp["imported_at"] = "2026-02-30T04:00:00Z"
        result = decode_manifest_v2_primitive(invalid_timestamp)
        self.assertIn(
            ReasonCode.INVALID_TIMESTAMP,
            {issue.reason_code for issue in result.issues},
        )
        self.assertIn(("imported_at",), {issue.path for issue in result.issues})

        for identifier in ("TASK-000", "TASK-0001", "TASK-01", "task-001"):
            with self.subTest(identifier=identifier):
                value = valid_manifest_v2()
                value["tasks"][0]["id"] = identifier
                result = decode_manifest_v2_primitive(value)
                self.assertIn(
                    ReasonCode.INVALID_IDENTIFIER,
                    {issue.reason_code for issue in result.issues},
                )

    def test_mapping_is_bijective_complete_and_bounded(self) -> None:
        duplicate_target = valid_manifest_v2()
        duplicate_target["task_id_mapping"]["trellis/task-b"] = "TASK-001"
        result = decode_manifest_v2_primitive(duplicate_target)
        self.assertIn(
            ReasonCode.INVALID_MAPPING,
            {issue.reason_code for issue in result.issues},
        )

        incomplete = valid_manifest_v2()
        incomplete["task_id_mapping"].pop("trellis/task-b")
        result = decode_manifest_v2_primitive(incomplete)
        self.assertIn(
            ReasonCode.INVALID_MANIFEST,
            {issue.reason_code for issue in result.issues},
        )

        too_many = valid_manifest_v2()
        too_many["task_id_mapping"] = {
            f"trellis/task-{index:03d}": f"TASK-{index:03d}"
            for index in range(1, 66)
        }
        result = decode_manifest_v2_primitive(too_many)
        self.assertIn(
            ReasonCode.ITEM_LIMIT_EXCEEDED,
            {issue.reason_code for issue in result.issues},
        )

        normalized_collision = valid_manifest_v2()
        normalized_collision["task_id_mapping"] = {
            "trellis/caf\u00e9": "TASK-001",
            "trellis/cafe\u0301": "TASK-002",
        }
        result = decode_manifest_v2_primitive(normalized_collision)
        self.assertIn(
            ReasonCode.NORMALIZED_KEY_COLLISION,
            {issue.reason_code for issue in result.issues},
        )

    def test_unknown_fields_fail_closed_at_every_v2_object(self) -> None:
        mutations = (
            lambda value: value.__setitem__("unknown_root", True),
            lambda value: value["approved"]["gate_b"].__setitem__("status", None),
            lambda value: value["execution_budget"].__setitem__("currency", "USD"),
            lambda value: value["requirements"][0].__setitem__("history", []),
            lambda value: value["tasks"][0].__setitem__("progress", 50),
            lambda value: value["tasks"][0]["regression_commands"][0].__setitem__(
                "shell", True
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = valid_manifest_v2()
                mutate(value)
                result = decode_manifest_v2_primitive(value)
                self.assertFalse(result.ok)
                self.assertIn(
                    ReasonCode.UNKNOWN_FIELD,
                    {issue.reason_code for issue in result.issues},
                )

    def test_hostile_raw_bytes_never_cross_the_v2_boundary(self) -> None:
        for case in HOSTILE_RAW_BYTES:
            with self.subTest(name=case.name):
                result = decode_manifest_v2_bytes(case.raw)
                self.assertFalse(result.ok)
                self.assertEqual(
                    hashlib.sha256(case.raw).hexdigest(),
                    result.source_sha256,
                )

        raw = json.dumps(valid_manifest_v2(), separators=(",", ":")).encode("utf-8")
        decoded = decode_manifest_v2_bytes(raw)
        self.assertTrue(decoded.ok, decoded.report.render_text())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), decoded.source_sha256)

    def test_v1_and_v2_decoders_are_explicit_compatibility_surfaces(self) -> None:
        v2 = valid_manifest_v2()
        self.assertTrue(decode_manifest_v2_primitive(v2).ok)
        legacy = decode_manifest_primitive(v2)
        self.assertFalse(legacy.ok)
        self.assertIn(
            ReasonCode.UNSUPPORTED_SCHEMA_VERSION,
            {issue.reason_code for issue in legacy.issues},
        )


if __name__ == "__main__":
    unittest.main()
