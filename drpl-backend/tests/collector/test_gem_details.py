"""
GeM enrichment: document URLs, the richer description, and request retry.

Two things are pinned here that came out of running the collector against the
live portal rather than out of the build sheet:

  * the document endpoint depends on the BID TYPE. Read off GeM's own card
    renderer on /all-bids. Sending every bid to ``showbidDocument`` -- which a
    naive mapper does, and mine did -- gives a reverse auction a URL that is
    not its document. Both PDF forms were fetched live on 2026-09-10 and
    returned real ``application/pdf`` bodies.

  * ``/all-bids-data`` intermittently answers 500 and succeeds seconds later.
    ``bootstrap`` had a retry; ``fetch_page`` did not, so the 500 surfaced as a
    raw ``HTTPStatusError`` and killed the sweep. That is the failure this
    module's second half exists to prevent regressing.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

import collector.config as cfg
from collector.portals import gem
from collector.portals.base import PortalUnavailable
from collector.portals.gem import (
    GemSession,
    bid_type_label,
    build_description,
    document_path,
    to_tender,
)


@pytest.fixture(autouse=True)
def _instant_backoff():
    """Zero the politeness delay so retry tests do not actually wait.

    Set through configuration rather than by patching ``asyncio.sleep``:
    ``gem.asyncio`` IS the asyncio module, so patching its sleep replaces it
    for everything, including the replacement.
    """
    os.environ["REQUEST_DELAY_SECONDS"] = "0"
    cfg.get_settings.cache_clear()
    yield
    os.environ.pop("REQUEST_DELAY_SECONDS", None)
    cfg.get_settings.cache_clear()


def _doc(bid_type: int, eval_type: int = 0, bid_id: int = 999, **extra) -> dict:
    d = {
        "b_bid_number": ["GEM/2026/B/1"],
        "b_id": [bid_id],
        "b_bid_type": [bid_type],
        "b_eval_type": [eval_type],
    }
    d.update({k: [v] for k, v in extra.items()})
    return d


# -- document URLs -------------------------------------------------------


def test_a_plain_bid_uses_showbiddocument():
    assert document_path(_doc(1)) == "/showbidDocument/999"


def test_a_reverse_auction_uses_showradocumentpdf():
    """THE bug: an RA sent to showbidDocument points at the wrong file."""
    assert document_path(_doc(2)) == "/showradocumentPdf/999"


def test_an_ra_with_schedules_links_to_the_schedule_list():
    assert document_path(_doc(2, eval_type=1)) == "/list-ra-schedules/999"


def test_a_direct_ra_uses_its_own_endpoint():
    assert document_path(_doc(5)) == "/showdirectradocumentPdf/999"


def test_a_doc_with_no_id_falls_back_rather_than_building_a_broken_url():
    assert document_path({"b_bid_number": ["GEM/2026/B/1"]}) == "/all-bids"


def test_the_bid_document_is_shipped_as_the_nit():
    """On GeM one PDF carries scope, eligibility, EMD and schedule.

    Putting it in nitDocumentLinks is what makes drpl-backend's existing
    nit_link_fetch_service download it and the tender analyzer read it --
    which is where estimatedValue and EMD actually come from.
    """
    t = to_tender(_doc(1, bid_id=42))
    assert t["nitDocumentLinks"] == ["https://bidplus.gem.gov.in/showbidDocument/42"]
    assert t["documentLinks"] == t["nitDocumentLinks"]
    assert t["detailUrl"] == t["sourceUrl"]


def test_an_ra_also_links_back_to_its_parent_bid():
    t = to_tender(_doc(2, bid_id=7, b_id_parent=3, b_bid_number_parent="GEM/2026/B/9"))
    assert "https://bidplus.gem.gov.in/showbidDocument/3" in t["documentLinks"]
    assert len(t["documentLinks"]) == 2


def test_is_detail_extracted_is_not_claimed():
    """Listing data plus a link is not a parsed detail page.

    Claiming otherwise would stop drpl-backend's _update_existing_tender from
    ever enriching the row when the real detail arrives.
    """
    assert to_tender(_doc(1)).get("isDetailExtracted") in (None, False)


# -- the richer description ---------------------------------------------


def test_the_description_carries_what_has_no_column_of_its_own():
    text = build_description(_doc(
        1,
        bd_category_name="Traction motor spares",
        b_total_quantity=40,
        bbt_title="BOQ-2026-11",
        ba_official_details_minName="Ministry of Railways",
        ba_official_details_deptName="Indian Railways",
    ))
    assert "Traction motor spares" in text
    assert "Quantity: 40" in text
    assert "BOQ-2026-11" in text
    assert "Ministry of Railways / Indian Railways" in text


def test_bid_type_label_mirrors_what_gem_prints():
    assert bid_type_label(_doc(1)) == "Bid"
    assert bid_type_label(_doc(2)) == "RA"
    assert bid_type_label(_doc(1, is_rc_bid=1)) == "Bid (Rate Contract)"
    assert bid_type_label(_doc(1, ba_is_global_tendering=1)) == "Bid (Global Tender)"


def test_high_value_and_single_packet_survive_into_the_description():
    text = build_description(_doc(1, is_high_value=True, ba_is_single_packet=1))
    assert "High value bid" in text
    assert "Single packet" in text


def test_a_zero_quantity_is_still_reported():
    """0 is a fact about the bid; falsy-checking it away loses information."""
    assert "Quantity: 0" in build_description(_doc(1, b_total_quantity=0))


# -- retry on the data endpoint -----------------------------------------


def _session(handler) -> GemSession:
    return GemSession(httpx.AsyncClient(transport=httpx.MockTransport(handler)), "tok")


async def _fetch(handler, **kw):
    s = _session(handler)
    try:
        return await gem.fetch_page(s, 1, **kw)
    finally:
        await s.aclose()


def test_fetch_page_retries_a_500_instead_of_raising_httpstatuserror():
    """The exact failure reported: HTTPStatusError 500 on /all-bids-data."""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json={"response": {"response": {"numFound": 5, "docs": []}}})

    total, _docs = asyncio.run(_fetch(handler, search="railway"))
    assert calls["n"] == 3, "retried rather than raising on the first 500"
    assert total == 5


def test_a_non_json_200_is_treated_as_transient():
    """GeM serves JSON as text/html, so a WAF page arrives as an ordinary 200.

    Verified live: Content-Type is 'text/html; charset=UTF-8' even on the
    successful JSON response, so status alone cannot tell them apart.
    """
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(200, text="<html>Request blocked</html>")
        return httpx.Response(200, json={"response": {"response": {"numFound": 1, "docs": []}}})

    total, _ = asyncio.run(_fetch(handler))
    assert calls["n"] == 2
    assert total == 1


def test_a_403_is_raised_for_rebootstrap_not_retried():
    """A stale CSRF token stays stale; the sweep must re-bootstrap, not retry."""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(403)

    with pytest.raises(PermissionError):
        asyncio.run(_fetch(handler))
    assert calls["n"] == 1


def test_exhausted_retries_surface_as_portal_unavailable():
    """One exception type the sweep already knows how to report."""
    with pytest.raises(PortalUnavailable):
        asyncio.run(_fetch(lambda _r: httpx.Response(500)))


def test_a_429_is_retried_too():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"response": {"response": {"numFound": 0, "docs": []}}})

    asyncio.run(_fetch(handler))
    assert calls["n"] == 2


# -- corrigenda ----------------------------------------------------------


def test_a_corrigendum_is_flagged_on_the_tender():
    """A corrigendum changes scope or dates on a live tender.

    Missing one means bidding against superseded terms.
    """
    doc = _doc(1, bid_id=5)
    tender = to_tender(doc)

    def handler(_request):
        return httpx.Response(200, json={
            "status": 1, "code": 200,
            "response": {"corrigendum": True, "representation": False},
        })

    async def go():
        s = _session(handler)
        try:
            return await gem.enrich(s, [doc], [tender])
        finally:
            await s.aclose()

    assert asyncio.run(go()) == 1
    assert tender["corrigendaLinks"] == [tender["sourceUrl"]]
    assert tender["numberOfAmendments"] == 1
    assert "CORRIGENDUM ISSUED" in tender["description"]


def test_no_corrigendum_leaves_the_tender_alone():
    doc = _doc(1, bid_id=5)
    tender = to_tender(doc)
    before = dict(tender)

    def handler(_request):
        return httpx.Response(200, json={
            "response": {"corrigendum": False, "representation": False}})

    async def go():
        s = _session(handler)
        try:
            return await gem.enrich(s, [doc], [tender])
        finally:
            await s.aclose()

    assert asyncio.run(go()) == 0
    assert tender == before


def test_a_failing_enrichment_never_costs_the_tender():
    """The listing data is still worth shipping without the extra."""
    doc = _doc(1, bid_id=5)
    tender = to_tender(doc)

    async def go():
        s = _session(lambda _r: httpx.Response(500))
        try:
            return await gem.enrich(s, [doc], [tender])
        finally:
            await s.aclose()

    assert asyncio.run(go()) == 0
    assert tender["tenderId"] == "GEM/2026/B/1"


def test_enrichment_is_bounded_per_page():
    """Volume is what gets a collector blocked."""
    os.environ["GEM_ENRICH_MAX_PER_PAGE"] = "2"
    os.environ["GEM_ENRICH_DELAY_SECONDS"] = "0"
    cfg.get_settings.cache_clear()
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(200, json={"response": {"corrigendum": False}})

    docs = [_doc(1, bid_id=i) for i in range(6)]
    tenders = []
    for i, d in enumerate(docs):
        d["b_bid_number"] = [f"GEM/2026/B/{i}"]
        tenders.append(to_tender(d))

    async def go():
        s = _session(handler)
        try:
            return await gem.enrich(s, docs, tenders)
        finally:
            await s.aclose()

    try:
        asyncio.run(go())
    finally:
        os.environ.pop("GEM_ENRICH_MAX_PER_PAGE", None)
        os.environ.pop("GEM_ENRICH_DELAY_SECONDS", None)
        cfg.get_settings.cache_clear()

    assert calls["n"] == 2, "stopped at the configured per-page budget"
