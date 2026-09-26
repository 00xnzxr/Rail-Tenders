"""
DRPL Backend - GeM Search (collect) API routes.

The server-side portal sweep behind the sidebar's "GeM Search". The job is
pushed onto its own RQ queue by STRING NAME and served by the ordinary
worker (worker.py listens on both queues); the collector package lives in
``drpl-backend/collector`` and runs in-process -- no second service, no
service token, no HTTP hop (see collector/sink.py).

    POST /api/collect/runs      start a sweep, get a run_id back immediately
    GET  /api/collect/coverage  what the ledger knows (read-only)
    GET  /api/collect/active    the sweep still in flight, if any

The run it creates is an ordinary AgentRun, so `GET /api/runs/{id}/events`,
`POST /api/runs/{id}/cancel` and `GET /api/runs/active` all already work on it
with no changes.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.redis_client import get_redis, is_redis_enabled
from app.core.roles import require_surface
from app.models.agent_run import AgentRun
from app.models.user import User
from app.services import run_service

logger = logging.getLogger(__name__)

# The same surface as the tender list: whoever can see tenders can search for them.
router = APIRouter(prefix="/collect", tags=["collect"],
                   dependencies=[Depends(require_surface("tenders"))])

#: The collector's queue. Must match COLLECTOR RQ_QUEUE_NAME.
COLLECT_QUEUE = "collect"
#: Dotted path to the collector's RQ entrypoint. Enqueued as a string so this
#: process never imports it.
COLLECT_TASK = "collector.tasks.collect_task"

#: One sweep at a time, platform-wide. A sweep is minutes of portal traffic
#: against one portal from one egress IP, and it takes one of the worker's
#: twelve slots; a second concurrent sweep is a rate-limit incident and a
#: costing slot lost, not extra throughput. A second person who presses
#: Search is handed the running sweep to watch.
MAX_ACTIVE_COLLECTS = 1

#: "gem_full" enumerates every live bid and filters on GeM's own ministry
#: field, so its coverage is arithmetic rather than a property of a keyword
#: list. It is the portal the Search button should send, and the only one that
#: can honestly report "nothing was missed".
SUPPORTED_PORTALS = ("gem", "gem_full", "ireps")

#: "incremental" stops on known ground and reports no coverage -- the button.
#: "ministry" asks GeM's own Advanced Search to filter by the buyer's ministry
#: and repeats until the distinct count reaches the portal's own total for it:
#: the same tenders as "full" for about a third of the requests, which is what
#: makes a complete sweep cheap enough to run routinely. "full" reads the whole
#: corpus and is the only mode that would notice a railway bid GeM has
#: mislabelled. See docs/GEM-collector.md.
SUPPORTED_MODES = ("incremental", "ministry", "full")


class StartCollectBody(BaseModel):
    portals: list[str] = Field(default_factory=lambda: ["gem_full"])
    mode: str = Field(
        "incremental",
        description="incremental | ministry | full. incremental answers "
                    "'what is new?' and cannot report coverage; ministry "
                    "asks GeM's own ministry filter and converges on its "
                    "count; full walks the whole portal.",
    )
    terms: Optional[list[str]] = Field(
        None,
        description="Search terms. Only used by the keyword portal 'gem'; "
                    "'gem_full' enumerates and ignores them.",
    )
    ministries: Optional[list[str]] = Field(
        None, description="Override the ministry filter. [] keeps every ministry."
    )
    max_pages: Optional[int] = Field(None, ge=1, le=5000)
    with_details: Optional[bool] = Field(
        None,
        description="Fetch and parse each in-scope bid document for value, "
                    "EMD, ePBG and the eligibility block. Defaults to on.",
    )


@router.post("/runs")
def start_collect_run(
    body: StartCollectBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start a portal sweep and return its run_id before any work begins.

    The UI opens the SSE stream on that id immediately -- before the worker has
    even woken up -- which is why the run row is allocated here rather than in
    the worker.
    """
    if not is_redis_enabled():
        raise HTTPException(
            status_code=503,
            detail="Redis/queue unavailable - the collector cannot be reached.",
        )

    portals = [p.strip().lower() for p in (body.portals or []) if p and p.strip()]
    unknown = [p for p in portals if p not in SUPPORTED_PORTALS]
    if unknown:
        raise HTTPException(400, f"Unknown portal(s): {', '.join(unknown)}")
    if not portals:
        portals = ["gem"]
    mode = (body.mode or "incremental").lower()
    if mode not in SUPPORTED_MODES:
        raise HTTPException(
            400, "mode must be 'incremental', 'ministry' or 'full'"
        )
    # GeM's ministry filter takes exactly one ministry, so an override that
    # names none or several cannot be served. Saying so here beats queueing a
    # sweep that raises ValueError in the worker a minute later.
    if mode == "ministry" and body.ministries is not None and len(body.ministries) != 1:
        raise HTTPException(
            400,
            "mode 'ministry' needs exactly one ministry (GeM's filter takes "
            "one); omit 'ministries' to use the configured default, or use "
            "mode 'full' which filters client-side and handles a list.",
        )

    # One sweep at a time, whoever started it.
    live_collects = _live_collect_runs(db)
    if len(live_collects) >= MAX_ACTIVE_COLLECTS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "collect_already_running",
                "run_id": live_collects[0].id,
                "message": "A sweep is already running. Watch it or stop it first.",
            },
        )

    params = {
        "portals": portals,
        "mode": mode,
        "terms": body.terms,
        "ministries": body.ministries,
        "max_pages": body.max_pages,
        "with_details": body.with_details,
    }
    params = {k: v for k, v in params.items() if v is not None}

    run = AgentRun(
        user_id=current_user.id,
        status="queued",
        prompt=f"Collect tenders from {', '.join(portals)} ({mode})",
        display_message=f"Searching {', '.join(p.upper() for p in portals)}...",
        selected_agents=["collector"],
        meta={"kind": "collect", "params": params},
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    job_id = enqueue_collect_run(run, db)
    if job_id is None:
        run.status = "failed"
        run.error_message = "Could not reach the collect queue."
        db.commit()
        raise HTTPException(503, "Collect queue enqueue failed.")

    logger.info("collect.queued user_id=%s run_id=%s portals=%s", current_user.id, run.id, portals)
    return {
        "run_id": run.id,
        "rq_job_id": job_id,
        "status": run.status,
        "events_url": f"/api/runs/{run.id}/events",
        "params": params,
    }


def _live_collect_runs(db: Session) -> list[AgentRun]:
    """Collect runs still in flight, newest first, ignoring rows the reaper
    would call stale (a worker killed mid-sweep must not block the button
    until the next web boot)."""
    rows = (
        db.query(AgentRun)
        .filter(
            AgentRun.status.in_(run_service.LIVE_STATUSES),
            ~run_service._stale_run_clause(),
        )
        .order_by(AgentRun.created_at.desc())
        .all()
    )
    return [r for r in rows if (r.meta or {}).get("kind") == "collect"]


def enqueue_collect_run(run: AgentRun, db: Optional[Session] = None) -> Optional[str]:
    """Push a collect run onto the COLLECTOR'S queue.

    Two differences from ``run_service.enqueue_run``, and both matter:

      * ``Queue("collect", ...)`` rather than the default ``drpl-runs``. A sweep
        runs for twenty minutes; with twelve worker slots shared against agent
        runs, two concurrent sweeps would starve the costing and analysis
        pipelines. ``/health/capacity`` exists precisely because those slots
        fill.
      * the job is named by STRING, so drpl-backend never imports collector
        code and its image never grows a Playwright dependency.
    """
    from rq import Queue

    conn = get_redis()
    if conn is None:
        return None
    try:
        q = Queue(COLLECT_QUEUE, connection=conn)
        job = q.enqueue(
            COLLECT_TASK,
            run.id,
            job_timeout=60 * 30,     # match _RUN_JOB_TIMEOUT_SECONDS
            result_ttl=60 * 60,
            failure_ttl=60 * 60,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("enqueue_collect_run[%s] failed: %s", run.id, e, exc_info=True)
        return None

    if db is not None:
        try:
            run.rq_job_id = job.id
            db.commit()
        except Exception:
            db.rollback()
    return job.id


@router.get("/active")
def active_collect_run(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The sweep still in flight, if there is one -- anyone's, because there
    is only ever one and every user's page shows it.

    Always 200; ``{"run": null}`` is the ordinary answer. This is how a browser
    with no local pointer reattaches -- a second device, another browser, or
    after site data was cleared.
    """
    for r in _live_collect_runs(db):
        return {
            "run": {
                "id": r.id,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "params": (r.meta or {}).get("params", {}),
                "mine": r.user_id == current_user.id,
            }
        }
    return {"run": None}


@router.get("/recent")
def recent_collect_runs(
    limit: int = Query(8, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The last few sweeps, for the page's history list."""
    rows = (
        db.query(AgentRun)
        .order_by(AgentRun.created_at.desc())
        .limit(300)
        .all()
    )
    out = []
    for r in rows:
        if (r.meta or {}).get("kind") != "collect":
            continue
        out.append({
            "id": r.id,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "params": (r.meta or {}).get("params", {}),
            "summary": r.result_summary,
            "error": r.error_message,
            "mine": r.user_id == current_user.id,
        })
        if len(out) >= limit:
            break
    return {"runs": out}


@router.get("/coverage")
def collect_coverage(
    portal: str = Query("gem"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """What the collector's ledger knows about coverage.

    Reads the collect_* tables with plain SQL rather than importing the
    collector's models -- the boundary holds in this direction too. Returns
    an ``available: false`` payload rather than a 500 when the collector has
    never run and the tables do not exist yet.
    """
    try:
        # Before the first sweep the ledger tables do not exist; create them
        # (checkfirst, four tables, never a backend one) rather than log a
        # traceback on every page load.
        from collector.db import ensure_ledger
        ensure_ledger()
        seen = db.execute(
            text("SELECT COUNT(*) FROM collect_seen WHERE portal = :p"), {"p": portal}
        ).scalar()
        gaps = db.execute(
            text(
                "SELECT COUNT(*) FROM collect_gaps "
                "WHERE portal = :p AND resolved_at IS NULL"
            ),
            {"p": portal},
        ).scalar()
        last = db.execute(
            text(
                "SELECT run_id, started_at, finished_at, status, rows_new, "
                "rows_seen, expected_total, mode FROM collect_sweeps "
                "WHERE portal = :p ORDER BY id DESC LIMIT 1"
            ),
            {"p": portal},
        ).mappings().first()
        drift = db.execute(
            text(
                "SELECT field, fill_rate, baseline, detected_at FROM collect_drift "
                "WHERE portal = :p AND acknowledged = false "
                "ORDER BY detected_at DESC LIMIT 5"
            ),
            {"p": portal},
        ).mappings().all()
    except Exception as e:  # noqa: BLE001
        logger.info("collect coverage unavailable for %s: %s", portal, e)
        return {"available": False, "portal": portal, "reason": "collector has not run yet"}

    return {
        "available": True,
        "portal": portal,
        "tenders_seen": int(seen or 0),
        "open_gaps": int(gaps or 0),
        "last_sweep": dict(last) if last else None,
        "drift": [dict(d) for d in drift],
    }
