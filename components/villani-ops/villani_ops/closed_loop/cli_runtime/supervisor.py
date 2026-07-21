"""Asynchronous, provider-neutral supervision for exactly one CLI invocation."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from villani_ops.execution_environment.secrets import registered_secret_values

from ..durable_io import write_json_atomic
from ..event_writer import redact_data, redact_message
from .cancellation import CliCancellationHandle
from .models import (
    CliCancellationOrigin,
    CliEnvironmentVariable,
    CliFailure,
    CliFailureDetail,
    CliInvocation,
    CliInvocationRecord,
    CliOutputTail,
    CliProcessResult,
    CliStreamResult,
)
from .process_tree import (
    ProcessTreeCleanup,
    ProcessTreeController,
    subprocess_group_options,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _FailureCollector:
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self.items: list[CliFailureDetail] = []
        self.event = asyncio.Event()
        self.secrets = secrets

    def add(
        self,
        code: CliFailure,
        message: str,
        *,
        stream: str | None = None,
        configured_limit_bytes: int | None = None,
        observed_bytes: int | None = None,
    ) -> None:
        if any(item.code == code for item in self.items):
            return
        explicitly_redacted = redact_data(message, secrets=self.secrets)
        self.items.append(
            CliFailureDetail(
                code=code,
                message=redact_message(str(explicitly_redacted), limit=1000),
                stream=stream,  # type: ignore[arg-type]
                configured_limit_bytes=configured_limit_bytes,
                observed_bytes=observed_bytes,
            )
        )
        self.event.set()


class _BoundedTail:
    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self.value = bytearray()

    def append(self, data: bytes) -> None:
        if not data:
            return
        self.value.extend(data)
        excess = len(self.value) - self.maximum_bytes
        if excess > 0:
            del self.value[:excess]

    def text(self) -> str:
        return bytes(self.value).decode("utf-8", errors="replace")


class _StreamingSecretRedactor:
    """Redact known byte values even when a value spans two pipe reads."""

    def __init__(self, secrets: tuple[bytes, ...]) -> None:
        self.secrets = tuple(
            sorted({item for item in secrets if item}, key=len, reverse=True)
        )
        self.maximum_secret_bytes = max((len(item) for item in self.secrets), default=0)
        self.pending = bytearray()

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        self.pending.extend(data)
        if not self.secrets:
            value = bytes(self.pending)
            self.pending.clear()
            return value
        safe_start_limit = (
            len(self.pending)
            if final
            else max(len(self.pending) - self.maximum_secret_bytes + 1, 0)
        )
        if safe_start_limit <= 0:
            return b""
        output = bytearray()
        cursor = 0
        raw = bytes(self.pending)
        while cursor < safe_start_limit:
            match = next(
                (secret for secret in self.secrets if raw.startswith(secret, cursor)),
                None,
            )
            if match is not None:
                output.extend(b"[REDACTED]")
                cursor += len(match)
            else:
                output.append(raw[cursor])
                cursor += 1
        del self.pending[:cursor]
        return bytes(output)


class _Utf8Monitor:
    def __init__(
        self,
        policy: str,
        stream_name: str,
        failures: _FailureCollector,
    ) -> None:
        self.policy = policy
        self.stream_name = stream_name
        self.failures = failures
        self.replacements = False
        errors = "strict" if policy == "strict" else "replace"
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors=errors)

    def feed(self, data: bytes, *, final: bool = False) -> None:
        try:
            decoded = self.decoder.decode(data, final=final)
            self.replacements = self.replacements or "\ufffd" in decoded
        except UnicodeDecodeError as error:
            self.failures.add(
                CliFailure.OUTPUT_DECODE_FAILED,
                f"{self.stream_name} was not valid UTF-8 at byte {error.start}",
                stream=self.stream_name,
                observed_bytes=error.start,
            )


class _JsonlEvidence:
    def __init__(
        self,
        handle: BinaryIO,
        maximum_line_bytes: int,
        failures: _FailureCollector,
    ) -> None:
        self.handle = handle
        self.maximum_line_bytes = maximum_line_bytes
        self.failures = failures
        self.buffer = bytearray()
        self.line_number = 0
        self.total_bytes_observed = 0
        self.bytes_persisted = 0
        self.largest_line_bytes = 0
        self.limit_exceeded = False
        self.decode_replacements = False
        self.output_after_cancellation = False
        self._discarding = False

    def _write(self, data: bytes) -> None:
        try:
            written = self.handle.write(data)
            if written != len(data):
                raise OSError("short raw-event artifact write")
            self.bytes_persisted += written
        except Exception as error:
            self.failures.add(
                CliFailure.ARTIFACT_WRITE_FAILED,
                f"raw-event artifact write failed: {type(error).__name__}: {error}",
                stream="artifact",
            )

    def _line(self, line: bytes) -> None:
        self.line_number += 1
        content = line[:-1] if line.endswith(b"\n") else line
        if content.endswith(b"\r"):
            content = content[:-1]
        self.largest_line_bytes = max(self.largest_line_bytes, len(content))
        if len(content) > self.maximum_line_bytes:
            self.limit_exceeded = True
            self._write(line[: self.maximum_line_bytes])
            self.failures.add(
                CliFailure.EVENT_LINE_LIMIT_EXCEEDED,
                f"JSONL line {self.line_number} exceeded the configured byte limit",
                stream="events",
                configured_limit_bytes=self.maximum_line_bytes,
                observed_bytes=len(content),
            )
            return
        self._write(line)
        try:
            decoded = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            self.failures.add(
                CliFailure.OUTPUT_DECODE_FAILED,
                f"JSONL line {self.line_number} was not valid UTF-8",
                stream="events",
                observed_bytes=error.start,
            )
            return
        try:
            document = json.loads(decoded)
        except json.JSONDecodeError as error:
            self.failures.add(
                CliFailure.MALFORMED_STREAM,
                f"JSONL line {self.line_number} was malformed at column {error.colno}",
                stream="events",
            )
            return
        if not isinstance(document, dict):
            self.failures.add(
                CliFailure.MALFORMED_STREAM,
                f"JSONL line {self.line_number} must be an object",
                stream="events",
            )

    def consume(self, data: bytes, *, after_cancellation: bool) -> None:
        self.total_bytes_observed += len(data)
        self.output_after_cancellation = self.output_after_cancellation or (
            after_cancellation and bool(data)
        )
        for byte in data:
            if self._discarding:
                if byte == 10:
                    self._discarding = False
                continue
            self.buffer.append(byte)
            if byte == 10:
                line = bytes(self.buffer)
                self.buffer.clear()
                self._line(line)
            elif len(self.buffer) > self.maximum_line_bytes:
                observed = len(self.buffer)
                self.limit_exceeded = True
                self.line_number += 1
                self._write(bytes(self.buffer[: self.maximum_line_bytes]))
                self.buffer.clear()
                self._discarding = True
                self.failures.add(
                    CliFailure.EVENT_LINE_LIMIT_EXCEEDED,
                    f"JSONL line {self.line_number} exceeded the configured byte limit",
                    stream="events",
                    configured_limit_bytes=self.maximum_line_bytes,
                    observed_bytes=observed,
                )

    def finish(self, *, after_cancellation: bool) -> None:
        if self.buffer and not self._discarding:
            self.consume(b"", after_cancellation=after_cancellation)
            line = bytes(self.buffer)
            self.buffer.clear()
            self._line(line)

    def result(self, path: Path) -> CliStreamResult:
        return CliStreamResult(
            artifact_path=str(path),
            total_bytes_observed=self.total_bytes_observed,
            bytes_persisted=self.bytes_persisted,
            limit_exceeded=self.limit_exceeded,
            largest_read_bytes=self.largest_line_bytes,
            decode_replacements=self.decode_replacements,
            output_after_cancellation=self.output_after_cancellation,
        )


class _StreamCapture:
    def __init__(
        self,
        *,
        name: str,
        handle: BinaryIO,
        maximum_bytes: int,
        maximum_chunk_bytes: int,
        maximum_tail_bytes: int,
        utf8_policy: str,
        redactor: _StreamingSecretRedactor,
        failures: _FailureCollector,
        jsonl: _JsonlEvidence | None = None,
    ) -> None:
        self.name = name
        self.handle = handle
        self.maximum_bytes = maximum_bytes
        self.maximum_chunk_bytes = maximum_chunk_bytes
        self.redactor = redactor
        self.failures = failures
        self.jsonl = jsonl
        self.tail = _BoundedTail(maximum_tail_bytes)
        self.utf8 = _Utf8Monitor(utf8_policy, name, failures)
        self.total_bytes_observed = 0
        self.bytes_persisted = 0
        self.largest_read_bytes = 0
        self.limit_exceeded = False
        self.output_after_cancellation = False

    def _persist(self, data: bytes, *, after_cancellation: bool) -> None:
        if not data:
            return
        remaining = max(self.maximum_bytes - self.bytes_persisted, 0)
        bounded = data[:remaining]
        if len(data) > remaining:
            self.limit_exceeded = True
            self.failures.add(
                CliFailure.STDOUT_LIMIT_EXCEEDED
                if self.name == "stdout"
                else CliFailure.STDERR_LIMIT_EXCEEDED,
                f"redacted {self.name} artifact exceeded the configured byte limit",
                stream=self.name,
                configured_limit_bytes=self.maximum_bytes,
                observed_bytes=self.total_bytes_observed,
            )
        try:
            written = self.handle.write(bounded)
            if written != len(bounded):
                raise OSError(f"short {self.name} artifact write")
            self.bytes_persisted += written
        except OSError as error:
            self.failures.add(
                CliFailure.ARTIFACT_WRITE_FAILED,
                f"{self.name} artifact write failed: {type(error).__name__}: {error}",
                stream="artifact",
            )
            return
        self.tail.append(bounded)
        self.utf8.feed(bounded)
        if self.jsonl is not None:
            self.jsonl.consume(bounded, after_cancellation=after_cancellation)

    def consume(self, data: bytes, *, after_cancellation: bool) -> None:
        if not data:
            return
        self.total_bytes_observed += len(data)
        self.largest_read_bytes = max(self.largest_read_bytes, len(data))
        self.output_after_cancellation = (
            self.output_after_cancellation or after_cancellation
        )
        if len(data) > self.maximum_chunk_bytes:
            self.limit_exceeded = True
            self.failures.add(
                CliFailure.STDOUT_LIMIT_EXCEEDED
                if self.name == "stdout"
                else CliFailure.STDERR_LIMIT_EXCEEDED,
                f"one {self.name} read exceeded the configured chunk limit",
                stream=self.name,
                configured_limit_bytes=self.maximum_chunk_bytes,
                observed_bytes=len(data),
            )
        remaining_observed = max(
            self.maximum_bytes - (self.total_bytes_observed - len(data)), 0
        )
        accepted = data[:remaining_observed]
        self._persist(
            self.redactor.feed(accepted), after_cancellation=after_cancellation
        )
        if len(data) > remaining_observed:
            self.limit_exceeded = True
            self.failures.add(
                CliFailure.STDOUT_LIMIT_EXCEEDED
                if self.name == "stdout"
                else CliFailure.STDERR_LIMIT_EXCEEDED,
                f"total {self.name} exceeded the configured byte limit",
                stream=self.name,
                configured_limit_bytes=self.maximum_bytes,
                observed_bytes=self.total_bytes_observed,
            )

    def finish(self, *, after_cancellation: bool) -> None:
        self._persist(
            self.redactor.feed(b"", final=True), after_cancellation=after_cancellation
        )
        self.utf8.feed(b"", final=True)
        if self.jsonl is not None:
            self.jsonl.finish(after_cancellation=after_cancellation)

    def result(self, path: Path) -> CliStreamResult:
        return CliStreamResult(
            artifact_path=str(path),
            total_bytes_observed=self.total_bytes_observed,
            bytes_persisted=self.bytes_persisted,
            limit_exceeded=self.limit_exceeded,
            largest_read_bytes=self.largest_read_bytes,
            decode_replacements=self.utf8.replacements,
            output_after_cancellation=self.output_after_cancellation,
        )


def _environment_key(environment: dict[str, str], requested: str) -> str | None:
    if os.name != "nt":
        return requested if requested in environment else None
    requested_folded = requested.casefold()
    return next(
        (key for key in environment if key.casefold() == requested_folded), None
    )


def _known_secret_bytes(invocation: CliInvocation) -> tuple[bytes, ...]:
    values: list[str] = list(registered_secret_values())
    environment = dict(invocation.environment)
    for requested in invocation.environment_redaction_keys:
        key = _environment_key(environment, requested)
        if key is not None:
            values.append(environment[key])
    return tuple(value.encode("utf-8") for value in values if value)


def _redacted_arguments(
    invocation: CliInvocation, secret_values: tuple[bytes, ...]
) -> list[str]:
    decoded_secrets = tuple(
        value.decode("utf-8", errors="ignore") for value in secret_values if value
    )
    result: list[str] = []
    for index, argument in enumerate(invocation.arguments):
        if index in invocation.argument_redaction_indices:
            result.append("[REDACTED]")
            continue
        value = argument
        for secret in decoded_secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        result.append(str(redact_data(value, secrets=decoded_secrets)))
    return result


def _environment_metadata(invocation: CliInvocation) -> list[CliEnvironmentVariable]:
    if invocation.environment_metadata:
        return list(invocation.environment_metadata)
    redacted = {
        key.casefold() if os.name == "nt" else key
        for key in invocation.environment_redaction_keys
    }
    return [
        CliEnvironmentVariable(
            name=name,
            provenance="explicit",
            redacted=(name.casefold() if os.name == "nt" else name) in redacted,
        )
        for name in sorted(invocation.environment, key=lambda item: item.casefold())
    ]


def _invocation_record(
    invocation: CliInvocation,
    started_at: datetime,
    secret_values: tuple[bytes, ...],
) -> CliInvocationRecord:
    stdin_digest = (
        f"sha256:{hashlib.sha256(invocation.stdin_bytes).hexdigest()}"
        if invocation.stdin_bytes is not None
        else None
    )
    return CliInvocationRecord(
        executable=str(invocation.executable),
        executable_identity={"status": "unresolved", "sha256": None},
        arguments=_redacted_arguments(invocation, secret_values),
        environment=_environment_metadata(invocation),
        role_workspace_identity=redact_data(dict(invocation.role_workspace_identity)),
        target_repository_writable=invocation.target_repository_writable,
        cwd=str(invocation.cwd),
        stdin={
            "provided": invocation.stdin_bytes is not None,
            "size_bytes": len(invocation.stdin_bytes or b""),
            "artifact_reference": invocation.prompt_artifact_reference,
            "sha256": invocation.prompt_sha256 or stdin_digest,
        },
        timeout_seconds=invocation.timeout_seconds,
        graceful_shutdown_seconds=invocation.graceful_shutdown_seconds,
        limits=invocation.output_limits,
        event_stream_format=invocation.event_stream_format,
        utf8_policy=invocation.utf8_policy,
        final_output_path=(
            str(invocation.final_output_path)
            if invocation.final_output_path is not None
            else None
        ),
        require_final_output=invocation.require_final_output,
        started_at=started_at,
    )


def _resolve_executable(
    invocation: CliInvocation,
) -> tuple[Path | None, CliFailure | None]:
    raw = str(invocation.executable)
    contains_separator = any(
        separator in raw for separator in (os.sep, os.altsep) if separator
    )
    if invocation.executable.is_absolute() or contains_separator:
        candidate = (
            invocation.executable
            if invocation.executable.is_absolute()
            else invocation.cwd / invocation.executable
        )
        candidate = candidate.resolve(strict=False)
        if not candidate.exists():
            return None, CliFailure.EXECUTABLE_NOT_FOUND
    else:
        environment = dict(invocation.environment)
        path_key = _environment_key(environment, "PATH")
        candidate_value = shutil.which(
            raw, path=environment.get(path_key) if path_key is not None else None
        )
        if candidate_value is None:
            return None, CliFailure.EXECUTABLE_NOT_FOUND
        candidate = Path(candidate_value)
    if not candidate.is_file():
        return None, CliFailure.EXECUTABLE_NOT_RUNNABLE
    if os.name == "posix" and not os.access(candidate, os.X_OK):
        return None, CliFailure.EXECUTABLE_NOT_RUNNABLE
    return candidate, None


def _empty_stream(path: Path) -> CliStreamResult:
    return CliStreamResult(
        artifact_path=str(path),
        total_bytes_observed=0,
        bytes_persisted=0,
        limit_exceeded=False,
        largest_read_bytes=0,
        decode_replacements=False,
        output_after_cancellation=False,
    )


class CliProcessSupervisor:
    """Launch and supervise one fully specified external process."""

    async def run(
        self,
        invocation: CliInvocation,
        cancellation: CliCancellationHandle | None = None,
    ) -> CliProcessResult:
        cancellation = cancellation or CliCancellationHandle()
        started_at = _utc_now()
        started_monotonic = time.monotonic()
        secret_values = _known_secret_bytes(invocation)
        decoded_secret_values = tuple(
            value.decode("utf-8", errors="ignore") for value in secret_values if value
        )
        failures = _FailureCollector(decoded_secret_values)
        artifact_handles: list[BinaryIO] = []
        stdout_handle: BinaryIO | None = None
        stderr_handle: BinaryIO | None = None
        event_handle: BinaryIO | None = None
        process: asyncio.subprocess.Process | None = None
        tree: ProcessTreeController | None = None
        cleanup = ProcessTreeCleanup(
            status="not_required",
            graceful_requested=False,
            graceful_succeeded=False,
            forced=False,
        )
        timed_out = False
        cancelled = False
        cancellation_origin: CliCancellationOrigin | None = None
        termination_reason: str | None = None
        stdin_delivered = 0
        termination_started = asyncio.Event()
        stdout_capture: _StreamCapture | None = None
        stderr_capture: _StreamCapture | None = None
        jsonl_capture: _JsonlEvidence | None = None
        stream_tasks: list[asyncio.Task[None]] = []
        stdin_task: asyncio.Task[None] | None = None

        try:
            for path in (
                invocation.invocation_path,
                invocation.stdout_path,
                invocation.stderr_path,
                invocation.raw_event_path,
                invocation.output_tail_path,
                invocation.process_result_path,
            ):
                assert path is not None
                path.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = invocation.stdout_path.open("wb")
            artifact_handles.append(stdout_handle)
            stderr_handle = invocation.stderr_path.open("wb")
            artifact_handles.append(stderr_handle)
            assert invocation.raw_event_path is not None
            event_handle = invocation.raw_event_path.open("wb")
            artifact_handles.append(event_handle)
            assert invocation.invocation_path is not None
            write_json_atomic(
                invocation.invocation_path,
                redact_data(
                    _invocation_record(
                        invocation, started_at, secret_values
                    ).model_dump(mode="json"),
                    secrets=decoded_secret_values,
                ),
            )
        except Exception as error:
            failures.add(
                CliFailure.ARTIFACT_WRITE_FAILED,
                f"invocation artifact setup failed: {type(error).__name__}: {error}",
                stream="artifact",
            )

        if not failures.items and cancellation.is_cancelled:
            cancelled = True
            cancellation_origin = cancellation.origin or CliCancellationOrigin.USER
            termination_reason = cancellation_origin.value
            failures.add(CliFailure.CANCELLED, "invocation was cancelled before spawn")

        executable: Path | None = None
        if not failures.items:
            executable, executable_failure = _resolve_executable(invocation)
            if executable_failure is not None:
                failures.add(
                    executable_failure,
                    f"executable {str(invocation.executable)!r} is unavailable",
                )

        if not failures.items and executable is not None:
            try:
                process = await asyncio.create_subprocess_exec(
                    str(executable),
                    *invocation.arguments,
                    cwd=str(invocation.cwd),
                    env=dict(invocation.environment),
                    stdin=(
                        asyncio.subprocess.PIPE
                        if invocation.stdin_bytes is not None
                        else asyncio.subprocess.DEVNULL
                    ),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **subprocess_group_options(),
                )
                tree = ProcessTreeController(process)
                if os.name == "nt" and (
                    tree.windows_job is None or tree.windows_job_error is not None
                ):
                    failures.add(
                        CliFailure.PROCESS_TREE_CLEANUP_FAILED,
                        tree.windows_job_error
                        or "Windows Job Object attachment could not be proved",
                    )
            except FileNotFoundError:
                failures.add(
                    CliFailure.EXECUTABLE_NOT_FOUND,
                    f"executable {str(invocation.executable)!r} was not found at spawn",
                )
            except PermissionError:
                failures.add(
                    CliFailure.EXECUTABLE_NOT_RUNNABLE,
                    f"executable {str(invocation.executable)!r} is not runnable",
                )
            except OSError as error:
                failures.add(
                    CliFailure.SPAWN_FAILED,
                    f"process spawn failed: {type(error).__name__}: {error}",
                )

        async def read_stream(
            reader: asyncio.StreamReader, capture: _StreamCapture
        ) -> None:
            try:
                while True:
                    chunk = await reader.read(invocation.output_limits.read_chunk_bytes)
                    if not chunk:
                        break
                    capture.consume(
                        chunk, after_cancellation=termination_started.is_set()
                    )
            except OSError as error:
                failures.add(
                    CliFailure.UNKNOWN_INFRASTRUCTURE_FAILURE,
                    f"{capture.name} pipe read failed: {type(error).__name__}: {error}",
                    stream=capture.name,
                )
            finally:
                capture.finish(after_cancellation=termination_started.is_set())

        async def write_stdin() -> None:
            nonlocal stdin_delivered
            assert process is not None and process.stdin is not None
            try:
                data = invocation.stdin_bytes or b""
                if data:
                    process.stdin.write(data)
                    await process.stdin.drain()
                    stdin_delivered = len(data)
            except (BrokenPipeError, ConnectionError, OSError) as error:
                failures.add(
                    CliFailure.STDIN_FAILED,
                    f"stdin delivery failed: {type(error).__name__}: {error}",
                    stream="stdin",
                    observed_bytes=stdin_delivered,
                )
            finally:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionError, OSError):
                    pass

        if process is not None:
            assert process.stdout is not None and process.stderr is not None
            assert stdout_handle is not None and stderr_handle is not None
            assert event_handle is not None
            if invocation.event_stream_format == "jsonl":
                jsonl_capture = _JsonlEvidence(
                    event_handle,
                    invocation.output_limits.maximum_event_line_bytes,
                    failures,
                )
            stdout_capture = _StreamCapture(
                name="stdout",
                handle=stdout_handle,
                maximum_bytes=invocation.output_limits.maximum_stdout_bytes,
                maximum_chunk_bytes=invocation.output_limits.maximum_stdout_chunk_bytes,
                maximum_tail_bytes=invocation.output_limits.maximum_tail_bytes,
                utf8_policy=invocation.utf8_policy,
                redactor=_StreamingSecretRedactor(secret_values),
                failures=failures,
                jsonl=jsonl_capture,
            )
            stderr_capture = _StreamCapture(
                name="stderr",
                handle=stderr_handle,
                maximum_bytes=invocation.output_limits.maximum_stderr_bytes,
                maximum_chunk_bytes=invocation.output_limits.maximum_stderr_chunk_bytes,
                maximum_tail_bytes=invocation.output_limits.maximum_tail_bytes,
                utf8_policy=invocation.utf8_policy,
                redactor=_StreamingSecretRedactor(secret_values),
                failures=failures,
            )
            stream_tasks = [
                asyncio.create_task(read_stream(process.stdout, stdout_capture)),
                asyncio.create_task(read_stream(process.stderr, stderr_capture)),
            ]
            if invocation.stdin_bytes is not None:
                stdin_task = asyncio.create_task(write_stdin())

            process_wait = asyncio.create_task(process.wait())
            cancellation_wait = asyncio.create_task(cancellation.wait())
            failure_wait = asyncio.create_task(failures.event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {process_wait, cancellation_wait, failure_wait},
                    timeout=invocation.timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    timed_out = True
                    cancellation_origin = CliCancellationOrigin.TIMEOUT
                    termination_reason = "timeout"
                    failures.add(
                        CliFailure.TIMEOUT,
                        f"invocation exceeded {invocation.timeout_seconds} seconds",
                    )
                    termination_started.set()
                    assert tree is not None
                    cleanup = await tree.terminate(invocation.graceful_shutdown_seconds)
                elif cancellation_wait in done:
                    cancelled = True
                    cancellation_origin = cancellation_wait.result()
                    termination_reason = cancellation_origin.value
                    failures.add(
                        CliFailure.CANCELLED,
                        f"invocation cancelled by {cancellation_origin.value}",
                    )
                    termination_started.set()
                    assert tree is not None
                    cleanup = await tree.terminate(invocation.graceful_shutdown_seconds)
                elif failure_wait in done and failures.items:
                    termination_reason = "runtime_failure"
                    termination_started.set()
                    assert tree is not None
                    cleanup = await tree.terminate(invocation.graceful_shutdown_seconds)
                else:
                    assert tree is not None
                    cleanup = await tree.cleanup_after_exit(
                        invocation.graceful_shutdown_seconds
                    )
            except asyncio.CancelledError:
                cancelled = True
                cancellation_origin = CliCancellationOrigin.PARENT_SERVICE_SHUTDOWN
                termination_reason = cancellation_origin.value
                failures.add(
                    CliFailure.CANCELLED,
                    "invocation cancelled by parent service shutdown",
                )
                termination_started.set()
                assert tree is not None
                cleanup = await tree.terminate(invocation.graceful_shutdown_seconds)
            finally:
                for wait_task in (cancellation_wait, failure_wait):
                    wait_task.cancel()
                await asyncio.gather(
                    cancellation_wait, failure_wait, return_exceptions=True
                )
                if process.returncode is None and tree is not None:
                    termination_started.set()
                    cleanup = await tree.terminate(invocation.graceful_shutdown_seconds)
                await process.wait()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*stream_tasks, return_exceptions=True),
                        timeout=max(invocation.graceful_shutdown_seconds, 1.0) + 1.0,
                    )
                except asyncio.TimeoutError:
                    failures.add(
                        CliFailure.PROCESS_TREE_CLEANUP_FAILED,
                        "output pipes did not close after process-tree cleanup",
                    )
                    for stream_task in stream_tasks:
                        stream_task.cancel()
                    await asyncio.gather(*stream_tasks, return_exceptions=True)
                if stdin_task is not None:
                    await asyncio.gather(stdin_task, return_exceptions=True)
                process_wait.cancel()
                await asyncio.gather(process_wait, return_exceptions=True)

        if cleanup.status == "failed":
            failures.add(
                CliFailure.PROCESS_TREE_CLEANUP_FAILED,
                cleanup.error or "process-tree cleanup failed",
            )

        exit_code = process.returncode if process is not None else None
        if (
            process is not None
            and exit_code not in {None, 0}
            and not (timed_out or cancelled or failures.items)
        ):
            failures.add(
                CliFailure.NONZERO_EXIT,
                f"external process exited with code {exit_code}",
            )
        elif (
            process is not None
            and exit_code not in {None, 0}
            and not any(
                item.code
                in {
                    CliFailure.TIMEOUT,
                    CliFailure.CANCELLED,
                    CliFailure.STDOUT_LIMIT_EXCEEDED,
                    CliFailure.STDERR_LIMIT_EXCEEDED,
                    CliFailure.EVENT_LINE_LIMIT_EXCEEDED,
                    CliFailure.OUTPUT_DECODE_FAILED,
                    CliFailure.MALFORMED_STREAM,
                    CliFailure.ARTIFACT_WRITE_FAILED,
                    CliFailure.STDIN_FAILED,
                }
                for item in failures.items
            )
        ):
            failures.add(
                CliFailure.NONZERO_EXIT,
                f"external process exited with code {exit_code}",
            )

        final_output_present = (
            invocation.final_output_path.is_file()
            if invocation.final_output_path is not None
            else None
        )
        if invocation.require_final_output and not final_output_present:
            failures.add(
                CliFailure.FINAL_OUTPUT_MISSING,
                "the required final-output file was not produced",
            )

        for handle in artifact_handles:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError as error:
                failures.add(
                    CliFailure.ARTIFACT_WRITE_FAILED,
                    f"artifact flush failed: {type(error).__name__}: {error}",
                    stream="artifact",
                )
            finally:
                handle.close()

        stdout_result = (
            stdout_capture.result(invocation.stdout_path)
            if stdout_capture is not None
            else _empty_stream(invocation.stdout_path)
        )
        stderr_result = (
            stderr_capture.result(invocation.stderr_path)
            if stderr_capture is not None
            else _empty_stream(invocation.stderr_path)
        )
        assert invocation.raw_event_path is not None
        raw_event_result = (
            jsonl_capture.result(invocation.raw_event_path)
            if jsonl_capture is not None
            else _empty_stream(invocation.raw_event_path)
        )

        tail = CliOutputTail(
            stdout=stdout_capture.tail.text() if stdout_capture is not None else "",
            stderr=stderr_capture.tail.text() if stderr_capture is not None else "",
            maximum_tail_bytes=invocation.output_limits.maximum_tail_bytes,
            utf8_policy=invocation.utf8_policy,
            stdout_decode_replacements=(
                stdout_capture.utf8.replacements
                if stdout_capture is not None
                else False
            ),
            stderr_decode_replacements=(
                stderr_capture.utf8.replacements
                if stderr_capture is not None
                else False
            ),
        )

        if timed_out:
            infrastructure_state = "timed_out"
        elif cancelled:
            infrastructure_state = "cancelled"
        elif failures.items:
            infrastructure_state = "failed"
        else:
            infrastructure_state = "succeeded"
        completed_at = _utc_now()
        primary_failure = failures.items[0].code if failures.items else None
        assert invocation.invocation_path is not None
        assert invocation.output_tail_path is not None
        assert invocation.process_result_path is not None
        result = CliProcessResult(
            infrastructure_state=infrastructure_state,  # type: ignore[arg-type]
            failure=primary_failure,
            failures=list(failures.items),
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
            pid=process.pid if process is not None else None,
            exit_code=exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            cancellation_origin=cancellation_origin,
            termination_reason=termination_reason,
            graceful_termination_requested=cleanup.graceful_requested,
            graceful_termination_succeeded=cleanup.graceful_succeeded,
            forced_termination=cleanup.forced,
            cleanup_status=cleanup.status,  # type: ignore[arg-type]
            cleanup_error=(
                redact_message(
                    str(redact_data(cleanup.error, secrets=decoded_secret_values)),
                    limit=1000,
                )
                if cleanup.error
                else None
            ),
            target_repository_writable=invocation.target_repository_writable,
            stdin_bytes_delivered=stdin_delivered,
            stdout=stdout_result,
            stderr=stderr_result,
            raw_events=raw_event_result,
            final_output_path=(
                str(invocation.final_output_path)
                if invocation.final_output_path is not None
                else None
            ),
            final_output_present=final_output_present,
            invocation_artifact=str(invocation.invocation_path),
            output_tail_artifact=str(invocation.output_tail_path),
            process_result_artifact=str(invocation.process_result_path),
            artifact_set_complete=False,
        )
        try:
            write_json_atomic(
                invocation.output_tail_path,
                redact_data(
                    tail.model_dump(mode="json"), secrets=decoded_secret_values
                ),
            )
            artifact_setup_failed = any(
                item.code == CliFailure.ARTIFACT_WRITE_FAILED
                for item in result.failures
            )
            expected_before_result = (
                invocation.invocation_path,
                invocation.stdout_path,
                invocation.stderr_path,
                invocation.raw_event_path,
                invocation.output_tail_path,
            )
            artifact_set_complete = not artifact_setup_failed and all(
                path is not None and path.is_file() for path in expected_before_result
            )
            complete_result = result.model_copy(
                update={"artifact_set_complete": artifact_set_complete}
            )
            write_json_atomic(
                invocation.process_result_path,
                redact_data(
                    complete_result.model_dump(mode="json"),
                    secrets=decoded_secret_values,
                ),
            )
            result = complete_result
        except Exception as error:
            artifact_detail = CliFailureDetail(
                code=CliFailure.ARTIFACT_WRITE_FAILED,
                message=redact_message(
                    str(
                        redact_data(
                            f"result artifact write failed: {type(error).__name__}: {error}",
                            secrets=decoded_secret_values,
                        )
                    ),
                    limit=1000,
                ),
                stream="artifact",
            )
            all_failures = [*result.failures, artifact_detail]
            result = result.model_copy(
                update={
                    "infrastructure_state": "failed",
                    "failure": result.failure or CliFailure.ARTIFACT_WRITE_FAILED,
                    "failures": all_failures,
                    "artifact_set_complete": False,
                }
            )
        return result


__all__ = ["CliProcessSupervisor"]
