"""Chat uploads, analysed one document at a time -- in the report users know.

A turn with PDFs attached used to send every PDF in ONE native-PDF call and
ask for the 7-section report. That call is capped at 100 pages and 32 MB per
request, so a real tender (an NIT, an RFP and a few annexures) failed it and
fell through to a ReAct agent that cannot open chat attachments, then to a
call that saw only the message text. It also read every page again on every
upload, and left nothing behind that the rest of the platform could reuse:
no per-document record, no document type for the schedule parser, no tender
reference or value for an auto-created tender.

This path reads each PDF on its own with the per-document extractor the
tender analyzer uses (one vision call per document, cached by the file's
bytes, so the same PDF is never read twice), runs a few documents at once so
the first answer is not slower, writes the per-document records, fills in the
auto-created tender from them, and then writes the SAME 7-section report --
same sections, same instructions (CHAT_REPORT_SPEC is shared with the
one-call path word for word) -- from the verbatim extracts instead of the
raw pages. The one-call path stays as the fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── the report (shared, word for word, with the one-call path) ───────────

CHAT_REPORT_INTRO = (
    'You are the most critical, thorough, and uncompromising tender document analyst. Your analysis drives ALL subsequent work — checklists, proposals, costing, bid decisions. Every overlooked clause is a potential bid rejection. Leave NOTHING to chance.\n'
    '\n'
)

CHAT_REPORT_SPEC = (
    '## 1. TENDER SUMMARY & KEY INTELLIGENCE\n'
    'Title, reference number, issuing authority, scope of work (specific quantities/items), estimated value, EMD/bid security (amount + form + exemptions), ALL key dates, delivery location & timeline, tender type, evaluation method (L1/QCBS/RA), list of all documents/annexures that form part of this tender.\n'
    '\n'
    '## 2. COMPLETE DOCUMENT REQUIREMENTS\n'
    'Extract EVERY document the bidder must submit. For each: exact name, MANDATORY or optional, required format (original/copy/notarized/self-attested), validity requirements, which envelope/part (technical/financial/PQ). Search ALL sections — requirements are often scattered across eligibility, submission format, and terms sections. Reconcile differences.\n'
    '\n'
    '## 3. ELIGIBILITY ASSESSMENT — GO / NO-GO / CONDITIONAL\n'
    'List all hard eligibility criteria (turnover, experience, certifications, financial capacity). For each: quote the exact requirement. Provide a clear GO/NO-GO/CONDITIONAL recommendation.\n'
    '\n'
    '## 4. NEGATIVE KEYWORDS & REJECTION RISK ANALYSIS\n'
    "Scan EVERY page for disqualification triggers: 'summarily rejected', 'shall not be considered', 'failing which', 'non-submission', 'forfeiture of EMD', 'liquidated damages', 'debarred', etc. Output as table: | # | Quoted Clause | Keyword | Risk Level | Page/Section | Required Action |\n"
    'Also flag: exact-item experience requirements, tight delivery timelines (<30 days), heavy LD (>10%), uncapped liability, unilateral termination rights.\n'
    '\n'
    "## 5. WHAT'S MISSING — GAPS & RED FLAGS\n"
    'Identify what a prudent bidder would expect but is NOT present: missing payment terms, unclear scope boundaries, no variation mechanism, vague delivery schedule, missing force majeure, referenced annexures not attached. For each: explain WHY it matters and what risk it creates.\n'
    '\n'
    '## 6. REGULATORY & COMPLIANCE INTELLIGENCE\n'
    'Flag applicable regulations even if not explicitly in the tender: Make in India requirements, MSE purchase preference, applicable BIS/IS standards, GST implications, recent policy changes affecting this category.\n'
    '\n'
    '## 7. CRITICAL NEXT STEPS & ACTIONABLE RECOMMENDATIONS\n'
    'Prioritized action plan: Immediate (24h), Before Submission, Risk Mitigation. Include pre-bid questions to ask, documents to prepare, certifications to verify.\n'
    '\n'
    'RULES:\n'
    '- Quote EXACT text when referencing clauses\n'
    '- Cross-reference requirements across different sections and documents\n'
    '- Flag contradictions between documents\n'
    '- Note any linked/referenced documents that need separate analysis\n'
    '- Use Indian procurement terminology (NIT, EMD, RDSO, GeM, GFR)\n'
    '- ALWAYS end with Section 7 — the user must leave with a clear action plan'
)

#: The one-call path's prompt: exactly the text it has always sent.
CHAT_REPORT_PDF_PROMPT = (
    CHAT_REPORT_INTRO
    + 'Analyze the attached document(s) and produce a comprehensive 7-section report:\n\n'
    + CHAT_REPORT_SPEC
)

#: Where the one-call path reads pages, this path reads the extracts. The
#: sections, their instructions and the rules after them are CHAT_REPORT_SPEC,
#: unchanged.
CHAT_REPORT_EXTRACTS_LEAD = (
    "The user attached the tender document(s) listed below. Each one has already "
    "been read in full -- every page, scanned pages included -- and extracted into "
    "a per-document JSON record: verbatim quotes of every requirement, eligibility "
    "criterion, critical clause, date, amount and commercial term, each with its "
    "page. Those records are your evidence. Quote from their `text` fields EXACTLY, "
    "cite `doc_name` and `page`, and where a detail is absent write \"Not stated in "
    "the attached documents\" rather than supplying it. Where a section below says "
    "to scan every page, go through every entry of every record. A record marked "
    "`unreadable` is a document that could not be read: name it in Section 5 and "
    "say why that matters.\n\n"
    "From these records, produce a comprehensive 7-section report:\n\n"
)
CHAT_REPORT_EXTRACTS_PROMPT = CHAT_REPORT_INTRO + CHAT_REPORT_EXTRACTS_LEAD + CHAT_REPORT_SPEC

#: The one-call path's default request, kept for the same wording.
DEFAULT_REQUEST = "Analyze the attached tender document(s) comprehensively."

#: A document whose size is not known is assumed this large for the byte gate.
_UNKNOWN_SIZE = 16 * 1024 * 1024
#: PDF bytes allowed in flight at once. Each read holds its file, its base64
#: and the JSON body (~3.7x the file) until the response arrives; the tender
#: analyzer runs one document at a time for that reason. A document larger
#: than this still runs -- alone.
_BYTE_BUDGET = 64 * 1024 * 1024
#: The records the report call reads. Haiku's window is 200k tokens; this
#: leaves room for the prompt and the report.
_MAX_RECORDS_CHARS = 400_000

#: A report shorter than this is not a report (empty, or a disabled-agent
#: placeholder); the one-call path takes over.
_MIN_REPORT_CHARS = 200

REPORT_CACHE_METHOD = "chat_upload_report"
_REPORT_PAGE_INDEX = -3


def per_doc_uploads_enabled(db: Session) -> bool:
    from app.services.settings_service import get_setting_value
    try:
        return bool(get_setting_value(db, "tender_analyzer.chat_uploads_per_doc", True))
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return True


def _parallelism(db: Session) -> int:
    from app.services.settings_service import get_setting_value
    try:
        n = int(get_setting_value(db, "tender_analyzer.chat_upload_parallel", 3) or 3)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        n = 3
    return max(1, min(n, 4))


def user_request_of(message: str) -> str:
    """The user's own words, without the attached-files block -- exactly what
    the one-call path sends."""
    text = re.sub(r"\[ATTACHED FILES\].*", "", message or "", flags=re.DOTALL).strip()
    return text or DEFAULT_REQUEST


class _ByteGate:
    """At most `slots` documents in flight, and at most `budget` bytes of PDF
    between them; a document alone may exceed the budget."""

    def __init__(self, slots: int, budget: int):
        self._slots = max(1, slots)
        self._budget = budget
        self._inflight = 0
        self._bytes = 0
        self._cond = asyncio.Condition()

    def slot(self, size: int) -> "_Slot":
        return _Slot(self, max(0, int(size)))


class _Slot:
    def __init__(self, gate: _ByteGate, size: int):
        self.gate = gate
        self.size = size

    async def __aenter__(self):
        g = self.gate
        async with g._cond:
            await g._cond.wait_for(
                lambda: g._inflight == 0
                or (g._inflight < g._slots and g._bytes + self.size <= g._budget)
            )
            g._inflight += 1
            g._bytes += self.size
        return self

    async def __aexit__(self, *exc):
        g = self.gate
        async with g._cond:
            g._inflight -= 1
            g._bytes -= self.size
            g._cond.notify_all()
        return False


def tender_of_attachments(db: Session, pdf_keys: list[str]) -> Optional[int]:
    """The tender the attached files were registered on, when there is one.

    The streaming handler registers a turn's PDFs on the session's tender
    before any agent runs, so the files say which tender they belong to.
    A delegated call's `tender_id` comes from the model (or a regex over its
    text) and can be missing or wrong; the files cannot.
    """
    from app.models.tender import TenderDocument

    keys = [k for k in dict.fromkeys(pdf_keys or []) if k]
    if not keys:
        return None
    try:
        owners = {
            t for (t,) in db.query(TenderDocument.tender_id)
            .filter(TenderDocument.file_path.in_(keys))
            .distinct()
            .all()
        }
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return None
    return owners.pop() if len(owners) == 1 else None


def _attached_documents(db: Session, tender_id: int, pdf_keys: list[str]) -> list[dict]:
    """The tender's documents for the attached files, in attachment order, as
    plain dicts -- the session is released before the reads, and a rollback
    expires ORM rows, so reading them afterwards would reopen a transaction
    for the length of the reads.

    The streaming handler registers every attached PDF as a TenderDocument
    (creating the tender if the session had none) before any agent runs.
    """
    from app.models.tender import TenderDocument

    keys = [k for k in dict.fromkeys(pdf_keys or []) if k]
    if not keys:
        return []
    rows = (
        db.query(TenderDocument)
        .filter(TenderDocument.tender_id == tender_id, TenderDocument.file_path.in_(keys))
        .order_by(TenderDocument.id.asc())
        .all()
    )
    by_path: dict = {}
    for r in rows:
        by_path.setdefault(r.file_path, {
            "id": r.id,
            "file_name": r.file_name,
            "file_path": r.file_path,
            "file_size": r.file_size,
        })
    return [by_path[k] for k in keys if k in by_path]


async def _read_documents(
    tender_id: int, docs: list[dict], sizes: dict, parallel: int, force_refresh: bool,
) -> list[dict]:
    """Per-document extraction for every attached PDF, `parallel` at a time."""
    import os

    from app.services.ai_service import _emit_reliability_event
    from app.services.langchain.graphs.document_analysis_agent import (
        _analyze_single_document_native,
    )

    gate = _ByteGate(parallel, _BYTE_BUDGET)
    total = len(docs)
    _emit_reliability_event(None, "analyzer_progress", {
        "phase": "starting", "completed": 0, "total": total,
    })
    done = [0]
    lock = asyncio.Lock()

    async def one(idx: int, doc: dict) -> dict:
        name = doc["file_name"] or os.path.basename(doc["file_path"])
        size = sizes.get(doc["file_path"]) or doc["file_size"] or _UNKNOWN_SIZE
        res = await _analyze_single_document_native(
            tender_id=tender_id,
            doc_id=doc["id"],
            doc_name=name,
            doc_path=doc["file_path"],
            doc_index=idx + 1,
            doc_total=total,
            semaphore=gate.slot(size),
            force_refresh=force_refresh,
        )
        async with lock:
            done[0] += 1
            k = done[0]
        _emit_reliability_event(None, "analyzer_progress", {
            "phase": "per_doc_complete", "completed": k, "total": total, "doc_name": name,
        })
        return res

    raw = await asyncio.gather(*(one(i, d) for i, d in enumerate(docs)), return_exceptions=True)
    results: list[dict] = []
    for idx, r in enumerate(raw):
        if isinstance(r, BaseException):
            doc = docs[idx]
            logger.error(f"[chat upload] tender {tender_id}: doc {doc['id']} raised {r!r}")
            results.append({
                "doc_id": doc["id"],
                "doc_name": doc["file_name"] or os.path.basename(doc["file_path"]),
                "page_count": 0,
                "summary": None,
                "unreadable": True,
                "error": f"unhandled_exception: {type(r).__name__}: {str(r)[:200]}",
                "elapsed_ms": 0,
            })
        else:
            results.append(r)
    return results


def _records_payload(summaries: list[dict], max_chars: int = _MAX_RECORDS_CHARS) -> str:
    """The records as compact JSON, whole entries only.

    Over budget, the longest requirement lists lose entries from their end and
    say how many -- the record is never cut mid-string, which would be read as
    fact rather than as damage.
    """
    records = [dict(s) for s in summaries]

    def dump() -> str:
        return json.dumps(records, ensure_ascii=False, default=str, separators=(",", ":"))

    text = dump()
    while len(text) > max_chars:
        longest = max(
            (r for r in records if isinstance(r.get("requirements"), list) and r["requirements"]),
            key=lambda r: len(r["requirements"]),
            default=None,
        )
        if longest is None:
            break
        keep = max(1, int(len(longest["requirements"]) * 0.9))
        dropped = len(longest["requirements"]) - keep
        if dropped <= 0:
            break
        longest["requirements"] = longest["requirements"][:keep]
        longest["_requirements_omitted_for_length"] = (
            longest.get("_requirements_omitted_for_length", 0) + dropped
        )
        text = dump()
    return text


def _report_key(per_doc_results: list[dict], model: str, request: str) -> str:
    """Identity of a report: the documents' bytes, the model, the prompt and
    the user's own words."""
    docs = sorted(
        (r.get("source_sha1") or f"unhashed:{r.get('doc_id')}:{r.get('doc_name')}")
        for r in per_doc_results
    )
    basis = json.dumps({
        "docs": docs,
        "model": model or "",
        "prompt": hashlib.sha1(CHAT_REPORT_EXTRACTS_PROMPT.encode("utf-8")).hexdigest(),
        "request": " ".join((request or "").split()).lower(),
    }, sort_keys=True)
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _cached_report(db: Session, key: str) -> Optional[str]:
    from app.models.pdf_vision_cache import DocumentPageVisionCache
    try:
        row = (
            db.query(DocumentPageVisionCache)
            .filter(
                DocumentPageVisionCache.source_key == f"chatrep:{key}"[:64],
                DocumentPageVisionCache.page_index == _REPORT_PAGE_INDEX,
                DocumentPageVisionCache.page_sha1 == key,
            )
            .first()
        )
        if row is None:
            return None
        report = (json.loads(row.text or "{}") or {}).get("report")
        return report if isinstance(report, str) and report.strip() else None
    except Exception as e:
        logger.warning(f"[chat upload] report cache lookup failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _store_report(db: Session, key: str, report: str, model: str) -> None:
    from app.models.pdf_vision_cache import DocumentPageVisionCache
    try:
        db.query(DocumentPageVisionCache).filter(
            DocumentPageVisionCache.source_key == f"chatrep:{key}"[:64],
            DocumentPageVisionCache.page_index == _REPORT_PAGE_INDEX,
            DocumentPageVisionCache.page_sha1 == key,
        ).delete(synchronize_session=False)
        db.add(DocumentPageVisionCache(
            source_key=f"chatrep:{key}"[:64],
            page_index=_REPORT_PAGE_INDEX,
            page_sha1=key,
            text=json.dumps({"report": report}, ensure_ascii=False),
            method=REPORT_CACHE_METHOD,
            model=(model or "")[:100],
        ))
        db.commit()
    except Exception as e:
        logger.warning(f"[chat upload] could not store the report: {e}")
        try:
            db.rollback()
        except Exception:
            pass


def _enrich_tender(db: Session, tender_id: int) -> None:
    """Fill an auto-created tender (portal reference, title, value) from the
    per-document facts -- deterministic, no model call."""
    try:
        from app.services.ai_service import _emit_reliability_event
        from app.services.tender_enrichment_service import enrich_tender_from_analysis

        enriched = enrich_tender_from_analysis(db, tender_id)
        if enriched:
            _emit_reliability_event(None, "tender_enriched", {"tender_id": tender_id, **enriched})
    except Exception as e:
        logger.warning(f"[chat upload] tender {tender_id}: enrichment failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass


async def analyze_chat_uploads(
    db: Session,
    message: str,
    tender_id: int,
    pdf_keys: list[str],
    *,
    sizes: Optional[dict] = None,
    force_refresh: bool = False,
) -> Optional[dict]:
    """The 7-section report for the PDFs attached to this turn, or None.

    None means "use the one-call path": the attachments are not registered on
    the tender, or no document could be read.
    """
    from app.services.ai_service import (
        _emit_reliability_event,
        _get_agent_config,
        _get_effective_model,
        call_ai,
    )
    from app.services.analysis_reuse import reuse_enabled
    from app.services.langchain.graphs.document_analysis_agent import (
        _persist_per_doc_results,
        _release_db_before_long_await,
    )

    docs = _attached_documents(db, tender_id, pdf_keys)
    if not docs:
        logger.info(f"[chat upload] tender {tender_id}: attachments not registered; one-call path")
        return None

    t0 = time.monotonic()
    parallel = _parallelism(db)
    logger.info(
        f"[chat upload] tender {tender_id}: reading {len(docs)} attached PDF(s) one "
        f"document at a time, up to {parallel} at once"
    )
    _release_db_before_long_await(db, label="chat_upload_per_doc")
    per_doc = await _read_documents(tender_id, docs, sizes or {}, parallel, force_refresh)

    summaries, unreadable = _persist_per_doc_results(db, tender_id, per_doc)
    readable = len(per_doc) - unreadable
    if readable <= 0:
        logger.warning(
            f"[chat upload] tender {tender_id}: none of {len(per_doc)} document(s) could "
            f"be read one at a time; one-call path"
        )
        return None
    reused_docs = sum(1 for r in per_doc if r.get("cached"))
    _enrich_tender(db, tender_id)

    request = user_request_of(message)
    cfg = _get_agent_config(db, "deep_analyzer")
    model = _get_effective_model(db, dict(cfg))
    key = _report_key(per_doc, model, request)
    use_cache = not force_refresh and reuse_enabled(db)
    report = _cached_report(db, key) if use_cache else None
    report_reused = report is not None

    if report is None:
        _emit_reliability_event(None, "analyzer_progress", {
            "phase": "synthesis_start", "completed": len(per_doc), "total": len(per_doc),
        })
        user_prompt = (
            f"{request}\n\n"
            f"PER-DOCUMENT RECORDS ({len(per_doc)} document(s), {unreadable} unreadable):\n"
            f"{_records_payload(summaries)}"
        )
        _release_db_before_long_await(db, label="chat_upload_report")
        report = await call_ai(
            CHAT_REPORT_EXTRACTS_PROMPT,
            user_prompt,
            db,
            "deep_analyzer",
            max_tokens_override=max(int(cfg.get("max_tokens") or 0), 8192),
        )
        if len((report or "").strip()) < _MIN_REPORT_CHARS:
            # Empty, or a disabled-agent placeholder: the one-call path decides.
            logger.warning(
                f"[chat upload] tender {tender_id}: report came back "
                f"{len((report or '').strip())} chars; one-call path"
            )
            return None
        _store_report(db, key, report, model)

    _emit_reliability_event(None, "analyzer_progress", {
        "phase": "complete", "completed": len(per_doc), "total": len(per_doc),
    })
    output = report
    if report_reused:
        output = report.rstrip() + (
            "\n\n_These documents and this request were analysed before, so the "
            "earlier report was returned without reading them again. Ask to "
            "re-analyze to run it afresh._\n"
        )
    logger.info(
        f"[chat upload] tender {tender_id}: report ready in {time.monotonic() - t0:.0f}s "
        f"({readable}/{len(per_doc)} readable, {reused_docs} document(s) reused, "
        f"report {'reused' if report_reused else 'written'})"
    )
    return {
        "output": output,
        "output_type": "document_analysis",
        "agent_key": "deep_analyzer",
        "tool_calls": [],
        "metrics": {
            "method": "per_doc_upload",
            "documents_count": len(per_doc),
            "documents_unreadable": unreadable,
            "documents_reused": reused_docs,
            "report_reused": report_reused,
        },
        "status": "completed",
    }
