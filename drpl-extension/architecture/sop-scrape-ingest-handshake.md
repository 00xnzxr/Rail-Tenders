# SOP — Scrape → Ingest Handshake

## Goal

Scrape tender data from IREPS / GeM / aggregator portals, batch it, ship it to the backend, dedup it, and surface upload status back to the user — all while the user is logged in to their portal with their DSC token.

## Inputs

### Content script POSTs (via `chrome.runtime.sendMessage`)

| Portal | File | DOM trigger | Manual trigger |
|---|---|---|---|
| IREPS | [../src/content-scripts/ireps.ts:619-657](../src/content-scripts/ireps.ts) | MutationObserver on `table tr` (lines 619-628) | `TRIGGER_SCRAPE` message handler (lines 660-705) |
| GeM | [../src/content-scripts/gem.ts:421-465](../src/content-scripts/gem.ts) | React-aware MutationObserver on `.MuiCard-root` / `[class*="bid"]` | `TRIGGER_SCRAPE` (lines 469-505) |
| GeM auto-search | [../src/content-scripts/gem-search-driver.ts:326-339](../src/content-scripts/gem-search-driver.ts) | Service worker sends `RUN_GEM_AUTO_SEARCH` ([service-worker.ts:680](../src/service-worker.ts)) | n/a |
| Aggregators | [../src/content-scripts/aggregators.ts:478-523](../src/content-scripts/aggregators.ts) | `TRIGGER_SCRAPE` only | Same |

### Message shape

```jsonc
{
  "type": "TENDER_DATA_EXTRACTED",
  "portal": "ireps | gem | tendertiger | bidassist | tenderdetail | tendersinfo | projectstoday",
  "payload": {
    "tenders": [ { "tender_id": "...", "title": "...", "url": "...", /* …deep-scrape fields */ } ],
    "pageType": "list | detail | search",
    "pageUrl": "string"
  }
}
```

This is the **only** message shape that carries scraped tenders. Other message types (`EXTRACT_DETAIL`, `RUN_GEM_AUTO_SEARCH`, `GEM_AUTO_SEARCH_PROGRESS`) are control plane, not data plane.

## Outputs

### Service worker → backend

- **Endpoint.** `POST https://drpl-platform-production.up.railway.app/api/extension/tenders` ([api-client.ts:43](../src/api-client.ts)).
- **Auth.** `Authorization: Bearer ${authToken}` from `chrome.storage.local.drpl_token` ([api-client.ts:46](../src/api-client.ts)).
- **Batching.** Chunks of 25 (`BATCH_SIZE` at [service-worker.ts:15](../src/service-worker.ts)).
- **Retry.** 3 attempts, 2000ms × attempt linear backoff ([api-client.ts:50-81](../src/api-client.ts)).
- **Timeout.** 20s per detail page (`DEEP_SCRAPE_TIMEOUT_MS` at [service-worker.ts:18](../src/service-worker.ts)).

### Backend → service worker

[../drpl-backend/app/api/routes/extension.py:37-66](../drpl-backend/app/api/routes/extension.py) responds with:

```jsonc
{
  "received": 25, "new": 18, "duplicates": 7, "errors": [],
  "new_ids": [101, 102, ...]
}
```

On success, the backend enqueues `score_relevance_background(new_ids)` ([extension.py:57](../drpl-backend/app/api/routes/extension.py)) — the post-ingest AI fan-out.

### Service worker → popup

`chrome.storage.local.lastUploadError` ([service-worker.ts:225-227](../src/service-worker.ts)) — the popup reads this for its error banner. See [../CONSTITUTION.md](../CONSTITUTION.md#3-popup-error-envelope) for the shape.

## Determinism boundary

No LLM in this path. Dedup is deterministic — composite unique constraint `(portal, tender_id)` at [../drpl-backend/app/services/tender_service.py:36-40](../drpl-backend/app/services/tender_service.py).

## Tool contract

Not applicable — no LangChain tools. The "tools" here are the scrape functions (per content script) and `apiRequest` (in `api-client.ts`).

## Edge cases

1. **Duplicate ingestion.** Same `(portal, tender_id)` arrives twice. `tender_service.upsert` ([tender_service.py:30-114](../drpl-backend/app/services/tender_service.py)) updates status/dates/deep-scrape fields if the incoming row is richer; increments `duplicate_count`.
2. **Partial batch failure.** Per-tender savepoint rollback ([tender_service.py:109-126](../drpl-backend/app/services/tender_service.py)) — one bad row does not fail the batch.
3. **Session expired (401).** [api-client.ts:54-57](../src/api-client.ts) clears the session; popup prompts re-login on next open.
4. **Page navigates mid-scrape.** MutationObserver is debounced; partial scrapes are dropped, not partially uploaded.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Popup shows "Upload failed: 502" repeatedly | Backend down or migration in progress | Wait + retry; check Railway logs |
| Tenders not appearing in dashboard despite popup showing success | Dedup matched an existing tender; `new=0, duplicates=N` is expected | Check `received` vs `new` in the response |
| Some tenders persistently fail with `errors[]` non-empty | Schema drift on the backend (e.g. new required column) | Run `_apply_schema_drift_fixes` / Alembic; the schema-drift self-heal is supposed to catch this — if it doesn't, file a backend defect |
| `TENDER_DATA_EXTRACTED` never fires on a portal page | Content script crashed on load (e.g. surviving ESM import) | Run [sop-content-script-bundling.md](sop-content-script-bundling.md) probe |

## Verification

```bash
# 1. Manual flow
# Open Chrome, navigate to ireps.gov.in, log in with DSC
# Open a tender list page; popup should auto-show "N tenders found"
# Click "Upload" → popup should show "N uploaded, M new"

# 2. Backend confirms
psql -c "SELECT portal, COUNT(*) FROM tenders GROUP BY portal ORDER BY 2 DESC;"

# 3. Service worker reachability (no scrape, just round-trip)
# Open chrome://extensions, click "service worker" on the DRPL card,
# in the DevTools console:
fetch('https://drpl-platform-production.up.railway.app/health/capacity').then(r => r.json()).then(console.log)
```

## Open work

None for the handshake itself. The [sop-ota-selectors.md](sop-ota-selectors.md) gap is the adjacent risk: if a portal changes its DOM and selectors are stale, scrapes return empty payloads, and the handshake succeeds (200 with `received: 0`) — silent failure mode.
