from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.adapters import (
    EvidenceSelectorAdapter,
    PatchMaterializerAdapter,
    VillaniVerifierAdapter,
)
from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    CliAgentSystemConfig,
)
from villani_ops.closed_loop.claude_code_cli.attempt import (
    ClaudeCodeCliAttemptAdapter,
)
from villani_ops.closed_loop.claude_code_cli.driver import ClaudeCodeCliDriver
from villani_ops.closed_loop.claude_code_cli.events import (
    ClaudeEventParseError,
    parse_claude_events,
)
from villani_ops.closed_loop.claude_code_cli.models import (
    ClaudeFailure,
    ClaudeProbeResult,
)
from villani_ops.closed_loop.claude_code_cli.prompt import (
    build_claude_coding_prompt,
)
from villani_ops.closed_loop.codex_cli.attempt import CodexCliAttemptAdapter
from villani_ops.closed_loop.codex_cli.driver import CodexCliDriver
from villani_ops.closed_loop.codex_cli.models import CodexProbeResult
from villani_ops.closed_loop.controller import ClosedLoopController
from villani_ops.closed_loop.interfaces import AttemptContext, ClosedLoopRunRequest
from villani_ops.tests.closed_loop.fakes import (
    FakeClassifier,
    FakeMonotonic,
    FakePolicyEngine,
    FixedNow,
    StableIds,
    backend,
    policy,
)


HERE = Path(__file__).resolve().parent
FAKE_CLAUDE = HERE / "fixtures" / "claude_code_cli" / "fake_claude.py"
FAKE_CODEX = HERE / "fixtures" / "codex_cli" / "fake_codex.py"
EVENT_FIXTURES = HERE / "fixtures" / "claude_code_cli"


def _git(
    repository: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        capture_output=True,
        check=check,
    )


def _repository(tmp_path: Path, name: str = "target repository ü") -> Path:
    repository = tmp_path / name
    repository.mkdir(parents=True)
    (repository / "target.txt").write_text("baseline\n", encoding="utf-8")
    (repository / "rename_me.txt").write_text("rename\n", encoding="utf-8")
    (repository / "delete_me.txt").write_text("delete\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "config", "user.email", "claude-fixture@example.invalid")
    _git(repository, "config", "user.name", "Claude Fixture")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "baseline")
    return repository


def _system(
    *,
    executable: str | None = None,
    timeout_seconds: int = 5,
    provider_options: dict[str, Any] | None = None,
    instruction_policy: str = "native_project",
) -> CliAgentSystemConfig:
    options: dict[str, Any] = {
        "launcher_arguments": [str(FAKE_CLAUDE)],
        "graceful_shutdown_seconds": 0.25,
        "max_turns": 8,
    }
    options.update(provider_options or {})
    return CliAgentSystemConfig(
        kind="cli_agent",
        id="claude-coder",
        enabled=True,
        driver="claude_code",
        executable=executable or sys.executable,
        model="claude-sonnet-fixture",
        roles={AgentRole.CODING},
        timeout_seconds=timeout_seconds,
        max_parallel=2,
        instruction_policy=instruction_policy,  # type: ignore[arg-type]
        permission_profile="workspace_write",
        environment_policy="inherit",
        provider_options=options,
    )


def _ready_probe(system: CliAgentSystemConfig) -> ClaudeProbeResult:
    capabilities = {
        "print_mode": True,
        "stream_json": True,
        "structured_output": True,
        "no_session_persistence": True,
        "model_selection": True,
        "permission_mode": True,
        "tools": True,
        "allowed_tools": True,
        "verbose": True,
        "no_chrome": True,
        "bare": True,
        "settings": True,
        "setting_sources": True,
        "strict_mcp_config": True,
        "mcp_config": True,
        "disable_slash_commands": True,
        "stdin_prompt": True,
        "max_turns": True,
    }
    return ClaudeProbeResult(
        system_id=system.id,
        checked_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        configured_executable=system.executable,
        resolved_executable=str(Path(system.executable).resolve()),
        exact_version_output="2.1.138 (Claude Code fixture)",
        parsed_version="2.1.138",
        authentication_ready=True,
        authentication_method="claude_ai",
        doctor_ready=True,
        capabilities=capabilities,
        resolved_flags={"print": "-p", "allowed_tools": "--allowedTools"},
        ready=True,
    )


def _context(
    tmp_path: Path,
    repository: Path,
    *,
    attempt_id: str = "attempt_001",
    cancellation_event: threading.Event | None = None,
    task: str | None = None,
) -> AttemptContext:
    run_directory = tmp_path / "run"
    attempt_directory = run_directory / "attempts" / attempt_id
    attempt_directory.mkdir(parents=True, exist_ok=True)
    return AttemptContext(
        run_id="run_claude_fixture",
        trace_id="trace_claude_fixture",
        task_id="task_claude_fixture",
        attempt_id=attempt_id,
        ordinal=1,
        task=task
        or "Change target.txt exactly as requested.\nKeep this task verbatim.",
        repository_path=str(repository),
        success_criteria="A non-empty isolated Git patch exists.\nTests are reported.",
        requires_file_changes=True,
        backend_name="claude-coder",
        model="claude-sonnet-fixture",
        policy_configuration={"isolation": {}},
        run_directory=run_directory,
        attempt_directory=attempt_directory,
        baseline_sha256="b" * 64,
        cancellation_event=cancellation_event,
    )


def _adapter(system: CliAgentSystemConfig | None = None) -> ClaudeCodeCliAttemptAdapter:
    configured = system or _system()
    driver = ClaudeCodeCliDriver(configured)
    return ClaudeCodeCliAttemptAdapter(driver, probe=_ready_probe(configured))


def _run_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    *,
    timeout_seconds: int = 5,
    task: str | None = None,
) -> tuple[Any, AttemptContext, Path]:
    repository = _repository(tmp_path)
    context = _context(tmp_path, repository, task=task)
    monkeypatch.setenv("VILLANI_FAKE_CLAUDE_SCENARIO", scenario)
    result = _adapter(_system(timeout_seconds=timeout_seconds)).run(context)
    return result, context, repository


def _source_snapshot(repository: Path) -> tuple[str, str]:
    return (
        _git(repository, "rev-parse", "HEAD").stdout.strip(),
        _git(repository, "status", "--porcelain", "--untracked-files=all").stdout,
    )


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        try:
            return bool(
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ) and (exit_code.value == 259)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


def test_official_stream_fixture_maps_command_edit_result_and_usage() -> None:
    parsed = parse_claude_events(
        EVENT_FIXTURES / "events-success.jsonl",
        started_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        run_id="run_fixture",
        attempt_id="attempt_fixture",
        worktree_path="worktree",
        baseline_sha256="a" * 64,
    )

    names = [event.event_type for event in parsed.runtime_events]
    assert parsed.session_id == "session-fixture"
    assert parsed.input_tokens == 50
    assert parsed.output_tokens == 10
    assert parsed.total_cost_usd == 0.01
    assert parsed.structured_output["status"] == "completed"
    assert "command_started" in names
    assert "command_completed" in names
    assert "file_write_started" in names
    assert "file_write" in names
    assert "agent_message" in names
    assert names[-1] == "turn_completed"


def test_unknown_stream_event_is_preserved_namespaced() -> None:
    parsed = parse_claude_events(
        EVENT_FIXTURES / "events-unknown.jsonl",
        started_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        run_id="run_fixture",
        attempt_id="attempt_fixture",
        worktree_path="worktree",
        baseline_sha256=None,
    )
    unknown = next(
        event
        for event in parsed.runtime_events
        if event.event_type == "claude_code.raw_event"
    )
    assert unknown.payload["namespace"] == "claude_code.raw"
    assert unknown.payload["event"]["provider_extension"] == {"answer": 42}


def test_malformed_stream_reports_exact_line_and_column(tmp_path: Path) -> None:
    fixture = tmp_path / "malformed.jsonl"
    fixture.write_text('{"type":"system"}\n{broken\n', encoding="utf-8")
    with pytest.raises(ClaudeEventParseError, match=r"line 2 is malformed at column 2"):
        parse_claude_events(
            fixture,
            started_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
            run_id="run_fixture",
            attempt_id="attempt_fixture",
            worktree_path="worktree",
            baseline_sha256=None,
        )


def test_probe_records_exact_version_auth_doctor_and_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "VILLANI_FAKE_CLAUDE_AUTH",
        "VILLANI_FAKE_CLAUDE_UNSUPPORTED",
        "VILLANI_FAKE_CLAUDE_DOCTOR",
    ):
        monkeypatch.delenv(name, raising=False)
    probe = ClaudeCodeCliDriver(_system()).probe()
    assert probe.ready is True
    assert probe.exact_version_output == "2.1.138 (Claude Code fixture)"
    assert probe.parsed_version == "2.1.138"
    assert probe.authentication_method == "claude_ai"
    assert probe.doctor_ready is True
    assert all(probe.capabilities.values())
    assert probe.resolved_flags["allowed_tools"] == "--allowedTools"


@pytest.mark.parametrize(
    ("environment_name", "value", "failure"),
    [
        (
            "VILLANI_FAKE_CLAUDE_AUTH",
            "missing",
            ClaudeFailure.NOT_AUTHENTICATED,
        ),
        (
            "VILLANI_FAKE_CLAUDE_UNSUPPORTED",
            "1",
            ClaudeFailure.MISSING_STRUCTURED_OUTPUT_CAPABILITY,
        ),
        (
            "VILLANI_FAKE_CLAUDE_DOCTOR",
            "failed",
            ClaudeFailure.AMBIENT_STARTUP_FAILURE,
        ),
    ],
)
def test_probe_readiness_failures_are_actionable(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    value: str,
    failure: ClaudeFailure,
) -> None:
    monkeypatch.setenv(environment_name, value)
    probe = ClaudeCodeCliDriver(_system()).probe()
    assert probe.ready is False
    assert failure in probe.failures
    assert probe.messages


def test_probe_reports_missing_executable_without_spawning(tmp_path: Path) -> None:
    probe = ClaudeCodeCliDriver(
        _system(executable=str(tmp_path / "missing claude executable"))
    ).probe()
    assert probe.ready is False
    assert probe.failures == [ClaudeFailure.NOT_INSTALLED]
    assert probe.resolved_executable is None


def test_driver_builds_safe_stdin_noninteractive_no_session_invocation(
    tmp_path: Path,
) -> None:
    system = _system()
    driver = ClaudeCodeCliDriver(system)
    worktree = tmp_path / "work tree 雪"
    agent = tmp_path / "agent"
    worktree.mkdir()
    agent.mkdir()
    schema = (
        Path(__file__).resolve().parents[4]
        / "schemas"
        / "v1"
        / "claude-coder-result.schema.json"
    )
    prompt = build_claude_coding_prompt(
        task="verbatim task",
        success_criteria="verbatim criteria",
        attempt_id="attempt_001",
        worktree=worktree,
        instruction_policy="native_project",
    )
    invocation = driver.build_invocation(
        probe=_ready_probe(system),
        worktree=worktree,
        agent_directory=agent,
        prompt_bytes=prompt.bytes,
        prompt_reference="attempts/attempt_001/agent/prompt.txt",
        prompt_sha256=prompt.sha256,
        output_schema_path=schema,
        run_id="run_fixture",
        attempt_id="attempt_001",
        baseline_sha256="b" * 64,
    )
    command = driver.safe_command(invocation)
    assert invocation.cwd == worktree.resolve()
    assert invocation.stdin_bytes == prompt.bytes
    assert command[:3] == (str(Path(sys.executable).resolve()), str(FAKE_CLAUDE), "-p")
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--no-session-persistence" in command
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert command[command.index("--tools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    assert (
        command[command.index("--allowedTools") + 1] == "Bash,Read,Edit,Write,Glob,Grep"
    )
    assert command[command.index("--json-schema") + 1] == "<inline-coder-result-schema>"
    assert "verbatim task" not in " ".join(command)
    forbidden = {
        "--dangerously-skip-permissions",
        "--resume",
        "--continue",
        "--teleport",
        "--remote",
    }
    assert forbidden.isdisjoint(command)


def test_villani_controlled_uses_bare_empty_mcp_and_disabled_ambient_features(
    tmp_path: Path,
) -> None:
    system = _system(instruction_policy="villani_controlled")
    driver = ClaudeCodeCliDriver(system)
    worktree = tmp_path / "controlled"
    agent = tmp_path / "agent"
    worktree.mkdir()
    agent.mkdir()
    settings = agent / "settings.json"
    mcp = agent / "mcp.json"
    schema = (
        Path(__file__).resolve().parents[4]
        / "schemas"
        / "v1"
        / "claude-coder-result.schema.json"
    )
    settings.write_text("{}", encoding="utf-8")
    mcp.write_text('{"mcpServers":{}}', encoding="utf-8")
    prompt = build_claude_coding_prompt(
        task="task",
        success_criteria="criteria",
        attempt_id="attempt_001",
        worktree=worktree,
        instruction_policy="villani_controlled",
    )
    invocation = driver.build_invocation(
        probe=_ready_probe(system),
        worktree=worktree,
        agent_directory=agent,
        prompt_bytes=prompt.bytes,
        prompt_reference="agent/prompt.txt",
        prompt_sha256=prompt.sha256,
        output_schema_path=schema,
        run_id="run",
        attempt_id="attempt_001",
        baseline_sha256=None,
        controlled_settings_path=settings,
        controlled_mcp_path=mcp,
    )
    assert "--bare" in invocation.arguments
    assert "--strict-mcp-config" in invocation.arguments
    assert "--disable-slash-commands" in invocation.arguments
    assert "--setting-sources=" in invocation.arguments
    assert (
        invocation.role_workspace_identity["project_user_discovery_permitted"] is False
    )
    assert "hooks" in invocation.role_workspace_identity["disabled_ambient_features"]


def test_successful_patch_records_canonical_artifacts_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, context, repository = _run_scenario(tmp_path, monkeypatch, "success")
    assert result.status == "completed"
    assert result.error is None
    assert result.patch and "claude change" in result.patch
    assert result.metadata["changed_files"] == ["target.txt"]
    assert result.runner_telemetry["cost_usd"] == 0.0123
    assert _source_snapshot(repository)[1] == ""
    assert (repository / "target.txt").read_text(encoding="utf-8") == "baseline\n"
    names = {event.event_type for event in result.runtime_events}
    assert {
        "command_started",
        "command_completed",
        "file_write",
        "agent_message",
    } <= names
    agent = context.attempt_directory / "agent"
    repository_artifacts = context.attempt_directory / "repository"
    for path in (
        agent / "provider.json",
        agent / "invocation.json",
        agent / "prompt.txt",
        agent / "prompt.digest",
        agent / "stdout.log",
        agent / "stderr.log",
        agent / "claude-events.jsonl",
        agent / "normalized-events.jsonl",
        agent / "final-output.json",
        agent / "normalized-result.json",
        agent / "process-result.json",
        repository_artifacts / "baseline.json",
        repository_artifacts / "status.json",
        repository_artifacts / "changed-files.json",
        repository_artifacts / "candidate.patch",
        repository_artifacts / "cleanup.json",
    ):
        assert path.is_file(), path
    invocation = json.loads((agent / "invocation.json").read_text(encoding="utf-8"))
    assert (
        invocation["stdin"]["artifact_reference"]
        == "attempts/attempt_001/agent/prompt.txt"
    )
    assert "Change target.txt exactly as requested" not in json.dumps(invocation)


def test_no_patch_is_coding_outcome_not_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _context_value, _repository_value = _run_scenario(
        tmp_path, monkeypatch, "no_patch"
    )
    assert result.status == "failed"
    assert result.patch is None
    assert result.error and result.error.code == ClaudeFailure.COMPLETED_NO_PATCH.value
    assert result.metadata["infrastructure_failure"] is False


@pytest.mark.parametrize(
    ("scenario", "expected_paths"),
    [
        ("untracked", {"new file ü.txt", "target.txt"}),
        ("rename_delete", {"delete_me.txt", "renamed ü.txt", "target.txt"}),
    ],
)
def test_git_truth_captures_untracked_renamed_and_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_paths: set[str],
) -> None:
    result, context, _repository_value = _run_scenario(tmp_path, monkeypatch, scenario)
    document = json.loads(
        (context.attempt_directory / "repository" / "changed-files.json").read_text(
            encoding="utf-8"
        )
    )
    assert result.status == "completed"
    assert expected_paths.issubset(set(document["changed_files"]))
    if scenario == "untracked":
        assert "new file ü.txt" in document["added_files"]
    else:
        assert "delete_me.txt" in document["deleted_files"]
        assert document["renamed_files"] == ["rename_me.txt -> renamed ü.txt"]


@pytest.mark.parametrize(
    ("scenario", "failure"),
    [
        ("malformed", ClaudeFailure.INVALID_JSON),
        ("missing_final", ClaudeFailure.MISSING_FINAL_RESULT),
        ("invalid_schema", ClaudeFailure.JSON_SCHEMA_FAILURE),
        ("model_unavailable", ClaudeFailure.MODEL_UNAVAILABLE),
        ("permission_denial", ClaudeFailure.PERMISSION_DENIED),
        ("tool_denial", ClaudeFailure.TOOL_DENIED),
        ("startup_failure", ClaudeFailure.AMBIENT_STARTUP_FAILURE),
        ("provider_auth_failure", ClaudeFailure.PROVIDER_AUTHENTICATION_FAILURE),
        ("rate_limit", ClaudeFailure.PROVIDER_RATE_LIMIT_OR_OVERLOAD),
    ],
)
def test_provider_stream_and_permission_failures_are_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    failure: ClaudeFailure,
) -> None:
    result, _context_value, _repository_value = _run_scenario(
        tmp_path, monkeypatch, scenario
    )
    assert result.status == "failed"
    assert result.error and result.error.code == failure.value
    assert result.metadata["infrastructure_failure"] is True


def test_nonzero_exit_preserves_partial_git_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, context, repository = _run_scenario(tmp_path, monkeypatch, "partial_crash")
    assert result.error and result.error.code == ClaudeFailure.PROCESS_CRASH.value
    assert result.patch and "claude change" in result.patch
    assert result.error.details["partial_patch_preserved"] is True
    assert (context.attempt_directory / "repository" / "candidate.patch").is_file()
    assert _source_snapshot(repository)[1] == ""


def test_timeout_preserves_partial_patch_and_cleans_process_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _context_value, _repository_value = _run_scenario(
        tmp_path, monkeypatch, "timeout_partial", timeout_seconds=1
    )
    assert result.error and result.error.code == ClaudeFailure.PROCESS_TIMEOUT.value
    assert result.patch and "claude change" in result.patch
    assert result.runner_telemetry["process"]["timed_out"] is True
    assert result.runner_telemetry["process"]["cleanup_status"] == "succeeded"


def test_controller_cancellation_cleans_descendant_and_preserves_partial_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    cancellation = threading.Event()
    context = _context(tmp_path, repository, cancellation_event=cancellation)
    monkeypatch.setenv("VILLANI_FAKE_CLAUDE_SCENARIO", "child_cancel")
    adapter = _adapter(_system(timeout_seconds=10))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(adapter.run, context)
        child_path = context.attempt_directory / "worktree" / "child.pid"
        deadline = time.monotonic() + 5
        while not child_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_path.is_file(), "fake Claude Code did not spawn its child"
        child_pid = int(child_path.read_text(encoding="ascii"))
        cancellation.set()
        result = future.result(timeout=10)
    deadline = time.monotonic() + 3
    while _process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert result.status == "cancelled"
    assert (
        result.error and result.error.code == ClaudeFailure.PROCESS_CANCELLATION.value
    )
    assert result.patch is not None
    assert not _process_alive(child_pid)


def test_large_prompt_and_non_ascii_path_use_stdin_and_preserve_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path, "source with spaces 雪")
    before = _source_snapshot(repository)
    task = "LARGE_PROMPT_SENTINEL\n" + ("large context ü\n" * 8_000)
    context = _context(tmp_path / "run root 雪", repository, task=task)
    monkeypatch.setenv("VILLANI_FAKE_CLAUDE_SCENARIO", "large_prompt")
    result = _adapter().run(context)
    assert result.status == "completed"
    assert " 雪" in result.worktree_path
    assert _source_snapshot(repository) == before
    invocation = json.loads(
        (context.attempt_directory / "agent" / "invocation.json").read_text(
            encoding="utf-8"
        )
    )
    assert invocation["stdin"]["size_bytes"] > 100_000
    assert "LARGE_PROMPT_SENTINEL" not in json.dumps(invocation)


def test_parallel_candidates_have_distinct_workspaces_processes_and_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("VILLANI_FAKE_CLAUDE_SCENARIO", "success")
    contexts = [
        _context(
            tmp_path / f"parallel-{index}",
            repository,
            attempt_id=f"attempt_{index:03d}",
        )
        for index in (1, 2)
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_adapter().run, contexts))
    assert all(result.status == "completed" for result in results)
    assert len({result.worktree_path for result in results}) == 2
    assert len({result.metadata["claude_code_session_id"] for result in results}) == 2
    assert _source_snapshot(repository)[1] == ""


def test_exact_identity_secrets_and_hidden_reasoning_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "claude-test-secret-value-7c91"
    monkeypatch.setenv("CLAUDE_TEST_SECRET", secret)
    monkeypatch.setenv("VILLANI_REGISTERED_SECRET_ENV_VARS", "CLAUDE_TEST_SECRET")
    monkeypatch.setenv("VILLANI_FAKE_ECHO_ENV_NAMES", "CLAUDE_TEST_SECRET")
    result, context, _repository_value = _run_scenario(tmp_path, monkeypatch, "success")
    provider = json.loads(
        (context.attempt_directory / "agent" / "provider.json").read_text(
            encoding="utf-8"
        )
    )
    assert provider["exact_version_output"] == "2.1.138 (Claude Code fixture)"
    assert provider["reported_model"] == "claude-sonnet-fixture"
    assert provider["session_id"].startswith("fake-claude-session-")
    assert provider["resolved_executable"] == str(Path(sys.executable).resolve())
    assert provider["executable_sha256"] == (
        "sha256:" + hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    )
    assert provider["billing_identity"] == "not_reported"
    assert result.status == "completed"
    forbidden = (
        secret.encode(),
        b"fixture hidden chain of thought must never persist",
        b"fixture-thinking-signature",
    )
    for path in context.attempt_directory.rglob("*"):
        if path.is_file():
            payload = path.read_bytes()
            assert all(value not in payload for value in forbidden), path


def test_public_cli_configures_doctors_and_binds_claude_coding_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "villani home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    backend_config = {
        "provider": "openai-compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "fixture-model",
        "roles": ["classification", "coding", "review", "selection"],
        "capability_score": 50,
        "enabled": True,
    }
    unified._write_config(
        unified._config_path(), {"backends": {"fixture": backend_config}}
    )
    runner = CliRunner()
    added = runner.invoke(
        unified.app,
        [
            "agents",
            "add",
            "claude-coder",
            "--driver",
            "claude_code",
            "--executable",
            sys.executable,
            "--model",
            "claude-sonnet-fixture",
            "--roles",
            "coding",
        ],
    )
    assert added.exit_code == 0, added.output
    configuration = yaml.safe_load(unified._config_path().read_text(encoding="utf-8"))
    configured = configuration["agent_systems"]["systems"]["claude-coder"]
    configured["provider_options"] = {
        "launcher_arguments": [str(FAKE_CLAUDE)],
        "max_turns": 8,
    }
    unified._write_config(unified._config_path(), configuration)
    doctor = runner.invoke(unified.app, ["agents", "doctor", "claude-coder", "--json"])
    assert doctor.exit_code == 0, doctor.output
    report = json.loads(doctor.stdout)["reports"][0]
    assert report["selectable"] is True
    assert {check["name"] for check in report["checks"]} >= {
        "version_and_capabilities",
        "authentication",
        "claude_doctor",
    }
    bound = runner.invoke(
        unified.app, ["profiles", "set-role", "hybrid", "coding", "claude-coder"]
    )
    assert bound.exit_code == 0, bound.output
    profile = runner.invoke(unified.app, ["profiles", "inspect", "hybrid", "--json"])
    assert profile.exit_code == 0, profile.output
    inspected = json.loads(profile.stdout)
    assert inspected["runnable"] is True
    assert inspected["bindings"]["bindings"]["coding"] == "claude-coder"
    assert inspected["bindings"]["bindings"]["classification"].startswith("api-")


def _codex_adapter() -> CodexCliAttemptAdapter:
    system = CliAgentSystemConfig(
        kind="cli_agent",
        id="codex-coder",
        enabled=True,
        driver="codex",
        executable=sys.executable,
        model="gpt-fixture-codex",
        roles={AgentRole.CODING},
        timeout_seconds=5,
        max_parallel=1,
        instruction_policy="native_project",
        permission_profile="workspace_write",
        environment_policy="inherit",
        provider_options={
            "launcher_arguments": [str(FAKE_CODEX)],
            "graceful_shutdown_seconds": 0.25,
        },
    )
    probe = CodexProbeResult(
        system_id=system.id,
        checked_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        configured_executable=system.executable,
        resolved_executable=str(Path(sys.executable).resolve()),
        exact_version_output="codex-cli 9.9.9-fixture",
        authentication_ready=True,
        authentication_method="chatgpt",
        capabilities={
            "exec": True,
            "jsonl_output": True,
            "model_selection": True,
            "workspace_selection": True,
            "sandbox_selection": True,
            "schema_output": True,
            "last_message_output": True,
            "ephemeral": True,
            "noninteractive_approval": True,
            "ignore_user_config": True,
            "ignore_project_rules": True,
        },
        ready=True,
    )
    return CodexCliAttemptAdapter(CodexCliDriver(system), probe=probe)


def test_codex_and_claude_candidate_contracts_are_provider_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("VILLANI_FAKE_CODEX_SCENARIO", "success")
    monkeypatch.setenv("VILLANI_FAKE_CLAUDE_SCENARIO", "success")
    codex_context = _context(tmp_path / "codex", repository)
    claude_context = _context(tmp_path / "claude", repository)
    codex = _codex_adapter().run(codex_context)
    claude = _adapter().run(claude_context)
    assert codex.status == claude.status == "completed"
    codex_candidate = json.loads(
        (codex_context.attempt_directory / "candidate" / "candidate.json").read_text(
            encoding="utf-8"
        )
    )
    claude_candidate = json.loads(
        (claude_context.attempt_directory / "candidate" / "candidate.json").read_text(
            encoding="utf-8"
        )
    )
    assert codex_candidate["schema_version"] == claude_candidate["schema_version"]
    assert set(codex_candidate) == set(claude_candidate)
    assert (
        codex.metadata["candidate_bundle_schema_version"]
        == (claude.metadata["candidate_bundle_schema_version"])
    )
    assert _source_snapshot(repository)[1] == ""


def test_claude_coder_completes_with_existing_verifier_and_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("VILLANI_FAKE_CLAUDE_SCENARIO", "success")
    verifier_calls: list[dict[str, Any]] = []

    def raw_verifier(**kwargs: Any) -> dict[str, Any]:
        verifier_calls.append(kwargs)
        requirements = kwargs["verification_context"]["requirements"]
        return {
            "result": 1,
            "verdict": "success",
            "confidence": 0.99,
            "recommendedAction": "accept",
            "reason": "The Git-derived diff contains the requested literal change.",
            "requirementResults": [
                {
                    "id": item["requirement_id"],
                    "requirement": item["description"],
                    "critical": item["critical"],
                    "status": "passed",
                    "evidence": ["diff-evidence"],
                    "risks": [],
                }
                for item in requirements
            ],
            "successEvidence": [
                {
                    "id": "diff-evidence",
                    "kind": "source_inspection",
                    "summary": "The canonical patch adds the requested line.",
                }
            ],
            "failureEvidence": [],
            "missingEvidence": [],
            "riskFlags": [],
            "criticalRequirementCoverageProven": True,
            "focusedProbeRequests": [],
        }

    controller = ClosedLoopController(
        classifier=FakeClassifier(),
        policy_engine=FakePolicyEngine(
            [
                policy("attempt", backend_option=backend("claude-coder")),
                policy("select"),
            ]
        ),
        attempt_runner=_adapter(),
        verifier=VillaniVerifierAdapter(raw_verifier=raw_verifier, no_llm=False),
        selector=EvidenceSelectorAdapter(),
        materializer=PatchMaterializerAdapter(),
        now=FixedNow(),
        monotonic=FakeMonotonic(),
        id_factory=StableIds(),
    )
    request = ClosedLoopRunRequest(
        task="Append the literal line claude change to target.txt.",
        repository_path=repository,
        success_criteria="The canonical Git patch adds the literal line claude change.",
        runs_root=tmp_path / "runs",
        max_attempts=1,
        policy_configuration={
            "version": "claude_m4_fixture",
            "repository_validation_commands": [],
        },
    )
    result = controller.run(request)
    assert result.terminal_state == "COMPLETED"
    assert result.selected_attempt_id == "attempt_001"
    assert len(verifier_calls) == 1
    assert (repository / "target.txt").read_text(encoding="utf-8") == (
        "baseline\nclaude change\n"
    )
    attempt = json.loads(
        (result.run_directory / "attempts" / "attempt_001" / "attempt.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt["runner_name"] == "claude_code_cli:claude-coder"
    assert not (result.run_directory / "attempts" / "attempt_001" / "worktree").exists()


@pytest.mark.integration
def test_real_claude_code_smoke_is_explicitly_opt_in(tmp_path: Path) -> None:
    if os.environ.get("VILLANI_ENABLE_REAL_CLAUDE_TESTS") != "1":
        pytest.skip(
            "set VILLANI_ENABLE_REAL_CLAUDE_TESTS=1 to enable the paid/external real Claude Code smoke test"
        )
    executable = shutil.which("claude")
    if executable is None:
        pytest.skip(
            "real Claude Code smoke prerequisite missing: `claude` executable was not found"
        )
    model = os.environ.get("VILLANI_REAL_CLAUDE_MODEL")
    if not model:
        pytest.skip(
            "set VILLANI_REAL_CLAUDE_MODEL to an installed Claude Code model string"
        )
    system = _system(
        executable=executable,
        timeout_seconds=120,
        provider_options={
            "launcher_arguments": [],
            "graceful_shutdown_seconds": 3,
            "max_turns": 8,
        },
    ).model_copy(update={"model": model})
    driver = ClaudeCodeCliDriver(system)
    probe = driver.probe()
    if not probe.authentication_ready:
        pytest.skip(
            "real Claude Code smoke prerequisite missing: `claude auth status` is not ready"
        )
    if not probe.ready:
        pytest.skip(
            "real Claude Code smoke prerequisite missing: installed CLI lacks required safe print/stream/schema capabilities"
        )
    repository = _repository(tmp_path, "real-claude-disposable")
    context = replace(
        _context(tmp_path, repository),
        task="Append exactly one line containing VILLANI_CLAUDE_SMOKE to target.txt.",
        success_criteria="target.txt has a non-empty Git diff containing VILLANI_CLAUDE_SMOKE.",
    )
    result = ClaudeCodeCliAttemptAdapter(driver, probe=probe).run(context)
    assert result.status == "completed"
    assert result.patch and "VILLANI_CLAUDE_SMOKE" in result.patch
    assert _source_snapshot(repository)[1] == ""
