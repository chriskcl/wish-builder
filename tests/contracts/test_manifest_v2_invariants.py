from __future__ import annotations

import dataclasses
import unittest
from collections.abc import Callable

from wish_builder.contracts import (
    ManifestGateEvidence,
    RequirementStatus,
    decode_manifest_v2_primitive,
)

from .test_manifest_v2 import valid_manifest_v2


class ManifestV2InvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        decoded = decode_manifest_v2_primitive(valid_manifest_v2())
        if not decoded.ok or decoded.value is None:
            raise AssertionError(decoded.report.render_text())
        cls.manifest = decoded.value

    def assert_rejected(
        self,
        cases: tuple[tuple[str, Callable[[], object], type[BaseException], str], ...],
    ) -> None:
        for name, operation, error_type, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(error_type, message):
                operation()

    def test_command_and_budget_reject_unsafe_process_boundaries(self) -> None:
        command = self.manifest.tasks[0].regression_commands[0]
        budget = self.manifest.execution_budget
        self.assert_rejected(
            (
                (
                    "argument type",
                    lambda: dataclasses.replace(command, argv=(object(),)),
                    TypeError,
                    "must be a string",
                ),
                (
                    "argument length",
                    lambda: dataclasses.replace(command, argv=("x" * 131_073,)),
                    ValueError,
                    "argument limit",
                ),
                (
                    "argument Unicode",
                    lambda: dataclasses.replace(command, argv=("bad\ud800",)),
                    ValueError,
                    "valid Unicode",
                ),
                (
                    "argument control",
                    lambda: dataclasses.replace(command, argv=("bad\x01",)),
                    ValueError,
                    "contract control",
                ),
                (
                    "empty argv",
                    lambda: dataclasses.replace(command, argv=()),
                    ValueError,
                    "non-empty tuple",
                ),
                (
                    "argument count",
                    lambda: dataclasses.replace(
                        command, argv=tuple("x" for _ in range(4_097))
                    ),
                    ValueError,
                    "argument count",
                ),
                (
                    "environment name",
                    lambda: dataclasses.replace(
                        command, environment_allowlist=("INVALID-NAME",)
                    ),
                    ValueError,
                    "invalid name",
                ),
                (
                    "network policy type",
                    lambda: dataclasses.replace(command, network_policy="denied"),
                    TypeError,
                    "NetworkPolicy",
                ),
                (
                    "budget bool",
                    lambda: dataclasses.replace(budget, max_output_bytes=True),
                    ValueError,
                    "between 1",
                ),
                (
                    "billing posture type",
                    lambda: dataclasses.replace(budget, billing_posture="preapproved"),
                    TypeError,
                    "BillingPosture",
                ),
            )
        )
        nested = dataclasses.replace(command, working_directory="tests/contracts")
        self.assertEqual("tests/contracts", nested.working_directory)

    def test_gate_requirement_and_task_types_are_closed(self) -> None:
        approvals = self.manifest.approvals
        requirement = self.manifest.requirements[0]
        task = self.manifest.tasks[0]
        command = task.regression_commands[0]

        self.assert_rejected(
            (
                (
                    "gate A type",
                    lambda: ManifestGateEvidence(gate_a=object()),
                    TypeError,
                    "gate_a",
                ),
                (
                    "gate B type",
                    lambda: dataclasses.replace(approvals, gate_b=object()),
                    TypeError,
                    "gate_b",
                ),
                (
                    "requirement status type",
                    lambda: dataclasses.replace(requirement, status="approved"),
                    TypeError,
                    "RequirementStatus",
                ),
                (
                    "implemented requirement",
                    lambda: dataclasses.replace(
                        requirement, status=RequirementStatus.IMPLEMENTED
                    ),
                    ValueError,
                    "runtime status",
                ),
                (
                    "deferred without decision",
                    lambda: dataclasses.replace(
                        requirement,
                        status=RequirementStatus.DEFERRED,
                        decision_ref=None,
                    ),
                    ValueError,
                    "decision_ref",
                ),
                (
                    "self dependency",
                    lambda: dataclasses.replace(task, depends_on=(task.id,)),
                    ValueError,
                    "depend on itself",
                ),
                (
                    "empty command tuple",
                    lambda: dataclasses.replace(task, regression_commands=()),
                    ValueError,
                    "must not be empty",
                ),
                (
                    "command tuple limit",
                    lambda: dataclasses.replace(
                        task, regression_commands=tuple(command for _ in range(257))
                    ),
                    ValueError,
                    "item limit",
                ),
                (
                    "command tuple type",
                    lambda: dataclasses.replace(task, regression_commands=(object(),)),
                    TypeError,
                    "invalid item type",
                ),
                (
                    "wave bool",
                    lambda: dataclasses.replace(task, wave=True),
                    ValueError,
                    "wave",
                ),
                (
                    "risk type",
                    lambda: dataclasses.replace(task, risk="medium"),
                    TypeError,
                    "RiskLevel",
                ),
                (
                    "contract flag type",
                    lambda: dataclasses.replace(task, may_change_contracts=1),
                    TypeError,
                    "bool",
                ),
                (
                    "incomplete frozen inputs",
                    lambda: dataclasses.replace(
                        task,
                        instruction_context_digest=None,
                        approved_document_digests=(),
                        task_packet_template_digest=None,
                    ),
                    ValueError,
                    "frozen worker inputs",
                ),
            )
        )
        self.assertIs(task.regression_commands, task.acceptance_commands)
        self.assertEqual(
            ("trellis/task-a", "TASK-001"),
            self.manifest.task_id_mapping[0].to_primitive(),
        )

    def test_manifest_rejects_duplicate_mapping_and_unknown_references(self) -> None:
        manifest = self.manifest
        first_mapping, second_mapping = manifest.task_id_mapping
        first_requirement, second_requirement = manifest.requirements
        first_task, second_task = manifest.tasks

        duplicate_source = dataclasses.replace(
            second_mapping, trellis_task_id=first_mapping.trellis_task_id
        )
        duplicate_target = dataclasses.replace(
            second_mapping, task_id=first_mapping.task_id
        )
        unknown_requirement = dataclasses.replace(
            first_task, requirement_ids=("REQ-999",)
        )
        unknown_dependency = dataclasses.replace(second_task, depends_on=("TASK-999",))

        self.assert_rejected(
            (
                (
                    "schema",
                    lambda: dataclasses.replace(manifest, schema_version=True),
                    ValueError,
                    "schema_version",
                ),
                (
                    "graph projection",
                    lambda: dataclasses.replace(manifest, graph_projection_version=2),
                    ValueError,
                    "graph_projection_version",
                ),
                (
                    "graph digest",
                    lambda: dataclasses.replace(
                        manifest, trellis_graph_digest="sha256:short"
                    ),
                    ValueError,
                    "full sha256",
                ),
                (
                    "duplicate source mapping",
                    lambda: dataclasses.replace(
                        manifest,
                        task_id_mapping=(first_mapping, duplicate_source),
                    ),
                    ValueError,
                    "source IDs",
                ),
                (
                    "duplicate target mapping",
                    lambda: dataclasses.replace(
                        manifest,
                        task_id_mapping=(first_mapping, duplicate_target),
                    ),
                    ValueError,
                    "target IDs",
                ),
                (
                    "approval type",
                    lambda: dataclasses.replace(manifest, approvals=object()),
                    TypeError,
                    "ManifestGateEvidence",
                ),
                (
                    "provider type",
                    lambda: dataclasses.replace(manifest, provider="codex"),
                    TypeError,
                    "WorkerProvider",
                ),
                (
                    "scheduler type",
                    lambda: dataclasses.replace(
                        manifest, scheduler_mode="wish_builder"
                    ),
                    TypeError,
                    "SchedulerMode",
                ),
                (
                    "budget type",
                    lambda: dataclasses.replace(manifest, execution_budget=object()),
                    TypeError,
                    "ExecutionBudgetPolicy",
                ),
                (
                    "path case type",
                    lambda: dataclasses.replace(manifest, path_case_mode="insensitive"),
                    TypeError,
                    "PathCaseMode",
                ),
                (
                    "duplicate requirements",
                    lambda: dataclasses.replace(
                        manifest,
                        requirements=(first_requirement, first_requirement),
                    ),
                    ValueError,
                    "requirement IDs",
                ),
                (
                    "duplicate tasks",
                    lambda: dataclasses.replace(
                        manifest,
                        tasks=(first_task, first_task),
                    ),
                    ValueError,
                    "task IDs",
                ),
                (
                    "unknown requirement",
                    lambda: dataclasses.replace(
                        manifest,
                        requirements=(first_requirement, second_requirement),
                        tasks=(unknown_requirement, second_task),
                    ),
                    ValueError,
                    "unknown requirement",
                ),
                (
                    "unknown dependency",
                    lambda: dataclasses.replace(
                        manifest,
                        tasks=(first_task, unknown_dependency),
                    ),
                    ValueError,
                    "unknown dependency",
                ),
            )
        )
        self.assertIs(manifest.approvals, manifest.approved)
        self.assertIs(manifest.provider, manifest.worker_backend)
        self.assertEqual(manifest.capability_digest, manifest.channel_capability_digest)


if __name__ == "__main__":
    unittest.main()
