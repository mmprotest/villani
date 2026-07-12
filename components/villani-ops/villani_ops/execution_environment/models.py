"""Typed execution-environment configuration and durable reports."""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping

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

    def durable_report(self) -> dict[str, Any]:
        report = self.model_dump(mode="json", exclude={"environment"})
        setup = report.get("setup_result")
        if isinstance(setup, dict):
            # Counts and truncation evidence are durable; command output may contain credentials.
            setup["stdout"] = ""
            setup["stderr"] = ""
            setup["content_persisted"] = False
        return report
