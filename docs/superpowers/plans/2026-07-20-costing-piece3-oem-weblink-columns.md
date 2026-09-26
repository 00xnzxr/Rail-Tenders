# Costing Piece 3 — OEM Manufacturer + Web Link Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a web-priced costing line item carry its **OEM/manufacturer** (best-effort) and **web source link** (guaranteed) as first-class, structured fields — surviving end-to-end from the agent's output through persistence to two clickable/labeled Excel columns — instead of dying in the free-text `source_ref`.

**Architecture:** Additive, zero correctness risk to existing behavior. Add two nullable columns to `CostBreakdownLine` (`oem_manufacturer` String, `source_url` Text), self-heal on existing deployments (Alembic + Postgres + SQLite drift), thread the two keys through the normalized-line dict + merge-whitelist so they persist and round-trip, pass them through the cost-calculator output, add them to the agent-output→XLSX row mapping, render them as columns in the two layouts where web-priced lines appear (default single-sheet + margin/multi-sheet) — `source_url` as a clickable hyperlink — and instruct the costing agent to populate them. The web-search tool already returns `url`; no change needed there.

**Tech Stack:** Python 3, SQLAlchemy, Alembic, openpyxl, pytest.

## Global Constraints

- **Local `.env` `DATABASE_URL` is the LIVE production Neon DB.** Never run `alembic upgrade`/`downgrade` or any DB write. Author the Alembic revision file only (`alembic revision -m "..."` without `--autogenerate` writes a template with no DB connection). **Importing `app.main` triggers the startup self-heal which runs `ADD COLUMN IF NOT EXISTS` against the live DB** — adding these two nullable columns is benign (matches what the next deploy does), but be aware. Tests are pure/unit — no DB writes.
- Schema-drift discipline: a new column REQUIRES an Alembic revision AND an entry in `_apply_schema_drift_fixes()` (Postgres) AND `_add_missing_columns()` (SQLite). New Alembic revision's `down_revision = "f92bd2162176"` (the current head).
- OEM is **best-effort** (may be empty/None); the **web link is guaranteed** on web-priced lines (the agent must populate `source_url` when `rate_source="web_search"`). Both columns are nullable; empty renders as a blank cell — never break on missing values.
- Column types match existing provenance convention: `oem_manufacturer = Column(String(128))`, `source_url = Column(Text)` (mirroring `rate_source` String / `source_ref` Text at `cost_breakdown.py:166-167`).
- Render the new columns ONLY in the default single-sheet (`_COST_COLUMNS`) and margin/multi-sheet (`_MARGIN_COLUMNS`) layouts — NOT the NIT-mirror or client-annexure layouts (those mirror the tender schedule, where web-priced provenance doesn't apply and always-empty columns would widen an already-wide sheet).
- The two new columns must NOT be added to `_CURRENCY_COLS` or `_PERCENT_COLS` (`xlsx_generator_tool.py:62-71`) — they are text/link, not numeric.

---

## File Structure

- `drpl-backend/app/models/cost_breakdown.py` — add the two columns.
- `drpl-backend/app/main.py` — schema-drift self-heal (Postgres + SQLite).
- `drpl-backend/alembic/versions/` — new revision.
- `drpl-backend/app/services/cost_breakdown_service.py` — normalized-dict keys + merge whitelist.
- `drpl-backend/app/services/langchain/tools/cost_calculator_tool.py` — input model + output pass-through.
- `drpl-backend/app/services/langchain/tools/xlsx_generator_tool.py` — columns + hyperlink rendering.
- `drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py` — agent-output→row mapping.
- `drpl-backend/app/services/langchain/graphs/costing_agent.py` — prompt schema + web-source rule.
- `drpl-backend/tests/test_costing_oem_weblink.py` — new.

---

### Task 1: Add the model columns + schema-drift self-heal + Alembic

**Files:**
- Modify: `drpl-backend/app/models/cost_breakdown.py` (after line 168, `confidence`)
- Modify: `drpl-backend/app/main.py` — `_apply_schema_drift_fixes()` (after line 217) + `_add_missing_columns()` migrations list (after line 642)
- Create: `drpl-backend/alembic/versions/<rev>_cost_line_oem_source_url.py`
- Test: `drpl-backend/tests/test_costing_oem_weblink.py` (create)

**Interfaces:**
- Produces: `CostBreakdownLine.oem_manufacturer: Optional[str]`, `CostBreakdownLine.source_url: Optional[str]`. Consumed by Tasks 2, 3, 4.

- [ ] **Step 1: Write the failing test**

Create `drpl-backend/tests/test_costing_oem_weblink.py`:

```python
"""Piece 3 — OEM manufacturer + web link as first-class costing line fields."""
from app.models.cost_breakdown import CostBreakdownLine


def test_model_has_oem_and_source_url_columns():
    cols = {c.name for c in CostBreakdownLine.__table__.columns}
    assert "oem_manufacturer" in cols
    assert "source_url" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_oem_weblink.py -v`
Expected: FAIL — assertion error (columns not defined).

- [ ] **Step 3: Add the model columns**

In `cost_breakdown.py`, immediately after `confidence = Column(...)` at line 168, add:
```python
    # Piece 3 — structured provenance for web-priced lines.
    oem_manufacturer = Column(String(128), nullable=True)  # best-effort OEM / manufacturer
    source_url = Column(Text, nullable=True)               # guaranteed web link
```

- [ ] **Step 4: Add schema-drift self-heal**

In `app/main.py` `_apply_schema_drift_fixes()`, after the last `cost_breakdown_lines` ADD COLUMN statement (line 217), add:
```python
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS oem_manufacturer VARCHAR(128)",
        "ALTER TABLE cost_breakdown_lines ADD COLUMN IF NOT EXISTS source_url TEXT",
```
In `_add_missing_columns()` migrations list, after the last `cost_breakdown_lines` tuple (line 642), add:
```python
        ("cost_breakdown_lines", "oem_manufacturer", "VARCHAR(128)"),
        ("cost_breakdown_lines", "source_url", "TEXT"),
```

- [ ] **Step 5: Author the Alembic revision (do NOT run it)**

Run: `cd drpl-backend && ./venv/Scripts/alembic.exe revision -m "cost_line_oem_source_url"` (plain `revision`, NO `--autogenerate`, NO `upgrade` — this only writes a template file, no DB connection). Edit the generated file:
```python
down_revision = "f92bd2162176"

def upgrade():
    op.add_column("cost_breakdown_lines",
                  sa.Column("oem_manufacturer", sa.String(length=128), nullable=True))
    op.add_column("cost_breakdown_lines",
                  sa.Column("source_url", sa.Text(), nullable=True))

def downgrade():
    op.drop_column("cost_breakdown_lines", "source_url")
    op.drop_column("cost_breakdown_lines", "oem_manufacturer")
```
(Ensure `import sqlalchemy as sa` and `from alembic import op` are present — the template adds them.)

- [ ] **Step 6: Run test to verify it passes + app imports**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_oem_weblink.py -v` → PASS.
Then: `cd drpl-backend && ./venv/Scripts/python.exe -c "import app.main"` → no error. (Note: this triggers the self-heal on the live DB — adds the two nullable columns, benign.)

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/models/cost_breakdown.py drpl-backend/app/main.py drpl-backend/alembic/versions/ drpl-backend/tests/test_costing_oem_weblink.py
git commit -m "feat(costing): add oem_manufacturer + source_url columns to CostBreakdownLine

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Persist + round-trip the two fields (normalized dict + merge whitelist)

`_normalize_line_dict` builds the clean dict that becomes a `CostBreakdownLine` via `CostBreakdownLine(**line)`. Add the two keys so they persist. Also add them to the merge/update whitelist so edits/re-runs don't drop them.

**Files:**
- Modify: `drpl-backend/app/services/cost_breakdown_service.py` — `_normalize_line_dict` (after line 414), the merge whitelist (lines 970-971), and the read-back emit dicts (lines 1729-1734, 1834-1851)
- Test: `drpl-backend/tests/test_costing_oem_weblink.py` (extend)

**Interfaces:**
- Consumes: `CostBreakdownLine.oem_manufacturer`/`source_url` (Task 1).
- Produces: normalized line dict now carries `oem_manufacturer`/`source_url`; merge path preserves them.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_costing_oem_weblink.py`:
```python
from app.services.cost_breakdown_service import _normalize_line_dict


def test_normalize_line_dict_carries_oem_and_source_url():
    line = {
        "description": "LED fitting", "quantity": 10, "rate": 500,
        "rate_source": "web_search",
        "oem_manufacturer": "Wipro Lighting",
        "source_url": "https://example.com/led-fitting",
    }
    # _normalize_line_dict(line, sr_no, breakdown_margin_percent)
    out = _normalize_line_dict(line, 1, 0.0)
    assert out["oem_manufacturer"] == "Wipro Lighting"
    assert out["source_url"] == "https://example.com/led-fitting"


def test_normalize_line_dict_defaults_missing_to_none():
    out = _normalize_line_dict({"description": "X", "quantity": 1, "rate": 1}, 1, 0.0)
    assert out["oem_manufacturer"] is None
    assert out["source_url"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_oem_weblink.py -k normalize -v`
Expected: FAIL — `KeyError: 'oem_manufacturer'` (keys not in the normalized dict).

- [ ] **Step 3: Add the keys to the normalized dict**

In `cost_breakdown_service.py` `_normalize_line_dict`, immediately after the `source_ref` line (line 414), add:
```python
        "oem_manufacturer": (line.get("oem_manufacturer") or "").strip() or None,
        "source_url": (line.get("source_url") or "").strip() or None,
```

- [ ] **Step 4: Add to the merge/update whitelist**

At the allowed-field tuple (lines 970-971) that currently lists `"rate_source", "source_ref", "confidence", ...`, add `"oem_manufacturer", "source_url"` so the `setattr` merge loop (line 1032) carries them on edits/re-runs.

- [ ] **Step 5: Add to the read-back emit dicts**

At the two row-emit dicts that emit `rate_source`/`source_ref` (lines 1729-1734 and 1834-1851), add `"oem_manufacturer": line.oem_manufacturer` and `"source_url": line.source_url` so persisted lines round-trip to the editor/XLSX. (Match the exact dict style at each site — read the surrounding lines and mirror how `source_ref` is emitted there.)

- [ ] **Step 6: Run tests + import check**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_oem_weblink.py -v` → PASS.
Then: `cd drpl-backend && ./venv/Scripts/python.exe -c "import app.services.cost_breakdown_service"` → no error.
Then regression: `./venv/Scripts/python.exe -m pytest tests/ -k costing -q` → PASS.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/cost_breakdown_service.py drpl-backend/tests/test_costing_oem_weblink.py
git commit -m "feat(costing): persist + round-trip oem_manufacturer/source_url on cost lines

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Render OEM + clickable Web-Source columns in the two web-priced layouts

Add the two columns to `_COST_COLUMNS` (default single-sheet) and `_MARGIN_COLUMNS` (margin/multi-sheet). OEM renders as plain text via the existing generic branch. Web Source needs a NEW hyperlink branch (no openpyxl hyperlink usage exists in this file — this is net-new code).

**Files:**
- Modify: `drpl-backend/app/services/langchain/tools/xlsx_generator_tool.py` — `_COST_COLUMNS` (lines 27-37), `_MARGIN_COLUMNS` (lines 48-60), and the row-writing loops that need the hyperlink branch: single-sheet (lines 1192-1213), margin per-schedule (lines 1585-1598), margin consolidated (lines 1688-1699).
- Test: `drpl-backend/tests/test_costing_oem_weblink.py` (extend)

**Interfaces:**
- Consumes: row dicts carrying `oem_manufacturer`/`source_url` (Task 4 supplies them; tests here pass them directly).
- Produces: xlsx with an "OEM / Manufacturer" text column and a "Web Source" hyperlink column in both layouts.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_costing_oem_weblink.py`:
```python
import openpyxl
from app.services.langchain.tools.xlsx_generator_tool import build_cost_xlsx


def test_single_sheet_has_oem_and_clickable_web_source(tmp_path):
    out = tmp_path / "c.xlsx"
    build_cost_xlsx(
        str(out), "Cost",
        rows=[{"sr_no": "1", "description": "LED", "qty": 2, "rate": 500,
               "amount": 1000, "rate_source": "web_search",
               "oem_manufacturer": "Wipro",
               "source_url": "https://example.com/led"}],
    )
    wb = openpyxl.load_workbook(str(out))
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert "OEM / Manufacturer" in header
    assert "Web Source" in header
    # OEM text present; Web Source cell is a hyperlink.
    texts = [c.value for r in ws.iter_rows() for c in r]
    assert "Wipro" in texts
    link_cells = [c for r in ws.iter_rows() for c in r if c.hyperlink is not None]
    assert any(c.hyperlink.target == "https://example.com/led" for c in link_cells)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_oem_weblink.py -k web_source -v`
Expected: FAIL — `assert "OEM / Manufacturer" in header` (columns not added yet).

- [ ] **Step 3: Add the columns to both column lists**

In `_COST_COLUMNS` (lines 27-37), after the `source_ref` tuple, add:
```python
    ("oem_manufacturer", "OEM / Manufacturer", 22),
    ("source_url", "Web Source", 40),
```
In `_MARGIN_COLUMNS` (lines 48-60), after the `cost_buildup_note` tuple, add the same two tuples.

- [ ] **Step 4: Add the hyperlink branch to each row-writing loop**

In each of the three row loops (single-sheet ~1192-1213, margin per-schedule ~1585-1598, margin consolidated ~1688-1699), add a `source_url` branch BEFORE the generic `else`. For the single-sheet loop the branch is (adapt cell/row/col variable names to each loop — the single-sheet loop uses `ws`, `r_idx`, `c_idx`, `border_thin`; read each loop and match its locals):
```python
            elif key == "source_url":
                from openpyxl.styles import Font as _Font
                url = (raw or "").strip()
                cell = ws.cell(row=r_idx, column=c_idx, value=(url or None))
                if url:
                    cell.hyperlink = url
                    cell.font = _Font(color="0563C1", underline="single")
                cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)
```
`oem_manufacturer` needs NO special branch — the generic `else` renders it as wrapped text automatically. Confirm `oem_manufacturer` and `source_url` are NOT in `_CURRENCY_COLS`/`_PERCENT_COLS` (they aren't) so they don't hit the numeric branch.

- [ ] **Step 5: Run test to verify it passes + xlsx regression**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_oem_weblink.py -v` → PASS.
Then: `./venv/Scripts/python.exe -m pytest tests/ -k "xlsx or costing" -q` → PASS (existing layouts unaffected — the new columns are additive and render empty when absent).

- [ ] **Step 6: Commit**

```bash
git add drpl-backend/app/services/langchain/tools/xlsx_generator_tool.py drpl-backend/tests/test_costing_oem_weblink.py
git commit -m "feat(costing): render OEM + clickable Web-Source columns in cost xlsx layouts

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Thread the fields through the calculator + agent-output mapping + prompt

Wire the source ends: the cost-calculator passes the fields through (so agent-invoked calc lines keep them), the agent-output→XLSX mapping includes them in row dicts, and the prompt instructs the agent to populate them.

**Files:**
- Modify: `drpl-backend/app/services/langchain/tools/cost_calculator_tool.py` — `_CalcLine` (after line 76) + output `line_out` dict (lines 285-293)
- Modify: `drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py` — `_emit_costing_xlsx_artifact` mapping loop (read ~lines 3138-3139, write into appended dict ~lines 3176-3177)
- Modify: `drpl-backend/app/services/langchain/graphs/costing_agent.py` — `COSTING_AGENT_SYSTEM_PROMPT` schema block (lines 232-234) + web-source rule (lines 279-281)
- Test: `drpl-backend/tests/test_costing_oem_weblink.py` (extend)

**Interfaces:**
- Consumes: nothing new. Produces: agent-output row dicts + calc output rows carry the two fields; prompt documents them.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_costing_oem_weblink.py`:
```python
from app.services.langchain.tools.cost_calculator_tool import _CalcLine


def test_calcline_accepts_oem_and_source_url():
    ln = _CalcLine(description="X", quantity=1, rate=10,
                   oem_manufacturer="ACME", source_url="https://acme.test")
    assert ln.oem_manufacturer == "ACME"
    assert ln.source_url == "https://acme.test"


def test_prompt_documents_oem_and_source_url():
    from app.services.langchain.graphs.costing_agent import COSTING_AGENT_SYSTEM_PROMPT
    p = COSTING_AGENT_SYSTEM_PROMPT
    assert "oem_manufacturer" in p
    assert "source_url" in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_oem_weblink.py -k "calcline or prompt" -v`
Expected: FAIL — `_CalcLine` rejects the kwargs / prompt lacks the strings.

- [ ] **Step 3: Add the fields to `_CalcLine` + output pass-through**

In `cost_calculator_tool.py`, after the `rate_source` field (line 76), add:
```python
    oem_manufacturer: Optional[str] = None
    source_url: Optional[str] = None
```
(Confirm `Optional` is imported — the model already uses `Optional[...]` fields.) Then in the `line_out` dict (lines 285-293), add:
```python
        "oem_manufacturer": ln.oem_manufacturer,
        "source_url": ln.source_url,
```

- [ ] **Step 4: Add to the agent-output→XLSX row mapping**

In `chat_agent_wrappers.py` `_emit_costing_xlsx_artifact`, near where `rate_source`/`source_ref` are read (~lines 3138-3139) add:
```python
            oem_manufacturer = (item.get("oem_manufacturer") or "").strip()
            source_url = (item.get("source_url") or "").strip()
```
and in the appended row dict (~lines 3176-3177, where `rate_source`/`source_ref` are written) add:
```python
                "oem_manufacturer": oem_manufacturer,
                "source_url": source_url,
```
(Read the surrounding loop to match the exact indentation/variable names — the item loop variable is `item`.)

- [ ] **Step 5: Update the prompt**

In `costing_agent.py` `COSTING_AGENT_SYSTEM_PROMPT`:
- In the line-item JSON schema block (near lines 232-234, alongside `rate_source`/`source_ref`), add two field docs:
```
- oem_manufacturer : string — best-effort OEM / manufacturer / brand name for a web-priced or catalogue item (e.g. "Wipro", "Cummins"). Omit / null if not clearly identifiable.
- source_url       : string — the canonical web link the price came from. REQUIRED whenever rate_source="web_search".
```
- In the `rate_source='web_search'` special-case rule (near lines 279-281), append:
```
Additionally, put the single canonical product/listing URL in `source_url` (not only in source_ref), and the brand/OEM name in `oem_manufacturer` when identifiable.
```
(Preserve the existing `source_ref` instruction — the new fields are additive, not a replacement.)

- [ ] **Step 6: Run tests + imports + regression**

Run: `cd drpl-backend && ./venv/Scripts/python.exe -m pytest tests/test_costing_oem_weblink.py -v` → PASS (all).
Then imports: `./venv/Scripts/python.exe -c "import app.services.langchain.tools.cost_calculator_tool; import app.services.langchain.graphs.chat_agent_wrappers; import app.services.langchain.graphs.costing_agent"` → no error.
Then regression: `./venv/Scripts/python.exe -m pytest tests/ -k "costing or xlsx" -q` → PASS.

- [ ] **Step 7: Commit**

```bash
git add drpl-backend/app/services/langchain/tools/cost_calculator_tool.py \
  drpl-backend/app/services/langchain/graphs/chat_agent_wrappers.py \
  drpl-backend/app/services/langchain/graphs/costing_agent.py \
  drpl-backend/tests/test_costing_oem_weblink.py
git commit -m "feat(costing): thread oem_manufacturer/source_url through calc, mapping, prompt

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage (Piece 3, spec §6):**
- `oem_manufacturer` (String) + `source_url` (Text) on `CostBreakdownLine`, with Alembic + drift self-heal → Task 1. ✅
- Web search return preserves `url` → already does (no change needed); documented in Architecture. ✅
- Calculator passes the fields through (currently drops provenance) → Task 4 (`_CalcLine` + `line_out`). ✅
- Costing prompt instructs the agent to populate them (OEM best-effort, source_url required on web_search) → Task 4. ✅
- XLSX "OEM / Manufacturer" + clickable "Web Source" columns in the relevant layouts → Task 3. ✅
- OEM best-effort, web link guaranteed → Task 4 prompt wording + nullable columns. ✅
- Persist + round-trip (not in spec but required for the data to survive) → Task 2 (normalized dict + merge whitelist + read-back). ✅

**Placeholder scan:** Tasks 2 Step 5 and 3 Step 4 tell the implementer to "match the surrounding style / adapt local variable names" at multiple similar sites rather than pasting one literal block — this is deliberate: the three xlsx row loops and the two read-back dicts have different local names, and pasting identical code would be wrong. The exact code shape and the anchor lines ARE given for one representative site each; the implementer adapts locals. All other steps carry exact code.

**Type consistency:** `oem_manufacturer` is `String(128)`/`str` and `source_url` is `Text`/`str` consistently across model (Task 1), normalized dict (Task 2), calc model (Task 4), and xlsx (Task 3, text + hyperlink). Alembic `down_revision="f92bd2162176"` matches the current head. The two keys are spelled identically (`oem_manufacturer`, `source_url`) in all 6 files — a spelling drift would silently drop data, so the tests in Tasks 1-4 each assert the exact key names.

**Out of scope:** rendering the columns in NIT-mirror / client-annexure layouts (web-priced provenance doesn't apply there); structured OEM extraction inside the web-search tool (OEM stays best-effort, inferred by the agent).
