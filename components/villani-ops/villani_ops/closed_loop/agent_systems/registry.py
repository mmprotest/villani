"""Safe registry and factory for configured agent systems."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from villani_ops.core.backend import Backend
from villani_ops.closed_loop.adapters.villani_code_attempt import (
    VillaniCodeAttemptAdapter,
)
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
from .discovery import discover_agent_harnesses, resolve_harness_command
from .models import AgentSystemDoctorReport, AgentSystemIdentity, DoctorCheck
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
        self.configuration = dict(configuration)
        self.backends = dict(backends)
        migrated, _ = migrate_agent_system_configuration(configuration)
        raw_systems = migrated.get("agent_systems", {}).get("systems", {})
        configured_commands: dict[str, str] = {}
        for route_name, raw in raw_systems.items():
            if not isinstance(raw, Mapping):
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
        self.discoveries = discover_agent_harnesses(configured_commands)
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
        return tuple(reports)

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
