# Extension Gated Auto-Capture (Piece B) — Design

**Date:** 2026-07-13
**Status:** Design (awaiting user review)
**Source backlog:** [`docs/PENDING.md`](../../PENDING.md) §3 "Piece B — Extension gated auto-capture"
**Approach:** A — service-worker-orchestrated local pre-filter + reuse of the existing document-hunt path

---

## Purpose

Today the Chrome extension scrapes tender metadata and uploads it, but the
**document hunt** (open detail page → extract PDF links → download bytes in-session
→ re-upload to backend) is **manual**: the user must click "Hunt Documents", which
runs all-or-nothing over the pending queue. Backend eager analysis (Piece A, shipped
but OFF) can therefore only analyze docs the manual hunt already uploaded.

Piece B closes that gap: for tenders that pass a **local scope pre-filter**, the
extension **auto-runs the existing document hunt right after a scrape**, so in-scope
tenders arrive at the backend already carrying their PDFs — with no extra click.

**Key framing:** this is mostly *assembly*, not new construction. The extension
already (a) downloads+uploads PDF bytes in-session using the user's live portal
session (`downloadAndUploadDocument`, `service-worker.ts:~802`) and (b) has the
backend scope profile synced locally (`chrome.storage.local.scopeProfile`). What is
missing is a pure scope-match function and the wiring to auto-trigger the existing
hunt for the passing subset, behind a feature flag.

The "AI integration" here means in-scope documents now reach the backend's AI
pipeline automatically. The extension itself does deterministic keyword/ministry/value
matching — **not** local ML.

---

## Approach A (chosen)

- One pure module `utils/scope-matcher.ts` (`(tender, profile) → ScopeMatch`).
- The **service worker** — which already receives `TENDER_DATA_EXTRACTED`, already
  holds the synced `scopeProfile`, and already owns `processDocumentHunt` — filters
  the scraped batch and, when the `auto_capture` feature flag is ON, auto-invokes the
  existing hunt path on the passing subset (capped, sequential).
- **Content scripts are untouched.** Only the service worker (a real ES-module
  context) imports the new module, so the MV3 content-script bundling constraint does
  not apply.

Rejected alternatives: **B (content-script pre-filter)** spreads the same logic across
three entry points and through the bundler-inlining constraint for no gain; **C
(backend-driven capture list)** defeats the point of a *local, in-session* pre-filter
(the goal is docs arriving *with* the tender using the user's live cookies, without a
backend round-trip).

---

## Component 1 — `utils/scope-matcher.ts`

A pure, synchronous, I/O-free function (no `chrome.*`, no `fetch`), mirroring the
existing `utils/*` plain modules.

```ts
export interface ScopeMatch {
  inScope: boolean;
  reasons: string[];   // e.g. ["keyword:pumps", "ministry:Railways", "value:in-range"]
  missing: string[];   // fields that couldn't be evaluated, e.g. ["ministry","value"]
}

export function matchesScope(tender: TenderData, profile: ScopeProfile): ScopeMatch
```

**Strict rule (keyword AND ministry AND value):**

1. **Keyword** — the tender's `title` (plus `department`/`category` when present),
   case/whitespace-normalized, contains ≥1 keyword from any
   `profile.keyword_groups[].keywords`, **and** contains **none** of
   `profile.exclusion_terms`.
2. **Ministry** — the tender's `department`/`organisation`/`sourcePortal`
   (normalized contains) matches ≥1 entry in `profile.target_ministries`.
3. **Value** — the tender's `estimatedValue` lies within
   `[profile.value_min, profile.value_max]`. Either bound may be null → that side is
   open-ended.

All three must pass → `inScope: true`.

**Missing-metadata handling — fail-closed but visible:** a field that is
null/empty/unparseable is treated as **not passing** (so `inScope: false`), and its
name is recorded in `missing[]`. This keeps precision high (no false auto-downloads
from sparse aggregator rows) while making the *reason* auditable so the UI/logs can
report e.g. "3 skipped: missing value". **Exclusion terms always apply**, even when
other fields are missing (an excluded tender is never in scope).

**Known trade-off (documented, accepted):** the strict gate will drop many aggregator
tenders that lack ministry or value. That is the intended precision-over-recall
posture; the `missing[]` output and the untouched manual "Hunt Documents" button are
the escape hatch (nothing in-scope-but-sparse is lost — it stays huntable manually).

---

## Component 2 — Service-worker wiring

**Hook point:** `handleTenderData()`, immediately after the existing per-tender
metadata upload and `pendingDocumentHunt` population (`service-worker.ts:~257`).

**Flow:**

```
scrape finishes → tenders uploaded (UNCHANGED)
  → read features.auto_capture from chrome.storage.local (synced /config)
  → if OFF: behave exactly as today (manual "Hunt Documents"). Done.
  → if ON:
      inScope  = tenders.filter(t => matchesScope(t, scopeProfile).inScope)
      capped   = inScope.slice(0, AUTO_CAPTURE_MAX_PER_SESSION)   // = 10
      overflow = inScope.length - capped.length
      run processDocumentHunt(capped) SEQUENTIALLY, throttled
      log/surface: "captured {capped} of {inScope} in-scope; {overflow} left for manual hunt"
      overflow + out-of-scope tenders REMAIN in pendingDocumentHunt (nothing lost)
```

**Guardrails:**

1. **Feature-flag gated, default OFF.** New `auto_capture` in the backend `/config`
   `features` map (default `false`). Ships dark; enabled per-deployment. Mirrors the
   existing `document_download` posture and Piece A's off-by-default stance.
2. **Per-session cap.** `AUTO_CAPTURE_MAX_PER_SESSION = 10` (service-worker const).
   Excess falls to the manual queue. The cap is **logged and surfaced**, never a
   silent truncation.
3. **Sequential + throttled.** Reuse `processDocumentHunt`'s existing one-background-
   tab-at-a-time loop; add a small inter-tender delay so we don't hammer the portal
   and risk rate-limiting the user's live session.

**Deliberate override of `downloadDocumentsEnabled`:** the design intentionally does
**not** gate auto-capture on the existing `settings.downloadDocumentsEnabled` /
`document_download` toggle. Auto-capture passes an explicit `forceDownload = true`
into the hunt path so byte download happens even if that toggle is off. Rationale:
without it, turning `auto_capture` on while `document_download` is off would extract
doc links but upload zero bytes — a confusing dead end. The strict filter + per-session
cap + master `auto_capture` flag already bound the blast radius, so the override is
safe and intentional.

**Failure handling:** per-tender failures (tab won't open, PDF 404, >10MB) are already
handled inside `downloadAndUploadDocument` (skip + log). Auto-capture inherits this;
one tender failing never aborts the batch.

---

## Component 3 — Config / flag plumbing

- **Backend (the only backend change):** add `extension_auto_capture_enabled: bool =
  False` to `app/core/config.py`, and surface it as `auto_capture` in the `features`
  map of `ExtensionConfigResponse` (`GET /api/extension/config`, `extension.py`). No
  new columns, no migration, no schema-drift entries.
- **Extension:** add `auto_capture` to the extension's `features` type
  (`utils/types.ts`); the existing `fetchConfig()`/`syncScopeProfile()` already persist
  the whole `/config` response, so no new fetch is needed. The service worker reads the
  flag at capture-decision time.

---

## Testing strategy

- **Unit (primary coverage) — `scope-matcher.test.ts`:** pure-function tests over
  `TenderData` + `ScopeProfile` fixtures:
  - full match (keyword ∧ ministry ∧ value) → `inScope: true`, reasons populated
  - exclusion term present → out
  - missing ministry → out, `missing:["ministry"]`
  - missing value → out, `missing:["value"]`
  - keyword miss → out
  - value out of range → out
  - open-ended bounds (null `value_min`/`value_max`) → value side passes
- **Cap/overflow split:** if extracted as a pure helper, a small unit test for
  `slice(0, MAX)` + overflow count.
- **Not unit-tested (documented as a manual smoke):** the `chrome.tabs`/`fetch`/
  background-tab orchestration inside `processDocumentHunt` is existing, un-mocked
  code. Verification is a **load-unpacked smoke** (short runbook): enable the flag,
  scrape a page containing a known in-scope tender, confirm its PDFs auto-upload and an
  out-of-scope tender's do not, and that the cap message appears when >10 match.
- **Build check:** after `npm run build`, confirm `dist/content-scripts/*.js` contain
  no leftover `import` statements (per CLAUDE.md). Under Approach A the new module is
  imported only by the service worker, so this should be unaffected — verified, not
  assumed.

---

## Boundaries (what Piece B does NOT do)

- No local ML/AI — deterministic keyword/ministry/value matching only.
- No per-tender UI picker; no local keyword editing (scope stays backend-admin-managed
  via the existing Scope admin page).
- Does not modify Piece A (backend eager analysis) or Piece C (metadata
  reconciliation) — those remain separate future work.
- No new portal support; works on the existing IREPS/GeM/aggregator entry points.

---

## Change footprint

- New: `drpl-extension/src/utils/scope-matcher.ts` + `scope-matcher.test.ts`
- Modified: `drpl-extension/src/background/service-worker.ts` (filter + cap + auto-hunt
  wiring; `forceDownload` param threaded into the hunt path),
  `drpl-extension/src/utils/types.ts` (`auto_capture` feature),
  `drpl-backend/app/core/config.py` (flag),
  `drpl-backend/app/api/routes/extension.py` (surface flag in `features`)
- Rebuilt `drpl-extension/dist/`
- New: a short load-unpacked smoke runbook (`docs/RUNBOOK-extension-auto-capture.md`)

---

## Risks & open decisions

- **Strict filter drops sparse aggregator tenders** — accepted precision-over-recall;
  mitigated by `missing[]` visibility + the untouched manual hunt button.
- **`downloadDocumentsEnabled` override** — intentional (see Component 2); bounded by
  filter + cap + master flag.
- **Portal rate-limiting** — mitigated by sequential + throttled processing and the
  per-session cap; the user's live session is the constraint being protected.
- **Orchestration not unit-tested** — inherent to the existing un-mocked Chrome-API
  code; covered by the manual smoke runbook, matching how the current hunt path is
  already verified.
