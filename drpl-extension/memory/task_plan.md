# Extension — Task Plan

## North Star

Apply the System Pilot protocol to the Chrome MV3 scrape→ingest pipeline.

## Phases

| Phase | State | Notes |
|---|---|---|
| Blueprint | ✅ done | Captured in [../CONSTITUTION.md](../CONSTITUTION.md) |
| Link | 🟡 in progress | Probes in [../execution/probes/](../execution/probes/): `check-content-script-bundles.mjs`, `check-selectors-ota.mjs` |
| Architect | ✅ done | Three SOPs in [../architecture/](../architecture/) |
| Stylize | n/a | Popup UI unchanged |
| Trigger | n/a | DOM events + manual `TRIGGER_SCRAPE` unchanged |

## Active surfaces

- **Content script bundling** → [../architecture/sop-content-script-bundling.md](../architecture/sop-content-script-bundling.md)
- **Scrape → ingest handshake** → [../architecture/sop-scrape-ingest-handshake.md](../architecture/sop-scrape-ingest-handshake.md)
- **OTA selectors** (INERT) → [../architecture/sop-ota-selectors.md](../architecture/sop-ota-selectors.md)

## Open work

- Decide Option A (wire OTA) vs Option B (remove backend endpoint) — see [decisions.md](decisions.md).
- Fix the transitive-chunk-import risk in `vite.config.ts:101-102` — deferred until the probe catches a real failure.
