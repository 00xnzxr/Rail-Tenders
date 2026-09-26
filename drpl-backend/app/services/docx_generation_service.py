"""
DRPL Backend - DOCX Generation Service
Converts markdown/HTML content into formatted Word documents using python-docx.
"""

import io
import logging
import os
import re
from typing import Optional

from docx import Document
from docx.shared import Pt, Inches, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.section import WD_ORIENT, WD_SECTION

logger = logging.getLogger(__name__)


def _apply_orientation(doc: Document, orientation: str) -> None:
    """Flip every section's page size + orientation to landscape when requested.

    python-docx stores page width/height on the section; switching to landscape
    means swapping them AND setting the orientation flag. Defaults to A4-ish
    (Letter is close enough — we don't introspect original paper size).
    """
    if (orientation or "portrait").lower() != "landscape":
        return
    for section in doc.sections:
        new_width, new_height = section.page_height, section.page_width
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width = new_width
        section.page_height = new_height


def generate_docx_from_markdown(
    markdown_content: str,
    title: Optional[str] = None,
    letterhead_text: Optional[str] = None,
    orientation: str = "portrait",
) -> bytes:
    """
    Convert markdown content to a Word document.
    Returns the DOCX file as bytes.

    ``orientation`` accepts "portrait" (default) or "landscape". Landscape
    is useful for wide inspection tables / schedules with many columns.
    """
    doc = Document()

    # Set default font
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(11)

    # Set margins
    for section in doc.sections:
        section.top_margin = Cm(2.54)
        section.bottom_margin = Cm(2.54)
        section.left_margin = Cm(2.54)
        section.right_margin = Cm(2.54)

    # Apply requested orientation BEFORE adding content so tables auto-fit
    # against the final page width.
    _apply_orientation(doc, orientation)

    # Add letterhead if provided
    if letterhead_text:
        header_para = doc.add_paragraph()
        header_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = header_para.add_run(letterhead_text)
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(100, 100, 100)
        doc.add_paragraph()  # spacer

    # Add title if provided
    if title:
        title_para = doc.add_heading(title, level=0)
        title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # Parse markdown and build document
    _parse_markdown_to_docx(doc, markdown_content)

    # Save to bytes
    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer.read()


def _apply_orientation_to_section(section, orientation: str) -> None:
    """Apply portrait/landscape to a single section (not the whole doc).

    Mirrors ``_apply_orientation`` but operates per-section so a combined
    document can carry annexures of mixed orientation — each annexure lives in
    its own section. Idempotent for portrait (the python-docx default).
    """
    if (orientation or "portrait").lower() != "landscape":
        return
    new_width, new_height = section.page_height, section.page_width
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = new_width
    section.page_height = new_height


def _set_section_margins(section) -> None:
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)


def generate_combined_docx(parts: list[dict]) -> bytes:
    """Build ONE Word document from many annexures, each in its own section.

    ``parts`` is an ordered list of ``{"html": str|None, "markdown": str|None,
    "title": str|None, "orientation": "portrait"|"landscape"}``. HTML is
    preferred when present (it reflects the user's latest editor state, same as
    the PDF path); markdown is the fallback. Each part starts on a new page via
    a fresh section so its page orientation is independent — a landscape
    annexure keeps landscape without flipping its neighbours. This lets the
    client export every annexure as one continuous DOCX with no manual merge.
    """
    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    for idx, part in enumerate(parts):
        html = (part.get("html") or "").strip()
        markdown = (part.get("markdown") or "").strip()
        title = part.get("title")
        orientation = part.get("orientation") or "portrait"

        if idx == 0:
            # Reuse the document's initial section for the first annexure.
            section = doc.sections[0]
        else:
            # New page-break section so this annexure's orientation is its own.
            section = doc.add_section(WD_SECTION.NEW_PAGE)

        _set_section_margins(section)
        _apply_orientation_to_section(section, orientation)

        # Letterhead in DOCX is text-only: a PDF letterhead is an image overlay
        # that python-docx cannot reproduce, so we carry the company name into
        # the section header instead. The PDF export remains the fidelity path.
        letterhead_text = (part.get("letterhead_text") or "").strip()
        if letterhead_text:
            try:
                header_para = section.header.paragraphs[0]
                header_para.text = letterhead_text
                header_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in header_para.runs:
                    run.font.size = Pt(10)
                    run.font.bold = True
            except Exception:
                # A malformed header must not sink the whole export.
                pass

        if title:
            heading = doc.add_heading(title, level=0)
            heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

        if html:
            _parse_html_to_docx(doc, html)
        else:
            _parse_markdown_to_docx(doc, markdown)

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer.read()


def generate_docx_from_html(
    html_content: str,
    title: Optional[str] = None,
    orientation: str = "portrait",
) -> bytes:
    """
    Convert HTML content to a Word document.
    Handles headings, paragraphs, tables, bold, italic, underline.

    ``orientation`` accepts "portrait" (default) or "landscape".
    """
    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    for section in doc.sections:
        section.top_margin = Cm(2.54)
        section.bottom_margin = Cm(2.54)
        section.left_margin = Cm(2.54)
        section.right_margin = Cm(2.54)

    _apply_orientation(doc, orientation)

    if title:
        heading = doc.add_heading(title, level=0)
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    _parse_html_to_docx(doc, html_content)

    buffer = io.BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer.read()


def _parse_markdown_to_docx(doc: Document, content: str):
    """Parse markdown content and add elements to a python-docx Document."""
    lines = content.split("\n")
    i = 0
    table_buffer = []
    in_table = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Heading detection
        if stripped.startswith("#"):
            if in_table:
                _flush_table(doc, table_buffer)
                table_buffer = []
                in_table = False

            level = 0
            for ch in stripped:
                if ch == "#":
                    level += 1
                else:
                    break
            level = min(level, 4)
            heading_text = stripped[level:].strip()
            doc.add_heading(heading_text, level=level)

        # Table row detection
        elif stripped.startswith("|") and stripped.endswith("|"):
            # Skip separator rows (|---|---|)
            if re.match(r'^\|[\s\-:]+\|', stripped):
                i += 1
                continue
            in_table = True
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            table_buffer.append(cells)

        # Horizontal rule
        elif stripped in ("---", "***", "___"):
            if in_table:
                _flush_table(doc, table_buffer)
                table_buffer = []
                in_table = False
            # Add a thin horizontal line via an empty paragraph with bottom border
            doc.add_paragraph()

        # Empty line
        elif not stripped:
            if in_table:
                _flush_table(doc, table_buffer)
                table_buffer = []
                in_table = False

        # Regular paragraph / list item
        else:
            if in_table:
                _flush_table(doc, table_buffer)
                table_buffer = []
                in_table = False

            # List items
            list_match = re.match(r'^(\d+)\.\s+(.*)', stripped)
            bullet_match = re.match(r'^[-*+]\s+(.*)', stripped)

            if list_match:
                para = doc.add_paragraph(style="List Number")
                _add_formatted_text(para, list_match.group(2))
            elif bullet_match:
                para = doc.add_paragraph(style="List Bullet")
                _add_formatted_text(para, bullet_match.group(1))
            else:
                para = doc.add_paragraph()
                _add_formatted_text(para, stripped)

        i += 1

    # Flush remaining table
    if in_table and table_buffer:
        _flush_table(doc, table_buffer)


def _flush_table(doc: Document, rows: list[list[str]]):
    """Convert accumulated table rows into a Word table."""
    if not rows:
        return

    max_cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=max_cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    for row_idx, row_data in enumerate(rows):
        for col_idx, cell_text in enumerate(row_data):
            if col_idx < max_cols:
                cell = table.cell(row_idx, col_idx)
                cell.text = cell_text
                # Bold the header row
                if row_idx == 0:
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.bold = True
                            run.font.size = Pt(10)

    doc.add_paragraph()  # spacer after table


def _add_formatted_text(para, text: str):
    """Add text with markdown inline formatting (bold, italic) to a paragraph."""
    # Split on bold/italic patterns
    parts = re.split(r'(\*\*\*.*?\*\*\*|\*\*.*?\*\*|\*.*?\*|__.*?__|_.*?_)', text)

    for part in parts:
        if not part:
            continue

        if part.startswith("***") and part.endswith("***"):
            run = para.add_run(part[3:-3])
            run.bold = True
            run.italic = True
        elif part.startswith("**") and part.endswith("**"):
            run = para.add_run(part[2:-2])
            run.bold = True
        elif (part.startswith("*") and part.endswith("*")) or (part.startswith("_") and part.endswith("_")):
            run = para.add_run(part[1:-1])
            run.italic = True
        elif part.startswith("__") and part.endswith("__"):
            run = para.add_run(part[2:-2])
            run.bold = True
        else:
            para.add_run(part)


def _parse_html_to_docx(doc: Document, html: str):
    """Basic HTML to DOCX conversion for common elements."""
    # Strip HTML tags and extract structure
    # Use a simple regex approach for common patterns
    html = html.replace("\r\n", "\n").replace("\r", "\n")

    # Extract heading blocks
    for level in range(1, 5):
        pattern = f'<h{level}[^>]*>(.*?)</h{level}>'
        html = re.sub(pattern, lambda m: f"\n###{'#' * (level-1)} {_strip_tags(m.group(1))}\n", html, flags=re.DOTALL | re.IGNORECASE)

    # Extract table blocks
    table_pattern = r'<table[^>]*>(.*?)</table>'
    tables_found = re.findall(table_pattern, html, flags=re.DOTALL | re.IGNORECASE)

    # Replace tables with placeholders
    for idx, table_html in enumerate(tables_found):
        html = html.replace(f"<table{re.search(r'<table([^>]*>)', html, re.IGNORECASE).group(1) if re.search(r'<table([^>]*>)', html, re.IGNORECASE) else '>'}{table_html}</table>", f"\n[TABLE_{idx}]\n", 1)

    # Convert <p>, <li>, <br> to newlines
    html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<p[^>]*>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</p>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<li[^>]*>', '\n- ', html, flags=re.IGNORECASE)
    html = re.sub(r'</li>', '', html, flags=re.IGNORECASE)

    # Handle bold/italic
    html = re.sub(r'<strong[^>]*>(.*?)</strong>', r'**\1**', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<b[^>]*>(.*?)</b>', r'**\1**', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<em[^>]*>(.*?)</em>', r'*\1*', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<i[^>]*>(.*?)</i>', r'*\1*', html, flags=re.DOTALL | re.IGNORECASE)

    # Strip remaining tags
    html = re.sub(r'<[^>]+>', '', html)

    # Now parse as markdown
    _parse_markdown_to_docx(doc, html)

    # Add extracted tables
    for idx, table_html in enumerate(tables_found):
        _add_html_table(doc, table_html)


def _strip_tags(text: str) -> str:
    """Remove HTML tags from text."""
    return re.sub(r'<[^>]+>', '', text).strip()


def _add_html_table(doc: Document, table_html: str):
    """Parse an HTML table and add it to the document."""
    rows_html = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, flags=re.DOTALL | re.IGNORECASE)
    if not rows_html:
        return

    table_data = []
    for row_html in rows_html:
        cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row_html, flags=re.DOTALL | re.IGNORECASE)
        table_data.append([_strip_tags(c) for c in cells])

    if table_data:
        _flush_table(doc, table_data)


def generate_document_from_content(
    content: str,
    title: str,
    output_format: str = "docx",
    content_type: str = "markdown",
) -> bytes:
    """
    Generate a document from content string.
    This is the function referenced in command_center.py for document export.
    """
    if output_format == "docx":
        if content_type == "html":
            return generate_docx_from_html(content, title=title)
        return generate_docx_from_markdown(content, title=title)
    else:
        raise ValueError(f"Unsupported output format: {output_format}")
