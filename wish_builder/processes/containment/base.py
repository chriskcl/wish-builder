"""Typed process-tree containment contracts.

Containment is a safety boundary, not a best-effort cleanup helper.  A backend
may report a tree as gone only when its operating-system primitive can prove
that fact.  Callers must block on ``UNSUPPORTED`` or ``UNKNOWN``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ContainmentStatus(StrEnum):
    PROVEN = "proven"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class TreeState(StrEnum):
    EMPTY = "empty"
    ACTIVE = "active"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ContainmentCapability:
    status: ContainmentStatus
    backend: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not ContainmentStatus:
            raise TypeError("status must be a ContainmentStatus")
        if type(self.backend) is not str or not self.backend:
            raise ValueError("backend must be a non-empty string")
        if self.detail is not None and type(self.detail) is not str:
            raise TypeError("detail must be a string or null")


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    process_start_id: str
    containment_id: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise ValueError("pid must be positive")
        if type(self.process_start_id) is not str or not self.process_start_id:
            raise ValueError("process_start_id must be a non-empty string")
        if type(self.containment_id) is not str or not self.containment_id:
            raise ValueError("containment_id must be a non-empty string")


@dataclass(frozen=True, slots=True)
class AttachResult:
    status: ContainmentStatus
    identity: ProcessIdentity | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not ContainmentStatus:
            raise TypeError("status must be a ContainmentStatus")
        if self.status is ContainmentStatus.PROVEN and self.identity is None:
            raise ValueError("proven attachment requires process identity")
        if self.status is not ContainmentStatus.PROVEN and self.identity is not None:
            raise ValueError("unproven attachment cannot expose process identity")


@dataclass(frozen=True, slots=True)
class KillResult:
    state: TreeState
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not TreeState:
            raise TypeError("state must be a TreeState")


class ContainmentSession(Protocol):
    """One containment primitive owned by one process launch."""

    @property
    def capability(self) -> ContainmentCapability: ...

    @property
    def creation_flags(self) -> int: ...

    @property
    def start_new_session(self) -> bool: ...

    def attach(self, process: subprocess.Popen[bytes]) -> AttachResult: ...

    def tree_state(self) -> TreeState: ...

    def kill_tree(self) -> KillResult: ...

    def abort_start(self, process: subprocess.Popen[bytes]) -> None: ...

    def close(self) -> None: ...


class UnavailableContainment:
    """A non-launching session for unsupported or failed capability setup."""

    def __init__(
        self,
        status: ContainmentStatus,
        backend: str,
        detail: str,
    ) -> None:
        if status is ContainmentStatus.PROVEN:
            raise ValueError("unavailable containment cannot be proven")
        self._capability = ContainmentCapability(status, backend, detail)

    @property
    def capability(self) -> ContainmentCapability:
        return self._capability

    @property
    def creation_flags(self) -> int:
        return 0

    @property
    def start_new_session(self) -> bool:
        return False

    def attach(self, process: subprocess.Popen[bytes]) -> AttachResult:
        return AttachResult(self._capability.status, detail=self._capability.detail)

    def tree_state(self) -> TreeState:
        return TreeState.UNKNOWN

    def kill_tree(self) -> KillResult:
        return KillResult(TreeState.UNKNOWN, self._capability.detail)

    def abort_start(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.kill()
        except OSError:
            pass

    def close(self) -> None:
        return None


__all__ = [
    "AttachResult",
    "ContainmentCapability",
    "ContainmentSession",
    "ContainmentStatus",
    "KillResult",
    "ProcessIdentity",
    "TreeState",
    "UnavailableContainment",
]
