"""
DRPL Backend - Ratecard Models

Structured OEM/standard ratecards (Cummins, Fleetguard, …) used as the PRIMARY
source for bottom-up component build-up costing.

Unlike the unstructured TrainingDataset text corpus, these tables give the
costing agent an exact Part-No -> Rate lookup PLUS the notion of a
"check schedule" (a B/C/D-check kit grouping a set of spare parts per engine
type). The costing agent expands an NIT scope item like "D-check on VTA 28L"
into its constituent priced parts via RatecardCheckSchedule + RatecardItem.

Ingested from Excel ratecards via app/services/ratecard_ingest_service.py.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey, Index,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class Ratecard(Base):
    """A named ratecard (one uploaded price list / collection of price lists)."""
    __tablename__ = "ratecards"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)         # "Cummins/Fleetguard FY26"
    source_label = Column(String(64), nullable=True)               # default source: "Cummins", "Fleetguard", …
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="active")  # active | archived

    original_file_name = Column(String(500), nullable=True)
    created_by = Column(Integer, nullable=True)                    # User id
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items = relationship(
        "RatecardItem",
        back_populates="ratecard",
        cascade="all, delete-orphan",
    )
    check_schedules = relationship(
        "RatecardCheckSchedule",
        back_populates="ratecard",
        cascade="all, delete-orphan",
    )


class RatecardCheckSchedule(Base):
    """
    A B/C/D-check kit: the set of spare parts replaced for a given engine type
    at a given check level. Created from section banners in the ratecard sheet
    (e.g. "NTA855R 'C' Check Schedule"). RatecardItem.check_schedule_id points
    back here so expand_check_schedule() can return the full part list.
    """
    __tablename__ = "ratecard_check_schedules"

    id = Column(Integer, primary_key=True, index=True)
    ratecard_id = Column(
        Integer, ForeignKey("ratecards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    engine_type = Column(String(64), nullable=True, index=True)    # "VTA 28L", "NTA 855R", …
    check_level = Column(String(16), nullable=True)               # "B" | "C" | "D"
    name = Column(String(255), nullable=True)                     # verbatim banner text
    # Optional phrases that map an NIT line to this kit ("D-check VTA 28L").
    nit_scope_keywords = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    ratecard = relationship("Ratecard", back_populates="check_schedules")
    items = relationship(
        "RatecardItem",
        back_populates="check_schedule",
    )

    __table_args__ = (
        Index("ix_rc_check_engine_level", "engine_type", "check_level"),
    )


class RatecardItem(Base):
    """A single priced part row in a ratecard."""
    __tablename__ = "ratecard_items"

    id = Column(Integer, primary_key=True, index=True)
    ratecard_id = Column(
        Integer, ForeignKey("ratecards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The kit this part belongs to (null for pure price-list rows reachable
    # only by exact Part No lookup).
    check_schedule_id = Column(
        Integer, ForeignKey("ratecard_check_schedules.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    engine_type = Column(String(64), nullable=True, index=True)   # "VTA 28L", "NTA 855R", …
    part_no = Column(String(128), nullable=True, index=True)      # verbatim Part No
    # Drift-tolerant exact-match key: uppercased, alnum-only (handles
    # "43828   B" vs "43828B", "AK 6065  SS" vs "AK6065SS").
    part_no_norm = Column(String(128), nullable=True, index=True)
    description = Column(Text, nullable=True)
    uom = Column(String(32), nullable=True)                       # "Nos", "Set", "L", …
    qty = Column(Float, nullable=True, default=1.0)               # kit composition qty (per engine/check)
    rate = Column(Float, nullable=True)                           # "Our Rate"
    source = Column(String(64), nullable=True)                    # Cummins | Fleetguard | Market | MMCT LOA | ABB | Online
    remark = Column(Text, nullable=True)                          # verbatim Remark cell

    is_mandatory = Column(Boolean, nullable=False, default=True)  # mandatory vs optional spare
    annexure = Column(String(8), nullable=True)                   # "A".."L" client annexure group
    sr_no = Column(Integer, nullable=True)                        # original sheet row order
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    ratecard = relationship("Ratecard", back_populates="items")
    check_schedule = relationship("RatecardCheckSchedule", back_populates="items")

    __table_args__ = (
        Index("ix_ratecard_items_part_norm", "part_no_norm"),
        Index("ix_ratecard_items_engine_check", "engine_type", "check_schedule_id"),
    )
