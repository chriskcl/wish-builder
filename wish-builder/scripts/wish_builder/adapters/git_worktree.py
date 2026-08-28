"""Strict local Git boundary for isolated Wish Builder attempts.

The target worktree is never used as an attempt workspace.  Creation, staging,
promotion, and removal all share one cross-process repository mutation lock and
revalidate the M1-D32 workspace identity immediately before mutation.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import stat
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Generic, Protocol, TypeVar

from wish_builder.adapters.git_identity import (
    FilesystemIdentity,
    GitIdentityError,
    WorkspaceIdentity,
    capture_filesystem_identity,
    capture_workspace_identity,
    revalidate_workspace_identity,
)
from wish_builder.contracts.manifest_v2 import PathCaseMode
from wish_builder.contracts.runtime import (
    AdapterKind,
    EffectObjectType,
    EffectOperation,
    EffectReceipt,
    EffectReceiptValue,
    EffectStatus,
    EvidenceProducer,
    EvidenceRef,
    EvidenceRenderPolicy,
    EvidenceRole,
    EvidenceSensitivity,
    EvidenceType,
    ExecutionIdentity,
    JournalEventType,
    OperationOutcome,
    OutcomeKind,
    RuntimeReasonCode,
)
from wish_builder.contracts.serialization import canonical_json_bytes
from wish_builder.services.cleanup import (
    CleanupCandidate,
    CleanupCommand,
    CleanupDisposition,
    CleanupInspection,
    CleanupObservation,
    CleanupPlan,
)
from wish_builder.services.ports import PreparedEffect
from wish_builder.services.promotion import (
    PromotionCommand,
    PromotionDisposition,
    PromotionObservation,
    PromotionPlan,
    StagedResultView,
)


PORTABLE_GIT_PROFILE_VERSION = 1
MAX_TREE_ENTRIES = 10_000
MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_TREE_BYTES = 128 * 1024 * 1024
MAX_PATH_BYTES = 1024
MAX_COMPONENT_BYTES = 255
DEFAULT_REPOSITORY_LOCK_TIMEOUT_SECONDS = 30.0
_STDERR_LIMIT = 64 * 1024
_READ_CHUNK = 64 * 1024
_ALLOWED_MODES = frozenset({"100644", "100755"})
_ZERO_SHA1 = "0" * 40
_ZERO_SHA256 = "0" * 64
_FIXED_COMMIT_DATE = "946684800 +0000"
_WINDOWS_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "clock$",
        "conin$",
        "conout$",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        *(f"com{index}" for index in "¹²³"),
        *(f"lpt{index}" for index in "¹²³"),
    }
)
_INVALID_WINDOWS_CHARACTERS = frozenset('<>"|*?')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256_ref(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _raw_sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _nonempty(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _git_oid(value: object, field_name: str) -> str:
    text = _nonempty(value, field_name)
    if len(text) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{field_name} must be a lowercase Git object ID")
    return text


def _sha256(value: object, field_name: str) -> str:
    text = _nonempty(value, field_name)
    if len(text) != 71 or not text.startswith("sha256:") or any(
        character not in "0123456789abcdef" for character in text[7:]
    ):
        raise ValueError(f"{field_name} must be a full sha256 reference")
    return text


def _is_reparse(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse)


class GitBoundaryError(RuntimeError):
    """Named local Git protocol, identity, or mutation failure."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        reason_code: RuntimeReasonCode = RuntimeReasonCode.GIT_STATE_CONFLICT,
    ) -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.reason_code = reason_code


class GitWorktreeCrash(RuntimeError):
    """Failpoint exception used to model process death at mutation boundaries."""


class GitMutationFailpoint(Protocol):
    def __call__(self, point: str, path: Path) -> None: ...


class AttemptEffectDisposition(StrEnum):
    APPLIED = "applied"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    path: str
    mode: str
    object_id: str
    byte_length: int

    def __post_init__(self) -> None:
        _validate_portable_path(self.path)
        if self.mode not in _ALLOWED_MODES:
            raise ValueError("tree entry mode is outside the portable profile")
        _git_oid(self.object_id, "object_id")
        if type(self.byte_length) is not int or not 0 <= self.byte_length <= MAX_BLOB_BYTES:
            raise ValueError("byte_length is outside the blob limit")

    def to_primitive(self) -> dict[str, object]:
        return {
            "byte_length": self.byte_length,
            "mode": self.mode,
            "object_id": self.object_id,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class ChangedPath:
    path: str
    base: GitTreeEntry | None
    result: GitTreeEntry | None

    def __post_init__(self) -> None:
        _validate_portable_path(self.path)
        if self.base is None and self.result is None:
            raise ValueError("a changed path requires a base or result entry")
        for entry in (self.base, self.result):
            if entry is not None and type(entry) is not GitTreeEntry:
                raise TypeError("changed entries must be GitTreeEntry values")
            if entry is not None and entry.path != self.path:
                raise ValueError("changed entry path does not match")

    @property
    def change(self) -> str:
        if self.base is None:
            return "added"
        if self.result is None:
            return "deleted"
        return "modified"

    def to_primitive(self) -> dict[str, object]:
        return {
            "base": None if self.base is None else self.base.to_primitive(),
            "change": self.change,
            "path": self.path,
            "result": None if self.result is None else self.result.to_primitive(),
        }


def _canonical_pattern(value: object) -> str:
    if type(value) is not str:
        raise TypeError("repository path patterns must be strings")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value or not normalized or "\\" in normalized:
        raise ValueError("repository path pattern is not canonical")
    recursive = normalized.endswith("/**")
    core = normalized[:-3] if recursive else normalized
    if (
        not core
        or core.startswith("/")
        or core.endswith("/")
        or any(token in core for token in "*?[")
    ):
        raise ValueError("repository path pattern is not canonical")
    _validate_portable_path(core)
    return normalized


def _patterns(
    values: Iterable[str],
    field_name: str,
    *,
    nonempty: bool,
) -> tuple[str, ...]:
    try:
        result = tuple(sorted({_canonical_pattern(value) for value in values}))
    except TypeError as exc:
        raise TypeError(f"{field_name} must be iterable path patterns") from exc
    if nonempty and not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


def _validate_portable_path(path: str) -> None:
    if type(path) is not str or not path:
        raise GitBoundaryError("git_path_invalid")
    if unicodedata.normalize("NFC", path) != path:
        raise GitBoundaryError("git_path_not_nfc", path)
    try:
        encoded = path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise GitBoundaryError("git_path_invalid_utf8") from exc
    if len(encoded) > MAX_PATH_BYTES:
        raise GitBoundaryError(
            "git_path_limit",
            path,
            reason_code=RuntimeReasonCode.LIMIT_EXCEEDED,
        )
    if path.startswith(("/", "\\")) or "\\" in path or "\x00" in path:
        raise GitBoundaryError("git_path_escape", path)
    components = path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise GitBoundaryError("git_path_escape", path)
    for component in components:
        if len(component.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise GitBoundaryError(
                "git_component_limit",
                component,
                reason_code=RuntimeReasonCode.LIMIT_EXCEEDED,
            )
        if (
            ":" in component
            or component.endswith((" ", "."))
            or any(
                ord(character) < 0x20
                or ord(character) == 0x7F
                or character in _INVALID_WINDOWS_CHARACTERS
                for character in component
            )
        ):
            raise GitBoundaryError("git_path_not_portable", path)
        alias = component.casefold().rstrip(" .")
        stem = alias.split(".", 1)[0]
        if alias == ".git" or stem in _WINDOWS_DEVICE_NAMES:
            raise GitBoundaryError("git_path_not_portable", path)


def _pattern_matches(pattern: str, path: str, mode: PathCaseMode) -> bool:
    compared_pattern = pattern if mode is PathCaseMode.SENSITIVE else pattern.casefold()
    compared_path = path if mode is PathCaseMode.SENSITIVE else path.casefold()
    if compared_pattern.endswith("/**"):
        root = compared_pattern[:-3]
        return compared_path.startswith(root + "/")
    return compared_path == compared_pattern


@dataclass(frozen=True, slots=True)
class AttemptWorktreeCommand:
    operation_id: str
    identity: ExecutionIdentity
    local_repository_id: str
    target_workspace_hash: str
    base_commit_sha: str
    base_tree_sha: str
    directory_name: str
    owned_paths: tuple[str, ...]
    allowed_auxiliary_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    path_case_mode: PathCaseMode

    def __post_init__(self) -> None:
        _nonempty(self.operation_id, "operation_id")
        if type(self.identity) is not ExecutionIdentity or not self.identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        if self.identity.correlation_id != self.operation_id:
            raise ValueError("operation_id must match correlation identity")
        _sha256(self.local_repository_id, "local_repository_id")
        _sha256(self.target_workspace_hash, "target_workspace_hash")
        _git_oid(self.base_commit_sha, "base_commit_sha")
        _git_oid(self.base_tree_sha, "base_tree_sha")
        name = _nonempty(self.directory_name, "directory_name")
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("directory_name must be one safe path component")
        object.__setattr__(
            self, "owned_paths", _patterns(self.owned_paths, "owned_paths", nonempty=True)
        )
        object.__setattr__(
            self,
            "allowed_auxiliary_paths",
            _patterns(
                self.allowed_auxiliary_paths,
                "allowed_auxiliary_paths",
                nonempty=False,
            ),
        )
        object.__setattr__(
            self,
            "protected_paths",
            _patterns(self.protected_paths, "protected_paths", nonempty=False),
        )
        if type(self.path_case_mode) is not PathCaseMode:
            raise TypeError("path_case_mode must be a PathCaseMode")

    def to_primitive(self) -> dict[str, object]:
        return {
            "allowed_auxiliary_paths": list(self.allowed_auxiliary_paths),
            "base_commit_sha": self.base_commit_sha,
            "base_tree_sha": self.base_tree_sha,
            "directory_name": self.directory_name,
            "identity": self.identity.to_primitive(),
            "local_repository_id": self.local_repository_id,
            "operation": EffectOperation.REPOSITORY_UPDATE.value,
            "operation_id": self.operation_id,
            "owned_paths": list(self.owned_paths),
            "path_case_mode": self.path_case_mode.value,
            "protected_paths": list(self.protected_paths),
            "target_workspace_hash": self.target_workspace_hash,
        }


@dataclass(frozen=True, slots=True)
class AttemptWorktree:
    identity: ExecutionIdentity
    path: str
    external_object_id: str
    local_repository_id: str
    target_workspace_hash: str
    worktree_root: FilesystemIdentity
    git_dir: FilesystemIdentity
    base_commit_sha: str
    base_tree_sha: str
    owned_paths: tuple[str, ...]
    allowed_auxiliary_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    path_case_mode: PathCaseMode

    def __post_init__(self) -> None:
        if type(self.identity) is not ExecutionIdentity or not self.identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        _nonempty(self.path, "path")
        _nonempty(self.external_object_id, "external_object_id")
        _sha256(self.local_repository_id, "local_repository_id")
        _sha256(self.target_workspace_hash, "target_workspace_hash")
        if type(self.worktree_root) is not FilesystemIdentity:
            raise TypeError("worktree_root must be a FilesystemIdentity")
        if type(self.git_dir) is not FilesystemIdentity:
            raise TypeError("git_dir must be a FilesystemIdentity")
        _git_oid(self.base_commit_sha, "base_commit_sha")
        _git_oid(self.base_tree_sha, "base_tree_sha")
        if type(self.path_case_mode) is not PathCaseMode:
            raise TypeError("path_case_mode must be a PathCaseMode")

    @property
    def run_id(self) -> str:
        return self.identity.run_id

    @property
    def task_id(self) -> str:
        assert self.identity.task_id is not None
        return self.identity.task_id

    @property
    def attempt_number(self) -> int:
        assert self.identity.attempt is not None
        return self.identity.attempt

    @property
    def attempt_hash(self) -> str:
        return _sha256_ref(self.to_primitive())

    def to_primitive(self) -> dict[str, object]:
        return {
            "allowed_auxiliary_paths": list(self.allowed_auxiliary_paths),
            "base_commit_sha": self.base_commit_sha,
            "base_tree_sha": self.base_tree_sha,
            "external_object_id": self.external_object_id,
            "git_dir": self.git_dir.to_primitive(),
            "identity": self.identity.to_primitive(),
            "local_repository_id": self.local_repository_id,
            "owned_paths": list(self.owned_paths),
            "path": self.path,
            "path_case_mode": self.path_case_mode.value,
            "protected_paths": list(self.protected_paths),
            "target_workspace_hash": self.target_workspace_hash,
            "worktree_root": self.worktree_root.to_primitive(),
        }


@dataclass(frozen=True, slots=True)
class AttemptResultManifest:
    schema_version: int
    identity: ExecutionIdentity
    local_repository_id: str
    attempt_hash: str
    base_commit_sha: str
    base_tree_sha: str
    result_commit_sha: str
    result_tree_sha: str
    path_case_mode: PathCaseMode
    changed_paths: tuple[ChangedPath, ...]
    total_blob_bytes: int
    portable_profile_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if type(self.identity) is not ExecutionIdentity or not self.identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        _sha256(self.local_repository_id, "local_repository_id")
        _sha256(self.attempt_hash, "attempt_hash")
        for name in (
            "base_commit_sha",
            "base_tree_sha",
            "result_commit_sha",
            "result_tree_sha",
        ):
            _git_oid(getattr(self, name), name)
        if type(self.path_case_mode) is not PathCaseMode:
            raise TypeError("path_case_mode must be a PathCaseMode")
        if type(self.changed_paths) is not tuple or not all(
            type(item) is ChangedPath for item in self.changed_paths
        ):
            raise TypeError("changed_paths must contain ChangedPath values")
        if tuple(item.path for item in self.changed_paths) != tuple(
            sorted(item.path for item in self.changed_paths)
        ):
            raise ValueError("changed_paths must use stable path order")
        if len({item.path for item in self.changed_paths}) != len(self.changed_paths):
            raise ValueError("changed_paths must not contain duplicate paths")
        if type(self.total_blob_bytes) is not int or not 0 <= self.total_blob_bytes <= MAX_TREE_BYTES:
            raise ValueError("total_blob_bytes is outside the tree limit")
        _sha256(self.portable_profile_hash, "portable_profile_hash")

    @property
    def manifest_hash(self) -> str:
        return _sha256_ref(self.to_primitive())

    def to_primitive(self) -> dict[str, object]:
        return {
            "attempt_hash": self.attempt_hash,
            "base_commit_sha": self.base_commit_sha,
            "base_tree_sha": self.base_tree_sha,
            "changed_paths": [item.to_primitive() for item in self.changed_paths],
            "identity": self.identity.to_primitive(),
            "local_repository_id": self.local_repository_id,
            "path_case_mode": self.path_case_mode.value,
            "portable_profile_hash": self.portable_profile_hash,
            "result_commit_sha": self.result_commit_sha,
            "result_tree_sha": self.result_tree_sha,
            "schema_version": self.schema_version,
            "total_blob_bytes": self.total_blob_bytes,
        }


@dataclass(frozen=True, slots=True)
class ResultValidation:
    accepted: bool
    manifest: AttemptResultManifest | None
    reason_code: RuntimeReasonCode | None
    violations: tuple[str, ...]
    evidence_hash: str

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a bool")
        if self.accepted:
            if type(self.manifest) is not AttemptResultManifest or self.reason_code is not None:
                raise ValueError("accepted validation requires only its manifest")
        elif self.manifest is not None or self.reason_code is None or not self.violations:
            raise ValueError("rejected validation requires a reason and violations")
        if type(self.violations) is not tuple or not all(
            type(item) is str for item in self.violations
        ):
            raise TypeError("violations must be a tuple of strings")
        _sha256(self.evidence_hash, "evidence_hash")


@dataclass(frozen=True, slots=True)
class StageResultCommand:
    operation_id: str
    identity: ExecutionIdentity
    local_repository_id: str
    target_workspace_hash: str
    result_manifest_hash: str
    result_commit_sha: str
    result_tree_sha: str
    staged_ref: str

    def __post_init__(self) -> None:
        _nonempty(self.operation_id, "operation_id")
        if type(self.identity) is not ExecutionIdentity or not self.identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        _sha256(self.local_repository_id, "local_repository_id")
        _sha256(self.target_workspace_hash, "target_workspace_hash")
        _sha256(self.result_manifest_hash, "result_manifest_hash")
        _git_oid(self.result_commit_sha, "result_commit_sha")
        _git_oid(self.result_tree_sha, "result_tree_sha")
        if not self.staged_ref.startswith("refs/wish-builder/staged/"):
            raise ValueError("staged_ref is outside the controlled namespace")

    def to_primitive(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_primitive(),
            "local_repository_id": self.local_repository_id,
            "operation": EffectOperation.RESULT_STAGE.value,
            "operation_id": self.operation_id,
            "result_commit_sha": self.result_commit_sha,
            "result_manifest_hash": self.result_manifest_hash,
            "result_tree_sha": self.result_tree_sha,
            "staged_ref": self.staged_ref,
            "target_workspace_hash": self.target_workspace_hash,
        }


@dataclass(frozen=True, slots=True)
class StagedResult:
    manifest: AttemptResultManifest
    staged_ref: str
    stage_effect_hash: str

    def __post_init__(self) -> None:
        if type(self.manifest) is not AttemptResultManifest:
            raise TypeError("manifest must be an AttemptResultManifest")
        if not self.staged_ref.startswith("refs/wish-builder/staged/"):
            raise ValueError("staged_ref is outside the controlled namespace")
        _sha256(self.stage_effect_hash, "stage_effect_hash")

    @property
    def run_id(self) -> str:
        return self.manifest.identity.run_id

    @property
    def task_id(self) -> str:
        assert self.manifest.identity.task_id is not None
        return self.manifest.identity.task_id

    @property
    def attempt(self) -> int:
        assert self.manifest.identity.attempt is not None
        return self.manifest.identity.attempt

    @property
    def local_repository_id(self) -> str:
        return self.manifest.local_repository_id

    @property
    def result_commit_sha(self) -> str:
        return self.manifest.result_commit_sha

    @property
    def result_tree_sha(self) -> str:
        return self.manifest.result_tree_sha

    @property
    def result_manifest_hash(self) -> str:
        return self.manifest.manifest_hash


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RepositoryEffect(Generic[T]):
    receipt: EffectReceipt
    disposition: AttemptEffectDisposition
    value: T | None = None
    reason_code: RuntimeReasonCode | None = None
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.receipt) is not EffectReceipt:
            raise TypeError("receipt must be an EffectReceipt")
        if type(self.disposition) is not AttemptEffectDisposition:
            raise TypeError("disposition must be an AttemptEffectDisposition")
        if self.receipt.status is not EffectStatus(self.disposition.value):
            raise ValueError("receipt status does not match disposition")
        if self.disposition is AttemptEffectDisposition.APPLIED:
            if self.value is None or self.reason_code is not None:
                raise ValueError("applied repository effects require only their value")
        elif self.value is not None or self.reason_code is None:
            raise ValueError("non-applied repository effects require a reason")
        if type(self.details) is not tuple or not all(
            type(item) is str for item in self.details
        ):
            raise TypeError("details must be a tuple of strings")

    def to_outcome(self) -> OperationOutcome:
        return OperationOutcome(
            1,
            OutcomeKind.SUCCESS,
            value=EffectReceiptValue(self.receipt),
        )


PORTABLE_GIT_PROFILE_HASH = _sha256_ref(
    {
        "allowed_modes": sorted(_ALLOWED_MODES),
        "casefold": "manifest_path_case_mode",
        "max_blob_bytes": MAX_BLOB_BYTES,
        "max_component_bytes": MAX_COMPONENT_BYTES,
        "max_path_bytes": MAX_PATH_BYTES,
        "max_tree_bytes": MAX_TREE_BYTES,
        "max_tree_entries": MAX_TREE_ENTRIES,
        "normalization": "NFC",
        "reject_gitlinks": True,
        "reject_symlinks": True,
        "version": PORTABLE_GIT_PROFILE_VERSION,
    }
)


def _git_environment(
    *,
    index_file: Path | None = None,
    commit_identity: bool = False,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
        }
    )
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    if commit_identity:
        environment.update(
            {
                "GIT_AUTHOR_DATE": _FIXED_COMMIT_DATE,
                "GIT_AUTHOR_EMAIL": "promotion@wish-builder.invalid",
                "GIT_AUTHOR_NAME": "Wish Builder Promotion",
                "GIT_COMMITTER_DATE": _FIXED_COMMIT_DATE,
                "GIT_COMMITTER_EMAIL": "promotion@wish-builder.invalid",
                "GIT_COMMITTER_NAME": "Wish Builder Promotion",
            }
        )
    return environment


def _run_git(
    repository: Path,
    arguments: Iterable[str],
    *,
    input_data: bytes | None = None,
    environment: dict[str, str] | None = None,
    check: bool = True,
) -> bytes:
    hooks_path = "NUL" if os.name == "nt" else "/dev/null"
    command = [
        "git",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        f"core.hooksPath={hooks_path}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "commit.gpgSign=false",
        "-C",
        str(repository),
        *arguments,
    ]
    try:
        completed = subprocess.run(
            command,
            input=input_data,
            stdin=subprocess.DEVNULL if input_data is None else None,
            capture_output=True,
            check=False,
            shell=False,
            env=environment or _git_environment(),
        )
    except OSError as exc:
        raise GitBoundaryError("git_unavailable", command[0]) from exc
    if check and completed.returncode != 0:
        detail = completed.stderr[:_STDERR_LIMIT].decode(
            "utf-8", errors="replace"
        ).strip()
        raise GitBoundaryError("git_command_failed", detail[:2048])
    return completed.stdout


def _git_text(repository: Path, *arguments: str) -> str:
    raw = _run_git(repository, arguments)
    try:
        return raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise GitBoundaryError("git_output_invalid_utf8") from exc


def _git_text_environment(
    repository: Path,
    arguments: Iterable[str],
    environment: dict[str, str],
) -> str:
    raw = _run_git(repository, arguments, environment=environment)
    try:
        return raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise GitBoundaryError("git_output_invalid_utf8") from exc


def _git_text_with_input(
    repository: Path,
    arguments: Iterable[str],
    input_data: bytes,
    *,
    environment: dict[str, str],
) -> str:
    raw = _run_git(
        repository,
        arguments,
        input_data=input_data,
        environment=environment,
    )
    try:
        return raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise GitBoundaryError("git_output_invalid_utf8") from exc


def _git_object_id_length(repository: Path) -> int:
    object_format = _git_text(repository, "rev-parse", "--show-object-format")
    if object_format == "sha1":
        return 40
    if object_format == "sha256":
        return 64
    raise GitBoundaryError("git_object_format_unsupported", object_format)


def _sync_file(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


class _RepositoryMutationLock:
    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds

    @contextmanager
    def acquire(self) -> Iterator[None]:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise GitBoundaryError("repository_lock_open_failed", str(self.path)) from exc
        with open(descriptor, "r+b", buffering=0, closefd=True) as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                _sync_file(handle)
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError) as exc:
                    busy = isinstance(exc, BlockingIOError) or getattr(
                        exc, "winerror", None
                    ) in {33, 36} or getattr(exc, "errno", None) in {
                        errno.EACCES,
                        errno.EAGAIN,
                    }
                    if not busy or time.monotonic() >= deadline:
                        raise GitBoundaryError("repository_lock_busy") from exc
                    time.sleep(0.01)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True, slots=True)
class _AttemptObservation:
    exists: bool
    exact: bool
    attempt: AttemptWorktree | None
    head_sha: str | None
    clean: bool
    details: tuple[str, ...]


class GitWorktreeAdapter:
    """Real local repository port for active-M1 attempt effects."""

    def __init__(
        self,
        repository: str | os.PathLike[str],
        attempts_root: str | os.PathLike[str],
        expected_workspace: WorkspaceIdentity,
        *,
        lock_timeout_seconds: float = DEFAULT_REPOSITORY_LOCK_TIMEOUT_SECONDS,
        clock: Callable[[], str] = _utc_now,
        failpoint: GitMutationFailpoint | None = None,
    ) -> None:
        if type(expected_workspace) is not WorkspaceIdentity:
            raise TypeError("expected_workspace must be a WorkspaceIdentity")
        if (
            type(lock_timeout_seconds) not in {int, float}
            or not math.isfinite(lock_timeout_seconds)
            or lock_timeout_seconds <= 0
        ):
            raise ValueError("lock_timeout_seconds must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")
        requested = Path(repository).expanduser().absolute()
        root = Path(expected_workspace.worktree_root.canonical_path)
        try:
            if requested.resolve(strict=True) != root.resolve(strict=True):
                raise GitBoundaryError("workspace_identity_mismatch")
        except OSError as exc:
            raise GitBoundaryError("workspace_unavailable") from exc
        attempts = Path(attempts_root).expanduser().absolute()
        try:
            attempts_identity = capture_filesystem_identity(attempts)
            attempts_resolved = Path(attempts_identity.canonical_path).resolve(strict=True)
            target_resolved = root.resolve(strict=True)
        except (GitIdentityError, OSError) as exc:
            raise GitBoundaryError("attempt_root_unavailable", str(attempts)) from exc
        if attempts_identity.is_link_or_reparse_point:
            raise GitBoundaryError("attempt_root_is_link", str(attempts))
        if not attempts_resolved.is_dir():
            raise GitBoundaryError("attempt_root_not_directory", str(attempts))
        try:
            attempts_resolved.relative_to(target_resolved)
        except ValueError:
            pass
        else:
            raise GitBoundaryError("attempt_root_inside_target", str(attempts_resolved))
        try:
            target_resolved.relative_to(attempts_resolved)
        except ValueError:
            pass
        else:
            raise GitBoundaryError("target_inside_attempt_root", str(target_resolved))

        self.repository = target_resolved
        self.attempts_root = attempts_resolved
        self.attempts_root_identity = attempts_identity
        self._expected_workspace = expected_workspace
        self._clock = clock
        self._failpoint = failpoint
        common_dir = Path(expected_workspace.common_dir.canonical_path)
        self._mutation_lock = _RepositoryMutationLock(
            common_dir / "wish-builder.repository.lock",
            float(lock_timeout_seconds),
        )
        self._object_id_length = _git_object_id_length(self.repository)
        self._verify_repository_profile()

    @property
    def expected_workspace(self) -> WorkspaceIdentity:
        return self._expected_workspace

    def _trigger(self, point: str, path: Path) -> None:
        if self._failpoint is not None:
            self._failpoint(point, path)

    def _verify_repository_profile(self) -> None:
        if _git_text(self.repository, "rev-parse", "--is-shallow-repository") != "false":
            raise GitBoundaryError("shallow_repository_unsupported")
        alternates = (
            Path(self._expected_workspace.common_dir.canonical_path)
            / "objects"
            / "info"
            / "alternates"
        )
        if alternates.exists():
            raise GitBoundaryError("git_alternates_unsupported")
        if _git_text(self.repository, "for-each-ref", "--format=%(refname)", "refs/replace"):
            raise GitBoundaryError("git_replace_refs_unsupported")

    def _guard_attempt_root(self) -> None:
        try:
            observed = capture_filesystem_identity(self.attempts_root)
        except GitIdentityError as exc:
            raise GitBoundaryError("attempt_root_drift", exc.reason) from exc
        if observed != self.attempts_root_identity:
            raise GitBoundaryError("attempt_root_drift")

    def _guard_target(self) -> None:
        comparison = revalidate_workspace_identity(self._expected_workspace)
        if not comparison.ok:
            raise GitBoundaryError(
                "workspace_drift",
                ",".join(comparison.mismatches),
                reason_code=RuntimeReasonCode.WORKSPACE_DRIFT,
            )

    def _guard_target_structure(self) -> WorkspaceIdentity:
        try:
            observed = capture_workspace_identity(
                self.repository,
                self._expected_workspace.scopes,
            )
        except GitIdentityError as exc:
            raise GitBoundaryError("workspace_drift", exc.reason) from exc
        expected = self._expected_workspace
        if (
            observed.local_repository_id != expected.local_repository_id
            or observed.local_worktree_id != expected.local_worktree_id
            or observed.target_full_ref != expected.target_full_ref
        ):
            raise GitBoundaryError(
                "workspace_drift",
                reason_code=RuntimeReasonCode.WORKSPACE_DRIFT,
            )
        return observed

    def _zero_oid(self) -> str:
        return _ZERO_SHA1 if self._object_id_length == 40 else _ZERO_SHA256

    def _receipt(
        self,
        identity: ExecutionIdentity,
        operation: EffectOperation,
        status: EffectStatus,
        *,
        effect_value: object | None = None,
        external_object_id: str | None = None,
        details: tuple[str, ...] = (),
        evidence: tuple[EvidenceRef, ...] = (),
    ) -> EffectReceipt:
        observed_at = self._clock()
        if status is EffectStatus.UNKNOWN:
            raw = canonical_json_bytes(
                {
                    "details": list(details),
                    "identity": identity.to_primitive(),
                    "operation": operation.value,
                }
            )
            evidence = (
                *evidence,
                EvidenceRef(
                    1,
                    _raw_sha256(raw),
                    len(raw),
                    EvidenceType.GIT,
                    EvidenceProducer(
                        identity,
                        external_object_id="git-worktree-adapter",
                    ),
                    observed_at,
                    EvidenceSensitivity.INTERNAL,
                    EvidenceRenderPolicy.METADATA_ONLY,
                    EvidenceRole.REQUIRED,
                    _sha256_ref(
                        {
                            "identity": identity.to_primitive(),
                            "operation": operation.value,
                        }
                    ),
                ),
            )
        effect_hash = (
            None
            if status is not EffectStatus.APPLIED
            else _sha256_ref(effect_value if effect_value is not None else {})
        )
        return EffectReceipt(
            1,
            identity,
            operation,
            status,
            observed_at,
            effect_hash=effect_hash,
            external_object_id=(
                external_object_id if status is EffectStatus.APPLIED else None
            ),
            evidence=evidence,
        )

    def plan_attempt(
        self,
        identity: ExecutionIdentity,
        *,
        owned_paths: Iterable[str],
        protected_paths: Iterable[str] = (),
        allowed_auxiliary_paths: Iterable[str] = (),
        path_case_mode: PathCaseMode = PathCaseMode.SENSITIVE,
    ) -> AttemptWorktreeCommand:
        if type(identity) is not ExecutionIdentity or not identity.is_attempt:
            raise ValueError("identity must be a complete attempt identity")
        if identity.correlation_id is None:
            raise ValueError("attempt creation requires correlation identity")
        self._guard_target()
        owned = _patterns(owned_paths, "owned_paths", nonempty=True)
        auxiliary = _patterns(
            allowed_auxiliary_paths,
            "allowed_auxiliary_paths",
            nonempty=False,
        )
        protected = _patterns(protected_paths, "protected_paths", nonempty=False)
        requested_scopes = set(owned + auxiliary + protected)
        if not requested_scopes <= set(self._expected_workspace.scopes):
            raise GitBoundaryError("workspace_scope_not_pinned")
        base_commit = self._expected_workspace.base_commit_sha
        base_tree = _git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{base_commit}^{{tree}}",
        )
        self._tree_entries(base_commit, path_case_mode)
        directory_digest = hashlib.sha256(
            canonical_json_bytes(identity.to_primitive())
        ).hexdigest()[:24]
        directory_name = f"attempt-{directory_digest}"
        return AttemptWorktreeCommand(
            operation_id=identity.correlation_id,
            identity=identity,
            local_repository_id=self._expected_workspace.local_repository_id,
            target_workspace_hash=self._expected_workspace.workspace_hash,
            base_commit_sha=base_commit,
            base_tree_sha=base_tree,
            directory_name=directory_name,
            owned_paths=owned,
            allowed_auxiliary_paths=auxiliary,
            protected_paths=protected,
            path_case_mode=path_case_mode,
        )

    def create_attempt(
        self,
        effect: PreparedEffect[AttemptWorktreeCommand],
    ) -> RepositoryEffect[AttemptWorktree]:
        command = self._validate_attempt_effect(effect)
        destination = self.attempts_root / command.directory_name
        with self._mutation_lock.acquire():
            self._guard_attempt_root()
            self._guard_target()
            observed = self._inspect_attempt(command)
            if observed.exists:
                return self._attempt_observation_effect(command, observed)
            self._guard_attempt_root()
            self._guard_target()
            self._trigger("before_attempt_create", destination)
            try:
                _run_git(
                    self.repository,
                    (
                        "worktree",
                        "add",
                        "--detach",
                        str(destination),
                        command.base_commit_sha,
                    ),
                )
                self._trigger("after_attempt_create", destination)
            except GitWorktreeCrash:
                raise
            except GitBoundaryError as exc:
                reconciled = self._inspect_attempt(command)
                if reconciled.exists:
                    return self._attempt_observation_effect(command, reconciled)
                receipt = self._receipt(
                    command.identity,
                    EffectOperation.REPOSITORY_UPDATE,
                    EffectStatus.ABSENT,
                )
                return RepositoryEffect(
                    receipt,
                    AttemptEffectDisposition.ABSENT,
                    reason_code=exc.reason_code,
                    details=(exc.code, exc.detail),
                )
            observed = self._inspect_attempt(command)
            return self._attempt_observation_effect(command, observed)

    def inspect_attempt(
        self,
        command: AttemptWorktreeCommand,
    ) -> RepositoryEffect[AttemptWorktree]:
        if type(command) is not AttemptWorktreeCommand:
            raise TypeError("command must be an AttemptWorktreeCommand")
        return self._attempt_observation_effect(command, self._inspect_attempt(command))

    def _validate_attempt_effect(
        self,
        effect: PreparedEffect[AttemptWorktreeCommand],
    ) -> AttemptWorktreeCommand:
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        command = effect.command
        if type(command) is not AttemptWorktreeCommand:
            raise TypeError("prepared command must be an AttemptWorktreeCommand")
        payload = effect.request.payload
        if effect.request.event.event_type is not JournalEventType.EFFECT_REQUESTED:
            raise ValueError("attempt creation requires effect_requested")
        if (
            payload.operation is not EffectOperation.REPOSITORY_UPDATE
            or payload.adapter is not AdapterKind.GIT
            or payload.object_type is not EffectObjectType.WORKTREE
        ):
            raise ValueError("attempt request has the wrong effect boundary")
        if payload.normalized_target_hash != command.target_workspace_hash:
            raise ValueError("attempt request target hash does not match")
        if effect.request.identity != command.identity:
            raise ValueError("attempt request identity does not match")
        if (
            command.local_repository_id
            != self._expected_workspace.local_repository_id
            or command.target_workspace_hash != self._expected_workspace.workspace_hash
            or command.base_commit_sha != self._expected_workspace.base_commit_sha
        ):
            raise ValueError("attempt command is stale for this repository")
        effect.command_hash
        return command

    def _attempt_observation_effect(
        self,
        command: AttemptWorktreeCommand,
        observed: _AttemptObservation,
    ) -> RepositoryEffect[AttemptWorktree]:
        if observed.exists and observed.exact and observed.attempt is not None:
            receipt = self._receipt(
                command.identity,
                EffectOperation.REPOSITORY_UPDATE,
                EffectStatus.APPLIED,
                effect_value=observed.attempt.to_primitive(),
                external_object_id=observed.attempt.external_object_id,
            )
            return RepositoryEffect(
                receipt,
                AttemptEffectDisposition.APPLIED,
                value=observed.attempt,
            )
        if not observed.exists and observed.exact:
            receipt = self._receipt(
                command.identity,
                EffectOperation.REPOSITORY_UPDATE,
                EffectStatus.ABSENT,
            )
            return RepositoryEffect(
                receipt,
                AttemptEffectDisposition.ABSENT,
                reason_code=RuntimeReasonCode.GIT_STATE_CONFLICT,
                details=observed.details or ("attempt_absent",),
            )
        receipt = self._receipt(
            command.identity,
            EffectOperation.REPOSITORY_UPDATE,
            EffectStatus.UNKNOWN,
            details=observed.details,
        )
        return RepositoryEffect(
            receipt,
            AttemptEffectDisposition.UNKNOWN,
            reason_code=RuntimeReasonCode.EXTERNAL_OUTCOME_UNKNOWN,
            details=observed.details or ("attempt_identity_ambiguous",),
        )

    def _registered_worktree_paths(self) -> tuple[Path, ...]:
        raw = _run_git(
            self.repository,
            ("worktree", "list", "--porcelain", "-z"),
        )
        paths: list[Path] = []
        for field in raw.split(b"\0"):
            if not field.startswith(b"worktree "):
                continue
            try:
                value = field[len(b"worktree ") :].decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise GitBoundaryError("git_path_invalid_utf8") from exc
            paths.append(Path(value).absolute())
        return tuple(paths)

    def _inspect_attempt(self, command: AttemptWorktreeCommand) -> _AttemptObservation:
        destination = self.attempts_root / command.directory_name
        normalized_destination = os.path.normcase(str(destination.absolute()))
        try:
            registered = {
                os.path.normcase(str(path.absolute()))
                for path in self._registered_worktree_paths()
            }
        except GitBoundaryError as exc:
            return _AttemptObservation(
                destination.exists(),
                False,
                None,
                None,
                False,
                (exc.code,),
            )
        try:
            destination_stat = os.lstat(destination)
        except FileNotFoundError:
            if normalized_destination in registered:
                return _AttemptObservation(
                    False,
                    False,
                    None,
                    None,
                    False,
                    ("registered_worktree_path_missing",),
                )
            return _AttemptObservation(False, True, None, None, True, ("attempt_absent",))
        except OSError as exc:
            return _AttemptObservation(
                True,
                False,
                None,
                None,
                False,
                (f"attempt_lstat_failed:{type(exc).__name__}",),
            )
        if _is_reparse(destination_stat) or not stat.S_ISDIR(destination_stat.st_mode):
            return _AttemptObservation(
                True,
                False,
                None,
                None,
                False,
                ("attempt_path_replaced",),
            )
        try:
            resolved = destination.resolve(strict=True)
            resolved.relative_to(self.attempts_root)
            root_identity = capture_filesystem_identity(destination)
            common_path = Path(
                _git_text(
                    destination,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                )
            )
            git_path = Path(
                _git_text(
                    destination,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-dir",
                )
            )
            common_identity = capture_filesystem_identity(common_path)
            git_identity = capture_filesystem_identity(git_path)
            repository_id = _sha256_ref(common_identity.to_primitive())
            head = _git_text(destination, "rev-parse", "--verify", "HEAD^{commit}")
            base_tree = _git_text(
                destination,
                "rev-parse",
                "--verify",
                f"{command.base_commit_sha}^{{tree}}",
            )
            merge_base = _git_text(
                destination,
                "merge-base",
                command.base_commit_sha,
                head,
            )
            status = _run_git(
                destination,
                (
                    "status",
                    "--porcelain=v2",
                    "-z",
                    "--untracked-files=all",
                    "--ignored=matching",
                ),
            )
            exact = (
                normalized_destination in registered
                and repository_id == command.local_repository_id
                and base_tree == command.base_tree_sha
                and merge_base == command.base_commit_sha
                and not root_identity.is_link_or_reparse_point
                and not git_identity.is_link_or_reparse_point
            )
            attempt = AttemptWorktree(
                identity=command.identity,
                path=str(resolved),
                external_object_id=f"attempt-{hashlib.sha256(command.operation_id.encode('ascii')).hexdigest()[:24]}",
                local_repository_id=repository_id,
                target_workspace_hash=command.target_workspace_hash,
                worktree_root=root_identity,
                git_dir=git_identity,
                base_commit_sha=command.base_commit_sha,
                base_tree_sha=command.base_tree_sha,
                owned_paths=command.owned_paths,
                allowed_auxiliary_paths=command.allowed_auxiliary_paths,
                protected_paths=command.protected_paths,
                path_case_mode=command.path_case_mode,
            )
            details = () if exact else ("attempt_identity_mismatch",)
            return _AttemptObservation(
                True,
                exact,
                attempt if exact else None,
                head,
                not status,
                details,
            )
        except (GitBoundaryError, GitIdentityError, OSError, ValueError) as exc:
            code = exc.code if isinstance(exc, GitBoundaryError) else type(exc).__name__
            return _AttemptObservation(True, False, None, None, False, (code,))

    def _tree_entries(
        self,
        treeish: str,
        path_case_mode: PathCaseMode,
    ) -> dict[str, GitTreeEntry]:
        if type(path_case_mode) is not PathCaseMode:
            raise TypeError("path_case_mode must be a PathCaseMode")
        raw = _run_git(
            self.repository,
            ("ls-tree", "-r", "-z", "--full-tree", treeish),
        )
        records = tuple(record for record in raw.split(b"\0") if record)
        if len(records) > MAX_TREE_ENTRIES:
            raise GitBoundaryError(
                "git_tree_entry_limit",
                reason_code=RuntimeReasonCode.LIMIT_EXCEEDED,
            )
        entries: dict[str, GitTreeEntry] = {}
        collisions: dict[str, str] = {}
        total = 0
        for record in records:
            try:
                metadata, encoded_path = record.split(b"\t", 1)
                mode_bytes, object_type, oid_bytes = metadata.split(b" ", 2)
                mode = mode_bytes.decode("ascii", errors="strict")
                oid = oid_bytes.decode("ascii", errors="strict")
                path = encoded_path.decode("utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError) as exc:
                raise GitBoundaryError("git_tree_record_invalid") from exc
            _validate_portable_path(path)
            if object_type != b"blob" or mode not in _ALLOWED_MODES:
                raise GitBoundaryError("git_tree_mode_or_type_rejected", f"{mode}:{path}")
            collision_key = (
                path if path_case_mode is PathCaseMode.SENSITIVE else path.casefold()
            )
            prior = collisions.get(collision_key)
            if prior is not None and prior != path:
                raise GitBoundaryError("git_path_collision", f"{prior}:{path}")
            collisions[collision_key] = path
            if path in entries:
                raise GitBoundaryError("git_duplicate_path", path)
            size_text = _git_text(self.repository, "cat-file", "-s", oid)
            try:
                size = int(size_text)
            except ValueError as exc:
                raise GitBoundaryError("git_blob_size_invalid", path) from exc
            if not 0 <= size <= MAX_BLOB_BYTES:
                raise GitBoundaryError(
                    "git_blob_size_limit",
                    path,
                    reason_code=RuntimeReasonCode.LIMIT_EXCEEDED,
                )
            total += size
            if total > MAX_TREE_BYTES:
                raise GitBoundaryError(
                    "git_tree_size_limit",
                    reason_code=RuntimeReasonCode.LIMIT_EXCEEDED,
                )
            entries[path] = GitTreeEntry(path, mode, oid, size)
        return entries

    def _verify_result_manifest(self, manifest: AttemptResultManifest) -> None:
        if type(manifest) is not AttemptResultManifest:
            raise TypeError("manifest must be an AttemptResultManifest")
        if manifest.local_repository_id != self._expected_workspace.local_repository_id:
            raise GitBoundaryError("result_repository_mismatch")
        if manifest.portable_profile_hash != PORTABLE_GIT_PROFILE_HASH:
            raise GitBoundaryError("portable_profile_drift")
        base_tree = _git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{manifest.base_commit_sha}^{{tree}}",
        )
        result_tree = _git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{manifest.result_commit_sha}^{{tree}}",
        )
        parents = _git_text(
            self.repository,
            "rev-list",
            "--parents",
            "-n",
            "1",
            manifest.result_commit_sha,
        ).split()
        if base_tree != manifest.base_tree_sha:
            raise GitBoundaryError("base_tree_mismatch")
        if result_tree != manifest.result_tree_sha:
            raise GitBoundaryError("result_tree_mismatch")
        if parents != [manifest.result_commit_sha, manifest.base_commit_sha]:
            raise GitBoundaryError("result_parent_mismatch", " ".join(parents))

        base_entries = self._tree_entries(
            manifest.base_commit_sha,
            manifest.path_case_mode,
        )
        result_entries = self._tree_entries(
            manifest.result_commit_sha,
            manifest.path_case_mode,
        )
        actual_changes = tuple(
            ChangedPath(path, base_entries.get(path), result_entries.get(path))
            for path in sorted(set(base_entries) | set(result_entries))
            if base_entries.get(path) != result_entries.get(path)
        )
        if actual_changes != manifest.changed_paths:
            raise GitBoundaryError("result_manifest_change_mismatch")
        total_blob_bytes = sum(
            change.result.byte_length
            for change in actual_changes
            if change.result is not None
        )
        if total_blob_bytes != manifest.total_blob_bytes:
            raise GitBoundaryError("result_manifest_size_mismatch")

    def _attempt_current_state(
        self,
        attempt: AttemptWorktree,
    ) -> tuple[str, bool, tuple[str, ...]]:
        details: list[str] = []
        path = Path(attempt.path)
        try:
            path.resolve(strict=True).relative_to(self.attempts_root)
            root_identity = capture_filesystem_identity(path)
            git_path = Path(
                _git_text(
                    path,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-dir",
                )
            )
            common_path = Path(
                _git_text(
                    path,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                )
            )
            git_identity = capture_filesystem_identity(git_path)
            common_identity = capture_filesystem_identity(common_path)
            repository_id = _sha256_ref(common_identity.to_primitive())
            if root_identity != attempt.worktree_root:
                details.append("worktree_root_identity")
            if git_identity != attempt.git_dir:
                details.append("git_dir_identity")
            if repository_id != attempt.local_repository_id:
                details.append("local_repository_id")
            head = _git_text(path, "rev-parse", "--verify", "HEAD^{commit}")
            status = _run_git(
                path,
                (
                    "status",
                    "--porcelain=v2",
                    "-z",
                    "--untracked-files=all",
                    "--ignored=matching",
                ),
            )
            return head, not status, tuple(details)
        except (GitBoundaryError, GitIdentityError, OSError, ValueError) as exc:
            code = exc.code if isinstance(exc, GitBoundaryError) else type(exc).__name__
            return attempt.base_commit_sha, False, (code,)

    def _validate_materialized_result(
        self,
        attempt: AttemptWorktree,
        changes: tuple[ChangedPath, ...],
    ) -> None:
        root = Path(attempt.path).resolve(strict=True)
        count = 0
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            relative_current = current_path.relative_to(root)
            for name in tuple(directories) + tuple(files):
                if relative_current == Path(".") and name == ".git":
                    continue
                candidate = current_path / name
                try:
                    candidate_stat = os.lstat(candidate)
                except OSError as exc:
                    raise GitBoundaryError("worktree_scan_race", str(candidate)) from exc
                count += 1
                if count > MAX_TREE_ENTRIES * 2:
                    raise GitBoundaryError(
                        "worktree_path_limit",
                        reason_code=RuntimeReasonCode.LIMIT_EXCEEDED,
                    )
                if _is_reparse(candidate_stat):
                    raise GitBoundaryError("worktree_link_or_reparse", str(candidate))
                if not (
                    stat.S_ISREG(candidate_stat.st_mode)
                    or stat.S_ISDIR(candidate_stat.st_mode)
                ):
                    raise GitBoundaryError("worktree_special_file", str(candidate))
        for change in changes:
            candidate = root / Path(change.path)
            parent = candidate.parent.resolve(strict=True)
            try:
                parent.relative_to(root)
            except ValueError as exc:
                raise GitBoundaryError("git_path_escape", change.path) from exc
            if change.result is None:
                continue
            candidate_stat = os.lstat(candidate)
            if _is_reparse(candidate_stat) or not stat.S_ISREG(candidate_stat.st_mode):
                raise GitBoundaryError("worktree_result_not_regular", change.path)
            if candidate_stat.st_nlink > 1:
                raise GitBoundaryError("worktree_hardlink_rejected", change.path)

    def validate_result(
        self,
        attempt: AttemptWorktree,
        *,
        process_tree_terminated: bool,
    ) -> ResultValidation:
        if type(attempt) is not AttemptWorktree:
            raise TypeError("attempt must be an AttemptWorktree")
        if type(process_tree_terminated) is not bool:
            raise TypeError("process_tree_terminated must be a bool")
        if not process_tree_terminated:
            return self._rejected_validation(
                RuntimeReasonCode.PROCESS_CONTAINMENT_UNKNOWN,
                ("process_tree_not_proven_terminated",),
            )
        try:
            self._guard_target()
            head, clean, identity_details = self._attempt_current_state(attempt)
            if identity_details:
                raise GitBoundaryError(
                    "attempt_identity_changed",
                    ",".join(identity_details),
                )
            parents = _git_text(
                Path(attempt.path),
                "rev-list",
                "--parents",
                "-n",
                "1",
                head,
            ).split()
            if len(parents) != 2 or parents[0] != head or parents[1] != attempt.base_commit_sha:
                raise GitBoundaryError("result_parent_mismatch", " ".join(parents))
            base_tree_sha = _git_text(
                self.repository,
                "rev-parse",
                "--verify",
                f"{attempt.base_commit_sha}^{{tree}}",
            )
            if base_tree_sha != attempt.base_tree_sha:
                raise GitBoundaryError("base_tree_mismatch")
            result_tree_sha = _git_text(
                self.repository,
                "rev-parse",
                "--verify",
                f"{head}^{{tree}}",
            )
            base_entries = self._tree_entries(
                attempt.base_commit_sha,
                attempt.path_case_mode,
            )
            result_entries = self._tree_entries(head, attempt.path_case_mode)
            if not clean:
                raise GitBoundaryError("attempt_worktree_dirty")
            changed = tuple(
                ChangedPath(path, base_entries.get(path), result_entries.get(path))
                for path in sorted(set(base_entries) | set(result_entries))
                if base_entries.get(path) != result_entries.get(path)
            )
            if not changed:
                raise GitBoundaryError("result_has_no_changes")
            writable = attempt.owned_paths + attempt.allowed_auxiliary_paths
            violations: list[str] = []
            for item in changed:
                if any(
                    _pattern_matches(pattern, item.path, attempt.path_case_mode)
                    for pattern in attempt.protected_paths
                ):
                    violations.append(f"protected_path:{item.path}")
                if not any(
                    _pattern_matches(pattern, item.path, attempt.path_case_mode)
                    for pattern in writable
                ):
                    violations.append(f"unowned_path:{item.path}")
            if violations:
                raise GitBoundaryError("result_path_ownership", ",".join(violations))
            self._validate_materialized_result(attempt, changed)
            final_head, final_clean, final_details = self._attempt_current_state(attempt)
            if final_head != head or not final_clean or final_details:
                raise GitBoundaryError("result_validation_race")
            self._guard_target()
            total = sum(
                item.result.byte_length
                for item in changed
                if item.result is not None
            )
            manifest = AttemptResultManifest(
                schema_version=1,
                identity=attempt.identity,
                local_repository_id=attempt.local_repository_id,
                attempt_hash=attempt.attempt_hash,
                base_commit_sha=attempt.base_commit_sha,
                base_tree_sha=attempt.base_tree_sha,
                result_commit_sha=head,
                result_tree_sha=result_tree_sha,
                path_case_mode=attempt.path_case_mode,
                changed_paths=changed,
                total_blob_bytes=total,
                portable_profile_hash=PORTABLE_GIT_PROFILE_HASH,
            )
            return ResultValidation(
                True,
                manifest,
                None,
                (),
                _sha256_ref(
                    {
                        "accepted": True,
                        "manifest_hash": manifest.manifest_hash,
                    }
                ),
            )
        except GitBoundaryError as exc:
            return self._rejected_validation(
                exc.reason_code,
                tuple(item for item in (exc.code, exc.detail) if item),
            )
        except (GitIdentityError, OSError, ValueError) as exc:
            return self._rejected_validation(
                RuntimeReasonCode.GIT_STATE_CONFLICT,
                (type(exc).__name__,),
            )

    @staticmethod
    def _rejected_validation(
        reason_code: RuntimeReasonCode,
        violations: tuple[str, ...],
    ) -> ResultValidation:
        evidence = {
            "accepted": False,
            "reason_code": reason_code.value,
            "violations": list(violations),
        }
        return ResultValidation(
            False,
            None,
            reason_code,
            violations,
            _sha256_ref(evidence),
        )

    def plan_stage(
        self,
        validation: ResultValidation,
        *,
        operation_id: str,
    ) -> StageResultCommand:
        if type(validation) is not ResultValidation or not validation.accepted:
            raise ValueError("only an accepted validation can be staged")
        assert validation.manifest is not None
        manifest = validation.manifest
        suffix = manifest.manifest_hash[7:31]
        assert manifest.identity.task_id is not None
        staged_ref = (
            "refs/wish-builder/staged/"
            f"{manifest.identity.run_id.lower()}/"
            f"{manifest.identity.task_id.lower()}-{manifest.identity.attempt}-{suffix}"
        )
        return StageResultCommand(
            operation_id=operation_id,
            identity=manifest.identity,
            local_repository_id=manifest.local_repository_id,
            target_workspace_hash=self._expected_workspace.workspace_hash,
            result_manifest_hash=manifest.manifest_hash,
            result_commit_sha=manifest.result_commit_sha,
            result_tree_sha=manifest.result_tree_sha,
            staged_ref=staged_ref,
        )

    def stage_result(
        self,
        effect: PreparedEffect[StageResultCommand],
        validation: ResultValidation,
    ) -> RepositoryEffect[StagedResult]:
        command = self._validate_stage_effect(effect, validation)
        with self._mutation_lock.acquire():
            self._guard_target()
            assert validation.manifest is not None
            self._verify_result_manifest(validation.manifest)
            existing = self._stage_observation(command, validation)
            if existing.disposition is not AttemptEffectDisposition.ABSENT:
                return existing
            self._guard_target()
            self._trigger("before_result_stage", self.repository)
            try:
                _run_git(
                    self.repository,
                    (
                        "update-ref",
                        command.staged_ref,
                        command.result_commit_sha,
                        self._zero_oid(),
                    ),
                )
                self._trigger("after_result_stage", self.repository)
            except GitWorktreeCrash:
                raise
            except GitBoundaryError:
                return self._stage_observation(command, validation)
            return self._stage_observation(command, validation)

    def inspect_stage(
        self,
        command: StageResultCommand,
        validation: ResultValidation,
    ) -> RepositoryEffect[StagedResult]:
        self._validate_stage_binding(command, validation)
        with self._mutation_lock.acquire():
            self._guard_target_structure()
            assert validation.manifest is not None
            self._verify_result_manifest(validation.manifest)
            return self._stage_observation(command, validation)

    def _validate_stage_binding(
        self,
        command: StageResultCommand,
        validation: ResultValidation,
    ) -> AttemptResultManifest:
        if type(command) is not StageResultCommand:
            raise TypeError("command must be a StageResultCommand")
        if type(validation) is not ResultValidation or not validation.accepted:
            raise ValueError("only an accepted validation can be staged")
        assert validation.manifest is not None
        manifest = validation.manifest
        if (
            command.identity != manifest.identity
            or command.result_manifest_hash != manifest.manifest_hash
            or command.result_commit_sha != manifest.result_commit_sha
            or command.result_tree_sha != manifest.result_tree_sha
            or command.local_repository_id != manifest.local_repository_id
        ):
            raise ValueError("stage command does not bind the validated result")
        return manifest

    def _validate_stage_effect(
        self,
        effect: PreparedEffect[StageResultCommand],
        validation: ResultValidation,
    ) -> StageResultCommand:
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        command = effect.command
        if type(command) is not StageResultCommand:
            raise TypeError("prepared command must be a StageResultCommand")
        self._validate_stage_binding(command, validation)
        payload = effect.request.payload
        identity = effect.request.identity
        if effect.request.event.event_type is not JournalEventType.EFFECT_REQUESTED:
            raise ValueError("result staging requires effect_requested")
        if (
            payload.operation is not EffectOperation.RESULT_STAGE
            or payload.adapter is not AdapterKind.GIT
            or payload.object_type is not EffectObjectType.RESULT_BUNDLE
        ):
            raise ValueError("stage request has the wrong effect boundary")
        if payload.normalized_target_hash != command.target_workspace_hash:
            raise ValueError("stage request target hash does not match")
        if (
            command.local_repository_id
            != self._expected_workspace.local_repository_id
            or command.target_workspace_hash != self._expected_workspace.workspace_hash
        ):
            raise ValueError("stage command is stale for this repository")
        source_identity = command.identity
        if (
            identity.run_id != source_identity.run_id
            or identity.coordinator_epoch != source_identity.coordinator_epoch
            or identity.task_id != source_identity.task_id
            or identity.attempt != source_identity.attempt
            or identity.correlation_id != command.operation_id
        ):
            raise ValueError("stage request identity does not match")
        effect.command_hash
        return command

    def _stage_observation(
        self,
        command: StageResultCommand,
        validation: ResultValidation,
    ) -> RepositoryEffect[StagedResult]:
        assert validation.manifest is not None
        raw = _run_git(
            self.repository,
            ("show-ref", "--verify", "--hash", command.staged_ref),
            check=False,
        )
        try:
            observed = raw.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError:
            observed = "invalid"
        if not observed:
            receipt = self._receipt(
                command.identity,
                EffectOperation.RESULT_STAGE,
                EffectStatus.ABSENT,
            )
            return RepositoryEffect(
                receipt,
                AttemptEffectDisposition.ABSENT,
                reason_code=RuntimeReasonCode.GIT_STATE_CONFLICT,
                details=("staged_ref_absent",),
            )
        if observed == command.result_commit_sha:
            staged = StagedResult(
                validation.manifest,
                command.staged_ref,
                _sha256_ref(command.to_primitive()),
            )
            receipt = self._receipt(
                command.identity,
                EffectOperation.RESULT_STAGE,
                EffectStatus.APPLIED,
                effect_value={
                    "manifest_hash": staged.result_manifest_hash,
                    "ref": staged.staged_ref,
                    "source": staged.result_commit_sha,
                },
                external_object_id=f"stage-{command.result_manifest_hash[7:31]}",
            )
            return RepositoryEffect(
                receipt,
                AttemptEffectDisposition.APPLIED,
                value=staged,
            )
        receipt = self._receipt(
            command.identity,
            EffectOperation.RESULT_STAGE,
            EffectStatus.UNKNOWN,
            details=("staged_ref_conflict", observed),
        )
        return RepositoryEffect(
            receipt,
            AttemptEffectDisposition.UNKNOWN,
            reason_code=RuntimeReasonCode.EXTERNAL_OUTCOME_UNKNOWN,
            details=("staged_ref_conflict", observed),
        )

    def prepare_promotion(
        self,
        source: StagedResultView,
        *,
        expected_target_sha: str,
        topological_position: int,
        operation_id: str,
        coordinator_epoch: int,
    ) -> PromotionPlan:
        if type(source) is not StagedResult:
            raise TypeError("source must be a StagedResult")
        _git_oid(expected_target_sha, "expected_target_sha")
        _nonempty(operation_id, "operation_id")
        if type(coordinator_epoch) is not int or coordinator_epoch <= 0:
            raise ValueError("coordinator_epoch must be positive")
        with self._mutation_lock.acquire():
            self._guard_target()
            if self._expected_workspace.base_commit_sha != expected_target_sha:
                raise GitBoundaryError(
                    "expected_target_stale",
                    reason_code=RuntimeReasonCode.MERGE_CANDIDATE_STALE,
                )
            self._verify_staged_source(source)
            candidate_tree = self._build_candidate_tree(source, expected_target_sha)
            message = self._promotion_message(source, topological_position)
            candidate_commit = _git_text_with_input(
                self.repository,
                (
                    "commit-tree",
                    candidate_tree,
                    "-p",
                    expected_target_sha,
                ),
                message,
                environment=_git_environment(commit_identity=True),
            )
            command = PromotionCommand(
                operation_id=operation_id,
                run_id=source.run_id,
                coordinator_epoch=coordinator_epoch,
                task_id=source.task_id,
                attempt=source.attempt,
                topological_position=topological_position,
                local_repository_id=source.local_repository_id,
                target_workspace_hash=self._expected_workspace.workspace_hash,
                expected_target_sha=expected_target_sha,
                staged_ref=source.staged_ref,
                result_manifest_hash=source.result_manifest_hash,
                source_commit_sha=source.result_commit_sha,
                source_tree_sha=source.result_tree_sha,
                candidate_commit_sha=candidate_commit,
                candidate_tree_sha=candidate_tree,
            )
            plan = PromotionPlan(command, source)
            self._verify_promotion_plan(plan)
            return plan

    @contextmanager
    def materialize_promotion_candidate(
        self,
        plan: PromotionPlan,
    ) -> Iterator[Path]:
        if type(plan) is not PromotionPlan:
            raise TypeError("plan must be a PromotionPlan")
        if plan.command.acceptance_bound:
            raise ValueError("acceptance candidate requires an unbound plan")
        raw_path = tempfile.mkdtemp(prefix="acceptance-", dir=self.attempts_root)
        candidate = Path(raw_path)
        candidate.rmdir()
        try:
            with self._mutation_lock.acquire():
                self._guard_attempt_root()
                self._guard_target()
                self._verify_staged_source(plan.source)
                self._verify_promotion_plan(plan)
                _run_git(
                    self.repository,
                    (
                        "worktree",
                        "add",
                        "--detach",
                        candidate.as_posix(),
                        plan.command.candidate_commit_sha,
                    ),
                )
                if (
                    _git_text(candidate, "rev-parse", "HEAD")
                    != plan.command.candidate_commit_sha
                    or _git_text(candidate, "rev-parse", "HEAD^{tree}")
                    != plan.command.candidate_tree_sha
                    or _run_git(
                        candidate,
                        (
                            "status",
                            "--porcelain=v2",
                            "-z",
                            "--untracked-files=all",
                            "--ignored=no",
                        ),
                    )
                ):
                    raise GitBoundaryError("acceptance_candidate_invalid")
                self._guard_target()
            yield candidate
        finally:
            with self._mutation_lock.acquire():
                normalized_candidate = os.path.normcase(str(candidate.absolute()))
                try:
                    registered = normalized_candidate in {
                        os.path.normcase(str(path.absolute()))
                        for path in self._registered_worktree_paths()
                    }
                except GitBoundaryError as exc:
                    raise GitBoundaryError(
                        "acceptance_candidate_cleanup_incomplete",
                        exc.code,
                    ) from exc
                if registered:
                    try:
                        _run_git(
                            self.repository,
                            (
                                "worktree",
                                "remove",
                                "--force",
                                candidate.as_posix(),
                            ),
                        )
                        _run_git(
                            self.repository,
                            ("worktree", "prune", "--expire", "now"),
                        )
                    except GitBoundaryError as exc:
                        raise GitBoundaryError(
                            "acceptance_candidate_cleanup_incomplete",
                            exc.code,
                        ) from exc
                if candidate.exists():
                    raise GitBoundaryError("acceptance_candidate_cleanup_incomplete")

    def apply_promotion(
        self,
        effect: PreparedEffect[PromotionCommand],
        plan: PromotionPlan,
    ) -> PromotionObservation:
        self._validate_promotion_effect(effect, plan)
        with self._mutation_lock.acquire():
            observed = self._inspect_promotion_locked(plan)
            if observed.disposition is not PromotionDisposition.ABSENT:
                return observed
            try:
                self._guard_target()
                self._verify_staged_source(plan.source)
                self._verify_promotion_plan(plan)
                self._guard_target()
                self._trigger("before_target_promotion", self.repository)
                _run_git(
                    self.repository,
                    (
                        "merge",
                        "--ff-only",
                        "--no-edit",
                        plan.command.candidate_commit_sha,
                    ),
                    environment=_git_environment(),
                )
                self._trigger("after_target_promotion", self.repository)
            except GitWorktreeCrash:
                raise
            except GitBoundaryError:
                return self._inspect_promotion_locked(plan)
            return self._inspect_promotion_locked(plan)

    def inspect_promotion(self, plan: PromotionPlan) -> PromotionObservation:
        if type(plan) is not PromotionPlan:
            raise TypeError("plan must be a PromotionPlan")
        with self._mutation_lock.acquire():
            return self._inspect_promotion_locked(plan)

    def _validate_promotion_effect(
        self,
        effect: PreparedEffect[PromotionCommand],
        plan: PromotionPlan,
    ) -> None:
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        if type(plan) is not PromotionPlan:
            raise TypeError("plan must be a PromotionPlan")
        if type(effect.command) is not PromotionCommand or effect.command != plan.command:
            raise ValueError("durable effect does not bind the promotion plan")
        payload = effect.request.payload
        identity = effect.request.identity
        command = plan.command
        if not command.acceptance_bound:
            raise ValueError("promotion requires bound acceptance evidence")
        if effect.request.event.event_type is not JournalEventType.PROMOTION_REQUESTED:
            raise ValueError("promotion requires promotion_requested")
        if (
            payload.operation is not EffectOperation.RESULT_PROMOTION
            or payload.adapter is not AdapterKind.GIT
            or payload.object_type is not EffectObjectType.GIT_REF
        ):
            raise ValueError("promotion request has the wrong effect boundary")
        if payload.normalized_target_hash != plan.command.target_workspace_hash:
            raise ValueError("promotion request target hash does not match")
        if command.local_repository_id != self._expected_workspace.local_repository_id:
            raise ValueError("promotion repository identity does not match")
        if (
            identity.run_id != command.run_id
            or identity.coordinator_epoch != command.coordinator_epoch
            or identity.task_id != command.task_id
            or identity.attempt != command.attempt
            or identity.correlation_id != command.operation_id
        ):
            raise ValueError("promotion request identity does not match its command")
        effect.command_hash

    def _verify_staged_source(self, source: StagedResultView) -> None:
        if type(source) is not StagedResult:
            raise TypeError("source must be a StagedResult")
        if source.local_repository_id != self._expected_workspace.local_repository_id:
            raise GitBoundaryError("staged_repository_mismatch")
        self._verify_result_manifest(source.manifest)
        observed = _run_git(
            self.repository,
            ("show-ref", "--verify", "--hash", source.staged_ref),
            check=False,
        ).decode("ascii", errors="replace").strip()
        if observed != source.result_commit_sha:
            raise GitBoundaryError("staged_ref_drift", observed)
        tree = _git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{source.result_commit_sha}^{{tree}}",
        )
        if tree != source.result_tree_sha:
            raise GitBoundaryError("staged_tree_drift")

    def _build_candidate_tree(
        self,
        source: StagedResult,
        expected_target_sha: str,
    ) -> str:
        self._guard_attempt_root()
        current = self._tree_entries(
            expected_target_sha,
            source.manifest.path_case_mode,
        )
        for change in source.manifest.changed_paths:
            if current.get(change.path) != change.base:
                raise GitBoundaryError(
                    "promotion_path_stale",
                    change.path,
                    reason_code=RuntimeReasonCode.MERGE_CANDIDATE_STALE,
                )
        descriptor, raw_index_path = tempfile.mkstemp(
            prefix="promotion-index-",
            dir=self.attempts_root,
        )
        os.close(descriptor)
        index_path = Path(raw_index_path)
        index_path.unlink()
        environment = _git_environment(index_file=index_path)
        try:
            _run_git(
                self.repository,
                ("read-tree", expected_target_sha),
                environment=environment,
            )
            for change in source.manifest.changed_paths:
                if change.result is None:
                    _run_git(
                        self.repository,
                        ("update-index", "--force-remove", "--", change.path),
                        environment=environment,
                    )
                else:
                    _run_git(
                        self.repository,
                        (
                            "update-index",
                            "--add",
                            "--cacheinfo",
                            f"{change.result.mode},{change.result.object_id},{change.path}",
                        ),
                        environment=environment,
                    )
            return _git_text_environment(
                self.repository,
                ("write-tree",),
                environment,
            )
        finally:
            for candidate in (index_path, Path(str(index_path) + ".lock")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _promotion_message(
        source: StagedResult,
        topological_position: int,
    ) -> bytes:
        text = (
            f"wish-builder promote {source.task_id}\n\n"
            f"Topological-Position: {topological_position}\n"
            f"Source-Commit: {source.result_commit_sha}\n"
            f"Result-Manifest: {source.result_manifest_hash}\n"
        )
        return text.encode("utf-8")

    def _verify_promotion_plan(self, plan: PromotionPlan) -> None:
        command = plan.command
        expected_tree = self._build_candidate_tree(
            plan.source,
            command.expected_target_sha,
        )
        if expected_tree != command.candidate_tree_sha:
            raise GitBoundaryError("promotion_candidate_tree_mismatch")
        expected_commit = _git_text_with_input(
            self.repository,
            (
                "commit-tree",
                expected_tree,
                "-p",
                command.expected_target_sha,
            ),
            self._promotion_message(plan.source, command.topological_position),
            environment=_git_environment(commit_identity=True),
        )
        if expected_commit != command.candidate_commit_sha:
            raise GitBoundaryError("promotion_candidate_commit_mismatch")
        tree = _git_text(
            self.repository,
            "rev-parse",
            "--verify",
            f"{command.candidate_commit_sha}^{{tree}}",
        )
        parents = _git_text(
            self.repository,
            "rev-list",
            "--parents",
            "-n",
            "1",
            command.candidate_commit_sha,
        ).split()
        if tree != command.candidate_tree_sha or parents != [
            command.candidate_commit_sha,
            command.expected_target_sha,
        ]:
            raise GitBoundaryError("promotion_candidate_invalid")

    def _inspect_promotion_locked(self, plan: PromotionPlan) -> PromotionObservation:
        command = plan.command
        identity = ExecutionIdentity(
            command.run_id,
            command.coordinator_epoch,
            command.task_id,
            command.attempt,
            command.operation_id,
        )
        try:
            observed_workspace = self._guard_target_structure()
            self._verify_staged_source(plan.source)
            self._verify_promotion_plan(plan)
            head = observed_workspace.base_commit_sha
            if head == command.expected_target_sha:
                receipt = self._receipt(
                    identity,
                    EffectOperation.RESULT_PROMOTION,
                    EffectStatus.ABSENT,
                    evidence=command.acceptance_evidence,
                )
                return PromotionObservation(
                    receipt,
                    PromotionDisposition.ABSENT,
                    reason_code=RuntimeReasonCode.MERGE_CANDIDATE_STALE,
                    details=("candidate_not_promoted",),
                )
            merge_base = _git_text(
                self.repository,
                "merge-base",
                command.candidate_commit_sha,
                head,
            )
            if merge_base == command.candidate_commit_sha:
                target_status = _run_git(
                    self.repository,
                    (
                        "status",
                        "--porcelain=v2",
                        "-z",
                        "--untracked-files=all",
                        "--ignored=no",
                        "--",
                        *observed_workspace.scopes,
                    ),
                )
                if target_status:
                    raise GitBoundaryError(
                        "promoted_target_worktree_dirty",
                        reason_code=RuntimeReasonCode.WORKSPACE_DRIFT,
                    )
                record = plan.candidate_record()
                self._expected_workspace = observed_workspace
                receipt = self._receipt(
                    identity,
                    EffectOperation.RESULT_PROMOTION,
                    EffectStatus.APPLIED,
                    effect_value=record.to_primitive(),
                    external_object_id=command.candidate_commit_sha,
                    evidence=record.acceptance_evidence,
                )
                return PromotionObservation(
                    receipt,
                    PromotionDisposition.APPLIED,
                    record=record,
                )
            details = ("target_ref_drift", head)
        except (GitBoundaryError, GitIdentityError, OSError, ValueError) as exc:
            details = (
                exc.code if isinstance(exc, GitBoundaryError) else type(exc).__name__,
            )
        receipt = self._receipt(
            identity,
            EffectOperation.RESULT_PROMOTION,
            EffectStatus.UNKNOWN,
            details=details,
            evidence=command.acceptance_evidence,
        )
        return PromotionObservation(
            receipt,
            PromotionDisposition.UNKNOWN,
            reason_code=RuntimeReasonCode.EXTERNAL_OUTCOME_UNKNOWN,
            details=details,
        )

    def inspect_cleanup(self, candidate: CleanupCandidate) -> CleanupInspection:
        if type(candidate) is not CleanupCandidate:
            raise TypeError("candidate must be a CleanupCandidate")
        if type(candidate.attempt) is not AttemptWorktree:
            raise TypeError("cleanup candidate must carry an AttemptWorktree")
        attempt = candidate.attempt
        path = Path(attempt.path)
        target_workspace_hash = self._expected_workspace.workspace_hash
        registered_detail: tuple[str, ...] = ()
        try:
            registered = {
                os.path.normcase(str(item.absolute()))
                for item in self._registered_worktree_paths()
            }
        except GitBoundaryError as exc:
            state = {
                "clean": False,
                "details": [exc.code],
                "exists": path.exists(),
                "head": None,
                "identity_ok": False,
                "target_workspace_hash": target_workspace_hash,
            }
            return CleanupInspection(
                exists=path.exists(),
                identity_ok=False,
                clean=False,
                observed_head_sha=None,
                target_workspace_hash=target_workspace_hash,
                state_hash=_sha256_ref(state),
                details=(exc.code,),
            )
        normalized = os.path.normcase(str(path.absolute()))
        try:
            os.lstat(path)
        except FileNotFoundError:
            if normalized in registered:
                registered_detail = ("registered_worktree_path_missing",)
            state = {
                "clean": not registered_detail,
                "details": list(registered_detail),
                "exists": False,
                "head": None,
                "identity_ok": not registered_detail,
                "target_workspace_hash": target_workspace_hash,
            }
            return CleanupInspection(
                exists=False,
                identity_ok=not registered_detail,
                clean=not registered_detail,
                observed_head_sha=None,
                target_workspace_hash=target_workspace_hash,
                state_hash=_sha256_ref(state),
                details=registered_detail,
            )
        except OSError as exc:
            details = (f"attempt_lstat_failed:{type(exc).__name__}",)
            state = {
                "clean": False,
                "details": list(details),
                "exists": True,
                "head": None,
                "identity_ok": False,
                "target_workspace_hash": target_workspace_hash,
            }
            return CleanupInspection(
                exists=True,
                identity_ok=False,
                clean=False,
                observed_head_sha=None,
                target_workspace_hash=target_workspace_hash,
                state_hash=_sha256_ref(state),
                details=details,
            )
        head, clean, details = self._attempt_current_state(attempt)
        identity_ok = not details and normalized in registered
        if normalized not in registered:
            details = (*details, "worktree_registration_missing")
        state = {
            "clean": clean,
            "details": list(details),
            "exists": True,
            "head": head,
            "identity_ok": identity_ok,
            "target_workspace_hash": target_workspace_hash,
            "worktree_identity": attempt.attempt_hash,
        }
        return CleanupInspection(
            exists=True,
            identity_ok=identity_ok,
            clean=clean,
            observed_head_sha=head,
            target_workspace_hash=target_workspace_hash,
            state_hash=_sha256_ref(state),
            details=details,
        )

    def apply_cleanup(
        self,
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
    ) -> CleanupObservation:
        self._validate_cleanup_effect(effect, plan)
        with self._mutation_lock.acquire():
            current = self.inspect_cleanup(plan.candidate)
            if not current.exists and current.identity_ok:
                return self._cleanup_applied_observation(
                    effect,
                    plan,
                    CleanupDisposition.ALREADY_ABSENT,
                )
            if (
                not plan.command.remove_allowed
                or current.state_hash != plan.command.observed_state_hash
                or not current.identity_ok
                or not current.clean
                or current.observed_head_sha != plan.command.expected_head_sha
            ):
                return self._cleanup_quarantined_observation(
                    effect,
                    plan,
                    RuntimeReasonCode.CLEANUP_INCOMPLETE,
                    current.details or ("cleanup_state_changed",),
                )
            try:
                self._guard_attempt_root()
                self._guard_target()
                self._trigger(
                    "before_attempt_remove",
                    Path(plan.candidate.attempt.path),
                )
                _run_git(
                    self.repository,
                    (
                        "worktree",
                        "remove",
                        Path(plan.candidate.attempt.path).as_posix(),
                    ),
                )
                self._trigger(
                    "after_attempt_remove",
                    Path(plan.candidate.attempt.path),
                )
            except GitWorktreeCrash:
                raise
            except GitBoundaryError:
                reconciled = self.inspect_cleanup(plan.candidate)
                if not reconciled.exists and reconciled.identity_ok:
                    return self._cleanup_applied_observation(
                        effect,
                        plan,
                        CleanupDisposition.REMOVED,
                    )
                if reconciled.exists and reconciled.identity_ok:
                    return self._cleanup_quarantined_observation(
                        effect,
                        plan,
                        RuntimeReasonCode.CLEANUP_INCOMPLETE,
                        reconciled.details or ("cleanup_not_applied",),
                    )
                return self._cleanup_unknown_observation(
                    effect,
                    plan,
                    reconciled.details or ("cleanup_outcome_unknown",),
                )
            reconciled = self.inspect_cleanup(plan.candidate)
            if not reconciled.exists and reconciled.identity_ok:
                return self._cleanup_applied_observation(
                    effect,
                    plan,
                    CleanupDisposition.REMOVED,
                )
            return self._cleanup_unknown_observation(
                effect,
                plan,
                reconciled.details or ("cleanup_outcome_unknown",),
            )

    def _validate_cleanup_effect(
        self,
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
    ) -> None:
        if type(effect) is not PreparedEffect:
            raise TypeError("effect must be a PreparedEffect")
        if type(plan) is not CleanupPlan:
            raise TypeError("plan must be a CleanupPlan")
        if type(effect.command) is not CleanupCommand or effect.command != plan.command:
            raise ValueError("durable effect does not bind the cleanup plan")
        payload = effect.request.payload
        identity = effect.request.identity
        command = plan.command
        if effect.request.event.event_type is not JournalEventType.CLEANUP_REQUESTED:
            raise ValueError("cleanup requires cleanup_requested")
        if (
            payload.operation is not EffectOperation.CLEANUP
            or payload.adapter is not AdapterKind.GIT
            or payload.object_type is not EffectObjectType.CLEANUP_ITEM
        ):
            raise ValueError("cleanup request has the wrong effect boundary")
        if payload.normalized_target_hash != command.target_workspace_hash:
            raise ValueError("cleanup request target hash does not match")
        if (
            command.local_repository_id
            != self._expected_workspace.local_repository_id
            or command.target_workspace_hash != self._expected_workspace.workspace_hash
        ):
            raise ValueError("cleanup repository identity does not match")
        if (
            identity.run_id != command.run_id
            or identity.coordinator_epoch != command.coordinator_epoch
            or identity.task_id != command.task_id
            or identity.attempt != command.attempt
            or identity.correlation_id != command.operation_id
        ):
            raise ValueError("cleanup request identity does not match its command")
        if command.remove_allowed and (
            not plan.candidate.evidence
            or not plan.candidate.reconciliation_complete
            or not plan.candidate.process_tree_terminated
            or not plan.candidate.outcome_known
            or not plan.inspection.identity_ok
            or (
                plan.inspection.exists
                and (
                    not plan.inspection.clean
                    or plan.inspection.observed_head_sha != command.expected_head_sha
                )
            )
        ):
            raise ValueError("cleanup command bypasses quarantine requirements")
        effect.command_hash

    def _cleanup_applied_observation(
        self,
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
        disposition: CleanupDisposition,
    ) -> CleanupObservation:
        receipt = self._receipt(
            effect.request.identity,
            EffectOperation.CLEANUP,
            EffectStatus.APPLIED,
            effect_value={
                "disposition": disposition.value,
                "evidence": list(plan.command.evidence_digests),
                "external_object_id": plan.command.external_object_id,
            },
            external_object_id=plan.command.external_object_id,
        )
        return CleanupObservation(
            receipt=receipt,
            disposition=disposition,
            external_object_id=plan.command.external_object_id,
            evidence=plan.candidate.evidence,
        )

    def _cleanup_quarantined_observation(
        self,
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
        reason: RuntimeReasonCode,
        details: tuple[str, ...],
    ) -> CleanupObservation:
        receipt = EffectReceipt(
            1,
            effect.request.identity,
            EffectOperation.CLEANUP,
            EffectStatus.ABSENT,
            self._clock(),
            evidence=plan.candidate.evidence,
        )
        return CleanupObservation(
            receipt=receipt,
            disposition=CleanupDisposition.QUARANTINED,
            external_object_id=plan.command.external_object_id,
            evidence=plan.candidate.evidence,
            reason_code=reason,
            details=details,
        )

    def _cleanup_unknown_observation(
        self,
        effect: PreparedEffect[CleanupCommand],
        plan: CleanupPlan,
        details: tuple[str, ...],
    ) -> CleanupObservation:
        receipt = self._receipt(
            effect.request.identity,
            EffectOperation.CLEANUP,
            EffectStatus.UNKNOWN,
            details=details,
        )
        return CleanupObservation(
            receipt=receipt,
            disposition=CleanupDisposition.UNKNOWN,
            external_object_id=plan.command.external_object_id,
            evidence=plan.candidate.evidence,
            reason_code=RuntimeReasonCode.EXTERNAL_OUTCOME_UNKNOWN,
            details=details,
        )


__all__ = [
    "AttemptEffectDisposition",
    "AttemptResultManifest",
    "AttemptWorktree",
    "AttemptWorktreeCommand",
    "ChangedPath",
    "GitBoundaryError",
    "GitMutationFailpoint",
    "GitTreeEntry",
    "GitWorktreeAdapter",
    "GitWorktreeCrash",
    "MAX_BLOB_BYTES",
    "MAX_COMPONENT_BYTES",
    "MAX_PATH_BYTES",
    "MAX_TREE_BYTES",
    "MAX_TREE_ENTRIES",
    "PORTABLE_GIT_PROFILE_HASH",
    "PORTABLE_GIT_PROFILE_VERSION",
    "RepositoryEffect",
    "ResultValidation",
    "StageResultCommand",
    "StagedResult",
]
