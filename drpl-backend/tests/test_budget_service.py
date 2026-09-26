"""Per-user monthly budget: the $30 cap, its status bands, and enforcement.

Spend is derived from the APIUsageLog ledger, never stored as a counter that
can drift from the rows it summarises. Enforcement happens once at run start —
never mid-run, which would throw away everything already spent.
"""

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models.api_usage import APIUsageLog
from app.models.user import User
from app.models.user_budget import UserBudget
from app.services import budget_service as bs


# `User.email` is UNIQUE and `datetime.now().timestamp()` does not tick once
# per call — on Windows it is flat across thousands of calls — so two users
# created in the same tick collided and the suite failed with an
# IntegrityError that moved between tests depending on run order.
_EMAILS = itertools.count()


def _user(db, role="costing_research", email=None):
    u = User(
        email=email or f"u{next(_EMAILS)}@t.co",
        name="Test",
        hashed_password="x",
        role=role,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _spend(db, user_id, usd, when=None):
    db.add(
        APIUsageLog(
            provider="anthropic",
            model="claude-opus-5",
            agent_name="decision_maker",
            user_id=user_id,
            cost_estimate=usd,
            created_at=when or datetime.now(timezone.utc),
        )
    )
    db.commit()


# ------------------------------------------------------------------ period --


def test_period_start_is_the_first_of_the_month_utc():
    start = bs.period_start(datetime(2026, 9, 17, 13, 45, tzinfo=timezone.utc))
    assert start == datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def test_spend_excludes_the_previous_period(db):
    u = _user(db)
    now = datetime.now(timezone.utc)
    _spend(db, u.id, 5.0, when=bs.period_start(now) - timedelta(days=1))
    _spend(db, u.id, 2.0)
    assert bs.get_usage(db, u).spend_usd == pytest.approx(2.0)


def test_spend_is_per_user(db):
    a, b = _user(db), _user(db)
    _spend(db, a.id, 3.0)
    _spend(db, b.id, 7.0)
    assert bs.get_usage(db, a).spend_usd == pytest.approx(3.0)
    assert bs.get_usage(db, b).spend_usd == pytest.approx(7.0)


# ------------------------------------------------------------------ limits --


def test_default_limit_applies_with_no_budget_row(db):
    u = _user(db)
    assert bs.get_usage(db, u).limit_usd == pytest.approx(30.0)


def test_per_user_limit_overrides_the_default(db):
    u = _user(db)
    db.add(UserBudget(user_id=u.id, monthly_limit_usd=100.0))
    db.commit()
    assert bs.get_usage(db, u).limit_usd == pytest.approx(100.0)


# ----------------------------------------------------------------- statuses --


@pytest.mark.parametrize(
    "spend,expected",
    [(0.0, "ok"), (23.9, "ok"), (24.0, "warning"), (29.9, "warning"),
     (30.0, "exceeded"), (45.0, "exceeded")],
)
def test_status_bands(db, spend, expected):
    u = _user(db)
    _spend(db, u.id, spend)
    assert bs.get_usage(db, u).status == expected


def test_percent_used_is_reported_for_the_bar(db):
    u = _user(db)
    _spend(db, u.id, 15.0)
    assert bs.get_usage(db, u).percent_used == pytest.approx(50.0)


def test_percent_used_is_capped_at_100_for_display(db):
    """Overshoot is real, but a progress bar past 100% renders as nonsense."""
    u = _user(db)
    _spend(db, u.id, 60.0)
    usage = bs.get_usage(db, u)
    assert usage.percent_used == 100.0
    assert usage.spend_usd == pytest.approx(60.0)


# -------------------------------------------------------------- enforcement --


def test_under_budget_passes(db):
    u = _user(db)
    _spend(db, u.id, 5.0)
    bs.assert_within_budget(db, u)  # must not raise


def test_exceeded_is_blocked_with_402(db):
    u = _user(db)
    _spend(db, u.id, 31.0)
    with pytest.raises(HTTPException) as e:
        bs.assert_within_budget(db, u)
    assert e.value.status_code == 402
    assert "administrator" in str(e.value.detail).lower()


def test_master_admin_is_metered_but_never_blocked(db):
    """An owner must not be able to lock themselves out of their platform."""
    u = _user(db, role="master_admin")
    _spend(db, u.id, 500.0)
    assert bs.get_usage(db, u).spend_usd == pytest.approx(500.0)
    bs.assert_within_budget(db, u)  # must not raise


def test_a_live_admin_override_unblocks(db):
    u = _user(db)
    _spend(db, u.id, 45.0)
    db.add(UserBudget(
        user_id=u.id,
        override_until=datetime.now(timezone.utc) + timedelta(days=2),
        override_granted_by=1,
    ))
    db.commit()
    bs.assert_within_budget(db, u)
    assert bs.get_usage(db, u).override_active is True


def test_an_expired_override_does_not_unblock(db):
    u = _user(db)
    _spend(db, u.id, 45.0)
    db.add(UserBudget(
        user_id=u.id,
        override_until=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    db.commit()
    with pytest.raises(HTTPException):
        bs.assert_within_budget(db, u)
    assert bs.get_usage(db, u).override_active is False


def test_enforcement_can_be_switched_off_without_a_deploy(db, monkeypatch):
    """Same escape hatch as the confirm gate — a setting, not a redeploy."""
    u = _user(db)
    _spend(db, u.id, 99.0)
    monkeypatch.setattr(bs.get_settings(), "budget_enforcement_enabled", False)
    bs.assert_within_budget(db, u)  # must not raise
