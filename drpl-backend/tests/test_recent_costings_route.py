"""GET /api/cost-breakdowns/recent returns newest breakdowns joined to tender title."""
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base, get_db
from app.core.auth import get_current_user
from app.main import app
from app.models.tender import Tender
from app.models.cost_breakdown import CostBreakdown


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


def test_recent_costings_newest_first_with_title():
    db = _session()
    try:
        db.add(Tender(id=1, portal="ireps", tender_id="A", title="Alpha tender"))
        db.add(Tender(id=2, portal="ireps", tender_id="B", title="Beta tender"))
        db.commit()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.add(CostBreakdown(tender_id=1, version=1, grand_total=100.0,
                             margin_pct=12.5, status="draft", updated_at=base))
        db.add(CostBreakdown(tender_id=2, version=1, grand_total=200.0,
                             margin_pct=20.0, status="finalized",
                             updated_at=base + timedelta(days=5)))
        db.commit()

        r = _client(db).get("/api/cost-breakdowns/recent?limit=5")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 2
        assert rows[0]["tender_id"] == 2                 # newest updated_at first
        assert rows[0]["title"] == "Beta tender"
        assert rows[0]["grand_total"] == 200.0
        assert rows[0]["status"] == "finalized"
        assert rows[1]["title"] == "Alpha tender"
    finally:
        app.dependency_overrides.clear()
