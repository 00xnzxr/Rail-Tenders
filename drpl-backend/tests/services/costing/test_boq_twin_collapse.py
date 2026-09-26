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
