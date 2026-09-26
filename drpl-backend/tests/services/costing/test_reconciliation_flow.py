"""Wires the reconciliation gate (Task 2's `reconcile`) into the costing
service so a persisted CostBreakdown carries a `reconciliation` report +
`needs_review` flag whenever the tender has captured BOQScheduleTotal rows.
Never mutates line amounts to force a match — flag only."""
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


def _seed_tender_with_schedule_totals(db, *, stated_total_a: float):
    db.add(Tender(id=1, portal="ireps", tender_id="T-1", title="Tender one"))
    db.add(BOQScheduleTotal(tender_id=1, schedule_code="A",
                             stated_total=stated_total_a, advertised_value=stated_total_a))
    db.commit()


def _make_breakdown_with_lines(db, *, line_amounts_a: list[float]) -> CostBreakdown:
    breakdown = CostBreakdown(tender_id=1, version=1, status="draft")
    db.add(breakdown)
    db.flush()
    for i, amt in enumerate(line_amounts_a):
        db.add(CostBreakdownLine(
            cost_breakdown_id=breakdown.id, sr_no=i + 1, description=f"Line {i+1}",
            schedule_name="A", quantity=1, unit="no",
            rate=amt, amount=amt, tender_amount=amt,
        ))
    db.commit()
    db.refresh(breakdown)
    return breakdown


def test_reconciliation_passes_when_lines_match_stated_total():
    db = _session()
    _seed_tender_with_schedule_totals(db, stated_total_a=250.0)
    breakdown = _make_breakdown_with_lines(db, line_amounts_a=[100.0, 150.0])

    breakdown = svc.recompute_breakdown_totals(db, breakdown)

    assert breakdown.needs_review is False
    payload = svc.to_dict(breakdown)
    assert payload["needs_review"] is False
    sched = next(s for s in payload["reconciliation"]["schedules"] if s["code"] == "A")
    assert sched["ok"] is True
    assert abs(sched["delta"]) < 1e-6


def test_reconciliation_flags_shortfall():
    db = _session()
    # Stated total is 300 but the persisted lines only sum to 250 — a ₹50 shortfall.
    _seed_tender_with_schedule_totals(db, stated_total_a=300.0)
    breakdown = _make_breakdown_with_lines(db, line_amounts_a=[100.0, 150.0])

    breakdown = svc.recompute_breakdown_totals(db, breakdown)

    assert breakdown.needs_review is True
    payload = svc.to_dict(breakdown)
    assert payload["needs_review"] is True
    sched = next(s for s in payload["reconciliation"]["schedules"] if s["code"] == "A")
    assert sched["ok"] is False
    assert abs(sched["delta"] + 50.0) < 1e-6
    # Never mutate line amounts to force a match.
    amounts = sorted(ln.amount for ln in breakdown.lines)
    assert amounts == [100.0, 150.0]


def test_reconciliation_matches_schedule_code_case_insensitively():
    # BOQScheduleTotal.schedule_code is persisted upper-cased ("A"), but an
    # agent-authored breakdown line may carry a lower-cased schedule_name
    # ("a"). Matching must normalize both sides to strip().upper() so a real
    # shortfall isn't silently swallowed by a casing mismatch.
    db = _session()
    _seed_tender_with_schedule_totals(db, stated_total_a=300.0)
    breakdown = CostBreakdown(tender_id=1, version=1, status="draft")
    db.add(breakdown)
    db.flush()
    for i, amt in enumerate([100.0, 150.0]):
        db.add(CostBreakdownLine(
            cost_breakdown_id=breakdown.id, sr_no=i + 1, description=f"Line {i+1}",
            schedule_name="a", quantity=1, unit="no",
            rate=amt, amount=amt, tender_amount=amt,
        ))
    db.commit()
    db.refresh(breakdown)

    breakdown = svc.recompute_breakdown_totals(db, breakdown)

    assert breakdown.needs_review is True
    payload = svc.to_dict(breakdown)
    assert payload["needs_review"] is True
    sched = next(s for s in payload["reconciliation"]["schedules"] if s["code"] == "A")
    assert sched["ok"] is False
    assert abs(sched["delta"] + 50.0) < 1e-6


def test_no_captured_schedule_totals_does_not_flag():
    db = _session()
    db.add(Tender(id=2, portal="ireps", tender_id="T-2", title="Tender two"))
    db.commit()
    breakdown = CostBreakdown(tender_id=2, version=1, status="draft")
    db.add(breakdown)
    db.flush()
    db.add(CostBreakdownLine(
        cost_breakdown_id=breakdown.id, sr_no=1, description="Line 1",
        schedule_name="A", quantity=1, unit="no", rate=100.0, amount=100.0,
    ))
    db.commit()
    db.refresh(breakdown)

    breakdown = svc.recompute_breakdown_totals(db, breakdown)

    assert breakdown.needs_review is False
    payload = svc.to_dict(breakdown)
    assert payload["reconciliation"] is None
