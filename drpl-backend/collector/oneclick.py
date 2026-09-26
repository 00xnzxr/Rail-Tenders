"""
DRPL Collector - the one-click GeM run.

    python -m collector.oneclick                  # today's new railway tenders
    python -m collector.oneclick --mode ministry  # every live one, cheaply
    python -m collector.oneclick --mode full      # every live one, audited
    python -m collector.oneclick --dry-run --out tenders.json

One entry point, no queue, no Redis, no database, no browser. It is the same
code the worker runs -- ``GemFullFetcher`` and the detail stage -- with a
progress line instead of a Redis stream, so what you watch here is exactly what
production does.

WHAT ONE CLICK ACTUALLY DOES
----------------------------
1. Bootstrap a GeM session (the live Chrome one if it is offered, see
   ``collector.session.chrome``; a plain HTTPS one otherwise).
2. Walk the bid list. ``incremental`` stops as soon as it recognises 30
   consecutive bid numbers it has already seen; ``ministry`` asks GeM's own
   ministry filter and repeats until it reaches the portal's count for that
   ministry (~510 pages); ``full`` walks all 4,300-odd pages.
3. Keep the rows whose stated ministry matches, using GeM's own structured
   field rather than a keyword.
4. For each kept row, fetch its bid document and read out the value, the EMD,
   the ePBG, the eligibility block, the consignee and the delivery terms.
5. Ship them to DRPL, or write them to a file with ``--dry-run``.

THE THREE MODES ARE NOT THE SAME PROMISE
----------------------------------------
``incremental`` is the button a person presses: it answers "what is new?" in
under a minute by walking newest-first and stopping on known ground. It cannot
see a back-dated publication or a bid whose status changed, because it stops
before reaching them -- that is the deal, and it is the right one for a button.
It reports no coverage, because it has not earned one.

``ministry`` and ``full`` can both claim nothing was missed, and both say so in
the summary as a coverage figure with the arithmetic behind it. They differ in
what they measure against. ``ministry`` uses GeM's own count for one ministry,
which is cheap -- 2,208 requests against 6,476 -- and is the right routine run.
``full`` reads the whole corpus, so it is the only one that could ever notice a
railway tender whose ministry field GeM has filled in wrongly. Cheap one
hourly, audit nightly.

``--mode full`` or ``--mode ministry`` on a first-ever run is the correct
starting point, because an incremental sweep with an empty ledger recognises
nothing and walks everything anyway, just without the stable sort order that
makes it safe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from collector.config import get_settings

logger = logging.getLogger("oneclick")

__all__ = ["OneClickResult", "run_once", "main"]


@dataclass
class OneClickResult:
    """Everything the run did, in a shape both a human and the API can read."""

    mode: str = "incremental"
    portal: str = "gem"
    #: "chrome" when the run used the browser's cookies, "direct" otherwise.
    session_source: str = "direct"
    started_at: float = field(default_factory=time.time)
    elapsed_seconds: float = 0.0
    pages_fetched: int = 0
    pages_failed: int = 0
    rows_seen: int = 0
    tenders_found: int = 0
    tenders_new: int = 0
    tenders_shipped: int = 0
    expected_total: Optional[int] = None
    #: The portal's total when the walk ended. Lower than expected_total by
    #: the bids that closed meanwhile -- those left from the tail, so they
    #: are not a hole in coverage and are reported separately.
    final_total: Optional[int] = None
    closed_during_sweep: int = 0
    rows_distinct: int = 0
    #: In-scope tenders the repair pass found that the enumeration had not.
    recovered: int = 0
    coverage: Optional[float] = None
    complete: bool = False
    details: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    tenders: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        d = {
            "mode": self.mode,
            "portal": self.portal,
            "session_source": self.session_source,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "rows_seen": self.rows_seen,
            "tenders_found": self.tenders_found,
            "tenders_new": self.tenders_new,
            "tenders_shipped": self.tenders_shipped,
            "expected_total": self.expected_total,
            "final_total": self.final_total,
            "closed_during_sweep": self.closed_during_sweep,
            "rows_distinct": self.rows_distinct,
            "recovered": self.recovered,
            "coverage": self.coverage,
            "complete": self.complete,
            "details": self.details,
        }
        if self.errors:
            d["errors"] = self.errors
        return d


def _fill_rate(tenders: list[dict], key: str) -> str:
    """How many shipped rows actually carry this field. The honesty metric."""
    if not tenders:
        return "0/0"
    n = sum(1 for t in tenders if t.get(key) not in (None, "", [], {}))
    return f"{n}/{len(tenders)}"


async def run_once(
    *,
    mode: str = "incremental",
    ministries: Optional[list[str]] = None,
    max_pages: Optional[int] = None,
    with_details: bool = True,
    dry_run: bool = False,
    known: Optional[set[str]] = None,
    use_chrome: bool = False,
    chrome_port: int = 9222,
    use_ledger: bool = True,
    progress=None,
) -> OneClickResult:
    """Run one GeM collection and return what it found.

    ``progress`` is called with each ``Batch`` as it lands, so a caller can
    stream rows into a UI while the sweep is still walking -- which is the
    whole reason the fetcher yields per page rather than returning a list.
    """
    from collector.portals.gem_full import GemFullFetcher

    s = get_settings()
    result = OneClickResult(mode=mode)
    started = time.monotonic()

    # Borrow the live browser's session when asked. Never required: if Chrome
    # is not there, is not logged in, or has no DevTools port, the sweep runs
    # on a direct session and says so in the summary rather than failing.
    session = None
    if use_chrome:
        from collector.session.chrome import chrome_session

        session, result.session_source = await chrome_session(chrome_port)
    else:
        result.session_source = "direct"

    fetcher = GemFullFetcher(session)

    # The ledger is what makes incremental mode mean anything: with nothing
    # known, "stop after 30 consecutive already-seen bids" never fires and the
    # run walks the whole portal. Reading it here keeps the CLI honest about
    # being the same thing the worker does.
    known_ids = set(known or ())
    if not known_ids and use_ledger:
        try:
            from collector.db import ensure_ledger, session_scope
            from collector.ledger import known_ids as ledger_known

            ensure_ledger()
            with session_scope() as db:
                known_ids = ledger_known(db, "gem")
            logger.info("oneclick: ledger knows %s gem tenders", len(known_ids))
        except Exception as e:  # noqa: BLE001 - no ledger is not a failure
            logger.info("oneclick: no ledger available (%s) -- treating all as new", e)

    collected: list[dict] = []

    try:
        params = {
            "mode": mode,
            "ministries": ministries,
            "max_pages": max_pages,
            "with_details": with_details,
        }
        async for batch in fetcher.sweep(known_ids, params):
            collected.extend(batch.tenders)
            result.pages_fetched = fetcher.stats.pages_fetched
            result.rows_seen = fetcher.stats.rows_seen
            if progress is not None:
                progress(batch, fetcher.stats)
    except Exception as e:  # noqa: BLE001 - report, never crash the caller
        result.errors.append(f"{type(e).__name__}: {e}")
        logger.exception("oneclick: sweep failed")
    finally:
        await fetcher.aclose()

    stats = fetcher.stats
    result.expected_total = stats.expected_total
    result.final_total = stats.final_total
    result.closed_during_sweep = stats.closed_during_sweep
    result.rows_distinct = stats.rows_distinct
    result.recovered = stats.recovered
    result.coverage = stats.coverage
    result.pages_failed = stats.pages_failed
    result.complete = stats.is_complete and not result.errors
    result.tenders_found = len(collected)
    result.tenders_new = sum(1 for t in collected if t["tenderId"] not in known_ids)
    result.tenders = collected
    result.details = {
        "with_value": _fill_rate(collected, "estimatedValue"),
        "with_emd": _fill_rate(collected, "emdAmount"),
        "with_eligibility": _fill_rate(collected, "eligibilityCriteria"),
        "with_delivery": _fill_rate(collected, "deliveryLocation"),
        "detail_extracted": _fill_rate(collected, "isDetailExtracted"),
    }

    if not dry_run and collected and s.drpl_service_token:
        from collector.sink import post as sink_post

        try:
            sunk = await sink_post(collected)
            result.tenders_shipped = sunk.new
            result.details["ingest"] = {
                "received": sunk.received, "new": sunk.new,
                "duplicates": sunk.duplicates, "errors": sunk.errors,
            }
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"sink: {type(e).__name__}: {e}")
            logger.exception("oneclick: shipping failed")

    result.elapsed_seconds = time.monotonic() - started
    return result


# -- CLI -----------------------------------------------------------------


def _print_progress(batch, stats) -> None:
    total = stats.expected_total or 0
    pages_total = (total + 9) // 10 if total else 0
    # Distinct rows against the portal's total, NOT pages fetched against pages
    # expected. The two agree while a sweep reads each page once, and only one
    # of them still means something when it does not: a ministry sweep makes
    # several convergence passes over the same pages, so the page ratio sails
    # past 100% (198.8% was the first run's second pass) while the distinct
    # count is the thing actually converging. Capped, because a bid published
    # behind the walker makes even the honest ratio exceed 1 for a moment.
    pct = (
        f"{min(100.0, 100 * stats.rows_distinct / total):5.1f}%"
        if total else "  ?  "
    )
    if batch.term:
        sys.stderr.write(
            f"\r  repair {batch.term!r:<20} page {batch.page:>4}  "
            f"recovered {stats.recovered:>3}   "
        )
    else:
        sys.stderr.write(
            f"\r  {pct}  page {batch.page:>5}/{pages_total or '?':<5} "
            f"rows {stats.rows_seen:>6}  in scope {stats.rows_in_scope:>4}   "
        )
    sys.stderr.flush()
    if batch.tenders:
        sys.stderr.write("\n")
        for t in batch.tenders:
            value = t.get("estimatedValue")
            money = f"Rs {value:,.0f}" if value else "-"
            sys.stderr.write(
                f"    + {t['tenderId']:<22} {money:>16}  "
                f"{(t.get('title') or '')[:52]}\n"
            )
        sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m collector.oneclick",
        description="One-click GeM tender collection for DRPL.",
    )
    p.add_argument(
        "--mode", choices=("incremental", "full", "ministry"), default="incremental",
        help="incremental stops on known ground (fast, reports no coverage); "
             "ministry asks GeM's own ministry filter and repeats until it "
             "reaches the portal's count (complete, 89%% fewer listing "
             "requests, same wall clock once documents are read); full walks "
             "every page of the portal (complete, and the only mode that "
             "audits the whole corpus)",
    )
    p.add_argument(
        "--ministries", default=None,
        help="comma-separated; overrides TARGET_MINISTRIES. Pass '' for no "
             "filter, which keeps every live bid on the portal",
    )
    p.add_argument("--max-pages", type=int, default=None,
                   help="stop after this many pages (a smoke run, not a sweep)")
    p.add_argument("--no-details", action="store_true",
                   help="listing rows only: no bid documents, no eligibility")
    p.add_argument("--dry-run", action="store_true",
                   help="never POST to DRPL, whatever the token says")
    p.add_argument("--out", default=None, help="write the tenders to this JSON file")
    p.add_argument("--chrome", action="store_true",
                   help="scrape through a logged-in Chrome's session; falls "
                        "back to a direct session if none is attached")
    p.add_argument("--chrome-port", type=int, default=9222)
    p.add_argument("--no-ledger", action="store_true",
                   help="ignore the local ledger and treat every bid as new")
    p.add_argument("--quiet", action="store_true", help="summary only")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    ministries = None
    if args.ministries is not None:
        ministries = [m.strip() for m in args.ministries.split(",") if m.strip()]

    s = get_settings()
    shipping = bool(s.drpl_service_token) and not args.dry_run
    sys.stderr.write(
        f"GeM one-click: mode={args.mode} "
        f"ministries={ministries if ministries is not None else s.ministries} "
        f"details={'off' if args.no_details else 'on'} "
        f"ship={'DRPL' if shipping else 'no (dry run)'}\n\n"
    )

    result = asyncio.run(run_once(
        mode=args.mode,
        ministries=ministries,
        max_pages=args.max_pages,
        with_details=not args.no_details,
        dry_run=args.dry_run,
        use_chrome=args.chrome,
        chrome_port=args.chrome_port,
        use_ledger=not args.no_ledger,
        progress=None if args.quiet else _print_progress,
    ))

    sys.stderr.write("\n\n")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result.tenders, f, indent=2, ensure_ascii=False)
        sys.stderr.write(f"wrote {len(result.tenders)} tenders to {args.out}\n")

    print(json.dumps(result.summary(), indent=2))

    if result.errors:
        return 1
    if args.mode in ("full", "ministry") and not result.complete:
        # A sweep that claims completeness and did not reach it has not done
        # its job, and the exit code is how a cron job finds that out.
        sys.stderr.write(
            f"WARNING: {args.mode} sweep did not reach complete coverage "
            f"({result.pages_failed} pages failed, coverage {result.coverage}).\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
