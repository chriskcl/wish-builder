"""Read-only local Git and filesystem identity probes for active M1."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Self

from wish_builder.contracts.serialization import canonical_json_bytes

_READ_CHUNK = 64 * 1024
_STDERR_LIMIT = 64 * 1024


class GitIdentityError(RuntimeError):
    """A stable, read-only identity probe failure."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _sha256_ref(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _canonical_existing_path(path: str | os.PathLike[str]) -> tuple[Path, Path]:
    lexical = Path(path).expanduser().absolute()
    try:
        resolved = lexical.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise GitIdentityError("path_unavailable", str(lexical)) from exc
    return lexical, resolved


def _access_control_hash(path: Path, path_stat: os.stat_result) -> str:
    if os.name != "nt":
        return _sha256_ref(
            {
                "gid": int(path_stat.st_gid),
                "mode": stat.S_IMODE(path_stat.st_mode),
                "uid": int(path_stat.st_uid),
            }
        )

    import ctypes
    from ctypes import wintypes

    owner = 0x00000001
    group = 0x00000002
    dacl = 0x00000004
    security_information = owner | group | dacl
    required = wintypes.DWORD()
    get_file_security = ctypes.WinDLL("advapi32", use_last_error=True).GetFileSecurityW
    get_file_security.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_file_security.restype = wintypes.BOOL
    get_file_security(str(path), security_information, None, 0, ctypes.byref(required))
    error = ctypes.get_last_error()
    if required.value == 0 or error not in {0, 122}:
        raise GitIdentityError("filesystem_acl_failed", str(error))
    descriptor = (ctypes.c_ubyte * required.value)()
    if not get_file_security(
        str(path),
        security_information,
        descriptor,
        required.value,
        ctypes.byref(required),
    ):
        raise GitIdentityError("filesystem_acl_failed", str(ctypes.get_last_error()))
    return "sha256:" + hashlib.sha256(bytes(descriptor[: required.value])).hexdigest()


@dataclass(frozen=True, slots=True)
class FilesystemIdentity:
    lexical_path: str
    canonical_path: str
    link_device: int
    link_inode: int
    target_device: int
    target_inode: int
    is_link_or_reparse_point: bool
    access_control_hash: str

    def to_primitive(self) -> dict[str, object]:
        return {
            "access_control_hash": self.access_control_hash,
            "canonical_path": self.canonical_path,
            "is_link_or_reparse_point": self.is_link_or_reparse_point,
            "lexical_path": self.lexical_path,
            "link_device": self.link_device,
            "link_inode": self.link_inode,
            "target_device": self.target_device,
            "target_inode": self.target_inode,
        }

    @property
    def identity_hash(self) -> str:
        return _sha256_ref(self.to_primitive())


def capture_filesystem_identity(
    path: str | os.PathLike[str],
) -> FilesystemIdentity:
    lexical, resolved = _canonical_existing_path(path)
    try:
        link_stat = os.lstat(lexical)
        target_stat = os.stat(resolved)
    except OSError as exc:
        raise GitIdentityError("filesystem_identity_failed", str(lexical)) from exc
    file_attributes = getattr(link_stat, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    is_link = stat.S_ISLNK(link_stat.st_mode) or bool(
        file_attributes & reparse_attribute
    )
    return FilesystemIdentity(
        lexical_path=os.path.normcase(str(lexical)),
        canonical_path=os.path.normcase(str(resolved)),
        link_device=int(link_stat.st_dev),
        link_inode=int(link_stat.st_ino),
        target_device=int(target_stat.st_dev),
        target_inode=int(target_stat.st_ino),
        is_link_or_reparse_point=is_link,
        access_control_hash=_access_control_hash(resolved, target_stat),
    )


def _normalize_scope(scope: object) -> str:
    if type(scope) is not str:
        raise GitIdentityError("invalid_scope", "scope must be a string")
    normalized = unicodedata.normalize("NFC", scope.replace("\\", "/"))
    if (
        not normalized
        or "\x00" in normalized
        or normalized.startswith("/")
        or (len(normalized) >= 2 and normalized[1] == ":")
    ):
        raise GitIdentityError("invalid_scope", scope)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.startswith(":"):
        raise GitIdentityError("invalid_scope", scope)
    normalized = normalized.rstrip("/")
    components = normalized.split("/")
    if not normalized or any(component in {"", ".", ".."} for component in components):
        raise GitIdentityError("invalid_scope", scope)
    return normalized


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "LANG": "C",
            "LC_ALL": "C",
        }
    )
    return environment


def _run_git(
    repository: Path,
    arguments: Iterable[str],
    *,
    stream_to: BinaryIO | None = None,
) -> bytes:
    command = [
        "git",
        "--no-optional-locks",
        "-C",
        str(repository),
        *arguments,
    ]
    captured = bytearray()
    try:
        with tempfile.TemporaryFile() as stderr_file:
            with subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                shell=False,
                env=_git_environment(),
            ) as process:
                assert process.stdout is not None
                while chunk := process.stdout.read(_READ_CHUNK):
                    if stream_to is None:
                        captured.extend(chunk)
                    else:
                        stream_to.write(chunk)
                return_code = process.wait()
            stderr_file.seek(0)
            stderr = stderr_file.read(_STDERR_LIMIT)
    except OSError as exc:
        raise GitIdentityError("git_unavailable", command[0]) from exc
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise GitIdentityError("git_probe_failed", detail[:1024])
    return bytes(captured)


def _git_text(repository: Path, *arguments: str) -> str:
    raw = _run_git(repository, arguments)
    try:
        return raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise GitIdentityError("git_output_invalid_utf8") from exc


class _HashWriter:
    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.byte_count = 0

    def write(self, value: bytes) -> int:
        self._digest.update(value)
        self.byte_count += len(value)
        return len(value)

    @property
    def sha256(self) -> str:
        return self._digest.hexdigest()


def _scope_fingerprint(repository: Path, scopes: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(b"wish-builder-workspace-fingerprint-v1\0")
    digest.update(canonical_json_bytes(list(scopes)))
    path_arguments = ("--", *scopes)
    commands = (
        ("ls-files", "--stage", "-z", *path_arguments),
        (
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignored=no",
            *path_arguments,
        ),
        (
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            *path_arguments,
        ),
        (
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            *path_arguments,
        ),
    )
    for command in commands:
        writer = _HashWriter()
        _run_git(repository, command, stream_to=writer)
        digest.update(
            canonical_json_bytes(
                {
                    "arguments": list(command),
                    "stdout_bytes": writer.byte_count,
                    "stdout_sha256": writer.sha256,
                }
            )
        )
    untracked = _run_git(
        repository,
        ("ls-files", "--others", "--exclude-standard", "-z", *path_arguments),
    )
    for encoded_path in sorted(filter(None, untracked.split(b"\0"))):
        try:
            relative = encoded_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitIdentityError("git_path_invalid_utf8") from exc
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise GitIdentityError("git_path_escape", relative)
        repository_root = repository.resolve(strict=True)
        candidate = repository / relative_path
        try:
            resolved_parent = candidate.parent.resolve(strict=True)
            resolved_parent.relative_to(repository_root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise GitIdentityError("git_path_escape", relative) from exc
        candidate = resolved_parent / candidate.name
        try:
            file_stat = os.lstat(candidate)
            digest.update(len(encoded_path).to_bytes(8, "big"))
            digest.update(encoded_path)
            digest.update(int(file_stat.st_mode).to_bytes(8, "big", signed=False))
            if stat.S_ISLNK(file_stat.st_mode):
                target = os.fsencode(os.readlink(candidate))
                verified_stat = os.lstat(candidate)
                if not stat.S_ISLNK(verified_stat.st_mode) or (
                    verified_stat.st_dev,
                    verified_stat.st_ino,
                ) != (file_stat.st_dev, file_stat.st_ino):
                    raise GitIdentityError("workspace_probe_race", relative)
                digest.update(hashlib.sha256(target).digest())
            elif stat.S_ISREG(file_stat.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(candidate, flags)
                with open(descriptor, "rb", closefd=True) as handle:
                    opened_stat = os.fstat(handle.fileno())
                    if not stat.S_ISREG(opened_stat.st_mode) or (
                        opened_stat.st_dev,
                        opened_stat.st_ino,
                    ) != (file_stat.st_dev, file_stat.st_ino):
                        raise GitIdentityError("workspace_probe_race", relative)
                    file_digest = hashlib.sha256()
                    while chunk := handle.read(_READ_CHUNK):
                        file_digest.update(chunk)
                digest.update(file_digest.digest())
            else:
                raise GitIdentityError("unsupported_dirty_path", relative)
        except OSError as exc:
            raise GitIdentityError("workspace_probe_race", relative) from exc
    return "sha256:" + digest.hexdigest()


def _pristine_scope_fingerprint(repository: Path, scopes: tuple[str, ...]) -> str:
    """Bind the current index while reconstructing an otherwise clean scope."""

    digest = hashlib.sha256()
    digest.update(b"wish-builder-workspace-fingerprint-v1\0")
    digest.update(canonical_json_bytes(list(scopes)))
    path_arguments = ("--", *scopes)
    commands = (
        ("ls-files", "--stage", "-z", *path_arguments),
        (
            "status",
            "--porcelain=v2",
            "-z",
            "--untracked-files=all",
            "--ignored=no",
            *path_arguments,
        ),
        (
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            *path_arguments,
        ),
        (
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "--full-index",
            *path_arguments,
        ),
    )
    for index, command in enumerate(commands):
        writer = _HashWriter()
        if index == 0:
            _run_git(repository, command, stream_to=writer)
        digest.update(
            canonical_json_bytes(
                {
                    "arguments": list(command),
                    "stdout_bytes": writer.byte_count,
                    "stdout_sha256": writer.sha256,
                }
            )
        )
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    local_repository_id: str
    local_worktree_id: str
    common_dir: FilesystemIdentity
    worktree_root: FilesystemIdentity
    git_dir: FilesystemIdentity
    target_full_ref: str
    base_commit_sha: str
    scopes: tuple[str, ...]
    index_dirty_fingerprint: str

    def to_primitive(self) -> dict[str, object]:
        return {
            "base_commit_sha": self.base_commit_sha,
            "common_dir": self.common_dir.to_primitive(),
            "git_dir": self.git_dir.to_primitive(),
            "index_dirty_fingerprint": self.index_dirty_fingerprint,
            "local_repository_id": self.local_repository_id,
            "local_worktree_id": self.local_worktree_id,
            "scopes": list(self.scopes),
            "target_full_ref": self.target_full_ref,
            "worktree_root": self.worktree_root.to_primitive(),
        }

    @property
    def workspace_hash(self) -> str:
        return _sha256_ref(self.to_primitive())


def capture_workspace_identity(
    repository: str | os.PathLike[str],
    scopes: Iterable[str],
) -> WorkspaceIdentity:
    requested, _ = _canonical_existing_path(repository)
    normalized_scopes = tuple(sorted({_normalize_scope(scope) for scope in scopes}))
    if not normalized_scopes:
        raise GitIdentityError("invalid_scope", "at least one scope is required")
    worktree_path = Path(_git_text(requested, "rev-parse", "--show-toplevel"))
    common_path = Path(
        _git_text(
            requested,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    )
    git_path = Path(
        _git_text(
            requested,
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
        )
    )
    target_full_ref = _git_text(requested, "symbolic-ref", "--quiet", "HEAD")
    if not target_full_ref.startswith("refs/heads/"):
        raise GitIdentityError("unsupported_head", target_full_ref)
    base_commit = _git_text(requested, "rev-parse", "--verify", "HEAD^{commit}")
    if len(base_commit) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in base_commit
    ):
        raise GitIdentityError("invalid_commit_identity", base_commit)

    common_identity = capture_filesystem_identity(common_path)
    worktree_identity = capture_filesystem_identity(worktree_path)
    git_identity = capture_filesystem_identity(git_path)
    repository_id = _sha256_ref(common_identity.to_primitive())
    worktree_id = _sha256_ref(
        {
            "git_dir": git_identity.to_primitive(),
            "local_repository_id": repository_id,
            "worktree_root": worktree_identity.to_primitive(),
        }
    )
    fingerprint = _scope_fingerprint(worktree_path, normalized_scopes)
    return WorkspaceIdentity(
        local_repository_id=repository_id,
        local_worktree_id=worktree_id,
        common_dir=common_identity,
        worktree_root=worktree_identity,
        git_dir=git_identity,
        target_full_ref=target_full_ref,
        base_commit_sha=base_commit,
        scopes=normalized_scopes,
        index_dirty_fingerprint=fingerprint,
    )


def reconstruct_pristine_workspace_identity(
    observed: WorkspaceIdentity,
) -> WorkspaceIdentity:
    """Reconstruct Gate-clean identity while retaining the current Git index.

    Callers must independently prove that every working-tree change is an
    allowed derived projection. Staged changes remain bound by ``ls-files`` and
    therefore cannot be hidden by this reconstruction.
    """

    if type(observed) is not WorkspaceIdentity:
        raise TypeError("observed must be a WorkspaceIdentity")
    repository = Path(observed.worktree_root.canonical_path)
    fingerprint = _pristine_scope_fingerprint(repository, observed.scopes)
    return replace(observed, index_dirty_fingerprint=fingerprint)


@dataclass(frozen=True, slots=True)
class IdentityComparison:
    ok: bool
    reason: str | None
    mismatches: tuple[str, ...]
    observed: WorkspaceIdentity | None


def compare_workspace_identity(
    expected: WorkspaceIdentity,
    observed: WorkspaceIdentity,
) -> IdentityComparison:
    fields = (
        "local_repository_id",
        "local_worktree_id",
        "target_full_ref",
        "base_commit_sha",
        "scopes",
        "index_dirty_fingerprint",
    )
    mismatches = tuple(
        field
        for field in fields
        if getattr(expected, field) != getattr(observed, field)
    )
    return IdentityComparison(
        ok=not mismatches,
        reason=None if not mismatches else "workspace_drift",
        mismatches=mismatches,
        observed=observed,
    )


def revalidate_workspace_identity(expected: WorkspaceIdentity) -> IdentityComparison:
    try:
        observed = capture_workspace_identity(
            expected.worktree_root.lexical_path, expected.scopes
        )
    except GitIdentityError as exc:
        return IdentityComparison(False, "workspace_drift", (exc.reason,), None)
    return compare_workspace_identity(expected, observed)


@dataclass(frozen=True, slots=True)
class ControlRootComparison:
    ok: bool
    reason: str | None
    observed: FilesystemIdentity | None


def revalidate_control_root(
    expected: FilesystemIdentity,
) -> ControlRootComparison:
    if type(expected) is not FilesystemIdentity:
        raise TypeError("expected must be a FilesystemIdentity")
    try:
        observed = capture_filesystem_identity(expected.lexical_path)
    except GitIdentityError:
        return ControlRootComparison(False, "control_root_drift", None)
    if observed != expected:
        return ControlRootComparison(False, "control_root_drift", observed)
    return ControlRootComparison(True, None, observed)


def _windows_directory_handle_identity(handle: int) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = int(information.dwFileAttributes)
    if not attributes & 0x00000010 or attributes & 0x00000400:
        raise OSError("protected control root is not a non-reparse directory")
    file_index = (int(information.nFileIndexHigh) << 32) | int(
        information.nFileIndexLow
    )
    return int(information.dwVolumeSerialNumber), file_index, attributes


def _open_windows_directory_handle(path: str) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        path,
        0x00000080,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if not handle or handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(handle)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


class ProtectedControlRoot:
    """A path identity bound to an open non-reparse directory handle."""

    __slots__ = ("_handle", "_handle_identity", "_windows", "expected")

    def __init__(
        self,
        expected: FilesystemIdentity,
        handle: int,
        handle_identity: tuple[int, ...],
        *,
        windows: bool,
    ) -> None:
        if type(expected) is not FilesystemIdentity:
            raise TypeError("expected must be a FilesystemIdentity")
        if type(handle) is not int or handle < 0:
            raise ValueError("handle must be a non-negative integer")
        if type(handle_identity) is not tuple or not handle_identity:
            raise ValueError("handle_identity must be a non-empty tuple")
        if type(windows) is not bool:
            raise TypeError("windows must be a boolean")
        self.expected = expected
        self._handle: int | None = handle
        self._handle_identity = handle_identity
        self._windows = windows

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> ProtectedControlRoot:
        expected = capture_filesystem_identity(path)
        if expected.is_link_or_reparse_point:
            raise GitIdentityError("control_root_drift", "reparse roots are denied")
        if os.name == "nt":
            try:
                handle = _open_windows_directory_handle(expected.lexical_path)
                handle_identity = _windows_directory_handle_identity(handle)
            except OSError as exc:
                raise GitIdentityError(
                    "control_root_handle_failed", expected.lexical_path
                ) from exc
            windows = True
        else:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                handle = os.open(expected.lexical_path, flags)
                handle_stat = os.fstat(handle)
                if not stat.S_ISDIR(handle_stat.st_mode):
                    raise OSError("protected control root is not a directory")
                handle_identity = (
                    int(handle_stat.st_dev),
                    int(handle_stat.st_ino),
                    int(handle_stat.st_mode),
                )
            except OSError as exc:
                try:
                    os.close(handle)
                except (OSError, UnboundLocalError):
                    pass
                raise GitIdentityError(
                    "control_root_handle_failed", expected.lexical_path
                ) from exc
            windows = False
        protected = cls(expected, handle, handle_identity, windows=windows)
        if not protected.revalidate().ok:
            protected.close()
            raise GitIdentityError("control_root_drift", expected.lexical_path)
        return protected

    @property
    def closed(self) -> bool:
        return self._handle is None

    def revalidate(self) -> ControlRootComparison:
        observed = revalidate_control_root(self.expected)
        if not observed.ok or self._handle is None:
            return ControlRootComparison(
                False,
                "control_root_drift",
                observed.observed,
            )
        try:
            if self._windows:
                handle_identity = _windows_directory_handle_identity(self._handle)
            else:
                handle_stat = os.fstat(self._handle)
                if not stat.S_ISDIR(handle_stat.st_mode):
                    raise OSError("protected control root is not a directory")
                handle_identity = (
                    int(handle_stat.st_dev),
                    int(handle_stat.st_ino),
                    int(handle_stat.st_mode),
                )
        except OSError:
            return ControlRootComparison(False, "control_root_drift", observed.observed)
        if handle_identity != self._handle_identity:
            return ControlRootComparison(False, "control_root_drift", observed.observed)
        return observed

    def close(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        if self._windows:
            _close_windows_handle(handle)
        else:
            os.close(handle)

    def __enter__(self) -> Self:
        if self.closed:
            raise RuntimeError("protected control root is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
