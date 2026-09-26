"""Task 7 — fabrication lockdown.

When a tender has a captured NIT bidding schedule (BOQItem rows), the costing
agent's persisted output must contain ONLY lines that mirror those captured
rows — an agent-invented line (extra schedule item that was never in the NIT)
must be dropped from the schedule and quarantined into `assumptions`, never
silently added as a priced CostBreakdownLine. This is what stops phantom rows
like "Robotic AC duct cleaning" from reaching the persisted breakdown.

Mirrors the DB/session harness from test_reconciliation_flow.py.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tender import Tender
from app.models.costing_template import BOQItem
from app.services import cost_breakdown_service as svc


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_tender_with_2_nit_lines(db):
    db.add(Tender(id=1, portal="ireps", tender_id="T-1", title="Tender one"))
    db.add(BOQItem(
        tender_id=1, sr_no=1, item_code="A1", schedule_name="A",
        description="Monthly preventive maintenance visit", quantity=12, unit="Nos",
        estimated_rate=1000.0, basic_value=12000.0, escalation_pct=5.0,
        bidding_unit="Nos", is_tax_line=False,
    ))
    db.add(BOQItem(
        tender_id=1, sr_no=2, item_code="A2", schedule_name="A",
        description="Annual overhaul", quantity=1, unit="Nos",
        estimated_rate=50000.0, basic_value=50000.0, escalation_pct=5.0,
        bidding_unit="Nos", is_tax_line=False,
    ))
    db.commit()


def test_invented_line_is_dropped_and_quarantined():
    db = _session()
    _seed_tender_with_2_nit_lines(db)

    agent_output = {
        "line_items": [
            {
                "sr_no": 1, "item_code": "A1", "schedule_name": "A",
                "description": "Monthly preventive maintenance visit",
                "quantity": 12, "unit": "Nos", "rate": 900.0,
                "rate_source": "training_data", "boq_item_id": 1,
            },
            {
                "sr_no": 2, "item_code": "A2", "schedule_name": "A",
                "description": "Annual overhaul",
                "quantity": 1, "unit": "Nos", "rate": 45000.0,
                "rate_source": "training_data", "boq_item_id": 2,
            },
            {
                # Invented — not part of the captured NIT schedule at all.
                "sr_no": 3, "item_code": "A99", "schedule_name": "A",
                "description": "Robotic AC duct cleaning",
                "quantity": 1, "unit": "Nos", "rate": 850000.0,
                "rate_source": "web_search",
            },
        ],
        "assumptions": [],
    }

    breakdown = svc.persist_from_agent_output(db, tender_id=1, costing=agent_output)

    assert breakdown is not None
    lines = list(breakdown.lines)
    assert len(lines) == 2
    descriptions = {ln.description for ln in lines}
    assert "Robotic AC duct cleaning" not in descriptions
    assert descriptions == {
        "Monthly preventive maintenance visit",
        "Annual overhaul",
    }

    # Quarantined into assumptions, not silently discarded.
    import json
    assumptions = json.loads(breakdown.assumptions_json or "[]")
    assert any("Robotic AC duct cleaning" in a for a in assumptions)
    assert any("FABRICATED" in a.upper() for a in assumptions)


def test_agent_derived_escalation_is_overridden_by_nit_value():
    """The agent must not be able to invent its own escalation_pct for a
    matched NIT line — the captured BOQItem's own Escl.(%) is authoritative."""
    db = _session()
    _seed_tender_with_2_nit_lines(db)

    agent_output = {
        "line_items": [
            {
                "sr_no": 1, "item_code": "A1", "schedule_name": "A",
                "description": "Monthly preventive maintenance visit",
                "quantity": 12, "unit": "Nos", "rate": 900.0,
                "rate_source": "training_data", "boq_item_id": 1,
                "escalation_pct": 40.0,  # agent-invented, should be overridden
            },
            {
                "sr_no": 2, "item_code": "A2", "schedule_name": "A",
                "description": "Annual overhaul",
                "quantity": 1, "unit": "Nos", "rate": 45000.0,
                "rate_source": "training_data", "boq_item_id": 2,
            },
        ],
        "assumptions": [],
    }

    breakdown = svc.persist_from_agent_output(db, tender_id=1, costing=agent_output)

    assert breakdown is not None
    line_a1 = next(ln for ln in breakdown.lines if ln.item_code == "A1")
    assert line_a1.escalation_pct == 5.0  # the NIT's own value, not 40.0


def test_real_line_with_drifted_item_code_is_kept_not_quarantined():
    """A genuine NIT line the agent costed correctly must survive even when
    its `item_code` formatting drifted from the stored BOQItem (e.g.
    "A-1" vs the captured "A1"). The strict-key partition matcher used to
    quarantine this as a fabricated line; it must now fall back to
    boq_item_id / normalized-code matching, same as merge_batch_rates."""
    db = _session()
    _seed_tender_with_2_nit_lines(db)  # BOQItem item_code="A1" for sr_no=1

    agent_output = {
        "line_items": [
            {
                "sr_no": 1, "item_code": "A-1", "schedule_name": "A",
                "description": "Monthly preventive maintenance visit",
                "quantity": 12, "unit": "Nos", "rate": 900.0,
                "rate_source": "training_data", "boq_item_id": 1,
            },
            {
                "sr_no": 2, "item_code": "A2", "schedule_name": "A",
                "description": "Annual overhaul",
                "quantity": 1, "unit": "Nos", "rate": 45000.0,
                "rate_source": "training_data", "boq_item_id": 2,
            },
        ],
        "assumptions": [],
    }

    breakdown = svc.persist_from_agent_output(db, tender_id=1, costing=agent_output)

    assert breakdown is not None
    lines = list(breakdown.lines)
    assert len(lines) == 2
    descriptions = {ln.description for ln in lines}
    assert descriptions == {
        "Monthly preventive maintenance visit",
        "Annual overhaul",
    }

    import json
    assumptions = json.loads(breakdown.assumptions_json or "[]")
    assert not any("FABRICATED" in a.upper() for a in assumptions)
    assert not any(
        "Monthly preventive maintenance visit" in a and "DROPPED" in a.upper()
        for a in assumptions
    )

    # The NIT's own escalation is still authoritative for the drifted line.
    line_a1 = next(ln for ln in lines if ln.description == "Monthly preventive maintenance visit")
    assert line_a1.escalation_pct == 5.0


def test_no_captured_schedule_allows_freeform_line_items():
    """When the tender has NO captured BOQItem schedule, persist_from_agent_output
    must behave exactly as before — nothing to validate against, so freeform
    line items pass through unchanged."""
    db = _session()
    db.add(Tender(id=2, portal="ireps", tender_id="T-2", title="Tender two"))
    db.commit()

    agent_output = {
        "line_items": [
            {
                "sr_no": 1, "description": "Ad-hoc consumables",
                "quantity": 1, "unit": "Lot", "rate": 5000.0,
                "rate_source": "training_data",
            },
        ],
        "assumptions": [],
    }

    breakdown = svc.persist_from_agent_output(db, tender_id=2, costing=agent_output)

    assert breakdown is not None
    assert len(list(breakdown.lines)) == 1
    assert breakdown.lines[0].description == "Ad-hoc consumables"
