"""Market-price research accepts a price only on evidence it can check.

One Claude call with web search per row that can have a market price. A
price is used only when the product found matches the item exactly or
closely, and when its URL is one that call's own search returned -- the
model cannot cite a page it did not see. No network: the client is faked.
"""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from app.models.costing_template import BOQItem
from app.models.tender import Tender
from app.services import cost_breakdown_service as cbs
from app.services.costing import market_price_research as mpr

URL = "https://www.indiamart.com/proddetail/geyser-1l-3kw.html"


def _answer(**kw):
    base = {"found": True, "price_inr": 2400, "per_unit": "piece", "price_per_tender_unit": 2400,
            "url": URL, "product": "Crompton 1 L 3 kW instant geyser", "price_date": "Aug 2026",
            "match": "close", "note": "same capacity and rating"}
    base.update(kw)
    return base


def test_a_close_match_from_a_searched_page_is_accepted():
    ev = mpr._accept(_answer(), {URL})
    assert ev["rate"] == 2400 and ev["url"] == URL and ev["match"] == "close"


@pytest.mark.parametrize("answer, urls", [
    (_answer(match="generic"), {URL}),               # the same kind of product is not the item
    (_answer(match="none"), {URL}),
    (_answer(found=False), {URL}),
    (_answer(), {"https://elsewhere.example/x"}),     # a page the search never returned
    (_answer(url=""), {URL}),
    (_answer(price_per_tender_unit=None), {URL}),
    (_answer(price_per_tender_unit=0), {URL}),
    (None, {URL}),
])
def test_anything_less_is_refused(answer, urls):
    assert mpr._accept(answer, urls) is None


def test_the_answer_is_read_from_a_fenced_block_after_prose():
    text = "I searched three sellers.\n```json\n" + json.dumps(_answer()) + "\n```"
    assert mpr._parse(text)["url"] == URL
    assert mpr._parse("no json here") is None


@pytest.mark.parametrize("desc, skip", [
    ("Web to Drg No.LE11317", True),
    ("Door Arrangements as per RCF's Drg. No. LA51100", True),
    ("Labour cost for installation of cable transit entry sealing", True),
    ("Fitting & Repairing Charges.", True),
    ("Material cost for Dismantling and stripping of old SBC", True),
    ("instant Water Heater(Geyser) Capacity - 1 Litre, 3 KW", False),
    ("LED LIGHT FITTING FOR TOILET INDICATION IN LHB AC COACHES", False),
    ("Thin walled Flexible Elastomeric cable 4.0 Sq mm", False),
])
def test_rows_no_shop_sells_are_not_searched(desc, skip):
    assert mpr.not_a_market_item({"description": desc}) is skip


def test_components_and_tax_lines_are_not_searched():
    assert mpr.not_a_market_item({"description": "Geyser 1 L", "component_of": "A-1"})
    assert mpr.not_a_market_item({"description": "GST", "is_tax_line": True})


class _Client:
    def __init__(self, api_key=None):
        self.messages = self

    async def create(self, **kw):
        text = kw["messages"][0]["content"]
        found = "Geyser" in text
        return NS(
            usage=NS(input_tokens=1, output_tokens=1),
            content=[
                NS(type="web_search_tool_result", content=[NS(url=URL, title="t", page_age=None)]),
                NS(type="text", text=json.dumps(_answer() if found else _answer(found=False))),
            ],
        )


def test_research_prices_what_it_finds_and_skips_the_rest(monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)
    monkeypatch.setattr("app.services.ai_service._log_usage", lambda *a, **k: None)
    rows = [
        {"boq_item_id": 1, "description": "instant Water Heater(Geyser) 1 L 3 KW", "unit": "Numbers"},
        {"boq_item_id": 2, "description": "Copper crimping socket 25 sq mm EDTS 200", "unit": "Numbers"},
        {"boq_item_id": 3, "description": "Web to Drg No.LE11317", "unit": "Numbers"},
    ]
    found = asyncio.run(mpr.research_market_prices(rows, api_key="k", model="m", concurrency=2))
    assert set(found) == {1}
    line = mpr.as_batch_lines(found)[0]
    assert line["rate_source"] == "web_search" and line["source_url"] == URL
    assert line["boq_item_id"] == 1 and line["rate"] == 2400


def test_the_merged_price_is_market_evidence(db):
    tid = 960001
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="t", source_url="x"))
    item = BOQItem(tender_id=tid, sr_no=47, item_code="47", schedule_name="P", quantity=37,
                   unit="Numbers", estimated_rate=2537.0, description="Geyser 1 L 3 kW")
    db.add(item)
    db.commit()
    bd = cbs.build_skeleton_from_boq(db, tid)
    ev = mpr._accept(_answer(), {URL})
    out = cbs.merge_batch_rates(db, bd.id, mpr.as_batch_lines({item.id: ev}),
                                platform_verified=True)
    assert out == {"matched": 1, "unmatched": 0}
    cbs.settle_rates_on_evidence(db, bd.id, 10, 15)
    db.expire_all()
    ln = bd.lines[0]
    assert ln.rate == pytest.approx(2400) and ln.cost_buildup_note.startswith(cbs.BASIS_MARKET)
