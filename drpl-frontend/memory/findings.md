# Frontend — Findings

## Router

- `src/router.tsx` — three guards: `ProtectedRoute`, `AdminRoute` (admin | master_admin), `MasterAdminRoute` (master_admin only)
- All `/admin/*` routes are master-admin only
- Role from `localStorage.drpl_role` (set at login)

## Auth

- JWT in `localStorage.drpl_token`
- `src/lib/api.ts` attaches token to every request, redirects to `/login` on 401

## Large multi-component features

- `src/components/command-center/` — Command Center at `/command-center/:sessionId`
- `src/components/workspace/` — per-tender Workspace at `/tenders/:id/workspace/*`

## Build / deploy

- Vite + React 18
- Vercel SPA — `vercel.json` rewrites all routes to `index.html`
- `VITE_API_URL` defaults to `http://localhost:8000`
