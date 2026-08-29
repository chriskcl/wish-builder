#!/usr/bin/env python3
"""Build byte-reproducible wheel and sdist artifacts from the worktree."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_EPOCH = 315532800  # 1980-01-01T00:00:00Z, the ZIP lower bound.
CANONICAL_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
CANONICAL_FILE_MODE = 0o644
CANONICAL_DIRECTORY_MODE = 0o755
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class DistributionBuildError(RuntimeError):
    """Raised when a canonical distribution cannot be built safely."""


@dataclass(frozen=True)
class _SourceFile:
    relative: Path
    index_mode: int
    tracked: bool


@dataclass(frozen=True)
class _OutputDirectory:
    lexical: Path
    resolved: Path
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _PromotedArtifact:
    path: Path
    sha256: str
    size_bytes: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _portable_text_key(path: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", component).casefold()
        for component in path.rstrip("/").split("/")
    )


def _portable_path_key(path: Path) -> str:
    return _portable_text_key(path.as_posix())


def _validate_portable_name(name: str, *, kind: str) -> str:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or re.match(r"[A-Za-z]:", name) is not None
    ):
        raise DistributionBuildError(f"unsafe {kind} path: {name!r}")
    path = name[:-1] if name.endswith("/") else name
    components = path.split("/")
    pure = PurePosixPath(path)
    if (
        not path
        or any(component in {"", ".", ".."} for component in components)
        or tuple(pure.parts) != tuple(components)
    ):
        raise DistributionBuildError(f"unsafe {kind} path: {name!r}")
    for component in components:
        if component.endswith((" ", ".")) or any(
            ord(character) < 32
            or ord(character) == 127
            or character in _WINDOWS_FORBIDDEN_CHARS
            for character in component
        ):
            raise DistributionBuildError(f"unsafe {kind} path: {name!r}")
        normalized = unicodedata.normalize("NFC", component)
        device_stem = normalized.split(".", 1)[0].rstrip(" .").casefold()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise DistributionBuildError(f"unsafe {kind} path: {name!r}")
    return path


def _decode_git_path(raw_path: bytes) -> Path:
    try:
        text = raw_path.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DistributionBuildError("Git reported a non-UTF-8 source path") from exc
    validated = _validate_portable_name(text, kind="Git source")
    relative = Path(*validated.split("/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise DistributionBuildError(f"unsafe Git path: {text!r}")
    return relative


def _git_ls_files(repository_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "ls-files", "-z", *arguments],
        cwd=repository_root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _source_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _source_metadata(
    repository_root: Path, source_file: _SourceFile
) -> os.stat_result | None:
    source = repository_root / source_file.relative
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        if source_file.tracked:
            return None
        raise DistributionBuildError(
            f"untracked distribution source disappeared: {source_file.relative.as_posix()}"
        ) from None
    if source_file.index_mode == 0o120000:
        raise DistributionBuildError(
            f"Git symlink cannot be a distribution source: {source_file.relative.as_posix()}"
        )
    if source_file.index_mode not in {0o100644, 0o100755}:
        raise DistributionBuildError(
            "unsupported Git source mode "
            f"{source_file.index_mode:o}: {source_file.relative.as_posix()}"
        )
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise DistributionBuildError(
            f"distribution source must be a regular file: {source_file.relative.as_posix()}"
        )
    return metadata


def _worktree_sources(repository_root: Path) -> list[_SourceFile]:
    sources: list[_SourceFile] = []
    for record in _git_ls_files(repository_root, "--cached", "--stage").split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_text, _object_id, stage_text = header.split()
            index_mode = int(mode_text, 8)
            stage_number = int(stage_text)
        except (ValueError, TypeError) as exc:
            raise DistributionBuildError(
                "Git reported malformed index metadata"
            ) from exc
        if stage_number != 0:
            raise DistributionBuildError(
                "unmerged Git index entries cannot be distributed"
            )
        sources.append(
            _SourceFile(
                relative=_decode_git_path(raw_path),
                index_mode=index_mode,
                tracked=True,
            )
        )
    for raw_path in _git_ls_files(
        repository_root, "--others", "--exclude-standard"
    ).split(b"\0"):
        if raw_path:
            sources.append(
                _SourceFile(
                    relative=_decode_git_path(raw_path),
                    index_mode=0o100644,
                    tracked=False,
                )
            )

    filtered: list[_SourceFile] = []
    seen: set[str] = set()
    for source_file in sources:
        if _source_metadata(repository_root, source_file) is None:
            continue
        key = _portable_path_key(source_file.relative)
        if key in seen:
            raise DistributionBuildError(
                "distribution source paths collide portably: "
                f"{source_file.relative.as_posix()}"
            )
        seen.add(key)
        filtered.append(source_file)
    if not filtered:
        raise DistributionBuildError("Git did not report any distribution source files")
    return sorted(filtered, key=lambda item: item.relative.as_posix())


def _worktree_files(repository_root: Path) -> list[Path]:
    return [source.relative for source in _worktree_sources(repository_root)]


def _copy_source_file(
    repository_root: Path, destination_root: Path, source_file: _SourceFile
) -> None:
    source = repository_root / source_file.relative
    target = destination_root / source_file.relative
    before = _source_metadata(repository_root, source_file)
    if before is None:
        raise DistributionBuildError(
            f"distribution source disappeared: {source_file.relative.as_posix()}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)
    target_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    source_fd = -1
    target_fd = -1
    try:
        source_fd = os.open(source, source_flags)
        opened = os.fstat(source_fd)
        if (
            _metadata_is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _source_identity(opened) != _source_identity(before)
        ):
            raise DistributionBuildError(
                f"distribution source changed before copy: {source_file.relative.as_posix()}"
            )
        target_fd = os.open(target, target_flags, CANONICAL_FILE_MODE)
        with (
            os.fdopen(source_fd, "rb", closefd=True) as source_handle,
            os.fdopen(target_fd, "wb", closefd=True) as target_handle,
        ):
            source_fd = -1
            target_fd = -1
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            after_open = os.fstat(source_handle.fileno())
        try:
            after_path = source.lstat()
        except FileNotFoundError as exc:
            raise DistributionBuildError(
                f"distribution source changed during copy: {source_file.relative.as_posix()}"
            ) from exc
        if _source_identity(after_open) != _source_identity(opened) or _source_identity(
            after_path
        ) != _source_identity(opened):
            raise DistributionBuildError(
                f"distribution source changed during copy: {source_file.relative.as_posix()}"
            )
        os.chmod(target, CANONICAL_FILE_MODE)
        os.utime(target, (CANONICAL_EPOCH, CANONICAL_EPOCH))
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if target_fd >= 0:
            os.close(target_fd)


def _stage_worktree(repository_root: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for source_file in _worktree_sources(repository_root):
        _copy_source_file(repository_root, destination, source_file)


def _single_artifact(directory: Path, pattern: str, *, kind: str) -> Path:
    matches = sorted(directory.glob(pattern), key=lambda path: path.name)
    if len(matches) != 1:
        raise DistributionBuildError(
            f"expected exactly one raw {kind}, found {len(matches)}"
        )
    path = matches[0]
    if path.is_symlink() or not path.is_file():
        raise DistributionBuildError(f"raw {kind} must be a regular file")
    return path


def canonicalize_wheel(source: Path, destination: Path) -> None:
    """Rewrite a wheel with stable member metadata and compression."""
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    seen: set[str] = set()
    try:
        with (
            zipfile.ZipFile(source, "r") as raw,
            zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_STORED
            ) as canonical,
        ):
            members: list[tuple[str, zipfile.ZipInfo]] = []
            for member in raw.infolist():
                normalized = _validate_portable_name(
                    member.orig_filename, kind="wheel member"
                )
                if member.orig_filename.rstrip("/") != member.filename.rstrip("/"):
                    raise DistributionBuildError(
                        f"unsafe wheel member path: {member.orig_filename!r}"
                    )
                key = _portable_text_key(normalized)
                if key in seen:
                    raise DistributionBuildError(
                        f"wheel contains duplicate member: {member.filename}"
                    )
                seen.add(key)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise DistributionBuildError(
                        f"wheel contains a symlink: {member.filename}"
                    )
                is_directory = member.is_dir()
                file_type = stat.S_IFMT(mode)
                if is_directory:
                    if not member.filename.endswith("/"):
                        raise DistributionBuildError(
                            f"wheel directory name is not canonical: {member.filename}"
                        )
                elif file_type not in {0, stat.S_IFREG}:
                    raise DistributionBuildError(
                        f"wheel contains a non-regular member: {member.filename}"
                    )
                members.append((normalized, member))

            for normalized, member in sorted(members, key=lambda item: item[0]):
                is_directory = member.is_dir()
                archive_name = normalized + ("/" if is_directory else "")
                content = raw.read(member)
                if is_directory and content:
                    raise DistributionBuildError(
                        f"wheel directory contains data: {member.filename}"
                    )
                info = zipfile.ZipInfo(archive_name, CANONICAL_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.create_version = 20
                info.extract_version = 20
                info.external_attr = (
                    ((stat.S_IFDIR | CANONICAL_DIRECTORY_MODE) << 16) | 0x10
                    if is_directory
                    else (stat.S_IFREG | CANONICAL_FILE_MODE) << 16
                )
                info.internal_attr = 0
                info.extra = b""
                info.comment = b""
                canonical.writestr(info, content, compress_type=zipfile.ZIP_STORED)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def canonicalize_sdist(source: Path, destination: Path) -> None:
    """Rewrite an sdist with stable tar, PAX, owner, and gzip metadata."""
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    seen: set[str] = set()
    try:
        with tarfile.open(source, "r:gz") as raw, temporary.open("wb") as output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=0,
                fileobj=output,
                mtime=CANONICAL_EPOCH,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as canonical:
                    members: list[tuple[str, tarfile.TarInfo]] = []
                    for member in raw.getmembers():
                        normalized = _validate_portable_name(
                            member.name, kind="sdist member"
                        )
                        key = _portable_text_key(normalized)
                        if key in seen:
                            raise DistributionBuildError(
                                f"sdist contains duplicate member: {member.name}"
                            )
                        seen.add(key)
                        if not (member.isfile() or member.isdir()):
                            raise DistributionBuildError(
                                f"sdist contains a non-regular member: {member.name}"
                            )
                        members.append((normalized, member))

                    for normalized, member in sorted(members, key=lambda item: item[0]):
                        info = copy.copy(member)
                        info.name = normalized
                        info.mtime = CANONICAL_EPOCH
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mode = (
                            CANONICAL_DIRECTORY_MODE
                            if member.isdir()
                            else CANONICAL_FILE_MODE
                        )
                        info.linkname = ""
                        info.devmajor = 0
                        info.devminor = 0
                        info.pax_headers = {}
                        info.sparse = None
                        if member.isdir():
                            info.size = 0
                            canonical.addfile(info)
                            continue
                        extracted = raw.extractfile(member)
                        if extracted is None:
                            raise DistributionBuildError(
                                f"sdist member cannot be read: {member.name}"
                            )
                        content = extracted.read()
                        if len(content) != member.size:
                            raise DistributionBuildError(
                                f"sdist member changed while reading: {member.name}"
                            )
                        canonical.addfile(info, io.BytesIO(content))
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _run_raw_build(source_root: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(CANONICAL_EPOCH)
    environment["PYTHONHASHSEED"] = "0"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(output),
        ],
        cwd=source_root,
        env=environment,
        check=True,
    )


def _build_once(repository_root: Path, workspace: Path) -> dict[str, Path]:
    source_root = workspace / "source"
    raw_dist = workspace / "raw-dist"
    canonical_dist = workspace / "canonical-dist"
    _stage_worktree(repository_root, source_root)
    _run_raw_build(source_root, raw_dist)
    canonical_dist.mkdir()
    wheel = _single_artifact(raw_dist, "*.whl", kind="wheel")
    sdist = _single_artifact(raw_dist, "*.tar.gz", kind="sdist")
    canonical_wheel = canonical_dist / wheel.name
    canonical_sdist = canonical_dist / sdist.name
    canonicalize_wheel(wheel, canonical_wheel)
    canonicalize_sdist(sdist, canonical_sdist)
    return {"wheel": canonical_wheel, "sdist": canonical_sdist}


def _new_output_temp(destination: Path, *, suffix: str) -> tuple[int, Path]:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=suffix,
        dir=destination.parent,
    )
    return descriptor, Path(name)


def _stage_promotion(source: Path, destination: Path, *, suffix: str = ".tmp") -> Path:
    try:
        before = source.lstat()
    except FileNotFoundError as exc:
        raise DistributionBuildError("promotion source disappeared") from exc
    if _metadata_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise DistributionBuildError("promotion source must be a regular file")
    descriptor, temporary = _new_output_temp(destination, suffix=suffix)
    source_descriptor = -1
    try:
        source_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, source_flags)
        opened = os.fstat(source_descriptor)
        if (
            _metadata_is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or _source_identity(opened) != _source_identity(before)
        ):
            raise DistributionBuildError("promotion source changed before copy")
        with (
            os.fdopen(source_descriptor, "rb", closefd=True) as source_handle,
            os.fdopen(descriptor, "wb", closefd=True) as target_handle,
        ):
            source_descriptor = -1
            descriptor = -1
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
            after_open = os.fstat(source_handle.fileno())
        try:
            after_path = source.lstat()
        except FileNotFoundError as exc:
            raise DistributionBuildError(
                "promotion source changed during copy"
            ) from exc
        if _source_identity(after_open) != _source_identity(opened) or _source_identity(
            after_path
        ) != _source_identity(opened):
            raise DistributionBuildError("promotion source changed during copy")
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if descriptor >= 0:
            os.close(descriptor)


def _existing_regular_file(path: Path, *, kind: str) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise DistributionBuildError(f"{kind} must be a regular non-symlink file")
    return True


def _promote(source: Path, destination: Path) -> None:
    temporary = _stage_promotion(source, destination)
    try:
        _existing_regular_file(destination, kind="promotion destination")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        getattr(metadata, "st_file_attributes", 0),
    )


def _path_is_junction(path: Path) -> bool:
    predicate = getattr(path, "is_junction", None)
    return bool(predicate is not None and predicate())


def _assert_no_link_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if _metadata_is_link_or_reparse(metadata) or _path_is_junction(current):
            raise DistributionBuildError(
                f"distribution output has a link or reparse ancestor: {current}"
            )


def _prepare_output_directory(output: Path) -> _OutputDirectory:
    if output.is_symlink() or _path_is_junction(output):
        raise DistributionBuildError("distribution output must not be a symlink")
    lexical = Path(os.path.abspath(os.fspath(output)))
    _assert_no_link_components(lexical)
    lexical.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(lexical)
    metadata = lexical.lstat()
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise DistributionBuildError("distribution output must be a regular directory")
    return _OutputDirectory(
        lexical=lexical,
        resolved=lexical.resolve(strict=True),
        identity=_directory_identity(metadata),
    )


def _assert_output_directory(output: _OutputDirectory) -> None:
    _assert_no_link_components(output.lexical)
    try:
        metadata = output.lexical.lstat()
        resolved = output.lexical.resolve(strict=True)
    except FileNotFoundError as exc:
        raise DistributionBuildError("distribution output disappeared") from exc
    if (
        _metadata_is_link_or_reparse(metadata)
        or not stat.S_ISDIR(metadata.st_mode)
        or _directory_identity(metadata) != output.identity
        or resolved != output.resolved
    ):
        raise DistributionBuildError("distribution output changed during build")


@contextmanager
def _exclusive_output_lock(output: _OutputDirectory) -> Iterator[None]:
    _assert_output_directory(output)
    lock_path = output.resolved / ".wish-builder-build.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise DistributionBuildError(
            "another distribution promotion is active or left a stale lock"
        ) from exc
    lock_identity = _directory_identity(os.fstat(descriptor))
    active_error: BaseException | None = None
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(f"pid={os.getpid()}\n".encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        _assert_output_directory(output)
        yield
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            current = lock_path.lstat()
        except FileNotFoundError:
            current = None
        cleanup_error: OSError | None = None
        cleanup_message: str | None = None
        if current is None:
            cleanup_message = "distribution promotion lock disappeared during use"
        elif _directory_identity(current) != lock_identity:
            cleanup_message = "distribution promotion lock changed during use"
        else:
            try:
                lock_path.unlink()
            except OSError as exc:
                cleanup_error = exc
                cleanup_message = (
                    f"distribution promotion lock could not be removed: {exc}"
                )
        if cleanup_message is not None:
            if active_error is not None:
                active_error.add_note(cleanup_message)
            else:
                raise DistributionBuildError(cleanup_message) from cleanup_error


def _promote_distribution_set(
    sources: dict[str, Path], output: _OutputDirectory
) -> dict[str, _PromotedArtifact]:
    expected_kinds = ("wheel", "sdist")
    if set(sources) != set(expected_kinds):
        raise DistributionBuildError("distribution set must contain wheel and sdist")
    destinations = {
        kind: output.resolved / sources[kind].name for kind in expected_kinds
    }
    if len(set(destinations.values())) != len(destinations):
        raise DistributionBuildError("distribution artifact destinations collide")
    with _exclusive_output_lock(output):
        staged: dict[str, Path] = {}
        backups: dict[str, Path] = {}
        existed: dict[str, bool] = {}
        attempted: list[str] = []
        preserve_backups: set[Path] = set()
        committed = False
        try:
            for kind in expected_kinds:
                staged[kind] = _stage_promotion(sources[kind], destinations[kind])
            for kind in expected_kinds:
                destination = destinations[kind]
                existed[kind] = _existing_regular_file(
                    destination, kind=f"existing {kind} destination"
                )
                if existed[kind]:
                    backups[kind] = _stage_promotion(
                        destination, destination, suffix=".bak"
                    )
            _assert_output_directory(output)
            for kind in expected_kinds:
                attempted.append(kind)
                os.replace(staged[kind], destinations[kind])
            _assert_output_directory(output)
            promoted = {
                kind: _PromotedArtifact(
                    path=destinations[kind],
                    sha256=_sha256(destinations[kind]),
                    size_bytes=destinations[kind].stat().st_size,
                )
                for kind in expected_kinds
            }
            committed = True
        except BaseException as exc:
            rollback_errors: list[str] = []
            for kind in reversed(attempted):
                if existed[kind]:
                    backup = backups[kind]
                    try:
                        os.replace(backup, destinations[kind])
                    except OSError as rollback_exc:
                        preserve_backups.add(backup)
                        rollback_errors.append(
                            f"restore {kind} from {backup}: {rollback_exc}"
                        )
                else:
                    try:
                        destinations[kind].unlink(missing_ok=True)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"remove {kind}: {rollback_exc}")
            if rollback_errors:
                raise DistributionBuildError(
                    "distribution promotion failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise
        else:
            cleanup_errors: list[str] = []
            for backup in backups.values():
                try:
                    backup.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    preserve_backups.add(backup)
                    cleanup_errors.append(f"{backup}: {cleanup_exc}")
            if cleanup_errors:
                raise DistributionBuildError(
                    "distribution promotion committed but backup cleanup was incomplete: "
                    + "; ".join(cleanup_errors)
                )
            return promoted
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
            if not committed:
                for backup in backups.values():
                    if backup not in preserve_backups:
                        backup.unlink(missing_ok=True)


def build_distributions(
    output: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    verify_repeat: bool = True,
) -> dict[str, object]:
    repository_root = repository_root.resolve(strict=True)
    output_directory = _prepare_output_directory(output)

    with tempfile.TemporaryDirectory(prefix="wish-builder-dist-") as first_name:
        first = _build_once(repository_root, Path(first_name))
        if verify_repeat:
            with tempfile.TemporaryDirectory(
                prefix="wish-builder-dist-repeat-"
            ) as second_name:
                second = _build_once(repository_root, Path(second_name))
                for kind in ("wheel", "sdist"):
                    if first[kind].name != second[kind].name:
                        raise DistributionBuildError(
                            f"repeated {kind} build produced a different filename"
                        )
                    if first[kind].read_bytes() != second[kind].read_bytes():
                        raise DistributionBuildError(
                            f"repeated canonical {kind} build produced different bytes"
                        )
        promoted = _promote_distribution_set(first, output_directory)
        artifacts = []
        for kind in ("wheel", "sdist"):
            artifact = promoted[kind]
            artifacts.append(
                {
                    "kind": kind,
                    "path": artifact.path.name,
                    "sha256": "sha256:" + artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
            )
    return {
        "artifacts": artifacts,
        "canonical_epoch": CANONICAL_EPOCH,
        "repeat_verified": verify_repeat,
        "status": "passed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build canonical wheel and sdist artifacts twice."
    )
    parser.add_argument("--outdir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--skip-repeat-check",
        action="store_true",
        help="build once; intended only for focused local diagnostics",
    )
    arguments = parser.parse_args(argv)
    try:
        result = build_distributions(
            arguments.outdir,
            verify_repeat=not arguments.skip_repeat_check,
        )
    except (DistributionBuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"distribution build failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
