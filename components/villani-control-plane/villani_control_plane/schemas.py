from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestBatchRequest(StrictRequest):
    batch_id: str = Field(min_length=1, max_length=128)
    events: list[dict[str, Any]] = Field(min_length=1, max_length=10_000)


class ArtifactDescriptorRequest(StrictRequest):
    run_id: str = Field(min_length=1, max_length=128)
    descriptor: dict[str, Any]


class EnrollmentRequest(StrictRequest):
    enrollment_token: str = Field(min_length=24)
    installation_id: str = Field(min_length=1, max_length=128)
    agent_name: str = Field(min_length=1, max_length=255)
    agent_version: str | None = Field(default=None, max_length=128)


class GPUCapability(StrictRequest):
    vendor: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    count: int = Field(ge=1, le=1024)
    memory_bytes: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerCapabilities(StrictRequest):
    platform: str = Field(min_length=1, max_length=64)
    architecture: str = Field(min_length=1, max_length=64)
    execution_providers: list[str] = Field(min_length=1, max_length=32)
    agent_adapters: list[str] = Field(min_length=1, max_length=64)
    reachable_models: list[str] = Field(default_factory=list, max_length=256)
    reachable_runtimes: list[str] = Field(default_factory=list, max_length=256)
    cpu_count: float = Field(gt=0, le=4096)
    memory_bytes: int = Field(gt=0)
    gpus: list[GPUCapability] = Field(default_factory=list, max_length=64)
    concurrency: int = Field(ge=1, le=1024)
    network_class: str = Field(min_length=1, max_length=64)
    data_residency_labels: list[str] = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=128)


class WorkerHeartbeatRequest(StrictRequest):
    capabilities: WorkerCapabilities
    status: Literal["online", "draining"] = "online"


class CheckoutSecretReference(StrictRequest):
    broker_reference: str = Field(min_length=1, max_length=255)
    scope_repository_id: str = Field(min_length=1, max_length=128)
    expires_in_seconds: int = Field(ge=1, le=900)


class RepositoryReference(StrictRequest):
    repository_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=1, max_length=255)
    checkout_url: str | None = Field(default=None, max_length=4096)
    checkout_secret: CheckoutSecretReference | None = None


class CapabilityConstraints(StrictRequest):
    platforms: list[str] = Field(default_factory=list, max_length=32)
    architectures: list[str] = Field(default_factory=list, max_length=32)
    execution_providers: list[str] = Field(default_factory=list, max_length=32)
    agent_adapters: list[str] = Field(default_factory=list, max_length=64)
    reachable_models: list[str] = Field(default_factory=list, max_length=256)
    reachable_runtimes: list[str] = Field(default_factory=list, max_length=256)
    min_cpu_count: float = Field(default=0, ge=0, le=4096)
    min_memory_bytes: int = Field(default=0, ge=0)
    gpu_required: bool = False
    gpu_vendors: list[str] = Field(default_factory=list, max_length=64)
    min_gpu_memory_bytes: int = Field(default=0, ge=0)
    network_classes: list[str] = Field(default_factory=list, max_length=32)
    data_residency_labels: list[str] = Field(default_factory=list, max_length=64)


class RemoteTaskRequest(StrictRequest):
    task_id: str = Field(min_length=1, max_length=128)
    submission_idempotency_key: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=128)
    task_input: dict[str, Any]
    policy_version: str = Field(min_length=1, max_length=128)
    repository: RepositoryReference
    required_capabilities: CapabilityConstraints = Field(default_factory=CapabilityConstraints)
    priority: int = Field(default=0, ge=-1_000_000, le=1_000_000)
    deadline: datetime | None = None
    max_attempts: int = Field(default=3, ge=1, le=100)


class TaskCancellationRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=255)


class TaskCompletionRequest(StrictRequest):
    idempotency_key: str = Field(min_length=1, max_length=255)
    finalization_idempotency_key: str = Field(min_length=1, max_length=255)
    status: Literal["succeeded", "failed", "cancelled"]
    materialized: bool = False
    finalized: bool = False
    result: dict[str, Any] = Field(default_factory=dict)


class OutcomeProvenance(StrictRequest):
    source: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=255)
    observed_at: datetime
    actor: str | None = Field(default=None, max_length=255)
    attributes: dict[str, Any] = Field(default_factory=dict)


class OutcomeLedgerRequest(StrictRequest):
    outcome: dict[str, Any]
    provenance: OutcomeProvenance
    confidence: float = Field(ge=0, le=1)
    corrects_version: int | None = Field(default=None, ge=1)


class GitOutcomeEvent(StrictRequest):
    event_type: Literal[
        "run",
        "attempt",
        "verification",
        "materialization",
        "ci",
        "developer_disposition",
        "merge",
        "revert",
        "defect",
    ]
    state: str = Field(min_length=1, max_length=64)
    external_id: str = Field(min_length=1, max_length=255)
    confidence: float = Field(ge=0, le=1)
    correction_of_signal_id: str | None = Field(default=None, max_length=36)
    attributes: dict[str, Any] = Field(default_factory=dict)


class GitOutcomeWebhook(StrictRequest):
    contract_version: Literal["villani.git_outcome_webhook.v1"] = "villani.git_outcome_webhook.v1"
    provider: str = Field(min_length=1, max_length=64)
    delivery_id: str = Field(min_length=1, max_length=255)
    repository_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    attempt_id: str | None = Field(default=None, max_length=128)
    observed_at: datetime
    events: list[GitOutcomeEvent] = Field(min_length=1, max_length=100)


class ShadowRoutingObservationRequest(StrictRequest):
    run_id: str = Field(min_length=1, max_length=128)
    recommendation_id: str = Field(min_length=1, max_length=128)
    shadow_strategy: str | None = Field(default=None, max_length=255)
    actual_strategy: str | None = Field(default=None, max_length=255)
    shadow_policy_version: str = Field(min_length=1, max_length=128)
    actual_policy_version: str = Field(min_length=1, max_length=128)
    recorded_at: datetime


class EvaluationPublicationProvenance(StrictRequest):
    evaluation_report_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_dataset_id: str = Field(min_length=1, max_length=255)
    assignment_provenance_complete: bool
    propensity_known: bool


class PolicyPublicationCreateRequest(StrictRequest):
    policy_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_snapshot: dict[str, Any]
    prior_publication_id: str | None = Field(default=None, max_length=36)
    canary_percentage: float = Field(default=0, ge=0, le=100)
    rollback_thresholds: dict[str, float] = Field(default_factory=dict)
    manual_approval_required: bool = True
    evaluation_provenance: EvaluationPublicationProvenance


class PolicyPublicationTransitionRequest(StrictRequest):
    state: Literal["draft", "shadow", "canary", "active", "paused", "rolled_back"]
    reason: str = Field(min_length=1, max_length=255)


class PolicyPublicationApprovalRequest(StrictRequest):
    evidence: dict[str, Any]


class PolicyCanaryEvaluationRequest(StrictRequest):
    success_rate: float | None = Field(default=None, ge=0, le=1)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    calibration_error: float | None = Field(default=None, ge=0, le=1)


class PolicyEmergencyDisableRequest(StrictRequest):
    disabled: bool
    reason: str = Field(min_length=1, max_length=255)


class EventPage(BaseModel):
    events: list[dict[str, Any]]
    next_cursor: str | None
    cursor: str | None = None


class SpanPage(BaseModel):
    spans: list[dict[str, Any]]
    next_cursor: str | None


class ArtifactPage(BaseModel):
    artifacts: list[dict[str, Any]]
    next_cursor: str | None


class RunSummary(BaseModel):
    id: str
    workspace_id: str
    project_id: str
    repository_id: str
    trace_id: str
    status: str
    first_occurred_at: datetime
    first_observed_at: datetime
    last_observed_at: datetime


class RunDetail(RunSummary):
    attempts: list[dict[str, Any]]
    outcomes: list[dict[str, Any]]
    artifact_count: int
    canonical_projection: dict[str, Any] = Field(default_factory=dict)
    task_instruction: str | None = None
    success_criteria: str | None = None
    repository: str | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    raw_classification: dict[str, Any] | None = None
    effective_classification: dict[str, Any] | None = None
    classification_confidence: float | None = None
    classification_adjustments: list[dict[str, Any]] = Field(default_factory=list)
    policy_version: str | None = None
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list)
    selected_attempt_id: str | None = None
    selected_backend: str | None = None
    selected_model: str | None = None
    attempt_count: int = 0
    escalation_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    token_accounting_status: str = "unknown"
    coding_cost_usd: float | None = None
    verifier_cost_usd: float | None = None
    total_cost_usd: float | None = None
    cost_accounting_status: str = "unknown"
    duration_ms: int | None = None
    verification_status: str | None = None
    verification_authority: str | None = None
    candidate_outcomes: dict[str, Any] = Field(default_factory=dict)
    selection_reason: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    file_write_count: int = 0
    materialization_status: str | None = None
    failure_category: str | None = None
    terminal_reason: str | None = None
    redaction_status: dict[str, Any] | None = None
    redaction_applied: bool = False
    redacted_field_count: int = 0
    redaction_categories: list[str] = Field(default_factory=list)
    withheld_artifact_count: int = 0
    withheld_artifact_categories: list[str] = Field(default_factory=list)


class RunList(BaseModel):
    runs: list[RunSummary]


class FleetFilters(StrictRequest):
    started_after: datetime | None = None
    started_before: datetime | None = None
    organization_id: str | None = Field(default=None, max_length=128)
    workspace_id: str | None = Field(default=None, max_length=128)
    project_id: str | None = Field(default=None, max_length=128)
    repository_id: str | None = Field(default=None, max_length=128)
    agent: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=255)
    provider: str | None = Field(default=None, max_length=128)
    policy_version: str | None = Field(default=None, max_length=128)
    task_category: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=64)
    verification: str | None = Field(default=None, max_length=64)
    failure_category: str | None = Field(default=None, max_length=128)
    min_cost_usd: float | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    min_tokens: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    min_duration_ms: int | None = Field(default=None, ge=0)
    max_duration_ms: int | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list, max_length=32)


class FleetSearchRequest(StrictRequest):
    filters: FleetFilters = Field(default_factory=FleetFilters)
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class FleetRunPage(BaseModel):
    runs: list[dict[str, Any]]
    next_cursor: str | None


class SavedViewRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=255)
    visibility: Literal["private", "workspace"] = "private"
    filter_ast: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list, max_length=64)
    sort: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    version: int = Field(default=1, ge=1)


class MetricRequest(StrictRequest):
    filters: FleetFilters = Field(default_factory=FleetFilters)
    group_by: Literal["agent", "model", "provider", "policy_version"] | None = None


class AlertRuleRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=255)
    rule_type: Literal[
        "spend",
        "failure_rate",
        "latency",
        "loop_signature",
        "provider_health",
        "verifier_disagreement",
        "policy_drift",
        "suspicious_tools",
        "spool_backlog",
        "worker_capacity",
    ]
    filter_ast: dict[str, Any] = Field(default_factory=dict)
    threshold: dict[str, Any]
    cooldown_seconds: int = Field(default=300, ge=0, le=604800)
    destination: dict[str, Any] = Field(default_factory=lambda: {"type": "test_webhook"})
    enabled: bool = True


class FeedbackRequest(StrictRequest):
    kind: Literal["annotation", "label", "developer_disposition", "correction"]
    document: dict[str, Any]
    corrects_feedback_id: str | None = Field(default=None, max_length=36)


class ReviewQueueRequest(StrictRequest):
    run_id: str = Field(min_length=1, max_length=128)
    queue: str = Field(min_length=1, max_length=128)
    priority: int = Field(default=0, ge=-1000, le=1000)
    reason: str = Field(min_length=1, max_length=255)


class FleetExportRequest(StrictRequest):
    filters: FleetFilters = Field(default_factory=FleetFilters)
    format: Literal["csv", "json"] = "json"


class InterrogationRequestModel(StrictRequest):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=36)
