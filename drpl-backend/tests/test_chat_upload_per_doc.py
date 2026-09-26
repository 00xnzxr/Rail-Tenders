"""Chat uploads are read one document at a time, in the report users know.

The one-call path sent every attached PDF in one request -- capped at 100
pages -- and read every page again on every upload. The per-document path
reads each PDF once (cached by its bytes), a few at a time, and writes the
same 7-section report from the extracts. These tests pin: the report format
is the one-call path's, word for word; documents run concurrently within
the byte budget; nothing is read twice; every failure falls back to the
one-call path.
"""

import asyncio
import itertools
import re
import uuid

import pytest

from app.models.document_analysis import DocumentExtractionResult
from app.models.tender import Tender, TenderDocument
from app.services.langchain.graphs import chat_upload_analysis as cua
from app.services.langchain.graphs import document_analysis_agent as daa

_ids = itertools.count(980000)

SECTION_HEADINGS = [
    "## 1. TENDER SUMMARY & KEY INTELLIGENCE",
    "## 2. COMPLETE DOCUMENT REQUIREMENTS",
    "## 3. ELIGIBILITY ASSESSMENT — GO / NO-GO / CONDITIONAL",
    "## 4. NEGATIVE KEYWORDS & REJECTION RISK ANALYSIS",
    "## 5. WHAT'S MISSING — GAPS & RED FLAGS",
    "## 6. REGULATORY & COMPLIANCE INTELLIGENCE",
    "## 7. CRITICAL NEXT STEPS & ACTIONABLE RECOMMENDATIONS",
]

REPORT = "\n\n".join(f"{h}\nContent for {h[3:]}." for h in SECTION_HEADINGS) + "\n" + ("x" * 300)


@pytest.fixture
def tender(db):
    tid = next(_ids)
    db.add(Tender(id=tid, portal="command_center", tender_id=f"cc-{uuid.uuid4().hex[:8]}",
                  title="New Session", source_url="x"))
    keys = [f"chat/{tid}/nit.pdf", f"chat/{tid}/rfp.pdf", f"chat/{tid}/annex.pdf"]
    for k in keys:
        # As the streaming handler registers a chat upload.
        db.add(TenderDocument(tender_id=tid, file_name=k.rsplit("/", 1)[1], file_path=k,
                              extraction_status="completed"))
    db.commit()
    yield tid, keys
    db.query(DocumentExtractionResult).filter(DocumentExtractionResult.tender_id == tid).delete()
    db.query(TenderDocument).filter(TenderDocument.tender_id == tid).delete()
    db.query(Tender).filter(Tender.id == tid).delete()
    db.commit()


def _fake_reader(monkeypatch, *, unreadable=(), delay=0.02, track=None):
    calls = []

    async def fake(tender_id, doc_id, doc_name, doc_path, doc_index, doc_total,
                   semaphore, force_refresh=False):
        async with semaphore:
            calls.append(doc_name)
            if track is not None:
                track["now"] += 1
                track["max"] = max(track["max"], track["now"])
            await asyncio.sleep(delay)
            if track is not None:
                track["now"] -= 1
        if doc_name in unreadable:
            return {"doc_id": doc_id, "doc_name": doc_name, "page_count": 3, "summary": None,
                    "unreadable": True, "error": "page_count_exceeded", "source_sha1": f"sha-{tender_id}-{doc_name}"}
        return {
            "doc_id": doc_id, "doc_name": doc_name, "page_count": 10,
            "summary": {"doc_type": "RFP", "doc_id": doc_id, "doc_name": doc_name,
                        "key_facts": {"tender_reference": "ER/LLH/2026/77", "scope_summary": "Coach work"},
                        "requirements": [{"category": "eligibility", "text": "Turnover Rs 2 crore", "page": 4}],
                        "critical_clauses": [{"text": "shall be rejected", "flag_type": "rejection",
                                              "severity": "critical", "page": 7}]},
            "unreadable": False, "error": None, "elapsed_ms": 5, "source_sha1": f"sha-{tender_id}-{doc_name}",
        }

    monkeypatch.setattr(daa, "_analyze_single_document_native", fake)
    return calls


def _fake_report(monkeypatch, text=REPORT):
    seen = []

    async def fake_call_ai(system_prompt, user_prompt, db=None, agent_name=None, **kw):
        seen.append({"system": system_prompt, "user": user_prompt, "agent": agent_name, **kw})
        return text

    monkeypatch.setattr("app.services.ai_service.call_ai", fake_call_ai)
    return seen


# ── the report format is the one-call path's ─────────────────────────────────


def test_the_one_call_prompt_is_unchanged_and_shared():
    """The section spec lives in one place; both paths send it verbatim."""
    import inspect
    from app.services.langchain.graphs import chat_agent_wrappers as caw

    assert "CHAT_REPORT_PDF_PROMPT" in inspect.getsource(caw._run_native_pdf_analysis_inner)
    for prompt in (cua.CHAT_REPORT_PDF_PROMPT, cua.CHAT_REPORT_EXTRACTS_PROMPT):
        assert prompt.startswith(cua.CHAT_REPORT_INTRO)
        assert prompt.endswith(cua.CHAT_REPORT_SPEC)
        for heading in SECTION_HEADINGS:
            assert heading in prompt, heading
    assert "Analyze the attached document(s) and produce a comprehensive 7-section report" \
        in cua.CHAT_REPORT_PDF_PROMPT


def test_uploads_are_reported_in_the_seven_section_format(db, tender, monkeypatch):
    tid, keys = tender
    calls = _fake_reader(monkeypatch)
    seen = _fake_report(monkeypatch)
    msg = "please analyze these\n[ATTACHED FILES]\n- nit.pdf"
    out = asyncio.run(cua.analyze_chat_uploads(db, msg, tid, keys))

    assert sorted(calls) == ["annex.pdf", "nit.pdf", "rfp.pdf"]
    assert out["output"] == REPORT and out["output_type"] == "document_analysis"
    assert out["metrics"]["method"] == "per_doc_upload"
    assert out["metrics"]["documents_count"] == 3
    call = seen[0]
    assert call["system"] == cua.CHAT_REPORT_EXTRACTS_PROMPT
    assert call["agent"] == "deep_analyzer"
    # The user's own words, without the attached-files block, then the records.
    assert call["user"].startswith("please analyze these\n")
    assert "[ATTACHED FILES]" not in call["user"]
    assert "Turnover Rs 2 crore" in call["user"] and "shall be rejected" in call["user"]
    # The per-document records are kept for everything downstream.
    rows = db.query(DocumentExtractionResult).filter(
        DocumentExtractionResult.tender_id == tid,
        DocumentExtractionResult.extraction_type == "per_doc_summary").count()
    assert rows == 3


def test_unreadable_documents_are_named_to_the_report(db, tender, monkeypatch):
    tid, keys = tender
    _fake_reader(monkeypatch, unreadable={"annex.pdf"})
    seen = _fake_report(monkeypatch)
    out = asyncio.run(cua.analyze_chat_uploads(db, "analyze", tid, keys))
    assert out["metrics"]["documents_unreadable"] == 1
    assert '"unreadable":true' in seen[0]["user"] and "annex.pdf" in seen[0]["user"]


# ── not slower: documents run concurrently, within the byte budget ──────────


def test_documents_are_read_concurrently(db, tender, monkeypatch):
    tid, keys = tender
    track = {"now": 0, "max": 0}
    _fake_reader(monkeypatch, track=track, delay=0.05)
    _fake_report(monkeypatch)
    asyncio.run(cua.analyze_chat_uploads(db, "analyze", tid, keys,
                                         sizes={k: 1_000_000 for k in keys}))
    assert track["max"] == 3


def test_large_files_are_read_one_at_a_time(db, tender, monkeypatch):
    tid, keys = tender
    track = {"now": 0, "max": 0}
    _fake_reader(monkeypatch, track=track, delay=0.05)
    _fake_report(monkeypatch)
    asyncio.run(cua.analyze_chat_uploads(db, "analyze", tid, keys,
                                         sizes={k: 40 * 1024 * 1024 for k in keys}))
    assert track["max"] == 1


def test_the_byte_gate_lets_a_document_over_budget_run_alone():
    async def run():
        gate = cua._ByteGate(3, 100)
        order = []

        async def job(name, size):
            async with gate.slot(size):
                order.append(("in", name))
                await asyncio.sleep(0.01)
                order.append(("out", name))

        await asyncio.gather(job("big", 500), job("a", 10), job("b", 10))
        return order

    order = asyncio.run(run())
    big_in, big_out = order.index(("in", "big")), order.index(("out", "big"))
    assert all(not (big_in < order.index(("in", n)) < big_out) for n in ("a", "b"))


# ── nothing is read twice ────────────────────────────────────────────────────


def test_the_same_files_and_request_return_the_earlier_report(db, tender, monkeypatch):
    tid, keys = tender
    _fake_reader(monkeypatch)
    seen = _fake_report(monkeypatch)
    first = asyncio.run(cua.analyze_chat_uploads(db, "analyze these", tid, keys))
    second = asyncio.run(cua.analyze_chat_uploads(db, "analyze these", tid, keys))
    assert len(seen) == 1, "the report was written twice"
    assert second["metrics"]["report_reused"] is True
    assert second["output"].startswith(first["output"].rstrip())
    assert "re-analyze" in second["output"]
    # A different request is a different report.
    asyncio.run(cua.analyze_chat_uploads(db, "what are the eligibility rules?", tid, keys))
    assert len(seen) == 2


def test_asking_again_writes_it_afresh(db, tender, monkeypatch):
    tid, keys = tender
    _fake_reader(monkeypatch)
    seen = _fake_report(monkeypatch)
    asyncio.run(cua.analyze_chat_uploads(db, "analyze these", tid, keys))
    asyncio.run(cua.analyze_chat_uploads(db, "analyze these", tid, keys, force_refresh=True))
    assert len(seen) == 2


# ── every failure falls back to the one-call path ────────────────────────────


def test_unregistered_attachments_fall_back(db, tender, monkeypatch):
    tid, _keys = tender
    _fake_reader(monkeypatch)
    _fake_report(monkeypatch)
    assert asyncio.run(cua.analyze_chat_uploads(db, "analyze", tid, ["chat/elsewhere.pdf"])) is None


def test_nothing_readable_falls_back(db, tender, monkeypatch):
    tid, keys = tender
    _fake_reader(monkeypatch, unreadable={"nit.pdf", "rfp.pdf", "annex.pdf"})
    seen = _fake_report(monkeypatch)
    assert asyncio.run(cua.analyze_chat_uploads(db, "analyze", tid, keys)) is None
    assert seen == []


def test_a_placeholder_report_falls_back(db, tender, monkeypatch):
    tid, keys = tender
    _fake_reader(monkeypatch)
    _fake_report(monkeypatch, text="Agent disabled.")
    assert asyncio.run(cua.analyze_chat_uploads(db, "analyze", tid, keys)) is None


def test_chat_analysis_uses_the_per_document_path_then_the_one_call_path(db, tender, monkeypatch):
    from app.services.langchain.graphs import chat_agent_wrappers as caw

    tid, keys = tender
    meta = {"attachment_paths": [{"path": k, "is_pdf": True, "size": 1000} for k in keys]}
    legacy = []

    async def fake_legacy(*a, **k):
        legacy.append(True)
        return {"output": "one-call report", "output_type": "document_analysis",
                "status": "completed", "metrics": {}, "tool_calls": [], "agent_key": "deep_analyzer"}

    monkeypatch.setattr(caw, "_analyze_uploaded_document", fake_legacy)
    monkeypatch.setattr(caw, "_kick_post_analysis_pipeline", lambda tid: None)

    async def per_doc(*a, **k):
        return {"output": "per-doc report", "output_type": "document_analysis",
                "status": "completed", "metrics": {"method": "per_doc_upload"},
                "tool_calls": [], "agent_key": "deep_analyzer"}

    monkeypatch.setattr(cua, "analyze_chat_uploads", per_doc)
    out = asyncio.run(caw.chat_document_analysis(db, "analyze", tender_id=tid, file_metadata=meta))
    assert out["output"] == "per-doc report" and legacy == []

    async def none(*a, **k):
        return None

    monkeypatch.setattr(cua, "analyze_chat_uploads", none)
    out = asyncio.run(caw.chat_document_analysis(db, "analyze", tender_id=tid, file_metadata=meta))
    assert out["output"] == "one-call report" and legacy == [True]


def test_attachment_sizes_are_read_from_the_metadata():
    from app.services.langchain.graphs.chat_agent_wrappers import _attachment_sizes

    meta = {"attachment_paths": [{"path": "a", "size": 12}, {"path": "b"}, {"path": "c", "size": "x"}]}
    assert _attachment_sizes(meta) == {"a": 12}
    assert _attachment_sizes(None) == {}


# ── the checklist reads the chat report's document list ──────────────────────


def test_the_checklist_reads_section_two_of_a_chat_report(db, tender):
    from app.services.checklist_service import required_documents_from_analysis

    tid, _keys = tender
    report = REPORT.replace(
        "Content for 2. COMPLETE DOCUMENT REQUIREMENTS.",
        "| Document | Mandatory |\n|---|---|\n| EMD of Rs 1,00,000 | Mandatory |\n"
        "| RDSO vendor approval | Mandatory |\n| Annexure-III declaration | Mandatory |\n"
        "| Power of attorney | Optional |",
    )
    db.add(DocumentExtractionResult(
        tender_id=tid, document_name="chat_attachment_analysis", extraction_type="full_analysis",
        items=[], raw_text=report,
    ))
    db.commit()
    body = required_documents_from_analysis(db, tid)
    assert "RDSO vendor approval" in body and "Annexure-III declaration" in body
    assert "ELIGIBILITY ASSESSMENT" not in body


# ── the files decide which tender they are analysed on ──────────────────────


def test_the_attached_files_name_their_tender(db, tender):
    tid, keys = tender
    assert cua.tender_of_attachments(db, keys) == tid
    assert cua.tender_of_attachments(db, ["chat/unknown.pdf"]) is None
    assert cua.tender_of_attachments(db, []) is None


def test_a_wrong_delegated_tender_id_is_corrected_by_the_files(db, tender, monkeypatch):
    """A delegated tender_id is the model's; the upload was registered on the
    session's tender, and that is where it is analysed and persisted."""
    from app.services.langchain.graphs import chat_agent_wrappers as caw

    tid, keys = tender
    meta = {"attachment_paths": [{"path": k, "is_pdf": True} for k in keys]}
    seen = {}

    async def per_doc(db_, message, tender_id, pdf_keys, **kw):
        seen["tender_id"] = tender_id
        return {"output": "r", "output_type": "document_analysis", "status": "completed",
                "metrics": {}, "tool_calls": [], "agent_key": "deep_analyzer"}

    persisted = []
    monkeypatch.setattr(cua, "analyze_chat_uploads", per_doc)
    monkeypatch.setattr(caw, "_persist_chat_analysis_to_db",
                        lambda db_, t, r: persisted.append(t))
    monkeypatch.setattr(caw, "_kick_post_analysis_pipeline", lambda t: None)
    asyncio.run(caw.chat_document_analysis(db, "analyze", tender_id=tid + 12345,
                                           file_metadata=meta))
    assert seen["tender_id"] == tid and persisted == [tid]
