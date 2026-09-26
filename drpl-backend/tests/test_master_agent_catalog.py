"""The Master Agent's reach: reading back costings, and a roster without
duplicate specialists.

Two measured gaps. Nothing on the platform could read a saved cost breakdown —
cost_calculator computes and xlsx_generator exports, so "why did this costing
come out this way" could only ever be answered rhetorically. And the worker
roster exposed 22 call_* tools including four aliases for one analyzer;
duplicate tools with near-identical descriptions are how a model picks the
wrong one.
"""

import app.models  # noqa: F401
import app.models.agent_memory  # noqa: F401
import pytest

from app.core.database import Base, engine


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


def test_a_stored_costing_can_be_read_back(db):
    from app.core.actor_context import actor_scope

    from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
    from app.services.langchain.tools.cost_breakdown_tool import CostBreakdownReadTool

    cb = CostBreakdown(
        # created_by is the ownership column the firewall scopes on
        # (app/core/ownership.py); the read below runs under a matching actor
        # scope, which the Command Center opens for every real run.
        tender_id=4242, version=1, status="draft", created_by=4242001,
        overhead_percent=10.0, margin_percent=15.0, gst_percent=18.0,
        subtotal=1000.0, grand_total=1298.0,
    )
    db.add(cb)
    db.flush()
    db.add(CostBreakdownLine(
        cost_breakdown_id=cb.id, sr_no=1, description="SS 304 trough",
        quantity=2, unit="no", rate=500.0, amount=1000.0,
    ))
    db.commit()

    with actor_scope(user_id=4242001, role="costing_research"):
        out = CostBreakdownReadTool(db=db)._run(tender_id=4242)
    assert "SS 304 trough" in out
    assert "1298" in out.replace(",", "")


def test_reading_a_missing_costing_reports_not_invents(db):
    from app.services.langchain.tools.cost_breakdown_tool import CostBreakdownReadTool

    out = CostBreakdownReadTool(db=db)._run(tender_id=999999)
    assert "no cost breakdown" in out.lower()


def test_cost_breakdown_read_is_registered_and_read_tier():
    from app.services.langchain.capability_registry import get
    from app.services.langchain.tool_policy import READ, classify_tool

    cap = get("cost_breakdown_read")
    assert cap is not None, "cost_breakdown_read is not in the capability registry"
    assert cap.domain == "costing"
    assert classify_tool("cost_breakdown_read") == READ


def test_the_worker_roster_has_no_duplicate_specialists(db):
    from app.models.agent_builder import CustomAgent
    from app.services.langchain.graphs.orchestrator_tools import list_worker_agents

    # Seed the alias rows the production database actually has, so this test
    # cannot pass vacuously on an empty test DB.
    for key in ("deep_analyzer", "tender_doc_analyzer", "document_analyzer",
                "tender_analysis", "checklist", "checklist_generator",
                "proposal", "proposal_creator", "costing_researcher"):
        if not db.query(CustomAgent).filter(CustomAgent.agent_key == key).first():
            db.add(CustomAgent(agent_key=key, display_name=key,
                               system_prompt="t", is_enabled=True))
    db.commit()

    keys = [w["agent_key"] for w in list_worker_agents(db)]
    assert len(keys) == len(set(keys))
    analyzers = {"deep_analyzer", "tender_doc_analyzer",
                 "document_analyzer", "tender_analysis"} & set(keys)
    assert len(analyzers) <= 1, f"still exposing aliases: {analyzers}"
    checklists = {"checklist", "checklist_generator"} & set(keys)
    assert len(checklists) <= 1, f"still exposing aliases: {checklists}"
    proposals = {"proposal", "proposal_creator"} & set(keys)
    assert len(proposals) <= 1, f"still exposing aliases: {proposals}"


# ── the use_capability dispatcher ──────────────────────────────────────────


def _dispatcher(db, role="master_admin"):
    from app.services.langchain.graphs.capability_dispatcher import (
        build_capability_dispatcher,
    )

    return build_capability_dispatcher(
        db, user_id=1, user_role=role, surface="master", context={},
    )


def test_the_dispatcher_lists_tier_two_capabilities_in_its_description(db):
    tool = _dispatcher(db)
    assert tool.name == "use_capability"
    assert "ratecard_lookup" in tool.description
    assert "platform_ops" in tool.description


def test_an_unknown_key_returns_guidance_not_a_crash(db):
    out = _dispatcher(db)._run(key="nope", args={})
    assert "nope" in out and "available" in out.lower()


def test_bad_arguments_return_the_expected_shape(db):
    out = _dispatcher(db)._run(key="ratecard_lookup", args={"wrong_field": 1})
    assert "ratecard_lookup" in out
    assert "expects" in out.lower()


def test_the_dispatcher_is_not_a_way_around_the_role_check(db):
    tool = _dispatcher(db, role="admin")
    assert "update_platform_setting" not in tool.description
    out = tool._run(key="update_platform_setting", args={"key": "x", "value": "y"})
    assert "administrator" in out.lower()


def test_the_dispatcher_is_not_a_way_around_the_write_gate(db):
    """A gated write called through the dispatcher must still suspend."""
    from app.services.langchain.tool_policy import PendingActionCapture, policy_scope

    tool = _dispatcher(db)
    capture = PendingActionCapture()
    with policy_scope(capture=capture):
        tool._run(key="finalize_document", args={"tender_id": 1, "item_id": 1})
    assert capture.is_pending(), "the write ran without the gate seeing it"
