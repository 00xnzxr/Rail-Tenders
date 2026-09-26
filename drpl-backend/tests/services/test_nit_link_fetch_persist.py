import pytest
from unittest.mock import patch, MagicMock
from app.services import nit_link_fetch_service as svc


class _FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def first(self): return self._rows[0] if self._rows else None
    def all(self): return self._rows


def test_skips_dedup_when_source_url_exists(monkeypatch):
    tender = MagicMock(id=1, nit_document_links=["https://www.ireps.gov.in/x/viewNitPdf_1.pdf"],
                       document_links=[])
    existing_doc = MagicMock(id=99, source_url="https://www.ireps.gov.in/x/viewNitPdf_1.pdf")
    db = MagicMock()
    # Tender lookup returns tender; TenderDocument dedup lookup returns existing.
    db.query.side_effect = lambda model: _FakeQuery([tender]) if model.__name__ == "Tender" else _FakeQuery([existing_doc])
    called = {"http": 0}
    monkeypatch.setattr(svc, "_http_get", lambda *a, **k: called.__setitem__("http", called["http"] + 1) or None)
    res = svc.fetch_document_links_for_tender(db, 1)
    assert res.skipped_dedup == 1
    assert res.fetched == 0
    assert called["http"] == 0  # dedup short-circuits before any network


def test_skips_non_allowlisted_url(monkeypatch):
    tender = MagicMock(id=1, nit_document_links=["https://evil.example.com/x.pdf"], document_links=[])
    db = MagicMock()
    db.query.side_effect = lambda model: _FakeQuery([tender]) if model.__name__ == "Tender" else _FakeQuery([])
    monkeypatch.setattr(svc, "_http_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    res = svc.fetch_document_links_for_tender(db, 1)
    assert res.skipped_not_public == 1
    assert res.fetched == 0


def test_fetches_and_persists_pdf(monkeypatch):
    url = "https://www.ireps.gov.in/x/viewNitPdf_1.pdf"
    tender = MagicMock(id=1, nit_document_links=[url], document_links=[])
    db = MagicMock()
    db.query.side_effect = lambda model: _FakeQuery([tender]) if model.__name__ == "Tender" else _FakeQuery([])
    monkeypatch.setattr(svc, "resolves_to_private_ip", lambda h: False)
    monkeypatch.setattr(svc, "_http_get", lambda *a, **k: (200, "application/pdf", b"%PDF-1.4 body"))
    fake_storage = MagicMock()
    monkeypatch.setattr(svc, "get_storage_service", lambda: fake_storage)
    monkeypatch.setattr(svc, "key_tender_doc", lambda tid, fn: f"key/{tid}/{fn}")
    res = svc.fetch_document_links_for_tender(db, 1)
    assert res.fetched == 1
    assert fake_storage.upload_file_sync.called
    assert db.add.called and db.commit.called


def test_redirect_is_not_followed_and_skipped(monkeypatch):
    url = "https://www.ireps.gov.in/x/viewNitPdf_1.pdf"
    tender = MagicMock(id=1, nit_document_links=[url], document_links=[])
    db = MagicMock()
    db.query.side_effect = lambda model: _FakeQuery([tender]) if model.__name__ == "Tender" else _FakeQuery([])
    monkeypatch.setattr(svc, "resolves_to_private_ip", lambda h: False)
    # Simulate a 302 surfaced as a non-200 status by _http_get.
    monkeypatch.setattr(svc, "_http_get", lambda *a, **k: (302, "text/html", b"redirecting"))
    fake_storage = MagicMock()
    monkeypatch.setattr(svc, "get_storage_service", lambda: fake_storage)
    res = svc.fetch_document_links_for_tender(db, 1)
    assert res.fetched == 0
    assert not fake_storage.upload_file_sync.called


def test_no_redirect_handler_refuses():
    h = svc._NoRedirect()
    assert h.redirect_request(None, None, 302, "Found", {}, "http://evil.internal/") is None


def test_candidate_cap_bounds_attempts(monkeypatch):
    # 20 allowlisted URLs that all fail the content check must make at most
    # `cap` network attempts, not 20.
    urls = [f"https://www.ireps.gov.in/x/viewNitPdf_{i}.pdf" for i in range(20)]
    tender = MagicMock(id=1, nit_document_links=urls, document_links=[])
    db = MagicMock()
    db.query.side_effect = lambda model: _FakeQuery([tender]) if model.__name__ == "Tender" else _FakeQuery([])
    monkeypatch.setattr(svc, "resolves_to_private_ip", lambda h: False)
    calls = {"n": 0}
    def fake_get(*a, **k):
        calls["n"] += 1
        return (404, "text/html", b"nope")
    monkeypatch.setattr(svc, "_http_get", fake_get)
    monkeypatch.setattr(svc, "time", MagicMock())  # neutralize sleep
    from app.core.config import get_settings
    cap = get_settings().nit_link_fetch_max_docs_per_tender
    svc.fetch_document_links_for_tender(db, 1)
    assert calls["n"] <= cap, f"made {calls['n']} attempts, cap is {cap}"
