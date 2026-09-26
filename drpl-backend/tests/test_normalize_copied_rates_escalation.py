from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
from app.services.cost_breakdown_service import normalize_copied_rates


def _mk_breakdown(db):
    bd = CostBreakdown(tender_id=1)
    db.add(bd); db.commit(); db.refresh(bd)
    return bd


def test_fallback_tender_amount_includes_escalation(db):
    bd = _mk_breakdown(db)
    # A non-tax line that copies the published rate, qty set, escalation 10%,
    # tender_amount unset so the fallback path at :1025 fires.
    ln = CostBreakdownLine(
        cost_breakdown_id=bd.id, is_tax_line=False, description="Test line",
        tender_rate=100.0, quantity=2.0, rate=100.0,
        rate_source="tender_estimate", escalation_pct=10.0,
        tender_amount=None,
    )
    db.add(ln); db.commit()

    normalize_copied_rates(db, bd.id, overhead_pct=10.0, margin_pct=15.0)

    db.refresh(ln)
    # 100 * 2 * (1 + 10/100) = 220.0, NOT 200.0
    assert ln.tender_amount == 220.0
