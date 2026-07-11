"""artifact transfer, enrollment, synchronization, and live outbox leases

Revision ID: e18b9e61f721
Revises: d4973fd72304
Create Date: 2026-07-11
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e18b9e61f721"
down_revision: Union[str, Sequence[str], None] = "d4973fd72304"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifacts", sa.Column("status", sa.String(32), server_default="pending", nullable=False)
    )
    op.add_column("artifacts", sa.Column("object_key", sa.String(512), nullable=True))
    op.execute(
        "UPDATE artifacts SET object_key = 'organizations/' || organization_id || "
        "'/sha256/' || substring(digest_sha256,1,2) || '/' || digest_sha256"
    )
    op.alter_column("artifacts", "object_key", nullable=False)
    op.add_column("artifacts", sa.Column("upload_id", sa.String(36)))
    op.add_column("artifacts", sa.Column("upload_token_hash", sa.Text()))
    op.add_column("artifacts", sa.Column("upload_expires_at", sa.DateTime(timezone=True)))
    op.add_column("artifacts", sa.Column("available_at", sa.DateTime(timezone=True)))
    op.add_column("artifacts", sa.Column("rejection_reason", sa.String(255)))
    op.create_unique_constraint("uq_artifacts_upload_id", "artifacts", ["upload_id"])
    op.create_index(
        "ix_artifacts_digest_status", "artifacts", ["organization_id", "digest_sha256", "status"]
    )

    op.add_column("agent_installations", sa.Column("credential_lookup_digest", sa.String(64)))
    op.add_column("agent_installations", sa.Column("credential_hash", sa.Text()))
    op.add_column(
        "agent_installations",
        sa.Column("credential_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "agent_installations", sa.Column("credential_rotated_at", sa.DateTime(timezone=True))
    )
    op.create_unique_constraint(
        "uq_agent_installations_credential_lookup",
        "agent_installations",
        ["credential_lookup_digest"],
    )

    op.add_column("ingest_batches", sa.Column("installation_id", sa.String(128)))
    op.create_foreign_key(
        "fk_ingest_batches_installation_tenant",
        "ingest_batches",
        "agent_installations",
        ["organization_id", "installation_id"],
        ["organization_id", "id"],
        ondelete="RESTRICT",
    )

    op.add_column("outbox", sa.Column("lease_owner", sa.String(128)))
    op.add_column("outbox", sa.Column("leased_until", sa.DateTime(timezone=True)))
    op.add_column("outbox", sa.Column("last_error", sa.String(255)))

    op.create_table(
        "enrollment_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(128), nullable=False),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("lookup_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_enrollment_tokens_workspace_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_enrollment_tokens_active",
        "enrollment_tokens",
        ["lookup_digest", "expires_at", "used_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_enrollment_tokens_active", table_name="enrollment_tokens")
    op.drop_table("enrollment_tokens")
    for column in ("last_error", "leased_until", "lease_owner"):
        op.drop_column("outbox", column)
    op.drop_constraint(
        "fk_ingest_batches_installation_tenant", "ingest_batches", type_="foreignkey"
    )
    op.drop_column("ingest_batches", "installation_id")
    op.drop_constraint(
        "uq_agent_installations_credential_lookup", "agent_installations", type_="unique"
    )
    for column in (
        "credential_rotated_at",
        "credential_version",
        "credential_hash",
        "credential_lookup_digest",
    ):
        op.drop_column("agent_installations", column)
    op.drop_index("ix_artifacts_digest_status", table_name="artifacts")
    op.drop_constraint("uq_artifacts_upload_id", "artifacts", type_="unique")
    for column in (
        "rejection_reason",
        "available_at",
        "upload_expires_at",
        "upload_token_hash",
        "upload_id",
        "object_key",
        "status",
    ):
        op.drop_column("artifacts", column)
