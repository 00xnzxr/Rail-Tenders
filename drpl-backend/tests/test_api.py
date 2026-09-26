"""
DRPL Backend - Basic Tests
Run with: pytest tests/
"""

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "DRPL Tender Intelligence Platform"


def test_extension_config_requires_auth():
    """Extension config endpoint should require JWT auth."""
    response = client.get("/api/extension/config")
    assert response.status_code == 401  # No token provided


def test_tender_upload_requires_auth():
    """Tender upload should require JWT auth."""
    response = client.post("/api/extension/tenders", json={"tenders": []})
    assert response.status_code == 401
