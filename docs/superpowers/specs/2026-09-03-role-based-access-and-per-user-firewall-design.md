# Role-based access and the per-user firewall

**Date:** 2026-09-03
**Status:** Approved for implementation
**Related:** [Usage metering and per-user budget caps](2026-09-03-usage-metering-and-budget-caps-design.md)

## Problem

The platform needs two working roles beneath `master_admin`, each seeing a
different version of the platform, and users must not see each other's work.

Half the mechanism exists. `User.role` is already `operator | admin |
master_admin`; every `/admin/*` route is already `MasterAdminRoute` on the
frontend and `require_master_admin` behind it. What does not exist in any form
is **isolation**: tenders are listed with no per-user filter, and while many
tables carry `created_by` or `user_id`, enforcement is inconsistent — proposal
sessions filter by user, most things do not.

## Roles

| role | reach |
|---|---|
| `master_admin` | everything, including all admin settings and all users' work |
| `tender_search` | the whole platform except `/admin/*` |
| `costing_research` | Command Center, tenders, dashboard, per-tender workspace (checklist + annexures), cost breakdowns + XLSX export, archive, notifications |

`costing_research` explicitly **cannot** reach: My Work (uploaded documents,
signing), signatures, the document generator and document library, and all of
`/admin/*`.

The existing `admin` and `operator` values map forward: `admin` →
`tender_search`, `operator` → `costing_research`. Both old values keep working
during the transition.

### What is shared and what is walled

**Shared — visible to every user who can see the tender:**

The tender record itself, including one a costing user uploads manually; its
captured documents; and the expensive per-tender AI output — the tender analysis
summary, checklists, and extracted annexures. These are facts about the tender,
computed once. A user who did not run the analysis can open that tender in their
own Command Center and work from the existing summary rather than paying to
regenerate it. This matters directly to the $30 cap: walling analysis per user
would bill the same PDFs against every user's budget.

**Walled — visible only to the user who created it, and to master_admin:**

Command Center chat sessions and messages, cost breakdowns, generated documents,
workspace content, signatures, uploaded personal documents, agent runs.

Isolation is **per individual user**, not per role. Two costing researchers do
not see each other's work.

## Design

### Ownership scoping in one place

A new `app/core/ownership.py` holds the whole rule:

- `OWNED_MODELS` — the registry of user-owned models and the column that owns
  each one (`created_by` on some, `user_id` on others; the codebase uses both).
- `scoped_query(db, model, actor)` — returns a query filtered to the actor,
  unfiltered for `master_admin`.
- `assert_can_read(obj, actor)` — the single-object check for detail routes,
  raising 404 rather than 403 so the wall does not confirm that another user's
  row exists.

Every list and detail route touching an owned model goes through these.

**A drift test makes the registry load-bearing.** It enumerates models carrying
an owner column and fails when one is absent from `OWNED_MODELS`, and it scans
route modules for unscoped queries against owned models. This mirrors the
existing `tool_policy` unclassified-tool tests: adding a new owned table fails
the suite loudly rather than shipping a silent cross-user leak. Per-endpoint
filters were rejected precisely because missing one leaks quietly.

A SQLAlchemy session-level global filter was also rejected: it would apply
invisibly to background agent sessions, the seeders, and the archive sweep,
none of which have a user context, and a security boundary should not be
implicit.

### The agent-shaped hole

The HTTP layer is not the only reader. Agent tools query with a raw `db` session
and no notion of who is asking, so without this section a costing user could ask
the Master Agent "show me the cost breakdown for tender 303" and a tool would
return another user's row — through the front door, with no HTTP route involved.

The fix hangs off the ambient actor introduced by the metering spec
(`app/core/actor_context.py`). `platform_tools.py` already reads the caller's
role from the database inside `run_decision_maker` to enforce its minimum-role
bounding, which proves the seam exists. Ownership scoping goes in the same
place:

- Tools that read owned models call `scoped_query` with `current_actor()`
  instead of querying unscoped.
- A tool that runs with no ambient actor and touches an owned model **refuses**,
  in the same fail-closed spirit as a gated tool called with no `policy_scope`
  open. Failing open here would silently defeat the wall.
- The drift test covers tools, not only routes.

### Route and navigation gating

Backend: a `require_roles(...)` dependency alongside the existing
`require_admin` / `require_master_admin`, applied to the routes
`costing_research` must not reach (documents generator/library, signatures,
personal document upload). Role is read from the database, never from the
request.

Frontend: `src/router.tsx` gains a `RoleRoute` guard beside the existing three,
and the navigation is filtered so a costing user is not shown links they cannot
use. **The frontend guard is cosmetic.** `localStorage.drpl_role` is user-editable,
so every restriction it expresses must also be enforced server-side; a test
asserts each role-gated route has a backend dependency.

### Migration of existing data

Every existing row predates the wall and has whatever `created_by` it was
written with. Rows with a null owner are treated as **master_admin-only** — the
conservative reading, since guessing an owner could hand one user another's
work. An admin can reassign from the user-management screen.

## Testing

- Each role reaches exactly its allowed surface; a `costing_research` user gets
  403 on documents/signatures/admin routes, 200 on tenders/costing/workspace.
- User A cannot read user B's session, cost breakdown, or generated document,
  by route or by agent tool; master_admin can read both.
- Shared objects stay shared: user B sees the tender, its documents and the
  analysis summary that user A's run produced.
- A tool touching an owned model with no ambient actor refuses.
- The two drift tests: unregistered owned model, unscoped query.
- Legacy `admin` / `operator` values keep working.

## Out of scope

Custom permission editing, per-tender ACLs, sharing or handoff between users,
and organisation/tenant grouping above the user. The isolation is per user, as
specified; group sharing would be a later change to `OWNED_MODELS` semantics.
