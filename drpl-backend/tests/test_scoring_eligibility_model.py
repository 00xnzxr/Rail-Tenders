"""Regression test: score_relevance and check_eligibility must never fall through
to the platform default (Sonnet) model. They should always pass an explicit
Haiku model_override to call_ai — either the live-tunable auto_scoring_model
setting (when db is available) or the hard-coded Haiku fallback (when db=None).
"""
import asyncio

import app.services.ai_service as ai
from app.models.tender import Tender


def _capture(monkeypatch):
    seen = {}

    async def fake_call_ai(system, user, db, agent, model_override=None, **kw):
        seen["model"] = model_override
        seen["agent"] = agent
        if agent == "relevance":
            return "0.8"
        return '{"eligible": true, "score": 0.9, "notes": "ok"}'

    monkeypatch.setattr(ai, "call_ai", fake_call_ai)
    return seen


def test_relevance_uses_haiku(monkeypatch):
    seen = _capture(monkeypatch)
    t = Tender(portal="gem", tender_id="1", title="X")
    result = asyncio.run(ai.score_relevance(t, db=None))
    assert seen["agent"] == "relevance"
    assert seen["model"] == "claude-haiku-4-5"
    assert result == 0.8


def test_eligibility_uses_haiku(monkeypatch):
    seen = _capture(monkeypatch)
    # check_eligibility takes a plain tender_text string, not a Tender object.
    result = asyncio.run(ai.check_eligibility("Some tender description text", db=None))
    assert seen["agent"] == "eligibility"
    assert seen["model"] == "claude-haiku-4-5"
    assert result["eligible"] is True
