"""
DRPL Backend - Redis client & RQ queue accessors.

Phase C infra: Redis is optional. When `REDIS_URL` is empty (local dev,
test, early Railway rollout), every accessor returns `None` and callers
must fall back to their synchronous / inline behaviour. That way the
queue and cache layers can be merged ahead of the Railway Redis plugin
without breaking anything.

Guarantees:
- Clients are created lazily on first use and memoised per-process.
- A single failed PING causes the client to be marked unavailable for
  the rest of the process lifetime (no retry storms on a down Redis).
- `get_queue()` returns an RQ queue bound to the same connection.
- `is_redis_enabled()` is the cheap probe for call sites that want to
  branch without touching the network.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_redis_client = None            # type: ignore[var-annotated]
_async_redis_client = None      # type: ignore[var-annotated]
_queue = None                   # type: ignore[var-annotated]
_initialized = False
_enabled = False


def _init_once() -> None:
    """Build the Redis client on first use. Never raises; logs and disables on failure."""
    global _redis_client, _queue, _initialized, _enabled
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        _initialized = True

        try:
            from app.core.config import get_settings
            settings = get_settings()
            url = (settings.redis_url or "").strip()
        except Exception as e:
            logger.warning(f"redis_client: could not load settings: {e}")
            return

        if not url:
            logger.info("redis_client: REDIS_URL not set — Redis disabled (queue/cache no-op).")
            return

        try:
            import redis
            client = redis.Redis.from_url(
                url,
                decode_responses=False,
                socket_connect_timeout=3,
                socket_timeout=5,
                health_check_interval=30,
            )
            client.ping()
        except Exception as e:
            logger.warning(f"redis_client: connection failed — disabling. {e}")
            _redis_client = None
            return

        _redis_client = client
        _enabled = True
        logger.info(f"redis_client: connected ({url.split('@')[-1] if '@' in url else url}).")

        try:
            from rq import Queue
            _queue = Queue(settings.rq_queue_name, connection=client)
            logger.info(f"redis_client: RQ queue '{settings.rq_queue_name}' ready.")
        except Exception as e:
            logger.warning(f"redis_client: RQ queue init failed: {e}")
            _queue = None


def is_redis_enabled() -> bool:
    """Fast probe — does NOT hit the network after first init."""
    _init_once()
    return _enabled


def get_redis():
    """Return the low-level Redis client, or None if disabled."""
    _init_once()
    return _redis_client


def get_async_redis():
    """Return an async `redis.asyncio` client, or None if disabled.

    Built on the same `REDIS_URL` as the sync client. Kept separate so
    FastAPI handlers can XREAD without blocking the event loop. Created
    lazily on first call and cached for the process lifetime.
    """
    global _async_redis_client
    _init_once()
    if not _enabled:
        return None
    if _async_redis_client is not None:
        return _async_redis_client
    with _lock:
        if _async_redis_client is not None:
            return _async_redis_client
        try:
            import redis.asyncio as aioredis
            from app.core.config import get_settings
            url = (get_settings().redis_url or "").strip()
            if not url:
                return None
            _async_redis_client = aioredis.from_url(
                url,
                decode_responses=False,
                socket_connect_timeout=3,
                socket_timeout=30,
                health_check_interval=30,
            )
        except Exception as e:
            logger.warning(f"redis_client: async client init failed: {e}")
            _async_redis_client = None
    return _async_redis_client


def get_queue():
    """Return the RQ Queue, or None if disabled."""
    _init_once()
    return _queue


def run_channel(user_id: int | str, run_id: str) -> str:
    """Canonical pub/sub channel name for a user's run. Keep in sync with subscribers."""
    return f"user:{user_id}:run:{run_id}"


def cache_key(namespace: str, *parts: str | int) -> str:
    """Canonical cache key: `drpl:cache:<namespace>:<part>:<part>`."""
    tail = ":".join(str(p) for p in parts) if parts else ""
    return f"drpl:cache:{namespace}" + (f":{tail}" if tail else "")


def publish_event(channel: str, payload: bytes | str) -> int:
    """Publish a single event, returning subscriber count (or 0 when Redis is off)."""
    client = get_redis()
    if client is None:
        return 0
    try:
        return int(client.publish(channel, payload))
    except Exception as e:
        logger.debug(f"redis_client: publish failed ({channel}): {e}")
        return 0


def run_stream_key(run_id: str) -> str:
    """Canonical Redis Streams key holding an agent run's SSE event log."""
    return f"drpl:run:{run_id}:events"


#: Retention on a run's event stream. One source for the worker, which sets it
#: when a run ends, and for the cancel path, which can create the key from the
#: web process before any worker has written to it — a stream created without
#: an expiry lives in Redis for good.
RUN_STREAM_TTL_SECONDS = 60 * 60


def run_cancel_key(run_id: str) -> str:
    """Key whose presence means "stop this run at the next safe point".

    A flag rather than a signal, because the run executes in a different
    process from the request that cancels it. Killing the work horse would
    stop it sooner and leave whatever it was writing half-written; the worker
    reads this between events instead, so it stops on a boundary it chose.
    """
    return f"drpl:run:{run_id}:cancel"


# ── JSON TTL cache helpers ──────────────────────────────────────────────
#
# Thin cache layer over Redis. All four helpers are no-ops when Redis is
# disabled so callers can sprinkle them onto hot read paths without
# branching. Values are JSON-serialised; non-JSON types (datetime, UUID)
# are stringified via `default=str`.

import json as _json  # noqa: E402  (kept local to avoid polluting top-level imports)


def cache_get_json(key: str):
    """Return the cached value or None. Also None on miss / Redis off / decode error."""
    client = get_redis()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as e:
        logger.debug(f"cache_get_json({key}): {e}")
        return None
    if raw is None:
        return None
    try:
        return _json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:
        return None


def cache_set_json(key: str, value, ttl: Optional[int] = None) -> bool:
    """Write a JSON-serialisable value with TTL (defaults to redis_cache_ttl_seconds)."""
    client = get_redis()
    if client is None:
        return False
    if ttl is None:
        try:
            from app.core.config import get_settings
            ttl = int(get_settings().redis_cache_ttl_seconds or 300)
        except Exception:
            ttl = 300
    try:
        payload = _json.dumps(value, default=str)
        client.setex(key, ttl, payload)
        return True
    except Exception as e:
        logger.debug(f"cache_set_json({key}): {e}")
        return False


def cache_delete(*keys: str) -> int:
    """Drop one or more cache keys. Returns count of keys actually deleted."""
    client = get_redis()
    if client is None or not keys:
        return 0
    try:
        return int(client.delete(*keys))
    except Exception as e:
        logger.debug(f"cache_delete({keys}): {e}")
        return 0


def cache_delete_prefix(prefix: str) -> int:
    """SCAN + DEL everything matching `<prefix>*`. O(N) over matched keys."""
    client = get_redis()
    if client is None or not prefix:
        return 0
    deleted = 0
    try:
        for batch in _scan_batches(client, prefix + "*"):
            if batch:
                deleted += int(client.delete(*batch))
    except Exception as e:
        logger.debug(f"cache_delete_prefix({prefix}): {e}")
    return deleted


def _scan_batches(client, pattern: str, batch_size: int = 200):
    """Yield lists of keys matching `pattern` via SCAN (non-blocking)."""
    cursor = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match=pattern, count=batch_size)
        if keys:
            yield keys
        if cursor == 0:
            break


def reset_for_tests() -> None:
    """Test-only: wipe the memoised client so tests can swap REDIS_URL."""
    global _redis_client, _async_redis_client, _queue, _initialized, _enabled
    with _lock:
        _redis_client = None
        _async_redis_client = None
        _queue = None
        _initialized = False
        _enabled = False
