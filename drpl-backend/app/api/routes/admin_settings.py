"""
DRPL Backend - Admin Settings Routes
Platform settings management (master_admin only)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.models.user import User
from app.services.settings_service import (
    get_all_settings, get_settings_by_category, get_setting,
    set_setting, reset_setting, seed_defaults,
)
from app.services.audit_service import log_action

router = APIRouter(prefix="/admin/settings", tags=["admin-settings"])


class SettingResponse(BaseModel):
    id: int
    key: str
    value: str
    value_type: str
    category: str
    description: Optional[str]
    is_secret: bool
    #: Whether a value is actually stored. Only interesting for a secret,
    #: where `value` is the mask either way -- and where the page used to
    #: render an empty password box whichever it was, so a key that had never
    #: been set looked exactly like one that had. On a tab with 55 settings
    #: that is not a cosmetic problem: it is how someone concludes a key is
    #: configured, tries the thing it powers, and gets an authorisation error
    #: they then go looking for in the wrong place.
    is_set: bool = False
    updated_by: Optional[int]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class UpdateSettingInput(BaseModel):
    value: Any


@router.get("/", response_model=list[SettingResponse])
def list_settings(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all platform settings."""
    seed_defaults(db)
    settings = get_all_settings(db)
    results = []
    for s in settings:
        val = s.value if not s.is_secret else "••••••••"
        results.append(SettingResponse(
            id=s.id, key=s.key, value=val, value_type=s.value_type,
            category=s.category, description=s.description, is_secret=s.is_secret,
            is_set=bool((s.value or "").strip()),
            updated_by=s.updated_by,
            updated_at=s.updated_at.isoformat() if s.updated_at else None,
        ))
    return results


@router.get("/{category}", response_model=list[SettingResponse])
def list_settings_by_category(
    category: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List settings by category."""
    seed_defaults(db)
    settings = get_settings_by_category(db, category)
    results = []
    for s in settings:
        val = s.value if not s.is_secret else "••••••••"
        results.append(SettingResponse(
            id=s.id, key=s.key, value=val, value_type=s.value_type,
            category=s.category, description=s.description, is_secret=s.is_secret,
            is_set=bool((s.value or "").strip()),
            updated_by=s.updated_by,
            updated_at=s.updated_at.isoformat() if s.updated_at else None,
        ))
    return results


@router.put("/{key}")
def update_setting(
    key: str,
    body: UpdateSettingInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update a setting value."""
    try:
        setting = set_setting(db, key, body.value, admin.id)
        log_action(db, admin.id, admin.email, "setting.updated", "setting", key, {"value": str(body.value)})
        return {"key": setting.key, "value": setting.value, "status": "updated"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/reset/{key}")
def reset_setting_endpoint(
    key: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Reset a setting to its default value."""
    result = reset_setting(db, key, admin.id)
    if not result:
        raise HTTPException(status_code=404, detail="Setting not found or no default available")
    log_action(db, admin.id, admin.email, "setting.reset", "setting", key)
    return {"key": result.key, "value": result.value, "status": "reset"}
