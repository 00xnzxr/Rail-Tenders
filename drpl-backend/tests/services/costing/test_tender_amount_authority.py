"""tender_amount must ALWAYS be the NIT-authoritative value
`tender_rate * qty * (1 + escalation_pct/100)` and must NEVER come from the
LLM.

Covers two whole-branch-review findings:

  - Important 1: `merge_batch_rates` used to spread the agent's raw batch
    line (`{**raw, ...}`) into `_normalize_line_dict` without forcing
    `tender_amount`, so an agent-supplied wrong `tender_amount` flowed
    straight onto the persisted line. Fixed by making `_normalize_line_dict`
    always recompute `tender_amount` from `tender_rate`/`quantity`/
    `escalation_pct` whenever a `tender_rate` is present, and by excluding
    `tender_amount` from `merge_batch_rates`' `_COST_FIELDS` (it is
    NIT-authoritative, like tender_rate/quantity/item_code/schedule_name,
    which were already excluded).

  - Important 2: `_normalize_line_dict` used to compute
    `tender_amount = round(tender_rate * qty, 2)` with NO escalation factor,
    even though the NIT's own `BOQScheduleTotal.stated_total` and the xlsx
    Tender Amount formula are both escalation-INCLUSIVE
    (`qty * tender_rate * (1 + escl/100)`). Fixed by including the
    escalation factor in the persisted `tender_amount`.

Mirrors the DB/session harness from test_reconciliation_flow.py.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tender import Tender
from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
from app.models.costing_template import BOQScheduleTotal
from app.services import cost_breakdown_service as svc


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _make_skeleton_line(db, *, tender_rate: float, quantity: float,
                         escalation_pct: float = 0.0) -> CostBreakdown:
    """Build a single-line skeleton CostBreakdownLine the way
    build_skeleton_from_boq would: tender_rate/quantity/escalation_pct set,
    rate/amount unpriced (needs_input), tender_amount computed by
    _normalize_line_dict."""
    db.add(Tender(id=1, portal="ireps", tender_id="T-1", title="Tender one"))
    db.commit()

    breakdown = CostBreakdown(tender_id=1, version=1, status="draft")
    db.add(breakdown)
    db.flush()

    normalized = svc._normalize_line_dict(
        {
            "sr_no": 1,
            "description": "Line 1",
            "quantity": quantity,
            "unit": "Nos",
            "item_code": "A1",
            "schedule_name": "A",
            "tender_rate": tender_rate,
            "escalation_pct": escalation_pct,
            "rate": None,
        },
        sr_no=1,
        breakdown_margin_percent=10.0,
    )
    line = CostBreakdownLine(cost_breakdown_id=breakdown.id, **normalized)
    db.add(line)
    db.commit()
    db.refresh(breakdown)
    return breakdown


def test_agent_wrong_tender_amount_is_ignored_by_merge():
    """merge_batch_rates must re-derive tender_amount from the NIT-captured
    tender_rate/quantity — never trust the agent's own tender_amount field,
    even though the prompt explicitly asks the agent to echo it back."""
    db = _session()
    breakdown = _make_skeleton_line(db, tender_rate=100.0, quantity=10.0, escalation_pct=0.0)
    correct_tender_amount = 1000.0  # 100 * 10 * (1 + 0/100)

    line = breakdown.lines[0]
    assert line.tender_amount == correct_tender_amount  # sanity: skeleton is correct

    batch = [
        {
            "boq_item_id": line.boq_item_id,
            "schedule_name": line.schedule_name,
            "item_code": line.item_code,
            "sr_no": line.sr_no,
            "rate": 80.0,
            "rate_source": "training_data",
            # Wrong on purpose — agent hallucinated a bogus tender_amount.
            "tender_amount": 999999.0,
        },
    ]
    result = svc.merge_batch_rates(db, breakdown.id, batch)
    assert result == {"matched": 1, "unmatched": 0}

    db.refresh(line)
    assert line.tender_amount == correct_tender_amount
    assert line.tender_amount != 999999.0
    # The cost-side field the agent DID legitimately own was applied.
    assert line.rate == 80.0


def test_persisted_tender_amount_includes_nit_escalation_and_reconciles():
    """A 5% Escl.(%) line must have tender_amount = rate*qty*1.05, not the
    escalation-free rate*qty — otherwise the reconciliation gate false-flags
    needs_review against the NIT's own escalation-inclusive stated_total."""
    db = _session()
    breakdown = _make_skeleton_line(db, tender_rate=100.0, quantity=10.0, escalation_pct=5.0)
    line = breakdown.lines[0]

    expected_tender_amount = 1050.0  # 100 * 10 * 1.05
    assert line.tender_amount == expected_tender_amount

    # merge_batch_rates must preserve this even when the agent tries to
    # overwrite it with the escalation-free (wrong) value.
    batch = [
        {
            "boq_item_id": line.boq_item_id,
            "schedule_name": line.schedule_name,
            "item_code": line.item_code,
            "sr_no": line.sr_no,
            "rate": 80.0,
            "rate_source": "training_data",
            "tender_amount": 1000.0,  # escalation-free — wrong
        },
    ]
    svc.merge_batch_rates(db, breakdown.id, batch)
    db.refresh(line)
    assert line.tender_amount == expected_tender_amount

    # Now reconcile against an escl-inclusive stated_total.
    db.add(BOQScheduleTotal(tender_id=1, schedule_code="A",
                             stated_total=expected_tender_amount,
                             advertised_value=expected_tender_amount))
    db.commit()

    breakdown = svc.recompute_breakdown_totals(db, breakdown)
    assert breakdown.needs_review is False
    payload = svc.to_dict(breakdown)
    sched = next(s for s in payload["reconciliation"]["schedules"] if s["code"] == "A")
    assert sched["ok"] is True
    assert abs(sched["delta"]) < 1e-6
