from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from villani_distribution import cli
from villani_distribution.onboarding import (
    build_cli_configuration,
    load_configuration,
    validate_configuration,
)
from villani_distribution.services import ServiceStatus
from villani_ops.closed_loop.agent_systems.management import (
    AgentSystemDiagnostic,
    AgentSystemManagementDocument,
    CliModelValidation,
    DoctorStatus,
)
from villani_ops.closed_loop.agent_systems.role_models import AgentRole
from villani_ops.closed_loop.agent_systems.role_registry import RoleSystemRegistry


def _diagnostic(driver: str, *, ready: bool) -> AgentSystemDiagnostic:
    system_id = "detected-codex" if driver == "codex" else "detected-claude-code"
    return AgentSystemDiagnostic(
        system_id=system_id,
        display_name="Codex CLI" if driver == "codex" else "Claude Code",
        driver=driver,
        configured=False,
        status=DoctorStatus.READY if ready else DoctorStatus.ACTION_REQUIRED,
        configured_executable="codex" if driver == "codex" else "claude",
        resolved_path=f"/fixtures/{driver}",
        safe_display_path=f"/fixtures/{driver}",
        resolved_path_digest="sha256:" + ("1" if driver == "codex" else "2") * 64,
        exact_version="9.9.9 fixture" if ready else None,
        authentication_ready=ready,
        authentication_status="ready" if ready else "not_ready",
        supported_roles=list(AgentRole) if ready else [],
        configured_roles=list(AgentRole),
        configured_model=None,
        instruction_policy="native_project",
        permission_policy="workspace_write",
        conformance_status="passed" if ready else "action_required",
        last_doctor_time=datetime(2026, 7, 22, tzinfo=timezone.utc),
        affected_roles=[] if ready else list(AgentRole),
        what_failed=None if ready else f"{driver} is not installed",
        exact_next_action=(
            f"villani agents doctor {system_id}"
            if ready
            else "npm install -g @anthropic-ai/claude-code"
        ),
        evidence_path="diagnostics/agent-systems/setup-detect.json",
        role_results=[],
    )


class _SetupRegistry:
    def __init__(self, configuration: dict[str, object], backends: dict[str, object]):
        self._registry = RoleSystemRegistry(configuration, backends)

    def resolve_profile(self, profile_id: str):
        return self._registry.resolve_profile(profile_id)

    def require_profile_runnable(self, _bindings: object) -> None:
        return None

    def list_configured(self):
        return self._registry.list_configured()


def _stopped(home: Path) -> ServiceStatus:
    return ServiceStatus(
        "win32",
        False,
        str(home / "service" / "windows-task.json"),
        False,
        running=False,
        log_path=str(home / "agentd" / "agentd.log"),
    )


def test_cli_configuration_uses_explicit_model_and_complete_role_bindings() -> None:
    configuration = build_cli_configuration(
        repository=None,
        codex_model="user-entered-codex-model",
    )
    assert configuration["active_execution_profile"] == "cli"
    profile = configuration["execution_profiles"]["cli"]
    assert profile["profile_type"] == "cli"
    assert profile["bindings"] == {role.value: "codex-cli" for role in AgentRole}
    system = configuration["agent_systems"]["systems"]["codex-cli"]
    assert system["model"] == "user-entered-codex-model"
    assert system["role_policies"]["coding"]["permission_profile"] == "workspace_write"
    assert system["role_policies"]["verification"]["permission_profile"] == "read_only"
    assert validate_configuration(configuration) == {}


def test_simple_cli_setup_needs_no_yaml_and_records_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VILLANI_HOME", str(home))
    monkeypatch.setattr(cli, "detect_repository", lambda: None)
    monkeypatch.setattr(cli, "detect_session_sources", lambda: ())
    monkeypatch.setattr(cli, "service_status", lambda: _stopped(home))
    monkeypatch.setattr(
        cli,
        "detect_cli_agent_systems",
        lambda **_kwargs: AgentSystemManagementDocument(
            generated_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
            systems=[
                _diagnostic("codex", ready=True),
                _diagnostic("claude_code", ready=False),
            ],
        ),
    )
    monkeypatch.setattr(
        cli,
        "build_agent_system_registry",
        lambda configuration, backends: _SetupRegistry(configuration, backends),
    )
    monkeypatch.setattr(
        cli,
        "validate_cli_model",
        lambda system, **_kwargs: CliModelValidation(
            system_id=system.id,
            configured_model=system.model,
            status="PASS",
            process_spawned=True,
            structured_output_valid=True,
            reason="fixture model probe passed",
            exact_next_action=f"villani agents doctor {system.id}",
            evidence_path="diagnostics/agent-systems/model-probes/fixture.json",
        ),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "setup",
            "--yes",
            "--execution-mode",
            "cli",
            "--codex-model",
            "user-entered-codex-model",
            "--no-start",
            "--no-open",
            "--no-sample",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Active execution profile: cli" in result.output
    assert "Write code: codex-cli" in result.output
    configuration = load_configuration(home / "config.yaml")
    assert configuration["active_execution_profile"] == "cli"
    assert configuration["backends"] == {}
    record = json.loads((home / "setup-record.json").read_text(encoding="utf-8"))
    assert record["model_validations"][0]["status"] == "PASS"
    encoded = json.dumps(record).casefold()
    assert "api_key" not in encoded
    assert "secret" not in encoded
    assert "quota" not in encoded
