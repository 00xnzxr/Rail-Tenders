# Frontend Architecture SOPs

The frontend is not the primary agentic surface — it consumes backend outputs. These two SOPs cover the consumer contracts that, if broken, cascade across every feature.

| SOP | Surface | Status |
|---|---|---|
| [sop-command-center-sse.md](sop-command-center-sse.md) | SSE consumer for agent run streams | Active |
| [sop-workspace-doc-render.md](sop-workspace-doc-render.md) | HTML-from-DB render contract | Active |

## Cross-cutting

- **Auth.** JWT in `localStorage.drpl_token`; attached by [../src/lib/api.ts](../src/lib/api.ts) to every request. 401 → redirect to `/login`.
- **Route guards.** Three exact roles: `ProtectedRoute`, `AdminRoute`, `MasterAdminRoute`. All `/admin/*` is master-admin only.
- **SPA rewrite.** Vercel rewrites all routes to `index.html` ([../vercel.json](../vercel.json)).
