"""
DRPL Backend - Cost Breakdown PDF Service

Renders the NIT-mirror cost-breakdown as a PDF that visually matches the
tender's "Schedule of Items" table — one section per Schedule (A, B, ...),
identical column structure (S.No. / Item Code / Description / Item Qty /
Qty Unit / Unit Rate / Basic Value / Escl.(%) / Amount / Bidding Unit),
totals + GST line. Uses the WeasyPrint backend already wired up in
pdf_generation_service.py; falls back to ReportLab when WeasyPrint is
unavailable.

Plan: ~/.claude/plans/now-i-need-to-synchronous-taco.md
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from html import escape
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
from app.models.costing_template import BOQItem
from app.services.pdf_generation_service import _render_html_to_pdf

logger = logging.getLogger(__name__)


def _fmt_inr(value: Optional[float]) -> str:
    """Indian-format numeric. Returns '—' for None."""
    if value is None:
        return "—"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_int(value: Optional[float]) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _group_lines_by_schedule(lines: list[CostBreakdownLine]) -> dict[str, list[CostBreakdownLine]]:
    groups: dict[str, list[CostBreakdownLine]] = {}
    order: list[str] = []
    for ln in lines:
        sched = (ln.schedule_name or "?").strip() or "?"
        if sched not in groups:
            groups[sched] = []
            order.append(sched)
        groups[sched].append(ln)
    return {s: groups[s] for s in order}


def _render_schedule_table(
    schedule_name: str,
    lines: list[CostBreakdownLine],
) -> str:
    """Render one Schedule table as HTML. Mirrors the NIT column structure
    so the PDF reads side-by-side with the source NIT."""
    label = "Other Items" if schedule_name == "?" else f"Schedule {schedule_name}"
    parts: list[str] = []
    parts.append(f'<h2 class="schedule-title">{escape(label)}</h2>')
    parts.append('<table class="nit-table">')
    parts.append("<thead><tr>")
    for col in (
        "S.No.", "Item Code", "Description", "Item Qty", "Qty Unit",
        "Unit Rate", "Basic Value", "Escl.(%)", "Amount", "Bidding Unit",
    ):
        parts.append(f'<th>{escape(col)}</th>')
    parts.append("</tr></thead><tbody>")

    schedule_total = 0.0
    for ln in lines:
        is_tax = bool(ln.is_tax_line)
        row_cls = "tax-row" if is_tax else ""
        # For non-tax rows, use the persisted amount; tax rows show blank.
        amount = ln.amount if not is_tax else None
        if amount is not None:
            schedule_total += float(amount)
        parts.append(f'<tr class="{row_cls}">')
        parts.append(f'<td class="num">{escape(str(ln.sr_no or "—"))}</td>')
        parts.append(f'<td>{escape(ln.item_code or "—")}</td>')
        parts.append(f'<td class="desc">{escape((ln.description or "").strip())}</td>')
        parts.append(f'<td class="num">{_fmt_int(ln.quantity)}</td>')
        parts.append(f'<td>{escape(ln.unit or "—")}</td>')
        parts.append(f'<td class="num">{_fmt_inr(ln.rate) if not is_tax else "—"}</td>')
        parts.append(f'<td class="num">{_fmt_inr(ln.basic_value)}</td>')
        parts.append(
            f'<td class="num">{_fmt_inr(ln.escalation_pct) if ln.escalation_pct is not None else "0.00"}</td>'
        )
        parts.append(f'<td class="num">{_fmt_inr(amount)}</td>')
        parts.append(f'<td>{escape(ln.bidding_unit or "—")}</td>')
        parts.append("</tr>")

    parts.append("</tbody></table>")
    parts.append(
        f'<p class="schedule-total"><strong>{escape(label)} Total:</strong> '
        f'₹ {_fmt_inr(schedule_total)}</p>'
    )
    return "\n".join(parts), schedule_total


def _render_summary_block(
    breakdown: CostBreakdown,
    schedule_totals: dict[str, float],
    strategic_summary: Optional[dict] = None,
) -> str:
    parts: list[str] = []
    parts.append('<div class="summary">')
    parts.append('<h2>Cost Summary</h2>')

    snap = (strategic_summary or {}).get("tender_snapshot") or {}
    parts.append('<table class="meta">')
    parts.append('<tbody>')
    parts.append(f'<tr><th>Tender ID</th><td>{escape(str(breakdown.tender_id))}</td></tr>')
    parts.append(
        f'<tr><th>Version</th><td>v{escape(str(breakdown.version))}</td></tr>'
    )
    for label, key in (
        ("Tender No", "tender_no"),
        ("Scope", "scope_one_liner"),
        ("Period", "period"),
        ("Tender Value (₹)", "tender_value_inr"),
        ("EMD (₹)", "emd_inr"),
    ):
        val = snap.get(key)
        if val in (None, ""):
            continue
        parts.append(f"<tr><th>{escape(label)}</th><td>{escape(str(val))}</td></tr>")
    parts.append('</tbody></table>')

    parts.append('<h3>Per-Schedule Totals</h3>')
    parts.append('<table class="totals"><thead><tr><th>Schedule</th><th class="num">Total (₹)</th></tr></thead><tbody>')
    grand_total_pre_tax = 0.0
    for sched, total in schedule_totals.items():
        label = "Other Items" if sched == "?" else f"Schedule {sched}"
        parts.append(f'<tr><td>{escape(label)}</td><td class="num">{_fmt_inr(total)}</td></tr>')
        grand_total_pre_tax += float(total)
    parts.append('</tbody></table>')

    overhead_amount = grand_total_pre_tax * (breakdown.overhead_percent or 0) / 100.0
    margin_amount = (grand_total_pre_tax + overhead_amount) * (breakdown.margin_percent or 0) / 100.0
    gst_base = grand_total_pre_tax + overhead_amount + margin_amount
    gst_amount = gst_base * (breakdown.gst_percent or 0) / 100.0
    bid_total = gst_base + gst_amount

    parts.append('<h3>Commercial Roll-Up</h3>')
    parts.append('<table class="totals"><tbody>')
    parts.append(
        f'<tr><th>Schedules subtotal (pre-GST)</th><td class="num">{_fmt_inr(grand_total_pre_tax)}</td></tr>'
    )
    parts.append(
        f'<tr><th>+ Overhead ({breakdown.overhead_percent or 0:.2f}%)</th>'
        f'<td class="num">{_fmt_inr(overhead_amount)}</td></tr>'
    )
    parts.append(
        f'<tr><th>+ Margin ({breakdown.margin_percent or 0:.2f}%)</th>'
        f'<td class="num">{_fmt_inr(margin_amount)}</td></tr>'
    )
    parts.append(
        f'<tr><th>+ GST ({breakdown.gst_percent or 0:.2f}%)</th>'
        f'<td class="num">{_fmt_inr(gst_amount)}</td></tr>'
    )
    parts.append(
        f'<tr class="grand"><th>Bid Total</th>'
        f'<td class="num">₹ {_fmt_inr(bid_total)}</td></tr>'
    )
    parts.append('</tbody></table>')
    parts.append('</div>')
    return "\n".join(parts)


_PDF_CSS = """
@page { size: A4 landscape; margin: 16mm 14mm 16mm 14mm; }
body { font-family: "Helvetica", "Arial", sans-serif; font-size: 9.5pt; color: #1f2937; }
h1 { font-size: 16pt; margin: 0 0 4pt 0; color: #1f4e78; }
h2 { font-size: 12pt; margin: 18pt 0 6pt 0; color: #1f4e78; }
h3 { font-size: 10.5pt; margin: 12pt 0 4pt 0; color: #1f4e78; }
p { margin: 4pt 0; }
.subtitle { color: #6b7280; font-size: 9pt; margin: 0 0 8pt 0; }
.schedule-title { background: #e0eaf6; padding: 4pt 8pt; border-left: 4pt solid #1f4e78; }
.schedule-total { text-align: right; margin: 6pt 0 14pt 0; font-size: 10pt; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0; }
table.nit-table th, table.nit-table td { border: 0.5pt solid #9ca3af; padding: 3pt 5pt; vertical-align: top; }
table.nit-table thead th { background: #1f4e78; color: #fff; font-weight: 600; }
table.nit-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.nit-table td.desc { max-width: 360pt; }
table.nit-table tr.tax-row { background: #fce4d6; }
table.meta th, table.meta td, table.totals th, table.totals td {
  border: 0.5pt solid #d1d5db; padding: 3pt 6pt;
}
table.meta th, table.totals th { background: #f3f4f6; text-align: left; font-weight: 600; width: 38%; }
table.totals td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.totals tr.grand th, table.totals tr.grand td { background: #d9e1f2; font-size: 11pt; font-weight: 700; }
.footer { color: #6b7280; font-size: 8pt; margin-top: 20pt; }
"""


def render_cost_breakdown_pdf(
    db: Session,
    breakdown: CostBreakdown,
) -> bytes:
    """Render the NIT-mirror cost-breakdown as a PDF, returning raw bytes.

    Walks the breakdown's CostBreakdownLine rows grouped by schedule_name,
    laying out each schedule as a table that mirrors the NIT exactly. Reuses
    `_render_html_to_pdf` from pdf_generation_service so the WeasyPrint /
    ReportLab fallback chain is consistent with the rest of the platform.
    """
    lines: list[CostBreakdownLine] = list(breakdown.lines)
    groups = _group_lines_by_schedule(lines)

    # Optional context — strategic_summary holds tender_snapshot fields.
    strategic_summary: Optional[dict] = None
    if breakdown.strategic_summary_json:
        try:
            strategic_summary = json.loads(breakdown.strategic_summary_json)
        except Exception:
            strategic_summary = None

    # Render each schedule and capture its total.
    body_parts: list[str] = []
    schedule_totals: dict[str, float] = {}
    for sched, sched_lines in groups.items():
        html, total = _render_schedule_table(sched, sched_lines)
        body_parts.append(html)
        schedule_totals[sched] = total

    summary_html = _render_summary_block(breakdown, schedule_totals, strategic_summary)

    title = breakdown.title or f"Cost Breakdown — Tender #{breakdown.tender_id}"
    generated_at = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    full_html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>{_PDF_CSS}</style></head>
<body>
<h1>{escape(title)}</h1>
<p class="subtitle">NIT-mirror cost breakdown · generated {escape(generated_at)}</p>
{summary_html}
{''.join(body_parts)}
<p class="footer">Generated by DRPL Platform. NIT-mirror layout: rates/amounts editable in the cost breakdown editor; this PDF reflects the current draft.</p>
</body></html>"""

    return _render_html_to_pdf(full_html, orientation="landscape")


def regenerate_pdf_artifact(
    db: Session,
    breakdown: CostBreakdown,
    *,
    session_id: Optional[int] = None,
) -> Optional[dict]:
    """Generate a fresh NIT-mirror PDF from the current breakdown state and
    persist it as a `cost_breakdown_pdf` artifact in the given session.
    Returns the artifact info (path, name, id) for the route handler.
    """
    from app.services.artifact_service import create_artifact

    if session_id is None:
        session_id = breakdown.session_id
    if session_id is None:
        logger.warning("[cost_breakdown_pdf] no session_id, skipping artifact creation")
        return None

    # Bidding-schedule sanity: require at least some NIT-mirror data so we
    # don't silently produce a PDF that doesn't match the user's expectation
    # of an IREPS-format document.
    has_schedule = any(
        ln.schedule_name or ln.item_code or ln.boq_item_id
        for ln in breakdown.lines
    )
    if not has_schedule:
        boq_count = (
            db.query(BOQItem)
            .filter(BOQItem.tender_id == breakdown.tender_id)
            .count()
        )
        if boq_count == 0:
            logger.warning(
                f"[cost_breakdown_pdf] tender {breakdown.tender_id}: no captured "
                f"NIT schedule — PDF will still render but with un-grouped rows"
            )

    pdf_bytes = render_cost_breakdown_pdf(db, breakdown)

    settings = get_settings()
    upload_dir = os.path.join(settings.upload_dir, "generated_docs")
    os.makedirs(upload_dir, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"cost_tender_{breakdown.tender_id}_{ts}_{uuid.uuid4().hex[:6]}.pdf"
    fpath = os.path.join(upload_dir, fname)
    with open(fpath, "wb") as fh:
        fh.write(pdf_bytes)

    title = breakdown.title or f"Cost Breakdown — Tender #{breakdown.tender_id}"
    artifact = create_artifact(
        db=db,
        session_id=session_id,
        artifact_type="cost_breakdown_pdf",
        title=title,
        content=json.dumps({"cost_breakdown_id": breakdown.id, "version": breakdown.version}),
        structured_data={"cost_breakdown_id": breakdown.id, "version": breakdown.version},
        agent_key="costing_researcher",
        metadata={
            "tender_id": breakdown.tender_id,
            "file_name": fname,
            "cost_breakdown_id": breakdown.id,
        },
    )
    artifact.file_path = fpath
    artifact.file_name = fname
    db.commit()

    logger.info(
        f"[cost_breakdown_pdf] generated PDF for tender {breakdown.tender_id} "
        f"v{breakdown.version}: {len(pdf_bytes)} bytes, {fname}"
    )

    return {
        "artifact_id": artifact.id,
        "artifact_type": "cost_breakdown_pdf",
        "title": title,
        "version": artifact.version,
        "file_name": fname,
        "file_path": fpath,
        "byte_size": len(pdf_bytes),
    }
