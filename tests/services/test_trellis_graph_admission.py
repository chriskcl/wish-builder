from __future__ import annotations

import copy
import unittest

from tests.adapters.test_trellis_graph_import import (
    digest,
    payload,
    settings,
    snapshot,
    task,
)
from wish_builder.adapters.trellis import import_trellis_snapshot
from wish_builder.services.trellis_graph_admission import (
    TrellisGraphAdmissionReason,
    TrellisGraphAdmissionService,
)


class SequenceGraphPort:
    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[str] = []

    def export_snapshot(self, parent_task_id: str):
        self.calls.append(parent_task_id)
        if not self._outcomes:
            raise RuntimeError("no graph outcome remains")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TrellisGraphAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = snapshot()
        self.manifest = import_trellis_snapshot(self.source, settings()).manifest

    def test_each_call_performs_a_fresh_export(self) -> None:
        port = SequenceGraphPort(*((self.source,) * 4))
        service = TrellisGraphAdmissionService(self.manifest, port)

        self.assertTrue(service.admit().admitted)
        self.assertTrue(service.admit().admitted)
        self.assertEqual(
            [self.manifest.trellis_parent_task_id] * 4,
            port.calls,
        )

    def test_lifecycle_only_progress_does_not_invalidate_gate_b(self) -> None:
        changed = copy.deepcopy(payload())
        changed["revision"] = digest("6")
        changed["progress"] = {"completed": 1}
        changed["tasks"][0]["status"] = "completed"
        candidate = snapshot(
            changed,
            revision=digest("6"),
        )

        result = TrellisGraphAdmissionService(
            self.manifest,
            SequenceGraphPort(candidate, candidate),
        ).admit()

        self.assertTrue(result.admitted)
        self.assertIs(TrellisGraphAdmissionReason.NONE, result.reason)
        self.assertEqual(self.manifest.trellis_graph_digest, result.graph_digest)
        self.assertEqual(digest("6"), result.revision)

    def test_material_task_changes_invalidate_gate_b(self) -> None:
        def add_dependency(value) -> None:
            value["requirements"].append(
                {
                    "id": "REQ-003",
                    "text": "Add a second prerequisite",
                    "status": "approved",
                    "decision_ref": None,
                }
            )
            value["tasks"].append(
                task(
                    "trellis/task-gamma",
                    "REQ-003",
                    depends_on=["trellis/task-alpha"],
                    wave=1,
                )
            )
            value["tasks"][0]["depends_on"].append("trellis/task-gamma")
            value["tasks"][0]["wave"] = 2

        mutations = {
            "dependency": add_dependency,
            "owned_path": lambda value: value["tasks"][0].__setitem__(
                "owned_paths", ["src/changed/**"]
            ),
            "acceptance_command": lambda value: value["tasks"][0][
                "regression_commands"
            ][0].__setitem__("argv", ["python", "-m", "changed"]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(payload())
                revision = digest(
                    {
                        "dependency": "6",
                        "owned_path": "7",
                        "acceptance_command": "8",
                    }[name]
                )
                changed["revision"] = revision
                mutate(changed)
                result = TrellisGraphAdmissionService(
                    self.manifest,
                    SequenceGraphPort(
                        *(
                            snapshot(changed, revision=revision),
                        )
                        * 2
                    ),
                ).admit()
                self.assertFalse(result.admitted)
                self.assertIs(
                    TrellisGraphAdmissionReason.GRAPH_CHANGED,
                    result.reason,
                )

    def test_transport_and_invalid_snapshot_fail_closed(self) -> None:
        cases = {
            "transport": (
                RuntimeError("timeout"),
                TrellisGraphAdmissionReason.GRAPH_UNAVAILABLE,
            ),
            "incomplete": (
                (snapshot(complete=False),) * 2,
                TrellisGraphAdmissionReason.GRAPH_INVALID,
            ),
            "malformed": (
                (snapshot(raw=b"{}"),) * 2,
                TrellisGraphAdmissionReason.GRAPH_INVALID,
            ),
        }
        for name, (outcome, reason) in cases.items():
            with self.subTest(name=name):
                outcomes = outcome if type(outcome) is tuple else (outcome,)
                result = TrellisGraphAdmissionService(
                    self.manifest,
                    SequenceGraphPort(*outcomes),
                ).admit()
                self.assertFalse(result.admitted)
                self.assertIs(reason, result.reason)

    def test_changed_or_unavailable_second_read_fails_closed(self) -> None:
        advanced = copy.deepcopy(payload())
        advanced["revision"] = digest("9")
        advanced_snapshot = snapshot(
            advanced,
            revision=digest("9"),
        )
        cases = {
            "revision_changed": (
                self.source,
                advanced_snapshot,
                TrellisGraphAdmissionReason.GRAPH_UNSTABLE,
            ),
            "adapter_outage": (
                self.source,
                RuntimeError("second read failed"),
                TrellisGraphAdmissionReason.GRAPH_UNAVAILABLE,
            ),
            "invalid_port_value": (
                self.source,
                object(),
                TrellisGraphAdmissionReason.GRAPH_UNAVAILABLE,
            ),
        }
        for name, (first, second, reason) in cases.items():
            with self.subTest(name=name):
                result = TrellisGraphAdmissionService(
                    self.manifest,
                    SequenceGraphPort(first, second),
                ).admit()
                self.assertFalse(result.admitted)
                self.assertIs(reason, result.reason)


if __name__ == "__main__":
    unittest.main()
