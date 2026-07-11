"""offline evaluation policy publication lifecycle

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_publications",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("policy_id", sa.String(128), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("prior_publication_id", sa.String(36)),
        sa.Column("canary_percentage", sa.Float(), nullable=False),
        sa.Column("rollback_thresholds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evaluation_provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manual_approval_required", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "workspace_id"], ["workspaces.organization_id", "workspaces.id"], name="fk_policy_publications_workspace", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "prior_publication_id"], ["policy_publications.organization_id", "policy_publications.id"], name="fk_policy_publications_prior"),
        sa.UniqueConstraint("organization_id", "workspace_id", "policy_id", "policy_version", name="uq_policy_publication_version"),
    )
    op.create_index("ix_policy_publications_policy", "policy_publications", ["organization_id", "workspace_id", "policy_id", "created_at"])
    op.execute(
        """
        CREATE FUNCTION villani_policy_publication_immutable() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'policy publications are immutable';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_policy_publication_immutable
        BEFORE UPDATE OR DELETE ON policy_publications
        FOR EACH ROW EXECUTE FUNCTION villani_policy_publication_immutable();
        """
    )
    op.create_table(
        "policy_publication_transitions",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("publication_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "publication_id"], ["policy_publications.organization_id", "policy_publications.id"], name="fk_policy_transition_publication", ondelete="CASCADE"),
    )
    op.create_index("ix_policy_transitions_publication", "policy_publication_transitions", ["organization_id", "publication_id", "created_at", "id"])
    op.create_table(
        "policy_publication_approvals",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("publication_id", sa.String(36), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "publication_id"], ["policy_publications.organization_id", "policy_publications.id"], name="fk_policy_approval_publication", ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "publication_id", name="uq_policy_publication_approval"),
    )
    op.create_table(
        "policy_safety_controls",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("workspace_id", sa.String(128), primary_key=True),
        sa.Column("globally_disabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "workspace_id"], ["workspaces.organization_id", "workspaces.id"], name="fk_policy_safety_workspace", ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("policy_safety_controls")
    op.drop_table("policy_publication_approvals")
    op.drop_index("ix_policy_transitions_publication", table_name="policy_publication_transitions")
    op.drop_table("policy_publication_transitions")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_publication_immutable ON policy_publications")
    op.execute("DROP FUNCTION IF EXISTS villani_policy_publication_immutable()")
    op.drop_index("ix_policy_publications_policy", table_name="policy_publications")
    op.drop_table("policy_publications")
