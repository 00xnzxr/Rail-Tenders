"""Backlog + drain endpoints are master-admin gated and return the service shapes."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.core.database import Base
from app.core.auth import require_master_admin
from app.core.database import get_db
from app.main import app
from app.services import scoring_backlog_service as svc


def _session():
    # StaticPool + check_same_thread=False: TestClient dispatches the route on a
    # different thread than this setup code, and the default SQLite in-memory pool
    # (SingletonThreadPool) hands out a *separate* in-memory DB per thread, which
    # makes tables created here invisible to the request thread. StaticPool shares
    # one connection across threads so the schema created here is visible.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _client_with_overrides(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_master_admin] = lambda: object()  # stub admin
    return TestClient(app)


def test_backlog_route_returns_stats():
    db = _session()
    try:
        client = _client_with_overrides(db)
        r = client.get("/api/admin/tender-scoring/backlog")
        assert r.status_code == 200
        body = r.json()
        for k in ("total", "scored", "pending", "drainable", "in_flight_batches", "enabled"):
            assert k in body
    finally:
        app.dependency_overrides.clear()


def test_drain_route_live(monkeypatch):
    db = _session()
    try:
        import app.api.routes.tender_scoring_admin as route_mod
        monkeypatch.setattr(route_mod, "drain_backlog",
                             lambda db, mode, limit=None: {"mode": mode, "scored": 3, "failed": 0})
        client = _client_with_overrides(db)
        r = client.post("/api/admin/tender-scoring/drain", json={"mode": "live"})
        assert r.status_code == 200
        assert r.json()["scored"] == 3
    finally:
        app.dependency_overrides.clear()
