from __future__ import annotations

from wish_builder.adapters.fakes import FakeBackendChannelPort

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.processes.test_coordinator import one_task_manifest
from tests.processes.test_production import (
    git,
    initialize_repository,
    one_task_graph_snapshot,
)
from wish_builder.adapters.trellis import (
    FakeTrellisGraphPort,
    FakeExternalState,
)
from wish_builder.compatibility import bundled_compatibility_bytes
from wish_builder.contracts import (
    canonical_sha256,
    decode_compatibility_bundle_bytes,
)
from wish_builder.contracts.compatibility import Provider
from wish_builder.contracts.runtime import (
    ActorType,
    AdapterKind,
    EffectOperation,
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
from wish_builder.processes import CoordinatorReason, CoordinatorStatus
from wish_builder.processes import production as production_module
from wish_builder.processes.foreground import (
    PreparedForegroundAttempt,
    WorkerBatchResult,
)
from wish_builder.processes.production import ProductionForegroundRunComponents
from wish_builder.processes.workflow import WorkflowStatus
from wish_builder.services.backend_admission import current_platform
from wish_builder.services.journal import (
    AppendStatus,
    GENESIS_HEAD,
    JournalEventDraft,
)
from wish_builder.services.ports import (
    AttemptObservation,
    CheckObservation,
    FinishObservation,
    PreparedEffect,
    TrellisLifecycleState,
    TurnState,
)
from wish_builder.services.trellis_lifecycle_effects import (
    TrellisLifecycleEffectCrash,
    TrellisLifecycleEffectService,
)


def _effect_digest(kind: str, command_hash: str) -> str:
    return "sha256:" + canonical_sha256(
        {"command_hash": command_hash, "production_lifecycle_test": kind}
    )


class _LifecycleState:
    def __init__(self) -> None:
        self.operations: dict[str, tuple[str, str, object]] = {}
        self.prepared_attempts: set[str] = set()
        self.checked_attempts: set[str] = set()
        self.calls: list[tuple[str, object]] = []


class _RecordingLifecyclePort:
    """Small exact-ID lifecycle fake shaped like the pinned Core operations."""

    def __init__(
        self,
        state: _LifecycleState,
        *,
        worktree_path: str,
        check_fault: str | None = None,
    ):
        self.state = state
        self.worktree_path = worktree_path
        self.check_fault = check_fault

    def prepare_attempt(self, effect):
        typed = self._effect(effect)
        command = typed.command
        self.state.calls.append(("prepare", command))
        existing = self._existing(typed, "prepare")
        if existing is not None:
            return existing
        observation = AttemptObservation(
            operation_id=command.operation_id,
            status=EffectStatus.APPLIED,
            observed_at="2026-08-19T10:00:00Z",
            lifecycle_state=TrellisLifecycleState.PREPARED,
            effect_digest=_effect_digest("prepare", typed.command_hash),
            attempt_id=command.operation_id,
            trellis_task_id=command.trellis_task_id,
            worktree_id=f"worktree-{command.attempt}",
            worktree_path=self.worktree_path,
            base_commit=command.expected_base_commit,
        )
        self._save(typed, "prepare", observation)
        self.state.prepared_attempts.add(command.operation_id)
        return observation

    def check_attempt(self, effect):
        typed = self._effect(effect)
        command = typed.command
        self.state.calls.append(("check", command))
        existing = self._existing(typed, "check")
        if existing is not None:
            return existing
        if self.check_fault == "collision":
            return CheckObservation(
                operation_id=command.operation_id,
                status=EffectStatus.UNKNOWN,
                observed_at="2026-08-19T10:00:01Z",
                evidence=(f"operation_id_collision:{command.operation_id}",),
            )
        attempt_id = (
            "wrong-attempt-id"
            if self.check_fault == "wrong_attempt"
            else command.attempt_id
        )
        observation = CheckObservation(
            operation_id=command.operation_id,
            status=EffectStatus.APPLIED,
            observed_at="2026-08-19T10:00:01Z",
            effect_digest=_effect_digest("check", typed.command_hash),
            attempt_id=attempt_id,
            passed=True,
            head_commit=command.expected_head_commit,
            check_digest=_effect_digest("check-result", typed.command_hash),
        )
        self._save(typed, "check", observation)
        self.state.checked_attempts.add(command.attempt_id)
        return observation

    def finish_attempt(self, effect):
        typed = self._effect(effect)
        command = typed.command
        self.state.calls.append(("finish", command))
        existing = self._existing(typed, "finish")
        if existing is not None:
            return existing
        observation = FinishObservation(
            operation_id=command.operation_id,
            status=EffectStatus.APPLIED,
            observed_at="2026-08-19T10:00:02Z",
            effect_digest=_effect_digest("finish", typed.command_hash),
            attempt_id=command.attempt_id,
            finished=True,
            delivered_commit=command.delivered_commit,
            finish_digest=_effect_digest("finish-result", typed.command_hash),
        )
        self._save(typed, "finish", observation)
        return observation

    def inspect_attempt(self, operation_id, **keywords):
        return self._inspect(operation_id, AttemptObservation, **keywords)

    def inspect_check(self, operation_id, **keywords):
        return self._inspect(operation_id, CheckObservation, **keywords)

    def inspect_finish(self, operation_id, **keywords):
        return self._inspect(operation_id, FinishObservation, **keywords)

    @staticmethod
    def _effect(effect):
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        return effect

    def _existing(self, effect, kind: str):
        existing = self.state.operations.get(effect.operation_id)
        if existing is None:
            return None
        existing_kind, command_hash, observation = existing
        if existing_kind == kind and command_hash == effect.command_hash:
            return observation
        if kind == "prepare":
            return AttemptObservation(
                operation_id=effect.operation_id,
                status=EffectStatus.UNKNOWN,
                observed_at="2026-08-19T10:00:03Z",
                lifecycle_state=TrellisLifecycleState.UNKNOWN,
                evidence=(f"operation_id_collision:{effect.operation_id}",),
            )
        observation_type = CheckObservation if kind == "check" else FinishObservation
        return observation_type(
            operation_id=effect.operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at="2026-08-19T10:00:03Z",
            evidence=(f"operation_id_collision:{effect.operation_id}",),
        )

    def _save(self, effect, kind: str, observation: object) -> None:
        self.state.operations[effect.operation_id] = (
            kind,
            effect.command_hash,
            observation,
        )

    def _inspect(
        self,
        operation_id: str,
        observation_type,
        *,
        expected_request_payload_hash: str | None = None,
    ):
        existing = self.state.operations.get(operation_id)
        if existing is not None and type(existing[2]) is observation_type:
            if (
                expected_request_payload_hash is not None
                and existing[1] != expected_request_payload_hash
            ):
                if observation_type is AttemptObservation:
                    return AttemptObservation(
                        operation_id=operation_id,
                        status=EffectStatus.UNKNOWN,
                        observed_at="2026-08-19T10:00:04Z",
                        lifecycle_state=TrellisLifecycleState.UNKNOWN,
                        evidence=("request_hash_mismatch",),
                    )
                return observation_type(
                    operation_id=operation_id,
                    status=EffectStatus.UNKNOWN,
                    observed_at="2026-08-19T10:00:04Z",
                    evidence=("request_hash_mismatch",),
                )
            return existing[2]
        if observation_type is AttemptObservation:
            return AttemptObservation(
                operation_id=operation_id,
                status=EffectStatus.ABSENT,
                observed_at="2026-08-19T10:00:04Z",
                lifecycle_state=TrellisLifecycleState.ABSENT,
            )
        return observation_type(
            operation_id=operation_id,
            status=EffectStatus.ABSENT,
            observed_at="2026-08-19T10:00:04Z",
        )


class _SeparatedTrellisFactories:
    def __init__(
        self,
        capabilities,
        *,
        send_state: TurnState = TurnState.DONE,
        check_fault: str | None = None,
    ) -> None:
        self.capabilities = capabilities
        self.send_state = send_state
        self.check_fault = check_fault
        self.channel_state = FakeExternalState()
        self.lifecycle_state = _LifecycleState()

    def __call__(self, attempt):
        return FakeBackendChannelPort(
            self.capabilities,
            state=self.channel_state,
            send_state=self.send_state,
        )

    def lifecycle_for(self, attempt):
        return _RecordingLifecyclePort(
            self.lifecycle_state,
            worktree_path=attempt.path,
            check_fault=self.check_fault,
        )


def _lifecycle_requests(events: tuple[JournalEvent, ...]) -> tuple[JournalEvent, ...]:
    operations = {
        EffectOperation.PREPARE_ATTEMPT,
        EffectOperation.CHECK_ATTEMPT,
        EffectOperation.FINISH_ATTEMPT,
    }
    return tuple(
        event
        for event in events
        if event.event_type is JournalEventType.EFFECT_REQUESTED
        and type(event.payload) is EffectRequestPayload
        and event.payload.adapter is AdapterKind.TRELLIS
        and event.payload.operation in operations
    )


class ProductionLifecycleIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.component_index = 0
        decoded = decode_compatibility_bundle_bytes(bundled_compatibility_bytes())
        self.assertTrue(decoded.ok, decoded.report.render_text())
        bundle = decoded.value
        self.assertIsNotNone(bundle)
        self.cell = bundle.platform(Provider.CODEX, current_platform())
        self.manifest = dataclasses.replace(
            one_task_manifest(),
            capability_digest=self.cell.capabilities.capability_digest,
            launch_profile_digest=self.cell.launch_profile_digest,
            policy_digest=self.cell.capabilities.policy_digest,
        )

    def components(
        self,
        *,
        send_state: TurnState = TurnState.DONE,
        check_fault: str | None = None,
    ) -> tuple[ProductionForegroundRunComponents, _SeparatedTrellisFactories]:
        self.component_index += 1
        repository = self.root / f"p{self.component_index}"
        runtime_root = self.root / f"r{self.component_index}"
        initialize_repository(repository)
        factory = _SeparatedTrellisFactories(
            production_module.channel_capabilities_from_compatibility(self.cell),
            send_state=send_state,
            check_fault=check_fault,
        )
        command = (
            str((self.root / "node.exe").absolute()),
            str((self.root / "bridge.mjs").absolute()),
        )
        with (
            mock.patch.object(
                production_module,
                "_compatibility_cell",
                return_value=self.cell,
            ),
            mock.patch.object(
                production_module,
                "_bridge_command",
                return_value=command,
            ),
            mock.patch.object(
                production_module,
                "WishBuilderBackendAttemptChannelFactory",
                return_value=factory,
            ),
            mock.patch.object(
                production_module,
                "TrellisCoreGraphPort",
                return_value=FakeTrellisGraphPort(one_task_graph_snapshot()),
            ),
        ):
            built = ProductionForegroundRunComponents.from_runtime_inputs(
                self.manifest,
                runtime_root=runtime_root,
                workspace_root=repository,
            )
        built._lifecycle_factory = factory.lifecycle_for
        self.addCleanup(built.close)
        self._seed_executing_graph(built)
        recovered = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        acquired = built.acquire_lease(recovered)
        self.assertIsNotNone(acquired)
        built._test_active_cursor = acquired
        return built, factory

    def _seed_executing_graph(self, built) -> None:
        head = GENESIS_HEAD
        phase_steps = (
            (
                JournalEventType.RUN_INITIALIZED,
                RuntimeState.NONE,
                RuntimeState.PREFLIGHT,
            ),
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
        for index, (event_type, from_state, to_state) in enumerate(
            phase_steps,
            start=1,
        ):
            result = built._journal.append_draft(
                JournalEventDraft(
                    f"EVENT-LIFECYCLE-SEED-{index:04d}",
                    event_type,
                    ExecutionIdentity(self.manifest.run_id, 1),
                    ActorType.SYSTEM,
                    "test-bootstrap",
                    TransitionPayload(
                        TransitionSubject.RUN,
                        from_state,
                        to_state,
                    ),
                ),
                expected_head=head,
            )
            self.assertIs(result.status, AppendStatus.COMMITTED)
            self.assertIsNotNone(result.head)
            head = result.head

    def reserve_prepare_dispatch(self, built):
        active = built._test_active_cursor
        reserved = built.coordinator(active).reserve_ready(limit=1)
        self.assertIs(reserved.status, CoordinatorStatus.PROGRESSED)
        identity = reserved.reserved[0]
        prepared = built.workflow(reserved.cursor).prepare_attempt(identity)
        self.assertIs(prepared.status, WorkflowStatus.PROGRESSED)
        self.assertIsNotNone(prepared.attempt)
        dispatched = built.coordinator(prepared.cursor).dispatch_reserved(identity)
        self.assertIs(dispatched.status, CoordinatorStatus.PROGRESSED)
        return identity, prepared, dispatched

    @staticmethod
    def commit_result(prepared) -> str:
        attempt_root = Path(prepared.attempt.path)
        source = attempt_root / "src" / "req-001" / "base.txt"
        source.write_text("implemented\n", encoding="utf-8")
        git(attempt_root, "add", ".")
        git(attempt_root, "commit", "-m", "implement task")
        return git(attempt_root, "rev-parse", "HEAD")

    def crash_after_lifecycle_request(
        self,
        built,
        cursor,
        identity,
        command,
        method_name: str,
    ) -> None:
        route = built._route_for_identity(cursor, identity)

        def interrupt(point: str, operation_id: str) -> None:
            if point == "after_request_append":
                raise TrellisLifecycleEffectCrash(operation_id)

        service = TrellisLifecycleEffectService(
            built._journal,
            built._router((route,)),
            built._evidence_store,
            coordinator_id=built._coordinator_id,
            fencing_token=built._fencing_token,
            failpoint=interrupt,
        )
        with self.assertRaises(TrellisLifecycleEffectCrash):
            getattr(service, method_name)(
                identity,
                command,
                expected_head=cursor.head,
            )

    def recover_pending_lifecycle(self, built):
        recovered = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        self.assertTrue(built._last_recovery.pending_external_effects)
        recovery_results = []
        original = production_module.reconcile_pending_external_effects

        def capture(*args, **kwargs):
            result = original(*args, **kwargs)
            recovery_results.append(result)
            return result

        with (
            mock.patch.object(
                production_module,
                "reconcile_pending_external_effects",
                side_effect=capture,
            ),
            mock.patch.object(built, "_retry_admitted", return_value=True),
        ):
            acquired = built.acquire_lease(recovered)
        self.assertIsNotNone(acquired, recovery_results)
        self.assertFalse(built._last_recovery.pending_external_effects)
        return acquired

    def test_graph_drift_between_reserve_and_dispatch_has_no_effect(self) -> None:
        built, _factory = self.components()
        active = built._test_active_cursor
        channel_factory = mock.Mock(
            side_effect=AssertionError("backend must not be invoked")
        )
        built._channel_factory = channel_factory

        def admit_until_reserved() -> bool:
            return not any(
                event.event_type is JournalEventType.ATTEMPT_RESERVED
                for event in built._read_verified_events()
            )

        with mock.patch.object(
            built,
            "_live_graph_admitted",
            side_effect=admit_until_reserved,
        ) as admitted:
            coordinator = built.coordinator(active)
            reservation = coordinator.reserve_ready(limit=1)
            self.assertIs(CoordinatorStatus.PROGRESSED, reservation.status)
            before_dispatch = reservation.cursor.head
            result = coordinator.dispatch_reserved(reservation.reserved[0])

        self.assertIs(CoordinatorStatus.BLOCKED, result.status)
        self.assertIs(
            CoordinatorReason.GRAPH_SNAPSHOT_NOT_ADMITTED,
            result.reason,
        )
        self.assertEqual((), result.events)
        self.assertEqual(before_dispatch, result.cursor.head)
        events = built._read_verified_events()
        self.assertFalse(
            any(
                event.event_type is JournalEventType.DISPATCH_REQUESTED
                for event in events
            )
        )
        self.assertEqual((), _lifecycle_requests(events))
        self.assertGreaterEqual(admitted.call_count, 2)
        channel_factory.assert_not_called()

    def test_prepare_is_journaled_after_git_creation_and_before_dispatch(self) -> None:
        built, factory = self.components()
        _identity, prepared, dispatched = self.reserve_prepare_dispatch(built)

        lifecycle = _lifecycle_requests(dispatched.events)
        self.assertEqual(
            (EffectOperation.PREPARE_ATTEMPT,),
            tuple(event.payload.operation for event in lifecycle),
        )
        prepare_request = lifecycle[0]
        git_observation = next(
            event
            for event in prepared.events
            if event.event_type is JournalEventType.EFFECT_OBSERVED
            and event.payload.adapter is AdapterKind.GIT
        )
        self.assertLess(git_observation.sequence, prepare_request.sequence)
        self.assertEqual(prepared.cursor.head.sequence, prepared.events[-1].sequence)
        self.assertEqual(prepared.cursor.head.event_hash, prepared.events[-1].event_hash)
        dispatch_request = next(
            event
            for event in dispatched.events
            if event.event_type is JournalEventType.DISPATCH_REQUESTED
        )
        self.assertLess(prepare_request.sequence, dispatch_request.sequence)
        self.assertEqual(dispatched.cursor.head.sequence, dispatched.events[-1].sequence)
        self.assertEqual(dispatched.cursor.head.event_hash, dispatched.events[-1].event_hash)
        self.assertEqual(
            ["prepare"],
            [kind for kind, _command in factory.lifecycle_state.calls],
        )
        prepare_command = factory.lifecycle_state.calls[0][1]
        plan = built._plan_factory(prepared.attempt.identity)
        self.assertEqual(plan.reserve.attempt_id, prepare_command.operation_id)
        evidence_objects = built._evidence_store.objects
        self.assertTrue(tuple(evidence_objects.glob("*.json")))
        if os.name == "nt":
            legacy_temporary = evidence_objects / (
                "." + "a" * 64 + ".json." + "b" * 32 + ".tmp"
            )
            self.assertGreater(len(str(legacy_temporary)), 260)

    def test_successful_worker_projects_check_and_finish_before_result_admission(
        self,
    ) -> None:
        built, factory = self.components()
        identity, prepared, dispatched = self.reserve_prepare_dispatch(built)
        result_head = self.commit_result(prepared)
        original_validate = built._repository.validate_result

        with mock.patch.object(
            built._repository,
            "validate_result",
            wraps=original_validate,
        ) as validate:
            batch = built.run_workers(
                (PreparedForegroundAttempt(identity, prepared.attempt),),
                dispatched.cursor,
            )

        self.assertIs(type(batch), WorkerBatchResult)
        self.assertTrue(batch.outcomes_known)
        self.assertEqual((identity,), tuple(item.identity for item in batch.proposals))
        validate.assert_called_once_with(
            prepared.attempt,
            process_tree_terminated=True,
        )
        self.assertEqual(
            ["prepare"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )
        self.assertFalse(_lifecycle_requests(batch.events))

        admitted = built.coordinator(batch.cursor).accept_worker_result(
            batch.proposals[0]
        )
        self.assertIs(admitted.status, CoordinatorStatus.PROGRESSED)
        calls = factory.lifecycle_state.calls
        self.assertEqual(["prepare", "check", "finish"], [kind for kind, _ in calls])
        check_command = calls[1][1]
        finish_command = calls[2][1]
        suffix = built._plan_factory(identity).reserve.attempt_id.removeprefix(
            "ATTEMPT-"
        )
        self.assertEqual(f"CHECK-{suffix}", check_command.operation_id)
        self.assertEqual(f"FINISH-{suffix}", finish_command.operation_id)
        self.assertEqual(result_head, check_command.expected_head_commit)
        self.assertEqual(result_head, finish_command.delivered_commit)
        lifecycle = _lifecycle_requests(admitted.events)
        self.assertEqual(
            (EffectOperation.CHECK_ATTEMPT, EffectOperation.FINISH_ATTEMPT),
            tuple(event.payload.operation for event in lifecycle),
        )
        self.assertIs(
            next(
                attempt.state
                for attempt in admitted.cursor.snapshot.attempts
                if attempt.task_id == identity.task_id
            ),
            RuntimeState.SUCCEEDED,
        )
        succeeded = next(
            event
            for event in admitted.events
            if event.event_type is JournalEventType.ATTEMPT_SUCCEEDED
        )
        self.assertGreater(succeeded.sequence, lifecycle[-1].sequence)
        self.assertEqual(admitted.events[-1].sequence, admitted.cursor.head.sequence)
        self.assertEqual(admitted.events[-1].event_hash, admitted.cursor.head.event_hash)

    def test_failed_worker_never_projects_check_or_finish(self) -> None:
        built, factory = self.components(send_state=TurnState.FAILED)
        identity, prepared, dispatched = self.reserve_prepare_dispatch(built)

        with mock.patch.object(
            built._repository,
            "validate_result",
            side_effect=AssertionError("failed workers must not enter Git validation"),
        ):
            batch = built.run_workers(
                (PreparedForegroundAttempt(identity, prepared.attempt),),
                dispatched.cursor,
            )

        self.assertTrue(batch.outcomes_known)
        self.assertEqual(1, len(batch.proposals))
        self.assertFalse(batch.proposals[0].succeeded)
        self.assertIs(batch.proposals[0].reason_code, RuntimeReasonCode.CHECK_FAILED)
        accepted = built.coordinator(batch.cursor).accept_worker_result(
            batch.proposals[0]
        )
        self.assertIs(accepted.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual(
            ["prepare"],
            [kind for kind, _command in factory.lifecycle_state.calls],
        )
        self.assertFalse(_lifecycle_requests(accepted.events))
        self.assertIs(accepted.events[0].event_type, JournalEventType.ATTEMPT_FAILED)

    def test_head_drift_after_worker_validation_stops_before_trellis_check(
        self,
    ) -> None:
        built, factory = self.components()
        identity, prepared, dispatched = self.reserve_prepare_dispatch(built)
        self.commit_result(prepared)
        batch = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            dispatched.cursor,
        )
        self.assertTrue(batch.outcomes_known)
        self.assertTrue(batch.proposals[0].succeeded)

        attempt_root = Path(prepared.attempt.path)
        source = attempt_root / "src" / "req-001" / "base.txt"
        source.write_text("drifted after validation\n", encoding="utf-8")
        git(attempt_root, "add", ".")
        git(attempt_root, "commit", "-m", "drift after worker validation")
        accepted = built.coordinator(batch.cursor).accept_worker_result(
            batch.proposals[0]
        )

        self.assertIs(accepted.status, CoordinatorStatus.BLOCKED)
        self.assertFalse(accepted.events)
        self.assertEqual(
            ["prepare"],
            [kind for kind, _command in factory.lifecycle_state.calls],
        )
        self.assertEqual(batch.cursor, accepted.cursor)

    def test_check_identity_and_collision_faults_fail_closed(self) -> None:
        for fault in ("wrong_attempt", "collision"):
            with self.subTest(fault=fault):
                built, factory = self.components(check_fault=fault)
                identity, prepared, dispatched = self.reserve_prepare_dispatch(built)
                self.commit_result(prepared)

                batch = built.run_workers(
                    (PreparedForegroundAttempt(identity, prepared.attempt),),
                    dispatched.cursor,
                )
                self.assertTrue(batch.outcomes_known)
                self.assertEqual(1, len(batch.proposals))
                accepted = built.coordinator(batch.cursor).accept_worker_result(
                    batch.proposals[0]
                )

                self.assertIs(accepted.status, CoordinatorStatus.BLOCKED)
                self.assertEqual(
                    ["prepare", "check"],
                    [kind for kind, _command in factory.lifecycle_state.calls],
                )
                self.assertNotIn(
                    EffectOperation.FINISH_ATTEMPT,
                    tuple(
                        event.payload.operation
                        for event in _lifecycle_requests(accepted.events)
                    ),
                )
                self.assertNotIn(
                    JournalEventType.ATTEMPT_SUCCEEDED,
                    tuple(event.event_type for event in accepted.events),
                )
                self.assertEqual(
                    accepted.events[-1].sequence,
                    accepted.cursor.head.sequence,
                )
                self.assertEqual(
                    accepted.events[-1].event_hash,
                    accepted.cursor.head.event_hash,
                )

    def test_absent_prepare_is_reconstructed_and_retried_once(self) -> None:
        built, factory = self.components()
        active = built._test_active_cursor
        reserved = built.coordinator(active).reserve_ready(limit=1)
        identity = reserved.reserved[0]
        prepared = built.workflow(reserved.cursor).prepare_attempt(identity)
        route = built._route_for_identity(prepared.cursor, identity)
        command = built._prepare_lifecycle_command(identity, route)

        self.crash_after_lifecycle_request(
            built,
            prepared.cursor,
            identity,
            command,
            "prepare",
        )
        self.assertFalse(factory.lifecycle_state.calls)
        recovered = self.recover_pending_lifecycle(built)
        self.assertEqual(
            ["prepare"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )

        dispatched = built.coordinator(recovered).dispatch_reserved(identity)
        self.assertIs(dispatched.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual(
            ["prepare"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )

    def test_corrupt_attempt_route_fails_closed_during_recovery(self) -> None:
        built, factory = self.components()
        active = built._test_active_cursor
        reserved = built.coordinator(active).reserve_ready(limit=1)
        identity = reserved.reserved[0]
        prepared = built.workflow(reserved.cursor).prepare_attempt(identity)
        route = built._route_for_identity(prepared.cursor, identity)
        command = built._prepare_lifecycle_command(identity, route)
        self.crash_after_lifecycle_request(
            built,
            prepared.cursor,
            identity,
            command,
            "prepare",
        )
        attempt_path = Path(prepared.attempt.path)
        attempt_path.rename(attempt_path.with_name(attempt_path.name + "-moved"))

        recovered = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        acquired = built.acquire_lease(recovered)

        self.assertIsNone(acquired)
        self.assertFalse(factory.lifecycle_state.calls)

    def test_absent_check_is_reconstructed_before_finish(self) -> None:
        built, factory = self.components()
        identity, prepared, dispatched = self.reserve_prepare_dispatch(built)
        self.commit_result(prepared)
        batch = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            dispatched.cursor,
        )
        route = built._route_for_identity(batch.cursor, identity)
        validation = built._validated_results[identity]
        command = built._check_lifecycle_command(identity, route, validation)

        self.crash_after_lifecycle_request(
            built,
            batch.cursor,
            identity,
            command,
            "check",
        )
        recovered = self.recover_pending_lifecycle(built)
        self.assertEqual(
            ["prepare", "check"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )

        replayed = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            recovered,
        )
        accepted = built.coordinator(replayed.cursor).accept_worker_result(
            replayed.proposals[0]
        )
        self.assertIs(accepted.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual(
            ["prepare", "check", "finish"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )

    def test_check_recovery_rejects_a_send_identity_mismatch(self) -> None:
        built, factory = self.components()
        identity, prepared, dispatched = self.reserve_prepare_dispatch(built)
        self.commit_result(prepared)
        batch = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            dispatched.cursor,
        )
        route = built._route_for_identity(batch.cursor, identity)
        validation = built._validated_results[identity]
        check = built._check_lifecycle_command(identity, route, validation)
        self.crash_after_lifecycle_request(
            built,
            batch.cursor,
            identity,
            check,
            "check",
        )

        send_id = route.plan.send.operation_id
        record = factory.channel_state.channel_records[send_id]
        bad_turn = dataclasses.replace(record.response, message_id="MESSAGE-WRONG")
        factory.channel_state.channel_records[send_id] = dataclasses.replace(
            record,
            response=bad_turn,
        )

        recovered = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        acquired = built.acquire_lease(recovered)

        self.assertIsNone(acquired)
        self.assertEqual(
            ["prepare"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )

    def test_absent_finish_rebuilds_the_exact_delivery_digest(self) -> None:
        built, factory = self.components()
        identity, prepared, dispatched = self.reserve_prepare_dispatch(built)
        self.commit_result(prepared)
        batch = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            dispatched.cursor,
        )
        proposal = batch.proposals[0]
        route = built._route_for_identity(batch.cursor, identity)
        validation = built._validated_results[identity]
        check = built._check_lifecycle_command(identity, route, validation)
        service = built._lifecycle_service(route)
        checked = service.check(identity, check, expected_head=batch.cursor.head)
        self.assertIsNotNone(checked.observation)
        self.assertTrue(checked.observation.passed)
        finish = built._finish_lifecycle_command(
            identity,
            route,
            validation,
            proposal,
        )

        coordinator = built.coordinator(batch.cursor)
        self.assertIs(
            coordinator._adopt_committed_events(checked.events),
            production_module.CoordinatorReason.NONE,
        )
        checked_cursor = coordinator.cursor
        self.crash_after_lifecycle_request(
            built,
            checked_cursor,
            identity,
            finish,
            "finish",
        )
        recovered = self.recover_pending_lifecycle(built)
        self.assertEqual(
            ["prepare", "check", "finish"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )

        replayed = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            recovered,
        )
        accepted = built.coordinator(replayed.cursor).accept_worker_result(
            replayed.proposals[0]
        )
        self.assertIs(accepted.status, CoordinatorStatus.PROGRESSED)
        self.assertEqual(
            ["prepare", "check", "finish"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )

    def test_finish_recovery_rejects_missing_worker_evidence(self) -> None:
        built, factory = self.components()
        identity, prepared, dispatched = self.reserve_prepare_dispatch(built)
        self.commit_result(prepared)
        batch = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            dispatched.cursor,
        )
        proposal = batch.proposals[0]
        route = built._route_for_identity(batch.cursor, identity)
        validation = built._validated_results[identity]
        check = built._check_lifecycle_command(identity, route, validation)
        checked = built._lifecycle_service(route).check(
            identity,
            check,
            expected_head=batch.cursor.head,
        )
        coordinator = built.coordinator(batch.cursor)
        self.assertIs(
            coordinator._adopt_committed_events(checked.events),
            production_module.CoordinatorReason.NONE,
        )
        finish = built._finish_lifecycle_command(
            identity,
            route,
            validation,
            proposal,
        )
        self.crash_after_lifecycle_request(
            built,
            coordinator.cursor,
            identity,
            finish,
            "finish",
        )
        worker_evidence = proposal.evidence[0]
        worker_object = built._evidence_store.objects / (
            worker_evidence.digest.removeprefix("sha256:") + ".json"
        )
        worker_object.unlink()

        recovered = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        acquired = built.acquire_lease(recovered)

        self.assertIsNone(acquired)
        self.assertEqual(
            ["prepare", "check"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )

    def test_finish_recovery_rejects_a_check_commit_mismatch(self) -> None:
        built, factory = self.components()
        identity, prepared, dispatched = self.reserve_prepare_dispatch(built)
        result_commit = self.commit_result(prepared)
        batch = built.run_workers(
            (PreparedForegroundAttempt(identity, prepared.attempt),),
            dispatched.cursor,
        )
        proposal = batch.proposals[0]
        route = built._route_for_identity(batch.cursor, identity)
        validation = built._validated_results[identity]
        check = built._check_lifecycle_command(identity, route, validation)
        checked = built._lifecycle_service(route).check(
            identity,
            check,
            expected_head=batch.cursor.head,
        )
        coordinator = built.coordinator(batch.cursor)
        self.assertIs(
            coordinator._adopt_committed_events(checked.events),
            production_module.CoordinatorReason.NONE,
        )
        finish = built._finish_lifecycle_command(
            identity,
            route,
            validation,
            proposal,
        )
        self.crash_after_lifecycle_request(
            built,
            coordinator.cursor,
            identity,
            finish,
            "finish",
        )
        check_record = factory.lifecycle_state.operations[check.operation_id]
        bad_check = dataclasses.replace(
            check_record[2],
            head_commit="f" * 40,
        )
        factory.lifecycle_state.operations[check.operation_id] = (
            check_record[0],
            check_record[1],
            bad_check,
        )

        recovered = built.recover_verified_cursor(self.manifest)
        self.assertIsNotNone(recovered)
        acquired = built.acquire_lease(recovered)

        self.assertIsNone(acquired)
        self.assertEqual(result_commit, validation.manifest.result_commit_sha)
        self.assertEqual(
            ["prepare", "check"],
            [kind for kind, _ in factory.lifecycle_state.calls],
        )


if __name__ == "__main__":
    unittest.main()
