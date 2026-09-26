# NIT-Faithful Costing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the costing agent transcribe an IREPS NIT's published schedule numbers exactly (every qty/rate/amount, each schedule total, and the advertised value), de-duplicate line rows, and hard-reconcile the result — flagging any mismatch instead of fabricating numbers.

**Architecture:** Add a deterministic IREPS schedule parser and a pure reconciliation gate as new, independently-testable modules. Wire the parser in as the authoritative first pass in `boq_parser_service` (demoting the AI/vision passes to gated fallback), persist the newly-captured schedule totals + advertised value, run the reconciliation gate in the costing flow (marking `needs_review` on mismatch), split the tender vs firm number columns, and disable the line-fabrication branches whenever a real schedule exists.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy, PyMuPDF (`fitz`) + pdfplumber for PDF text, pytest, Alembic. LangChain/LangGraph costing graph (`enhanced_costing_agent.py`).

## Global Constraints

- Verified ground-truth case: **Tender #2531**, NIT `PR-C-RMPU-26-27-792`, Advertised Value **₹60,879,392.16**; Schedule A **5,975,127.60**, B **53,693,176.56**, C **1,211,088.00**. All assertions use these exact values.
- Reconciliation tolerance: **±₹1.00** (rounding only). Never insert a balancing/phantom line.
- The LLM must never write tender columns (`item_code, description, qty, unit, tender_unit_rate, tender_amount, schedule_stated_total, advertised_value`). It may only write firm-estimate columns (`est_unit_cost, est_total_cost, margin_amount, margin_pct`).
- Follow existing repo patterns: schema changes need an Alembic revision **and** a self-healing entry in `app/main.py` (`_apply_schema_drift_fixes` for Postgres, `_add_missing_columns` for SQLite) per CLAUDE.md.
- Test PDF path (local): `C:\Users\anike\Downloads\command_center_256_1783409696_CR - Parel - SS2 & SS3 Work - NIT - Opnd on 20.07.2026 (2).pdf`. Copy it into `drpl-backend/tests/fixtures/nit_parel_2531.pdf` in Task 1 so tests are portable.
- Run backend commands with the venv: `drpl-backend/venv/Scripts/python.exe`. When verifying against the live server, restart it **without** relying on `--reload` (its watcher is unreliable on this Windows host).

---

## File Structure

**New files**
- `app/services/costing/nit_schedule_parser.py` — pure deterministic IREPS parser. Text → `ParsedNIT` (schedules, lines, per-schedule stated totals, advertised value). No DB, no LLM.
- `app/services/costing/nit_reconciliation.py` — pure reconciliation gate. `ParsedNIT` (or persisted rows) → `ReconciliationReport` (per-schedule pass/fail + deltas).
- `tests/fixtures/nit_parel_2531.pdf` — golden NIT fixture.
- `tests/services/costing/test_nit_schedule_parser.py`
- `tests/services/costing/test_nit_reconciliation.py`
- `tests/services/costing/test_boq_parser_integration.py`
- `alembic/versions/<rev>_boq_schedule_totals.py` — migration for the new columns/table.

**Modified files**
- `app/services/boq_parser_service.py` — deterministic-first ordering; stronger dedup; persist stated totals + advertised value.
- `app/models/costing_template.py` — add `BOQScheduleTotal` (or JSON column) + advertised-value storage.
- `app/main.py` — self-healing column adds.
- `app/services/cost_breakdown_service.py` — reconciliation gate call; `needs_review`; authoritative `validate_nit_mirror`; tender/firm column split.
- `app/services/langchain/graphs/enhanced_costing_agent.py` — disable fabrication branches when a schedule exists.
- `app/services/langchain/tools/xlsx_generator_tool.py` — tender "Unit Rate" reads NIT rate; add reconciliation block.

---

## Task 1: Deterministic IREPS schedule parser

**Files:**
- Create: `app/services/costing/__init__.py` (empty), `app/services/costing/nit_schedule_parser.py`
- Create: `tests/fixtures/nit_parel_2531.pdf` (copy of the local NIT)
- Test: `tests/services/costing/test_nit_schedule_parser.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) NITLine`: `schedule_code: str, sr_no: int, item_code: str, description: str, qty: float | None, unit: str | None, unit_rate: float | None, basic_value: float | None, escalation_pct: float, amount: float | None, bidding_unit: str | None`
  - `@dataclass(frozen=True) NITSchedule`: `code: str, title: str, stated_total: float | None, lines: tuple[NITLine, ...]`
  - `@dataclass(frozen=True) ParsedNIT`: `advertised_value: float | None, schedules: tuple[NITSchedule, ...]`
  - `def parse_nit_text(text: str) -> ParsedNIT` — pure, deterministic.
  - `def is_ireps_schedule_format(text: str) -> bool` — cheap gate: True when the text contains a `Schedule ()` banner and the `S.No./Item/Code/Item Qty` header block.

- [ ] **Step 1: Copy the fixture**

```bash
mkdir -p drpl-backend/tests/fixtures drpl-backend/tests/services/costing
cp "/c/Users/anike/Downloads/command_center_256_1783409696_CR - Parel - SS2 & SS3 Work - NIT - Opnd on 20.07.2026 (2).pdf" drpl-backend/tests/fixtures/nit_parel_2531.pdf
touch drpl-backend/app/services/costing/__init__.py drpl-backend/tests/services/costing/__init__.py
```

- [ ] **Step 2: Write the failing test**

The NIT text (via PyMuPDF `get_text()`) is a newline-separated token stream. A schedule banner line is `Schedule () <CODE>-<title>` immediately followed by a numeric line = the schedule's stated total. Each line item is the token run `sr_no, item_code, qty, unit, unit_rate, basic_value, <escl-or-"AT Par">, amount` followed by one or more `Description:- …` lines. Advertised value is the numeric line right after a line equal to `Advertised Value`.

```python
# tests/services/costing/test_nit_schedule_parser.py
import fitz  # PyMuPDF
from pathlib import Path
from app.services.costing.nit_schedule_parser import parse_nit_text, is_ireps_schedule_format

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "nit_parel_2531.pdf"

def _fixture_text() -> str:
    doc = fitz.open(str(FIXTURE))
    return "\n".join(pg.get_text() for pg in doc)

def test_detects_ireps_format():
    assert is_ireps_schedule_format(_fixture_text()) is True

def test_parses_advertised_value():
    parsed = parse_nit_text(_fixture_text())
    assert parsed.advertised_value == 60879392.16

def test_parses_three_schedules_with_stated_totals():
    parsed = parse_nit_text(_fixture_text())
    by_code = {s.code: s for s in parsed.schedules}
    assert set(by_code) == {"A", "B", "C"}
    assert by_code["A"].stated_total == 5975127.60
    assert by_code["B"].stated_total == 53693176.56
    assert by_code["C"].stated_total == 1211088.00

def test_schedule_a_has_exactly_two_lines_no_duplicates():
    parsed = parse_nit_text(_fixture_text())
    a = next(s for s in parsed.schedules if s.code == "A")
    assert len(a.lines) == 2
    line1 = a.lines[0]
    assert line1.sr_no == 1 and line1.item_code == "1"
    assert line1.qty == 120.0 and line1.unit == "Set"
    assert line1.unit_rate == 42398.05 and line1.amount == 5087766.00
    assert "POH maintenance" in line1.description

def test_schedule_c_single_line():
    parsed = parse_nit_text(_fixture_text())
    c = next(s for s in parsed.schedules if s.code == "C")
    assert len(c.lines) == 1
    assert c.lines[0].item_code == "a" and c.lines[0].qty == 120.0
    assert c.lines[0].amount == 1211088.00

def test_line_amounts_sum_to_stated_total_per_schedule():
    parsed = parse_nit_text(_fixture_text())
    for s in parsed.schedules:
        line_sum = round(sum(l.amount for l in s.lines if l.amount is not None), 2)
        assert abs(line_sum - s.stated_total) <= 1.0, f"{s.code}: {line_sum} vs {s.stated_total}"

def test_no_prose_lines_captured():
    parsed = parse_nit_text(_fixture_text())
    all_desc = " ".join(l.description for s in parsed.schedules for l in s.lines).lower()
    assert "i/we the tenderer" not in all_desc
    assert "eligibility" not in all_desc
    assert "gst mandate" not in all_desc
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/costing/test_nit_schedule_parser.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.costing.nit_schedule_parser`.

- [ ] **Step 4: Implement the parser**

```python
# app/services/costing/nit_schedule_parser.py
"""Deterministic parser for IREPS NIT bidding schedules.

The IREPS "2. SCHEDULE" section is a rigid token stream. This parser reads it
verbatim with zero LLM involvement so the tender's published numbers are
captured exactly. It is intentionally format-specific to IREPS; callers gate on
`is_ireps_schedule_format` and fall back to the AI/vision path otherwise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A schedule banner: "Schedule () A-PART 'A':- Repair…" or "Schedule () B7-…".
# Tolerates the IREPS "1000 more rows at the bottom" export artifact by matching
# only up to the code + trailing title text on the SAME logical line.
_BANNER_RE = re.compile(r"Schedule\s*\(\)\s*([A-Z]\d*)\s*-\s*(.*)")
_SECTION_END_RE = re.compile(
    r"^\s*(3\.\s*ITEM\s*BREAKUP|No item break up added|4\.\s*ELIGIBILITY|"
    r"Special (Financial|Technical) Criteria|5\.\s*COMPLIANCE|General Instruction)",
    re.IGNORECASE,
)
_NUM_RE = re.compile(r"^-?\d[\d,]*\.?\d*$")
_INT_RE = re.compile(r"^\d+$")


def _num(tok: str) -> float | None:
    t = (tok or "").replace(",", "").strip()
    if not _NUM_RE.match(tok.strip()):
        return None
    try:
        return float(t)
    except ValueError:
        return None


@dataclass(frozen=True)
class NITLine:
    schedule_code: str
    sr_no: int
    item_code: str
    description: str
    qty: float | None
    unit: str | None
    unit_rate: float | None
    basic_value: float | None
    escalation_pct: float
    amount: float | None
    bidding_unit: str | None


@dataclass(frozen=True)
class NITSchedule:
    code: str
    title: str
    stated_total: float | None
    lines: tuple[NITLine, ...]


@dataclass(frozen=True)
class ParsedNIT:
    advertised_value: float | None
    schedules: tuple[NITSchedule, ...]


def is_ireps_schedule_format(text: str) -> bool:
    return bool(_BANNER_RE.search(text or "")) and "Item Qty" in (text or "")


def parse_nit_text(text: str) -> ParsedNIT:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    advertised_value = _find_advertised_value(lines)

    # Split the stream into schedule blocks at each banner; stop at section end.
    blocks: list[tuple[str, str, float | None, list[str]]] = []
    i = 0
    n = len(lines)
    current: tuple[str, str, float | None] | None = None
    body: list[str] = []
    while i < n:
        ln = lines[i]
        if _SECTION_END_RE.match(ln) and current is not None:
            blocks.append((*current, body))
            current, body = None, []
            break
        m = _BANNER_RE.search(ln)
        if m:
            if current is not None:
                blocks.append((*current, body))
            code = m.group(1).upper()
            title = m.group(2).strip()
            # Stated total is the next pure-numeric line after the banner.
            stated = None
            j = i + 1
            while j < n and lines[j] == "":
                j += 1
            if j < n and _NUM_RE.match(lines[j]):
                stated = _num(lines[j])
                i = j
            current, body = (code, title, stated), []
            i += 1
            continue
        if current is not None:
            body.append(ln)
        i += 1
    if current is not None:
        blocks.append((*current, body))

    schedules = tuple(
        NITSchedule(code=code, title=title, stated_total=stated,
                    lines=tuple(_parse_lines(code, body)))
        for (code, title, stated, body) in blocks
    )
    return ParsedNIT(advertised_value=advertised_value, schedules=schedules)


def _find_advertised_value(lines: list[str]) -> float | None:
    for idx, ln in enumerate(lines):
        if ln.lower() == "advertised value":
            for k in range(idx + 1, min(idx + 4, len(lines))):
                v = _num(lines[k])
                if v is not None:
                    return v
    return None


def _parse_lines(schedule_code: str, body: list[str]) -> list[NITLine]:
    """A row is: sr_no(int), item_code, qty(num), unit(word), unit_rate(num),
    basic_value(num), escl-or-'AT Par', amount(num), then 'Description:- …'.
    We scan for the sr_no+item_code+qty anchor and read the fixed field run."""
    out: list[NITLine] = []
    i, n = 0, len(body)
    while i < n:
        # header rows ("S.No.", "Item", "Code", …) and blanks are skipped
        if not _INT_RE.match(body[i]):
            i += 1
            continue
        # Candidate row start: sr_no, item_code, qty, unit, rate, basic, escl, amount
        if i + 7 >= n:
            break
        sr_no = int(body[i])
        item_code = body[i + 1]
        qty = _num(body[i + 2])
        unit = body[i + 3]
        unit_rate = _num(body[i + 4])
        basic_value = _num(body[i + 5])
        escl_tok = body[i + 6]
        amount = _num(body[i + 7])
        # Validate the shape: qty & rate & amount numeric, unit is a word.
        if qty is None or unit_rate is None or amount is None or _NUM_RE.match(unit):
            i += 1
            continue
        escl_pct = 0.0 if not _NUM_RE.match(escl_tok) else (_num(escl_tok) or 0.0)
        bidding_unit = escl_tok if not _NUM_RE.match(escl_tok) else "AT Par"
        # Collect the Description:- continuation lines.
        desc_parts: list[str] = []
        j = i + 8
        while j < n and not _INT_RE.match(body[j]):
            t = body[j]
            if t.startswith("Description:-"):
                desc_parts.append(t[len("Description:-"):].strip())
            elif desc_parts:
                desc_parts.append(t)
            j += 1
        out.append(NITLine(
            schedule_code=schedule_code, sr_no=sr_no, item_code=item_code.strip(),
            description=" ".join(desc_parts).strip(), qty=qty, unit=unit.strip(),
            unit_rate=unit_rate, basic_value=basic_value, escalation_pct=escl_pct,
            amount=amount, bidding_unit=bidding_unit,
        ))
        i = j
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/costing/test_nit_schedule_parser.py -v`
Expected: PASS (all 7). If `test_line_amounts_sum_to_stated_total_per_schedule` fails, print the parsed lines for the failing schedule and fix the field-run offsets before proceeding — this test is the fidelity guarantee.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/costing drpl-backend/tests/services/costing drpl-backend/tests/fixtures/nit_parel_2531.pdf
git commit -m "feat(costing): deterministic IREPS NIT schedule parser with golden test"
```

---

## Task 2: Reconciliation gate

**Files:**
- Create: `app/services/costing/nit_reconciliation.py`
- Test: `tests/services/costing/test_nit_reconciliation.py`

**Interfaces:**
- Consumes: `ParsedNIT`, `NITSchedule` from Task 1.
- Produces:
  - `@dataclass(frozen=True) ScheduleRecon`: `code: str, line_sum: float, stated_total: float | None, delta: float, ok: bool`
  - `@dataclass(frozen=True) ReconciliationReport`: `schedules: tuple[ScheduleRecon, ...], grand_line_sum: float, advertised_value: float | None, grand_delta: float, ok: bool`
  - `def reconcile(parsed: ParsedNIT, tol: float = 1.0) -> ReconciliationReport`

- [ ] **Step 1: Write the failing test**

```python
# tests/services/costing/test_nit_reconciliation.py
from app.services.costing.nit_schedule_parser import ParsedNIT, NITSchedule, NITLine
from app.services.costing.nit_reconciliation import reconcile

def _line(code, sr, amt):
    return NITLine(code, sr, str(sr), f"item {sr}", 1, "Nos", amt, amt, 0.0, amt, "AT Par")

def test_pass_when_lines_match_totals():
    sched = NITSchedule("A", "t", 300.0, (_line("A",1,100.0), _line("A",2,200.0)))
    parsed = ParsedNIT(advertised_value=300.0, schedules=(sched,))
    rep = reconcile(parsed)
    assert rep.ok is True
    assert rep.schedules[0].ok is True and rep.schedules[0].delta == 0.0

def test_flag_when_schedule_lines_dont_match_stated_total():
    # stated total 300 but lines only sum to 250 -> flag, no fabrication
    sched = NITSchedule("A", "t", 300.0, (_line("A",1,100.0), _line("A",2,150.0)))
    parsed = ParsedNIT(advertised_value=300.0, schedules=(sched,))
    rep = reconcile(parsed)
    assert rep.ok is False
    assert rep.schedules[0].ok is False
    assert abs(rep.schedules[0].delta - (-50.0)) < 1e-6

def test_flag_when_schedules_dont_sum_to_advertised_value():
    sched = NITSchedule("A", "t", 300.0, (_line("A",1,300.0),))
    parsed = ParsedNIT(advertised_value=999.0, schedules=(sched,))
    rep = reconcile(parsed)
    assert rep.ok is False
    assert abs(rep.grand_delta - (300.0 - 999.0)) < 1e-6

def test_within_one_rupee_tolerance_passes():
    sched = NITSchedule("A", "t", 300.0, (_line("A",1,299.4),))
    parsed = ParsedNIT(advertised_value=300.0, schedules=(sched,))
    assert reconcile(parsed).ok is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/costing/test_nit_reconciliation.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

```python
# app/services/costing/nit_reconciliation.py
"""Pure reconciliation of parsed NIT schedules against the tender's own printed
totals. Never mutates data and never fabricates a balancing line — it only
reports PASS/FLAG with the exact deltas."""
from __future__ import annotations

from dataclasses import dataclass

from app.services.costing.nit_schedule_parser import ParsedNIT


@dataclass(frozen=True)
class ScheduleRecon:
    code: str
    line_sum: float
    stated_total: float | None
    delta: float          # line_sum - stated_total (0.0 when stated_total is None)
    ok: bool


@dataclass(frozen=True)
class ReconciliationReport:
    schedules: tuple[ScheduleRecon, ...]
    grand_line_sum: float
    advertised_value: float | None
    grand_delta: float    # grand_line_sum - advertised_value
    ok: bool


def reconcile(parsed: ParsedNIT, tol: float = 1.0) -> ReconciliationReport:
    sched_recons: list[ScheduleRecon] = []
    grand = 0.0
    all_ok = True
    for s in parsed.schedules:
        line_sum = round(sum(l.amount for l in s.lines if l.amount is not None), 2)
        grand += line_sum
        if s.stated_total is None:
            delta, ok = 0.0, True
        else:
            delta = round(line_sum - s.stated_total, 2)
            ok = abs(delta) <= tol
        all_ok = all_ok and ok
        sched_recons.append(ScheduleRecon(s.code, line_sum, s.stated_total, delta, ok))
    grand = round(grand, 2)
    if parsed.advertised_value is None:
        grand_delta, grand_ok = 0.0, True
    else:
        grand_delta = round(grand - parsed.advertised_value, 2)
        grand_ok = abs(grand_delta) <= tol
    return ReconciliationReport(
        schedules=tuple(sched_recons), grand_line_sum=grand,
        advertised_value=parsed.advertised_value, grand_delta=grand_delta,
        ok=all_ok and grand_ok,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/costing/test_nit_reconciliation.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: End-to-end fixture reconciliation (parser + gate together)**

Append to `tests/services/costing/test_nit_reconciliation.py`:

```python
import fitz
from pathlib import Path
from app.services.costing.nit_schedule_parser import parse_nit_text

def test_parel_fixture_reconciles_exactly():
    p = Path(__file__).parent.parent.parent / "fixtures" / "nit_parel_2531.pdf"
    text = "\n".join(pg.get_text() for pg in fitz.open(str(p)))
    rep = reconcile(parse_nit_text(text))
    assert rep.ok is True
    assert rep.grand_line_sum == 60879392.16
```

Run the same pytest command; expected PASS.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/costing/nit_reconciliation.py drpl-backend/tests/services/costing/test_nit_reconciliation.py
git commit -m "feat(costing): NIT reconciliation gate (pure, no fabrication)"
```

---

## Task 3: Persist schedule stated totals + advertised value

**Files:**
- Modify: `app/models/costing_template.py` (add `BOQScheduleTotal` model + `BOQItem` already stores per-line fields)
- Create: `alembic/versions/<rev>_boq_schedule_totals.py`
- Modify: `app/main.py` (`_add_missing_columns` / `_apply_schema_drift_fixes`) to self-heal
- Test: `tests/services/costing/test_boq_schedule_totals_model.py`

**Interfaces:**
- Produces ORM `BOQScheduleTotal`: `id, tender_id: int, schedule_code: str, stated_total: float | None, advertised_value: float | None, created_at`. One row per (tender, schedule). `advertised_value` is repeated per row (tender-level) for simple joins; the reconciliation reader uses the max/first.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/costing/test_boq_schedule_totals_model.py
from app.models.costing_template import BOQScheduleTotal

def test_boq_schedule_total_fields():
    row = BOQScheduleTotal(tender_id=1, schedule_code="A",
                           stated_total=5975127.60, advertised_value=60879392.16)
    assert row.schedule_code == "A"
    assert row.stated_total == 5975127.60
    assert row.advertised_value == 60879392.16
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/costing/test_boq_schedule_totals_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'BOQScheduleTotal'`.

- [ ] **Step 3: Add the model**

Append to `app/models/costing_template.py` (after `BOQItem`):

```python
class BOQScheduleTotal(Base):
    """Per-schedule stated total + tender advertised value, captured verbatim
    from the NIT header/banners. Used by the reconciliation gate as the
    immutable benchmark for sum(line amounts)."""
    __tablename__ = "boq_schedule_totals"

    id = Column(Integer, primary_key=True, index=True)
    tender_id = Column(Integer, nullable=False, index=True)
    schedule_code = Column(String(64), nullable=False)
    stated_total = Column(Float, nullable=True)
    advertised_value = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
```

- [ ] **Step 4: Run the model test**

Run: same pytest command. Expected: PASS. (`Base.metadata.create_all` in the test DB fixture creates the new table; confirm the SQLite test harness picks it up.)

- [ ] **Step 5: Alembic revision + self-heal**

```bash
cd drpl-backend && venv/Scripts/python.exe -m alembic revision -m "boq_schedule_totals"
```

Fill the generated file's `upgrade()`:

```python
def upgrade():
    op.create_table(
        "boq_schedule_totals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tender_id", sa.Integer(), nullable=False, index=True),
        sa.Column("schedule_code", sa.String(64), nullable=False),
        sa.Column("stated_total", sa.Float(), nullable=True),
        sa.Column("advertised_value", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )

def downgrade():
    op.drop_table("boq_schedule_totals")
```

`create_all` in `main.py` already creates new tables on existing deploys, so no `_add_missing_columns` entry is needed for a brand-new table (add one only if a later task adds a column to an existing table). Note this in the commit message.

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/models/costing_template.py drpl-backend/alembic/versions drpl-backend/tests/services/costing/test_boq_schedule_totals_model.py
git commit -m "feat(costing): persist per-schedule stated totals + advertised value"
```

---

## Task 4: Deterministic-first wiring + strong dedup in boq_parser_service

**Files:**
- Modify: `app/services/boq_parser_service.py` — `parse_boq_from_tender()` (~`:109`), `_boq_row_key()` (`:702-718`), and the extraction ordering (`_extract_doc_complete` ~`:1136`, union sites `:1176-1229`).
- Test: `tests/services/costing/test_boq_parser_integration.py`

**Interfaces:**
- Consumes: `parse_nit_text`, `is_ireps_schedule_format` (Task 1); persists `BOQScheduleTotal` (Task 3).
- Produces: `BOQItem` rows with `estimated_rate` = NIT `unit_rate`, `basic_value` = NIT `basic_value`, exactly one row per NIT line; `BOQScheduleTotal` rows for the tender.

- [ ] **Step 1: Read the target functions first**

Before editing, read `parse_boq_from_tender` (`app/services/boq_parser_service.py:109`), `_extract_doc_complete` (`~:1136`), and the union/persist tail (`:1176-1260`). Identify (a) where per-document text is available, (b) where `BOQItem` rows are written. The deterministic pass must run **before** the AI/vision passes and short-circuit them when it succeeds.

- [ ] **Step 2: Write the failing integration test**

```python
# tests/services/costing/test_boq_parser_integration.py
import fitz
from pathlib import Path
from app.services.boq_parser_service import build_boq_items_from_text  # new helper

FIXTURE = Path(__file__).parent.parent.parent / "fixtures" / "nit_parel_2531.pdf"

def _text():
    return "\n".join(pg.get_text() for pg in fitz.open(str(FIXTURE)))

def test_deterministic_pass_yields_one_row_per_line_no_dupes():
    items, sched_totals = build_boq_items_from_text(_text())
    # Schedule A: 2 lines, C: 1 line; B: many. No duplicates: (schedule, sr_no,
    # item_code) is unique across the whole set.
    keys = [(it["schedule_name"], it["sr_no"], it["item_code"]) for it in items]
    assert len(keys) == len(set(keys)), "duplicate BOQ rows present"
    a = [it for it in items if it["schedule_name"] == "A"]
    assert len(a) == 2
    # estimated_rate carries the NIT unit rate verbatim
    assert any(abs(it["estimated_rate"] - 42398.05) < 1e-6 for it in a)

def test_schedule_totals_captured():
    _items, sched_totals = build_boq_items_from_text(_text())
    by = {s["schedule_code"]: s for s in sched_totals}
    assert by["A"]["stated_total"] == 5975127.60
    assert by["B"]["stated_total"] == 53693176.56
    assert by["A"]["advertised_value"] == 60879392.16
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/costing/test_boq_parser_integration.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_boq_items_from_text'`.

- [ ] **Step 4: Add the deterministic adapter + strengthen dedup**

Add to `app/services/boq_parser_service.py`:

```python
from app.services.costing.nit_schedule_parser import parse_nit_text, is_ireps_schedule_format

def build_boq_items_from_text(text: str):
    """Deterministic-first BOQ extraction for IREPS-format NITs. Returns
    (boq_item_dicts, schedule_total_dicts). Empty lists when the text is not the
    IREPS schedule format (caller then falls back to the AI/vision path)."""
    if not is_ireps_schedule_format(text):
        return [], []
    parsed = parse_nit_text(text)
    items: list[dict] = []
    for s in parsed.schedules:
        for l in s.lines:
            items.append({
                "sr_no": l.sr_no,
                "item_code": l.item_code,
                "description": l.description,
                "quantity": l.qty,
                "unit": l.unit,
                "estimated_rate": l.unit_rate,      # tender rate, verbatim
                "basic_value": l.basic_value,
                "escalation_pct": l.escalation_pct,
                "amount": l.amount,
                "bidding_unit": l.bidding_unit,
                "schedule_name": l.schedule_code,
                "is_tax_line": False,
                "extraction_confidence": "high",
            })
    sched_totals = [
        {"schedule_code": s.code, "stated_total": s.stated_total,
         "advertised_value": parsed.advertised_value}
        for s in parsed.schedules
    ]
    return items, sched_totals
```

Then change `_boq_row_key` so the deterministic (schedule, sr_no, item_code) triple is always the identity when present (kills the coded/code-less twin):

```python
def _boq_row_key(it: dict) -> tuple:
    sched = (it.get("schedule_name") or "").strip().upper()
    code = re.sub(r"\s+", " ", (it.get("item_code") or "").strip()).upper()
    sr = str(it.get("sr_no") or "")
    # Deterministic identity: schedule + serial + item code. sr_no+code is unique
    # per NIT line (sub-codes a/b share sr_no but differ by code), so this never
    # collapses distinct lines and never keeps a duplicate of the same line.
    if sr and code:
        return (sched, sr, code)
    if code and re.match(r"^[A-Z]{1,3}\d+$", re.sub(r"[^A-Za-z0-9]", "", code)):
        return (sched, code)
    return (sched, sr, (it.get("description") or "").strip()[:40])
```

- [ ] **Step 5: Wire deterministic-first into `parse_boq_from_tender`**

In `parse_boq_from_tender` (`:109`), for each tender document, extract text (the service already has `_load_pdf_pages` / advanced parser), join it, and call `build_boq_items_from_text`. If it returns non-empty items, **use them as the authoritative set and skip the AI + vision union** for that document; persist the returned `schedule_total` dicts into `BOQScheduleTotal` (dedup on `(tender_id, schedule_code)`). Only when it returns `[]` (non-IREPS) run the existing AI/vision passes. Keep the final `_dedup_boq_rows` call as a safety net. Add a `logger.info("[boq_parser] deterministic IREPS parse: %d rows, %d schedules", …)` line.

- [ ] **Step 6: Run integration test + full parser suite**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/costing/ -v`
Expected: PASS across all costing tests.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/boq_parser_service.py drpl-backend/tests/services/costing/test_boq_parser_integration.py
git commit -m "feat(costing): deterministic-first BOQ extraction + strict (schedule,sr_no,code) dedup"
```

---

## Task 5: Reconciliation gate in the costing flow + needs_review

**Files:**
- Modify: `app/services/cost_breakdown_service.py` — after breakdown build, load `BOQScheduleTotal` rows, run `reconcile`, set `needs_review` + attach report. Anchor points: `recompute_breakdown_totals` (`:1011`), `persist_from_agent_output` (`:1095-1108`), `build_strategic_summary` (`:906`).
- Test: `tests/services/costing/test_reconciliation_flow.py`

**Interfaces:**
- Consumes: `reconcile` (Task 2), `BOQScheduleTotal` (Task 3).
- Produces: on the persisted `CostBreakdown`, a `reconciliation` JSON (`{ok, schedules:[{code,line_sum,stated_total,delta,ok}], grand_line_sum, advertised_value, grand_delta}`) and a `needs_review: bool` flag mirrored into the return payload.

- [ ] **Step 1: Read the target + write the failing test**

Read `persist_from_agent_output` and the breakdown return-payload builder (`~:1600-1640`). Write a test that builds a breakdown for a tender whose `BOQScheduleTotal` rows are seeded, with line amounts that (a) match → `needs_review False`, (b) are short by ₹50 → `needs_review True` and the delta reported. Use the service's existing test-DB fixture pattern (mirror an existing `tests/test_*cost*` test's setup).

```python
def test_reconciliation_flags_shortfall(seeded_tender_with_schedule_totals):
    # lines sum to 250 in schedule A but stated_total is 300
    result = build_and_reconcile(db, tender_id)
    assert result["needs_review"] is True
    sched = next(s for s in result["reconciliation"]["schedules"] if s["code"] == "A")
    assert sched["ok"] is False and abs(sched["delta"] + 50.0) < 1e-6
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd drpl-backend && venv/Scripts/python.exe -m pytest tests/services/costing/test_reconciliation_flow.py -v`
Expected: FAIL — `build_and_reconcile` / reconciliation field missing.

- [ ] **Step 3: Implement the gate**

In `cost_breakdown_service.py`, add a helper that loads the tender's `BOQScheduleTotal` rows, builds a `ParsedNIT`-shaped object from the persisted `CostBreakdownLine`s (group by `schedule_name`, sum `tender_amount`/`amount`), calls `reconcile`, and writes `reconciliation` + `needs_review` onto the breakdown and the return payload. Call it at the end of `recompute_breakdown_totals`. Never mutate line amounts to force a match.

- [ ] **Step 4: Run to verify it passes**

Run: same pytest command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add drpl-backend/app/services/cost_breakdown_service.py drpl-backend/tests/services/costing/test_reconciliation_flow.py
git commit -m "feat(costing): reconciliation gate sets needs_review, never fabricates"
```

---

## Task 6: Tender/firm column split + xlsx tender-rate fix

**Files:**
- Modify: `app/services/langchain/tools/xlsx_generator_tool.py` — NIT-mirror "Unit Rate" mapping (`:512`), amount formula (`:635-638`), add reconciliation block.
- Modify: `app/services/cost_breakdown_service.py` — ensure `tender_rate`/`tender_amount` are sourced from `BOQItem.estimated_rate`/NIT amount and never overwritten by LLM `rate`.
- Test: `tests/services/costing/test_xlsx_tender_columns.py`

**Interfaces:**
- Consumes: persisted breakdown lines with distinct `tender_rate` (NIT) vs `rate` (firm).
- Produces: an xlsx where the tender "Unit Rate"/"Tender Amount" columns equal the NIT numbers and the firm "Est." columns are separate.

- [ ] **Step 1: Read `_build_nit_mirror` (`xlsx_generator_tool.py:658-715`) and the column map (`:500-520`).** Confirm which column currently pulls `rate` vs `tender_rate`.

- [ ] **Step 2: Write the failing test** — build an xlsx from a small breakdown where `tender_rate=100`, firm `rate=80`, `qty=2`; open with openpyxl and assert the tender "Unit Rate" cell is 100 (not 80) and "Tender Amount" formula/value is 200.

- [ ] **Step 3: Run to verify it fails.** Run: `pytest tests/services/costing/test_xlsx_tender_columns.py -v`.

- [ ] **Step 4: Fix the mapping** so the tender columns read NIT `tender_rate`/`tender_amount` and add a "Reconciliation" section (per-schedule stated total vs line sum + PASS/FLAG) sourced from the breakdown's `reconciliation` field.

- [ ] **Step 5: Run to verify it passes.**

- [ ] **Step 6: Commit** `feat(costing): xlsx tender columns read NIT rates; add reconciliation block`.

---

## Task 7: Fabrication lockdown

**Files:**
- Modify: `app/services/langchain/graphs/enhanced_costing_agent.py` — the no-BOQ/freeform branch (`:1435-1509`), routing `_pick_costing_strategy` (`:2451`), component-expansion prompt (`:477-513`), escalation injection (`:454`).
- Modify: `app/services/cost_breakdown_service.py` — make `validate_nit_mirror` (`:409`) authoritative and run it on the batched path (`persist_from_agent_output` `:1095-1108`).
- Test: `tests/services/costing/test_fabrication_lockdown.py`

**Interfaces:**
- Consumes: the captured NIT line set (Task 4).
- Produces: when a schedule exists, the persisted breakdown contains **only** lines matching a captured NIT line; invented lines are dropped (quarantined into `assumptions`, not into schedule rows).

- [ ] **Step 1: Write the failing test** — given a captured schedule with 2 lines and an agent output that adds a third invented line ("Robotic AC duct cleaning"), assert the persisted breakdown has exactly 2 schedule lines and the invented line is not present (optionally recorded under `assumptions`/`quarantined`).

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** — (a) in `_pick_costing_strategy`, when `BOQItem` rows exist, never route to the freeform "produce ≥5 line items" branch; (b) in `persist_from_agent_output`, call `validate_nit_mirror` and drop lines whose `(schedule, item_code/sr_no)` is not in the captured set; (c) block **invented/agent-derived** escalation only — the NIT's OWN `Escl.(%)` stays on the tender amount (Task 6 keeps `qty × tender_rate × (1+NIT_escl%)` so it matches the NIT's printed Amount; decision 2026-07-12). Do NOT strip the NIT escalation from tender numbers.

- [ ] **Step 4: Run to verify it passes.**

- [ ] **Step 5: Commit** `feat(costing): lock line set to captured NIT; quarantine invented rows`.

---

## Task 8: End-to-end verification on Tender #2531

**Files:**
- Test: `tests/services/costing/test_end_to_end_2531.py` (integration; may be marked `@pytest.mark.slow`).

- [ ] **Step 1: Write the end-to-end assertion** — parse the fixture through `build_boq_items_from_text`, persist to a test DB, run the costing build (mock the LLM firm-cost step to return fixed est costs so the test is deterministic), and assert:

```python
def test_2531_totals_exact():
    ...
    assert scheduleA_total == 5975127.60
    assert scheduleB_total == 53693176.56
    assert scheduleC_total == 1211088.00
    assert grand_total == 60879392.16
    assert reconciliation["ok"] is True
    # one row per NIT line, zero invented lines
    assert len(scheduleA_lines) == 2 and len(scheduleC_lines) == 1
    assert not any("robotic" in l["description"].lower() and l["schedule_name"] != "C"
                   for l in all_lines)
```

- [ ] **Step 2: Run to verify it fails, then passes** as the pipeline is wired.

- [ ] **Step 3: Live smoke test** — restart the backend cleanly (no `--reload` dependence), trigger a costing run for the real Tender #2531 via the cost_breakdown route, download the xlsx, and confirm the Summary grand total reads **60,879,392.16** and the reconciliation block is PASS.

- [ ] **Step 4: Commit** `test(costing): end-to-end NIT fidelity assertions for Tender #2531`.

---

## Self-Review

- **Spec coverage:** transcribe-verbatim → Tasks 1,4,6; hard-reconcile/flag → Tasks 2,5; deterministic-first → Tasks 1,4; persist stated totals + advertised value → Task 3; dedup → Task 4; fabrication lockdown → Task 7; two number-worlds/xlsx fix → Task 6; verification on #2531 → Task 8. All spec sections covered.
- **Type consistency:** `NITLine/NITSchedule/ParsedNIT` (Task 1) consumed unchanged by Tasks 2,4; `ScheduleRecon/ReconciliationReport` (Task 2) consumed by Task 5; `BOQScheduleTotal` fields (Task 3) consumed by Tasks 4,5; `build_boq_items_from_text` signature identical in Tasks 4,8.
- **Placeholder note:** Tasks 5–8 intentionally give exact anchors + logic but defer some literal code because they edit large existing files (`cost_breakdown_service.py`, `enhanced_costing_agent.py`, `xlsx_generator_tool.py`); each of those tasks starts with a "read the target function first" step. Tasks 1–3 (the new, load-bearing units) are fully coded and golden-tested against the real NIT.
