#!/usr/bin/env python3
"""Build the distributable Skill ZIP with deterministic metadata."""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "wish-builder"
OUTPUT = REPOSITORY_ROOT / "wish-builder-skill.zip"
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def distributable_files() -> list[Path]:
    return sorted(
        (
            path
            for path in SKILL_ROOT.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        ),
        key=lambda path: path.as_posix(),
    )


def build(output: Path = OUTPUT) -> str:
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(
        temporary,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in distributable_files():
            relative = path.relative_to(REPOSITORY_ROOT).as_posix()
            info = zipfile.ZipInfo(relative, FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    os.replace(temporary, output)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    digest = build()
    print(f"sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
