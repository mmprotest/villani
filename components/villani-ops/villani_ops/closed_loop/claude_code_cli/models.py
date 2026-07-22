"""Versioned, secret-free contracts for the Claude Code coding adapter."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CLAUDE_CODER_RESULT_SCHEMA_VERSION = "villani.claude_coder_result.v1"
CLAUDE_PROBE_SCHEMA_VERSION = "villani.claude_code_probe.v1"
CLAUDE_PROVIDER_IDENTITY_SCHEMA_VERSION = "villani.claude_code_provider_identity.v1"


class StrictClaudeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaudeFailure(str, Enum):
    NOT_INSTALLED = "claude_not_installed"
    NOT_AUTHENTICATED = "claude_not_authenticated"
    UNSUPPORTED_VERSION = "unsupported_claude_version"
    UNSUPPORTED_REQUIRED_CAPABILITY = "unsupported_required_capability"
    MISSING_STRUCTURED_OUTPUT_CAPABILITY = (
        "missing_required_structured_output_capability"
    )
    MODEL_UNAVAILABLE = "model_unavailable"
    PERMISSION_DENIED = "permission_denied"
    TOOL_DENIED = "tool_denied"
    AMBIENT_STARTUP_FAILURE = "mcp_plugin_hook_startup_failure"
    PROVIDER_AUTHENTICATION_FAILURE = "provider_authentication_failure"
    PROVIDER_RATE_LIMIT_OR_OVERLOAD = "provider_rate_limit_or_overload"
    INVALID_JSON = "invalid_json"
    MISSING_FINAL_RESULT = "missing_final_result"
    JSON_SCHEMA_FAILURE = "json_schema_failure"
    PROCESS_TIMEOUT = "process_timeout"
    PROCESS_CANCELLATION = "process_cancellation"
    PROCESS_CRASH = "process_crash"
    COMPLETED_NO_PATCH = "coding_completed_with_no_patch"
    PATH_VIOLATION = "path_violation"
    CLEANUP_FAILURE = "cleanup_failure"


class ClaudeReportedTest(StrictClaudeModel):
    command: str = Field(min_length=1)
    reported_exit_status: int | None
    reported_result: str


class ClaudeCoderResult(StrictClaudeModel):
    """Supplementary agent report; Git and verifier evidence remain canonical."""

    schema_version: Literal["villani.claude_coder_result.v1"] = (
        "villani.claude_coder_result.v1"
    )
    status: Literal["completed", "blocked"]
    summary: str
    tests_run: list[ClaudeReportedTest] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    files_the_agent_believes_changed: list[str] = Field(default_factory=list)


class ClaudeProbeResult(StrictClaudeModel):
    schema_version: Literal["villani.claude_code_probe.v1"] = (
        "villani.claude_code_probe.v1"
    )
    system_id: str = Field(min_length=1)
    checked_at: datetime
    configured_executable: str = Field(min_length=1)
    resolved_executable: str | None
    exact_version_output: str | None
    parsed_version: str | None
    authentication_ready: bool
    authentication_method: Literal[
        "claude_ai",
        "api_key",
        "authenticated_unspecified",
        "not_authenticated",
        "unknown",
    ]
    doctor_ready: bool
    capabilities: dict[str, bool]
    resolved_flags: dict[str, str]
    ready: bool
    failures: list[ClaudeFailure] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_readiness(self) -> "ClaudeProbeResult":
        if self.ready and self.failures:
            raise ValueError("a ready Claude Code probe cannot contain failures")
        if self.ready and (
            not self.resolved_executable
            or not self.authentication_ready
            or not self.doctor_ready
        ):
            raise ValueError(
                "a ready Claude Code probe requires executable, authentication, and doctor readiness"
            )
        return self


class ClaudeProviderIdentity(StrictClaudeModel):
    schema_version: Literal["villani.claude_code_provider_identity.v1"] = (
        "villani.claude_code_provider_identity.v1"
    )
    system_id: str = Field(min_length=1)
    driver: Literal["claude_code"] = "claude_code"
    provider: Literal["anthropic"] = "anthropic"
    resolved_executable: str = Field(min_length=1)
    executable_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    exact_version_output: str = Field(min_length=1)
    parsed_version: str = Field(min_length=1)
    configured_model: str = Field(min_length=1)
    reported_model: str | None = None
    session_id: str | None = None
    authentication_ready: bool
    authentication_method: Literal[
        "claude_ai",
        "api_key",
        "authenticated_unspecified",
        "not_authenticated",
        "unknown",
    ]
    capabilities: dict[str, bool]
    resolved_flags: dict[str, str]
    instruction_policy: Literal["native_project", "villani_controlled"]
    permission_profile: str = Field(min_length=1)
    permission_mode: Literal["acceptEdits"] = "acceptEdits"
    allowed_tools: tuple[str, ...]
    environment_policy: str = Field(min_length=1)
    no_session_persistence: Literal[True] = True
    project_user_discovery_permitted: bool
    disabled_ambient_features: tuple[str, ...]
    reported_tools: tuple[str, ...] = ()
    reported_mcp_servers: tuple[str, ...] = ()
    reported_plugins: tuple[str, ...] = ()
    billing_identity: Literal["not_reported"] = "not_reported"
    probed_at: datetime


__all__ = [
    "CLAUDE_CODER_RESULT_SCHEMA_VERSION",
    "CLAUDE_PROBE_SCHEMA_VERSION",
    "CLAUDE_PROVIDER_IDENTITY_SCHEMA_VERSION",
    "ClaudeCoderResult",
    "ClaudeFailure",
    "ClaudeProbeResult",
    "ClaudeProviderIdentity",
    "ClaudeReportedTest",
]
