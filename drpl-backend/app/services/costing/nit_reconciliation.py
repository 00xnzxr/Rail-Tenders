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
