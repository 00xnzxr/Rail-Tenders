from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_settings_route_requires_auth():
    r = client.get("/api/admin/tender-scoring/settings")
    assert r.status_code in (401, 403)


def test_stats_route_requires_auth():
    r = client.get("/api/admin/tender-scoring/stats")
    assert r.status_code in (401, 403)
