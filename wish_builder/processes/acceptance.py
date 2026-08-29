"""Production acceptance runner for typed manifest command specs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from wish_builder.contracts.manifest_v2 import CommandSpec, ManifestTask, NetworkPolicy
from wish_builder.contracts.runtime import (
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    OutcomeKind,
    RuntimeReasonCode,
)
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.services.promotion import PromotionRecord

from .runner import (
    EnvironmentVariable,
    ProcessConfigurationError,
    ProcessLimits,
    ProcessOutcome,
    ProcessOutcomeStatus,
    ProcessRequest,
    ProcessRunner,
    StreamLimits,
)
from .workflow import AcceptanceResult

_DEFAULT_FRAME_LIMIT = 10_000
_RESULT_FRAME_LIMIT = 1_024


RunnerFactory = Callable[[CommandSpec], ProcessRunner]
Clock = Callable[[], datetime | str]


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _timestamp(clock: Clock) -> str:
    value = clock()
    if type(value) is str:
        return value
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime or timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256_ref(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _path_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _reject(reason_code: RuntimeReasonCode) -> AcceptanceResult:
    return AcceptanceResult(False, (), reason_code)


class ProcessAcceptancePort:
    """Run every typed acceptance command in a materialized candidate repository."""

    def __init__(
        self,
        *,
        identity: ExecutionIdentity | None = None,
        identities: Mapping[str, ExecutionIdentity] | None = None,
        executable_profiles: Mapping[str, str | os.PathLike[str]] | None = None,
        runner_factory: RunnerFactory | None = None,
        clock: Clock = _default_clock,
    ) -> None:
        if identity is not None and type(identity) is not ExecutionIdentity:
            raise TypeError("identity must be an ExecutionIdentity or null")
        normalized: dict[str, ExecutionIdentity] = {}
        if identities is not None:
            for task_id, task_identity in identities.items():
                if type(task_id) is not str or not task_id:
                    raise ValueError("identity map task IDs must be non-empty")
                if type(task_identity) is not ExecutionIdentity:
                    raise TypeError("identity map values must be ExecutionIdentity")
                normalized[task_id] = task_identity
        if identity is not None:
            if identity.task_id is None:
                raise ValueError("identity must include a task_id")
            normalized[identity.task_id] = identity
        self._identities = normalized
        self._executable_profiles = dict(executable_profiles or {})
        self._runner_factory = runner_factory or (
            lambda command: ProcessRunner(
                environment_allowlist=command.environment_allowlist
            )
        )
        if not callable(self._runner_factory):
            raise TypeError("runner_factory must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock

    def verify(
        self,
        task: ManifestTask,
        repository: Path,
        promotion: PromotionRecord,
    ) -> AcceptanceResult:
        if type(task) is not ManifestTask:
            raise TypeError("task must be a ManifestTask")
        if type(promotion) is not PromotionRecord:
            raise TypeError("promotion must be a PromotionRecord")
        if task.id != promotion.task_id:
            return _reject(RuntimeReasonCode.INVARIANT_VIOLATION)
        identity = self._identities.get(task.id)
        if (
            identity is None
            or identity.run_id is None
            or identity.task_id != task.id
            or identity.attempt is None
            or identity.coordinator_epoch <= 0
        ):
            return _reject(RuntimeReasonCode.INVARIANT_VIOLATION)
        try:
            repository_root = Path(repository).resolve(strict=True)
        except (OSError, RuntimeError):
            return _reject(RuntimeReasonCode.GIT_STATE_CONFLICT)
        if not repository_root.is_dir():
            return _reject(RuntimeReasonCode.GIT_STATE_CONFLICT)

        observations: list[dict[str, object]] = []
        for index, command in enumerate(task.acceptance_commands):
            result = self._run_command(
                index,
                command,
                repository_root,
                task,
                promotion,
            )
            if isinstance(result, RuntimeReasonCode):
                return _reject(result)
            observations.append(result)

        raw = canonical_json_bytes(
            {
                "commands": observations,
                "producer": "process-acceptance-port",
                "promotion": promotion.to_primitive(),
                "task_id": task.id,
            }
        )
        digest = _sha256_ref(raw)
        return AcceptanceResult(
            True,
            (
                EvidenceRef(
                    1,
                    digest,
                    len(raw),
                    EvidenceType.PROCESS,
                    EvidenceProducer(
                        identity,
                        external_object_id=f"acceptance-{task.id}",
                    ),
                    _timestamp(self._clock),
                    EvidenceSensitivity.INTERNAL,
                    EvidenceRenderPolicy.METADATA_ONLY,
                    EvidenceRole.REQUIRED,
                    digest,
                ),
            ),
        )

    def _run_command(
        self,
        index: int,
        command: CommandSpec,
        repository_root: Path,
        task: ManifestTask,
        promotion: PromotionRecord,
    ) -> dict[str, object] | RuntimeReasonCode:
        if command.network_policy is not NetworkPolicy.DENIED:
            return RuntimeReasonCode.POLICY_DENIED
        try:
            executable = self._resolve_executable(command)
            cwd = (repository_root / command.working_directory).resolve(strict=True)
        except (OSError, RuntimeError, ProcessConfigurationError):
            return RuntimeReasonCode.PROCESS_START_FAILED
        if not _path_within(cwd, repository_root):
            return RuntimeReasonCode.GIT_STATE_CONFLICT
        environment: list[EnvironmentVariable] = []
        for name in command.environment_allowlist:
            if name in os.environ:
                try:
                    environment.append(EnvironmentVariable(name, os.environ[name]))
                except ProcessConfigurationError:
                    return RuntimeReasonCode.PROCESS_START_FAILED
        try:
            request = ProcessRequest.create(
                executable=executable,
                arguments=command.argv[1:],
                cwd=cwd,
                environment=tuple(environment),
                timeout_seconds=command.timeout_seconds,
                limits=ProcessLimits(
                    StreamLimits(command.stdout_limit_bytes, _DEFAULT_FRAME_LIMIT),
                    StreamLimits(command.stderr_limit_bytes, _DEFAULT_FRAME_LIMIT),
                    StreamLimits(command.result_limit_bytes, _RESULT_FRAME_LIMIT),
                ),
            )
        except (OSError, RuntimeError, ProcessConfigurationError):
            return RuntimeReasonCode.PROCESS_START_FAILED
        if f"sha256:{request.executable.sha256}" != command.executable_identity_digest:
            return RuntimeReasonCode.PROCESS_START_FAILED
        outcome = self._runner_factory(command).run(request)
        reason = self._admission_failure(outcome)
        if reason is not None:
            return reason
        if outcome.result.data:
            try:
                decoded_result = json.loads(outcome.result.data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return RuntimeReasonCode.EVIDENCE_INVALID
            if type(decoded_result) is not dict:
                return RuntimeReasonCode.EVIDENCE_INVALID
        return self._observation(index, command, task, promotion, outcome)

    def _resolve_executable(self, command: CommandSpec) -> Path:
        configured = self._executable_profiles.get(command.executable_profile)
        if configured is not None:
            return Path(configured)
        resolved = shutil.which(command.argv[0])
        if resolved is None:
            raise ProcessConfigurationError(
                "executable_unavailable",
                command.executable_profile,
            )
        return Path(resolved)

    @staticmethod
    def _admission_failure(outcome: ProcessOutcome) -> RuntimeReasonCode | None:
        if type(outcome) is not ProcessOutcome:
            return RuntimeReasonCode.EXTERNAL_OUTCOME_UNKNOWN
        if (
            outcome.status is not ProcessOutcomeStatus.SUCCESS
            or outcome.kind is not OutcomeKind.SUCCESS
            or outcome.reason_code is not None
            or outcome.failure is not None
            or outcome.exit_code != 0
            or outcome.termination_attempted
            or not outcome.termination_proven
            or not outcome.stdout.complete
            or not outcome.stderr.complete
            or not outcome.result.complete
        ):
            return outcome.reason_code or RuntimeReasonCode.CHECK_FAILED
        return None

    @staticmethod
    def _observation(
        index: int,
        command: CommandSpec,
        task: ManifestTask,
        promotion: PromotionRecord,
        outcome: ProcessOutcome,
    ) -> dict[str, object]:
        return {
            "argv_sha256": outcome.argv_sha256,
            "command_index": index,
            "cwd": {
                "canonical_path": outcome.cwd.canonical_path,
                "target_device": outcome.cwd.target_device,
                "target_inode": outcome.cwd.target_inode,
            },
            "display_text": command.display_text,
            "duration_microseconds": int(round(outcome.duration_seconds * 1_000_000)),
            "executable": {
                "canonical_path": outcome.executable.canonical_path,
                "sha256": "sha256:" + outcome.executable.sha256,
                "target_device": outcome.executable.target_device,
                "target_inode": outcome.executable.target_inode,
            },
            "exit_code": outcome.exit_code,
            "promotion_commit_sha": promotion.promoted_commit_sha,
            "result": _stream_observation(outcome.result.data),
            "stderr": _stream_observation(outcome.stderr.data),
            "stdout": _stream_observation(outcome.stdout.data),
            "task_id": task.id,
        }


def _stream_observation(data: bytes) -> dict[str, object]:
    return {
        "byte_length": len(data),
        "digest": _sha256_ref(data),
    }


__all__ = ["ProcessAcceptancePort"]
