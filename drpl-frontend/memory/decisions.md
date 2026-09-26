# Frontend — Decisions Log

## 2026-05-18 — Frontend Constitution intentionally thin

**Decision.** The frontend Constitution covers only the two consumer contracts (SSE event stream, workspace HTML render) and three thin rules (auth read-only, `VITE_API_URL` no hard-codes, HTML-only workspace).

**Why.** The frontend is not the agentic surface — it consumes backend outputs. Loading it up with arbitrary rules would create governance theater without protecting any real invariant. The two SOPs cover the real cascade risks.

## 2026-05-18 — No new design-system / styling layer added

**Decision.** Stylize phase is "n/a" for this pass — existing components and styling are out of scope.

**Why.** Scope guard. The System Pilot pass is about agentic system hardening, not visual refresh.
