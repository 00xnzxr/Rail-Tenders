# Costing Piece 2 — Reconciliation Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the BOQ reconciliation result (already computed and persisted by the existing gate) visible to the user in the two places it currently isn't: the strategic-summary Excel layout, and the costing agent's chat reply.

**Architecture:** No new reconciliation logic, no model changes. The breakdown-time gate (`app/services/costing/nit_reconciliation.reconcile`, wired via `cost_breakdown_service._run_reconciliation_gate`) already runs, reads `BOQScheduleTotal.stated_total` from the DB, and persists a report to `CostBreakdown.reconciliation_json` + `CostBreakdown.needs_review`. The Excel renderer `_write_reconciliation_block` already exists and is already called from both NIT-mirror layouts. Piece 2 closes exactly three wiring gaps: (1) call the existing `_write_reconciliation_block` from `_build_multi_sheet` (the strategic-summary layout, which currently omits it); (2) pass `reconciliation` into `breakdown_meta` in `_emit_costing_xlsx_artifact` (which currently doesn't); (3) append a failure-only reconciliation callout to the chat reply.

**Tech Stack:** Python 3, openpyxl, SQLAlchemy, pytest.

## Global Constraints

- No changes to `nit_reconciliation.py`, the `_run_reconciliation_gate`, or any DB model. Reconciliation is already computed and persisted — Piece 2 only surfaces it.
- Chat callout is **failure-only** (user decision): show a `⚠️` reconciliation callout ONLY when the report says not-ok; say nothing on success. Matches the existing `⚠️`/`🌐`/`🧮` callout style in `_format_costing_output`, which only fire on issues.
- Excel: **reuse the existing `_write_reconciliation_block`** (user decision) — do NOT add an inline column to the schedule table. Same per-schedule `Schedule | Line Sum | Stated Total | Delta | Status` table already shown in the NIT-mirror layouts.
- The reconciliation report shape (from `nit_reconciliation.ReconciliationReport`, serialized to `reconciliation_json`): a dict with keys `ok: bool`, `schedules: list[{code, line_sum, stated_total, delta, ok}]`, `grand_line_sum: float`, `advertised_value: float|None`, `grand_delta: float`. `_write_reconciliation_block` already consumes exactly this shape and is a no-op when passed a falsy value.
- `_write_reconciliation_block` signature (unchanged, `xlsx_generator_tool.py:164`): `_write_reconciliation_block(ws, start_row, reconciliation, *, header_fill, header_font, bold_font, border) -> int` (returns next free row).

---

## File Structure

- `drpl-backend/app/services/langchain/tools/xlsx_generator_tool.py` — call the existing reconciliation renderer from `_build_multi_sheet`.
- `drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py` — pass reconciliation into the artifact-emit `breakdown_meta`; add the failure-only chat callout.
- `drpl-backend/tests/test_costing_reconciliation_visibility.py` — new; tests for the callout helper + the multi-sheet render wiring.

---

### Task 1: Render the reconciliation block in the strategic-summary Excel layout

`_build_multi_sheet` builds the "1. Summary" sheet (tender snapshot + schedule-wise profitability table) but never calls `_write_reconciliation_block`, so the reconciliation table is missing from this layout (it only appears in the NIT-mirror layouts). Add the call right after the schedule-wise table, using the style vars already in scope.

**Files:**
- Modify: `drpl-backend/app/services/langchain/tools/xlsx_generator_tool.py` — inside `_build_multi_sheet`, immediately after the schedule-wise profitability table ends (the `row += 1` at line 1459, before the `KEY OBSERVATIONS` block at line 1461).
- Test: `drpl-backend/tests/test_costing_reconciliation_visibility.py` (create)

**Interfaces:**
- Consumes: existing `_write_reconciliation_block(ws, start_row, reconciliation, *, header_fill, header_font, bold_font, border) -> int` (`xlsx_generator_tool.py:164`); `breakdown_meta` dict (in scope in `_build_multi_sheet`); style vars in scope at line 1459: `header_fill` (line 1334), `header_font` (line 1333), `totals_font` (line 1336), `border_thin` (line 1338).
- Produces: no new signature; behavior change is that `_build_multi_sheet` output includes the reconciliation block when `breakdown_meta["reconciliation"]` is present.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_costing_reconciliation_visibility.py`:

```python
"""Piece 2 — reconciliation surfaced in the strategic-summary xlsx + chat reply."""
import openpyxl
from app.services.langchain.tools.xlsx_generator_tool import build_cost_xlsx


def _recon_report(ok=False):
    return {
        "ok": ok,
        "schedules": [
            {"code": "A", "line_sum": 100.0, "stated_total": 100.0, "delta": 0.0, "ok": True},
            {"code": "B", "line_sum": 250.0, "stated_total": 200.0, "delta": 50.0, "ok": False},
        ],
        "grand_line_sum": 350.0,
        "advertised_value": 300.0,
        "grand_delta": 50.0,
    }


def test_multi_sheet_renders_reconciliation_block(tmp_path):
    # Strategic-summary layout is selected when strategic_summary is present.
    out = tmp_path / "cost.xlsx"
    build_cost_xlsx(
        str(out),
        "Test Cost Sheet",
        rows=[
            {"sr_no": "1", "description": "Item A", "schedule_name": "A",
             "quantity": 1, "tender_rate": 100, "tender_amount": 100,
             "rate": 90, "amount": 90},
        ],
        strategic_summary={
            "tender_snapshot": {"tender_no": "T1"},
            "schedule_breakdown": [
                {"schedule": "Schedule A", "tender_value_inr": 100,
                 "estimated_cost_inr": 90, "gross_margin_inr": 10, "gross_margin_pct": 10.0},
            ],
        },
        breakdown_meta={"tender_id": 1, "reconciliation": _recon_report(ok=False)},
    )
    wb = openpyxl.load_workbook(str(out))
    ws = wb["1. Summary"]
    texts = [c.value for r in ws.iter_rows() for c in r if isinstance(c.value, str)]
    assert any("Reconciliation" in t for t in texts), "reconciliation block missing from Summary sheet"
    # The failing schedule B must be shown with a FLAG status.
    assert any(t == "FLAG" for t in texts), "failed schedule not flagged in reconciliation block"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_reconciliation_visibility.py::test_multi_sheet_renders_reconciliation_block -v`
Expected: FAIL — `AssertionError: reconciliation block missing from Summary sheet` (the multi-sheet layout doesn't render it yet).

- [ ] **Step 3: Write minimal implementation**

In `xlsx_generator_tool.py` `_build_multi_sheet`, the schedule-wise table ends with `row += 1` at line 1459, immediately before `obs = (strategic_summary or {}).get("key_observations") or []` at line 1461. Insert the reconciliation block between them:

```python
    # Reconciliation (breakdown-time gate) — reuse the shared renderer so this
    # layout shows the same per-schedule stated-total vs line-sum table the
    # NIT-mirror layouts already show. No-op when no reconciliation report.
    reconciliation = (breakdown_meta or {}).get("reconciliation")
    if reconciliation:
        row = _write_reconciliation_block(
            summary_ws,
            row,
            reconciliation,
            header_fill=header_fill,
            header_font=header_font,
            bold_font=totals_font,
            border=border_thin,
        )
        row += 1
```

(`summary_ws` is the Summary worksheet variable in `_build_multi_sheet` (`xlsx_generator_tool.py:1328`); `row` is the running cursor used by the schedule table above; `header_fill`/`header_font`/`totals_font`/`border_thin` are all defined at lines 1333-1338.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_reconciliation_visibility.py::test_multi_sheet_renders_reconciliation_block -v`
Expected: PASS.

- [ ] **Step 5: Run xlsx regression to confirm no layout break**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/ -k "xlsx or costing" -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/langchain/tools/xlsx_generator_tool.py drpl-backend/tests/test_costing_reconciliation_visibility.py
git commit -m "feat(costing): render reconciliation block in strategic-summary xlsx layout

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Pass reconciliation into `_emit_costing_xlsx_artifact`'s breakdown_meta

`_emit_costing_xlsx_artifact` builds `breakdown_meta={"tender_id": tender_id, **_defaults}` (no `reconciliation`), so even after Task 1, the artifact-emit path renders no reconciliation block. Fetch the persisted report from the tender's latest `CostBreakdown.reconciliation_json` and add it — mirroring how `render_breakdown_xlsx_bytes` already does it (`cost_breakdown_service.py:1891-1914`).

**Files:**
- Modify: `drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py` — `_emit_costing_xlsx_artifact`, at the `build_cost_xlsx(...)` call (`breakdown_meta` at line 3193).
- Test: `drpl-backend/tests/test_costing_reconciliation_visibility.py` (extend)

**Interfaces:**
- Consumes: `CostBreakdown.reconciliation_json` (Text, JSON-serialized report) — the latest breakdown for `tender_id`. Reuse the existing model import already present in `chat_agent_wrappers.py`.
- Produces: no new signature; `breakdown_meta` now carries `"reconciliation"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_costing_reconciliation_visibility.py`:

```python
def test_emit_artifact_threads_reconciliation_into_meta(monkeypatch):
    """_emit_costing_xlsx_artifact must put the persisted reconciliation report
    into breakdown_meta so the multi-sheet layout can render it. We capture the
    breakdown_meta handed to build_cost_xlsx."""
    import app.services.langchain.graphs.chat_agent_wrappers as caw

    captured = {}

    def _fake_build(path, title, rows, *, cost_assumptions, strategic_summary,
                    breakdown_meta, single_sheet=False):
        captured["meta"] = breakdown_meta
        # Write a tiny valid xlsx so the surrounding code can read bytes.
        import openpyxl
        openpyxl.Workbook().save(path)
        return {"total_amount": 0, "total_with_gst": 0}

    monkeypatch.setattr(caw, "build_cost_xlsx", _fake_build)

    # Stub the breakdown lookup to return a report.
    report = {"ok": False, "schedules": [], "grand_line_sum": 0,
              "advertised_value": None, "grand_delta": 0}
    monkeypatch.setattr(caw, "_latest_reconciliation_for_tender",
                        lambda db, tid: report)

    # Minimal stubs for the rest of the emit path.
    class _DB: pass
    costing = {"line_items": [], "strategic_summary": {}, "cost_assumptions": []}
    caw._emit_costing_xlsx_artifact(_DB(), proposal_session_id=None,
                                    tender_id=1, costing=costing)
    assert captured["meta"].get("reconciliation") == report
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_reconciliation_visibility.py::test_emit_artifact_threads_reconciliation_into_meta -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_latest_reconciliation_for_tender'` (helper not defined yet).

- [ ] **Step 3: Write minimal implementation**

In `chat_agent_wrappers.py`, add a small module-level helper (near the other costing helpers, above `_emit_costing_xlsx_artifact`):

```python
def _latest_reconciliation_for_tender(db, tender_id: int) -> Optional[dict]:
    """Return the parsed reconciliation report from the tender's most recent
    CostBreakdown, or None. Mirrors cost_breakdown_service.render_breakdown_xlsx_bytes.
    """
    import json
    from app.models.cost_breakdown import CostBreakdown
    try:
        bd = (
            db.query(CostBreakdown)
            .filter(CostBreakdown.tender_id == tender_id)
            .order_by(CostBreakdown.version.desc())
            .first()
        )
        if bd and bd.reconciliation_json:
            return json.loads(bd.reconciliation_json)
    except Exception:
        pass
    return None
```

Then change the `breakdown_meta` at the `build_cost_xlsx` call (line 3193) from:
```python
            breakdown_meta={"tender_id": tender_id, **_defaults},
```
to:
```python
            breakdown_meta={
                "tender_id": tender_id,
                "reconciliation": _latest_reconciliation_for_tender(db, tender_id),
                **_defaults,
            },
```

(If `Optional` is not already imported in this module, it is — the file uses `Optional[dict]` return hints throughout; confirm with a grep and only add the import if absent.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_reconciliation_visibility.py::test_emit_artifact_threads_reconciliation_into_meta -v`
Expected: PASS.

- [ ] **Step 5: Confirm module still imports**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -c "import app.services.langchain.graphs.chat_agent_wrappers"`
Expected: no error.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py drpl-backend/tests/test_costing_reconciliation_visibility.py
git commit -m "feat(costing): thread persisted reconciliation into costing xlsx artifact meta

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Failure-only reconciliation callout in the chat reply

Add a `⚠️` callout to `_format_costing_output` shown ONLY when the reconciliation report is present and not-ok, listing how many schedules don't reconcile. On success (or no report), say nothing.

**Files:**
- Modify: `drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py` — `_format_costing_output` (callout blocks region, near lines 3453-3470) and `chat_costing_research` (to make the reconciliation report available to the formatter after `cost_breakdown_id` is known).
- Test: `drpl-backend/tests/test_costing_reconciliation_visibility.py` (extend)

**Interfaces:**
- Consumes: `_latest_reconciliation_for_tender` (Task 2). A reconciliation report dict (or None).
- Produces: `_reconciliation_callout(reconciliation: Optional[dict]) -> Optional[str]` — returns the markdown callout string when the report is present and `ok is False`, else None. Used by `_format_costing_output`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_costing_reconciliation_visibility.py`:

```python
from app.services.langchain.graphs.chat_agent_wrappers import _reconciliation_callout


def test_callout_none_when_reconciled():
    report = {"ok": True, "schedules": [
        {"code": "A", "line_sum": 100, "stated_total": 100, "delta": 0, "ok": True}]}
    assert _reconciliation_callout(report) is None


def test_callout_none_when_no_report():
    assert _reconciliation_callout(None) is None


def test_callout_warns_and_counts_failed_schedules():
    report = {"ok": False, "schedules": [
        {"code": "A", "line_sum": 100, "stated_total": 100, "delta": 0, "ok": True},
        {"code": "B", "line_sum": 250, "stated_total": 200, "delta": 50, "ok": False},
        {"code": "C", "line_sum": 300, "stated_total": 250, "delta": 50, "ok": False}]}
    out = _reconciliation_callout(report)
    assert out is not None
    assert "⚠️" in out
    assert "2" in out          # two schedules failed
    assert "B" in out and "C" in out  # names the failing schedules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_reconciliation_visibility.py -k callout -v`
Expected: FAIL — `ImportError: cannot import name '_reconciliation_callout'`.

- [ ] **Step 3: Write minimal implementation**

In `chat_agent_wrappers.py`, add the helper near `_format_costing_output`:

```python
def _reconciliation_callout(reconciliation: Optional[dict]) -> Optional[str]:
    """Failure-only markdown callout for the costing chat reply. Returns None
    when there is no report or the report reconciles; otherwise names the
    schedules whose extracted line-sum doesn't match the NIT's printed total.
    """
    if not reconciliation or reconciliation.get("ok"):
        return None
    failed = [s for s in (reconciliation.get("schedules") or []) if not s.get("ok")]
    if not failed:
        return None
    codes = ", ".join(str(s.get("code")) for s in failed)
    return (
        f"> ⚠️ **{len(failed)} schedule(s) don't reconcile with the NIT** "
        f"({codes}) — the extracted line totals differ from the tender's printed "
        f"sub-totals. Review these schedules before submitting."
    )
```

Then in `_format_costing_output`, the function needs the reconciliation report. Its current signature is `_format_costing_output(costing, tender_id)`. Add an optional `reconciliation` param defaulting to None so existing callers are unaffected:
```python
def _format_costing_output(costing, tender_id, reconciliation=None):
```
and in the callout region (after the `derived` block that ends at line 3470), append:
```python
    _recon_callout = _reconciliation_callout(reconciliation)
    if _recon_callout:
        parts.append(_recon_callout)
        parts.append("")
```

- [ ] **Step 4: Wire the report into the reply in `chat_costing_research`**

Verified structure of `chat_costing_research`: `output` is first assigned at line 2463 (BEFORE persistence); persistence + the reconciliation gate run at ~lines 2510-2566; the `resp` dict that reads `output` is built at line 2592; `cost_breakdown_id` is available by line 2606. Because the reconciliation gate runs during persistence, the current run's report only exists AFTER line ~2566.

Do NOT touch line 2463 — leave it as-is (it's the fallback default and keeps `output` defined for any early path). Instead, recompute `output` with the reconciliation report immediately BEFORE the `resp` dict is built at line 2592. Insert directly above `resp = {` (line 2592):

```python
        # Recompute the reply with this run's reconciliation report, now that
        # persistence (which runs the reconciliation gate) has completed.
        if tender_id:
            _recon = _latest_reconciliation_for_tender(db, tender_id)
            if _recon is not None:
                output = _format_costing_output(costing, tender_id, reconciliation=_recon)
```

This re-derives `output` only when a report exists, so the failure-only callout reflects the current run; the untouched line-2463 assignment covers the no-tender / no-report path. `resp["output"] = output` at line 2593 then picks up the recomputed value.

- [ ] **Step 5: Run callout + reply tests**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_reconciliation_visibility.py -v`
Expected: PASS (all callout tests + Task 1/2 tests).

- [ ] **Step 6: Confirm module imports + costing regression**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -c "import app.services.langchain.graphs.chat_agent_wrappers"` (no error), then `./venv/Scripts/python.exe -m pytest tests/ -k "costing or xlsx" -q` (PASS).

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py drpl-backend/tests/test_costing_reconciliation_visibility.py
git commit -m "feat(costing): failure-only reconciliation callout in costing chat reply

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage (Piece 2 scope):**
- Reconciliation carried through to the final Excel (strategic-summary layout) → Task 1 (reuse `_write_reconciliation_block`). ✅
- Reconciliation data reaches the artifact-emit path → Task 2 (`_latest_reconciliation_for_tender` into `breakdown_meta`). ✅
- Reconciliation surfaced in the agent reply → Task 3 (failure-only callout). ✅
- Reuse the existing gate / no re-implementation → confirmed: `nit_reconciliation.reconcile` + `_run_reconciliation_gate` already run and persist; Piece 2 only reads `reconciliation_json`. ✅
- Failure-only chat callout (user decision) → Task 3 `_reconciliation_callout` returns None on ok/absent. ✅
- Reuse `_write_reconciliation_block` not an inline column (user decision) → Task 1. ✅
- No model/gate changes → none in any task. ✅

**Placeholder scan:** clean. Task 3 Step 4's ordering dependency in `chat_costing_research` is pinned to exact verified line anchors (2463 assign, ~2566 persistence, 2592 `resp`, 2606 `cost_breakdown_id`) with an exact insertion point (directly above `resp = {` at line 2592) and exact code. No "figure it out on site" steps remain. All steps carry exact code and commands.

**Type consistency:** `_latest_reconciliation_for_tender(db, tender_id) -> Optional[dict]` defined in Task 2, consumed in Task 3. `_reconciliation_callout(Optional[dict]) -> Optional[str]` defined + consumed in Task 3. `_write_reconciliation_block(...)` signature matches its definition at `xlsx_generator_tool.py:164` and the existing call at line 809. The report dict shape (`ok`, `schedules[{code,line_sum,stated_total,delta,ok}]`, ...) is consistent across all three tasks and matches `ReconciliationReport`.

**Out of scope (Piece 3):** OEM manufacturer + web-link columns — separate plan.
