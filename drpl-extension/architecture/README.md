# Extension Architecture SOPs

SOPs for the Chrome MV3 extension. Same shape as backend SOPs: Goal · Inputs · Outputs · Determinism boundary · Tool contract · Edge cases · Failure modes · Verification.

| SOP | Surface | Status |
|---|---|---|
| [sop-content-script-bundling.md](sop-content-script-bundling.md) | Vite plugin + MV3 ESM invariant | Active — latent risk documented |
| [sop-scrape-ingest-handshake.md](sop-scrape-ingest-handshake.md) | Content script → service worker → backend | Active |
| [sop-ota-selectors.md](sop-ota-selectors.md) | OTA selector updates | **Inert** — backend serves, extension does not fetch |

## Three rules that govern the extension

1. **Service worker is the only backend client.** Content scripts emit `chrome.runtime.sendMessage`; the service worker does the network.
2. **`dist/content-scripts/*.js` is import-free.** The Vite plugin inlines chunks. A surviving `import` statement = silent Chrome load failure.
3. **Selectors live in `selectors.json`, never hard-coded.** Generic structural selectors (e.g. `'table tr'`) are fine; portal-specific (`'#tenderTable .row-3'`) are not.
