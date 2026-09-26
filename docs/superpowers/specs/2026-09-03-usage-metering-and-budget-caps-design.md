# Usage metering and per-user budget caps

**Date:** 2026-09-03
**Status:** Approved for implementation
**Related:** [Role-based access and the per-user firewall](2026-09-03-role-based-access-and-per-user-firewall-design.md)

## Problem

Each user is to receive a **$30 monthly budget** for platform AI spend, shown in
the frontend as a percentage bar, and enforced when exhausted.

None of that is measurable today, though the reason is narrower than it first
appears. Rows *are* written: `DRPLCallbackHandler.on_llm_end` calls
`ai_service._log_usage` for every LLM call, on a fresh `SessionLocal` so
telemetry can never poison the caller's transaction, and wrapped so a failure
cannot break a run. That machinery is sound and stays.

What is missing is the single column a per-user budget needs. `_log_usage`
never sets `user_id`, and the callers that spend the most — every graph
constructing a `DRPLCallbackHandler` — have no user id to pass it. Every row on
the LangChain path is therefore unattributed, and the platform cannot answer
"what did this user spend" for the majority of what it spends.

## What we are building

1. Every LLM call made anywhere on the platform writes one `APIUsageLog` row
   attributed to the acting user.
2. A per-user monthly budget (default $30, per-user overridable) with a soft
   warning at 80% and a hard block at 100%.
3. A percentage bar in the frontend showing remaining budget.
4. A master-admin surface to see usage and to release a blocked user.

## Design

### The actor context (shared foundation)

Both this subsystem and the firewall need the acting user's identity available
deep inside the agent layer, where there is no request and no `current_user`.

`DRPLCallbackHandler` is constructed at 8 sites, most of them inside graphs that
never received a user id. Threading a parameter through all of them would be
invasive and would silently miss any site that has no user to pass.

Instead, a new `app/core/actor_context.py` provides an ambient actor, following
the pattern `app/core/run_context.py` already establishes for run ids:

```python
@contextmanager
def actor_scope(user_id: int | None, role: str | None = None): ...

def current_actor() -> Actor | None:   # (user_id, role)
```

The scope is opened at the entry points where a real user is known — the
Command Center SSE handler, the runs router, agent chat — alongside the existing
`run_id_scope` and `policy_scope` calls. `create_task` copies the context, so
the binding survives into the task the same way `policy_scope` already does.

`tool_policy._current_user_id` keeps working as-is; it is set by `policy_scope`
and is not disturbed. Where both are available they must agree, and a test
asserts that.

### Writing usage rows

`_log_usage` resolves `user_id` from `current_actor()` rather than from a new
parameter, so every existing caller gains attribution without a signature
change and without eight call sites to keep in sync. A missing actor records
the row unattributed — seeders, the archive sweep, scheduled jobs — rather than
guessing an owner.

The two properties that already hold are preserved and pinned by tests: the
write happens on its own short-lived session, and a metering failure logs and
continues instead of breaking the run.

One thing does have to change:

- **Cache tokens must be priced correctly.** `provider_config.estimate_cost`
  currently reads only `input_tokens` and `output_tokens` from the usage dict
  and ignores `cache_read_input_tokens` / `cache_creation_input_tokens`
  entirely — even though the handler already collects both. Anthropic bills
  cache reads at ~0.1x input and cache writes at 1.25x. Charging a cache read
  at full input price would systematically overbill users against a real $30
  cap, so `estimate_cost` is extended to price the cache components. This is a
  correctness fix to an existing function, and it changes numbers on the
  existing admin dashboard as a side effect.

### The budget

New table `user_budgets`:

| column | type | note |
|---|---|---|
| `user_id` | int, unique, indexed | FK to users.id |
| `monthly_limit_usd` | float | default from config (`default_monthly_budget_usd = 30.0`) |
| `override_until` | datetime, nullable | a master-admin release, valid to end of the current period |
| `override_granted_by` | int, nullable | which admin granted it |
| `override_note` | text, nullable | why |

A user with no row uses the config default. The limit is per-user so an admin
can raise one person without changing everyone.

**Period:** calendar month, resetting at 00:00 UTC on the 1st. Spend for the
period is `SUM(cost_estimate)` over `APIUsageLog` for that user since the period
start — derived, never a stored counter that can drift from its own ledger.

**Statuses:** `ok` (<80%), `warning` (80–100%), `exceeded` (>=100%).

### Enforcement

Checked once at run start, not per LLM call. `assert_within_budget(db, user)`
raises HTTP 402 when the user is `exceeded` and holds no active override.

It is called at the three points a run begins: the Command Center SSE handler,
the runs router, and agent chat. A run already executing is never killed
mid-flight — that would waste everything it had already spent and produce
exactly the half-finished state the Master Agent budget fix was about.

The consequence is bounded overshoot: a user can end a period slightly over
100% by the cost of the single run that crossed the line. That is accepted. The
alternative — enforcing inside `llm_factory` on every call — kills runs
mid-delegation and was rejected.

**master_admin is metered but never blocked.** An owner must not be able to
lock themselves out of their own platform.

**At 100% everything agentic stops.** No self-serve override. The user sees
"budget exhausted — contact your administrator"; a master admin grants a release
from the admin panel, which writes `override_until` and is recorded in
`AuditLog`. Reads, navigation, and previously-produced output remain fully
available — only new agent work is blocked.

### API

- `GET /api/usage/me` → `{period_start, spend_usd, limit_usd, percent_used, status, override_active}`. Any authenticated user, own data only.
- `GET /api/admin/usage` → per-user rollup for the current period. master_admin only.
- `POST /api/admin/usage/{user_id}/override` → grant a release. master_admin only, audit-logged.
- `PUT /api/admin/usage/{user_id}/limit` → set that user's monthly limit. master_admin only, audit-logged.

### Frontend

A budget bar in the app shell, fed by `GET /api/usage/me`, polled on navigation
and refreshed when a run completes. Green below 80%, amber in the warning band,
red at exhausted with the "contact your administrator" message. A blocked run
surfaces the 402 as that same message rather than a generic error.

Master admins get a usage table under `/admin` with the per-user rollup and the
override and limit controls.

## Testing

- A run through `DRPLCallbackHandler` writes an `APIUsageLog` row carrying the
  ambient actor's `user_id`. This is the regression that currently exists.
- A metering failure does not fail the run, and does not poison the run's
  session.
- `estimate_cost` prices cache reads below input rate and cache creation above
  it; a cached-heavy call costs less than the same call priced without cache
  awareness.
- Period spend excludes rows from the previous period.
- `assert_within_budget` raises for an exceeded user, passes for one under,
  passes for an exceeded user holding a live override, and passes for
  master_admin regardless.
- A drift test: every entry point that starts an agent run calls
  `assert_within_budget`. An unguarded new entry point fails the suite, in the
  spirit of the existing `tool_policy` classification tests.

## Out of scope

Hard multi-currency support, invoicing, prepaid top-ups, and per-tender or
per-client cost attribution. The ledger this builds would support them later.
