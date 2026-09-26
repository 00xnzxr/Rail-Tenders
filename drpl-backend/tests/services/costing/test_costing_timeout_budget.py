"""The costing timeout must match the route the run actually takes.

Regression: Command Center session 286 (tender 3810, 44 BOQ rows).
`_pick_costing_strategy` routes to the batched path whenever a captured
schedule exists (`count > 0`, regardless of size), but the timeout only widened
when `count > costing.batch_size` (60). A 44-row tender therefore ran the
batched path — per-line web research plus re-cost sweeps — on the 300s
single-call budget and was killed with TimeoutError, while a 61-row tender got
900s. Session 284 (375 rows) survived only because it cleared that threshold.
"""
import pytest

from app.services.langchain.graphs.enhanced_costing_agent import (
    _costing_timeout_seconds,
)

BS = 60          # costing.batch_size (prod value)
SWEEPS = 2       # costing.costing_max_sweeps (prod value)


def test_no_captured_schedule_keeps_the_single_call_budget():
    # No BOQ rows => genuinely the single-call path => 300s is correct.
    assert _costing_timeout_seconds(0, BS, SWEEPS) == 300


@pytest.mark.parametrize("count", [1, 10, 44, 59, 60])
def test_small_captured_schedules_get_the_batched_budget(count):
    """Any captured schedule routes batched, so it must be budgeted batched."""
    assert _costing_timeout_seconds(count, BS, SWEEPS) >= 900


def test_session_286_row_count_is_not_capped_at_300s():
    # Tender 3810 had 44 captured rows and was killed at exactly 300s.
    assert _costing_timeout_seconds(44, BS, SWEEPS) > 300


def test_session_284_budget_is_unchanged():
    # 375 rows / 60 = 7 batches; 7 * 240 * (1 + 2) = 5040.
    assert _costing_timeout_seconds(375, BS, SWEEPS) == 5040


def test_budget_never_shrinks_as_the_schedule_grows():
    """The batch_size boundary must not be a cliff — this is the actual bug."""
    budgets = [_costing_timeout_seconds(n, BS, SWEEPS) for n in range(1, 200)]
    assert budgets == sorted(budgets), "timeout budget must be non-decreasing"


def test_zero_or_negative_sweeps_are_tolerated():
    assert _costing_timeout_seconds(44, BS, 0) >= 900
    assert _costing_timeout_seconds(44, BS, -5) >= 900
