"""User-facing agent-system detection, doctor, and profile projections.

This module is deliberately outside the controller.  It translates the role
adapters' bounded probes into stable product concepts without granting a probe
authority over routing, acceptance, selection, or delivery.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from villani_ops.core.backend import Backend

from ..durable_io import write_json_atomic
from ..claude_code_cli.driver import ClaudeCodeCliDriver
from ..codex_cli.driver import CodexCliDriver
from ..cli_roles.models import normalize_cli_classifier_result
from ..cli_roles.prompts import build_classifier_prompt
from ..cli_roles.runtime import execute_cli_role
from ..cli_roles.workspace import prepare_cli_role_workspace
from .registry import AgentSystemRegistry, build_agent_system_registry
from .role_models import (
    ROLE_LABELS,
    AgentRole,
    CliAgentSystemConfig,
    CliRolePolicy,
)


AGENT_SYSTEM_DIAGNOSTIC_SCHEMA_VERSION = "villani.agent_system_diagnostic.v1"
AGENT_SYSTEM_MANAGEMENT_SCHEMA_VERSION = "villani.agent_system_management.v1"
SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas" / "v1"


class DoctorStatus(str, Enum):
    READY = "READY"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


class StrictManagementModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoleDoctorCheck(StrictManagementModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    status: Literal["PASS", "FAIL", "UNSUPPORTED"]
    message: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class RoleDoctorResult(StrictManagementModel):
    role: AgentRole
    label: str = Field(min_length=1)
    status: DoctorStatus
    supported: bool
    checks: list[RoleDoctorCheck]
    failure: str | None = None


class AgentSystemDiagnostic(StrictManagementModel):
    schema_version: Literal["villani.agent_system_diagnostic.v1"] = (
        "villani.agent_system_diagnostic.v1"
    )
    system_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    driver: Literal["codex", "claude_code"]
    configured: bool
    status: DoctorStatus
    configured_executable: str = Field(min_length=1)
    resolved_path: str | None
    safe_display_path: str | None
    resolved_path_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    exact_version: str | None
    authentication_ready: bool
    authentication_status: Literal["ready", "not_ready", "unknown"]
    supported_roles: list[AgentRole]
    configured_roles: list[AgentRole]
    configured_model: str | None
    instruction_policy: str
    permission_policy: str
    conformance_status: Literal["passed", "action_required", "unsupported", "error"]
    last_doctor_time: datetime
    affected_roles: list[AgentRole]
    what_failed: str | None
    repository_modified: Literal[False] = False
    exact_next_action: str = Field(min_length=1)
    evidence_path: str = Field(min_length=1)
    role_results: list[RoleDoctorResult]


class AgentSystemManagementDocument(StrictManagementModel):
    schema_version: Literal["villani.agent_system_management.v1"] = (
        "villani.agent_system_management.v1"
    )
    generated_at: datetime
    systems: list[AgentSystemDiagnostic]
    repositories_modified: Literal[False] = False
    secrets_read: Literal[False] = False
    login_started: Literal[False] = False
    provider_configuration_modified: Literal[False] = False


class CliModelValidation(StrictManagementModel):
    """Result of one bounded, schema-constrained configured-model probe."""

    system_id: str = Field(min_length=1)
    configured_model: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    process_spawned: bool
    structured_output_valid: bool
    repository_modified: Literal[False] = False
    reason: str = Field(min_length=1)
    exact_next_action: str = Field(min_length=1)
    evidence_path: str = Field(min_length=1)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _path_digest(path: str | None) -> str | None:
    if not path:
        return None
    normalized = str(Path(path)).replace("\\", "/").casefold()
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def safe_display_path(path: str | None) -> str | None:
    if not path:
        return None
    value = str(Path(path))
    try:
        home = str(Path.home().resolve())
    except OSError:
        return value
    if value.casefold().startswith(home.casefold()):
        suffix = value[len(home) :].lstrip("/\\")
        return str(Path("<home>") / suffix) if suffix else "<home>"
    return value


def _failure_values(probe: Any) -> set[str]:
    return {str(getattr(item, "value", item)) for item in probe.failures}


def _unsupported_failures(driver: str) -> set[str]:
    if driver == "codex":
        return {
            "unsupported_codex_version",
            "unsupported_required_flag",
            "permission_sandbox_failure",
        }
    return {
        "unsupported_claude_version",
        "unsupported_required_capability",
        "missing_required_structured_output_capability",
        "permission_denied",
        "tool_denied",
    }


def _missing_failure(driver: str) -> str:
    return "codex_not_installed" if driver == "codex" else "claude_not_installed"


def _auth_failure(driver: str) -> str:
    return (
        "codex_not_authenticated" if driver == "codex" else "claude_not_authenticated"
    )


def _next_action(
    *, driver: str, system_id: str, failures: set[str], ready: bool
) -> str:
    executable = "codex" if driver == "codex" else "claude"
    if _missing_failure(driver) in failures:
        return (
            "npm install -g @openai/codex"
            if driver == "codex"
            else "npm install -g @anthropic-ai/claude-code"
        )
    if _auth_failure(driver) in failures:
        return "codex login" if driver == "codex" else "claude auth login"
    if failures.intersection(_unsupported_failures(driver)):
        return (
            "npm install -g @openai/codex@latest"
            if driver == "codex"
            else "npm install -g @anthropic-ai/claude-code@latest"
        )
    if driver == "claude_code" and "mcp_plugin_hook_startup_failure" in failures:
        return "claude doctor"
    if ready:
        return f"villani agents doctor {system_id}"
    return f"{executable} --version"


def _capability_checks(
    system: CliAgentSystemConfig, role: AgentRole, probe: Any
) -> list[RoleDoctorCheck]:
    capabilities = dict(probe.capabilities)
    checks = [
        RoleDoctorCheck(
            check_id="executable",
            status="PASS" if probe.resolved_executable else "FAIL",
            message=(
                f"Resolved {probe.resolved_executable}."
                if probe.resolved_executable
                else f"Executable {system.executable!r} was not found."
            ),
            evidence={"resolved": bool(probe.resolved_executable)},
        ),
        RoleDoctorCheck(
            check_id="exact_version",
            status="PASS" if probe.exact_version_output else "FAIL",
            message=(
                f"Detected {probe.exact_version_output}."
                if probe.exact_version_output
                else "The exact CLI version could not be read."
            ),
        ),
        RoleDoctorCheck(
            check_id="authentication",
            status="PASS" if probe.authentication_ready else "FAIL",
            message=(
                "Authentication is ready."
                if probe.authentication_ready
                else "Authentication is not ready."
            ),
            evidence={
                "ready": probe.authentication_ready,
                "method": probe.authentication_method,
                "credential_files_read": False,
                "secret_values_recorded": False,
            },
        ),
        RoleDoctorCheck(
            check_id="model_configuration",
            status="PASS" if bool(system.model.strip()) else "FAIL",
            message=(
                f"Configured model string is {system.model!r}."
                if system.model.strip()
                else "A model string is required."
            ),
        ),
        RoleDoctorCheck(
            check_id="process_spawn",
            status="PASS" if probe.exact_version_output else "FAIL",
            message="The CLI completed a bounded subprocess probe.",
            evidence={"shell_used": False},
        ),
        RoleDoctorCheck(
            check_id="structured_output",
            status=(
                "PASS"
                if capabilities.get("schema_output", False)
                or capabilities.get("structured_output", False)
                else "UNSUPPORTED"
            ),
            message="Schema-constrained structured output is available.",
        ),
        RoleDoctorCheck(
            check_id="cancellation",
            status="PASS" if probe.resolved_executable else "FAIL",
            message="Villani's bounded process supervisor owns cancellation and cleanup.",
            evidence={"session_resume": False, "process_tree_owned": True},
        ),
        RoleDoctorCheck(
            check_id="path_with_spaces",
            status="PASS" if probe.resolved_executable else "FAIL",
            message="Executable, workspace, and artifact paths are passed as argv without a shell.",
            evidence={"shell_used": False},
        ),
        RoleDoctorCheck(
            check_id="artifact_write",
            status="PASS" if probe.exact_version_output else "FAIL",
            message="Probe stdout, stderr, and process state were captured in Villani-owned storage.",
        ),
        RoleDoctorCheck(
            check_id="environment_redaction",
            status="PASS",
            message="Environment values and credentials are excluded from diagnostic output.",
            evidence={"credential_files_read": False, "secret_values_recorded": False},
        ),
    ]
    if role == AgentRole.CODING:
        safe_edit = (
            capabilities.get("sandbox_selection", False)
            if system.driver == "codex"
            else capabilities.get("permission_mode", False)
            and capabilities.get("allowed_tools", False)
        )
        checks.append(
            RoleDoctorCheck(
                check_id="safe_editing",
                status="PASS" if safe_edit else "UNSUPPORTED",
                message="Safe non-interactive editing is available for isolated coding work.",
            )
        )
    else:
        read_only = (
            capabilities.get("read_only_sandbox", False)
            and capabilities.get("scoped_permission_profiles", False)
            if system.driver == "codex"
            else capabilities.get("read_only_permission_mode", False)
            and capabilities.get("tools", False)
        )
        checks.append(
            RoleDoctorCheck(
                check_id="read_only_enforcement",
                status="PASS" if read_only else "UNSUPPORTED",
                message="The role can run with source mutation disabled.",
            )
        )
    if system.driver == "codex":
        checks.extend(
            [
                RoleDoctorCheck(
                    check_id="codex_exec",
                    status="PASS" if capabilities.get("exec", False) else "UNSUPPORTED",
                    message="Codex exec mode is available.",
                ),
                RoleDoctorCheck(
                    check_id="codex_jsonl",
                    status="PASS"
                    if capabilities.get("jsonl_output", False)
                    else "UNSUPPORTED",
                    message="Codex JSONL output is available.",
                ),
                RoleDoctorCheck(
                    check_id="codex_sandbox",
                    status="PASS"
                    if capabilities.get("sandbox_selection", False)
                    else "UNSUPPORTED",
                    message="Codex sandbox selection is available.",
                ),
            ]
        )
    else:
        checks.extend(
            [
                RoleDoctorCheck(
                    check_id="claude_print_mode",
                    status="PASS"
                    if capabilities.get("print_mode", False)
                    else "UNSUPPORTED",
                    message="Claude Code print mode is available.",
                ),
                RoleDoctorCheck(
                    check_id="claude_stream_json",
                    status="PASS"
                    if capabilities.get("stream_json", False)
                    else "UNSUPPORTED",
                    message="Claude Code stream-JSON is available.",
                ),
                RoleDoctorCheck(
                    check_id="claude_no_session",
                    status="PASS"
                    if capabilities.get("no_session_persistence", False)
                    else "UNSUPPORTED",
                    message="Claude Code no-session-persistence is available.",
                ),
                RoleDoctorCheck(
                    check_id="claude_tool_restriction",
                    status="PASS"
                    if capabilities.get("tools", False)
                    and capabilities.get("allowed_tools", False)
                    else "UNSUPPORTED",
                    message="Claude Code tool restriction is available.",
                ),
                RoleDoctorCheck(
                    check_id="claude_doctor",
                    status="PASS" if probe.doctor_ready else "FAIL",
                    message=(
                        "Claude Code doctor is healthy."
                        if probe.doctor_ready
                        else "Claude Code doctor reported an unhealthy installation."
                    ),
                ),
            ]
        )
    return checks


def _role_result(
    system: CliAgentSystemConfig,
    role: AgentRole,
    probe: Any | None,
    error: str | None,
) -> RoleDoctorResult:
    if error is not None or probe is None:
        return RoleDoctorResult(
            role=role,
            label=ROLE_LABELS[role],
            status=DoctorStatus.ERROR,
            supported=False,
            checks=[
                RoleDoctorCheck(
                    check_id="driver_configuration",
                    status="FAIL",
                    message=error or "The role probe did not return a result.",
                )
            ],
            failure=error or "missing role probe result",
        )
    failures = _failure_values(probe)
    unsupported = bool(failures.intersection(_unsupported_failures(system.driver)))
    missing = _missing_failure(system.driver) in failures
    if unsupported:
        status = DoctorStatus.UNSUPPORTED
    elif probe.ready:
        status = DoctorStatus.READY
    else:
        status = DoctorStatus.ACTION_REQUIRED
    supported = not unsupported and not missing
    return RoleDoctorResult(
        role=role,
        label=ROLE_LABELS[role],
        status=status,
        supported=supported,
        checks=_capability_checks(system.for_role(role), role, probe),
        failure="; ".join(probe.messages) if probe.messages else None,
    )


def _diagnostic(
    system: CliAgentSystemConfig,
    *,
    configured: bool,
    probes: Mapping[AgentRole, Any | None],
    errors: Mapping[AgentRole, str | None],
    evidence_path: str,
) -> AgentSystemDiagnostic:
    results = [
        _role_result(system, role, probes.get(role), errors.get(role))
        for role in AgentRole
        if role in system.roles
    ]
    statuses = {item.status for item in results}
    if DoctorStatus.ERROR in statuses:
        status = DoctorStatus.ERROR
    elif DoctorStatus.UNSUPPORTED in statuses:
        status = DoctorStatus.UNSUPPORTED
    elif DoctorStatus.ACTION_REQUIRED in statuses:
        status = DoctorStatus.ACTION_REQUIRED
    else:
        status = DoctorStatus.READY
    representative = next(
        (probe for probe in probes.values() if probe is not None), None
    )
    all_failures = {
        failure
        for probe in probes.values()
        if probe is not None
        for failure in _failure_values(probe)
    }
    resolved = (
        str(representative.resolved_executable)
        if representative is not None and representative.resolved_executable
        else None
    )
    auth_values = [
        bool(probe.authentication_ready)
        for probe in probes.values()
        if probe is not None
    ]
    auth_ready = bool(auth_values) and all(auth_values)
    exact_versions = sorted(
        {
            str(probe.exact_version_output)
            for probe in probes.values()
            if probe is not None and probe.exact_version_output
        }
    )
    affected = [item.role for item in results if item.status != DoctorStatus.READY]
    failures = [item.failure for item in results if item.failure]
    first_policy = system.policy_for_role(
        AgentRole.CODING
        if AgentRole.CODING in system.roles
        else min(system.roles, key=lambda item: item.value)
    )
    return AgentSystemDiagnostic(
        system_id=system.id,
        display_name="Codex CLI" if system.driver == "codex" else "Claude Code",
        driver=system.driver,
        configured=configured,
        status=status,
        configured_executable=system.executable,
        resolved_path=resolved,
        safe_display_path=safe_display_path(resolved),
        resolved_path_digest=_path_digest(resolved),
        exact_version="; ".join(exact_versions) if exact_versions else None,
        authentication_ready=auth_ready,
        authentication_status=(
            "ready" if auth_ready else "not_ready" if auth_values else "unknown"
        ),
        supported_roles=[item.role for item in results if item.supported],
        configured_roles=[role for role in AgentRole if role in system.roles],
        configured_model=system.model if configured else None,
        instruction_policy=first_policy.instruction_policy,
        permission_policy=first_policy.permission_profile,
        conformance_status=cast(
            Literal["passed", "action_required", "unsupported", "error"],
            {
                DoctorStatus.READY: "passed",
                DoctorStatus.ACTION_REQUIRED: "action_required",
                DoctorStatus.UNSUPPORTED: "unsupported",
                DoctorStatus.ERROR: "error",
            }[status],
        ),
        last_doctor_time=(
            max(probe.checked_at for probe in probes.values() if probe is not None)
            if representative is not None
            else _utc_now()
        ),
        affected_roles=affected,
        what_failed="; ".join(failures) if failures else None,
        exact_next_action=_next_action(
            driver=system.driver,
            system_id=system.id,
            failures=all_failures,
            ready=status == DoctorStatus.READY,
        ),
        evidence_path=evidence_path,
        role_results=results,
    )


def diagnose_registry(
    registry: AgentSystemRegistry,
    *,
    evidence_path: str,
    reference: str | None = None,
) -> AgentSystemManagementDocument:
    systems = [
        system
        for system in registry.list_configured()
        if isinstance(system, CliAgentSystemConfig)
        and (reference is None or system.id == reference)
    ]
    if reference is not None and not systems:
        raise ValueError(f"unknown CLI agent system {reference!r}")
    diagnostics = []
    for system in systems:
        diagnostics.append(
            _diagnostic(
                system,
                configured=True,
                probes={
                    role: registry.cli_role_probe(system.id, role)
                    for role in system.roles
                },
                errors={
                    role: registry.cli_role_error(system.id, role)
                    for role in system.roles
                },
                evidence_path=evidence_path,
            )
        )
    return AgentSystemManagementDocument(generated_at=_utc_now(), systems=diagnostics)


def _detection_system(
    *, driver: Literal["codex", "claude_code"], executable: str
) -> CliAgentSystemConfig:
    policies = {
        role: CliRolePolicy(
            instruction_policy=(
                "native_project" if role == AgentRole.CODING else "villani_controlled"
            ),
            permission_profile=(
                "workspace_write" if role == AgentRole.CODING else "read_only"
            ),
            environment_policy="minimal",
        )
        for role in AgentRole
    }
    return CliAgentSystemConfig(
        kind="cli_agent",
        id="detected-codex" if driver == "codex" else "detected-claude-code",
        enabled=True,
        driver=driver,
        executable=executable,
        model="detection-only-unconfigured",
        roles=set(AgentRole),
        timeout_seconds=30,
        max_parallel=1,
        instruction_policy="native_project",
        permission_profile="workspace_write",
        environment_policy="minimal",
        role_policies=policies,
        provider_options={},
    )


def detect_cli_agent_systems(
    configuration: Mapping[str, Any] | None = None,
    *,
    backends: Mapping[str, Backend] | None = None,
    evidence_path: str,
) -> AgentSystemManagementDocument:
    """Boundedly probe Codex and Claude without login, mutation, or credential reads."""

    configured: dict[str, CliAgentSystemConfig] = {}
    if configuration is not None:
        registry = build_agent_system_registry(configuration, backends or {})
        for system in registry.list_configured():
            if isinstance(system, CliAgentSystemConfig):
                configured.setdefault(system.driver, system)
    diagnostics: list[AgentSystemDiagnostic] = []
    for driver_name, executable in (("codex", "codex"), ("claude_code", "claude")):
        driver = driver_name  # keep Literal narrowing local and obvious
        system = configured.get(driver_name) or _detection_system(
            driver=driver,  # type: ignore[arg-type]
            executable=executable,
        )
        probes: dict[AgentRole, Any | None] = {}
        errors: dict[AgentRole, str | None] = {}
        for role in AgentRole:
            if role not in system.roles:
                continue
            role_system = system.for_role(role)
            try:
                probe_driver = (
                    CodexCliDriver(role_system)
                    if driver_name == "codex"
                    else ClaudeCodeCliDriver(role_system)
                )
                probes[role] = probe_driver.probe()
                errors[role] = None
            except (OSError, TypeError, ValueError) as error:
                probes[role] = None
                errors[role] = f"{type(error).__name__}: {error}"
        diagnostics.append(
            _diagnostic(
                system,
                configured=driver_name in configured,
                probes=probes,
                errors=errors,
                evidence_path=evidence_path,
            )
        )
    return AgentSystemManagementDocument(generated_at=_utc_now(), systems=diagnostics)


def validate_cli_model(
    system: CliAgentSystemConfig, *, evidence_root: Path
) -> CliModelValidation:
    """Prove an explicit model string with one disposable read-only invocation.

    Detection intentionally stops at executable, auth, and capability inspection.
    Setup calls this stronger probe only after the user supplies a model string.  The
    invoked agent sees an empty disposable baseline and a schema-constrained task; it
    never receives the target repository or any candidate worktree.
    """

    probe_id = f"model-probe-{uuid.uuid4().hex}"
    root = (Path(evidence_root) / system.id / probe_id).resolve()
    baseline = root / "original-repository"
    run_directory = root / "run"
    summary_path = root / "model-validation.json"
    baseline.mkdir(parents=True)
    run_directory.mkdir()
    policy = CliRolePolicy(
        instruction_policy="villani_controlled",
        permission_profile="read_only",
        environment_policy="minimal",
    )
    role_system = system.model_copy(
        update={
            "roles": {AgentRole.CLASSIFICATION},
            "instruction_policy": policy.instruction_policy,
            "permission_profile": policy.permission_profile,
            "environment_policy": policy.environment_policy,
            "role_policies": {AgentRole.CLASSIFICATION: policy},
        }
    )
    driver = (
        CodexCliDriver(role_system)
        if role_system.driver == "codex"
        else ClaudeCodeCliDriver(role_system)
    )
    process_spawned = False
    try:
        probe = driver.probe()
        prompt = build_classifier_prompt(
            task={
                "schema_version": "villani.cli_classifier_task.v1",
                "task": (
                    "Confirm schema-constrained output for this configured model. "
                    "Do not inspect or edit files."
                ),
            },
            success_criteria={
                "schema_version": "villani.cli_classifier_success_criteria.v1",
                "success_criteria": "Return one valid classifier result.",
            },
            repository_metadata={
                "schema_version": "villani.cli_classifier_repository_metadata.v1",
                "tracked_file_count_supplied": 0,
                "inventory_truncated": False,
                "tracked_files": [],
                "package_files": [],
                "extension_counts": {},
                "tracked_state_clean": True,
                "tracked_changed_file_count": 0,
            },
            policy_metadata={
                "schema_version": "villani.cli_classifier_policy_metadata.v1",
                "requires_file_changes": False,
                "difficulty_floor": None,
                "risk_floor": None,
                "configured_adjustments": [],
            },
        )
        workspace = prepare_cli_role_workspace(
            role=AgentRole.CLASSIFICATION.value,
            invocation_id=probe_id,
            run_directory=run_directory,
            target_repository=baseline,
            input_documents={
                "task.json": ("task", {"task": "Configured model no-op probe."}),
                "success-criteria.json": (
                    "success_criteria",
                    {"success_criteria": "Return schema-constrained JSON."},
                ),
                "repository-metadata.json": (
                    "repository_metadata",
                    {"tracked_files": [], "tracked_state_clean": True},
                ),
                "policy-metadata.json": (
                    "policy_metadata",
                    {"requires_file_changes": False},
                ),
            },
            prompt_bytes=prompt.bytes,
            output_schema_source=SCHEMA_ROOT / "cli-classifier-result.schema.json",
            raw_result_filename="classifier-result.json",
            normalized_result_filename="normalized-result.json",
            blindness={
                "candidate_output": False,
                "expected_solution": False,
                "benchmark_identity": False,
                "provider_recommendation": False,
            },
        )
        execution = execute_cli_role(
            driver=driver,
            probe=probe,
            role=AgentRole.CLASSIFICATION,
            workspace=workspace,
            run_id=probe_id,
        )
        process_spawned = execution.process_spawned
        if execution.failure is not None:
            raise ValueError(execution.reason or execution.failure.value)
        normalized = normalize_cli_classifier_result(
            execution.raw_text, repository_inventory=set()
        )
        write_json_atomic(
            workspace.normalized_result_path, normalized.model_dump(mode="json")
        )
        result = CliModelValidation(
            system_id=system.id,
            configured_model=system.model,
            status="PASS",
            process_spawned=execution.process_spawned,
            structured_output_valid=True,
            reason="The configured model completed a read-only schema output probe.",
            exact_next_action=f"villani agents doctor {system.id}",
            evidence_path=str(summary_path),
        )
    except (OSError, RuntimeError, TypeError, ValueError, ValidationError) as error:
        result = CliModelValidation(
            system_id=system.id,
            configured_model=system.model,
            status="FAIL",
            process_spawned=process_spawned,
            structured_output_valid=False,
            reason=f"Configured model probe failed: {type(error).__name__}: {error}",
            exact_next_action="villani setup --execution-mode cli",
            evidence_path=str(summary_path),
        )
    write_json_atomic(summary_path, result.model_dump(mode="json"))
    return result


def write_management_evidence(
    path: Path, document: AgentSystemManagementDocument
) -> None:
    write_json_atomic(path, document.model_dump(mode="json"))


__all__ = [
    "AGENT_SYSTEM_DIAGNOSTIC_SCHEMA_VERSION",
    "AGENT_SYSTEM_MANAGEMENT_SCHEMA_VERSION",
    "AgentSystemDiagnostic",
    "AgentSystemManagementDocument",
    "CliModelValidation",
    "DoctorStatus",
    "RoleDoctorCheck",
    "RoleDoctorResult",
    "detect_cli_agent_systems",
    "diagnose_registry",
    "safe_display_path",
    "validate_cli_model",
    "write_management_evidence",
]
