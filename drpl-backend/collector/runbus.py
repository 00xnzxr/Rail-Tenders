"""
DRPL Collector - the run event bus.

Thirty lines that buy the entire live-progress UI. The frontend's stream
reader is already written and tested; all the collector has to do is write
events into the same Redis stream in the same shape.

THE SHAPE IS LOAD-BEARING. drpl-backend's SSE endpoint reads entries with
``_field(fields, "event")`` / ``_field(fields, "data")`` / ``_field(fields,
"sse")``. If those field names drift, the lookup returns None and the events
vanish with no error anywhere -- the one place this design can fail silently.
So this mirrors ``app/worker/run_tasks.py::_xadd_event`` field for field:

    payload = {"event": <name>}
    payload["data"] = json.dumps(data, default=str)   # when data is not None

and the endpoint renders anything without an ``sse`` field as::

    event: {event}\\ndata: {data}\\n\\nid: {stream_id}\\n

Events named ``run_done`` terminate the stream on the client, so exactly one
is emitted, at the very end, by ``finish()``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# -- Shared contract 2: the two key formats -------------------------------
#
# Copied from app/core/redis_client.py rather than imported. Both sides are
# pinned by tests/test_contract.py.

#: Retention on a run's event stream. Must match RUN_STREAM_TTL_SECONDS in
#: drpl-backend -- a stream created without an expiry lives in Redis for good.
STREAM_TTL_SECONDS = 60 * 60


def stream_key(run_id: str) -> str:
    return f"drpl:run:{run_id}:events"


def cancel_key(run_id: str) -> str:
    return f"drpl:run:{run_id}:cancel"


def user_active_key(user_id: int | str) -> str:
    """The per-user concurrency set drpl-backend admits runs into.

    A collect run enqueued through ``/api/collect/runs`` is admitted the same
    way an agent run is, so the worker must give the slot back when it ends or
    the cap leaks until the key's own hour-long expiry.
    """
    return f"drpl:user:{user_id}:active_runs"


# -- Client -------------------------------------------------------------

_lock = threading.Lock()
_client = None
_initialised = False


def get_redis():
    """The Redis client, or None when REDIS_URL is unset or unreachable.

    Fails to None permanently on the first bad PING -- same contract as
    drpl-backend's redis_client, so a sweep started without Redis simply runs
    without progress reporting instead of crash-looping.
    """
    global _client, _initialised
    if _initialised:
        return _client
    with _lock:
        if _initialised:
            return _client
        _initialised = True

        # The backend's client: same REDIS_URL, same stream and cancel keys
        # (`run_service` / `run_tasks` own those formats; test_contract pins
        # that this module still spells them the same way).
        try:
            from app.core.redis_client import get_redis as _backend_redis

            _client = _backend_redis()
            if _client is None:
                logger.info("runbus: Redis not configured -- progress reporting disabled.")
        except Exception as e:  # noqa: BLE001
            logger.warning("runbus: Redis unavailable (%s) -- progress disabled.", e)
            _client = None
    return _client


# -- Emit / cancel / finish ---------------------------------------------


def emit(r, run_id: str, event: str, data: Optional[dict] = None) -> None:
    """Append one event to the run's stream. Never raises.

    Progress reporting must never fail a sweep: the tenders are the product,
    the progress bar is a courtesy.
    """
    if r is None or not run_id:
        return
    payload: dict[str, Any] = {"event": event}
    if data is not None:
        payload["data"] = json.dumps(data, default=str)
    try:
        r.xadd(stream_key(run_id), payload)
    except Exception as e:  # noqa: BLE001
        logger.debug("runbus.emit(%s/%s) failed: %s", run_id, event, e)


def is_cancelled(r, run_id: str) -> bool:
    """Has somebody asked this run to stop?

    Fails to False when Redis is unreachable, deliberately: a blip must not
    cancel a sweep somebody is waiting for. The opposite failure -- a cancel
    that does not take -- is visible and retryable.
    """
    if r is None or not run_id:
        return False
    try:
        return bool(r.exists(cancel_key(run_id)))
    except Exception as e:  # noqa: BLE001
        logger.debug("runbus.is_cancelled(%s) failed: %s", run_id, e)
        return False


def clear_cancel_flag(r, run_id: str) -> None:
    """Drop the flag once the run has stopped. Best-effort; it has a TTL."""
    if r is None or not run_id:
        return
    try:
        r.delete(cancel_key(run_id))
    except Exception as e:  # noqa: BLE001
        logger.debug("runbus.clear_cancel_flag(%s) failed: %s", run_id, e)


def release_user_slot(r, user_id: Optional[int], run_id: str) -> None:
    """Give the user back the concurrency slot the enqueue took."""
    if r is None or user_id is None:
        return
    try:
        r.srem(user_active_key(user_id), run_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("runbus.release_user_slot(%s) failed: %s", run_id, e)


def finish(r, run_id: str, status: str, summary: Optional[dict] = None) -> None:
    """Close the stream.

    Emits the single ``run_done`` the SSE endpoint returns on, then sets the
    expiry. The expire call matters even on a stream the worker created: it is
    the only thing standing between an hour of retention and forever.
    """
    if r is None or not run_id:
        return
    emit(r, run_id, "run_done", {"run_id": run_id, "status": status, **(summary or {})})
    try:
        r.expire(stream_key(run_id), STREAM_TTL_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.debug("runbus.finish(%s) expire failed: %s", run_id, e)


def reset_for_tests() -> None:
    global _client, _initialised
    with _lock:
        _client = None
        _initialised = False
