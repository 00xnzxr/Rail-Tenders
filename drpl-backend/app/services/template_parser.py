"""
DRPL Backend - Template Parser
Extracts text from uploaded proposal template PDFs and uses AI to parse the structure.
"""

import json
import logging
from typing import Optional

import pdfplumber

from app.services.ai_service import call_ai

logger = logging.getLogger(__name__)


async def parse_uploaded_template(file_path: str, db=None) -> dict:
    """
    Extract text from a PDF template using pdfplumber, then call AI to get a
    structured JSON representation of the template sections, fields, and tables.

    Returns a dict with keys like:
    {
        "sections": [
            {"title": "...", "description": "...", "order": 1, "fields": [...], "tables": [...]}
        ],
        "metadata": {"template_type": "...", "total_pages": N}
    }
    """
    # Extract text from PDF
    extracted_text = ""
    total_pages = 0
    try:
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                extracted_text += f"\n--- Page {page.page_number} ---\n{page_text}"
    except Exception as e:
        logger.error(f"Failed to extract text from PDF {file_path}: {e}")
        return {"sections": [], "metadata": {"error": str(e), "total_pages": 0}}

    if not extracted_text.strip():
        return {"sections": [], "metadata": {"error": "No text extracted from PDF", "total_pages": total_pages}}

    # Truncate to avoid exceeding token limits
    max_chars = 15000
    if len(extracted_text) > max_chars:
        extracted_text = extracted_text[:max_chars] + "\n\n[... truncated ...]"

    # Call AI to parse the structure
    system_prompt = """You are a document structure analyst. Given the text content of a government tender proposal template,
extract a structured JSON representation of the template.

Return ONLY valid JSON with this structure:
{
  "sections": [
    {
      "title": "Section title",
      "description": "What this section should contain",
      "order": 1,
      "fields": [
        {"name": "field_name", "label": "Field Label", "type": "text|number|date|textarea|table", "required": true}
      ],
      "tables": [
        {"name": "table_name", "columns": ["Col1", "Col2", "Col3"], "description": "What data goes here"}
      ]
    }
  ],
  "metadata": {
    "template_type": "works_single_packet|service_two_packet|supply|amc|custom",
    "contract_type": "Works|Service|Supply",
    "bidding_system": "single_packet|two_packet",
    "railway_zone": "zone name if mentioned or null",
    "total_pages": N
  }
}

Be thorough — capture every section, subsection, form field, table, and declaration in the template.
Focus on identifying the structural skeleton that a proposal writer would need to fill in."""

    user_prompt = f"Parse this proposal template ({total_pages} pages):\n\n{extracted_text}"

    try:
        result = await call_ai(system_prompt, user_prompt, db, "template_parser")
        result = result.strip()

        # Strip markdown code fences if present
        if result.startswith("```"):
            result = result.split("\n", 1)[1].rsplit("```", 1)[0]

        structure = json.loads(result)

        # Ensure metadata has total_pages
        if "metadata" not in structure:
            structure["metadata"] = {}
        structure["metadata"]["total_pages"] = total_pages

        return structure

    except json.JSONDecodeError as e:
        logger.error(f"AI returned invalid JSON for template parsing: {e}")
        return {
            "sections": [],
            "metadata": {"error": "AI returned invalid JSON", "total_pages": total_pages},
        }
    except Exception as e:
        logger.error(f"Template parsing AI call failed: {e}")
        return {
            "sections": [],
            "metadata": {"error": str(e), "total_pages": total_pages},
        }


async def parse_pdf_as_costing_template(file_path: str, db=None) -> dict:
    """
    Extract tabular/costing structure from a PDF file.
    Uses pdfplumber to extract tables and text, then AI to identify
    column definitions and footer rows suitable for CostingTemplate.
    """
    extracted_text = ""
    extracted_tables = []
    total_pages = 0

    try:
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                extracted_text += f"\n--- Page {page.page_number} ---\n{page_text}"

                # Also extract tables directly
                tables = page.extract_tables()
                for tbl in tables:
                    if tbl and len(tbl) >= 2:
                        extracted_tables.append(tbl)
    except Exception as e:
        logger.error(f"Failed to extract text from PDF {file_path}: {e}")
        return {"column_definitions": [], "footer_rows": [], "metadata": {"error": str(e)}}

    if not extracted_text.strip() and not extracted_tables:
        return {"column_definitions": [], "footer_rows": [], "metadata": {"error": "No content extracted from PDF"}}

    # Build table representation for AI
    table_text = ""
    if extracted_tables:
        for i, tbl in enumerate(extracted_tables):
            table_text += f"\n\nExtracted Table {i+1}:\n"
            for row in tbl[:20]:  # Limit rows
                table_text += "  | ".join(str(cell or "") for cell in row) + "\n"

    # Truncate
    max_chars = 15000
    combined = extracted_text + table_text
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n[... truncated ...]"

    system_prompt = """You are a financial document analyst specializing in Indian government tender costing formats.
Given the text and table content extracted from a PDF costing/BOQ template, identify the column structure
and footer calculation rows.

Return ONLY valid JSON:
{
  "column_definitions": [
    {"name": "sr_no", "label": "Sr. No.", "type": "serial|text|number|currency|formula|percentage"}
  ],
  "footer_rows": [
    {"label": "Sub Total", "formula": "SUM(amount)", "type": "sum|tax|overhead|margin|grand_total"}
  ],
  "metadata": {
    "format_type": "boq|rate_schedule|cost_statement|price_bid",
    "zone": "railway zone if identifiable or GENERAL",
    "description": "Brief description of this costing format",
    "total_pages": """ + str(total_pages) + """
  }
}

Instructions:
- Look for table headers (columns like Sr. No., Description, Qty, Unit, Rate, Amount, etc.)
- Identify footer rows that contain totals, subtotals, GST, tax, grand total, overhead, profit, etc.
- Infer column types: serial (row numbers), text (descriptions), number (quantities), currency (amounts/rates), percentage (GST/tax rates)
- If the PDF contains multiple tables, focus on the primary costing/BOQ table
- Preserve the original column labels from the document"""

    user_prompt = f"Extract costing template structure from this PDF ({total_pages} pages):\n\n{combined}"

    try:
        result = await call_ai(system_prompt, user_prompt, db, "template_parser")
        result = result.strip()
        if result.startswith("```"):
            result = result.split("\n", 1)[1].rsplit("```", 1)[0]

        structure = json.loads(result)

        if "column_definitions" not in structure:
            structure["column_definitions"] = []
        if "footer_rows" not in structure:
            structure["footer_rows"] = []
        if "metadata" not in structure:
            structure["metadata"] = {}
        structure["metadata"]["total_pages"] = total_pages

        return structure

    except json.JSONDecodeError:
        logger.error("AI returned invalid JSON for costing PDF parsing")
        return {"column_definitions": [], "footer_rows": [], "metadata": {"error": "AI returned invalid JSON", "total_pages": total_pages}}
    except Exception as e:
        logger.error(f"Costing PDF template parsing failed: {e}")
        return {"column_definitions": [], "footer_rows": [], "metadata": {"error": str(e), "total_pages": total_pages}}


async def parse_template_file(file_path: str, template_kind: str, db=None) -> dict:
    """
    Dispatch template parsing to the correct parser based on file extension
    and template kind. Supports PDF, DOCX, and XLSX files.
    """
    import os
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        if template_kind == "costing":
            return await parse_pdf_as_costing_template(file_path, db)
        return await parse_uploaded_template(file_path, db)
    elif ext == ".docx":
        from app.services.template_parser_docx import parse_docx_template
        return await parse_docx_template(file_path, db)
    elif ext in (".xlsx", ".xls"):
        from app.services.template_parser_xlsx import parse_xlsx_template
        return await parse_xlsx_template(file_path, db)
    else:
        return {"sections": [], "metadata": {"error": f"Unsupported file type: {ext}"}}


def get_template_prompt_context(template) -> str:
    """
    Convert a ProposalTemplate's structure_json into a text block suitable
    for injection into the AI system prompt.
    """
    structure = template.structure_json or {}
    sections = structure.get("sections", [])

    if not sections:
        return f"Template: {template.name}\nNo parsed structure available."

    lines = [
        f"## PROPOSAL TEMPLATE: {template.name}",
        f"Type: {template.template_type} | Source: {template.source}",
    ]

    if template.description:
        lines.append(f"Description: {template.description}")

    metadata = structure.get("metadata", {})
    if metadata.get("contract_type"):
        lines.append(f"Contract Type: {metadata['contract_type']}")
    if metadata.get("bidding_system"):
        lines.append(f"Bidding System: {metadata['bidding_system']}")
    if metadata.get("railway_zone"):
        lines.append(f"Railway Zone: {metadata['railway_zone']}")

    lines.append("")
    lines.append("### REQUIRED SECTIONS:")

    for section in sorted(sections, key=lambda s: s.get("order", 0)):
        order = section.get("order", "?")
        title = section.get("title", "Untitled")
        desc = section.get("description", "")
        lines.append(f"\n**{order}. {title}**")
        if desc:
            lines.append(f"   {desc}")

        # Fields
        fields = section.get("fields", [])
        if fields:
            for f in fields:
                req = " (required)" if f.get("required") else ""
                lines.append(f"   - {f.get('label', f.get('name', '?'))} [{f.get('type', 'text')}]{req}")

        # Tables
        tables = section.get("tables", [])
        if tables:
            for t in tables:
                cols = ", ".join(t.get("columns", []))
                lines.append(f"   - TABLE: {t.get('name', '?')} | Columns: {cols}")
                if t.get("description"):
                    lines.append(f"     {t['description']}")

    return "\n".join(lines)
