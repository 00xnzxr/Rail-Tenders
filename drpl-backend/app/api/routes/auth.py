"""
DRPL Backend - Auth Routes
Login, token management, and user profile
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from app.core.database import get_db
from app.core.auth import create_access_token, get_current_user, create_api_token
from app.services.audit_service import log_action
from app.models.user import User
from app.models.api_token import APIToken
from app.schemas import (
    TokenRequest, TokenResponse, UserProfileResponse,
    CreateAPITokenRequest, CreateAPITokenResponse, APITokenInfo,
)

router = APIRouter(prefix="/auth", tags=["auth"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@router.post("/token", response_model=TokenResponse)
def login(request: TokenRequest, db: Session = Depends(get_db)):
    """Authenticate user and return JWT token for extension linking."""
    user = db.query(User).filter(User.email == request.email).first()

    if not user or not pwd_context.verify(request.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    # Track last login
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    log_action(db, user.id, user.email, "user.login", "user", str(user.id))

    token = create_access_token(user.id, user.email)

    return TokenResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
    )


@router.get("/me", response_model=UserProfileResponse)
def get_profile(current_user: User = Depends(get_current_user)):
    """Get current user profile."""
    return current_user


# --- API Token Management ---

@router.post("/api-tokens", response_model=CreateAPITokenResponse)
def generate_api_token(
    request: CreateAPITokenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a new API token for extension linking. Token is shown ONCE."""
    # Limit to 5 active tokens per user
    active_count = db.query(APIToken).filter(
        APIToken.user_id == current_user.id,
        APIToken.is_active == True,
    ).count()

    if active_count >= 5:
        raise HTTPException(
            status_code=400,
            detail="Maximum 5 active API tokens allowed. Revoke an existing token first.",
        )

    raw_token, token_obj = create_api_token(db, current_user.id, request.name)

    return CreateAPITokenResponse(
        id=token_obj.id,
        name=token_obj.name,
        token=raw_token,  # shown ONCE
        prefix=token_obj.prefix,
        created_at=token_obj.created_at,
    )


@router.get("/api-tokens", response_model=list[APITokenInfo])
def list_api_tokens(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all active API tokens for the current user."""
    tokens = db.query(APIToken).filter(
        APIToken.user_id == current_user.id,
        APIToken.is_active == True,
    ).order_by(APIToken.created_at.desc()).all()
    return tokens


@router.delete("/api-tokens/{token_id}")
def revoke_api_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revoke an API token (soft-delete)."""
    token = db.query(APIToken).filter(
        APIToken.id == token_id,
        APIToken.user_id == current_user.id,
    ).first()

    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    token.is_active = False
    db.commit()

    return {"success": True, "message": "Token revoked"}
