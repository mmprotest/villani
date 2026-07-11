"""Strict Python models for the Villani v2 transport and platform contracts."""

from __future__ import annotations

import re
from typing import Any, Literal, TypeAlias

from pydantic import Field, field_validator, model_validator

from .protocol import AccountingStatus, StrictProtocolModel, UtcDateTime, _check_accounting


ProvenanceStatus: TypeAlias = Literal["recorded", "derived", "unknown"]
SPAN_KINDS = frozenset(
    {
        "controller_stage",
        "agent_run",
        "model_call",
        "tool_call",
        "command",
        "file_operation",
        "verifier",
        "policy_decision",
        "selection",
        "materialization",
        "queue",
        "external_service",
    }
)
_NAME = re.compile(r"^[a-z][a-z0-9_.-]*$")


class ResourceV2(StrictProtocolModel):
    schema_version: Literal["villani.resource.v2"]
    service_name: str = Field(min_length=1)
    service_version: str | None = Field(min_length=1)
    deployment_environment: str | None = Field(min_length=1)
    host_id: str | None = Field(min_length=1)
    process_id: str | None = Field(min_length=1)
    attributes: dict[str, Any]


class DigestV2(StrictProtocolModel):
    algorithm: Literal["sha256"]
    value: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactDescriptorV2(StrictProtocolModel):
    schema_version: Literal["villani.artifact_descriptor.v2"]
    artifact_id: str = Field(min_length=1)
    digest: DigestV2
    size_bytes: int = Field(ge=0)
    media_type: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    logical_role: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    sensitivity: Literal["public", "internal", "confidential", "restricted", "secret"]
    retention_class: Literal["ephemeral", "run", "project", "compliance", "legal_hold"]
    encryption_status: Literal["unencrypted", "encrypted", "unknown"]
    storage_reference: str | None = Field(min_length=1)
    provenance_status: ProvenanceStatus
    attributes: dict[str, Any]


class OutcomeV2(StrictProtocolModel):
    schema_version: Literal["villani.outcome.v2"]
    run_id: str = Field(min_length=1)
    attempt_id: str | None = Field(min_length=1)
    verification_status: Literal["accepted", "rejected", "unclear", "error", "not_run"] | None
    accepted: bool | None
    materialized: bool | None
    merged: bool | None
    reverted: bool | None
    ci_state: Literal["pending", "passed", "failed", "cancelled", "not_run"] | None
    developer_disposition: Literal["approved", "rejected", "modified", "pending", "not_reviewed"] | None
    defect_association: str | None = Field(min_length=1)
    cost: float | None = Field(ge=0)
    currency: str | None = Field(pattern=r"^[A-Z]{3}$")
    cost_accounting_status: AccountingStatus
    latency_ms: int | None = Field(ge=0)
    latency_accounting_status: AccountingStatus
    provenance_status: ProvenanceStatus
    provenance: dict[str, Any]

    @model_validator(mode="after")
    def validate_unknowns(self) -> "OutcomeV2":
        _check_accounting(self.cost_accounting_status, (self.cost,), "cost")
        _check_accounting(self.latency_accounting_status, (self.latency_ms,), "latency")
        if self.cost is None and self.currency is not None:
            raise ValueError("currency must be null when cost is null")
        if self.cost is not None and self.currency is None:
            raise ValueError("currency is required when cost is known")
        return self


class SpanV2(StrictProtocolModel):
    schema_version: Literal["villani.span.v2"]
    trace_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    span_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    parent_span_id: str | None = Field(pattern=r"^[a-f0-9]{16}$")
    run_id: str = Field(min_length=1)
    attempt_id: str | None = Field(min_length=1)
    kind: str
    name: str = Field(min_length=1)
    status: str
    started_at: UtcDateTime | None
    ended_at: UtcDateTime | None
    attributes: dict[str, Any]

    @field_validator("kind", "status")
    @classmethod
    def validate_open_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("must be a lower-case extensible protocol name")
        return value

    @field_validator("trace_id", "span_id", "parent_span_id")
    @classmethod
    def validate_nonzero_id(cls, value: str | None) -> str | None:
        if value is not None and set(value) == {"0"}:
            raise ValueError("W3C trace and span identifiers must be non-zero")
        return value


class AgentCapabilityV2(StrictProtocolModel):
    schema_version: Literal["villani.agent_capability.v2"]
    capability_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    agent_version: str | None = Field(min_length=1)
    runner_protocols: list[str]
    models: list[str]
    features: list[str]
    limits: dict[str, Any]
    published_at: UtcDateTime
    provenance_status: ProvenanceStatus
    attributes: dict[str, Any]


class VerifierCapabilityV2(StrictProtocolModel):
    schema_version: Literal["villani.verifier_capability.v2"]
    capability_id: str = Field(min_length=1)
    verifier_name: str = Field(min_length=1)
    verifier_version: str | None = Field(min_length=1)
    evidence_kinds: list[str]
    task_categories: list[str]
    supports_acceptance_grade: bool
    limits: dict[str, Any]
    published_at: UtcDateTime
    provenance_status: ProvenanceStatus
    attributes: dict[str, Any]


class PolicyScopeV2(StrictProtocolModel):
    organization_id: str | None = Field(min_length=1)
    workspace_id: str | None = Field(min_length=1)
    project_id: str | None = Field(min_length=1)
    repository_id: str | None = Field(min_length=1)


class PolicyPublicationV2(StrictProtocolModel):
    schema_version: Literal["villani.policy_publication.v2"]
    publication_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    published_at: UtcDateTime
    effective_at: UtcDateTime
    expires_at: UtcDateTime | None
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: PolicyScopeV2
    rules: dict[str, Any]
    provenance_status: ProvenanceStatus
    attributes: dict[str, Any]


class TelemetryEnvelopeV2(StrictProtocolModel):
    schema_version: Literal["villani.telemetry_envelope.v2"]
    event_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    occurred_at: UtcDateTime
    observed_at: UtcDateTime
    sequence: int = Field(ge=1)
    sequence_scope: str = Field(min_length=1)
    organization_id: str | None = Field(min_length=1)
    workspace_id: str | None = Field(min_length=1)
    project_id: str | None = Field(min_length=1)
    repository_id: str | None = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    span_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    parent_span_id: str | None = Field(pattern=r"^[a-f0-9]{16}$")
    attempt_id: str | None = Field(min_length=1)
    source: str
    kind: str
    name: str = Field(min_length=1)
    status: str
    resource: ResourceV2
    attributes: dict[str, Any]
    body: dict[str, Any]

    @field_validator("source", "kind", "status")
    @classmethod
    def validate_open_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("must be a lower-case extensible protocol name")
        return value

    @field_validator("trace_id", "span_id", "parent_span_id")
    @classmethod
    def validate_nonzero_id(cls, value: str | None) -> str | None:
        if value is not None and set(value) == {"0"}:
            raise ValueError("W3C trace and span identifiers must be non-zero")
        return value


ProtocolDocumentV2: TypeAlias = (
    ResourceV2 | ArtifactDescriptorV2 | OutcomeV2 | SpanV2 | AgentCapabilityV2
    | VerifierCapabilityV2 | PolicyPublicationV2 | TelemetryEnvelopeV2
)

PROTOCOL_V2_MODEL_BY_VERSION: dict[str, type[StrictProtocolModel]] = {
    "villani.resource.v2": ResourceV2,
    "villani.artifact_descriptor.v2": ArtifactDescriptorV2,
    "villani.outcome.v2": OutcomeV2,
    "villani.span.v2": SpanV2,
    "villani.agent_capability.v2": AgentCapabilityV2,
    "villani.verifier_capability.v2": VerifierCapabilityV2,
    "villani.policy_publication.v2": PolicyPublicationV2,
    "villani.telemetry_envelope.v2": TelemetryEnvelopeV2,
}
