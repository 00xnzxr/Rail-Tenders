"""Selection + gating helpers for eager tender analysis. Query-only here;
the sweep/job orchestration lives in app/worker/eager_analysis_tasks.py."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.tender import Tender
from app.models.tender import TenderDocument
from app.models.document_analysis import DocumentExtractionResult
from app.models.platform_setting import PlatformSetting
from app.services.eager_analysis_settings import get_eager_analysis_settings

log = logging.getLogger(__name__)


def find_eager_analysis_candidates(db: Session, limit: "int | None" = None) -> list[int]:
    """In-scope tenders that have documents, aren't analyzed yet, and aren't
    in-flight. Ordered oldest-first so a backlog drains fairly."""
    s = get_eager_analysis_settings(db)
    limit = limit or s["eager_analysis_batch_size"]
    min_score = s["eager_analysis_min_score"]

    analyzed = db.query(DocumentExtractionResult.tender_id).distinct().subquery()
    has_doc = db.query(TenderDocument.tender_id).distinct().subquery()

    rows = (
        db.query(Tender.id)
        .filter(or_(Tender.ai_relevance_score >= min_score,
                    Tender.is_eligible_indicator == True))       # noqa: E712
        .filter(Tender.is_archived == False)                     # noqa: E712
        .filter(Tender.is_duplicate == False)                    # noqa: E712
        .filter(Tender.eager_analysis_status.is_(None) | (Tender.eager_analysis_status == "failed"))
        .filter(Tender.id.in_(db.query(has_doc.c.tender_id)))
        .filter(~Tender.id.in_(db.query(analyzed.c.tender_id)))
        .order_by(Tender.created_at.asc())
        .limit(limit)
        .all()
    )
    return [r[0] for r in rows]


_DAILY_KEY = "eager_analysis_daily_count"


def capacity_ok(queue_depth: int, rate_limits_5m: int, pool_util: float, ceiling: int) -> bool:
    """Pure backpressure gate. Any failing signal -> defer this tick."""
    return queue_depth <= ceiling and rate_limits_5m <= 0 and pool_util <= 0.80


def gather_capacity(db: Session) -> tuple[int, int, float]:
    """Read the real capacity signals (mirrors GET /health/capacity).

    Fails CLOSED: if any signal can't be read, it's defaulted to a value that
    makes capacity_ok() return False (defer this tick) rather than proceeding
    blind. Backpressure must never silently pass just because Redis/metrics/
    the DB pool were unreadable.
    """
    queue_depth = 0
    try:
        from app.core.redis_client import get_queue
        q = get_queue()
        queue_depth = int(q.count) if q is not None else 0
    except Exception:
        queue_depth = 10**9
        log.warning("gather_capacity: queue_depth read failed; defaulting to fail-closed value", exc_info=True)
    rate_limits = 0
    try:
        from app.core.llm_metrics import count_recent_rate_limits
        rate_limits = int(count_recent_rate_limits(minutes=5))
    except Exception:
        rate_limits = 1
        log.warning("gather_capacity: rate_limits read failed; defaulting to fail-closed value", exc_info=True)
    pool_util = 0.0
    try:
        from app.core.database import engine
        pool = engine.pool
        cap = (pool.size() + pool.overflow()) or 1
        pool_util = pool.checkedout() / cap
    except Exception:
        pool_util = 1.0
        log.warning("gather_capacity: pool_util read failed; defaulting to fail-closed value", exc_info=True)
    return queue_depth, rate_limits, pool_util


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def daily_count(db: Session) -> int:
    row = db.query(PlatformSetting).filter(PlatformSetting.key == _DAILY_KEY).first()
    if not row or not row.value or ":" not in row.value:
        return 0
    day, _, n = row.value.partition(":")
    return int(n) if day == today_key() and n.isdigit() else 0


def bump_daily_count(db: Session, n: int) -> None:
    cur = daily_count(db)
    row = db.query(PlatformSetting).filter(PlatformSetting.key == _DAILY_KEY).first()
    val = f"{today_key()}:{cur + n}"
    if row:
        row.value = val
    else:
        db.add(PlatformSetting(
            key=_DAILY_KEY,
            value=val,
            value_type="string",
            category="eager_analysis",
        ))
    db.commit()


def reap_stuck_eager_analysis(db: Session, older_than_minutes: int = 30) -> int:
    """Reset orphaned queued/running markers (worker killed mid-analysis) so the
    sweep can pick the tender up again. Terminal states (done/failed) untouched."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
    rows = (db.query(Tender)
            .filter(Tender.eager_analysis_status.in_(["queued", "running"]))
            .filter((Tender.eager_analysis_at.is_(None)) | (Tender.eager_analysis_at < cutoff))
            .all())
    for t in rows:
        t.eager_analysis_status = None
    if rows:
        db.commit()
    return len(rows)
