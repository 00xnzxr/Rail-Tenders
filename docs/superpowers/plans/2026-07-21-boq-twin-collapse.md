# BOQ Twin-Row Collapse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the costing Excel from showing every NIT line twice by collapsing value-redundant BOQ twin rows at capture time, and clean the 991 already-persisted twin rows across 12 tenders.

**Architecture:** One pure predicate `_collapse_value_redundant_twins` in `boq_parser_service.py` is the single source of truth for "same physical line". It is wired unconditionally into `parse_boq_from_pdf` (forward fix) and reused verbatim by a dry-run-first cleanup script (existing data). No schema change, no migration.

**Tech Stack:** Python 3.14, SQLAlchemy, pytest. Backend venv: `drpl-backend/venv/` (base Python has no pytest).

## Global Constraints

- Predicate is **pure**: no DB, no LLM, no IO, deterministic, idempotent.
- Extraction row dicts carry value under `basic_value`, and rate under EITHER `unit_rate` OR `estimated_rate` (mirror `_reconcile_schedules` line 1234). Check both for rate.
- Group key is `(schedule_name.upper(), sr_no)`. Tax lines (`is_tax_line` truthy) are NEVER merged.
- **Safety invariant:** never merge rows with distinct non-zero values. A group with ≥2 distinct non-zero values and no shell is left untouched.
- Best-code survivor ranking: `(has-non-null-value desc, code-is-bare-numeric desc, first-seen asc)`.
- Cleanup touches `boq_items` only. Never mutate/delete `CostBreakdown` rows.
- All commands run from `drpl-backend/`. Run pytest via `venv/Scripts/python.exe -m pytest`.
- Work on branch `fix/boq-twin-collapse` (already created; design doc already committed there).

---

### Task 1: Value-redundancy twin-collapse predicate

**Files:**
- Modify: `drpl-backend/app/services/boq_parser_service.py` (add `_collapse_value_redundant_twins` + helpers near `_collapse_over_schedules`, ~line 1350)
- Test: `drpl-backend/tests/services/costing/test_boq_twin_collapse.py` (create)

**Interfaces:**
- Consumes: extraction row dicts with keys `schedule_name, sr_no, item_code, quantity, unit, basic_value, unit_rate?/estimated_rate?, is_tax_line?`.
- Produces:
  - `_row_value(it: dict) -> Optional[float]` — `basic_value` if non-zero, else `quantity*rate` (rate = `unit_rate` or `estimated_rate`), else `None`.
  - `_is_shell(it: dict) -> bool` — `True` when `_row_value` is None/0 AND rate (`unit_rate`/`estimated_rate`) is None/0.
  - `_is_bare_numeric_code(code) -> bool` — `True` when the code is all digits after stripping whitespace/leading zeros (e.g. `1`, `001`), `False` for `A1`, `3a`.
  - `_collapse_value_redundant_twins(items: list[dict]) -> tuple[list[dict], int, list[dict]]` — returns `(survivors, dropped_count, unresolvable_groups)`. `unresolvable_groups` is a list of `{"schedule": str, "sr_no": Any, "codes": list, "values": list}` for groups with ≥2 distinct non-zero values and no shell (reported, never mutated).

- [ ] **Step 1: Write the failing test file**

Create `drpl-backend/tests/services/costing/test_boq_twin_collapse.py`:

```python
from app.services.boq_parser_service import _collapse_value_redundant_twins


def _row(sched, sr, code, qty, unit, basic=None, rate=None, tax=False):
    return {
        "schedule_name": sched, "sr_no": sr, "item_code": code,
        "quantity": qty, "unit": unit, "basic_value": basic,
        "unit_rate": rate, "is_tax_line": tax,
    }


def test_shell_absorbed_keeps_valued_bare_numeric():
    # 3750 shape: valued '1' + null-value shell 'A1' under (A, 1)
    items = [
        _row("A", 1, "1", 1104.0, "Per Coach", basic=349603.68, rate=316.67),
        _row("A", 1, "A1", 1104.0, "Coach", basic=None, rate=None),
    ]
    survivors, dropped, unresolved = _collapse_value_redundant_twins(items)
    assert dropped == 1
    assert unresolved == []
    assert len(survivors) == 1
    assert survivors[0]["item_code"] == "1"
    assert survivors[0]["basic_value"] == 349603.68


def test_shell_with_wrong_qty_dropped():
    # 3750 sr_no 10: shell A10 has WRONG qty 38400 (null value) -> dropped,
    # valued '10' qty 33600 survives.
    items = [
        _row("A", 10, "A10", 38400.0, "Numbers", basic=None, rate=None),
        _row("A", 10, "10", 33600.0, "Numbers", basic=500000.0, rate=14.88),
    ]
    survivors, dropped, unresolved = _collapse_value_redundant_twins(items)
    assert dropped == 1
    assert len(survivors) == 1
    assert survivors[0]["item_code"] == "10"
    assert survivors[0]["quantity"] == 33600.0


def test_identical_value_twins_deduped_bare_numeric_wins():
    # 2624 shape: '001' and 'A1' identical value/qty -> keep one, prefer '001'.
    items = [
        _row("A", 1, "001", 12.0, "Job", basic=1137829.08, rate=94819.09),
        _row("A", 1, "A1", 12.0, "Job", basic=1137829.08, rate=94819.09),
    ]
    survivors, dropped, unresolved = _collapse_value_redundant_twins(items)
    assert dropped == 1
    assert unresolved == []
    assert len(survivors) == 1
    assert survivors[0]["item_code"] == "001"


def test_distinct_subitems_never_collapse():
    # 2531 shape: 3a (229276) and 4a (861403) are distinct sub-items.
    items = [
        _row("B", 3, "3a", 480.0, "Numbers", basic=229276.8, rate=477.66),
        _row("B", 3, "4a", 480.0, "Numbers", basic=861403.2, rate=1794.59),
    ]
    survivors, dropped, unresolved = _collapse_value_redundant_twins(items)
    assert dropped == 0
    assert len(survivors) == 2
    assert len(unresolved) == 1
    assert unresolved[0]["schedule"] == "B"
    assert unresolved[0]["sr_no"] == 3


def test_letter_subitems_never_collapse():
    # 1198 shape: A1a / A1b distinct values under (A, 1).
    items = [
        _row("A", 1, "A1a", 75.0, "Numbers", basic=130950.0, rate=1746.0),
        _row("A", 1, "A1b", 75.0, "Numbers", basic=545625.0, rate=7275.0),
    ]
    survivors, dropped, unresolved = _collapse_value_redundant_twins(items)
    assert dropped == 0
    assert len(survivors) == 2


def test_tax_lines_never_merged():
    items = [
        _row("A", 99, "GST", 1.0, "LS", basic=1000.0, rate=1000.0, tax=True),
        _row("A", 99, "GST2", 1.0, "LS", basic=1000.0, rate=1000.0, tax=True),
    ]
    survivors, dropped, unresolved = _collapse_value_redundant_twins(items)
    assert dropped == 0
    assert len(survivors) == 2


def test_idempotent():
    items = [
        _row("A", 1, "1", 1104.0, "Per Coach", basic=349603.68, rate=316.67),
        _row("A", 1, "A1", 1104.0, "Coach", basic=None, rate=None),
    ]
    once, d1, _ = _collapse_value_redundant_twins(items)
    twice, d2, _ = _collapse_value_redundant_twins(once)
    assert d2 == 0
    assert [r["item_code"] for r in once] == [r["item_code"] for r in twice]


def test_clean_tender_noop():
    items = [
        _row("A", 1, "1", 10.0, "Nos", basic=100.0, rate=10.0),
        _row("A", 2, "2", 20.0, "Nos", basic=400.0, rate=20.0),
    ]
    survivors, dropped, unresolved = _collapse_value_redundant_twins(items)
    assert dropped == 0
    assert len(survivors) == 2
    assert unresolved == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/Scripts/python.exe -m pytest tests/services/costing/test_boq_twin_collapse.py -v`
Expected: FAIL — `ImportError: cannot import name '_collapse_value_redundant_twins'`.

- [ ] **Step 3: Write the implementation**

In `drpl-backend/app/services/boq_parser_service.py`, add just after `_collapse_over_schedules` (ends ~line 1350). Uses `re` (already imported) and `Optional` (already imported):

```python
def _row_rate(it: dict):
    r = it.get("unit_rate")
    if r is None:
        r = it.get("estimated_rate")
    return r


def _row_value(it: dict) -> Optional[float]:
    """Best value estimate for a row: basic_value if non-zero, else qty*rate."""
    v = it.get("basic_value")
    try:
        if v is not None and float(v) != 0.0:
            return float(v)
    except (TypeError, ValueError):
        pass
    q, r = it.get("quantity"), _row_rate(it)
    try:
        if q is not None and r is not None:
            prod = float(q) * float(r)
            return prod if prod != 0.0 else None
    except (TypeError, ValueError):
        pass
    return None


def _is_shell(it: dict) -> bool:
    """A row carrying no value AND no rate — an empty duplicate placeholder."""
    if _row_value(it) not in (None, 0.0):
        return False
    r = _row_rate(it)
    try:
        return r is None or float(r) == 0.0
    except (TypeError, ValueError):
        return False


def _is_bare_numeric_code(code) -> bool:
    """True for NIT-faithful integer codes ('1', '001'); False for 'A1', '3a'."""
    if code is None:
        return False
    s = re.sub(r"\s+", "", str(code))
    return s.isdigit()


def _twin_survivor_rank(it: dict) -> tuple:
    """Higher tuple wins. Prefer valued rows, then bare-numeric NIT codes."""
    has_value = 1 if _row_value(it) is not None else 0
    bare = 1 if _is_bare_numeric_code(it.get("item_code")) else 0
    return (has_value, bare)


def _collapse_value_redundant_twins(
    items: list[dict],
) -> tuple[list[dict], int, list[dict]]:
    """Collapse value-redundant twin rows sharing one (schedule, sr_no).

    Merges ONLY: (A) shells absorbed into the single distinct valued row, and
    (B) non-shell rows with identical (round(value,2), round(qty,3)). Rows with
    distinct non-zero values are NEVER merged — a group with >=2 distinct
    non-zero values and no shell is left untouched and reported in the third
    return value. Tax lines pass through untouched. Pure, deterministic,
    idempotent. Returns (survivors, dropped_count, unresolvable_groups).
    """
    # Group indices by (schedule, sr_no); tax + no-sr_no rows never grouped.
    groups: dict[tuple, list[int]] = {}
    passthrough: list[int] = []
    for i, it in enumerate(items):
        sr = it.get("sr_no")
        if it.get("is_tax_line") or sr is None:
            passthrough.append(i)
            continue
        key = ((it.get("schedule_name") or "").upper(), sr)
        groups.setdefault(key, []).append(i)

    drop_idx: set[int] = set()
    unresolvable: list[dict] = []

    for (sched, sr), idxs in groups.items():
        if len(idxs) < 2:
            continue
        shells = [i for i in idxs if _is_shell(items[i])]
        non_shells = [i for i in idxs if i not in shells]

        # Distinct non-zero values among non-shell members.
        val_of = {i: round(_row_value(items[i]), 2) for i in non_shells
                  if _row_value(items[i]) is not None}
        distinct_vals = set(val_of.values())

        if len(distinct_vals) >= 2:
            # Legitimate distinct sub-items — never merge; report and skip.
            unresolvable.append({
                "schedule": sched, "sr_no": sr,
                "codes": [items[i].get("item_code") for i in idxs],
                "values": sorted(distinct_vals),
            })
            continue

        # Rule A — shell absorption: exactly one distinct value among non-shells.
        if shells and len(distinct_vals) == 1:
            drop_idx.update(shells)

        # Rule B — dedup identical-value non-shell twins by (value, qty).
        seen: dict[tuple, int] = {}
        for i in sorted(non_shells, key=lambda j: (-_twin_survivor_rank(items[j])[0],
                                                   -_twin_survivor_rank(items[j])[1], j)):
            v = val_of.get(i)
            q = items[i].get("quantity")
            try:
                qk = round(float(q), 3) if q is not None else None
            except (TypeError, ValueError):
                qk = q
            tk = (v, qk)
            if tk in seen:
                drop_idx.add(i)
            else:
                seen[tk] = i

    survivors = [it for i, it in enumerate(items) if i not in drop_idx]
    return survivors, len(drop_idx), unresolvable
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/Scripts/python.exe -m pytest tests/services/costing/test_boq_twin_collapse.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/boq_parser_service.py tests/services/costing/test_boq_twin_collapse.py
git commit -m "feat(boq): value-redundancy twin-collapse predicate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire predicate into extraction (forward fix)

**Files:**
- Modify: `drpl-backend/app/services/boq_parser_service.py:1480` (inside `parse_boq_from_pdf`)
- Test: `drpl-backend/tests/services/costing/test_boq_twin_collapse.py` (append integration-shape test is optional; the unit tests already cover the predicate — this task adds the call site + a log)

**Interfaces:**
- Consumes: `_collapse_value_redundant_twins` from Task 1.
- Produces: `parse_boq_from_pdf` returns twin-free `items`.

- [ ] **Step 1: Add the unconditional call after `_dedup_boq_rows`**

In `parse_boq_from_pdf`, replace the line at ~1480:

```python
    items = _dedup_boq_rows(items)
```

with:

```python
    items = _dedup_boq_rows(items)
    # Unconditional value-redundancy twin collapse. NOT gated on reconciliation:
    # empty-shell twins (A{n} with null value) leave the schedule value-sum
    # correct, so subtotal reconciliation never flags them — proven on live
    # tenders 3750 (no_anchor) and 2624 (no captured subtotal). See design
    # docs/superpowers/specs/2026-07-21-boq-twin-collapse-design.md.
    items, twin_dropped, twin_unresolvable = _collapse_value_redundant_twins(items)
    if twin_dropped:
        logger.info(
            f"[boq_parser] twin-collapse for {os.path.basename(file_path)}: "
            f"dropped {twin_dropped} value-redundant twin row(s)"
        )
    if twin_unresolvable:
        logger.warning(
            f"[boq_parser] twin-collapse for {os.path.basename(file_path)}: "
            f"{len(twin_unresolvable)} (schedule, sr_no) group(s) with distinct "
            f"non-zero values left untouched: "
            f"{[(g['schedule'], g['sr_no']) for g in twin_unresolvable]}"
        )
```

- [ ] **Step 2: Run the full predicate test + a smoke import**

Run: `venv/Scripts/python.exe -m pytest tests/services/costing/test_boq_twin_collapse.py -v`
Expected: PASS (unchanged — call site is exercised via existing tests of the predicate).

Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('app/services/boq_parser_service.py').read()); print('syntax ok')"`
Expected: `syntax ok` (avoids importing `app.main`, which runs prod schema self-heal — see [[local-env-targets-live-neon]]).

- [ ] **Step 3: Run the existing costing test suite for regressions**

Run: `venv/Scripts/python.exe -m pytest tests/test_costing_dedup.py tests/services/costing/ -q`
Expected: PASS (no regressions in the reconciliation/collapse suite).

- [ ] **Step 4: Commit**

```bash
git add app/services/boq_parser_service.py
git commit -m "fix(boq): collapse value-redundant twins unconditionally at capture

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Dry-run-first cleanup script for existing data

**Files:**
- Create: `drpl-backend/scripts/collapse_boq_twins.py`

**Interfaces:**
- Consumes: `_collapse_value_redundant_twins`, `_row_value` from Task 1; `BOQItem` model; `SessionLocal`.
- Produces: a CLI: default dry-run prints per-tender deletion tables + unresolvable report; `--apply` deletes flagged rows with a per-schedule value-sum guardrail.

- [ ] **Step 1: Write the script**

Create `drpl-backend/scripts/collapse_boq_twins.py`:

```python
"""Collapse already-persisted value-redundant BOQ twin rows.

DRY-RUN by default (writes nothing). Pass --apply to delete flagged rows.
Reuses boq_parser_service._collapse_value_redundant_twins so cleanup and the
forward fix share one predicate. Fixes boq_items only; existing CostBreakdown
versions are untouched — re-run costing for a clean Excel.

Usage (from drpl-backend/):
  venv/Scripts/python.exe scripts/collapse_boq_twins.py            # dry-run, all affected tenders
  venv/Scripts/python.exe scripts/collapse_boq_twins.py --tender 3750
  venv/Scripts/python.exe scripts/collapse_boq_twins.py --apply
"""
import argparse
import sys
from collections import defaultdict

from app.core.database import SessionLocal
from app.models.costing_template import BOQItem
from app.services.boq_parser_service import (
    _collapse_value_redundant_twins,
    _row_value,
)

EPS = 0.05  # per-schedule value-sum drift tolerance (rupees)

FIELDS = ("id", "schedule_name", "sr_no", "item_code", "quantity", "unit",
          "basic_value", "estimated_rate", "unit_rate")


def _row_to_dict(r: BOQItem) -> dict:
    return {
        "id": r.id,
        "schedule_name": r.schedule_name,
        "sr_no": r.sr_no,
        "item_code": r.item_code,
        "quantity": r.quantity,
        "unit": r.unit,
        "basic_value": r.basic_value,
        "estimated_rate": r.estimated_rate,
        "is_tax_line": r.is_tax_line,
    }


def _affected_tender_ids(db) -> list[int]:
    from sqlalchemy import text
    rows = db.execute(text("""
        SELECT tender_id FROM boq_items WHERE sr_no IS NOT NULL
        GROUP BY tender_id
        HAVING EXISTS (
          SELECT 1 FROM (
            SELECT schedule_name, sr_no FROM boq_items b2
            WHERE b2.tender_id = boq_items.tender_id AND b2.sr_no IS NOT NULL
            GROUP BY schedule_name, sr_no
            HAVING COUNT(DISTINCT item_code) > 1
          ) g
        )
        ORDER BY tender_id
    """)).fetchall()
    return [r[0] for r in rows]


def _sched_sums(dicts: list[dict]) -> dict:
    out = defaultdict(float)
    for it in dicts:
        if it.get("is_tax_line"):
            continue
        v = _row_value(it)
        if v is not None:
            out[(it.get("schedule_name") or "").upper()] += v
    return out


def process_tender(db, tid: int, apply: bool) -> tuple[int, list]:
    rows = db.query(BOQItem).filter(BOQItem.tender_id == tid).all()
    dicts = [_row_to_dict(r) for r in rows]
    survivors, dropped, unresolvable = _collapse_value_redundant_twins(dicts)
    survivor_ids = {d["id"] for d in survivors}
    to_delete = [d for d in dicts if d["id"] not in survivor_ids]

    print(f"\n===== tender {tid}: {len(rows)} rows -> drop {dropped} =====")
    for d in to_delete:
        print(f"  DELETE id={d['id']} sch={d['schedule_name']!r} sr={d['sr_no']} "
              f"code={d['item_code']!r} qty={d['quantity']} value={_row_value(d)}")
    for g in unresolvable:
        print(f"  UNRESOLVED sch={g['schedule']!r} sr={g['sr_no']} "
              f"codes={g['codes']} values={g['values']} (left untouched)")

    if apply and to_delete:
        before, after = _sched_sums(dicts), _sched_sums(survivors)
        for sched, bsum in before.items():
            if abs(bsum - after.get(sched, 0.0)) > EPS:
                print(f"  !! ABORT tender {tid}: schedule {sched!r} value-sum "
                      f"drift {bsum:.2f} -> {after.get(sched, 0.0):.2f} > {EPS}")
                db.rollback()
                return 0, unresolvable
        assert survivor_ids, f"tender {tid}: refusing to leave 0 survivors"
        del_ids = [d["id"] for d in to_delete]
        db.query(BOQItem).filter(BOQItem.id.in_(del_ids)).delete(
            synchronize_session=False)
        db.commit()
        print(f"  APPLIED: deleted {len(del_ids)} row(s).")

    return len(to_delete), unresolvable


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="delete flagged rows")
    ap.add_argument("--tender", type=int, help="limit to one tender_id")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        tids = [args.tender] if args.tender else _affected_tender_ids(db)
        print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}; tenders: {tids}")
        total, unresolved = 0, 0
        for tid in tids:
            n, unres = process_tender(db, tid, args.apply)
            total += n
            unresolved += len(unres)
        print(f"\nTotal rows {'deleted' if args.apply else 'to delete'}: "
              f"{total}; unresolved groups: {unresolved}")
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Verify the BOQItem import resolves**

Run: `venv/Scripts/python.exe -c "from app.models.costing_template import BOQItem; print('ok')"`
Expected: `ok`. (`BOQItem` is defined in `app/models/costing_template.py` — confirmed during planning.)

- [ ] **Step 3: Dry-run on tender 3750 only (safe, read-only)**

Run: `venv/Scripts/python.exe scripts/collapse_boq_twins.py --tender 3750`
Expected: prints 11 `DELETE` lines (all `A{n}` shells for schedule A), 0 UNRESOLVED, "to delete: 11".
**Note:** the DB is live prod (see [[local-env-targets-live-neon]]); dry-run writes nothing. If the sandbox classifier blocks the DB connection, run with the sandbox disabled ONLY after user confirmation.

- [ ] **Step 4: Dry-run on all affected tenders; user reviews**

Run: `venv/Scripts/python.exe scripts/collapse_boq_twins.py`
Expected: per-tender deletion tables for all 12 tenders + any UNRESOLVED groups.
**STOP — present the output to the user for review before any --apply.**

- [ ] **Step 5: Commit the script (dry-run verified; NOT yet applied)**

```bash
git add scripts/collapse_boq_twins.py
git commit -m "chore(boq): dry-run cleanup script for persisted twin rows

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Apply cleanup + verify (gated on user approval)

**Files:** none (operational task).

- [ ] **Step 1: Apply, one confirmation from the user first**

After the user approves the Task 3 dry-run output:
Run: `venv/Scripts/python.exe scripts/collapse_boq_twins.py --apply`
Expected: `APPLIED: deleted N row(s)` per tender; no `ABORT` lines; "Total rows deleted: ~991".

- [ ] **Step 2: Verify zero twins remain**

Run the twin-count probe (from `scratchpad/probe_twins.py`):
`venv/Scripts/python.exe <scratchpad>/probe_twins.py`
Expected: "Tenders with twin sr_nos: 0".

- [ ] **Step 3: Verify tender 3750 breakdown regenerates clean**

Re-run costing on tender 3750 through the app (user action / existing costing entrypoint), then re-open the Excel.
Expected: one row per NIT item, bare-integer item codes (`1..11`), real tender rate/amount, no `A{n}` rows.

- [ ] **Step 4: Merge the branch**

Use `superpowers:finishing-a-development-branch` to merge `fix/boq-twin-collapse` into `main`.

---

## Self-Review

**Spec coverage:**
- §1 predicate → Task 1 ✓ (Rules A/B, safety invariant, best-code rank, unresolvable report all tested)
- §2 forward fix (unconditional) → Task 2 ✓
- §3 cleanup dry-run-first + guardrails → Task 3 (script) + Task 4 (apply/verify) ✓
- §4 tests → Task 1 test file ✓
- §5 rollout order → Task ordering 1→2→3→4 ✓

**Placeholder scan:** none — all code and commands are concrete.

**Type consistency:** `_collapse_value_redundant_twins` returns a 3-tuple `(survivors, dropped, unresolvable)` everywhere (Task 1 def, Task 2 call, Task 3 call). `_row_value` used in Task 1 and Task 3. Row-dict keys (`schedule_name, sr_no, item_code, quantity, unit, basic_value, unit_rate/estimated_rate, is_tax_line`) consistent across tasks.

**Known follow-up for the implementer:** `BOQItem` is imported from `app.models.costing_template` (confirmed during planning); Task 3 Step 2 re-verifies.
