# Extension — Decisions Log

## 2026-05-18 — OTA selectors: decision pending (Option A vs B)

**Decision.** Documented the OTA selectors gap as INERT in [../architecture/sop-ota-selectors.md](../architecture/sop-ota-selectors.md). No implementation in this pass. Two options surfaced:

- **Option A.** Wire OTA fully — backend adds `X-Selectors-Hash` header; service worker fetches every 6h + on startup; content scripts read from `chrome.storage.local.selectorsBundle` with bundled fallback. Recommended.
- **Option B.** Remove the backend endpoint and update CLAUDE.md to drop the OTA claim.

**Why pending.** Requires a strategy call: does the team accept the operational cost of OTA selector updates (publishing JSON to a backend route every time a portal changes its DOM), or do they want selectors-as-code? Tracked here until decided.

## 2026-05-18 — Vite plugin transitive-import risk: documented, fix deferred

**Decision.** [vite.config.ts:101-102](../vite.config.ts) strips nested chunk imports without inlining them. The risk is documented in [../architecture/sop-content-script-bundling.md](../architecture/sop-content-script-bundling.md). The probe at `../execution/probes/check-content-script-bundles.mjs` catches surviving imports before they ship.

**Why defer.** No real failure has surfaced because most chunks are leaves. Recursive inlining is the right long-term fix but adds plugin complexity. The probe is sufficient safety net for now.

## 2026-05-18 — Content scripts never call the backend directly (formalized as Rule 1)

**Decision.** Promoted from convention to enforceable rule in [../CONSTITUTION.md](../CONSTITUTION.md). Any `fetch()` against `*.railway.app` in a content script is a refusal trigger.

**Why.** MV3 content scripts run in the page's CSP context; CORS / auth / retries belong in the service worker. The convention was already followed; this just makes it explicit.
