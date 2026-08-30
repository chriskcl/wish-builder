#!/usr/bin/env python3
"""Publish one independently reviewed backend qualification candidate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from wish_builder.compatibility import load_bundled_backend_qualification  # noqa: E402
from wish_builder.services.backend_qualification_publisher import (  # noqa: E402
    BackendQualificationPublicationError,
    prepare_backend_qualification_publication,
    publish_backend_qualification,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reverify, preserve, and locally publish one exact backend/OS "
            "qualification candidate."
        )
    )
    parser.add_argument("candidate_root", type=Path)
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--expected-artifact-digest", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--review-reference", required=True)
    parser.add_argument("--human-approver", required=True)
    parser.add_argument("--human-approval-reference", required=True)
    parser.add_argument("--review-test-count", type=int, required=True)
    parser.add_argument("--review-skip-count", type=int, required=True)
    parser.add_argument(
        "--accept-detached-provider-provenance",
        action="store_true",
        help=(
            "record explicit human acceptance of local detached provider "
            "provenance; this does not claim an official provider attestation"
        ),
    )
    parser.add_argument("--trellis-version", default="0.6.15")
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository_root = args.repository_root.resolve()
    candidate_root = args.candidate_root.resolve()
    try:
        base_bundle = load_bundled_backend_qualification(args.trellis_version)
        publication = prepare_backend_qualification_publication(
            candidate_root,
            expected_source_revision=args.expected_source_revision,
            expected_artifact_digest=args.expected_artifact_digest,
            reviewer=args.reviewer,
            review_reference=args.review_reference,
            human_approver=args.human_approver,
            human_approval_reference=args.human_approval_reference,
            review_test_count=args.review_test_count,
            review_skip_count=args.review_skip_count,
            accept_detached_provider_provenance=(
                args.accept_detached_provider_provenance
            ),
            trellis_version=args.trellis_version,
            base_bundle=base_bundle,
        )
        evidence_root = repository_root / "wish_builder" / publication.evidence_relative
        changed = publish_backend_qualification(
            publication,
            record_path=(
                repository_root
                / "wish_builder"
                / "compatibility"
                / f"backend-qualification-{args.trellis_version}.json"
            ),
            pin_path=(
                repository_root
                / "wish_builder"
                / "compatibility"
                / "_backend_qualification_pin.py"
            ),
            evidence_root=evidence_root,
            base_bundle=base_bundle,
            trellis_version=args.trellis_version,
        )
    except (
        BackendQualificationPublicationError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    report = publication.report_bytes.decode("utf-8").rstrip()
    print(report)
    print("publicationState=" + ("published" if changed else "already_published"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
