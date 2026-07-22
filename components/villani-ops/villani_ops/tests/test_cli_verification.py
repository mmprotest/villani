from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.adapters import VillaniVerifierAdapter
from villani_ops.closed_loop.adapters.git_isolation import GitIsolationAdapter
from villani_ops.closed_loop.agent_systems.factories import (
    RoleFactoryDependencies,
    build_verifier,
)
from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    ApiAgentSystemConfig,
    CliAgentSystemConfig,
    RoleBindings,
)
from villani_ops.closed_loop.claude_code_cli.driver import ClaudeCodeCliDriver
from villani_ops.closed_loop.claude_code_cli.models import (
    ClaudeFailure,
    ClaudeProbeResult,
)
from villani_ops.closed_loop.cli_verification.adapter import CliVerifierAdapter
from villani_ops.closed_loop.cli_verification.models import (
    CliVerifierFailure,
    normalize_cli_verifier_result,
)
from villani_ops.closed_loop.codex_cli.driver import CodexCliDriver
from villani_ops.closed_loop.codex_cli.models import CodexFailure, CodexProbeResult
from villani_ops.closed_loop.interfaces import AttemptContext, AttemptResult


HERE = Path(__file__).resolve().parent
FAKE_CODEX = HERE / "fixtures" / "cli_verifier" / "fake_codex_verifier.py"
FAKE_CLAUDE = HERE / "fixtures" / "cli_verifier" / "fake_claude_verifier.py"
NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        capture_output=True,
        check=True,
    )


def _repository(root: Path) -> Path:
    repository = root / "target-repository"
    repository.mkdir(parents=True)
    (repository / "target.txt").write_text("baseline\n", encoding="utf-8")
    (repository / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "config", "user.email", "verifier@example.invalid")
    _git(repository, "config", "user.name", "Verifier Fixture")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "baseline")
    return repository


def _system(
    driver: str,
    *,
    timeout_seconds: int = 5,
    scenario: str | None = None,
) -> CliAgentSystemConfig:
    fixture = FAKE_CODEX if driver == "codex" else FAKE_CLAUDE
    configured_driver = "codex" if driver == "codex" else "claude_code"
    return CliAgentSystemConfig(
        kind="cli_agent",
        id=f"{driver}-verifier",
        enabled=True,
        driver=configured_driver,  # type: ignore[arg-type]
        executable=sys.executable,
        model=f"{driver}-verifier-model",
        roles={AgentRole.VERIFICATION},
        timeout_seconds=timeout_seconds,
        max_parallel=4,
        instruction_policy="villani_controlled",
        permission_profile="read_only",
        environment_policy="minimal",
        provider_options={
            "launcher_arguments": [
                str(fixture),
                *(["--fixture-scenario", scenario] if scenario is not None else []),
            ],
            "graceful_shutdown_seconds": 0.1,
            "max_turns": 4,
        },
    )


def _ready_probe(
    system: CliAgentSystemConfig,
) -> CodexProbeResult | ClaudeProbeResult:
    executable = str(Path(sys.executable).resolve())
    if system.driver == "codex":
        return CodexProbeResult(
            system_id=system.id,
            checked_at=NOW,
            configured_executable=system.executable,
            resolved_executable=executable,
            exact_version_output="codex-cli 9.9.9-verifier-fixture",
            authentication_ready=True,
            authentication_method="chatgpt",
            capabilities={
                "exec": True,
                "jsonl_output": True,
                "model_selection": True,
                "workspace_selection": True,
                "sandbox_selection": True,
                "read_only_sandbox": True,
                "schema_output": True,
                "last_message_output": True,
                "ephemeral": True,
                "noninteractive_approval": True,
                "ignore_user_config": True,
                "ignore_project_rules": True,
                "strict_config": True,
                "config_override": True,
                "scoped_permission_profiles": True,
            },
            ready=True,
        )
    return ClaudeProbeResult(
        system_id=system.id,
        checked_at=NOW,
        configured_executable=system.executable,
        resolved_executable=executable,
        exact_version_output="2.1.138 (Claude Code verifier fixture)",
        parsed_version="2.1.138",
        authentication_ready=True,
        authentication_method="claude_ai",
        doctor_ready=True,
        capabilities={
            "print_mode": True,
            "stream_json": True,
            "structured_output": True,
            "no_session_persistence": True,
            "model_selection": True,
            "permission_mode": True,
            "read_only_permission_mode": True,
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
        },
        resolved_flags={"print": "-p", "allowed_tools": "--allowedTools"},
        ready=True,
    )


def _adapter(
    driver_name: str,
    *,
    timeout_seconds: int = 5,
    scenario: str | None = None,
    probe: CodexProbeResult | ClaudeProbeResult | None = None,
) -> CliVerifierAdapter:
    system = _system(
        driver_name,
        timeout_seconds=timeout_seconds,
        scenario=scenario,
    )
    driver = (
        CodexCliDriver(system)
        if driver_name == "codex"
        else ClaudeCodeCliDriver(system)
    )
    return CliVerifierAdapter(driver, probe=probe or _ready_probe(system))


@dataclass(frozen=True, slots=True)
class CandidateCase:
    context: AttemptContext
    result: AttemptResult
    repository: Path
    transcript_marker: str
    provider_marker: str
    competitor_marker: str


def _candidate(
    root: Path,
    *,
    coder: str = "api",
    ordinal: int = 1,
    cancellation_event: threading.Event | None = None,
) -> CandidateCase:
    repository = _repository(root)
    attempt_id = f"attempt_{ordinal:03d}"
    run_directory = root / "run"
    attempt_directory = run_directory / "attempts" / attempt_id
    attempt_directory.mkdir(parents=True)
    context = AttemptContext(
        run_id=f"run_verifier_{ordinal}",
        trace_id=f"trace_verifier_{ordinal}",
        task_id=f"task_verifier_{ordinal}",
        attempt_id=attempt_id,
        ordinal=ordinal,
        task="Update target.txt with the verified-change marker.",
        repository_path=str(repository),
        success_criteria="The verified-change marker must exist in target.txt.",
        requires_file_changes=True,
        backend_name=f"{coder}-coder",
        model=f"{coder}-coder-model-secret",
        policy_configuration={"isolation": {}},
        run_directory=run_directory,
        attempt_directory=attempt_directory,
        baseline_sha256="b" * 64,
        cancellation_event=cancellation_event,
    )
    isolation = GitIsolationAdapter()
    isolated = isolation.create(context)
    (isolated.copied.worktree_path / "target.txt").write_text(
        "baseline\nverified-change\n", encoding="utf-8"
    )
    capture = isolation.capture(isolated)
    patch = isolated.patch_path.read_text(encoding="utf-8")

    transcript_marker = f"CODER_TRANSCRIPT_MUST_NOT_LEAK_{ordinal}"
    provider_marker = f"CODER_PROVIDER_IDENTITY_MUST_NOT_LEAK_{ordinal}"
    competitor_marker = f"COMPETING_CANDIDATE_MUST_NOT_LEAK_{ordinal}"
    transcript = attempt_directory / "coder-transcript.jsonl"
    transcript.write_text(transcript_marker + "\n", encoding="utf-8")
    process_result = attempt_directory / "coder-process-result.json"
    process_result.write_text(
        json.dumps({"pid": 2_000_000_000 + ordinal}), encoding="utf-8"
    )
    runner_name = {
        "api": "api:coder",
        "codex": "codex_cli:coder",
        "claude": "claude_code_cli:coder",
    }[coder]
    metadata: dict[str, Any] = {
        "worktree": isolated.metadata,
        "changed_files": list(capture.changed_files),
        "candidate_patch_path": str(isolated.patch_path),
        "candidate_quality_report": {
            "schema_version": "villani.candidate_patch_quality.v1",
            "candidate_id": f"ranked-candidate-{ordinal}",
            "status": "eligible",
            "tracked_files_changed": list(capture.changed_files),
            "relevant_files_changed": list(capture.changed_files),
            "untracked_files": [],
            "ignored_files": [],
            "villani_owned_files": [],
            "generated_files": [],
            "semantic_lines_added": 1,
            "semantic_lines_removed": 0,
            "line_ending_only_lines": 0,
            "whitespace_only_lines": 0,
            "file_mode_only_changes": [],
            "bulk_rewrite_files": [],
            "relevant_diff_ratio": 1.0,
            "reason_codes": [],
            "provider": provider_marker,
            "rank": ordinal,
            "cost": 123.45,
        },
        "process_result_path": str(process_result),
        "debug_trace_path": str(transcript),
        "provider_identity": provider_marker,
        "candidate_rank": ordinal,
        "competing_candidates": [competitor_marker],
        "selector_output": "must-not-leak",
    }
    if coder == "codex":
        metadata["codex_thread_id"] = f"coder-codex-session-{ordinal}"
    elif coder == "claude":
        metadata["claude_code_session_id"] = f"coder-claude-session-{ordinal}"
    result = AttemptResult(
        runner_name=runner_name,
        status="completed",
        worktree_path=str(isolated.copied.worktree_path),
        patch=patch,
        exit_code=0,
        model=f"{coder}-coder-model-secret",
        cost_usd=123.45,
        cost_accounting_status="complete",
        metadata=metadata,
    )
    return CandidateCase(
        context=context,
        result=result,
        repository=repository,
        transcript_marker=transcript_marker,
        provider_marker=provider_marker,
        competitor_marker=competitor_marker,
    )


def _run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    verifier: str,
    scenario: str,
    coder: str = "api",
    timeout_seconds: int = 5,
    cancellation_event: threading.Event | None = None,
    probe: CodexProbeResult | ClaudeProbeResult | None = None,
) -> tuple[CandidateCase, Any]:
    case = _candidate(root, coder=coder, cancellation_event=cancellation_event)
    del monkeypatch
    verification = _adapter(
        verifier,
        timeout_seconds=timeout_seconds,
        scenario=scenario,
        probe=probe,
    ).verify(case.context, case.result)
    return case, verification


def _workspace(case: CandidateCase, verification: Any) -> Path:
    return case.context.run_directory / str(
        verification.metadata["cli_verifier_workspace"]
    )


def _input_text(workspace: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted((workspace / "input").rglob("*"))
        if path.is_file()
    )


def _independence(workspace: Path) -> dict[str, Any]:
    return json.loads(
        (workspace / "agent" / "independence.json").read_text(encoding="utf-8")
    )


def _assert_zero(verification: Any, failure: CliVerifierFailure) -> None:
    assert verification.acceptance_eligible is False
    assert verification.metadata["cli_verifier_failure"] == failure.value
    projection = verification.metadata["binary_user_projection"]
    assert projection.keys() == {"decision", "reason"}
    assert projection["decision"] == 0
    assert isinstance(projection["reason"], str) and projection["reason"]


def test_codex_verifier_decision_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="success"
    )
    assert verification.outcome == "accepted"
    assert verification.acceptance_eligible is True
    assert verification.metadata["binary_user_projection"]["decision"] == 1
    assert (
        _workspace(case, verification) / "output" / "normalized-result.json"
    ).is_file()


def test_codex_verifier_decision_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="reject"
    )
    _assert_zero(verification, CliVerifierFailure.SEMANTIC_REJECTION)


def test_claude_verifier_decision_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="claude", scenario="success"
    )
    assert verification.outcome == "accepted"
    assert verification.acceptance_eligible is True
    assert verification.metadata["binary_user_projection"]["decision"] == 1


def test_claude_verifier_decision_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="claude", scenario="insufficient"
    )
    _assert_zero(verification, CliVerifierFailure.INSUFFICIENT_EVIDENCE)


def test_verifier_workspace_structure_and_manifest_digests_are_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="success"
    )
    workspace = _workspace(case, verification)
    required = {
        "input/task.json",
        "input/success-criteria.json",
        "input/original-repository/target.txt",
        "input/candidate.patch",
        "input/changed-files.json",
        "input/validation-evidence.json",
        "input/debug-artifacts/index.json",
        "input/manifest.json",
        "output/verifier-result.json",
        "output/normalized-result.json",
        "agent/invocation.json",
        "agent/stdout.log",
        "agent/stderr.log",
        "agent/raw-events.jsonl",
        "agent/normalized-events.jsonl",
        "agent/process-result.json",
        "agent/cleanup.json",
    }
    actual = {
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert required <= actual
    manifest = json.loads((workspace / "input" / "manifest.json").read_text())
    assert set(manifest) == {
        "schema_version",
        "digest_scope",
        "artifacts",
        "original_repository",
        "access_policy",
        "blindness",
    }
    for artifact in manifest["artifacts"]:
        data = (workspace / artifact["path"]).read_bytes()
        assert artifact.keys() == {"path", "kind", "sha256", "bytes"}
        assert artifact["sha256"] == f"sha256:{hashlib.sha256(data).hexdigest()}"
        assert artifact["bytes"] == len(data)


def test_semantic_one_cannot_override_missing_deterministic_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    case = _candidate(tmp_path)
    observable = replace(
        case.context,
        task="target.txt must include verified-change.",
        success_criteria="target.txt must include verified-change.",
    )
    verification = _adapter("codex", scenario="success").verify(observable, case.result)
    assert verification.metadata["cli_verifier_process_spawned"] is True
    assert verification.acceptance_eligible is False
    workspace = _workspace(case, verification)
    normalized = json.loads(
        (workspace / "output" / "normalized-result.json").read_text()
    )
    assert normalized["semantic_decision"] == 1
    assert normalized["decision"] == 0
    assert normalized["binary_user_projection"]["decision"] == 0


def test_malformed_json_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="malformed"
    )
    _assert_zero(verification, CliVerifierFailure.MALFORMED_OUTPUT)


def test_wrong_decision_type_is_schema_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="wrong_decision_type"
    )
    _assert_zero(verification, CliVerifierFailure.SCHEMA_FAILURE)


def test_unknown_requirement_id_is_schema_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="claude", scenario="unknown_requirement"
    )
    _assert_zero(verification, CliVerifierFailure.SCHEMA_FAILURE)


def test_missing_final_result_is_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="missing_final"
    )
    _assert_zero(verification, CliVerifierFailure.MISSING_FINAL_RESULT)


def test_timeout_is_distinct_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path,
        monkeypatch,
        verifier="codex",
        scenario="timeout",
        timeout_seconds=1,
    )
    _assert_zero(verification, CliVerifierFailure.TIMEOUT)


def test_cancellation_is_distinct_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancellation = threading.Event()
    cancellation.set()
    _case, verification = _run(
        tmp_path,
        monkeypatch,
        verifier="claude",
        scenario="cancellation",
        cancellation_event=cancellation,
    )
    _assert_zero(verification, CliVerifierFailure.CANCELLATION)


def test_permission_failure_is_distinct_from_missing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="permission_failure"
    )
    _assert_zero(verification, CliVerifierFailure.PERMISSION_FAILURE)


def test_auth_missing_is_reported_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    system = _system("codex")
    options = dict(system.provider_options)
    options["launcher_arguments"] = [str(FAKE_CODEX), "--fixture-auth-missing"]
    system = system.model_copy(update={"provider_options": options})
    driver = CodexCliDriver(system)
    probe = driver.probe()
    assert probe.ready is False
    assert probe.failures == [CodexFailure.NOT_AUTHENTICATED]
    case = _candidate(tmp_path)
    verification = CliVerifierAdapter(driver, probe=probe).verify(
        case.context, case.result
    )
    _assert_zero(verification, CliVerifierFailure.AUTH_MISSING)
    assert verification.metadata["cli_verifier_process_spawned"] is False


def test_missing_executable_is_reported_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = _system("codex")
    probe = _ready_probe(system)
    assert isinstance(probe, CodexProbeResult)
    unavailable = probe.model_copy(
        update={
            "ready": False,
            "resolved_executable": None,
            "failures": [CodexFailure.NOT_INSTALLED],
            "messages": ["Codex executable is missing."],
        }
    )
    _case, verification = _run(
        tmp_path,
        monkeypatch,
        verifier="codex",
        scenario="success",
        probe=unavailable,
    )
    _assert_zero(verification, CliVerifierFailure.EXECUTABLE_MISSING)
    assert verification.metadata["cli_verifier_process_spawned"] is False


def test_unsupported_read_only_capability_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    system = _system("claude")
    options = dict(system.provider_options)
    options["launcher_arguments"] = [str(FAKE_CLAUDE), "--fixture-unsupported"]
    system = system.model_copy(update={"provider_options": options})
    driver = ClaudeCodeCliDriver(system)
    probe = driver.probe()
    assert probe.ready is False
    assert ClaudeFailure.MISSING_STRUCTURED_OUTPUT_CAPABILITY in probe.failures
    case = _candidate(tmp_path)
    verification = CliVerifierAdapter(driver, probe=probe).verify(
        case.context, case.result
    )
    _assert_zero(verification, CliVerifierFailure.UNSUPPORTED_CAPABILITY)


def test_path_traversal_manifest_is_rejected_before_process_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    case = _candidate(tmp_path)
    metadata = dict(case.result.metadata)
    metadata["changed_files"] = ["../outside.txt"]
    unsafe = replace(case.result, metadata=metadata)
    verification = _adapter("claude", scenario="success").verify(case.context, unsafe)
    _assert_zero(verification, CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE)
    assert verification.metadata["cli_verifier_process_spawned"] is False


def test_workspace_inside_target_is_rejected_without_target_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    case = _candidate(tmp_path)
    nested_run = case.repository / "forbidden-run-root"
    context = replace(case.context, run_directory=nested_run)
    before = _git(
        case.repository, "status", "--porcelain", "--untracked-files=all"
    ).stdout
    verification = _adapter("codex", scenario="success").verify(context, case.result)
    after = _git(
        case.repository, "status", "--porcelain", "--untracked-files=all"
    ).stdout
    _assert_zero(verification, CliVerifierFailure.ARTIFACT_PREPARATION_FAILURE)
    assert before == after == ""
    assert not nested_run.exists()


@pytest.mark.parametrize("verifier", ["codex", "claude"])
def test_read_only_policy_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verifier: str
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier=verifier, scenario="success"
    )
    workspace = _workspace(case, verification)
    assert all(
        path.stat().st_mode & 0o222 == 0 for path in (workspace / "input").rglob("*")
    )
    invocation = json.loads(
        (workspace / "agent" / "invocation.json").read_text(encoding="utf-8")
    )
    command = invocation["arguments"]
    if verifier == "codex":
        assert "--sandbox" not in command
        assert "--strict-config" in command
        assert 'default_permissions="villani_verifier_read_only"' in command
        assert (
            'permissions.villani_verifier_read_only.filesystem={":minimal"="read",":workspace_roots"={"."="read"}}'
            in command
        )
        assert "permissions.villani_verifier_read_only.network.enabled=false" in command
        assert 'web_search="disabled"' in command
        assert "allow_login_shell=false" in command
        assert "--ephemeral" in command
    else:
        assert "plan" in command
        assert "Read,Glob,Grep" in command
        assert not {"Bash", "Edit", "Write"}.intersection(command)
    environment_names = {item["name"] for item in invocation["environment"]}
    assert "CODEX_THREAD_ID" not in environment_names
    assert "LLM_MODEL" not in environment_names
    assert "OPENAI_API_KEY" not in environment_names
    assert "ANTHROPIC_AUTH_TOKEN" not in environment_names


@pytest.mark.parametrize("verifier", ["codex", "claude"])
def test_attempted_edit_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verifier: str
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier=verifier, scenario="attempt_edit"
    )
    assert verification.acceptance_eligible is True
    assert verification.metadata["cli_verifier_input_integrity_proved"] is True
    assert (
        _workspace(case, verification) / "input" / "original-repository" / "target.txt"
    ).read_text(encoding="utf-8") == "baseline\n"


@pytest.mark.parametrize("verifier", ["codex", "claude"])
def test_verifier_invocation_has_no_candidate_worktree_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verifier: str
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier=verifier, scenario="success"
    )
    workspace = _workspace(case, verification)
    invocation = json.loads(
        (workspace / "agent" / "invocation.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(invocation, ensure_ascii=False)
    assert str(Path(case.result.worktree_path).resolve()) not in serialized
    assert verification.metadata["cli_verifier_candidate_unchanged"] is True


@pytest.mark.parametrize("verifier", ["codex", "claude"])
def test_verifier_invocation_has_no_target_repository_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verifier: str
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier=verifier, scenario="success"
    )
    workspace = _workspace(case, verification)
    invocation = json.loads(
        (workspace / "agent" / "invocation.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(invocation, ensure_ascii=False)
    assert str(case.repository.resolve()) not in serialized
    assert verification.metadata["cli_verifier_target_unchanged"] is True


def test_coder_transcript_is_excluded_from_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="success"
    )
    text = _input_text(_workspace(case, verification))
    assert case.transcript_marker not in text
    assert "coder-transcript.jsonl" not in text


def test_provider_and_model_identity_are_excluded_from_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier="claude", scenario="success"
    )
    workspace = _workspace(case, verification)
    text = _input_text(workspace)
    assert case.provider_marker not in text
    assert str(case.result.model) not in text
    manifest = json.loads((workspace / "input" / "manifest.json").read_text())
    assert manifest["blindness"]["provider_identity_included"] is False
    assert manifest["blindness"]["model_identity_included"] is False


def test_cost_rank_and_candidate_number_are_excluded_from_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="success"
    )
    workspace = _workspace(case, verification)
    manifest = json.loads((workspace / "input" / "manifest.json").read_text())
    assert manifest["blindness"]["cost_included"] is False
    assert manifest["blindness"]["rank_included"] is False
    assert manifest["blindness"]["candidate_number_included"] is False
    quality = json.loads(
        (
            workspace / "input" / "debug-artifacts" / "candidate-patch-quality.json"
        ).read_text()
    )
    assert "candidate_id" not in quality
    assert "cost" not in quality
    assert "rank" not in quality


def test_competing_candidate_and_selector_output_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, verification = _run(
        tmp_path, monkeypatch, verifier="claude", scenario="success"
    )
    workspace = _workspace(case, verification)
    text = _input_text(workspace)
    assert case.competitor_marker not in text
    assert "must-not-leak" not in text
    manifest = json.loads((workspace / "input" / "manifest.json").read_text())
    assert manifest["blindness"]["competing_candidate_included"] is False
    assert manifest["blindness"]["selector_output_included"] is False


def test_independent_process_session_cwd_and_writable_roots_are_proved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, verification = _run(
        tmp_path,
        monkeypatch,
        verifier="codex",
        scenario="success",
        coder="claude",
    )
    proof = _independence(_workspace(case, verification))
    assert proof["process_ids_distinct"] is True
    assert proof["session_ids_distinct"] is True
    assert proof["cwd_differs_from_coder_worktree"] is True
    assert proof["writable_roots_exclude_candidate_and_target"] is True
    assert proof["process_ids_public"] is False
    assert "verifier_process_id" not in verification.metadata
    assert "coder_process_id" not in verification.metadata


def test_one_verifier_process_and_workspace_per_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_case, first = _run(
        tmp_path / "first", monkeypatch, verifier="codex", scenario="success"
    )
    second_case, second = _run(
        tmp_path / "second", monkeypatch, verifier="codex", scenario="success"
    )
    first_workspace = _workspace(first_case, first)
    second_workspace = _workspace(second_case, second)
    first_proof = _independence(first_workspace)
    second_proof = _independence(second_workspace)
    assert first_workspace != second_workspace
    assert first_proof["verifier_process_id"] != second_proof["verifier_process_id"]
    assert first_proof["verifier_session_id"] != second_proof["verifier_session_id"]


@pytest.mark.parametrize(
    ("coder", "verifier"),
    [
        ("codex", "claude"),
        ("claude", "codex"),
        ("codex", "codex"),
        ("claude", "claude"),
    ],
    ids=[
        "codex-coder-to-claude-verifier",
        "claude-coder-to-codex-verifier",
        "codex-coder-to-codex-verifier",
        "claude-coder-to-claude-verifier",
    ],
)
def test_all_coder_verifier_combinations_use_independent_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    coder: str,
    verifier: str,
) -> None:
    case, verification = _run(
        tmp_path,
        monkeypatch,
        verifier=verifier,
        scenario="success",
        coder=coder,
    )
    assert verification.acceptance_eligible is True
    proof = _independence(_workspace(case, verification))
    assert proof["process_ids_distinct"] is True
    assert proof["session_ids_distinct"] is True
    assert proof["same_provider"] is (coder == verifier)


def test_api_verifier_factory_remains_compatible() -> None:
    api_system = ApiAgentSystemConfig(
        kind="api",
        id="api-verifier",
        provider="existing-api",
        model="existing-model",
        roles={AgentRole.VERIFICATION},
        existing_backend_reference="existing-verifier",
    )

    class Registry:
        def inspect_configured(self, system_id: str) -> ApiAgentSystemConfig:
            assert system_id == "api-verifier"
            return api_system

    existing = VillaniVerifierAdapter(no_llm=True)
    bindings = RoleBindings(
        profile_id="api-compatible",
        bindings={
            AgentRole.CLASSIFICATION: "unused-classifier",
            AgentRole.CODING: "unused-coder",
            AgentRole.VERIFICATION: "api-verifier",
            AgentRole.SELECTION: "unused-selector",
        },
    )
    resolved = build_verifier(
        bindings,
        Registry(),  # type: ignore[arg-type]
        RoleFactoryDependencies(api_verifiers={"existing-verifier": existing}),
    )
    assert resolved is existing


def test_cli_verifier_factory_implements_existing_verifier_port() -> None:
    cli_system = _system("codex")

    class Registry:
        def inspect_configured(self, system_id: str) -> CliAgentSystemConfig:
            assert system_id == cli_system.id
            return cli_system

    implementation = _adapter("codex", scenario="success")
    bindings = RoleBindings(
        profile_id="cli-verifier",
        bindings={
            AgentRole.CLASSIFICATION: "unused-classifier",
            AgentRole.CODING: "unused-coder",
            AgentRole.VERIFICATION: cli_system.id,
            AgentRole.SELECTION: "unused-selector",
        },
    )
    resolved = build_verifier(
        bindings,
        Registry(),  # type: ignore[arg-type]
        RoleFactoryDependencies(cli_verifiers={cli_system.id: implementation}),
    )
    assert resolved is implementation


@pytest.mark.parametrize(
    ("driver_name", "fixture"),
    [("codex", FAKE_CODEX), ("claude_code", FAKE_CLAUDE)],
)
def test_public_cli_configures_doctors_and_binds_verification_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    driver_name: str,
    fixture: Path,
) -> None:
    home = tmp_path / "villani-home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
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
    system_id = f"{driver_name}-verifier"
    added = runner.invoke(
        unified.app,
        [
            "agents",
            "add",
            system_id,
            "--driver",
            driver_name,
            "--executable",
            sys.executable,
            "--model",
            "verifier-fixture-model",
            "--roles",
            "verification",
        ],
    )
    assert added.exit_code == 0, added.output
    configuration = yaml.safe_load(unified._config_path().read_text(encoding="utf-8"))
    configured = configuration["agent_systems"]["systems"][system_id]
    assert configured["roles"] == ["verification"]
    assert configured["instruction_policy"] == "villani_controlled"
    assert configured["permission_profile"] == "read_only"
    assert configured["environment_policy"] == "minimal"
    configured["provider_options"] = {"launcher_arguments": [str(fixture)]}
    unified._write_config(unified._config_path(), configuration)

    doctor = runner.invoke(unified.app, ["agents", "doctor", system_id, "--json"])
    assert doctor.exit_code == 0, doctor.output
    report = json.loads(doctor.stdout)["reports"][0]
    assert report["selectable"] is True
    bound = runner.invoke(
        unified.app,
        ["profiles", "set-role", "hybrid", "verification", system_id],
    )
    assert bound.exit_code == 0, bound.output
    inspected = runner.invoke(unified.app, ["profiles", "inspect", "hybrid", "--json"])
    assert inspected.exit_code == 0, inspected.output
    profile = json.loads(inspected.stdout)
    assert profile["runnable"] is True
    assert profile["bindings"]["bindings"]["verification"] == system_id


@pytest.mark.parametrize("role", ["classification", "selection"])
def test_cli_classification_and_selection_are_single_role_read_only_systems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    monkeypatch.setenv("VILLANI_HOME", str(tmp_path / "villani-home"))
    unified._write_config(unified._config_path(), {"backends": {}})
    result = CliRunner().invoke(
        unified.app,
        [
            "agents",
            "add",
            f"cli-{role}",
            "--driver",
            "codex",
            "--model",
            "fixture-model",
            "--roles",
            role,
        ],
    )
    assert result.exit_code == 0, result.output
    configuration = yaml.safe_load(unified._config_path().read_text(encoding="utf-8"))
    configured = configuration["agent_systems"]["systems"][f"cli-{role}"]
    assert configured["roles"] == [role]
    assert configured["instruction_policy"] == "villani_controlled"
    assert configured["permission_profile"] == "read_only"
    assert configured["environment_policy"] == "minimal"


@pytest.mark.parametrize(
    ("verifier", "scenario"),
    [
        ("codex", "malformed"),
        ("codex", "process_crash"),
        ("claude", "missing_final"),
        ("claude", "permission_failure"),
    ],
)
def test_infrastructure_failure_can_never_yield_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verifier: str,
    scenario: str,
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier=verifier, scenario=scenario
    )
    assert verification.acceptance_eligible is False
    assert verification.metadata["binary_user_projection"]["decision"] == 0
    assert verification.metadata["cli_verifier_failure"] not in {
        None,
        CliVerifierFailure.SEMANTIC_REJECTION.value,
        CliVerifierFailure.INSUFFICIENT_EVIDENCE.value,
    }


def test_binary_user_projection_is_exact_for_both_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _accepted_case, accepted = _run(
        tmp_path / "accepted", monkeypatch, verifier="codex", scenario="success"
    )
    _rejected_case, rejected = _run(
        tmp_path / "rejected", monkeypatch, verifier="claude", scenario="reject"
    )
    for expected, verification in ((1, accepted), (0, rejected)):
        projection = verification.metadata["binary_user_projection"]
        assert projection.keys() == {"decision", "reason"}
        assert type(projection["decision"]) is int
        assert projection["decision"] == expected
        assert isinstance(projection["reason"], str) and projection["reason"]


@pytest.mark.parametrize("scenario", ["missing_field", "extra_field"])
def test_missing_or_additional_result_fields_fail_schema_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario=scenario
    )
    _assert_zero(verification, CliVerifierFailure.SCHEMA_FAILURE)


def test_duplicate_result_fields_fail_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _case, verification = _run(
        tmp_path, monkeypatch, verifier="codex", scenario="duplicate_field"
    )
    _assert_zero(verification, CliVerifierFailure.MALFORMED_OUTPUT)


def test_strict_normalizer_rejects_boolean_decision() -> None:
    raw = json.dumps(
        {
            "decision": True,
            "reason": "Boolean is not an integer decision.",
            "requirements_proved": ["req-known"],
            "requirements_not_proved": [],
            "blocking_issues": [],
        }
    )
    with pytest.raises(ValueError, match="schema failure"):
        normalize_cli_verifier_result(raw, requirement_ids={"req-known"})


def _real_system(driver: str, executable: str, model: str) -> CliAgentSystemConfig:
    configured_driver = "codex" if driver == "codex" else "claude_code"
    return CliAgentSystemConfig(
        kind="cli_agent",
        id=f"real-{driver}-verifier-smoke",
        driver=configured_driver,  # type: ignore[arg-type]
        executable=executable,
        model=model,
        roles={AgentRole.VERIFICATION},
        timeout_seconds=180,
        max_parallel=1,
        instruction_policy="villani_controlled",
        permission_profile="read_only",
        environment_policy="minimal",
        provider_options={"max_turns": 8},
    )


@pytest.mark.integration
@pytest.mark.parametrize("driver_name", ["codex", "claude"])
def test_real_cli_verifier_smoke_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver_name: str
) -> None:
    import os

    if os.environ.get("VILLANI_RUN_REAL_CLI_VERIFIER_SMOKE") != "1":
        pytest.skip("set VILLANI_RUN_REAL_CLI_VERIFIER_SMOKE=1 to run")
    executable = shutil.which("codex" if driver_name == "codex" else "claude")
    if executable is None:
        pytest.skip(f"{driver_name} CLI is not installed")
    model_variable = (
        "VILLANI_REAL_CODEX_VERIFIER_MODEL"
        if driver_name == "codex"
        else "VILLANI_REAL_CLAUDE_VERIFIER_MODEL"
    )
    model = os.environ.get(model_variable)
    if not model:
        pytest.skip(f"set {model_variable} to select the real smoke-test model")
    system = _real_system(driver_name, executable, model)
    driver = (
        CodexCliDriver(system)
        if driver_name == "codex"
        else ClaudeCodeCliDriver(system)
    )
    probe = driver.probe()
    if not probe.ready:
        pytest.skip("; ".join(probe.messages))
    case = _candidate(tmp_path)
    monkeypatch.delenv("VILLANI_FAKE_CODEX_VERIFIER_SCENARIO", raising=False)
    monkeypatch.delenv("VILLANI_FAKE_CLAUDE_VERIFIER_SCENARIO", raising=False)
    verification = CliVerifierAdapter(driver, probe=probe).verify(
        case.context, case.result
    )
    assert verification.metadata["cli_verifier_process_spawned"] is True
    assert verification.metadata["binary_user_projection"]["decision"] in {0, 1}
