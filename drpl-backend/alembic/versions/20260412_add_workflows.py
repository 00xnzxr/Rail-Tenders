"""Add workflow builder tables and proposal_sessions workflow columns

Revision ID: workflows_001
Revises: gem_file_id_001
Create Date: 2026-04-12
"""

from alembic import op
import sqlalchemy as sa

revision = "workflows_001"
down_revision = "gem_file_id_001"
branch_labels = None
depends_on = None


def upgrade():
    # --- workflows ---
    op.create_table(
        "workflows",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_key", sa.String(100), unique=True, nullable=False, index=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("tags", sa.JSON(), server_default="[]"),
        sa.Column("current_version", sa.Integer(), server_default="1"),
        sa.Column("status", sa.String(30), server_default="draft"),
        sa.Column("is_default_router", sa.Boolean(), server_default="false"),
        sa.Column("trigger_type", sa.String(50), server_default="manual"),
        sa.Column("input_schema", sa.JSON(), nullable=True),
        sa.Column("output_schema", sa.JSON(), nullable=True),
        sa.Column("variables_schema", sa.JSON(), nullable=True),
        sa.Column("canvas_state", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # --- workflow_versions ---
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_id", sa.Integer(), nullable=False, index=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("nodes_snapshot", sa.JSON(), nullable=False),
        sa.Column("edges_snapshot", sa.JSON(), nullable=False),
        sa.Column("variables_snapshot", sa.JSON(), nullable=True),
        sa.Column("change_description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_workflow_versions_wf_ver",
        "workflow_versions",
        ["workflow_id", "version_number"],
        unique=True,
    )

    # --- workflow_nodes ---
    op.create_table(
        "workflow_nodes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_id", sa.Integer(), nullable=False, index=True),
        sa.Column("node_key", sa.String(100), nullable=False),
        sa.Column("node_type", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("position_x", sa.Float(), server_default="0"),
        sa.Column("position_y", sa.Float(), server_default="0"),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_workflow_nodes_wf_key",
        "workflow_nodes",
        ["workflow_id", "node_key"],
        unique=True,
    )

    # --- workflow_edges ---
    op.create_table(
        "workflow_edges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_id", sa.Integer(), nullable=False, index=True),
        sa.Column("source_node_key", sa.String(100), nullable=False),
        sa.Column("target_node_key", sa.String(100), nullable=False),
        sa.Column("source_handle", sa.String(50), nullable=True),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_workflow_edges_wf_src_tgt",
        "workflow_edges",
        ["workflow_id", "source_node_key", "target_node_key", "source_handle"],
        unique=True,
    )

    # --- workflow_executions ---
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_id", sa.Integer(), nullable=False, index=True),
        sa.Column("workflow_version", sa.Integer(), nullable=True),
        sa.Column("session_id", sa.String(100), nullable=True, index=True),
        sa.Column("trigger", sa.String(100), server_default="manual"),
        sa.Column("status", sa.String(50), server_default="running"),
        sa.Column("current_node_key", sa.String(100), nullable=True),
        sa.Column("state_snapshot", sa.JSON(), nullable=True),
        sa.Column("execution_path", sa.JSON(), server_default="[]"),
        sa.Column("total_latency_ms", sa.Integer(), nullable=True),
        sa.Column("total_tokens_input", sa.Integer(), server_default="0"),
        sa.Column("total_tokens_output", sa.Integer(), server_default="0"),
        sa.Column("total_cost", sa.Float(), server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_node_key", sa.String(100), nullable=True),
        sa.Column("pending_approval", sa.Boolean(), server_default="false"),
        sa.Column("approval_prompt", sa.Text(), nullable=True),
        sa.Column("approval_response", sa.Text(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("input_data", sa.JSON(), nullable=True),
        sa.Column("output_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- workflow_node_executions ---
    op.create_table(
        "workflow_node_executions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workflow_execution_id", sa.Integer(), nullable=False, index=True),
        sa.Column("node_key", sa.String(100), nullable=False),
        sa.Column("node_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(50), server_default="running"),
        sa.Column("input_data", sa.JSON(), nullable=True),
        sa.Column("output_data", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_input", sa.Integer(), server_default="0"),
        sa.Column("tokens_output", sa.Integer(), server_default="0"),
        sa.Column("cost_estimate", sa.Float(), server_default="0"),
        sa.Column("agent_execution_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_wf_node_exec_exec_node",
        "workflow_node_executions",
        ["workflow_execution_id", "node_key"],
    )

    # --- Add workflow columns to proposal_sessions ---
    op.add_column(
        "proposal_sessions",
        sa.Column("active_workflow_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "proposal_sessions",
        sa.Column("workflow_execution_id", sa.Integer(), nullable=True),
    )


def downgrade():
    op.drop_column("proposal_sessions", "workflow_execution_id")
    op.drop_column("proposal_sessions", "active_workflow_id")
    op.drop_table("workflow_node_executions")
    op.drop_table("workflow_executions")
    op.drop_table("workflow_edges")
    op.drop_table("workflow_nodes")
    op.drop_table("workflow_versions")
    op.drop_table("workflows")
