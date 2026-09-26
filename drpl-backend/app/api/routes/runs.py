"""
DRPL Backend - Agent Runs API.

Phase C endpoints that let the UI enqueue an agent run and stream its
SSE events via Redis Streams (browser-close survivable, parallel-safe).

Endpoints
---------
POST /runs/enqueue
    Allocate an AgentRun row, push it onto RQ, return {run_id, rq_job_id}.
    Body: same payload as the command-center chat-stream endpoint.

GET /runs/{run_id}
    Status row for the run (for polling fallback / runs list UI).

GET /runs/{run_id}/events  (SSE)
    Replay the run's Redis Stream from ``?last_id=`` (default "0" =
    beginning) then block for new entries until a ``run_done`` event
    arrives. Re-read the full history on each reconnect by omitting
    ``last_id`` — the stream is retained for an hour.

Redis availability
------------------
All three endpoints require ``REDIS_URL``. When it's absent they return
503 so the UI can fall back to the legacy inline SSE endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.services.budget_service import assert_within_budget
from app.core.database import get_db
from app.core.redis_client import (
    get_async_redis, get_redis, is_redis_enabled, run_stream_key,
)
from app.core.run_context import run_id_scope
from app.models.user import User
from app.models.proposal import ProposalSession
from app.services import run_service

# Stage 2 fairness knobs. Env-overridable so we can tune without a deploy.
PER_USER_ACTIVE_CAP = int(os.getenv("COMMAND_CENTER_PER_USER_CAP", "2"))
GLOBAL_QUEUE_MAX = int(os.getenv("COMMAND_CENTER_GLOBAL_QUEUE_MAX", "30"))


def _user_active_key(user_id: int) -> str:
    return f"drpl:user:{user_id}:active_runs"


# Atomic admit: check cap + SADD in one round trip so two concurrent
# submits can't both squeak past SCARD.
_ADMIT_USER_LUA = """
local cap = tonumber(ARGV[1])
local run_id = ARGV[2]
local current = redis.call('SCARD', KEYS[1])
if current >= cap then return 0 end
redis.call('SADD', KEYS[1], run_id)
redis.call('EXPIRE', KEYS[1], 3600)
return 1
"""

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/runs", tags=["runs"])


# ── Schemas ──────────────────────────────────────────────────────────────

class EnqueueRunBody(BaseModel):
    message: str = Field(..., description="User message to send to the router.")
    display_message: Optional[str] = Field(
        None,
        description="What to show in chat history (defaults to `message`).",
    )
    proposal_session_id: Optional[int] = Field(
        None,
        description="Command Center session to attach the run to.",
    )
    tender_id: Optional[int] = Field(None, description="Tender context.")
    file_ids: Optional[list[int]] = Field(
        None,
        description="ChatAttachment ids to include with this run. Server will resolve + extract text.",
    )
    file_metadata: Optional[dict] = Field(
        None,
        description="Pre-resolved file_metadata (overrides file_ids resolution when set).",
    )
    selected_agents: Optional[list[str]] = Field(
        None,
        description="Optional hint for which agent(s) the router should dispatch to.",
    )


class RunStatus(BaseModel):
    id: str
    status: str
    user_id: int
    proposal_session_id: Optional[int]
    router_session_id: Optional[str]
    tender_id: Optional[int]
    created_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    error_message: Optional[str]
    rq_job_id: Optional[str]
    # What the run had streamed when it stopped, if it stopped early. The
    # same text is also written into the session history for a run that ended
    # in `cancelled` or `failed`; this is for tooling that has the run id.
    partial_output: Optional[str] = None
    has_partial_output: bool = False


def _run_to_status(run) -> RunStatus:
    def _iso(dt) -> Optional[str]:
        return dt.isoformat() if dt else None
    return RunStatus(
        id=run.id,
        status=run.status,
        user_id=run.user_id,
        proposal_session_id=run.proposal_session_id,
        router_session_id=run.router_session_id,
        tender_id=run.tender_id,
        created_at=_iso(run.created_at) or "",
        started_at=_iso(run.started_at),
        finished_at=_iso(run.finished_at),
        error_message=run.error_message,
        rq_job_id=run.rq_job_id,
        partial_output=getattr(run, "partial_output", None),
        has_partial_output=bool(getattr(run, "partial_output", None)),
    )


# ── POST /runs/enqueue ───────────────────────────────────────────────────

@router.post("/enqueue")
async def enqueue_run_endpoint(
    body: EnqueueRunBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not is_redis_enabled():
        raise HTTPException(
            status_code=503,
            detail="Redis/queue unavailable — fall back to inline /command-center/.../chat/stream.",
        )

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    # Same gate as the inline path — a queued run spends exactly as much.
    assert_within_budget(db, current_user)

    # Cheap pre-checks (fast-fail 429s before we do any DB writes).
    redis_client = get_redis()
    if redis_client is not None:
        try:
            from app.core.redis_client import get_queue
            q = get_queue()
            if q is not None and int(q.count) >= GLOBAL_QUEUE_MAX:
                raise HTTPException(
                    status_code=429,
                    detail={"error": "queue_full", "limit": GLOBAL_QUEUE_MAX},
                    headers={"Retry-After": "10"},
                )
            current_active = redis_client.scard(_user_active_key(current_user.id))
            if int(current_active or 0) >= PER_USER_ACTIVE_CAP:
                raise HTTPException(
                    status_code=429,
                    detail={"error": "concurrency_limit", "limit": PER_USER_ACTIVE_CAP},
                    headers={"Retry-After": "5"},
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.debug(f"runs.enqueue capacity pre-check failed (soft): {e}")

    # Resolve router_session_id from the proposal session if one was passed.
    # All sync SQLAlchemy calls are wrapped in asyncio.to_thread so they
    # don't block the uvicorn event loop — otherwise 10 concurrent submits
    # serialize behind a single handler and submit p95 balloons to ~30s.
    router_session_id: Optional[str] = None
    session = None
    if body.proposal_session_id is not None:
        def _load_session():
            return (
                db.query(ProposalSession)
                .filter(
                    ProposalSession.id == body.proposal_session_id,
                    ProposalSession.created_by == current_user.id,
                )
                .first()
            )
        session = await asyncio.to_thread(_load_session)
        if not session:
            raise HTTPException(status_code=404, detail="Proposal session not found")
        router_session_id = session.router_session_id

    # Resolve file attachments (shared logic with /chat/stream). Pre-resolved
    # file_metadata on the body wins; otherwise file_ids are looked up here so
    # the user_msg row and attachment links get created eagerly.
    file_context = ""
    file_metadata = body.file_metadata or {}
    file_ids = body.file_ids or []
    if file_ids and not body.file_metadata and body.proposal_session_id is not None:
        from app.api.routes.command_center import resolve_file_attachments
        file_context, file_metadata = await resolve_file_attachments(
            db, body.proposal_session_id, file_ids,
        )

    enriched_message = message + file_context if file_context else message

    # Persist user message + link attachments so the UI sees them immediately
    # (same pattern as /chat/stream — don't defer this to the worker).
    if body.proposal_session_id is not None:
        from app.models.proposal import ProposalMessage
        from app.models.chat_attachment import ChatAttachment

        def _persist_user_message():
            user_msg = ProposalMessage(
                session_id=body.proposal_session_id,
                role="user",
                content=message,
                message_type="text",
                metadata_json={"file_ids": file_ids} if file_ids else None,
            )
            db.add(user_msg)
            db.commit()
            if file_ids:
                db.query(ChatAttachment).filter(
                    ChatAttachment.id.in_(file_ids),
                    ChatAttachment.session_id == body.proposal_session_id,
                ).update({"message_id": user_msg.id}, synchronize_session="fetch")
                db.commit()

        await asyncio.to_thread(_persist_user_message)

    def _create_run():
        return run_service.create_run(
            db,
            user_id=current_user.id,
            prompt=enriched_message,
            display_message=body.display_message or message,
            proposal_session_id=body.proposal_session_id,
            router_session_id=router_session_id,
            tender_id=body.tender_id,
            file_metadata=file_metadata if file_metadata else None,
            selected_agents=body.selected_agents,
        )

    run = await asyncio.to_thread(_create_run)

    # Atomic admit — defends against the race between two concurrent submits
    # that both pass the cheap SCARD pre-check above. If we lose the race,
    # soft-fail the run (status=failed) so the row stays for audit.
    if redis_client is not None:
        try:
            admitted = redis_client.eval(
                _ADMIT_USER_LUA, 1,
                _user_active_key(current_user.id),
                PER_USER_ACTIVE_CAP, run.id,
            )
            if not int(admitted or 0):
                try:
                    run.status = "failed"
                    run.error_message = "concurrency_limit"
                    db.commit()
                except Exception:
                    db.rollback()
                raise HTTPException(
                    status_code=429,
                    detail={"error": "concurrency_limit", "limit": PER_USER_ACTIVE_CAP},
                    headers={"Retry-After": "5"},
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"runs.enqueue admit EVAL failed (allowing through): {e}")

    rq_job_id = await asyncio.to_thread(run_service.enqueue_run, run, db)
    if rq_job_id is None:
        # Release active-set membership so the cap doesn't leak.
        if redis_client is not None:
            try:
                redis_client.srem(_user_active_key(current_user.id), run.id)
            except Exception:
                pass
        raise HTTPException(status_code=503, detail="Queue enqueue failed.")

    with run_id_scope(run.id):
        logger.info(
            f"run.queued user_id={current_user.id} rq_job_id={rq_job_id} "
            f"session_id={body.proposal_session_id}"
        )

    return {
        "run_id": run.id,
        "rq_job_id": rq_job_id,
        "status": run.status,
        "events_url": f"/api/runs/{run.id}/events",
    }


# ── GET /runs/active ─────────────────────────────────────────────────────

@router.get("/active")
def get_active_run_endpoint(
    proposal_session_id: int = Query(..., description="Command Center session id."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The run still in flight for this session, if there is one.

    Durability without discovery is only half of it: the work survived the
    closed window, but the only pointer back to it lived in the `localStorage`
    of the browser that started it. Open the session on a second device, in
    another browser, or after clearing site data, and a run that was very much
    alive was unreachable — which looks exactly like a run that died.

    Always 200. `{"run": null}` is the ordinary answer.
    """
    run = run_service.find_live_run_for_session(
        db, user_id=current_user.id, proposal_session_id=proposal_session_id
    )
    return {"run": _run_to_status(run).model_dump() if run else None}


# ── GET /runs/{run_id} ───────────────────────────────────────────────────

@router.get("/{run_id}", response_model=RunStatus)
def get_run_endpoint(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = run_service.get_run_for_user(db, run_id, current_user.id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_to_status(run)


# ── GET /runs ────────────────────────────────────────────────────────────

@router.get("")
def list_runs_endpoint(
    proposal_session_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    runs = run_service.list_runs_for_user(
        db,
        user_id=current_user.id,
        proposal_session_id=proposal_session_id,
        limit=limit,
    )
    return {"runs": [_run_to_status(r).model_dump() for r in runs]}


# ── POST /runs/{run_id}/cancel ───────────────────────────────────────────

@router.post("/{run_id}/cancel")
def cancel_run_endpoint(
    run_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stop a run.

    The Stop button used to abort the browser's fetch and nothing else: the
    browser stopped listening, the worker carried on to completion, and the
    user kept being billed for a run they had told to stop. On a platform that
    meters per user and enforces a monthly cap, that is not a missing
    convenience.

    Two live outcomes, and they are genuinely different:

    - ``cancelled``  — nothing had started; the job is out of the queue and the
      row is closed. No model call was ever spent on it.
    - ``cancelling`` — a worker is executing it and has been asked to stop. It
      ends at its next event boundary, and the stream's `run_done` says so.

    Reporting the second as "cancelled" would be the same lie as the button
    this replaces, so it does not.

    Idempotent: cancelling a finished run is a 200 that says it had finished.
    """
    run = run_service.get_run_for_user(db, run_id, current_user.id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    try:
        return run_service.request_cancel(db, run)
    except RuntimeError as e:
        # Redis is how the web process reaches the worker. Without it the
        # honest answer is that we cannot stop the run, not a 200 implying we
        # did.
        raise HTTPException(status_code=503, detail=str(e))


# ── GET /runs/{run_id}/events (SSE) ──────────────────────────────────────

@router.get("/{run_id}/events")
async def run_events_endpoint(
    run_id: str,
    request: Request,
    last_id: str = Query("0", description="Resume from this stream id (default '0' = whole history)."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    run = run_service.get_run_for_user(db, run_id, current_user.id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    client = get_async_redis()
    if client is None:
        raise HTTPException(status_code=503, detail="Redis unavailable.")

    stream_key = run_stream_key(run_id)

    async def event_stream() -> AsyncGenerator[bytes, None]:
        cursor = last_id or "0"
        idle_ticks = 0
        redis_failures = 0
        # Absolute wall-clock cap: 40 min. RQ job_timeout is 30 min, so the
        # worker will have published `run_done` by then in every normal
        # path. This guards against a truly stuck run with no sentinel.
        hard_deadline = asyncio.get_event_loop().time() + 60 * 40

        while True:
            if await request.is_disconnected():
                return
            if asyncio.get_event_loop().time() > hard_deadline:
                yield _sse("error", {"message": "Event stream timed out."})
                return

            try:
                # XREAD blocks up to 5s for new entries. If nothing arrives
                # we loop, send a keepalive comment, and check for disconnect.
                batch = await client.xread({stream_key: cursor}, block=5000, count=100)
                redis_failures = 0
            except Exception as e:
                # Redis went away under the read. It used to end the stream
                # here with "Event stream error." on the first failure; the
                # managed Redis security patch restarted the server for
                # seconds, and every open costing lost its stream while the
                # worker kept running. Retry with backoff, keeping the
                # browser's fetch alive with a comment, and give up only once
                # the retry window is spent. The cursor is kept, so nothing
                # published in the meantime is skipped.
                redis_failures += 1
                delay = _redis_retry_delay(redis_failures)
                if delay is None:
                    logger.warning(
                        f"runs SSE: xread failed on {stream_key} after "
                        f"{redis_failures} attempts, giving up: {e}"
                    )
                    yield _sse("error", {"message": "Event stream error."})
                    return
                logger.warning(
                    f"runs SSE: xread failed on {stream_key} (attempt "
                    f"{redis_failures}, retrying in {delay}s): {e}"
                )
                yield b": keepalive\n\n"
                await asyncio.sleep(delay)
                continue

            if not batch:
                idle_ticks += 1
                # Keepalive comment every ~15s (3 empty reads)
                if idle_ticks >= 3:
                    # Nothing has arrived for 15s. Before promising the client
                    # more, check whether there is still anybody to send it: a
                    # worker killed by SIGKILL or a deploy never publishes
                    # `run_done`, so this loop would keepalive against a dead
                    # stream until the 40-minute deadline. The row is the
                    # canonical truth (see sop-agent-runs-rq.md), so when it
                    # has gone terminal without a sentinel, say so and stop.
                    terminal = _terminal_status(run_id)
                    if terminal is not None:
                        yield _sse("run_done", {
                            "run_id": run_id,
                            "status": terminal,
                            "note": "Run ended without publishing a final event.",
                        })
                        return
                    yield b": keepalive\n\n"
                    idle_ticks = 0
                continue

            idle_ticks = 0
            # batch: [(stream_key_bytes, [(id_bytes, {field_bytes: val_bytes}), ...])]
            for _stream, entries in batch:
                for entry_id, fields in entries:
                    cursor = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
                    event_name = _field(fields, "event") or "sse"
                    sse_verbatim = _field(fields, "sse")
                    data_json = _field(fields, "data")

                    if sse_verbatim is not None:
                        # Already SSE-formatted upstream — replay as-is.
                        yield sse_verbatim.encode("utf-8") if isinstance(sse_verbatim, str) else sse_verbatim
                    else:
                        # Synthetic events (run_started, run_done, error)
                        yield f"event: {event_name}\ndata: {data_json or '{}'}\n\nid: {cursor}\n".encode("utf-8")

                    if event_name in ("run_done",):
                        return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _terminal_status(run_id: str) -> Optional[str]:
    """The run's status if it has finished, else None.

    Deliberately on its own short-lived session rather than the request's: an
    events stream stays open for up to 40 minutes, and a session held across
    that is exactly the idle-in-transaction connection Neon reaps out from
    under us. Best-effort — a failed read means "keep waiting", which is the
    behaviour this check is an improvement on, not a regression from.
    """
    from app.core.database import SessionLocal
    from app.models.agent_run import AgentRun

    probe = SessionLocal()
    try:
        status = (
            probe.query(AgentRun.status).filter(AgentRun.id == run_id).scalar()
        )
        return status if status not in run_service.LIVE_STATUSES else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"runs SSE: status probe failed for {run_id}: {e}")
        return None
    finally:
        try:
            probe.close()
        except Exception:
            pass


#: Total seconds the events stream keeps retrying a failing Redis read before
#: telling the browser the stream is gone.
_SSE_REDIS_RETRY_TOTAL_S = 180
_SSE_REDIS_RETRY_MAX_DELAY_S = 15


def _redis_retry_delay(attempt: int) -> Optional[float]:
    """Seconds to wait before retry number `attempt` (1-based), or None when
    the retry window is spent. Backs off 1, 2, 4, 8, 15, 15 ... seconds."""
    spent = sum(
        min(2 ** (k - 1), _SSE_REDIS_RETRY_MAX_DELAY_S) for k in range(1, attempt)
    )
    if spent >= _SSE_REDIS_RETRY_TOTAL_S:
        return None
    return float(min(2 ** (attempt - 1), _SSE_REDIS_RETRY_MAX_DELAY_S))


def _field(fields, name: str) -> Optional[str]:
    """Pluck a value from an XREAD field-map that may be bytes-keyed or str-keyed."""
    if not fields:
        return None
    key_bytes = name.encode() if isinstance(name, str) else name
    val = fields.get(key_bytes) if key_bytes in fields else fields.get(name)
    if val is None:
        return None
    return val.decode("utf-8") if isinstance(val, (bytes, bytearray)) else val


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
