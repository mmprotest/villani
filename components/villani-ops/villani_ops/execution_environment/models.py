"""Typed execution-environment configuration and durable reports."""

from __future__ import annotations

import hashlib
import json
import platform
import re
from typing import Any, Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetupLimits(StrictModel):
    timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    stdout_bytes: int = Field(default=1_048_576, ge=1)
    stderr_bytes: int = Field(default=1_048_576, ge=1)
    disk_bytes: int = Field(default=2_147_483_648, ge=1)
    process_count: int = Field(default=32, ge=1, le=1024)
    cpu_count: float = Field(default=2.0, gt=0, le=256)
    memory_bytes: int = Field(default=2_147_483_648, ge=16_777_216)
    tmpfs_bytes: int = Field(default=268_435_456, ge=1_048_576)


class NetworkPolicy(StrictModel):
    mode: Literal["inherit", "deny", "allowlist"] | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    denied_hosts: list[str] = Field(default_factory=list)
    proxy_url: str | None = None
    proxy_network: str | None = None
    proxy_boundary_verified: bool = False

    @model_validator(mode="after")
    def validate_boundary(self) -> "NetworkPolicy":
        if self.mode == "allowlist" and (
            not self.proxy_url
            or not self.proxy_network
            or not self.proxy_boundary_verified
        ):
            raise ValueError(
                "allowlist network policy requires proxy_url, proxy_network, and proxy_boundary_verified: true"
            )
        return self


class ActionPolicy(StrictModel):
    command_allow: list[str] = Field(default_factory=list)
    command_deny: list[str] = Field(default_factory=list)
    path_allow: list[str] = Field(default_factory=list)
    path_deny: list[str] = Field(default_factory=list)
    domain_allow: list[str] = Field(default_factory=list)
    domain_deny: list[str] = Field(default_factory=list)
    allow_symlinks: bool = False
    max_file_bytes: int = Field(default=67_108_864, ge=1)
    max_archive_entries: int = Field(default=10_000, ge=1)
    max_archive_uncompressed_bytes: int = Field(default=536_870_912, ge=1)
    max_archive_ratio: float = Field(default=200.0, ge=1)


class ContainerSettings(StrictModel):
    engine: Literal["auto", "docker", "podman"] = "auto"
    image: str | None = None
    user: str | None = None
    workspace_target: str = "/workspace"
    read_only_root: bool = True
    temporary_filesystem: bool = True
    network: NetworkPolicy = NetworkPolicy()
    storage_opt_size: bool = False


class DevcontainerSettings(StrictModel):
    cli: str = "devcontainer"
    config_path: str | None = None
    engine: Literal["auto", "docker", "podman"] = "auto"


class SecretRequest(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    source: Literal["environment", "command"] = "environment"
    environment_variable: str | None = None
    command_argv: list[str] | None = None
    target: Literal["environment", "file"] = "environment"
    target_name: str | None = None
    required: bool = True

    @model_validator(mode="after")
    def validate_source(self) -> "SecretRequest":
        if self.source == "command" and not self.command_argv:
            raise ValueError("command secret source requires command_argv")
        if self.source == "environment" and self.command_argv:
            raise ValueError("environment secret source cannot configure command_argv")
        target_name = self.target_name or self.name
        if self.target == "file" and (
            target_name in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", target_name)
        ):
            raise ValueError("secret target_name must be a portable file name")
        if self.target == "environment" and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", target_name
        ):
            raise ValueError("environment secret target_name must be a variable name")
        return self


class ExecutionEnvironmentConfig(StrictModel):
    provider: Literal["inherit", "setup-command", "container", "devcontainer"] = (
        "inherit"
    )
    mode: Literal["local", "controlled", "remote"] = "local"
    denied_variables: list[str] = Field(default_factory=list)
    sensitive_variables: list[str] = Field(default_factory=list)
    private_paths: list[str] = Field(default_factory=list)
    setup_argv: list[str] | None = None
    shell: bool = False
    shell_command: str | None = None
    limits: SetupLimits = SetupLimits()
    cache: bool = True
    required: bool = True
    container: ContainerSettings = ContainerSettings()
    devcontainer: DevcontainerSettings = DevcontainerSettings()
    policy: ActionPolicy = ActionPolicy()
    secrets: list[SecretRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_setup(self) -> "ExecutionEnvironmentConfig":
        if self.provider in {"inherit", "container", "devcontainer"}:
            if self.setup_argv or self.shell_command or self.shell:
                raise ValueError(
                    f"{self.provider} provider cannot configure a setup command"
                )
            if self.provider == "container" and not self.container.image:
                raise ValueError("container provider requires container.image")
            network = self.container.network
            if network.mode is None:
                network.mode = "inherit" if self.mode == "local" else "deny"
            if network.mode == "inherit" and any(
                (
                    network.allowed_domains,
                    network.allowed_hosts,
                    network.denied_domains,
                    network.denied_hosts,
                    self.policy.domain_allow,
                    self.policy.domain_deny,
                )
            ):
                raise ValueError(
                    "domain policies require network mode deny or an enforced allowlist proxy boundary"
                )
            return self
        if self.shell:
            if not self.shell_command:
                raise ValueError("shell_command is required when shell is true")
            if self.setup_argv:
                raise ValueError("setup_argv and shell_command are mutually exclusive")
        elif not self.setup_argv:
            raise ValueError("setup_argv is required for shell-free setup-command")
        elif self.shell_command:
            raise ValueError("shell_command requires shell: true")
        return self

    @classmethod
    def from_configuration(
        cls, configuration: Mapping[str, Any], selection: str | None = None
    ) -> "ExecutionEnvironmentConfig":
        if selection:
            configured = configuration.get("execution_environments")
            if not isinstance(configured, Mapping) or selection not in configured:
                raise ValueError(
                    f"execution environment {selection!r} is not configured"
                )
            raw = configured[selection]
        else:
            raw = configuration.get("execution_environment", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError("execution_environment must be a mapping")
        value = dict(raw)
        # Accept the natural nested spelling while keeping one strict internal model.
        setup = value.pop("setup_command", None)
        if setup is not None:
            if not isinstance(setup, Mapping):
                raise ValueError(
                    "execution_environment.setup_command must be a mapping"
                )
            nested = dict(setup)
            if "argv" in nested:
                value["setup_argv"] = nested.pop("argv")
            if "command" in nested:
                value["shell_command"] = nested.pop("command")
            for key in ("shell", "limits", "cache"):
                if key in nested:
                    value[key] = nested.pop(key)
            if nested:
                raise ValueError(
                    f"unknown setup_command keys: {', '.join(sorted(nested))}"
                )
        return cls.model_validate(value)


class EnvironmentRemoval(StrictModel):
    name: str
    reason: Literal[
        "sensitive", "denied", "villani_private_path", "villani_private_variable"
    ]


class CommandResult(StrictModel):
    exit_code: int
    duration_ms: int = Field(ge=0)
    stdout: str
    stderr: str
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    disk_limit_exceeded: bool
    process_limit_exceeded: bool
    failure_classification: (
        Literal[
            "timeout", "disk_limit", "process_limit", "memory_limit", "policy_denied"
        ]
        | None
    ) = None


CandidateCommandRole: TypeAlias = Literal[
    "coding_attempt",
    "repository_validation",
    "verifier_probe",
    "final_acceptance_validation",
]
CandidateCommandStatus: TypeAlias = Literal[
    "passed",
    "failed",
    "timed_out",
    "infrastructure_error",
    "policy_denied",
]
RepositoryValidationFailureCode: TypeAlias = Literal[
    "repository_validation_passed",
    "repository_validation_test_failure",
    "repository_validation_timeout",
    "repository_validation_executable_missing",
    "repository_validation_environment_mismatch",
    "repository_validation_provider_failure",
    "repository_validation_policy_denied",
    "repository_validation_unavailable",
    "repository_validation_malformed_result",
]
FocusedProbeFailureCode: TypeAlias = Literal[
    "focused_probe_passed",
    "focused_probe_behavior_failure",
    "focused_probe_timeout",
    "focused_probe_executable_missing",
    "focused_probe_environment_mismatch",
    "focused_probe_provider_failure",
    "focused_probe_policy_denied",
    "focused_probe_malformed_result",
]
CandidateCommandFailureCode: TypeAlias = (
    RepositoryValidationFailureCode | FocusedProbeFailureCode
)


class CandidateCommandResult(StrictModel):
    validation_id: str = Field(min_length=1)
    argv: list[str] = Field(min_length=1)
    command_role: CandidateCommandRole
    status: CandidateCommandStatus
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    stdout: str
    stderr: str
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    stdout_truncated: bool
    stderr_truncated: bool
    execution_environment_fingerprint: str = Field(min_length=1)
    execution_provider: str = Field(min_length=1)
    worktree_path: str = Field(min_length=1)
    baseline_sha256: str = Field(min_length=1)
    candidate_state: Literal["post_mutation"]
    started_at: str = Field(min_length=1)
    completed_at: str = Field(min_length=1)
    failure_code: CandidateCommandFailureCode | None


class RepositoryValidationCommandResult(CandidateCommandResult):
    command_role: Literal["repository_validation"]
    failure_code: RepositoryValidationFailureCode | None


class RepositoryValidationReport(StrictModel):
    schema_version: Literal["villani.repository_validation.v2"]
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    execution_environment_fingerprint: str = Field(min_length=1)
    execution_provider: str = Field(min_length=1)
    commands: list[RepositoryValidationCommandResult]
    status: Literal[
        "passed",
        "failed",
        "unavailable",
        "infrastructure_error",
    ]
    authoritative: bool
    completed_at: str = Field(min_length=1)
    retry_count: int = Field(default=0, ge=0)
    failure_code: RepositoryValidationFailureCode | None = None
    authority_source: Literal[
        "repository_validation_v2",
        "legacy_runtime_events",
    ] = "repository_validation_v2"

    @model_validator(mode="after")
    def validate_authority(self) -> "RepositoryValidationReport":
        if self.status == "passed":
            if not self.commands or any(
                item.status != "passed" for item in self.commands
            ):
                raise ValueError(
                    "passed validation requires one or more passed commands"
                )
            if not self.authoritative:
                raise ValueError("passed validation must be authoritative")
        elif self.status == "failed":
            if not any(item.status == "failed" for item in self.commands):
                raise ValueError("failed validation requires a failed command")
            if not self.authoritative:
                raise ValueError("failed validation must be authoritative")
        elif self.authoritative:
            raise ValueError(
                "unavailable and infrastructure-error validation cannot be authoritative"
            )
        return self


class CandidatePatchQuality(StrictModel):
    schema_version: Literal["villani.candidate_patch_quality.v1"]
    candidate_id: str = Field(min_length=1)
    status: Literal["eligible", "ineligible", "warning"]
    tracked_files_changed: list[str]
    relevant_files_changed: list[str]
    untracked_files: list[str]
    ignored_files: list[str]
    villani_owned_files: list[str]
    generated_files: list[str]
    semantic_lines_added: int = Field(ge=0)
    semantic_lines_removed: int = Field(ge=0)
    line_ending_only_lines: int = Field(ge=0)
    whitespace_only_lines: int = Field(ge=0)
    file_mode_only_changes: list[str]
    bulk_rewrite_files: list[str]
    relevant_diff_ratio: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str]


class CandidateBundleManifest(StrictModel):
    schema_version: Literal["villani.candidate.v1"]
    candidate_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    base_commit: str | None
    baseline_sha256: str = Field(min_length=1)
    patch_path: str = Field(min_length=1)
    patch_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    patch_bytes: int = Field(ge=0)
    changed_files: list[str]
    untracked_files: list[str]
    worktree_status: dict[str, Any]
    execution_provider: str = Field(min_length=1)
    execution_environment_fingerprint: str = Field(min_length=1)
    repository_validation_path: str = Field(min_length=1)
    candidate_patch_quality_path: str | None = None
    created_at: str = Field(min_length=1)
    materialization_status: Literal["not_materialized", "succeeded", "failed"]


def _safe_runtime_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    if isinstance(value, list):
        return [_safe_runtime_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_safe_runtime_value(item, secrets) for item in value]
    if isinstance(value, dict):
        mapping_output: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(
                marker in normalized
                for marker in ("api_key", "password", "secret_value", "token_value")
            ):
                mapping_output[str(key)] = "[REDACTED]"
            else:
                mapping_output[str(key)] = _safe_runtime_value(item, secrets)
        return mapping_output
    return value


class PreparedEnvironment(StrictModel):
    provider: Literal["inherit", "setup-command", "container", "devcontainer"]
    provider_version: str
    repository_path: str
    worktree_path: str
    environment: dict[str, str] = Field(exclude=True)
    removals: list[EnvironmentRemoval]
    fingerprint: str
    cache_key: str | None
    cache_hit: bool
    setup_result: CommandResult | None
    inspection: dict[str, Any]
    runtime_state: dict[str, Any] = Field(default_factory=dict)
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    execution_environment_selection: str | None = None
    command_prefix_digest: str | None = None
    configuration_digest: str | None = None

    def durable_report(self) -> dict[str, Any]:
        from .secrets import registered_secret_values

        secrets = registered_secret_values()
        path_value = self.environment.get("PATH", self.environment.get("Path", ""))
        setup_status = (
            "not_configured"
            if self.setup_result is None
            else "passed"
            if self.setup_result.exit_code == 0
            else "failed"
        )
        runtime_state = _safe_runtime_value(self.runtime_state, secrets)
        policy_decisions = _safe_runtime_value(self.policy_decisions, secrets)
        report = self.model_dump(
            mode="json",
            exclude={
                "environment",
                "runtime_state",
                "policy_decisions",
                "execution_environment_selection",
                "command_prefix_digest",
                "configuration_digest",
            },
        )
        setup = report.get("setup_result")
        if isinstance(setup, dict):
            # Counts and truncation evidence are durable; command output may contain credentials.
            setup["stdout"] = ""
            setup["stderr"] = ""
            setup["content_persisted"] = False
        return {
            "schema_version": "villani.execution_environment.v2",
            **report,
            "execution_environment_selection": (
                self.execution_environment_selection or self.provider
            ),
            "setup_status": setup_status,
            "runtime_state_summary": runtime_state,
            # Preserve the pre-v2 key for existing readers while keeping the
            # same redacted summary as the new descriptor.
            "runtime_state": runtime_state,
            "policy_decisions": policy_decisions,
            "command_prefix_digest": self.command_prefix_digest
            or hashlib.sha256(b"[]").hexdigest(),
            "environment_variable_names": sorted(self.environment),
            "path_digest": hashlib.sha256(path_value.encode()).hexdigest(),
            "configuration_digest": self.configuration_digest
            or hashlib.sha256(
                json.dumps(
                    {"provider": self.provider},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
        }
