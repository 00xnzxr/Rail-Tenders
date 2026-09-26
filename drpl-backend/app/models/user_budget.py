"""Per-user monthly AI budget.

Deliberately holds only the *policy* — the limit and any admin release. Actual
spend is never stored here: it is summed from `APIUsageLog` for the current
period, so the figure a user is blocked on is always derived from the same
ledger an admin can audit, and can never drift from it.

A user with no row uses `settings.default_monthly_budget_usd`, so the common
case needs no row at all.
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, Text

from app.core.database import Base


class UserBudget(Base):
    __tablename__ = "user_budgets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, unique=True, index=True)  # FK to users.id

    # Null means "use the platform default" rather than "zero budget" — a row
    # created only to grant an override must not silently cap the user at 0.
    monthly_limit_usd = Column(Float, nullable=True)

    # A master admin's release for a user who hit the wall. Blocking is absolute
    # otherwise: there is no self-serve override.
    override_until = Column(DateTime(timezone=True), nullable=True)
    override_granted_by = Column(Integer, nullable=True)  # FK to users.id
    override_note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
