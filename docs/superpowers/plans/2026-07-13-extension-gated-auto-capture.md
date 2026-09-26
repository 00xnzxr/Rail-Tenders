# Extension Gated Auto-Capture (Piece B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a scrape, the extension auto-runs the existing document hunt for tenders that pass a strict local scope filter, gated by a default-OFF feature flag.

**Architecture:** A pure `utils/scope-matcher.ts` decides scope (keyword AND ministry AND value, fail-closed but visible). The service worker filters the scraped batch and, when `features.auto_capture` is ON, runs the existing `processDocumentHunt` on the capped, in-scope subset (sequential, throttled) without wiping the manual-hunt queue for the remainder. One backend line surfaces the flag.

**Tech Stack:** TypeScript, Chrome MV3 extension, Vite, Vitest. Backend: FastAPI (`extension.py`, `config.py`).

## Global Constraints

- **Spec:** [`docs/superpowers/specs/2026-07-13-extension-gated-auto-capture-design.md`](../specs/2026-07-13-extension-gated-auto-capture-design.md) — authoritative.
- **Strict scope rule:** in-scope requires ALL of keyword-match (≥1 keyword_group keyword in title/department/category AND no exclusion_terms), ministry-match (target_ministries in department/organisation/sourcePortal), and value-in-range (`[value_min, value_max]`, null bound = open). A field that is null/empty/unparseable → that check does NOT pass, and the field name is recorded in `missing[]`. Exclusion terms apply even when other fields are missing.
- **Feature flag default OFF:** `extension_auto_capture_enabled: bool = False`; surfaced as `features.auto_capture`. When OFF, behavior is byte-for-byte identical to today.
- **Per-session cap:** `AUTO_CAPTURE_MAX_PER_SESSION = 10`. Overflow + out-of-scope tenders MUST remain in `pendingDocumentHunt` for the manual button. Never silently truncate — log/notify the overflow count.
- **Sequential + throttled:** reuse `processDocumentHunt`'s existing serial loop + `sleep(500)`. No parallel tabs.
- **`forceDownload` override:** auto-capture downloads bytes even if `settings.downloadDocumentsEnabled` is off (otherwise the feature is a dead end). Manual hunt keeps respecting the toggle.
- **MV3 build:** the new module is imported ONLY by the service worker (ES-module context) — do not import it from content scripts. After build, `dist/content-scripts/*.js` must contain no `import` statements (verify, per CLAUDE.md).
- **Commands:** run from `drpl-extension/`: `npm run test` (vitest), `npm run build`. Backend from `drpl-backend/`: `venv/Scripts/python.exe -m pytest tests/`.
- **Branch:** `feat/extension-gated-auto-capture` (already created, spec committed). Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `drpl-extension/src/utils/scope-matcher.ts` | Pure `matchesScope(tender, profile) → ScopeMatch` + `splitByScope` cap helper | Create |
| `drpl-extension/src/utils/scope-matcher.test.ts` | Unit tests for the matcher + cap split | Create |
| `drpl-extension/src/utils/types.ts` | Add `auto_capture` to the extension features type | Modify |
| `drpl-extension/src/background/service-worker.ts` | Auto-capture wiring; `processDocumentHunt` gains `{ clearQueue, forceDownload }` options | Modify (`processPerTenderAtomic` end ~:256; `processDocumentHunt` sig ~:284, toggle :340, clear :421) |
| `drpl-backend/app/core/config.py` | `extension_auto_capture_enabled: bool = False` | Modify (~:63) |
| `drpl-backend/app/api/routes/extension.py` | Surface flag in `features` map | Modify (:281-288) |
| `docs/RUNBOOK-extension-auto-capture.md` | Load-unpacked smoke runbook | Create |

---

## Task 1: `scope-matcher.ts` — pure matcher + tests (TDD)

**Files:**
- Create: `drpl-extension/src/utils/scope-matcher.ts`
- Create: `drpl-extension/src/utils/scope-matcher.test.ts`

**Interfaces:**
- Consumes: `TenderData` (`title: string`, `department: string`, `organisation: string`, `estimatedValue: number|null`, `category?: string`, `sourcePortal?: string`), `ScopeProfile` (`keyword_groups: {label,keywords}[]`, `exclusion_terms: string[]`, `target_ministries: string[]`, `value_min: number|null`, `value_max: number|null`) — both from `./types`.
- Produces: `matchesScope(tender, profile): ScopeMatch`; `splitByScope(tenders, profile, cap): { capture: TenderData[]; overflow: TenderData[]; skipped: TenderData[] }`; `interface ScopeMatch { inScope: boolean; reasons: string[]; missing: string[] }`.

- [ ] **Step 1: Write the failing tests**

Create `drpl-extension/src/utils/scope-matcher.test.ts`:
```ts
import { describe, it, expect } from 'vitest';
import { matchesScope, splitByScope } from './scope-matcher';
import type { TenderData, ScopeProfile } from './types';

const profile: ScopeProfile = {
  name: 'default',
  keyword_groups: [{ label: 'pumps', keywords: ['pump', 'motor'] }],
  exclusion_terms: ['scrap'],
  target_ministries: ['Railways'],
  value_min: 100000,
  value_max: 5000000,
  relevance_threshold: 0.6,
  is_active: true,
  updated_at: null,
};

function tender(over: Partial<TenderData>): TenderData {
  return {
    portal: 'ireps', tenderId: 'T1', title: 'Supply of pump sets',
    department: 'Railways', organisation: 'NR', estimatedValue: 500000,
    ...over,
  } as TenderData;
}

describe('matchesScope', () => {
  it('in-scope when keyword AND ministry AND value all match', () => {
    const m = matchesScope(tender({}), profile);
    expect(m.inScope).toBe(true);
    expect(m.missing).toEqual([]);
  });

  it('out when an exclusion term is present', () => {
    const m = matchesScope(tender({ title: 'pump scrap disposal' }), profile);
    expect(m.inScope).toBe(false);
  });

  it('out + missing:ministry when department/organisation do not match', () => {
    const m = matchesScope(tender({ department: 'PWD', organisation: 'State' }), profile);
    expect(m.inScope).toBe(false);
    expect(m.missing).toContain('ministry');
  });

  it('out + missing:value when estimatedValue is null', () => {
    const m = matchesScope(tender({ estimatedValue: null }), profile);
    expect(m.inScope).toBe(false);
    expect(m.missing).toContain('value');
  });

  it('out when no keyword matches', () => {
    const m = matchesScope(tender({ title: 'office stationery' }), profile);
    expect(m.inScope).toBe(false);
  });

  it('out when value is out of range', () => {
    const m = matchesScope(tender({ estimatedValue: 50000 }), profile);
    expect(m.inScope).toBe(false);
  });

  it('value passes with open-ended (null) bounds', () => {
    const open = { ...profile, value_min: null, value_max: null };
    const m = matchesScope(tender({ estimatedValue: 9_000_000 }), open);
    expect(m.inScope).toBe(true);
  });
});

describe('splitByScope', () => {
  it('caps capture, routes rest to overflow, non-matching to skipped', () => {
    const inScope = Array.from({ length: 12 }, (_, i) => tender({ tenderId: `IN-${i}` }));
    const out = tender({ tenderId: 'OUT', title: 'stationery' });
    const res = splitByScope([...inScope, out], profile, 10);
    expect(res.capture).toHaveLength(10);
    expect(res.overflow).toHaveLength(2);
    expect(res.skipped.map((t) => t.tenderId)).toContain('OUT');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd drpl-extension && npm run test -- scope-matcher`
Expected: FAIL — `matchesScope`/`splitByScope` not exported / module missing.

- [ ] **Step 3: Implement `scope-matcher.ts`**

Create `drpl-extension/src/utils/scope-matcher.ts`:
```ts
import type { TenderData, ScopeProfile } from './types';

export interface ScopeMatch {
  inScope: boolean;
  reasons: string[];
  missing: string[];
}

function norm(s: string | null | undefined): string {
  return (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
}

/** Strict scope gate: keyword AND ministry AND value must all pass.
 * Missing/unparseable fields fail their check and are listed in `missing`.
 * Exclusion terms apply regardless of other fields. */
export function matchesScope(tender: TenderData, profile: ScopeProfile): ScopeMatch {
  const reasons: string[] = [];
  const missing: string[] = [];

  const haystack = [norm(tender.title), norm(tender.department), norm(tender.category)]
    .filter(Boolean)
    .join(' | ');

  // Exclusion first — an excluded tender is never in scope.
  const excluded = (profile.exclusion_terms || []).some((t) => t && haystack.includes(norm(t)));
  if (excluded) {
    return { inScope: false, reasons: ['excluded'], missing };
  }

  // 1. Keyword
  let keywordOk = false;
  if (!haystack) {
    missing.push('keyword');
  } else {
    for (const g of profile.keyword_groups || []) {
      const hit = (g.keywords || []).find((k) => k && haystack.includes(norm(k)));
      if (hit) { keywordOk = true; reasons.push(`keyword:${hit}`); break; }
    }
    if (!keywordOk) missing.push('keyword');
  }

  // 2. Ministry
  const ministryHay = [norm(tender.department), norm(tender.organisation), norm(tender.sourcePortal)]
    .filter(Boolean)
    .join(' | ');
  let ministryOk = false;
  if (!ministryHay) {
    missing.push('ministry');
  } else {
    const hit = (profile.target_ministries || []).find((m) => m && ministryHay.includes(norm(m)));
    if (hit) { ministryOk = true; reasons.push(`ministry:${hit}`); }
    else missing.push('ministry');
  }

  // 3. Value
  let valueOk = false;
  const v = tender.estimatedValue;
  if (v === null || v === undefined || Number.isNaN(v)) {
    missing.push('value');
  } else {
    const minOk = profile.value_min === null || v >= profile.value_min;
    const maxOk = profile.value_max === null || v <= profile.value_max;
    if (minOk && maxOk) { valueOk = true; reasons.push('value:in-range'); }
    else missing.push('value');
  }

  return { inScope: keywordOk && ministryOk && valueOk, reasons, missing };
}

/** Partition a scraped batch: `capture` (in-scope, up to `cap`), `overflow`
 * (in-scope beyond the cap), `skipped` (out of scope). */
export function splitByScope(
  tenders: TenderData[],
  profile: ScopeProfile,
  cap: number,
): { capture: TenderData[]; overflow: TenderData[]; skipped: TenderData[] } {
  const inScope: TenderData[] = [];
  const skipped: TenderData[] = [];
  for (const t of tenders) {
    if (matchesScope(t, profile).inScope) inScope.push(t);
    else skipped.push(t);
  }
  return { capture: inScope.slice(0, cap), overflow: inScope.slice(cap), skipped };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd drpl-extension && npm run test -- scope-matcher`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-extension/src/utils/scope-matcher.ts drpl-extension/src/utils/scope-matcher.test.ts
git commit -m "feat(ext): pure scope-matcher (strict keyword AND ministry AND value)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Feature-flag plumbing (backend + extension type)

**Files:**
- Modify: `drpl-backend/app/core/config.py` (~:63, near `eager_analysis_enabled`)
- Modify: `drpl-backend/app/api/routes/extension.py` (:281-288)
- Modify: `drpl-extension/src/utils/types.ts` (extension features type)
- Modify: `drpl-extension/src/utils/api-client.ts` (`syncScopeProfile`, :127-136 — also persist `features`)

**Interfaces:**
- Produces: backend `/api/extension/config` `features.auto_capture: bool`; extension-side `features.auto_capture?: boolean` on whatever type models the config response; `chrome.storage.local.extensionFeatures` holding the `features` map.

**VERIFIED FACT (critical):** `syncScopeProfile` (`api-client.ts:127-136`) currently persists ONLY `result.data.scope_profile` under the `scopeProfile` key. The `features` map is fetched but **never stored**. Task 4's flag read therefore requires this task to ALSO persist `features`. The scope profile key is confirmed as `scopeProfile`.

- [ ] **Step 1: Add the backend config flag**

In `drpl-backend/app/core/config.py`, add near `eager_analysis_enabled` (line ~63):
```python
    extension_auto_capture_enabled: bool = False
```

- [ ] **Step 2: Surface it in the features map**

In `drpl-backend/app/api/routes/extension.py`, in the `features={...}` dict (lines 281-288), add:
```python
            "auto_capture": settings.extension_auto_capture_enabled,
```
Ensure `settings` is in scope (it is imported/available in this module via `get_settings()`; if the function doesn't already reference it, add `settings = get_settings()` at the top of `get_extension_config`).

- [ ] **Step 3: Verify the backend still imports and the flag is present**

Run:
```bash
cd drpl-backend && venv/Scripts/python.exe -c "
from app.core.config import get_settings
print('auto_capture flag default:', get_settings().extension_auto_capture_enabled)
"
```
Expected: `auto_capture flag default: False`.

- [ ] **Step 4: Add `auto_capture` to the extension features type**

In `drpl-extension/src/utils/types.ts`, find the interface that models the extension config `features` map (search for `document_download` or `gem_auto_search`; if features is typed as an inline object or `Record<string, boolean>`, no change is needed — note that in the report). If there is an explicit `features` interface, add:
```ts
  auto_capture?: boolean;
```

- [ ] **Step 5: Persist the `features` map in `syncScopeProfile`**

In `drpl-extension/src/utils/api-client.ts`, change `syncScopeProfile` (:127-136) so it also persists the features map. Replace:
```ts
  const profile = result.data?.scope_profile || null;
  await chrome.storage.local.set({
    scopeProfile: profile,
    scopeProfileSyncedAt: new Date().toISOString(),
  });
  return profile;
```
with:
```ts
  const profile = result.data?.scope_profile || null;
  const features = result.data?.features || {};
  await chrome.storage.local.set({
    scopeProfile: profile,
    extensionFeatures: features,
    scopeProfileSyncedAt: new Date().toISOString(),
  });
  return profile;
```
This makes `chrome.storage.local.extensionFeatures.auto_capture` the single source of truth Task 4 reads. (Confirm no OTHER caller already stores `features` under a different key; if one does, prefer that key and note it in the report.)

- [ ] **Step 6: Typecheck the extension**

Run: `cd drpl-extension && npx tsc --noEmit`
Expected: no new type errors.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/core/config.py drpl-backend/app/api/routes/extension.py drpl-extension/src/utils/types.ts drpl-extension/src/utils/api-client.ts
git commit -m "feat: surface + persist features.auto_capture flag (default OFF)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `processDocumentHunt` options — do not wipe the queue for a subset

**Files:**
- Modify: `drpl-extension/src/background/service-worker.ts` (`processDocumentHunt` signature ~:284, toggle check :340, queue clear :421)

**Interfaces:**
- Produces: `processDocumentHunt(tenders: TenderData[], opts?: { clearQueue?: boolean; forceDownload?: boolean }): Promise<{ docsUploaded: number; errors: number }>`.
- Consumes: existing `downloadAndUploadDocument`, `extractDetailFromTab`, `getSettings`.

**Why:** today `processDocumentHunt` clears ALL of `pendingDocumentHunt` (line 421) and downloads only when `settings.downloadDocumentsEnabled` (line 340). For auto-capture on a capped subset we must (a) NOT clear the whole queue, and (b) force download regardless of the toggle. Default behavior (manual button) stays identical.

- [ ] **Step 1: Add the options param + return value (signature)**

Change the signature at line ~284 from:
```ts
async function processDocumentHunt(tenders: TenderData[]) {
  const settings = await getSettings();
```
to:
```ts
async function processDocumentHunt(
  tenders: TenderData[],
  opts: { clearQueue?: boolean; forceDownload?: boolean } = {},
): Promise<{ docsUploaded: number; errors: number }> {
  const { clearQueue = true, forceDownload = false } = opts;
  const settings = await getSettings();
```

- [ ] **Step 2: Honor `forceDownload` at the toggle check**

Change line 340 from:
```ts
      if (settings.downloadDocumentsEnabled) {
```
to:
```ts
      if (forceDownload || settings.downloadDocumentsEnabled) {
```

- [ ] **Step 3: Guard the queue clear + return counts**

Change the queue clear at line ~421 from:
```ts
  // Clear the pending hunt queue
  await chrome.storage.local.remove('pendingDocumentHunt');
```
to:
```ts
  // Clear the pending hunt queue only for the manual/full run. Auto-capture
  // passes clearQueue:false and manages the remainder itself.
  if (clearQueue) {
    await chrome.storage.local.remove('pendingDocumentHunt');
  }
```
And at the very end of the function (after the notification block, before the closing brace) add:
```ts
  return { docsUploaded: totalDocsUploaded, errors: errorsCount };
```

- [ ] **Step 4: Confirm existing callers still compile (manual hunt unaffected)**

Run: `cd drpl-extension && npx tsc --noEmit`
Expected: no errors. `handleStartDocumentHunt` calls `processDocumentHunt(tenders)` with no opts → defaults (`clearQueue:true, forceDownload:false`) preserve today's behavior exactly.

- [ ] **Step 5: Commit**

```bash
git add drpl-extension/src/background/service-worker.ts
git commit -m "refactor(ext): processDocumentHunt gains clearQueue/forceDownload opts

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Auto-capture wiring in `processPerTenderAtomic`

**Files:**
- Modify: `drpl-extension/src/background/service-worker.ts` (end of `processPerTenderAtomic`, after line 258)

**Interfaces:**
- Consumes: `splitByScope` from `../utils/scope-matcher`; `processDocumentHunt(…, { clearQueue:false, forceDownload:true })`; `chrome.storage.local.scopeProfile`, `chrome.storage.local` config `features.auto_capture`.

**Why:** this is the feature's trigger — after metadata upload, if `auto_capture` is ON, capture docs for the in-scope, capped subset and leave the remainder for the manual button.

- [ ] **Step 1: Add the import at the top of service-worker.ts**

Add to the existing imports:
```ts
import { splitByScope } from '../utils/scope-matcher';
```

- [ ] **Step 2: Add the auto-capture constant near the top of the file**

```ts
const AUTO_CAPTURE_MAX_PER_SESSION = 10;
```

- [ ] **Step 3: Add a helper to read the flag + profile**

Add this function (near `getSettings`):
```ts
async function getAutoCaptureContext(): Promise<{ enabled: boolean; profile: ScopeProfile | null }> {
  const store = await chrome.storage.local.get(['extensionFeatures', 'scopeProfile']);
  const enabled = Boolean(store.extensionFeatures?.auto_capture);
  const profile = (store.scopeProfile as ScopeProfile | undefined) || null;
  return { enabled, profile };
}
```
KEYS CONFIRMED: `scopeProfile` is written by `syncScopeProfile` (`api-client.ts:132`); `extensionFeatures` is added by Task 2 Step 5. Both are read here. (If Task 2 chose a different features key per its note, use that key.)

- [ ] **Step 4: Wire auto-capture at the end of `processPerTenderAtomic`**

Immediately AFTER the existing line 258 (`await chrome.storage.local.set({ pendingDocumentHunt: huntable });`), insert:
```ts
  // Piece B — gated auto-capture: for in-scope tenders, run the document hunt
  // automatically (capped, sequential). Overflow + out-of-scope stay in the
  // pending queue for the manual "Hunt Documents" button.
  const { enabled: autoCapture, profile: scopeProfile } = await getAutoCaptureContext();
  if (autoCapture && scopeProfile) {
    const { capture, overflow, skipped } = splitByScope(
      huntable, scopeProfile, AUTO_CAPTURE_MAX_PER_SESSION,
    );
    // Remainder that the manual button should still be able to hunt.
    const remainder = [...overflow, ...skipped];
    await chrome.storage.local.set({ pendingDocumentHunt: remainder });

    if (capture.length > 0) {
      console.log(
        `[Ext] Auto-capture: ${capture.length} in-scope` +
        (overflow.length ? `, ${overflow.length} over cap left for manual hunt` : ''),
      );
      const res = await processDocumentHunt(capture, { clearQueue: false, forceDownload: true });
      if (settings.notificationsEnabled) {
        chrome.notifications.create({
          type: 'basic',
          iconUrl: 'icons/icon-48.png',
          title: 'Auto-capture complete',
          message:
            `${res.docsUploaded} docs auto-captured for ${capture.length} in-scope tender(s).` +
            (overflow.length ? ` ${overflow.length} over cap left for manual hunt.` : ''),
        });
      }
    } else {
      console.log('[Ext] Auto-capture: no in-scope tenders this session.');
    }
  }
```
NOTE: `settings` is already in scope (declared at line 196). If `ScopeProfile` isn't already imported in this file, add it to the `../utils/types` import.

- [ ] **Step 5: Typecheck**

Run: `cd drpl-extension && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Run the extension test suite (matcher unaffected, ensure nothing broke)**

Run: `cd drpl-extension && npm run test`
Expected: all pass (existing + the 8 new matcher tests).

- [ ] **Step 7: Commit**

```bash
git add drpl-extension/src/background/service-worker.ts
git commit -m "feat(ext): gated auto-capture after scrape (in-scope, capped, throttled)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Build, verify bundle, smoke runbook

**Files:**
- Create: `docs/RUNBOOK-extension-auto-capture.md`

- [ ] **Step 1: Production build**

Run: `cd drpl-extension && npm run build`
Expected: build succeeds, `dist/` emitted.

- [ ] **Step 2: Verify content scripts are self-contained (MV3 constraint)**

Run: `cd drpl-extension && grep -rn "^import \|require(" dist/content-scripts/ || echo "CLEAN: no leftover imports"`
Expected: `CLEAN: no leftover imports`. (The new module is imported only by the service worker, so content scripts should be unaffected.)

- [ ] **Step 3: Write the smoke runbook**

Create `docs/RUNBOOK-extension-auto-capture.md`:
```markdown
# Runbook — Extension gated auto-capture (Piece B) smoke test

Auto-capture is gated on the backend flag `extension_auto_capture_enabled`
(surfaced as `features.auto_capture`, default OFF). The orchestration
(background tabs / fetch) is not unit-testable; verify by hand.

## Enable
1. Backend: set `EXTENSION_AUTO_CAPTURE_ENABLED=true` (env) or flip the config
   default, and redeploy / restart so `/api/extension/config` returns
   `features.auto_capture: true`.
2. Extension: load `drpl-extension/dist/` unpacked (chrome://extensions), log in,
   and let it sync `/config` (reopen the popup once to force a fetch).

## Verify
1. Navigate to a portal listing (IREPS/GeM/aggregator) that contains a tender
   you know is in scope (matches a keyword_group keyword, a target ministry,
   and value within [value_min, value_max]) AND one clearly out of scope.
2. Run a scrape from the popup.
3. Expect: after metadata upload, the in-scope tender's detail page opens in a
   background tab automatically and its PDFs upload (watch the service-worker
   console: `[Ext] Auto-capture: N in-scope`). The out-of-scope tender's docs
   are NOT auto-captured and it remains under "Hunt Documents".
4. Cap check: if >10 in-scope tenders match, confirm exactly 10 are auto-captured
   and the notification/console reports the overflow left for manual hunt, and
   the manual "Hunt Documents" button still lists the remainder.
5. Off check: set the flag back to false, re-sync, scrape again — behavior is
   identical to today (manual button only, nothing auto-runs).
```

- [ ] **Step 4: Commit**

```bash
git add drpl-extension/dist docs/RUNBOOK-extension-auto-capture.md
git commit -m "build(ext): rebuild dist with auto-capture; add smoke runbook

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Component 1 (scope-matcher, strict + fail-closed-visible) → Task 1. ✓
- Component 2 (service-worker wiring, cap, sequential, forceDownload override, remainder preserved) → Tasks 3 + 4. ✓
- Component 3 (config/flag plumbing) → Task 2. ✓
- Testing strategy (unit matcher + cap; manual smoke; bundle check) → Tasks 1, 5. ✓
- Boundaries (no ML, no per-tender UI, no Piece A/C changes) → respected; no task touches them. ✓

**Placeholder scan:** No TBD/TODO. Storage keys are now CONFIRMED, not guessed: `scopeProfile` is written by `syncScopeProfile` (`api-client.ts:132`), and `extensionFeatures` is explicitly added by Task 2 Step 5 (the `features` map was previously fetched but never persisted — a real gap this plan closes). Task 4 reads both confirmed keys.

**Type consistency:** `matchesScope`/`splitByScope`/`ScopeMatch` signatures identical across Task 1 def and Task 4 use. `processDocumentHunt(tenders, opts)` options object identical across Task 3 def and Task 4 call (`{ clearQueue:false, forceDownload:true }`). `ScopeProfile`/`TenderData` field names match `types.ts` (verified: `keyword_groups`, `exclusion_terms`, `target_ministries`, `value_min|null`, `value_max|null`; `title`, `department`, `organisation`, `estimatedValue:number|null`, `category?`, `sourcePortal?`). ✓

**Data-flow verified end-to-end:** backend `config.py` flag → `extension.py` `features.auto_capture` → `syncScopeProfile` persists to `extensionFeatures` (Task 2) → `getAutoCaptureContext` reads it (Task 4). No broken link.
