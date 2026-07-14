"""Portable access to process primitives that only exist on Windows."""

from __future__ import annotations

import ctypes
import signal
import subprocess
from typing import Any


def windows_creation_flags(*, detached: bool = False) -> int:
    """Return Windows process-group flags without portable attribute access."""

    group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    if not detached:
        return group
    return group | int(getattr(subprocess, "DETACHED_PROCESS", 0))


def windows_ctrl_break_event() -> int:
    """Return the Windows console break signal, or a controlled fallback."""

    return int(getattr(signal, "CTRL_BREAK_EVENT", 0))


def windows_process_exists(pid: int) -> bool:
    """Return whether ``pid`` names a live Windows process.

    Windows does not implement the POSIX ``os.kill(pid, 0)`` existence probe;
    some Python runtimes raise ``WinError 87`` for it. Querying a process handle
    also lets us distinguish a terminated process whose PID has not yet been
    reaped from a genuinely live service.
    """

    if pid <= 0:
        return False
    win_dll: Any = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return False
    try:
        kernel32: Any = win_dll("kernel32", use_last_error=True)
    except OSError:
        return False
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied still proves that the process exists.
        get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
        return int(get_last_error()) == 5
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def windows_total_physical_memory() -> int:
    """Query physical memory through a lazily loaded Windows kernel adapter."""

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    win_dll: Any = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        return 0
    try:
        kernel32: Any = win_dll("kernel32", use_last_error=True)
    except OSError:
        return 0
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    return int(status.total_physical) if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else 0
