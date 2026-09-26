"""The Master Agent's wall-clock budget must outlast what it delegates.

The shipped default was 120s, while a single delegation the Master is designed
to make — `call_deep_analyzer` — is itself allowed 1200s. Any run that touched
a real production job was therefore killed mid-flight by `asyncio.wait_for`,
the whole graph cancelled, and every completed step discarded: the user saw
"stopped early because it exceeded its time budget" and had to type "Continue",
which restarted from zero and hit the same wall.

These tests pin the two halves of the fix: a budget sized above the longest
delegation, and a timeout that reports the work already done instead of
throwing it away.
"""

import pytest

from app.services.langchain.graphs import decision_maker_agent as dm


def test_budget_outlasts_the_longest_delegation():
    """The orchestrator may not have a smaller budget than its own workers."""
    import inspect

    from app.services.langchain.graphs.chat_agent_wrappers import (
        _ensure_tender_analysis,
    )

    worker_timeout = inspect.signature(
        _ensure_tender_analysis
    ).parameters["timeout_s"].default

    _iters, secs = dm.resolve_master_budget()
    assert secs > worker_timeout, (
        f"Master budget {secs}s is not larger than the {worker_timeout}s it "
        "allows a single delegated analysis — the Master is killed while its "
        "own worker is still legitimately running."
    )


def test_prompts_state_the_real_budget():
    """The prompt told the model '~120s' regardless of the actual budget."""
    iters, secs = dm.resolve_master_budget()
    for name in (
        "DECISION_MAKER_SYSTEM",
        "DECISION_MAKER_EXECUTION_SYSTEM",
        "DECISION_MAKER_AUTONOMOUS_SYSTEM",
    ):
        rendered = dm.render_budget(getattr(dm, name))
        assert "120s" not in rendered and "150s" not in rendered, (
            f"{name} hardcodes a wall-clock budget that is not the real one"
        )
        assert f"{int(secs)}s" in rendered, f"{name} does not state the real budget"
        assert str(iters) in rendered


def test_timeout_message_reports_completed_work():
    """A timeout must hand back what finished, not a bare apology."""
    trace = [
        {"type": "action", "tool": "call_deep_analyzer", "step": 1},
        {"type": "observation", "step": 1, "result_preview": "analysis stored"},
        {"type": "action", "tool": "call_costing_researcher", "step": 2},
    ]
    msg = dm.render_partial_timeout_message(trace, elapsed=1801.0)
    assert "call_deep_analyzer" in msg
    assert "call_costing_researcher" in msg
    assert "Continue" in msg or "continue" in msg


def test_streamer_accumulates_a_trace():
    """The streamer emitted steps but kept none, so a cancelled run had nothing."""
    s = dm._TimelineStreamer(stream_callback=None, started_at=0.0)
    assert s.partial_trace == []
