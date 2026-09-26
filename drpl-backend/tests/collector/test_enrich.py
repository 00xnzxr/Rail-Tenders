"""
Document enrichment: fetching the bid PDF and reading it with Haiku.

The reason this stage exists is a gap measured in the live platform: WORTH and
EMD are empty on every tender card, because those numbers are not on any
listing page on either portal -- they are inside the bid PDF.

Two invariants are pinned hardest here, because both are places where a
plausible-looking implementation would quietly lose data:

  * enrichment can only ever ADD. No document, no API key, a refusal, a
    timeout -- the tender still ships with its listing data.
  * a value the portal itself stated is never overwritten by a reading of its
    prose.

No network and no API key are needed to run these.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import collector.config as cfg
from collector import enrich
from collector.enrich import ai as ai_mod
from collector.enrich.documents import fetch_document

PDF = b"%PDF-1.4\n%fake bid document\n"


@pytest.fixture(autouse=True)
def _clean():
    ai_mod.reset_for_tests()
    cfg.get_settings.cache_clear()
    yield
    ai_mod.reset_for_tests()
    cfg.get_settings.cache_clear()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# -- fetching ------------------------------------------------------------


def test_a_pdf_is_fetched():
    def handler(_r):
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})

    async def go():
        c = _client(handler)
        try:
            return await fetch_document(c, "https://bidplus.gem.gov.in/showbidDocument/1")
        finally:
            await c.aclose()

    doc = asyncio.run(go())
    assert doc is not None and doc.is_pdf and doc.size == len(PDF)


def test_an_html_error_page_served_as_200_is_rejected():
    """GeM answers a document URL with an HTML error page and a 200.

    Checking the magic bytes is the only reliable tell -- the status code and
    the content-type both look fine.
    """
    def handler(_r):
        return httpx.Response(200, text="<html>Sorry, error</html>",
                              headers={"content-type": "application/pdf"})

    async def go():
        c = _client(handler)
        try:
            return await fetch_document(c, "https://x/doc")
        finally:
            await c.aclose()

    assert asyncio.run(go()) is None


def test_a_404_is_none_not_an_exception():
    async def go():
        c = _client(lambda _r: httpx.Response(404))
        try:
            return await fetch_document(c, "https://x/doc")
        finally:
            await c.aclose()

    assert asyncio.run(go()) is None


def test_an_oversized_document_is_skipped_by_declared_length():
    def handler(_r):
        return httpx.Response(200, content=PDF,
                              headers={"content-type": "application/pdf",
                                       "content-length": str(50 * 1024 * 1024)})

    async def go():
        c = _client(handler)
        try:
            return await fetch_document(c, "https://x/doc")
        finally:
            await c.aclose()

    assert asyncio.run(go()) is None


def test_an_oversized_document_is_abandoned_mid_stream():
    """A lying content-length must not get a worker to read 50MB into memory."""
    big = b"%PDF-1.4" + b"x" * 200_000

    def handler(_r):
        return httpx.Response(200, content=big, headers={"content-type": "application/pdf"})

    async def go():
        c = _client(handler)
        try:
            return await fetch_document(c, "https://x/doc", max_bytes=1000)
        finally:
            await c.aclose()

    assert asyncio.run(go()) is None


# -- mapping to TenderInput ---------------------------------------------


def _extraction(**fields) -> ai_mod.Extraction:
    base = {k: None for k in ai_mod.EXTRACTION_SCHEMA["properties"]}
    base.update(fields)
    return ai_mod.Extraction(fields=base, input_tokens=1000, output_tokens=200)


def test_extracted_fields_map_to_tenderinput_names():
    out = _extraction(
        estimated_value_inr=12545000,
        emd_amount_inr=250900,
        eligibility_criteria="Three years of similar work.",
        delivery_location="ELS Kalyan",
    ).to_tender_fields()
    assert out["estimatedValue"] == 12545000
    assert out["emdAmount"] == 250900
    assert out["eligibilityCriteria"] == "Three years of similar work."
    assert out["deliveryLocation"] == "ELS Kalyan"


def test_nulls_are_dropped_rather_than_sent():
    """An explicit null would overwrite a value another source already found."""
    out = _extraction(estimated_value_inr=100).to_tender_fields()
    assert set(out) == {"estimatedValue"}


def test_empty_strings_are_dropped_too():
    assert _extraction(eligibility_criteria="   ").to_tender_fields() == {}


def test_a_zero_emd_is_kept_because_nil_is_a_real_answer():
    """"EMD: Nil" is a fact about the bid, not a missing field."""
    assert _extraction(emd_amount_inr=0).to_tender_fields()["emdAmount"] == 0


def test_a_string_number_is_coerced_and_nonsense_is_dropped():
    assert _extraction(estimated_value_inr="4500000").to_tender_fields()["estimatedValue"] == 4500000
    assert "estimatedValue" not in _extraction(estimated_value_inr="about 45 lakh").to_tender_fields()


def test_a_negative_value_is_refused():
    assert "estimatedValue" not in _extraction(estimated_value_inr=-5).to_tender_fields()


def test_cost_is_reported_at_haiku_list_price():
    """$1 / MTok in, $5 / MTok out."""
    e = ai_mod.Extraction(fields={}, input_tokens=1_000_000, output_tokens=1_000_000)
    assert e.cost_usd == pytest.approx(6.0)


# -- the orchestrator ----------------------------------------------------


def _tender(tid="GEM/2026/B/1", **kw):
    t = {
        "portal": "gem",
        "tenderId": tid,
        "title": "Supply of traction motor bearings",
        "nitDocumentLinks": [f"https://bidplus.gem.gov.in/showbidDocument/{tid[-1]}"],
        "documentLinks": [f"https://bidplus.gem.gov.in/showbidDocument/{tid[-1]}"],
    }
    t.update(kw)
    return t


def _enable(monkeypatch, extraction=None, doc=PDF):
    """Turn enrichment on with a stubbed model and document fetch."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ENRICH_DOCUMENTS_ENABLED", "true")
    cfg.get_settings.cache_clear()
    monkeypatch.setattr(ai_mod, "is_available", lambda: True)

    async def fake_extract(_bytes, **_kw):
        return extraction

    monkeypatch.setattr(ai_mod, "extract_from_pdf", fake_extract)

    def handler(_r):
        if doc is None:
            return httpx.Response(404)
        return httpx.Response(200, content=doc, headers={"content-type": "application/pdf"})

    return _client(handler)


def test_enrichment_fills_the_fields_the_listing_could_not(monkeypatch):
    c = _enable(monkeypatch, _extraction(estimated_value_inr=12545000, emd_amount_inr=250900))
    t = _tender()

    async def go():
        try:
            return await enrich.enrich_tenders(c, [t])
        finally:
            await c.aclose()

    res = asyncio.run(go())
    assert res.enriched == 1
    assert t["estimatedValue"] == 12545000
    assert t["emdAmount"] == 250900
    assert t["isDetailExtracted"] is True


def test_a_portal_stated_value_is_never_overwritten(monkeypatch):
    """The portal's own structured field beats a reading of its prose."""
    c = _enable(monkeypatch, _extraction(estimated_value_inr=999))
    t = _tender(estimatedValue=4500000)

    async def go():
        try:
            return await enrich.enrich_tenders(c, [t])
        finally:
            await c.aclose()

    asyncio.run(go())
    assert t["estimatedValue"] == 4500000


def test_a_missing_document_still_ships_the_tender(monkeypatch):
    c = _enable(monkeypatch, _extraction(estimated_value_inr=1), doc=None)
    t = _tender()

    async def go():
        try:
            return await enrich.enrich_tenders(c, [t])
        finally:
            await c.aclose()

    res = asyncio.run(go())
    assert res.no_document == 1 and res.enriched == 0
    assert t["tenderId"] == "GEM/2026/B/1", "the tender is untouched and still shippable"
    assert "isDetailExtracted" not in t


def test_a_model_failure_still_ships_the_tender(monkeypatch):
    c = _enable(monkeypatch, extraction=None)
    t = _tender()

    async def go():
        try:
            return await enrich.enrich_tenders(c, [t])
        finally:
            await c.aclose()

    res = asyncio.run(go())
    assert res.no_extraction == 1 and res.enriched == 0
    assert "estimatedValue" not in t


def test_already_known_tenders_are_not_re_enriched(monkeypatch):
    """Cost scales with what a sweep FOUND, not how far it walked."""
    c = _enable(monkeypatch, _extraction(estimated_value_inr=1))
    t = _tender()

    async def go():
        try:
            return await enrich.enrich_tenders(c, [t], known={"GEM/2026/B/1"})
        finally:
            await c.aclose()

    res = asyncio.run(go())
    assert res.attempted == 0


def test_enrichment_is_off_without_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ENRICH_DOCUMENTS_ENABLED", "true")
    cfg.get_settings.cache_clear()
    assert enrich.is_enabled() is False


def test_enrichment_can_be_switched_off_with_a_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ENRICH_DOCUMENTS_ENABLED", "false")
    cfg.get_settings.cache_clear()
    assert enrich.is_enabled() is False


def test_one_failure_does_not_cancel_its_siblings(monkeypatch):
    c = _enable(monkeypatch, _extraction(estimated_value_inr=7))
    tenders = [_tender(f"GEM/2026/B/{i}") for i in range(1, 4)]

    calls = {"n": 0}
    real = ai_mod.extract_from_pdf

    async def flaky(b, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("model exploded")
        return await real(b, **kw)

    monkeypatch.setattr(ai_mod, "extract_from_pdf", flaky)

    async def go():
        try:
            return await enrich.enrich_tenders(c, tenders)
        finally:
            await c.aclose()

    res = asyncio.run(go())
    assert res.enriched == 2, "the other two still landed"


def test_the_per_page_budget_is_respected(monkeypatch):
    monkeypatch.setenv("ENRICH_MAX_PER_PAGE", "2")
    c = _enable(monkeypatch, _extraction(estimated_value_inr=1))
    cfg.get_settings.cache_clear()
    tenders = [_tender(f"GEM/2026/B/{i}") for i in range(1, 6)]

    async def go():
        try:
            return await enrich.enrich_tenders(c, tenders)
        finally:
            await c.aclose()

    assert asyncio.run(go()).attempted == 2


def test_a_tender_with_no_links_is_skipped(monkeypatch):
    c = _enable(monkeypatch, _extraction(estimated_value_inr=1))
    t = _tender(nitDocumentLinks=[], documentLinks=[])

    async def go():
        try:
            return await enrich.enrich_tenders(c, [t])
        finally:
            await c.aclose()

    assert asyncio.run(go()).attempted == 0


# -- the request the SDK is asked to make --------------------------------


def test_the_model_call_matches_the_haiku_contract(monkeypatch):
    """Haiku 4.5 rejects `effort`, and PDFs go in as native document blocks.

    Asserting the request shape here is what stops a later edit from adding an
    `output_config.effort` that 400s only in production.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg.get_settings.cache_clear()
    captured: dict = {}

    class FakeMessages:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=json.dumps(
                    {k: None for k in ai_mod.EXTRACTION_SCHEMA["properties"]}
                    | {"estimated_value_inr": 500000}))],
                usage=SimpleNamespace(input_tokens=1200, output_tokens=90),
                stop_reason="end_turn",
            )

    monkeypatch.setattr(ai_mod, "get_client", lambda: SimpleNamespace(messages=FakeMessages()))

    out = asyncio.run(ai_mod.extract_from_pdf(PDF, tender_id="GEM/2026/B/1", title="Bearings"))

    assert out is not None and out.fields["estimated_value_inr"] == 500000
    assert captured["model"] == "claude-haiku-4-5"
    assert "effort" not in json.dumps(captured.get("output_config", {}))
    assert "thinking" not in captured
    block = captured["messages"][0]["content"][0]
    assert block["type"] == "document"
    assert block["source"]["media_type"] == "application/pdf"
    assert captured["output_config"]["format"]["type"] == "json_schema"


def test_a_refusal_returns_none_rather_than_reading_empty_content(monkeypatch):
    """A refusal is HTTP 200 with no usable content -- check before reading."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg.get_settings.cache_clear()

    class FakeMessages:
        async def create(self, **_kw):
            return SimpleNamespace(content=[], usage=None, stop_reason="refusal")

    monkeypatch.setattr(ai_mod, "get_client", lambda: SimpleNamespace(messages=FakeMessages()))
    assert asyncio.run(ai_mod.extract_from_pdf(PDF)) is None


def test_unparseable_json_returns_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg.get_settings.cache_clear()

    class FakeMessages:
        async def create(self, **_kw):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="not json at all")],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                stop_reason="end_turn",
            )

    monkeypatch.setattr(ai_mod, "get_client", lambda: SimpleNamespace(messages=FakeMessages()))
    assert asyncio.run(ai_mod.extract_from_pdf(PDF)) is None


def test_no_client_means_no_call_and_no_error(monkeypatch, tmp_path):
    """Without a key the collector still sweeps -- it just ships empty fields.

    Points COLLECTOR_ENV_FILE at nothing so a developer's real .env cannot
    make this pass or fail by accident.
    """
    monkeypatch.setenv("COLLECTOR_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg.get_settings.cache_clear()
    ai_mod.reset_for_tests()

    assert ai_mod.get_client() is None
    assert ai_mod.is_available() is False
    assert asyncio.run(ai_mod.extract_from_pdf(PDF)) is None
