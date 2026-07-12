from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    event as sqlalchemy_event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Organization(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    region: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    residency_labels: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    __table_args__ = (UniqueConstraint("name", name="uq_organizations_name"),)


class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)


class Identity(Base, TimestampMixin):
    __tablename__ = "identities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False, default="local")
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    __table_args__ = (
        UniqueConstraint("provider", "issuer", "subject", name="uq_identities_subject"),
        Index("ix_identities_user", "user_id"),
    )


class Membership(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_memberships_org_user"),
        Index("ix_memberships_org_active", "organization_id", "status", "deleted_at"),
    )


class Group(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "groups"
    organization_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_groups_org_name"),)


class GroupMembership(Base):
    __tablename__ = "group_memberships"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    membership_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "group_id"],
            ["groups.organization_id", "groups.id"],
            ondelete="CASCADE",
            name="fk_group_memberships_group",
        ),
        ForeignKeyConstraint(
            ["membership_id"],
            ["memberships.id"],
            ondelete="CASCADE",
            name="fk_group_memberships_membership",
        ),
    )


class Role(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    built_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permissions: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_roles_org_name"),
        Index("ix_roles_org_active", "organization_id", "deleted_at"),
    )


class RoleAssignment(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "role_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "role_id",
            "subject_type",
            "subject_id",
            "workspace_id",
            name="uq_role_assignments_scope",
        ),
        Index("ix_role_assignments_subject", "organization_id", "subject_type", "subject_id"),
    )


class Workspace(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "workspaces"
    organization_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    residency_labels: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_workspaces_org_name"),
        Index("ix_workspaces_org_active", "organization_id", "deleted_at"),
    )


class Project(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "projects"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255))
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    residency_labels: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    chargeback_tags: Mapped[dict[str, str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_projects_workspace_tenant",
        ),
        UniqueConstraint("organization_id", "workspace_id", "name", name="uq_projects_name"),
        Index("ix_projects_workspace_active", "organization_id", "workspace_id", "deleted_at"),
    )


class Repository(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "repositories"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255))
    canonical_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_repositories_workspace_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_repositories_project_tenant",
        ),
        UniqueConstraint("organization_id", "project_id", "name", name="uq_repositories_name"),
        Index("ix_repositories_project_active", "organization_id", "project_id", "deleted_at"),
    )


class Run(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "runs"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    repository_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="unknown")
    agent_name: Mapped[str | None] = mapped_column(String(128))
    model_name: Mapped[str | None] = mapped_column(String(255))
    provider_name: Mapped[str | None] = mapped_column(String(128))
    policy_version: Mapped[str | None] = mapped_column(String(128))
    task_category: Mapped[str | None] = mapped_column(String(128))
    verification_status: Mapped[str | None] = mapped_column(String(64))
    failure_category: Mapped[str | None] = mapped_column(String(128))
    cost_usd: Mapped[float | None] = mapped_column(Float)
    cost_accounting_status: Mapped[str] = mapped_column(String(32), default="unknown")
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    token_accounting_status: Mapped[str] = mapped_column(String(32), default="unknown")
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    queue_time_ms: Mapped[int | None] = mapped_column(BigInteger)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    escalation_count: Mapped[int] = mapped_column(Integer, default=0)
    verifier_cost_usd: Mapped[float | None] = mapped_column(Float)
    verifier_disagreement: Mapped[bool | None] = mapped_column(Boolean)
    rejected_cost_usd: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list)
    tags_text: Mapped[str] = mapped_column(Text, default="")
    first_occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_runs_workspace_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_runs_project_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            ondelete="RESTRICT",
            name="fk_runs_repository_tenant",
        ),
        Index("ix_runs_tenant_filters", "organization_id", "project_id", "repository_id", "status"),
        Index("ix_runs_tenant_time", "organization_id", "last_observed_at", "id"),
        Index(
            "ix_runs_fleet_dimensions",
            "organization_id",
            "workspace_id",
            "provider_name",
            "model_name",
            "status",
        ),
        Index(
            "ix_runs_fleet_policy",
            "organization_id",
            "workspace_id",
            "policy_version",
            "task_category",
            "verification_status",
        ),
        Index(
            "ix_runs_fleet_cost",
            "organization_id",
            "workspace_id",
            "cost_usd",
            "total_tokens",
            "duration_ms",
        ),
        Index(
            "ix_runs_fleet_failure",
            "organization_id",
            "workspace_id",
            "failure_category",
            "last_observed_at",
        ),
    )


class Attempt(Base, TimestampMixin):
    __tablename__ = "attempts"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="unknown")
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
            name="fk_attempts_run_tenant",
        ),
        Index("ix_attempts_run", "organization_id", "run_id"),
    )


class Span(Base, TimestampMixin):
    __tablename__ = "spans"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    kind: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
            name="fk_spans_run_tenant",
        ),
        Index("ix_spans_run", "organization_id", "run_id"),
    )


class Event(Base):
    __tablename__ = "events"
    internal_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    repository_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    sequence_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    retention_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
            name="fk_events_run_tenant",
        ),
        UniqueConstraint("organization_id", "event_id", name="uq_events_tenant_event"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_events_tenant_idempotency"),
        UniqueConstraint(
            "organization_id",
            "run_id",
            "sequence_scope",
            "sequence",
            name="uq_events_tenant_run_sequence",
        ),
        Index("ix_events_run_cursor", "organization_id", "run_id", "observed_at", "internal_id"),
        Index("ix_events_run_occurred", "organization_id", "run_id", "occurred_at"),
    )


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    digest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    upload_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    upload_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    upload_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    retention_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_artifacts_workspace_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
            name="fk_artifacts_run_tenant",
        ),
        UniqueConstraint(
            "organization_id", "run_id", "digest_sha256", name="uq_artifacts_run_digest"
        ),
        Index("ix_artifacts_run", "organization_id", "run_id"),
        Index("ix_artifacts_digest_status", "organization_id", "digest_sha256", "status"),
        UniqueConstraint("upload_id", name="uq_artifacts_upload_id"),
    )


class Outcome(Base, TimestampMixin):
    __tablename__ = "outcomes"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_outcome_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    capability_success_label: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_outcomes_workspace_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
            name="fk_outcomes_run_tenant",
        ),
        UniqueConstraint(
            "organization_id",
            "run_id",
            "attempt_key",
            "version",
            name="uq_outcomes_attempt_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supersedes_outcome_id"],
            ["outcomes.organization_id", "outcomes.id"],
            name="fk_outcomes_supersedes",
        ),
        Index("ix_outcomes_run", "organization_id", "run_id"),
    )


class OutcomeSignal(Base):
    __tablename__ = "outcome_signals"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    correction_of_signal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
            name="fk_outcome_signals_run_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "correction_of_signal_id"],
            ["outcome_signals.organization_id", "outcome_signals.id"],
            name="fk_outcome_signals_correction",
        ),
        UniqueConstraint(
            "organization_id",
            "source_provider",
            "source_event_id",
            "signal_type",
            name="uq_outcome_signal_source",
        ),
        Index("ix_outcome_signals_run", "organization_id", "run_id", "observed_at"),
    )


class ShadowRoutingObservation(Base):
    __tablename__ = "shadow_routing_observations"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    recommendation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    shadow_strategy: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actual_strategy: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shadow_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    actual_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
            name="fk_shadow_observations_run_tenant",
        ),
        UniqueConstraint(
            "organization_id",
            "run_id",
            "recommendation_id",
            name="uq_shadow_observation_recommendation",
        ),
        Index("ix_shadow_observations_metrics", "organization_id", "workspace_id", "recorded_at"),
    )


class PolicyPublication(Base):
    __tablename__ = "policy_publications"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prior_publication_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    canary_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    rollback_thresholds: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    evaluation_provenance: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    manual_approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_policy_publications_workspace",
        ),
        ForeignKeyConstraint(
            ["organization_id", "prior_publication_id"],
            ["policy_publications.organization_id", "policy_publications.id"],
            name="fk_policy_publications_prior",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "policy_id",
            "policy_version",
            name="uq_policy_publication_version",
        ),
        Index(
            "ix_policy_publications_policy",
            "organization_id",
            "workspace_id",
            "policy_id",
            "created_at",
        ),
    )


class PolicyPublicationTransition(Base):
    __tablename__ = "policy_publication_transitions"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    publication_id: Mapped[str] = mapped_column(String(36), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "publication_id"],
            ["policy_publications.organization_id", "policy_publications.id"],
            ondelete="CASCADE",
            name="fk_policy_transition_publication",
        ),
        Index(
            "ix_policy_transitions_publication",
            "organization_id",
            "publication_id",
            "created_at",
            "id",
        ),
    )


class PolicyPublicationApproval(Base):
    __tablename__ = "policy_publication_approvals"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    publication_id: Mapped[str] = mapped_column(String(36), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "publication_id"],
            ["policy_publications.organization_id", "policy_publications.id"],
            ondelete="CASCADE",
            name="fk_policy_approval_publication",
        ),
        UniqueConstraint(
            "organization_id", "publication_id", name="uq_policy_publication_approval"
        ),
    )


class PolicySafetyControl(Base, TimestampMixin):
    __tablename__ = "policy_safety_controls"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    globally_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="not_disabled")
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="CASCADE",
            name="fk_policy_safety_workspace",
        ),
    )


class AgentInstallation(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "agent_installations"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict)
    credential_lookup_digest: Mapped[str | None] = mapped_column(String(64), unique=True)
    credential_hash: Mapped[str | None] = mapped_column(Text)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    credential_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_agent_installations_workspace_tenant",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "agent_name", "id", name="uq_agent_installation"
        ),
        Index("ix_agent_installations_workspace", "organization_id", "workspace_id", "deleted_at"),
    )


class IngestBatch(Base):
    __tablename__ = "ingest_batches"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(32), default="v2", server_default="v2")
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    installation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_ingest_batches_workspace_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "installation_id"],
            ["agent_installations.organization_id", "agent_installations.id"],
            ondelete="RESTRICT",
            name="fk_ingest_batches_installation_tenant",
        ),
        Index("ix_ingest_batches_received", "organization_id", "workspace_id", "received_at"),
    )


class ApiToken(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "api_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    lookup_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=lambda: ["*"])
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_from_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    service_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("service_accounts.id", ondelete="CASCADE"), nullable=True
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_api_tokens_workspace_tenant",
        ),
        UniqueConstraint("organization_id", "workspace_id", "name", name="uq_api_tokens_name"),
        Index("ix_api_tokens_tenant_active", "organization_id", "workspace_id", "deleted_at"),
    )


class ServiceAccount(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "service_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_service_accounts_workspace",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "name", name="uq_service_accounts_name"
        ),
    )


class BrowserSession(Base, TimestampMixin):
    __tablename__ = "browser_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    lookup_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_ip_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        Index("ix_browser_sessions_active", "lookup_digest", "expires_at", "revoked_at"),
    )


class Invitation(Base, TimestampMixin):
    __tablename__ = "invitations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role_ids: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    invited_by: Mapped[str] = mapped_column(String(128), nullable=False)
    token_lookup_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AdministrativeAuditEvent(Base):
    __tablename__ = "administrative_audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_ip_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    before_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="0" * 64)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    corrects_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (Index("ix_admin_audit_org_time", "organization_id", "occurred_at", "id"),)


@sqlalchemy_event.listens_for(AdministrativeAuditEvent, "before_update")
@sqlalchemy_event.listens_for(AdministrativeAuditEvent, "before_delete")
def _administrative_audit_events_are_immutable(*_args) -> None:
    raise ValueError("administrative audit events are immutable")


class GovernancePolicy(Base, TimestampMixin):
    __tablename__ = "governance_policies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retention_days: Mapped[dict[str, int]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    metadata_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusions: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    redaction_rules: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    dlp_hook: Mapped[str] = mapped_column(String(64), nullable=False, default="builtin")
    allowed_regions: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    required_residency_labels: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        Index(
            "ix_governance_policy_scope", "organization_id", "workspace_id", "project_id", "active"
        ),
    )


class LegalHold(Base, TimestampMixin):
    __tablename__ = "legal_holds"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("ix_legal_hold_target", "organization_id", "target_type", "target_id", "active"),
    )


class DeletionWorkflow(Base, TimestampMixin):
    __tablename__ = "deletion_workflows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="requested")
    tombstone: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    completion_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GovernanceExport(Base, TimestampMixin):
    __tablename__ = "governance_exports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    digest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)


class QuotaPolicy(Base, TimestampMixin):
    __tablename__ = "quota_policies"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    limits: Mapped[dict[str, float]] = mapped_column(JSON_DOCUMENT, nullable=False)
    soft_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=80)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        Index("ix_quota_policy_scope", "organization_id", "workspace_id", "project_id", "active"),
    )


class UsageRecord(Base):
    __tablename__ = "usage_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metric: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    chargeback_tags: Mapped[dict[str, str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    __table_args__ = (
        UniqueConstraint("organization_id", "metric", "source_id", name="uq_usage_metric_source"),
        Index(
            "ix_usage_scope_metric",
            "organization_id",
            "workspace_id",
            "project_id",
            "metric",
            "recorded_at",
        ),
    )


class KeyRotationMetadata(Base, TimestampMixin):
    __tablename__ = "key_rotation_metadata"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    key_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_key_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "key_id", "version", name="uq_key_rotation_version"),
    )


class RunCommitment(Base):
    __tablename__ = "run_commitments"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    root_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    finalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    correction_of_root: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_outbox_workspace_tenant",
        ),
        Index("ix_outbox_pending", "published_at", "created_at"),
        Index("ix_outbox_tenant", "organization_id", "workspace_id", "created_at"),
    )


class EnrollmentToken(Base, TimestampMixin):
    __tablename__ = "enrollment_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lookup_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_enrollment_tokens_workspace_tenant",
        ),
        Index("ix_enrollment_tokens_active", "lookup_digest", "expires_at", "used_at"),
    )


class Worker(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "workers"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    installation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="online")
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    capabilities_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    active_leases: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_workers_workspace_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "installation_id"],
            ["agent_installations.organization_id", "agent_installations.id"],
            ondelete="RESTRICT",
            name="fk_workers_installation_tenant",
        ),
        UniqueConstraint(
            "organization_id", "installation_id", "id", name="uq_workers_installation"
        ),
        Index(
            "ix_workers_available",
            "organization_id",
            "workspace_id",
            "status",
            "last_heartbeat_at",
        ),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    internal_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    organization_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    active_leases: Mapped[int] = mapped_column(Integer, nullable=False)
    capabilities_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "worker_id"],
            ["workers.organization_id", "workers.id"],
            ondelete="CASCADE",
            name="fk_worker_heartbeats_worker_tenant",
        ),
        Index(
            "ix_worker_heartbeats_worker",
            "organization_id",
            "worker_id",
            "observed_at",
        ),
    )


class RemoteTask(Base, TimestampMixin):
    __tablename__ = "remote_tasks"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    repository_id: Mapped[str] = mapped_column(String(128), nullable=False)
    repository_reference: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    submission_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    task_input: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    task_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    required_capabilities: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    next_eligible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(255))
    terminal_reason: Mapped[str | None] = mapped_column(String(255))
    finalization_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    completion_idempotency_key: Mapped[str | None] = mapped_column(String(255))
    completion_sha256: Mapped[str | None] = mapped_column(String(64))
    completion: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    materialized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    finalized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    lifecycle_span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="RESTRICT",
            name="fk_remote_tasks_workspace_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
            name="fk_remote_tasks_run_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            ondelete="RESTRICT",
            name="fk_remote_tasks_repository_tenant",
        ),
        UniqueConstraint(
            "organization_id",
            "submission_idempotency_key",
            name="uq_remote_tasks_submission_idempotency",
        ),
        UniqueConstraint(
            "organization_id",
            "finalization_idempotency_key",
            name="uq_remote_tasks_finalization_idempotency",
        ),
        UniqueConstraint(
            "organization_id",
            "completion_idempotency_key",
            name="uq_remote_tasks_completion_idempotency",
        ),
        Index(
            "ix_remote_tasks_claim",
            "organization_id",
            "workspace_id",
            "state",
            "next_eligible_at",
            "priority",
            "created_at",
        ),
        Index("ix_remote_tasks_run", "organization_id", "run_id", "created_at"),
    )


class TaskLease(Base):
    __tablename__ = "task_leases"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    installation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_idempotency_key: Mapped[str | None] = mapped_column(String(255))
    completion_sha256: Mapped[str | None] = mapped_column(String(64))
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["remote_tasks.organization_id", "remote_tasks.id"],
            ondelete="CASCADE",
            name="fk_task_leases_task_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "worker_id"],
            ["workers.organization_id", "workers.id"],
            ondelete="RESTRICT",
            name="fk_task_leases_worker_tenant",
        ),
        ForeignKeyConstraint(
            ["organization_id", "installation_id"],
            ["agent_installations.organization_id", "agent_installations.id"],
            ondelete="RESTRICT",
            name="fk_task_leases_installation_tenant",
        ),
        UniqueConstraint(
            "organization_id", "task_id", "attempt_number", name="uq_task_leases_attempt"
        ),
        Index("ix_task_leases_expiration", "state", "expires_at"),
        Index("ix_task_leases_worker", "organization_id", "worker_id", "state"),
    )


class SavedView(Base, TimestampMixin):
    __tablename__ = "saved_views"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, default="private")
    filter_ast: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    columns: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    sort: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="CASCADE",
        ),
        Index("ix_saved_views_scope", "organization_id", "workspace_id", "visibility", "owner_id"),
    )


class AlertRule(Base, TimestampMixin):
    __tablename__ = "alert_rules"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(64), nullable=False)
    filter_ast: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    threshold: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    destination: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="CASCADE",
        ),
        Index("ix_alert_rules_active", "organization_id", "workspace_id", "enabled", "rule_type"),
    )


class AlertInstance(Base, TimestampMixin):
    __tablename__ = "alert_instances"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    last_value: Mapped[float | None] = mapped_column(Float)
    last_source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "rule_id", "dedupe_key", name="uq_alert_instances_dedupe"
        ),
        Index("ix_alert_instances_state", "organization_id", "workspace_id", "state", "updated_at"),
    )


class AlertEvent(Base, TimestampMixin):
    __tablename__ = "alert_events"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "rule_id", "source_message_id", name="uq_alert_event_replay"
        ),
        Index("ix_alert_events_scope", "organization_id", "workspace_id", "created_at"),
    )


class RunFeedback(Base, TimestampMixin):
    __tablename__ = "run_feedback"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    corrects_feedback_id: Mapped[str | None] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "run_id"], ["runs.organization_id", "runs.id"], ondelete="CASCADE"
        ),
        Index("ix_run_feedback_queue", "organization_id", "workspace_id", "kind", "created_at"),
    )


class ReviewQueueItem(Base, TimestampMixin):
    __tablename__ = "review_queue_items"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    queue: Mapped[str] = mapped_column(String(128), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    assigned_to: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "run_id"], ["runs.organization_id", "runs.id"], ondelete="CASCADE"
        ),
        Index(
            "ix_review_queue",
            "organization_id",
            "workspace_id",
            "queue",
            "state",
            "priority",
            "created_at",
        ),
    )


class FailureCluster(Base, TimestampMixin):
    __tablename__ = "failure_clusters"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    failure_category: Mapped[str] = mapped_column(String(128), nullable=False)
    deterministic_label: Mapped[str] = mapped_column(String(255), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    advisory_label: Mapped[str | None] = mapped_column(String(255))
    advisory_label_version: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        Index(
            "ix_failure_clusters_scope",
            "organization_id",
            "workspace_id",
            "occurrence_count",
            "last_seen_at",
        ),
    )


class QueryConversation(Base, TimestampMixin):
    __tablename__ = "query_conversations"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    structured_context: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_query_conversations_owner",
            "organization_id",
            "workspace_id",
            "owner_id",
            "updated_at",
        ),
    )


class QueryAuditLog(Base, TimestampMixin):
    __tablename__ = "query_audit_logs"
    organization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(String(36))
    question_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_usage: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    query_plan: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    sql_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(128))
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            ondelete="CASCADE",
        ),
        Index(
            "ix_query_audit_scope",
            "organization_id",
            "workspace_id",
            "created_at",
        ),
    )
