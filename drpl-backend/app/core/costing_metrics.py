"""
Costing agent reliability metrics — minute-bucketed counters in Redis.

Tracks the events that the costing-reliability plan
(now-i-need-to-synchronous-taco.md) added pre-flight context-budget trims
and persistence failures so `/health/costing` can surface "is the platform
working?" in one place. Same shape as `llm_metrics.py` — INCR with a TTL,
no Prometheus, no external metrics stack.

Counters:
    drpl:metrics:costing_trim:{minute_bucket}            — pre-flight trim ran
    drpl:metrics:costing_overflow:{minute_bucket}        — overflow even AFTER trim
    drpl:metrics:costing_persist_fail:{minute_bucket}    — CostBreakdown not written
    drpl:metrics:costing_persist_ok:{minute_bucket}      — CostBreakdown written
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_METRIC_TTL_SECONDS = 60 * 60  # keep 1 hour of buckets

_COUNTER_KEYS = {
    "trim": "drpl:metrics:costing_trim",
    "overflow": "drpl:metrics:costing_overflow",
    "persist_fail": "drpl:metrics:costing_persist_fail",
    "persist_ok": "drpl:metrics:costing_persist_ok",
}


def _current_bucket() -> int:
    return int(time.time() // 60)


def _record(name: str) -> None:
    """Fire-and-forget counter increment. Swallows all errors."""
    try:
        from app.core.redis_client import get_redis
    except Exception:
        return
    client = get_redis()
    if client is None:
        return
    prefix = _COUNTER_KEYS.get(name)
    if not prefix:
        return
    key = f"{prefix}:{_current_bucket()}"
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, _METRIC_TTL_SECONDS)
        pipe.execute()
    except Exception as e:
        logger.debug(f"costing_metrics._record({name!r}) failed: {e}")


def record_trim() -> None:
    """The pre-flight trim policy ran (at least one trim was applied)."""
    _record("trim")


def record_overflow() -> None:
    """Anthropic returned context_length_exceeded even after the trim."""
    _record("overflow")


def record_persist_fail(reason: Optional[str] = None) -> None:
    """A costing run produced output but the CostBreakdown row was NOT
    persisted — the user would see a typed `persistence_failed` error.
    """
    _record("persist_fail")
    if reason:
        logger.info(f"[costing_metrics] persist_fail reason={reason!r}")


def record_persist_ok() -> None:
    """A costing run produced output AND the CostBreakdown row was persisted —
    the artifact card opens to a populated editor.
    """
    _record("persist_ok")


def _count_recent(name: str, minutes: int) -> int:
    """Sum the named counter across the last N minute-buckets."""
    try:
        from app.core.redis_client import get_redis
    except Exception:
        return 0
    client = get_redis()
    if client is None:
        return 0
    prefix = _COUNTER_KEYS.get(name)
    if not prefix:
        return 0
    now = _current_bucket()
    keys = [f"{prefix}:{now - i}" for i in range(minutes)]
    try:
        vals = client.mget(keys)
    except Exception as e:
        logger.debug(f"costing_metrics._count_recent({name!r}) failed: {e}")
        return 0
    total = 0
    for v in vals:
        if v is None:
            continue
        try:
            total += int(v)
        except Exception:
            pass
    return total


def get_costing_health(minutes: int = 60) -> dict:
    """Return a snapshot of the costing-agent reliability counters across
    the last `minutes` minute-buckets. Used by `/health/costing`.
    """
    persist_ok = _count_recent("persist_ok", minutes)
    persist_fail = _count_recent("persist_fail", minutes)
    trims = _count_recent("trim", minutes)
    overflows = _count_recent("overflow", minutes)
    total_runs = persist_ok + persist_fail
    success_rate = (persist_ok / total_runs) if total_runs > 0 else None
    return {
        "window_minutes": minutes,
        "runs_total": total_runs,
        "runs_successful": persist_ok,
        "runs_persist_failed": persist_fail,
        "success_rate": success_rate,
        "trims_applied": trims,
        "overflows_after_trim": overflows,
    }
