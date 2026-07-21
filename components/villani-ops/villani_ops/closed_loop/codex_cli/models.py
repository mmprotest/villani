"""Versioned, secret-free contracts for the Codex coding adapter."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CODEX_CODER_RESULT_SCHEMA_VERSION = "villani.codex_coder_result.v1"
CODEX_PROBE_SCHEMA_VERSION = "villani.codex_probe.v1"
CODEX_PROVIDER_IDENTITY_SCHEMA_VERSION = "villani.codex_provider_identity.v1"


class StrictCodexModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodexFailure(str, Enum):
    NOT_INSTALLED = "codex_not_installed"
    NOT_AUTHENTICATED = "codex_not_authenticated"
    UNSUPPORTED_VERSION = "unsupported_codex_version"
    UNSUPPORTED_REQUIRED_FLAG = "unsupported_required_flag"
    MODEL_UNAVAILABLE = "model_unavailable"
    PERMISSION_SANDBOX_FAILURE = "permission_sandbox_failure"
    PROVIDER_AUTHENTICATION_FAILURE = "provider_authentication_failure"
    PROVIDER_RATE_LIMIT_OR_OVERLOAD = "provider_rate_limit_or_overload"
    MALFORMED_JSONL = "malformed_jsonl"
    MISSING_FINAL_STRUCTURED_OUTPUT = "missing_final_structured_output"
    STRUCTURED_OUTPUT_SCHEMA_FAILURE = "structured_output_schema_failure"
    PROCESS_TIMEOUT = "process_timeout"
    PROCESS_CANCELLATION = "process_cancellation"
    PROCESS_CRASH = "process_crash"
    COMPLETED_NO_PATCH = "coding_completed_with_no_patch"
    PATH_VIOLATION = "path_violation"
    CLEANUP_FAILURE = "cleanup_failure"


class CodexReportedTest(StrictCodexModel):
    command: str = Field(min_length=1)
    reported_exit_status: int | None
    reported_result: str


class CodexCoderResult(StrictCodexModel):
    """Supplementary agent report; Git and verifier evidence remain canonical."""

    schema_version: Literal["villani.codex_coder_result.v1"] = (
        "villani.codex_coder_result.v1"
    )
    status: Literal["completed", "blocked"]
    summary: str
    tests_run: list[CodexReportedTest] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    files_the_agent_believes_changed: list[str] = Field(default_factory=list)


class CodexProbeResult(StrictCodexModel):
    schema_version: Literal["villani.codex_probe.v1"] = "villani.codex_probe.v1"
    system_id: str = Field(min_length=1)
    checked_at: datetime
    configured_executable: str = Field(min_length=1)
    resolved_executable: str | None
    exact_version_output: str | None
    authentication_ready: bool
    authentication_method: Literal[
        "chatgpt",
        "api_key",
        "authenticated_unspecified",
        "not_authenticated",
        "unknown",
    ]
    capabilities: dict[str, bool]
    ready: bool
    failures: list[CodexFailure] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_readiness(self) -> "CodexProbeResult":
        if self.ready and self.failures:
            raise ValueError("a ready Codex probe cannot contain failures")
        if self.ready and (
            not self.resolved_executable or not self.authentication_ready
        ):
            raise ValueError(
                "a ready Codex probe requires executable and authentication"
            )
        return self


class CodexProviderIdentity(StrictCodexModel):
    schema_version: Literal["villani.codex_provider_identity.v1"] = (
        "villani.codex_provider_identity.v1"
    )
    system_id: str = Field(min_length=1)
    driver: Literal["codex"] = "codex"
    resolved_executable: str = Field(min_length=1)
    executable_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    exact_version_output: str = Field(min_length=1)
    model: str = Field(min_length=1)
    authentication_ready: bool
    authentication_method: Literal[
        "chatgpt",
        "api_key",
        "authenticated_unspecified",
        "not_authenticated",
        "unknown",
    ]
    capabilities: dict[str, bool]
    instruction_policy: Literal["native_project", "villani_controlled"]
    permission_profile: str = Field(min_length=1)
    environment_policy: str = Field(min_length=1)
    sandbox: Literal["workspace-write"] = "workspace-write"
    approval_behavior: Literal["never"] = "never"
    billing_identity: Literal["not_reported"] = "not_reported"
    probed_at: datetime


__all__ = [
    "CODEX_CODER_RESULT_SCHEMA_VERSION",
    "CODEX_PROBE_SCHEMA_VERSION",
    "CODEX_PROVIDER_IDENTITY_SCHEMA_VERSION",
    "CodexCoderResult",
    "CodexFailure",
    "CodexProbeResult",
    "CodexProviderIdentity",
    "CodexReportedTest",
]
