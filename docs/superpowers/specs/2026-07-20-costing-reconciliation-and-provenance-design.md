# Costing Reconciliation & Provenance — Design Spec

**Date:** 2026-07-20
**Status:** Approved design → ready for implementation planning
**Goal:** Fix the three reported costing defects — (1) BOQ row duplication vs. the NIT, (2) missing OEM manufacturer + web link for web-priced components, (3) total / schedule-value mismatch between the created Excel and the NIT — via a safe, staged dev path that never destabilizes the fragile costing pipeline.

---

## 1. Problem, as proven by the live data

A read-only probe of the live Neon DB (22 tenders with parsed BOQ rows, 2075 rows) established the root cause with hard evidence, not theory:

- **Duplication is systematic and integer-multiple.** Comparing `sum(basic_value)` per schedule against the NIT's captured printed total (`BOQScheduleTotal.stated_total`):
  - Clean extractions reconcile **exactly** — `t=2723 sch=B` and `t=2733 sch=C` at **0.0%** diff to the rupee.
  - Broken ones are integer multiples: `t=2732 sch=D` = **100.0% (2×)**, `t=2693 sch=A` ≈ **1000% (11×)**, `t=2733 sch=B` ≈ **2016% (21×)**.
  - `sr_no` repeats within a tender confirm the mechanism: `t=1198 sr_no=1` appears **22×**, `t=2732 sr_no=2` appears **9×**.
- **Mechanism:** in `boq_parser_service.py`, a single logical NIT line item whose item-code / description cell is *merged* across several unmerged sub-rows gets emitted once **per spanned sub-row** — an integer-multiple duplication.
- **Consequence chain:** duplicated rows feed the Excel's live `SUM` formulas → inflated schedule totals and grand total. So the "total mismatch" defect is *caused by* the duplication defect; they are one root cause.
- **The reliable detector already exists but is under-captured.** Schedule-total reconciliation is exact on clean data, but `BOQScheduleTotal` was captured for only **4 of 22** tenders (~18%). Fixing capture is a prerequisite to gating on it.
- **Existing dedup does NOT help here.** `_dedup_lines_by_content` (design `2026-07-13-costing-dedup`) keys on `description + qty + unit`; merged sub-rows differ on those, so they slip through. `item_code` is unreliable as a key (values like `"Mechanical"`, `"a"`, `"1"`). **Value reconciliation, not content dedup, is the correct primary detector.**

Re-probe script: `probe_nit_totals.py` (uses `app.core.database.SessionLocal`, models in `app.models.costing_template`). **Local `.env` `DATABASE_URL` is the live prod Neon DB — all probing is read-only.**

---

## 2. Core principle & reconciliation contract

**A BOQ extraction is *trusted* only if its rows reconcile to the NIT's own printed totals.** Reconciliation is a hard contract enforced at the extraction boundary — deterministic arithmetic, never model whim.

**Reconciliation key:** group extracted rows by `schedule_name`, sum `basic_value` per group, compare to the captured `stated_total` for that `schedule_code` within a tolerance band.

**Per-tender / per-schedule `reconciliation_status`:**
- `reconciled` — sums match a printed total within tolerance.
- `reconciliation_failed` — a printed total exists and the sum is out of tolerance (the duplication case).
- `no_anchor` — no printed total to check against (fall back to a grand-total check vs `Tender.estimated_value`; if that's also absent, `no_anchor`, and we do **not** block).

**On `reconciliation_failed` (user-chosen policy): block + flag for review.** Rows are still persisted, but the tender's BOQ is flagged `reconciliation_failed` and a visible warning is surfaced to the UI and the agent. A silently-wrong sheet must never ship. (Not chosen: hard no-persist.)

> **Implementation note (2026-07-20):** the shipped Piece 1 *does* deterministically auto-collapse the specific, verified duplication shapes (null/blank-item_code merged-summary rows + identical-value `(sr_no, value, qty)` twins) — this was validated against live-prod tenders to reconcile all mismatched schedules to +0.0%. Auto-collapse here is the deterministic remedy, not the "best-effort" guess this section originally set aside; anything it cannot collapse still falls through to `reconciliation_failed` + flag. See `docs/superpowers/plans/2026-07-20-costing-piece1-reconciliation-gate.md`.

**Determinism boundary (CONSTITUTION discipline):** the reconciliation *gate* is pure/deterministic; only *extraction* is LLM. The "is this correct?" decision is arithmetic against the NIT's printed number, never a model judgment.

---

## 3. Dev path: three independent, separately-shippable pieces

Shipped in dependency order; each is its own branch, spec-plan, and PR, and merges green before the next starts.

- **Piece 1** — kill duplication at the source + reconciliation gate (upstream root cause).
- **Piece 2** — carry reconciliation through to a visible end-to-end guarantee (validates Piece 1).
- **Piece 3** — OEM manufacturer + web link as first-class columns (additive, zero correctness risk).

**Ordering rationale:** Piece 1 causes the total mismatch, so it is highest-leverage. Piece 2 can't be trusted until Piece 1 is fixed (it validates it). Piece 3 is independent and safe, so it goes last where it cannot endanger the correctness work.

---

## 4. Piece 1 — Extraction fix + reconciliation gate

All in `boq_parser_service.py` unless noted. Named, independently-testable units.

### A. `_reconcile_schedules(rows, schedule_totals, tender_estimated_value) -> ReconResult` (pure, deterministic)
- Groups rows by `schedule_name`, sums `basic_value` per group.
- For each schedule with a `stated_total`: `diff_pct = (summed - stated) / stated * 100`; passes if `abs(diff_pct) <= TOLERANCE`.
- Duplication fingerprint: `ratio = summed / stated`; if `ratio` is within `0.02` of an integer `>= 2`, tag `suspected_multiple = round(ratio)`.
- Returns per-schedule status + an overall `reconciled | reconciliation_failed | no_anchor`. No printed totals anywhere → fall back to a grand-total check against `Tender.estimated_value`; if absent → `no_anchor`.
  - **Caveat for the fallback:** `Tender.estimated_value` may be GST-inclusive and/or rounded, while `sum(basic_value)` is pre-GST. The grand-total fallback must use a **wider tolerance** (proposed 20%) and is only strong enough to catch gross integer-multiple duplication (2×, 11×), **not** to certify `reconciled`. A schedule-level `stated_total` match is the only signal that yields `reconciled`; the fallback can only yield `reconciliation_failed` (gross mismatch) or `no_anchor`.
- **Zero LLM. Unit-testable with the real numbers** (t=2693, t=2723, t=2732, t=2733).

### B. Merged-cell-aware extraction prompt (`_BOQ_AI_SYSTEM_PROMPT`, ~`:868`)
- Add a rule block: *a single logical line item may span a merged cell (item code / description) with several unmerged sub-rows; emit exactly ONE row per logical item — never repeat the item because it visually spans multiple grid rows; if sub-rows carry their own quantities, sum them into the parent's quantity.*
- Add: *capture each schedule's printed total and the grand total verbatim into the `schedule_totals` output* (fixes the 18%-capture gap).

### C. `_capture_schedule_totals(...)`
- Reliably persist printed per-schedule + grand totals into `BOQScheduleTotal` (`stated_total`, `advertised_value`). Idempotent per `(tender_id, schedule_code)`.

### D. Re-extraction escalation (`parse_boq_from_tender`)
- On `reconciliation_failed`, run **exactly one** additional vision pass with a sharpened prompt naming the failing schedule and its `suspected_multiple` (e.g. *"Schedule B rows sum to ~2× the printed total — you are likely repeating merged rows; emit each logical item once"*). Reconcile again.
- Still failing → `reconciliation_failed`, persist + flag. (Reconcile-first, escalate once — user-chosen.)

### E. Persistence + flag
- Extend the write path so `reconciliation_status` and per-schedule diff are stored and returned. `reconciliation_failed` sets a tender-visible warning field.

### Config (tunable via env, `app/core/config.py`)
- `costing_reconcile_tolerance_pct = 1.0` (`COSTING_RECONCILE_TOLERANCE_PCT`) — clean rows hit 0.0%, so 1% is generous.
- `costing_reconcile_reextract = True` (`COSTING_RECONCILE_REEXTRACT`) — toggle the escalation pass.
- Reconcile gate default ON.

### Explicitly NOT changed
- The deterministic `pdfplumber` fast-path, the batched costing skeleton, and `_dedup_lines_by_content` (it catches a different, LLM-emitted duplication and is harmless here).

---

## 5. Piece 2 — Reconciliation as a visible end-to-end guarantee

- **`cost_breakdown_service`**: when building the final breakdown, **reuse Piece 1's `_reconcile_schedules`** (single source of truth — no second implementation) against `BOQScheduleTotal`. Store the result on `CostBreakdown` (`reconciliation_status`, per-schedule diff JSON).
- **`build_strategic_summary` / Excel summary sheet**: add a **Reconciliation** badge per schedule — `✓ matches NIT (₹X)` or `⚠ differs by N% (extracted ₹Y vs NIT ₹X)`. Grand total gets the same treatment vs `Tender.estimated_value`. This directly answers the "total & schedule value mismatch with the Excel" defect: the sheet proves it matches, or flags exactly where it doesn't.
- **`chat_agent_wrappers`**: include reconciliation status in the costing response so the agent says "Schedule B didn't reconcile" instead of confidently returning wrong numbers.
- **No new LLM calls** — pure passthrough of Piece 1's arithmetic.

---

## 6. Piece 3 — OEM manufacturer + web link as first-class columns (additive)

**Decision: OEM is best-effort; the web link is guaranteed on every web-priced line.**

- **`CostBreakdownLine` model** (`cost_breakdown.py`): add `oem_manufacturer` (String) and `source_url` (Text). Schema-drift discipline: Alembic revision **+** entries in `_apply_schema_drift_fixes()` **and** `_add_missing_columns()` so existing deployments self-heal.
- **Web search return** (`web_search_tool.py`): preserve `url` explicitly as `source_url` in the structure the agent consumes (today it survives only in truncated prose); best-effort `oem_manufacturer` — if the prose does not clearly name a manufacturer, leave it empty rather than forcing a fragile parse.
- **`cost_calculator_tool.py`**: pass `oem_manufacturer` and `source_url` straight through from input line to output row (today the calculator drops all provenance) — no calculation, just carry.
- **Costing prompt** (`costing_agent.py`): instruct the agent to populate `oem_manufacturer` (best-effort) and `source_url` (required) on each web-priced line.
- **`xlsx_generator_tool.py`**: add **"OEM / Manufacturer"** and **"Web Source"** columns (the latter rendered as a clickable Excel hyperlink) to the relevant layouts. Empty for non-web-sourced lines — no clutter.

---

## 7. Error handling

- **Gate is fail-safe, never fail-open.** Any exception inside `_reconcile_schedules` → treat as `no_anchor` (don't block), log a warning with `run_id`. A bug in the gate can never drop a tender's costing.
- **Re-extraction is bounded** — exactly one retry, hard-capped; a pathological NIT cannot loop or blow the token budget. Timeout inherits the existing extraction timeout.
- **`no_anchor` is not a failure** — NITs that print no schedule total pass through unflagged, labeled "not verifiable" (honest, unchanged behavior otherwise).
- **Piece 3 columns degrade gracefully** — missing `oem_manufacturer` / `source_url` render as empty cells; nothing breaks if the agent omits them.

---

## 8. Testing

Each piece ships only when its gate is green.

- **Piece 1** — unit tests on `_reconcile_schedules` using the **real DB numbers**: `t=2723/sch=B` and `t=2733/sch=C` → `reconciled` (0.0%); `t=2693/sch=A` (11×) and `t=2732/sch=D` (2×) → `reconciliation_failed` with correct `suspected_multiple`; totals-absent → `no_anchor`. Pure-function, fast, no LLM. Plus a merged-row extraction fixture from the ECOR Mancheswar NIT asserting one row per logical item.
- **Piece 2** — a failed reconciliation surfaces the ⚠ badge in the strategic summary; a clean one asserts `✓`.
- **Piece 3** — the calculator passes `source_url` / `oem_manufacturer` through; the xlsx writes a clickable hyperlink cell.
- **Regression** — existing `tests/test_costing_*.py` (including `test_costing_dedup.py`) stay green.

---

## 9. Verification before "done" & rollout

- **Verification (verification-before-completion discipline):** each piece proves itself by running its tests AND re-running the read-only reconciliation probe against the live DB after a real extraction, showing previously-mismatched tenders now reconcile.
- **Rollout:** Piece 1 behind `costing_reconcile_*` config knobs (reconcile default ON; `reextract` toggleable) so it can be dialed back via env without a deploy — same pattern as `tender_analyzer_*`. Three separate PRs in dependency order; each merges green before the next starts.

---

## 10. Sample / reference data

- Sample NIT: `F:\Projects\01. DRPL\Data for training\ECOR - Mancheswar - Non Core Work - NIT - Opnd on 18-03-2026.pdf`
- Live-DB reconciliation evidence: tenders 2693, 2723, 2732, 2733 (see §1).
- Related design: `docs/superpowers/plans/2026-07-13-costing-dedup.md` (content-dedup — complementary, not a substitute).
