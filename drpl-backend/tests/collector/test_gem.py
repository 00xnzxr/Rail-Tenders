"""GeM fetcher: mapping, scope, sequence parsing, and the request payload.

All offline, against a response saved from the live endpoint on 2026-09-10.
Re-save the fixture with scripts/ when GeM changes shape -- a fixture that no
longer matches the portal is drift, and the test failing is the point.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from collector.portals.base import ParserDrift, PortalUnavailable
from collector.portals.gem import (
    FETCHER_VERSION,
    MINISTRY_PATH,
    MINISTRY_REFERER,
    GemSession,
    build_ministry_payload,
    build_payload,
    fetch_page,
    in_scope,
    ist_instant,
    kind,
    ministry_of,
    sequence_parts,
    to_tender,
)

FIXTURE = Path(__file__).parent / "fixtures" / "gem_all_bids_data_railway.json"


def _as_dt(s: str) -> datetime:
    """Compare instants, not spellings: "Z" and "+00:00" are the same time."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def docs() -> list[dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["response"]["response"]["docs"]


@pytest.fixture(scope="module")
def num_found() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["response"]["response"]["numFound"]


# -- the response shape --------------------------------------------------


def test_fixture_has_the_shape_the_fetcher_expects(docs, num_found):
    assert isinstance(num_found, int) and num_found > 0
    assert len(docs) == 10, "GeM serves ten docs per page, server-fixed"
    assert "b_bid_number" in docs[0]


def test_payload_matches_what_the_endpoint_accepts():
    p = build_payload(2, search="railway")
    assert p["page"] == 2
    assert p["param"] == {"searchBid": "railway", "searchType": "fullText"}
    assert p["filter"]["bidStatusType"] == "ongoing_bids"
    assert p["filter"]["sort"] == "Bid-Start-Date-Latest"
    # Must be JSON-serialisable -- it is sent as a form field, not a JSON body.
    assert json.loads(json.dumps(p)) == p


# -- scope ---------------------------------------------------------------


def test_scope_matches_the_structured_ministry_not_a_keyword(docs):
    """The whole fix for the keyword problem.

    A full-text search for "railway" returns rows from other ministries that
    merely mention one. The ministry field does not guess.
    """
    kept = [d for d in docs if in_scope(d, ["Ministry of Railways"])]
    assert kept, "the railway fixture should contain railway tenders"
    assert all(ministry_of(d) == "Ministry of Railways" for d in kept)


def test_scope_is_exact_not_substring():
    assert in_scope({"ba_official_details_minName": ["Ministry of Railways"]},
                    ["Ministry of Railways"])
    # A ministry that merely CONTAINS the target must not pass.
    assert not in_scope({"ba_official_details_minName": ["Ministry of Railways Welfare"]},
                        ["Ministry of Railways"])


def test_scope_is_case_insensitive():
    assert in_scope({"ba_official_details_minName": ["MINISTRY OF RAILWAYS"]},
                    ["ministry of railways"])


def test_empty_ministry_list_keeps_everything():
    """A deliberate unfiltered audit sweep must not silently drop rows."""
    assert in_scope({"ba_official_details_minName": ["Ministry of Steel"]}, [])


def test_missing_ministry_is_out_of_scope_when_filtering():
    assert not in_scope({}, ["Ministry of Railways"])


# -- bid numbers ---------------------------------------------------------


def test_kind_distinguishes_bids_from_reverse_auctions():
    assert kind("GEM/2026/B/7916525") == "bid"
    assert kind("GEM/2026/R/732042") == "ra"
    assert kind("nonsense") == "other"


def test_both_kinds_are_kept(docs):
    """Different objects with different deadlines -- tagged, never mixed away."""
    mapped = [to_tender(d) for d in docs]
    assert all(m["bidType"].startswith(("Bid", "RA")) for m in mapped)


def test_sequence_parts_splits_prefix_from_number():
    assert sequence_parts("GEM/2026/B/7916525") == ("GEM/2026/B", 7916525)
    assert sequence_parts("GEM/2026/R/732042") == ("GEM/2026/R", 732042)
    assert sequence_parts("") == (None, None)
    assert sequence_parts("IREPS-12212090") == (None, None)


# -- mapping -------------------------------------------------------------


def test_mapping_produces_the_dedupe_key(docs):
    for doc in docs:
        t = to_tender(doc)
        assert t["portal"] == "gem"
        assert t["tenderId"].startswith("GEM/")


def test_mapping_records_which_fetcher_produced_the_row(docs):
    """Not just which portal. The audit needs to attribute a miss to a fetcher."""
    t = to_tender(docs[0])
    assert t["sourcePortal"] == FETCHER_VERSION


def test_mapping_prefers_the_full_category_over_gems_truncation():
    """b_category_name is GeM's own 100-char clip of bd_category_name."""
    doc = {
        "b_bid_number": ["GEM/2026/B/1"],
        "b_category_name": ["Supply of Structure of AT Shelter,Supply of Construction Mat"],
        "bd_category_name": ["Supply of Structure of AT Shelter,Supply of Construction Material,"
                             "Supply of Building and repair"],
    }
    assert to_tender(doc)["title"] == doc["bd_category_name"][0]


def test_mapping_leaves_estimated_value_unset():
    """estimatedValue is not on the listing.

    A missing field must route to "fetch the detail page and find out", never
    to "out of scope" -- the old extension's scope gate treated an unparseable
    value as a failed check and so excluded exactly the tenders that most
    needed a detail fetch. Enrichment is a downstream stage, never a gate.
    """
    t = to_tender({"b_bid_number": ["GEM/2026/B/1"]})
    assert "estimatedValue" not in t
    assert "emdAmount" not in t


def test_mapping_returns_none_without_a_bid_number():
    assert to_tender({"bd_category_name": ["something"]}) is None


def test_mapping_carries_the_search_term(docs):
    t = to_tender(docs[0], term="traction motor")
    assert t["searchMatchKeyword"] == "traction motor"


def test_mapping_converts_portal_dates_from_ist(docs):
    """GeM's listing "Z" is IST wearing a UTC label. Converting is the fix.

    This test used to assert the opposite -- that the value was carried
    verbatim because "GeM already returns ISO-8601 Z". It does not. The portal
    displays the very same digits as IST in its own UI, so reading them as UTC
    put every closing date 5h30m late on every row.
    """
    doc = next(d for d in docs if d.get("final_end_date_sort"))
    t = to_tender(doc)
    raw = doc["final_end_date_sort"][0]
    assert t["closingDate"] != raw, "the unconverted value is the bug"
    assert _as_dt(t["closingDate"]) == _as_dt(raw) - timedelta(hours=5, minutes=30)


def test_ist_instant_converts_the_unearned_z():
    # The exact pair measured live: GeM's UI showed this bid starting at
    # "19-09-2026 10:00 PM" while the API returned 22:00:00Z.
    assert ist_instant("2026-09-19T22:00:00Z") == "2026-09-19T16:30:00+00:00"


def test_ist_instant_reads_a_naive_timestamp_as_ist_too():
    assert ist_instant("2026-09-19T22:00:00") == "2026-09-19T16:30:00+00:00"


def test_ist_instant_trusts_an_explicit_offset():
    """If GeM ever states the real offset, stop rewriting it."""
    assert _as_dt(ist_instant("2026-09-19T22:00:00+05:30")) == _as_dt(
        "2026-09-19T16:30:00+00:00"
    )


def test_ist_instant_passes_through_what_it_cannot_parse():
    """Losing every date is a worse failure than the one being fixed."""
    assert ist_instant("not a date") == "not a date"
    assert ist_instant(None) is None
    assert ist_instant("") == ""


def test_ist_instant_is_deliberately_not_idempotent():
    """Pinned because it is a footgun, not because it is desirable.

    A real UTC "+00:00" and GeM's unearned one are byte-identical, so the
    function cannot tell a converted value from a raw one -- applying it twice
    shifts by 11 hours. That is why ``to_tender`` is its only caller and calls
    it exactly once, on the raw listing field. This test fails the moment
    someone makes it self-applying, which is the warning worth keeping.
    """
    once = ist_instant("2026-09-19T22:00:00Z")
    assert ist_instant(once) != once


def test_mapping_is_stable_across_repeated_calls(docs):
    """to_tender re-reads the raw field, so it never compounds the shift."""
    doc = next(d for d in docs if d.get("final_end_date_sort"))
    assert to_tender(doc)["closingDate"] == to_tender(doc)["closingDate"]


def test_mapping_builds_a_document_url_when_there_is_an_id(docs):
    doc = next(d for d in docs if d.get("b_id"))
    t = to_tender(doc)
    # NOT hardcoded to showbidDocument any more: the first fixture row is a
    # reverse auction, and asserting that path here is what caught the bug.
    assert t["sourceUrl"].startswith("https://bidplus.gem.gov.in/")
    assert str(doc["b_id"][0]) in t["sourceUrl"]


def test_every_fixture_row_maps_without_raising(docs):
    """A field GeM sometimes omits must not take the whole page down."""
    assert all(to_tender(d) is not None for d in docs)


# -- bootstrap resilience ------------------------------------------------
#
# Observed live on 2026-09-10: /all-bids intermittently answers 500. An F5 WAF
# in front of a busy origin does that and it clears within seconds. Failing the
# whole sweep on the first blip would mean a Search button that fails for a
# reason the portal has already forgotten about.


def _no_backoff(monkeypatch):
    """Make the bootstrap backoff instant.

    Zeroing the configured delay rather than patching ``asyncio.sleep``:
    ``gem.asyncio`` IS the asyncio module, so patching its ``sleep`` replaces
    it for everything, including the replacement.
    """
    from collector.config import get_settings

    monkeypatch.setenv("REQUEST_DELAY_SECONDS", "0")
    get_settings.cache_clear()
    monkeypatch.undo_extra = get_settings.cache_clear


def test_bootstrap_retries_a_transient_500_then_succeeds(monkeypatch):
    import asyncio

    import httpx

    from collector.portals import gem

    _no_backoff(monkeypatch)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="upstream hiccup")
        return httpx.Response(
            200,
            text="csrf_bd_gem_nk': '" + "a" * 32 + "'",
            headers={"set-cookie": f"csrf_gem_cookie={'b' * 32}; Path=/"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = asyncio.run(gem.bootstrap(client))
    assert calls["n"] == 3
    assert session.token == "b" * 32
    asyncio.run(client.aclose())


def test_bootstrap_gives_up_as_portal_unavailable_not_a_raw_http_error(monkeypatch):
    """One exception type the caller already knows how to report."""
    import asyncio

    import httpx

    from collector.portals import gem

    _no_backoff(monkeypatch)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down"))
    )
    with pytest.raises(PortalUnavailable):
        asyncio.run(gem.bootstrap(client))
    asyncio.run(client.aclose())


def test_bootstrap_does_not_retry_a_flat_refusal(monkeypatch):
    """A 404 will still be a 404 in nine seconds."""
    import asyncio

    import httpx

    from collector.portals import gem

    _no_backoff(monkeypatch)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(PortalUnavailable):
        asyncio.run(gem.bootstrap(client))
    assert calls["n"] == 1
    asyncio.run(client.aclose())


def test_bootstrap_falls_back_to_the_inline_token_when_the_cookie_is_absent(monkeypatch):
    """The cookie is primary; the page's jQuery is the fallback.

    Note the live form is ``csrf_bd_gem_nk': '<hex>'`` -- a JS object entry,
    NOT the ``value="..."`` attribute the build sheet assumed.
    """
    import asyncio

    import httpx

    from collector.portals import gem

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="csrf_bd_gem_nk': '" + "c" * 32 + "'")
        )
    )
    session = asyncio.run(gem.bootstrap(client))
    assert session.token == "c" * 32
    asyncio.run(client.aclose())


def test_bootstrap_raises_parser_drift_when_no_token_exists_anywhere(monkeypatch):
    import asyncio

    import httpx

    from collector.portals import gem
    from collector.portals.base import ParserDrift

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>no token</html>"))
    )
    with pytest.raises(ParserDrift):
        asyncio.run(gem.bootstrap(client))
    asyncio.run(client.aclose())


# -- GeM's own ministry filter -------------------------------------------


def test_ministry_payload_is_what_advanced_search_sends():
    """Verified against the live endpoint on 2026-09-22: numFound 1703."""
    p = build_ministry_payload(3, "Ministry of Railways")
    assert p["searchType"] == "ministry-search"
    assert p["ministry"] == "Ministry of Railways"
    assert p["page"] == 3
    # The form marks State and both dates required; its own validator does
    # not. Sending them blank is what makes one ministry enough.
    assert p["buyerState"] == "" and p["bidEndFromMin"] == "" and p["bidEndToMin"] == ""
    assert json.loads(json.dumps(p)) == p


def test_ministry_value_is_spelled_the_way_the_scope_check_reads_it():
    """The filter and the scope check must agree by construction, not by luck."""
    name = "Ministry of Railways"
    assert build_ministry_payload(1, name)["ministry"] == name
    assert in_scope({"ba_official_details_minName": [name]}, [name])


@pytest.mark.asyncio
async def test_fetch_page_routes_a_ministry_query_to_advanced_search():
    sent = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"response": {"response": {"numFound": 1703, "docs": []}}}

    class FakeClient:
        async def post(self, url, data=None, headers=None):
            sent["url"] = url
            sent["payload"] = json.loads(data["payload"])
            sent["referer"] = headers["Referer"]
            return FakeResponse()

    session = GemSession(client=FakeClient(), token="tok")

    total, docs = await fetch_page(session, 2, ministry="Ministry of Railways")
    assert total == 1703
    assert sent["url"].endswith(MINISTRY_PATH)
    assert sent["referer"].endswith(MINISTRY_REFERER)
    assert sent["payload"]["searchType"] == "ministry-search"

    # ...and leaves the ordinary listing query exactly where it was.
    await fetch_page(session, 2, search="railway")
    assert sent["url"].endswith("/all-bids-data")
    assert sent["referer"].endswith("/all-bids")
    assert sent["payload"]["param"] == {"searchBid": "railway", "searchType": "fullText"}
