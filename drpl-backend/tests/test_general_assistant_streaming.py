"""Streaming a ReAct agent into a UI that concatenates tokens.

`CommandCenterPage` accumulates every `token` event into one string and saves
that as the message. A ReAct agent has several model turns, so anything it says
on the way to a tool call — "Let me search for that" — would be glued onto the
front of the final answer and persisted there.

`token_reset` tells the client to drop what it has: that turn was narration,
not the reply.
"""

import asyncio
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401
import app.models.agent_memory  # noqa: F401
from app.core.database import Base, engine
from app.services.langchain.graphs import general_assistant_agent as ga


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


class _FakeAgent:
    """Two model turns: narration + tool call, then the real answer."""

    def __init__(self, events):
        self._events = events

    async def astream_events(self, _inputs, config=None, version=None):
        for ev in self._events:
            yield ev


def _chunk(text):
    return SimpleNamespace(content=text)


def _ai(text, tool_calls=None):
    return SimpleNamespace(content=text, tool_calls=tool_calls or [])


NARRATED_RUN = [
    {"event": "on_chat_model_stream", "data": {"chunk": _chunk("Let me search for that.")}},
    {"event": "on_chat_model_end",
     "data": {"output": _ai("Let me search for that.",
                            [{"name": "web_search", "args": {}, "id": "1"}])}},
    {"event": "on_tool_start", "name": "web_search", "data": {"input": {"query": "rate"}}},
    {"event": "on_tool_end", "name": "web_search",
     "data": {"output": "Found https://example.gov.in/rates"}},
    {"event": "on_chat_model_stream", "data": {"chunk": _chunk("The rate is ")}},
    {"event": "on_chat_model_stream", "data": {"chunk": _chunk("₹75,940.72.")}},
    {"event": "on_chat_model_end", "data": {"output": _ai("The rate is ₹75,940.72.")}},
]


def _run(db, monkeypatch, events):
    """Drive run_general_assistant over a scripted event stream."""
    monkeypatch.setattr(ga, "_build_tools", lambda *a, **k: [])
    monkeypatch.setattr(
        "langgraph.prebuilt.create_react_agent",
        lambda **kwargs: _FakeAgent(events),
    )
    monkeypatch.setattr(
        "app.services.langchain.llm_factory.get_chat_model",
        lambda *a, **k: object(),
    )

    captured: list[tuple[str, dict]] = []

    result = asyncio.run(ga.run_general_assistant(
        db, "what is the rate?",
        tender_id=301, session_id="s", user_id=1,
        stream_callback=lambda name, data: captured.append((name, data)),
    ))
    return result, captured


def test_pre_tool_narration_is_discarded(db, monkeypatch):
    result, events = _run(db, monkeypatch, NARRATED_RUN)

    assert result["output"] == "The rate is ₹75,940.72."
    assert "Let me search" not in result["output"]


def test_a_token_reset_is_emitted_before_the_final_turn(db, monkeypatch):
    _result, events = _run(db, monkeypatch, NARRATED_RUN)
    names = [name for name, _ in events]

    assert "token_reset" in names
    reset_at = names.index("token_reset")
    after = "".join(
        data["content"] for name, data in events[reset_at + 1:] if name == "token"
    )
    assert after == "The rate is ₹75,940.72."


def test_tool_activity_is_reported_to_the_ui(db, monkeypatch):
    """The existing agent_status pill, so a search isn't a silent pause."""
    _result, events = _run(db, monkeypatch, NARRATED_RUN)
    statuses = [data for name, data in events if name == "agent_status"]

    assert any(s["phase"] == "tool_running" and "Searching" in s["message"] for s in statuses)
    assert any(s["phase"] == "tool_done" for s in statuses)


def test_only_urls_a_tool_returned_are_cited(db, monkeypatch):
    """The bug was three domains the model never visited. Sources come from
    tool observations, not from the model."""
    result, _events = _run(db, monkeypatch, NARRATED_RUN)

    assert result["sources"] == ["https://example.gov.in/rates"]


def test_a_clean_single_turn_run_needs_no_reset(db, monkeypatch):
    events = [
        {"event": "on_chat_model_stream", "data": {"chunk": _chunk("Hello.")}},
        {"event": "on_chat_model_end", "data": {"output": _ai("Hello.")}},
    ]
    result, emitted = _run(db, monkeypatch, events)

    assert result["output"] == "Hello."
    assert "token_reset" not in [name for name, _ in emitted]
