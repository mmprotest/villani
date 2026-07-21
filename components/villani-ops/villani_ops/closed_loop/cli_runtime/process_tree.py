"""Cross-platform process-group and Windows Job Object lifecycle management."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProcessTreeCleanup:
    status: str
    graceful_requested: bool
    graceful_succeeded: bool
    forced: bool
    error: str | None = None


def subprocess_group_options() -> dict[str, Any]:
    """Options for an invocation-owned process group without a command shell."""

    if os.name == "posix":
        return {"start_new_session": True}
    # CREATE_SUSPENDED (0x4) closes the otherwise unavoidable race between
    # CreateProcess returning and Job Object assignment. The initial thread is
    # resumed only after the process is inside its invocation-owned job.
    return {
        "creationflags": int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        | 0x00000004
    }


def _send_posix_group_signal(process_group: int, process_signal: int) -> None:
    kill_group = getattr(os, "killpg", None)
    if not callable(kill_group):
        raise OSError("POSIX process-group signals are unavailable")
    kill_group(process_group, process_signal)


def _resume_windows_process(process_id: int) -> None:
    """Resume every initially suspended thread owned by ``process_id``."""

    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise OSError("Windows thread APIs are unavailable")
    kernel: Any = win_dll("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry32)]
    kernel.Thread32First.restype = wintypes.BOOL
    kernel.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(ThreadEntry32)]
    kernel.Thread32Next.restype = wintypes.BOOL
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = ctypes.c_void_p
    kernel.ResumeThread.argtypes = [ctypes.c_void_p]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    resumed = 0
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel.Thread32First(snapshot, ctypes.byref(entry)))
        while has_entry:
            if int(entry.th32OwnerProcessID) == process_id:
                thread = kernel.OpenThread(0x0002, False, entry.th32ThreadID)
                if thread:
                    try:
                        if kernel.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed += 1
                    finally:
                        kernel.CloseHandle(thread)
            has_entry = bool(kernel.Thread32Next(snapshot, ctypes.byref(entry)))
    finally:
        kernel.CloseHandle(snapshot)
    if resumed < 1:
        raise OSError("the suspended invocation thread could not be resumed")


class _WindowsJob:
    """Kill-on-close Job Object adapted from the existing Villani runner helper."""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9
    _BASIC_ACCOUNTING_INFORMATION = 1

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are available only on Windows")
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("Windows kernel APIs are unavailable")
        self._kernel: Any = win_dll("kernel32", use_last_error=True)
        self._configure_signatures()
        self._handle = self._kernel.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = self._extended_limit_type()()
            limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
            if not self._kernel.SetInformationJobObject(
                self._handle,
                self._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = self._process_handle(process)
            if not self._kernel.AssignProcessToJobObject(
                self._handle, ctypes.c_void_p(process_handle)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _basic_limit_type() -> type[ctypes.Structure]:
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
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

        return BasicLimitInformation

    @classmethod
    def _extended_limit_type(cls) -> type[ctypes.Structure]:
        from ctypes import wintypes

        basic_type = cls._basic_limit_type()

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", basic_type),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _ = wintypes
        return ExtendedLimitInformation

    @staticmethod
    def _accounting_type() -> type[ctypes.Structure]:
        from ctypes import wintypes

        class BasicAccountingInformation(ctypes.Structure):
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

        return BasicAccountingInformation

    def _configure_signatures(self) -> None:
        self._kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._kernel.SetInformationJobObject.restype = ctypes.c_int
        self._kernel.AssignProcessToJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._kernel.AssignProcessToJobObject.restype = ctypes.c_int
        self._kernel.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._kernel.QueryInformationJobObject.restype = ctypes.c_int
        self._kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel.TerminateJobObject.restype = ctypes.c_int
        self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel.CloseHandle.restype = ctypes.c_int

    @staticmethod
    def _process_handle(process: asyncio.subprocess.Process) -> int:
        transport = getattr(process, "_transport", None)
        popen = None
        if transport is not None:
            get_extra_info = getattr(transport, "get_extra_info", None)
            if callable(get_extra_info):
                popen = get_extra_info("subprocess")
            popen = popen or getattr(transport, "_proc", None)
        handle = getattr(popen, "_handle", None)
        if handle is None:
            raise OSError("asyncio did not expose the child process handle")
        return int(handle)

    def active_processes(self) -> int:
        if not self._handle:
            return 0
        accounting = self._accounting_type()()
        if not self._kernel.QueryInformationJobObject(
            self._handle,
            self._BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.ActiveProcesses)

    def terminate(self) -> None:
        if self._handle and not self._kernel.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._kernel.CloseHandle(handle)


class ProcessTreeController:
    """Own exactly one group/job and never targets an unrelated process."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.process_group = process.pid if os.name == "posix" else None
        self.windows_job: _WindowsJob | None = None
        self.windows_job_error: str | None = None
        self._closed = False
        if os.name == "nt":
            errors: list[str] = []
            try:
                self.windows_job = _WindowsJob(process)
            except OSError as error:
                errors.append(
                    f"Job Object assignment failed: {type(error).__name__}: {error}"
                )
            try:
                _resume_windows_process(process.pid)
            except OSError as error:
                errors.append(f"process resume failed: {type(error).__name__}: {error}")
            if errors:
                self.windows_job_error = "; ".join(errors)

    def _posix_group_alive(self) -> bool:
        if os.name != "posix" or self.process_group is None:
            return False
        try:
            _send_posix_group_signal(self.process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _tree_alive(self) -> bool:
        if os.name == "posix":
            return self._posix_group_alive()
        if self.windows_job is not None:
            return self.windows_job.active_processes() > 0
        return self.process.returncode is None

    async def _wait_until_gone(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(seconds, 0)
        while True:
            try:
                alive = self._tree_alive()
            except OSError:
                return False
            if not alive:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(0.02, max(deadline - time.monotonic(), 0)))

    async def _reap_parent(self, seconds: float) -> bool:
        if self.process.returncode is not None:
            await self.process.wait()
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(self.process.wait()), timeout=max(seconds, 0.05)
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def _windows_taskkill(self) -> None:
        helper = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(self.process.pid),
            "/T",
            "/F",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(helper.wait(), timeout=5)

    def _request_windows_graceful_shutdown(self) -> None:
        """Send CTRL_BREAK to only the invocation's new process group."""

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError("Windows console control APIs are unavailable")
        kernel: Any = win_dll("kernel32", use_last_error=True)
        kernel.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        kernel.GenerateConsoleCtrlEvent.restype = ctypes.c_int
        ctrl_break = int(getattr(signal, "CTRL_BREAK_EVENT", 1))
        if not kernel.GenerateConsoleCtrlEvent(ctrl_break, self.process.pid):
            raise ctypes.WinError(ctypes.get_last_error())

    async def terminate(self, grace_seconds: float) -> ProcessTreeCleanup:
        graceful_succeeded = False
        forced = False
        errors: list[str] = []
        try:
            if os.name == "posix":
                if self._posix_group_alive():
                    try:
                        _send_posix_group_signal(
                            self.process_group or self.process.pid, signal.SIGTERM
                        )
                    except ProcessLookupError:
                        pass
                    graceful_succeeded = await self._wait_until_gone(grace_seconds)
                else:
                    graceful_succeeded = True
                if not graceful_succeeded and self._posix_group_alive():
                    forced = True
                    try:
                        _send_posix_group_signal(
                            self.process_group or self.process.pid,
                            getattr(signal, "SIGKILL", signal.SIGTERM),
                        )
                    except ProcessLookupError:
                        pass
            else:
                if self.process.returncode is None:
                    try:
                        self._request_windows_graceful_shutdown()
                    except (OSError, ProcessLookupError) as error:
                        errors.append(f"graceful signal failed: {type(error).__name__}")
                graceful_succeeded = await self._wait_until_gone(grace_seconds)
                if not graceful_succeeded:
                    forced = True
                    if self.windows_job is not None:
                        self.windows_job.terminate()
                    elif self.process.returncode is None:
                        await self._windows_taskkill()
                    else:
                        errors.append(
                            "process exited before descendants could be identified without a Job Object"
                        )
            await self._reap_parent(max(grace_seconds, 1.0))
            if not await self._wait_until_gone(max(grace_seconds, 0.5)):
                errors.append("one or more invocation-owned processes remained alive")
        except (OSError, asyncio.TimeoutError) as error:
            errors.append(f"{type(error).__name__}: {error}")
            try:
                self.process.kill()
                await self._reap_parent(1.0)
            except (OSError, ProcessLookupError, asyncio.TimeoutError) as fallback:
                errors.append(
                    f"fallback kill failed: {type(fallback).__name__}: {fallback}"
                )
        finally:
            self.close()
        return ProcessTreeCleanup(
            status="failed" if errors else "succeeded",
            graceful_requested=True,
            graceful_succeeded=graceful_succeeded,
            forced=forced,
            error="; ".join(errors) if errors else None,
        )

    async def cleanup_after_exit(self, grace_seconds: float) -> ProcessTreeCleanup:
        """Close descendant handles after a normal parent exit."""

        await self._reap_parent(max(grace_seconds, 0.05))
        try:
            alive = self._tree_alive()
        except OSError as error:
            self.close()
            return ProcessTreeCleanup(
                status="failed",
                graceful_requested=False,
                graceful_succeeded=False,
                forced=False,
                error=f"{type(error).__name__}: {error}",
            )
        if alive:
            return await self.terminate(grace_seconds)
        self.close()
        return ProcessTreeCleanup(
            status="succeeded",
            graceful_requested=False,
            graceful_succeeded=False,
            forced=False,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.windows_job is not None:
            self.windows_job.close()


__all__ = [
    "ProcessTreeCleanup",
    "ProcessTreeController",
    "subprocess_group_options",
]
