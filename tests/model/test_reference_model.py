from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path

from tests.kernel.test_state import manifest_from
from tests.model import reference
from tests.model.reference import (
    ReferenceReason,
    ReferenceSnapshot,
    ReferenceTransition,
    first_failure,
    generate_invalid_sequences,
    generate_legal_sequences,
    reduce_transition,
    shrink_failure,
    transition_from_primitive,
    transition_to_primitive,
    transitions_digest,
)
from wish_builder.contracts.runtime import ExecutionIdentity, RuntimeState
from wish_builder.kernel.dag import TaskDag
from wish_builder.kernel.state import (
    ApplyReason,
    KernelSnapshot,
    StateTransition,
    apply_transition,
)


SEED = 0x5EED_1200


def kernel_transition(item: ReferenceTransition) -> StateTransition:
    identity = item.identity
    return StateTransition(
        sequence=item.sequence,
        event_id=item.event_id,
        event_hash=item.event_hash,
        previous_event_hash=item.previous_event_hash,
        event_type=item.event_type,
        subject=item.subject,
        from_state=item.from_state,
        to_state=item.to_state,
        identity=ExecutionIdentity(
            identity.run_id,
            identity.coordinator_epoch,
            identity.task_id,
            identity.attempt,
            identity.correlation_id,
        ),
        reason_code=item.reason_code,
    )


def snapshot_view(snapshot: object) -> tuple[object, ...]:
    return (
        snapshot.run_id,
        snapshot.coordinator_epoch,
        snapshot.phase,
        snapshot.status,
        snapshot.run_reason_code,
        tuple(
            (task.task_id, task.state, task.reason_code)
            for task in snapshot.tasks
        ),
        tuple(
            (
                attempt.task_id,
                attempt.attempt,
                attempt.correlation_id,
                attempt.coordinator_epoch,
                attempt.state,
                attempt.reason_code,
            )
            for attempt in snapshot.attempts
        ),
        snapshot.last_sequence,
        snapshot.last_event_id,
        snapshot.last_event_hash,
    )


class FullReferenceModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = manifest_from()
        self.dag = TaskDag.compile(self.manifest)
        self.conflicts = {
            node.task_id: frozenset(node.ownership_conflicts)
            for node in self.dag.nodes
        }

    def test_reference_reducer_has_no_kernel_import(self) -> None:
        source = Path(reference.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        self.assertFalse(
            any(name.startswith("wish_builder.kernel") for name in imported),
            imported,
        )

    def test_legal_sequences_match_every_kernel_prefix(self) -> None:
        cases = generate_legal_sequences(self.manifest, seed=SEED)
        self.assertEqual(cases, generate_legal_sequences(self.manifest, seed=SEED))
        self.assertEqual(
            {
                ("TASK-001", "TASK-002", "TASK-003", "TASK-004"),
                ("TASK-001", "TASK-003", "TASK-002", "TASK-004"),
            },
            {case.task_order for case in cases},
        )

        for case in cases:
            with self.subTest(case=case.name):
                model = ReferenceSnapshot.initial(self.manifest)
                kernel = KernelSnapshot.initial(
                    self.manifest.run_id,
                    1,
                    self.dag,
                )
                self.assertEqual(snapshot_view(model), snapshot_view(kernel))
                blocked_prefixes = 0

                for prefix, item in enumerate(case.transitions, start=1):
                    model_result = reduce_transition(model, item)
                    kernel_result = apply_transition(kernel, kernel_transition(item))
                    self.assertTrue(model_result.accepted, (prefix, model_result.reason))
                    self.assertTrue(kernel_result.accepted, (prefix, kernel_result.reason))
                    self.assertEqual(
                        model_result.reason.value,
                        kernel_result.reason.value,
                        prefix,
                    )
                    model = model_result.snapshot
                    kernel = kernel_result.snapshot
                    self.assertEqual(
                        snapshot_view(model),
                        snapshot_view(kernel),
                        prefix,
                    )
                    self.assertEqual(
                        model.ready(self.manifest, self.conflicts),
                        kernel.ready(self.dag),
                        prefix,
                    )
                    if model.status in {
                        RuntimeState.PAUSING,
                        RuntimeState.PAUSED,
                        RuntimeState.BLOCKED,
                        RuntimeState.ESCALATED,
                    }:
                        blocked_prefixes += 1
                        self.assertEqual((), model.ready(self.manifest, self.conflicts))
                        self.assertEqual((), kernel.ready(self.dag))

                self.assertGreaterEqual(blocked_prefixes, 4)
                self.assertIs(RuntimeState.COMPLETE, model.phase)
                self.assertIs(RuntimeState.ARCHIVED, model.status)
                self.assertTrue(
                    all(task.state is RuntimeState.VERIFIED for task in model.tasks)
                )
                self.assertTrue(
                    all(
                        attempt.state is RuntimeState.SUCCEEDED
                        for attempt in model.attempts
                    )
                )
                self.assertEqual(4, len(model.attempts))

    def test_exact_duplicate_is_idempotent_in_both_models(self) -> None:
        item = generate_legal_sequences(self.manifest, seed=SEED)[0].transitions[0]
        model_initial = ReferenceSnapshot.initial(self.manifest)
        kernel_initial = KernelSnapshot.initial(self.manifest.run_id, 1, self.dag)
        model_applied = reduce_transition(model_initial, item)
        kernel_applied = apply_transition(kernel_initial, kernel_transition(item))

        model_duplicate = reduce_transition(model_applied.snapshot, item)
        kernel_duplicate = apply_transition(
            kernel_applied.snapshot,
            kernel_transition(item),
        )
        self.assertTrue(model_duplicate.accepted)
        self.assertTrue(kernel_duplicate.accepted)
        self.assertIs(ReferenceReason.IDEMPOTENT_REPLAY, model_duplicate.reason)
        self.assertIs(ApplyReason.IDEMPOTENT_REPLAY, kernel_duplicate.reason)
        self.assertIs(model_applied.snapshot, model_duplicate.snapshot)
        self.assertIs(kernel_applied.snapshot, kernel_duplicate.snapshot)

    def test_invalid_sequences_match_kernel_reason_at_each_prefix(self) -> None:
        cases = generate_invalid_sequences(self.manifest, seed=SEED)
        self.assertEqual(cases, generate_invalid_sequences(self.manifest, seed=SEED))
        self.assertEqual(
            {
                ReferenceReason.SEQUENCE_CONFLICT,
                ReferenceReason.STALE_SEQUENCE,
                ReferenceReason.SEQUENCE_GAP,
                ReferenceReason.HASH_CHAIN_MISMATCH,
                ReferenceReason.RUN_MISMATCH,
                ReferenceReason.STALE_EPOCH,
                ReferenceReason.IDENTITY_MISMATCH,
                ReferenceReason.ILLEGAL_TRANSITION,
                ReferenceReason.STATE_MISMATCH,
                ReferenceReason.ACTIVE_ATTEMPT_EXISTS,
                ReferenceReason.STALE_ATTEMPT,
                ReferenceReason.STALE_CORRELATION,
            },
            {case.expected_reason for case in cases},
        )

        for case in cases:
            with self.subTest(case=case.name):
                model = ReferenceSnapshot.initial(self.manifest)
                kernel = KernelSnapshot.initial(self.manifest.run_id, 1, self.dag)
                for index, item in enumerate(case.transitions):
                    before_model = model
                    before_kernel = kernel
                    model_result = reduce_transition(model, item)
                    kernel_result = apply_transition(kernel, kernel_transition(item))
                    self.assertEqual(model_result.accepted, kernel_result.accepted)
                    self.assertEqual(
                        model_result.reason.value,
                        kernel_result.reason.value,
                    )
                    self.assertEqual(
                        snapshot_view(model_result.snapshot),
                        snapshot_view(kernel_result.snapshot),
                    )
                    if index == len(case.transitions) - 1:
                        self.assertFalse(model_result.accepted)
                        self.assertIs(case.expected_reason, model_result.reason)
                        self.assertEqual(before_model, model_result.snapshot)
                        self.assertEqual(before_kernel, kernel_result.snapshot)
                    else:
                        self.assertTrue(model_result.accepted)
                        model = model_result.snapshot
                        kernel = kernel_result.snapshot

    def test_failure_shrinking_is_deterministic_and_one_minimal(self) -> None:
        initial = ReferenceSnapshot.initial(self.manifest)
        expected_lengths = {
            "sequence-conflict": 2,
            "stale-sequence": 3,
            "sequence-gap": 1,
            "hash-chain-mismatch": 1,
            "run-mismatch": 1,
            "stale-epoch": 1,
            "identity-mismatch": 1,
            "illegal-transition": 1,
            "state-mismatch": 1,
            "active-attempt-exists": 2,
            "stale-attempt": 1,
            "stale-correlation": 2,
        }
        trailing_noise = generate_legal_sequences(
            self.manifest,
            seed=SEED,
        )[0].transitions[0]

        for case in generate_invalid_sequences(self.manifest, seed=SEED):
            with self.subTest(case=case.name):
                noisy = (*case.transitions, trailing_noise)
                shrunk = shrink_failure(initial, noisy, case.expected_reason)
                self.assertEqual(
                    shrunk,
                    shrink_failure(initial, noisy, case.expected_reason),
                )
                self.assertEqual(expected_lengths[case.name], len(shrunk))
                failure = first_failure(initial, shrunk)
                self.assertIsNotNone(failure)
                assert failure is not None
                self.assertEqual(len(shrunk) - 1, failure.index)
                self.assertIs(case.expected_reason, failure.reason)
                for index in range(len(shrunk)):
                    trial = (*shrunk[:index], *shrunk[index + 1 :])
                    trial_failure = first_failure(initial, trial)
                    self.assertFalse(
                        trial_failure is not None
                        and trial_failure.reason is case.expected_reason,
                        (case.name, index),
                    )

    def test_witness_encoding_and_digest_are_canonical(self) -> None:
        all_transitions = tuple(
            item
            for case in generate_invalid_sequences(self.manifest, seed=SEED)
            for item in case.transitions
        )
        encoded = tuple(transition_to_primitive(item) for item in all_transitions)
        decoded = tuple(transition_from_primitive(item) for item in encoded)
        self.assertEqual(all_transitions, decoded)
        self.assertEqual(
            transitions_digest(all_transitions),
            transitions_digest(decoded),
        )

    def test_reference_projection_is_immutable(self) -> None:
        snapshot = ReferenceSnapshot.initial(self.manifest)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.status = RuntimeState.BLOCKED  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
