from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    __table_args__ = (UniqueConstraint("name", name="uq_organizations_name"),)


class Workspace(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "workspaces"
    organization_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("organizations.id", ondelete="RESTRICT"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
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
        UniqueConstraint("organization_id", "run_id", "attempt_key", name="uq_outcomes_attempt"),
        Index("ix_outcomes_run", "organization_id", "run_id"),
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
