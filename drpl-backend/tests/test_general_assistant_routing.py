"""Routing into — and around — the general assistant.

Two halves of the original bug live here. The general path passed no
`tender_id`, so a question about a costing the platform had already produced
could only be answered from the chat scrollback. And it never wrote a
`ProposalMessage`, so whatever it did answer disappeared on reload.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import app.models  # noqa: F401 - registers every table on Base before create_all
import app.models.agent_memory  # noqa: F401 - not re-exported by app.models
from app.core.database import Base, engine
from app.services.langchain import streaming_handler as sh


@pytest.fixture(autouse=True)
def _router_engine(monkeypatch):
    """These tests exercise the ROUTER path. chat_engine now defaults to
    'master', so pin the engine rather than depend on the platform default."""
    real = sh.get_effective_setting

    def fake(db, key, default=None):
        if key == "chat_engine":
            return "router"
        return real(db, key, default)

    monkeypatch.setattr(sh, "get_effective_setting", fake)


@pytest.fixture(autouse=True)
def _schema():
    """The session-scoped schema fixture runs before this module's imports have
    registered every model, so platform_settings can be missing. Cheap to
    re-run: create_all skips tables that already exist."""
    Base.metadata.create_all(bind=engine)


async def _drain(agen) -> list[str]:
    return [event async for event in agen]


def _fake_classification(intent: str, agents: list[str]) -> dict:
    return {
        "intent": intent,
        "selected_agents": agents,
        "metadata": {"classification": {"reasoning": "test"}},
    }


@pytest.fixture
def general_result():
    """What run_general_assistant returns on a clean run."""
    return {
        "output": "The rate is ₹75,940.72.",
        "output_type": "general",
        "agent_key": "general_assistant",
        "tool_calls": [{"tool": "web_search", "input": {"query": "SS 304 rate"}}],
        "sources": ["https://example.gov.in/rates"],
        "status": "completed",
        "pending_action": None,
    }


def test_general_query_passes_the_tender_id(db, general_result):
    """The old path never passed tender_id at all — which is why "why did the
    costing come out this way" was answered rhetorically."""
    captured: dict = {}

    async def fake_run(db_, message, **kwargs):
        captured.update(kwargs)
        captured["message"] = message
        return general_result

    with patch.object(sh, "_fresh_db", return_value=db), \
         patch("app.services.langchain.graphs.agent_router_graph.classify_intent_node",
               new=AsyncMock(return_value=_fake_classification("general_query", []))), \
         patch("app.services.langchain.graphs.general_assistant_agent.run_general_assistant",
               new=fake_run):
        asyncio.run(_drain(sh.stream_router_response(
            db,
            message="why is this rate so high?",
            session_id="test-session",
            tender_id=301,
            user_id=1,
        )))

    assert captured["tender_id"] == 301
    assert captured["session_id"] == "test-session"
    assert captured["user_id"] == 1


def test_general_answer_streams_and_reports_its_sources(db, general_result):
    async def fake_run(db_, message, **kwargs):
        return general_result

    with patch.object(sh, "_fresh_db", return_value=db), \
         patch("app.services.langchain.graphs.agent_router_graph.classify_intent_node",
               new=AsyncMock(return_value=_fake_classification("general_query", []))), \
         patch("app.services.langchain.graphs.general_assistant_agent.run_general_assistant",
               new=fake_run):
        events = asyncio.run(_drain(sh.stream_router_response(
            db,
            message="what is the rate?", session_id="test-session",
            tender_id=301, user_id=1,
        )))

    blob = "".join(events)
    assert "general_assistant" in blob
    assert "https://example.gov.in/rates" in blob


def test_a_named_general_assistant_does_not_fall_through(db, general_result):
    """It is a seeded CustomAgent now, so the classifier can name it. It must
    not land on the generic custom-agent executor."""
    called: dict = {}

    async def fake_run(db_, message, **kwargs):
        called["yes"] = True
        return general_result

    with patch.object(sh, "_fresh_db", return_value=db), \
         patch("app.services.langchain.graphs.agent_router_graph.classify_intent_node",
               new=AsyncMock(return_value=_fake_classification(
                          "orchestrate_complex", ["general_assistant"]))), \
         patch("app.services.langchain.graphs.general_assistant_agent.run_general_assistant",
               new=fake_run):
        asyncio.run(_drain(sh.stream_router_response(
            db,
            message="draft me an email", session_id="test-session", user_id=1,
        )))

    assert called.get("yes"), "general_assistant fell through to the generic executor"


def test_specialist_routing_is_unchanged(db):
    """The generalist must not be inserted in front of the deterministic
    NIT-schedule costing path."""
    dispatched: dict = {}

    async def fake_costing(*args, **kwargs):
        dispatched["costing"] = True
        return {"output": "cost breakdown", "output_type": "cost_breakdown"}

    with patch.object(sh, "_fresh_db", return_value=db), \
         patch("app.services.langchain.graphs.agent_router_graph.classify_intent_node",
               new=AsyncMock(return_value=_fake_classification(
                          "estimate_costing", ["costing_researcher"]))), \
         patch("app.services.langchain.graphs.chat_agent_wrappers.chat_costing_research",
               new=fake_costing):
        asyncio.run(_drain(sh.stream_router_response(
            db,
            message="build the full costing for this tender",
            session_id="test-session", tender_id=301, user_id=1,
        )))

    assert dispatched.get("costing"), "costing no longer reaches its canonical path"


def test_general_answer_is_persisted_as_a_proposal_message(db, general_result):
    """The general branch wrote to conversation history but never to
    proposal_messages — so a reply was streamed, stored where the UI does not
    read, and gone on reload. Global-assistant session 293 showed two user
    messages and no answer, while history proved one had been sent."""
    from app.models.proposal import ProposalMessage, ProposalSession

    session = ProposalSession(
        title="test", agent_type="general", mode="standalone", created_by=1,
    )
    db.add(session)
    db.commit()

    async def fake_run(db_, message, **kwargs):
        return general_result

    with patch.object(sh, "_fresh_db", return_value=db), \
         patch("app.services.langchain.graphs.agent_router_graph.classify_intent_node",
               new=AsyncMock(return_value=_fake_classification("general_query", []))), \
         patch("app.services.langchain.graphs.general_assistant_agent.run_general_assistant",
               new=fake_run):
        asyncio.run(_drain(sh.stream_router_response(
            db,
            message="what is the rate?",
            session_id="test-session",
            proposal_session_id=session.id,
            user_id=1,
        )))

    saved = (
        db.query(ProposalMessage)
        .filter(ProposalMessage.session_id == session.id,
                ProposalMessage.role == "assistant")
        .all()
    )
    assert len(saved) == 1, "the answer would vanish on reload"
    assert saved[0].content == general_result["output"]
    assert saved[0].metadata_json["sources"] == general_result["sources"]
