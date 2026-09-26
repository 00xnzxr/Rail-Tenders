"""
DRPL Collector - SQLAlchemy models.

Two groups, and the split matters:

``AgentRun`` is a MIRROR of drpl-backend's ``app/models/agent_run.py``,
mapped onto the same ``agent_runs`` table. The collector never imports
backend code, so it declares the columns it actually touches and nothing
else. It NEVER creates or migrates this table -- drpl-backend owns it.
``create_ledger_tables`` below is explicit about which tables it makes.

``CollectSeen`` / ``CollectSweep`` / ``CollectGap`` are the collector's own
ledger. They are what lets you answer "did we miss anything?" with evidence
rather than a shrug, and they ship with the first fetcher rather than after it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# -- agent_runs is drpl-backend's own model --------------------------------
#
# The collector runs inside the backend, so a collect run is literally an
# ``app.models.agent_run.AgentRun``: same class, same table, same session.
from app.models.agent_run import AgentRun  # noqa: E402,F401


# -- The ledger ----------------------------------------------------------


class CollectSeen(Base):
    """One row per (portal, tender_id) the collector has ever shipped.

    Written AFTER ``sink.post`` returns success -- never before. The old
    extension marked tenders done before uploading them, so a failed upload
    lost that tender permanently and silently. Getting this order right is the
    single highest-value line in the service.

    The primary key deliberately matches drpl-backend's own unique constraint
    ``ix_tenders_portal_tender_id`` so "seen here" and "stored there" cannot
    drift apart.
    """

    __tablename__ = "collect_seen"

    portal = Column(String(50), primary_key=True)
    tender_id = Column(String(255), primary_key=True)
    first_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    # WHICH fetcher produced this, e.g. "gem-allbids-v1". Without it,
    # cross-source reconciliation can say a tender was missed but not by whom.
    source_fetcher = Column(String(64), nullable=True)
    # Which search term surfaced it -- mirrors Tender.search_match_keyword.
    match_term = Column(String(255), nullable=True)
    # Sequence-gap detection needs the numeric tail of the bid number and the
    # prefix it belongs to, parsed once at write time.
    seq_prefix = Column(String(64), nullable=True, index=True)
    seq_number = Column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_collect_seen_fetcher", "source_fetcher"),
        Index("ix_collect_seen_seq", "portal", "seq_prefix", "seq_number"),
    )


class CollectSweep(Base):
    """One row per (run, portal) pass. The unit reconciliation compares."""

    __tablename__ = "collect_sweeps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, index=True)
    portal = Column(String(50), nullable=False, index=True)
    fetcher_version = Column(String(64), nullable=True)
    mode = Column(String(20), nullable=False, default="incremental")  # incremental | ministry | full
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    # The portal's OWN count (GeM's numFound). Compared against rows_seen.
    expected_total = Column(Integer, nullable=True)
    pages_walked = Column(Integer, nullable=False, default=0)
    rows_seen = Column(Integer, nullable=False, default=0)
    rows_new = Column(Integer, nullable=False, default=0)
    rows_duplicate = Column(Integer, nullable=False, default=0)
    rows_skipped = Column(Integer, nullable=False, default=0)  # out of ministry scope
    status = Column(String(20), nullable=False, default="running")
    error = Column(Text, nullable=True)

    __table_args__ = (Index("ix_collect_sweeps_run_portal", "run_id", "portal"),)


class CollectGap(Base):
    """A bid number the sequence says should exist but we have never seen.

    GeM bid numbers run sequentially within a year and prefix, so an absent
    number is the closest thing to a proof of coverage either portal offers.
    """

    __tablename__ = "collect_gaps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portal = Column(String(50), nullable=False, index=True)
    missing_id = Column(String(255), nullable=False)
    detected_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    # fetched | not_found | out_of_scope | expired
    resolution = Column(String(30), nullable=True)

    __table_args__ = (
        UniqueConstraint("portal", "missing_id", name="uq_collect_gaps_portal_missing"),
    )


class CollectDrift(Base):
    """A parser degrading rather than dying.

    These portals do not throw when they change -- they return a perfectly
    valid page with the fields renamed, and a selector-based parser reports
    zero rows as if it were a quiet day. Fill rate is the alarm.
    """

    __tablename__ = "collect_drift"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portal = Column(String(50), nullable=False, index=True)
    fetcher = Column(String(64), nullable=True)
    field = Column(String(64), nullable=False)
    fill_rate = Column(Float, nullable=False)
    baseline = Column(Float, nullable=False)
    sample_size = Column(Integer, nullable=False, default=0)
    detected_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    run_id = Column(String(36), nullable=True, index=True)
    acknowledged = Column(Boolean, nullable=False, default=False)


# Only these. ``agent_runs`` is drpl-backend's and is never created here --
# create_all against a shared Base would otherwise try to build the backend's
# table from the collector's partial column list.
LEDGER_TABLES = (
    CollectSeen.__table__,
    CollectSweep.__table__,
    CollectGap.__table__,
    CollectDrift.__table__,
)


def create_ledger_tables(engine) -> None:
    """Create the collector's own tables. Idempotent; never touches agent_runs."""
    Base.metadata.create_all(bind=engine, tables=list(LEDGER_TABLES))
