#!/usr/bin/env python3
"""Build and clean-install evidence for one canonical distribution artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import venv
import zipfile
from email.parser import BytesParser
from email.policy import default as default_email_policy
from pathlib import Path
from typing import Any, BinaryIO


SCHEMA_VERSION = 1
CELL_SCHEMA_VERSION = 1
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LICENSE_EXPRESSION = "GPL-3.0-only"
PACKAGE_NAME = "wish-builder"
PACKAGE_FILENAME_NAME = "wish_builder"
EXPECTED_PLATFORMS = ("ubuntu-latest", "windows-latest")
EXPECTED_PYTHON_VERSIONS = ("3.11", "3.12", "3.13")
_REVISION_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANONICAL_PACKAGE_VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))+"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?"
    r"(?:\.post(?:0|[1-9][0-9]*))?"
    r"(?:\.dev(?:0|[1-9][0-9]*))?\Z"
)
_TRELLIS_QUALIFIED_SPEC_RE = re.compile(
    rb"@mindfoldhq/(?:trellis-core|trellis)@"
    rb"(?P<spec>[^\s\"'`<>{}\[\](),;]+)",
    re.IGNORECASE,
)
_PACKAGE_MANAGER_COMMAND_GAP = 4096
_BARE_TRELLIS_INSTALL_RE = re.compile(
    (
        rb"\b(?:npm|pnpm|yarn)\b[^\r\n\x00]{0,%d}?"
        rb"\b(?:add|i|install|pack|view)\b[^\r\n\x00]{0,%d}?"
        rb"[\"']?@mindfoldhq/(?:trellis-core|trellis)"
        rb"(?=[\s\"'`<>{}\[\](),;&|\\]|\Z)"
    )
    % (_PACKAGE_MANAGER_COMMAND_GAP, _PACKAGE_MANAGER_COMMAND_GAP),
    re.IGNORECASE,
)
_SUPPORTED_TRELLIS_INSTALL_SPEC = b"0.6.15"
_ARCHIVE_SCAN_CHUNK_SIZE = 64 * 1024
_ARCHIVE_SCAN_OVERLAP = (_PACKAGE_MANAGER_COMMAND_GAP * 2) + 256
MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200
MAX_ARCHIVE_METADATA_BYTES = 1024 * 1024
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "aux",
        "clock$",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "com\u00b9",
        "com\u00b2",
        "com\u00b3",
        "con",
        "conin$",
        "conout$",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
        "lpt\u00b9",
        "lpt\u00b2",
        "lpt\u00b3",
        "nul",
        "prn",
    }
)


class DistributionEvidenceError(ValueError):
    """Raised when local distribution files cannot prove an unambiguous build."""


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _require_revision(value: object) -> str:
    if type(value) is not str or _REVISION_RE.fullmatch(value) is None:
        raise DistributionEvidenceError(
            "revision must be a lowercase 40- or 64-character commit id"
        )
    return value


def _regular_file(path: Path, *, kind: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DistributionEvidenceError(f"{kind} file is missing: {path}") from exc
    if path.is_symlink() or not resolved.is_file():
        raise DistributionEvidenceError(f"{kind} must be a regular non-symlink file")
    return resolved


def _single_distribution(dist_dir: Path, pattern: str, *, kind: str) -> Path:
    try:
        root = dist_dir.resolve(strict=True)
    except OSError as exc:
        raise DistributionEvidenceError(f"distribution directory is missing: {dist_dir}") from exc
    if not root.is_dir() or dist_dir.is_symlink():
        raise DistributionEvidenceError("distribution root must be a regular directory")
    matches = sorted(root.glob(pattern), key=lambda item: item.name)
    if len(matches) != 1:
        raise DistributionEvidenceError(
            f"expected exactly one {kind}, found {len(matches)}"
        )
    return _regular_file(matches[0], kind=kind)


def _reject_top_level_npm_tarballs(dist_dir: Path) -> None:
    try:
        root = dist_dir.resolve(strict=True)
    except OSError as exc:
        raise DistributionEvidenceError(
            f"distribution directory is missing: {dist_dir}"
        ) from exc
    if not root.is_dir() or dist_dir.is_symlink():
        raise DistributionEvidenceError("distribution root must be a regular directory")
    forbidden = sorted(
        path.name for path in root.iterdir() if path.name.casefold().endswith(".tgz")
    )
    if forbidden:
        raise DistributionEvidenceError(
            "distribution directory contains forbidden top-level .tgz files: "
            + ", ".join(forbidden)
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _artifact(kind: str, path: Path) -> dict[str, object]:
    return {
        "kind": kind,
        "path": path.name,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _validated_archive_member_name(kind: str, member_name: str) -> str:
    if "\\" in member_name:
        raise DistributionEvidenceError(
            f"{kind} archive contains an unsafe member path: {member_name!r}"
        )
    normalized = member_name.replace("\\", "/")
    path = normalized[:-1] if normalized.endswith("/") else normalized
    components = path.split("/")
    if (
        not path
        or "\x00" in path
        or normalized.startswith("/")
        or re.match(r"[A-Za-z]:", normalized) is not None
        or any(component in {"", ".", ".."} for component in components)
        or any(_is_unsafe_windows_component(component) for component in components)
    ):
        raise DistributionEvidenceError(
            f"{kind} archive contains an unsafe member path: {member_name!r}"
        )
    if any(
        component.casefold().endswith(".tgz")
        for component in path.split("/")
        if component
    ):
        raise DistributionEvidenceError(
            f"{kind} archive contains a forbidden .tgz member: {member_name!r}"
        )
    return normalized


def _is_unsafe_windows_component(component: str) -> bool:
    if component[-1] in {" ", "."}:
        return True
    if any(
        ord(character) < 32
        or ord(character) == 127
        or character in _WINDOWS_FORBIDDEN_CHARS
        for character in component
    ):
        return True
    normalized = unicodedata.normalize("NFC", component)
    device_stem = normalized.split(".", 1)[0].rstrip(" .").casefold()
    return device_stem in _WINDOWS_RESERVED_NAMES


def _portable_archive_path_key(normalized_name: str) -> str:
    path = normalized_name.rstrip("/")
    return "/".join(
        unicodedata.normalize("NFC", component).casefold()
        for component in path.split("/")
    )


def _record_archive_member(
    seen: set[str], *, kind: str, normalized_name: str
) -> None:
    key = _portable_archive_path_key(normalized_name)
    if key in seen:
        raise DistributionEvidenceError(
            f"{kind} archive contains a duplicate member path: {normalized_name!r}"
        )
    seen.add(key)


def _is_archive_metadata_member(normalized_name: str) -> bool:
    return normalized_name.rstrip("/").rsplit("/", 1)[-1] in {
        "LICENSE",
        "METADATA",
        "PKG-INFO",
    }


def _validate_archive_member_budget(
    *,
    kind: str,
    member_name: str,
    uncompressed_size: int,
    compressed_size: int | None = None,
    metadata: bool = False,
) -> None:
    if type(uncompressed_size) is not int or uncompressed_size < 0:
        raise DistributionEvidenceError(
            f"{kind} archive member has an invalid size: {member_name!r}"
        )
    if uncompressed_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise DistributionEvidenceError(
            f"{kind} archive member exceeds the {MAX_ARCHIVE_MEMBER_BYTES}-byte "
            f"uncompressed limit: {member_name!r}"
        )
    if metadata and uncompressed_size > MAX_ARCHIVE_METADATA_BYTES:
        raise DistributionEvidenceError(
            f"{kind} archive metadata member exceeds the "
            f"{MAX_ARCHIVE_METADATA_BYTES}-byte read limit: {member_name!r}"
        )
    if compressed_size is not None:
        if type(compressed_size) is not int or compressed_size < 0:
            raise DistributionEvidenceError(
                f"{kind} archive member has an invalid compressed size: "
                f"{member_name!r}"
            )
        if uncompressed_size > 0 and (
            compressed_size == 0
            or uncompressed_size
            > compressed_size * MAX_ARCHIVE_COMPRESSION_RATIO
        ):
            raise DistributionEvidenceError(
                f"{kind} archive member exceeds the "
                f"{MAX_ARCHIVE_COMPRESSION_RATIO}:1 compression-ratio limit: "
                f"{member_name!r}"
            )


def _validate_archive_totals(
    *,
    kind: str,
    member_count: int,
    uncompressed_size: int,
    compressed_archive_size: int | None = None,
) -> None:
    if member_count > MAX_ARCHIVE_MEMBERS:
        raise DistributionEvidenceError(
            f"{kind} archive exceeds the {MAX_ARCHIVE_MEMBERS}-member limit"
        )
    if uncompressed_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise DistributionEvidenceError(
            f"{kind} archive exceeds the {MAX_ARCHIVE_UNCOMPRESSED_BYTES}-byte "
            "total uncompressed limit"
        )
    if (
        compressed_archive_size is not None
        and uncompressed_size > 0
        and (
            compressed_archive_size <= 0
            or uncompressed_size
            > compressed_archive_size * MAX_ARCHIVE_COMPRESSION_RATIO
        )
    ):
        raise DistributionEvidenceError(
            f"{kind} archive exceeds the "
            f"{MAX_ARCHIVE_COMPRESSION_RATIO}:1 compression-ratio limit"
        )


def _read_bounded_archive_metadata(
    handle: BinaryIO, *, kind: str, member_name: str
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= MAX_ARCHIVE_METADATA_BYTES:
        block = handle.read(
            min(_ARCHIVE_SCAN_CHUNK_SIZE, MAX_ARCHIVE_METADATA_BYTES + 1 - total)
        )
        if not block:
            return b"".join(chunks)
        chunks.append(block)
        total += len(block)
    raise DistributionEvidenceError(
        f"{kind} archive metadata member exceeds the "
        f"{MAX_ARCHIVE_METADATA_BYTES}-byte read limit: {member_name!r}"
    )


def _reject_zip_member_type(kind: str, member: zipfile.ZipInfo) -> None:
    unix_mode = member.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        raise DistributionEvidenceError(
            f"{kind} archive contains a symbolic-link member: {member.filename!r}"
        )
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise DistributionEvidenceError(
            f"{kind} archive contains a non-regular member: {member.filename!r}"
        )


def _scan_archive_member(
    handle: BinaryIO, *, kind: str, member_name: str
) -> None:
    tail = b""
    while True:
        block = handle.read(_ARCHIVE_SCAN_CHUNK_SIZE)
        if not block:
            return
        candidate = tail + block
        qualified_specs = _TRELLIS_QUALIFIED_SPEC_RE.finditer(candidate)
        if any(
            match.group("spec") != _SUPPORTED_TRELLIS_INSTALL_SPEC
            for match in qualified_specs
        ) or _BARE_TRELLIS_INSTALL_RE.search(candidate) is not None:
            raise DistributionEvidenceError(
                f"{kind} archive contains a forbidden Trellis install spec "
                f"in member {member_name!r}"
            )
        tail = candidate[-_ARCHIVE_SCAN_OVERLAP:]


def _expected_license_bytes() -> bytes:
    license_path = _regular_file(REPOSITORY_ROOT / "LICENSE", kind="repository license")
    return license_path.read_bytes()


def _validate_package_metadata(
    metadata_bytes: bytes,
    *,
    kind: str,
    expected_version: str | None = None,
) -> str:
    try:
        metadata = BytesParser(policy=default_email_policy).parsebytes(metadata_bytes)
    except (TypeError, ValueError) as exc:
        raise DistributionEvidenceError(
            f"{kind} package metadata cannot be parsed"
        ) from exc
    if metadata.defects:
        raise DistributionEvidenceError(
            f"{kind} package metadata contains parser defects"
        )
    if metadata.get_all("Name", failobj=[]) != [PACKAGE_NAME]:
        raise DistributionEvidenceError(
            f"{kind} metadata must declare exactly one Name: {PACKAGE_NAME}"
        )
    versions = metadata.get_all("Version", failobj=[])
    if (
        len(versions) != 1
        or _CANONICAL_PACKAGE_VERSION_RE.fullmatch(versions[0]) is None
    ):
        raise DistributionEvidenceError(
            f"{kind} metadata must declare one canonical Version"
        )
    version = versions[0]
    if expected_version is not None and version != expected_version:
        raise DistributionEvidenceError(
            f"{kind} metadata Version must equal {expected_version}"
        )
    if metadata.get_all("License-Expression", failobj=[]) != [LICENSE_EXPRESSION]:
        raise DistributionEvidenceError(
            f"{kind} metadata must declare License-Expression: {LICENSE_EXPRESSION}"
        )
    license_files = metadata.get_all("License-File", failobj=[])
    if "LICENSE" not in license_files:
        raise DistributionEvidenceError(
            f"{kind} metadata must declare License-File: LICENSE"
        )
    return version


def _validate_archive_license(
    *,
    kind: str,
    license_members: list[bytes],
    metadata_members: list[bytes] | None = None,
    expected_version: str | None = None,
) -> str | None:
    if len(license_members) != 1:
        raise DistributionEvidenceError(
            f"{kind} archive must contain exactly one canonical LICENSE file"
        )
    if license_members[0] != _expected_license_bytes():
        raise DistributionEvidenceError(
            f"{kind} archive LICENSE does not match the repository GPL-3.0 text"
        )
    if metadata_members is not None:
        if len(metadata_members) != 1:
            raise DistributionEvidenceError(
                f"{kind} archive must contain exactly one package metadata file"
            )
        return _validate_package_metadata(
            metadata_members[0],
            kind=kind,
            expected_version=expected_version,
        )
    return None


def _inspect_zip_archive(
    path: Path,
    *,
    kind: str,
    expected_version: str | None = None,
) -> str | None:
    license_members: list[bytes] = []
    license_member_names: list[str] = []
    metadata_members: list[bytes] = []
    metadata_member_names: list[str] = []
    seen_members: set[str] = set()
    try:
        with zipfile.ZipFile(path, mode="r") as archive:
            members = archive.infolist()
            _validate_archive_totals(
                kind=kind,
                member_count=len(members),
                uncompressed_size=0,
            )
            prepared_members: list[tuple[zipfile.ZipInfo, str]] = []
            total_uncompressed = 0
            for member in members:
                normalized = _validated_archive_member_name(
                    kind, member.orig_filename
                )
                _record_archive_member(
                    seen_members,
                    kind=kind,
                    normalized_name=normalized,
                )
                _reject_zip_member_type(kind, member)
                if kind in {"skill_zip", "skill_zip_repeat"} and not normalized.startswith(
                    "wish-builder/"
                ):
                    raise DistributionEvidenceError(
                        f"{kind} archive member is outside the wish-builder root: "
                        f"{member.filename!r}"
                    )
                if member.is_dir():
                    if member.file_size != 0:
                        raise DistributionEvidenceError(
                            f"{kind} archive directory member contains data: "
                            f"{member.filename!r}"
                        )
                    continue
                _validate_archive_member_budget(
                    kind=kind,
                    member_name=member.filename,
                    uncompressed_size=member.file_size,
                    compressed_size=member.compress_size,
                    metadata=_is_archive_metadata_member(normalized),
                )
                total_uncompressed += member.file_size
                _validate_archive_totals(
                    kind=kind,
                    member_count=len(members),
                    uncompressed_size=total_uncompressed,
                )
                prepared_members.append((member, normalized))

            for member, normalized in prepared_members:
                with archive.open(member, mode="r") as handle:
                    _scan_archive_member(
                        handle,
                        kind=kind,
                        member_name=member.filename,
                    )
                if kind == "wheel":
                    if re.fullmatch(r"[^/]+\.dist-info/licenses/LICENSE", normalized):
                        with archive.open(member, mode="r") as handle:
                            license_members.append(
                                _read_bounded_archive_metadata(
                                    handle,
                                    kind=kind,
                                    member_name=member.filename,
                                )
                            )
                        license_member_names.append(normalized)
                    elif re.fullmatch(r"[^/]+\.dist-info/METADATA", normalized):
                        with archive.open(member, mode="r") as handle:
                            metadata_members.append(
                                _read_bounded_archive_metadata(
                                    handle,
                                    kind=kind,
                                    member_name=member.filename,
                                )
                            )
                        metadata_member_names.append(normalized)
                elif kind in {"skill_zip", "skill_zip_repeat"}:
                    if normalized == "wish-builder/LICENSE":
                        with archive.open(member, mode="r") as handle:
                            license_members.append(
                                _read_bounded_archive_metadata(
                                    handle,
                                    kind=kind,
                                    member_name=member.filename,
                                )
                            )
        if kind == "wheel":
            version = _validate_archive_license(
                kind=kind,
                license_members=license_members,
                metadata_members=metadata_members,
                expected_version=expected_version,
            )
            if version is None:
                raise DistributionEvidenceError("wheel metadata version is missing")
            expected_dist_info = f"{PACKAGE_FILENAME_NAME}-{version}.dist-info"
            if metadata_member_names != [f"{expected_dist_info}/METADATA"]:
                raise DistributionEvidenceError(
                    "wheel archive metadata path is not canonical"
                )
            if license_member_names != [f"{expected_dist_info}/licenses/LICENSE"]:
                raise DistributionEvidenceError(
                    "wheel archive license path is not canonical"
                )
            expected_filename = (
                f"{PACKAGE_FILENAME_NAME}-{version}-py3-none-any.whl"
            )
            if path.name != expected_filename:
                raise DistributionEvidenceError(
                    f"wheel filename must be canonical: {expected_filename}"
                )
            return version
        elif kind in {"skill_zip", "skill_zip_repeat"}:
            _validate_archive_license(kind=kind, license_members=license_members)
            return None
    except DistributionEvidenceError:
        raise
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise DistributionEvidenceError(
            f"{kind} archive cannot be inspected"
        ) from exc


def _inspect_sdist_archive(
    path: Path, *, expected_version: str | None = None
) -> str:
    kind = "sdist"
    license_members: list[bytes] = []
    license_member_names: list[str] = []
    metadata_members: list[bytes] = []
    metadata_member_names: list[str] = []
    seen_members: set[str] = set()
    try:
        compressed_archive_size = path.stat().st_size
        member_count = 0
        total_uncompressed = 0
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive:
                member_count += 1
                _validate_archive_totals(
                    kind=kind,
                    member_count=member_count,
                    uncompressed_size=total_uncompressed,
                    compressed_archive_size=compressed_archive_size,
                )
                normalized = _validated_archive_member_name(kind, member.name)
                _record_archive_member(
                    seen_members,
                    kind=kind,
                    normalized_name=normalized,
                )
                if member.isdir():
                    continue
                if not member.isfile():
                    raise DistributionEvidenceError(
                        f"{kind} archive contains a non-regular member: "
                        f"{member.name!r}"
                    )
                _validate_archive_member_budget(
                    kind=kind,
                    member_name=member.name,
                    uncompressed_size=member.size,
                    metadata=_is_archive_metadata_member(normalized),
                )
                total_uncompressed += member.size
                _validate_archive_totals(
                    kind=kind,
                    member_count=member_count,
                    uncompressed_size=total_uncompressed,
                    compressed_archive_size=compressed_archive_size,
                )
                handle = archive.extractfile(member)
                if handle is None:
                    raise DistributionEvidenceError(
                        f"{kind} archive member cannot be inspected: {member.name!r}"
                    )
                with handle:
                    _scan_archive_member(
                        handle,
                        kind=kind,
                        member_name=member.name,
                    )
                if re.fullmatch(r"[^/]+/LICENSE", normalized):
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise DistributionEvidenceError(
                            f"{kind} archive member cannot be inspected: {member.name!r}"
                        )
                    with extracted:
                        license_members.append(
                            _read_bounded_archive_metadata(
                                extracted,
                                kind=kind,
                                member_name=member.name,
                            )
                        )
                    license_member_names.append(normalized)
                elif re.fullmatch(r"[^/]+/PKG-INFO", normalized):
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise DistributionEvidenceError(
                            f"{kind} archive member cannot be inspected: {member.name!r}"
                        )
                    with extracted:
                        metadata_members.append(
                            _read_bounded_archive_metadata(
                                extracted,
                                kind=kind,
                                member_name=member.name,
                            )
                        )
                    metadata_member_names.append(normalized)
        version = _validate_archive_license(
            kind=kind,
            license_members=license_members,
            metadata_members=metadata_members,
            expected_version=expected_version,
        )
        if version is None:
            raise DistributionEvidenceError("sdist metadata version is missing")
        expected_root = f"{PACKAGE_FILENAME_NAME}-{version}"
        if metadata_member_names != [f"{expected_root}/PKG-INFO"]:
            raise DistributionEvidenceError(
                "sdist archive metadata path is not canonical"
            )
        if license_member_names != [f"{expected_root}/LICENSE"]:
            raise DistributionEvidenceError(
                "sdist archive license path is not canonical"
            )
        expected_filename = f"{expected_root}.tar.gz"
        if path.name != expected_filename:
            raise DistributionEvidenceError(
                f"sdist filename must be canonical: {expected_filename}"
            )
        return version
    except DistributionEvidenceError:
        raise
    except (EOFError, OSError, tarfile.TarError) as exc:
        raise DistributionEvidenceError(
            f"{kind} archive cannot be inspected"
        ) from exc


def validate_distribution_artifacts(
    wheel: Path,
    sdist: Path,
    skill_zip: Path,
    skill_zip_repeat: Path,
    *,
    expected_version: str | None = None,
) -> str:
    """Strictly inspect canonical archive bytes and return their package version."""
    resolved = (
        _regular_file(wheel, kind="wheel"),
        _regular_file(sdist, kind="sdist"),
        _regular_file(skill_zip, kind="skill_zip"),
        _regular_file(skill_zip_repeat, kind="skill_zip_repeat"),
    )
    if len(set(resolved)) != len(resolved):
        raise DistributionEvidenceError(
            "distribution roles must reference distinct files"
        )
    if resolved[2].suffix != ".zip" or resolved[3].suffix != ".zip":
        raise DistributionEvidenceError("Skill distribution files must use .zip")

    wheel_version = _inspect_zip_archive(
        resolved[0], kind="wheel", expected_version=expected_version
    )
    sdist_version = _inspect_sdist_archive(
        resolved[1], expected_version=expected_version
    )
    _inspect_zip_archive(resolved[2], kind="skill_zip")
    _inspect_zip_archive(resolved[3], kind="skill_zip_repeat")
    if _sha256(resolved[2]) != _sha256(resolved[3]):
        raise DistributionEvidenceError(
            "repeated deterministic Skill ZIP build has different raw bytes"
        )
    if wheel_version is None:
        raise DistributionEvidenceError("wheel metadata version is missing")
    if wheel_version != sdist_version:
        raise DistributionEvidenceError(
            "wheel and sdist metadata Version values do not match"
        )
    return wheel_version


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DistributionEvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_object(path: Path) -> dict[str, Any]:
    source = _regular_file(path, kind="distribution evidence")
    try:
        value = json.loads(
            source.read_bytes(),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                DistributionEvidenceError(f"invalid JSON constant: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DistributionEvidenceError("distribution evidence is not valid JSON") from exc
    if type(value) is not dict:
        raise DistributionEvidenceError("distribution evidence must be a JSON object")
    return value


def _require_exact_fields(
    value: dict[str, Any], expected: set[str], *, label: str
) -> None:
    if set(value) != expected:
        raise DistributionEvidenceError(f"{label} fields do not match schema")


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise DistributionEvidenceError(f"{label} must be a sha256 digest")
    return value


def _validated_build_artifacts(
    report: dict[str, Any], dist_dir: Path, candidate: str
) -> tuple[str, list[dict[str, object]], str]:
    _reject_top_level_npm_tarballs(dist_dir)
    _require_exact_fields(
        report,
        {
            "artifact_count",
            "artifacts",
            "evidence_digest",
            "github_sha",
            "revision",
            "schema_version",
            "skill_zip_deterministic",
            "status",
        },
        label="distribution evidence",
    )
    if (
        report["schema_version"] != SCHEMA_VERSION
        or report["status"] != "passed"
        or report["skill_zip_deterministic"] is not True
    ):
        raise DistributionEvidenceError("distribution build evidence did not pass")
    if (
        _require_revision(report["revision"]) != candidate
        or report["github_sha"] != candidate
    ):
        raise DistributionEvidenceError(
            "distribution build evidence is bound to another revision"
        )
    evidence_digest = _require_digest(
        report["evidence_digest"], label="distribution evidence digest"
    )
    digest_input = dict(report)
    digest_input.pop("evidence_digest")
    expected_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(digest_input)
    ).hexdigest()
    if evidence_digest != expected_digest:
        raise DistributionEvidenceError("distribution evidence digest is inconsistent")

    artifacts = report["artifacts"]
    if (
        type(artifacts) is not list
        or report["artifact_count"] != 4
        or len(artifacts) != 4
    ):
        raise DistributionEvidenceError("distribution artifact set is incomplete")
    expected_kinds = {"wheel", "sdist", "skill_zip", "skill_zip_repeat"}
    normalized: list[dict[str, object]] = []
    seen_kinds: set[str] = set()
    seen_paths: set[str] = set()
    root = dist_dir.resolve(strict=True)
    if dist_dir.is_symlink() or not root.is_dir():
        raise DistributionEvidenceError("distribution root must be a regular directory")
    for item in artifacts:
        if type(item) is not dict:
            raise DistributionEvidenceError("distribution artifact must be an object")
        _require_exact_fields(
            item, {"kind", "path", "sha256", "size_bytes"}, label="artifact"
        )
        kind = item["kind"]
        filename = item["path"]
        size = item["size_bytes"]
        if type(kind) is not str or kind not in expected_kinds:
            raise DistributionEvidenceError("distribution artifact kind is invalid")
        if (
            type(filename) is not str
            or filename != Path(filename).name
            or "\\" in filename
            or "\x00" in filename
        ):
            raise DistributionEvidenceError("distribution artifact path is invalid")
        if type(size) is not int or isinstance(size, bool) or size <= 0:
            raise DistributionEvidenceError("distribution artifact size is invalid")
        digest = _require_digest(item["sha256"], label=f"{kind} digest")
        artifact_path = _regular_file(root / filename, kind=kind)
        if artifact_path.parent != root:
            raise DistributionEvidenceError("distribution artifact escapes its root")
        if artifact_path.stat().st_size != size or _sha256(artifact_path) != digest:
            raise DistributionEvidenceError(
                f"{kind} file does not match distribution build evidence"
            )
        if kind in seen_kinds or filename in seen_paths:
            raise DistributionEvidenceError("distribution artifacts are duplicated")
        seen_kinds.add(kind)
        seen_paths.add(filename)
        normalized.append(
            {"kind": kind, "path": filename, "sha256": digest, "size_bytes": size}
        )
    if seen_kinds != expected_kinds:
        raise DistributionEvidenceError("distribution artifact roles are incomplete")
    by_kind = {str(item["kind"]): item for item in normalized}
    if by_kind["skill_zip"]["sha256"] != by_kind["skill_zip_repeat"]["sha256"]:
        raise DistributionEvidenceError("Skill ZIP build is not deterministic")
    package_version = validate_distribution_artifacts(
        root / str(by_kind["wheel"]["path"]),
        root / str(by_kind["sdist"]["path"]),
        root / str(by_kind["skill_zip"]["path"]),
        root / str(by_kind["skill_zip_repeat"]["path"]),
    )
    return evidence_digest, normalized, package_version


def build_distribution_evidence(
    dist_dir: Path,
    skill_zip: Path,
    skill_zip_repeat: Path,
    *,
    revision: str,
    expected_version: str | None = None,
) -> dict[str, object]:
    """Return strict raw-file evidence for one candidate revision."""
    candidate = _require_revision(revision)
    _reject_top_level_npm_tarballs(dist_dir)
    wheel = _single_distribution(dist_dir, "*.whl", kind="wheel")
    sdist = _single_distribution(dist_dir, "*.tar.gz", kind="sdist")
    first_zip = _regular_file(skill_zip, kind="skill_zip")
    repeated_zip = _regular_file(skill_zip_repeat, kind="skill_zip_repeat")
    if first_zip.suffix != ".zip" or repeated_zip.suffix != ".zip":
        raise DistributionEvidenceError("Skill distribution files must use .zip")

    validate_distribution_artifacts(
        wheel,
        sdist,
        first_zip,
        repeated_zip,
        expected_version=expected_version,
    )

    artifacts = [
        _artifact("wheel", wheel),
        _artifact("sdist", sdist),
        _artifact("skill_zip", first_zip),
        _artifact("skill_zip_repeat", repeated_zip),
    ]
    if artifacts[2]["sha256"] != artifacts[3]["sha256"]:
        raise DistributionEvidenceError(
            "repeated deterministic Skill ZIP build has different raw bytes"
        )
    body: dict[str, object] = {
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "github_sha": candidate,
        "revision": candidate,
        "schema_version": SCHEMA_VERSION,
        "skill_zip_deterministic": True,
        "status": "passed",
    }
    body["evidence_digest"] = "sha256:" + hashlib.sha256(
        canonical_json_bytes(body)
    ).hexdigest()
    return body


_INSTALLED_PACKAGE_CHECK = (
    "import sys; from importlib.metadata import version as package_version; "
    "from wish_builder.compatibility import "
    "load_bundled_backend_qualification, load_bundled_trellis_compatibility; "
    "expected_version=sys.argv[1]; expected='0.6.15'; "
    "assert package_version('wish-builder') == expected_version; "
    "trellis=load_bundled_trellis_compatibility(); "
    "backend=load_bundled_backend_qualification(); "
    "assert trellis.trellis_version == expected; "
    "assert backend.trellis_compatibility_digest == trellis.compatibility_digest; "
    "assert tuple((item.name, item.version) for item in trellis.packages) == "
    "(('@mindfoldhq/trellis', expected), "
    "('@mindfoldhq/trellis-core', expected)); "
    "assert trellis.published is True; "
    "assert b'@latest' not in trellis.canonical_json_bytes()"
)


def _venv_executable(environment: Path, name: str) -> Path:
    if os.name == "nt":
        suffix = ".exe" if name in {"python", "wishctl"} else ""
        return environment / "Scripts" / f"{name}{suffix}"
    return environment / "bin" / name


def _run_checked(arguments: list[str]) -> None:
    subprocess.run(arguments, check=True)


def _smoke_install(
    artifact: Path,
    *,
    kind: str,
    environment: Path,
    expected_version: str,
) -> None:
    venv.EnvBuilder(with_pip=True, clear=False).create(environment)
    python = _venv_executable(environment, "python")
    if kind == "sdist":
        _run_checked(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-deps",
                "setuptools==83.0.0",
                "wheel==0.47.0",
            ]
        )
    install = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
    ]
    if kind == "sdist":
        install.append("--no-build-isolation")
    install.append(str(artifact))
    _run_checked(install)
    _run_checked([str(_venv_executable(environment, "wishctl")), "--help"])
    _run_checked(
        [str(python), "-I", "-c", _INSTALLED_PACKAGE_CHECK, expected_version]
    )


def _expected_runtime_platform() -> str:
    if sys.platform == "win32":
        return "windows-latest"
    if sys.platform.startswith("linux"):
        return "ubuntu-latest"
    raise DistributionEvidenceError(
        f"unsupported clean-install runtime platform: {sys.platform}"
    )


def build_clean_install_evidence(
    dist_dir: Path,
    build_evidence_path: Path,
    *,
    revision: str,
    platform_name: str,
    python_version: str,
    cell_id: str,
    work_dir: Path | None = None,
) -> dict[str, object]:
    """Install one canonical wheel and sdist and bind the result to one CI cell."""
    candidate = _require_revision(revision)
    if platform_name not in EXPECTED_PLATFORMS:
        raise DistributionEvidenceError("clean-install platform is unsupported")
    if python_version not in EXPECTED_PYTHON_VERSIONS:
        raise DistributionEvidenceError("clean-install Python version is unsupported")
    expected_cell_id = f"{platform_name}-py{python_version}"
    if cell_id != expected_cell_id:
        raise DistributionEvidenceError("clean-install cell id is inconsistent")
    actual_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_version != python_version:
        raise DistributionEvidenceError(
            f"clean-install runtime is Python {actual_version}, expected {python_version}"
        )
    actual_platform = _expected_runtime_platform()
    if actual_platform != platform_name:
        raise DistributionEvidenceError(
            f"clean-install runtime is {actual_platform}, expected {platform_name}"
        )

    report_path = _regular_file(
        build_evidence_path, kind="distribution build evidence"
    )
    build_evidence_sha256 = _sha256(report_path)
    report = _read_json_object(report_path)
    build_digest, all_artifacts, package_version = _validated_build_artifacts(
        report, dist_dir, candidate
    )
    install_artifacts = sorted(
        (
            item
            for item in all_artifacts
            if item["kind"] in {"wheel", "sdist"}
        ),
        key=lambda item: str(item["kind"]),
    )
    root = dist_dir.resolve(strict=True)
    temporary_parent: Path | None = None
    if work_dir is not None:
        temporary_parent = work_dir.resolve(strict=True)
        if work_dir.is_symlink() or not temporary_parent.is_dir():
            raise DistributionEvidenceError("clean-install work root is invalid")
    with tempfile.TemporaryDirectory(
        prefix="wish-builder-clean-install-",
        dir=temporary_parent,
    ) as temporary:
        smoke_root = Path(temporary)
        for item in install_artifacts:
            kind = str(item["kind"])
            _smoke_install(
                root / str(item["path"]),
                kind=kind,
                environment=smoke_root / kind,
                expected_version=package_version,
            )

    stable_report = _regular_file(report_path, kind="distribution build evidence")
    if _sha256(stable_report) != build_evidence_sha256:
        raise DistributionEvidenceError(
            "distribution build evidence changed during clean installation"
        )
    for item in install_artifacts:
        kind = str(item["kind"])
        path = _regular_file(root / str(item["path"]), kind=kind)
        if (
            path.stat().st_size != item["size_bytes"]
            or _sha256(path) != item["sha256"]
        ):
            raise DistributionEvidenceError(
                f"{kind} changed during clean installation"
            )

    body: dict[str, object] = {
        "artifacts": install_artifacts,
        "cell_id": expected_cell_id,
        "distribution_evidence_digest": build_digest,
        "distribution_evidence_sha256": build_evidence_sha256,
        "github_sha": candidate,
        "installations": [
            {
                "artifact_kind": str(item["kind"]),
                "artifact_sha256": str(item["sha256"]),
                "status": "passed",
            }
            for item in install_artifacts
        ],
        "platform": platform_name,
        "python_version": python_version,
        "revision": candidate,
        "runtime": {
            "implementation": sys.implementation.name,
            "python_full_version": ".".join(str(part) for part in sys.version_info[:3]),
            "sys_platform": sys.platform,
        },
        "schema_version": CELL_SCHEMA_VERSION,
        "status": "passed",
    }
    body["evidence_digest"] = "sha256:" + hashlib.sha256(
        canonical_json_bytes(body)
    ).hexdigest()
    return body


def _write_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("build", "verify-cell"), default="build"
    )
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--skill-zip", type=Path)
    parser.add_argument("--skill-zip-repeat", type=Path)
    parser.add_argument("--build-evidence", type=Path)
    parser.add_argument("--cell-id")
    parser.add_argument("--platform")
    parser.add_argument("--python-version")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA"))
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.mode == "build":
            if arguments.skill_zip is None or arguments.skill_zip_repeat is None:
                raise DistributionEvidenceError(
                    "build mode requires --skill-zip and --skill-zip-repeat"
                )
            result = build_distribution_evidence(
                arguments.dist_dir,
                arguments.skill_zip,
                arguments.skill_zip_repeat,
                revision=arguments.revision,
            )
        else:
            if (
                arguments.build_evidence is None
                or arguments.cell_id is None
                or arguments.platform is None
                or arguments.python_version is None
            ):
                raise DistributionEvidenceError(
                    "verify-cell mode requires build evidence and complete cell identity"
                )
            result = build_clean_install_evidence(
                arguments.dist_dir,
                arguments.build_evidence,
                revision=arguments.revision,
                platform_name=arguments.platform,
                python_version=arguments.python_version,
                cell_id=arguments.cell_id,
                work_dir=arguments.work_dir,
            )
        exit_code = 0
    except (DistributionEvidenceError, OSError, subprocess.CalledProcessError) as exc:
        result = {
            "cell_id": arguments.cell_id if arguments.mode == "verify-cell" else None,
            "error": str(exc) or type(exc).__name__,
            "evidence_digest": None,
            "github_sha": arguments.revision,
            "mode": arguments.mode,
            "platform": (
                arguments.platform if arguments.mode == "verify-cell" else None
            ),
            "python_version": (
                arguments.python_version if arguments.mode == "verify-cell" else None
            ),
            "revision": arguments.revision,
            "schema_version": (
                CELL_SCHEMA_VERSION
                if arguments.mode == "verify-cell"
                else SCHEMA_VERSION
            ),
            "status": "error",
        }
        exit_code = 2
    encoded = canonical_json_bytes(result)
    try:
        _write_atomic(arguments.output, encoded)
    except OSError as exc:
        print(f"cannot write distribution evidence: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(encoded)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
