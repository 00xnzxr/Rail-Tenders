"""Tool resolution honesty: what the registry declares is what the agent gets,
and what the user assigns is never silently dropped.

Defect #1: deep_analyzer ran with 4 tools while canonical_registry declared 6 —
web_search and memory_store were declared and never passed, because the graph
carried its own hardcoded default_keys.

Defect #2: resolve_tool_keys intersected the user's Agent Builder assignment
with those hardcoded keys, so a tool assigned in the UI could vanish at runtime
with nothing but a log line.
"""

import app.models  # noqa: F401
import app.models.agent_memory  # noqa: F401
import pytest

from app.core.database import Base, engine


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


def _seed_agent_and_tools(db):
    """A tender_doc_analyzer row plus the agent_tools rows the tests reference."""
    from app.services.agent_tools_service import seed_system_tools
    from app.models.agent_builder import CustomAgent

    seed_system_tools(db)
    agent = (
        db.query(CustomAgent)
        .filter(CustomAgent.agent_key == "tender_doc_analyzer")
        .first()
    )
    if agent is None:
        agent = CustomAgent(
            agent_key="tender_doc_analyzer",
            display_name="Tender Doc Analyzer",
            system_prompt="test",
            is_system=True, is_enabled=True, is_published=True,
        )
        db.add(agent)
        db.commit()
    return agent


def test_deep_analyzer_runs_the_tools_the_registry_declares(db):
    from app.services.langchain.canonical_registry import (
        CANONICAL_AGENTS, resolve_tool_keys,
    )

    _seed_agent_and_tools(db)
    declared = set(CANONICAL_AGENTS["tender_doc_analyzer"]["default_tools"])
    resolved, _src = resolve_tool_keys(db, "tender_doc_analyzer")
    assert set(resolved) == declared


def test_graphs_no_longer_carry_their_own_tool_lists():
    """The graphs must ask the registry, not restate it. A literal default_keys
    at a call site is how declared-but-never-passed happened."""
    import inspect

    from app.services.langchain.graphs import (
        document_analysis_agent, proposal_agent,
    )

    for module in (document_analysis_agent, proposal_agent):
        source = inspect.getsource(module)
        assert "default_keys=[" not in source.replace(" ", ""), module.__name__


def test_an_assigned_tool_is_never_silently_dropped(db):
    from app.models.agent_builder import AgentTool
    from app.services.langchain.canonical_registry import resolve_tool_keys

    agent = _seed_agent_and_tools(db)
    row = db.query(AgentTool).filter(AgentTool.tool_key == "xlsx_generator").first()
    assert row is not None, "xlsx_generator has no agent_tools row"

    agent.is_user_customized = True
    agent.tools = [{"tool_id": row.id, "config": {}}]
    db.commit()

    resolved, source = resolve_tool_keys(db, "tender_doc_analyzer")
    assert "xlsx_generator" in resolved, "the assignment was dropped at runtime"
    assert source == "user_customized"

    # Clean up so other tests see the uncustomized default.
    agent.is_user_customized = False
    agent.tools = []
    db.commit()


def test_an_unknown_assigned_key_is_reported_not_dropped(db):
    from app.services.langchain.canonical_registry import resolve_unknown_tool_keys

    unknown = resolve_unknown_tool_keys(["web_search", "not_a_real_tool"])
    assert unknown == ["not_a_real_tool"]


# ── the Master Agent's catalog ─────────────────────────────────────────────


def _seed_workers(db):
    from app.models.agent_builder import CustomAgent

    for key in ("deep_analyzer", "checklist_generator", "proposal_creator",
                "costing_researcher", "annexure_finder", "workspace_manager"):
        if not db.query(CustomAgent).filter(CustomAgent.agent_key == key).first():
            db.add(CustomAgent(agent_key=key, display_name=key,
                               system_prompt="t", is_enabled=True))
    db.commit()


def test_the_master_loses_no_capability_it_had(db):
    """Parity by enumeration, not by review: the new catalog must be a superset
    of the old five-builder catalog unioned with the general assistant's belt."""
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_catalog,
    )
    from app.services.langchain.graphs.general_assistant_agent import (
        GENERAL_ASSISTANT_TOOL_KEYS,
    )

    _seed_workers(db)
    reachable = build_master_catalog(
        db, user_id=1, user_role="master_admin", context={}, reachable_only=True,
    )
    previously = {
        "inspect_workspace", "inspect_tender", "inspect_session",
        "list_recent_errors", "check_annexure_extraction",
        "init_workspace_force", "regenerate_checklist", "regenerate_annexures",
        "finalize_document", "retry_failed_agent_run",
        "run_with_llm", "web_search", "document_reader",
        "list_platform_settings", "get_platform_health", "list_recent_agent_runs",
        "list_platform_users", "update_platform_setting",
        "diagnose_tender_outputs",
    }
    missing = (previously | set(GENERAL_ASSISTANT_TOOL_KEYS)) - set(reachable)
    assert not missing, f"capability lost in the move: {sorted(missing)}"


def test_the_bound_catalog_stays_small(db):
    """Reach is total; bound schema is not. Large tool sets measurably degrade
    tool selection — the whole reason TOOL_FREE_AGENTS exists."""
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_catalog,
    )

    _seed_workers(db)
    bound = build_master_catalog(db, user_id=1, user_role="master_admin", context={})
    assert len(bound) <= 24, f"bound catalog is {len(bound)} tools"
    names = {t.name for t in bound}
    assert "use_capability" in names
    assert "call_costing_researcher" in names
    assert "tender_lookup" in names
    assert "cost_breakdown_read" in names


def test_the_master_prompt_carries_the_manual_and_doctrine():
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_prompt_preamble,
    )

    preamble = build_master_prompt_preamble("master_admin", workers=[
        {"agent_key": "costing_researcher", "display_name": "Costing Researcher",
         "description": "Produces cost breakdowns."},
    ])
    assert "Platform Capability Manual" in preamble
    assert "Delegation doctrine" in preamble
    assert "call_costing_researcher" in preamble
