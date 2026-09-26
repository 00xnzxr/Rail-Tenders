"""Add pending_clarifications table for agent-initiated questions (Phase B)

Revision ID: pending_clarifications_001
Revises: tender_analyzer_v2
Create Date: 2026-04-19
"""

from alembic import op
import sqlalchemy as sa

revision = "pending_clarifications_001"
down_revision = "tender_analyzer_v2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pending_clarifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("router_session_id", sa.String(length=100), nullable=True),
        sa.Column("agent_key", sa.String(length=100), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_pending_clarifications_session_id",
        "pending_clarifications",
        ["session_id"],
    )
    op.create_index(
        "ix_pending_clarifications_router_session_id",
        "pending_clarifications",
        ["router_session_id"],
    )
    op.create_index(
        "ix_pending_clarifications_status",
        "pending_clarifications",
        ["status"],
    )


def downgrade():
    op.drop_index("ix_pending_clarifications_status", table_name="pending_clarifications")
    op.drop_index("ix_pending_clarifications_router_session_id", table_name="pending_clarifications")
    op.drop_index("ix_pending_clarifications_session_id", table_name="pending_clarifications")
    op.drop_table("pending_clarifications")
