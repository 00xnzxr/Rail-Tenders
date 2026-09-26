"""POST /tenders/{id}/cost-breakdown/regenerate-xlsx hands back a link.

This request builds the workbook, holds every byte of it in memory and uploads
it. Without a link in the response, the only way the client can obtain those
bytes is a second request that makes the backend fetch them back out of storage
and re-stream them — the file crosses the network three times to be delivered
once. That second leg was the slow half of the "Download XLSX" button.
"""
import pytest

from app.api.routes import cost_breakdown as route
from app.services import cost_breakdown_service


class _Breakdown:
    id = 7
    tender_id = 42
    cost_sheet_template = None


_INFO = {
    "artifact_id": 11,
    "artifact_type": "cost_breakdown_xlsx",
    "title": "Cost Breakdown",
    "version": 1,
    "file_name": "cost_tender_42.xlsx",
    "file_path": "generated_docs/cost_tender_42.xlsx",
}


@pytest.fixture
def stub_regen(monkeypatch):
    monkeypatch.setattr(
        cost_breakdown_service, "get_latest_for_tender", lambda db, tid: _Breakdown()
    )
    monkeypatch.setattr(
        cost_breakdown_service, "regenerate_xlsx_artifact", lambda **kw: dict(_INFO)
    )


def test_response_carries_a_presigned_link(monkeypatch, stub_regen, auth_client):
    captured = {}

    def fake_presign(self, key, expires_in=None, response_content_disposition=None):
        captured.update(
            key=key, expires_in=expires_in, disposition=response_content_disposition
        )
        return "https://r2.example/signed?sig=abc"

    from app.services import storage_service as ss
    monkeypatch.setattr(ss.StorageService, "get_presigned_url_sync", fake_presign)

    r = auth_client.post("/api/tenders/42/cost-breakdown/regenerate-xlsx", json={})

    assert r.status_code == 200
    assert r.json()["download_url"] == "https://r2.example/signed?sig=abc"
    assert captured["key"] == "generated_docs/cost_tender_42.xlsx"
    # The browser must save it under the workbook's own name, not the URL's.
    assert 'filename="cost_tender_42.xlsx"' in captured["disposition"]
    # Short-lived: this link is used immediately, unlike an embedded preview.
    assert captured["expires_in"] == route.get_settings().presigned_download_ttl_seconds


def test_local_backend_reports_no_link(monkeypatch, stub_regen, auth_client):
    """Nothing to presign on the filesystem backend; the client streams instead."""
    from app.services import storage_service as ss
    monkeypatch.setattr(
        ss.StorageService, "get_presigned_url_sync", lambda self, *a, **k: None
    )

    r = auth_client.post("/api/tenders/42/cost-breakdown/regenerate-xlsx", json={})

    assert r.status_code == 200
    assert r.json()["download_url"] is None


def test_presign_failure_does_not_fail_the_regeneration(
    monkeypatch, stub_regen, auth_client
):
    """The workbook is the deliverable; the link is an optimisation.

    A signing error must degrade to the streaming path, not lose the workbook
    this request already built and stored.
    """
    def boom(self, *a, **k):
        raise RuntimeError("credentials expired")

    from app.services import storage_service as ss
    monkeypatch.setattr(ss.StorageService, "get_presigned_url_sync", boom)

    r = auth_client.post("/api/tenders/42/cost-breakdown/regenerate-xlsx", json={})

    assert r.status_code == 200
    assert r.json()["artifact_id"] == 11
    assert r.json()["download_url"] is None
