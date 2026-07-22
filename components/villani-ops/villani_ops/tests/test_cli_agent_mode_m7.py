from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from villani_ops.cli import unified
from villani_ops.closed_loop.agent_systems.configuration import (
    migrate_agent_system_configuration,
)
from villani_ops.closed_loop.agent_systems.management import (
    DoctorStatus,
    detect_cli_agent_systems,
    diagnose_registry,
    validate_cli_model,
)
from villani_ops.closed_loop.agent_systems.registry import build_agent_system_registry
from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    CliAgentSystemConfig,
)
from villani_ops.closed_loop.agent_systems.role_registry import (
    RoleBindingConfigurationError,
)
from villani_ops.closed_loop.claude_code_cli.driver import ClaudeCodeCliDriver
from villani_ops.closed_loop.claude_code_cli.models import (
    ClaudeFailure,
    ClaudeProbeResult,
)
from villani_ops.closed_loop.codex_cli.driver import CodexCliDriver
from villani_ops.closed_loop.codex_cli.models import CodexFailure, CodexProbeResult
from villani_ops.closed_loop.invocation_evidence import collect_role_invocations
from villani_ops.closed_loop.schema_validation import validate_protocol_document
from villani_ops.core.backend import Backend


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
ROLES = [role.value for role in AgentRole]
FAKE_CODEX_ROLES = (
    Path(__file__).resolve().parent / "fixtures" / "cli_roles" / "fake_codex_roles.py"
)


def _cli_system(
    system_id: str,
    driver: str,
    *,
    roles: list[str] | None = None,
    state: str = "ready",
    model: str | None = None,
) -> dict[str, Any]:
    selected_roles = roles or ROLES
    return {
        "kind": "cli_agent",
        "id": system_id,
        "enabled": True,
        "driver": driver,
        "executable": sys.executable,
        "model": model or f"user-selected-{driver}-model",
        "roles": selected_roles,
        "timeout_seconds": 30,
        "max_parallel": 1,
        "instruction_policy": "native_project"
        if "coding" in selected_roles
        else "villani_controlled",
        "permission_profile": "workspace_write"
        if "coding" in selected_roles
        else "read_only",
        "environment_policy": "minimal",
        "role_policies": {
            role: {
                "instruction_policy": "native_project"
                if role == "coding"
                else "villani_controlled",
                "permission_profile": "workspace_write"
                if role == "coding"
                else "read_only",
                "environment_policy": "minimal",
            }
            for role in selected_roles
        },
        "provider_options": {"fixture_state": state},
    }


def _configuration(
    systems: dict[str, dict[str, Any]],
    bindings: dict[str, str] | None = None,
    *,
    profile_id: str = "cli",
    profile_type: str = "cli",
) -> dict[str, Any]:
    selected = bindings or {role: next(iter(systems)) for role in ROLES}
    return {
        "config_version": 1,
        "backends": {},
        "agent_systems": {
            "schema_version": "villani.agent_system_configuration.v1",
            "systems": systems,
        },
        "execution_profiles": {
            profile_id: {
                "schema_version": "villani.role_bindings.v1",
                "profile_id": profile_id,
                "profile_type": profile_type,
                "bindings": selected,
            }
        },
        "active_execution_profile": profile_id,
    }


def _codex_probe(system: Any) -> CodexProbeResult:
    state = (
        "missing"
        if system.id.startswith("detected-")
        else str(system.provider_options.get("fixture_state") or "ready")
    )
    role = next(iter(system.roles))
    missing = state == "missing"
    auth_missing = state == "auth_missing"
    unsupported = state == "unsupported" or (
        state == "selection_unsupported" and role == AgentRole.SELECTION
    )
    failures = (
        [CodexFailure.NOT_INSTALLED]
        if missing
        else [CodexFailure.NOT_AUTHENTICATED]
        if auth_missing
        else [CodexFailure.UNSUPPORTED_REQUIRED_FLAG]
        if unsupported
        else []
    )
    return CodexProbeResult(
        system_id=system.id,
        checked_at=NOW,
        configured_executable=system.executable,
        resolved_executable=None if missing else str(Path(sys.executable).resolve()),
        exact_version_output=None if missing else "codex-cli 9.9.9-m7-fixture",
        authentication_ready=not missing and not auth_missing,
        authentication_method=(
            "not_authenticated" if auth_missing else "unknown" if missing else "chatgpt"
        ),
        capabilities={
            "exec": not unsupported,
            "jsonl_output": not unsupported,
            "model_selection": not unsupported,
            "workspace_selection": not unsupported,
            "sandbox_selection": not unsupported,
            "read_only_sandbox": not unsupported,
            "schema_output": not unsupported,
            "last_message_output": not unsupported,
            "ephemeral": not unsupported,
            "noninteractive_approval": not unsupported,
            "ignore_user_config": not unsupported,
            "ignore_project_rules": not unsupported,
            "strict_config": not unsupported,
            "config_override": not unsupported,
            "scoped_permission_profiles": not unsupported,
        },
        ready=not failures,
        failures=failures,
        messages=[failure.value for failure in failures],
    )


def _claude_probe(system: Any) -> ClaudeProbeResult:
    state = (
        "missing"
        if system.id.startswith("detected-")
        else str(system.provider_options.get("fixture_state") or "ready")
    )
    missing = state == "missing"
    auth_missing = state == "auth_missing"
    unsupported = state == "unsupported"
    failures = (
        [ClaudeFailure.NOT_INSTALLED]
        if missing
        else [ClaudeFailure.NOT_AUTHENTICATED]
        if auth_missing
        else [ClaudeFailure.UNSUPPORTED_REQUIRED_CAPABILITY]
        if unsupported
        else []
    )
    return ClaudeProbeResult(
        system_id=system.id,
        checked_at=NOW,
        configured_executable=system.executable,
        resolved_executable=None if missing else str(Path(sys.executable).resolve()),
        exact_version_output=None if missing else "2.9.9 (Claude Code m7 fixture)",
        parsed_version=None if missing else "2.9.9",
        authentication_ready=not missing and not auth_missing,
        authentication_method=(
            "not_authenticated"
            if auth_missing
            else "unknown"
            if missing
            else "claude_ai"
        ),
        doctor_ready=not missing and not unsupported,
        capabilities={
            "print_mode": not unsupported,
            "stream_json": not unsupported,
            "structured_output": not unsupported,
            "no_session_persistence": not unsupported,
            "model_selection": not unsupported,
            "permission_mode": not unsupported,
            "read_only_permission_mode": not unsupported,
            "tools": not unsupported,
            "allowed_tools": not unsupported,
            "verbose": not unsupported,
            "no_chrome": not unsupported,
            "bare": not unsupported,
            "settings": not unsupported,
            "setting_sources": not unsupported,
            "strict_mcp_config": not unsupported,
            "mcp_config": not unsupported,
            "disable_slash_commands": not unsupported,
            "stdin_prompt": not unsupported,
            "max_turns": not unsupported,
        },
        resolved_flags={"print": "-p"},
        ready=not failures,
        failures=failures,
        messages=[failure.value for failure in failures],
    )


@pytest.fixture(autouse=True)
def deterministic_cli_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CodexCliDriver, "probe", lambda self: _codex_probe(self.system))
    monkeypatch.setattr(
        ClaudeCodeCliDriver, "probe", lambda self: _claude_probe(self.system)
    )


@pytest.mark.parametrize(
    ("systems", "expected_ready"),
    [
        ({}, set()),
        ({"codex-cli": _cli_system("codex-cli", "codex")}, {"codex-cli"}),
        ({"claude-cli": _cli_system("claude-cli", "claude_code")}, {"claude-cli"}),
        (
            {
                "codex-cli": _cli_system("codex-cli", "codex"),
                "claude-cli": _cli_system("claude-cli", "claude_code"),
            },
            {"codex-cli", "claude-cli"},
        ),
    ],
)
def test_detection_none_one_or_both_cli_systems(
    systems: dict[str, dict[str, Any]], expected_ready: set[str]
) -> None:
    configuration = _configuration(systems) if systems else None
    document = detect_cli_agent_systems(
        configuration,
        evidence_path="diagnostics/agent-systems/detect.json",
    )
    ready = {
        item.system_id
        for item in document.systems
        if item.configured and item.status == DoctorStatus.READY
    }
    assert ready == expected_ready
    assert document.repositories_modified is False
    assert document.secrets_read is False
    assert document.login_started is False
    assert document.provider_configuration_modified is False


@pytest.mark.parametrize(
    ("state", "status", "action"),
    [
        ("auth_missing", DoctorStatus.ACTION_REQUIRED, "codex login"),
        (
            "unsupported",
            DoctorStatus.UNSUPPORTED,
            "npm install -g @openai/codex@latest",
        ),
    ],
)
def test_doctor_is_actionable_for_auth_and_unsupported_versions(
    state: str, status: DoctorStatus, action: str
) -> None:
    configuration = _configuration(
        {"codex-cli": _cli_system("codex-cli", "codex", state=state)}
    )
    registry = build_agent_system_registry(configuration, {})
    diagnostic = diagnose_registry(
        registry, evidence_path="diagnostics/agent-systems/doctor.json"
    ).systems[0]
    assert diagnostic.status == status
    assert diagnostic.exact_next_action == action
    assert diagnostic.repository_modified is False
    assert diagnostic.affected_roles


def test_configured_model_is_proved_by_bounded_read_only_invocation(
    tmp_path: Path,
) -> None:
    document = _cli_system(
        "codex-model-probe", "codex", roles=[AgentRole.CLASSIFICATION.value]
    )
    document["provider_options"] = {
        "launcher_arguments": [str(FAKE_CODEX_ROLES)],
        "graceful_shutdown_seconds": 0.1,
    }
    system = CliAgentSystemConfig.model_validate(document)

    result = validate_cli_model(system, evidence_root=tmp_path / "diagnostics")

    assert result.status == "PASS"
    assert result.process_spawned is True
    assert result.structured_output_valid is True
    assert result.repository_modified is False
    summary = Path(result.evidence_path)
    assert summary.is_file()
    invocation = json.loads(
        next(summary.parent.rglob("agent/invocation.json")).read_text(encoding="utf-8")
    )
    arguments = invocation["arguments"]
    assert arguments[arguments.index("--model") + 1] == system.model
    assert "--sandbox" not in arguments
    assert list((summary.parent / "original-repository").iterdir()) == []


def test_one_unsupported_role_blocks_profile_without_fallback() -> None:
    configuration = _configuration(
        {"codex-cli": _cli_system("codex-cli", "codex", state="selection_unsupported")}
    )
    registry = build_agent_system_registry(configuration, {})
    status = registry.profile_status("cli")
    assert status.runnable is False
    assert any(reason.startswith("selection:") for reason in status.reasons)
    with pytest.raises(RoleBindingConfigurationError, match="will not fall back"):
        registry.require_profile_runnable(registry.resolve_profile("cli"))


def test_hybrid_role_assignment_and_exact_invocation_snapshot() -> None:
    cli = _cli_system("claude-cli", "claude_code", roles=["verification", "selection"])
    api_backend = Backend(
        name="api-main",
        provider="openai-compatible",
        base_url="http://127.0.0.1:11434/v1",
        model="api-user-model",
        roles=["classification", "coding"],
    )
    api_system = {
        "kind": "api",
        "id": "api-main-system",
        "enabled": True,
        "provider": "openai-compatible",
        "model": "api-user-model",
        "roles": ["classification", "coding"],
        "existing_backend_reference": "api-main",
        "timeout_seconds": 30,
        "max_parallel": 1,
        "metadata": {},
    }
    bindings = {
        "classification": "api-main-system",
        "coding": "api-main-system",
        "verification": "claude-cli",
        "selection": "claude-cli",
    }
    configuration = _configuration(
        {"api-main-system": api_system, "claude-cli": cli},
        bindings,
        profile_id="hybrid",
        profile_type="hybrid",
    )
    registry = build_agent_system_registry(configuration, {"api-main": api_backend})
    resolved = registry.resolve_profile("hybrid")
    registry.require_profile_runnable(resolved)
    identities = {
        role.value: registry.role_registry.invocation_identity(resolved, role)
        for role in AgentRole
    }
    assert {
        role: identity.agent_system_id for role, identity in identities.items()
    } == bindings
    assert identities["verification"].driver == "claude_code"
    assert identities["verification"].model == "user-selected-claude_code-model"
    assert len({identity.invocation_id for identity in identities.values()}) == 4


def test_existing_api_migration_is_default_and_idempotent() -> None:
    legacy = {
        "config_version": 1,
        "backends": {
            "api-main": {
                "provider": "openai-compatible",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "existing-user-model",
                "roles": ["classification", "coding", "review", "selection"],
            }
        },
    }
    first, first_report = migrate_agent_system_configuration(legacy)
    second, second_report = migrate_agent_system_configuration(first)
    assert first["active_execution_profile"] == "api"
    assert first["execution_profiles"]["api"] == second["execution_profiles"]["api"]
    assert first["backends"] == legacy["backends"]
    assert first_report["destructive_changes"] is False
    assert second_report["destructive_changes"] is False
    assert "api_key" not in json.dumps(first["agent_systems"])


def _write_role_process(run: Path) -> None:
    agent = run / "attempts" / "attempt-1" / "agent"
    agent.mkdir(parents=True)
    stream = {
        "artifact_path": "stream.log",
        "total_bytes_observed": 0,
        "bytes_persisted": 0,
        "limit_exceeded": False,
        "largest_read_bytes": 0,
        "decode_replacements": False,
        "output_after_cancellation": False,
    }
    invocation = {
        "schema_version": "villani.cli_invocation.v1",
        "executable": str(Path(sys.executable).resolve()),
        "executable_identity": {"status": "resolved", "sha256": None},
        "arguments": ["exec", "--json"],
        "environment": [{"name": "PATH", "provenance": "inherited", "redacted": False}],
        "role_workspace_identity": {
            "role": "coding",
            "agent_system_id": "codex-cli",
            "driver": "codex",
            "configured_model": "user-model",
            "cli_version": "codex-cli 9.9.9",
            "instruction_policy": "native_project",
            "permission_policy": "workspace_write",
        },
        "target_repository_writable": True,
        "cwd": str(agent.parent),
        "stdin": {
            "provided": True,
            "size_bytes": 7,
            "artifact_reference": "prompt.txt",
            "sha256": "sha256:" + "0" * 64,
        },
        "timeout_seconds": 30.0,
        "graceful_shutdown_seconds": 1.0,
        "limits": {
            "maximum_stdout_bytes": 1024,
            "maximum_stderr_bytes": 1024,
            "maximum_stdout_chunk_bytes": 1024,
            "maximum_stderr_chunk_bytes": 1024,
            "maximum_event_line_bytes": 1024,
            "maximum_tail_bytes": 1024,
            "read_chunk_bytes": 1024,
        },
        "event_stream_format": "jsonl",
        "utf8_policy": "replacement",
        "final_output_path": None,
        "require_final_output": False,
        "started_at": "2026-07-22T12:00:00Z",
    }
    process = {
        "schema_version": "villani.cli_process_result.v1",
        "infrastructure_state": "succeeded",
        "failure": None,
        "failures": [],
        "started_at": "2026-07-22T12:00:00Z",
        "completed_at": "2026-07-22T12:00:01Z",
        "duration_ms": 1000,
        "pid": 4242,
        "exit_code": 0,
        "timed_out": False,
        "cancelled": False,
        "cancellation_origin": None,
        "termination_reason": None,
        "graceful_termination_requested": False,
        "graceful_termination_succeeded": False,
        "forced_termination": False,
        "cleanup_status": "not_required",
        "cleanup_error": None,
        "target_repository_writable": True,
        "stdin_bytes_delivered": 7,
        "stdout": stream,
        "stderr": stream,
        "raw_events": stream,
        "final_output_path": None,
        "final_output_present": None,
        "invocation_artifact": "invocation.json",
        "output_tail_artifact": "output-tail.json",
        "process_result_artifact": "process-result.json",
        "artifact_set_complete": True,
    }
    (agent / "invocation.json").write_text(json.dumps(invocation), encoding="utf-8")
    (agent / "process-result.json").write_text(json.dumps(process), encoding="utf-8")


def test_role_invocation_evidence_is_safe_strict_and_preserves_unknown_cost(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    _write_role_process(run)
    index = collect_role_invocations(
        run,
        [
            {
                "role": "coding",
                "agent_system_id": "codex-cli",
                "driver": "codex",
                "model": "user-model",
            }
        ],
    )
    assert len(index.invocations) == 1
    invocation = index.invocations[0]
    assert invocation.cost.value is None
    assert invocation.cost.accounting_status == "unknown"
    assert invocation.usage.accounting_status == "unknown"
    assert invocation.permission_policy == "workspace_write"
    public = invocation.model_dump(mode="json")
    encoded = json.dumps(public).casefold()
    assert "pid" not in public
    assert "provider" not in encoded
    assert "secret" not in encoded
    assert "quota" not in encoded
    validate_protocol_document(index.model_dump(mode="json"))


def test_diagnostic_and_role_labels_match_public_contract() -> None:
    configuration = _configuration({"codex-cli": _cli_system("codex-cli", "codex")})
    registry = build_agent_system_registry(configuration, {})
    diagnostic = diagnose_registry(
        registry, evidence_path="diagnostics/agent-systems/doctor.json"
    ).systems[0]
    assert [item.label for item in diagnostic.role_results] == [
        "Understand task",
        "Write code",
        "Verify result",
        "Choose candidate",
    ]
    assert diagnostic.status == DoctorStatus.READY
    assert diagnostic.exact_version == "codex-cli 9.9.9-m7-fixture"
    assert diagnostic.authentication_ready is True


def test_public_cli_detects_and_activates_profiles_without_yaml_editing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    configuration = _configuration(
        {
            "codex-cli": _cli_system("codex-cli", "codex"),
            "claude-cli": _cli_system("claude-cli", "claude_code"),
        },
        {
            "classification": "codex-cli",
            "coding": "codex-cli",
            "verification": "claude-cli",
            "selection": "claude-cli",
        },
        profile_id="hybrid",
        profile_type="hybrid",
    )
    configuration["execution_profiles"]["cli"] = {
        "schema_version": "villani.role_bindings.v1",
        "profile_id": "cli",
        "profile_type": "cli",
        "bindings": {role: "codex-cli" for role in ROLES},
    }
    (home / "config.yaml").write_text(
        yaml.safe_dump(configuration, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setenv("VILLANI_HOME", str(home))
    detected = CliRunner().invoke(unified.app, ["agents", "detect", "--json"])
    assert detected.exit_code == 0, detected.output
    detection = json.loads(detected.stdout)
    assert {item["driver"] for item in detection["systems"]} == {
        "codex",
        "claude_code",
    }
    activated = CliRunner().invoke(
        unified.app, ["profiles", "activate", "cli", "--json"]
    )
    assert activated.exit_code == 0, activated.output
    activation = json.loads(activated.stdout)
    assert activation["active_execution_profile"] == "cli"
    assert activation["fallback_used"] is False
    persisted = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert persisted["active_execution_profile"] == "cli"
