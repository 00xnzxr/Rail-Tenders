"""
DRPL Backend - User-facing notification endpoints.

Routes
------
GET    /api/notifications/                List for current user (paginated)
GET    /api/notifications/unread-count    Bell badge count
POST   /api/notifications/{id}/read       Mark one as read
POST   /api/notifications/mark-all-read   Mark all unread as read
GET    /api/notifications/sse             SSE stream (live updates)

SSE
---
The stream reads from `drpl:notifications:user:{user_id}` (a Redis Stream
maintained by `notification_service._publish_sse`). The pattern mirrors
``runs.router`` — XREAD with a 5s block, keepalive comments every ~15s, hard
deadline of 8h so a forgotten tab doesn't pin a worker forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, verify_token, verify_api_token
from app.core.database import get_db
from app.core.redis_client import get_async_redis
from app.models.notification import Notification
from app.models.user import User
from app.services.notification_service import user_stream_key


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _to_dict(n: Notification) -> dict:
    return {
        "id": n.id,
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "action_url": n.action_url,
        "tender_id": n.tender_id,
        "is_read": bool(n.is_read),
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "email_sent_at": n.email_sent_at.isoformat() if n.email_sent_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("/")
def list_notifications(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unread_only: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List notifications for the current user, newest first."""
    q = db.query(Notification).filter(Notification.user_id == current_user.id)
    if unread_only:
        q = q.filter(Notification.is_read == False)  # noqa: E712
    total = q.count()
    rows = (
        q.order_by(Notification.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_to_dict(n) for n in rows],
    }


@router.get("/unread-count")
def unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = (
        db.query(Notification)
        .filter(
            Notification.user_id == current_user.id,
            Notification.is_read == False,  # noqa: E712
        )
        .count()
    )
    return {"count": count}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    n = (
        db.query(Notification)
        .filter(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
        .first()
    )
    if n is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not n.is_read:
        n.is_read = True
        n.read_at = datetime.now(timezone.utc)
        db.commit()
    return _to_dict(n)


@router.post("/mark-all-read")
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    updated = (
        db.query(Notification)
        .filter(
            Notification.user_id == current_user.id,
            Notification.is_read == False,  # noqa: E712
        )
        .update({"is_read": True, "read_at": now}, synchronize_session=False)
    )
    db.commit()
    return {"marked_read": updated}


# ── SSE ────────────────────────────────────────────────────────────────────


@router.get("/sse")
async def notifications_sse(
    request: Request,
    last_id: str = Query("$", description="Resume from this stream id. '$' = only new entries."),
    token: Optional[str] = Query(None, description="JWT or API token (EventSource cannot set headers)."),
    db: Session = Depends(get_db),
):
    """Stream new notifications to the current user.

    Authentication: EventSource cannot set the Authorization header, so this
    endpoint also accepts the JWT (or `drpl_`-prefixed API token) via the
    `?token=` query parameter. Falls back to the standard Authorization header
    when present (e.g. when called via fetch).

    By default (`last_id="$"`), only entries that arrive after connection are
    delivered — the client should hydrate initial state via `GET /` and only
    rely on SSE for live deltas. Pass `last_id=0` to replay the whole stream.
    """
    # Resolve the current user from either the Authorization header or ?token=.
    auth_header = request.headers.get("Authorization") or ""
    raw_token = token or (auth_header.split(" ", 1)[1] if auth_header.lower().startswith("bearer ") else None)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Missing token")

    current_user: Optional[User] = None
    if raw_token.startswith("drpl_"):
        current_user = verify_api_token(raw_token, db)
    else:
        try:
            payload = verify_token(raw_token)
            user_id = payload.get("sub")
            if user_id:
                current_user = db.query(User).filter(User.id == int(user_id)).first()
        except Exception:
            current_user = None
    if current_user is None or not current_user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    client = get_async_redis()
    if client is None:
        raise HTTPException(status_code=503, detail="Redis unavailable.")

    stream_key = user_stream_key(current_user.id)

    async def event_stream() -> AsyncGenerator[bytes, None]:
        cursor = last_id or "$"
        idle_ticks = 0
        # 8h soft cap — abandoned tabs should not pin a worker forever.
        hard_deadline = asyncio.get_event_loop().time() + 60 * 60 * 8

        # Initial open frame so the browser EventSource fires `onopen`.
        yield b": connected\n\n"

        while True:
            if await request.is_disconnected():
                return
            if asyncio.get_event_loop().time() > hard_deadline:
                yield _sse("error", {"message": "Notification stream timed out."})
                return

            try:
                batch = await client.xread({stream_key: cursor}, block=5000, count=50)
            except Exception as e:
                logger.warning("notifications SSE: xread failed on %s: %s", stream_key, e)
                yield _sse("error", {"message": "Notification stream error."})
                return

            if not batch:
                idle_ticks += 1
                if idle_ticks >= 3:  # ~15s
                    yield b": keepalive\n\n"
                    idle_ticks = 0
                continue

            idle_ticks = 0
            for _stream, entries in batch:
                for entry_id, fields in entries:
                    cursor = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
                    data_raw = fields.get(b"data") or fields.get("data")
                    if data_raw is None:
                        continue
                    if isinstance(data_raw, (bytes, bytearray)):
                        data_raw = data_raw.decode("utf-8")
                    yield f"event: notification\ndata: {data_raw}\n\nid: {cursor}\n".encode("utf-8")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")
