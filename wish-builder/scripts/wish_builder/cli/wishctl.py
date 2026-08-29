#!/usr/bin/env python3
"""Validate and inspect wish-builder execution manifests."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wish_builder.adapters.git_identity import (
    GitIdentityError,
    ProtectedControlRoot,
    WorkspaceIdentity,
    capture_workspace_identity,
)
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.adapters.trellis import (
    TrellisCoreGraphPort,
    TrellisGraphAdapterError,
    TrellisGraphImportError,
    TrellisImportSettings,
    import_trellis_snapshot,
)
from wish_builder.contracts import (
    DEFAULT_DECODE_LIMITS,
    ActorIdentity,
    ActorType,
    AdapterKind,
    BillingPosture,
    DecisionChoice,
    DecisionCommand,
    DecisionObservedPayload,
    DecisionRequest,
    DecisionRequestPayload,
    DispatchRecoveryPayload,
    EffectOperation,
    EffectRequestPayload,
    ExecutionBudgetPolicy,
    ExecutionIdentity,
    ExecutionManifest,
    ExecutionManifestModel,
    ExecutionManifestV2,
    GateApproval,
    JournalEvent,
    JournalEventType,
    OperationOutcome,
    PathCaseMode,
    RuntimeState,
    Severity,
    SourceChannel,
    WorkerProvider,
    decode_decision_request_bytes,
    decode_dispatch_recovery_payload_bytes,
    decode_journal_event_bytes,
    decode_manifest_bytes,
    decode_manifest_v2_bytes,
)
from wish_builder.kernel.gates import evaluate_decision
from wish_builder.kernel.graph_index import GraphIndex
from wish_builder.kernel.validation import (
    validate_manifest as validate_admitted_manifest,
)
from wish_builder.processes import (
    CoordinatorCursor,
    CoordinatorStatus,
    ForegroundCoordinator,
)
from wish_builder.processes.foreground import (
    ForegroundRunComponents,
    ForegroundRunService,
    ForegroundRunStatus,
)
from wish_builder.services.decisions import commit_decision
from wish_builder.services.checkpoints import CheckpointLoadStatus, CheckpointStore
from wish_builder.services.execution_admission import admit_execution_snapshot
from wish_builder.services.journal import GENESIS_HEAD, DurableJournal, JournalHead
from wish_builder.services.ports import PersistedEffectRequest, TrellisGraphSnapshot
from wish_builder.services.recovery import (
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)


CLI_HELP_WIDTH = 82
CLI_USAGE_WIDTH = 104


class _StableHelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog: str) -> None:
        # argparse otherwise derives wrapping from the caller's terminal width.
        super().__init__(prog, width=CLI_HELP_WIDTH, max_help_position=24)

    def _format_usage(self, *args: Any, **kwargs: Any) -> str:
        # Keep the top-level command list on one line across argparse versions
        # without widening the option descriptions below it.
        help_width = self._width
        self._width = CLI_USAGE_WIDTH
        try:
            return super()._format_usage(*args, **kwargs)
        finally:
            self._width = help_width


class _StableArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", _StableHelpFormatter)
        super().__init__(*args, **kwargs)

SCHEMA_VERSION = 1
REQUIREMENT_STATUSES = {"approved", "implemented", "deferred", "out_of_scope"}
TASK_STATUSES = {
    "proposed",
    "approved",
    "ready",
    "dispatched",
    "pr_open",
    "merged",
    "verified",
    "archived",
    "blocked",
    "failed",
}
DONE_STATUSES = {"merged", "verified", "archived"}
ACTIVE_STATUSES = {"dispatched", "pr_open"}
DISPATCHABLE_STATUSES = {"approved", "ready"}
RISK_LEVELS = {"low", "medium", "high"}
ID_RE = re.compile(r"^[A-Z][A-Z0-9]*-[A-Z0-9][A-Z0-9-]*$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_IMPORT_SETTINGS_BYTES = 1_048_576
MAX_DECISION_REQUEST_BYTES = 1_048_576
MAX_RECOVERY_PROOF_BYTES = 1_048_576
_SEGMENT_NAME_RE = re.compile(r"^segment-([0-9]{8})\.jsonl$")

_IMPORT_SETTINGS_FIELDS = frozenset(
    {
        "base_branch",
        "capability_digest",
        "execution_budget",
        "export_version",
        "gate_a",
        "goal",
        "imported_at",
        "launch_profile_digest",
        "lease_clock_skew_seconds",
        "lease_ttl_seconds",
        "max_concurrency",
        "observed_at",
        "parent_task_id",
        "path_case_mode",
        "policy_digest",
        "protected_paths",
        "provider",
        "revision",
        "run_id",
        "trellis_version",
    }
)
_GATE_APPROVAL_FIELDS = frozenset(
    {"approved_at", "approved_by", "artifact_hash"}
)
_EXECUTION_BUDGET_FIELDS = frozenset(
    {
        "attempt_deadline_seconds",
        "billing_posture",
        "max_attempts_per_run",
        "max_attempts_per_task",
        "max_concurrent_workers",
        "max_output_bytes",
        "max_retained_evidence_bytes",
        "total_worker_seconds",
    }
)


class ManifestError(Exception):
    """Raised for an unreadable manifest."""


class DecisionCliError(Exception):
    """Raised when a direct CLI decision cannot be admitted safely."""


class RecoveryCliError(Exception):
    """Raised when a direct CLI dispatch recovery cannot be admitted safely."""


class RunCliError(Exception):
    """Raised when a foreground execution request is not structurally valid."""


class _RecoveryOnlyTaskPort:
    """Constructor guard for a command that must never execute worker effects."""

    @property
    def adapter_kind(self) -> AdapterKind:
        return AdapterKind.TASK

    @property
    def operations(self) -> frozenset[EffectOperation]:
        return frozenset({EffectOperation.WORKER_DISPATCH})

    def apply(self, request: PersistedEffectRequest) -> OperationOutcome:
        del request
        raise RecoveryCliError("resume cannot execute worker effects")

    def lookup(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
    ) -> OperationOutcome:
        del identity, operation
        raise RecoveryCliError("resume cannot reconcile worker effects")


class _DuplicateJsonKey(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class _InvalidJsonConstant(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise _InvalidJsonConstant(value)


def _settings_error(code: str, path: str, message: str) -> ManifestError:
    return ManifestError(f"settings rejected: {code} at {path}: {message}")


def _closed_settings_object(
    value: object,
    *,
    path: str,
    fields: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        raise _settings_error(
            "settings.wrong_container_type",
            path,
            "expected a JSON object",
        )
    unknown = sorted(set(value) - fields)
    if unknown:
        raise _settings_error(
            "settings.unknown_field",
            f"{path}/{unknown[0]}",
            "unknown settings field",
        )
    missing = sorted(fields - set(value))
    if missing:
        raise _settings_error(
            "settings.missing_field",
            f"{path}/{missing[0]}",
            "required settings field is missing",
        )
    return value


def _decode_import_settings(raw: bytes, source: Path) -> dict[str, object]:
    if len(raw) > MAX_IMPORT_SETTINGS_BYTES:
        raise _settings_error(
            "settings.byte_limit_exceeded",
            "$",
            f"{source} exceeds {MAX_IMPORT_SETTINGS_BYTES} bytes",
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _settings_error(
            "json.invalid_utf8",
            "$",
            f"{source} is not valid UTF-8",
        ) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJsonKey as exc:
        raise _settings_error(
            "json.duplicate_key",
            "$",
            f"duplicate object key: {exc.key}",
        ) from exc
    except _InvalidJsonConstant as exc:
        raise _settings_error(
            "json.invalid_constant",
            "$",
            f"non-standard JSON constant: {exc}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise _settings_error(
            "json.invalid_syntax",
            "$",
            f"{source}:{exc.lineno}:{exc.colno}: {exc.msg}",
        ) from exc
    return _closed_settings_object(
        value,
        path="$",
        fields=_IMPORT_SETTINGS_FIELDS,
    )


def _load_trellis_import(
    snapshot_path: str | Path,
    settings_path: str | Path,
) -> tuple[TrellisGraphSnapshot, TrellisImportSettings]:
    snapshot_source = Path(snapshot_path)
    settings_source = Path(settings_path)
    try:
        snapshot_bytes = snapshot_source.read_bytes()
    except FileNotFoundError as exc:
        raise ManifestError(f"snapshot not found: {snapshot_source}") from exc
    try:
        settings_bytes = settings_source.read_bytes()
    except FileNotFoundError as exc:
        raise ManifestError(f"settings not found: {settings_source}") from exc

    root = _decode_import_settings(settings_bytes, settings_source)
    gate = _closed_settings_object(
        root["gate_a"],
        path="$/gate_a",
        fields=_GATE_APPROVAL_FIELDS,
    )
    budget = _closed_settings_object(
        root["execution_budget"],
        path="$/execution_budget",
        fields=_EXECUTION_BUDGET_FIELDS,
    )
    protected_paths = root["protected_paths"]
    if type(protected_paths) is not list or not all(
        type(item) is str for item in protected_paths
    ):
        raise _settings_error(
            "settings.wrong_container_type",
            "$/protected_paths",
            "expected an array of strings",
        )

    try:
        gate_a = GateApproval(
            approved_by=gate["approved_by"],
            approved_at=gate["approved_at"],
            artifact_hash=gate["artifact_hash"],
        )
        execution_budget = ExecutionBudgetPolicy(
            max_attempts_per_task=budget["max_attempts_per_task"],
            max_attempts_per_run=budget["max_attempts_per_run"],
            attempt_deadline_seconds=budget["attempt_deadline_seconds"],
            total_worker_seconds=budget["total_worker_seconds"],
            max_output_bytes=budget["max_output_bytes"],
            max_retained_evidence_bytes=budget["max_retained_evidence_bytes"],
            max_concurrent_workers=budget["max_concurrent_workers"],
            billing_posture=BillingPosture(budget["billing_posture"]),
        )
        settings = TrellisImportSettings(
            run_id=root["run_id"],
            goal=root["goal"],
            base_branch=root["base_branch"],
            imported_at=root["imported_at"],
            gate_a=gate_a,
            provider=WorkerProvider(root["provider"]),
            capability_digest=root["capability_digest"],
            launch_profile_digest=root["launch_profile_digest"],
            policy_digest=root["policy_digest"],
            execution_budget=execution_budget,
            max_concurrency=root["max_concurrency"],
            lease_ttl_seconds=root["lease_ttl_seconds"],
            lease_clock_skew_seconds=root["lease_clock_skew_seconds"],
            path_case_mode=PathCaseMode(root["path_case_mode"]),
            protected_paths=tuple(protected_paths),
        )
        snapshot = TrellisGraphSnapshot(
            export_version=root["export_version"],
            trellis_version=root["trellis_version"],
            parent_task_id=root["parent_task_id"],
            revision=root["revision"],
            observed_at=root["observed_at"],
            snapshot_bytes=snapshot_bytes,
            source_sha256="sha256:" + hashlib.sha256(snapshot_bytes).hexdigest(),
            complete=True,
        )
    except (TypeError, ValueError) as exc:
        raise _settings_error(
            "settings.invalid_value",
            "$",
            str(exc),
        ) from exc
    return snapshot, settings


def _atomic_write_bytes(path: str | Path, payload: bytes, *, force: bool) -> None:
    target = Path(path)
    if target.exists() and not force:
        raise ManifestError(
            f"output exists: {target}; pass --force to replace it"
        )

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if target.exists() and not force:
            raise ManifestError(
                f"output exists: {target}; pass --force to replace it"
            )
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _write_stdout_bytes(payload: bytes) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(payload)
        stream.flush()
        return
    sys.stdout.write(payload.decode("utf-8", errors="strict"))


def _run_trellis_import(args: argparse.Namespace) -> int:
    snapshot, settings = _load_trellis_import(args.snapshot, args.settings)
    result = import_trellis_snapshot(
        snapshot,
        settings,
        approved_graph_digest=args.approved_graph_digest,
    )
    candidate_bytes = result.manifest.canonical_json_bytes()
    decoded = decode_manifest_v2_bytes(candidate_bytes)
    if not decoded.ok or decoded.value is None:
        raise ManifestError(
            "generated manifest rejected:\n"
            + decoded.report.render_text().rstrip()
        )
    manifest_bytes = decoded.value.canonical_json_bytes()
    summary: dict[str, object] = {
        "gate_b_invalidated": result.gate_b_invalidated,
        "manifest_digest": decoded.value.canonical_sha256(),
        "trellis_graph_digest": result.trellis_graph_digest,
    }
    if args.output:
        _atomic_write_bytes(args.output, manifest_bytes, force=args.force)
        summary["output"] = str(Path(args.output))
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        _write_stdout_bytes(manifest_bytes)
        print(
            json.dumps(summary, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
        )
    return 0


def _run_trellis_snapshot(args: argparse.Namespace) -> int:
    checkout_root = Path(args.checkout_root).expanduser().absolute()
    core_root = _required_trellis_path(
        args.core_root,
        "WISH_BUILDER_TRELLIS_CORE_ROOT",
        directory=True,
    )
    core_archive = _required_trellis_path(
        args.core_archive,
        "WISH_BUILDER_TRELLIS_CORE_ARCHIVE",
        directory=False,
    )
    node_value = args.node or shutil.which("node")
    if not node_value:
        raise ManifestError("Node.js is required to read official Trellis task records")
    node = Path(node_value).expanduser().absolute()
    bridge = (
        Path(__file__).resolve().parents[1]
        / "bridges"
        / "trellis_core"
        / "bridge.mjs"
    )
    port = TrellisCoreGraphPort(
        bridge_command=(str(node), str(bridge)),
        checkout_root=checkout_root,
        working_directory=checkout_root,
        environment={
            "WISH_BUILDER_TRELLIS_CORE_ROOT": str(core_root),
            "WISH_BUILDER_TRELLIS_CORE_ARCHIVE": str(core_archive),
        },
    )
    snapshot = port.export_snapshot(args.parent_task_id)
    summary: dict[str, object] = {
        "byte_length": snapshot.byte_length,
        "export_version": snapshot.export_version,
        "observed_at": snapshot.observed_at,
        "parent_task_id": snapshot.parent_task_id,
        "revision": snapshot.revision,
        "source_sha256": snapshot.source_sha256,
        "trellis_version": snapshot.trellis_version,
    }
    if args.output:
        _atomic_write_bytes(args.output, snapshot.snapshot_bytes, force=args.force)
        summary["output"] = str(Path(args.output))
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        _write_stdout_bytes(snapshot.snapshot_bytes)
        print(
            json.dumps(summary, sort_keys=True, separators=(",", ":")),
            file=sys.stderr,
        )
    return 0


def _required_trellis_path(
    argument: str | None,
    environment_name: str,
    *,
    directory: bool,
) -> Path:
    value = argument or os.environ.get(environment_name)
    if not value:
        option = "--core-root" if directory else "--core-archive"
        raise ManifestError(
            f"{option} or {environment_name} is required for verified Trellis access"
        )
    path = Path(value).expanduser().absolute()
    valid = path.is_dir() if directory else path.is_file()
    if not valid or path.is_symlink():
        kind = "directory" if directory else "file"
        raise ManifestError(f"{environment_name} must identify an existing non-link {kind}")
    return path.resolve(strict=True)


def _run_foreground(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    if type(manifest) is not ExecutionManifestV2:
        raise RunCliError("run requires an execution manifest v2")

    runtime_root = (
        None
        if args.runtime_root is None
        else Path(args.runtime_root).expanduser().absolute()
    )
    workspace_root = Path(args.workspace_root).expanduser().absolute()
    provider_sdk_root = None
    if args.provider_sdk_root is not None:
        selected_sdk_root = Path(args.provider_sdk_root).expanduser()
        if not selected_sdk_root.is_absolute():
            raise RunCliError("--provider-sdk-root must be an absolute path")
        provider_sdk_root = selected_sdk_root.resolve(strict=False)

    def components_factory() -> ForegroundRunComponents:
        kwargs: dict[str, object] = {
            "runtime_root": runtime_root,
            "workspace_root": workspace_root,
        }
        if provider_sdk_root is not None:
            kwargs["provider_sdk_root"] = provider_sdk_root
        return _build_production_components(manifest, **kwargs)

    result = ForegroundRunService(
        manifest,
        components_factory=components_factory,
    ).run()
    print(
        json.dumps(
            {
                "backend_admission_reason": result.backend_admission.reason.value,
                "batch_count": result.batch_count,
                "completed_task_ids": list(result.completed_task_ids),
                "provider": manifest.provider.value,
                "reason": result.reason.value,
                "run_id": manifest.run_id,
                "stage": result.stage.value,
                "status": result.status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if result.status is ForegroundRunStatus.COMPLETED else 1


def _build_production_components(
    manifest: ExecutionManifestV2,
    *,
    runtime_root: Path | None,
    workspace_root: Path,
    provider_sdk_root: Path | None = None,
) -> ForegroundRunComponents:
    """Enter the effectful production composition only after backend admission."""

    from wish_builder.processes.production import ProductionForegroundRunComponents

    kwargs: dict[str, object] = {
        "runtime_root": runtime_root,
        "workspace_root": workspace_root,
    }
    if provider_sdk_root is not None:
        kwargs["provider_sdk_root"] = provider_sdk_root
    return ProductionForegroundRunComponents.from_runtime_inputs(manifest, **kwargs)


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_token(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._:@/-]", "-", value.strip())[:128]
    normalized = normalized.strip("-./")
    return normalized or fallback


def _control_root_path(journal_root: str | Path) -> Path:
    return Path(journal_root).expanduser().absolute().parent


def _control_root_validator(control_root: ProtectedControlRoot):
    return lambda: control_root.revalidate().ok


def _capture_cli_workspace(
    repository: str | Path,
    scopes: Iterable[str],
    *,
    label: str,
    error_type: type[DecisionCliError] | type[RecoveryCliError],
) -> WorkspaceIdentity:
    try:
        return capture_workspace_identity(repository, scopes)
    except GitIdentityError as exc:
        raise error_type(f"{label} identity capture failed: {exc.reason}") from exc


def _decision_workspace_scopes(args: argparse.Namespace) -> tuple[str, ...]:
    scopes = tuple(args.workspace_scope or ())
    if not scopes:
        raise DecisionCliError(
            "decision workspace identity requires at least one --workspace-scope"
        )
    return scopes


def _manifest_workspace_scopes(manifest: ExecutionManifestV2) -> tuple[str, ...]:
    scopes = {
        *manifest.protected_paths,
        *(
            path
            for task in manifest.tasks
            for path in (*task.owned_paths, *task.allowed_auxiliary_paths)
        ),
    }
    if not scopes:
        raise RecoveryCliError("manifest has no workspace identity scopes")
    return tuple(sorted(scopes))


def _workspace_matches_lease(
    observed: WorkspaceIdentity,
    lease_owner: object,
) -> bool:
    return (
        getattr(lease_owner, "local_repository_id", None)
        == observed.local_repository_id
        and getattr(lease_owner, "local_worktree_id", None)
        == observed.local_worktree_id
        and getattr(lease_owner, "workspace_hash", None) == observed.workspace_hash
    )


def _load_decision_request(path: str | Path) -> DecisionRequest:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except FileNotFoundError as exc:
        raise DecisionCliError(f"decision request not found: {source}") from exc
    if len(raw) > MAX_DECISION_REQUEST_BYTES:
        raise DecisionCliError(
            f"decision request exceeds {MAX_DECISION_REQUEST_BYTES} bytes: {source}"
        )
    decoded = decode_decision_request_bytes(raw)
    if not decoded.ok or decoded.value is None:
        raise DecisionCliError(
            "decision request rejected:\n" + decoded.report.render_text().rstrip()
        )
    return decoded.value


def _read_verified_journal(journal_root: str | Path) -> tuple[JournalEvent, ...]:
    root = Path(journal_root)
    segments = root / "segments"
    try:
        entries = tuple(segments.iterdir())
    except FileNotFoundError as exc:
        raise DecisionCliError(f"journal segments not found: {segments}") from exc
    numbered: list[tuple[int, Path]] = []
    for entry in entries:
        match = _SEGMENT_NAME_RE.fullmatch(entry.name)
        if match is not None:
            numbered.append((int(match.group(1)), entry))
    numbered.sort(key=lambda item: item[0])
    if not numbered or [item[0] for item in numbered] != list(
        range(1, numbered[-1][0] + 1)
    ):
        raise DecisionCliError("journal segment layout is missing or non-contiguous")

    events: list[JournalEvent] = []
    head = GENESIS_HEAD
    run_id: str | None = None
    for _, segment in numbered:
        try:
            raw_lines = segment.read_bytes().splitlines(keepends=True)
        except OSError as exc:
            raise DecisionCliError(f"journal segment unreadable: {segment}") from exc
        if not raw_lines:
            raise DecisionCliError(f"journal segment is empty: {segment}")
        for raw in raw_lines:
            if not raw.endswith((b"\n", b"\r")):
                raise DecisionCliError(f"journal has an incomplete final frame: {segment}")
            decoded = decode_journal_event_bytes(raw)
            if not decoded.ok or decoded.value is None:
                raise DecisionCliError(
                    "journal event rejected:\n"
                    + decoded.report.render_text().rstrip()
                )
            event = decoded.value
            if (
                event.sequence != head.sequence + 1
                or event.previous_event_hash != head.event_hash
            ):
                raise DecisionCliError("journal hash chain or sequence is invalid")
            if run_id is None:
                run_id = event.identity.run_id
            elif event.identity.run_id != run_id:
                raise DecisionCliError("journal contains multiple run identities")
            events.append(event)
            head = JournalHead(event.sequence, event.event_hash)
    return tuple(events)


def _decision_context(
    request: DecisionRequest,
    events: tuple[JournalEvent, ...],
) -> tuple[JournalEvent, JournalEvent | None, JournalHead]:
    requests = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.DECISION_REQUESTED
        and type(event.payload) is DecisionRequestPayload
        and event.payload.request.command.request_id == request.command.request_id
    )
    if len(requests) != 1 or requests[0].payload.request != request:
        raise DecisionCliError("decision request does not exactly match one Journal event")
    request_event = requests[0]
    if request.command.expected_sequence != request_event.sequence:
        raise DecisionCliError("decision request sequence does not match its Journal event")

    observed = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.DECISION_OBSERVED
        and type(event.payload) is DecisionObservedPayload
        and event.payload.observation.decision.request.command.request_id
        == request.command.request_id
    )
    if len(observed) > 1:
        raise DecisionCliError("Journal contains conflicting decision observations")
    current = events[-1]
    current_head = JournalHead(current.sequence, current.event_hash)
    return request_event, observed[0] if observed else None, current_head


def _run_decide(args: argparse.Namespace) -> int:
    request = _load_decision_request(args.request)
    if args.workspace_hash != request.workspace_hash:
        print("ERROR: decision rejected: workspace_drift", file=sys.stderr)
        return 1
    workspace = _capture_cli_workspace(
        args.workspace_root,
        _decision_workspace_scopes(args),
        label="decision workspace",
        error_type=DecisionCliError,
    )
    if workspace.workspace_hash != request.workspace_hash:
        print("ERROR: decision rejected: workspace_drift", file=sys.stderr)
        return 1
    events = _read_verified_journal(args.journal_root)
    request_event, observed_event, current_head = _decision_context(request, events)
    try:
        choice = DecisionChoice(args.choice)
    except ValueError as exc:
        raise DecisionCliError(f"unsupported decision choice: {args.choice}") from exc
    if args.actor_id != request.expected_actor_id:
        print("ERROR: decision rejected: actor_mismatch", file=sys.stderr)
        return 1
    if choice not in request.options:
        print("ERROR: decision rejected: invalid_choice", file=sys.stderr)
        return 1

    observed = (
        None
        if observed_event is None
        else observed_event.payload.observation
    )
    if observed is not None and (
        observed.decision.choice is choice
        and observed.decision.actor.actor_id == args.actor_id
    ):
        command = observed.decision
        prior = events[observed_event.sequence - 2]
        expected_head = JournalHead(prior.sequence, prior.event_hash)
    else:
        host_id = _safe_token(args.host_id or platform.node(), fallback="localhost")
        try:
            command = DecisionCommand(
                decision_id=(
                    args.decision_id
                    or f"DECISION-{request.command.request_id}-{choice.value.upper()}"
                ),
                request=request,
                choice=choice,
                actor=ActorIdentity(
                    ActorType.HUMAN,
                    args.actor_id,
                    host_id,
                    os.getpid(),
                    f"cli-start-{time.time_ns()}",
                ),
                source_channel=SourceChannel.DIRECT_CLI,
                decided_at=_utc_now(),
            )
        except (TypeError, ValueError) as exc:
            raise DecisionCliError(f"decision command rejected: {exc}") from exc
        expected_head = current_head

    try:
        evaluation = evaluate_decision(
            request,
            command,
            current_sequence=current_head.sequence,
            current_workspace_hash=workspace.workspace_hash,
            observed=observed,
        )
    except (TypeError, ValueError) as exc:
        raise DecisionCliError(f"decision admission rejected: {exc}") from exc
    if not evaluation.accepted:
        print(f"ERROR: decision rejected: {evaluation.reason.value}", file=sys.stderr)
        return 1
    confirmed = _capture_cli_workspace(
        args.workspace_root,
        workspace.scopes,
        label="decision workspace",
        error_type=DecisionCliError,
    )
    if confirmed.workspace_hash != workspace.workspace_hash:
        print("ERROR: decision rejected: workspace_drift", file=sys.stderr)
        return 1
    try:
        with ProtectedControlRoot.open(_control_root_path(args.journal_root)) as control_root:
            result = commit_decision(
                evaluation,
                DurableJournal(
                    request_event.identity.run_id,
                    FilesystemJournalStorage(
                        args.journal_root,
                        request_event.identity.run_id,
                        control_root=control_root,
                    ),
                ),
                expected_head=expected_head,
                identity=ExecutionIdentity(
                    request_event.identity.run_id,
                    request_event.identity.coordinator_epoch,
                ),
                event_id=f"EVENT-{command.decision_id}",
            )
    except GitIdentityError as exc:
        raise DecisionCliError(f"control root identity failed: {exc.reason}") from exc
    if not result.durable:
        print(
            f"ERROR: decision rejected: {result.evaluation.reason.value}",
            file=sys.stderr,
        )
        return 1
    assert result.event is not None
    print(
        json.dumps(
            {
                "decision_id": command.decision_id,
                "event_hash": result.event.event_hash,
                "event_sequence": result.event.sequence,
                "idempotent": result.evaluation.idempotent,
                "reason": result.evaluation.reason.value,
                "run_id": request_event.identity.run_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _load_dispatch_recovery_proof(
    path: str | Path,
) -> DispatchRecoveryPayload:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except FileNotFoundError as exc:
        raise RecoveryCliError(f"recovery proof not found: {source}") from exc
    if len(raw) > MAX_RECOVERY_PROOF_BYTES:
        raise RecoveryCliError(
            f"recovery proof exceeds {MAX_RECOVERY_PROOF_BYTES} bytes: {source}"
        )
    decoded = decode_dispatch_recovery_payload_bytes(raw)
    if not decoded.ok or decoded.value is None:
        raise RecoveryCliError(
            "recovery proof rejected:\n" + decoded.report.render_text().rstrip()
        )
    return decoded.value


def _matching_dispatch_request(
    events: tuple[JournalEvent, ...],
    proof: DispatchRecoveryPayload,
) -> JournalEvent:
    matches = tuple(
        event
        for event in events
        if event.event_type is JournalEventType.DISPATCH_REQUESTED
        and type(event.payload) is EffectRequestPayload
        and event.payload.operation is EffectOperation.WORKER_DISPATCH
        and event.identity == proof.subject_identity
        and event.event_id == proof.request_event_id
        and event.sequence == proof.request_sequence
        and event.event_hash == proof.request_event_hash
    )
    if len(matches) != 1:
        raise RecoveryCliError(
            "recovery proof does not identify exactly one dispatch request"
        )
    return matches[0]


def _authority_now() -> datetime:
    return datetime.now(UTC)


def _run_resume(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    if type(manifest) is not ExecutionManifestV2:
        raise RecoveryCliError("resume requires an execution manifest v2")
    proof = _load_dispatch_recovery_proof(args.proof)
    try:
        control_root = ProtectedControlRoot.open(_control_root_path(args.journal_root))
    except GitIdentityError as exc:
        raise RecoveryCliError(f"control root identity failed: {exc.reason}") from exc
    with control_root:
        events = _read_verified_journal(args.journal_root)
        initial_epoch = events[0].identity.coordinator_epoch
        try:
            recovered = recover_coordinator_lease(
                args.journal_root,
                manifest,
                coordinator_epoch=initial_epoch,
                repair_derived=False,
                control_root_validator=_control_root_validator(control_root),
            )
        except (OSError, TypeError, ValueError) as exc:
            raise RecoveryCliError(f"Journal recovery failed: {exc}") from exc
        if recovered.status is not LeaseRecoveryStatus.RECOVERED:
            detail = "unknown recovery fault"
            if recovered.fault is not None:
                detail = f"{recovered.fault.code.value}: {recovered.fault.detail}"
            raise RecoveryCliError(f"Journal recovery blocked: {detail}")
        observed_head = JournalHead(events[-1].sequence, events[-1].event_hash)
        if observed_head != recovered.replay.head:
            raise RecoveryCliError("Journal changed during recovery; retry resume")
        lease_state = recovered.lease_state
        if lease_state is None or not lease_state.active or lease_state.lease is None:
            raise RecoveryCliError("resume requires one active coordinator lease")
        lease = lease_state.lease
        if lease.manifest_digest != manifest.canonical_sha256():
            raise RecoveryCliError(
                "active lease manifest digest does not match the manifest"
            )
        if lease.owner.control_root_id != control_root.expected.identity_hash:
            raise RecoveryCliError("control_root_drift")
        workspace = _capture_cli_workspace(
            args.workspace_root,
            _manifest_workspace_scopes(manifest),
            label="resume workspace",
            error_type=RecoveryCliError,
        )
        if not _workspace_matches_lease(workspace, lease.owner):
            raise RecoveryCliError("workspace_drift")
        request = _matching_dispatch_request(events, proof)
        confirmed = _capture_cli_workspace(
            args.workspace_root,
            workspace.scopes,
            label="resume workspace",
            error_type=RecoveryCliError,
        )
        if confirmed.workspace_hash != workspace.workspace_hash:
            raise RecoveryCliError("workspace_drift")
        if not control_root.revalidate().ok:
            raise RecoveryCliError("control_root_drift")
        journal = DurableJournal(
            manifest.run_id,
            FilesystemJournalStorage(
                args.journal_root,
                manifest.run_id,
                authority_clock=_authority_now,
                control_root=control_root,
            ),
        )
        coordinator = ForegroundCoordinator(
            manifest,
            CoordinatorCursor(
                recovered.replay.snapshot,
                recovered.replay.graph_index,
                lease_state,
                recovered.dispatch_recoveries,
            ),
            journal,
            _RecoveryOnlyTaskPort(),
            coordinator_id=lease.coordinator_id,
            owner=lease.owner,
            fencing_token=lease.fencing_token,
            authority_clock=_authority_now,
        )
        result = coordinator.resume_unknown_dispatch(request, proof)
    if result.status is not CoordinatorStatus.PROGRESSED:
        print(
            f"ERROR: dispatch recovery rejected: {result.reason.value}",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "event_hash": result.cursor.head.event_hash,
                "event_sequence": result.cursor.head.sequence,
                "events": [
                    {
                        "event_hash": event.event_hash,
                        "event_id": event.event_id,
                        "event_sequence": event.sequence,
                        "event_type": event.event_type.value,
                    }
                    for event in result.events
                ],
                "idempotent": not result.events,
                "recovery_id": proof.recovery_id,
                "run_id": manifest.run_id,
                "status": result.status.value,
                "task_id": proof.subject_identity.task_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _manifest_schema_version(raw: bytes) -> int | None:
    """Probe only enough bounded JSON to choose the strict manifest decoder."""

    if len(raw) > DEFAULT_DECODE_LIMITS.max_bytes:
        return None
    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _InvalidJsonConstant,
        RecursionError,
        ValueError,
    ):
        return None
    if type(value) is not dict:
        return None
    schema_version = value.get("schema_version")
    return schema_version if type(schema_version) is int else None


def load_manifest(path: str | Path) -> ExecutionManifestModel:
    manifest_path = Path(path)
    try:
        raw = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {manifest_path}") from exc
    decoder = (
        decode_manifest_v2_bytes
        if _manifest_schema_version(raw) == 2
        else decode_manifest_bytes
    )
    decoded = decoder(raw)
    if not decoded.ok or decoded.value is None:
        raise ManifestError(
            f"manifest rejected: {manifest_path}\n{decoded.report.render_text().rstrip()}"
        )
    return decoded.value


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_id(value: Any) -> bool:
    if isinstance(value, int):
        return value > 0
    return _nonempty_string(value)


def _string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_nonempty_string(item) for item in value)
    )


def _gate_is_approved(manifest: dict[str, Any], gate_name: str) -> bool:
    gate = manifest.get("approved", {}).get(gate_name, {})
    return (
        isinstance(gate, dict)
        and _nonempty_string(gate.get("approved_by"))
        and _nonempty_string(gate.get("approved_at"))
        and isinstance(gate.get("artifact_hash"), str)
        and bool(HASH_RE.match(gate["artifact_hash"]))
    )


def _normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _static_prefix(pattern: str) -> str:
    normalized = _normalize_path(pattern)
    glob_positions = [
        position for token in "*?[" if (position := normalized.find(token)) >= 0
    ]
    if glob_positions:
        normalized = normalized[: min(glob_positions)]
    return normalized.rstrip("/")


def _contains_path(prefix: str, path: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def patterns_overlap(left: str, right: str) -> bool:
    """Conservatively detect whether two repository path patterns may overlap."""
    left_normalized = _normalize_path(left).casefold()
    right_normalized = _normalize_path(right).casefold()
    if left_normalized == right_normalized:
        return True

    left_prefix = _static_prefix(left_normalized)
    right_prefix = _static_prefix(right_normalized)
    if not left_prefix or not right_prefix:
        return True
    return _contains_path(left_prefix, right_prefix) or _contains_path(
        right_prefix, left_prefix
    )


def path_matches(path: str, pattern: str) -> bool:
    normalized_path = _normalize_path(path).casefold()
    normalized_pattern = _normalize_path(pattern).casefold()
    if fnmatch.fnmatchcase(normalized_path, normalized_pattern):
        return True
    has_glob = any(token in normalized_pattern for token in "*?[")
    if normalized_pattern.endswith("/**"):
        prefix = normalized_pattern[:-3].rstrip("/")
        return bool(prefix) and _contains_path(prefix, normalized_path)
    if has_glob:
        return False
    return _contains_path(normalized_pattern, normalized_path)


def _task_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = manifest.get("tasks", [])
    if not isinstance(tasks, list):
        return {}
    return {
        task["id"]: task
        for task in tasks
        if isinstance(task, dict) and _nonempty_string(task.get("id"))
    }


def _depends_on(
    task_id: str,
    dependency_id: str,
    tasks: dict[str, dict[str, Any]],
    seen: set[str] | None = None,
) -> bool:
    if seen is None:
        seen = set()
    if task_id in seen or task_id not in tasks:
        return False
    seen.add(task_id)
    dependencies = tasks[task_id].get("depends_on", [])
    if dependency_id in dependencies:
        return True
    return any(
        _depends_on(parent, dependency_id, tasks, seen.copy())
        for parent in dependencies
        if parent in tasks
    )


def _find_cycles(tasks: dict[str, dict[str, Any]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(task_id: str) -> None:
        state[task_id] = 1
        stack.append(task_id)
        for dependency in tasks[task_id].get("depends_on", []):
            if dependency not in tasks:
                continue
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                cycle = stack[start:] + [dependency]
                if cycle not in cycles:
                    cycles.append(cycle)
        stack.pop()
        state[task_id] = 2

    for task_id in tasks:
        if state.get(task_id, 0) == 0:
            visit(task_id)
    return cycles


def _max_depth(tasks: dict[str, dict[str, Any]]) -> int:
    memo: dict[str, int] = {}

    def depth(task_id: str, visiting: set[str]) -> int:
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting:
            return 0
        dependencies = [
            dependency
            for dependency in tasks[task_id].get("depends_on", [])
            if dependency in tasks
        ]
        result = 0 if not dependencies else 1 + max(
            depth(dependency, visiting | {task_id}) for dependency in dependencies
        )
        memo[task_id] = result
        return result

    return max((depth(task_id, set()) for task_id in tasks), default=0)


def validate_manifest(
    manifest: dict[str, Any], stage: str = "planning"
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION}, got {manifest.get('schema_version')!r}"
        )
    for field in ("run_id", "goal", "base_branch"):
        if not _nonempty_string(manifest.get(field)):
            errors.append(f"{field} must be a non-empty string")

    max_concurrency = manifest.get("max_concurrency", 3)
    if not isinstance(max_concurrency, int) or max_concurrency < 1:
        errors.append("max_concurrency must be a positive integer")

    protected_paths = manifest.get("protected_paths", [])
    if not isinstance(protected_paths, list) or not all(
        _nonempty_string(path) for path in protected_paths
    ):
        errors.append("protected_paths must be a list of non-empty strings")

    requirements = manifest.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append("requirements must be a non-empty list")
        requirements = []

    requirement_ids: set[str] = set()
    active_requirement_ids: set[str] = set()
    for index, requirement in enumerate(requirements):
        label = f"requirements[{index}]"
        if not isinstance(requirement, dict):
            errors.append(f"{label} must be an object")
            continue
        requirement_id = requirement.get("id")
        if not _nonempty_string(requirement_id) or not ID_RE.match(requirement_id):
            errors.append(f"{label}.id must be a stable uppercase ID such as REQ-001")
            continue
        if requirement_id in requirement_ids:
            errors.append(f"duplicate requirement id: {requirement_id}")
        requirement_ids.add(requirement_id)
        if not _nonempty_string(requirement.get("text")):
            errors.append(f"{requirement_id}.text must be non-empty")
        status = requirement.get("status")
        if status not in REQUIREMENT_STATUSES:
            errors.append(
                f"{requirement_id}.status must be one of {sorted(REQUIREMENT_STATUSES)}"
            )
        if status in {"approved", "implemented"}:
            active_requirement_ids.add(requirement_id)

    raw_tasks = manifest.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        errors.append("tasks must be a non-empty list")
        raw_tasks = []

    task_ids: set[str] = set()
    tasks: dict[str, dict[str, Any]] = {}
    issue_owners: dict[str, str] = {}
    branch_owners: dict[str, str] = {}
    pr_owners: dict[str, str] = {}
    for index, task in enumerate(raw_tasks):
        label = f"tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{label} must be an object")
            continue
        task_id = task.get("id")
        if not _nonempty_string(task_id) or not ID_RE.match(task_id):
            errors.append(f"{label}.id must be a stable uppercase ID such as TASK-001")
            continue
        if task_id in task_ids:
            errors.append(f"duplicate task id: {task_id}")
            continue
        task_ids.add(task_id)
        tasks[task_id] = task

        if not _nonempty_string(task.get("title")):
            errors.append(f"{task_id}.title must be non-empty")
        if not _string_list(task.get("requirement_ids")):
            errors.append(f"{task_id}.requirement_ids must be a non-empty string list")
        else:
            unknown = set(task["requirement_ids"]) - requirement_ids
            if unknown:
                errors.append(f"{task_id} references unknown requirements: {sorted(unknown)}")
        if not isinstance(task.get("depends_on"), list) or not all(
            _nonempty_string(value) for value in task.get("depends_on", [])
        ):
            errors.append(f"{task_id}.depends_on must be a string list")
        if not _string_list(task.get("owned_paths")):
            errors.append(f"{task_id}.owned_paths must be a non-empty string list")
        auxiliary = task.get("allowed_auxiliary_paths", [])
        if not isinstance(auxiliary, list) or not all(
            _nonempty_string(value) for value in auxiliary
        ):
            errors.append(f"{task_id}.allowed_auxiliary_paths must be a string list")
        if not _string_list(task.get("acceptance_criteria")):
            errors.append(f"{task_id}.acceptance_criteria must be a non-empty string list")
        if not _string_list(task.get("regression_commands")):
            errors.append(f"{task_id}.regression_commands must be a non-empty string list")
        if not _nonempty_string(task.get("rollback")):
            errors.append(f"{task_id}.rollback must be a non-empty string")
        docs = task.get("documentation", [])
        if not isinstance(docs, list) or not all(_nonempty_string(value) for value in docs):
            errors.append(f"{task_id}.documentation must be a string list")
        if task.get("wave") not in {0, 1, 2}:
            errors.append(f"{task_id}.wave must be 0, 1, or 2")
        if task.get("risk") not in RISK_LEVELS:
            errors.append(f"{task_id}.risk must be one of {sorted(RISK_LEVELS)}")
        if task.get("status") not in TASK_STATUSES:
            errors.append(f"{task_id}.status must be one of {sorted(TASK_STATUSES)}")
        if task.get("may_change_contracts", False) and task.get("wave") != 0:
            errors.append(f"{task_id} may change protected contracts only in Wave 0")

        if stage in {"execution", "finish"}:
            if not _nonempty_id(task.get("issue_id")):
                errors.append(f"{task_id}.issue_id is required for {stage}")
            else:
                issue_key = str(task["issue_id"])
                if issue_key in issue_owners:
                    errors.append(
                        f"Issue {issue_key} is shared by {issue_owners[issue_key]} and {task_id}"
                    )
                issue_owners[issue_key] = task_id
            if not _nonempty_string(task.get("branch")):
                errors.append(f"{task_id}.branch is required for {stage}")
            else:
                branch = task["branch"]
                if branch == manifest.get("base_branch"):
                    errors.append(f"{task_id}.branch must differ from base_branch")
                if branch in branch_owners:
                    errors.append(
                        f"branch {branch!r} is shared by {branch_owners[branch]} and {task_id}"
                    )
                branch_owners[branch] = task_id
            if task.get("status") == "proposed":
                errors.append(f"{task_id} is still proposed after Gate B")
        if task.get("status") in ACTIVE_STATUSES and not _nonempty_string(
            task.get("agent_owner")
        ):
            errors.append(f"{task_id}.agent_owner is required while work is active")
        if task.get("status") in {"pr_open", *DONE_STATUSES}:
            if not _nonempty_id(task.get("pr_id")):
                errors.append(f"{task_id}.pr_id is required for status {task.get('status')}")
            else:
                pr_key = str(task["pr_id"])
                if pr_key in pr_owners:
                    errors.append(
                        f"PR {pr_key} is shared by {pr_owners[pr_key]} and {task_id}"
                    )
                pr_owners[pr_key] = task_id
        if task.get("status") in DONE_STATUSES and not _nonempty_string(
            task.get("squash_commit")
        ):
            errors.append(f"{task_id}.squash_commit is required after merge")
        if stage == "finish":
            if not _nonempty_id(task.get("pr_id")):
                errors.append(f"{task_id}.pr_id is required for finish")
            if task.get("status") not in DONE_STATUSES:
                errors.append(f"{task_id} is not finished: {task.get('status')!r}")

    for task_id, task in tasks.items():
        dependencies = task.get("depends_on", [])
        if task_id in dependencies:
            errors.append(f"{task_id} depends on itself")
        unknown = set(dependencies) - task_ids
        if unknown:
            errors.append(f"{task_id} references unknown dependencies: {sorted(unknown)}")
        for dependency in dependencies:
            if dependency in tasks and isinstance(task.get("wave"), int):
                dependency_wave = tasks[dependency].get("wave")
                if isinstance(dependency_wave, int) and dependency_wave > task["wave"]:
                    errors.append(
                        f"{task_id} in Wave {task['wave']} depends on later-wave {dependency}"
                    )

    cycles = _find_cycles(tasks)
    for cycle in cycles:
        errors.append("dependency cycle: " + " -> ".join(cycle))

    covered_requirements = {
        requirement_id
        for task in tasks.values()
        for requirement_id in task.get("requirement_ids", [])
    }
    uncovered = active_requirement_ids - covered_requirements
    if uncovered:
        errors.append(f"active requirements without a task: {sorted(uncovered)}")

    if not cycles:
        depth = _max_depth(tasks)
        if depth > 4:
            warnings.append(f"dependency depth is {depth}; explain why it exceeds four")

    for wave in (0, 2):
        serial_tasks = [task_id for task_id, task in tasks.items() if task.get("wave") == wave]
        for index, left in enumerate(serial_tasks):
            for right in serial_tasks[index + 1 :]:
                if not _depends_on(left, right, tasks) and not _depends_on(right, left, tasks):
                    errors.append(
                        f"Wave {wave} must be serial, but {left} and {right} are unordered"
                    )

    parallel_tasks = [task_id for task_id, task in tasks.items() if task.get("wave") == 1]
    for index, left in enumerate(parallel_tasks):
        for right in parallel_tasks[index + 1 :]:
            if _depends_on(left, right, tasks) or _depends_on(right, left, tasks):
                continue
            overlaps = [
                (left_path, right_path)
                for left_path in tasks[left].get("owned_paths", [])
                for right_path in tasks[right].get("owned_paths", [])
                if patterns_overlap(left_path, right_path)
            ]
            if overlaps:
                errors.append(
                    f"parallel ownership overlap between {left} and {right}: {overlaps}"
                )

    required_gates = ["gate_a"]
    if stage in {"execution", "finish"}:
        required_gates.append("gate_b")
    for gate_name in required_gates:
        if not _gate_is_approved(manifest, gate_name):
            errors.append(f"{gate_name} approval evidence is incomplete")
    if stage == "finish":
        unfinished_requirements = [
            requirement.get("id")
            for requirement in requirements
            if isinstance(requirement, dict) and requirement.get("status") == "approved"
        ]
        if unfinished_requirements:
            errors.append(
                f"approved requirements remain unimplemented: {unfinished_requirements}"
            )

    return errors, warnings


def _validate_manifest_v2(
    manifest: ExecutionManifestV2,
    stage: str,
) -> tuple[list[str], list[str]]:
    """Render the shared kernel validation result for CLI compatibility."""

    # Manifest v2 is an immutable execution snapshot. Runtime completion is
    # proven from Journal replay and a terminal checkpoint, not by mutating the
    # approved requirement/task records inside that snapshot.
    report = validate_admitted_manifest(
        manifest,
        "execution" if stage == "finish" else stage,
    )
    errors = [
        issue.message
        for issue in report.issues
        if issue.severity is Severity.ERROR
    ]
    warnings = [
        issue.message
        for issue in report.issues
        if issue.severity is Severity.WARNING
    ]
    return errors, warnings


def _validate_finish_runtime(
    manifest: ExecutionManifestV2,
    journal_root: str | None,
    checkpoint_root: str | None,
) -> list[str]:
    """Prove terminal state from a stable Journal and checkpoint observation."""

    missing = tuple(
        option
        for option, value in (
            ("--journal-root", journal_root),
            ("--checkpoint-root", checkpoint_root),
        )
        if value is None
    )
    if missing:
        return [
            "finish validation requires " + " and ".join(missing)
        ]
    assert journal_root is not None and checkpoint_root is not None
    journal_path = Path(journal_root).expanduser().absolute()
    checkpoint_path = Path(checkpoint_root).expanduser().absolute()
    if journal_path == checkpoint_path or journal_path.parent != checkpoint_path.parent:
        return [
            "finish Journal and checkpoint roots must be distinct siblings "
            "under one protected control root"
        ]

    try:
        control_root = ProtectedControlRoot.open(journal_path.parent)
    except Exception as exc:  # noqa: BLE001 - validation must report, never traceback
        return [f"finish control root could not be protected: {type(exc).__name__}"]

    with control_root:
        validator = _control_root_validator(control_root)
        try:
            before_events = _read_verified_journal(journal_path)
            if not before_events:
                return ["finish Journal is empty"]
            if any(event.identity.run_id != manifest.run_id for event in before_events):
                return ["finish Journal run identity does not match the manifest"]
            coordinator_epoch = before_events[0].identity.coordinator_epoch
            store = CheckpointStore(
                checkpoint_path,
                control_root_validator=validator,
            )
            before_checkpoint = store.load(
                manifest,
                coordinator_epoch=coordinator_epoch,
            )
            recovered = recover_coordinator_lease(
                journal_path,
                manifest,
                coordinator_epoch=coordinator_epoch,
                checkpoint_store=store,
                repair_derived=False,
                control_root_validator=validator,
            )
            after_events = _read_verified_journal(journal_path)
            after_checkpoint = store.load(
                manifest,
                coordinator_epoch=coordinator_epoch,
            )
        except Exception as exc:  # noqa: BLE001 - all trust-boundary faults are blockers
            detail = str(exc).strip()
            suffix = type(exc).__name__ if not detail else f"{type(exc).__name__}: {detail}"
            return [f"finish runtime evidence could not be verified: {suffix}"]

        errors: list[str] = []
        before_head = JournalHead(
            before_events[-1].sequence,
            before_events[-1].event_hash,
        )
        after_head = JournalHead(
            after_events[-1].sequence,
            after_events[-1].event_hash,
        )
        if before_head != after_head or before_events != after_events:
            errors.append("finish Journal changed during validation")
        if recovered.status is not LeaseRecoveryStatus.RECOVERED:
            detail = (
                "unknown"
                if recovered.fault is None
                else f"{recovered.fault.code.value}: {recovered.fault.detail}"
            )
            errors.append(f"finish Journal replay did not recover: {detail}")
            return errors
        if recovered.replay.head != after_head:
            errors.append("finish Journal head does not match the recovered replay")
        if recovered.replay.quarantined_tail is not None:
            errors.append("finish Journal contains a quarantined tail")
        if recovered.replay.derived_faults:
            errors.append("finish replay reported invalid derived state")

        lease_state = recovered.lease_state
        lease = None if lease_state is None else lease_state.lease
        if lease is None:
            errors.append("finish Gate B admission has no durable lease identity")
        else:
            admission = admit_execution_snapshot(
                manifest,
                after_events,
                workspace_hash=lease.owner.workspace_hash,
            )
            if not admission.admitted:
                errors.append(
                    "finish Gate B execution admission failed: "
                    + admission.reason.value
                )

        snapshot = recovered.replay.snapshot
        graph_index = recovered.replay.graph_index
        if not graph_index.verify(manifest, snapshot):
            errors.append("finish GraphIndex does not match the manifest and replay")
        if snapshot.phase is not RuntimeState.COMPLETE:
            errors.append(
                f"finish run phase is {snapshot.phase.value!r}, not 'complete'"
            )
        unfinished = tuple(
            task.task_id
            for task in snapshot.tasks
            if task.state not in {RuntimeState.VERIFIED, RuntimeState.ARCHIVED}
        )
        if unfinished:
            errors.append(
                "finish tasks are not verified or archived: " + ", ".join(unfinished)
            )

        terminal_lease = recovered.last_lease_event
        if (
            lease_state is None
            or lease_state.active
            or lease_state.event_type is not JournalEventType.LEASE_RELEASED
            or terminal_lease is None
            or terminal_lease.event_type is not JournalEventType.LEASE_RELEASED
            or terminal_lease.sequence != after_head.sequence
            or terminal_lease.event_hash != after_head.event_hash
        ):
            errors.append("finish coordinator lease is not terminally released")
        if recovered.pending_dispatch_requests or recovered.pending_external_effects:
            errors.append("finish replay still contains pending external effects")
        if any(not record.complete for record in recovered.dispatch_recoveries):
            errors.append("finish replay still contains incomplete dispatch recovery")

        if (
            before_checkpoint.status is not CheckpointLoadStatus.LOADED
            or after_checkpoint.status is not CheckpointLoadStatus.LOADED
            or before_checkpoint.checkpoint is None
            or after_checkpoint.checkpoint is None
        ):
            fault = after_checkpoint.fault or before_checkpoint.fault
            detail = (
                after_checkpoint.status.value
                if fault is None
                else f"{fault.code.value}: {fault.detail}"
            )
            errors.append(f"finish terminal checkpoint is not verified: {detail}")
        elif before_checkpoint != after_checkpoint:
            errors.append("finish checkpoint changed during validation")
        else:
            checkpoint = after_checkpoint.checkpoint
            if (
                checkpoint.snapshot != snapshot
                or checkpoint.graph_index != graph_index
                or checkpoint.journal_through_sequence != after_head.sequence
                or checkpoint.journal_through_event_hash != after_head.event_hash
            ):
                errors.append(
                    "finish checkpoint does not cover the terminal Journal state"
                )
        if not control_root.revalidate().ok:
            errors.append("finish control root changed during validation")
        return errors


def _validate_admitted_manifest(
    manifest: ExecutionManifestModel,
    stage: str,
) -> tuple[list[str], list[str]]:
    if type(manifest) is ExecutionManifestV2:
        return _validate_manifest_v2(manifest, stage)
    if type(manifest) is ExecutionManifest:
        return validate_manifest(manifest.to_primitive(), stage)
    raise TypeError("manifest must be an admitted execution manifest")


def ready_tasks(manifest: dict[str, Any], limit: int | None = None) -> dict[str, Any]:
    tasks = _task_map(manifest)
    unfinished = [
        task for task in tasks.values() if task.get("status") not in DONE_STATUSES
    ]
    if not unfinished:
        return {"wave": None, "task_ids": [], "capacity": 0, "complete": True}

    current_wave = min(task.get("wave", 99) for task in unfinished)
    wave_tasks = [task for task in unfinished if task.get("wave") == current_wave]
    active = [task for task in wave_tasks if task.get("status") in ACTIVE_STATUSES]

    configured_limit = limit if limit is not None else manifest.get("max_concurrency", 3)
    concurrency = 1 if current_wave in {0, 2} else configured_limit
    capacity = max(0, concurrency - len(active))
    if capacity == 0:
        return {
            "wave": current_wave,
            "task_ids": [],
            "capacity": 0,
            "complete": False,
        }

    selected: list[dict[str, Any]] = []
    for task in sorted(wave_tasks, key=lambda item: item["id"]):
        if task.get("status") not in DISPATCHABLE_STATUSES:
            continue
        dependencies = task.get("depends_on", [])
        if any(tasks[dependency].get("status") not in DONE_STATUSES for dependency in dependencies):
            continue
        occupied = active + selected
        if any(
            patterns_overlap(candidate_path, occupied_path)
            for candidate_path in task.get("owned_paths", [])
            for other in occupied
            for occupied_path in other.get("owned_paths", [])
        ):
            continue
        selected.append(task)
        if len(selected) >= capacity:
            break

    return {
        "wave": current_wave,
        "task_ids": [task["id"] for task in selected],
        "capacity": capacity,
        "complete": False,
    }


def _ready_manifest_v2(
    manifest: ExecutionManifestV2,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return a pre-Gate-B scheduling preview from the immutable graph index."""

    graph_index = GraphIndex.compile(manifest)
    selected_limit = (
        manifest.max_concurrency
        if limit is None
        else min(limit, manifest.max_concurrency)
    )
    task_ids = list(graph_index.ready_set[:selected_limit])
    task_waves = {task.id: task.wave for task in manifest.tasks}
    waves = sorted({task_waves[task_id] for task_id in task_ids})
    return {
        "admission_blocker": "gate_b_approval_required",
        "capacity": selected_limit,
        "complete": False,
        "execution_admitted": False,
        "task_ids": task_ids,
        "wave": waves[0] if len(waves) == 1 else None,
        "waves": waves,
    }


def drift_report(
    manifest: dict[str, Any], task_id: str, changed_files: Iterable[str]
) -> dict[str, Any]:
    tasks = _task_map(manifest)
    if task_id not in tasks:
        raise ManifestError(f"unknown task id: {task_id}")
    task = tasks[task_id]
    allowed = task.get("owned_paths", []) + task.get("allowed_auxiliary_paths", [])
    protected = manifest.get("protected_paths", [])
    changed = sorted({_normalize_path(path) for path in changed_files if path.strip()})
    outside = [
        path for path in changed if not any(path_matches(path, pattern) for pattern in allowed)
    ]
    protected_changes = [
        path
        for path in changed
        if any(path_matches(path, pattern) for pattern in protected)
        and not task.get("may_change_contracts", False)
    ]
    return {
        "task_id": task_id,
        "changed_files": changed,
        "outside_owned_paths": outside,
        "protected_path_changes": protected_changes,
        "ok": not outside and not protected_changes,
    }


def trace_markdown(manifest: dict[str, Any]) -> str:
    tasks = _task_map(manifest)
    rows = [
        "# Requirement Traceability",
        "",
        "| Requirement | Status | Tasks | Issues | PRs | Commits | Regression commands |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    def escape(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    def command_text(command: object) -> str:
        if _nonempty_string(command):
            return command
        if type(command) is dict:
            display_text = command.get("display_text")
            if _nonempty_string(display_text):
                return display_text
            argv = command.get("argv")
            if isinstance(argv, list) and all(
                _nonempty_string(argument) for argument in argv
            ):
                return " ".join(argv)
        return json.dumps(command, sort_keys=True, separators=(",", ":"))

    for requirement in manifest.get("requirements", []):
        requirement_id = requirement.get("id", "")
        mapped = [
            task
            for task in tasks.values()
            if requirement_id in task.get("requirement_ids", [])
        ]
        rows.append(
            "| {req} | {status} | {tasks} | {issues} | {prs} | {commits} | {commands} |".format(
                req=escape(requirement_id),
                status=escape(requirement.get("status", "")),
                tasks=escape(", ".join(task["id"] for task in mapped) or "-"),
                issues=escape(", ".join(str(task.get("issue_id") or "-") for task in mapped) or "-"),
                prs=escape(", ".join(str(task.get("pr_id") or "-") for task in mapped) or "-"),
                commits=escape(
                    ", ".join(str(task.get("squash_commit") or "-") for task in mapped)
                    or "-"
                ),
                commands=escape(
                    "; ".join(
                        command_text(command)
                        for task in mapped
                        for command in task.get("regression_commands", [])
                    )
                    or "-"
                ),
            )
        )
    rows.extend(["", f"Generated for `{manifest.get('run_id', 'unknown')}`.", ""])
    return "\n".join(rows)


def _print_validation(errors: list[str], warnings: list[str]) -> None:
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print(f"OK: manifest valid ({len(warnings)} warning(s))")


def _read_changed_files(args: argparse.Namespace) -> list[str]:
    changed = list(args.changed_file or [])
    if args.changed_files:
        changed.extend(
            Path(args.changed_files).read_text(encoding="utf-8").splitlines()
        )
    if not changed and not sys.stdin.isatty():
        changed.extend(sys.stdin.read().splitlines())
    if not any(path.strip() for path in changed):
        raise ManifestError("no changed files provided")
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = _StableArgumentParser(
        prog="wishctl",
        description="Validate and inspect wish-builder execution manifests."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a manifest")
    validate_parser.add_argument("manifest")
    validate_parser.add_argument(
        "--stage", choices=("planning", "execution", "finish"), default="planning"
    )
    validate_parser.add_argument(
        "--journal-root",
        help="Journal directory required for finish validation",
    )
    validate_parser.add_argument(
        "--checkpoint-root",
        help="checkpoint directory required for finish validation",
    )

    ready_parser = subparsers.add_parser("ready", help="print dispatchable task IDs")
    ready_parser.add_argument("manifest")
    ready_parser.add_argument("--limit", type=int)

    drift_parser = subparsers.add_parser("drift", help="check changed files against ownership")
    drift_parser.add_argument("manifest")
    drift_parser.add_argument("--task", required=True)
    drift_parser.add_argument("--changed-file", action="append")
    drift_parser.add_argument("--changed-files", help="newline-delimited file list")

    trace_parser = subparsers.add_parser("trace", help="render requirement trace Markdown")
    trace_parser.add_argument("manifest")
    trace_parser.add_argument("--output")

    hash_parser = subparsers.add_parser("hash", help="print a gate artifact SHA-256")
    hash_parser.add_argument("artifact")

    import_parser = subparsers.add_parser(
        "import-trellis",
        help=(
            "compile a Wish Builder-derived graph snapshot from official Trellis "
            "task records into manifest v2"
        ),
    )
    import_parser.add_argument(
        "snapshot",
        help="complete Wish Builder-derived snapshot from official Trellis task records",
    )
    import_parser.add_argument("settings", help="explicit import and admission settings JSON")
    import_parser.add_argument("--output", help="write the manifest atomically to this path")
    import_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file",
    )
    import_parser.add_argument(
        "--approved-graph-digest",
        help="approved sha256 graph digest to compare for Gate B invalidation",
    )

    snapshot_parser = subparsers.add_parser(
        "snapshot-trellis",
        help="derive a Wish Builder graph snapshot from official Trellis tasks",
    )
    snapshot_parser.add_argument("parent_task_id", help="Trellis parent task ID")
    snapshot_parser.add_argument(
        "--checkout-root",
        default=".",
        help="Git checkout containing .trellis/tasks (default: .)",
    )
    snapshot_parser.add_argument(
        "--core-root",
        help="extracted @mindfoldhq/trellis-core 0.6.15 package root",
    )
    snapshot_parser.add_argument(
        "--core-archive",
        help="official @mindfoldhq/trellis-core 0.6.15 npm tarball",
    )
    snapshot_parser.add_argument("--node", help=argparse.SUPPRESS)
    snapshot_parser.add_argument("--output", help="write the snapshot atomically")
    snapshot_parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="run one qualified frozen execution manifest in the foreground",
    )
    run_parser.add_argument("manifest", help="approved execution manifest v2")
    run_parser.add_argument(
        "--runtime-root",
        help="directory for protected run state (required after backend admission)",
    )
    run_parser.add_argument(
        "--workspace-root",
        default=".",
        help="Git worktree containing the approved execution manifest (default: .)",
    )
    run_parser.add_argument(
        "--provider-sdk-root",
        help=(
            "absolute npm project (or package) root containing the exact pinned "
            "Codex, Pi, or Oh My Pi SDK; no registry resolution is performed"
        ),
    )

    decide_parser = subparsers.add_parser(
        "decide",
        help="commit one direct-CLI Gate decision to the Journal",
    )
    decide_parser.add_argument("request", help="canonical decision request JSON")
    decide_parser.add_argument(
        "--journal-root",
        required=True,
        help="Journal directory containing the segments directory",
    )
    decide_parser.add_argument(
        "--workspace-hash",
        required=True,
        help="freshly revalidated workspace sha256 identity",
    )
    decide_parser.add_argument(
        "--workspace-root",
        default=".",
        help=argparse.SUPPRESS,
    )
    decide_parser.add_argument(
        "--workspace-scope",
        action="append",
        help=argparse.SUPPRESS,
    )
    decide_parser.add_argument(
        "--choice",
        required=True,
        choices=tuple(choice.value for choice in DecisionChoice),
    )
    decide_parser.add_argument("--actor-id", required=True)
    decide_parser.add_argument("--host-id")
    decide_parser.add_argument("--decision-id")

    resume_parser = subparsers.add_parser(
        "resume",
        help="resume one unknown dispatch from a direct-CLI recovery proof",
    )
    resume_parser.add_argument("manifest", help="approved execution manifest v2")
    resume_parser.add_argument("proof", help="canonical dispatch recovery proof JSON")
    resume_parser.add_argument(
        "--journal-root",
        required=True,
        help="Journal directory containing the segments directory",
    )
    resume_parser.add_argument(
        "--workspace-root",
        default=".",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import-trellis":
            return _run_trellis_import(args)
        if args.command == "snapshot-trellis":
            return _run_trellis_snapshot(args)
        if args.command == "run":
            return _run_foreground(args)
        if args.command == "decide":
            return _run_decide(args)
        if args.command == "resume":
            return _run_resume(args)
        if args.command == "hash":
            digest = hashlib.sha256(Path(args.artifact).read_bytes()).hexdigest()
            print(f"sha256:{digest}")
            return 0
        manifest = load_manifest(args.manifest)
        primitive = manifest.to_primitive()
        if args.command == "validate":
            errors, warnings = _validate_admitted_manifest(manifest, args.stage)
            if args.stage == "finish" and type(manifest) is ExecutionManifestV2:
                # Manifest v2 deliberately keeps Gate B null to avoid a
                # self-referential digest. At finish, the supplied runtime
                # evidence is the authority for post-Gate-B execution.
                errors = [
                    error
                    for error in errors
                    if error != "Gate B approval evidence is incomplete."
                ]
                errors.extend(
                    _validate_finish_runtime(
                        manifest,
                        args.journal_root,
                        args.checkpoint_root,
                    )
                )
            elif args.journal_root is not None or args.checkpoint_root is not None:
                errors.append(
                    "--journal-root and --checkpoint-root are only used by "
                    "manifest v2 finish validation"
                )
            _print_validation(errors, warnings)
            return 1 if errors else 0
        if args.command == "ready":
            validation_stage = (
                "planning"
                if type(manifest) is ExecutionManifestV2
                else "execution"
            )
            errors, warnings = _validate_admitted_manifest(
                manifest,
                validation_stage,
            )
            if errors:
                _print_validation(errors, warnings)
                return 1
            if args.limit is not None and args.limit < 1:
                raise ManifestError("--limit must be a positive integer")
            ready = (
                _ready_manifest_v2(manifest, args.limit)
                if type(manifest) is ExecutionManifestV2
                else ready_tasks(primitive, args.limit)
            )
            print(json.dumps(ready, indent=2))
            return 0
        if args.command == "drift":
            report = drift_report(
                primitive,
                args.task,
                _read_changed_files(args),
            )
            print(json.dumps(report, indent=2))
            return 0 if report["ok"] else 1
        if args.command == "trace":
            output = trace_markdown(primitive)
            if args.output:
                Path(args.output).write_text(output, encoding="utf-8", newline="\n")
            else:
                print(output, end="")
            return 0
    except (
        DecisionCliError,
        ManifestError,
        RecoveryCliError,
        RunCliError,
        TrellisGraphAdapterError,
        TrellisGraphImportError,
        OSError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
