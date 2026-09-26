# Extension Constitution

System Pilot artifact for `drpl-extension/`. Onboarding lives in [../CLAUDE.md](../CLAUDE.md); this file is the Constitution layer for the Chrome MV3 extension that scrapes IREPS / GeM / aggregator portals.

---

## Data Schemas

### 1. `TENDER_DATA_EXTRACTED` message (content script → service worker)

Sent via `chrome.runtime.sendMessage` from [ireps.ts:639-657](src/content-scripts/ireps.ts), [gem.ts:448-465](src/content-scripts/gem.ts), and [aggregators.ts:478-523](src/content-scripts/aggregators.ts).

```jsonc
{
  "type": "TENDER_DATA_EXTRACTED",
  "portal": "ireps | gem | tendertiger | bidassist | tenderdetail | tendersinfo | projectstoday",
  "payload": {
    "tenders": [
      { "tender_id": "string", "title": "string", "url": "string", /* …deep-scrape fields */ }
    ],
    "pageType": "list | detail | search",
    "pageUrl": "string"
  }
}
```

The service worker batches these (`BATCH_SIZE=25`, [service-worker.ts:15](src/service-worker.ts)) and POSTs to the backend ingest route. **No other message type carries scraped tenders.**

### 2. `selectors.json` schema

Shipped in [src/config/selectors.json](src/config/selectors.json), version `1.1.0`. Mirrored on the backend at `selector_configs/<portal>.json` served by [extension.py:205-223](../drpl-backend/app/api/routes/extension.py).

```jsonc
{
  "version": "1.1.0",
  "portals": {
    "ireps": {
      "list_table": "...",
      "row_selectors": { "tender_id": "...", "title": "...", "closing_date": "..." },
      "column_patterns": { /* regex header matches */ }
    },
    "gem": { ... },
    "aggregators": { ... }
  }
}
```

**Status:** the OTA fetch path is currently inert — see [architecture/sop-ota-selectors.md](architecture/sop-ota-selectors.md). Selector changes require a Chrome Web Store re-review until that gap is closed.

### 3. Popup error envelope

The service worker writes to `chrome.storage.local.lastUploadError` ([service-worker.ts:225-227](src/service-worker.ts)) and the popup displays it.

```jsonc
{
  "lastUploadError": {
    "ts": 1700000000,
    "portal": "ireps",
    "batchSize": 25,
    "httpStatus": 502,
    "message": "string",
    "retriesAttempted": 3
  }
}
```

---

## Behavioral Rules (enforceable, with refusal triggers)

### Rule 1 — Service worker is the only backend client

**Content scripts never call the backend directly.** They emit `chrome.runtime.sendMessage` and let [service-worker.ts](src/service-worker.ts) handle the network.

- **Refusal trigger.** Refuse any content-script code that imports from `api-client.ts`, references `fetch()` against `*.railway.app`, or constructs a `Bearer` header.
- **Why.** MV3 content scripts run in the page's CSP context; CORS, auth, and retries are owned by the service worker. Direct fetches make the extension fragile and bypass batching.

### Rule 2 — Content scripts must be self-contained at build time

**`dist/content-scripts/*.js` must contain zero ESM `import` statements.** The Vite plugin `bundle-content-scripts` in [vite.config.ts:18-151](vite.config.ts) inlines chunk imports; if a transitive import survives, Chrome will silently fail to load the content script.

- **Refusal trigger.** Refuse to merge a build whose `dist/content-scripts/*.js` contains an `import` statement (validated by [execution/probes/check-content-script-bundles.mjs](execution/probes/check-content-script-bundles.mjs)).
- **Known risk.** [vite.config.ts:101-102](vite.config.ts) strips nested chunk imports without inlining them. If a chunk imports from another chunk, the secondary import is removed unbundled. See [architecture/sop-content-script-bundling.md](architecture/sop-content-script-bundling.md).

### Rule 3 — Selectors live in `selectors.json`, never hard-coded

**No portal-specific CSS selector may be embedded in TypeScript source.** All selectors come from `selectors.json`, even when they're "obvious" or "won't change."

- **Refusal trigger.** Refuse a PR that adds a string like `'#tenderTable tr.row'` directly inside `ireps.ts` / `gem.ts` / `aggregators.ts`.
- **Exception.** Generic structural selectors (`'table tr'`, `'.MuiCard-root'`) used as DOM observers — not portal-specific. These already exist and are fine.

---

## Architectural Invariants

1. **Three content-script entry points.** `ireps.ts`, `gem.ts` (+ `gem-search-driver.ts`), `aggregators.ts`. Adding a new portal means adding a fourth entry, not extending one of these.
2. **Service worker owns auth + retries.** Token lives in `chrome.storage.local.drpl_token`; retry policy is 3 attempts with linear backoff ([api-client.ts:50-81](src/api-client.ts)).
3. **Manifest host_permissions is the source of truth for portal scope.** [public/manifest.json](public/manifest.json) lists every domain the extension touches; content-script `matches` patterns must be a subset.
4. **No DOM mutation, only DOM read.** Content scripts scrape; they never click buttons or fill forms (exception: `gem-search-driver.ts` automated search keyword entry, which is explicitly opt-in).

---

## B.L.A.S.T. Phase Outputs

| Phase | Output |
|---|---|
| **B — Blueprint** | Message-shape contract + selector OTA design — captured in [memory/task_plan.md](memory/task_plan.md). |
| **L — Link** | Probes in [execution/probes/](execution/probes/): `check-content-script-bundles.mjs` (post-build invariant), `check-selectors-ota.mjs` (backend round-trip). |
| **A — Architect** | SOPs under [architecture/](architecture/) — `sop-content-script-bundling.md`, `sop-scrape-ingest-handshake.md`, `sop-ota-selectors.md`. |
| **S — Stylize** | Popup UI in `src/popup/` — out of scope for this pass. |
| **T — Trigger** | DOM events in the user's browser (MutationObserver on portal pages) and manual `TRIGGER_SCRAPE` messages from the popup. |

---

## Pointers

- SOPs: [architecture/README.md](architecture/README.md)
- Memory: [memory/](memory/)
- Probes: [execution/probes/](execution/probes/)
- Onboarding: [../CLAUDE.md](../CLAUDE.md)
