"""
DRPL Backend - Workflow Models
Visual agentic workflow builder: workflows, nodes, edges, executions, and node execution logs.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, JSON, Index
from app.core.database import Base


class Workflow(Base):
    """Top-level workflow definition — a configurable multi-agent graph."""
    __tablename__ = "workflows"

    id = Column(Integer, primary_key=True, index=True)
    workflow_key = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    category = Column(String(100), nullable=True)                              # tender_processing, general, custom
    tags = Column(JSON, default=list)

    # Versioning & status
    current_version = Column(Integer, default=1)
    status = Column(String(30), default="draft")                               # draft, published, archived

    # Binding
    is_default_router = Column(Boolean, default=False)                         # Replaces agent_router_graph for Command Center
    trigger_type = Column(String(50), default="manual")                        # manual, command_center, api, scheduled

    # Input/Output contracts
    input_schema = Column(JSON, nullable=True)                                 # Expected input shape
    output_schema = Column(JSON, nullable=True)                                # Expected output shape
    variables_schema = Column(JSON, nullable=True)                             # Workflow-level variable defs with types/defaults

    # Canvas layout metadata (viewport position, zoom for React Flow)
    canvas_state = Column(JSON, nullable=True)

    # Audit
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class WorkflowVersion(Base):
    """Immutable snapshot of a workflow graph for rollback and audit."""
    __tablename__ = "workflow_versions"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(Integer, nullable=False, index=True)                  # FK to workflows.id
    version_number = Column(Integer, nullable=False)

    # Full serialized graph at this version
    nodes_snapshot = Column(JSON, nullable=False)
    edges_snapshot = Column(JSON, nullable=False)
    variables_snapshot = Column(JSON, nullable=True)

    change_description = Column(Text, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_workflow_versions_wf_ver", "workflow_id", "version_number", unique=True),
    )


class WorkflowNode(Base):
    """Individual node on the workflow canvas."""
    __tablename__ = "workflow_nodes"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(Integer, nullable=False, index=True)                  # FK to workflows.id
    node_key = Column(String(100), nullable=False)                             # Unique within workflow

    # Node type: start, end, agent, classify, if_else, while_loop,
    #            user_approval, transform, set_state, tool, note
    node_type = Column(String(50), nullable=False)
    display_name = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)

    # Canvas position
    position_x = Column(Float, default=0.0)
    position_y = Column(Float, default=0.0)

    # Type-specific configuration (JSON schema varies by node_type)
    config = Column(JSON, nullable=False, default=dict)

    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_workflow_nodes_wf_key", "workflow_id", "node_key", unique=True),
    )


class WorkflowEdge(Base):
    """Connection between two nodes in a workflow."""
    __tablename__ = "workflow_edges"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(Integer, nullable=False, index=True)                  # FK to workflows.id
    source_node_key = Column(String(100), nullable=False)
    target_node_key = Column(String(100), nullable=False)
    source_handle = Column(String(50), nullable=True)                          # For multi-output: "true"/"false", branch labels
    label = Column(String(255), nullable=True)
    condition = Column(Text, nullable=True)                                    # Optional runtime expression
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_workflow_edges_wf_src_tgt", "workflow_id", "source_node_key", "target_node_key", "source_handle", unique=True),
    )


class WorkflowExecution(Base):
    """Runtime execution instance of a workflow."""
    __tablename__ = "workflow_executions"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(Integer, nullable=False, index=True)                  # FK to workflows.id
    workflow_version = Column(Integer, nullable=True)
    session_id = Column(String(100), nullable=True, index=True)                # Links to Command Center session
    trigger = Column(String(100), default="manual")                            # manual, command_center, api

    # Execution state
    status = Column(String(50), default="running")                             # running, paused, completed, failed, cancelled
    current_node_key = Column(String(100), nullable=True)
    state_snapshot = Column(JSON, nullable=True)                               # Current workflow variable state
    execution_path = Column(JSON, default=list)                                # Ordered list of node_keys executed

    # Metrics
    total_latency_ms = Column(Integer, nullable=True)
    total_tokens_input = Column(Integer, default=0)
    total_tokens_output = Column(Integer, default=0)
    total_cost = Column(Float, default=0.0)

    # Error info
    error_message = Column(Text, nullable=True)
    error_node_key = Column(String(100), nullable=True)

    # User approval state
    pending_approval = Column(Boolean, default=False)
    approval_prompt = Column(Text, nullable=True)
    approval_response = Column(Text, nullable=True)

    # Audit
    user_id = Column(Integer, nullable=True)
    input_data = Column(JSON, nullable=True)
    output_data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)


class WorkflowNodeExecution(Base):
    """Per-node execution log within a workflow execution."""
    __tablename__ = "workflow_node_executions"

    id = Column(Integer, primary_key=True, index=True)
    workflow_execution_id = Column(Integer, nullable=False, index=True)        # FK to workflow_executions.id
    node_key = Column(String(100), nullable=False)
    node_type = Column(String(50), nullable=False)
    status = Column(String(50), default="running")                             # running, completed, failed, skipped

    input_data = Column(JSON, nullable=True)
    output_data = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    latency_ms = Column(Integer, nullable=True)
    tokens_input = Column(Integer, default=0)
    tokens_output = Column(Integer, default=0)
    cost_estimate = Column(Float, default=0.0)
    agent_execution_id = Column(Integer, nullable=True)                        # FK to agent_executions for agent nodes

    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_wf_node_exec_exec_node", "workflow_execution_id", "node_key"),
    )
