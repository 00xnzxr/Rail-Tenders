"""`exclude_expired` hides past-closing-date tenders from the list and the funnel counts.

This is a VIEW filter only: it never archives, never deletes, and the rows stay
reachable with the flag off. Distinct from the archive sweep, which additionally
requires a grace period, workflow_status='new', no assignee, and no work
artifacts — see app/services/tender_archive_service.py.
"""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tender import Tender
from app.services.tender_service import count_tenders, get_tenders

NOW = datetime.now(timezone.utc)
_n = 0


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _mk(db, *, closing_days_from_now=None, **kw):
    """closing_days_from_now=-5 → closed 5 days ago; None → no closing_date at all."""
    global _n
    _n += 1
    closing = (None if closing_days_from_now is None
               else NOW + timedelta(days=closing_days_from_now))
    t = Tender(portal="ireps", tender_id=f"EXP{_n}", title=f"expired test {_n}",
               closing_date=closing, **kw)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_expired_tender_is_hidden_when_flag_set():
    db = _session()
    live = _mk(db, closing_days_from_now=5)
    expired = _mk(db, closing_days_from_now=-5)

    ids = {t.id for t in get_tenders(db, exclude_expired=True)}
    assert live.id in ids
    assert expired.id not in ids


def test_expired_tender_is_visible_by_default():
    """The flag must be opt-in — default behaviour is unchanged."""
    db = _session()
    expired = _mk(db, closing_days_from_now=-5)

    assert expired.id in {t.id for t in get_tenders(db)}
    assert expired.id in {t.id for t in get_tenders(db, exclude_expired=False)}


def test_null_closing_date_is_not_treated_as_expired():
    """A tender with no closing date is UNKNOWN, not expired.

    `closing_date >= now` alone would drop NULL rows under SQL three-valued
    logic, silently hiding tenders whose date was never scraped.
    """
    db = _session()
    undated = _mk(db, closing_days_from_now=None)

    assert undated.id in {t.id for t in get_tenders(db, exclude_expired=True)}


def test_tender_closing_today_is_still_live():
    """Boundary: closing later today has not expired yet."""
    db = _session()
    today = _mk(db, closing_days_from_now=0.5)   # ~12h from now

    assert today.id in {t.id for t in get_tenders(db, exclude_expired=True)}


def test_count_tenders_honours_exclude_expired():
    """The funnel counts must agree with the list, or the numbers lie."""
    db = _session()
    _mk(db, closing_days_from_now=5)
    _mk(db, closing_days_from_now=-5)
    _mk(db, closing_days_from_now=None)

    assert count_tenders(db) == 3
    assert count_tenders(db, exclude_expired=True) == 2   # live + undated


def test_exclude_expired_composes_with_score_filters():
    """View-count piles pass score bounds too; the filters must stack."""
    db = _session()
    live_hi = _mk(db, closing_days_from_now=5, ai_relevance_score=0.9)
    _mk(db, closing_days_from_now=-5, ai_relevance_score=0.9)

    ids = {t.id for t in get_tenders(db, exclude_expired=True, score_min=0.7)}
    assert ids == {live_hi.id}


def test_exclude_expired_stacks_with_the_archive_filter():
    """The two filters are independent; neither re-admits what the other hides."""
    db = _session()
    archived_live = _mk(db, closing_days_from_now=5, is_archived=True)
    _mk(db, closing_days_from_now=-5, is_archived=True)

    # Archived rows hidden by default, whatever their closing date.
    assert count_tenders(db, exclude_expired=True) == 0
    # Asking for archived rows returns only the live one — expiry still applies.
    ids = {t.id for t in get_tenders(db, exclude_expired=True, include_archived=True)}
    assert ids == {archived_live.id}


# ── Route-level: the API surfaces the flag on both endpoints ────────────────


def _client(db):
    from fastapi.testclient import TestClient
    from app.core.database import get_db
    from app.core.auth import get_current_user
    from app.main import app
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app), app


def test_list_route_accepts_exclude_expired():
    db = _session()
    client, app = _client(db)
    try:
        live = _mk(db, closing_days_from_now=5)
        _mk(db, closing_days_from_now=-5)

        body = client.get("/api/tenders/?exclude_expired=true").json()
        assert body["total"] == 1
        assert [i["id"] for i in body["items"]] == [live.id]

        # The ROUTE now defaults to hiding expired, so the API is honest without
        # relying on the caller. Passing false explicitly widens it back out.
        assert client.get("/api/tenders/").json()["total"] == 1
        assert client.get("/api/tenders/?exclude_expired=false").json()["total"] == 2
    finally:
        app.dependency_overrides.clear()


def test_view_counts_route_honours_exclude_expired():
    """Funnel counts must track the list, or the piles contradict the rows."""
    db = _session()
    client, app = _client(db)
    try:
        _mk(db, closing_days_from_now=5, ai_relevance_score=0.9)
        _mk(db, closing_days_from_now=-5, ai_relevance_score=0.9)

        # Default now hides expired, matching /tenders/stats and the list.
        filtered = client.get("/api/tenders/view-counts").json()
        assert filtered["to_bid"] == 1
        assert filtered["all"] == 1
        # Explicitly asking for expired widens it back out.
        wide = client.get("/api/tenders/view-counts?exclude_expired=false").json()
        assert wide["to_bid"] == 2
    finally:
        app.dependency_overrides.clear()
