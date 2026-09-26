"""
DRPL Backend - Admin notification preferences API.

Master-admin only. Lets an admin toggle which kinds are delivered in-app
and/or via email, system-wide. There is no per-user override in v1.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.notification import NotificationPreference
from app.models.user import User


router = APIRouter(prefix="/api/admin/notification-preferences", tags=["admin-notifications"])


def _require_master_admin(user: User) -> None:
    if user.role != "master_admin":
        raise HTTPException(status_code=403, detail="Master admin required")


class PreferenceUpdate(BaseModel):
    in_app_enabled: bool | None = None
    email_enabled: bool | None = None


def _to_dict(p: NotificationPreference) -> dict:
    return {
        "kind": p.kind,
        "in_app_enabled": bool(p.in_app_enabled),
        "email_enabled": bool(p.email_enabled),
        "description": p.description,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("/")
def list_preferences(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_master_admin(current_user)
    rows = db.query(NotificationPreference).order_by(NotificationPreference.kind).all()
    return {"items": [_to_dict(p) for p in rows]}


@router.put("/{kind}")
def update_preference(
    kind: str,
    body: PreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_master_admin(current_user)
    pref = db.query(NotificationPreference).filter(NotificationPreference.kind == kind).first()
    if pref is None:
        raise HTTPException(status_code=404, detail=f"Unknown notification kind: {kind}")
    if body.in_app_enabled is not None:
        pref.in_app_enabled = body.in_app_enabled
    if body.email_enabled is not None:
        pref.email_enabled = body.email_enabled
    db.commit()
    db.refresh(pref)
    return _to_dict(pref)
