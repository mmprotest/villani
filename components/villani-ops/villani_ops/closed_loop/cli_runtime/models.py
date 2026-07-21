"""Immutable contracts and durable records for the shared CLI runtime."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


CLI_INVOCATION_SCHEMA_VERSION = "villani.cli_invocation.v1"
CLI_PROCESS_RESULT_SCHEMA_VERSION = "villani.cli_process_result.v1"
CLI_OUTPUT_TAIL_SCHEMA_VERSION = "villani.cli_output_tail.v1"


class CliFailure(str, Enum):
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    EXECUTABLE_NOT_RUNNABLE = "executable_not_runnable"
    SPAWN_FAILED = "spawn_failed"
    STDIN_FAILED = "stdin_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    NONZERO_EXIT = "nonzero_exit"
    PROCESS_TREE_CLEANUP_FAILED = "process_tree_cleanup_failed"
    STDOUT_LIMIT_EXCEEDED = "stdout_limit_exceeded"
    STDERR_LIMIT_EXCEEDED = "stderr_limit_exceeded"
    EVENT_LINE_LIMIT_EXCEEDED = "event_line_limit_exceeded"
    OUTPUT_DECODE_FAILED = "output_decode_failed"
    ARTIFACT_WRITE_FAILED = "artifact_write_failed"
    MALFORMED_STREAM = "malformed_stream"
    FINAL_OUTPUT_MISSING = "final_output_missing"
    UNKNOWN_INFRASTRUCTURE_FAILURE = "unknown_infrastructure_failure"


class CliCancellationOrigin(str, Enum):
    USER = "user"
    CONTROLLER = "controller"
    TIMEOUT = "timeout"
    PARENT_SERVICE_SHUTDOWN = "parent_service_shutdown"
    RUNTIME_FAILURE = "runtime_failure"


class StrictRuntimeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CliEnvironmentVariable(StrictRuntimeRecord):
    name: str = Field(min_length=1)
    provenance: Literal["inherited", "addition", "override", "explicit"]
    redacted: bool


class CliOutputLimits(StrictRuntimeRecord):
    maximum_stdout_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    maximum_stderr_bytes: int = Field(default=16 * 1024 * 1024, ge=1)
    maximum_stdout_chunk_bytes: int = Field(default=1024 * 1024, ge=1)
    maximum_stderr_chunk_bytes: int = Field(default=1024 * 1024, ge=1)
    maximum_event_line_bytes: int = Field(default=1024 * 1024, ge=1)
    maximum_tail_bytes: int = Field(default=16 * 1024, ge=1)
    read_chunk_bytes: int = Field(default=64 * 1024, ge=1, le=1024 * 1024)


@dataclass(frozen=True, slots=True)
class CliInvocation:
    """Fully constructed request; no provider knowledge is needed downstream."""

    executable: Path
    arguments: tuple[str, ...]
    cwd: Path
    stdin_bytes: bytes | None
    environment: Mapping[str, str]
    environment_redaction_keys: frozenset[str]
    timeout_seconds: float
    graceful_shutdown_seconds: float
    stdout_path: Path
    stderr_path: Path
    raw_event_path: Path | None = None
    invocation_path: Path | None = None
    process_result_path: Path | None = None
    output_tail_path: Path | None = None
    output_limits: CliOutputLimits = field(default_factory=CliOutputLimits)
    environment_metadata: tuple[CliEnvironmentVariable, ...] = ()
    argument_redaction_indices: frozenset[int] = frozenset()
    role_workspace_identity: Mapping[str, JsonValue] = field(default_factory=dict)
    target_repository_writable: bool = False
    prompt_artifact_reference: str | None = None
    prompt_sha256: str | None = None
    event_stream_format: Literal["none", "jsonl"] = "none"
    utf8_policy: Literal["replacement", "strict"] = "replacement"
    final_output_path: Path | None = None
    require_final_output: bool = False

    def __post_init__(self) -> None:
        executable = Path(self.executable)
        cwd = Path(self.cwd)
        stdout_path = Path(self.stdout_path)
        stderr_path = Path(self.stderr_path)
        artifact_directory = stdout_path.parent
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "stdout_path", stdout_path)
        object.__setattr__(self, "stderr_path", stderr_path)
        object.__setattr__(
            self,
            "raw_event_path",
            Path(self.raw_event_path)
            if self.raw_event_path is not None
            else artifact_directory / "raw-events.jsonl",
        )
        object.__setattr__(
            self,
            "invocation_path",
            Path(self.invocation_path)
            if self.invocation_path is not None
            else artifact_directory / "invocation.json",
        )
        object.__setattr__(
            self,
            "process_result_path",
            Path(self.process_result_path)
            if self.process_result_path is not None
            else artifact_directory / "process-result.json",
        )
        object.__setattr__(
            self,
            "output_tail_path",
            Path(self.output_tail_path)
            if self.output_tail_path is not None
            else artifact_directory / "output-tail.json",
        )
        if self.final_output_path is not None:
            object.__setattr__(self, "final_output_path", Path(self.final_output_path))
        object.__setattr__(
            self, "arguments", tuple(str(item) for item in self.arguments)
        )
        object.__setattr__(
            self, "environment", MappingProxyType(dict(self.environment))
        )
        object.__setattr__(
            self,
            "environment_redaction_keys",
            frozenset(str(item) for item in self.environment_redaction_keys),
        )
        object.__setattr__(
            self,
            "argument_redaction_indices",
            frozenset(int(item) for item in self.argument_redaction_indices),
        )
        object.__setattr__(
            self,
            "role_workspace_identity",
            MappingProxyType(dict(self.role_workspace_identity)),
        )
        if not executable.name:
            raise ValueError("executable must name one program")
        if not cwd.is_dir():
            raise ValueError(f"cwd must be an existing directory: {cwd}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if self.graceful_shutdown_seconds < 0:
            raise ValueError("graceful_shutdown_seconds must not be negative")
        if any("\x00" in argument for argument in self.arguments):
            raise ValueError("arguments must not contain NUL bytes")
        for key, value in self.environment.items():
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError(f"invalid environment entry {key!r}")
        invalid_indices = [
            index
            for index in self.argument_redaction_indices
            if index < 0 or index >= len(self.arguments)
        ]
        if invalid_indices:
            raise ValueError("argument_redaction_indices contains an invalid index")
        if self.prompt_sha256 is not None:
            if (
                not self.prompt_sha256.startswith("sha256:")
                or len(self.prompt_sha256) != 71
            ):
                raise ValueError("prompt_sha256 must be a sha256:<hex> digest")
            try:
                int(self.prompt_sha256[7:], 16)
            except ValueError as error:
                raise ValueError(
                    "prompt_sha256 must be a sha256:<hex> digest"
                ) from error
            if self.stdin_bytes is not None:
                actual = f"sha256:{hashlib.sha256(self.stdin_bytes).hexdigest()}"
                if actual != self.prompt_sha256:
                    raise ValueError("prompt_sha256 does not match stdin_bytes")
        if (self.prompt_artifact_reference is None) != (self.prompt_sha256 is None):
            raise ValueError(
                "prompt_artifact_reference and prompt_sha256 must be supplied together"
            )
        if self.require_final_output and self.final_output_path is None:
            raise ValueError("require_final_output needs final_output_path")


class CliInvocationRecord(StrictRuntimeRecord):
    schema_version: Literal["villani.cli_invocation.v1"] = "villani.cli_invocation.v1"
    executable: str
    executable_identity: dict[str, JsonValue]
    arguments: list[str]
    environment: list[CliEnvironmentVariable]
    role_workspace_identity: dict[str, JsonValue]
    target_repository_writable: bool
    cwd: str
    stdin: dict[str, JsonValue]
    timeout_seconds: float = Field(gt=0)
    graceful_shutdown_seconds: float = Field(ge=0)
    limits: CliOutputLimits
    event_stream_format: Literal["none", "jsonl"]
    utf8_policy: Literal["replacement", "strict"]
    final_output_path: str | None
    require_final_output: bool
    started_at: datetime

    @model_validator(mode="after")
    def no_environment_values(self) -> "CliInvocationRecord":
        forbidden = {"value", "environment_values", "stdin_bytes", "prompt"}
        if forbidden.intersection(self.model_dump(mode="json")):
            raise ValueError(
                "invocation record must not contain governed or secret values"
            )
        return self


class CliFailureDetail(StrictRuntimeRecord):
    code: CliFailure
    message: str = Field(min_length=1, max_length=1000)
    stream: Literal["stdout", "stderr", "events", "stdin", "artifact"] | None = None
    configured_limit_bytes: int | None = Field(default=None, ge=1)
    observed_bytes: int | None = Field(default=None, ge=0)


class CliStreamResult(StrictRuntimeRecord):
    artifact_path: str
    total_bytes_observed: int = Field(ge=0)
    bytes_persisted: int = Field(ge=0)
    limit_exceeded: bool
    largest_read_bytes: int = Field(ge=0)
    decode_replacements: bool
    output_after_cancellation: bool


class CliProcessResult(StrictRuntimeRecord):
    schema_version: Literal["villani.cli_process_result.v1"] = (
        "villani.cli_process_result.v1"
    )
    infrastructure_state: Literal["succeeded", "failed", "cancelled", "timed_out"]
    failure: CliFailure | None
    failures: list[CliFailureDetail] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)
    pid: int | None = Field(default=None, ge=1)
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    cancellation_origin: CliCancellationOrigin | None
    termination_reason: str | None
    graceful_termination_requested: bool
    graceful_termination_succeeded: bool
    forced_termination: bool
    cleanup_status: Literal["succeeded", "failed", "not_required"]
    cleanup_error: str | None
    target_repository_writable: bool
    stdin_bytes_delivered: int = Field(ge=0)
    stdout: CliStreamResult
    stderr: CliStreamResult
    raw_events: CliStreamResult
    final_output_path: str | None
    final_output_present: bool | None
    invocation_artifact: str
    output_tail_artifact: str
    process_result_artifact: str
    artifact_set_complete: bool

    @model_validator(mode="after")
    def consistent_state(self) -> "CliProcessResult":
        if self.infrastructure_state == "succeeded" and self.failure is not None:
            raise ValueError("successful process results cannot contain a failure")
        if self.infrastructure_state == "cancelled" and not self.cancelled:
            raise ValueError("cancelled infrastructure state requires cancelled=true")
        if self.infrastructure_state == "timed_out" and not self.timed_out:
            raise ValueError("timed_out infrastructure state requires timed_out=true")
        return self


class CliOutputTail(StrictRuntimeRecord):
    schema_version: Literal["villani.cli_output_tail.v1"] = "villani.cli_output_tail.v1"
    stdout: str
    stderr: str
    maximum_tail_bytes: int = Field(ge=1)
    utf8_policy: Literal["replacement", "strict"]
    stdout_decode_replacements: bool
    stderr_decode_replacements: bool


@dataclass(frozen=True, slots=True)
class CliRawEvent:
    line_number: int
    raw_line: bytes
    value: Mapping[str, Any]


__all__ = [
    "CLI_INVOCATION_SCHEMA_VERSION",
    "CLI_OUTPUT_TAIL_SCHEMA_VERSION",
    "CLI_PROCESS_RESULT_SCHEMA_VERSION",
    "CliCancellationOrigin",
    "CliEnvironmentVariable",
    "CliFailure",
    "CliFailureDetail",
    "CliInvocation",
    "CliInvocationRecord",
    "CliOutputLimits",
    "CliOutputTail",
    "CliProcessResult",
    "CliRawEvent",
    "CliStreamResult",
]
