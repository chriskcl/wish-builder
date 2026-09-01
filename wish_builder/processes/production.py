"""Production composition for the guarded foreground runner.

The path and dispatch-plan contracts remain side-effect free.  The concrete
component set is constructed only by the lazy factory used after backend
admission, and then composes the existing Journal, Git, Trellis, recovery,
acceptance, cleanup, checkpoint, and terminal boundaries.
"""

from __future__ import annotations

from wish_builder.adapters import FilesystemExternalEvidenceStore

import hashlib
import math
import os
import platform as host_platform
import re
import shutil
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from wish_builder.adapters.git_identity import (
    ProtectedControlRoot,
    WorkspaceIdentity,
    capture_workspace_identity,
    reconstruct_pristine_workspace_identity,
    revalidate_workspace_identity,
)
from wish_builder.adapters.git_worktree import (
    AttemptEffectDisposition,
    AttemptWorktree,
    GitWorktreeAdapter,
    ResultValidation,
)
from wish_builder.adapters.process_identity import capture_process_start_id
from wish_builder.adapters.storage import FilesystemJournalStorage
from wish_builder.adapters.trellis import (
    TrellisAuthoritativeProjectionProvider,
    TrellisCoreGraphPort,
    TrellisCoreLifecyclePort,
    TrellisCoreProjectionPort,
)
from wish_builder.compatibility import load_bundled_compatibility
from wish_builder.contracts import (
    ActorIdentity,
    ActorType,
    AdapterKind,
    EffectObjectType,
    EffectObservationPayload,
    EffectOperation,
    EffectReceipt,
    EffectRequestPayload,
    EffectStatus,
    EvidenceRef,
    JournalEvent,
    JournalEventType,
    LeaseOwner,
    WorkerProvider,
    canonical_sha256,
    canonical_json_bytes,
    decode_journal_event_bytes,
)
from wish_builder.contracts.task_packet import generated_task_packet_bytes
from wish_builder.contracts.compatibility import (
    Platform,
    PlatformCompatibility,
    Provider,
)
from wish_builder.contracts.manifest_v2 import ExecutionManifestV2, ManifestTask
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import (
    ExecutionIdentity,
    RuntimeReasonCode,
    RuntimeState,
)
from wish_builder.kernel.state import apply_journal_event
from wish_builder.processes.acceptance import ProcessAcceptancePort
from wish_builder.processes.coordinator import (
    CoordinatorCursor,
    CoordinatorReason,
    CoordinatorStatus,
    CoordinatorStepResult,
    ForegroundCoordinator,
    WorkerResultProposal,
)
from wish_builder.processes.foreground import (
    ForegroundTerminalResult,
    PreparedForegroundAttempt,
    WorkerBatchResult,
    WorkerLeaseRenewalResult,
)
from wish_builder.processes.production_recovery import (
    ExternalRecoveryCommand,
    reconcile_pending_external_effects,
    resolve_external_recovery_command,
)
from wish_builder.processes.production_routing import (
    AttemptChannelRoute,
    AttemptOperationRoute,
    AttemptBackendChannelRouter,
    WishBuilderBackendAttemptChannelFactory,
)
from wish_builder.processes.production_terminal import ProductionTerminalFinalizer
from wish_builder.processes.backend_workers import BackendWorkerTurnMonitor
from wish_builder.processes.workflow import (
    AcceptancePort,
    CleanupStepResult,
    LocalExecutionWorkflow,
)
from wish_builder.services.backend_admission import admit_backend, current_platform
from wish_builder.services.checkpoints import CheckpointStore
from wish_builder.services.cleanup import CleanupCandidate, CleanupService
from wish_builder.services.dispatch_recovery import (
    DispatchRecoveryProjectionError,
    PendingExternalEffect,
    advance_dispatch_recoveries,
)
from wish_builder.services.execution_admission import (
    ExecutionAdmissionReason,
    ExecutionAdmissionResult,
    admit_execution_snapshot,
)
from wish_builder.services.execution_checkpoints import (
    ExecutionCheckpointPublisher,
    ExecutionCheckpointReason,
    ExecutionCheckpointResult,
    ExecutionCheckpointStatus,
)
from wish_builder.services.journal import (
    GENESIS_HEAD,
    DurableJournal,
    JournalHead,
)
from wish_builder.services.ports import (
    BackendCapabilities,
    CancelTurn,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    PrepareAttempt,
    ReserveChannel,
    SendTaskPacket,
    TurnObservation,
    TurnState,
)
from wish_builder.services.ports.backend import MAX_TASK_PACKET_BYTES
from wish_builder.services.promotion import PromotionRecord
from wish_builder.services.recovery import (
    CoordinatorLeaseService,
    LeaseMutationResult,
    LeaseMutationStatus,
    LeaseRecoveryResult,
    LeaseRecoveryStatus,
    recover_coordinator_lease,
)
from wish_builder.services.backend_effects import BackendDispatchPlan
from wish_builder.services.backend_effects import BackendDispatchEffectService
from wish_builder.services.trellis_graph_admission import (
    TrellisGraphAdmissionService,
)
from wish_builder.services.trellis_lifecycle_effects import (
    TrellisLifecycleEffectReason,
    TrellisLifecycleEffectService,
    TrellisLifecycleEffectStatus,
)
from wish_builder.services.trellis_projection import (
    TrellisProjectionService,
    TrellisProjectionSyncResult,
)


_PROJECT_KEY_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PLATFORMS = frozenset({"linux", "windows"})
_PROVIDERS = {
    Provider.CODEX: WorkerProvider.CODEX,
    Provider.OMP: WorkerProvider.OH_MY_PI,
    Provider.PI: WorkerProvider.PI,
}
_COMPATIBILITY_PROVIDERS = {
    WorkerProvider.CODEX: Provider.CODEX,
    WorkerProvider.OH_MY_PI: Provider.OMP,
    WorkerProvider.PI: Provider.PI,
}
_SEGMENT_RE = re.compile(r"segment-([0-9]{8})\.jsonl\Z")
_MAX_ADMISSION_EVENTS = 1_000_000
_AttemptRouteKey = tuple[str, int, str, int]
_TRELLIS_PATH_ENVIRONMENT = (
    "WISH_BUILDER_TRELLIS_CORE_ARCHIVE",
    "WISH_BUILDER_TRELLIS_CORE_MODULE",
    "WISH_BUILDER_TRELLIS_CORE_ROOT",
    "WISH_BUILDER_TRELLIS_CLI_ARCHIVE",
    "WISH_BUILDER_TRELLIS_CLI_ROOT",
)
_PROVIDER_ENVIRONMENT = (
    "APPDATA",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "PATH",
    "USERPROFILE",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
)


def _recovery_route_key(
    operation: AttemptOperationRoute,
    projected_keys: set[_AttemptRouteKey],
) -> _AttemptRouteKey:
    identity = operation.identity
    assert identity.task_id is not None and identity.attempt is not None
    key = (
        identity.run_id,
        identity.coordinator_epoch,
        identity.task_id,
        identity.attempt,
    )
    if key in projected_keys or operation.operation is not EffectOperation.CANCEL_TURN:
        return key
    candidates = tuple(
        candidate
        for candidate in projected_keys
        if candidate[0] == key[0]
        and candidate[2:] == key[2:]
        and candidate[1] < key[1]
    )
    if len(candidates) != 1:
        raise ValueError("takeover cancel does not resolve to one older attempt")
    return candidates[0]


def _absolute_path(value: object, field_name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"{field_name} must be a path")
    raw = os.fspath(value)
    if type(raw) is not str or not raw:
        raise ValueError(f"{field_name} must be a non-empty path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be absolute")
    normalized = Path(os.path.abspath(path))
    if normalized == normalized.parent:
        raise ValueError(f"{field_name} must not be a filesystem root")
    return normalized


def _comparable(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _contains(parent: Path, child: Path, *, strict: bool) -> bool:
    parent_value = _comparable(parent)
    child_value = _comparable(child)
    try:
        common = os.path.commonpath((parent_value, child_value))
    except ValueError:
        return False
    return common == parent_value and (not strict or child_value != parent_value)


def _overlaps(left: Path, right: Path) -> bool:
    return _contains(left, right, strict=False) or _contains(
        right, left, strict=False
    )


@dataclass(frozen=True, slots=True)
class ProductionRuntimeLayout:
    """Side-effect-free path contract for one immutable execution run."""

    run_id: str
    repository: Path
    run_root: Path
    control_root: Path
    journal_root: Path
    evidence_root: Path
    checkpoint_root: Path
    attempts_root: Path

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        for field_name in (
            "repository",
            "run_root",
            "control_root",
            "journal_root",
            "evidence_root",
            "checkpoint_root",
            "attempts_root",
        ):
            object.__setattr__(
                self,
                field_name,
                _absolute_path(getattr(self, field_name), field_name),
            )

        if _overlaps(self.repository, self.run_root):
            raise ValueError("run_root and repository must be disjoint")
        if not _contains(self.run_root, self.control_root, strict=True):
            raise ValueError("control_root must be inside run_root")
        if not _contains(self.run_root, self.attempts_root, strict=True):
            raise ValueError("attempts_root must be inside run_root")
        if _overlaps(self.control_root, self.attempts_root):
            raise ValueError("control_root and attempts_root must be disjoint")

        protected_children = (
            self.journal_root,
            self.evidence_root,
            self.checkpoint_root,
        )
        if not all(
            _contains(self.control_root, child, strict=True)
            for child in protected_children
        ):
            raise ValueError(
                "journal, evidence, and checkpoint roots must be inside control_root"
            )
        for index, left in enumerate(protected_children):
            if any(_overlaps(left, right) for right in protected_children[index + 1 :]):
                raise ValueError(
                    "journal, evidence, and checkpoint roots must be disjoint"
                )

    @classmethod
    def for_run(
        cls,
        repository: str | os.PathLike[str],
        state_root: str | os.PathLike[str],
        run_id: str,
    ) -> "ProductionRuntimeLayout":
        """Derive stable paths without creating or inspecting any of them."""

        repository_path = _absolute_path(repository, "repository")
        state_path = _absolute_path(state_root, "state_root")
        if type(run_id) is not str or not run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        run_key = hashlib.sha256(run_id.encode("utf-8", errors="strict")).hexdigest()
        run_root = state_path / f"run-{run_key}"
        control_root = run_root / "control"
        return cls(
            run_id,
            repository_path,
            run_root,
            control_root,
            control_root / "journal",
            control_root / "trellis-evidence",
            control_root / "checkpoints",
            run_root / "attempts",
        )


def channel_capabilities_from_compatibility(
    cell: PlatformCompatibility,
) -> BackendCapabilities:
    """Project one validated compatibility cell into the Channel port contract."""

    if type(cell) is not PlatformCompatibility:
        raise TypeError("cell must be a PlatformCompatibility")
    capabilities = cell.capabilities
    provider = _PROVIDERS[capabilities.provider]
    guarantees = tuple(guarantee for _, guarantee in capabilities.operations)
    return BackendCapabilities(
        provider=provider,
        platform=cell.platform.value,
        capability_digest=capabilities.capability_digest,
        launch_profile_digest=cell.launch_profile_digest,
        policy_digest=capabilities.policy_digest,
        max_task_packet_bytes=capabilities.max_task_packet_bytes,
        caller_supplied_ids=capabilities.features.caller_controlled_operation_ids,
        idempotent_operations=all(item.idempotent for item in guarantees),
        inspect_operations=all(item.inspectable for item in guarantees),
        fresh_session_per_attempt=capabilities.features.fresh_provider_sessions,
    )


@dataclass(frozen=True, slots=True)
class ProductionRuntimeConfig:
    """Validated, inert inputs captured by a future lazy production factory.

    ``bridge_command=None`` means that composition must receive an injected
    ``BackendChannelPort``. Supplying an absolute command records how to build
    the real adapter later, but construction here never checks or executes it.
    """

    manifest: ExecutionManifestV2
    layout: ProductionRuntimeLayout
    channel_capabilities: BackendCapabilities
    project_key: str
    bridge_command: tuple[str, str] | None = None
    worker_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if type(self.manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if type(self.layout) is not ProductionRuntimeLayout:
            raise TypeError("layout must be a ProductionRuntimeLayout")
        if type(self.channel_capabilities) is not BackendCapabilities:
            raise TypeError("channel_capabilities must be BackendCapabilities")
        if self.layout.run_id != self.manifest.run_id:
            raise ValueError("layout run_id does not match the manifest")
        if (
            type(self.project_key) is not str
            or not _PROJECT_KEY_RE.fullmatch(self.project_key)
        ):
            raise ValueError("project_key is not a safe Trellis project token")

        capabilities = self.channel_capabilities
        if capabilities.platform not in _PLATFORMS:
            raise ValueError("channel platform is outside the M1 matrix")
        if capabilities.provider is not self.manifest.provider:
            raise ValueError("channel provider does not match the manifest")
        for field_name in (
            "capability_digest",
            "launch_profile_digest",
            "policy_digest",
        ):
            if getattr(capabilities, field_name) != getattr(self.manifest, field_name):
                raise ValueError(f"channel {field_name} does not match the manifest")
        if capabilities.max_task_packet_bytes > MAX_TASK_PACKET_BYTES:
            raise ValueError("channel task packet limit exceeds the local contract")
        if not (
            capabilities.caller_supplied_ids
            and capabilities.idempotent_operations
            and capabilities.inspect_operations
            and capabilities.fresh_session_per_attempt
        ):
            raise ValueError(
                "channel lacks the required deterministic dispatch guarantees"
            )

        if self.bridge_command is not None:
            if (
                type(self.bridge_command) is not tuple
                or len(self.bridge_command) != 2
            ):
                raise ValueError(
                    "bridge_command must contain the Node executable and bridge path"
                )
            normalized_command: list[str] = []
            for value in self.bridge_command:
                if type(value) is not str or not value or "\x00" in value:
                    raise ValueError("bridge_command contains an invalid path")
                path = Path(value).expanduser()
                if not path.is_absolute():
                    raise ValueError("bridge_command paths must be absolute")
                normalized_command.append(os.path.abspath(path))
            object.__setattr__(self, "bridge_command", tuple(normalized_command))

        for field_name in ("worker_timeout_seconds", "poll_interval_seconds"):
            value = getattr(self, field_name)
            if (
                type(value) not in {int, float}
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                raise ValueError(f"{field_name} must be a positive finite number")
            object.__setattr__(self, field_name, float(value))
        if self.poll_interval_seconds > self.worker_timeout_seconds:
            raise ValueError("poll interval must not exceed worker timeout")

    @classmethod
    def from_compatibility_cell(
        cls,
        manifest: ExecutionManifestV2,
        layout: ProductionRuntimeLayout,
        cell: PlatformCompatibility,
        *,
        project_key: str,
        bridge_command: Sequence[str] | None = None,
        worker_timeout_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
    ) -> "ProductionRuntimeConfig":
        if bridge_command is None:
            normalized_command = None
        elif isinstance(bridge_command, (str, bytes)):
            raise TypeError("bridge_command must be a sequence of paths or null")
        else:
            normalized_command = tuple(bridge_command)
        return cls(
            manifest,
            layout,
            channel_capabilities_from_compatibility(cell),
            project_key,
            normalized_command,  # type: ignore[arg-type]
            worker_timeout_seconds,
            poll_interval_seconds,
        )


class DeterministicBackendDispatchPlanFactory:
    """Compile stable backend commands from a frozen manifest.

    Expanded-input tasks receive a canonical JSON packet containing the frozen
    task contract and attempt identity.  A task that names an approved packet
    template must receive those exact normalized bytes through
    ``task_packet_templates``; missing or mismatched templates fail closed.
    """

    def __init__(
        self,
        config: ProductionRuntimeConfig,
        *,
        task_packet_templates: Mapping[str, str] | None = None,
    ) -> None:
        if type(config) is not ProductionRuntimeConfig:
            raise TypeError("config must be a ProductionRuntimeConfig")
        if task_packet_templates is not None and not isinstance(
            task_packet_templates, Mapping
        ):
            raise TypeError("task_packet_templates must be a mapping or null")
        templates: dict[str, str] = {}
        for digest, packet in (task_packet_templates or {}).items():
            if type(digest) is not str or not HASH_RE.fullmatch(digest):
                raise ValueError("task packet template key must be a full sha256 reference")
            if type(packet) is not str:
                raise TypeError("task packet templates must contain strings")
            normalized = unicodedata.normalize(
                "NFC",
                packet.replace("\r\n", "\n").replace("\r", "\n"),
            )
            actual = "sha256:" + hashlib.sha256(
                normalized.encode("utf-8", errors="strict")
            ).hexdigest()
            if actual != digest:
                raise ValueError("task packet template bytes do not match their digest")
            templates[digest] = normalized

        self._config = config
        self._manifest = config.manifest
        self._manifest_digest = self._manifest.canonical_sha256()
        self._tasks = {task.id: task for task in self._manifest.tasks}
        self._trellis_task_ids = {
            item.task_id: item.trellis_task_id
            for item in self._manifest.task_id_mapping
        }
        self._templates = templates

    def __call__(self, identity: ExecutionIdentity) -> BackendDispatchPlan:
        if type(identity) is not ExecutionIdentity or not identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        if identity.run_id != self._manifest.run_id:
            raise ValueError("identity run_id does not match the manifest")
        assert identity.task_id is not None
        task = self._tasks.get(identity.task_id)
        if task is None:
            raise ValueError("identity task_id is not present in the manifest")
        assert identity.correlation_id is not None

        packet = self._task_packet(identity, task)
        packet_bytes = packet.encode("utf-8", errors="strict")
        if len(packet_bytes) > self._config.channel_capabilities.max_task_packet_bytes:
            raise ValueError("task packet exceeds the admitted Channel capability")
        packet_digest = "sha256:" + hashlib.sha256(packet_bytes).hexdigest()
        suffix = hashlib.sha256(
            canonical_json_bytes(
                {
                    "identity": identity.to_primitive(),
                    "manifest_digest": self._manifest_digest,
                }
            )
        ).hexdigest()[:48].upper()
        attempt_id = f"ATTEMPT-{suffix}"
        channel_id = f"CHANNEL-{suffix}"
        capabilities = self._config.channel_capabilities
        return BackendDispatchPlan(
            ReserveChannel(
                operation_id=f"RESERVE-{suffix}",
                attempt_id=attempt_id,
                dispatch_id=identity.correlation_id,
                channel_id=channel_id,
                provider=self._manifest.provider,
                capability_digest=capabilities.capability_digest,
                launch_profile_digest=capabilities.launch_profile_digest,
                policy_digest=capabilities.policy_digest,
            ),
            SendTaskPacket(
                operation_id=f"SEND-{suffix}",
                attempt_id=attempt_id,
                dispatch_id=identity.correlation_id,
                channel_id=channel_id,
                message_id=f"MESSAGE-{suffix}",
                turn_id=f"TURN-{suffix}",
                task_packet=packet,
                task_packet_digest=packet_digest,
            ),
        )

    def task_packet(self, identity: ExecutionIdentity) -> str:
        """Return the exact packet text without constructing child commands."""

        if type(identity) is not ExecutionIdentity or not identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        if identity.run_id != self._manifest.run_id:
            raise ValueError("identity run_id does not match the manifest")
        assert identity.task_id is not None
        task = self._tasks.get(identity.task_id)
        if task is None:
            raise ValueError("identity task_id is not present in the manifest")
        return self._task_packet(identity, task)

    def _task_packet(
        self,
        identity: ExecutionIdentity,
        task: ManifestTask,
    ) -> str:
        if task.task_packet_template_digest is not None:
            try:
                return self._templates[task.task_packet_template_digest]
            except KeyError as exc:
                raise ValueError("approved task packet template is unavailable") from exc

        return generated_task_packet_bytes(
            self._manifest,
            task,
            self._trellis_task_ids[task.id],
            identity,
        ).decode("utf-8", errors="strict")


def _utc_datetime() -> datetime:
    return datetime.now(UTC)


def _workspace_scopes(manifest: ExecutionManifestV2) -> tuple[str, ...]:
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


def _reconstruct_projection_workspace(
    provider: TrellisAuthoritativeProjectionProvider,
    run_id: str,
    observed: WorkspaceIdentity,
) -> WorkspaceIdentity:
    """Prove one stable projection-only view and return its clean identity."""

    before = provider.ensure(run_id)
    if before.workspace != observed:
        raise ValueError("projection workspace changed before reconstruction")
    reconstructed = reconstruct_pristine_workspace_identity(observed)
    after = provider.ensure(run_id)
    if after.workspace != observed:
        raise ValueError("projection workspace changed during reconstruction")
    return reconstructed


def _safe_host_id() -> str:
    value = re.sub(r"[^A-Za-z0-9._:@/-]", "-", host_platform.node().strip())
    return value.strip("-./")[:128] or "localhost"


def _bridge_environment(
    layout: ProductionRuntimeLayout,
    *,
    trellis_core_root: str | os.PathLike[str] | None = None,
    trellis_core_archive: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in _PROVIDER_ENVIRONMENT
        if key in os.environ
    }
    for key in _TRELLIS_PATH_ENVIRONMENT:
        value = os.environ.get(key)
        if not value:
            continue
        if key.endswith("_MODULE") and value.startswith("file:"):
            environment[key] = value
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        environment[key] = os.path.abspath(path)
    for key, value, directory in (
        ("WISH_BUILDER_TRELLIS_CORE_ROOT", trellis_core_root, True),
        ("WISH_BUILDER_TRELLIS_CORE_ARCHIVE", trellis_core_archive, False),
    ):
        if value is None:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{key} must be an absolute path")
        path = path.resolve(strict=True)
        if path.is_symlink() or (not path.is_dir() if directory else not path.is_file()):
            raise ValueError(f"{key} must identify a non-link {'directory' if directory else 'file'}")
        environment[key] = str(path)
    return environment


def _compatibility_cell(manifest: ExecutionManifestV2) -> PlatformCompatibility:
    platform = current_platform()
    if platform not in {Platform.WINDOWS, Platform.LINUX}:
        raise RuntimeError("the current host is outside the M1 compatibility matrix")
    provider = _COMPATIBILITY_PROVIDERS.get(manifest.provider)
    if provider is None:
        raise RuntimeError("the manifest provider is outside the M1 matrix")
    bundle = load_bundled_compatibility()
    if manifest.policy_digest != bundle.policy_digest:
        raise ValueError("manifest policy digest does not match the bundled policy")
    return bundle.platform(provider, platform)


def _bridge_command(
    node_executable: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    executable = node_executable or shutil.which("node")
    if executable is None:
        raise FileNotFoundError("Node.js is required for the Trellis Core bridge")
    node = Path(executable).expanduser()
    if not node.is_absolute():
        raise ValueError("Node.js executable must be an absolute path")
    node = node.resolve(strict=True)
    bridge = (
        Path(__file__).resolve().parents[1]
        / "bridges"
        / "trellis_core"
        / "bridge.mjs"
    ).resolve(strict=True)
    if not node.is_file() or not bridge.is_file():
        raise FileNotFoundError("the Trellis Core bridge runtime is unavailable")
    return str(node), str(bridge)


@dataclass(frozen=True, slots=True)
class _LifecycleProjection:
    succeeded: bool
    reason: CoordinatorReason
    events: tuple[JournalEvent, ...] = ()

    def __post_init__(self) -> None:
        if type(self.succeeded) is not bool:
            raise TypeError("succeeded must be a boolean")
        if type(self.reason) is not CoordinatorReason:
            raise TypeError("reason must be a CoordinatorReason")
        if type(self.events) is not tuple or not all(
            type(event) is JournalEvent for event in self.events
        ):
            raise TypeError("events must contain JournalEvent values")
        if self.succeeded != (self.reason is CoordinatorReason.NONE):
            raise ValueError("lifecycle success and reason are inconsistent")
        if any(
            current.sequence != previous.sequence + 1
            or current.previous_event_hash != previous.event_hash
            for previous, current in zip(self.events, self.events[1:])
        ):
            raise ValueError("lifecycle events must form one Journal chain")


LifecyclePrepareProjector = Callable[
    [CoordinatorCursor, ExecutionIdentity],
    _LifecycleProjection,
]
LifecycleCompletionProjector = Callable[
    [CoordinatorCursor, WorkerResultProposal],
    _LifecycleProjection,
]
TaskProjectionProjector = Callable[[JournalEvent], None]


class _ProductionForegroundCoordinator(ForegroundCoordinator):
    """Insert the Trellis lifecycle around canonical coordinator transitions."""

    def __init__(
        self,
        *args,
        prepare_lifecycle: LifecyclePrepareProjector,
        complete_lifecycle: LifecycleCompletionProjector,
        project_task_event: TaskProjectionProjector,
        **kwargs,
    ) -> None:
        if (
            not callable(prepare_lifecycle)
            or not callable(complete_lifecycle)
            or not callable(project_task_event)
        ):
            raise TypeError("lifecycle projectors must be callable")
        super().__init__(*args, **kwargs)
        self._prepare_lifecycle = prepare_lifecycle
        self._complete_lifecycle = complete_lifecycle
        self._project_task_event = project_task_event

    def dispatch_reserved(
        self,
        identity: ExecutionIdentity,
    ) -> CoordinatorStepResult:
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        attempt = self._matching_attempt(identity)
        task_state = dict(self.cursor.graph_index.task_states).get(
            identity.task_id or ""
        )
        if (
            attempt is None
            or attempt.state is not RuntimeState.RESERVED
            or task_state is not RuntimeState.LEASED
        ):
            return super().dispatch_reserved(identity)

        projected = self._prepare_lifecycle(self.cursor, identity)
        failure = self._adopt_lifecycle_projection(projected)
        if failure is not None:
            return failure
        dispatched = super().dispatch_reserved(identity)
        return replace(
            dispatched,
            events=projected.events + dispatched.events,
        )

    def accept_worker_result(
        self,
        proposal: WorkerResultProposal,
    ) -> CoordinatorStepResult:
        if type(proposal) is not WorkerResultProposal:
            raise TypeError("proposal must be a WorkerResultProposal")
        admission = self._admission_reason()
        if admission is not CoordinatorReason.NONE:
            return self._result(CoordinatorStatus.BLOCKED, admission)
        attempt = self._matching_attempt(proposal.identity)
        task_state = dict(self.cursor.graph_index.task_states).get(
            proposal.identity.task_id or ""
        )
        if (
            not proposal.succeeded
            or attempt is None
            or attempt.state is not RuntimeState.RUNNING
            or task_state is not RuntimeState.DISPATCHED
        ):
            return super().accept_worker_result(proposal)

        projected = self._complete_lifecycle(self.cursor, proposal)
        failure = self._adopt_lifecycle_projection(projected)
        if failure is not None:
            return failure
        accepted = super().accept_worker_result(proposal)
        return replace(
            accepted,
            events=projected.events + accepted.events,
        )

    def _adopt_lifecycle_projection(
        self,
        projected: _LifecycleProjection,
    ) -> CoordinatorStepResult | None:
        if type(projected) is not _LifecycleProjection:
            raise TypeError("lifecycle projector returned an invalid result")
        advanced = self._adopt_committed_events(projected.events)
        if advanced is not CoordinatorReason.NONE:
            return CoordinatorStepResult(
                CoordinatorStatus.BLOCKED,
                advanced,
                self.cursor,
                projected.events,
            )
        if not projected.succeeded:
            return CoordinatorStepResult(
                CoordinatorStatus.BLOCKED,
                projected.reason,
                self.cursor,
                projected.events,
            )
        return None

    def _complete_dispatch(
        self,
        receipt: EffectReceipt,
        events: list[JournalEvent],
        *,
        observation_identity: ExecutionIdentity | None = None,
    ) -> CoordinatorStepResult:
        result = super()._complete_dispatch(
            receipt,
            events,
            observation_identity=observation_identity,
        )
        if receipt.status is EffectStatus.APPLIED:
            observed = next(
                (
                    event
                    for event in reversed(result.events)
                    if event.event_type is JournalEventType.DISPATCH_OBSERVED
                    and event.sequence == result.cursor.head.sequence
                    and event.event_hash == result.cursor.head.event_hash
                ),
                None,
            )
            if observed is not None:
                self._project_task_event(observed)
        return result


class _ProductionLocalExecutionWorkflow(LocalExecutionWorkflow):
    """Observe durable task verification without changing workflow authority."""

    def __init__(
        self,
        *args,
        project_task_event: TaskProjectionProjector,
        **kwargs,
    ) -> None:
        if not callable(project_task_event):
            raise TypeError("project_task_event must be callable")
        super().__init__(*args, **kwargs)
        self._project_task_event = project_task_event

    def _append_payload(self, event_type, *args, **kwargs):
        appended = super()._append_payload(event_type, *args, **kwargs)
        if (
            event_type is JournalEventType.TASK_VERIFIED
            and appended.event is not None
        ):
            self._project_task_event(appended.event)
        return appended


class ProductionForegroundRunComponents:
    """Concrete M1 composition behind ``ForegroundRunService``.

    Trellis remains the editable task and worker lifecycle authority.  This
    object only executes the approved manifest snapshot and rebuilds its local
    safety projections from the Journal.
    """

    def __init__(
        self,
        config: ProductionRuntimeConfig,
        workspace: WorkspaceIdentity,
        control_root: ProtectedControlRoot,
        journal: DurableJournal,
        repository: GitWorktreeAdapter,
        evidence_store: FilesystemExternalEvidenceStore,
        checkpoint_store: CheckpointStore,
        lease_service: CoordinatorLeaseService,
        graph_admission: TrellisGraphAdmissionService,
        channel_factory,
        *,
        lifecycle_factory=None,
        projection_service: TrellisProjectionService | None = None,
        coordinator_id: str,
        owner: LeaseOwner,
        authority_clock=_utc_datetime,
    ) -> None:
        if type(config) is not ProductionRuntimeConfig:
            raise TypeError("config must be a ProductionRuntimeConfig")
        if type(workspace) is not WorkspaceIdentity:
            raise TypeError("workspace must be a WorkspaceIdentity")
        if type(control_root) is not ProtectedControlRoot:
            raise TypeError("control_root must be a ProtectedControlRoot")
        if type(journal) is not DurableJournal:
            raise TypeError("journal must be a DurableJournal")
        if type(repository) is not GitWorktreeAdapter:
            raise TypeError("repository must be a GitWorktreeAdapter")
        if type(evidence_store) is not FilesystemExternalEvidenceStore:
            raise TypeError("evidence_store must be a FilesystemExternalEvidenceStore")
        if type(checkpoint_store) is not CheckpointStore:
            raise TypeError("checkpoint_store must be a CheckpointStore")
        if type(lease_service) is not CoordinatorLeaseService:
            raise TypeError("lease_service must be a CoordinatorLeaseService")
        if type(graph_admission) is not TrellisGraphAdmissionService:
            raise TypeError("graph_admission must be a TrellisGraphAdmissionService")
        if not callable(channel_factory):
            raise TypeError("channel_factory must be callable")
        if lifecycle_factory is not None and not callable(lifecycle_factory):
            raise TypeError("lifecycle_factory must be callable or null")
        if projection_service is not None and type(
            projection_service
        ) is not TrellisProjectionService:
            raise TypeError("projection_service must be a TrellisProjectionService or null")
        if type(coordinator_id) is not str or not coordinator_id:
            raise ValueError("coordinator_id must be non-empty")
        if type(owner) is not LeaseOwner or owner.actor.actor_id != coordinator_id:
            raise ValueError("owner must identify the coordinator")
        if not callable(authority_clock):
            raise TypeError("authority_clock must be callable")

        self._config = config
        self._manifest = config.manifest
        self._workspace = workspace
        self._control_root = control_root
        self._journal = journal
        self._repository = repository
        self._evidence_store = evidence_store
        self._checkpoint_store = checkpoint_store
        self._lease_service = lease_service
        self._graph_admission = graph_admission
        self._channel_factory = channel_factory
        self._lifecycle_factory = lifecycle_factory
        self._projection_service = projection_service
        self._coordinator_id = coordinator_id
        self._owner = owner
        self._authority_clock = authority_clock
        self._plan_factory = DeterministicBackendDispatchPlanFactory(config)
        self._checkpoint_publisher = ExecutionCheckpointPublisher(
            self._manifest,
            self._journal,
            self._checkpoint_store,
        )
        self._cleanup = CleanupService(
            self._repository,
            available_bytes=lambda: shutil.disk_usage(
                self._config.layout.run_root
            ).free,
            minimum_free_bytes=(
                self._manifest.execution_budget.max_output_bytes
                + self._manifest.execution_budget.max_retained_evidence_bytes
            ),
        )
        self._trellis_task_ids = {
            item.task_id: item.trellis_task_id
            for item in self._manifest.task_id_mapping
        }
        self._attempts: dict[str, AttemptWorktree] = {}
        self._validated_results: dict[ExecutionIdentity, ResultValidation] = {}
        self._completed_lifecycle_commands: dict[
            str,
            tuple[ExecutionIdentity, EffectOperation, str],
        ] = {}
        self._last_recovery: LeaseRecoveryResult | None = None
        self._projection_results: list[TrellisProjectionSyncResult] = []
        self._fencing_token = 0
        self._closed = False

    @classmethod
    def from_runtime_inputs(
        cls,
        manifest: ExecutionManifestV2,
        *,
        runtime_root: str | os.PathLike[str] | None,
        workspace_root: str | os.PathLike[str],
        provider_sdk_root: str | os.PathLike[str] | None = None,
        trellis_core_root: str | os.PathLike[str] | None = None,
        trellis_core_archive: str | os.PathLike[str] | None = None,
        node_executable: str | os.PathLike[str] | None = None,
        authority_clock: Callable[[], datetime] = _utc_datetime,
    ) -> "ProductionForegroundRunComponents":
        """Build the effectful component set after backend admission."""

        if type(manifest) is not ExecutionManifestV2:
            raise TypeError("manifest must be an ExecutionManifestV2")
        if not callable(authority_clock):
            raise TypeError("authority_clock must be callable")
        if runtime_root is None:
            raise ValueError("runtime_root is required after backend admission")
        layout = ProductionRuntimeLayout.for_run(
            workspace_root,
            runtime_root,
            manifest.run_id,
        )
        cell = _compatibility_cell(manifest)
        command = _bridge_command(node_executable)
        config = ProductionRuntimeConfig.from_compatibility_cell(
            manifest,
            layout,
            cell,
            project_key=(
                "wish-builder-"
                + hashlib.sha256(manifest.run_id.encode("utf-8")).hexdigest()[:32]
            ),
            bridge_command=command,
            worker_timeout_seconds=float(
                manifest.execution_budget.attempt_deadline_seconds
            ),
            poll_interval_seconds=1.0,
        )
        workspace = capture_workspace_identity(
            layout.repository,
            _workspace_scopes(manifest),
        )
        projection_target = TrellisAuthoritativeProjectionProvider(
            layout.repository,
            workspace,
        )

        def normalize_projection_workspace(
            observed: WorkspaceIdentity,
        ) -> WorkspaceIdentity:
            return _reconstruct_projection_workspace(
                projection_target,
                manifest.run_id,
                observed,
            )

        pristine_workspace = reconstruct_pristine_workspace_identity(workspace)
        git_workspace = (
            workspace
            if pristine_workspace == workspace
            else normalize_projection_workspace(workspace)
        )

        layout.control_root.mkdir(parents=True, exist_ok=True)
        layout.attempts_root.mkdir(parents=True, exist_ok=True)
        protected: ProtectedControlRoot | None = None
        try:
            protected = ProtectedControlRoot.open(layout.control_root)
            validator = lambda: (
                not protected.closed and protected.revalidate().ok
            )
            storage = FilesystemJournalStorage(
                layout.journal_root,
                manifest.run_id,
                control_root=protected,
                authority_clock=authority_clock,
            )
            journal = DurableJournal(manifest.run_id, storage)
            checkpoint_store = CheckpointStore(
                layout.checkpoint_root,
                control_root_validator=validator,
            )
            evidence_store = FilesystemExternalEvidenceStore(layout.evidence_root)
            repository = GitWorktreeAdapter(
                layout.repository,
                layout.attempts_root,
                git_workspace,
                projection_workspace_validator=normalize_projection_workspace,
            )
            process_start_id = capture_process_start_id()
            coordinator_seed = canonical_json_bytes(
                {
                    "host_id": _safe_host_id(),
                    "process_id": os.getpid(),
                    "process_start_id": process_start_id,
                    "run_id": manifest.run_id,
                }
            )
            coordinator_id = (
                "coordinator-" + hashlib.sha256(coordinator_seed).hexdigest()[:32]
            )
            owner = LeaseOwner(
                ActorIdentity(
                    ActorType.COORDINATOR,
                    coordinator_id,
                    _safe_host_id(),
                    os.getpid(),
                    process_start_id,
                ),
                workspace.local_repository_id,
                workspace.local_worktree_id,
                workspace.workspace_hash,
                protected.expected.identity_hash,
            )

            def recover() -> LeaseRecoveryResult:
                return recover_coordinator_lease(
                    layout.journal_root,
                    manifest,
                    coordinator_epoch=1,
                    checkpoint_store=checkpoint_store,
                    repair_derived=True,
                    control_root_validator=validator,
                )

            lease_service = CoordinatorLeaseService(
                journal,
                recover,
                run_id=manifest.run_id,
                owner=owner,
                manifest_digest=manifest.canonical_sha256(),
                lease_ttl_seconds=manifest.lease_ttl_seconds,
                lease_clock_skew_seconds=manifest.lease_clock_skew_seconds,
            )
            bridge_environment = _bridge_environment(
                layout,
                trellis_core_root=trellis_core_root,
                trellis_core_archive=trellis_core_archive,
            )
            channel_factory_kwargs: dict[str, object] = {
                "compatibility_cell": cell,
            }
            # Keep disabled/inert composition side-effect free.  A real
            # dispatch cell must supply both an explicit SDK root and a state
            # root outside every Git attempt worktree.
            if provider_sdk_root is not None:
                sdk_root = Path(provider_sdk_root).expanduser()
                if not sdk_root.is_absolute():
                    raise ValueError("provider_sdk_root must be an absolute path")
                channel_factory_kwargs.update(
                    {
                        "provider_sdk_root": sdk_root.resolve(strict=False),
                        "state_root": layout.run_root / "provider-state",
                        "requested_concurrency": manifest.max_concurrency,
                    }
                )
            channel_factory = WishBuilderBackendAttemptChannelFactory(
                **channel_factory_kwargs,
            )
            graph_port = TrellisCoreGraphPort(
                bridge_command=command,
                checkout_root=layout.repository,
                working_directory=layout.repository,
                environment=bridge_environment,
            )
            graph_admission = TrellisGraphAdmissionService(manifest, graph_port)
            projection_port = TrellisCoreProjectionPort(
                bridge_command=command,
                working_directory=layout.repository,
                environment=bridge_environment,
            )
            projection_service = TrellisProjectionService(
                manifest,
                journal,
                projection_target,
                projection_port,
            )

            trellis_task_ids = {
                item.task_id: item.trellis_task_id
                for item in manifest.task_id_mapping
            }

            def lifecycle_factory(attempt: AttemptWorktree) -> TrellisCoreLifecyclePort:
                trellis_task_id = trellis_task_ids.get(attempt.task_id)
                if trellis_task_id is None:
                    raise ValueError("attempt task is not mapped by the approved manifest")
                worktree_id = (
                    "worktree-"
                    + attempt.worktree_root.identity_hash.removeprefix("sha256:")[:48]
                )
                return TrellisCoreLifecyclePort(
                    bridge_command=command,
                    checkout_root=layout.repository,
                    working_directory=layout.repository,
                    environment=bridge_environment,
                    trellis_task_id=trellis_task_id,
                    worktree_path=attempt.path,
                    worktree_id=worktree_id,
                )

            return cls(
                config,
                workspace,
                protected,
                journal,
                repository,
                evidence_store,
                checkpoint_store,
                lease_service,
                graph_admission,
                channel_factory,
                lifecycle_factory=lifecycle_factory,
                projection_service=projection_service,
                coordinator_id=coordinator_id,
                owner=owner,
                authority_clock=authority_clock,
            )
        except Exception:
            if protected is not None:
                protected.close()
            raise

    @property
    def acceptance(self) -> AcceptancePort:
        identities = {
            task_id: attempt.identity
            for task_id, attempt in self._attempts.items()
        }
        return ProcessAcceptancePort(identities=identities)

    @property
    def projection_results(self) -> tuple[TrellisProjectionSyncResult, ...]:
        """Derived Trellis sync outcomes observed during this process."""

        return tuple(self._projection_results)

    def validate_execution(
        self,
        manifest: ExecutionManifestV2,
    ) -> ExecutionAdmissionResult:
        if manifest != self._manifest:
            return ExecutionAdmissionResult(
                False,
                ExecutionAdmissionReason.MANIFEST_DIGEST_MISMATCH,
            )
        try:
            events = self._read_verified_events()
        except (OSError, TypeError, ValueError):
            return ExecutionAdmissionResult(
                False,
                ExecutionAdmissionReason.JOURNAL_CHAIN_INVALID,
            )
        admission = admit_execution_snapshot(
            manifest,
            events,
            workspace_hash=self._workspace.workspace_hash,
        )
        if admission.reason is ExecutionAdmissionReason.WORKSPACE_DRIFT:
            reconstructed = self._projection_only_gate_workspace()
            if reconstructed is not None:
                admission = admit_execution_snapshot(
                    manifest,
                    events,
                    workspace_hash=reconstructed.workspace_hash,
                )
        if not admission.admitted or not self._live_graph_admitted():
            return (
                admission
                if not admission.admitted
                else ExecutionAdmissionResult(
                    False,
                    ExecutionAdmissionReason.TRELLIS_GRAPH_CHANGED,
                )
            )
        return admission

    def _projection_only_gate_workspace(self) -> WorkspaceIdentity | None:
        """Recover the Gate workspace hash across verified task projections."""

        if self._projection_service is None:
            return None
        try:
            provider = TrellisAuthoritativeProjectionProvider(
                self._config.layout.repository,
                self._workspace,
            )
            reconstructed = _reconstruct_projection_workspace(
                provider,
                self._manifest.run_id,
                self._workspace,
            )
        except Exception:
            return None
        return reconstructed

    def protect_control_root(self) -> bool:
        return not self._closed and self._control_root.revalidate().ok

    def verify_workspace_identity(self, manifest: ExecutionManifestV2) -> bool:
        return (
            manifest == self._manifest
            and revalidate_workspace_identity(self._workspace).ok
        )

    def recover_verified_cursor(
        self,
        manifest: ExecutionManifestV2,
    ) -> CoordinatorCursor | None:
        if (
            manifest != self._manifest
            or not self.protect_control_root()
            or not self.verify_workspace_identity(manifest)
        ):
            return None
        recovery = self._recover()
        cursor = self._cursor_from_recovery(recovery)
        if cursor is None:
            return None
        self._last_recovery = recovery
        self._reconcile_task_projections(cursor.head)
        return cursor

    def acquire_lease(self, cursor: CoordinatorCursor) -> CoordinatorCursor | None:
        if type(cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        latest = self._recover()
        latest_cursor = self._cursor_from_recovery(latest)
        if latest_cursor != cursor:
            return None
        if not self._live_graph_admitted():
            return None
        lease_key = hashlib.sha256(
            canonical_json_bytes(
                {
                    "owner": self._owner.to_primitive(),
                    "run_id": self._manifest.run_id,
                }
            )
        ).hexdigest()[:48].upper()
        mutation = self._lease_service.acquire(
            event_id=f"EVENT-LEASE-ACQ-{lease_key}",
            lease_id=f"LEASE-{lease_key}",
        )
        if (
            type(mutation) is not LeaseMutationResult
            or mutation.status
            not in {LeaseMutationStatus.COMMITTED, LeaseMutationStatus.IDEMPOTENT}
            or mutation.recovery.replay.head != cursor.head
        ):
            return None
        if not self._live_graph_admitted():
            return None

        recovery = self._recover()
        current = self._cursor_from_recovery(recovery)
        if current is None or not current.lease_state.active:
            return None
        lease = current.lease_state.lease
        if lease is None or lease.owner != self._owner:
            return None
        self._fencing_token = lease.fencing_token

        if recovery.pending_external_effects:
            try:
                routes = self._routes_for_cursor(
                    current,
                    recovery.pending_external_effects,
                )
                router = self._router(routes)
                recovery_routes = {
                    operation: route
                    for route in routes
                    for operation in route.recovery_operations
                }
                reconciled = reconcile_pending_external_effects(
                    recovery.pending_external_effects,
                    lease_recovery=recovery,
                    manifest=self._manifest,
                    journal=self._journal,
                    backend_channel=router,
                    trellis_lifecycle=router,
                    evidence_store=self._evidence_store,
                    cursor=current,
                    plan_factory=lambda pending: recovery_routes[
                        AttemptOperationRoute.from_pending(pending)
                    ].plan,
                    retry_admitted=self._retry_admitted,
                    command_resolver=lambda pending, plan: (
                        self._resolve_recovery_command(
                            pending,
                            plan,
                            recovery_routes.get(
                                AttemptOperationRoute.from_pending(pending)
                            ),
                            router,
                        )
                    ),
                )
            except Exception:
                return None
            if not reconciled.success:
                return None
            recovery = self._recover()
            current = self._cursor_from_recovery(recovery)
            if current is None or recovery.pending_external_effects:
                return None

        self._last_recovery = recovery
        recovered_dispatch = self._recover_cancelled_dispatch(current)
        if recovered_dispatch is None:
            return None
        if recovered_dispatch.head != current.head:
            recovery = self._recover()
            current = self._cursor_from_recovery(recovery)
            if (
                current is None
                or recovery.pending_external_effects
                or current != recovered_dispatch
            ):
                return None
        else:
            current = recovered_dispatch
        # M1 has no trustworthy parent backend dispatch receipt reconstruction.
        if recovery.pending_dispatch_requests:
            return None
        try:
            self._refresh_completed_lifecycle_commands()
        except (TypeError, ValueError):
            return None
        stale_reservations = tuple(
            attempt
            for attempt in current.snapshot.attempts
            if attempt.coordinator_epoch < self._fencing_token
            and (
                attempt.state is RuntimeState.RESERVED
                or (
                    attempt.state is RuntimeState.TERMINATED
                    and attempt.reason_code is RuntimeReasonCode.LEASE_LOST
                )
            )
        )
        if stale_reservations:
            reclaimed = self.coordinator(current).reclaim_stale_reservations()
            if (
                reclaimed.status is not CoordinatorStatus.PROGRESSED
                or len(reclaimed.reserved) != len(stale_reservations)
            ):
                return None
            current = reclaimed.cursor
        return self._recover_absent_preparation(current)

    def _recover_cancelled_dispatch(
        self,
        cursor: CoordinatorCursor,
    ) -> CoordinatorCursor | None:
        """Re-fence one cancelled prior-epoch dispatch with untouched ownership."""

        candidates = tuple(
            attempt
            for attempt in cursor.snapshot.attempts
            if attempt.coordinator_epoch < self._fencing_token
            and attempt.state is RuntimeState.RUNNING
        )
        if not candidates:
            return cursor
        if len(candidates) != 1 or cursor.snapshot.status is not RuntimeState.RUNNING:
            return None
        attempt = candidates[0]
        task_state = dict(cursor.graph_index.task_states).get(attempt.task_id)
        if task_state is not RuntimeState.DISPATCHED:
            return None
        identity = ExecutionIdentity(
            self._manifest.run_id,
            attempt.coordinator_epoch,
            attempt.task_id,
            attempt.attempt,
            attempt.correlation_id,
        )
        try:
            events = self._read_verified_events()
            requests = tuple(
                event
                for event in events
                if event.event_type is JournalEventType.DISPATCH_REQUESTED
                and event.identity == identity
                and type(event.payload) is EffectRequestPayload
                and event.payload.adapter is AdapterKind.TASK
                and event.payload.operation is EffectOperation.WORKER_DISPATCH
                and event.payload.object_type is EffectObjectType.WORKER
            )
            observations = tuple(
                event
                for event in events
                if event.event_type is JournalEventType.DISPATCH_OBSERVED
                and event.identity == identity
                and type(event.payload) is EffectObservationPayload
                and event.payload.adapter is AdapterKind.TASK
                and event.payload.receipt.identity == identity
                and event.payload.receipt.operation
                is EffectOperation.WORKER_DISPATCH
                and event.payload.receipt.status is EffectStatus.APPLIED
            )
            if len(requests) != 1 or len(observations) != 1:
                return None
            cancel_requests = tuple(
                event
                for event in events
                if event.event_type is JournalEventType.EFFECT_REQUESTED
                and type(event.payload) is EffectRequestPayload
                and event.payload.adapter is AdapterKind.BACKEND
                and event.payload.operation is EffectOperation.CANCEL_TURN
                and event.payload.object_type is EffectObjectType.TURN
                and event.identity.run_id == identity.run_id
                and event.identity.task_id == identity.task_id
                and event.identity.attempt == identity.attempt
                and identity.coordinator_epoch
                < event.identity.coordinator_epoch
                < self._fencing_token
            )
            recovered_cancellations = tuple(
                (request, event)
                for request in cancel_requests
                for event in events
                if event.event_type is JournalEventType.EFFECT_RECONCILED
                and type(event.payload) is EffectObservationPayload
                and event.payload.adapter is AdapterKind.BACKEND
                and event.payload.receipt.operation
                is EffectOperation.CANCEL_TURN
                and event.payload.receipt.status is EffectStatus.APPLIED
                and event.payload.receipt.identity == request.identity
                and event.sequence > request.sequence
            )
            if len(recovered_cancellations) > 1:
                return None
            recovered_cancellation = None
            if recovered_cancellations:
                cancel_request, cancel_event = recovered_cancellations[0]
                pending_cancel = PendingExternalEffect(cancel_request)
                cancel_routes = self._routes_for_cursor(cursor, (pending_cancel,))
                cancel_router = self._router(cancel_routes)
                cancel_turn = cancel_router.inspect_turn(
                    pending_cancel.operation_id
                )
                try:
                    self._evidence_store.verify_existing(
                        cancel_turn,
                        identity=cancel_request.identity,
                        operation=EffectOperation.CANCEL_TURN,
                    )
                except Exception:
                    return None
                recovered_cancellation = (cancel_event, cancel_turn)
            task = next(item for item in self._manifest.tasks if item.id == attempt.task_id)
            command = self._repository.plan_attempt(
                identity,
                owned_paths=task.owned_paths,
                protected_paths=self._manifest.protected_paths,
                allowed_auxiliary_paths=task.allowed_auxiliary_paths,
                path_case_mode=self._manifest.path_case_mode,
            )
            inspected = self._repository.inspect_attempt(command)
            if (
                inspected.disposition is not AttemptEffectDisposition.APPLIED
                or type(inspected.value) is not AttemptWorktree
            ):
                return None
            owned_path_changes = self._repository.inspect_owned_path_changes(command)
            if owned_path_changes != ():
                return None
            reclaimed = self.coordinator(cursor).reclaim_cancelled_dispatch(
                requests[0],
                observations[0],
                owned_path_changes=owned_path_changes,
                recovered_cancellation=recovered_cancellation,
            )
        except Exception:
            return None
        if (
            reclaimed.status is not CoordinatorStatus.PROGRESSED
            or len(reclaimed.reserved) != 1
            or reclaimed.reserved[0].attempt != attempt.attempt
            or reclaimed.reserved[0].coordinator_epoch != self._fencing_token
            or len(reclaimed.cursor.snapshot.attempts)
            != len(cursor.snapshot.attempts)
        ):
            return None
        return reclaimed.cursor

    def _recover_absent_preparation(
        self,
        cursor: CoordinatorCursor,
    ) -> CoordinatorCursor | None:
        """Requeue one proven-absent pre-dispatch worktree attempt."""

        if cursor.snapshot.status is not RuntimeState.BLOCKED:
            return cursor
        if cursor.snapshot.run_reason_code is not RuntimeReasonCode.GIT_STATE_CONFLICT:
            return cursor
        if not self._retry_admitted():
            return None
        try:
            events = self._read_verified_events()
            tasks = {task.task_id: task for task in cursor.snapshot.tasks}
            attempts_by_task: dict[str, list[object]] = {}
            for attempt in cursor.snapshot.attempts:
                attempts_by_task.setdefault(attempt.task_id, []).append(attempt)
            candidates: list[tuple[JournalEvent, JournalEvent]] = []
            for request, observation in zip(events, events[1:], strict=False):
                payload = request.payload
                observed = observation.payload
                if (
                    request.event_type is not JournalEventType.EFFECT_REQUESTED
                    or type(payload) is not EffectRequestPayload
                    or payload.adapter is not AdapterKind.GIT
                    or payload.operation is not EffectOperation.REPOSITORY_UPDATE
                    or payload.object_type is not EffectObjectType.WORKTREE
                    or observation.event_type is not JournalEventType.EFFECT_OBSERVED
                    or type(observed) is not EffectObservationPayload
                    or observed.adapter is not AdapterKind.GIT
                    or observed.receipt.identity != request.identity
                    or observed.receipt.operation is not EffectOperation.REPOSITORY_UPDATE
                    or observed.receipt.status is not EffectStatus.ABSENT
                    or request.identity.task_id is None
                    or request.identity.attempt is None
                ):
                    continue
                task = tasks.get(request.identity.task_id)
                attempts = attempts_by_task.get(request.identity.task_id, [])
                if task is None or not attempts:
                    continue
                latest_attempt = max(item.attempt for item in attempts)
                current_attempt = next(
                    (
                        item
                        for item in attempts
                        if item.attempt == request.identity.attempt
                        and item.correlation_id == request.identity.correlation_id
                    ),
                    None,
                )
                if (
                    current_attempt is None
                    or current_attempt.attempt != latest_attempt
                    or current_attempt.state
                    not in {RuntimeState.RESERVED, RuntimeState.TERMINATED}
                    or task.state not in {RuntimeState.BLOCKED, RuntimeState.READY}
                    or (
                        task.state is RuntimeState.BLOCKED
                        and task.reason_code is not RuntimeReasonCode.GIT_STATE_CONFLICT
                    )
                ):
                    continue
                candidates.append((request, observation))
            if len(candidates) != 1:
                return cursor
            request, observation = candidates[0]
            assert request.identity.task_id is not None
            task = next(
                item
                for item in self._manifest.tasks
                if item.id == request.identity.task_id
            )
            command = self._repository.plan_attempt(
                request.identity,
                owned_paths=task.owned_paths,
                protected_paths=self._manifest.protected_paths,
                allowed_auxiliary_paths=task.allowed_auxiliary_paths,
                path_case_mode=self._manifest.path_case_mode,
            )
            payload = request.payload
            assert type(payload) is EffectRequestPayload
            if (
                payload.normalized_target_hash != command.target_workspace_hash
                or payload.request_payload_hash
                != "sha256:" + canonical_sha256(command.to_primitive())
            ):
                return None
            inspected = self._repository.inspect_attempt(command)
            if inspected.disposition is not AttemptEffectDisposition.ABSENT:
                return None
            retried = self.coordinator(cursor).retry_absent_preparation(
                request,
                observation,
            )
        except Exception:
            return None
        if (
            retried.status is not CoordinatorStatus.PROGRESSED
            or retried.cursor.snapshot.status is not RuntimeState.RUNNING
            or dict(retried.cursor.graph_index.task_states).get(request.identity.task_id)
            is not RuntimeState.READY
        ):
            return None
        return retried.cursor

    def coordinator(self, cursor: CoordinatorCursor) -> ForegroundCoordinator:
        self._require_active_cursor(cursor)
        routes = self._routes_for_cursor(cursor)
        effects = BackendDispatchEffectService(
            self._journal,
            self._router(routes),
            self._evidence_store,
            coordinator_id=self._coordinator_id,
            fencing_token=self._fencing_token,
        )
        return _ProductionForegroundCoordinator(
            self._manifest,
            cursor,
            self._journal,
            None,
            backend_effects=effects,
            backend_plan_factory=self._plan_factory,
            coordinator_id=self._coordinator_id,
            owner=self._owner,
            fencing_token=self._fencing_token,
            authority_clock=self._authority_clock,
            execution_snapshot_admitter=self._dispatch_runtime_admitted,
            prepare_lifecycle=self._project_prepare_lifecycle,
            complete_lifecycle=self._project_completion_lifecycle,
            project_task_event=self._project_committed_task_event,
        )

    def workflow(self, cursor: CoordinatorCursor) -> LocalExecutionWorkflow:
        self._require_active_cursor(cursor)
        return _ProductionLocalExecutionWorkflow(
            self._manifest,
            cursor,
            self._journal,
            self._repository,
            coordinator_id=self._coordinator_id,
            owner=self._owner,
            fencing_token=self._fencing_token,
            authority_clock=self._authority_clock,
            project_task_event=self._project_committed_task_event,
        )

    def _project_committed_task_event(self, event: JournalEvent) -> None:
        if type(event) is not JournalEvent:
            raise TypeError("event must be a JournalEvent")
        if self._projection_service is None:
            return
        try:
            result = self._projection_service.project_committed_event(event)
        except Exception:  # noqa: BLE001 - projection is a repairable derived view
            return
        if type(result) is TrellisProjectionSyncResult:
            self._projection_results.append(result)

    def _reconcile_task_projections(self, verified_head: JournalHead) -> None:
        if self._projection_service is None:
            return
        try:
            events = self._read_verified_events()
            results = self._projection_service.reconcile_verified_events(
                events,
                verified_head=verified_head,
            )
        except Exception:  # noqa: BLE001 - recovery authority remains the Journal
            return
        if type(results) is tuple and all(
            type(result) is TrellisProjectionSyncResult for result in results
        ):
            self._projection_results.extend(results)

    def run_workers(
        self,
        attempts: tuple[PreparedForegroundAttempt, ...],
        cursor: CoordinatorCursor,
    ) -> WorkerBatchResult:
        self._require_active_cursor(cursor)
        if type(attempts) is not tuple or not all(
            type(item) is PreparedForegroundAttempt for item in attempts
        ):
            raise TypeError("attempts must contain PreparedForegroundAttempt values")
        routes: list[AttemptChannelRoute] = []
        for prepared in attempts:
            if type(prepared.attempt) is not AttemptWorktree:
                return WorkerBatchResult(False, cursor=cursor)
            attempt = prepared.attempt
            assert prepared.identity.task_id is not None
            self._attempts[prepared.identity.task_id] = attempt
            routes.append(
                AttemptChannelRoute(attempt, self._plan_factory(prepared.identity))
            )
        router = self._router(tuple(routes))
        monitor = BackendWorkerTurnMonitor(
            router,
            self._evidence_store,
            self._plan_factory,
            timeout_seconds=self._config.worker_timeout_seconds,
            poll_interval_seconds=self._config.poll_interval_seconds,
            lease_renewal=self._renew_worker_lease,
        )
        result = monitor.run(attempts, cursor)
        if not result.outcomes_known:
            return result

        attempts_by_identity = {item.identity: item.attempt for item in attempts}
        proposals: list[WorkerResultProposal] = []
        proposals_changed = False
        for proposal in result.proposals:
            if not proposal.succeeded:
                proposals.append(proposal)
                continue
            attempt = attempts_by_identity.get(proposal.identity)
            if type(attempt) is not AttemptWorktree:
                return WorkerBatchResult(False, cursor=result.cursor, events=result.events)
            validation = self._repository.validate_result(
                attempt,
                process_tree_terminated=True,
            )
            if not validation.accepted:
                assert validation.reason_code is not None
                self._validated_results.pop(proposal.identity, None)
                proposals.append(
                    replace(
                        proposal,
                        succeeded=False,
                        reason_code=validation.reason_code,
                    )
                )
                proposals_changed = True
                continue
            self._validated_results[proposal.identity] = validation
            proposals.append(proposal)
        if not proposals_changed:
            return result
        return WorkerBatchResult(
            True,
            tuple(proposals),
            result.cursor,
            result.events,
        )

    def _project_prepare_lifecycle(
        self,
        cursor: CoordinatorCursor,
        identity: ExecutionIdentity,
    ) -> _LifecycleProjection:
        route = self._route_for_identity(cursor, identity)
        command = self._prepare_lifecycle_command(identity, route)
        service = self._lifecycle_service(route)
        return self._apply_lifecycle_command(
            service,
            identity,
            command,
            EffectOperation.PREPARE_ATTEMPT,
            cursor.head,
            observation_valid=lambda observation: (
                observation.worktree_path is not None
                and _comparable(Path(observation.worktree_path))
                == _comparable(Path(route.attempt.path))
            ),
        )

    def _project_completion_lifecycle(
        self,
        cursor: CoordinatorCursor,
        proposal: WorkerResultProposal,
    ) -> _LifecycleProjection:
        identity = proposal.identity
        validation = self._validated_results.get(identity)
        attempt = self._attempts.get(identity.task_id or "")
        if (
            not proposal.evidence
            or type(validation) is not ResultValidation
            or not validation.accepted
            or type(attempt) is not AttemptWorktree
            or attempt.identity != identity
            or not self._worker_evidence_matches(identity, proposal)
        ):
            return _LifecycleProjection(
                False,
                CoordinatorReason.PORT_OUTCOME_INVALID,
            )
        current_validation = self._repository.validate_result(
            attempt,
            process_tree_terminated=True,
        )
        if current_validation != validation or current_validation.manifest is None:
            return _LifecycleProjection(
                False,
                CoordinatorReason.PORT_OUTCOME_INVALID,
            )

        route = self._route_for_identity(cursor, identity)
        service = self._lifecycle_service(route)
        check = self._check_lifecycle_command(identity, route, current_validation)
        checked = self._apply_lifecycle_command(
            service,
            identity,
            check,
            EffectOperation.CHECK_ATTEMPT,
            cursor.head,
            observation_valid=lambda observation: observation.passed is True,
        )
        if not checked.succeeded:
            return checked

        finish_head = (
            JournalHead(
                checked.events[-1].sequence,
                checked.events[-1].event_hash,
            )
            if checked.events
            else cursor.head
        )
        finish = self._finish_lifecycle_command(
            identity,
            route,
            current_validation,
            proposal,
        )
        finished = self._apply_lifecycle_command(
            service,
            identity,
            finish,
            EffectOperation.FINISH_ATTEMPT,
            finish_head,
            observation_valid=lambda observation: observation.finished is True,
        )
        return _LifecycleProjection(
            finished.succeeded,
            finished.reason,
            checked.events + finished.events,
        )

    def _prepare_lifecycle_command(
        self,
        identity: ExecutionIdentity,
        route: AttemptChannelRoute,
    ) -> PrepareAttempt:
        if route.attempt.identity != identity:
            raise ValueError("prepare route does not match the attempt identity")
        if identity.task_id is None or identity.attempt is None:
            raise ValueError("prepare identity is incomplete")
        return PrepareAttempt(
            operation_id=route.plan.reserve.attempt_id,
            run_id=identity.run_id,
            parent_task_id=self._manifest.trellis_parent_task_id,
            trellis_task_id=self._trellis_task_ids[identity.task_id],
            task_id=identity.task_id,
            attempt=identity.attempt,
            dispatch_id=identity.correlation_id or "",
            manifest_digest=self._manifest.canonical_sha256(),
            trellis_graph_digest=self._manifest.trellis_graph_digest,
            expected_base_commit=route.attempt.base_commit_sha,
        )

    def _check_lifecycle_command(
        self,
        identity: ExecutionIdentity,
        route: AttemptChannelRoute,
        validation: ResultValidation,
    ) -> CheckAttempt:
        if (
            route.attempt.identity != identity
            or identity.task_id is None
            or not validation.accepted
            or validation.manifest is None
        ):
            raise ValueError("check command requires an accepted attempt result")
        suffix = route.plan.reserve.attempt_id.removeprefix("ATTEMPT-")
        return CheckAttempt(
            operation_id=f"CHECK-{suffix}",
            attempt_id=route.plan.reserve.attempt_id,
            trellis_task_id=self._trellis_task_ids[identity.task_id],
            task_id=identity.task_id,
            task_packet_digest=route.plan.send.task_packet_digest,
            expected_head_commit=validation.manifest.result_commit_sha,
        )

    def _finish_lifecycle_command(
        self,
        identity: ExecutionIdentity,
        route: AttemptChannelRoute,
        validation: ResultValidation,
        proposal: WorkerResultProposal,
    ) -> FinishAttempt:
        if (
            route.attempt.identity != identity
            or proposal.identity != identity
            or identity.task_id is None
            or not validation.accepted
            or validation.manifest is None
            or not proposal.succeeded
            or not proposal.evidence
            or not self._worker_evidence_matches(identity, proposal)
        ):
            raise ValueError("finish command requires an accepted worker result")
        suffix = route.plan.reserve.attempt_id.removeprefix("ATTEMPT-")
        return FinishAttempt(
            operation_id=f"FINISH-{suffix}",
            attempt_id=route.plan.reserve.attempt_id,
            trellis_task_id=self._trellis_task_ids[identity.task_id],
            task_id=identity.task_id,
            delivered_commit=validation.manifest.result_commit_sha,
            delivery_evidence_digest=self._delivery_evidence_digest(
                identity,
                validation,
                proposal,
            ),
        )

    def _resolve_recovery_command(
        self,
        pending: PendingExternalEffect,
        plan: BackendDispatchPlan | None,
        route: AttemptChannelRoute | None,
        router: AttemptBackendChannelRouter,
    ) -> ExternalRecoveryCommand | None:
        planned = resolve_external_recovery_command(pending, plan)
        if planned is not None:
            return planned
        recovered_operation = AttemptOperationRoute.from_pending(pending)
        route_key = (
            None
            if type(route) is not AttemptChannelRoute
            else self._attempt_key(route.attempt.identity)
        )
        pending_key = self._attempt_key(pending.request_event.identity)
        takeover_cancel = (
            pending.operation is EffectOperation.CANCEL_TURN
            and route_key is not None
            and route_key[0] == pending_key[0]
            and route_key[2:] == pending_key[2:]
            and route_key[1] < pending_key[1]
        )
        if (
            type(route) is not AttemptChannelRoute
            or type(plan) is not BackendDispatchPlan
            or route.plan != plan
            or recovered_operation not in route.recovery_operations
            or (route_key != pending_key and not takeover_cancel)
        ):
            return None

        identity = route.attempt.identity
        command: ExternalRecoveryCommand | None = None
        if pending.operation is EffectOperation.PREPARE_ATTEMPT:
            command = self._prepare_lifecycle_command(identity, route)
        elif takeover_cancel:
            cancel_suffix = canonical_sha256(
                {
                    "fencing_token": pending_key[1],
                    "identity": identity.to_primitive(),
                    "operation": EffectOperation.CANCEL_TURN.value,
                }
            )[:48].upper()
            if pending.operation_id != f"CANCEL-{cancel_suffix}":
                return None
            command = CancelTurn(
                operation_id=pending.operation_id,
                attempt_id=plan.send.attempt_id,
                channel_id=plan.send.channel_id,
                turn_id=plan.send.turn_id,
                reason_code="lease_lost_takeover",
            )
        elif pending.operation in {
            EffectOperation.CHECK_ATTEMPT,
            EffectOperation.FINISH_ATTEMPT,
        }:
            validation = self._repository.validate_result(
                route.attempt,
                process_tree_terminated=True,
            )
            if not validation.accepted or validation.manifest is None:
                return None
            turn = router.inspect_turn(plan.send.operation_id)
            if not self._successful_turn_matches(plan.send, turn):
                return None
            evidence_identity = replace(
                identity,
                correlation_id=plan.send.operation_id,
            )
            try:
                worker_evidence = self._evidence_store.verify_existing(
                    turn,
                    identity=evidence_identity,
                    operation=EffectOperation.SEND_TASK_PACKET,
                )
            except Exception:
                return None
            if not self._has_applied_trellis_operation(
                identity,
                EffectOperation.SEND_TASK_PACKET,
                plan.send.operation_id,
                plan.send.canonical_sha256(),
                observation=turn,
                evidence=worker_evidence,
            ):
                return None
            if pending.operation is EffectOperation.CHECK_ATTEMPT:
                command = self._check_lifecycle_command(identity, route, validation)
            else:
                check = self._check_lifecycle_command(identity, route, validation)
                checked = router.inspect_check(check.operation_id)
                if (
                    not self._successful_check_matches(check, checked)
                    or not self._has_applied_lifecycle_observation(
                        identity,
                        check,
                        checked,
                    )
                ):
                    return None
                proposal = WorkerResultProposal(
                    identity,
                    f"backend-turn:{plan.send.turn_id}",
                    True,
                    evidence=(worker_evidence,),
                )
                command = self._finish_lifecycle_command(
                    identity,
                    route,
                    validation,
                    proposal,
                )

        if (
            command is None
            or command.operation_id != pending.operation_id
            or command.canonical_sha256()
            != pending.request_event.payload.request_payload_hash
        ):
            return None
        return command

    @staticmethod
    def _successful_turn_matches(
        command: SendTaskPacket,
        observation: object,
    ) -> bool:
        return (
            type(observation) is TurnObservation
            and observation.operation_id == command.operation_id
            and observation.status is EffectStatus.APPLIED
            and observation.state is TurnState.DONE
            and observation.attempt_id == command.attempt_id
            and observation.channel_id == command.channel_id
            and observation.message_id == command.message_id
            and observation.turn_id == command.turn_id
            and observation.result_digest is not None
        )

    @staticmethod
    def _successful_check_matches(
        command: CheckAttempt,
        observation: object,
    ) -> bool:
        return (
            type(observation) is CheckObservation
            and observation.operation_id == command.operation_id
            and observation.status is EffectStatus.APPLIED
            and observation.attempt_id == command.attempt_id
            and observation.passed is True
            and observation.head_commit == command.expected_head_commit
            and observation.check_digest is not None
        )

    def _has_applied_trellis_operation(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
        operation_id: str,
        request_payload_hash: str,
        *,
        observation: TurnObservation | None = None,
        evidence: object | None = None,
    ) -> bool:
        expected_identity = replace(identity, correlation_id=operation_id)
        requests = 0
        applied = 0
        try:
            events = self._read_verified_events()
        except (OSError, TypeError, ValueError):
            return False
        for event in events:
            payload = event.payload
            if (
                event.event_type is JournalEventType.EFFECT_REQUESTED
                and event.identity == expected_identity
                and type(payload) is EffectRequestPayload
                and payload.adapter is AdapterKind.BACKEND
                and payload.operation is operation
                and payload.request_payload_hash == request_payload_hash
            ):
                requests += 1
            elif (
                event.event_type
                in {
                    JournalEventType.EFFECT_OBSERVED,
                    JournalEventType.EFFECT_RECONCILED,
                }
                and type(payload) is EffectObservationPayload
                and payload.adapter is AdapterKind.BACKEND
                and payload.receipt.identity == expected_identity
                and payload.receipt.operation is operation
                and payload.receipt.status is EffectStatus.APPLIED
            ):
                receipt = payload.receipt
                if observation is None:
                    applied += 1
                elif (
                    receipt.observed_at == observation.observed_at
                    and receipt.effect_hash == observation.effect_digest
                    and receipt.external_object_id == observation.turn_id
                    and receipt.evidence == (evidence,)
                ):
                    applied += 1
        return requests == 1 and applied == 1

    def _has_applied_lifecycle_observation(
        self,
        identity: ExecutionIdentity,
        command: CheckAttempt,
        observation: CheckObservation,
    ) -> bool:
        evidence_identity = replace(
            identity,
            correlation_id=command.operation_id,
        )
        try:
            evidence = self._evidence_store.verify_existing(
                observation,
                identity=evidence_identity,
                operation=EffectOperation.CHECK_ATTEMPT,
            )
            events = self._read_verified_events()
        except Exception:
            return False
        request_count = 0
        receipt_count = 0
        for event in events:
            payload = event.payload
            if (
                event.event_type is JournalEventType.EFFECT_REQUESTED
                and event.identity == evidence_identity
                and type(payload) is EffectRequestPayload
                and payload.adapter is AdapterKind.TRELLIS
                and payload.operation is EffectOperation.CHECK_ATTEMPT
                and payload.request_payload_hash == command.canonical_sha256()
            ):
                request_count += 1
            elif (
                event.event_type
                in {
                    JournalEventType.EFFECT_OBSERVED,
                    JournalEventType.EFFECT_RECONCILED,
                }
                and type(payload) is EffectObservationPayload
                and payload.adapter is AdapterKind.TRELLIS
            ):
                receipt = payload.receipt
                if (
                    receipt.identity == evidence_identity
                    and receipt.operation is EffectOperation.CHECK_ATTEMPT
                    and receipt.status is EffectStatus.APPLIED
                    and receipt.observed_at == observation.observed_at
                    and receipt.effect_hash == observation.effect_digest
                    and receipt.external_object_id == observation.attempt_id
                    and receipt.evidence == (evidence,)
                ):
                    receipt_count += 1
        return request_count == 1 and receipt_count == 1

    def _route_for_identity(
        self,
        cursor: CoordinatorCursor,
        identity: ExecutionIdentity,
    ) -> AttemptChannelRoute:
        routes = tuple(
            route
            for route in self._routes_for_cursor(cursor)
            if route.attempt.identity == identity
        )
        if len(routes) != 1:
            raise ValueError("the lifecycle attempt route is not uniquely recoverable")
        return routes[0]

    def _lifecycle_service(
        self,
        route: AttemptChannelRoute,
    ) -> TrellisLifecycleEffectService:
        return TrellisLifecycleEffectService(
            self._journal,
            self._router((route,)),
            self._evidence_store,
            coordinator_id=self._coordinator_id,
            fencing_token=self._fencing_token,
        )

    def _apply_lifecycle_command(
        self,
        service: TrellisLifecycleEffectService,
        identity: ExecutionIdentity,
        command: PrepareAttempt | CheckAttempt | FinishAttempt,
        operation: EffectOperation,
        expected_head: JournalHead,
        *,
        observation_valid: Callable[[object], bool],
    ) -> _LifecycleProjection:
        request_identity = replace(identity, correlation_id=command.operation_id)
        command_hash = command.canonical_sha256()
        completed = self._completed_lifecycle_commands.get(command.operation_id)
        if completed is not None:
            if completed != (request_identity, operation, command_hash):
                return _LifecycleProjection(
                    False,
                    CoordinatorReason.PORT_OUTCOME_INVALID,
                )
            return _LifecycleProjection(True, CoordinatorReason.NONE)

        if operation is EffectOperation.PREPARE_ATTEMPT:
            assert type(command) is PrepareAttempt
            result = service.prepare(identity, command, expected_head=expected_head)
        elif operation is EffectOperation.CHECK_ATTEMPT:
            assert type(command) is CheckAttempt
            result = service.check(identity, command, expected_head=expected_head)
        else:
            assert operation is EffectOperation.FINISH_ATTEMPT
            assert type(command) is FinishAttempt
            result = service.finish(identity, command, expected_head=expected_head)

        if (
            result.status is TrellisLifecycleEffectStatus.APPLIED
            and result.observation is not None
            and observation_valid(result.observation)
        ):
            self._completed_lifecycle_commands[command.operation_id] = (
                request_identity,
                operation,
                command_hash,
            )
            return _LifecycleProjection(
                True,
                CoordinatorReason.NONE,
                result.events,
            )
        return _LifecycleProjection(
            False,
            self._lifecycle_failure_reason(result.reason),
            result.events,
        )

    @staticmethod
    def _lifecycle_failure_reason(
        reason: TrellisLifecycleEffectReason,
    ) -> CoordinatorReason:
        if reason is TrellisLifecycleEffectReason.JOURNAL_CONFLICT:
            return CoordinatorReason.JOURNAL_CONFLICT
        if reason in {
            TrellisLifecycleEffectReason.PERSISTENCE_FAILED,
            TrellisLifecycleEffectReason.EVIDENCE_NOT_DURABLE,
        }:
            return CoordinatorReason.PERSISTENCE_FAILED
        if reason is TrellisLifecycleEffectReason.EFFECT_ABSENT:
            return CoordinatorReason.EFFECT_ABSENT_AFTER_APPLY
        if reason is TrellisLifecycleEffectReason.EFFECT_OUTCOME_UNKNOWN:
            return CoordinatorReason.EFFECT_OUTCOME_UNKNOWN
        return CoordinatorReason.PORT_OUTCOME_INVALID

    @staticmethod
    def _worker_evidence_matches(
        identity: ExecutionIdentity,
        proposal: WorkerResultProposal,
    ) -> bool:
        return all(
            evidence.producer.identity.run_id == identity.run_id
            and evidence.producer.identity.coordinator_epoch
            == identity.coordinator_epoch
            and evidence.producer.identity.task_id == identity.task_id
            and evidence.producer.identity.attempt == identity.attempt
            for evidence in proposal.evidence
        )

    @staticmethod
    def _delivery_evidence_digest(
        identity: ExecutionIdentity,
        validation: ResultValidation,
        proposal: WorkerResultProposal,
    ) -> str:
        evidence = sorted(
            (item.to_primitive() for item in proposal.evidence),
            key=canonical_json_bytes,
        )
        return "sha256:" + canonical_sha256(
            {
                "identity": identity.to_primitive(),
                "kind": "wish_builder_delivery_evidence",
                "result_validation_evidence": validation.evidence_hash,
                "schema_version": 1,
                "worker_evidence": evidence,
            }
        )

    def cleanup_attempt(
        self,
        workflow: LocalExecutionWorkflow,
        attempt: object,
        promotion: object,
    ) -> CleanupStepResult:
        if not isinstance(workflow, LocalExecutionWorkflow):
            raise TypeError("workflow must be a LocalExecutionWorkflow")
        if type(attempt) is not AttemptWorktree:
            raise TypeError("attempt must be an AttemptWorktree")
        if type(promotion) is not PromotionRecord:
            raise TypeError("promotion must be a PromotionRecord")
        candidate = CleanupCandidate(
            attempt,
            promotion.source_commit_sha,
            promotion.acceptance_evidence,
            reconciliation_complete=True,
            process_tree_terminated=True,
            outcome_known=True,
        )
        operation_id = (
            f"CLEANUP-{attempt.task_id}-{attempt.attempt_number:04d}-"
            f"EPOCH-{self._fencing_token:04d}"
        )
        return workflow.cleanup_attempt(
            self._cleanup,
            candidate,
            operation_id=operation_id,
        )

    def publish_checkpoint(
        self,
        cursor: CoordinatorCursor,
        events: tuple[JournalEvent, ...],
    ) -> ExecutionCheckpointResult:
        if type(cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        if type(events) is not tuple or not events or not all(
            type(event) is JournalEvent for event in events
        ):
            return ExecutionCheckpointResult(
                ExecutionCheckpointStatus.BLOCKED,
                ExecutionCheckpointReason.STATE_MISMATCH,
            )
        return self._checkpoint_publisher.observe(
            cursor.snapshot,
            cursor.graph_index,
            cursor.head,
            events[-1],
        )

    def finish(self, cursor: CoordinatorCursor) -> ForegroundTerminalResult:
        recovered_release = None
        if (
            not cursor.lease_state.active
            and self._last_recovery is not None
            and self._last_recovery.last_lease_event is not None
            and self._last_recovery.last_lease_event.event_hash
            == cursor.head.event_hash
        ):
            recovered_release = self._last_recovery.last_lease_event
        return ProductionTerminalFinalizer(
            self._manifest,
            self._journal,
            self._lease_service,
            self._checkpoint_publisher,
            coordinator_id=self._coordinator_id,
            fencing_token=cursor.snapshot.coordinator_epoch,
            recovered_terminal_event=recovered_release,
        ).finish(cursor)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._control_root.close()

    def _recover(self) -> LeaseRecoveryResult:
        return recover_coordinator_lease(
            self._config.layout.journal_root,
            self._manifest,
            coordinator_epoch=1,
            checkpoint_store=self._checkpoint_store,
            repair_derived=True,
            control_root_validator=self.protect_control_root,
        )

    def _cursor_from_recovery(
        self,
        recovery: LeaseRecoveryResult,
    ) -> CoordinatorCursor | None:
        if (
            type(recovery) is not LeaseRecoveryResult
            or recovery.status is not LeaseRecoveryStatus.RECOVERED
            or recovery.lease_state is None
            or recovery.replay.head != recovery.lease_state.head
        ):
            return None
        try:
            cursor = CoordinatorCursor(
                recovery.replay.snapshot,
                recovery.replay.graph_index,
                recovery.lease_state,
                recovery.dispatch_recoveries,
            )
        except (TypeError, ValueError):
            return None
        return (
            cursor
            if cursor.graph_index.verify(self._manifest, cursor.snapshot)
            else None
        )

    def _require_active_cursor(self, cursor: CoordinatorCursor) -> None:
        if type(cursor) is not CoordinatorCursor:
            raise TypeError("cursor must be a CoordinatorCursor")
        lease = cursor.lease_state.lease
        if (
            self._closed
            or not cursor.lease_state.active
            or lease is None
            or lease.owner != self._owner
            or lease.fencing_token != self._fencing_token
            or cursor.snapshot.coordinator_epoch != self._fencing_token
            or not cursor.graph_index.verify(self._manifest, cursor.snapshot)
        ):
            raise ValueError("cursor is not admitted for this coordinator lease")

    def _routes_for_cursor(
        self,
        cursor: CoordinatorCursor,
        pending: tuple[PendingExternalEffect, ...] = (),
    ) -> tuple[AttemptChannelRoute, ...]:
        recovery_by_key: dict[
            tuple[str, int, str, int], list[AttemptOperationRoute]
        ] = {}
        projected_keys = {
            (
                self._manifest.run_id,
                projected.coordinator_epoch,
                projected.task_id,
                projected.attempt,
            )
            for projected in cursor.snapshot.attempts
            if projected.correlation_id is not None
        }
        for item in pending:
            operation = AttemptOperationRoute.from_pending(item)
            route_key = _recovery_route_key(operation, projected_keys)
            recovery_by_key.setdefault(route_key, []).append(operation)

        if recovery_by_key:
            for event in self._read_verified_events():
                payload = event.payload
                if (
                    event.sequence > cursor.head.sequence
                    or event.event_type is not JournalEventType.EFFECT_REQUESTED
                    or type(payload) is not EffectRequestPayload
                    or payload.adapter is not AdapterKind.TRELLIS
                    or payload.operation is not EffectOperation.CHECK_ATTEMPT
                ):
                    continue
                key = self._attempt_key(event.identity)
                if key not in recovery_by_key:
                    continue
                operation = AttemptOperationRoute(
                    event.identity,
                    payload.operation,
                    payload.request_payload_hash,
                )
                existing = next(
                    (
                        item
                        for item in recovery_by_key[key]
                        if item.identity.correlation_id
                        == operation.identity.correlation_id
                    ),
                    None,
                )
                if existing is None:
                    recovery_by_key[key].append(operation)
                elif existing != operation:
                    raise ValueError("lifecycle recovery operation identity conflicts")

        task_by_id = {task.id: task for task in self._manifest.tasks}
        routes: list[AttemptChannelRoute] = []
        found: set[tuple[str, int, str, int]] = set()
        for projected in cursor.snapshot.attempts:
            if projected.correlation_id is None:
                continue
            identity = ExecutionIdentity(
                self._manifest.run_id,
                projected.coordinator_epoch,
                projected.task_id,
                projected.attempt,
                projected.correlation_id,
            )
            key = self._attempt_key(identity)
            task = task_by_id.get(projected.task_id)
            if task is None:
                continue
            command = self._repository.plan_attempt(
                identity,
                owned_paths=task.owned_paths,
                protected_paths=self._manifest.protected_paths,
                allowed_auxiliary_paths=task.allowed_auxiliary_paths,
                path_case_mode=self._manifest.path_case_mode,
            )
            observed = self._repository.inspect_attempt(command)
            if (
                observed.disposition is not AttemptEffectDisposition.APPLIED
                or type(observed.value) is not AttemptWorktree
            ):
                if key in recovery_by_key:
                    raise ValueError("pending external effect has no exact attempt worktree")
                continue
            attempt = observed.value
            self._attempts[projected.task_id] = attempt
            found.add(key)
            routes.append(
                AttemptChannelRoute(
                    attempt,
                    self._plan_factory(identity),
                    tuple(recovery_by_key.get(key, ())),
                )
            )
        if set(recovery_by_key) != found.intersection(recovery_by_key):
            raise ValueError("pending external effect attempt is not recoverable")
        return tuple(
            sorted(
                routes,
                key=lambda route: self._attempt_key(route.attempt.identity),
            )
        )

    def _router(
        self,
        routes: tuple[AttemptChannelRoute, ...],
    ) -> AttemptBackendChannelRouter:
        return AttemptBackendChannelRouter(
            routes,
            expected_capabilities=self._config.channel_capabilities,
            channel_factory=self._channel_factory,
            lifecycle_factory=self._lifecycle_factory,
        )

    def _renew_worker_lease(
        self,
        cursor: CoordinatorCursor,
    ) -> WorkerLeaseRenewalResult:
        try:
            self._require_active_cursor(cursor)
            mutation = self._lease_service.renew(
                event_id=(
                    "EVENT-PRODUCTION-LEASE-RENEWED-"
                    f"{cursor.head.sequence + 1:08d}"
                )
            )
            event = (
                None
                if mutation.append_result is None
                else mutation.append_result.event
            )
            recovery = self._recover()
            renewed = self._cursor_from_recovery(recovery)
            if (
                mutation.status
                not in {LeaseMutationStatus.COMMITTED, LeaseMutationStatus.IDEMPOTENT}
                or event is None
                or renewed is None
                or event.sequence != cursor.head.sequence + 1
                or event.previous_event_hash != cursor.head.event_hash
                or renewed.head.sequence != event.sequence
                or renewed.head.event_hash != event.event_hash
            ):
                return WorkerLeaseRenewalResult(False)
            self._last_recovery = recovery
            return WorkerLeaseRenewalResult(True, renewed, event)
        except Exception:
            return WorkerLeaseRenewalResult(False)

    def renew_lease(self, cursor: CoordinatorCursor) -> WorkerLeaseRenewalResult:
        """Durably renew the coordinator lease before a foreground effect."""

        return self._renew_worker_lease(cursor)

    def _retry_admitted(self) -> bool:
        try:
            return (
                admit_backend(self._manifest).admitted
                and self._lifecycle_factory is not None
                and self.protect_control_root()
                and self.verify_workspace_identity(self._manifest)
                and self._live_graph_admitted()
            )
        except Exception:
            return False

    def _live_graph_admitted(self) -> bool:
        try:
            return self._graph_admission.admit().admitted
        except Exception:
            return False

    def _dispatch_runtime_admitted(self) -> bool:
        return self._lifecycle_factory is not None and self._live_graph_admitted()

    def _refresh_completed_lifecycle_commands(self) -> None:
        lifecycle_operations = {
            EffectOperation.PREPARE_ATTEMPT,
            EffectOperation.CHECK_ATTEMPT,
            EffectOperation.FINISH_ATTEMPT,
        }
        requests: dict[
            tuple[str, int, str | None, int | None, str | None],
            tuple[ExecutionIdentity, EffectOperation, str],
        ] = {}
        completed: dict[
            str,
            tuple[ExecutionIdentity, EffectOperation, str],
        ] = {}
        for event in self._read_verified_events():
            payload = event.payload
            if (
                event.event_type is JournalEventType.EFFECT_REQUESTED
                and type(payload) is EffectRequestPayload
                and payload.adapter is AdapterKind.TRELLIS
                and payload.operation in lifecycle_operations
            ):
                key = self._lifecycle_identity_key(event.identity)
                if key in requests:
                    raise ValueError("lifecycle operation identity is reused")
                requests[key] = (
                    event.identity,
                    payload.operation,
                    payload.request_payload_hash,
                )
                continue
            if (
                event.event_type
                not in {
                    JournalEventType.EFFECT_OBSERVED,
                    JournalEventType.EFFECT_RECONCILED,
                }
                or type(payload) is not EffectObservationPayload
                or payload.adapter is not AdapterKind.TRELLIS
                or payload.receipt.operation not in lifecycle_operations
                or payload.receipt.status is not EffectStatus.APPLIED
            ):
                continue
            receipt = payload.receipt
            key = self._lifecycle_identity_key(receipt.identity)
            request = requests.get(key)
            operation_id = receipt.identity.correlation_id
            if (
                request is None
                or operation_id is None
                or request[0] != receipt.identity
                or request[1] is not receipt.operation
                or operation_id in completed
            ):
                raise ValueError("applied lifecycle observation has no exact request")
            completed[operation_id] = request
        self._completed_lifecycle_commands = completed

    @staticmethod
    def _lifecycle_identity_key(
        identity: ExecutionIdentity,
    ) -> tuple[str, int, str | None, int | None, str | None]:
        return (
            identity.run_id,
            identity.coordinator_epoch,
            identity.task_id,
            identity.attempt,
            identity.correlation_id,
        )

    def _read_verified_events(self) -> tuple[JournalEvent, ...]:
        if not self.protect_control_root():
            raise ValueError("control_root_drift")
        segments_root = self._config.layout.journal_root / "segments"
        try:
            entries = tuple(segments_root.iterdir())
        except FileNotFoundError:
            return ()
        numbered: list[tuple[int, Path]] = []
        for entry in entries:
            match = _SEGMENT_RE.fullmatch(entry.name)
            if (
                match is None
                or entry.is_symlink()
                or not entry.is_file()
            ):
                raise ValueError("journal segment layout is invalid")
            numbered.append((int(match.group(1)), entry))
        numbered.sort(key=lambda item: item[0])
        if not numbered:
            return ()
        if tuple(number for number, _ in numbered) != tuple(
            range(1, numbered[-1][0] + 1)
        ):
            raise ValueError("journal segment layout is non-contiguous")

        events: list[JournalEvent] = []
        head = GENESIS_HEAD
        for _, segment in numbered:
            frames = segment.read_bytes().splitlines(keepends=True)
            if not frames:
                raise ValueError("journal segment is empty")
            for frame in frames:
                if len(events) >= _MAX_ADMISSION_EVENTS:
                    raise ValueError("journal admission event limit exceeded")
                decoded = decode_journal_event_bytes(frame)
                if not decoded.ok or decoded.value is None:
                    raise ValueError("journal event failed strict decoding")
                event = decoded.value
                if (
                    event.identity.run_id != self._manifest.run_id
                    or event.sequence != head.sequence + 1
                    or event.previous_event_hash != head.event_hash
                    or frame != event.canonical_json_bytes()
                ):
                    raise ValueError("journal hash chain is invalid")
                events.append(event)
                head = JournalHead(event.sequence, event.event_hash)
        self._journal.current_position(expected_head=head)
        if not self.protect_control_root():
            raise ValueError("control_root_drift")
        return tuple(events)

    @staticmethod
    def _attempt_key(
        identity: ExecutionIdentity,
    ) -> tuple[str, int, str, int]:
        if (
            type(identity) is not ExecutionIdentity
            or not identity.is_attempt
            or identity.task_id is None
            or identity.attempt is None
        ):
            raise ValueError("attempt identity is incomplete")
        return (
            identity.run_id,
            identity.coordinator_epoch,
            identity.task_id,
            identity.attempt,
        )


__all__ = [
    "DeterministicBackendDispatchPlanFactory",
    "ProductionForegroundRunComponents",
    "ProductionRuntimeConfig",
    "ProductionRuntimeLayout",
    "channel_capabilities_from_compatibility",
]
