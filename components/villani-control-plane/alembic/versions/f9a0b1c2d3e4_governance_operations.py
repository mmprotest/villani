"""data governance, quotas, encryption metadata, and tamper commitments

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-07-12
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, Sequence[str], None] = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.add_column(
        "organizations", sa.Column("region", sa.String(64), nullable=False, server_default="local")
    )
    op.add_column(
        "organizations",
        sa.Column("residency_labels", sa.JSON(), nullable=False, server_default="[]"),
    )
    for table in ("workspaces", "projects"):
        op.add_column(table, sa.Column("region", sa.String(64)))
        op.add_column(
            table, sa.Column("residency_labels", sa.JSON(), nullable=False, server_default="[]")
        )
    op.add_column(
        "projects", sa.Column("chargeback_tags", sa.JSON(), nullable=False, server_default="{}")
    )
    op.add_column(
        "administrative_audit_events",
        sa.Column("previous_hash", sa.String(64), nullable=False, server_default="0" * 64),
    )
    op.add_column(
        "administrative_audit_events",
        sa.Column("event_hash", sa.String(64), nullable=False, server_default="0" * 64),
    )
    op.add_column("administrative_audit_events", sa.Column("corrects_event_id", sa.String(36)))
    op.add_column("events", sa.Column("retention_expires_at", sa.DateTime(timezone=True)))
    op.add_column("artifacts", sa.Column("retention_expires_at", sa.DateTime(timezone=True)))

    op.create_table(
        "governance_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128)),
        sa.Column("project_id", sa.String(128)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("retention_days", sa.JSON(), nullable=False),
        sa.Column("metadata_only", sa.Boolean(), nullable=False),
        sa.Column("exclusions", sa.JSON(), nullable=False),
        sa.Column("redaction_rules", sa.JSON(), nullable=False),
        sa.Column("dlp_hook", sa.String(64), nullable=False),
        sa.Column("allowed_regions", sa.JSON(), nullable=False),
        sa.Column("required_residency_labels", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "ix_governance_policy_scope",
        "governance_policies",
        ["organization_id", "workspace_id", "project_id", "active"],
    )
    op.create_table(
        "legal_holds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_index(
        "ix_legal_hold_target",
        "legal_holds",
        ["organization_id", "target_type", "target_id", "active"],
    )
    op.create_table(
        "deletion_workflows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("tombstone", sa.JSON(), nullable=False),
        sa.Column("completion_evidence", sa.JSON(), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *_timestamps(),
    )
    op.create_table(
        "governance_exports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("digest_sha256", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "quota_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128)),
        sa.Column("project_id", sa.String(128)),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("soft_percent", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        *_timestamps(),
    )
    op.create_index(
        "ix_quota_policy_scope",
        "quota_policies",
        ["organization_id", "workspace_id", "project_id", "active"],
    )
    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("project_id", sa.String(128)),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("chargeback_tags", sa.JSON(), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "metric", "source_id", name="uq_usage_metric_source"
        ),
    )
    op.create_index(
        "ix_usage_scope_metric",
        "usage_records",
        ["organization_id", "workspace_id", "project_id", "metric", "recorded_at"],
    )
    op.create_table(
        "key_rotation_metadata",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("key_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("previous_key_id", sa.String(128)),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint("organization_id", "key_id", "version", name="uq_key_rotation_version"),
    )
    op.create_table(
        "run_commitments",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("root_sha256", sa.String(64), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correction_of_root", sa.String(64)),
    )


def downgrade() -> None:
    for table in (
        "run_commitments",
        "key_rotation_metadata",
        "usage_records",
        "quota_policies",
        "governance_exports",
        "deletion_workflows",
        "legal_holds",
        "governance_policies",
    ):
        op.drop_table(table)
    for column in ("corrects_event_id", "event_hash", "previous_hash"):
        op.drop_column("administrative_audit_events", column)
    op.drop_column("artifacts", "retention_expires_at")
    op.drop_column("events", "retention_expires_at")
    op.drop_column("projects", "chargeback_tags")
    for table in ("projects", "workspaces"):
        op.drop_column(table, "residency_labels")
        op.drop_column(table, "region")
    op.drop_column("organizations", "residency_labels")
    op.drop_column("organizations", "region")
