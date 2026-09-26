# NIT-Faithful Costing — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan
**Scope:** Piece 1 of 3. Makes the tender's *published* schedule numbers exact and
trustworthy. Labour-wage sourcing (piece 2) and OEM/source links (piece 3) are
explicitly out of scope here and get their own specs.

---

## 1. Problem

The costing agent produces a cost-breakdown Excel whose published-tender numbers
do not match the source NIT. Verified against a real case — **Tender #2531**,
NIT `PR-C-RMPU-26-27-792` (CR Parel, RMPU SS2/SS3), Advertised Value
**₹60,879,392.16**:

| Schedule | NIT stated total | Agent Excel | Error |
|---|---|---|---|
| A – Repair & Maintenance | 5,975,127.60 | 6,024,920.34 | +49,792.74 |
| B – List of spares | 53,693,176.56 | 153,775,506.91 | **+100,082,330 (≈2.9×)** |
| C – Misc Works | 1,211,088.00 | 1,211,088.00 | exact |
| **Grand (= Advertised Value)** | **60,879,392.16** | **161,011,515.25** | **+100M (2.6×)** |

### Root causes (code-level, with anchors)

1. **Duplicated line rows inflate every schedule total.**
   `boq_parser_service.py` runs three extraction passes (pdfplumber
   `_extract_with_pdfplumber` ~`:451`, chunked AI `_extract_doc_complete` ~`:1136`,
   vision fallback ~`:1213`) and **unions** them (`:1176-1179`, repeated at
   `:1204`, `:1228`). Dedup key `_boq_row_key()` (`:702-718`) uses
   `(schedule, item_code)` only when `item_code` matches `^[A-Z]{1,3}\d+$`, else
   falls back to `(schedule, sr_no, description[:40])`, and **ignores quantity**.
   So the same physical line extracted once *with* a code and once *code-less*
   (or with a drifted sr_no / long description) gets two different keys and
   **both survive**. This is the "qty 480 vs qty 4, with/without code" twin.
   Proof it is the inflation cause: Schedule A's exact +49,792.74 overage equals
   the two phantom qty-1 duplicate rows (42,398.06 + 7,394.68).

2. **No reconciliation against the NIT's own totals.**
   The NIT prints each schedule's total under its name and the Advertised Value
   in the header — and they sum exactly. The parser *reads* the printed sub-total
   (`_extract_schedule_subtotals` `:1064`) only as a transient completeness
   signal in `_reconcile_schedules` (`:1086`); it is **never persisted**, and the
   **Advertised Value is never captured at all**. Totals are computed three
   different, unreconciled ways: `_compute_totals` (`cost_breakdown_service.py:111`,
   excludes `needs_input`), `build_strategic_summary` (`:921`), and Excel `=SUM`
   formulas (`xlsx_generator_tool.py:658/1457/1558`). Nothing asserts
   `sum(lines) == stated schedule total == advertised value`.

3. **The pipeline is allowed to fabricate line items.**
   The no-BOQ / single-call freeform branch *orders* fabrication: "produce at
   least 5 line items", AMC playbook (`enhanced_costing_agent.py:1435-1509`),
   component-expansion (`:477-513`), `derived_estimate` +
   escalation multipliers (`costing_agent.py:43,54-56,187`;
   `enhanced_costing_agent.py:454`). `validate_nit_mirror`
   (`cost_breakdown_service.py:409`) only *warns*; `persist_from_agent_output`
   (`:1095-1108`) saves invented rows anyway, and the batched path skips
   validation entirely. This produced the phantom "Robotic AC duct cleaning
   ₹850,000", the "GST mandate form" line, the "future-year escalation 75/100%"
   multipliers, and item 45a (Differential pressure switch, actually the last
   line of Schedule B) mis-assigned to Schedule C.

Note: the Excel amount columns are **live formulas** (`=E4*F4`, schedule total
`=SUM(G4:G7)`), not blank — so once duplicates are removed and reconciliation is
enforced, the totals self-correct. The guarantee comes from the reconciliation
gate, not from the formulas.

---

## 2. Approved decisions

1. **Transcribe the NIT verbatim, cost separately.** The tender's
   qty/rate/amount/schedule-totals are captured once and locked; the LLM's
   independent cost estimate lives in separate columns for margin. Never mixed.
2. **Hard-fail & flag on mismatch, never fabricate.** If captured lines don't
   reconcile to the NIT's printed schedule total (beyond ₹1 rounding), the run is
   flagged for review with the exact discrepancy. No balancing/phantom line is
   inserted.
3. **Deterministic-first extraction + reconciliation gate.** Parse the structured
   IREPS schedule table deterministically; AI/vision only as a gated fallback.
4. **Ship this piece alone**, verified end-to-end on Tender #2531, before pieces
   2 and 3.

---

## 3. Design

### 3.1 Two number-worlds

```
NIT PDF ──(deterministic parser)──▶ LOCKED tender lines ──┐
                                    + schedule stated totals ├─▶ Reconciliation gate ─▶ PASS ─▶ Excel
                                    + Advertised Value        │      (hard-flag mismatch)   FLAG ─▶ needs_review
LLM cost estimator ──▶ firm est-cost columns ONLY ────────────┘      (never fabricates)
```

The tender numbers are immutable after capture. The LLM is scoped to estimating
the firm's own cost per already-captured line.

### 3.2 NIT capture (`boq_parser_service` rework)

- Deterministic parse of the IREPS schedule structure (regular columns +
  `Description:-` continuation). Captures per line: `schedule_name, s_no,
  item_code, description, qty, unit, unit_rate, basic_value, amount, escl_pct,
  bidding_unit`.
- **Newly persisted:** per-schedule `stated_total` (printed under the schedule
  name) and tender-level `advertised_value` (NIT header). These need storage on
  the schedule/breakdown model (or the tender), currently absent.
- **Strict dedup:** exactly one row per `(schedule, s_no, item_code)` with
  `item_code` normalized (blank and word-form codes canonicalized) so a line
  cannot survive twice. Quantity is never part of identity.
- **AI + vision demoted:** run only when the deterministic parse fails to read a
  schedule; their output still passes through the same dedup + reconciliation
  gate.

### 3.3 Reconciliation gate

- Per schedule: assert `sum(line amount) == stated_total` within ±₹1.
- Tender: assert `sum(stated_total) == advertised_value` within ±₹1.
- On any failure: mark the breakdown `needs_review`, attach a structured
  discrepancy report (per-schedule delta + the specific lines that don't add up),
  surface it in the run output and in a new **"Reconciliation" block** in the
  Excel. Do not fabricate.

### 3.4 Data model

- **Tender (locked, NIT-verbatim):** `item_code, description, qty, unit,
  tender_unit_rate, tender_amount, schedule_stated_total, advertised_value`.
- **Firm estimate (LLM-owned):** `est_unit_cost, est_total_cost, margin_amount,
  margin_pct`.
- Fix `xlsx_generator` NIT-mirror bug: the tender "Unit Rate" column must read
  the **NIT rate**, not the firm `rate` (`xlsx_generator_tool.py:512`,
  amount formula `:635-638`).

### 3.5 Fabrication lockdown

- When a schedule/BOQ exists, **disable** the "≥5 line items" / AMC-playbook /
  component-expansion freeform branches. The line set is fixed to the captured
  NIT lines; the LLM may only attach a cost to an existing line, never create one.
- Make `validate_nit_mirror` **authoritative**: lines not matching a captured NIT
  line are quarantined (not persisted into the schedule); the batched path must
  run it too.
- Ban **invented/agent-derived** escalation multipliers (the hallucinated
  "future-year escalation 75/100%"). The NIT's OWN `Escl.(%)` column is
  legitimate and part of the published figure: Tender Amount reproduces the
  NIT's printed Amount exactly = `qty × tender_rate × (1 + NIT_escl%)`, so
  "exact NIT match" holds even for escalated tenders. (Decision 2026-07-12:
  the earlier "remove escalation from tender numbers" wording meant *never
  fabricate* escalation, not strip the NIT's own column.)

### 3.6 Excel output

Keep live formulas; source the tender "Unit Rate" from the locked NIT rate; add
the reconciliation block; duplicates are gone upstream so no row-level change is
needed beyond column sourcing.

---

## 4. Verification (definition of done)

Run costing on **Tender #2531** and assert exactly:

| Schedule | Must equal |
|---|---|
| A | 5,975,127.60 |
| B | 53,693,176.56 |
| C | 1,211,088.00 |
| **Grand** | **60,879,392.16** |

Plus:
- Exactly one row per NIT line (no duplicates) — Schedule A has 2 lines,
  Schedule C has 1 line.
- Zero invented lines (no "Robotic cleaning ₹850K" phantom, no "GST mandate
  form", no escalation multipliers on tender numbers).
- Every tender line's `qty × tender_unit_rate == amount` equals the NIT's printed
  amount.
- The reconciliation gate reports PASS for this tender.

Regression: pick one non-IREPS / no-schedule tender and confirm the gated AI
fallback still produces a breakdown (flagged, not fabricated) without crashing.

---

## 5. Explicitly out of scope

- Labour costing from Indian government minimum-wage documents (piece 2).
- OEM / IndiaMART / website source links per costed part (piece 3).
- Any change to the firm's *cost-estimation* quality beyond isolating it from the
  tender numbers. The firm estimate can stay as-is; this piece only guarantees the
  published tender numbers are exact.
