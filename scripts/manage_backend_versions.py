#!/usr/bin/env python3
"""Publish candidate, qualified, or quarantined backend version records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from wish_builder.compatibility import load_bundled_backend_version_registry  # noqa: E402
from wish_builder.contracts.compatibility import Platform, Provider  # noqa: E402
from wish_builder.services.backend_version_registry import (  # noqa: E402
    BackendVersionRegistryUpdateError,
    prepare_backend_version_candidate,
    prepare_backend_version_qualification,
    prepare_backend_version_quarantine,
    publish_backend_version_registry,
)


_PROVIDERS = {
    "codex": Provider.CODEX,
    "oh_my_pi": Provider.OMP,
    "pi": Provider.PI,
}


def _identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", required=True, choices=tuple(_PROVIDERS))
    parser.add_argument(
        "--platform",
        required=True,
        choices=tuple(item.value for item in Platform),
    )
    parser.add_argument("--backend-version", required=True)
    parser.add_argument("--expected-registry-digest", required=True)
    parser.add_argument("--note", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Maintain exact backend/OS/version qualification records without "
            "changing the Wish Builder execution kernel."
        )
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    candidate = subparsers.add_parser(
        "candidate", help="record a detected version without enabling dispatch"
    )
    _identity_arguments(candidate)
    candidate.add_argument("--protocol-profile", required=True)
    candidate.add_argument("--npm-shasum", required=True)
    candidate.add_argument("--npm-integrity", required=True)

    qualify = subparsers.add_parser(
        "qualify", help="publish independently reviewed local qualification evidence"
    )
    _identity_arguments(qualify)
    qualify.add_argument("--max-concurrency", required=True, type=int)
    qualify.add_argument("--evidence-digest", required=True)
    qualify.add_argument("--publication-receipt-digest", required=True)
    qualify.add_argument("--review-reference", required=True)

    quarantine = subparsers.add_parser(
        "quarantine", help="immediately disable one known backend version"
    )
    _identity_arguments(quarantine)
    quarantine.add_argument("--review-reference", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository_root = args.repository_root.resolve()
    provider = _PROVIDERS[args.provider]
    platform = Platform(args.platform)
    try:
        current = load_bundled_backend_version_registry()
        common = {
            "expected_registry_digest": args.expected_registry_digest,
            "provider": provider,
            "platform": platform,
            "backend_version": args.backend_version,
            "note": args.note,
        }
        if args.command == "candidate":
            updated = prepare_backend_version_candidate(
                current,
                protocol_profile=args.protocol_profile,
                package_shasum=args.npm_shasum,
                package_integrity=args.npm_integrity,
                **common,
            )
        elif args.command == "qualify":
            updated = prepare_backend_version_qualification(
                current,
                max_concurrency=args.max_concurrency,
                evidence_digest=args.evidence_digest,
                publication_receipt_digest=args.publication_receipt_digest,
                review_reference=args.review_reference,
                **common,
            )
        else:
            updated = prepare_backend_version_quarantine(
                current,
                review_reference=args.review_reference,
                **common,
            )

        compatibility_root = repository_root / "wish_builder" / "compatibility"
        changed = publish_backend_version_registry(
            updated,
            record_path=compatibility_root / "backend-version-registry.json",
            pin_path=compatibility_root / "_backend_version_registry_pin.py",
            expected_current_digest=args.expected_registry_digest,
        )
    except (BackendVersionRegistryUpdateError, OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "backendVersion": args.backend_version,
                "platform": platform.value,
                "provider": args.provider,
                "publicationState": "published" if changed else "unchanged",
                "registryDigest": updated.registry_digest,
                "status": updated.record(
                    provider, platform, args.backend_version
                ).status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
