# Frontend — Task Plan

## North Star

Maintain the two consumer contracts (SSE event stream, workspace HTML render) that, if broken, cascade across every feature.

## Phases

| Phase | State | Notes |
|---|---|---|
| Blueprint | ✅ done | Captured in [../CONSTITUTION.md](../CONSTITUTION.md) — consumer contracts only |
| Link | ✅ done | One probe: [../execution/probes/backend-health.mjs](../execution/probes/backend-health.mjs) |
| Architect | ✅ done | Two SOPs in [../architecture/](../architecture/) |
| Stylize | n/a | Existing component library |
| Trigger | n/a | Browser navigation, unchanged |

## Active surfaces

- **Command Center SSE consumer** → [../architecture/sop-command-center-sse.md](../architecture/sop-command-center-sse.md)
- **Workspace HTML render** → [../architecture/sop-workspace-doc-render.md](../architecture/sop-workspace-doc-render.md)

## No open work

The frontend was not the focus of this pass. Future surface-specific SOPs (e.g. a dedicated tender Workspace SOP, an Admin pages SOP) can be added under `../architecture/` when those surfaces undergo non-trivial changes.
