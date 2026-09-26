"""Pipeline steps that were several model calls, or a model call over numbers.

- The manual "Analyze" ran four sequential calls (category, relevance, risk,
  summary) over the identical tender context. One call answers all four;
  a malformed reply or an admin customisation falls back to the four.
- Completeness was an LLM call whose whole input was item counts and whose
  prompt was a list of arithmetic rules.
"""

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
import app.services.ai_service as ai
from app.services.tender_analysis_service import completeness_from_counts


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _tender(db):
    t = Tender(portal="ireps", tender_id="Q1", title="Supply of traction motor bearings",
               description="500 nos bearings for WAG-9", estimated_value=4_000_000.0)
    db.add(t); db.commit()
    return t


def test_quick_analysis_is_one_call(monkeypatch):
    db = _session()
    t = _tender(db)
    calls = []

    async def _fake(system, user, db_, agent, **kw):
        calls.append(agent)
        return ('{"category": "Electrical", "relevance": 0.92, "risk": 0.3, '
                '"summary": "Bearings for WAG-9 traction motors; core railway supply."}')

    monkeypatch.setattr(ai, "call_ai", _fake)
    out = asyncio.run(ai.analyze_tender(db, t.id))
    assert len(calls) == 1
    assert out["ai_category"] == "Electrical"
    assert out["ai_relevance_score"] == 0.92 and out["ai_risk_score"] == 0.3
    assert out["ai_summary"].startswith("Bearings")


def test_a_malformed_reply_falls_back_to_the_four_calls(monkeypatch):
    db = _session()
    t = _tender(db)
    calls = []

    async def _fake(system, user, db_, agent, **kw):
        calls.append(agent)
        if len(calls) == 1:
            return "I think it is electrical."
        return {"classifier": "Electrical", "relevance": "0.9", "risk": "0.2",
                "summary": "A summary."}[agent]

    monkeypatch.setattr(ai, "call_ai", _fake)
    out = asyncio.run(ai.analyze_tender(db, t.id))
    assert calls[1:] == ["classifier", "relevance", "risk", "summary"]
    assert out["ai_category"] == "Electrical" and out["ai_relevance_score"] == 0.9


def test_an_unknown_category_is_other_and_scores_are_clamped(monkeypatch):
    db = _session()
    t = _tender(db)

    async def _fake(*a, **k):
        return '{"category": "Railways", "relevance": 1.7, "risk": -2, "summary": "x"}'

    monkeypatch.setattr(ai, "call_ai", _fake)
    out = asyncio.run(ai.analyze_tender(db, t.id))
    assert out["ai_category"] == "Other"
    assert out["ai_relevance_score"] == 1.0 and out["ai_risk_score"] == 0.0


def test_a_customised_agent_keeps_the_separate_calls(monkeypatch):
    db = _session()
    t = _tender(db)
    calls = []
    monkeypatch.setattr(ai, "_quick_analysis_customised", lambda _db: True)

    async def _fake(system, user, db_, agent, **kw):
        calls.append(agent)
        return {"classifier": "Civil", "relevance": "0.1", "risk": "0.5", "summary": "s"}[agent]

    monkeypatch.setattr(ai, "call_ai", _fake)
    asyncio.run(ai.analyze_tender(db, t.id))
    assert calls == ["classifier", "relevance", "risk", "summary"]


def test_completeness_follows_the_old_prompts_rules():
    assert completeness_from_counts({}) == 0.0
    full = {c: 10 for c in ("requirements", "eligibility", "terms_conditions",
                            "technical_specs", "financial", "experience", "compliance")}
    assert completeness_from_counts(full) == 1.0
    # A missing major category scores lower than the same total spread over it.
    no_financial = dict(full, financial=0, requirements=20)
    assert completeness_from_counts(no_financial) < completeness_from_counts(full)
    # Fewer than five items reads as an incomplete extraction.
    assert completeness_from_counts({"requirements": 2, "eligibility": 1, "financial": 1}) <= 0.3
    # More items, all else equal, never scores lower.
    assert completeness_from_counts({"requirements": 30, "eligibility": 5, "financial": 5}) >= \
        completeness_from_counts({"requirements": 10, "eligibility": 5, "financial": 5})


def test_completeness_makes_no_model_call(monkeypatch):
    import inspect
    from app.services import tender_analysis_service as tas

    assert "call_ai" not in inspect.getsource(tas.score_completeness)
    assert "call_ai" not in inspect.getsource(tas.completeness_from_counts)


# ── session titles and workspace format instructions ────────────────────────


def test_session_titles_need_no_model():
    from app.services.langchain.streaming_handler import session_title_from

    assert session_title_from("x", "Supply of 500 composite brake blocks") == \
        "Supply of 500 composite brake blocks"
    assert session_title_from(
        "hi, can you please do the costing properly and completely for this tender"
    ).startswith("Do the costing properly")
    assert session_title_from("What did the costing say about Schedule B? Also GST") == \
        "What did the costing say about Schedule B"
    assert session_title_from("   ") is None
    # A default tender title is not a name.
    assert session_title_from("analyze it", "New Session") == "Analyze it"


def test_session_title_generation_makes_no_model_call():
    import inspect
    from app.services.langchain import streaming_handler as sh

    src = inspect.getsource(sh._generate_session_title)
    assert "get_chat_model" not in src and "ainvoke" not in src


def test_workspace_format_instructions_go_through_the_metered_path(monkeypatch):
    """No raw SDK client, no dated model pin, no env-only key."""
    import inspect
    from app.services import workspace_service as ws

    src = inspect.getsource(ws._ai_extract_format_instructions)
    assert "anthropic.Anthropic" not in src
    assert "claude-sonnet-4-20250514" not in src
    assert "call_ai" in src


# ── the checklist reads the analysis's required-documents table ─────────────


_REPORT = """### SECTION 5: CRITICAL CLAUSES
| # | Clause |
|---|---|
| 1 | x |

### SECTION 6: REQUIRED DOCUMENTS LIST
| Document Name (exact) | Mandatory/Optional | Format | Envelope | Notes | Source |
|---|---|---|---|---|---|
| RDSO vendor approval certificate | Mandatory | self-attested | Technical | valid | NIT p.4 |
| Annexure-III declaration | Mandatory | original | Technical | signed | Annex |

### SECTION 7: COSTING BASIS HANDOFF
7.1 ...
"""


def test_the_checklist_is_built_from_section_six(db, monkeypatch):
    from app.models.checklist import ChecklistItem
    from app.models.document_analysis import TenderAnalysisSummary
    from app.services import checklist_service as cs

    t = Tender(portal="ireps", tender_id="CHK-1", title="Coach rehab")
    db.add(t); db.commit()
    db.add(TenderAnalysisSummary(tender_id=t.id, analysis_status="completed",
                                 requirement_summary=_REPORT))
    db.commit()
    seen = {}

    async def _parse(tender_text, document_text="", db=None, **kw):
        seen["doc"] = document_text
        seen.update(kw)
        return [{"name": "RDSO vendor approval certificate", "is_required": True}]

    async def _classify(db, checklist_items, tender_id):
        return [{} for _ in checklist_items]

    monkeypatch.setattr(cs, "parse_document_checklist", _parse)
    monkeypatch.setattr(cs, "classify_checklist_items", _classify)
    try:
        items = asyncio.run(cs.generate_checklist(db, t.id))
        assert "RDSO vendor approval certificate" in seen["doc"]
        assert "Annexure-III declaration" in seen["doc"]
        assert "COSTING BASIS" not in seen["doc"] and "CRITICAL CLAUSES" not in seen["doc"]
        assert seen["max_document_chars"] > 4000
        assert items and items[0].item_name == "RDSO vendor approval certificate"
    finally:
        db.query(ChecklistItem).filter_by(tender_id=t.id).delete()
        db.query(TenderAnalysisSummary).filter_by(tender_id=t.id).delete()
        db.delete(t); db.commit()


def test_a_heading_without_rows_is_not_a_list(db):
    from app.models.document_analysis import TenderAnalysisSummary
    from app.services.checklist_service import required_documents_from_analysis

    t = Tender(portal="ireps", tender_id="CHK-2", title="x")
    db.add(t); db.commit()
    db.add(TenderAnalysisSummary(tender_id=t.id, analysis_status="completed",
                                 requirement_summary="### SECTION 6: REQUIRED DOCUMENTS LIST\nNone found.\n"))
    db.commit()
    try:
        assert required_documents_from_analysis(db, t.id) == ""
    finally:
        db.query(TenderAnalysisSummary).filter_by(tender_id=t.id).delete()
        db.delete(t); db.commit()
