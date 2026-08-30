from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from tests.adapters.test_trellis_graph_import import (
    payload as trellis_payload,
    settings as trellis_settings,
    snapshot as trellis_snapshot,
    task as trellis_task,
)
from wish_builder.adapters.trellis import TrellisGraphImportError, import_trellis_snapshot
from wish_builder.compatibility import (
    BUNDLED_BACKEND_QUALIFICATION_DIGESTS,
    load_bundled_compatibility,
)
from wish_builder.contracts import (
    ExecutionIdentity,
    canonical_json_bytes,
    canonical_sha256,
    generated_task_packet_bytes,
)
from wish_builder.contracts.compatibility import Platform, Provider, QualificationStatus
from wish_builder.contracts.qualification_evidence import (
    QUALIFICATION_EVIDENCE_ROLE_ORDER,
    QUALIFICATION_EVENT_GENESIS_DIGEST,
    QUALIFICATION_SCENARIO_ORDER,
    AttemptPreparedPayload,
    CancelObservedPayload,
    CancelRequestedPayload,
    ChannelReservedPayload,
    CleanupObservedPayload,
    CleanupRequestedPayload,
    CrashInjectedPayload,
    PrepareRequestedPayload,
    ProcessRestartedPayload,
    QualificationEffectStatus,
    QualificationEvent,
    QualificationEventPayload,
    QualificationEventSource,
    QualificationEventType,
    QualificationEvidenceArtifact,
    QualificationEvidenceInventory,
    QualificationEvidenceRole,
    QualificationEvidenceScenario,
    QualificationHarnessDescriptor,
    QualificationProvenance,
    QualificationProvenanceKind,
    QualificationProvenanceSubject,
    QualificationRunOutcome,
    QualificationTurnState,
    QualificationTurnTerminalState,
    ReconcileInspectedPayload,
    ReconcileRequestedPayload,
    ReserveRequestedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    SendRequestedPayload,
    TaskPacketSentPayload,
    TurnStartedPayload,
    TurnTerminalPayload,
    qualification_event_log_bytes,
)
from wish_builder.services.backend_qualification_builder import (
    BackendQualificationCandidateError,
    build_backend_qualification_candidate,
    verify_backend_qualification_candidate,
)


SOURCE_REVISION = "a" * 40
BASE_COMMIT = "b" * 40
RUN_ID = "QUAL-CODEX-WINDOWS-001"
RECORDED_AT = "2026-08-20T02:00:00Z"
HOST_BOOT_ID = "boot-windows-001"
HARNESS_VERSION = "1.0.0"


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _bytes_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _field_values(value: object) -> dict[str, object]:
    return {field.name: getattr(value, field.name) for field in fields(value)}


@dataclass(frozen=True, slots=True)
class _PendingEvent:
    scenario: QualificationEvidenceScenario
    payload: QualificationEventPayload
    source: QualificationEventSource
    process_identity: str = "process-main"


@dataclass(frozen=True, slots=True)
class _EvidenceOptions:
    omit: tuple[QualificationEvidenceScenario, type[QualificationEventPayload]] | None = None
    swap: (
        tuple[
            QualificationEvidenceScenario,
            type[QualificationEventPayload],
            type[QualificationEventPayload],
        ]
        | None
    ) = None
    cancel_identity_mismatch: bool = False
    crash_duplicate_send: bool = False
    crash_bad_reconcile_digest: bool = False
    cleanup_incomplete: bool = False
    cancel_outside_turn: bool = False
    touching_overlap: bool = False
    path_conflict: str | None = None
    overlap_dependency: bool = False
    bad_manifest_pin: bool = False
    bad_sdk_pin: bool = False
    bad_trellis_pin: bool = False
    inventory_provider: Provider = Provider.CODEX
    inventory_platform: Platform = Platform.WINDOWS
    inventory_run_id: str = RUN_ID
    event_provider: Provider = Provider.CODEX
    event_platform: Platform = Platform.WINDOWS
    event_run_id: str = RUN_ID
    event_source_override: QualificationEventSource | None = None
    full_turn_unknown_task: bool = False
    full_turn_wrong_paths: bool = False
    full_turn_wrong_packet: bool = False
    crash_wrong_failpoint: bool = False
    overlap_reuse_channel_dispatch: bool = False


def _source(payload: QualificationEventPayload) -> QualificationEventSource:
    if type(payload) in (RunStartedPayload, RunFinishedPayload, ProcessRestartedPayload):
        return QualificationEventSource.RUNNER
    if type(payload) in (
        ChannelReservedPayload,
        TaskPacketSentPayload,
        TurnStartedPayload,
        TurnTerminalPayload,
        CancelObservedPayload,
        ReconcileInspectedPayload,
    ):
        return QualificationEventSource.PROVIDER
    return QualificationEventSource.WISH_BUILDER


def _pending(
    scenario: QualificationEvidenceScenario,
    payload: QualificationEventPayload,
    *,
    process_identity: str = "process-main",
) -> _PendingEvent:
    return _PendingEvent(scenario, payload, _source(payload), process_identity)


def _attempt_payloads(
    *,
    label: str,
    task_id: str,
    trellis_task_id: str,
    writable_paths: tuple[str, ...],
    task_packet: str,
    terminal_state: QualificationTurnTerminalState = QualificationTurnTerminalState.DONE,
) -> dict[str, QualificationEventPayload]:
    operation_prepare = f"op-{label}-prepare"
    operation_reserve = f"op-{label}-reserve"
    operation_send = f"op-{label}-send"
    dispatch_id = "DISPATCH-" + label.upper().replace("_", "-")
    attempt_id = f"attempt-{label}"
    worktree_id = f"worktree-{label}"
    channel_id = f"channel-{label}"
    session_id = f"session-{label}"
    message_id = f"message-{label}"
    turn_id = f"turn-{label}"
    packet_digest = _bytes_digest(task_packet.encode("utf-8"))
    result_digest = (
        _digest("4")
        if terminal_state is QualificationTurnTerminalState.DONE
        else None
    )
    prepare = PrepareRequestedPayload(
        operation_prepare,
        dispatch_id,
        attempt_id,
        task_id,
        trellis_task_id,
        worktree_id,
        BASE_COMMIT,
        writable_paths,
    )
    return {
        "prepare": prepare,
        "prepared": AttemptPreparedPayload(**_field_values(prepare)),
        "reserve": ReserveRequestedPayload(
            operation_reserve, dispatch_id, attempt_id, task_id, channel_id
        ),
        "reserved": ChannelReservedPayload(
            operation_reserve,
            dispatch_id,
            attempt_id,
            task_id,
            channel_id,
            session_id,
        ),
        "send": SendRequestedPayload(
            operation_send,
            dispatch_id,
            attempt_id,
            task_id,
            channel_id,
            task_packet,
            packet_digest,
        ),
        "sent": TaskPacketSentPayload(
            operation_send,
            dispatch_id,
            attempt_id,
            task_id,
            channel_id,
            session_id,
            message_id,
            task_packet,
            packet_digest,
        ),
        "started": TurnStartedPayload(
            dispatch_id,
            attempt_id,
            task_id,
            channel_id,
            session_id,
            message_id,
            turn_id,
        ),
        "terminal": TurnTerminalPayload(
            dispatch_id,
            attempt_id,
            task_id,
            channel_id,
            session_id,
            message_id,
            turn_id,
            terminal_state,
            result_digest,
        ),
    }


def _base_attempt_events(
    scenario: QualificationEvidenceScenario,
    values: dict[str, QualificationEventPayload],
) -> list[_PendingEvent]:
    return [
        _pending(scenario, values[name])
        for name in (
            "prepare",
            "prepared",
            "reserve",
            "reserved",
            "send",
            "sent",
            "started",
            "terminal",
        )
    ]


def _run_start_payload(
    *,
    harness_digest: str,
    manifest_digest: str,
    snapshot_digest: str,
    bundle: object,
) -> RunStartedPayload:
    provider = next(item for item in bundle.providers if item.provider is Provider.CODEX)
    cell = bundle.platform(Provider.CODEX, Platform.WINDOWS)
    return RunStartedPayload(
        source_revision=SOURCE_REVISION,
        harness_digest=harness_digest,
        harness_version=HARNESS_VERSION,
        trellis_version="0.6.15",
        trellis_compatibility_digest=bundle.trellis_compatibility_digest,
        policy_digest=bundle.policy_digest,
        launch_profile_digest=cell.launch_profile_digest,
        capability_digest=cell.capabilities.capability_digest,
        manifest_digest=manifest_digest,
        trellis_snapshot_digest=snapshot_digest,
        sdk_name=provider.sdk.name,
        sdk_version=provider.sdk.version,
        sdk_shasum=provider.sdk.shasum,
    )


def _scenario(
    scenario: QualificationEvidenceScenario,
    start: RunStartedPayload,
    body: list[_PendingEvent],
) -> list[_PendingEvent]:
    return [
        _pending(scenario, start),
        *body,
        _pending(scenario, RunFinishedPayload(QualificationRunOutcome.COMPLETED)),
    ]


def _build_pending_events(
    *,
    manifest: object,
    run_start: RunStartedPayload,
    options: _EvidenceOptions,
) -> tuple[list[_PendingEvent], tuple[int, int] | None]:
    task_by_id = {item.id: item for item in manifest.tasks}
    trellis_by_id = {item.task_id: item.trellis_task_id for item in manifest.task_id_mapping}
    task_ids = tuple(sorted(task_by_id))
    sibling_task_ids = tuple(
        sorted(task.id for task in manifest.tasks if task.wave == 1)
    )
    if len(sibling_task_ids) != 2:
        raise AssertionError("qualification fixture requires exactly two Wave 1 siblings")

    def attempt(
        label: str,
        task_id: str,
        terminal: QualificationTurnTerminalState = QualificationTurnTerminalState.DONE,
    ) -> dict[str, QualificationEventPayload]:
        task = task_by_id[task_id]
        identity = ExecutionIdentity(
            manifest.run_id,
            1,
            task_id,
            1,
            "DISPATCH-" + label.upper().replace("_", "-"),
        )
        task_packet = generated_task_packet_bytes(
            manifest,
            task,
            trellis_by_id[task_id],
            identity,
        ).decode("utf-8")
        return _attempt_payloads(
            label=label,
            task_id=task_id,
            trellis_task_id=trellis_by_id[task_id],
            writable_paths=tuple(task.owned_paths + task.allowed_auxiliary_paths),
            task_packet=task_packet,
            terminal_state=terminal,
        )

    full = attempt("full", sibling_task_ids[0])
    if options.full_turn_unknown_task:
        full["prepare"] = replace(
            full["prepare"],
            task_id="TASK-999",
            trellis_task_id="trellis-task-unknown",
        )
        full["prepared"] = AttemptPreparedPayload(
            **_field_values(full["prepare"])
        )
        for name in ("reserve", "reserved", "send", "sent", "started", "terminal"):
            full[name] = replace(full[name], task_id="TASK-999")
    if options.full_turn_wrong_paths:
        full["prepare"] = replace(full["prepare"], owned_paths=("src/unbound/**",))
        full["prepared"] = AttemptPreparedPayload(
            **_field_values(full["prepare"])
        )
    if options.full_turn_wrong_packet:
        wrong_packet = canonical_json_bytes({"kind": "unbound-packet"}).decode("utf-8")
        wrong_digest = _bytes_digest(wrong_packet.encode("utf-8"))
        full["send"] = replace(
            full["send"],
            task_packet=wrong_packet,
            task_packet_digest=wrong_digest,
        )
        full["sent"] = replace(
            full["sent"],
            task_packet=wrong_packet,
            task_packet_digest=wrong_digest,
        )
    cancellation = attempt(
        "cancel", sibling_task_ids[0], QualificationTurnTerminalState.CANCELLED
    )
    cancel_started = cancellation["started"]
    assert type(cancel_started) is TurnStartedPayload
    cancel_request = CancelRequestedPayload(
        "op-cancel-active",
        cancel_started.dispatch_id,
        cancel_started.attempt_id,
        cancel_started.task_id,
        cancel_started.channel_id,
        cancel_started.provider_session_id,
        cancel_started.provider_message_id,
        cancel_started.provider_turn_id,
    )
    cancel_observed = CancelObservedPayload(
        **_field_values(cancel_request), effect_status=QualificationEffectStatus.APPLIED
    )
    if options.cancel_identity_mismatch:
        cancel_observed = replace(cancel_observed, provider_turn_id="turn-cancel-other")

    crash = attempt("crash", sibling_task_ids[0])
    crash_started = crash["started"]
    crash_send = crash["send"]
    assert type(crash_started) is TurnStartedPayload
    assert type(crash_send) is SendRequestedPayload
    request_digest = "sha256:" + canonical_sha256(crash_send.to_primitive())
    if options.crash_bad_reconcile_digest:
        request_digest = _digest("9")
    crash_injected = CrashInjectedPayload(
        "wrong-failpoint" if options.crash_wrong_failpoint else "after-send-before-journal",
        crash_send.operation_id,
        crash_started.dispatch_id,
        crash_started.attempt_id,
        crash_started.task_id,
        crash_started.channel_id,
        crash_started.provider_session_id,
        crash_started.provider_message_id,
        crash_started.provider_turn_id,
    )
    reconcile = ReconcileRequestedPayload(
        crash_send.operation_id,
        request_digest,
        crash_started.dispatch_id,
        crash_started.attempt_id,
        crash_started.task_id,
        crash_started.channel_id,
        crash_started.provider_session_id,
        crash_started.provider_message_id,
        crash_started.provider_turn_id,
    )
    reconcile_observed = ReconcileInspectedPayload(
        **_field_values(reconcile),
        effect_status=QualificationEffectStatus.APPLIED,
        turn_state=QualificationTurnState.DONE,
        result_digest=crash["terminal"].result_digest,
    )

    cleanup = attempt("cleanup", sibling_task_ids[0])
    cleanup_prepared = cleanup["prepared"]
    cleanup_reserved = cleanup["reserved"]
    assert type(cleanup_prepared) is AttemptPreparedPayload
    assert type(cleanup_reserved) is ChannelReservedPayload
    cleanup_request = CleanupRequestedPayload(
        "op-cleanup-release",
        cleanup_prepared.dispatch_id,
        cleanup_prepared.attempt_id,
        cleanup_prepared.task_id,
        cleanup_reserved.channel_id,
        cleanup_reserved.provider_session_id,
        cleanup_prepared.worktree_id,
        ("provider-cleanup", "provider-cleanup-child"),
    )
    target_resources = (
        "channel:" + cleanup_request.channel_id,
        "process:provider-cleanup",
        "process:provider-cleanup-child",
        "provider_session:" + cleanup_request.provider_session_id,
        "worktree:" + cleanup_request.worktree_id,
    )
    sibling_resources = (
        "channel:channel-sibling",
        "provider_session:session-sibling",
        "worktree:worktree-sibling",
    )
    cleanup_observed = CleanupObservedPayload(
        **_field_values(cleanup_request),
        resources_before=target_resources + sibling_resources,
        resources_after=(
            sibling_resources + (("worktree:" + cleanup_request.worktree_id),)
            if options.cleanup_incomplete
            else sibling_resources
        ),
    )

    overlap_a = attempt("overlap-a", sibling_task_ids[0])
    overlap_b = attempt("overlap-b", sibling_task_ids[1])
    if options.overlap_reuse_channel_dispatch:
        a_prepare = overlap_a["prepare"]
        a_reserved = overlap_a["reserved"]
        assert type(a_prepare) is PrepareRequestedPayload
        assert type(a_reserved) is ChannelReservedPayload
        for name, value in tuple(overlap_b.items()):
            replacements: dict[str, str] = {}
            if hasattr(value, "dispatch_id"):
                replacements["dispatch_id"] = a_prepare.dispatch_id
            if hasattr(value, "channel_id"):
                replacements["channel_id"] = a_reserved.channel_id
            if replacements:
                overlap_b[name] = replace(value, **replacements)
        packet = json.loads(overlap_b["send"].task_packet)
        packet["execution"]["dispatch_id"] = a_prepare.dispatch_id
        packet["execution"]["identity"]["correlation_id"] = a_prepare.dispatch_id
        packet_text = canonical_json_bytes(packet).decode("utf-8")
        packet_digest = _bytes_digest(packet_text.encode("utf-8"))
        overlap_b["send"] = replace(
            overlap_b["send"],
            task_packet=packet_text,
            task_packet_digest=packet_digest,
        )
        overlap_b["sent"] = replace(
            overlap_b["sent"],
            task_packet=packet_text,
            task_packet_digest=packet_digest,
        )

    pending: list[_PendingEvent] = []
    pending.extend(
        _scenario(
            QualificationEvidenceScenario.FULL_TURN,
            run_start,
            _base_attempt_events(QualificationEvidenceScenario.FULL_TURN, full),
        )
    )

    cancel_body = _base_attempt_events(
        QualificationEvidenceScenario.ACTIVE_TURN_CANCELLATION, cancellation
    )
    cancel_terminal = next(
        index
        for index, item in enumerate(cancel_body)
        if type(item.payload) is TurnTerminalPayload
    )
    cancel_events = [
        _pending(QualificationEvidenceScenario.ACTIVE_TURN_CANCELLATION, cancel_request),
        _pending(QualificationEvidenceScenario.ACTIVE_TURN_CANCELLATION, cancel_observed),
    ]
    if options.cancel_outside_turn:
        cancel_body.extend(cancel_events)
    else:
        cancel_body[cancel_terminal:cancel_terminal] = cancel_events
    pending.extend(
        _scenario(
            QualificationEvidenceScenario.ACTIVE_TURN_CANCELLATION,
            run_start,
            cancel_body,
        )
    )

    crash_body = _base_attempt_events(
        QualificationEvidenceScenario.CRASH_RECONCILE, crash
    )
    crash_terminal = next(
        index
        for index, item in enumerate(crash_body)
        if type(item.payload) is TurnTerminalPayload
    )
    crash_recovery = [
        _pending(
            QualificationEvidenceScenario.CRASH_RECONCILE,
            crash_injected,
            process_identity="process-main",
        ),
        _pending(
            QualificationEvidenceScenario.CRASH_RECONCILE,
            ProcessRestartedPayload("process-main", "recovery-crash-001"),
            process_identity="process-restarted",
        ),
    ]
    if options.crash_duplicate_send:
        crash_recovery.append(
            _pending(
                QualificationEvidenceScenario.CRASH_RECONCILE,
                crash_send,
                process_identity="process-restarted",
            )
        )
    crash_recovery.extend(
        (
            _pending(
                QualificationEvidenceScenario.CRASH_RECONCILE,
                reconcile,
                process_identity="process-restarted",
            ),
            _pending(
                QualificationEvidenceScenario.CRASH_RECONCILE,
                reconcile_observed,
                process_identity="process-restarted",
            ),
        )
    )
    crash_body[crash_terminal:crash_terminal] = crash_recovery
    crash_body[crash_terminal + len(crash_recovery)] = replace(
        crash_body[crash_terminal + len(crash_recovery)],
        process_identity="process-restarted",
    )
    pending.extend(
        _scenario(QualificationEvidenceScenario.CRASH_RECONCILE, run_start, crash_body)
    )

    cleanup_body = _base_attempt_events(QualificationEvidenceScenario.CLEANUP, cleanup)
    cleanup_body.extend(
        (
            _pending(QualificationEvidenceScenario.CLEANUP, cleanup_request),
            _pending(QualificationEvidenceScenario.CLEANUP, cleanup_observed),
        )
    )
    pending.extend(
        _scenario(QualificationEvidenceScenario.CLEANUP, run_start, cleanup_body)
    )

    overlap_scenario = QualificationEvidenceScenario.SIBLING_OVERLAP
    a_prefix = _base_attempt_events(overlap_scenario, overlap_a)[:-1]
    b_prefix = _base_attempt_events(overlap_scenario, overlap_b)[:-1]
    a_terminal = _pending(overlap_scenario, overlap_a["terminal"])
    b_started = b_prefix[-1]
    touch_pair: tuple[int, int] | None = None
    if options.touching_overlap:
        overlap_body = [*a_prefix, *b_prefix[:-1], a_terminal, b_started]
        touch_pair = (len(overlap_body) - 2, len(overlap_body) - 1)
    else:
        overlap_body = [*a_prefix, *b_prefix, a_terminal]
    overlap_body.append(_pending(overlap_scenario, overlap_b["terminal"]))
    overlap_segment = _scenario(overlap_scenario, run_start, overlap_body)
    if touch_pair is not None:
        segment_offset = len(pending) + 1
        touch_pair = (
            segment_offset + touch_pair[0],
            segment_offset + touch_pair[1],
        )
    pending.extend(overlap_segment)

    if options.omit is not None:
        scenario, payload_type = options.omit
        target = next(
            index
            for index, item in enumerate(pending)
            if item.scenario is scenario and type(item.payload) is payload_type
        )
        pending.pop(target)
    if options.swap is not None:
        scenario, first_type, second_type = options.swap
        first = next(
            index
            for index, item in enumerate(pending)
            if item.scenario is scenario and type(item.payload) is first_type
        )
        second = next(
            index
            for index, item in enumerate(pending)
            if item.scenario is scenario and type(item.payload) is second_type
        )
        pending[first], pending[second] = pending[second], pending[first]
    return pending, touch_pair


def _materialize_events(
    pending: list[_PendingEvent],
    touch_pair: tuple[int, int] | None,
    *,
    qualification_run_id: str = RUN_ID,
    provider: Provider = Provider.CODEX,
    platform: Platform = Platform.WINDOWS,
    source_override: QualificationEventSource | None = None,
) -> tuple[QualificationEvent, ...]:
    events: list[QualificationEvent] = []
    previous = QUALIFICATION_EVENT_GENESIS_DIGEST
    for index, item in enumerate(pending):
        monotonic_ns = (index + 1) * 100
        if touch_pair is not None and index == touch_pair[1]:
            monotonic_ns = (touch_pair[0] + 1) * 100
        event = QualificationEvent.create(
            sequence=index + 1,
            qualification_run_id=qualification_run_id,
            scenario=item.scenario,
            provider=provider,
            platform=platform,
            source=source_override or item.source,
            event_type=item.payload.EVENT_TYPE,
            recorded_at=RECORDED_AT,
            monotonic_ns=monotonic_ns,
            host_boot_id=HOST_BOOT_ID,
            process_identity=item.process_identity,
            payload=item.payload,
            previous_event_digest=previous,
        )
        events.append(event)
        previous = event.event_digest
    return tuple(events)


def _artifact(
    role: QualificationEvidenceRole,
    path: str,
    raw: bytes,
    media_type: str,
) -> QualificationEvidenceArtifact:
    return QualificationEvidenceArtifact(
        role=role,
        path=path,
        digest=_bytes_digest(raw),
        byte_length=len(raw),
        media_type=media_type,
    )


def _write_evidence_root(root: Path, options: _EvidenceOptions = _EvidenceOptions()) -> None:
    root.mkdir(parents=True)
    bundle = load_bundled_compatibility()
    cell = bundle.platform(Provider.CODEX, Platform.WINDOWS)

    graph = copy.deepcopy(trellis_payload())
    graph["requirements"].append(
        {
            "id": "REQ-003",
            "text": "Project a second independent task",
            "status": "approved",
            "decision_ref": None,
        }
    )
    graph["tasks"] = [
        trellis_task("trellis-task-foundation", "REQ-001", wave=0),
        trellis_task(
            "trellis-task-alpha",
            "REQ-002",
            depends_on=["trellis-task-foundation"],
            wave=1,
        ),
        trellis_task(
            "trellis-task-zeta",
            "REQ-003",
            depends_on=["trellis-task-foundation"],
            wave=1,
        ),
    ]
    if options.overlap_dependency:
        graph["tasks"][2]["depends_on"].append("trellis-task-alpha")
    if options.path_conflict == "exact":
        graph["tasks"][2]["owned_paths"] = list(graph["tasks"][1]["owned_paths"])
    elif options.path_conflict == "ancestor":
        graph["tasks"][1]["owned_paths"] = ["src/shared/**"]
        graph["tasks"][2]["owned_paths"] = ["src/shared/nested/**"]
    elif options.path_conflict == "glob":
        graph["tasks"][1]["owned_paths"] = ["src/*/generated/**"]
        graph["tasks"][2]["owned_paths"] = ["src/api/generated/model.py"]
    elif options.path_conflict == "case":
        graph["tasks"][1]["owned_paths"] = ["src/CaseSensitive/**"]
        graph["tasks"][2]["owned_paths"] = ["SRC/casesensitive/model.py"]
    snapshot_bytes = canonical_json_bytes(graph)
    snapshot = trellis_snapshot(raw=snapshot_bytes)
    settings = replace(
        trellis_settings(),
        policy_digest=bundle.policy_digest,
        launch_profile_digest=cell.launch_profile_digest,
        capability_digest=(
            _digest("9")
            if options.bad_manifest_pin
            else cell.capabilities.capability_digest
        ),
    )
    manifest = import_trellis_snapshot(snapshot, settings).manifest
    manifest_bytes = manifest.canonical_json_bytes()
    harness = QualificationHarnessDescriptor(
        schema_version=1,
        harness_version=HARNESS_VERSION,
        source_revision=SOURCE_REVISION,
        entrypoint="scripts/live_backend_qualification.py",
        event_schema_version=1,
        scenarios=QUALIFICATION_SCENARIO_ORDER,
    )
    harness_bytes = harness.canonical_json_bytes()
    run_start = _run_start_payload(
        harness_digest=_bytes_digest(harness_bytes),
        manifest_digest=_bytes_digest(manifest_bytes),
        snapshot_digest=_bytes_digest(snapshot_bytes),
        bundle=bundle,
    )
    if options.bad_sdk_pin:
        run_start = replace(run_start, sdk_version="999.0.0")
    if options.bad_trellis_pin:
        run_start = replace(
            run_start,
            trellis_compatibility_digest="sha256:" + "9" * 64,
        )
    pending, touch_pair = _build_pending_events(
        manifest=manifest,
        run_start=run_start,
        options=options,
    )
    event_bytes = qualification_event_log_bytes(
        _materialize_events(
            pending,
            touch_pair,
            qualification_run_id=options.event_run_id,
            provider=options.event_provider,
            platform=options.event_platform,
            source_override=options.event_source_override,
        )
    )
    raw_by_role = {
        QualificationEvidenceRole.EVENT_LOG: event_bytes,
        QualificationEvidenceRole.HARNESS_DESCRIPTOR: harness_bytes,
        QualificationEvidenceRole.EXECUTION_MANIFEST: manifest_bytes,
        QualificationEvidenceRole.TRELLIS_SNAPSHOT: snapshot_bytes,
    }
    paths = {
        QualificationEvidenceRole.EVENT_LOG: "events.jsonl",
        QualificationEvidenceRole.HARNESS_DESCRIPTOR: "harness.json",
        QualificationEvidenceRole.EXECUTION_MANIFEST: "execution-manifest.json",
        QualificationEvidenceRole.TRELLIS_SNAPSHOT: "trellis-snapshot.json",
        QualificationEvidenceRole.PROVENANCE: "provenance.json",
    }
    media_types = {
        QualificationEvidenceRole.EVENT_LOG: "application/x-ndjson",
        QualificationEvidenceRole.HARNESS_DESCRIPTOR: "application/json",
        QualificationEvidenceRole.EXECUTION_MANIFEST: "application/json",
        QualificationEvidenceRole.TRELLIS_SNAPSHOT: "application/json",
        QualificationEvidenceRole.PROVENANCE: "application/json",
    }
    non_provenance = tuple(
        _artifact(role, paths[role], raw_by_role[role], media_types[role])
        for role in QUALIFICATION_EVIDENCE_ROLE_ORDER[:-1]
    )
    provenance = QualificationProvenance(
        schema_version=1,
        kind=QualificationProvenanceKind.GITHUB_ACTIONS,
        issuer="https://token.actions.githubusercontent.com",
        reference="https://github.com/example/wish-builder/actions/runs/1001",
        identity="repo:example/wish-builder:ref:refs/heads/main",
        source_revision=SOURCE_REVISION,
        subjects=tuple(
            QualificationProvenanceSubject.from_artifact(item)
            for item in non_provenance
        ),
    )
    provenance_bytes = provenance.canonical_json_bytes()
    raw_by_role[QualificationEvidenceRole.PROVENANCE] = provenance_bytes
    provenance_artifact = _artifact(
        QualificationEvidenceRole.PROVENANCE,
        paths[QualificationEvidenceRole.PROVENANCE],
        provenance_bytes,
        media_types[QualificationEvidenceRole.PROVENANCE],
    )
    inventory = QualificationEvidenceInventory(
        schema_version=1,
        qualification_run_id=options.inventory_run_id,
        provider=options.inventory_provider,
        platform=options.inventory_platform,
        artifacts=non_provenance + (provenance_artifact,),
    )
    for role in QUALIFICATION_EVIDENCE_ROLE_ORDER:
        (root / paths[role]).write_bytes(raw_by_role[role])
    (root / "inventory.json").write_bytes(inventory.canonical_json_bytes())


def _directory_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class BackendQualificationBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def evidence(
        self,
        name: str,
        **options: object,
    ) -> Path:
        root = self.root / name
        _write_evidence_root(root, _EvidenceOptions(**options))
        return root

    def assert_candidate_error(
        self,
        code: str,
        root: Path,
    ) -> BackendQualificationCandidateError:
        with self.assertRaises(BackendQualificationCandidateError) as raised:
            verify_backend_qualification_candidate(root)
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_verify_and_build_are_deterministic_and_never_authorize_dispatch(self) -> None:
        evidence = self.evidence("valid")
        bundle_before = load_bundled_compatibility()

        first = verify_backend_qualification_candidate(evidence)
        second = verify_backend_qualification_candidate(evidence)
        output_a = self.root / "candidate-a"
        output_b = self.root / "candidate-b"
        built_a = build_backend_qualification_candidate(evidence, output_a)
        built_b = build_backend_qualification_candidate(evidence, output_b)

        self.assertEqual(first.artifact, second.artifact)
        self.assertEqual(first.derived_objects, second.derived_objects)
        self.assertEqual(first.report_bytes, second.report_bytes)
        self.assertEqual(first, built_a)
        self.assertEqual(first, built_b)
        self.assertEqual(_directory_bytes(output_a), _directory_bytes(output_b))
        report = first.report
        self.assertFalse(report["dispatchAuthorized"])
        self.assertFalse(report["enabledForDispatch"])
        self.assertFalse(report["published"])
        self.assertEqual("evidence", report["evidenceRoot"])
        self.assertEqual("candidate_unverified", report["status"])
        self.assertEqual(Provider.CODEX.value, report["provider"])
        self.assertEqual(Platform.WINDOWS.value, report["platform"])
        self.assertTrue(all(item.live for item in first.artifact.scenarios))
        self.assertTrue(
            all(item.status is QualificationStatus.PASSED for item in first.artifact.scenarios)
        )
        bundled_cell = bundle_before.platform(Provider.CODEX, Platform.WINDOWS)
        self.assertTrue(bundle_before.published)
        self.assertTrue(bundled_cell.qualification.enabled_for_dispatch)
        self.assertIsNotNone(bundled_cell.qualification.artifact)
        self.assertEqual(bundle_before, load_bundled_compatibility())

    def test_object_store_rejects_hash_size_and_orphan_mismatches(self) -> None:
        cases: tuple[tuple[str, str, Callable[[Path], None]], ...] = (
            (
                "hash",
                "artifact_digest_mismatch",
                lambda root: (root / "events.jsonl").write_bytes(
                    b"[" + (root / "events.jsonl").read_bytes()[1:]
                ),
            ),
            (
                "size",
                "artifact_size_mismatch",
                lambda root: (root / "events.jsonl").write_bytes(
                    (root / "events.jsonl").read_bytes() + b" "
                ),
            ),
            (
                "orphan",
                "artifact_inventory_mismatch",
                lambda root: (root / "unlisted.json").write_bytes(b"{}\n"),
            ),
        )
        for name, code, mutate in cases:
            with self.subTest(name=name):
                root = self.evidence(name)
                mutate(root)
                self.assert_candidate_error(code, root)

    def test_provenance_subject_mismatch_fails_closed(self) -> None:
        root = self.evidence("provenance-mismatch")
        provenance = json.loads((root / "provenance.json").read_bytes())
        provenance["subjects"][0]["digest"] = _digest("f")
        provenance_bytes = canonical_json_bytes(provenance)
        (root / "provenance.json").write_bytes(provenance_bytes)

        inventory = json.loads((root / "inventory.json").read_bytes())
        artifact = next(
            item for item in inventory["artifacts"] if item["role"] == "provenance"
        )
        artifact["digest"] = _bytes_digest(provenance_bytes)
        artifact["byteLength"] = len(provenance_bytes)
        (root / "inventory.json").write_bytes(canonical_json_bytes(inventory))

        self.assert_candidate_error("provenance_binding_invalid", root)

    def test_scenario_missing_step_and_wrong_order_fail_closed(self) -> None:
        missing = self.evidence(
            "missing-step",
            omit=(QualificationEvidenceScenario.FULL_TURN, ChannelReservedPayload),
        )
        self.assert_candidate_error("event_cardinality", missing)

        wrong_order = self.evidence(
            "wrong-order",
            swap=(
                QualificationEvidenceScenario.FULL_TURN,
                AttemptPreparedPayload,
                ReserveRequestedPayload,
            ),
        )
        self.assert_candidate_error("attempt_order_invalid", wrong_order)

    def test_cancellation_must_preserve_the_active_turn_identity(self) -> None:
        root = self.evidence("cancel-identity", cancel_identity_mismatch=True)
        self.assert_candidate_error("cancellation_invalid", root)

        outside = self.evidence("cancel-outside", cancel_outside_turn=True)
        self.assert_candidate_error("cancellation_invalid", outside)

    def test_crash_reconcile_rejects_resend_and_rewritten_request(self) -> None:
        resent = self.evidence("crash-resend", crash_duplicate_send=True)
        self.assert_candidate_error("event_cardinality", resent)

        rewritten = self.evidence(
            "crash-reconcile-rewritten", crash_bad_reconcile_digest=True
        )
        self.assert_candidate_error("crash_reconcile_invalid", rewritten)
        wrong_failpoint = self.evidence(
            "crash-wrong-failpoint", crash_wrong_failpoint=True
        )
        self.assert_candidate_error("crash_reconcile_invalid", wrong_failpoint)

    def test_cleanup_requires_every_resource_to_be_absent(self) -> None:
        root = self.evidence("cleanup-incomplete", cleanup_incomplete=True)
        self.assert_candidate_error("cleanup_invalid", root)

    def test_touching_turn_intervals_do_not_count_as_overlap(self) -> None:
        root = self.evidence("touching-overlap", touching_overlap=True)
        self.assert_candidate_error("event_log_invalid", root)

    def test_conflicting_sibling_paths_cannot_qualify_overlap(self) -> None:
        for kind in ("exact", "ancestor", "glob", "case"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(
                    TrellisGraphImportError, "parallel_ownership_conflict"
                ):
                    self.evidence(f"path-conflict-{kind}", path_conflict=kind)

    def test_dependency_ancestors_cannot_qualify_as_siblings(self) -> None:
        root = self.evidence("dependent-overlap", overlap_dependency=True)
        self.assert_candidate_error("overlap_not_siblings", root)

    def test_overlap_requires_distinct_dispatch_and_channel_identities(self) -> None:
        root = self.evidence(
            "overlap-identity-reuse",
            overlap_reuse_channel_dispatch=True,
        )
        self.assert_candidate_error("overlap_identity_reuse", root)

    def test_provider_platform_sdk_and_pin_mismatches_fail_closed(self) -> None:
        cases = (
            (
                "provider",
                "provider_mismatch",
                {"inventory_provider": Provider.PI},
            ),
            (
                "platform",
                "event_inventory_identity_mismatch",
                {"event_platform": Platform.LINUX},
            ),
            ("sdk", "run_identity_mismatch", {"bad_sdk_pin": True}),
            ("trellis", "run_identity_mismatch", {"bad_trellis_pin": True}),
            ("manifest", "manifest_pin_mismatch", {"bad_manifest_pin": True}),
            (
                "run-id",
                "event_inventory_identity_mismatch",
                {"inventory_run_id": "QUAL-CODEX-WINDOWS-OTHER"},
            ),
        )
        for name, code, options in cases:
            with self.subTest(name=name):
                self.assert_candidate_error(code, self.evidence(name, **options))

    def test_every_scenario_attempt_is_bound_to_the_frozen_manifest(self) -> None:
        unknown = self.evidence("unknown-task", full_turn_unknown_task=True)
        self.assert_candidate_error("attempt_task_unbound", unknown)
        wrong_paths = self.evidence("wrong-paths", full_turn_wrong_paths=True)
        self.assert_candidate_error("attempt_paths_unbound", wrong_paths)
        wrong_packet = self.evidence("wrong-packet", full_turn_wrong_packet=True)
        self.assert_candidate_error("task_packet_manifest_mismatch", wrong_packet)

    def test_event_sources_are_not_self_asserted(self) -> None:
        root = self.evidence(
            "wrong-sources",
            event_source_override=QualificationEventSource.RUNNER,
        )
        self.assert_candidate_error("event_source_mismatch", root)

    def test_missing_files_and_symlinks_fail_closed(self) -> None:
        missing = self.evidence("missing")
        (missing / "events.jsonl").unlink()
        self.assert_candidate_error("artifact_missing", missing)

        symlinked = self.evidence("symlink")
        target = symlinked / "outside.json"
        target.write_bytes(b"{}")
        link = symlinked / "unlisted-link.json"
        try:
            os.symlink(target, link)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        self.assert_candidate_error("evidence_symlink", symlinked)

    def test_atomic_build_failure_leaves_no_candidate_or_temporary_tree(self) -> None:
        evidence = self.evidence("atomic-failure")
        output = self.root / "candidate-failed"
        with patch(
            "wish_builder.services.backend_qualification_builder.Path.write_bytes",
            side_effect=OSError("injected copy failure"),
        ):
            with self.assertRaisesRegex(OSError, "injected copy failure"):
                build_backend_qualification_candidate(evidence, output)
        self.assertFalse(output.exists())
        self.assertEqual([], list(self.root.glob(".qualification-candidate-*")))

    def test_build_materializes_only_the_bytes_captured_during_verification(self) -> None:
        evidence = self.evidence("captured-bytes")
        original_events = (evidence / "events.jsonl").read_bytes()
        output = self.root / "captured-output"
        original_verify = verify_backend_qualification_candidate

        def verify_then_mutate(root: Path, *, bundle: object = None) -> object:
            candidate = original_verify(root, bundle=bundle)
            (root / "events.jsonl").write_bytes(b"post-verify replacement")
            return candidate

        with patch(
            "wish_builder.services.backend_qualification_builder."
            "verify_backend_qualification_candidate",
            side_effect=verify_then_mutate,
        ):
            build_backend_qualification_candidate(evidence, output)

        self.assertEqual(original_events, (output / "evidence" / "events.jsonl").read_bytes())
        rebuilt = original_verify(output / "evidence")
        self.assertEqual("candidate_unverified", rebuilt.report["status"])

    def test_cli_verifies_and_builds_without_an_enable_path(self) -> None:
        evidence = self.evidence("cli")
        script = Path(__file__).resolve().parents[2] / "scripts" / "ci_backend_qualification.py"
        verified = subprocess.run(
            [sys.executable, str(script), "verify", str(evidence)],
            check=False,
            capture_output=True,
        )
        self.assertEqual(0, verified.returncode, verified.stderr.decode())
        self.assertEqual("candidate_unverified", json.loads(verified.stdout)["status"])

        output = self.root / "cli-candidate"
        built = subprocess.run(
            [sys.executable, str(script), "build", str(evidence), "--output", str(output)],
            check=False,
            capture_output=True,
        )
        self.assertEqual(0, built.returncode, built.stderr.decode())
        self.assertTrue((output / "verification-report.json").is_file())
        self.assertTrue((output / "evidence" / "inventory.json").is_file())
        rejected = subprocess.run(
            [sys.executable, str(script), "verify", str(evidence), "--enable"],
            check=False,
            capture_output=True,
        )
        self.assertNotEqual(0, rejected.returncode)

    def test_bundled_qualification_bytes_and_trust_pins_never_change(self) -> None:
        compatibility_root = Path(__file__).resolve().parents[2] / "wish_builder" / "compatibility"
        bundled_path = compatibility_root / "backend-qualification-0.6.15.json"
        bundled_before = bundled_path.read_bytes()
        pins_before = dict(BUNDLED_BACKEND_QUALIFICATION_DIGESTS)
        evidence = self.evidence("immutable-bundle")
        build_backend_qualification_candidate(evidence, self.root / "immutable-output")
        self.assertEqual(bundled_before, bundled_path.read_bytes())
        self.assertEqual(pins_before, BUNDLED_BACKEND_QUALIFICATION_DIGESTS)


if __name__ == "__main__":
    unittest.main()
