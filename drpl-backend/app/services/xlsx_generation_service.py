"""
DRPL Backend - XLSX Generation Service
Generates formatted Excel spreadsheets from costing data and BOQ items.
"""

import io
import logging
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill, numbers
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

# Indian currency format
INR_FORMAT = '₹ #,##,##0.00'
NUMBER_FORMAT = '#,##,##0.00'

# Styling constants
HEADER_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
DATA_FONT = Font(name="Calibri", size=10)
TOTAL_FONT = Font(name="Calibri", size=11, bold=True)
TOTAL_FILL = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")
GRAND_TOTAL_FILL = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
GRAND_TOTAL_FONT = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
ALT_ROW_FILL = PatternFill(start_color="F2F7FB", end_color="F2F7FB", fill_type="solid")
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)


def generate_xlsx_from_costing(
    items: list[dict],
    column_definitions: Optional[list[dict]] = None,
    footer_rows: Optional[list[dict]] = None,
    title: Optional[str] = None,
    sheet_name: str = "Cost Sheet",
) -> bytes:
    """
    Generate an Excel spreadsheet from costing/BOQ data.

    Args:
        items: List of dicts, each representing a row of data
        column_definitions: Column schema [{name, label, type, formula}]
        footer_rows: Footer formula rows [{label, formula, type}]
        title: Optional title for the sheet header
        sheet_name: Name of the worksheet

    Returns:
        XLSX file as bytes
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name

    # Use default columns if not provided
    if not column_definitions:
        column_definitions = [
            {"name": "sr_no", "label": "Sr. No.", "type": "serial"},
            {"name": "description", "label": "Description", "type": "text"},
            {"name": "quantity", "label": "Qty", "type": "number"},
            {"name": "unit", "label": "Unit", "type": "text"},
            {"name": "rate", "label": "Rate (₹)", "type": "currency"},
            {"name": "amount", "label": "Amount (₹)", "type": "currency"},
        ]

    num_cols = len(column_definitions)
    current_row = 1

    # Title row
    if title:
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=num_cols)
        title_cell = ws.cell(row=1, column=1, value=title)
        title_cell.font = Font(name="Calibri", size=14, bold=True)
        title_cell.alignment = Alignment(horizontal="center", vertical="center")
        current_row = 3  # leave a blank row

    # Header row
    header_row = current_row
    for col_idx, col_def in enumerate(column_definitions, 1):
        cell = ws.cell(row=header_row, column=col_idx, value=col_def.get("label", col_def.get("name", "")))
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER

    current_row = header_row + 1

    # Data rows
    for row_idx, item in enumerate(items):
        for col_idx, col_def in enumerate(column_definitions, 1):
            col_name = col_def.get("name", "")
            col_type = col_def.get("type", "text")
            value = item.get(col_name, "")

            # Handle serial number
            if col_type == "serial":
                value = row_idx + 1

            cell = ws.cell(row=current_row, column=col_idx, value=value)
            cell.font = DATA_FONT
            cell.border = THIN_BORDER

            # Apply number format
            if col_type == "currency":
                cell.number_format = INR_FORMAT
                cell.alignment = Alignment(horizontal="right")
            elif col_type == "number":
                cell.number_format = NUMBER_FORMAT
                cell.alignment = Alignment(horizontal="right")
            elif col_type == "percentage":
                cell.number_format = '0.00%'
                cell.alignment = Alignment(horizontal="right")
            else:
                cell.alignment = Alignment(wrap_text=True, vertical="top")

            # Alternating row colors
            if row_idx % 2 == 1:
                cell.fill = ALT_ROW_FILL

        current_row += 1

    # Footer rows (subtotal, taxes, grand total)
    data_start_row = header_row + 1
    data_end_row = current_row - 1

    if footer_rows:
        # Add a blank separator row
        current_row += 1

        for footer in footer_rows:
            footer_type = footer.get("type", "sum")
            label = footer.get("label", "Total")

            # Label cell spans most columns
            label_span = max(1, num_cols - 2)
            ws.merge_cells(
                start_row=current_row, start_column=1,
                end_row=current_row, end_column=label_span,
            )
            label_cell = ws.cell(row=current_row, column=1, value=label)

            # Value cell (last column or second-to-last)
            value_col = num_cols
            formula_str = footer.get("formula", "")

            # Generate SUM formula for the amount column
            if "SUM" in formula_str.upper() or footer_type == "sum":
                col_letter = get_column_letter(value_col)
                cell_formula = f"=SUM({col_letter}{data_start_row}:{col_letter}{data_end_row})"
            elif footer_type in ("tax", "overhead", "margin"):
                # Reference the previous footer row
                prev_row = current_row - 1
                col_letter = get_column_letter(value_col)
                # Try to extract percentage from formula or label
                pct = _extract_percentage(formula_str or label)
                if pct:
                    cell_formula = f"={col_letter}{prev_row}*{pct/100}"
                else:
                    cell_formula = f"={col_letter}{prev_row}"
            elif footer_type == "grand_total":
                # Sum all previous footer values
                col_letter = get_column_letter(value_col)
                # Sum from subtotal row to current - 1
                first_footer_row = data_end_row + 2
                cell_formula = f"=SUM({col_letter}{first_footer_row}:{col_letter}{current_row - 1})"
            else:
                cell_formula = ""

            value_cell = ws.cell(row=current_row, column=value_col, value=cell_formula if cell_formula else "")
            value_cell.number_format = INR_FORMAT

            # Styling based on footer type
            if footer_type == "grand_total":
                label_cell.font = GRAND_TOTAL_FONT
                label_cell.fill = GRAND_TOTAL_FILL
                value_cell.font = GRAND_TOTAL_FONT
                value_cell.fill = GRAND_TOTAL_FILL
            else:
                label_cell.font = TOTAL_FONT
                label_cell.fill = TOTAL_FILL
                value_cell.font = TOTAL_FONT
                value_cell.fill = TOTAL_FILL

            # Apply borders
            for col in range(1, num_cols + 1):
                ws.cell(row=current_row, column=col).border = THIN_BORDER

            label_cell.alignment = Alignment(horizontal="right")
            value_cell.alignment = Alignment(horizontal="right")

            current_row += 1

    # Auto-size columns
    for col_idx, col_def in enumerate(column_definitions, 1):
        col_type = col_def.get("type", "text")
        label_len = len(col_def.get("label", ""))

        if col_type == "text" and col_def.get("name") == "description":
            width = 45
        elif col_type in ("currency",):
            width = max(15, label_len + 2)
        elif col_type == "serial":
            width = 8
        else:
            width = max(12, label_len + 2)

        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # Freeze header row
    ws.freeze_panes = f"A{header_row + 1}"

    # Print settings
    ws.print_title_rows = f"{header_row}:{header_row}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1

    # Save to bytes
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer.read()


def generate_xlsx_from_boq_items(
    boq_items: list,
    costing_template=None,
    title: Optional[str] = None,
    overhead_pct: float = 10.0,
    margin_pct: float = 15.0,
    gst_pct: float = 18.0,
) -> bytes:
    """
    Generate an Excel spreadsheet from BOQItem model instances.

    Args:
        boq_items: List of BOQItem model instances
        costing_template: Optional CostingTemplate with column definitions
        title: Title for the sheet
        overhead_pct: Overhead percentage
        margin_pct: Contractor margin percentage
        gst_pct: GST percentage
    """
    # Use template columns or default
    if costing_template and costing_template.column_definitions:
        col_defs = costing_template.column_definitions
    else:
        col_defs = [
            {"name": "sr_no", "label": "Sr. No.", "type": "serial"},
            {"name": "description", "label": "Description of Item", "type": "text"},
            {"name": "quantity", "label": "Quantity", "type": "number"},
            {"name": "unit", "label": "Unit", "type": "text"},
            {"name": "rate", "label": "Rate (₹)", "type": "currency"},
            {"name": "amount", "label": "Amount (₹)", "type": "currency"},
        ]

    # Use template footers or default
    if costing_template and costing_template.footer_rows:
        footers = costing_template.footer_rows
    else:
        footers = [
            {"label": "Sub Total", "formula": "SUM(amount)", "type": "sum"},
            {"label": f"Site Overhead ({overhead_pct}%)", "formula": f"*{overhead_pct/100}", "type": "overhead"},
            {"label": f"Contractor's Profit ({margin_pct}%)", "formula": f"*{margin_pct/100}", "type": "margin"},
            {"label": f"GST ({gst_pct}%)", "formula": f"*{gst_pct/100}", "type": "tax"},
            {"label": "GRAND TOTAL", "formula": "SUM", "type": "grand_total"},
        ]

    # Convert BOQItem instances to dicts
    items = []
    for item in boq_items:
        rate = getattr(item, "computed_rate", None) or getattr(item, "estimated_rate", None) or 0
        qty = getattr(item, "quantity", None) or 0
        items.append({
            "sr_no": getattr(item, "sr_no", 0),
            "description": getattr(item, "description", ""),
            "quantity": qty,
            "unit": getattr(item, "unit", ""),
            "rate": rate,
            "amount": rate * qty if rate and qty else 0,
        })

    return generate_xlsx_from_costing(
        items=items,
        column_definitions=col_defs,
        footer_rows=footers,
        title=title,
        sheet_name="BOQ Cost Sheet",
    )


def _extract_percentage(text: str) -> Optional[float]:
    """Extract a percentage value from text like '10%' or 'GST (18%)'."""
    import re
    match = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
    if match:
        return float(match.group(1))
    return None


# ---------------------------------------------------------------------------
# Content-based XLSX generation (from markdown/HTML tables)
# ---------------------------------------------------------------------------

def _parse_markdown_tables(content: str) -> list[list[list[str]]]:
    """
    Extract tables from markdown content.
    Returns list of tables, each table is a list of rows, each row is a list of cell strings.
    """
    import re
    tables = []
    current_table = []

    for line in content.split("\n"):
        stripped = line.strip()
        # Detect table rows: must have at least 2 pipe characters
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 3:
            # Skip separator rows (|---|---|)
            if re.match(r"^\|[\s\-:]+\|", stripped):
                continue
            cells = [c.strip() for c in stripped.split("|")[1:-1]]  # skip first/last empty
            current_table.append(cells)
        else:
            # End of table — save if we have rows
            if current_table and len(current_table) >= 2:
                tables.append(current_table)
            current_table = []

    # Don't forget the last table
    if current_table and len(current_table) >= 2:
        tables.append(current_table)

    return tables


def _try_parse_number(text: str) -> float | str:
    """Try to parse a cell value as a number (handling ₹, commas, etc.)."""
    import re
    cleaned = text.strip()
    if not cleaned or cleaned == "-" or cleaned == "—":
        return ""
    # Remove currency symbols and commas
    cleaned = re.sub(r"[₹$€£,\s]", "", cleaned)
    # Remove trailing % for percentage cells
    cleaned = cleaned.rstrip("%")
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return text.strip()


def generate_xlsx_from_content_tables(
    content: str,
    title: Optional[str] = None,
) -> Optional[bytes]:
    """
    Extract markdown tables from free-form content and generate an XLSX workbook.
    Falls back to a single-sheet plain text export if no tables found.
    Returns bytes or None if content is empty.
    """
    if not content or not content.strip():
        return None

    tables = _parse_markdown_tables(content)
    if not tables:
        logger.info("No markdown tables found in content for XLSX generation")
        return None

    wb = Workbook()
    ws = wb.active
    ws.title = (title or "Sheet 1")[:31]  # Excel sheet name limit

    current_row = 1

    # Title row
    if title:
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(tables[0][0]) if tables else 3, 3))
        cell = ws.cell(row=1, column=1, value=title)
        cell.font = Font(name="Calibri", size=14, bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        current_row = 3

    for table_idx, table in enumerate(tables):
        if not table:
            continue

        num_cols = max(len(row) for row in table)

        # Header row (first row of each table)
        header = table[0] if table else []
        for col_idx, cell_val in enumerate(header):
            cell = ws.cell(row=current_row, column=col_idx + 1, value=cell_val)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = THIN_BORDER
        current_row += 1

        # Data rows
        for row_idx, row in enumerate(table[1:]):
            for col_idx in range(num_cols):
                cell_val = row[col_idx] if col_idx < len(row) else ""
                parsed = _try_parse_number(cell_val)
                cell = ws.cell(row=current_row, column=col_idx + 1, value=parsed)
                cell.font = DATA_FONT
                cell.border = THIN_BORDER
                cell.alignment = Alignment(vertical="center", wrap_text=True)

                # Apply number/currency formatting
                if isinstance(parsed, (int, float)):
                    # Check if original had currency symbol
                    if "₹" in cell_val or "Rs" in cell_val.lower():
                        cell.number_format = INR_FORMAT
                    else:
                        cell.number_format = NUMBER_FORMAT

                # Alternating row colors
                if row_idx % 2 == 0:
                    cell.fill = ALT_ROW_FILL

            current_row += 1

        # Auto-width columns
        for col_idx in range(num_cols):
            col_letter = get_column_letter(col_idx + 1)
            max_width = 12
            for row in table:
                if col_idx < len(row):
                    max_width = max(max_width, len(str(row[col_idx])) + 2)
            ws.column_dimensions[col_letter].width = min(max_width, 40)

        # Gap between tables
        current_row += 2

    # Freeze top rows for readability
    ws.freeze_panes = "A4" if title else "A2"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
