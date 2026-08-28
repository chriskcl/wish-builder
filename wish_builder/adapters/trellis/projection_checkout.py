"""Revalidated authoritative target for Trellis task projections."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wish_builder.adapters.git_identity import (
    WorkspaceIdentity,
    revalidate_workspace_identity,
)

_READ_CHUNK = 64 * 1024
_MAX_GIT_OUTPUT = 8 * 1024 * 1024
_MAX_GIT_STDERR = 64 * 1024


class TrellisProjectionCheckoutError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail[:1024]
        super().__init__(reason if not self.detail else f"{reason}: {self.detail}")


@dataclass(frozen=True, slots=True)
class TrellisAuthoritativeProjectionTarget:
    """One revalidated view of the repository that owns the Trellis tasks."""

    path: Path
    workspace: WorkspaceIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("path must be an absolute Path")
        if type(self.workspace) is not WorkspaceIdentity:
            raise TypeError("workspace must be a WorkspaceIdentity")
        if self.path != Path(self.workspace.worktree_root.canonical_path):
            raise ValueError("path must match the revalidated workspace root")


class TrellisAuthoritativeProjectionProvider:
    """Resolve the editable Trellis task store in its authoritative checkout.

    Projection changes Trellis task bytes by design, and result promotion can
    advance the checked-out commit. Those mutable fields are not target
    identity. Repository, worktree, and branch identities remain bound to the
    approved workspace.
    """

    def __init__(
        self,
        repository: str | os.PathLike[str],
        expected_workspace: WorkspaceIdentity,
    ) -> None:
        if type(expected_workspace) is not WorkspaceIdentity:
            raise TypeError("expected_workspace must be a WorkspaceIdentity")
        requested = Path(repository).expanduser().absolute()
        expected = Path(expected_workspace.worktree_root.canonical_path)
        try:
            repository_path = requested.resolve(strict=True)
            expected_path = expected.resolve(strict=True)
        except OSError as exc:
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_root_unavailable"
            ) from exc
        if repository_path != expected_path:
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_identity_mismatch"
            )
        if expected_workspace.worktree_root.is_link_or_reparse_point:
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_root_unsafe"
            )
        self.repository = repository_path
        self.expected_workspace = expected_workspace

    def ensure(self, run_id: str) -> TrellisAuthoritativeProjectionTarget:
        if type(run_id) is not str or not run_id or len(run_id) > 256:
            raise ValueError("run_id is invalid")
        comparison = revalidate_workspace_identity(self.expected_workspace)
        observed = comparison.observed
        if observed is None:
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_workspace_drift"
            )
        identity_mismatches = set(comparison.mismatches) - {
            "base_commit_sha",
            "index_dirty_fingerprint",
        }
        if identity_mismatches:
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_workspace_drift",
                ",".join(sorted(identity_mismatches)),
            )
        observed_root = Path(observed.worktree_root.canonical_path)
        if observed_root != self.repository:
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_workspace_drift",
                "worktree_root",
            )
        _guard_authoritative_task_store(self.repository)
        unexpected = tuple(
            path
            for path in _changed_paths(self.repository)
            if not _is_projection_task_path(path)
        )
        if unexpected:
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_workspace_dirty",
                unexpected[0],
            )
        return TrellisAuthoritativeProjectionTarget(self.repository, observed)


def _guard_authoritative_task_store(repository: Path) -> None:
    trellis_root = repository / ".trellis"
    tasks_root = trellis_root / "tasks"
    for path, missing_reason in (
        (trellis_root, "projection_authoritative_store_missing"),
        (tasks_root, "projection_authoritative_task_store_missing"),
    ):
        try:
            value = os.lstat(path)
        except FileNotFoundError as exc:
            raise TrellisProjectionCheckoutError(missing_reason) from exc
        except OSError as exc:
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_store_unreadable"
            ) from exc
        if _is_link(value) or not stat.S_ISDIR(value.st_mode):
            raise TrellisProjectionCheckoutError(
                "projection_authoritative_store_unsafe"
            )
    try:
        resolved_trellis = trellis_root.resolve(strict=True)
        resolved_tasks = tasks_root.resolve(strict=True)
    except OSError as exc:
        raise TrellisProjectionCheckoutError(
            "projection_authoritative_store_unreadable"
        ) from exc
    if resolved_trellis.parent != repository or resolved_tasks.parent != resolved_trellis:
        raise TrellisProjectionCheckoutError(
            "projection_authoritative_store_escape"
        )


def _changed_paths(repository: Path) -> tuple[str, ...]:
    raw_values = (
        _git(repository, "diff", "--name-only", "-z"),
        _git(repository, "diff", "--cached", "--name-only", "-z"),
        _git(repository, "ls-files", "--others", "--exclude-standard", "-z"),
    )
    paths: set[str] = set()
    for raw in raw_values:
        for encoded in filter(None, raw.split(b"\0")):
            try:
                value = encoded.decode("utf-8", errors="strict").replace("\\", "/")
            except UnicodeDecodeError as exc:
                raise TrellisProjectionCheckoutError("git_path_invalid_utf8") from exc
            if value.startswith("/") or ".." in value.split("/"):
                raise TrellisProjectionCheckoutError(
                    "projection_authoritative_dirty_path"
                )
            paths.add(value)
    return tuple(sorted(paths, key=lambda value: value.encode("utf-8")))


def _is_projection_task_path(value: str) -> bool:
    parts = value.split("/")
    return (
        len(parts) == 4
        and parts[0] == ".trellis"
        and parts[1] == "tasks"
        and bool(parts[2])
        and parts[2] not in {".", "..", "archive"}
        and parts[3] == "task.json"
    )


def _git(repository: Path, *arguments: str) -> bytes:
    command = ["git", "--no-optional-locks", "-C", str(repository), *arguments]
    environment = os.environ.copy()
    environment.update(
        {"GIT_OPTIONAL_LOCKS": "0", "LANG": "C", "LC_ALL": "C"}
    )
    captured = bytearray()
    try:
        with tempfile.TemporaryFile() as stderr_file:
            with subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                shell=False,
                env=environment,
            ) as process:
                assert process.stdout is not None
                while chunk := process.stdout.read(_READ_CHUNK):
                    if len(captured) + len(chunk) > _MAX_GIT_OUTPUT:
                        process.kill()
                        process.wait(timeout=5)
                        raise TrellisProjectionCheckoutError("git_output_limit")
                    captured.extend(chunk)
                return_code = process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read(_MAX_GIT_STDERR)
    except OSError as exc:
        raise TrellisProjectionCheckoutError("git_unavailable") from exc
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise TrellisProjectionCheckoutError("git_command_failed", detail)
    return bytes(captured)


def _is_link(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & 0x400
    )


__all__ = [
    "TrellisAuthoritativeProjectionProvider",
    "TrellisAuthoritativeProjectionTarget",
    "TrellisProjectionCheckoutError",
]
