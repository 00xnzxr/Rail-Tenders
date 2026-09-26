"""The agent layer does not carry one user's work to another.

Three paths leaked through the agents rather than through a route:

- Agent memory was one pool. Learning extraction, exemplar capture and the
  agent's own `memory_store` wrote user work with no owner, and every agent
  run retrieved the pool into its system prompt -- one user's rates and
  costing narratives reached the next user's answer.
- `inspect_session`, `list_recent_errors` and `retry_failed_agent_run`
  read (and re-ran) any user's runs by id.
- The conversation-history routes returned any transcript by session id.
"""

import json
import uuid

import pytest

from app.core.actor_context import Actor, actor_scope
from app.models.agent_memory import AgentMemory, AgentConversationHistory
from app.models.agent_run import AgentRun
from app.models.proposal import ProposalSession
from app.models.user import User
from app.services.langchain import memory_service as ms


@pytest.fixture
def people(db):
    """A master_admin, and two ordinary users."""
    tag = uuid.uuid4().hex[:8]
    admin = User(email=f"admin-{tag}@t", name="admin", hashed_password="x", role="master_admin", is_active=True)
    alice = User(email=f"alice-{tag}@t", name="alice", hashed_password="x", role="costing_research", is_active=True)
    bob = User(email=f"bob-{tag}@t", name="bob", hashed_password="x", role="costing_research", is_active=True)
    db.add_all([admin, alice, bob])
    db.commit()
    yield admin, alice, bob
    db.query(AgentMemory).filter(
        AgentMemory.created_by.in_([admin.id, alice.id, bob.id])
    ).delete(synchronize_session=False)
    for u in (admin, alice, bob):
        db.delete(u)
    db.commit()


def _contents(rows):
    return {r.content for r in rows}


# ── memory ───────────────────────────────────────────────────────────────────


def test_a_memory_is_owned_by_whoever_the_platform_acts_for(db, people):
    _admin, alice, _bob = people
    with actor_scope(user_id=alice.id, role=alice.role):
        mem = ms.store_memory(db, "costing_researcher", "fact", "alice rate 812 per kg zzq")
    assert mem.created_by == alice.id


def test_one_users_memory_never_reaches_another_users_prompt(db, people):
    _admin, alice, bob = people
    with actor_scope(user_id=alice.id, role=alice.role):
        ms.store_memory(db, "costing_researcher", "learning",
                        "alice margin twelve percent zzq", keywords=["zzq"],
                        context="Auto-extracted from interaction")
    with actor_scope(user_id=bob.id, role=bob.role):
        seen = ms.retrieve_memories(db, "costing_researcher", "margin zzq", min_score=0.0)
    assert "alice margin twelve percent zzq" not in _contents(seen)
    with actor_scope(user_id=alice.id, role=alice.role):
        own = ms.retrieve_memories(db, "costing_researcher", "margin zzq", min_score=0.0)
    assert "alice margin twelve percent zzq" in _contents(own)


def test_curated_admin_knowledge_is_shared(db, people):
    admin, _alice, bob = people
    ms.bulk_store_memories(db, [{"content": "company prefers ISO 9001 vendors zzq"}],
                           created_by=admin.id)
    with actor_scope(user_id=bob.id, role=bob.role):
        seen = ms.retrieve_memories(db, None, "vendors zzq", min_score=0.0)
    assert "company prefers ISO 9001 vendors zzq" in _contents(seen)


def test_an_admins_run_captures_stay_the_admins(db, people):
    """Captured from a run is not curated, whoever ran it."""
    admin, _alice, bob = people
    with actor_scope(user_id=admin.id, role=admin.role):
        ms.store_memory(db, None, "exemplar", "admin costing narrative zzq",
                        context="Auto-captured cost_breakdown output")
        ms.store_memory(db, None, "fact", "admin agent write zzq",
                        context="[agent] stored during a run")
    with actor_scope(user_id=bob.id, role=bob.role):
        seen = _contents(ms.retrieve_memories(db, None, "admin zzq", min_score=0.0))
    assert "admin costing narrative zzq" not in seen
    assert "admin agent write zzq" not in seen


def test_background_work_reads_curated_knowledge_only(db, people):
    admin, alice, _bob = people
    ms.bulk_store_memories(db, [{"content": "curated background fact zzq"}], created_by=admin.id)
    with actor_scope(user_id=alice.id, role=alice.role):
        ms.store_memory(db, None, "fact", "alice private fact zzq")
    seen = _contents(ms.retrieve_memories(db, None, "fact zzq", min_score=0.0))
    assert "curated background fact zzq" in seen
    assert "alice private fact zzq" not in seen


def test_an_unowned_legacy_row_stays_with_master_admin(db, people):
    admin, alice, _bob = people
    row = AgentMemory(agent_key=None, memory_type="fact", content="legacy pooled row zzq",
                      keywords=[], created_by=None)
    db.add(row)
    db.commit()
    try:
        with actor_scope(user_id=alice.id, role=alice.role):
            assert "legacy pooled row zzq" not in _contents(
                ms.retrieve_memories(db, None, "legacy zzq", min_score=0.0))
        with actor_scope(user_id=admin.id, role=admin.role):
            assert "legacy pooled row zzq" in _contents(
                ms.retrieve_memories(db, None, "legacy zzq", min_score=0.0))
    finally:
        db.delete(row)
        db.commit()


def test_a_user_cannot_delete_another_users_memory(db, people):
    _admin, alice, bob = people
    with actor_scope(user_id=alice.id, role=alice.role):
        mem = ms.store_memory(db, None, "fact", "alice keeps this zzq")
    assert ms.delete_memory(db, mem.id, actor=Actor(bob.id, bob.role)) is False
    assert ms.delete_memory(db, mem.id, actor=Actor(alice.id, alice.role)) is True


def test_memory_stats_count_only_what_the_caller_can_read(db, people):
    _admin, alice, bob = people
    with actor_scope(user_id=alice.id, role=alice.role):
        ms.store_memory(db, "zzq_agent", "fact", "alice stat row zzq")
    assert ms.get_memory_stats(db, actor=Actor(bob.id, bob.role))["by_agent"].get("zzq_agent") is None
    assert ms.get_memory_stats(db, actor=Actor(alice.id, alice.role))["by_agent"]["zzq_agent"] == 1


def test_the_memory_tool_marks_its_writes_as_captured(db, people):
    from app.services.langchain.tools.memory_tool import MemoryStoreTool

    _admin, alice, _bob = people
    with actor_scope(user_id=alice.id, role=alice.role):
        out = json.loads(MemoryStoreTool(db=db, agent_key="x")._run(content="tool write zzq"))
    row = db.query(AgentMemory).get(out["memory_id"])
    assert row.created_by == alice.id
    assert row.context.startswith("[agent] ")


# ── diagnostic and repair tools ──────────────────────────────────────────────


@pytest.fixture
def two_sessions(db, people):
    _admin, alice, bob = people
    s_alice = ProposalSession(title="a", created_by=alice.id)
    s_bob = ProposalSession(title="b", created_by=bob.id)
    db.add_all([s_alice, s_bob])
    db.commit()
    runs = [
        AgentRun(id=str(uuid.uuid4()), user_id=alice.id, proposal_session_id=s_alice.id,
                 status="failed", prompt="alice secret prompt", error_message="alice error"),
        AgentRun(id=str(uuid.uuid4()), user_id=bob.id, proposal_session_id=s_bob.id,
                 status="failed", prompt="bob prompt", error_message="bob error"),
    ]
    db.add_all(runs)
    db.commit()
    yield s_alice, s_bob, runs
    for r in runs:
        db.delete(r)
    db.delete(s_alice)
    db.delete(s_bob)
    db.commit()


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_inspect_session_does_not_open_another_users_session(db, people, two_sessions):
    from app.services.langchain.graphs.orchestrator_tools import build_diagnostic_tools

    _admin, _alice, bob = people
    s_alice, _s_bob, _runs = two_sessions
    with actor_scope(user_id=bob.id, role=bob.role):
        out = _tool(build_diagnostic_tools(bob.id), "inspect_session")._run(session_id=s_alice.id)
    assert "session_not_found" in out
    assert "alice error" not in out


def test_list_recent_errors_lists_only_the_callers_runs(db, people, two_sessions):
    from app.services.langchain.graphs.orchestrator_tools import build_diagnostic_tools

    _admin, _alice, bob = people
    with actor_scope(user_id=bob.id, role=bob.role):
        out = _tool(build_diagnostic_tools(bob.id), "list_recent_errors")._run()
    assert "bob error" in out
    assert "alice error" not in out


def test_retry_does_not_rerun_another_users_prompt(db, people, two_sessions, monkeypatch):
    from app.services.langchain.graphs import agent_router_graph
    from app.services.langchain.graphs.orchestrator_tools import build_action_tools

    _admin, _alice, bob = people
    _s_alice, _s_bob, runs = two_sessions
    called = []

    async def _never(**kw):
        called.append(kw)
        return {"output": "leaked"}

    monkeypatch.setattr(agent_router_graph, "route_and_execute", _never)
    with actor_scope(user_id=bob.id, role=bob.role):
        out = _tool(build_action_tools(bob.id), "retry_failed_agent_run")._run(
            agent_run_id=runs[0].id)
    assert "not found" in out
    assert called == []


# ── conversation history routes ──────────────────────────────────────────────


def test_history_routes_do_not_serve_another_users_transcript(db, intruder_client):
    """intruder_client is user 2, a costing_research user."""
    sid = f"conv-{uuid.uuid4().hex}"
    sess = ProposalSession(title="owned", created_by=1, router_session_id=sid)
    db.add(sess)
    db.add(AgentConversationHistory(session_id=sid, agent_key="x", role="user",
                                    content="owner's private question"))
    db.commit()
    try:
        for url in (f"/api/langchain/proposal-chat/{sid}/history",
                    f"/api/langchain/agents/x/chat/{sid}/history"):
            r = intruder_client.get(url)
            assert r.status_code == 404, (url, r.status_code)
            assert "owner's private question" not in r.text
    finally:
        db.query(AgentConversationHistory).filter(
            AgentConversationHistory.session_id == sid).delete()
        db.delete(sess)
        db.commit()


def test_the_owner_still_reads_their_transcript(db, auth_client):
    sid = f"conv-{uuid.uuid4().hex}"
    sess = ProposalSession(title="owned", created_by=1, router_session_id=sid)
    db.add(sess)
    db.add(AgentConversationHistory(session_id=sid, agent_key="x", role="user", content="mine"))
    db.commit()
    try:
        r = auth_client.get(f"/api/langchain/proposal-chat/{sid}/history")
        assert r.status_code == 200 and "mine" in r.text
    finally:
        db.query(AgentConversationHistory).filter(
            AgentConversationHistory.session_id == sid).delete()
        db.delete(sess)
        db.commit()
