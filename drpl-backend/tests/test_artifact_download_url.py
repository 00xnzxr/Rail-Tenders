"""GET /artifacts/{id}/download-url hands the browser a presigned R2 URL so the
backend never proxies the workbook bytes.

Ownership matters here: the endpoint goes through `assert_artifact_access`, so
every fixture artifact needs a real ProposalSession owned by the caller.
"""
import pytest

from app.models.artifact import CommandCenterArtifact
from app.models.proposal import ProposalSession

OWNER_ID = 1


@pytest.fixture
def artifact(db):
    session = ProposalSession(created_by=OWNER_ID, status="draft", title="s")
    db.add(session)
    db.commit()
    db.refresh(session)

    a = CommandCenterArtifact(
        session_id=session.id,
        artifact_type="cost_breakdown_xlsx",
        title="Cost Breakdown - Tender #1",
        content="{}",
        file_path="generated_docs/cost_tender_1.xlsx",
        file_name="cost_tender_1.xlsx",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    yield a


def _patch_exists(monkeypatch, value: bool):
    """Patch the ASYNC existence check.

    The endpoint must use `file_exists`, not `file_exists_sync`: it is an
    `async def`, and the sync variant is a blocking boto3 HEAD that would run
    on the event loop and stall every other request the worker is serving.
    Patching only the async one is therefore also the test that it is the one
    being called.
    """
    from app.services import storage_service as ss

    async def fake_exists(self, key):
        return value

    monkeypatch.setattr(ss.StorageService, "file_exists", fake_exists)


def test_returns_presigned_url_on_r2(monkeypatch, artifact, auth_client):
    captured = {}

    async def fake_presign(self, key, expires_in=None, **kw):
        captured["key"] = key
        captured["expires_in"] = expires_in
        captured["disposition"] = kw.get("response_content_disposition")
        return "https://r2.example/signed?sig=abc"

    from app.services import storage_service as ss
    monkeypatch.setattr(ss.StorageService, "get_presigned_url", fake_presign)
    _patch_exists(monkeypatch, True)
    monkeypatch.setattr(ss.get_storage_service(), "_backend", "r2", raising=False)

    r = auth_client.get(f"/api/command-center/artifacts/{artifact.id}/download-url")

    assert r.status_code == 200
    assert r.json()["url"] == "https://r2.example/signed?sig=abc"
    assert r.json()["file_name"] == "cost_tender_1.xlsx"
    assert captured["key"] == "generated_docs/cost_tender_1.xlsx"
    # Filename must survive the redirect to R2.
    assert 'filename="cost_tender_1.xlsx"' in captured["disposition"]


def test_does_not_block_the_event_loop(monkeypatch, artifact, auth_client):
    """The blocking HEAD must never be reachable from this endpoint."""
    from app.services import storage_service as ss

    def boom(self, key):
        raise AssertionError("download-url called the blocking file_exists_sync")

    monkeypatch.setattr(ss.StorageService, "file_exists_sync", boom)
    _patch_exists(monkeypatch, True)
    monkeypatch.setattr(ss.get_storage_service(), "_backend", "local", raising=False)

    assert auth_client.get(
        f"/api/command-center/artifacts/{artifact.id}/download-url"
    ).status_code == 200


def test_does_not_load_the_artifact_payload(monkeypatch, artifact, auth_client):
    """It answers from a narrow column read, not the whole row.

    `content` and `structured_data` on a costing artifact each hold the full
    line-item JSON. This endpoint reads two strings; pulling megabytes to
    produce them is precisely the cost the download is trying to avoid.
    """
    from app.services import artifact_service, storage_service as ss

    def boom(db, artifact_id):
        raise AssertionError("download-url loaded the full artifact row")

    monkeypatch.setattr(artifact_service, "get_artifact", boom)
    _patch_exists(monkeypatch, True)
    monkeypatch.setattr(ss.get_storage_service(), "_backend", "local", raising=False)

    assert auth_client.get(
        f"/api/command-center/artifacts/{artifact.id}/download-url"
    ).status_code == 200


def test_returns_null_url_on_local_backend(monkeypatch, artifact, auth_client):
    """Local dev has no presigning; the frontend must fall back to /download."""
    from app.services import storage_service as ss
    monkeypatch.setattr(ss.get_storage_service(), "_backend", "local", raising=False)
    _patch_exists(monkeypatch, True)

    r = auth_client.get(f"/api/command-center/artifacts/{artifact.id}/download-url")

    assert r.status_code == 200
    assert r.json()["url"] is None


def test_missing_object_reports_regenerable(monkeypatch, artifact, auth_client):
    """A lost R2 object is not a 404 -- /download can rebuild it from the DB."""
    _patch_exists(monkeypatch, False)

    r = auth_client.get(f"/api/command-center/artifacts/{artifact.id}/download-url")

    assert r.status_code == 200
    assert r.json()["url"] is None
    assert r.json()["regenerate_required"] is True


def test_unknown_artifact_404s(auth_client):
    assert auth_client.get(
        "/api/command-center/artifacts/999999/download-url"
    ).status_code == 404
