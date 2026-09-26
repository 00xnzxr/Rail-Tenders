"""
DRPL Backend - PDF Generation Service
Generates professional PDFs on company letterhead with digital signatures.
Uses WeasyPrint for HTML→PDF and ReportLab for signature overlay.
All binary assets (letterhead PDFs, header/footer/watermark images, signature
images, generated PDFs) live in Cloudflare R2 — paths stored in the DB are
storage keys, not filesystem paths.
"""

import base64
import contextlib
import io
import logging
import mimetypes
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.letterhead import LetterheadTemplate, DigitalSignature, GeneratedDocument
from app.models.proposal import ProposalMessage
from app.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)


def _generated_doc_key(document_id: int, file_name: str) -> str:
    return f"generated_documents/{document_id}/{file_name}"


def _image_key_to_data_uri(key: str) -> Optional[str]:
    """Download an image from storage and return it as a base64 data URI.

    Returns None if the object is missing or unreadable — caller should
    fall back to omitting the image.
    """
    if not key:
        return None
    try:
        storage = get_storage_service()
        data = storage.download_file_sync(key)
    except Exception as e:
        logger.warning(f"[DRPL] Could not fetch image asset '{key}': {e}")
        return None
    mime = mimetypes.guess_type(key)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def generate_pdf(db: Session, document_id: int, orientation: Optional[str] = None) -> str:
    """Generate a PDF from a GeneratedDocument with letterhead and signatures.

    ``orientation`` explicitly overrides the document's stored ``page_orientation``;
    when omitted, the document's own ``page_orientation`` column is used.
    Accepts "portrait" (default) or "landscape" — drives the CSS @page size
    so wide annexure tables render on landscape pages.
    """
    doc = db.query(GeneratedDocument).filter(GeneratedDocument.id == document_id).first()
    if not doc:
        raise ValueError(f"Document {document_id} not found")
    effective_orientation = (orientation or getattr(doc, "page_orientation", None) or "portrait")

    # Get letterhead template
    letterhead = None
    if doc.letterhead_template_id:
        letterhead = db.query(LetterheadTemplate).filter(
            LetterheadTemplate.id == doc.letterhead_template_id
        ).first()

    # Build HTML content — convert markdown to HTML if needed
    content = doc.content_html
    if not content and doc.content_markdown:
        content = _markdown_to_html(doc.content_markdown)
        logger.info("[DRPL] Generate: converted markdown to HTML (%d chars)", len(content))
    content = content or ""

    logger.info("[DRPL] Generate PDF doc #%d: content_len=%d, letterhead=%s",
                document_id, len(content), bool(letterhead))

    html_content = _build_html_document(
        content=content,
        template_vars=doc.template_variables or {},
        letterhead=letterhead,
        orientation=effective_orientation,
    )

    # Generate content PDF via WeasyPrint or ReportLab fallback
    pdf_bytes = _render_html_to_pdf(html_content, orientation=effective_orientation)

    # Validate PDF bytes — generate error PDF if empty
    if not pdf_bytes or len(pdf_bytes) < 100:
        logger.error("[DRPL] PDF rendering produced empty output, generating error PDF")
        pdf_bytes = _generate_error_pdf("PDF rendering failed. Please check server logs.")

    storage = get_storage_service()

    # If a full PDF letterhead is uploaded, merge content onto it
    if letterhead and letterhead.letterhead_pdf_path:
        with contextlib.ExitStack() as stack:
            local_lh = _resolve_letterhead_pdf(stack, storage, letterhead.letterhead_pdf_path)
            if local_lh:
                pdf_bytes = _merge_content_onto_letterhead(pdf_bytes, local_lh)

    # Apply signatures if any
    signatures_config = doc.signatures or []
    if signatures_config:
        signature_records = []
        for sig_config in signatures_config:
            sig = db.query(DigitalSignature).filter(
                DigitalSignature.id == sig_config.get("signature_id")
            ).first()
            if sig:
                signature_records.append({
                    "signature": sig,
                    "position": sig_config.get("position", sig.default_position),
                    "position_x": sig_config.get("position_x", sig.default_position_x),
                    "position_y": sig_config.get("position_y", sig.default_position_y),
                    "page": sig_config.get("page", "last"),
                })

        if signature_records:
            pdf_bytes = _apply_signatures(pdf_bytes, signature_records)

    # Persist the generated PDF to R2
    file_name = f"doc_{document_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    storage_key = _generated_doc_key(document_id, file_name)
    storage.upload_file_sync(storage_key, pdf_bytes, content_type="application/pdf")

    # Clean up any previously generated PDF for this document
    if doc.generated_file_path and doc.generated_file_path != storage_key:
        try:
            storage.delete_file_sync(doc.generated_file_path)
        except Exception as e:
            logger.warning(f"[DRPL] failed to delete previous generated PDF {doc.generated_file_path}: {e}")

    doc.generated_file_path = storage_key
    doc.generated_file_name = file_name
    doc.file_size = len(pdf_bytes)
    doc.status = "generated"
    db.commit()
    db.refresh(doc)

    return storage_key


def generate_preview(db: Session, document_id: int, orientation: Optional[str] = None) -> bytes:
    """Generate preview PDF bytes without persisting.

    ``orientation`` overrides the document's stored ``page_orientation`` when
    provided (e.g. the editor preview sends the currently-selected toggle
    state before the user hits Save).
    """
    doc = db.query(GeneratedDocument).filter(GeneratedDocument.id == document_id).first()
    if not doc:
        raise ValueError(f"Document {document_id} not found")
    effective_orientation = (orientation or getattr(doc, "page_orientation", None) or "portrait")

    letterhead = None
    if doc.letterhead_template_id:
        letterhead = db.query(LetterheadTemplate).filter(
            LetterheadTemplate.id == doc.letterhead_template_id
        ).first()

    # Convert markdown to HTML if needed
    content = doc.content_html
    if not content and doc.content_markdown:
        content = _markdown_to_html(doc.content_markdown)
        logger.info("[DRPL] Preview: converted markdown to HTML (%d chars → %d chars)",
                     len(doc.content_markdown), len(content))
    content = content or ""

    logger.info("[DRPL] Preview doc #%d: content_html=%s, content_markdown=%s, content_len=%d, letterhead=%s",
                document_id,
                bool(doc.content_html), bool(doc.content_markdown),
                len(content),
                bool(letterhead))

    html_content = _build_html_document(
        content=content,
        template_vars=doc.template_variables or {},
        letterhead=letterhead,
        orientation=effective_orientation,
    )

    pdf_bytes = _render_html_to_pdf(html_content, orientation=effective_orientation)

    # Validate PDF bytes — generate error PDF if empty
    if not pdf_bytes or len(pdf_bytes) < 100:
        logger.error("[DRPL] PDF rendering produced empty output, generating error PDF")
        pdf_bytes = _generate_error_pdf("PDF rendering failed. Please check server logs.")

    # Merge onto PDF letterhead if available
    if letterhead and letterhead.letterhead_pdf_path:
        storage = get_storage_service()
        with contextlib.ExitStack() as stack:
            local_lh = _resolve_letterhead_pdf(stack, storage, letterhead.letterhead_pdf_path)
            if local_lh:
                pdf_bytes = _merge_content_onto_letterhead(pdf_bytes, local_lh)

    # Apply digital signatures (same logic as generate_pdf)
    signatures_config = doc.signatures or []
    if signatures_config:
        signature_records = []
        for sig_config in signatures_config:
            sig = db.query(DigitalSignature).filter(
                DigitalSignature.id == sig_config.get("signature_id")
            ).first()
            if sig:
                signature_records.append({
                    "signature": sig,
                    "position": sig_config.get("position", sig.default_position),
                    "position_x": sig_config.get("position_x", sig.default_position_x),
                    "position_y": sig_config.get("position_y", sig.default_position_y),
                    "page": sig_config.get("page", "last"),
                })
        if signature_records:
            pdf_bytes = _apply_signatures(pdf_bytes, signature_records)

    return pdf_bytes


def generate_workspace_preview(
    db: Session,
    tender_id: int,
    item_id: int,
) -> bytes:
    """Render a PDF preview from a DocumentWorkspace draft, with no
    GeneratedDocument required.

    Used by the workspace editor's right-side PDF preview pane so the user
    can see the live document — content + letterhead + signatures —
    before they hit Finalize. Reads draft content directly off the
    workspace row, so unsaved-in-editor content (still in React state) will
    not appear; the caller should flush any pending auto-save first.
    """
    from app.models.workspace import DocumentWorkspace
    from app.models.checklist import ChecklistItem

    item = db.query(ChecklistItem).filter(
        ChecklistItem.id == item_id,
        ChecklistItem.tender_id == tender_id,
    ).first()
    if not item:
        raise ValueError(f"Checklist item {item_id} not found for tender {tender_id}")

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id,
        DocumentWorkspace.tender_id == tender_id,
    ).first()
    if not ws:
        raise ValueError(f"Document workspace not found for checklist item {item_id}")

    effective_orientation = (ws.page_orientation or "portrait").lower()

    # Letterhead resolution, most specific first:
    #   1. per-document override (ws.letterhead_template_id)
    #   2. per-tender default (WorkspaceConfig.default_letterhead_id)
    # Annexure rows created by annexure_finder never set (1), so without the (2)
    # fallback every combined annexure export rendered with no letterhead at all.
    # `letterhead_disabled` is an explicit per-document opt-out that must beat the
    # tender default — used for forms that belong on a bank's or CA's letterhead.
    letterhead = None
    if not bool(getattr(ws, "letterhead_disabled", False)):
        letterhead_id = ws.letterhead_template_id
        if not letterhead_id:
            from app.models.workspace import WorkspaceConfig
            cfg = db.query(WorkspaceConfig).filter(
                WorkspaceConfig.tender_id == tender_id
            ).first()
            letterhead_id = cfg.default_letterhead_id if cfg else None
        if letterhead_id:
            letterhead = db.query(LetterheadTemplate).filter(
                LetterheadTemplate.id == letterhead_id
            ).first()

    content = ws.draft_content_html
    if not content and ws.draft_content_markdown:
        content = _markdown_to_html(ws.draft_content_markdown)
    content = (content or "").strip()
    if not content:
        # Render an explicit placeholder PDF so the iframe shows something
        # rather than failing or rendering a blank A4. Lets the user know
        # the preview is wired up; they just need to add content first.
        content = (
            '<p style="text-align:center;color:#999;margin-top:80px;font-style:italic;">'
            "No content yet — write or AI-generate the document body to see it here."
            "</p>"
        )

    logger.info(
        "[DRPL] Workspace preview tender=%d item=%d: content_len=%d, letterhead=%s, orientation=%s",
        tender_id, item_id, len(content), bool(letterhead), effective_orientation,
    )

    html_content = _build_html_document(
        content=content,
        template_vars={},
        letterhead=letterhead,
        orientation=effective_orientation,
    )

    pdf_bytes = _render_html_to_pdf(html_content, orientation=effective_orientation)

    if not pdf_bytes or len(pdf_bytes) < 100:
        logger.error("[DRPL] Workspace preview PDF rendering produced empty output")
        pdf_bytes = _generate_error_pdf("PDF rendering failed. Please check server logs.")

    # Merge onto PDF letterhead if available (same path as generate_pdf).
    if letterhead and letterhead.letterhead_pdf_path:
        storage = get_storage_service()
        with contextlib.ExitStack() as stack:
            local_lh = _resolve_letterhead_pdf(stack, storage, letterhead.letterhead_pdf_path)
            if local_lh:
                pdf_bytes = _merge_content_onto_letterhead(pdf_bytes, local_lh)

    # Apply signatures from workspace.signatures_json (same shape as
    # GeneratedDocument.signatures — list of dicts with signature_id,
    # position, position_x, position_y, page).
    signatures_config = ws.signatures_json or []
    if signatures_config:
        signature_records = []
        for sig_config in signatures_config:
            sig = db.query(DigitalSignature).filter(
                DigitalSignature.id == sig_config.get("signature_id")
            ).first()
            if sig:
                signature_records.append({
                    "signature": sig,
                    "position": sig_config.get("position", sig.default_position),
                    "position_x": sig_config.get("position_x", sig.default_position_x),
                    "position_y": sig_config.get("position_y", sig.default_position_y),
                    "page": sig_config.get("page", "last"),
                })
        if signature_records:
            pdf_bytes = _apply_signatures(pdf_bytes, signature_records)

    return pdf_bytes


def generate_letterhead_preview(db: Session, template_id: int) -> bytes:
    """Generate a blank letterhead preview."""
    letterhead = db.query(LetterheadTemplate).filter(
        LetterheadTemplate.id == template_id
    ).first()
    if not letterhead:
        raise ValueError(f"Template {template_id} not found")

    # If a full PDF letterhead is uploaded, return it directly as preview
    if letterhead.letterhead_pdf_path:
        try:
            return get_storage_service().download_file_sync(letterhead.letterhead_pdf_path)
        except Exception as e:
            logger.warning(
                f"[DRPL] Could not fetch letterhead PDF '{letterhead.letterhead_pdf_path}': {e}"
            )

    sample_content = """
    <p style="text-align: center; color: #999; margin-top: 100px; font-size: 18px;">
        Letterhead Preview
    </p>
    <p style="text-align: center; color: #ccc; font-size: 14px;">
        This is a sample preview of how documents will appear on this letterhead template.
    </p>
    """

    html_content = _build_html_document(
        content=sample_content,
        template_vars={"date": datetime.now().strftime("%d %B %Y")},
        letterhead=letterhead,
    )

    return _render_html_to_pdf(html_content)


def generate_proposal_pdf(
    db: Session, session_id: int, letterhead_id: Optional[int] = None,
    signature_ids: Optional[list[int]] = None
) -> GeneratedDocument:
    """Create a GeneratedDocument from a proposal session and generate its PDF."""
    from app.models.proposal import ProposalSession

    session = db.query(ProposalSession).filter(ProposalSession.id == session_id).first()
    if not session:
        raise ValueError(f"Proposal session {session_id} not found")

    # Collect assistant messages as proposal content
    messages = db.query(ProposalMessage).filter(
        ProposalMessage.session_id == session_id,
        ProposalMessage.role == "assistant",
    ).order_by(ProposalMessage.created_at).all()

    content_parts = [msg.content for msg in messages if msg.content]
    content = "\n\n".join(content_parts)

    # Convert markdown to basic HTML
    content_html = _markdown_to_html(content)

    # Build signatures config
    sigs = []
    if signature_ids:
        for sid in signature_ids:
            sig = db.query(DigitalSignature).filter(DigitalSignature.id == sid).first()
            if sig:
                sigs.append({"signature_id": sid, "position": sig.default_position, "page": "last"})

    # Create document
    doc = GeneratedDocument(
        title=session.title or f"Proposal - Session {session_id}",
        document_type="proposal",
        tender_id=session.tender_id,
        proposal_session_id=session_id,
        letterhead_template_id=letterhead_id,
        content_html=content_html,
        content_markdown=content,
        template_variables={
            "date": datetime.now().strftime("%d %B %Y"),
            "ref_number": f"DRPL/PROP/{session_id}/{datetime.now().strftime('%Y')}",
        },
        signatures=sigs,
        created_by=session.created_by,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Generate the PDF
    generate_pdf(db, doc.id)
    db.refresh(doc)

    return doc


# --- Internal Helpers ---

def _resolve_letterhead_pdf(stack, storage, key: str) -> Optional[str]:
    """Fetch a letterhead PDF from storage into a temp file for the duration of
    the calling `ExitStack`. Returns the local path, or None on failure."""
    if not key:
        return None
    try:
        return stack.enter_context(storage.as_local_file(key, suffix=".pdf"))
    except Exception as e:
        logger.warning(f"[DRPL] Could not fetch letterhead PDF '{key}': {e}")
        return None


def _merge_content_onto_letterhead(content_pdf_bytes: bytes, letterhead_pdf_path: str) -> bytes:
    """Merge content PDF pages onto the uploaded PDF letterhead.

    Each content page is overlaid on top of the letterhead PDF page.
    If the content has more pages than the letterhead, the last letterhead page is reused.
    """
    try:
        from PyPDF2 import PdfReader, PdfWriter

        letterhead_reader = PdfReader(letterhead_pdf_path)
        content_reader = PdfReader(io.BytesIO(content_pdf_bytes))
        writer = PdfWriter()

        letterhead_pages = len(letterhead_reader.pages)

        for i, content_page in enumerate(content_reader.pages):
            # Use matching letterhead page, or last page if content has more pages
            lh_index = min(i, letterhead_pages - 1)
            import copy
            lh_page = copy.deepcopy(letterhead_reader.pages[lh_index])

            # Merge content on top of letterhead
            lh_page.merge_page(content_page)
            writer.add_page(lh_page)

        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    except ImportError:
        logger.warning("[DRPL] PyPDF2 not available for letterhead merge. Returning content PDF as-is.")
        return content_pdf_bytes
    except Exception as e:
        logger.error(f"[DRPL] Failed to merge content onto letterhead PDF: {e}")
        return content_pdf_bytes

def _build_html_document(
    content: str,
    template_vars: dict,
    letterhead: Optional[LetterheadTemplate] = None,
    orientation: str = "portrait",
) -> str:
    """Build a full HTML document with letterhead CSS.

    ``orientation`` controls the CSS ``@page size``. Passing "landscape" emits
    ``size: A4 landscape`` so WeasyPrint (and the ReportLab fallback) lays out
    the page wider than it is tall — required for wide inspection tables.
    """
    # Replace template variables in content
    for key, value in template_vars.items():
        content = content.replace(f"{{{{{key}}}}}", str(value))

    # Default styles
    font_family = "Times New Roman, serif"
    font_size = 12
    line_height = 1.5
    margin_top = 30
    margin_bottom = 25
    margin_left = 20
    margin_right = 20
    header_html = ""
    footer_html = ""
    watermark_css = ""
    custom_css = ""

    if letterhead:
        font_family = letterhead.font_family or font_family
        font_size = letterhead.font_size_pt or font_size
        line_height = letterhead.line_height or line_height
        if letterhead.margin_top_mm is not None:
            margin_top = letterhead.margin_top_mm
        if letterhead.margin_bottom_mm is not None:
            margin_bottom = letterhead.margin_bottom_mm
        if letterhead.margin_left_mm is not None:
            margin_left = letterhead.margin_left_mm
        if letterhead.margin_right_mm is not None:
            margin_right = letterhead.margin_right_mm

        if letterhead.header_html:
            header_html = letterhead.header_html
        elif letterhead.header_image_path:
            header_uri = _image_key_to_data_uri(letterhead.header_image_path)
            if header_uri:
                header_html = f'<img src="{header_uri}" style="display: block; width: 100%; max-height: 80px; object-fit: contain; margin: 0 auto;" />'

        if letterhead.footer_html:
            footer_html = letterhead.footer_html
        elif letterhead.footer_image_path:
            footer_uri = _image_key_to_data_uri(letterhead.footer_image_path)
            if footer_uri:
                footer_html = f'<img src="{footer_uri}" style="display: block; width: 100%; max-height: 50px; object-fit: contain; margin: 0 auto;" />'

        if letterhead.watermark_image_path:
            watermark_uri = _image_key_to_data_uri(letterhead.watermark_image_path)
            if watermark_uri:
                opacity = letterhead.watermark_opacity or 0.1
                watermark_css = f"""
                @page {{
                    background-image: url('{watermark_uri}');
                    background-repeat: no-repeat;
                    background-position: center center;
                    background-size: 50%;
                }}
                """

        if letterhead.css_overrides:
            custom_css = letterhead.css_overrides

    # Template variables display
    tv_html = ""
    if template_vars.get("date") or template_vars.get("ref_number"):
        tv_html += '<div style="display: flex; justify-content: space-between; margin-bottom: 10px;">'
        if template_vars.get("ref_number"):
            tv_html += f'<div><strong>Ref:</strong> {template_vars["ref_number"]}</div>'
        else:
            tv_html += '<div></div>'
        if template_vars.get("date"):
            tv_html += f'<div style="text-align: right;"><strong>Date:</strong> {template_vars["date"]}</div>'
        tv_html += '</div>'
    if template_vars.get("addressee"):
        tv_html += f'<div style="margin-bottom: 10px; white-space: pre-line;">{template_vars["addressee"]}</div>'
    if template_vars.get("subject"):
        tv_html += f'<div style="margin-bottom: 15px;"><strong>Subject:</strong> <u>{template_vars["subject"]}</u></div>'

    # When using a PDF letterhead, skip running header/footer elements
    # (the letterhead PDF already has them) and ensure transparent background.

    # Resolve and validate the PDF letterhead file path (normalize for Windows).
    # All asset paths are now R2 keys. The HTML rendering path below only needs
    # to know whether a key is set — `_image_key_to_data_uri` handles the actual
    # fetch + embed and silently degrades if the object is missing. PDF-letterhead
    # merging happens downstream in generate_pdf/generate_preview, not here.
    has_pdf_letterhead = bool(letterhead and letterhead.letterhead_pdf_path)
    is_pdf_letterhead_type = has_pdf_letterhead

    has_image_header = bool(
        letterhead and (letterhead.header_html or letterhead.header_image_path)
    )

    logger.info(
        "[DRPL] _build_html_document: has_pdf_letterhead=%s, is_pdf_letterhead_type=%s, "
        "has_image_header=%s, margin_top_before=%s",
        has_pdf_letterhead, is_pdf_letterhead_type, has_image_header, margin_top,
    )

    # Ensure content never overlaps the letterhead header.
    # PDF-type letterheads (DRPL style) typically have headers ~40-50mm tall → 55mm page margin.
    # Image/HTML running headers are rendered in the margin area → 40mm minimum.
    lh_top_spacer = ""
    if is_pdf_letterhead_type:
        if margin_top < 55:
            margin_top = 55
    elif has_image_header:
        if margin_top < 40:
            margin_top = 40

    logger.info("[DRPL] _build_html_document: margin_top_final=%s, spacer=%s", margin_top, bool(lh_top_spacer))

    # CSS token for @page size — "" = portrait (default), "landscape" adds
    # the keyword after the paper size.
    page_orientation_css = "landscape" if (orientation or "").lower() == "landscape" else ""

    if has_pdf_letterhead:
        page_header_footer_css = ""  # No running elements — letterhead PDF has them
        header_footer_body_css = ""
        body_background = "background: transparent;"
    else:
        page_header_footer_css = f"""
        @top-center {{
            content: element(header);
        }}
        @bottom-center {{
            content: element(footer);
        }}"""
        header_footer_body_css = f"""
    .header {{
        position: running(header);
        width: 100%;
        text-align: center;
    }}

    .footer {{
        position: running(footer);
        width: 100%;
        text-align: center;
        font-size: 9pt;
        color: #666;
    }}"""
        body_background = ""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    @page {{
        size: A4 {page_orientation_css};
        margin: {margin_top}mm {margin_right}mm {margin_bottom}mm {margin_left}mm;
        {page_header_footer_css}
    }}

    {watermark_css}

    body {{
        font-family: {font_family};
        font-size: {font_size}pt;
        line-height: {line_height};
        color: #333;
        {body_background}
    }}

    {header_footer_body_css}

    h1 {{ font-size: 18pt; margin-bottom: 10px; }}
    h2 {{ font-size: 15pt; margin-bottom: 8px; }}
    h3 {{ font-size: 13pt; margin-bottom: 6px; }}

    table {{
        width: 100%;
        border-collapse: collapse;
        margin: 10px 0;
    }}
    th, td {{
        border: 1px solid #ddd;
        padding: 6px 8px;
        text-align: left;
        font-size: 10pt;
    }}
    th {{
        background: #f5f5f5;
        font-weight: bold;
    }}

    {custom_css}
</style>
</head>
<body>
    {"" if has_pdf_letterhead else f'<div class="header">{header_html}</div>'}
    {"" if has_pdf_letterhead else f'<div class="footer">{footer_html}</div>'}
    {lh_top_spacer}
    <div class="content">
        {tv_html}
        {content}
    </div>
</body>
</html>"""

    return html


def _render_html_to_pdf(html: str, orientation: str = "portrait") -> bytes:
    """Render HTML to PDF using WeasyPrint, with ReportLab fallback.

    WeasyPrint picks up the CSS ``@page size`` directly, so the
    orientation set in the HTML already flows through. The ReportLab
    fallback doesn't parse CSS and needs the orientation passed explicitly,
    plus has its own table-rendering path (see _parse_html_table_to_flowable).
    """
    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html).write_pdf()
        if pdf_bytes and len(pdf_bytes) > 100:
            logger.info("[DRPL] PDF backend=WeasyPrint (%d bytes)", len(pdf_bytes))
            return pdf_bytes
        logger.warning("[DRPL] WeasyPrint returned empty PDF, falling back to ReportLab")
    except ImportError:
        logger.warning(
            "[DRPL] WeasyPrint not installed — using ReportLab fallback. "
            "Install WeasyPrint for richer CSS support: pip install weasyprint"
        )
    except Exception as e:
        logger.error(f"[DRPL] WeasyPrint rendering failed: {e}. Falling back to ReportLab.")

    pdf_bytes = _fallback_pdf_generation(html, orientation=orientation)
    logger.info("[DRPL] PDF backend=ReportLab (%d bytes)", len(pdf_bytes))
    return pdf_bytes


def _extract_body_content(html: str) -> str:
    """Extract only the content inside <div class='content'> or <body>, stripping <style>, <head>, and running elements."""
    import re

    # Try to extract content div first (most precise)
    content_match = re.search(
        r'<div\s+class="content"[^>]*>(.*?)</div>\s*</body>',
        html, re.DOTALL | re.IGNORECASE
    )
    if content_match:
        return content_match.group(1).strip()

    # Fall back to extracting body content
    body_match = re.search(r'<body[^>]*>(.*?)</body>', html, re.DOTALL | re.IGNORECASE)
    body_html = body_match.group(1) if body_match else html

    # Remove header/footer running elements (they're for WeasyPrint only)
    body_html = re.sub(r'<div\s+class="header"[^>]*>.*?</div>', '', body_html, flags=re.DOTALL | re.IGNORECASE)
    body_html = re.sub(r'<div\s+class="footer"[^>]*>.*?</div>', '', body_html, flags=re.DOTALL | re.IGNORECASE)

    # Remove any remaining <style> blocks
    body_html = re.sub(r'<style[^>]*>.*?</style>', '', body_html, flags=re.DOTALL | re.IGNORECASE)

    return body_html.strip()


def _parse_html_table_to_flowable(table_html: str, available_width_pt: float, styles):
    """Parse a single ``<table>...</table>`` HTML block into a ReportLab Table
    flowable so the PDF preserves the tabular layout.

    Falls back to ``None`` on any parse error — caller should treat as plain
    text in that case.
    """
    try:
        from reportlab.platypus import Table, TableStyle, Paragraph
        from reportlab.lib import colors
        import re

        # Pull rows in order — capture both <tr>...</tr> blocks
        row_html_list = re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", table_html, flags=re.IGNORECASE)
        if not row_html_list:
            return None

        rows: list[list] = []
        is_header_row: list[bool] = []
        for row_html in row_html_list:
            cell_matches = re.findall(
                r"<(th|td)[^>]*>([\s\S]*?)</\1>",
                row_html,
                flags=re.IGNORECASE,
            )
            if not cell_matches:
                continue
            row_cells = []
            row_is_header = any(tag.lower() == "th" for tag, _ in cell_matches)
            is_header_row.append(row_is_header)
            for tag, raw_content in cell_matches:
                # Convert nested <br> to newlines, strip non-bold/italic tags,
                # then wrap in a Paragraph so long text wraps inside the cell.
                cleaned = re.sub(r"<br\s*/?>", "<br/>", raw_content, flags=re.IGNORECASE)
                cleaned = cleaned.replace("<strong>", "<b>").replace("</strong>", "</b>")
                cleaned = cleaned.replace("<em>", "<i>").replace("</em>", "</i>")
                cleaned = re.sub(r"<(?!/?(?:b|i|u|br)\b)[^>]+>", "", cleaned)
                cleaned = re.sub(r"&(?!amp;|lt;|gt;|quot;|#\d+;)", "&amp;", cleaned)
                cleaned = cleaned.strip() or "&nbsp;"
                style = styles.get("DocTableHeader") if tag.lower() == "th" else styles["DocTableCell"]
                try:
                    row_cells.append(Paragraph(cleaned, style))
                except Exception:
                    plain = re.sub(r"<[^>]+>", "", cleaned).strip() or " "
                    row_cells.append(plain)
            rows.append(row_cells)

        if not rows:
            return None

        # Equal column widths fitted to the available content width.
        max_cols = max(len(r) for r in rows)
        # Pad short rows with empty strings so the grid is rectangular
        for r in rows:
            while len(r) < max_cols:
                r.append("")
        col_width = available_width_pt / max_cols

        tbl = Table(rows, colWidths=[col_width] * max_cols, repeatRows=1)
        ts = TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
        # Shade header rows
        for i, was_header in enumerate(is_header_row):
            if was_header:
                ts.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f5f5f5"))
        tbl.setStyle(ts)
        return tbl
    except Exception as e:
        logger.warning(f"[DRPL] Table parse failed, falling back to text: {e}")
        return None


def _fallback_pdf_generation(html: str, orientation: str = "portrait") -> bytes:
    """Fallback PDF generation using ReportLab when WeasyPrint is unavailable."""
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.units import mm
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib.enums import TA_LEFT, TA_RIGHT
        from reportlab.lib.colors import HexColor
        import re

        page_size = landscape(A4) if (orientation or "").lower() == "landscape" else A4

        # Extract margins from the @page CSS rule so ReportLab honours the same
        # spacing that was computed in _build_html_document (e.g. the 85mm top
        # margin for PDF-letterhead documents).
        page_margin_match = re.search(
            r'margin:\s*([\d.]+)mm\s+([\d.]+)mm\s+([\d.]+)mm\s+([\d.]+)mm',
            html,
        )
        if page_margin_match:
            rl_top    = float(page_margin_match.group(1))
            rl_right  = float(page_margin_match.group(2))
            rl_bottom = float(page_margin_match.group(3))
            rl_left   = float(page_margin_match.group(4))
        else:
            rl_top, rl_right, rl_bottom, rl_left = 30, 20, 25, 20

        logger.info(
            "[DRPL] ReportLab fallback: using margins top=%smm right=%smm bottom=%smm left=%smm",
            rl_top, rl_right, rl_bottom, rl_left,
        )

        # Extract only the body/content HTML (skip CSS, head, running elements)
        body_html = _extract_body_content(html)
        logger.info("[DRPL] ReportLab fallback: extracted body content (%d chars)", len(body_html))

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, pagesize=page_size,
            leftMargin=rl_left * mm, rightMargin=rl_right * mm,
            topMargin=rl_top * mm, bottomMargin=rl_bottom * mm,
        )

        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(
            name='DocBody', parent=styles['Normal'],
            fontSize=12, leading=18, spaceAfter=6,
            textColor=HexColor('#333333'),
        ))
        styles.add(ParagraphStyle(
            name='DocH1', parent=styles['Heading1'],
            fontSize=18, leading=22, spaceAfter=12,
            textColor=HexColor('#111111'),
        ))
        styles.add(ParagraphStyle(
            name='DocH2', parent=styles['Heading2'],
            fontSize=15, leading=19, spaceAfter=10,
            textColor=HexColor('#222222'),
        ))
        styles.add(ParagraphStyle(
            name='DocH3', parent=styles['Heading3'],
            fontSize=13, leading=17, spaceAfter=8,
            textColor=HexColor('#333333'),
        ))
        styles.add(ParagraphStyle(
            name='DocRight', parent=styles['Normal'],
            fontSize=12, leading=18, spaceAfter=6,
            alignment=TA_RIGHT, textColor=HexColor('#333333'),
        ))
        # Table cell paragraph styles — used by _parse_html_table_to_flowable
        # so cell text wraps inside the cell instead of overflowing.
        styles.add(ParagraphStyle(
            name='DocTableCell', parent=styles['Normal'],
            fontSize=9, leading=11, textColor=HexColor('#333333'),
        ))
        styles.add(ParagraphStyle(
            name='DocTableHeader', parent=styles['Normal'],
            fontSize=9, leading=11, textColor=HexColor('#111111'),
            fontName='Helvetica-Bold',
        ))

        # Width available for tables = page width minus left+right margins
        available_width_pt = page_size[0] - (rl_left + rl_right) * mm

        story = []

        def safe_paragraph(text: str, style) -> bool:
            """Add a paragraph to story, return True if successful."""
            text = text.strip()
            if not text:
                return False
            # Escape XML-special characters that aren't part of allowed tags
            # First preserve allowed tags, then escape, then restore
            text = text.replace('<strong>', '<b>').replace('</strong>', '</b>')
            text = text.replace('<em>', '<i>').replace('</em>', '</i>')
            # Escape ampersands that aren't already entities
            text = re.sub(r'&(?!amp;|lt;|gt;|quot;|#\d+;)', '&amp;', text)
            try:
                story.append(Paragraph(text, style))
                return True
            except Exception:
                # Strip ALL markup and try plain text
                clean = re.sub(r'<[^>]+>', '', text).strip()
                clean = clean.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                if clean:
                    try:
                        story.append(Paragraph(clean, style))
                        return True
                    except Exception as e2:
                        logger.warning(f"[DRPL] ReportLab paragraph failed: {e2}")
                return False

        # Split body HTML into processable chunks
        # First, handle div blocks with inline styles (like tv-date with text-align: right)
        # Note the new <table>...</table> capture group — table HTML must be
        # kept whole so we can hand it to the table parser below; otherwise
        # the inner regex strips <tr>/<td> and we lose the grid (which is
        # exactly the bug that produced single-line PDFs for annexures).
        chunks = re.split(
            r'(<table[^>]*>[\s\S]*?</table>|<h[1-6][^>]*>.*?</h[1-6]>|<div[^>]*>|</div>|<p[^>]*>|</p>|<br\s*/?>)',
            body_html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        pending_style = None
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk:
                continue

            # Table block — convert to a real ReportLab Table flowable so the
            # PDF renders the grid (instead of flattening cells into one line).
            if re.match(r'^<table\b', chunk, re.IGNORECASE):
                tbl = _parse_html_table_to_flowable(chunk, available_width_pt, styles)
                if tbl is not None:
                    story.append(tbl)
                    story.append(Spacer(1, 6))
                else:
                    # Last-ditch: render as plain text so content isn't lost
                    text = re.sub(r'<[^>]+>', ' ', chunk).strip()
                    if text:
                        safe_paragraph(text, styles['DocBody'])
                continue

            # Opening div with style — check for right-align
            div_open = re.match(r'<div[^>]*style="[^"]*text-align:\s*right[^"]*"[^>]*>', chunk, re.IGNORECASE)
            if div_open:
                pending_style = 'DocRight'
                continue

            # Skip closing tags
            if re.match(r'^</(div|p)>$', chunk, re.IGNORECASE):
                pending_style = None
                continue

            # Skip opening tags without style
            if re.match(r'^<(div|p|br)[^>]*>$', chunk, re.IGNORECASE):
                continue

            # Handle heading tags
            h_match = re.match(r'<h([1-3])[^>]*>(.*?)</h\1>', chunk, re.IGNORECASE | re.DOTALL)
            if h_match:
                level = h_match.group(1)
                text = re.sub(r'<[^>]+>', '', h_match.group(2)).strip()
                style_name = f'DocH{level}' if int(level) <= 3 else 'DocH3'
                safe_paragraph(text, styles[style_name])
                continue

            # Regular text content
            # Remove non-content tags but keep b/i/u/strong/em
            text = re.sub(r'<(?!/?(?:b|i|u|strong|em)\b)[^>]+>', '', chunk)
            text = text.strip()
            if text:
                style = styles[pending_style] if pending_style else styles['DocBody']
                safe_paragraph(text, style)
                pending_style = None

        # If nothing was generated, try plain text extraction
        if not story:
            logger.warning("[DRPL] No content parsed from HTML, falling back to plain text extraction")
            plain = re.sub(r'<[^>]+>', '', body_html).strip()
            if plain:
                for line in plain.split('\n'):
                    line = line.strip()
                    if line:
                        safe_paragraph(line, styles['DocBody'])
                    else:
                        story.append(Spacer(1, 8))

        if not story:
            safe_paragraph("(Empty document)", styles['DocBody'])

        doc.build(story)
        result = buffer.getvalue()
        logger.info("[DRPL] ReportLab fallback generated PDF (%d bytes, %d elements)", len(result), len(story))
        return result

    except Exception as e:
        logger.error(f"[DRPL] Fallback PDF generation failed: {e}", exc_info=True)
        return _generate_error_pdf(f"PDF generation failed: {str(e)}")


def _generate_error_pdf(message: str) -> bytes:
    """Generate a minimal valid PDF with an error message, so the browser always gets renderable PDF."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm

        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4

        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(width / 2, height / 2 + 20, "PDF Generation Error")
        c.setFont("Helvetica", 11)
        c.drawCentredString(width / 2, height / 2 - 10, message[:100])
        c.setFont("Helvetica", 9)
        c.drawCentredString(width / 2, height / 2 - 35, "Please check that WeasyPrint and its dependencies are installed.")
        c.save()
        return buffer.getvalue()
    except Exception as e:
        logger.error(f"[DRPL] Even error PDF generation failed: {e}")
        # Return an absolute minimal valid PDF
        return b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 595 842]/Parent 2 0 R>>endobj\nxref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"


def _resolve_signature_position(position: str, rec: dict, page_width: float, page_height: float, sig_width: float, sig_height: float):
    """Resolve signature position to (x, y) coordinates in points.

    Supports 9 named presets (top/middle/bottom + left/center/right),
    'custom' with explicit mm coordinates, and legacy fallback.
    """
    from reportlab.lib.units import mm

    # Custom coordinates — use explicit position_x/y in mm
    if position == "custom":
        x = float(rec.get("position_x", 20)) * mm
        y = float(rec.get("position_y", 30)) * mm
        return x, y

    # Named presets
    PRESETS = {
        "top-left":       ("left",   "top"),
        "top-center":     ("center", "top"),
        "top-right":      ("right",  "top"),
        "middle-left":    ("left",   "middle"),
        "middle-center":  ("center", "middle"),
        "middle-right":   ("right",  "middle"),
        "bottom-left":    ("left",   "bottom"),
        "bottom-center":  ("center", "bottom"),
        "bottom-right":   ("right",  "bottom"),
    }

    if position in PRESETS:
        h_align, v_align = PRESETS[position]
    else:
        # Legacy fallback — parse from position string
        h_align = "left" if "left" in position else ("center" if "center" in position else "right")
        v_align = "top" if "top" in position else ("middle" if "middle" in position else "bottom")

    # Calculate X
    if h_align == "left":
        x = 20 * mm
    elif h_align == "center":
        x = (page_width - sig_width) / 2
    else:  # right
        x = page_width - sig_width - 20 * mm

    # Calculate Y
    if v_align == "top":
        y = page_height - 40 * mm
    elif v_align == "middle":
        y = (page_height - sig_height) / 2
    else:  # bottom
        y = 30 * mm

    return x, y


# Cap embedded signature/stamp images at this DPI relative to their on-page
# display size. Clients upload multi-megapixel scans; embedding one at full
# resolution on every page ballooned signed PDFs to tens of MB. 300 DPI is
# print quality yet keeps each embedded bitmap tiny.
_OVERLAY_IMAGE_DPI = 300


def _overlay_image_reader(local_path: str, display_w_pt: float, display_h_pt: float, cache: dict):
    """Return a ReportLab ImageReader for `local_path`, downscaled so it never
    exceeds `_OVERLAY_IMAGE_DPI` at its on-page display size. Cached per
    (path, dims) so an 'all pages' placement reuses one small bitmap. Falls back
    to the raw path if Pillow/ReportLab are unavailable (behaviour unchanged)."""
    max_w = max(1, int(display_w_pt / 72.0 * _OVERLAY_IMAGE_DPI))
    max_h = max(1, int(display_h_pt / 72.0 * _OVERLAY_IMAGE_DPI))
    key = (local_path, max_w, max_h)
    if key in cache:
        return cache[key]
    result = local_path
    try:
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        img = Image.open(local_path)
        img.load()
        if img.width > max_w or img.height > max_h:
            img = img.copy()
            img.thumbnail((max_w, max_h), Image.LANCZOS)
        result = ImageReader(img)
    except Exception as e:
        logger.warning(f"[DRPL] Could not downscale overlay image '{local_path}': {e}")
        result = local_path
    cache[key] = result
    return result


def _compress_pdf(pdf_bytes: bytes) -> bytes:
    """Garbage-collect + deflate the PDF so repeated image XObjects (one per page
    from an 'all pages' signature) collapse to a single shared object and every
    stream is compressed. Returns the smaller of original/compressed; on any
    failure returns the input unchanged."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            compressed = doc.tobytes(garbage=4, deflate=True, clean=True)
        finally:
            doc.close()
        if compressed and len(compressed) < len(pdf_bytes):
            return compressed
    except Exception as e:
        logger.warning(f"[DRPL] PDF compression skipped: {e}")
    return pdf_bytes


def _apply_signatures(pdf_bytes: bytes, signature_records: list[dict]) -> bytes:
    """Overlay signature images onto PDF pages using ReportLab.

    Signature and stamp image paths stored on DigitalSignature rows are R2
    object keys. Each one is resolved to a local temp file via
    `storage.as_local_file(...)` for the duration of the ReportLab canvas
    build (ReportLab's `drawImage` needs a filesystem path).
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.lib.units import mm
        from PyPDF2 import PdfReader, PdfWriter

        storage = get_storage_service()
        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        total_pages = len(reader.pages)

        # Resolve every referenced signature/stamp key to a local temp file once,
        # reusing the temp files across all pages. The ExitStack keeps them alive
        # until the overlay is fully flattened into the writer.
        with contextlib.ExitStack() as stack:
            path_cache: dict[str, Optional[str]] = {}
            # Downscaled ImageReader cache, reused across pages so an 'all pages'
            # placement embeds one small bitmap rather than a full-res copy each.
            reader_cache: dict = {}

            def local_for(key: Optional[str], suffix: str) -> Optional[str]:
                if not key:
                    return None
                if key in path_cache:
                    return path_cache[key]
                try:
                    local = stack.enter_context(storage.as_local_file(key, suffix=suffix))
                except Exception as e:
                    logger.warning(f"[DRPL] Could not fetch signature asset '{key}': {e}")
                    local = None
                path_cache[key] = local
                return local

            for page_idx in range(total_pages):
                page = reader.pages[page_idx]

                # Check if any signature goes on this page
                sigs_for_page = []
                for rec in signature_records:
                    target_page = rec.get("page", "last")
                    if target_page == "last" and page_idx == total_pages - 1:
                        sigs_for_page.append(rec)
                    elif target_page == "first" and page_idx == 0:
                        sigs_for_page.append(rec)
                    elif target_page == "all":
                        sigs_for_page.append(rec)
                    elif isinstance(target_page, int) and page_idx == target_page - 1:
                        sigs_for_page.append(rec)

                if sigs_for_page:
                    overlay_buffer = io.BytesIO()
                    # Size the overlay canvas to the actual page rather than a
                    # hardcoded A4. Offline-uploaded PDFs may be Letter/Legal or
                    # any mediabox; using the real page size keeps named-position
                    # presets and custom mm coords landing correctly. Falls back
                    # to A4 if the mediabox is unreadable. (A4 docs are unaffected
                    # since their mediabox already equals A4.)
                    try:
                        width = float(page.mediabox.width)
                        height = float(page.mediabox.height)
                    except Exception:
                        width, height = A4
                    c = canvas.Canvas(overlay_buffer, pagesize=(width, height))

                    for rec in sigs_for_page:
                        sig = rec["signature"]
                        position = rec.get("position", "bottom-right")

                        sig_width = 60 * mm
                        sig_height = 25 * mm

                        x, y = _resolve_signature_position(
                            position, rec, width, height, sig_width, sig_height
                        )

                        sig_ext = os.path.splitext(sig.signature_image_path or "")[1] or ".png"
                        sig_local = local_for(sig.signature_image_path, sig_ext)
                        if sig_local:
                            try:
                                sig_img = _overlay_image_reader(
                                    sig_local, sig_width, sig_height, reader_cache
                                )
                                c.drawImage(
                                    sig_img, x, y,
                                    width=sig_width, height=sig_height,
                                    preserveAspectRatio=True, mask='auto',
                                )
                            except Exception as e:
                                logger.warning(f"[DRPL] Failed to draw signature image: {e}")

                        # Intentionally no name/designation caption is drawn below
                        # the signature image — the rendered PNG already contains
                        # the full "For DRPL / [signature] / DIRECTOR" block, and
                        # rendering the row's internal name+designation here would
                        # duplicate that text (user complaint 2026-04-21).

                        stamp_ext = os.path.splitext(sig.stamp_image_path or "")[1] or ".png"
                        stamp_local = local_for(sig.stamp_image_path, stamp_ext)
                        if stamp_local:
                            stamp_x = x + sig_width + 5 * mm
                            try:
                                stamp_img = _overlay_image_reader(
                                    stamp_local, 25 * mm, 25 * mm, reader_cache
                                )
                                c.drawImage(
                                    stamp_img, stamp_x, y,
                                    width=25 * mm, height=25 * mm,
                                    preserveAspectRatio=True, mask='auto',
                                )
                            except Exception as e:
                                logger.warning(f"[DRPL] Failed to draw stamp image: {e}")

                    c.save()
                    overlay_buffer.seek(0)

                    overlay_reader = PdfReader(overlay_buffer)
                    if len(overlay_reader.pages) > 0:
                        page.merge_page(overlay_reader.pages[0])

                writer.add_page(page)

            output = io.BytesIO()
            writer.write(output)
            # Collapse the per-page image copies and deflate all streams so the
            # signed PDF stays small (keeps 2-3MB uploads well under the 8MB cap).
            return _compress_pdf(output.getvalue())

    except ImportError as e:
        logger.warning(f"[DRPL] PyPDF2 or ReportLab not available for signature overlay: {e}")
        return pdf_bytes
    except Exception as e:
        logger.error(f"[DRPL] Signature overlay failed: {e}")
        return pdf_bytes


def apply_signatures_to_pdf(db, pdf_bytes: bytes, signature_configs: list[dict]) -> bytes:
    """Public entrypoint: stamp the given signatures/stamps onto a PDF.

    Resolves each config's `signature_id` to its `DigitalSignature` row, builds
    the `signature_records` shape the overlay expects, and returns the stamped
    PDF. Configs referencing missing/inactive signatures are skipped. With no
    resolvable signatures the input bytes are returned unchanged.

    `signature_configs` items: `{signature_id, position, position_x,
    position_y, page}` (same shape the placement board produces).
    """
    signature_records = []
    for sig_config in (signature_configs or []):
        sig = db.query(DigitalSignature).filter(
            DigitalSignature.id == sig_config.get("signature_id")
        ).first()
        if sig:
            signature_records.append({
                "signature": sig,
                "position": sig_config.get("position", sig.default_position),
                "position_x": sig_config.get("position_x", sig.default_position_x),
                "position_y": sig_config.get("position_y", sig.default_position_y),
                "page": sig_config.get("page", "last"),
            })

    if not signature_records:
        return pdf_bytes
    return _apply_signatures(pdf_bytes, signature_records)


def _markdown_to_html(markdown_text: str) -> str:
    """Basic markdown to HTML conversion."""
    try:
        import markdown
        return markdown.markdown(markdown_text, extensions=['tables', 'nl2br'])
    except ImportError:
        # Very basic fallback
        html = markdown_text.replace('\n\n', '</p><p>')
        html = html.replace('\n', '<br/>')
        html = f'<p>{html}</p>'
        # Bold
        import re
        html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html)
        html = re.sub(r'__(.+?)__', r'<strong>\1</strong>', html)
        # Italic
        html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', html)
        # Headers
        html = re.sub(r'^### (.+)', r'<h3>\1</h3>', html, flags=re.MULTILINE)
        html = re.sub(r'^## (.+)', r'<h2>\1</h2>', html, flags=re.MULTILINE)
        html = re.sub(r'^# (.+)', r'<h1>\1</h1>', html, flags=re.MULTILINE)
        return html
