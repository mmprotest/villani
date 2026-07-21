"""Safe registry and factory for configured agent systems."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from villani_ops.core.backend import Backend
from villani_ops.closed_loop.adapters.villani_code_attempt import (
    VillaniCodeAttemptAdapter,
)
from villani_ops.closed_loop.codex_cli.attempt import CodexCliAttemptAdapter
from villani_ops.closed_loop.codex_cli.driver import CodexCliDriver
from villani_ops.closed_loop.claude_code_cli.attempt import (
    ClaudeCodeCliAttemptAdapter,
)
from villani_ops.closed_loop.claude_code_cli.driver import ClaudeCodeCliDriver
from villani_ops.runners.claude_code import ClaudeCodeRunner
from villani_ops.runners.codex_app_server import CodexAppServerRunner
from villani_ops.runners.base import Runner

from .adapters import (
    AgentSystemAttemptRunner,
    HarnessAdapter,
    VillaniCodeHarnessAdapter,
)
from .configuration import (
    build_agent_system_identities,
    migrate_agent_system_configuration,
)
from .discovery import discover_harness, resolve_harness_command
from .models import AgentSystemDoctorReport, AgentSystemIdentity, DoctorCheck, utc_now
from .role_models import (
    AgentRole,
    AgentSystemConfig,
    AgentSystemInspection,
    CliAgentSystemConfig,
    ExecutionProfileInspection,
    RoleBindings,
)
from .role_registry import RoleSystemRegistry
from ..qualification.store import QualificationStore


class AgentSystemRegistry:
    def __init__(
        self,
        configuration: Mapping[str, Any],
        backends: Mapping[str, Backend],
        *,
        qualification_store: QualificationStore | None = None,
    ) -> None:
        identities, by_backend, migration = build_agent_system_identities(
            configuration, backends
        )
        self.identities = identities
        self.by_backend = by_backend
        self.migration_report = migration
        self.qualification_store = qualification_store
        migrated, _ = migrate_agent_system_configuration(configuration)
        self.configuration = migrated
        self.backends = dict(backends)
        base_role_registry = RoleSystemRegistry(migrated, backends)
        cli_inspections: dict[str, AgentSystemInspection] = {}
        self._cli_attempt_runners: dict[str, Any] = {}
        self._codex_drivers: dict[str, CodexCliDriver] = {}
        self._codex_probes = {}
        self._claude_drivers: dict[str, ClaudeCodeCliDriver] = {}
        self._claude_probes = {}
        for system in base_role_registry.list_configured():
            if not isinstance(system, CliAgentSystemConfig) or not system.enabled:
                continue
            if system.roles != {AgentRole.CODING}:
                cli_inspections[system.id] = AgentSystemInspection(
                    system=system,
                    status="configured",
                    runnable=False,
                    reason=(
                        f"Agent Mode supports {system.driver} CLI only for a "
                        "coding-only system; CLI classification, verification, and "
                        "selection remain unavailable."
                    ),
                )
                continue
            if system.driver == "codex":
                try:
                    codex_driver = CodexCliDriver(system)
                    codex_probe = codex_driver.probe()
                except (OSError, TypeError, ValueError) as error:
                    cli_inspections[system.id] = AgentSystemInspection(
                        system=system,
                        status="unavailable",
                        runnable=False,
                        reason=f"Codex driver configuration failed: {error}",
                    )
                    continue
                self._codex_drivers[system.id] = codex_driver
                self._codex_probes[system.id] = codex_probe
                if codex_probe.ready:
                    cli_inspections[system.id] = AgentSystemInspection(
                        system=system,
                        status="ready",
                        runnable=True,
                        reason=(
                            "Codex coding driver passed doctor with "
                            f"{codex_probe.exact_version_output}."
                        ),
                    )
                    self._cli_attempt_runners[system.id] = CodexCliAttemptAdapter(
                        codex_driver, probe=codex_probe
                    )
                else:
                    cli_inspections[system.id] = AgentSystemInspection(
                        system=system,
                        status="unavailable",
                        runnable=False,
                        reason=(
                            "; ".join(codex_probe.messages) or "Codex doctor failed."
                        ),
                    )
                continue
            if system.driver == "claude_code":
                try:
                    claude_driver = ClaudeCodeCliDriver(system)
                    claude_probe = claude_driver.probe()
                except (OSError, TypeError, ValueError) as error:
                    cli_inspections[system.id] = AgentSystemInspection(
                        system=system,
                        status="unavailable",
                        runnable=False,
                        reason=f"Claude Code driver configuration failed: {error}",
                    )
                    continue
                self._claude_drivers[system.id] = claude_driver
                self._claude_probes[system.id] = claude_probe
                if claude_probe.ready:
                    cli_inspections[system.id] = AgentSystemInspection(
                        system=system,
                        status="ready",
                        runnable=True,
                        reason=(
                            "Claude Code coding driver passed doctor with "
                            f"{claude_probe.exact_version_output}."
                        ),
                    )
                    self._cli_attempt_runners[system.id] = ClaudeCodeCliAttemptAdapter(
                        claude_driver, probe=claude_probe
                    )
                else:
                    cli_inspections[system.id] = AgentSystemInspection(
                        system=system,
                        status="unavailable",
                        runnable=False,
                        reason=(
                            "; ".join(claude_probe.messages)
                            or "Claude Code doctor failed."
                        ),
                    )
        self.role_registry = RoleSystemRegistry(
            migrated, backends, cli_inspections=cli_inspections
        )
        raw_systems = migrated.get("agent_systems", {}).get("systems", {})
        configured_commands: dict[str, str] = {}
        for route_name, raw in raw_systems.items():
            if not isinstance(raw, Mapping):
                continue
            if raw.get("kind") is not None:
                continue
            harness = raw.get("harness")
            if not isinstance(harness, Mapping):
                continue
            harness_id = str(harness.get("id") or "")
            backend_name = str(raw.get("backend") or route_name)
            backend = backends.get(backend_name)
            default = "claude" if harness_id == "claude-code" else harness_id
            command = str(
                harness.get("command")
                or (backend.command_name if backend is not None else None)
                or default
            )
            configured_commands[harness_id] = command
        # Legacy harness discovery remains separate from the neutral CLI coding
        # probes above.
        self.discoveries = tuple(
            discover_harness(harness_id, command)
            for harness_id, command in sorted(configured_commands.items())
        )
        adapters: dict[str, HarnessAdapter] = {}
        for identity in identities:
            if identity.production_enabled:
                raw = raw_systems.get(identity.route_name, {})
                harness_raw = raw.get("harness", {}) if isinstance(raw, Mapping) else {}
                backend_name = (
                    str(raw.get("backend") or identity.route_name)
                    if isinstance(raw, Mapping)
                    else identity.route_name
                )
                backend = backends.get(backend_name)
                default = (
                    "claude"
                    if identity.harness.harness_id == "claude-code"
                    else identity.harness.harness_id
                )
                configured_command = str(
                    harness_raw.get("command")
                    if isinstance(harness_raw, Mapping) and harness_raw.get("command")
                    else backend.command_name
                    if backend is not None and backend.command_name
                    else default
                )
                prefix, _ = resolve_harness_command(
                    configured_command, identity.harness.harness_id
                )
                command = (
                    str(Path(prefix[-1]).resolve())
                    if prefix and len(prefix) == 1 and Path(prefix[-1]).is_file()
                    else configured_command
                )
                runner: Runner | None
                if identity.harness.harness_id == "codex":
                    runner = CodexAppServerRunner(
                        command=command,
                        expected_version=identity.harness.version,
                        reasoning_effort=(
                            str(backend.metadata["reasoning_effort"])
                            if backend is not None
                            and backend.metadata.get("reasoning_effort") is not None
                            else None
                        ),
                    )
                elif identity.harness.harness_id == "claude-code":
                    runner = ClaudeCodeRunner(
                        command=command,
                        expected_version=identity.harness.version,
                        strict_native_sandbox_available=bool(
                            identity.readiness
                            and (
                                identity.readiness.details.get(
                                    "strict_sandbox_available"
                                )
                                or identity.readiness.details.get(
                                    "strict_outer_isolation"
                                )
                            )
                        ),
                        resume_same_attempt=bool(
                            backend is not None
                            and backend.metadata.get("resume_same_attempt") is True
                        ),
                    )
                else:
                    runner = None
                adapters[identity.system_id] = VillaniCodeHarnessAdapter(
                    identity,
                    backends,
                    implementation=(
                        None
                        if runner is None
                        else VillaniCodeAttemptAdapter(backends=backends, runner=runner)
                    ),
                    command=command,
                )
        self._adapters = adapters

    def list(self) -> tuple[AgentSystemIdentity, ...]:
        return self.identities

    def list_configured(self) -> tuple[AgentSystemConfig, ...]:
        return self.role_registry.list_configured()

    def inspect_configured(self, system_id: str) -> AgentSystemConfig:
        return self.role_registry.inspect_configured(system_id)

    def profile_status(self, profile_id: str) -> ExecutionProfileInspection:
        return self.role_registry.profile_status(profile_id)

    def list_profiles(self) -> tuple[ExecutionProfileInspection, ...]:
        return self.role_registry.list_profiles()

    def resolve_profile(self, profile_id: str | None = None) -> RoleBindings:
        return self.role_registry.resolve_profile(profile_id)

    def require_profile_runnable(self, bindings: RoleBindings) -> None:
        self.role_registry.require_runnable(bindings)

    def backend_reference(self, bindings: RoleBindings, role: AgentRole) -> str | None:
        return self.role_registry.backend_reference(bindings, role)

    def inspect(self, reference: str) -> AgentSystemIdentity:
        matches = [
            identity
            for identity in self.identities
            if reference in {identity.system_id, identity.route_name}
        ]
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous agent system {reference!r}")
        return matches[0]

    def doctor(
        self, reference: str | None = None
    ) -> tuple[AgentSystemDoctorReport, ...]:
        if reference is None:
            configured_reports = tuple(
                self._configured_doctor(system.id)
                for system in self.role_registry.list_configured()
            )
        elif reference in self.role_registry.system_by_id:
            return (self._configured_doctor(reference),)
        else:
            configured_reports = ()
        identities = (
            self.identities if reference is None else (self.inspect(reference),)
        )
        reports: list[AgentSystemDoctorReport] = []
        for identity in identities:
            adapter = self._adapters.get(identity.system_id)
            if adapter is not None:
                reports.append(adapter.doctor())
            else:
                reports.append(
                    AgentSystemDoctorReport(
                        system_id=identity.system_id,
                        checked_at=identity.detection_time,
                        selectable=False,
                        checks=[
                            DoctorCheck(
                                name="production_enablement",
                                status=(
                                    "pass" if identity.production_enabled else "fail"
                                ),
                                message=(
                                    "Agent system is production enabled."
                                    if identity.production_enabled
                                    else "Agent system is disabled and cannot be selected."
                                ),
                            ),
                            DoctorCheck(
                                name="harness_adapter",
                                status="fail",
                                message=(
                                    "No qualified production adapter is registered for "
                                    f"{identity.harness.harness_id}."
                                ),
                                evidence={
                                    "qualification_status": identity.qualification_status,
                                    "protocol_version": identity.harness.protocol_version,
                                    "repair_action": (
                                        identity.readiness.repair_action
                                        if identity.readiness
                                        else "Configure a supported harness adapter."
                                    ),
                                },
                            ),
                        ],
                    )
                )
        return (*configured_reports, *reports)

    def _configured_doctor(self, system_id: str) -> AgentSystemDoctorReport:
        system = self.role_registry.inspect_configured(system_id)
        inspection = self.role_registry.inspect_system(system_id)
        probe = self._codex_probes.get(system_id)
        if (
            isinstance(system, CliAgentSystemConfig)
            and system.driver == "codex"
            and probe is not None
        ):
            checks = [
                DoctorCheck(
                    name="executable",
                    status="pass" if probe.resolved_executable else "fail",
                    message=(
                        f"Resolved executable: {probe.resolved_executable}"
                        if probe.resolved_executable
                        else f"Executable {system.executable!r} was not found."
                    ),
                    evidence={"resolved_executable": probe.resolved_executable},
                ),
                DoctorCheck(
                    name="version_and_capabilities",
                    status=(
                        "pass"
                        if probe.exact_version_output
                        and all(
                            probe.capabilities.get(name, False)
                            for name in (
                                "exec",
                                "jsonl_output",
                                "model_selection",
                                "workspace_selection",
                                "sandbox_selection",
                                "schema_output",
                                "last_message_output",
                                "ephemeral",
                                "noninteractive_approval",
                            )
                        )
                        else "fail"
                    ),
                    message=(
                        f"Detected {probe.exact_version_output}."
                        if probe.exact_version_output
                        else "Exact Codex version or required exec capabilities are unavailable."
                    ),
                    evidence={
                        "exact_version_output": probe.exact_version_output,
                        "capabilities": probe.capabilities,
                    },
                ),
                DoctorCheck(
                    name="authentication",
                    status="pass" if probe.authentication_ready else "fail",
                    message=(
                        "Codex CLI reports an active login."
                        if probe.authentication_ready
                        else "Codex CLI authentication is missing; run the Codex login flow directly."
                    ),
                    evidence={
                        "ready": probe.authentication_ready,
                        "method": probe.authentication_method,
                        "credential_values_recorded": False,
                    },
                ),
                DoctorCheck(
                    name="coding_scope",
                    status="pass" if inspection.runnable else "fail",
                    message=inspection.reason,
                    evidence={
                        "roles": sorted(role.value for role in system.roles),
                        "permission_profile": system.permission_profile,
                        "instruction_policy": system.instruction_policy,
                    },
                ),
            ]
            return AgentSystemDoctorReport(
                system_id=system.id,
                checked_at=probe.checked_at,
                selectable=inspection.runnable,
                checks=checks,
            )
        claude_probe = self._claude_probes.get(system_id)
        if (
            isinstance(system, CliAgentSystemConfig)
            and system.driver == "claude_code"
            and claude_probe is not None
        ):
            required = {
                "print_mode",
                "stream_json",
                "structured_output",
                "no_session_persistence",
                "model_selection",
                "permission_mode",
                "tools",
                "allowed_tools",
                "verbose",
                "no_chrome",
                "stdin_prompt",
            }
            if system.instruction_policy == "villani_controlled":
                required.update(
                    {
                        "bare",
                        "settings",
                        "setting_sources",
                        "strict_mcp_config",
                        "mcp_config",
                        "disable_slash_commands",
                    }
                )
            capabilities_ready = bool(
                claude_probe.exact_version_output
                and all(claude_probe.capabilities.get(name, False) for name in required)
            )
            checks = [
                DoctorCheck(
                    name="executable",
                    status="pass" if claude_probe.resolved_executable else "fail",
                    message=(
                        f"Resolved executable: {claude_probe.resolved_executable}"
                        if claude_probe.resolved_executable
                        else f"Executable {system.executable!r} was not found."
                    ),
                    evidence={"resolved_executable": claude_probe.resolved_executable},
                ),
                DoctorCheck(
                    name="version_and_capabilities",
                    status="pass" if capabilities_ready else "fail",
                    message=(
                        f"Detected {claude_probe.exact_version_output}."
                        if claude_probe.exact_version_output
                        else "Exact Claude Code version or required capabilities are unavailable."
                    ),
                    evidence={
                        "exact_version_output": claude_probe.exact_version_output,
                        "parsed_version": claude_probe.parsed_version,
                        "capabilities": claude_probe.capabilities,
                        "resolved_flags": claude_probe.resolved_flags,
                    },
                ),
                DoctorCheck(
                    name="authentication",
                    status=("pass" if claude_probe.authentication_ready else "fail"),
                    message=(
                        "Claude Code reports active authentication."
                        if claude_probe.authentication_ready
                        else "Claude Code authentication is missing; use the Claude Code auth flow directly."
                    ),
                    evidence={
                        "ready": claude_probe.authentication_ready,
                        "method": claude_probe.authentication_method,
                        "credential_values_recorded": False,
                        "billing_identity": "not_reported",
                    },
                ),
                DoctorCheck(
                    name="claude_doctor",
                    status="pass" if claude_probe.doctor_ready else "fail",
                    message=(
                        "`claude doctor` completed successfully."
                        if claude_probe.doctor_ready
                        else "`claude doctor` reported an unhealthy installation or configuration."
                    ),
                    evidence={"ready": claude_probe.doctor_ready},
                ),
                DoctorCheck(
                    name="coding_scope",
                    status="pass" if inspection.runnable else "fail",
                    message=inspection.reason,
                    evidence={
                        "roles": sorted(role.value for role in system.roles),
                        "permission_profile": system.permission_profile,
                        "instruction_policy": system.instruction_policy,
                        "no_session_persistence": True,
                    },
                ),
            ]
            return AgentSystemDoctorReport(
                system_id=system.id,
                checked_at=claude_probe.checked_at,
                selectable=inspection.runnable,
                checks=checks,
            )
        return AgentSystemDoctorReport(
            system_id=system.id,
            checked_at=utc_now(),
            selectable=inspection.runnable,
            checks=[
                DoctorCheck(
                    name="configuration",
                    status="pass" if inspection.runnable else "fail",
                    message=inspection.reason,
                    evidence={"kind": system.kind},
                )
            ],
        )

    def cli_attempt_runners(self):
        return dict(self._cli_attempt_runners)

    def attempt_runner(self) -> AgentSystemAttemptRunner:
        return AgentSystemAttemptRunner(
            self.identities,
            self.by_backend,
            self._adapters,
            migration_report=self.migration_report,
            qualification_store=self.qualification_store,
            backends=self.backends,
        )


def build_agent_system_registry(
    configuration: Mapping[str, Any],
    backends: Mapping[str, Backend],
    *,
    qualification_store: QualificationStore | None = None,
) -> AgentSystemRegistry:
    return AgentSystemRegistry(
        configuration, backends, qualification_store=qualification_store
    )


def build_agent_system_runner(
    configuration: Mapping[str, Any],
    backends: Mapping[str, Backend],
    *,
    qualification_store: QualificationStore | None = None,
) -> AgentSystemAttemptRunner:
    return build_agent_system_registry(
        configuration, backends, qualification_store=qualification_store
    ).attempt_runner()


__all__ = [
    "AgentSystemRegistry",
    "build_agent_system_registry",
    "build_agent_system_runner",
]
