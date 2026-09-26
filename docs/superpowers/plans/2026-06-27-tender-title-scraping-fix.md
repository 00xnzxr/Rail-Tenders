# Tender Title Scraping Fix & Data Repair — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture real, full tender titles at scrape time (IREPS + GeM), clean them on the backend, let re-scrapes overwrite corrupt titles, and repair existing bad rows with a one-time script.

**Architecture:** Three coordinated parts. (A) Extension content scripts get a shared `cleanTitle()` util; IREPS prefers the cell `title=` attribute then cleans; GeM reuses the proven label-anchored "Tender Title" extractor. (B) Backend cleans titles on ingest and updates a stored title when a clearly-better one arrives. (C) A standalone idempotent backfill script repairs existing rows, leaving unrecoverable GeM `Quantity:` rows for re-scrape.

**Tech Stack:** TypeScript (Vite, Chrome MV3 content scripts), Python (FastAPI, SQLAlchemy), pytest (backend).

## Global Constraints

- **Backend has pytest** → use TDD for Parts B/C (test first). Run: `cd drpl-backend && pytest`.
- **Extension has NO JS test runner** → verify via `npm run build` + grep `dist/content-scripts/*.js` for leftover `import` + manual scrape. Do NOT add a JS test framework.
- **MV3 content scripts cannot use runtime ES module imports.** Shared utils must be inlined by the existing `bundle-content-scripts` vite plugin (CLAUDE.md). After build, `dist/content-scripts/*.js` must contain no `import` statements.
- Dedupe key is `(portal, tender_id)`; unique index `ix_tenders_portal_tender_id`.
- `clean_tender_title()` (backend) and `cleanTitle()` (extension) must implement the SAME rules so server and client agree:
  1. replace `\xa0` (U+00A0) with a normal space
  2. strip trailing "Tender Type:" suffix — regex `/\s*Tender\s*Type\s*:.*$/i`
  3. strip trailing ellipsis/dot runs — regex `/[.…]{2,}\s*$/`
  4. collapse whitespace `\s+`→`' '`, trim
- A title is "corrupt" if it: contains `......` (`[.…]{2,}`), contains `Tender Type:`, starts with `Quantity:` (case-insensitive), or contains `\xa0`.
- Never replace a good stored title with a shorter/equal one; only overwrite when the stored title is corrupt OR the new clean title is strictly longer.
- The backfill must NOT fabricate titles for GeM `Quantity:` rows (real title unrecoverable) — count them as "needs re-scrape".

---

## Part B — Backend (do first; TDD, and Part A/C reference its rules)

### Task B1: `clean_tender_title` + `is_corrupt_title` helpers

**Files:**
- Modify: `drpl-backend/app/services/tender_service.py`
- Test: `drpl-backend/tests/test_tender_title_cleaning.py` (create)

**Interfaces:**
- Produces: `clean_tender_title(raw: str | None) -> str`, `is_corrupt_title(title: str | None) -> bool` (module-level functions in `tender_service.py`).

- [ ] **Step 1: Write failing tests**

Create `drpl-backend/tests/test_tender_title_cleaning.py`:

```python
from app.services.tender_service import clean_tender_title, is_corrupt_title


def test_clean_strips_tender_type_suffix_and_ellipsis():
    raw = "(i) Coach alteration in electrical system in 1000 Coaches of......\tTender Type:\tOpen"
    assert clean_tender_title(raw) == "(i) Coach alteration in electrical system in 1000 Coaches of"


def test_clean_normalizes_nbsp():
    assert clean_tender_title("Quantity:\xa05370") == "Quantity: 5370"


def test_clean_handles_none_and_empty():
    assert clean_tender_title(None) == ""
    assert clean_tender_title("   ") == ""


def test_clean_leaves_good_title_untouched():
    good = "Annual Maintenance Contract of 4 Nos. 8-wheeler tower wagons"
    assert clean_tender_title(good) == good


def test_is_corrupt_detects_each_marker():
    assert is_corrupt_title("foo......")
    assert is_corrupt_title("foo Tender Type: Open")
    assert is_corrupt_title("Quantity: 5370")
    assert is_corrupt_title("Quantity:\xa05370")
    assert is_corrupt_title("bad\xa0title")


def test_is_corrupt_false_for_clean_title():
    assert not is_corrupt_title("Annual Maintenance Contract of 4 Nos.")
    assert not is_corrupt_title("")
    assert not is_corrupt_title(None)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd drpl-backend && pytest tests/test_tender_title_cleaning.py -v`
Expected: FAIL — `ImportError: cannot import name 'clean_tender_title'`.

- [ ] **Step 3: Implement the helpers**

In `drpl-backend/app/services/tender_service.py`, add after the imports (around line 13, before `ingest_tender_batch`):

```python
import re

# Markers that identify a corrupt scraped title (see plan Global Constraints).
_TENDER_TYPE_SUFFIX_RE = re.compile(r"\s*Tender\s*Type\s*:.*$", re.IGNORECASE)
_TRAILING_ELLIPSIS_RE = re.compile(r"[.…]{2,}\s*$")
_ELLIPSIS_ANYWHERE_RE = re.compile(r"[.…]{2,}")


def clean_tender_title(raw: "str | None") -> str:
    """Normalize a scraped tender title: drop NBSP, the embedded 'Tender Type:'
    suffix, and trailing ellipsis runs, then collapse whitespace. Pure string fn."""
    if not raw:
        return ""
    s = raw.replace("\xa0", " ")
    s = _TENDER_TYPE_SUFFIX_RE.sub("", s)
    s = _TRAILING_ELLIPSIS_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_corrupt_title(title: "str | None") -> bool:
    """True if the title shows scrape-corruption markers and should not be trusted."""
    if not title:
        return False
    if "\xa0" in title:
        return True
    if _ELLIPSIS_ANYWHERE_RE.search(title):
        return True
    if re.search(r"Tender\s*Type\s*:", title, re.IGNORECASE):
        return True
    if re.match(r"\s*Quantity\s*:", title, re.IGNORECASE):
        return True
    return False
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd drpl-backend && pytest tests/test_tender_title_cleaning.py -v`
Expected: PASS (all 6).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/tender_service.py drpl-backend/tests/test_tender_title_cleaning.py
git commit -m "feat(tenders): add clean_tender_title + is_corrupt_title helpers"
```

---

### Task B2: Clean on insert + allow title update on re-scrape

**Files:**
- Modify: `drpl-backend/app/services/tender_service.py:55-63` (insert) and `:137-192` (`_update_existing_tender`)
- Test: `drpl-backend/tests/test_tender_title_cleaning.py` (extend)

**Interfaces:**
- Consumes: `clean_tender_title`, `is_corrupt_title` from B1.

- [ ] **Step 1: Write failing tests for the update rule**

Append to `drpl-backend/tests/test_tender_title_cleaning.py`:

```python
from types import SimpleNamespace
from app.services.tender_service import _update_existing_tender


def _existing(title):
    return SimpleNamespace(
        title=title, status="open", closing_date=None, document_links=[],
        is_detail_extracted=False, description="", updated_at=None,
    )


def _incoming(title):
    # Minimal TenderInput-like stub with only the fields _update_existing_tender reads.
    return SimpleNamespace(
        title=title, status=None, closingDate=None, documentLinks=None,
        isDetailExtracted=False, description=None,
    )


def test_update_overwrites_corrupt_stored_title_with_clean():
    existing = _existing("Coach alteration ...of......\tTender Type:\tOpen")
    _update_existing_tender(existing, _incoming("Coach alteration in electrical system full title"))
    assert existing.title == "Coach alteration in electrical system full title"


def test_update_does_not_replace_good_with_shorter():
    existing = _existing("A complete and correct long tender title here")
    _update_existing_tender(existing, _incoming("Short title"))
    assert existing.title == "A complete and correct long tender title here"


def test_update_replaces_good_with_strictly_longer_clean():
    existing = _existing("Short title")
    _update_existing_tender(existing, _incoming("Short title with much more useful detail"))
    assert existing.title == "Short title with much more useful detail"


def test_update_ignores_incoming_corrupt_title():
    existing = _existing("A complete and correct long tender title here")
    _update_existing_tender(existing, _incoming("Quantity:\xa05370"))
    assert existing.title == "A complete and correct long tender title here"
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd drpl-backend && pytest tests/test_tender_title_cleaning.py -v -k update`
Expected: FAIL — `_update_existing_tender` does not set `title` yet (assertions mismatch).

- [ ] **Step 3: Clean the insert path**

In `tender_service.py`, change the insert title block (currently lines 55-57):

```python
                # Ensure title is not empty (DB requires non-null)
                title = (tender_input.title or "").strip()
                if not title:
                    title = f"{tender_input.portal} #{tender_input.tenderId}"
```

to:

```python
                # Clean + ensure title is not empty (DB requires non-null)
                title = clean_tender_title(tender_input.title)
                if not title or is_corrupt_title(title):
                    title = f"{tender_input.portal} #{tender_input.tenderId}"
```

- [ ] **Step 4: Add the title-update rule to `_update_existing_tender`**

In `_update_existing_tender`, immediately after the docstring (before the `if new_data.status` line at ~142), insert:

```python
    # Repair / upgrade the stored title when a clearly-better one arrives, so a
    # re-scrape with the fixed extension self-heals corrupt rows. Never downgrade
    # a good title to a shorter/equal one.
    new_clean = clean_tender_title(getattr(new_data, "title", None))
    if new_clean and not is_corrupt_title(new_clean):
        if is_corrupt_title(existing.title) or len(new_clean) > len(existing.title or ""):
            existing.title = new_clean
```

- [ ] **Step 5: Run tests, verify they pass**

Run: `cd drpl-backend && pytest tests/test_tender_title_cleaning.py -v`
Expected: PASS (all, including the 4 update tests).

- [ ] **Step 6: Run the broader tender test suite for regressions**

Run: `cd drpl-backend && pytest tests/ -k tender -q`
Expected: PASS (no regressions). If a pre-existing unrelated test fails, note it but do not fix here.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/tender_service.py drpl-backend/tests/test_tender_title_cleaning.py
git commit -m "feat(tenders): clean titles on ingest and self-heal on re-scrape"
```

---

### Task B3: One-time backfill script for existing rows

**Files:**
- Create: `drpl-backend/cleanup_tender_titles.py`

**Interfaces:**
- Consumes: `clean_tender_title`, `is_corrupt_title` from B1; `Tender` model; `SessionLocal`.

- [ ] **Step 1: Write the script**

Create `drpl-backend/cleanup_tender_titles.py` (mirrors the idempotent `cleanup_checklist_markers.py` pattern):

```python
"""
One-time, idempotent repair of corrupt tender titles already in the DB.

Fixes IREPS titles like '...of......\\tTender Type:\\tOpen' by stripping the
ellipsis + 'Tender Type:' suffix. GeM rows stored as 'Quantity: 5370' have NO
recoverable title — these are reported as "needs re-scrape" and left unchanged
(re-scraping with the fixed extension repairs them). Safe to run multiple times.

Usage:  python cleanup_tender_titles.py
"""

import logging

from app.core.database import SessionLocal
from app.models.tender import Tender
from app.services.tender_service import clean_tender_title, is_corrupt_title

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("cleanup_tender_titles")


def main() -> None:
    db = SessionLocal()
    fixed = 0
    needs_rescrape = 0
    unchanged = 0
    try:
        # Pull only candidate rows (corrupt markers). Broad LIKE prefilter; the
        # is_corrupt_title() check below is the real gate.
        candidates = (
            db.query(Tender)
            .filter(
                (Tender.title.like("%......%"))
                | (Tender.title.like("%Tender Type:%"))
                | (Tender.title.like("Quantity:%"))
                | (Tender.title.like("%\xa0%"))
            )
            .all()
        )
        log.info("Found %d candidate rows", len(candidates))

        for t in candidates:
            cleaned = clean_tender_title(t.title)
            # Unrecoverable: cleaning still leaves a corrupt/quantity-only value.
            if not cleaned or is_corrupt_title(cleaned):
                needs_rescrape += 1
                log.info("NEEDS RE-SCRAPE id=%s portal=%s tender_id=%s title=%r",
                         t.id, t.portal, t.tender_id, t.title)
                continue
            if cleaned != t.title:
                t.title = cleaned
                fixed += 1
            else:
                unchanged += 1

        if fixed:
            db.commit()
        log.info("Done. fixed=%d needs_rescrape=%d unchanged=%d", fixed, needs_rescrape, unchanged)
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Dry-run sanity check (no-write path) by reading code**

Confirm the script imports resolve:

Run: `cd drpl-backend && python -c "import cleanup_tender_titles; print('import OK')"`
Expected: `import OK` (plus any DB-engine log lines). No exception.

- [ ] **Step 3: Run the backfill**

Run: `cd drpl-backend && python cleanup_tender_titles.py`
Expected: prints `Found N candidate rows` then `Done. fixed=X needs_rescrape=Y unchanged=Z`. IREPS `...Tender Type` rows should land in `fixed`; GeM `Quantity:` rows in `needs_rescrape`.

- [ ] **Step 4: Verify idempotency**

Run it a second time: `cd drpl-backend && python cleanup_tender_titles.py`
Expected: `fixed=0` (the previously-fixed rows no longer match or are already clean); `needs_rescrape` may still list the unrecoverable GeM rows (unchanged, expected).

- [ ] **Step 5: Spot-check the DB**

Run:
```bash
cd drpl-backend && python -c "
from app.core.database import SessionLocal
from app.models.tender import Tender
db = SessionLocal()
for t in db.query(Tender).filter(Tender.portal=='ireps').order_by(Tender.id.desc()).limit(5):
    print(repr(t.title)[:120])
"
```
Expected: IREPS titles no longer contain `......` or `Tender Type:`.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/cleanup_tender_titles.py
git commit -m "feat(tenders): one-time backfill script to repair corrupt titles"
```

---

## Part A — Extension scrapers

### Task A1: Shared `cleanTitle` + `pickLabeledField` utils

**Files:**
- Modify: `drpl-extension/src/utils/selectors.ts`

**Interfaces:**
- Produces: `export function cleanTitle(raw: string | null | undefined): string` and
  `export function pickLabeledField(text: string, label: string): string` in `selectors.ts`.

- [ ] **Step 1: Add the utils**

In `drpl-extension/src/utils/selectors.ts`, after `extractText` (ends line 55), add:

```typescript
/**
 * Normalize a scraped tender title. Mirrors the backend clean_tender_title():
 * drop NBSP, the embedded "Tender Type:" suffix, and trailing ellipsis runs,
 * then collapse whitespace.
 */
export function cleanTitle(raw: string | null | undefined): string {
  if (!raw) return '';
  return raw
    .replace(/ /g, ' ')
    .replace(/\s*Tender\s*Type\s*:.*$/i, '')
    .replace(/[.…]{2,}\s*$/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Extract a labelled value from a card's collapsed text, e.g.
 * pickLabeledField(text, 'Tender\\s*Title') → the title up to the next label.
 * (Same logic the GeM search-driver uses; shared here so passive paths reuse it.)
 */
export function pickLabeledField(text: string, label: string): string {
  const re = new RegExp(`${label}\\s*:\\s*([^\\n]+?)(?=\\s{2,}\\w[\\w ]+\\s*:|\\n|$)`, 'i');
  const m = text.match(re);
  return m ? m[1].trim() : '';
}
```

- [ ] **Step 2: Type-check**

Run: `cd drpl-extension && npx tsc --noEmit`
Expected: exit 0 (no type errors).

- [ ] **Step 3: Commit**

```bash
git add drpl-extension/src/utils/selectors.ts
git commit -m "feat(extension): shared cleanTitle + pickLabeledField utils"
```

---

### Task A2: IREPS — prefer `title=` attribute, then clean

**Files:**
- Modify: `drpl-extension/src/content-scripts/ireps.ts` (`getCellText` area `:323-328`, title assignment `:446`/`:459`, payload `:507`/`:510`)

**Interfaces:**
- Consumes: `cleanTitle` from A1 (import from `../utils/selectors`).

- [ ] **Step 1: Import `cleanTitle`**

In `ireps.ts`, find the existing import from `../utils/selectors` and add `cleanTitle` to it. (It already imports `extractText`, `extractNumber`, etc. from there — add `cleanTitle` to the same named-import list.)

- [ ] **Step 2: Add a title-cell resolver helper**

In `ireps.ts`, right after `getCellText` (ends line 328), add:

```typescript
/**
 * Resolve a tender title from a table cell, preferring the cell's (or its inner
 * anchor's) `title=` attribute — IREPS sets the full, untruncated text there
 * when the visible cell text is clipped — then falling back to the cell text.
 * Always cleaned (strips "Tender Type:" suffix + trailing "......").
 *
 * ASSUMPTION: IREPS populates title= on the title cell/anchor. The textContent
 * fallback works regardless; verify on a live IREPS listing.
 */
function getTitleCellText(row: Element, colIndex: number): string {
  if (colIndex < 0) return '';
  const cells = row.querySelectorAll('td');
  if (colIndex >= cells.length) return cleanTitle('');
  const cell = cells[colIndex];
  const attr =
    cell.getAttribute('title') ||
    cell.querySelector('a[title], [title]')?.getAttribute('title') ||
    '';
  const raw = attr.trim() || extractText(cell);
  return cleanTitle(raw);
}
```

- [ ] **Step 3: Use the resolver for the mapped title**

In the `if (mapping)` branch, change line 446:

```typescript
        title = getCellText(row, mapping.title);
```

to:

```typescript
        title = getTitleCellText(row, mapping.title);
```

- [ ] **Step 4: Clean the positional-fallback title**

In the `else` branch, change line 459:

```typescript
        title = texts[2] || texts[1] || '';
```

to:

```typescript
        title = cleanTitle(texts[2] || texts[1] || '');
```

- [ ] **Step 5: Ensure payload title/description use the cleaned value**

The payload already uses `title` (`:507` `title: title || tenderNo`) and `description: title` (`:510`). Since `title` is now cleaned upstream, no change needed there — confirm by reading those two lines remain `title: title || tenderNo` and `description: title`.

- [ ] **Step 6: Type-check**

Run: `cd drpl-extension && npx tsc --noEmit`
Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add drpl-extension/src/content-scripts/ireps.ts
git commit -m "feat(extension): IREPS reads title= attr and cleans the title"
```

---

### Task A3: GeM — reuse label-anchored title extractor

**Files:**
- Modify: `drpl-extension/src/content-scripts/gem.ts` (`extractFromCard`, title at `:235`)
- Modify: `drpl-extension/src/content-scripts/gem-search-driver.ts` (use shared `pickLabeledField`, DRY)

**Interfaces:**
- Consumes: `cleanTitle`, `pickLabeledField` from A1.

- [ ] **Step 1: Import the shared utils in `gem.ts`**

In `gem.ts`, add `cleanTitle` and `pickLabeledField` to the existing named import from `../utils/selectors`.

- [ ] **Step 2: Replace the GeM title extraction**

In `gem.ts` `extractFromCard`, change line 235:

```typescript
  const title = extractFieldByLabel(card, ['item', 'product', 'description', 'title', 'name', 'spec']) || text.slice(0, 200);
```

to:

```typescript
  // Prefer the labelled "Tender Title" (reliable on GeM/bidnext cards); fall
  // back to the old heuristic only if no labelled title is present.
  const labeledTitle =
    pickLabeledField(text, 'Tender\\s*Title') || pickLabeledField(text, 'Title');
  const title = cleanTitle(
    labeledTitle ||
      extractFieldByLabel(card, ['item', 'product', 'description', 'title', 'name', 'spec']) ||
      text.slice(0, 200),
  );
```

- [ ] **Step 3: Clean the GeM description too**

In the same `return` object, the description is `description: title || text.slice(0, 500)` (line 253). Leave as-is — `title` is now clean, and the `text.slice` fallback is acceptable. No change. (Confirm line 253 still reads `description: title || text.slice(0, 500)`.)

- [ ] **Step 4: DRY the search-driver onto the shared util**

In `gem-search-driver.ts`, the local `pickField` (lines 389-394) duplicates A1's `pickLabeledField`. Replace its body to delegate (keeps the call sites `pickField(text, 'Tender\\s*Title')` unchanged):

Change lines 389-394:

```typescript
function pickField(text: string, label: string): string {
  // Matches e.g. "Tender Title: DYDROGESTERONE 10MG" up to the next labelled line.
  const re = new RegExp(`${label}\\s*:\\s*([^\\n]+?)(?=\\s{2,}\\w[\\w ]+\\s*:|\\n|$)`, 'i');
  const m = text.match(re);
  return m ? m[1].trim() : '';
}
```

to:

```typescript
import { pickLabeledField } from '../utils/selectors';

function pickField(text: string, label: string): string {
  return pickLabeledField(text, label);
}
```

(Place the `import` with the other top-of-file imports rather than inline if the file groups imports there; the inline form is shown for locality. Ensure only ONE import line for it.) Then apply `cleanTitle` at the title call site, line 401:

```typescript
  const title = pickField(text, 'Tender\\s*Title') || bidNo;
```

to:

```typescript
  const title = cleanTitle(pickField(text, 'Tender\\s*Title')) || bidNo;
```

Add `cleanTitle` to this file's `../utils/selectors` import as well.

- [ ] **Step 5: Type-check**

Run: `cd drpl-extension && npx tsc --noEmit`
Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add drpl-extension/src/content-scripts/gem.ts drpl-extension/src/content-scripts/gem-search-driver.ts
git commit -m "feat(extension): GeM uses label-anchored Tender Title extractor"
```

---

### Task A4: Build integrity — verify content scripts are self-contained

**Files:** none (verification only)

- [ ] **Step 1: Production build**

Run: `cd drpl-extension && npm run build`
Expected: build succeeds, emits `dist/`.

- [ ] **Step 2: Confirm no leftover ES imports in content scripts**

Run: `cd drpl-extension && grep -rEl "^import |require\(" dist/content-scripts/ || echo "CLEAN: no imports in content scripts"`
Expected: `CLEAN: no imports in content scripts`. If any file matches, the shared util did not inline — investigate the `bundle-content-scripts` plugin before shipping (do NOT load an unpacked build with leftover imports; content scripts will throw at runtime).

- [ ] **Step 3: Confirm the shared util actually inlined**

Run: `cd drpl-extension && grep -rl "Tender Type" dist/content-scripts/ && echo "cleanTitle logic present in content scripts"`
Expected: `ireps.js` / `gem.js` listed → `cleanTitle logic present...`. (Proves the util is bundled into the content scripts, not lost.)

- [ ] **Step 4: Manual scrape verification (USER-assisted)**

Load `dist/` unpacked in Chrome (reload on the extension card). Then:
- Open an IREPS tender listing → trigger a scrape → confirm uploaded titles are full/clean (no `......`, no `Tender Type:`). **Also confirm the `title=` attribute assumption held** (titles are full, not just de-dotted clips).
- Open a GeM listing → trigger a scrape → confirm titles are real tender titles, not `Quantity: …`.

(If the IREPS `title=` attribute turns out absent, titles will be clipped-but-clean; report back and we add a detail-page fallback.)

---

## Self-Review

**Spec coverage:**
- A1 shared `cleanTitle()` → Task A1. ✓
- IREPS `title=` attr-first + clean → Task A2. ✓
- GeM reuse label-anchored extractor (DRY) → Task A3. ✓
- Backend clean on ingest → Task B2 Step 3. ✓
- Backend allow title update / self-heal → Task B2 Step 4. ✓
- One-time backfill, idempotent, no fabrication for GeM Quantity rows → Task B3. ✓
- MV3 build integrity (no leftover imports) → Task A4. ✓
- IREPS attr assumption flagged + user live-verify → A2 Step 2 comment, A4 Step 4. ✓
- Backend pytest TDD; extension build+grep+manual → reflected throughout. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; all code shown verbatim. ✓

**Type/name consistency:** `clean_tender_title`/`is_corrupt_title` (backend) and `cleanTitle`/`pickLabeledField` (extension) used consistently across tasks; regexes match the Global Constraints rules; `getTitleCellText` defined in A2 and used in A2; `pickField` delegates to `pickLabeledField`. ✓

**Ordering note:** Part B is implemented first so the cleaning rules exist and the backfill can run; Part A then makes scrapes emit clean data; A4 verifies the build. Each task is independently committable and testable.
