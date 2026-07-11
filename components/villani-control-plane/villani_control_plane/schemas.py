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


class EventPage(BaseModel):
    events: list[dict[str, Any]]
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


class RunList(BaseModel):
    runs: list[RunSummary]
