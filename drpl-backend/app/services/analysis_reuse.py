"""Reuse of document-level LLM results when the documents have not changed.

Every stage that reads a tender's PDFs — the per-document analysis pass, the
annexure discovery pass, the chat "analyze tender" request — used to start
from scratch on every call, paying vision tokens again for byte-identical
files. This module is the one place that decides whether an earlier result
still applies:

  * a per-document result is reusable when the file's content hash, the model
    and the extraction prompt are the ones it was produced with;
  * a stored tender analysis is current when it completed, has a report, and
    every PDF on the tender today was read (or explicitly marked unreadable)
    with the same content hash;
  * a chat request re-runs everything only when the user asks for it
    ("re-analyze", "again", "fresh", ...);
  * an annexure discovery pass over a PDF is reused for the same bytes,
    model and prompt.

Nothing here changes what a fresh analysis produces. It only decides whether
to produce one. The provenance tag travels inside `summary_json` under the
`_source` key so no schema change is needed, and it is stripped before the
summaries are handed to the synthesis prompt so that prompt is byte-identical
to what it was.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
import logging
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Key under which the provenance tag is stored inside `summary_json`.
SOURCE_KEY = "_source"

#: Platform setting that switches document-result reuse off (default on).
REUSE_SETTING_KEY = "tender_analyzer.reuse_document_results"

_FRESH_PATTERNS = re.compile(
    r"\b(re-?analy[sz]e|re-?run|re-?do|again|fresh|from scratch|refresh|"
    r"re-?extract|force|ignore (the )?(cache|previous|earlier|stored))\b",
    re.IGNORECASE,
)


def reuse_enabled(db: Optional[Session]) -> bool:
    """`tender_analyzer.reuse_document_results` (default True)."""
    if db is None:
        return True
    try:
        from app.services.settings_service import get_setting_value
        v = get_setting_value(db, REUSE_SETTING_KEY, True)
        if isinstance(v, str):
            return v.strip().lower() not in ("false", "0", "no", "off")
        return bool(v) if v is not None else True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return True


def wants_fresh_analysis(message: Optional[str]) -> bool:
    """True when the user's wording asks for the analysis to be redone."""
    return bool(message) and bool(_FRESH_PATTERNS.search(message))


def file_sha1(path: str) -> Optional[str]:
    """Content hash of a local file; None when it cannot be read."""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        logger.debug(f"file_sha1 failed for {path}: {e}")
        return None


def text_sha1(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def source_tag(*, sha1: str, model: str, prompt: str, page_count: int = 0) -> dict:
    """Provenance stored beside a per-document result."""
    return {
        "sha1": sha1,
        "model": model or "",
        "prompt_sha1": text_sha1(prompt),
        "page_count": int(page_count or 0),
    }


def tag_matches(tag: Any, *, sha1: str, model: str, prompt: str) -> bool:
    if not isinstance(tag, dict):
        return False
    return (
        tag.get("sha1") == sha1
        and (tag.get("model") or "") == (model or "")
        and tag.get("prompt_sha1") == text_sha1(prompt)
    )


def strip_source(summary: Any) -> Any:
    """A copy of a per-doc summary without its provenance tag."""
    if isinstance(summary, dict) and SOURCE_KEY in summary:
        return {k: v for k, v in summary.items() if k != SOURCE_KEY}
    return summary


# ── per-document analysis results ──────────────────────────────────────────

def find_cached_per_doc_summary(
    db: Session,
    tender_id: int,
    document_id: int,
    *,
    sha1: str,
    model: str,
    prompt: str,
) -> Optional[dict]:
    """The newest readable per-doc summary produced from this exact file,
    model and prompt, or None.

    Identity is the bytes, not the row: a summary is a function of the file,
    the model and the prompt, and the caller re-stamps doc_id / doc_name /
    page_count on what it reuses. So after this document's own rows, any
    per-doc row on the same tender carrying the same content hash answers --
    tenders routinely hold the same NIT twice (a portal copy and a chat
    upload, or a re-upload under a new name), and each copy was read in full.
    Within one run, where rows are persisted only when every document is
    done, `remember_per_doc_summary` answers the second copy.
    """
    from app.models.document_analysis import DocumentExtractionResult

    memo = _RUN_MEMO.get(_memo_key(tender_id, sha1, model, prompt))
    if memo is not None:
        return dict(memo)
    try:
        rows = (
            db.query(DocumentExtractionResult)
            .filter(
                DocumentExtractionResult.tender_id == tender_id,
                DocumentExtractionResult.extraction_type == "per_doc_summary",
            )
            .order_by(DocumentExtractionResult.id.desc())
            .all()
        )
    except Exception as e:
        logger.warning(f"per-doc cache lookup failed for doc {document_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return None
    # This document's own rows first, then any sibling with the same bytes.
    rows.sort(key=lambda r: r.document_id != document_id)
    for row in rows:
        sj = row.summary_json
        if not isinstance(sj, dict) or sj.get("unreadable"):
            continue
        if "raw_response_preview" in sj:  # a parse failure, never reusable
            continue
        if tag_matches(sj.get(SOURCE_KEY), sha1=sha1, model=model, prompt=prompt):
            return dict(sj)
    return None


# A per-process memo of summaries read in the last hour, so a run that meets
# the same bytes twice reads them once. Bounded; content-addressed, so it can
# never serve a summary of different bytes.
_RUN_MEMO: "OrderedDict[str, dict]" = OrderedDict()
_RUN_MEMO_MAX = 256


def _memo_key(tender_id: int, sha1: str, model: str, prompt: str) -> str:
    return f"{int(tender_id)}:{sha1}:{model or ''}:{text_sha1(prompt)}"


def remember_per_doc_summary(
    tender_id: int, *, sha1: str, model: str, prompt: str, summary: dict,
) -> None:
    """Note a fresh, readable per-doc summary for reuse within this process."""
    if not sha1 or not isinstance(summary, dict) or summary.get("unreadable"):
        return
    if "raw_response_preview" in summary:
        return
    key = _memo_key(tender_id, sha1, model, prompt)
    _RUN_MEMO[key] = dict(summary)
    _RUN_MEMO.move_to_end(key)
    while len(_RUN_MEMO) > _RUN_MEMO_MAX:
        _RUN_MEMO.popitem(last=False)


# ── whole-tender analysis freshness ────────────────────────────────────────

def _current_pdf_docs(db: Session, tender_id: int) -> list:
    from app.models.tender import TenderDocument
    docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender_id).all()
    return [d for d in docs if d.file_path and d.file_path.lower().endswith(".pdf")]


def analysis_is_current(db: Session, tender_id: int) -> tuple[bool, str]:
    """Is the stored analysis built from exactly the PDFs the tender has now?

    Returns (current, reason). Current means: a completed summary with a
    report exists, and every PDF on the tender has a per-doc row (readable
    or an unreadable marker) whose recorded content hash equals the file's
    hash today. A tender with no PDFs, no summary, or any PDF the analysis
    never saw is not current. Hashing needs the files, so this downloads
    them from storage; that is I/O only, never an LLM call.
    """
    from app.models.document_analysis import (
        DocumentExtractionResult,
        TenderAnalysisSummary,
    )
    from app.services.storage_service import get_storage_service

    try:
        summary = (
            db.query(TenderAnalysisSummary)
            .filter(TenderAnalysisSummary.tender_id == tender_id)
            .first()
        )
        if not summary:
            return False, "no_summary"
        if summary.analysis_status != "completed":
            return False, f"status={summary.analysis_status}"
        if not (summary.requirement_summary or "").strip():
            return False, "empty_report"

        docs = _current_pdf_docs(db, tender_id)
        if not docs:
            return False, "no_pdfs"

        # Cheap pre-check: a document uploaded after the analysis finished
        # cannot have been read by it.
        last = summary.last_analyzed_at
        if last is not None:
            for d in docs:
                up = getattr(d, "uploaded_at", None)
                if (
                    up is not None and up.tzinfo is not None
                    and last.tzinfo is not None and up > last
                ):
                    return False, f"doc {d.id} uploaded after analysis"

        rows = (
            db.query(DocumentExtractionResult)
            .filter(
                DocumentExtractionResult.tender_id == tender_id,
                DocumentExtractionResult.extraction_type == "per_doc_summary",
            )
            .order_by(DocumentExtractionResult.id.desc())
            .all()
        )
        hashes_by_doc: dict[int, set[str]] = {}
        for r in rows:
            sj = r.summary_json if isinstance(r.summary_json, dict) else {}
            tag = sj.get(SOURCE_KEY) if isinstance(sj, dict) else None
            if isinstance(tag, dict) and tag.get("sha1") and r.document_id is not None:
                hashes_by_doc.setdefault(r.document_id, set()).add(tag["sha1"])
        if not hashes_by_doc:
            return False, "no_hashed_per_doc_rows"

        storage = get_storage_service()
        for d in docs:
            known = hashes_by_doc.get(d.id)
            if not known:
                return False, f"doc {d.id} never analyzed"
            try:
                with storage.as_local_file(d.file_path, suffix=".pdf") as local_path:
                    sha = file_sha1(local_path)
            except Exception as e:
                return False, f"doc {d.id} unreadable from storage: {type(e).__name__}"
            if not sha or sha not in known:
                return False, f"doc {d.id} changed"
        return True, "all documents unchanged"
    except Exception as e:
        logger.warning(f"analysis_is_current failed for tender {tender_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return False, f"check_failed: {type(e).__name__}"


def post_analysis_outputs_exist(db: Session, tender_id: int) -> bool:
    """Did the post-analysis pipeline (annexures + checklist) already leave
    rows for this tender? Used to avoid re-running it on a reused analysis."""
    from app.models.checklist import ChecklistItem
    try:
        n = (
            db.query(ChecklistItem)
            .filter(
                ChecklistItem.tender_id == tender_id,
                ChecklistItem.agent_key.in_(["annexure_finder", "checklist_generator"]),
            )
            .count()
        )
        return n > 0
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False


# ── annexure discovery results ─────────────────────────────────────────────
#
# Stored in the per-page vision cache table under its own key namespace: that
# table is only ever read by exact (source_key, page_index, page_sha1), so a
# discovery record can never be mistaken for a page, and nothing that counts
# or lists `document_extraction_results` rows (eager analysis, the Master's
# dependency check, the scope extractor) sees it.

ANNEXURE_DISCOVERY_METHOD = "annexure_discovery"
_ANNEXURE_PAGE_INDEX = -1


def _annexure_cache_key(tender_id: int, sha1: str) -> str:
    return f"annex:{int(tender_id)}:{sha1}"[:64]


def _annexure_variant(model: str, prompt: str) -> str:
    return text_sha1(f"{model or ''}\n{prompt or ''}")


def find_cached_annexure_discovery(
    db: Session, tender_id: int, *, sha1: str, model: str, prompt: str
) -> Optional[list[dict]]:
    """Pass-1 discovery output (identifier/title/pages) for this exact PDF,
    model and prompt, or None."""
    import json
    from app.models.pdf_vision_cache import DocumentPageVisionCache
    try:
        row = (
            db.query(DocumentPageVisionCache)
            .filter(
                DocumentPageVisionCache.source_key == _annexure_cache_key(tender_id, sha1),
                DocumentPageVisionCache.page_index == _ANNEXURE_PAGE_INDEX,
                DocumentPageVisionCache.page_sha1 == _annexure_variant(model, prompt),
            )
            .first()
        )
        if row is None:
            return None
        found = json.loads(row.text or "[]")
        if not isinstance(found, list):
            return None
        return [dict(a) for a in found if isinstance(a, dict)]
    except Exception as e:
        logger.warning(f"annexure discovery cache lookup failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return None


def store_annexure_discovery(
    db: Session,
    tender_id: int,
    *,
    sha1: str,
    model: str,
    prompt: str,
    annexures: list[dict],
    document_name: Optional[str] = None,
) -> None:
    """Record a discovery pass so an unchanged PDF is not re-read next time."""
    import json
    from app.models.pdf_vision_cache import DocumentPageVisionCache
    public = [
        {k: v for k, v in a.items() if not str(k).startswith("_")}
        for a in annexures if isinstance(a, dict)
    ]
    key = _annexure_cache_key(tender_id, sha1)
    variant = _annexure_variant(model, prompt)
    try:
        db.query(DocumentPageVisionCache).filter(
            DocumentPageVisionCache.source_key == key,
            DocumentPageVisionCache.page_index == _ANNEXURE_PAGE_INDEX,
            DocumentPageVisionCache.page_sha1 == variant,
        ).delete(synchronize_session=False)
        db.add(DocumentPageVisionCache(
            source_key=key,
            page_index=_ANNEXURE_PAGE_INDEX,
            page_sha1=variant,
            text=json.dumps(public, ensure_ascii=False, default=str),
            method=ANNEXURE_DISCOVERY_METHOD,
            model=(model or "")[:100],
        ))
        db.commit()
    except Exception as e:
        logger.warning(f"could not store annexure discovery for tender {tender_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass


def annexure_bodies_exist(db: Session, tender_id: int, identifiers: list[str]) -> bool:
    """True when every identifier already has its ChecklistItem + a workspace
    with a body, i.e. transcribing it again would only rewrite the same form."""
    from app.models.checklist import ChecklistItem
    from app.models.workspace import DocumentWorkspace
    if not identifiers:
        return False
    try:
        for ident in identifiers:
            item = (
                db.query(ChecklistItem)
                .filter(
                    ChecklistItem.tender_id == tender_id,
                    ChecklistItem.source_section == f"annexure_finder:{ident}",
                )
                .first()
            )
            if item is None:
                return False
            ws = (
                db.query(DocumentWorkspace)
                .filter(DocumentWorkspace.checklist_item_id == item.id)
                .first()
            )
            if ws is None or not (ws.draft_content_markdown or "").strip():
                return False
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False
