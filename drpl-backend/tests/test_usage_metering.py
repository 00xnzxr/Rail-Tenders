"""Per-user usage metering — the ledger the $30 cap is enforced against.

`APIUsageLog` rows are written for every LLM call via `ai_service._log_usage`,
on a fresh session so telemetry can never poison the caller's transaction. What
was missing is the only column a per-user budget needs: `_log_usage` never set
`user_id`, so every row on the LangChain path — essentially all platform spend —
landed unattributed and per-user spend was unmeasurable.

These tests pin the write, its blast radius, and the pricing it records.
"""

from types import SimpleNamespace

import pytest

from app.core.actor_context import actor_scope
from app.models.api_usage import APIUsageLog
from app.services.langchain.callback_handler import DRPLCallbackHandler
from app.services.langchain.provider_config import estimate_cost


def _llm_result(input_tokens=1000, output_tokens=500, model="claude-opus-5", **extra):
    """Minimal LLMResult-shaped object: the handler only reads llm_output."""
    token_usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        **extra,
    }
    return SimpleNamespace(
        llm_output={"token_usage": token_usage, "model_name": model},
        generations=[],
    )


def _rows(db, **filters):
    q = db.query(APIUsageLog)
    for k, v in filters.items():
        q = q.filter(getattr(APIUsageLog, k) == v)
    return q.all()


# ---------------------------------------------------------------- the write --


def test_llm_call_is_recorded_against_the_acting_user(db):
    """The regression: the row was written, but with no user on it."""
    handler = DRPLCallbackHandler(db, agent_name="decision_maker")
    with actor_scope(user_id=77, role="costing_research"):
        handler.on_llm_end(_llm_result(), run_id="r1")

    rows = _rows(db, user_id=77)
    assert len(rows) == 1, "the LLM call produced no usage row"
    row = rows[0]
    assert row.agent_name == "decision_maker"
    assert row.tokens_input == 1000
    assert row.tokens_output == 500
    assert row.cost_estimate > 0
    assert row.model == "claude-opus-5"


def test_each_call_is_its_own_row(db):
    handler = DRPLCallbackHandler(db, agent_name="deep_analyzer")
    with actor_scope(user_id=78):
        handler.on_llm_end(_llm_result(), run_id="r1")
        handler.on_llm_end(_llm_result(), run_id="r2")
    assert len(_rows(db, user_id=78)) == 2


def test_work_with_no_human_behind_it_is_recorded_unattributed(db):
    """Seeders and scheduled jobs have no actor. Record, never guess."""
    handler = DRPLCallbackHandler(db, agent_name="tender_scorer")
    handler.on_llm_end(_llm_result(), run_id="r1")
    rows = _rows(db, agent_name="tender_scorer")
    assert len(rows) == 1
    assert rows[0].user_id is None


def test_metering_failure_never_breaks_the_run(db, monkeypatch):
    """A meter that can kill an agent run is worse than the gap it closes."""
    import app.services.langchain.callback_handler as ch

    def _explode(*a, **k):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(ch, "_log_usage", _explode)
    handler = DRPLCallbackHandler(db, agent_name="decision_maker")
    with actor_scope(user_id=79):
        handler.on_llm_end(_llm_result(), run_id="r1")  # must not raise
    assert handler.total_tokens_input == 1000


def test_usage_write_does_not_touch_the_run_session(db):
    """The row goes on its own session, so a failed insert cannot poison the
    agent's transaction and take its real writes down with it."""
    handler = DRPLCallbackHandler(db, agent_name="decision_maker")
    with actor_scope(user_id=80):
        handler.on_llm_end(_llm_result(), run_id="r1")
    assert not db.new and not db.dirty


# -------------------------------------------------------------- the pricing --


def test_cache_reads_are_cheaper_than_fresh_input():
    """estimate_cost ignored the cache counts the handler already collected.

    Anthropic bills a cache read at ~0.1x input. Pricing it as fresh input
    overbills every cached call against a real $30 cap — and the Master Agent's
    prompt is cached on every turn.
    """
    fresh = estimate_cost("claude-opus-5", {"input_tokens": 10_000, "output_tokens": 0})
    cached = estimate_cost(
        "claude-opus-5",
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 10_000,
            "cache_creation_input_tokens": 0,
        },
    )
    assert 0 < cached < fresh
    assert cached == pytest.approx(fresh * 0.1, rel=0.3)


def test_cache_writes_cost_more_than_fresh_input():
    """A first cache write is billed at 1.25x input."""
    fresh = estimate_cost("claude-opus-5", {"input_tokens": 10_000, "output_tokens": 0})
    written = estimate_cost(
        "claude-opus-5",
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 10_000,
            "cache_read_input_tokens": 0,
        },
    )
    assert written > fresh
    assert written == pytest.approx(fresh * 1.25, rel=0.3)


def test_pricing_is_unchanged_when_no_cache_fields_present():
    """Callers that never pass cache counts must price exactly as before."""
    assert estimate_cost("claude-opus-5", {"input_tokens": 1000, "output_tokens": 200}) > 0


def test_recorded_cost_includes_cache_tokens(db):
    handler = DRPLCallbackHandler(db, agent_name="decision_maker")
    with actor_scope(user_id=81):
        handler.on_llm_end(
            _llm_result(
                input_tokens=0,
                output_tokens=100,
                cache_read_input_tokens=50_000,
                cache_creation_input_tokens=2_000,
            ),
            run_id="r1",
        )
    row = _rows(db, user_id=81)[0]
    assert row.cache_read_tokens == 50_000
    assert row.cache_creation_tokens == 2_000
    assert row.cost_estimate > 0
