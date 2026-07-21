from __future__ import annotations

from villani_ops.closed_loop.agent_systems.factories import (
    RoleFactoryDependencies,
    build_attempt_runner,
)
from villani_ops.closed_loop.agent_systems.role_models import AgentRole
from villani_ops.closed_loop.agent_systems.role_registry import RoleSystemRegistry


def test_coding_role_can_resolve_claude_without_rebinding_other_roles() -> None:
    claude_runner = object()
    configuration = {
        "agent_systems": {
            "systems": {
                "api-all": {
                    "kind": "api",
                    "id": "api-all",
                    "enabled": True,
                    "provider": "fixture",
                    "model": "fixture-api",
                    "roles": [role.value for role in AgentRole],
                    "existing_backend_reference": "fixture",
                    "timeout_seconds": 60,
                    "max_parallel": 1,
                    "metadata": {},
                },
                "claude-coder": {
                    "kind": "cli_agent",
                    "id": "claude-coder",
                    "enabled": True,
                    "driver": "claude_code",
                    "executable": "claude",
                    "model": "claude-fixture",
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
                "classification": "api-all",
                "coding": "claude-coder",
                "verification": "api-all",
                "selection": "api-all",
            }
        },
    }
    registry = RoleSystemRegistry(configuration, {})
    dependencies = RoleFactoryDependencies(
        cli_attempt_runners={"claude-coder": claude_runner}  # type: ignore[dict-item]
    )

    resolved = build_attempt_runner(
        registry.resolve_profile("hybrid"), registry, dependencies
    )

    assert resolved is claude_runner
