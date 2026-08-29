#!/usr/bin/env python3
"""Independent active-M1 safety-path governance for CI gates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


def _repository_python_path(value: object) -> str | None:
    if type(value) is not str or not value or "\x00" in value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
        return None
    normalized = path.as_posix()
    if normalized != value or not normalized.startswith("wish_builder/"):
        return None
    return normalized


def _repository_root(value: object) -> str | None:
    if type(value) is not str or not value or "\x00" in value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix:
        return None
    normalized = path.as_posix().rstrip("/")
    if normalized != value or not normalized.startswith("wish_builder/"):
        return None
    return normalized


@dataclass(frozen=True, slots=True)
class SafetyPathRegistry:
    """Classify safety sources without depending on mutation registrations."""

    exact_paths: tuple[str, ...]
    recursive_python_roots: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    excluded_recursive_python_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.exact_paths) is not tuple
            or any(_repository_python_path(path) is None for path in self.exact_paths)
            or len(set(self.exact_paths)) != len(self.exact_paths)
        ):
            raise ValueError("exact_paths must be unique canonical Python paths")
        if (
            type(self.recursive_python_roots) is not tuple
            or any(
                _repository_root(root) is None for root in self.recursive_python_roots
            )
            or len(set(self.recursive_python_roots))
            != len(self.recursive_python_roots)
        ):
            raise ValueError(
                "recursive_python_roots must be unique canonical package roots"
            )
        if (
            type(self.excluded_paths) is not tuple
            or any(_repository_python_path(path) is None for path in self.excluded_paths)
            or len(set(self.excluded_paths)) != len(self.excluded_paths)
        ):
            raise ValueError("excluded_paths must be unique canonical Python paths")
        if (
            type(self.excluded_recursive_python_roots) is not tuple
            or any(
                _repository_root(root) is None
                for root in self.excluded_recursive_python_roots
            )
            or len(set(self.excluded_recursive_python_roots))
            != len(self.excluded_recursive_python_roots)
        ):
            raise ValueError(
                "excluded_recursive_python_roots must be unique canonical package roots"
            )

    def governs(self, value: object) -> bool:
        path = _repository_python_path(value)
        if path is None:
            return False
        if path in self.excluded_paths or any(
            path.startswith(root + "/")
            for root in self.excluded_recursive_python_roots
        ):
            return False
        if path in self.exact_paths:
            return True
        return any(path.startswith(root + "/") for root in self.recursive_python_roots)

    @property
    def git_pathspecs(self) -> tuple[str, ...]:
        """Return pathspecs that include additions, modifications, and deletions."""
        recursive = tuple(
            f":(glob){root}/**/*.py" for root in self.recursive_python_roots
        )
        excluded = tuple(f":(exclude){path}" for path in self.excluded_paths)
        excluded_recursive = tuple(
            f":(exclude,glob){root}/**/*.py"
            for root in self.excluded_recursive_python_roots
        )
        return tuple(
            sorted(
                (
                    *self.exact_paths,
                    *recursive,
                    *excluded,
                    *excluded_recursive,
                )
            )
        )


ACTIVE_M1_SAFETY_PATHS = SafetyPathRegistry(
    exact_paths=(
        "wish_builder/contracts/serialization.py",
        "wish_builder/kernel/gates.py",
        "wish_builder/kernel/state.py",
        "wish_builder/services/backend_admission.py",
        "wish_builder/services/recovery.py",
        "wish_builder/services/replay.py",
    ),
    recursive_python_roots=("wish_builder/adapters",),
    excluded_paths=("wish_builder/adapters/fakes.py",),
    excluded_recursive_python_roots=("wish_builder/adapters/fake",),
)
