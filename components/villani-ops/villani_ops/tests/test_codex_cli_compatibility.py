from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from villani_ops.closed_loop.agent_systems.management import (
    DoctorStatus,
    diagnose_registry,
)
from villani_ops.closed_loop.agent_systems.registry import (
    build_agent_system_registry,
)
from villani_ops.closed_loop.agent_systems.role_models import (
    AgentRole,
    AgentSystemInspection,
    CliAgentSystemConfig,
    CliRolePolicy,
)
from villani_ops.closed_loop.agent_systems.role_registry import (
    RoleSystemRegistry,
)
from villani_ops.closed_loop.codex_cli.driver import (
    CodexCliDriver,
    CodexDriverUnavailable,
    run_coroutine_sync,
)
from villani_ops.closed_loop.codex_cli.models import (
    CodexFailure,
    CodexProbeResult,
)
from villani_ops.closed_loop.schema_validation import (
    PACKAGED_SCHEMA_ROOT,
    ROOT_SCHEMA_ROOT,
)


HERE = Path(__file__).resolve().parent
FAKE_CODEX_0144 = HERE / "fixtures" / "codex_cli" / "fake_codex_0144.py"
ALL_ROLES = set(AgentRole)
FORBIDDEN_PRODUCTION_MARKERS = (
    "VillaniCodexCalibrationSmoke",
    "gpt-5.6-sol",
    "gpt-5.6-luna",
    "--dangerously-bypass-approvals-and-sandbox",
    "--yolo",
)


def _policy(role: AgentRole) -> CliRolePolicy:
    return CliRolePolicy(
        instruction_policy=(
            "native_project" if role == AgentRole.CODING else "villani_controlled"
        ),
        permission_profile=(
            "workspace_write" if role == AgentRole.CODING else "read_only"
        ),
        environment_policy="minimal",
    )


def _system(
    *,
    roles: set[AgentRole] | None = None,
    config_mode: str = "accept",
    global_mode: str = "accept",
    help_mode: str = "plain",
    auth_mode: str = "ready",
    omit: str = "none",
    probe_timeout_seconds: float = 2.0,
    reasoning_effort: str | None = None,
    marker: Path | None = None,
    executable: str | None = None,
    launcher_arguments: list[str] | None = None,
    system_id: str = "codex-0144-fixture",
) -> CliAgentSystemConfig:
    advertised = roles or {AgentRole.CODING}
    arguments = (
        list(launcher_arguments)
        if launcher_arguments is not None
        else [
            str(FAKE_CODEX_0144),
            "--fixture-config",
            config_mode,
            "--fixture-global",
            global_mode,
            "--fixture-help",
            help_mode,
            "--fixture-auth",
            auth_mode,
            "--fixture-omit",
            omit,
        ]
    )
    if marker is not None:
        arguments.extend(("--fixture-model-marker", str(marker)))
    provider_options: dict[str, Any] = {
        "launcher_arguments": arguments,
        "graceful_shutdown_seconds": 0.1,
        "probe_timeout_seconds": probe_timeout_seconds,
    }
    if reasoning_effort is not None:
        provider_options["reasoning_effort"] = reasoning_effort
    default_role = (
        AgentRole.CODING
        if AgentRole.CODING in advertised
        else min(advertised, key=lambda role: role.value)
    )
    default_policy = _policy(default_role)
    return CliAgentSystemConfig(
        kind="cli_agent",
        id=system_id,
        enabled=True,
        driver="codex",
        executable=executable or sys.executable,
        model="fixture-codex-model",
        roles=advertised,
        timeout_seconds=5,
        max_parallel=4,
        instruction_policy=default_policy.instruction_policy,
        permission_profile=default_policy.permission_profile,
        environment_policy=default_policy.environment_policy,
        role_policies={role: _policy(role) for role in advertised},
        provider_options=provider_options,
    )


def _configuration(
    system: CliAgentSystemConfig, *, bind_all_roles: bool = False
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "config_version": 1,
        "backends": {},
        "agent_systems": {
            "schema_version": "villani.agent_system_configuration.v1",
            "systems": {
                system.id: system.model_dump(mode="json"),
            },
        },
    }
    if bind_all_roles:
        document["execution_profiles"] = {
            "all-codex": {
                "schema_version": "villani.role_bindings.v1",
                "profile_id": "all-codex",
                "profile_type": "cli",
                "bindings": {role.value: system.id for role in AgentRole},
            }
        }
        document["active_execution_profile"] = "all-codex"
    return document


def _diagnostic(system: CliAgentSystemConfig):
    registry = build_agent_system_registry(_configuration(system), {})
    return diagnose_registry(
        registry,
        evidence_path="diagnostics/codex-0144-doctor.json",
        reference=system.id,
    ).systems[0]


def _build_invocation(
    driver: CodexCliDriver,
    probe: CodexProbeResult,
    role: AgentRole,
    root: Path,
):
    workspace = root / f"{role.value} workspace with spaces"
    artifacts = root / f"{role.value} artifacts"
    workspace.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    schema = artifacts / f"{role.value}-schema.json"
    schema.write_text('{"type":"object"}\n', encoding="utf-8")
    final = artifacts / "final-output.json"
    prompt = f"fresh {role.value} prompt".encode("utf-8")
    common = {
        "probe": probe,
        "prompt_bytes": prompt,
        "prompt_reference": f"prompts/{role.value}.txt",
        "prompt_sha256": (f"sha256:{hashlib.sha256(prompt).hexdigest()}"),
        "output_schema_path": schema,
        "final_output_path": final,
    }
    if role == AgentRole.CODING:
        invocation = driver.build_invocation(
            worktree=workspace,
            agent_directory=artifacts,
            run_id="run_compatibility",
            attempt_id="attempt_coding",
            baseline_sha256="b" * 64,
            **common,
        )
    elif role == AgentRole.CLASSIFICATION:
        invocation = driver.build_classifier_invocation(
            workspace=workspace,
            artifact_directory=artifacts,
            classification_id="classification_invocation",
            **common,
        )
    elif role == AgentRole.VERIFICATION:
        invocation = driver.build_verifier_invocation(
            workspace=workspace,
            artifact_directory=artifacts,
            verification_id="verification_invocation",
            **common,
        )
    else:
        invocation = driver.build_selector_invocation(
            workspace=workspace,
            artifact_directory=artifacts,
            selection_id="selection_invocation",
            **common,
        )
    return invocation, workspace, artifacts, schema, prompt


def test_codex_0144_help_is_parsed_by_scope_and_prefers_strict_config() -> None:
    system = _system()
    probe = CodexCliDriver(system).probe()

    assert probe.ready is True
    assert probe.exact_version_output == "codex-cli 0.144.6"
    assert probe.authentication_method == "chatgpt"
    assert probe.approval_strategy == "config_override"
    assert probe.approval_policy == "never"
    assert probe.approval_probe_used_model is False
    assert probe.capabilities["unattended_safe_execution"] is True
    assert probe.capabilities["noninteractive_approval"] is True
    assert probe.capabilities["approval_policy_never_config_override"] is True
    assert probe.capabilities["global_approval_flag_advertised"] is True
    assert probe.capabilities["global_approval_flag_validated"] is True
    config_evidence = probe.approval_probe_evidence["config_override"]
    assert config_evidence["exit_code"] == 0
    assert config_evidence["used_model"] is False
    assert config_evidence["argv"] == [
        "-c",
        'approval_policy="never"',
        "--strict-config",
        "exec",
        "--help",
    ]
    assert "--ask-for-approval" not in str(config_evidence["stdout_excerpt"])


def test_fixture_rejects_unknown_strict_approval_configuration() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(FAKE_CODEX_0144),
            "-c",
            'approval_policy_typo="never"',
            "--strict-config",
            "exec",
            "--help",
        ],
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert completed.returncode != 0
    assert "unknown configuration key" in completed.stderr


def test_global_approval_fallback_is_strictly_validated() -> None:
    probe = CodexCliDriver(_system(config_mode="reject", global_mode="accept")).probe()

    assert probe.ready is True
    assert probe.approval_strategy == "global_flag"
    assert probe.capabilities["approval_policy_never_config_override"] is False
    assert probe.capabilities["global_approval_flag_validated"] is True
    assert probe.approval_probe_evidence["config_override"]["exit_code"] == 2
    assert probe.approval_probe_evidence["global_flag"]["exit_code"] == 0


def test_probe_fails_closed_when_neither_approval_strategy_is_valid() -> None:
    probe = CodexCliDriver(_system(config_mode="reject", global_mode="reject")).probe()

    assert probe.ready is False
    assert probe.approval_strategy is None
    assert probe.capabilities["unattended_safe_execution"] is False
    assert CodexFailure.UNSUPPORTED_REQUIRED_FLAG in probe.failures
    assert any("unattended_safe_execution" in message for message in probe.messages)


def test_approval_probe_timeout_is_bounded_and_fails_closed() -> None:
    started = time.monotonic()
    probe = CodexCliDriver(
        _system(
            config_mode="timeout",
            global_mode="reject",
            probe_timeout_seconds=0.1,
        )
    ).probe()
    elapsed = time.monotonic() - started

    assert elapsed < 3
    assert probe.ready is False
    assert probe.approval_strategy is None
    assert probe.approval_probe_evidence["config_override"]["timed_out"] is True
    assert probe.approval_probe_used_model is False


def test_ansi_coloured_crlf_help_output_parses_deterministically() -> None:
    probe = CodexCliDriver(_system(help_mode="ansi_crlf")).probe()

    assert probe.ready is True
    assert probe.approval_strategy == "config_override"
    assert probe.capabilities["jsonl_output"] is True
    assert probe.capabilities["read_only_sandbox"] is True


def test_config_strategy_builds_global_options_before_exec(
    tmp_path: Path,
) -> None:
    system = _system(reasoning_effort="medium")
    driver = CodexCliDriver(system)
    probe = driver.probe()
    invocation, workspace, _artifacts, schema, prompt = _build_invocation(
        driver, probe, AgentRole.CODING, tmp_path
    )
    arguments = list(invocation.arguments)
    exec_index = arguments.index("exec")

    assert arguments.index("-c") < exec_index
    assert 'approval_policy="never"' in arguments[:exec_index]
    assert 'model_reasoning_effort="medium"' in arguments[:exec_index]
    assert 'web_search="disabled"' in arguments[:exec_index]
    assert "--strict-config" in arguments[:exec_index]
    assert "--ask-for-approval" not in arguments
    assert arguments[arguments.index("--sandbox") + 1] == "workspace-write"
    assert arguments[arguments.index("--cd") + 1] == str(workspace.resolve())
    assert arguments[arguments.index("--output-schema") + 1] == str(schema.resolve())
    assert invocation.stdin_bytes == prompt
    assert arguments[-1] == "-"
    assert {
        "--ephemeral",
        "--json",
        "--model",
        "--output-schema",
        "--output-last-message",
        "--cd",
    } <= set(arguments)
    assert not {
        "--dangerously-bypass-approvals-and-sandbox",
        "--yolo",
        "--full-auto",
    }.intersection(arguments)


def test_global_flag_is_before_exec_and_never_added_as_exec_option(
    tmp_path: Path,
) -> None:
    system = _system(config_mode="reject", global_mode="accept")
    driver = CodexCliDriver(system)
    probe = driver.probe()
    invocation, *_ = _build_invocation(driver, probe, AgentRole.CODING, tmp_path)
    arguments = list(invocation.arguments)
    exec_index = arguments.index("exec")
    approval_index = arguments.index("--ask-for-approval")

    assert approval_index < exec_index
    assert arguments[approval_index + 1] == "never"
    assert "--ask-for-approval" not in arguments[exec_index + 1 :]


@pytest.mark.parametrize(
    "role",
    [
        AgentRole.CLASSIFICATION,
        AgentRole.VERIFICATION,
        AgentRole.SELECTION,
    ],
)
def test_non_coding_roles_use_fresh_scoped_read_only_invocations(
    tmp_path: Path, role: AgentRole
) -> None:
    system = _system(roles={role})
    driver = CodexCliDriver(system)
    probe = driver.probe()
    invocation, workspace, artifacts, schema, prompt = _build_invocation(
        driver, probe, role, tmp_path
    )
    arguments = list(invocation.arguments)
    identity = invocation.role_workspace_identity
    exec_index = arguments.index("exec")

    assert probe.ready is True
    assert identity["role"] == role.value
    assert identity["agent_system_id"] == system.id
    assert identity["sandbox"] == "read-only"
    assert identity["sandbox_enforcement"] == "scoped_permission_profile"
    assert identity["candidate_worktree_writable"] is False
    assert identity["network_access"] is False
    assert identity["session_resume"] is False
    assert "--skip-git-repo-check" in arguments
    assert arguments.index("--skip-git-repo-check") > exec_index
    assert invocation.target_repository_writable is False
    assert invocation.cwd == workspace.resolve()
    assert invocation.stdin_bytes == prompt
    assert arguments[arguments.index("--output-schema") + 1] == str(schema.resolve())
    assert arguments[arguments.index("--output-last-message") + 1] == str(
        (artifacts / "final-output.json").resolve()
    )
    assert "--sandbox" not in arguments
    assert "villani_verifier_read_only" in " ".join(arguments[:exec_index])
    assert "--ask-for-approval" not in arguments[exec_index + 1 :]


def test_one_multi_role_system_launches_four_independent_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tmp_path / "process records"
    records.mkdir()
    system = _system(
        roles=ALL_ROLES,
        marker=records,
        reasoning_effort="medium",
        system_id="codex-all-roles",
    )
    driver = CodexCliDriver(system)
    probe = driver.probe()
    assert list(records.iterdir()) == []
    monkeypatch.setenv(
        "VILLANI_COMPATIBILITY_SECRET",
        "compatibility-secret-must-not-be-recorded",
    )

    invocations = {
        role: _build_invocation(driver, probe, role, tmp_path / "roles")[0]
        for role in AgentRole
    }
    results = {
        role: run_coroutine_sync(driver.supervisor.run(invocation))
        for role, invocation in invocations.items()
    }

    assert all(
        result.infrastructure_state == "succeeded" for result in results.values()
    )
    pids = {result.pid for result in results.values()}
    assert None not in pids
    assert len(pids) == len(AgentRole)
    process_records = [
        json.loads(path.read_text(encoding="utf-8")) for path in records.glob("*.json")
    ]
    assert len({record["pid"] for record in process_records}) == len(AgentRole)
    assert {record["stdin"] for record in process_records} == {
        f"fresh {role.value} prompt" for role in AgentRole
    }
    assert len(
        {
            invocation.role_workspace_identity["role"]
            for invocation in invocations.values()
        }
    ) == len(AgentRole)
    assert len(
        {
            invocation.role_workspace_identity.get(
                "role_invocation_id",
                invocation.role_workspace_identity.get("attempt_id"),
            )
            for invocation in invocations.values()
        }
    ) == len(AgentRole)
    assert len(
        {
            tuple(invocation.arguments)[
                tuple(invocation.arguments).index("--output-schema") + 1
            ]
            for invocation in invocations.values()
        }
    ) == len(AgentRole)
    assert all(
        invocation.role_workspace_identity["agent_system_id"] == system.id
        for invocation in invocations.values()
    )
    assert (
        invocations[AgentRole.CODING].role_workspace_identity["sandbox"]
        == "workspace-write"
    )
    for role in (
        AgentRole.CLASSIFICATION,
        AgentRole.VERIFICATION,
        AgentRole.SELECTION,
    ):
        assert invocations[role].role_workspace_identity["sandbox"] == "read-only"
        assert invocations[role].role_workspace_identity["session_resume"] is False
    evidence_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert "compatibility-secret-must-not-be-recorded" not in evidence_text


def test_role_not_advertised_fails_before_invocation_construction(
    tmp_path: Path,
) -> None:
    system = _system(roles={AgentRole.CODING})
    driver = CodexCliDriver(system)
    probe = driver.probe()
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    workspace.mkdir()
    artifacts.mkdir()

    with pytest.raises(
        CodexDriverUnavailable,
        match="requires a system advertising verification",
    ):
        driver.build_verifier_invocation(
            probe=probe,
            workspace=workspace,
            artifact_directory=artifacts,
            prompt_bytes=b"verify",
            prompt_reference="prompt.txt",
            prompt_sha256=(f"sha256:{hashlib.sha256(b'verify').hexdigest()}"),
            output_schema_path=artifacts / "schema.json",
            final_output_path=artifacts / "final.json",
            verification_id="verification_not_advertised",
        )
    assert not (artifacts / "invocation.json").exists()


def test_profile_can_bind_same_multi_role_system_to_every_role() -> None:
    system = _system(roles=ALL_ROLES, system_id="shared-codex-system")
    configuration = _configuration(system, bind_all_roles=True)
    inspection = AgentSystemInspection(
        system=system,
        status="ready",
        runnable=True,
        reason="fixture ready",
    )
    registry = RoleSystemRegistry(
        configuration,
        {},
        cli_inspections={system.id: inspection},
    )
    bindings = registry.resolve_profile("all-codex")
    identities = registry.invocation_identities(bindings)

    assert {bindings.system_id_for(role) for role in AgentRole} == {system.id}
    assert {identity.agent_system_id for identity in identities} == {system.id}
    assert {identity.role for identity in identities} == ALL_ROLES
    assert len({identity.invocation_id for identity in identities}) == len(AgentRole)


def test_codex_0144_doctor_is_ready_without_upgrade_or_model_use(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "model was invoked.json"
    diagnostic = _diagnostic(_system(marker=marker))

    assert diagnostic.status == DoctorStatus.READY
    assert diagnostic.selectable is True
    assert diagnostic.supported_roles == [AgentRole.CODING]
    assert diagnostic.authentication_status == "ready"
    assert diagnostic.unattended_safe_execution is True
    assert diagnostic.approval_strategy == "config_override"
    assert diagnostic.exact_next_action == "No action required."
    assert "upgrade" not in diagnostic.exact_next_action.casefold()
    assert "npm install" not in diagnostic.exact_next_action.casefold()
    assert marker.exists() is False
    checks = {
        check.check_id: check
        for result in diagnostic.role_results
        for check in result.checks
    }
    assert checks["unattended_safe_execution"].status == "PASS"
    assert checks["unattended_safe_execution"].evidence["probe_used_model"] is False
    assert checks["safe_editing"].status == "PASS"


@pytest.mark.parametrize(
    ("system", "status", "action_fragment"),
    [
        (
            _system(auth_mode="missing"),
            DoctorStatus.ACTION_REQUIRED,
            "Run codex login.",
        ),
        (
            _system(omit="structured"),
            DoctorStatus.UNSUPPORTED,
            "codex exec --json",
        ),
        (
            _system(omit="sandbox"),
            DoctorStatus.UNSUPPORTED,
            "read-only and workspace-write sandbox",
        ),
        (
            _system(config_mode="reject", global_mode="reject"),
            DoctorStatus.UNSUPPORTED,
            'approval_policy="never"',
        ),
    ],
)
def test_doctor_repair_action_matches_actual_failed_check(
    system: CliAgentSystemConfig,
    status: DoctorStatus,
    action_fragment: str,
) -> None:
    diagnostic = _diagnostic(system)

    assert diagnostic.status == status
    assert diagnostic.selectable is False
    assert diagnostic.supported_roles == []
    assert action_fragment in diagnostic.exact_next_action
    assert "npm install -g @openai/codex@latest" not in (diagnostic.exact_next_action)


def test_doctor_reports_partial_multi_role_support() -> None:
    system = _system(
        roles={AgentRole.CODING, AgentRole.VERIFICATION},
        omit="read-only",
    )
    diagnostic = _diagnostic(system)

    assert diagnostic.status == DoctorStatus.UNSUPPORTED
    assert diagnostic.selectable is False
    assert diagnostic.supported_roles == [AgentRole.CODING]
    assert diagnostic.affected_roles == [AgentRole.VERIFICATION]
    coding = next(
        result for result in diagnostic.role_results if result.role == AgentRole.CODING
    )
    verification = next(
        result
        for result in diagnostic.role_results
        if result.role == AgentRole.VERIFICATION
    )
    assert coding.supported is True
    assert verification.supported is False
    assert (
        next(
            check
            for check in verification.checks
            if check.check_id == "read_only_enforcement"
        ).status
        == "UNSUPPORTED"
    )


def test_safe_editing_and_role_support_share_one_semantic_capability() -> None:
    diagnostic = _diagnostic(_system(config_mode="reject", global_mode="reject"))
    result = diagnostic.role_results[0]
    checks = {check.check_id: check.status for check in result.checks}

    assert checks["unattended_safe_execution"] == "UNSUPPORTED"
    assert checks["safe_editing"] == "UNSUPPORTED"
    assert result.supported is False
    assert diagnostic.status == DoctorStatus.UNSUPPORTED


def test_legacy_probe_field_normalizes_without_rewriting_schema_version() -> None:
    document = {
        "schema_version": "villani.codex_probe.v1",
        "system_id": "legacy-codex",
        "checked_at": datetime(2026, 7, 26, tzinfo=timezone.utc).isoformat(),
        "configured_executable": "codex",
        "resolved_executable": str(Path(sys.executable).resolve()),
        "exact_version_output": "codex-cli 0.130.0",
        "authentication_ready": True,
        "authentication_method": "chatgpt",
        "capabilities": {
            "exec": True,
            "jsonl_output": True,
            "model_selection": True,
            "workspace_selection": True,
            "sandbox_selection": True,
            "schema_output": True,
            "last_message_output": True,
            "ephemeral": True,
            "noninteractive_approval": True,
        },
        "ready": True,
        "failures": [],
        "messages": [],
    }

    probe = CodexProbeResult.model_validate(document)

    assert probe.schema_version == "villani.codex_probe.v1"
    assert probe.capabilities["noninteractive_approval"] is True
    assert probe.capabilities["unattended_safe_execution"] is True
    assert probe.approval_strategy == "global_flag"
    assert probe.approval_policy == "never"


@pytest.mark.skipif(os.name != "nt", reason="Windows batch shim behavior")
def test_windows_cmd_launcher_uses_safe_argv_prefix(
    tmp_path: Path,
) -> None:
    launcher = tmp_path / "Codex Fixture With Spaces.cmd"
    launcher.write_text(
        f'@echo off\r\n"{sys.executable}" "{FAKE_CODEX_0144}" %*\r\n',
        encoding="utf-8",
    )
    system = _system(
        executable=str(launcher),
        launcher_arguments=[],
        system_id="codex-windows-cmd",
    )
    driver = CodexCliDriver(system)
    probe = driver.probe()

    assert probe.ready is True
    assert probe.resolved_executable == str(launcher.resolve())
    invocation, *_ = _build_invocation(
        driver, probe, AgentRole.CODING, tmp_path / "runtime"
    )
    command = driver.safe_command(invocation)
    assert Path(command[0]).name.casefold() == "cmd.exe"
    assert command[1:4] == ("/d", "/c", "call")
    assert command[4] == str(launcher.resolve())
    assert command.index("exec") > command.index('approval_policy="never"')


def test_generic_production_paths_contain_no_experiment_specific_rules() -> None:
    production_roots = [
        HERE.parent / "closed_loop" / "codex_cli",
        HERE.parent / "closed_loop" / "agent_systems",
        HERE.parent / "cli",
    ]
    production = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for root in production_roots
        for path in root.rglob("*.py")
    )

    for marker in FORBIDDEN_PRODUCTION_MARKERS:
        assert marker not in production


def test_cli_output_schemas_fit_strict_structured_output_subset() -> None:
    """Keep every Codex output schema acceptable to strict response formats."""

    schema_names = (
        "codex-coder-result.schema.json",
        "cli-classifier-result.schema.json",
        "cli-verifier-result.schema.json",
        "cli-selector-result.schema.json",
    )

    def inspect(node: Any, location: str) -> None:
        if isinstance(node, dict):
            if "enum" in node or "const" in node:
                assert "type" in node, f"{location} constrains a value without a type"
            assert "uniqueItems" not in node, (
                f"{location} uses an unsupported strict structured-output keyword"
            )
            if node.get("type") == "object":
                properties = node.get("properties", {})
                assert node.get("additionalProperties") is False, location
                assert set(node.get("required", [])) == set(properties), location
            for key, value in node.items():
                inspect(value, f"{location}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                inspect(value, f"{location}[{index}]")

    for schema_name in schema_names:
        root_document = json.loads(
            (ROOT_SCHEMA_ROOT / schema_name).read_text(encoding="utf-8")
        )
        packaged_document = json.loads(
            (PACKAGED_SCHEMA_ROOT / schema_name).read_text(encoding="utf-8")
        )
        assert packaged_document == root_document
        inspect(root_document, schema_name)
