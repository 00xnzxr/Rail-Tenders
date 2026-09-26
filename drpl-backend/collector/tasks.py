"""
DRPL Collector - the RQ task.

A collect run IS an AgentRun. Same table, same UUID, same Redis stream key,
same cancel key, same ``GET /api/runs/{run_id}/events`` endpoint the frontend
already talks to. That one decision is what makes this coherent: run
durability, cooperative cancellation, partial output and SSE reattachment were
all built for agent runs, and a portal sweep has exactly the same requirements
-- it takes minutes, it must survive a closed laptop, the user wants a Stop
button, and they want to reattach and see what happened.

So there is no second progress mechanism. The Search button reuses
``openRunStream``; closing the tab and reopening works because
``GET /runs/active`` already exists; Stop works because ``run_tasks``
established the flag-not-kill contract; and a sweep that dies at page 40 of 60
leaves ``partial_trace`` behind saying so.

The order inside ``_sweep_portal`` is the one thing to preserve when editing:

    fetch -> check cancel -> emit progress -> POST -> WRITE LEDGER -> emit

The ledger write happens only after the POST returns success. Reverse those
two and a failed upload silently loses a tender for good, which is the bug
this service exists to end.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from collector import enrich, ledger, runbus
from collector.config import get_settings
from collector.db import SessionLocal, ensure_ledger
from collector.models import AgentRun
from collector.parsing import drift as drift_mod
from collector.portals.base import Batch, ParserDrift, PortalUnavailable
from collector.sink import InProcessSink, Sink, SinkError

logger = logging.getLogger(__name__)


class Cancelled(Exception):
    """The user asked this sweep to stop, and it did, on a page boundary.

    Distinct from a failure: nothing went wrong, so the row ends ``cancelled``
    rather than ``failed``.
    """


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- Fetcher registry ----------------------------------------------------
#
# Imported lazily so the GeM path never pays for Playwright. A collector
# deployed without a browser image can still sweep GeM.


def build_fetcher(portal: str):
    if portal == "gem":
        from collector.portals.gem import GemFetcher

        return GemFetcher()
    if portal == "gem_full":
        # Complete enumeration rather than a keyword sweep: every live bid is
        # examined and the ministry filter runs on GeM's own structured field,
        # so coverage is arithmetic instead of a property of the word list.
        # Same Fetcher protocol, so everything downstream is unchanged.
        from collector.portals.gem_full import GemFullFetcher

        return GemFullFetcher()
    if portal == "ireps":
        from collector.portals.ireps import IrepsFetcher

        return IrepsFetcher()
    raise ValueError(f"unknown portal: {portal!r}")


SUPPORTED_PORTALS = ("gem", "gem_full", "ireps")


# -- RQ entrypoint -------------------------------------------------------


def collect_task(run_id: str) -> dict:
    """RQ entry point. Sync, because RQ workers are not async-native."""
    return asyncio.run(_collect(run_id))


async def _collect(run_id: str) -> dict:
    ensure_ledger()
    r = runbus.get_redis()
    db = SessionLocal()

    run: Optional[AgentRun] = db.get(AgentRun, run_id)
    if run is None:
        db.close()
        logger.error("collect_task[%s]: AgentRun not found.", run_id)
        return {"status": "error", "reason": "not_found"}

    params: dict[str, Any] = dict((run.meta or {}).get("params") or {})
    user_id = run.user_id

    # Cancelled while it sat in the queue. Two signals, either sufficient: the
    # Redis flag the cancel request set, and the row itself, which the request
    # (or drpl-backend's startup reaper) may already have closed. A row that
    # says cancelled is not run, whoever said it.
    if runbus.is_cancelled(r, run_id) or run.status == "cancelled":
        run.status = "cancelled"
        run.finished_at = run.finished_at or _utcnow()
        run.error_message = run.error_message or "Cancelled before it started."
        db.commit()
        runbus.finish(r, run_id, "cancelled", {"note": "Cancelled before it started."})
        runbus.clear_cancel_flag(r, run_id)
        runbus.release_user_slot(r, user_id, run_id)
        db.close()
        return {"status": "cancelled", "run_id": run_id}

    run.status = "running"
    run.started_at = _utcnow()
    run.error_message = None
    db.commit()
    runbus.emit(r, run_id, "run_started", {"run_id": run_id, "kind": "collect"})

    portals = [p for p in (params.get("portals") or ["gem"]) if p in SUPPORTED_PORTALS]
    if not portals:
        portals = ["gem"]

    # One scope profile for the whole run, fetched once. Every portal reads the
    # same admin-configured keywords, so what the sweep looks for is what the
    # Tender Scope page says -- with no deploy and no second list.
    from collector.scope import fetch_profile, resolve_terms

    profile = await fetch_profile()
    terms = resolve_terms(profile, params)
    ministries = params.get("ministries") or profile.ministries(get_settings().ministries)
    mode = (params.get("mode") or "incremental").lower()

    runbus.emit(
        r,
        run_id,
        "collect_started",
        {
            "run_id": run_id,
            "portals": portals,
            "terms": terms,
            "ministries": ministries,
            "mode": mode,
        },
    )

    sink: Sink = InProcessSink(user_id)
    final_status = "failed"
    error_msg: Optional[str] = None

    try:
        for portal in portals:
            await _sweep_portal(
                db=db,
                r=r,
                run=run,
                run_id=run_id,
                portal=portal,
                sink=sink,
                profile=profile,
                params={**params, "terms": terms, "ministries": ministries, "mode": mode},
            )

        # Coverage questions, asked after the tenders have already landed.
        # Best-effort by construction: an audit failure must never turn a
        # successful sweep into a failed run.
        try:
            from collector import audit

            for portal in portals:
                gaps = audit.scan_gaps(db, portal)
                if gaps.get("recorded"):
                    runbus.emit(r, run_id, "gaps_found", gaps)
            for finding in audit.reconcile(db, run_id):
                runbus.emit(r, run_id, "reconciliation", finding)
        except Exception as e:  # noqa: BLE001
            logger.warning("collect_task[%s]: audit pass failed: %s", run_id, e)

        final_status = "completed"

    except Cancelled:
        final_status = "cancelled"
        error_msg = "Cancelled at the user's request."
        logger.info("collect_task[%s]: stopped on cancel request", run_id)
        runbus.emit(r, run_id, "cancelled", {"run_id": run_id})

    except Exception as e:  # noqa: BLE001
        error_msg = f"{type(e).__name__}: {e}"[:2000]
        logger.error("collect_task[%s] failed: %s", run_id, error_msg, exc_info=True)
        runbus.emit(r, run_id, "error", {"message": error_msg})

    finally:
        await sink.aclose()
        summary: dict = {}
        try:
            from collector import audit

            summary = audit.run_summary(db, run_id)
        except Exception:  # noqa: BLE001
            summary = {}

        try:
            row = db.get(AgentRun, run_id)
            if row is not None:
                row.status = final_status
                row.finished_at = _utcnow()
                if error_msg:
                    row.error_message = error_msg
                row.result_summary = _render_summary(summary, final_status)
                row.partial_trace = ledger.trace(db, run_id)
                db.commit()
        except Exception as e:  # noqa: BLE001
            logger.warning("collect_task[%s]: terminal write failed: %s", run_id, e)
            db.rollback()

        runbus.finish(r, run_id, final_status, summary)
        runbus.clear_cancel_flag(r, run_id)
        runbus.release_user_slot(r, user_id, run_id)
        db.close()

    return {"status": final_status, "run_id": run_id, **summary}


def _render_summary(summary: dict, status: str) -> str:
    if not summary:
        return f"Collect run {status}."
    return (
        f"{summary.get('rows_new', 0)} new, "
        f"{summary.get('rows_duplicate', 0)} already known, "
        f"{summary.get('rows_skipped', 0)} out of scope "
        f"across {summary.get('pages_walked', 0)} page(s) of "
        f"{', '.join(summary.get('portals') or []) or 'no portals'}."
    )


# -- One portal ----------------------------------------------------------


async def _sweep_portal(
    *,
    db,
    r,
    run: AgentRun,
    run_id: str,
    portal: str,
    sink: Sink,
    profile,
    params: dict,
) -> None:
    """Walk one portal, shipping each page as it lands."""
    fetcher = build_fetcher(portal)
    sweep = ledger.begin_sweep(
        db, run_id, portal, getattr(fetcher, "version", portal), mode=params.get("mode", "incremental")
    )
    known = ledger.known_ids(db, portal)
    logger.info("collect[%s]: %s starting, %s known ids", run_id, portal, len(known))

    drift_rows: list[dict] = []
    sweep_status = "completed"
    sweep_error: Optional[str] = None

    # Enrichment spend is bounded per RUN, not per page: the cost of a sweep
    # should scale with what it found, never with how far it walked.
    enrich_budget = {"left": int(get_settings().enrich_max_per_run)}
    enrich_total = enrich.EnrichmentResult()

    try:
        async for batch in fetcher.sweep(known, params):
            # Cooperative cancellation, on a boundary the fetcher chose. Killing
            # the process would stop it sooner and leave a batch half-shipped.
            if runbus.is_cancelled(r, run_id):
                sweep_status = "cancelled"
                raise Cancelled()

            runbus.emit(r, run_id, "collect_progress", batch.progress())

            # Drop rows the admin has excluded before spending a POST on them.
            batch.tenders = [
                t for t in batch.tenders
                if not profile.excluded(f"{t.get('title', '')} {t.get('description', '')}")
            ]
            drift_rows.extend(batch.tenders)

            if not batch.tenders:
                # Still counts as walked -- an empty page is information.
                ledger.record(db, portal, batch, _EMPTY_RESULT, sweep)
                continue

            # Read the bid document for anything NEW, BEFORE shipping it.
            # `_update_existing_tender` never assigns estimated_value or
            # emd_amount, so a value found after the row exists is dropped on
            # the floor -- it has to be present when the row is created.
            if enrich_budget["left"] > 0 and enrich.is_enabled():
                new_here = [t for t in batch.tenders if t.get("tenderId") not in known]
                if new_here:
                    runbus.emit(r, run_id, "enrich_started", {
                        "portal": portal, "page": batch.page, "count": len(new_here),
                    })
                    http = getattr(getattr(fetcher, "_session", None), "client", None)
                    try:
                        er = await enrich.enrich_tenders(
                            http or _fallback_http(), batch.tenders, known
                        )
                        enrich_total.merge(er)
                        enrich_budget["left"] -= er.attempted
                        if er.attempted:
                            runbus.emit(r, run_id, "enriched", er.summary())
                    except Exception as e:  # noqa: BLE001 -- an extra, never fatal
                        logger.warning("collect[%s]: enrichment failed: %s", run_id, e)

            try:
                result = await sink.post(batch.tenders)
            except SinkError as e:
                # The batch did NOT land, so the ledger stays untouched and
                # these tenders remain unknown -- the next sweep will find
                # them again. That is the point of the ordering.
                logger.error("collect[%s]: %s page %s failed to ship: %s",
                             run_id, portal, batch.page, e)
                runbus.emit(r, run_id, "ingest_failed", {
                    "portal": portal, "page": batch.page, "count": len(batch.tenders),
                    "error": str(e)[:300],
                })
                raise

            # LEDGER AFTER THE POST. Never before.
            ledger.record(db, portal, batch, result, sweep)
            known.update(t["tenderId"] for t in batch.tenders if t.get("tenderId"))

            runbus.emit(r, run_id, "tenders_ingested", {
                "portal": portal,
                "page": batch.page,
                "term": batch.term,
                "new": result.new,
                "duplicates": result.duplicates,
                "errors": result.errors,
                "ids": result.new_ids,
            })

            # Written every page so a run killed outright leaves at most one
            # page of progress unrecorded.
            _flush_partial(db, run_id)

    except Cancelled:
        sweep_status = "cancelled"
        raise
    except (ParserDrift, PortalUnavailable) as e:
        sweep_status = "failed"
        sweep_error = f"{type(e).__name__}: {e}"
        logger.error("collect[%s]: %s sweep failed: %s", run_id, portal, sweep_error)
        runbus.emit(r, run_id, "portal_failed", {"portal": portal, "error": str(e)[:300]})
        # A failed portal is not a failed run: the other portals still get
        # their pass, and the sweep row records what happened here.
    except SinkError as e:
        sweep_status = "failed"
        sweep_error = f"SinkError: {e}"
        raise
    finally:
        # A parser degrading rather than dying -- the failure mode a
        # selector-based scraper reports as a quiet day.
        try:
            findings = drift_mod.check_fill(
                drift_rows,
                getattr(fetcher, "fill_baseline", {}) or {},
                portal,
                getattr(fetcher, "version", ""),
            )
            if findings:
                ledger.record_drift(db, run_id, portal, getattr(fetcher, "version", ""), findings)
                for f in findings:
                    runbus.emit(r, run_id, "drift", f)
                logger.warning("collect[%s]: %s %s", run_id, portal, drift_mod.summarise(findings))
        except Exception as e:  # noqa: BLE001
            logger.debug("collect[%s]: drift check failed: %s", run_id, e)

        try:
            ledger.close_sweep(db, sweep, status=sweep_status, error=sweep_error)
            summary = ledger.sweep_summary(sweep)
            if enrich_total.attempted:
                summary["enrichment"] = enrich_total.summary()
            # Whether the sweep can claim it missed nothing, and the arithmetic
            # behind the claim. Carried on the event rather than the
            # collect_sweeps row on purpose: it is derived from the fetcher's
            # own stats, it needs no migration, and the frontend is the only
            # thing that has ever wanted it. A fetcher that cannot speak to
            # coverage (the keyword sweep) simply omits both keys, which is
            # what tells the UI not to claim completeness on its behalf.
            stats = getattr(fetcher, "stats", None)
            if stats is not None and getattr(stats, "coverage", None) is not None:
                summary["coverage"] = stats.coverage
                summary["complete"] = bool(stats.is_complete)
                summary["rows_distinct"] = stats.rows_distinct
                summary["pages_failed"] = stats.pages_failed
                summary["final_total"] = stats.final_total
            runbus.emit(r, run_id, "portal_done", summary)
        except Exception as e:  # noqa: BLE001
            logger.warning("collect[%s]: could not close sweep: %s", run_id, e)

        closer = getattr(fetcher, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception as e:  # noqa: BLE001
                logger.debug("collect[%s]: fetcher close failed: %s", run_id, e)


def _fallback_http():
    """An httpx client for enrichment when the fetcher exposes none.

    GeM's fetcher hands over its own warm session (the PDF endpoints sit
    behind the same WAF cookies as the listing), so this is only reached by a
    fetcher that has no HTTP client of its own.
    """
    import httpx

    from collector import netconfig

    s = get_settings()
    return netconfig.make_client(
        timeout=httpx.Timeout(s.enrich_timeout_seconds, connect=20.0),
        headers={"User-Agent": s.user_agent},
    )


class _EmptyResult:
    new = 0
    duplicates = 0
    errors = 0
    new_ids: list[int] = []


_EMPTY_RESULT = _EmptyResult()


def _flush_partial(db, run_id: str) -> None:
    """Write the step list onto the row so a killed run still says what it did."""
    try:
        row = db.get(AgentRun, run_id)
        if row is not None:
            row.partial_trace = ledger.trace(db, run_id)
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("collect[%s]: partial flush failed: %s", run_id, e)
        db.rollback()
