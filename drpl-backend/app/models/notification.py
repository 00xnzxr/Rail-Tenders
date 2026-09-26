"""
DRPL Backend - Notification models.

Two tables:

* notifications              — per-user in-app event rows (also the audit trail
                                of what we attempted to email).
* notification_preferences   — system-wide admin defaults, one row per `kind`,
                                toggling whether the event creates an in-app
                                row and/or fires an email.

The Notification row is the source of truth; email is a side-effect that the
worker stamps `email_sent_at` onto when it succeeds.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey,
)

from app.core.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(64), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    action_url = Column(String(512), nullable=True)
    tender_id = Column(Integer, ForeignKey("tenders.id"), nullable=True, index=True)

    is_read = Column(Boolean, default=False, nullable=False, index=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    email_sent_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
        nullable=False,
    )


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    kind = Column(String(64), primary_key=True)
    in_app_enabled = Column(Boolean, default=True, nullable=False)
    email_enabled = Column(Boolean, default=True, nullable=False)
    description = Column(String(255), nullable=True)

    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
