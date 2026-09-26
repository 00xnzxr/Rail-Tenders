"""
DRPL Backend - XLSX Template Parser
Extracts column structure and formulas from uploaded Excel templates
for costing/BOQ template creation.
"""

import json
import logging
from typing import Optional

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.services.ai_service import call_ai

logger = logging.getLogger(__name__)


def _infer_column_type(cell) -> str:
    """Infer column type from cell value and format."""
    if cell.value is None:
        return "text"
    fmt = (cell.number_format or "General").lower()
    if "%" in fmt:
        return "percentage"
    if any(sym in fmt for sym in ("₹", "$", "#,##", "0.00")):
        return "currency"
    if isinstance(cell.value, (int, float)):
        return "number"
    val = str(cell.value).lower()
    if val.startswith("=sum") or val.startswith("="):
        return "formula"
    return "text"


def _extract_xlsx_structure(file_path: str) -> dict:
    """Extract header row, column definitions, and footer rows from an XLSX file."""
    wb = load_workbook(file_path, data_only=False)
    ws = wb.active

    if ws.max_row is None or ws.max_row < 2:
        return {"column_definitions": [], "footer_rows": [], "metadata": {"error": "Empty spreadsheet"}}

    # Find header row (first row with mostly non-empty cells, or bold cells)
    header_row_idx = 1
    for row_idx in range(1, min(10, ws.max_row + 1)):
        row_cells = [ws.cell(row=row_idx, column=c) for c in range(1, ws.max_column + 1)]
        non_empty = sum(1 for c in row_cells if c.value)
        is_bold = any(c.font and c.font.bold for c in row_cells if c.font)
        if non_empty >= 2 and (is_bold or non_empty >= ws.max_column * 0.5):
            header_row_idx = row_idx
            break

    # Extract column definitions from header row
    column_definitions = []
    for col_idx in range(1, ws.max_column + 1):
        header_cell = ws.cell(row=header_row_idx, column=col_idx)
        if not header_cell.value:
            continue

        # Look at data cells below to infer type
        col_type = "text"
        for data_row in range(header_row_idx + 1, min(header_row_idx + 5, ws.max_row + 1)):
            data_cell = ws.cell(row=data_row, column=col_idx)
            if data_cell.value is not None:
                col_type = _infer_column_type(data_cell)
                break

        label = str(header_cell.value).strip()
        name = label.lower().replace(" ", "_").replace(".", "").replace("(", "").replace(")", "")

        # Check for formulas in this column
        formula = None
        for data_row in range(header_row_idx + 1, min(header_row_idx + 5, ws.max_row + 1)):
            data_cell = ws.cell(row=data_row, column=col_idx)
            if data_cell.value and str(data_cell.value).startswith("="):
                formula = str(data_cell.value)
                col_type = "formula"
                break

        col_def = {"name": name, "label": label, "type": col_type}
        if formula:
            col_def["formula"] = formula
        column_definitions.append(col_def)

    # Find footer rows (rows near the bottom with labels like Total, Sub Total, GST, Grand Total)
    footer_rows = []
    footer_keywords = {"total", "sub total", "subtotal", "grand total", "gst", "tax", "net",
                        "overhead", "profit", "margin", "amount", "sum"}

    # Scan last 15 rows for totals
    start_scan = max(header_row_idx + 1, ws.max_row - 15)
    for row_idx in range(start_scan, ws.max_row + 1):
        first_cell = ws.cell(row=row_idx, column=1)
        cell_text = str(first_cell.value or "").strip().lower()

        if any(kw in cell_text for kw in footer_keywords):
            # Look for formula/value in the same row
            formula = None
            footer_type = "sum"
            for col_idx in range(2, ws.max_column + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                if cell.value and str(cell.value).startswith("="):
                    formula = str(cell.value)
                    break

            if "gst" in cell_text or "tax" in cell_text:
                footer_type = "tax"
            elif "grand" in cell_text:
                footer_type = "grand_total"
            elif "overhead" in cell_text:
                footer_type = "overhead"
            elif "profit" in cell_text or "margin" in cell_text:
                footer_type = "margin"

            footer_rows.append({
                "label": str(first_cell.value).strip(),
                "formula": formula,
                "type": footer_type,
            })

    # Build text representation for AI enrichment
    text_lines = [f"Header row {header_row_idx}:"]
    for cd in column_definitions:
        text_lines.append(f"  - {cd['label']} (type: {cd['type']})")

    # Sample data rows
    text_lines.append(f"\nSample data ({min(5, ws.max_row - header_row_idx)} rows):")
    for row_idx in range(header_row_idx + 1, min(header_row_idx + 6, ws.max_row + 1)):
        values = []
        for col_idx in range(1, ws.max_column + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            values.append(str(cell.value or ""))
        text_lines.append(f"  {' | '.join(values)}")

    if footer_rows:
        text_lines.append(f"\nFooter rows ({len(footer_rows)}):")
        for fr in footer_rows:
            text_lines.append(f"  - {fr['label']} (type: {fr['type']}, formula: {fr.get('formula', 'N/A')})")

    return {
        "column_definitions": column_definitions,
        "footer_rows": footer_rows,
        "text_representation": "\n".join(text_lines),
        "header_row": header_row_idx,
        "data_rows": ws.max_row - header_row_idx - len(footer_rows),
        "sheet_name": ws.title,
    }


async def parse_xlsx_template(file_path: str, db=None) -> dict:
    """
    Parse an XLSX template file, extract column structure and footer formulas,
    and use AI to classify and enrich the structure.
    """
    try:
        raw = _extract_xlsx_structure(file_path)
    except Exception as e:
        logger.error(f"Failed to parse XLSX {file_path}: {e}")
        return {"column_definitions": [], "footer_rows": [], "metadata": {"error": str(e)}}

    if not raw.get("column_definitions"):
        return {"column_definitions": [], "footer_rows": [], "metadata": {"error": "No columns found"}}

    # Use AI to classify and enrich
    system_prompt = """You are a financial document analyst specializing in Indian government tender costing formats.
Given the extracted structure of an Excel spreadsheet template, classify it and enrich the column definitions.

Return ONLY valid JSON:
{
  "column_definitions": [
    {"name": "sr_no", "label": "Sr. No.", "type": "serial|text|number|currency|formula|percentage", "formula": "optional formula"}
  ],
  "footer_rows": [
    {"label": "Sub Total", "formula": "SUM(amount)", "type": "sum|tax|overhead|margin|grand_total"}
  ],
  "metadata": {
    "format_type": "boq|rate_schedule|cost_statement|price_bid",
    "zone": "railway zone if identifiable or GENERAL",
    "description": "Brief description of this costing format"
  }
}

Preserve the original column labels. Correct the type inference if needed.
Identify the format_type based on column structure (BOQ has quantities, rate schedule has items and rates, etc.)."""

    text_repr = raw.get("text_representation", "")
    if len(text_repr) > 10000:
        text_repr = text_repr[:10000]

    user_prompt = f"Classify and enrich this spreadsheet template:\n\n{text_repr}"

    try:
        result = await call_ai(system_prompt, user_prompt, db, "template_parser")
        result = result.strip()
        if result.startswith("```"):
            result = result.split("\n", 1)[1].rsplit("```", 1)[0]
        structure = json.loads(result)

        # Merge AI enrichment with raw extraction
        if "column_definitions" not in structure:
            structure["column_definitions"] = raw["column_definitions"]
        if "footer_rows" not in structure:
            structure["footer_rows"] = raw["footer_rows"]
        if "metadata" not in structure:
            structure["metadata"] = {}

        return structure

    except json.JSONDecodeError:
        logger.error("AI returned invalid JSON for XLSX parsing")
        return {
            "column_definitions": raw["column_definitions"],
            "footer_rows": raw["footer_rows"],
            "metadata": {"format_type": "boq", "zone": "GENERAL"},
        }
    except Exception as e:
        logger.error(f"XLSX template parsing AI call failed: {e}")
        return {
            "column_definitions": raw["column_definitions"],
            "footer_rows": raw["footer_rows"],
            "metadata": {"error": str(e), "format_type": "boq", "zone": "GENERAL"},
        }
