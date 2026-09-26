"""
LLM rate-limit metrics — minute-bucketed counters in Redis.

Records 429/quota-exhausted events from the FailoverChatModel so
`/health/capacity` can surface `llm_429_last_5m`. No Prometheus; a few
INCRs with a 10-min TTL is enough until we have an SRE.

Keys:
    drpl:metrics:llm_429:{minute_bucket}                 — global counter
    drpl:metrics:llm_429:{provider}:{minute_bucket}      — per-provider

Called from both sync and async contexts (FailoverChatModel has both).
Uses the sync Redis client — a single INCR is microseconds and keeps
the call site identical in either context. Swallows all errors.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_METRIC_TTL_SECONDS = 10 * 60  # keep 10 min of buckets


def _current_bucket() -> int:
    return int(time.time() // 60)


def record_rate_limit(provider: Optional[str] = None) -> None:
    """Fire-and-forget: increment global + per-provider 429 counters."""
    try:
        from app.core.redis_client import get_redis
    except Exception:
        return
    client = get_redis()
    if client is None:
        return

    bucket = _current_bucket()
    provider_tag = (provider or "unknown").lower()
    global_key = f"drpl:metrics:llm_429:{bucket}"
    provider_key = f"drpl:metrics:llm_429:{provider_tag}:{bucket}"

    try:
        pipe = client.pipeline()
        pipe.incr(global_key)
        pipe.expire(global_key, _METRIC_TTL_SECONDS)
        pipe.incr(provider_key)
        pipe.expire(provider_key, _METRIC_TTL_SECONDS)
        pipe.execute()
    except Exception as e:
        logger.debug(f"llm_metrics.record_rate_limit failed: {e}")


def count_recent_rate_limits(minutes: int = 5) -> int:
    """Sum 429s across the last N minute-buckets. Returns 0 on any error."""
    try:
        from app.core.redis_client import get_redis
    except Exception:
        return 0
    client = get_redis()
    if client is None:
        return 0

    now = _current_bucket()
    keys = [f"drpl:metrics:llm_429:{now - i}" for i in range(minutes)]
    try:
        vals = client.mget(keys)
    except Exception as e:
        logger.debug(f"llm_metrics.count_recent_rate_limits failed: {e}")
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
