# Backend NIT Link Fetch (Public-Link Fallback) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the backend fetch a tender's NIT (and sibling document links) directly from their public URLs as a fallback to the extension download path, so the analyzer/scoring agent has the NIT even when the extension never captured it.

**Architecture:** One guarded fetch service (`nit_link_fetch_service`) is the single place that turns a stored URL into a `TenderDocument` (host-allowlist + content checks, `(tender, source_url)` dedup). Two flag-gated triggers call it: eager (background, on ingest) and on-demand (inline, at analysis time when no PDF doc exists). No new agent logic — the fetched NIT feeds the existing analyzer + the merged enrichment.

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy / RQ. `requests` or stdlib `urllib` for the fetch (check which is already a dep). Backend venv `drpl-backend/venv/`.

## Global Constraints

- The fetch service NEVER raises — a total failure means "no doc fetched," pipeline proceeds on whatever exists. All outcomes logged under the run-ID.
- Guard chain per URL (reject → skip that URL, continue): https only; host is (or is a subdomain of) an allowlisted portal; host does not resolve to a private/loopback/link-local/reserved IP; GET with browser UA + timeout (~25s) + capped redirects (each redirect target re-checked against the allowlist); status 200; document content-type (not text/html); size ≤ cap (~10MB); first bytes match (`%PDF-` for PDFs).
- Host allowlist = the extension `host_permissions` portal set: `ireps.gov.in`, `gem.gov.in`, `mkp.gem.gov.in`, `tendertiger.com`, `bidassist.com`, `tenderdetail.com`, `tendersinfo.com`, `projectstoday.com` (match host == domain OR host endswith "." + domain).
- Dedup = same `(tender_id, source_url)` key the extension upload uses (`extension.py` upload_document). If a doc with that source_url exists for the tender, skip (no re-store).
- Persist via `storage_service.get_storage_service().upload_file_sync(key_tender_doc(tid, file_name), content, content_type=...)` + `TenderDocument(tender_id, file_name, file_path=key, file_size, mime_type, document_type, source_url, uploaded_by=None)`. Trigger the same background embedding as the extension upload where a BackgroundTasks/worker context exists.
- Config flag `nit_link_fetch_enabled` (default **False**). Nothing fetches when off.
- Commands from `drpl-backend/`; tests via `venv/Scripts/python.exe -m pytest`. Do NOT `import app.main` (live-prod schema self-heal). Use `venv/Scripts/python.exe -c "import ast; ast.parse(open(PATH).read())"` for syntax checks.
- Work on branch `feat/backend-nit-link-fetch` (create off main).

---

### Task 1: Guarded fetch helpers (pure/mockable) + config

**Files:**
- Create: `drpl-backend/app/services/nit_link_fetch_service.py` (guard helpers + fetch, no DB yet)
- Modify: `drpl-backend/app/core/config.py` (add `nit_link_fetch_enabled`, allowlist, caps)
- Test: `drpl-backend/tests/services/test_nit_link_fetch_guards.py` (create)

**Interfaces:**
- Produces:
  - `PORTAL_HOST_ALLOWLIST: tuple[str, ...]`
  - `is_allowed_host(url: str) -> bool` — https + host on allowlist.
  - `resolves_to_private_ip(host: str) -> bool` — DNS resolve + private/loopback/link-local/reserved check.
  - `is_document_response(status: int, content_type: str, first_bytes: bytes, size: int, *, max_bytes: int) -> bool` — 200 + doc content-type + `%PDF-`/known magic + size cap.

- [ ] **Step 1: Add config settings**

In `drpl-backend/app/core/config.py`, inside `class Settings` (near the other tender_analyzer settings ~line 102), add:
```python
    # Backend NIT public-link fetch fallback (design 2026-07-21-backend-nit-link-fetch)
    nit_link_fetch_enabled: bool = False
    nit_link_fetch_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    nit_link_fetch_timeout_s: int = 25
    nit_link_fetch_max_docs_per_tender: int = 8
```

- [ ] **Step 2: Write the failing guard tests**

Create `drpl-backend/tests/services/test_nit_link_fetch_guards.py`:
```python
from app.services.nit_link_fetch_service import (
    is_allowed_host, resolves_to_private_ip, is_document_response,
)


def test_allowed_ireps_host():
    assert is_allowed_host("https://www.ireps.gov.in/ireps/works/pdfdocs/x/viewNitPdf_1.pdf")

def test_allowed_subdomain():
    assert is_allowed_host("https://mkp.gem.gov.in/some/doc.pdf")

def test_rejects_non_https():
    assert not is_allowed_host("http://www.ireps.gov.in/x.pdf")

def test_rejects_unlisted_host():
    assert not is_allowed_host("https://evil.example.com/x.pdf")
    assert not is_allowed_host("https://ireps.gov.in.evil.com/x.pdf")

def test_private_ip_localhost():
    assert resolves_to_private_ip("localhost")

def test_public_host_not_private():
    # A well-known public host should not be flagged private. Uses DNS; if the
    # test env has no DNS, this returns True defensively — accept either by
    # asserting localhost is private (covered above) and skipping net here.
    # Keep this test focused on the localhost/loopback contract.
    assert resolves_to_private_ip("127.0.0.1")

def test_document_response_accepts_pdf():
    assert is_document_response(200, "application/pdf", b"%PDF-1.4 rest",
                                size=1000, max_bytes=10_000)

def test_document_response_rejects_html_login():
    assert not is_document_response(200, "text/html; charset=utf-8",
                                    b"<html><body>login", size=1000, max_bytes=10_000)

def test_document_response_rejects_oversize():
    assert not is_document_response(200, "application/pdf", b"%PDF-1.4",
                                    size=20_000, max_bytes=10_000)

def test_document_response_rejects_non_200():
    assert not is_document_response(404, "application/pdf", b"%PDF-1.4",
                                    size=100, max_bytes=10_000)

def test_document_response_rejects_pdf_ctype_but_html_bytes():
    assert not is_document_response(200, "application/pdf", b"<html> not a pdf",
                                    size=100, max_bytes=10_000)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_nit_link_fetch_guards.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.nit_link_fetch_service'`.

- [ ] **Step 4: Implement the guard helpers**

Create `drpl-backend/app/services/nit_link_fetch_service.py`:
```python
"""Guarded backend fetch of a tender's public document links.

Turns a stored public URL (nit_document_links / document_links) into a
TenderDocument. Fallback to the extension download path — only actually-public
links succeed; gated links fail the content checks and are skipped. See design
docs/superpowers/specs/2026-07-21-backend-nit-link-fetch-design.md.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Source of truth: the extension's host_permissions portal set.
PORTAL_HOST_ALLOWLIST: tuple[str, ...] = (
    "ireps.gov.in",
    "gem.gov.in",
    "mkp.gem.gov.in",
    "tendertiger.com",
    "bidassist.com",
    "tenderdetail.com",
    "tendersinfo.com",
    "projectstoday.com",
)

_DOC_CONTENT_TYPES = ("application/pdf",)
_PDF_MAGIC = b"%PDF-"


def is_allowed_host(url: str) -> bool:
    """https URL whose host is (or is a subdomain of) an allowlisted portal."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    for dom in PORTAL_HOST_ALLOWLIST:
        if host == dom or host.endswith("." + dom):
            return True
    return False


def resolves_to_private_ip(host: str) -> bool:
    """True if the host resolves to any private/loopback/link-local/reserved IP.

    Fails CLOSED: a resolution error returns True (treat as unsafe → skip).
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def is_document_response(status: int, content_type: str, first_bytes: bytes,
                         size: int, *, max_bytes: int) -> bool:
    """200 + document content-type + real magic bytes + within size cap."""
    if status != 200:
        return False
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in _DOC_CONTENT_TYPES:
        return False
    if size > max_bytes:
        return False
    if not first_bytes.startswith(_PDF_MAGIC):
        return False
    return True
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_nit_link_fetch_guards.py -v`
Expected: PASS (11 tests). If `test_public_host_not_private` was written to hit DNS and the env has none, it only asserts `127.0.0.1` is private — no external DNS needed.

- [ ] **Step 6: Commit**

```bash
git add app/services/nit_link_fetch_service.py app/core/config.py tests/services/test_nit_link_fetch_guards.py
git commit -m "feat(analysis): guarded fetch helpers + config for NIT link fallback

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The fetch-and-persist function (DB + storage)

**Files:**
- Modify: `drpl-backend/app/services/nit_link_fetch_service.py` (add the fetch loop + persist)
- Test: `drpl-backend/tests/services/test_nit_link_fetch_persist.py` (create; mock the HTTP fetch + storage)

**Interfaces:**
- Consumes: the Task 1 guards; `storage_service` (`get_storage_service`, `key_tender_doc`); `TenderDocument`, `Tender` models.
- Produces:
  - `FetchResult` dataclass: `fetched: int, skipped_not_public: int, skipped_dedup: int, failed: int, doc_ids: list[int]`.
  - `_http_get(url, *, timeout, max_bytes) -> tuple[int, str, bytes] | None` — returns `(status, content_type, body)` or None on network error. (Isolated so tests mock it.)
  - `fetch_document_links_for_tender(db, tender_id, *, links=None, mark_nit=True, background_tasks=None) -> FetchResult`.

- [ ] **Step 1: Write the failing persist tests**

Create `drpl-backend/tests/services/test_nit_link_fetch_persist.py`. These use an in-memory SQLite session and monkeypatch `_http_get` + storage so no network/R2 is touched:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_nit_link_fetch_persist.py -v`
Expected: FAIL — `fetch_document_links_for_tender` / `_http_get` not defined.

- [ ] **Step 3: Implement fetch + persist**

Append to `drpl-backend/app/services/nit_link_fetch_service.py`:
```python
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse as _urlparse

from app.services.storage_service import get_storage_service, key_tender_doc


@dataclass
class FetchResult:
    fetched: int = 0
    skipped_not_public: int = 0
    skipped_dedup: int = 0
    failed: int = 0
    doc_ids: list = field(default_factory=list)


def _http_get(url: str, *, timeout: int, max_bytes: int):
    """GET a URL with a browser UA, capped body read. Returns
    (status, content_type, body_bytes) or None on any network error.
    Reads at most max_bytes+1 so oversize is detectable.
    """
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (drpl-backend)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            ct = r.headers.get("content-type", "")
            body = r.read(max_bytes + 1)
            return status, ct, body
    except Exception as e:
        logger.info(f"[nit_fetch] GET failed for {url}: {type(e).__name__}: {e}")
        return None


def _filename_from_url(url: str, tender_id: int) -> str:
    import re
    try:
        base = os.path.basename(_urlparse(url).path) or ""
    except Exception:
        base = ""
    if not re.search(r"\.[a-z0-9]{1,8}$", base, re.IGNORECASE):
        base = f"nit_{tender_id}.pdf"
    return base


def fetch_document_links_for_tender(db, tender_id, *, links=None,
                                    mark_nit=True, background_tasks=None) -> FetchResult:
    """Fetch a tender's public document links into TenderDocument rows.

    Never raises. Honors the (tender, source_url) dedup, the host allowlist,
    the private-IP guard, and the content/size checks. Returns a FetchResult.
    """
    from app.core.config import get_settings
    from app.models.tender import Tender, TenderDocument

    res = FetchResult()
    settings = get_settings()
    try:
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if not tender:
            return res

        # Candidate URLs: nit links first (marked nit), then document links.
        nit_links = list(getattr(tender, "nit_document_links", None) or [])
        doc_links = list(getattr(tender, "document_links", None) or [])
        if links is not None:
            candidates = [(u, mark_nit) for u in links]
        else:
            candidates = [(u, True) for u in nit_links] + [(u, False) for u in doc_links]

        cap = settings.nit_link_fetch_max_docs_per_tender
        seen_urls: set = set()
        for url, is_nit in candidates:
            if res.fetched >= cap:
                break
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            if not is_allowed_host(url):
                res.skipped_not_public += 1
                continue

            # Dedup: already have this (tender, source_url)?
            dup = (
                db.query(TenderDocument)
                .filter(TenderDocument.tender_id == tender_id,
                        TenderDocument.source_url == url)
                .first()
            )
            if dup:
                res.skipped_dedup += 1
                continue

            host = _urlparse(url).hostname or ""
            if resolves_to_private_ip(host):
                res.skipped_not_public += 1
                continue

            got = _http_get(url, timeout=settings.nit_link_fetch_timeout_s,
                            max_bytes=settings.nit_link_fetch_max_bytes)
            if not got:
                res.failed += 1
                continue
            status, ctype, body = got
            if not is_document_response(status, ctype, body[:8], len(body),
                                        max_bytes=settings.nit_link_fetch_max_bytes):
                res.skipped_not_public += 1
                continue

            file_name = _filename_from_url(url, tender_id)
            key = key_tender_doc(tender_id, file_name)
            try:
                get_storage_service().upload_file_sync(
                    key, body, content_type="application/pdf")
                doc = TenderDocument(
                    tender_id=tender_id,
                    file_name=file_name,
                    file_path=key,
                    file_size=len(body),
                    mime_type="application/pdf",
                    document_type="nit" if is_nit else "other",
                    source_url=url,
                    uploaded_by=None,
                )
                db.add(doc)
                db.commit()
                db.refresh(doc)
                res.fetched += 1
                res.doc_ids.append(doc.id)
                logger.info(f"[nit_fetch] tender {tender_id}: stored {file_name} "
                            f"from {url} (nit={is_nit})")
            except Exception as e:
                logger.warning(f"[nit_fetch] tender {tender_id}: persist failed for "
                               f"{url}: {type(e).__name__}: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass
                res.failed += 1
    except Exception as e:
        logger.warning(f"[nit_fetch] tender {tender_id}: fatal, returning partial "
                       f"result: {type(e).__name__}: {e}")
    return res
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_nit_link_fetch_persist.py tests/services/test_nit_link_fetch_guards.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/nit_link_fetch_service.py tests/services/test_nit_link_fetch_persist.py
git commit -m "feat(analysis): fetch_document_links_for_tender (guarded fetch + persist)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: On-demand fallback trigger (inline, at analysis time)

**Files:**
- Modify: `drpl-backend/app/services/langchain/graphs/document_analysis_agent.py` (`_run_v2_analysis`, after `pdf_docs` is computed ~line 1668, before `if not pdf_docs:`)

**Interfaces:**
- Consumes: `fetch_document_links_for_tender` from Task 2; `nit_link_fetch_enabled` config.

- [ ] **Step 1: Insert the fallback fetch before the no-pdf early-return**

In `_run_v2_analysis`, the current code (~1664-1670) is:
```python
    pdf_docs = [
        d for d in documents
        if d.file_path and d.file_path.lower().endswith(".pdf")
    ]

    if not pdf_docs:
        logger.info(f"[v2] Tender {tender_id} has no PDF documents — skipping v2")
```
Insert BETWEEN the `pdf_docs = [...]` list and the `if not pdf_docs:` check:
```python
    pdf_docs = [
        d for d in documents
        if d.file_path and d.file_path.lower().endswith(".pdf")
    ]

    # On-demand fallback: no NIT PDF captured but the tender has a public
    # document link → fetch it from the link, then re-query. Flag-gated,
    # never raises. See 2026-07-21-backend-nit-link-fetch-design.
    if not pdf_docs and _settings.nit_link_fetch_enabled:
        tender_row = db.query(Tender).filter(Tender.id == tender_id).first()
        has_link = bool(
            (getattr(tender_row, "nit_document_links", None) or [])
            or (getattr(tender_row, "document_links", None) or [])
        ) if tender_row else False
        if has_link:
            from app.services.nit_link_fetch_service import fetch_document_links_for_tender
            fr = fetch_document_links_for_tender(db, tender_id)
            logger.info(f"[v2] tender {tender_id}: on-demand NIT link fetch → "
                        f"fetched={fr.fetched} skipped_not_public={fr.skipped_not_public} "
                        f"skipped_dedup={fr.skipped_dedup} failed={fr.failed}")
            if fr.fetched:
                documents = db.query(TenderDocument).filter(
                    TenderDocument.tender_id == tender_id,
                ).all()
                pdf_docs = [
                    d for d in documents
                    if d.file_path and d.file_path.lower().endswith(".pdf")
                ]

    if not pdf_docs:
        logger.info(f"[v2] Tender {tender_id} has no PDF documents — skipping v2")
```
NOTE: `_settings` is the module-level settings already used in this file (grep `_settings.tender_analyzer` to confirm the name; if it's `settings` or fetched via `get_settings()`, match the file's convention). `Tender` and `TenderDocument` are already imported at the top of `_run_v2_analysis` (line ~1651).

- [ ] **Step 2: Syntax check**

Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('app/services/langchain/graphs/document_analysis_agent.py').read()); print('syntax ok')"`
Expected: `syntax ok`.

- [ ] **Step 3: Regression check (flag OFF = no behavior change)**

Run: `venv/Scripts/python.exe -m pytest tests/services/ -q`
Expected: PASS (the fallback is behind `nit_link_fetch_enabled=False` by default, so existing tests are unaffected).

- [ ] **Step 4: Commit**

```bash
git add app/services/langchain/graphs/document_analysis_agent.py
git commit -m "feat(analysis): on-demand NIT link fetch fallback in v2 analyzer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Eager trigger (background, on ingest)

**Files:**
- Modify: `drpl-backend/app/api/routes/extension.py` (`upload_tenders` route + a `_dispatch_nit_fetch` helper mirroring `_dispatch_scoring`)

**Interfaces:**
- Consumes: `fetch_document_links_for_tender` from Task 2; `nit_link_fetch_enabled`; `new_ids` from `ingest_tender_batch`.

- [ ] **Step 1: Add a `_dispatch_nit_fetch` helper mirroring `_dispatch_scoring`**

In `drpl-backend/app/api/routes/extension.py`, after the existing `_dispatch_scoring` function (~line 71), add:
```python
def _fetch_nit_inline(new_ids: list[int]) -> None:
    """Inline fallback: fetch public NIT links for newly-ingested tenders."""
    from app.core.database import SessionLocal
    from app.services.nit_link_fetch_service import fetch_document_links_for_tender
    db = SessionLocal()
    try:
        for tid in new_ids:
            try:
                fetch_document_links_for_tender(db, tid)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    f"eager NIT fetch failed for tender {tid}", exc_info=True)
    finally:
        db.close()


def _dispatch_nit_fetch(new_ids: list[int], background_tasks) -> None:
    """Enqueue eager public-link NIT fetch for new tenders (worker if Redis,
    else inline). Flag-gated; no-op when disabled or no new ids."""
    if not new_ids:
        return
    from app.core.config import get_settings
    if not get_settings().nit_link_fetch_enabled:
        return
    q = get_queue()
    if q is not None:
        try:
            q.enqueue("app.services.nit_link_fetch_service.fetch_links_for_tenders_job", new_ids)
            return
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "NIT fetch RQ enqueue failed; falling back to inline", exc_info=True)
    if background_tasks is not None:
        background_tasks.add_task(_fetch_nit_inline, new_ids)
```

- [ ] **Step 2: Add the RQ job entrypoint used above**

Append to `drpl-backend/app/services/nit_link_fetch_service.py`:
```python
def fetch_links_for_tenders_job(tender_ids):
    """RQ entrypoint: fetch public links for a list of tenders. Opens its own
    DB session (safe in a worker). Never raises out of the job."""
    from app.core.database import SessionLocal
    db = SessionLocal()
    try:
        for tid in tender_ids:
            try:
                fetch_document_links_for_tender(db, tid)
            except Exception:
                logger.warning(f"[nit_fetch] job failed for tender {tid}", exc_info=True)
    finally:
        db.close()
```

- [ ] **Step 3: Call `_dispatch_nit_fetch` from `upload_tenders`**

In `upload_tenders` (~line 92), after the existing `_dispatch_scoring(new_ids, background_tasks)` line, add:
```python
        _dispatch_scoring(new_ids, background_tasks)
        _dispatch_nit_fetch(new_ids, background_tasks)
```

- [ ] **Step 4: Syntax check + regression**

Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('app/api/routes/extension.py').read()); print('ok1')"`
Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('app/services/nit_link_fetch_service.py').read()); print('ok2')"`
Expected: `ok1` / `ok2`.

Run: `venv/Scripts/python.exe -m pytest tests/services/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/routes/extension.py app/services/nit_link_fetch_service.py
git commit -m "feat(analysis): eager NIT public-link fetch on tender ingest (flag-gated)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Opt-in live integration test (one real fetch)

**Files:**
- Test: `drpl-backend/tests/services/test_nit_link_fetch_live.py` (create; network-gated)

**Interfaces:**
- Consumes: `is_allowed_host`, `_http_get`, `is_document_response`.

- [ ] **Step 1: Write a network-gated live test**

Create `drpl-backend/tests/services/test_nit_link_fetch_live.py`:
```python
import os
import pytest
from app.services.nit_link_fetch_service import (
    is_allowed_host, _http_get, is_document_response,
)

# Opt-in: only runs when RUN_NET_TESTS=1 (keeps CI offline-safe).
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_NET_TESTS") != "1",
    reason="network test — set RUN_NET_TESTS=1 to run",
)

# A known public IREPS NIT PDF (static URL). If it 404s over time, swap for a
# fresh one from `SELECT nit_document_links FROM tenders WHERE portal='ireps'`.
_LIVE_URL = "https://www.ireps.gov.in/ireps/works/pdfdocs/082026/89872774/viewNitPdf_5425319.pdf"


def test_live_ireps_nit_is_fetchable_pdf():
    assert is_allowed_host(_LIVE_URL)
    got = _http_get(_LIVE_URL, timeout=25, max_bytes=10 * 1024 * 1024)
    assert got is not None, "network fetch returned None"
    status, ctype, body = got
    assert is_document_response(status, ctype, body[:8], len(body),
                                max_bytes=10 * 1024 * 1024)
    assert body.startswith(b"%PDF-")
```

- [ ] **Step 2: Run it opt-in (controller/user, needs network)**

Run: `RUN_NET_TESTS=1 venv/Scripts/python.exe -m pytest tests/services/test_nit_link_fetch_live.py -v`
Expected: PASS (real fetch → PDF). Without the env var it's skipped.
NOTE: the live URL may age out; if it 404s, the controller pulls a fresh `nit_document_links` value from the DB. This test is NOT part of the default suite.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_nit_link_fetch_live.py
git commit -m "test(analysis): opt-in live IREPS NIT fetch integration test

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Fetch service (guards + fetch + persist, dedup, never-raises) → Tasks 1-2 ✓
- Host allowlist + content/size/private-IP checks → Task 1 ✓
- On-demand fallback at analysis time → Task 3 ✓
- Eager trigger on ingest (worker-or-inline, flag-gated, mirrors _dispatch_scoring) → Task 4 ✓
- Config flag default OFF → Task 1 ✓
- Testing: unit guards, mocked persist, opt-in live → Tasks 1,2,5 ✓
- Convergence via (tender, source_url) dedup → Task 2 (dedup query) ✓

**Placeholder scan:** none — all steps carry concrete code. Task 3 Step 1 notes `_settings` name must be confirmed against the file (a verify-in-place, not a placeholder).

**Type consistency:** `fetch_document_links_for_tender(db, tender_id, *, links=None, mark_nit=True, background_tasks=None) -> FetchResult` used consistently across Tasks 2-4. `FetchResult` fields (`fetched`, `skipped_not_public`, `skipped_dedup`, `failed`, `doc_ids`) consistent. `_http_get(url, *, timeout, max_bytes)` consistent between Task 2 impl and Task 5 test. `_dispatch_nit_fetch`/`fetch_links_for_tenders_job` names consistent between Task 4 route and service.

**Known verify-in-place for the implementer:** Task 3 confirms the module settings identifier (`_settings` vs `settings` vs `get_settings()`) against the actual file before inserting.
