"""The Liluah costing (~190 rows: NIT 16 + Annex-I 4 + Annex-II ~176 +
Annex-VII 9) captured everything and then never finished.

Four faults, pinned here:

1. TIME -- four batches of sixty ran one after another under a 2,880 s
   budget; the RQ job is killed at 1,800 s. The budget is now per wave of
   concurrent batches and capped below the job and Master Agent limits, and
   a run that hits the wall returns the batches already saved.
2. MERGE -- batch 3 (annexure rows) came back 60 rows, 19 "merged", 41
   unmatched, none by boq_item_id. Annexure rows have no schedule, so the
   (schedule, code) index never held them, and the bare sr_no fallback bound
   their prices to unrelated NIT rows. They match by their ANX code now, and
   a serial alone binds nothing.
3. CONCURRENCY -- the analyzer re-captured the schedule with force=True on
   every run, with no lock, replacing every BOQItem id under a running
   costing. It takes the re-capture lock and stands down while a costing
   runs.
4. LOGS -- the worker echoed every SQL statement; Railway drops lines past
   500/s. Silenced at boot.
"""
from __future__ import annotations

import itertools
import logging

import pytest

from app.models.cost_breakdown import CostBreakdownLine
from app.models.costing_template import BOQItem
from app.models.tender import Tender
from app.services import boq_parser_service as bps
from app.services import cost_breakdown_service as cbs
from app.services.langchain.graphs import enhanced_costing_agent as eca
from app.services.run_service import _RUN_JOB_TIMEOUT_SECONDS

_ids = itertools.count(810_000)


@pytest.fixture
def tid():
    return next(_ids)


# ── 1. TIME ──────────────────────────────────────────────────────────────────

BS, SWEEPS = 60, 2


def test_the_budget_never_exceeds_the_job_that_runs_it():
    cap = eca._costing_budget_cap_s()
    assert cap <= _RUN_JOB_TIMEOUT_SECONDS - eca._OUTER_HEADROOM_S
    for rows in (16, 60, 190, 375, 1000):
        assert eca._costing_timeout_seconds(rows, BS, SWEEPS, concurrency=4, cap=cap) <= cap


def test_the_liluah_budget_is_one_wave_and_under_thirty_minutes():
    # 190 rows / 60 = 4 batches; 4 at a time = 1 wave of 4 sharing the
    # model: 1 * (240 * 2) * 3 = 1440, inside the 1500 cap and under the
    # 1800 job limit with the batches stopping 180 s early.
    budget = eca._costing_timeout_seconds(190, BS, SWEEPS, concurrency=4, cap=eca._costing_budget_cap_s())
    assert budget == 1440
    assert budget - eca._FINALIZE_RESERVE_S + eca._OUTER_HEADROOM_S <= _RUN_JOB_TIMEOUT_SECONDS
    # Sequential, uncapped, it was 4 * 240 * 3 = 2880 -- more than the job allowed.
    assert eca._costing_timeout_seconds(190, BS, SWEEPS) == 2880
    assert eca._costing_timeout_seconds(190, BS, SWEEPS, cap=1500) == 1500


def test_concurrency_shortens_the_budget_by_waves_not_below_the_floor():
    assert eca._costing_timeout_seconds(375, BS, SWEEPS, concurrency=1) == 5040   # 7 batches
    assert eca._costing_timeout_seconds(375, BS, SWEEPS, concurrency=4) == 2880   # 2 waves of 4
    assert eca._costing_timeout_seconds(375, BS, SWEEPS, concurrency=4, cap=1500) == 1500
    assert eca._costing_timeout_seconds(120, BS, SWEEPS, concurrency=4) == 900    # 2 in flight: floor
    assert eca._costing_timeout_seconds(10, BS, SWEEPS, concurrency=8) == 900     # floor


def test_the_batched_node_stops_before_the_outer_timer():
    assert eca._FINALIZE_RESERVE_S < eca._BATCHED_MIN_TIMEOUT_S
    assert eca._MIN_BATCH_SECONDS < eca._FINALIZE_RESERVE_S


def test_component_research_is_bounded_and_printed_rates_are_not_current_evidence():
    assert eca._has_printed_rate({"estimated_rate": 460.0})
    assert not eca._has_printed_rate({"estimated_rate": None})
    assert not eca._has_printed_rate({"estimated_rate": 0})
    assert not eca._has_printed_rate({})
    assert "Do NOT web-search" in eca._COMPONENT_RESEARCH_RULE
    block = eca._render_bidding_schedule_block([
        {"boq_item_id": 1, "sr_no": 1, "item_code": "ANX-II-1", "description": "Door frame",
         "quantity": 2, "unit": "Nos", "estimated_rate": 460, "annexure_ref": "II",
         "component_of": "Schedule A item CONVERSION MAT: Material Cost", "quantity_basis": "per set"},
    ])
    assert "current rate evidence" in block
    assert "8 searches per batch" in eca._COMPONENT_RESEARCH_RULE


# ── 2. MERGE ─────────────────────────────────────────────────────────────────


def _tender(db, tid):
    db.add(Tender(id=tid, portal="ireps", tender_id=f"T-{tid}", title="Liluah", source_url="x"))
    db.commit()


def _liluah_like(db, tid):
    """Two schedules whose serials both start at 1, and an annexure whose
    thirty rows restart at serial 1 per sub-table -- the shape that broke."""
    _tender(db, tid)
    items = [
        {"sr_no": 1, "item_code": "STRIPPING", "schedule_name": "A", "quantity": 15,
         "unit": "Coach Set", "estimated_rate": 38000, "description": "Stripping"},
        {"sr_no": 2, "item_code": "CONVERSION MAT", "schedule_name": "A", "quantity": 15,
         "unit": "Coach Set", "estimated_rate": 816051,
         "description": "Material Cost for Conversion work (As per Annexure-II of Material list)"},
        {"sr_no": 1, "item_code": "PAINT LABOUR", "schedule_name": "B", "quantity": 20,
         "unit": "Coach Set", "estimated_rate": 5000, "description": "Painting labour"},
    ]
    for sub in (1, 2):
        for sr in (1, 2, 3):
            items.append({
                "sr_no": sr, "item_code": None, "schedule_name": None, "annexure_ref": "II",
                "quantity": sr, "unit": "Nos", "estimated_rate": 100 * sub + sr,
                "description": f"Part {sub}.{sr}",
            })
    bps._assign_annexure_codes(items)
    bps._link_annexure_components(items)
    records = []
    for it in items:
        b = BOQItem(
            tender_id=tid, sr_no=it["sr_no"], description=it["description"],
            quantity=it.get("quantity"), unit=it.get("unit"),
            estimated_rate=it.get("estimated_rate"), item_code=it.get("item_code"),
            schedule_name=it.get("schedule_name"), annexure_ref=it.get("annexure_ref"),
        )
        db.add(b)
        records.append(b)
    db.commit()
    for it, b in zip(items, records):
        if it.get("_parent_idx") is not None:
            b.parent_item_id = records[it["_parent_idx"]].id
    db.commit()
    bd = cbs.build_skeleton_from_boq(db, tid)
    return bd, records


def _line(db, bd, code):
    return (
        db.query(CostBreakdownLine)
        .filter(CostBreakdownLine.cost_breakdown_id == bd.id, CostBreakdownLine.item_code == code)
        .one()
    )


def test_annexure_rows_merge_by_their_anx_code_when_the_ids_are_stale(db, tid):
    bd, records = _liluah_like(db, tid)
    anx = [r for r in records if r.annexure_ref == "II"]
    assert [r.item_code for r in anx] == [f"ANX-II-{n}" for n in range(1, 7)]
    # The agent echoes ids from a schedule a concurrent re-capture replaced.
    batch = [
        {"boq_item_id": r.id + 1_000_000, "item_code": r.item_code, "sr_no": r.sr_no,
         "schedule_name": "", "rate": 10.0 * n, "rate_source": "derived_estimate"}
        for n, r in enumerate(anx, start=1)
    ]
    out = cbs.merge_batch_rates(db, bd.id, batch)
    assert out == {"matched": 6, "unmatched": 0}
    for n, r in enumerate(anx, start=1):
        assert _line(db, bd, r.item_code).rate == pytest.approx(10.0 * n)
    # Nothing leaked onto the NIT rows that share those serials.
    for code in ("STRIPPING", "CONVERSION MAT", "PAINT LABOUR"):
        assert _line(db, bd, code).rate is None


def test_anx_code_matching_tolerates_formatting_drift_and_a_schedule_label(db, tid):
    bd, records = _liluah_like(db, tid)
    batch = [
        {"item_code": " anx-ii-4 ", "schedule_name": "Annexure-II components", "rate": 7.5},
        {"item_code": "ANX II 5", "schedule_name": "20", "sr_no": 2, "rate": 8.5},
    ]
    assert cbs.merge_batch_rates(db, bd.id, batch) == {"matched": 2, "unmatched": 0}
    assert _line(db, bd, "ANX-II-4").rate == pytest.approx(7.5)
    assert _line(db, bd, "ANX-II-5").rate == pytest.approx(8.5)


def test_a_serial_alone_binds_nothing(db, tid):
    bd, _ = _liluah_like(db, tid)
    # sr_no 1 names STRIPPING (A), PAINT LABOUR (B) and two annexure parts.
    batch = [{"sr_no": 1, "rate": 999.0}, {"sr_no": 2, "item_code": "", "rate": 999.0}]
    assert cbs.merge_batch_rates(db, bd.id, batch) == {"matched": 0, "unmatched": 2}
    assert all(
        ln.rate is None
        for ln in db.query(CostBreakdownLine).filter(CostBreakdownLine.cost_breakdown_id == bd.id)
    )


def test_a_serial_inside_its_schedule_still_binds_when_unique(db, tid):
    bd, _ = _liluah_like(db, tid)
    batch = [{"schedule_name": "B", "sr_no": 1, "rate": 4200.0, "rate_source": "web_search"}]
    assert cbs.merge_batch_rates(db, bd.id, batch) == {"matched": 1, "unmatched": 0}
    assert _line(db, bd, "PAINT LABOUR").rate == pytest.approx(4200.0)
    assert _line(db, bd, "STRIPPING").rate is None


def test_an_annexure_serial_never_binds_by_serial(db, tid):
    bd, _ = _liluah_like(db, tid)
    # Annexure serials restart per sub-table: "II, sr 2" names two rows.
    batch = [{"schedule_name": "II", "sr_no": 2, "rate": 1.0},
             {"annexure_ref": "II", "sr_no": 3, "rate": 1.0}]
    assert cbs.merge_batch_rates(db, bd.id, batch) == {"matched": 0, "unmatched": 2}


def test_unmatched_rows_stay_needs_input_and_the_rest_roll_up(db, tid):
    bd, records = _liluah_like(db, tid)
    anx = [r for r in records if r.annexure_ref == "II"]
    batch = [{"item_code": r.item_code, "rate": 5.0} for r in anx[:5]]
    batch.append({"sr_no": 3, "rate": 5.0})            # the sixth, serial only
    out = cbs.merge_batch_rates(db, bd.id, batch)
    assert out == {"matched": 5, "unmatched": 1}
    sixth = _line(db, bd, "ANX-II-6")
    assert sixth.needs_input and sixth.rate is None
    cbs.rollup_component_lines(db, bd.id)
    parent = _line(db, bd, "CONVERSION MAT")
    assert parent.rate == pytest.approx(sum(5.0 * r.quantity for r in anx[:5]))


# ── 3. CONCURRENCY ───────────────────────────────────────────────────────────


class _FakeRedis:
    def __init__(self):
        self.keys: dict[str, str] = {}
        self.sets: list[tuple] = []

    def set(self, key, value, nx=False, ex=None):
        self.sets.append((key, nx, ex))
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def get(self, key):
        return self.keys.get(key)

    def exists(self, key):
        return 1 if key in self.keys else 0

    def delete(self, *keys):
        return sum(1 for k in keys if self.keys.pop(k, None) is not None)

    def incr(self, key):
        self.keys[key] = str(int(self.keys.get(key, "0")) + 1)
        return int(self.keys[key])

    def decr(self, key):
        self.keys[key] = str(int(self.keys.get(key, "0")) - 1)
        return int(self.keys[key])

    def expire(self, key, ttl):
        return True


def _fake_parse(calls):
    async def parse(_db, _tid, force=False):
        calls.append(force)
        return ["row"] * 3
    return parse


@pytest.mark.asyncio
async def test_the_analysis_takes_the_recapture_lock_and_releases_it(tid, monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: r)
    calls: list[bool] = []
    monkeypatch.setattr(bps, "parse_boq_from_tender", _fake_parse(calls))

    assert await bps.recapture_schedule_for_analysis(None, tid) == ["row"] * 3
    assert calls == [True]
    key = bps._recapture_lock_key(tid)
    assert (key, True, bps._RECAPTURE_LOCK_TTL_SECONDS) in r.sets
    assert key not in r.keys


@pytest.mark.asyncio
async def test_the_analysis_does_not_recapture_under_a_running_costing(tid, monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: r)
    calls: list[bool] = []
    monkeypatch.setattr(bps, "parse_boq_from_tender", _fake_parse(calls))

    assert bps.mark_costing_running(tid, ttl_seconds=900) is True
    assert bps.costing_is_running(tid)
    await bps.recapture_schedule_for_analysis(None, tid)
    assert calls == [False], "a forced re-capture ran under a live costing"
    assert bps._recapture_lock_key(tid) not in r.sets

    # Two costings: the flag drops only when the last one clears it.
    bps.mark_costing_running(tid)
    bps.clear_costing_running(tid)
    assert bps.costing_is_running(tid)
    bps.clear_costing_running(tid)
    assert not bps.costing_is_running(tid)
    await bps.recapture_schedule_for_analysis(None, tid)
    assert calls == [False, True]


@pytest.mark.asyncio
async def test_the_analysis_waits_for_another_recapture_instead_of_racing_it(db, tid, monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: r)
    calls: list[bool] = []
    monkeypatch.setattr(bps, "parse_boq_from_tender", _fake_parse(calls))
    monkeypatch.setattr(bps, "_RECAPTURE_POLL_SECONDS", 0)
    r.keys[bps._recapture_lock_key(tid)] = "1"     # a costing is re-capturing

    async def _cleared(_tid):
        r.keys.pop(bps._recapture_lock_key(tid), None)
        return True
    monkeypatch.setattr(bps, "_wait_for_recapture", _cleared)
    _tender(db, tid)
    assert await bps.recapture_schedule_for_analysis(db, tid) == []
    assert calls == []


def test_without_redis_the_flag_is_inert(monkeypatch):
    monkeypatch.setattr("app.core.redis_client.get_redis", lambda: None)
    assert bps.mark_costing_running(1) is False
    assert bps.costing_is_running(1) is False
    bps.clear_costing_running(1)


def test_the_analyzer_no_longer_forces_a_parse_directly():
    import inspect
    from app.services.langchain.graphs import document_analysis_agent as daa
    src = inspect.getsource(daa)
    assert "parse_boq_from_tender(db, tender_id, force=True)" not in src
    assert "recapture_schedule_for_analysis(db, tender_id)" in src


# ── 4. LOGS ──────────────────────────────────────────────────────────────────


def test_the_worker_silences_sqlalchemy_echo():
    import importlib
    import worker
    importlib.reload(worker)
    from app.core import database
    eng_logger = logging.getLogger("sqlalchemy.engine.Engine")
    was = eng_logger.level
    try:
        eng_logger.setLevel(logging.INFO)
        database.engine.echo = True
        worker._silence_sql_logging()
        assert logging.getLogger("sqlalchemy.engine").level == logging.WARNING
        assert eng_logger.level == logging.WARNING
        assert not database.engine.echo
    finally:
        database.engine.echo = False
        eng_logger.setLevel(was)


# ── 5. ROLL-UP against the published rate ────────────────────────────────────


def test_an_annexure_quantity_that_contradicts_its_printed_total_is_rederived():
    items = [
        # 575 kg at Rs 39.18 = Rs 22,523.19 printed; the weight column was misread.
        {"annexure_ref": "II", "quantity": 12243.2, "estimated_rate": 39.18, "basic_value": 22523.19, "description": "Chequered plate"},
        # consistent: left alone
        {"annexure_ref": "II", "quantity": 15.712, "estimated_rate": 29.29, "basic_value": 460.21, "description": "Channel"},
        # no printed total: left as read
        {"annexure_ref": "VII", "quantity": 12, "estimated_rate": None, "basic_value": None, "description": "Primer"},
        # a schedule row is never touched
        {"schedule_name": "A", "quantity": 15, "estimated_rate": 38000, "basic_value": 570000, "description": "Stripping"},
        # the printed total read into BOTH the quantity and the rate, no total:
        # one lot at that total, not 4,360 kg of screws at Rs 4,360/kg.
        {"annexure_ref": "II", "quantity": 4360.13, "estimated_rate": 4360.13, "basic_value": None, "description": "SL to CSK HD Screw M6x30"},
    ]
    assert bps._reconcile_annexure_quantities(items) == 2
    assert items[4]["quantity"] == 1.0 and items[4]["basic_value"] == 4360.13 and items[4]["estimated_rate"] == 4360.13
    assert items[0]["quantity"] == pytest.approx(22523.19 / 39.18, abs=0.001)
    assert items[1]["quantity"] == 15.712
    assert items[2]["quantity"] == 12
    assert items[3]["quantity"] == 15


def test_rollup_divides_out_components_printed_for_the_whole_contract():
    # Annexure-I: "15" against every item is the fifteen coach sets, and the
    # printed totals sum to the schedule's Rs 38,000 x 15, not Rs 38,000.
    parent = {"boq_item_id": 1, "quantity": 15, "unit": "Coach Set", "tender_rate": 38000.0,
              "rate": None, "rate_source": "needs_user_input", "schedule_name": "A", "item_code": "STRIPPING"}
    comps = [
        {"boq_item_id": 2, "parent_boq_item_id": 1, "annexure_ref": "I", "quantity": 15,
         "tender_rate": 24033.83, "basic_value": 360507.39, "rate": 19226.64, "amount": 288399.6,
         "rate_source": "derived_estimate", "confidence": "medium"},
        {"boq_item_id": 3, "parent_boq_item_id": 1, "annexure_ref": "I", "quantity": 15,
         "tender_rate": 13966.17, "basic_value": 209492.61, "rate": 11172.94, "amount": 167594.1,
         "rate_source": "derived_estimate", "confidence": "medium"},
    ]
    cbs.rollup_component_parents([parent] + comps)
    assert parent["rate"] == pytest.approx((288399.6 + 167594.1) / 15, abs=0.01)
    assert parent["amount"] == pytest.approx(288399.6 + 167594.1, abs=0.1)
    assert parent["rate"] < parent["tender_rate"]
    assert "divided" in parent["cost_buildup_note"]


def test_rollup_keeps_per_set_components_per_set():
    # Annexure-II: printed totals sum to the published per-set rate itself.
    parent = {"boq_item_id": 1, "quantity": 15, "unit": "Coach Set", "tender_rate": 816051.0,
              "rate": None, "rate_source": "needs_user_input"}
    comps = [
        {"boq_item_id": 2, "parent_boq_item_id": 1, "annexure_ref": "II", "quantity": 100,
         "tender_rate": 4000.0, "basic_value": 400000.0, "rate": 3200.0, "amount": 320000.0, "rate_source": "web_search"},
        {"boq_item_id": 3, "parent_boq_item_id": 1, "annexure_ref": "II", "quantity": 100,
         "tender_rate": 4100.0, "basic_value": 410000.0, "rate": 3300.0, "amount": 330000.0, "rate_source": "web_search"},
    ]
    cbs.rollup_component_parents([parent] + comps)
    assert parent["rate"] == pytest.approx(650000.0)
    assert parent["amount"] == pytest.approx(650000.0 * 15)
    assert "divided" not in parent["cost_buildup_note"]
    # No printed totals at all (Annexure-VII): per set, unchanged behaviour.
    parent2 = {"boq_item_id": 9, "quantity": 20, "unit": "Coach Set", "tender_rate": 22547.44, "rate": None}
    comps2 = [{"boq_item_id": 10, "parent_boq_item_id": 9, "annexure_ref": "VII", "quantity": 12,
               "rate": 400.0, "amount": 4800.0, "rate_source": "web_search"}]
    cbs.rollup_component_parents([parent2] + comps2)
    assert parent2["rate"] == pytest.approx(4800.0)


def test_rollup_calls_out_a_build_up_far_above_the_published_rate():
    parent = {"boq_item_id": 9, "quantity": 20, "unit": "Coach Set", "tender_rate": 22547.44, "rate": None,
              "schedule_name": "B", "item_code": "PAINT MATERIAL"}
    comps = [{"boq_item_id": 10, "parent_boq_item_id": 9, "annexure_ref": "VII", "quantity": 12,
              "rate": 2400.0, "amount": 28800.0, "rate_source": "derived_estimate"},
             {"boq_item_id": 11, "parent_boq_item_id": 9, "annexure_ref": "VII", "quantity": 40,
              "rate": 750.0, "amount": 30000.0, "rate_source": "derived_estimate"}]
    out = cbs.rollup_component_parents([parent] + comps)
    assert parent["rate"] == pytest.approx(58800.0)
    assert "2.6x the published rate" in parent["cost_buildup_note"]
    assert any("check the component rates" in n for n in out["notes"])
    # A build-up under the published rate is not flagged.
    parent2 = {"boq_item_id": 1, "quantity": 15, "unit": "Coach Set", "tender_rate": 38000.0, "rate": None}
    comps2 = [{"boq_item_id": 2, "parent_boq_item_id": 1, "annexure_ref": "I", "quantity": 1,
               "rate": 30000.0, "amount": 30000.0, "rate_source": "web_search"}]
    out2 = cbs.rollup_component_parents([parent2] + comps2)
    assert "published rate" not in parent2["cost_buildup_note"] and out2["notes"] == []
