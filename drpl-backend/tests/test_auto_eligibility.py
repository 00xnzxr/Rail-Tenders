"""IREPS blue-tick eligibility rides on the scoring call.

It used to be a second Haiku call per blue-tick tender -- the same capability
blurb, the same model and the tender text again, straight after the score --
whose own "score" duplicated the relevance score and was read nowhere. The
scorer now answers one more line, ``ELIGIBLE: yes|no``, for those tenders
only, and a missing line means "unknown", never "not eligible".
"""
import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tender import Tender
import app.services.auto_scoring_service as svc
from app.services.auto_scoring_helpers import parse_eligible_reply, parse_score_reply


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _stub(monkeypatch, reply_for):
    """reply_for(user_prompt) -> reply text; records every call."""
    monkeypatch.setattr(svc, "get_scoring_system_prompt", lambda db: "SYS")
    calls = []

    async def _fake_call_ai(system, user, db, agent, model_override=None, force_cache_system=False):
        calls.append((agent, user))
        return reply_for(user)

    monkeypatch.setattr(svc, "call_ai", _fake_call_ai)
    return calls


def test_one_call_per_tender_and_eligibility_only_for_blue_tick(monkeypatch):
    db = _session()
    ireps = Tender(portal="ireps", tender_id="1", title="Railway AMC",
                   is_eligible_indicator=True, estimated_value=9_000_000.0)
    gem = Tender(portal="gem", tender_id="2", title="Non-IREPS tender",
                 estimated_value=9_000_000.0)
    db.add_all([ireps, gem]); db.commit()

    calls = _stub(monkeypatch, lambda u: (
        "MATCH: 88\nREASON: strong fit\nELIGIBLE: yes" if "ELIGIBLE" in u
        else "MATCH: 88\nREASON: strong fit"
    ))

    out = svc.score_tenders_batch(db, [ireps.id, gem.id])
    db.refresh(ireps); db.refresh(gem)

    assert out["scored"] == 2
    assert [a for a, _u in calls] == ["tender_scorer", "tender_scorer"], "a second call was made"
    asked = [u for _a, u in calls if "ELIGIBLE" in u]
    assert len(asked) == 1 and "Railway AMC" in asked[0]

    assert ireps.eligibility_status == "eligible"
    assert round(ireps.eligibility_score, 2) == 0.88
    assert ireps.fit_reasoning == "strong fit"
    assert gem.eligibility_status is None
    assert gem.fit_reasoning == "strong fit"


def test_ireps_non_blue_tick_is_not_asked(monkeypatch):
    db = _session()
    t = Tender(portal="ireps", tender_id="3", title="Railway AMC 2",
               is_eligible_indicator=False, estimated_value=9_000_000.0)
    db.add(t); db.commit()
    calls = _stub(monkeypatch, lambda u: "MATCH: 70\nREASON: ok")
    svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert all("ELIGIBLE" not in u for _a, u in calls)
    assert t.eligibility_status is None


def test_a_missing_eligible_line_is_unknown_not_ineligible(monkeypatch):
    db = _session()
    t = Tender(portal="ireps", tender_id="4", title="Railway AMC 3",
               is_eligible_indicator=True, estimated_value=9_000_000.0)
    db.add(t); db.commit()
    _stub(monkeypatch, lambda u: "MATCH: 88\nREASON: strong fit")
    out = svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert out["scored"] == 1 and out["failed"] == 0
    assert round(t.ai_relevance_score, 2) == 0.88
    assert t.eligibility_status is None


def test_a_no_is_recorded(monkeypatch):
    db = _session()
    t = Tender(portal="ireps", tender_id="5", title="Hospital linen supply",
               is_eligible_indicator=True, estimated_value=9_000_000.0)
    db.add(t); db.commit()
    _stub(monkeypatch, lambda u: "MATCH: 10\nREASON: different domain\nELIGIBLE: no")
    svc.score_tenders_batch(db, [t.id])
    db.refresh(t)
    assert t.eligibility_status == "not_eligible"
    assert t.fit_reasoning == "different domain"


def test_identical_requests_share_one_call(monkeypatch):
    """GeM lists one bid under several ids; the prompt is the whole input."""
    db = _session()
    twins = [Tender(portal="gem", tender_id=str(10 + i), title="Supply of brake blocks",
                    description="Composition brake blocks, 500 nos", estimated_value=9e6)
             for i in range(3)]
    db.add_all(twins); db.commit()
    calls = _stub(monkeypatch, lambda u: "MATCH: 91\nREASON: core")
    out = svc.score_tenders_batch(db, [t.id for t in twins])
    assert len(calls) == 1 and out["shared"] == 2 and out["scored"] == 3
    for t in twins:
        db.refresh(t)
        assert round(t.ai_relevance_score, 2) == 0.91


def test_a_shared_failure_fails_every_copy(monkeypatch):
    db = _session()
    twins = [Tender(portal="gem", tender_id=str(20 + i), title="Same", description="Same",
                    estimated_value=9e6) for i in range(2)]
    db.add_all(twins); db.commit()
    monkeypatch.setattr(svc, "get_scoring_system_prompt", lambda db: "SYS")

    async def _down(*a, **k):
        raise RuntimeError("overloaded")

    monkeypatch.setattr(svc, "call_ai", _down)
    out = svc.score_tenders_batch(db, [t.id for t in twins])
    assert out["failed"] == 2 and out["scored"] == 0
    for t in twins:
        db.refresh(t)
        assert t.ai_relevance_score is None and t.scoring_attempts == 1


def test_parsers_keep_reason_and_eligibility_apart():
    reply = "MATCH: 77\nREASON: adjacent coach work\nELIGIBLE: Yes"
    assert parse_score_reply(reply) == (0.77, "adjacent coach work")
    assert parse_eligible_reply(reply) is True
    assert parse_eligible_reply("MATCH: 77\nREASON: x") is None
    assert parse_eligible_reply("MATCH: 1\nREASON: x\nELIGIBLE: no") is False
