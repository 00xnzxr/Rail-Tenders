"""Task 8 — end-to-end NIT-fidelity verification for Tender #2531 (Parel).

Proves the whole deterministic-first chain (Tasks 1-7) reconciles the real
Parel NIT exactly, with NO real LLM call:

  parse (Task 1: build_boq_items_from_text) -> persist BOQItem +
  BOQScheduleTotal (Task 3) -> build the NIT-mirror skeleton
  (cost_breakdown_service.build_skeleton_from_boq) -> reconciliation gate
  (Task 2/5) -> fabrication lockdown (Task 7, satisfied by construction since
  the skeleton IS the captured schedule).

The only step that would normally call an LLM (estimating the firm's own
cost per line) is stubbed with a fixed, deterministic multiplier fed through
the real `merge_batch_rates` merge path — every assertion below is about the
tender's own published numbers (tender_rate / tender_amount / the
reconciliation report), which come from the deterministic parser, not from
that estimate, so the test is fully deterministic regardless.

Mirrors the DB/session harness from test_reconciliation_flow.py and
test_fabrication_lockdown.py.
"""
from pathlib import Path

import fitz

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tender import Tender
from app.models.costing_template import BOQItem, BOQScheduleTotal
from app.services.boq_parser_service import build_boq_items_from_text
from app.services import cost_breakdown_service as svc

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "nit_parel_2531.pdf"
TENDER_ID = 2531


def _text() -> str:
    return "\n".join(pg.get_text() for pg in fitz.open(str(FIXTURE)))


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _persist_boq(db, tender_id: int, items: list[dict], sched_totals: list[dict]) -> None:
    """Mirrors the exact BOQItem/BOQScheduleTotal field mapping used by the
    real persist path in boq_parser_service.parse_boq_from_tender /
    _persist_schedule_totals."""
    for i, item in enumerate(items):
        db.add(BOQItem(
            tender_id=tender_id,
            sr_no=item.get("sr_no") or (i + 1),
            description=(item.get("description") or "").strip(),
            quantity=item.get("quantity"),
            unit=item.get("unit"),
            estimated_rate=item.get("estimated_rate"),
            item_code=(item.get("item_code") or None),
            schedule_name=(item.get("schedule_name") or None),
            bidding_unit=(item.get("bidding_unit") or None),
            basic_value=item.get("basic_value"),
            escalation_pct=item.get("escalation_pct"),
            is_tax_line=bool(item.get("is_tax_line")),
            extraction_confidence=item.get("extraction_confidence"),
        ))
    for s in sched_totals:
        db.add(BOQScheduleTotal(
            tender_id=tender_id,
            schedule_code=s["schedule_code"],
            stated_total=s.get("stated_total"),
            advertised_value=s.get("advertised_value"),
        ))
    db.commit()


def test_2531_totals_exact():
    db = _session()
    db.add(Tender(id=TENDER_ID, portal="ireps", tender_id="IREPS-2531",
                   title="Parel tender"))
    db.commit()

    # Step 1 — deterministic parse + persist (Tasks 1, 3, 4).
    items, sched_totals = build_boq_items_from_text(_text())
    assert items and sched_totals, "fixture failed to parse as IREPS schedule format"
    _persist_boq(db, TENDER_ID, items, sched_totals)

    # Step 2 — build the NIT-mirror skeleton. This is a pure DB->DB
    # transcription (no LLM): every BOQItem row becomes exactly one
    # CostBreakdownLine carrying tender_rate=item.estimated_rate verbatim.
    breakdown = svc.build_skeleton_from_boq(db, TENDER_ID)
    assert breakdown is not None

    # Stub the firm-cost estimation step (normally the costing LLM) with a
    # fixed deterministic multiplier fed through the real merge path, so the
    # "costing build" is genuinely exercised without a live model call.
    batch = [
        {
            "boq_item_id": ln.boq_item_id,
            "schedule_name": ln.schedule_name,
            "item_code": ln.item_code,
            "sr_no": ln.sr_no,
            "rate": round((ln.tender_rate or 0.0) * 0.85, 2),
            "rate_source": "training_data",
        }
        for ln in breakdown.lines
        if not ln.is_tax_line
    ]
    merge_result = svc.merge_batch_rates(db, breakdown.id, batch)
    assert merge_result["unmatched"] == 0

    breakdown = svc.recompute_breakdown_totals(db, breakdown)
    payload = svc.to_dict(breakdown)

    # --- Reconciliation must PASS, not be flagged for review -----------------
    recon = payload["reconciliation"]
    assert recon is not None
    assert recon["ok"] is True
    assert payload["needs_review"] is False

    sched_by_code = {s["code"]: s for s in recon["schedules"]}
    assert sched_by_code["A"]["ok"] is True
    assert sched_by_code["B"]["ok"] is True
    assert sched_by_code["C"]["ok"] is True

    # --- Exact totals from the NIT (non-negotiable) ---------------------------
    assert sched_by_code["A"]["line_sum"] == 5975127.60
    assert sched_by_code["B"]["line_sum"] == 53693176.56
    assert sched_by_code["C"]["line_sum"] == 1211088.00
    assert recon["grand_line_sum"] == 60879392.16
    assert recon["advertised_value"] == 60879392.16

    # --- One row per NIT line, no dupes ----------------------------------------
    lines = list(breakdown.lines)
    a_lines = [l for l in lines if (l.schedule_name or "").strip().upper() == "A"]
    b_lines = [l for l in lines if (l.schedule_name or "").strip().upper() == "B"]
    c_lines = [l for l in lines if (l.schedule_name or "").strip().upper() == "C"]
    assert len(a_lines) == 2
    assert len(c_lines) == 1
    assert len(b_lines) == len([it for it in items if it["schedule_name"] == "B"])
    assert len(lines) == len(items)  # 1:1 mirror — nothing invented, nothing dropped

    # --- Zero invented lines ----------------------------------------------------
    # "Robotic AC duct cleaning" is a genuine NIT line but belongs ONLY to
    # Schedule C — it must never appear fabricated into another schedule.
    for l in lines:
        desc = (l.description or "").lower()
        if "robotic" in desc:
            assert (l.schedule_name or "").strip().upper() == "C"
        assert "gst mandate form" not in desc

    # --- Every persisted line retains a captured tender_rate/tender_amount. ----
    for l in lines:
        assert l.tender_rate is not None
        assert l.tender_amount is not None

    # --- Cross-check against the NIT's OWN independently-printed columns. ------
    # The persisted tender_amount is itself computed by the pipeline as
    # round(tender_rate * qty, 2), so re-deriving it from tender_rate/qty would
    # be tautological (can only ever catch a float-rounding bug). Instead,
    # verify the parser's captured estimated_rate/quantity against the NIT's
    # own separately-printed "Basic Value" and "Amount" columns (parsed
    # independently from Unit Rate/Qty by build_boq_items_from_text) — this is
    # a real NIT-fidelity guarantee: it would catch a parser rate/qty misread
    # that happened to still be internally self-consistent post-persistence.
    TOL = 0.01
    checked = 0
    for item in items:
        rate = item.get("estimated_rate")
        qty = item.get("quantity")
        basic_value = item.get("basic_value")
        amount = item.get("amount")
        if rate is None or qty is None or basic_value is None or amount is None:
            continue
        escl = (item.get("escalation_pct") or 0.0) / 100.0
        expected_basic_value = round(rate * qty, 2)
        assert abs(expected_basic_value - basic_value) <= TOL, (
            f"sr_no={item.get('sr_no')!r} desc={item.get('description')!r}: "
            f"rate*qty={expected_basic_value} vs NIT basic_value={basic_value}"
        )
        expected_amount = round(basic_value * (1 + escl), 2)
        assert abs(expected_amount - amount) <= TOL, (
            f"sr_no={item.get('sr_no')!r} desc={item.get('description')!r}: "
            f"basic_value*(1+escl)={expected_amount} vs NIT amount={amount}"
        )
        checked += 1
    assert checked == len(items)
