from fastapi.testclient import TestClient


def test_segment_override_route_shape():
    # Route exists and requires auth (403 without token), proving it's mounted.
    from app.main import app
    client = TestClient(app)
    r = client.post("/api/tenders/1/segment", json={"segment": "to_bid"})
    assert r.status_code in (401, 403)
