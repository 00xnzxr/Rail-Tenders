# BOQ Twin-Row Collapse — Design

**Date:** 2026-07-21
**Status:** Approved (brainstorming) — pending implementation plan
**Related:** [[costing-piece1-reconciliation-gate]] (`2026-07-20-costing-reconciliation-and-provenance-design.md`), [[costing-line-dedup]] (`2026-07-13-costing-dedup-design.md`)

## Problem

The costing Excel renders every NIT line item **twice**:

- an `A{n}` row — `rate_source=training_data`, blank tender rate / amount (an empty "shell"), and
- a `{n}` row — `rate_source=derived_estimate`, real values.

The NIT itself (source of truth) prints each item **once**, with a plain integer Item Code (`1, 2, 3, …`). The `A1, A2, …` codes are fabricated by one of the extraction passes and do not exist in the tender.

## Root cause (confirmed against live DB)

The duplication originates in **BOQ capture**, not in the costing/Excel layer:

1. `boq_parser_service._merge_boq_rows` unions two extraction passes. The AI/vision pass emits schedule-letter-prefixed codes (`A1`, per the `_BOQ_AI_SYSTEM_PROMPT` examples "A1, A71 …"); the pdfplumber text pass reads the literal Item Code column (`1`).
2. `_boq_row_key` keys a row on `(schedule, sr_no, item_code)`. `("A","1","A1")` and `("A","1","1")` are **different keys**, so the union keeps **both** rows for the same physical NIT line.
3. Downstream, the costing fabrication-lockdown gate (`_partition_lines_against_schedule`) keeps both because **both are legitimate captured `BOQItem` rows**; and `_dedup_lines_by_content` cannot merge them because their `unit` differs (`Coach` vs `Per Coach`) and their descriptions differ by a Unicode ligature (`ﬁ` vs `fi`).
4. The Piece 1 `_collapse_over_schedules` never fixes it: it only runs when a schedule is flagged `over`/`dup_over` by subtotal reconciliation, and its twin-key requires **matching `basic_value`**. For tender 3750 the `A{n}` twins are **empty shells** (null `basic_value`), so the schedule value-sum equals the exact printed total (₹11,255,870.40) and the schedule is never flagged — in fact it has **no captured subtotal at all** (`reconciliation_status='no_anchor'`), so the collapse is never even a candidate.

### Live-data evidence

- Tender 3750: 22 `boq_items` rows for 11 NIT lines; each `sr_no` has a valued `{n}` row and a null-value `A{n}` shell. `sr_no 10` additionally has a **wrong qty** on the shell (`A10`=38400 vs `10`=33600).
- **12 tenders, 991 `boq_items` rows** sit in twinned `(schedule, sr_no)` groups.
- The twin patterns are **not uniform**:
  - **True twins** — 3750 (`1` valued + `A1` shell), 2624 (`001` + `A1`, identical value). Must collapse.
  - **Legitimate distinct sub-items** — 2531 (`3a`/`4a`, different values), 1198 (`A1a`/`A1b`), 3746 (many distinct spare-part codes under one `sr_no`). Must **never** collapse.

## Non-goals

- No change to the two-pass extraction itself (why both passes disagree on code convention is out of scope; we reconcile at merge time).
- No schema change, no Alembic migration.
- No mutation or deletion of existing `CostBreakdown` versions. Cleanup fixes `boq_items` only; the user re-runs costing to obtain a clean Excel (a new breakdown version).

## Design

### 1. Twin-detection predicate — value redundancy

A single pure function is the source of truth for "are these rows the same physical line", used by BOTH the forward fix and the cleanup script so they can never diverge.

```
_collapse_value_redundant_twins(items) -> (survivors, dropped_count)
```

Group non-tax rows by `(schedule_name.upper(), sr_no)`. Within a group of ≥2 members:

1. **Value classification.** `v = basic_value` if present & non-zero; else `quantity × estimated_rate` if both present; else `None`. A member is a **shell** when `v` is `None`/0 **and** `estimated_rate` is `None`/0.
2. **Rule A — shell absorption.** If the group has shell(s) **and** exactly one distinct non-zero value among the non-shell members, drop the shells and keep the valued rows. (3750: drop `A1`, keep `1`. Side effect: the wrong-qty shell `A10`=38400 is dropped, correct `10`=33600 survives.)
3. **Rule B — identical-value dedup.** Among non-shell members, dedup those sharing the same `round(v, 2)` **and** `round(quantity, 3)`, keeping the best-code survivor. (2624: `001`/`A1` identical → keep one.)
4. **Safety invariant — never merge across distinct non-zero values.** `3a`(229,276) vs `4a`(861,403), `A1a` vs `A1b` survive untouched.

**Best-code / survivor selection** (when a rule keeps one of several): rank by `(has-non-null-value desc, code-is-bare-numeric desc, first-seen asc)`. This prefers the NIT-faithful bare-numeric code (`1`, `001`) and the row that carries the value over a letter-prefixed shell (`A1`). The survivor keeps its own richer fields.

**Unresolvable groups.** A group with multiple distinct non-zero values and no shell is left **completely untouched** (these are legitimate sub-items). The cleanup script additionally **reports** such groups so a human can inspect ambiguous cases rather than the tool silently leaving them.

Properties: pure, deterministic, idempotent, no DB/LLM/IO. No-op on already-clean tenders and on tax lines.

### 2. Forward fix — wire into extraction

Call `_collapse_value_redundant_twins(items)` in `parse_boq_from_pdf` immediately after the existing `items = _dedup_boq_rows(items)` (~line 1480), **unconditionally** — independent of `subtotals` / reconciliation status (proven necessary: 3750 and 2624 are never flagged, and 3750/2624 have no or `no_anchor` subtotals). Log the dropped count.

### 3. Data cleanup — all 12 affected tenders

Standalone script `drpl-backend/scripts/collapse_boq_twins.py`, reusing the same predicate:

- **Dry-run (default):** per affected tender, run the predicate in memory; print a table of rows that WOULD be deleted (id, schedule, sr_no, code, value) and survivors; report unresolvable groups. Writes nothing.
- **`--apply`:** delete only flagged twin rows, one tender per transaction, logging each deletion.
- **Guardrails:** never leave 0 survivors in a group (asserted); never touch rows outside a twinned group; **refuse to apply** if any schedule's post-collapse value-sum shifts beyond a rounding epsilon (dropping shells / identical dupes must preserve the value-sum).

The affected-tender list is derived at runtime by the same twin query, not hard-coded.

### 4. Testing

Pure-function unit tests (extend `tests/test_costing_dedup.py` or add under `tests/services/costing/`), seeded from the real twin shapes:

- 3750 `1`+`A1` shell → keep `1`, drop `A1`; qty-mismatch `sr_no 10` shell case.
- 2624 `001`+`A1` identical value → keep one.
- **Must-not-collapse:** 2531 `3a`/`4a`, 1198 `A1a`/`A1b`, 3746 distinct spares → all survive.
- Idempotence; tax lines never merged; unresolvable-group reporting.

Run in the backend venv (`drpl-backend/venv/`).

### 5. Rollout

1. Land pure function + tests (green tests gate merge).
2. Cleanup **dry-run** on all 12 tenders; user reviews deletion table + unresolvable report.
3. Cleanup `--apply`; verify twin count → 0 across all 12 tenders and value-sums unchanged.
4. Re-run costing on 3750; confirm the Excel is clean — one row per NIT item, bare-integer codes, real values.

Forward fix is pure Python, ships on next backend deploy; cleanup is a one-time manual script (not wired into startup).

## Accepted risks

- A genuine future NIT where a valued row and a truly-distinct shell share `(schedule, sr_no)` would over-collapse — bounded by the "exactly one distinct non-zero value" precondition in Rule A, and surfaced by the unresolvable-group report otherwise.
- Cleanup does not regenerate existing breakdowns; stale duplicated breakdown versions remain until the user re-runs costing (by design — history is preserved).
