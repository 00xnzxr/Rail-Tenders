"""
DRPL Backend - Authentication
JWT token creation/verification + API token support for extension
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models.user import User
from app.models.api_token import APIToken

settings = get_settings()
security = HTTPBearer()


# --- JWT ---

def create_access_token(user_id: int, email: str) -> str:
    """Create a JWT token for a user."""
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiry_hours)
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def verify_token(token: str) -> dict:
    """Verify and decode a JWT token."""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


# --- API Tokens ---

def hash_token(raw_token: str) -> str:
    """SHA-256 hash of an API token."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def create_api_token(db: Session, user_id: int, name: str) -> tuple[str, APIToken]:
    """Generate a new API token. Returns (raw_token, db_record)."""
    raw_token = "drpl_" + secrets.token_hex(20)
    token_obj = APIToken(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        name=name,
        prefix=raw_token[:12] + "...",
        is_active=True,
    )
    db.add(token_obj)
    db.commit()
    db.refresh(token_obj)
    return raw_token, token_obj


def verify_api_token(raw_token: str, db: Session) -> Optional[User]:
    """Verify an API token and return the associated user."""
    token_record = db.query(APIToken).filter(
        APIToken.token_hash == hash_token(raw_token),
        APIToken.is_active == True,
    ).first()

    if not token_record:
        return None

    # Update last_used_at
    token_record.last_used_at = datetime.now(timezone.utc)
    db.commit()

    user = db.query(User).filter(User.id == token_record.user_id).first()
    return user if user and user.is_active else None


# --- Unified Auth Dependency ---

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency: extracts and validates the current user.
    Supports both JWT tokens and API tokens (prefixed with 'drpl_').
    """
    token = credentials.credentials

    # API token path
    if token.startswith("drpl_"):
        user = verify_api_token(token, db)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid or revoked API token")
        return user

    # JWT path
    payload = verify_token(token)
    user_id = payload.get("sub")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency: ensures the current user has admin or master_admin role."""
    if current_user.role not in ("admin", "master_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def require_master_admin(current_user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency: ensures the current user has master_admin role."""
    if current_user.role != "master_admin":
        raise HTTPException(status_code=403, detail="Master admin access required")
    return current_user
