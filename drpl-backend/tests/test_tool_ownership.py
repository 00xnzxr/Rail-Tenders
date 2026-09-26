"""The firewall's sharpest edge is not HTTP.

Agent tools query with a raw `db` session and no notion of who is asking. A
costing researcher can type "show me the cost breakdown for tender 303" into
the Command Center and a tool will answer — through the front door, with no
route involved. Scoping that lives only in the API layer has an agent-shaped
hole in it.
"""

import pytest

from app.core.actor_context import actor_scope
from app.models.cost_breakdown import CostBreakdown
from app.services.langchain.tools.cost_breakdown_tool import CostBreakdownReadTool

TENDER = 4242


def _breakdown(db, owner):
    cb = CostBreakdown(tender_id=TENDER, created_by=owner, version=1, status="draft")
    db.add(cb)
    db.commit()
    return cb


def test_owner_reads_their_own_breakdown(db):
    _breakdown(db, owner=501)
    tool = CostBreakdownReadTool(db=db)
    with actor_scope(user_id=501, role="costing_research"):
        out = tool._run(tender_id=TENDER)
    assert "Cost Breakdown" in out


def test_another_user_cannot_read_it_through_the_agent(db):
    """The leak this test exists to prevent: same tender, different person."""
    _breakdown(db, owner=502)
    tool = CostBreakdownReadTool(db=db)
    with actor_scope(user_id=503, role="costing_research"):
        out = tool._run(tender_id=TENDER)
    assert "No cost breakdown" in out
    assert "502" not in out


def test_master_admin_reads_anyones(db):
    _breakdown(db, owner=504)
    tool = CostBreakdownReadTool(db=db)
    with actor_scope(user_id=1, role="master_admin"):
        out = tool._run(tender_id=TENDER)
    assert "Cost Breakdown" in out


def test_a_tool_with_no_actor_refuses(db):
    """Fails closed, exactly like a gated tool called with no policy_scope open.

    Failing open here would silently defeat the wall for every background path.
    """
    _breakdown(db, owner=505)
    tool = CostBreakdownReadTool(db=db)
    out = tool._run(tender_id=TENDER)
    assert "No cost breakdown" in out or "cannot" in out.lower()


def test_a_new_breakdown_records_its_owner(db):
    """Otherwise created_by is NULL, the firewall reads that as a pre-wall row,
    and the costing researcher cannot see the breakdown they just produced."""
    from app.services.cost_breakdown_service import _create_breakdown_with_lines

    defaults = {"overhead_percent": 10.0, "margin_percent": 10.0, "gst_percent": 18.0}
    with actor_scope(user_id=606, role="costing_research"):
        cb = _create_breakdown_with_lines(
            db, tender_id=7777, normalised=[], costing={}, defaults=defaults,
        )
    assert cb.created_by == 606

    tool = CostBreakdownReadTool(db=db)
    with actor_scope(user_id=606, role="costing_research"):
        assert "Cost Breakdown" in tool._run(tender_id=7777)
    with actor_scope(user_id=607, role="costing_research"):
        assert "No cost breakdown" in tool._run(tender_id=7777)
