"""The chat entrypoint: Master Agent by default, router one setting away.

The Master Agent had produced three conversation turns in its entire history
because the router escalated to it only under rules E1-E5 — ordinary messages
never qualified. chat_engine=master makes it the entrypoint; chat_engine=router
restores the old path exactly, live from PlatformSetting, because putting Opus
on every message must be revertible without a deploy.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import app.models  # noqa: F401
import app.models.agent_memory  # noqa: F401
import pytest

from app.core.database import Base, engine
from app.services.langchain import streaming_handler as sh


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


async def _drain(agen):
    return [e async for e in agen]


def _master_result():
    return {
        "output": "ok", "output_type": "decision_maker_trace",
        "agent_key": "decision_maker", "tool_calls": [], "trace": [],
        "metrics": {}, "status": "completed",
    }


def _patch_engine(value):
    """chat_engine is read through get_effective_setting inside the handler."""
    real = sh.get_effective_setting

    def fake(db, key, default=None):
        if key == "chat_engine":
            return value
        return real(db, key, default)

    return patch.object(sh, "get_effective_setting", side_effect=fake)


def test_chat_engine_master_reaches_the_master_agent(db):
    called = {}

    async def fake_master(*args, **kwargs):
        called["kwargs"] = kwargs
        return _master_result()

    with patch.object(sh, "_fresh_db", return_value=db), \
         _patch_engine("master"), \
         patch("app.services.langchain.graphs.decision_maker_agent.run_decision_maker",
               new=fake_master):
        asyncio.run(_drain(sh.stream_router_response(
            db, message="hello", session_id="s-master", tender_id=88, user_id=1)))

    assert called, "chat did not reach the Master Agent"
    assert called["kwargs"].get("tender_id") == 88


def test_chat_engine_router_restores_the_old_path_exactly(db):
    classified = {}

    async def fake_classify(state, db_):
        classified["yes"] = True
        return {"intent": "general_query", "selected_agents": [], "metadata": {}}

    with patch.object(sh, "_fresh_db", return_value=db), \
         _patch_engine("router"), \
         patch("app.services.langchain.graphs.agent_router_graph.classify_intent_node",
               new=fake_classify), \
         patch("app.services.langchain.graphs.general_assistant_agent.run_general_assistant",
               new=AsyncMock(return_value={
                   "output": "ok", "output_type": "general",
                   "agent_key": "general_assistant", "tool_calls": [],
                   "sources": [], "status": "completed",
               })):
        asyncio.run(_drain(sh.stream_router_response(
            db, message="hello", session_id="s-router", user_id=1)))

    assert classified.get("yes"), "the router kill switch did not restore the old path"


def test_master_path_does_not_consult_the_classifier(db):
    """The classifier stops being a gatekeeper: what you can ask no longer
    depends on a Haiku call guessing right."""
    classified = {}

    async def fake_classify(state, db_):
        classified["yes"] = True
        return {"intent": "general_query", "selected_agents": [], "metadata": {}}

    async def fake_master(*args, **kwargs):
        return _master_result()

    with patch.object(sh, "_fresh_db", return_value=db), \
         _patch_engine("master"), \
         patch("app.services.langchain.graphs.agent_router_graph.classify_intent_node",
               new=fake_classify), \
         patch("app.services.langchain.graphs.decision_maker_agent.run_decision_maker",
               new=fake_master):
        asyncio.run(_drain(sh.stream_router_response(
            db, message="hello", session_id="s-noclass", user_id=1)))

    assert not classified, "the master path still routes through the classifier"


def test_general_assistant_is_a_callable_worker_but_not_its_own(db):
    """Under the Master, the generalist becomes the cheap delegate for
    ordinary conversational work — while keeping the self-call guard."""
    from app.models.agent_builder import CustomAgent
    from app.services.langchain.graphs.orchestrator_tools import list_worker_agents

    for key in ("general_assistant", "costing_researcher"):
        if not db.query(CustomAgent).filter(CustomAgent.agent_key == key).first():
            db.add(CustomAgent(agent_key=key, display_name=key,
                               system_prompt="t", is_enabled=True))
    db.commit()

    keys = {w["agent_key"] for w in list_worker_agents(db)}
    assert "general_assistant" in keys

    from app.services.langchain.graphs.general_assistant_agent import _build_tools

    own = {t.name for t in _build_tools(
        db, proposal_session_id=None, session_id="t", user_id=None,
        conversation_history=None, file_metadata=None, stream_callback=None,
    )}
    assert "call_general_assistant" not in own, "the generalist can call itself"
