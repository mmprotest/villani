from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from pathlib import Path

from villani_agentd.config import AgentdPaths, ServerConfig
from villani_agentd.lifecycle import start_background, stop_background


ROOT = Path(__file__).resolve().parents[3]


def _cpu_seconds(pid: int) -> float:
    if os.name == "nt":
        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

            def seconds(self) -> float:
                return ((self.high << 32) | self.low) / 10_000_000

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            raise OSError(f"cannot inspect idle service process {pid}")
        creation, exit_time, kernel, user = (FileTime() for _ in range(4))
        try:
            if not ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                raise OSError(f"cannot read idle service process times for {pid}")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return kernel.seconds() + user.seconds()
    completed = subprocess.run(
        ["ps", "-o", "time=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    value = completed.stdout.strip()
    day_parts = value.split("-")
    days = int(day_parts[0]) if len(day_parts) == 2 else 0
    fields = day_parts[-1].split(":")
    seconds = float(fields[-1])
    minutes = int(fields[-2]) if len(fields) >= 2 else 0
    hours = int(fields[-3]) if len(fields) >= 3 else 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _children(pid: int) -> list[tuple[int, str]]:
    if os.name == "nt":
        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("size", ctypes.c_uint32),
                ("usage", ctypes.c_uint32),
                ("process_id", ctypes.c_uint32),
                ("default_heap_id", ctypes.c_size_t),
                ("module_id", ctypes.c_uint32),
                ("threads", ctypes.c_uint32),
                ("parent_process_id", ctypes.c_uint32),
                ("base_priority", ctypes.c_long),
                ("flags", ctypes.c_uint32),
                ("exe_file", ctypes.c_wchar * 260),
            ]

        snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot(0x2, 0)
        if snapshot in {0, ctypes.c_void_p(-1).value}:
            raise OSError("cannot enumerate idle service descendants")
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(ProcessEntry)
        children: list[tuple[int, str]] = []
        try:
            available = ctypes.windll.kernel32.Process32FirstW(
                snapshot, ctypes.byref(entry)
            )
            while available:
                if entry.parent_process_id == pid:
                    children.append((int(entry.process_id), str(entry.exe_file)))
                available = ctypes.windll.kernel32.Process32NextW(
                    snapshot, ctypes.byref(entry)
                )
        finally:
            ctypes.windll.kernel32.CloseHandle(snapshot)
        return children
    completed = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    children: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3 and int(fields[1]) == pid:
            children.append((int(fields[0]), fields[2]))
    return children


def test_idle_service_has_low_cpu_and_no_model_or_gpu_processes(tmp_path: Path) -> None:
    targets = {
        item["id"]: item["maximum"]
        for item in json.loads(
            (ROOT / "release" / "performance-targets.json").read_text(encoding="utf-8")
        )["targets"]
    }
    paths = AgentdPaths(tmp_path / "agentd")
    endpoint = start_background(ServerConfig(), paths)
    pid = int(endpoint["pid"])
    try:
        before = _cpu_seconds(pid)
        time.sleep(2)
        after = _cpu_seconds(pid)
        children = _children(pid)
        model_or_gpu_children = [
            item
            for item in children
            if any(
                marker in item[1].casefold()
                for marker in (
                    "ollama",
                    "lm studio",
                    "lmstudio",
                    "llama-server",
                    "vllm",
                    "nvidia-smi",
                    "rocm-smi",
                )
            )
        ]
        assert after - before <= targets["idle_cpu_time"]
        assert len(model_or_gpu_children) <= targets["idle_model_or_gpu_processes"]
    finally:
        stop_background(paths)
