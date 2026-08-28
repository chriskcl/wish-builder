from __future__ import annotations

import copy
import dataclasses
import itertools
import unittest
from unittest.mock import patch

from wish_builder.contracts import ReasonCode, decode_manifest_primitive
from wish_builder.contracts.models import MAX_TASKS, ExecutionManifest
from wish_builder.contracts.runtime import RuntimeState
from wish_builder.kernel.dag import MAX_GRAPH_TASKS, DagError, TaskDag
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.kernel.state import KernelSnapshot
from wish_builder.kernel.validation import validate_manifest, validate_manifest_shape

from .test_state import manifest_from
from .test_validation import valid_manifest


class CompleteWritableSetTests(unittest.TestCase):
    def test_every_owned_auxiliary_cross_combination_blocks_parallel_tasks(
        self,
    ) -> None:
        fields = ("owned_paths", "allowed_auxiliary_paths")
        for left_field, right_field in itertools.product(fields, repeat=2):
            with self.subTest(left_field=left_field, right_field=right_field):
                value = valid_manifest()
                left = value["tasks"][1]
                right = value["tasks"][2]
                left["owned_paths"] = ["src/left/**"]
                left["allowed_auxiliary_paths"] = [".trellis/left/**"]
                right["owned_paths"] = ["src/right/**"]
                right["allowed_auxiliary_paths"] = [".trellis/right/**"]
                left[left_field] = ["shared/root/**"]
                right[right_field] = ["SHARED/root/nested/**"]

                decoded = decode_manifest_primitive(value)
                self.assertTrue(decoded.ok, decoded.report.render_text())
                assert decoded.value is not None
                conflicts = [
                    issue
                    for issue in validate_manifest(decoded.value).issues
                    if issue.rule_id == "manifest.parallel_ownership"
                ]
                self.assertEqual(1, len(conflicts))
                self.assertEqual(
                    ("tasks", "TASK-002", left_field),
                    conflicts[0].path,
                )
                self.assertEqual(
                    (("tasks", "TASK-003", right_field),),
                    conflicts[0].related_paths,
                )

                dag = TaskDag.compile(decoded.value)
                self.assertEqual(
                    ("TASK-003",),
                    dag.node("TASK-002").ownership_conflicts,
                )
                states = {
                    task_id: RuntimeState.APPROVED
                    for task_id in dag.topological_order
                }
                states["TASK-001"] = RuntimeState.VERIFIED
                self.assertEqual(("TASK-002",), dag.ready(states))

                initial = KernelSnapshot.initial(decoded.value.run_id, 1, dag)
                snapshot = dataclasses.replace(
                    initial,
                    phase=RuntimeState.EXECUTING,
                    tasks=tuple(
                        dataclasses.replace(task, state=states[task.task_id])
                        for task in initial.tasks
                    ),
                )
                self.assertEqual(
                    ("TASK-002",),
                    GraphIndex.rebuild(decoded.value, snapshot).ready_set,
                )

    def test_task_limit_is_shared_and_precedes_pairwise_work(self) -> None:
        self.assertEqual(MAX_TASKS, MAX_GRAPH_TASKS)

        manifest = manifest_from()
        forged = object.__new__(ExecutionManifest)
        object.__setattr__(
            forged,
            "tasks",
            manifest.tasks
            + (manifest.tasks[-1],)
            * (MAX_TASKS + 1 - len(manifest.tasks)),
        )
        with (
            patch(
                "wish_builder.kernel.dag._summarize_writable_set",
                side_effect=AssertionError("pairwise work started"),
            ),
            self.assertRaisesRegex(DagError, "task_limit_exceeded"),
        ):
            TaskDag.compile(forged)

        raw = valid_manifest()
        template = raw["tasks"][1]
        tasks = []
        for index in range(MAX_TASKS + 1):
            task = copy.deepcopy(template)
            task["id"] = f"TASK-{index:03d}"
            task["title"] = f"Task {index:03d}"
            task["depends_on"] = []
            task["owned_paths"] = [f"src/{index:03d}/**"]
            task["allowed_auxiliary_paths"] = [f".trellis/{index:03d}/**"]
            task["issue_id"] = index + 1
            task["branch"] = f"feat/{index:03d}"
            tasks.append(task)
        raw["tasks"] = tasks
        with patch(
            "wish_builder.kernel.validation._summarize_writable_set",
            side_effect=AssertionError("semantic pairwise work started"),
        ):
            report = validate_manifest_shape(raw)
        self.assertIn(
            ReasonCode.ITEM_LIMIT_EXCEEDED,
            {issue.reason_code for issue in report.issues},
        )


class IncrementalGraphIndexTests(unittest.TestCase):
    def test_normal_event_advances_without_dag_compile_or_full_rebuild(self) -> None:
        manifest = manifest_from()
        dag = TaskDag.compile(manifest)
        initial = KernelSnapshot.initial(manifest.run_id, 1, dag)
        previous = dataclasses.replace(
            initial,
            phase=RuntimeState.EXECUTING,
            tasks=tuple(
                dataclasses.replace(task, state=RuntimeState.APPROVED)
                for task in initial.tasks
            ),
        )
        index = GraphIndex.rebuild(manifest, previous)
        current = dataclasses.replace(
            previous,
            tasks=(
                dataclasses.replace(
                    previous.tasks[0],
                    state=RuntimeState.VERIFIED,
                ),
                *previous.tasks[1:],
            ),
            last_sequence=1,
            last_event_id="EVENT-1",
            last_event_hash="sha256:" + "1" * 64,
        )

        with (
            patch.object(
                TaskDag,
                "compile",
                side_effect=AssertionError("DAG recompiled during normal scheduling"),
            ),
            patch.object(
                GraphIndex,
                "rebuild",
                side_effect=AssertionError(
                    "full index replay during normal scheduling"
                ),
            ),
        ):
            advanced = index.advance(previous, current)

        self.assertEqual(0, advanced.remaining_for("TASK-002"))
        self.assertEqual(0, advanced.remaining_for("TASK-003"))
        self.assertEqual(("TASK-002", "TASK-003"), advanced.ready_set)


if __name__ == "__main__":
    unittest.main()
