# Frontend Constitution

System Pilot artifact for `drpl-frontend/`. Onboarding lives in [../CLAUDE.md](../CLAUDE.md); this file is the Constitution layer for the React + Vite SPA.

The frontend is **not** the primary agentic surface — it consumes backend outputs. The Constitution here is intentionally thin: it pins the two consumer contracts (SSE event stream, workspace doc render) that, if broken, cascade across every feature.

---

## Data Schemas (consumer contracts)

### 1. SSE event consumer — `GET /api/runs/{run_id}/events`

Produced by [../drpl-backend/app/api/routes/runs.py:338-415](../drpl-backend/app/api/routes/runs.py). Consumed by the Command Center under `src/components/command-center/`.

```
event: token         → { delta: string, ts: number }
event: tool_call     → { name: string, args: object, id: string }
event: tool_result   → { id: string, output: string }
event: status        → { status: "succeeded" | "failed" | "cancelled", result?: object }
```

The frontend's SSE handler MUST treat unknown event names as forward-compatible (log + skip), not as errors.

### 2. Workspace document — HTML in DB

Workspace documents are HTML strings persisted on the backend (post the 2026-04-06 markdown→HTML migration noted in [../CLAUDE.md](../CLAUDE.md)). The render contract:

```jsonc
{
  "id": 123,
  "tender_id": 456,
  "title": "Cost Statement",
  "content_html": "<p>...</p>",      // ALWAYS HTML, never markdown
  "version": 7,
  "updated_at": "ISO-8601"
}
```

No markdown rendering path is allowed in the frontend. If a doc returns markdown, it is a backend defect; surface an error rather than silently rendering markdown-as-text.

---

## Behavioral Rules (enforceable)

### Rule 1 — Auth is read-only client-side

JWT lives in `localStorage.drpl_token`; role lives in `localStorage.drpl_role`. The frontend trusts these values for routing only — it never claims a user is authorized. The backend re-validates every request.

- **Refusal trigger.** Refuse any client-side check that gates a privileged action on `localStorage.drpl_role` alone without a backend call. The role guards (`AdminRoute`, `MasterAdminRoute` in `src/router.tsx`) route — they do not enforce.

### Rule 2 — API base URL from `VITE_API_URL`, no hard-codes

Every request goes through [src/lib/api.ts](src/lib/api.ts). No `fetch('https://drpl-platform-production.up.railway.app/...')` strings anywhere else in the source tree.

### Rule 3 — HTML-only workspace render

See Data Schema 2. No markdown library imported in workspace-render paths.

---

## Architectural Invariants

1. **Three route guards, exact roles.** `ProtectedRoute` (any logged-in), `AdminRoute` (admin | master_admin), `MasterAdminRoute` (master_admin only). All `/admin/*` is master-admin only.
2. **SPA rewrite via Vercel.** `vercel.json` rewrites all routes to `index.html` — never assume server-side rendering.
3. **Two large multi-component features.** Command Center (`src/components/command-center/`) and Workspace (`src/components/workspace/`). Cross-cutting changes there warrant a feature SOP under [architecture/](architecture/).

---

## B.L.A.S.T. Phase Outputs

| Phase | Output |
|---|---|
| **B — Blueprint** | Consumer contracts (above) — captured in [memory/task_plan.md](memory/task_plan.md). |
| **L — Link** | Probe at [execution/probes/backend-health.mjs](execution/probes/backend-health.mjs) — verifies `/health/capacity` shape. |
| **A — Architect** | SOPs under [architecture/](architecture/) — `sop-command-center-sse.md`, `sop-workspace-doc-render.md`. |
| **S — Stylize** | Existing component library. No new design-system layer added. |
| **T — Trigger** | Browser navigation (React Router). Auth-protected routes redirect to `/login` on 401. |

---

## Pointers

- SOPs: [architecture/README.md](architecture/README.md)
- Memory: [memory/](memory/)
- Probes: [execution/probes/](execution/probes/)
- Onboarding: [../CLAUDE.md](../CLAUDE.md)
