"""
DRPL Backend - Agent Builder Models
Custom agents, versions, tools, test cases, test runs, and execution logs
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, BigInteger, String, Float, DateTime, Text, Boolean, JSON, Index
from app.core.database import Base


class CustomAgent(Base):
    __tablename__ = "custom_agents"

    id = Column(Integer, primary_key=True, index=True)
    agent_key = Column(String(100), unique=True, nullable=False, index=True)     # Slug identifier
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    agent_type = Column(String(50), default="chain_of_thought")                  # react, chain_of_thought, tool_use, orchestrator

    # AI Configuration
    system_prompt = Column(Text, nullable=True)
    model = Column(String(255), nullable=True)
    provider = Column(String(50), default="anthropic")
    temperature = Column(Float, default=0.7)
    max_tokens = Column(Integer, default=4096)

    # Extended Thinking & Effort
    thinking_mode = Column(String(20), nullable=True)          # "auto", "adaptive", "enabled", "disabled"
    thinking_budget_tokens = Column(Integer, nullable=True)    # Token budget for manual extended thinking
    effort = Column(String(20), nullable=True)                 # "low", "medium", "high", "max"

    # Tool integration
    tools = Column(JSON, default=list)                                           # [{"tool_id": N, "config": {...}}]
    mcp_servers = Column(JSON, default=list)                                     # ["server-name-1", "server-name-2"]

    # Schema definitions
    input_schema = Column(JSON, nullable=True)                                   # Expected input format
    output_schema = Column(JSON, nullable=True)                                  # Expected output format

    # Multi-agent orchestration
    orchestration_config = Column(JSON, nullable=True)                           # {"steps": [{"agent_key": "...", "input_mapping": {...}}]}

    # LangChain-specific configuration (for react/tool_use agent types)
    langchain_config = Column(JSON, nullable=True)                               # {"max_iterations": 10, "return_intermediate_steps": true, ...}

    # Learning
    learning_enabled = Column(Boolean, default=True)                             # Auto-extract learnings after interactions

    # Phase 3d — User-customization flag.
    # When True, this agent's `system_prompt` + `tools` differ from the code
    # canonical (defined in app.services.langchain.canonical_registry). The
    # runtime resolver uses the DB version instead of the code constant; the
    # startup seed scripts skip the prompt+tools resync so user edits survive
    # restarts. Auto-set by agent_builder_service.update_agent on every save
    # that changes prompt/tools; reset to False by the
    # POST /reset-to-default endpoint and on first startup if SHA(prompt)
    # matches the code canonical.
    is_user_customized = Column(Boolean, nullable=False, default=False, server_default="false")

    # Metadata
    is_system = Column(Boolean, default=False)                                   # True for built-in agents
    is_enabled = Column(Boolean, default=True)
    is_published = Column(Boolean, default=False)                                # Visible in library
    category = Column(String(100), nullable=True)                                # tender_analysis, proposal, document, custom
    tags = Column(JSON, default=list)
    document_categories = Column(JSON, default=list)                             # ["declaration", "letter", "boq"] — doc types this agent specializes in
    current_version = Column(Integer, default=1)

    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class AgentVersion(Base):
    __tablename__ = "agent_versions"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, nullable=False, index=True)                       # FK to custom_agents.id
    version_number = Column(Integer, nullable=False)

    # Snapshot of agent config at this version
    system_prompt = Column(Text, nullable=True)
    tools = Column(JSON, default=list)
    temperature = Column(Float, nullable=True)
    max_tokens = Column(Integer, nullable=True)
    model = Column(String(255), nullable=True)
    orchestration_config = Column(JSON, nullable=True)

    change_description = Column(Text, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_agent_versions_agent_version", "agent_id", "version_number", unique=True),
    )


class AgentTool(Base):
    __tablename__ = "agent_tools"

    id = Column(Integer, primary_key=True, index=True)
    tool_key = Column(String(100), unique=True, nullable=False)
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    tool_type = Column(String(50), nullable=False)                               # api_call, db_query, document_op, web_search, calculation, code_exec

    # Configuration
    config_schema = Column(JSON, default=dict)                                   # Schema for tool configuration
    default_config = Column(JSON, nullable=True)
    handler_module = Column(String(255), nullable=True)                          # Python module path for built-in tools

    is_system = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class AgentTestCase(Base):
    __tablename__ = "agent_test_cases"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, nullable=False, index=True)                       # FK to custom_agents.id
    test_name = Column(String(255), nullable=False)
    input_data = Column(JSON, nullable=False)                                    # Test input
    expected_output = Column(Text, nullable=True)                                # What correct output should contain
    evaluation_criteria = Column(JSON, nullable=True)                            # {"must_contain": [], "must_not_contain": [], "score_threshold": 0.8}

    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AgentTestRun(Base):
    __tablename__ = "agent_test_runs"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, nullable=False, index=True)                       # FK to custom_agents.id
    agent_version = Column(Integer, nullable=True)
    test_case_id = Column(Integer, nullable=True)                                # FK to agent_test_cases.id

    input_data = Column(JSON, nullable=True)
    output_text = Column(Text, nullable=True)
    tokens_input = Column(Integer, default=0)
    tokens_output = Column(Integer, default=0)
    latency_ms = Column(Integer, nullable=True)
    cost_estimate = Column(Float, default=0.0)

    score = Column(Float, nullable=True)                                         # Automated evaluation score
    passed = Column(Boolean, nullable=True)
    error_message = Column(Text, nullable=True)

    run_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AgentTestDocument(Base):
    """Uploaded documents for agent testing - stored on disk with DB metadata."""
    __tablename__ = "agent_test_documents"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, nullable=False, index=True)                       # FK to custom_agents.id
    file_name = Column(String(500), nullable=False)
    file_path = Column(Text, nullable=False)                                     # Absolute path on disk
    file_size = Column(Integer, default=0)
    mime_type = Column(String(100), nullable=True)
    extracted_text = Column(Text, nullable=True)                                 # Parsed text content
    page_count = Column(Integer, nullable=True)
    extraction_method = Column(String(50), nullable=True)                        # pdfplumber, docx, ocr, text
    extraction_status = Column(String(50), default="pending")                    # pending, success, failed, partial
    extraction_error = Column(Text, nullable=True)

    uploaded_by = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AgentExecution(Base):
    __tablename__ = "agent_executions"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, nullable=False, index=True)                       # FK to custom_agents.id
    agent_version = Column(Integer, nullable=True)
    trigger = Column(String(100), default="manual")                              # manual, api, orchestration, scheduled

    input_summary = Column(Text, nullable=True)
    output_summary = Column(Text, nullable=True)
    status = Column(String(50), default="running")                               # running, completed, failed, timeout

    tokens_input = Column(Integer, default=0)
    tokens_output = Column(Integer, default=0)
    # BigInteger: the startup reaper back-fills this with (now - created_at) for
    # rows orphaned at status="running". A row stuck for months exceeds INTEGER's
    # 2^31-1 ceiling and fails the whole batch UPDATE on Postgres.
    latency_ms = Column(BigInteger, nullable=True)
    cost_estimate = Column(Float, default=0.0)
    error_message = Column(Text, nullable=True)

    parent_execution_id = Column(Integer, nullable=True)                         # For multi-agent tracing
    metadata_json = Column(JSON, nullable=True)
    user_id = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
