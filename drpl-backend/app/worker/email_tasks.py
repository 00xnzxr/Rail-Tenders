"""
DRPL Backend - RQ worker tasks for notification emails.

This module is imported lazily by RQ when a job runs — it must be importable
without side effects (no DB connections at import time).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.database import SessionLocal
from app.models.notification import Notification
from app.models.user import User
from app.services import email_service
from app.services.notification_templates import render_notification_html


log = logging.getLogger(__name__)


def send_notification_email(notification_id: int) -> dict:
    """Send the email for a single Notification row.

    Stamps ``email_sent_at`` on success so retries don't double-send. Returns
    a dict for RQ's result store (`{"sent": True, "id": ...}` /
    `{"skipped": "..."}`). Raises on transport errors so RQ records the job
    as failed and the operator sees it.
    """
    db = SessionLocal()
    try:
        notif = db.query(Notification).filter(Notification.id == notification_id).first()
        if notif is None:
            return {"skipped": "notification_deleted"}
        if notif.email_sent_at is not None:
            return {"skipped": "already_sent"}

        user = db.query(User).filter(User.id == notif.user_id).first()
        if user is None or not user.is_active or not user.email:
            return {"skipped": "no_recipient"}

        html = render_notification_html(notif, user)
        result = email_service.send_email(
            to=user.email,
            subject=notif.title,
            html=html,
        )

        if result.get("skipped"):
            # Email service deliberately bypassed (no API key etc.) — leave
            # email_sent_at null so a future retry can pick it up once
            # the operator configures Resend.
            return result

        notif.email_sent_at = datetime.now(timezone.utc)
        db.commit()
        return {"sent": True, "id": notification_id, "resend_id": result.get("id")}
    finally:
        db.close()
