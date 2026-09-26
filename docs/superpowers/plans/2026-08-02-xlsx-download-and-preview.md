# Cost-Breakdown XLSX — Fast Download & Faithful Multi-Tab Preview — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the cost-breakdown Excel download fast and reliable by serving it from a presigned R2 URL, and replace the flat HTML preview with the real workbook rendered by LibreOffice, with one tab per worksheet.

**Architecture:** The download endpoint stops proxying bytes and returns a short-lived presigned R2 URL instead. Separately, an RQ worker job converts the whole workbook to PDF with headless LibreOffice (after a print-setup pass that stops columns falling off the page), reads the PDF outline to map each worksheet to a start page, stores the PDF in R2, and records a manifest on the artifact. The frontend renders that PDF with pdf.js behind a tab bar.

**Tech Stack:** FastAPI, SQLAlchemy, RQ + Redis, openpyxl, PyMuPDF (`fitz`), LibreOffice Calc (headless), React 18 + Vite, pdfjs-dist, Cloudflare R2 (S3-compatible via boto3).

**Design spec:** `docs/superpowers/specs/2026-08-02-xlsx-download-and-preview-design.md`

## Global Constraints

- All file IO goes through `app/services/storage_service.py`. Never write to `uploads/` directly. (CLAUDE.md)
- Background-task entry points wrap work in `run_id_scope(run_id)` so logs carry `[run=...]`. (CLAUDE.md, `app/core/run_context.py`)
- New DB columns need an Alembic revision **and** an entry in `_apply_schema_drift_fixes()` / `_add_missing_columns()`. **This plan adds no columns** — the manifest lives in the existing `command_center_artifacts.metadata_json` JSON column.
- Tests run against a throwaway SQLite DB. `tests/conftest.py` forces `DATABASE_URL` before any `app.*` import — never import `app.*` at the top of `conftest.py`, and never point tests at the live Neon DB in `.env`.
- The distributed `.xlsx` is never modified. The print-setup pass writes to a copy.
- Preview failure must never fail a costing run.
- Existing endpoint `GET /api/command-center/artifacts/{id}/download` stays working unchanged.
- Python 3.11. Backend venv is `drpl-backend/.venv/Scripts/python.exe` on this machine.

---

## File Structure

**Phase A — download (independent, ships first)**

| File | Responsibility |
|---|---|
| `app/core/config.py` (modify) | Add `presigned_download_ttl_seconds` |
| `app/api/routes/command_center.py` (modify) | Add `GET /artifacts/{id}/download-url` |
| `tests/test_artifact_download_url.py` (create) | Endpoint behaviour on both storage backends |
| `drpl-frontend/src/components/command-center/ArtifactPanel.tsx` (modify) | Use the URL; fall back to blob |

**Phase B — preview**

| File | Responsibility |
|---|---|
| `app/services/xlsx_print_setup.py` (create) | Pure: apply print setup to a workbook copy |
| `app/services/xlsx_preview_service.py` (create) | Pure: build the manifest from sheet names + PDF outline; soffice invocation |
| `app/worker/preview_tasks.py` (create) | RQ job: orchestrate download → fit → convert → upload → persist |
| `app/services/cost_breakdown_service.py` (modify) | Shared artifact-creation helper + enqueue |
| `app/services/langchain/graphs/chat_agent_wrappers.py` (modify) | Use the shared helper |
| `app/api/routes/command_center.py` (modify) | Add `GET /artifacts/{id}/preview` |
| `drpl-backend/Dockerfile` (modify) | Install `libreoffice-calc` |
| `app/main.py` (modify) | Startup probe logging `soffice` presence |
| `tests/services/preview/*` (create) | Unit tests for the pure pieces |
| `drpl-frontend/src/components/command-center/XlsxPreview.tsx` (create) | Tab bar + pdf.js viewer, extracted from `ArtifactPanel.tsx` |

`XlsxPreview` moves out of `ArtifactPanel.tsx` (already ~500 lines) into its own file, because it grows from a 70-line table into a stateful viewer.

---

## Task 1: `download-url` endpoint

**Files:**
- Modify: `drpl-backend/app/core/config.py:36`
- Modify: `drpl-backend/app/api/routes/command_center.py` (add after the `download` route ending at line 1577)
- Test: `drpl-backend/tests/test_artifact_download_url.py`

**Interfaces:**
- Consumes: `StorageService.get_presigned_url(key, expires_in) -> str` (async), `StorageService.normalize_key(path) -> str`, `settings.storage_backend`
- Produces: `GET /api/command-center/artifacts/{artifact_id}/download-url` returning `{"url": str | None, "file_name": str}`

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_artifact_download_url.py`:

```python
"""GET /artifacts/{id}/download-url hands the browser a presigned R2 URL so the
backend never proxies the workbook bytes."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.artifact import CommandCenterArtifact


@pytest.fixture
def artifact(db):
    a = CommandCenterArtifact(
        session_id=1,
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


def test_returns_presigned_url_on_r2(monkeypatch, artifact, auth_client):
    captured = {}

    async def fake_presign(self, key, expires_in=None, **kw):
        captured["key"] = key
        captured["expires_in"] = expires_in
        captured["disposition"] = kw.get("response_content_disposition")
        return "https://r2.example/signed?sig=abc"

    from app.services import storage_service as ss
    monkeypatch.setattr(ss.StorageService, "get_presigned_url", fake_presign)
    monkeypatch.setattr(ss.StorageService, "file_exists_sync", lambda self, k: True)
    monkeypatch.setattr(ss.get_storage_service(), "_backend", "r2", raising=False)

    r = auth_client.get(f"/api/command-center/artifacts/{artifact.id}/download-url")

    assert r.status_code == 200
    assert r.json()["url"] == "https://r2.example/signed?sig=abc"
    assert r.json()["file_name"] == "cost_tender_1.xlsx"
    assert captured["key"] == "generated_docs/cost_tender_1.xlsx"
    # Filename must survive the redirect to R2.
    assert 'filename="cost_tender_1.xlsx"' in captured["disposition"]


def test_returns_null_url_on_local_backend(monkeypatch, artifact, auth_client):
    """Local dev has no presigning; the frontend must fall back to /download."""
    from app.services import storage_service as ss
    monkeypatch.setattr(ss.get_storage_service(), "_backend", "local", raising=False)
    monkeypatch.setattr(ss.StorageService, "file_exists_sync", lambda self, k: True)

    r = auth_client.get(f"/api/command-center/artifacts/{artifact.id}/download-url")

    assert r.status_code == 200
    assert r.json()["url"] is None


def test_missing_object_reports_regenerable(monkeypatch, artifact, auth_client):
    """A lost R2 object is not a 404 -- /download can rebuild it from the DB."""
    from app.services import storage_service as ss
    monkeypatch.setattr(ss.StorageService, "file_exists_sync", lambda self, k: False)

    r = auth_client.get(f"/api/command-center/artifacts/{artifact.id}/download-url")

    assert r.status_code == 200
    assert r.json()["url"] is None
    assert r.json()["regenerate_required"] is True


def test_unknown_artifact_404s(auth_client):
    assert auth_client.get(
        "/api/command-center/artifacts/999999/download-url"
    ).status_code == 404
```

Add the shared auth fixture to `drpl-backend/tests/conftest.py` (append at the end of the file):

```python
@pytest.fixture
def auth_client():
    """TestClient with authentication dependency overridden to a stub user."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.auth import get_current_user
    from app.models.user import User

    def _stub_user():
        return User(id=1, email="test@drpl.local", role="master_admin", is_active=True)

    app.dependency_overrides[get_current_user] = _stub_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
```

(`command_center.py:21` imports `get_current_user` from `app.core.auth` — verified.)

- [ ] **Step 2: Run test to verify it fails**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/test_artifact_download_url.py -v
```
Expected: FAIL — all four tests 404, because the route does not exist.

- [ ] **Step 3: Add the config setting**

In `app/core/config.py`, directly after `presigned_url_ttl_seconds: int = 3600`:

```python
    # Download links are short-lived: a browser follows them immediately, so
    # they do not need the hour that embed URLs get.
    presigned_download_ttl_seconds: int = 300
```

- [ ] **Step 4: Add `response_content_disposition` support to the storage service**

In `app/services/storage_service.py`, replace the body of `get_presigned_url`:

```python
    async def get_presigned_url(
        self,
        key: str,
        expires_in: Optional[int] = None,
        response_content_disposition: Optional[str] = None,
    ) -> str:
        """Return a short-lived URL for direct client download. Local backend returns a relative path."""
        key = self.normalize_key(key)
        if self._backend == "r2":
            ttl = expires_in or self._settings.presigned_url_ttl_seconds
            params = {"Bucket": self._bucket, "Key": key}
            if response_content_disposition:
                params["ResponseContentDisposition"] = response_content_disposition
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=ttl,
            )
        # Local: caller should use StreamingResponse instead — return the local path.
        return self._local_path(key)
```

- [ ] **Step 5: Add the endpoint**

In `app/api/routes/command_center.py`, immediately after `download_artifact_file` (which ends at line 1577 with its `raise HTTPException`):

```python
@router.get("/artifacts/{artifact_id}/download-url")
async def get_artifact_download_url(
    artifact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hand the browser a short-lived presigned URL for the artifact's file.

    The bytes go R2 -> browser directly. The previous `/download` route pulled
    the whole object into backend memory and re-streamed it, which crossed
    Railway twice and held a threadpool slot for the entire transfer. That route
    is kept for backward compatibility and as the local-backend fallback.

    Always 200. `url: null` means "use /download instead" — either the backend
    cannot presign (local filesystem) or the object is missing and /download's
    regenerate-from-CostBreakdown branch needs to run.
    """
    from app.services.artifact_service import get_artifact as do_get
    from app.services.storage_service import get_storage_service, StorageService
    from app.core.config import get_settings

    artifact = do_get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    file_name = artifact.file_name or "download.xlsx"
    if not artifact.file_path:
        return {"url": None, "file_name": file_name, "regenerate_required": True}

    storage = get_storage_service()
    key = StorageService.normalize_key(artifact.file_path)

    try:
        exists = storage.file_exists_sync(key)
    except Exception as e:
        logger.warning(f"[artifact download-url] existence check failed for {key!r}: {e}")
        exists = False
    if not exists:
        return {"url": None, "file_name": file_name, "regenerate_required": True}

    if getattr(storage, "_backend", "local") != "r2":
        return {"url": None, "file_name": file_name, "regenerate_required": False}

    url = await storage.get_presigned_url(
        key,
        expires_in=get_settings().presigned_download_ttl_seconds,
        response_content_disposition=f'attachment; filename="{file_name}"',
    )
    return {"url": url, "file_name": file_name, "regenerate_required": False}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/test_artifact_download_url.py -v
```
Expected: 4 passed.

- [ ] **Step 7: Run the full suite for regressions**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/ -q
```
Expected: no failures. Baseline on this branch is 378 passed, 1 skipped, so 382 passed.
(The 415 figure from the costing work includes 37 tests that live on
`fix/costing-tax-line-and-timeout`, not this branch.)

- [ ] **Step 8: Commit**

```bash
git add drpl-backend/app/core/config.py drpl-backend/app/services/storage_service.py \
        drpl-backend/app/api/routes/command_center.py \
        drpl-backend/tests/test_artifact_download_url.py drpl-backend/tests/conftest.py
git commit -m "feat(artifacts): presigned download-url endpoint so the backend stops proxying xlsx bytes"
```

---

## Task 2: Frontend uses the presigned URL

**Files:**
- Modify: `drpl-frontend/src/components/command-center/ArtifactPanel.tsx:117-141`

**Interfaces:**
- Consumes: `GET /api/command-center/artifacts/{id}/download-url` → `{url, file_name, regenerate_required}`
- Produces: `downloadArtifactFile(id: number, fallbackName: string): Promise<void>` (same signature as today — all four call sites at lines 207, 223, 340, 402 are unchanged)

- [ ] **Step 1: Replace `downloadArtifactFile`**

In `ArtifactPanel.tsx`, replace the whole function at lines 117-141:

```tsx
  /** Trigger a file download.
   *
   * Preferred path: ask the backend for a short-lived presigned R2 URL and point
   * an anchor at it, so the bytes go R2 -> browser and never enter the JS heap.
   * Falls back to streaming through the backend when the URL is null (local
   * filesystem backend, or a missing object that /download can regenerate).
   */
  const downloadArtifactFile = async (id: number, fallbackName: string) => {
    const token = localStorage.getItem('drpl_token');
    const auth = { Authorization: `Bearer ${token}` };

    const saveAs = (href: string, name: string) => {
      const a = document.createElement('a');
      a.href = href;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    };

    try {
      const meta = await fetch(
        `${API_BASE}/api/command-center/artifacts/${id}/download-url`,
        { headers: auth },
      );
      if (meta.ok) {
        const { url, file_name } = await meta.json();
        if (url) {
          saveAs(url, file_name || fallbackName);
          return;
        }
      }

      // Fallback: stream through the backend (local dev, or regenerate-on-miss).
      const res = await fetch(
        `${API_BASE}/api/command-center/artifacts/${id}/download`,
        { headers: auth },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const disposition = res.headers.get('content-disposition') || '';
      const match = disposition.match(/filename="?([^"]+)"?/);
      const blobUrl = URL.createObjectURL(await res.blob());
      saveAs(blobUrl, match ? match[1] : fallbackName);
      URL.revokeObjectURL(blobUrl);
    } catch (err) {
      console.error('Artifact download failed:', err);
      window.alert(
        'Could not download the Excel file. Please try again — if it keeps ' +
        'failing, re-run the costing for this tender or contact support.',
      );
    }
  };
```

- [ ] **Step 2: Typecheck and build**

```bash
cd drpl-frontend && npm run build
```
Expected: `tsc -b` clean, Vite build succeeds.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/command-center/ArtifactPanel.tsx
git commit -m "feat(artifacts): download xlsx straight from R2 via presigned URL"
```

**Phase A is now complete and independently shippable.** Deploy and confirm download speed before continuing.

---

## Task 3: Print-setup pass

Without this, LibreOffice clips at the page boundary and drops whole columns — the spike lost Estimated Cost, Gross Margin, GM % and the Reconciliation status columns off the Summary sheet.

**Files:**
- Create: `drpl-backend/app/services/xlsx_print_setup.py`
- Test: `drpl-backend/tests/services/preview/test_xlsx_print_setup.py`
- Create: `drpl-backend/tests/services/preview/__init__.py` (empty)

**Interfaces:**
- Produces: `apply_print_setup(src_path: str, dest_path: str) -> list[str]` — writes a fitted copy, returns worksheet titles in tab order. Never mutates `src_path`.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/services/preview/test_xlsx_print_setup.py`:

```python
"""The print-setup pass is what keeps columns on the page.

Without it LibreOffice clips at the page boundary: the spike against prod
breakdown 76 lost four columns off the Summary sheet (Estimated Cost, Gross
Margin, GM %, and the Reconciliation status columns) and truncated the
narrative. Excel hides this on screen by overflowing text across empty cells.
"""
import os

from openpyxl import Workbook, load_workbook

from app.services.xlsx_print_setup import apply_print_setup, MAX_WIDTH, MIN_WIDTH


def _workbook(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "1. Summary"
    ws["A1"] = "Schedule ELECTRICAL AND PNEUMATIC SPARES FOR DETC"
    ws["B1"] = 1234.5
    ws["C1"] = "x" * 200          # longer than the clamp
    ws["D1"] = "=SUM(B1:B1)"      # formula: its text must not drive width
    second = wb.create_sheet("2. Cost Assumptions")
    second["A1"] = "short"
    src = str(tmp_path / "src.xlsx")
    wb.save(src)
    return src


def test_returns_sheet_titles_in_tab_order(tmp_path):
    src = _workbook(tmp_path)
    names = apply_print_setup(src, str(tmp_path / "out.xlsx"))
    assert names == ["1. Summary", "2. Cost Assumptions"]


def test_source_workbook_is_never_modified(tmp_path):
    src = _workbook(tmp_path)
    before = open(src, "rb").read()
    apply_print_setup(src, str(tmp_path / "out.xlsx"))
    assert open(src, "rb").read() == before


def test_column_width_fits_content(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    ws = load_workbook(dest)["1. Summary"]
    # 48-char label + padding, under the clamp.
    assert ws.column_dimensions["A"].width >= len(
        "Schedule ELECTRICAL AND PNEUMATIC SPARES FOR DETC"
    )
    assert ws.column_dimensions["A"].width <= MAX_WIDTH


def test_overlong_cell_is_clamped_and_wrapped(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    ws = load_workbook(dest)["1. Summary"]
    assert ws.column_dimensions["C"].width == MAX_WIDTH
    assert ws["C1"].alignment.wrap_text is True


def test_narrow_column_gets_a_floor(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    ws = load_workbook(dest)["1. Summary"]
    assert ws.column_dimensions["B"].width >= MIN_WIDTH


def test_formula_text_does_not_drive_width(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    ws = load_workbook(dest)["1. Summary"]
    # "=SUM(B1:B1)" is 11 chars; the column must not be sized to the formula
    # source, only floored to MIN_WIDTH.
    assert ws.column_dimensions["D"].width == MIN_WIDTH


def test_fit_to_width_is_set_on_every_sheet(tmp_path):
    src = _workbook(tmp_path)
    dest = str(tmp_path / "out.xlsx")
    apply_print_setup(src, dest)
    wb = load_workbook(dest)
    for ws in wb.worksheets:
        assert ws.page_setup.fitToWidth == 1
        assert ws.page_setup.fitToHeight == 0
        assert ws.sheet_properties.pageSetUpPr.fitToPage is True
        assert ws.page_setup.orientation == "landscape"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/services/preview/test_xlsx_print_setup.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.xlsx_print_setup'`.

- [ ] **Step 3: Implement**

Create `drpl-backend/app/services/xlsx_print_setup.py`:

```python
"""Prepare a workbook copy for faithful PDF rendering.

The generated cost workbook sets no print setup. LibreOffice therefore clips at
the page boundary, and columns past that boundary are dropped entirely — the
spike against prod breakdown 76 lost Estimated Cost, Gross Margin, GM % and the
Reconciliation status columns off the Summary sheet, and truncated the Key
Observations narrative. Excel masks this on screen by overflowing text across
adjacent empty cells; it only appears when the sheet is printed.

This pass sizes columns to their content, switches to landscape, and scales each
sheet to one page wide. Verified to restore every clipped column and to reduce
that workbook from 52 pages to 9.

The source file is never modified — the distributed .xlsx must stay byte-identical
to what costing produced.
"""
from __future__ import annotations

import logging

from openpyxl import load_workbook
from openpyxl.styles import Alignment

logger = logging.getLogger(__name__)

# Column width bounds, in approximate characters (openpyxl's unit).
MIN_WIDTH = 8
MAX_WIDTH = 60


def apply_print_setup(src_path: str, dest_path: str) -> list[str]:
    """Write a print-ready copy of ``src_path`` to ``dest_path``.

    Returns the worksheet titles in tab order — the authoritative tab list for
    the preview manifest.
    """
    wb = load_workbook(src_path)

    for ws in wb.worksheets:
        widest: dict[str, int] = {}
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if value is None:
                    continue
                text = str(value)
                # A formula's source text is not what prints; its result is, and
                # we cannot know that width here. Let the floor cover it.
                if text.startswith("="):
                    continue
                longest = max((len(seg) for seg in text.split("\n")), default=0)
                col = cell.column_letter
                if longest > widest.get(col, 0):
                    widest[col] = longest

        for col, longest in widest.items():
            ws.column_dimensions[col].width = max(
                MIN_WIDTH, min(MAX_WIDTH, longest + 2)
            )
            if longest > MAX_WIDTH:
                # Too wide to fit: wrap instead of letting it run off the page.
                for cell in ws[col]:
                    ws[cell.coordinate].alignment = Alignment(
                        horizontal=cell.alignment.horizontal,
                        vertical=cell.alignment.vertical,
                        wrap_text=True,
                    )

        # Columns with only formulas never entered `widest`; give them the floor
        # so they are not left at openpyxl's default.
        for col_cells in ws.iter_cols():
            letter = col_cells[0].column_letter
            if letter not in widest and any(c.value is not None for c in col_cells):
                ws.column_dimensions[letter].width = MIN_WIDTH

        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_margins.left = 0.3
        ws.page_margins.right = 0.3
        ws.page_margins.top = 0.4
        ws.page_margins.bottom = 0.4

    wb.save(dest_path)
    names = [ws.title for ws in wb.worksheets]
    wb.close()
    logger.info(
        "[xlsx print setup] fitted %d sheet(s): %s", len(names), ", ".join(names)
    )
    return names
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/services/preview/test_xlsx_print_setup.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/xlsx_print_setup.py drpl-backend/tests/services/preview/
git commit -m "feat(preview): print-setup pass so PDF rendering stops dropping columns"
```

---

## Task 4: Manifest builder

**Files:**
- Create: `drpl-backend/app/services/xlsx_preview_service.py`
- Test: `drpl-backend/tests/services/preview/test_preview_manifest.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `build_manifest(sheet_names: list[str], toc: list, page_count: int) -> dict` → `{"sheets": [{"name": str, "start_page": int}], "page_count": int}`
  - `convert_to_pdf(src_path: str, out_dir: str, timeout_s: int = 180) -> str` (added in Task 5)

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/services/preview/test_preview_manifest.py`:

```python
"""Mapping the PDF outline onto the worksheet tab list.

LibreOffice Calc emits one PDF bookmark per worksheet (confirmed by the
2026-08-02 spike: 3 entries with start pages 1 / 4 / 13). The manifest turns
that into the preview's tab bar.
"""
from app.services.xlsx_preview_service import build_manifest


def test_maps_each_sheet_to_its_start_page():
    toc = [
        [1, "1. Summary", 1],
        [1, "2. Cost Assumptions", 4],
        [1, "3. All Schedules", 13],
    ]
    m = build_manifest(["1. Summary", "2. Cost Assumptions", "3. All Schedules"], toc, 52)
    assert m["page_count"] == 52
    assert m["sheets"] == [
        {"name": "1. Summary", "start_page": 1},
        {"name": "2. Cost Assumptions", "start_page": 4},
        {"name": "3. All Schedules", "start_page": 13},
    ]


def test_empty_outline_falls_back_to_one_tab():
    """A workbook whose outline is missing still previews -- just less navigable."""
    m = build_manifest(["1. Summary", "2. Cost Assumptions"], [], 9)
    assert m["sheets"] == [{"name": "1. Summary", "start_page": 1}]
    assert m["page_count"] == 9


def test_partial_outline_falls_back_to_one_tab():
    """Fewer bookmarks than sheets means the mapping is untrustworthy."""
    m = build_manifest(["A", "B", "C"], [[1, "A", 1]], 6)
    assert m["sheets"] == [{"name": "A", "start_page": 1}]


def test_outline_order_wins_over_name_matching():
    """Excel truncates sheet names to 31 chars, so bookmark text may not match
    the openpyxl title exactly. Position is the reliable key, not the string."""
    long_a = "A" * 40
    long_b = "B" * 40
    toc = [[1, "A" * 31, 1], [1, "B" * 31, 5]]
    m = build_manifest([long_a, long_b], toc, 8)
    # Names come from openpyxl (authoritative), pages from the outline.
    assert m["sheets"] == [
        {"name": long_a, "start_page": 1},
        {"name": long_b, "start_page": 5},
    ]


def test_nested_outline_entries_are_ignored():
    """Only top-level (level 1) bookmarks are sheets."""
    toc = [[1, "A", 1], [2, "A sub-heading", 2], [1, "B", 5]]
    m = build_manifest(["A", "B"], toc, 8)
    assert m["sheets"] == [
        {"name": "A", "start_page": 1},
        {"name": "B", "start_page": 5},
    ]


def test_no_sheets_yields_no_tabs():
    assert build_manifest([], [], 0)["sheets"] == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/services/preview/test_preview_manifest.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.xlsx_preview_service'`.

- [ ] **Step 3: Implement**

Create `drpl-backend/app/services/xlsx_preview_service.py`:

```python
"""Render a cost-breakdown workbook to a previewable PDF.

One `soffice` pass over the WHOLE workbook. Converting sheet-by-sheet is not an
option: the workbook uses cross-sheet formulas and named ranges
(`xlsx_generator_tool.py:767-812`), so isolating a sheet turns those references
into #REF!. A single pass also lets LibreOffice compute the totals, which
openpyxl never wrote cached values for.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def build_manifest(sheet_names: list[str], toc: list, page_count: int) -> dict:
    """Map worksheet tab order onto PDF start pages.

    `sheet_names` comes from openpyxl and is authoritative for the tab labels.
    `toc` is PyMuPDF's `Document.get_toc()` — `[level, title, page]` rows.

    Names are taken from openpyxl rather than the bookmark text because Excel
    truncates sheet names to 31 characters, so the two can differ. Position is
    the reliable key.

    When the outline is absent or has fewer top-level entries than there are
    sheets, the mapping cannot be trusted, so we emit a single tab spanning the
    document. The preview is then less navigable but still correct.
    """
    if not sheet_names:
        return {"sheets": [], "page_count": page_count}

    tops = [row for row in (toc or []) if row and row[0] == 1]

    if len(tops) < len(sheet_names):
        if tops:
            logger.warning(
                "[xlsx preview] outline has %d top-level entries for %d sheet(s) "
                "— falling back to a single tab",
                len(tops), len(sheet_names),
            )
        return {
            "sheets": [{"name": sheet_names[0], "start_page": 1}],
            "page_count": page_count,
        }

    return {
        "sheets": [
            {"name": name, "start_page": int(tops[i][2])}
            for i, name in enumerate(sheet_names)
        ],
        "page_count": page_count,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/services/preview/test_preview_manifest.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/xlsx_preview_service.py drpl-backend/tests/services/preview/test_preview_manifest.py
git commit -m "feat(preview): map PDF outline onto worksheet tabs"
```

---

## Task 5: LibreOffice conversion

**Files:**
- Modify: `drpl-backend/app/services/xlsx_preview_service.py`
- Test: `drpl-backend/tests/services/preview/test_convert_to_pdf.py`

**Interfaces:**
- Consumes: `build_manifest` (Task 4)
- Produces: `convert_to_pdf(src_path: str, out_dir: str, timeout_s: int = 180) -> str` — returns the PDF path; raises `PreviewRenderError` on failure. Also exports `PreviewRenderError`, `SOFFICE_BIN`, `soffice_available() -> bool`.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/services/preview/test_convert_to_pdf.py`:

```python
"""soffice invocation: isolation, timeout, and failure surfacing.

The 4 worker replicas each run conversions, so they must not share a LibreOffice
user profile -- a shared profile takes an exclusive lock and the second process
silently exits. Each job therefore gets its own -env:UserInstallation.
"""
import subprocess

import pytest

from app.services import xlsx_preview_service as svc


def test_command_isolates_the_user_profile(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["timeout"] = kw.get("timeout")
        (tmp_path / "book.pdf").write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, 0, "converted", "")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    out = svc.convert_to_pdf(str(src), str(tmp_path), timeout_s=90)

    assert out == str(tmp_path / "book.pdf")
    assert captured["timeout"] == 90
    assert any(a.startswith("-env:UserInstallation=file://") for a in captured["cmd"])
    assert "--headless" in captured["cmd"]
    assert "--convert-to" in captured["cmd"]


def test_two_calls_use_different_profiles(monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(next(a for a in cmd if a.startswith("-env:UserInstallation=")))
        (tmp_path / "book.pdf").write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    svc.convert_to_pdf(str(src), str(tmp_path))
    svc.convert_to_pdf(str(src), str(tmp_path))

    assert seen[0] != seen[1]


def test_nonzero_exit_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        svc.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError) as exc:
        svc.convert_to_pdf(str(src), str(tmp_path))
    assert "boom" in str(exc.value)


def test_timeout_raises_rather_than_hanging_the_worker(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 180))

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError) as exc:
        svc.convert_to_pdf(str(src), str(tmp_path))
    assert "timed out" in str(exc.value).lower()


def test_missing_output_raises(monkeypatch, tmp_path):
    """rc=0 but no file is still a failure -- do not return a phantom path."""
    monkeypatch.setattr(
        svc.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    src = tmp_path / "book.xlsx"
    src.write_bytes(b"fake")

    with pytest.raises(svc.PreviewRenderError):
        svc.convert_to_pdf(str(src), str(tmp_path))
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/services/preview/test_convert_to_pdf.py -v
```
Expected: FAIL — `AttributeError: module ... has no attribute 'convert_to_pdf'`.

- [ ] **Step 3: Implement**

Append to `drpl-backend/app/services/xlsx_preview_service.py` (and add `import os`, `import shutil`, `import subprocess`, `import tempfile`, `import uuid` to the imports at the top):

```python
SOFFICE_BIN = os.environ.get("SOFFICE_BIN", "soffice")


class PreviewRenderError(RuntimeError):
    """The workbook could not be rendered. Never fatal to a costing run."""


def soffice_available() -> bool:
    """True when the LibreOffice binary is on PATH."""
    return shutil.which(SOFFICE_BIN) is not None


def convert_to_pdf(src_path: str, out_dir: str, timeout_s: int = 180) -> str:
    """Convert a workbook to PDF with headless LibreOffice.

    Each call gets a private ``-env:UserInstallation`` profile: LibreOffice takes
    an exclusive lock on its profile directory, so concurrent worker replicas
    sharing one would have all but the first silently exit.
    """
    profile = os.path.join(tempfile.gettempdir(), f"lo_profile_{uuid.uuid4().hex}")
    cmd = [
        SOFFICE_BIN,
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--norestore",
        "--convert-to", "pdf:calc_pdf_Export",
        "--outdir", out_dir,
        src_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise PreviewRenderError(
            f"soffice timed out after {timeout_s}s converting {os.path.basename(src_path)}"
        )
    except FileNotFoundError:
        raise PreviewRenderError(
            f"{SOFFICE_BIN} not found — is libreoffice-calc installed in this image?"
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    if proc.returncode != 0:
        raise PreviewRenderError(
            f"soffice exited {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:300]}"
        )

    stem = os.path.splitext(os.path.basename(src_path))[0]
    pdf_path = os.path.join(out_dir, f"{stem}.pdf")
    if not os.path.exists(pdf_path):
        raise PreviewRenderError(f"soffice reported success but {pdf_path} is missing")
    return pdf_path
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/services/preview/ -v
```
Expected: 18 passed (7 + 6 + 5).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/xlsx_preview_service.py drpl-backend/tests/services/preview/test_convert_to_pdf.py
git commit -m "feat(preview): headless LibreOffice conversion with per-job profile and timeout"
```

---

## Task 6: Render job

**Files:**
- Create: `drpl-backend/app/worker/preview_tasks.py`
- Test: `drpl-backend/tests/worker/test_preview_tasks.py`

**Interfaces:**
- Consumes: `apply_print_setup` (Task 3), `build_manifest` / `convert_to_pdf` / `PreviewRenderError` (Tasks 4-5), `StorageService`, `CommandCenterArtifact`
- Produces:
  - `render_xlsx_preview(artifact_id: int) -> dict` — RQ entrypoint, returns `{"status": ..., "artifact_id": ...}`
  - `preview_key(artifact_id: int, version: int) -> str`
  - `enqueue_preview_render(artifact_id: int) -> bool`

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/worker/test_preview_tasks.py`:

```python
"""The preview render job.

A preview failure must never fail the costing run that produced the workbook --
it records status="failed" with a reason and returns.
"""
import pytest

from app.models.artifact import CommandCenterArtifact
from app.worker import preview_tasks


@pytest.fixture
def artifact(db):
    a = CommandCenterArtifact(
        session_id=1,
        artifact_type="cost_breakdown_xlsx",
        title="Cost Breakdown",
        content="{}",
        version=2,
        file_path="generated_docs/book.xlsx",
        file_name="book.xlsx",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    yield a


def test_preview_key_is_versioned(artifact):
    """A regenerated workbook must not serve the previous render."""
    assert preview_tasks.preview_key(artifact.id, 2) == f"previews/artifact_{artifact.id}/v2.pdf"
    assert preview_tasks.preview_key(artifact.id, 3) != preview_tasks.preview_key(artifact.id, 2)


def test_successful_render_writes_a_ready_manifest(monkeypatch, db, artifact):
    monkeypatch.setattr(preview_tasks, "_download_workbook", lambda k, d: f"{d}/book.xlsx")
    monkeypatch.setattr(
        preview_tasks, "apply_print_setup",
        lambda src, dest: ["1. Summary", "2. Cost Assumptions"],
    )
    monkeypatch.setattr(preview_tasks, "convert_to_pdf", lambda src, out, **kw: f"{out}/book.pdf")
    monkeypatch.setattr(
        preview_tasks, "_read_pdf_outline",
        lambda p: ([[1, "1. Summary", 1], [1, "2. Cost Assumptions", 4]], 9),
    )
    uploaded = {}
    monkeypatch.setattr(
        preview_tasks, "_upload_pdf",
        lambda key, path: uploaded.setdefault("key", key),
    )

    result = preview_tasks.render_xlsx_preview(artifact.id)

    assert result["status"] == "ready"
    db.refresh(artifact)
    preview = artifact.metadata_json["preview"]
    assert preview["status"] == "ready"
    assert preview["page_count"] == 9
    assert preview["sheets"] == [
        {"name": "1. Summary", "start_page": 1},
        {"name": "2. Cost Assumptions", "start_page": 4},
    ]
    assert preview["pdf_key"] == uploaded["key"] == f"previews/artifact_{artifact.id}/v2.pdf"
    assert preview["error"] is None


def test_render_failure_is_recorded_not_raised(monkeypatch, db, artifact):
    from app.services.xlsx_preview_service import PreviewRenderError

    monkeypatch.setattr(preview_tasks, "_download_workbook", lambda k, d: f"{d}/book.xlsx")
    monkeypatch.setattr(preview_tasks, "apply_print_setup", lambda src, dest: ["S"])

    def boom(src, out, **kw):
        raise PreviewRenderError("soffice exited 1: boom")

    monkeypatch.setattr(preview_tasks, "convert_to_pdf", boom)

    result = preview_tasks.render_xlsx_preview(artifact.id)

    assert result["status"] == "failed"
    db.refresh(artifact)
    preview = artifact.metadata_json["preview"]
    assert preview["status"] == "failed"
    assert "boom" in preview["error"]


def test_existing_metadata_is_preserved(monkeypatch, db, artifact):
    artifact.metadata_json = {"tender_id": 3808, "file_name": "book.xlsx"}
    db.commit()
    monkeypatch.setattr(preview_tasks, "_download_workbook", lambda k, d: f"{d}/book.xlsx")
    monkeypatch.setattr(preview_tasks, "apply_print_setup", lambda src, dest: ["S"])
    monkeypatch.setattr(preview_tasks, "convert_to_pdf", lambda src, out, **kw: f"{out}/book.pdf")
    monkeypatch.setattr(preview_tasks, "_read_pdf_outline", lambda p: ([[1, "S", 1]], 3))
    monkeypatch.setattr(preview_tasks, "_upload_pdf", lambda key, path: None)

    preview_tasks.render_xlsx_preview(artifact.id)

    db.refresh(artifact)
    assert artifact.metadata_json["tender_id"] == 3808
    assert artifact.metadata_json["preview"]["status"] == "ready"


def test_unknown_artifact_returns_missing(db):
    assert preview_tasks.render_xlsx_preview(999999)["status"] == "missing"


def test_artifact_without_file_path_is_skipped(db):
    a = CommandCenterArtifact(
        session_id=1, artifact_type="cost_breakdown_xlsx",
        title="t", content="{}", version=1,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    assert preview_tasks.render_xlsx_preview(a.id)["status"] == "skipped"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/worker/test_preview_tasks.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.worker.preview_tasks'`.

- [ ] **Step 3: Implement**

Create `drpl-backend/app/worker/preview_tasks.py`:

```python
"""RQ job: render a cost-breakdown workbook to a previewable PDF.

Enqueued when a `cost_breakdown_xlsx` artifact is created. A failure here is
recorded on the artifact and never propagated — the costing run that produced
the workbook has already succeeded, and the UI degrades to its table view.
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone

from app.core.run_context import run_id_scope
from app.services.xlsx_preview_service import (
    PreviewRenderError,
    build_manifest,
    convert_to_pdf,
)
from app.services.xlsx_print_setup import apply_print_setup

logger = logging.getLogger(__name__)

PREVIEW_MIME = "application/pdf"


def preview_key(artifact_id: int, version: int) -> str:
    """Storage key for a render. Versioned so a regenerated workbook (which
    bumps the artifact version) never serves the previous render."""
    return f"previews/artifact_{artifact_id}/v{version}.pdf"


def _download_workbook(key: str, dest_dir: str) -> str:
    from app.services.storage_service import get_storage_service

    data = get_storage_service().download_file_sync(key)
    path = os.path.join(dest_dir, "book.xlsx")
    with open(path, "wb") as f:
        f.write(data)
    return path


def _upload_pdf(key: str, path: str) -> None:
    from app.services.storage_service import get_storage_service

    with open(path, "rb") as f:
        get_storage_service().upload_file_sync(key, f.read(), content_type=PREVIEW_MIME)


def _read_pdf_outline(pdf_path: str) -> tuple[list, int]:
    """Return ``(toc, page_count)`` for a PDF."""
    import fitz

    doc = fitz.open(pdf_path)
    try:
        return doc.get_toc(), doc.page_count
    finally:
        doc.close()


def _save_preview(db, artifact, **fields) -> None:
    """Merge a preview manifest into metadata_json without dropping siblings."""
    meta = dict(artifact.metadata_json or {})
    meta["preview"] = {
        "rendered_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
        **fields,
    }
    artifact.metadata_json = meta
    # JSON columns are mutated in place; flag_modified guarantees the UPDATE.
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(artifact, "metadata_json")
    db.commit()


def render_xlsx_preview(artifact_id: int) -> dict:
    """RQ entrypoint. Never raises — records failure on the artifact."""
    from app.core.database import SessionLocal
    from app.models.artifact import CommandCenterArtifact

    with run_id_scope(f"preview-{artifact_id}"):
        db = SessionLocal()
        try:
            artifact = (
                db.query(CommandCenterArtifact)
                .filter(CommandCenterArtifact.id == artifact_id)
                .first()
            )
            if artifact is None:
                logger.warning("[preview] artifact %s not found", artifact_id)
                return {"status": "missing", "artifact_id": artifact_id}
            if not artifact.file_path:
                logger.warning("[preview] artifact %s has no file_path", artifact_id)
                return {"status": "skipped", "artifact_id": artifact_id}

            key = preview_key(artifact.id, artifact.version or 1)
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    src = _download_workbook(artifact.file_path, tmp)
                    fitted = os.path.join(tmp, "fitted.xlsx")
                    sheet_names = apply_print_setup(src, fitted)
                    pdf_path = convert_to_pdf(fitted, tmp)
                    toc, page_count = _read_pdf_outline(pdf_path)
                    _upload_pdf(key, pdf_path)

                manifest = build_manifest(sheet_names, toc, page_count)
                _save_preview(
                    db, artifact,
                    status="ready",
                    pdf_key=key,
                    page_count=manifest["page_count"],
                    sheets=manifest["sheets"],
                )
                logger.info(
                    "[preview] artifact %s rendered: %d sheet(s), %d page(s)",
                    artifact_id, len(manifest["sheets"]), manifest["page_count"],
                )
                return {"status": "ready", "artifact_id": artifact_id}

            except Exception as e:  # noqa: BLE001  (PreviewRenderError included)
                reason = f"{type(e).__name__}: {e}"[:300]
                logger.exception("[preview] artifact %s render failed", artifact_id)
                try:
                    db.rollback()
                    artifact = (
                        db.query(CommandCenterArtifact)
                        .filter(CommandCenterArtifact.id == artifact_id)
                        .first()
                    )
                    meta = dict(artifact.metadata_json or {})
                    meta["preview"] = {
                        "status": "failed",
                        "error": reason,
                        "pdf_key": None,
                        "page_count": 0,
                        "sheets": [],
                        "rendered_at": datetime.now(timezone.utc).isoformat(),
                    }
                    artifact.metadata_json = meta
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(artifact, "metadata_json")
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
                return {"status": "failed", "artifact_id": artifact_id, "error": reason}
        finally:
            db.close()


def enqueue_preview_render(artifact_id: int) -> bool:
    """Queue a render. Returns False when Redis is off (local dev) — never raises."""
    from app.core.redis_client import get_queue

    q = get_queue()
    if q is None:
        logger.info("[preview] queue unavailable; skipping render for %s", artifact_id)
        return False
    try:
        q.enqueue(
            "app.worker.preview_tasks.render_xlsx_preview",
            artifact_id,
            job_id=f"preview-render-{artifact_id}",
            result_ttl=86400,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[preview] enqueue failed for artifact %s: %s", artifact_id, e)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/worker/test_preview_tasks.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/worker/preview_tasks.py drpl-backend/tests/worker/test_preview_tasks.py
git commit -m "feat(preview): RQ job rendering the workbook to a versioned PDF"
```

---

## Task 7: Enqueue from both artifact-creation sites

Both sites end with the same "upload key → create_artifact → set file_path → commit" block. Factoring it into one helper means a future third call site cannot silently skip the preview.

**Files:**
- Modify: `drpl-backend/app/services/cost_breakdown_service.py:1996-2021`
- Modify: `drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py:3242-3268`
- Test: `drpl-backend/tests/services/preview/test_artifact_enqueue.py`

**Interfaces:**
- Consumes: `enqueue_preview_render(artifact_id) -> bool` (Task 6)
- Produces: `persist_xlsx_artifact(db, *, session_id, title, fname, data, rows, summary, structured_extra=None, metadata_extra=None) -> dict` in `cost_breakdown_service`, returning `{"artifact_id", "artifact_type", "title", "version", "file_name", "file_path"}`

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/services/preview/test_artifact_enqueue.py`:

```python
"""Creating a cost_breakdown_xlsx artifact always queues a preview render.

Both producers go through one helper so a new call site cannot silently skip it.
"""
from app.services import cost_breakdown_service as cbs


def test_persist_creates_artifact_and_enqueues(monkeypatch, db):
    monkeypatch.setattr(cbs, "_upload_xlsx", lambda key, data: None)
    queued = []
    monkeypatch.setattr(cbs, "enqueue_preview_render", lambda aid: queued.append(aid))

    info = cbs.persist_xlsx_artifact(
        db,
        session_id=1,
        title="Cost Breakdown",
        fname="cost_tender_1.xlsx",
        data=b"fake",
        rows=[{"description": "x"}],
        summary={"total_amount": 100},
        metadata_extra={"tender_id": 3808},
    )

    assert info["artifact_type"] == "cost_breakdown_xlsx"
    assert info["file_path"] == "generated_docs/cost_tender_1.xlsx"
    assert queued == [info["artifact_id"]]


def test_enqueue_failure_does_not_break_artifact_creation(monkeypatch, db):
    """Redis being down must not fail a costing run."""
    monkeypatch.setattr(cbs, "_upload_xlsx", lambda key, data: None)

    def boom(aid):
        raise RuntimeError("redis down")

    monkeypatch.setattr(cbs, "enqueue_preview_render", boom)

    info = cbs.persist_xlsx_artifact(
        db, session_id=1, title="t", fname="f.xlsx",
        data=b"x", rows=[{"description": "x"}], summary={},
    )
    assert info["artifact_id"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/services/preview/test_artifact_enqueue.py -v
```
Expected: FAIL — `AttributeError: module 'app.services.cost_breakdown_service' has no attribute 'persist_xlsx_artifact'`.

- [ ] **Step 3: Add the shared helper**

In `app/services/cost_breakdown_service.py`, add above `regenerate_xlsx_artifact` (line 1962), plus the module-level import `from app.worker.preview_tasks import enqueue_preview_render`:

```python
def _upload_xlsx(key: str, data: bytes) -> None:
    from app.services.storage_service import get_storage_service

    get_storage_service().upload_file_sync(key, data, content_type=_XLSX_MIME)


def persist_xlsx_artifact(
    db: Session,
    *,
    session_id: int,
    title: str,
    fname: str,
    data: bytes,
    rows: list,
    summary: dict,
    structured_extra: Optional[dict] = None,
    metadata_extra: Optional[dict] = None,
) -> dict:
    """Store a rendered workbook and create its artifact, then queue the preview.

    The single place a `cost_breakdown_xlsx` artifact is born. Both producers
    (this module's `regenerate_xlsx_artifact` and the chat wrapper) call it, so
    the preview render can never be skipped by a new call site.
    """
    from app.services.artifact_service import create_artifact

    key = f"generated_docs/{fname}"
    _upload_xlsx(key, data)

    artifact = create_artifact(
        db=db,
        session_id=session_id,
        artifact_type="cost_breakdown_xlsx",
        title=title,
        content=json.dumps({"rows": rows, **summary}, default=str),
        structured_data={"rows": rows, **summary, **(structured_extra or {})},
        agent_key="costing_researcher",
        metadata={"file_name": fname, **(metadata_extra or {})},
    )
    artifact.file_path = key  # storage KEY (storage-service resolves local/R2)
    artifact.file_name = fname
    db.commit()

    # A preview is a nice-to-have; never let it fail the costing run.
    try:
        enqueue_preview_render(artifact.id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cost_breakdown] preview enqueue failed: {e}")

    return {
        "artifact_id": artifact.id,
        "artifact_type": "cost_breakdown_xlsx",
        "title": title,
        "version": artifact.version,
        "file_name": fname,
        "file_path": key,
    }
```

- [ ] **Step 4: Rewrite `regenerate_xlsx_artifact` to use it**

Replace lines 1996-2021 (from `key = f"generated_docs/{fname}"` through `return {...}`) with:

```python
    title = breakdown.title or f"Cost Breakdown — Tender #{breakdown.tender_id}"
    info = persist_xlsx_artifact(
        db,
        session_id=session_id,
        title=title,
        fname=fname,
        data=data,
        rows=rows,
        summary=summary,
        structured_extra={"cost_breakdown_id": breakdown.id, "version": breakdown.version},
        metadata_extra={"tender_id": breakdown.tender_id, "cost_breakdown_id": breakdown.id},
    )
    breakdown.artifact_id = info["artifact_id"]
    db.commit()
    return info
```

- [ ] **Step 5: Rewrite the chat-wrapper site to use it**

In `app/services/langchain/graphs/chat_agent_wrappers.py`, replace lines 3242-3268 (from `key = f"generated_docs/{fname}"` through `return {...}`) with:

```python
    from app.services.cost_breakdown_service import persist_xlsx_artifact

    return persist_xlsx_artifact(
        db,
        session_id=proposal_session_id,
        title=title,
        fname=fname,
        data=_data,
        rows=rows,
        summary=summary,
        metadata_extra={"tender_id": tender_id},
    )
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/services/preview/ tests/worker/test_preview_tasks.py -v
```
Expected: 26 passed.

- [ ] **Step 7: Run the full suite for regressions**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/ -q
```
Expected: no failures. Costing tests must still pass — the helper changed how artifacts are written.

- [ ] **Step 8: Commit**

```bash
git add drpl-backend/app/services/cost_breakdown_service.py \
        drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py \
        drpl-backend/tests/services/preview/test_artifact_enqueue.py
git commit -m "refactor(costing): one place creates xlsx artifacts and queues the preview"
```

---

## Task 8: Preview endpoint

**Files:**
- Modify: `drpl-backend/app/api/routes/command_center.py` (after the `download-url` route from Task 1)
- Test: `drpl-backend/tests/test_artifact_preview_route.py`

**Interfaces:**
- Consumes: manifest written by Task 6; `StorageService.get_presigned_url`
- Produces: `GET /api/command-center/artifacts/{id}/preview` → `{"status", "pdf_url", "page_count", "sheets", "error"}`

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_artifact_preview_route.py`:

```python
"""The preview endpoint always returns 200 with a status.

A missing preview is an expected state (old artifacts, a failed render), not an
error -- the UI degrades to its table view on anything that is not "ready".
"""
import pytest

from app.models.artifact import CommandCenterArtifact


def _artifact(db, preview=None):
    a = CommandCenterArtifact(
        session_id=1, artifact_type="cost_breakdown_xlsx",
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/test_artifact_preview_route.py -v
```
Expected: FAIL — 404 on all, route missing.

- [ ] **Step 3: Implement**

Add to `app/api/routes/command_center.py`, after `get_artifact_download_url`:

```python
@router.get("/artifacts/{artifact_id}/preview")
async def get_artifact_preview(
    artifact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manifest + presigned PDF URL for the rendered workbook preview.

    Always 200 with a status. `pending` (never rendered — including every
    artifact created before this feature) and `failed` are expected states; the
    UI falls back to its table view for anything that is not `ready`.
    """
    from app.services.artifact_service import get_artifact as do_get
    from app.services.storage_service import get_storage_service
    from app.core.config import get_settings

    artifact = do_get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    preview = (artifact.metadata_json or {}).get("preview") or {}
    status = preview.get("status") or "pending"
    body = {
        "status": status,
        "pdf_url": None,
        "page_count": preview.get("page_count", 0),
        "sheets": preview.get("sheets", []),
        "error": preview.get("error"),
    }

    pdf_key = preview.get("pdf_key")
    if status == "ready" and pdf_key:
        storage = get_storage_service()
        if getattr(storage, "_backend", "local") == "r2":
            body["pdf_url"] = await storage.get_presigned_url(
                pdf_key, expires_in=get_settings().presigned_download_ttl_seconds
            )
        else:
            body["status"] = "unavailable"
    return body
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/test_artifact_preview_route.py -v
```
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/api/routes/command_center.py drpl-backend/tests/test_artifact_preview_route.py
git commit -m "feat(preview): endpoint serving the manifest and a presigned PDF url"
```

---

## Task 9: Dockerfile + startup probe

**Files:**
- Modify: `drpl-backend/Dockerfile:6-17`
- Modify: `drpl-backend/app/main.py` (startup section)

**Interfaces:**
- Consumes: `soffice_available()` (Task 5)

- [ ] **Step 1: Add LibreOffice to the image**

In `drpl-backend/Dockerfile`, add `libreoffice-calc \` to the existing apt block, and extend the comment:

```dockerfile
# Install system dependencies for WeasyPrint (PDF generation), Tesseract (OCR),
# poppler-utils (pdf2image / pdftoppm — required for the Tesseract fallback
# in advanced_document_parser._ocr_pdf_page), and LibreOffice Calc (headless
# xlsx -> PDF for the cost-breakdown preview; worker-only, but web and worker
# share this image).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    libcairo2 \
    libharfbuzz0b \
    tesseract-ocr \
    poppler-utils \
    libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Add the startup probe**

In `app/main.py`, inside the existing startup sequence (after the seeders), add:

```python
    # Diagnose a missing LibreOffice at boot rather than as a mystery preview
    # failure hours later.
    try:
        from app.services.xlsx_preview_service import soffice_available, SOFFICE_BIN

        if soffice_available():
            logger.info(f"[startup] {SOFFICE_BIN} present — xlsx previews enabled")
        else:
            logger.warning(
                f"[startup] {SOFFICE_BIN} NOT found — xlsx previews will fail; "
                f"install libreoffice-calc in the image"
            )
    except Exception as e:
        logger.warning(f"[startup] soffice probe failed: {e}")
```

- [ ] **Step 3: Verify the image builds and soffice runs**

```bash
cd drpl-backend && docker build -t drpl-backend-preview .
docker run --rm drpl-backend-preview soffice --version
```
Expected: build succeeds; version string like `LibreOffice 7.4.x`.

- [ ] **Step 4: Commit**

```bash
git add drpl-backend/Dockerfile drpl-backend/app/main.py
git commit -m "build(preview): install libreoffice-calc and probe it at startup"
```

---

## Task 10: Frontend tab bar + pdf.js viewer

**Files:**
- Create: `drpl-frontend/src/components/command-center/XlsxPreview.tsx`
- Modify: `drpl-frontend/src/components/command-center/ArtifactPanel.tsx` (delete the inline `XlsxPreview` at lines 439-510; import the new one)
- Modify: `drpl-frontend/package.json` (add `pdfjs-dist`)

**Interfaces:**
- Consumes: `GET /api/command-center/artifacts/{id}/preview`
- Produces: `<XlsxPreview artifact={artifact} onDownload={() => void} />` — same props as the component it replaces, so `ArtifactPanel`'s three usages are unchanged.

- [ ] **Step 1: Install pdf.js**

```bash
cd drpl-frontend && npm install pdfjs-dist@^4.0.0
```

- [ ] **Step 2: Create the component**

Create `drpl-frontend/src/components/command-center/XlsxPreview.tsx`:

```tsx
import { useEffect, useRef, useState } from 'react';
import { Download, FileSpreadsheet, Loader2 } from 'lucide-react';
import * as pdfjsLib from 'pdfjs-dist';
// Bundle the worker locally — a CDN URL would break the app wherever
// third-party network access is blocked.
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

import { API_BASE } from '../../lib/api';
import type { Artifact } from '../../types/command-center';

pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorker;

interface PreviewManifest {
  status: 'ready' | 'pending' | 'failed' | 'unavailable';
  pdf_url: string | null;
  page_count: number;
  sheets: { name: string; start_page: number }[];
  error: string | null;
}

/** Faithful preview of the generated workbook: one tab per worksheet over a
 *  LibreOffice-rendered PDF. Falls back to a plain table when no render exists
 *  (artifacts created before this feature, or a failed render). */
export function XlsxPreview({
  artifact,
  onDownload,
}: {
  artifact: Artifact;
  onDownload: () => void;
}) {
  const [manifest, setManifest] = useState<PreviewManifest | null>(null);
  const [activeSheet, setActiveSheet] = useState(0);
  const [loading, setLoading] = useState(true);
  const containerRef = useRef<HTMLDivElement>(null);
  const docRef = useRef<any>(null);

  const sd: any = (artifact as any).structured_data || {};
  const rows: any[] = Array.isArray(sd.rows) ? sd.rows : [];

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const token = localStorage.getItem('drpl_token');
        const res = await fetch(
          `${API_BASE}/api/command-center/artifacts/${artifact.id}/preview`,
          { headers: { Authorization: `Bearer ${token}` } },
        );
        const body: PreviewManifest = await res.json();
        if (!cancelled) setManifest(body);
      } catch {
        if (!cancelled) setManifest(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifact.id]);

  // Render the pages of the active sheet.
  useEffect(() => {
    if (manifest?.status !== 'ready' || !manifest.pdf_url) return;
    let cancelled = false;

    (async () => {
      const container = containerRef.current;
      if (!container) return;
      if (!docRef.current) {
        docRef.current = await pdfjsLib.getDocument(manifest.pdf_url!).promise;
      }
      if (cancelled) return;

      const sheets = manifest.sheets;
      const start = sheets[activeSheet]?.start_page ?? 1;
      const end =
        activeSheet + 1 < sheets.length
          ? sheets[activeSheet + 1].start_page - 1
          : manifest.page_count;

      container.innerHTML = '';
      for (let p = start; p <= end; p++) {
        const page = await docRef.current.getPage(p);
        if (cancelled) return;
        const viewport = page.getViewport({ scale: 1.4 });
        const canvas = document.createElement('canvas');
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        canvas.className = 'w-full h-auto border border-border rounded mb-3 bg-white';
        container.appendChild(canvas);
        await page.render({ canvasContext: canvas.getContext('2d')!, viewport }).promise;
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [manifest, activeSheet]);

  const header = (
    <div className="rounded-xl border border-emerald-200 dark:border-emerald-500/20 bg-emerald-50/50 dark:bg-emerald-500/10 p-4 flex items-center gap-3">
      <div className="w-10 h-10 rounded-lg bg-card border border-emerald-200 dark:border-emerald-500/20 flex items-center justify-center">
        <FileSpreadsheet size={18} className="text-emerald-600 dark:text-emerald-400" />
      </div>
      <div className="flex-1 min-w-0">
        <p className="font-semibold text-foreground truncate">{artifact.title}</p>
        <p className="text-xs text-muted-foreground">
          {manifest?.status === 'ready'
            ? `${manifest.sheets.length} sheet${manifest.sheets.length === 1 ? '' : 's'} · ${manifest.page_count} pages`
            : `${rows.length} line items`}
        </p>
      </div>
      <button
        onClick={onDownload}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 text-white text-xs font-semibold hover:bg-emerald-700 transition-colors"
      >
        <Download size={13} />
        Download .xlsx
      </button>
    </div>
  );

  if (loading) {
    return (
      <div className="space-y-4">
        {header}
        <div className="flex items-center gap-2 text-sm text-muted-foreground p-6 justify-center">
          <Loader2 size={16} className="animate-spin" />
          Loading preview…
        </div>
      </div>
    );
  }

  if (manifest?.status === 'ready' && manifest.pdf_url) {
    return (
      <div className="space-y-3">
        {header}
        <div className="flex gap-1 overflow-x-auto border-b border-border pb-px">
          {manifest.sheets.map((s, i) => (
            <button
              key={s.name}
              onClick={() => setActiveSheet(i)}
              className={`px-3 py-1.5 text-xs font-medium whitespace-nowrap rounded-t-lg border border-b-0 transition-colors ${
                i === activeSheet
                  ? 'bg-card border-border text-foreground'
                  : 'bg-muted/40 border-transparent text-muted-foreground hover:text-foreground'
              }`}
            >
              {s.name}
            </button>
          ))}
        </div>
        <div ref={containerRef} className="max-h-[70vh] overflow-y-auto" />
      </div>
    );
  }

  // Fallback: no render available. Keep the previous table so nothing regresses.
  return (
    <div className="space-y-4">
      {header}
      {manifest?.status === 'pending' && (
        <p className="text-xs text-muted-foreground px-1">
          Full spreadsheet preview is still being generated. Showing the line items below.
        </p>
      )}
      {manifest?.status === 'failed' && (
        <p className="text-xs text-amber-600 dark:text-amber-500 px-1">
          Full spreadsheet preview could not be generated. Showing the line items below —
          the downloaded file is unaffected.
        </p>
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="min-w-full text-xs">
            <thead className="bg-muted/40 text-muted-foreground">
              <tr>
                <th className="px-2 py-2 text-left font-semibold">Description</th>
                <th className="px-2 py-2 text-left font-semibold">Category</th>
                <th className="px-2 py-2 text-right font-semibold">Qty</th>
                <th className="px-2 py-2 text-left font-semibold">Unit</th>
                <th className="px-2 py-2 text-right font-semibold">Rate (₹)</th>
                <th className="px-2 py-2 text-right font-semibold">Amount (₹)</th>
                <th className="px-2 py-2 text-right font-semibold">GST %</th>
                <th className="px-2 py-2 text-right font-semibold">Total (₹)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-t border-border">
                  <td className="px-2 py-2 align-top">{r.description}</td>
                  <td className="px-2 py-2 align-top">{r.category}</td>
                  <td className="px-2 py-2 text-right align-top">{r.qty ?? ''}</td>
                  <td className="px-2 py-2 align-top">{r.unit}</td>
                  <td className="px-2 py-2 text-right align-top">{r.rate ?? ''}</td>
                  <td className="px-2 py-2 text-right align-top">{r.amount ?? ''}</td>
                  <td className="px-2 py-2 text-right align-top">{r.gst_pct ?? ''}</td>
                  <td className="px-2 py-2 text-right align-top">{r.total ?? ''}</td>
                </tr>
              ))}
              {typeof sd.total_amount === 'number' && (
                <tr className="border-t-2 border-border bg-muted/40 font-semibold">
                  <td className="px-2 py-2" colSpan={5}>TOTAL</td>
                  <td className="px-2 py-2 text-right">{sd.total_amount.toLocaleString('en-IN')}</td>
                  <td />
                  <td className="px-2 py-2 text-right">
                    {typeof sd.total_with_gst === 'number' ? sd.total_with_gst.toLocaleString('en-IN') : ''}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Wire it into ArtifactPanel**

In `ArtifactPanel.tsx`: delete the inline `function XlsxPreview(...)` (lines 439-510) and add near the other imports:

```tsx
import { XlsxPreview } from './XlsxPreview';
```

(`API_BASE` is already exported from `src/lib/api.ts:7` and imported by
`ArtifactPanel.tsx:9` — verified. No change needed there.)

- [ ] **Step 4: Typecheck and build**

```bash
cd drpl-frontend && npm run build
```
Expected: `tsc -b` clean, Vite build succeeds. If the worker import errors, confirm the `pdfjs-dist` v4 path is `pdfjs-dist/build/pdf.worker.min.mjs`.

- [ ] **Step 5: Commit**

```bash
git add drpl-frontend/src/components/command-center/XlsxPreview.tsx \
        drpl-frontend/src/components/command-center/ArtifactPanel.tsx \
        drpl-frontend/package.json drpl-frontend/package-lock.json
git commit -m "feat(preview): render the real workbook with a tab per worksheet"
```

---

## Task 11: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Full backend suite**

```bash
cd drpl-backend && .venv/Scripts/python.exe -m pytest tests/ -q
```
Expected: no failures.

- [ ] **Step 2: Render a real workbook through the actual job**

```bash
cd drpl-backend && docker build -t drpl-backend-preview .
docker run --rm -v "$PWD:/app" -w /app drpl-backend-preview \
  python -c "
from app.services.xlsx_print_setup import apply_print_setup
from app.services.xlsx_preview_service import convert_to_pdf, build_manifest
import fitz, tempfile, os
tmp = tempfile.mkdtemp()
names = apply_print_setup('tests/fixtures/spike_workbook.xlsx', f'{tmp}/fitted.xlsx')
pdf = convert_to_pdf(f'{tmp}/fitted.xlsx', tmp)
d = fitz.open(pdf)
print(build_manifest(names, d.get_toc(), d.page_count))
txt = '\n'.join(d.load_page(i).get_text() for i in range(d.page_count))
assert 'Schedule ELECTRICAL AND PNEUMATIC SPARES FOR DETC' in txt, 'column clipped'
assert '#REF!' not in txt, 'formula broke'
print('OK')
"
```

`drpl-backend/tests/fixtures/spike_workbook.xlsx` is already committed — the real
workbook rendered from prod breakdown 76 during the design spike (3 sheets, 375
rows, 54 KB). If it is ever lost, regenerate it by calling
`cost_breakdown_service.render_breakdown_xlsx_bytes(db, breakdown)` on any
breakdown with several schedules and writing the returned bytes to that path.

Expected: a manifest with one entry per sheet, and `OK`.

The two assertions are the regression guards for the defect the spike caught:
the first fails if the print-setup pass stops working (columns clipped off the
page), the second if per-sheet isolation is ever reintroduced (cross-sheet
formulas would break to `#REF!`).

- [ ] **Step 3: Manual check in the app**

Run a costing on a tender, then open the Artifacts panel:
1. The tab bar shows every worksheet.
2. Each tab renders the styled sheet, with all financial columns present.
3. "Download .xlsx" saves instantly (network tab shows the request going to R2, not the backend).

- [ ] **Step 4: Confirm nothing is left uncommitted**

```bash
git status --short
```
Expected: clean tree. The fixture was committed alongside this plan.

---

## Self-Review Notes

**Spec coverage:** §1 download → Tasks 1-2. §2.1 storage layout → Task 6 (`preview_key`). §2.2 manifest → Tasks 4, 6. §2.3 render job incl. print-setup → Tasks 3, 5, 6. §2.4 enqueue → Task 7. §2.5 endpoint → Task 8. §2.6 frontend → Task 10. §3 infrastructure → Task 9. §4 testing → tests in each task plus Task 11.

**Deliberate deviation from the spec's test table:** "Source workbook untouched" is covered in Task 3 (`test_source_workbook_is_never_modified`) rather than as a download test, since that is where the copy is made.

**Not implemented (spec non-goals):** backfilling previews for the 37 existing artifacts; splitting the Dockerfile so only the worker carries LibreOffice.
