# Tender Title Scraping & Data Repair — Design

**Date:** 2026-06-27
**Status:** Approved (pending spec review)
**Repos touched:** `drpl-extension` (content scripts), `drpl-backend` (ingest + one-time cleanup)

## Problem

Stored tender `title` values are corrupt at the source (confirmed in DB):

- **IREPS:** `'(i) Coach alteration in electrical system in 1000 Coaches of......\tTender Type:\tOpen'`
  — clipped title (literal `......`) with the "Tender Type: Open" label mixed into the same cell.
- **GeM:** `'Quantity:\xa05370'` — not a title at all; the scraper grabbed the wrong element.

No frontend change can recover a title that was never stored correctly. The fix is in the
extension scrapers (capture the real title) + the backend (clean on ingest, allow title
updates, repair existing rows).

## Root causes (from investigation)

**IREPS — `drpl-extension/src/content-scripts/ireps.ts`**
- Title read from a composite table cell whose `textContent` includes the clipped title **plus**
  the embedded "Tender Type: Open" label. Mapped path `ireps.ts:447`, positional fallback `:459`,
  via `getCellText`→`extractText` (`src/utils/selectors.ts:52-55`). Column-match regex is broad
  (`ireps.ts:216`).
- The literal `......` is the **portal's own rendered clipped text**; `extractText` only collapses
  whitespace and never strips trailing ellipsis.
- The full title likely lives in the cell/anchor `title=` attribute (IREPS sets these — proven by
  `ireps.ts:619` reading `getAttribute('title')` for docs) and/or the detail page, but the title
  path never reads them.

**GeM — `drpl-extension/src/content-scripts/gem.ts`**
- `extractFromCard` (`gem.ts:235`) calls `extractFieldByLabel(card, ['item','product',...])`
  (`gem.ts:296-333`) which uses loose `includes` substring matching, picks the wrong element, and
  returns its **next sibling** — the "Quantity: 5370" block. `\xa0` survives because it bypasses
  `extractText`.
- A correct label-anchored extractor already exists but is unused by the passive path:
  `gem-search-driver.ts:401` uses `pickField(text, 'Tender\\s*Title')` (`:389-394`).

**Backend — `drpl-backend/app/services/tender_service.py`**
- Only `.strip()` is applied to title on insert (`:55`, stored `:62`). No cleaning.
- **`_update_existing_tender` (`:137-192`) never assigns `title`** — title is set only on INSERT.
  So fixing the scraper alone won't repair existing rows, and even fixed re-scrapes of the same
  `(portal, tender_id)` won't update the title.
- Dedupe key is `(portal, tender_id)` (code `:36-41`, unique index `tender.py:94-96`).
- `title`/`description` are `Text` (no length cap); `title` is NOT NULL.

## Decisions (from brainstorming)

1. **Apply fixed titles to existing rows via BOTH:** (a) a one-time backfill to repair existing
   bad titles, and (b) make ingest UPDATE the title when a clearly-better one arrives (future
   re-scrapes self-heal).
2. **IREPS source:** prefer the cell/anchor `title=` attribute, fall back to cell text, then clean
   (strip `Tender Type:` suffix and trailing `......`). No extra page loads.
3. **GeM fix:** reuse the proven label-anchored `Tender Title` extractor from
   `gem-search-driver.ts` in the passive `gem.ts` path (extract to a shared util — DRY).
4. **IREPS `title=` attr is an ASSUMPTION** (browser not connected to confirm). Build defensively:
   attr-first with text-clean fallback works either way; **user verifies on a live IREPS page**.
5. **Backfill = standalone manual script** (`cleanup_checklist_markers.py` pattern), not auto-run
   on boot. One-time data repair shouldn't run every startup.
6. **GeM `Quantity:` rows have NO recoverable title** in stored data. The backfill will NOT
   fabricate titles for them; it leaves/flags them for re-scrape repair via the fixed extension.

## Design

### Part A — Extension scraper fixes (`drpl-extension`)

**A1. Shared `cleanTitle()` util** (`src/utils/selectors.ts` or a new `src/utils/text.ts`):
```
cleanTitle(raw): string
  - replace \xa0 with normal space
  - strip trailing "Tender Type: …" suffix:  /\s*Tender\s*Type\s*:.*$/i
  - strip trailing ellipsis runs:            /[.…]{2,}\s*$/
  - collapse \s+ → ' ', trim
```

**A2. IREPS (`ireps.ts`)** — new title resolution used where title is currently assigned
(`:447`/`:459` path, final assignment `:507`):
```
resolveIrepsTitle(cell):
  1. cell.getAttribute('title') (or inner anchor's title=) if non-empty
  2. else extractText(cell)
  3. return cleanTitle(result)
```
Apply to both mapped and positional paths so the cleaned, attribute-preferred title flows into the
payload. `description` (currently `= title`, `:510`) uses the same cleaned value.

**A3. GeM (`gem.ts`)** — in `extractFromCard` (`:235`), replace the loose
`extractFieldByLabel([...title...])` call with a label-anchored extractor:
- Extract the `pickField`-style "Tender Title" logic from `gem-search-driver.ts:389-401` into the
  shared util (e.g. `pickLabeledField(text, label)`), and use it for the title:
  `pickLabeledField(cardText, 'Tender\\s*Title') || pickLabeledField(cardText, 'Title')`.
- Fall back to the previous heuristic only if no labeled title is found.
- Run the result through `cleanTitle()`.

**A4. Build integrity:** content scripts can't use ES module imports at runtime (MV3). Per
CLAUDE.md, the vite `bundle-content-scripts` plugin inlines shared chunks. After build, verify
`dist/content-scripts/*.js` contain no leftover `import` statements (the shared util must inline).

### Part B — Backend: clean on ingest + allow title updates (`tender_service.py`)

**B1. `clean_tender_title(raw: str) -> str`** — server-side mirror of `cleanTitle()` (defense in
depth; old extensions in the field still send dirty data):
- normalize `\xa0`, strip `Tender Type:` suffix, strip trailing `......`, collapse ws, trim.

**B2. `_is_corrupt_title(title: str) -> bool`** — detects: contains `......`, contains
`\tTender Type:` / `Tender Type:` suffix, starts with `Quantity:`, or contains `\xa0`. Used to
decide "is the incoming title better than what we have."

**B3. Insert path (`:62`):** store `clean_tender_title(title)`.

**B4. Update path (`_update_existing_tender`, `:137-192`):** add title update when the incoming
cleaned title is clearly better:
```
new_clean = clean_tender_title(new_data.title or "")
if new_clean and not _is_corrupt_title(new_clean):
    if _is_corrupt_title(existing.title) or len(new_clean) > len(existing.title or ""):
        existing.title = new_clean
```
(Conservative: only overwrite if the stored one is corrupt or the new one is strictly longer/better.
Never replace a good title with a shorter/equal one.)

### Part C — One-time backfill script (`drpl-backend/cleanup_tender_titles.py`)

Follows `cleanup_checklist_markers.py` (idempotent, manual `python cleanup_tender_titles.py`):
- SELECT rows where title looks corrupt: `LIKE '%......%'` OR `LIKE '%Tender Type:%'` OR
  `LIKE 'Quantity:%'` OR contains `\xa0`.
- For each: `cleaned = clean_tender_title(title)`.
  - If `cleaned` is a usable title (not still corrupt, non-empty, not just "Quantity: …") →
    UPDATE the row.
  - If the row is a GeM `Quantity:` style with **no recoverable title** → **do not fabricate**; log
    it as "needs re-scrape" (optionally leave title unchanged). Count separately.
- UPDATE only changed rows; print a summary (`fixed`, `needs_rescrape`, `unchanged`). Re-runnable.

## Component boundaries

- `cleanTitle()` (extension) / `clean_tender_title()` (backend): pure string→string, testable in
  isolation, no DOM/DB deps.
- `_is_corrupt_title()`: pure predicate.
- IREPS/GeM resolvers: DOM-in, clean-string-out; depend only on the shared util.
- Backfill script: depends on the model + `clean_tender_title`; self-contained.

## Testing / Verification

- **Backend (pytest available):** unit tests for `clean_tender_title` and `_is_corrupt_title`
  against the exact corrupt samples (`'...of......\tTender Type:\tOpen'` → `'...of'`;
  `'Quantity:\xa05370'` flagged corrupt). Test `_update_existing_tender` overwrites a corrupt
  stored title with a clean longer one, and does NOT overwrite a good title with a shorter one.
- **Extension (no JS test runner):** `npm run build`; grep `dist/content-scripts/*.js` for leftover
  `import`. Manual: scrape an IREPS listing + a GeM listing, confirm payload titles are clean/full.
- **Backfill:** run on a copy/locally; verify summary counts and idempotency (second run = 0 fixed).
- **USER live-verifies:** the IREPS `title=` attribute assumption on a real IREPS listing page.

## Out of scope

- Frontend expandable-title work (already shipped) — unchanged; benefits automatically.
- Following IREPS detail pages for titles (rejected: too slow/heavy).
- Fabricating titles for unrecoverable GeM `Quantity:` rows (they need re-scrape).
