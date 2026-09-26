# Unified Capability Layer and the Master Agent

**Date:** 2026-09-02
**Status:** Approved for implementation
**Scope:** `drpl-backend`
**Plan:** `docs/superpowers/plans/2026-09-02-unified-capability-layer.md`

## Problem

The platform has a Master Agent (`decision_maker`, `claude-opus-5`) that was built
to orchestrate every other agent and repair platform state. Measured against the
live database on 2026-09-02:

- It has produced **three conversation turns in its entire history** —
  2026-06-15, 2026-06-28, 2026-07-31 — and the last one exceeded its time budget.
  Zero in the preceding 30 days.
- The Ctrl+K global assistant does **not** route to it. `GLOBAL_ASSISTANT_AGENT_TYPE`
  appears six times in `command_center.py` and every use concerns session listing
  or deletion, never agent selection. Ordinary messages go through the router,
  which escalates to the Master only under rules E1–E5.
- Its **content** reach is three tools: `run_with_llm`, `web_search`,
  `document_reader`. It has no `tender_lookup`, no `semantic_search`, no
  `ratecard_lookup`, no way to read a stored cost breakdown, and no file
  generation of its own.
- Its diagnostic tools all require an id the caller must already know
  (`inspect_tender(tender_id)`). It cannot browse, so "check the latest run"
  has no path to an answer.

It is functional — invoked directly it completes in ~13s on `claude-opus-5` — but
it is unreachable and, when reached, half-blind.

### Root cause: four catalogs that disagree

Capability is decided independently in four places, and they have diverged:

| Site | How it decides | Consequence |
|---|---|---|
| `tool_loader._TOOL_CLASS_REGISTRY` | 25 classes; empty `tools` column means the whole repo | The generic path is broad |
| Canonical graphs | Hardcoded `default_keys` per graph | `deep_analyzer` runs 4 tools while `canonical_registry` declares 6 |
| `canonical_registry.resolve_tool_keys` | Intersects the user's Agent Builder choice with those hardcoded keys | A tool assigned in the UI is silently dropped at runtime |
| `run_decision_maker` | Five inline builders | The Master cannot reach anything in the first two |

Three defects, one cause. A capability added today lands in one catalog and is
invisible to the other three.

### Two further facts this design depends on

- The worker roster exposes **22** `call_*` tools, including four aliases for the
  same analyzer (`deep_analyzer`, `tender_doc_analyzer`, `document_analyzer`,
  `tender_analysis`) and two each for checklist and proposal.
- Large tool sets measurably degrade tool selection. The codebase already argues
  this in `tool_loader.TOOL_FREE_AGENTS`, where the catalog is ~7,900 tokens of
  schema.

## Goals

1. One registry is the single source of truth for what capabilities exist, what
   each does, what it costs in risk, and who may hold it.
2. The Master Agent can reach every part of the platform, and **knows what each
   part is for** — from an authored manual, not inference.
3. The Master Agent is the chat entrypoint, not a path the classifier may or may
   not grant.
4. The three deferred defects are fixed at their shared root, in one change.
5. A large reach does not mean a large bound catalog.

## Non-goals

- Replacing LangGraph. Every defect found was plumbing: a toolless node, a
  `hasattr` check that is False under Pydantic v2, hardcoded key lists, an
  intersection filter, and routing rules. A framework migration re-implements all
  five and fixes none.
- Changing the costing pipeline's internals.
- Exposing capabilities outside this codebase. The registry is what an MCP server
  would later sit in front of; that server is not built here.

## Design

### 1. The capability registry

New module `app/services/langchain/capability_registry.py`.

```python
@dataclass(frozen=True)
class Capability:
    key: str                      # "inspect_tender"
    kind: Literal["class", "factory"]
    target: str                   # import path: "module:Attr"
    tier: str                     # READ | WRITE | DESTRUCTIVE  (tool_policy constants)
    min_role: str                 # operator | admin | master_admin
    surfaces: frozenset[str]      # {"master", "generalist", "specialist"}
    domain: str                   # see the domain list below
    purpose: str                  # one line: what it does
    use_when: str                 # when the agent should reach for it
    not_for: str                  # what it must not be used for
    produces: str                 # what comes back
    summary: str                  # plain-language, for the confirmation card
```

**`kind` is the load-bearing field.** Today's catalogs hold two incompatible
species: `tool_loader` holds *classes* instantiated with `db`, while
`orchestrator_tools` holds *factories* closing over `user_id`, `session_id` and
`stream_callback`. Naming both kinds is what allows one resolver, and therefore
one catalog. A single `build_catalog(db, *, surface, user_role, context)`
resolves either.

**Domains** — the structured bifurcation. Every capability belongs to exactly one:

| Domain | What lives here |
|---|---|
| `tender_intel` | Finding, reading and understanding tenders and their documents |
| `costing` | Rates, calculations, stored cost breakdowns |
| `documents` | Producing and managing deliverable files |
| `workspace` | The canvas: checklists, annexures, generated documents |
| `research` | The outside world: web search, page fetch |
| `platform_ops` | Health, settings, runs, users — the platform about itself |
| `diagnostics` | Inspecting and repairing state that has gone wrong |
| `delegation` | Handing work to a specialist agent |
| `meta` | Memory, clarification, arbitrary LLM calls |

**What the registry absorbs, and what keeps its API:**

- `tool_loader._TOOL_CLASS_REGISTRY` → entries with `kind="class"`.
- `tool_policy._READ_TOOLS` / `_WRITE_TOOLS` / `_DESTRUCTIVE_TOOLS` → the `tier`
  field. `classify_tool()` keeps its signature and behaviour, **including that an
  unregistered tool classifies as a write**.
- `tool_policy._SUMMARIES` → the `summary` field.
- `platform_tools` per-spec `min_role` → the `min_role` field. Build-time
  filtering stays a convenience; the call-time check inside each tool remains the
  actual control.
- `agent_tools_service.SYSTEM_TOOLS` → generated from the registry, so a
  capability that can run can always be assigned.

### 2. The Platform Capability Manual

`purpose` / `use_when` / `not_for` / `produces` are authored per capability and
rendered, grouped by domain, into the Master Agent's system prompt by
`render_capability_manual(surface, user_role)`.

This is the instruction set the Master reasons from. It does not infer what the
costing researcher is; it reads what it is, when to use it, and when not to.
Because the manual is generated from the registry, it cannot drift from what
actually exists — a capability added without manual fields fails a drift test.

The manual entry for the costing worker reads, in full:

> **`call_costing_researcher`** — *Produces a complete, itemised cost breakdown
> for a tender.*
> **Use when:** the user wants a costing produced — a BOQ priced, a bidding
> schedule costed, "price every item", "full costing", "estimate for this NIT".
> **Not for:** explaining, adjusting or answering questions about a costing that
> already exists — read it with `cost_breakdown_read` and answer directly.
> **Produces:** a saved CostBreakdown with one line per schedule row, plus a
> chat summary.

### 3. Delegation doctrine

The Master's prompt gains a doctrine section alongside the manual:

- **Produce vs. explain.** Producing a new artefact — a cost breakdown, a
  forensic analysis, annexure extraction, a checklist — is always the specialist's
  job. Explaining, adjusting, or answering about an existing one is the Master's.
- **Recognition cues, spelled out:** a bidding schedule, a BOQ, a NIT with line
  items, "price every item", "full costing" → `call_costing_researcher`, always,
  and never priced by the Master itself.
- **The reason, stated:** the canonical costing path parses the schedule and
  prices every row in batches. A freeform pass collapses a 138-row spares
  schedule into a single summary line, and nothing raises an error when it does.

Non-blocking net: `quality_tools.diagnose_tender_outputs` already detects
collapsed schedules deterministically. It runs on costing output and flags. It
never overrides the Master.

### 4. Two-tier catalog

Reach must be total; bound schema must stay small.

**Tier 1 — always bound (~20 tools):**
- the deduplicated worker `call_*` set (aliases collapse 22 → ~9);
- the cross-cutting reads every job starts from: `tender_lookup`,
  `document_reader`, `cost_breakdown_read`, `semantic_search`, `web_search`;
- conversation control: `propose_plan`, `ask_user`, `clarify`;
- the dispatcher below.

**Tier 2 — everything else, through one tool:**

```python
use_capability(key: str, args: dict) -> str
```

Its description carries the domain index — every Tier 2 key with its one-line
`purpose`, grouped by domain — so the model can see what exists without paying
for each schema. It validates `key` against the registry and `args` against that
capability's schema, and on a mistake returns the expected argument shape rather
than a bare failure. Role and policy are applied identically to a directly bound
call; the dispatcher is not a way around either.

### 5. The Master Agent as the chat entrypoint

`stream_router_response` has four callers — `command_center.py` (two),
`langchain_agents.py`, and the RQ worker in `run_tasks.py` — plus
`workflow_streaming_handler`, which is a drop-in replacement for it. It keeps its
signature and becomes a thin dispatcher over a new `stream_master_response`, so
every entrypoint switches at one seam and the SSE contract the frontend already
handles is unchanged.

A `chat_engine` PlatformSetting selects `master` (new default) or `router`
(today's behaviour), read live. This ships with the change because it puts Opus on
every message: reverting must not require a deploy.

`classify_intent_node` stops being a gatekeeper. It remains in the module for
non-chat callers; nothing about what a user may ask depends on it.

`general_assistant` becomes a callable worker — Sonnet with a focused belt — that
the Master delegates ordinary conversational and research work to rather than
answering on Opus. It keeps its self-call guard.

### 6. Write gate, loosened deliberately

New PlatformSetting `confirm_gate_scope`:

- `destructive_only` — **the new default.** Ordinary writes run uninterrupted.
  `_DESTRUCTIVE_TOOLS` (`init_workspace_force`, `finalize_document`) still raise
  the confirmation card.
- `all_writes` — today's behaviour, one setting away.

`ASSISTANT_CONFIRM_GATE_ENABLED` stays. The decision lives in `tool_policy` alone.

Because a prompt is being traded for a log, **every ungated write records an audit
entry** through the existing `_log_audit` path: tool, arguments, user, outcome.
Role bounding is untouched.

## Risks, accepted deliberately

1. **Opus on every message**, including trivial ones. Mitigated by prompt caching
   (already enabled; measured 14,192 cache-creation tokens, read at ~10% on later
   turns in a session), by the alias dedupe, and by `master_agent_model` being
   configurable.
2. **Costing delegation becomes judgement, not routing.** Mitigated by the manual,
   the doctrine, and the post-hoc collapsed-schedule flag — not by a pre-dispatch,
   which was considered and rejected in favour of an agent that understands the
   platform.
3. **Ungated ordinary writes.** Mitigated by the audit trail and by
   `confirm_gate_scope` being one setting away from today's behaviour.

## Success criteria

- The Master's catalog is a strict superset of today's `decision_maker` catalog
  unioned with the general assistant's belt, asserted by enumeration.
- Every registry capability has manual fields, a tier, a role and a surface.
- A tool assigned in Agent Builder either loads or is reported — never silently
  dropped.
- `deep_analyzer` runs with exactly the tools the registry declares.
- Ctrl+K reaches the Master Agent on every message, with `chat_engine=router`
  restoring the old path exactly.
