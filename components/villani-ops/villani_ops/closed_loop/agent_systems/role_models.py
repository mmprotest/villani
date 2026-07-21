"""Neutral agent-system configuration and role-binding contracts.

These models sit below the controller's role-specific ports.  They describe
which complete system supplies a role without replacing those typed ports with
one generic callback.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_serializer,
    model_validator,
)

from .models import configuration_digest, non_secret_configuration


AGENT_SYSTEM_CONFIG_SCHEMA_VERSION: Final = "villani.agent_system_config.v1"
ROLE_BINDINGS_SCHEMA_VERSION: Final = "villani.role_bindings.v1"
AGENT_INVOCATION_IDENTITY_SCHEMA_VERSION: Final = "villani.agent_invocation_identity.v1"

SYSTEM_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
INVOCATION_ID_PATTERN = r"^ainv_[0-9a-f]{64}$"


class StrictRoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_serializer("roles", check_fields=False)
    def serialize_roles(self, roles: set["AgentRole"]) -> list[str]:
        return sorted(role.value for role in roles)


class AgentRole(str, Enum):
    CLASSIFICATION = "classification"
    CODING = "coding"
    VERIFICATION = "verification"
    SELECTION = "selection"


REQUIRED_AGENT_ROLES = frozenset(AgentRole)


def _reject_secret_configuration(value: BaseModel) -> None:
    _projection, removed = non_secret_configuration(value.model_dump(mode="json"))
    if removed:
        raise ValueError(
            "agent-system configuration contains a secret value; store only a "
            "secret reference such as an *_env or *_ref field"
        )


class ApiAgentSystemConfig(StrictRoleModel):
    kind: Literal["api"]
    id: str = Field(pattern=SYSTEM_ID_PATTERN)
    enabled: bool = True
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    roles: set[AgentRole] = Field(min_length=1)
    existing_backend_reference: str | None = Field(default=None, min_length=1)
    timeout_seconds: int = Field(default=180, ge=1)
    max_parallel: int = Field(default=1, ge=1, le=32)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_secrets(self) -> "ApiAgentSystemConfig":
        _reject_secret_configuration(self)
        return self


class InternalRunnerSystemConfig(StrictRoleModel):
    kind: Literal["internal_runner"]
    id: str = Field(pattern=SYSTEM_ID_PATTERN)
    enabled: bool = True
    runner: str = Field(min_length=1)
    roles: set[AgentRole] = Field(min_length=1)
    timeout_seconds: int = Field(default=180, ge=1)
    max_parallel: int = Field(default=1, ge=1, le=32)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_secrets(self) -> "InternalRunnerSystemConfig":
        _reject_secret_configuration(self)
        return self


class CliAgentSystemConfig(StrictRoleModel):
    kind: Literal["cli_agent"]
    id: str = Field(pattern=SYSTEM_ID_PATTERN)
    enabled: bool = True
    driver: Literal["codex", "claude_code"]
    executable: str = Field(min_length=1)
    model: str = Field(min_length=1)
    roles: set[AgentRole] = Field(min_length=1)
    timeout_seconds: int = Field(default=180, ge=1)
    max_parallel: int = Field(default=1, ge=1, le=32)
    instruction_policy: Literal["native_project", "villani_controlled"]
    permission_profile: str = Field(min_length=1)
    environment_policy: str = Field(min_length=1)
    provider_options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_secrets(self) -> "CliAgentSystemConfig":
        _reject_secret_configuration(self)
        return self


AgentSystemConfig = Annotated[
    ApiAgentSystemConfig | InternalRunnerSystemConfig | CliAgentSystemConfig,
    Field(discriminator="kind"),
]
AGENT_SYSTEM_CONFIG_ADAPTER: TypeAdapter[AgentSystemConfig] = TypeAdapter(
    AgentSystemConfig
)


class AgentSystemCatalog(StrictRoleModel):
    schema_version: Literal["villani.agent_system_config.v1"] = (
        AGENT_SYSTEM_CONFIG_SCHEMA_VERSION
    )
    systems: list[AgentSystemConfig]

    @model_validator(mode="after")
    def unique_system_ids(self) -> "AgentSystemCatalog":
        seen: set[str] = set()
        duplicates: set[str] = set()
        for system in self.systems:
            if system.id in seen:
                duplicates.add(system.id)
            seen.add(system.id)
        if duplicates:
            joined = ", ".join(repr(item) for item in sorted(duplicates))
            raise ValueError(f"duplicate agent-system id(s): {joined}")
        return self


class RoleBindings(StrictRoleModel):
    schema_version: Literal["villani.role_bindings.v1"] = ROLE_BINDINGS_SCHEMA_VERSION
    profile_id: str = Field(min_length=1, pattern=SYSTEM_ID_PATTERN)
    bindings: dict[AgentRole, str]

    @model_validator(mode="after")
    def require_complete_roles(self) -> "RoleBindings":
        actual = set(self.bindings)
        missing = sorted(role.value for role in REQUIRED_AGENT_ROLES - actual)
        extra = sorted(str(role) for role in actual - REQUIRED_AGENT_ROLES)
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append(
                    f"missing required role binding(s): {', '.join(missing)}"
                )
            if extra:
                details.append(f"unknown role binding(s): {', '.join(extra)}")
            raise ValueError("; ".join(details))
        return self

    def system_id_for(self, role: AgentRole) -> str:
        return self.bindings[role]


class AgentInvocationIdentity(StrictRoleModel):
    schema_version: Literal["villani.agent_invocation_identity.v1"] = (
        AGENT_INVOCATION_IDENTITY_SCHEMA_VERSION
    )
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    profile_id: str = Field(min_length=1)
    role: AgentRole
    agent_system_id: str = Field(pattern=SYSTEM_ID_PATTERN)
    system_kind: Literal["api", "internal_runner", "cli_agent"]
    implementation_id: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    driver: Literal["codex", "claude_code"] | None = None
    executable: str | None = None
    timeout_seconds: int = Field(ge=1)
    max_parallel: int = Field(ge=1, le=32)
    availability: Literal["ready", "unavailable"]
    unavailable_reason: str | None = None
    configuration_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    configuration: dict[str, Any]

    @model_validator(mode="after")
    def validate_identity(self) -> "AgentInvocationIdentity":
        digest, projection, removed = configuration_digest(self.configuration)
        if removed or projection != self.configuration:
            raise ValueError("invocation identity configuration must be secret-free")
        if digest != self.configuration_digest:
            raise ValueError(
                "configuration_digest must address the invocation configuration"
            )
        encoded = json.dumps(
            {
                "profile_id": self.profile_id,
                "role": self.role.value,
                "agent_system_id": self.agent_system_id,
                "configuration_digest": self.configuration_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        expected = f"ainv_{hashlib.sha256(encoded).hexdigest()}"
        if self.invocation_id != expected:
            raise ValueError("invocation_id must be derived from the resolved identity")
        if self.availability == "ready" and self.unavailable_reason is not None:
            raise ValueError(
                "ready invocation identities cannot have an unavailable reason"
            )
        if self.availability == "unavailable" and not self.unavailable_reason:
            raise ValueError("unavailable invocation identities require a reason")
        return self


class AgentSystemInspection(StrictRoleModel):
    system: AgentSystemConfig
    status: Literal["ready", "configured", "unavailable", "disabled"]
    runnable: bool
    reason: str


class ExecutionProfileInspection(StrictRoleModel):
    profile_id: str
    status: Literal["ready", "unavailable", "invalid"]
    runnable: bool
    bindings: RoleBindings | None = None
    reasons: list[str] = Field(default_factory=list)


def parse_agent_system(value: Any) -> AgentSystemConfig:
    return AGENT_SYSTEM_CONFIG_ADAPTER.validate_python(value)


__all__ = [
    "AGENT_INVOCATION_IDENTITY_SCHEMA_VERSION",
    "AGENT_SYSTEM_CONFIG_ADAPTER",
    "AGENT_SYSTEM_CONFIG_SCHEMA_VERSION",
    "AgentInvocationIdentity",
    "AgentRole",
    "AgentSystemCatalog",
    "AgentSystemConfig",
    "AgentSystemInspection",
    "ApiAgentSystemConfig",
    "CliAgentSystemConfig",
    "ExecutionProfileInspection",
    "InternalRunnerSystemConfig",
    "REQUIRED_AGENT_ROLES",
    "ROLE_BINDINGS_SCHEMA_VERSION",
    "RoleBindings",
    "parse_agent_system",
]
