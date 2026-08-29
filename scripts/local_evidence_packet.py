#!/usr/bin/env python3
"""Build a canonical M1 evidence manifest from replayable local test artifacts."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.ci_evidence_packet import (
    EvidencePacketError,
    canonical_json_bytes,
    validate_evidence_artifacts,
)


SCHEMA_VERSION = 1


def build_local_evidence_manifest(
    evidence_root: Path,
    *,
    candidate_revision: str,
    safety_base_ref: str,
) -> dict[str, object]:
    """Validate all M1 gates and identify the result as local provenance."""
    manifest = validate_evidence_artifacts(
        evidence_root,
        candidate_revision=candidate_revision,
        safety_base_ref=safety_base_ref,
    )
    manifest.update(
        {
            "provenance_kind": "local",
            "schema_version": SCHEMA_VERSION,
            "status": "passed",
        }
    )
    manifest["evidence_digest"] = "sha256:" + hashlib.sha256(
        canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest


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


def write_manifest_and_digest(
    output: Path,
    digest_output: Path,
    manifest: Mapping[str, object],
) -> None:
    encoded = canonical_json_bytes(manifest)
    _write_atomic(output, encoded)
    raw_digest = "sha256:" + hashlib.sha256(encoded).hexdigest() + "\n"
    _write_atomic(digest_output, raw_digest.encode("ascii"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--candidate-revision", required=True)
    parser.add_argument("--safety-base-ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--digest-output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        manifest = build_local_evidence_manifest(
            arguments.evidence_root,
            candidate_revision=arguments.candidate_revision,
            safety_base_ref=arguments.safety_base_ref,
        )
        write_manifest_and_digest(arguments.output, arguments.digest_output, manifest)
    except (EvidencePacketError, OSError, ValueError) as exc:
        print(f"local evidence validation failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(canonical_json_bytes(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
