"""
DRPL Backend - Admin User Management Routes
User CRUD, role assignment, activate/deactivate (master_admin only)
"""

from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from passlib.context import CryptContext

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.models.user import User
from app.services.audit_service import log_action

router = APIRouter(prefix="/admin/users", tags=["admin-users"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class CreateUserInput(BaseModel):
    email: str
    name: str
    password: str
    role: str = "costing_research"

class UpdateUserInput(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None

class ResetPasswordInput(BaseModel):
    new_password: str

class UserResponse(BaseModel):
    id: int
    email: str
    name: str
    role: str
    is_active: bool
    last_login_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


# The three current roles plus the legacy values existing users still hold —
# accepted so an admin editing an unrelated field on a legacy user does not
# have to re-role them, and so nobody is locked out mid-transition.
# app/core/roles.py maps the legacy pair forward.
VALID_ROLES = {
    "master_admin", "tender_search", "costing_research",
    "operator", "admin",
}


@router.get("/", response_model=list[UserResponse])
def list_users(
    role: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all users with optional filters."""
    query = db.query(User)
    if role:
        query = query.filter(User.role == role)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)
    if search:
        query = query.filter(
            (User.name.ilike(f"%{search}%")) | (User.email.ilike(f"%{search}%"))
        )
    users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
    results = []
    for u in users:
        results.append(UserResponse(
            id=u.id, email=u.email, name=u.name, role=u.role, is_active=u.is_active,
            last_login_at=u.last_login_at.isoformat() if u.last_login_at else None,
            created_at=u.created_at.isoformat() if u.created_at else None,
            updated_at=u.updated_at.isoformat() if u.updated_at else None,
        ))
    return results


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get a user by ID."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(
        id=user.id, email=user.email, name=user.name, role=user.role, is_active=user.is_active,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
        created_at=user.created_at.isoformat() if user.created_at else None,
        updated_at=user.updated_at.isoformat() if user.updated_at else None,
    )


@router.post("/", response_model=UserResponse)
def create_user(
    body: CreateUserInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Create a new user."""
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")

    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=body.email,
        name=body.name,
        hashed_password=pwd_context.hash(body.password),
        role=body.role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_action(db, admin.id, admin.email, "user.created", "user", str(user.id), {"email": body.email, "role": body.role})
    return UserResponse(
        id=user.id, email=user.email, name=user.name, role=user.role, is_active=user.is_active,
        created_at=user.created_at.isoformat() if user.created_at else None,
    )


@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    body: UpdateUserInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update a user's name, email, or role."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.name is not None:
        user.name = body.name
    if body.email is not None:
        existing = db.query(User).filter(User.email == body.email, User.id != user_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Email already in use")
        user.email = body.email
    if body.role is not None:
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {VALID_ROLES}")
        user.role = body.role

    user.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    log_action(db, admin.id, admin.email, "user.updated", "user", str(user_id), body.model_dump(exclude_none=True))
    return UserResponse(
        id=user.id, email=user.email, name=user.name, role=user.role, is_active=user.is_active,
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
        created_at=user.created_at.isoformat() if user.created_at else None,
        updated_at=user.updated_at.isoformat() if user.updated_at else None,
    )


@router.patch("/{user_id}/activate")
def activate_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Activate a user."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = True
    db.commit()
    log_action(db, admin.id, admin.email, "user.activated", "user", str(user_id))
    return {"status": "activated", "user_id": user_id}


@router.patch("/{user_id}/deactivate")
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Deactivate a user."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = False
    db.commit()
    log_action(db, admin.id, admin.email, "user.deactivated", "user", str(user_id))
    return {"status": "deactivated", "user_id": user_id}


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    body: ResetPasswordInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Reset a user's password."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.hashed_password = pwd_context.hash(body.new_password)
    user.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_action(db, admin.id, admin.email, "user.password_reset", "user", str(user_id))
    return {"status": "password_reset", "user_id": user_id}
