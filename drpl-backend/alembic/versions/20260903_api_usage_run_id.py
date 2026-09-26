"""api_usage_logs.run_id — attribute LLM spend to the run that caused it

The ledger already carried one row per LLM call with its tokens and priced
cost, attributed to a user. It recorded no run, so the platform could say what
a user spent this month but not what any single run spent — and a run is the
unit a user recognises.

Nullable on purpose: spend outside any run (seeders, the archive sweep,
scheduled jobs) records NULL rather than being forced into a phantom run.

Revision ID: 20260903_usage_run_id
Revises: 20260901_latency_bigint
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_usage_run_id"
down_revision = "20260901_latency_bigint"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "api_usage_logs",
        sa.Column("run_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_api_usage_logs_run_id", "api_usage_logs", ["run_id"]
    )


def downgrade():
    op.drop_index("ix_api_usage_logs_run_id", table_name="api_usage_logs")
    op.drop_column("api_usage_logs", "run_id")
