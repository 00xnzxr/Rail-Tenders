"""
Complete enumeration and the detail stage.

The properties worth pinning here are not "does it fetch a page" -- they are the
three things that decide whether the collector can honestly say it missed
nothing:

  * the walk is ordered oldest-first, because newest-first pagination over a
    live list silently drops rows;
  * a page that fails is counted as a hole in coverage, not swallowed;
  * ``is_complete`` is false whenever either of those went wrong.

Everything here runs against fakes. The live portal is exercised separately --
these have to pass on a laptop with no network.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from collector.config import get_settings
from collector.portals import gem_detail, gem_full
from collector.portals.base import PortalUnavailable
from collector.portals.gem_detail import DetailResult
from collector.portals.gem_full import (
    FRESH_SORT,
    STABLE_SORT,
    FullSweepStats,
    GemFullFetcher,
    enumerate_pages,
)


def _doc(n: int, ministry: str = "Ministry of Railways") -> dict:
    """A GeM listing row, in the Solr-style single-element-list shape."""
    return {
        "b_bid_number": [f"GEM/2026/B/{7000000 + n}"],
        "b_id": [9000000 + n],
        "b_bid_type": [1],
        "b_eval_type": [0],
        "bd_category_name": [f"Item {n}"],
        "ba_official_details_minName": [ministry],
        "ba_official_details_deptName": ["Indian Railways"],
        "final_start_date_sort": ["2026-09-01T10:00:00Z"],
        "final_end_date_sort": ["2026-09-30T10:00:00Z"],
    }


class FakeSession:
    """Stands in for GemSession. Records what was asked for."""

    def __init__(self, total: int, fail_pages: Optional[set[int]] = None,
                 ministry_of_page=None):
        self.total = total
        self.fail_pages = fail_pages or set()
        self.calls: list[tuple[int, str, str]] = []
        #: Every `ministry` argument fetch_page was called with, in order.
        #: Kept apart from `calls` so the existing page/search/sort
        #: assertions stay exactly as strict as they were.
        self.ministry_calls: list = []
        self.ministry_of_page = ministry_of_page
        self.client = None
        self.token = "t"
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.fixture
def patched_fetch(monkeypatch):
    """Replace gem.fetch_page with one served from a FakeSession."""

    async def fake_fetch_page(session, page, search="", sort="", ministry=None):
        session.calls.append((page, search, sort))
        session.ministry_calls.append(ministry)
        if page in session.fail_pages:
            raise PortalUnavailable(f"page {page} refused")
        start = (page - 1) * 10
        if start >= session.total:
            return session.total, []
        rows = []
        for i in range(start, min(start + 10, session.total)):
            ministry = "Ministry of Railways"
            if session.ministry_of_page is not None:
                ministry = session.ministry_of_page(page)
            rows.append(_doc(i, ministry))
        return session.total, rows

    monkeypatch.setattr(gem_full, "fetch_page", fake_fetch_page)
    return fake_fetch_page


# -- enumeration ---------------------------------------------------------


class TestEnumeratePages:
    @pytest.mark.asyncio
    async def test_walks_every_page_exactly_once(self, patched_fetch):
        session = FakeSession(total=95)
        pages, rows = [], 0
        async for page, docs, total in enumerate_pages(session, concurrency=4):
            pages.append(page)
            rows += len(docs)
        assert pages == sorted(pages), "pages must be yielded in order"
        assert len(pages) == len(set(pages)), "no page fetched twice"
        assert pages == list(range(1, 11))
        assert rows == 95

    @pytest.mark.asyncio
    async def test_default_sort_is_oldest_start_first(self, patched_fetch):
        # The load-bearing decision in this module, and it was measured three
        # ways on the live portal: newest-first is not walkable; soonest-
        # closing-last looked right and lost 287 of 1,779 tenders in one walk
        # because end dates cluster at round times and the index churns inside
        # those tie groups; oldest-start-first lost 4, all to bids that closed
        # behind the walker, and the repair pass exists for those.
        session = FakeSession(total=30)
        async for _ in enumerate_pages(session):
            pass
        assert STABLE_SORT == "Bid-Start-Date-Oldest"
        assert {sort for _, _, sort in session.calls} == {STABLE_SORT}

    @pytest.mark.asyncio
    async def test_a_failed_page_yields_none_rather_than_stopping(self, patched_fetch):
        # None, not []: an empty list is what the end of a shrinking list
        # looks like, and the caller must be able to tell a hole from that.
        session = FakeSession(total=100, fail_pages={3, 7})
        seen = {}
        async for page, docs, _ in enumerate_pages(session, concurrency=3):
            seen[page] = docs
        assert len(seen) == 10, "the walk continues past a bad page"
        assert seen[3] is None and seen[7] is None
        assert len(seen[4]) == 10

    @pytest.mark.asyncio
    async def test_a_slow_page_does_not_hold_up_the_others(self, patched_fetch, monkeypatch):
        # Rolling, not windowed. With one page out for a long time the other
        # slots keep fetching; a gathered window sat on all four until the
        # straggler came back, and GeM's latency varies enough that the walk
        # ran well below the rate its concurrency implied.
        import collector.config as cfg

        monkeypatch.setenv("GEM_PAGE_DELAY_SECONDS", "0")
        cfg.get_settings.cache_clear()
        listing = patched_fetch
        gate = asyncio.Event()

        async def uneven(s, page, search="", sort="", ministry=None):
            if page == 2:
                await gate.wait()
            return await listing(s, page, search, sort)

        monkeypatch.setattr(gem_full, "fetch_page", uneven)
        session = FakeSession(total=200)
        walk = enumerate_pages(session, concurrency=4)
        first = await walk.__anext__()
        assert first[0] == 1
        # Page 2 is stuck. Give the other slots a moment to run ahead of it.
        for _ in range(20):
            await asyncio.sleep(0)
        fetched_meanwhile = {p for p, _, _ in session.calls} - {1, 2}
        assert len(fetched_meanwhile) >= 6, (
            f"only {sorted(fetched_meanwhile)} fetched while page 2 was out"
        )
        gate.set()
        rest = [p async for p, _, _ in walk]
        assert rest == list(range(2, 21)), "still yielded in order"

    @pytest.mark.asyncio
    async def test_max_pages_caps_the_walk(self, patched_fetch):
        session = FakeSession(total=1000)
        pages = [p async for p, _, _ in enumerate_pages(session, max_pages=5)]
        assert pages == [1, 2, 3, 4, 5]

    @pytest.mark.asyncio
    async def test_empty_portal_terminates(self, patched_fetch):
        session = FakeSession(total=0)
        pages = [p async for p, _, _ in enumerate_pages(session)]
        assert pages == [1]


# -- coverage accounting -------------------------------------------------


class TestFullSweepStats:
    def test_coverage_is_distinct_rows_over_the_portals_final_total(self):
        s = FullSweepStats(expected_total=1000, final_total=1000)
        s.rows_seen = s.rows_distinct = 1000
        assert s.coverage == 1.0
        assert s.is_complete is True

    def test_bids_closing_mid_walk_do_not_count_against_coverage(self):
        # The list was 1,000 rows when the walk began and 950 when it ended:
        # 50 bids closed under it. Under the stable sort they left from the
        # tail, so the 950 rows read are every row that is still live.
        s = FullSweepStats(expected_total=1000, final_total=950)
        s.rows_seen = s.rows_distinct = 950
        assert s.closed_during_sweep == 50
        assert s.coverage == 1.0
        assert s.is_complete is True

    def test_a_row_seen_twice_is_counted_once(self):
        # A bid published behind the walker pushes one row onto the next
        # page, where it is read again. That must not be able to make 990
        # distinct rows look like 1,000.
        s = FullSweepStats(expected_total=1000, final_total=1000)
        s.rows_seen = 1000
        s.rows_distinct = 990
        assert s.coverage == 0.99
        assert s.is_complete is False

    def test_a_single_failed_page_makes_the_sweep_incomplete(self):
        # Strict on purpose. "Complete" is a claim about every live bid having
        # been examined; one lost page means it was not, and a run that says
        # complete with a hole in it is worse than one that admits a gap.
        s = FullSweepStats(expected_total=1000, final_total=1000)
        s.rows_seen = s.rows_distinct = 1000
        s.pages_failed = 1
        assert s.coverage == 1.0
        assert s.is_complete is False

    def test_short_read_is_incomplete(self):
        s = FullSweepStats(expected_total=1000)
        s.rows_seen = s.rows_distinct = 900
        assert s.coverage == 0.9
        assert s.is_complete is False

    def test_coverage_is_none_when_the_portal_stated_no_total(self):
        assert FullSweepStats().coverage is None
        assert FullSweepStats().is_complete is False


# -- the fetcher ---------------------------------------------------------


class TestGemFullFetcher:
    @pytest.mark.asyncio
    async def test_filters_on_the_structured_ministry_field(self, patched_fetch, monkeypatch):
        # Odd pages are Railways, even pages are Defence.
        session = FakeSession(
            total=60,
            ministry_of_page=lambda p: "Ministry of Railways" if p % 2 else "Ministry of Defence",
        )
        fetcher = GemFullFetcher(session)
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": ["Ministry of Railways"],
            "with_details": False,
        })]
        kept = [t for b in batches for t in b.tenders]
        assert kept, "railway rows should survive the filter"
        assert len(kept) == 30
        assert fetcher.stats.rows_seen == 60
        assert fetcher.stats.rows_in_scope == 30

    @pytest.mark.asyncio
    async def test_empty_ministry_list_keeps_everything(self, patched_fetch):
        session = FakeSession(total=30, ministry_of_page=lambda p: "Ministry of Coal")
        fetcher = GemFullFetcher(session)
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": False,
        })]
        assert sum(len(b.tenders) for b in batches) == 30

    @pytest.mark.asyncio
    async def test_duplicate_bid_numbers_are_counted_not_shipped_twice(
        self, patched_fetch, monkeypatch
    ):
        async def repeating(session, page, search="", sort="", ministry=None):
            session.calls.append((page, search, sort))
            if page > 3:
                return 30, []
            return 30, [_doc(1)] * 10   # the same bid, over and over

        monkeypatch.setattr(gem_full, "fetch_page", repeating)
        session = FakeSession(total=30)
        fetcher = GemFullFetcher(session)
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": False,
        })]
        assert sum(len(b.tenders) for b in batches) == 1
        assert fetcher.stats.duplicates == 29

    @pytest.mark.asyncio
    async def test_incremental_mode_uses_newest_first_and_stops_on_known(
        self, patched_fetch
    ):
        # Newest-first is correct *here* and wrong for a full walk: the
        # incremental pass stops on known ground after a few pages, so a row
        # that shifts under it is at worst seen twice, never skipped.
        session = FakeSession(total=5000)
        known = {f"GEM/2026/B/{7000000 + i}" for i in range(5000)}
        fetcher = GemFullFetcher(session)
        batches = [b async for b in fetcher.sweep(known, {
            "mode": "incremental", "terms": ["railway", "bogie"],
            "ministries": [], "with_details": False,
        })]
        assert {sort for _, _, sort in session.calls} == {FRESH_SORT}
        # stop_after_known is 30, i.e. three clean pages per term. Two terms
        # against a 500-page list must cost six pages, not a thousand.
        assert len(batches) == 6, f"walked {len(batches)} pages before stopping"

    @pytest.mark.asyncio
    async def test_later_terms_stop_on_rows_this_run_already_collected(
        self, patched_fetch
    ):
        # Terms overlap almost completely. With an empty ledger the first term
        # collects everything and every later term sees only duplicates -- if
        # a duplicate does not count as known ground, the stop condition never
        # fires again and each later term walks to its page cap.
        session = FakeSession(total=5000)
        fetcher = GemFullFetcher(session)
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "incremental", "terms": ["railway", "bogie", "axle"],
            "ministries": [], "with_details": False,
        })]
        first_term_pages = sum(1 for b in batches if b.term == "railway")
        later_pages = sum(1 for b in batches if b.term in ("bogie", "axle"))
        assert later_pages <= 8, (
            f"later terms walked {later_pages} pages of duplicates"
        )
        assert first_term_pages > later_pages

    @pytest.mark.asyncio
    async def test_a_list_that_shrinks_mid_walk_is_complete_not_failed(
        self, patched_fetch, monkeypatch
    ):
        # numFound says 100 on page 1; from page 6 the portal says 60 and the
        # pages past it come back empty. Those are bids that closed, not pages
        # that failed, and the walk examined every row still live. The first
        # version counted them as 18 failed pages on a real sweep and refused
        # to call a 99.6% walk complete.
        async def shrinking(session, page, search="", sort="", ministry=None):
            session.calls.append((page, search, sort))
            total = 100 if page < 6 else 60
            start = (page - 1) * 10
            if start >= total:
                return total, []
            return total, [_doc(i) for i in range(start, start + 10)]

        monkeypatch.setattr(gem_full, "fetch_page", shrinking)
        fetcher = GemFullFetcher(FakeSession(total=100))
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": False,
        })]
        assert fetcher.stats.pages_failed == 0
        assert fetcher.stats.expected_total == 100
        assert fetcher.stats.final_total == 60
        assert fetcher.stats.closed_during_sweep == 40
        assert fetcher.stats.rows_distinct == 60
        assert fetcher.stats.coverage == 1.0
        assert fetcher.stats.is_complete is True
        assert len(batches) == 6

    @pytest.mark.asyncio
    async def test_full_mode_walks_the_terms_after_the_enumeration(
        self, patched_fetch, monkeypatch
    ):
        # The enumeration sees 30 rows. The term "railway" also returns one
        # the enumeration never saw -- a row that moved up a page when a bid
        # behind the walker closed. The repair pass finds it, counts it, and
        # leaves the enumeration's coverage figure alone.
        listing = patched_fetch

        async def with_a_shifted_row(session, page, search="", sort="", ministry=None):
            total, docs = await listing(session, page, search, sort)
            if search == "railway" and page == 3:
                docs = docs + [_doc(777)]
            return total, docs

        monkeypatch.setattr(gem_full, "fetch_page", with_a_shifted_row)
        session = FakeSession(total=30)
        fetcher = GemFullFetcher(session)
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": ["Ministry of Railways"],
            "terms": ["railway"], "with_details": False,
        })]
        ids = {t["tenderId"] for b in batches for t in b.tenders}
        assert "GEM/2026/B/7000777" in ids
        assert len(ids) == 31
        assert fetcher.stats.recovered == 1
        assert fetcher.stats.repair_pages == 3
        assert fetcher.stats.rows_seen == 30, "the term pass is not the enumeration"
        assert fetcher.stats.rows_distinct == 30
        assert fetcher.stats.coverage == 1.0
        assert {sort for _, _, sort in session.calls} == {STABLE_SORT}
        assert [b.term for b in batches].count(None) == 3
        assert [b.term for b in batches].count("railway") == 3

    @pytest.mark.asyncio
    async def test_the_repair_pass_never_stops_on_known_ground(
        self, patched_fetch, monkeypatch
    ):
        # Everything the term returns is already known -- except one row on
        # its last page. An incremental-style stop after 30 known rows would
        # never reach it, which is the whole reason this pass walks to the end.
        listing = patched_fetch

        async def last_page_has_it(session, page, search="", sort="", ministry=None):
            total, docs = await listing(session, page, search, sort)
            if search and page == 100:
                docs = docs + [_doc(7777)]
            return total, docs

        monkeypatch.setattr(gem_full, "fetch_page", last_page_has_it)
        fetcher = GemFullFetcher(FakeSession(total=1000))
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": ["Ministry of Railways"],
            "terms": ["railway"], "with_details": False,
        })]
        assert fetcher.stats.repair_pages == 100
        assert fetcher.stats.recovered == 1
        assert any(t["tenderId"] == "GEM/2026/B/7007777"
                   for b in batches for t in b.tenders)

    @pytest.mark.asyncio
    async def test_no_ministry_filter_means_no_repair_pass(self, patched_fetch):
        # With nothing filtered out, the enumeration already kept every row.
        session = FakeSession(total=30)
        fetcher = GemFullFetcher(session)
        async for _ in fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "terms": ["railway"],
            "with_details": False,
        }):
            pass
        assert {search for _, search, _ in session.calls} == {""}
        assert fetcher.stats.repair_pages == 0

    @pytest.mark.asyncio
    async def test_sweep_reports_the_portal_total(self, patched_fetch):
        session = FakeSession(total=250)
        fetcher = GemFullFetcher(session)
        async for _ in fetcher.sweep(set(), {"mode": "full", "ministries": [],
                                             "with_details": False}):
            pass
        assert fetcher.stats.expected_total == 250
        assert fetcher.stats.rows_seen == 250
        assert fetcher.stats.is_complete is True


# -- the detail stage runs behind the walk, not inside it ----------------


async def _collect(agen) -> list:
    return [b async for b in agen]


class TestPipelinedDetails:
    """The page walk and the document reads overlap.

    What has to stay true while they do: batches arrive in page order, each
    carrying its own documents; the walk cannot race unboundedly ahead of the
    batches it has yet to ship; one semaphore bounds the reads for the whole
    sweep; and closing the sweep early stops reads nobody is waiting for.
    """

    @pytest.mark.asyncio
    async def test_the_walk_does_not_wait_for_the_detail_stage(
        self, patched_fetch, monkeypatch
    ):
        # Page 1's documents cannot be read until the walk has reached page 12.
        # A detail stage run inline per page never gets there -- the walk is
        # parked behind the read -- so page 1 would time out and ship
        # unenriched. Measured on the live portal, that inline design made a
        # full sweep take three times as long as the walk alone.
        session = FakeSession(total=200)
        reached_page_12 = asyncio.Event()
        listing = patched_fetch

        async def gated(s, page, search="", sort="", ministry=None):
            if page >= 12:
                reached_page_12.set()
            return await listing(s, page, search, sort)

        monkeypatch.setattr(gem_full, "fetch_page", gated)

        async def read(session, docs, tenders, *, semaphore=None, **_):
            if docs[0]["b_id"][0] == 9000000:            # page 1's batch
                await asyncio.wait_for(reached_page_12.wait(), timeout=2)
            for t in tenders:
                t["estimatedValue"] = 1.0
            return DetailResult()

        monkeypatch.setattr(gem_detail, "enrich_with_details", read)

        fetcher = GemFullFetcher(session)
        batches = await asyncio.wait_for(_collect(fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": True,
        })), timeout=10)
        assert [b.page for b in batches] == list(range(1, 21))
        assert all(t["estimatedValue"] == 1.0 for t in batches[0].tenders)

    @pytest.mark.asyncio
    async def test_batches_arrive_in_page_order_with_their_own_documents(
        self, patched_fetch, monkeypatch
    ):
        # Reads finish in a scrambled order; batches must not, and each must
        # carry the documents that were read for it, not a neighbour's.
        async def read(session, docs, tenders, *, semaphore=None, **_):
            await asyncio.sleep(0.001 * (docs[0]["b_id"][0] % 5))
            for t in tenders:
                t["estimatedValue"] = float(docs[0]["b_id"][0])
            return DetailResult()

        monkeypatch.setattr(gem_detail, "enrich_with_details", read)
        fetcher = GemFullFetcher(FakeSession(total=150))
        batches = await _collect(fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": True,
        }))
        assert [b.page for b in batches] == list(range(1, 16))
        for b in batches:
            first_id = 9000000 + (b.page - 1) * 10
            assert all(t["estimatedValue"] == float(first_id) for t in b.tenders), b.page

    @pytest.mark.asyncio
    async def test_document_reads_share_one_semaphore_across_batches(
        self, patched_fetch, monkeypatch
    ):
        # A semaphore made per batch would multiply the configured bound by
        # however many batches are reading at once.
        seen: list = []

        async def read(session, docs, tenders, *, semaphore=None, **_):
            seen.append(semaphore)
            return DetailResult()

        monkeypatch.setattr(gem_detail, "enrich_with_details", read)
        fetcher = GemFullFetcher(FakeSession(total=50))
        await _collect(fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": True,
        }))
        assert len(seen) == 5
        assert isinstance(seen[0], asyncio.Semaphore)
        assert all(s is seen[0] for s in seen)

    @pytest.mark.asyncio
    async def test_the_walk_waits_when_the_lookahead_is_full(
        self, patched_fetch, monkeypatch
    ):
        # Every read blocks. With the lookahead at 2, the walk may run only a
        # couple of batches past the oldest unread one before it has to wait;
        # otherwise a slow document endpoint lets the walk race the whole
        # portal ahead and pile up unshipped batches in memory.
        import collector.config as cfg

        monkeypatch.setenv("GEM_DETAIL_LOOKAHEAD", "2")
        monkeypatch.setenv("GEM_CONCURRENCY", "1")
        monkeypatch.setenv("GEM_PAGE_DELAY_SECONDS", "0")
        cfg.get_settings.cache_clear()

        release = asyncio.Event()

        async def read(session, docs, tenders, *, semaphore=None, **_):
            await release.wait()
            return DetailResult()

        monkeypatch.setattr(gem_detail, "enrich_with_details", read)
        session = FakeSession(total=300)
        fetcher = GemFullFetcher(session)
        collecting = asyncio.create_task(_collect(fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": True,
        })))
        await asyncio.sleep(0.05)
        walked = len(session.calls)
        assert walked < 30, f"walk ran {walked} pages ahead of the unread batches"
        assert walked <= 2 + 3, f"walk ran {walked} pages with a lookahead of 2"

        release.set()
        batches = await asyncio.wait_for(collecting, timeout=5)
        assert [b.page for b in batches] == list(range(1, 31))

    @pytest.mark.asyncio
    async def test_closing_the_sweep_early_cancels_the_reads_in_flight(
        self, patched_fetch, monkeypatch
    ):
        # A cancelled run, or a consumer that stopped iterating: the reads
        # still going are for batches nobody will receive.
        cancelled = asyncio.Event()

        async def read(session, docs, tenders, *, semaphore=None, **_):
            if docs[0]["b_id"][0] == 9000000:
                return DetailResult()                   # page 1 is instant
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return DetailResult()

        monkeypatch.setattr(gem_detail, "enrich_with_details", read)
        fetcher = GemFullFetcher(FakeSession(total=100))
        sweep = fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": True,
        })
        first = await sweep.__anext__()
        assert first.page == 1
        await sweep.aclose()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert cancelled.is_set(), "a read for an unshipped batch kept running"


# -- several sessions ----------------------------------------------------


class TestSessionPool:
    """GeM serves about four requests at a time per session cookie and queues
    the rest. Measured 2026-09-12: six concurrent pages on one session, four
    back in 0.5 s and two in 1.6 s; the same six on six sessions, all back in
    0.5 s. So the walk spreads its pages over a small pool, and the PDF reads
    get a session of their own instead of queueing behind the walk."""

    @pytest.mark.asyncio
    async def test_pages_are_spread_over_the_walk_sessions(self, patched_fetch, monkeypatch):
        import collector.config as cfg

        monkeypatch.setenv("GEM_SESSIONS", "4")
        cfg.get_settings.cache_clear()
        extras: list[FakeSession] = []

        async def fake_bootstrap():
            extras.append(FakeSession(total=120))
            return extras[-1]

        monkeypatch.setattr(gem_full, "bootstrap", fake_bootstrap)
        primary = FakeSession(total=120)
        fetcher = GemFullFetcher(primary)
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": False,
        })]
        assert [b.page for b in batches] == list(range(1, 13))
        assert len(extras) == 3, "three extras: two for the walk, one for the documents"
        per_session = [sorted(p for p, _, _ in s.calls) for s in (primary, *extras[:2])]
        assert all(per_session), f"every walk session fetched pages: {per_session}"
        assert sum(len(p) for p in per_session) == 12, "each page fetched exactly once"
        assert extras[2].calls == [], "the detail session never walks"
        await fetcher.aclose()
        assert all(s.closed for s in extras), "the pool is closed with the fetcher"

    @pytest.mark.asyncio
    async def test_documents_are_read_on_their_own_session(self, patched_fetch, monkeypatch):
        import collector.config as cfg

        monkeypatch.setenv("GEM_SESSIONS", "2")
        cfg.get_settings.cache_clear()
        detail_session = FakeSession(total=30)

        async def fake_bootstrap():
            return detail_session

        monkeypatch.setattr(gem_full, "bootstrap", fake_bootstrap)
        used: list = []

        async def read(session, docs, tenders, *, semaphore=None, **_):
            used.append(session)
            return DetailResult()

        monkeypatch.setattr(gem_detail, "enrich_with_details", read)
        primary = FakeSession(total=30)
        fetcher = GemFullFetcher(primary)
        await _collect(fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": True,
        }))
        assert used and all(s is detail_session for s in used)
        assert detail_session.calls == [], "and the walk stayed on the primary"
        assert len(primary.calls) == 3

    @pytest.mark.asyncio
    async def test_an_extra_session_that_fails_to_open_is_done_without(
        self, patched_fetch, monkeypatch
    ):
        import collector.config as cfg

        monkeypatch.setenv("GEM_SESSIONS", "4")
        cfg.get_settings.cache_clear()

        async def refused():
            raise PortalUnavailable("WAF said no")

        monkeypatch.setattr(gem_full, "bootstrap", refused)
        fetcher = GemFullFetcher(FakeSession(total=50))
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": [], "with_details": False,
        })]
        assert len(batches) == 5
        assert fetcher.stats.is_complete is True, "slower, not failed"


# -- the ministry-filtered sweep -----------------------------------------


class TestMinistryMode:
    """GeM's own ministry filter, and the convergence that makes it safe.

    The endpoint ignores the sort key and pages unstably: measured live on
    2026-09-22, one pass returned 1,426 of 1,703 distinct bids and four passes
    reached exactly 1,703. So the mode is written as "repeat until the distinct
    count reaches the portal's own numFound", and these tests are about that
    loop -- that it converges, that it stops, and above all that it never
    reports complete when it did not get there.
    """

    @staticmethod
    def _lossy(total: int, *, drop_every: int = 4, stable_loss: bool = False):
        """A fetch_page that under-serves each pass, the way /search-bids does.

        ``numFound`` is always the honest total; the rows actually served drop
        one in ``drop_every``. With ``stable_loss`` the same rows are dropped
        every pass, so the union can never reach numFound -- which is the case
        the settle guard exists for.
        """
        ids = list(range(total))
        state = {"pass_no": 0}

        async def fake(session, page, search="", sort="", ministry=None):
            session.calls.append((page, search, sort))
            session.ministry_calls.append(ministry)
            if page == 1:
                state["pass_no"] += 1
            offset = 0 if stable_loss else state["pass_no"]
            served = [i for n, i in enumerate(ids) if (n + offset) % drop_every]
            start = (page - 1) * 10
            rows = [_doc(i) for i in served[start:start + 10]]
            return total, rows

        return fake

    @pytest.mark.asyncio
    async def test_it_asks_gem_to_do_the_filtering(self, patched_fetch):
        session = FakeSession(total=30)
        fetcher = GemFullFetcher(session)
        [b async for b in fetcher.sweep(set(), {
            "mode": "ministry", "ministries": ["Ministry of Railways"],
            "with_details": False,
        })]
        assert session.ministry_calls, "no page was fetched"
        assert set(session.ministry_calls) == {"Ministry of Railways"}

    @pytest.mark.asyncio
    async def test_full_mode_still_asks_for_no_ministry(self, patched_fetch):
        """The cheap path must not leak into the mode that audits the corpus."""
        session = FakeSession(total=30)
        fetcher = GemFullFetcher(session)
        [b async for b in fetcher.sweep(set(), {
            "mode": "full", "ministries": ["Ministry of Railways"],
            "with_details": False,
        })]
        assert set(session.ministry_calls) == {None}

    @pytest.mark.asyncio
    async def test_it_repeats_until_it_reaches_gems_own_count(
        self, patched_fetch, monkeypatch
    ):
        monkeypatch.setattr(gem_full, "fetch_page", self._lossy(200))
        session = FakeSession(total=200)
        fetcher = GemFullFetcher(session)
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "ministry", "ministries": ["Ministry of Railways"],
            "with_details": False,
        })]
        assert fetcher.stats.rows_distinct == 200, "did not converge on numFound"
        assert fetcher.stats.coverage == 1.0
        assert fetcher.stats.is_complete
        assert sum(len(b.tenders) for b in batches) == 200

    @pytest.mark.asyncio
    async def test_one_pass_alone_would_not_have_been_enough(
        self, patched_fetch, monkeypatch
    ):
        """Pins the reason the loop exists rather than assuming it."""
        fake = self._lossy(200)
        monkeypatch.setattr(gem_full, "fetch_page", fake)
        session = FakeSession(total=200)
        seen = set()
        async for page, docs, total in gem_full.enumerate_pages(
            session, ministry="Ministry of Railways", concurrency=4
        ):
            for d in docs or []:
                seen.add(d["b_bid_number"][0])
        assert 0 < len(seen) < 200

    @pytest.mark.asyncio
    async def test_it_stops_and_admits_it_when_the_count_is_unreachable(
        self, patched_fetch, monkeypatch
    ):
        """The failure that matters: short of numFound, and honest about it.

        A mode whose denominator is the portal's own count cannot quietly
        under-collect -- but only if falling short is reported as falling
        short rather than rounded up to done.
        """
        monkeypatch.setattr(gem_full, "fetch_page", self._lossy(200, stable_loss=True))
        session = FakeSession(total=200)
        fetcher = GemFullFetcher(session)
        [b async for b in fetcher.sweep(set(), {
            "mode": "ministry", "ministries": ["Ministry of Railways"],
            "with_details": False,
        })]
        assert fetcher.stats.rows_distinct == 150
        assert fetcher.stats.coverage == 0.75
        assert not fetcher.stats.is_complete

    @pytest.mark.asyncio
    async def test_it_does_not_spend_the_whole_ceiling_on_a_settled_sweep(
        self, patched_fetch, monkeypatch
    ):
        """Stopping early is the point of the settle guard."""
        monkeypatch.setattr(gem_full, "fetch_page", self._lossy(200, stable_loss=True))
        session = FakeSession(total=200)
        fetcher = GemFullFetcher(session)
        [b async for b in fetcher.sweep(set(), {
            "mode": "ministry", "ministries": ["Ministry of Railways"],
            "with_details": False,
        })]
        s = get_settings()
        passes = session.calls.count((1, "", STABLE_SORT))
        assert passes < s.gem_ministry_max_passes
        assert passes <= 1 + s.gem_ministry_settle_passes

    @pytest.mark.asyncio
    async def test_a_ministry_sweep_needs_exactly_one_ministry(self, patched_fetch):
        """GeM's filter takes one. Failing loudly beats filtering by the first."""
        fetcher = GemFullFetcher(FakeSession(total=30))
        with pytest.raises(ValueError, match="needs a ministry"):
            [b async for b in fetcher.sweep(set(), {
                "mode": "ministry", "ministries": [], "with_details": False,
            })]
        fetcher = GemFullFetcher(FakeSession(total=30))
        with pytest.raises(ValueError, match="exactly one ministry"):
            [b async for b in fetcher.sweep(set(), {
                "mode": "ministry",
                "ministries": ["Ministry of Railways", "Ministry of Coal"],
                "with_details": False,
            })]

    @pytest.mark.asyncio
    async def test_the_scope_check_still_runs_underneath(
        self, patched_fetch, monkeypatch
    ):
        """Defence in depth: trust GeM's filter, verify it anyway.

        The filter and the scope check read the same field, so agreement is
        the expected case -- and a row that disagrees is drift, not a tender.
        """
        async def wrong_ministry(session, page, search="", sort="", ministry=None):
            session.calls.append((page, search, sort))
            session.ministry_calls.append(ministry)
            if page > 1:
                return 10, []
            return 10, [_doc(i, "Ministry of Coal") for i in range(10)]

        monkeypatch.setattr(gem_full, "fetch_page", wrong_ministry)
        fetcher = GemFullFetcher(FakeSession(total=10))
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "ministry", "ministries": ["Ministry of Railways"],
            "with_details": False,
        })]
        assert sum(len(b.tenders) for b in batches) == 0

    @pytest.mark.asyncio
    async def test_a_ministry_with_no_live_bids_does_not_claim_completeness(
        self, patched_fetch, monkeypatch
    ):
        """0/0 is undefined, and undefined must not round up to done.

        A genuinely empty ministry and a WAF hiccup that answers numFound 0
        look identical from here. Reporting "complete, nothing found" would be
        right for the first and badly wrong for the second, so the sweep
        reports no coverage and the caller's exit code says to look.
        """
        async def empty(session, page, search="", sort="", ministry=None):
            session.calls.append((page, search, sort))
            session.ministry_calls.append(ministry)
            return 0, []

        monkeypatch.setattr(gem_full, "fetch_page", empty)
        fetcher = GemFullFetcher(FakeSession(total=0))
        batches = [b async for b in fetcher.sweep(set(), {
            "mode": "ministry", "ministries": ["Ministry of Railways"],
            "with_details": False,
        })]
        assert sum(len(b.tenders) for b in batches) == 0
        assert fetcher.stats.coverage is None
        assert not fetcher.stats.is_complete
