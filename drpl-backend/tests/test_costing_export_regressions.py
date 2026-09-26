"""Regressions reproduced from tender 5156's exported workbook and PDFs."""
import io
import itertools
import json

import pytest
from openpyxl import load_workbook

from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
from app.models.costing_template import BOQItem
from app.models.tender import Tender
from app.services import boq_parser_service as bps
from app.services import cost_breakdown_service as cbs
from app.services.langchain.graphs import enhanced_costing_agent as eca
from app.services.langchain.graphs import chat_agent_wrappers as wrappers

_ids = itertools.count(930000)


@pytest.fixture
def breakdown(db):
    tid = next(_ids)
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="Liluah", source_url="x"))
    parent = BOQItem(tender_id=tid, sr_no=3, item_code="CONVERSION MAT", schedule_name="A",
                     quantity=15, unit="Coach Set", estimated_rate=1000,
                     description="Materials as per Annexure-II")
    db.add(parent)
    db.flush()
    db.add_all([
        BOQItem(tender_id=tid, sr_no=4, item_code="CONVERSION MAT", schedule_name="A",
                quantity=2, unit="Nos", estimated_rate=100, description="Other material"),
        BOQItem(tender_id=tid, sr_no=1, item_code="ANX-II-1", annexure_ref="II",
                parent_item_id=parent.id, quantity=2, unit="kg", estimated_rate=100,
                basic_value=200, description="Channel"),
    ])
    db.commit()
    return cbs.build_skeleton_from_boq(db, tid)


def test_finalization_and_chat_export_keep_component_links_and_current_totals(db, breakdown, monkeypatch):
    # Leave the component unpriced: the existing reference fallback changes
    # it during finalization. Summary must use the final, rolled-up value.
    result = eca._finalize_batched_breakdown(
        db, breakdown, tender_id=breakdown.tender_id, analysis_result={}, assumptions=[],
        cost_assumptions=[], manpower=[], total_batches=1, max_sweeps=0,
    )["costing_result"]
    component = next(r for r in result["line_items"] if r["item_code"] == "ANX-II-1")
    assert component["parent_boq_item_id"] is not None
    assert component["schedule_section"].startswith("Annexure-II")
    assert result["line_items"][-1] is component
    summary_cost = sum(r["estimated_cost_inr"] for r in result["strategic_summary"]["schedule_breakdown"])
    top_cost = sum(r["amount"] or 0 for r in result["line_items"] if not r["is_component"])
    assert summary_cost == pytest.approx(top_cost)

    saved = {}
    def capture(_db, **kwargs):
        saved.update(kwargs)
        return {"artifact_id": 1}
    monkeypatch.setattr(cbs, "persist_xlsx_artifact", capture)
    wrappers._emit_costing_xlsx_artifact(db, 1, breakdown.tender_id, result)
    assert saved["summary"]["total_amount"] == pytest.approx(top_cost)
    ws = load_workbook(io.BytesIO(saved["data"]))["3. All Schedules"]
    totals = [row[0].row for row in ws if row[0].value == "TOTAL"]
    grand = next(row for row in ws if row[0].value == "GRAND TOTAL")
    assert len(totals) == 2
    assert grand[8].value == f"=SUM(I{totals[0]})"
    assert any(str(row[0].value).startswith("Annexure-II") for row in ws)


def test_ambiguous_word_code_never_prices_the_last_matching_row(db, breakdown):
    assert cbs.merge_batch_rates(db, breakdown.id, [
        {"item_code": "CONVERSION MAT", "schedule_name": "A", "rate": 123, "rate_source": "web_search"},
    ]) == {"matched": 0, "unmatched": 1}
    assert cbs.merge_batch_rates(db, breakdown.id, [
        {"item_code": "CONVERSION MAT", "schedule_name": "A", "sr_no": 4,
         "rate": 80, "amount": 999999, "margin_amount": 888888, "rate_source": "web_search"},
    ]) == {"matched": 1, "unmatched": 0}
    db.expire_all()
    row = next(r for r in breakdown.lines if r.schedule_name == "A" and r.sr_no == 4)
    assert row.amount == 160
    assert row.margin_amount == 40


def test_unpriced_export_keeps_the_tenders_reference_columns(db, breakdown):
    rows = cbs._xlsx_rows_from_breakdown(breakdown)
    row = next(r for r in rows if r["schedule_name"] == "A" and r["sr_no"] == 4)
    assert row["rate"] is None
    assert row["tender_rate"] == 100
    assert row["tender_amount"] == 200


def test_source_cells_prevent_tenfold_inflation():
    rows = [
        {"annexure_ref": "II", "quantity": 40, "printed_quantity": 4,
         "weight_per_item": None, "unit_rate": 25695.83, "basic_value": 1027833.3},
        {"annexure_ref": "II", "quantity": 24400.466, "printed_quantity": 4,
         "weight_per_item": 610, "unit_rate": 49.81, "basic_value": 1215387.2},
    ]
    bps._reconcile_annexure_quantities(rows)
    assert [r["quantity"] for r in rows] == [4, 2440]
    assert all(r["extraction_confidence"] == "low" for r in rows)


def test_printed_weight_percentage_is_computed_in_python():
    row = {"annexure_ref": "II", "printed_quantity": 2, "weight_per_item": 359.27,
           "quantity_percent": 80, "unit_rate": 39.18, "basic_value": 22523.19}
    bps._reconcile_annexure_quantities([row])
    assert row["quantity"] == pytest.approx(574.832)
    assert row["unit"] == "kg"


def test_equal_parts_in_different_assemblies_survive_both_dedup_passes():
    first = {"annexure_ref": "II", "sr_no": 1, "description": "Hook Lock", "quantity": 2,
             "basic_value": 5.63, "subassembly": "Door Frame Complete /21043"}
    second = {**first, "subassembly": "Door Frame Assembly /21044"}
    rows = bps._dedup_boq_rows([first, second, dict(first)])
    assert len(rows) == 2
    rows, dropped, _ = bps._collapse_value_redundant_twins(rows)
    assert len(rows) == 2 and dropped == 0


def test_distinct_labour_charge_is_kept_but_separately_scheduled_labour_is_not_counted_twice():
    rows = [
        {"schedule_name": "A", "description": "Material Cost (as per Annexure-II)", "unit_rate": 816051},
        {"schedule_name": "A", "description": "Labour Cost for Mechanical Furnishing", "unit_rate": 333949},
        {"annexure_ref": "II", "description": "Labour Cost (B)", "basic_value": 333949},
        {"annexure_ref": "III", "description": "Labour cost", "basic_value": 36386.76},
    ]
    assert bps._is_valid_schedule_item(rows[3], table_context=True)
    kept = bps._drop_separately_scheduled_labour(rows)
    assert rows[2] not in kept and rows[3] in kept


@pytest.mark.asyncio
async def test_numeric_transcription_repair_is_bounded_and_does_not_invent_a_quantity(monkeypatch):
    from app.services import ai_service
    calls = []
    bad = {"annexure": "II", "sr_no": 1, "description": "Sliding door", "printed_quantity": 40, "weight_per_item": None,
           "quantity": 40, "unit_rate": 25695.83, "basic_value": 1027833.3}
    async def respond(**kwargs):
        calls.append(kwargs)
        return json.dumps([bad])
    monkeypatch.setattr(ai_service, "call_ai_with_documents", respond)
    rows = await bps._call_boq_chunk(None, [{"page_num": 6, "text":
        "ANNEXURE-II\n1 Sliding door 4 set 25,695.83 1,02,783.33"}], None)
    assert len(calls) == 2
    rows[0]["annexure_ref"] = "II"
    bps._reconcile_annexure_quantities(rows)
    assert rows[0]["quantity"] is None
    assert rows[0]["unit_rate"] is None
    assert rows[0]["extraction_confidence"] == "low"


@pytest.mark.asyncio
async def test_numeric_repair_accepts_source_grounded_indian_amount(monkeypatch):
    from app.services import ai_service
    good = {"annexure": "II", "sr_no": 1, "description": "Sliding door", "printed_quantity": 4, "weight_per_item": None,
            "quantity": 4, "unit_rate": 25695.83, "basic_value": 102783.33}
    replies = iter([{**good, "basic_value": 1027833.3}, good])
    async def respond(**kwargs):
        return json.dumps([next(replies)])
    monkeypatch.setattr(ai_service, "call_ai_with_documents", respond)
    rows = await bps._call_boq_chunk(None, [{"page_num": 6, "text":
        "ANNEXURE-II\n1 Sliding door 4 set 25,695.83 1,02,783.33"}], None)
    assert rows == [good]


def test_printed_labour_footers_are_recovered_in_annexure_order():
    rows = [{"annexure": "II", "description": "Health faucet"},
            {"annexure": "III", "description": "Hand brake"}]
    text = "Health faucet\nLabour Cost (B)  ₹ 3,33,949.00\nANNEXURE-III\nHand brake\nLabour cost ₹ 36,386.76\nTotal Cost per coach ₹ 65,000.00"
    out = bps._recover_annexure_labour_rows(rows, text)
    assert [r["annexure"] for r in out] == ["II", "II", "III", "III"]
    assert out[1]["basic_value"] == 333949
    assert out[3]["basic_value"] == 36386.76
    assert len(bps._recover_annexure_labour_rows(out, text)) == 4


def test_total_only_material_is_a_named_lot_but_missing_quantity_is_not_invented():
    rows = [
        {"annexure_ref": "II", "description": "LP sheet", "quantity": None,
         "unit": None, "basic_value": 50769.01},
        {"annexure_ref": "VII", "description": "Paint", "quantity": None,
         "unit": "LTR", "unit_rate": 200},
    ]
    bps._reconcile_annexure_quantities(rows)
    assert rows[0]["quantity"] == 1
    assert rows[0]["unit"] == "Lot (printed total)"
    assert rows[0]["unit_rate"] == 50769.01
    assert rows[1]["quantity"] is None


def test_reference_fallback_preserves_evidenced_equal_rate_and_unknown_quantity(db):
    bd = CostBreakdown(tender_id=next(_ids))
    db.add(bd)
    db.flush()
    known = CostBreakdownLine(cost_breakdown_id=bd.id, description="Supplier quote", quantity=2,
                              rate=100, tender_rate=100, amount=200, needs_input=False,
                              rate_source="web_search", source_url="https://supplier.example/quote")
    unknown = CostBreakdownLine(cost_breakdown_id=bd.id, description="Missing quantity", quantity=None,
                                tender_rate=100, needs_input=True)
    db.add_all([known, unknown])
    db.commit()
    assert cbs.normalize_copied_rates(db, bd.id, 10, 15) == 0
    assert known.rate == 100
    assert unknown.amount is None and unknown.needs_input


def test_incomplete_or_loss_making_costing_does_not_recommend_a_bid(db, breakdown):
    summary = cbs.build_strategic_summary(db, breakdown)
    assert "incomplete" in summary["recommended_bid_strategy"]
    for line in breakdown.lines:
        line.needs_input = False
        line.rate = 10000
        line.amount = line.quantity * 10000
    db.flush()
    summary = cbs.build_strategic_summary(db, breakdown)
    assert "exceeds the tender value" in summary["recommended_bid_strategy"]
