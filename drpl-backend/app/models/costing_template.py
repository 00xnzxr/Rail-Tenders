"""
DRPL Backend - Costing Template & BOQ Models
Stores zone-specific cost sheet formats and parsed Bill of Quantities line items.
"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, JSON
from app.core.database import Base


class CostingTemplate(Base):
    """Zone-specific BOQ/cost sheet format templates."""
    __tablename__ = "costing_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)                      # e.g., "CR Standard BOQ Format"
    zone = Column(String(50), nullable=False, index=True)           # Railway zone: CR, SECR, NFR, NR, etc.
    format_type = Column(String(50), default="boq")                 # boq, rate_schedule, cost_statement, price_bid

    # Column definitions: [{name, label, type, formula}]
    # type: "serial", "text", "number", "currency", "formula", "percentage"
    column_definitions = Column(JSON, default=list)

    # Footer computed rows: subtotal, taxes, grand total definitions
    # [{label, formula, type}] e.g., [{"label": "Sub Total", "formula": "SUM(amount)"}, ...]
    footer_rows = Column(JSON, default=list)

    # Templates for rendering
    html_template = Column(Text, nullable=True)                     # Jinja2/HTML for PDF rendering
    markdown_template = Column(Text, nullable=True)                 # Markdown template for AI generation

    # Reference
    sample_pdf_path = Column(Text, nullable=True)                   # Path to reference PDF
    description = Column(Text, nullable=True)

    output_format = Column(String(10), default="xlsx")                  # xlsx always for costing
    original_file_name = Column(String(500), nullable=True)

    is_default = Column(Boolean, default=False)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))


class BOQItem(Base):
    """Parsed Bill of Quantities line items from tender documents."""
    __tablename__ = "boq_items"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, index=True)         # FK to tenders.id
    checklist_item_id = Column(Integer, nullable=True)              # FK to checklist_items.id

    sr_no = Column(Integer, nullable=False)
    description = Column(Text, nullable=False)
    quantity = Column(Float, nullable=True)
    unit = Column(String(50), nullable=True)                        # e.g., "Nos", "Mtr", "Sqm", "Kg", "Lump Sum"

    # NIT bidding-schedule structural fields. Populated by the NIT-aware path
    # in boq_parser_service; null on rows from the legacy lean path.
    # item_code: the tender's own item code/number (often == str(sr_no), but
    #   can be schedule-prefixed like "A-3", or alphabetic).
    # schedule_name: short schedule label from the NIT — "A", "B", … —
    #   used to group rows on download (one sheet per schedule).
    # bidding_unit: verbatim "Bidding Unit" cell from the NIT (e.g. "AT Par",
    #   "Above/Below/Par"). Preserved so the IREPS submission matches.
    # basic_value: verbatim Basic Value column (estimated_rate × quantity at
    #   the tender's stated rate, before escalation).
    # escalation_pct: numeric form of the Escl.(%) column. Most schedules
    #   are "AT Par" → 0; some have line-level escalation.
    # is_tax_line: rows like "Provision of GST @ 18% on SCHEDULE-A" that the
    #   bidder doesn't cost separately — pass through unchanged.
    # extraction_confidence: "high" / "medium" / "low" from the extractor so
    #   the UI can flag rows the user should eyeball.
    item_code = Column(String(64), nullable=True)
    schedule_name = Column(String(64), nullable=True, index=True)
    bidding_unit = Column(String(64), nullable=True)
    basic_value = Column(Float, nullable=True)
    escalation_pct = Column(Float, nullable=True)
    is_tax_line = Column(Boolean, nullable=False, default=False)
    extraction_confidence = Column(String(16), nullable=True)

    # Annexure components. A NIT schedule item can say "Material Cost for
    # Conversion work (As per Annexure-II of Material list uploaded in
    # Document Section of NIT)": the annexure is a table of the materials
    # that make up that ONE item, per coach set. Its rows are captured as
    # BOQItems too, and these three columns are what keep them from being
    # read as extra scope on top of the item they break down.
    #
    # annexure_ref: the normalised label of the annexure table the row was
    #   read from ("II", "VII", "B"); null for a schedule row.
    # parent_item_id: the schedule BOQItem this row is a component of, bound
    #   by `boq_parser_service._link_annexure_components` from the citation
    #   in the parent's description; null when no schedule item cites the
    #   annexure (then the row stands alone, and costs as its own scope).
    # source_document_id: the TenderDocument the row was captured from, so a
    #   run can say which file contributed what.
    annexure_ref = Column(String(32), nullable=True, index=True)
    parent_item_id = Column(Integer, nullable=True, index=True)     # FK to boq_items.id (soft)
    source_document_id = Column(Integer, nullable=True)             # FK to tender_documents.id (soft)

    # Rates
    estimated_rate = Column(Float, nullable=True)                   # Rate from tender document (if provided)
    computed_rate = Column(Float, nullable=True)                    # Rate computed by costing agent
    rate_source = Column(String(255), nullable=True)                # "dsr_2024", "web_search", "training_data", "user_override"

    # Cost breakdown
    material_cost = Column(Float, nullable=True)
    labour_cost = Column(Float, nullable=True)
    overhead_cost = Column(Float, nullable=True)
    total_amount = Column(Float, nullable=True)

    notes = Column(Text, nullable=True)
    costing_template_id = Column(Integer, nullable=True)            # FK to costing_templates.id

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))


class BOQScheduleTotal(Base):
    """Per-schedule stated total + tender advertised value, captured verbatim
    from the NIT header/banners. Used by the reconciliation gate as the
    immutable benchmark for sum(line amounts)."""
    __tablename__ = "boq_schedule_totals"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, index=True)         # FK to tenders.id
    schedule_code = Column(String(64), nullable=False)
    stated_total = Column(Float, nullable=True)
    advertised_value = Column(Float, nullable=True)
    reconciliation_status = Column(String(16), nullable=True)  # reconciled | failed | no_anchor
    # The schedule's banner as printed ("COST OF LABOUR: ... (INCLUSIVE OF ALL
    # TAXES AND CHARGES)"): whether its rows price material, labour or both,
    # and whether their rates include GST. The costing reads it.
    title = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
