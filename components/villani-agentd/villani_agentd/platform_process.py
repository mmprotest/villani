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
