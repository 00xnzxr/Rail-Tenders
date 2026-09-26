"""Every schedule row takes the figure its evidence supports -- never the
railway's rate, unless nothing of the platform's own could be finished.

The Mid-Life NIT (Liluah, 286 rows) came back with 284 rows at the railway's
rate divided by 1.25 -- "the rate which platform used is the same railway
rate". Settlement had given every row without a verified market price the
railway's estimate, because the costing agent's blind build-ups ran 0.15x to
6.45x of it. Now the platform builds each row's cost itself
(costing/cost_buildup.py) and `settle_rates_on_evidence` keeps, in order: a
verified market price or the firm's own rate data within 0.5-2x of the
railway's cost; the platform's own build-up (already held to that band by a
second look); the agent's own build-up when it is within the band. Only a row
left with none of them takes the railway's estimate, and says why.
"""
import itertools

import pytest

from app.models.costing_template import BOQItem
from app.models.tender import Tender
from app.services import cost_breakdown_service as cbs
from app.services.costing.cost_buildup import BUILDUP_MARKER

_ids = itertools.count(950000)
OH, MG = 10, 15  # reference cost = published / 1.25


@pytest.fixture
def breakdown(db):
    tid = next(_ids)
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="Mid-Life", source_url="x"))
    db.add_all([
        BOQItem(tender_id=tid, sr_no=1, item_code="1", schedule_name="P", quantity=100,
                unit="Metre", estimated_rate=55.22, description="Elastomeric cable 4 sq mm"),
        BOQItem(tender_id=tid, sr_no=2, item_code="2", schedule_name="O", quantity=37,
                unit="Per Coach", estimated_rate=188800.0, description="Fitting & Repairing Charges"),
        BOQItem(tender_id=tid, sr_no=3, item_code="3", schedule_name="A", quantity=44,
                unit="Numbers", estimated_rate=2239.21, description="Web to Drg No LE11185"),
        BOQItem(tender_id=tid, sr_no=4, item_code="4", schedule_name="P", quantity=37,
                unit="Numbers", estimated_rate=991.14, description="LED tail lamp EDTS-215"),
    ])
    db.commit()
    return cbs.build_skeleton_from_boq(db, tid)


def _price(db, bd, code, sched, rate, source, ref, url=None, verified=False, note=None):
    if note is None:
        note = f"{cbs.VERIFIED_WEB_PRICE}, exact match: {ref}" if verified else ref
    # `verified` stands for the platform's own research or build-up, the only
    # callers allowed to write their markers.
    cbs.merge_batch_rates(db, bd.id, [{
        "schedule_name": sched, "item_code": code, "sr_no": int(code), "rate": rate,
        "rate_source": source, "source_ref": ref, "source_url": url, "cost_buildup_note": note,
    }], platform_verified=verified)


def _platform(db, bd, code, sched, rate):
    _price(db, bd, code, sched, rate, "derived_estimate",
           "Platform build-up: materials Rs 1 + labour Rs 1 + other Rs 0",
           note=f"{BUILDUP_MARKER} for one unit (labour only): materials Rs 0 + labour Rs {rate:,.0f} "
                f"+ other Rs 0 = Rs {rate:,.0f}.", verified=True)


def _line(bd, code):
    return next(ln for ln in bd.lines if ln.item_code == code)


def _all_priced(db, bd):
    _price(db, bd, "1", "P", 45, "web_search", "IndiaMART Jun-2026: Rs 45/m",
           "https://dir.indiamart.com/impcat/single-core-cables/wire-size-4-sqmm-q13140002/",
           verified=True)
    _price(db, bd, "2", "O", 6500, "derived_estimate", "4 workers x 16 hr @ Rs 90/hr")
    _price(db, bd, "3", "A", 1700, "derived_estimate", "MS 2 kg @ Rs 65/kg + 2 hr @ Rs 700/hr + consumables")
    _price(db, bd, "4", "P", 22000, "web_search", "RCF tail lamp listing Rs 22,000",
           "https://example.com/tail-lamp", verified=True)


def test_each_row_takes_the_figure_its_evidence_supports(db, breakdown):
    _all_priced(db, breakdown)
    counts = cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    assert counts == {"market": 1, "buildup": 1, "reference": 2, "replaced": 2}
    db.expire_all()

    cable = _line(breakdown, "1")  # 45 against a reference of 44.18: market price stands
    assert cable.rate == pytest.approx(45) and cable.source_url
    assert cable.cost_buildup_note.startswith(cbs.BASIS_MARKET)

    web = _line(breakdown, "3")  # the agent's build-up, 0.95x the reference: its own figure stands
    assert web.rate == pytest.approx(1700)
    assert web.cost_buildup_note.startswith(cbs.BASIS_BUILDUP)
    assert "0.95 times" in web.cost_buildup_note

    labour = _line(breakdown, "2")  # 6500 against 151,040: 0.04x, not trusted
    assert labour.rate == pytest.approx(151040.0)
    assert labour.amount == pytest.approx(151040.0 * 37)
    assert labour.cost_buildup_note.startswith(cbs.BASIS_REFERENCE)
    assert "Rs 6,500.00" in labour.cost_buildup_note  # the original figure is kept
    assert labour.margin_pct == pytest.approx(20.0)

    lamp = _line(breakdown, "4")  # a 22x "market price" is a different product
    assert lamp.rate == pytest.approx(round(991.14 / 1.25, 2))
    assert lamp.source_url is None and "market price found" in lamp.cost_buildup_note


def test_the_platforms_own_build_up_stands_even_far_from_the_railway(db, breakdown):
    """Its band was already applied (a second look, the nearer figure kept);
    settlement states the ratio and leaves the figure alone."""
    _platform(db, breakdown, "2", "O", 60000.0)  # 0.40x of 151,040
    counts = cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    labour = _line(breakdown, "2")
    assert labour.rate == pytest.approx(60000.0)
    assert labour.cost_buildup_note.startswith(cbs.BASIS_BUILDUP)
    assert "platform's own cost build-up" in labour.cost_buildup_note
    assert "0.40 times" in labour.cost_buildup_note and BUILDUP_MARKER in labour.cost_buildup_note
    assert "implies a cost of Rs 151,040.00" in labour.cost_buildup_note
    assert counts["buildup"] >= 1


def test_no_row_the_platform_built_up_is_the_railway_rate_scaled(db, breakdown):
    for code, sched, rate in (("1", "P", 50.0), ("2", "O", 170000.0), ("3", "A", 1500.0), ("4", "P", 900.0)):
        _platform(db, breakdown, code, sched, rate)
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    for ln in breakdown.lines:
        assert ln.cost_buildup_note.startswith(cbs.BASIS_BUILDUP)
        assert abs(ln.rate - ln.tender_rate / 1.25) > 0.01
        assert abs(ln.rate - ln.tender_rate) > 0.01


def test_settling_twice_changes_nothing(db, breakdown):
    _all_priced(db, breakdown)
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    before = {ln.item_code: (ln.rate, ln.cost_buildup_note) for ln in breakdown.lines}
    again = cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    assert {ln.item_code: (ln.rate, ln.cost_buildup_note) for ln in breakdown.lines} == before
    # A replaced row is a railway-estimate row on the second pass, not a new replacement.
    assert again == {"market": 1, "buildup": 1, "reference": 2, "replaced": 0}


def test_a_gst_inclusive_schedule_is_measured_without_its_gst(db, breakdown):
    """Banner "(INCLUSIVE OF ALL TAXES AND CHARGES)": the railway's rate
    carries 18% GST, so the cost it implies is published / 1.18 / 1.25."""
    # Never priced: finalisation's copied-rate guard derives it, then settlement
    # states the labelled fallback.
    cbs.normalize_copied_rates(db, breakdown.id, OH, MG)
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG, gst_pct=18, taxes_inclusive={"O": True})
    db.expire_all()
    labour = _line(breakdown, "2")
    assert labour.rate == pytest.approx(round(188800.0 / 1.25 / 1.18, 2))
    assert "less 18% GST" in labour.cost_buildup_note
    assert labour.cost_buildup_note.count(cbs._FORMULA_NOTE) == 1


def test_a_web_price_without_its_source_is_not_a_market_price(db, breakdown):
    _price(db, breakdown, "1", "P", 45, "web_search", "about Rs 45 a metre")
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    assert _line(breakdown, "1").cost_buildup_note.startswith(cbs.BASIS_REFERENCE)


def test_a_web_price_the_agent_cited_itself_is_not_verified(db, breakdown):
    _price(db, breakdown, "1", "P", 45, "web_search", "IndiaMART category page, Rs 45/m",
           "https://dir.indiamart.com/impcat/single-core-cables.html")
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    assert _line(breakdown, "1").cost_buildup_note.startswith(cbs.BASIS_REFERENCE)


def test_the_firms_own_rate_data_counts_as_evidence(db, breakdown):
    _price(db, breakdown, "3", "A", 1700, "training_data", "DRPL rate card 2026 row 41: web plate Rs 1,700")
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    web = _line(breakdown, "3")
    assert web.rate == pytest.approx(1700) and web.cost_buildup_note.startswith(cbs.BASIS_MARKET)
    assert "firm's own rate data" in web.cost_buildup_note


def test_a_row_with_no_published_rate_keeps_its_build_up(db, breakdown):
    ln = _line(breakdown, "3")
    ln.tender_rate = None
    db.commit()
    _price(db, breakdown, "3", "A", 1700, "derived_estimate", "MS 2 kg @ Rs 65/kg + 2 hr @ Rs 700/hr")
    counts = cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    assert _line(breakdown, "3").rate == pytest.approx(1700)
    assert _line(breakdown, "3").cost_buildup_note.startswith(cbs.BASIS_BUILDUP)
    assert counts["buildup"] == 1


def test_the_summary_says_how_each_line_was_costed(db, breakdown):
    _all_priced(db, breakdown)
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    obs = " ".join(cbs.build_strategic_summary(db, breakdown)["key_observations"])
    assert "1 by a cost build-up" in obs
    assert "1 at a verified current market price or the firm's own rate data" in obs
    assert "2 from the railway's own estimate" in obs
    assert "benchmark for margin, not the cost" in obs
    # The replaced rows are counted with the formula rows, not as research.
    assert "2 line item(s)" in obs and "by formula" in obs


def test_the_summary_names_no_railway_rows_when_there_are_none(db, breakdown):
    for code, sched, rate in (("1", "P", 50.0), ("2", "O", 170000.0), ("3", "A", 1500.0), ("4", "P", 900.0)):
        _platform(db, breakdown, code, sched, rate)
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    obs = " ".join(cbs.build_strategic_summary(db, breakdown)["key_observations"])
    assert "4 by a cost build-up" in obs
    assert "railway's own estimate" not in obs
    assert "by formula" not in obs


def test_an_agent_cannot_verify_its_own_web_price(db, breakdown):
    """The costing agent writes "Verified web price" as ordinary prose. Only the
    platform's market research may mark a price verified; the agent's own
    claim is a cited price like any other and is not trusted as one."""
    cbs.merge_batch_rates(db, breakdown.id, [{
        "schedule_name": "P", "item_code": "1", "sr_no": 1, "rate": 45,
        "rate_source": "web_search", "source_ref": "IndiaMART category page",
        "source_url": "https://dir.indiamart.com/impcat/cables.html",
        "cost_buildup_note": f"{cbs.VERIFIED_WEB_PRICE} from an IndiaMART listing, close match",
    }])
    db.expire_all()
    cable = _line(breakdown, "1")
    assert cbs.VERIFIED_WEB_PRICE not in (cable.cost_buildup_note or "")
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    assert _line(breakdown, "1").cost_buildup_note.startswith(cbs.BASIS_REFERENCE)


def test_an_agent_cannot_write_the_platforms_build_up_marker(db, breakdown):
    """Only the platform's build-up may say it is one: from any other caller
    the marker is ordinary text and earns nothing."""
    _price(db, breakdown, "2", "O", 6500, "derived_estimate", "guess",
           note=f"{BUILDUP_MARKER} for one coach: labour Rs 6,500", verified=False)
    db.expire_all()
    ln = _line(breakdown, "2")
    assert not cbs.is_platform_buildup(ln)
    assert BUILDUP_MARKER not in (ln.cost_buildup_note or "")
    cbs.settle_rates_on_evidence(db, breakdown.id, OH, MG)
    db.expire_all()
    # 0.04x and only the agent's word for it: not trusted.
    assert _line(breakdown, "2").cost_buildup_note.startswith(cbs.BASIS_REFERENCE)


def test_the_summary_takes_the_gst_out_of_a_tax_inclusive_schedules_margin(db, breakdown):
    """Schedule O's banner says its rates include all taxes: its tender value
    carries 18% GST, which the table's margin would otherwise count as the
    firm's."""
    from app.models.costing_template import BOQScheduleTotal

    db.add(BOQScheduleTotal(tender_id=breakdown.tender_id, schedule_code="O",
                            title="Cost of Labour: stripping (INCLUSIVE OF ALL TAXES AND CHARGES)"))
    db.commit()
    _platform(db, breakdown, "2", "O", 120000.0)
    db.expire_all()
    obs = " ".join(cbs.build_strategic_summary(db, breakdown, tender_id=breakdown.tender_id)["key_observations"])
    tv = 188800.0 * 37
    tv_ex = tv / 1.18
    gm = tv_ex - 120000.0 * 37
    assert "Schedule(s) O print their rates inclusive of all taxes" in obs
    assert f"Rs {tv_ex:,.0f} without GST" in obs
    assert f"margin of Rs {gm:,.0f} ({gm / tv_ex * 100:.1f}%)" in obs
