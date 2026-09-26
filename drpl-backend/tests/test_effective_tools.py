"""What an agent actually runs with, versus what the Tools tab used to show.

The `tools` column is only one of four sources. The Master Agent builds its
catalog per run and stores nothing; two chat wrappers hardcode a fixed set;
canonical agents fall back to registry defaults when the column is empty. The
UI showed only the stored list, so the Master Agent displayed zero tools while
running with more than thirty, and doc-costing-analyst displayed fifteen that
were never bound.
"""

import pytest

from app.api.routes.agent_builder import (
    MASTER_AGENT_KEY,
    _WRAPPER_TOOL_KEYS,
    get_effective_tools,
)
from app.models.agent_builder import CustomAgent
from app.models.user import User  # noqa: F401 — registers the table


@pytest.fixture
def master_user(db):
    import uuid as _uuid

    u = User(
        email=f"eff-{_uuid.uuid4().hex[:8]}@example.com",
        name="Effective Tools Test",
        hashed_password="x",
        role="master_admin",
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    yield u
    db.query(User).filter(User.id == u.id).delete()
    db.commit()


@pytest.fixture
def agent(db):
    made = []

    def make(agent_key, tools=None, **kw):
        row = CustomAgent(
            agent_key=agent_key,
            display_name=agent_key,
            tools=tools or [],
            is_enabled=True,
            **kw,
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


def _call(db, row, user):
    return get_effective_tools(agent_id=row.id, db=db, current_user=user)


# ── the master agent ────────────────────────────────────────────────────────


def test_master_reports_its_runtime_catalog(db, agent, master_user):
    """It stores no tools but runs with the full catalog. Reporting the stored
    zero was the misleading part."""
    existing = db.query(CustomAgent).filter(
        CustomAgent.agent_key == MASTER_AGENT_KEY
    ).first()
    row = existing or agent(MASTER_AGENT_KEY)
    # The delegation roster is registry-driven, so it is empty unless a worker
    # is enabled — which is the correct behaviour, not a fixture convenience.
    agent("eff_test_roster_worker")

    result = _call(db, row, master_user)

    assert result["source"] == "runtime"
    names = {t["name"] for t in result["tools"]}
    assert len(names) > 10
    # Delegation, diagnostics, repair, and the platform tools its role allows.
    assert any(n.startswith("call_") for n in names)
    assert "inspect_tender" in names
    assert "regenerate_checklist" in names
    assert "diagnose_tender_outputs" in names


def test_master_tools_carry_their_gate_tier(db, agent, master_user):
    """The UI shows which of these will ask for confirmation."""
    existing = db.query(CustomAgent).filter(
        CustomAgent.agent_key == MASTER_AGENT_KEY
    ).first()
    row = existing or agent(MASTER_AGENT_KEY)

    by_name = {t["name"]: t["tier"] for t in _call(db, row, master_user)["tools"]}

    assert by_name["inspect_tender"] == "read"
    assert by_name["regenerate_checklist"] == "write"
    assert by_name["finalize_document"] == "destructive"


def test_master_role_bounds_what_is_reported(db, agent, master_user):
    """An operator does not get the platform tools, so they must not be listed
    as part of what the agent will run with for that user."""
    master_user.role = "operator"
    db.add(master_user)
    db.commit()

    existing = db.query(CustomAgent).filter(
        CustomAgent.agent_key == MASTER_AGENT_KEY
    ).first()
    row = existing or agent(MASTER_AGENT_KEY)

    names = {t["name"] for t in _call(db, row, master_user)["tools"]}
    assert "update_platform_setting" not in names
    assert "list_platform_settings" not in names


# ── wrapper-driven agents ───────────────────────────────────────────────────


def test_wrapper_agent_reports_its_fixed_set(db, agent, master_user):
    """proposal_creator ran with a hardcoded five-tool list while the UI showed
    whatever happened to be stored."""
    existing = db.query(CustomAgent).filter(
        CustomAgent.agent_key == "proposal_creator"
    ).first()
    row = existing or agent("proposal_creator")

    result = _call(db, row, master_user)

    assert result["source"] == "wrapper"
    assert {t["name"] for t in result["tools"]} == set(
        _WRAPPER_TOOL_KEYS["proposal_creator"]
    )
    assert "not used on that path" in result["note"]


# ── ordinary agents ─────────────────────────────────────────────────────────


def test_unconfigured_agent_reports_the_shared_repo(db, agent, master_user):
    """An empty assignment means the whole repo now, not nothing. Reporting
    "no tools" for it was the display half of the bug that left
    costing_researcher bound to nothing."""
    row = agent("eff_test_no_tools")
    result = _call(db, row, master_user)

    assert result["source"] == "shared-repo"
    assert len(result["tools"]) > 20
    assert result["stored_tool_count"] == 0


def test_tool_free_agent_is_reported_as_deliberate(db, agent, master_user):
    """Zero tools on a scoring agent is a choice, and the UI should say which
    kind of zero it is."""
    existing = db.query(CustomAgent).filter(
        CustomAgent.agent_key == "relevance"
    ).first()
    row = existing or agent("relevance")

    result = _call(db, row, master_user)

    assert result["source"] == "tool-free"
    assert result["tools"] == []


def test_stored_tool_count_is_always_reported(db, agent, master_user):
    """So the UI can show the gap between configured and effective."""
    row = agent("eff_test_counts")
    assert _call(db, row, master_user)["stored_tool_count"] == 0


def test_missing_agent_is_a_404(db, master_user):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        get_effective_tools(agent_id=99999999, db=db, current_user=master_user)
    assert exc.value.status_code == 404


# ── the orchestrator execution-mode bug ─────────────────────────────────────


def test_orchestrator_with_tools_is_upgraded_to_react():
    """`orchestrator` had no execution branch — it fell through to the simple
    single-call path, so doc-costing-analyst's 15 configured tools were never
    bound. Any agent with tools must be able to use them."""
    import inspect

    from app.services import agent_execution_service

    source = inspect.getsource(agent_execution_service.execute_agent)
    assert '("chain_of_thought", "orchestrator")' in source, (
        "orchestrator must be auto-upgraded to react when tools are configured"
    )


def test_endpoint_agrees_with_the_loader(db, agent, master_user):
    """The whole point of this endpoint is that it reports what actually runs.
    It briefly reported canonical defaults for an unconfigured agent while the
    loader was giving it the shared repo — the same class of disagreement it
    exists to expose."""
    from app.services.langchain.tools.tool_loader import resolve_agent_tool_keys

    row = agent("eff_test_agreement")
    reported = {t["name"] for t in _call(db, row, master_user)["tools"]}
    resolved = set(resolve_agent_tool_keys("eff_test_agreement", []) or [])

    assert reported == resolved
