"""The preview endpoint always returns 200 with a status.

A missing preview is an expected state (old artifacts, a failed render), not an
error -- the UI degrades to its table view on anything that is not "ready".
"""
import pytest

from app.models.artifact import CommandCenterArtifact
from app.models.proposal import ProposalSession

OWNER_ID = 1
INTRUDER_ID = 2


def _artifact(db, preview=None, owner_id=OWNER_ID):
    session = ProposalSession(created_by=owner_id, status="draft", title="s")
    db.add(session)
    db.commit()
    db.refresh(session)

    a = CommandCenterArtifact(
        session_id=session.id, artifact_type="cost_breakdown_xlsx",
        title="t", content="{}", version=1,
        file_path="generated_docs/book.xlsx", file_name="book.xlsx",
        metadata_json={"preview": preview} if preview else {},
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def test_ready_preview_returns_presigned_pdf_url(monkeypatch, db, auth_client):
    a = _artifact(db, {
        "status": "ready",
        "pdf_key": "previews/artifact_1/v1.pdf",
        "page_count": 9,
        "sheets": [{"name": "1. Summary", "start_page": 1}],
        "error": None,
    })

    async def fake_presign(self, key, expires_in=None, **kw):
        return f"https://r2.example/{key}"

    from app.services import storage_service as ss
    monkeypatch.setattr(ss.StorageService, "get_presigned_url", fake_presign)
    monkeypatch.setattr(ss.get_storage_service(), "_backend", "r2", raising=False)

    r = auth_client.get(f"/api/command-center/artifacts/{a.id}/preview")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["pdf_url"] == "https://r2.example/previews/artifact_1/v1.pdf"
    assert body["page_count"] == 9
    assert body["sheets"] == [{"name": "1. Summary", "start_page": 1}]


def test_artifact_with_no_preview_reports_pending(db, auth_client):
    a = _artifact(db)
    r = auth_client.get(f"/api/command-center/artifacts/{a.id}/preview")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["pdf_url"] is None


def test_failed_preview_surfaces_the_reason(db, auth_client):
    a = _artifact(db, {
        "status": "failed", "error": "soffice exited 1: boom",
        "pdf_key": None, "page_count": 0, "sheets": [],
    })
    r = auth_client.get(f"/api/command-center/artifacts/{a.id}/preview")
    assert r.json()["status"] == "failed"
    assert "boom" in r.json()["error"]
    assert r.json()["pdf_url"] is None


def test_unknown_artifact_404s(auth_client):
    assert auth_client.get(
        "/api/command-center/artifacts/999999/preview"
    ).status_code == 404


def test_non_owner_gets_403(db, intruder_client):
    """The IDOR fix applies here too -- a master_admin non-owner must not read
    another user's preview manifest just because they guessed the artifact id."""
    a = _artifact(db, {
        "status": "ready",
        "pdf_key": "previews/artifact_1/v1.pdf",
        "page_count": 9,
        "sheets": [],
        "error": None,
    }, owner_id=OWNER_ID)
    r = intruder_client.get(f"/api/command-center/artifacts/{a.id}/preview")
    assert r.status_code == 403


def test_unavailable_when_backend_not_r2(monkeypatch, db, auth_client):
    """When preview status is ready with a valid pdf_key, but the storage
    backend is not r2 (e.g. local filesystem dev), presigned URLs do not exist.
    The route overrides status to 'unavailable' and returns no pdf_url."""
    a = _artifact(db, {
        "status": "ready",
        "pdf_key": "previews/artifact_1/v1.pdf",
        "page_count": 9,
        "sheets": [{"name": "1. Summary", "start_page": 1}],
        "error": None,
    })

    from app.services import storage_service as ss
    monkeypatch.setattr(ss.get_storage_service(), "_backend", "local", raising=False)

    r = auth_client.get(f"/api/command-center/artifacts/{a.id}/preview")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["pdf_url"] is None
    assert body["page_count"] == 9
    assert body["sheets"] == [{"name": "1. Summary", "start_page": 1}]


def test_preview_does_not_load_the_artifact_payload(monkeypatch, db, auth_client):
    """The manifest comes from a narrow column read, not the whole row.

    XlsxPreview polls this endpoint every three seconds for as long as the pane
    is open, and the pane opens automatically the moment a costing lands. On a
    costing artifact `content` and `structured_data` each hold the full
    line-item JSON, so loading the row would drag that payload out of the
    database twenty times over, on the same workers the user is waiting on for
    the download itself.
    """
    a = _artifact(db)

    from app.services import artifact_service

    def boom(db_, artifact_id):
        raise AssertionError("preview loaded the full artifact row")

    monkeypatch.setattr(artifact_service, "get_artifact", boom)

    r = auth_client.get(f"/api/command-center/artifacts/{a.id}/preview")

    assert r.status_code == 200
    assert r.json()["status"] == "pending"
