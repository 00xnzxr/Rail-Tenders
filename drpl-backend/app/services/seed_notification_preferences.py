"""
DRPL Backend - Seed default notification preferences.

Called from ``app/main.py`` at startup. Idempotent — only inserts rows for
kinds that don't already exist. Admin edits made via
``PUT /api/admin/notification-preferences/{kind}`` are preserved across
restarts.
"""

from __future__ import annotations

import logging
from sqlalchemy.orm import Session

from app.models.notification import NotificationPreference


log = logging.getLogger(__name__)


# Canonical list of notification kinds. Every event hookpoint in the code must
# use one of these exact strings. New kinds added here are seeded on next boot.
DEFAULT_PREFERENCES: list[tuple[str, bool, bool, str]] = [
    # (kind, in_app_enabled, email_enabled, description)
    ("tender.assigned",            True, True,  "A tender is assigned to you"),
    ("tender.analysis_complete",   True, True,  "Deep analysis of a tender's documents is finished"),
    ("tender.costing_complete",    True, True,  "A costing run for your tender has finalised"),
    ("tender.annexures_extracted", True, False, "Annexures discovered and added to a tender's workspace"),
    ("tender.closing_soon",        True, True,  "An assigned tender's submission deadline is within 48 hours"),
    ("proposal.submitted",         True, True,  "A proposal has been submitted for review (admins only)"),
    ("proposal.approved",          True, True,  "Your proposal has been approved"),
    ("proposal.rejected",          True, True,  "Your proposal has been rejected"),
    ("proposal.changes_requested", True, True,  "Reviewer requested changes on your proposal"),
    ("agent_run.failed",           True, False, "An agent run on your session failed"),
    ("digest.daily",               False, True, "Daily summary email of activity on your tenders"),
]


def seed_notification_preferences(db: Session) -> int:
    """Insert any missing rows. Returns count of rows created."""
    existing_kinds = {
        row.kind for row in db.query(NotificationPreference.kind).all()
    }
    created = 0
    for kind, in_app, email, description in DEFAULT_PREFERENCES:
        if kind in existing_kinds:
            continue
        db.add(NotificationPreference(
            kind=kind,
            in_app_enabled=in_app,
            email_enabled=email,
            description=description,
        ))
        created += 1
    if created:
        db.commit()
        log.info("seed_notification_preferences: inserted %d new preference rows", created)
    return created
