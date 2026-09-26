"""
DRPL Backend - Proposal Models
Tracks proposal creation sessions, messages, documents, and reviews
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, JSON
from app.core.database import Base


class ProposalSession(Base):
    __tablename__ = "proposal_sessions"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=True, index=True)            # FK to tenders.id (nullable for standalone)
    created_by = Column(Integer, nullable=False)                      # FK to users.id
    status = Column(String(50), default="draft")                      # draft, submitted, under_review, approved, rejected, revision_requested
    title = Column(String(500), nullable=True)                        # For standalone sessions
    agent_type = Column(String(50), default="tender_proposal")        # tender_proposal or standalone
    context_data = Column(JSON, nullable=True)                        # Freeform context for standalone
    template_id = Column(String(100), nullable=True)
    current_version = Column(Integer, default=1)
    # Command Center extensions
    router_session_id = Column(String(100), nullable=True, index=True)  # Links to AgentConversationHistory.session_id
    pipeline_state = Column(JSON, nullable=True)                        # Tracks completed pipeline steps
    mode = Column(String(20), nullable=True, default="tender_linked")   # tender_linked or standalone
    # Workflow builder integration
    active_workflow_id = Column(Integer, nullable=True)                     # FK to workflows.id
    workflow_execution_id = Column(Integer, nullable=True)                  # FK to workflow_executions.id
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ProposalMessage(Base):
    __tablename__ = "proposal_messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)          # FK to proposal_sessions.id
    role = Column(String(20), nullable=False)                          # user, assistant, system
    content = Column(Text, nullable=False)
    message_type = Column(String(50), default="text")                  # text, proposal_section, template_fill, document_reference
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ProposalDocument(Base):
    __tablename__ = "proposal_documents"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)          # FK to proposal_sessions.id
    version = Column(Integer, default=1)
    file_path = Column(Text, nullable=False)
    file_name = Column(String(500), nullable=False)
    file_size = Column(Integer, nullable=True)
    generated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ProposalReview(Base):
    __tablename__ = "proposal_reviews"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)          # FK to proposal_sessions.id
    reviewer_id = Column(Integer, nullable=False)                      # FK to users.id
    status = Column(String(50), default="pending")                     # pending, approved, rejected, changes_requested
    comments = Column(Text, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
