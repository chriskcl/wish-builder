from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import unittest
from pathlib import Path

from tests.contracts.test_decoder import generated_shape
from tests.kernel.test_state import (
    complete_task,
    freeze_graph,
    hash_ref,
    manifest_from,
    transition,
)
from tests.model.reference import (
    ReferenceReason,
    ReferenceSnapshot,
    ReferenceTransition,
    generate_invalid_sequences,
    generate_legal_sequences,
    ready_tasks,
    reduce_transition,
    shrink_failure,
    transition_from_primitive,
    transition_to_primitive,
    transitions_digest,
)
from wish_builder.contracts import DecodeLimits, decode_manifest_primitive
from wish_builder.contracts.runtime import (
    ExecutionIdentity,
    JournalEventType,
    RuntimeState,
    TransitionSubject,
)
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.kernel.state import (
    KernelSnapshot,
    StateTransition,
    apply_transition,
    replay,
)

FIXTURE = Path(__file__).with_name("saved-sequences.json")


def load_fixture() -> dict[str, object]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise AssertionError("saved sequence fixture must be an object")
    return value


def digest(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def kernel_transition(item: ReferenceTransition) -> StateTransition:
    identity = item.identity
    return StateTransition(
        item.sequence,
        item.event_id,
        item.event_hash,
        item.previous_event_hash,
        item.event_type,
        item.subject,
        item.from_state,
        item.to_state,
        ExecutionIdentity(
            identity.run_id,
            identity.coordinator_epoch,
            identity.task_id,
            identity.attempt,
            identity.correlation_id,
        ),
        item.reason_code,
    )


class SavedSequenceTests(unittest.TestCase):
    def test_reference_generators_match_saved_digests_and_minimized_witnesses(
        self,
    ) -> None:
        fixture = load_fixture()
        self.assertEqual(2, fixture["schema_version"])
        saved = fixture["reference_model"]
        self.assertIsInstance(saved, dict)
        seed = saved["seed"]
        self.assertIsInstance(seed, int)
        manifest = manifest_from()

        legal = [
            {
                "digest": transitions_digest(case.transitions),
                "event_count": len(case.transitions),
                "id": case.name,
                "task_order": list(case.task_order),
            }
            for case in generate_legal_sequences(manifest, seed=seed)
        ]
        self.assertEqual(saved["legal_sequences"], legal)

        initial = ReferenceSnapshot.initial(manifest)
        generated = {
            case.name: case
            for case in generate_invalid_sequences(manifest, seed=seed)
        }
        witnesses = []
        for witness in saved["minimized_witnesses"]:
            case = generated[witness["id"]]
            shrunk = shrink_failure(
                initial,
                case.transitions,
                case.expected_reason,
            )
            witnesses.append(
                {
                    "events": [transition_to_primitive(item) for item in shrunk],
                    "expected_reason": case.expected_reason.value,
                    "id": case.name,
                }
            )
        self.assertEqual(saved["minimized_witnesses"], witnesses)
        self.assertEqual(set(generated), {item["id"] for item in witnesses})

    def test_persisted_minimized_witnesses_match_reference_and_kernel(self) -> None:
        fixture = load_fixture()
        manifest = manifest_from()
        dag = TaskDag.compile(manifest)
        saved = fixture["reference_model"]

        for witness in saved["minimized_witnesses"]:
            with self.subTest(witness=witness["id"]):
                expected = ReferenceReason(witness["expected_reason"])
                events = tuple(
                    transition_from_primitive(item) for item in witness["events"]
                )
                self.assertEqual(
                    events,
                    shrink_failure(
                        ReferenceSnapshot.initial(manifest),
                        events,
                        expected,
                    ),
                )
                model = ReferenceSnapshot.initial(manifest)
                kernel = KernelSnapshot.initial(manifest.run_id, 1, dag)

                for index, item in enumerate(events):
                    model_result = reduce_transition(model, item)
                    kernel_result = apply_transition(kernel, kernel_transition(item))
                    self.assertEqual(model_result.accepted, kernel_result.accepted)
                    self.assertEqual(
                        model_result.reason.value,
                        kernel_result.reason.value,
                    )
                    if index == len(events) - 1:
                        self.assertFalse(model_result.accepted)
                        self.assertIs(expected, model_result.reason)
                        self.assertEqual(model, model_result.snapshot)
                        self.assertEqual(kernel, kernel_result.snapshot)
                    else:
                        self.assertTrue(model_result.accepted)
                        model = model_result.snapshot
                        kernel = kernel_result.snapshot

    def test_saved_decoder_seed_has_a_pinned_diagnostic_digest(self) -> None:
        fixture = load_fixture()
        cases = fixture["decoder_cases"]
        self.assertIsInstance(cases, list)
        for case in cases:
            with self.subTest(case=case["id"]):
                randomizer = random.Random(case["seed"])
                limits = DecodeLimits(
                    max_depth=8,
                    max_items=512,
                    max_string_length=64,
                )
                diagnostics = [
                    decode_manifest_primitive(
                        generated_shape(randomizer, case["depth"]),
                        limits=limits,
                    ).diagnostic_sha256()
                    for _ in range(case["sample_count"])
                ]
                combined = hashlib.sha256(
                    ("\n".join(diagnostics) + "\n").encode("ascii")
                ).hexdigest()
                self.assertEqual(case["expected_digest"], "sha256:" + combined)

    def test_saved_state_seed_matches_replay_reference_and_rebuilt_index(self) -> None:
        fixture = load_fixture()
        case = fixture["state_generator"]
        randomizer = random.Random(case["seed"])
        records = []

        for _ in range(case["sample_count"]):
            manifest = manifest_from()
            dag = TaskDag.compile(manifest)
            initial = KernelSnapshot.initial(manifest.run_id, 1, dag)
            current, emitted = freeze_graph(initial)
            emitted_events = list(emitted)
            index = GraphIndex.compile(manifest, current)
            conflicts = {
                node.task_id: frozenset(node.ownership_conflicts)
                for node in dag.nodes
            }
            siblings = ["TASK-002", "TASK-003"]
            randomizer.shuffle(siblings)
            task_order = ["TASK-001", *siblings, "TASK-004"]
            ready_after_task = []

            for task_id in task_order:
                _, task_events = complete_task(current, task_id)
                for event in task_events:
                    previous = current
                    applied = apply_transition(previous, event)
                    self.assertTrue(applied.accepted, applied.reason)
                    current = applied.snapshot
                    emitted_events.append(event)
                    index = index.advance(previous, current)

                    replayed = replay(initial, tuple(emitted_events))
                    self.assertTrue(replayed.accepted, replayed.reason)
                    self.assertEqual(current, replayed.snapshot)
                    self.assertEqual(GraphIndex.rebuild(manifest, current), index)
                    self.assertEqual(
                        ready_tasks(manifest, current.task_states(), conflicts),
                        index.ready_set,
                    )
                ready_after_task.append(list(index.ready_set))

            records.append(
                {
                    "order": task_order,
                    "ready_after_task": ready_after_task,
                }
            )

        self.assertEqual(case["expected_digest"], digest(records))

    def test_minimized_illegal_sequences_keep_their_exact_reason(self) -> None:
        fixture = load_fixture()
        manifest = manifest_from()
        dag = TaskDag.compile(manifest)
        initial = KernelSnapshot.initial(manifest.run_id, 1, dag)
        frozen, _ = freeze_graph(initial)
        valid = transition(
            frozen,
            JournalEventType.TASK_READY,
            TransitionSubject.TASK,
            RuntimeState.APPROVED,
            RuntimeState.READY,
            task_id="TASK-001",
        )

        for case in fixture["minimized_failures"]:
            mutation = case["mutation"]
            if mutation == "sequence_gap":
                candidate = dataclasses.replace(
                    valid,
                    sequence=valid.sequence + 2,
                    event_id="EVENT-MINIMIZED-GAP",
                    event_hash=hash_ref(9001),
                )
            elif mutation == "previous_hash":
                candidate = dataclasses.replace(
                    valid,
                    previous_event_hash=hash_ref(9002),
                )
            elif mutation == "coordinator_epoch":
                candidate = dataclasses.replace(
                    valid,
                    identity=ExecutionIdentity(
                        valid.identity.run_id,
                        valid.identity.coordinator_epoch + 1,
                        valid.identity.task_id,
                    ),
                )
            elif mutation == "transition_pair":
                candidate = dataclasses.replace(
                    valid,
                    from_state=RuntimeState.READY,
                    to_state=RuntimeState.APPROVED,
                )
            else:
                self.fail(f"unknown saved mutation: {mutation}")

            with self.subTest(case=case["id"]):
                result = apply_transition(frozen, candidate)
                self.assertFalse(result.accepted)
                self.assertEqual(case["expected_reason"], result.reason.value)
                self.assertEqual(frozen, result.snapshot)
                self.assertEqual(1, case["event_count"])


if __name__ == "__main__":
    unittest.main()
