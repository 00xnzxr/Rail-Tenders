# Cost-Breakdown XLSX — Fast Download & Faithful Multi-Tab Preview

**Date:** 2026-08-02
**Status:** Approved (design), pending implementation plan
**Gate:** RESOLVED — LibreOffice spike run 2026-08-02, results in "Spike results"

## Problem

Two user-facing complaints about the cost-breakdown Excel produced at the end of
a costing run:

1. **Download is slow and sometimes fails.** Clicking "Download .xlsx" in the
   Command Center Artifacts panel takes a long time and intermittently errors.
2. **The preview is not the spreadsheet.** The Artifacts panel renders a single
   flat HTML table of `structured_data.rows`. The real workbook has multiple
   tabs, merged header blocks, number formats and a Reconciliation section —
   none of which the user can see without downloading the file.

## Goal

Downloads come straight from R2 without the backend touching the bytes, and the
Artifacts panel shows the actual rendered workbook with a tab per worksheet.

## Decisions (agreed with user)

| Question | Decision |
|---|---|
| Production storage backend | **Cloudflare R2** (confirmed) |
| Preview fidelity | **Pixel-perfect**, not an HTML approximation |
| Render route | **LibreOffice → PDF**, rendered in-browser with pdf.js |
| Render timing | **Eagerly**, on the RQ worker, right after costing |
| Missing/failed render | Degrade to today's table + download button |

## Non-goals

- Interactive spreadsheet editing in the preview. It is read-only; editing
  already exists in `CostBreakdownEditor`.
- Backfilling previews for the 37 existing `cost_breakdown_xlsx` artifacts.
  They fall back to the table view. A backfill script is optional follow-up.
- Splitting the shared Dockerfile so only the worker carries LibreOffice.
  Called out as a known cost, deliberately deferred.

---

## Evidence

Gathered from the production database and the current code before designing.

- All 37 `cost_breakdown_xlsx` artifacts have a populated `file_path`. The
  regenerate-on-miss branch is therefore **not** the common path; the slowness
  is in the normal serve path.
- The workbook is genuinely multi-tab. For prod breakdown 76 (tender 3808, 375
  rows) it renders as `1. Summary`, `2. Cost Assumptions`, `3. All Schedules`
  (three tabs, 54 KB). Deployments with `costing.nit_single_sheet=false` add one
  sheet per schedule.
- The workbook contains **cross-sheet formulas and named ranges**
  (`=SUM(...)`, `=B12*NIT_GST_PCT/100`, Summary cells referencing schedule
  sheets — `xlsx_generator_tool.py:767-812`). openpyxl writes formulas with **no
  cached values**.
- It contains **no charts or images**.

### Spike results (2026-08-02)

Run in the backend base image (`python:3.11-slim-bookworm` +
`libreoffice-calc`) against the real workbook rendered from prod breakdown 76.

| Question | Result |
|---|---|
| Does `soffice --convert-to pdf` succeed? | **Yes**, rc=0, **2.2s** for 375 rows |
| Does the PDF outline name each sheet? | **Yes** — 3 entries, exactly the 3 sheet names, with start pages 1 / 4 / 13 |
| Are formulas computed? | **Yes** — totals resolved; zero `#REF!` / `#NAME?` / `#VALUE!` / `#DIV/0!` |

The decision gate is therefore **resolved in favour of the single-pass design**.
The value-baking fallback is not needed and is dropped from the spec.

The spike also surfaced a defect that would otherwise have shipped: **the raw
conversion loses content off the right edge.** The generated workbook sets no
print setup, so LibreOffice clips at the page boundary. On the Summary sheet
this dropped four entire columns — Estimated Cost, Gross Margin, GM %, and
Reconciliation's Stated Total / Delta / Status — and truncated the Key
Observations narrative. Excel overflows such text across empty cells on screen,
which is why the defect is invisible until the workbook is printed.

A print-setup pass fixes it (verified, see §2.3 step 3): all previously clipped
strings render in full, and the document shrinks from **52 pages to 9**.

Two consequences drive the design:

1. A client-side JS spreadsheet renderer (SheetJS and similar) would show every
   total as **blank**, because it can only read cached values that openpyxl
   never wrote. Server-side LibreOffice is the only route that computes them.
2. Rendering each sheet separately is invalid: deleting sheets to isolate one
   turns the cross-sheet references into `#REF!`. The whole workbook must be
   converted in a single pass.

---

## 1. Download — stop proxying the bytes

### Current behaviour

`GET /api/command-center/artifacts/{id}/download`
(`app/api/routes/command_center.py:1481`) does, per request:

1. `storage.file_exists_sync(key)` — a HEAD round trip to R2.
2. `storage.download_file_sync(key)` — a full GET, whole file into backend RAM.
3. `StreamingResponse(io.BytesIO(data))` — re-streams to the browser.

Four compounding problems:

| # | Problem | Effect |
|---|---|---|
| 1 | Every byte crosses Railway twice (R2 → backend → browser) | Slow, scales with file size |
| 2 | Endpoint is a sync `def` doing blocking network IO | Holds one of FastAPI's 40 threadpool slots for the whole fetch; concurrent downloads starve the pool — the most likely cause of intermittent failure |
| 3 | `StreamingResponse` over `BytesIO` sets no `Content-Length` | Indeterminate browser progress; intermediate proxies may buffer |
| 4 | Frontend `fetch` → `res.blob()` buffers the file again in the JS heap | Extra memory, no timeout, all failures collapse to one generic alert |

### Design

Add a sibling endpoint that returns a URL instead of bytes:

```
GET /api/command-center/artifacts/{id}/download-url
  -> 200 {"url": "<presigned R2 URL>", "file_name": "cost_tender_3808_....xlsx"}
```

- Uses the existing `StorageService.get_presigned_url` (`storage_service.py:192`).
- The presigned URL carries `ResponseContentDisposition: attachment;
  filename="..."` so the browser saves under the correct name.
- TTL: a new `presigned_download_ttl_seconds` setting, default **300s**. The
  existing `presigned_url_ttl_seconds` (3600) is for longer-lived embeds; a
  download link does not need an hour.
- The cheap HEAD (`file_exists_sync`) is retained, purely to decide whether to
  fall through to the existing regenerate-from-`CostBreakdown` branch. What is
  removed is the full GET and the re-stream.
- Declared `async def`, so the blocking S3 calls go through
  `asyncio.to_thread` rather than occupying a threadpool slot for the transfer.

On the **local** storage backend `get_presigned_url` returns a relative path, so
the endpoint reports `{"url": null}` and the frontend falls back to the existing
`/download` route. Local dev keeps working.

`GET .../download` is **kept as-is** for backward compatibility — other callers
and any bookmarked links keep working.

### Frontend

`downloadArtifactFile` (`ArtifactPanel.tsx:117`) calls `download-url`, then:

```ts
const a = document.createElement('a');
a.href = url;           // presigned R2 URL — bytes never enter the JS heap
a.download = fileName;
a.click();
```

Falls back to the current blob path when `url` is null. Failure messaging
distinguishes "could not get a link" from "the file is missing".

---

## 2. Preview — render the real workbook

### 2.1 Storage layout

Rendered PDFs live alongside the workbook in R2:

```
previews/artifact_{artifact_id}/v{artifact_version}.pdf
```

Keying on artifact version means a regenerated workbook (the
`regenerate-xlsx` route bumps the version) renders to a fresh key rather than
serving a stale render.

### 2.2 Manifest

Persisted on the artifact's `metadata_json` under a `preview` key:

```json
{
  "preview": {
    "status": "ready",
    "pdf_key": "previews/artifact_279/v1.pdf",
    "page_count": 14,
    "sheets": [
      {"name": "1. Summary",          "start_page": 1},
      {"name": "2. Cost Assumptions", "start_page": 3},
      {"name": "3. All Schedules",    "start_page": 5}
    ],
    "rendered_at": "2026-08-02T10:00:00Z",
    "error": null
  }
}
```

`status` is one of `pending | ready | failed`. `error` carries a short reason
when `failed`, so the UI and the logs agree on why a preview is missing.

### 2.3 Render job

New RQ job `render_xlsx_preview(artifact_id)` in `app/worker/`:

1. Download the `.xlsx` from R2 to a temp dir.
2. Enumerate sheet names with openpyxl — this is the authoritative tab order.
3. **Print-setup pass** (openpyxl, on a copy — the distributed `.xlsx` is never
   modified). Per worksheet:
   - column widths derived from the longest non-formula cell text, clamped to
     `[8, 60]` characters; cells longer than the clamp get `wrap_text`;
   - `page_setup.fitToWidth = 1`, `fitToHeight = 0`,
     `sheet_properties.pageSetUpPr.fitToPage = True`;
   - landscape orientation; margins 0.3" left/right, 0.4" top/bottom.

   Without this the render silently loses columns off the right edge (see
   "Spike results"). This step is **not** cosmetic — it is what makes the
   preview a faithful view of the data.
4. **One** conversion of the whole workbook:
   `soffice --headless --norestore --convert-to pdf:calc_pdf_Export
   --outdir <tmp> <file>`. A single pass preserves cross-sheet formulas and
   named ranges, and LibreOffice computes all totals.
5. Read the PDF outline with PyMuPDF (`fitz`, already a dependency) —
   `doc.get_toc()` — to map each sheet name to its start page.
6. Upload the PDF to R2; write the manifest; commit.

`soffice` is invoked with an explicit per-job `-env:UserInstallation` temp
profile so concurrent worker replicas cannot collide on a shared profile lock,
and with a subprocess timeout so a wedged conversion fails the job rather than
hanging a worker.

Run inside `run_id_scope` so log lines are correlatable, per the repo's
run-ID logging rule.

Failure is non-fatal: `status="failed"` with the reason, and the UI degrades.
The costing run itself is never blocked by a preview failure.

**Decision gate: RESOLVED.** The spike confirmed LibreOffice Calc emits one PDF
bookmark per worksheet, formulas resolve, and a whole-workbook conversion costs
~2s. The single-pass design stands; the value-baking fallback is dropped.

There remains a **runtime** fallback for an individual workbook
whose outline comes back empty or shorter than the sheet list even though the
mechanism works in general: emit a manifest with a single tab spanning all
pages. The preview is then less navigable but still correct and pixel-faithful.
The two are not alternatives — the design-level fallback picks the rendering
strategy once, the runtime fallback handles one odd workbook.

### 2.4 Enqueue point

The xlsx artifact is created in two places:

- `cost_breakdown_service.py:2003`
- `chat_agent_wrappers.py:3250`

Both are refactored to call one shared helper that creates the artifact **and**
enqueues the render, so a new call site cannot silently skip the preview.

### 2.5 Endpoint

```
GET /api/command-center/artifacts/{id}/preview
  -> 200 {"status": "ready", "pdf_url": "<presigned>", "page_count": 14,
          "sheets": [...]}
  -> 200 {"status": "pending"|"failed", "pdf_url": null, "error": "..."}
```

Always 200 with a status — a missing preview is an expected state, not an error.

### 2.6 Frontend

`XlsxPreview` (`ArtifactPanel.tsx:439`) becomes:

- a **tab bar** built from `sheets`, one button per worksheet;
- a **pdf.js canvas** (new dependency `pdfjs-dist`) showing the PDF, scrolling
  to `start_page` when a tab is clicked;
- the existing download button, unchanged in position;
- **fallback**: when `status != "ready"`, render today's table exactly as now,
  with a quiet note for `pending` / `failed`. No regression for the 37 existing
  artifacts.

The pdf.js worker is bundled locally (Vite `?url` import), not fetched from a
CDN — the app must keep working without third-party network access.

---

## 3. Infrastructure

`drpl-backend/Dockerfile` gains:

```dockerfile
libreoffice-calc \
```

to the existing `apt-get install --no-install-recommends` block.

**Known cost:** web and worker share this Dockerfile, so ~300 MB lands in the
web image too, though only the worker converts. This slows deploys and inflates
the web image. Accepted for now; splitting the Dockerfile is a separate change.

A startup probe logs whether `soffice` is present, so a missing binary is
diagnosed at boot rather than as a mysterious render failure.

---

## 4. Testing

| Area | Test |
|---|---|
| `download-url` on R2 | Returns a presigned URL with the right `ResponseContentDisposition`; the bytes are never read by the endpoint |
| `download-url` on local backend | Returns `url: null` so the frontend falls back |
| Missing object | Still triggers the regenerate-from-`CostBreakdown` branch |
| Manifest parsing | Outline entries → `sheets[]` mapping, including a sheet whose name Excel truncated to 31 chars |
| No outline | Falls back to a single tab covering all pages rather than crashing |
| Print-setup pass | A sheet with a 200-char cell and a 12-column table renders every column; asserts the widest column is clamped and `fitToPage` is set. Guards the regression the spike caught |
| Source workbook untouched | The pass writes to a copy; the bytes served by `download-url` are byte-identical to what costing produced |
| Job failure | Sets `status="failed"` with a reason; the costing run still completes |
| Enqueue coverage | Both artifact-creation call sites enqueue exactly one render |
| Frontend fallback | `status != "ready"` renders the legacy table |

The LibreOffice conversion itself is exercised by the spike and by a single
integration test marked to skip when `soffice` is absent, so the suite stays
green on developer machines without LibreOffice.

---

## 4a. DEPLOYMENT BLOCKER — R2 bucket CORS

**The preview will not work in production until the R2 bucket has a CORS policy.
This is a Cloudflare configuration change, not a code change.**

Why it is needed now and was not before: the download path points an `<a href>`
at the presigned URL, which is a navigation and needs no CORS. The preview path
is different — pdf.js fetches the PDF over XHR and then issues HTTP **Range**
requests for anything over ~128 KB. That is a cross-origin request from the
Vercel frontend origin to `<account>.r2.cloudflarestorage.com`.

`StorageService.get_presigned_url` had **zero callers anywhere in the codebase**
before this branch, so no browser has ever fetched R2 directly from this
application and no CORS rule has ever been required.

This cannot be caught by the test suite or by the containerised end-to-end run,
both of which use the local storage backend. It fails only in production, and it
fails silently from the backend's point of view.

Required rule on the bucket:

| Field | Value |
|---|---|
| `AllowedOrigins` | the Vercel frontend origin(s) |
| `AllowedMethods` | `GET`, `HEAD` |
| `AllowedHeaders` | `Range` |
| `ExposeHeaders` | `Content-Range`, `Content-Length`, `Accept-Ranges` |
| `MaxAgeSeconds` | `3600` |

Without `Range` in `AllowedHeaders` and `Content-Range` in `ExposeHeaders`,
pdf.js cannot stream and will fail on any non-trivial PDF even if the initial
request succeeds.

Until this is applied, the UI degrades to the legacy line-items table (the
`renderError` fallback) rather than showing a blank pane — so the branch is safe
to merge, but the preview stays invisible to users.

## 5. Rollout

0. **Apply the R2 CORS rule above** (see 4a). The preview cannot work without it.
1. Merge the download fix first — it is independent, small, and delivers the
   most-complained-about improvement immediately.
2. ~~Run the spike; settle the decision gate in 2.3.~~ **Done 2026-08-02.**
3. Ship the render job with the Dockerfile change. Previews appear on new
   costing runs only.
4. Optionally backfill the 37 existing artifacts with a script that reuses the
   same job.
