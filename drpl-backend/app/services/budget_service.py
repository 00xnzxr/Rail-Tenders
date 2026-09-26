"""The $30/month cap: what a user has spent, and whether they may start a run.

Spend is **derived**, always: `SUM(cost_estimate)` over `APIUsageLog` since the
start of the calendar month. There is no stored counter, so the number a user
is blocked on cannot drift away from the rows an admin can audit.

Enforcement is checked **once at run start**, never per LLM call. Killing a run
mid-flight would throw away everything it had already spent and leave exactly
the half-finished state the Master Agent's own time budget was fixed to avoid.
The cost is bounded overshoot — a user can finish a period slightly over their
limit by the price of the one run that crossed it — which is accepted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import NamedTuple, Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.api_usage import APIUsageLog
from app.models.user import User
from app.models.user_budget import UserBudget

logger = logging.getLogger(__name__)

#: Roles that are metered but never blocked. An owner must not be able to lock
#: themselves out of their own platform.
UNBLOCKABLE_ROLES = frozenset({"master_admin"})


class Usage(NamedTuple):
    period_start: datetime
    spend_usd: float
    limit_usd: float
    percent_used: float
    status: str  # "ok" | "warning" | "exceeded"
    override_active: bool


def period_start(now: Optional[datetime] = None) -> datetime:
    """00:00 UTC on the 1st of the current calendar month."""
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _budget_row(db: Session, user_id: int) -> Optional[UserBudget]:
    return db.query(UserBudget).filter(UserBudget.user_id == user_id).first()


def _override_is_live(row: Optional[UserBudget]) -> bool:
    if row is None or row.override_until is None:
        return False
    until = row.override_until
    if until.tzinfo is None:  # SQLite hands back naive datetimes
        until = until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def get_usage(db: Session, user: User) -> Usage:
    """This user's spend and standing for the current period."""
    settings = get_settings()
    start = period_start()

    spend = (
        db.query(func.sum(APIUsageLog.cost_estimate))
        .filter(APIUsageLog.user_id == user.id, APIUsageLog.created_at >= start)
        .scalar()
    ) or 0.0

    row = _budget_row(db, user.id)
    limit = (
        row.monthly_limit_usd
        if row is not None and row.monthly_limit_usd is not None
        else settings.default_monthly_budget_usd
    )

    ratio = (spend / limit) if limit > 0 else 1.0
    if ratio >= 1.0:
        status = "exceeded"
    elif ratio >= settings.budget_warning_threshold:
        status = "warning"
    else:
        status = "ok"

    return Usage(
        period_start=start,
        spend_usd=round(float(spend), 6),
        limit_usd=float(limit),
        # Capped for display: overshoot is real and reported in spend_usd, but a
        # progress bar past 100% renders as nonsense.
        percent_used=round(min(ratio, 1.0) * 100, 2),
        status=status,
        override_active=_override_is_live(row),
    )


def assert_within_budget(db: Session, user: User) -> None:
    """Raise 402 if this user may not start new agent work.

    Called at run entry points only. Reads, navigation, and output the user has
    already produced stay available when blocked — only new spend stops.
    """
    settings = get_settings()
    if not settings.budget_enforcement_enabled:
        return
    if (user.role or "") in UNBLOCKABLE_ROLES:
        return

    usage = get_usage(db, user)
    if usage.status != "exceeded" or usage.override_active:
        return

    logger.info(
        "budget.blocked user_id=%s spend=%.4f limit=%.2f",
        user.id, usage.spend_usd, usage.limit_usd,
    )
    raise HTTPException(
        status_code=402,
        detail=(
            f"Monthly AI budget of ${usage.limit_usd:.2f} is exhausted "
            f"(${usage.spend_usd:.2f} used). Contact your administrator to "
            f"continue this month."
        ),
    )
