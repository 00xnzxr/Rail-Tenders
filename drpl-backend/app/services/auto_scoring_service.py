"""Batch auto-scoring of tenders on Haiku 4.5 with a cached digest system prompt."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.platform_setting import PlatformSetting
from app.models.tender import Tender
from app.services.ai_service import call_ai
from app.services.auto_scoring_helpers import (
    compute_segment,
    is_below_threshold,
    parse_eligible_reply,
    parse_score_reply,
)
from app.services.auto_scoring_settings import get_scoring_settings
from app.services.seed_scoring_agent import get_scoring_system_prompt

log = logging.getLogger(__name__)


def find_unscored_tender_ids(db: Session, limit: "int | None" = None) -> list[int]:
    scoring = get_scoring_settings(db)
    limit = limit or scoring["auto_scoring_batch_size"]
    rows = (
        db.query(Tender.id)
        .filter(Tender.ai_relevance_score.is_(None))
        .filter(Tender.scoring_attempts < scoring["auto_scoring_max_retries"])
        .filter(Tender.is_archived == False)  # noqa: E712
        .order_by(Tender.created_at.desc())
        .limit(limit)
        .all()
    )
    return [r[0] for r in rows]


#: Eligibility rides on the scoring call. It used to be a second Haiku call
#: per IREPS blue-tick tender with the same capability blurb, the same model
#: and the tender text again, run straight after the score. Its "score"
#: duplicated the relevance score and was read nowhere; its verdict drives
#: one list filter.
_ELIGIBILITY_REQUEST = (
    "\n\nThis tender carries the portal's eligible-bidder mark. After REASON, add "
    "one more line:\nELIGIBLE: yes|no -- whether DRPL, with the capabilities "
    "described above, can take up this work.\n\nTender details:\n"
)
_ELIGIBILITY_CONTEXT_MAX_CHARS = 8000


def _wants_eligibility(tender: Tender) -> bool:
    return tender.portal == "ireps" and bool(tender.is_eligible_indicator)


def _user_prompt(tender: Tender) -> str:
    scope = tender.ai_summary or (tender.description or tender.full_description or "")
    prompt = f"Title: {tender.title}\n\nScope: {scope[:1500]}"
    if _wants_eligibility(tender):
        from app.services.ai_service import _build_tender_context
        prompt += _ELIGIBILITY_REQUEST + _build_tender_context(tender)[:_ELIGIBILITY_CONTEXT_MAX_CHARS]
    return prompt


def score_tenders_batch(db: Session, tender_ids: list[int]) -> dict:
    """Score the given tenders. Bounded concurrency; writes results; retry-safe."""
    if not tender_ids:
        return {"scored": 0, "flagged_below_threshold": 0, "failed": 0, "shared": 0}
    from app.core.run_context import run_id_scope
    ctx = run_id_scope(f"autoscore-{tender_ids[0]}")

    scoring = get_scoring_settings(db)
    system_prompt = get_scoring_system_prompt(db)
    sem = asyncio.Semaphore(scoring["auto_scoring_max_concurrency"])
    tenders = db.query(Tender).filter(Tender.id.in_(tender_ids)).all()

    result = {"scored": 0, "flagged_below_threshold": 0, "failed": 0, "shared": 0}

    # Identical requests share one call. GeM lists the same bid under several
    # ids and a sweep ingests them together; the prompt is the whole input,
    # so one answer is the answer for every copy.
    replies: dict[str, "asyncio.Future"] = {}

    async def _reply_for(prompt: str) -> str:
        fut = replies.get(prompt)
        if fut is not None:
            result["shared"] += 1
            return await asyncio.shield(fut)
        fut = asyncio.get_running_loop().create_future()
        replies[prompt] = fut
        try:
            async with sem:
                reply = await call_ai(system_prompt, prompt, db,
                                      "tender_scorer", model_override=scoring["auto_scoring_model"],
                                      force_cache_system=True)
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved; each waiter re-raises it itself
            raise
        fut.set_result(reply)
        return reply

    async def _score_and_apply(t: Tender):
        try:
            reply = await _reply_for(_user_prompt(t))
            score, reason = parse_score_reply(reply)
            t.ai_relevance_score = score
            t.fit_reasoning = reason
            if is_below_threshold(t.estimated_value, scoring["value_threshold_inr"]):
                t.below_threshold = True
                result["flagged_below_threshold"] += 1
            result["scored"] += 1
            if not t.segment_overridden:
                t.segment = compute_segment(
                    t.ai_relevance_score, t.estimated_value,
                    discard_below=scoring["segment_discard_below"],
                    bidable_at=scoring["segment_bidable_at"],
                    value_threshold=scoring["value_threshold_inr"],
                )

            if _wants_eligibility(t):
                eligible = parse_eligible_reply(reply)
                if eligible is not None:
                    t.eligibility_status = "eligible" if eligible else "not_eligible"
                    t.eligibility_score = t.ai_relevance_score
        except Exception as e:  # noqa: BLE001
            t.scoring_attempts = (t.scoring_attempts or 0) + 1
            result["failed"] += 1
            log.warning("auto-score failed for tender %s (attempt %s): %s",
                        t.id, t.scoring_attempts, e)

    async def _run():
        await asyncio.gather(*(_score_and_apply(t) for t in tenders))

    with ctx:
        asyncio.run(_run())
        db.commit()

    log.info("auto-scoring batch: found=%d scored=%d flagged_below=%d failed=%d shared=%d",
             len(tenders), result["scored"], result["flagged_below_threshold"], result["failed"],
             result["shared"])

    try:
        _stamp_last_run(db)
    except Exception:  # noqa: BLE001
        log.warning("failed to stamp auto_scoring_last_run", exc_info=True)

    return result


def _stamp_last_run(db: Session) -> None:
    """Upsert PlatformSetting `auto_scoring_last_run` with the current UTC timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    row = db.query(PlatformSetting).filter(PlatformSetting.key == "auto_scoring_last_run").first()
    if row is None:
        row = PlatformSetting(key="auto_scoring_last_run", value=now,
                               value_type="string", category="auto_scoring")
        db.add(row)
    else:
        row.value = now
    db.commit()


def resegment_all(db: Session) -> dict:
    """Recompute `segment` for every scored, non-overridden tender using current thresholds."""
    scoring = get_scoring_settings(db)
    q = (db.query(Tender)
         .filter(Tender.ai_relevance_score.isnot(None))
         .filter(Tender.segment_overridden == False))  # noqa: E712
    n = 0
    for t in q.all():
        t.segment = compute_segment(
            t.ai_relevance_score, t.estimated_value,
            discard_below=scoring["segment_discard_below"],
            bidable_at=scoring["segment_bidable_at"],
            value_threshold=scoring["value_threshold_inr"],
        )
        n += 1
    db.commit()
    return {"resegmented": n}
