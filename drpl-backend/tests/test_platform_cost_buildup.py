"""The platform builds up its own cost for a schedule row.

Sahil's report, twice: "the rate which platform used is the same railway
rate". The Mid-Life NIT's 286 rows came back at the NIT rate times 0.77, then
at the NIT rate divided by 1.25. The costing agent had priced them blind --
sixty rows a call, no schedule banners, no wages, no current prices -- and its
figures ran 0.15x-6.45x of the railway's, so settlement replaced them with the
railway's own number.

costing/cost_buildup.py builds each row the way an estimator does: the
schedule banner says whether the row is material or labour, a verified rate
basis gives wages and material prices, a strong model gives the quantities,
weights and hours for a few rows at a time, and the platform does the sums.
The railway's rate is withheld from the model and used only to catch a
misread row (a second look), never as the figure.
"""
import asyncio
import itertools
import re
import time
from types import SimpleNamespace

import pytest

from app.models.costing_template import BOQItem
from app.models.tender import Tender
from app.services import cost_breakdown_service as cbs
from app.services.costing import cost_buildup as cb
from app.services.costing import schedule_context as sc

_ids = itertools.count(990000)


# ── the platform's arithmetic ─────────────────────────────────────────────────


def _basis() -> cb.RateBasis:
    b = cb.fallback_basis(30.0)
    b.materials["ms_steel"] = cb.MaterialPrice(
        "ms_steel", "Mild steel plates and rolled sections, IS 2062 E250", "kg", 60.0,
        "https://steel.example/price-list", "Sep 2026", True,
    )
    return b


_ROW = {"boq_item_id": 7, "unit": "Numbers", "description": "Cross member", "schedule_name": "A"}


def _answer(**over):
    a = {
        "boq_item_id": 7, "work_type": "material_supply", "one_unit_is": "one cross member",
        "materials": [
            {"item": "MS plate 8 mm", "quantity": 10, "unit": "kg", "unit_price_inr": 80,
             "price_basis": "basis:ms_steel"},
            {"item": "M12 bolts", "quantity": 4, "unit": "nos", "unit_price_inr": 12.5,
             "price_basis": "https://shop.example/m12-bolt"},
        ],
        "labour": [
            {"skill": "skilled", "hours": 2, "task": "weld"},
            {"skill": "unskilled", "hours": 3, "task": "handle"},
        ],
        "other_costs": [{"item": "consumables", "amount_inr": 40}],
        "assumptions": "8 mm plate, 0.6 x 0.25 m", "confidence": "medium",
    }
    a.update(over)
    return a


def test_the_platform_does_the_arithmetic_on_verified_prices():
    basis = _basis()
    p = cb.price_buildup(_ROW, _answer(), basis, {"https://shop.example/m12-bolt"})
    skilled = 1008 * 1.30 / 8
    unskilled = 827 * 1.30 / 8
    # The rate-basis price replaces the model's Rs 80/kg; the cited bolt page
    # is one the search returned.
    materials = 10 * 60.0 + 4 * 12.5
    labour = 2 * round(skilled, 2) + 3 * round(unskilled, 2)
    assert p.materials_total == pytest.approx(materials)
    assert p.labour_total == pytest.approx(labour, abs=0.02)
    assert p.unit_cost == pytest.approx(materials + labour + 40, abs=0.02)
    assert p.note.startswith(cb.BUILDUP_MARKER)
    assert "(rate basis)" in p.note and "(market price)" in p.note
    assert "https://steel.example/price-list" in p.source_ref
    assert "https://shop.example/m12-bolt" in p.source_ref
    assert "30% statutory" in p.note


def test_a_page_the_search_did_not_return_is_an_estimate():
    p = cb.price_buildup(_ROW, _answer(), _basis(), searched_urls=set())
    assert "M12 bolts @ Rs 12.50 = Rs 50.00 (estimated price)" in p.note
    assert "shop.example" not in p.source_ref


def test_a_basis_price_for_another_unit_is_not_forced_onto_the_row():
    ans = _answer(materials=[{"item": "MS sheet", "quantity": 2, "unit": "sheet",
                              "unit_price_inr": 900, "price_basis": "basis:ms_steel"}],
                  labour=[], other_costs=[])
    p = cb.price_buildup(_ROW, ans, _basis(), set())
    assert p.unit_cost == pytest.approx(1800)


def test_a_build_up_that_prices_nothing_is_no_build_up():
    ans = _answer(materials=[], labour=[], other_costs=[])
    assert cb.price_buildup(_ROW, ans, _basis(), set()) is None


def test_impossible_entries_are_dropped_not_summed():
    ans = _answer(
        materials=[{"item": "x", "quantity": -3, "unit": "kg", "unit_price_inr": 60, "price_basis": "estimate"},
                   {"item": "y", "quantity": 1, "unit": "kg", "unit_price_inr": float("nan"), "price_basis": "estimate"}],
        labour=[{"skill": "skilled", "hours": 1e9, "task": "?"}, {"skill": "wizard", "hours": 5, "task": "?"}],
        other_costs=[{"item": "consumables", "amount_inr": 25}],
    )
    p = cb.price_buildup(_ROW, ans, _basis(), set())
    assert p.unit_cost == pytest.approx(25)


def test_units_are_compared_as_words():
    assert cb._unit("per kg") == cb._unit("Rs/kg") == cb._unit("KGS.") == "kg"
    assert cb._unit("sq. m") == cb._unit("m2") == "sqm"
    assert cb._unit("sheet") != "kg"


# ── the railway's figure checks the build-up; it never becomes it ─────────────


def test_the_benchmark_strips_overhead_margin_and_included_gst():
    assert cb.benchmark_cost(1250, overhead_pct=10, margin_pct=15) == pytest.approx(1000)
    assert cb.benchmark_cost(1475, overhead_pct=10, margin_pct=15, gst_pct=18,
                             taxes_inclusive=True) == pytest.approx(1000)
    assert cb.benchmark_cost(1475, overhead_pct=10, margin_pct=15, gst_pct=18,
                             taxes_inclusive=None) == pytest.approx(1180)
    assert cb.benchmark_cost(None, overhead_pct=10, margin_pct=15) is None
    assert cb.benchmark_cost(0, overhead_pct=10, margin_pct=15) is None


def _p(cost, ratio):
    return cb.PricedBuildup(1, cost, 0, cost, 0, f"{cb.BUILDUP_MARKER}", "", "medium", ratio=ratio)


def test_a_second_look_inside_the_band_stands():
    first, second = _p(6500, 0.05), _p(140000, 1.1)
    assert cb.choose(first, second) is second and second.first_cost == 6500


def test_of_two_outside_the_band_the_nearer_stands():
    first, second = _p(300, 0.3), _p(2500, 2.5)
    assert cb.choose(first, second) is second
    first, second = _p(300, 0.45), _p(9000, 9.0)
    assert cb.choose(first, second) is first and first.first_cost is None


# ── the rate basis: kept only when its page was really returned ───────────────


class _FakeMessages:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    async def create(self, **params):
        self.calls.append(params)
        out = self.handler(params)
        if isinstance(out, Exception):
            raise out
        return out


class _FakeClient:
    def __init__(self, handler):
        self.messages = _FakeMessages(handler)
        self.beta = SimpleNamespace(messages=self.messages)


def _resp(tool_name, tool_input, urls=()):
    content = []
    if urls:
        content.append(SimpleNamespace(type="web_search_tool_result",
                                       content=[SimpleNamespace(url=u) for u in urls]))
    content.append(SimpleNamespace(type="tool_use", name=tool_name, input=tool_input))
    usage = SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, server_tool_use=None)
    return SimpleNamespace(content=content, stop_reason="tool_use", usage=usage)


_CLC = "https://clc.gov.in/clc/sites/default/files/vda-apr-2026.pdf"
_FAMILIES = [("ms_steel", "Mild steel", "kg"), ("aluminium_extrusion", "Aluminium", "kg"),
             ("epdm_rubber", "EPDM", "kg")]


def _basis_answer(**over):
    a = {
        "work_location": "Howrah, West Bengal", "wage_area": "Area A (Kolkata)",
        "wages": [{"skill": s, "daily_wage_inr": w, "source_url": _CLC, "effective": "01-04-2026"}
                  for s, w in (("unskilled", 827), ("semi_skilled", 918), ("skilled", 1008),
                               ("highly_skilled", 1094))],
        "materials": [
            {"key": "ms_steel", "price_inr": 58.5, "unit": "per kg",
             "source_url": "https://steel.example/list", "as_of": "Sep 2026"},
            {"key": "aluminium_extrusion", "price_inr": 330, "unit": "kg",
             "source_url": "https://not-searched.example/al", "as_of": "Sep 2026"},
            {"key": "epdm_rubber", "price_inr": 320000, "unit": "kg",   # a per-tonne figure
             "source_url": "https://steel.example/list", "as_of": "Sep 2026"},
        ],
    }
    a.update(over)
    return a


def test_the_rate_basis_keeps_only_what_its_search_returned():
    client = _FakeClient(lambda p: _resp("submit_rate_basis", _basis_answer(),
                                         urls=[_CLC, "https://steel.example/list"]))
    basis = asyncio.run(cb.research_rate_basis(
        client, "claude-sonnet-5", context="Mid-Life Rehabilitation, Liluah", families=_FAMILIES,
        loading_pct=30.0, deadline=time.monotonic() + 60,
    ))
    assert all(r.verified for r in basis.labour.values())
    assert basis.labour["skilled"].hourly_cost == pytest.approx(round(1008 * 1.3 / 8, 2))
    assert _CLC in basis.wages_source and "Area A" in basis.wages_source
    assert set(basis.materials) == {"ms_steel"}  # unsearched page and per-tonne figure dropped
    assert basis.materials["ms_steel"].price == pytest.approx(58.5)
    params = client.messages.calls[0]
    assert params["thinking"] == {"type": "adaptive"}
    assert any(t.get("type") == "web_search_20260209" for t in params["tools"])
    assert "temperature" not in params


def test_wages_that_cannot_all_be_verified_fall_back_to_the_central_notification():
    ans = _basis_answer()
    ans["wages"][2]["source_url"] = "https://invented.example/wages"
    client = _FakeClient(lambda p: _resp("submit_rate_basis", ans, urls=[_CLC]))
    basis = asyncio.run(cb.research_rate_basis(
        client, "claude-sonnet-5", context="x", families=_FAMILIES, loading_pct=30.0,
        deadline=time.monotonic() + 60,
    ))
    assert {s: r.daily_wage for s, r in basis.labour.items()} == cb.FALLBACK_WAGES
    assert not any(r.verified for r in basis.labour.values())


def test_a_failed_basis_research_is_the_fallback_basis_not_an_error():
    client = _FakeClient(lambda p: RuntimeError("overloaded"))
    basis = asyncio.run(cb.research_rate_basis(
        client, "claude-sonnet-5", context="x", families=_FAMILIES, loading_pct=30.0,
        deadline=time.monotonic() + 60,
    ))
    assert basis.labour["unskilled"].daily_wage == 827.0 and not basis.materials


def test_opus_calls_opt_into_refusal_fallbacks_and_drop_them_if_refused():
    seen = []

    def handler(p):
        seen.append(dict(p))
        if "fallbacks" in p:
            return RuntimeError("fallbacks is not enabled for this organization")
        return _resp("submit_rate_basis", _basis_answer(), urls=[_CLC, "https://steel.example/list"])

    client = _FakeClient(handler)
    basis = asyncio.run(cb.research_rate_basis(
        client, "claude-opus-5", context="x", families=_FAMILIES, loading_pct=30.0,
        deadline=time.monotonic() + 60,
    ))
    assert seen[0]["fallbacks"] == "default" and seen[0]["betas"] == ["server-side-fallback-2026-07-01"]
    assert "fallbacks" not in seen[1]
    assert basis.labour["skilled"].verified


# ── end to end: build up, check, look again, merge ────────────────────────────


@pytest.fixture
def midlife(db):
    tid = next(_ids)
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="Mid-Life", source_url="x"))
    db.add_all([
        BOQItem(tender_id=tid, sr_no=2, item_code="2", schedule_name="O", quantity=37,
                unit="Per Coach", estimated_rate=188800.0,
                description="Fitting & Repairing Charges."),
        BOQItem(tender_id=tid, sr_no=10, item_code="10", schedule_name="A", quantity=43,
                unit="Numbers", estimated_rate=279.66, description="WEB to Drg. No. LE11185, Alt.-a"),
    ])
    db.commit()
    bd = cbs.build_skeleton_from_boq(db, tid)
    rows = [
        {"boq_item_id": ln.boq_item_id, "schedule_name": ln.schedule_name, "sr_no": ln.sr_no,
         "description": ln.description, "unit": ln.unit, "quantity": ln.quantity}
        for ln in bd.lines
    ]
    return tid, bd, rows


_SCHEDULES = {
    "O": sc.ScheduleContext("O", "Cost of Labour: Mid Life Rehabilitation of LHB Coaches (For stripping "
                                 "of Coaches) (INCLUSIVE OF ALL TAXES AND CHARGES)", sc.LABOUR, True),
    "A": sc.ScheduleContext("A", "(Mechanical )Cost of Material: Mid Life Rehabilitation of LHB Coaches "
                                 "(INCLUSIVE OF ALL TAXES AND CHARGES)", sc.MATERIAL, True),
}


def _user_text(params) -> str:
    return " ".join(b["text"] for b in params["messages"][0]["content"])


def _buildup_handler(prompts: list):
    def handler(params):
        text = _user_text(params)
        prompts.append(text)
        rows = []
        for bid in [int(x) for x in re.findall(r"boq_item_id (\d+) \|", text)]:
            if "Fitting" in text.split(f"boq_item_id {bid} |", 1)[1][:400]:
                hours = (700, 300) if "SECOND LOOK" in text else (40, 0)
                rows.append({
                    "boq_item_id": bid, "work_type": "labour_only", "one_unit_is": "all fitting on one coach",
                    "materials": [], "labour": [{"skill": "skilled", "hours": hours[0], "task": "fit"},
                                                {"skill": "unskilled", "hours": hours[1], "task": "help"}],
                    "other_costs": [{"item": "consumables", "amount_inr": 500}],
                    "assumptions": "fitting of all refurbished assemblies", "confidence": "medium",
                })
            else:
                rows.append({
                    "boq_item_id": bid, "work_type": "material_supply", "one_unit_is": "one web plate",
                    "materials": [{"item": "corrosion resistant steel plate", "quantity": 2.2, "unit": "kg",
                                   "unit_price_inr": 75, "price_basis": "estimate"}],
                    "labour": [{"skill": "semi_skilled", "hours": 0.25, "task": "cut"}],
                    "other_costs": [], "assumptions": "5 mm x 250 x 220", "confidence": "medium",
                })
        return _resp("submit_cost_buildups", {"rows": rows})
    return handler


def _run(db, bd, rows, monkeypatch, prompts, deadline_s=600.0, handler=None):
    client = _FakeClient(handler or _buildup_handler(prompts))
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda **kw: client)
    return asyncio.run(cb.run_platform_buildup(
        rows, breakdown_id=bd.id, tender_id=bd.tender_id, tender_context="Mid-Life Rehabilitation",
        schedules=_SCHEDULES, published={ln.boq_item_id: ln.tender_rate for ln in bd.lines},
        api_key="sk-test", settings=cb.BuildupSettings(model="claude-sonnet-5", concurrency=2),
        overhead_pct=10, margin_pct=15, gst_pct=18, deadline=time.monotonic() + deadline_s,
        basis=cb.fallback_basis(30.0),
    )), client


def test_rows_are_built_up_checked_and_looked_at_again(db, midlife, monkeypatch):
    _tid, bd, rows = midlife
    prompts: list = []
    stats, client = _run(db, bd, rows, monkeypatch, prompts)
    assert stats["priced"] == 2 and stats["failed"] == 0
    assert stats["second_looks"] == 1          # 40 hours a coach is 0.05x: looked at again
    db.expire_all()
    by_code = {ln.item_code: ln for ln in bd.lines}

    fitting = by_code["2"]
    skilled, unskilled = round(1008 * 1.3 / 8, 2), round(827 * 1.3 / 8, 2)
    assert fitting.rate == pytest.approx(700 * skilled + 300 * unskilled + 500, abs=0.05)
    assert cbs.is_platform_buildup(fitting)
    assert "Re-derived: a first build-up of" in fitting.cost_buildup_note

    web = by_code["10"]
    assert web.rate == pytest.approx(2.2 * 75 + 0.25 * round(918 * 1.3 / 8, 2), abs=0.02)
    assert cbs.is_platform_buildup(web) and "Re-derived" not in web.cost_buildup_note

    # The second look was told the direction only, and no call ever saw the
    # railway's rate.
    second = [p for p in prompts if "SECOND LOOK" in p]
    assert len(second) == 1 and "FAR BELOW" in second[0]
    for p in prompts:
        assert "188800" not in p and "188,800" not in p and "279.66" not in p
    # Each call reads its schedule's banner and what it prices.
    assert any("SCHEDULE O: Cost of Labour" in p and "the cost of labour" in p for p in prompts)


def test_a_build_up_with_no_time_left_writes_nothing(db, midlife, monkeypatch):
    _tid, bd, rows = midlife
    stats, _client = _run(db, bd, rows, monkeypatch, [], deadline_s=30.0)
    assert stats["priced"] == 0
    db.expire_all()
    assert not any(cbs.is_platform_buildup(ln) for ln in bd.lines)


def test_a_failed_call_is_retried_once_and_then_left_for_the_fallback(db, midlife, monkeypatch):
    _tid, bd, rows = midlife
    calls = []

    def handler(params):
        calls.append(1)
        return RuntimeError("upstream 529")

    stats, _client = _run(db, bd, rows, monkeypatch, [], handler=handler)
    assert stats["priced"] == 0 and stats["failed"] == 2
    assert len(calls) == 4  # two schedules, one retry each


# ── market prices: a far listing or the firm's own rate is not overwritten ────


def test_a_listing_far_from_the_railway_does_not_replace_the_build_up(db, midlife, monkeypatch):
    from app.services.langchain.graphs import enhanced_costing_agent as eca

    _tid, bd, rows = midlife
    stats, _client = _run(db, bd, rows, monkeypatch, [])
    db.expire_all()
    by_code = {ln.item_code: ln for ln in bd.lines}
    web_bid = by_code["10"].boq_item_id
    built = by_code["10"].rate

    async def go(found):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(found)
        await eca._merge_market_research(fut, bd.id, 5.0, bd.tender_id, schedules=_SCHEDULES,
                                         defaults={"overhead_percent": 10, "margin_percent": 15,
                                                   "gst_percent": 18})

    far = {web_bid: {"rate": 2400.0, "url": "https://shop.example/generic-plate", "product": "plate",
                     "price_date": "", "match": "close", "note": "", "listed": None, "per_unit": ""}}
    asyncio.run(go(far))
    db.expire_all()
    assert {ln.item_code: ln for ln in bd.lines}["10"].rate == pytest.approx(built)

    near = {web_bid: dict(far[web_bid], rate=205.0)}
    asyncio.run(go(near))
    db.expire_all()
    web = {ln.item_code: ln for ln in bd.lines}["10"]
    assert web.rate == pytest.approx(205.0) and web.rate_source == "web_search"


def test_the_firms_own_rate_is_not_overwritten_by_a_listing(db, midlife):
    from app.services.langchain.graphs import enhanced_costing_agent as eca

    _tid, bd, _rows = midlife
    cbs.merge_batch_rates(db, bd.id, [{"schedule_name": "A", "item_code": "10", "sr_no": 10,
                                       "rate": 190.0, "rate_source": "training_data",
                                       "source_ref": "firm rate card 2026 row 12"}])
    db.expire_all()
    bid = {ln.item_code: ln for ln in bd.lines}["10"].boq_item_id

    async def go():
        fut = asyncio.get_running_loop().create_future()
        fut.set_result({bid: {"rate": 205.0, "url": "https://shop.example/p", "product": "plate",
                              "price_date": "", "match": "exact", "note": "", "listed": None,
                              "per_unit": ""}})
        await eca._merge_market_research(fut, bd.id, 5.0, bd.tender_id, schedules=_SCHEDULES)

    asyncio.run(go())
    db.expire_all()
    web = {ln.item_code: ln for ln in bd.lines}["10"]
    assert web.rate == pytest.approx(190.0) and web.rate_source == "training_data"


def test_the_copied_rate_guard_leaves_a_build_up_equal_to_the_published_rate(db, midlife):
    _tid, bd, _rows = midlife
    web = {ln.item_code: ln for ln in bd.lines}["10"]
    cbs.merge_batch_rates(db, bd.id, cb.as_batch_lines([cb.PricedBuildup(
        web.boq_item_id, 279.66, 200.0, 79.66, 0.0, f"{cb.BUILDUP_MARKER} for one Numbers: ...",
        "Platform build-up", "medium")]), platform_verified=True)
    cbs.normalize_copied_rates(db, bd.id, 10, 15)
    db.expire_all()
    web = {ln.item_code: ln for ln in bd.lines}["10"]
    assert web.rate == pytest.approx(279.66) and cbs.is_platform_buildup(web)


def test_the_build_up_context_keeps_the_tenders_identity_out(db, midlife):
    from app.services.langchain.graphs import enhanced_costing_agent as eca

    tid, _bd, _rows = midlife
    text = eca._buildup_tender_context(
        db, tid, {"summary": "Mid-life rehabilitation at the workshop, NIT No LRC-3-CT_MLR_RSP_T."},
        _SCHEDULES, {"[TENDER_REF_1]": "LRC-3-CT_MLR_RSP_T"},
    )
    assert "LRC-3-CT_MLR_RSP_T" not in text and "[TENDER_REF_1]" in text
    assert "O: Cost of Labour" in text and "Name of work: Mid-Life" in text


def test_the_same_item_twice_in_a_schedule_is_built_up_once_at_one_figure(db, monkeypatch):
    """Schedule A lists "Door Arrangements R.H and L.H ... LA51100" twice at
    Rs 61,360 each; two independent build-ups would price them differently."""
    tid = next(_ids)
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="Mid-Life", source_url="x"))
    desc = "Door Arrangements R.H and L.H for LHB Coaches as per RCF's Drg. No. LA51100, Alt. No.- b"
    db.add_all([
        BOQItem(tender_id=tid, sr_no=22, item_code="22", schedule_name="A", quantity=37, unit="Set",
                estimated_rate=61360.0, description=desc),
        BOQItem(tender_id=tid, sr_no=42, item_code="42", schedule_name="A", quantity=37, unit="Set",
                estimated_rate=61360.0, description=desc.replace("RCF's", "RCFs") + "."),
    ])
    db.commit()
    bd = cbs.build_skeleton_from_boq(db, tid)
    rows = [{"boq_item_id": ln.boq_item_id, "schedule_name": "A", "sr_no": ln.sr_no,
             "description": ln.description, "unit": ln.unit, "quantity": ln.quantity} for ln in bd.lines]
    asked: list[int] = []

    def handler(params):
        ids = [int(x) for x in re.findall(r"boq_item_id (\d+) \|", _user_text(params))]
        asked.extend(ids)
        return _resp("submit_cost_buildups", {"rows": [{
            "boq_item_id": i, "work_type": "material_supply", "one_unit_is": "one door set",
            "materials": [{"item": "door set", "quantity": 1, "unit": "set", "unit_price_inr": 42000,
                           "price_basis": "estimate"}],
            "labour": [], "other_costs": [], "assumptions": "", "confidence": "medium"} for i in ids]})

    stats, _client = _run(db, bd, rows, monkeypatch, [], handler=handler)
    assert len(asked) == 1 and stats["priced"] == 2
    db.expire_all()
    rates = {ln.item_code: ln.rate for ln in bd.lines}
    assert rates["22"] == rates["42"] == pytest.approx(42000)
    assert "same item as Sr 22" in {ln.item_code: ln for ln in bd.lines}["42"].cost_buildup_note


def test_the_costing_reply_states_each_basis_without_asking_for_a_review():
    from app.services.langchain.graphs.chat_agent_wrappers import _format_costing_output

    def item(sr, basis, source="derived_estimate"):
        return {"sr_no": sr, "description": f"row {sr}", "quantity": 1, "unit": "Nos", "rate": 100.0,
                "amount": 100.0, "tender_rate": 150.0, "tender_amount": 150.0, "rate_source": source,
                "cost_buildup_note": f"{basis} -- words.", "schedule_name": "A"}

    out = _format_costing_output({"line_items": [
        item(1, cbs.BASIS_BUILDUP), item(2, cbs.BASIS_BUILDUP),
        item(3, cbs.BASIS_MARKET, "web_search"),
    ]}, tender_id=1)
    assert "2 item(s) costed by a build-up" in out
    assert "1 item(s) at a verified market price" in out
    assert "railway's estimate" not in out
    assert "please verify" not in out.lower() and "before submitting" not in out.lower()


def test_the_workbook_source_column_is_in_words_and_the_rows_keep_their_codes(tmp_path):
    import openpyxl

    from app.services.langchain.tools.xlsx_generator_tool import _source_words, build_cost_xlsx

    assert _source_words({"rate_source": "derived_estimate",
                          "cost_buildup_note": f"{cbs.BASIS_BUILDUP} -- ..."}) == "Cost build-up"
    assert _source_words({"rate_source": "derived_estimate",
                          "cost_buildup_note": f"{cbs.BASIS_REFERENCE} -- ..."}) == "Railway estimate (fallback)"
    assert _source_words({"rate_source": "web_search"}) == "Market price (web)"
    assert _source_words({"rate_source": "something_new"}) == "something_new"

    rows = [{"sr_no": 1, "item_code": "1", "schedule_name": "A", "boq_item_id": 1,
             "description": "Cross member", "qty": 2, "quantity": 2, "unit": "Nos", "rate": 900.0,
             "amount": 1800.0, "tender_rate": 1200.0, "tender_amount": 2400.0,
             "rate_source": "derived_estimate", "cost_buildup_note": f"{cbs.BASIS_BUILDUP} -- build-up"}]
    path = tmp_path / "w.xlsx"
    build_cost_xlsx(str(path), "t", rows, layout="margin_analysis")
    assert rows[0]["rate_source"] == "derived_estimate"
    cells = {str(c.value) for ws in openpyxl.load_workbook(path).worksheets for r in ws.iter_rows() for c in r}
    assert "Cost build-up" in cells and "derived_estimate" not in cells


def test_a_model_the_account_cannot_use_falls_to_the_next_not_to_the_railway(db, midlife, monkeypatch):
    """An account without the configured model would otherwise lose every
    build-up and put every row back on the railway's figure."""
    import anthropic
    import httpx

    _tid, bd, rows = midlife
    prompts: list = []
    good = _buildup_handler(prompts)
    models: list[str] = []

    def handler(params):
        models.append(params["model"])
        if params["model"] == "claude-opus-5":
            req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            return anthropic.NotFoundError("model: claude-opus-5", response=httpx.Response(404, request=req),
                                           body=None)
        return good(params)

    client = _FakeClient(handler)
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda **kw: client)
    stats = asyncio.run(cb.run_platform_buildup(
        rows, breakdown_id=bd.id, tender_id=bd.tender_id, tender_context="x", schedules=_SCHEDULES,
        published={ln.boq_item_id: ln.tender_rate for ln in bd.lines}, api_key="sk-test",
        settings=cb.BuildupSettings(model="claude-opus-5", concurrency=1), overhead_pct=10,
        margin_pct=15, gst_pct=18, deadline=time.monotonic() + 600, basis=cb.fallback_basis(30.0),
    ))
    assert stats["priced"] == 2
    assert "claude-sonnet-5" in models
    assert models.count("claude-opus-5") == 1   # switched once, for the rest of the run
    assert cb.model_chain("claude-opus-5") == ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
    assert cb.model_chain("claude-sonnet-5")[0] == "claude-sonnet-5"


def test_a_refused_request_feature_is_dropped_and_the_call_sent_again():
    """A 400 naming a feature (strict schemas, the newer web-search tool,
    effort, thinking) must not cost every row its build-up."""
    import anthropic
    import httpx

    sent = []

    def bad(msg):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        return anthropic.BadRequestError(msg, response=httpx.Response(400, request=req), body=None)

    def handler(params):
        sent.append(params)
        tools = params["tools"]
        if any(t.get("strict") for t in tools):
            return bad("tools.1.strict: strict mode is not supported with this configuration")
        if any(t.get("type") == "web_search_20260209" for t in tools):
            return bad("tools.0.type: web_search_20260209 is not available for this model")
        return _resp("submit_rate_basis", _basis_answer(), urls=[_CLC, "https://steel.example/list"])

    client = _FakeClient(handler)
    basis = asyncio.run(cb.research_rate_basis(
        client, "claude-sonnet-5", context="x", families=_FAMILIES, loading_pct=30.0,
        deadline=time.monotonic() + 60,
    ))
    assert basis.labour["skilled"].verified
    assert len(sent) == 3
    assert not any(t.get("strict") for t in sent[-1]["tools"])
    assert any(t.get("type") == "web_search_20250305" for t in sent[-1]["tools"])


def test_no_credit_is_not_a_feature_to_drop():
    import anthropic
    import httpx

    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    e = anthropic.BadRequestError("Your credit balance is too low to access the Anthropic API",
                                  response=httpx.Response(400, request=req), body=None)
    opts = {"fallbacks": True, "thinking": True, "effort": True, "strict": True, "search_type": None}
    assert cb._drop_refused_feature(e, opts) is None and opts["strict"] is True
