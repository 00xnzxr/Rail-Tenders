"""
DRPL Backend - Tender Model
Stores all extracted tender data from IREPS, GeM, and aggregators
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, Boolean, JSON, Index
from app.core.database import Base


class Tender(Base):
    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True, index=True)

    # Portal identification
    portal = Column(String(50), nullable=False, index=True)        # ireps, gem, tendertiger, etc.
    tender_id = Column(String(255), nullable=False)                 # Portal's own tender number
    source_url = Column(Text)

    # Core tender info
    title = Column(Text, nullable=False)
    department = Column(String(255), index=True)                    # Mechanical, Electrical, etc.
    organisation = Column(String(255), index=True)                  # Railway zone, buyer org
    description = Column(Text)
    estimated_value = Column(Float, nullable=True)
    currency = Column(String(10), default="INR")
    emd_amount = Column(Float, nullable=True)

    # Dates
    opening_date = Column(DateTime(timezone=True), nullable=True)
    closing_date = Column(DateTime(timezone=True), nullable=True, index=True)
    pre_bid_date = Column(DateTime(timezone=True), nullable=True)

    # Status tracking
    status = Column(String(50), default="open", index=True)        # open, closed, awarded, cancelled
    document_links = Column(JSON, default=list)                     # List of PDF URLs

    # Deep scrape fields (from detail pages)
    detail_url = Column(Text, nullable=True)
    full_description = Column(Text, nullable=True)                  # Full scope from detail page
    eligibility_criteria = Column(Text, nullable=True)              # Experience, turnover, certifications
    technical_specifications = Column(Text, nullable=True)          # Tech specs from detail page
    evaluation_criteria = Column(Text, nullable=True)               # How bids are evaluated
    performance_guarantee = Column(Float, nullable=True)
    performance_guarantee_percent = Column(Float, nullable=True)
    pre_bid_meeting_location = Column(String(500), nullable=True)
    delivery_location = Column(String(500), nullable=True)
    delivery_timeline = Column(String(500), nullable=True)
    buyer_contact_name = Column(String(255), nullable=True)
    buyer_contact_email = Column(String(255), nullable=True)
    buyer_contact_phone = Column(String(100), nullable=True)
    number_of_amendments = Column(Integer, default=0)
    corrigenda_links = Column(JSON, default=list)
    nit_document_links = Column(JSON, default=list)
    amendment_links = Column(JSON, default=list)
    is_detail_extracted = Column(Boolean, default=False)

    # AI Pipeline fields (Phase 2)
    ai_category = Column(String(255), nullable=True)                # AI-classified category
    ai_relevance_score = Column(Float, nullable=True)               # 0-1 relevance to DRPL capabilities
    ai_risk_score = Column(Float, nullable=True)
    ai_summary = Column(Text, nullable=True)

    # Eligibility (Phase 6)
    eligibility_status = Column(String(20), nullable=True)             # eligible, not_eligible, unknown
    eligibility_score = Column(Float, nullable=True)                    # 0-1 AI eligibility score
    eligibility_notes = Column(Text, nullable=True)

    # Scope-profile driven scraping (Phase 7 — Chrome extension overhaul)
    search_match_keyword = Column(String(255), nullable=True, index=True)  # Which scope keyword surfaced this row (GeM auto-search)
    is_eligible_indicator = Column(Boolean, nullable=True)                  # IREPS blue-tick / arrow flag (visual buyer hint, distinct from AI eligibility)
    fit_reasoning = Column(Text, nullable=True)                             # Free-text justification from the scope-aware relevance agent

    # Detailed-card fields (2026-07 redesign) — portal-stated metadata for the
    # card list. All nullable; old rows render "—". source_portal is only set
    # for aggregator listings (portal="tendertiger" + source_portal="gem").
    location = Column(String(255), nullable=True)                          # "Saran, Bihar, India"
    bid_type = Column(String(50), nullable=True)                           # NCB / GCB / Limited / Single
    source_portal = Column(String(50), nullable=True)                      # originating portal for aggregators
    category = Column(String(255), nullable=True)                          # portal-stated sector/category

    # Workflow & Organization (Phase 1)
    priority = Column(String(20), default="medium", index=True)    # critical, high, medium, low
    assigned_to = Column(Integer, nullable=True, index=True)        # FK to users.id
    assigned_at = Column(DateTime(timezone=True), nullable=True)
    submission_deadline = Column(DateTime(timezone=True), nullable=True)
    workflow_status = Column(String(50), default="new", index=True) # new, in_progress, checklist_ready, proposal_draft, proposal_review, approved, submitted

    # Metadata
    extracted_by = Column(Integer, nullable=True)                   # User ID who extracted this
    extracted_at = Column(DateTime(timezone=True))                  # When content script extracted it
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    is_duplicate = Column(Boolean, default=False)
    is_archived = Column(Boolean, default=False)
    # Archive lifecycle — see docs/superpowers/specs/2026-07-31-tender-archive-and-purge-design.md
    archived_at = Column(DateTime(timezone=True), nullable=True, index=True)  # purge clock
    archive_reason = Column(String(30), nullable=True)   # past_due | auto_discard | manual

    # Workspace
    workspace_enabled = Column(Boolean, default=False)               # Canvas workspace activated for this tender

    # Auto tender-scoring agent
    below_threshold = Column(Boolean, default=False, nullable=False)   # value < ₹50L → hidden from default list
    scoring_attempts = Column(Integer, default=0, nullable=False)      # auto-scoring retry counter
    segment = Column(String(20), nullable=True)                        # to_bid | not_bidable | discarded | None(unscored)
    segment_overridden = Column(Boolean, default=False, nullable=False) # True once a user manually sets the segment

    # Eager analysis (pre-fetch pipeline warmup)
    eager_analysis_status = Column(String(16), nullable=True, index=True)   # null/queued/running/done/failed
    eager_analysis_at = Column(DateTime(timezone=True), nullable=True)      # last eager-analysis state change

    # Composite unique constraint: one tender per portal
    __table_args__ = (
        Index("ix_tenders_portal_tender_id", "portal", "tender_id", unique=True),
    )


class TenderDocument(Base):
    __tablename__ = "tender_documents"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, index=True)         # FK to tenders.id
    file_name = Column(String(500), nullable=False)
    file_path = Column(Text, nullable=False)
    file_size = Column(Integer, nullable=True)
    mime_type = Column(String(100), default="application/pdf")
    document_type = Column(String(100), nullable=True)               # checklist_upload, tender_notice, proposal
    checklist_item_id = Column(Integer, nullable=True)               # FK to checklist_items.id
    uploaded_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    uploaded_by = Column(Integer, nullable=True)

    # Linked document tracking
    parent_document_id = Column(Integer, nullable=True, index=True)  # FK to tender_documents.id (self-ref)
    source_url = Column(Text, nullable=True)                         # URL this was downloaded from
    extraction_status = Column(String(30), default="pending")        # pending, processing, completed, failed, skipped
    gem_file_id = Column(String(50), nullable=True, index=True)      # GEM portal file identifier


class ScrapeLog(Base):
    __tablename__ = "scrape_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    portal = Column(String(50), nullable=False)
    session_id = Column(String(255))
    status = Column(String(50))                                     # running, completed, error
    tenders_found = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)
