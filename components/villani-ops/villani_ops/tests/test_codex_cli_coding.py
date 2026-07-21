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
from villani_ops.closed_loop.agent_systems.factories import (
    RoleFactoryDependencies,
    build_attempt_runner,
)
from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    CliAgentSystemConfig,
)
from villani_ops.closed_loop.agent_systems.role_registry import RoleSystemRegistry
from villani_ops.closed_loop.codex_cli.attempt import CodexCliAttemptAdapter
from villani_ops.closed_loop.codex_cli.driver import CodexCliDriver
from villani_ops.closed_loop.codex_cli.events import (
    CodexEventParseError,
    parse_codex_events,
)
from villani_ops.closed_loop.codex_cli.models import (
    CodexFailure,
    CodexProbeResult,
)
from villani_ops.closed_loop.codex_cli.prompt import build_codex_coding_prompt
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
FAKE_CODEX = HERE / "fixtures" / "codex_cli" / "fake_codex.py"
EVENT_FIXTURES = HERE / "fixtures" / "codex_cli"


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
    _git(repository, "config", "user.email", "codex-fixture@example.invalid")
    _git(repository, "config", "user.name", "Codex Fixture")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "baseline")
    return repository


def _system(
    *,
    executable: str | None = None,
    timeout_seconds: int = 5,
    provider_options: dict[str, Any] | None = None,
    roles: set[AgentRole] | None = None,
) -> CliAgentSystemConfig:
    options: dict[str, Any] = {
        "launcher_arguments": [str(FAKE_CODEX)],
        "graceful_shutdown_seconds": 0.25,
    }
    options.update(provider_options or {})
    return CliAgentSystemConfig(
        kind="cli_agent",
        id="codex-coder",
        enabled=True,
        driver="codex",
        executable=executable or sys.executable,
        model="gpt-fixture-codex",
        roles=roles or {AgentRole.CODING},
        timeout_seconds=timeout_seconds,
        max_parallel=2,
        instruction_policy="native_project",
        permission_profile="workspace_write",
        environment_policy="inherit",
        provider_options=options,
    )


def _ready_probe(system: CliAgentSystemConfig) -> CodexProbeResult:
    return CodexProbeResult(
        system_id=system.id,
        checked_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        configured_executable=system.executable,
        resolved_executable=str(Path(system.executable).resolve()),
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


def _context(
    tmp_path: Path,
    repository: Path,
    *,
    attempt_id: str = "attempt_001",
    cancellation_event: threading.Event | None = None,
) -> AttemptContext:
    run_directory = tmp_path / "run"
    attempt_directory = run_directory / "attempts" / attempt_id
    attempt_directory.mkdir(parents=True, exist_ok=True)
    return AttemptContext(
        run_id="run_codex_fixture",
        trace_id="trace_codex_fixture",
        task_id="task_codex_fixture",
        attempt_id=attempt_id,
        ordinal=1,
        task="Change target.txt exactly as requested.\nKeep this task verbatim.",
        repository_path=str(repository),
        success_criteria="A non-empty isolated Git patch exists.\nTests are reported.",
        requires_file_changes=True,
        backend_name="codex-coder",
        model="gpt-fixture-codex",
        policy_configuration={"isolation": {}},
        run_directory=run_directory,
        attempt_directory=attempt_directory,
        baseline_sha256="b" * 64,
        cancellation_event=cancellation_event,
    )


def _adapter(system: CliAgentSystemConfig | None = None) -> CodexCliAttemptAdapter:
    configured = system or _system()
    driver = CodexCliDriver(configured)
    return CodexCliAttemptAdapter(driver, probe=_ready_probe(configured))


def _run_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    *,
    timeout_seconds: int = 5,
) -> tuple[Any, AttemptContext, Path]:
    repository = _repository(tmp_path)
    context = _context(tmp_path, repository)
    monkeypatch.setenv("VILLANI_FAKE_CODEX_SCENARIO", scenario)
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

        handle = ctypes.WinDLL("kernel32", use_last_error=True).OpenProcess(
            0x1000, False, pid
        )
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        try:
            return (
                bool(
                    ctypes.WinDLL("kernel32", use_last_error=True).GetExitCodeProcess(
                        handle, ctypes.byref(exit_code)
                    )
                )
                and exit_code.value == 259
            )
        finally:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


def test_official_format_fixture_maps_command_and_usage_events() -> None:
    parsed = parse_codex_events(
        EVENT_FIXTURES / "events-success.jsonl",
        started_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        run_id="run_fixture",
        attempt_id="attempt_fixture",
        worktree_path="worktree",
        baseline_sha256="a" * 64,
    )

    names = [event.event_type for event in parsed.runtime_events]
    assert parsed.thread_id == "thread_fixture_1"
    assert parsed.input_tokens == 24763
    assert parsed.output_tokens == 122
    assert names == [
        "session_started",
        "turn_started",
        "command_started",
        "command_completed",
        "file_write",
        "agent_message",
        "usage_update",
        "turn_completed",
    ]
    command = next(
        event
        for event in parsed.runtime_events
        if event.event_type == "command_completed"
    )
    assert command.payload["command"] == "python -m pytest -q"
    assert command.payload["exit_code"] == 0


def test_unknown_event_is_preserved_as_namespaced_raw_event() -> None:
    parsed = parse_codex_events(
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
        if event.event_type == "codex.raw_event"
    )
    assert unknown.payload["namespace"] == "codex.raw"
    assert unknown.payload["event"]["provider_extension"] == {"answer": 42}


def test_malformed_event_fixture_fails_with_line_and_column(tmp_path: Path) -> None:
    fixture = tmp_path / "malformed.jsonl"
    fixture.write_text('{"type":"turn.started"}\n{broken\n', encoding="utf-8")

    with pytest.raises(CodexEventParseError, match=r"line 2 is malformed at column 2"):
        parse_codex_events(
            fixture,
            started_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
            run_id="run_fixture",
            attempt_id="attempt_fixture",
            worktree_path="worktree",
            baseline_sha256=None,
        )


def test_probe_records_exact_version_auth_and_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VILLANI_FAKE_CODEX_AUTH", raising=False)
    monkeypatch.delenv("VILLANI_FAKE_CODEX_UNSUPPORTED", raising=False)

    probe = CodexCliDriver(_system()).probe()

    assert probe.ready is True
    assert probe.exact_version_output == "codex-cli 9.9.9-fixture"
    assert probe.authentication_method == "chatgpt"
    assert all(probe.capabilities.values())
    assert probe.resolved_executable == str(Path(sys.executable).resolve())


@pytest.mark.parametrize(
    ("environment_name", "failure"),
    [
        ("VILLANI_FAKE_CODEX_AUTH", CodexFailure.NOT_AUTHENTICATED),
        ("VILLANI_FAKE_CODEX_UNSUPPORTED", CodexFailure.UNSUPPORTED_REQUIRED_FLAG),
    ],
)
def test_probe_reports_actionable_readiness_failures(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    failure: CodexFailure,
) -> None:
    monkeypatch.setenv(
        environment_name, "missing" if "AUTH" in environment_name else "1"
    )

    probe = CodexCliDriver(_system()).probe()

    assert probe.ready is False
    assert failure in probe.failures
    assert probe.messages


def test_probe_reports_missing_executable_without_spawning(tmp_path: Path) -> None:
    missing = tmp_path / "missing codex executable"
    probe = CodexCliDriver(_system(executable=str(missing))).probe()

    assert probe.ready is False
    assert probe.failures == [CodexFailure.NOT_INSTALLED]
    assert probe.resolved_executable is None


def test_driver_constructs_shell_free_safe_noninteractive_invocation(
    tmp_path: Path,
) -> None:
    system = _system()
    driver = CodexCliDriver(system)
    worktree = tmp_path / "work tree ü"
    agent = tmp_path / "agent"
    worktree.mkdir()
    prompt = build_codex_coding_prompt(
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
        output_schema_path=agent / "schema.json",
        final_output_path=agent / "final-output.json",
        run_id="run_fixture",
        attempt_id="attempt_001",
        baseline_sha256="b" * 64,
    )

    command = driver.safe_command(invocation)
    assert invocation.cwd == worktree.resolve()
    assert invocation.stdin_bytes == prompt.bytes
    assert command[0] == str(Path(sys.executable).resolve())
    assert command[1] == str(FAKE_CODEX)
    assert command[2:5] == ("exec", "--ephemeral", "--json")
    assert command[-1] == "-"
    assert command[command.index("--model") + 1] == "gpt-fixture-codex"
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--cd") + 1] == str(worktree.resolve())
    assert command[command.index("--ask-for-approval") + 1] == "never"
    forbidden = {
        "--yolo",
        "--dangerously-bypass-approvals-and-sandbox",
        "danger-full-access",
        "--full-auto",
        "resume",
    }
    assert forbidden.isdisjoint(command)


def test_villani_controlled_instruction_policy_uses_supported_suppression_flags(
    tmp_path: Path,
) -> None:
    system = _system().model_copy(update={"instruction_policy": "villani_controlled"})
    driver = CodexCliDriver(system)
    worktree = tmp_path / "controlled-worktree"
    worktree.mkdir()
    prompt = build_codex_coding_prompt(
        task="task",
        success_criteria="criteria",
        attempt_id="attempt_001",
        worktree=worktree,
        instruction_policy="villani_controlled",
    )

    invocation = driver.build_invocation(
        probe=_ready_probe(system),
        worktree=worktree,
        agent_directory=tmp_path / "agent",
        prompt_bytes=prompt.bytes,
        prompt_reference="agent/prompt.txt",
        prompt_sha256=prompt.sha256,
        output_schema_path=tmp_path / "agent" / "schema.json",
        final_output_path=tmp_path / "agent" / "final-output.json",
        run_id="run_fixture",
        attempt_id="attempt_001",
        baseline_sha256=None,
    )

    assert "--ignore-user-config" in invocation.arguments
    assert "--ignore-rules" in invocation.arguments
    assert (
        invocation.role_workspace_identity["instruction_policy"] == "villani_controlled"
    )


def test_successful_attempt_records_git_patch_and_complete_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, context, repository = _run_scenario(tmp_path, monkeypatch, "success")

    assert result.status == "completed"
    assert result.error is None
    assert result.patch and "codex change" in result.patch
    assert result.metadata["changed_files"] == ["target.txt"]
    assert result.metadata["has_non_empty_patch"] is True
    assert _source_snapshot(repository)[1] == ""
    assert (repository / "target.txt").read_text(encoding="utf-8") == "baseline\n"
    agent = context.attempt_directory / "agent"
    repository_artifacts = context.attempt_directory / "repository"
    for path in (
        agent / "provider.json",
        agent / "invocation.json",
        agent / "prompt.txt",
        agent / "prompt.digest",
        agent / "stdout.log",
        agent / "stderr.log",
        agent / "codex-events.jsonl",
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
    assert (
        invocation["stdin"]["sha256"]
        == (agent / "prompt.digest").read_text(encoding="utf-8").strip()
    )
    assert "Change target.txt exactly as requested" not in json.dumps(invocation)


def test_no_patch_is_non_infrastructure_coding_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _context_value, _repository_value = _run_scenario(
        tmp_path, monkeypatch, "no_patch"
    )

    assert result.status == "failed"
    assert result.patch is None
    assert result.error and result.error.code == CodexFailure.COMPLETED_NO_PATCH.value
    assert result.metadata["infrastructure_failure"] is False
    assert result.error.details["infrastructure_failure"] is False


@pytest.mark.parametrize(
    ("scenario", "expected_paths"),
    [
        ("untracked", {"new file ü.txt", "target.txt"}),
        ("rename_delete", {"delete_me.txt", "renamed ü.txt", "target.txt"}),
    ],
)
def test_git_truth_captures_untracked_renamed_and_deleted_files(
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
    assert document["has_non_empty_patch"] is True
    if scenario == "untracked":
        assert "new file ü.txt" in document["added_files"]
    else:
        assert "delete_me.txt" in document["deleted_files"]
        assert document["renamed_files"] == ["rename_me.txt -> renamed ü.txt"]


@pytest.mark.parametrize(
    ("scenario", "failure"),
    [
        ("malformed", CodexFailure.MALFORMED_JSONL),
        ("missing_final", CodexFailure.MISSING_FINAL_STRUCTURED_OUTPUT),
        ("invalid_final", CodexFailure.STRUCTURED_OUTPUT_SCHEMA_FAILURE),
        ("model_unavailable", CodexFailure.MODEL_UNAVAILABLE),
        ("permission_failure", CodexFailure.PERMISSION_SANDBOX_FAILURE),
        ("provider_auth_failure", CodexFailure.PROVIDER_AUTHENTICATION_FAILURE),
        ("rate_limit", CodexFailure.PROVIDER_RATE_LIMIT_OR_OVERLOAD),
    ],
)
def test_provider_and_output_failures_are_distinguished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    failure: CodexFailure,
) -> None:
    result, _context_value, _repository_value = _run_scenario(
        tmp_path, monkeypatch, scenario
    )

    assert result.status == "failed"
    assert result.error and result.error.code == failure.value
    assert result.metadata["failure_category"] == failure.value
    assert result.metadata["infrastructure_failure"] is True


def test_forbidden_villani_path_is_rejected_and_excluded_from_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, context, repository = _run_scenario(tmp_path, monkeypatch, "path_violation")

    assert result.status == "failed"
    assert result.error and result.error.code == CodexFailure.PATH_VIOLATION.value
    assert result.metadata["path_violation"] is True
    assert ".villani/forbidden.txt" in result.metadata["forbidden_paths_touched"]
    assert result.patch and ".villani/forbidden.txt" not in result.patch
    assert _source_snapshot(repository)[1] == ""
    status = json.loads(
        (context.attempt_directory / "repository" / "status.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["path_violation"] is True
    assert status["forbidden_paths_touched"] == [".villani/forbidden.txt"]


def test_nonzero_exit_preserves_partial_git_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, context, repository = _run_scenario(tmp_path, monkeypatch, "partial_crash")

    assert result.error and result.error.code == CodexFailure.PROCESS_CRASH.value
    assert result.patch and "codex change" in result.patch
    assert result.error.details["partial_patch_preserved"] is True
    assert (context.attempt_directory / "repository" / "candidate.patch").read_text(
        encoding="utf-8"
    )
    assert _source_snapshot(repository)[1] == ""


def test_timeout_preserves_partial_patch_and_terminates_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, _context_value, _repository_value = _run_scenario(
        tmp_path, monkeypatch, "timeout_partial", timeout_seconds=1
    )

    assert result.error and result.error.code == CodexFailure.PROCESS_TIMEOUT.value
    assert result.patch and "codex change" in result.patch
    assert result.runner_telemetry["process"]["timed_out"] is True
    assert result.runner_telemetry["process"]["cleanup_status"] == "succeeded"


def test_controller_cancellation_is_distinct_and_cleans_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    cancellation = threading.Event()
    context = _context(tmp_path, repository, cancellation_event=cancellation)
    monkeypatch.setenv("VILLANI_FAKE_CODEX_SCENARIO", "child_cancel")
    adapter = _adapter(_system(timeout_seconds=10))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(adapter.run, context)
        child_path = context.attempt_directory / "worktree" / "child.pid"
        deadline = time.monotonic() + 5
        while not child_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_path.is_file(), "fake Codex did not spawn its child"
        child_pid = int(child_path.read_text(encoding="ascii"))
        cancellation.set()
        result = future.result(timeout=10)

    deadline = time.monotonic() + 3
    while _process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert result.status == "cancelled"
    assert result.error and result.error.code == CodexFailure.PROCESS_CANCELLATION.value
    assert result.patch is not None
    assert result.runner_telemetry["process"]["cleanup_status"] == "succeeded"
    assert not _process_alive(child_pid)


def test_spaces_non_ascii_isolation_and_target_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path, "source with spaces 雪")
    before = _source_snapshot(repository)
    context = _context(tmp_path / "run root 雪", repository)
    monkeypatch.setenv("VILLANI_FAKE_CODEX_SCENARIO", "success")

    result = _adapter().run(context)

    assert result.status == "completed"
    assert " 雪" in result.worktree_path
    assert _source_snapshot(repository) == before
    invocation = json.loads(
        (context.attempt_directory / "agent" / "invocation.json").read_text(
            encoding="utf-8"
        )
    )
    assert invocation["cwd"] == str(Path(result.worktree_path).resolve())


def test_parallel_candidates_use_separate_processes_workspaces_and_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("VILLANI_FAKE_CODEX_SCENARIO", "success")
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
    assert len({result.metadata["codex_thread_id"] for result in results}) == 2
    assert _source_snapshot(repository)[1] == ""


def test_provider_identity_exact_and_secret_values_absent_from_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "codex-test-secret-value-4f89d8"
    monkeypatch.setenv("CODEX_TEST_SECRET", secret)
    monkeypatch.setenv("VILLANI_REGISTERED_SECRET_ENV_VARS", "CODEX_TEST_SECRET")
    monkeypatch.setenv("VILLANI_FAKE_ECHO_ENV_NAMES", "CODEX_TEST_SECRET")
    result, context, _repository_value = _run_scenario(tmp_path, monkeypatch, "success")

    provider = json.loads(
        (context.attempt_directory / "agent" / "provider.json").read_text(
            encoding="utf-8"
        )
    )
    assert provider["exact_version_output"] == "codex-cli 9.9.9-fixture"
    assert provider["model"] == "gpt-fixture-codex"
    assert provider["resolved_executable"] == str(Path(sys.executable).resolve())
    assert (
        provider["executable_sha256"]
        == "sha256:" + hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    )
    assert provider["billing_identity"] == "not_reported"
    assert result.status == "completed"
    for path in context.attempt_directory.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def test_hybrid_profile_still_constructs_existing_api_coder_when_selected() -> None:
    api_runner = object()
    configuration = {
        "agent_systems": {
            "systems": {
                "api-all": {
                    "kind": "api",
                    "id": "api-all",
                    "enabled": True,
                    "provider": "fixture",
                    "model": "fixture",
                    "roles": [role.value for role in AgentRole],
                    "existing_backend_reference": "fixture",
                    "timeout_seconds": 60,
                    "max_parallel": 1,
                    "metadata": {},
                },
                "codex-coder": _system().model_dump(mode="json"),
            }
        },
        "execution_profiles": {"api": {role.value: "api-all" for role in AgentRole}},
    }
    registry = RoleSystemRegistry(configuration, {})
    dependencies = RoleFactoryDependencies(
        api_attempt_runners={"fixture": api_runner}  # type: ignore[dict-item]
    )

    resolved = build_attempt_runner(
        registry.resolve_profile("api"), registry, dependencies
    )

    assert resolved is api_runner


def test_public_cli_configures_doctors_and_binds_codex_coding_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "villani home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    monkeypatch.delenv("VILLANI_FAKE_CODEX_AUTH", raising=False)
    monkeypatch.delenv("VILLANI_FAKE_CODEX_UNSUPPORTED", raising=False)
    backend = {
        "provider": "openai-compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "fixture-model",
        "roles": ["classification", "coding", "review", "selection"],
        "capability_score": 50,
        "enabled": True,
    }
    unified._write_config(unified._config_path(), {"backends": {"fixture": backend}})
    runner = CliRunner()

    added = runner.invoke(
        unified.app,
        [
            "agents",
            "add",
            "codex-coder",
            "--driver",
            "codex",
            "--executable",
            sys.executable,
            "--model",
            "gpt-fixture-codex",
            "--roles",
            "coding",
        ],
    )
    assert added.exit_code == 0, added.output

    configuration = yaml.safe_load(unified._config_path().read_text(encoding="utf-8"))
    configured = configuration["agent_systems"]["systems"]["codex-coder"]
    configured["provider_options"] = {"launcher_arguments": [str(FAKE_CODEX)]}
    unified._write_config(unified._config_path(), configuration)

    doctor = runner.invoke(unified.app, ["agents", "doctor", "codex-coder", "--json"])
    assert doctor.exit_code == 0, doctor.output
    report = json.loads(doctor.stdout)["reports"][0]
    assert report["selectable"] is True
    version_check = next(
        check
        for check in report["checks"]
        if check["name"] == "version_and_capabilities"
    )
    assert (
        version_check["evidence"]["exact_version_output"] == "codex-cli 9.9.9-fixture"
    )

    bound = runner.invoke(
        unified.app, ["profiles", "set-role", "hybrid", "coding", "codex-coder"]
    )
    assert bound.exit_code == 0, bound.output
    profile = runner.invoke(unified.app, ["profiles", "inspect", "hybrid", "--json"])
    assert profile.exit_code == 0, profile.output
    inspected = json.loads(profile.stdout)
    assert inspected["runnable"] is True
    assert inspected["bindings"]["bindings"]["coding"] == "codex-coder"
    assert inspected["bindings"]["bindings"]["classification"].startswith("api-")


def test_controller_source_has_no_codex_or_claude_driver_imports() -> None:
    source = (HERE.parent / "closed_loop" / "controller.py").read_text(encoding="utf-8")
    assert "codex_cli" not in source
    assert "claude" not in source.casefold()


def test_codex_coder_completes_with_existing_verifier_and_delivery_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("VILLANI_FAKE_CODEX_SCENARIO", "success")
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
                    "summary": "The canonical patch adds the requested line to target.txt.",
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
                policy("attempt", backend_option=backend("codex-coder")),
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
        task="Append the literal line codex change to target.txt.",
        repository_path=repository,
        success_criteria=(
            "The canonical Git patch for target.txt adds the literal line codex change."
        ),
        runs_root=tmp_path / "runs",
        max_attempts=1,
        policy_configuration={
            "version": "codex_m3_fixture",
            "repository_validation_commands": [],
        },
    )

    result = controller.run(request)

    assert result.terminal_state == "COMPLETED"
    assert result.selected_attempt_id == "attempt_001"
    assert len(verifier_calls) == 1
    assert (repository / "target.txt").read_text(encoding="utf-8") == (
        "baseline\ncodex change\n"
    )
    attempt = json.loads(
        (result.run_directory / "attempts" / "attempt_001" / "attempt.json").read_text(
            encoding="utf-8"
        )
    )
    verification = json.loads(
        (result.run_directory / "verification" / "attempt_001.json").read_text(
            encoding="utf-8"
        )
    )
    assert attempt["runner_name"] == "codex_cli:codex-coder"
    assert attempt["metadata"]["candidate_quality_report"]["status"] == "eligible"
    assert verification["verifier"] == "villani_ops_verifier_pipeline"
    assert verification["acceptance_eligible"] is True
    assert not (result.run_directory / "attempts" / "attempt_001" / "worktree").exists()


@pytest.mark.integration
def test_real_codex_coding_smoke_is_explicitly_opt_in(tmp_path: Path) -> None:
    if os.environ.get("VILLANI_ENABLE_REAL_CODEX_TESTS") != "1":
        pytest.skip(
            "set VILLANI_ENABLE_REAL_CODEX_TESTS=1 to enable the paid/external real Codex smoke test"
        )
    executable = shutil.which("codex")
    if executable is None:
        pytest.skip(
            "real Codex smoke prerequisite missing: `codex` executable was not found"
        )
    system = _system(
        executable=executable,
        timeout_seconds=120,
        provider_options={"launcher_arguments": [], "graceful_shutdown_seconds": 3},
    )
    driver = CodexCliDriver(system)
    probe = driver.probe()
    if not probe.authentication_ready:
        pytest.skip(
            "real Codex smoke prerequisite missing: `codex login status` is not ready"
        )
    if not probe.ready:
        pytest.skip(
            "real Codex smoke prerequisite missing: installed Codex lacks required safe exec capabilities"
        )
    repository = _repository(tmp_path, "real-codex-disposable")
    context = _context(tmp_path, repository)
    context = replace(
        context,
        task="Append exactly one line containing VILLANI_CODEX_SMOKE to target.txt.",
        success_criteria=(
            "target.txt has a non-empty Git diff containing VILLANI_CODEX_SMOKE."
        ),
    )

    result = CodexCliAttemptAdapter(driver, probe=probe).run(context)

    assert result.status == "completed"
    assert result.patch and "VILLANI_CODEX_SMOKE" in result.patch
    assert _source_snapshot(repository)[1] == ""
