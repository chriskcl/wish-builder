"""Platform process-tree containment selection."""

from __future__ import annotations

import os

from .base import (
    AttachResult,
    ContainmentCapability,
    ContainmentSession,
    ContainmentStatus,
    KillResult,
    ProcessIdentity,
    TreeState,
    UnavailableContainment,
)


def create_containment_session() -> ContainmentSession:
    if os.name == "nt":
        from .windows import WindowsJobSession

        return WindowsJobSession.create()
    if os.name == "posix":
        from .posix import PosixProcessGroupSession

        return PosixProcessGroupSession.create()
    return UnavailableContainment(
        ContainmentStatus.UNSUPPORTED,
        "none",
        f"unsupported operating-system family: {os.name}",
    )


__all__ = [
    "AttachResult",
    "ContainmentCapability",
    "ContainmentSession",
    "ContainmentStatus",
    "KillResult",
    "ProcessIdentity",
    "TreeState",
    "UnavailableContainment",
    "create_containment_session",
]
