# Costing line-item content dedup — design

**Date:** 2026-07-13
**Status:** Approved, ready for implementation plan
**Surface:** `drpl-backend/app/services/cost_breakdown_service.py`

## Problem

The costing agent's exported Excel shows **duplicate rows** — the same physical
work item appearing twice with different `rate_source` values. Concrete case
(reference: ECOR Mancheswar Non-Core Work NIT):

| SN | Description | Qty | Unit | rate_source |
|----|-------------|-----|------|-------------|
| 2  | Scrapping, cleaning and painting work of Battery Box … (ICF type coaches) | 2,208 | Numbers | `derived_estimate` |
| A2 | Scrapping, cleaning and painting work of Battery Box … (ICF type coaches) | 2,208 | Numbers | `training_data` |

Same description, same quantity, same unit — two rows, two different source tags
and two different SNs (`2` vs `A2`).

## Root cause

The XLSX builder (`xlsx_generator_tool.py`) renders exactly the rows it is
given — it is not the source of the duplication. The duplicate rows are created
**upstream**: the LLM emits the same work item twice (once as a
`derived_estimate` labour roll-up, once as a `training_data` benchmark), and
nothing collapses them before they are persisted as `CostBreakdownLine` rows.

There is an existing dedup gate — `_partition_lines_against_schedule`
(`cost_breakdown_service.py`, the fabrication-lockdown / RULE 5 pass). It drops a
line that "duplicates an already-kept NIT row." But it has two holes that this
case falls through:

1. **Skipped for component build-up.** `persist_from_agent_output(...,
   skip_nit_validation=True)` (the `component_expansion` route) bypasses the gate
   entirely — no dedup runs at all.
2. **Only dedups by NIT-row identity.** Two agent lines collapse only if they
   resolve to the *same* `boq_item_id` or `(schedule_name, item_code)`. The
   screenshot rows have **different SNs** (`2` vs `A2`), so they map to
   different/no NIT rows and both survive.

The three costing routes (`_pick_costing_strategy`):
- `batched` (deterministic NIT skeleton) — structurally immune; fills rates into
  a fixed 1:1 skeleton, never adds rows. **Untouched by this change.**
- `single` (freeform ReAct) — trusts the LLM row list; hole #2 applies.
- `component_expansion` — trusts the LLM row list; hole #1 applies (no dedup).

## Fix

A **content-based dedup pass** that runs on every persistence path (including
component build-up), independent of the NIT-schedule identity check. It collapses
lines describing the same work regardless of differing SN / item_code /
rate_source, keeping the best-evidence occurrence.

### New unit — `_dedup_lines_by_content`

Pure helper in `cost_breakdown_service.py`:

```python
def _dedup_lines_by_content(line_items: list[dict]) -> tuple[list[dict], int]:
    """Collapse lines describing the same physical work item, keeping the
    best-evidence occurrence. Returns (deduped_lines, dropped_count)."""
```

- **Does:** groups lines by a content key, keeps one winner per group, returns
  survivors in original order plus the count of dropped rows.
- **Used by:** called once inside `persist_from_agent_output`, on every path.
- **Depends on:** only the line dicts + a small source-rank constant. No DB, no
  I/O — unit-testable in isolation.

### Match key ("same work item")

`(normalize(description), round(qty, 3), normalize(unit))` where `normalize` =
lowercased, internal whitespace collapsed, surrounding punctuation stripped.

Deliberately strict — description **and** qty **and** unit must all match. If the
agent emits the same work with a different qty, it will **not** merge. This is
intentional: never silently drop cost.

Never-merge guards:
- **Tax lines** (`is_tax_line=True`) are never deduped — they legitimately repeat
  across schedules.
- Lines with empty / `(no description)` description are never merged (no reliable
  key).

### Winner selection

Rank by `rate_source` (evidence order, best → weakest):

```
training_data (6) > tender_estimate (5) > web_search (4) >
memory (3) > derived_estimate (2) > needs_user_input (1) > unknown (0)
```

Highest rank wins; ties broken by first occurrence. Losers are discarded (their
rate is weaker by definition — no data is "lost" that a stronger source didn't
already supply).

### Integration point

In `persist_from_agent_output`, immediately **after**
`_partition_lines_against_schedule` (when it runs) and **before**
`_normalize_line_dict`:

```python
    line_items, dup_dropped = _dedup_lines_by_content(line_items)
    if dup_dropped:
        logger.warning(
            f"[cost_breakdown] tender {tender_id}: content-dedup dropped "
            f"{dup_dropped} duplicate line(s)"
        )
```

Layered defense: the existing NIT-row dedup still runs first (catches
same-item_code dupes); this new pass catches the different-SN / different-source
case both gates miss today, and also covers the `skip_nit_validation=True`
component path that has no dedup at all.

### Visibility

Silent to the user — backend `logger.warning` only. No `assumptions` note added.

## Testing

Unit tests on `_dedup_lines_by_content` in isolation:

1. **Screenshot case** — two rows same desc/qty/unit, sources `derived_estimate`
   + `training_data` → one row kept, `training_data` wins, `dropped_count == 1`.
2. Distinct qty → both kept.
3. Distinct unit → both kept.
4. Two tax lines same desc → both kept (tax never merged).
5. Empty / missing descriptions → both kept.
6. Order of survivors preserved.

## Out of scope (YAGNI)

- Fuzzy / similarity matching.
- Any `assumptions` note (silent per decision).
- Any change to `xlsx_generator_tool.py`.
- Any change to the deterministic `batched` skeleton path.
