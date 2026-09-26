# Tenders Detailed-Card Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the tender table with a detailed, expandable card list in DRPL's visual language, and capture four new fields (location, bid_type, source_portal, category) end-to-end from the Chrome extension through to the card UI.

**Architecture:** Three layers shipped back-to-front-compatible in order: (1) backend adds four nullable columns + self-healing migrations + exposes them on the list response and adds a `relevance` sort; (2) the extension scrapers capture the fields and send them; (3) the frontend swaps `TenderTable`/`TenderRow` for `TenderCardList`/`TenderCard` and restyles the filter bar into a toolbar. Each layer is independently deployable — the frontend renders "—" until data arrives, and optional fields are ignored by an un-migrated backend.

**Tech Stack:** FastAPI + SQLAlchemy + Alembic (backend), React 18 + Vite + Tailwind (frontend, shadcn-style HSL tokens), TypeScript + Vite + vitest/jsdom (extension).

## Global Constraints

- New DB columns are **nullable**; never backfilled. Old rows render "—". (spec §2, §4)
- Schema changes go in **three** places so deployments self-heal: Alembic revision, `_apply_schema_drift_fixes()` (Postgres), `_add_missing_columns()` (SQLite). (CLAUDE.md, spec §4)
- Frontend components use **semantic Tailwind tokens** (`bg-card`, `text-muted-foreground`, `text-accent`…) — never raw hex — so dark mode works. (drpl-frontend/src/index.css)
- Bid-type badge uses a **violet** hue, deliberately NOT the blue `accent`. (spec §7.2)
- Titles are **never truncated** — they wrap. (matches current `TenderRow`)
- `source_portal` is only set for aggregator listings; guard against `source_portal === portal`. (spec §4, §8)
- Null `emd_amount` renders "—" (not "Refer Document"). (spec §8)
- Extension content scripts must stay **self-contained after build** — no `import` in `dist/content-scripts/*.js`. (CLAUDE.md)
- Changing `drpl-extension/src/config/selectors.json` requires a backend OTA push too. (CLAUDE.md)
- Backend `sort_by=relevance` orders by `ai_relevance_score DESC NULLS LAST`. (spec §6)

---

## File Structure

**Backend (`drpl-backend/`)**
- `app/models/tender.py` — +4 columns on `Tender`.
- `app/main.py` — +4 entries in `_apply_schema_drift_fixes()` and `_add_missing_columns()`.
- `alembic/versions/20260707_tender_card_fields.py` — new revision (created).
- `app/schemas/__init__.py` — `TenderInput`, `TenderResponse`, `TenderDetailResponse` gain fields.
- `app/services/tender_service.py` — ingest mapping, `relevance` sort, `bid_type`/`location` filters.
- `app/api/routes/tenders.py` — `list_tenders` accepts `bid_type`/`location`.
- `tests/test_tender_card_fields.py` — new test (created).

**Extension (`drpl-extension/`)**
- `src/utils/types.ts` — `TenderData` gains 4 optional fields.
- `src/content-scripts/aggregators.ts` — export + wire a testable token parser; emit new fields.
- `src/content-scripts/aggregators.tokens.test.ts` — new vitest test (created).
- `src/content-scripts/gem.ts`, `src/content-scripts/ireps.ts` — best-effort capture.
- `src/config/selectors.json` — new selector keys (if used).

**Frontend (`drpl-frontend/`)**
- `src/types/tender.ts` — `Tender` + `TenderFilters` gain fields.
- `src/lib/api.ts` — `getTenders` passes `bid_type`/`location`.
- `src/hooks/useTenders.ts` — dependency array.
- `src/components/tenders/TenderCard.tsx` — new (created).
- `src/components/tenders/TenderCardList.tsx` — new (created).
- `src/components/tenders/TenderFilters.tsx` — restyled toolbar + new params.
- `src/pages/TendersPage.tsx` — swap list container, default sort `relevance`.
- Delete `src/components/tenders/TenderRow.tsx` + `TenderTable.tsx` after wiring.

---

## Task 1: Backend — Tender columns + self-healing migrations

**Files:**
- Modify: `drpl-backend/app/models/tender.py` (after line 73, the Phase-7 block)
- Modify: `drpl-backend/app/main.py` (`_apply_schema_drift_fixes` ~line 170; `_add_missing_columns` ~line 550)
- Create: `drpl-backend/alembic/versions/20260707_tender_card_fields.py`
- Test: `drpl-backend/tests/test_tender_card_fields.py`

**Interfaces:**
- Produces: `Tender.location: str|None`, `Tender.bid_type: str|None`, `Tender.source_portal: str|None`, `Tender.category: str|None`.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_tender_card_fields.py`:
```python
from app.core.database import Base, engine, SessionLocal
from app.models.tender import Tender


def test_tender_has_card_columns():
    """The four new card fields persist on the Tender model."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        t = Tender(
            portal="tendertiger", tender_id="TEST-CARD-1", title="Card test",
            location="Saran, Bihar, India", bid_type="NCB",
            source_portal="gem", category="Railways Transport Services",
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        assert t.location == "Saran, Bihar, India"
        assert t.bid_type == "NCB"
        assert t.source_portal == "gem"
        assert t.category == "Railways Transport Services"
    finally:
        db.query(Tender).filter(Tender.tender_id == "TEST-CARD-1").delete()
        db.commit()
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_tender_card_fields.py -v`
Expected: FAIL — `TypeError: 'location' is an invalid keyword argument for Tender` (column not yet on model).

- [ ] **Step 3: Add the columns to the model**

In `drpl-backend/app/models/tender.py`, immediately after the Phase-7 block (after the `fit_reasoning` column, ~line 73), add:
```python
    # Detailed-card fields (2026-07 redesign) — portal-stated metadata for the
    # card list. All nullable; old rows render "—". source_portal is only set
    # for aggregator listings (portal="tendertiger" + source_portal="gem").
    location = Column(String(255), nullable=True)                          # "Saran, Bihar, India"
    bid_type = Column(String(50), nullable=True)                           # NCB / GCB / Limited / Single
    source_portal = Column(String(50), nullable=True)                      # originating portal for aggregators
    category = Column(String(255), nullable=True)                          # portal-stated sector/category
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && pytest tests/test_tender_card_fields.py -v`
Expected: PASS (SQLite `create_all` adds the new columns to a fresh table).

- [ ] **Step 5: Add self-healing drift fixes for existing deployments**

In `drpl-backend/app/main.py`, inside `_apply_schema_drift_fixes()` (the list of SQL strings, after the `fit_reasoning` line ~169), add:
```python
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS location VARCHAR(255)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS bid_type VARCHAR(50)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS source_portal VARCHAR(50)",
        "ALTER TABLE tenders ADD COLUMN IF NOT EXISTS category VARCHAR(255)",
```

In `_add_missing_columns()` (the `migrations` list, near the other `("tenders", …)` entries ~line 550), add:
```python
        ("tenders", "location", "VARCHAR(255)"),
        ("tenders", "bid_type", "VARCHAR(50)"),
        ("tenders", "source_portal", "VARCHAR(50)"),
        ("tenders", "category", "VARCHAR(255)"),
```

- [ ] **Step 6: Create the Alembic revision**

Create `drpl-backend/alembic/versions/20260707_tender_card_fields.py`:
```python
"""Add detailed-card fields to tenders (location, bid_type, source_portal, category).

Revision ID: tender_card_fields_001
Revises: offline_doc_source_001
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa


revision = "tender_card_fields_001"
down_revision = "offline_doc_source_001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tenders") as batch_op:
        batch_op.add_column(sa.Column("location", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("bid_type", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("source_portal", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("category", sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table("tenders") as batch_op:
        batch_op.drop_column("category")
        batch_op.drop_column("source_portal")
        batch_op.drop_column("bid_type")
        batch_op.drop_column("location")
```

- [ ] **Step 7: Verify Alembic history is linear**

Run: `cd drpl-backend && alembic history | head -3`
Expected: `tender_card_fields_001` is the new head, revising `offline_doc_source_001`. (No DB connection needed to validate the chain; if `alembic history` requires config, confirm `down_revision` matches the prior head string instead.)

- [ ] **Step 8: Commit**

```bash
git add drpl-backend/app/models/tender.py drpl-backend/app/main.py drpl-backend/alembic/versions/20260707_tender_card_fields.py drpl-backend/tests/test_tender_card_fields.py
git commit -m "feat(backend): add location/bid_type/source_portal/category to Tender"
```

---

## Task 2: Backend — schemas expose the new fields

**Files:**
- Modify: `drpl-backend/app/schemas/__init__.py` (`TenderInput` ~line 73, `TenderResponse` ~line 123, `TenderDetailResponse` ~line 144)
- Test: `drpl-backend/tests/test_tender_card_fields.py` (extend)

**Interfaces:**
- Consumes: `Tender` columns from Task 1.
- Produces: `TenderInput.location/bidType/sourcePortal/category` (camelCase, optional); `TenderResponse` now includes `location`, `bid_type`, `source_portal`, `category`, `emd_amount`, `ai_summary`.

- [ ] **Step 1: Write the failing test**

Append to `drpl-backend/tests/test_tender_card_fields.py`:
```python
def test_tender_response_serializes_card_fields():
    from app.schemas import TenderResponse
    from datetime import datetime, timezone

    class _Row:
        id = 1; portal = "tendertiger"; tender_id = "X1"; title = "t"
        department = None; organisation = None; estimated_value = None
        emd_amount = 200000.0; closing_date = None; status = "open"
        ai_relevance_score = 0.9; ai_summary = "fit"; priority = "medium"
        workflow_status = "new"; assigned_to = None; eligibility_status = None
        created_at = datetime.now(timezone.utc)
        location = "Saran, Bihar, India"; bid_type = "NCB"
        source_portal = "gem"; category = "Railways Transport Services"

    out = TenderResponse.model_validate(_Row())
    assert out.location == "Saran, Bihar, India"
    assert out.bid_type == "NCB"
    assert out.source_portal == "gem"
    assert out.category == "Railways Transport Services"
    assert out.emd_amount == 200000.0
    assert out.ai_summary == "fit"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_tender_card_fields.py::test_tender_response_serializes_card_fields -v`
Expected: FAIL — `AttributeError`/validation error, or `out.location` missing (field not on `TenderResponse`).

- [ ] **Step 3: Add fields to the schemas**

In `TenderInput` (after `isEligibleIndicator` ~line 111):
```python
    # Detailed-card fields (2026-07)
    location: Optional[str] = None
    bidType: Optional[str] = None
    sourcePortal: Optional[str] = None
    category: Optional[str] = None
```

In `TenderResponse` (after `eligibility_status`, before `created_at` ~line 137), add:
```python
    emd_amount: Optional[float] = None
    ai_summary: Optional[str] = None
    location: Optional[str] = None
    bid_type: Optional[str] = None
    source_portal: Optional[str] = None
    category: Optional[str] = None
```

In `TenderDetailResponse` (after `eligibility_notes` ~line 172), add:
```python
    location: Optional[str] = None
    bid_type: Optional[str] = None
    source_portal: Optional[str] = None
    category: Optional[str] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && pytest tests/test_tender_card_fields.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/schemas/__init__.py drpl-backend/tests/test_tender_card_fields.py
git commit -m "feat(backend): expose card fields on tender schemas"
```

---

## Task 3: Backend — ingest mapping, relevance sort, new filters

**Files:**
- Modify: `drpl-backend/app/services/tender_service.py` (`ingest_tender_batch` ~line 96; `_update_existing_tender` ~line 173; `get_tenders` ~line 263)
- Modify: `drpl-backend/app/api/routes/tenders.py` (`list_tenders` ~line 32)
- Test: `drpl-backend/tests/test_tender_card_fields.py` (extend)

**Interfaces:**
- Consumes: `TenderInput` fields (Task 2), `Tender` columns (Task 1).
- Produces: `get_tenders(..., sort_by="relevance", bid_type=None, location=None)` supporting the new sort/filters.

- [ ] **Step 1: Write the failing tests**

Append to `drpl-backend/tests/test_tender_card_fields.py`:
```python
def test_ingest_maps_card_fields():
    from app.core.database import SessionLocal
    from app.schemas import TenderInput
    from app.services.tender_service import ingest_tender_batch, get_tenders
    from app.models.tender import Tender

    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.tender_id == "ING-1").delete(); db.commit()
        ti = TenderInput(
            portal="tendertiger", tenderId="ING-1", title="Goods transport",
            location="Saran, Bihar, India", bidType="NCB",
            sourcePortal="gem", category="Railways Transport Services",
        )
        res = ingest_tender_batch(db, [ti], user_id=1)
        assert res.new == 1
        row = db.query(Tender).filter(Tender.tender_id == "ING-1").first()
        assert row.location == "Saran, Bihar, India"
        assert row.bid_type == "NCB"
        assert row.source_portal == "gem"
        assert row.category == "Railways Transport Services"
    finally:
        db.query(Tender).filter(Tender.tender_id == "ING-1").delete(); db.commit(); db.close()


def test_get_tenders_relevance_sort_puts_nulls_last():
    from app.core.database import SessionLocal
    from app.models.tender import Tender
    from app.services.tender_service import get_tenders

    db = SessionLocal()
    try:
        db.query(Tender).filter(Tender.tender_id.in_(["REL-HI", "REL-NULL"])).delete(); db.commit()
        db.add(Tender(portal="gem", tender_id="REL-HI", title="hi", ai_relevance_score=0.95))
        db.add(Tender(portal="gem", tender_id="REL-NULL", title="null", ai_relevance_score=None))
        db.commit()
        rows = get_tenders(db, sort_by="relevance", limit=200)
        ids = [r.tender_id for r in rows]
        assert ids.index("REL-HI") < ids.index("REL-NULL")
    finally:
        db.query(Tender).filter(Tender.tender_id.in_(["REL-HI", "REL-NULL"])).delete(); db.commit(); db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd drpl-backend && pytest tests/test_tender_card_fields.py -v -k "ingest_maps or relevance_sort"`
Expected: FAIL — ingest ignores new fields (row.location is None) and `sort_by="relevance"` falls through to default ordering.

- [ ] **Step 3: Map new fields on insert**

In `ingest_tender_batch`, in the `new_tender = Tender(...)` constructor (after the Phase-7 `is_eligible_indicator=...` line ~135), add:
```python
                    # Detailed-card fields (2026-07)
                    location=tender_input.location,
                    bid_type=tender_input.bidType,
                    source_portal=tender_input.sourcePortal,
                    category=tender_input.category,
```

- [ ] **Step 4: Fill new fields on re-scrape when empty**

In `_update_existing_tender`, just before `existing.updated_at = ...` (~line 236), add (mirrors the fill-if-empty pattern used for `search_match_keyword`):
```python
    # Detailed-card fields — fill only when empty so a richer re-scrape self-heals
    # old rows without clobbering manually-corrected values.
    if getattr(new_data, "location", None) and not existing.location:
        existing.location = new_data.location
    if getattr(new_data, "bidType", None) and not existing.bid_type:
        existing.bid_type = new_data.bidType
    if getattr(new_data, "sourcePortal", None) and not existing.source_portal:
        existing.source_portal = new_data.sourcePortal
    if getattr(new_data, "category", None) and not existing.category:
        existing.category = new_data.category
```

- [ ] **Step 5: Add relevance sort + filters to `get_tenders`**

In `get_tenders`, add the two filter params to the signature (after `assigned_to`):
```python
    bid_type: Optional[str] = None,
    location: Optional[str] = None,
```
After the existing `if assigned_to:`/`if workflow_status:` filter block and before the sort handling, add:
```python
    if bid_type:
        query = query.filter(Tender.bid_type == bid_type)
    if location:
        query = query.filter(Tender.location.ilike(f"%{location}%"))
```
In the sort handling, add a branch for `relevance` (place it with the other `sort_by` cases):
```python
    if sort_by == "relevance":
        query = query.order_by(Tender.ai_relevance_score.desc().nullslast())
```

- [ ] **Step 6: Thread the filters through the route**

In `drpl-backend/app/api/routes/tenders.py`, `list_tenders`, add two query params (after `assigned_to`):
```python
    bid_type: Optional[str] = Query(None, description="Filter by bid type (NCB, GCB, Limited, …)"),
    location: Optional[str] = Query(None, description="Filter by location substring"),
```
Update the `sort_by` param description to mention `relevance`, and pass the two new args into `get_tenders(...)`:
```python
        assigned_to=assigned_to, sort_by=sort_by,
        bid_type=bid_type, location=location,
        limit=limit, offset=offset, include_archived=include_archived,
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd drpl-backend && pytest tests/test_tender_card_fields.py -v`
Expected: PASS (all four tests).

- [ ] **Step 8: Commit**

```bash
git add drpl-backend/app/services/tender_service.py drpl-backend/app/api/routes/tenders.py drpl-backend/tests/test_tender_card_fields.py
git commit -m "feat(backend): ingest card fields, relevance sort, bid_type/location filters"
```

---

## Task 4: Extension — capture card fields on aggregators

**Files:**
- Modify: `drpl-extension/src/utils/types.ts` (`TenderData` ~line 27)
- Modify: `drpl-extension/src/content-scripts/aggregators.ts` (`extractTenderTiger` ~line 94)
- Create: `drpl-extension/src/content-scripts/aggregators.tokens.test.ts`

**Interfaces:**
- Consumes: nothing prior.
- Produces: `TenderData.location/bidType/sourcePortal/category` (optional); exported `parseAggregatorTokens(text: string): { location?: string; bidType?: string; sourcePortal?: string }`.

- [ ] **Step 1: Add the optional fields to `TenderData`**

In `drpl-extension/src/utils/types.ts`, inside `TenderData` (after `isEligibleIndicator?` ~line 72), add:
```ts
  // --- Detailed-card fields (2026-07) ---
  location?: string;                     // "Saran, Bihar, India"
  bidType?: string;                      // NCB / GCB / Limited / Single
  sourcePortal?: string;                 // originating portal for aggregator listings (e.g. "gem")
  category?: string;                     // portal-stated sector/category
```

- [ ] **Step 2: Write the failing test**

Create `drpl-extension/src/content-scripts/aggregators.tokens.test.ts`:
```ts
import { describe, it, expect } from 'vitest';
import { parseAggregatorTokens } from './aggregators';

describe('parseAggregatorTokens', () => {
  it('extracts location, bid type, and source portal from a TenderTiger row', () => {
    const text = 'TID:97729103 Railways Transport Services Saran, Bihar, India GeM NCB ' +
      'Tender Invited For Goods Transportation Worth :INR 1.00 Cr EMD :INR 2.00 Lac Due Date :14 July 2026';
    const t = parseAggregatorTokens(text);
    expect(t.location).toBe('Saran, Bihar, India');
    expect(t.bidType).toBe('NCB');
    expect(t.sourcePortal).toBe('gem');
  });

  it('returns undefined fields when tokens are absent', () => {
    const t = parseAggregatorTokens('TID:1 some tender with no portal tokens');
    expect(t.bidType).toBeUndefined();
    expect(t.sourcePortal).toBeUndefined();
  });

  it('normalizes IREPS as source portal', () => {
    const t = parseAggregatorTokens('TID:2 Pune, Maharashtra, India IREPS GCB widget');
    expect(t.sourcePortal).toBe('ireps');
    expect(t.bidType).toBe('GCB');
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd drpl-extension && npx vitest run src/content-scripts/aggregators.tokens.test.ts`
Expected: FAIL — `parseAggregatorTokens is not a function` (not yet exported).

- [ ] **Step 4: Implement the token parser and wire it in**

In `drpl-extension/src/content-scripts/aggregators.ts`, add this exported helper near the other helpers (e.g. above `parseIndianValue` ~line 454):
```ts
/**
 * Pull card tokens out of an aggregator row's flat text. Pure + testable —
 * the DOM-dependent bits (category link) are handled by the caller.
 */
export function parseAggregatorTokens(text: string): { location?: string; bidType?: string; sourcePortal?: string } {
  const location = text.match(/([A-Z][a-z]+(?:,\s*[A-Za-z ]+)*,\s*India)/)?.[1];
  const bidType = text.match(/\b(NCB|GCB|LTE|Limited|Single|Global|Open|EOI)\b/)?.[1];
  const portalRaw = text.match(/\b(GeM|IREPS|CPPP|eProc|eProcurement)\b/i)?.[1];
  const portalMap: Record<string, string> = {
    gem: 'gem', ireps: 'ireps', cppp: 'cppp', eproc: 'eproc', eprocurement: 'eproc',
  };
  const sourcePortal = portalRaw ? portalMap[portalRaw.toLowerCase()] : undefined;
  return { location, bidType, sourcePortal };
}
```
Then, inside `extractTenderTiger`, replace the existing inline `locationMatch`/`location` block (~lines 156-158) and the `const tender: TenderData = { ... }` object so the tokens are used. Specifically, after `const emdAmount = parseIndianValue(emdText);` add:
```ts
      const { location, bidType, sourcePortal } = parseAggregatorTokens(text);
      // Category: the sector link text (org link already captured as `org`).
      const category = org || undefined;
```
and in the `tender` object literal, add these keys (after `status: 'Open',`):
```ts
        location,
        bidType,
        sourcePortal,
        category,
```
(Remove the now-dead `locationMatch`/`location` lines that were previously computed but unused.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd drpl-extension && npx vitest run src/content-scripts/aggregators.tokens.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 6: Build and verify the bundle is self-contained**

Run: `cd drpl-extension && npm run build`
Then verify no ES imports leaked into the content-script bundle:
Run: `grep -c "^import\|from '" dist/content-scripts/aggregators.js || echo "clean"`
Expected: `clean` (or `0`). If imports remain, the `bundle-content-scripts` plugin needs the helper inlined — confirm `parseAggregatorTokens` lives in `aggregators.ts` itself (it does), not a shared module.

- [ ] **Step 7: Commit**

```bash
git add drpl-extension/src/utils/types.ts drpl-extension/src/content-scripts/aggregators.ts drpl-extension/src/content-scripts/aggregators.tokens.test.ts
git commit -m "feat(ext): capture location/bidType/sourcePortal/category on aggregators"
```

---

## Task 5: Extension — best-effort capture on GeM & IREPS

**Files:**
- Modify: `drpl-extension/src/content-scripts/gem.ts`
- Modify: `drpl-extension/src/content-scripts/ireps.ts`
- Modify: `drpl-extension/src/config/selectors.json` (if new selector keys are added)

**Interfaces:**
- Consumes: `TenderData` fields (Task 4).
- Produces: populated `location`/`bidType`/`category` on GeM/IREPS `TenderData` where the DOM exposes them. `sourcePortal` stays undefined (these are direct sources, not aggregators).

> These portals have no jsdom fixture in-repo, so this task verifies by a live manual scrape rather than a unit test. Keep additions defensive — never throw if a field is absent.

- [ ] **Step 1: Add fields to the GeM extractor**

In `drpl-extension/src/content-scripts/gem.ts`, locate where the `TenderData` object is assembled for each row (search for `portal: 'gem'`). Add, using existing selector/text access already present in that function:
```ts
        // Detailed-card fields — undefined when the DOM doesn't expose them.
        location: locationText || undefined,   // buyer address / delivery location if scraped
        bidType: bidTypeText || undefined,      // GeM "Bid Type" field if present
        category: categoryText || undefined,    // GeM category if present
```
Where `locationText`/`bidTypeText`/`categoryText` are read from the row using the same pattern the file already uses for other fields (e.g. `row.querySelector(sel)?.textContent?.trim()`). If a portal field genuinely isn't on the listing DOM, leave that key omitted rather than inventing a selector.

- [ ] **Step 2: Add fields to the IREPS extractor**

In `drpl-extension/src/content-scripts/ireps.ts`, in the equivalent `TenderData` assembly (search for `portal: 'ireps'`), add:
```ts
        location: locationText || undefined,    // work/consignee location if scraped
        bidType: bidTypeText || undefined,       // tender type (Open/Limited/Global) if present
        category: categoryText || undefined,     // commodity/category if present
```

- [ ] **Step 3: Register any new OTA selectors**

If Steps 1-2 introduced new CSS selectors, add matching keys to `drpl-extension/src/config/selectors.json` under the relevant portal block so the shipped copy and the OTA copy agree. If no new selectors were needed (fields read from existing text), skip this step and note it in the commit body.

- [ ] **Step 4: Build and verify self-contained bundles**

Run: `cd drpl-extension && npm run build`
Run: `for f in gem ireps; do grep -c "^import" dist/content-scripts/$f.js; done`
Expected: `0` for both.

- [ ] **Step 5: Manual live verification**

Load `dist/` unpacked in Chrome (`chrome://extensions` → reload). Log into GeM and IREPS, run a scrape, and in the service-worker console confirm the POSTed `TenderData` carries `location`/`bidType`/`category` where the portal shows them. Document which fields each portal actually exposed in the commit body (portals vary; "—" on the card is acceptable where absent).

- [ ] **Step 6: Commit**

```bash
git add drpl-extension/src/content-scripts/gem.ts drpl-extension/src/content-scripts/ireps.ts drpl-extension/src/config/selectors.json
git commit -m "feat(ext): best-effort location/bidType/category on GeM & IREPS"
```

> **Backend OTA push reminder:** if `selectors.json` changed, redeploy the backend so its OTA-served copy matches (CLAUDE.md).

---

## Task 6: Frontend — types, API passthrough, hook deps

**Files:**
- Modify: `drpl-frontend/src/types/tender.ts` (`Tender` ~line 1, `TenderFilters` ~line 58)
- Modify: `drpl-frontend/src/lib/api.ts` (`getTenders` ~line 70)
- Modify: `drpl-frontend/src/hooks/useTenders.ts` (dep array ~line 17)

**Interfaces:**
- Consumes: backend `TenderResponse` fields (Task 2).
- Produces: `Tender` type with `location`, `bid_type`, `source_portal`, `category`, `emd_amount`, `ai_summary`; `TenderFilters` with `bid_type`, `location`.

> Frontend has no unit-test runner; verification for Tasks 6-9 is a clean `tsc -b` build (part of `npm run build`) plus driving the app (verify skill).

- [ ] **Step 1: Extend the `Tender` and `TenderFilters` types**

In `drpl-frontend/src/types/tender.ts`, add to the `Tender` interface (after `created_at`):
```ts
  location: string | null;
  bid_type: string | null;
  source_portal: string | null;
  category: string | null;
  emd_amount: number | null;
  ai_summary: string | null;
```
Add to `TenderFilters` (after `workflow_status`):
```ts
  bid_type?: string;
  location?: string;
```

- [ ] **Step 2: Pass the new filters through `getTenders`**

In `drpl-frontend/src/lib/api.ts`, in `getTenders`, after `if (filters.workflow_status) …`, add:
```ts
  if (filters.bid_type) params.bid_type = filters.bid_type;
  if (filters.location) params.location = filters.location;
```

- [ ] **Step 3: Add the filters to the hook dependency array**

In `drpl-frontend/src/hooks/useTenders.ts`, extend the `useCallback` dep array to include the two new fields:
```ts
  }, [filters.portal, filters.department, filters.status, filters.priority, filters.workflow_status, filters.assigned_to, filters.sort_by, filters.bid_type, filters.location, filters.limit, filters.offset]);
```

- [ ] **Step 4: Verify the build compiles**

Run: `cd drpl-frontend && npm run build`
Expected: `tsc -b` passes (no type errors) and vite build succeeds.

- [ ] **Step 5: Commit**

```bash
git add drpl-frontend/src/types/tender.ts drpl-frontend/src/lib/api.ts drpl-frontend/src/hooks/useTenders.ts
git commit -m "feat(frontend): tender card field types + filter passthrough"
```

---

## Task 7: Frontend — `TenderCard` component

**Files:**
- Create: `drpl-frontend/src/components/tenders/TenderCard.tsx`

**Interfaces:**
- Consumes: `Tender` type (Task 6); `analyzeTender` from `../../lib/api`; `formatCurrency`, `formatDate` from `../../lib/formatters`; `date-fns` `differenceInDays`/`parseISO`.
- Produces: `export default function TenderCard(props: { tender: Tender; selectable?: boolean; selected?: boolean; onToggleSelect?: (id: number) => void })`.

- [ ] **Step 1: Create the component**

Create `drpl-frontend/src/components/tenders/TenderCard.tsx`:
```tsx
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { MapPin, Building2, Clock, FileText, Sparkles, LayoutGrid, ChevronDown, Loader2, User, Flag, Archive } from 'lucide-react';
import type { Tender } from '../../types/tender';
import { analyzeTender } from '../../lib/api';
import { formatCurrency, formatDate } from '../../lib/formatters';
import { portalLabel } from '../../lib/formatters';
import { differenceInDays, parseISO } from 'date-fns';

interface TenderCardProps {
  tender: Tender;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (id: number) => void;
}

function scoreClasses(pct: number): string {
  if (pct >= 70) return 'text-emerald-600 dark:text-emerald-400';
  if (pct >= 40) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

function DaysToGo({ closingDate }: { closingDate: string }) {
  const days = differenceInDays(parseISO(closingDate), new Date());
  const base = 'inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold whitespace-nowrap';
  if (days < 0) return <span className={`${base} bg-red-50 text-red-600 dark:bg-red-500/15 dark:text-red-400`}><Clock size={12} />Expired</span>;
  if (days === 0) return <span className={`${base} bg-red-50 text-red-600 dark:bg-red-500/15 dark:text-red-400`}><Clock size={12} />Today</span>;
  if (days <= 3) return <span className={`${base} bg-red-50 text-red-600 dark:bg-red-500/15 dark:text-red-400`}><Clock size={12} />{days} days</span>;
  if (days <= 7) return <span className={`${base} bg-amber-50 text-amber-600 dark:bg-amber-500/15 dark:text-amber-400`}><Clock size={12} />{days} days</span>;
  return <span className={`${base} bg-muted text-muted-foreground`}><Clock size={12} />{days} days</span>;
}

export default function TenderCard({ tender, selectable, selected, onToggleSelect }: TenderCardProps) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);

  const pct = tender.ai_relevance_score != null ? Math.round(tender.ai_relevance_score * 100) : null;
  // source_portal only meaningful when it differs from the aggregator portal.
  const showOrigin = !!tender.source_portal && tender.source_portal !== tender.portal;

  const runAnalyze = async () => {
    setAnalyzing(true);
    try { await analyzeTender(tender.id); navigate(`/tenders/${tender.id}`); }
    finally { setAnalyzing(false); }
  };

  return (
    <article className={`grid grid-cols-[72px_1fr_auto] bg-card border rounded-lg overflow-hidden transition-shadow ${open ? 'border-accent/60 shadow-md' : 'border-border hover:border-accent/40 hover:shadow-md'}`}>
      {/* Left rail: match % + optional select checkbox */}
      <div className="flex flex-col items-center justify-center gap-1 p-4 border-r border-border bg-muted/30 relative">
        {selectable && (
          <input
            type="checkbox"
            checked={!!selected}
            onChange={() => onToggleSelect?.(tender.id)}
            onClick={(e) => e.stopPropagation()}
            className="absolute top-2 left-2 h-4 w-4 rounded border-input text-accent focus:ring-ring cursor-pointer"
            aria-label={`Select tender ${tender.tender_id}`}
          />
        )}
        {pct != null ? (
          <>
            <span className={`text-lg font-extrabold leading-none ${scoreClasses(pct)}`}>{pct}%</span>
            <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground font-bold">Match</span>
          </>
        ) : (
          <span className="text-sm text-muted-foreground">—</span>
        )}
      </div>

      {/* Body */}
      <div className="p-4 min-w-0 cursor-pointer" onClick={() => setOpen(o => !o)}>
        <div className="flex items-center gap-2 flex-wrap mb-1.5 text-xs text-muted-foreground">
          <span className="font-mono font-semibold">{tender.tender_id}</span>
          {tender.location && (<><span className="w-1 h-1 rounded-full bg-muted-foreground/50" /><span className="inline-flex items-center gap-1"><MapPin size={12} />{tender.location}</span></>)}
        </div>
        <h2 className="text-[0.98rem] font-bold tracking-tight leading-snug mb-1.5 whitespace-normal break-words">{tender.title}</h2>
        <p className="text-sm text-muted-foreground mb-3 inline-flex items-center gap-1.5">
          <Building2 size={13} className="opacity-80" />{tender.organisation || '—'}{tender.department ? ` · ${tender.department}` : ''}
        </p>
        <div className="flex items-center gap-1.5 flex-wrap">
          {showOrigin ? (
            <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-muted text-muted-foreground border border-border">via {portalLabel(tender.portal)} · {portalLabel(tender.source_portal!)}</span>
          ) : (
            <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-accent/12 text-accent border border-accent/25">{portalLabel(tender.portal)}</span>
          )}
          {tender.bid_type && <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-violet-500/12 text-violet-600 dark:text-violet-400 border border-violet-500/25">{tender.bid_type}</span>}
          {tender.category && <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-muted text-muted-foreground border border-border">{tender.category}</span>}
          <span className="inline-flex items-center px-2 py-0.5 rounded-md text-[0.68rem] font-bold bg-emerald-500/14 text-emerald-700 dark:text-emerald-400 border border-emerald-500/25">{tender.status}</span>
        </div>
        <div className="flex items-center flex-wrap mt-3">
          <div className="flex flex-col pr-4 mr-4 border-r border-border">
            <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground font-bold">Worth</span>
            <span className="text-sm font-bold tabular-nums">{formatCurrency(tender.estimated_value)}</span>
          </div>
          <div className="flex flex-col pr-4 mr-4 border-r border-border">
            <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground font-bold">EMD</span>
            <span className="text-sm font-bold tabular-nums">{tender.emd_amount != null ? formatCurrency(tender.emd_amount) : '—'}</span>
          </div>
          <div className="flex flex-col">
            <span className="text-[0.6rem] uppercase tracking-wide text-muted-foreground font-bold">Due date</span>
            <span className="text-sm font-bold tabular-nums">{formatDate(tender.closing_date)}</span>
          </div>
        </div>
      </div>

      {/* Right: days-to-go + expander */}
      <div className="flex flex-col items-end justify-between p-4 gap-3">
        {tender.closing_date && <DaysToGo closingDate={tender.closing_date} />}
        <button onClick={() => setOpen(o => !o)} className="inline-flex items-center gap-1 text-xs text-muted-foreground font-semibold hover:text-foreground" aria-expanded={open}>
          Details <ChevronDown size={16} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
        </button>
      </div>

      {/* Expanded detail */}
      {open && (
        <div className="col-span-3 border-t border-border bg-muted/20 px-5 py-5">
          <div className="grid grid-cols-1 md:grid-cols-[1.5fr_1fr] gap-6">
            <div>
              <p className="text-[0.62rem] uppercase tracking-wider text-muted-foreground font-extrabold mb-1.5">Scope</p>
              <p className="text-sm text-foreground/90 leading-relaxed">{tender.title}</p>
            </div>
            <div>
              {tender.ai_summary && (
                <div className="bg-accent/6 border border-accent/20 rounded-lg px-3 py-3">
                  <p className="text-[0.62rem] uppercase tracking-wider text-accent font-extrabold mb-1.5">AI fit assessment</p>
                  <p className="text-sm leading-relaxed">{tender.ai_summary}</p>
                </div>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2.5 flex-wrap mt-5 pt-4 border-t border-dashed border-border">
            <button onClick={runAnalyze} disabled={analyzing} className="inline-flex items-center gap-1.5 text-sm font-bold px-4 py-2 rounded-lg bg-accent text-accent-foreground hover:bg-accent/90 disabled:opacity-50">
              {analyzing ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}{analyzing ? 'Analyzing…' : 'Analyze'}
            </button>
            <button onClick={() => navigate(`/tenders/${tender.id}/workspace/annexures`)} className="inline-flex items-center gap-1.5 text-sm font-bold px-4 py-2 rounded-lg border border-border hover:border-accent hover:text-accent">
              <LayoutGrid size={15} />Open workspace
            </button>
            <div className="flex-1" />
            <button onClick={() => navigate(`/tenders/${tender.id}`)} title="Full detail" className="inline-flex items-center gap-1.5 text-sm font-semibold px-3 py-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/60"><FileText size={15} /></button>
            <button title="Assign" className="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/60"><User size={15} /></button>
            <button title="Set priority" className="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/60"><Flag size={15} /></button>
            <button title="Archive" className="p-2 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted/60"><Archive size={15} /></button>
          </div>
        </div>
      )}
    </article>
  );
}
```

- [ ] **Step 2: Verify the build compiles**

Run: `cd drpl-frontend && npm run build`
Expected: `tsc -b` passes. If `portalLabel` import path/usage errors, confirm it's exported from `src/lib/formatters.ts` (it is — `PortalBadge.tsx` imports it there).

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/tenders/TenderCard.tsx
git commit -m "feat(frontend): TenderCard detailed expandable card"
```

---

## Task 8: Frontend — `TenderCardList` container

**Files:**
- Create: `drpl-frontend/src/components/tenders/TenderCardList.tsx`

**Interfaces:**
- Consumes: `TenderCard` (Task 7); `EmptyState` from `../ui/EmptyState`; `Tender` type.
- Produces: `export default function TenderCardList(props: { tenders: Tender[]; selectedIds?: Set<number>; onToggleSelect?: (id: number) => void; onToggleAll?: () => void })` — a drop-in replacement for `TenderTable`'s prop shape.

- [ ] **Step 1: Create the component**

Create `drpl-frontend/src/components/tenders/TenderCardList.tsx`:
```tsx
import type { Tender } from '../../types/tender';
import TenderCard from './TenderCard';
import EmptyState from '../ui/EmptyState';

interface TenderCardListProps {
  tenders: Tender[];
  selectedIds?: Set<number>;
  onToggleSelect?: (id: number) => void;
  onToggleAll?: () => void;
}

export default function TenderCardList({ tenders, selectedIds, onToggleSelect, onToggleAll }: TenderCardListProps) {
  if (tenders.length === 0) {
    return <EmptyState message="No tenders found matching your filters" />;
  }
  const selectable = !!onToggleSelect;
  const allSelected = selectable && tenders.every(t => selectedIds?.has(t.id));

  return (
    <div className="space-y-3">
      {selectable && (
        <label className="flex items-center gap-2 text-xs text-muted-foreground font-medium px-1">
          <input
            type="checkbox"
            checked={!!allSelected}
            onChange={onToggleAll}
            className="h-4 w-4 rounded border-input text-accent focus:ring-ring cursor-pointer"
            aria-label="Select all visible tenders"
          />
          Select all visible
        </label>
      )}
      {tenders.map(t => (
        <TenderCard
          key={t.id}
          tender={t}
          selectable={selectable}
          selected={selectedIds?.has(t.id)}
          onToggleSelect={onToggleSelect}
        />
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Verify the build compiles**

Run: `cd drpl-frontend && npm run build`
Expected: `tsc -b` passes.

- [ ] **Step 3: Commit**

```bash
git add drpl-frontend/src/components/tenders/TenderCardList.tsx
git commit -m "feat(frontend): TenderCardList container"
```

---

## Task 9: Frontend — wire the page, restyle filters, remove the table

**Files:**
- Modify: `drpl-frontend/src/pages/TendersPage.tsx` (import + `<TenderTable>` ~line 270; default sort ~line 18)
- Modify: `drpl-frontend/src/components/tenders/TenderFilters.tsx` (add bid_type/location; default sort)
- Delete: `drpl-frontend/src/components/tenders/TenderRow.tsx`, `drpl-frontend/src/components/tenders/TenderTable.tsx`

**Interfaces:**
- Consumes: `TenderCardList` (Task 8); `TenderFilters` type with `bid_type`/`location` (Task 6).

- [ ] **Step 1: Swap the list container and default sort in `TendersPage`**

In `drpl-frontend/src/pages/TendersPage.tsx`:
- Replace the import `import TenderTable from '../components/tenders/TenderTable';` with `import TenderCardList from '../components/tenders/TenderCardList';`.
- In the initial `useState<Filters>` (~line 18), set the default sort:
```tsx
  const [filters, setFilters] = useState<Filters>({
    limit: PAGE_SIZE,
    offset: 0,
    sort_by: 'relevance',
  });
```
- Replace the `<TenderTable ... />` JSX (~line 270) with:
```tsx
            <TenderCardList
              tenders={tenders}
              selectedIds={advanced ? selectedIds : undefined}
              onToggleSelect={advanced ? toggleSelect : undefined}
              onToggleAll={advanced ? toggleAll : undefined}
            />
```

- [ ] **Step 2: Add bid_type + location + Match sort to `TenderFilters`**

In `drpl-frontend/src/components/tenders/TenderFilters.tsx`:
- Add to the props type: `bid_type?: string; location?: string;` on the `onApply` filter object.
- Add state: `const [bidType, setBidType] = useState('');` and `const [location, setLocation] = useState('');`.
- Default the sort state to relevance: `const [sortBy, setSortBy] = useState('relevance');`.
- Include them in `handleApply`'s `onApply({ ... })` call: `bid_type: bidType || undefined, location: location || undefined,`.
- Reset them in `handleReset` (`setBidType(''); setLocation(''); setSortBy('relevance');`).
- Add a Bid type `<select>` (options: `''`→All, `NCB`, `GCB`, `Limited`, `Single`, `Global`), a Location text `<input>` (placeholder "City / state…", Enter triggers `handleApply`), and make the Sort `<select>` lead with:
```tsx
          <option value="relevance">Best match</option>
          <option value="closing_date">Closing Date</option>
          <option value="created_at">Newest First</option>
          <option value="priority">Priority</option>
```
Follow the exact markup pattern of the existing `<select>` blocks (same className strings) so styling stays consistent.

- [ ] **Step 3: Delete the obsolete table components**

Run:
```bash
git rm drpl-frontend/src/components/tenders/TenderRow.tsx drpl-frontend/src/components/tenders/TenderTable.tsx
```

- [ ] **Step 4: Verify no dangling imports and the build compiles**

Run: `cd drpl-frontend && grep -rn "TenderTable\|TenderRow" src/ || echo "no refs"`
Expected: `no refs`.
Run: `cd drpl-frontend && npm run build`
Expected: `tsc -b` passes and vite build succeeds.

- [ ] **Step 5: Drive the app to verify behavior (verify skill)**

Start the frontend (`npm run dev`) against a backend with tenders. Confirm: cards render collapsed; clicking expands one; the match-sorted order is highest-% first; toggling Advanced shows the select-all + per-card checkboxes and the batch bar still works; Bid type / Location filters refetch. Check both light and dark themes.

- [ ] **Step 6: Commit**

```bash
git add drpl-frontend/src/pages/TendersPage.tsx drpl-frontend/src/components/tenders/TenderFilters.tsx
git commit -m "feat(frontend): swap tender table for detailed card list + toolbar filters"
```

---

## Self-Review (against the spec)

**Spec coverage:**
- §4 model columns → Task 1. ✅
- §4 three-place self-healing (Alembic + drift + sqlite) → Task 1 Steps 5-6. ✅
- §5 extension type + aggregator scraper → Task 4; GeM/IREPS → Task 5. ✅
- §5.2 selectors.json + OTA push → Task 5 Step 3 + reminder. ✅
- §5.3 self-contained bundle check → Task 4 Step 6, Task 5 Step 4. ✅
- §6 TenderInput/TenderResponse/TenderDetailResponse (incl. emd_amount, ai_summary) → Task 2. ✅
- §6 ingest mapping + fill-if-empty + relevance sort + bid_type/location filters + route params → Task 3. ✅
- §7.1 frontend types + filters → Task 6. ✅
- §7.2 TenderCard (rail/badges/money/expand/actions, violet bid badge, source guard) → Task 7. ✅
- §7.2 keep-then-remove old table → Tasks 8-9. ✅
- §7.3 page swap preserving batch tools + default relevance sort → Task 9. ✅
- §7.4 toolbar filters → Task 9 Step 2. ✅
- §7.5 useTenders deps → Task 6 Step 3. ✅
- §8 edge cases: null score (rail "—", Task 7), source_portal===portal guard (Task 7 `showOrigin`), null EMD "—" (Task 7), title wrap (Task 7). ✅
- §9 verification: backend pytest (Tasks 1-3), extension vitest+build (Task 4), frontend tsc+drive (Tasks 6-9). ✅
- §10 rollout order backend→extension→frontend → task order. ✅

**Placeholder scan:** No TBD/TODO; every code step shows real code; edge-case handling is concrete, not "add validation". ✅

**Type consistency:** `parseAggregatorTokens` signature identical in Task 4 interface + test + impl. `Tender` field names (`bid_type`, `source_portal`, `emd_amount`, `ai_summary`) consistent across Tasks 6-9. `TenderCard`/`TenderCardList` prop shapes match `TendersPage` usage in Task 9. Backend camelCase input (`bidType`, `sourcePortal`) vs snake_case columns (`bid_type`, `source_portal`) is intentional and consistently mapped in Task 3. ✅
