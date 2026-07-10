"""Typed Python representation of the normative Villani v1 wire protocol."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)


AccountingStatus: TypeAlias = Literal[
    "complete", "partial", "unknown", "not_applicable"
]
ControllerState: TypeAlias = Literal[
    "CREATED",
    "CLASSIFYING",
    "CLASSIFIED",
    "POLICY_SELECTED",
    "ATTEMPT_RUNNING",
    "ATTEMPT_COMPLETED",
    "VERIFYING",
    "VERIFIED",
    "REJECTED",
    "ESCALATING",
    "SELECTING",
    "MATERIALIZING",
    "COMPLETED",
    "EXHAUSTED",
    "FAILED",
]


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return value


def _serialize_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[
    datetime,
    AfterValidator(_require_utc),
    PlainSerializer(_serialize_utc, return_type=str, when_used="json"),
]


def _check_accounting(
    status: AccountingStatus, values: tuple[int | float | None, ...], label: str
) -> None:
    if status == "complete" and any(value is None for value in values):
        raise ValueError(f"complete {label} accounting requires non-null values")
    if status in {"unknown", "not_applicable"} and any(
        value is not None for value in values
    ):
        raise ValueError(f"{status} {label} accounting requires null values")


class StrictProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageUsage(StrictProtocolModel):
    """One model invocation or aggregate for a named controller stage."""

    stage: Literal["classification", "coding", "verification", "selection", "materialization", "total"]
    backend: str | None = None
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    token_accounting_status: AccountingStatus = "unknown"
    model_calls: int | None = Field(default=None, ge=0)
    model_call_accounting_status: AccountingStatus = "unknown"
    cost: float | None = Field(default=None, ge=0)
    cost_accounting_status: AccountingStatus = "unknown"
    currency: str = Field(default="USD", min_length=3, max_length=3)
    duration_ms: int | None = Field(default=None, ge=0)
    duration_accounting_status: AccountingStatus = "unknown"
    failure_state: Literal["succeeded", "failed", "unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_stage_accounting(self) -> "StageUsage":
        if not self.currency.isalpha():
            raise ValueError("currency must contain only letters")
        self.currency = self.currency.upper()
        _check_accounting(
            self.token_accounting_status,
            (self.input_tokens, self.output_tokens, self.total_tokens),
            "token",
        )
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        _check_accounting(self.cost_accounting_status, (self.cost,), "cost")
        _check_accounting(
            self.duration_accounting_status, (self.duration_ms,), "duration"
        )
        _check_accounting(
            self.model_call_accounting_status, (self.model_calls,), "model-call"
        )
        return self


class FailureDetail(StrictProtocolModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: dict[str, Any]


class TaskSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.task.v1"]
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    created_at: UtcDateTime
    repository_path: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    success_criteria: str = Field(min_length=1)
    constraints: list[str]
    requires_file_changes: bool
    metadata: dict[str, Any]


class RunArtifactPaths(StrictProtocolModel):
    task: str = Field(min_length=1)
    classification: str = Field(min_length=1)
    state: str = Field(min_length=1)
    events: str = Field(min_length=1)
    policy_decisions: str = Field(min_length=1)
    selection: str = Field(min_length=1)
    materialization: str = Field(min_length=1)


class RunManifestSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.run_manifest.v1"]
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    created_at: UtcDateTime
    updated_at: UtcDateTime
    completed_at: UtcDateTime | None
    final_state: ControllerState
    attempt_ids: list[str]
    selected_attempt_id: str | None
    total_cost_usd: float | None = Field(ge=0)
    cost_accounting_status: AccountingStatus
    total_input_tokens: int | None = Field(ge=0)
    total_output_tokens: int | None = Field(ge=0)
    token_accounting_status: AccountingStatus
    total_duration_ms: int | None = Field(ge=0)
    duration_accounting_status: AccountingStatus
    artifact_paths: RunArtifactPaths
    metadata: dict[str, Any]
    currency: str = Field(default="USD", min_length=3, max_length=3)
    stage_metrics: dict[str, StageUsage] = Field(default_factory=dict)
    total_model_calls: int | None = Field(default=None, ge=0)
    model_call_accounting_status: AccountingStatus = "unknown"
    run_wall_clock_duration_ms: int | None = Field(default=None, ge=0)
    run_wall_clock_duration_accounting_status: AccountingStatus = "unknown"

    @model_validator(mode="after")
    def validate_accounting(self) -> RunManifestSnapshot:
        if not self.currency.isalpha():
            raise ValueError("currency must contain only letters")
        self.currency = self.currency.upper()
        _check_accounting(
            self.cost_accounting_status, (self.total_cost_usd,), "cost"
        )
        _check_accounting(
            self.token_accounting_status,
            (self.total_input_tokens, self.total_output_tokens),
            "token",
        )
        _check_accounting(
            self.duration_accounting_status, (self.total_duration_ms,), "duration"
        )
        _check_accounting(
            self.model_call_accounting_status,
            (self.total_model_calls,),
            "model-call",
        )
        _check_accounting(
            self.run_wall_clock_duration_accounting_status,
            (self.run_wall_clock_duration_ms,),
            "run-wall-clock-duration",
        )
        return self


class RunStateSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.run_state.v1"]
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    state: ControllerState
    previous_state: ControllerState | None
    terminal: bool
    updated_at: UtcDateTime
    last_event_id: str = Field(min_length=1)
    last_sequence: int = Field(ge=1)
    active_attempt_id: str | None
    attempt_count: int = Field(ge=0)
    accepted_candidate_ids: list[str]
    failure: FailureDetail | None
    metadata: dict[str, Any]

    @model_validator(mode="after")
    def validate_terminal_flag(self) -> RunStateSnapshot:
        expected = self.state in {"COMPLETED", "EXHAUSTED", "FAILED"}
        if self.terminal is not expected:
            raise ValueError(f"state {self.state} requires terminal={expected}")
        return self


class EventEnvelope(StrictProtocolModel):
    schema_version: Literal["villani.event.v1"]
    event_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    timestamp: UtcDateTime
    trace_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt_id: str | None
    parent_event_id: str | None
    source: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    event_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_attempt_scope(self) -> EventEnvelope:
        prefixes = (
            "attempt_",
            "verification_",
            "patch_",
            "model_",
            "tool_",
            "command_",
            "file_",
        )
        if self.event_type.startswith(prefixes) and self.attempt_id is None:
            raise ValueError(f"{self.event_type} requires a non-null attempt_id")
        return self


class ClassificationSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.classification.v1"]
    classification_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    classified_at: UtcDateTime
    difficulty: Literal["easy", "medium", "hard"]
    risk: Literal["low", "medium", "high"]
    category: str = Field(min_length=1)
    required_capabilities: list[str]
    estimated_attempts_needed: int = Field(ge=1)
    needs_tests: bool
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str
    signals: dict[str, Any]
    metadata: dict[str, Any]
    llm_usage: list[StageUsage] = Field(default_factory=list)


class BackendConsideration(StrictProtocolModel):
    backend_name: str = Field(min_length=1)
    model: str | None
    eligible: bool
    capability_score: float | None = Field(ge=0)
    estimated_cost_usd: float | None = Field(ge=0)
    cost_accounting_status: AccountingStatus
    rejection_reasons: list[str]

    @model_validator(mode="after")
    def validate_accounting(self) -> BackendConsideration:
        _check_accounting(
            self.cost_accounting_status, (self.estimated_cost_usd,), "cost"
        )
        return self


class BudgetSnapshot(StrictProtocolModel):
    remaining_attempts: int | None = Field(ge=0)
    remaining_cost_usd: float | None = Field(ge=0)
    cost_accounting_status: AccountingStatus
    remaining_wall_time_ms: int | None = Field(ge=0)
    duration_accounting_status: AccountingStatus

    @model_validator(mode="after")
    def validate_accounting(self) -> BudgetSnapshot:
        _check_accounting(
            self.cost_accounting_status, (self.remaining_cost_usd,), "cost"
        )
        _check_accounting(
            self.duration_accounting_status,
            (self.remaining_wall_time_ms,),
            "duration",
        )
        return self


class PolicyDecisionSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.policy_decision.v1"]
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    timestamp: UtcDateTime
    decision_sequence: int = Field(ge=1)
    classification_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    action: Literal["attempt", "retry", "escalate", "select", "exhaust", "fail"]
    reason: str = Field(min_length=1)
    considered_backends: list[BackendConsideration]
    chosen_backend: str | None
    chosen_model: str | None
    attempt_id: str | None
    budget_before: BudgetSnapshot
    budget_after: BudgetSnapshot
    metadata: dict[str, Any]


class AttemptSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.attempt.v1"]
    attempt_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    ordinal: int = Field(ge=1)
    backend_name: str = Field(min_length=1)
    runner_name: str = Field(min_length=1)
    model: str | None
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    started_at: UtcDateTime | None
    completed_at: UtcDateTime | None
    worktree_path: str = Field(min_length=1)
    patch_path: str | None
    patch_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    patch_bytes: int | None = Field(ge=0)
    stdout_path: str | None
    stderr_path: str | None
    runner_telemetry_path: str | None
    trace_path: str | None
    exit_code: int | None
    duration_ms: int | None = Field(ge=0)
    duration_accounting_status: AccountingStatus
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    token_accounting_status: AccountingStatus
    cost_usd: float | None = Field(ge=0)
    cost_accounting_status: AccountingStatus
    error: FailureDetail | None
    metadata: dict[str, Any]

    @model_validator(mode="after")
    def validate_accounting(self) -> AttemptSnapshot:
        _check_accounting(
            self.duration_accounting_status, (self.duration_ms,), "duration"
        )
        _check_accounting(
            self.token_accounting_status,
            (self.input_tokens, self.output_tokens),
            "token",
        )
        _check_accounting(self.cost_accounting_status, (self.cost_usd,), "cost")
        return self


class RequirementResult(StrictProtocolModel):
    requirement_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    outcome: Literal["passed", "failed", "missing", "not_applicable"]
    evidence_ids: list[str]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    evidence_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    artifact_path: str | None


class VerificationSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.verification.v1"]
    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    verified_at: UtcDateTime
    verifier: str = Field(min_length=1)
    outcome: Literal["accepted", "rejected", "unclear", "error"]
    acceptance_eligible: bool
    confidence: float | None = Field(ge=0, le=1)
    reason: str = Field(min_length=1)
    requirement_results: list[RequirementResult]
    success_evidence: list[Evidence]
    failure_evidence: list[Evidence]
    missing_evidence: list[Evidence]
    risk_flags: list[str]
    recommended_action: Literal[
        "accept", "reject", "retry_verifier", "escalate", "fail"
    ]
    raw_verifier_artifact: str | None
    metadata: dict[str, Any]
    llm_usage: list[StageUsage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_acceptance_eligibility(self) -> VerificationSnapshot:
        if self.acceptance_eligible and (
            self.outcome != "accepted" or self.recommended_action != "accept"
        ):
            raise ValueError(
                "acceptance_eligible requires outcome=accepted and "
                "recommended_action=accept"
            )
        return self


class CandidateRanking(StrictProtocolModel):
    attempt_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    reason: str = Field(min_length=1)
    actual_cost_usd: float | None = Field(ge=0)
    cost_accounting_status: AccountingStatus
    evidence: dict[str, Any]

    @model_validator(mode="after")
    def validate_accounting(self) -> CandidateRanking:
        _check_accounting(
            self.cost_accounting_status, (self.actual_cost_usd,), "cost"
        )
        return self


class SelectionSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.selection.v1"]
    selection_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    selected_at: UtcDateTime
    strategy: str = Field(min_length=1)
    eligible_candidate_ids: list[str]
    selected_candidate_ids: list[str] = Field(max_length=1)
    rankings: list[CandidateRanking]
    reason: str = Field(min_length=1)
    advisory_comparison: dict[str, Any] | None
    metadata: dict[str, Any]

    @model_validator(mode="after")
    def validate_selected_candidates(self) -> SelectionSnapshot:
        unexpected = set(self.selected_candidate_ids) - set(
            self.eligible_candidate_ids
        )
        if unexpected:
            raise ValueError(
                "selected candidates are not acceptance eligible: "
                + ", ".join(sorted(unexpected))
            )
        return self


class MaterializationSnapshot(StrictProtocolModel):
    schema_version: Literal["villani.materialization.v1"]
    materialization_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    selection_id: str = Field(min_length=1)
    selected_attempt_id: str = Field(min_length=1)
    started_at: UtcDateTime
    completed_at: UtcDateTime | None
    status: Literal["pending", "running", "succeeded", "failed"]
    source_patch_path: str = Field(min_length=1)
    target_repository_path: str = Field(min_length=1)
    materialized_patch_path: str | None
    patch_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    changed_files: list[str]
    failure: FailureDetail | None
    metadata: dict[str, Any]

    @model_validator(mode="after")
    def validate_success(self) -> MaterializationSnapshot:
        if self.status == "succeeded" and (
            self.completed_at is None
            or self.materialized_patch_path is None
            or self.patch_sha256 is None
            or self.failure is not None
        ):
            raise ValueError(
                "successful materialization requires completion and patch evidence"
            )
        return self


ProtocolDocument: TypeAlias = (
    TaskSnapshot
    | RunManifestSnapshot
    | RunStateSnapshot
    | EventEnvelope
    | ClassificationSnapshot
    | PolicyDecisionSnapshot
    | AttemptSnapshot
    | VerificationSnapshot
    | SelectionSnapshot
    | MaterializationSnapshot
)


PROTOCOL_MODEL_BY_VERSION: dict[str, type[StrictProtocolModel]] = {
    "villani.task.v1": TaskSnapshot,
    "villani.run_manifest.v1": RunManifestSnapshot,
    "villani.run_state.v1": RunStateSnapshot,
    "villani.event.v1": EventEnvelope,
    "villani.classification.v1": ClassificationSnapshot,
    "villani.policy_decision.v1": PolicyDecisionSnapshot,
    "villani.attempt.v1": AttemptSnapshot,
    "villani.verification.v1": VerificationSnapshot,
    "villani.selection.v1": SelectionSnapshot,
    "villani.materialization.v1": MaterializationSnapshot,
}
