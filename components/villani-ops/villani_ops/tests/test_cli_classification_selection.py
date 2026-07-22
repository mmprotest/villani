from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from villani_ops.closed_loop.agent_systems.factories import (
    RoleFactoryDependencies,
    build_attempt_runner,
    build_classifier,
    build_selector,
    build_verifier,
)
from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    AgentSystemInspection,
    ApiAgentSystemConfig,
    CliAgentSystemConfig,
    InternalRunnerSystemConfig,
    RoleBindings,
)
from villani_ops.closed_loop.agent_systems.role_registry import RoleSystemRegistry
from villani_ops.closed_loop.claude_code_cli.driver import ClaudeCodeCliDriver
from villani_ops.closed_loop.claude_code_cli.models import ClaudeProbeResult
from villani_ops.closed_loop.cli_classification import CliClassifierAdapter
from villani_ops.closed_loop.cli_roles.models import CliRoleFailure
from villani_ops.closed_loop.cli_selection import CliSelectorAdapter
from villani_ops.closed_loop.codex_cli.driver import CodexCliDriver
from villani_ops.closed_loop.codex_cli.models import CodexProbeResult
from villani_ops.closed_loop.interfaces import (
    Classification,
    ClassificationContext,
    EligibleCandidate,
    SelectionContext,
)


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cli_roles"
FAKE_CODEX = FIXTURES / "fake_codex_roles.py"
FAKE_CLAUDE = FIXTURES / "fake_claude_roles.py"
NOW = datetime.now(timezone.utc)


def _system(
    driver_name: str,
    role: AgentRole,
    *,
    scenario: str = "success",
    timeout_seconds: int = 5,
    auth_missing: bool = False,
    unsupported: bool = False,
) -> CliAgentSystemConfig:
    fixture = FAKE_CODEX if driver_name == "codex" else FAKE_CLAUDE
    arguments = [str(fixture)]
    if scenario != "success":
        arguments.extend(["--fixture-scenario", scenario])
    if auth_missing:
        arguments.append("--fixture-auth-missing")
    if unsupported:
        arguments.append("--fixture-unsupported")
    return CliAgentSystemConfig(
        kind="cli_agent",
        id=f"{driver_name}-{role.value}-{scenario}",
        driver="codex" if driver_name == "codex" else "claude_code",
        executable=sys.executable,
        model=f"user-configured-{role.value}-model",
        roles={role},
        timeout_seconds=timeout_seconds,
        max_parallel=1,
        instruction_policy="villani_controlled",
        permission_profile="read_only",
        environment_policy="minimal",
        provider_options={
            "launcher_arguments": arguments,
            "graceful_shutdown_seconds": 0.1,
            "max_turns": 4,
        },
    )


def _probe(system: CliAgentSystemConfig):
    executable = str(Path(sys.executable).resolve())
    if system.driver == "codex":
        return CodexProbeResult(
            system_id=system.id,
            checked_at=NOW,
            configured_executable=system.executable,
            resolved_executable=executable,
            exact_version_output="codex-cli 9.9.9-role-fixture",
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
        exact_version_output="2.1.138 (Claude Code role fixture)",
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


def _driver(system: CliAgentSystemConfig):
    return (
        CodexCliDriver(system)
        if system.driver == "codex"
        else ClaudeCodeCliDriver(system)
    )


def _repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir()
    (repository / "target.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "test_target.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    return repository


def _classification_context(root: Path, repository: Path) -> ClassificationContext:
    run_directory = root / "run"
    run_directory.mkdir()
    return ClassificationContext(
        run_id="run-cli-classifier",
        trace_id="trace-cli-classifier",
        task_id="task-cli-classifier",
        repository_path=str(repository),
        success_criteria="The target behavior passes explicit tests.",
        requires_file_changes=True,
        policy_configuration={},
        run_directory=run_directory,
    )


def _classifier(
    driver_name: str,
    *,
    scenario: str = "success",
    timeout_seconds: int = 5,
) -> CliClassifierAdapter:
    system = _system(
        driver_name,
        AgentRole.CLASSIFICATION,
        scenario=scenario,
        timeout_seconds=timeout_seconds,
    )
    return CliClassifierAdapter(_driver(system), probe=_probe(system))


@pytest.mark.parametrize("driver_name", ["codex", "claude"])
@pytest.mark.parametrize("difficulty", ["easy", "medium", "hard"])
def test_cli_classifier_valid_fixtures_and_canonical_mapping(
    tmp_path: Path, driver_name: str, difficulty: str
) -> None:
    repository = _repository(tmp_path)
    baseline = {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }
    context = _classification_context(tmp_path, repository)
    classification = _classifier(driver_name, scenario=difficulty).classify(
        "Update target.py and run its tests.", context
    )
    assert classification.metadata["classification_fallback"] is False
    assert classification.metadata["model_classification"]["difficulty"] == difficulty
    assert classification.difficulty in {"easy", "medium", "hard"}
    assert classification.required_capabilities == (
        "repository_editing",
        "test_execution",
    )
    assert classification.signals["uncertainty"] in {"low", "medium", "high"}
    workspace = Path(str(classification.metadata["cli_classifier_workspace"]))
    manifest = json.loads((workspace / "input" / "manifest.json").read_text())
    assert manifest["role"] == "classification"
    assert all(value is False for value in manifest["blindness"].values())
    assert {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    } == baseline
    independence = json.loads((workspace / "agent" / "independence.json").read_text())
    assert independence["resume_requested"] is False
    assert independence["agent_writable_roots"] == []


@pytest.mark.parametrize(
    ("scenario", "failure"),
    [
        ("malformed", CliRoleFailure.MALFORMED_OUTPUT),
        ("wrong_type", CliRoleFailure.SCHEMA_FAILURE),
        ("direct_provider_selection", CliRoleFailure.SCHEMA_FAILURE),
        ("missing_final", CliRoleFailure.MISSING_FINAL_RESULT),
        ("permission_failure", CliRoleFailure.PERMISSION_FAILURE),
        ("process_crash", CliRoleFailure.PROCESS_CRASH),
    ],
)
def test_cli_classifier_failures_use_explicit_conservative_fallback(
    tmp_path: Path, scenario: str, failure: CliRoleFailure
) -> None:
    repository = _repository(tmp_path)
    context = _classification_context(tmp_path, repository)
    classification = _classifier("codex", scenario=scenario).classify(
        "Update target.py.", context
    )
    assert classification.difficulty == "hard"
    assert classification.risk == "high"
    assert classification.metadata["classification_fallback"] is True
    assert classification.metadata["cli_classifier_failure"] == failure.value


def test_cli_classifier_timeout_falls_back(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    context = _classification_context(tmp_path, repository)
    classification = _classifier(
        "codex", scenario="timeout", timeout_seconds=1
    ).classify("Update target.py.", context)
    assert classification.metadata["cli_classifier_failure"] == "timeout"
    assert classification.metadata["classification_fallback"] is True


@pytest.mark.parametrize("driver_name", ["codex", "claude"])
@pytest.mark.parametrize(
    ("probe_option", "expected_failure"),
    [
        ("auth_missing", "auth_missing"),
        ("unsupported", "unsupported_capability"),
    ],
)
def test_classifier_doctor_failure_prevents_role_process_start(
    tmp_path: Path,
    driver_name: str,
    probe_option: str,
    expected_failure: str,
) -> None:
    system = _system(
        driver_name,
        AgentRole.CLASSIFICATION,
        auth_missing=probe_option == "auth_missing",
        unsupported=probe_option == "unsupported",
    )
    driver = _driver(system)
    probe = driver.probe()
    assert probe.ready is False
    repository = _repository(tmp_path)
    result = CliClassifierAdapter(driver, probe=probe).classify(
        "Update target.py.", _classification_context(tmp_path, repository)
    )
    effective_failure = (
        "unsupported_version"
        if driver_name == "codex" and probe_option == "unsupported"
        else expected_failure
    )
    assert result.metadata["cli_classifier_failure"] == effective_failure
    assert result.metadata["cli_classifier_process_spawned"] is False


def test_classifier_cancellation_is_explicit_infrastructure_fallback(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    cancellation = threading.Event()
    cancellation.set()
    context = replace(
        _classification_context(tmp_path, repository),
        cancellation_event=cancellation,
    )
    result = _classifier("codex", scenario="timeout").classify(
        "Update target.py.", context
    )
    assert result.metadata["cli_classifier_failure"] == "cancellation"
    assert result.metadata["classification_fallback"] is True


def _candidate(
    root: Path,
    attempt_id: str,
    changed_file: str,
    *,
    eligible: bool = True,
) -> EligibleCandidate:
    attempt_directory = root / "run" / "attempts" / attempt_id
    attempt_directory.mkdir(parents=True)
    patch = (
        f"diff --git a/{changed_file} b/{changed_file}\n"
        f"--- a/{changed_file}\n+++ b/{changed_file}\n@@ -1 +1 @@\n-old\n+new\n"
    )
    patch_path = attempt_directory / "candidate.patch"
    patch_path.write_text(patch, encoding="utf-8")
    (attempt_directory / "stdout.log").write_text("tests passed\n", encoding="utf-8")
    (attempt_directory / "stderr.log").write_text("", encoding="utf-8")
    worktree = root / f"worktree-{attempt_id}"
    worktree.mkdir()
    (worktree / changed_file).write_text("new\n", encoding="utf-8")
    requirement = SimpleNamespace(
        requirement_id="req-stable",
        description="The requested behavior is implemented.",
        outcome="passed" if eligible else "failed",
        evidence_ids=[f"repo-validation:{attempt_id}"],
    )
    evidence = SimpleNamespace(
        kind="repository_test",
        summary=f"Repository tests passed for {attempt_id}.",
    )
    verification = SimpleNamespace(
        acceptance_eligible=eligible,
        confidence=0.9,
        requirement_results=[requirement],
        success_evidence=[evidence] if eligible else [],
        failure_evidence=[] if eligible else [evidence],
        missing_evidence=[],
        risk_flags=[],
        metadata={"repository_validation_status": "passed"},
        reason=(
            "All supplied requirements and repository validation are proven for "
            f"{attempt_id}."
        ),
    )
    attempt = SimpleNamespace(
        attempt_id=attempt_id,
        patch_path=patch_path.relative_to(root / "run").as_posix(),
        metadata={
            "changed_files": [changed_file],
            "provider": "MUST_NOT_LEAK",
            "model": "MUST_NOT_LEAK",
            "cost": 999,
            "candidate_rank": 1,
            "coder_transcript": "MUST_NOT_LEAK",
        },
        cost_usd=999,
        cost_accounting_status="complete",
        worktree_path=str(worktree),
    )
    return EligibleCandidate(attempt=attempt, verification=verification, patch=patch)


def _selection_case(root: Path):
    repository = _repository(root)
    (root / "run").mkdir(exist_ok=True)
    context = SelectionContext(
        run_id="run-cli-selector",
        trace_id="trace-cli-selector",
        task="Implement the requested behavior.",
        repository_path=str(repository),
        success_criteria="Repository tests pass.",
        policy_configuration={},
        run_directory=root / "run",
    )
    candidates = (
        _candidate(root, "attempt_alpha", "other.py"),
        _candidate(root, "attempt_beta", "preferred.py"),
    )
    return repository, context, candidates


def _selector(
    driver_name: str,
    *,
    scenario: str = "success",
    timeout_seconds: int = 5,
) -> CliSelectorAdapter:
    system = _system(
        driver_name,
        AgentRole.SELECTION,
        scenario=scenario,
        timeout_seconds=timeout_seconds,
    )
    return CliSelectorAdapter(_driver(system), probe=_probe(system))


@pytest.mark.parametrize("driver_name", ["codex", "claude"])
def test_cli_selector_two_tied_eligible_candidates_and_blind_packet(
    tmp_path: Path, driver_name: str
) -> None:
    repository, context, candidates = _selection_case(tmp_path)
    baseline = hashlib.sha256((repository / "target.py").read_bytes()).hexdigest()
    selection = _selector(driver_name).select(candidates, context)
    assert selection.selected_attempt_id == "attempt_beta"
    assert selection.strategy == "cli_semantic_evidence_tiebreak_v1"
    assert selection.metadata["cli_selector_fallback"] is False
    workspace = Path(str(selection.metadata["cli_selector_workspace"]))
    input_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (workspace / "input").glob("*")
        if path.is_file()
    )
    for forbidden in (
        "MUST_NOT_LEAK",
        "attempt_alpha",
        "attempt_beta",
        '"cost":999',
    ):
        assert forbidden not in input_text
    invocation = json.loads((workspace / "agent" / "invocation.json").read_text())
    assert invocation["role_workspace_identity"]["writable_roots"] == []
    assert invocation["role_workspace_identity"]["agent_writable_roots"] == []
    assert (
        hashlib.sha256((repository / "target.py").read_bytes()).hexdigest() == baseline
    )


def test_claude_selector_disables_all_tools_and_ambient_features(
    tmp_path: Path,
) -> None:
    _repository_value, context, candidates = _selection_case(tmp_path)
    selection = _selector("claude").select(candidates, context)
    workspace = Path(str(selection.metadata["cli_selector_workspace"]))
    invocation = json.loads((workspace / "agent" / "invocation.json").read_text())
    arguments = invocation["arguments"]
    assert arguments[arguments.index("--tools") + 1] == ""
    assert arguments[arguments.index("--allowedTools") + 1] == ""
    assert "--no-session-persistence" in arguments
    assert "--bare" in arguments
    assert "--strict-mcp-config" in arguments
    assert not {"Bash", "Edit", "Write"}.intersection(arguments)


def test_selector_zero_eligible_candidates_does_not_spawn(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (tmp_path / "run").mkdir()
    context = SelectionContext(
        run_id="run-zero",
        trace_id="trace-zero",
        task="Task",
        repository_path=str(repository),
        success_criteria="Criteria",
        policy_configuration={},
        run_directory=tmp_path / "run",
    )
    with pytest.raises(ValueError, match="no normalized eligible"):
        _selector("codex", scenario="process_crash").select((), context)
    assert not (tmp_path / "run" / "selection").exists()


def test_selector_input_order_cannot_encode_route_preference(tmp_path: Path) -> None:
    _repository_value, context, candidates = _selection_case(tmp_path)
    first = _selector("codex").select(candidates, context)
    second = _selector("codex").select(tuple(reversed(candidates)), context)
    assert first.selected_attempt_id == "attempt_beta"
    assert second.selected_attempt_id == "attempt_beta"


def test_role_manifest_has_exact_digests_and_no_identity_fields(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    context = _classification_context(tmp_path, repository)
    result = _classifier("codex").classify("Update target.py.", context)
    workspace = Path(str(result.metadata["cli_classifier_workspace"]))
    manifest = json.loads((workspace / "input" / "manifest.json").read_text())
    assert set(manifest) == {
        "schema_version",
        "role",
        "invocation_id",
        "created_at",
        "artifacts",
        "blindness",
        "security",
    }
    for record in manifest["artifacts"]:
        data = (workspace / record["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == record["sha256"]
        assert len(data) == record["bytes"]
    serialized = json.dumps(manifest).casefold()
    for forbidden in ("provider_identity", "model_identity", "cost", "route_rank"):
        assert forbidden not in serialized


def test_one_eligible_candidate_skips_cli_and_preserves_patch_identity(
    tmp_path: Path,
) -> None:
    _repository_value, context, candidates = _selection_case(tmp_path)
    selection = _selector("codex", scenario="process_crash").select(
        (candidates[1],), context
    )
    assert selection.selected_attempt_id == "attempt_beta"
    assert selection.metadata["cli_selector_invoked"] is False
    assert selection.metadata["cli_selector_fallback"] is False
    selected = next(
        item
        for item in candidates
        if item.attempt.attempt_id == selection.selected_attempt_id
    )
    assert selected.patch == candidates[1].patch


def test_rejected_candidate_is_excluded_before_selector(tmp_path: Path) -> None:
    _repository_value, context, candidates = _selection_case(tmp_path)
    rejected = _candidate(tmp_path, "attempt_rejected", "rejected.py", eligible=False)
    selection = _selector("codex", scenario="process_crash").select(
        (candidates[1], rejected), context
    )
    assert selection.selected_attempt_id == "attempt_beta"
    assert selection.metadata["cli_selector_invoked"] is False


def test_selector_supplies_every_eligible_candidate_and_only_eligible_candidates(
    tmp_path: Path,
) -> None:
    _repository_value, context, candidates = _selection_case(tmp_path)
    third = _candidate(tmp_path, "attempt_gamma", "gamma.py")
    rejected = _candidate(tmp_path, "attempt_rejected", "rejected.py", eligible=False)
    selection = _selector("codex").select((*candidates, third, rejected), context)
    workspace = Path(str(selection.metadata["cli_selector_workspace"]))
    document = json.loads(
        (workspace / "input" / "candidates.json").read_text(encoding="utf-8")
    )
    assert len(document["candidates"]) == 3
    assert len({item["candidate_id"] for item in document["candidates"]}) == 3
    input_text = json.dumps(document, sort_keys=True)
    assert "attempt_rejected" not in input_text


@pytest.mark.parametrize(
    ("scenario", "failure"),
    [
        ("unknown_id", CliRoleFailure.SCHEMA_FAILURE),
        ("duplicate_ranking", CliRoleFailure.SCHEMA_FAILURE),
        ("missing_candidate", CliRoleFailure.SCHEMA_FAILURE),
        ("malformed", CliRoleFailure.MALFORMED_OUTPUT),
        ("missing_final", CliRoleFailure.MISSING_FINAL_RESULT),
        ("permission_failure", CliRoleFailure.PERMISSION_FAILURE),
        ("process_crash", CliRoleFailure.PROCESS_CRASH),
    ],
)
def test_selector_failure_uses_explicit_deterministic_fallback(
    tmp_path: Path, scenario: str, failure: CliRoleFailure
) -> None:
    _repository_value, context, candidates = _selection_case(tmp_path)
    selection = _selector("codex", scenario=scenario).select(candidates, context)
    assert selection.selected_attempt_id in {"attempt_alpha", "attempt_beta"}
    assert selection.metadata["deterministic_evidence_controls_selection"] is True
    assert selection.metadata["cli_selector_fallback"] is True
    assert selection.metadata["cli_selector_failure"] == failure.value


def test_selector_timeout_uses_deterministic_fallback(tmp_path: Path) -> None:
    _repository_value, context, candidates = _selection_case(tmp_path)
    selection = _selector("claude", scenario="timeout", timeout_seconds=1).select(
        candidates, context
    )
    assert selection.metadata["cli_selector_failure"] == "timeout"
    assert selection.metadata["cli_selector_fallback"] is True


@pytest.mark.parametrize("driver_name", ["codex", "claude"])
@pytest.mark.parametrize(
    ("probe_option", "expected_failure"),
    [
        ("auth_missing", "auth_missing"),
        ("unsupported", "unsupported_capability"),
    ],
)
def test_selector_doctor_failure_prevents_role_process_start(
    tmp_path: Path,
    driver_name: str,
    probe_option: str,
    expected_failure: str,
) -> None:
    system = _system(
        driver_name,
        AgentRole.SELECTION,
        auth_missing=probe_option == "auth_missing",
        unsupported=probe_option == "unsupported",
    )
    driver = _driver(system)
    probe = driver.probe()
    assert probe.ready is False
    _repository_value, context, candidates = _selection_case(tmp_path)
    selection = CliSelectorAdapter(driver, probe=probe).select(candidates, context)
    effective_failure = (
        "unsupported_version"
        if driver_name == "codex" and probe_option == "unsupported"
        else expected_failure
    )
    assert selection.metadata["cli_selector_failure"] == effective_failure
    assert selection.metadata["cli_selector_invoked"] is False


def test_selector_cancellation_uses_explicit_deterministic_fallback(
    tmp_path: Path,
) -> None:
    _repository_value, context, candidates = _selection_case(tmp_path)
    cancellation = threading.Event()
    cancellation.set()
    context = replace(context, cancellation_event=cancellation)
    selection = _selector("claude", scenario="timeout").select(candidates, context)
    assert selection.metadata["cli_selector_failure"] == "cancellation"
    assert selection.metadata["cli_selector_fallback"] is True


@pytest.mark.parametrize(
    ("classifier_driver", "selector_driver"),
    [
        ("codex", "codex"),
        ("codex", "claude"),
        ("claude", "codex"),
        ("claude", "claude"),
    ],
)
def test_classifier_and_selector_always_use_independent_processes_and_sessions(
    tmp_path: Path, classifier_driver: str, selector_driver: str
) -> None:
    classification_root = tmp_path / "classification-case"
    classification_root.mkdir()
    repository = _repository(classification_root)
    classification_context = _classification_context(classification_root, repository)
    classification = _classifier(classifier_driver).classify(
        "Update target.py.", classification_context
    )
    classification_workspace = Path(
        str(classification.metadata["cli_classifier_workspace"])
    )
    classification_proof = json.loads(
        (classification_workspace / "agent" / "independence.json").read_text()
    )

    selection_root = tmp_path / "selection-case"
    selection_root.mkdir()
    _selection_repository, selection_context, candidates = _selection_case(
        selection_root
    )
    selection = _selector(selector_driver).select(candidates, selection_context)
    selection_workspace = Path(str(selection.metadata["cli_selector_workspace"]))
    selection_proof = json.loads(
        (selection_workspace / "agent" / "independence.json").read_text()
    )
    assert (
        classification_proof["role_invocation_id"]
        != selection_proof["role_invocation_id"]
    )
    assert classification_proof["process_id"] != selection_proof["process_id"]
    assert classification_proof["session_id"] != selection_proof["session_id"]
    assert classification_proof["cwd"] != selection_proof["cwd"]
    assert classification_proof["resume_requested"] is False
    assert selection_proof["resume_requested"] is False


def test_api_factories_remain_compatible_and_cli_factories_use_same_ports() -> None:
    api_classifier = SimpleNamespace(
        classify=lambda *_: Classification("easy", "low", "x")
    )
    api_selector = SimpleNamespace(select=lambda *_: None)
    cli_classifier = object()
    cli_selector = object()
    api_systems = {
        "api-classifier": ApiAgentSystemConfig(
            kind="api",
            id="api-classifier",
            provider="api",
            model="configured",
            roles={AgentRole.CLASSIFICATION},
            existing_backend_reference="api-ref",
        ),
        "api-selector": ApiAgentSystemConfig(
            kind="api",
            id="api-selector",
            provider="api",
            model="configured",
            roles={AgentRole.SELECTION},
            existing_backend_reference="api-ref",
        ),
    }

    class Registry:
        def __init__(self, systems):
            self.systems = systems

        def inspect_configured(self, system_id):
            return self.systems[system_id]

    api_bindings = RoleBindings(
        profile_id="api",
        bindings={
            AgentRole.CLASSIFICATION: "api-classifier",
            AgentRole.CODING: "unused-coder",
            AgentRole.VERIFICATION: "unused-verifier",
            AgentRole.SELECTION: "api-selector",
        },
    )
    dependencies = RoleFactoryDependencies(
        api_classifiers={"api-ref": api_classifier},
        api_selectors={"api-ref": api_selector},
    )
    registry = Registry(api_systems)
    assert build_classifier(api_bindings, registry, dependencies) is api_classifier
    assert build_selector(api_bindings, registry, dependencies) is api_selector

    classifier_system = _system("codex", AgentRole.CLASSIFICATION)
    selector_system = _system("claude", AgentRole.SELECTION)
    cli_bindings = api_bindings.model_copy(
        update={
            "profile_id": "cli",
            "bindings": {
                **api_bindings.bindings,
                AgentRole.CLASSIFICATION: classifier_system.id,
                AgentRole.SELECTION: selector_system.id,
            },
        }
    )
    cli_registry = Registry(
        {classifier_system.id: classifier_system, selector_system.id: selector_system}
    )
    cli_dependencies = RoleFactoryDependencies(
        cli_classifiers={classifier_system.id: cli_classifier},
        cli_selectors={selector_system.id: cli_selector},
    )
    assert (
        build_classifier(cli_bindings, cli_registry, cli_dependencies) is cli_classifier
    )
    assert build_selector(cli_bindings, cli_registry, cli_dependencies) is cli_selector


@pytest.mark.parametrize(
    ("profile_id", "role_kinds"),
    [
        ("all-api", ("api", "api", "api", "api")),
        ("all-codex", ("codex", "codex", "codex", "codex")),
        ("all-claude", ("claude", "claude", "claude", "claude")),
        (
            "codex-coder-claude-verifier-selector",
            ("api", "codex", "claude", "claude"),
        ),
        (
            "claude-coder-codex-verifier-selector",
            ("api", "claude", "codex", "codex"),
        ),
        (
            "api-classifier-cli-coder-verifier",
            ("api", "codex", "claude", "deterministic"),
        ),
        (
            "cli-classifier-api-coder-verifier",
            ("claude", "api", "api", "deterministic"),
        ),
        (
            "deterministic-selector",
            ("codex", "claude", "codex", "deterministic"),
        ),
    ],
)
def test_declared_profile_combinations_resolve_through_existing_role_ports(
    profile_id: str, role_kinds: tuple[str, str, str, str]
) -> None:
    systems: dict[str, object] = {}
    bindings_by_role: dict[AgentRole, str] = {}
    expected: dict[AgentRole, object] = {}
    api: dict[AgentRole, dict[str, object]] = {role: {} for role in AgentRole}
    cli: dict[AgentRole, dict[str, object]] = {role: {} for role in AgentRole}
    internal: dict[AgentRole, dict[str, object]] = {role: {} for role in AgentRole}
    for role, kind in zip(AgentRole, role_kinds, strict=True):
        implementation = object()
        expected[role] = implementation
        if kind == "api":
            reference = f"api-ref-{profile_id}-{role.value}"
            system = ApiAgentSystemConfig(
                kind="api",
                id=f"api-{profile_id}-{role.value}",
                provider="existing-api",
                model=f"configured-{role.value}",
                roles={role},
                existing_backend_reference=reference,
            )
            api[role][reference] = implementation
        elif kind == "deterministic":
            assert role == AgentRole.SELECTION
            system = InternalRunnerSystemConfig(
                kind="internal_runner",
                id=f"internal-{profile_id}-{role.value}",
                runner="evidence_selector",
                roles={role},
            )
            internal[role][system.runner] = implementation
        else:
            system = _system(kind, role)
            cli[role][system.id] = implementation
        systems[system.id] = system
        bindings_by_role[role] = system.id

    class Registry:
        def inspect_configured(self, system_id: str):
            return systems[system_id]

    bindings = RoleBindings(profile_id=profile_id, bindings=bindings_by_role)
    dependencies = RoleFactoryDependencies(
        api_classifiers=api[AgentRole.CLASSIFICATION],
        api_attempt_runners=api[AgentRole.CODING],
        api_verifiers=api[AgentRole.VERIFICATION],
        api_selectors=api[AgentRole.SELECTION],
        internal_classifiers=internal[AgentRole.CLASSIFICATION],
        internal_attempt_runners=internal[AgentRole.CODING],
        internal_verifiers=internal[AgentRole.VERIFICATION],
        internal_selectors=internal[AgentRole.SELECTION],
        cli_classifiers=cli[AgentRole.CLASSIFICATION],
        cli_attempt_runners=cli[AgentRole.CODING],
        cli_verifiers=cli[AgentRole.VERIFICATION],
        cli_selectors=cli[AgentRole.SELECTION],
    )
    registry = Registry()
    assert (
        build_classifier(bindings, registry, dependencies)
        is expected[AgentRole.CLASSIFICATION]
    )
    assert (
        build_attempt_runner(bindings, registry, dependencies)
        is expected[AgentRole.CODING]
    )
    assert (
        build_verifier(bindings, registry, dependencies)
        is expected[AgentRole.VERIFICATION]
    )
    assert (
        build_selector(bindings, registry, dependencies)
        is expected[AgentRole.SELECTION]
    )


def test_same_vendor_role_bindings_have_distinct_invocation_identities() -> None:
    systems = {role.value: _system("codex", role) for role in AgentRole}
    configuration = {
        "agent_systems": {
            "systems": {
                system.id: system.model_dump(mode="json") for system in systems.values()
            }
        },
        "execution_profiles": {
            "same-vendor": {role.value: systems[role.value].id for role in AgentRole}
        },
    }
    inspections = {
        system.id: AgentSystemInspection(
            system=system,
            status="ready",
            runnable=True,
            reason="fixture doctor passed",
        )
        for system in systems.values()
    }
    registry = RoleSystemRegistry(
        configuration,
        {},
        cli_inspections=inspections,
    )
    bindings = registry.resolve_profile("same-vendor")
    identities = registry.invocation_identities(bindings)
    assert len(identities) == 4
    assert len({item.invocation_id for item in identities}) == 4
    assert {item.role for item in identities} == set(AgentRole)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("driver_name", "command"), [("codex", "codex"), ("claude", "claude")]
)
def test_real_cli_classifier_selector_smoke_is_opt_in(
    tmp_path: Path, driver_name: str, command: str
) -> None:
    if os.environ.get("VILLANI_RUN_REAL_CLI_ROLE_SMOKE") != "1":
        pytest.skip(
            "set VILLANI_RUN_REAL_CLI_ROLE_SMOKE=1 to run real role smoke tests"
        )
    executable = shutil.which(command)
    if executable is None:
        pytest.skip(f"{driver_name} CLI is not installed")
    model_variable = (
        "VILLANI_REAL_CODEX_ROLE_MODEL"
        if driver_name == "codex"
        else "VILLANI_REAL_CLAUDE_ROLE_MODEL"
    )
    model = os.environ.get(model_variable)
    if not model:
        pytest.skip(f"set {model_variable} to select the real smoke-test model")

    def real_adapter(role: AgentRole):
        system = CliAgentSystemConfig(
            kind="cli_agent",
            id=f"real-{driver_name}-{role.value}-smoke",
            driver="codex" if driver_name == "codex" else "claude_code",
            executable=executable,
            model=model,
            roles={role},
            timeout_seconds=180,
            max_parallel=1,
            instruction_policy="villani_controlled",
            permission_profile="read_only",
            environment_policy="minimal",
            provider_options={"max_turns": 8},
        )
        driver = _driver(system)
        probe = driver.probe()
        if not probe.ready:
            pytest.skip("; ".join(probe.messages))
        return driver, probe

    classifier_driver, classifier_probe = real_adapter(AgentRole.CLASSIFICATION)
    classification_root = tmp_path / "classification"
    classification_root.mkdir()
    repository = _repository(classification_root)
    classification = CliClassifierAdapter(
        classifier_driver, probe=classifier_probe
    ).classify(
        "Explain the difficulty of updating target.py without editing it.",
        _classification_context(classification_root, repository),
    )
    assert classification.metadata["classification_fallback"] is False
    assert classification.metadata["cli_classifier_process_spawned"] is True

    selector_driver, selector_probe = real_adapter(AgentRole.SELECTION)
    selection_root = tmp_path / "selection"
    selection_root.mkdir()
    _repository_value, context, candidates = _selection_case(selection_root)
    selection = CliSelectorAdapter(selector_driver, probe=selector_probe).select(
        candidates, context
    )
    assert selection.selected_attempt_id in {"attempt_alpha", "attempt_beta"}
    assert selection.metadata["cli_selector_invoked"] is True
    assert selection.metadata["cli_selector_fallback"] is False
