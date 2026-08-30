from __future__ import annotations

import dataclasses
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.processes.test_coordinator import CoordinatorHarness, sibling_manifest
from tests.services.test_execution_admission import WORKSPACE_HASH, admitted_events
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts.compatibility import Platform, Provider
from wish_builder.contracts.manifest_v2 import WorkerProvider
from wish_builder.contracts.runtime import (
    ActorType,
    ExecutionIdentity,
    JournalEvent,
    JournalEventType,
    LeaseDraftPayload,
    RuntimeReasonCode,
    RuntimeState,
    TransitionPayload,
    TransitionSubject,
)
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.kernel.state import (
    ApplyReason,
    AttemptProjection,
    apply_journal_event,
)
from wish_builder.processes.coordinator import (
    CoordinatorReason,
    CoordinatorReservationResult,
    CoordinatorStatus,
    CoordinatorStepResult,
    WorkerResultProposal,
)
from wish_builder.processes.foreground import (
    ForegroundRunReason,
    ForegroundRunResult,
    ForegroundRunService,
    ForegroundRunStage,
    ForegroundRunStatus,
    ForegroundTerminalResult,
    PreparedForegroundAttempt,
    WorkerBatchResult,
    WorkerLeaseRenewalResult,
)
from wish_builder.processes.workflow import (
    AttemptPreparationResult,
    CleanupStepResult,
    PromotionBatchResult,
    ResultStageResult,
    WorkflowReason,
    WorkflowStatus,
)
from wish_builder.services.backend_admission import (
    BackendAdmissionReason,
    BackendAdmissionResult,
    admit_backend,
)
from wish_builder.services.execution_admission import admit_execution_snapshot
from wish_builder.services.execution_admission import (
    ExecutionAdmissionReason,
    ExecutionAdmissionResult,
)
from wish_builder.services.execution_checkpoints import (
    ExecutionCheckpointReason,
    ExecutionCheckpointResult,
    ExecutionCheckpointStatus,
)


class _Acceptance:
    def verify(self, task, repository, promotion):  # pragma: no cover - fake workflow
        raise AssertionError("the fake workflow owns this deterministic test boundary")


def _renew_cursor(cursor, *, seconds: int = 100):
    lease = cursor.lease_state.lease
    assert lease is not None
    committed_at = datetime.fromisoformat(
        lease.committed_at.replace("Z", "+00:00")
    ) + timedelta(seconds=seconds)
    payload = LeaseDraftPayload(
        lease.lease_id,
        lease.coordinator_id,
        lease.owner,
        lease.scheduler_mode,
        lease.fencing_token,
        lease.manifest_digest,
        lease.lease_ttl_seconds,
        lease.lease_clock_skew_seconds,
    ).materialize(committed_at, terminal=False)
    event = JournalEvent.create(
        sequence=cursor.head.sequence + 1,
        event_id=f"EVENT-WORKER-LEASE-RENEWED-{cursor.head.sequence + 1:08d}",
        event_type=JournalEventType.LEASE_RENEWED,
        identity=ExecutionIdentity(cursor.snapshot.run_id, lease.fencing_token),
        actor_type=ActorType.COORDINATOR,
        actor_id=lease.coordinator_id,
        recorded_at=payload.committed_at,
        previous_event_hash=cursor.head.event_hash,
        payload=payload,
    )
    applied = apply_journal_event(cursor.snapshot, event)
    assert applied.accepted
    renewed = type(cursor)(
        applied.snapshot,
        cursor.graph_index.advance(cursor.snapshot, applied.snapshot),
        cursor.lease_state.advance(event),
        cursor.dispatch_recoveries,
    )
    return WorkerLeaseRenewalResult(True, renewed, event)


class _Coordinator:
    def __init__(self, components: "_Components") -> None:
        self.components = components

    def reserve_ready(self, *, limit=None):
        self.components.trace.append("reserve")
        if limit is not None:
            raise AssertionError(
                "the foreground service must leave limits to coordinator"
            )
        self.components.set_task_state(RuntimeState.LEASED)
        self.components.set_attempt_state(RuntimeState.RESERVED)
        return CoordinatorReservationResult(
            CoordinatorStatus.PROGRESSED,
            CoordinatorReason.NONE,
            self.components.cursor,
            reserved=(self.components.identity,),
        )

    def dispatch_reserved(self, identity):
        self.components.trace.append("dispatch")
        if identity != self.components.identity:
            raise AssertionError("unexpected dispatch identity")
        self.components.set_task_state(RuntimeState.DISPATCHED)
        self.components.set_attempt_state(RuntimeState.RUNNING)
        return CoordinatorStepResult(
            CoordinatorStatus.PROGRESSED,
            CoordinatorReason.NONE,
            self.components.cursor,
            dispatched=(self.components.identity,),
        )

    def accept_worker_result(self, proposal):
        self.components.trace.append("worker_result")
        self.components.set_attempt_state(
            RuntimeState.SUCCEEDED if proposal.succeeded else RuntimeState.FAILED,
            proposal.reason_code,
        )
        return CoordinatorStepResult(
            CoordinatorStatus.PROGRESSED,
            CoordinatorReason.NONE,
            self.components.cursor,
        )


class _Workflow:
    def __init__(self, components: "_Components") -> None:
        self.components = components

    def prepare_attempt(self, identity):
        self.components.trace.append("prepare")
        return AttemptPreparationResult(
            WorkflowStatus.PROGRESSED,
            WorkflowReason.NONE,
            self.components.cursor,
            (),
            self.components.attempt,
        )

    def stage_attempt_result(self, attempt):
        self.components.trace.append("stage")
        self.components.set_task_state(RuntimeState.STAGED)
        return ResultStageResult(
            WorkflowStatus.PROGRESSED,
            WorkflowReason.NONE,
            self.components.cursor,
            (),
            staged=SimpleNamespace(task_id=self.components.identity.task_id),
        )

    def promote_staged(self, sources, acceptance):
        self.components.trace.append("accept_promote")
        if self.components.acceptance_fails:
            return PromotionBatchResult(
                WorkflowStatus.BLOCKED,
                WorkflowReason.ACCEPTANCE_FAILED,
                self.components.cursor,
                (),
            )
        self.components.set_task_state(RuntimeState.VERIFIED)
        return PromotionBatchResult(
            WorkflowStatus.PROGRESSED,
            WorkflowReason.NONE,
            self.components.cursor,
            (),
            (SimpleNamespace(task_id=self.components.identity.task_id),),
        )


class _Components:
    """Traceable adapter around the real admission contract and typed results."""

    acceptance = _Acceptance()

    def __init__(self, harness: CoordinatorHarness) -> None:
        self.harness_manifest = harness.manifest
        self.cursor = harness.coordinator.cursor
        self.identity = ExecutionIdentity(
            harness.manifest.run_id,
            1,
            harness.manifest.tasks[0].id,
            1,
            "CORRELATION-FOREGROUND-001",
        )
        self.attempt = SimpleNamespace(identity=self.identity)
        self.trace: list[str] = []
        self.execution_admission_calls = 0
        self.worker_unknown = False
        self.worker_fails = False
        self.worker_renews = False
        self.worker_unknown_after_renewal = False
        self.worker_omits_renewal_head = False
        self.worker_input_cursor = None
        self.checkpoint_events = ()
        self.coordinator_cursors = []
        self.acceptance_fails = False
        self.cleanup_blocks = False
        self.checkpoint_blocks = False
        self.terminal_blocks = False
        self.validation_result = True
        self.control_root_result = True
        self.workspace_result = True
        self.recovery_result = True
        self.lease_result = True

    def admit_backend(self, manifest):
        self.trace.append("backend_admission")
        return _admitted_backend(manifest)

    def set_task_state(self, state):
        snapshot = dataclasses.replace(
            self.cursor.snapshot,
            tasks=tuple(
                dataclasses.replace(task, state=state, reason_code=None)
                if task.task_id == self.identity.task_id
                else task
                for task in self.cursor.snapshot.tasks
            ),
        )
        self.cursor = type(self.cursor)(
            snapshot,
            GraphIndex.compile(self.harness_manifest, snapshot),
            self.cursor.lease_state,
            self.cursor.dispatch_recoveries,
        )

    def set_attempt_state(self, state, reason_code=None):
        attempts = self.cursor.snapshot.attempts
        if not attempts:
            attempts = (
                AttemptProjection(
                    self.identity.task_id,
                    self.identity.attempt,
                    self.identity.correlation_id,
                    self.identity.coordinator_epoch,
                    state,
                    reason_code,
                ),
            )
        else:
            attempts = tuple(
                dataclasses.replace(attempt, state=state, reason_code=reason_code)
                if attempt.task_id == self.identity.task_id
                else attempt
                for attempt in attempts
            )
        snapshot = dataclasses.replace(self.cursor.snapshot, attempts=attempts)
        self.cursor = type(self.cursor)(
            snapshot,
            GraphIndex.compile(self.harness_manifest, snapshot),
            self.cursor.lease_state,
            self.cursor.dispatch_recoveries,
        )

    def advance(self, event):
        previous = self.cursor.snapshot
        applied = apply_journal_event(previous, event)
        if applied.accepted:
            current = applied.snapshot
        elif applied.reason is ApplyReason.UNSUPPORTED_EVENT:
            current = dataclasses.replace(
                previous,
                last_sequence=event.sequence,
                last_event_id=event.event_id,
                last_event_hash=event.event_hash,
            )
        else:
            raise AssertionError(applied.reason)
        self.cursor = type(self.cursor)(
            current,
            self.cursor.graph_index.advance(previous, current),
            self.cursor.lease_state.advance(event),
            self.cursor.dispatch_recoveries,
        )

    def validate_execution(self, manifest):
        self.trace.append("execution_admission")
        self.execution_admission_calls += 1
        admitted_manifest, events = admitted_events()
        if admitted_manifest != manifest:
            return False
        result = admit_execution_snapshot(
            manifest,
            tuple(events),
            workspace_hash=WORKSPACE_HASH,
        )
        return (
            result
            if self.validation_result
            else ExecutionAdmissionResult(
                False,
                ExecutionAdmissionReason.MANIFEST_DIGEST_MISMATCH,
            )
        )

    def protect_control_root(self):
        self.trace.append("control_root")
        return self.control_root_result

    def verify_workspace_identity(self, manifest):
        self.trace.append("workspace_identity")
        return self.workspace_result

    def recover_verified_cursor(self, manifest):
        self.trace.append("recovery")
        return self.cursor if self.recovery_result else None

    def acquire_lease(self, cursor):
        self.trace.append("lease")
        return self.cursor if self.lease_result else None

    def coordinator(self, cursor):
        self.trace.append("coordinator")
        self.coordinator_cursors.append(cursor)
        return _Coordinator(self)

    def workflow(self, cursor):
        self.trace.append("workflow")
        return _Workflow(self)

    def run_workers(self, attempts, cursor):
        self.trace.append("workers")
        self.worker_input_cursor = cursor
        if self.worker_unknown:
            return WorkerBatchResult(False)
        proposal = WorkerResultProposal(
            self.identity,
            "worker-foreground-001",
            not self.worker_fails,
            RuntimeReasonCode.CHECK_FAILED if self.worker_fails else None,
        )
        if not self.worker_renews:
            return WorkerBatchResult(True, (proposal,))

        first = _renew_cursor(cursor)
        assert first.cursor is not None and first.event is not None
        self.cursor = first.cursor
        events = (first.event,)
        result_cursor = first.cursor
        if self.worker_omits_renewal_head:
            second = _renew_cursor(first.cursor)
            assert second.cursor is not None and second.event is not None
            self.cursor = second.cursor
            events = (second.event,)
            result_cursor = second.cursor
        if self.worker_unknown_after_renewal:
            return WorkerBatchResult(False, (), result_cursor, events)
        return WorkerBatchResult(True, (proposal,), result_cursor, events)

    def cleanup_attempt(self, workflow, attempt, promotion):
        self.trace.append("cleanup")
        return CleanupStepResult(
            WorkflowStatus.BLOCKED
            if self.cleanup_blocks
            else WorkflowStatus.PROGRESSED,
            (
                WorkflowReason.CLEANUP_BLOCKED
                if self.cleanup_blocks
                else WorkflowReason.NONE
            ),
            self.cursor,
            (),
        )

    def publish_checkpoint(self, cursor, events):
        self.trace.append("checkpoint")
        self.checkpoint_events = events
        if self.checkpoint_blocks:
            return ExecutionCheckpointResult(
                ExecutionCheckpointStatus.BLOCKED,
                ExecutionCheckpointReason.PUBLISH_FAILED,
            )
        return ExecutionCheckpointResult(
            ExecutionCheckpointStatus.SKIPPED,
            ExecutionCheckpointReason.NOT_DUE,
        )

    def finish(self, cursor):
        self.trace.append("terminal")
        if self.terminal_blocks:
            return ForegroundTerminalResult(False, self.cursor)
        events = []
        phase = self.cursor.snapshot.phase
        for event_type, from_state, to_state in (
            (
                JournalEventType.EXECUTION_COMPLETED,
                RuntimeState.EXECUTING,
                RuntimeState.INTEGRATION,
            ),
            (
                JournalEventType.INTEGRATION_VERIFIED,
                RuntimeState.INTEGRATION,
                RuntimeState.QUALITY_DOCS,
            ),
            (
                JournalEventType.QUALITY_DOCS_VERIFIED,
                RuntimeState.QUALITY_DOCS,
                RuntimeState.COMPLETE,
            ),
        ):
            if phase is not from_state:
                raise AssertionError((phase, from_state))
            event = JournalEvent.create(
                sequence=self.cursor.head.sequence + 1,
                event_id=f"EVENT-FOREGROUND-{event_type.value.upper().replace('_', '-')}",
                event_type=event_type,
                identity=ExecutionIdentity(self.cursor.snapshot.run_id, 1),
                actor_type=ActorType.COORDINATOR,
                actor_id="coordinator-001",
                recorded_at="2026-08-19T00:01:00Z",
                previous_event_hash=self.cursor.head.event_hash,
                payload=TransitionPayload(
                    TransitionSubject.RUN,
                    from_state,
                    to_state,
                ),
            )
            self.advance(event)
            events.append(event)
            phase = to_state
        lease = self.cursor.lease_state.lease
        assert lease is not None
        payload = LeaseDraftPayload(
            lease.lease_id,
            lease.coordinator_id,
            lease.owner,
            lease.scheduler_mode,
            lease.fencing_token,
            lease.manifest_digest,
            lease.lease_ttl_seconds,
            lease.lease_clock_skew_seconds,
        ).materialize(datetime(2026, 8, 19, 0, 1, tzinfo=UTC), terminal=True)
        released = JournalEvent.create(
            sequence=self.cursor.head.sequence + 1,
            event_id="EVENT-FOREGROUND-LEASE-RELEASED",
            event_type=JournalEventType.LEASE_RELEASED,
            identity=ExecutionIdentity(self.cursor.snapshot.run_id, 1),
            actor_type=ActorType.COORDINATOR,
            actor_id="coordinator-001",
            recorded_at=payload.committed_at,
            previous_event_hash=self.cursor.head.event_hash,
            payload=payload,
        )
        self.advance(released)
        events.append(released)
        return ForegroundTerminalResult(True, self.cursor, tuple(events))


def _admitted_backend(manifest):
    del manifest
    bundle = load_bundled_compatibility()
    cell = bundle.platform(Provider.CODEX, Platform.WINDOWS)
    # A test-only admitted result isolates composition from the qualification gate.
    return BackendAdmissionResult(True, BackendAdmissionReason.NONE, cell)


class ForegroundRunServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.harness = CoordinatorHarness(Path(self.temporary.name))
        self.components = _Components(self.harness)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def service(self):
        return ForegroundRunService(
            self.harness.manifest,
            components=self.components,
            backend_admitter=self.components.admit_backend,
        )

    def test_disabled_backend_is_typed_and_reaches_no_component_or_fake_port(self):
        bundle = load_bundled_compatibility()
        cell = bundle.platform(Provider.PI, Platform.WINDOWS)
        manifest = dataclasses.replace(
            self.harness.manifest,
            provider=WorkerProvider.PI,
            capability_digest=cell.capabilities.capability_digest,
            launch_profile_digest=cell.launch_profile_digest,
            policy_digest=bundle.policy_digest,
        )

        with mock.patch("wish_builder.adapters.fake.FakeTaskPort") as fake_port:
            result = ForegroundRunService(
                manifest,
                components=self.components,
                backend_admitter=lambda candidate: admit_backend(
                    candidate,
                    bundle=bundle,
                    platform=Platform.WINDOWS,
                ),
            ).run()

        self.assertIs(result.status, ForegroundRunStatus.REJECTED)
        self.assertIs(result.reason, ForegroundRunReason.DISPATCH_NOT_QUALIFIED)
        self.assertIs(result.stage, ForegroundRunStage.BACKEND_ADMISSION)
        self.assertIs(
            result.backend_admission.reason,
            BackendAdmissionReason.DISPATCH_NOT_QUALIFIED,
        )
        self.assertEqual([], self.components.trace)
        fake_port.assert_not_called()

    def test_disabled_backend_never_reads_an_effectful_component_property(self):
        class ExplodingComponents:
            @property
            def acceptance(self):
                raise AssertionError("component access preceded backend admission")

            def validate_execution(self, manifest):
                raise AssertionError("component access preceded backend admission")

            def protect_control_root(self):
                raise AssertionError("component access preceded backend admission")

            def verify_workspace_identity(self, manifest):
                raise AssertionError("component access preceded backend admission")

            def recover_verified_cursor(self, manifest):
                raise AssertionError("component access preceded backend admission")

            def acquire_lease(self, cursor):
                raise AssertionError("component access preceded backend admission")

            def coordinator(self, cursor):
                raise AssertionError("component access preceded backend admission")

            def workflow(self, cursor):
                raise AssertionError("component access preceded backend admission")

            def run_workers(self, attempts, cursor):
                raise AssertionError("component access preceded backend admission")

            def cleanup_attempt(self, workflow, attempt, promotion):
                raise AssertionError("component access preceded backend admission")

            def publish_checkpoint(self, cursor, events):
                raise AssertionError("component access preceded backend admission")

            def finish(self, cursor):
                raise AssertionError("component access preceded backend admission")

        result = ForegroundRunService(
            self.harness.manifest,
            components=ExplodingComponents(),
            backend_admitter=lambda manifest: BackendAdmissionResult(
                False,
                BackendAdmissionReason.DISPATCH_NOT_QUALIFIED,
            ),
        ).run()

        self.assertIs(result.reason, ForegroundRunReason.DISPATCH_NOT_QUALIFIED)

    def test_disabled_backend_never_builds_or_closes_lazy_components(self):
        components = _Components(self.harness)
        components.close = mock.Mock()
        factory = mock.Mock(return_value=components)

        result = ForegroundRunService(
            self.harness.manifest,
            components_factory=factory,
            backend_admitter=lambda manifest: BackendAdmissionResult(
                False,
                BackendAdmissionReason.DISPATCH_NOT_QUALIFIED,
            ),
        ).run()

        self.assertIs(result.reason, ForegroundRunReason.DISPATCH_NOT_QUALIFIED)
        factory.assert_not_called()
        components.close.assert_not_called()

    def test_forged_trellis_scheduler_is_rejected_before_composition(self):
        forged = object.__new__(type(self.harness.manifest))
        for field in dataclasses.fields(self.harness.manifest):
            object.__setattr__(
                forged,
                field.name,
                getattr(self.harness.manifest, field.name),
            )
        object.__setattr__(forged, "scheduler_mode", "trellis")
        factory = mock.Mock(
            side_effect=AssertionError(
                "a Trellis-scheduled manifest must not create a second dispatcher"
            )
        )

        result = ForegroundRunService(
            forged,
            components_factory=factory,
            backend_admitter=lambda manifest: admit_backend(
                manifest,
                platform=Platform.WINDOWS,
            ),
        ).run()

        self.assertIs(result.status, ForegroundRunStatus.REJECTED)
        self.assertIs(result.reason, ForegroundRunReason.BACKEND_NOT_ADMITTED)
        self.assertIs(
            result.backend_admission.reason,
            BackendAdmissionReason.SCHEDULER_MISMATCH,
        )
        factory.assert_not_called()

    def test_admitted_backend_builds_and_closes_lazy_components_once(self):
        self.components.close = mock.Mock()
        factory = mock.Mock(return_value=self.components)

        result = ForegroundRunService(
            self.harness.manifest,
            components_factory=factory,
            backend_admitter=self.components.admit_backend,
        ).run()

        self.assertIs(result.status, ForegroundRunStatus.COMPLETED)
        factory.assert_called_once_with()
        self.components.close.assert_called_once_with()

    def test_lazy_components_close_once_after_early_blocked_return(self):
        components = _Components(self.harness)
        components.validation_result = False
        components.close = mock.Mock()
        factory = mock.Mock(return_value=components)

        result = ForegroundRunService(
            self.harness.manifest,
            components_factory=factory,
            backend_admitter=components.admit_backend,
        ).run()

        self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
        self.assertIs(result.reason, ForegroundRunReason.MANIFEST_NOT_ADMITTED)
        factory.assert_called_once_with()
        components.close.assert_called_once_with()

    def test_directly_injected_components_remain_caller_owned(self):
        self.components.close = mock.Mock()

        result = self.service().run()

        self.assertIs(result.status, ForegroundRunStatus.COMPLETED)
        self.components.close.assert_not_called()

    def test_close_exception_does_not_mask_the_original_result(self):
        components = _Components(self.harness)
        components.control_root_result = False
        components.close = mock.Mock(side_effect=RuntimeError("close failed"))

        result = ForegroundRunService(
            self.harness.manifest,
            components_factory=lambda: components,
            backend_admitter=components.admit_backend,
        ).run()

        self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
        self.assertIs(
            result.reason,
            ForegroundRunReason.CONTROL_ROOT_NOT_PROTECTED,
        )
        self.assertIs(result.stage, ForegroundRunStage.CONTROL_ROOT)
        components.close.assert_called_once_with()

    def test_lazy_component_failure_is_typed_and_fail_closed(self):
        factory = mock.Mock(side_effect=OSError("composition unavailable"))

        result = ForegroundRunService(
            self.harness.manifest,
            components_factory=factory,
            backend_admitter=self.components.admit_backend,
        ).run()

        self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
        self.assertIs(result.reason, ForegroundRunReason.COMPOSITION_UNAVAILABLE)
        self.assertIs(result.stage, ForegroundRunStage.MANIFEST_VALIDATION)
        factory.assert_called_once_with()

    def test_lazy_component_contract_check_does_not_read_properties(self):
        class DeferredAcceptanceComponents(_Components):
            @property
            def acceptance(self):
                raise AssertionError("acceptance was read before manifest validation")

        components = DeferredAcceptanceComponents(self.harness)
        components.validation_result = False

        result = ForegroundRunService(
            self.harness.manifest,
            components_factory=lambda: components,
            backend_admitter=components.admit_backend,
        ).run()

        self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
        self.assertIs(result.reason, ForegroundRunReason.MANIFEST_NOT_ADMITTED)
        self.assertEqual(["backend_admission", "execution_admission"], components.trace)

    def test_admitted_lifecycle_uses_formal_execution_admission_and_order(self):
        result = self.service().run()

        self.assertIs(result.status, ForegroundRunStatus.COMPLETED)
        self.assertIs(result.reason, ForegroundRunReason.NONE)
        self.assertEqual(
            (self.harness.manifest.tasks[0].id,), result.completed_task_ids
        )
        self.assertEqual(1, result.batch_count)
        self.assertEqual(1, self.components.execution_admission_calls)
        self.assertEqual(
            [
                "backend_admission",
                "execution_admission",
                "control_root",
                "workspace_identity",
                "recovery",
                "lease",
                "coordinator",
                "reserve",
                "workflow",
                "prepare",
                "coordinator",
                "dispatch",
                "workers",
                "coordinator",
                "worker_result",
                "workflow",
                "stage",
                "workflow",
                "accept_promote",
                "workflow",
                "cleanup",
                "checkpoint",
                "terminal",
            ],
            self.components.trace,
        )

    def test_recovered_verified_and_archived_tasks_count_as_completed(self):
        harness = CoordinatorHarness(
            Path(self.temporary.name) / "recovered-partial",
            manifest=sibling_manifest(),
        )
        components = _Components(harness)
        task_ids = components.cursor.graph_index.topological_order
        for position, state in enumerate(
            (RuntimeState.VERIFIED, RuntimeState.ARCHIVED)
        ):
            components.identity = ExecutionIdentity(
                harness.manifest.run_id,
                1,
                task_ids[position],
                1,
                f"CORRELATION-RECOVERED-{position + 1:03d}",
            )
            components.set_task_state(state)
        components.identity = ExecutionIdentity(
            harness.manifest.run_id,
            1,
            task_ids[-1],
            1,
            "CORRELATION-RECOVERED-REMAINING",
        )
        components.attempt = SimpleNamespace(identity=components.identity)

        service = ForegroundRunService(
            harness.manifest,
            components=components,
            backend_admitter=components.admit_backend,
        )
        with mock.patch.object(
            service,
            "_execution_admission_valid",
            return_value=True,
        ):
            result = service.run()

        self.assertIs(result.status, ForegroundRunStatus.COMPLETED)
        self.assertEqual(task_ids, result.completed_task_ids)
        self.assertEqual(1, result.batch_count)

    def test_recovered_release_at_cursor_head_completes_without_reacquiring(self):
        self.components.set_task_state(RuntimeState.VERIFIED)
        terminal = self.components.finish(self.components.cursor)
        recovered_cursor = terminal.cursor
        recovered_release = terminal.events[-1]
        self.components.trace.clear()
        self.components.acquire_lease = mock.Mock(
            side_effect=AssertionError("terminal recovery must not reacquire a lease")
        )

        def finish(cursor):
            self.components.trace.append("terminal")
            return ForegroundTerminalResult(True, cursor, (recovered_release,))

        self.components.finish = mock.Mock(side_effect=finish)

        result = self.service().run()

        self.assertIs(result.status, ForegroundRunStatus.COMPLETED)
        self.assertEqual(
            (self.harness.manifest.tasks[0].id,),
            result.completed_task_ids,
        )
        self.assertEqual(0, result.batch_count)
        self.assertEqual(recovered_cursor, result.cursor)
        self.components.acquire_lease.assert_not_called()
        self.components.finish.assert_called_once_with(recovered_cursor)
        self.assertNotIn("coordinator", self.components.trace)

    def test_recovered_completed_tasks_with_active_lease_still_admit_lease(self):
        self.components.set_task_state(RuntimeState.VERIFIED)

        result = self.service().run()

        self.assertIs(result.status, ForegroundRunStatus.COMPLETED)
        self.assertEqual(0, result.batch_count)
        self.assertIn("lease", self.components.trace)
        self.assertIn("terminal", self.components.trace)
        self.assertNotIn("coordinator", self.components.trace)

    def test_execution_admission_result_cannot_be_reused_for_another_manifest(self):
        sibling_harness = CoordinatorHarness(
            Path(self.temporary.name) / "sibling",
            manifest=sibling_manifest(),
        )
        components = _Components(sibling_harness)
        one_task_manifest, events = admitted_events()
        admitted = admit_execution_snapshot(
            one_task_manifest,
            tuple(events),
            workspace_hash=WORKSPACE_HASH,
        )

        with mock.patch.object(components, "validate_execution", return_value=admitted):
            result = ForegroundRunService(
                sibling_harness.manifest,
                components=components,
                backend_admitter=components.admit_backend,
            ).run()

        self.assertIs(result.reason, ForegroundRunReason.MANIFEST_NOT_ADMITTED)
        self.assertNotIn("control_root", components.trace)

    def test_unknown_worker_fails_closed_before_result_or_git_followups(self):
        self.components.worker_unknown = True

        result = self.service().run()

        self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
        self.assertIs(result.reason, ForegroundRunReason.WORKER_OUTCOME_UNKNOWN)
        self.assertIs(result.stage, ForegroundRunStage.WORKER)
        self.assertNotIn("worker_result", self.components.trace)
        self.assertNotIn("stage", self.components.trace)
        self.assertNotIn("cleanup", self.components.trace)
        self.assertNotIn("terminal", self.components.trace)

    def test_failed_sibling_does_not_strand_successful_sibling(self):
        harness = CoordinatorHarness(
            Path(self.temporary.name) / "mixed-siblings",
            manifest=sibling_manifest(),
        )
        components = _Components(harness)
        foundation_id, successful_id, failed_id = (
            components.cursor.graph_index.topological_order
        )
        components.set_task_state(RuntimeState.VERIFIED)
        identities = tuple(
            ExecutionIdentity(
                harness.manifest.run_id,
                1,
                task_id,
                1,
                f"CORRELATION-MIXED-{position:03d}",
            )
            for position, task_id in enumerate(
                (failed_id, successful_id),
                start=1,
            )
        )
        attempts = {
            identity: SimpleNamespace(identity=identity) for identity in identities
        }
        dispatch_calls: list[str] = []
        worker_batches: list[tuple[str, ...]] = []
        stage_calls: list[str] = []
        promotion_calls: list[str] = []
        cleanup_calls: list[str] = []

        def replace_cursor(snapshot):
            components.cursor = type(components.cursor)(
                snapshot,
                GraphIndex.compile(harness.manifest, snapshot),
                components.cursor.lease_state,
                components.cursor.dispatch_recoveries,
            )

        def set_task(task_id, state, reason_code=None):
            replace_cursor(
                dataclasses.replace(
                    components.cursor.snapshot,
                    tasks=tuple(
                        dataclasses.replace(
                            task,
                            state=state,
                            reason_code=reason_code,
                        )
                        if task.task_id == task_id
                        else task
                        for task in components.cursor.snapshot.tasks
                    ),
                )
            )

        def set_attempt(identity, state, reason_code=None):
            replacement = AttemptProjection(
                identity.task_id,
                identity.attempt,
                identity.correlation_id,
                identity.coordinator_epoch,
                state,
                reason_code,
            )
            current = components.cursor.snapshot.attempts
            if any(item.task_id == identity.task_id for item in current):
                updated = tuple(
                    replacement if item.task_id == identity.task_id else item
                    for item in current
                )
            else:
                updated = current + (replacement,)
            replace_cursor(
                dataclasses.replace(
                    components.cursor.snapshot,
                    attempts=updated,
                )
            )

        def block_run():
            replace_cursor(
                dataclasses.replace(
                    components.cursor.snapshot,
                    status=RuntimeState.BLOCKED,
                    run_reason_code=RuntimeReasonCode.CHECK_FAILED,
                )
            )

        class SiblingCoordinator:
            def reserve_ready(self, *, limit=None):
                if limit is not None:
                    raise AssertionError("unexpected foreground limit")
                if components.cursor.snapshot.status is RuntimeState.BLOCKED:
                    return CoordinatorReservationResult(
                        CoordinatorStatus.BLOCKED,
                        CoordinatorReason.TASK_NOT_READY,
                        components.cursor,
                    )
                for identity in identities:
                    set_task(identity.task_id, RuntimeState.LEASED)
                    set_attempt(identity, RuntimeState.RESERVED)
                return CoordinatorReservationResult(
                    CoordinatorStatus.PROGRESSED,
                    CoordinatorReason.NONE,
                    components.cursor,
                    reserved=identities,
                )

            def dispatch_reserved(self, identity):
                dispatch_calls.append(identity.task_id)
                set_task(identity.task_id, RuntimeState.DISPATCHED)
                set_attempt(identity, RuntimeState.RUNNING)
                return CoordinatorStepResult(
                    CoordinatorStatus.PROGRESSED,
                    CoordinatorReason.NONE,
                    components.cursor,
                    dispatched=(identity,),
                )

            def accept_worker_result(self, proposal):
                state = (
                    RuntimeState.SUCCEEDED
                    if proposal.succeeded
                    else RuntimeState.FAILED
                )
                set_attempt(proposal.identity, state, proposal.reason_code)
                if not proposal.succeeded:
                    set_task(
                        proposal.identity.task_id,
                        RuntimeState.BLOCKED,
                        proposal.reason_code,
                    )
                    block_run()
                return CoordinatorStepResult(
                    CoordinatorStatus.PROGRESSED,
                    CoordinatorReason.NONE,
                    components.cursor,
                )

        class SiblingWorkflow:
            def prepare_attempt(self, identity):
                return AttemptPreparationResult(
                    WorkflowStatus.PROGRESSED,
                    WorkflowReason.NONE,
                    components.cursor,
                    (),
                    attempts[identity],
                )

            def stage_attempt_result(self, attempt):
                task_id = attempt.identity.task_id
                stage_calls.append(task_id)
                set_task(task_id, RuntimeState.STAGED)
                return ResultStageResult(
                    WorkflowStatus.PROGRESSED,
                    WorkflowReason.NONE,
                    components.cursor,
                    (),
                    staged=SimpleNamespace(task_id=task_id),
                )

            def promote_staged(self, sources, acceptance):
                del acceptance
                promoted = []
                for source in sources:
                    promotion_calls.append(source.task_id)
                    set_task(source.task_id, RuntimeState.VERIFIED)
                    promoted.append(SimpleNamespace(task_id=source.task_id))
                return PromotionBatchResult(
                    WorkflowStatus.PROGRESSED,
                    WorkflowReason.NONE,
                    components.cursor,
                    (),
                    tuple(promoted),
                )

        def run_workers(prepared, cursor):
            del cursor
            worker_batches.append(tuple(item.identity.task_id for item in prepared))
            return WorkerBatchResult(
                True,
                (
                    WorkerResultProposal(
                        identities[0],
                        "worker-failed",
                        False,
                        RuntimeReasonCode.CHECK_FAILED,
                    ),
                    WorkerResultProposal(identities[1], "worker-success", True),
                ),
            )

        def cleanup_attempt(workflow, attempt, promotion):
            del workflow, promotion
            cleanup_calls.append(attempt.identity.task_id)
            return CleanupStepResult(
                WorkflowStatus.PROGRESSED,
                WorkflowReason.NONE,
                components.cursor,
                (),
            )

        components.coordinator = lambda cursor: SiblingCoordinator()
        components.workflow = lambda cursor: SiblingWorkflow()
        components.run_workers = run_workers
        components.cleanup_attempt = cleanup_attempt
        components.finish = mock.Mock(
            side_effect=AssertionError("a mixed worker batch cannot finish the run")
        )
        service = ForegroundRunService(
            harness.manifest,
            components=components,
            backend_admitter=components.admit_backend,
        )

        with mock.patch.object(
            service,
            "_execution_admission_valid",
            return_value=True,
        ):
            result = service.run()
            restarted = service.run()

        self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
        self.assertIs(result.reason, ForegroundRunReason.WORKER_FAILED)
        self.assertIs(result.stage, ForegroundRunStage.WORKER)
        self.assertEqual((foundation_id, successful_id), result.completed_task_ids)
        self.assertEqual(1, result.batch_count)
        self.assertEqual([successful_id], stage_calls)
        self.assertEqual([successful_id], promotion_calls)
        self.assertEqual([successful_id], cleanup_calls)
        self.assertEqual([(failed_id, successful_id)], worker_batches)
        self.assertEqual([failed_id, successful_id], dispatch_calls)
        self.assertIs(restarted.reason, ForegroundRunReason.COORDINATOR_BLOCKED)
        self.assertEqual([(failed_id, successful_id)], worker_batches)
        self.assertEqual([failed_id, successful_id], dispatch_calls)
        self.assertEqual(
            {
                foundation_id: RuntimeState.VERIFIED,
                successful_id: RuntimeState.VERIFIED,
                failed_id: RuntimeState.BLOCKED,
            },
            dict(result.cursor.graph_index.task_states),
        )
        attempt_states = {
            attempt.task_id: attempt.state
            for attempt in result.cursor.snapshot.attempts
        }
        self.assertEqual(RuntimeState.SUCCEEDED, attempt_states[successful_id])
        self.assertEqual(RuntimeState.FAILED, attempt_states[failed_id])
        components.finish.assert_not_called()

    def test_worker_renewal_cursor_and_event_are_adopted_before_result_admission(self):
        self.components.worker_renews = True

        result = self.service().run()

        self.assertIs(result.status, ForegroundRunStatus.COMPLETED)
        self.assertIsNotNone(self.components.worker_input_cursor)
        self.assertIn(
            JournalEventType.LEASE_RENEWED,
            tuple(event.event_type for event in self.components.checkpoint_events),
        )
        renewal = next(
            event
            for event in self.components.checkpoint_events
            if event.event_type is JournalEventType.LEASE_RENEWED
        )
        self.assertEqual(
            renewal.event_hash,
            self.components.coordinator_cursors[-1].head.event_hash,
        )

    def test_unknown_after_renewal_returns_latest_cursor_and_fails_closed(self):
        self.components.worker_renews = True
        self.components.worker_unknown_after_renewal = True

        result = self.service().run()

        self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
        self.assertIs(result.reason, ForegroundRunReason.WORKER_OUTCOME_UNKNOWN)
        self.assertIsNotNone(result.cursor)
        assert result.cursor is not None
        self.assertIs(
            result.cursor.lease_state.event_type,
            JournalEventType.LEASE_RENEWED,
        )
        self.assertEqual(self.components.cursor.head, result.cursor.head)
        self.assertNotIn("worker_result", self.components.trace)

    def test_worker_cursor_with_an_omitted_journal_head_is_rejected(self):
        self.components.worker_renews = True
        self.components.worker_omits_renewal_head = True

        result = self.service().run()

        self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
        self.assertIs(result.reason, ForegroundRunReason.WORKER_RESULT_INVALID)
        self.assertEqual(self.components.worker_input_cursor, result.cursor)
        self.assertNotIn("worker_result", self.components.trace)

    def test_acceptance_cleanup_checkpoint_and_terminal_each_fail_closed(self):
        cases = (
            (
                "acceptance_fails",
                ForegroundRunReason.ACCEPTANCE_FAILED,
                ForegroundRunStage.ACCEPTANCE,
            ),
            (
                "cleanup_blocks",
                ForegroundRunReason.CLEANUP_BLOCKED,
                ForegroundRunStage.CLEANUP,
            ),
            (
                "checkpoint_blocks",
                ForegroundRunReason.CHECKPOINT_BLOCKED,
                ForegroundRunStage.CHECKPOINT,
            ),
            (
                "terminal_blocks",
                ForegroundRunReason.TERMINAL_LIFECYCLE_BLOCKED,
                ForegroundRunStage.TERMINAL_LIFECYCLE,
            ),
        )
        for flag, reason, stage in cases:
            with self.subTest(flag=flag):
                components = _Components(self.harness)
                setattr(components, flag, True)
                result = ForegroundRunService(
                    self.harness.manifest,
                    components=components,
                    backend_admitter=components.admit_backend,
                ).run()
                self.assertIs(result.status, ForegroundRunStatus.BLOCKED)
                self.assertIs(result.reason, reason)
                self.assertIs(result.stage, stage)

    def test_pre_effect_admission_boundaries_fail_closed_in_order(self):
        cases = (
            (
                "validation_result",
                ForegroundRunReason.MANIFEST_NOT_ADMITTED,
                ["backend_admission", "execution_admission"],
            ),
            (
                "control_root_result",
                ForegroundRunReason.CONTROL_ROOT_NOT_PROTECTED,
                ["backend_admission", "execution_admission", "control_root"],
            ),
            (
                "workspace_result",
                ForegroundRunReason.WORKSPACE_IDENTITY_NOT_VERIFIED,
                [
                    "backend_admission",
                    "execution_admission",
                    "control_root",
                    "workspace_identity",
                ],
            ),
            (
                "recovery_result",
                ForegroundRunReason.RECOVERY_NOT_VERIFIED,
                [
                    "backend_admission",
                    "execution_admission",
                    "control_root",
                    "workspace_identity",
                    "recovery",
                ],
            ),
            (
                "lease_result",
                ForegroundRunReason.LEASE_NOT_ADMITTED,
                [
                    "backend_admission",
                    "execution_admission",
                    "control_root",
                    "workspace_identity",
                    "recovery",
                    "lease",
                ],
            ),
        )
        for flag, reason, trace in cases:
            with self.subTest(flag=flag):
                components = _Components(self.harness)
                setattr(components, flag, False)
                result = ForegroundRunService(
                    self.harness.manifest,
                    components=components,
                    backend_admitter=components.admit_backend,
                ).run()
                self.assertIs(result.reason, reason)
                self.assertEqual(trace, components.trace)

    def test_boundary_exceptions_and_invalid_admission_fail_closed(self):
        for admitter in (
            lambda manifest: (_ for _ in ()).throw(RuntimeError("backend crashed")),
            lambda manifest: object(),
        ):
            with self.subTest(admitter=admitter):
                result = ForegroundRunService(
                    self.harness.manifest,
                    components=self.components,
                    backend_admitter=admitter,
                ).run()
                self.assertIs(result.status, ForegroundRunStatus.REJECTED)
                self.assertIs(result.reason, ForegroundRunReason.BACKEND_NOT_ADMITTED)

        without_components = ForegroundRunService(
            self.harness.manifest,
            backend_admitter=_admitted_backend,
        ).run()
        self.assertIs(
            without_components.reason,
            ForegroundRunReason.COMPOSITION_UNAVAILABLE,
        )

        cases = (
            (
                "validate_execution",
                ForegroundRunReason.MANIFEST_NOT_ADMITTED,
            ),
            (
                "protect_control_root",
                ForegroundRunReason.CONTROL_ROOT_NOT_PROTECTED,
            ),
            (
                "verify_workspace_identity",
                ForegroundRunReason.WORKSPACE_IDENTITY_NOT_VERIFIED,
            ),
            (
                "recover_verified_cursor",
                ForegroundRunReason.RECOVERY_NOT_VERIFIED,
            ),
            (
                "acquire_lease",
                ForegroundRunReason.LEASE_NOT_ADMITTED,
            ),
        )
        for method, reason in cases:
            with self.subTest(method=method):
                components = _Components(self.harness)
                with mock.patch.object(
                    components,
                    method,
                    side_effect=RuntimeError(f"{method} crashed"),
                ):
                    result = ForegroundRunService(
                        self.harness.manifest,
                        components=components,
                        backend_admitter=components.admit_backend,
                    ).run()
                self.assertIs(result.reason, reason)

    def test_effect_boundary_failures_never_become_completion(self):
        def coordinator_raises(components):
            return mock.patch.object(
                _Coordinator,
                "reserve_ready",
                side_effect=RuntimeError("coordinator crashed"),
            )

        def coordinator_idle(components):
            return mock.patch.object(
                _Coordinator,
                "reserve_ready",
                return_value=CoordinatorReservationResult(
                    CoordinatorStatus.IDLE,
                    CoordinatorReason.NO_READY_TASKS,
                    components.cursor,
                ),
            )

        def coordinator_invalid(components):
            return mock.patch.object(
                _Coordinator,
                "reserve_ready",
                return_value=object(),
            )

        def coordinator_empty(components):
            return mock.patch.object(
                _Coordinator,
                "reserve_ready",
                return_value=object(),
            )

        def dispatch_raises(components):
            return mock.patch.object(
                _Coordinator,
                "dispatch_reserved",
                side_effect=RuntimeError("dispatch crashed"),
            )

        def prepare_raises(components):
            return mock.patch.object(
                _Workflow,
                "prepare_attempt",
                side_effect=RuntimeError("prepare crashed"),
            )

        def worker_missing(components):
            return mock.patch.object(
                components,
                "run_workers",
                return_value=WorkerBatchResult(True, ()),
            )

        def worker_raises(components):
            return mock.patch.object(
                components,
                "run_workers",
                side_effect=RuntimeError("worker crashed"),
            )

        def result_raises(components):
            return mock.patch.object(
                _Coordinator,
                "accept_worker_result",
                side_effect=RuntimeError("result crashed"),
            )

        def stage_raises(components):
            return mock.patch.object(
                _Workflow,
                "stage_attempt_result",
                side_effect=RuntimeError("stage crashed"),
            )

        def promotion_raises(components):
            return mock.patch.object(
                _Workflow,
                "promote_staged",
                side_effect=RuntimeError("promotion crashed"),
            )

        def cleanup_raises(components):
            return mock.patch.object(
                components,
                "cleanup_attempt",
                side_effect=RuntimeError("cleanup crashed"),
            )

        def checkpoint_raises(components):
            return mock.patch.object(
                components,
                "publish_checkpoint",
                side_effect=RuntimeError("checkpoint crashed"),
            )

        def terminal_raises(components):
            return mock.patch.object(
                components,
                "finish",
                side_effect=RuntimeError("terminal crashed"),
            )

        cases = (
            (coordinator_raises, ForegroundRunReason.COORDINATOR_BLOCKED),
            (coordinator_idle, ForegroundRunReason.COORDINATOR_IDLE),
            (coordinator_invalid, ForegroundRunReason.INVARIANT_VIOLATION),
            (coordinator_empty, ForegroundRunReason.INVARIANT_VIOLATION),
            (prepare_raises, ForegroundRunReason.WORKFLOW_BLOCKED),
            (dispatch_raises, ForegroundRunReason.COORDINATOR_BLOCKED),
            (worker_missing, ForegroundRunReason.WORKER_RESULT_INVALID),
            (worker_raises, ForegroundRunReason.WORKER_OUTCOME_UNKNOWN),
            (result_raises, ForegroundRunReason.COORDINATOR_BLOCKED),
            (stage_raises, ForegroundRunReason.WORKFLOW_BLOCKED),
            (promotion_raises, ForegroundRunReason.WORKFLOW_BLOCKED),
            (cleanup_raises, ForegroundRunReason.CLEANUP_BLOCKED),
            (checkpoint_raises, ForegroundRunReason.CHECKPOINT_BLOCKED),
            (terminal_raises, ForegroundRunReason.TERMINAL_LIFECYCLE_BLOCKED),
        )
        for patch_factory, reason in cases:
            with self.subTest(patch_factory=patch_factory):
                components = _Components(self.harness)
                with patch_factory(components):
                    result = ForegroundRunService(
                        self.harness.manifest,
                        components=components,
                        backend_admitter=components.admit_backend,
                    ).run()
                self.assertIsNot(result.status, ForegroundRunStatus.COMPLETED)
                self.assertIs(result.reason, reason)

        failed_worker = _Components(self.harness)
        failed_worker.worker_fails = True
        result = ForegroundRunService(
            self.harness.manifest,
            components=failed_worker,
            backend_admitter=failed_worker.admit_backend,
        ).run()
        self.assertIs(result.reason, ForegroundRunReason.WORKER_FAILED)
        self.assertNotIn("stage", failed_worker.trace)

    def test_promotion_identity_mismatch_and_batch_bound_fail_closed(self):
        components = _Components(self.harness)

        def mismatched_promotion(workflow, sources, acceptance):
            del sources, acceptance
            workflow.components.set_task_state(RuntimeState.VERIFIED)
            return PromotionBatchResult(
                WorkflowStatus.PROGRESSED,
                WorkflowReason.NONE,
                workflow.components.cursor,
                (),
                (SimpleNamespace(task_id="TASK-999"),),
            )

        with mock.patch.object(_Workflow, "promote_staged", mismatched_promotion):
            result = ForegroundRunService(
                self.harness.manifest,
                components=components,
                backend_admitter=components.admit_backend,
            ).run()
        self.assertIs(result.reason, ForegroundRunReason.INVARIANT_VIOLATION)

        sibling_harness = CoordinatorHarness(
            Path(self.temporary.name) / "bounded",
            manifest=sibling_manifest(),
        )
        bounded_components = _Components(sibling_harness)
        service = ForegroundRunService(
            sibling_harness.manifest,
            components=bounded_components,
            backend_admitter=bounded_components.admit_backend,
            max_batches=1,
        )
        with mock.patch.object(
            service,
            "_execution_admission_valid",
            return_value=True,
        ):
            bounded = service.run()
        self.assertIs(bounded.reason, ForegroundRunReason.BOUNDED_RUN_EXHAUSTED)
        self.assertEqual(1, bounded.batch_count)

    def test_public_contracts_reject_malformed_composition_values(self):
        with self.assertRaises(TypeError):
            ForegroundRunService(object())
        with self.assertRaises(TypeError):
            ForegroundRunService(self.harness.manifest, components=object())
        with self.assertRaises(TypeError):
            ForegroundRunService(self.harness.manifest, components_factory=object())
        with self.assertRaises(ValueError):
            ForegroundRunService(
                self.harness.manifest,
                components=self.components,
                components_factory=lambda: self.components,
            )
        with self.assertRaises(TypeError):
            ForegroundRunService(self.harness.manifest, backend_admitter=None)
        for max_batches in (0, -1, True):
            with self.subTest(max_batches=max_batches), self.assertRaises(ValueError):
                ForegroundRunService(
                    self.harness.manifest,
                    backend_admitter=_admitted_backend,
                    max_batches=max_batches,
                )

        with self.assertRaises(ValueError):
            PreparedForegroundAttempt(
                ExecutionIdentity(self.harness.manifest.run_id, 1),
                object(),
            )
        with self.assertRaises(ValueError):
            PreparedForegroundAttempt(self.components.identity, None)
        with self.assertRaises(TypeError):
            WorkerBatchResult(1)
        with self.assertRaises(TypeError):
            WorkerBatchResult(True, [object()])
        with self.assertRaises(ValueError):
            WorkerBatchResult(
                False,
                (
                    WorkerResultProposal(
                        self.components.identity,
                        "worker-contract-001",
                        True,
                    ),
                ),
            )
        with self.assertRaises(TypeError):
            WorkerLeaseRenewalResult(1)
        with self.assertRaises(ValueError):
            WorkerLeaseRenewalResult(False, self.components.cursor)
        with self.assertRaises(ValueError):
            WorkerBatchResult(
                True,
                (),
                self.components.cursor,
                (
                    JournalEvent.create(
                        sequence=self.components.cursor.head.sequence + 1,
                        event_id="EVENT-WORKER-NOT-A-RENEWAL",
                        event_type=JournalEventType.RUN_PAUSED,
                        identity=ExecutionIdentity(
                            self.harness.manifest.run_id,
                            1,
                        ),
                        actor_type=ActorType.SYSTEM,
                        actor_id="test",
                        recorded_at="2026-08-19T00:01:00Z",
                        previous_event_hash=self.components.cursor.head.event_hash,
                        payload=TransitionPayload(
                            TransitionSubject.RUN,
                            RuntimeState.RUNNING,
                            RuntimeState.PAUSED,
                        ),
                    ),
                ),
            )

        admission = _admitted_backend(self.harness.manifest)
        with self.assertRaises(ValueError):
            ForegroundRunResult(
                ForegroundRunStatus.COMPLETED,
                ForegroundRunReason.NONE,
                ForegroundRunStage.TERMINAL_LIFECYCLE,
                admission,
            )
        with self.assertRaises(ValueError):
            ForegroundRunResult(
                ForegroundRunStatus.BLOCKED,
                ForegroundRunReason.NONE,
                ForegroundRunStage.CHECKPOINT,
                admission,
                self.components.cursor,
            )


if __name__ == "__main__":
    unittest.main()
