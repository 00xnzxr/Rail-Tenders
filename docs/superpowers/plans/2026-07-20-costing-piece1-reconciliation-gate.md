# Costing Piece 1 — Extraction Fix + Reconciliation Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `boq_parser_service.py` from persisting integer-multiple duplicated BOQ rows (2×–21× the NIT's printed schedule total), and record a tender-level reconciliation status so a wrong schedule is visible instead of silent.

**Architecture:** Extend the *existing* reconciliation machinery in `boq_parser_service.py` (do NOT rebuild it). Today `_reconcile_schedules` only detects **SHORT** schedules (extracted sum *below* printed) and re-extraction *adds* rows. The proven bug is the opposite — **OVER** schedules (extracted sum an integer multiple *above* printed) from merged NIT cells — which the current gate ignores. We (1) teach `_reconcile_schedules` to also report OVER schedules with their integer multiple, (2) add a deterministic collapse pass that divides an over-counted schedule back down to its printed total, (3) persist a per-schedule `reconciliation_status` on `BOQScheduleTotal`, and (4) harden the extraction prompt against merged-row repetition. All gate logic stays pure/deterministic; only extraction is LLM.

**Tech Stack:** Python 3, SQLAlchemy, pytest, LangChain (extraction prompt only).

## Global Constraints

- **Local `.env` `DATABASE_URL` is the LIVE production Neon DB.** Never run write operations against it during development. Unit tests use pure functions with in-memory dicts — no DB. The read-only probe (`probe_nit_totals.py`) is the only DB touch and is SELECT-only.
- Determinism boundary: the reconciliation *gate* and the collapse pass are pure/deterministic. Only *extraction* (`_walk_pages_chunked`) is LLM. The "is this correct / how many copies" decision is arithmetic against the NIT's printed number — never a model judgment.
- Schema-drift discipline: a new DB column REQUIRES an Alembic revision **and** an entry in `_apply_schema_drift_fixes()` (`app/main.py:161`, Postgres) **and** `_add_missing_columns()` (`app/main.py:571`, SQLite) so existing deployments self-heal.
- Tunable knobs go through the existing settings-service pattern (`_get_float_setting(db, "costing.<key>", default)`), NOT `app/core/config.py`. Existing keys live under the `costing.boq_*` namespace.
- Tax lines (`is_tax_line` truthy) are never collapsed or counted in a schedule sum (matches `_reconcile_schedules:1205`).
- Never silently ship a wrong sheet: an unresolved OVER/SHORT schedule sets `reconciliation_status="failed"` and keeps the existing `extraction_confidence="low"` row flag.
- Reuse existing helpers verbatim — `_boq_row_key` (`:798`), `_dedup_boq_rows` (`:1003`), `_merge_boq_rows` (`:828`), `_get_float_setting`/`_get_int_setting`/`_get_bool_setting` (`:894`–`:918`).

---

## File Structure

- `app/services/boq_parser_service.py` — all extraction/reconcile/collapse logic. Extended, not restructured.
- `app/models/costing_template.py` — add `reconciliation_status` column to `BOQScheduleTotal`.
- `app/main.py` — schema-drift self-heal entries for the new column.
- `alembic/versions/` — new revision for the new column.
- `tests/test_boq_reconciliation.py` — new; pure-function tests using the real DB numbers.

---

### Task 1: Detect OVER (integer-multiple) schedules in `_reconcile_schedules`

Today the function returns `(all_ok, report_lines, short_codes)` and only flags schedules whose extracted sum is *below* printed. Extend it to also detect schedules whose extracted sum is an integer multiple *above* printed (the duplication case) and return, per over-counted schedule, the detected multiple.

**Files:**
- Modify: `app/services/boq_parser_service.py:1193-1240` (`_reconcile_schedules`)
- Test: `tests/test_boq_reconciliation.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_reconcile_schedules(items, subtotals, tolerance_pct) -> tuple[bool, list[str], set, dict[str, int]]` — a 4-tuple now. The new 4th element `over: dict[str, int]` maps `schedule_code -> integer_multiple` (e.g. `{"A": 11, "D": 2}`) for schedules whose extracted sum ≈ printed × k for integer k ≥ 2 within tolerance. Consumed by Tasks 2 and 3. **All existing callers (`_extract_doc_complete:1292,1312,1336`) must be updated to unpack the 4th value** (they currently unpack 3).

- [ ] **Step 1: Write the failing test**

Create `tests/test_boq_reconciliation.py`:

```python
"""Pure-function tests for the BOQ reconciliation gate. No DB.

Numbers are the real live-DB cases pulled during design (tenders 2693/2723/
2732/2733) — see docs/superpowers/specs/2026-07-20-costing-reconciliation-and-
provenance-design.md section 1.
"""
from app.services.boq_parser_service import _reconcile_schedules


def _rows(code, n, basic_value):
    """n identical rows in one schedule, each carrying basic_value."""
    return [
        {"schedule_name": code, "sr_no": i + 1, "item_code": f"{code}{i+1}",
         "basic_value": basic_value, "is_tax_line": False}
        for i in range(n)
    ]


def test_clean_schedule_reconciles_no_over_no_short():
    # t=2723 sch=B: one row summing exactly to printed -> ok.
    items = [{"schedule_name": "B", "sr_no": 1, "item_code": "B1",
              "basic_value": 2026057.0, "is_tax_line": False}]
    subtotals = {"B": 2026057.0}
    ok, report, short, over = _reconcile_schedules(items, subtotals, 2.0)
    assert ok is True
    assert short == set()
    assert over == {}


def test_exact_2x_duplication_detected_as_over():
    # t=2732 sch=D: extracted 800160 vs printed 400080 -> multiple 2.
    items = _rows("D", 2, 400080.0)  # two copies of the one true row
    subtotals = {"D": 400080.0}
    ok, report, short, over = _reconcile_schedules(items, subtotals, 2.0)
    assert ok is False
    assert over == {"D": 2}
    assert "D" not in short  # OVER is not SHORT


def test_11x_duplication_detected_as_over():
    # t=2693 sch=A: extracted ~11x printed.
    printed = 5975128.0
    items = _rows("A", 11, printed)  # 11 copies
    subtotals = {"A": printed}
    ok, report, short, over = _reconcile_schedules(items, subtotals, 2.0)
    assert over == {"A": 11}


def test_short_schedule_still_detected_and_not_over():
    # Extracted below printed -> SHORT (existing behavior preserved).
    items = [{"schedule_name": "C", "sr_no": 1, "item_code": "C1",
              "basic_value": 500000.0, "is_tax_line": False}]
    subtotals = {"C": 1000000.0}
    ok, report, short, over = _reconcile_schedules(items, subtotals, 2.0)
    assert ok is False
    assert "C" in short
    assert over == {}


def test_non_integer_ratio_is_not_over():
    # 1.5x is neither a clean multiple nor short-by-tolerance in the wrong dir;
    # must NOT be reported as OVER (we only auto-handle integer multiples).
    items = _rows("E", 3, 500000.0)  # sum 1.5M
    subtotals = {"E": 1000000.0}     # ratio 1.5
    ok, report, short, over = _reconcile_schedules(items, subtotals, 2.0)
    assert over == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py -v`
Expected: FAIL — `ValueError: not enough values to unpack (expected 4, got 3)` (function still returns a 3-tuple).

- [ ] **Step 3: Write minimal implementation**

In `app/services/boq_parser_service.py`, replace the body of `_reconcile_schedules` (`:1193-1240`) — keep the sum/count loop (`:1201-1223`) unchanged; replace the reporting block (`:1225-1240`) with over-detection added:

```python
    report: list[str] = []
    short: set = set()
    over: dict[str, int] = {}
    OVER_RATIO_TOL = 0.02  # how close sum/printed must be to an integer >= 2
    for code, printed in sorted(subtotals.items()):
        if not printed or printed <= 0:
            continue
        got = sums.get(code, 0.0)
        n = counts.get(code, 0)
        delta_pct = (printed - got) / printed * 100.0
        ratio = got / printed if printed else 0.0
        # OVER: extracted is a clean integer multiple (>=2) of printed.
        multiple = round(ratio)
        is_over = multiple >= 2 and abs(ratio - multiple) <= OVER_RATIO_TOL
        # SHORT: extracted materially below printed (existing behavior).
        is_short = (not is_over) and ((n == 0) or (delta_pct > tolerance_pct))
        if is_over:
            over[code] = multiple
        if is_short:
            short.add(code)
        status = "OVER x%d" % multiple if is_over else ("SHORT" if is_short else "ok")
        report.append(
            f"schedule {code}: {n} row(s), extracted Rs {got:,.2f} vs printed "
            f"Rs {printed:,.2f} (delta {delta_pct:+.1f}%) [{status}]"
        )
    return (len(short) == 0 and len(over) == 0, report, short, over)
```

- [ ] **Step 4: Update the three existing callers to unpack 4 values**

In `_extract_doc_complete`, three call sites currently unpack 3. Change each:

At `:1292`:
```python
        _ok, report, short, _over = _reconcile_schedules(items, subtotals, tolerance)
```
At `:1312`:
```python
            _ok, report, short, _over = _reconcile_schedules(items, subtotals, tolerance)
```
At `:1336`:
```python
            _ok, report, short, _over = _reconcile_schedules(items, subtotals, tolerance)
```
(These callers handle SHORT only; wiring OVER into extraction is Task 2. Unpacking into `_over` keeps them compiling now.)

- [ ] **Step 5: Run test to verify it passes**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py -v`
Expected: PASS (5 tests).

- [ ] **Step 6: Run existing BOQ tests to confirm no regression**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/ -k "boq or costing" -q`
Expected: PASS (no import/unpack errors from the signature change).

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/boq_parser_service.py drpl-backend/tests/test_boq_reconciliation.py
git commit -m "feat(costing): detect OVER (integer-multiple) schedules in reconcile gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Deterministic collapse of integer-multiple duplicated rows

When a schedule is OVER by an integer multiple k, the extractor emitted each logical row k times (merged-cell repetition). Collapse it deterministically: within the over-counted schedule, group rows by content identity and keep `count // k` of each group (never fewer than 1), so the schedule's row set and sum divide back down to the printed total.

**Files:**
- Modify: `app/services/boq_parser_service.py` (add `_collapse_over_schedules` directly after `_reconcile_schedules`, i.e. after the updated function that now ends near `:1245`; wire it into `_extract_doc_complete` before the final `_dedup_boq_rows` at `:1338`)
- Test: `tests/test_boq_reconciliation.py` (extend)

**Interfaces:**
- Consumes: `_reconcile_schedules` 4-tuple (Task 1); `_boq_row_key` (`:798`).
- Produces: `_collapse_over_schedules(items: list[dict], over: dict[str, int]) -> tuple[list[dict], int]` — returns `(collapsed_items, dropped_count)`. Rows in schedules not in `over` pass through untouched and keep their original relative order.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_boq_reconciliation.py`:

```python
from app.services.boq_parser_service import _collapse_over_schedules


def test_collapse_2x_halves_the_rows():
    # Schedule D has each of 2 distinct rows duplicated 2x (4 rows), over={D:2}.
    items = [
        {"schedule_name": "D", "sr_no": 1, "item_code": "D1", "basic_value": 100.0},
        {"schedule_name": "D", "sr_no": 2, "item_code": "D2", "basic_value": 200.0},
        {"schedule_name": "D", "sr_no": 1, "item_code": "D1", "basic_value": 100.0},
        {"schedule_name": "D", "sr_no": 2, "item_code": "D2", "basic_value": 200.0},
    ]
    collapsed, dropped = _collapse_over_schedules(items, {"D": 2})
    assert dropped == 2
    # one of each distinct content key survives
    assert sorted(r["item_code"] for r in collapsed) == ["D1", "D2"]


def test_collapse_leaves_non_over_schedules_untouched():
    items = [
        {"schedule_name": "A", "sr_no": 1, "item_code": "A1", "basic_value": 10.0},
        {"schedule_name": "A", "sr_no": 2, "item_code": "A2", "basic_value": 20.0},
    ]
    collapsed, dropped = _collapse_over_schedules(items, {"D": 2})
    assert dropped == 0
    assert collapsed == items


def test_collapse_never_drops_below_one_per_group():
    # A single distinct row seen 3x with over multiple 3 -> keep exactly 1.
    items = [{"schedule_name": "D", "sr_no": 1, "item_code": "D1",
              "basic_value": 5.0} for _ in range(3)]
    collapsed, dropped = _collapse_over_schedules(items, {"D": 3})
    assert dropped == 2
    assert len(collapsed) == 1


def test_collapse_preserves_order_of_survivors():
    items = [
        {"schedule_name": "D", "sr_no": 1, "item_code": "D1", "basic_value": 1.0},
        {"schedule_name": "D", "sr_no": 2, "item_code": "D2", "basic_value": 2.0},
        {"schedule_name": "D", "sr_no": 1, "item_code": "D1", "basic_value": 1.0},
        {"schedule_name": "D", "sr_no": 2, "item_code": "D2", "basic_value": 2.0},
    ]
    collapsed, dropped = _collapse_over_schedules(items, {"D": 2})
    assert [r["item_code"] for r in collapsed] == ["D1", "D2"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py -k collapse -v`
Expected: FAIL — `ImportError: cannot import name '_collapse_over_schedules'`.

- [ ] **Step 3: Write minimal implementation**

Insert directly after `_reconcile_schedules` in `app/services/boq_parser_service.py`:

```python
def _collapse_over_schedules(
    items: list[dict], over: dict[str, int]
) -> tuple[list[dict], int]:
    """Collapse integer-multiple duplication in over-counted schedules.

    For each schedule in `over` with multiple k, the extractor emitted each
    logical row k times (merged-cell repetition). Within that schedule we group
    rows by content identity (`_boq_row_key`) and keep ceil(count / k) of each
    group — at least 1 — dropping the surplus copies. Rows in schedules not in
    `over` pass through untouched, preserving original order. Deterministic; no
    LLM. Returns (collapsed_items, dropped_count).
    """
    if not over:
        return items, 0
    over_up = {c.upper(): k for c, k in over.items() if k >= 2}
    # kept_per_key[key] = how many copies of this content key we've already kept
    kept_per_key: dict[tuple, int] = {}
    seen_total: dict[tuple, int] = {}
    # First count total copies per key within over-schedules so we know the cap.
    for it in items:
        code = (it.get("schedule_name") or "").upper()
        if code in over_up:
            k = _boq_row_key(it)
            seen_total[k] = seen_total.get(k, 0) + 1
    out: list[dict] = []
    dropped = 0
    for it in items:
        code = (it.get("schedule_name") or "").upper()
        if code not in over_up:
            out.append(it)
            continue
        key = _boq_row_key(it)
        mult = over_up[code]
        total = seen_total.get(key, 1)
        cap = max(1, -(-total // mult))  # ceil(total / mult)
        if kept_per_key.get(key, 0) < cap:
            kept_per_key[key] = kept_per_key.get(key, 0) + 1
            out.append(it)
        else:
            dropped += 1
    return out, dropped
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py -k collapse -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire collapse into extraction, after SHORT handling, before final dedup**

In `_extract_doc_complete`, the block that currently ends the reconcile section runs SHORT re-extraction then `items = _dedup_boq_rows(items)` at `:1338`. Insert an OVER collapse immediately BEFORE that line. Replace the reconcile call at `:1292` and the final flag block so OVER is computed and applied:

At `:1292` change to capture `over`:
```python
        _ok, report, short, over = _reconcile_schedules(items, subtotals, tolerance)
```

Then immediately BEFORE `items = _dedup_boq_rows(items)` at `:1338`, insert:
```python
        # OVER handling: integer-multiple duplication from merged NIT cells.
        # Deterministic collapse (no re-extraction — re-extracting can't fix a
        # duplication). Recompute reconciliation afterward so `over`/`short`
        # reflect the collapsed set for the caller's low-confidence flagging.
        if subtotals and over:
            items, n_dropped = _collapse_over_schedules(items, over)
            logger.info(
                f"[boq_parser] collapse OVER for {os.path.basename(file_path)}: "
                f"dropped {n_dropped} duplicate row(s) in {sorted(over)}"
            )
            _ok, report, short, over = _reconcile_schedules(items, subtotals, tolerance)
```

(Note: the SHORT `while` loop at `:1299` and vision fallback at `:1320` already reassign `short` from the 3→4-tuple updated in Task 1 Step 4; leave those unpacking into `_over` — OVER is handled by this new block, which runs once after them.)

- [ ] **Step 6: Run the full reconciliation suite + BOQ regression**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py tests/ -k "boq or costing" -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/boq_parser_service.py drpl-backend/tests/test_boq_reconciliation.py
git commit -m "fix(costing): deterministically collapse integer-multiple duplicated BOQ rows

Merged NIT cells make the extractor emit each row k times (2x-21x observed in
prod). When reconcile flags a schedule OVER by integer k, collapse it back to
its printed total instead of shipping inflated rows/totals.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Persist per-schedule `reconciliation_status` on `BOQScheduleTotal`

So Piece 2 (and the UI/agent) can see which schedules reconciled. Add a nullable `reconciliation_status` column (`reconciled | failed | no_anchor`) and set it when persisting schedule totals.

**Files:**
- Modify: `app/models/costing_template.py:100-110` (`BOQScheduleTotal`)
- Modify: `app/main.py` — `_apply_schema_drift_fixes()` (`:161`, Postgres) + `_add_missing_columns()` (`:571`, SQLite)
- Create: `alembic/versions/<rev>_boq_schedule_reconciliation_status.py`
- Modify: `app/services/boq_parser_service.py` — `_persist_schedule_totals` (`:1356`) accepts a `statuses` map; `parse_boq_from_tender` (`:330-331`) computes and passes it
- Test: `tests/test_boq_reconciliation.py` (extend — pure status-derivation helper)

**Interfaces:**
- Consumes: `_reconcile_schedules` 4-tuple (Task 1).
- Produces:
  - `BOQScheduleTotal.reconciliation_status: Optional[str]`
  - `_derive_schedule_statuses(subtotals, short, over) -> dict[str, str]` — maps every `schedule_code` in `subtotals` to `"reconciled" | "failed"`, and codes absent from `subtotals` are simply not included (caller defaults them to `"no_anchor"`). Consumed by `_persist_schedule_totals`.
  - `_persist_schedule_totals(db, tender_id, schedule_totals, statuses=None)` — new optional 4th arg.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_boq_reconciliation.py`:

```python
from app.services.boq_parser_service import _derive_schedule_statuses


def test_status_reconciled_when_not_short_or_over():
    statuses = _derive_schedule_statuses({"A": 100.0, "B": 200.0}, set(), {})
    assert statuses == {"A": "reconciled", "B": "reconciled"}


def test_status_failed_when_short_or_over():
    statuses = _derive_schedule_statuses(
        {"A": 100.0, "B": 200.0, "C": 300.0}, short={"B"}, over={"C": 2})
    assert statuses["A"] == "reconciled"
    assert statuses["B"] == "failed"
    assert statuses["C"] == "failed"


def test_status_omits_codes_without_subtotal():
    # A schedule with no printed subtotal isn't in `subtotals` -> not returned;
    # caller treats absence as no_anchor.
    statuses = _derive_schedule_statuses({"A": 100.0}, set(), {})
    assert "Z" not in statuses
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py -k status -v`
Expected: FAIL — `ImportError: cannot import name '_derive_schedule_statuses'`.

- [ ] **Step 3: Add the model column**

In `app/models/costing_template.py`, after `advertised_value` (`:110`) add:
```python
    reconciliation_status = Column(String(16), nullable=True)  # reconciled | failed | no_anchor
```

- [ ] **Step 4: Add the status-derivation helper + persist plumbing**

In `app/services/boq_parser_service.py`, add directly after `_collapse_over_schedules`:
```python
def _derive_schedule_statuses(
    subtotals: dict[str, float], short: set, over: dict[str, int]
) -> dict[str, str]:
    """Map each schedule that HAS a printed subtotal to reconciled|failed.
    A schedule is 'failed' if it is SHORT or OVER; else 'reconciled'. Schedules
    with no printed subtotal are omitted (caller defaults them to 'no_anchor')."""
    short_up = {c.upper() for c in short}
    over_up = {c.upper() for c in over}
    out: dict[str, str] = {}
    for code in subtotals:
        cu = code.upper()
        out[code] = "failed" if (cu in short_up or cu in over_up) else "reconciled"
    return out
```

Change `_persist_schedule_totals` signature (`:1356-1358`) and the row build (`:1380-1388`):
```python
def _persist_schedule_totals(
    db: Session, tender_id: int, schedule_totals: list[dict],
    statuses: Optional[dict[str, str]] = None,
) -> None:
```
and inside the insert loop, set the status (default `no_anchor` when we have a total but no computed status):
```python
    for code, s in by_code.items():
        db.add(
            BOQScheduleTotal(
                tender_id=tender_id,
                schedule_code=code,
                stated_total=s.get("stated_total"),
                advertised_value=s.get("advertised_value"),
                reconciliation_status=(
                    (statuses or {}).get(code) or "no_anchor"
                ),
            )
        )
```

- [ ] **Step 5: Compute + pass statuses in `parse_boq_from_tender`**

`_extract_doc_complete` currently returns `(items, short)` at `:1353`. Extend it to also return the reconciliation inputs so the caller can derive status.

First, initialize `subtotals` and `over` so they always exist on every return path (the early returns at `:1263` and `:1268` return before `subtotals`/`over` are assigned). Immediately after `items = list(pdfplumber_items)` at `:1261`, add:
```python
    subtotals: dict[str, float] = {}
    over: dict[str, int] = {}
```
Change the two early returns to the 4-tuple shape: `:1263` `return items, set()` → `return items, set(), subtotals, over`; `:1268` `return items, set()` → `return items, set(), subtotals, over`.

Change the final `return items, short` (`:1353`) to:
```python
    return items, short, subtotals, over
```

Update the caller at `:250-254` (this is inside the `else` / AI-vision branch only — the deterministic IREPS branch at `:227-228` never produces OVER/SHORT because its rows are the tender's own verbatim numbers, so it needs no reconcile inputs):
```python
                    items, low_conf, doc_subtotals, doc_over = await _extract_doc_complete(
                        db, local_path, items,
                        force_ai=force_ai, legacy_incomplete=legacy_incomplete,
                    )
                    low_conf_all |= low_conf
                    # accumulate reconcile inputs across docs for status derivation
                    recon_subtotals.update(doc_subtotals)
                    recon_over.update(doc_over)
                    recon_short |= low_conf
```
Initialize `recon_subtotals: dict = {}`, `recon_over: dict = {}`, `recon_short: set = set()` alongside `low_conf_all` at `:193`. (`low_conf` here is the set of schedule codes that stayed short/over after all passes — the ones we mark failed. The deterministic branch contributes nothing to these three, which is correct: its schedule totals get status `reconciled` via `_derive_schedule_statuses` since they're absent from `recon_short`/`recon_over`.) Then at `:330-331` replace:
```python
    if schedule_totals_all:
        statuses = _derive_schedule_statuses(recon_subtotals, recon_short, recon_over)
        _persist_schedule_totals(db, tender_id, schedule_totals_all, statuses)
```

- [ ] **Step 6: Alembic revision**

Run: `cd drpl-backend && ./venv/Scripts/alembic.exe revision -m "boq_schedule_reconciliation_status"`
Edit the generated file's `upgrade()` / `downgrade()`:
```python
def upgrade():
    op.add_column("boq_schedule_totals",
                  sa.Column("reconciliation_status", sa.String(length=16), nullable=True))

def downgrade():
    op.drop_column("boq_schedule_totals", "reconciliation_status")
```

- [ ] **Step 7: Schema-drift self-heal entries**

In `app/main.py` `_apply_schema_drift_fixes()` (Postgres, near `:161`), add alongside the other `ADD COLUMN IF NOT EXISTS` statements:
```python
        "ALTER TABLE boq_schedule_totals ADD COLUMN IF NOT EXISTS reconciliation_status VARCHAR(16)",
```
In `_add_missing_columns()` (SQLite, near `:571`), follow the existing per-column pattern used there to add `boq_schedule_totals.reconciliation_status` (TEXT) if absent. (Match whatever idiom that function already uses — inspect a neighboring column add and copy it exactly.)

- [ ] **Step 8: Run tests**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py -v`
Expected: PASS (all status + over + reconcile + collapse tests).

Then broader: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/ -k "boq or costing" -q` → PASS.

- [ ] **Step 9: Commit**

```bash
git add drpl-backend/app/models/costing_template.py drpl-backend/app/main.py \
  drpl-backend/alembic/versions/ drpl-backend/app/services/boq_parser_service.py \
  drpl-backend/tests/test_boq_reconciliation.py
git commit -m "feat(costing): persist per-schedule reconciliation_status on BOQScheduleTotal

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Harden the extraction prompt against merged-row repetition

Reduce duplication at the source so the collapse pass is a safety net, not the primary fix. Add an explicit merged-cell rule and a self-check to `_BOQ_AI_SYSTEM_PROMPT`.

**Files:**
- Modify: `app/services/boq_parser_service.py:868-891` (`_BOQ_AI_SYSTEM_PROMPT`)
- Test: `tests/test_boq_reconciliation.py` (extend — assert the guardrail text is present; prompt content is not otherwise unit-testable without an LLM call)

**Interfaces:**
- Consumes: nothing. Produces: nothing new (string constant edit).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_boq_reconciliation.py`:

```python
from app.services import boq_parser_service as bps


def test_prompt_has_merged_cell_guardrail():
    p = bps._BOQ_AI_SYSTEM_PROMPT.lower()
    assert "merged" in p
    assert "once" in p  # "emit each logical item exactly once"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py -k merged_cell -v`
Expected: FAIL — assertion error (`merged` not in prompt).

- [ ] **Step 3: Add the guardrail to the prompt**

In `_BOQ_AI_SYSTEM_PROMPT`, insert a new paragraph immediately after the schedule-banner paragraph (after the line ending "…most recent schedule banner above it." at `:874`):

```
MERGED CELLS — emit each logical line item EXACTLY ONCE. NIT tables often merge the Item Code / Description cell across several unmerged sub-rows (sub-quantities, component breakdowns, or continuation lines). This is ONE line item, not many. Do NOT repeat the item once per grid row it visually spans. If the sub-rows carry their own quantities that belong to the same item, SUM them into that item's single `quantity`. Before returning, sanity-check: within a schedule, the number of objects you emit must equal the number of DISTINCT printed serial numbers — never a multiple of it.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_boq_reconciliation.py -k merged_cell -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/boq_parser_service.py drpl-backend/tests/test_boq_reconciliation.py
git commit -m "feat(costing): merged-cell guardrail in BOQ extraction prompt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: End-to-end verification against the live-DB cases

Prove the fix on the real data before declaring Piece 1 done. No new prod writes — re-parse into a scratch/staging path or assert via the pure pipeline on captured rows.

**Files:**
- Create: scratch verification note (not committed): confirm behavior.

- [ ] **Step 1: Re-run the read-only reconciliation probe**

Run the read-only `probe_nit_totals.py` (SELECT-only) to capture the CURRENT mismatched schedules (t=2693, t=2732, t=2733) as the baseline.

- [ ] **Step 2: Unit-simulate the full gate on the captured rows**

Write a throwaway script (in the job tmp dir, not committed) that loads the real BOQItem rows + BOQScheduleTotal for one mismatched tender via a **read-only** session, runs `_reconcile_schedules` then `_collapse_over_schedules` then `_reconcile_schedules` again, and asserts the post-collapse result is `reconciled` (or `failed` with a clear report) for each schedule. This exercises the deterministic pipeline on production data WITHOUT writing.

- [ ] **Step 3: Confirm the full suite is green**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/ -k "boq or costing" -q`
Expected: PASS.

- [ ] **Step 4: Record the verification result**

Note in the PR description: baseline mismatched schedules, and that the deterministic collapse brings them within tolerance (or flags them `failed` with the report). Only then is Piece 1 complete.

---

## Self-Review

**Spec coverage (Piece 1 scope only):**
- Merged-cell-aware prompt → Task 4. ✅
- Reliable schedule-total capture → already exists (`_extract_schedule_subtotals` + `_persist_schedule_totals`); Piece 1 adds status, Task 3. ✅
- Deterministic reconciliation gate → `_reconcile_schedules` extended for OVER, Task 1. ✅
- Detect integer-multiple duplication fingerprint → Task 1 (`over` map). ✅
- Handle the duplication (collapse, since re-extraction can't fix OVER) → Task 2. ✅
- One re-extraction on SHORT (reconcile-first, escalate once) → already exists (`max_passes` loop), unchanged. ✅
- Block + flag on unresolved → `reconciliation_status="failed"` (Task 3) + existing `extraction_confidence="low"`. ✅
- Fail-safe gate (never fail-open) → collapse/reconcile are pure; empty `over`/`subtotals` → no-op. ✅
- Config tunable via settings-service (`costing.boq_reconcile_tolerance_pct` already exists at `:1272`). ✅
- Verification on live-DB cases → Task 5. ✅

**Placeholder scan:** Task 3 Step 7 SQLite entry says "match the existing idiom" rather than pasting exact code — acceptable because `_add_missing_columns` uses a per-file idiom the implementer must read to match; the Postgres line IS given verbatim. All other steps carry real code/commands.

**Type consistency:** `_reconcile_schedules` returns a 4-tuple `(bool, list[str], set, dict[str,int])` — defined in Task 1, consumed with that exact shape in Tasks 2, 3. `_collapse_over_schedules(list, dict) -> (list, int)` consistent Task 2↔wiring. `_derive_schedule_statuses(dict, set, dict) -> dict[str,str]` consistent Task 3. `_persist_schedule_totals(..., statuses=None)` optional arg is back-compatible with any other caller.

**Out of scope (Pieces 2 & 3):** surfacing the ✓/⚠ badge in the Excel/agent (Piece 2) and OEM/web-link columns (Piece 3) are separate plans, as designed.
