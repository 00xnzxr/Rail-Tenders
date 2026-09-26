"""Dashboard stats expose meaningful counts (promising / to_bid / closing_soon / costing)."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.core.auth import get_current_user
from app.main import app
from app.models.tender import Tender

# Register models the stats path touches so create_all builds their tables.
from app.models import platform_setting as _ps  # noqa: F401
from app.models import cost_breakdown as _cb  # noqa: F401


def _session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


_n = 0


def _mk(db, score=None, status="open", segment=None, closing_in_days=None):
    global _n
    _n += 1
    closing = None
    if closing_in_days is not None:
        closing = datetime.now(timezone.utc) + timedelta(days=closing_in_days)
    t = Tender(portal="ireps", tender_id=f"T{_n}", title=f"t{_n}",
               ai_relevance_score=score, status=status, segment=segment,
               closing_date=closing)
    db.add(t)
    db.commit()
    return t


def test_stats_meaningful_counts():
    db = _session()
    try:
        _mk(db, score=0.80, status="open", segment="to_bid", closing_in_days=3)   # promising, open, to_bid, closing soon
        _mk(db, score=0.72, status="open", closing_in_days=30)                     # promising, open, not closing soon
        _mk(db, score=0.69, status="open", closing_in_days=2)                      # NOT promising (below 0.70)
        _mk(db, score=0.95, status="closed", closing_in_days=1)                    # promising but closed
        from app.models.cost_breakdown import CostBreakdown
        db.add(CostBreakdown(tender_id=1, version=1))
        db.commit()

        r = _client(db).get("/api/tenders/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["promising_count"] == 3          # 0.80, 0.72, 0.95 (0.69 excluded)
        # to_bid_count IS promising_count — one definition of "the good pile",
        # the AI-score tier the funnel uses. It used to read `segment`, which is
        # only written after an auto-scorer config pass (271 vs 579 in prod).
        assert body["to_bid_count"] == 3
        assert body["closing_soon_count"] == 1        # only the 0.80 open+soon one (0.69 excluded, 0.95 closed)
        assert body["promising_open_count"] == 2      # 0.80, 0.72
        assert body["with_costing_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_stats_exclude_archived_tenders():
    db = _session()
    try:
        _mk(db, score=0.9, status="open", segment="to_bid")
        archived = _mk(db, score=0.9, status="open", segment="to_bid")
        archived.is_archived = True
        archived.archive_reason = "past_due"
        db.commit()

        body = _client(db).get("/api/tenders/stats").json()
        assert body["total_tenders"] == 1
        assert body["open_tenders"] == 1
        assert body["promising_count"] == 1
        assert body["to_bid_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_with_costing_count_excludes_archived_tender():
    """An archived tender with a CostBreakdown must not count toward with_costing_count.

    This exercises the JOIN specifically (not just a WHERE on Tender), since
    CostBreakdown itself has no archive column — the exclusion only works if
    with_costing_count joins back to Tender and filters on is_archived there.
    """
    db = _session()
    try:
        active = _mk(db, score=0.5, status="open")
        archived = _mk(db, score=0.5, status="open")
        archived.is_archived = True
        db.commit()

        from app.models.cost_breakdown import CostBreakdown
        db.add(CostBreakdown(tender_id=active.id, version=1))
        db.add(CostBreakdown(tender_id=archived.id, version=1))
        db.commit()

        body = _client(db).get("/api/tenders/stats").json()
        assert body["with_costing_count"] == 1
    finally:
        app.dependency_overrides.clear()
