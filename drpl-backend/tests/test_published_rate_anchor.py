"""A rate computed from the tender's published rate is not a cost.

The Mid-Life Rehabilitation NIT (Liluah, 285 rows, schedules A-T) came back
with every row `derived_estimate` and a source of "Derived from published
Rs X by stripping ~23%": every platform rate was the railway's rate times
0.77, beside a material build-up that summed to a fraction of it. The copied-
rate guard only caught a rate *equal* to the published one, so nothing did.
"""
import itertools

import pytest

from app.models.costing_template import BOQItem
from app.models.tender import Tender
from app.services import cost_breakdown_service as cbs

_ids = itertools.count(940000)

# Verbatim from the Mid-Life run.
ANCHORED = [
    "Derived from published ₹236K by stripping ~23% for embedded labour+overhead; material-dominant assembly",
    "Published ref rate ₹979.65/unit stripped of 15% margin + 10% overhead = ₹680",
    "Derived from tender published rate ₹79,060.00 by stripping 25% overhead+margin: ₹79,060.00 ÷ 1.25 = ₹63,248.00",
    "Derived from published ₹37,000 by stripping 25% margin: ₹37,000 ÷ 1.29 = ₹28,682 (conservative)",
    "Derived from published ₹2,794.24; plate component",
    "77% of the published rate",
    "NIT rate ₹4,425 ÷ 1.25 = ₹3,540",
]

# Real evidence and real work items that merely mention the same words.
INDEPENDENT = [
    "indiamart.com Apr-2026 listings: ₹6,500, ₹7,250 — picked ₹7,250 median; below the published ₹9,000",
    "Manhours 8 × ₹450/hr + 2.5 kg @ ₹180/kg = ₹4,050 (published ₹5,200 for reference)",
    "L1 rate of previous Liluah tender 2025 ₹1,850 escalated 6%",
    "Stripping of coach interiors: 40 manhours @ ₹600/hr",
    "Tender scope: stripping and repainting, 12 hr @ ₹600/hr",
    "Derived from first principles: MS 70 kg @ ₹65/kg + labour 8 hr @ ₹600/hr",
    "Derived from DSR 2023 item 12.4 escalated 8%",
    "Rate from published DSR 2023 item 4.1",
    "Derived from NIT drawing quantities: 70 kg @ ₹65/kg",
    "web: ₹1,450/unit vs published ₹1,119.60/unit (quote 1.3x)",
]


@pytest.mark.parametrize("text", ANCHORED)
def test_a_rate_derived_from_the_published_rate_is_recognised(text):
    assert cbs.rate_anchored_to_published(text)


@pytest.mark.parametrize("text", INDEPENDENT)
def test_evidence_that_mentions_the_published_rate_is_not(text):
    assert not cbs.rate_anchored_to_published(text)


def test_the_note_counts_as_much_as_the_source():
    assert cbs.rate_anchored_to_published(None, "Published ref rate ₹2239.21 stripped of 15% margin")
    assert not cbs.rate_anchored_to_published(None, None)


@pytest.fixture
def breakdown(db):
    tid = next(_ids)
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="Mid-Life", source_url="x"))
    parent = BOQItem(tender_id=tid, sr_no=1, item_code="1", schedule_name="A",
                     quantity=44, unit="Numbers", estimated_rate=314430.12,
                     description="Front part")
    db.add(parent)
    db.flush()
    db.add_all([
        BOQItem(tender_id=tid, sr_no=2, item_code="2", schedule_name="A",
                quantity=44, unit="Numbers", estimated_rate=2239.21, description="Web"),
        BOQItem(tender_id=tid, sr_no=1, item_code="ANX-M-1", annexure_ref="M",
                parent_item_id=parent.id, quantity=1, unit="Nos", estimated_rate=500,
                basic_value=500, description="Child part"),
    ])
    db.commit()
    return cbs.build_skeleton_from_boq(db, tid)


def _line(bd, code):
    return next(ln for ln in bd.lines if ln.item_code == code)


def test_merge_sends_an_anchored_rate_back_for_recosting(db, breakdown):
    out = cbs.merge_batch_rates(db, breakdown.id, [
        {"schedule_name": "A", "item_code": "2", "sr_no": 2, "rate": 1560,
         "rate_source": "derived_estimate",
         "source_ref": "Published ref rate ₹2239.21/unit stripped of 15% margin + 10% overhead = ₹1560",
         "cost_buildup_note": "MS steel 0.22 kg @ ₹50/kg + fabrication labour ₹340 = ₹1560/unit"},
    ])
    assert out == {"matched": 1, "unmatched": 0, "rejected_anchored": 1}
    db.expire_all()
    row = _line(breakdown, "2")
    assert row.needs_input and row.rate is None and row.amount is None
    assert row.rate_source == "needs_user_input"
    assert "published rate" in row.source_ref


def test_merge_keeps_an_independent_rate_even_at_the_published_figure(db, breakdown):
    out = cbs.merge_batch_rates(db, breakdown.id, [
        {"schedule_name": "A", "item_code": "2", "sr_no": 2, "rate": 2239.21,
         "rate_source": "web_search", "source_url": "https://example.com/q",
         "source_ref": "two supplier quotes Sep-2026: ₹2,200 and ₹2,280"},
    ])
    assert out == {"matched": 1, "unmatched": 0}
    db.expire_all()
    row = _line(breakdown, "2")
    assert not row.needs_input and row.rate == pytest.approx(2239.21)


def test_an_annexure_component_keeps_its_printed_basis(db, breakdown):
    out = cbs.merge_batch_rates(db, breakdown.id, [
        {"item_code": "ANX-M-1", "rate": 400, "rate_source": "derived_estimate",
         "source_ref": "Derived from published ₹500 by stripping 25% overhead+margin"},
    ])
    assert out == {"matched": 1, "unmatched": 0}
    db.expire_all()
    assert _line(breakdown, "ANX-M-1").rate == pytest.approx(400)


def test_a_rejected_row_that_is_never_recosted_is_labelled_by_the_guard(db, breakdown):
    cbs.merge_batch_rates(db, breakdown.id, [
        {"schedule_name": "A", "item_code": "2", "sr_no": 2, "rate": 1791,
         "rate_source": "derived_estimate",
         "source_ref": "Derived from published ₹2,239.21 by stripping 20% margin"},
    ])
    cbs.normalize_copied_rates(db, breakdown.id, overhead_pct=10, margin_pct=15)
    db.expire_all()
    row = _line(breakdown, "2")
    assert row.rate_source == "derived_estimate" and not row.needs_input
    assert row.rate == pytest.approx(round(2239.21 / 1.25, 2))
    assert "rejected" in row.source_ref


def test_the_batched_agent_never_sees_a_schedule_rows_published_rate():
    from app.services.langchain.graphs import enhanced_costing_agent as eca
    rows = [
        {"boq_item_id": 1, "sr_no": 17, "item_code": "17", "schedule_name": "A",
         "description": "Web to Drg No.LE11317", "quantity": 59, "unit": "Numbers",
         "estimated_rate": 979.65, "basic_value": 57799.35},
        {"boq_item_id": 2, "sr_no": 1, "item_code": "ANX-M-1", "annexure_ref": "M",
         "component_of": "Schedule A item 1", "description": "Child part",
         "quantity": 1, "unit": "Nos", "estimated_rate": 4321.5, "basic_value": 4321.5},
    ]
    blind = eca._render_bidding_schedule_block(rows, withhold_published=True)
    assert "979.65" not in blind and "57799.35" not in blind
    assert "4321.5" in blind  # a component keeps its printed basis
    assert rows[0]["estimated_rate"] == 979.65  # the caller's rows are not mutated
    # The single-call path still shows it: its agent emits tender_rate itself.
    assert "979.65" in eca._render_bidding_schedule_block(rows)


def _price(db, bd, code, rate, note):
    cbs.merge_batch_rates(db, bd.id, [
        {"schedule_name": "A", "item_code": code, "sr_no": int(code), "rate": rate,
         "rate_source": "derived_estimate", "source_ref": note, "cost_buildup_note": note},
    ])


def test_an_agent_build_up_is_not_counted_as_a_formula_copy(db, breakdown):
    _price(db, breakdown, "2", 1900, "MS 20 kg @ Rs 65/kg + 1 hr @ Rs 600/hr")
    obs = " ".join(cbs.build_strategic_summary(db, breakdown)["key_observations"])
    assert "derived from the published rate by formula" not in obs
    # ... while the guard's own fallback still is.
    _price(db, breakdown, "2", 1791, "Derived from published Rs 2,239.21 by stripping 20% margin")
    cbs.normalize_copied_rates(db, breakdown.id, overhead_pct=10, margin_pct=15)
    db.expire_all()
    obs = " ".join(cbs.build_strategic_summary(db, breakdown)["key_observations"])
    assert "derived from the published rate by formula" in obs
