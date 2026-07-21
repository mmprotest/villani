"""Registry for neutral role systems and execution profiles.

The registry performs configuration validation and identity resolution only.
It never imports, probes, or launches Codex or Claude Code drivers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from villani_ops.core.backend import Backend

from .models import configuration_digest
from .role_models import (
    AgentInvocationIdentity,
    AgentRole,
    AgentSystemCatalog,
    AgentSystemConfig,
    AgentSystemInspection,
    ApiAgentSystemConfig,
    CliAgentSystemConfig,
    ExecutionProfileInspection,
    InternalRunnerSystemConfig,
    RoleBindings,
    parse_agent_system,
)


KNOWN_INTERNAL_RUNNERS: dict[AgentRole, frozenset[str]] = {
    AgentRole.CLASSIFICATION: frozenset({"task_classifier"}),
    AgentRole.CODING: frozenset({"villani_code"}),
    AgentRole.VERIFICATION: frozenset({"villani_verifier"}),
    AgentRole.SELECTION: frozenset({"evidence_selector"}),
}

_CANONICAL_BACKEND_ROLE = {
    "classification": AgentRole.CLASSIFICATION,
    "coding": AgentRole.CODING,
    "review": AgentRole.VERIFICATION,
    "selection": AgentRole.SELECTION,
}


class RoleBindingConfigurationError(ValueError):
    """Actionable configuration failure at the role-system boundary."""


def _validation_message(prefix: str, error: ValidationError) -> str:
    issue = error.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(part) for part in issue.get("loc", ()))
    path = ".".join(part for part in (prefix, location) if part)
    return f"{path}: {issue.get('msg', 'invalid value')}"


def _system_document(system: AgentSystemConfig) -> dict[str, Any]:
    document = system.model_dump(mode="json")
    document["roles"] = sorted(str(role) for role in document["roles"])
    return document


class RoleSystemRegistry:
    def __init__(
        self,
        configuration: Mapping[str, Any],
        backends: Mapping[str, Backend],
        *,
        cli_inspections: Mapping[str, AgentSystemInspection] | None = None,
    ) -> None:
        self.configuration = dict(configuration)
        self.backends = dict(backends)
        self.systems = self._parse_systems(configuration)
        self.system_by_id = {system.id: system for system in self.systems}
        self.cli_inspections = dict(cli_inspections or {})
        raw_profiles = configuration.get("execution_profiles")
        if raw_profiles is None:
            raw_profiles = {}
        if not isinstance(raw_profiles, Mapping):
            raise RoleBindingConfigurationError(
                "execution_profiles: expected a mapping keyed by profile id"
            )
        self.raw_profiles = {str(key): value for key, value in raw_profiles.items()}

    @staticmethod
    def _parse_systems(
        configuration: Mapping[str, Any],
    ) -> tuple[AgentSystemConfig, ...]:
        candidates: list[tuple[str, Any]] = []
        container = configuration.get("agent_systems")
        if container is not None:
            if not isinstance(container, Mapping):
                raise RoleBindingConfigurationError("agent_systems: expected a mapping")
            raw_systems = container.get("systems", {})
            if not isinstance(raw_systems, Mapping):
                raise RoleBindingConfigurationError(
                    "agent_systems.systems: expected a mapping keyed by system id"
                )
            candidates.extend(
                (str(key), value)
                for key, value in raw_systems.items()
                if isinstance(value, Mapping) and "kind" in value
            )

        catalog = configuration.get("agent_system_catalog")
        if catalog is not None:
            if not isinstance(catalog, Mapping):
                raise RoleBindingConfigurationError(
                    "agent_system_catalog: expected a mapping"
                )
            raw_catalog_systems = catalog.get("systems", [])
            if not isinstance(raw_catalog_systems, list):
                raise RoleBindingConfigurationError(
                    "agent_system_catalog.systems: expected a list"
                )
            for index, value in enumerate(raw_catalog_systems):
                if not isinstance(value, Mapping):
                    raise RoleBindingConfigurationError(
                        f"agent_system_catalog.systems.{index}: expected a mapping"
                    )
                candidates.append((str(value.get("id") or index), value))

        parsed: list[AgentSystemConfig] = []
        for key, raw in candidates:
            if not isinstance(raw, Mapping):
                raise RoleBindingConfigurationError(
                    f"agent_systems.systems.{key}: expected a mapping"
                )
            document = dict(raw)
            document.setdefault("id", key)
            try:
                system = parse_agent_system(document)
            except ValidationError as error:
                raise RoleBindingConfigurationError(
                    _validation_message(f"agent_systems.systems.{key}", error)
                ) from error
            if key != system.id and not key.isdigit():
                raise RoleBindingConfigurationError(
                    f"agent_systems.systems.{key}.id: must match mapping key {key!r}"
                )
            parsed.append(system)
        try:
            catalog_model = AgentSystemCatalog(systems=parsed)
        except ValidationError as error:
            raise RoleBindingConfigurationError(
                _validation_message("agent_systems", error)
            ) from error
        return tuple(sorted(catalog_model.systems, key=lambda item: item.id))

    def list_configured(self) -> tuple[AgentSystemConfig, ...]:
        return self.systems

    def inspect_configured(self, system_id: str) -> AgentSystemConfig:
        system = self.system_by_id.get(system_id)
        if system is None:
            raise RoleBindingConfigurationError(
                f"unknown agent-system id {system_id!r}; use `villani agents list`"
            )
        return system

    def inspect_system(self, system_id: str) -> AgentSystemInspection:
        system = self.inspect_configured(system_id)
        if not system.enabled:
            return AgentSystemInspection(
                system=system,
                status="disabled",
                runnable=False,
                reason="The configured system is disabled.",
            )
        if isinstance(system, CliAgentSystemConfig):
            resolved = self.cli_inspections.get(system.id)
            if resolved is not None:
                return resolved
            return AgentSystemInspection(
                system=system,
                status="configured",
                runnable=False,
                reason=(
                    f"{system.driver} CLI integration is unavailable because it is "
                    "not registered for this role and capability set."
                ),
            )
        if isinstance(system, ApiAgentSystemConfig):
            reference = system.existing_backend_reference
            if reference is None:
                return AgentSystemInspection(
                    system=system,
                    status="unavailable",
                    runnable=False,
                    reason=(
                        "No existing_backend_reference connects this API system to "
                        "an installed implementation."
                    ),
                )
            backend = self.backends.get(reference)
            if backend is None:
                return AgentSystemInspection(
                    system=system,
                    status="unavailable",
                    runnable=False,
                    reason=f"Referenced backend {reference!r} is not configured.",
                )
            if not backend.enabled:
                return AgentSystemInspection(
                    system=system,
                    status="unavailable",
                    runnable=False,
                    reason=f"Referenced backend {reference!r} is disabled.",
                )
            backend_roles = {
                canonical
                for role in backend.roles
                if (canonical := _CANONICAL_BACKEND_ROLE.get(str(role))) is not None
            }
            unsupported_roles = sorted(
                role.value for role in system.roles - backend_roles
            )
            if unsupported_roles:
                return AgentSystemInspection(
                    system=system,
                    status="unavailable",
                    runnable=False,
                    reason=(
                        f"Referenced backend {reference!r} does not implement "
                        f"declared role(s): {', '.join(unsupported_roles)}."
                    ),
                )
            return AgentSystemInspection(
                system=system,
                status="ready",
                runnable=True,
                reason=f"Uses existing backend {reference!r}.",
            )
        unsupported_roles = [
            role.value
            for role in system.roles
            if system.runner not in KNOWN_INTERNAL_RUNNERS[role]
        ]
        if unsupported_roles:
            return AgentSystemInspection(
                system=system,
                status="unavailable",
                runnable=False,
                reason=(
                    f"Internal runner {system.runner!r} is not registered for "
                    f"role(s): {', '.join(sorted(unsupported_roles))}."
                ),
            )
        return AgentSystemInspection(
            system=system,
            status="ready",
            runnable=True,
            reason=f"Internal runner {system.runner!r} is registered.",
        )

    def _raw_profile(self, profile_id: str) -> Mapping[str, Any]:
        raw = self.raw_profiles.get(profile_id)
        if raw is None:
            raise RoleBindingConfigurationError(
                f"unknown execution profile {profile_id!r}; use `villani profiles list`"
            )
        if not isinstance(raw, Mapping):
            raise RoleBindingConfigurationError(
                f"execution_profiles.{profile_id}: expected a mapping"
            )
        return raw

    def resolve_profile(self, profile_id: str | None = None) -> RoleBindings:
        selected = str(
            profile_id or self.configuration.get("active_execution_profile") or "api"
        )
        raw = self._raw_profile(selected)
        bindings_value = raw.get("bindings") if "bindings" in raw else raw
        if not isinstance(bindings_value, Mapping):
            raise RoleBindingConfigurationError(
                f"execution_profiles.{selected}.bindings: expected a mapping"
            )
        canonical: dict[str, Any] = {
            str(key): value for key, value in bindings_value.items()
        }
        document: dict[str, Any] = {
            "profile_id": raw.get("profile_id", selected),
            "bindings": canonical,
        }
        if "schema_version" in raw:
            document["schema_version"] = raw["schema_version"]
        try:
            bindings = RoleBindings.model_validate(document)
        except ValidationError as error:
            raise RoleBindingConfigurationError(
                _validation_message(f"execution_profiles.{selected}", error)
            ) from error
        if bindings.profile_id != selected:
            raise RoleBindingConfigurationError(
                f"execution_profiles.{selected}.profile_id: must equal {selected!r}"
            )
        for role in AgentRole:
            system_id = bindings.system_id_for(role)
            system = self.system_by_id.get(system_id)
            path = f"execution_profiles.{selected}.{role.value}"
            if system is None:
                raise RoleBindingConfigurationError(
                    f"{path}: unknown agent-system id {system_id!r}"
                )
            if not system.enabled:
                raise RoleBindingConfigurationError(
                    f"{path}: agent system {system_id!r} is disabled"
                )
            if role not in system.roles:
                declared = ", ".join(sorted(item.value for item in system.roles))
                raise RoleBindingConfigurationError(
                    f"{path}: agent system {system_id!r} does not declare role "
                    f"{role.value!r}; declared roles: {declared}"
                )
        return bindings

    def profile_status(self, profile_id: str) -> ExecutionProfileInspection:
        try:
            bindings = self.resolve_profile(profile_id)
        except RoleBindingConfigurationError as error:
            return ExecutionProfileInspection(
                profile_id=profile_id,
                status="invalid",
                runnable=False,
                reasons=[str(error)],
            )
        reasons: list[str] = []
        for role in AgentRole:
            system_id = bindings.system_id_for(role)
            inspection = self.inspect_system(system_id)
            if not inspection.runnable:
                reasons.append(f"{role.value}: {inspection.reason}")
        return ExecutionProfileInspection(
            profile_id=profile_id,
            status="ready" if not reasons else "unavailable",
            runnable=not reasons,
            bindings=bindings,
            reasons=reasons,
        )

    def list_profiles(self) -> tuple[ExecutionProfileInspection, ...]:
        return tuple(
            self.profile_status(profile_id) for profile_id in sorted(self.raw_profiles)
        )

    def require_runnable(self, bindings: RoleBindings) -> None:
        status = self.profile_status(bindings.profile_id)
        if not status.runnable:
            reason = "; ".join(status.reasons) or "profile integration is unavailable"
            raise RoleBindingConfigurationError(
                f"execution profile {bindings.profile_id!r} is unavailable: {reason}; "
                "Villani will not fall back to another profile"
            )

    def invocation_identity(
        self, bindings: RoleBindings, role: AgentRole
    ) -> AgentInvocationIdentity:
        system = self.inspect_configured(bindings.system_id_for(role))
        inspection = self.inspect_system(system.id)
        configuration = _system_document(system)
        digest, safe_configuration, removed = configuration_digest(configuration)
        if removed:
            raise RoleBindingConfigurationError(
                f"agent system {system.id!r} contains a secret value"
            )
        if isinstance(system, ApiAgentSystemConfig):
            implementation_id = (
                f"api_backend:{system.existing_backend_reference}"
                if system.existing_backend_reference
                else f"api_provider:{system.provider}"
            )
            provider = system.provider
            model = system.model
            driver = None
            executable = None
        elif isinstance(system, InternalRunnerSystemConfig):
            implementation_id = f"internal_runner:{system.runner}"
            provider = None
            model = None
            driver = None
            executable = None
        else:
            implementation_id = f"cli_driver:{system.driver}"
            provider = None
            model = system.model
            driver = system.driver
            executable = system.executable
        identity_source = json.dumps(
            {
                "profile_id": bindings.profile_id,
                "role": role.value,
                "agent_system_id": system.id,
                "configuration_digest": digest,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return AgentInvocationIdentity(
            invocation_id=f"ainv_{hashlib.sha256(identity_source).hexdigest()}",
            profile_id=bindings.profile_id,
            role=role,
            agent_system_id=system.id,
            system_kind=system.kind,
            implementation_id=implementation_id,
            provider=provider,
            model=model,
            driver=driver,
            executable=executable,
            timeout_seconds=system.timeout_seconds,
            max_parallel=system.max_parallel,
            availability="ready" if inspection.runnable else "unavailable",
            unavailable_reason=None if inspection.runnable else inspection.reason,
            configuration_digest=digest,
            configuration=safe_configuration,
        )

    def invocation_identities(
        self, bindings: RoleBindings
    ) -> tuple[AgentInvocationIdentity, ...]:
        return tuple(self.invocation_identity(bindings, role) for role in AgentRole)

    def backend_reference(self, bindings: RoleBindings, role: AgentRole) -> str | None:
        system = self.inspect_configured(bindings.system_id_for(role))
        return (
            system.existing_backend_reference
            if isinstance(system, ApiAgentSystemConfig)
            else None
        )


__all__ = [
    "KNOWN_INTERNAL_RUNNERS",
    "RoleBindingConfigurationError",
    "RoleSystemRegistry",
]
