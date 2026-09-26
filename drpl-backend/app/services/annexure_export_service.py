"""
DRPL Backend - Combined Annexure Export Service

Renders every annexure of a tender into ONE continuous file so the bidder can
export the whole annexure set in a single shot — no manual PDF merging.

- ``build_combined_annexures_pdf`` concatenates each annexure's per-item PDF
  (already rendered with letterhead + signatures + per-item orientation by
  ``generate_workspace_preview``) into one PDF using PyMuPDF, which preserves
  each annexure's own page size so mixed portrait/landscape stitches cleanly.
- ``build_combined_annexures_docx`` appends every annexure into one editable
  Word document, each in its own section so orientation is independent.

Both read live ``DocumentWorkspace`` draft content — nothing is finalized or
mutated, so the user can keep editing after exporting.
"""

import io
import logging

from sqlalchemy.orm import Session

from app.models.tender import Tender
from app.models.checklist import ChecklistItem
from app.models.workspace import DocumentWorkspace

logger = logging.getLogger(__name__)

# Annexure checklist items are tagged with this source_section prefix by the
# annexure_finder agent (e.g. "annexure_finder:Annexure-1.3").
ANNEXURE_SOURCE_PREFIX = "annexure_finder:"


def _safe_filename(stem: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in stem)[:80]


def get_annexure_items(db: Session, tender_id: int) -> list[tuple[ChecklistItem, DocumentWorkspace]]:
    """Return (ChecklistItem, DocumentWorkspace) pairs for every annexure of a
    tender, in stable display order. Items without a workspace row are skipped.
    """
    items = (
        db.query(ChecklistItem)
        .filter(
            ChecklistItem.tender_id == tender_id,
            ChecklistItem.source_section.like(f"{ANNEXURE_SOURCE_PREFIX}%"),
        )
        .order_by(ChecklistItem.display_order.asc(), ChecklistItem.id.asc())
        .all()
    )

    pairs: list[tuple[ChecklistItem, DocumentWorkspace]] = []
    for item in items:
        ws = (
            db.query(DocumentWorkspace)
            .filter(DocumentWorkspace.checklist_item_id == item.id)
            .first()
        )
        if ws is not None:
            pairs.append((item, ws))
    return pairs


def _resolve_letterhead_text(db: Session, tender_id: int, ws: DocumentWorkspace) -> str:
    """Company name for the DOCX section header, following the same precedence
    the PDF path uses: per-document opt-out > per-document override > tender
    default. Returns "" when no letterhead applies.
    """
    if bool(getattr(ws, "letterhead_disabled", False)):
        return ""

    from app.models.letterhead import LetterheadTemplate
    from app.models.workspace import WorkspaceConfig

    letterhead_id = ws.letterhead_template_id
    if not letterhead_id:
        cfg = db.query(WorkspaceConfig).filter(
            WorkspaceConfig.tender_id == tender_id
        ).first()
        letterhead_id = cfg.default_letterhead_id if cfg else None
    if not letterhead_id:
        return ""

    lh = db.query(LetterheadTemplate).filter(
        LetterheadTemplate.id == letterhead_id
    ).first()
    if not lh:
        return ""
    return (lh.company_name_override or lh.name or "").strip()


def build_combined_annexures_pdf(db: Session, tender_id: int) -> tuple[bytes, str]:
    """Render and concatenate all annexures into one PDF.

    Returns (pdf_bytes, filename). Raises ValueError if the tender has no
    annexures.
    """
    import fitz  # PyMuPDF

    from app.services.pdf_generation_service import generate_workspace_preview

    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise ValueError("Tender not found")

    pairs = get_annexure_items(db, tender_id)
    if not pairs:
        raise ValueError("No annexures found for this tender")

    combined = fitz.open()
    rendered = 0
    try:
        for item, _ws in pairs:
            try:
                pdf_bytes = generate_workspace_preview(db, tender_id, item.id)
            except Exception as e:
                # One bad annexure shouldn't sink the whole export — log and skip.
                logger.warning(
                    "[ANNEXURE_EXPORT] tender=%d item=%d render failed, skipping: %s",
                    tender_id, item.id, e,
                )
                continue
            if not pdf_bytes or len(pdf_bytes) < 100:
                logger.warning(
                    "[ANNEXURE_EXPORT] tender=%d item=%d produced empty PDF, skipping",
                    tender_id, item.id,
                )
                continue
            with fitz.open(stream=pdf_bytes, filetype="pdf") as part:
                combined.insert_pdf(part)
            rendered += 1

        if rendered == 0:
            raise ValueError("No annexures could be rendered")

        out = combined.tobytes()
    finally:
        combined.close()

    filename = f"DRPL_Annexures_{_safe_filename(tender.title or str(tender_id))}.pdf"
    logger.info(
        "[ANNEXURE_EXPORT] tender=%d combined PDF: %d/%d annexures, %d bytes",
        tender_id, rendered, len(pairs), len(out),
    )
    return out, filename


def build_combined_annexures_docx(db: Session, tender_id: int) -> tuple[bytes, str]:
    """Render all annexures into one editable DOCX.

    Returns (docx_bytes, filename). Raises ValueError if the tender has no
    annexures.
    """
    from app.services.docx_generation_service import generate_combined_docx

    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise ValueError("Tender not found")

    pairs = get_annexure_items(db, tender_id)
    if not pairs:
        raise ValueError("No annexures found for this tender")

    parts: list[dict] = []
    for item, ws in pairs:
        html = (ws.draft_content_html or "").strip()
        markdown = (ws.draft_content_markdown or "").strip()
        if not html and not markdown:
            continue
        parts.append({
            "title": item.item_name,
            "html": html or None,
            "markdown": markdown or None,
            "orientation": (ws.page_orientation or "portrait").lower(),
            "letterhead_text": _resolve_letterhead_text(db, tender_id, ws),
        })

    if not parts:
        raise ValueError("No annexure content to export")

    docx_bytes = generate_combined_docx(parts)
    filename = f"DRPL_Annexures_{_safe_filename(tender.title or str(tender_id))}.docx"
    logger.info(
        "[ANNEXURE_EXPORT] tender=%d combined DOCX: %d annexures, %d bytes",
        tender_id, len(parts), len(docx_bytes),
    )
    return docx_bytes, filename


def build_combined_annexures_zip(db: Session, tender_id: int) -> tuple[bytes, str]:
    """Bundle the combined PDF and combined DOCX into one ZIP."""
    import zipfile

    pdf_bytes, pdf_name = build_combined_annexures_pdf(db, tender_id)
    docx_bytes, docx_name = build_combined_annexures_docx(db, tender_id)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(pdf_name, pdf_bytes)
        zf.writestr(docx_name, docx_bytes)
    buffer.seek(0)

    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    stem = _safe_filename((tender.title if tender else None) or str(tender_id))
    return buffer.read(), f"DRPL_Annexures_{stem}.zip"
