"""Dependency boundaries and immutable data for the deterministic controller."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, TypeAlias

from .protocol import (
    AccountingStatus,
    AttemptSnapshot,
    ClassificationSnapshot,
    ControllerState,
    VerificationSnapshot,
)


PolicyAction: TypeAlias = Literal[
    "attempt", "retry", "escalate", "select", "exhaust", "fail"
]


@dataclass(frozen=True, slots=True)
class Classification:
    difficulty: Literal["easy", "medium", "hard"]
    risk: Literal["low", "medium", "high"]
    category: str
    required_capabilities: tuple[str, ...] = ()
    estimated_attempts_needed: int = 1
    needs_tests: bool = True
    confidence: float = 1.0
    reasoning_summary: str = ""
    signals: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendOption:
    backend_name: str
    model: str | None = None
    eligible: bool = True
    capability_score: float | None = None
    estimated_cost_usd: float | None = None
    cost_accounting_status: AccountingStatus = "unknown"
    rejection_reasons: tuple[str, ...] = ()
    cost_components: Mapping[str, Any] = field(default_factory=dict)
    cost_source: str = "configured_estimate"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: PolicyAction
    reason: str
    considered_backends: tuple[BackendOption, ...] = ()
    chosen_backend: str | None = None
    chosen_model: str | None = None
    policy_version: str = "fake_v1"
    classification_reference: str | None = None
    required_capability_score: float | None = None
    required_capability_rule: str = "unspecified"
    repeats_prior_backend: bool = False
    escalates_from_prior_backend: bool = False
    budget_before: BudgetContext | None = None
    budget_projection_after: BudgetContext | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DependencyFailure:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_type: str
    timestamp: datetime
    payload: Mapping[str, Any] = field(default_factory=dict)
    source_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptResult:
    runner_name: str
    status: Literal["completed", "failed", "cancelled"]
    worktree_path: str
    patch: str | None
    exit_code: int | None
    model: str | None = None
    stdout: str = ""
    stderr: str = ""
    runner_telemetry: Mapping[str, Any] = field(default_factory=dict)
    trace: Mapping[str, Any] = field(default_factory=dict)
    trace_path: str | None = None
    telemetry_path: str | None = None
    runtime_events: tuple[RuntimeEvent, ...] = ()
    duration_ms: int | None = None
    duration_accounting_status: AccountingStatus = "unknown"
    input_tokens: int | None = None
    output_tokens: int | None = None
    token_accounting_status: AccountingStatus = "unknown"
    cost_usd: float | None = None
    cost_accounting_status: AccountingStatus = "unknown"
    error: DependencyFailure | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Requirement:
    requirement_id: str
    description: str
    outcome: Literal["passed", "failed", "missing", "not_applicable"]
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    kind: str
    summary: str
    artifact_path: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Verification:
    verifier: str
    outcome: Literal["accepted", "rejected", "unclear", "error"]
    acceptance_eligible: bool
    confidence: float | None
    reason: str
    recommended_action: Literal[
        "accept", "reject", "retry_verifier", "escalate", "fail"
    ]
    requirement_results: tuple[Requirement, ...] = ()
    success_evidence: tuple[EvidenceItem, ...] = ()
    failure_evidence: tuple[EvidenceItem, ...] = ()
    missing_evidence: tuple[EvidenceItem, ...] = ()
    risk_flags: tuple[str, ...] = ()
    raw_verifier_artifact: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    llm_usage: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SelectionRanking:
    attempt_id: str
    rank: int
    reason: str
    actual_cost_usd: float | None
    cost_accounting_status: AccountingStatus
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Selection:
    selected_attempt_id: str | None
    strategy: str
    reason: str
    rankings: tuple[SelectionRanking, ...] = ()
    advisory_comparison: Mapping[str, Any] | None = None
    report: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Materialization:
    status: Literal["succeeded", "failed"]
    final_patch: str | None
    final_report: str
    changed_files: tuple[str, ...] = ()
    failure: DependencyFailure | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BudgetContext:
    remaining_attempts: int
    remaining_cost_usd: float | None
    cost_accounting_status: AccountingStatus
    remaining_wall_time_ms: int | None
    duration_accounting_status: AccountingStatus
    actual_attempts_used: int = 0
    actual_cost_consumed_usd: float | None = None
    actual_cost_accounting_status: AccountingStatus = "unknown"
    actual_wall_time_ms: int | None = None
    actual_stage_attempts_used: int = 0


@dataclass(frozen=True, slots=True)
class ClassificationContext:
    run_id: str
    trace_id: str
    task_id: str
    repository_path: str
    success_criteria: str
    requires_file_changes: bool
    policy_configuration: Mapping[str, Any]
    classification_backend_name: str | None = None
    classification_backend_model: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    attempt_id: str
    backend_name: str
    exit_code: int | None
    status: str
    cost_usd: float | None
    cost_accounting_status: AccountingStatus
    failure_category: str | None = None
    material_progress: bool = False
    duration_ms: int | None = None
    rate_limited: bool = False


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    attempt_id: str
    outcome: str
    acceptance_eligible: bool
    recommended_action: str
    failure_category: str | None = None
    verifier_retry_count: int = 0
    disagreement: bool = False


@dataclass(frozen=True, slots=True)
class PolicyContext:
    run_id: str
    trace_id: str
    state: ControllerState
    classification: ClassificationSnapshot
    attempts: tuple[AttemptSummary, ...]
    verifications: tuple[VerificationSummary, ...]
    eligible_candidate_ids: tuple[str, ...]
    budget: BudgetContext
    policy_configuration: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AttemptContext:
    run_id: str
    trace_id: str
    task_id: str
    attempt_id: str
    ordinal: int
    task: str
    repository_path: str
    success_criteria: str
    requires_file_changes: bool
    backend_name: str
    model: str | None
    policy_configuration: Mapping[str, Any]
    run_directory: Path
    attempt_directory: Path
    execution_provider: str | None = None
    guarded_task_route: Mapping[str, Any] = field(default_factory=dict)
    candidate_dimensions: Mapping[str, Any] = field(default_factory=dict)
    classification: Mapping[str, Any] = field(default_factory=dict)
    baseline_sha256: str | None = None
    repair_source_attempt_id: str | None = None
    cancellation_event: Any | None = field(
        default=None, repr=False, compare=False, metadata={"plugin_exclude": True}
    )


@dataclass(frozen=True, slots=True)
class EligibleCandidate:
    attempt: AttemptSnapshot
    verification: VerificationSnapshot
    patch: str


@dataclass(frozen=True, slots=True)
class SelectionContext:
    run_id: str
    trace_id: str
    task: str
    repository_path: str
    success_criteria: str
    policy_configuration: Mapping[str, Any]
    run_directory: Path


@dataclass(frozen=True, slots=True)
class MaterializationContext:
    run_id: str
    trace_id: str
    repository_path: str
    selected_candidate: EligibleCandidate
    policy_configuration: Mapping[str, Any]
    run_directory: Path
    risk: str | None = None


@dataclass(frozen=True, slots=True)
class ClosedLoopRunRequest:
    task: str
    repository_path: str | Path
    success_criteria: str
    runs_root: str | Path
    max_attempts: int
    policy_configuration: Mapping[str, Any]
    max_cost: float | None = None
    max_wall_time: float | None = None
    requires_file_changes: bool = True

    def __post_init__(self) -> None:
        if not self.task:
            raise ValueError("task must not be empty")
        if not self.success_criteria:
            raise ValueError("success_criteria must not be empty")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.max_cost is not None and self.max_cost < 0:
            raise ValueError("max_cost must not be negative")
        if self.max_wall_time is not None and self.max_wall_time < 0:
            raise ValueError("max_wall_time must not be negative")


@dataclass(frozen=True, slots=True)
class ClosedLoopRunResult:
    run_id: str
    terminal_state: Literal["COMPLETED", "EXHAUSTED", "FAILED"]
    selected_attempt_id: str | None
    run_directory: Path
    actual_known_cost_usd: float | None
    accounting_status: AccountingStatus
    failure_or_exhaustion_reason: str | None
    currency: str = "USD"

    @property
    def actual_known_cost(self) -> float | None:
        return self.actual_known_cost_usd


class Classifier(Protocol):
    def classify(self, task: str, context: ClassificationContext) -> Classification: ...


class PolicyEngine(Protocol):
    def decide(self, context: PolicyContext) -> PolicyDecision: ...


class AttemptRunner(Protocol):
    def run(self, attempt_context: AttemptContext) -> AttemptResult: ...


class Verifier(Protocol):
    def verify(
        self, attempt_context: AttemptContext, attempt_result: AttemptResult
    ) -> Verification: ...


class Selector(Protocol):
    def select(
        self,
        eligible_candidates: tuple[EligibleCandidate, ...],
        context: SelectionContext,
    ) -> Selection: ...


class Materializer(Protocol):
    def materialize(
        self, selection: Selection, context: MaterializationContext
    ) -> Materialization: ...
