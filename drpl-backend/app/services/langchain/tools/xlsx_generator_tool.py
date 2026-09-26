"""
DRPL LangChain Tool - XLSX Generator
Emits an Excel spreadsheet from a cost-breakdown JSON payload using openpyxl.
Primary consumer is the costing agent, which parses its own COSTING_JSON block
and calls this tool to produce a downloadable .xlsx artifact.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Cost-only column layout (single rate, formula-driven). The agent emits
# one rate per line; the user fills Margin % per line (or leaves blank to use
# the global default). All Amount / Margin Amount cells are FORMULAS so
# editing rates or percentages in Excel re-computes totals on the fly.
_COST_COLUMNS = [
    ("sr_no", "SN", 5),
    ("description", "Description", 48),
    ("category", "Category", 16),
    ("qty", "Qty", 10),
    ("unit", "Unit", 10),
    ("rate", "Rate (₹)", 14),
    ("amount", "Amount (₹)", 16),
    ("rate_source", "Source", 16),
    ("source_ref", "Source Ref / Note", 40),
    ("oem_manufacturer", "OEM / Manufacturer", 22),
    ("source_url", "Web Source", 40),
]

# Legacy aliases — older callers (and pre-existing breakdowns persisted with
# range data) may still pass through here. We keep the names but route them
# all through the formula-driven builder.
_DEFAULT_COLUMNS = _COST_COLUMNS
_RANGE_COLUMNS = _COST_COLUMNS

# Phase 3b — margin-analysis layout matching the reference Excel's
# Schedule X Costing sheets: Tender Rate / Tender Amount / Est. Cost /
# Margin / Margin %. Used when ANY row carries tender_rate / tender_amount.
_MARGIN_COLUMNS = [
    ("sr_no", "SN", 5),
    ("item_code", "Item Code", 12),
    ("description", "Item Description", 40),
    ("unit", "UoM", 8),
    ("qty", "Qty", 8),
    ("tender_rate", "Tender Rate (₹)", 14),
    ("tender_amount", "Tender Amount (₹)", 16),
    ("rate", "Est. Unit Cost (₹)", 16),
    ("amount", "Est. Total Cost (₹)", 17),
    ("rate_source", "Source", 14),
    ("cost_buildup_note", "Cost build-up note", 36),
    ("oem_manufacturer", "OEM / Manufacturer", 22),
    ("source_url", "Web Source", 40),
]

_CURRENCY_COLS = {
    "rate", "rate_low", "rate_high",
    "amount", "amount_low", "amount_high",
    "profit_amount", "profit_amount_low", "profit_amount_high",
    "total", "total_low", "total_high",
    "tender_rate", "tender_amount",
    "margin_amount", "margin_amount_low", "margin_amount_high",
    "rate_inr",
}
_PERCENT_COLS = {"gst_pct", "profit_pct", "margin_pct"}


#: The Source column in words a bidder reads. The stored value stays the
#: code (the editor and the artifact preview key on it); only the cell says
#: what it means.
_SOURCE_WORDS = {
    "derived_estimate": "Cost build-up",
    "web_search": "Market price (web)",
    "training_data": "Firm's rate data",
    "memory": "Firm's rate note",
    "ratecard": "Firm's rate card",
    "component_buildup": "Sum of its annexure items",
    "printed_nil_total": "Nil, as printed",
    "tender_estimate": "From the NIT (tax line)",
    "needs_user_input": "Needs a rate",
}


def _source_words(row: dict) -> str:
    """The row's Source cell: what its figure rests on, in plain words."""
    raw = str(row.get("rate_source") or "")
    if str(row.get("cost_buildup_note") or "").startswith("Basis: railway estimate"):
        return "Railway estimate (fallback)"
    return _SOURCE_WORDS.get(raw, raw)


def _with_source_words(rows: list[dict]) -> list[dict]:
    return [{**r, "rate_source": _source_words(r)} if isinstance(r, dict) else r for r in rows]


def _is_range_layout(rows: list[dict]) -> bool:
    """Switch to the range column layout when any row has low/high data."""
    range_keys = (
        "rate_low", "rate_high", "amount_low", "amount_high",
        "profit_pct", "profit_amount", "profit_amount_low", "profit_amount_high",
    )
    for row in rows:
        for k in range_keys:
            if row.get(k) not in (None, "", 0):
                return True
    return False


def _is_margin_layout(rows: list[dict]) -> bool:
    """Switch to the margin-analysis layout when any row has tender_rate / tender_amount."""
    for row in rows:
        if row.get("tender_rate") not in (None, "", 0) or row.get("tender_amount") not in (None, "", 0):
            return True
    return False


def _is_nit_mirror_layout(rows: list[dict]) -> bool:
    """Switch to the NIT-mirror layout when the costing agent has produced a
    1:1 mirror of the tender's bidding schedule. Trigger: any row carries
    a `boq_item_id` (the FK from CostBreakdownLine to BOQItem) OR has both
    `item_code` and `schedule_name` set.

    This layout matches the IREPS/GeM bidding-schedule shape exactly so the
    output XLSX can be uploaded to the procurement portal with minimal
    reformatting.
    """
    for row in rows:
        if row.get("boq_item_id") not in (None, "", 0):
            return True
        if row.get("item_code") and row.get("schedule_name"):
            return True
    return False


def _is_client_annexure_layout(rows: list[dict]) -> bool:
    """Switch to the client-annexure layout when any row is tagged with an
    annexure group (the component build-up costing path sets this)."""
    for row in rows:
        if (row.get("annexure") or "").strip():
            return True
    return False


def _group_rows_by_annexure(rows: list[dict]) -> "dict[str, list[dict]]":
    """Bucket rows by `annexure` ("A".."L"), preserving first-seen order."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        ann = (row.get("annexure") or "").strip() or "?"
        if ann not in groups:
            groups[ann] = []
            order.append(ann)
        groups[ann].append(row)
    return {ann: groups[ann] for ann in order}


def _group_rows_by_schedule_name(rows: list[dict]) -> "dict[str, list[dict]]":
    """NIT-mirror grouping — bucket by `schedule_name` (short code like "A").
    Distinct from `_group_rows_by_schedule` which buckets on the free-text
    `schedule_section` used by the margin-analysis layout.
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        sn = (row.get("schedule_name") or "").strip() or "?"
        if sn not in groups:
            groups[sn] = []
            order.append(sn)
        groups[sn].append(row)
    return {sn: groups[sn] for sn in order}


def _is_component_group(rows: list[dict]) -> bool:
    """True when every row of a group is an annexure component (its
    `parent_boq_item_id` set): a per-set build-up of ONE schedule item,
    whose cost that item already carries."""
    return bool(rows) and all(
        r.get("is_component") or r.get("parent_boq_item_id") not in (None, "", 0)
        for r in rows
    )


def _group_rows_by_schedule(rows: list[dict]) -> "dict[str, list[dict]]":
    """Bucket rows by schedule_section, preserving original order within each."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in rows:
        sec = (row.get("schedule_section") or "").strip() or ""
        if sec not in groups:
            groups[sec] = []
            order.append(sec)
        groups[sec].append(row)
    return {sec: groups[sec] for sec in order}


def _write_reconciliation_block(
    ws,
    start_row: int,
    reconciliation: Optional[dict],
    *,
    header_fill,
    header_font,
    bold_font,
    border,
) -> int:
    """Write a "Reconciliation" section (Task 5's per-schedule stated total
    vs line-sum check) starting at ``start_row``. No-op (returns start_row
    unchanged) when there is no reconciliation report — e.g. the tender has
    no captured BOQScheduleTotal rows to check against.

    Returns the next free row after the block.
    """
    from openpyxl.styles import Font, PatternFill

    if not reconciliation:
        return start_row

    r = start_row
    ws.cell(row=r, column=1, value="Reconciliation").font = Font(bold=True, size=12)
    r += 1

    headers = ["Schedule", "Line Sum (₹)", "Stated Total (₹)", "Delta (₹)", "Status"]
    for i, label in enumerate(headers, start=1):
        c = ws.cell(row=r, column=i, value=label)
        c.fill = header_fill
        c.font = header_font
        c.border = border
    r += 1

    for sched in reconciliation.get("schedules") or []:
        ws.cell(row=r, column=1, value=sched.get("code")).border = border
        c2 = ws.cell(row=r, column=2, value=sched.get("line_sum"))
        c2.number_format = '#,##0.00'
        c2.border = border
        c3 = ws.cell(row=r, column=3, value=sched.get("stated_total"))
        c3.number_format = '#,##0.00'
        c3.border = border
        c4 = ws.cell(row=r, column=4, value=sched.get("delta"))
        c4.number_format = '#,##0.00'
        c4.border = border
        status = "PASS" if sched.get("ok") else "FLAG"
        c5 = ws.cell(row=r, column=5, value=status)
        c5.font = bold_font
        c5.border = border
        if not sched.get("ok"):
            c5.fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
        r += 1

    r += 1
    grand_status = "PASS" if reconciliation.get("ok") else "FLAG"
    ws.cell(row=r, column=1, value="Grand Total").font = bold_font
    gc2 = ws.cell(row=r, column=2, value=reconciliation.get("grand_line_sum"))
    gc2.number_format = '#,##0.00'
    gc3 = ws.cell(row=r, column=3, value=reconciliation.get("advertised_value"))
    gc3.number_format = '#,##0.00'
    gc4 = ws.cell(row=r, column=4, value=reconciliation.get("grand_delta"))
    gc4.number_format = '#,##0.00'
    gc5 = ws.cell(row=r, column=5, value=grand_status)
    gc5.font = bold_font
    r += 1

    return r + 1


class XlsxGeneratorInput(BaseModel):
    """Input schema for the xlsx_generator tool."""
    title: str = Field(
        "Cost Breakdown",
        description="Title shown as the sheet tab and in the title row of the worksheet.",
    )
    rows: list[dict] = Field(
        ...,
        description=(
            "List of row dicts. Standard keys: description, category, qty, unit, "
            "rate, amount, gst_pct, total. Missing keys render as blank cells."
        ),
    )
    tender_id: Optional[int] = Field(
        None,
        description="Tender this breakdown belongs to (for filename tagging).",
    )
    session_id: Optional[int] = Field(
        None,
        description="Command Center session to attach the generated artifact to.",
    )


def build_cost_xlsx(
    path: str,
    title: str,
    rows: list[dict],
    *,
    cost_assumptions: Optional[list[dict]] = None,
    strategic_summary: Optional[dict] = None,
    breakdown_meta: Optional[dict] = None,
    single_sheet: Optional[bool] = None,
    layout: Optional[str] = None,
) -> dict:
    """Render a cost-breakdown workbook to ``path``. Returns totals + row count.

    ``layout`` (when given) forces a specific output layout, overriding the
    auto-detection below. One of: "client_annexure", "nit_mirror",
    "margin_analysis". None preserves the legacy row-sniffing behaviour.

    Layout selection (NIT-mirror takes precedence):
      - **NIT-mirror** (RULE 5) — if any row has `boq_item_id` OR has both
        `item_code` and `schedule_name`, produce a workbook that mirrors the
        tender's NIT "Schedule of Items" table exactly: Summary sheet plus
        one sheet per schedule, columns S.No. / Item Code / Description /
        Item Qty / Qty Unit / Unit Rate / Basic Value / Escl.(%) / Amount /
        Bidding Unit. Tax rows pass through as the NIT renders them. The
        output can be submitted directly to IREPS/GeM with minimal edits.
      - Else if `strategic_summary` or `cost_assumptions` is supplied OR any
        row has `tender_rate` (margin-analysis mode), produces a multi-sheet
        workbook matching the user's reference Excel:
          1. Summary  — tender snapshot + schedule-wise profitability + observations
          2. Cost Assumptions  — flat rate-card library
          3+. Schedule X Costing  — one sheet per `schedule_section` (or one
              consolidated sheet when no schedule grouping)
      - Else falls back to the legacy single-rate _DEFAULT_COLUMNS layout.
    """
    from openpyxl import Workbook  # noqa: F401 — kept for symmetry with callers

    # Copies, so the caller's rows (persisted with the artifact) keep the codes.
    rows = _with_source_words(rows or [])

    # Explicit layout selection takes precedence over auto-detection.
    layout = (layout or "").strip().lower() or None
    if layout == "client_annexure" or (
        layout is None and _is_client_annexure_layout(rows)
    ):
        return _build_client_annexure(
            path, title, rows,
            breakdown_meta=breakdown_meta or {},
            strategic_summary=strategic_summary or {},
        )

    use_margin = _is_margin_layout(rows) or bool(strategic_summary) or bool(cost_assumptions)
    if layout == "margin_analysis":
        return _build_multi_sheet(
            path, title, rows,
            cost_assumptions=cost_assumptions or [],
            strategic_summary=strategic_summary or {},
            breakdown_meta=breakdown_meta or {},
            single_sheet=bool(single_sheet),
        )
    if layout == "nit_mirror":
        if single_sheet:
            return _build_nit_mirror_single_sheet(
                path, title, rows,
                breakdown_meta=breakdown_meta or {},
                strategic_summary=strategic_summary or {},
            )
        return _build_nit_mirror(
            path, title, rows,
            breakdown_meta=breakdown_meta or {},
            strategic_summary=strategic_summary or {},
        )

    # NIT-mirror (bare IREPS columns) normally takes precedence. BUT when the
    # caller asked for a single sheet AND the rows carry cost/margin data, the
    # user wants the consolidated margin sheet (full Est. Cost / Margin / Source
    # columns) stacked in NIT order — not the bare submission sheet. So only take
    # the NIT-mirror branch when we're NOT producing the consolidated margin sheet.
    if _is_nit_mirror_layout(rows) and not (single_sheet and use_margin):
        if single_sheet:
            return _build_nit_mirror_single_sheet(
                path, title, rows,
                breakdown_meta=breakdown_meta or {},
                strategic_summary=strategic_summary or {},
            )
        return _build_nit_mirror(
            path, title, rows,
            breakdown_meta=breakdown_meta or {},
            strategic_summary=strategic_summary or {},
        )

    if use_margin:
        return _build_multi_sheet(
            path, title, rows,
            cost_assumptions=cost_assumptions or [],
            strategic_summary=strategic_summary or {},
            breakdown_meta=breakdown_meta or {},
            single_sheet=bool(single_sheet),
        )
    return _build_single_sheet(path, title, rows, breakdown_meta=breakdown_meta or {})


def _build_client_annexure(
    path: str,
    title: str,
    rows: list[dict],
    *,
    breakdown_meta: Optional[dict] = None,
    strategic_summary: Optional[dict] = None,
) -> dict:
    """Client-annexure builder — bottom-up component build-up, single tab.

    Mirrors the client's Costing Sheet format: rows grouped into Annexures
    (A..L), each with columns
        S.N. | Part No. | Part Desc | Qty | Our Rate | Rate | Remark
    where Rate = Qty × Our Rate as a live formula and Remark carries the rate
    source (Cummins / Fleetguard / Market / …). Per-annexure subtotals, then a
    grand total + overhead/margin/GST roll-up with editable named knob cells.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName

    breakdown_meta = breakdown_meta or {}
    overhead_pct = float(breakdown_meta.get("overhead_percent", 10.0) or 0.0)
    margin_pct = float(breakdown_meta.get("margin_percent", 15.0) or 0.0)
    gst_pct = float(breakdown_meta.get("gst_percent", 18.0) or 0.0)
    tender_id = breakdown_meta.get("tender_id")

    # Column order matches the client's sheet. "Our Rate" is the editable cost
    # rate; "Rate" (= Qty × Our Rate) is the computed line amount.
    COLUMNS = [
        ("sr_no",       "S.N.",      6),
        ("item_code",   "Part No.",  18),
        ("description", "Part Desc", 50),
        ("quantity",    "Qty",       8),
        ("rate",        "Our Rate",  14),
        ("amount",      "Rate",      16),
        ("source_ref",  "Remark",    18),
    ]
    n_cols = len(COLUMNS)
    col_keys = [k for k, _, _ in COLUMNS]
    qty_idx = col_keys.index("quantity") + 1
    ourrate_idx = col_keys.index("rate") + 1
    amount_idx = col_keys.index("amount") + 1
    qty_col = get_column_letter(qty_idx)
    ourrate_col = get_column_letter(ourrate_idx)
    amount_col = get_column_letter(amount_idx)

    HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
    BANNER_FILL = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    BANNER_FONT = Font(color="FFFFFF", bold=True, size=12)
    EDITABLE_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    TOTAL_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    BOLD = Font(bold=True)
    THIN = Side(style="thin", color="9E9E9E")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    wb = Workbook()
    ws = wb.active
    sheet_name = "Costing Sheet"
    ws.title = sheet_name
    for i, (_, _, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    r = 1
    ws.cell(row=r, column=1, value=title or "Costing Sheet").font = Font(size=14, bold=True)
    r += 1
    if tender_id:
        ws.cell(row=r, column=1, value="Tender ID").font = BOLD
        ws.cell(row=r, column=2, value=int(tender_id))
        r += 1

    # Editable commercial knobs (named cells).
    r += 1
    ws.cell(row=r, column=1, value="Commercial knobs (edit in place)").font = BOLD
    r += 1
    for label, key, default in (
        ("GST %", "ANN_GST_PCT", gst_pct),
    ):
        ws.cell(row=r, column=1, value=label).font = BOLD
        cell = ws.cell(row=r, column=2, value=float(default))
        cell.fill = EDITABLE_FILL
        cell.border = BORDER
        coord = f"'{sheet_name}'!${get_column_letter(2)}${r}"
        wb.defined_names[key] = DefinedName(name=key, attr_text=coord)
        r += 1

    r += 1  # spacer

    groups = _group_rows_by_annexure(rows)
    sorted_annexures = sorted(groups.keys(), key=lambda s: (s == "?", s))
    per_annexure_total_rows: list[int] = []

    for ann in sorted_annexures:
        ann_rows = groups[ann]
        banner_label = f"Annexure {ann}" if ann != "?" else "Other Items"
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n_cols)
        bcell = ws.cell(row=r, column=1, value=banner_label)
        bcell.fill = BANNER_FILL
        bcell.font = BANNER_FONT
        bcell.alignment = Alignment(horizontal="left", vertical="center")
        r += 1

        for i, (_, label, _) in enumerate(COLUMNS, start=1):
            c = ws.cell(row=r, column=i, value=label)
            c.fill = HEADER_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = BORDER
        r += 1

        data_start = r
        for offset, row in enumerate(ann_rows):
            xl_row = r
            for i, (key, _, _) in enumerate(COLUMNS, start=1):
                if key == "sr_no":
                    val = row.get("sr_no") or (offset + 1)
                elif key == "amount":
                    val = f"=IFERROR({qty_col}{xl_row}*{ourrate_col}{xl_row},0)"
                else:
                    val = row.get(key)
                    if key == "rate" and val is None:
                        val = 0
                    if key == "quantity" and val is None:
                        val = 1
                cell = ws.cell(row=xl_row, column=i, value=val)
                cell.border = BORDER
                if key in ("quantity", "rate"):
                    cell.fill = EDITABLE_FILL
                if key in ("quantity", "rate", "amount"):
                    cell.number_format = '#,##0.00'
            r += 1
        data_end = r - 1

        ws.cell(row=r, column=1, value=f"Annexure {ann} Total").font = BOLD
        tcell = ws.cell(
            row=r,
            column=amount_idx,
            value=(
                f"=SUM({amount_col}{data_start}:{amount_col}{data_end})"
                if data_end >= data_start else 0
            ),
        )
        tcell.font = BOLD
        tcell.fill = TOTAL_FILL
        tcell.number_format = '#,##0.00'
        tcell.border = BORDER
        per_annexure_total_rows.append(r)
        r += 2

    # Grand total + overhead / margin / GST roll-up.
    ws.cell(row=r, column=1, value="Grand Total (pre-GST)").font = Font(bold=True, size=12)
    if per_annexure_total_rows:
        grand_val = "=" + "+".join(f"{amount_col}{tr}" for tr in per_annexure_total_rows)
    else:
        grand_val = 0
    gcell = ws.cell(row=r, column=amount_idx, value=grand_val)
    gcell.font = Font(bold=True, size=12)
    gcell.fill = TOTAL_FILL
    gcell.number_format = '#,##0.00'
    grand_row = r
    r += 1
    ws.cell(row=r, column=1, value="+ GST").alignment = Alignment(indent=1)
    ws.cell(
        row=r, column=amount_idx,
        value=f"={amount_col}{grand_row}*ANN_GST_PCT/100",
    ).number_format = '#,##0.00'
    gst_row = r
    r += 1
    ws.cell(row=r, column=1, value="Bid Total (with GST)").font = Font(bold=True, size=12)
    btcell = ws.cell(
        row=r, column=amount_idx,
        value=f"={amount_col}{grand_row}+{amount_col}{gst_row}",
    )
    btcell.font = Font(bold=True, size=12)
    btcell.fill = TOTAL_FILL
    btcell.number_format = '#,##0.00'

    # Reconciliation (breakdown-time gate) — show the per-schedule stated-total
    # vs line-sum table here too, so no layout silently omits it. No-op when
    # there is no reconciliation report.
    _write_reconciliation_block(
        ws,
        r + 2,
        breakdown_meta.get("reconciliation"),
        header_fill=HEADER_FILL,
        header_font=HEADER_FONT,
        bold_font=BOLD,
        border=BORDER,
    )

    wb.save(path)

    return {
        "title": title,
        "row_count": len(rows),
        "annexure_count": len(groups),
        "layout": "client_annexure",
    }


def _build_nit_mirror(
    path: str,
    title: str,
    rows: list[dict],
    *,
    breakdown_meta: Optional[dict] = None,
    strategic_summary: Optional[dict] = None,
) -> dict:
    """NIT-mirror builder.

    Layout:
      - **Summary sheet**: tender header (no / title / closing date if available
        via strategic_summary), editable margin/overhead/GST knobs as named
        cells (NIT_MARGIN_PCT / NIT_OVERHEAD_PCT / NIT_GST_PCT) so the user
        can tweak commercials directly in Excel, plus per-schedule totals.
      - **One sheet per `schedule_name`** ("Schedule A", "Schedule B", ...)
        with NIT columns. Amount = Item Qty × Unit Rate × (1 + Escl%/100)
        as a live formula. Tax rows show the GST formula referring to the
        prior schedule's total so editing rates ripples through.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName

    breakdown_meta = breakdown_meta or {}
    overhead_pct = float(breakdown_meta.get("overhead_percent", 10.0) or 0.0)
    margin_pct = float(breakdown_meta.get("margin_percent", 15.0) or 0.0)
    gst_pct = float(breakdown_meta.get("gst_percent", 18.0) or 0.0)
    tender_id = breakdown_meta.get("tender_id")

    # NIT column order — matches the IREPS Schedule of Items table. "Unit
    # Rate"/"Tender Amount" mirror the NIT's OWN published numbers
    # (tender_rate/tender_amount); the firm's own cost estimate is a separate
    # "Est. Rate"/"Est. Amount" pair so the two are never conflated.
    NIT_COLUMNS = [
        ("sr_no",         "S.No.",        8),
        ("item_code",     "Item Code",    12),
        ("description",   "Description", 60),
        ("quantity",      "Item Qty",    12),
        ("unit",          "Qty Unit",    14),
        ("tender_rate",   "Unit Rate",   14),
        ("basic_value",   "Basic Value", 16),
        ("escalation_pct", "Escl.(%)",    10),
        ("tender_amount", "Tender Amount", 16),
        ("rate",          "Est. Rate",   14),
        ("amount",        "Est. Amount", 16),
        ("bidding_unit",  "Bidding Unit", 14),
    ]

    HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
    EDITABLE_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    TAX_FILL = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
    TOTAL_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    BOLD = Font(bold=True)
    THIN = Side(style="thin", color="9E9E9E")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    wb = Workbook()

    # -- Summary sheet --
    summary_ws = wb.active
    summary_ws.title = "Summary"
    summary_ws.column_dimensions["A"].width = 32
    summary_ws.column_dimensions["B"].width = 24

    row_cursor = 1
    summary_ws.cell(row=row_cursor, column=1, value=title or "Cost Breakdown — NIT Mirror").font = Font(size=14, bold=True)
    row_cursor += 1
    if tender_id:
        summary_ws.cell(row=row_cursor, column=1, value="Tender ID").font = BOLD
        summary_ws.cell(row=row_cursor, column=2, value=int(tender_id))
        row_cursor += 1
    snap = (strategic_summary or {}).get("tender_snapshot") or {}
    for label, key in (
        ("Tender No", "tender_no"),
        ("Scope", "scope_one_liner"),
        ("Period", "period"),
        ("Tender Value (₹)", "tender_value_inr"),
        ("EMD (₹)", "emd_inr"),
        ("Performance Guarantee", "performance_guarantee"),
        ("Bid Validity (days)", "bid_validity_days"),
    ):
        val = snap.get(key)
        if val in (None, ""):
            continue
        summary_ws.cell(row=row_cursor, column=1, value=label).font = BOLD
        summary_ws.cell(row=row_cursor, column=2, value=val)
        row_cursor += 1

    row_cursor += 1
    summary_ws.cell(row=row_cursor, column=1, value="Commercial knobs (edit in place)").font = BOLD
    row_cursor += 1
    # Named cells for the three commercial knobs.
    knob_cells: dict[str, str] = {}
    for label, key, default in (
        ("GST %", "NIT_GST_PCT", gst_pct),
    ):
        summary_ws.cell(row=row_cursor, column=1, value=label).font = BOLD
        cell = summary_ws.cell(row=row_cursor, column=2, value=float(default))
        cell.fill = EDITABLE_FILL
        cell.border = BORDER
        coord = f"Summary!${get_column_letter(2)}${row_cursor}"
        wb.defined_names[key] = DefinedName(name=key, attr_text=coord)
        knob_cells[key] = coord
        row_cursor += 1

    row_cursor += 1
    schedule_totals_header_row = row_cursor
    summary_ws.cell(row=row_cursor, column=1, value="Schedule").font = BOLD
    summary_ws.cell(row=row_cursor, column=2, value="Schedule Total (₹)").font = BOLD
    row_cursor += 1
    schedule_totals_start_row = row_cursor

    groups = _group_rows_by_schedule_name(rows)
    # Sort: real schedule codes alphabetically, "?" bucket last.
    sorted_schedule_names = sorted(
        groups.keys(),
        key=lambda s: (s == "?", s),
    )

    # We need to know each schedule sheet's total cell to reference from
    # Summary. Build them as we create the sheets, then come back and write
    # the references into Summary.
    per_schedule_total_refs: dict[str, str] = {}

    # -- One sheet per schedule --
    for sched in sorted_schedule_names:
        sched_rows = groups[sched]
        sheet_title = (f"Schedule {sched}" if sched != "?" else "Other Items")[:31]
        ws = wb.create_sheet(title=sheet_title)

        for i, (_, _, width) in enumerate(NIT_COLUMNS, start=1):
            ws.column_dimensions[get_column_letter(i)].width = width

        # Title banner.
        ws.cell(row=1, column=1, value=f"{title} — {sheet_title}").font = Font(size=12, bold=True)

        # Header row.
        header_row = 3
        for i, (_, label, _) in enumerate(NIT_COLUMNS, start=1):
            c = ws.cell(row=header_row, column=i, value=label)
            c.fill = HEADER_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = BORDER

        # Data rows.
        col_keys = [k for k, _, _ in NIT_COLUMNS]
        qty_col = get_column_letter(col_keys.index("quantity") + 1)
        tender_rate_col = get_column_letter(col_keys.index("tender_rate") + 1)
        escl_col = get_column_letter(col_keys.index("escalation_pct") + 1)
        rate_col = get_column_letter(col_keys.index("rate") + 1)
        data_start = header_row + 1
        for offset, r in enumerate(sched_rows):
            xl_row = data_start + offset
            is_tax = bool(r.get("is_tax_line"))
            for i, (key, _, _) in enumerate(NIT_COLUMNS, start=1):
                if key == "sr_no":
                    val = r.get("sr_no") or (offset + 1)
                elif key == "tender_amount":
                    # Live formula: Qty × NIT Unit Rate × (1 + Escl%/100). This
                    # is the tender's OWN amount — never the firm's estimate.
                    if is_tax:
                        # Tax rows: amount left blank — total is rolled up
                        # below via formula referring to the previous schedule.
                        val = None
                    else:
                        val = (
                            f"=IFERROR({qty_col}{xl_row}*{tender_rate_col}{xl_row}"
                            f"*(1+{escl_col}{xl_row}/100),0)"
                        )
                elif key == "amount":
                    # Est. Amount — the firm's own cost estimate: Qty × Est. Rate.
                    if is_tax:
                        val = None
                    else:
                        val = f"=IFERROR({qty_col}{xl_row}*{rate_col}{xl_row},0)"
                else:
                    val = r.get(key)
                    if key in ("tender_rate", "rate") and val is None and not is_tax:
                        # An empty rate would break the Amount formula —
                        # set 0 so the user sees something to edit.
                        val = 0
                    if key == "escalation_pct" and val is None:
                        val = 0
                cell = ws.cell(row=xl_row, column=i, value=val)
                cell.border = BORDER
                if key in ("tender_rate", "rate", "escalation_pct") and not is_tax:
                    cell.fill = EDITABLE_FILL
                if is_tax:
                    cell.fill = TAX_FILL
                if key in ("quantity", "tender_rate", "rate", "basic_value", "tender_amount", "amount"):
                    cell.number_format = '#,##0.00'

        data_end = data_start + len(sched_rows) - 1

        # Schedule total row — sums the NIT's own Tender Amount column (this
        # sheet mirrors the tender's published Schedule of Items).
        amount_col_idx = [k for k, _, _ in NIT_COLUMNS].index("tender_amount") + 1
        amount_col_letter = get_column_letter(amount_col_idx)
        total_row = data_end + 2
        ws.cell(row=total_row, column=1, value=f"Schedule {sched} Total").font = BOLD
        total_cell = ws.cell(
            row=total_row,
            column=amount_col_idx,
            value=f"=SUM({amount_col_letter}{data_start}:{amount_col_letter}{data_end})",
        )
        total_cell.font = BOLD
        total_cell.fill = TOTAL_FILL
        total_cell.number_format = '#,##0.00'
        total_cell.border = BORDER
        per_schedule_total_refs[sched] = f"'{sheet_title}'!{amount_col_letter}{total_row}"

    # -- Back to Summary: per-schedule totals + grand total --
    for sched in sorted_schedule_names:
        summary_ws.cell(row=row_cursor, column=1, value=f"Schedule {sched}" if sched != "?" else "Other Items")
        ref = per_schedule_total_refs.get(sched)
        if ref:
            cell = summary_ws.cell(row=row_cursor, column=2, value=f"={ref}")
            cell.number_format = '#,##0.00'
            cell.font = BOLD
        row_cursor += 1

    schedule_totals_end_row = row_cursor - 1
    row_cursor += 1
    summary_ws.cell(row=row_cursor, column=1, value="Grand Total (pre-GST)").font = Font(bold=True, size=12)
    grand_cell = summary_ws.cell(
        row=row_cursor,
        column=2,
        value=(
            f"=SUM(B{schedule_totals_start_row}:B{schedule_totals_end_row})"
            if schedule_totals_end_row >= schedule_totals_start_row
            else 0
        ),
    )
    grand_cell.number_format = '#,##0.00'
    grand_cell.font = Font(bold=True, size=12)
    grand_cell.fill = TOTAL_FILL
    grand_row = row_cursor
    row_cursor += 1
    summary_ws.cell(row=row_cursor, column=1, value="+ GST").alignment = Alignment(indent=1)
    summary_ws.cell(
        row=row_cursor, column=2,
        value=f"=B{grand_row}*NIT_GST_PCT/100",
    ).number_format = '#,##0.00'
    gst_row = row_cursor
    row_cursor += 1
    summary_ws.cell(row=row_cursor, column=1, value="Bid Total (with GST)").font = Font(bold=True, size=12)
    bid_total_cell = summary_ws.cell(
        row=row_cursor, column=2,
        value=f"=B{grand_row}+B{gst_row}",
    )
    bid_total_cell.font = Font(bold=True, size=12)
    bid_total_cell.fill = TOTAL_FILL
    bid_total_cell.number_format = '#,##0.00'

    # Add header for the Schedule totals block we filled above.
    summary_ws.cell(row=schedule_totals_header_row, column=1).fill = HEADER_FILL
    summary_ws.cell(row=schedule_totals_header_row, column=1).font = HEADER_FONT
    summary_ws.cell(row=schedule_totals_header_row, column=2).fill = HEADER_FILL
    summary_ws.cell(row=schedule_totals_header_row, column=2).font = HEADER_FONT

    # -- Reconciliation (Task 5's stated-total vs line-sum gate) --
    row_cursor += 2
    _write_reconciliation_block(
        summary_ws,
        row_cursor,
        breakdown_meta.get("reconciliation"),
        header_fill=HEADER_FILL,
        header_font=HEADER_FONT,
        bold_font=BOLD,
        border=BORDER,
    )

    wb.save(path)

    # Return totals + row count (matches the contract of the other builders).
    n_priced = sum(1 for r in rows if not r.get("is_tax_line"))
    n_tax = sum(1 for r in rows if r.get("is_tax_line"))
    return {
        "title": title,
        "row_count": len(rows),
        "priced_row_count": n_priced,
        "tax_row_count": n_tax,
        "schedule_count": len(groups),
        "layout": "nit_mirror",
    }


def _build_nit_mirror_single_sheet(
    path: str,
    title: str,
    rows: list[dict],
    *,
    breakdown_meta: Optional[dict] = None,
    strategic_summary: Optional[dict] = None,
) -> dict:
    """NIT-mirror builder — SINGLE-SHEET variant.

    Same IREPS "Schedule of Items" shape as `_build_nit_mirror`, but stacks
    EVERY schedule into ONE worksheet/tab: for each schedule a styled banner
    row → column header → data rows → per-schedule total, repeated in NIT order,
    then a grand total + overhead/margin/GST roll-up. Commercial knobs live as
    editable named cells (NIT_OVERHEAD_PCT/MARGIN_PCT/GST_PCT) in a small header
    block at the top of the same sheet (no separate Summary tab). This is the
    "exact NIT representation in a single tab" export requested by the user; the
    multi-sheet `_build_nit_mirror` is unchanged and remains the default.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName

    breakdown_meta = breakdown_meta or {}
    overhead_pct = float(breakdown_meta.get("overhead_percent", 10.0) or 0.0)
    margin_pct = float(breakdown_meta.get("margin_percent", 15.0) or 0.0)
    gst_pct = float(breakdown_meta.get("gst_percent", 18.0) or 0.0)
    tender_id = breakdown_meta.get("tender_id")

    # NIT column order — matches the IREPS Schedule of Items table (same as the
    # multi-sheet builder). "Unit Rate"/"Tender Amount" mirror the NIT's OWN
    # published numbers (tender_rate/tender_amount); the firm's own cost
    # estimate is a separate "Est. Rate"/"Est. Amount" pair.
    NIT_COLUMNS = [
        ("sr_no",         "S.No.",        8),
        ("item_code",     "Item Code",    12),
        ("description",   "Description", 60),
        ("quantity",      "Item Qty",    12),
        ("unit",          "Qty Unit",    14),
        ("tender_rate",   "Unit Rate",   14),
        ("basic_value",   "Basic Value", 16),
        ("escalation_pct", "Escl.(%)",    10),
        ("tender_amount", "Tender Amount", 16),
        ("rate",          "Est. Rate",   14),
        ("amount",        "Est. Amount", 16),
        ("bidding_unit",  "Bidding Unit", 14),
    ]
    n_cols = len(NIT_COLUMNS)
    col_keys = [k for k, _, _ in NIT_COLUMNS]
    qty_idx = col_keys.index("quantity") + 1
    tender_rate_idx = col_keys.index("tender_rate") + 1
    rate_idx = col_keys.index("rate") + 1
    escl_idx = col_keys.index("escalation_pct") + 1
    tender_amount_idx = col_keys.index("tender_amount") + 1
    amount_idx = col_keys.index("amount") + 1
    qty_col = get_column_letter(qty_idx)
    tender_rate_col = get_column_letter(tender_rate_idx)
    rate_col = get_column_letter(rate_idx)
    escl_col = get_column_letter(escl_idx)
    tender_amount_col = get_column_letter(tender_amount_idx)
    amount_col = get_column_letter(amount_idx)

    HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
    BANNER_FILL = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    BANNER_FONT = Font(color="FFFFFF", bold=True, size=12)
    EDITABLE_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    TAX_FILL = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid")
    TOTAL_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    BOLD = Font(bold=True)
    THIN = Side(style="thin", color="9E9E9E")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    wb = Workbook()
    ws = wb.active
    sheet_name = "Schedule of Items"
    ws.title = sheet_name
    for i, (_, _, width) in enumerate(NIT_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    r = 1
    ws.cell(row=r, column=1, value=title or "Cost Breakdown — NIT Mirror").font = Font(size=14, bold=True)
    r += 1
    if tender_id:
        ws.cell(row=r, column=1, value="Tender ID").font = BOLD
        ws.cell(row=r, column=2, value=int(tender_id))
        r += 1
    snap = (strategic_summary or {}).get("tender_snapshot") or {}
    tno = snap.get("tender_no")
    if tno:
        ws.cell(row=r, column=1, value="Tender No").font = BOLD
        ws.cell(row=r, column=2, value=tno)
        r += 1

    # Editable commercial knobs (named cells) — inline, no separate tab.
    r += 1
    ws.cell(row=r, column=1, value="Commercial knobs (edit in place)").font = BOLD
    r += 1
    for label, key, default in (
        ("GST %", "NIT_GST_PCT", gst_pct),
    ):
        ws.cell(row=r, column=1, value=label).font = BOLD
        cell = ws.cell(row=r, column=2, value=float(default))
        cell.fill = EDITABLE_FILL
        cell.border = BORDER
        coord = f"'{sheet_name}'!${get_column_letter(2)}${r}"
        wb.defined_names[key] = DefinedName(name=key, attr_text=coord)
        r += 1

    r += 1  # spacer before the first schedule

    groups = _group_rows_by_schedule_name(rows)
    sorted_schedule_names = sorted(groups.keys(), key=lambda s: (s == "?", s))
    per_schedule_total_rows: list[int] = []

    for sched in sorted_schedule_names:
        sched_rows = groups[sched]

        # Schedule banner row (merged across all NIT columns).
        if sched != "?":
            banner_label = f"Schedule {sched}"
        elif _is_component_group(sched_rows):
            banner_label = (
                "Annexure components (per set — already inside their schedule "
                "item's Est. Rate; not added to the totals)"
            )
        else:
            banner_label = "Other Items"
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n_cols)
        bcell = ws.cell(row=r, column=1, value=banner_label)
        bcell.fill = BANNER_FILL
        bcell.font = BANNER_FONT
        bcell.alignment = Alignment(horizontal="left", vertical="center")
        r += 1

        # Column-header row.
        for i, (_, label, _) in enumerate(NIT_COLUMNS, start=1):
            c = ws.cell(row=r, column=i, value=label)
            c.fill = HEADER_FILL
            c.font = HEADER_FONT
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = BORDER
        r += 1

        # Data rows.
        data_start = r
        for offset, row in enumerate(sched_rows):
            is_tax = bool(row.get("is_tax_line"))
            xl_row = r
            for i, (key, _, _) in enumerate(NIT_COLUMNS, start=1):
                if key == "sr_no":
                    val = row.get("sr_no") or (offset + 1)
                elif key == "tender_amount":
                    # Live formula: Qty × NIT Unit Rate × (1 + Escl%/100). This
                    # is the tender's OWN amount — never the firm's estimate.
                    if is_tax:
                        val = None
                    else:
                        val = (
                            f"=IFERROR({qty_col}{xl_row}*{tender_rate_col}{xl_row}"
                            f"*(1+{escl_col}{xl_row}/100),0)"
                        )
                elif key == "amount":
                    # Est. Amount — the firm's own cost estimate: Qty × Est. Rate.
                    if is_tax:
                        val = None
                    else:
                        val = f"=IFERROR({qty_col}{xl_row}*{rate_col}{xl_row},0)"
                else:
                    val = row.get(key)
                    if key in ("tender_rate", "rate") and val is None and not is_tax:
                        val = 0
                    if key == "escalation_pct" and val is None:
                        val = 0
                cell = ws.cell(row=xl_row, column=i, value=val)
                cell.border = BORDER
                if key in ("tender_rate", "rate", "escalation_pct") and not is_tax:
                    cell.fill = EDITABLE_FILL
                if is_tax:
                    cell.fill = TAX_FILL
                if key in ("quantity", "tender_rate", "rate", "basic_value", "tender_amount", "amount"):
                    cell.number_format = '#,##0.00'
            r += 1
        data_end = r - 1

        # Per-schedule total row — sums the NIT's own Tender Amount column
        # (this sheet mirrors the tender's published Schedule of Items).
        ws.cell(row=r, column=1, value=f"Schedule {sched} Total").font = BOLD
        tcell = ws.cell(
            row=r,
            column=tender_amount_idx,
            value=(
                f"=SUM({tender_amount_col}{data_start}:{tender_amount_col}{data_end})"
                if data_end >= data_start else 0
            ),
        )
        tcell.font = BOLD
        tcell.fill = TOTAL_FILL
        tcell.number_format = '#,##0.00'
        tcell.border = BORDER
        per_schedule_total_rows.append(r)
        r += 2  # total row + spacer

    # Grand total + overhead / margin / GST roll-up — sums the NIT's own
    # Tender Amount column (this sheet mirrors the tender's published
    # Schedule of Items, not the firm's cost estimate).
    ws.cell(row=r, column=1, value="Grand Total (pre-GST)").font = Font(bold=True, size=12)
    if per_schedule_total_rows:
        grand_val = "=" + "+".join(f"{tender_amount_col}{tr}" for tr in per_schedule_total_rows)
    else:
        grand_val = 0
    gcell = ws.cell(row=r, column=tender_amount_idx, value=grand_val)
    gcell.font = Font(bold=True, size=12)
    gcell.fill = TOTAL_FILL
    gcell.number_format = '#,##0.00'
    grand_row = r
    r += 1
    ws.cell(row=r, column=1, value="+ GST").alignment = Alignment(indent=1)
    ws.cell(
        row=r, column=tender_amount_idx,
        value=f"={tender_amount_col}{grand_row}*NIT_GST_PCT/100",
    ).number_format = '#,##0.00'
    gst_row = r
    r += 1
    ws.cell(row=r, column=1, value="Bid Total (with GST)").font = Font(bold=True, size=12)
    btcell = ws.cell(
        row=r, column=tender_amount_idx,
        value=f"={tender_amount_col}{grand_row}+{tender_amount_col}{gst_row}",
    )
    btcell.font = Font(bold=True, size=12)
    btcell.fill = TOTAL_FILL
    btcell.number_format = '#,##0.00'
    r += 1

    # -- Reconciliation (Task 5's stated-total vs line-sum gate) --
    r += 1
    _write_reconciliation_block(
        ws,
        r,
        breakdown_meta.get("reconciliation"),
        header_fill=HEADER_FILL,
        header_font=HEADER_FONT,
        bold_font=BOLD,
        border=BORDER,
    )

    wb.save(path)

    n_priced = sum(1 for x in rows if not x.get("is_tax_line"))
    n_tax = sum(1 for x in rows if x.get("is_tax_line"))
    return {
        "title": title,
        "row_count": len(rows),
        "priced_row_count": n_priced,
        "tax_row_count": n_tax,
        "schedule_count": len(groups),
        "layout": "nit_mirror_single",
    }


def _build_single_sheet(
    path: str,
    title: str,
    rows: list[dict],
    *,
    breakdown_meta: Optional[dict] = None,
) -> dict:
    """Single-sheet builder with LIVE FORMULAS.

    Layout:
      - Row 1: editable GST % parameter cell with named range GST_PCT.
      - Row 5: title banner.
      - Row 6: column header.
      - Row 7+: data rows. The Amount cell (= Qty × Rate) is a formula.
      - Footer block: Subtotal / GST / Grand Total — chained formulas so
        editing a rate or the GST % recomputes the total. No margin/overhead.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.workbook.defined_name import DefinedName

    breakdown_meta = breakdown_meta or {}
    gst_pct = float(breakdown_meta.get("gst_percent", 18.0) or 0.0)

    columns = _COST_COLUMNS
    n_cols = len(columns)
    col_pos = {key: i + 1 for i, (key, _, _) in enumerate(columns)}

    wb = Workbook()
    ws = wb.active
    sheet_title = (title or "Cost Breakdown")[:31]
    ws.title = sheet_title

    # ---- Styles ----
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    title_font = Font(bold=True, size=14)
    totals_font = Font(bold=True, size=11)
    totals_fill = PatternFill("solid", fgColor="DDEBF7")
    grand_fill = PatternFill("solid", fgColor="C6E0B4")
    param_label_font = Font(bold=True, color="7F6000")
    param_value_fill = PatternFill("solid", fgColor="FFF2CC")
    border_thin = Border(
        left=Side(style="thin", color="BFBFBF"),
        right=Side(style="thin", color="BFBFBF"),
        top=Side(style="thin", color="BFBFBF"),
        bottom=Side(style="thin", color="BFBFBF"),
    )
    border_param = Border(
        left=Side(style="medium", color="BF9000"),
        right=Side(style="medium", color="BF9000"),
        top=Side(style="medium", color="BF9000"),
        bottom=Side(style="medium", color="BF9000"),
    )

    # ---- Rows 1-3: editable parameter block (yellow fill = "edit me") ----
    param_rows = [
        ("GST %", gst_pct, "GST_PCT"),
    ]
    for i, (label, val, name) in enumerate(param_rows, start=1):
        lcell = ws.cell(row=i, column=1, value=label)
        lcell.font = param_label_font
        lcell.alignment = Alignment(horizontal="right", vertical="center")
        vcell = ws.cell(row=i, column=2, value=val)
        vcell.fill = param_value_fill
        vcell.font = Font(bold=True)
        vcell.alignment = Alignment(horizontal="right", vertical="center")
        vcell.number_format = "0.00"
        vcell.border = border_param
        # Workbook-level named range pointing to this single cell.
        cell_ref = f"'{sheet_title}'!${get_column_letter(2)}${i}"
        wb.defined_names[name] = DefinedName(name=name, attr_text=cell_ref)
    # Helper note for user
    note_cell = ws.cell(row=2, column=4, value=(
        "↑ Edit the yellow GST % cell to change the tax rate. "
        "Line Amounts and totals recompute automatically."
    ))
    note_cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    note_cell.font = Font(italic=True, color="595959", size=9)
    ws.merge_cells(start_row=2, start_column=4, end_row=2, end_column=n_cols)

    # ---- Row 5: title banner ----
    ws.cell(row=5, column=1, value=title).font = title_font
    ws.merge_cells(start_row=5, start_column=1, end_row=5, end_column=n_cols)

    # ---- Row 6: header ----
    header_row = 6
    for idx, (_, label, width) in enumerate(columns, start=1):
        cell = ws.cell(row=header_row, column=idx, value=label)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border_thin
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.row_dimensions[header_row].height = 26

    # ---- Row 7+: data rows with formulas ----
    first_data_row = header_row + 1
    qty_col = get_column_letter(col_pos["qty"])
    rate_col = get_column_letter(col_pos["rate"])
    amount_col = get_column_letter(col_pos["amount"])

    sum_amount_static = 0.0  # only used for the JSON return payload

    for r_offset, row in enumerate(rows):
        r_idx = first_data_row + r_offset
        for c_idx, (key, _, _) in enumerate(columns, start=1):
            raw = row.get(key)
            if key == "sr_no":
                value = raw or (r_offset + 1)
                cell = ws.cell(row=r_idx, column=c_idx, value=value)
                cell.alignment = Alignment(horizontal="center")
            elif key == "amount":
                # Formula: amount = qty × rate
                formula = f"={qty_col}{r_idx}*{rate_col}{r_idx}"
                cell = ws.cell(row=r_idx, column=c_idx, value=formula)
                cell.number_format = '"₹"#,##0.00'
                cell.alignment = Alignment(horizontal="right")
            elif key in _CURRENCY_COLS or key == "qty":
                cell = ws.cell(row=r_idx, column=c_idx, value=_coerce(raw, key))
                cell.number_format = '"₹"#,##0.00' if key in _CURRENCY_COLS else "#,##0.00"
                cell.alignment = Alignment(horizontal="right")
            elif key == "source_url":
                from openpyxl.styles import Font as _Font
                url = (raw or "").strip()
                cell = ws.cell(row=r_idx, column=c_idx, value=(url or None))
                if url:
                    cell.hyperlink = url
                    cell.font = _Font(color="0563C1", underline="single")
                cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            else:
                cell = ws.cell(row=r_idx, column=c_idx, value=_coerce(raw, key))
                cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
            cell.border = border_thin

        # Track static subtotal for the JSON return payload (Excel uses formulas)
        amt = _safe_num(row.get("amount"))
        if amt == 0 and row.get("rate") not in (None, ""):
            amt = _safe_num(row.get("qty")) * _safe_num(row.get("rate"))
        sum_amount_static += amt

    last_data_row = first_data_row + len(rows) - 1
    if last_data_row < first_data_row:
        # No data rows — point the SUM at the header row to avoid #REF! later.
        last_data_row = first_data_row

    # ---- Footer formula chain ----
    footer_start = first_data_row + len(rows)
    sum_amount_range = f"{amount_col}{first_data_row}:{amount_col}{last_data_row}"

    label_col_count = max(col_pos["amount"] - 1, 1)

    def _emit_footer(row_idx: int, label: str, formula: str, fill, font_extra=None):
        """Write a footer row: label (merged) + formula in the Amount column."""
        ws.cell(row=row_idx, column=1, value=label)
        if label_col_count > 1:
            ws.merge_cells(
                start_row=row_idx, start_column=1,
                end_row=row_idx, end_column=label_col_count,
            )
        cell = ws.cell(row=row_idx, column=col_pos["amount"], value=formula)
        cell.number_format = '"₹"#,##0.00'
        cell.alignment = Alignment(horizontal="right")
        for c in range(1, n_cols + 1):
            fc = ws.cell(row=row_idx, column=c)
            fc.font = font_extra or totals_font
            fc.fill = fill
            fc.border = border_thin
        # Re-apply right alignment on the formula cell after font/fill loop
        cell.alignment = Alignment(horizontal="right")

    # Cell refs for chained formulas
    subtotal_cell = f"{amount_col}{footer_start}"
    gst_cell = f"{amount_col}{footer_start + 1}"

    _emit_footer(footer_start, "SUBTOTAL (sum of line amounts)",
                 f"=SUM({sum_amount_range})", totals_fill)
    _emit_footer(footer_start + 1, "GST (= Subtotal × GST %)",
                 f"={subtotal_cell}*GST_PCT/100", totals_fill)
    _emit_footer(footer_start + 2, "GRAND TOTAL (= Subtotal + GST)",
                 f"={subtotal_cell}+{gst_cell}", grand_fill,
                 font_extra=Font(bold=True, size=12))

    # Reconciliation (breakdown-time gate) — surface the per-schedule stated
    # total vs line-sum table on the legacy single-sheet layout too. No-op when
    # there is no reconciliation report.
    _write_reconciliation_block(
        ws,
        footer_start + 4,
        breakdown_meta.get("reconciliation"),
        header_fill=header_fill,
        header_font=header_font,
        bold_font=totals_font,
        border=border_thin,
    )

    # Freeze parameter block + header
    ws.freeze_panes = "A7"

    wb.save(path)

    # Roll-up payload (static — for API response. Excel uses formulas.)
    gst_amt = sum_amount_static * (gst_pct / 100.0)
    grand_total = sum_amount_static + gst_amt

    return {
        "row_count": len(rows),
        "layout": "single_sheet_formula",
        "total_amount": round(sum_amount_static, 2),
        "total_with_gst": round(grand_total, 2),
        "gst_percent": gst_pct,
    }


def _coerce(val: Any, key: str) -> Any:
    """Numeric-ish columns → float; others → string or original."""
    if val is None:
        return None
    if key in _CURRENCY_COLS or key in _PERCENT_COLS or key == "qty":
        return _safe_num(val)
    return val if isinstance(val, (int, float)) else str(val)


def _safe_num(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        # Strip currency symbols, commas
        s = str(val).replace("₹", "").replace(",", "").replace("Rs.", "").replace("Rs", "").strip()
        return float(s)
    except (TypeError, ValueError):
        return 0.0


# -----------------------------------------------------------------------------
# Phase 3b — multi-sheet builder for margin-analysis mode (matches reference
# Excel layout: Summary | Cost Assumptions | Schedule X Costing).
# -----------------------------------------------------------------------------

def _build_multi_sheet(
    path: str,
    title: str,
    rows: list[dict],
    *,
    cost_assumptions: list[dict],
    strategic_summary: dict,
    breakdown_meta: dict,
    single_sheet: bool = False,
) -> dict:
    """Render the multi-sheet margin-analysis workbook. Mirrors the user's
    NFR Lumding reference: Sheet 1 (snapshot + schedule profitability + obs),
    Sheet 2 (rate-card library), Sheet 3+ (per-schedule line-item sheets).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    # Default first sheet — we'll repurpose it as the Summary sheet.
    summary_ws = wb.active
    summary_ws.title = "1. Summary"

    title_font = Font(bold=True, size=14)
    section_font = Font(bold=True, size=12, color="1F4E78")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="1F4E78")
    sub_header_fill = PatternFill("solid", fgColor="DDEBF7")
    totals_font = Font(bold=True, size=11)
    totals_fill = PatternFill("solid", fgColor="DDEBF7")
    border_thin = Border(
        left=Side(style="thin", color="BFBFBF"),
        right=Side(style="thin", color="BFBFBF"),
        top=Side(style="thin", color="BFBFBF"),
        bottom=Side(style="thin", color="BFBFBF"),
    )
    neg_font = Font(bold=True, color="C00000")  # red for negative margins

    def _style_data_cell(cell, key: str):
        cell.border = border_thin
        if key in _CURRENCY_COLS:
            cell.number_format = '"₹"#,##0.00'
            cell.alignment = Alignment(horizontal="right")
        elif key in _PERCENT_COLS:
            cell.number_format = "0.0"
            cell.alignment = Alignment(horizontal="right")
        elif key == "qty":
            cell.number_format = "#,##0.00"
            cell.alignment = Alignment(horizontal="right")
        elif key == "sr_no":
            cell.alignment = Alignment(horizontal="center")
        else:
            cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)

    # ---------------------------------------------------------------- Sheet 1
    ws = summary_ws
    ws.cell(row=1, column=1, value=title).font = title_font
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=6)
    for col_idx, w in enumerate([22, 28, 22, 22, 22, 22], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = w

    snap = (strategic_summary or {}).get("tender_snapshot") or {}
    row = 3
    if snap:
        ws.cell(row=row, column=1, value="TENDER SNAPSHOT").font = section_font
        row += 1
        snap_pairs = [
            ("Tender No.", snap.get("tender_no")),
            ("Scope", snap.get("scope_one_liner")),
            ("Tender Value (₹)", snap.get("tender_value_inr")),
            ("Period", snap.get("period")),
            ("Depots / Locations", ", ".join(snap.get("depots_or_locations") or []) or None),
            ("EMD (₹)", snap.get("emd_inr")),
            ("Performance Guarantee", snap.get("performance_guarantee")),
            ("Bid Validity (days)", snap.get("bid_validity_days")),
            ("Penalty Cap %", snap.get("penalty_cap_pct_of_contract")),
            ("Min. Eligibility", "; ".join(snap.get("min_eligibility") or []) or None),
        ]
        for label, val in snap_pairs:
            if val in (None, "", 0) and not isinstance(val, bool):
                continue
            lcell = ws.cell(row=row, column=1, value=label)
            lcell.font = Font(bold=True)
            lcell.border = border_thin
            vcell = ws.cell(row=row, column=2, value=val)
            vcell.border = border_thin
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=6)
            if label.endswith("(₹)"):
                vcell.number_format = '"₹"#,##0.00'
                vcell.alignment = Alignment(horizontal="right")
            else:
                vcell.alignment = Alignment(horizontal="left", wrap_text=True)
            row += 1
        row += 1

    schedules = (strategic_summary or {}).get("schedule_breakdown") or []
    if schedules:
        ws.cell(row=row, column=1, value="SCHEDULE-WISE PROFITABILITY").font = section_font
        row += 1
        sched_headers = ["Schedule", "Tender Value (₹)", "Estimated Cost (₹)", "Gross Margin (₹)", "GM %"]
        for ci, h in enumerate(sched_headers, start=1):
            cell = ws.cell(row=row, column=ci, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = border_thin
        row += 1
        sum_tv = sum_ec = sum_gm = 0.0
        for s in schedules:
            tv = _safe_num(s.get("tender_value_inr"))
            ec = _safe_num(s.get("estimated_cost_inr"))
            gm = s.get("gross_margin_inr")
            gm = _safe_num(gm) if gm is not None else (tv - ec)
            gp = s.get("gross_margin_pct")
            gp = _safe_num(gp) if gp is not None else ((gm / tv * 100.0) if tv else 0.0)
            sum_tv += tv
            sum_ec += ec
            sum_gm += gm
            cells = [
                (s.get("schedule") or "", None),
                (tv, '"₹"#,##0.00'),
                (ec, '"₹"#,##0.00'),
                (gm, '"₹"#,##0.00'),
                (gp, "0.0"),
            ]
            for ci, (val, fmt) in enumerate(cells, start=1):
                cell = ws.cell(row=row, column=ci, value=val)
                cell.border = border_thin
                if fmt:
                    cell.number_format = fmt
                    cell.alignment = Alignment(horizontal="right")
            row += 1
        # Total row
        if len(schedules) > 1:
            total_pct = (sum_gm / sum_tv * 100.0) if sum_tv else 0.0
            total_cells = [
                ("TOTAL CONTRACT", None),
                (sum_tv, '"₹"#,##0.00'),
                (sum_ec, '"₹"#,##0.00'),
                (sum_gm, '"₹"#,##0.00'),
                (total_pct, "0.0"),
            ]
            for ci, (val, fmt) in enumerate(total_cells, start=1):
                cell = ws.cell(row=row, column=ci, value=val)
                cell.font = totals_font
                cell.fill = totals_fill
                cell.border = border_thin
                if fmt:
                    cell.number_format = fmt
                    cell.alignment = Alignment(horizontal="right")
            row += 1
        row += 1

    # Reconciliation (breakdown-time gate) — reuse the shared renderer so this
    # layout shows the same per-schedule stated-total vs line-sum table the
    # NIT-mirror layouts already show. No-op when no reconciliation report.
    reconciliation = (breakdown_meta or {}).get("reconciliation")
    if reconciliation:
        row = _write_reconciliation_block(
            summary_ws,
            row,
            reconciliation,
            header_fill=header_fill,
            header_font=header_font,
            bold_font=totals_font,
            border=border_thin,
        )
        row += 1

    obs = (strategic_summary or {}).get("key_observations") or []
    if obs:
        ws.cell(row=row, column=1, value="KEY OBSERVATIONS & STRATEGY").font = section_font
        row += 1
        for o in obs:
            ws.cell(row=row, column=1, value=f"• {o}").alignment = Alignment(horizontal="left", wrap_text=True)
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
            ws.row_dimensions[row].height = 28
            row += 1
        row += 1

    bid = (strategic_summary or {}).get("recommended_bid_strategy")
    if bid:
        cell = ws.cell(row=row, column=1, value=f"Recommended bid: {bid}")
        cell.font = Font(bold=True, italic=True, color="1F4E78")
        cell.alignment = Alignment(horizontal="left", wrap_text=True)
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        ws.row_dimensions[row].height = 32

    # ---------------------------------------------------------------- Sheet 2
    if cost_assumptions:
        ca_ws = wb.create_sheet(title="2. Cost Assumptions")
        ca_ws.cell(row=1, column=1, value="COST ASSUMPTIONS — Rate-Card Library").font = title_font
        ca_ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=5)
        # Group entries by section
        sections: dict[str, list[dict]] = {}
        section_order: list[str] = []
        for entry in cost_assumptions:
            sec = (entry.get("section") or "Other").strip() or "Other"
            if sec not in sections:
                sections[sec] = []
                section_order.append(sec)
            sections[sec].append(entry)

        widths = [50, 14, 12, 50]
        for col_idx, w in enumerate(widths, start=1):
            ca_ws.column_dimensions[get_column_letter(col_idx)].width = w
        ca_row = 3
        for sec in section_order:
            cell = ca_ws.cell(row=ca_row, column=1, value=sec)
            cell.font = section_font
            cell.fill = sub_header_fill
            ca_ws.merge_cells(start_row=ca_row, start_column=1, end_row=ca_row, end_column=4)
            ca_row += 1
            # Sub-header
            for ci, h in enumerate(["Item / Component", "Cost (₹)", "UoM", "Source / remark"], start=1):
                cell = ca_ws.cell(row=ca_row, column=ci, value=h)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = border_thin
            ca_row += 1
            for entry in sections[sec]:
                vals = [
                    entry.get("item") or "",
                    _safe_num(entry.get("rate_inr")) if entry.get("rate_inr") not in (None, "") else None,
                    entry.get("uom") or "",
                    entry.get("source_ref") or "",
                ]
                for ci, val in enumerate(vals, start=1):
                    cell = ca_ws.cell(row=ca_row, column=ci, value=val)
                    cell.border = border_thin
                    if ci == 2 and val is not None:
                        cell.number_format = '"₹"#,##0.00'
                        cell.alignment = Alignment(horizontal="right")
                    else:
                        cell.alignment = Alignment(horizontal="left", wrap_text=True, vertical="top")
                ca_row += 1
            ca_row += 1
        ca_ws.freeze_panes = "A3"

    # ---------------------------------------------------------- Sheet 3+ (per-schedule)
    grouped = _group_rows_by_schedule(rows)
    # Filter out groups with no priced rows + ensure stable naming
    schedule_keys = [k for k in grouped.keys() if k]  # non-empty schedule labels
    unscheduled = grouped.get("", [])

    def _render_schedule_sheet(sheet_title: str, schedule_label: str, schedule_rows: list[dict]):
        # Sheet titles capped at 31 chars (Excel limit).
        sw = wb.create_sheet(title=sheet_title[:31])
        n_cols = len(_MARGIN_COLUMNS)
        col_pos = {key: i + 1 for i, (key, _, _) in enumerate(_MARGIN_COLUMNS)}

        # Title row
        sw.cell(row=1, column=1, value=schedule_label).font = title_font
        sw.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n_cols)

        # Header row
        for idx, (_, label, width) in enumerate(_MARGIN_COLUMNS, start=1):
            cell = sw.cell(row=2, column=idx, value=label)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border_thin
            sw.column_dimensions[get_column_letter(idx)].width = width
        sw.row_dimensions[2].height = 28

        # Column letters used in formulas
        qty_l = get_column_letter(col_pos["qty"])
        tr_l = get_column_letter(col_pos["tender_rate"])
        ta_l = get_column_letter(col_pos["tender_amount"])
        rate_l = get_column_letter(col_pos["rate"])
        amt_l = get_column_letter(col_pos["amount"])

        # Data rows — use formulas so the user can edit qty / tender_rate /
        # rate (Est. Unit Cost) and see costs recompute live.
        sum_cost_static = 0.0
        first_data = 3
        for r_idx, row_data in enumerate(schedule_rows, start=first_data):
            for c_idx, (key, _, _) in enumerate(_MARGIN_COLUMNS, start=1):
                if key == "sr_no":
                    cell = sw.cell(row=r_idx, column=c_idx, value=row_data.get("sr_no") or (r_idx - first_data + 1))
                elif key == "tender_amount":
                    formula = f"={qty_l}{r_idx}*{tr_l}{r_idx}"
                    cell = sw.cell(row=r_idx, column=c_idx, value=formula)
                elif key == "amount":
                    formula = f"={qty_l}{r_idx}*{rate_l}{r_idx}"
                    cell = sw.cell(row=r_idx, column=c_idx, value=formula)
                elif key == "source_url":
                    from openpyxl.styles import Font as _Font
                    url = (row_data.get(key) or "").strip()
                    cell = sw.cell(row=r_idx, column=c_idx, value=(url or None))
                    if url:
                        cell.hyperlink = url
                        cell.font = _Font(color="0563C1", underline="single")
                else:
                    cell = sw.cell(row=r_idx, column=c_idx, value=_coerce(row_data.get(key), key))
                _style_data_cell(cell, key)
            sum_cost_static += _safe_num(row_data.get("amount"))

        # Totals footer (formulas)
        footer_row = first_data + len(schedule_rows)
        last_data_row = footer_row - 1
        sw.cell(row=footer_row, column=1, value="TOTAL")
        sw.merge_cells(
            start_row=footer_row, start_column=1,
            end_row=footer_row, end_column=col_pos["tender_amount"] - 1,
        )
        sw.cell(
            row=footer_row, column=col_pos["tender_amount"],
            value=f"=SUM({ta_l}{first_data}:{ta_l}{last_data_row})",
        ).number_format = '"₹"#,##0.00'
        sw.cell(
            row=footer_row, column=col_pos["amount"],
            value=f"=SUM({amt_l}{first_data}:{amt_l}{last_data_row})",
        ).number_format = '"₹"#,##0.00'
        for c in range(1, n_cols + 1):
            cell = sw.cell(row=footer_row, column=c)
            cell.font = totals_font
            cell.fill = totals_fill
            cell.border = border_thin
            if c in (col_pos["tender_amount"], col_pos["amount"]):
                cell.alignment = Alignment(horizontal="right")
        sw.freeze_panes = "A3"

    def _render_consolidated_sheet():
        """Stack EVERY schedule into a single "3. All Schedules" worksheet:
        per schedule a banner row -> header row -> data rows -> per-schedule
        TOTAL, repeated in NIT (input) order, then a GRAND TOTAL summing the
        per-schedule totals. Same _MARGIN_COLUMNS + live formulas as the
        per-tab builder, but every row uses its own ABSOLUTE sheet row index.
        """
        sw = wb.create_sheet(title="3. All Schedules"[:31])
        n_cols = len(_MARGIN_COLUMNS)
        col_pos = {key: i + 1 for i, (key, _, _) in enumerate(_MARGIN_COLUMNS)}

        # Column letters used in formulas (identical to _render_schedule_sheet)
        qty_l = get_column_letter(col_pos["qty"])
        tr_l = get_column_letter(col_pos["tender_rate"])
        ta_l = get_column_letter(col_pos["tender_amount"])
        rate_l = get_column_letter(col_pos["rate"])
        amt_l = get_column_letter(col_pos["amount"])

        # Column widths once for the whole sheet
        for idx, (_, _, width) in enumerate(_MARGIN_COLUMNS, start=1):
            sw.column_dimensions[get_column_letter(idx)].width = width

        # Ordered groups, matching the multi-tab labels/order.
        groups: list[tuple[str, list[dict]]] = []
        if schedule_keys:
            for sec in schedule_keys:
                groups.append((sec, grouped[sec]))
            if unscheduled:
                groups.append(("Unscheduled lines", unscheduled))
        else:
            groups.append((title, rows))

        # Sheet title row
        sw.cell(row=1, column=1, value=title).font = title_font
        sw.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n_cols)

        r = 2
        total_rows_for_grand: list[int] = []
        for label, schedule_rows in groups:
            if not schedule_rows:
                continue

            # Banner row (merged across all columns)
            banner = sw.cell(row=r, column=1, value=label)
            banner.font = section_font
            banner.fill = sub_header_fill
            banner.alignment = Alignment(horizontal="left", vertical="center")
            banner.border = border_thin
            sw.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n_cols)
            r += 1

            # Header row
            for idx, (_, hlabel, _) in enumerate(_MARGIN_COLUMNS, start=1):
                cell = sw.cell(row=r, column=idx, value=hlabel)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = border_thin
            sw.row_dimensions[r].height = 28
            r += 1

            # Data rows — absolute row index r drives the formulas.
            first_data = r
            for n, row_data in enumerate(schedule_rows, start=1):
                for c_idx, (key, _, _) in enumerate(_MARGIN_COLUMNS, start=1):
                    if key == "sr_no":
                        cell = sw.cell(row=r, column=c_idx, value=row_data.get("sr_no") or n)
                    elif key == "tender_amount":
                        cell = sw.cell(row=r, column=c_idx, value=f"={qty_l}{r}*{tr_l}{r}")
                    elif key == "amount":
                        cell = sw.cell(row=r, column=c_idx, value=f"={qty_l}{r}*{rate_l}{r}")
                    elif key == "source_url":
                        from openpyxl.styles import Font as _Font
                        url = (row_data.get(key) or "").strip()
                        cell = sw.cell(row=r, column=c_idx, value=(url or None))
                        if url:
                            cell.hyperlink = url
                            cell.font = _Font(color="0563C1", underline="single")
                    else:
                        cell = sw.cell(row=r, column=c_idx, value=_coerce(row_data.get(key), key))
                    _style_data_cell(cell, key)
                r += 1
            last_data = r - 1

            # Per-schedule TOTAL — SUM only this group's contiguous data rows.
            total_r = r
            sw.cell(row=total_r, column=1, value="TOTAL")
            sw.merge_cells(
                start_row=total_r, start_column=1,
                end_row=total_r, end_column=col_pos["tender_amount"] - 1,
            )
            sw.cell(
                row=total_r, column=col_pos["tender_amount"],
                value=f"=SUM({ta_l}{first_data}:{ta_l}{last_data})",
            ).number_format = '"₹"#,##0.00'
            sw.cell(
                row=total_r, column=col_pos["amount"],
                value=f"=SUM({amt_l}{first_data}:{amt_l}{last_data})",
            ).number_format = '"₹"#,##0.00'
            for c in range(1, n_cols + 1):
                cell = sw.cell(row=total_r, column=c)
                cell.font = totals_font
                cell.fill = totals_fill
                cell.border = border_thin
                if c in (col_pos["tender_amount"], col_pos["amount"]):
                    cell.alignment = Alignment(horizontal="right")
            # An annexure component group's total is a per-set cost already
            # inside its parent item's Est. Unit Cost. It is shown so the
            # build-up can be checked, and kept out of the grand total so it
            # is not counted twice.
            if not _is_component_group(schedule_rows):
                total_rows_for_grand.append(total_r)
            r += 2  # one blank spacer row between schedules

        # Grand total — sum the per-schedule TOTAL cells explicitly (robust to
        # the blank spacer rows; a full-range SUM would double-count the totals).
        if total_rows_for_grand:
            gr = r
            sw.cell(row=gr, column=1, value="GRAND TOTAL")
            sw.merge_cells(
                start_row=gr, start_column=1,
                end_row=gr, end_column=col_pos["tender_amount"] - 1,
            )
            refs_ta = ",".join(f"{ta_l}{tr}" for tr in total_rows_for_grand)
            refs_amt = ",".join(f"{amt_l}{tr}" for tr in total_rows_for_grand)
            sw.cell(
                row=gr, column=col_pos["tender_amount"], value=f"=SUM({refs_ta})",
            ).number_format = '"₹"#,##0.00'
            sw.cell(
                row=gr, column=col_pos["amount"], value=f"=SUM({refs_amt})",
            ).number_format = '"₹"#,##0.00'
            for c in range(1, n_cols + 1):
                cell = sw.cell(row=gr, column=c)
                cell.font = totals_font
                cell.fill = totals_fill
                cell.border = border_thin
                if c in (col_pos["tender_amount"], col_pos["amount"]):
                    cell.alignment = Alignment(horizontal="right")

        sw.freeze_panes = "A2"

    if single_sheet:
        _render_consolidated_sheet()
    elif schedule_keys:
        for i, sec in enumerate(schedule_keys, start=3):
            _render_schedule_sheet(f"{i}. {sec}"[:31], sec, grouped[sec])
        if unscheduled:
            _render_schedule_sheet(
                f"{len(schedule_keys) + 3}. Unscheduled"[:31],
                "Unscheduled lines",
                unscheduled,
            )
    else:
        _render_schedule_sheet("3. Cost Breakdown", title, rows)

    wb.save(path)

    # Roll-up totals for the response payload. Component rows are inside
    # their parent's amount already (see _is_component_group).
    _top = [r for r in rows if not r.get("is_component")]
    total_tender = sum(_safe_num(r.get("tender_amount")) for r in _top)
    total_cost = sum(_safe_num(r.get("amount")) for r in _top)
    total_margin = sum(_safe_num(r.get("margin_amount")) for r in _top)
    return {
        "row_count": len(rows),
        "layout": "multi_sheet_margin_analysis",
        "schedule_count": len(schedule_keys) or 1,
        "total_amount": round(total_cost, 2),
        "total_tender_amount": round(total_tender, 2),
        "total_margin_amount": round(total_margin, 2),
        "total_margin_pct": round(total_margin / total_tender * 100.0, 2) if total_tender else 0.0,
    }


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _persist_workbook_to_storage(local_path: str, file_name: str) -> str:
    """Upload a freshly-built workbook to the shared storage backend and return
    its storage key (``generated_docs/<file_name>``).

    The costing agent runs in the RQ worker container; downloads are served by
    the web container. Persisting only a local temp path meant the web side never
    found the file and regenerated the whole workbook on every download (slow).
    Uploading to storage — the same ``generated_docs/`` prefix the download route
    reads — lets downloads serve the stored bytes directly.
    """
    from app.services.storage_service import get_storage_service

    with open(local_path, "rb") as f:
        data = f.read()
    key = f"generated_docs/{file_name}"
    get_storage_service().upload_file_sync(key, data, content_type=_XLSX_MIME)
    return key


class XlsxGeneratorTool(BaseTool):
    """Generate a cost-breakdown Excel workbook and persist it as an artifact."""
    name: str = "xlsx_generator"
    description: str = (
        "Generate an Excel (.xlsx) cost-breakdown spreadsheet. Input is a list of "
        "rows with keys: description, category, qty, unit, rate, amount, gst_pct, "
        "total. Produces a formatted workbook (bold header, currency columns, "
        "totals row, frozen header) and returns the file path. If session_id is "
        "provided, also creates a 'cost_breakdown_xlsx' artifact attached to that "
        "Command Center session."
    )
    args_schema: Type[BaseModel] = XlsxGeneratorInput

    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        title: str = "Cost Breakdown",
        rows: Optional[list[dict]] = None,
        tender_id: Optional[int] = None,
        session_id: Optional[int] = None,
    ) -> str:
        rows = rows or []
        if not rows:
            return json.dumps({"status": "error", "message": "rows is empty"})

        settings = get_settings()
        upload_dir = os.path.join(settings.upload_dir, "generated_docs")
        os.makedirs(upload_dir, exist_ok=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        tender_tag = f"tender_{tender_id}_" if tender_id else ""
        fname = f"cost_{tender_tag}{ts}_{uuid.uuid4().hex[:6]}.xlsx"
        fpath = os.path.join(upload_dir, fname)

        try:
            summary = build_cost_xlsx(fpath, title, rows)
        except Exception as e:
            logger.error(f"xlsx_generator: build failed: {e}", exc_info=True)
            return json.dumps({"status": "error", "message": f"xlsx build failed: {e}"})

        # Upload to shared storage so downloads serve the stored bytes instead of
        # regenerating the workbook on every click (worker builds it, web serves it).
        storage_key = fpath
        try:
            storage_key = _persist_workbook_to_storage(fpath, fname)
        except Exception as e:
            logger.error(f"xlsx_generator: storage upload failed, keeping local path: {e}", exc_info=True)

        result: dict = {
            "status": "success",
            "file_path": storage_key,
            "file_name": fname,
            **summary,
        }

        if session_id and self.db:
            try:
                from app.services.artifact_service import create_artifact
                artifact = create_artifact(
                    db=self.db,
                    session_id=session_id,
                    artifact_type="cost_breakdown_xlsx",
                    title=title or "Cost Breakdown",
                    content=json.dumps({"rows": rows, **summary}, default=str),
                    structured_data={"rows": rows, **summary},
                    agent_key="costing_researcher",
                    metadata={"tender_id": tender_id, "file_name": fname},
                )
                artifact.file_path = storage_key
                artifact.file_name = fname
                self.db.commit()
                result["artifact_id"] = artifact.id

                # This tool is a third producer of cost_breakdown_xlsx artifacts
                # (alongside cost_breakdown_service.persist_xlsx_artifact's two
                # call sites) — the preview must not depend on which path built
                # the workbook, so queue the render here too. Never let a queue
                # failure fail the costing run.
                try:
                    from app.worker.preview_tasks import enqueue_preview_render
                    enqueue_preview_render(artifact.id)
                except Exception as e:
                    logger.warning(f"xlsx_generator: preview enqueue failed: {e}")
            except Exception as e:
                logger.error(f"xlsx_generator: artifact persist failed: {e}", exc_info=True)
                result["artifact_error"] = str(e)

        return json.dumps(result, default=str)
