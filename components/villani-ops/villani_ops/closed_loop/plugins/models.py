"""Versioned, data-only contracts for closed-loop plugins."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PLUGIN_RPC_VERSION = "villani.plugin.rpc.v1"


class StrictPluginModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PluginKind(str, Enum):
    AGENT_RUNNER = "agent_runner"
    VERIFIER = "verifier"
    SELECTOR = "selector"
    MATERIALIZER = "materializer"
    EXECUTION_PROVIDER = "execution_provider"


PROTOCOL_VERSIONS: dict[PluginKind, str] = {
    PluginKind.AGENT_RUNNER: "villani.agent_runner.v1",
    PluginKind.VERIFIER: "villani.verifier_plugin.v1",
    PluginKind.SELECTOR: "villani.selector_plugin.v1",
    PluginKind.MATERIALIZER: "villani.materializer_plugin.v1",
    PluginKind.EXECUTION_PROVIDER: "villani.execution_provider.v1",
}


class ResourceRequirements(StrictPluginModel):
    cpu_count: float | None = Field(default=None, gt=0)
    memory_bytes: int | None = Field(default=None, ge=1)
    disk_bytes: int | None = Field(default=None, ge=1)
    network: Literal["none", "optional", "required"] = "none"
    process_count: int | None = Field(default=None, ge=1)


class PluginManifest(StrictPluginModel):
    """A manifest is inert configuration; validating it never imports plugin code."""

    schema_version: Literal["villani.plugin_manifest.v1"] = "villani.plugin_manifest.v1"
    kind: PluginKind
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    version: str = Field(min_length=1)
    protocol_versions: list[str] = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    configuration_schema: dict[str, Any]
    required_secrets: list[str] = Field(default_factory=list)
    supported_platforms: list[str] = Field(min_length=1)
    resource_requirements: ResourceRequirements
    trust_level: Literal["built_in_trusted", "local_untrusted", "signed"]
    enabled: bool = False
    transport: Literal["length-prefixed-json", "jsonl", "in-process"] = (
        "length-prefixed-json"
    )
    entrypoint: list[str] | None = None
    artifact_path: str | None = None
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    builtin: bool = False

    @model_validator(mode="after")
    def validate_execution_boundary(self) -> "PluginManifest":
        expected = PROTOCOL_VERSIONS[self.kind]
        if expected not in self.protocol_versions:
            raise ValueError(f"manifest does not support required protocol {expected}")
        if len(set(self.required_secrets)) != len(self.required_secrets):
            raise ValueError("required_secrets must not contain duplicates")
        if self.transport == "in-process":
            if not self.builtin or self.trust_level != "built_in_trusted":
                raise ValueError(
                    "in-process plugins must be built-in trusted implementations"
                )
            if self.entrypoint is not None or self.artifact_path is not None:
                raise ValueError(
                    "in-process plugins cannot declare an external entrypoint"
                )
        elif not self.entrypoint or not self.artifact_path:
            raise ValueError(
                "out-of-process plugins require entrypoint and artifact_path"
            )
        if self.configuration_schema.get("type") != "object":
            raise ValueError("configuration_schema must describe a JSON object")
        pending: list[Any] = [self.configuration_schema]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if "$ref" in value:
                    raise ValueError(
                        "configuration_schema cannot contain external or local references"
                    )
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return self

    def identity(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "version": self.version,
            "protocol_versions": list(self.protocol_versions),
            "digest": self.digest,
            "trust_level": self.trust_level,
            "transport": self.transport,
        }


class AgentRunnerPlugin(PluginManifest):
    kind: Literal[PluginKind.AGENT_RUNNER] = PluginKind.AGENT_RUNNER


class VerifierPlugin(PluginManifest):
    kind: Literal[PluginKind.VERIFIER] = PluginKind.VERIFIER


class SelectorPlugin(PluginManifest):
    kind: Literal[PluginKind.SELECTOR] = PluginKind.SELECTOR


class MaterializerPlugin(PluginManifest):
    kind: Literal[PluginKind.MATERIALIZER] = PluginKind.MATERIALIZER


class ExecutionProviderPlugin(PluginManifest):
    kind: Literal[PluginKind.EXECUTION_PROVIDER] = PluginKind.EXECUTION_PROVIDER


class PluginCallRequest(StrictPluginModel):
    schema_version: Literal["villani.plugin.rpc.v1"] = "villani.plugin.rpc.v1"
    request_id: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    payload: dict[str, Any]
    configuration: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class PluginFailure(StrictPluginModel):
    classification: Literal[
        "crash",
        "timeout",
        "cancelled",
        "oversized_message",
        "malformed_response",
        "protocol_mismatch",
        "configuration_error",
    ]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class PluginCallResponse(StrictPluginModel):
    schema_version: Literal["villani.plugin.rpc.v1"] = "villani.plugin.rpc.v1"
    request_id: str = Field(min_length=1)
    protocol_version: str = Field(min_length=1)
    status: Literal["ok", "error"]
    result: dict[str, Any] | None = None
    error: PluginFailure | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "PluginCallResponse":
        if (self.status == "ok") != (self.result is not None):
            raise ValueError(
                "successful response requires result and error response forbids it"
            )
        if (self.status == "error") != (self.error is not None):
            raise ValueError(
                "error response requires error and successful response forbids it"
            )
        return self


class PluginExecutionError(RuntimeError):
    def __init__(self, failure: PluginFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure
