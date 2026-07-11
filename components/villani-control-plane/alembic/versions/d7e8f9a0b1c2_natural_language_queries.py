"""natural language query plans and audit

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-12
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    workspace_fk = sa.ForeignKeyConstraint(
        ["organization_id", "workspace_id"],
        ["workspaces.organization_id", "workspaces.id"],
        ondelete="CASCADE",
    )
    op.create_table(
        "query_conversations",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("structured_context", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        workspace_fk,
    )
    op.create_index(
        "ix_query_conversations_owner",
        "query_conversations",
        ["organization_id", "workspace_id", "owner_id", "updated_at"],
    )
    op.create_table(
        "query_audit_logs",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", sa.String(36)),
        sa.Column("question_sha256", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_usage", sa.JSON(), nullable=False),
        sa.Column("query_plan", sa.JSON(), nullable=False),
        sa.Column("sql_sha256", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_category", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        workspace_fk.copy(),
    )
    op.create_index(
        "ix_query_audit_scope",
        "query_audit_logs",
        ["organization_id", "workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("query_audit_logs")
    op.drop_table("query_conversations")
