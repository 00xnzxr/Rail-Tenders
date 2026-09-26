"""Three layers have to agree about what tools exist.

1. `tool_loader._TOOL_CLASS_REGISTRY` — what can actually run.
2. `agent_tools_service.SYSTEM_TOOLS` -> the `agent_tools` table — what the
   Agent Builder offers.
3. `CustomAgent.tools` — what a given agent is assigned.

They had drifted at every join. Five implemented tools had no `agent_tools`
row, so they could not be assigned from the UI and the code implementing them
was unreachable — four of those were costing_researcher's core research tools.
And costing_researcher's own assignment was empty, which makes
`load_tools_for_agent` return `[]`: a research agent bound to no tools at all,
displayed in the UI as an honest "Assigned Tools (0)".
"""

import pytest

from app.models.agent_builder import AgentTool, CustomAgent
from app.services.agent_tools_service import SYSTEM_TOOLS
from app.services.langchain.canonical_registry import CANONICAL_AGENTS
from app.services.langchain.tools.tool_loader import get_available_tool_keys
from app.services.seed_agent_tools import seed_agent_tools


# ── layer 1 vs layer 2 ──────────────────────────────────────────────────────


def test_every_implemented_tool_is_offered_in_the_builder():
    """A tool the code can run but the UI cannot offer is dead code that looks
    alive."""
    declared = {t["tool_key"] for t in SYSTEM_TOOLS}
    undeclared = sorted(set(get_available_tool_keys()) - declared)
    assert not undeclared, (
        f"Implemented but not registered in SYSTEM_TOOLS: {undeclared}. "
        "They cannot be assigned to any agent."
    )


def test_system_tool_entries_are_well_formed():
    for tool in SYSTEM_TOOLS:
        assert tool.get("tool_key"), tool
        assert tool.get("display_name"), tool["tool_key"]
        # The picker shows the description; an empty one is an unusable row.
        assert (tool.get("description") or "").strip(), tool["tool_key"]


def test_system_tool_keys_are_unique():
    keys = [t["tool_key"] for t in SYSTEM_TOOLS]
    assert len(keys) == len(set(keys)), "duplicate tool_key in SYSTEM_TOOLS"


# ── layer 2 vs layer 3 ──────────────────────────────────────────────────────


def test_canonical_defaults_only_name_registerable_tools():
    """A canonical default naming a tool with no row silently drops on
    backfill, leaving the agent short of what its registry promises."""
    declared = {t["tool_key"] for t in SYSTEM_TOOLS}
    problems = []
    for agent_key, spec in CANONICAL_AGENTS.items():
        for key in spec.get("default_tools") or []:
            if key not in declared:
                problems.append(f"{agent_key}:{key}")
    assert not problems, f"canonical defaults naming unregistered tools: {problems}"


# ── the backfill ────────────────────────────────────────────────────────────


@pytest.fixture
def registered_tools(db):
    """A minimal agent_tools table for the keys the backfill needs."""
    made = []
    for key in ("memory_store", "memory_retrieve", "cost_calculator"):
        if db.query(AgentTool).filter(AgentTool.tool_key == key).first():
            continue
        row = AgentTool(
            tool_key=key, display_name=key, description=key, tool_type="db_query"
        )
        db.add(row)
        made.append(key)
    db.commit()
    yield
    for key in made:
        db.query(AgentTool).filter(AgentTool.tool_key == key).delete()
    db.commit()


@pytest.fixture
def agent(db):
    made = []

    def make(agent_key, tools=None):
        row = CustomAgent(
            agent_key=agent_key, display_name=agent_key,
            tools=tools if tools is not None else [], is_enabled=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        made.append(row.id)
        return row

    yield make
    for rid in made:
        db.query(CustomAgent).filter(CustomAgent.id == rid).delete()
    db.commit()


def _tools_of(db, key):
    db.expire_all()
    return db.query(CustomAgent).filter(CustomAgent.agent_key == key).first().tools


def test_empty_assignment_means_the_whole_repo(db):
    """The inverted default. An empty column used to mean "no tools", which is
    how costing_researcher ended up bound to nothing while its registry
    declared eight. It now means the agent reaches everything."""
    from app.services.langchain.tools.tool_loader import (
        get_available_tool_keys,
        resolve_agent_tool_keys,
    )

    resolved = resolve_agent_tool_keys("costing_researcher", [])
    assert resolved == get_available_tool_keys()
    assert len(resolved) > 20


def test_scoring_agents_stay_tool_free(db):
    """Auto-scoring makes 750 calls an hour and the catalog is ~7,900 tokens of
    schema. Binding it to agents that score a tender 0-1 is pure overhead, and
    a large tool set degrades selection for the agents that do choose."""
    from app.services.langchain.tools.tool_loader import resolve_agent_tool_keys

    for key in ("relevance", "risk", "classifier", "summary", "eligibility"):
        assert resolve_agent_tool_keys(key, []) == [], key


def test_explicit_assignment_still_wins(db):
    """Narrowing an agent on purpose has to keep working, or the shared repo is
    a rule with no exceptions rather than a default with one."""
    from app.services.langchain.tools.tool_loader import resolve_agent_tool_keys

    assert resolve_agent_tool_keys("costing_researcher", [{"tool_id": 1}]) is None


def test_release_frees_a_canonical_assignment(db, agent, registered_tools):
    """The previous backfill copied canonical defaults into the column. Against
    the inverted default those lists cap the agent, so they are cleared."""
    from app.models.agent_builder import AgentTool as _AT

    ids = {
        r.tool_key: r.id
        for r in db.query(_AT.id, _AT.tool_key).all()
    }
    canonical = CANONICAL_AGENTS["proposal_creator"]["default_tools"]
    if not all(k in ids for k in canonical):
        pytest.skip("canonical tools not registered in this database")

    existing = db.query(CustomAgent).filter(
        CustomAgent.agent_key == "proposal_creator"
    ).first()
    row = existing or agent("proposal_creator")
    row.tools = [{"tool_id": ids[k], "config": {}} for k in canonical]
    db.add(row)
    db.commit()

    seed_agent_tools(db)

    db.expire_all()
    freed = db.query(CustomAgent).filter(
        CustomAgent.agent_key == "proposal_creator"
    ).first()
    assert freed.tools == []


def test_release_never_touches_a_human_assignment(db, agent, registered_tools):
    """An assignment a person chose differs from the canonical list."""
    tool = db.query(AgentTool).first()
    row = agent("release_test_custom", tools=[{"tool_id": tool.id, "config": {}}])

    seed_agent_tools(db)

    db.expire_all()
    kept = db.query(CustomAgent).filter(
        CustomAgent.agent_key == "release_test_custom"
    ).first()
    assert len(kept.tools) == 1


def test_release_can_be_disabled(db, monkeypatch):
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("AGENT_TOOL_RELEASE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert seed_agent_tools(db).get("skipped") is True
    finally:
        get_settings.cache_clear()
