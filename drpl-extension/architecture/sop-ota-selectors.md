# SOP — OTA Selectors (CURRENT STATUS: INERT)

## Goal

Selectors that the content scripts use to scrape portal DOMs should be updatable without a Chrome Web Store re-review. The backend is supposed to serve fresh `selectors.json`; the extension is supposed to fetch and apply it.

## Current status — INERT

**As of 2026-05-18, the OTA path does not work end-to-end.**

- ✅ Backend serves `GET /api/extension/selectors/{portal}` at [../../drpl-backend/app/api/routes/extension.py:205-223](../../drpl-backend/app/api/routes/extension.py).
- ✅ Extension bundles [../src/config/selectors.json](../src/config/selectors.json) (version 1.1.0).
- ❌ **Extension reads from `chrome.runtime.getURL()` (bundled copy) only** — see e.g. [../src/content-scripts/ireps.ts:34-61](../src/content-scripts/ireps.ts) `loadEligibilityConfig()`.
- ❌ Backend response has no etag / hash / version header for cache invalidation.
- ❌ Service worker has no periodic fetch loop.

The result: selector changes require a Chrome Web Store re-review, despite the project's CLAUDE.md claiming OTA is wired up.

## Two paths forward — decision required

### Option A — Wire OTA fully (RECOMMENDED)

1. Add a SHA-256 hash header to the backend response: `X-Selectors-Hash: <sha256>`.
2. Service worker fetches every 6 hours (and on extension startup), compares hash against `chrome.storage.local.selectorsHash`, and on mismatch downloads + caches the new JSON in `chrome.storage.local.selectorsBundle`.
3. Content scripts read from `chrome.storage.local.selectorsBundle` first, falling back to the bundled `selectors.json` if not yet cached.
4. Versioning rule: the backend's `selectors.json` MUST be a strict superset of the bundled version (additive keys only). Removing a key requires bumping the bundled `version` field and shipping a new extension.

### Option B — Remove the backend endpoint

If the team prefers selectors-as-code (Chrome Web Store re-review for every change), delete the route at [extension.py:205-223](../../drpl-backend/app/api/routes/extension.py) and remove the `selector_configs/` directory. Update CLAUDE.md to drop the OTA claim.

### Status

**Decision pending.** Logged in [../memory/decisions.md](../memory/decisions.md). Until the decision is made, the SOP documents the gap so no one assumes OTA works.

## Inputs (Option A, target state)

- Service worker timer (every 6h + on startup).
- `GET /api/extension/selectors/{portal}` response body (JSON) and `X-Selectors-Hash` header.
- `chrome.storage.local.selectorsHash` (cached hash).
- `chrome.storage.local.selectorsBundle` (cached JSON).

## Outputs (Option A, target state)

- Updated `chrome.storage.local.selectorsBundle` on hash mismatch.
- Content scripts read fresh selectors without an extension reinstall.

## Determinism boundary

Deterministic. No LLM.

## Edge cases (Option A)

1. **First-run cold start.** No cached bundle → fall back to bundled `selectors.json`. First periodic fetch populates the cache.
2. **Backend down during fetch.** Use cached bundle (or bundled fallback). Do not error to the user.
3. **Backend serves malformed JSON.** Validate against a JSON schema (or at minimum, `JSON.parse` + presence check) before caching. Reject silently and keep the previous cache.
4. **Selector regression.** A bad selector ships → scrapes return empty payloads. Mitigation: track scrape-success rate per portal (already implicit in `received` field from backend); alert when it drops to zero for >1 hour.

## Failure modes (Option A)

| Symptom | Cause | Fix |
|---|---|---|
| Scrapes return empty after a working portal change | Backend hasn't published an updated selector | Push the new selector to `selector_configs/{portal}.json` on the backend |
| Extension keeps fetching but cache never updates | Hash header missing from response | Add `X-Selectors-Hash` to the route handler |
| Mismatched fields between cached and bundled selectors | Backwards-incompatible bundle update without `version` bump | Always bump `version` in `selectors.json` when changing key shape; the service worker should refuse a cached bundle whose `version` is less than the bundled `version` |

## Verification

Currently the only verification possible is the existence check:

```bash
cd drpl-extension

# 1. Verify the backend endpoint serves
node execution/probes/check-selectors-ota.mjs

# 2. Confirm the extension does NOT call it (current state)
grep -r "selectors/ireps\|selectors/gem" src/
# Expected: no matches (extension reads bundled file only)
```

Once Option A ships, verification becomes:

```bash
# 3. Force-refresh from extension dev tools console
chrome.runtime.sendMessage({ type: 'FETCH_SELECTORS_NOW' })

# 4. Inspect cache
chrome.storage.local.get('selectorsBundle', console.log)
```

## Open work

- **Decision (Option A vs B).** Owner: TBD. Tracked in [../memory/decisions.md](../memory/decisions.md).
- If Option A: implement hash header, service worker fetch loop, cache, content-script read order.
- If Option B: remove the backend route, delete `selector_configs/`, update CLAUDE.md.
