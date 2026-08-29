"""Closed runtime contracts shared by the active-M1 kernel and adapters.

The execution journal is the authority after Gate B.  Values crossing that
boundary are immutable, versioned, bounded, and canonically serializable.  In
particular, journal payloads and adapter outcomes never carry an untyped
``dict`` supplied by a caller.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypeAlias

from .manifest_v2 import (
    MAX_LEASE_TTL_SECONDS,
    MIN_LEASE_TTL_SECONDS,
    SchedulerMode,
    validate_lease_timing,
)
from .models import HASH_RE, ID_RE, _has_disallowed_contract_control
from .serialization import MAX_CANONICAL_INTEGER, canonical_json_bytes, canonical_sha256

RUNTIME_SCHEMA_VERSION = 1
JOURNAL_EVENT_VERSION = "1.0"
MAX_RUNTIME_ID_LENGTH = 64
MAX_RUNTIME_TOKEN_LENGTH = 128
MAX_RUNTIME_TEXT_LENGTH = 512
MAX_EVIDENCE_REFS = 64
MAX_DECISION_OPTIONS = 16
MAX_AFFECTED_IDENTITIES = 256

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_MESSAGE_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def _normalized_string(value: object, field_name: str, limit: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    if not normalized or not normalized.strip():
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > limit:
        raise ValueError(f"{field_name} exceeds the string limit")
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must contain valid Unicode") from exc
    if _has_disallowed_contract_control(normalized):
        raise ValueError(f"{field_name} contains a disallowed contract control")
    return normalized


def _stable_id(value: object, field_name: str) -> str:
    normalized = _normalized_string(value, field_name, MAX_RUNTIME_ID_LENGTH)
    if not ID_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} is not a stable uppercase ID")
    return normalized


def _token(value: object, field_name: str) -> str:
    normalized = _normalized_string(value, field_name, MAX_RUNTIME_TOKEN_LENGTH)
    if not _TOKEN_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} is not a stable token")
    return normalized


def _message_key(value: object) -> str:
    normalized = _normalized_string(value, "user_message_key", MAX_RUNTIME_TOKEN_LENGTH)
    if not _MESSAGE_KEY_RE.fullmatch(normalized):
        raise ValueError("user_message_key is not a stable message key")
    return normalized


def _hash(value: object, field_name: str) -> str:
    if type(value) is not str or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return value


def _optional_hash(value: object, field_name: str) -> str | None:
    return None if value is None else _hash(value, field_name)


def _timestamp(value: object, field_name: str) -> str:
    normalized = _normalized_string(value, field_name, 32)
    if not _TIMESTAMP_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a UTC timestamp")
    try:
        datetime.fromisoformat(normalized[:-1])
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid timestamp") from exc
    return normalized


def _timestamp_value(value: str) -> datetime:
    """Return one validated UTC timestamp as an aware datetime."""

    normalized = _timestamp(value, "timestamp")
    return datetime.fromisoformat(normalized)


def _format_utc_timestamp(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("authority time must be a timezone-aware datetime")
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _nonnegative(value: object, field_name: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CANONICAL_INTEGER:
        raise ValueError(f"{field_name} must be a non-negative signed 64-bit integer")
    return value


def _positive(value: object, field_name: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_CANONICAL_INTEGER:
        raise ValueError(f"{field_name} must be a positive signed 64-bit integer")
    return value


def _schema_version(value: object) -> int:
    if type(value) is not int or value != RUNTIME_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {RUNTIME_SCHEMA_VERSION}")
    return value


def _contract_tuple(
    value: object,
    expected_type: type[object],
    field_name: str,
    *,
    max_items: int,
    nonempty: bool = False,
) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if nonempty and not value:
        raise ValueError(f"{field_name} must not be empty")
    if len(value) > max_items:
        raise ValueError(f"{field_name} exceeds the item limit")
    if not all(type(item) is expected_type for item in value):
        raise TypeError(f"{field_name} contains an invalid value")
    return value


class CanonicalRuntimeContract:
    """Canonical byte helpers shared by every top-level runtime contract."""

    __slots__ = ()

    def to_primitive(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_primitive())

    def canonical_sha256(self) -> str:
        return "sha256:" + canonical_sha256(self.to_primitive())


class ActorType(StrEnum):
    HUMAN = "human"
    COORDINATOR = "coordinator"
    WORKER = "worker"
    ADAPTER = "adapter"
    SYSTEM = "system"


class SourceChannel(StrEnum):
    DIRECT_CLI = "direct_cli"
    CODEX_RELAY = "codex_relay"
    COORDINATOR = "coordinator"
    RECOVERY = "recovery"


class CommandKind(StrEnum):
    DECIDE = "decide"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RECONCILE = "reconcile"
    RETRY = "retry"
    CLEANUP = "cleanup"


class DecisionType(StrEnum):
    GATE_A = "gate_a"
    GATE_B = "gate_b"
    RESUME = "resume"
    RETRY = "retry"
    CLEANUP = "cleanup"


class DecisionChoice(StrEnum):
    APPROVE = "approve"
    REVISE = "revise"
    REJECT = "reject"
    CONTINUE = "continue"
    ABORT = "abort"


class DecisionAdmissionReason(StrEnum):
    ACCEPTED = "accepted"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    REQUEST_MISMATCH = "request_mismatch"
    STALE_SEQUENCE = "stale_sequence"
    STALE_CANDIDATE = "stale_candidate"
    STALE_NONCE = "stale_nonce"
    ACTOR_MISMATCH = "actor_mismatch"
    CHANNEL_DENIED = "channel_denied"
    WORKSPACE_DRIFT = "workspace_drift"
    INVALID_CHOICE = "invalid_choice"
    DECISION_CONFLICT = "decision_conflict"
    PERSISTENCE_FAILED = "persistence_failed"
    MATERIAL_DRIFT = "material_drift"


class EvidenceType(StrEnum):
    CONTRACT = "contract"
    JOURNAL_EVENT = "journal_event"
    PROCESS = "process"
    GIT = "git"
    RESULT = "result"
    EFFECT_RECEIPT = "effect_receipt"
    DIAGNOSTIC = "diagnostic"
    CHECKPOINT = "checkpoint"


class EvidenceSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"


class EvidenceRenderPolicy(StrEnum):
    TEXT = "text"
    DOWNLOAD = "download"
    METADATA_ONLY = "metadata_only"
    NEVER = "never"


class EvidenceRole(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class OutcomeKind(StrEnum):
    SUCCESS = "success"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    TERMINAL = "terminal"


class RuntimeReasonCode(StrEnum):
    CONTROL_INIT_FAILED = "control_init_failed"
    CONTROL_STATE_MISSING = "control_state_missing"
    CONTROL_ROOT_DRIFT = "control_root_drift"
    WORKSPACE_DRIFT = "workspace_drift"
    COORDINATOR_CRASH = "coordinator_crash"
    COORDINATOR_CRASH_LOOP = "coordinator_crash_loop"
    PERSISTENCE_FAILED = "persistence_failed"
    PROTOCOL_INVALID = "protocol_invalid"
    INVARIANT_VIOLATION = "invariant_violation"
    SETUP_REQUIRED = "setup_required"
    VERSION_UNSUPPORTED = "version_unsupported"
    DISCOVERY_INCOMPLETE = "discovery_incomplete"
    TRELLIS_GRAPH_INCOMPLETE = "trellis_graph_incomplete"
    # Legacy replay value. New writes must use TRELLIS_GRAPH_INCOMPLETE.
    DECOMPOSITION_INCOMPLETE = "decomposition_incomplete"
    VALIDATION_FAILED = "validation_failed"
    DEPENDENCY_NOT_READY = "dependency_not_ready"
    OWNERSHIP_CONFLICT = "ownership_conflict"
    LEASE_CONFLICT = "lease_conflict"
    LEASE_LOST = "lease_lost"
    STALE_FENCING_TOKEN = "stale_fencing_token"
    HEAD_DRIFT = "head_drift"
    MERGE_CANDIDATE_STALE = "merge_candidate_stale"
    EXTERNAL_TIMEOUT = "external_timeout"
    EXTERNAL_OUTCOME_UNKNOWN = "external_outcome_unknown"
    WORKER_OUTCOME_UNKNOWN = "worker_outcome_unknown"
    RATE_LIMITED = "rate_limited"
    AUTH_FAILED = "auth_failed"
    POLICY_DENIED = "policy_denied"
    PAUSE_REQUESTED = "pause_requested"
    CONTROL_REQUEST_SUPERSEDED = "control_request_superseded"
    MODEL_OUTPUT_EMPTY = "model_output_empty"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    MODEL_REFUSED = "model_refused"
    MODEL_CONTEXT_LIMIT = "model_context_limit"
    MODEL_POLICY_DENIED = "model_policy_denied"
    MODEL_AUTH_FAILED = "model_auth_failed"
    MODEL_RATE_LIMITED = "model_rate_limited"
    MODEL_TRANSPORT_UNKNOWN = "model_transport_unknown"
    GIT_STATE_CONFLICT = "git_state_conflict"
    JOURNAL_CORRUPT = "journal_corrupt"
    REGISTRY_CORRUPT = "registry_corrupt"
    SNAPSHOT_VERIFICATION_FAILED = "snapshot_verification_failed"
    STORAGE_EXHAUSTED = "storage_exhausted"
    CLOCK_ANOMALY = "clock_anomaly"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_TOO_LARGE = "evidence_too_large"
    EVIDENCE_INVALID = "evidence_invalid"
    LIMIT_EXCEEDED = "limit_exceeded"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"
    PROCESS_START_FAILED = "process_start_failed"
    PROCESS_CONTAINMENT_UNKNOWN = "process_containment_unknown"
    CHECK_FAILED = "check_failed"
    REVERTED = "reverted"
    RETRY_EXHAUSTED = "retry_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"
    CAPABILITY_STALE = "capability_stale"
    LEAF_INCOMPLETE = "leaf_incomplete"
    INTEGRATION_FAILED = "integration_failed"
    QUALITY_DOCS_FAILED = "quality_docs_failed"
    CANCELLED_BY_USER = "cancelled_by_user"
    GATE_INVALIDATED = "gate_invalidated"


class BudgetDimension(StrEnum):
    EFFORT_UNITS = "effort_units"
    WORKER_SECONDS = "worker_seconds"
    ATTEMPTS = "attempts"
    OUTPUT_BYTES = "output_bytes"


class BudgetDisposition(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"


class EffectStatus(StrEnum):
    ABSENT = "absent"
    APPLIED = "applied"
    UNKNOWN = "unknown"


class EffectOperation(StrEnum):
    TASK_EXECUTION = "task_execution"
    MODEL_INFERENCE = "model_inference"
    WORKER_DISPATCH = "worker_dispatch"
    PREPARE_ATTEMPT = "prepare_attempt"
    RESERVE_CHANNEL = "reserve_channel"
    SEND_TASK_PACKET = "send_task_packet"
    CANCEL_TURN = "cancel_turn"
    CHECK_ATTEMPT = "check_attempt"
    FINISH_ATTEMPT = "finish_attempt"
    REPOSITORY_UPDATE = "repository_update"
    RESULT_STAGE = "result_stage"
    RESULT_PROMOTION = "result_promotion"
    PROCESS_TERMINATION = "process_termination"
    CLEANUP = "cleanup"


class AdapterKind(StrEnum):
    TASK = "task"
    MODEL = "model"
    BACKEND = "backend"
    TRELLIS = "trellis"
    REPOSITORY = "repository"
    GIT = "git"
    PROCESS = "process"
    STORAGE = "storage"


class EffectObjectType(StrEnum):
    WORKER = "worker"
    ATTEMPT = "attempt"
    CHANNEL = "channel"
    TASK_PACKET = "task_packet"
    TURN = "turn"
    PROCESS = "process"
    GIT_REF = "git_ref"
    RESULT_BUNDLE = "result_bundle"
    WORKTREE = "worktree"
    CHECKPOINT = "checkpoint"
    CLEANUP_ITEM = "cleanup_item"


class TransitionSubject(StrEnum):
    RUN = "run"
    TASK = "task"
    ATTEMPT = "attempt"


class RuntimeState(StrEnum):
    NONE = "none"
    PREFLIGHT = "preflight"
    DISCOVERY = "discovery"
    GATE_A_PENDING = "gate_a_pending"
    TRELLIS_PREPARATION = "trellis_preparation"
    # Legacy replay value. New writes must use TRELLIS_PREPARATION.
    DECOMPOSITION = "decomposition"
    GATE_B_PENDING = "gate_b_pending"
    EXECUTING = "executing"
    INTEGRATION = "integration"
    QUALITY_DOCS = "quality_docs"
    COMPLETE = "complete"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ARCHIVED = "archived"
    PROPOSED = "proposed"
    APPROVED = "approved"
    READY = "ready"
    LEASED = "leased"
    DISPATCHED = "dispatched"
    PR_OPEN = "pr_open"
    MERGED = "merged"
    STAGED = "staged"
    PROMOTED = "promoted"
    VERIFIED = "verified"
    INVALIDATED = "invalidated"
    REVERTED = "reverted"
    PLANNED = "planned"
    RESERVED = "reserved"
    DISPATCH_REQUESTED = "dispatch_requested"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    TERMINATED = "terminated"
    OUTCOME_UNKNOWN = "outcome_unknown"


class JournalEventType(StrEnum):
    RUN_INITIALIZED = "run_initialized"
    PREFLIGHT_COMPLETED = "preflight_completed"
    DISCOVERY_COMPLETED = "discovery_completed"
    GATE_APPROVED = "gate_approved"
    TRELLIS_GRAPH_IMPORTED = "trellis_graph_imported"
    # Legacy replay value. New writes must use TRELLIS_GRAPH_IMPORTED.
    DECOMPOSITION_COMPLETED = "decomposition_completed"
    TASK_GRAPH_FROZEN = "task_graph_frozen"
    EXECUTION_COMPLETED = "execution_completed"
    INTEGRATION_VERIFIED = "integration_verified"
    QUALITY_DOCS_VERIFIED = "quality_docs_verified"
    PAUSE_REQUESTED = "pause_requested"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    RUN_BLOCKED = "run_blocked"
    RUN_ESCALATED = "run_escalated"
    CANCEL_REQUESTED = "cancel_requested"
    RUN_CANCELLED = "run_cancelled"
    RUN_FAILED = "run_failed"
    RUN_ARCHIVED = "run_archived"
    TASK_READY = "task_ready"
    LEASE_ACQUIRED = "lease_acquired"
    LEASE_RENEWED = "lease_renewed"
    LEASE_RELEASED = "lease_released"
    LEASE_LOST = "lease_lost"
    TASK_BLOCKED = "task_blocked"
    PR_OBSERVED = "pr_observed"
    MERGE_OBSERVED = "merge_observed"
    REVERT_OBSERVED = "revert_observed"
    TASK_VERIFIED = "task_verified"
    TASK_ARCHIVED = "task_archived"
    TASK_RETRY_SCHEDULED = "task_retry_scheduled"
    TASK_INVALIDATED = "task_invalidated"
    REPAIR_SCHEDULED = "repair_scheduled"
    REWORK_SCHEDULED = "rework_scheduled"
    TASK_REVERIFIED = "task_reverified"
    ATTEMPT_RESERVED = "attempt_reserved"
    ATTEMPT_RELEASED = "attempt_released"
    ATTEMPT_SUCCEEDED = "attempt_succeeded"
    ATTEMPT_FAILED = "attempt_failed"
    ATTEMPT_TERMINATED = "attempt_terminated"
    ATTEMPT_OUTCOME_UNKNOWN = "attempt_outcome_unknown"
    DECISION_REQUESTED = "decision_requested"
    DECISION_OBSERVED = "decision_observed"
    DISPATCH_REQUESTED = "dispatch_requested"
    DISPATCH_OBSERVED = "dispatch_observed"
    EFFECT_REQUESTED = "effect_requested"
    EFFECT_OBSERVED = "effect_observed"
    EFFECT_RECONCILED = "effect_reconciled"
    RESULT_STAGED = "result_staged"
    PROMOTION_REQUESTED = "promotion_requested"
    PROMOTION_OBSERVED = "promotion_observed"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    CLEANUP_REQUESTED = "cleanup_requested"
    CLEANUP_OBSERVED = "cleanup_observed"


@dataclass(frozen=True, slots=True)
class ExecutionIdentity(CanonicalRuntimeContract):
    """Run/task/attempt/correlation identity carried across every async boundary."""

    run_id: str
    coordinator_epoch: int
    task_id: str | None = None
    attempt: int | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _stable_id(self.run_id, "run_id"))
        object.__setattr__(
            self,
            "coordinator_epoch",
            _nonnegative(self.coordinator_epoch, "coordinator_epoch"),
        )
        if self.task_id is not None:
            object.__setattr__(self, "task_id", _stable_id(self.task_id, "task_id"))
        if self.attempt is not None:
            object.__setattr__(self, "attempt", _positive(self.attempt, "attempt"))
        if self.correlation_id is not None:
            object.__setattr__(
                self,
                "correlation_id",
                _stable_id(self.correlation_id, "correlation_id"),
            )
        if self.task_id is None and (
            self.attempt is not None or self.correlation_id is not None
        ):
            raise ValueError("attempt or correlation identity requires task_id")
        if self.attempt is None and self.correlation_id is not None:
            raise ValueError("correlation identity requires attempt")
        if self.attempt is not None and self.coordinator_epoch == 0:
            raise ValueError("attempt identity requires a positive coordinator epoch")

    @property
    def is_attempt(self) -> bool:
        return self.attempt is not None

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "coordinator_epoch": self.coordinator_epoch,
            "correlation_id": self.correlation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
        }


@dataclass(frozen=True, slots=True)
class ActorIdentity(CanonicalRuntimeContract):
    actor_type: ActorType
    actor_id: str
    host_id: str
    process_id: int
    process_start_id: str

    def __post_init__(self) -> None:
        if type(self.actor_type) is not ActorType:
            raise TypeError("actor_type must be an ActorType")
        object.__setattr__(self, "actor_id", _token(self.actor_id, "actor_id"))
        object.__setattr__(self, "host_id", _token(self.host_id, "host_id"))
        object.__setattr__(self, "process_id", _positive(self.process_id, "process_id"))
        object.__setattr__(
            self, "process_start_id", _token(self.process_start_id, "process_start_id")
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "actor_id": self.actor_id,
            "actor_type": self.actor_type.value,
            "host_id": self.host_id,
            "process_id": self.process_id,
            "process_start_id": self.process_start_id,
        }


@dataclass(frozen=True, slots=True)
class CommandIdentity(CanonicalRuntimeContract):
    schema_version: int
    command_id: str
    request_id: str
    kind: CommandKind
    expected_sequence: int
    request_nonce: str
    actor: ActorIdentity
    source_channel: SourceChannel
    submitted_at: str

    def __post_init__(self) -> None:
        _schema_version(self.schema_version)
        object.__setattr__(
            self, "command_id", _stable_id(self.command_id, "command_id")
        )
        object.__setattr__(
            self, "request_id", _stable_id(self.request_id, "request_id")
        )
        if type(self.kind) is not CommandKind:
            raise TypeError("kind must be a CommandKind")
        object.__setattr__(
            self,
            "expected_sequence",
            _nonnegative(self.expected_sequence, "expected_sequence"),
        )
        object.__setattr__(
            self, "request_nonce", _token(self.request_nonce, "request_nonce")
        )
        if type(self.actor) is not ActorIdentity:
            raise TypeError("actor must be an ActorIdentity")
        if type(self.source_channel) is not SourceChannel:
            raise TypeError("source_channel must be a SourceChannel")
        object.__setattr__(
            self, "submitted_at", _timestamp(self.submitted_at, "submitted_at")
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "actor": self.actor.to_primitive(),
            "command_id": self.command_id,
            "expected_sequence": self.expected_sequence,
            "kind": self.kind.value,
            "request_id": self.request_id,
            "request_nonce": self.request_nonce,
            "schema_version": self.schema_version,
            "source_channel": self.source_channel.value,
            "submitted_at": self.submitted_at,
        }


@dataclass(frozen=True, slots=True)
class DecisionRequest(CanonicalRuntimeContract):
    command: CommandIdentity
    decision_type: DecisionType
    candidate_hash: str
    workspace_hash: str
    expected_actor_id: str
    options: tuple[DecisionChoice, ...]

    def __post_init__(self) -> None:
        if (
            type(self.command) is not CommandIdentity
            or self.command.kind is not CommandKind.DECIDE
        ):
            raise ValueError("a decision request requires a decide command identity")
        if type(self.decision_type) is not DecisionType:
            raise TypeError("decision_type must be a DecisionType")
        object.__setattr__(
            self, "candidate_hash", _hash(self.candidate_hash, "candidate_hash")
        )
        object.__setattr__(
            self, "workspace_hash", _hash(self.workspace_hash, "workspace_hash")
        )
        object.__setattr__(
            self,
            "expected_actor_id",
            _token(self.expected_actor_id, "expected_actor_id"),
        )
        _contract_tuple(
            self.options,
            DecisionChoice,
            "options",
            max_items=MAX_DECISION_OPTIONS,
            nonempty=True,
        )
        if len(set(self.options)) != len(self.options):
            raise ValueError("options must not contain duplicates")

    def to_primitive(self) -> dict[str, object]:
        return {
            "candidate_hash": self.candidate_hash,
            "command": self.command.to_primitive(),
            "decision_type": self.decision_type.value,
            "expected_actor_id": self.expected_actor_id,
            "options": [option.value for option in self.options],
            "workspace_hash": self.workspace_hash,
        }


@dataclass(frozen=True, slots=True)
class DecisionCommand(CanonicalRuntimeContract):
    decision_id: str
    request: DecisionRequest
    choice: DecisionChoice
    actor: ActorIdentity
    source_channel: SourceChannel
    decided_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_id", _stable_id(self.decision_id, "decision_id")
        )
        if type(self.request) is not DecisionRequest:
            raise TypeError("request must be a DecisionRequest")
        if type(self.choice) is not DecisionChoice:
            raise TypeError("choice must be a DecisionChoice")
        if self.choice not in self.request.options:
            raise ValueError("choice must be one of the requested options")
        if type(self.actor) is not ActorIdentity:
            raise TypeError("actor must be an ActorIdentity")
        if self.actor.actor_id != self.request.expected_actor_id:
            raise ValueError("decision actor must match the requested actor")
        if type(self.source_channel) is not SourceChannel:
            raise TypeError("source_channel must be a SourceChannel")
        object.__setattr__(
            self, "decided_at", _timestamp(self.decided_at, "decided_at")
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "actor": self.actor.to_primitive(),
            "choice": self.choice.value,
            "decided_at": self.decided_at,
            "decision_id": self.decision_id,
            "request": self.request.to_primitive(),
            "source_channel": self.source_channel.value,
        }


@dataclass(frozen=True, slots=True)
class DecisionObservation(CanonicalRuntimeContract):
    decision: DecisionCommand
    event_sequence: int
    submission_hash: str

    def __post_init__(self) -> None:
        if type(self.decision) is not DecisionCommand:
            raise TypeError("decision must be a DecisionCommand")
        object.__setattr__(
            self, "event_sequence", _positive(self.event_sequence, "event_sequence")
        )
        object.__setattr__(
            self, "submission_hash", _hash(self.submission_hash, "submission_hash")
        )
        expected_hash = "sha256:" + canonical_sha256(self.decision.to_primitive())
        if self.submission_hash != expected_hash:
            raise ValueError("submission_hash does not match the decision command")

    def to_primitive(self) -> dict[str, object]:
        return {
            "decision": self.decision.to_primitive(),
            "event_sequence": self.event_sequence,
            "submission_hash": self.submission_hash,
        }


@dataclass(frozen=True, slots=True)
class DecisionEvaluation:
    accepted: bool
    reason: DecisionAdmissionReason
    observation: DecisionObservation | None = None
    idempotent: bool = False

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool or type(self.idempotent) is not bool:
            raise TypeError("decision flags must be booleans")
        if type(self.reason) is not DecisionAdmissionReason:
            raise TypeError("reason must be a DecisionAdmissionReason")
        if self.accepted != (self.observation is not None):
            raise ValueError("accepted decisions require exactly one observation")
        if (
            self.idempotent
            and self.reason is not DecisionAdmissionReason.IDEMPOTENT_REPLAY
        ):
            raise ValueError("only an exact replay may be idempotent")


@dataclass(frozen=True, slots=True)
class EvidenceProducer(CanonicalRuntimeContract):
    identity: ExecutionIdentity
    event_id: str | None = None
    external_object_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.identity) is not ExecutionIdentity:
            raise TypeError("identity must be an ExecutionIdentity")
        if self.event_id is not None:
            object.__setattr__(self, "event_id", _stable_id(self.event_id, "event_id"))
        if self.external_object_id is not None:
            object.__setattr__(
                self,
                "external_object_id",
                _token(self.external_object_id, "external_object_id"),
            )
        if self.event_id is None and self.external_object_id is None:
            raise ValueError(
                "an evidence producer requires an event or external object identity"
            )

    def to_primitive(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "external_object_id": self.external_object_id,
            "identity": self.identity.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class EvidenceRef(CanonicalRuntimeContract):
    schema_version: int
    digest: str
    byte_length: int
    evidence_type: EvidenceType
    producer: EvidenceProducer
    created_at: str
    sensitivity: EvidenceSensitivity
    render_policy: EvidenceRenderPolicy
    role: EvidenceRole
    structured_subject_hash: str | None = None

    def __post_init__(self) -> None:
        _schema_version(self.schema_version)
        object.__setattr__(self, "digest", _hash(self.digest, "digest"))
        object.__setattr__(
            self, "byte_length", _nonnegative(self.byte_length, "byte_length")
        )
        if type(self.evidence_type) is not EvidenceType:
            raise TypeError("evidence_type must be an EvidenceType")
        if type(self.producer) is not EvidenceProducer:
            raise TypeError("producer must be an EvidenceProducer")
        object.__setattr__(
            self, "created_at", _timestamp(self.created_at, "created_at")
        )
        if type(self.sensitivity) is not EvidenceSensitivity:
            raise TypeError("sensitivity must be an EvidenceSensitivity")
        if type(self.render_policy) is not EvidenceRenderPolicy:
            raise TypeError("render_policy must be an EvidenceRenderPolicy")
        if type(self.role) is not EvidenceRole:
            raise TypeError("role must be an EvidenceRole")
        object.__setattr__(
            self,
            "structured_subject_hash",
            _optional_hash(self.structured_subject_hash, "structured_subject_hash"),
        )
        if self.sensitivity is EvidenceSensitivity.SENSITIVE and self.render_policy in {
            EvidenceRenderPolicy.TEXT,
            EvidenceRenderPolicy.DOWNLOAD,
        }:
            raise ValueError("sensitive evidence cannot be rendered or downloaded")

    def to_primitive(self) -> dict[str, object]:
        return {
            "byte_length": self.byte_length,
            "created_at": self.created_at,
            "digest": self.digest,
            "evidence_type": self.evidence_type.value,
            "producer": self.producer.to_primitive(),
            "render_policy": self.render_policy.value,
            "role": self.role.value,
            "schema_version": self.schema_version,
            "sensitivity": self.sensitivity.value,
            "structured_subject_hash": self.structured_subject_hash,
        }


def _evidence_tuple(value: object, field_name: str) -> tuple[EvidenceRef, ...]:
    evidence = _contract_tuple(
        value, EvidenceRef, field_name, max_items=MAX_EVIDENCE_REFS
    )
    typed = tuple(evidence)  # type: ignore[arg-type]
    digests = [item.digest for item in typed]
    if len(set(digests)) != len(digests):
        raise ValueError(f"{field_name} must not repeat a digest")
    return tuple(sorted(typed, key=lambda item: item.digest))


@dataclass(frozen=True, slots=True)
class RetryMetadata(CanonicalRuntimeContract):
    attempt: int
    ceiling: int
    retry_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempt", _positive(self.attempt, "attempt"))
        object.__setattr__(self, "ceiling", _positive(self.ceiling, "ceiling"))
        if self.attempt > self.ceiling:
            raise ValueError("retry attempt cannot exceed its ceiling")
        object.__setattr__(self, "retry_at", _timestamp(self.retry_at, "retry_at"))

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "ceiling": self.ceiling,
            "retry_at": self.retry_at,
        }


@dataclass(frozen=True, slots=True)
class BudgetCharge(CanonicalRuntimeContract):
    dimension: BudgetDimension
    amount: int
    disposition: BudgetDisposition

    def __post_init__(self) -> None:
        if type(self.dimension) is not BudgetDimension:
            raise TypeError("dimension must be a BudgetDimension")
        object.__setattr__(self, "amount", _nonnegative(self.amount, "amount"))
        if type(self.disposition) is not BudgetDisposition:
            raise TypeError("disposition must be a BudgetDisposition")

    def to_primitive(self) -> dict[str, object]:
        return {
            "amount": self.amount,
            "dimension": self.dimension.value,
            "disposition": self.disposition.value,
        }


@dataclass(frozen=True, slots=True)
class EffectReceipt(CanonicalRuntimeContract):
    schema_version: int
    identity: ExecutionIdentity
    operation: EffectOperation
    status: EffectStatus
    observed_at: str
    effect_hash: str | None = None
    external_object_id: str | None = None
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        _schema_version(self.schema_version)
        if type(self.identity) is not ExecutionIdentity or not self.identity.is_attempt:
            raise ValueError("effect receipts require a complete attempt identity")
        if self.identity.correlation_id is None:
            raise ValueError("effect receipts require correlation identity")
        if type(self.operation) is not EffectOperation:
            raise TypeError("operation must be an EffectOperation")
        if type(self.status) is not EffectStatus:
            raise TypeError("status must be an EffectStatus")
        object.__setattr__(
            self, "observed_at", _timestamp(self.observed_at, "observed_at")
        )
        object.__setattr__(
            self, "effect_hash", _optional_hash(self.effect_hash, "effect_hash")
        )
        if self.external_object_id is not None:
            object.__setattr__(
                self,
                "external_object_id",
                _token(self.external_object_id, "external_object_id"),
            )
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "evidence"))
        if self.status is EffectStatus.ABSENT and (
            self.effect_hash is not None or self.external_object_id is not None
        ):
            raise ValueError("an absent effect cannot claim effect identity")
        if self.status is EffectStatus.APPLIED and self.effect_hash is None:
            raise ValueError("an applied effect requires effect_hash")
        if self.status is EffectStatus.UNKNOWN and not self.evidence:
            raise ValueError("an unknown effect requires evidence")

    def to_primitive(self) -> dict[str, object]:
        return {
            "effect_hash": self.effect_hash,
            "evidence": [item.to_primitive() for item in self.evidence],
            "external_object_id": self.external_object_id,
            "identity": self.identity.to_primitive(),
            "observed_at": self.observed_at,
            "operation": self.operation.value,
            "schema_version": self.schema_version,
            "status": self.status.value,
        }


class OutcomeValueType(StrEnum):
    ACKNOWLEDGEMENT = "acknowledgement"
    IDENTITY = "identity"
    JOURNAL_POSITION = "journal_position"
    EFFECT_RECEIPT = "effect_receipt"
    EVIDENCE_SET = "evidence_set"


@dataclass(frozen=True, slots=True)
class Acknowledgement(CanonicalRuntimeContract):
    value_type = OutcomeValueType.ACKNOWLEDGEMENT

    def to_primitive(self) -> dict[str, object]:
        return {"type": self.value_type.value}


@dataclass(frozen=True, slots=True)
class IdentityObservation(CanonicalRuntimeContract):
    identifier: str
    value_type = OutcomeValueType.IDENTITY

    def __post_init__(self) -> None:
        object.__setattr__(self, "identifier", _token(self.identifier, "identifier"))

    def to_primitive(self) -> dict[str, object]:
        return {"identifier": self.identifier, "type": self.value_type.value}


@dataclass(frozen=True, slots=True)
class JournalPosition(CanonicalRuntimeContract):
    sequence: int
    event_id: str
    event_hash: str
    value_type = OutcomeValueType.JOURNAL_POSITION

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", _positive(self.sequence, "sequence"))
        object.__setattr__(self, "event_id", _stable_id(self.event_id, "event_id"))
        object.__setattr__(self, "event_hash", _hash(self.event_hash, "event_hash"))

    def to_primitive(self) -> dict[str, object]:
        return {
            "event_hash": self.event_hash,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "type": self.value_type.value,
        }


@dataclass(frozen=True, slots=True)
class EffectReceiptValue(CanonicalRuntimeContract):
    receipt: EffectReceipt
    value_type = OutcomeValueType.EFFECT_RECEIPT

    def __post_init__(self) -> None:
        if type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt")

    def to_primitive(self) -> dict[str, object]:
        return {"receipt": self.receipt.to_primitive(), "type": self.value_type.value}


@dataclass(frozen=True, slots=True)
class EvidenceSet(CanonicalRuntimeContract):
    evidence: tuple[EvidenceRef, ...]
    value_type = OutcomeValueType.EVIDENCE_SET

    def __post_init__(self) -> None:
        normalized = _evidence_tuple(self.evidence, "evidence")
        if not normalized:
            raise ValueError("an evidence-set value must not be empty")
        object.__setattr__(self, "evidence", normalized)

    def to_primitive(self) -> dict[str, object]:
        return {
            "evidence": [item.to_primitive() for item in self.evidence],
            "type": self.value_type.value,
        }


OutcomeValue: TypeAlias = (
    Acknowledgement
    | IdentityObservation
    | JournalPosition
    | EffectReceiptValue
    | EvidenceSet
)
_OUTCOME_VALUE_TYPES = (
    Acknowledgement,
    IdentityObservation,
    JournalPosition,
    EffectReceiptValue,
    EvidenceSet,
)


@dataclass(frozen=True, slots=True)
class OperationOutcome(CanonicalRuntimeContract):
    """Validated result for every expected fallible domain/adapter boundary."""

    schema_version: int
    kind: OutcomeKind
    value: OutcomeValue | None = None
    reason_code: RuntimeReasonCode | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    retry: RetryMetadata | None = None
    budget_charge: BudgetCharge | None = None
    user_message_key: str | None = None

    def __post_init__(self) -> None:
        _schema_version(self.schema_version)
        if type(self.kind) is not OutcomeKind:
            raise TypeError("kind must be an OutcomeKind")
        if self.value is not None and type(self.value) not in _OUTCOME_VALUE_TYPES:
            raise TypeError("value must be a closed runtime outcome value")
        if (
            self.reason_code is not None
            and type(self.reason_code) is not RuntimeReasonCode
        ):
            raise TypeError("reason_code must be a RuntimeReasonCode or null")
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "evidence"))
        if self.retry is not None and type(self.retry) is not RetryMetadata:
            raise TypeError("retry must be RetryMetadata or null")
        if (
            self.budget_charge is not None
            and type(self.budget_charge) is not BudgetCharge
        ):
            raise TypeError("budget_charge must be BudgetCharge or null")
        if self.user_message_key is not None:
            object.__setattr__(
                self, "user_message_key", _message_key(self.user_message_key)
            )

        if self.kind is OutcomeKind.SUCCESS:
            if (
                self.reason_code is not None
                or self.retry is not None
                or self.user_message_key is not None
            ):
                raise ValueError("success cannot carry failure or retry metadata")
        else:
            if self.value is not None:
                raise ValueError("non-success outcomes cannot expose a value")
            if self.reason_code is None or self.user_message_key is None:
                raise ValueError(
                    "non-success outcomes require a reason and message key"
                )
        if self.kind is OutcomeKind.RETRYABLE:
            if self.retry is None:
                raise ValueError("retryable outcomes require retry metadata")
        elif self.retry is not None:
            raise ValueError("only retryable outcomes may carry retry metadata")
        if self.kind is OutcomeKind.BLOCKED and not self.evidence:
            raise ValueError("blocked outcomes require evidence")

    def to_primitive(self) -> dict[str, object]:
        return {
            "budget_charge": (
                None
                if self.budget_charge is None
                else self.budget_charge.to_primitive()
            ),
            "evidence": [item.to_primitive() for item in self.evidence],
            "kind": self.kind.value,
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "retry": None if self.retry is None else self.retry.to_primitive(),
            "schema_version": self.schema_version,
            "user_message_key": self.user_message_key,
            "value": None if self.value is None else self.value.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class TransitionPayload(CanonicalRuntimeContract):
    subject: TransitionSubject
    from_state: RuntimeState
    to_state: RuntimeState
    evidence: tuple[EvidenceRef, ...] = ()
    payload_type = "transition"

    def __post_init__(self) -> None:
        if type(self.subject) is not TransitionSubject:
            raise TypeError("subject must be a TransitionSubject")
        if (
            type(self.from_state) is not RuntimeState
            or type(self.to_state) is not RuntimeState
        ):
            raise TypeError("transition states must be RuntimeState values")
        if self.from_state is self.to_state:
            raise ValueError("a transition must change state")
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "evidence"))

    def to_primitive(self) -> dict[str, object]:
        return {
            "evidence": [item.to_primitive() for item in self.evidence],
            "from_state": self.from_state.value,
            "payload_type": self.payload_type,
            "subject": self.subject.value,
            "to_state": self.to_state.value,
        }


@dataclass(frozen=True, slots=True)
class DecisionRequestPayload(CanonicalRuntimeContract):
    request: DecisionRequest
    payload_type = "decision_request"

    def __post_init__(self) -> None:
        if type(self.request) is not DecisionRequest:
            raise TypeError("request must be a DecisionRequest")

    def to_primitive(self) -> dict[str, object]:
        return {
            "payload_type": self.payload_type,
            "request": self.request.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class DecisionObservedPayload(CanonicalRuntimeContract):
    observation: DecisionObservation
    payload_type = "decision_observed"

    def __post_init__(self) -> None:
        if type(self.observation) is not DecisionObservation:
            raise TypeError("observation must be a DecisionObservation")

    def to_primitive(self) -> dict[str, object]:
        return {
            "observation": self.observation.to_primitive(),
            "payload_type": self.payload_type,
        }


@dataclass(frozen=True, slots=True)
class EffectRequestPayload(CanonicalRuntimeContract):
    operation: EffectOperation
    adapter: AdapterKind
    object_type: EffectObjectType
    normalized_target_hash: str
    request_payload_hash: str
    expected_sequence: int
    fencing_token: int
    base_hash: str | None = None
    head_hash: str | None = None
    payload_type = "effect_request"

    def __post_init__(self) -> None:
        if type(self.operation) is not EffectOperation:
            raise TypeError("operation must be an EffectOperation")
        if type(self.adapter) is not AdapterKind:
            raise TypeError("adapter must be an AdapterKind")
        if type(self.object_type) is not EffectObjectType:
            raise TypeError("object_type must be an EffectObjectType")
        object.__setattr__(
            self,
            "normalized_target_hash",
            _hash(self.normalized_target_hash, "normalized_target_hash"),
        )
        object.__setattr__(
            self,
            "request_payload_hash",
            _hash(self.request_payload_hash, "request_payload_hash"),
        )
        object.__setattr__(
            self,
            "expected_sequence",
            _nonnegative(self.expected_sequence, "expected_sequence"),
        )
        object.__setattr__(
            self, "fencing_token", _positive(self.fencing_token, "fencing_token")
        )
        object.__setattr__(
            self, "base_hash", _optional_hash(self.base_hash, "base_hash")
        )
        object.__setattr__(
            self, "head_hash", _optional_hash(self.head_hash, "head_hash")
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "adapter": self.adapter.value,
            "base_hash": self.base_hash,
            "expected_sequence": self.expected_sequence,
            "fencing_token": self.fencing_token,
            "head_hash": self.head_hash,
            "normalized_target_hash": self.normalized_target_hash,
            "object_type": self.object_type.value,
            "operation": self.operation.value,
            "payload_type": self.payload_type,
            "request_payload_hash": self.request_payload_hash,
        }


@dataclass(frozen=True, slots=True)
class EffectObservationPayload(CanonicalRuntimeContract):
    adapter: AdapterKind
    receipt: EffectReceipt
    payload_type = "effect_observation"

    def __post_init__(self) -> None:
        if type(self.adapter) is not AdapterKind:
            raise TypeError("adapter must be an AdapterKind")
        if type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt")

    def to_primitive(self) -> dict[str, object]:
        return {
            "adapter": self.adapter.value,
            "payload_type": self.payload_type,
            "receipt": self.receipt.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class RecoveryPayload(CanonicalRuntimeContract):
    last_valid_sequence: int
    last_valid_event_hash: str
    affected_identities: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]
    payload_type = "recovery"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "last_valid_sequence",
            _nonnegative(self.last_valid_sequence, "last_valid_sequence"),
        )
        object.__setattr__(
            self,
            "last_valid_event_hash",
            _hash(self.last_valid_event_hash, "last_valid_event_hash"),
        )
        if type(self.affected_identities) is not tuple:
            raise TypeError("affected_identities must be a tuple")
        if len(self.affected_identities) > MAX_AFFECTED_IDENTITIES:
            raise ValueError("affected_identities exceeds the item limit")
        identities = tuple(
            _stable_id(item, "affected_identity") for item in self.affected_identities
        )
        if len(set(identities)) != len(identities):
            raise ValueError("affected_identities must not contain duplicates")
        object.__setattr__(self, "affected_identities", tuple(sorted(identities)))
        object.__setattr__(self, "evidence", _evidence_tuple(self.evidence, "evidence"))

    def to_primitive(self) -> dict[str, object]:
        return {
            "affected_identities": list(self.affected_identities),
            "evidence": [item.to_primitive() for item in self.evidence],
            "last_valid_event_hash": self.last_valid_event_hash,
            "last_valid_sequence": self.last_valid_sequence,
            "payload_type": self.payload_type,
        }


@dataclass(frozen=True, slots=True)
class DispatchRecoveryPayload(CanonicalRuntimeContract):
    """Human-authorized proof that one old dispatch is safe to retry."""

    recovery_id: str
    command: CommandIdentity
    subject_identity: ExecutionIdentity
    request_event_id: str
    request_sequence: int
    request_event_hash: str
    receipt: EffectReceipt
    process_tree_termination_proven: bool
    last_valid_sequence: int
    last_valid_event_hash: str
    evidence: tuple[EvidenceRef, ...]
    payload_type = "dispatch_recovery"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "recovery_id",
            _stable_id(self.recovery_id, "recovery_id"),
        )
        if type(self.command) is not CommandIdentity:
            raise TypeError("command must be a CommandIdentity")
        if self.command.kind is not CommandKind.RECONCILE:
            raise ValueError("dispatch recovery requires a reconcile command")
        if self.command.source_channel is not SourceChannel.DIRECT_CLI:
            raise ValueError("dispatch recovery requires the direct CLI channel")
        if self.command.actor.actor_type is not ActorType.HUMAN:
            raise ValueError("dispatch recovery requires a human actor")
        if (
            type(self.subject_identity) is not ExecutionIdentity
            or not self.subject_identity.is_attempt
            or self.subject_identity.correlation_id is None
        ):
            raise ValueError("subject_identity must be a complete attempt identity")
        object.__setattr__(
            self,
            "request_event_id",
            _stable_id(self.request_event_id, "request_event_id"),
        )
        object.__setattr__(
            self,
            "request_sequence",
            _positive(self.request_sequence, "request_sequence"),
        )
        object.__setattr__(
            self,
            "request_event_hash",
            _hash(self.request_event_hash, "request_event_hash"),
        )
        if type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt")
        if self.receipt.identity != self.subject_identity:
            raise ValueError("receipt identity must match subject_identity")
        if self.receipt.operation is not EffectOperation.WORKER_DISPATCH:
            raise ValueError("dispatch recovery requires a worker dispatch receipt")
        if self.receipt.status is not EffectStatus.ABSENT:
            raise ValueError("dispatch recovery requires an absent effect receipt")
        if self.process_tree_termination_proven is not True:
            raise ValueError("process tree termination must be proven")
        object.__setattr__(
            self,
            "last_valid_sequence",
            _nonnegative(self.last_valid_sequence, "last_valid_sequence"),
        )
        object.__setattr__(
            self,
            "last_valid_event_hash",
            _hash(self.last_valid_event_hash, "last_valid_event_hash"),
        )
        if self.request_sequence > self.last_valid_sequence:
            raise ValueError("request_sequence cannot follow the recovery anchor")
        if self.command.expected_sequence != self.last_valid_sequence:
            raise ValueError("command sequence must match the recovery anchor")
        admitted = _evidence_tuple(self.evidence, "evidence")
        if not admitted:
            raise ValueError("dispatch recovery requires evidence")
        required_types = {
            EvidenceType.EFFECT_RECEIPT,
            EvidenceType.PROCESS,
        }
        admitted_types = {
            item.evidence_type
            for item in admitted
            if item.role is EvidenceRole.REQUIRED
        }
        if not required_types.issubset(admitted_types):
            raise ValueError(
                "dispatch recovery requires effect receipt and process evidence"
            )
        if any(item.producer.identity != self.subject_identity for item in admitted):
            raise ValueError("recovery evidence must bind to subject_identity")
        object.__setattr__(self, "evidence", admitted)

    def to_primitive(self) -> dict[str, object]:
        return {
            "command": self.command.to_primitive(),
            "evidence": [item.to_primitive() for item in self.evidence],
            "last_valid_event_hash": self.last_valid_event_hash,
            "last_valid_sequence": self.last_valid_sequence,
            "payload_type": self.payload_type,
            "process_tree_termination_proven": (
                self.process_tree_termination_proven
            ),
            "receipt": self.receipt.to_primitive(),
            "recovery_id": self.recovery_id,
            "request_event_hash": self.request_event_hash,
            "request_event_id": self.request_event_id,
            "request_sequence": self.request_sequence,
            "subject_identity": self.subject_identity.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class LeaseOwner(CanonicalRuntimeContract):
    """Exact process and local workspace identity holding a coordinator lease."""

    actor: ActorIdentity
    local_repository_id: str
    local_worktree_id: str
    workspace_hash: str
    control_root_id: str

    def __post_init__(self) -> None:
        if type(self.actor) is not ActorIdentity:
            raise TypeError("actor must be an ActorIdentity")
        if self.actor.actor_type is not ActorType.COORDINATOR:
            raise ValueError("lease owner actor must be a coordinator")
        for name in (
            "local_repository_id",
            "local_worktree_id",
            "workspace_hash",
            "control_root_id",
        ):
            object.__setattr__(self, name, _hash(getattr(self, name), name))

    def to_primitive(self) -> dict[str, object]:
        return {
            "actor": self.actor.to_primitive(),
            "control_root_id": self.control_root_id,
            "local_repository_id": self.local_repository_id,
            "local_worktree_id": self.local_worktree_id,
            "workspace_hash": self.workspace_hash,
        }


@dataclass(frozen=True, slots=True)
class LeasePayload(CanonicalRuntimeContract):
    """Authority-stamped coordinator lease evidence stored in the Journal."""

    lease_id: str
    coordinator_id: str
    owner: LeaseOwner
    scheduler_mode: SchedulerMode
    fencing_token: int
    manifest_digest: str
    lease_ttl_seconds: int
    lease_clock_skew_seconds: int
    committed_at: str
    expires_at: str
    payload_type = "lease"

    def __post_init__(self) -> None:
        object.__setattr__(self, "lease_id", _stable_id(self.lease_id, "lease_id"))
        object.__setattr__(
            self,
            "coordinator_id",
            _token(self.coordinator_id, "coordinator_id"),
        )
        if type(self.owner) is not LeaseOwner:
            raise TypeError("owner must be a LeaseOwner")
        if self.owner.actor.actor_id != self.coordinator_id:
            raise ValueError("lease owner actor_id must match coordinator_id")
        if type(self.scheduler_mode) is not SchedulerMode:
            raise TypeError("scheduler_mode must be a SchedulerMode")
        if self.scheduler_mode is not SchedulerMode.WISH_BUILDER:
            raise ValueError("coordinator leases require wish_builder scheduler mode")
        object.__setattr__(
            self,
            "fencing_token",
            _positive(self.fencing_token, "fencing_token"),
        )
        object.__setattr__(
            self,
            "manifest_digest",
            _hash(self.manifest_digest, "manifest_digest"),
        )
        ttl, skew = validate_lease_timing(
            self.lease_ttl_seconds,
            self.lease_clock_skew_seconds,
        )
        object.__setattr__(self, "lease_ttl_seconds", ttl)
        object.__setattr__(self, "lease_clock_skew_seconds", skew)
        committed_at = _timestamp(self.committed_at, "committed_at")
        expires_at = _timestamp(self.expires_at, "expires_at")
        if _timestamp_value(expires_at) < _timestamp_value(committed_at):
            raise ValueError("expires_at must not precede committed_at")
        object.__setattr__(self, "committed_at", committed_at)
        object.__setattr__(self, "expires_at", expires_at)

    def to_primitive(self) -> dict[str, object]:
        return {
            "committed_at": self.committed_at,
            "coordinator_id": self.coordinator_id,
            "expires_at": self.expires_at,
            "fencing_token": self.fencing_token,
            "lease_clock_skew_seconds": self.lease_clock_skew_seconds,
            "lease_id": self.lease_id,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "manifest_digest": self.manifest_digest,
            "owner": self.owner.to_primitive(),
            "payload_type": self.payload_type,
            "scheduler_mode": self.scheduler_mode.value,
        }


@dataclass(frozen=True, slots=True)
class LeaseDraftPayload(CanonicalRuntimeContract):
    """Caller-controlled lease fields before authority time is assigned."""

    lease_id: str
    coordinator_id: str
    owner: LeaseOwner
    scheduler_mode: SchedulerMode
    fencing_token: int
    manifest_digest: str
    lease_ttl_seconds: int
    lease_clock_skew_seconds: int = 0

    def __post_init__(self) -> None:
        # Reuse the final payload validation with inert, valid authority times.
        validated = LeasePayload(
            lease_id=self.lease_id,
            coordinator_id=self.coordinator_id,
            owner=self.owner,
            scheduler_mode=self.scheduler_mode,
            fencing_token=self.fencing_token,
            manifest_digest=self.manifest_digest,
            lease_ttl_seconds=self.lease_ttl_seconds,
            lease_clock_skew_seconds=self.lease_clock_skew_seconds,
            committed_at="1970-01-01T00:00:00Z",
            expires_at="1970-01-01T00:00:00Z",
        )
        for name in (
            "lease_id",
            "coordinator_id",
            "owner",
            "scheduler_mode",
            "fencing_token",
            "manifest_digest",
            "lease_ttl_seconds",
            "lease_clock_skew_seconds",
        ):
            object.__setattr__(self, name, getattr(validated, name))

    def materialize(
        self,
        committed_at: datetime,
        *,
        terminal: bool,
    ) -> LeasePayload:
        if type(committed_at) is not datetime or committed_at.tzinfo is None:
            raise ValueError("committed_at must be a timezone-aware datetime")
        if type(terminal) is not bool:
            raise TypeError("terminal must be a bool")
        committed = committed_at.astimezone(UTC)
        expires = (
            committed
            if terminal
            else committed + timedelta(seconds=self.lease_ttl_seconds)
        )
        return LeasePayload(
            lease_id=self.lease_id,
            coordinator_id=self.coordinator_id,
            owner=self.owner,
            scheduler_mode=self.scheduler_mode,
            fencing_token=self.fencing_token,
            manifest_digest=self.manifest_digest,
            lease_ttl_seconds=self.lease_ttl_seconds,
            lease_clock_skew_seconds=self.lease_clock_skew_seconds,
            committed_at=_format_utc_timestamp(committed),
            expires_at=_format_utc_timestamp(expires),
        )

    def to_primitive(self) -> dict[str, object]:
        return {
            "coordinator_id": self.coordinator_id,
            "fencing_token": self.fencing_token,
            "lease_clock_skew_seconds": self.lease_clock_skew_seconds,
            "lease_id": self.lease_id,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "manifest_digest": self.manifest_digest,
            "owner": self.owner.to_primitive(),
            "scheduler_mode": self.scheduler_mode.value,
        }


JournalPayload: TypeAlias = (
    TransitionPayload
    | DecisionRequestPayload
    | DecisionObservedPayload
    | EffectRequestPayload
    | EffectObservationPayload
    | RecoveryPayload
    | DispatchRecoveryPayload
    | LeasePayload
)

_DECISION_PAYLOADS = {
    JournalEventType.DECISION_REQUESTED: DecisionRequestPayload,
    JournalEventType.DECISION_OBSERVED: DecisionObservedPayload,
}
_EFFECT_REQUEST_EVENTS = {
    JournalEventType.DISPATCH_REQUESTED,
    JournalEventType.EFFECT_REQUESTED,
    JournalEventType.PROMOTION_REQUESTED,
    JournalEventType.CLEANUP_REQUESTED,
}
_EFFECT_OBSERVATION_EVENTS = {
    JournalEventType.DISPATCH_OBSERVED,
    JournalEventType.EFFECT_OBSERVED,
    JournalEventType.EFFECT_RECONCILED,
    JournalEventType.PROMOTION_OBSERVED,
    JournalEventType.CLEANUP_OBSERVED,
}
_LEASE_EVENTS = {
    JournalEventType.LEASE_ACQUIRED,
    JournalEventType.LEASE_RENEWED,
    JournalEventType.LEASE_RELEASED,
    JournalEventType.LEASE_LOST,
}
_RUN_TRANSITION_EVENTS = {
    JournalEventType.RUN_INITIALIZED,
    JournalEventType.PREFLIGHT_COMPLETED,
    JournalEventType.DISCOVERY_COMPLETED,
    JournalEventType.GATE_APPROVED,
    JournalEventType.TRELLIS_GRAPH_IMPORTED,
    JournalEventType.DECOMPOSITION_COMPLETED,
    JournalEventType.TASK_GRAPH_FROZEN,
    JournalEventType.EXECUTION_COMPLETED,
    JournalEventType.INTEGRATION_VERIFIED,
    JournalEventType.QUALITY_DOCS_VERIFIED,
    JournalEventType.PAUSE_REQUESTED,
    JournalEventType.RUN_PAUSED,
    JournalEventType.RUN_RESUMED,
    JournalEventType.RUN_BLOCKED,
    JournalEventType.RUN_ESCALATED,
    JournalEventType.CANCEL_REQUESTED,
    JournalEventType.RUN_CANCELLED,
    JournalEventType.RUN_FAILED,
    JournalEventType.RUN_ARCHIVED,
}
_TASK_TRANSITION_EVENTS = {
    JournalEventType.TASK_READY,
    JournalEventType.LEASE_ACQUIRED,
    JournalEventType.TASK_BLOCKED,
    JournalEventType.PR_OBSERVED,
    JournalEventType.MERGE_OBSERVED,
    JournalEventType.REVERT_OBSERVED,
    JournalEventType.TASK_VERIFIED,
    JournalEventType.TASK_ARCHIVED,
    JournalEventType.TASK_RETRY_SCHEDULED,
    JournalEventType.TASK_INVALIDATED,
    JournalEventType.REPAIR_SCHEDULED,
    JournalEventType.REWORK_SCHEDULED,
    JournalEventType.TASK_REVERIFIED,
    JournalEventType.RESULT_STAGED,
}
_ATTEMPT_TRANSITION_EVENTS = {
    JournalEventType.ATTEMPT_RESERVED,
    JournalEventType.ATTEMPT_RELEASED,
    JournalEventType.CANCEL_REQUESTED,
    JournalEventType.ATTEMPT_SUCCEEDED,
    JournalEventType.ATTEMPT_FAILED,
    JournalEventType.ATTEMPT_TERMINATED,
    JournalEventType.ATTEMPT_OUTCOME_UNKNOWN,
}


def _required_payload_type(
    event_type: JournalEventType,
) -> type[object] | tuple[type[object], ...]:
    if event_type in _DECISION_PAYLOADS:
        return _DECISION_PAYLOADS[event_type]
    if event_type in _EFFECT_REQUEST_EVENTS:
        return EffectRequestPayload
    if event_type in _EFFECT_OBSERVATION_EVENTS:
        return EffectObservationPayload
    if event_type is JournalEventType.RECOVERY_STARTED:
        return RecoveryPayload
    if event_type is JournalEventType.RECOVERY_COMPLETED:
        return (RecoveryPayload, DispatchRecoveryPayload)
    if event_type is JournalEventType.LEASE_ACQUIRED:
        # TransitionPayload is the legacy task-lease replay shape.
        return (TransitionPayload, LeasePayload)
    if event_type in _LEASE_EVENTS:
        return LeasePayload
    return TransitionPayload


@dataclass(frozen=True, slots=True)
class JournalEvent(CanonicalRuntimeContract):
    event_version: str
    sequence: int
    event_id: str
    event_type: JournalEventType
    identity: ExecutionIdentity
    actor_type: ActorType
    actor_id: str
    recorded_at: str
    reason_code: RuntimeReasonCode | None
    previous_event_hash: str
    payload_hash: str
    payload: JournalPayload
    event_hash: str

    def __post_init__(self) -> None:
        if self.event_version != JOURNAL_EVENT_VERSION:
            raise ValueError(f"event_version must be {JOURNAL_EVENT_VERSION}")
        object.__setattr__(self, "sequence", _positive(self.sequence, "sequence"))
        object.__setattr__(self, "event_id", _stable_id(self.event_id, "event_id"))
        if type(self.event_type) is not JournalEventType:
            raise TypeError("event_type must be a JournalEventType")
        if type(self.identity) is not ExecutionIdentity:
            raise TypeError("identity must be an ExecutionIdentity")
        if type(self.actor_type) is not ActorType:
            raise TypeError("actor_type must be an ActorType")
        object.__setattr__(self, "actor_id", _token(self.actor_id, "actor_id"))
        object.__setattr__(
            self, "recorded_at", _timestamp(self.recorded_at, "recorded_at")
        )
        if (
            self.reason_code is not None
            and type(self.reason_code) is not RuntimeReasonCode
        ):
            raise TypeError("reason_code must be a RuntimeReasonCode or null")
        object.__setattr__(
            self,
            "previous_event_hash",
            _hash(self.previous_event_hash, "previous_event_hash"),
        )
        object.__setattr__(
            self, "payload_hash", _hash(self.payload_hash, "payload_hash")
        )
        object.__setattr__(self, "event_hash", _hash(self.event_hash, "event_hash"))
        required_type = _required_payload_type(self.event_type)
        payload_types = (
            required_type if type(required_type) is tuple else (required_type,)
        )
        if type(self.payload) not in payload_types:
            required_name = " or ".join(item.__name__ for item in payload_types)
            raise TypeError(f"{self.event_type.value} requires {required_name}")
        if self.payload_hash != self.computed_payload_hash():
            raise ValueError("payload_hash does not match the canonical payload")
        if self.event_hash != self.computed_event_hash():
            raise ValueError("event_hash does not match the canonical event")
        if self.sequence == 1 and self.previous_event_hash != "sha256:" + "0" * 64:
            raise ValueError("the first event must use the genesis previous hash")
        if self.event_type in _EFFECT_REQUEST_EVENTS | _EFFECT_OBSERVATION_EVENTS and (
            not self.identity.is_attempt or self.identity.correlation_id is None
        ):
            raise ValueError(
                "effect events require complete attempt/correlation identity"
            )
        if type(self.payload) is TransitionPayload:
            expected_subjects = (
                {TransitionSubject.RUN, TransitionSubject.ATTEMPT}
                if self.event_type is JournalEventType.CANCEL_REQUESTED
                else {
                    TransitionSubject.RUN
                    if self.event_type in _RUN_TRANSITION_EVENTS
                    else TransitionSubject.TASK
                    if self.event_type in _TASK_TRANSITION_EVENTS
                    else TransitionSubject.ATTEMPT
                }
            )
            if self.payload.subject not in expected_subjects:
                raise ValueError("transition subject does not match event_type")
            if (
                self.payload.subject is TransitionSubject.RUN
                and self.identity.task_id is not None
            ):
                raise ValueError("run transitions cannot carry task identity")
            if self.payload.subject is TransitionSubject.TASK and (
                self.identity.task_id is None or self.identity.attempt is not None
            ):
                raise ValueError("task transitions require task-only identity")
            if (
                self.payload.subject is TransitionSubject.ATTEMPT
                and not self.identity.is_attempt
            ):
                raise ValueError("attempt transitions require attempt identity")
        if type(self.payload) is LeasePayload:
            if self.event_type not in _LEASE_EVENTS:
                raise ValueError("lease payload requires a lease event_type")
            if (
                self.identity.task_id is not None
                or self.identity.attempt is not None
                or self.identity.correlation_id is not None
            ):
                raise ValueError("coordinator lease events require run-only identity")
            if self.identity.coordinator_epoch != self.payload.fencing_token:
                raise ValueError("lease fencing token must match coordinator_epoch")
            if self.recorded_at != self.payload.committed_at:
                raise ValueError("lease committed_at must match event recorded_at")
            terminal = self.event_type in {
                JournalEventType.LEASE_RELEASED,
                JournalEventType.LEASE_LOST,
            }
            committed = _timestamp_value(self.payload.committed_at)
            expires = _timestamp_value(self.payload.expires_at)
            if terminal and expires != committed:
                raise ValueError("terminal lease events must expire at authority time")
            if not terminal and expires <= committed:
                raise ValueError("active lease events require a future expiry")
            if self.event_type in {
                JournalEventType.LEASE_ACQUIRED,
                JournalEventType.LEASE_RENEWED,
                JournalEventType.LEASE_RELEASED,
            } and (
                self.actor_type is not ActorType.COORDINATOR
                or self.actor_id != self.payload.coordinator_id
            ):
                raise ValueError("lease holder must be the event actor")
            if self.event_type is JournalEventType.LEASE_LOST:
                if self.reason_code is not RuntimeReasonCode.LEASE_LOST:
                    raise ValueError("lease_lost requires the lease_lost reason code")
            elif self.reason_code is not None:
                raise ValueError("non-loss lease events cannot carry a reason code")
        if type(self.payload) is DecisionRequestPayload:
            command = self.payload.request.command
            if command.expected_sequence != self.sequence:
                raise ValueError(
                    "decision request sequence does not match its journal event"
                )
            if (
                command.actor.actor_type is not self.actor_type
                or command.actor.actor_id != self.actor_id
            ):
                raise ValueError(
                    "decision request actor does not match its journal event"
                )
        if type(self.payload) is DecisionObservedPayload:
            observation = self.payload.observation
            if observation.event_sequence != self.sequence:
                raise ValueError(
                    "decision observation sequence does not match its journal event"
                )
            if (
                observation.decision.request.command.expected_sequence + 1
                != self.sequence
            ):
                raise ValueError("decision observation is not the next expected event")
            if (
                observation.decision.actor.actor_type is not self.actor_type
                or observation.decision.actor.actor_id != self.actor_id
            ):
                raise ValueError("decision actor does not match its journal event")
        if type(self.payload) is DispatchRecoveryPayload:
            subject = self.payload.subject_identity
            if self.event_type is not JournalEventType.RECOVERY_COMPLETED:
                raise ValueError(
                    "dispatch recovery payload requires recovery_completed"
                )
            if (
                self.identity.task_id is not None
                or self.identity.attempt is not None
                or self.identity.correlation_id is not None
            ):
                raise ValueError("dispatch recovery events require run-only identity")
            if self.identity.run_id != subject.run_id:
                raise ValueError("dispatch recovery run does not match its subject")
            if self.identity.coordinator_epoch <= subject.coordinator_epoch:
                raise ValueError("dispatch recovery requires a newer coordinator epoch")
            if (
                self.actor_type is not self.payload.command.actor.actor_type
                or self.actor_id != self.payload.command.actor.actor_id
            ):
                raise ValueError("recovery command actor does not match its event")
            if self.payload.last_valid_sequence != self.sequence - 1:
                raise ValueError("recovery sequence anchor must be the predecessor")
            if self.payload.last_valid_event_hash != self.previous_event_hash:
                raise ValueError("recovery hash anchor must be the predecessor")
        if type(self.payload) is EffectObservationPayload:
            receipt_identity = self.payload.receipt.identity
            same_attempt = (
                receipt_identity.run_id == self.identity.run_id
                and receipt_identity.task_id == self.identity.task_id
                and receipt_identity.attempt == self.identity.attempt
                and receipt_identity.correlation_id == self.identity.correlation_id
            )
            cross_epoch_dispatch_reconciliation = (
                self.event_type is JournalEventType.DISPATCH_OBSERVED
                and same_attempt
                and self.identity.coordinator_epoch
                > receipt_identity.coordinator_epoch
            )
            cross_epoch_child_effect_reconciliation = (
                self.event_type is JournalEventType.EFFECT_RECONCILED
                and (
                    (
                        self.payload.adapter is AdapterKind.BACKEND
                        and self.payload.receipt.operation
                        in {
                            EffectOperation.RESERVE_CHANNEL,
                            EffectOperation.SEND_TASK_PACKET,
                            EffectOperation.CANCEL_TURN,
                        }
                    )
                    or (
                        self.payload.adapter is AdapterKind.TRELLIS
                        and self.payload.receipt.operation
                        in {
                            EffectOperation.PREPARE_ATTEMPT,
                            EffectOperation.CHECK_ATTEMPT,
                            EffectOperation.FINISH_ATTEMPT,
                        }
                    )
                )
                and same_attempt
                and self.identity.coordinator_epoch
                > receipt_identity.coordinator_epoch
            )
            if (
                receipt_identity != self.identity
                and not cross_epoch_dispatch_reconciliation
                and not cross_epoch_child_effect_reconciliation
            ):
                raise ValueError(
                    "effect receipt identity does not match its journal event"
                )
        if (
            self.event_type
            in {
                JournalEventType.RUN_BLOCKED,
                JournalEventType.RUN_FAILED,
                JournalEventType.TASK_BLOCKED,
                JournalEventType.ATTEMPT_FAILED,
                JournalEventType.ATTEMPT_OUTCOME_UNKNOWN,
            }
            and self.reason_code is None
        ):
            raise ValueError("blocking or failure events require a reason_code")

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        event_id: str,
        event_type: JournalEventType,
        identity: ExecutionIdentity,
        actor_type: ActorType,
        actor_id: str,
        recorded_at: str,
        previous_event_hash: str,
        payload: JournalPayload,
        reason_code: RuntimeReasonCode | None = None,
    ) -> JournalEvent:
        if type(payload) not in (
            TransitionPayload,
            DecisionRequestPayload,
            DecisionObservedPayload,
            EffectRequestPayload,
            EffectObservationPayload,
            RecoveryPayload,
            DispatchRecoveryPayload,
            LeasePayload,
        ):
            raise TypeError("payload must be a closed journal payload")
        if event_type is JournalEventType.DECOMPOSITION_COMPLETED:
            raise ValueError("legacy decomposition event_type is replay-only")
        if reason_code is RuntimeReasonCode.DECOMPOSITION_INCOMPLETE:
            raise ValueError("legacy decomposition reason_code is replay-only")
        if type(payload) is TransitionPayload and RuntimeState.DECOMPOSITION in {
            payload.from_state,
            payload.to_state,
        }:
            raise ValueError("legacy decomposition state is replay-only")
        payload_hash = "sha256:" + canonical_sha256(payload.to_primitive())
        provisional = cls.__new__(cls)
        values = {
            "event_version": JOURNAL_EVENT_VERSION,
            "sequence": sequence,
            "event_id": event_id,
            "event_type": event_type,
            "identity": identity,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "recorded_at": recorded_at,
            "reason_code": reason_code,
            "previous_event_hash": previous_event_hash,
            "payload_hash": payload_hash,
            "payload": payload,
        }
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        event_hash = provisional.computed_event_hash()
        return cls(event_hash=event_hash, **values)

    def computed_payload_hash(self) -> str:
        return "sha256:" + canonical_sha256(self.payload.to_primitive())

    def _unsigned_primitive(self) -> dict[str, object]:
        return {
            "actor_id": self.actor_id,
            "actor_type": self.actor_type.value,
            "attempt": self.identity.attempt,
            "coordinator_epoch": self.identity.coordinator_epoch,
            "correlation_id": self.identity.correlation_id,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "event_version": self.event_version,
            "payload": self.payload.to_primitive(),
            "payload_hash": self.payload_hash,
            "previous_event_hash": self.previous_event_hash,
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "recorded_at": self.recorded_at,
            "run_id": self.identity.run_id,
            "sequence": self.sequence,
            "task_id": self.identity.task_id,
        }

    def computed_event_hash(self) -> str:
        return "sha256:" + canonical_sha256(self._unsigned_primitive())

    def to_primitive(self) -> dict[str, object]:
        return {**self._unsigned_primitive(), "event_hash": self.event_hash}


__all__ = [
    "JOURNAL_EVENT_VERSION",
    "MAX_AFFECTED_IDENTITIES",
    "MAX_DECISION_OPTIONS",
    "MAX_EVIDENCE_REFS",
    "MAX_LEASE_TTL_SECONDS",
    "MAX_RUNTIME_ID_LENGTH",
    "MAX_RUNTIME_TEXT_LENGTH",
    "MAX_RUNTIME_TOKEN_LENGTH",
    "MIN_LEASE_TTL_SECONDS",
    "RUNTIME_SCHEMA_VERSION",
    "Acknowledgement",
    "ActorIdentity",
    "ActorType",
    "AdapterKind",
    "BudgetCharge",
    "BudgetDimension",
    "BudgetDisposition",
    "CanonicalRuntimeContract",
    "CommandIdentity",
    "CommandKind",
    "DecisionAdmissionReason",
    "DecisionChoice",
    "DecisionCommand",
    "DecisionEvaluation",
    "DecisionObservation",
    "DecisionObservedPayload",
    "DecisionRequest",
    "DecisionRequestPayload",
    "DecisionType",
    "DispatchRecoveryPayload",
    "EffectObjectType",
    "EffectObservationPayload",
    "EffectOperation",
    "EffectReceipt",
    "EffectReceiptValue",
    "EffectRequestPayload",
    "EffectStatus",
    "EvidenceProducer",
    "EvidenceRef",
    "EvidenceRenderPolicy",
    "EvidenceRole",
    "EvidenceSensitivity",
    "EvidenceSet",
    "EvidenceType",
    "ExecutionIdentity",
    "IdentityObservation",
    "JournalEvent",
    "JournalEventType",
    "JournalPayload",
    "JournalPosition",
    "LeaseDraftPayload",
    "LeaseOwner",
    "LeasePayload",
    "OperationOutcome",
    "OutcomeKind",
    "OutcomeValue",
    "OutcomeValueType",
    "RecoveryPayload",
    "RetryMetadata",
    "RuntimeReasonCode",
    "RuntimeState",
    "SourceChannel",
    "TransitionPayload",
    "TransitionSubject",
]
