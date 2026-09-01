from __future__ import annotations

import copy
import dataclasses
import itertools
import unittest
from types import SimpleNamespace

from wish_builder.contracts import (
    ManifestTask,
    TaskDefinition,
    decode_journal_event_bytes,
    decode_manifest_primitive,
)
from wish_builder.contracts.runtime import (
    AdapterKind,
    ActorType,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectRequestPayload,
    EffectStatus,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    RuntimeReasonCode,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel.dag import MAX_GRAPH_EDGES, DagError, TaskDag
from wish_builder.kernel.state import (
    GENESIS_HASH,
    ApplyResult,
    ApplyReason,
    AttemptProjection,
    KernelSnapshot,
    StateTransition,
    TaskProjection,
    apply_journal_event,
    apply_transition,
    replay,
    replay_journal_events,
    validate_transition,
)

from tests.model.reference import ready_tasks, topological_order

from .test_validation import valid_manifest


LEGACY_DECOMPOSITION_EVENT_BYTES = (
    b'{"actor_id":"legacy-runtime","actor_type":"system","attempt":null,'
    b'"coordinator_epoch":1,"correlation_id":null,'
    b'"event_hash":"sha256:e480a072e20c93e80f827400d63fa0a29c97728999f05299856d897a64b63613",'
    b'"event_id":"EVENT-LEGACY-004","event_type":"decomposition_completed",'
    b'"event_version":"1.0","payload":{"evidence":[],'
    b'"from_state":"decomposition","payload_type":"transition","subject":"run",'
    b'"to_state":"gate_b_pending"},'
    b'"payload_hash":"sha256:fc36cedc23f3c284a76fb9896c62b1f6d2524fd65471d4e8e264bfbff5f6cbeb",'
    b'"previous_event_hash":"sha256:'
    b'3333333333333333333333333333333333333333333333333333333333333333",'
    b'"reason_code":null,"recorded_at":"2026-08-18T03:00:00Z",'
    b'"run_id":"WISH-LEGACY-001","sequence":4,"task_id":null}\n'
)


def manifest_from(value: dict[str, object] | None = None):
    decoded = decode_manifest_primitive(valid_manifest() if value is None else value)
    if not decoded.ok or decoded.value is None:
        raise AssertionError(decoded.report.render_text())
    return decoded.value


def hash_ref(number: int) -> str:
    return "sha256:" + f"{number:064x}"


def transition(
    snapshot: KernelSnapshot,
    event_type: JournalEventType,
    subject: TransitionSubject,
    from_state: RuntimeState,
    to_state: RuntimeState,
    *,
    task_id: str | None = None,
    attempt: int | None = None,
    correlation_id: str | None = None,
    epoch: int | None = None,
    reason_code: RuntimeReasonCode | None = None,
) -> StateTransition:
    sequence = snapshot.last_sequence + 1
    return StateTransition(
        sequence=sequence,
        event_id=f"EVENT-{sequence:06d}",
        event_hash=hash_ref(sequence),
        previous_event_hash=snapshot.last_event_hash,
        event_type=event_type,
        subject=subject,
        from_state=from_state,
        to_state=to_state,
        identity=ExecutionIdentity(
            snapshot.run_id,
            snapshot.coordinator_epoch if epoch is None else epoch,
            task_id,
            attempt,
            correlation_id,
        ),
        reason_code=reason_code,
    )


def accepted(snapshot: KernelSnapshot, item: StateTransition) -> KernelSnapshot:
    result = apply_transition(snapshot, item)
    if not result.accepted:
        raise AssertionError(result.reason)
    return result.snapshot


def freeze_graph(snapshot: KernelSnapshot) -> tuple[KernelSnapshot, tuple[StateTransition, ...]]:
    transitions = (
        (JournalEventType.RUN_INITIALIZED, RuntimeState.NONE, RuntimeState.PREFLIGHT),
        (
            JournalEventType.PREFLIGHT_COMPLETED,
            RuntimeState.PREFLIGHT,
            RuntimeState.DISCOVERY,
        ),
        (
            JournalEventType.DISCOVERY_COMPLETED,
            RuntimeState.DISCOVERY,
            RuntimeState.GATE_A_PENDING,
        ),
        (
            JournalEventType.GATE_APPROVED,
            RuntimeState.GATE_A_PENDING,
            RuntimeState.TRELLIS_PREPARATION,
        ),
        (
            JournalEventType.TRELLIS_GRAPH_IMPORTED,
            RuntimeState.TRELLIS_PREPARATION,
            RuntimeState.GATE_B_PENDING,
        ),
        (
            JournalEventType.TASK_GRAPH_FROZEN,
            RuntimeState.GATE_B_PENDING,
            RuntimeState.EXECUTING,
        ),
    )
    emitted: list[StateTransition] = []
    for event_type, from_state, to_state in transitions:
        item = transition(
            snapshot,
            event_type,
            TransitionSubject.RUN,
            from_state,
            to_state,
        )
        emitted.append(item)
        snapshot = accepted(snapshot, item)
    return snapshot, tuple(emitted)


def complete_task(
    snapshot: KernelSnapshot, task_id: str
) -> tuple[KernelSnapshot, tuple[StateTransition, ...]]:
    steps = (
        (JournalEventType.TASK_READY, RuntimeState.APPROVED, RuntimeState.READY),
        (JournalEventType.LEASE_ACQUIRED, RuntimeState.READY, RuntimeState.LEASED),
        (
            JournalEventType.DISPATCH_OBSERVED,
            RuntimeState.LEASED,
            RuntimeState.DISPATCHED,
        ),
        (
            JournalEventType.PR_OBSERVED,
            RuntimeState.DISPATCHED,
            RuntimeState.PR_OPEN,
        ),
        (
            JournalEventType.MERGE_OBSERVED,
            RuntimeState.PR_OPEN,
            RuntimeState.MERGED,
        ),
        (
            JournalEventType.TASK_VERIFIED,
            RuntimeState.MERGED,
            RuntimeState.VERIFIED,
        ),
    )
    emitted: list[StateTransition] = []
    for event_type, from_state, to_state in steps:
        item = transition(
            snapshot,
            event_type,
            TransitionSubject.TASK,
            from_state,
            to_state,
            task_id=task_id,
        )
        emitted.append(item)
        snapshot = accepted(snapshot, item)
    return snapshot, tuple(emitted)


class DagTests(unittest.TestCase):
    def test_compile_is_stable_and_matches_independent_reference(self) -> None:
        manifest = manifest_from()
        dag = TaskDag.compile(manifest)
        self.assertEqual(topological_order(manifest), dag.topological_order)
        self.assertEqual(("TASK-001", "TASK-002", "TASK-003", "TASK-004"), dag.topological_order)
        self.assertEqual(4, dag.edge_count)
        self.assertEqual(("TASK-002", "TASK-003", "TASK-004"), dag.descendants("TASK-001"))

        states = {task_id: RuntimeState.APPROVED for task_id in dag.topological_order}
        self.assertEqual(("TASK-001",), dag.ready(states))
        states["TASK-001"] = RuntimeState.VERIFIED
        conflicts = {
            node.task_id: frozenset(node.ownership_conflicts) for node in dag.nodes
        }
        self.assertEqual(
            ready_tasks(manifest, states, conflicts),
            dag.ready(states),
        )
        self.assertEqual(("TASK-002", "TASK-003"), dag.ready(states))
        self.assertEqual(("TASK-002", "TASK-003"), dag.blocked_by("TASK-004", states))

    def test_parallel_ownership_conflict_is_precomputed_and_enforced(self) -> None:
        value = valid_manifest()
        value["tasks"][2]["owned_paths"] = ["src/a/nested/**"]
        manifest = manifest_from(value)
        dag = TaskDag.compile(manifest)
        self.assertEqual(("TASK-003",), dag.node("TASK-002").ownership_conflicts)
        states = {task_id: RuntimeState.APPROVED for task_id in dag.topological_order}
        states["TASK-001"] = RuntimeState.VERIFIED
        self.assertEqual(("TASK-002",), dag.ready(states))

    def test_missing_self_cycle_and_state_set_fail_closed(self) -> None:
        manifest = manifest_from()
        task = manifest.tasks[0]
        cases = (
            ("missing_dependency", (dataclasses.replace(task, depends_on=("TASK-999",)), *manifest.tasks[1:])),
            ("self_dependency", (dataclasses.replace(task, depends_on=(task.id,)), *manifest.tasks[1:])),
            (
                "dependency_cycle",
                (
                    dataclasses.replace(task, depends_on=("TASK-004",)),
                    *manifest.tasks[1:],
                ),
            ),
        )
        for reason, tasks in cases:
            with self.subTest(reason=reason):
                candidate = dataclasses.replace(manifest, tasks=tuple(tasks))
                with self.assertRaisesRegex(DagError, reason):
                    TaskDag.compile(candidate)

        dag = TaskDag.compile(manifest)
        with self.assertRaisesRegex(DagError, "state_task_set_mismatch"):
            dag.ready({"TASK-001": RuntimeState.APPROVED})

    def test_dag_defensive_boundaries_are_typed(self) -> None:
        manifest = manifest_from()
        dag = TaskDag.compile(manifest)
        with self.assertRaisesRegex(TypeError, "ExecutionManifest"):
            TaskDag.compile(object())  # type: ignore[arg-type]
        with self.assertRaises(KeyError):
            dag.node("TASK-999")
        with self.assertRaisesRegex(TypeError, "mapping"):
            dag.ready([])  # type: ignore[arg-type]
        invalid_states = {
            task_id: RuntimeState.APPROVED for task_id in dag.topological_order
        }
        invalid_states["TASK-001"] = "approved"  # type: ignore[assignment]
        with self.assertRaisesRegex(TypeError, "RuntimeState"):
            dag.ready(invalid_states)  # type: ignore[arg-type]
        valid_states = {
            task_id: RuntimeState.APPROVED for task_id in dag.topological_order
        }
        with self.assertRaisesRegex(DagError, "unknown_active_task"):
            dag.ready(valid_states, active_task_ids=("TASK-999",))

        forged = object.__new__(type(manifest))
        object.__setattr__(forged, "tasks", ())
        with self.assertRaisesRegex(DagError, "task_limit_exceeded"):
            TaskDag.compile(forged)

        dense_edges = tuple(
            SimpleNamespace(
                id=f"TASK-{index:03d}",
                depends_on=("TASK-000",) * (MAX_GRAPH_EDGES + 1)
                if index == 63
                else (),
                owned_paths=(f"src/{index}/**",),
                wave=1,
            )
            for index in range(64)
        )
        object.__setattr__(forged, "tasks", dense_edges)
        with self.assertRaisesRegex(DagError, "edge_limit_exceeded"):
            TaskDag.compile(forged)

        blocked_states = dict(valid_states)
        self.assertEqual(
            ("TASK-001", "TASK-002", "TASK-003"),
            dag.blocked_by("TASK-004", blocked_states),
        )


class StateKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = manifest_from()
        self.dag = TaskDag.compile(self.manifest)
        self.initial = KernelSnapshot.initial("WISH-001", 1, self.dag)

    def test_graph_freeze_and_all_wave_one_completion_orders_are_equivalent(self) -> None:
        frozen, prefix = freeze_graph(self.initial)
        self.assertEqual(("TASK-001",), frozen.ready(self.dag))
        after_wave_zero, wave_zero = complete_task(frozen, "TASK-001")
        self.assertEqual(("TASK-002", "TASK-003"), after_wave_zero.ready(self.dag))

        semantic_results = []
        for order in itertools.permutations(("TASK-002", "TASK-003")):
            current = after_wave_zero
            emitted: list[StateTransition] = [*prefix, *wave_zero]
            for task_id in order:
                current, task_events = complete_task(current, task_id)
                emitted.extend(task_events)
            semantic_results.append((current.task_states(), current.ready(self.dag)))
            replayed = replay(self.initial, tuple(emitted))
            self.assertTrue(replayed.accepted, replayed.reason)
            self.assertEqual(current, replayed.snapshot)

        self.assertEqual(semantic_results[0], semantic_results[1])
        self.assertEqual(("TASK-004",), semantic_results[0][1])

    def test_task_definition_is_immutable_while_runtime_state_lives_in_projection(self) -> None:
        self.assertIs(TaskDefinition, ManifestTask)
        definition_fields = {field.name for field in dataclasses.fields(TaskDefinition)}
        lifecycle_fields = {
            "status",
            "state",
            "reason_code",
            "attempt",
            "branch",
            "pr_id",
            "squash_commit",
            "agent_owner",
        }
        self.assertTrue(lifecycle_fields.isdisjoint(definition_fields))

        projection = TaskProjection("TASK-001", RuntimeState.READY)
        projection_fields = {field.name for field in dataclasses.fields(TaskProjection)}
        self.assertTrue({"task_id", "state", "reason_code"}.issubset(projection_fields))
        self.assertEqual(RuntimeState.READY, projection.state)

    def test_legacy_decomposition_bytes_decode_and_replay_without_rewriting(self) -> None:
        decoded = decode_journal_event_bytes(LEGACY_DECOMPOSITION_EVENT_BYTES)
        self.assertTrue(decoded.ok, decoded.report.render_text())
        event = decoded.value
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(LEGACY_DECOMPOSITION_EVENT_BYTES, event.canonical_json_bytes())
        self.assertEqual(
            "sha256:e480a072e20c93e80f827400d63fa0a29c97728999f05299856d897a64b63613",
            event.event_hash,
        )

        initial = KernelSnapshot.initial("WISH-LEGACY-001", 1, self.dag)
        legacy_snapshot = dataclasses.replace(
            initial,
            phase=RuntimeState.DECOMPOSITION,
            last_sequence=3,
            last_event_id="EVENT-LEGACY-003",
            last_event_hash="sha256:" + "3" * 64,
        )
        replayed = apply_journal_event(legacy_snapshot, event)
        self.assertTrue(replayed.accepted, replayed.reason)
        self.assertEqual(RuntimeState.GATE_B_PENDING, replayed.snapshot.phase)

    def test_exact_duplicate_conflict_stale_gap_hash_and_epoch_are_typed(self) -> None:
        frozen, _ = freeze_graph(self.initial)
        item = transition(
            frozen,
            JournalEventType.TASK_READY,
            TransitionSubject.TASK,
            RuntimeState.APPROVED,
            RuntimeState.READY,
            task_id="TASK-001",
        )
        applied = apply_transition(frozen, item)
        self.assertTrue(applied.accepted)
        duplicate = apply_transition(applied.snapshot, item)
        self.assertTrue(duplicate.accepted)
        self.assertEqual(ApplyReason.IDEMPOTENT_REPLAY, duplicate.reason)

        conflict = dataclasses.replace(item, event_hash=hash_ref(999))
        self.assertEqual(
            ApplyReason.SEQUENCE_CONFLICT,
            apply_transition(applied.snapshot, conflict).reason,
        )
        gap = dataclasses.replace(
            item,
            sequence=item.sequence + 2,
            event_id="EVENT-999998",
            event_hash=hash_ref(998),
        )
        self.assertEqual(ApplyReason.SEQUENCE_GAP, apply_transition(applied.snapshot, gap).reason)

        next_item = transition(
            applied.snapshot,
            JournalEventType.LEASE_ACQUIRED,
            TransitionSubject.TASK,
            RuntimeState.READY,
            RuntimeState.LEASED,
            task_id="TASK-001",
        )
        after_next = accepted(applied.snapshot, next_item)
        self.assertEqual(ApplyReason.STALE_SEQUENCE, apply_transition(after_next, item).reason)

        wrong_hash = dataclasses.replace(
            next_item,
            sequence=after_next.last_sequence + 1,
            event_id="EVENT-999997",
            event_hash=hash_ref(997),
            previous_event_hash=hash_ref(996),
        )
        self.assertEqual(
            ApplyReason.HASH_CHAIN_MISMATCH,
            apply_transition(after_next, wrong_hash).reason,
        )
        wrong_epoch = transition(
            after_next,
            JournalEventType.TASK_BLOCKED,
            TransitionSubject.TASK,
            RuntimeState.LEASED,
            RuntimeState.BLOCKED,
            task_id="TASK-001",
            epoch=2,
            reason_code=RuntimeReasonCode.LEASE_LOST,
        )
        self.assertEqual(ApplyReason.STALE_EPOCH, apply_transition(after_next, wrong_epoch).reason)

    def test_attempt_identity_rejects_active_stale_and_wrong_correlation_results(self) -> None:
        frozen, _ = freeze_graph(self.initial)
        reserve = transition(
            frozen,
            JournalEventType.ATTEMPT_RESERVED,
            TransitionSubject.ATTEMPT,
            RuntimeState.PLANNED,
            RuntimeState.RESERVED,
            task_id="TASK-001",
            attempt=1,
            correlation_id="CORRELATION-001",
        )
        reserved = accepted(frozen, reserve)
        second = transition(
            reserved,
            JournalEventType.ATTEMPT_RESERVED,
            TransitionSubject.ATTEMPT,
            RuntimeState.PLANNED,
            RuntimeState.RESERVED,
            task_id="TASK-001",
            attempt=2,
            correlation_id="CORRELATION-002",
        )
        self.assertEqual(
            ApplyReason.ACTIVE_ATTEMPT_EXISTS,
            apply_transition(reserved, second).reason,
        )

        dispatch = transition(
            reserved,
            JournalEventType.DISPATCH_REQUESTED,
            TransitionSubject.ATTEMPT,
            RuntimeState.RESERVED,
            RuntimeState.DISPATCH_REQUESTED,
            task_id="TASK-001",
            attempt=1,
            correlation_id="CORRELATION-001",
        )
        dispatched = accepted(reserved, dispatch)
        wrong_correlation = transition(
            dispatched,
            JournalEventType.ATTEMPT_FAILED,
            TransitionSubject.ATTEMPT,
            RuntimeState.DISPATCH_REQUESTED,
            RuntimeState.FAILED,
            task_id="TASK-001",
            attempt=1,
            correlation_id="CORRELATION-999",
            reason_code=RuntimeReasonCode.PROCESS_START_FAILED,
        )
        self.assertEqual(
            ApplyReason.STALE_CORRELATION,
            apply_transition(dispatched, wrong_correlation).reason,
        )

        missing_attempt = dataclasses.replace(
            wrong_correlation,
            identity=ExecutionIdentity(
                "WISH-001", 1, "TASK-001", 99, "CORRELATION-099"
            ),
        )
        self.assertEqual(
            ApplyReason.STALE_ATTEMPT,
            apply_transition(dispatched, missing_attempt).reason,
        )

    def test_terminated_reservation_can_only_be_reclaimed_by_a_higher_epoch(self) -> None:
        frozen, _ = freeze_graph(self.initial)
        reserved = accepted(
            frozen,
            transition(
                frozen,
                JournalEventType.ATTEMPT_RESERVED,
                TransitionSubject.ATTEMPT,
                RuntimeState.PLANNED,
                RuntimeState.RESERVED,
                task_id="TASK-001",
                attempt=1,
                correlation_id="CORRELATION-OLD",
            ),
        )
        terminated = accepted(
            reserved,
            transition(
                reserved,
                JournalEventType.ATTEMPT_RELEASED,
                TransitionSubject.ATTEMPT,
                RuntimeState.RESERVED,
                RuntimeState.TERMINATED,
                task_id="TASK-001",
                attempt=1,
                correlation_id="CORRELATION-OLD",
                reason_code=RuntimeReasonCode.LEASE_LOST,
            ),
        )
        same_epoch = transition(
            terminated,
            JournalEventType.ATTEMPT_RESERVED,
            TransitionSubject.ATTEMPT,
            RuntimeState.TERMINATED,
            RuntimeState.RESERVED,
            task_id="TASK-001",
            attempt=1,
            correlation_id="CORRELATION-NEW",
        )
        self.assertEqual(
            ApplyReason.STALE_EPOCH,
            apply_transition(terminated, same_epoch).reason,
        )

        takeover = dataclasses.replace(terminated, coordinator_epoch=2)
        reclaimed = accepted(
            takeover,
            transition(
                takeover,
                JournalEventType.ATTEMPT_RESERVED,
                TransitionSubject.ATTEMPT,
                RuntimeState.TERMINATED,
                RuntimeState.RESERVED,
                task_id="TASK-001",
                attempt=1,
                correlation_id="CORRELATION-NEW",
                epoch=2,
            ),
        )
        self.assertEqual(1, len(reclaimed.attempts))
        self.assertEqual(1, reclaimed.attempts[0].attempt)
        self.assertEqual(2, reclaimed.attempts[0].coordinator_epoch)
        self.assertEqual("CORRELATION-NEW", reclaimed.attempts[0].correlation_id)
        self.assertIs(RuntimeState.RESERVED, reclaimed.attempts[0].state)

    def test_illegal_transition_and_blocked_run_close_readiness(self) -> None:
        frozen, _ = freeze_graph(self.initial)
        illegal = transition(
            frozen,
            JournalEventType.TASK_VERIFIED,
            TransitionSubject.TASK,
            RuntimeState.APPROVED,
            RuntimeState.VERIFIED,
            task_id="TASK-001",
        )
        self.assertEqual(
            ApplyReason.ILLEGAL_TRANSITION,
            apply_transition(frozen, illegal).reason,
        )

        blocked = transition(
            frozen,
            JournalEventType.RUN_BLOCKED,
            TransitionSubject.RUN,
            RuntimeState.RUNNING,
            RuntimeState.BLOCKED,
            reason_code=RuntimeReasonCode.INVARIANT_VIOLATION,
        )
        blocked_snapshot = accepted(frozen, blocked)
        self.assertEqual((), blocked_snapshot.ready(self.dag))
        self.assertEqual(
            RuntimeReasonCode.INVARIANT_VIOLATION,
            blocked_snapshot.run_reason_code,
        )

    def test_transition_contract_and_journal_adapter_boundaries(self) -> None:
        base = StateTransition(
            1,
            "EVENT-000001",
            hash_ref(1),
            GENESIS_HASH,
            JournalEventType.RUN_INITIALIZED,
            TransitionSubject.RUN,
            RuntimeState.NONE,
            RuntimeState.PREFLIGHT,
            ExecutionIdentity("WISH-001", 1),
        )
        cases = (
            ({"sequence": 0}, ValueError),
            ({"event_id": "bad"}, ValueError),
            ({"event_hash": "sha256:BAD"}, ValueError),
            ({"previous_event_hash": "sha256:BAD"}, ValueError),
            ({"event_type": "run_initialized"}, TypeError),
            ({"subject": "run"}, TypeError),
            ({"from_state": "none"}, TypeError),
            ({"identity": object()}, TypeError),
            ({"reason_code": "invariant_violation"}, TypeError),
        )
        for updates, error in cases:
            with self.subTest(field=tuple(updates)):
                with self.assertRaises(error):
                    dataclasses.replace(base, **updates)
        with self.assertRaisesRegex(ValueError, "reason_code"):
            dataclasses.replace(
                base,
                event_type=JournalEventType.RUN_BLOCKED,
                from_state=RuntimeState.RUNNING,
                to_state=RuntimeState.BLOCKED,
            )

        event = JournalEvent.create(
            sequence=1,
            event_id="EVENT-000001",
            event_type=JournalEventType.RUN_INITIALIZED,
            identity=ExecutionIdentity("WISH-001", 1),
            actor_type=ActorType.SYSTEM,
            actor_id="state-kernel",
            recorded_at="2026-08-18T03:00:00Z",
            previous_event_hash=GENESIS_HASH,
            payload=TransitionPayload(
                TransitionSubject.RUN,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
        )
        self.assertEqual(
            dataclasses.replace(base, event_hash=event.event_hash),
            StateTransition.from_journal_event(event),
        )
        with self.assertRaisesRegex(TypeError, "JournalEvent"):
            StateTransition.from_journal_event(object())  # type: ignore[arg-type]
        forged = object.__new__(JournalEvent)
        object.__setattr__(forged, "payload", object())
        with self.assertRaisesRegex(Exception, "unsupported_event"):
            StateTransition.from_journal_event(forged)

        with self.assertRaisesRegex(TypeError, "StateTransition"):
            validate_transition(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "TaskDag"):
            KernelSnapshot.initial("WISH-001", 1, object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "KernelSnapshot"):
            apply_transition(object(), base)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "StateTransition"):
            apply_transition(self.initial, object())  # type: ignore[arg-type]

    def test_run_and_task_identity_state_and_archive_guards(self) -> None:
        run_mismatch = dataclasses.replace(
            transition(
                self.initial,
                JournalEventType.RUN_INITIALIZED,
                TransitionSubject.RUN,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
            identity=ExecutionIdentity("WISH-999", 1),
        )
        self.assertEqual(
            ApplyReason.RUN_MISMATCH,
            apply_transition(self.initial, run_mismatch).reason,
        )
        run_with_task = dataclasses.replace(
            run_mismatch,
            identity=ExecutionIdentity("WISH-001", 1, "TASK-001"),
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_transition(self.initial, run_with_task).reason,
        )
        wrong_run_state = transition(
            self.initial,
            JournalEventType.PREFLIGHT_COMPLETED,
            TransitionSubject.RUN,
            RuntimeState.PREFLIGHT,
            RuntimeState.DISCOVERY,
        )
        self.assertEqual(
            ApplyReason.STATE_MISMATCH,
            apply_transition(self.initial, wrong_run_state).reason,
        )
        premature_archive = transition(
            self.initial,
            JournalEventType.RUN_ARCHIVED,
            TransitionSubject.RUN,
            RuntimeState.RUNNING,
            RuntimeState.ARCHIVED,
        )
        self.assertEqual(
            ApplyReason.STATE_MISMATCH,
            apply_transition(self.initial, premature_archive).reason,
        )

        before_freeze = self.initial
        for event_type, from_state, to_state in (
            (JournalEventType.RUN_INITIALIZED, RuntimeState.NONE, RuntimeState.PREFLIGHT),
            (JournalEventType.PREFLIGHT_COMPLETED, RuntimeState.PREFLIGHT, RuntimeState.DISCOVERY),
            (JournalEventType.DISCOVERY_COMPLETED, RuntimeState.DISCOVERY, RuntimeState.GATE_A_PENDING),
            (
                JournalEventType.GATE_APPROVED,
                RuntimeState.GATE_A_PENDING,
                RuntimeState.TRELLIS_PREPARATION,
            ),
            (
                JournalEventType.TRELLIS_GRAPH_IMPORTED,
                RuntimeState.TRELLIS_PREPARATION,
                RuntimeState.GATE_B_PENDING,
            ),
        ):
            before_freeze = accepted(
                before_freeze,
                transition(
                    before_freeze,
                    event_type,
                    TransitionSubject.RUN,
                    from_state,
                    to_state,
                ),
            )
        changed_tasks = (
            dataclasses.replace(before_freeze.tasks[0], state=RuntimeState.APPROVED),
            *before_freeze.tasks[1:],
        )
        inconsistent = dataclasses.replace(before_freeze, tasks=changed_tasks)
        freeze = transition(
            inconsistent,
            JournalEventType.TASK_GRAPH_FROZEN,
            TransitionSubject.RUN,
            RuntimeState.GATE_B_PENDING,
            RuntimeState.EXECUTING,
        )
        self.assertEqual(
            ApplyReason.STATE_MISMATCH,
            apply_transition(inconsistent, freeze).reason,
        )

        task_without_id = transition(
            before_freeze,
            JournalEventType.TASK_READY,
            TransitionSubject.TASK,
            RuntimeState.APPROVED,
            RuntimeState.READY,
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_transition(before_freeze, task_without_id).reason,
        )
        unknown_task = dataclasses.replace(
            task_without_id,
            identity=ExecutionIdentity("WISH-001", 1, "TASK-999"),
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_transition(before_freeze, unknown_task).reason,
        )
        task_state_mismatch = dataclasses.replace(
            task_without_id,
            identity=ExecutionIdentity("WISH-001", 1, "TASK-001"),
        )
        self.assertEqual(
            ApplyReason.STATE_MISMATCH,
            apply_transition(before_freeze, task_state_mismatch).reason,
        )

    def test_attempt_missing_identity_state_and_old_number_are_rejected(self) -> None:
        frozen, _ = freeze_graph(self.initial)
        no_attempt = transition(
            frozen,
            JournalEventType.ATTEMPT_RESERVED,
            TransitionSubject.ATTEMPT,
            RuntimeState.PLANNED,
            RuntimeState.RESERVED,
            task_id="TASK-001",
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_transition(frozen, no_attempt).reason,
        )
        unknown_task = dataclasses.replace(
            no_attempt,
            identity=ExecutionIdentity(
                "WISH-001", 1, "TASK-999", 1, "CORRELATION-001"
            ),
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_transition(frozen, unknown_task).reason,
        )

        reserve_two = dataclasses.replace(
            no_attempt,
            identity=ExecutionIdentity(
                "WISH-001", 1, "TASK-001", 2, "CORRELATION-002"
            ),
        )
        reserved = accepted(frozen, reserve_two)
        release = transition(
            reserved,
            JournalEventType.ATTEMPT_RELEASED,
            TransitionSubject.ATTEMPT,
            RuntimeState.RESERVED,
            RuntimeState.TERMINATED,
            task_id="TASK-001",
            attempt=2,
            correlation_id="CORRELATION-002",
        )
        terminated = accepted(reserved, release)
        old_number = transition(
            terminated,
            JournalEventType.ATTEMPT_RESERVED,
            TransitionSubject.ATTEMPT,
            RuntimeState.PLANNED,
            RuntimeState.RESERVED,
            task_id="TASK-001",
            attempt=1,
            correlation_id="CORRELATION-001",
        )
        self.assertEqual(
            ApplyReason.STALE_ATTEMPT,
            apply_transition(terminated, old_number).reason,
        )

        wrong_state = transition(
            reserved,
            JournalEventType.ATTEMPT_SUCCEEDED,
            TransitionSubject.ATTEMPT,
            RuntimeState.RUNNING,
            RuntimeState.SUCCEEDED,
            task_id="TASK-001",
            attempt=2,
            correlation_id="CORRELATION-002",
        )
        self.assertEqual(
            ApplyReason.STATE_MISMATCH,
            apply_transition(reserved, wrong_state).reason,
        )

        invalid_replay = replay(self.initial, (old_number,))
        self.assertFalse(invalid_replay.accepted)

    def test_projection_constructor_invariants_reject_invalid_state(self) -> None:
        valid_task = TaskProjection("TASK-001")
        task_cases = (
            ({"task_id": "bad"}, ValueError),
            ({"state": RuntimeState.RUNNING}, ValueError),
            ({"reason_code": "invariant_violation"}, TypeError),
            ({"state": RuntimeState.BLOCKED}, ValueError),
            (
                {
                    "state": RuntimeState.APPROVED,
                    "reason_code": RuntimeReasonCode.INVARIANT_VIOLATION,
                },
                ValueError,
            ),
        )
        for updates, error in task_cases:
            with self.subTest(projection="task", field=tuple(updates)):
                with self.assertRaises(error):
                    dataclasses.replace(valid_task, **updates)

        valid_attempt = AttemptProjection(
            "TASK-001",
            1,
            "CORRELATION-001",
            1,
            RuntimeState.RESERVED,
        )
        attempt_cases = (
            ({"task_id": "bad"}, ValueError),
            ({"attempt": 0}, ValueError),
            ({"correlation_id": "bad"}, ValueError),
            ({"coordinator_epoch": 0}, ValueError),
            ({"state": RuntimeState.READY}, ValueError),
            ({"reason_code": "process_start_failed"}, TypeError),
            ({"state": RuntimeState.FAILED}, ValueError),
            (
                {
                    "state": RuntimeState.RESERVED,
                    "reason_code": RuntimeReasonCode.PROCESS_START_FAILED,
                },
                ValueError,
            ),
        )
        for updates, error in attempt_cases:
            with self.subTest(projection="attempt", field=tuple(updates)):
                with self.assertRaises(error):
                    dataclasses.replace(valid_attempt, **updates)

        for state in (RuntimeState.CANCEL_REQUESTED, RuntimeState.TERMINATED):
            with self.subTest(reason_retaining_state=state):
                retained = dataclasses.replace(
                    valid_attempt,
                    state=state,
                    reason_code=RuntimeReasonCode.CANCELLED_BY_USER,
                )
                self.assertEqual(RuntimeReasonCode.CANCELLED_BY_USER, retained.reason_code)

    def test_snapshot_and_apply_result_constructor_invariants(self) -> None:
        attempt = AttemptProjection(
            "TASK-001",
            1,
            "CORRELATION-001",
            1,
            RuntimeState.RESERVED,
        )
        snapshot_cases = (
            ({"phase": RuntimeState.RUNNING}, ValueError),
            ({"status": RuntimeState.EXECUTING}, ValueError),
            ({"run_reason_code": "invariant_violation"}, TypeError),
            ({"status": RuntimeState.BLOCKED}, ValueError),
            (
                {
                    "status": RuntimeState.RUNNING,
                    "run_reason_code": RuntimeReasonCode.INVARIANT_VIOLATION,
                },
                ValueError,
            ),
            ({"tasks": list(self.initial.tasks)}, TypeError),
            ({"tasks": ()}, ValueError),
            ({"tasks": (self.initial.tasks[0], self.initial.tasks[0])}, ValueError),
            ({"attempts": [attempt]}, TypeError),
            ({"attempts": (attempt, attempt)}, ValueError),
            (
                {
                    "attempts": (
                        dataclasses.replace(attempt, task_id="TASK-999"),
                    )
                },
                ValueError,
            ),
            ({"last_sequence": -1}, ValueError),
            ({"last_event_hash": "sha256:BAD"}, ValueError),
            ({"last_event_id": "EVENT-000001"}, ValueError),
            ({"last_event_hash": hash_ref(1)}, ValueError),
            (
                {
                    "last_sequence": 1,
                    "last_event_id": None,
                    "last_event_hash": hash_ref(1),
                },
                ValueError,
            ),
        )
        for updates, error in snapshot_cases:
            with self.subTest(snapshot_field=tuple(updates)):
                with self.assertRaises(error):
                    dataclasses.replace(self.initial, **updates)

        valid = ApplyResult(True, ApplyReason.APPLIED, self.initial)
        result_cases = (
            ({"accepted": 1}, TypeError),
            ({"reason": "applied"}, TypeError),
            ({"snapshot": object()}, TypeError),
            ({"accepted": False}, ValueError),
            ({"reason": ApplyReason.STATE_MISMATCH}, ValueError),
        )
        for updates, error in result_cases:
            with self.subTest(result_field=tuple(updates)):
                with self.assertRaises(error):
                    dataclasses.replace(valid, **updates)

    def test_cancelled_run_inherits_request_reason_and_replay_validates_arguments(self) -> None:
        cancelling = transition(
            self.initial,
            JournalEventType.CANCEL_REQUESTED,
            TransitionSubject.RUN,
            RuntimeState.RUNNING,
            RuntimeState.CANCELLING,
            reason_code=RuntimeReasonCode.CANCELLED_BY_USER,
        )
        cancelling_snapshot = accepted(self.initial, cancelling)
        cancelled = transition(
            cancelling_snapshot,
            JournalEventType.RUN_CANCELLED,
            TransitionSubject.RUN,
            RuntimeState.CANCELLING,
            RuntimeState.CANCELLED,
        )
        cancelled_snapshot = accepted(cancelling_snapshot, cancelled)
        self.assertEqual(
            RuntimeReasonCode.CANCELLED_BY_USER,
            cancelled_snapshot.run_reason_code,
        )

        with self.assertRaisesRegex(TypeError, "KernelSnapshot"):
            replay(object(), ())  # type: ignore[arg-type]
        for invalid in ([], (object(),)):
            with self.subTest(transitions_type=type(invalid).__name__):
                with self.assertRaisesRegex(TypeError, "StateTransition"):
                    replay(self.initial, invalid)  # type: ignore[arg-type]

    def test_dispatch_effect_events_atomically_advance_attempt_and_task(self) -> None:
        frozen, _ = freeze_graph(self.initial)
        ready = accepted(
            frozen,
            transition(
                frozen,
                JournalEventType.TASK_READY,
                TransitionSubject.TASK,
                RuntimeState.APPROVED,
                RuntimeState.READY,
                task_id="TASK-001",
            ),
        )
        leased = accepted(
            ready,
            transition(
                ready,
                JournalEventType.LEASE_ACQUIRED,
                TransitionSubject.TASK,
                RuntimeState.READY,
                RuntimeState.LEASED,
                task_id="TASK-001",
            ),
        )
        identity = ExecutionIdentity(
            "WISH-001",
            1,
            "TASK-001",
            1,
            "CORRELATION-001",
        )
        reserved = accepted(
            leased,
            transition(
                leased,
                JournalEventType.ATTEMPT_RESERVED,
                TransitionSubject.ATTEMPT,
                RuntimeState.PLANNED,
                RuntimeState.RESERVED,
                task_id="TASK-001",
                attempt=1,
                correlation_id="CORRELATION-001",
            ),
        )
        request_event = JournalEvent.create(
            sequence=reserved.last_sequence + 1,
            event_id="EVENT-DISPATCH-REQUEST",
            event_type=JournalEventType.DISPATCH_REQUESTED,
            identity=identity,
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-001",
            recorded_at="2026-08-18T03:00:00Z",
            previous_event_hash=reserved.last_event_hash,
            payload=EffectRequestPayload(
                EffectOperation.WORKER_DISPATCH,
                AdapterKind.TASK,
                EffectObjectType.WORKER,
                hash_ref(70),
                hash_ref(71),
                reserved.last_sequence,
                1,
            ),
        )
        requested = apply_journal_event(reserved, request_event)
        self.assertTrue(requested.accepted, requested.reason)
        self.assertEqual(RuntimeState.LEASED, requested.snapshot.tasks[0].state)
        self.assertEqual(RuntimeState.DISPATCH_REQUESTED, requested.snapshot.attempts[0].state)

        receipt = EffectReceipt(
            1,
            identity,
            EffectOperation.WORKER_DISPATCH,
            EffectStatus.APPLIED,
            "2026-08-18T03:00:01Z",
            effect_hash=hash_ref(72),
            external_object_id="worker-001",
        )
        observed_event = JournalEvent.create(
            sequence=request_event.sequence + 1,
            event_id="EVENT-DISPATCH-OBSERVED",
            event_type=JournalEventType.DISPATCH_OBSERVED,
            identity=identity,
            actor_type=ActorType.ADAPTER,
            actor_id="fake-task-adapter",
            recorded_at="2026-08-18T03:00:01Z",
            previous_event_hash=request_event.event_hash,
            payload=EffectObservationPayload(AdapterKind.TASK, receipt),
        )
        observed = apply_journal_event(requested.snapshot, observed_event)
        self.assertTrue(observed.accepted, observed.reason)
        self.assertEqual(RuntimeState.DISPATCHED, observed.snapshot.tasks[0].state)
        self.assertEqual(RuntimeState.RUNNING, observed.snapshot.attempts[0].state)
        self.assertEqual(
            ApplyReason.IDEMPOTENT_REPLAY,
            apply_journal_event(observed.snapshot, observed_event).reason,
        )

        takeover_snapshot = dataclasses.replace(
            requested.snapshot,
            coordinator_epoch=2,
        )
        takeover_identity = ExecutionIdentity(
            "WISH-001",
            2,
            "TASK-001",
            1,
            "CORRELATION-001",
        )
        takeover_observed_event = JournalEvent.create(
            sequence=request_event.sequence + 1,
            event_id="EVENT-DISPATCH-OBSERVED-TAKEOVER",
            event_type=JournalEventType.DISPATCH_OBSERVED,
            identity=takeover_identity,
            actor_type=ActorType.ADAPTER,
            actor_id="fake-task-adapter",
            recorded_at="2026-08-18T03:00:01Z",
            previous_event_hash=request_event.event_hash,
            payload=EffectObservationPayload(AdapterKind.TASK, receipt),
        )
        takeover_observed = apply_journal_event(
            takeover_snapshot,
            takeover_observed_event,
        )
        self.assertTrue(takeover_observed.accepted, takeover_observed.reason)
        self.assertEqual(
            RuntimeState.RUNNING,
            takeover_observed.snapshot.attempts[0].state,
        )
        self.assertEqual(
            1,
            takeover_observed.snapshot.attempts[0].coordinator_epoch,
        )

        for invalid_epoch in (1, 2):
            with self.subTest(invalid_takeover_epoch=invalid_epoch):
                invalid_identity = ExecutionIdentity(
                    "WISH-001",
                    invalid_epoch,
                    "TASK-001",
                    1,
                    "CORRELATION-001",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "effect receipt identity does not match",
                ):
                    JournalEvent.create(
                        sequence=request_event.sequence + 1,
                        event_id=f"EVENT-DISPATCH-INVALID-EPOCH-{invalid_epoch}",
                        event_type=JournalEventType.DISPATCH_OBSERVED,
                        identity=invalid_identity,
                        actor_type=ActorType.ADAPTER,
                        actor_id="fake-task-adapter",
                        recorded_at="2026-08-18T03:00:01Z",
                        previous_event_hash=request_event.event_hash,
                        payload=EffectObservationPayload(
                            AdapterKind.TASK,
                            dataclasses.replace(
                            receipt,
                                identity=dataclasses.replace(
                                    identity,
                                    coordinator_epoch=invalid_epoch + 1,
                                ),
                            ),
                        ),
                    )

        cancelled_event = JournalEvent.create(
            sequence=observed_event.sequence + 1,
            event_id="EVENT-ATTEMPT-CANCEL",
            event_type=JournalEventType.CANCEL_REQUESTED,
            identity=identity,
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-001",
            recorded_at="2026-08-18T03:00:02Z",
            previous_event_hash=observed_event.event_hash,
            payload=TransitionPayload(
                TransitionSubject.ATTEMPT,
                RuntimeState.RUNNING,
                RuntimeState.CANCEL_REQUESTED,
            ),
            reason_code=RuntimeReasonCode.CANCELLED_BY_USER,
        )
        cancelled = apply_journal_event(observed.snapshot, cancelled_event)
        self.assertTrue(cancelled.accepted, cancelled.reason)
        self.assertEqual(RuntimeState.CANCEL_REQUESTED, cancelled.snapshot.attempts[0].state)

        replayed = replay_journal_events(
            reserved,
            (request_event, observed_event, cancelled_event),
        )
        self.assertTrue(replayed.accepted, replayed.reason)
        self.assertEqual(cancelled.snapshot, replayed.snapshot)

        absent = dataclasses.replace(
            receipt,
            status=EffectStatus.ABSENT,
            effect_hash=None,
            external_object_id=None,
        )
        unsupported = JournalEvent.create(
            sequence=request_event.sequence + 1,
            event_id="EVENT-DISPATCH-ABSENT",
            event_type=JournalEventType.DISPATCH_OBSERVED,
            identity=identity,
            actor_type=ActorType.ADAPTER,
            actor_id="fake-task-adapter",
            recorded_at="2026-08-18T03:00:01Z",
            previous_event_hash=request_event.event_hash,
            payload=EffectObservationPayload(AdapterKind.TASK, absent),
        )
        self.assertEqual(
            ApplyReason.UNSUPPORTED_EVENT,
            apply_journal_event(requested.snapshot, unsupported).reason,
        )

    def test_promotion_observation_advances_local_task_to_verification(self) -> None:
        snapshot, _ = freeze_graph(self.initial)
        steps = (
            (JournalEventType.TASK_READY, RuntimeState.APPROVED, RuntimeState.READY),
            (JournalEventType.LEASE_ACQUIRED, RuntimeState.READY, RuntimeState.LEASED),
            (
                JournalEventType.DISPATCH_OBSERVED,
                RuntimeState.LEASED,
                RuntimeState.DISPATCHED,
            ),
            (
                JournalEventType.RESULT_STAGED,
                RuntimeState.DISPATCHED,
                RuntimeState.STAGED,
            ),
        )
        for event_type, from_state, to_state in steps:
            snapshot = accepted(
                snapshot,
                transition(
                    snapshot,
                    event_type,
                    TransitionSubject.TASK,
                    from_state,
                    to_state,
                    task_id="TASK-001",
                ),
            )
        identity = ExecutionIdentity(
            "WISH-001",
            1,
            "TASK-001",
            1,
            "PROMOTION-TASK-001-001",
        )
        receipt = EffectReceipt(
            1,
            identity,
            EffectOperation.RESULT_PROMOTION,
            EffectStatus.APPLIED,
            "2026-08-18T03:00:01Z",
            effect_hash=hash_ref(80),
            external_object_id="promotion-task-001",
        )
        observed_event = JournalEvent.create(
            sequence=snapshot.last_sequence + 1,
            event_id="EVENT-PROMOTION-OBSERVED",
            event_type=JournalEventType.PROMOTION_OBSERVED,
            identity=identity,
            actor_type=ActorType.ADAPTER,
            actor_id="git-worktree-adapter",
            recorded_at="2026-08-18T03:00:01Z",
            previous_event_hash=snapshot.last_event_hash,
            payload=EffectObservationPayload(AdapterKind.GIT, receipt),
        )
        promoted = apply_journal_event(snapshot, observed_event)
        self.assertTrue(promoted.accepted, promoted.reason)
        self.assertEqual(RuntimeState.PROMOTED, promoted.snapshot.tasks[0].state)

        verified = accepted(
            promoted.snapshot,
            transition(
                promoted.snapshot,
                JournalEventType.TASK_VERIFIED,
                TransitionSubject.TASK,
                RuntimeState.PROMOTED,
                RuntimeState.VERIFIED,
                task_id="TASK-001",
            ),
        )
        self.assertEqual(RuntimeState.VERIFIED, verified.tasks[0].state)

        for name, bad_payload in (
            (
                "adapter",
                EffectObservationPayload(AdapterKind.REPOSITORY, receipt),
            ),
            (
                "operation",
                EffectObservationPayload(
                    AdapterKind.GIT,
                    dataclasses.replace(
                        receipt,
                        operation=EffectOperation.RESULT_STAGE,
                    ),
                ),
            ),
        ):
            with self.subTest(name=name):
                forged = JournalEvent.create(
                    sequence=snapshot.last_sequence + 1,
                    event_id=f"EVENT-PROMOTION-BAD-{name.upper()}",
                    event_type=JournalEventType.PROMOTION_OBSERVED,
                    identity=identity,
                    actor_type=ActorType.ADAPTER,
                    actor_id="git-worktree-adapter",
                    recorded_at="2026-08-18T03:00:01Z",
                    previous_event_hash=snapshot.last_event_hash,
                    payload=bad_payload,
                )
                self.assertEqual(
                    ApplyReason.UNSUPPORTED_EVENT,
                    apply_journal_event(snapshot, forged).reason,
                )

    def test_journal_effect_fold_rejects_every_identity_and_order_mismatch(self) -> None:
        frozen, _ = freeze_graph(self.initial)
        ready = accepted(
            frozen,
            transition(
                frozen,
                JournalEventType.TASK_READY,
                TransitionSubject.TASK,
                RuntimeState.APPROVED,
                RuntimeState.READY,
                task_id="TASK-001",
            ),
        )
        leased = accepted(
            ready,
            transition(
                ready,
                JournalEventType.LEASE_ACQUIRED,
                TransitionSubject.TASK,
                RuntimeState.READY,
                RuntimeState.LEASED,
                task_id="TASK-001",
            ),
        )
        identity = ExecutionIdentity(
            "WISH-001",
            1,
            "TASK-001",
            1,
            "CORRELATION-001",
        )
        reserved = accepted(
            leased,
            transition(
                leased,
                JournalEventType.ATTEMPT_RESERVED,
                TransitionSubject.ATTEMPT,
                RuntimeState.PLANNED,
                RuntimeState.RESERVED,
                task_id="TASK-001",
                attempt=1,
                correlation_id="CORRELATION-001",
            ),
        )

        def request_event(
            *,
            event_id: str,
            sequence: int | None = None,
            previous_hash: str | None = None,
            event_identity: ExecutionIdentity = identity,
            operation: EffectOperation = EffectOperation.WORKER_DISPATCH,
            adapter: AdapterKind = AdapterKind.TASK,
            object_type: EffectObjectType = EffectObjectType.WORKER,
            expected_sequence: int | None = None,
        ) -> JournalEvent:
            return JournalEvent.create(
                sequence=(reserved.last_sequence + 1 if sequence is None else sequence),
                event_id=event_id,
                event_type=JournalEventType.DISPATCH_REQUESTED,
                identity=event_identity,
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at="2026-08-18T03:00:00Z",
                previous_event_hash=(
                    reserved.last_event_hash if previous_hash is None else previous_hash
                ),
                payload=EffectRequestPayload(
                    operation,
                    adapter,
                    object_type,
                    hash_ref(70),
                    hash_ref(71),
                    (
                        reserved.last_sequence
                        if expected_sequence is None
                        else expected_sequence
                    ),
                    1,
                ),
            )

        valid_request = request_event(event_id="EVENT-DISPATCH-REQUEST")
        requested = apply_journal_event(reserved, valid_request)
        self.assertTrue(requested.accepted, requested.reason)

        order_cases = (
            (
                ApplyReason.SEQUENCE_CONFLICT,
                request_event(
                    event_id="EVENT-SEQUENCE-CONFLICT",
                    sequence=reserved.last_sequence,
                ),
            ),
            (
                ApplyReason.STALE_SEQUENCE,
                request_event(
                    event_id="EVENT-STALE-SEQUENCE",
                    sequence=reserved.last_sequence - 1,
                ),
            ),
            (
                ApplyReason.SEQUENCE_GAP,
                request_event(
                    event_id="EVENT-SEQUENCE-GAP",
                    sequence=reserved.last_sequence + 2,
                ),
            ),
            (
                ApplyReason.HASH_CHAIN_MISMATCH,
                request_event(
                    event_id="EVENT-HASH-MISMATCH",
                    previous_hash=hash_ref(999),
                ),
            ),
            (
                ApplyReason.RUN_MISMATCH,
                request_event(
                    event_id="EVENT-RUN-MISMATCH",
                    event_identity=ExecutionIdentity(
                        "WISH-999", 1, "TASK-001", 1, "CORRELATION-001"
                    ),
                ),
            ),
            (
                ApplyReason.STALE_EPOCH,
                request_event(
                    event_id="EVENT-EPOCH-MISMATCH",
                    event_identity=ExecutionIdentity(
                        "WISH-001", 2, "TASK-001", 1, "CORRELATION-001"
                    ),
                ),
            ),
        )
        for reason, event in order_cases:
            with self.subTest(reason=reason):
                snapshot = requested.snapshot if reason is ApplyReason.STALE_SEQUENCE else reserved
                result = apply_journal_event(snapshot, event)
                self.assertFalse(result.accepted)
                self.assertEqual(reason, result.reason)
                self.assertIs(snapshot, result.snapshot)

        malformed_requests = (
            request_event(
                event_id="EVENT-WRONG-OPERATION",
                operation=EffectOperation.TASK_EXECUTION,
            ),
            request_event(
                event_id="EVENT-WRONG-ADAPTER",
                adapter=AdapterKind.MODEL,
            ),
            request_event(
                event_id="EVENT-WRONG-OBJECT",
                object_type=EffectObjectType.PROCESS,
            ),
            request_event(
                event_id="EVENT-WRONG-EXPECTED-SEQUENCE",
                expected_sequence=reserved.last_sequence - 1,
            ),
        )
        for event in malformed_requests:
            with self.subTest(event_id=event.event_id):
                self.assertEqual(
                    ApplyReason.UNSUPPORTED_EVENT,
                    apply_journal_event(reserved, event).reason,
                )

        def observation_event(
            event_identity: ExecutionIdentity,
            *,
            event_id: str,
        ) -> JournalEvent:
            receipt = EffectReceipt(
                1,
                event_identity,
                EffectOperation.WORKER_DISPATCH,
                EffectStatus.APPLIED,
                "2026-08-18T03:00:01Z",
                effect_hash=hash_ref(72),
                external_object_id="worker-001",
            )
            return JournalEvent.create(
                sequence=valid_request.sequence + 1,
                event_id=event_id,
                event_type=JournalEventType.DISPATCH_OBSERVED,
                identity=event_identity,
                actor_type=ActorType.ADAPTER,
                actor_id="fake-task-adapter",
                recorded_at="2026-08-18T03:00:01Z",
                previous_event_hash=valid_request.event_hash,
                payload=EffectObservationPayload(AdapterKind.TASK, receipt),
            )

        unknown_identities = (
            ExecutionIdentity(
                "WISH-001", 1, "TASK-999", 1, "CORRELATION-999"
            ),
            ExecutionIdentity(
                "WISH-001", 1, "TASK-001", 99, "CORRELATION-099"
            ),
        )
        for index, unknown_identity in enumerate(unknown_identities, start=1):
            result = apply_journal_event(
                requested.snapshot,
                observation_event(
                    unknown_identity,
                    event_id=f"EVENT-UNKNOWN-IDENTITY-{index:03d}",
                ),
            )
            self.assertEqual(ApplyReason.IDENTITY_MISMATCH, result.reason)

        stale_identity = ExecutionIdentity(
            "WISH-001", 1, "TASK-001", 1, "CORRELATION-999"
        )
        stale_observation = observation_event(
            stale_identity,
            event_id="EVENT-STALE-CORRELATION",
        )
        self.assertEqual(
            ApplyReason.STALE_CORRELATION,
            apply_journal_event(requested.snapshot, stale_observation).reason,
        )

        valid_observation = observation_event(
            identity,
            event_id="EVENT-VALID-OBSERVATION",
        )
        wrong_task_state = dataclasses.replace(
            requested.snapshot,
            tasks=(
                dataclasses.replace(
                    requested.snapshot.tasks[0], state=RuntimeState.READY
                ),
                *requested.snapshot.tasks[1:],
            ),
        )
        wrong_attempt_state = dataclasses.replace(
            requested.snapshot,
            attempts=(
                dataclasses.replace(
                    requested.snapshot.attempts[0], state=RuntimeState.RESERVED
                ),
            ),
        )
        for snapshot in (wrong_task_state, wrong_attempt_state):
            self.assertEqual(
                ApplyReason.STATE_MISMATCH,
                apply_journal_event(snapshot, valid_observation).reason,
            )

        forged = object.__new__(JournalEvent)
        for field in dataclasses.fields(valid_observation):
            object.__setattr__(forged, field.name, getattr(valid_observation, field.name))
        object.__setattr__(
            forged,
            "identity",
            ExecutionIdentity("WISH-001", 1, "TASK-001"),
        )
        self.assertEqual(
            ApplyReason.IDENTITY_MISMATCH,
            apply_journal_event(requested.snapshot, forged).reason,
        )

        with self.assertRaisesRegex(TypeError, "KernelSnapshot"):
            apply_journal_event(object(), valid_request)  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "JournalEvent"):
            apply_journal_event(reserved, object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "KernelSnapshot"):
            replay_journal_events(object(), ())  # type: ignore[arg-type]
        for invalid in ([], (object(),)):
            with self.subTest(events_type=type(invalid).__name__):
                with self.assertRaisesRegex(TypeError, "JournalEvent"):
                    replay_journal_events(reserved, invalid)  # type: ignore[arg-type]

        rejected_replay = replay_journal_events(
            reserved,
            (valid_request, stale_observation),
        )
        self.assertFalse(rejected_replay.accepted)
        self.assertEqual(ApplyReason.STALE_CORRELATION, rejected_replay.reason)
        self.assertEqual(requested.snapshot, rejected_replay.snapshot)


if __name__ == "__main__":
    unittest.main()
