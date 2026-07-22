"""Role-specific factories over the neutral agent-system registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, cast

from villani_ops.closed_loop.interfaces import (
    AttemptRunner,
    Classifier,
    Selector,
    Verifier,
)

from .role_models import (
    AgentRole,
    ApiAgentSystemConfig,
    CliAgentSystemConfig,
    InternalRunnerSystemConfig,
    RoleBindings,
)
from .role_registry import RoleBindingConfigurationError, RoleSystemRegistry


class AgentSystemIntegrationUnavailable(RoleBindingConfigurationError):
    """A valid configuration has no implementation in this milestone."""


@dataclass(frozen=True, slots=True)
class RoleFactoryDependencies:
    api_classifiers: Mapping[str, Classifier] = field(default_factory=dict)
    api_attempt_runners: Mapping[str, AttemptRunner] = field(default_factory=dict)
    api_verifiers: Mapping[str, Verifier] = field(default_factory=dict)
    api_selectors: Mapping[str, Selector] = field(default_factory=dict)
    internal_classifiers: Mapping[str, Classifier] = field(default_factory=dict)
    internal_attempt_runners: Mapping[str, AttemptRunner] = field(default_factory=dict)
    internal_verifiers: Mapping[str, Verifier] = field(default_factory=dict)
    internal_selectors: Mapping[str, Selector] = field(default_factory=dict)
    cli_classifiers: Mapping[str, Classifier] = field(default_factory=dict)
    cli_attempt_runners: Mapping[str, AttemptRunner] = field(default_factory=dict)
    cli_verifiers: Mapping[str, Verifier] = field(default_factory=dict)
    cli_selectors: Mapping[str, Selector] = field(default_factory=dict)


def _implementation(
    role: AgentRole,
    role_binding: RoleBindings,
    registry: RoleSystemRegistry,
    *,
    api: Mapping[str, Any],
    internal: Mapping[str, Any],
    cli: Mapping[str, Any] | None = None,
) -> Any:
    system_id = role_binding.system_id_for(role)
    system = registry.inspect_configured(system_id)
    if role not in system.roles:
        raise RoleBindingConfigurationError(
            f"agent system {system_id!r} does not declare role {role.value!r}"
        )
    if not system.enabled:
        raise RoleBindingConfigurationError(
            f"agent system {system_id!r} is disabled for role {role.value!r}"
        )
    if isinstance(system, CliAgentSystemConfig):
        if system.driver in {"codex", "claude_code"}:
            implementation = (cli or {}).get(system.id)
            if implementation is not None:
                return implementation
        raise AgentSystemIntegrationUnavailable(
            f"execution profile {role_binding.profile_id!r} binds {role.value} to "
            f"{system.driver} CLI system {system.id!r}, but CLI driver integration "
            "is unavailable for this role; no fallback was selected"
        )
    if isinstance(system, ApiAgentSystemConfig):
        reference = system.existing_backend_reference
        if reference is None:
            raise AgentSystemIntegrationUnavailable(
                f"API agent system {system.id!r} has no existing_backend_reference "
                f"for role {role.value!r}"
            )
        implementation = api.get(reference)
        if implementation is None:
            raise AgentSystemIntegrationUnavailable(
                f"API agent system {system.id!r} references backend {reference!r}, "
                f"which has no {role.value} factory implementation"
            )
        return implementation
    if isinstance(system, InternalRunnerSystemConfig):
        implementation = internal.get(system.runner)
        if implementation is None:
            raise AgentSystemIntegrationUnavailable(
                f"internal runner {system.runner!r} has no {role.value} factory "
                "implementation"
            )
        return implementation
    raise TypeError(f"unsupported agent-system configuration {type(system).__name__}")


def build_classifier(
    role_binding: RoleBindings,
    registry: RoleSystemRegistry,
    dependencies: RoleFactoryDependencies,
) -> Classifier:
    return cast(
        Classifier,
        _implementation(
            AgentRole.CLASSIFICATION,
            role_binding,
            registry,
            api=dependencies.api_classifiers,
            internal=dependencies.internal_classifiers,
            cli=dependencies.cli_classifiers,
        ),
    )


def build_attempt_runner(
    role_binding: RoleBindings,
    registry: RoleSystemRegistry,
    dependencies: RoleFactoryDependencies,
) -> AttemptRunner:
    return cast(
        AttemptRunner,
        _implementation(
            AgentRole.CODING,
            role_binding,
            registry,
            api=dependencies.api_attempt_runners,
            internal=dependencies.internal_attempt_runners,
            cli=dependencies.cli_attempt_runners,
        ),
    )


def build_verifier(
    role_binding: RoleBindings,
    registry: RoleSystemRegistry,
    dependencies: RoleFactoryDependencies,
) -> Verifier:
    return cast(
        Verifier,
        _implementation(
            AgentRole.VERIFICATION,
            role_binding,
            registry,
            api=dependencies.api_verifiers,
            internal=dependencies.internal_verifiers,
            cli=dependencies.cli_verifiers,
        ),
    )


def build_selector(
    role_binding: RoleBindings,
    registry: RoleSystemRegistry,
    dependencies: RoleFactoryDependencies,
) -> Selector:
    return cast(
        Selector,
        _implementation(
            AgentRole.SELECTION,
            role_binding,
            registry,
            api=dependencies.api_selectors,
            internal=dependencies.internal_selectors,
            cli=dependencies.cli_selectors,
        ),
    )


__all__ = [
    "AgentSystemIntegrationUnavailable",
    "RoleFactoryDependencies",
    "build_attempt_runner",
    "build_classifier",
    "build_selector",
    "build_verifier",
]
