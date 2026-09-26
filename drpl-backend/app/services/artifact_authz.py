"""Ownership checks for Command Center artifacts.

`artifact_service.get_artifact()` deliberately filters on id alone — it is used
by background jobs that have no user context. Every HTTP route that exposes an
artifact must therefore apply this check itself.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.proposal import ProposalSession

logger = logging.getLogger(__name__)


def assert_artifact_access(db: Session, artifact, user_id: int) -> None:
    """Raise 403 unless `user_id` created the session this artifact belongs to.

    Fails closed: an artifact with no session, or whose session row is gone, is
    denied rather than treated as unowned and therefore public.

    No role exemption here. master_admin *does* see every user's work
    (app/core/roles.py), but that exemption is applied by the caller — see
    `command_center._load_owned_session` — so this helper stays a plain
    ownership predicate rather than a role-aware one.
    """
    session_id = getattr(artifact, "session_id", None)
    if not session_id:
        logger.warning(
            "[artifact authz] artifact %s has no session_id — denying user %s",
            getattr(artifact, "id", "?"), user_id,
        )
        raise HTTPException(status_code=403, detail="Not authorized for this artifact")

    owner_id = (
        db.query(ProposalSession.created_by)
        .filter(ProposalSession.id == session_id)
        .scalar()
    )
    if owner_id is None or owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this artifact")


def assert_session_access(db: Session, session_id: int, user_id: int) -> None:
    """Raise 404 if the session is absent, 403 unless `user_id` created it.

    Separate from `assert_artifact_access` because the caller here has only an
    id: a missing session is a genuine 404, whereas an artifact pointing at a
    vanished session is a fail-closed 403.
    """
    owner_id = (
        db.query(ProposalSession.created_by)
        .filter(ProposalSession.id == session_id)
        .scalar()
    )
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if owner_id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this session")
