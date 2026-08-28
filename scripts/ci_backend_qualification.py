#!/usr/bin/env python3
"""Verify or build a backend-qualification candidate without enabling dispatch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from wish_builder.services.backend_qualification_builder import (
    BackendQualificationCandidateError,
    build_backend_qualification_candidate,
    verify_backend_qualification_candidate,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate replayable backend evidence and produce only an untrusted "
            "qualification candidate."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="verify evidence without writing files")
    verify.add_argument("evidence_root", type=Path)
    build = subparsers.add_parser(
        "build", help="atomically build a self-contained candidate directory"
    )
    build.add_argument("evidence_root", type=Path)
    build.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            candidate = verify_backend_qualification_candidate(args.evidence_root)
        else:
            candidate = build_backend_qualification_candidate(
                args.evidence_root,
                args.output,
            )
    except (BackendQualificationCandidateError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(candidate.report_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
