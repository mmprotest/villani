from __future__ import annotations

import asyncio
import ctypes
import hashlib
import inspect
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from villani_ops.closed_loop.cli_runtime import (
    CliCancellationHandle,
    CliCancellationOrigin,
    CliEnvironmentPolicy,
    CliFailure,
    CliInvocation,
    CliOutputLimits,
    CliProcessSupervisor,
)
from villani_ops.closed_loop.schema_validation import validate_protocol_document


FAKE_CLI = (
    Path(__file__).resolve().parents[1] / "fixtures" / "cli_runtime" / "fake_cli.py"
)


def _resolved_environment(
    *,
    mode: str = "inherit",
    additions: dict[str, str] | None = None,
    overrides: dict[str, str] | None = None,
    removals: frozenset[str] = frozenset(),
    redaction_keys: frozenset[str] = frozenset(),
):
    return CliEnvironmentPolicy(
        mode=mode,  # type: ignore[arg-type]
        additions=additions or {},
        overrides=overrides or {},
        removals=removals,
        redaction_keys=redaction_keys,
    ).resolve()


def _invocation(
    tmp_path: Path,
    mode: str,
    *arguments: str,
    timeout: float = 5,
    grace: float = 0.25,
    limits: CliOutputLimits | None = None,
    environment=None,
    stdin: bytes | None = None,
    event_stream_format: str = "none",
    utf8_policy: str = "replacement",
    final_output_path: Path | None = None,
    require_final_output: bool = False,
    executable: Path | None = None,
    fake_path: Path = FAKE_CLI,
    prompt_reference: str | None = None,
    prompt_digest: str | None = None,
) -> CliInvocation:
    artifact_directory = tmp_path / "agent"
    workspace = tmp_path / "workspace space 雪"
    workspace.mkdir(parents=True, exist_ok=True)
    resolved = environment or _resolved_environment()
    return CliInvocation(
        executable=executable or Path(sys.executable),
        arguments=(str(fake_path), "--mode", mode, *arguments),
        cwd=workspace,
        stdin_bytes=stdin,
        environment=resolved.values,
        environment_metadata=resolved.metadata,
        environment_redaction_keys=resolved.redaction_keys,
        timeout_seconds=timeout,
        graceful_shutdown_seconds=grace,
        stdout_path=artifact_directory / "stdout.log",
        stderr_path=artifact_directory / "stderr.log",
        raw_event_path=artifact_directory / "raw-events.jsonl",
        output_limits=limits or CliOutputLimits(),
        role_workspace_identity={
            "run_id": "run_cli_runtime_test",
            "attempt_id": "attempt_001",
            "role": "coding",
            "workspace": "isolated_attempt",
        },
        target_repository_writable=False,
        prompt_artifact_reference=prompt_reference,
        prompt_sha256=prompt_digest,
        event_stream_format=event_stream_format,  # type: ignore[arg-type]
        utf8_policy=utf8_policy,  # type: ignore[arg-type]
        final_output_path=final_output_path,
        require_final_output=require_final_output,
    )


def _run(invocation: CliInvocation, handle: CliCancellationHandle | None = None):
    return asyncio.run(CliProcessSupervisor().run(invocation, handle))


async def _cancel_after(
    invocation: CliInvocation,
    *,
    delay: float = 0.15,
    origin: CliCancellationOrigin = CliCancellationOrigin.USER,
    wait_for_stdout: bool = False,
):
    handle = CliCancellationHandle()
    task = asyncio.create_task(CliProcessSupervisor().run(invocation, handle))
    if wait_for_stdout:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                if invocation.stdout_path.stat().st_size > 0:
                    break
            except OSError:
                pass
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("fake CLI did not produce output before cancellation")
    await asyncio.sleep(delay)
    assert handle.cancel(origin)
    return await task, handle


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        exit_code = ctypes.c_ulong()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
            exit_code.value == 259
        )
    finally:
        kernel.CloseHandle(handle)


def _wait_for_pid_file(path: Path, timeout: float = 3) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text(encoding="ascii").strip():
            return int(path.read_text(encoding="ascii"))
        time.sleep(0.01)
    raise AssertionError("fake child did not record its PID")


def _wait_dead(pid: int, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.02)
    assert not _pid_alive(pid), f"child process {pid} remained alive"


def test_shared_cli_runtime_boundary_is_provider_neutral_and_shell_free() -> None:
    invocation_fields = set(inspect.signature(CliInvocation).parameters)
    assert {"executable", "arguments", "cwd", "environment"} <= invocation_fields
    source = inspect.getsource(CliProcessSupervisor)
    assert "codex" not in source.lower()
    assert "claude" not in source.lower()
    assert "create_subprocess_shell" not in source
    assert "shell=True" not in source.replace(" ", "")


def test_successful_process_writes_complete_artifact_set(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path, "success", "--message", "complete")
    result = _run(invocation)
    assert result.infrastructure_state == "succeeded"
    assert result.exit_code == 0
    assert result.failure is None
    assert result.artifact_set_complete is True
    assert invocation.stdout_path.read_bytes() == b"complete\n"
    assert {path.name for path in invocation.stdout_path.parent.iterdir()} == {
        "invocation.json",
        "stdout.log",
        "stderr.log",
        "process-result.json",
        "raw-events.jsonl",
        "output-tail.json",
    }


def test_arguments_spaces_non_ascii_and_exact_cwd_are_preserved(tmp_path: Path) -> None:
    invocation = _invocation(
        tmp_path,
        "arguments",
        "--value",
        "two words",
        "--value",
        "雪 and $HOME && untouched",
    )
    result = _run(invocation)
    assert result.failure is None
    document = json.loads(invocation.stdout_path.read_text(encoding="utf-8"))
    assert document["values"] == ["two words", "雪 and $HOME && untouched"]
    assert Path(document["cwd"]).resolve() == invocation.cwd.resolve()


def test_stdin_is_delivered_and_closed(tmp_path: Path) -> None:
    payload = "stdin with spaces and 雪\n".encode()
    invocation = _invocation(tmp_path, "stdin", stdin=payload)
    result = _run(invocation)
    assert result.failure is None
    assert result.stdin_bytes_delivered == len(payload)
    assert invocation.stdout_path.read_bytes() == payload


def test_large_stdout_and_stderr_are_drained_concurrently(tmp_path: Path) -> None:
    size = 2_000_000
    invocation = _invocation(
        tmp_path,
        "dual-output",
        "--bytes",
        str(size),
        limits=CliOutputLimits(
            maximum_stdout_bytes=size + 1,
            maximum_stderr_bytes=size + 1,
        ),
    )
    result = _run(invocation)
    assert result.failure is None
    assert result.stdout.total_bytes_observed == size
    assert result.stderr.total_bytes_observed == size
    assert invocation.stdout_path.stat().st_size == size
    assert invocation.stderr_path.stat().st_size == size


def test_valid_jsonl_is_copied_and_validated_without_provider_parsing(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path, "valid-jsonl", event_stream_format="jsonl")
    result = _run(invocation)
    assert result.failure is None
    assert invocation.raw_event_path is not None
    events = [
        json.loads(line)
        for line in invocation.raw_event_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert invocation.raw_event_path.read_bytes() == invocation.stdout_path.read_bytes()


def test_stdout_may_close_before_process_completion(tmp_path: Path) -> None:
    invocation = _invocation(
        tmp_path, "close-stdout", "--seconds", "0.1", "--exit-code", "0"
    )
    result = _run(invocation)
    assert result.failure is None
    assert invocation.stdout_path.read_bytes() == b""
    assert b"stdout-closed" in invocation.stderr_path.read_bytes()


def test_nonzero_exit_is_infrastructure_failure(tmp_path: Path) -> None:
    result = _run(_invocation(tmp_path, "success", "--exit-code", "17"))
    assert result.infrastructure_state == "failed"
    assert result.failure == CliFailure.NONZERO_EXIT
    assert result.exit_code == 17


def test_timeout_preserves_partial_artifacts(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path, "sleep", "--seconds", "30", timeout=0.15)
    result = _run(invocation)
    assert result.infrastructure_state == "timed_out"
    assert result.failure == CliFailure.TIMEOUT
    assert result.timed_out is True
    assert result.cancellation_origin == CliCancellationOrigin.TIMEOUT
    assert result.target_repository_writable is False
    assert result.artifact_set_complete is True


def test_user_cancellation_is_distinct_and_preserves_artifacts(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path, "sleep", "--seconds", "30")
    result, _handle = asyncio.run(_cancel_after(invocation))
    assert result.infrastructure_state == "cancelled"
    assert result.failure == CliFailure.CANCELLED
    assert result.cancelled is True
    assert result.cancellation_origin == CliCancellationOrigin.USER
    assert result.artifact_set_complete is True


def test_cancellation_is_idempotent_under_concurrent_callers(tmp_path: Path) -> None:
    async def scenario():
        handle = CliCancellationHandle()
        invocation = _invocation(tmp_path, "sleep", "--seconds", "30")
        running = asyncio.create_task(CliProcessSupervisor().run(invocation, handle))
        await asyncio.sleep(0.1)
        outcomes = await asyncio.gather(
            *(
                asyncio.to_thread(handle.cancel, CliCancellationOrigin.CONTROLLER)
                for _ in range(12)
            )
        )
        return await running, handle, outcomes

    result, handle, outcomes = asyncio.run(scenario())
    assert outcomes.count(True) == 1
    assert handle.request_count == 12
    assert result.failure == CliFailure.CANCELLED
    assert sum(item.code == CliFailure.CANCELLED for item in result.failures) == 1


def test_parent_task_cancellation_becomes_service_shutdown_state(
    tmp_path: Path,
) -> None:
    async def scenario():
        invocation = _invocation(tmp_path, "sleep", "--seconds", "30")
        running = asyncio.create_task(CliProcessSupervisor().run(invocation))
        await asyncio.sleep(0.1)
        running.cancel()
        return await running

    result = asyncio.run(scenario())
    assert result.infrastructure_state == "cancelled"
    assert result.cancellation_origin == CliCancellationOrigin.PARENT_SERVICE_SHUTDOWN


def test_graceful_shutdown_is_requested_before_force(tmp_path: Path) -> None:
    # The Windows test host may not own a console to deliver CTRL_BREAK into;
    # exiting inside the grace window still proves that force is deferred. POSIX
    # additionally proves the signal handler path.
    invocation = _invocation(tmp_path, "graceful", "--seconds", "0.5", grace=1.0)
    result, _handle = asyncio.run(_cancel_after(invocation, delay=0.15))
    assert result.failure == CliFailure.CANCELLED
    assert result.graceful_termination_requested is True
    assert result.graceful_termination_succeeded is True
    assert result.forced_termination is False
    if os.name == "posix":
        assert b"graceful-shutdown" in invocation.stderr_path.read_bytes()


def test_ignored_shutdown_is_forced_after_grace_period(tmp_path: Path) -> None:
    invocation = _invocation(
        tmp_path, "ignore-termination", "--seconds", "30", grace=0.1
    )
    result, _handle = asyncio.run(_cancel_after(invocation, delay=0.2))
    assert result.failure == CliFailure.CANCELLED
    assert result.graceful_termination_succeeded is False
    assert result.forced_termination is True


@pytest.mark.parametrize("_repeat", range(5))
def test_child_process_tree_is_cleaned_repeatedly(tmp_path: Path, _repeat: int) -> None:
    child_pid_path = tmp_path / f"child-{_repeat}.pid"

    async def scenario():
        invocation = _invocation(
            tmp_path / str(_repeat),
            "spawn-child",
            "--seconds",
            "30",
            "--child-pid-path",
            str(child_pid_path),
            grace=0.1,
        )
        handle = CliCancellationHandle()
        running = asyncio.create_task(CliProcessSupervisor().run(invocation, handle))
        child_pid = await asyncio.to_thread(_wait_for_pid_file, child_pid_path)
        handle.cancel(CliCancellationOrigin.CONTROLLER)
        return await running, child_pid

    result, child_pid = asyncio.run(scenario())
    assert result.failure == CliFailure.CANCELLED
    _wait_dead(child_pid)


def test_missing_executable_is_classified_without_spawn(tmp_path: Path) -> None:
    invocation = _invocation(
        tmp_path, "success", executable=tmp_path / "missing executable"
    )
    result = _run(invocation)
    assert result.failure == CliFailure.EXECUTABLE_NOT_FOUND
    assert result.pid is None
    assert result.artifact_set_complete is True


def test_directory_is_not_a_runnable_executable(tmp_path: Path) -> None:
    result = _run(_invocation(tmp_path, "success", executable=tmp_path))
    assert result.failure == CliFailure.EXECUTABLE_NOT_RUNNABLE


def test_spawn_os_error_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_spawn(*_args, **_kwargs):
        raise OSError("fixture spawn failure")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_spawn)
    result = _run(_invocation(tmp_path, "success"))
    assert result.failure == CliFailure.SPAWN_FAILED
    assert result.pid is None


def test_stdin_pipe_failure_is_classified(tmp_path: Path) -> None:
    invocation = _invocation(
        tmp_path,
        "close-stdin",
        "--seconds",
        "0.2",
        stdin=b"x" * (8 * 1024 * 1024),
    )
    result = _run(invocation)
    assert CliFailure.STDIN_FAILED in {item.code for item in result.failures}


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX executable bits are not used on Windows"
)
def test_non_runnable_executable_is_classified_on_posix(tmp_path: Path) -> None:
    executable = tmp_path / "not-runnable"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR)
    result = _run(_invocation(tmp_path, "success", executable=executable))
    assert result.failure == CliFailure.EXECUTABLE_NOT_RUNNABLE


@pytest.mark.parametrize(
    ("stream_limit", "expected"),
    [
        ("stdout", CliFailure.STDOUT_LIMIT_EXCEEDED),
        ("stderr", CliFailure.STDERR_LIMIT_EXCEEDED),
    ],
)
def test_total_and_chunk_output_limits_are_failures(
    tmp_path: Path, stream_limit: str, expected: CliFailure
) -> None:
    large = 500_000
    limits = CliOutputLimits(
        maximum_stdout_bytes=4_096 if stream_limit == "stdout" else large + 1,
        maximum_stderr_bytes=4_096 if stream_limit == "stderr" else large + 1,
        maximum_stdout_chunk_bytes=64 if stream_limit == "stdout" else large + 1,
        maximum_stderr_chunk_bytes=64 if stream_limit == "stderr" else large + 1,
    )
    invocation = _invocation(
        tmp_path, "dual-output", "--bytes", str(large), limits=limits
    )
    result = _run(invocation)
    assert expected in {item.code for item in result.failures}
    stream = result.stdout if stream_limit == "stdout" else result.stderr
    assert stream.limit_exceeded is True
    assert stream.bytes_persisted <= 4_096


def test_oversized_jsonl_line_is_classified_and_bounded(tmp_path: Path) -> None:
    invocation = _invocation(
        tmp_path,
        "oversized-jsonl",
        "--bytes",
        "20000",
        event_stream_format="jsonl",
        limits=CliOutputLimits(maximum_event_line_bytes=512),
    )
    result = _run(invocation)
    assert CliFailure.EVENT_LINE_LIMIT_EXCEEDED in {
        item.code for item in result.failures
    }
    assert invocation.raw_event_path is not None
    assert invocation.raw_event_path.stat().st_size <= 512


def test_malformed_jsonl_is_provider_neutral_malformed_stream(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path, "malformed-jsonl", event_stream_format="jsonl")
    result = _run(invocation)
    assert CliFailure.MALFORMED_STREAM in {item.code for item in result.failures}
    assert invocation.raw_event_path is not None
    assert b'"sequence":1' in invocation.raw_event_path.read_bytes()


def test_strict_malformed_utf8_fails_and_replacement_policy_is_documented(
    tmp_path: Path,
) -> None:
    strict = _invocation(tmp_path / "strict", "invalid-utf8", utf8_policy="strict")
    strict_result = _run(strict)
    assert CliFailure.OUTPUT_DECODE_FAILED in {
        item.code for item in strict_result.failures
    }
    replacement = _invocation(
        tmp_path / "replacement", "invalid-utf8", utf8_policy="replacement"
    )
    replacement_result = _run(replacement)
    assert replacement_result.failure is None
    assert replacement_result.stdout.decode_replacements is True
    tail = json.loads(replacement.output_tail_path.read_text(encoding="utf-8"))
    assert "�" in tail["stdout"]


def test_partial_output_is_preserved_after_crash(tmp_path: Path) -> None:
    invocation = _invocation(tmp_path, "partial-crash", "--exit-code", "23")
    result = _run(invocation)
    assert result.failure == CliFailure.NONZERO_EXIT
    assert b"partial-stdout" in invocation.stdout_path.read_bytes()
    assert b"partial-stderr" in invocation.stderr_path.read_bytes()
    assert result.artifact_set_complete is True


def test_environment_policy_records_names_and_provenance_not_values(
    tmp_path: Path,
) -> None:
    secret = "runtime-secret-should-never-be-persisted"
    environment = _resolved_environment(
        mode="minimal",
        additions={"VISIBLE_NAME": "present", "TOP_SECRET": secret},
        overrides={"VISIBLE_NAME": "overridden"},
        redaction_keys=frozenset({"TOP_SECRET"}),
    )
    invocation = _invocation(
        tmp_path,
        "emit-environment-value",
        "--environment-name",
        "TOP_SECRET",
        environment=environment,
    )
    result = _run(invocation)
    assert result.failure is None
    invocation_document = json.loads(
        invocation.invocation_path.read_text(encoding="utf-8")
    )
    metadata = {item["name"]: item for item in invocation_document["environment"]}
    assert metadata["TOP_SECRET"] == {
        "name": "TOP_SECRET",
        "provenance": "addition",
        "redacted": True,
    }
    assert metadata["VISIBLE_NAME"]["provenance"] == "override"
    for artifact in invocation.stdout_path.parent.iterdir():
        assert secret.encode() not in artifact.read_bytes(), artifact
    assert b"[REDACTED]" in invocation.stdout_path.read_bytes()
    assert b"[REDACTED]" in invocation.stderr_path.read_bytes()


def test_secret_argument_is_redacted_from_invocation_and_output(tmp_path: Path) -> None:
    secret = "argument-secret-value-that-must-disappear"
    environment = _resolved_environment(
        mode="minimal",
        additions={"ARGUMENT_SECRET": secret},
        redaction_keys=frozenset({"ARGUMENT_SECRET"}),
    )
    invocation = _invocation(
        tmp_path,
        "success",
        "--message",
        secret,
        environment=environment,
    )
    result = _run(invocation)
    assert result.failure is None
    assert secret.encode() not in invocation.invocation_path.read_bytes()
    assert secret.encode() not in invocation.stdout_path.read_bytes()
    assert b"[REDACTED]" in invocation.stdout_path.read_bytes()


def test_prompt_is_recorded_only_by_reference_and_digest(tmp_path: Path) -> None:
    prompt = "governed prompt text that is not invocation metadata".encode()
    digest = f"sha256:{hashlib.sha256(prompt).hexdigest()}"
    invocation = _invocation(
        tmp_path,
        "stdin",
        stdin=prompt,
        prompt_reference="prompts/coding.txt",
        prompt_digest=digest,
    )
    result = _run(invocation)
    assert result.failure is None
    invocation_bytes = invocation.invocation_path.read_bytes()
    assert prompt not in invocation_bytes
    document = json.loads(invocation_bytes)
    assert document["stdin"]["artifact_reference"] == "prompts/coding.txt"
    assert document["stdin"]["sha256"] == digest


def test_runtime_json_artifacts_validate_against_normative_schemas(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path, "success")
    result = _run(invocation)
    assert result.failure is None
    for path in (
        invocation.invocation_path,
        invocation.process_result_path,
        invocation.output_tail_path,
    ):
        assert path is not None
        validate_protocol_document(json.loads(path.read_text(encoding="utf-8")))


def test_artifact_write_failure_is_classified_without_process_launch(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path, "success")
    stdout_path = tmp_path / "stdout.log"
    bad_stderr = tmp_path / "stderr-is-a-directory"
    bad_stderr.mkdir()
    invocation = replace(
        invocation,
        stdout_path=stdout_path,
        stderr_path=bad_stderr,
        raw_event_path=tmp_path / "raw-events.jsonl",
        invocation_path=tmp_path / "invocation.json",
        process_result_path=tmp_path / "process-result.json",
        output_tail_path=tmp_path / "output-tail.json",
    )
    result = _run(invocation)
    assert result.failure == CliFailure.ARTIFACT_WRITE_FAILED
    assert result.pid is None
    assert result.artifact_set_complete is False
    renamed = stdout_path.with_suffix(".checked")
    stdout_path.rename(renamed)
    renamed.rename(stdout_path)


def test_output_write_failure_during_execution_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation = _invocation(tmp_path, "success")
    original_open = Path.open

    class FailingOutput:
        def __init__(self, wrapped) -> None:
            self.wrapped = wrapped

        def write(self, _value: bytes) -> int:
            raise OSError("fixture output write failure")

        def __getattr__(self, name: str):
            return getattr(self.wrapped, name)

    def controlled_open(path: Path, *args, **kwargs):
        wrapped = original_open(path, *args, **kwargs)
        return FailingOutput(wrapped) if path == invocation.stdout_path else wrapped

    monkeypatch.setattr(Path, "open", controlled_open)
    result = _run(invocation)
    assert CliFailure.ARTIFACT_WRITE_FAILED in {item.code for item in result.failures}
    assert result.artifact_set_complete is False


def test_no_shell_api_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("shell subprocess API was invoked")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)
    result = _run(_invocation(tmp_path, "success"))
    assert result.failure is None


def test_repeated_runs_leave_no_open_artifact_handles(tmp_path: Path) -> None:
    for index in range(12):
        invocation = _invocation(tmp_path / str(index), "success")
        result = _run(invocation)
        assert result.failure is None
        for artifact in invocation.stdout_path.parent.iterdir():
            renamed = artifact.with_suffix(artifact.suffix + ".checked")
            artifact.rename(renamed)
            renamed.rename(artifact)


def test_output_after_cancellation_is_bounded_and_recorded(tmp_path: Path) -> None:
    invocation = _invocation(
        tmp_path,
        "output-until-killed",
        grace=0.15,
        limits=CliOutputLimits(maximum_stdout_bytes=2_000_000),
    )
    result, _handle = asyncio.run(
        _cancel_after(invocation, delay=0.05, wait_for_stdout=True)
    )
    assert result.failure == CliFailure.CANCELLED
    assert result.stdout.output_after_cancellation is True
    assert result.stdout.bytes_persisted <= 2_000_000


def test_process_tree_cleanup_does_not_target_unrelated_process(tmp_path: Path) -> None:
    unrelated = subprocess.Popen(
        [
            sys.executable,
            str(FAKE_CLI),
            "--mode",
            "sleep",
            "--seconds",
            "30",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        invocation = _invocation(
            tmp_path, "ignore-termination", "--seconds", "30", grace=0.1
        )
        result, _handle = asyncio.run(_cancel_after(invocation, delay=0.15))
        assert result.failure == CliFailure.CANCELLED
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        try:
            unrelated.wait(timeout=2)
        except subprocess.TimeoutExpired:
            unrelated.kill()
            unrelated.wait(timeout=2)


def test_final_output_requirement_is_provider_neutral(tmp_path: Path) -> None:
    final_output = tmp_path / "produced" / "workspace space 雪" / "final.txt"
    produced = _invocation(
        tmp_path / "produced",
        "final-output",
        "--path",
        str(final_output),
        final_output_path=final_output,
        require_final_output=True,
    )
    produced_result = _run(produced)
    assert produced_result.failure is None
    assert produced_result.final_output_present is True

    missing_path = tmp_path / "missing" / "final.txt"
    missing = _invocation(
        tmp_path / "missing-run",
        "success",
        final_output_path=missing_path,
        require_final_output=True,
    )
    missing_result = _run(missing)
    assert missing_result.failure == CliFailure.FINAL_OUTPUT_MISSING


@pytest.mark.skipif(os.name != "nt", reason="Windows path behavior")
def test_windows_paths_and_case_insensitive_path_environment(tmp_path: Path) -> None:
    copied_directory = tmp_path / "fake scripts with spaces 雪"
    copied_directory.mkdir()
    copied_fake = copied_directory / "fake cli.py"
    copied_fake.write_bytes(FAKE_CLI.read_bytes())
    path_key = next(key for key in os.environ if key.casefold() == "path")
    environment = _resolved_environment(
        mode="inherit", overrides={path_key.swapcase(): os.environ[path_key]}
    )
    invocation = _invocation(
        tmp_path,
        "success",
        "--message",
        "windows-path-ok",
        environment=environment,
        fake_path=copied_fake,
    )
    result = _run(invocation)
    assert result.failure is None
    assert invocation.stdout_path.read_text().strip() == "windows-path-ok"
    names = [item.name.casefold() for item in environment.metadata]
    assert names.count("path") == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX path behavior")
def test_posix_path_lookup_and_non_ascii_path(tmp_path: Path) -> None:
    executable = Path(sys.executable)
    environment = _resolved_environment(
        mode="minimal", additions={"PATH": str(executable.parent)}
    )
    invocation = _invocation(
        tmp_path,
        "success",
        "--message",
        "posix-path-ok",
        environment=environment,
        executable=Path(executable.name),
    )
    result = _run(invocation)
    assert result.failure is None
    assert invocation.stdout_path.read_text().strip() == "posix-path-ok"
