"""Shell-free, bounded, cancellation-aware subprocess execution."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Mapping, Sequence

from .platform_process import windows_creation_flags, windows_ctrl_break_event


@dataclass(frozen=True, slots=True)
class CapturedStream:
    content: str
    captured_bytes: int
    total_bytes: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    pid: int
    duration_ms: int
    stdout: CapturedStream
    stderr: CapturedStream
    cancelled: bool


def is_windows() -> bool:
    return os.name == "nt"


def _capture(
    stream: BinaryIO,
    limit: int,
    result: dict[str, CapturedStream],
    key: str,
    callback: Callable[[bytes], None] | None = None,
) -> None:
    captured = bytearray()
    total = 0
    while True:
        chunk = stream.read(65_536)
        if not chunk:
            break
        if callback is not None:
            callback(chunk)
        total += len(chunk)
        remaining = max(0, limit - len(captured))
        if remaining:
            captured.extend(chunk[:remaining])
    result[key] = CapturedStream(
        content=bytes(captured).decode("utf-8", errors="replace"),
        captured_bytes=len(captured),
        total_bytes=total,
        truncated=total > len(captured),
    )


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if is_windows():
        try:
            process.send_signal(windows_ctrl_break_event())
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        killpg = getattr(os, "killpg", None)
        if killpg is None:  # pragma: no cover - defensive for unusual POSIX runtimes
            process.terminate()
            return
        try:
            killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            process.kill()


def run_process(
    command: Sequence[str],
    stdout_limit: int,
    stderr_limit: int,
    env: Mapping[str, str] | None = None,
    stdout_callback: Callable[[bytes], None] | None = None,
    stderr_callback: Callable[[bytes], None] | None = None,
) -> ProcessResult:
    if not command:
        raise ValueError("wrapped command must not be empty")
    windows = is_windows()
    creationflags = windows_creation_flags() if windows else 0
    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        shell=False,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env) if env is not None else None,
        creationflags=creationflags,
        start_new_session=not windows,
    )
    assert process.stdout is not None and process.stderr is not None
    captured: dict[str, CapturedStream] = {}
    threads = [
        threading.Thread(
            target=_capture,
            args=(process.stdout, stdout_limit, captured, "stdout", stdout_callback),
        ),
        threading.Thread(
            target=_capture,
            args=(process.stderr, stderr_limit, captured, "stderr", stderr_callback),
        ),
    ]
    for thread in threads:
        thread.start()
    cancelled = False
    try:
        exit_code = process.wait()
    except KeyboardInterrupt:
        cancelled = True
        terminate_process_tree(process)
        exit_code = 130
    finally:
        for thread in threads:
            thread.join(timeout=10)
    empty = CapturedStream("", 0, 0, False)
    return ProcessResult(
        exit_code=exit_code,
        pid=process.pid,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        stdout=captured.get("stdout", empty),
        stderr=captured.get("stderr", empty),
        cancelled=cancelled,
    )
