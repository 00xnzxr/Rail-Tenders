"""
DRPL Backend - Letterhead, Digital Signature, and Generated Document Models
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, JSON
from app.core.database import Base


class LetterheadTemplate(Base):
    __tablename__ = "letterhead_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_default = Column(Boolean, default=False)

    # Full PDF letterhead (primary — used as base template when available)
    letterhead_pdf_path = Column(Text, nullable=True)                # Uploaded PDF letterhead

    # Individual image assets (fallback when no PDF letterhead)
    header_image_path = Column(Text, nullable=True)
    footer_image_path = Column(Text, nullable=True)
    watermark_image_path = Column(Text, nullable=True)
    watermark_opacity = Column(Float, default=0.1)
    logo_path = Column(Text, nullable=True)
    logo_position = Column(String(50), default="top-left")          # top-left, top-center, top-right

    # Layout
    margin_top_mm = Column(Float, default=30.0)
    margin_bottom_mm = Column(Float, default=25.0)
    margin_left_mm = Column(Float, default=20.0)
    margin_right_mm = Column(Float, default=20.0)
    page_size = Column(String(20), default="A4")                     # A4, Letter, Legal

    # Typography
    font_family = Column(String(100), default="Times New Roman")
    font_size_pt = Column(Integer, default=12)
    line_height = Column(Float, default=1.5)

    # Custom content
    header_html = Column(Text, nullable=True)                        # Custom HTML for header
    footer_html = Column(Text, nullable=True)                        # Custom HTML for footer
    css_overrides = Column(Text, nullable=True)                      # Extra CSS rules

    # Company info override
    company_name_override = Column(String(500), nullable=True)

    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class DigitalSignature(Base):
    __tablename__ = "digital_signatures"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)            # FK to users.id
    name = Column(String(255), nullable=False)                       # Display label
    designation = Column(String(255), nullable=True)                 # "Managing Director"
    signature_image_path = Column(Text, nullable=True)               # Uploaded signature image
    stamp_image_path = Column(Text, nullable=True)                   # Company stamp/seal image
    default_position = Column(String(50), default="bottom-right")    # top/middle/bottom + left/center/right, or "custom"
    default_position_x = Column(Float, nullable=True)               # Custom X coordinate in mm from left edge
    default_position_y = Column(Float, nullable=True)               # Custom Y coordinate in mm from bottom edge
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class GeneratedDocument(Base):
    __tablename__ = "generated_documents"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    document_type = Column(String(100), default="custom")            # proposal, letter, cost_statement, certificate, custom

    # Linkages (optional)
    tender_id = Column(Integer, nullable=True, index=True)           # FK to tenders.id
    proposal_session_id = Column(Integer, nullable=True)             # FK to proposal_sessions.id

    # Template and content
    letterhead_template_id = Column(Integer, nullable=True)          # FK to letterhead_templates.id
    content_html = Column(Text, nullable=True)                       # Body content (HTML/Markdown)
    content_markdown = Column(Text, nullable=True)                   # Raw markdown for editing

    # Template variables
    template_variables = Column(JSON, default=dict)                  # {date, ref_number, addressee, subject, ...}

    # Signatures
    signatures = Column(JSON, default=list)                          # [{signature_id, position, page}]

    # Source file (offline-upload flow): the original, unsigned PDF the user
    # uploaded. Signing always re-applies from this clean source so repeated
    # re-signs never accumulate ghost overlays. Null for generated documents.
    source_file_path = Column(Text, nullable=True)

    # Generated file
    generated_file_path = Column(Text, nullable=True)
    generated_file_name = Column(String(500), nullable=True)
    file_size = Column(Integer, nullable=True)

    # Status
    status = Column(String(50), default="draft")                     # draft, generated, signed, finalized

    # Page orientation for PDF export — "portrait" (default) or "landscape"
    page_orientation = Column(String(16), default="portrait", nullable=False)

    created_by = Column(Integer, nullable=False)                     # FK to users.id
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
