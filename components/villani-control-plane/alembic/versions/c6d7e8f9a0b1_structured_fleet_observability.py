"""structured fleet observability

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-07-11
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, Sequence[str], None] = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def tenant_columns():
    return [
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
    ]


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    additions = [
        ("agent_name", sa.String(128), None),
        ("model_name", sa.String(255), None),
        ("provider_name", sa.String(128), None),
        ("policy_version", sa.String(128), None),
        ("task_category", sa.String(128), None),
        ("verification_status", sa.String(64), None),
        ("failure_category", sa.String(128), None),
        ("cost_usd", sa.Float(), None),
        ("cost_accounting_status", sa.String(32), "unknown"),
        ("total_tokens", sa.BigInteger(), None),
        ("token_accounting_status", sa.String(32), "unknown"),
        ("duration_ms", sa.BigInteger(), None),
        ("queue_time_ms", sa.BigInteger(), None),
        ("attempt_count", sa.Integer(), "0"),
        ("escalation_count", sa.Integer(), "0"),
        ("verifier_cost_usd", sa.Float(), None),
        ("verifier_disagreement", sa.Boolean(), None),
        ("rejected_cost_usd", sa.Float(), None),
        ("tags", sa.JSON(), "[]"),
        ("tags_text", sa.Text(), ""),
    ]
    for name, type_, default in additions:
        op.add_column("runs", sa.Column(name, type_, server_default=default))
    op.create_index(
        "ix_runs_fleet_dimensions",
        "runs",
        ["organization_id", "workspace_id", "provider_name", "model_name", "status"],
    )
    op.create_index(
        "ix_runs_fleet_policy",
        "runs",
        [
            "organization_id",
            "workspace_id",
            "policy_version",
            "task_category",
            "verification_status",
        ],
    )
    op.create_index(
        "ix_runs_fleet_cost",
        "runs",
        ["organization_id", "workspace_id", "cost_usd", "total_tokens", "duration_ms"],
    )
    op.create_index(
        "ix_runs_fleet_failure",
        "runs",
        ["organization_id", "workspace_id", "failure_category", "last_observed_at"],
    )

    workspace_fk = sa.ForeignKeyConstraint(
        ["organization_id", "workspace_id"],
        ["workspaces.organization_id", "workspaces.id"],
        ondelete="CASCADE",
    )
    op.create_table(
        "saved_views",
        *tenant_columns(),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("visibility", sa.String(32), nullable=False),
        sa.Column("filter_ast", sa.JSON(), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("sort", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        *timestamps(),
        workspace_fk,
    )
    op.create_index(
        "ix_saved_views_scope",
        "saved_views",
        ["organization_id", "workspace_id", "visibility", "owner_id"],
    )
    op.create_table(
        "alert_rules",
        *tenant_columns(),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("rule_type", sa.String(64), nullable=False),
        sa.Column("filter_ast", sa.JSON(), nullable=False),
        sa.Column("threshold", sa.JSON(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("destination", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *timestamps(),
    )
    op.create_index(
        "ix_alert_rules_active",
        "alert_rules",
        ["organization_id", "workspace_id", "enabled", "rule_type"],
    )
    op.create_table(
        "alert_instances",
        *tenant_columns(),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rule_id", sa.String(36), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("last_value", sa.Float()),
        sa.Column("last_source_id", sa.String(128), nullable=False),
        sa.Column("last_fired_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.UniqueConstraint(
            "organization_id", "rule_id", "dedupe_key", name="uq_alert_instances_dedupe"
        ),
    )
    op.create_index(
        "ix_alert_instances_state",
        "alert_instances",
        ["organization_id", "workspace_id", "state", "updated_at"],
    )
    op.create_table(
        "alert_events",
        *tenant_columns(),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rule_id", sa.String(36), nullable=False),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("source_message_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "organization_id", "rule_id", "source_message_id", name="uq_alert_event_replay"
        ),
    )
    op.create_index(
        "ix_alert_events_scope", "alert_events", ["organization_id", "workspace_id", "created_at"]
    )
    run_fk = sa.ForeignKeyConstraint(
        ["organization_id", "run_id"], ["runs.organization_id", "runs.id"], ondelete="CASCADE"
    )
    op.create_table(
        "run_feedback",
        *tenant_columns(),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("corrects_feedback_id", sa.String(36)),
        sa.Column("version", sa.Integer(), nullable=False),
        *timestamps(),
        run_fk,
    )
    op.create_index(
        "ix_run_feedback_queue",
        "run_feedback",
        ["organization_id", "workspace_id", "kind", "created_at"],
    )
    op.create_table(
        "review_queue_items",
        *tenant_columns(),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("queue", sa.String(128), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("assigned_to", sa.String(128)),
        sa.Column("reason", sa.String(255), nullable=False),
        *timestamps(),
        run_fk.copy(),
    )
    op.create_index(
        "ix_review_queue",
        "review_queue_items",
        ["organization_id", "workspace_id", "queue", "state", "priority", "created_at"],
    )
    op.create_table(
        "failure_clusters",
        *tenant_columns(),
        sa.Column("signature", sa.String(64), primary_key=True),
        sa.Column("failure_category", sa.String(128), nullable=False),
        sa.Column("deterministic_label", sa.String(255), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("advisory_label", sa.String(255)),
        sa.Column("advisory_label_version", sa.String(64)),
        *timestamps(),
    )
    op.create_index(
        "ix_failure_clusters_scope",
        "failure_clusters",
        ["organization_id", "workspace_id", "occurrence_count", "last_seen_at"],
    )


def downgrade() -> None:
    for table in (
        "failure_clusters",
        "review_queue_items",
        "run_feedback",
        "alert_events",
        "alert_instances",
        "alert_rules",
        "saved_views",
    ):
        op.drop_table(table)
    for index in (
        "ix_runs_fleet_failure",
        "ix_runs_fleet_cost",
        "ix_runs_fleet_policy",
        "ix_runs_fleet_dimensions",
    ):
        op.drop_index(index, table_name="runs")
    for name in (
        "tags_text",
        "tags",
        "rejected_cost_usd",
        "verifier_disagreement",
        "verifier_cost_usd",
        "escalation_count",
        "attempt_count",
        "queue_time_ms",
        "duration_ms",
        "token_accounting_status",
        "total_tokens",
        "cost_accounting_status",
        "cost_usd",
        "failure_category",
        "verification_status",
        "task_category",
        "policy_version",
        "provider_name",
        "model_name",
        "agent_name",
    ):
        op.drop_column("runs", name)
