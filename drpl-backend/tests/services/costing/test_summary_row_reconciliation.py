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
    _drop_summary_placeholder_rows,
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


def test_placeholder_wording_variants_are_detected():
    """IREPS phrasing varies; a missed placeholder is a silent wrong number."""
    for description in [
        "Please see Item Breakup for details.",
        "Please refer Item Breakup for details.",
        "Refer to the Item Breakup",
        "See item break-up",
        "As per Item Breakup",
    ]:
        row = _placeholder("D", 22245831.95)
        row["description"] = description
        assert _is_summary_placeholder_row(row) is True, description


def test_priced_row_is_never_a_placeholder_whatever_the_wording():
    """The pointer phrase alone must never drop a row that carries a price."""
    for description in [
        "Please refer Item Breakup for details.",
        "As per Item Breakup",
    ]:
        row = _real_row("D", 1, 19.0, 8015.29)
        row["description"] = description
        assert _is_summary_placeholder_row(row) is False, description
