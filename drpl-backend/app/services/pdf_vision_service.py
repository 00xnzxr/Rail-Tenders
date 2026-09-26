"""
DRPL Backend - Per-page PDF Claude vision fallback.

When a PDF page has almost no extractable text via pdfplumber, it is most
likely a scanned image. Before falling back to Tesseract OCR (noisy, slow,
often wrong on Indian-language forms), we re-read that single page via
Claude's native document-vision API. Results are cached in
`document_page_vision_cache` keyed by (source_key, page_index, page_sha1)
so re-opening the same document is free.

Sync-only — callers that live in the `advanced_document_parser.py` are
synchronous. We talk to the Anthropic API via `httpx.Client` directly
instead of going through `ai_service._call_anthropic` (which is async).
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import time
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.pdf_vision_cache import DocumentPageVisionCache

logger = logging.getLogger(__name__)


_VISION_PROMPT = (
    "Extract all readable text from this single PDF page exactly as it "
    "appears. Preserve tables as aligned plain text with columns separated "
    "by two or more spaces. Preserve bullet markers and numbering. Do NOT "
    "summarise, paraphrase, translate, or add commentary. If a region is "
    "illegible write [illegible]. Output only the extracted page text — no "
    "preamble, no trailing notes."
)


# ── Density check ────────────────────────────────────────────────────────────

def page_text_is_sparse(text: Optional[str], min_chars: int) -> bool:
    """True if the text extracted from a page is short enough to suspect a scan."""
    if text is None:
        return True
    return len(text.strip()) < max(1, int(min_chars))


# ── Stable keys ──────────────────────────────────────────────────────────────

def compute_source_key(file_path: str) -> str:
    """
    Stable fingerprint for the source file — SHA-1 of the first 64 KB plus
    file size. Full-file hashing is too slow for big tenders and not
    necessary: page_sha1 already guards against content drift per page.
    """
    h = hashlib.sha1()
    try:
        size = os.path.getsize(file_path)
        h.update(str(size).encode("ascii"))
        with open(file_path, "rb") as f:
            h.update(f.read(64 * 1024))
    except OSError as e:
        logger.warning(f"source_key: cannot stat/read {file_path}: {e}")
        h.update(file_path.encode("utf-8", errors="ignore"))
    return h.hexdigest()


def extract_single_page_pdf_bytes(file_path: str, page_index: int) -> Optional[bytes]:
    """Carve out page `page_index` as a standalone single-page PDF blob."""
    try:
        from PyPDF2 import PdfReader, PdfWriter
    except ImportError:
        logger.warning("PyPDF2 not installed — cannot extract single page for vision.")
        return None

    try:
        reader = PdfReader(file_path)
        if page_index < 0 or page_index >= len(reader.pages):
            return None
        writer = PdfWriter()
        writer.add_page(reader.pages[page_index])
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"extract_single_page_pdf_bytes failed p={page_index}: {e}")
        return None


def compute_page_sha1(page_bytes: bytes) -> str:
    return hashlib.sha1(page_bytes).hexdigest()


# ── Cache ────────────────────────────────────────────────────────────────────

def get_cached_vision(
    db: Session, source_key: str, page_index: int, page_sha1: str,
) -> Optional[DocumentPageVisionCache]:
    try:
        return (
            db.query(DocumentPageVisionCache)
            .filter(
                DocumentPageVisionCache.source_key == source_key,
                DocumentPageVisionCache.page_index == page_index,
                DocumentPageVisionCache.page_sha1 == page_sha1,
            )
            .first()
        )
    except Exception as e:
        logger.debug(f"vision cache lookup failed: {e}")
        try: db.rollback()
        except Exception: pass
        return None


def save_cached_vision(
    db: Session,
    source_key: str,
    page_index: int,
    page_sha1: str,
    text: str,
    method: str,
    model: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> None:
    row = DocumentPageVisionCache(
        source_key=source_key,
        page_index=page_index,
        page_sha1=page_sha1,
        text=text,
        method=method,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    try:
        db.add(row)
        db.commit()
    except Exception as e:
        logger.debug(f"vision cache insert failed (likely race): {e}")
        try: db.rollback()
        except Exception: pass


# ── Claude vision call (sync) ────────────────────────────────────────────────

def _get_api_key(db: Optional[Session]) -> Optional[str]:
    """Mirror of ai_service._get_effective_api_key but inline and tolerant of a missing db."""
    # Prefer PlatformSetting override, fall back to env-sourced settings.
    if db is not None:
        try:
            from app.models.platform_setting import PlatformSetting
            row = db.query(PlatformSetting).filter(PlatformSetting.key == "anthropic_api_key").first()
            if row and row.value:
                return row.value
        except Exception:
            try: db.rollback()
            except Exception: pass
    return get_settings().anthropic_api_key


def extract_page_via_claude_vision_sync(
    pdf_path: str,
    page_index: int,
    *,
    db: Optional[Session] = None,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 4000,
    timeout_s: float = 120.0,
) -> Optional[dict]:
    """
    Send a single PDF page to Claude as a native `document` block and return
    the extracted text. Returns dict {text, input_tokens, output_tokens} or
    None on failure. Errors are logged, not raised — the caller will fall
    through to Tesseract OCR.
    """
    api_key = _get_api_key(db)
    if not api_key:
        logger.warning("pdf_vision: no anthropic_api_key available — skipping vision fallback.")
        return None

    page_bytes = extract_single_page_pdf_bytes(pdf_path, page_index)
    if not page_bytes:
        return None

    b64 = base64.standard_b64encode(page_bytes).decode("ascii")
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": _VISION_PROMPT},
                ],
            }
        ],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        with httpx.Client(timeout=timeout_s) as client:
            t0 = time.time()
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=body,
            )
            elapsed = int((time.time() - t0) * 1000)
            if resp.status_code != 200:
                logger.warning(
                    f"pdf_vision: anthropic HTTP {resp.status_code} "
                    f"(page {page_index}, {elapsed}ms): {resp.text[:300]}"
                )
                return None
            data = resp.json()
    except Exception as e:
        logger.warning(f"pdf_vision: HTTP call failed (page {page_index}): {e}")
        return None

    # Extract text from response blocks.
    parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        return None

    usage = data.get("usage", {}) or {}
    # Every scanned page read at upload and by the schedule parser used to be
    # invisible to api_usage_logs, the admin cost dashboard and the budget.
    try:
        from app.services.ai_service import _log_usage
        _log_usage(None, "anthropic", model, "pdf_page_vision", usage, elapsed, True)
    except Exception:
        pass
    return {
        "text": text,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "model": model,
    }
