import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from villani_ops.core.backend import Backend
from villani_ops.execution_environment.secrets import _process_alive
from villani_ops.runners.base import RunnerContext
from villani_ops.runners import villani_code as runner_module
from villani_ops.runners.villani_code import (
    VillaniCodeRunner,
    provider_for_villani_code_cli,
)


def _wait_for(predicate: Callable[[], Any], *, timeout: float, description: str) -> Any:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.02)
    detail = f" ({type(last_error).__name__}: {last_error})" if last_error else ""
    raise AssertionError(f"timed out waiting for {description}{detail}")


def _read_json_artifact(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _heartbeat_value(path: Path) -> tuple[int, int] | None:
    value = _read_json_artifact(path)
    if value is None:
        return None
    sequence = value.get("sequence")
    if not isinstance(sequence, int):
        return None
    return sequence, path.stat().st_mtime_ns


def _test_process_alive(pid: int) -> bool:
    if not _process_alive(pid):
        return False
    if os.name != "nt":
        status = Path(f"/proc/{pid}/stat")
        try:
            fields = status.read_text(encoding="utf-8").split()
        except OSError:
            return True
        if len(fields) >= 3 and fields[2] == "Z":
            return False
    return True


def _force_test_tree_stopped(parent_pid: int | None, child_pid: int | None) -> None:
    if os.name == "nt":
        if parent_pid and _test_process_alive(parent_pid):
            subprocess.run(
                ["taskkill", "/PID", str(parent_pid), "/T", "/F"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        if child_pid and _test_process_alive(child_pid):
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        return
    if parent_pid and _test_process_alive(parent_pid):
        try:
            os.killpg(parent_pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if child_pid and _test_process_alive(child_pid):
        try:
            os.kill(child_pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


def _install_fake_villani_code(tmp_path: Path, monkeypatch) -> Path:
    exe = tmp_path / "villani-code"
    exe.write_text(
        "#!/usr/bin/env python\n"
        "import pathlib, sys\n"
        'pathlib.Path("args.txt").write_text("\\n".join(sys.argv))\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return exe


def _run_runner(tmp_path: Path, monkeypatch, provider: str):
    fake_command = _install_fake_villani_code(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    run = tmp_path / f"run-{provider.replace(' ', '-')}"
    run.mkdir()
    backend = Backend(
        name="b",
        provider=provider,
        base_url="http://127.0.0.1:1234/v1",
        model="villanis/models/qwen3.6-35b-a3b-ud-iq4_xs.gguf",
        api_key="secret",
        command_name=str(fake_command),
    )
    result = VillaniCodeRunner().run(
        RunnerContext(
            attempt_id="a1",
            repo_path=str(repo),
            task_instruction="do",
            backend=backend,
            run_dir=str(run),
            timeout_seconds=5,
        )
    )
    assert result.exit_code == 0
    return backend, repo, run


def _value_after(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_provider_mapping_function_for_villani_code_cli():
    assert provider_for_villani_code_cli("openai-compatible") == "openai"
    assert provider_for_villani_code_cli("openai_compatible") == "openai"
    assert provider_for_villani_code_cli("openai compatible") == "openai"
    assert provider_for_villani_code_cli("openai") == "openai"
    assert provider_for_villani_code_cli("anthropic") == "anthropic"
    assert provider_for_villani_code_cli("custom-provider") == "custom-provider"


def test_openai_compatible_maps_to_openai_for_villani_code_cli(tmp_path, monkeypatch):
    backend, repo, run = _run_runner(tmp_path, monkeypatch, "openai-compatible")

    args = (repo / "args.txt").read_text().splitlines()
    command_artifact = json.loads((run / "villani_code_command.json").read_text())

    assert _value_after(args, "--provider") == "openai"
    assert _value_after(command_artifact, "--provider") == "openai"
    assert _value_after(args, "--provider") != "openai-compatible"
    assert _value_after(command_artifact, "--provider") != "openai-compatible"
    assert backend.provider == "openai-compatible"


def test_openai_remains_openai_for_villani_code_cli(tmp_path, monkeypatch):
    _backend, repo, run = _run_runner(tmp_path, monkeypatch, "openai")

    args = (repo / "args.txt").read_text().splitlines()
    command_artifact = json.loads((run / "villani_code_command.json").read_text())

    assert _value_after(args, "--provider") == "openai"
    assert _value_after(command_artifact, "--provider") == "openai"


def test_anthropic_remains_anthropic_for_villani_code_cli(tmp_path, monkeypatch):
    _backend, repo, run = _run_runner(tmp_path, monkeypatch, "anthropic")

    args = (repo / "args.txt").read_text().splitlines()
    command_artifact = json.loads((run / "villani_code_command.json").read_text())

    assert _value_after(args, "--provider") == "anthropic"
    assert _value_after(command_artifact, "--provider") == "anthropic"


def test_debug_flags_and_api_key_redaction_remain_present(tmp_path, monkeypatch):
    _backend, repo, run = _run_runner(tmp_path, monkeypatch, "openai-compatible")

    args = (repo / "args.txt").read_text().splitlines()
    command_artifact = json.loads((run / "villani_code_command.json").read_text())

    assert _value_after(args, "--debug") == "trace"
    assert _value_after(args, "--debug-dir") == str(run / "villani_code_debug")
    assert _value_after(command_artifact, "--debug") == "trace"
    assert _value_after(command_artifact, "--debug-dir") == str(
        run / "villani_code_debug"
    )
    assert _value_after(args, "--api-key") == "secret"
    assert "secret" not in (run / "villani_code_command.json").read_text()
    assert _value_after(command_artifact, "--api-key") == "***REDACTED***"


def test_villani_code_runner_timeout_kills_child_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness = tmp_path / "process-tree-ready.json"
    child_started = tmp_path / "child-started.json"
    heartbeat = tmp_path / "child-heartbeat.json"
    child = tmp_path / "child.py"
    child.write_text(
        f"""import json, os, pathlib, time
started = pathlib.Path({str(child_started)!r})
heartbeat = pathlib.Path({str(heartbeat)!r})

def write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)

write(started, {{'child_pid': os.getpid(), 'monotonic_start': time.monotonic()}})
sequence = 0
while True:
    sequence += 1
    write(heartbeat, {{'child_pid': os.getpid(), 'sequence': sequence, 'monotonic': time.monotonic()}})
    time.sleep(0.1)
""",
        encoding="utf-8",
    )
    exe = tmp_path / "villani-code"
    exe.write_text(
        f"""#!{sys.executable}
import json, os, pathlib, subprocess, sys, time
readiness = pathlib.Path({str(readiness)!r})
child_started = pathlib.Path({str(child_started)!r})
started_at = time.monotonic()
descendant = subprocess.Popen([sys.executable, {str(child)!r}])
deadline = time.monotonic() + 10
while time.monotonic() < deadline and not child_started.is_file():
    if descendant.poll() is not None:
        raise SystemExit('descendant exited before readiness')
    time.sleep(0.02)
if not child_started.is_file() or descendant.poll() is not None:
    raise SystemExit('descendant did not become ready')
child_document = json.loads(child_started.read_text(encoding='utf-8'))
temporary = readiness.with_name(readiness.name + '.tmp')
with temporary.open('w', encoding='utf-8') as handle:
    json.dump({{
        'parent_pid': os.getpid(),
        'child_pid': child_document['child_pid'],
        'monotonic_start': started_at,
    }}, handle, sort_keys=True)
    handle.flush()
    os.fsync(handle.fileno())
os.replace(temporary, readiness)
time.sleep(30)
""",
        encoding="utf-8",
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    original_popen = subprocess.Popen
    observed: dict[str, Any] = {}

    class ReadinessPopen(original_popen):
        def communicate(self, input=None, timeout=None):  # type: ignore[no-untyped-def]
            document = _wait_for(
                lambda: _read_json_artifact(readiness),
                timeout=10,
                description="parent and descendant readiness artifact",
            )
            assert isinstance(document, dict)
            parent_pid = int(document["parent_pid"])
            child_pid = int(document["child_pid"])
            assert isinstance(document.get("monotonic_start"), (int, float))
            assert _test_process_alive(self.pid)
            assert _test_process_alive(parent_pid)
            assert _test_process_alive(child_pid)
            first = _wait_for(
                lambda: _heartbeat_value(heartbeat),
                timeout=5,
                description="first descendant heartbeat",
            )
            second = _wait_for(
                lambda: (
                    value
                    if (value := _heartbeat_value(heartbeat)) is not None
                    and value[0] > first[0]
                    else None
                ),
                timeout=5,
                description="advancing descendant heartbeat",
            )
            observed.update(
                {
                    "readiness": document,
                    "launcher_pid": self.pid,
                    "first_heartbeat": first,
                    "second_heartbeat": second,
                    "requested_timeout": timeout,
                }
            )
            # Start the production timeout only after the descendant handshake.
            return super().communicate(input=input, timeout=0.2)

    class SubprocessProxy:
        Popen = ReadinessPopen

        def __getattr__(self, name: str) -> Any:
            return getattr(subprocess, name)

    monkeypatch.setattr(runner_module, "subprocess", SubprocessProxy())
    parent_pid: int | None = None
    child_pid: int | None = None
    try:
        b = Backend(
            name="b",
            provider="local",
            model="m",
            api_key="dummy",
            command_name=str(exe),
            metadata={"allow_dummy_api_key": True},
        )
        res = VillaniCodeRunner().run(
            RunnerContext(
                attempt_id="a",
                repo_path=str(tmp_path),
                task_instruction="x",
                backend=b,
                timeout_seconds=1,
                run_dir=str(tmp_path / "run"),
            )
        )
        assert res.exit_code == 124 and "timed out" in res.stderr.lower()
        document = observed["readiness"]
        parent_pid = int(document["parent_pid"])
        child_pid = int(document["child_pid"])
        launcher_pid = int(observed["launcher_pid"])
        assert observed["requested_timeout"] == 1
        assert observed["second_heartbeat"][0] > observed["first_heartbeat"][0]
        _wait_for(
            lambda: not _test_process_alive(parent_pid),
            timeout=5,
            description="terminated parent process",
        )
        _wait_for(
            lambda: not _test_process_alive(child_pid),
            timeout=5,
            description="terminated descendant process",
        )
        _wait_for(
            lambda: not _test_process_alive(launcher_pid),
            timeout=5,
            description="terminated runner launcher process",
        )
        stopped = _heartbeat_value(heartbeat)
        assert stopped is not None
        time.sleep(0.4)
        assert _heartbeat_value(heartbeat) == stopped
        assert not _test_process_alive(parent_pid)
        assert not _test_process_alive(child_pid)
    finally:
        if parent_pid is None or child_pid is None:
            document = _read_json_artifact(readiness) or {}
            parent_pid = (
                int(document["parent_pid"]) if document.get("parent_pid") else None
            )
            child_pid = (
                int(document["child_pid"]) if document.get("child_pid") else None
            )
        _force_test_tree_stopped(parent_pid, child_pid)
        for artifact in (
            readiness,
            readiness.with_name(readiness.name + ".tmp"),
            child_started,
            child_started.with_name(child_started.name + ".tmp"),
            heartbeat,
            heartbeat.with_name(heartbeat.name + ".tmp"),
        ):
            artifact.unlink(missing_ok=True)
