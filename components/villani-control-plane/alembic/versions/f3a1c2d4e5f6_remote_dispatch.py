"""controlled pull-based remote dispatch

Revision ID: f3a1c2d4e5f6
Revises: e18b9e61f721
Create Date: 2026-07-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f3a1c2d4e5f6"
down_revision: Union[str, Sequence[str], None] = "e18b9e61f721"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("installation_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("capabilities_sha256", sa.String(64), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("active_leases", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_workers_workspace_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "installation_id"],
            ["agent_installations.organization_id", "agent_installations.id"],
            name="fk_workers_installation_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id", "installation_id", "id", name="uq_workers_installation"
        ),
    )
    op.create_index(
        "ix_workers_available",
        "workers",
        ["organization_id", "workspace_id", "status", "last_heartbeat_at"],
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("internal_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("active_leases", sa.Integer(), nullable=False),
        sa.Column("capabilities_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "worker_id"],
            ["workers.organization_id", "workers.id"],
            name="fk_worker_heartbeats_worker_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_worker_heartbeats_worker",
        "worker_heartbeats",
        ["organization_id", "worker_id", "observed_at"],
    )
    op.create_table(
        "remote_tasks",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("repository_id", sa.String(128), nullable=False),
        sa.Column("repository_reference", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("submission_idempotency_key", sa.String(255), nullable=False),
        sa.Column("task_input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("task_input_sha256", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("required_capabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_reason", sa.String(255)),
        sa.Column("terminal_reason", sa.String(255)),
        sa.Column("finalization_idempotency_key", sa.String(255), nullable=False),
        sa.Column("completion_idempotency_key", sa.String(255)),
        sa.Column("completion_sha256", sa.String(64)),
        sa.Column("completion", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("materialized", sa.Boolean(), nullable=False),
        sa.Column("finalized", sa.Boolean(), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("lifecycle_span_id", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_remote_tasks_workspace_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            name="fk_remote_tasks_run_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "repository_id"],
            ["repositories.organization_id", "repositories.id"],
            name="fk_remote_tasks_repository_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "submission_idempotency_key",
            name="uq_remote_tasks_submission_idempotency",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "finalization_idempotency_key",
            name="uq_remote_tasks_finalization_idempotency",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "completion_idempotency_key",
            name="uq_remote_tasks_completion_idempotency",
        ),
    )
    op.create_index(
        "ix_remote_tasks_claim",
        "remote_tasks",
        [
            "organization_id",
            "workspace_id",
            "state",
            "next_eligible_at",
            "priority",
            "created_at",
        ],
    )
    op.create_index(
        "ix_remote_tasks_run", "remote_tasks", ["organization_id", "run_id", "created_at"]
    )
    op.execute(
        """
        CREATE FUNCTION villani_remote_task_immutable() RETURNS trigger AS $$
        BEGIN
          IF NEW.task_input IS DISTINCT FROM OLD.task_input
             OR NEW.task_input_sha256 IS DISTINCT FROM OLD.task_input_sha256
             OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
             OR NEW.repository_id IS DISTINCT FROM OLD.repository_id
             OR NEW.repository_reference IS DISTINCT FROM OLD.repository_reference
             OR NEW.required_capabilities IS DISTINCT FROM OLD.required_capabilities
             OR NEW.priority IS DISTINCT FROM OLD.priority
             OR NEW.deadline IS DISTINCT FROM OLD.deadline
             OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
             OR NEW.submission_idempotency_key IS DISTINCT FROM OLD.submission_idempotency_key
             OR NEW.finalization_idempotency_key IS DISTINCT FROM OLD.finalization_idempotency_key
          THEN
            RAISE EXCEPTION 'remote task input and policy fields are immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_remote_tasks_immutable
        BEFORE UPDATE ON remote_tasks
        FOR EACH ROW EXECUTE FUNCTION villani_remote_task_immutable();
        """
    )
    op.create_table(
        "task_leases",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("installation_id", sa.String(128), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("completion_idempotency_key", sa.String(255)),
        sa.Column("completion_sha256", sa.String(64)),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["remote_tasks.organization_id", "remote_tasks.id"],
            name="fk_task_leases_task_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "worker_id"],
            ["workers.organization_id", "workers.id"],
            name="fk_task_leases_worker_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "installation_id"],
            ["agent_installations.organization_id", "agent_installations.id"],
            name="fk_task_leases_installation_tenant",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id", "task_id", "attempt_number", name="uq_task_leases_attempt"
        ),
    )
    op.create_index("ix_task_leases_expiration", "task_leases", ["state", "expires_at"])
    op.create_index(
        "ix_task_leases_worker",
        "task_leases",
        ["organization_id", "worker_id", "state"],
    )
    op.create_index(
        "uq_task_leases_one_active_task",
        "task_leases",
        ["organization_id", "task_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_task_leases_one_active_task", table_name="task_leases")
    op.drop_index("ix_task_leases_worker", table_name="task_leases")
    op.drop_index("ix_task_leases_expiration", table_name="task_leases")
    op.drop_table("task_leases")
    op.execute("DROP TRIGGER IF EXISTS trg_remote_tasks_immutable ON remote_tasks")
    op.execute("DROP FUNCTION IF EXISTS villani_remote_task_immutable()")
    op.drop_index("ix_remote_tasks_run", table_name="remote_tasks")
    op.drop_index("ix_remote_tasks_claim", table_name="remote_tasks")
    op.drop_table("remote_tasks")
    op.drop_index("ix_worker_heartbeats_worker", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
    op.drop_index("ix_workers_available", table_name="workers")
    op.drop_table("workers")
