"""Budget endpoints: what a user has left, and the master admin's controls.

`GET /usage/me` is the only one a normal user can reach, and it reports only
their own figures — a user cannot enumerate anyone else's spend.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_master_admin
from app.core.config import get_settings
from app.core.database import get_db
from app.models.api_usage import APIUsageLog
from app.models.user import User
from app.models.user_budget import UserBudget
from app.services import budget_service as bs
from app.services.audit_service import log_action

router = APIRouter(prefix="/usage", tags=["usage"])
admin_router = APIRouter(prefix="/admin/usage", tags=["admin", "usage"])


def _as_payload(user: User, usage: bs.Usage) -> dict:
    return {
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "period_start": usage.period_start.isoformat(),
        "spend_usd": usage.spend_usd,
        "limit_usd": usage.limit_usd,
        "percent_used": usage.percent_used,
        "status": usage.status,
        "override_active": usage.override_active,
        # master_admin is metered but never blocked, so the bar must not tell
        # an owner they are about to be cut off.
        "enforced": (user.role or "") not in bs.UNBLOCKABLE_ROLES
        and get_settings().budget_enforcement_enabled,
    }


@router.get("/me")
def my_usage(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """This user's own budget standing. Feeds the usage bar in the app shell."""
    return _as_payload(current_user, bs.get_usage(db, current_user))


@admin_router.get("")
def all_usage(
    db: Session = Depends(get_db),
    _: User = Depends(require_master_admin),
):
    """Per-user rollup for the current period, biggest spender first."""
    users = db.query(User).filter(User.is_active == True).all()  # noqa: E712
    rows = [_as_payload(u, bs.get_usage(db, u)) for u in users]
    rows.sort(key=lambda r: r["spend_usd"], reverse=True)

    unattributed = (
        db.query(func.sum(APIUsageLog.cost_estimate))
        .filter(
            APIUsageLog.user_id.is_(None),
            APIUsageLog.created_at >= bs.period_start(),
        )
        .scalar()
    ) or 0.0

    return {
        "period_start": bs.period_start().isoformat(),
        "users": rows,
        "total_spend_usd": round(sum(r["spend_usd"] for r in rows), 4),
        # Platform work with no human behind it — seeders, the archive sweep,
        # scheduled scoring. Shown separately so it is never mistaken for a
        # user's spend or silently lost from the total.
        "unattributed_spend_usd": round(float(unattributed), 4),
    }


@admin_router.get("/runs")
def recent_runs(
    limit: int = 50,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_master_admin),
):
    """Recent agent runs and what each one cost, newest first.

    One row per run rather than per LLM call — a single run is the Master plus
    every worker it delegated to plus every retry, and the per-call view buries
    that. Master-admin only: the rows name other users' runs.
    """
    from app.services.run_cost_service import run_costs_for

    runs = run_costs_for(db, user_id=user_id, limit=max(1, min(limit, 500)))
    emails = {
        u.id: u.email
        for u in db.query(User).filter(
            User.id.in_({r.user_id for r in runs if r.user_id})
        ).all()
    } if any(r.user_id for r in runs) else {}

    return {
        "runs": [
            {
                "run_id": r.run_id,
                "calls": r.calls,
                "tokens_input": r.tokens_input,
                "tokens_output": r.tokens_output,
                "tokens_total": r.tokens_total,
                "cost_usd": round(r.cost_usd, 6),
                "user_id": r.user_id,
                "email": emails.get(r.user_id),
                "agent_name": r.agent_name,
                "last_call_at": r.last_call_at.isoformat() if r.last_call_at else None,
            }
            for r in runs
        ],
    }


class OverrideBody(BaseModel):
    days: int = Field(default=7, ge=1, le=90)
    note: Optional[str] = None


class LimitBody(BaseModel):
    monthly_limit_usd: Optional[float] = Field(default=None, ge=0)


def _budget_row(db: Session, user_id: int) -> UserBudget:
    row = db.query(UserBudget).filter(UserBudget.user_id == user_id).first()
    if row is None:
        row = UserBudget(user_id=user_id)
        db.add(row)
    return row


@admin_router.post("/{user_id}/override")
def grant_override(
    user_id: int,
    body: OverrideBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Release a blocked user. There is no self-serve path to this."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    row = _budget_row(db, user_id)
    row.override_until = datetime.now(timezone.utc) + timedelta(days=body.days)
    row.override_granted_by = admin.id
    row.override_note = body.note
    db.commit()

    log_action(
        db, admin.id, admin.email, "budget.override_granted",
        resource_type="user", resource_id=user_id,
        details={"days": body.days, "note": body.note},
    )
    return _as_payload(user, bs.get_usage(db, user))


@admin_router.delete("/{user_id}/override")
def revoke_override(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    row = _budget_row(db, user_id)
    row.override_until = None
    db.commit()
    log_action(
        db, admin.id, admin.email, "budget.override_revoked",
        resource_type="user", resource_id=user_id,
    )
    return _as_payload(user, bs.get_usage(db, user))


@admin_router.put("/{user_id}/limit")
def set_limit(
    user_id: int,
    body: LimitBody,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Set one user's monthly limit. Null restores the platform default."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    row = _budget_row(db, user_id)
    row.monthly_limit_usd = body.monthly_limit_usd
    db.commit()
    log_action(
        db, admin.id, admin.email, "budget.limit_changed",
        resource_type="user", resource_id=user_id,
        details={"monthly_limit_usd": body.monthly_limit_usd},
    )
    return _as_payload(user, bs.get_usage(db, user))
