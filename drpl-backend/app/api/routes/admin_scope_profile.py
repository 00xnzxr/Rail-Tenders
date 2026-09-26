"""
DRPL Backend - Admin Tender Scope Profile Routes

Master-admin CRUD for the singleton TenderScopeProfile that drives the
extension's GeM auto-search keyword strategy and the relevance agent's
prompt context. Modeled after admin_settings.py.
"""

from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_master_admin
from app.core.database import get_db
from app.models.user import User
from app.services.audit_service import log_action
from app.services.scope_profile_service import (
    get_active_profile,
    serialize_for_admin,
    upsert_profile,
)

router = APIRouter(prefix="/admin/scope-profile", tags=["admin-scope-profile"])


class KeywordGroupInput(BaseModel):
    label: str
    keywords: list[str] = []


class ScopeProfileInput(BaseModel):
    keyword_groups: Optional[list[KeywordGroupInput]] = None
    exclusion_terms: Optional[list[str]] = None
    target_ministries: Optional[list[str]] = None
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    relevance_threshold: Optional[float] = None
    max_pages_per_keyword: Optional[int] = None
    is_active: Optional[bool] = None


class HistoricalFallback(BaseModel):
    keywords: list[str] = []
    ministries: list[str] = []
    departments: list[str] = []


class ScopeProfileResponse(BaseModel):
    id: int
    name: str
    keyword_groups: list[dict]
    exclusion_terms: list[str]
    target_ministries: list[str]
    value_min: Optional[float]
    value_max: Optional[float]
    relevance_threshold: float
    max_pages_per_keyword: int = 5
    historical_fallback: HistoricalFallback = HistoricalFallback()
    is_active: bool
    updated_by: Optional[int]
    updated_at: Optional[str]
    created_at: Optional[str]


@router.get("/", response_model=ScopeProfileResponse)
def get_profile(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Return the active scope profile, auto-creating defaults on first call."""
    profile = get_active_profile(db)
    return serialize_for_admin(profile, db=db)


@router.put("/", response_model=ScopeProfileResponse)
def update_profile(
    body: ScopeProfileInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Upsert fields on the active scope profile."""
    payload: dict[str, Any] = body.model_dump(exclude_unset=True)
    if "keyword_groups" in payload and payload["keyword_groups"] is not None:
        payload["keyword_groups"] = [g for g in payload["keyword_groups"]]

    profile = upsert_profile(db, payload, admin.id)
    log_action(
        db,
        admin.id,
        admin.email,
        "scope_profile.updated",
        "scope_profile",
        str(profile.id),
        {"fields": list(payload.keys())},
    )
    return serialize_for_admin(profile, db=db)
