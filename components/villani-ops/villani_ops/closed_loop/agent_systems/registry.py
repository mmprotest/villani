"""Safe registry and factory for configured agent systems."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from villani_ops.core.backend import Backend

from .adapters import AgentSystemAttemptRunner, HarnessAdapter, VillaniCodeHarnessAdapter
from .configuration import build_agent_system_identities
from .models import AgentSystemDoctorReport, AgentSystemIdentity, DoctorCheck


class AgentSystemRegistry:
    def __init__(
        self, configuration: Mapping[str, Any], backends: Mapping[str, Backend]
    ) -> None:
        identities, by_backend, migration = build_agent_system_identities(
            configuration, backends
        )
        self.identities = identities
        self.by_backend = by_backend
        self.migration_report = migration
        adapters: dict[str, HarnessAdapter] = {}
        for identity in identities:
            if (
                identity.production_enabled
                and identity.harness.harness_id == "villani-code"
            ):
                adapters[identity.system_id] = VillaniCodeHarnessAdapter(
                    identity, backends
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

    def doctor(self, reference: str | None = None) -> tuple[AgentSystemDoctorReport, ...]:
        identities = self.identities if reference is None else (self.inspect(reference),)
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
        )


def build_agent_system_registry(
    configuration: Mapping[str, Any], backends: Mapping[str, Backend]
) -> AgentSystemRegistry:
    return AgentSystemRegistry(configuration, backends)


def build_agent_system_runner(
    configuration: Mapping[str, Any], backends: Mapping[str, Backend]
) -> AgentSystemAttemptRunner:
    return build_agent_system_registry(configuration, backends).attempt_runner()


__all__ = [
    "AgentSystemRegistry",
    "build_agent_system_registry",
    "build_agent_system_runner",
]
