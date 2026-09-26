# Backend — Decisions Log

Append one entry per architectural decision. Each entry: date, decision, why.

## 2026-05-18 — Per-package System Pilot scaffolding

**Decision.** Scaffold `memory/`, `architecture/`, `execution/`, `.tmp/` per package (backend, frontend, extension) instead of once at the repo root.

**Why.** Three different deploy boundaries (Railway web + worker, Vercel SPA, Chrome Web Store). Each package has its own constitution, its own SOPs, its own probes. A single root scaffolding would conflate concerns.

## 2026-05-18 — Split `CONSTITUTION.md` from existing `CLAUDE.md`

**Decision.** Keep `CLAUDE.md` as the onboarding / how-to-work-in-this-codebase guide. Add `CONSTITUTION.md` per package for System Pilot artifacts (Data Schemas, Behavioral Rules, Architectural Invariants, B.L.A.S.T. outputs). Root `CLAUDE.md` gets one-line pointers to each.

**Why.** `CLAUDE.md` is 116 lines and well-tuned for onboarding. Merging the Constitution layer into it would push it past 400 lines and conflate audience (new contributor vs. AI assistant making architectural decisions).

## 2026-05-18 — R2 storage discipline excluded from behavioral rules

**Decision.** Despite R2 being half the source of truth (with Postgres), the "all file IO through `storage_service`" rule is documented as an architectural invariant but not promoted to a Rule-1-style refusal trigger.

**Why.** Out of scope per user direction for this hardening pass. Re-evaluate if `storage_service` bypasses appear in code reviews.

## 2026-05-18 — Costing pipeline determinism fix scope

**Decision.** The `costing_agent.py` system prompt will be edited to make `cost_calculator` invocation mandatory and require the LLM to transcribe `amount` from tool output rather than compute it. The `cost_calculator_tool.py` itself and `cost_breakdown_service.persist_from_agent_output` are unchanged.

**Why.** Surgical fix. The downstream consumer still expects an `amount` field in the LLM output; removing it would cascade. Transcription preserves the contract while pulling actual computation through the deterministic tool. The follow-up enforcement (`|amount - qty*rate| < 0.5` rejection in `cost_breakdown_service`) is documented in [../architecture/sop-costing-pipeline.md](../architecture/sop-costing-pipeline.md) as the closing-the-loop work.

## 2026-05-18 — `tender_analyzer_max_parallel=1` documented as an invariant

**Decision.** The sequential-analysis rule is promoted from a config comment to an explicit invariant in [../architecture/sop-tender-analyzer-v2.md](../architecture/sop-tender-analyzer-v2.md). Overrides require an entry here in `decisions.md` plus a feature flag, not a global env-var bump.

**Why.** The silent empty-synthesis failure mode is corrosive — runs succeed individually but synthesis comes back blank. Anyone bumping the env var without reading the failure-mode block reintroduces the defect.
