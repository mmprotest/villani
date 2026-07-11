"""recommendation-only shadow routing and append-only outcome ledger

Revision ID: a4b5c6d7e8f9
Revises: f3a1c2d4e5f6
Create Date: 2026-07-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, Sequence[str], None] = "f3a1c2d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_outcomes_attempt", "outcomes", type_="unique")
    op.add_column("outcomes", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("outcomes", sa.Column("supersedes_outcome_id", sa.String(36)))
    op.add_column("outcomes", sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("outcomes", sa.Column("confidence", sa.Float(), nullable=False, server_default="1"))
    op.add_column("outcomes", sa.Column("capability_success_label", sa.Boolean()))
    op.create_unique_constraint("uq_outcomes_attempt_version", "outcomes", ["organization_id", "run_id", "attempt_key", "version"])
    op.create_foreign_key("fk_outcomes_supersedes", "outcomes", "outcomes", ["organization_id", "supersedes_outcome_id"], ["organization_id", "id"])
    op.create_table(
        "outcome_signals",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("attempt_id", sa.String(128)),
        sa.Column("signal_type", sa.String(32), nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("source_provider", sa.String(64), nullable=False),
        sa.Column("source_event_id", sa.String(255), nullable=False),
        sa.Column("correction_of_signal_id", sa.String(36)),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "run_id"], ["runs.organization_id", "runs.id"], name="fk_outcome_signals_run_tenant", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id", "correction_of_signal_id"], ["outcome_signals.organization_id", "outcome_signals.id"], name="fk_outcome_signals_correction"),
        sa.UniqueConstraint("organization_id", "source_provider", "source_event_id", "signal_type", name="uq_outcome_signal_source"),
    )
    op.create_index("ix_outcome_signals_run", "outcome_signals", ["organization_id", "run_id", "observed_at"])
    op.create_table(
        "shadow_routing_observations",
        sa.Column("organization_id", sa.String(128), primary_key=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("recommendation_id", sa.String(128), nullable=False),
        sa.Column("shadow_strategy", sa.String(255)),
        sa.Column("actual_strategy", sa.String(255)),
        sa.Column("shadow_policy_version", sa.String(128), nullable=False),
        sa.Column("actual_policy_version", sa.String(128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id", "run_id"], ["runs.organization_id", "runs.id"], name="fk_shadow_observations_run_tenant", ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "run_id", "recommendation_id", name="uq_shadow_observation_recommendation"),
    )
    op.create_index("ix_shadow_observations_metrics", "shadow_routing_observations", ["organization_id", "workspace_id", "recorded_at"])


def downgrade() -> None:
    op.drop_index("ix_shadow_observations_metrics", table_name="shadow_routing_observations")
    op.drop_table("shadow_routing_observations")
    op.drop_index("ix_outcome_signals_run", table_name="outcome_signals")
    op.drop_table("outcome_signals")
    op.drop_constraint("fk_outcomes_supersedes", "outcomes", type_="foreignkey")
    op.drop_constraint("uq_outcomes_attempt_version", "outcomes", type_="unique")
    for name in ("capability_success_label", "confidence", "provenance", "supersedes_outcome_id", "version"):
        op.drop_column("outcomes", name)
    op.create_unique_constraint("uq_outcomes_attempt", "outcomes", ["organization_id", "run_id", "attempt_key"])
