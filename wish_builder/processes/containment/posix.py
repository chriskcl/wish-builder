"""POSIX process-group containment."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
from pathlib import Path

from .base import (
    AttachResult,
    ContainmentCapability,
    ContainmentStatus,
    KillResult,
    ProcessIdentity,
    TreeState,
    UnavailableContainment,
)


def _linux_process_start_id(pid: int) -> str:
    raw = Path(f"/proc/{pid}/stat").read_bytes()
    closing_parenthesis = raw.rfind(b")")
    if closing_parenthesis < 0:
        raise OSError("malformed /proc process identity")
    fields_after_name = raw[closing_parenthesis + 1 :].split()
    # Field 22 is starttime; the slice begins at field 3.
    if len(fields_after_name) <= 19:
        raise OSError("incomplete /proc process identity")
    start_ticks = fields_after_name[19].decode("ascii", errors="strict")
    if not start_ticks.isdecimal():
        raise OSError("invalid /proc process start time")
    return f"linux-proc-start:{start_ticks}"


class PosixProcessGroupSession:
    """A fresh POSIX session whose process group id is the root pid."""

    def __init__(self) -> None:
        self._pid: int | None = None
        self._capability = ContainmentCapability(
            ContainmentStatus.PROVEN,
            "posix_process_group",
        )

    @classmethod
    def create(cls) -> PosixProcessGroupSession | UnavailableContainment:
        if os.name != "posix" or not hasattr(os, "killpg"):
            return UnavailableContainment(
                ContainmentStatus.UNSUPPORTED,
                "posix_process_group",
                "POSIX process groups are unavailable",
            )
        if not Path("/proc/self/stat").is_file():
            return UnavailableContainment(
                ContainmentStatus.UNSUPPORTED,
                "posix_process_group",
                "stable process-start identity is unavailable",
            )
        return cls()

    @property
    def capability(self) -> ContainmentCapability:
        return self._capability

    @property
    def creation_flags(self) -> int:
        return 0

    @property
    def start_new_session(self) -> bool:
        return True

    def attach(self, process: subprocess.Popen[bytes]) -> AttachResult:
        self._pid = process.pid
        try:
            process_start_id = _linux_process_start_id(process.pid)
            process_group = os.getpgid(process.pid)
        except ProcessLookupError:
            # A process remains visible in /proc until its Popen handle reaps it;
            # reaching this branch means its identity could not be proven.
            return AttachResult(
                ContainmentStatus.UNKNOWN,
                detail="process disappeared before containment verification",
            )
        except (OSError, UnicodeError) as exc:
            return AttachResult(
                ContainmentStatus.UNKNOWN,
                detail=f"process identity verification failed: {exc}",
            )
        if process_group != process.pid:
            return AttachResult(
                ContainmentStatus.UNKNOWN,
                detail="root process is not its process-group leader",
            )
        return AttachResult(
            ContainmentStatus.PROVEN,
            ProcessIdentity(
                pid=process.pid,
                process_start_id=process_start_id,
                containment_id=f"posix-pgid:{process_group}",
            ),
        )

    def tree_state(self) -> TreeState:
        if self._pid is None:
            return TreeState.UNKNOWN
        try:
            os.killpg(self._pid, 0)
        except ProcessLookupError:
            return TreeState.EMPTY
        except PermissionError:
            return TreeState.UNKNOWN
        except OSError as exc:
            return TreeState.EMPTY if exc.errno == errno.ESRCH else TreeState.UNKNOWN
        return TreeState.ACTIVE

    def kill_tree(self) -> KillResult:
        if self._pid is None:
            return KillResult(TreeState.UNKNOWN, "process group was never attached")
        try:
            os.killpg(self._pid, signal.SIGKILL)
        except ProcessLookupError:
            return KillResult(TreeState.EMPTY)
        except PermissionError as exc:
            return KillResult(TreeState.UNKNOWN, f"process-group kill denied: {exc}")
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return KillResult(TreeState.EMPTY)
            return KillResult(TreeState.UNKNOWN, f"process-group kill failed: {exc}")
        return KillResult(TreeState.ACTIVE)

    def abort_start(self, process: subprocess.Popen[bytes]) -> None:
        result = self.kill_tree()
        if result.state is TreeState.UNKNOWN:
            try:
                process.kill()
            except OSError:
                pass

    def close(self) -> None:
        return None


__all__ = ["PosixProcessGroupSession"]
