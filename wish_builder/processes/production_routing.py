"""Attempt-aware routing for Wish Builder backend operations.

A provider adapter fixes its working directory at construction time.  A single
adapter therefore cannot safely serve sibling attempts: every child effect must
run from the Git worktree prepared for that exact attempt.  This module supplies
the narrow routing layer between the journal-owned Channel effects and
per-attempt provider adapters.

Routes are rebuilt from immutable ``AttemptWorktree`` values, deterministic
dispatch plans, and any pending child operations recovered from the Journal.
Task readiness and ``GraphIndex`` are intentionally outside this boundary.
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from wish_builder.adapters.git_identity import (
    GitIdentityError,
    capture_filesystem_identity,
)
from wish_builder.adapters.git_worktree import AttemptWorktree
from wish_builder.adapters.providers import (
    CodexAppServerChannel,
    CodexAppServerConfig,
    CodexAppServerLaunch,
    JsonlRpcBackendChannel,
    JsonlRpcBackendConfig,
    JsonlRpcLaunch,
    JsonlRpcProtocol,
)
from wish_builder.compatibility import load_bundled_backend_version_registry
from wish_builder.contracts import WorkerProvider
from wish_builder.contracts.backend_registry import (
    BackendProtocolProfile,
    BackendVersionQualificationRecord,
    BackendVersionRegistry,
    BackendVersionStatus,
)
from wish_builder.contracts.compatibility import (
    Platform,
    PlatformCompatibility,
    Provider,
    SdkPin,
    NPM_INTEGRITY_RE,
    VERSION_RE,
)
from wish_builder.contracts.models import HASH_RE
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectStatus,
    ExecutionIdentity,
)
from wish_builder.services.dispatch_recovery import PendingExternalEffect
from wish_builder.services.ports import (
    AttemptObservation,
    BackendCapabilities,
    BackendChannelPort,
    CancelTurn,
    ChannelObservation,
    CheckAttempt,
    CheckObservation,
    FinishAttempt,
    FinishObservation,
    PrepareAttempt,
    PreparedEffect,
    ReserveChannel,
    SendTaskPacket,
    TrellisLifecyclePort,
    TrellisLifecycleState,
    TurnObservation,
    TurnState,
)
from wish_builder.services.backend_effects import BackendDispatchPlan

AttemptChannelFactory = Callable[[AttemptWorktree], BackendChannelPort]
AttemptLifecycleFactory = Callable[[AttemptWorktree], TrellisLifecyclePort]
RoutingClock = Callable[[], str]

_MAX_PROVIDER_METADATA_BYTES = 16 * 1024 * 1024

_EXTERNAL_EFFECT_OPERATIONS = frozenset(
    {
        EffectOperation.PREPARE_ATTEMPT,
        EffectOperation.RESERVE_CHANNEL,
        EffectOperation.SEND_TASK_PACKET,
        EffectOperation.CANCEL_TURN,
        EffectOperation.CHECK_ATTEMPT,
        EffectOperation.FINISH_ATTEMPT,
    }
)
_OBJECT_TYPES = {
    EffectOperation.PREPARE_ATTEMPT: EffectObjectType.ATTEMPT,
    EffectOperation.RESERVE_CHANNEL: EffectObjectType.CHANNEL,
    EffectOperation.SEND_TASK_PACKET: EffectObjectType.TASK_PACKET,
    EffectOperation.CANCEL_TURN: EffectObjectType.TURN,
    EffectOperation.CHECK_ATTEMPT: EffectObjectType.ATTEMPT,
    EffectOperation.FINISH_ATTEMPT: EffectObjectType.ATTEMPT,
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class BackendDispatchUnavailable(RuntimeError):
    """Raised before effects when no qualified Wish Builder backend can run."""


class ProviderSdkUnavailable(BackendDispatchUnavailable):
    """Raised when an explicitly selected provider SDK cannot be admitted."""


@dataclass(frozen=True, slots=True)
class ProviderSdkResolution:
    """A verified, immutable executable entrypoint for one provider SDK."""

    provider: WorkerProvider
    package_name: str
    package_version: str
    package_shasum: str
    package_integrity: str
    protocol_profile: str
    package_root: Path
    entrypoint: Path
    runtime: Path


@dataclass(frozen=True, slots=True)
class _ProviderSdkSpec:
    package_name: str
    version: str
    shasum: str
    integrity: str
    bin_name: str
    entrypoint: str
    runtime: str
    protocol_profile: str


_CONTRACT_PROVIDERS = {
    WorkerProvider.CODEX: Provider.CODEX,
    WorkerProvider.OH_MY_PI: Provider.OMP,
    WorkerProvider.PI: Provider.PI,
}


def _spec_from_record(
    profile: BackendProtocolProfile,
    record: BackendVersionQualificationRecord,
) -> _ProviderSdkSpec:
    return _ProviderSdkSpec(
        profile.package_name,
        record.backend_version,
        record.package_shasum,
        record.package_integrity,
        profile.bin_name,
        profile.entrypoint,
        profile.runtime,
        profile.profile_id,
    )


def _default_sdk_specs() -> dict[WorkerProvider, _ProviderSdkSpec]:
    """Compatibility view retained for tests and diagnostics, not admission."""

    registry = load_bundled_backend_version_registry()
    result: dict[WorkerProvider, _ProviderSdkSpec] = {}
    for worker, provider in _CONTRACT_PROVIDERS.items():
        candidates = tuple(item for item in registry.records if item.provider is provider)
        selected = next(
            (item for item in candidates if item.status is BackendVersionStatus.QUALIFIED),
            candidates[0],
        )
        result[worker] = _spec_from_record(
            registry.profile(selected.protocol_profile), selected
        )
    return result


_PROVIDER_SDK_SPECS = _default_sdk_specs()


class BackendVersionProbeStatus(StrEnum):
    UNKNOWN = "unknown"
    CANDIDATE = "candidate"
    QUALIFIED = "qualified"
    QUARANTINED = "quarantined"
    DRIFT = "drift"


@dataclass(frozen=True, slots=True)
class BackendVersionProbeResult:
    provider: WorkerProvider
    platform: Platform
    package_name: str
    backend_version: str
    protocol_profile: str
    protocol: str
    launch_profile_digest: str
    observed_integrity: str
    status: BackendVersionProbeStatus
    enabled_for_dispatch: bool
    max_concurrency: int
    evidence_digest: str | None
    reason: str

    def to_primitive(self) -> dict[str, object]:
        return {
            "backendVersion": self.backend_version,
            "enabledForDispatch": self.enabled_for_dispatch,
            "evidenceDigest": self.evidence_digest,
            "launchProfileDigest": self.launch_profile_digest,
            "maxConcurrency": self.max_concurrency,
            "npmIntegrity": self.observed_integrity,
            "packageName": self.package_name,
            "platform": self.platform.value,
            "protocol": self.protocol,
            "protocolProfile": self.protocol_profile,
            "provider": self.provider.value,
            "reason": self.reason,
            "status": self.status.value,
        }


def _provider_for_cell(cell: PlatformCompatibility) -> WorkerProvider:
    try:
        return {
            "codex": WorkerProvider.CODEX,
            "pi": WorkerProvider.PI,
            "omp": WorkerProvider.OH_MY_PI,
        }[cell.capabilities.provider.value]
    except (AttributeError, KeyError) as exc:  # pragma: no cover - guarded by type
        raise ProviderSdkUnavailable("compatibility cell has an unsupported provider") from exc


def _safe_absolute_directory(value: str | os.PathLike[str], field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProviderSdkUnavailable(f"{field_name} must be an absolute directory")
    if not path.exists() or not path.is_dir() or path.is_symlink():
        raise ProviderSdkUnavailable(
            f"{field_name} must identify an existing non-link directory"
        )
    return path.resolve(strict=True)


def _read_json_object(path: Path, description: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ProviderSdkUnavailable(f"{description} is missing or is a link")
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_PROVIDER_METADATA_BYTES:
            raise ProviderSdkUnavailable(f"{description} exceeds the byte limit")
        text = raw.decode("utf-8", errors="strict")

        def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise ProviderSdkUnavailable(
                        f"{description} contains a duplicate JSON key"
                    )
                result[key] = item
            return result

        def reject_constant(value: str) -> None:
            raise ProviderSdkUnavailable(
                f"{description} contains a non-finite JSON number: {value}"
            )

        value = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except ProviderSdkUnavailable:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ProviderSdkUnavailable(f"{description} is not valid JSON") from exc
    if type(value) is not dict:
        raise ProviderSdkUnavailable(f"{description} must contain a JSON object")
    return value


def _package_lock_entry(
    lock: dict[str, object], package_name: str, package_root: Path, lock_root: Path
) -> dict[str, object] | None:
    packages = lock.get("packages")
    if type(packages) is dict:
        try:
            key = package_root.relative_to(lock_root).as_posix()
        except ValueError:
            key = f"node_modules/{package_name}"
        entry = packages.get(key)
        if type(entry) is dict:
            return entry
    dependencies = lock.get("dependencies")
    if type(dependencies) is dict:
        scope, name = package_name.rsplit("/", 1)
        scoped = dependencies.get(scope.lstrip("@"))
        if type(scoped) is dict:
            scoped_entry = scoped.get(name)
            if type(scoped_entry) is dict:
                return scoped_entry
        entry = dependencies.get(package_name)
        if type(entry) is dict:
            return entry
    return None


def _dependency_version(root_manifest: dict[str, object], package_name: str) -> object:
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        values = root_manifest.get(section)
        if type(values) is dict and package_name in values:
            return values[package_name]
    return None


@dataclass(frozen=True, slots=True)
class _ProviderSdkInspection:
    provider: WorkerProvider
    platform: Platform
    profile: BackendProtocolProfile
    record: BackendVersionQualificationRecord | None
    root: Path
    package_root: Path
    entrypoint: Path
    version: str
    integrity: str
    status: BackendVersionProbeStatus
    reason: str


def _profile_for_cell(
    cell: PlatformCompatibility,
    registry: BackendVersionRegistry,
) -> tuple[WorkerProvider, Provider, BackendProtocolProfile]:
    worker = _provider_for_cell(cell)
    provider = _CONTRACT_PROVIDERS[worker]
    try:
        profile = registry.profile_for_protocol(
            provider,
            cell.launch_profile.protocol,
        )
    except KeyError as exc:
        raise ProviderSdkUnavailable(
            "backend launch protocol has no admitted adapter profile"
        ) from exc
    return worker, provider, profile


def _provider_package_root(
    root: Path,
    profile: BackendProtocolProfile,
) -> tuple[Path, dict[str, object] | None]:
    direct_manifest_path = root / "package.json"
    direct_manifest = (
        _read_json_object(direct_manifest_path, "provider SDK package.json")
        if direct_manifest_path.is_file()
        else None
    )
    if direct_manifest is not None and direct_manifest.get("name") == profile.package_name:
        return root, direct_manifest

    package_candidate = root / "node_modules" / Path(profile.package_name)
    if not package_candidate.exists() or not package_candidate.is_dir():
        raise ProviderSdkUnavailable(
            f"provider SDK package {profile.package_name} is not installed under "
            "the explicit root"
        )
    if package_candidate.is_symlink():
        raise ProviderSdkUnavailable("provider SDK package directory must not be a link")
    package_root = package_candidate.resolve(strict=True)
    try:
        package_root.relative_to(root)
    except ValueError as exc:
        raise ProviderSdkUnavailable(
            "provider SDK package escapes the explicit root"
        ) from exc
    return package_root, direct_manifest


def _inspect_provider_sdk(
    compatibility_cell: PlatformCompatibility,
    sdk_root: str | os.PathLike[str],
    *,
    registry: BackendVersionRegistry,
) -> _ProviderSdkInspection:
    worker, provider, profile = _profile_for_cell(compatibility_cell, registry)
    root = _safe_absolute_directory(sdk_root, "provider SDK root")
    package_root, direct_manifest = _provider_package_root(root, profile)
    package_manifest = _read_json_object(
        package_root / "package.json", "provider SDK package.json"
    )
    if package_manifest.get("name") != profile.package_name:
        raise ProviderSdkUnavailable(
            "provider SDK package name does not match the protocol profile"
        )
    installed_version = package_manifest.get("version")
    if type(installed_version) is not str or not VERSION_RE.fullmatch(installed_version):
        raise ProviderSdkUnavailable(
            "provider SDK package version is not an exact semantic version"
        )
    declared = _dependency_version(direct_manifest or {}, profile.package_name)
    if declared not in (None, installed_version):
        raise ProviderSdkUnavailable(
            "provider SDK version drift: dependency must use the exact pinned version"
        )

    bin_value = package_manifest.get("bin")
    if type(bin_value) is str:
        bin_target = (
            bin_value
            if profile.bin_name == Path(profile.package_name).name
            else None
        )
    elif type(bin_value) is dict:
        bin_target = bin_value.get(profile.bin_name)
    else:
        bin_target = None
    if bin_target != profile.entrypoint:
        raise ProviderSdkUnavailable(
            "provider SDK executable entrypoint does not match the protocol profile"
        )
    if type(bin_target) is not str or "\x00" in bin_target:
        raise ProviderSdkUnavailable("provider SDK executable entrypoint is invalid")
    entrypoint_candidate = package_root / bin_target
    if not entrypoint_candidate.is_file() or entrypoint_candidate.is_symlink():
        raise ProviderSdkUnavailable(
            "provider SDK executable entrypoint is missing or is a link"
        )
    entrypoint = entrypoint_candidate.resolve(strict=True)
    try:
        entrypoint.relative_to(package_root)
    except ValueError as exc:
        raise ProviderSdkUnavailable(
            "provider SDK entrypoint escapes its package root"
        ) from exc

    lock_path = root / "package-lock.json"
    if not lock_path.is_file() and package_root.parent.parent.parent.exists():
        candidate = package_root.parent.parent.parent / "package-lock.json"
        if candidate.is_file():
            lock_path = candidate
    if not lock_path.is_file():
        raise ProviderSdkUnavailable(
            "provider SDK package-lock.json is required for integrity admission"
        )
    lock = _read_json_object(lock_path, "provider SDK package-lock.json")
    lock_entry = _package_lock_entry(
        lock,
        profile.package_name,
        package_root,
        lock_path.parent.resolve(strict=True),
    )
    if lock_entry is None:
        raise ProviderSdkUnavailable(
            "package-lock.json has no entry for the provider SDK"
        )
    if lock_entry.get("version") != installed_version:
        raise ProviderSdkUnavailable(
            "package-lock provider version does not match package.json"
        )
    observed_integrity = lock_entry.get("integrity")
    if (
        type(observed_integrity) is not str
        or not NPM_INTEGRITY_RE.fullmatch(observed_integrity)
    ):
        raise ProviderSdkUnavailable(
            "package-lock provider integrity is missing or invalid"
        )
    root_lock = lock.get("packages")
    if type(root_lock) is dict and type(root_lock.get("")) is dict:
        locked_declaration = _dependency_version(root_lock[""], profile.package_name)
        if locked_declaration not in (None, installed_version):
            raise ProviderSdkUnavailable(
                "package-lock root dependency is not exactly pinned"
            )

    record = registry.record(provider, compatibility_cell.platform, installed_version)
    status = BackendVersionProbeStatus.UNKNOWN
    reason = "backend version has no qualification record"
    if record is not None:
        if record.protocol_profile != profile.profile_id:
            status = BackendVersionProbeStatus.DRIFT
            reason = "backend protocol profile does not match its qualification record"
        elif record.launch_profile_digest != compatibility_cell.launch_profile_digest:
            status = BackendVersionProbeStatus.DRIFT
            reason = "backend launch profile does not match its qualification record"
        elif record.package_integrity != observed_integrity:
            status = BackendVersionProbeStatus.DRIFT
            reason = "backend package integrity does not match its qualification record"
        elif record.status is BackendVersionStatus.CANDIDATE:
            status = BackendVersionProbeStatus.CANDIDATE
            reason = "backend version is a candidate and cannot dispatch"
        elif record.status is BackendVersionStatus.QUARANTINED:
            status = BackendVersionProbeStatus.QUARANTINED
            reason = "backend version is quarantined"
        else:
            status = BackendVersionProbeStatus.QUALIFIED
            reason = "exact backend version and protocol profile are qualified"
    return _ProviderSdkInspection(
        worker,
        compatibility_cell.platform,
        profile,
        record,
        root,
        package_root,
        entrypoint,
        installed_version,
        observed_integrity,
        status,
        reason,
    )


def probe_provider_sdk(
    compatibility_cell: PlatformCompatibility,
    sdk_root: str | os.PathLike[str],
    *,
    registry: BackendVersionRegistry | None = None,
) -> BackendVersionProbeResult:
    """Inspect one exact local backend package without launching the provider."""

    if type(compatibility_cell) is not PlatformCompatibility:
        raise TypeError("compatibility_cell must be a PlatformCompatibility")
    if registry is not None and type(registry) is not BackendVersionRegistry:
        raise TypeError("registry must be a BackendVersionRegistry or null")
    selected_registry = registry or load_bundled_backend_version_registry()
    inspected = _inspect_provider_sdk(
        compatibility_cell,
        sdk_root,
        registry=selected_registry,
    )
    record = inspected.record
    return BackendVersionProbeResult(
        provider=inspected.provider,
        platform=inspected.platform,
        package_name=inspected.profile.package_name,
        backend_version=inspected.version,
        protocol_profile=inspected.profile.profile_id,
        protocol=inspected.profile.protocol,
        launch_profile_digest=compatibility_cell.launch_profile_digest,
        observed_integrity=inspected.integrity,
        status=inspected.status,
        enabled_for_dispatch=inspected.status is BackendVersionProbeStatus.QUALIFIED,
        max_concurrency=0 if record is None else record.max_concurrency,
        evidence_digest=None if record is None else record.evidence_digest,
        reason=inspected.reason,
    )


def resolve_provider_sdk(
    compatibility_cell: PlatformCompatibility,
    sdk_root: str | os.PathLike[str],
    *,
    sdk_pin: SdkPin | None = None,
    runtime_executable: str | os.PathLike[str] | None = None,
    registry: BackendVersionRegistry | None = None,
    requested_concurrency: int = 1,
) -> ProviderSdkResolution:
    """Verify and resolve one exact provider package without starting it.

    ``sdk_root`` is deliberately explicit.  It may be an npm project root
    containing ``node_modules`` or the package directory itself.  No registry
    lookup or ``@latest`` resolution is performed.
    """

    if type(compatibility_cell) is not PlatformCompatibility:
        raise TypeError("compatibility_cell must be a PlatformCompatibility")
    if registry is not None and type(registry) is not BackendVersionRegistry:
        raise TypeError("registry must be a BackendVersionRegistry or null")
    if type(requested_concurrency) is not int or isinstance(
        requested_concurrency, bool
    ) or requested_concurrency < 1:
        raise ValueError("requested_concurrency must be a positive integer")
    selected_registry = registry or load_bundled_backend_version_registry()
    if sdk_pin is not None:
        if type(sdk_pin) is not SdkPin:
            raise TypeError("sdk_pin must be an SdkPin or null")
        _, provider, profile = _profile_for_cell(
            compatibility_cell,
            selected_registry,
        )
        pinned_record = selected_registry.record(
            provider,
            compatibility_cell.platform,
            sdk_pin.version,
        )
        if pinned_record is None:
            raise ProviderSdkUnavailable(
                "provider SDK pin does not match the admitted M1 pin"
            )
        pinned_spec = _spec_from_record(profile, pinned_record)
        if (
            sdk_pin.name != pinned_spec.package_name
            or sdk_pin.version != pinned_spec.version
            or sdk_pin.shasum != pinned_spec.shasum
        ):
            raise ProviderSdkUnavailable(
                "provider SDK pin does not match the admitted M1 pin"
            )
    inspected = _inspect_provider_sdk(
        compatibility_cell,
        sdk_root,
        registry=selected_registry,
    )
    if inspected.status is not BackendVersionProbeStatus.QUALIFIED:
        raise ProviderSdkUnavailable(inspected.reason)
    record = inspected.record
    assert record is not None
    if requested_concurrency > record.max_concurrency:
        raise ProviderSdkUnavailable(
            "requested concurrency exceeds the exact backend version qualification"
        )
    provider = inspected.provider
    spec = _spec_from_record(inspected.profile, record)
    if sdk_pin is not None:
        if (
            sdk_pin.name != spec.package_name
            or sdk_pin.version != spec.version
            or sdk_pin.shasum != spec.shasum
        ):
            raise ProviderSdkUnavailable("provider SDK pin does not match the admitted M1 pin")

    runtime_name = spec.runtime
    runtime_raw = (
        os.fspath(runtime_executable)
        if runtime_executable is not None
        else shutil.which(runtime_name)
    )
    if not runtime_raw:
        raise ProviderSdkUnavailable(f"{runtime_name} runtime is not installed or not on PATH")
    runtime_candidate = Path(runtime_raw).expanduser()
    if not runtime_candidate.is_absolute() or not runtime_candidate.is_file():
        raise ProviderSdkUnavailable(f"{runtime_name} runtime must be an absolute executable file")
    if runtime_candidate.stem.casefold() != runtime_name:
        raise ProviderSdkUnavailable(
            f"provider SDK requires the exact {runtime_name} runtime"
        )
    runtime = runtime_candidate.resolve(strict=True)
    if not runtime.is_file():  # pragma: no cover - guarded by the strict resolve
        raise ProviderSdkUnavailable(f"{runtime_name} runtime target is not a file")
    return ProviderSdkResolution(
        provider,
        spec.package_name,
        spec.version,
        spec.shasum,
        spec.integrity,
        spec.protocol_profile,
        inspected.package_root,
        inspected.entrypoint,
        runtime,
    )


def _state_directory_for_attempt(state_root: Path, attempt: AttemptWorktree) -> Path:
    identity = attempt.identity
    task_id = identity.task_id
    attempt_number = identity.attempt
    correlation_id = identity.correlation_id
    if task_id is None or attempt_number is None or correlation_id is None:
        raise ProviderSdkUnavailable("attempt identity is incomplete")
    seed = "\0".join(
        (
            identity.run_id,
            str(identity.coordinator_epoch),
            task_id,
            str(attempt_number),
            correlation_id,
        )
    ).encode("utf-8", errors="strict")
    token = hashlib.sha256(seed).hexdigest()
    state = state_root / f"run-{hashlib.sha256(identity.run_id.encode('utf-8')).hexdigest()}" / (
        f"task-{hashlib.sha256(task_id.encode('utf-8')).hexdigest()}"
    ) / f"attempt-{attempt_number}-{token}"
    try:
        state.resolve(strict=False).relative_to(Path(attempt.path).resolve(strict=True))
    except (ValueError, OSError):
        return state
    raise ProviderSdkUnavailable("provider state directory must be outside the attempt worktree")


def _backend_capabilities(cell: PlatformCompatibility) -> BackendCapabilities:
    capabilities = cell.capabilities
    guarantees = tuple(guarantee for _, guarantee in capabilities.operations)
    provider = _provider_for_cell(cell)
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


class WishBuilderBackendAttemptChannelFactory:
    """Build one isolated provider channel after backend admission.

    This factory never changes the compatibility cell's enable bit.  A disabled
    cell, a rejected admission result, a missing SDK root, or any package drift
    all fail before a provider process can be started.
    """

    def __init__(
        self,
        *,
        compatibility_cell: PlatformCompatibility,
        provider_sdk_root: str | os.PathLike[str] | None = None,
        sdk_root: str | os.PathLike[str] | None = None,
        state_root: str | os.PathLike[str] | None = None,
        sdk_pin: SdkPin | None = None,
        runtime_executable: str | os.PathLike[str] | None = None,
        registry: BackendVersionRegistry | None = None,
        requested_concurrency: int = 1,
        channel_constructors: Mapping[
            WorkerProvider, Callable[[object], BackendChannelPort]
        ]
        | None = None,
    ) -> None:
        if type(compatibility_cell) is not PlatformCompatibility:
            raise TypeError("compatibility_cell must be a PlatformCompatibility")
        if provider_sdk_root is not None and sdk_root is not None:
            raise ValueError("provider_sdk_root and sdk_root are aliases; pass only one")
        selected_root = provider_sdk_root if provider_sdk_root is not None else sdk_root
        if state_root is not None:
            state_path = Path(state_root).expanduser()
            if not state_path.is_absolute():
                raise ValueError("state_root must be an absolute path")
            state_path = state_path.resolve(strict=False)
        else:
            state_path = None
        if channel_constructors is not None and not isinstance(
            channel_constructors, Mapping
        ):
            raise TypeError("channel_constructors must be a mapping or null")
        if registry is not None and type(registry) is not BackendVersionRegistry:
            raise TypeError("registry must be a BackendVersionRegistry or null")
        if type(requested_concurrency) is not int or isinstance(
            requested_concurrency, bool
        ) or requested_concurrency < 1:
            raise ValueError("requested_concurrency must be a positive integer")
        self._cell = compatibility_cell
        self._sdk_root = selected_root
        self._state_root = state_path
        self._sdk_pin = sdk_pin
        self._runtime_executable = runtime_executable
        self._registry = registry
        self._requested_concurrency = requested_concurrency
        self._constructors = dict(channel_constructors or {})
        self._lock = threading.RLock()

    def __call__(self, attempt: AttemptWorktree) -> BackendChannelPort:
        if type(attempt) is not AttemptWorktree:
            raise TypeError("attempt must be an AttemptWorktree")
        target = (
            f"{self._cell.capabilities.provider.value}/"
            f"{self._cell.platform.value}"
        )
        if self._sdk_root is None:
            raise ProviderSdkUnavailable(
                f"backend dispatch is unavailable for {target}: "
                "no Wish Builder provider adapter is installed; an explicit "
                "provider SDK root is required"
            )
        with self._lock:
            resolution = resolve_provider_sdk(
                self._cell,
                self._sdk_root,
                sdk_pin=self._sdk_pin,
                runtime_executable=self._runtime_executable,
                registry=self._registry,
                requested_concurrency=self._requested_concurrency,
            )
        state_root = self._state_root
        if state_root is None:
            raise ProviderSdkUnavailable("state_root is required for provider dispatch")
        state_directory = _state_directory_for_attempt(state_root, attempt)
        working_directory = Path(attempt.path).expanduser().resolve(strict=True)
        capabilities = _backend_capabilities(self._cell)
        constructor = self._constructors.get(resolution.provider)
        # The adapters merge this empty overlay onto the child environment.
        # Provider-owned credential stores remain in place; Wish Builder never
        # reads, logs, or copies their contents into attempt state.
        environment: tuple[tuple[str, str], ...] = ()
        if resolution.provider is WorkerProvider.CODEX:
            launch = CodexAppServerLaunch(
                command_prefix=(str(resolution.runtime), str(resolution.entrypoint)),
                sdk_version=resolution.package_version,
                sdk_shasum=resolution.package_shasum,
                sdk_integrity=resolution.package_integrity,
            )
            config = CodexAppServerConfig(
                capabilities=capabilities,
                launch=launch,
                working_directory=working_directory,
                state_directory=state_directory,
                environment=environment,
            )
            factory = CodexAppServerChannel if constructor is None else constructor
            channel = factory(config)
        else:
            protocol = (
                JsonlRpcProtocol.PI
                if resolution.provider is WorkerProvider.PI
                else JsonlRpcProtocol.OH_MY_PI_V2
            )
            launch = JsonlRpcLaunch(
                provider=resolution.provider,
                protocol=protocol,
                command_prefix=(str(resolution.runtime), str(resolution.entrypoint)),
                sdk_name=resolution.package_name,
                sdk_version=resolution.package_version,
            )
            config = JsonlRpcBackendConfig(
                capabilities=capabilities,
                launch=launch,
                working_directory=working_directory,
                state_directory=state_directory,
                environment=environment,
            )
            factory = JsonlRpcBackendChannel if constructor is None else constructor
            channel = factory(config)
        if not isinstance(channel, BackendChannelPort):
            raise TypeError("provider channel constructor must return a BackendChannelPort")
        if channel.probe() != capabilities:
            raise BackendDispatchUnavailable(
                "provider channel capabilities do not match the admitted cell"
            )
        return channel


@dataclass(frozen=True, slots=True, order=True)
class _AttemptKey:
    run_id: str
    coordinator_epoch: int
    task_id: str
    attempt: int

    @classmethod
    def from_identity(cls, identity: ExecutionIdentity) -> "_AttemptKey":
        if (
            type(identity) is not ExecutionIdentity
            or not identity.is_attempt
            or identity.task_id is None
            or identity.attempt is None
            or identity.correlation_id is None
        ):
            raise ValueError("identity must be a complete correlated attempt identity")
        return cls(
            identity.run_id,
            identity.coordinator_epoch,
            identity.task_id,
            identity.attempt,
        )


@dataclass(frozen=True, slots=True)
class AttemptOperationRoute:
    """A durable child operation identity used to rebuild recovery routing."""

    identity: ExecutionIdentity
    operation: EffectOperation
    request_payload_hash: str

    def __post_init__(self) -> None:
        _AttemptKey.from_identity(self.identity)
        if self.operation not in _EXTERNAL_EFFECT_OPERATIONS:
            raise ValueError("operation is not a supported external effect operation")
        if (
            type(self.request_payload_hash) is not str
            or not HASH_RE.fullmatch(self.request_payload_hash)
        ):
            raise ValueError("request_payload_hash must be a full sha256 reference")

    @classmethod
    def from_pending(
        cls,
        pending: PendingExternalEffect,
    ) -> "AttemptOperationRoute":
        if type(pending) is not PendingExternalEffect:
            raise TypeError("pending must be a PendingExternalEffect")
        return cls(
            pending.request_event.identity,
            pending.operation,
            pending.request_event.payload.request_payload_hash,
        )


@dataclass(frozen=True, slots=True)
class AttemptChannelRoute:
    """One prepared Git attempt and its immutable backend dispatch identity."""

    attempt: AttemptWorktree
    plan: BackendDispatchPlan
    recovery_operations: tuple[AttemptOperationRoute, ...] = ()

    def __post_init__(self) -> None:
        if type(self.attempt) is not AttemptWorktree:
            raise TypeError("attempt must be an AttemptWorktree")
        if type(self.plan) is not BackendDispatchPlan:
            raise TypeError("plan must be a BackendDispatchPlan")
        if type(self.recovery_operations) is not tuple or not all(
            type(item) is AttemptOperationRoute for item in self.recovery_operations
        ):
            raise TypeError(
                "recovery_operations must contain AttemptOperationRoute values"
            )
        if len(set(self.recovery_operations)) != len(self.recovery_operations):
            raise ValueError("recovery_operations must not contain duplicates")

        key = _AttemptKey.from_identity(self.attempt.identity)
        dispatch_id = self.attempt.identity.correlation_id
        assert dispatch_id is not None
        if (
            self.plan.reserve.dispatch_id != dispatch_id
            or self.plan.send.dispatch_id != dispatch_id
        ):
            raise ValueError("dispatch plan does not match the Git attempt identity")

        expected_ids = {
            EffectOperation.PREPARE_ATTEMPT: self.plan.reserve.attempt_id,
            EffectOperation.RESERVE_CHANNEL: self.plan.reserve.operation_id,
            EffectOperation.SEND_TASK_PACKET: self.plan.send.operation_id,
        }
        for recovered in self.recovery_operations:
            if _AttemptKey.from_identity(recovered.identity) != key:
                raise ValueError("recovery operation does not match the Git attempt")
            operation_id = recovered.identity.correlation_id
            assert operation_id is not None
            expected_id = expected_ids.get(recovered.operation)
            if expected_id is not None and operation_id != expected_id:
                raise ValueError(
                    "recovered planned operation does not match the plan"
                )
            expected_hash = {
                EffectOperation.RESERVE_CHANNEL: self.plan.reserve.canonical_sha256(),
                EffectOperation.SEND_TASK_PACKET: self.plan.send.canonical_sha256(),
            }.get(recovered.operation)
            if (
                expected_hash is not None
                and recovered.request_payload_hash != expected_hash
            ):
                raise ValueError(
                    "recovered reserve or send payload does not match the plan"
                )
            if (
                recovered.operation is EffectOperation.CANCEL_TURN
                and operation_id in expected_ids.values()
            ):
                raise ValueError("cancel operation collides with the dispatch plan")
            if recovered.operation in {
                EffectOperation.CHECK_ATTEMPT,
                EffectOperation.FINISH_ATTEMPT,
            } and operation_id in expected_ids.values():
                raise ValueError("lifecycle operation collides with the dispatch plan")


@dataclass(frozen=True, slots=True)
class _OperationBinding:
    attempt_key: _AttemptKey
    operation: EffectOperation
    request_payload_hash: str


@dataclass(frozen=True, slots=True)
class _LifecycleRouteIdentity:
    run_id: str
    task_id: str
    dispatch_id: str
    trellis_task_id: str
    parent_task_id: str
    manifest_digest: str
    trellis_graph_digest: str


def _lifecycle_route_identity(
    route: AttemptChannelRoute,
) -> _LifecycleRouteIdentity | None:
    """Read identity only from Wish Builder's canonical expanded task packet."""

    try:
        packet = json.loads(route.plan.send.task_packet)
        if type(packet) is not dict or packet.get("kind") != "wish_builder_task_packet":
            return None
        execution = packet["execution"]
        task = packet["task"]
        trellis = packet["trellis"]
        if not all(type(value) is dict for value in (execution, task, trellis)):
            return None
        packet_identity = execution["identity"]
        if type(packet_identity) is not dict:
            return None
        values = _LifecycleRouteIdentity(
            run_id=execution["run_id"],
            task_id=task["id"],
            dispatch_id=execution["dispatch_id"],
            trellis_task_id=task["trellis_task_id"],
            parent_task_id=trellis["parent_task_id"],
            manifest_digest=execution["manifest_digest"],
            trellis_graph_digest=trellis["graph_digest"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    identity = route.attempt.identity
    if (
        any(
            type(value) is not str or not value
            for value in (
                values.run_id,
                values.task_id,
                values.dispatch_id,
                values.trellis_task_id,
                values.parent_task_id,
                values.manifest_digest,
                values.trellis_graph_digest,
            )
        )
        or values.run_id != identity.run_id
        or values.task_id != identity.task_id
        or values.dispatch_id != identity.correlation_id
        or packet_identity != identity.to_primitive()
        or not HASH_RE.fullmatch(values.manifest_digest)
        or not HASH_RE.fullmatch(values.trellis_graph_digest)
    ):
        return None
    return values


class AttemptBackendChannelRouter:
    """Route each Channel effect to the exact prepared attempt worktree.

    ``expected_capabilities`` is the already-admitted compatibility contract.
    Every lazily constructed per-attempt port is probed once and must return the
    exact same value before it may receive an effect.
    """

    def __init__(
        self,
        routes: tuple[AttemptChannelRoute, ...],
        *,
        expected_capabilities: BackendCapabilities,
        channel_factory: AttemptChannelFactory,
        lifecycle_factory: AttemptLifecycleFactory | None = None,
        clock: RoutingClock = _utc_now,
    ) -> None:
        if type(routes) is not tuple or not all(
            type(route) is AttemptChannelRoute for route in routes
        ):
            raise TypeError("routes must contain AttemptChannelRoute values")
        if type(expected_capabilities) is not BackendCapabilities:
            raise TypeError("expected_capabilities must be BackendCapabilities")
        if not callable(channel_factory):
            raise TypeError("channel_factory must be callable")
        if lifecycle_factory is not None and not callable(lifecycle_factory):
            raise TypeError("lifecycle_factory must be callable or null")
        if not callable(clock):
            raise TypeError("clock must be callable")

        route_index: dict[_AttemptKey, AttemptChannelRoute] = {}
        lifecycle_index: dict[_AttemptKey, _LifecycleRouteIdentity | None] = {}
        operation_index: dict[str, _OperationBinding] = {}
        paths: set[str] = set()
        attempt_ids: set[str] = set()
        channel_ids: set[str] = set()
        message_ids: set[str] = set()
        turn_ids: set[str] = set()

        for route in routes:
            key = _AttemptKey.from_identity(route.attempt.identity)
            if key in route_index:
                raise ValueError("attempt route identity is duplicated")
            comparable_path = route.attempt.worktree_root.canonical_path
            if comparable_path in paths:
                raise ValueError("attempt worktree path is duplicated")
            paths.add(comparable_path)
            self._validate_plan_capabilities(route.plan, expected_capabilities)
            for value, seen, field_name in (
                (route.plan.reserve.attempt_id, attempt_ids, "attempt_id"),
                (route.plan.reserve.channel_id, channel_ids, "channel_id"),
                (route.plan.send.message_id, message_ids, "message_id"),
                (route.plan.send.turn_id, turn_ids, "turn_id"),
            ):
                if value in seen:
                    raise ValueError(f"dispatch plan {field_name} is duplicated")
                seen.add(value)
            route_index[key] = route
            lifecycle_index[key] = _lifecycle_route_identity(route)
            self._add_binding(
                operation_index,
                route.plan.reserve.operation_id,
                _OperationBinding(
                    key,
                    EffectOperation.RESERVE_CHANNEL,
                    route.plan.reserve.canonical_sha256(),
                ),
            )
            self._add_binding(
                operation_index,
                route.plan.send.operation_id,
                _OperationBinding(
                    key,
                    EffectOperation.SEND_TASK_PACKET,
                    route.plan.send.canonical_sha256(),
                ),
            )
            for recovered in route.recovery_operations:
                operation_id = recovered.identity.correlation_id
                assert operation_id is not None
                self._add_binding(
                    operation_index,
                    operation_id,
                    _OperationBinding(
                        key,
                        recovered.operation,
                        recovered.request_payload_hash,
                    ),
                )

        self._routes = route_index
        self._lifecycle_identities = lifecycle_index
        self._operations = operation_index
        self._expected_capabilities = expected_capabilities
        self._channel_factory = channel_factory
        self._lifecycle_factory = lifecycle_factory
        self._clock = clock
        self._channels: dict[_AttemptKey, BackendChannelPort] = {}
        self._lifecycles: dict[_AttemptKey, TrellisLifecyclePort] = {}
        self._lock = threading.RLock()

    def probe(self) -> BackendCapabilities:
        """Return the admitted router contract without selecting an attempt."""

        return self._expected_capabilities

    def reserve(
        self,
        effect: PreparedEffect[ReserveChannel],
    ) -> ChannelObservation:
        typed = self._require_effect(effect, ReserveChannel)
        route, reason = self._route_effect(
            typed,
            EffectOperation.RESERVE_CHANNEL,
        )
        if route is None:
            return self._unknown_channel(typed.operation_id, reason)
        try:
            observation = self._channel(route).reserve(typed)
        except Exception:
            return self._unknown_channel(
                typed.operation_id,
                "attempt channel adapter failed",
            )
        if not self._valid_observation(
            route,
            EffectOperation.RESERVE_CHANNEL,
            typed.operation_id,
            observation,
        ):
            return self._unknown_channel(
                typed.operation_id,
                "attempt channel observation mismatch",
            )
        assert type(observation) is ChannelObservation
        return observation

    def send(
        self,
        effect: PreparedEffect[SendTaskPacket],
    ) -> TurnObservation:
        typed = self._require_effect(effect, SendTaskPacket)
        route, reason = self._route_effect(
            typed,
            EffectOperation.SEND_TASK_PACKET,
        )
        if route is None:
            return self._unknown_turn(typed.operation_id, reason)
        try:
            observation = self._channel(route).send(typed)
        except Exception:
            return self._unknown_turn(
                typed.operation_id,
                "attempt channel adapter failed",
            )
        if not self._valid_observation(
            route,
            EffectOperation.SEND_TASK_PACKET,
            typed.operation_id,
            observation,
        ):
            return self._unknown_turn(
                typed.operation_id,
                "attempt channel observation mismatch",
            )
        assert type(observation) is TurnObservation
        return observation

    def cancel(
        self,
        effect: PreparedEffect[CancelTurn],
    ) -> TurnObservation:
        typed = self._require_effect(effect, CancelTurn)
        route, reason = self._route_effect(
            typed,
            EffectOperation.CANCEL_TURN,
        )
        if route is None:
            return self._unknown_turn(typed.operation_id, reason)
        try:
            observation = self._channel(route).cancel(typed)
        except Exception:
            return self._unknown_turn(
                typed.operation_id,
                "attempt channel adapter failed",
            )
        if not self._valid_observation(
            route,
            EffectOperation.CANCEL_TURN,
            typed.operation_id,
            observation,
        ):
            return self._unknown_turn(
                typed.operation_id,
                "attempt channel observation mismatch",
            )
        assert type(observation) is TurnObservation
        return observation

    def prepare_attempt(
        self,
        effect: PreparedEffect[PrepareAttempt],
    ) -> AttemptObservation:
        typed = self._require_effect(effect, PrepareAttempt)
        route, reason = self._route_lifecycle_effect(
            typed, EffectOperation.PREPARE_ATTEMPT
        )
        if route is None:
            return self._unknown_attempt(typed.operation_id, reason)
        try:
            lifecycle = self._lifecycle(route)
            observation = lifecycle.prepare_attempt(typed)
        except Exception:
            return self._unknown_attempt(
                typed.operation_id, "attempt lifecycle adapter failed"
            )
        if not self._valid_lifecycle_observation(
            route, EffectOperation.PREPARE_ATTEMPT, typed.operation_id, observation,
            command=typed.command,
        ):
            return self._unknown_attempt(
                typed.operation_id, "attempt lifecycle observation mismatch"
            )
        assert type(observation) is AttemptObservation
        return observation

    def check_attempt(
        self,
        effect: PreparedEffect[CheckAttempt],
    ) -> CheckObservation:
        typed = self._require_effect(effect, CheckAttempt)
        route, reason = self._route_lifecycle_effect(
            typed, EffectOperation.CHECK_ATTEMPT
        )
        if route is None:
            return self._unknown_check(typed.operation_id, reason)
        try:
            lifecycle = self._lifecycle(route)
            observation = lifecycle.check_attempt(typed)
        except Exception:
            return self._unknown_check(
                typed.operation_id, "attempt lifecycle adapter failed"
            )
        if not self._valid_lifecycle_observation(
            route, EffectOperation.CHECK_ATTEMPT, typed.operation_id, observation,
            command=typed.command,
        ):
            return self._unknown_check(
                typed.operation_id, "attempt lifecycle observation mismatch"
            )
        assert type(observation) is CheckObservation
        return observation

    def finish_attempt(
        self,
        effect: PreparedEffect[FinishAttempt],
    ) -> FinishObservation:
        typed = self._require_effect(effect, FinishAttempt)
        route, reason = self._route_lifecycle_effect(
            typed, EffectOperation.FINISH_ATTEMPT
        )
        if route is None:
            return self._unknown_finish(typed.operation_id, reason)
        try:
            lifecycle = self._lifecycle(route)
            observation = lifecycle.finish_attempt(typed)
        except Exception:
            return self._unknown_finish(
                typed.operation_id, "attempt lifecycle adapter failed"
            )
        if not self._valid_lifecycle_observation(
            route, EffectOperation.FINISH_ATTEMPT, typed.operation_id, observation,
            command=typed.command,
        ):
            return self._unknown_finish(
                typed.operation_id, "attempt lifecycle observation mismatch"
            )
        assert type(observation) is FinishObservation
        return observation

    def inspect_reservation(self, operation_id: str) -> ChannelObservation:
        normalized = self._operation_id(operation_id)
        route, reason = self._route_operation(
            normalized,
            EffectOperation.RESERVE_CHANNEL,
        )
        if route is None:
            return self._unknown_channel(normalized, reason)
        try:
            observation = self._channel(route).inspect_reservation(normalized)
        except Exception:
            return self._unknown_channel(normalized, "attempt channel adapter failed")
        if not self._valid_observation(
            route,
            EffectOperation.RESERVE_CHANNEL,
            normalized,
            observation,
        ):
            return self._unknown_channel(
                normalized,
                "attempt channel observation mismatch",
            )
        assert type(observation) is ChannelObservation
        return observation

    def inspect_turn(self, operation_id: str) -> TurnObservation:
        normalized = self._operation_id(operation_id)
        with self._lock:
            binding = self._operations.get(normalized)
        if binding is None or binding.operation not in {
            EffectOperation.SEND_TASK_PACKET,
            EffectOperation.CANCEL_TURN,
        }:
            return self._unknown_turn(normalized, "attempt operation route unknown")
        route, reason = self._route_operation(normalized, binding.operation)
        if route is None:
            return self._unknown_turn(normalized, reason)
        try:
            observation = self._channel(route).inspect_turn(normalized)
        except Exception:
            return self._unknown_turn(normalized, "attempt channel adapter failed")
        if not self._valid_observation(
            route,
            binding.operation,
            normalized,
            observation,
        ):
            return self._unknown_turn(
                normalized,
                "attempt channel observation mismatch",
            )
        assert type(observation) is TurnObservation
        return observation

    def inspect_attempt(self, operation_id: str) -> AttemptObservation:
        normalized = self._operation_id(operation_id)
        route, reason = self._route_operation(
            normalized, EffectOperation.PREPARE_ATTEMPT
        )
        if route is None:
            return self._unknown_attempt(normalized, reason)
        try:
            observation = self._lifecycle(route).inspect_attempt(
                normalized,
                expected_request_payload_hash=self._operation_hash(normalized),
            )
        except Exception:
            return self._unknown_attempt(normalized, "attempt lifecycle adapter failed")
        if not self._valid_lifecycle_observation(
            route, EffectOperation.PREPARE_ATTEMPT, normalized, observation
        ):
            return self._unknown_attempt(
                normalized, "attempt lifecycle observation mismatch"
            )
        assert type(observation) is AttemptObservation
        return observation

    def inspect_check(self, operation_id: str) -> CheckObservation:
        normalized = self._operation_id(operation_id)
        route, reason = self._route_operation(
            normalized, EffectOperation.CHECK_ATTEMPT
        )
        if route is None:
            return self._unknown_check(normalized, reason)
        try:
            observation = self._lifecycle(route).inspect_check(
                normalized,
                expected_request_payload_hash=self._operation_hash(normalized),
            )
        except Exception:
            return self._unknown_check(normalized, "attempt lifecycle adapter failed")
        if not self._valid_lifecycle_observation(
            route, EffectOperation.CHECK_ATTEMPT, normalized, observation
        ):
            return self._unknown_check(
                normalized, "attempt lifecycle observation mismatch"
            )
        assert type(observation) is CheckObservation
        return observation

    def inspect_finish(self, operation_id: str) -> FinishObservation:
        normalized = self._operation_id(operation_id)
        route, reason = self._route_operation(
            normalized, EffectOperation.FINISH_ATTEMPT
        )
        if route is None:
            return self._unknown_finish(normalized, reason)
        try:
            observation = self._lifecycle(route).inspect_finish(
                normalized,
                expected_request_payload_hash=self._operation_hash(normalized),
            )
        except Exception:
            return self._unknown_finish(normalized, "attempt lifecycle adapter failed")
        if not self._valid_lifecycle_observation(
            route, EffectOperation.FINISH_ATTEMPT, normalized, observation
        ):
            return self._unknown_finish(
                normalized, "attempt lifecycle observation mismatch"
            )
        assert type(observation) is FinishObservation
        return observation

    def _route_effect(
        self,
        effect: PreparedEffect[ReserveChannel]
        | PreparedEffect[SendTaskPacket]
        | PreparedEffect[CancelTurn],
        operation: EffectOperation,
    ) -> tuple[AttemptChannelRoute | None, str]:
        identity = effect.request.identity
        try:
            key = _AttemptKey.from_identity(identity)
        except ValueError:
            return None, "attempt effect identity incomplete"
        with self._lock:
            route = self._routes.get(key)
        if route is None:
            return None, "attempt identity route unknown"

        command = effect.command
        payload = effect.request.payload
        if (
            payload.adapter is not AdapterKind.BACKEND
            or payload.operation is not operation
            or payload.object_type is not _OBJECT_TYPES[operation]
            or identity.correlation_id != command.operation_id
        ):
            return None, "attempt effect boundary mismatch"

        valid_command = False
        if operation is EffectOperation.RESERVE_CHANNEL:
            valid_command = command == route.plan.reserve
        elif operation is EffectOperation.SEND_TASK_PACKET:
            valid_command = command == route.plan.send
        elif type(command) is CancelTurn:
            sent = route.plan.send
            valid_command = (
                command.operation_id
                not in {route.plan.reserve.operation_id, sent.operation_id}
                and command.attempt_id == sent.attempt_id
                and command.channel_id == sent.channel_id
                and command.turn_id == sent.turn_id
            )
        if not valid_command:
            return None, "attempt command does not match route"

        binding = _OperationBinding(key, operation, effect.command_hash)
        with self._lock:
            existing = self._operations.get(command.operation_id)
            if existing is not None and existing != binding:
                return None, "attempt operation route collision"
            self._operations[command.operation_id] = binding
        if not self._worktree_matches(route.attempt):
            return None, "attempt worktree identity mismatch"
        return route, ""

    def _route_lifecycle_effect(
        self,
        effect: PreparedEffect[PrepareAttempt]
        | PreparedEffect[CheckAttempt]
        | PreparedEffect[FinishAttempt],
        operation: EffectOperation,
    ) -> tuple[AttemptChannelRoute | None, str]:
        identity = effect.request.identity
        try:
            key = _AttemptKey.from_identity(identity)
        except ValueError:
            return None, "attempt effect identity incomplete"
        with self._lock:
            route = self._routes.get(key)
            route_identity = self._lifecycle_identities.get(key)
        if route is None:
            return None, "attempt identity route unknown"
        if route_identity is None:
            return None, "canonical task packet identity unavailable"

        command = effect.command
        payload = effect.request.payload
        if (
            payload.adapter is not AdapterKind.TRELLIS
            or payload.operation is not operation
            or payload.object_type is not EffectObjectType.ATTEMPT
            or identity.correlation_id != command.operation_id
            or command.task_id != key.task_id
            or command.trellis_task_id != route_identity.trellis_task_id
        ):
            return None, "attempt effect boundary mismatch"

        reserve = route.plan.reserve
        valid_command = False
        if type(command) is PrepareAttempt:
            valid_command = (
                command.operation_id == reserve.attempt_id
                and command.run_id == route_identity.run_id
                and command.parent_task_id == route_identity.parent_task_id
                and command.attempt == key.attempt
                and command.dispatch_id == route_identity.dispatch_id
                and command.manifest_digest == route_identity.manifest_digest
                and command.trellis_graph_digest
                == route_identity.trellis_graph_digest
                and command.expected_base_commit == route.attempt.base_commit_sha
            )
        elif type(command) is CheckAttempt:
            valid_command = (
                command.operation_id
                not in {
                    reserve.operation_id,
                    route.plan.send.operation_id,
                    reserve.attempt_id,
                }
                and command.attempt_id == reserve.attempt_id
                and command.task_packet_digest
                == route.plan.send.task_packet_digest
            )
        elif type(command) is FinishAttempt:
            valid_command = (
                command.operation_id
                not in {
                    reserve.operation_id,
                    route.plan.send.operation_id,
                    reserve.attempt_id,
                }
                and command.attempt_id == reserve.attempt_id
            )
        if not valid_command:
            return None, "attempt lifecycle command does not match route"

        binding = _OperationBinding(key, operation, effect.command_hash)
        with self._lock:
            existing = self._operations.get(command.operation_id)
            if existing is not None and existing != binding:
                return None, "attempt operation route collision"
            self._operations[command.operation_id] = binding
        if not self._worktree_matches(route.attempt):
            return None, "attempt worktree identity mismatch"
        return route, ""

    def _route_operation(
        self,
        operation_id: str,
        operation: EffectOperation,
    ) -> tuple[AttemptChannelRoute | None, str]:
        with self._lock:
            binding = self._operations.get(operation_id)
            route = None if binding is None else self._routes.get(binding.attempt_key)
        if binding is None or binding.operation is not operation or route is None:
            return None, "attempt operation route unknown"
        if not self._worktree_matches(route.attempt):
            return None, "attempt worktree identity mismatch"
        return route, ""

    def _operation_hash(self, operation_id: str) -> str:
        with self._lock:
            binding = self._operations.get(operation_id)
        if binding is None:
            raise LookupError("attempt operation route unknown")
        return binding.request_payload_hash

    def _channel(self, route: AttemptChannelRoute) -> BackendChannelPort:
        key = _AttemptKey.from_identity(route.attempt.identity)
        with self._lock:
            existing = self._channels.get(key)
            if existing is not None:
                return existing
            channel = self._channel_factory(route.attempt)
            if not isinstance(channel, BackendChannelPort):
                raise TypeError("channel_factory must return a BackendChannelPort")
            if channel.probe() != self._expected_capabilities:
                raise ValueError("attempt channel capabilities do not match admission")
            if any(existing is channel for existing in self._channels.values()):
                raise ValueError("attempt channel instance is shared across worktrees")
            self._channels[key] = channel
            return channel

    def _lifecycle(self, route: AttemptChannelRoute) -> TrellisLifecyclePort:
        factory = self._lifecycle_factory
        if factory is None:
            raise BackendDispatchUnavailable(
                "Trellis lifecycle dispatch is unavailable: no lifecycle adapter "
                "is installed"
            )
        key = _AttemptKey.from_identity(route.attempt.identity)
        with self._lock:
            existing = self._lifecycles.get(key)
            if existing is not None:
                return existing
            lifecycle = factory(route.attempt)
            if not isinstance(lifecycle, TrellisLifecyclePort):
                raise TypeError(
                    "lifecycle_factory must return a TrellisLifecyclePort"
                )
            if any(
                existing is lifecycle
                for existing in (*self._channels.values(), *self._lifecycles.values())
            ):
                raise ValueError(
                    "lifecycle adapter instance must be separate per attempt and "
                    "from backend channels"
                )
            self._lifecycles[key] = lifecycle
            return lifecycle

    @staticmethod
    def _worktree_matches(attempt: AttemptWorktree) -> bool:
        try:
            path = Path(attempt.path)
            git_dir = Path(attempt.git_dir.canonical_path)
            if (
                not path.is_absolute()
                or not path.is_dir()
                or not git_dir.is_absolute()
                or not git_dir.is_dir()
            ):
                return False
            return (
                capture_filesystem_identity(path) == attempt.worktree_root
                and capture_filesystem_identity(git_dir) == attempt.git_dir
            )
        except (GitIdentityError, OSError, ValueError):
            return False

    @staticmethod
    def _valid_observation(
        route: AttemptChannelRoute,
        operation: EffectOperation,
        operation_id: str,
        observation: object,
    ) -> bool:
        expected_id = {
            EffectOperation.RESERVE_CHANNEL: route.plan.reserve.operation_id,
            EffectOperation.SEND_TASK_PACKET: route.plan.send.operation_id,
        }.get(operation)
        if operation is EffectOperation.CANCEL_TURN:
            expected_type = TurnObservation
        else:
            expected_type = (
                ChannelObservation
                if operation is EffectOperation.RESERVE_CHANNEL
                else TurnObservation
            )
        if type(observation) is not expected_type:
            return False
        if observation.operation_id != operation_id:
            return False
        if expected_id is not None and operation_id != expected_id:
            return False
        if observation.status is not EffectStatus.APPLIED:
            return True
        if type(observation) is ChannelObservation:
            reserve = route.plan.reserve
            return (
                observation.attempt_id == reserve.attempt_id
                and observation.channel_id == reserve.channel_id
                and observation.provider is reserve.provider
            )
        send = route.plan.send
        return (
            observation.attempt_id == send.attempt_id
            and observation.channel_id == send.channel_id
            and observation.message_id == send.message_id
            and observation.turn_id == send.turn_id
        )

    def _valid_lifecycle_observation(
        self,
        route: AttemptChannelRoute,
        operation: EffectOperation,
        operation_id: str,
        observation: object,
        *,
        command: PrepareAttempt | CheckAttempt | FinishAttempt | None = None,
    ) -> bool:
        expected_type = {
            EffectOperation.PREPARE_ATTEMPT: AttemptObservation,
            EffectOperation.CHECK_ATTEMPT: CheckObservation,
            EffectOperation.FINISH_ATTEMPT: FinishObservation,
        }[operation]
        if type(observation) is not expected_type or observation.operation_id != operation_id:
            return False
        if observation.status is not EffectStatus.APPLIED:
            return True
        expected_attempt_id = route.plan.reserve.attempt_id
        if type(observation) is AttemptObservation:
            key = _AttemptKey.from_identity(route.attempt.identity)
            route_identity = self._lifecycle_identities.get(key)
            if route_identity is None:
                return False
            return (
                observation.lifecycle_state is TrellisLifecycleState.PREPARED
                and observation.attempt_id == expected_attempt_id
                and observation.trellis_task_id == route_identity.trellis_task_id
                and observation.worktree_path is not None
                and os.path.normcase(os.path.abspath(observation.worktree_path))
                == os.path.normcase(os.path.abspath(route.attempt.path))
                and observation.base_commit == route.attempt.base_commit_sha
            )
        if observation.attempt_id != expected_attempt_id:
            return False
        if type(observation) is CheckObservation and observation.passed is not True:
            return False
        if type(observation) is FinishObservation and observation.finished is not True:
            return False
        if type(command) is CheckAttempt:
            assert type(observation) is CheckObservation
            return observation.head_commit == command.expected_head_commit
        if type(command) is FinishAttempt:
            assert type(observation) is FinishObservation
            return observation.delivered_commit == command.delivered_commit
        return True

    @staticmethod
    def _validate_plan_capabilities(
        plan: BackendDispatchPlan,
        capabilities: BackendCapabilities,
    ) -> None:
        reserve = plan.reserve
        if (
            reserve.provider is not capabilities.provider
            or reserve.capability_digest != capabilities.capability_digest
            or reserve.launch_profile_digest != capabilities.launch_profile_digest
            or reserve.policy_digest != capabilities.policy_digest
            or len(plan.send.task_packet.encode("utf-8"))
            > capabilities.max_task_packet_bytes
        ):
            raise ValueError("dispatch plan does not match admitted capabilities")

    @staticmethod
    def _add_binding(
        index: dict[str, _OperationBinding],
        operation_id: str,
        binding: _OperationBinding,
    ) -> None:
        existing = index.get(operation_id)
        if existing is not None and existing != binding:
            raise ValueError(
                "external effect operation route is duplicated or inconsistent"
            )
        index[operation_id] = binding

    @staticmethod
    def _require_effect(
        effect: object,
        command_type: (
            type[PrepareAttempt]
            | type[ReserveChannel]
            | type[SendTaskPacket]
            | type[CancelTurn]
            | type[CheckAttempt]
            | type[FinishAttempt]
        ),
    ) -> (
        PreparedEffect[PrepareAttempt]
        | PreparedEffect[ReserveChannel]
        | PreparedEffect[SendTaskPacket]
        | PreparedEffect[CancelTurn]
        | PreparedEffect[CheckAttempt]
        | PreparedEffect[FinishAttempt]
    ):
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        if type(effect.command) is not command_type:
            raise TypeError(f"effect command must be {command_type.__name__}")
        _ = effect.operation_id
        _ = effect.command_hash
        return effect

    @staticmethod
    def _operation_id(value: object) -> str:
        if type(value) is not str:
            raise TypeError("operation_id must be a string")
        if not value:
            raise ValueError("operation_id must not be empty")
        return value

    def _unknown_channel(self, operation_id: str, reason: str) -> ChannelObservation:
        return ChannelObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            evidence=(reason,),
        )

    def _unknown_turn(self, operation_id: str, reason: str) -> TurnObservation:
        return TurnObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            state=TurnState.UNKNOWN,
            evidence=(reason,),
        )

    def _unknown_attempt(self, operation_id: str, reason: str) -> AttemptObservation:
        return AttemptObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            lifecycle_state=TrellisLifecycleState.UNKNOWN,
            evidence=(reason,),
        )

    def _unknown_check(self, operation_id: str, reason: str) -> CheckObservation:
        return CheckObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            evidence=(reason,),
        )

    def _unknown_finish(self, operation_id: str, reason: str) -> FinishObservation:
        return FinishObservation(
            operation_id=operation_id,
            status=EffectStatus.UNKNOWN,
            observed_at=self._clock(),
            evidence=(reason,),
        )


__all__ = [
    "AttemptChannelFactory",
    "AttemptLifecycleFactory",
    "AttemptChannelRoute",
    "AttemptOperationRoute",
    "AttemptBackendChannelRouter",
    "BackendDispatchUnavailable",
    "BackendVersionProbeResult",
    "BackendVersionProbeStatus",
    "ProviderSdkResolution",
    "ProviderSdkUnavailable",
    "RoutingClock",
    "probe_provider_sdk",
    "resolve_provider_sdk",
    "WishBuilderBackendAttemptChannelFactory",
]
