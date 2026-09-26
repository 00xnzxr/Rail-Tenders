"""
DRPL Backend - Notification service.

One public entry point for the rest of the app: :func:`create`. It is invoked
from event hookpoints (assignment, review decisions, agent failures, etc.).

The function:

1. Looks up the system-wide :class:`NotificationPreference` row for the kind.
   If the kind is unknown or both channels are disabled, the call is a no-op.
2. Inserts a :class:`Notification` row (when in-app is enabled).
3. Publishes the new row to the per-user Redis stream so the SSE endpoint
   can push it to a connected browser.
4. Enqueues an RQ job to send the email (when email is enabled).

Transaction policy
------------------
``create()`` flushes but does NOT commit. The caller's outer transaction
commits at its usual point (e.g. after the workflow_status update). That keeps
the notification atomic with the event that triggered it — if the event fails,
the notification rolls back too.

Redis / RQ are best-effort: when they're disabled (`REDIS_URL` empty) the
in-app row is still written and the function returns normally.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.core.redis_client import get_redis, get_queue
from app.models.notification import Notification, NotificationPreference
from app.models.user import User


log = logging.getLogger(__name__)


def create(
    db: Session,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: Optional[str] = None,
    action_url: Optional[str] = None,
    tender_id: Optional[int] = None,
) -> Optional[Notification]:
    """Dispatch a notification to a single user. Returns the in-app row or
    `None` if the kind is disabled / unknown.
    """
    pref = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.kind == kind)
        .first()
    )
    if pref is None:
        log.debug("notification_service: no preference row for kind=%s — skipping", kind)
        return None
    if not pref.in_app_enabled and not pref.email_enabled:
        return None

    notif: Optional[Notification] = None
    if pref.in_app_enabled:
        notif = Notification(
            user_id=user_id,
            kind=kind,
            title=title,
            body=body,
            action_url=action_url,
            tender_id=tender_id,
        )
        db.add(notif)
        db.flush()  # populate notif.id, attach to caller's transaction
        _publish_sse(user_id, notif)

    if pref.email_enabled and notif is not None:
        _enqueue_email(notif.id)

    return notif


def dispatch_to_admins(
    db: Session,
    *,
    kind: str,
    title: str,
    body: Optional[str] = None,
    action_url: Optional[str] = None,
    tender_id: Optional[int] = None,
    exclude_user_id: Optional[int] = None,
) -> int:
    """Send the same notification to every active admin / master_admin.

    Returns the number of in-app rows created (may be 0 if the kind is
    disabled). Caller is responsible for `db.commit()`.
    """
    admins = (
        db.query(User)
        .filter(User.is_active == True, User.role.in_(("admin", "master_admin")))  # noqa: E712
        .all()
    )
    created = 0
    for admin in admins:
        if exclude_user_id is not None and admin.id == exclude_user_id:
            continue
        if create(
            db,
            user_id=admin.id,
            kind=kind,
            title=title,
            body=body,
            action_url=action_url,
            tender_id=tender_id,
        ) is not None:
            created += 1
    return created


# ── Internal helpers ───────────────────────────────────────────────────────


def user_stream_key(user_id: int) -> str:
    """Canonical Redis Streams key for a user's notification feed.

    Subscribers (the SSE endpoint) must read from the same key. Capped at
    100 entries via XADD MAXLEN so the stream never grows unbounded for an
    offline user.
    """
    return f"drpl:notifications:user:{user_id}"


def _publish_sse(user_id: int, notif: Notification) -> None:
    """Best-effort XADD to the user's Redis stream. Never raises."""
    client = get_redis()
    if client is None:
        return
    try:
        payload = {
            "id": notif.id,
            "kind": notif.kind,
            "title": notif.title,
            "body": notif.body,
            "action_url": notif.action_url,
            "tender_id": notif.tender_id,
            "is_read": notif.is_read,
            "created_at": _iso(notif.created_at),
        }
        client.xadd(
            user_stream_key(user_id),
            {"data": json.dumps(payload, default=str)},
            maxlen=100,
            approximate=True,
        )
    except Exception as e:
        log.debug("notification_service: XADD failed for user=%s: %s", user_id, e)


def _enqueue_email(notification_id: int) -> None:
    """Best-effort RQ enqueue. Never raises — failure means email is dropped
    but the in-app row is still written.
    """
    q = get_queue()
    if q is None:
        log.debug("notification_service: queue unavailable — email skipped for notif=%s",
                  notification_id)
        return
    try:
        # Import inside to avoid pulling worker modules at app import time.
        q.enqueue(
            "app.worker.email_tasks.send_notification_email",
            notification_id,
            job_timeout=60,
            result_ttl=3600,
        )
    except Exception as e:
        log.warning("notification_service: enqueue failed for notif=%s: %s",
                    notification_id, e)


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
