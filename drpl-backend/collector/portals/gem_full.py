"""
DRPL Collector - complete enumeration of GeM's live bids.

THE PROBLEM THIS REPLACES -- AND THE ONE IT DOES NOT
----------------------------------------------------
It is tempting to say the keyword sweep in ``gem.py`` misses tenders because a
word list cannot describe a ministry. Measured against the live portal on
2026-09-12, that is **false**, and worth recording so nobody re-derives it: a
7,000-bid slice of the unfiltered list contained 163 Ministry of Railways bids,
and a complete sweep of the configured terms found all 163. Nothing was missed
for vocabulary reasons. GeM's full-text index covers the buyer's organisation
name, so "railway" and "indian railways" reach bids for multimedia projectors
and cleaning contracts alike.

The real defect is the page cap, and it is severe. ``gem_max_pages_per_term``
is 40, i.e. 400 rows per term, but the terms return far more than that:

    term                numFound   pages   reachable at 40 pages
    railway                1,812     182                     400
    indian railways        2,351     236                     400
    AMC                      933      94                     400
    annual maintenance       839      84                     400

On a first run the ledger is empty, so the stop-on-known condition never fires
and each term simply halts at page 40. Distinct Ministry of Railways bids
reachable that way: **468**. Reachable with no cap: **1,788**. The cap alone
puts **1,320 live railway tenders -- 73.8% -- out of reach**, and it does it
silently, because a sweep that stops at its own cap looks exactly like a sweep
that finished.

THE MEASUREMENT THAT SETTLES IT
-------------------------------
``POST /all-bids-data`` with an empty ``searchBid`` returns every live bid, and
it paginates all the way down. Verified against the live portal on 2026-09-12:

    numFound           47,246
    page 1             10 docs
    page 4,700         10 docs
    page 4,725          6 docs   <- the last page, and the arithmetic agrees
    page 4,800          0 docs
    per-page latency   0.2 - 0.6 s

So the whole corpus is 4,725 pages, and at six concurrent requests that is
roughly fifteen minutes. The ministry filter then runs on GeM's own structured
``ba_official_details_minName`` field. Coverage stops being a claim and becomes
arithmetic: every live bid was examined, and the ones kept are the ones whose
stated ministry matched. That is the property ``full`` mode exists for -- not
that keywords are blind, but that a cap is invisible and a page count is not.

SORT ORDER IS THE CORRECTNESS BUG NOBODY SEES
---------------------------------------------
Paginating a live list is quietly lossy whenever a row the walker has already
passed disappears: every row behind it moves up one place, the row that was
first on the next page is now last on the page just read, and it is skipped --
silently, with no gap, no error, and a numFound that still looks right.

Three orders were measured on 2026-09-12, against a term sweep of the whole
ministry taken straight after each walk:

    order                         live railway tenders the walk never saw
    Bid-Start-Date-Latest         (not walkable: every new bid shifts the
                                   rows under the walker)
    Bid-Start-Date-Oldest         4 of 1,785 -- all published days earlier,
                                   i.e. rows that moved up when 185 bids
                                   closed under the walk
    Bid-End-Date-Latest           287 of 1,779

The end-date order looked right on paper: the bid closing soonest is the last
row, so a closure leaves from the tail and shifts nothing the walker has read.
It lost 16% in practice because end dates cluster at round times, so that
order has tie groups thousands of rows wide, and GeM re-indexes a bid whenever
anything about it changes -- which moves it to the end of its tie group and
shifts every row between. Start dates are unique to the second, so under
``Bid-Start-Date-Oldest`` a re-index moves a row within a group of one or two
and nothing crosses a page boundary.

So the walk is oldest-start-first, and its one remaining loss -- a row that
moved up because a bid behind the walker closed -- is closed by a second,
cheap pass: after the enumeration, ``full`` mode walks the configured search
terms to the end, without stopping on known ground, and every in-scope tender
that pass finds which the enumeration did not is counted as ``recovered``.
That number is reported every run. It is the enumeration's audit as much as
its safety net: when it stays at a handful, the walk is doing what this
docstring says it does.

Coverage is measured as distinct rows the enumeration examined over the
portal's final total, so neither a row seen twice nor a bid that closed
mid-walk can flatter it.

Incremental runs use newest-first, because there the drift is harmless: the
walk stops on known ground after a few pages, and a shifted row is at worst
seen twice. Seeing a tender twice costs a dictionary lookup. Not seeing it
costs the tender.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Sequence

from collector.config import get_settings
from collector.portals.base import Batch, ParserDrift, PortalUnavailable
from collector.portals.gem import (
    PORTAL,
    GemSession,
    _first,
    bootstrap,
    fetch_page,
    in_scope,
    to_tender,
)

logger = logging.getLogger(__name__)

__all__ = ["COMPLETE_COVERAGE", "FullSweepStats", "GemFullFetcher", "enumerate_pages"]

#: Oldest-start-first: the order with the smallest tie groups, which is what
#: keeps a live index's churn from crossing page boundaries. See the module
#: docstring -- this is measured, not a preference, and the intuitive
#: alternative (soonest-closing last) lost 16% of the ministry in one walk.
STABLE_SORT = "Bid-Start-Date-Oldest"
#: Distinct live rows examined, as a fraction of the portal's final total,
#: below which a full walk does not get to call itself complete. Not 1.0:
#: a bid published behind the walker while it ran is live, unexamined, and
#: not a hole in the walk -- the next incremental run finds it at the front.
#: On a weekday afternoon that is a few dozen rows in 47,000.
COMPLETE_COVERAGE = 0.995
#: Newest-first, for the incremental path that stops early on purpose.
FRESH_SORT = "Bid-Start-Date-Latest"


@dataclass
class FullSweepStats:
    """What a full enumeration saw. Reported so coverage is auditable."""

    #: numFound on the first page: how long the list was when the walk began.
    expected_total: Optional[int] = None
    #: numFound on the last page read: how long it was when the walk ended.
    #: Lower than expected_total by the number of bids that closed meanwhile.
    final_total: Optional[int] = None
    pages_fetched: int = 0
    pages_failed: int = 0
    rows_seen: int = 0
    #: Distinct bid numbers among rows_seen. A row pushed down by a bid
    #: published behind the walker is seen twice; this does not count it twice.
    rows_distinct: int = 0
    rows_in_scope: int = 0
    duplicates: int = 0
    #: The term pass that follows a full enumeration. ``recovered`` is the
    #: number of in-scope tenders it found that the enumeration had not: the
    #: rows that moved up a page when a bid behind the walker closed.
    repair_pages: int = 0
    repair_pages_failed: int = 0
    repair_rows: int = 0
    recovered: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def live_total(self) -> Optional[int]:
        """The portal's total as of the end of the walk, or the start if unknown."""
        return self.final_total if self.final_total is not None else self.expected_total

    @property
    def closed_during_sweep(self) -> int:
        if self.expected_total is None or self.final_total is None:
            return 0
        return max(0, self.expected_total - self.final_total)

    @property
    def coverage(self) -> Optional[float]:
        """Fraction of the rows live at the end of the walk that it examined."""
        live = self.live_total
        if not live:
            return None
        return round(min(1.0, self.rows_distinct / live), 4)

    def summary(self) -> dict:
        return {
            "expected_total": self.expected_total,
            "final_total": self.final_total,
            "closed_during_sweep": self.closed_during_sweep,
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "rows_seen": self.rows_seen,
            "rows_distinct": self.rows_distinct,
            "rows_in_scope": self.rows_in_scope,
            "duplicates": self.duplicates,
            "coverage": self.coverage,
            "repair_pages": self.repair_pages,
            "repair_pages_failed": self.repair_pages_failed,
            "repair_rows": self.repair_rows,
            "recovered": self.recovered,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }

    @property
    def is_complete(self) -> bool:
        """True when the sweep can honestly claim it missed nothing.

        Deliberately strict about failures. A single failed page means some
        bids were never examined, and a run that reports "complete" with a
        hole in it is worse than one that reports "partial" -- the whole point
        of enumerating is to be able to say the word without qualifying it.
        The coverage bound is what a live list allows; see COMPLETE_COVERAGE.
        """
        return self.pages_failed == 0 and (self.coverage or 0) >= COMPLETE_COVERAGE

async def enumerate_pages(
    session: GemSession,
    *,
    search: str = "",
    sort: str = STABLE_SORT,
    concurrency: Optional[int] = None,
    max_pages: Optional[int] = None,
    start_page: int = 1,
    sessions: Optional[Sequence[GemSession]] = None,
    ministry: Optional[str] = None,
) -> AsyncIterator[tuple[int, Optional[list[dict]], Optional[int]]]:
    """Walk the bid list, yielding ``(page, docs, numFound)``.

    ``sessions`` spreads the pages round-robin over several independent
    portal sessions. The portal serves about four requests at a time per
    session and queues the rest, so six pages in flight on one session run
    at the speed of four; on three sessions they run at the speed of six.
    ``session`` is always the first of the pool.

    ``concurrency`` pages are in flight at all times and pages are yielded in
    order, so a consumer that writes a ledger sees a monotonic page sequence
    and can record where it got to. Rolling rather than windowed on purpose:
    the first version gathered pages in windows, which made every window as
    slow as its slowest request, and GeM's latency varies enough within one
    afternoon that the walk ran well below the rate the concurrency implied.

    ``docs`` is ``None`` for a page that failed every retry and ``[]`` for a
    page past the end of the list. The two must not be conflated: the first is
    a hole in coverage, the second is what a live list does when bids close
    during the walk, and the caller counts them differently. ``numFound`` is
    the total the portal reported with that page, so a consumer can watch the
    list shrink or grow while it walks.
    """
    s = get_settings()
    window = max(1, concurrency or s.gem_concurrency)
    pool: list[GemSession] = [session] + [x for x in (sessions or ()) if x is not session]

    # First page alone: it tells us how many there are.
    total, docs = await fetch_page(
        session, start_page, search=search, sort=sort, ministry=ministry)
    if not total:
        yield start_page, docs, total
        return
    last_page = (total + s.gem_page_size - 1) // s.gem_page_size
    if max_pages:
        last_page = min(last_page, start_page + max_pages - 1)

    async def attempt(page: int) -> tuple[int, Optional[list[dict]], Optional[int]]:
        sess = pool[page % len(pool)]
        try:
            found, rows = await fetch_page(
                sess, page, search=search, sort=sort, ministry=ministry)
            return page, rows, found
        except PermissionError:
            # The CSRF token or WAF cookie went stale mid-sweep. One re-bootstrap
            # is worth it; a second refusal on the same page is the portal
            # telling us something, and we listen.
            logger.info("gem-full: session rejected on page %s -- re-bootstrapping", page)
            try:
                fresh = await bootstrap()
                sess.client = fresh.client
                sess.token = fresh.token
                found, rows = await fetch_page(
                    sess, page, search=search, sort=sort, ministry=ministry)
                return page, rows, found
            except Exception as e:  # noqa: BLE001
                logger.warning("gem-full: page %s lost after re-bootstrap: %s", page, e)
                return page, None, None
        except (PortalUnavailable, ParserDrift) as e:
            logger.warning("gem-full: page %s failed: %s", page, e)
            return page, None, None

    slots = asyncio.Semaphore(window)

    async def one(page: int) -> tuple[int, Optional[list[dict]], Optional[int]]:
        async with slots:
            result = await attempt(page)
            # The politeness pause is per slot: each slot rests after its own
            # page, so the aggregate rate is window / (latency + pause) and a
            # slow request holds up nothing but itself.
            if s.gem_page_delay_seconds:
                await asyncio.sleep(s.gem_page_delay_seconds)
            return result

    # Twice the window is in flight or finished-and-waiting at any time: the
    # window that is fetching, plus a window of completed pages queued behind
    # a straggler so the slots never idle while the head is still out.
    in_flight: deque[asyncio.Task] = deque()
    next_page = start_page + 1

    def top_up() -> None:
        nonlocal next_page
        while next_page <= last_page and len(in_flight) < window * 2:
            in_flight.append(asyncio.create_task(one(next_page)))
            next_page += 1

    # Fill the slots before handing over the first page, so the consumer's
    # work on it already overlaps with the pages behind it.
    top_up()
    try:
        yield start_page, docs, total
        while in_flight:
            page, rows, found = await in_flight.popleft()
            top_up()
            yield page, rows, (found if found is not None else total)
    finally:
        # Closed early. Pages still in flight are for nobody, so stop them --
        # but never swallow a cancellation aimed at the task we are running in.
        for task in in_flight:
            task.cancel()
        for task in in_flight:
            try:
                await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception:  # noqa: BLE001 - a discarded page's error is noise
                pass
        in_flight.clear()

class GemFullFetcher:
    """Complete enumeration, ministry-filtered, with the detail stage attached.

    Implements the same ``Fetcher`` protocol as ``GemFetcher``, so it drops into
    ``tasks.py`` unchanged and a collect run streams the same events.
    """

    portal = PORTAL
    version = "gem-full-v1"

    fill_baseline = {
        "tenderId": 1.0,
        "title": 0.95,
        "organisation": 0.9,
        "closingDate": 0.95,
    }

    def __init__(self, session: Optional[GemSession] = None) -> None:
        self._session = session
        self._extra: list[GemSession] = []
        self.stats = FullSweepStats()

    async def _ensure_session(self) -> GemSession:
        if self._session is None:
            self._session = await bootstrap()
        return self._session

    async def _open_pool(self, primary: GemSession) -> tuple[list[GemSession], GemSession]:
        """``(walk_sessions, detail_session)`` per ``gem_sessions``.

        The primary -- possibly a login borrowed from Chrome -- always walks.
        Extra sessions are plain anonymous bootstraps, which every endpoint
        here accepts. The last one is reserved for the detail stage so the
        PDF reads never queue behind the page walk on one session. An extra
        session that cannot be opened is logged and done without: fewer
        sessions is a slower sweep, not a failed one.
        """
        want = max(1, get_settings().gem_sessions)
        for _ in range(want - 1 - len(self._extra)):
            try:
                self._extra.append(await bootstrap())
            except Exception as e:  # noqa: BLE001 - degrade, never fail
                logger.warning("gem-full: could not open an extra session: %s", e)
                break
        sessions = [primary] + self._extra
        if len(sessions) == 1:
            return sessions, primary
        return sessions[:-1], sessions[-1]

    async def aclose(self) -> None:
        for extra in self._extra:
            try:
                await extra.aclose()
            except Exception:  # noqa: BLE001
                pass
        self._extra = []
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    async def sweep(
        self, known: set[str], params: dict[str, Any]
    ) -> AsyncIterator[Batch]:
        """Enumerate the portal and yield the in-scope tenders.

        The three modes are three different queries, not one query with a cap.

        **full** asks for the whole list, unsearched, oldest-start-first,
        then walks the configured terms to the end to recover the few rows a
        closing bid shifted past the walker. It is the only mode that can
        report coverage, and it is what a first run and the nightly pass
        should use.

        **incremental** narrows by search term, newest-first, and stops on
        known ground. That narrowing is not taken on faith. Enumerating a
        7,000-bid slice of the live portal on 2026-09-12 found 163 Ministry of
        Railways bids, and a complete term sweep found all 163 -- GeM's
        full-text index covers the buyer's organisation name, so "railway" and
        "indian railways" reach the whole ministry. Narrowing lifts the density
        of rows we care about from about 3% to nearly 100%, which is the
        difference between a button that answers in seconds and one that walks
        a thousand pages to reach the same tenders.

        The catch that makes ``full`` still necessary: the term sweep is only
        complete if it reads each term's result set to the end. "railway"
        returns ~1,800 rows and "indian railways" ~2,350, so a first run
        against an empty ledger has nothing to stop on and must walk them all.
        ``full`` does that with the stable sort order, which is why it is the
        right first run rather than merely a thorough one.

        **ministry** asks GeM to do the filtering. ``POST /search-bids`` --
        the portal's own Advanced Search -- takes a ministry name and answers
        an anonymous request, so "Ministry of Railways" is 171 pages instead
        of 4,378 for the identical set of tenders. It pages unstably, though
        (it ignores the sort key, and one pass returned 1,426 of 1,703 when
        measured), so the mode repeats the query until the distinct count
        reaches the portal's own ``numFound`` for that ministry. The
        denominator is GeM's, so falling short is reported as coverage below
        1 rather than rounded up to done. Cheap enough to run hourly; ``full``
        stays the nightly audit, because it is the only mode that reads bids
        whose ministry field GeM has filled in wrongly.

        ``params``:
          mode          "full" (default) | "incremental" | "ministry"
          terms         search terms for incremental; defaults to the
                        configured always-terms plus the fallback list
          ministries    override the ministry filter; [] keeps everything
                        (ministry mode needs exactly one and raises otherwise)
          terms         full: the repair pass's terms (default: configured)
          max_pages     per-query cap, for a smoke run
          with_details  fetch and parse each bid document (default on)
          concurrency   pages in flight
        """
        s = get_settings()
        mode = (params.get("mode") or "full").lower()
        ministries = params.get("ministries")
        if ministries is None:
            ministries = s.ministries
        max_pages = params.get("max_pages")
        with_details = params.get("with_details")
        if with_details is None:
            with_details = s.gem_fetch_details
        concurrency = params.get("concurrency")

        #: Set only in ministry mode: the name handed to GeM's own filter.
        ministry_query: Optional[str] = None

        if mode == "ministry":
            # One ministry per sweep, because the endpoint takes one. The
            # scope list stays in force underneath -- the filter and the check
            # read the same field, so agreement is the expected case and a
            # disagreement is drift worth dropping the row for.
            if not ministries:
                raise ValueError(
                    "gem-full: mode='ministry' needs a ministry to filter on; "
                    "pass ministries=['Ministry of Railways'] or use mode='full'"
                )
            if len(ministries) > 1:
                raise ValueError(
                    "gem-full: GeM's ministry filter takes exactly one ministry, "
                    f"got {ministries!r}. Run one sweep per ministry, or use "
                    "mode='full' which filters client-side and handles a list."
                )
            ministry_query = ministries[0]
            sort = STABLE_SORT          # ignored by /search-bids; harmless
            stop_after_known = 0
            # Each "term" is one convergence pass over the same query. The
            # loop breaks out as soon as the distinct count reaches GeM's
            # numFound, so this is a ceiling and not a plan.
            terms = [""] * max(1, s.gem_ministry_max_passes)
        elif mode == "incremental":
            sort = FRESH_SORT
            stop_after_known = s.gem_stop_after_known
            terms = [t for t in (params.get("terms") or []) if t]
            if not terms:
                terms = list(dict.fromkeys(s.always_terms + s.fallback_terms))
            # A backstop, not the plan: the stop-on-known condition is what
            # ends an incremental term normally. This only bounds the case
            # where a term genuinely has thousands of unseen rows, which means
            # the ledger is cold and `full` was the right mode anyway.
            if max_pages is None:
                max_pages = s.gem_max_pages_per_term
        else:
            sort = STABLE_SORT
            stop_after_known = 0
            terms = [""]           # one unsearched pass over the whole list
            # Then the repair pass: every configured term, walked to its end.
            # Pointless without a ministry filter (the enumeration already
            # kept everything) and off by a setting for an audit that wants
            # the bare enumeration's number.
            if ministries and s.gem_full_repair_pass:
                repair = [t for t in (params.get("terms") or []) if t]
                if not repair:
                    repair = list(dict.fromkeys(s.always_terms + s.fallback_terms))
                terms += repair

        session = await self._ensure_session()
        walk_sessions, detail_session = await self._open_pool(session)
        self.stats = FullSweepStats()
        seen_ids: set[str] = set()
        all_ids: set[str] = set()
        pages_done = 0
        passes_done = 0
        barren_passes = 0

        # The detail stage runs behind the walk, not inside it. Each batch's
        # documents are read by a task while the next pages are being fetched,
        # and batches are still yielded in page order once their documents are
        # in. One semaphore for the whole sweep holds the document concurrency
        # at its configured bound however many batches are reading at once.
        detail_sem = asyncio.Semaphore(max(1, s.gem_detail_concurrency))
        lookahead = max(1, s.gem_detail_lookahead)
        pending: deque[tuple[Batch, Optional[asyncio.Task]]] = deque()

        def reading() -> int:
            return sum(1 for _, t in pending if t is not None and not t.done())

        try:
            for term in terms:
                consecutive_known = 0
                caught_up = False
                ids_before_pass = len(all_ids)
                async with contextlib.aclosing(enumerate_pages(
                    session, search=term, sort=sort,
                    concurrency=concurrency, max_pages=max_pages,
                    sessions=walk_sessions, ministry=ministry_query,
                )) as walk:
                    # In full mode a searched term is the repair pass; its
                    # pages and rows are counted apart so the enumeration's
                    # coverage stays the enumeration's.
                    repairing = bool(term) and mode != "incremental"
                    async for page, docs, total in walk:
                        # numFound belongs to the query. Only the unsearched
                        # pass can speak for the portal, so only it sets the
                        # coverage denominator -- a term's total would make
                        # 1,800 rows look like complete coverage of 47,000.
                        if not term and self.stats.expected_total is None:
                            self.stats.expected_total = total

                        if docs is None:
                            # Failed every retry: a hole in coverage, and the
                            # one thing that must never be reported as success.
                            if repairing:
                                self.stats.repair_pages_failed += 1
                            else:
                                self.stats.pages_failed += 1
                            continue
                        if not term:
                            self.stats.final_total = total
                        if not docs:
                            # Past the end of the list: bids closed while we
                            # walked and the tail moved up to meet us. The
                            # rows that shifted are what the repair pass is
                            # for; the empty page itself is not a failure.
                            continue

                        if repairing:
                            self.stats.repair_pages += 1
                            self.stats.repair_rows += len(docs)
                        else:
                            self.stats.pages_fetched += 1
                            self.stats.rows_seen += len(docs)
                            for doc in docs:
                                bid_no = _first(doc, "b_bid_number")
                                if bid_no:
                                    all_ids.add(str(bid_no).strip())
                            self.stats.rows_distinct = len(all_ids)
                        pages_done += 1

                        batch = Batch(
                            portal=PORTAL,
                            page=page,
                            pages_done=pages_done,
                            rows_seen=len(docs),
                            expected_total=total,
                            rows_distinct=self.stats.rows_distinct or None,
                            term=term or None,
                            fetcher=self.version,
                            raw=docs,
                        )

                        for doc in docs:
                            tender = to_tender(doc, term=term or None) if in_scope(doc, ministries) else None
                            if tender is None:
                                batch.rows_skipped += 1
                                continue
                            tid = tender["tenderId"]
                            if tid in seen_ids:
                                # Terms overlap heavily -- "railway" and
                                # "indian railways" share most of their
                                # results -- so this is the common case, not
                                # an anomaly. It also counts as known ground:
                                # a row this run has already collected is not
                                # new, and not counting it meant every term
                                # after the first found only duplicates,
                                # never advanced the stop counter, and walked
                                # to its page cap.
                                self.stats.duplicates += 1
                                batch.rows_skipped += 1
                                consecutive_known += 1
                                continue
                            seen_ids.add(tid)
                            batch.tenders.append(tender)
                            self.stats.rows_in_scope += 1
                            if repairing:
                                # The enumeration never saw this one. That is
                                # the number this pass exists to report.
                                self.stats.recovered += 1
                            consecutive_known = consecutive_known + 1 if tid in known else 0

                        read_task: Optional[asyncio.Task] = None
                        if batch.tenders and with_details:
                            read_task = asyncio.create_task(
                                self._read_documents(detail_session, docs, batch, detail_sem)
                            )
                        pending.append((batch, read_task))

                        if stop_after_known and consecutive_known >= stop_after_known:
                            logger.info(
                                "gem-full: %r caught up after %s consecutive known ids (page %s)",
                                term or "all", consecutive_known, page,
                            )
                            caught_up = True

                        # Ship whatever is ready at the head, in page order.
                        # Only when more batches than the lookahead are still
                        # reading does the walk wait for the oldest one --
                        # which is what stops a slow document endpoint from
                        # letting the walk race the whole portal ahead of the
                        # batches it has yet to ship.
                        while pending and (
                            pending[0][1] is None
                            or pending[0][1].done()
                            or reading() > lookahead
                        ):
                            ready, head = pending.popleft()
                            if head is not None:
                                await head
                            yield ready

                        if caught_up:
                            break

                if ministry_query:
                    passes_done += 1
                    grew = len(all_ids) - ids_before_pass
                    target = self.stats.live_total
                    logger.info(
                        "gem-full: ministry pass %s: %s distinct of %s (+%s)",
                        passes_done, len(all_ids), target, grew,
                    )
                    if target and len(all_ids) >= target:
                        # Reached GeM's own count for this ministry. Every
                        # further pass would be re-reading what we have.
                        break
                    if grew:
                        barren_passes = 0
                    else:
                        barren_passes += 1
                        if barren_passes >= s.gem_ministry_settle_passes:
                            logger.warning(
                                "gem-full: ministry sweep settled at %s of %s "
                                "after %s passes with nothing new -- reporting "
                                "coverage below 1 rather than claiming the rest",
                                len(all_ids), target, barren_passes,
                            )
                            break

            while pending:
                ready, head = pending.popleft()
                if head is not None:
                    await head
                yield ready
        finally:
            # Closed early -- a cancelled run, or a consumer that stopped
            # iterating. A document still being read for a batch nobody will
            # receive is not worth finishing.
            for _, read_task in pending:
                if read_task is not None and not read_task.done():
                    read_task.cancel()
            pending.clear()

        logger.info("gem-full: %s", self.stats.summary())

    async def _read_documents(self, session, docs: list[dict], batch: Batch, sem) -> None:
        """Read one batch's bid documents into its tenders, in place.

        Never raises into the sweep: a document is never worth the run. A
        cancellation is the one exception, because it is the sweep itself
        telling us to stop.
        """
        from collector.portals.gem_detail import enrich_with_details

        try:
            detail = await enrich_with_details(session, docs, batch.tenders, semaphore=sem)
            logger.debug("gem-full: page %s details %s", batch.page, detail.summary())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a detail is never worth a sweep
            logger.debug("gem-full: detail stage skipped on page %s: %s", batch.page, e)
