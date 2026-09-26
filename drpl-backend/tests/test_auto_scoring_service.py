"""Batch scoring writes results, sets below_threshold correctly, and caps retries."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
import app.services.auto_scoring_service as svc


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _stub_prompt_and_llm(monkeypatch, reply):
    monkeypatch.setattr(svc, "get_scoring_system_prompt", lambda db: "SYS")
    async def _fake_call_ai(system, user, db, agent, model_override=None, force_cache_system=False):
        return reply
    monkeypatch.setattr(svc, "call_ai", _fake_call_ai)


def test_score_writes_relevance_and_flags_below_threshold(monkeypatch):
    db = _session()
    t = Tender(portal="gem", tender_id="1", title="Railway AMC",
               estimated_value=4_000_000.0)   # under ₹50L
    db.add(t); db.commit()
    _stub_prompt_and_llm(monkeypatch, "MATCH: 88\nREASON: strong fit")

    out = svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert round(t.ai_relevance_score, 2) == 0.88
    assert t.fit_reasoning == "strong fit"
    assert t.below_threshold is True
    assert out == {"scored": 1, "flagged_below_threshold": 1, "failed": 0, "shared": 0}


def test_unknown_value_not_flagged(monkeypatch):
    db = _session()
    t = Tender(portal="gem", tender_id="2", title="X", estimated_value=None)
    db.add(t); db.commit()
    _stub_prompt_and_llm(monkeypatch, "MATCH: 70\nREASON: ok")
    svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert t.below_threshold is False
    assert round(t.ai_relevance_score, 2) == 0.70


def test_failure_increments_attempts_and_excludes_after_cap(monkeypatch):
    db = _session()
    t = Tender(portal="gem", tender_id="3", title="X")
    db.add(t); db.commit()
    monkeypatch.setattr(svc, "get_scoring_system_prompt", lambda db: "SYS")
    async def _boom(system, user, db, agent, model_override=None, force_cache_system=False):
        raise RuntimeError("api down")
    monkeypatch.setattr(svc, "call_ai", _boom)

    for _ in range(3):   # max_retries default = 3
        svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert t.scoring_attempts == 3
    assert t.ai_relevance_score is None
    assert t.id not in svc.find_unscored_tender_ids(db)   # excluded at cap


def test_find_unscored_excludes_already_scored(monkeypatch):
    db = _session()
    scored = Tender(portal="gem", tender_id="4", title="A", ai_relevance_score=0.5)
    unscored = Tender(portal="gem", tender_id="5", title="B")
    db.add_all([scored, unscored]); db.commit()
    ids = svc.find_unscored_tender_ids(db)
    assert unscored.id in ids
    assert scored.id not in ids


def test_scoring_call_forces_system_cache(monkeypatch):
    db = _session()
    t = Tender(portal="gem", tender_id="c1", title="X")
    db.add(t); db.commit()
    monkeypatch.setattr(svc, "get_scoring_system_prompt", lambda db: "SYS")
    seen = {}
    async def _capture(system, user, db, agent, model_override=None, force_cache_system=False):
        seen["force"] = force_cache_system
        return "MATCH: 50\nREASON: ok"
    monkeypatch.setattr(svc, "call_ai", _capture)
    svc.score_tenders_batch(db, [t.id])
    assert seen["force"] is True
