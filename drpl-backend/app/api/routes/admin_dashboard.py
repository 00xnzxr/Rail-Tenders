"""
DRPL Backend - Admin Dashboard Routes
System overview, health, and usage stats (master_admin only)
"""

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.core.config import get_settings
from app.models.user import User
from app.models.tender import Tender
from app.models.proposal import ProposalSession
from app.models.audit_log import AuditLog
from app.models.api_usage import APIUsageLog

router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])
settings = get_settings()


def _get_dir_size(path: str) -> int:
    """Get total size of a directory in bytes."""
    total = 0
    if os.path.exists(path):
        for dirpath, _dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
    return total


@router.get("/overview")
def get_system_overview(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """System overview statistics."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    total_users = db.query(User).count()
    active_users = db.query(User).filter(User.last_login_at >= week_ago).count()

    users_by_role = {}
    for role, count in db.query(User.role, func.count(User.id)).group_by(User.role).all():
        users_by_role[role] = count

    # Same LIVE population as /tenders/stats and the Tenders list — not archived,
    # not expired, not below threshold. Routed through the shared filter so this
    # cannot drift away from the other surfaces again.
    from app.services.tender_service import count_tenders
    total_tenders = count_tenders(db, exclude_expired=True)
    open_tenders = count_tenders(db, exclude_expired=True, status="open")

    total_proposals = db.query(ProposalSession).count()
    proposals_by_status = {}
    for status, count in db.query(ProposalSession.status, func.count(ProposalSession.id)).group_by(ProposalSession.status).all():
        proposals_by_status[status] = count

    storage_bytes = _get_dir_size(settings.upload_dir)
    storage_mb = round(storage_bytes / (1024 * 1024), 1)

    return {
        "users": {"total": total_users, "active_7d": active_users, "by_role": users_by_role},
        "tenders": {"total": total_tenders, "open": open_tenders},
        "proposals": {"total": total_proposals, "by_status": proposals_by_status},
        "storage_mb": storage_mb,
    }


@router.get("/api-usage")
def get_api_usage(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """AI API usage statistics over time."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    total_calls = db.query(APIUsageLog).filter(APIUsageLog.created_at >= since).count()
    total_tokens_in = db.query(func.sum(APIUsageLog.tokens_input)).filter(APIUsageLog.created_at >= since).scalar() or 0
    total_tokens_out = db.query(func.sum(APIUsageLog.tokens_output)).filter(APIUsageLog.created_at >= since).scalar() or 0
    total_cost = db.query(func.sum(APIUsageLog.cost_estimate)).filter(APIUsageLog.created_at >= since).scalar() or 0
    error_count = db.query(APIUsageLog).filter(APIUsageLog.created_at >= since, APIUsageLog.success == False).count()

    return {
        "period_days": days,
        "total_calls": total_calls,
        "total_tokens_input": total_tokens_in,
        "total_tokens_output": total_tokens_out,
        "total_cost_estimate": round(total_cost, 4),
        "error_count": error_count,
        "error_rate": round(error_count / max(total_calls, 1), 4),
    }


@router.get("/health")
def get_system_health(
    admin: User = Depends(require_master_admin),
):
    """System health check."""
    checks = {}

    # Database check
    try:
        from app.core.database import SessionLocal
        db = SessionLocal()
        db.execute("SELECT 1" if hasattr(db, 'execute') else None)
        db.close()
        checks["database"] = {"status": "healthy", "message": "Connected"}
    except Exception as e:
        checks["database"] = {"status": "error", "message": str(e)}

    # Upload directory
    upload_dir = settings.upload_dir
    if os.path.exists(upload_dir) and os.access(upload_dir, os.W_OK):
        checks["storage"] = {"status": "healthy", "message": f"Writable: {upload_dir}"}
    else:
        checks["storage"] = {"status": "warning", "message": f"Not writable: {upload_dir}"}

    # AI API key
    if settings.anthropic_api_key:
        checks["ai_api"] = {"status": "healthy", "message": "Anthropic API key configured"}
    elif settings.openai_api_key:
        checks["ai_api"] = {"status": "healthy", "message": "OpenAI API key configured"}
    else:
        checks["ai_api"] = {"status": "error", "message": "No AI API key configured"}

    overall = "healthy" if all(c["status"] == "healthy" for c in checks.values()) else "degraded"
    return {"overall": overall, "checks": checks}


@router.get("/active-users")
def get_active_users(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List recently active users."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    users = db.query(User).filter(User.last_login_at >= week_ago).order_by(User.last_login_at.desc()).limit(20).all()
    return [{
        "id": u.id, "name": u.name, "email": u.email, "role": u.role,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    } for u in users]
