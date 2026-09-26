# Extension — Findings

Code anchors from the 2026-05-18 exploration pass.

## Content scripts

- `src/content-scripts/ireps.ts` — MutationObserver lines 619-628; manual TRIGGER_SCRAPE lines 660-705; `TENDER_DATA_EXTRACTED` POST at 639-657; eligibility config loader at 34-61
- `src/content-scripts/gem.ts` — React-aware MutationObserver lines 421-438; POST at 448-465; manual trigger 469-505
- `src/content-scripts/gem-search-driver.ts` — Service-worker-triggered keyword automation; collect at 247-254; per-keyword batches 326-339
- `src/content-scripts/aggregators.ts` — Site detection lines 21-29; POST at 478-523

## Service worker

- `src/service-worker.ts:33-108` — `chrome.runtime.onMessage` handler
- `src/service-worker.ts:15` — `BATCH_SIZE=25`
- `src/service-worker.ts:18` — `DEEP_SCRAPE_TIMEOUT_MS=20000`
- `src/service-worker.ts:225-227` — `lastUploadError` write to `chrome.storage.local`
- `src/service-worker.ts:680` — sends `RUN_GEM_AUTO_SEARCH` to content script
- `src/api-client.ts:43-46` — backend URL + Bearer auth
- `src/api-client.ts:50-81` — 3-attempt retry, 2000ms linear backoff

## Build plugin

- `vite.config.ts:18-151` — `bundle-content-scripts` plugin in `closeBundle`
- `vite.config.ts:83` — parses `export { ... }` from chunks
- `vite.config.ts:101-102` — **latent risk**: strips nested imports without inlining them
- `vite.config.ts:110, 118-121, 124` — IIFE wrap, destructure, remove import statement
- `vite.config.ts:140-150` — cleans up chunks not referenced by popup/service worker

## Backend touchpoints

- `../../drpl-backend/app/api/routes/extension.py:37-66` — `POST /api/extension/tenders`
- `../../drpl-backend/app/api/routes/extension.py:205-223` — `GET /api/extension/selectors/{portal}` (served but extension does not fetch)
- `../../drpl-backend/app/services/tender_service.py:30-114` — dedup + savepoint rollback
- `../../drpl-backend/app/services/tender_service.py:36-40` — composite unique `(portal, tender_id)`
- `../../drpl-backend/app/services/tender_service.py:57` — `score_relevance_background(new_ids)` post-ingest fan-out

## Config

- `src/config/selectors.json` — version `1.1.0`, bundled in extension, NOT fetched OTA
