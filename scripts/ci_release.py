#!/usr/bin/env python3
"""Promote one green, revision-bound CI artifact set into release assets."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Mapping, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.ci_distribution_evidence import (
    DistributionEvidenceError,
    LICENSE_EXPRESSION,
    build_distribution_evidence,
    canonical_json_bytes,
)
from scripts.ci_evidence_packet import (
    SCHEMA_VERSION as EVIDENCE_PACKET_SCHEMA_VERSION,
)


RELEASE_MANIFEST_SCHEMA_VERSION = 1
GPL3_CANONICAL_TEXT_SHA256 = (
    "sha256:3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
)
_REVISION_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\.dev[0-9]+\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReleasePromotionError(ValueError):
    """Raised when CI artifacts cannot prove a safe release promotion."""


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleasePromotionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    source = _regular_file(path, label=label)
    raw = source.read_bytes()
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ReleasePromotionError(f"invalid JSON constant: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleasePromotionError(f"{label} is not valid JSON") from exc
    if type(value) is not dict:
        raise ReleasePromotionError(f"{label} must be a JSON object")
    return value, raw


def _regular_file(path: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReleasePromotionError(f"{label} is missing: {path}") from exc
    if path.is_symlink() or not resolved.is_file():
        raise ReleasePromotionError(f"{label} must be a regular non-symlink file")
    return resolved


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _require_revision(value: object) -> str:
    if type(value) is not str or _REVISION_RE.fullmatch(value) is None:
        raise ReleasePromotionError("revision must be a lowercase commit id")
    return value


def _require_version(value: object) -> str:
    if type(value) is not str or _VERSION_RE.fullmatch(value) is None:
        raise ReleasePromotionError(
            "version must be a three-part development prerelease"
        )
    return value


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ReleasePromotionError(f"{label} must be a sha256 digest")
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ReleasePromotionError(f"{label} must be a positive integer")
    return value


def _validated_gpl3_license(path: Path) -> Path:
    license_path = _regular_file(path, label="repository license")
    normalized = license_path.read_bytes().replace(b"\r\n", b"\n")
    if b"\r" in normalized:
        raise ReleasePromotionError("repository LICENSE has invalid line endings")
    normalized = normalized.rstrip(b"\n") + b"\n"
    if _sha256_bytes(normalized) != GPL3_CANONICAL_TEXT_SHA256:
        raise ReleasePromotionError(
            "repository LICENSE is not the canonical GNU GPL version 3 text"
        )
    return license_path


def _project_versions(repository_root: Path) -> dict[str, str]:
    pyproject = tomllib.loads(
        _regular_file(repository_root / "pyproject.toml", label="pyproject")
        .read_text(encoding="utf-8")
    )
    try:
        project_version = pyproject["project"]["version"]
        license_expression = pyproject["project"]["license"]
    except (KeyError, TypeError) as exc:
        raise ReleasePromotionError("pyproject release metadata is incomplete") from exc
    if license_expression != LICENSE_EXPRESSION:
        raise ReleasePromotionError("pyproject license expression is not GPL-3.0-only")

    init_path = _regular_file(
        repository_root / "wish_builder" / "__init__.py",
        label="package version module",
    )
    try:
        module = ast.parse(init_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise ReleasePromotionError("package version module cannot be parsed") from exc
    package_versions = [
        node.value.value
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) is str
    ]
    if len(package_versions) != 1:
        raise ReleasePromotionError("package must declare exactly one __version__")

    lock = tomllib.loads(
        _regular_file(repository_root / "uv.lock", label="uv lock")
        .read_text(encoding="utf-8")
    )
    lock_versions = [
        item.get("version")
        for item in lock.get("package", [])
        if type(item) is dict and item.get("name") == "wish-builder"
    ]
    if len(lock_versions) != 1 or type(lock_versions[0]) is not str:
        raise ReleasePromotionError("uv lock must contain one Wish Builder version")
    if type(project_version) is not str:
        raise ReleasePromotionError("pyproject version must be a string")
    return {
        "package": package_versions[0],
        "project": project_version,
        "uv_lock": lock_versions[0],
    }


def _validate_packet(
    packet_path: Path,
    digest_path: Path,
    *,
    revision: str,
    ci_run_id: int,
    ci_run_attempt: int,
) -> tuple[dict[str, Any], bytes]:
    packet, raw = _read_json(packet_path, label="active M1 evidence packet")
    if raw != canonical_json_bytes(packet):
        raise ReleasePromotionError("active M1 evidence packet is not canonical JSON")
    if (
        packet.get("schema_version") != EVIDENCE_PACKET_SCHEMA_VERSION
        or packet.get("status") != "passed"
        or packet.get("candidate_revision") != revision
    ):
        raise ReleasePromotionError("active M1 evidence packet did not pass for revision")
    workflow = packet.get("workflow")
    if type(workflow) is not dict or workflow.get("run_id") != ci_run_id:
        raise ReleasePromotionError("active M1 evidence packet belongs to another CI run")
    if workflow.get("run_attempt") != ci_run_attempt:
        raise ReleasePromotionError(
            "active M1 evidence packet belongs to another CI run attempt"
        )
    declared = _require_digest(packet.get("packet_digest"), label="packet digest")
    digest_input = dict(packet)
    digest_input.pop("packet_digest")
    if declared != _sha256_bytes(canonical_json_bytes(digest_input)):
        raise ReleasePromotionError("active M1 packet digest is inconsistent")

    digest_file = _regular_file(digest_path, label="active M1 packet digest")
    try:
        raw_digest = digest_file.read_text(encoding="ascii").strip()
    except UnicodeDecodeError as exc:
        raise ReleasePromotionError("active M1 packet digest is not ASCII") from exc
    if raw_digest != _sha256_bytes(raw):
        raise ReleasePromotionError("active M1 packet raw digest does not match")
    return packet, raw


def _artifact_map(items: object) -> dict[str, Mapping[str, object]]:
    if type(items) is not list or len(items) != 4:
        raise ReleasePromotionError("evidence packet distribution is incomplete")
    result: dict[str, Mapping[str, object]] = {}
    for item in items:
        if type(item) is not dict:
            raise ReleasePromotionError("distribution artifact entry is invalid")
        kind = item.get("kind")
        if type(kind) is not str or kind in result:
            raise ReleasePromotionError("distribution artifact roles are invalid")
        result[kind] = item
    if set(result) != {"wheel", "sdist", "skill_zip", "skill_zip_repeat"}:
        raise ReleasePromotionError("distribution artifact roles are incomplete")
    return result


def _validate_distribution(
    distribution_root: Path,
    packet: Mapping[str, Any],
    *,
    revision: str,
    release_version: str,
) -> dict[str, Path]:
    distribution = packet.get("distribution")
    if type(distribution) is not dict or distribution.get("status") != "passed":
        raise ReleasePromotionError("evidence packet distribution did not pass")
    packet_artifacts = _artifact_map(distribution.get("artifacts"))
    root = distribution_root.resolve(strict=True)
    if distribution_root.is_symlink() or not root.is_dir():
        raise ReleasePromotionError("distribution root must be a regular directory")
    paths: dict[str, Path] = {}
    for kind, item in packet_artifacts.items():
        filename = item.get("path")
        if type(filename) is not str or filename != Path(filename).name:
            raise ReleasePromotionError(f"{kind} artifact path is invalid")
        path = _regular_file(root / filename, label=kind)
        if path.parent != root:
            raise ReleasePromotionError(f"{kind} artifact escapes distribution root")
        if item.get("size_bytes") != path.stat().st_size:
            raise ReleasePromotionError(f"{kind} artifact size does not match packet")
        if _require_digest(item.get("sha256"), label=f"{kind} digest") != _sha256_file(path):
            raise ReleasePromotionError(f"{kind} artifact digest does not match packet")
        paths[kind] = path

    evidence_path = _regular_file(
        root / "distribution-evidence.json", label="distribution evidence"
    )
    evidence, evidence_raw = _read_json(evidence_path, label="distribution evidence")
    try:
        rebuilt = build_distribution_evidence(
            root,
            paths["skill_zip"],
            paths["skill_zip_repeat"],
            revision=revision,
            expected_version=release_version,
        )
    except DistributionEvidenceError as exc:
        raise ReleasePromotionError(
            f"distribution archive validation failed: {exc}"
        ) from exc
    if evidence_raw != canonical_json_bytes(evidence) or evidence != rebuilt:
        raise ReleasePromotionError("distribution evidence cannot be reconstructed")
    if distribution.get("evidence_digest") != rebuilt.get("evidence_digest"):
        raise ReleasePromotionError("packet distribution digest does not match")
    if distribution.get("evidence_sha256") != _sha256_bytes(evidence_raw):
        raise ReleasePromotionError("packet distribution evidence hash does not match")
    paths["distribution_evidence"] = evidence_path
    return paths


def _copy_asset(source: Path, destination: Path) -> dict[str, object]:
    shutil.copyfile(source, destination)
    return {
        "name": destination.name,
        "sha256": _sha256_file(destination),
        "size_bytes": destination.stat().st_size,
    }


def prepare_release(
    *,
    repository_root: Path,
    distribution_root: Path,
    packet_path: Path,
    packet_digest_path: Path,
    output_dir: Path,
    revision: str,
    version: str,
    tag: str,
    ci_run_id: int,
    ci_run_attempt: int,
) -> dict[str, object]:
    candidate = _require_revision(revision)
    release_version = _require_version(version)
    if tag != f"v{release_version}":
        raise ReleasePromotionError("release tag must equal v plus the project version")
    versions = _project_versions(repository_root)
    if set(versions.values()) != {release_version}:
        raise ReleasePromotionError(
            "pyproject, package, and uv lock versions must match the release version"
        )
    selected_run_id = _require_positive_int(ci_run_id, label="CI run id")
    selected_run_attempt = _require_positive_int(
        ci_run_attempt, label="CI run attempt"
    )

    packet, _ = _validate_packet(
        packet_path,
        packet_digest_path,
        revision=candidate,
        ci_run_id=selected_run_id,
        ci_run_attempt=selected_run_attempt,
    )
    sources = _validate_distribution(
        distribution_root,
        packet,
        revision=candidate,
        release_version=release_version,
    )
    license_path = _validated_gpl3_license(repository_root / "LICENSE")
    notices_path = _regular_file(
        repository_root / "THIRD_PARTY_NOTICES.md",
        label="third-party notices",
    )

    output_parent = output_dir.resolve(strict=False).parent
    output_parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        raise ReleasePromotionError("release output directory already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_parent))
    try:
        assets = [
            _copy_asset(sources["wheel"], temporary / sources["wheel"].name),
            _copy_asset(sources["sdist"], temporary / sources["sdist"].name),
            _copy_asset(
                sources["skill_zip"],
                temporary / f"wish-builder-skill-{release_version}.zip",
            ),
            _copy_asset(
                sources["distribution_evidence"],
                temporary / "distribution-evidence.json",
            ),
            _copy_asset(packet_path, temporary / "active-m1-evidence-packet.json"),
            _copy_asset(
                packet_digest_path,
                temporary / "active-m1-evidence-packet.sha256",
            ),
            _copy_asset(license_path, temporary / "LICENSE"),
            _copy_asset(
                notices_path,
                temporary / "THIRD_PARTY_NOTICES.md",
            ),
        ]
        manifest: dict[str, object] = {
            "artifact_count": len(assets),
            "artifacts": sorted(assets, key=lambda item: str(item["name"])),
            "candidate_revision": candidate,
            "ci_run_attempt": selected_run_attempt,
            "ci_run_id": selected_run_id,
            "license_expression": LICENSE_EXPRESSION,
            "prerelease": True,
            "schema_version": RELEASE_MANIFEST_SCHEMA_VERSION,
            "status": "passed",
            "tag": tag,
            "version": release_version,
        }
        manifest["manifest_digest"] = _sha256_bytes(canonical_json_bytes(manifest))
        manifest_path = temporary / "release-manifest.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest))

        checksum_paths = sorted(
            [path for path in temporary.iterdir() if path.is_file()],
            key=lambda path: path.name,
        )
        checksums = "".join(
            f"{_sha256_file(path).removeprefix('sha256:')}  {path.name}\n"
            for path in checksum_paths
        )
        (temporary / "SHA256SUMS").write_text(checksums, encoding="ascii", newline="\n")
        os.replace(temporary, output_dir)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--distribution-root", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--packet-digest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--ci-run-id", type=int, required=True)
    parser.add_argument("--ci-run-attempt", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = prepare_release(
            repository_root=arguments.repository_root,
            distribution_root=arguments.distribution_root,
            packet_path=arguments.packet,
            packet_digest_path=arguments.packet_digest,
            output_dir=arguments.output_dir,
            revision=arguments.revision,
            version=arguments.version,
            tag=arguments.tag,
            ci_run_id=arguments.ci_run_id,
            ci_run_attempt=arguments.ci_run_attempt,
        )
    except (OSError, ReleasePromotionError) as exc:
        print(f"release promotion failed: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
