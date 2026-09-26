"""widen agent_executions.latency_ms to BIGINT

The startup reaper (`reap_stuck_executions`) back-fills latency_ms with
(now - created_at) for rows orphaned at status="running". A row stuck for
months yields ~1.3e10 ms, past INTEGER's 2^31-1 ceiling, so the batch UPDATE
fails with NumericValueOutOfRange and every stuck row stays "running" — on
that boot and every boot after.

Revision ID: 20260901_latency_bigint
Revises: 20260731_tender_archive
Create Date: 2026-09-01
"""
from alembic import op
import sqlalchemy as sa

revision = "20260901_latency_bigint"
down_revision = "20260731_tender_archive"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "agent_executions",
        "latency_ms",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade():
    # Values above 2^31-1 cannot survive the narrowing; clamp them first so the
    # downgrade doesn't fail on exactly the rows that motivated the widening.
    op.execute(
        "UPDATE agent_executions SET latency_ms = 2147483647 "
        "WHERE latency_ms > 2147483647"
    )
    op.alter_column(
        "agent_executions",
        "latency_ms",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
