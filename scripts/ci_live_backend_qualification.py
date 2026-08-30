#!/usr/bin/env python3
"""Run live backend qualification against one pinned provider cell.

This harness deliberately uses the production provider channels.  It records
observations first and lets the normal qualification verifier decide whether
the resulting evidence is admissible.  A successful process therefore emits a
candidate evidence root, never an authorization decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform as host_platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from wish_builder.adapters.providers import (  # noqa: E402
    CodexAppServerChannel,
    CodexAppServerConfig,
    CodexAppServerLaunch,
    JsonlRpcBackendChannel,
    JsonlRpcBackendConfig,
    JsonlRpcLaunch,
    JsonlRpcProtocol,
)
from wish_builder.adapters.trellis import (  # noqa: E402
    SUPPORTED_TRELLIS_EXPORT_VERSION,
    TrellisImportSettings,
    import_trellis_snapshot,
)
from wish_builder.compatibility import load_bundled_compatibility  # noqa: E402
from wish_builder.contracts import (  # noqa: E402
    BillingPosture,
    ExecutionBudgetPolicy,
    ExecutionIdentity,
    GateApproval,
    PathCaseMode,
    WorkerProvider,
    canonical_json_bytes,
    generated_task_packet_bytes,
)
from wish_builder.contracts.compatibility import Platform, Provider  # noqa: E402
from wish_builder.contracts.qualification_evidence import (  # noqa: E402
    QUALIFICATION_EVENT_GENESIS_DIGEST,
    QUALIFICATION_EVIDENCE_ROLE_ORDER,
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
    QualificationEventSource,
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
from wish_builder.contracts.runtime import (  # noqa: E402
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectRequestPayload,
    EffectStatus,
    JournalEvent,
    JournalEventType,
)
from wish_builder.services.backend_qualification_builder import (  # noqa: E402
    BackendQualificationCandidateError,
    verify_backend_qualification_candidate,
)
from wish_builder.services.journal import (  # noqa: E402
    AppendResult,
    AppendStatus,
    JournalHead,
)
from wish_builder.services.ports import (  # noqa: E402
    BackendCapabilities,
    CancelTurn,
    ChannelObservation,
    PreparedEffect,
    ReserveChannel,
    SendTaskPacket,
    TrellisGraphSnapshot,
    TurnObservation,
    TurnState,
)


HARNESS_VERSION = "1.0.0"
CRASH_CHILD_EXIT = 86
_WORKTREE_CLEANUP_RETRY_SECONDS = 5.0
_WORKTREE_CLEANUP_RETRY_INTERVAL_SECONDS = 0.05
_REVISION_LENGTHS = {40, 64}
_SDK_PINS: dict[Provider, tuple[str, str, str, str]] = {
    Provider.CODEX: (
        "@openai/codex",
        "0.149.0",
        "2e38d3859f52f288a86596d0c22366a10154437b",
        "sha512-i4dryj2Y1j+00Mb5n+0n71EYnTK9/KDc2cdFo/dXD0d1oTog2bhUssKDEIOnKmnEf51P0Z/HJTWvTKw/UHyOvQ==",
    ),
    Provider.PI: (
        "@earendil-works/pi-coding-agent",
        "0.84.2",
        "e4d4c1e769963c816959f5cea02a0a10ccc0495a",
        "sha512-l4E+B7hgXKWddRo8bC/eSue2aWZjEgJ9xIpf5p0Og+lq8a2TArCwJ0HCoCPCgaBP/tN4zbYH/wOwvx9pJpeLCA==",
    ),
    Provider.OMP: (
        "@oh-my-pi/pi-coding-agent",
        "18.0.11",
        "bbb5bf3e89b4b6a2eb692976109578071369378d",
        "sha512-3H90cCc+3yLtvSKM2RooIvkhG+77OFFoXD6+9GPZDF3PQ3FF6uCnPP57OaUa8VZ8YwOm9Eio5ZmfdFuvwLn+VA==",
    ),
}
_WORKER_PROVIDER = {
    Provider.CODEX: WorkerProvider.CODEX,
    Provider.PI: WorkerProvider.PI,
    Provider.OMP: WorkerProvider.OH_MY_PI,
}
_BASE_ENVIRONMENT = {
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SystemRoot",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
}


class LiveQualificationError(RuntimeError):
    """A controlled fail-closed harness error without secret-bearing details."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(code if not message else f"{code}: {message}")


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _platform() -> Platform:
    if os.name == "nt":
        return Platform.WINDOWS
    if os.name == "posix":
        return Platform.LINUX
    raise LiveQualificationError("unsupported_platform")


def _provider(value: str) -> Provider:
    try:
        return Provider(value)
    except ValueError as exc:
        raise LiveQualificationError("unsupported_provider") from exc


def _require_revision(value: str) -> str:
    if type(value) is not str or len(value) not in _REVISION_LENGTHS:
        raise LiveQualificationError("invalid_source_revision")
    if any(character not in "0123456789abcdef" for character in value):
        raise LiveQualificationError("invalid_source_revision")
    return value


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveQualificationError("invalid_provider_metadata") from exc
    if type(value) is not dict:
        raise LiveQualificationError("invalid_provider_metadata")
    return value


@dataclass(frozen=True, slots=True)
class SdkPin:
    provider: Provider
    name: str
    version: str
    shasum: str
    integrity: str
    package_root: Path
    cli_path: Path


def _resolve_sdk(root: Path, provider: Provider, args: argparse.Namespace) -> SdkPin:
    expected_name, expected_version, expected_shasum, expected_integrity = _SDK_PINS[
        provider
    ]
    supplied = (
        args.sdk_name,
        args.sdk_version,
        args.sdk_shasum,
        args.sdk_integrity,
    )
    if any(type(item) is not str or not item for item in supplied):
        raise LiveQualificationError("sdk_pin_required")
    if supplied != (
        expected_name,
        expected_version,
        expected_shasum,
        expected_integrity,
    ):
        raise LiveQualificationError("sdk_pin_not_supported")
    package_root = root.joinpath("node_modules", *expected_name.split("/"))
    package_json = _read_json(package_root / "package.json")
    if package_json.get("name") != expected_name or package_json.get("version") != expected_version:
        raise LiveQualificationError("sdk_package_mismatch")
    lock = _read_json(root / "package-lock.json")
    packages = lock.get("packages")
    key = "node_modules/" + expected_name
    entry = packages.get(key) if type(packages) is dict else None
    if type(entry) is not dict or entry.get("version") != expected_version or entry.get("integrity") != expected_integrity:
        raise LiveQualificationError("sdk_lock_pin_mismatch")
    cli_path: Path
    if provider in {Provider.PI, Provider.OMP}:
        cli_path = package_root / "dist" / "cli.js"
    else:
        raw_bin = package_json.get("bin")
        if type(raw_bin) is str:
            raw_bin = {"codex": raw_bin}
        if type(raw_bin) is not dict:
            raise LiveQualificationError("sdk_cli_missing")
        candidate = raw_bin.get("codex")
        if type(candidate) is not str or not candidate:
            candidate = next((item for item in raw_bin.values() if type(item) is str), None)
        if type(candidate) is not str:
            raise LiveQualificationError("sdk_cli_missing")
        cli_path = package_root / candidate
    try:
        cli_path = cli_path.resolve(strict=True)
    except OSError as exc:
        raise LiveQualificationError("sdk_cli_missing") from exc
    if not cli_path.is_file() or cli_path.is_symlink():
        raise LiveQualificationError("sdk_cli_missing")
    return SdkPin(
        provider,
        expected_name,
        expected_version,
        expected_shasum,
        expected_integrity,
        package_root.resolve(),
        cli_path,
    )


def _runtime(path: Path) -> Path:
    try:
        value = path.resolve(strict=True)
    except OSError as exc:
        raise LiveQualificationError("runtime_missing") from exc
    if not value.is_file() or value.is_symlink():
        raise LiveQualificationError("runtime_missing")
    return value


def _scrub_environment(names: tuple[str, ...]) -> None:
    """Keep only baseline process settings and explicitly named provider inputs."""

    allowed = _BASE_ENVIRONMENT | set(names)
    if os.name == "nt":
        folded = {key.casefold() for key in allowed}
        values = {
            key: value
            for key, value in os.environ.items()
            if key.casefold() in folded
        }
    else:
        values = {
            key: value for key, value in os.environ.items() if key in allowed
        }
    values.setdefault("PYTHONUTF8", "1")
    values.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.clear()
    os.environ.update(values)


def _provider_environment(names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise LiveQualificationError("provider_environment_missing")
    return tuple((name, os.environ[name]) for name in names)


def _capabilities(bundle: object, provider: Provider, platform: Platform) -> BackendCapabilities:
    worker = _WORKER_PROVIDER[provider]
    cell = bundle.platform(provider, platform)  # type: ignore[attr-defined]
    return BackendCapabilities(
        provider=worker,
        platform=platform.value,
        capability_digest=cell.capabilities.capability_digest,
        launch_profile_digest=cell.launch_profile_digest,
        policy_digest=bundle.policy_digest,
        max_task_packet_bytes=cell.capabilities.max_task_packet_bytes,
    )


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    sdk: SdkPin
    runtime: Path
    environment: tuple[tuple[str, str], ...]

    @property
    def command_prefix(self) -> tuple[str, ...]:
        return (str(self.runtime), str(self.sdk.cli_path))


def _launch_spec(
    sdk: SdkPin,
    runtime: Path,
    environment: tuple[tuple[str, str], ...],
) -> LaunchSpec:
    return LaunchSpec(sdk, runtime, environment)


def _make_channel(
    provider: Provider,
    capabilities: BackendCapabilities,
    launch: LaunchSpec,
    worktree: Path,
    state: Path,
    *,
    timeout: float,
) -> object:
    if provider is Provider.CODEX:
        native = CodexAppServerLaunch(
            command_prefix=launch.command_prefix,
            sdk_version=launch.sdk.version,
            sdk_shasum=launch.sdk.shasum,
            sdk_integrity=launch.sdk.integrity,
        )
        return CodexAppServerChannel(
            CodexAppServerConfig(
                capabilities=capabilities,
                launch=native,
                working_directory=worktree,
                state_directory=state,
                environment=launch.environment,
                response_timeout_seconds=timeout,
            )
        )
    native = JsonlRpcLaunch(
        provider=_WORKER_PROVIDER[provider],
        protocol=JsonlRpcProtocol.PI if provider is Provider.PI else JsonlRpcProtocol.OH_MY_PI_V2,
        command_prefix=launch.command_prefix,
        sdk_name=launch.sdk.name,
        sdk_version=launch.sdk.version,
    )
    return JsonlRpcBackendChannel(
        JsonlRpcBackendConfig(
            capabilities=capabilities,
            launch=native,
            working_directory=worktree,
            state_directory=state,
            environment=launch.environment,
            handshake_timeout_seconds=timeout,
            response_timeout_seconds=timeout,
        )
    )


def _pid(channel: object) -> int | None:
    value = getattr(channel, "process_id", None)
    return value if type(value) is int and value > 0 else None


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ("tasklist", "/FI", f"PID eq {pid}", "/NH"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        return str(pid) in result.stdout.decode("utf-8", errors="replace")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid(pid: int | None) -> None:
    if pid is None or not _pid_exists(pid):
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ("taskkill", "/PID", str(pid), "/T", "/F"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        else:
            os.killpg(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.monotonic() + 10
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)


def _prepared_effect(
    identity: ExecutionIdentity,
    command: object,
    operation: EffectOperation,
    number: int,
) -> PreparedEffect:
    object_type = {
        EffectOperation.RESERVE_CHANNEL: EffectObjectType.CHANNEL,
        EffectOperation.SEND_TASK_PACKET: EffectObjectType.TASK_PACKET,
        EffectOperation.CANCEL_TURN: EffectObjectType.TURN,
    }[operation]
    operation_id = getattr(command, "operation_id")
    request_identity = ExecutionIdentity(
        identity.run_id,
        identity.coordinator_epoch,
        identity.task_id,
        identity.attempt,
        operation_id,
    )
    payload = EffectRequestPayload(
        operation,
        AdapterKind.BACKEND,
        object_type,
        _digest(canonical_json_bytes(request_identity.to_primitive())),
        _digest(canonical_json_bytes(command.to_primitive())),
        0,
        request_identity.coordinator_epoch,
    )
    event = JournalEvent.create(
        sequence=1,
        event_id=f"LIVE-EFFECT-{number:06d}",
        event_type=JournalEventType.EFFECT_REQUESTED,
        identity=request_identity,
        actor_type=ActorType.COORDINATOR,
        actor_id="live-qualification",
        recorded_at=_utc_now(),
        previous_event_hash="sha256:" + "0" * 64,
        payload=payload,
    )
    return PreparedEffect.from_append_result(
        AppendResult(
            AppendStatus.COMMITTED,
            JournalHead(event.sequence, event.event_hash),
            event,
        ),
        command,
    )


def _wait_terminal(channel: object, operation_id: str, timeout: float) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observation = channel.inspect_turn(operation_id)  # type: ignore[attr-defined]
        if observation.state in {TurnState.DONE, TurnState.FAILED, TurnState.CANCELLED}:
            return observation
        time.sleep(0.05)
    raise LiveQualificationError("turn_terminal_timeout")


def _require_applied(observation: object, *, code: str) -> object:
    if getattr(observation, "status", None).value != "applied":
        raise LiveQualificationError(code)
    return observation


def _task_graph(parent_id: str, source_revision: str) -> tuple[bytes, str]:
    def command() -> dict[str, object]:
        return {
            "executable_profile": "python",
            "executable_identity_digest": _digest(b"qualification-python"),
            "argv": ["python", "-c", "pass"],
            "working_directory": ".",
            "timeout_seconds": 30,
            "stdout_limit_bytes": 65536,
            "stderr_limit_bytes": 65536,
            "result_limit_bytes": 65536,
            "environment_allowlist": ["PATH"],
            "network_policy": "denied",
            "display_text": "qualification check",
        }
    def task(source: str, requirement: str, wave: int, paths: tuple[str, ...], depends: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "id": source,
            "title": "Complete backend qualification probe without repository changes",
            "requirement_ids": [requirement],
            "depends_on": list(depends),
            "owned_paths": list(paths),
            "allowed_auxiliary_paths": [],
            "acceptance_criteria": ["The provider turn returns a structured completion."],
            "regression_commands": [command()],
            "rollback": "Delete the qualification worktree.",
            "documentation": ["docs/qualification.md"],
            "wave": wave,
            "risk": "low",
            "may_change_contracts": False,
            "instruction_context_digest": _digest((source + "-context").encode()),
            "approved_document_digests": [_digest((source + "-approval").encode())],
            "task_packet_template_digest": None,
        }
    requirements = [
            {"id": "REQ-001", "text": "Run a real provider turn", "status": "approved", "decision_ref": None},
            {"id": "REQ-002", "text": "Reconcile an accepted turn", "status": "approved", "decision_ref": None},
            {"id": "REQ-003", "text": "Run sibling turns concurrently", "status": "approved", "decision_ref": None},
        ]
    tasks = [
            task("trellis-qualification-foundation", "REQ-001", 0, ("qualification/foundation/**",)),
            task("trellis-qualification-alpha", "REQ-002", 1, ("qualification/alpha/**",), ("trellis-qualification-foundation",)),
            task("trellis-qualification-zeta", "REQ-003", 1, ("qualification/zeta/**",), ("trellis-qualification-foundation",)),
        ]
    revision = _digest(
        canonical_json_bytes(
            {
                "format": SUPPORTED_TRELLIS_EXPORT_VERSION,
                "parent_task_id": parent_id,
                "requirements": requirements,
                "source_revision": source_revision,
                "tasks": tasks,
            }
        )
    )
    payload = {
        "schema_version": 1,
        "parent_task_id": parent_id,
        "revision": revision,
        "requirements": requirements,
        "tasks": tasks,
    }
    return canonical_json_bytes(payload), revision


@dataclass(frozen=True, slots=True)
class PendingEvent:
    scenario: QualificationEvidenceScenario
    payload: object
    source: QualificationEventSource
    monotonic_ns: int
    process_identity: str
    recorded_at: str


class EventCollector:
    def __init__(self, run_id: str, provider: Provider, platform: Platform, host_boot_id: str) -> None:
        self.run_id = run_id
        self.provider = provider
        self.platform = platform
        self.host_boot_id = host_boot_id
        self._events: list[PendingEvent] = []
        self._lock = threading.Lock()

    def add(self, scenario: QualificationEvidenceScenario, payload: object, source: QualificationEventSource, process: str, *, monotonic_ns: int | None = None, recorded_at: str | None = None) -> None:
        event = PendingEvent(
            scenario,
            payload,
            source,
            time.monotonic_ns() if monotonic_ns is None else monotonic_ns,
            process,
            _utc_now() if recorded_at is None else recorded_at,
        )
        with self._lock:
            self._events.append(event)

    def segment(self, scenario: QualificationEvidenceScenario) -> tuple[PendingEvent, ...]:
        with self._lock:
            return tuple(item for item in self._events if item.scenario is scenario)

    def materialize(self, ordered: tuple[PendingEvent, ...]) -> tuple[QualificationEvent, ...]:
        result: list[QualificationEvent] = []
        previous = QUALIFICATION_EVENT_GENESIS_DIGEST
        previous_mono = 0
        for index, item in enumerate(ordered, start=1):
            mono = max(item.monotonic_ns, previous_mono + 1)
            event = QualificationEvent.create(
                sequence=index,
                qualification_run_id=self.run_id,
                scenario=item.scenario,
                provider=self.provider,
                platform=self.platform,
                source=item.source,
                event_type=item.payload.EVENT_TYPE,
                recorded_at=item.recorded_at,
                monotonic_ns=mono,
                host_boot_id=self.host_boot_id,
                process_identity=item.process_identity,
                payload=item.payload,
                previous_event_digest=previous,
            )
            result.append(event)
            previous = event.event_digest
            previous_mono = mono
        return tuple(result)


@dataclass
class Attempt:
    scenario: QualificationEvidenceScenario
    task_id: str
    trellis_task_id: str
    attempt_id: str
    dispatch_id: str
    channel_id: str
    message_id: str
    turn_id: str
    worktree_id: str
    worktree: Path
    state: Path
    identity: ExecutionIdentity
    packet: str
    owned_paths: tuple[str, ...]
    base_commit: str
    reserve: ReserveChannel
    send: SendTaskPacket
    channel: object | None = None
    provider_session_id: str | None = None
    provider_message_id: str | None = None
    provider_turn_id: str | None = None
    result_digest: str | None = None


class WorktreeManager:
    def __init__(self, workspace: Path, revision: str) -> None:
        self.workspace = workspace
        self.revision = revision
        self.root = Path(tempfile.mkdtemp(prefix="wish-builder-live-"))
        self.paths: set[Path] = set()
        try:
            top = subprocess.run(("git", "-C", str(workspace), "rev-parse", "--show-toplevel"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=10)
            if top.returncode != 0 or Path(top.stdout.decode().strip()).resolve() != workspace.resolve():
                raise LiveQualificationError("workspace_not_git_root")
            checked = subprocess.run(("git", "-C", str(workspace), "cat-file", "-e", revision + "^{commit}"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
            if checked.returncode != 0:
                raise LiveQualificationError("source_revision_unavailable")
        except (OSError, subprocess.SubprocessError) as exc:
            raise LiveQualificationError("git_probe_failed") from exc

    def add(self, name: str) -> Path:
        path = self.root / name
        try:
            result = subprocess.run(("git", "-C", str(self.workspace), "worktree", "add", "--detach", str(path), self.revision), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise LiveQualificationError("worktree_create_failed") from exc
        if result.returncode != 0 or not path.is_dir():
            raise LiveQualificationError("worktree_create_failed")
        self.paths.add(path)
        return path

    def remove(self, path: Path) -> None:
        if path not in self.paths and not path.exists():
            return
        subprocess.run(("git", "-C", str(self.workspace), "worktree", "remove", "--force", str(path)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=60)
        if path.exists():
            _remove_tree(path)
        self.paths.discard(path)

    def close(self) -> None:
        for path in tuple(self.paths):
            self.remove(path)
        _remove_tree(self.root)


def _remove_tree(path: Path) -> None:
    deadline = time.monotonic() + _WORKTREE_CLEANUP_RETRY_SECONDS
    while True:
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) != 32:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(_WORKTREE_CLEANUP_RETRY_INTERVAL_SECONDS, remaining))


def _attempt(
    manifest: object,
    task_id: str,
    scenario: QualificationEvidenceScenario,
    worktrees: WorktreeManager,
    number: int,
) -> Attempt:
    task = next(item for item in manifest.tasks if item.id == task_id)
    mapping = {item.task_id: item.trellis_task_id for item in manifest.task_id_mapping}
    trellis_id = mapping[task_id]
    suffix = scenario.value.upper().replace("_", "-") + "-" + str(number)
    attempt_id = "ATTEMPT-" + suffix
    dispatch_id = "DISPATCH-" + suffix
    channel_id = "CHANNEL-" + suffix
    message_id = "MESSAGE-" + suffix
    turn_id = "TURN-" + suffix
    worktree_id = "WORKTREE-" + suffix
    identity = ExecutionIdentity(manifest.run_id, 1, task_id, 1, dispatch_id)
    packet = generated_task_packet_bytes(manifest, task, trellis_id, identity).decode("utf-8")
    reserve = ReserveChannel(
        operation_id="RESERVE-" + suffix,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
        channel_id=channel_id,
        provider=manifest.provider,
        capability_digest=manifest.capability_digest,
        launch_profile_digest=manifest.launch_profile_digest,
        policy_digest=manifest.policy_digest,
    )
    send = SendTaskPacket(
        operation_id="SEND-" + suffix,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
        channel_id=channel_id,
        message_id=message_id,
        turn_id=turn_id,
        task_packet=packet,
        task_packet_digest=_digest(packet.encode("utf-8")),
    )
    worktree = worktrees.add("attempt-" + suffix)
    state = worktrees.root / ("state-" + suffix)
    return Attempt(
        scenario,
        task_id,
        trellis_id,
        attempt_id,
        dispatch_id,
        channel_id,
        message_id,
        turn_id,
        worktree_id,
        worktree,
        state,
        identity,
        packet,
        tuple(task.owned_paths + task.allowed_auxiliary_paths),
        worktrees.revision,
        reserve,
        send,
    )


def _build_manifest(
    *,
    provider: Provider,
    platform: Platform,
    bundle: object,
    run_id: str,
    source_revision: str,
    imported_at: str,
) -> tuple[bytes, bytes, object]:
    parent_id = "qualification/" + run_id.lower()
    snapshot_bytes, task_records_revision = _task_graph(parent_id, source_revision)
    snapshot = TrellisGraphSnapshot(
        export_version=SUPPORTED_TRELLIS_EXPORT_VERSION,
        trellis_version="0.6.15",
        parent_task_id=parent_id,
        revision=task_records_revision,
        observed_at=imported_at,
        snapshot_bytes=snapshot_bytes,
        source_sha256=_digest(snapshot_bytes),
        complete=True,
    )
    worker = _WORKER_PROVIDER[provider]
    cell = bundle.platform(provider, platform)  # type: ignore[attr-defined]
    budget = ExecutionBudgetPolicy(
        max_attempts_per_task=1,
        max_attempts_per_run=8,
        attempt_deadline_seconds=900,
        total_worker_seconds=7_200,
        max_output_bytes=8_388_608,
        max_retained_evidence_bytes=16_777_216,
        max_concurrent_workers=2,
        billing_posture=BillingPosture.PREAPPROVED,
    )
    settings = TrellisImportSettings(
        run_id=run_id,
        goal="Qualify one real coding-agent backend",
        base_branch="qualification",
        imported_at=imported_at,
        gate_a=GateApproval(
            approved_by="qualification-harness",
            approved_at=imported_at,
            artifact_hash=_digest(("gate-a:" + source_revision).encode("ascii")),
        ),
        provider=worker,
        capability_digest=cell.capabilities.capability_digest,
        launch_profile_digest=cell.launch_profile_digest,
        policy_digest=bundle.policy_digest,  # type: ignore[attr-defined]
        execution_budget=budget,
        max_concurrency=2,
        lease_ttl_seconds=90,
        lease_clock_skew_seconds=2,
        path_case_mode=(
            PathCaseMode.INSENSITIVE
            if platform is Platform.WINDOWS
            else PathCaseMode.SENSITIVE
        ),
        protected_paths=(),
    )
    result = import_trellis_snapshot(snapshot, settings)
    return snapshot_bytes, result.manifest.canonical_json_bytes(), result.manifest


def _run_started(
    *,
    harness_digest: str,
    manifest_digest: str,
    snapshot_digest: str,
    bundle: object,
    provider: Provider,
    platform: Platform,
    sdk: SdkPin,
    source_revision: str,
) -> RunStartedPayload:
    cell = bundle.platform(provider, platform)  # type: ignore[attr-defined]
    return RunStartedPayload(
        source_revision=source_revision,
        harness_digest=harness_digest,
        harness_version=HARNESS_VERSION,
        trellis_version="0.6.15",
        trellis_compatibility_digest=bundle.trellis_compatibility_digest,  # type: ignore[attr-defined]
        policy_digest=bundle.policy_digest,  # type: ignore[attr-defined]
        launch_profile_digest=cell.launch_profile_digest,
        capability_digest=cell.capabilities.capability_digest,
        manifest_digest=manifest_digest,
        trellis_snapshot_digest=snapshot_digest,
        sdk_name=sdk.name,
        sdk_version=sdk.version,
        sdk_shasum=sdk.shasum,
    )


def _artifact(role: QualificationEvidenceRole, path: str, raw: bytes, media_type: str) -> QualificationEvidenceArtifact:
    return QualificationEvidenceArtifact(
        role=role,
        path=path,
        digest=_digest(raw),
        byte_length=len(raw),
        media_type=media_type,
    )


def _materialize_evidence(
    root: Path,
    *,
    events: tuple[QualificationEvent, ...],
    harness: QualificationHarnessDescriptor,
    manifest_bytes: bytes,
    snapshot_bytes: bytes,
    run_id: str,
    provider: Provider,
    platform: Platform,
    provenance_kind: QualificationProvenanceKind,
    provenance_issuer: str,
    provenance_reference: str,
    provenance_identity: str,
    source_revision: str,
) -> None:
    event_bytes = qualification_event_log_bytes(events)
    paths = {
        QualificationEvidenceRole.EVENT_LOG: "events.jsonl",
        QualificationEvidenceRole.HARNESS_DESCRIPTOR: "harness.json",
        QualificationEvidenceRole.EXECUTION_MANIFEST: "execution-manifest.json",
        QualificationEvidenceRole.TRELLIS_SNAPSHOT: "trellis-snapshot.json",
        QualificationEvidenceRole.PROVENANCE: "provenance.json",
    }
    media = {
        QualificationEvidenceRole.EVENT_LOG: "application/x-ndjson",
        QualificationEvidenceRole.HARNESS_DESCRIPTOR: "application/json",
        QualificationEvidenceRole.EXECUTION_MANIFEST: "application/json",
        QualificationEvidenceRole.TRELLIS_SNAPSHOT: "application/json",
        QualificationEvidenceRole.PROVENANCE: "application/json",
    }
    raw_by_role = {
        QualificationEvidenceRole.EVENT_LOG: event_bytes,
        QualificationEvidenceRole.HARNESS_DESCRIPTOR: harness.canonical_json_bytes(),
        QualificationEvidenceRole.EXECUTION_MANIFEST: manifest_bytes,
        QualificationEvidenceRole.TRELLIS_SNAPSHOT: snapshot_bytes,
    }
    non_provenance = tuple(
        _artifact(role, paths[role], raw_by_role[role], media[role])
        for role in QUALIFICATION_SCENARIO_EVIDENCE_ROLES
    )
    provenance = QualificationProvenance(
        schema_version=1,
        kind=provenance_kind,
        issuer=provenance_issuer,
        reference=provenance_reference,
        identity=provenance_identity,
        source_revision=source_revision,
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
        media[QualificationEvidenceRole.PROVENANCE],
    )
    inventory = QualificationEvidenceInventory(
        schema_version=1,
        qualification_run_id=run_id,
        provider=provider,
        platform=platform,
        artifacts=non_provenance + (provenance_artifact,),
    )
    root.mkdir(parents=True, exist_ok=False)
    for role in QUALIFICATION_EVIDENCE_ROLE_ORDER:
        (root / paths[role]).write_bytes(raw_by_role[role])
    (root / "inventory.json").write_bytes(inventory.canonical_json_bytes())


QUALIFICATION_SCENARIO_EVIDENCE_ROLES = QUALIFICATION_EVIDENCE_ROLE_ORDER[:-1]


def _record_prepare(collector: EventCollector, attempt: Attempt, process: str) -> None:
    prepare = PrepareRequestedPayload(
        "PREPARE-" + attempt.dispatch_id,
        attempt.dispatch_id,
        attempt.attempt_id,
        attempt.task_id,
        attempt.trellis_task_id,
        attempt.worktree_id,
        attempt.base_commit,
        attempt.owned_paths,
    )
    collector.add(attempt.scenario, prepare, QualificationEventSource.WISH_BUILDER, process)
    collector.add(
        attempt.scenario,
        AttemptPreparedPayload(
            prepare.operation_id,
            prepare.dispatch_id,
            prepare.attempt_id,
            prepare.task_id,
            prepare.trellis_task_id,
            prepare.worktree_id,
            prepare.base_commit,
            prepare.owned_paths,
        ),
        QualificationEventSource.WISH_BUILDER,
        process,
    )


def _start_attempt(
    collector: EventCollector,
    attempt: Attempt,
    channel: object,
    process: str,
    number: int,
) -> object:
    collector.add(
        attempt.scenario,
        ReserveRequestedPayload(
            attempt.reserve.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
        ),
        QualificationEventSource.WISH_BUILDER,
        process,
    )
    reserved = _require_applied(
        channel.reserve(
            _prepared_effect(
                attempt.identity,
                attempt.reserve,
                EffectOperation.RESERVE_CHANNEL,
                number,
            )
        ),
        code="reserve_not_applied",
    )
    attempt.provider_session_id = reserved.provider_session_id
    collector.add(
        attempt.scenario,
        ChannelReservedPayload(
            attempt.reserve.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            reserved.provider_session_id,
        ),
        QualificationEventSource.PROVIDER,
        process,
    )
    collector.add(
        attempt.scenario,
        SendRequestedPayload(
            attempt.send.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            attempt.packet,
            attempt.send.task_packet_digest,
        ),
        QualificationEventSource.WISH_BUILDER,
        process,
    )
    sent = _require_applied(
        channel.send(
            _prepared_effect(
                attempt.identity,
                attempt.send,
                EffectOperation.SEND_TASK_PACKET,
                number + 1,
            )
        ),
        code="send_not_applied",
    )
    if sent.message_id is None or sent.turn_id is None:
        raise LiveQualificationError("send_identity_missing")
    attempt.provider_message_id = sent.message_id
    attempt.provider_turn_id = sent.turn_id
    collector.add(
        attempt.scenario,
        TaskPacketSentPayload(
            attempt.send.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            reserved.provider_session_id,
            sent.message_id,
            attempt.packet,
            attempt.send.task_packet_digest,
        ),
        QualificationEventSource.PROVIDER,
        process,
    )
    collector.add(
        attempt.scenario,
        TurnStartedPayload(
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            reserved.provider_session_id,
            sent.message_id,
            sent.turn_id,
        ),
        QualificationEventSource.PROVIDER,
        process,
    )
    return sent


def _finish_attempt(
    collector: EventCollector,
    attempt: Attempt,
    channel: object,
    sent: object,
    process: str,
    timeout: float,
    *,
    terminal_state: QualificationTurnTerminalState = QualificationTurnTerminalState.DONE,
) -> object:
    terminal = sent if sent.state in {TurnState.DONE, TurnState.FAILED, TurnState.CANCELLED} else _wait_terminal(channel, attempt.send.operation_id, timeout)
    expected = {
        QualificationTurnTerminalState.DONE: TurnState.DONE,
        QualificationTurnTerminalState.CANCELLED: TurnState.CANCELLED,
        QualificationTurnTerminalState.FAILED: TurnState.FAILED,
    }[terminal_state]
    if terminal.state is not expected:
        raise LiveQualificationError("turn_terminal_state_mismatch")
    attempt.result_digest = terminal.result_digest
    if terminal_state is QualificationTurnTerminalState.DONE and attempt.result_digest is None:
        raise LiveQualificationError("turn_result_missing")
    assert attempt.provider_session_id is not None
    assert attempt.provider_message_id is not None
    assert attempt.provider_turn_id is not None
    collector.add(
        attempt.scenario,
        TurnTerminalPayload(
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            attempt.provider_session_id,
            attempt.provider_message_id,
            attempt.provider_turn_id,
            terminal_state,
            attempt.result_digest,
        ),
        QualificationEventSource.PROVIDER,
        process,
    )
    return terminal


def _scenario_bounds(
    collector: EventCollector,
    scenario: QualificationEvidenceScenario,
    run_start: RunStartedPayload,
    process: str,
) -> None:
    collector.add(scenario, run_start, QualificationEventSource.RUNNER, process)


def _scenario_finish(
    collector: EventCollector,
    scenario: QualificationEvidenceScenario,
    process: str,
) -> None:
    collector.add(
        scenario,
        RunFinishedPayload(QualificationRunOutcome.COMPLETED),
        QualificationEventSource.RUNNER,
        process,
    )


def _reserve_only(
    collector: EventCollector,
    attempt: Attempt,
    channel: object,
    process: str,
    number: int,
) -> object:
    collector.add(
        attempt.scenario,
        ReserveRequestedPayload(
            attempt.reserve.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
        ),
        QualificationEventSource.WISH_BUILDER,
        process,
    )
    reserved = _require_applied(
        channel.reserve(
            _prepared_effect(attempt.identity, attempt.reserve, EffectOperation.RESERVE_CHANNEL, number)
        ),
        code="reserve_not_applied",
    )
    attempt.provider_session_id = reserved.provider_session_id
    collector.add(
        attempt.scenario,
        ChannelReservedPayload(
            attempt.reserve.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            reserved.provider_session_id,
        ),
        QualificationEventSource.PROVIDER,
        process,
    )
    return reserved


def _send_only(
    collector: EventCollector,
    attempt: Attempt,
    channel: object,
    process: str,
    number: int,
) -> object:
    assert attempt.provider_session_id is not None
    collector.add(
        attempt.scenario,
        SendRequestedPayload(
            attempt.send.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            attempt.packet,
            attempt.send.task_packet_digest,
        ),
        QualificationEventSource.WISH_BUILDER,
        process,
    )
    sent = _require_applied(
        channel.send(
            _prepared_effect(attempt.identity, attempt.send, EffectOperation.SEND_TASK_PACKET, number)
        ),
        code="send_not_applied",
    )
    if sent.message_id is None or sent.turn_id is None:
        raise LiveQualificationError("send_identity_missing")
    attempt.provider_message_id = sent.message_id
    attempt.provider_turn_id = sent.turn_id
    collector.add(
        attempt.scenario,
        TaskPacketSentPayload(
            attempt.send.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            attempt.provider_session_id,
            sent.message_id,
            attempt.packet,
            attempt.send.task_packet_digest,
        ),
        QualificationEventSource.PROVIDER,
        process,
    )
    collector.add(
        attempt.scenario,
        TurnStartedPayload(
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
            attempt.provider_session_id,
            sent.message_id,
            sent.turn_id,
        ),
        QualificationEventSource.PROVIDER,
        process,
    )
    return sent


def _primitive_command(value: object) -> dict[str, object]:
    raw = value.to_primitive()  # type: ignore[attr-defined]
    if type(raw) is not dict:
        raise LiveQualificationError("crash_command_invalid")
    return raw


def _crash_child_config(
    path: Path,
    *,
    provider: Provider,
    platform: Platform,
    sdk: SdkPin,
    runtime: Path,
    environment_names: tuple[str, ...],
    capabilities: BackendCapabilities,
    attempt: Attempt,
    output: Path,
    timeout: float,
) -> None:
    body = {
        "provider": provider.value,
        "platform": platform.value,
        "sdk": {
            "name": sdk.name,
            "version": sdk.version,
            "shasum": sdk.shasum,
            "integrity": sdk.integrity,
            "cli": str(sdk.cli_path),
        },
        "runtime": str(runtime),
        "environmentNames": list(environment_names),
        "capabilities": capabilities.to_primitive(),
        "attempt": {
            "runId": attempt.identity.run_id,
            "epoch": attempt.identity.coordinator_epoch,
            "taskId": attempt.task_id,
            "attempt": attempt.identity.attempt,
            "dispatchId": attempt.dispatch_id,
            "reserve": _primitive_command(attempt.reserve),
            "send": _primitive_command(attempt.send),
            "worktree": str(attempt.worktree),
            "state": str(attempt.state),
        },
        "output": str(output),
        "timeoutMilliseconds": math.ceil(timeout * 1_000),
    }
    path.write_bytes(canonical_json_bytes(body))


def _decode_crash_observation(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveQualificationError("crash_observation_missing") from exc
    if type(value) is not dict or type(value.get("reserved")) is not dict or type(value.get("sent")) is not dict:
        raise LiveQualificationError("crash_observation_invalid")
    return value


def _run_crash_child(path: Path) -> int:
    """Run one accepted turn and terminate abruptly before coordinator journal."""

    try:
        config = json.loads(path.read_bytes())
        provider = Provider(config["provider"])
        platform = Platform(config["platform"])
        sdk_raw = config["sdk"]
        sdk = SdkPin(
            provider,
            sdk_raw["name"],
            sdk_raw["version"],
            sdk_raw["shasum"],
            sdk_raw["integrity"],
            Path(sdk_raw["cli"]).parent,
            Path(sdk_raw["cli"]),
        )
        capabilities_raw = config["capabilities"]
        capability_kwargs: dict[str, object] = {
            "provider": _WORKER_PROVIDER[provider],
            "platform": capabilities_raw["platform"],
            "capability_digest": capabilities_raw["capability_digest"],
            "launch_profile_digest": capabilities_raw["launch_profile_digest"],
            "policy_digest": capabilities_raw["policy_digest"],
            "max_task_packet_bytes": capabilities_raw["max_task_packet_bytes"],
        }
        capabilities = BackendCapabilities(**capability_kwargs)  # type: ignore[arg-type]
        launch = LaunchSpec(
            sdk,
            Path(config["runtime"]),
            _provider_environment(tuple(config["environmentNames"])),
        )
        attempt_raw = config["attempt"]
        identity = ExecutionIdentity(
            attempt_raw["runId"],
            attempt_raw["epoch"],
            attempt_raw["taskId"],
            attempt_raw["attempt"],
            attempt_raw["dispatchId"],
        )
        reserve_raw = attempt_raw["reserve"]
        reserve = ReserveChannel(
            operation_id=reserve_raw["operation_id"],
            attempt_id=reserve_raw["attempt_id"],
            dispatch_id=reserve_raw["dispatch_id"],
            channel_id=reserve_raw["channel_id"],
            provider=WorkerProvider(reserve_raw["provider"]),
            capability_digest=reserve_raw["capability_digest"],
            launch_profile_digest=reserve_raw["launch_profile_digest"],
            policy_digest=reserve_raw["policy_digest"],
        )
        send_raw = attempt_raw["send"]
        send = SendTaskPacket(
            operation_id=send_raw["operation_id"],
            attempt_id=send_raw["attempt_id"],
            dispatch_id=send_raw["dispatch_id"],
            channel_id=send_raw["channel_id"],
            message_id=send_raw["message_id"],
            turn_id=send_raw["turn_id"],
            task_packet=send_raw["task_packet"],
            task_packet_digest=send_raw["task_packet_digest"],
        )
        channel = _make_channel(
            provider,
            capabilities,
            launch,
            Path(attempt_raw["worktree"]),
            Path(attempt_raw["state"]),
            timeout=config["timeoutMilliseconds"] / 1_000,
        )
        reserved = _require_applied(channel.reserve(_prepared_effect(identity, reserve, EffectOperation.RESERVE_CHANNEL, 5001)), code="crash_reserve_not_applied")
        sent = _require_applied(channel.send(_prepared_effect(identity, send, EffectOperation.SEND_TASK_PACKET, 5002)), code="crash_send_not_applied")
        sent = _require_applied(
            _wait_terminal(
                channel,
                send.operation_id,
                config["timeoutMilliseconds"] / 1_000,
            ),
            code="crash_turn_not_applied",
        )
        if sent.state is not TurnState.DONE or sent.result_digest is None:
            raise LiveQualificationError("crash_turn_not_done")
        if sent.message_id is None or sent.turn_id is None:
            raise LiveQualificationError("crash_child_send_identity_missing")
        result = {
            "reserved": reserved.to_primitive(),
            "sent": sent.to_primitive(),
            "providerPid": _pid(channel),
        }
        output = Path(config["output"])
        with output.open("wb") as stream:
            stream.write(canonical_json_bytes(result))
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(CRASH_CHILD_EXIT)
    except LiveQualificationError:
        return 2
    except Exception:
        return 3


class LiveRunner:
    def __init__(
        self,
        *,
        run_id: str,
        source_revision: str,
        provider: Provider,
        platform: Platform,
        bundle: object,
        sdk: SdkPin,
        launch: LaunchSpec,
        environment_names: tuple[str, ...],
        manifest: object,
        worktrees: WorktreeManager,
        run_start: RunStartedPayload,
        timeout: float,
    ) -> None:
        self.run_id = run_id
        self.source_revision = source_revision
        self.provider = provider
        self.platform = platform
        self.bundle = bundle
        self.sdk = sdk
        self.launch = launch
        self.environment_names = environment_names
        self.manifest = manifest
        self.worktrees = worktrees
        self.run_start = run_start
        self.timeout = timeout
        self.capabilities = _capabilities(bundle, provider, platform)
        host = host_platform.node().encode("utf-8", errors="replace")
        self.collector = EventCollector(
            run_id,
            provider,
            platform,
            "host-" + hashlib.sha256(host).hexdigest()[:24],
        )
        self._effect_number = 10

    def _number(self) -> int:
        self._effect_number += 10
        return self._effect_number

    def _channel(self, attempt: Attempt) -> object:
        channel = _make_channel(
            self.provider,
            self.capabilities,
            self.launch,
            attempt.worktree,
            attempt.state,
            timeout=self.timeout,
        )
        attempt.channel = channel
        return channel

    def _new_attempt(
        self,
        scenario: QualificationEvidenceScenario,
        task_id: str,
        number: int,
    ) -> Attempt:
        return _attempt(
            self.manifest,
            task_id,
            scenario,
            self.worktrees,
            number,
        )

    def _dispose(self, attempt: Attempt, *, remove_worktree: bool = True) -> None:
        channel = attempt.channel
        pid = None if channel is None else _pid(channel)
        if channel is not None:
            try:
                channel.cleanup(remove_durable_state=True)
            except Exception:
                try:
                    channel.close()
                except Exception:
                    pass
        _terminate_pid(pid)
        if remove_worktree:
            self.worktrees.remove(attempt.worktree)

    def full_turn(self) -> None:
        scenario = QualificationEvidenceScenario.FULL_TURN
        process = "qualifier-main"
        _scenario_bounds(self.collector, scenario, self.run_start, process)
        attempt = self._new_attempt(scenario, "TASK-001", 1)
        channel = self._channel(attempt)
        try:
            _record_prepare(self.collector, attempt, process)
            sent = _start_attempt(
                self.collector, attempt, channel, process, self._number()
            )
            _finish_attempt(
                self.collector,
                attempt,
                channel,
                sent,
                process,
                self.timeout,
            )
        finally:
            self._dispose(attempt)
        _scenario_finish(self.collector, scenario, process)

    def cancellation(self) -> None:
        scenario = QualificationEvidenceScenario.ACTIVE_TURN_CANCELLATION
        process = "qualifier-main"
        _scenario_bounds(self.collector, scenario, self.run_start, process)
        attempt = self._new_attempt(scenario, "TASK-001", 1)
        channel = self._channel(attempt)
        try:
            _record_prepare(self.collector, attempt, process)
            sent = _start_attempt(
                self.collector, attempt, channel, process, self._number()
            )
            if sent.state in {TurnState.DONE, TurnState.FAILED, TurnState.CANCELLED}:
                raise LiveQualificationError("cancellation_turn_not_active")
            assert attempt.provider_session_id is not None
            assert attempt.provider_message_id is not None
            assert attempt.provider_turn_id is not None
            cancel = CancelTurn(
                operation_id="CANCEL-" + attempt.dispatch_id,
                attempt_id=attempt.attempt_id,
                channel_id=attempt.channel_id,
                turn_id=attempt.turn_id,
                reason_code="qualification_active_cancel",
            )
            requested = CancelRequestedPayload(
                cancel.operation_id,
                attempt.dispatch_id,
                attempt.attempt_id,
                attempt.task_id,
                attempt.channel_id,
                attempt.provider_session_id,
                attempt.provider_message_id,
                attempt.provider_turn_id,
            )
            self.collector.add(
                scenario,
                requested,
                QualificationEventSource.WISH_BUILDER,
                process,
            )
            observed = _require_applied(
                channel.cancel(
                    _prepared_effect(
                        attempt.identity,
                        cancel,
                        EffectOperation.CANCEL_TURN,
                        self._number(),
                    )
                ),
                code="cancel_not_applied",
            )
            if observed.state is not TurnState.CANCELLED:
                raise LiveQualificationError("cancel_not_observed")
            self.collector.add(
                scenario,
                CancelObservedPayload(
                    requested.operation_id,
                    requested.dispatch_id,
                    requested.attempt_id,
                    requested.task_id,
                    requested.channel_id,
                    requested.provider_session_id,
                    requested.provider_message_id,
                    requested.provider_turn_id,
                    QualificationEffectStatus.APPLIED,
                ),
                QualificationEventSource.PROVIDER,
                process,
            )
            _finish_attempt(
                self.collector,
                attempt,
                channel,
                sent,
                process,
                self.timeout,
                terminal_state=QualificationTurnTerminalState.CANCELLED,
            )
        finally:
            self._dispose(attempt)
        _scenario_finish(self.collector, scenario, process)

    def crash_reconcile(self) -> None:
        scenario = QualificationEvidenceScenario.CRASH_RECONCILE
        parent_process = "qualifier-restarted"
        child_process = "qualifier-before-crash"
        _scenario_bounds(self.collector, scenario, self.run_start, child_process)
        attempt = self._new_attempt(scenario, "TASK-001", 1)
        _record_prepare(self.collector, attempt, child_process)
        reserve_requested = ReserveRequestedPayload(
            attempt.reserve.operation_id,
            attempt.dispatch_id,
            attempt.attempt_id,
            attempt.task_id,
            attempt.channel_id,
        )
        self.collector.add(
            scenario,
            reserve_requested,
            QualificationEventSource.WISH_BUILDER,
            child_process,
        )
        config_path = self.worktrees.root / "crash-child.json"
        observation_path = self.worktrees.root / "crash-observation.json"
        _crash_child_config(
            config_path,
            provider=self.provider,
            platform=self.platform,
            sdk=self.sdk,
            runtime=self.launch.runtime,
            environment_names=self.environment_names,
            capabilities=self.capabilities,
            attempt=attempt,
            output=observation_path,
            timeout=self.timeout,
        )
        child_pid: int | None = None
        restarted: object | None = None
        try:
            try:
                completed = subprocess.run(
                    (
                        str(Path(sys.executable).resolve()),
                        str(Path(__file__).resolve()),
                        "_crash-child",
                        "--config",
                        str(config_path),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=dict(os.environ),
                    check=False,
                    timeout=self.timeout + 30,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise LiveQualificationError("crash_child_failed") from exc
            if completed.returncode != CRASH_CHILD_EXIT:
                raise LiveQualificationError("crash_child_failed")
            raw = _decode_crash_observation(observation_path)
            reserved_raw = raw["reserved"]
            sent_raw = raw["sent"]
            assert type(reserved_raw) is dict and type(sent_raw) is dict
            reserved = ChannelObservation(
                operation_id=reserved_raw["operation_id"],
                status=EffectStatus(reserved_raw["status"]),
                observed_at=reserved_raw["observed_at"],
                effect_digest=reserved_raw.get("effect_digest"),
                attempt_id=reserved_raw.get("attempt_id"),
                channel_id=reserved_raw.get("channel_id"),
                provider=WorkerProvider(reserved_raw["provider"]),
                provider_session_id=reserved_raw.get("provider_session_id"),
                evidence=tuple(reserved_raw.get("evidence", ())),
            )
            sent = TurnObservation(
                operation_id=sent_raw["operation_id"],
                status=EffectStatus(sent_raw["status"]),
                observed_at=sent_raw["observed_at"],
                state=TurnState(sent_raw["state"]),
                effect_digest=sent_raw.get("effect_digest"),
                attempt_id=sent_raw.get("attempt_id"),
                channel_id=sent_raw.get("channel_id"),
                message_id=sent_raw.get("message_id"),
                turn_id=sent_raw.get("turn_id"),
                result_digest=sent_raw.get("result_digest"),
                evidence=tuple(sent_raw.get("evidence", ())),
            )
            _require_applied(reserved, code="crash_reserve_not_applied")
            _require_applied(sent, code="crash_send_not_applied")
            child_pid = raw.get("providerPid") if type(raw.get("providerPid")) is int else None
            if child_pid is None:
                raise LiveQualificationError("crash_provider_process_missing")
            attempt.provider_session_id = reserved.provider_session_id
            attempt.provider_message_id = sent.message_id
            attempt.provider_turn_id = sent.turn_id
            assert attempt.provider_session_id is not None
            assert attempt.provider_message_id is not None
            assert attempt.provider_turn_id is not None
            self.collector.add(
                scenario,
                ChannelReservedPayload(
                    attempt.reserve.operation_id,
                    attempt.dispatch_id,
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.channel_id,
                    attempt.provider_session_id,
                ),
                QualificationEventSource.PROVIDER,
                child_process,
            )
            send_requested = SendRequestedPayload(
                attempt.send.operation_id,
                attempt.dispatch_id,
                attempt.attempt_id,
                attempt.task_id,
                attempt.channel_id,
                attempt.packet,
                attempt.send.task_packet_digest,
            )
            self.collector.add(
                scenario,
                send_requested,
                QualificationEventSource.WISH_BUILDER,
                child_process,
            )
            self.collector.add(
                scenario,
                TaskPacketSentPayload(
                    attempt.send.operation_id,
                    attempt.dispatch_id,
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.channel_id,
                    attempt.provider_session_id,
                    attempt.provider_message_id,
                    attempt.packet,
                    attempt.send.task_packet_digest,
                ),
                QualificationEventSource.PROVIDER,
                child_process,
            )
            self.collector.add(
                scenario,
                TurnStartedPayload(
                    attempt.dispatch_id,
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.channel_id,
                    attempt.provider_session_id,
                    attempt.provider_message_id,
                    attempt.provider_turn_id,
                ),
                QualificationEventSource.PROVIDER,
                child_process,
            )
            crash = CrashInjectedPayload(
                "after-send-before-journal",
                attempt.send.operation_id,
                attempt.dispatch_id,
                attempt.attempt_id,
                attempt.task_id,
                attempt.channel_id,
                attempt.provider_session_id,
                attempt.provider_message_id,
                attempt.provider_turn_id,
            )
            self.collector.add(
                scenario,
                crash,
                QualificationEventSource.WISH_BUILDER,
                child_process,
            )
            self.collector.add(
                scenario,
                ProcessRestartedPayload(child_process, "RECOVERY-CRASH-001"),
                QualificationEventSource.RUNNER,
                parent_process,
            )
            settle_deadline = time.monotonic() + self.timeout
            while _pid_exists(child_pid) and time.monotonic() < settle_deadline:
                time.sleep(0.05)
            if _pid_exists(child_pid):
                raise LiveQualificationError("crash_provider_process_not_settled")
            time.sleep(min(0.25, self.timeout))
            request_digest = _digest(canonical_json_bytes(send_requested.to_primitive()))
            reconcile = ReconcileRequestedPayload(
                attempt.send.operation_id,
                request_digest,
                attempt.dispatch_id,
                attempt.attempt_id,
                attempt.task_id,
                attempt.channel_id,
                attempt.provider_session_id,
                attempt.provider_message_id,
                attempt.provider_turn_id,
            )
            self.collector.add(
                scenario,
                reconcile,
                QualificationEventSource.WISH_BUILDER,
                parent_process,
            )
            restarted = self._channel(attempt)
            reconciled = restarted.inspect_turn(attempt.send.operation_id)
            _require_applied(reconciled, code="reconcile_not_applied")
            if reconciled.state is not TurnState.DONE or reconciled.result_digest is None:
                raise LiveQualificationError("reconcile_not_done")
            self.collector.add(
                scenario,
                ReconcileInspectedPayload(
                    reconcile.operation_id,
                    reconcile.request_digest,
                    reconcile.dispatch_id,
                    reconcile.attempt_id,
                    reconcile.task_id,
                    reconcile.channel_id,
                    reconcile.provider_session_id,
                    reconcile.provider_message_id,
                    reconcile.provider_turn_id,
                    QualificationEffectStatus.APPLIED,
                    QualificationTurnState.DONE,
                    reconciled.result_digest,
                ),
                QualificationEventSource.PROVIDER,
                parent_process,
            )
            self.collector.add(
                scenario,
                TurnTerminalPayload(
                    attempt.dispatch_id,
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.channel_id,
                    attempt.provider_session_id,
                    attempt.provider_message_id,
                    attempt.provider_turn_id,
                    QualificationTurnTerminalState.DONE,
                    reconciled.result_digest,
                ),
                QualificationEventSource.PROVIDER,
                parent_process,
            )
        finally:
            if restarted is not None:
                attempt.channel = restarted
            self._dispose(attempt)
            _terminate_pid(child_pid)
        _scenario_finish(self.collector, scenario, parent_process)

    def cleanup(self) -> None:
        scenario = QualificationEvidenceScenario.CLEANUP
        process = "qualifier-main"
        _scenario_bounds(self.collector, scenario, self.run_start, process)
        target = self._new_attempt(scenario, "TASK-001", 1)
        sibling = self._new_attempt(scenario, "TASK-003", 2)
        target_channel = self._channel(target)
        sibling_channel = self._channel(sibling)
        target_removed = False
        try:
            # The sibling reservation is an external control resource.  It is
            # intentionally omitted from the scenario trace, which permits
            # only the target attempt, but is checked before and after cleanup.
            sibling_reserved = _require_applied(
                sibling_channel.reserve(
                    _prepared_effect(
                        sibling.identity,
                        sibling.reserve,
                        EffectOperation.RESERVE_CHANNEL,
                        self._number(),
                    )
                ),
                code="cleanup_sibling_reserve_failed",
            )
            sibling.provider_session_id = sibling_reserved.provider_session_id
            sibling_pid = _pid(sibling_channel)
            if sibling_pid is None:
                raise LiveQualificationError("cleanup_sibling_process_missing")

            _record_prepare(self.collector, target, process)
            sent = _start_attempt(
                self.collector,
                target,
                target_channel,
                process,
                self._number(),
            )
            _finish_attempt(
                self.collector,
                target,
                target_channel,
                sent,
                process,
                self.timeout,
            )
            target_pid = _pid(target_channel)
            if target_pid is None:
                raise LiveQualificationError("cleanup_target_process_missing")
            assert target.provider_session_id is not None
            target_process_ids = ("pid-" + str(target_pid),)
            request = CleanupRequestedPayload(
                "CLEANUP-" + target.dispatch_id,
                target.dispatch_id,
                target.attempt_id,
                target.task_id,
                target.channel_id,
                target.provider_session_id,
                target.worktree_id,
                target_process_ids,
            )
            self.collector.add(
                scenario,
                request,
                QualificationEventSource.WISH_BUILDER,
                process,
            )
            target_resources = (
                "channel:" + target.channel_id,
                "process:" + target_process_ids[0],
                "provider_session:" + target.provider_session_id,
                "worktree:" + target.worktree_id,
            )
            sibling_resources = (
                "channel:" + sibling.channel_id,
                "process:pid-" + str(sibling_pid),
                "provider_session:" + sibling_reserved.provider_session_id,
                "worktree:" + sibling.worktree_id,
            )
            removed = set(target_channel.cleanup(remove_durable_state=True))
            if not {"provider_process_tree", "provider_session_state"} <= removed:
                raise LiveQualificationError("cleanup_adapter_incomplete")
            self.worktrees.remove(target.worktree)
            target_removed = True
            deadline = time.monotonic() + 10
            while _pid_exists(target_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            sibling_after = sibling_channel.inspect_reservation(
                sibling.reserve.operation_id
            )
            if (
                _pid_exists(target_pid)
                or target.state.exists()
                or target.worktree.exists()
                or sibling_after.status is not EffectStatus.APPLIED
                or not _pid_exists(sibling_pid)
            ):
                raise LiveQualificationError("cleanup_isolation_not_observed")
            self.collector.add(
                scenario,
                CleanupObservedPayload(
                    request.operation_id,
                    request.dispatch_id,
                    request.attempt_id,
                    request.task_id,
                    request.channel_id,
                    request.provider_session_id,
                    request.worktree_id,
                    request.process_tree_ids,
                    tuple(sorted(target_resources + sibling_resources)),
                    tuple(sorted(sibling_resources)),
                ),
                QualificationEventSource.WISH_BUILDER,
                process,
            )
        finally:
            if not target_removed:
                self._dispose(target)
            else:
                target.channel = None
            self._dispose(sibling)
        _scenario_finish(self.collector, scenario, process)

    def overlap(self) -> None:
        scenario = QualificationEvidenceScenario.SIBLING_OVERLAP
        process = "qualifier-main"
        _scenario_bounds(self.collector, scenario, self.run_start, process)
        attempts = (
            self._new_attempt(scenario, "TASK-001", 1),
            self._new_attempt(scenario, "TASK-003", 2),
        )
        channels = tuple(self._channel(item) for item in attempts)
        errors: list[BaseException] = []
        try:
            for index, (attempt, channel) in enumerate(
                zip(attempts, channels, strict=True), start=1
            ):
                _record_prepare(self.collector, attempt, process)
                _reserve_only(
                    self.collector,
                    attempt,
                    channel,
                    process,
                    self._number() + index,
                )

            send_effect_numbers = tuple(
                self._number() + index for index in range(len(attempts))
            )

            def run(index: int) -> None:
                try:
                    attempt = attempts[index]
                    channel = channels[index]
                    sent = _send_only(
                        self.collector,
                        attempt,
                        channel,
                        "qualifier-overlap-" + str(index + 1),
                        send_effect_numbers[index],
                    )
                    _finish_attempt(
                        self.collector,
                        attempt,
                        channel,
                        sent,
                        "qualifier-overlap-" + str(index + 1),
                        self.timeout,
                    )
                except BaseException as exc:
                    errors.append(exc)

            workers = tuple(
                threading.Thread(
                    target=run,
                    args=(index,),
                    name="live-qualification-overlap-" + str(index + 1),
                )
                for index in range(2)
            )
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=self.timeout + 15)
            if any(worker.is_alive() for worker in workers):
                raise LiveQualificationError("overlap_worker_timeout")
            if errors:
                error = errors[0]
                if isinstance(error, LiveQualificationError):
                    raise error
                raise LiveQualificationError("overlap_worker_failed") from error
        finally:
            for attempt in attempts:
                self._dispose(attempt)
        _scenario_finish(self.collector, scenario, process)

    def run(self) -> tuple[PendingEvent, ...]:
        self.full_turn()
        self.cancellation()
        self.crash_reconcile()
        self.cleanup()
        self.overlap()
        ordered: list[PendingEvent] = []
        for scenario in QUALIFICATION_SCENARIO_ORDER:
            segment = list(self.collector.segment(scenario))
            starts = [item for item in segment if type(item.payload) is RunStartedPayload]
            finishes = [item for item in segment if type(item.payload) is RunFinishedPayload]
            if len(starts) != 1 or len(finishes) != 1:
                raise LiveQualificationError("scenario_boundary_missing")
            body = [
                item
                for item in segment
                if type(item.payload) not in {RunStartedPayload, RunFinishedPayload}
            ]
            body.sort(key=lambda item: item.monotonic_ns)
            ordered.extend((starts[0], *body, finishes[0]))
        return tuple(ordered)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run five live scenarios through one pinned production backend "
            "adapter and atomically emit a verifier-admissible evidence root."
        )
    )
    parser.add_argument("--provider", choices=tuple(item.value for item in Provider), required=True)
    parser.add_argument("--platform", choices=tuple(item.value for item in Platform), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sdk-name", required=True)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument("--sdk-shasum", required=True)
    parser.add_argument("--sdk-integrity", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--providers-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument(
        "--provider-env",
        action="append",
        default=[],
        help=(
            "environment variable name explicitly admitted to provider children; "
            "values are never written to evidence"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--provenance-kind",
        choices=tuple(item.value for item in QualificationProvenanceKind),
        required=True,
    )
    parser.add_argument("--provenance-issuer", required=True)
    parser.add_argument("--provenance-reference", required=True)
    parser.add_argument("--provenance-identity", required=True)
    return parser


def _validate_run_id(value: str) -> str:
    try:
        ExecutionIdentity(value, 0)
    except (TypeError, ValueError):
        raise LiveQualificationError("invalid_run_id")
    return value


def _environment_names(values: list[str]) -> tuple[str, ...]:
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in values):
        raise LiveQualificationError("invalid_provider_environment_name")
    if len(set(values)) != len(values):
        raise LiveQualificationError("duplicate_provider_environment_name")
    missing = [item for item in values if not os.environ.get(item)]
    if missing:
        raise LiveQualificationError("provider_environment_missing")
    return tuple(values)


def _run_public(args: argparse.Namespace) -> bytes:
    provider = _provider(args.provider)
    platform = Platform(args.platform)
    if platform is not _platform():
        raise LiveQualificationError("platform_host_mismatch")
    run_id = _validate_run_id(args.run_id)
    source_revision = _require_revision(args.source_revision)
    if not (0 < args.timeout_seconds <= 900):
        raise LiveQualificationError("invalid_timeout")
    try:
        workspace = args.workspace.resolve(strict=True)
        providers_root = args.providers_root.resolve(strict=True)
    except OSError as exc:
        raise LiveQualificationError("input_path_missing") from exc
    if not workspace.is_dir() or not providers_root.is_dir():
        raise LiveQualificationError("input_path_not_directory")
    output = args.output.resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise LiveQualificationError("output_exists")
    if output == workspace or output in workspace.parents:
        raise LiveQualificationError("output_overlaps_workspace_root")
    sdk = _resolve_sdk(providers_root, provider, args)
    runtime = _runtime(args.runtime)
    environment_names = _environment_names(args.provider_env)
    bundle = load_bundled_compatibility()
    provider_entry = next(
        (item for item in bundle.providers if item.provider is provider), None
    )
    if provider_entry is None or (
        provider_entry.sdk.name,
        provider_entry.sdk.version,
        provider_entry.sdk.shasum,
    ) != (sdk.name, sdk.version, sdk.shasum):
        raise LiveQualificationError("bundle_sdk_pin_mismatch")
    bundle.platform(provider, platform)

    # The production clients inherit their process environment.  Scrubbing the
    # harness itself ensures only explicit names can reach any provider child.
    _scrub_environment(environment_names)
    environment = _provider_environment(environment_names)
    launch = _launch_spec(
        sdk,
        runtime,
        environment,
    )
    imported_at = _utc_now()
    snapshot_bytes, manifest_bytes, manifest = _build_manifest(
        provider=provider,
        platform=platform,
        bundle=bundle,
        run_id=run_id,
        source_revision=source_revision,
        imported_at=imported_at,
    )
    harness = QualificationHarnessDescriptor(
        schema_version=1,
        harness_version=HARNESS_VERSION,
        source_revision=source_revision,
        entrypoint="scripts/ci_live_backend_qualification.py",
        event_schema_version=1,
        scenarios=QUALIFICATION_SCENARIO_ORDER,
    )
    run_start = _run_started(
        harness_digest=_digest(harness.canonical_json_bytes()),
        manifest_digest=_digest(manifest_bytes),
        snapshot_digest=_digest(snapshot_bytes),
        bundle=bundle,
        provider=provider,
        platform=platform,
        sdk=sdk,
        source_revision=source_revision,
    )
    worktrees = WorktreeManager(workspace, source_revision)
    try:
        runner = LiveRunner(
            run_id=run_id,
            source_revision=source_revision,
            provider=provider,
            platform=platform,
            bundle=bundle,
            sdk=sdk,
            launch=launch,
            environment_names=environment_names,
            manifest=manifest,
            worktrees=worktrees,
            run_start=run_start,
            timeout=args.timeout_seconds,
        )
        pending = runner.run()
        events = runner.collector.materialize(pending)
    finally:
        worktrees.close()

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".live-qualification-", dir=output.parent)
    )
    evidence = temporary / "evidence"
    try:
        _materialize_evidence(
            evidence,
            events=events,
            harness=harness,
            manifest_bytes=manifest_bytes,
            snapshot_bytes=snapshot_bytes,
            run_id=run_id,
            provider=provider,
            platform=platform,
            provenance_kind=QualificationProvenanceKind(args.provenance_kind),
            provenance_issuer=args.provenance_issuer,
            provenance_reference=args.provenance_reference,
            provenance_identity=args.provenance_identity,
            source_revision=source_revision,
        )
        candidate = verify_backend_qualification_candidate(evidence)
        os.replace(evidence, output)
    finally:
        _remove_tree(temporary)
    return candidate.report_bytes


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] == ["_crash-child"]:
        child = argparse.ArgumentParser(add_help=False)
        child.add_argument("_command")
        child.add_argument("--config", type=Path, required=True)
        child_args = child.parse_args(values)
        return _run_crash_child(child_args.config)
    try:
        args = build_parser().parse_args(values)
        report = _run_public(args)
    except (
        BackendQualificationCandidateError,
        LiveQualificationError,
        OSError,
        ValueError,
    ) as exc:
        code = exc.code if isinstance(exc, (LiveQualificationError, BackendQualificationCandidateError)) else type(exc).__name__
        print("ERROR: " + code, file=sys.stderr)
        return 1
    except Exception as exc:
        print("ERROR: live_qualification_failed:" + type(exc).__name__, file=sys.stderr)
        return 1
    sys.stdout.buffer.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
