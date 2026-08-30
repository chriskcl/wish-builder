#!/usr/bin/env python3
"""Generate and package the standalone Skill runtime deterministically."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "wish-builder"
OUTPUT = REPOSITORY_ROOT / "wish-builder-skill.zip"
RUNTIME_MANIFEST_VERSION = 1
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {"LICENSE"}


class RuntimeDriftError(RuntimeError):
    """Raised when generated Skill runtime content is not source-identical."""


def _is_ignored_runtime_path(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}


def _reject_tgz(path: Path, root: Path, *, context: str) -> None:
    if path.suffix.lower() == ".tgz":
        relative = path.relative_to(root).as_posix()
        raise RuntimeDriftError(
            f"{context} must not contain npm tarballs: {relative}"
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _filesystem_path(path: Path) -> Path:
    """Return a Windows extended-length path for internal filesystem access."""

    if os.name != "nt":
        return path
    value = os.path.abspath(os.fspath(path))
    if value.startswith("\\\\?\\"):
        return Path(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def authoritative_runtime_files(
    repository_root: Path = REPOSITORY_ROOT,
) -> list[Path]:
    """Return every package-owned runtime file in stable path order."""
    package_root = repository_root / "wish_builder"
    if not package_root.is_dir() or package_root.is_symlink():
        raise RuntimeDriftError(
            "authoritative runtime root is missing or is a symlink: wish_builder"
        )
    files: list[Path] = []
    search_root = _filesystem_path(package_root)
    for discovered in search_root.rglob("*"):
        path = package_root / discovered.relative_to(search_root)
        if _is_ignored_runtime_path(path):
            continue
        _reject_tgz(path, repository_root, context="authoritative runtime")
        if discovered.is_symlink():
            raise RuntimeDriftError(
                f"authoritative runtime source must not be a symlink: "
                f"{path.relative_to(repository_root).as_posix()}"
            )
        if discovered.is_file():
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(repository_root).as_posix())


def runtime_file_map(
    repository_root: Path = REPOSITORY_ROOT,
) -> list[tuple[Path, Path]]:
    """Map authoritative sources to paths relative to the standalone Skill."""
    package_root = repository_root / "wish_builder"
    mappings = [
        (
            source,
            Path("scripts") / "wish_builder" / source.relative_to(package_root),
        )
        for source in authoritative_runtime_files(repository_root)
    ]
    cli_source = package_root / "cli" / "wishctl.py"
    if not cli_source.is_file() or cli_source.is_symlink():
        raise RuntimeDriftError(
            "authoritative CLI source is missing or is a symlink: "
            "wish_builder/cli/wishctl.py"
        )
    mappings.append((cli_source, Path("scripts") / "wishctl.py"))
    qualification_source = repository_root / "scripts" / "ci_backend_qualification.py"
    if not qualification_source.is_file() or qualification_source.is_symlink():
        raise RuntimeDriftError(
            "qualification CLI source is missing or is a symlink: "
            "scripts/ci_backend_qualification.py"
        )
    mappings.append(
        (
            qualification_source,
            Path("scripts") / "ci_backend_qualification.py",
        )
    )
    return sorted(mappings, key=lambda item: item[1].as_posix())


def runtime_manifest_bytes(
    repository_root: Path = REPOSITORY_ROOT,
) -> bytes:
    """Describe the complete generated runtime using source byte hashes."""
    skill_root = repository_root / "wish-builder"
    files = []
    for source, relative_destination in runtime_file_map(repository_root):
        # Git may materialize text files as CRLF or LF. Manifest hashes describe
        # the canonical distribution bytes so the tracked manifest is portable.
        source_content = archive_bytes(source)
        runtime_content = source_content
        files.append(
            {
                "destination": relative_destination.as_posix(),
                "runtime_sha256": _sha256(runtime_content),
                "runtime_size": len(runtime_content),
                "source": source.relative_to(repository_root).as_posix(),
                "source_sha256": _sha256(source_content),
                "source_size": len(source_content),
            }
        )
    manifest = {
        "files": files,
        "generated_root": (
            (skill_root / "scripts" / "wish_builder")
            .relative_to(skill_root)
            .as_posix()
        ),
        "generator": "scripts/sync_skill_runtime.py",
        "schema_version": RUNTIME_MANIFEST_VERSION,
    }
    return (
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _generated_runtime_files(skill_root: Path) -> set[Path]:
    generated_root = skill_root / "scripts" / "wish_builder"
    search_root = _filesystem_path(generated_root)
    if not search_root.exists():
        return set()
    files: set[Path] = set()
    for path in search_root.rglob("*"):
        relative = path.relative_to(search_root)
        if _is_ignored_runtime_path(relative):
            continue
        if path.is_symlink() or path.is_file():
            files.add(Path("scripts") / "wish_builder" / relative)
    return files


def _assert_no_symlink_ancestors(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeDriftError(f"generated path escapes the Skill root: {path}") from exc
    current = root
    if _filesystem_path(current).is_symlink():
        raise RuntimeDriftError(f"generated path has a symlink ancestor: {current}")
    for part in relative.parts[:-1]:
        current /= part
        if _filesystem_path(current).is_symlink():
            raise RuntimeDriftError(f"generated path has a symlink ancestor: {current}")


def runtime_drift(
    repository_root: Path = REPOSITORY_ROOT,
) -> list[str]:
    """Return stable diagnostics for stale or unexpected generated content."""
    skill_root = repository_root / "wish-builder"
    expected_map = {
        destination: source
        for source, destination in runtime_file_map(repository_root)
    }
    diagnostics: list[str] = []

    expected_generated = {
        destination
        for destination in expected_map
        if destination.parts[:2] == ("scripts", "wish_builder")
    }
    actual_generated = _generated_runtime_files(skill_root)
    scripts_root = skill_root / "scripts"
    generated_root = skill_root / "scripts" / "wish_builder"
    if scripts_root.is_symlink():
        diagnostics.append("generated runtime ancestor must not be a symlink: scripts")
    if generated_root.is_symlink():
        diagnostics.append("generated runtime root must not be a symlink: scripts/wish_builder")
    for unexpected in sorted(actual_generated - expected_generated):
        diagnostics.append(f"unexpected generated runtime file: {unexpected.as_posix()}")
    for missing in sorted(expected_generated - actual_generated):
        diagnostics.append(f"missing generated runtime file: {missing.as_posix()}")

    for relative_destination, source in sorted(
        expected_map.items(), key=lambda item: item[0].as_posix()
    ):
        destination = skill_root / relative_destination
        filesystem_destination = _filesystem_path(destination)
        if not filesystem_destination.exists():
            if relative_destination not in expected_generated:
                diagnostics.append(
                    f"missing generated runtime file: {relative_destination.as_posix()}"
                )
            continue
        if filesystem_destination.is_symlink():
            diagnostics.append(
                f"generated runtime file must not be a symlink: "
                f"{relative_destination.as_posix()}"
            )
            continue
        if not filesystem_destination.is_file():
            diagnostics.append(
                f"generated runtime path is not a file: "
                f"{relative_destination.as_posix()}"
            )
            continue
        if filesystem_destination.read_bytes() != _filesystem_path(source).read_bytes():
            diagnostics.append(
                f"stale generated runtime file: {relative_destination.as_posix()} "
                f"(source: {source.relative_to(repository_root).as_posix()})"
            )

    manifest_path = skill_root / "scripts" / "runtime-manifest.json"
    expected_manifest = runtime_manifest_bytes(repository_root)
    if not manifest_path.is_file() or manifest_path.is_symlink():
        diagnostics.append("missing generated runtime manifest: scripts/runtime-manifest.json")
    elif manifest_path.read_bytes() != expected_manifest:
        diagnostics.append("stale generated runtime manifest: scripts/runtime-manifest.json")
    return diagnostics


def assert_runtime_current(
    repository_root: Path = REPOSITORY_ROOT,
) -> None:
    diagnostics = runtime_drift(repository_root)
    if diagnostics:
        details = "\n".join(f"- {diagnostic}" for diagnostic in diagnostics)
        raise RuntimeDriftError(
            "standalone Skill runtime is stale; run "
            "`python scripts/sync_skill_runtime.py`:\n" + details
        )


def sync_runtime(
    repository_root: Path = REPOSITORY_ROOT,
) -> None:
    """Replace generated runtime bytes with their authoritative source bytes."""
    skill_root = repository_root / "wish-builder"
    generated_root = skill_root / "scripts" / "wish_builder"
    expected_map = {
        destination: source
        for source, destination in runtime_file_map(repository_root)
    }
    expected_generated = {
        destination
        for destination in expected_map
        if destination.parts[:2] == ("scripts", "wish_builder")
    }

    for relative_path in sorted(
        _generated_runtime_files(skill_root) - expected_generated,
        reverse=True,
    ):
        path = skill_root / relative_path
        filesystem_path = _filesystem_path(path)
        if filesystem_path.is_dir() and not filesystem_path.is_symlink():
            shutil.rmtree(filesystem_path)
        else:
            filesystem_path.unlink()

    for relative_destination, source in sorted(
        expected_map.items(), key=lambda item: item[0].as_posix()
    ):
        destination = skill_root / relative_destination
        _assert_no_symlink_ancestors(destination, skill_root)
        filesystem_destination = _filesystem_path(destination)
        filesystem_destination.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_ancestors(destination, skill_root)
        if filesystem_destination.is_symlink() or (
            filesystem_destination.exists() and not filesystem_destination.is_file()
        ):
            raise RuntimeDriftError(
                f"refusing to replace non-file generated path: "
                f"{relative_destination.as_posix()}"
            )
        filesystem_destination.write_bytes(_filesystem_path(source).read_bytes())

    manifest_path = skill_root / "scripts" / "runtime-manifest.json"
    _assert_no_symlink_ancestors(manifest_path, skill_root)
    if manifest_path.is_symlink() or (
        manifest_path.exists() and not manifest_path.is_file()
    ):
        raise RuntimeDriftError(
            "refusing to replace non-file generated path: scripts/runtime-manifest.json"
        )
    manifest_path.write_bytes(runtime_manifest_bytes(repository_root))
    assert_runtime_current(repository_root)

    # Generated package imports can leave caches; they are never source artifacts.
    for cache in _filesystem_path(generated_root).rglob("__pycache__"):
        if cache.is_dir() and not cache.is_symlink():
            shutil.rmtree(cache)


def distributable_files(skill_root: Path = SKILL_ROOT) -> list[Path]:
    files: list[Path] = []
    search_root = _filesystem_path(skill_root)
    for discovered in search_root.rglob("*"):
        path = skill_root / discovered.relative_to(search_root)
        if _is_ignored_runtime_path(path):
            continue
        _reject_tgz(path, skill_root, context="Skill distribution")
        if discovered.is_file():
            files.append(path)
    return sorted(files, key=lambda path: path.as_posix())


def archive_bytes(path: Path) -> bytes:
    data = _filesystem_path(path).read_bytes()
    if (
        path.suffix.lower() not in TEXT_SUFFIXES
        and path.name not in TEXT_FILENAMES
    ):
        return data
    text = data.decode("utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _reject_distributable_symlinks(paths: Iterable[Path], skill_root: Path) -> None:
    for path in paths:
        if _filesystem_path(path).is_symlink():
            raise RuntimeDriftError(
                "Skill archives cannot contain symlinks: "
                f"{path.relative_to(skill_root).as_posix()}"
            )


def build(output: Path = OUTPUT, repository_root: Path = REPOSITORY_ROOT) -> str:
    skill_root = repository_root / "wish-builder"
    assert_runtime_current(repository_root)
    files = distributable_files(skill_root)
    _reject_distributable_symlinks(files, skill_root)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in files:
                relative = path.relative_to(repository_root).as_posix()
                info = zipfile.ZipInfo(relative, FIXED_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, archive_bytes(path))
        assert_runtime_current(repository_root)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    digest = build()
    print(f"sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
