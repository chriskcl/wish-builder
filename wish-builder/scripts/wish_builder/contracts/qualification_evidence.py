"""Raw, content-addressed evidence contracts for backend qualification.

These contracts describe observations produced by a qualification harness.  They do
not grant dispatch admission and deliberately do not contain a self-reported
``passed`` flag.  A separate verifier must derive qualification claims from the
closed event stream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import ClassVar
from urllib.parse import urlsplit

from .compatibility import Platform, Provider
from .models import HASH_RE, TIMESTAMP_RE, _nonempty
from .serialization import MAX_CANONICAL_INTEGER, canonical_json_bytes, canonical_sha256


QUALIFICATION_EVIDENCE_SCHEMA_VERSION = 1
QUALIFICATION_EVENT_SCHEMA_VERSION = QUALIFICATION_EVIDENCE_SCHEMA_VERSION
QUALIFICATION_INVENTORY_SCHEMA_VERSION = QUALIFICATION_EVIDENCE_SCHEMA_VERSION
QUALIFICATION_HARNESS_SCHEMA_VERSION = QUALIFICATION_EVIDENCE_SCHEMA_VERSION
QUALIFICATION_PROVENANCE_SCHEMA_VERSION = QUALIFICATION_EVIDENCE_SCHEMA_VERSION

MAX_QUALIFICATION_EVENTS = 4_096
MAX_QUALIFICATION_ID_LENGTH = 256
MAX_QUALIFICATION_PATH_LENGTH = 1_024
MAX_QUALIFICATION_OWNED_PATHS = 256
MAX_QUALIFICATION_ARGUMENTS = 64
MAX_QUALIFICATION_MEDIA_TYPE_LENGTH = 128
MAX_QUALIFICATION_ARTIFACT_BYTES = MAX_CANONICAL_INTEGER
MAX_QUALIFICATION_TASK_PACKET_LENGTH = 1_048_576

ZERO_SHA256 = "sha256:" + "0" * 64
QUALIFICATION_EVENT_GENESIS_DIGEST = ZERO_SHA256
QUALIFICATION_EVENT_DIGEST_PLACEHOLDER = ZERO_SHA256

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$")
_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SEMVER_RE = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?$"
)
_MEDIA_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)


class QualificationEvidenceScenario(StrEnum):
    FULL_TURN = "full_turn"
    ACTIVE_TURN_CANCELLATION = "active_turn_cancellation"
    CRASH_RECONCILE = "crash_reconcile"
    CLEANUP = "cleanup"
    SIBLING_OVERLAP = "sibling_overlap"


class QualificationEventSource(StrEnum):
    RUNNER = "runner"
    WISH_BUILDER = "wish_builder"
    PROVIDER = "provider"


class QualificationEventType(StrEnum):
    RUN_STARTED = "run_started"
    PREPARE_REQUESTED = "prepare_requested"
    ATTEMPT_PREPARED = "attempt_prepared"
    RESERVE_REQUESTED = "reserve_requested"
    CHANNEL_RESERVED = "channel_reserved"
    SEND_REQUESTED = "send_requested"
    TASK_PACKET_SENT = "task_packet_sent"
    TURN_STARTED = "turn_started"
    TURN_TERMINAL = "turn_terminal"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_OBSERVED = "cancel_observed"
    CRASH_INJECTED = "crash_injected"
    PROCESS_RESTARTED = "process_restarted"
    RECONCILE_REQUESTED = "reconcile_requested"
    RECONCILE_INSPECTED = "reconcile_inspected"
    CLEANUP_REQUESTED = "cleanup_requested"
    CLEANUP_OBSERVED = "cleanup_observed"
    RUN_FINISHED = "run_finished"


class QualificationTurnTerminalState(StrEnum):
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QualificationTurnState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QualificationEffectStatus(StrEnum):
    ABSENT = "absent"
    APPLIED = "applied"
    UNKNOWN = "unknown"


class QualificationRunOutcome(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"


class QualificationEvidenceRole(StrEnum):
    EVENT_LOG = "event_log"
    HARNESS_DESCRIPTOR = "harness_descriptor"
    EXECUTION_MANIFEST = "execution_manifest"
    TRELLIS_SNAPSHOT = "trellis_snapshot"
    PROVENANCE = "provenance"


class QualificationProvenanceKind(StrEnum):
    GITHUB_ACTIONS = "github_actions"
    PROVIDER = "provider"


QUALIFICATION_SCENARIO_ORDER = tuple(QualificationEvidenceScenario)
QUALIFICATION_EVIDENCE_ROLE_ORDER = tuple(QualificationEvidenceRole)
QUALIFICATION_PROVENANCE_SUBJECT_ROLE_ORDER = (
    QualificationEvidenceRole.EVENT_LOG,
    QualificationEvidenceRole.HARNESS_DESCRIPTOR,
    QualificationEvidenceRole.EXECUTION_MANIFEST,
    QualificationEvidenceRole.TRELLIS_SNAPSHOT,
)


def _token(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, MAX_QUALIFICATION_ID_LENGTH)
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a stable qualification token")
    return normalized


def _digest(value: object, field_name: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return value


def _optional_digest(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _digest(value, field_name)


def _revision(value: object, field_name: str) -> str:
    if type(value) is not str or not _REVISION_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full lowercase source revision")
    return value


def _sha1(value: object, field_name: str) -> str:
    if type(value) is not str or not _SHA1_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full lowercase SHA-1 digest")
    return value


def _semver(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, 128)
    if not _SEMVER_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an exact semantic version")
    return normalized


def _timestamp(value: object, field_name: str) -> str:
    if type(value) is not str or not TIMESTAMP_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UTC timestamp") from exc
    return value


def _relative_path(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, MAX_QUALIFICATION_PATH_LENGTH)
    if (
        "\\" in normalized
        or normalized.startswith("/")
        or "//" in normalized
        or normalized.endswith("/")
    ):
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field_name} must not traverse outside its evidence root")
    if path.as_posix() != normalized:
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")
    return normalized


def _owned_paths(value: object, field_name: str = "owned_paths") -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > MAX_QUALIFICATION_OWNED_PATHS:
        raise ValueError(f"{field_name} exceeds the path limit")
    normalized = tuple(
        _relative_path(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _resource_ids(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if not value and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > MAX_QUALIFICATION_OWNED_PATHS:
        raise ValueError(f"{field_name} exceeds the resource limit")
    normalized = tuple(
        _token(item, f"{field_name}[{index}]") for index, item in enumerate(value)
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(normalized))


def _https_url(value: object, field_name: str) -> str:
    normalized = _nonempty(value, field_name, MAX_QUALIFICATION_PATH_LENGTH)
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{field_name} must be an absolute HTTPS URL")
    return normalized


def _positive_integer(value: object, field_name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_CANONICAL_INTEGER:
        raise ValueError(f"{field_name} must be a positive signed 64-bit integer")
    return value


def _nonnegative_integer(value: object, field_name: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CANONICAL_INTEGER:
        raise ValueError(f"{field_name} must be a non-negative signed 64-bit integer")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{field_name} must be a bool")
    return value


def _enum(value: object, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must be a {enum_type.__name__}")
    return value


def _normalize_tokens(instance: object, *field_names: str) -> None:
    for field_name in field_names:
        object.__setattr__(
            instance,
            field_name,
            _token(getattr(instance, field_name), field_name),
        )


def _normalize_digests(instance: object, *field_names: str) -> None:
    for field_name in field_names:
        object.__setattr__(
            instance,
            field_name,
            _digest(getattr(instance, field_name), field_name),
        )


class QualificationEventPayload:
    """Marker for the closed event-type/payload union."""

    EVENT_TYPE: ClassVar[QualificationEventType]
    JSON_FIELDS: ClassVar[tuple[tuple[str, str], ...]]

    def to_primitive(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for attribute, json_name in self.JSON_FIELDS:
            value = getattr(self, attribute)
            if isinstance(value, StrEnum):
                result[json_name] = value.value
            elif type(value) is tuple:
                result[json_name] = list(value)
            else:
                result[json_name] = value
        return result

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())


@dataclass(frozen=True, slots=True)
class RunStartedPayload(QualificationEventPayload):
    source_revision: str
    harness_digest: str
    harness_version: str
    trellis_version: str
    trellis_compatibility_digest: str
    policy_digest: str
    launch_profile_digest: str
    capability_digest: str
    manifest_digest: str
    trellis_snapshot_digest: str
    sdk_name: str
    sdk_version: str
    sdk_shasum: str

    EVENT_TYPE = QualificationEventType.RUN_STARTED
    JSON_FIELDS = (
        ("source_revision", "sourceRevision"),
        ("harness_digest", "harnessDigest"),
        ("harness_version", "harnessVersion"),
        ("trellis_version", "trellisVersion"),
        ("trellis_compatibility_digest", "trellisCompatibilityDigest"),
        ("policy_digest", "policyDigest"),
        ("launch_profile_digest", "launchProfileDigest"),
        ("capability_digest", "capabilityDigest"),
        ("manifest_digest", "manifestDigest"),
        ("trellis_snapshot_digest", "trellisSnapshotDigest"),
        ("sdk_name", "sdkName"),
        ("sdk_version", "sdkVersion"),
        ("sdk_shasum", "sdkShasum"),
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_revision", _revision(self.source_revision, "source_revision"))
        _normalize_digests(
            self,
            "harness_digest",
            "trellis_compatibility_digest",
            "policy_digest",
            "launch_profile_digest",
            "capability_digest",
            "manifest_digest",
            "trellis_snapshot_digest",
        )
        object.__setattr__(self, "harness_version", _semver(self.harness_version, "harness_version"))
        object.__setattr__(self, "trellis_version", _semver(self.trellis_version, "trellis_version"))
        object.__setattr__(self, "sdk_name", _nonempty(self.sdk_name, "sdk_name", 128))
        object.__setattr__(self, "sdk_version", _semver(self.sdk_version, "sdk_version"))
        object.__setattr__(self, "sdk_shasum", _sha1(self.sdk_shasum, "sdk_shasum"))


@dataclass(frozen=True, slots=True)
class PrepareRequestedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    trellis_task_id: str
    worktree_id: str
    base_commit: str
    owned_paths: tuple[str, ...]

    EVENT_TYPE = QualificationEventType.PREPARE_REQUESTED
    JSON_FIELDS = (
        ("operation_id", "operationId"),
        ("dispatch_id", "dispatchId"),
        ("attempt_id", "attemptId"),
        ("task_id", "taskId"),
        ("trellis_task_id", "trellisTaskId"),
        ("worktree_id", "worktreeId"),
        ("base_commit", "baseCommit"),
        ("owned_paths", "ownedPaths"),
    )

    def __post_init__(self) -> None:
        _normalize_tokens(
            self,
            "operation_id",
            "dispatch_id",
            "attempt_id",
            "task_id",
            "trellis_task_id",
            "worktree_id",
        )
        object.__setattr__(self, "base_commit", _revision(self.base_commit, "base_commit"))
        object.__setattr__(self, "owned_paths", _owned_paths(self.owned_paths))


@dataclass(frozen=True, slots=True)
class AttemptPreparedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    trellis_task_id: str
    worktree_id: str
    base_commit: str
    owned_paths: tuple[str, ...]

    EVENT_TYPE = QualificationEventType.ATTEMPT_PREPARED
    JSON_FIELDS = PrepareRequestedPayload.JSON_FIELDS

    def __post_init__(self) -> None:
        PrepareRequestedPayload.__post_init__(self)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReserveRequestedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str

    EVENT_TYPE = QualificationEventType.RESERVE_REQUESTED
    JSON_FIELDS = (
        ("operation_id", "operationId"),
        ("dispatch_id", "dispatchId"),
        ("attempt_id", "attemptId"),
        ("task_id", "taskId"),
        ("channel_id", "channelId"),
    )

    def __post_init__(self) -> None:
        _normalize_tokens(self, "operation_id", "dispatch_id", "attempt_id", "task_id", "channel_id")


@dataclass(frozen=True, slots=True)
class ChannelReservedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str

    EVENT_TYPE = QualificationEventType.CHANNEL_RESERVED
    JSON_FIELDS = ReserveRequestedPayload.JSON_FIELDS + (("provider_session_id", "providerSessionId"),)

    def __post_init__(self) -> None:
        _normalize_tokens(
            self,
            "operation_id",
            "dispatch_id",
            "attempt_id",
            "task_id",
            "channel_id",
            "provider_session_id",
        )


@dataclass(frozen=True, slots=True)
class SendRequestedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    task_packet: str
    task_packet_digest: str

    EVENT_TYPE = QualificationEventType.SEND_REQUESTED
    JSON_FIELDS = ReserveRequestedPayload.JSON_FIELDS + (
        ("task_packet", "taskPacket"),
        ("task_packet_digest", "taskPacketDigest"),
    )

    def __post_init__(self) -> None:
        _normalize_tokens(self, "operation_id", "dispatch_id", "attempt_id", "task_id", "channel_id")
        object.__setattr__(
            self,
            "task_packet",
            _nonempty(
                self.task_packet,
                "task_packet",
                MAX_QUALIFICATION_TASK_PACKET_LENGTH,
            ),
        )
        _normalize_digests(self, "task_packet_digest")


@dataclass(frozen=True, slots=True)
class TaskPacketSentPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    provider_message_id: str
    task_packet: str
    task_packet_digest: str

    EVENT_TYPE = QualificationEventType.TASK_PACKET_SENT
    JSON_FIELDS = (
        ("operation_id", "operationId"),
        ("dispatch_id", "dispatchId"),
        ("attempt_id", "attemptId"),
        ("task_id", "taskId"),
        ("channel_id", "channelId"),
        ("provider_session_id", "providerSessionId"),
        ("provider_message_id", "providerMessageId"),
        ("task_packet", "taskPacket"),
        ("task_packet_digest", "taskPacketDigest"),
    )

    def __post_init__(self) -> None:
        _normalize_tokens(
            self,
            "operation_id",
            "dispatch_id",
            "attempt_id",
            "task_id",
            "channel_id",
            "provider_session_id",
            "provider_message_id",
        )
        object.__setattr__(
            self,
            "task_packet",
            _nonempty(
                self.task_packet,
                "task_packet",
                MAX_QUALIFICATION_TASK_PACKET_LENGTH,
            ),
        )
        _normalize_digests(self, "task_packet_digest")


_TURN_JSON_FIELDS = (
    ("dispatch_id", "dispatchId"),
    ("attempt_id", "attemptId"),
    ("task_id", "taskId"),
    ("channel_id", "channelId"),
    ("provider_session_id", "providerSessionId"),
    ("provider_message_id", "providerMessageId"),
    ("provider_turn_id", "providerTurnId"),
)


def _normalize_turn_identity(instance: object) -> None:
    _normalize_tokens(
        instance,
        "dispatch_id",
        "attempt_id",
        "task_id",
        "channel_id",
        "provider_session_id",
        "provider_message_id",
        "provider_turn_id",
    )


@dataclass(frozen=True, slots=True)
class TurnStartedPayload(QualificationEventPayload):
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    provider_message_id: str
    provider_turn_id: str

    EVENT_TYPE = QualificationEventType.TURN_STARTED
    JSON_FIELDS = _TURN_JSON_FIELDS

    def __post_init__(self) -> None:
        _normalize_turn_identity(self)


@dataclass(frozen=True, slots=True)
class TurnTerminalPayload(QualificationEventPayload):
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    provider_message_id: str
    provider_turn_id: str
    terminal_state: QualificationTurnTerminalState
    result_digest: str | None

    EVENT_TYPE = QualificationEventType.TURN_TERMINAL
    JSON_FIELDS = _TURN_JSON_FIELDS + (
        ("terminal_state", "terminalState"),
        ("result_digest", "resultDigest"),
    )

    def __post_init__(self) -> None:
        _normalize_turn_identity(self)
        object.__setattr__(
            self,
            "terminal_state",
            _enum(self.terminal_state, QualificationTurnTerminalState, "terminal_state"),
        )
        object.__setattr__(self, "result_digest", _optional_digest(self.result_digest, "result_digest"))
        if self.terminal_state is QualificationTurnTerminalState.DONE and self.result_digest is None:
            raise ValueError("a completed turn must bind a result_digest")


@dataclass(frozen=True, slots=True)
class CancelRequestedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    provider_message_id: str
    provider_turn_id: str

    EVENT_TYPE = QualificationEventType.CANCEL_REQUESTED
    JSON_FIELDS = (("operation_id", "operationId"),) + _TURN_JSON_FIELDS

    def __post_init__(self) -> None:
        _normalize_tokens(self, "operation_id")
        _normalize_turn_identity(self)


@dataclass(frozen=True, slots=True)
class CancelObservedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    provider_message_id: str
    provider_turn_id: str
    effect_status: QualificationEffectStatus

    EVENT_TYPE = QualificationEventType.CANCEL_OBSERVED
    JSON_FIELDS = CancelRequestedPayload.JSON_FIELDS + (("effect_status", "effectStatus"),)

    def __post_init__(self) -> None:
        _normalize_tokens(self, "operation_id")
        _normalize_turn_identity(self)
        object.__setattr__(
            self,
            "effect_status",
            _enum(self.effect_status, QualificationEffectStatus, "effect_status"),
        )


@dataclass(frozen=True, slots=True)
class CrashInjectedPayload(QualificationEventPayload):
    failpoint: str
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    provider_message_id: str
    provider_turn_id: str

    EVENT_TYPE = QualificationEventType.CRASH_INJECTED
    JSON_FIELDS = (("failpoint", "failpoint"), ("operation_id", "operationId")) + _TURN_JSON_FIELDS

    def __post_init__(self) -> None:
        _normalize_tokens(self, "failpoint", "operation_id")
        _normalize_turn_identity(self)


@dataclass(frozen=True, slots=True)
class ProcessRestartedPayload(QualificationEventPayload):
    previous_process_identity: str
    recovery_id: str

    EVENT_TYPE = QualificationEventType.PROCESS_RESTARTED
    JSON_FIELDS = (
        ("previous_process_identity", "previousProcessIdentity"),
        ("recovery_id", "recoveryId"),
    )

    def __post_init__(self) -> None:
        _normalize_tokens(self, "previous_process_identity", "recovery_id")


@dataclass(frozen=True, slots=True)
class ReconcileRequestedPayload(QualificationEventPayload):
    operation_id: str
    request_digest: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    provider_message_id: str
    provider_turn_id: str

    EVENT_TYPE = QualificationEventType.RECONCILE_REQUESTED
    JSON_FIELDS = (
        ("operation_id", "operationId"),
        ("request_digest", "requestDigest"),
    ) + _TURN_JSON_FIELDS

    def __post_init__(self) -> None:
        _normalize_tokens(self, "operation_id")
        _normalize_digests(self, "request_digest")
        _normalize_turn_identity(self)


@dataclass(frozen=True, slots=True)
class ReconcileInspectedPayload(QualificationEventPayload):
    operation_id: str
    request_digest: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    provider_message_id: str
    provider_turn_id: str
    effect_status: QualificationEffectStatus
    turn_state: QualificationTurnState
    result_digest: str | None

    EVENT_TYPE = QualificationEventType.RECONCILE_INSPECTED
    JSON_FIELDS = ReconcileRequestedPayload.JSON_FIELDS + (
        ("effect_status", "effectStatus"),
        ("turn_state", "turnState"),
        ("result_digest", "resultDigest"),
    )

    def __post_init__(self) -> None:
        _normalize_tokens(self, "operation_id")
        _normalize_digests(self, "request_digest")
        _normalize_turn_identity(self)
        object.__setattr__(
            self,
            "effect_status",
            _enum(self.effect_status, QualificationEffectStatus, "effect_status"),
        )
        object.__setattr__(
            self,
            "turn_state",
            _enum(self.turn_state, QualificationTurnState, "turn_state"),
        )
        object.__setattr__(self, "result_digest", _optional_digest(self.result_digest, "result_digest"))


@dataclass(frozen=True, slots=True)
class CleanupRequestedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    worktree_id: str
    process_tree_ids: tuple[str, ...]

    EVENT_TYPE = QualificationEventType.CLEANUP_REQUESTED
    JSON_FIELDS = (
        ("operation_id", "operationId"),
        ("dispatch_id", "dispatchId"),
        ("attempt_id", "attemptId"),
        ("task_id", "taskId"),
        ("channel_id", "channelId"),
        ("provider_session_id", "providerSessionId"),
        ("worktree_id", "worktreeId"),
        ("process_tree_ids", "processTreeIds"),
    )

    def __post_init__(self) -> None:
        _normalize_tokens(
            self,
            "operation_id",
            "dispatch_id",
            "attempt_id",
            "task_id",
            "channel_id",
            "provider_session_id",
            "worktree_id",
        )
        object.__setattr__(
            self,
            "process_tree_ids",
            _resource_ids(self.process_tree_ids, "process_tree_ids"),
        )


@dataclass(frozen=True, slots=True)
class CleanupObservedPayload(QualificationEventPayload):
    operation_id: str
    dispatch_id: str
    attempt_id: str
    task_id: str
    channel_id: str
    provider_session_id: str
    worktree_id: str
    process_tree_ids: tuple[str, ...]
    resources_before: tuple[str, ...]
    resources_after: tuple[str, ...]

    EVENT_TYPE = QualificationEventType.CLEANUP_OBSERVED
    JSON_FIELDS = CleanupRequestedPayload.JSON_FIELDS + (
        ("resources_before", "resourcesBefore"),
        ("resources_after", "resourcesAfter"),
    )

    def __post_init__(self) -> None:
        CleanupRequestedPayload.__post_init__(self)  # type: ignore[arg-type]
        object.__setattr__(
            self,
            "resources_before",
            _resource_ids(self.resources_before, "resources_before"),
        )
        object.__setattr__(
            self,
            "resources_after",
            _resource_ids(self.resources_after, "resources_after"),
        )


@dataclass(frozen=True, slots=True)
class RunFinishedPayload(QualificationEventPayload):
    outcome: QualificationRunOutcome

    EVENT_TYPE = QualificationEventType.RUN_FINISHED
    JSON_FIELDS = (("outcome", "outcome"),)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcome", _enum(self.outcome, QualificationRunOutcome, "outcome"))


QUALIFICATION_EVENT_PAYLOAD_TYPES: dict[
    QualificationEventType, type[QualificationEventPayload]
] = {
    payload_type.EVENT_TYPE: payload_type
    for payload_type in (
        RunStartedPayload,
        PrepareRequestedPayload,
        AttemptPreparedPayload,
        ReserveRequestedPayload,
        ChannelReservedPayload,
        SendRequestedPayload,
        TaskPacketSentPayload,
        TurnStartedPayload,
        TurnTerminalPayload,
        CancelRequestedPayload,
        CancelObservedPayload,
        CrashInjectedPayload,
        ProcessRestartedPayload,
        ReconcileRequestedPayload,
        ReconcileInspectedPayload,
        CleanupRequestedPayload,
        CleanupObservedPayload,
        RunFinishedPayload,
    )
}


_QUALIFICATION_EVENT_FIELDS = frozenset(
    {
        "schemaVersion",
        "sequence",
        "qualificationRunId",
        "scenario",
        "provider",
        "platform",
        "source",
        "eventType",
        "recordedAt",
        "monotonicNs",
        "hostBootId",
        "processIdentity",
        "payload",
        "previousEventDigest",
        "eventDigest",
    }
)


def qualification_event_digest(value: object) -> str:
    """Hash an event with ``eventDigest`` replaced by the all-zero placeholder.

    The replacement makes the self-digest deterministic.  ``previousEventDigest``
    remains in the hashed body and therefore extends the event chain.
    """

    if type(value) is QualificationEvent:
        primitive = value.to_primitive()
    elif type(value) is dict:
        primitive = dict(value)
    else:
        raise TypeError("value must be a QualificationEvent or event primitive")
    if set(primitive) != _QUALIFICATION_EVENT_FIELDS:
        raise ValueError("event primitive must contain the exact event field set")
    primitive["eventDigest"] = QUALIFICATION_EVENT_DIGEST_PLACEHOLDER
    return "sha256:" + canonical_sha256(primitive)


@dataclass(frozen=True, slots=True)
class QualificationEvent:
    schema_version: int
    sequence: int
    qualification_run_id: str
    scenario: QualificationEvidenceScenario
    provider: Provider
    platform: Platform
    source: QualificationEventSource
    event_type: QualificationEventType
    recorded_at: str
    monotonic_ns: int
    host_boot_id: str
    process_identity: str
    payload: QualificationEventPayload
    previous_event_digest: str
    event_digest: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != QUALIFICATION_EVENT_SCHEMA_VERSION:
            raise ValueError("qualification event schema_version must be 1")
        object.__setattr__(self, "sequence", _positive_integer(self.sequence, "sequence"))
        object.__setattr__(self, "qualification_run_id", _token(self.qualification_run_id, "qualification_run_id"))
        object.__setattr__(self, "scenario", _enum(self.scenario, QualificationEvidenceScenario, "scenario"))
        object.__setattr__(self, "provider", _enum(self.provider, Provider, "provider"))
        object.__setattr__(self, "platform", _enum(self.platform, Platform, "platform"))
        object.__setattr__(self, "source", _enum(self.source, QualificationEventSource, "source"))
        object.__setattr__(self, "event_type", _enum(self.event_type, QualificationEventType, "event_type"))
        object.__setattr__(self, "recorded_at", _timestamp(self.recorded_at, "recorded_at"))
        object.__setattr__(self, "monotonic_ns", _nonnegative_integer(self.monotonic_ns, "monotonic_ns"))
        object.__setattr__(self, "host_boot_id", _token(self.host_boot_id, "host_boot_id"))
        object.__setattr__(self, "process_identity", _token(self.process_identity, "process_identity"))
        expected_payload_type = QUALIFICATION_EVENT_PAYLOAD_TYPES[self.event_type]
        if type(self.payload) is not expected_payload_type:
            raise TypeError(
                f"{self.event_type.value} payload must be {expected_payload_type.__name__}"
            )
        object.__setattr__(
            self,
            "previous_event_digest",
            _digest(self.previous_event_digest, "previous_event_digest"),
        )
        object.__setattr__(self, "event_digest", _digest(self.event_digest, "event_digest"))
        if self.sequence == 1 and self.previous_event_digest != QUALIFICATION_EVENT_GENESIS_DIGEST:
            raise ValueError("the first event must reference the genesis digest")
        if self.event_digest != qualification_event_digest(self):
            raise ValueError("event_digest does not match the canonical event body")

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        qualification_run_id: str,
        scenario: QualificationEvidenceScenario,
        provider: Provider,
        platform: Platform,
        source: QualificationEventSource,
        event_type: QualificationEventType,
        recorded_at: str,
        monotonic_ns: int,
        host_boot_id: str,
        process_identity: str,
        payload: QualificationEventPayload,
        previous_event_digest: str,
    ) -> "QualificationEvent":
        primitive: dict[str, object] = {
            "schemaVersion": QUALIFICATION_EVENT_SCHEMA_VERSION,
            "sequence": sequence,
            "qualificationRunId": qualification_run_id,
            "scenario": scenario.value,
            "provider": provider.value,
            "platform": platform.value,
            "source": source.value,
            "eventType": event_type.value,
            "recordedAt": recorded_at,
            "monotonicNs": monotonic_ns,
            "hostBootId": host_boot_id,
            "processIdentity": process_identity,
            "payload": payload.to_primitive(),
            "previousEventDigest": previous_event_digest,
            "eventDigest": QUALIFICATION_EVENT_DIGEST_PLACEHOLDER,
        }
        return cls(
            schema_version=QUALIFICATION_EVENT_SCHEMA_VERSION,
            sequence=sequence,
            qualification_run_id=qualification_run_id,
            scenario=scenario,
            provider=provider,
            platform=platform,
            source=source,
            event_type=event_type,
            recorded_at=recorded_at,
            monotonic_ns=monotonic_ns,
            host_boot_id=host_boot_id,
            process_identity=process_identity,
            payload=payload,
            previous_event_digest=previous_event_digest,
            event_digest=qualification_event_digest(primitive),
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "sequence": self.sequence,
            "qualificationRunId": self.qualification_run_id,
            "scenario": self.scenario.value,
            "provider": self.provider.value,
            "platform": self.platform.value,
            "source": self.source.value,
            "eventType": self.event_type.value,
            "recordedAt": self.recorded_at,
            "monotonicNs": self.monotonic_ns,
            "hostBootId": self.host_boot_id,
            "processIdentity": self.process_identity,
            "payload": self.payload.to_primitive(),
            "previousEventDigest": self.previous_event_digest,
            "eventDigest": self.event_digest,
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())


def qualification_event_log_bytes(events: tuple[QualificationEvent, ...]) -> bytes:
    if type(events) is not tuple or not events:
        raise ValueError("events must be a non-empty tuple")
    if len(events) > MAX_QUALIFICATION_EVENTS:
        raise ValueError("events exceeds the qualification event limit")
    expected_previous = QUALIFICATION_EVENT_GENESIS_DIGEST
    for expected_sequence, event in enumerate(events, start=1):
        if type(event) is not QualificationEvent:
            raise TypeError("events must contain only QualificationEvent values")
        if event.sequence != expected_sequence or event.previous_event_digest != expected_previous:
            raise ValueError("events must form one contiguous digest chain")
        expected_previous = event.event_digest
    return b"".join(event.canonical_json_bytes() for event in events)


@dataclass(frozen=True, slots=True)
class QualificationEvidenceArtifact:
    role: QualificationEvidenceRole
    path: str
    digest: str
    byte_length: int
    media_type: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _enum(self.role, QualificationEvidenceRole, "role"))
        object.__setattr__(self, "path", _relative_path(self.path, "path"))
        object.__setattr__(self, "digest", _digest(self.digest, "digest"))
        byte_length = _positive_integer(self.byte_length, "byte_length")
        if byte_length > MAX_QUALIFICATION_ARTIFACT_BYTES:
            raise ValueError("byte_length exceeds the artifact size limit")
        object.__setattr__(self, "byte_length", byte_length)
        media_type = _nonempty(self.media_type, "media_type", MAX_QUALIFICATION_MEDIA_TYPE_LENGTH)
        if not _MEDIA_TYPE_RE.fullmatch(media_type):
            raise ValueError("media_type must be a bare Internet media type")
        object.__setattr__(self, "media_type", media_type.lower())

    def to_primitive(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "path": self.path,
            "digest": self.digest,
            "byteLength": self.byte_length,
            "mediaType": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class QualificationEvidenceInventory:
    schema_version: int
    qualification_run_id: str
    provider: Provider
    platform: Platform
    artifacts: tuple[QualificationEvidenceArtifact, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != QUALIFICATION_INVENTORY_SCHEMA_VERSION:
            raise ValueError("qualification inventory schema_version must be 1")
        object.__setattr__(self, "qualification_run_id", _token(self.qualification_run_id, "qualification_run_id"))
        object.__setattr__(self, "provider", _enum(self.provider, Provider, "provider"))
        object.__setattr__(self, "platform", _enum(self.platform, Platform, "platform"))
        if type(self.artifacts) is not tuple or not all(
            type(item) is QualificationEvidenceArtifact for item in self.artifacts
        ):
            raise TypeError("artifacts must contain QualificationEvidenceArtifact values")
        if tuple(item.role for item in self.artifacts) != QUALIFICATION_EVIDENCE_ROLE_ORDER:
            raise ValueError("artifacts must contain the exact canonical evidence role set")
        paths = tuple(item.path for item in self.artifacts)
        if len(set(paths)) != len(paths):
            raise ValueError("artifact paths must be unique")

    def artifact(self, role: QualificationEvidenceRole) -> QualificationEvidenceArtifact:
        if not isinstance(role, QualificationEvidenceRole):
            raise TypeError("role must be a QualificationEvidenceRole")
        return self.artifacts[QUALIFICATION_EVIDENCE_ROLE_ORDER.index(role)]

    def to_primitive(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "qualificationRunId": self.qualification_run_id,
            "provider": self.provider.value,
            "platform": self.platform.value,
            "artifacts": [item.to_primitive() for item in self.artifacts],
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    def digest(self) -> str:
        return "sha256:" + canonical_sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class QualificationHarnessDescriptor:
    schema_version: int
    harness_version: str
    source_revision: str
    entrypoint: str
    event_schema_version: int
    scenarios: tuple[QualificationEvidenceScenario, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != QUALIFICATION_HARNESS_SCHEMA_VERSION:
            raise ValueError("qualification harness schema_version must be 1")
        object.__setattr__(self, "harness_version", _semver(self.harness_version, "harness_version"))
        object.__setattr__(self, "source_revision", _revision(self.source_revision, "source_revision"))
        object.__setattr__(self, "entrypoint", _relative_path(self.entrypoint, "entrypoint"))
        if type(self.event_schema_version) is not int or self.event_schema_version != QUALIFICATION_EVENT_SCHEMA_VERSION:
            raise ValueError("event_schema_version must identify qualification events v1")
        if type(self.scenarios) is not tuple or not all(
            isinstance(item, QualificationEvidenceScenario) for item in self.scenarios
        ):
            raise TypeError("scenarios must contain QualificationEvidenceScenario values")
        if self.scenarios != QUALIFICATION_SCENARIO_ORDER:
            raise ValueError("scenarios must contain the exact canonical scenario set")

    def to_primitive(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "harnessVersion": self.harness_version,
            "sourceRevision": self.source_revision,
            "entrypoint": self.entrypoint,
            "eventSchemaVersion": self.event_schema_version,
            "scenarios": [item.value for item in self.scenarios],
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    def digest(self) -> str:
        return "sha256:" + canonical_sha256(self.to_primitive())


@dataclass(frozen=True, slots=True)
class QualificationProvenanceSubject:
    role: QualificationEvidenceRole
    path: str
    digest: str
    byte_length: int
    media_type: str

    def __post_init__(self) -> None:
        artifact = QualificationEvidenceArtifact(
            role=self.role,
            path=self.path,
            digest=self.digest,
            byte_length=self.byte_length,
            media_type=self.media_type,
        )
        if artifact.role is QualificationEvidenceRole.PROVENANCE:
            raise ValueError("provenance cannot attest itself")
        object.__setattr__(self, "role", artifact.role)
        object.__setattr__(self, "path", artifact.path)
        object.__setattr__(self, "digest", artifact.digest)
        object.__setattr__(self, "byte_length", artifact.byte_length)
        object.__setattr__(self, "media_type", artifact.media_type)

    @classmethod
    def from_artifact(cls, artifact: QualificationEvidenceArtifact) -> "QualificationProvenanceSubject":
        if type(artifact) is not QualificationEvidenceArtifact:
            raise TypeError("artifact must be a QualificationEvidenceArtifact")
        return cls(
            role=artifact.role,
            path=artifact.path,
            digest=artifact.digest,
            byte_length=artifact.byte_length,
            media_type=artifact.media_type,
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "path": self.path,
            "digest": self.digest,
            "byteLength": self.byte_length,
            "mediaType": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class QualificationProvenance:
    schema_version: int
    kind: QualificationProvenanceKind
    issuer: str
    reference: str
    identity: str
    source_revision: str
    subjects: tuple[QualificationProvenanceSubject, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != QUALIFICATION_PROVENANCE_SCHEMA_VERSION:
            raise ValueError("qualification provenance schema_version must be 1")
        object.__setattr__(self, "kind", _enum(self.kind, QualificationProvenanceKind, "kind"))
        object.__setattr__(self, "issuer", _https_url(self.issuer, "issuer"))
        object.__setattr__(self, "reference", _https_url(self.reference, "reference"))
        object.__setattr__(self, "identity", _nonempty(self.identity, "identity", MAX_QUALIFICATION_PATH_LENGTH))
        object.__setattr__(self, "source_revision", _revision(self.source_revision, "source_revision"))
        if type(self.subjects) is not tuple or not all(
            type(item) is QualificationProvenanceSubject for item in self.subjects
        ):
            raise TypeError("subjects must contain QualificationProvenanceSubject values")
        if tuple(item.role for item in self.subjects) != QUALIFICATION_PROVENANCE_SUBJECT_ROLE_ORDER:
            raise ValueError("subjects must bind the exact four non-provenance roles")

    def binds_inventory(self, inventory: QualificationEvidenceInventory) -> bool:
        if type(inventory) is not QualificationEvidenceInventory:
            raise TypeError("inventory must be a QualificationEvidenceInventory")
        expected = tuple(
            QualificationProvenanceSubject.from_artifact(inventory.artifact(role))
            for role in QUALIFICATION_PROVENANCE_SUBJECT_ROLE_ORDER
        )
        return self.subjects == expected

    def to_primitive(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "kind": self.kind.value,
            "issuer": self.issuer,
            "reference": self.reference,
            "identity": self.identity,
            "sourceRevision": self.source_revision,
            "subjects": [item.to_primitive() for item in self.subjects],
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    def digest(self) -> str:
        return "sha256:" + canonical_sha256(self.to_primitive())


__all__ = [
    "AttemptPreparedPayload",
    "CancelObservedPayload",
    "CancelRequestedPayload",
    "ChannelReservedPayload",
    "CleanupObservedPayload",
    "CleanupRequestedPayload",
    "CrashInjectedPayload",
    "MAX_QUALIFICATION_EVENTS",
    "MAX_QUALIFICATION_TASK_PACKET_LENGTH",
    "ProcessRestartedPayload",
    "QUALIFICATION_EVIDENCE_ROLE_ORDER",
    "QUALIFICATION_EVIDENCE_SCHEMA_VERSION",
    "QUALIFICATION_EVENT_DIGEST_PLACEHOLDER",
    "QUALIFICATION_EVENT_GENESIS_DIGEST",
    "QUALIFICATION_EVENT_PAYLOAD_TYPES",
    "QUALIFICATION_EVENT_SCHEMA_VERSION",
    "QUALIFICATION_HARNESS_SCHEMA_VERSION",
    "QUALIFICATION_INVENTORY_SCHEMA_VERSION",
    "QUALIFICATION_PROVENANCE_SCHEMA_VERSION",
    "QUALIFICATION_PROVENANCE_SUBJECT_ROLE_ORDER",
    "QUALIFICATION_SCENARIO_ORDER",
    "PrepareRequestedPayload",
    "QualificationEffectStatus",
    "QualificationEvent",
    "QualificationEventPayload",
    "QualificationEventSource",
    "QualificationEventType",
    "QualificationEvidenceArtifact",
    "QualificationEvidenceInventory",
    "QualificationEvidenceRole",
    "QualificationEvidenceScenario",
    "QualificationHarnessDescriptor",
    "QualificationProvenance",
    "QualificationProvenanceKind",
    "QualificationProvenanceSubject",
    "QualificationRunOutcome",
    "QualificationTurnState",
    "QualificationTurnTerminalState",
    "ReconcileInspectedPayload",
    "ReconcileRequestedPayload",
    "ReserveRequestedPayload",
    "RunFinishedPayload",
    "RunStartedPayload",
    "SendRequestedPayload",
    "TaskPacketSentPayload",
    "TurnStartedPayload",
    "TurnTerminalPayload",
    "qualification_event_digest",
    "qualification_event_log_bytes",
]
