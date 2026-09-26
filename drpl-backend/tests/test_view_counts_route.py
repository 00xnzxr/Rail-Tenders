"""GET /api/tenders/view-counts returns the score-tiered funnel counts.

The funnel piles are keyed off the AI relevance score (matching the card
colours), not the stored `segment`: Pursue = score >= 0.7, Review = 0.4-0.7,
Set aside (discarded) = score < 0.4, New = unscored, All = everything
(including below-threshold). See src/lib/tenderViews.ts.
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


def _session():
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _client(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


_n = 0


def _mk(db, score=None, status="open", segment=None, closing_in_days=None, below=False):
    global _n
    _n += 1
    closing = datetime.now(timezone.utc) + timedelta(days=closing_in_days) if closing_in_days is not None else None
    t = Tender(portal="ireps", tender_id=f"V{_n}", title=f"v{_n}",
               ai_relevance_score=score, status=status, segment=segment,
               closing_date=closing, below_threshold=below)
    db.add(t); db.commit()
    return t


def test_view_counts():
    db = _session()
    try:
        _mk(db, score=0.9, status="open", closing_in_days=3)    # Pursue (>= 0.7)
        _mk(db, score=0.8, status="open", closing_in_days=40)   # Pursue (>= 0.7)
        _mk(db, score=0.5)                                       # Review (0.4-0.7)
        _mk(db, score=0.2)                                       # Set aside (< 0.4)
        _mk(db, score=None)                                      # New (unscored)
        _mk(db, score=None, below=True)                         # New (unscored, below-threshold)

        r = _client(db).get("/api/tenders/view-counts")
        assert r.status_code == 200
        body = r.json()
        assert body["to_bid"] == 2                # score >= 0.7
        assert body["worth_a_look"] == 1          # 0.4 <= score < 0.7
        assert body["discarded"] == 1             # score < 0.4
        # The piles now count the LIVE population by default — below-threshold
        # rows are excluded, matching the Tenders list and /tenders/stats...
        assert body["all"] == 5
        # ...except for New: an unscored row has no threshold to be below, so
        # the gate must not hide it. This matches what the New view's LIST
        # returns, which is the whole point — a pile must equal its list.
        assert body["new_unscored"] == 2

        # ...and the flag still widens the window when a caller asks for it.
        wide = _client(db).get("/api/tenders/view-counts?include_below_threshold=true").json()
        assert wide["new_unscored"] == 2
        assert wide["all"] == 6
        assert "closing_soon" not in body         # dropped as a pile; it's a filter now
    finally:
        app.dependency_overrides.clear()
