"""The Master Agent's worker roster comes from the admin agent registry.

Before this, the roster was a hardcoded list of six. An agent added in Admin →
Agent Builder was invisible to the Master Agent, and an agent disabled there
was still callable — the admin UI and the Master Agent's actual capabilities
had no relationship to each other. These tests pin that relationship.
"""

import re

import pytest

from app.models.agent_builder import CustomAgent
from app.services.langchain.graphs.orchestrator_tools import (
    build_agent_wrapper_tools,
    list_worker_agents,
)
from app.services.langchain.tool_policy import READ, classify_tool


@pytest.fixture
def agents(db):
    """A master, an enabled worker, a disabled worker, and a hyphenated key."""
    rows = [
        CustomAgent(agent_key="decision_maker", display_name="Master Agent",
                    description="The master", is_enabled=True, is_system=True),
        CustomAgent(agent_key="roster_test_worker", display_name="Roster Worker",
                    description="Does roster-test things.", is_enabled=True),
        CustomAgent(agent_key="roster_test_disabled", display_name="Disabled Worker",
                    description="Should never be callable.", is_enabled=False),
        CustomAgent(agent_key="roster-test-hyphen", display_name="Hyphen Worker",
                    description="Has a hyphenated key.", is_enabled=True),
        CustomAgent(agent_key="proposal_router", display_name="Router",
                    description="Routing infrastructure.", is_enabled=True),
    ]
    for r in rows:
        db.add(r)
    db.commit()
    yield rows
    for r in rows:
        db.query(CustomAgent).filter(CustomAgent.agent_key == r.agent_key).delete()
    db.commit()


def _keys(db):
    return {w["agent_key"] for w in list_worker_agents(db)}


def test_enabled_agent_is_callable(db, agents):
    assert "roster_test_worker" in _keys(db)


def test_disabled_agent_is_not_callable(db, agents):
    """Disabling in the admin section is the control on what the Master Agent
    can reach. If this leaks, that switch means nothing."""
    assert "roster_test_disabled" not in _keys(db)


def test_master_cannot_call_itself(db, agents):
    """Unbounded recursion, with a per-call price tag."""
    assert "decision_maker" not in _keys(db)


def test_routing_infrastructure_is_not_a_worker(db, agents):
    assert "proposal_router" not in _keys(db)


def test_tool_names_are_provider_safe(db, agents):
    """Agent keys are slugs and may contain hyphens; tool names have a
    character set the provider enforces."""
    tools = build_agent_wrapper_tools(None, None, user_id=1, db=db)
    for t in tools:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t.name), t.name
    assert "call_roster_test_hyphen" in {t.name for t in tools}


def test_every_worker_tool_has_a_description(db, agents):
    """The model picks tools by description; an empty one means that worker
    effectively does not exist."""
    for t in build_agent_wrapper_tools(None, None, user_id=1, db=db):
        assert (t.description or "").strip(), t.name


def test_delegation_tools_are_not_gated(db, agents):
    """Handing work to a worker is not itself a write — the worker's own tools
    are gated where they write. Gating the hand-off stopped the Master Agent
    orchestrating at all."""
    for t in build_agent_wrapper_tools(None, None, user_id=1, db=db):
        assert classify_tool(t.name) == READ, t.name


def test_agent_without_a_builtin_handler_is_still_callable(db, agents):
    """An admin-created agent has no purpose-built chat wrapper; it must route
    through the generic executor rather than be dropped."""
    workers = {w["agent_key"]: w for w in list_worker_agents(db)}
    assert workers["roster_test_worker"]["handler"] is None
    assert "call_roster_test_worker" in {
        t.name for t in build_agent_wrapper_tools(None, None, user_id=1, db=db)
    }


def test_builtin_agents_keep_their_chat_handler(db, agents):
    """Built-ins must not silently fall back to the generic path — that would
    lose their workspace/artifact integration."""
    row = CustomAgent(agent_key="costing_researcher", display_name="Costing",
                      description="Costing.", is_enabled=True)
    existing = db.query(CustomAgent).filter(
        CustomAgent.agent_key == "costing_researcher"
    ).first()
    created = False
    if not existing:
        db.add(row)
        db.commit()
        created = True
    try:
        workers = {w["agent_key"]: w for w in list_worker_agents(db)}
        assert workers["costing_researcher"]["handler"] == "chat_costing_research"
    finally:
        if created:
            db.query(CustomAgent).filter(
                CustomAgent.agent_key == "costing_researcher"
            ).delete()
            db.commit()


# ── the prompt and the tools must agree ─────────────────────────────────────


def test_prompt_roster_matches_the_callable_tools(db, agents):
    """Planning mode used to run its own, narrower query than the tool builder,
    so a plan could name an agent that was not actually callable."""
    from app.services.langchain.graphs.decision_maker_agent import (
        _worker_roster_section,
    )

    section = _worker_roster_section(db)
    for t in build_agent_wrapper_tools(None, None, user_id=1, db=db):
        assert f"`{t.name}`" in section, f"{t.name} is callable but not in the prompt"


def test_roster_section_handles_an_empty_registry(db, monkeypatch):
    """A master agent with no workers must say so, not emit a blank list."""
    from app.services.langchain.graphs import decision_maker_agent as dm

    monkeypatch.setattr(
        "app.services.langchain.graphs.orchestrator_tools.list_worker_agents",
        lambda _db: [],
    )
    section = dm._worker_roster_section(db)
    assert "No worker agents" in section


# ── master-directed tool injection ──────────────────────────────────────────


def test_delegation_tool_exposes_extra_tools(db, agents):
    """The master can grant a worker capability for a single call. Its docstring
    promised 'dynamic tool injection' for months with nothing behind it."""
    tool = build_agent_wrapper_tools(None, None, user_id=1, db=db)[0]
    fields = tool.args_schema.model_fields
    assert "extra_tools" in fields
    assert "message" in fields


def test_unknown_granted_tools_are_dropped_not_passed_on(db):
    """An LLM inventing a tool name must not reach the loader as a key that
    quietly resolves to nothing."""
    from app.services.langchain.graphs.orchestrator_tools import _validate_extra_tools

    granted = _validate_extra_tools("x", ["web_search", "not_a_real_tool"])
    assert granted == ["web_search"]


def test_no_grant_is_an_empty_list(db):
    from app.services.langchain.graphs.orchestrator_tools import _validate_extra_tools

    assert _validate_extra_tools("x", None) == []
    assert _validate_extra_tools("x", []) == []


def test_granted_tools_are_still_gated(db):
    """A grant widens what a worker can reach; it does not bypass the write
    confirmation."""
    from app.services.langchain.graphs.orchestrator_tools import _validate_extra_tools
    from app.services.langchain.tool_policy import classify_tool

    granted = _validate_extra_tools("x", ["workspace_generate_document"])
    assert granted == ["workspace_generate_document"]
    assert classify_tool(granted[0]) == "write"


# ── pipeline-only system rows are not chat workers ─────────────────────────


@pytest.mark.parametrize("key", [
    "classifier", "relevance", "risk", "summary", "eligibility",
    "costing_scope_extractor", "tender_pipeline", "local_model_probe",
    "doc-letter-writer", "doc-technical-writer", "doc-costing-analyst",
    "doc-compliance-writer",
])
def test_pipeline_only_rows_are_not_delegatable(db, key):
    """Every enabled row used to become a call_* tool, so the Master carried
    the scoring stubs, a pipeline stage, the RunPod probe and the workspace
    writers on every step -- schema tokens paid per step, and two of them
    (the probe, doc-costing-analyst) were wrong places to send real work."""
    created = False
    if not db.query(CustomAgent).filter(CustomAgent.agent_key == key).first():
        db.add(CustomAgent(agent_key=key, display_name=key,
                           description="x", is_enabled=True))
        db.commit()
        created = True
    try:
        assert key not in _keys(db)
        names = {t.name for t in build_agent_wrapper_tools(None, None, user_id=1, db=db)}
        assert f"call_{key.replace('-', '_')}" not in names
        assert f"call_{key}" not in names
    finally:
        if created:
            db.query(CustomAgent).filter(CustomAgent.agent_key == key).delete()
            db.commit()
