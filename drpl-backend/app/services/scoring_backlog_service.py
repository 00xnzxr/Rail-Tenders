"""Single source of truth for tender-scoring backlog stats + draining.

Scoring only (ai_relevance_score). Never touches the deep-analysis path.
"""
from __future__ import annotations

import hashlib
import logging
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.tender import Tender
from app.models.platform_setting import PlatformSetting
from app.models.message_batch import MessageBatch
from app.services.auto_scoring_settings import get_scoring_settings
from app.services.auto_scoring_service import find_unscored_tender_ids, score_tenders_batch

log = logging.getLogger(__name__)

# Rough per-score cost estimate (Haiku 4.5, ~800 cached-in + ~1500-char scope + 2-line out).
# Deliberately approximate — labelled an estimate in the UI. INR per scored tender, live price.
_EST_INR_PER_SCORE_LIVE = 0.15
_WS_RE = re.compile(r"\s+")


def _pending_query(db: Session):
    return db.query(Tender).filter(
        Tender.ai_relevance_score.is_(None),
        Tender.is_archived == False,  # noqa: E712
    )


def content_hash(title: str, scope: str) -> str:
    norm = _WS_RE.sub(" ", f"{title or ''}\n{(scope or '')[:1500]}").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _tender_scope(t: Tender) -> str:
    return t.ai_summary or (t.description or t.full_description or "")


def dedupe_by_content_hash(tenders: list[Tender]) -> tuple[list[Tender], dict[str, list[int]]]:
    """Return (one representative per hash, {hash: [all tender ids sharing it]})."""
    fanout: dict[str, list[int]] = {}
    unique: list[Tender] = []
    for t in tenders:
        h = content_hash(t.title, _tender_scope(t))
        if h not in fanout:
            fanout[h] = []
            unique.append(t)
        fanout[h].append(t.id)
    return unique, fanout


def _count_in_flight(db: Session) -> int:
    return db.query(func.count(MessageBatch.id)).filter(
        MessageBatch.batch_type == "tender_scoring",
        MessageBatch.status.notin_(("ended", "canceled", "expired")),
    ).scalar() or 0


def backlog_stats(db: Session) -> dict:
    scoring = get_scoring_settings(db)
    max_retries = scoring["auto_scoring_max_retries"]

    total = db.query(func.count(Tender.id)).filter(Tender.is_archived == False).scalar()  # noqa: E712
    scored = db.query(func.count(Tender.id)).filter(Tender.ai_relevance_score.isnot(None)).scalar()
    pending = _pending_query(db).count()
    drainable = _pending_query(db).filter(Tender.scoring_attempts < max_retries).count()

    in_flight = _count_in_flight(db)

    last = db.query(PlatformSetting).filter(
        PlatformSetting.key == "auto_scoring_last_run").first()

    return {
        "total": total,
        "scored": scored,
        "pending": pending,
        "drainable": drainable,
        "stuck_at_cap": pending - drainable,
        "in_flight_batches": in_flight,
        "last_reaper_run": last.value if last else None,
        "enabled": bool(scoring["auto_scoring_enabled"]),
        "est_cost_inr": round(drainable * _EST_INR_PER_SCORE_LIVE / 2, 2),  # batch path = 50% off
    }


def drain_backlog(db: Session, mode: str = "live", limit: int | None = None) -> dict:
    """Drain the unscored backlog.

    mode="live"  -> loop the existing reaper batch scorer until empty (full price, fast).
    mode="batch" -> submit an Anthropic Batch API job (50% off, async). Added in Task 4.
    """
    if mode == "batch":
        from app.services.batch_service import create_tender_scoring_batch
        scoring = get_scoring_settings(db)
        take = limit or (scoring["auto_scoring_batch_size"] * 10)  # batches can be large
        pending = (
            _pending_query(db)
            .filter(Tender.scoring_attempts < scoring["auto_scoring_max_retries"])
            .order_by(Tender.created_at.desc())
            .limit(take)
            .all()
        )
        if not pending:
            return {"mode": "batch", "submitted": 0, "deduped_saved": 0, "batch_id": None}
        unique, fanout = dedupe_by_content_hash(pending)
        import asyncio
        batch_record = asyncio.run(
            create_tender_scoring_batch(db, [t.id for t in unique], fanout)
        )
        return {
            "mode": "batch",
            "submitted": len(unique),
            "deduped_saved": len(pending) - len(unique),
            "batch_id": batch_record.batch_id,
        }

    scoring = get_scoring_settings(db)
    batch = scoring["auto_scoring_batch_size"]
    scored = failed = 0
    remaining = limit
    while True:
        take = batch if remaining is None else min(batch, remaining)
        if take <= 0:
            break
        ids = find_unscored_tender_ids(db, limit=take)
        if not ids:
            break
        out = score_tenders_batch(db, ids)
        scored += out.get("scored", 0)
        failed += out.get("failed", 0)
        if remaining is not None:
            remaining -= len(ids)
        # guard against a batch that scores nothing (all failed at retry cap) — avoid infinite loop
        if out.get("scored", 0) == 0 and out.get("failed", 0) == 0:
            break
    return {"mode": "live", "scored": scored, "failed": failed}
