"""Pure-function tests for the BOQ reconciliation gate. No DB.

Numbers are the real live-DB cases pulled during design (tenders 2693/2723/
2732/2733) — see docs/superpowers/specs/2026-07-20-costing-reconciliation-and-
provenance-design.md section 1.
"""
from app.services.boq_parser_service import (
    _reconcile_schedules,
    _collapse_over_schedules,
    _derive_schedule_statuses,
)


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
    ok, report, short, over, dup_over = _reconcile_schedules(items, subtotals, 2.0)
    assert ok is True
    assert short == set()
    assert over == {}
    assert dup_over == set()


def test_exact_2x_duplication_detected_as_over():
    # t=2732 sch=D: extracted 800160 vs printed 400080 -> multiple 2.
    items = _rows("D", 2, 400080.0)  # two copies of the one true row
    subtotals = {"D": 400080.0}
    ok, report, short, over, dup_over = _reconcile_schedules(items, subtotals, 2.0)
    assert ok is False
    assert over == {"D": 2}
    assert "D" not in short  # OVER is not SHORT


def test_11x_duplication_detected_as_over():
    # t=2693 sch=A: extracted ~11x printed.
    printed = 5975128.0
    items = _rows("A", 11, printed)  # 11 copies
    subtotals = {"A": printed}
    ok, report, short, over, dup_over = _reconcile_schedules(items, subtotals, 2.0)
    assert over == {"A": 11}


def test_short_schedule_still_detected_and_not_over():
    # Extracted below printed -> SHORT (existing behavior preserved).
    items = [{"schedule_name": "C", "sr_no": 1, "item_code": "C1",
              "basic_value": 500000.0, "is_tax_line": False}]
    subtotals = {"C": 1000000.0}
    ok, report, short, over, dup_over = _reconcile_schedules(items, subtotals, 2.0)
    assert ok is False
    assert "C" in short
    assert over == {}


def test_non_integer_ratio_is_not_over():
    # 1.5x is neither a clean multiple nor short-by-tolerance in the wrong dir;
    # must NOT be reported as OVER (we only auto-handle integer multiples).
    items = _rows("E", 3, 500000.0)  # sum 1.5M
    subtotals = {"E": 1000000.0}     # ratio 1.5
    ok, report, short, over, dup_over = _reconcile_schedules(items, subtotals, 2.0)
    assert over == {}


def _row(sched, sr, code, qty, val):
    return {"schedule_name": sched, "sr_no": sr, "item_code": code,
            "quantity": qty, "basic_value": val, "unit_rate": (val / qty if qty else 0),
            "is_tax_line": False}


def test_collapse_drops_null_code_artifact_keeps_coded_siblings():
    # t=2733 sch B shape: sr=3 has a(real), b(real), None(garbage). Keep a AND b, drop None.
    items = [
        _row("B", 3, "a", 480, 229276.8),
        _row("B", 3, "b", 480, 13987.2),
        _row("B", 3, None, 4, 2292768.0),   # null-code artifact
    ]
    collapsed, dropped = _collapse_over_schedules(items, {"B"})
    assert dropped == 1
    assert sorted(r["item_code"] for r in collapsed) == ["a", "b"]


def test_collapse_dedups_identical_value_twins_ignoring_desc():
    # t=2732 sch E shape: code=1 and code=E1, identical value+qty, desc differs by ligature.
    a = _row("E", 1, "1", 176, 574858.24); a["description"] = "LED light ﬁtting"   # ligature fi
    b = _row("E", 1, "E1", 176, 574858.24); b["description"] = "LED light fitting"      # plain fi
    collapsed, dropped = _collapse_over_schedules([a, b], {"E"})
    assert dropped == 1
    assert len(collapsed) == 1


def test_collapse_null_and_twin_combined_reconciles():
    # sr=1: coded 100 + prefixed-twin 100 + null artifact 900 -> keep one 100.
    items = [
        _row("A", 1, "1", 1, 100.0),
        _row("A", 1, "A1", 1, 100.0),   # identical-value twin
        _row("A", 1, None, 1, 900.0),   # null artifact
    ]
    collapsed, dropped = _collapse_over_schedules(items, {"A"})
    assert dropped == 2
    assert len(collapsed) == 1
    assert collapsed[0]["basic_value"] == 100.0


def test_collapse_keeps_distinct_value_rows_under_same_srno():
    # a and b are DISTINCT (different values) -> both kept, not deduped.
    items = [_row("B", 5, "a", 96, 2747393.28), _row("B", 5, "b", 96, 466182.72)]
    collapsed, dropped = _collapse_over_schedules(items, {"B"})
    assert dropped == 0
    assert len(collapsed) == 2


def test_collapse_non_over_schedule_untouched():
    items = [_row("Z", 1, None, 1, 999.0), _row("Z", 1, "Z1", 1, 10.0)]
    collapsed, dropped = _collapse_over_schedules(items, {"A"})  # Z not in set
    assert dropped == 0
    assert collapsed == items


def test_collapse_accepts_set_or_dict():
    items = [_row("A", 1, None, 1, 900.0), _row("A", 1, "A1", 1, 100.0)]
    c1, d1 = _collapse_over_schedules(list(items), {"A"})
    c2, d2 = _collapse_over_schedules(list(items), {"A": 2})
    assert d1 == d2 == 1 and len(c1) == len(c2) == 1


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


def test_reconcile_flags_non_integer_duplication_as_dup_over():
    # non-uniform: 3 sr_nos each appearing 3x -> extracted 3x printed but via repeated sr_no.
    rows = []
    for sr in (1, 2, 3):
        for tag in ("", "B", "BB"):
            rows.append({"schedule_name": "B", "sr_no": sr, "item_code": f"{tag}{sr}",
                         "basic_value": 1000.0, "quantity": 1, "unit_rate": 1000.0,
                         "is_tax_line": False})
    subtotals = {"B": 3000.0}  # true 3x1000; extracted 9000
    ok, rep, short, over, dup_over = _reconcile_schedules(rows, subtotals, 2.0)
    assert "B" in dup_over
    assert ok is False


def test_reconcile_clean_schedule_not_in_dup_over():
    rows = [{"schedule_name": "A", "sr_no": 1, "item_code": "A1", "basic_value": 100.0,
             "quantity": 1, "unit_rate": 100.0, "is_tax_line": False}]
    ok, rep, short, over, dup_over = _reconcile_schedules(rows, {"A": 100.0}, 2.0)
    assert dup_over == set()
    assert ok is True


def test_reconcile_short_schedule_not_in_dup_over():
    rows = [{"schedule_name": "C", "sr_no": 1, "item_code": "C1", "basic_value": 500.0,
             "quantity": 1, "unit_rate": 500.0, "is_tax_line": False}]
    ok, rep, short, over, dup_over = _reconcile_schedules(rows, {"C": 1000.0}, 2.0)
    assert "C" in short
    assert dup_over == set()


def test_prompt_has_merged_cell_guardrail():
    from app.services import boq_parser_service as bps
    p = bps._BOQ_AI_SYSTEM_PROMPT.lower()
    assert "merged" in p
    assert "once" in p  # "emit each logical item exactly once"
