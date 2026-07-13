from __future__ import annotations
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Protocol, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from villani_ops.core.backend import Backend


class CandidateExecutionAcknowledgement(BaseModel):
    """Execution-time proof of the configuration the runner actually applied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["villani.candidate_execution.v1"] = (
        "villani.candidate_execution.v1"
    )
    candidate_id: str = Field(min_length=1)
    requested_dimensions: dict[str, Any]
    applied_dimensions: dict[str, Any]
    unsupported_dimensions: dict[str, Any] = Field(default_factory=dict)
    rejected_dimensions: dict[str, Any] = Field(default_factory=dict)
    provider_acknowledgement: dict[str, Any] | None = None
    runner_acknowledged: bool
    rendered_prompt_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    effective_configuration_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    acknowledgement_timestamp: datetime

    @model_validator(mode="before")
    @classmethod
    def normalize_v0_document(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.setdefault("schema_version", "villani.candidate_execution.v1")
        if "rendered_prompt_digest" not in normalized:
            legacy = normalized.pop("effective_prompt_digest", None)
            if legacy is not None:
                normalized["rendered_prompt_digest"] = legacy
        return normalized

    @model_validator(mode="after")
    def validate_effective_digest(self) -> "CandidateExecutionAcknowledgement":
        expected = hashlib.sha256(
            json.dumps(
                self.applied_dimensions,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if self.effective_configuration_digest != expected:
            raise ValueError("effective configuration digest does not match applied dimensions")
        if self.runner_acknowledged and not self.applied_dimensions:
            raise ValueError("runner acknowledgement requires applied dimensions")
        return self


class RunnerContext(BaseModel):
    attempt_id: str
    repo_path: str
    task_instruction: str
    success_criteria: str | None = None
    backend: Backend
    timeout_seconds: int
    run_dir: str
    env: dict[str, str] = Field(default_factory=dict)
    inherit_parent_environment: bool = True
    execution_prefix: list[str] = Field(default_factory=list)
    workspace_limit_bytes: int | None = None
    cleanup_command: list[str] = Field(default_factory=list)
    secure_secret_injection: bool = False
    command: str | None = None
    candidate_dimensions: dict[str, Any] = Field(default_factory=dict)
    cancellation_event: Any | None = Field(default=None, exclude=True)


class RunnerResult(BaseModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int | None = None
    total_cost: float | None = None
    usage_records: list[Any] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    debug_artifact_dir: str | None = None
    resolved_trace_dir: str | None = None
    telemetry_path: str | None = None
    duration_ms: int | None = None
    model_requests: int = 0
    model_failures: int = 0
    total_tool_calls: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    total_file_reads: int = 0
    total_file_writes: int = 0
    commands_executed: int = 0
    commands_failed: int = 0
    first_substantive_file_read_tool_index: int | None = None
    first_substantive_file_read_seconds: float | None = None
    first_file_mutation_tool_index: int | None = None
    first_file_mutation_seconds: float | None = None
    first_command_tool_index: int | None = None
    first_command_seconds: float | None = None
    token_accounting_status: str = "missing"
    token_accounting_warnings: list[str] = Field(default_factory=list)
    telemetry: dict[str, Any] = Field(default_factory=dict)


class RunnerAdapter(Protocol):
    name: str

    def run_task(
        self,
        *,
        repo_path: Path,
        task: str,
        success_criteria: str | None,
        backend_name: str,
        backend_config: Backend,
        timeout_seconds: int | None,
        context: dict[str, Any],
        artifacts_dir: Path,
    ) -> RunnerResult: ...
    def run(self, context: RunnerContext) -> RunnerResult: ...


class UnsupportedRunnerAdapter:
    def __init__(self, name: str):
        self.name = name

    def run_task(self, **kwargs) -> RunnerResult:
        raise NotImplementedError(
            f"Runner '{self.name}' is registered but not implemented yet."
        )

    def run(self, context: RunnerContext) -> RunnerResult:
        raise NotImplementedError(
            f"Runner '{self.name}' is registered but not implemented yet."
        )


Runner = RunnerAdapter
