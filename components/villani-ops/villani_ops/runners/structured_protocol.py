"""Bounded, shell-free process primitives for structured coding harnesses."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from villani_ops.closed_loop.agent_systems.models import (
    MAXIMUM_BUFFERED_EVENT_BYTES,
    MAXIMUM_HARNESS_MESSAGE_BYTES,
)
from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.subprocess_utils import resolve_command_prefix


@dataclass(frozen=True, slots=True)
class StructuredProtocolError(RuntimeError):
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


class _BoundedBytes:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._value = bytearray()
        self.total = 0
        self._lock = threading.Lock()

    def append(self, value: bytes) -> None:
        with self._lock:
            self.total += len(value)
            remaining = max(self.limit - len(self._value), 0)
            if remaining:
                self._value.extend(value[:remaining])

    @property
    def truncated(self) -> bool:
        return self.total > len(self._value)

    def text(self) -> str:
        with self._lock:
            return bytes(self._value).decode("utf-8", errors="replace")


def resolved_structured_command(
    command: str, execution_prefix: Sequence[str], arguments: Sequence[str]
) -> list[str]:
    if execution_prefix:
        return [*execution_prefix, command, *arguments]
    prefix = resolve_command_prefix(command)
    if prefix is None:
        raise StructuredProtocolError(
            "executable_not_found",
            f"Structured harness command {Path(command).name!r} was not found.",
        )
    return [*prefix, *arguments]


def terminate_process_tree(
    process: subprocess.Popen[bytes], *, grace_seconds: float = 2.0
) -> None:
    """Terminate a harness and descendants after protocol cancellation was attempted."""

    if process.poll() is not None:
        return
    get_process_group = getattr(os, "getpgid", None)
    kill_process_group = getattr(os, "killpg", None)
    if (
        os.name == "posix"
        and callable(get_process_group)
        and callable(kill_process_group)
    ):
        try:
            process_group = get_process_group(process.pid)
        except OSError:
            process_group = process.pid
        try:
            kill_process_group(process_group, signal.SIGTERM)
            process.wait(timeout=grace_seconds)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            kill_process_group(
                process_group, getattr(signal, "SIGKILL", signal.SIGTERM)
            )
        except OSError:
            process.kill()
    else:
        try:
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            process.wait(timeout=grace_seconds)
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
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=grace_seconds)


class JsonLineProcess:
    """Supervise newline-delimited JSON with bounded buffering and stderr capture."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        max_message_bytes: int = MAXIMUM_HARNESS_MESSAGE_BYTES,
        max_buffer_bytes: int = MAXIMUM_BUFFERED_EVENT_BYTES,
    ) -> None:
        if not command:
            raise ValueError("structured process command cannot be empty")
        self.max_message_bytes = max_message_bytes
        self.max_buffer_bytes = max_buffer_bytes
        self._queue: queue.Queue[tuple[str, bytes | StructuredProtocolError]] = (
            queue.Queue()
        )
        self._buffered_bytes = 0
        self._buffer_lock = threading.Lock()
        self._stderr = _BoundedBytes(MAXIMUM_HARNESS_MESSAGE_BYTES)
        popen_options: dict[str, Any] = {
            "cwd": str(cwd),
            "env": dict(env),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if os.name == "posix":
            popen_options["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.process = subprocess.Popen(list(command), **popen_options)
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _enqueue(self, kind: str, value: bytes | StructuredProtocolError) -> None:
        size = len(value) if isinstance(value, bytes) else 0
        with self._buffer_lock:
            if self._buffered_bytes + size > self.max_buffer_bytes:
                self._queue.put(
                    (
                        "error",
                        StructuredProtocolError(
                            "protocol_backpressure_exceeded",
                            "Structured harness exceeded the bounded event buffer.",
                        ),
                    )
                )
                terminate_process_tree(self.process)
                return
            self._buffered_bytes += size
        self._queue.put((kind, value))

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                line = self.process.stdout.readline(self.max_message_bytes + 2)
                if not line:
                    break
                if len(line) > self.max_message_bytes or not line.endswith(b"\n"):
                    self._enqueue(
                        "error",
                        StructuredProtocolError(
                            "protocol_message_oversized",
                            "Structured harness emitted an oversized or unterminated message.",
                        ),
                    )
                    terminate_process_tree(self.process)
                    return
                self._enqueue("line", line)
        except OSError as error:
            self._enqueue(
                "error",
                StructuredProtocolError(
                    "protocol_transport_error",
                    f"Structured harness stdout failed: {type(error).__name__}.",
                    retryable=True,
                ),
            )
        finally:
            self._queue.put(("eof", b""))

    def _read_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        try:
            for chunk in iter(lambda: stderr.read(65_536), b""):
                self._stderr.append(chunk)
        except OSError:
            return

    def send(self, message: Mapping[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if b"\n" in encoded or len(encoded) > self.max_message_bytes:
            raise StructuredProtocolError(
                "protocol_message_oversized",
                "Villani refused to send an oversized structured message.",
            )
        if self.process.poll() is not None:
            raise StructuredProtocolError(
                "protocol_process_exited",
                "Structured harness exited before Villani could send a message.",
                retryable=True,
            )
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(encoded + b"\n")
            self.process.stdin.flush()
        except OSError as error:
            raise StructuredProtocolError(
                "protocol_transport_error",
                f"Structured harness stdin failed: {type(error).__name__}.",
                retryable=True,
            ) from error

    def send_input_file(
        self,
        path: Path,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        cancellation_event: Any | None = None,
    ) -> int:
        """Stream a controlled UTF-8 prompt file to stdin without argv truncation."""

        if self.process.poll() is not None:
            raise StructuredProtocolError(
                "protocol_process_exited",
                "Structured harness exited before Villani could send controlled input.",
                retryable=True,
            )
        size = path.stat().st_size
        if size > max_bytes:
            raise StructuredProtocolError(
                "protocol_input_oversized",
                "Controlled harness input exceeded the configured 64 MiB bound.",
            )
        assert self.process.stdin is not None
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65_536), b""):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise StructuredProtocolError(
                            "protocol_cancelled",
                            "Controlled harness input was cancelled.",
                        )
                    self.process.stdin.write(chunk)
            self.process.stdin.flush()
        except OSError as error:
            raise StructuredProtocolError(
                "protocol_transport_error",
                f"Structured harness stdin failed: {type(error).__name__}.",
                retryable=True,
            ) from error
        return size

    def receive(self, timeout_seconds: float) -> dict[str, Any] | None:
        try:
            kind, value = self._queue.get(timeout=timeout_seconds)
        except queue.Empty:
            return None
        if isinstance(value, bytes):
            with self._buffer_lock:
                self._buffered_bytes = max(self._buffered_bytes - len(value), 0)
        if kind == "eof":
            return {"_villani_eof": True}
        if kind == "error":
            assert isinstance(value, StructuredProtocolError)
            raise value
        assert isinstance(value, bytes)
        try:
            parsed = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StructuredProtocolError(
                "protocol_malformed_output",
                f"Structured harness emitted malformed JSON: {type(error).__name__}.",
            ) from error
        if not isinstance(parsed, dict):
            raise StructuredProtocolError(
                "protocol_malformed_output",
                "Structured harness message must be a JSON object.",
            )
        return parsed

    def close_input(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass

    def terminate(self) -> None:
        terminate_process_tree(self.process)

    def wait(self, timeout_seconds: float) -> int:
        try:
            return self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            terminate_process_tree(self.process)
            return 124
        finally:
            self._stdout_thread.join(timeout=2)
            self._stderr_thread.join(timeout=2)

    @property
    def stderr(self) -> str:
        return self._stderr.text()

    @property
    def stderr_truncated(self) -> bool:
        return self._stderr.truncated


def write_redacted_jsonl(
    path: Path,
    records: Sequence[Mapping[str, Any]],
    *,
    secrets: tuple[str, ...] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(
            redact_data(dict(record), secrets=secrets),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    )
    path.write_text(payload, encoding="utf-8")


def deadline_remaining(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)


def bounded_utf8_text(value: str, limit: int = MAXIMUM_HARNESS_MESSAGE_BYTES) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")
