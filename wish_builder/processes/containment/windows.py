"""Windows Job Object containment with race-free suspended launch."""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

from .base import (
    AttachResult,
    ContainmentCapability,
    ContainmentStatus,
    KillResult,
    ProcessIdentity,
    TreeState,
    UnavailableContainment,
)

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_ACCESS_DENIED = 5


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _windows_error(prefix: str) -> str:
    return f"{prefix}: winerror={ctypes.get_last_error()}"


class WindowsJobSession:
    """One verified kill-on-close Job Object with breakaway flags absent."""

    def __init__(self, kernel32: ctypes.WinDLL, job_handle: int) -> None:
        self._kernel32 = kernel32
        self._job_handle: int | None = job_handle
        self._process_handle: int | None = None
        self._assigned = False
        self._capability = ContainmentCapability(
            ContainmentStatus.PROVEN,
            "windows_job_object",
        )

    @classmethod
    def create(cls) -> WindowsJobSession | UnavailableContainment:
        if os.name != "nt":
            return UnavailableContainment(
                ContainmentStatus.UNSUPPORTED,
                "windows_job_object",
                "Windows Job Objects are unavailable",
            )
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            cls._configure_signatures(kernel32)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return UnavailableContainment(
                    ContainmentStatus.UNKNOWN,
                    "windows_job_object",
                    _windows_error("CreateJobObjectW failed"),
                )
            job_handle = int(handle)
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job_handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                detail = _windows_error("SetInformationJobObject failed")
                kernel32.CloseHandle(job_handle)
                return UnavailableContainment(
                    ContainmentStatus.UNKNOWN,
                    "windows_job_object",
                    detail,
                )
            observed = _ExtendedLimitInformation()
            if not kernel32.QueryInformationJobObject(
                job_handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(observed),
                ctypes.sizeof(observed),
                None,
            ):
                detail = _windows_error("QueryInformationJobObject failed")
                kernel32.CloseHandle(job_handle)
                return UnavailableContainment(
                    ContainmentStatus.UNKNOWN,
                    "windows_job_object",
                    detail,
                )
            flags = int(observed.BasicLimitInformation.LimitFlags)
            forbidden = (
                JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            )
            if not flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE or flags & forbidden:
                kernel32.CloseHandle(job_handle)
                return UnavailableContainment(
                    ContainmentStatus.UNKNOWN,
                    "windows_job_object",
                    "Job Object limits could not be verified",
                )
            return cls(kernel32, job_handle)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            return UnavailableContainment(
                ContainmentStatus.UNSUPPORTED,
                "windows_job_object",
                f"Job Object API unavailable: {exc}",
            )

    @staticmethod
    def _configure_signatures(kernel32: ctypes.WinDLL) -> None:
        kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
        )
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.IsProcessInJob.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        )
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        )
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        )
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

    @property
    def capability(self) -> ContainmentCapability:
        return self._capability

    @property
    def creation_flags(self) -> int:
        return CREATE_SUSPENDED

    @property
    def start_new_session(self) -> bool:
        return False

    def _creation_time(self, process_handle: int) -> str:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not self._kernel32.GetProcessTimes(
            process_handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise OSError(_windows_error("GetProcessTimes failed"))
        value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
        return f"windows-filetime:{value}"

    def _resume_initial_thread(self, pid: int) -> None:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if not snapshot or int(snapshot) == INVALID_HANDLE_VALUE:
            raise OSError(_windows_error("thread snapshot failed"))
        thread_ids: list[int] = []
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            available = self._kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if int(entry.th32OwnerProcessID) == pid:
                    thread_ids.append(int(entry.th32ThreadID))
                available = self._kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        if len(thread_ids) != 1:
            raise OSError(
                f"expected one suspended initial thread, found {len(thread_ids)}"
            )
        thread = self._kernel32.OpenThread(
            THREAD_SUSPEND_RESUME,
            False,
            thread_ids[0],
        )
        if not thread:
            raise OSError(_windows_error("OpenThread failed"))
        try:
            previous_count = int(self._kernel32.ResumeThread(thread))
        finally:
            self._kernel32.CloseHandle(thread)
        if previous_count != 1:
            raise OSError(f"unexpected initial thread suspend count {previous_count}")

    def attach(self, process: subprocess.Popen[bytes]) -> AttachResult:
        if self._job_handle is None:
            return AttachResult(
                ContainmentStatus.UNKNOWN, detail="Job Object is closed"
            )
        process_handle = int(process._handle)  # type: ignore[attr-defined]
        self._process_handle = process_handle
        if not self._kernel32.AssignProcessToJobObject(
            self._job_handle,
            process_handle,
        ):
            error = ctypes.get_last_error()
            status = (
                ContainmentStatus.UNSUPPORTED
                if error == ERROR_ACCESS_DENIED
                else ContainmentStatus.UNKNOWN
            )
            return AttachResult(
                status,
                detail=f"AssignProcessToJobObject failed: winerror={error}",
            )
        self._assigned = True
        in_job = wintypes.BOOL()
        if (
            not self._kernel32.IsProcessInJob(
                process_handle,
                self._job_handle,
                ctypes.byref(in_job),
            )
            or not in_job.value
        ):
            return AttachResult(
                ContainmentStatus.UNKNOWN,
                detail="Job Object membership could not be verified",
            )
        try:
            process_start_id = self._creation_time(process_handle)
            self._resume_initial_thread(process.pid)
        except OSError as exc:
            return AttachResult(ContainmentStatus.UNKNOWN, detail=str(exc))
        return AttachResult(
            ContainmentStatus.PROVEN,
            ProcessIdentity(
                pid=process.pid,
                process_start_id=process_start_id,
                containment_id=f"windows-job:{self._job_handle:x}",
            ),
        )

    def tree_state(self) -> TreeState:
        if self._job_handle is None or not self._assigned:
            return TreeState.UNKNOWN
        accounting = _BasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            self._job_handle,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            return TreeState.UNKNOWN
        return (
            TreeState.EMPTY
            if int(accounting.ActiveProcesses) == 0
            else TreeState.ACTIVE
        )

    def kill_tree(self) -> KillResult:
        if self._job_handle is None or not self._assigned:
            return KillResult(
                TreeState.UNKNOWN, "process was not assigned to the Job Object"
            )
        if not self._kernel32.TerminateJobObject(self._job_handle, 0xE0000001):
            return KillResult(
                TreeState.UNKNOWN,
                _windows_error("TerminateJobObject failed"),
            )
        return KillResult(TreeState.ACTIVE)

    def abort_start(self, process: subprocess.Popen[bytes]) -> None:
        if self._assigned:
            self.kill_tree()
            return
        try:
            process.kill()
        except OSError:
            pass

    def close(self) -> None:
        if self._job_handle is not None:
            self._kernel32.CloseHandle(self._job_handle)
            self._job_handle = None


__all__ = ["WindowsJobSession"]
