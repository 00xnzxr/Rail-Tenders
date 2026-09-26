# NIT Auto-Fetch + Tender Field Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the analyzer reliably get the NIT + sibling documents (via the extension's existing authenticated download) and persist the extracted bid value, EMD, closing date, full title, and scope back to the Tender row.

**Architecture:** Gap A is ~90% pre-built — the extension already downloads+uploads all tender-page docs (`processDocumentHunt` → `downloadAndUploadDocument` → `POST /api/extension/documents`). We only thread `source_url` through and store it (for dedup + NIT-authoritative selection). Gap B is new: two defensive parsers + an enrichment mapping that reads the analyzer's existing per-doc `commercials`/`key_facts` and writes the five fields to the Tender, lifting the current `command_center`-only guard.

**Tech Stack:** Python 3.14 / FastAPI / SQLAlchemy; TypeScript / Vite (extension); pytest + vitest. Backend venv `drpl-backend/venv/`.

## Global Constraints

- Backend pure helpers (`_parse_indian_currency`, `_parse_tender_date`) are deterministic, no IO, return `None` on ambiguity.
- Write-back rules: **title + description → NIT wins (overwrite)** (unless the NIT value is corrupt/empty per `is_corrupt_title`); **estimated_value + emd_amount + closing_date → fill-if-empty only**; unparseable numeric/date → leave column unchanged + log the verbatim string.
- Per-doc analyzer blob (`DocumentExtractionResult.summary_json`, `extraction_type="per_doc_summary"`) is the full dict with nested `key_facts` AND top-level `commercials` (schema at `document_analysis_agent.py:623/630`). `commercials` holds `advertised_value`, `emd`, `closing_date_raw`, `name_of_work`. `key_facts` holds `scope_summary`, `tender_reference`, `issuing_authority`, `estimated_value`. Each blob's row has `document_id` → `TenderDocument`.
- Prefer values from the NIT-flagged doc (`TenderDocument.document_type == 'nit'`) when multiple docs disagree.
- Backend cmds from `drpl-backend/`, tests via `venv/Scripts/python.exe -m pytest`. Do NOT `import app.main` (triggers live-prod schema self-heal). Extension cmds from `drpl-extension/`, tests via `npm test`.
- Work on branch `feat/nit-fetch-enrichment` (create off main).

---

### Task 1: Defensive currency + date parsers

**Files:**
- Create: `drpl-backend/app/services/tender_value_parsers.py`
- Test: `drpl-backend/tests/services/test_tender_value_parsers.py`

**Interfaces:**
- Produces:
  - `parse_indian_currency(s: str | None) -> float | None`
  - `parse_tender_date(s: str | None) -> datetime | None` (naive or tz-aware datetime; return value is stored into a `DateTime(timezone=True)` column so tz-aware preferred, naive acceptable)

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/services/test_tender_value_parsers.py`:

```python
from datetime import datetime
from app.services.tender_value_parsers import parse_indian_currency, parse_tender_date


def test_currency_plain_with_symbol_and_commas():
    assert parse_indian_currency("₹ 1,12,55,870.40") == 11255870.40

def test_currency_rs_with_trailing_slash():
    assert parse_indian_currency("Rs. 2,25,000/-") == 225000.0

def test_currency_lakh_unit():
    assert parse_indian_currency("INR 45.6 Lakh") == 4560000.0

def test_currency_crore_unit():
    assert parse_indian_currency("2.5 Cr") == 25000000.0

def test_currency_bare_number():
    assert parse_indian_currency("349603.68") == 349603.68

def test_currency_none_and_garbage():
    assert parse_indian_currency(None) is None
    assert parse_indian_currency("") is None
    assert parse_indian_currency("as per tender") is None
    assert parse_indian_currency("N/A") is None

def test_date_slash_with_time_and_hrs():
    d = parse_tender_date("21/07/2026 15:00 hrs")
    assert d is not None and d.year == 2026 and d.month == 7 and d.day == 21 and d.hour == 15

def test_date_dd_mon_yyyy():
    d = parse_tender_date("21-Jul-2026")
    assert d is not None and d.year == 2026 and d.month == 7 and d.day == 21

def test_date_iso():
    d = parse_tender_date("2026-07-21T15:00:00")
    assert d is not None and d.year == 2026 and d.month == 7 and d.day == 21

def test_date_none_and_garbage():
    assert parse_tender_date(None) is None
    assert parse_tender_date("") is None
    assert parse_tender_date("refer portal") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_tender_value_parsers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.tender_value_parsers'`.

- [ ] **Step 3: Write the implementation**

Create `drpl-backend/app/services/tender_value_parsers.py`:

```python
"""Deterministic, defensive parsers for tender commercial strings.

The analyzer captures bid value / EMD / closing date as verbatim strings.
These turn them into typed values for the Tender columns, returning None on
any ambiguity so a wrong value is never written.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_LAKH = 100_000.0
_CRORE = 10_000_000.0


def parse_indian_currency(s: Optional[str]) -> Optional[float]:
    """Parse an Indian-format currency string to a float amount.

    Handles: '₹ 1,12,55,870.40', 'Rs. 2,25,000/-', 'INR 45.6 Lakh', '2.5 Cr',
    bare numbers. Returns None for empty/None/non-numeric input.
    """
    if not s or not isinstance(s, str):
        return None
    text = s.strip().lower()
    if not text:
        return None
    # Detect a lakh/crore multiplier (word or common abbreviations).
    mult = 1.0
    if re.search(r"\bcr\b|\bcrore?s?\b", text):
        mult = _CRORE
    elif re.search(r"\blac?s?\b|\blakh?s?\b", text):
        mult = _LAKH
    # Strip currency words/symbols and a trailing '/-'.
    text = text.replace("/-", " ")
    text = re.sub(r"(₹|rs\.?|inr|/=|/\-)", " ", text)
    # Remove Indian digit-group commas.
    text = text.replace(",", "")
    # Grab the first decimal/integer number token.
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        value = float(m.group(0)) * mult
    except ValueError:
        return None
    # Guard against a lone bogus 0 from e.g. "0" placeholders? Keep 0 as valid.
    return value


# Ordered list of (strptime format, whether it carries time).
_DATE_FORMATS = [
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%d-%b-%Y %H:%M",
    "%d-%b-%Y",
    "%d %b %Y %H:%M",
    "%d %b %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def parse_tender_date(s: Optional[str]) -> Optional[datetime]:
    """Parse a tender date string to a datetime. Returns None on ambiguity.

    Handles '21/07/2026 15:00 hrs', '21-Jul-2026', ISO forms, etc. Strips a
    trailing 'hrs'/'IST' and collapses whitespace before matching.
    """
    if not s or not isinstance(s, str):
        return None
    text = s.strip()
    if not text:
        return None
    # Normalize: drop trailing 'hrs', 'ist', collapse whitespace.
    text = re.sub(r"\b(hrs?|ist)\b\.?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip().strip(",")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_tender_value_parsers.py -v`
Expected: PASS (12 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/tender_value_parsers.py tests/services/test_tender_value_parsers.py
git commit -m "feat(analysis): defensive Indian currency + tender date parsers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Enrichment reads commercials + NIT-doc preference; writes 5 fields for all tenders

**Files:**
- Modify: `drpl-backend/app/services/tender_enrichment_service.py` (add commercials collection + primary-blob selection + 5-field mapping; lift the command_center guard)
- Test: `drpl-backend/tests/services/test_tender_enrichment_commercials.py` (create)

**Interfaces:**
- Consumes: `parse_indian_currency`, `parse_tender_date` from Task 1.
- Produces (new/changed in `tender_enrichment_service.py`):
  - `_pick_primary_blob(db, tender_id) -> tuple[dict, Optional[str]]` — returns the full winning `summary_json` blob (not just its `key_facts`) and its doc_name, preferring the NIT-typed doc, else the richest `key_facts` (reuse `_score_facts_richness`).
  - Extended `enrich_tender_from_analysis(db, tender_id)` behavior: runs for ALL tenders; writes `title`/`description` (overwrite), `estimated_value`/`emd_amount`/`closing_date` (fill-if-empty).

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/services/test_tender_enrichment_commercials.py`. This is a unit test of the mapping logic via a small seam — the mapping is exercised through a helper `_apply_commercials_to_tender` that Task 2 adds (pure, takes a Tender-like object + a commercials dict). Test that helper directly:

```python
from types import SimpleNamespace
from datetime import datetime
from app.services.tender_enrichment_service import _apply_commercials_to_tender


def _tender(**kw):
    base = dict(title="ireps #123", description=None, estimated_value=None,
                emd_amount=None, closing_date=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_fills_empty_numerics_and_date():
    t = _tender()
    commercials = {"advertised_value": "₹ 1,12,55,870.40", "emd": "Rs. 2,25,000/-",
                   "closing_date_raw": "21/07/2026 15:00 hrs", "name_of_work": None}
    changes = _apply_commercials_to_tender(t, commercials)
    assert t.estimated_value == 11255870.40
    assert t.emd_amount == 225000.0
    assert isinstance(t.closing_date, datetime) and t.closing_date.year == 2026
    assert set(changes) >= {"estimated_value", "emd_amount", "closing_date"}


def test_does_not_overwrite_existing_numerics():
    t = _tender(estimated_value=999.0, emd_amount=1.0)
    commercials = {"advertised_value": "₹ 5,00,000", "emd": "Rs. 10,000"}
    _apply_commercials_to_tender(t, commercials)
    assert t.estimated_value == 999.0  # unchanged — fill-if-empty
    assert t.emd_amount == 1.0


def test_unparseable_numeric_is_skipped_not_written():
    t = _tender()
    commercials = {"advertised_value": "as per tender", "emd": "refer NIT"}
    changes = _apply_commercials_to_tender(t, commercials)
    assert t.estimated_value is None
    assert t.emd_amount is None
    assert "estimated_value" not in changes


def test_none_commercials_no_change():
    t = _tender()
    assert _apply_commercials_to_tender(t, None) == {}
    assert _apply_commercials_to_tender(t, {}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_tender_enrichment_commercials.py -v`
Expected: FAIL — `ImportError: cannot import name '_apply_commercials_to_tender'`.

- [ ] **Step 3: Add the pure mapping helper**

In `drpl-backend/app/services/tender_enrichment_service.py`, add near the top (after imports):

```python
from app.services.tender_value_parsers import parse_indian_currency, parse_tender_date


def _apply_commercials_to_tender(tender, commercials: Optional[dict]) -> dict:
    """Fill-if-empty numeric/date fields on a Tender from a per-doc
    `commercials` block. Pure: mutates the passed object, returns a dict of
    changes. Never overwrites a non-empty value; skips unparseable strings.
    """
    changes: dict = {}
    if not isinstance(commercials, dict) or not commercials:
        return changes

    if getattr(tender, "estimated_value", None) in (None, 0, 0.0):
        val = parse_indian_currency(commercials.get("advertised_value"))
        if val is not None:
            tender.estimated_value = val
            changes["estimated_value"] = val
        elif commercials.get("advertised_value"):
            logger.info(
                f"[enrich] advertised_value unparseable, skipped: "
                f"{commercials.get('advertised_value')!r}"
            )

    if getattr(tender, "emd_amount", None) in (None, 0, 0.0):
        emd = parse_indian_currency(commercials.get("emd"))
        if emd is not None:
            tender.emd_amount = emd
            changes["emd_amount"] = emd
        elif commercials.get("emd"):
            logger.info(
                f"[enrich] emd unparseable, skipped: {commercials.get('emd')!r}"
            )

    if getattr(tender, "closing_date", None) is None:
        dt = parse_tender_date(commercials.get("closing_date_raw"))
        if dt is not None:
            tender.closing_date = dt
            changes["closing_date"] = dt.isoformat()
        elif commercials.get("closing_date_raw"):
            logger.info(
                f"[enrich] closing_date unparseable, skipped: "
                f"{commercials.get('closing_date_raw')!r}"
            )
    return changes
```

- [ ] **Step 4: Run the mapping test to verify it passes**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_tender_enrichment_commercials.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Add `_pick_primary_blob` (NIT-preferred full-blob selection)**

In `tender_enrichment_service.py`, add after `_pick_primary_facts`:

```python
def _pick_primary_blob(db, tender_id: int) -> tuple[dict, Optional[str]]:
    """Return the most-authoritative FULL per-doc summary_json blob (which
    carries both `key_facts` and `commercials`) for this tender.

    Preference order: (1) a blob whose source doc is NIT-typed
    (TenderDocument.document_type == 'nit'); among those, the richest key_facts.
    (2) else the richest key_facts across all blobs. Returns ({}, None) when
    there are no per-doc summaries.
    """
    from app.models.document_analysis import DocumentExtractionResult
    from app.models.tender import TenderDocument

    rows = (
        db.query(DocumentExtractionResult)
        .filter(
            DocumentExtractionResult.tender_id == tender_id,
            DocumentExtractionResult.extraction_type == "per_doc_summary",
        )
        .all()
    )
    blobs = [(r.document_id, r.summary_json) for r in rows
             if isinstance(r.summary_json, dict)]
    if not blobs:
        return {}, None

    nit_doc_ids = {
        d.id for d in db.query(TenderDocument)
        .filter(TenderDocument.tender_id == tender_id,
                TenderDocument.document_type == "nit").all()
    }

    def _rank(item):
        doc_id, blob = item
        kf = blob.get("key_facts") if isinstance(blob.get("key_facts"), dict) else {}
        is_nit = 1 if doc_id in nit_doc_ids else 0
        return (is_nit, _score_facts_richness(kf))

    doc_id, best_blob = max(blobs, key=_rank)
    return best_blob, best_blob.get("doc_name")
```

- [ ] **Step 6: Lift the command_center guard and wire the 5-field write**

In `enrich_tender_from_analysis`, make these edits:

(a) DELETE the guard block at lines ~209-219 (`is_auto_created` early-return). Replace with a one-line comment:
```python
        # Enrich ALL tenders (was command_center-only). Title/scope overwrite,
        # numerics fill-if-empty — see design 2026-07-21-nit-fetch-and-enrichment.
```

(b) Change the title write from default-only to overwrite. Find:
```python
        if new_title and _is_default_title(tender.title):
            tender.title = new_title
            changes["title"] = new_title
```
Replace with (overwrite unless the new title is corrupt/empty):
```python
        from app.services.tender_service import is_corrupt_title
        if new_title and not is_corrupt_title(new_title):
            if tender.title != new_title:
                tender.title = new_title
                changes["title"] = new_title
        # Scope of work → description (overwrite when we have a real scope).
        if name_of_work and getattr(tender, "description", None) != name_of_work:
            tender.description = name_of_work
            changes["description"] = name_of_work
```

(c) After the title/description block and before the `changes` are committed, add the commercials write using the NIT-preferred blob:
```python
        primary_blob, _blob_doc = _pick_primary_blob(db, tender_id)
        commercials = primary_blob.get("commercials") if isinstance(primary_blob, dict) else None
        changes.update(_apply_commercials_to_tender(tender, commercials))
```

(d) The existing `portal`/`tender_id` writes (lines ~299-304) are `command_center`-specific — guard them so they only run for auto-created tenders now that the outer guard is gone:
```python
        _auto = tender.portal == "command_center" or (tender.tender_id or "").startswith("cc-")
        if _auto and tender.portal == "command_center":
            tender.portal = portal
            changes["portal"] = portal
        if _auto and (tender.tender_id or "").startswith("cc-") and tender_reference:
            tender.tender_id = tender_reference
            changes["tender_id"] = tender_reference
```

- [ ] **Step 7: Run the enrichment tests + full analysis-related suite**

Run: `venv/Scripts/python.exe -m pytest tests/services/test_tender_enrichment_commercials.py tests/services/test_tender_value_parsers.py -v`
Expected: PASS.

Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('app/services/tender_enrichment_service.py').read()); print('syntax ok')"`
Expected: `syntax ok`.

- [ ] **Step 8: Commit**

```bash
git add app/services/tender_enrichment_service.py tests/services/test_tender_enrichment_commercials.py
git commit -m "feat(analysis): persist NIT bid value/EMD/closing/title/scope for all tenders

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Thread source_url through the document upload (extension → backend)

**Files:**
- Modify: `drpl-extension/src/utils/api-client.ts:194-232` (`uploadDocument` — add `sourceUrl` param → FormData)
- Modify: `drpl-extension/src/background/service-worker.ts:873-929` (`downloadAndUploadDocument` — pass `doc.url`)
- Modify: `drpl-backend/app/api/routes/extension.py:140-250` (`upload_document` — accept `source_url` Form, store on `TenderDocument.source_url`, dedup on `(tender, source_url)`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `TenderDocument.source_url` populated on extension uploads; enables Task 2's NIT-doc preference (via `document_type`) and future URL dedup.

- [ ] **Step 1: Extension — add `sourceUrl` to `uploadDocument`**

In `drpl-extension/src/utils/api-client.ts`, change the signature and body:
```typescript
export async function uploadDocument(
  tenderId: string,
  fileName: string,
  fileBlob: Blob,
  documentType?: string,
  portal?: string,
  sourceUrl?: string,
): Promise<ApiResponse> {
```
and after the `portal` append:
```typescript
  if (portal) {
    formData.append('portal', portal);
  }
  if (sourceUrl) {
    formData.append('source_url', sourceUrl);
  }
```

- [ ] **Step 2: Extension — pass `doc.url` from `downloadAndUploadDocument`**

In `drpl-extension/src/background/service-worker.ts`, change the final upload call (line ~927):
```typescript
  await uploadDocument(tenderId, fileName, blob, doc.type, portal, doc.url);
```

- [ ] **Step 3: Backend — accept + store `source_url`, dedup on it**

In `drpl-backend/app/api/routes/extension.py`, add the Form param to `upload_document` (after `portal`):
```python
    portal: str = Form(None),
    source_url: str = Form(None),
```
Add a URL-based dedup BEFORE the size-based one (a re-scrape re-downloads the same URL). Find the existing `existing = (...)` block (lines ~192-200) and insert before it:
```python
    if source_url:
        by_url = (
            db.query(TenderDocument)
            .filter(
                TenderDocument.tender_id == tid,
                TenderDocument.source_url == source_url,
            )
            .first()
        )
        if by_url:
            return {
                "success": True,
                "file_name": file_name,
                "size": len(content),
                "deduplicated": True,
                "document_id": by_url.id,
            }
```
Add `source_url` to the `TenderDocument(...)` constructor (after `document_type=...`):
```python
        document_type=document_type or "other",
        source_url=source_url,
        uploaded_by=current_user.id,
```

- [ ] **Step 4: Backend — verify `TenderDocument.source_url` column exists**

Run: `venv/Scripts/python.exe -c "from app.models.tender import TenderDocument; print('source_url' in TenderDocument.__table__.columns)"`
Expected: `True`. (Column already exists per the code map at `tender.py:117-135`. If it prints `False`, STOP — the column must be added via Alembic + schema-drift self-heal; escalate.)

- [ ] **Step 5: Extension — build check (no import leftovers in content scripts)**

Run (from `drpl-extension/`): `npm run build`
Expected: build succeeds; the `bundle-content-scripts` plugin runs. (These changes touch the service worker + api-client, both module-capable, so no content-script import concern — but confirm the build is green.)

- [ ] **Step 6: Backend — syntax + upload route smoke**

Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('app/api/routes/extension.py').read()); print('syntax ok')"`
Expected: `syntax ok`.

- [ ] **Step 7: Commit**

```bash
git add drpl-extension/src/utils/api-client.ts drpl-extension/src/background/service-worker.ts drpl-backend/app/api/routes/extension.py
git commit -m "feat(ext+api): thread source_url through doc upload; dedup by (tender,url)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Trigger doc-fetch on tender open (reuse manual hunt) — extension

**Files:**
- Modify: `drpl-extension/src/background/service-worker.ts` (add a message handler to run `processDocumentHunt` for a single tender that lacks docs, reusing the existing manual-hunt path)
- Modify: `drpl-extension/src/popup/components/ScrapeProgressPanel.tsx` OR the tender detail UI that the user "opens" — wire a call to the new handler when a tender is opened and has no NIT doc yet
- Test: manual smoke (extension UI), documented below

**Interfaces:**
- Consumes: existing `processDocumentHunt(tenders, {clearQueue, forceDownload})` and `isDocumentHunting` guard.
- Produces: a `runDocumentHuntForTender(tenderId)` message action.

- [ ] **Step 1: Add a single-tender hunt message action**

In `service-worker.ts`, in the `chrome.runtime.onMessage` handler (find the existing `processDocumentHunt(tenders)` manual entrypoint at line ~1399 for the pattern), add a case:
```typescript
    if (message.type === 'HUNT_DOCS_FOR_TENDER') {
      const tender: TenderData | undefined = message.tender;
      if (!tender) { sendResponse({ success: false, error: 'no tender' }); return true; }
      if (isDocumentHunting) { sendResponse({ success: false, error: 'hunt in progress' }); return true; }
      (async () => {
        isDocumentHunting = true;
        try {
          const res = await processDocumentHunt([tender], { clearQueue: false, forceDownload: true });
          sendResponse({ success: true, ...res });
        } catch (e) {
          sendResponse({ success: false, error: (e as Error).message });
        } finally {
          isDocumentHunting = false;
        }
      })();
      return true;  // async response
    }
```

- [ ] **Step 2: Trigger it when a tender is opened without docs**

In the popup component that shows a selected/opened tender (`ScrapeProgressPanel.tsx` or the tender detail view — grep for where a tender row's detail/open action lives), when the user opens a tender whose `classifiedDocuments` is empty (or that the backend reports has no NIT doc), send the message:
```typescript
chrome.runtime.sendMessage(
  { type: 'HUNT_DOCS_FOR_TENDER', tender },
  (resp) => { /* update UI: 'Fetching documents…' → done/failed */ },
);
```
(Exact component + trigger point: pick the open/select handler; keep it behind a guard so it fires once per tender and not on every re-render.)

- [ ] **Step 3: Build check**

Run (from `drpl-extension/`): `npm run build`
Expected: build succeeds.

- [ ] **Step 4: Manual smoke (documented, run by controller/user)**

With the extension loaded and logged into IREPS/GeM:
1. Open a tender that has no documents captured yet.
2. Confirm the service worker logs `[Ext] Hunting docs for <id>` and uploads N docs.
3. Confirm `TenderDocument` rows appear for that tender with `source_url` populated and the NIT row `document_type == 'nit'`.
4. Trigger analysis → confirm the tender's title/scope get overwritten and bid value/EMD/closing date fill in.

- [ ] **Step 5: Commit**

```bash
git add drpl-extension/src/background/service-worker.ts drpl-extension/src/popup/components/ScrapeProgressPanel.tsx
git commit -m "feat(ext): fetch tender documents on open when none captured yet

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Gap A deltas (thread source_url + backend store/dedup) → Task 3 ✓
- Gap A trigger (fetch on open, reuse manual hunt) → Task 4 ✓
- Gap B parsers → Task 1 ✓
- Gap B mapping + lift command_center guard + overwrite title/scope + fill-if-empty numerics → Task 2 ✓
- Testing (pure parsers, mapping semantics, build checks, manual smoke) → Tasks 1-4 ✓

**Placeholder scan:** Task 4 Steps 2 names "pick the open/select handler" as a discovery step rather than an exact line — this is inherent (the UI trigger point depends on the current component structure); the implementer greps for the open action. All backend/logic steps are concrete.

**Type consistency:** `_apply_commercials_to_tender(tender, commercials) -> dict` and `_pick_primary_blob(db, tender_id) -> (dict, Optional[str])` used consistently across Task 2 steps. `uploadDocument(..., sourceUrl?)` new param matches the `downloadAndUploadDocument` call in Task 3. `parse_indian_currency`/`parse_tender_date` names consistent between Task 1 and Task 2.

**Known follow-ups for the implementer:** Task 4's UI trigger point is a grep-to-find; Task 3 Step 4 verifies the `source_url` column exists before relying on it.
