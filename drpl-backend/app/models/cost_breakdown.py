"""
DRPL Backend - Cost Breakdown Models
Per-tender editable cost breakdowns with versioning. The costing agent writes
the initial draft; users edit individual line items in the Command Center
artifact panel; XLSX exports regenerate from the latest saved state.
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey,
    Index,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class CostBreakdown(Base):
    """One per (tender, version). Newest version is the working copy."""
    __tablename__ = "cost_breakdowns"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)

    # "draft" — agent or user edits; "finalized" — locked snapshot
    status = Column(String(32), nullable=False, default="draft")
    title = Column(String(255), nullable=True)

    # Org defaults at creation time (so historical breakdowns reproduce
    # identically even if PlatformSetting values change later)
    overhead_percent = Column(Float, nullable=False, default=10.0)
    margin_percent = Column(Float, nullable=False, default=15.0)
    gst_percent = Column(Float, nullable=False, default=18.0)

    # Computed totals — "expected" band (recalculated whenever lines change).
    # Kept as the legacy single-value totals so existing API consumers,
    # XLSX exports, and the editable totals block all keep working unchanged.
    subtotal = Column(Float, nullable=False, default=0.0)
    overhead_amount = Column(Float, nullable=False, default=0.0)
    margin_amount = Column(Float, nullable=False, default=0.0)
    gst_amount = Column(Float, nullable=False, default=0.0)
    grand_total = Column(Float, nullable=False, default=0.0)
    needs_input_count = Column(Integer, nullable=False, default=0)

    # Range totals — populated when the costing agent supplies low/high rate
    # bands per line. Null when the breakdown was produced in single-rate mode.
    subtotal_low = Column(Float, nullable=True)
    subtotal_high = Column(Float, nullable=True)
    overhead_amount_low = Column(Float, nullable=True)
    overhead_amount_high = Column(Float, nullable=True)
    margin_amount_low = Column(Float, nullable=True)
    margin_amount_high = Column(Float, nullable=True)
    gst_amount_low = Column(Float, nullable=True)
    gst_amount_high = Column(Float, nullable=True)
    grand_total_low = Column(Float, nullable=True)
    grand_total_high = Column(Float, nullable=True)

    # Free-form arrays from the costing agent
    assumptions_json = Column(Text, nullable=True)        # JSON array of strings
    recommendations_json = Column(Text, nullable=True)    # JSON array of strings
    # Manpower & resource decomposition emitted by the costing agent — JSON
    # array of {scope_bucket, manpower:[], resources:[], volume_drivers:[]}.
    # Surfaced in the editable panel so users can audit the build-up.
    manpower_resource_analysis_json = Column(Text, nullable=True)
    # Phase 3b — reference-Excel structural fields.
    # cost_assumptions_json: flat rate-card library matching reference Sheet 2
    #   (4 sections: Materials, Major Component Repair, Electrical Spares,
    #    Labour & Overhead). Array of {section, item, rate_inr, uom, source_ref}.
    # strategic_summary_json: tender snapshot + key observations + recommended
    #   bid (matches reference Sheet 1 — tender#, value, EMD, eligibility,
    #   schedule-wise margin breakdown, 3-5 strategy bullets).
    cost_assumptions_json = Column(Text, nullable=True)
    strategic_summary_json = Column(Text, nullable=True)

    # Tender-vs-cost roll-up (set when tender_rate is present per line).
    # tender_total = SUM(tender_amount across priced lines); margin_total =
    # tender_total - subtotal_expected. Surfaces in the editor's summary table
    # so the user sees gross margin at a glance.
    tender_total = Column(Float, nullable=True)
    margin_total_low = Column(Float, nullable=True)
    margin_total = Column(Float, nullable=True)         # expected band
    margin_total_high = Column(Float, nullable=True)
    margin_pct = Column(Float, nullable=True)           # margin_total / tender_total × 100

    # Output layout selection for XLSX regeneration. One of:
    #   "nit_mirror" | "margin_analysis" | "client_annexure" | null (auto-detect).
    # null preserves the legacy row-sniffing behaviour; an explicit value forces
    # build_cost_xlsx to use that layout. Surfaced as a per-tender selector.
    cost_sheet_template = Column(String(32), nullable=True)

    # Reconciliation gate (see app/services/costing/nit_reconciliation.py).
    # reconciliation_json: serialized ReconciliationReport — per-schedule
    #   sum(line amounts) vs the tender's own stated schedule totals + grand
    #   total vs advertised value. Null when the tender has no captured
    #   BOQScheduleTotal rows to reconcile against.
    # needs_review: True when the gate found any schedule (or the grand total)
    #   out of tolerance (±₹1). Purely a flag — the gate NEVER mutates line
    #   amounts to force a match.
    reconciliation_json = Column(Text, nullable=True)
    needs_review = Column(Boolean, nullable=False, default=False)

    # Provenance / audit
    # created_by is the ownership column the per-user firewall scopes on
    # (app/core/ownership.py). This table was keyed only by tender + version,
    # so before it existed two costing researchers working the same tender saw
    # each other's numbers. Null means a row that predates the wall: readable
    # by master_admin only, never guessed onto a user.
    created_by = Column(Integer, nullable=True, index=True)  # FK to users.id
    created_by_agent = Column(String(100), nullable=True)   # "costing_researcher" or null if user-created
    last_edited_by = Column(Integer, nullable=True)         # User id
    session_id = Column(Integer, nullable=True)             # ProposalSession.id (for chat context)
    artifact_id = Column(Integer, nullable=True)            # latest cost_breakdown_xlsx artifact

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    lines = relationship(
        "CostBreakdownLine",
        back_populates="breakdown",
        cascade="all, delete-orphan",
        order_by="CostBreakdownLine.sr_no",
    )

    __table_args__ = (
        Index("ix_cost_breakdowns_tender_version", "tender_id", "version"),
    )


class CostBreakdownLine(Base):
    """One row per line item in a CostBreakdown."""
    __tablename__ = "cost_breakdown_lines"

    id = Column(Integer, primary_key=True, index=True)
    cost_breakdown_id = Column(
        Integer, ForeignKey("cost_breakdowns.id", ondelete="CASCADE"), nullable=False, index=True
    )

    sr_no = Column(Integer, nullable=False, default=1)
    description = Column(Text, nullable=False)
    category = Column(String(64), nullable=True)
    quantity = Column(Float, nullable=True)
    unit = Column(String(32), nullable=True)

    # Single-value rate / amount — represents the "expected" band when range
    # mode is in use. Kept as the editable primary so existing UI bindings,
    # XLSX exports, and the recompute logic all keep working unchanged.
    rate = Column(Float, nullable=True)        # null when needs_input
    amount = Column(Float, nullable=True)      # quantity * rate (server-computed on save)

    # Range fields — populated when the costing agent emits a low/high rate
    # band. Null in single-rate mode (legacy / user-entered lines).
    rate_low = Column(Float, nullable=True)
    rate_high = Column(Float, nullable=True)
    amount_low = Column(Float, nullable=True)
    amount_high = Column(Float, nullable=True)

    # Per-line profit — the costing agent varies margin by category
    # (labour 14–18%, equipment 12–15%, material 8–12%, etc.) and computes
    # profit on each band. Falls back to the breakdown-level margin_percent
    # when null (legacy lines / org-default behaviour).
    profit_pct = Column(Float, nullable=True)
    profit_amount_low = Column(Float, nullable=True)
    profit_amount = Column(Float, nullable=True)        # expected band
    profit_amount_high = Column(Float, nullable=True)

    # Provenance fields produced by the costing agent
    rate_source = Column(String(32), nullable=True)   # training_data | tender_estimate | web_search | memory | derived_estimate | needs_user_input | user_override
    source_ref = Column(Text, nullable=True)          # URL, dataset ref, build-up formula, or "needs input" question
    confidence = Column(String(16), nullable=True)    # high | medium | low

    # Piece 3 — structured provenance for web-priced lines.
    oem_manufacturer = Column(String(128), nullable=True)  # best-effort OEM / manufacturer
    source_url = Column(Text, nullable=True)               # guaranteed web link

    # Phase 3b — gross-margin analysis (matches reference Excel columns).
    # tender_rate: the rate stated in the tender BOQ for this line, when present.
    # tender_amount: tender_rate × quantity (cached so the editor can render
    #   without recomputing).
    # margin_amount_*: tender_amount - amount_band per band. Positive = gross
    #   margin we'd capture; negative = we'd bid below cost on this line.
    # margin_pct: margin_amount_expected / tender_amount × 100.
    # schedule_section: free-text grouping label ("Schedule A — Part 1", etc.).
    # cost_buildup_note: short string explaining the unit-cost build-up
    #   (matches reference Cost build-up note column).
    tender_rate = Column(Float, nullable=True)
    tender_amount = Column(Float, nullable=True)
    margin_amount_low = Column(Float, nullable=True)
    margin_amount = Column(Float, nullable=True)        # expected band
    margin_amount_high = Column(Float, nullable=True)
    margin_pct = Column(Float, nullable=True)
    schedule_section = Column(String(128), nullable=True, index=True)
    cost_buildup_note = Column(Text, nullable=True)

    # NIT-mirror fields. Set when the costing agent produces a 1:1 mirror of
    # the tender's bidding schedule (captured in BOQItem). boq_item_id is the
    # join key; the rest are denormalised from BOQItem so the editor and
    # XLSX/PDF exports can render without an extra fetch and so the costing
    # survives a BOQ re-parse via (schedule_name, item_code) rebind.
    boq_item_id = Column(
        Integer, ForeignKey("boq_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    item_code = Column(String(64), nullable=True)
    schedule_name = Column(String(64), nullable=True, index=True)
    bidding_unit = Column(String(64), nullable=True)
    # Component build-up grouping ("A".."L") for the client-annexure layout.
    # Set by the component-expansion costing path; null on NIT-mirror lines.
    annexure = Column(String(8), nullable=True, index=True)
    basic_value = Column(Float, nullable=True)
    escalation_pct = Column(Float, nullable=True)
    is_tax_line = Column(Boolean, nullable=False, default=False)

    # Annexure component lines (see BOQItem.parent_item_id). A line whose
    # parent_boq_item_id is set is one material of the schedule item it
    # points at: its amount is a per-set cost that ROLLS UP into the parent's
    # estimated rate (`cost_breakdown_service.rollup_component_lines`) and is
    # never added to the breakdown's own totals -- the parent already carries
    # it. annexure_ref names the table it came from, for the group label.
    parent_boq_item_id = Column(
        Integer, ForeignKey("boq_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    annexure_ref = Column(String(32), nullable=True)

    needs_input = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    breakdown = relationship("CostBreakdown", back_populates="lines")
