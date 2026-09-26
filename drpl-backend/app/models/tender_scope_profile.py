"""
DRPL Backend - Tender Scope Profile Model

Singleton-by-name configuration that defines what tenders count as "a fit"
for DRPL: keyword groups (used to drive GeM auto-search), exclusion terms,
target ministries, value range, and a relevance threshold for surfacing.

Served to the Chrome extension via /api/extension/config and consumed by the
relevance agent prompt context on ingest.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String

from app.core.database import Base


class TenderScopeProfile(Base):
    __tablename__ = "tender_scope_profiles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True, index=True, default="default")

    # JSON: list of {label: str, keywords: [str]} groups. Each keyword is fed
    # into the GeM advance-search BOQ Title input by the extension driver.
    keyword_groups = Column(JSON, nullable=False, default=list)

    # JSON: flat list of strings; tenders containing any of these terms in
    # title/description are deprioritised by the relevance agent.
    exclusion_terms = Column(JSON, nullable=False, default=list)

    # JSON: list of GeM Ministry/Org filter strings to apply alongside each
    # keyword search (e.g. ["Ministry of Railways"]).
    target_ministries = Column(JSON, nullable=False, default=list)

    value_min = Column(Float, nullable=True)
    value_max = Column(Float, nullable=True)

    # 0..1 — tenders below this score are hidden by default in the tender list view.
    relevance_threshold = Column(Float, nullable=False, default=0.6)

    # Per-keyword pagination cap for the GeM auto-search driver. Default 5 keeps
    # us portal-friendly; admins can raise it on the Tender Scope page.
    max_pages_per_keyword = Column(Integer, nullable=False, default=5)

    is_active = Column(Boolean, nullable=False, default=True)
    updated_by = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
