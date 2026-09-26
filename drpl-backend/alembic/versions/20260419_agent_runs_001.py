"""Add agent_runs table for Phase C run-id tracking

Revision ID: agent_runs_001
Revises: pdf_vision_cache_001
Create Date: 2026-04-19
"""

from alembic import op
import sqlalchemy as sa


revision = "agent_runs_001"
down_revision = "pdf_vision_cache_001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("proposal_session_id", sa.Integer(), nullable=True),
        sa.Column("router_session_id", sa.String(length=64), nullable=True),
        sa.Column("tender_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("display_message", sa.Text(), nullable=True),
        sa.Column("selected_agents", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("rq_job_id", sa.String(length=64), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_proposal_session_id", "agent_runs", ["proposal_session_id"])
    op.create_index("ix_agent_runs_router_session_id", "agent_runs", ["router_session_id"])
    op.create_index("ix_agent_runs_tender_id", "agent_runs", ["tender_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])
    op.create_index("ix_agent_runs_rq_job_id", "agent_runs", ["rq_job_id"])
    op.create_index("ix_agent_runs_created_at", "agent_runs", ["created_at"])
    op.create_index("ix_agent_runs_user_created", "agent_runs", ["user_id", "created_at"])
    op.create_index("ix_agent_runs_user_status", "agent_runs", ["user_id", "status"])


def downgrade():
    op.drop_index("ix_agent_runs_user_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_created", table_name="agent_runs")
    op.drop_index("ix_agent_runs_created_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_rq_job_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_tender_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_router_session_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_proposal_session_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_table("agent_runs")
