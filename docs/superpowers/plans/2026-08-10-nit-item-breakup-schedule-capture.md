# NIT "Item Breakup" Schedule Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop IREPS NITs from silently losing whole schedules, by (a) not letting the summary row's phrase "Item Breakup" truncate the document, and (b) making the reconciliation gate refuse to call a schedule `ok` when its only evidence is a summary placeholder row.

**Architecture:** Three surgical changes inside `drpl-backend/app/services/boq_parser_service.py`, each independently testable and each with its own regression test built from tender 3822's real text. First we make the section-boundary regex match only a genuine numbered section heading (so `"Please see Item Breakup for details."` stops truncating page 1). Second we teach the reconciliation gate to ignore placeholder rows so a detail-less schedule reports SHORT and triggers the existing re-extraction/vision passes. Third we drop the placeholder rows before persistence so they never reach costing as fake ₹0-quantity line items. No schema change, no new service, no config flag.

**Tech Stack:** Python 3.14, pytest, SQLAlchemy, PyMuPDF (`fitz`), pdfplumber.

## Global Constraints

- All edits are confined to `drpl-backend/`. No frontend, extension, or Alembic changes.
- Run every command from `drpl-backend/` using the repo venv: `./venv/Scripts/python.exe -m pytest ...` (Windows/Git Bash).
- Never run the parser against the live DB. `tests/conftest.py` forces a throwaway SQLite DB; do not bypass it. Local `.env` `DATABASE_URL` is the **live Neon prod DB**.
- These functions are pure/deterministic — no LLM, no DB, no network in any test in this plan. Do not add a test that calls an LLM.
- Preserve existing behaviour for the Parel fixture (`tests/fixtures/nit_parel_2531.pdf`); `tests/services/costing/test_nit_reconciliation.py::test_parel_fixture_reconciles_exactly` must stay green.
- Follow the existing test-module style: a module docstring naming the real tender/session the regression came from, then plain `def test_*` functions with `assert`.

## Background: the exact failure (verified against the live documents)

Command Center session 290 → tender 3822 (`BE-TLAC-RMPU-CMC-2026-28`, SWR Bangalore RMPU).

An IREPS NIT has two relevant sections:

- `2. SCHEDULE` — a **summary**. One row per schedule. Its description is literally `Please see Item Breakup for details.` and it carries the schedule's total.
- `3. ITEM BREAKUP` — the **real line items**, which is what costing needs.

Three compounding failures, each independently verified:

1. `_SCHEDULE_END_RE` matches the bare words `Item Breakup`. In tender 3822's NIT that phrase occurs **5 times inside summary rows** (lines 87/107/127/146/178 of the extracted text) before the one true heading `3. ITEM BREAKUP` (line 183).
2. `_load_pdf_pages` truncates at the first match and `break`s. Measured: the 12-page NIT collapses to a fragment of page 1 (1794 of 3316 chars), keeping only Schedule A's banner. **Pages 2–12 — the whole Item Breakup section — are discarded.**
3. The summary row's value exactly equals the schedule's printed subtotal, so `_reconcile_schedules` computes `delta +0.0%` and reports `[ok]`. **A schedule with zero real line items reads as fully reconciled.** Measured with only the four summary rows present: the gate flags only `E` as SHORT and passes `A`/`B`/`C`/`D`.

Net effect in production: `boq_items` for tender 3822 holds a Schedule D header row worth ₹22,245,831.95 and **zero** Schedule D detail rows, while every reconciliation signal reads green. The user saw "Schedule D — Tender Value ₹0.00" in two costing runs.

Note the semantic inversion in (1): `ITEM BREAKUP` is not the end of the interesting region, it is the **start** of it. Line 184 of the extracted text — immediately after the heading — is the first real schedule banner.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `drpl-backend/app/services/boq_parser_service.py` | NIT schedule extraction, reconciliation, persistence | Modify: `_SCHEDULE_END_RE` (~line 129), new `_is_summary_placeholder_row`, `_reconcile_schedules` (~line 1267), `parse_boq_from_tender` (~line 322) |
| `drpl-backend/tests/services/costing/test_nit_item_breakup_boundary.py` | Regression: the section boundary must not fire on a summary row | Create |
| `drpl-backend/tests/services/costing/test_summary_row_reconciliation.py` | Regression: a placeholder-only schedule must reconcile SHORT, not ok | Create |

Three tasks, one per behaviour. Task 1 restores the lost pages; Task 2 restores the safety net; Task 3 stops the placeholder rows reaching costing. Task 2 is the most valuable — it is what converts a silent wrong number into a visible one — and it is deliberately independent of Task 1 so it still protects us on NIT layouts Task 1 does not anticipate.

---

### Task 1: Stop the summary row from truncating the document

The section-end regex must match a genuine numbered section heading, not the phrase embedded in a sentence. `ITEM BREAKUP` is removed as a terminator entirely: it *starts* the region we want to keep.

**Files:**
- Modify: `drpl-backend/app/services/boq_parser_service.py:129-135`
- Test: `drpl-backend/tests/services/costing/test_nit_item_breakup_boundary.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_SCHEDULE_END_RE` — a compiled `re.Pattern` with unchanged name and usage. Callers (`_load_pdf_pages`, and the page-scan in Task 2's test) keep using `.search(text)` and reading `.start()`. No signature change.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/services/costing/test_nit_item_breakup_boundary.py`:

```python
"""The "Schedule of Items" boundary must not fire on a summary row.

Regression: Command Center session 290 (tender 3822, BE-TLAC-RMPU-CMC-2026-28).
An IREPS NIT's section `2. SCHEDULE` is a SUMMARY: one row per schedule whose
description is literally "Please see Item Breakup for details." carrying the
schedule total. The real line items live in section `3. ITEM BREAKUP`.

`_SCHEDULE_END_RE` matched the bare words "Item Breakup", so `_load_pdf_pages`
truncated the NIT at the FIRST summary row on page 1 and broke out of the loop,
discarding pages 2-12 -- the entire Item Breakup section. Schedule D's ~19
priced line items were never extracted; the user reported the gap on 2026-08-07.

"ITEM BREAKUP" is not a terminator at all: it is the START of the region we
want. Only a genuine numbered/anchored section heading may end the schedule.
"""
from app.services.boq_parser_service import _SCHEDULE_END_RE


# Verbatim from tender 3822's NIT (occurs 5x before the real heading).
SUMMARY_ROW = "Please see Item Breakup for details."


def test_summary_row_does_not_end_the_schedule_section():
    assert _SCHEDULE_END_RE.search(SUMMARY_ROW) is None


def test_item_breakup_heading_does_not_end_the_schedule_section():
    # It STARTS the line-item region -- keeping it would drop every real row.
    assert _SCHEDULE_END_RE.search("3. ITEM BREAKUP") is None


def test_real_section_headings_still_end_the_schedule_section():
    for heading in [
        "4. ELIGIBILITY CONDITIONS",
        "5. COMPLIANCE",
        "Special Financial Criteria",
        "Special Technical Criteria",
        "General Instructions",
        "Undertakings",
        "Documents attached with tender",
    ]:
        assert _SCHEDULE_END_RE.search(heading) is not None, heading


def test_prose_mentioning_a_heading_word_does_not_end_the_section():
    # A work item that merely mentions compliance is not a section boundary.
    assert _SCHEDULE_END_RE.search(
        "Supply of RDSO compliance-tested thermostat for Non-LHB coaches"
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/services/costing/test_nit_item_breakup_boundary.py -v`

Expected: FAIL. `test_summary_row_does_not_end_the_schedule_section` and `test_item_breakup_heading_does_not_end_the_schedule_section` both fail because the current regex returns a match (`'Item Breakup'` and `'3. ITEM BREAKUP'`). `test_prose_mentioning_a_heading_word_does_not_end_the_section` also fails — the current alternative `COMPLIANCE\b` matches mid-sentence.

- [ ] **Step 3: Write minimal implementation**

Replace the `_SCHEDULE_END_RE` block at `boq_parser_service.py:129-135` with:

```python
# Section headers that terminate the "Schedule of Items" in an IREPS NIT.
# Everything AFTER the first of these (once the schedule has begun) is
# eligibility / compliance / undertakings / annexures / document lists.
#
# Each alternative is ANCHORED at the start of a line (`^`, MULTILINE) and,
# where the NIT numbers it, requires that number. Two rules learned from
# tender 3822 (session 290):
#
#   1. "ITEM BREAKUP" is NOT a terminator. IREPS section `2. SCHEDULE` is a
#      summary whose every row reads "Please see Item Breakup for details.";
#      matching that phrase truncated the NIT at page 1 and threw away the
#      real line items. Section `3. ITEM BREAKUP` *starts* the region we want.
#   2. A bare unanchored keyword (the old `COMPLIANCE\b`) matches inside work
#      descriptions. Anchoring keeps prose from ending the schedule.
_SCHEDULE_END_RE = re.compile(
    r"^[ \t]*(?:\d+\.[ \t]*)?(?:"
    r"ELIGIBILITY\s+CONDITIONS?|"
    r"Special\s+(?:Financial|Technical)\s+Criteria|"
    r"Bidders\s+shall\s+confirm|"
    r"COMPLIANCE|"
    r"General\s+Instructions?|"
    r"Undertakings?|"
    r"Documents\s+attached\s+with\s+tender"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/Scripts/python.exe -m pytest tests/services/costing/test_nit_item_breakup_boundary.py -v`

Expected: PASS (4 passed).

- [ ] **Step 5: Verify no regression in the existing costing suite**

Run: `./venv/Scripts/python.exe -m pytest tests/services/costing/ -v`

Expected: PASS. Pay particular attention to `test_nit_reconciliation.py::test_parel_fixture_reconciles_exactly` and `test_boq_parser_integration.py` — the Parel NIT must still trim at its real eligibility heading. If a test that asserted the old truncation behaviour now fails, read it carefully: the old behaviour was the bug, so update that assertion and note why in the commit message. Do not weaken the new regex to satisfy it.

- [ ] **Step 6: Commit**

```bash
git add tests/services/costing/test_nit_item_breakup_boundary.py app/services/boq_parser_service.py
git commit -m "fix(boq): a summary row's 'Item Breakup' no longer truncates the NIT

IREPS section '2. SCHEDULE' is a summary whose every row reads 'Please see
Item Breakup for details.'. _SCHEDULE_END_RE matched that phrase, so
_load_pdf_pages truncated tender 3822's NIT at page 1 and discarded pages
2-12 -- the entire Item Breakup section, including all of Schedule D.

Anchor every terminator at line start and drop ITEM BREAKUP as a terminator
entirely: it starts the line-item region rather than ending it."
```

---

### Task 2: A placeholder-only schedule must reconcile SHORT

This is the load-bearing fix. Even with Task 1, an extraction can miss a schedule; the gate must notice. Today a summary row whose value equals the printed subtotal makes the gate report `+0.0% [ok]` with zero real rows captured.

**Files:**
- Modify: `drpl-backend/app/services/boq_parser_service.py` — add `_is_summary_placeholder_row` near `_detect_tax_line` (~line 838); modify `_reconcile_schedules` (~line 1267-1345)
- Test: `drpl-backend/tests/services/costing/test_summary_row_reconciliation.py`

**Interfaces:**
- Consumes: Task 1's `_SCHEDULE_END_RE` (unchanged usage; no dependency in this task's code).
- Produces:
  - `_is_summary_placeholder_row(item: dict) -> bool` — True when a row is an IREPS section-2 summary placeholder (a "see the breakup" description, and no quantity and no rate). Pure; no DB.
  - `_reconcile_schedules(items, subtotals, tolerance_pct)` — **unchanged signature and unchanged 5-tuple return** `(all_ok, report_lines, short_codes, over_multiples, dup_over_codes)`. Only the internal counting changes: placeholder rows no longer contribute to a schedule's row count or value sum.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/services/costing/test_summary_row_reconciliation.py`:

```python
"""A schedule evidenced only by a summary placeholder row is NOT reconciled.

Regression: Command Center session 290 (tender 3822). IREPS section
`2. SCHEDULE` prints one summary row per schedule -- description "Please see
Item Breakup for details.", no qty, no rate, value == the schedule's printed
subtotal. Because that value matches the subtotal EXACTLY, _reconcile_schedules
reported `delta +0.0% [ok]` for Schedule D while ZERO real line items had been
captured. The gate that exists to catch exactly this was neutralised by the
placeholder, so the miss reached the user as "Schedule D -- Tender Value 0.00".

A placeholder carries no scope and must not count as evidence of extraction.
"""
from app.services.boq_parser_service import (
    _is_summary_placeholder_row,
    _reconcile_schedules,
)


# Tender 3822's real printed subtotals.
SUBTOTALS = {
    "A": 180458734.37,
    "B": 48124014.98,
    "C": 134516700.80,
    "D": 22245831.95,
}


def _placeholder(schedule, value):
    """The section-2 summary row, exactly as extracted from tender 3822."""
    return {
        "schedule_name": schedule,
        "sr_no": 1,
        "item_code": schedule,
        "description": "Please see Item Breakup for details.",
        "quantity": None,
        "unit_rate": None,
        "basic_value": value,
    }


def _real_row(schedule, sr_no, qty, rate):
    return {
        "schedule_name": schedule,
        "sr_no": sr_no,
        "item_code": f"{schedule}{sr_no}",
        "description": f"Supply and replacement of component {sr_no}",
        "quantity": qty,
        "unit_rate": rate,
        "basic_value": qty * rate,
    }


def test_placeholder_row_is_detected():
    assert _is_summary_placeholder_row(_placeholder("D", 22245831.95)) is True


def test_real_priced_row_is_not_a_placeholder():
    assert _is_summary_placeholder_row(_real_row("D", 1, 19.0, 8015.29)) is False


def test_row_mentioning_breakup_but_carrying_a_price_is_not_a_placeholder():
    # A genuine work item is never discarded just for mentioning the breakup.
    row = _real_row("D", 2, 31.0, 2657.45)
    row["description"] = "Vane relay replacement (see Item Breakup for details)"
    assert _is_summary_placeholder_row(row) is False


def test_schedule_with_only_a_placeholder_is_short():
    """The exact tender 3822 failure: value matches the subtotal perfectly."""
    items = [_placeholder(code, SUBTOTALS[code]) for code in SUBTOTALS]
    ok, report, short, over, dup_over = _reconcile_schedules(items, SUBTOTALS, 2.0)

    assert ok is False
    assert short == {"A", "B", "C", "D"}, (
        "every placeholder-only schedule must be SHORT, not ok"
    )
    assert over == {}
    assert dup_over == set()


def test_placeholder_does_not_inflate_a_schedule_with_real_rows():
    """D's placeholder must not top up D's real rows toward the subtotal."""
    items = [
        _placeholder("D", 22245831.95),
        _real_row("D", 1, 19.0, 8015.29),   # 152,290.51
        _real_row("D", 2, 31.0, 2657.45),   # 82,380.95
    ]
    ok, report, short, over, dup_over = _reconcile_schedules(
        items, {"D": 22245831.95}, 2.0
    )

    # Real rows sum to ~234k against a printed 22.2M -- genuinely short.
    assert ok is False
    assert short == {"D"}
    assert "2 row(s)" in report[0], f"placeholder must not be counted: {report[0]}"


def test_fully_extracted_schedule_still_reconciles_ok():
    """The fix must not create false SHORT flags on a good extraction."""
    items = [
        _placeholder("D", 1000.0),
        _real_row("D", 1, 10.0, 60.0),   # 600
        _real_row("D", 2, 10.0, 40.0),   # 400
    ]
    ok, report, short, over, dup_over = _reconcile_schedules(items, {"D": 1000.0}, 2.0)

    assert ok is True
    assert short == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/services/costing/test_summary_row_reconciliation.py -v`

Expected: FAIL with `ImportError: cannot import name '_is_summary_placeholder_row'` — the function does not exist yet.

- [ ] **Step 3: Write minimal implementation — the detector**

Add to `boq_parser_service.py` immediately after `_detect_tax_line` (~line 838):

```python
# IREPS section `2. SCHEDULE` is a SUMMARY table: one row per schedule whose
# description is a pointer to the real breakup ("Please see Item Breakup for
# details.") and whose value is the schedule's total. It carries NO scope.
#
# Counting it as an extracted line is what let tender 3822's Schedule D pass
# reconciliation at +0.0% with zero real rows captured (session 290): the
# placeholder's value equals the printed subtotal by construction, so it always
# reconciles perfectly no matter how badly extraction did.
_SUMMARY_PLACEHOLDER_RE = re.compile(
    r"(?:please\s+)?(?:see|refer\s+to)\s+(?:the\s+)?item\s*break\s*-?\s*up",
    re.IGNORECASE,
)


def _is_summary_placeholder_row(item: dict) -> bool:
    """True for an IREPS section-2 summary row (a pointer to the Item Breakup).

    Requires BOTH the pointer description AND the absence of any per-unit
    pricing: a real work item that merely cites the breakup keeps its qty and
    rate, so it is never discarded here.
    """
    if not _SUMMARY_PLACEHOLDER_RE.search(item.get("description") or ""):
        return False

    def _positive(value) -> bool:
        try:
            return value is not None and float(value) > 0
        except (TypeError, ValueError):
            return False

    has_qty = _positive(item.get("quantity"))
    has_rate = _positive(item.get("unit_rate")) or _positive(item.get("estimated_rate"))
    return not (has_qty or has_rate)
```

- [ ] **Step 4: Write minimal implementation — exclude placeholders from the gate**

In `_reconcile_schedules`, the accumulation loop currently starts (~line 1288):

```python
    for it in items:
        code = (it.get("schedule_name") or "").upper()
        if not code or it.get("is_tax_line"):
            continue
```

Change that guard to also skip placeholders:

```python
    for it in items:
        code = (it.get("schedule_name") or "").upper()
        # Summary placeholders are excluded from BOTH the row count and the
        # value sum. Their value equals the printed subtotal by construction,
        # so counting them makes a zero-detail schedule reconcile at +0.0%
        # (tender 3822 / session 290) and hides the miss.
        if not code or it.get("is_tax_line") or _is_summary_placeholder_row(it):
            continue
```

No other change is needed: `is_short` already treats `n == 0` as SHORT, so a schedule left with no counted rows now flags correctly and feeds the existing targeted re-extraction and vision-reconcile passes.

- [ ] **Step 5: Run test to verify it passes**

Run: `./venv/Scripts/python.exe -m pytest tests/services/costing/test_summary_row_reconciliation.py -v`

Expected: PASS (6 passed).

- [ ] **Step 6: Verify no regression in the existing costing suite**

Run: `./venv/Scripts/python.exe -m pytest tests/services/costing/ -v`

Expected: PASS, including `test_nit_reconciliation.py` and `test_reconciliation_flow.py`.

- [ ] **Step 7: Commit**

```bash
git add tests/services/costing/test_summary_row_reconciliation.py app/services/boq_parser_service.py
git commit -m "fix(boq): a summary-only schedule reconciles SHORT, not ok

An IREPS section-2 summary row carries the schedule total and no scope, so
its value matches the printed subtotal by construction. Counting it let
tender 3822's Schedule D report delta +0.0% [ok] with ZERO real line items
captured -- the gate that exists to catch this was neutralised by the
placeholder, and the miss reached the user as 'Schedule D -- 0.00'.

Exclude placeholders from both the row count and the value sum so the
schedule flags SHORT and the existing re-extraction/vision passes fire."
```

---

### Task 3: Keep placeholder rows out of the persisted schedule

With Tasks 1–2 the real rows are captured and a miss is visible. The placeholders themselves must still not reach costing: persisted as BOQ items they become phantom line items with a large value, no quantity and no rate — which is exactly what produced the "Schedule D — Tender Value ₹0.00" row the user saw.

**Files:**
- Modify: `drpl-backend/app/services/boq_parser_service.py:322` (in `parse_boq_from_tender`, immediately after `_dedup_boq_rows`)
- Test: `drpl-backend/tests/services/costing/test_summary_row_reconciliation.py` (extend the file from Task 2)

**Interfaces:**
- Consumes: `_is_summary_placeholder_row(item: dict) -> bool` from Task 2.
- Produces: `_drop_summary_placeholder_rows(items: list[dict]) -> tuple[list[dict], int]` — returns `(survivors, dropped_count)`, preserving input order. Pure; no DB.

- [ ] **Step 1: Write the failing test**

Append to `drpl-backend/tests/services/costing/test_summary_row_reconciliation.py`:

```python
from app.services.boq_parser_service import _drop_summary_placeholder_rows


def test_placeholders_are_dropped_before_persistence():
    items = [
        _placeholder("D", 22245831.95),
        _real_row("D", 1, 19.0, 8015.29),
        _real_row("D", 2, 31.0, 2657.45),
    ]
    survivors, dropped = _drop_summary_placeholder_rows(items)

    assert dropped == 1
    assert len(survivors) == 2
    assert all(
        "Item Breakup" not in (s["description"] or "") for s in survivors
    )


def test_dropping_placeholders_preserves_order_and_keeps_real_rows():
    items = [
        _real_row("D", 1, 19.0, 8015.29),
        _placeholder("D", 22245831.95),
        _real_row("D", 2, 31.0, 2657.45),
    ]
    survivors, dropped = _drop_summary_placeholder_rows(items)

    assert dropped == 1
    assert [s["sr_no"] for s in survivors] == [1, 2]


def test_dropping_is_a_no_op_when_there_are_no_placeholders():
    items = [_real_row("D", 1, 19.0, 8015.29)]
    survivors, dropped = _drop_summary_placeholder_rows(items)

    assert dropped == 0
    assert survivors == items
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/Scripts/python.exe -m pytest tests/services/costing/test_summary_row_reconciliation.py -v`

Expected: FAIL with `ImportError: cannot import name '_drop_summary_placeholder_rows'`.

- [ ] **Step 3: Write minimal implementation**

Add to `boq_parser_service.py` immediately after `_is_summary_placeholder_row` (from Task 2):

```python
def _drop_summary_placeholder_rows(items: list[dict]) -> tuple[list[dict], int]:
    """Remove IREPS section-2 summary rows before persistence.

    A placeholder persisted as a BOQItem becomes a phantom line item carrying
    the whole schedule's value with no quantity and no rate -- which is what
    rendered as "Schedule D -- Tender Value 0.00" in session 290. Returns
    (survivors, dropped_count), preserving order.
    """
    survivors = [it for it in items if not _is_summary_placeholder_row(it)]
    return survivors, len(items) - len(survivors)
```

- [ ] **Step 4: Wire it into the persistence path**

In `parse_boq_from_tender`, find this line (~322):

```python
    parsed_items = _dedup_boq_rows(parsed_items)
```

Insert immediately after it, **before** the twin-collapse call:

```python
    # Drop IREPS section-2 summary placeholders at the universal choke point.
    # Every extraction path converges here, so this is the one place that
    # guarantees a "Please see Item Breakup for details." row never reaches
    # costing as a phantom line item (session 290 / tender 3822).
    parsed_items, _placeholders_dropped = _drop_summary_placeholder_rows(parsed_items)
    if _placeholders_dropped:
        logger.info(
            f"[boq_parser] tender {tender_id}: dropped "
            f"{_placeholders_dropped} section-2 summary placeholder row(s)"
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `./venv/Scripts/python.exe -m pytest tests/services/costing/test_summary_row_reconciliation.py -v`

Expected: PASS (9 passed — 6 from Task 2 plus 3 new).

- [ ] **Step 6: Run the full backend suite**

Run: `./venv/Scripts/python.exe -m pytest tests/ -q`

Expected: PASS, or the same set of failures that exist on `main` before this branch. Confirm the baseline first with `git stash && ./venv/Scripts/python.exe -m pytest tests/ -q && git stash pop` if anything looks pre-existing — do not attribute an unrelated pre-existing failure to this change, and do not fix unrelated failures in this branch.

- [ ] **Step 7: Commit**

```bash
git add tests/services/costing/test_summary_row_reconciliation.py app/services/boq_parser_service.py
git commit -m "fix(boq): drop section-2 summary placeholders before persistence

Persisted, a 'Please see Item Breakup for details.' row becomes a phantom
BOQ line carrying the whole schedule's value with no qty and no rate -- the
'Schedule D -- Tender Value 0.00' row from session 290. Drop them at the
universal choke point after dedup, where every extraction path converges."
```

---

### Task 4: Verify the fix against tender 3822's real documents

The three unit-tested changes must actually recover Schedule D from the real PDFs. This task is verification only — it writes no production code and touches no production data.

**Files:**
- Create: a throwaway script under the scratchpad directory (not committed).

**Interfaces:**
- Consumes: all three changes from Tasks 1–3.
- Produces: a written confirmation that the NIT no longer truncates, and that Schedule D reconciles or is correctly flagged SHORT.

- [ ] **Step 1: Confirm the NIT no longer truncates to one page**

The two PDFs for tender 3822 are already downloaded to the scratchpad as `NIT_3822.pdf` and `TD_3822.pdf`. If they are missing, re-fetch them read-only via `get_storage_service().as_local_file(key)` using the keys `command_center/290/1786009801_SWR - Bangalore - RMPU Work - NIT - Opnd on 12.08.2026.pdf` and `.../1786009739_... - TD - ....pdf`.

Write and run a scratchpad script that replicates the `_load_pdf_pages` trim loop (banner seen → look for `_SCHEDULE_END_RE` → truncate and break) over the NIT's 12 pages.

Expected **before** the fix: `KEPT pages: [1] of 12`.
Expected **after** the fix: pages 1–5 kept (the trim should now fire on page 5's `4. ELIGIBILITY CONDITIONS`), and the schedule codes visible in the kept text must include `D`.

- [ ] **Step 2: Confirm the reconciliation gate now flags a placeholder-only schedule**

In the same script, call `_reconcile_schedules` with only the four summary placeholder rows and tender 3822's real subtotals (`A: 180458734.37, B: 48124014.98, C: 134516700.80, D: 22245831.95, E: 67783712.43`).

Expected **before** the fix: `short == {'E'}` — A/B/C/D silently `ok`.
Expected **after** the fix: `short == {'A', 'B', 'C', 'D', 'E'}`.

- [ ] **Step 3: Record the outcome**

Report both measurements plainly. If either differs from the expectation, stop and investigate rather than adjusting the expectation — these numbers were measured against the real documents during diagnosis.

Note: a full re-extraction of tender 3822 calls the LLM and writes `boq_items` on the **live prod DB**. Do not run it as part of this task. Re-extraction and re-costing of affected tenders is a separate, explicitly-authorised follow-up (see below).

---

## Out of scope for this plan

- **Backfilling tender 3822 (and any other affected tenders).** The persisted costing for 3822 is wrong today and this plan does not correct it — the fix only stops new extractions from failing the same way. Re-running extraction is an LLM-cost, live-data write and needs its own authorisation and a re-costing pass afterward.
- **Finding the blast radius.** A read-only sweep for other tenders holding a placeholder row (description matching the "see Item Breakup" pattern, or a schedule with a header row and zero detail rows) would size the problem. Worth doing once Task 2 lands, since the gate fix is what makes such schedules detectable going forward.
- **The deterministic IREPS parser.** `nit_schedule_parser.parse_nit_text` returns zero lines for tender 3822's layout, so the run correctly fell through to the AI/vision path. Teaching it this second table shape (`S No. / Item No / Description of Item / Unit / Qty / Rate / Amount`) would make extraction cheaper and exact, but it is a separate piece of work with its own risk.
