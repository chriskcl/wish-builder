"""Local process identity probes for coordinator lease takeover."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from wish_builder.contracts import LeaseOwner


class LeaseOwnerProcessState(StrEnum):
    EXACT_ALIVE = "exact_alive"
    PID_REUSED = "pid_reused"
    DEAD = "dead"
    DIFFERENT_HOST = "different_host"
    UNKNOWN = "unknown"
    PROBE_ERROR = "probe_error"


@dataclass(frozen=True, slots=True)
class LeaseOwnerProcessProbeResult:
    state: LeaseOwnerProcessState
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not LeaseOwnerProcessState:
            raise TypeError("state must be a LeaseOwnerProcessState")
        if self.detail is not None and (
            type(self.detail) is not str or not self.detail
        ):
            raise ValueError("detail must be a non-empty string or null")


def capture_process_start_id(process_id: int | None = None) -> str:
    """Capture the stable start identity used to bind a coordinator lease."""

    selected = os.getpid() if process_id is None else process_id
    if type(selected) is not int or selected <= 0:
        raise ValueError("process_id must be a positive integer or null")
    if os.name == "posix":
        if not Path("/proc").is_dir():
            raise OSError("stable process-start identity is unavailable")
        return _linux_process_start_id(selected)
    if os.name == "nt":
        return _windows_process_start_id(selected)
    raise OSError(f"process identity capture is unsupported on {os.name}")


def probe_lease_owner_process(
    owner: LeaseOwner,
    *,
    local_host_id: str,
) -> LeaseOwnerProcessProbeResult:
    """Return exact liveness for a lease owner on this host, or fail closed."""

    if type(owner) is not LeaseOwner:
        raise TypeError("owner must be a LeaseOwner")
    if type(local_host_id) is not str or not local_host_id:
        raise ValueError("local_host_id must be a non-empty string")
    actor = owner.actor
    if actor.host_id != local_host_id:
        return LeaseOwnerProcessProbeResult(
            LeaseOwnerProcessState.DIFFERENT_HOST,
            "lease owner is on a different host",
        )
    if os.name == "posix":
        return _probe_posix_process(actor.process_id, actor.process_start_id)
    if os.name == "nt":
        return _probe_windows_process(actor.process_id, actor.process_start_id)
    return LeaseOwnerProcessProbeResult(
        LeaseOwnerProcessState.UNKNOWN,
        f"process identity probing is unsupported on {os.name}",
    )


def _probe_posix_process(
    process_id: int,
    expected_start_id: str,
) -> LeaseOwnerProcessProbeResult:
    if not Path("/proc").is_dir():
        return LeaseOwnerProcessProbeResult(
            LeaseOwnerProcessState.UNKNOWN,
            "stable process-start identity is unavailable",
        )
    try:
        start_id = _linux_process_start_id(process_id)
    except FileNotFoundError:
        return LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
    except (OSError, UnicodeError) as exc:
        return LeaseOwnerProcessProbeResult(
            LeaseOwnerProcessState.UNKNOWN,
            f"process identity probe failed: {exc}",
        )
    if start_id == expected_start_id:
        return LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.EXACT_ALIVE)
    return LeaseOwnerProcessProbeResult(
        LeaseOwnerProcessState.PID_REUSED,
        "pid belongs to a different process-start identity",
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


def _probe_windows_process(
    process_id: int,
    expected_start_id: str,
) -> LeaseOwnerProcessProbeResult:
    try:
        start_id = _windows_process_start_id(process_id)
    except ProcessLookupError:
        return LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.DEAD)
    except (OSError, TypeError, ValueError) as exc:
        return LeaseOwnerProcessProbeResult(
            LeaseOwnerProcessState.UNKNOWN,
            f"process identity probe failed: {exc}",
        )
    if start_id == expected_start_id:
        return LeaseOwnerProcessProbeResult(LeaseOwnerProcessState.EXACT_ALIVE)
    return LeaseOwnerProcessProbeResult(
        LeaseOwnerProcessState.PID_REUSED,
        "pid belongs to a different process-start identity",
    )


def _windows_process_start_id(pid: int) -> str:
    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            raise ProcessLookupError(pid)
        raise OSError(f"OpenProcess failed: winerror={error}")
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise OSError(
                f"GetProcessTimes failed: winerror={ctypes.get_last_error()}"
            )
        exit_value = (
            int(exit_time.dwHighDateTime) << 32
        ) | int(exit_time.dwLowDateTime)
        if exit_value != 0:
            raise ProcessLookupError(pid)
        value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return f"windows-filetime:{value}"
    finally:
        kernel32.CloseHandle(handle)


__all__ = [
    "capture_process_start_id",
    "LeaseOwnerProcessProbeResult",
    "LeaseOwnerProcessState",
    "probe_lease_owner_process",
]
