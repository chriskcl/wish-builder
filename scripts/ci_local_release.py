#!/usr/bin/env python3
"""Promote one revision-bound local M1 evidence set into release assets."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.ci_distribution_evidence import canonical_json_bytes
from scripts.ci_release import (
    ReleasePromotionError,
    _project_versions,
    _read_json,
    _regular_file,
    _require_digest,
    _require_revision,
    _require_version,
    _sha256_bytes,
    _validate_distribution,
    assemble_release_assets,
)
from scripts.local_evidence_packet import (
    SCHEMA_VERSION as LOCAL_EVIDENCE_SCHEMA_VERSION,
    build_local_evidence_manifest,
)


_FORBIDDEN_CI_FIELDS = frozenset(
    {"ci_run_attempt", "ci_run_id", "job_results", "needs", "workflow"}
)


def _require_clean_candidate(repository_root: Path, revision: str) -> None:
    """Require release inputs to come from the exact clean candidate checkout."""
    root = repository_root.resolve(strict=True)
    if repository_root.is_symlink() or not root.is_dir():
        raise ReleasePromotionError("candidate repository must be a regular directory")

    def run_git(*arguments: str) -> bytes:
        try:
            completed = subprocess.run(
                ("git", "-C", str(root), *arguments),
                check=False,
                capture_output=True,
                shell=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleasePromotionError(
                "candidate repository cannot be inspected"
            ) from exc
        if completed.returncode != 0:
            raise ReleasePromotionError("candidate repository cannot be inspected")
        return completed.stdout

    try:
        head = run_git("rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ReleasePromotionError("candidate repository HEAD is invalid") from exc
    if head != revision:
        raise ReleasePromotionError("candidate revision is not the checked-out HEAD")
    if run_git(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ):
        raise ReleasePromotionError("candidate repository is not clean")


def _validate_local_manifest(
    manifest_path: Path,
    digest_path: Path,
    *,
    revision: str,
) -> dict[str, Any]:
    manifest, raw = _read_json(manifest_path, label="local M1 evidence manifest")
    if raw != canonical_json_bytes(manifest):
        raise ReleasePromotionError("local M1 evidence manifest is not canonical JSON")
    if _FORBIDDEN_CI_FIELDS.intersection(manifest):
        raise ReleasePromotionError("local M1 evidence manifest contains CI provenance")
    if (
        manifest.get("schema_version") != LOCAL_EVIDENCE_SCHEMA_VERSION
        or manifest.get("provenance_kind") != "local"
        or manifest.get("status") != "passed"
        or manifest.get("candidate_revision") != revision
    ):
        raise ReleasePromotionError("local M1 evidence did not pass for revision")
    declared = _require_digest(
        manifest.get("evidence_digest"), label="local evidence digest"
    )
    digest_input = dict(manifest)
    digest_input.pop("evidence_digest")
    if declared != _sha256_bytes(canonical_json_bytes(digest_input)):
        raise ReleasePromotionError("local M1 evidence digest is inconsistent")

    digest_file = _regular_file(digest_path, label="local M1 raw evidence digest")
    try:
        raw_digest = digest_file.read_text(encoding="ascii").strip()
    except UnicodeDecodeError as exc:
        raise ReleasePromotionError("local M1 raw evidence digest is not ASCII") from exc
    if raw_digest != _sha256_bytes(raw):
        raise ReleasePromotionError("local M1 raw evidence digest does not match")
    return manifest


def prepare_local_release(
    *,
    repository_root: Path,
    evidence_root: Path,
    safety_base_ref: str,
    distribution_root: Path,
    manifest_path: Path,
    manifest_digest_path: Path,
    output_dir: Path,
    revision: str,
    version: str,
    tag: str,
) -> dict[str, object]:
    candidate = _require_revision(revision)
    release_version = _require_version(version)
    if tag != f"v{release_version}":
        raise ReleasePromotionError("release tag must equal v plus the project version")
    _require_clean_candidate(repository_root, candidate)
    versions = _project_versions(repository_root)
    if set(versions.values()) != {release_version}:
        raise ReleasePromotionError(
            "pyproject, package, and uv lock versions must match the release version"
        )

    manifest = _validate_local_manifest(
        manifest_path,
        manifest_digest_path,
        revision=candidate,
    )
    rebuilt_manifest = build_local_evidence_manifest(
        evidence_root,
        candidate_revision=candidate,
        safety_base_ref=safety_base_ref,
    )
    if manifest != rebuilt_manifest:
        raise ReleasePromotionError(
            "local M1 evidence manifest cannot be reconstructed from raw evidence"
        )
    sources = _validate_distribution(
        distribution_root,
        manifest,
        revision=candidate,
        release_version=release_version,
    )
    return assemble_release_assets(
        repository_root=repository_root,
        sources=sources,
        evidence_path=manifest_path,
        evidence_digest_path=manifest_digest_path,
        evidence_asset_name="local-m1-evidence-manifest.json",
        evidence_digest_asset_name="local-m1-evidence-manifest.sha256",
        output_dir=output_dir,
        candidate_revision=candidate,
        release_version=release_version,
        tag=tag,
        provenance_fields={
            "local_evidence_digest": manifest["evidence_digest"],
            "provenance_kind": "local",
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--safety-base-ref", required=True)
    parser.add_argument("--distribution-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-digest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = prepare_local_release(
            repository_root=arguments.repository_root,
            evidence_root=arguments.evidence_root,
            safety_base_ref=arguments.safety_base_ref,
            distribution_root=arguments.distribution_root,
            manifest_path=arguments.manifest,
            manifest_digest_path=arguments.manifest_digest,
            output_dir=arguments.output_dir,
            revision=arguments.revision,
            version=arguments.version,
            tag=arguments.tag,
        )
    except (OSError, ValueError) as exc:
        print(f"local release promotion failed: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_json_bytes(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
