"""scope canonical attempt identity by run

Revision ID: 0a1b2c3d4e5f
Revises: f9a0b1c2d3e4
Create Date: 2026-07-13
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0a1b2c3d4e5f"
down_revision: Union[str, Sequence[str], None] = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("canonical_projection", sa.JSON(), nullable=False, server_default="{}"),
    )
    if op.get_context().dialect.name == "postgresql":
        op.drop_index("ix_attempts_run", table_name="attempts")
        op.drop_constraint("attempts_pkey", "attempts", type_="primary")
        op.create_primary_key(
            "pk_attempts", "attempts", ["organization_id", "run_id", "id"]
        )
        op.create_index(
            "ix_attempts_run", "attempts", ["organization_id", "run_id", "id"]
        )
    else:
        with op.batch_alter_table("attempts", recreate="always") as batch:
            batch.drop_index("ix_attempts_run")
            batch.drop_constraint(None, type_="primary")
            batch.create_primary_key(
                "pk_attempts", ["organization_id", "run_id", "id"]
            )
            batch.create_index(
                "ix_attempts_run", ["organization_id", "run_id", "id"], unique=False
            )


def downgrade() -> None:
    # Refuse an unsafe downgrade if multiple runs reuse a canonical attempt ID.
    connection = op.get_bind()
    collision = connection.exec_driver_sql(
        """SELECT organization_id, id FROM attempts
           GROUP BY organization_id, id HAVING COUNT(*) > 1 LIMIT 1"""
    ).first()
    if collision is not None:
        raise RuntimeError(
            "cannot downgrade: canonical attempt IDs are reused across runs"
        )
    if op.get_context().dialect.name == "postgresql":
        op.drop_index("ix_attempts_run", table_name="attempts")
        op.drop_constraint("pk_attempts", "attempts", type_="primary")
        op.create_primary_key("attempts_pkey", "attempts", ["organization_id", "id"])
        op.create_index("ix_attempts_run", "attempts", ["organization_id", "run_id"])
    else:
        with op.batch_alter_table("attempts", recreate="always") as batch:
            batch.drop_index("ix_attempts_run")
            batch.drop_constraint("pk_attempts", type_="primary")
            batch.create_primary_key("attempts_pkey", ["organization_id", "id"])
            batch.create_index(
                "ix_attempts_run", ["organization_id", "run_id"], unique=False
            )
    op.drop_column("runs", "canonical_projection")
