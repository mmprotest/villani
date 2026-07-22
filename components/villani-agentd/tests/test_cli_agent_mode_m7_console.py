from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from villani_agentd.config import AgentdPaths, Limits
from villani_agentd.console import ConsoleService
from villani_agentd.spool import SQLiteSpool
from villani_ops.closed_loop.claude_code_cli.driver import ClaudeCodeCliDriver
from villani_ops.closed_loop.claude_code_cli.models import ClaudeProbeResult
from villani_ops.closed_loop.codex_cli.driver import CodexCliDriver
from villani_ops.closed_loop.codex_cli.models import CodexFailure, CodexProbeResult
from villani_ops.closed_loop.interfaces import ClosedLoopRunResult


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
ROLES = ["classification", "coding", "verification", "selection"]


def _system(system_id: str, driver: str, *, state: str = "ready") -> dict[str, Any]:
    return {
        "kind": "cli_agent",
        "id": system_id,
        "enabled": True,
        "driver": driver,
        "executable": sys.executable,
        "model": f"configured-{system_id}-model",
        "roles": ROLES,
        "timeout_seconds": 45,
        "max_parallel": 1,
        "instruction_policy": "native_project",
        "permission_profile": "workspace_write",
        "environment_policy": "minimal",
        "role_policies": {
            role: {
                "instruction_policy": "native_project"
                if role == "coding"
                else "villani_controlled",
                "permission_profile": "workspace_write" if role == "coding" else "read_only",
                "environment_policy": "minimal",
            }
            for role in ROLES
        },
        "provider_options": {"fixture_state": state},
    }


def _configuration(*, active: str = "cli", unavailable: bool = False) -> dict[str, Any]:
    codex = _system("codex-cli", "codex", state="unsupported" if unavailable else "ready")
    claude = _system("claude-cli", "claude_code")
    return {
        "config_version": 1,
        "backends": {},
        "agent_systems": {
            "schema_version": "villani.agent_system_configuration.v1",
            "systems": {"codex-cli": codex, "claude-cli": claude},
        },
        "execution_profiles": {
            "cli": {
                "schema_version": "villani.role_bindings.v1",
                "profile_id": "cli",
                "profile_type": "cli",
                "bindings": {role: "codex-cli" for role in ROLES},
            },
            "hybrid": {
                "schema_version": "villani.role_bindings.v1",
                "profile_id": "hybrid",
                "profile_type": "hybrid",
                "bindings": {
                    "classification": "codex-cli",
                    "coding": "codex-cli",
                    "verification": "claude-cli",
                    "selection": "claude-cli",
                },
            },
        },
        "active_execution_profile": active,
        "policy": {"version": "bootstrap_v1"},
        "budgets": {"max_attempts": 1, "max_cost": None},
    }


def _state(system: Any) -> str:
    return str(system.provider_options.get("fixture_state") or "ready")


def _codex_probe(system: Any) -> CodexProbeResult:
    unsupported = _state(system) == "unsupported"
    return CodexProbeResult(
        system_id=system.id,
        checked_at=NOW,
        configured_executable=system.executable,
        resolved_executable=str(Path(sys.executable).resolve()),
        exact_version_output="codex-cli 9.9.9-console-fixture",
        authentication_ready=True,
        authentication_method="chatgpt",
        capabilities={
            name: not unsupported
            for name in (
                "exec",
                "jsonl_output",
                "model_selection",
                "workspace_selection",
                "sandbox_selection",
                "read_only_sandbox",
                "schema_output",
                "last_message_output",
                "ephemeral",
                "noninteractive_approval",
                "ignore_user_config",
                "ignore_project_rules",
                "strict_config",
                "config_override",
                "scoped_permission_profiles",
            )
        },
        ready=not unsupported,
        failures=[CodexFailure.UNSUPPORTED_REQUIRED_FLAG] if unsupported else [],
        messages=["fixture capability is unsupported"] if unsupported else [],
    )


def _claude_probe(system: Any) -> ClaudeProbeResult:
    return ClaudeProbeResult(
        system_id=system.id,
        checked_at=NOW,
        configured_executable=system.executable,
        resolved_executable=str(Path(sys.executable).resolve()),
        exact_version_output="2.9.9 (Claude Code console fixture)",
        parsed_version="2.9.9",
        authentication_ready=True,
        authentication_method="claude_ai",
        doctor_ready=True,
        capabilities={
            name: True
            for name in (
                "print_mode",
                "stream_json",
                "structured_output",
                "no_session_persistence",
                "model_selection",
                "permission_mode",
                "read_only_permission_mode",
                "tools",
                "allowed_tools",
                "verbose",
                "no_chrome",
                "bare",
                "settings",
                "setting_sources",
                "strict_mcp_config",
                "mcp_config",
                "disable_slash_commands",
                "stdin_prompt",
                "max_turns",
            )
        },
        resolved_flags={"print": "-p"},
        ready=True,
        failures=[],
        messages=[],
    )


@pytest.fixture(autouse=True)
def probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CodexCliDriver, "probe", lambda self: _codex_probe(self.system))
    monkeypatch.setattr(ClaudeCodeCliDriver, "probe", lambda self: _claude_probe(self.system))


@pytest.fixture
def console(tmp_path: Path) -> tuple[ConsoleService, Path]:
    paths = AgentdPaths(tmp_path / "home" / "agentd")
    service = ConsoleService(paths, SQLiteSpool(paths, Limits()))
    home = paths.root.parent
    (home / "config.yaml").write_text(
        yaml.safe_dump(_configuration(), sort_keys=False), encoding="utf-8"
    )
    return service, home


def _git_repository(path: Path) -> Path:
    path.mkdir()
    for arguments in (
        ("init", "-q"),
        ("config", "user.email", "tests@example.invalid"),
        ("config", "user.name", "Villani tests"),
    ):
        subprocess.run(["git", *arguments], cwd=path, check=True)
    (path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    subprocess.run(["git", "add", "package.json"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=path, check=True)
    return path


def test_agent_system_payload_is_role_aware_and_schema_stable(console) -> None:
    service, home = console
    document = service.agent_systems()
    assert document["schema_version"] == "villani.console.agent_systems.v1"
    assert document["active_profile"] == "cli"
    codex = next(item for item in document["agent_systems"] if item["id"] == "codex-cli")
    assert codex["status"] == "READY"
    assert codex["exact_version"] == "codex-cli 9.9.9-console-fixture"
    assert [item["label"] for item in codex["role_badges"]] == [
        "Understand task",
        "Write code",
        "Verify result",
        "Choose candidate",
    ]
    assert codex["repository_modified"] is False
    assert (home / codex["evidence_path"]).is_file()


def test_profile_activation_and_ui_role_binding_are_persisted(console) -> None:
    service, home = console
    activated = service.profile_activate({"profile_id": "hybrid"})
    assert activated["active_profile"] == "hybrid"
    changed = service.profile_set_role(
        {
            "profile_id": "hybrid",
            "role": "verification",
            "agent_system_id": "codex-cli",
        }
    )
    hybrid = next(item for item in changed["profiles"] if item["profile_id"] == "hybrid")
    verification = next(item for item in hybrid["role_bindings"] if item["role"] == "verification")
    assert verification["agent_system_id"] == "codex-cli"
    persisted = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert persisted["active_execution_profile"] == "hybrid"
    assert persisted["execution_profiles"]["hybrid"]["bindings"]["verification"] == "codex-cli"


def test_settings_and_run_options_expose_profiles_without_quota_fields(console) -> None:
    service, _home = console
    settings = service.settings()
    options = service.run_options()
    assert settings["active_execution_profile"] == "cli"
    assert settings["role_bindings"]
    assert options["defaults"]["execution_profile"] == "cli"
    assert {item["id"] for item in options["execution_profiles"]} == {"cli", "hybrid"}
    encoded = json.dumps({"settings": settings, "options": options}).casefold()
    assert "quota" not in encoded


def test_unavailable_profile_fails_before_run_thread_and_never_falls_back(
    console, tmp_path: Path
) -> None:
    service, home = console
    (home / "config.yaml").write_text(
        yaml.safe_dump(_configuration(unavailable=True), sort_keys=False),
        encoding="utf-8",
    )
    repository = _git_repository(tmp_path / "repo-unavailable")
    response = service.start_run(
        {"repository": str(repository), "task": "Update the fixture safely."}
    )
    assert response["status"] == "FAILED"
    assert response["run_id"] is None
    assert response["failure"]["code"] == "no_usable_agent"
    assert "profile 'cli' is unavailable" in response["failure"]["what_failed"]
    assert service._run_threads == {}
    assert service._pending_runs == {}


def test_one_run_profile_override_is_snapshotted_into_controller_configuration(
    console, tmp_path: Path
) -> None:
    _original, home = console
    repository = _git_repository(tmp_path / "repo-override")
    captured: list[dict[str, Any]] = []
    started = threading.Event()

    class CapturingController:
        def run(self, request: Any) -> ClosedLoopRunResult:
            captured.append(dict(request.policy_configuration))
            started.set()
            return ClosedLoopRunResult(
                run_id=request.run_id,
                terminal_state="FAILED",
                selected_attempt_id=None,
                run_directory=Path(request.runs_root) / request.run_id,
                actual_known_cost_usd=None,
                accounting_status="unknown",
                failure_or_exhaustion_reason="fixture stopped after snapshot",
            )

    paths = AgentdPaths(home / "agentd")
    service = ConsoleService(
        paths,
        SQLiteSpool(paths, Limits()),
        controller_builder=lambda configuration, _events: CapturingController(),
    )
    response = service.start_run(
        {
            "repository": str(repository),
            "task": "Update the fixture safely.",
            "execution_profile": "hybrid",
        }
    )
    assert response["status"] == "QUEUED"
    assert started.wait(5)
    assert captured[0]["active_execution_profile"] == "hybrid"
    assert captured[0]["execution_profiles"]["hybrid"]["bindings"] == {
        "classification": "codex-cli",
        "coding": "codex-cli",
        "verification": "claude-cli",
        "selection": "claude-cli",
    }
    persisted = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert persisted["active_execution_profile"] == "cli"
