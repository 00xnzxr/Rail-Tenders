"""
DRPL Backend - Costing Format Service
Renders BOQ items into zone-specific formatted cost sheets.
Produces HTML and markdown matching railway zone cost document formats.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.costing_template import BOQItem, CostingTemplate

logger = logging.getLogger(__name__)


# Hardcoded fallbacks if PlatformSetting lookup fails — keep in sync with
# the seeded defaults in settings_service.DEFAULT_SETTINGS.
COSTING_FALLBACK_OVERHEAD = 10.0
COSTING_FALLBACK_MARGIN = 15.0
COSTING_FALLBACK_GST = 18.0


def get_costing_defaults(db: Optional[Session]) -> dict:
    """Return {overhead_percent, margin_percent, gst_percent} from PlatformSetting,
    falling back to the hardcoded defaults on any lookup failure.
    """
    overhead = COSTING_FALLBACK_OVERHEAD
    margin = COSTING_FALLBACK_MARGIN
    gst = COSTING_FALLBACK_GST
    if db is None:
        return {"overhead_percent": overhead, "margin_percent": margin, "gst_percent": gst}
    try:
        from app.services.settings_service import get_setting_value
        overhead = float(get_setting_value(db, "costing.default_overhead_percent", overhead) or overhead)
        margin = float(get_setting_value(db, "costing.default_margin_percent", margin) or margin)
        gst = float(get_setting_value(db, "costing.default_gst_percent", gst) or gst)
    except Exception as e:
        logger.debug(f"costing defaults lookup failed, using fallbacks: {e}")
        try:
            db.rollback()
        except Exception:
            pass
    return {"overhead_percent": overhead, "margin_percent": margin, "gst_percent": gst}

# Default column layout when no template is available
DEFAULT_COLUMNS = [
    {"name": "sr_no", "label": "Sr. No.", "type": "serial"},
    {"name": "description", "label": "Description of Item / Work", "type": "text"},
    {"name": "quantity", "label": "Quantity", "type": "number"},
    {"name": "unit", "label": "Unit", "type": "text"},
    {"name": "rate", "label": "Rate (Rs.)", "type": "currency"},
    {"name": "amount", "label": "Amount (Rs.)", "type": "formula"},
]


def render_costing_document(
    boq_items: list[BOQItem],
    template: Optional[CostingTemplate] = None,
    margin_percent: Optional[float] = None,
    gst_percent: Optional[float] = None,
    overhead_percent: Optional[float] = None,
    tender_title: str = "",
    db: Optional[Session] = None,
) -> dict:
    """
    Render BOQ items into a formatted cost sheet.

    Args:
        boq_items: List of BOQItem records (with computed_rate populated)
        template: Zone-specific costing template (or None for default)
        margin_percent: Profit margin %. None → load from PlatformSetting.
        gst_percent: GST %. None → load from PlatformSetting.
        overhead_percent: Overhead %. None → load from PlatformSetting.
        tender_title: Tender title for the header
        db: Optional DB session used to read costing defaults from PlatformSetting.

    Returns:
        {content_html, content_markdown, summary}
    """
    # Resolve defaults from PlatformSetting when not explicitly supplied
    if overhead_percent is None or margin_percent is None or gst_percent is None:
        defaults = get_costing_defaults(db)
        if overhead_percent is None:
            overhead_percent = defaults["overhead_percent"]
        if margin_percent is None:
            margin_percent = defaults["margin_percent"]
        if gst_percent is None:
            gst_percent = defaults["gst_percent"]
    columns = (template.column_definitions if template and template.column_definitions else DEFAULT_COLUMNS)
    footer_rows = (template.footer_rows if template and template.footer_rows else [])

    # Compute amounts for each item
    line_items = []
    subtotal = 0.0

    for item in boq_items:
        rate = item.computed_rate or item.estimated_rate or 0.0
        qty = item.quantity or 0.0
        amount = rate * qty
        subtotal += amount

        line_items.append({
            "sr_no": item.sr_no,
            "description": item.description,
            "quantity": qty,
            "unit": item.unit or "",
            "rate": rate,
            "amount": amount,
            "material_cost": item.material_cost,
            "labour_cost": item.labour_cost,
            "overhead_cost": item.overhead_cost,
            "rate_source": item.rate_source,
        })

    # Compute totals
    overhead_amount = subtotal * (overhead_percent / 100)
    subtotal_with_overhead = subtotal + overhead_amount
    margin_amount = subtotal_with_overhead * (margin_percent / 100)
    pre_tax_total = subtotal_with_overhead + margin_amount
    gst_amount = pre_tax_total * (gst_percent / 100)
    grand_total = pre_tax_total + gst_amount

    totals = {
        "subtotal": subtotal,
        "overhead_percent": overhead_percent,
        "overhead_amount": overhead_amount,
        "subtotal_with_overhead": subtotal_with_overhead,
        "margin_percent": margin_percent,
        "margin_amount": margin_amount,
        "pre_tax_total": pre_tax_total,
        "gst_percent": gst_percent,
        "gst_amount": gst_amount,
        "grand_total": grand_total,
    }

    # Use template HTML if available, otherwise generate default
    if template and template.html_template:
        content_html = _render_from_template(template.html_template, line_items, totals, tender_title)
    else:
        content_html = _render_default_html(line_items, totals, columns, tender_title)

    content_markdown = _render_markdown(line_items, totals, tender_title)

    summary = (
        f"Cost sheet with {len(line_items)} items. "
        f"Subtotal: Rs. {subtotal:,.2f}, "
        f"Overhead ({overhead_percent}%): Rs. {overhead_amount:,.2f}, "
        f"Margin ({margin_percent}%): Rs. {margin_amount:,.2f}, "
        f"GST ({gst_percent}%): Rs. {gst_amount:,.2f}, "
        f"Grand Total: Rs. {grand_total:,.2f}"
    )

    return {
        "content_html": content_html,
        "content_markdown": content_markdown,
        "summary": summary,
        "totals": totals,
        "item_count": len(line_items),
    }


def _render_from_template(html_template: str, line_items: list, totals: dict, tender_title: str) -> str:
    """Render using a Jinja2 HTML template."""
    try:
        from jinja2 import Template
        tmpl = Template(html_template)
        return tmpl.render(items=line_items, totals=totals, title=tender_title)
    except Exception as e:
        logger.warning(f"Template rendering failed, using default: {e}")
        return _render_default_html(line_items, totals, DEFAULT_COLUMNS, tender_title)


def _render_default_html(line_items: list, totals: dict, columns: list, tender_title: str) -> str:
    """Generate a default HTML cost sheet table."""
    html = f"""<div style="font-family: 'Times New Roman', serif; font-size: 12px;">
<h2 style="text-align: center; margin-bottom: 10px;">ABSTRACT OF COST</h2>
"""
    if tender_title:
        html += f'<p style="text-align: center; font-size: 11px; margin-bottom: 15px;"><strong>{tender_title}</strong></p>\n'

    html += """<table style="width: 100%; border-collapse: collapse; border: 1px solid #000;">
<thead>
<tr style="background-color: #f0f0f0;">"""

    for col in columns:
        align = "right" if col["type"] in ("number", "currency", "formula") else "left"
        html += f'<th style="border: 1px solid #000; padding: 6px; text-align: {align}; font-size: 11px;">{col["label"]}</th>'

    html += "</tr>\n</thead>\n<tbody>"

    for item in line_items:
        html += "<tr>"
        for col in columns:
            val = item.get(col["name"], "")
            align = "right"
            if col["type"] == "serial":
                val = item.get("sr_no", "")
                align = "center"
            elif col["type"] == "text":
                val = item.get(col["name"], "")
                align = "left"
            elif col["type"] in ("number", "currency", "formula"):
                val = item.get(col["name"], 0)
                if isinstance(val, (int, float)) and val:
                    val = f"{val:,.2f}"
                else:
                    val = ""
            html += f'<td style="border: 1px solid #000; padding: 5px; text-align: {align}; font-size: 11px;">{val}</td>'
        html += "</tr>\n"

    html += "</tbody>\n</table>\n"

    # Footer totals
    html += _render_totals_html(totals)
    html += "</div>"

    return html


def _render_totals_html(totals: dict) -> str:
    """Render the totals section as HTML."""
    rows = [
        ("Sub Total", totals["subtotal"]),
        (f"Overhead & Contingencies ({totals['overhead_percent']}%)", totals["overhead_amount"]),
        ("Total before Margin", totals["subtotal_with_overhead"]),
        (f"Contractor's Profit ({totals['margin_percent']}%)", totals["margin_amount"]),
        ("Total before Tax", totals["pre_tax_total"]),
        (f"GST ({totals['gst_percent']}%)", totals["gst_amount"]),
        ("GRAND TOTAL (Inclusive of GST)", totals["grand_total"]),
    ]

    html = '<table style="width: 60%; margin-left: auto; margin-top: 10px; border-collapse: collapse;">\n'
    for label, value in rows:
        bold = ' font-weight: bold;' if "GRAND" in label else ""
        html += (
            f'<tr>'
            f'<td style="padding: 4px 8px; border-bottom: 1px solid #ccc;{bold}">{label}</td>'
            f'<td style="padding: 4px 8px; text-align: right; border-bottom: 1px solid #ccc;{bold}">Rs. {value:,.2f}</td>'
            f'</tr>\n'
        )
    html += "</table>\n"
    return html


def _render_markdown(line_items: list, totals: dict, tender_title: str) -> str:
    """Render cost sheet as markdown."""
    md = "# ABSTRACT OF COST\n\n"
    if tender_title:
        md += f"**{tender_title}**\n\n"

    md += "| Sr. No. | Description | Qty | Unit | Rate (Rs.) | Amount (Rs.) |\n"
    md += "|---------|-------------|-----|------|------------|-------------|\n"

    for item in line_items:
        rate = f"{item['rate']:,.2f}" if item['rate'] else "-"
        amount = f"{item['amount']:,.2f}" if item['amount'] else "-"
        qty = f"{item['quantity']:g}" if item['quantity'] else "-"
        md += f"| {item['sr_no']} | {item['description']} | {qty} | {item['unit']} | {rate} | {amount} |\n"

    md += f"\n---\n\n"
    md += f"| | |\n|---|---:|\n"
    md += f"| Sub Total | Rs. {totals['subtotal']:,.2f} |\n"
    md += f"| Overhead ({totals['overhead_percent']}%) | Rs. {totals['overhead_amount']:,.2f} |\n"
    md += f"| Contractor's Profit ({totals['margin_percent']}%) | Rs. {totals['margin_amount']:,.2f} |\n"
    md += f"| GST ({totals['gst_percent']}%) | Rs. {totals['gst_amount']:,.2f} |\n"
    md += f"| **GRAND TOTAL** | **Rs. {totals['grand_total']:,.2f}** |\n"

    return md
