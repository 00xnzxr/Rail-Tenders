"""
DRPL Backend - Advanced Document Parser Service
Multi-format document reader with OCR fallback for scanned documents.
Supports: PDF (text + OCR), DOCX, images (JPG, PNG, TIFF)
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def extract_text_from_pdf_advanced(file_path: str) -> dict:
    """
    Extract text from PDF. For pages where pdfplumber returns almost nothing
    (likely scanned images), try Claude native PDF vision before falling
    through to Tesseract OCR. Vision results are cached per-page by sha1.

    Returns: {"pages": [{"page_num": 1, "text": "...", "method": "text|vision|ocr|empty"}], "total_pages": N}
    """
    pages = []

    # Pull vision-fallback config + a DB session once per document.
    vision_enabled, density_min, vision_model, vision_cap = _load_vision_config()
    db = _open_db_if_vision(vision_enabled)
    source_key = None
    vision_used = 0

    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text()
                if page_text and len(page_text.strip()) >= density_min:
                    pages.append({
                        "page_num": i + 1,
                        "text": page_text.strip(),
                        "method": "text",
                    })
                    continue

                # Sparse page — the page-vision cascade (Claude vision and/or
                # RunPod OCR in the configured order, cached per page). Past
                # the Claude page cap only the RunPod reader is offered.
                recovered = None
                if vision_enabled and db is not None:
                    recovered = _try_page_vision_cached(
                        db, file_path, i,
                        source_key_cached=source_key,
                        model=vision_model,
                        claude_allowed=vision_used < vision_cap,
                    )
                    if recovered:
                        source_key = recovered["source_key"]
                        if recovered.get("method") == "claude_vision":
                            vision_used += 1
                        pages.append({
                            "page_num": i + 1,
                            "text": recovered["text"],
                            "method": "vision",
                        })
                        continue

                # Fall back to Tesseract OCR as the last resort.
                ocr_text = _ocr_pdf_page(file_path, i)
                if ocr_text and ocr_text.strip():
                    pages.append({
                        "page_num": i + 1,
                        "text": ocr_text.strip(),
                        "method": "ocr",
                    })
                else:
                    # Preserve whatever sparse text we got — it's better than ""
                    pages.append({
                        "page_num": i + 1,
                        "text": (page_text or "").strip(),
                        "method": "empty",
                    })
        return {"pages": pages, "total_pages": total_pages}
    except Exception as e:
        logger.error(f"[DRPL] Advanced PDF extraction error for {file_path}: {e}")
        # Fallback: try basic extraction
        return _fallback_pdf_extraction(file_path)
    finally:
        if db is not None:
            try: db.close()
            except Exception: pass


def _load_vision_config() -> tuple[bool, int, str, int]:
    """Read vision-fallback settings once per extraction. Tolerates missing DB."""
    try:
        from app.core.database import SessionLocal
        from app.services.settings_service import get_setting_value
        db = SessionLocal()
        try:
            enabled = bool(get_setting_value(db, "pdf_vision_fallback_enabled", True))
            density = int(get_setting_value(db, "pdf_vision_density_min_chars", 40))
            model = str(get_setting_value(db, "pdf_vision_model", "claude-haiku-4-5-20251001"))
            cap = int(get_setting_value(db, "pdf_vision_max_pages_per_doc", 40))
            return enabled, density, model, cap
        finally:
            db.close()
    except Exception as e:
        logger.debug(f"[DRPL] vision config load failed, using defaults: {e}")
        return True, 40, "claude-haiku-4-5-20251001", 40


def _open_db_if_vision(enabled: bool):
    """Open a DB session for cache lookups/writes. Returns None when disabled or unavailable."""
    if not enabled:
        return None
    try:
        from app.core.database import SessionLocal
        return SessionLocal()
    except Exception as e:
        logger.debug(f"[DRPL] could not open db for vision cache: {e}")
        return None


_OCR_PROVIDER_ORDERS = {
    "claude_then_runpod": ("claude", "runpod"),
    "runpod_then_claude": ("runpod", "claude"),
    "claude": ("claude",),
    "runpod": ("runpod",),
}


def page_ocr_provider_order(db) -> tuple[str, ...]:
    """The readers a scanned page goes to, in order (PlatformSetting
    `pdf_ocr_provider`). RunPod is dropped from the order when it is not
    configured, so an unset key never costs a page its Claude reading."""
    try:
        from app.services.settings_service import get_setting_value
        key = str(get_setting_value(db, "pdf_ocr_provider", "claude_then_runpod") or "").strip().lower()
    except Exception:
        key = "claude_then_runpod"
    order = _OCR_PROVIDER_ORDERS.get(key, _OCR_PROVIDER_ORDERS["claude_then_runpod"])
    from app.services import runpod_ocr_service
    if "runpod" in order and not runpod_ocr_service.is_configured(db):
        order = tuple(p for p in order if p != "runpod") or ("claude",)
    return order


def _try_page_vision_cached(
    db, file_path: str, page_index: int, *,
    source_key_cached: Optional[str], model: str,
    claude_allowed: bool = True,
) -> Optional[dict]:
    """
    Cache-aware reading of a single scanned page through the configured
    provider order (`page_ocr_provider_order`). Returns
    {"text", "source_key", "method"} on hit (cache or live), None when every
    provider failed. `claude_allowed=False` (the per-document Claude page
    cap is spent) leaves only the RunPod reader in play.
    """
    from app.services.pdf_vision_service import (
        compute_source_key, extract_single_page_pdf_bytes,
        compute_page_sha1, get_cached_vision, save_cached_vision,
        extract_page_via_claude_vision_sync,
    )
    from app.services.runpod_ocr_service import extract_page_via_runpod_ocr_sync
    source_key = source_key_cached or compute_source_key(file_path)

    page_bytes = extract_single_page_pdf_bytes(file_path, page_index)
    if not page_bytes:
        return None
    page_sha1 = compute_page_sha1(page_bytes)

    cached = get_cached_vision(db, source_key, page_index, page_sha1)
    if cached and cached.text:
        return {"text": cached.text, "source_key": source_key, "method": cached.method or "claude_vision"}

    for provider in page_ocr_provider_order(db):
        if provider == "claude":
            if not claude_allowed:
                continue
            result = extract_page_via_claude_vision_sync(file_path, page_index, db=db, model=model)
            method = "claude_vision"
        else:
            result = extract_page_via_runpod_ocr_sync(file_path, page_index, db=db)
            method = "runpod_ocr"
        if not result or not result.get("text"):
            continue
        save_cached_vision(
            db,
            source_key=source_key,
            page_index=page_index,
            page_sha1=page_sha1,
            text=result["text"],
            method=method,
            model=result.get("model"),
            input_tokens=result.get("input_tokens"),
            output_tokens=result.get("output_tokens"),
        )
        return {"text": result["text"], "source_key": source_key, "method": method}
    return None


def _ocr_pdf_page(file_path: str, page_index: int) -> Optional[str]:
    """OCR a single PDF page: the RunPod reader when configured (the OCR
    engine now; Tesseract is not on the deployed image), then pytesseract."""
    try:
        from app.services import runpod_ocr_service
        if runpod_ocr_service.is_configured():
            result = runpod_ocr_service.extract_page_via_runpod_ocr_sync(file_path, page_index)
            if result and result.get("text"):
                return result["text"]
    except Exception as e:
        logger.warning(f"[DRPL] RunPod OCR failed for page {page_index + 1} of {file_path}: {e}")
    try:
        from pdf2image import convert_from_path
        import pytesseract

        images = convert_from_path(
            file_path,
            first_page=page_index + 1,
            last_page=page_index + 1,
            dpi=300,
        )
        if images:
            text = pytesseract.image_to_string(images[0], lang="eng")
            return text
    except ImportError:
        logger.warning("[DRPL] OCR dependencies not installed (pytesseract/pdf2image). Skipping OCR.")
    except Exception as e:
        logger.warning(f"[DRPL] OCR failed for page {page_index + 1} of {file_path}: {e}")
    return None


def _fallback_pdf_extraction(file_path: str) -> dict:
    """Basic PDF extraction as fallback."""
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                pages.append({
                    "page_num": i + 1,
                    "text": page_text.strip(),
                    "method": "text" if page_text.strip() else "empty",
                })
        return {"pages": pages, "total_pages": total_pages}
    except Exception as e:
        logger.error(f"[DRPL] Fallback PDF extraction failed for {file_path}: {e}")
        return {"pages": [], "total_pages": 0}


def extract_text_from_docx(file_path: str) -> dict:
    """
    Extract text from DOCX files using python-docx.
    Returns same structure as PDF extraction for consistency.
    """
    try:
        from docx import Document

        doc = Document(file_path)
        # Group paragraphs into logical pages (by section breaks or ~40 paragraphs per page)
        all_text = []
        current_page_text = []
        paragraph_count = 0
        page_num = 1
        pages = []

        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                current_page_text.append(text)
                paragraph_count += 1

            # Check for page breaks or group ~40 paragraphs per logical page
            if paragraph_count >= 40 or (para.paragraph_format.page_break_before and current_page_text):
                pages.append({
                    "page_num": page_num,
                    "text": "\n".join(current_page_text),
                    "method": "docx",
                })
                current_page_text = []
                paragraph_count = 0
                page_num += 1

        # Remaining text
        if current_page_text:
            pages.append({
                "page_num": page_num,
                "text": "\n".join(current_page_text),
                "method": "docx",
            })

        # Also extract tables
        for table_idx, table in enumerate(doc.tables):
            table_text = _extract_docx_table(table)
            if table_text:
                pages.append({
                    "page_num": page_num + table_idx + 1,
                    "text": f"[TABLE {table_idx + 1}]\n{table_text}",
                    "method": "docx_table",
                })

        return {"pages": pages, "total_pages": len(pages)}
    except ImportError:
        logger.warning("[DRPL] python-docx not installed. Cannot parse DOCX files.")
        return {"pages": [], "total_pages": 0}
    except Exception as e:
        logger.error(f"[DRPL] DOCX extraction error for {file_path}: {e}")
        return {"pages": [], "total_pages": 0}


def _extract_docx_table(table) -> str:
    """Extract text from a DOCX table as formatted rows."""
    rows = []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_text_from_image(file_path: str) -> dict:
    """
    Extract text from image files (JPG, PNG, TIFF) using OCR: the RunPod
    reader when configured, then pytesseract.
    """
    try:
        from app.services import runpod_ocr_service
        if runpod_ocr_service.is_configured():
            result = runpod_ocr_service.extract_image_via_runpod_ocr_sync(file_path)
            if result and result.get("text"):
                return {"pages": [{"page_num": 1, "text": result["text"], "method": "ocr"}], "total_pages": 1}
    except Exception as e:
        logger.warning(f"[DRPL] RunPod image OCR failed for {file_path}: {e}")
    try:
        from PIL import Image
        import pytesseract

        image = Image.open(file_path)
        text = pytesseract.image_to_string(image, lang="eng")
        return {
            "pages": [{
                "page_num": 1,
                "text": text.strip() if text else "",
                "method": "ocr",
            }],
            "total_pages": 1,
        }
    except ImportError:
        logger.warning("[DRPL] OCR dependencies not installed (pytesseract/Pillow). Cannot parse images.")
        return {"pages": [], "total_pages": 0}
    except Exception as e:
        logger.error(f"[DRPL] Image OCR error for {file_path}: {e}")
        return {"pages": [], "total_pages": 0}


def extract_text_from_file_advanced(file_path: str) -> dict:
    """
    Dispatch to appropriate parser based on file extension.
    Returns: {"pages": [...], "total_pages": N}
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        return extract_text_from_pdf_advanced(file_path)
    elif ext in (".docx", ".doc"):
        if ext == ".doc":
            logger.warning(f"[DRPL] .doc format not fully supported, attempting DOCX parser: {file_path}")
        return extract_text_from_docx(file_path)
    elif ext in (".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"):
        return extract_text_from_image(file_path)
    elif ext in (".txt", ".text", ".md"):
        return _extract_text_file(file_path)
    else:
        logger.warning(f"[DRPL] Unsupported file format: {ext} for {file_path}")
        return {"pages": [], "total_pages": 0}


def _extract_text_file(file_path: str) -> dict:
    """Extract text from plain text files."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        return {
            "pages": [{
                "page_num": 1,
                "text": text,
                "method": "text",
            }],
            "total_pages": 1,
        }
    except Exception as e:
        logger.error(f"[DRPL] Text file read error for {file_path}: {e}")
        return {"pages": [], "total_pages": 0}


def get_full_text(extraction_result: dict) -> str:
    """Helper: combine all pages into a single text string."""
    return "\n\n".join(
        f"--- Page {p['page_num']} ---\n{p['text']}"
        for p in extraction_result.get("pages", [])
        if p.get("text")
    )


def get_page_text(extraction_result: dict, page_num: int) -> Optional[str]:
    """Helper: get text for a specific page number."""
    for page in extraction_result.get("pages", []):
        if page["page_num"] == page_num:
            return page.get("text")
    return None
