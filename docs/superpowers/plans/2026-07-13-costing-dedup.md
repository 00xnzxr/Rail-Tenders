# Costing Line-Item Content Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the costing agent's Excel export from showing the same work item twice by collapsing content-duplicate line items (keeping the best-evidence one) before they are persisted.

**Architecture:** Add one pure helper, `_dedup_lines_by_content`, to `cost_breakdown_service.py`. Call it once inside `persist_from_agent_output` — after the existing NIT-schedule partition gate and before line normalization — so it runs on every agent-JSON persistence route (`single` and `component_expansion`). The deterministic `batched` path never calls this function and is already immune. Duplicates are decided by a strict content key (normalized description + qty + unit) and resolved by ranking `rate_source`.

**Tech Stack:** Python 3, SQLAlchemy, pytest.

## Global Constraints

- Match key is strict: description **AND** qty **AND** unit must all match to merge. Never merge on description alone.
- Tax lines (`is_tax_line` truthy) are NEVER deduped.
- Lines with empty / `(no description)` description are NEVER merged.
- Winner = highest `rate_source` rank; ties → first occurrence. Rank order (best→weakest): `training_data`(6) > `tender_estimate`(5) > `web_search`(4) > `memory`(3) > `derived_estimate`(2) > `needs_user_input`(1) > unknown/blank(0).
- Survivors keep their original relative order.
- Drops are silent to the user — `logger.warning` only, NO `assumptions` note.
- No change to `xlsx_generator_tool.py` or the `batched` skeleton path.
- Line dicts use key `quantity` (fallback `qty`), and `unit`, matching the existing convention at `cost_breakdown_service.py:244`.

---

### Task 1: `_dedup_lines_by_content` helper

**Files:**
- Modify: `drpl-backend/app/services/cost_breakdown_service.py` (add helper + module-level rank constant; place directly after `_partition_lines_against_schedule`, which ends at line 629)
- Test: `drpl-backend/tests/test_costing_dedup.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks. Reuses existing module helper `_coerce_float(val) -> Optional[float]` (defined at `cost_breakdown_service.py:49`).
- Produces: `_dedup_lines_by_content(line_items: list[dict]) -> tuple[list[dict], int]` — returns `(survivors, dropped_count)`. Used by Task 2.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_costing_dedup.py`:

```python
"""Unit tests for content-based costing line-item dedup."""
from app.services.cost_breakdown_service import _dedup_lines_by_content


def _line(desc, qty, unit, source, rate):
    return {
        "description": desc, "quantity": qty, "unit": unit,
        "rate_source": source, "rate": rate,
    }


def test_screenshot_case_keeps_best_evidence():
    # The exact reported bug: same work, two sources, different SNs.
    rows = [
        {"sr_no": "2", "description": "Scrapping, cleaning and painting work of Battery Box. (ICF type coaches)",
         "quantity": 2208, "unit": "Numbers", "rate_source": "derived_estimate", "rate": 126.64},
        {"sr_no": "A2", "description": "Scrapping, cleaning and painting work of Battery Box. (ICF type coaches)",
         "quantity": 2208, "unit": "Numbers", "rate_source": "training_data", "rate": 158.33},
    ]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 1
    assert len(survivors) == 1
    assert survivors[0]["rate_source"] == "training_data"


def test_distinct_qty_not_merged():
    rows = [_line("Item X", 10, "Nos", "training_data", 5),
            _line("Item X", 20, "Nos", "derived_estimate", 5)]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 0 and len(survivors) == 2


def test_distinct_unit_not_merged():
    rows = [_line("Item X", 10, "Nos", "training_data", 5),
            _line("Item X", 10, "Kg", "derived_estimate", 5)]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 0 and len(survivors) == 2


def test_tax_lines_never_merged():
    a = _line("GST @ 18%", 1, "", "", 0); a["is_tax_line"] = True
    b = _line("GST @ 18%", 1, "", "", 0); b["is_tax_line"] = True
    survivors, dropped = _dedup_lines_by_content([a, b])
    assert dropped == 0 and len(survivors) == 2


def test_empty_descriptions_never_merged():
    rows = [_line("", 10, "Nos", "training_data", 5),
            _line("   ", 10, "Nos", "derived_estimate", 5)]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 0 and len(survivors) == 2


def test_survivor_order_preserved():
    rows = [
        _line("Alpha", 1, "Nos", "web_search", 1),
        _line("Beta", 1, "Nos", "training_data", 2),
        _line("Alpha", 1, "Nos", "training_data", 3),  # dup of row 0, better source
    ]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 1
    assert [s["description"] for s in survivors] == ["Alpha", "Beta"]
    # Winner for Alpha is the training_data one (rate 3), placed at Alpha's first slot.
    assert survivors[0]["rate"] == 3


def test_normalization_collapses_whitespace_and_case():
    rows = [_line("Battery  BOX  work", 5, "Nos", "derived_estimate", 1),
            _line("battery box work", 5, "nos", "training_data", 2)]
    survivors, dropped = _dedup_lines_by_content(rows)
    assert dropped == 1 and survivors[0]["rate_source"] == "training_data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_costing_dedup.py -v`
Expected: FAIL — `ImportError: cannot import name '_dedup_lines_by_content'`.

- [ ] **Step 3: Write minimal implementation**

In `drpl-backend/app/services/cost_breakdown_service.py`, insert immediately after the end of `_partition_lines_against_schedule` (after line 629, before `def _create_breakdown_with_lines`):

```python
# rate_source evidence ranking (best → weakest). Higher wins a content-dup tie.
_RATE_SOURCE_RANK = {
    "training_data": 6,
    "tender_estimate": 5,
    "web_search": 4,
    "memory": 3,
    "derived_estimate": 2,
    "needs_user_input": 1,
}


def _normalize_desc(s: Optional[str]) -> str:
    """Lowercase, collapse internal whitespace, strip surrounding punctuation."""
    import re
    text = (s or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,:;-_/")


def _content_key(line: dict) -> Optional[tuple]:
    """Content identity for dedup: (norm description, qty, norm unit).

    Returns None when the line must never be merged (blank/placeholder
    description). Callers skip tax lines separately.
    """
    desc = _normalize_desc(line.get("description"))
    if not desc or desc == "(no description)":
        return None
    qty = _coerce_float(line.get("quantity") if line.get("quantity") is not None
                        else line.get("qty"))
    qty_key = round(qty, 3) if qty is not None else None
    unit = _normalize_desc(line.get("unit"))
    return (desc, qty_key, unit)


def _source_rank(line: dict) -> int:
    return _RATE_SOURCE_RANK.get((line.get("rate_source") or "").strip().lower(), 0)


def _dedup_lines_by_content(line_items: list[dict]) -> tuple[list[dict], int]:
    """Collapse lines describing the same physical work item, keeping the
    best-evidence occurrence.

    Two lines are "the same work" when their content key
    (normalized description + quantity + normalized unit) is equal. Tax lines
    and lines with a blank/placeholder description are never merged. When a
    group has more than one line, the one with the highest `rate_source` rank
    wins (ties → first occurrence); it is placed at the position of the group's
    FIRST occurrence so surviving order is stable.

    Returns (survivors, dropped_count).
    """
    # first_index[key] = position in `survivors` of the current winner for key.
    first_index: dict[tuple, int] = {}
    survivors: list[dict] = []
    dropped = 0
    for line in line_items:
        if line.get("is_tax_line"):
            survivors.append(line)
            continue
        key = _content_key(line)
        if key is None:
            survivors.append(line)
            continue
        if key not in first_index:
            first_index[key] = len(survivors)
            survivors.append(line)
            continue
        # Duplicate group — keep whichever line has the stronger source.
        pos = first_index[key]
        if _source_rank(line) > _source_rank(survivors[pos]):
            survivors[pos] = line
        dropped += 1
    return survivors, dropped
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && pytest tests/test_costing_dedup.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/cost_breakdown_service.py drpl-backend/tests/test_costing_dedup.py
git commit -m "feat(costing): content-based line-item dedup helper

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire dedup into `persist_from_agent_output`

**Files:**
- Modify: `drpl-backend/app/services/cost_breakdown_service.py` — inside `persist_from_agent_output`, between the advisory-validation block and the `defaults = get_costing_defaults(db)` line (currently line 1414).
- Test: `drpl-backend/tests/test_costing_dedup.py` (extend)

**Interfaces:**
- Consumes: `_dedup_lines_by_content` (Task 1).
- Produces: no new public interface; behavior change is that `persist_from_agent_output` writes deduped lines.

- [ ] **Step 1: Write the failing test**

Append to `drpl-backend/tests/test_costing_dedup.py`:

```python
def test_persist_call_order_dedups_before_normalize(monkeypatch):
    """persist_from_agent_output must run content-dedup on the line list it
    passes downstream. We stub the DB-touching internals and capture the
    line_items handed to _create_breakdown_with_lines."""
    import app.services.cost_breakdown_service as svc

    captured = {}

    # Component path (skip_nit_validation=True) so the NIT partition gate is
    # bypassed — this is the route with NO other dedup, proving ours runs.
    monkeypatch.setattr(svc, "get_costing_defaults",
                        lambda db: {"overhead_percent": 0, "margin_percent": 0, "gst_percent": 18})
    monkeypatch.setattr(svc, "_normalize_line_dict",
                        lambda line, **kw: dict(line))

    class _Q:
        def filter(self, *a, **k): return self
        def all(self): return []
    class _DB:
        def query(self, *a, **k): return _Q()

    def _fake_create(db, tender_id, normalised, costing, defaults, **kw):
        captured["lines"] = normalised
        return object()
    monkeypatch.setattr(svc, "_create_breakdown_with_lines", _fake_create)

    costing = {"line_items": [
        {"description": "Battery Box work", "quantity": 2208, "unit": "Numbers",
         "rate_source": "derived_estimate", "rate": 126.64},
        {"description": "Battery Box work", "quantity": 2208, "unit": "Numbers",
         "rate_source": "training_data", "rate": 158.33},
    ]}
    svc.persist_from_agent_output(
        db=_DB(), tender_id=1, costing=costing, skip_nit_validation=True,
    )
    assert len(captured["lines"]) == 1
    assert captured["lines"][0]["rate_source"] == "training_data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && pytest tests/test_costing_dedup.py::test_persist_call_order_dedups_before_normalize -v`
Expected: FAIL — `assert 2 == 1` (both lines still persisted; dedup not yet wired in).

- [ ] **Step 3: Write minimal implementation**

In `persist_from_agent_output`, insert immediately BEFORE the line `defaults = get_costing_defaults(db)` (currently line 1414):

```python
    # Content-based dedup (design: 2026-07-13-costing-dedup). Runs on EVERY
    # agent-JSON route — including component build-up, which sets
    # skip_nit_validation=True and thus bypasses _partition_lines_against_schedule
    # entirely. Collapses lines describing the same work (same normalized
    # description + qty + unit) that the LLM emitted twice with different
    # rate_source / SN, keeping the best-evidence one. The deterministic batched
    # path never reaches here and is already 1:1.
    line_items, dup_dropped = _dedup_lines_by_content(line_items)
    if dup_dropped:
        logger.warning(
            f"[cost_breakdown] tender {tender_id}: content-dedup dropped "
            f"{dup_dropped} duplicate line(s)"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && pytest tests/test_costing_dedup.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Run the broader costing suite to confirm no regression**

Run: `cd drpl-backend && pytest tests/test_costing_no_margin.py tests/test_costing_xlsx_storage.py tests/test_costing_dedup.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/cost_breakdown_service.py drpl-backend/tests/test_costing_dedup.py
git commit -m "fix(costing): dedup duplicate line items before persistence

Collapses same-work rows the agent emitted twice with different rate_source
(e.g. training_data + derived_estimate for the same item), keeping the
best-evidence one. Runs on single + component routes; batched is unaffected.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- New unit `_dedup_lines_by_content` → Task 1. ✅
- Match key (norm desc + qty + unit) → Task 1 `_content_key`, tests 2–3, 7. ✅
- Tax lines never merged → Task 1, test 4. ✅
- Empty descriptions never merged → Task 1 `_content_key` returns None, test 5. ✅
- Winner = best rate_source rank, ties first → Task 1 `_RATE_SOURCE_RANK` / `_source_rank`, tests 1, 6. ✅
- Survivor order preserved → Task 1 (winner placed at first slot), test 6. ✅
- Integration after partition gate, before normalize, every path → Task 2, covers `skip_nit_validation=True`. ✅
- Silent + log only, no assumptions note → Task 2 impl (`logger.warning` only). ✅
- `batched` path untouched → not called from `run_costing_batched_node`; noted in Task 2 comment. ✅

**Placeholder scan:** none — all steps carry real code and exact commands.

**Type consistency:** `_dedup_lines_by_content(list[dict]) -> tuple[list[dict], int]` is defined in Task 1 and consumed with that exact signature in Task 2. `_coerce_float`, `_normalize_desc`, `_content_key`, `_source_rank` names are consistent across tasks.
