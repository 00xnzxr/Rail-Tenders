"""Costing research spends less and says less, for the same evidence.

- The anonymizer map is built from the tender's records, not an LLM call.
- Market research strips the tender's names from what the search model
  sees, remembers recent answers, and meters its search fees.
- Outbound fetches never reach a non-public address.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace as NS

import pytest

from app.models.tender import Tender
from app.services.costing import anonymization as anon
from app.services.costing import market_price_research as mpr

URL = "https://www.indiamart.com/proddetail/geyser-1l-3kw.html"


# ── anonymization map ────────────────────────────────────────────────────────


def test_labelled_tender_references_are_found_and_drawings_are_not():
    text = ("Tender No. RCT/2024-25/0178; drawing ICF/SK-3-6-002; NIT No: 05241234/2026; "
            "Bid Number: GEM/2025/B/6512345")
    refs = anon.reference_numbers(text)
    assert "RCT/2024-25/0178" in refs
    assert "05241234/2026" in refs
    assert "GEM/2025/B/6512345" in refs
    assert not any("SK-3" in r for r in refs)


def test_the_map_comes_from_the_tender_row(db):
    t = Tender(portal="ireps", tender_id=f"ER-LLH-{uuid.uuid4().hex[:6]}/2026",
               title="Supply of LED lights", organisation="Eastern Railway Liluah Workshop",
               department="Mechanical", buyer_contact_email="buyer@example.gov.in")
    db.add(t)
    db.commit()
    try:
        m = anon.build_anonymization_map(db, t.id, {"notes": "Tender No. RCT/2024-25/0178"})
        originals = set(m.values())
        assert t.tender_id in originals
        assert "Eastern Railway Liluah Workshop" in originals
        assert "buyer@example.gov.in" in originals
        assert "RCT/2024-25/0178" in originals
        assert {"DRPL", "DRPL Manufacturing"} <= originals
        # A one-word department is a search term, and the title is the item.
        assert "Mechanical" not in originals
        assert "Supply of LED lights" not in originals
    finally:
        db.delete(t)
        db.commit()


def test_longer_names_are_replaced_before_the_names_they_contain():
    m = anon.build_anonymization_map(None, None, {}, extra=["Eastern Railway", "Eastern Railway Liluah"])
    order = list(m.values())
    assert order.index("Eastern Railway Liluah") < order.index("Eastern Railway")
    assert order.index("DRPL Manufacturing") < order.index("DRPL")


def test_the_costing_node_makes_no_model_call_for_the_map(db, monkeypatch):
    from app.services.langchain.graphs import enhanced_costing_agent as eca

    def _boom(*a, **k):
        raise AssertionError("the anonymizer called a model")

    monkeypatch.setattr(eca, "get_chat_model", _boom)
    out = asyncio.run(eca.load_training_context_node(
        {"tender_id": None, "analysis_result": {"x": "Tender No. RCT/2024-25/0178"}}, db))
    assert "RCT/2024-25/0178" in out["anonymization_map"].values()


# ── market research ──────────────────────────────────────────────────────────


def test_the_search_model_never_sees_the_tenders_names():
    q = mpr._query(
        {"description": "LED light fitting for Eastern Railway Liluah coaches", "unit": "Nos"},
        {"[ENTITY-1]": "Eastern Railway Liluah"},
    )
    assert "Eastern Railway Liluah" not in q
    assert "[ENTITY-1]" in q and "LED light fitting" in q


def test_part_numbers_survive_anonymization():
    """Entity substitution only -- no phone/PAN regex over a product text."""
    q = mpr._query({"description": "Relay part 9876543210 ABCDE1234F", "unit": "Nos"}, {})
    assert "9876543210" in q and "ABCDE1234F" in q


class _Client:
    calls = 0

    def __init__(self, api_key=None):
        self.messages = self

    async def create(self, **kw):
        type(self).calls += 1
        text = kw["messages"][0]["content"]
        found = "Geyser" in text
        answer = {"found": found, "price_inr": 2400, "per_unit": "piece",
                  "price_per_tender_unit": 2400 if found else None, "url": URL,
                  "product": "p", "price_date": "", "match": "close" if found else "none",
                  "note": ""}
        return NS(
            usage=NS(input_tokens=1, output_tokens=1,
                     server_tool_use=NS(web_search_requests=3)),
            content=[
                NS(type="web_search_tool_result", content=[NS(url=URL)]),
                NS(type="text", text=json.dumps(answer)),
            ],
        )


def _rows(tag):
    return [
        {"boq_item_id": 1, "description": f"instant Water Heater(Geyser) 1 L 3 KW {tag}", "unit": "Nos"},
        {"boq_item_id": 2, "description": f"EDTS 200 crimping socket {tag}", "unit": "Nos"},
    ]


def test_recent_research_is_reused_found_or_not(monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)
    monkeypatch.setattr("app.services.ai_service._log_usage", lambda *a, **k: None)
    tag = uuid.uuid4().hex
    _Client.calls = 0
    first = asyncio.run(mpr.research_market_prices(_rows(tag), api_key="k", model="m",
                                                   cache_ttl_days=14))
    assert set(first) == {1} and _Client.calls == 2
    second = asyncio.run(mpr.research_market_prices(_rows(tag), api_key="k", model="m",
                                                    cache_ttl_days=14))
    assert second == first
    assert _Client.calls == 2, "a recent answer was searched again"


def test_no_cache_means_every_run_searches(monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)
    monkeypatch.setattr("app.services.ai_service._log_usage", lambda *a, **k: None)
    tag = uuid.uuid4().hex
    _Client.calls = 0
    asyncio.run(mpr.research_market_prices(_rows(tag), api_key="k", model="m"))
    asyncio.run(mpr.research_market_prices(_rows(tag), api_key="k", model="m"))
    assert _Client.calls == 4


def test_stale_research_is_not_reused(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.core.database import SessionLocal
    from app.models.pdf_vision_cache import DocumentPageVisionCache
    from app.services.costing import market_research_cache as mrc

    tag = uuid.uuid4().hex
    row = _rows(tag)[0]
    mrc.store_many([(row, {"rate": 1.0, "url": URL})], "m")
    s = SessionLocal()
    try:
        rec = s.query(DocumentPageVisionCache).filter(
            DocumentPageVisionCache.page_sha1 == mrc.item_key(row, "m")).one()
        rec.created_at = datetime.now(timezone.utc) - timedelta(days=30)
        s.commit()
    finally:
        s.close()
    assert mrc.lookup_many([row], "m", 14) == {}
    assert mrc.item_key(row, "m") in mrc.lookup_many([row], "m", 60)


def test_a_failed_call_is_never_remembered(monkeypatch):
    import anthropic
    from app.services.costing import market_research_cache as mrc

    class _Down(_Client):
        async def create(self, **kw):
            raise RuntimeError("503")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Down)
    tag = uuid.uuid4().hex
    asyncio.run(mpr.research_market_prices(_rows(tag), api_key="k", model="m", cache_ttl_days=14))
    assert mrc.lookup_many(_rows(tag), "m", 14) == {}


def test_search_fees_are_metered(monkeypatch):
    import anthropic
    logged = []
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)
    monkeypatch.setattr("app.services.ai_service._log_usage",
                        lambda *a, **k: logged.append(a[4]))
    asyncio.run(mpr.research_market_prices(_rows(uuid.uuid4().hex), api_key="k", model="m"))
    assert logged and all(u["web_search_requests"] == 3 for u in logged)


def test_estimate_cost_prices_web_searches():
    from app.services.langchain.provider_config import estimate_cost

    base = estimate_cost("claude-haiku-4-5", {"input_tokens": 1000, "output_tokens": 100})
    with_search = estimate_cost("claude-haiku-4-5", {"input_tokens": 1000, "output_tokens": 100,
                                                     "web_search_requests": 3})
    assert round(with_search - base, 6) == 0.03


# ── outbound fetches ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/health",
    "http://localhost/admin",
    "http://10.0.0.5/",
    "http://[::1]/",
    "file:///etc/passwd",
    "gopher://example.com/",
])
def test_non_public_targets_are_refused(url):
    from app.core.url_safety import BlockedURLError, assert_public_url

    with pytest.raises(BlockedURLError):
        assert_public_url(url)


def test_web_fetch_refuses_the_metadata_endpoint():
    from app.services.langchain.tools.web_fetch_tool import fetch_url_content

    out = fetch_url_content("http://169.254.169.254/latest/meta-data/")
    assert out["status"] == "error"
    assert "non-public" in out["message"]


def test_a_redirect_inward_is_refused_at_the_hop(monkeypatch):
    """httpx runs request hooks on every hop; the second request is inward."""
    import httpx
    from app.core.url_safety import BlockedURLError, public_only_hook

    def handler(request):
        if request.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})
        return httpx.Response(200, text="secret")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                          event_hooks={"request": [public_only_hook]})
    with pytest.raises(BlockedURLError):
        client.get("http://93.184.216.34/")


def test_web_search_strips_personal_identifiers(monkeypatch):
    from app.services.langchain.tools import web_search_tool as wst

    seen = []
    monkeypatch.setattr(wst.WebSearchTool, "_enhance_query",
                        lambda self, q, t: seen.append(q) or q)
    monkeypatch.setattr(wst, "cache_get_json", lambda k: "cached")
    wst.WebSearchTool()._run("rates for GSTIN 27ABCDE1234F1Z5 call 9876543210")
    assert "27ABCDE1234F1Z5" not in seen[0]
    assert "9876543210" not in seen[0]
