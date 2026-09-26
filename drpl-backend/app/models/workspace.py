"""
DRPL Backend - Workspace Models
Per-tender canvas workspace for individual document management, editing, and agent integration
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, JSON, Index
from app.core.database import Base


class WorkspaceConfig(Base):
    """Per-tender workspace configuration and metadata."""
    __tablename__ = "workspace_configs"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, unique=True, index=True)   # FK to tenders.id

    # Layout
    view_mode = Column(String(20), default="grid")                          # grid, kanban, list
    layout_json = Column(JSON, nullable=True)                               # Card positions for freeform canvas

    # Defaults
    default_letterhead_id = Column(Integer, nullable=True)                  # FK to letterhead_templates.id
    default_agent_key = Column(String(100), nullable=True)                  # Fallback agent for all documents

    # Denormalized counts for fast overview
    total_items = Column(Integer, default=0)
    completed_items = Column(Integer, default=0)

    # Activity
    last_activity_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class DocumentWorkspace(Base):
    """Per-document editing state, agent assignment, and draft content within a tender workspace."""
    __tablename__ = "document_workspaces"

    id = Column(Integer, primary_key=True, index=True)
    checklist_item_id = Column(Integer, nullable=False, unique=True, index=True)  # FK to checklist_items.id
    tender_id = Column(Integer, nullable=False, index=True)                        # FK to tenders.id (denormalized)

    # Agent assignment
    agent_key = Column(String(100), nullable=True)                                 # FK concept to custom_agents.agent_key
    agent_config_override = Column(JSON, nullable=True)                            # Per-doc system prompt tweaks, temp overrides

    # Format template
    format_template_id = Column(Integer, nullable=True)                            # FK to document_format_templates.id
    format_instructions = Column(Text, nullable=True)                              # Free-text format instructions for agent

    # Draft content
    draft_content_html = Column(Text, nullable=True)
    draft_content_markdown = Column(Text, nullable=True)
    content_version = Column(Integer, default=0)

    # Per-document agent conversation
    conversation_session_id = Column(String(100), nullable=True, index=True)       # Links to AgentConversationHistory

    # User notes
    notes = Column(Text, nullable=True)

    # Review workflow
    review_status = Column(String(30), default="not_started")                      # not_started, drafting, in_review, approved, rejected
    reviewed_by = Column(Integer, nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    # PDF generation config
    letterhead_template_id = Column(Integer, nullable=True)                        # FK to letterhead_templates.id
    # Explicit "no letterhead on this document" opt-out. Distinct from
    # letterhead_template_id being NULL, which now means "inherit the tender
    # default" (WorkspaceConfig.default_letterhead_id). Needed for annexures that
    # belong on a third party's letterhead — e.g. a bank guarantee bond, which
    # must go on the issuing bank's letterhead, not the bidder's.
    letterhead_disabled = Column(Boolean, default=False, nullable=False)
    signatures_json = Column(JSON, default=list)                                   # [{signature_id, position, page}]
    page_orientation = Column(String(16), default="portrait", nullable=False)      # "portrait" | "landscape" — drives editor width + PDF page orientation

    # Cross-document references
    depends_on = Column(JSON, default=list)                                        # Array of checklist_item_id values
    referenced_by = Column(JSON, default=list)                                     # Inverse references

    # Tracking
    last_edited_at = Column(DateTime(timezone=True), nullable=True)
    last_edited_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class DocumentFormatTemplate(Base):
    """Reusable format template definitions for document types (annexures, declarations, BOQs, etc.)."""
    __tablename__ = "document_format_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    document_category = Column(String(50), nullable=False)                         # annexure, declaration, certificate, boq, letter, proposal, custom

    # Template structure
    structure_json = Column(JSON, nullable=True)                                   # Section headings and layout definition
    content_template_markdown = Column(Text, nullable=True)                        # Markdown skeleton
    content_template_html = Column(Text, nullable=True)                            # HTML skeleton

    # Rules & constraints
    format_rules = Column(JSON, default=list)                                      # Array of constraint strings
    required_sections = Column(JSON, default=list)                                 # ["header", "body", "signature_block"]

    # Auto-assignment patterns
    match_patterns = Column(JSON, default=list)                                    # Glob-like patterns, e.g. ["annexure*", "schedule of rates*"]

    # Output format
    output_format = Column(String(10), default="docx")                            # docx, xlsx (xlsx for boq category)
    original_file_path = Column(Text, nullable=True)                              # For uploaded templates
    original_file_name = Column(String(500), nullable=True)

    # Metadata
    is_system = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
