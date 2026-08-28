from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from tests.e2e.support import BASE_TIME, E2EHarness, serial_parallel_manifest
from tests.services import test_attempts as attempt_fixtures
from wish_builder.adapters.fake import FakeTaskPort
from wish_builder.adapters.git_identity import (
    FilesystemIdentity,
    WorkspaceIdentity,
    capture_workspace_identity,
)
from wish_builder.adapters.git_worktree import (
    AttemptResultManifest,
    AttemptWorktree,
    ChangedPath,
    GitTreeEntry,
    GitWorktreeAdapter,
    ResultValidation,
    StagedResult,
)
from wish_builder.adapters.process_identity import (
    LeaseOwnerProcessState,
    capture_process_start_id,
    probe_lease_owner_process,
)
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.contracts import (
    ActorIdentity,
    ActorType,
    EffectObjectType,
    EffectOperation,
    EffectReceiptValue,
    ExecutionIdentity,
    JournalEventType,
    LeaseOwner,
    PathCaseMode,
    RuntimeReasonCode,
    decode_journal_event_bytes,
)
from wish_builder.contracts.runtime_decoder import (
    decode_evidence_ref_primitive,
    decode_execution_identity_primitive,
)
from wish_builder.processes import CoordinatorCursor, ForegroundCoordinator
from wish_builder.services.cleanup import (
    CleanupCandidate,
    CleanupCommand,
    CleanupInspection,
    CleanupPlan,
    CleanupService,
)
from wish_builder.services.journal import DurableJournal
from wish_builder.services.promotion import (
    PromotionCommand,
    PromotionPlan,
    PromotionService,
)
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    LeaseMutationStatus,
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)

CRASH_EXIT_CODE = 86
RECOVERY_TIME = BASE_TIME + timedelta(seconds=1_000)
PORT_TIME = "2026-08-19T00:16:41Z"
RESTARTED_COORDINATOR_ID = "coordinator-e2e-restarted"


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with temporary.open("wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _events(journal_root: Path):
    events = []
    for segment in sorted((journal_root / "segments").glob("segment-*.jsonl")):
        for raw in segment.read_bytes().splitlines(keepends=True):
            decoded = decode_journal_event_bytes(raw)
            if not decoded.ok or decoded.value is None:
                raise AssertionError(decoded.report.render_text())
            events.append(decoded.value)
    return tuple(events)


def _head(event) -> dict[str, object]:
    return {"event_hash": event.event_hash, "sequence": event.sequence}


def _manifest_scopes(manifest) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *manifest.protected_paths,
                *(
                    path
                    for task in manifest.tasks
                    for path in (*task.owned_paths, *task.allowed_auxiliary_paths)
                ),
            }
        )
    )


def _receipt(outcome):
    value = outcome.value
    if type(value) is not EffectReceiptValue:
        raise AssertionError(f"expected an EffectReceiptValue, got {outcome!r}")
    return value.receipt


def _mapping(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise AssertionError(f"expected a JSON object, got {type(value).__name__}")
    return cast(dict[str, Any], value)


def _execution_identity(value: object) -> ExecutionIdentity:
    decoded = decode_execution_identity_primitive(value)
    if not decoded.ok or decoded.value is None:
        raise AssertionError(decoded.report.render_text())
    return decoded.value


def _evidence_ref(value: object):
    decoded = decode_evidence_ref_primitive(value)
    if not decoded.ok or decoded.value is None:
        raise AssertionError(decoded.report.render_text())
    return decoded.value


def _filesystem_identity(value: object) -> FilesystemIdentity:
    raw = _mapping(value)
    return FilesystemIdentity(
        lexical_path=raw["lexical_path"],
        canonical_path=raw["canonical_path"],
        link_device=raw["link_device"],
        link_inode=raw["link_inode"],
        target_device=raw["target_device"],
        target_inode=raw["target_inode"],
        is_link_or_reparse_point=raw["is_link_or_reparse_point"],
        access_control_hash=raw["access_control_hash"],
    )


def _workspace_identity(value: object) -> WorkspaceIdentity:
    raw = _mapping(value)
    return WorkspaceIdentity(
        local_repository_id=raw["local_repository_id"],
        local_worktree_id=raw["local_worktree_id"],
        common_dir=_filesystem_identity(raw["common_dir"]),
        worktree_root=_filesystem_identity(raw["worktree_root"]),
        git_dir=_filesystem_identity(raw["git_dir"]),
        target_full_ref=raw["target_full_ref"],
        base_commit_sha=raw["base_commit_sha"],
        scopes=tuple(raw["scopes"]),
        index_dirty_fingerprint=raw["index_dirty_fingerprint"],
    )


def _git_tree_entry(value: object | None) -> GitTreeEntry | None:
    if value is None:
        return None
    raw = _mapping(value)
    return GitTreeEntry(
        path=raw["path"],
        mode=raw["mode"],
        object_id=raw["object_id"],
        byte_length=raw["byte_length"],
    )


def _attempt_manifest(value: object) -> AttemptResultManifest:
    raw = _mapping(value)
    changed_paths = tuple(
        ChangedPath(
            path=item["path"],
            base=_git_tree_entry(item["base"]),
            result=_git_tree_entry(item["result"]),
        )
        for item in (_mapping(candidate) for candidate in raw["changed_paths"])
    )
    return AttemptResultManifest(
        schema_version=raw["schema_version"],
        identity=_execution_identity(raw["identity"]),
        local_repository_id=raw["local_repository_id"],
        attempt_hash=raw["attempt_hash"],
        base_commit_sha=raw["base_commit_sha"],
        base_tree_sha=raw["base_tree_sha"],
        result_commit_sha=raw["result_commit_sha"],
        result_tree_sha=raw["result_tree_sha"],
        path_case_mode=PathCaseMode(raw["path_case_mode"]),
        changed_paths=changed_paths,
        total_blob_bytes=raw["total_blob_bytes"],
        portable_profile_hash=raw["portable_profile_hash"],
    )


def _attempt_worktree(value: object) -> AttemptWorktree:
    raw = _mapping(value)
    return AttemptWorktree(
        identity=_execution_identity(raw["identity"]),
        path=raw["path"],
        external_object_id=raw["external_object_id"],
        local_repository_id=raw["local_repository_id"],
        target_workspace_hash=raw["target_workspace_hash"],
        worktree_root=_filesystem_identity(raw["worktree_root"]),
        git_dir=_filesystem_identity(raw["git_dir"]),
        base_commit_sha=raw["base_commit_sha"],
        base_tree_sha=raw["base_tree_sha"],
        owned_paths=tuple(raw["owned_paths"]),
        allowed_auxiliary_paths=tuple(raw["allowed_auxiliary_paths"]),
        protected_paths=tuple(raw["protected_paths"]),
        path_case_mode=PathCaseMode(raw["path_case_mode"]),
    )


def _staged_result_primitive(value: StagedResult) -> dict[str, object]:
    return {
        "manifest": value.manifest.to_primitive(),
        "stage_effect_hash": value.stage_effect_hash,
        "staged_ref": value.staged_ref,
    }


def _staged_result(value: object) -> StagedResult:
    raw = _mapping(value)
    return StagedResult(
        manifest=_attempt_manifest(raw["manifest"]),
        staged_ref=raw["staged_ref"],
        stage_effect_hash=raw["stage_effect_hash"],
    )


def _promotion_plan_primitive(plan: PromotionPlan) -> dict[str, object]:
    return {
        "command": plan.command.to_primitive(),
        "source": _staged_result_primitive(cast(StagedResult, plan.source)),
    }


def _promotion_plan(value: object) -> PromotionPlan:
    raw = _mapping(value)
    command_raw = _mapping(raw["command"])
    command = PromotionCommand(
        operation_id=command_raw["operation_id"],
        run_id=command_raw["run_id"],
        coordinator_epoch=command_raw["coordinator_epoch"],
        task_id=command_raw["task_id"],
        attempt=command_raw["attempt"],
        topological_position=command_raw["topological_position"],
        local_repository_id=command_raw["local_repository_id"],
        target_workspace_hash=command_raw["target_workspace_hash"],
        expected_target_sha=command_raw["expected_target_sha"],
        staged_ref=command_raw["staged_ref"],
        result_manifest_hash=command_raw["result_manifest_hash"],
        source_commit_sha=command_raw["source_commit_sha"],
        source_tree_sha=command_raw["source_tree_sha"],
        candidate_commit_sha=command_raw["candidate_commit_sha"],
        candidate_tree_sha=command_raw["candidate_tree_sha"],
        acceptance_evidence=tuple(
            _evidence_ref(item) for item in command_raw["acceptance_evidence"]
        ),
    )
    return PromotionPlan(command, _staged_result(raw["source"]))


def _cleanup_plan_primitive(plan: CleanupPlan) -> dict[str, object]:
    candidate = plan.candidate
    inspection = plan.inspection
    return {
        "candidate": {
            "attempt": cast(AttemptWorktree, candidate.attempt).to_primitive(),
            "evidence": [item.to_primitive() for item in candidate.evidence],
            "expected_head_sha": candidate.expected_head_sha,
            "outcome_known": candidate.outcome_known,
            "process_tree_terminated": candidate.process_tree_terminated,
            "reconciliation_complete": candidate.reconciliation_complete,
        },
        "command": plan.command.to_primitive(),
        "inspection": {
            "clean": inspection.clean,
            "details": list(inspection.details),
            "exists": inspection.exists,
            "identity_ok": inspection.identity_ok,
            "observed_head_sha": inspection.observed_head_sha,
            "state_hash": inspection.state_hash,
            "target_workspace_hash": inspection.target_workspace_hash,
        },
        "quarantine_reason": (
            None if plan.quarantine_reason is None else plan.quarantine_reason.value
        ),
    }


def _cleanup_plan(value: object) -> CleanupPlan:
    raw = _mapping(value)
    candidate_raw = _mapping(raw["candidate"])
    command_raw = _mapping(raw["command"])
    inspection_raw = _mapping(raw["inspection"])
    candidate = CleanupCandidate(
        attempt=_attempt_worktree(candidate_raw["attempt"]),
        expected_head_sha=candidate_raw["expected_head_sha"],
        evidence=tuple(_evidence_ref(item) for item in candidate_raw["evidence"]),
        reconciliation_complete=candidate_raw["reconciliation_complete"],
        process_tree_terminated=candidate_raw["process_tree_terminated"],
        outcome_known=candidate_raw["outcome_known"],
    )
    command = CleanupCommand(
        operation_id=command_raw["operation_id"],
        run_id=command_raw["run_id"],
        coordinator_epoch=command_raw["coordinator_epoch"],
        task_id=command_raw["task_id"],
        attempt=command_raw["attempt"],
        local_repository_id=command_raw["local_repository_id"],
        target_workspace_hash=command_raw["target_workspace_hash"],
        external_object_id=command_raw["external_object_id"],
        expected_head_sha=command_raw["expected_head_sha"],
        observed_state_hash=command_raw["observed_state_hash"],
        evidence_digests=tuple(command_raw["evidence_digests"]),
        remove_allowed=command_raw["remove_allowed"],
    )
    inspection = CleanupInspection(
        exists=inspection_raw["exists"],
        identity_ok=inspection_raw["identity_ok"],
        clean=inspection_raw["clean"],
        observed_head_sha=inspection_raw["observed_head_sha"],
        target_workspace_hash=inspection_raw["target_workspace_hash"],
        state_hash=inspection_raw["state_hash"],
        details=tuple(inspection_raw["details"]),
    )
    raw_reason = raw["quarantine_reason"]
    reason = None if raw_reason is None else RuntimeReasonCode(raw_reason)
    return CleanupPlan(command, candidate, inspection, reason)


def crash_dispatch(root: Path, point: str) -> None:
    harness = E2EHarness(root)
    marker_path = root / "dispatch-crash.json"
    marker: dict[str, object] = {
        "crash_pid": os.getpid(),
        "crash_process_start_id": capture_process_start_id(),
        "point": point,
        "repository_head": attempt_fixtures.git_text(
            harness.repository,
            "rev-parse",
            "HEAD",
        ),
    }
    _write_json(marker_path, marker)

    def crash_at_boundary(observed: str, _: Path) -> None:
        if observed != point:
            return
        events = _events(harness.journal_root)
        effect_paths = tuple(
            (harness.control_root / "effects/task/effects").glob("*.json")
        )
        marker.update(
            {
                "boundary_head": _head(events[-1]),
                "effect_sha256": (
                    None
                    if not effect_paths
                    else hashlib.sha256(effect_paths[0].read_bytes()).hexdigest()
                ),
                "effects": len(effect_paths),
                "last_event_type": events[-1].event_type.value,
                "receipts": len(
                    tuple(
                        (harness.control_root / "effects/task/receipts").glob("*.json")
                    )
                ),
            }
        )
        _write_json(marker_path, marker)
        os._exit(CRASH_EXIT_CODE)

    harness.port = FakeTaskPort(
        harness.control_root / "effects",
        clock=lambda: PORT_TIME,
        failpoint=crash_at_boundary,
    )
    harness.coordinator().dispatch_ready(limit=1)
    raise AssertionError(f"dispatch failpoint was not reached: {point}")


def _take_over_dispatch(root: Path):
    manifest = serial_parallel_manifest()
    journal_root = root / "control/journal"
    recovered = recover_coordinator_lease(
        journal_root,
        manifest,
        coordinator_epoch=1,
        repair_derived=False,
    )
    if (
        recovered.status is not LeaseRecoveryStatus.RECOVERED
        or recovered.lease_state is None
        or recovered.lease_state.lease is None
    ):
        raise AssertionError(recovered)
    prior_lease = recovered.lease_state.lease
    prior_process = probe_lease_owner_process(
        prior_lease.owner,
        local_host_id=prior_lease.owner.actor.host_id,
    )
    if prior_process.state is not LeaseOwnerProcessState.DEAD:
        raise AssertionError(prior_process)

    workspace = capture_workspace_identity(
        root / "repository",
        _manifest_scopes(manifest),
    )
    owner = LeaseOwner(
        ActorIdentity(
            ActorType.COORDINATOR,
            RESTARTED_COORDINATOR_ID,
            prior_lease.owner.actor.host_id,
            os.getpid(),
            capture_process_start_id(),
        ),
        workspace.local_repository_id,
        workspace.local_worktree_id,
        workspace.workspace_hash,
        prior_lease.owner.control_root_id,
    )
    if (
        workspace.local_repository_id != prior_lease.owner.local_repository_id
        or workspace.local_worktree_id != prior_lease.owner.local_worktree_id
        or workspace.workspace_hash != prior_lease.owner.workspace_hash
    ):
        raise AssertionError("workspace identity changed before recovery")

    storage = FilesystemJournalStorage(
        journal_root,
        manifest.run_id,
        authority_clock=lambda: RECOVERY_TIME,
    )
    journal = DurableJournal(manifest.run_id, storage)
    service = CoordinatorLeaseService(
        journal,
        lambda: recover_coordinator_lease(
            journal_root,
            manifest,
            coordinator_epoch=1,
            repair_derived=False,
        ),
        run_id=manifest.run_id,
        owner=owner,
        manifest_digest=manifest.canonical_sha256(),
        lease_ttl_seconds=300,
        lease_clock_skew_seconds=10,
    )
    takeover = service.acquire(
        event_id="EVENT-LEASE-TAKEOVER-E2E-001",
        lease_id="LEASE-E2E-RESTARTED-001",
    )
    if (
        takeover.status is not LeaseMutationStatus.COMMITTED
        or takeover.lease_state is None
        or takeover.lease_state.lease is None
    ):
        raise AssertionError(takeover)
    epoch = takeover.lease_state.lease.fencing_token
    if epoch != 2:
        raise AssertionError(f"expected fencing token 2, got {epoch}")
    new_process = probe_lease_owner_process(
        owner,
        local_host_id=owner.actor.host_id,
    )
    if new_process.state is not LeaseOwnerProcessState.EXACT_ALIVE:
        raise AssertionError(new_process)

    resumed = recover_coordinator_lease(
        journal_root,
        manifest,
        coordinator_epoch=1,
        repair_derived=False,
    )
    if (
        resumed.status is not LeaseRecoveryStatus.RECOVERED
        or resumed.lease_state is None
    ):
        raise AssertionError(resumed)
    cursor = CoordinatorCursor(
        resumed.replay.snapshot,
        resumed.replay.graph_index,
        resumed.lease_state,
        resumed.dispatch_recoveries,
    )
    return (
        manifest,
        journal,
        owner,
        recovered,
        prior_process,
        new_process,
        resumed,
        cursor,
    )


def recover_dispatch(root: Path, result_path: Path, *, corrupt_receipt: bool) -> None:
    marker = json.loads((root / "dispatch-crash.json").read_text(encoding="utf-8"))
    (
        manifest,
        journal,
        owner,
        first_recovery,
        prior_process,
        new_process,
        resumed,
        cursor,
    ) = _take_over_dispatch(root)
    if len(resumed.pending_dispatch_requests) != 1:
        raise AssertionError(resumed.pending_dispatch_requests)
    request = resumed.pending_dispatch_requests[0]
    effects_root = root / "control/effects"
    receipt_paths = tuple((effects_root / "task/receipts").glob("*.json"))
    if corrupt_receipt and len(receipt_paths) != 1:
        raise AssertionError(receipt_paths)

    port = FakeTaskPort(effects_root, clock=lambda: PORT_TIME)
    coordinator = ForegroundCoordinator(
        manifest,
        cursor,
        journal,
        port,
        coordinator_id=RESTARTED_COORDINATOR_ID,
        owner=owner,
        fencing_token=2,
        authority_clock=lambda: RECOVERY_TIME + timedelta(seconds=1),
    )
    reconciled = coordinator.reconcile_dispatch(request)
    replayed_request = coordinator.reconcile_dispatch(request)
    final = recover_coordinator_lease(
        root / "control/journal",
        manifest,
        coordinator_epoch=1,
        repair_derived=False,
    )
    if final.status is not LeaseRecoveryStatus.RECOVERED or final.lease_state is None:
        raise AssertionError(final)
    observed = _receipt(port.lookup(request.identity, EffectOperation.WORKER_DISPATCH))
    events = _events(root / "control/journal")
    observation_events = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.DISPATCH_OBSERVED
    )
    if len(observation_events) != 1:
        raise AssertionError(observation_events)
    observation_event = observation_events[0]
    observation_receipt = observation_event.payload.receipt
    effect_paths = tuple((effects_root / "task/effects").glob("*.json"))
    receipt_paths = tuple((effects_root / "task/receipts").glob("*.json"))
    attempt_states = tuple(item.state.value for item in final.replay.snapshot.attempts)
    task_states = dict(final.replay.graph_index.task_states)
    receipt = reconciled.receipt
    if receipt is None:
        raise AssertionError(reconciled)
    lease = final.lease_state.lease
    if lease is None:
        raise AssertionError("final replay lost the active lease")

    _write_json(
        result_path,
        {
            "attempt_states": list(attempt_states),
            "corrupt_receipt": corrupt_receipt,
            "crash_pid": marker["crash_pid"],
            "dispatch_observed_events": sum(
                event.event_type is JournalEventType.DISPATCH_OBSERVED
                for event in events
            ),
            "dispatch_requested_events": sum(
                event.event_type is JournalEventType.DISPATCH_REQUESTED
                for event in events
            ),
            "effect_files": len(effect_paths),
            "effect_sha256": (
                None
                if not effect_paths
                else hashlib.sha256(effect_paths[0].read_bytes()).hexdigest()
            ),
            "final_head": {
                "event_hash": final.replay.head.event_hash,
                "sequence": final.replay.head.sequence,
            },
            "final_lookup_status": observed.status.value,
            "final_pending_dispatches": len(final.pending_dispatch_requests),
            "final_replay_matches_cursor": (
                final.replay.snapshot == reconciled.cursor.snapshot
                and final.replay.head == reconciled.cursor.head
            ),
            "first_recovery_head": {
                "event_hash": first_recovery.replay.head.event_hash,
                "sequence": first_recovery.replay.head.sequence,
            },
            "first_recovery_pending_dispatches": len(
                first_recovery.pending_dispatch_requests
            ),
            "lease_epoch": lease.fencing_token,
            "lease_owner_matches_restart": lease.owner == owner,
            "new_process_probe": new_process.state.value,
            "observation_identity": observation_event.identity.to_primitive(),
            "observation_receipt_identity": (
                observation_receipt.identity.to_primitive()
            ),
            "prior_process_probe": prior_process.state.value,
            "receipt_evidence_count": len(receipt.evidence),
            "receipt_files": len(receipt_paths),
            "receipt_identity": receipt.identity.to_primitive(),
            "receipt_status": receipt.status.value,
            "replayed_reason": replayed_request.reason.value,
            "replayed_status": replayed_request.status.value,
            "request_identity": request.identity.to_primitive(),
            "restart_pid": os.getpid(),
            "restart_process_start_id": owner.actor.process_start_id,
            "run_state": final.replay.snapshot.status.value,
            "target_clean": not bool(
                attempt_fixtures.git_text(
                    root / "repository",
                    "status",
                    "--porcelain=v1",
                )
            ),
            "target_head": attempt_fixtures.git_text(
                root / "repository",
                "rev-parse",
                "HEAD",
            ),
            "status": reconciled.status.value,
            "reason": reconciled.reason.value,
            "task_state": task_states[request.identity.task_id].value,
        },
    )


GIT_BOUNDARY_POINTS = frozenset(
    {
        "before_attempt_create",
        "after_attempt_create",
        "before_result_stage",
        "after_result_stage",
        "before_target_promotion",
        "after_target_promotion",
        "before_attempt_remove",
        "after_attempt_remove",
    }
)
ATTEMPT_BOUNDARY_POINTS = frozenset({"before_attempt_create", "after_attempt_create"})
STAGE_BOUNDARY_POINTS = frozenset({"before_result_stage", "after_result_stage"})
PROMOTION_BOUNDARY_POINTS = frozenset(
    {"before_target_promotion", "after_target_promotion"}
)
CLEANUP_BOUNDARY_POINTS = frozenset({"before_attempt_remove", "after_attempt_remove"})


def _registered_worktrees(repository: Path) -> tuple[str, ...]:
    output = attempt_fixtures.git_text(
        repository,
        "worktree",
        "list",
        "--porcelain",
    )
    return tuple(
        line.removeprefix("worktree ")
        for line in output.splitlines()
        if line.startswith("worktree ")
    )


def _target_reflog(repository: Path, target_full_ref: str) -> tuple[str, ...]:
    output = attempt_fixtures.git_text(
        repository,
        "reflog",
        "show",
        "--format=%H",
        target_full_ref,
    )
    return tuple(output.splitlines())


def _ref_exists(repository: Path, reference: str) -> bool:
    return (
        attempt_fixtures.git(
            repository,
            "show-ref",
            "--verify",
            "--quiet",
            reference,
            check=False,
        ).returncode
        == 0
    )


def _git_adapter(
    root: Path,
    expected: WorkspaceIdentity,
    failpoint=None,
) -> GitWorktreeAdapter:
    return GitWorktreeAdapter(
        root / "repository",
        root / "attempts",
        expected,
        clock=lambda: attempt_fixtures.FIXED_TIME,
        failpoint=failpoint,
    )


def _attempt_inputs(
    adapter: GitWorktreeAdapter,
    *,
    task_id: str,
    owned_path: str,
    operation_id: str,
    ordinal: int,
):
    identity = ExecutionIdentity(
        "WISH-001",
        1,
        task_id,
        1,
        operation_id,
    )
    command = adapter.plan_attempt(
        identity,
        owned_paths=(owned_path,),
        protected_paths=(".github/**",),
        path_case_mode=PathCaseMode.INSENSITIVE,
    )
    effect = attempt_fixtures.prepared_effect(
        adapter,
        command,
        operation=EffectOperation.REPOSITORY_UPDATE,
        object_type=EffectObjectType.WORKTREE,
        event_type=JournalEventType.EFFECT_REQUESTED,
        ordinal=ordinal,
    )
    return command, effect


def _created_attempt(
    adapter: GitWorktreeAdapter,
    *,
    task_id: str,
    owned_path: str,
    operation_id: str,
    ordinal: int,
) -> AttemptWorktree:
    _, effect = _attempt_inputs(
        adapter,
        task_id=task_id,
        owned_path=owned_path,
        operation_id=operation_id,
        ordinal=ordinal,
    )
    created = adapter.create_attempt(effect)
    if created.value is None:
        raise AssertionError(created)
    return created.value


def _commit_attempt(
    attempt: AttemptWorktree,
    relative_path: str,
    content: str,
) -> str:
    path = Path(attempt.path)
    (path / relative_path).write_text(content, encoding="utf-8")
    attempt_fixtures.git(path, "add", "--", relative_path)
    attempt_fixtures.git(path, "commit", "-m", f"result {attempt.task_id}")
    return attempt_fixtures.git_text(path, "rev-parse", "HEAD")


def _stage_inputs(
    adapter: GitWorktreeAdapter,
    *,
    commit_result: bool,
) -> tuple[ResultValidation, object]:
    attempt = _created_attempt(
        adapter,
        task_id="TASK-001",
        owned_path="src/a.txt",
        operation_id="CREATE-PROCESS-EXIT",
        ordinal=1,
    )
    if commit_result:
        _commit_attempt(attempt, "src/a.txt", "process exit result\n")
    validation = adapter.validate_result(attempt, process_tree_terminated=True)
    if not validation.accepted:
        raise AssertionError(validation)
    command = adapter.plan_stage(validation, operation_id="STAGE-PROCESS-EXIT")
    effect = attempt_fixtures.prepared_effect(
        adapter,
        command,
        operation=EffectOperation.RESULT_STAGE,
        object_type=EffectObjectType.RESULT_BUNDLE,
        event_type=JournalEventType.EFFECT_REQUESTED,
        ordinal=2,
    )
    return validation, effect


def _prepared_promotion(
    adapter: GitWorktreeAdapter,
    expected: WorkspaceIdentity,
) -> tuple[PromotionService, PromotionPlan, object]:
    validation, stage_effect = _stage_inputs(adapter, commit_result=True)
    staged_effect = adapter.stage_result(stage_effect, validation)
    if staged_effect.value is None:
        raise AssertionError(staged_effect)
    staged = staged_effect.value
    service = PromotionService(adapter, attempt_fixtures.graph_index())
    plan = service.plan_next(
        (staged,),
        expected_target_sha=expected.base_commit_sha,
        operation_id="PROMOTE-PROCESS-EXIT",
        coordinator_epoch=1,
    )
    plan = service.bind_acceptance(
        plan,
        (attempt_fixtures.evidence(staged.manifest.identity, 51),),
    )
    effect = attempt_fixtures.prepared_effect(
        adapter,
        plan.command,
        operation=EffectOperation.RESULT_PROMOTION,
        object_type=EffectObjectType.GIT_REF,
        event_type=JournalEventType.PROMOTION_REQUESTED,
        ordinal=3,
    )
    return service, plan, effect


def _prepared_cleanup(
    adapter: GitWorktreeAdapter,
) -> tuple[AttemptWorktree, AttemptWorktree, CleanupService, CleanupPlan, object]:
    attempt = _created_attempt(
        adapter,
        task_id="TASK-001",
        owned_path="src/a.txt",
        operation_id="CREATE-CLEANUP-TARGET",
        ordinal=1,
    )
    head = _commit_attempt(attempt, "src/a.txt", "cleanup target\n")
    sibling = _created_attempt(
        adapter,
        task_id="TASK-002",
        owned_path="src/b.txt",
        operation_id="CREATE-CLEANUP-SIBLING",
        ordinal=2,
    )
    candidate = CleanupCandidate(
        attempt,
        head,
        (attempt_fixtures.evidence(attempt.identity, 71),),
        reconciliation_complete=True,
        process_tree_terminated=True,
        outcome_known=True,
    )
    service = CleanupService(
        adapter,
        available_bytes=lambda: 1_000_000,
        minimum_free_bytes=100,
        clock=lambda: attempt_fixtures.FIXED_TIME,
    )
    plan = service.plan(
        candidate,
        operation_id="CLEANUP-PROCESS-EXIT",
        coordinator_epoch=1,
    )
    effect = attempt_fixtures.prepared_effect(
        adapter,
        plan.command,
        operation=EffectOperation.CLEANUP,
        object_type=EffectObjectType.CLEANUP_ITEM,
        event_type=JournalEventType.CLEANUP_REQUESTED,
        ordinal=3,
    )
    return attempt, sibling, service, plan, effect


def crash_git_boundary(root: Path, point: str) -> None:
    if point not in GIT_BOUNDARY_POINTS:
        raise ValueError(f"unknown Git crash boundary: {point}")
    repository = root / "repository"
    attempts_root = root / "attempts"
    attempt_fixtures.initialize_repository(repository)
    attempts_root.mkdir()
    expected = capture_workspace_identity(repository, attempt_fixtures.SCOPES)
    marker_path = root / "git-crash.json"
    state_path = root / "git-operation.json"
    marker: dict[str, object] = {
        "base_head": expected.base_commit_sha,
        "crash_pid": os.getpid(),
        "crash_process_start_id": capture_process_start_id(),
        "initial_target_reflog_count": len(
            _target_reflog(repository, expected.target_full_ref)
        ),
        "initial_worktree_count": len(_registered_worktrees(repository)),
        "point": point,
    }
    state: dict[str, object] = {"workspace": expected.to_primitive()}
    _write_json(marker_path, marker)
    _write_json(state_path, state)

    def crash_at_boundary(observed: str, path: Path) -> None:
        if observed != point:
            return
        staged_ref = marker.get("staged_ref")
        marker.update(
            {
                "boundary_path": str(path.resolve()),
                "destination_exists": path.exists(),
                "staged_ref_exists": (
                    False
                    if type(staged_ref) is not str
                    else _ref_exists(repository, staged_ref)
                ),
                "target_head": attempt_fixtures.git_text(
                    repository,
                    "rev-parse",
                    "HEAD",
                ),
                "target_reflog_count": len(
                    _target_reflog(repository, expected.target_full_ref)
                ),
                "worktree_count": len(_registered_worktrees(repository)),
            }
        )
        if point in ATTEMPT_BOUNDARY_POINTS:
            marker["destination"] = str(path.resolve())
        _write_json(marker_path, marker)
        os._exit(CRASH_EXIT_CODE)

    adapter = _git_adapter(root, expected, crash_at_boundary)
    if point in ATTEMPT_BOUNDARY_POINTS:
        command, effect = _attempt_inputs(
            adapter,
            task_id="TASK-001",
            owned_path="src/a.txt",
            operation_id="CREATE-PROCESS-EXIT",
            ordinal=1,
        )
        marker["destination"] = str((attempts_root / command.directory_name).resolve())
        _write_json(marker_path, marker)
        adapter.create_attempt(effect)
    elif point in STAGE_BOUNDARY_POINTS:
        validation, effect = _stage_inputs(adapter, commit_result=True)
        marker.update(
            {
                "result_commit_sha": effect.command.result_commit_sha,
                "staged_ref": effect.command.staged_ref,
            }
        )
        _write_json(marker_path, marker)
        adapter.stage_result(effect, validation)
    elif point in PROMOTION_BOUNDARY_POINTS:
        service, plan, effect = _prepared_promotion(adapter, expected)
        state["promotion_plan"] = _promotion_plan_primitive(plan)
        marker.update(
            {
                "candidate_commit_sha": plan.command.candidate_commit_sha,
                "source_commit_sha": plan.command.source_commit_sha,
                "staged_ref": plan.command.staged_ref,
            }
        )
        _write_json(state_path, state)
        _write_json(marker_path, marker)
        service.apply(effect, plan)
    else:
        attempt, sibling, service, plan, effect = _prepared_cleanup(adapter)
        state["cleanup_plan"] = _cleanup_plan_primitive(plan)
        marker.update(
            {
                "cleanup_attempt_path": attempt.path,
                "sibling_head": attempt_fixtures.git_text(
                    Path(sibling.path),
                    "rev-parse",
                    "HEAD",
                ),
                "sibling_path": sibling.path,
            }
        )
        _write_json(state_path, state)
        _write_json(marker_path, marker)
        service.apply(effect, plan)
    raise AssertionError(f"Git failpoint was not reached: {point}")


def recover_git_boundary(root: Path, point: str, result_path: Path) -> None:
    marker = _mapping(json.loads((root / "git-crash.json").read_text(encoding="utf-8")))
    state = _mapping(
        json.loads((root / "git-operation.json").read_text(encoding="utf-8"))
    )
    if marker["point"] != point:
        raise AssertionError(
            f"recovery point {point!r} does not match {marker['point']!r}"
        )
    repository = root / "repository"
    expected = _workspace_identity(state["workspace"])
    adapter = _git_adapter(root, expected)
    result: dict[str, object] = {
        "base_head": marker["base_head"],
        "crash_pid": marker["crash_pid"],
        "point": point,
        "restart_pid": os.getpid(),
        "restart_process_start_id": capture_process_start_id(),
    }

    if point in ATTEMPT_BOUNDARY_POINTS:
        command, effect = _attempt_inputs(
            adapter,
            task_id="TASK-001",
            owned_path="src/a.txt",
            operation_id="CREATE-PROCESS-EXIT",
            ordinal=1,
        )
        observed = adapter.inspect_attempt(command)
        recovered = adapter.create_attempt(effect)
        replayed = adapter.create_attempt(effect)
        destination = root / "attempts" / command.directory_name
        result.update(
            {
                "destination": str(destination.resolve()),
                "destination_exists": destination.is_dir(),
                "exact_replay": recovered == replayed,
                "observed_disposition": observed.disposition.value,
                "recovered_disposition": recovered.disposition.value,
                "replayed_disposition": replayed.disposition.value,
            }
        )
    elif point in STAGE_BOUNDARY_POINTS:
        validation, effect = _stage_inputs(adapter, commit_result=False)
        observed = adapter.inspect_stage(effect.command, validation)
        recovered = adapter.stage_result(effect, validation)
        replayed = adapter.stage_result(effect, validation)
        staged_refs = attempt_fixtures.git_text(
            repository,
            "for-each-ref",
            "--format=%(refname)",
            "refs/wish-builder/staged",
        ).splitlines()
        result.update(
            {
                "exact_replay": recovered == replayed,
                "observed_disposition": observed.disposition.value,
                "recovered_disposition": recovered.disposition.value,
                "replayed_disposition": replayed.disposition.value,
                "staged_commit_sha": attempt_fixtures.git_text(
                    repository,
                    "show-ref",
                    "--verify",
                    "--hash",
                    effect.command.staged_ref,
                ),
                "staged_ref": effect.command.staged_ref,
                "staged_ref_count": len(staged_refs),
            }
        )
    elif point in PROMOTION_BOUNDARY_POINTS:
        plan = _promotion_plan(state["promotion_plan"])
        effect = attempt_fixtures.prepared_effect(
            adapter,
            plan.command,
            operation=EffectOperation.RESULT_PROMOTION,
            object_type=EffectObjectType.GIT_REF,
            event_type=JournalEventType.PROMOTION_REQUESTED,
            ordinal=3,
        )
        service = PromotionService(adapter, attempt_fixtures.graph_index())
        reconciled = service.reconcile(plan)
        recovered = service.apply(effect, plan)
        replayed = service.apply(effect, plan)
        result.update(
            {
                "candidate_parent_sha": attempt_fixtures.git_text(
                    repository,
                    "rev-parse",
                    f"{plan.command.candidate_commit_sha}^",
                ),
                "exact_replay": recovered == replayed,
                "reconciled_disposition": reconciled.disposition.value,
                "recovered_disposition": recovered.disposition.value,
                "replayed_disposition": replayed.disposition.value,
                "staged_source_sha": attempt_fixtures.git_text(
                    repository,
                    "show-ref",
                    "--verify",
                    "--hash",
                    plan.command.staged_ref,
                ),
            }
        )
    else:
        plan = _cleanup_plan(state["cleanup_plan"])
        effect = attempt_fixtures.prepared_effect(
            adapter,
            plan.command,
            operation=EffectOperation.CLEANUP,
            object_type=EffectObjectType.CLEANUP_ITEM,
            event_type=JournalEventType.CLEANUP_REQUESTED,
            ordinal=3,
        )
        service = CleanupService(
            adapter,
            available_bytes=lambda: 1_000_000,
            minimum_free_bytes=100,
            clock=lambda: attempt_fixtures.FIXED_TIME,
        )
        inspection = adapter.inspect_cleanup(plan.candidate)
        recovered = service.apply(effect, plan)
        replayed = service.apply(effect, plan)
        sibling_path = Path(marker["sibling_path"])
        result.update(
            {
                "cleanup_attempt_exists": Path(
                    cast(AttemptWorktree, plan.candidate.attempt).path
                ).exists(),
                "inspection_exists": inspection.exists,
                "recovered_disposition": recovered.disposition.value,
                "replayed_disposition": replayed.disposition.value,
                "sibling_exists": sibling_path.is_dir(),
                "sibling_head": attempt_fixtures.git_text(
                    sibling_path,
                    "rev-parse",
                    "HEAD",
                ),
            }
        )

    result.update(
        {
            "target_clean": not bool(
                attempt_fixtures.git_text(
                    repository,
                    "status",
                    "--porcelain=v1",
                )
            ),
            "target_head": attempt_fixtures.git_text(
                repository,
                "rev-parse",
                "HEAD",
            ),
            "target_reflog_count": len(
                _target_reflog(repository, expected.target_full_ref)
            ),
            "worktree_count": len(_registered_worktrees(repository)),
        }
    )
    _write_json(result_path, result)


def crash_git_attempt(root: Path) -> None:
    crash_git_boundary(root, "after_attempt_create")


def recover_git_attempt(root: Path, result_path: Path) -> None:
    recover_git_boundary(root, "after_attempt_create", result_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    crash_dispatch_parser = subparsers.add_parser("crash-dispatch")
    crash_dispatch_parser.add_argument("root", type=Path)
    crash_dispatch_parser.add_argument("point")
    recover_dispatch_parser = subparsers.add_parser("recover-dispatch")
    recover_dispatch_parser.add_argument("root", type=Path)
    recover_dispatch_parser.add_argument("result", type=Path)
    recover_dispatch_parser.add_argument("--corrupt-receipt", action="store_true")
    crash_git_parser = subparsers.add_parser("crash-git-attempt")
    crash_git_parser.add_argument("root", type=Path)
    recover_git_parser = subparsers.add_parser("recover-git-attempt")
    recover_git_parser.add_argument("root", type=Path)
    recover_git_parser.add_argument("result", type=Path)
    crash_git_boundary_parser = subparsers.add_parser("crash-git-boundary")
    crash_git_boundary_parser.add_argument("root", type=Path)
    crash_git_boundary_parser.add_argument("point")
    recover_git_boundary_parser = subparsers.add_parser("recover-git-boundary")
    recover_git_boundary_parser.add_argument("root", type=Path)
    recover_git_boundary_parser.add_argument("point")
    recover_git_boundary_parser.add_argument("result", type=Path)
    args = parser.parse_args()

    if args.command == "crash-dispatch":
        crash_dispatch(args.root, args.point)
    elif args.command == "recover-dispatch":
        recover_dispatch(
            args.root,
            args.result,
            corrupt_receipt=args.corrupt_receipt,
        )
    elif args.command == "crash-git-attempt":
        crash_git_attempt(args.root)
    elif args.command == "recover-git-attempt":
        recover_git_attempt(args.root, args.result)
    elif args.command == "crash-git-boundary":
        crash_git_boundary(args.root, args.point)
    else:
        recover_git_boundary(args.root, args.point, args.result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
