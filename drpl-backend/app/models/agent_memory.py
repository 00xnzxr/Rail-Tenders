"""
DRPL Backend - Agent Memory Models
Long-term memory store and conversation history for LangChain agents.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, JSON, Index
from app.core.database import Base


class AgentMemory(Base):
    """Persistent long-term memory for agents across sessions."""
    __tablename__ = "agent_memories"

    id = Column(Integer, primary_key=True, index=True)
    agent_key = Column(String(100), nullable=True, index=True)       # NULL = global memory accessible to all agents
    memory_type = Column(String(50), nullable=False)                 # fact, preference, learning, decision
    content = Column(Text, nullable=False)                           # The actual memory content
    context = Column(Text, nullable=True)                            # What situation led to this memory
    keywords = Column(JSON, default=list)                            # For keyword-based retrieval
    importance = Column(Float, default=0.5)                          # 0-1 importance score
    access_count = Column(Integer, default=0)                        # How often retrieved
    last_accessed_at = Column(DateTime(timezone=True), nullable=True)
    tender_id = Column(Integer, nullable=True, index=True)           # Optional tender association
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime(timezone=True), nullable=True)      # Optional TTL

    __table_args__ = (
        Index("ix_agent_memories_type_key", "memory_type", "agent_key"),
    )


class AgentConversationHistory(Base):
    """Per-session conversation turns with tool call details for LangChain agents."""
    __tablename__ = "agent_conversation_history"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), nullable=False, index=True)
    agent_key = Column(String(100), nullable=False, index=True)
    role = Column(String(20), nullable=False)                        # user, assistant, tool
    content = Column(Text, nullable=False)
    tool_calls = Column(JSON, nullable=True)                         # Tool call details [{name, input, output}]
    metadata_json = Column(JSON, nullable=True)                      # Extra metadata (tokens, latency, etc.)
    output_type = Column(String(50), nullable=True)                  # Rendering hint: document_analysis, checklist, proposal_document, cost_breakdown, general
    routed_from = Column(String(100), nullable=True)                 # Which agent produced this (for router sessions)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_agent_conv_session_created", "session_id", "created_at"),
    )
