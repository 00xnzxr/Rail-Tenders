"""Every headline tender count means the same thing: the LIVE population.

LIVE = not archived AND not expired AND not below threshold.

Before this, /tenders/stats and admin_dashboard hand-rolled their own queries
while /tenders/view-counts and the list went through _apply_tender_filters, so
the same idea was reported three different ways (production showed 1,649 vs 319
for "total tenders"). These tests exist to stop that drifting apart again.
"""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.core.auth import get_current_user
from app.main import app
from app.models.tender import Tender
from app.models import platform_setting as _ps  # noqa: F401
from app.models import cost_breakdown as _cb  # noqa: F401

NOW = datetime.now(timezone.utc)
_n = 0


def _session():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


class _FakeUser:
    """The admin overview route reads `.role`, so a bare object() won't do."""
    id = 1
    role = "master_admin"
    email = "test@example.com"
    is_active = True


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    return TestClient(app)


def _mk(db, *, closing_days=None, score=None, **kw):
    global _n
    _n += 1
    t = Tender(portal="ireps", tender_id=f"UC{_n}", title=f"unified {_n}",
               ai_relevance_score=score, status="open",
               closing_date=None if closing_days is None else NOW + timedelta(days=closing_days),
               **kw)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_population(db):
    """One of each kind. Exactly ONE is live."""
    live = _mk(db, closing_days=5, score=0.9)
    _mk(db, closing_days=-5, score=0.9)                        # expired
    _mk(db, closing_days=5, score=0.9, is_archived=True)       # archived
    _mk(db, closing_days=5, score=0.9, below_threshold=True)   # below threshold
    return live


def test_stats_total_counts_only_live_tenders():
    db = _session()
    client = _client(db)
    try:
        _make_population(db)
        body = client.get("/api/tenders/stats").json()
        assert body["total_tenders"] == 1, "expired/archived/low-value must not inflate the total"
        assert body["promising_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_stats_view_counts_and_list_all_agree():
    """The headline number must be identical on all three surfaces."""
    db = _session()
    client = _client(db)
    try:
        _make_population(db)

        stats_total = client.get("/api/tenders/stats").json()["total_tenders"]
        view_all = client.get("/api/tenders/view-counts").json()["all"]
        list_total = client.get("/api/tenders/").json()["total"]

        assert stats_total == view_all == list_total == 1, (
            f"surfaces disagree: stats={stats_total} view={view_all} list={list_total}")
    finally:
        app.dependency_overrides.clear()


def test_admin_overview_agrees_with_stats():
    db = _session()
    client = _client(db)
    try:
        _make_population(db)
        stats_total = client.get("/api/tenders/stats").json()["total_tenders"]
        overview = client.get("/api/admin/dashboard/overview").json()
        assert overview["tenders"]["total"] == stats_total
    finally:
        app.dependency_overrides.clear()


def test_good_pile_has_one_definition():
    """`to_bid_count` (dashboard) and the Pursue pile must use the same rule.

    Production showed 271 vs 579 because one read `segment` and the other read
    `ai_relevance_score`. `segment` is only set after an auto-scorer config
    pass, so it silently undercounts.
    """
    db = _session()
    client = _client(db)
    try:
        # High score but NO segment set — the common case in production.
        _mk(db, closing_days=5, score=0.9)

        stats = client.get("/api/tenders/stats").json()
        view = client.get("/api/tenders/view-counts").json()
        assert stats["to_bid_count"] == view["to_bid"] == 1
        assert stats["promising_count"] == stats["to_bid_count"]
    finally:
        app.dependency_overrides.clear()


def test_scored_uses_relevance_score_not_ai_category():
    """`analyzed_tenders` read `ai_category`, populated on 2 of 2,397 prod rows."""
    db = _session()
    client = _client(db)
    try:
        _mk(db, closing_days=5, score=0.55)   # scored, no ai_category
        _mk(db, closing_days=5, score=None)   # genuinely unscored

        body = client.get("/api/tenders/stats").json()
        assert body["scored_tenders"] == 1
        assert body["unscored_tenders"] == 1
    finally:
        app.dependency_overrides.clear()


def test_bifurcation_adds_up_to_every_row():
    """live + expired + archived + below-threshold must reconcile to the total.

    If these ever stop summing, a tender has become invisible on every surface.
    """
    db = _session()
    client = _client(db)
    try:
        _make_population(db)
        b = client.get("/api/tenders/stats").json()["bifurcation"]

        assert b["live"] == 1
        assert b["expired"] == 1
        assert b["archived"] == 1
        assert b["below_threshold"] == 1
        assert b["live"] + b["expired"] + b["archived"] + b["below_threshold"] == b["all_rows"] == 4
    finally:
        app.dependency_overrides.clear()


def test_bifurcation_buckets_do_not_double_count():
    """An archived AND expired tender must land in exactly one bucket."""
    db = _session()
    client = _client(db)
    try:
        _mk(db, closing_days=-5, is_archived=True)    # both archived and expired
        b = client.get("/api/tenders/stats").json()["bifurcation"]
        assert b["archived"] == 1
        assert b["expired"] == 0, "archived wins — a row must not be counted twice"
        assert b["live"] + b["expired"] + b["archived"] + b["below_threshold"] == b["all_rows"] == 1
    finally:
        app.dependency_overrides.clear()


def test_scored_count_is_not_total_minus_unscored():
    """`unscored=True` forces below-threshold rows back in, so subtracting it
    from `total` spans two populations and under-reports scored.
    """
    db = _session()
    client = _client(db)
    try:
        _mk(db, closing_days=5, score=0.9)                          # scored, live
        _mk(db, closing_days=5, score=None)                         # unscored, live
        _mk(db, closing_days=5, score=None, below_threshold=True)   # unscored, low value

        body = client.get("/api/tenders/stats").json()
        assert body["total_tenders"] == 2       # the low-value row is out
        assert body["scored_tenders"] == 1, "must count scored directly, not by subtraction"
    finally:
        app.dependency_overrides.clear()


def test_new_unscored_pile_matches_its_list():
    """Every pile must equal the list behind it, not just the "all" pile.

    `list_tenders` forces include_below_threshold when unscored=True (an
    unscored row has no threshold to be below), but view-counts did not — so
    the New tile undercounted the rows the New view actually showed.
    """
    db = _session()
    client = _client(db)
    try:
        _mk(db, closing_days=5, score=None)                        # unscored
        _mk(db, closing_days=5, score=None, below_threshold=True)  # unscored, low value

        pile = client.get("/api/tenders/view-counts").json()["new_unscored"]
        listed = client.get("/api/tenders/?unscored=true").json()["total"]
        assert pile == listed == 2, f"New pile={pile} but its list={listed}"
    finally:
        app.dependency_overrides.clear()


def test_counts_agree_when_is_archived_is_null():
    """A legacy NULL is_archived must not make the bifurcation disagree.

    `_tender_bifurcation` used `.isnot(True)` (NULL counts as not-archived)
    while every other count used `== False` (NULL dropped). If the column ever
    holds NULL, the same payload would report two different totals.
    """
    from sqlalchemy import text
    db = _session()
    client = _client(db)
    try:
        t = _mk(db, closing_days=5, score=0.9)
        db.execute(text("UPDATE tenders SET is_archived = NULL WHERE id = :i"), {"i": t.id})
        db.commit()
        assert db.execute(text("SELECT is_archived FROM tenders WHERE id = :i"),
                          {"i": t.id}).scalar() is None, "precondition: really NULL"

        body = client.get("/api/tenders/stats").json()
        assert body["total_tenders"] == body["bifurcation"]["live"], (
            f"total={body['total_tenders']} but bifurcation.live="
            f"{body['bifurcation']['live']} — NULL handling differs")
        b = body["bifurcation"]
        assert b["live"] + b["expired"] + b["archived"] + b["below_threshold"] == b["all_rows"]
    finally:
        app.dependency_overrides.clear()


def test_undated_tender_counts_as_live():
    """No closing date means UNKNOWN, not expired — it stays actionable."""
    db = _session()
    client = _client(db)
    try:
        _mk(db, closing_days=None, score=0.9)
        body = client.get("/api/tenders/stats").json()
        assert body["total_tenders"] == 1
        assert body["bifurcation"]["live"] == 1
        assert body["bifurcation"]["expired"] == 0
    finally:
        app.dependency_overrides.clear()
