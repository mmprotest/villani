from __future__ import annotations

from typing import cast

from villani_ops.closed_loop.agent_systems.factories import (
    RoleFactoryDependencies,
    build_attempt_runner,
)
from villani_ops.closed_loop.agent_systems.role_registry import RoleSystemRegistry
from villani_ops.closed_loop.interfaces import AttemptRunner


def test_coding_role_can_resolve_codex_without_rebinding_other_roles() -> None:
    """Regression: Milestone 1 could describe this hybrid but not construct it."""

    codex_runner = cast(AttemptRunner, object())
    configuration = {
        "agent_systems": {
            "systems": {
                "api-roles": {
                    "kind": "api",
                    "id": "api-roles",
                    "enabled": True,
                    "provider": "fixture",
                    "model": "fixture",
                    "roles": ["classification", "verification", "selection"],
                    "existing_backend_reference": "fixture",
                    "timeout_seconds": 60,
                    "max_parallel": 1,
                    "metadata": {},
                },
                "codex-coder": {
                    "kind": "cli_agent",
                    "id": "codex-coder",
                    "enabled": True,
                    "driver": "codex",
                    "executable": "codex",
                    "model": "fixture-model",
                    "roles": ["coding"],
                    "timeout_seconds": 60,
                    "max_parallel": 1,
                    "instruction_policy": "native_project",
                    "permission_profile": "workspace_write",
                    "environment_policy": "inherit",
                    "provider_options": {},
                },
            }
        },
        "execution_profiles": {
            "hybrid": {
                "classification": "api-roles",
                "coding": "codex-coder",
                "verification": "api-roles",
                "selection": "api-roles",
            }
        },
    }
    registry = RoleSystemRegistry(configuration, {})
    bindings = registry.resolve_profile("hybrid")
    dependencies = RoleFactoryDependencies(
        cli_attempt_runners={"codex-coder": codex_runner}
    )

    assert build_attempt_runner(bindings, registry, dependencies) is codex_runner
