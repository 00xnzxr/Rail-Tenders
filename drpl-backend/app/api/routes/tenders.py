"""
DRPL Backend - Tender Query Routes
Endpoints for querying, managing, and AI-analyzing tenders
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, or_

from app.core.database import get_db
from app.core.auth import get_current_user, require_admin
from app.models.user import User
from app.models.tender import Tender, TenderDocument, ScrapeLog
from app.schemas import (
    TenderResponse, TenderDetailResponse, TenderListResponse, ScrapeLogResponse,
    AIAnalysisResponse, AIBatchResponse, AIStatsResponse,
    TenderUpdateInput, TenderAssignInput, UserProfileResponse,
)
from app.services.tender_service import get_tenders, count_tenders, update_tender, assign_tender, get_users_list
from app.services.ai_service import analyze_tender, analyze_batch, get_ai_stats
from app.services.download_service import build_tender_zip

logger = logging.getLogger(__name__)

# A tender is "promising" for the dashboard when its AI relevance score is at
# least this. Single source of truth so the threshold is adjustable in one place.
DASHBOARD_PROMISING_THRESHOLD = 0.70

router = APIRouter(prefix="/tenders", tags=["tenders"])


@router.get("/", response_model=TenderListResponse)
def list_tenders(
    portal: Optional[str] = Query(None, description="Filter by portal (ireps, gem, etc.)"),
    search: Optional[str] = Query(None, description="Search tender titles by one or more keywords"),
    department: Optional[str] = Query(None, description="Filter by department"),
    status: Optional[str] = Query(None, description="Filter by status (open, closed, awarded)"),
    priority: Optional[str] = Query(None, description="Filter by priority (critical, high, medium, low)"),
    workflow_status: Optional[str] = Query(None, description="Filter by workflow status"),
    assigned_to: Optional[int] = Query(None, description="Filter by assigned user ID"),
    bid_type: Optional[str] = Query(None, description="Filter by bid type (NCB, GCB, Limited, …)"),
    location: Optional[str] = Query(None, description="Filter by location substring"),
    sort_by: str = Query("created_at", description="Sort by: created_at, closing_date, submission_deadline, priority, relevance"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False, description="Include archived tenders"),
    include_below_threshold: bool = Query(False, description="Include tenders flagged below the value threshold"),
    segment: Optional[str] = Query(None, description="Filter by segment (to_bid, not_bidable, discarded)"),
    score_min: Optional[float] = Query(None, description="Minimum AI relevance score"),
    score_max: Optional[float] = Query(None, description="Maximum AI relevance score"),
    value_min: Optional[float] = Query(None, description="Minimum estimated value"),
    value_max: Optional[float] = Query(None, description="Maximum estimated value"),
    emd_min: Optional[float] = Query(None, description="Minimum EMD amount"),
    emd_max: Optional[float] = Query(None, description="Maximum EMD amount"),
    closing_after: Optional[str] = Query(None, description="Only tenders closing on/after this ISO date"),
    closing_before: Optional[str] = Query(None, description="Only tenders closing on/before this ISO date"),
    eligibility_status: Optional[str] = Query(None, description="Filter by eligibility status"),
    unscored: bool = Query(False, description="Only tenders with no AI relevance score yet"),
    exclude_expired: bool = Query(True, description="Hide tenders whose closing date has passed (tenders with no closing date are kept). Default true — the list shows the LIVE population, matching /tenders/stats and the funnel."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List tenders with optional filters and sorting."""
    closing_after_dt = None
    if closing_after:
        try:
            closing_after_dt = datetime.fromisoformat(closing_after)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid closing_after date format; expected ISO 8601")

    closing_before_dt = None
    if closing_before:
        try:
            closing_before_dt = datetime.fromisoformat(closing_before)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid closing_before date format; expected ISO 8601")

    # Unscored rows have no threshold to be below, so never hide them behind the
    # below-threshold gate when the caller explicitly asks for the unscored view.
    common = dict(
        portal=portal, search=search, department=department, status=status, priority=priority,
        workflow_status=workflow_status, assigned_to=assigned_to, bid_type=bid_type,
        location=location, include_archived=include_archived,
        include_below_threshold=(include_below_threshold or unscored), segment=segment,
        score_min=score_min, score_max=score_max, value_min=value_min, value_max=value_max,
        emd_min=emd_min, emd_max=emd_max, closing_after=closing_after_dt,
        closing_before=closing_before_dt, eligibility_status=eligibility_status,
        unscored=unscored, exclude_expired=exclude_expired,
    )
    items = get_tenders(db, sort_by=sort_by, limit=limit, offset=offset, **common)
    total = count_tenders(db, **common)
    return {"items": items, "total": total}


@router.post("/bulk-archive")
def bulk_archive_tenders(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Archive (soft-hide) multiple tenders. Body: { tender_ids: [1,2,3] }"""
    tender_ids = body.get("tender_ids", [])
    if not tender_ids or not isinstance(tender_ids, list):
        raise HTTPException(status_code=400, detail="tender_ids is required and must be a list")
    if len(tender_ids) > 100:
        raise HTTPException(status_code=400, detail="Cannot archive more than 100 tenders at once")

    updated = (
        db.query(Tender)
        .filter(Tender.id.in_(tender_ids))
        .all()
    )
    # One clock for the whole batch, matching _run_discard_cleanup.
    now = datetime.now(timezone.utc)
    for t in updated:
        t.is_archived = True
        t.archived_at = now
        # SAFETY: "manual" keeps hand-archived rows out of the purge sweep
        # forever — purge_candidates_query only ever touches "past_due".
        t.archive_reason = "manual"
    db.commit()
    return {"archived": len(updated)}


@router.post("/bulk-delete")
def bulk_delete_tenders(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Permanently delete multiple tenders and their documents. Body: { tender_ids: [1,2,3] }"""
    tender_ids = body.get("tender_ids", [])
    if not tender_ids or not isinstance(tender_ids, list):
        raise HTTPException(status_code=400, detail="tender_ids is required and must be a list")
    if len(tender_ids) > 50:
        raise HTTPException(status_code=400, detail="Cannot delete more than 50 tenders at once")

    from app.services.tender_archive_service import delete_tenders_deep
    deleted = delete_tenders_deep(db, tender_ids)
    return {"deleted": deleted}


@router.get("/stats")
def tender_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dashboard stats over the LIVE population.

    LIVE = not archived AND not expired AND not below threshold — the same
    predicate `_apply_tender_filters` applies for the Tenders list and the
    funnel piles, so every headline number on every screen means one thing.

    This used to hand-roll its own queries, which is how production ended up
    showing 1,649 here and 319 on the Tenders page for the same idea. Route
    every count through `count_tenders` so they cannot drift apart again.
    """
    from app.services.tender_service import count_tenders
    from app.models.cost_breakdown import CostBreakdown

    # The one definition. Anything counted for a headline uses exactly this.
    LIVE = dict(exclude_expired=True)   # archived + below_threshold already excluded by default

    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=7)

    total = count_tenders(db, **LIVE)
    by_portal = {p: count_tenders(db, portal=p, **LIVE)
                 for p in ["ireps", "gem", "tendertiger", "bidassist"]}
    open_count = count_tenders(db, status="open", **LIVE)
    # Counted directly, NOT as total - unscored: `unscored=True` deliberately
    # forces below-threshold rows back in (they have no threshold to be below),
    # so that subtraction spans two different populations and under-reports.
    scored_count = db.query(Tender).filter(
        *_live_clause(now), Tender.ai_relevance_score.isnot(None)).count()
    unscored_count = count_tenders(db, unscored=True, **LIVE)

    # `promising` and `to_bid` are the SAME pile — both the AI-score tier the
    # funnel uses. `segment` is only written after an auto-scorer config pass,
    # so reading it here undercounted (271 vs 579 in production).
    promising_count = count_tenders(db, score_min=DASHBOARD_PROMISING_THRESHOLD, **LIVE)
    promising_open_count = count_tenders(
        db, score_min=DASHBOARD_PROMISING_THRESHOLD, status="open", **LIVE)
    closing_soon_count = count_tenders(
        db, score_min=DASHBOARD_PROMISING_THRESHOLD, status="open",
        closing_after=now, closing_before=soon, **LIVE)

    avg_relevance = db.query(func.avg(Tender.ai_relevance_score)).filter(
        *_live_clause(now), Tender.ai_relevance_score.isnot(None),
    ).scalar()

    with_costing_count = (db.query(CostBreakdown.tender_id)
                          .join(Tender, Tender.id == CostBreakdown.tender_id)
                          .filter(*_live_clause(now))
                          .distinct().count())

    return {
        "total_tenders": total,
        "by_portal": by_portal,
        "open_tenders": open_count,
        "scored_tenders": scored_count,
        "unscored_tenders": unscored_count,
        # Legacy alias — `analyzed_tenders` read `ai_category`, populated on 2 of
        # 2,397 production rows. Kept so old clients don't KeyError.
        "analyzed_tenders": scored_count,
        "avg_relevance": round(avg_relevance, 2) if avg_relevance else None,
        "promising_count": promising_count,
        "promising_open_count": promising_open_count,
        "closing_soon_count": closing_soon_count,
        "to_bid_count": promising_count,
        "with_costing_count": with_costing_count,
        # Where every row in the table actually is. Buckets are mutually
        # exclusive and sum to all_rows, so a headline dropping from 1,649 to
        # 269 reads as "recategorised", never as "data lost".
        "bifurcation": _tender_bifurcation(db, now),
    }


def _live_clause(now: datetime) -> tuple:
    """The LIVE predicate for queries that can't go through `count_tenders`
    (an average, a join). Mirrors `_apply_tender_filters` exactly, including the
    NULL-tolerant form — `== False` would drop NULL rows under SQL three-valued
    logic and this payload would then report two different populations.
    """
    return (
        Tender.is_archived.isnot(True),
        Tender.below_threshold.isnot(True),
        or_(Tender.closing_date.is_(None), Tender.closing_date >= now),
    )


def _tender_bifurcation(db: Session, now: datetime) -> dict:
    """Mutually-exclusive buckets covering every row in `tenders`.

    Precedence matters: archived wins over expired (an archived row is off the
    board whatever its date), and expired wins over below-threshold. Without a
    fixed order a row that is both would be counted twice and the parts would
    not sum to the whole.
    """
    archived = Tender.is_archived.is_(True)
    expired = and_(Tender.is_archived.isnot(True),
                   Tender.closing_date.isnot(None),
                   Tender.closing_date < now)
    low_value = and_(Tender.is_archived.isnot(True),
                     or_(Tender.closing_date.is_(None), Tender.closing_date >= now),
                     Tender.below_threshold.is_(True))
    live = and_(Tender.is_archived.isnot(True),
                or_(Tender.closing_date.is_(None), Tender.closing_date >= now),
                Tender.below_threshold.isnot(True))
    n = lambda clause: db.query(Tender).filter(clause).count()  # noqa: E731
    return {
        "live": n(live),
        "expired": n(expired),
        "archived": n(archived),
        "below_threshold": n(low_value),
        "all_rows": db.query(Tender).count(),
    }


@router.get("/view-counts")
def tender_view_counts(
    exclude_expired: bool = Query(True, description="Hide tenders whose closing date has passed. Default true — the funnel counts the LIVE population, matching /tenders/stats."),
    include_below_threshold: bool = Query(False, description="Include sub-threshold tenders (hidden from the default list)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Counts for the tender funnel piles, in one request.

    Piles are keyed off the AI relevance score (the green/amber/red tiers the
    UI shows), NOT the stored `segment` — `segment` is only set once the
    auto-scorer runs a config pass, so most tenders would fall through it.
    Boundaries mirror src/lib/tenderViews.ts: upper tier owns the threshold.

    Defaults match the LIVE population used by /tenders/stats and the list, so
    "all" here equals "total_tenders" there. The flags exist so a caller can
    deliberately widen the window, not so each screen can pick its own meaning.
    """
    from app.services.tender_service import count_tenders
    HI, MID, EPS = 0.7, 0.4, 0.0001
    common = dict(include_below_threshold=include_below_threshold,
                  exclude_expired=exclude_expired)
    return {
        "to_bid": count_tenders(db, score_min=HI, **common),
        "worth_a_look": count_tenders(db, score_min=MID, score_max=HI - EPS, **common),
        "discarded": count_tenders(db, score_max=MID - EPS, **common),
        "new_unscored": count_tenders(db, unscored=True, **common),
        "all": count_tenders(db, **common),
    }


@router.get("/ai-stats", response_model=AIStatsResponse)
def ai_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detailed AI analysis statistics."""
    return get_ai_stats(db)


@router.get("/scrape-logs/", response_model=list[ScrapeLogResponse], tags=["scrape-logs"])
def list_scrape_logs(
    portal: Optional[str] = Query(None, description="Filter by portal"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List scrape session logs for the monitor page."""
    query = db.query(ScrapeLog)
    if portal:
        query = query.filter(ScrapeLog.portal == portal)
    logs = query.order_by(ScrapeLog.started_at.desc()).offset(offset).limit(limit).all()
    return logs


@router.post("/analyze-batch")
async def trigger_batch_analysis(
    batch_size: int = Query(10, ge=1, le=500),
    use_batch_api: bool = Query(False, description="Use Claude Batches API (50% cost discount, async)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Trigger AI analysis on a batch of unanalyzed tenders.

    Set use_batch_api=true to use Claude's Message Batches API for 50% cost savings.
    Batch API processes asynchronously — poll /api/batch/{batch_id}/poll for results.
    """
    result = await analyze_batch(db, batch_size, use_batch_api=use_batch_api)
    return result


@router.get("/users", response_model=list[UserProfileResponse])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get list of active users for assignment dropdown."""
    users = get_users_list(db)
    return users


# ── Archive lifecycle ──────────────────────────────────────────────────────
#
# ROUTE ORDERING IS LOAD-BEARING: every route below this block is a
# path-parameter route (`/{tender_id}`, `/{tender_id}/...`). FastAPI matches in
# declaration order, so `/archive` and `/archive/purge-now` MUST stay above
# them — declared after `@router.patch("/{tender_id}")`, the literal segment
# "archive" would be captured as a tender_id and 422 on int coercion. Keep new
# literal-path tender routes above this line too.


@router.get("/archive")
def list_archived_tenders(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    reason: Optional[str] = Query(None, description="Filter by archive_reason"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Archived tenders, newest first, with a purge countdown for past-due rows."""
    from app.services.auto_scoring_settings import get_scoring_settings
    from app.services.tender_archive_service import PURGEABLE_ARCHIVE_REASON

    q = db.query(Tender).filter(Tender.is_archived == True)  # noqa: E712
    if reason:
        q = q.filter(Tender.archive_reason == reason)
    total = q.count()
    rows = (q.order_by(Tender.archived_at.desc().nullslast(), Tender.id.desc())
            .offset(offset).limit(limit).all())

    purge_days = get_scoring_settings(db)["archive_purge_days"]
    items = []
    for t in rows:
        # SAFETY INVARIANT: only `past_due` rows are ever auto-purged (see
        # tender_archive_service.purge_candidates_query). Showing a countdown on
        # an auto_discard/manual row would promise a deletion that never comes.
        purge_at = None
        if t.archive_reason == PURGEABLE_ARCHIVE_REASON and t.archived_at is not None:
            purge_at = (t.archived_at + timedelta(days=purge_days)).isoformat()
        items.append({
            "id": t.id, "portal": t.portal, "tender_id": t.tender_id,
            "title": t.title, "department": t.department,
            "estimated_value": t.estimated_value,
            "closing_date": t.closing_date.isoformat() if t.closing_date else None,
            "ai_relevance_score": t.ai_relevance_score,
            "archived_at": t.archived_at.isoformat() if t.archived_at else None,
            "archive_reason": t.archive_reason,
            "purge_at": purge_at,
        })
    return {"items": items, "total": total}


class PurgeRequest(BaseModel):
    """Body for /archive/purge-now.

    Typed rather than a raw dict so malformed input (non-list, nested/unhashable
    elements, non-integer ids) fails as a 422 at the boundary instead of blowing
    up later in `set(tender_ids)` or `Tender.id.in_([...])` as a 500.
    """
    tender_ids: list[int]


@router.post("/archive/purge-now")
def purge_archived_now(
    body: PurgeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Permanently delete archived tenders immediately. Body: { tender_ids: [...] }"""
    from app.services.tender_archive_service import delete_tenders_deep

    tender_ids = body.tender_ids
    if not tender_ids:
        raise HTTPException(status_code=400, detail="tender_ids is required and must be a list")
    if len(tender_ids) > 50:
        raise HTTPException(status_code=400, detail="Cannot purge more than 50 tenders at once")

    # This endpoint empties the ARCHIVE — it must never be a way to destroy a
    # live tender. A mistyped id would otherwise hard-delete an active tender
    # and every child row with it, irreversibly. Non-archived ids are dropped
    # here, so the returned count reflects what was actually deleted rather
    # than silently over-reporting. (The general-purpose /bulk-delete endpoint
    # remains available to admins for deleting live tenders on purpose.)
    archived_ids = [row.id for row in
                    db.query(Tender.id)
                    .filter(Tender.id.in_(tender_ids))
                    .filter(Tender.is_archived == True)  # noqa: E712
                    .all()]
    skipped = len(set(tender_ids)) - len(archived_ids)
    if skipped:
        logger.warning("purge-now: skipped %d non-archived/unknown tender id(s) of %d requested",
                       skipped, len(set(tender_ids)))
    return {"deleted": delete_tenders_deep(db, archived_ids), "skipped": skipped}


@router.post("/{tender_id}/restore")
def restore_tender(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Pull a tender back out of the archive.

    Sets `segment_overridden` — the existing "a human touched this" marker —
    so the restore actually sticks. All three consumers honour it:
      * `tender_archive_service.archive_candidates_query` excludes it from the
        past-due archive sweep (without this, the next tick would re-archive
        the row and restart its 7-day purge clock);
      * `scheduled_tasks._run_discard_cleanup` excludes it from auto-discard;
      * `auto_scoring_service` will not clobber its segment on a rescore.
    """
    t = db.query(Tender).filter(Tender.id == tender_id).first()
    if t is None:
        raise HTTPException(status_code=404, detail="Tender not found")
    t.is_archived = False
    t.archived_at = None
    t.archive_reason = None
    t.segment_overridden = True
    db.commit()
    return {"id": t.id, "restored": True}


@router.patch("/{tender_id}", response_model=TenderDetailResponse)
def update_tender_fields(
    tender_id: int,
    body: TenderUpdateInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update tender priority, workflow status, or submission deadline."""
    from app.services.tender_service import _parse_date
    deadline = _parse_date(body.submission_deadline) if body.submission_deadline else None
    tender = update_tender(
        db, tender_id,
        priority=body.priority,
        workflow_status=body.workflow_status,
        submission_deadline=deadline,
    )
    if not tender:
        raise HTTPException(status_code=404, detail="Tender not found")
    return tender


@router.post("/{tender_id}/assign", response_model=TenderDetailResponse)
def assign_tender_to_user(
    tender_id: int,
    body: TenderAssignInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Assign a tender to a user (admin only)."""
    tender = assign_tender(db, tender_id, body.user_id)
    if not tender:
        raise HTTPException(status_code=404, detail="Tender or user not found")
    return tender


class SegmentOverrideInput(BaseModel):
    segment: str


@router.post("/{tender_id}/segment")
def override_segment(
    tender_id: int,
    body: SegmentOverrideInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually override a tender's segment (to_bid / not_bidable / discarded)."""
    if body.segment not in ("to_bid", "not_bidable", "discarded"):
        raise HTTPException(status_code=400, detail="invalid segment")
    t = db.query(Tender).filter(Tender.id == tender_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="tender not found")
    t.segment = body.segment
    t.segment_overridden = True
    db.commit()
    return {"id": t.id, "segment": t.segment, "segment_overridden": True}


@router.get("/{tender_id}/download-zip")
def download_tender_zip(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download all tender documents + approved proposal as a ZIP."""
    try:
        buffer, filename = build_tender_zip(db, tender_id)
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{tender_id}/analyze", response_model=AIAnalysisResponse)
async def trigger_tender_analysis(
    tender_id: int,
    fanout: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger AI analysis on a single tender.

    When ``fanout`` is true (default) the post-analysis pipeline runs inline:
    annexure_finder + checklist_generator in parallel, then workspace init.
    Pass ``?fanout=false`` to get the legacy response shape (no pipeline key).
    """
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Tender not found")

    result = await analyze_tender(db, tender_id)
    if fanout:
        try:
            from app.services.post_analysis_pipeline import (
                run_post_analysis_pipeline,
            )
            result["pipeline"] = await run_post_analysis_pipeline(
                db, tender_id, current_user.id
            )
        except Exception as e:
            # Never fail the analysis response on pipeline errors — surface the
            # error as a status dict so the frontend can show it without
            # blocking the analysis result.
            logger.exception(
                f"Post-analysis pipeline failed for tender {tender_id}: {e}"
            )
            result["pipeline"] = {"status": "failed", "error": str(e)}
    return result


@router.post("/{tender_id}/annexures/extract")
async def extract_tender_annexures(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Synchronous annexure extraction via Claude Vision on the tender's PDFs.

    Creates paired ChecklistItem + DocumentWorkspace rows for each annexure
    found. Idempotent: matches by ChecklistItem.source_section; workspaces in
    in_review/approved status are skipped.
    """
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Tender not found")

    from app.services.langchain.graphs.annexure_finder_agent import (
        run_annexure_extraction,
    )
    try:
        return await run_annexure_extraction(db, tender_id)
    except Exception as e:
        logger.exception(
            f"Annexure extraction failed for tender {tender_id}: {e}"
        )
        raise HTTPException(
            status_code=500, detail=f"Annexure extraction failed: {e}"
        )


@router.get("/{tender_id}/annexures/export.pdf")
def export_annexures_pdf(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Render every annexure into ONE merged PDF (download).

    Each annexure is rendered with its letterhead + signatures + orientation
    (via generate_workspace_preview) and concatenated in display order. Nothing
    is finalized — exports reflect current draft content.
    """
    from app.services.annexure_export_service import build_combined_annexures_pdf

    try:
        pdf_bytes, filename = build_combined_annexures_pdf(db, tender_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Annexure PDF export failed for tender {tender_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tender_id}/annexures/preview-combined.pdf")
def preview_combined_annexures_pdf(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Same merged PDF as export.pdf but inline, for the live preview pane.

    Surfaces the rendered page count in X-PDF-Page-Count so the stacked
    annexures view can paginate, mirroring the per-item preview endpoint.
    """
    from app.services.annexure_export_service import build_combined_annexures_pdf

    try:
        pdf_bytes, _filename = build_combined_annexures_pdf(db, tender_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Annexure preview failed for tender {tender_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Preview failed: {e}")

    page_count: Optional[int] = None
    try:
        import io as _io
        from PyPDF2 import PdfReader
        page_count = len(PdfReader(_io.BytesIO(pdf_bytes)).pages)
    except Exception:
        page_count = None

    headers = {
        "Content-Disposition": 'inline; filename="annexures-preview.pdf"',
        "Access-Control-Expose-Headers": "X-PDF-Page-Count",
    }
    if page_count and page_count > 0:
        headers["X-PDF-Page-Count"] = str(page_count)

    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.get("/{tender_id}/annexures/export.docx")
def export_annexures_docx(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Render every annexure into ONE editable DOCX (download)."""
    from app.services.annexure_export_service import build_combined_annexures_docx

    try:
        docx_bytes, filename = build_combined_annexures_docx(db, tender_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Annexure DOCX export failed for tender {tender_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tender_id}/annexures/export.zip")
def export_annexures_zip(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Render every annexure and bundle the combined PDF + DOCX into one ZIP."""
    from app.services.annexure_export_service import build_combined_annexures_zip

    try:
        zip_bytes, filename = build_combined_annexures_zip(db, tender_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception(f"Annexure ZIP export failed for tender {tender_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Export failed: {e}")

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tender_id}", response_model=TenderDetailResponse)
def get_tender_detail(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get full detail for a single tender."""
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Tender not found")
    return tender


# --- Tender Documents ---

@router.get("/{tender_id}/documents")
def get_tender_documents(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all uploaded documents for a tender."""
    docs = (
        db.query(TenderDocument)
        .filter(TenderDocument.tender_id == tender_id)
        .order_by(TenderDocument.uploaded_at.desc())
        .all()
    )
    return {
        "documents": [
            {
                "id": d.id,
                "file_name": d.file_name,
                "file_size": d.file_size,
                "mime_type": d.mime_type,
                "document_type": d.document_type,
                "extraction_status": d.extraction_status,
                "source_url": d.source_url,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
            }
            for d in docs
        ],
        "total": len(docs),
    }


@router.get("/{tender_id}/documents/{document_id}/view")
def view_tender_document(
    tender_id: int,
    document_id: int,
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Serve a document for inline viewing (Content-Disposition: inline).
    Accepts JWT via ?token= query param for iframe embedding."""
    from app.core.auth import verify_token as _verify_token
    if not token:
        raise HTTPException(status_code=401, detail="Token required")
    payload = _verify_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    doc = (
        db.query(TenderDocument)
        .filter(TenderDocument.id == document_id, TenderDocument.tender_id == tender_id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    from app.services.storage_service import get_storage_service
    storage = get_storage_service()
    try:
        content = storage.download_file_sync(doc.file_path)
    except Exception:
        raise HTTPException(status_code=404, detail="Document file not found in storage")

    return Response(
        content=content,
        media_type=doc.mime_type or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{doc.file_name}"'},
    )


# --- Linked Documents ---

@router.get("/{tender_id}/documents/{document_id}/linked-documents")
def get_linked_documents(
    tender_id: int,
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get child documents extracted from a parent master document."""
    parent = db.query(TenderDocument).filter(
        TenderDocument.id == document_id,
        TenderDocument.tender_id == tender_id,
    ).first()
    if not parent:
        raise HTTPException(status_code=404, detail="Document not found")

    children = db.query(TenderDocument).filter(
        TenderDocument.parent_document_id == document_id,
    ).order_by(TenderDocument.uploaded_at.desc()).all()

    return {
        "parent": {
            "id": parent.id,
            "file_name": parent.file_name,
            "extraction_status": parent.extraction_status,
        },
        "linked_documents": [
            {
                "id": c.id,
                "file_name": c.file_name,
                "file_size": c.file_size,
                "mime_type": c.mime_type,
                "source_url": c.source_url,
                "uploaded_at": c.uploaded_at.isoformat() if c.uploaded_at else None,
            }
            for c in children
        ],
        "total": len(children),
    }


@router.post("/{tender_id}/documents/{document_id}/extract-links")
def trigger_link_extraction(
    tender_id: int,
    document_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually trigger (or re-trigger) link extraction for a document."""
    doc = db.query(TenderDocument).filter(
        TenderDocument.id == document_id,
        TenderDocument.tender_id == tender_id,
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if not doc.file_path or not doc.file_path.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF documents support link extraction")

    from app.services.document_link_service import process_document_links_background
    background_tasks.add_task(
        process_document_links_background,
        source_type="tender_document",
        source_id=doc.id,
        file_path=doc.file_path,
        tender_id=tender_id,
        session_id=None,
        user_id=current_user.id,
    )

    return {"message": "Link extraction started", "document_id": doc.id}
