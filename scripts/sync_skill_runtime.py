#!/usr/bin/env python3
"""Synchronize or verify the generated standalone Skill runtime."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

from build_skill_zip import (  # noqa: E402
    REPOSITORY_ROOT,
    RuntimeDriftError,
    assert_runtime_current,
    sync_runtime,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronize package-owned runtime files into wish-builder."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify generated files without modifying them",
    )
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
    try:
        if args.check:
            assert_runtime_current(repository_root)
            print("OK: standalone Skill runtime matches package sources")
        else:
            sync_runtime(repository_root)
            print("OK: synchronized standalone Skill runtime from package sources")
    except (OSError, RuntimeDriftError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
