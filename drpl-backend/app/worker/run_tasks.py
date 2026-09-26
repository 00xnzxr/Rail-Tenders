"""
DRPL Backend - RQ background tasks for agent runs.

The HTTP layer enqueues ``run_router_task(run_id)`` via RQ; this worker
function loads the AgentRun row, pumps
``streaming_handler.stream_router_response`` to completion, and writes
every SSE chunk onto a Redis Stream keyed by the run_id. The matching
``/runs/{run_id}/events`` SSE endpoint XREADs that stream (replaying the
whole history on reconnect, then blocking for new entries) so the UI
survives refreshes and tab closes.

Intentionally small — the real agent graph still lives in
``streaming_handler.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.core.redis_client import RUN_STREAM_TTL_SECONDS
from app.services import run_service

logger = logging.getLogger(__name__)


# 1-hour retention on the event stream. Long enough to survive a coffee
# break + laptop lid close; short enough to bound Redis memory per run.
_STREAM_TTL_SECONDS = RUN_STREAM_TTL_SECONDS  # one source with the cancel path


def _xadd_event(redis_client, stream_key: str, *, event: str, data: dict | None = None, sse: str | None = None) -> None:
    """Append one event to the run's Redis Stream. Swallows errors."""
    payload: dict[str, Any] = {"event": event}
    if data is not None:
        payload["data"] = json.dumps(data, default=str)
    if sse is not None:
        payload["sse"] = sse
    try:
        redis_client.xadd(stream_key, payload)
    except Exception as e:
        logger.debug(f"xadd failed on {stream_key}: {e}")


#: How often `_pump` asks Redis whether the run was cancelled. Once per event
#: would be one round trip per streamed token on the paths that stream tokens
#: live; once a second keeps the stop latency imperceptible and the load nil.
_CANCEL_CHECK_INTERVAL_S = 1.0

#: How often `_pump` writes the streamed-so-far text to the row. Redis holds
#: the full event log for an hour; this is the copy that outlives it, so a run
#: killed outright leaves at most this many seconds of output unrecorded.
_PARTIAL_FLUSH_INTERVAL_S = 5.0


class RunCancelled(Exception):
    """The user asked this run to stop, and it did.

    Distinct from a failure: nothing went wrong, so the row ends `cancelled`
    rather than `failed` and no "your run failed" notification is sent.
    """


def run_router_task(run_id: str) -> dict:
    """RQ entry point. Sync — RQ workers aren't async-native."""
    from app.core.run_context import install_run_id_filter, run_id_scope
    install_run_id_filter()  # worker process installs its own log filter
    with run_id_scope(run_id):
        return _run_router_task_inner(run_id)


def _run_router_task_inner(run_id: str) -> dict:
    from app.core.database import SessionLocal
    from app.core.redis_client import get_redis, run_stream_key
    from app.models.agent_run import AgentRun

    db = SessionLocal()
    r = get_redis()
    stream_key = run_stream_key(run_id)

    if r is None:
        logger.error(f"run_router_task[{run_id}]: Redis unavailable.")
        db.close()
        return {"status": "error", "reason": "redis_unavailable"}

    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if not run:
        logger.error(f"run_router_task: AgentRun {run_id} not found.")
        db.close()
        return {"status": "error", "reason": "not_found"}

    # Snapshot ORM fields before the commit expires them. After db.commit()
    # below, SQLAlchemy's default behaviour is to mark `run` as expired
    # and re-fetch on next attribute access — which fails once the stream
    # pump has opened its own session.
    args = {
        "message": run.prompt or "",
        "session_id": run.router_session_id,
        "tender_id": run.tender_id,
        "user_id": run.user_id,
        "proposal_session_id": run.proposal_session_id,
        "display_message": run.display_message,
        "file_metadata": (run.meta or {}).get("file_metadata"),
    }

    # Cancelled while it sat in the queue. RQ's own cancel is best-effort — a
    # job already handed to a worker cannot be pulled back — so this check is
    # what actually guarantees a cancelled run never spends a model call. Two
    # signals, either sufficient: the Redis flag the cancel request set, and
    # the row itself, which the request (or the startup reaper) may already
    # have closed. A row that says `cancelled` is not run, whoever said it.
    if run_service.cancel_requested(run_id) or run.status == "cancelled":
        run.status = "cancelled"
        run.finished_at = run.finished_at or datetime.now(timezone.utc)
        run.error_message = run.error_message or "Cancelled before it started."
        db.commit()
        _xadd_event(r, stream_key, event="run_done", data={
            "run_id": run_id, "status": "cancelled",
            "note": "Cancelled before it started.",
        })
        try:
            r.expire(stream_key, _STREAM_TTL_SECONDS)
        except Exception:
            pass
        run_service.clear_cancel_flag(run_id)
        try:
            if run.user_id is not None:
                r.srem(f"drpl:user:{run.user_id}:active_runs", run_id)
        except Exception:
            pass
        db.close()
        logger.info(f"run_router_task[{run_id}]: cancelled before start")
        return {"status": "cancelled", "run_id": run_id}

    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    # A row reaching this point is queued and unflagged, so it carries no
    # message; cleared anyway so a run can never finish "completed" wearing an
    # error written before it started.
    run.error_message = None
    db.commit()

    _xadd_event(r, stream_key, event="run_started", data={"run_id": run_id})

    final_status = "failed"
    error_msg: str | None = None
    try:
        asyncio.run(_pump(r, stream_key, args, run_id=run_id))
        final_status = "completed"
    except RunCancelled:
        # Not a failure — the user asked. `_pump` has already closed the
        # generator, so the pipeline's own `finally` blocks have run.
        final_status = "cancelled"
        error_msg = "Cancelled at the user's request."
        logger.info(f"run_router_task[{run_id}]: stopped on cancel request")
        _xadd_event(r, stream_key, event="cancelled", data={"run_id": run_id})
    except Exception as e:
        logger.error(f"run_router_task[{run_id}] failed: {e}", exc_info=True)
        error_msg = str(e)[:2000]
        _xadd_event(r, stream_key, event="error", data={"message": error_msg})

    try:
        # Re-read, not reuse: `_pump` wrote partial_output through its own
        # sessions, and the identity map still holds the row as first loaded.
        db.expire_all()
        run2 = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if run2:
            run2.status = final_status
            run2.finished_at = datetime.now(timezone.utc)
            if error_msg:
                run2.error_message = error_msg
            db.commit()

        # A run that ended without finishing writes what it had into the
        # session history — the transcript the user reloads into — so the
        # answer does not disappear with Redis's one-hour stream. A completed
        # run saved its own answer inside the pipeline; nothing to add.
        if run2 and final_status in ("cancelled", "failed"):
            reason = (
                "was stopped at your request" if final_status == "cancelled"
                else "stopped because of an error"
            )
            try:
                run_service.save_partial_turn(db, run2, reason=reason)
            except Exception as pe:  # noqa: BLE001 — never mask the terminal write
                logger.warning(f"run_router_task[{run_id}] partial turn failed: {pe}")
                try: db.rollback()
                except Exception: pass

        # Notify the run's owner when it ended in failure. Best-effort: never
        # mutates RQ job result on failure of the notification itself.
        # Only a genuine failure is worth a notification. A cancellation is
        # something the user just did; telling them it happened is noise.
        if run2 and final_status == "failed" and run2.user_id:
            try:
                from app.services import notification_service
                notification_service.create(
                    db,
                    user_id=run2.user_id,
                    kind="agent_run.failed",
                    title="Agent run failed",
                    body=(error_msg or "An agent run on your session encountered an error.")[:500],
                    action_url=(
                        f"/command-center/{run2.proposal_session_id}"
                        if run2.proposal_session_id else "/command-center"
                    ),
                    tender_id=run2.tender_id,
                )
                db.commit()
            except Exception as ne:
                logger.warning(f"run_router_task[{run_id}] notification dispatch failed: {ne}")
                try: db.rollback()
                except Exception: pass
    except Exception as e:
        logger.error(f"run_router_task[{run_id}] status write failed: {e}")
        try: db.rollback()
        except Exception: pass

    run_service.clear_cancel_flag(run_id)

    _xadd_event(
        r, stream_key,
        event="run_done",
        data={"run_id": run_id, "status": final_status, "error": error_msg},
    )
    try:
        r.expire(stream_key, _STREAM_TTL_SECONDS)
    except Exception:
        pass

    # Stage 2: release the user's active-run slot so the per-user cap
    # doesn't leak when runs finish. Best-effort — the EXPIRE set at
    # admission time (1h) is the backstop.
    try:
        user_id = args.get("user_id")
        if user_id is not None:
            r.srem(f"drpl:user:{user_id}:active_runs", run_id)
    except Exception:
        pass

    db.close()
    return {"status": final_status, "run_id": run_id}


async def _pump(r, stream_key: str, args: dict, *, run_id: str | None = None) -> None:
    """Consume the async generator and fan-out each SSE chunk to Redis.

    Between chunks it asks whether the run has been cancelled. That boundary is
    the whole design: it is a point the pipeline chose to stop at, so nothing
    is half-written, and every event the agent emits — tokens, tool calls,
    budget ticks, a sub-agent starting or finishing — is one, so the check is
    frequent without costing anything between them.

    On cancellation the generator is closed explicitly rather than left to the
    garbage collector, so `stream_router_response`'s own `finally` blocks run
    now, while the session is still alive, instead of at some later collection.
    """
    from app.core.database import SessionLocal
    from app.services.langchain.streaming_handler import stream_router_response

    pump_db = SessionLocal()
    stream = stream_router_response(db=pump_db, **args)
    cancelled = False
    last_check = float("-inf")  # so the first event always checks
    # What the run has said and done so far, kept on the row as it goes.
    # Redis holds the full stream for an hour; this is what survives it.
    progress = run_service.PartialProgress()
    last_flush = time.monotonic()
    try:
        async for chunk in stream:
            # `chunk` is already SSE-formatted; we preserve it verbatim in
            # the "sse" field and the /events endpoint replays it as-is.
            _xadd_event(r, stream_key, event="sse", sse=chunk)
            progress.note(chunk)

            now = time.monotonic()
            if run_id and now - last_flush >= _PARTIAL_FLUSH_INTERVAL_S:
                last_flush = now
                run_service.record_partial_progress(run_id, progress)
            if run_id and now - last_check >= _CANCEL_CHECK_INTERVAL_S:
                last_check = now
                if run_service.cancel_requested(run_id):
                    cancelled = True
                    break
    finally:
        # `break` leaves the generator suspended at its yield; closing it
        # here throws GeneratorExit in at that point, which is what runs
        # the pipeline's own `finally` blocks — now, while the session is
        # still alive, rather than at some later collection.
        try:
            await stream.aclose()
        except Exception as e:
            logger.debug(f"_pump: closing the stream raised {type(e).__name__}: {e}")
        pump_db.close()
        # Whatever the run had produced when it stopped — completed, cancelled
        # or failed — is on the row before anything else looks at it.
        if run_id:
            run_service.record_partial_progress(run_id, progress)

    if cancelled:
        raise RunCancelled(run_id or "")
