"""A captured schedule that yields ZERO costable rows must never fail silently.

Regression: Command Center session 285. Every captured row was (wrongly) flagged
`is_tax_line`, so `cost_rows` was empty, `total_batches` was 0, the batch loop
body never ran, and the run "succeeded" in 12 seconds having priced nothing.
The tax-line fix removes that specific cause; this guard makes the *class* of
failure loud rather than silent, whatever mislabels the rows next time.
"""
from app.services.langchain.graphs.enhanced_costing_agent import (
    split_costable_rows,
)


def _row(i, is_tax=False):
    return {"boq_item_id": i, "description": f"row {i}", "is_tax_line": is_tax}


def test_mixed_schedule_returns_non_tax_rows_and_no_note():
    rows = [_row(1), _row(2, is_tax=True), _row(3)]
    costable, note = split_costable_rows(rows)
    assert [r["boq_item_id"] for r in costable] == [1, 3]
    assert note is None


def test_all_tax_rows_yields_a_loud_note():
    rows = [_row(1, is_tax=True), _row(2, is_tax=True)]
    costable, note = split_costable_rows(rows)
    assert costable == []
    assert note is not None
    # The note must name the cause so the user isn't left with a bare
    # "[NEEDS RATE]" and no explanation.
    assert "2" in note
    assert "tax" in note.lower()


def test_single_tax_row_is_the_session_285_shape():
    costable, note = split_costable_rows([_row(1, is_tax=True)])
    assert costable == []
    assert note is not None


def test_no_captured_rows_is_not_this_failure():
    # An empty schedule is the single-call path's business, not a mislabelling.
    costable, note = split_costable_rows([])
    assert costable == []
    assert note is None


def test_missing_is_tax_line_key_counts_as_costable():
    costable, note = split_costable_rows([{"boq_item_id": 9, "description": "x"}])
    assert [r["boq_item_id"] for r in costable] == [9]
    assert note is None
