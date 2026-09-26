"""Tender 5157: every annexure row is held to the total the material list prints.

The cells below are the ones the extractor returned for the Liluah
Annexure-II/III (MaterialListICFtoNMGHSR), and the page text is the vision
text of those pages, trimmed to the rows under test.
"""
import itertools

import pytest

from app.models.costing_template import BOQItem
from app.models.tender import Tender
from app.services import boq_parser_service as bps
from app.services import cost_breakdown_service as cbs

_ids = itertools.count(957000)


def _settle(*rows):
    rows = [{"annexure_ref": "II", **r} for r in rows]
    bps._reconcile_annexure_quantities(rows)
    return rows


# --- the printed total is the check, and it is enforced --------------------

def test_an_assembly_price_read_into_a_parts_weight_cell_is_one_lot():
    # "SL to CSK HD Screw M6x30 | 64 | kg | ... | 4,360.13 | ₹ 4,360.13": the
    # merged Rs 4,360.13 of five parts, read as 64 x 4,360.13 kg of screws.
    (row,) = _settle({"sr_no": 3, "description": "SL to CSK HD Screw M6x30", "printed_quantity": 64,
                      "weight_per_item": 4360.13, "unit": "kg", "unit_rate": 4360.13, "basic_value": 4360.13})
    assert row["quantity"] == 1
    assert row["unit"] == "Lot (printed total)"
    assert row["unit_rate"] == 4360.13
    assert row["_merged_assembly_price"] is True


def test_a_drawing_length_in_the_qty_cell_gives_way_to_the_printed_total():
    # "15304 mm (80%)" above the real count 2: 4.4 million kg of plate.
    (row,) = _settle({"description": "Chequered Plate 6 x 682 x 19017", "printed_quantity": 15304,
                      "weight_per_item": 359.27, "quantity_percent": 80, "unit_rate": 39.18,
                      "basic_value": 22523.19})
    assert row["quantity"] == pytest.approx(574.864)
    assert row["quantity"] * row["unit_rate"] == pytest.approx(22523.19, abs=0.5)
    assert row["unit"] == "kg"
    assert row["extraction_confidence"] == "low"


def test_rate_in_the_weight_cell_and_total_in_the_rate_cell_is_a_count_at_the_rate():
    # "Angle 6 mm x 3249 x 64*48 | 4 | kg | IS: 2062 | | ₹ 536.38 | ₹ 2,145.53"
    (row,) = _settle({"description": "Angle 6 mm x 3249 x 64*48", "printed_quantity": 4,
                      "weight_per_item": 536.38, "unit": "kg", "unit_rate": 2145.53, "basic_value": None})
    assert (row["quantity"], row["unit"], row["unit_rate"], row["basic_value"]) == (4, "Nos", 536.38, 2145.53)
    assert row["weight_per_item"] is None


def test_rate_in_the_weight_cell_with_the_total_read_right_is_a_count_at_the_rate():
    # Louver frame arrangement for NMGH coach: 8 Nos at Rs 1,213.05 = Rs 9,704.41.
    (row,) = _settle({"description": "Louver frame arrangement", "printed_quantity": 8, "unit": "Nos",
                      "weight_per_item": 1213.05, "unit_rate": 9704.41, "basic_value": 9704.41})
    assert (row["quantity"], row["unit_rate"], row["basic_value"]) == (8, 1213.05, 9704.41)


def test_consistent_weight_rows_are_untouched():
    rows = _settle(
        {"description": "Channel - 1", "printed_quantity": 12, "weight_per_item": 5.208,
         "unit": "kg", "unit_rate": 29.29, "basic_value": 1830.54},
        {"description": "Rib (3.15 mm thick)", "printed_quantity": 8, "weight_per_item": 0.017,
         "unit": "kg", "unit_rate": 29.29, "basic_value": 3.98},
        {"description": "Lock for barrel", "printed_quantity": 2, "weight_per_item": 0.2,
         "unit": "kg", "unit_rate": 1861.29, "basic_value": 744.52},
    )
    assert [r["quantity"] for r in rows] == [pytest.approx(62.496), pytest.approx(0.136), pytest.approx(0.4)]
    assert [r["unit_rate"] for r in rows] == [29.29, 29.29, 1861.29]
    assert all(r.get("extraction_confidence") != "low" for r in rows)


def test_a_weight_that_happens_to_match_is_not_taken_for_a_rate_when_the_row_already_agrees():
    # 5 x 4 = 20 is the rate by coincidence; the row's own total settles it.
    (row,) = _settle({"description": "Plate", "printed_quantity": 5, "weight_per_item": 4,
                      "unit": "kg", "unit_rate": 20, "basic_value": 400})
    assert (row["quantity"], row["unit"], row["unit_rate"]) == (20, "kg", 20)


def test_one_printed_amount_in_the_rate_cell_is_a_lot_not_an_unpriced_row():
    # Guard room: "11 Bib Cock ... ₹ 1,965.25", read into the rate cell.
    (row,) = _settle({"description": "Bib Cock", "printed_quantity": None, "weight_per_item": None,
                      "unit": None, "unit_rate": 1965.25, "basic_value": None})
    assert (row["quantity"], row["unit"], row["unit_rate"], row["basic_value"]) == \
        (1, "Lot (printed total)", 1965.25, 1965.25)


def test_a_row_the_list_totals_at_nil_adds_nothing():
    (row,) = _settle({"description": "Step sheet at Doorn pillar 3.15 mm", "printed_quantity": 8,
                      "weight_per_item": None, "unit": None, "unit_rate": 181.58, "basic_value": 0.0,
                      "_printed_nil_total": True})
    assert row["quantity"] == 0
    assert row["description"].endswith("(printed total Rs 0.00)")
    # Idempotent: a second pass does not stack the note.
    bps._reconcile_annexure_quantities([row])
    assert row["description"].count("Rs 0.00") == 1


# --- page text: rupee amounts, wrapped cells, dropped rates ----------------

_SIDE_WALL = (
    "3         louvre frame                  8                 RDSO/CG/DRG                  ₹                ₹ 0.00\n"
    "          arrangement 3.15                                /20008            1,213.04\n"
    "          mm\n"
    "4         Step sheet at Doorn           8                 ICF/STD1-0-                  ₹ 181.58        ₹ 0.00\n"
    "5         Door corner sheet LH  2   kg  LB 14128   13.65   ₹ 55.26   ₹ 1,508.72\n"
)


def test_a_rupee_amount_wrapped_under_its_sign_is_money_and_a_weight_is_not():
    money, plain = bps._money_marked_numbers(_SIDE_WALL)
    assert {1213.04, 181.58, 55.26, 1508.72, 0.0} <= money
    assert 13.65 in plain and 13.65 not in money
    assert 1213.04 in bps._money_only_numbers(_SIDE_WALL)


def test_the_wrapped_rate_in_a_weight_cell_prices_the_count():
    (row,) = _settle({"description": "louvre frame arrangement 3.15 mm", "printed_quantity": 8,
                      "weight_per_item": 1213.04, "unit": None, "unit_rate": None, "basic_value": None,
                      "_weight_cell_is_money": True})
    assert (row["quantity"], row["unit"], row["unit_rate"]) == (8, "Nos", 1213.04)


def test_a_dropped_kg_rate_is_restored_from_the_page_only_when_it_divides_exactly():
    text = "4  Top Angle  4  RDSO/CG/DRG  15.97  ₹ 29.29  ₹ 1,871.07\n7 Angle-1 8x65 8 kg 10.715 ₹ 35.97 ₹ 3,083.55"
    money, _ = bps._money_marked_numbers(text)
    dropped = {"description": "Top Angle", "printed_quantity": 4, "weight_per_item": 15.97,
               "unit_rate": 1871.07, "basic_value": None}
    genuine = {"description": "Angle", "printed_quantity": 8, "weight_per_item": 10.715,
               "unit_rate": 35.97, "basic_value": None}
    bps._restore_dropped_weight_rates([dropped, genuine], money)
    assert (dropped["unit_rate"], dropped["basic_value"]) == (29.29, 1871.07)
    assert (genuine["unit_rate"], genuine["basic_value"]) == (35.97, None)


# --- an assembly's one price is not added to its parts ----------------------

def _sliding_door(subassembly):
    return [
        {"annexure_ref": "II", "sr_no": 1, "description": "Internal Locking Arrangement", "printed_quantity": 4,
         "unit": "kg", "subassembly": subassembly},
        {"annexure_ref": "II", "sr_no": 2, "description": "External Locking Arrangement", "printed_quantity": 4,
         "unit": "kg", "subassembly": subassembly},
        {"annexure_ref": "II", "sr_no": 3, "description": "SL to CSK HD Screw M6x30", "printed_quantity": 64,
         "weight_per_item": 4360.13, "unit": "kg", "unit_rate": 4360.13, "basic_value": 4360.13,
         "subassembly": subassembly},
        {"annexure_ref": "II", "sr_no": 4, "description": "SL to PAN HD TAP Screw", "printed_quantity": 48,
         "unit": "kg", "subassembly": subassembly},
        {"annexure_ref": "II", "sr_no": 5, "description": "COVER ANGLE 3.15", "printed_quantity": 4,
         "subassembly": subassembly},
        {"annexure_ref": "II", "sr_no": 1, "description": "Rib 3.15 x 136 x 171", "printed_quantity": 24,
         "weight_per_item": 0.604, "unit": "kg", "unit_rate": 29.95, "basic_value": 434.21,
         "subassembly": "SUPPORT COMPLETE FOR SLIDING DOOR"},
    ]


@pytest.mark.parametrize("subassembly", ["MOUNTING OF SLIDING DOOR (RDSO/CG/DRG/21014)", None])
def test_parts_of_a_merged_price_fold_into_the_one_lot(subassembly):
    rows = _sliding_door(subassembly)
    bps._reconcile_annexure_quantities(rows)
    out = bps._fold_merged_assembly_parts(rows)
    assert len(out) == 2
    lot = out[0]
    assert (lot["quantity"], lot["unit_rate"]) == (1, 4360.13)
    for part in ("Internal Locking", "External Locking", "CSK HD Screw", "PAN HD TAP", "COVER ANGLE"):
        assert part in lot["description"]
    assert out[1]["description"].startswith("Rib")


def test_parts_are_not_folded_when_another_part_prints_its_own_price():
    rows = _sliding_door("MOUNTING OF SLIDING DOOR")
    rows[1].update(unit_rate=55.0, basic_value=220.0)
    bps._reconcile_annexure_quantities(rows)
    assert len(bps._fold_merged_assembly_parts(rows)) == len(rows)


# --- a labour footer belongs to the annexure it is printed in ---------------

_PAGE_13 = (
    "11  Bib Cock                                  ₹ 1,965.25\n"
    "20  Health Faucet.                            ₹ 982.63\n"
    "    Total Material cost for Guard Room        ₹ 1,81,892.26\n"
    "          Total Material cost (1. to 30.) A   ₹ 8,16,051.00\n"
    "          Labour Cost (B)                     ₹ 3,33,949.00\n"
    "          Cost of Work (A+B)/Coach            ₹ 11,50,000.00\n\n"
    "                                              ANNEXURE-III\n"
    "1  Complete Pull Rod for Hand Brake  RDSO  1  Set  ₹ 405.02  ₹ 405.02\n"
    "          Total Material Cost/Coach           ₹ 28,613.24\n"
    "          Labour cost                         ₹ 36,386.76\n"
    "          Total Cost per coach                ₹ 65,000.00\n"
)


def test_a_footer_above_the_next_heading_stays_with_the_annexure_it_closes():
    # The continuation rows are unlabelled and the extractor gave the footer
    # the page's heading: Annexure-II's labour became Annexure-III's.
    rows = [
        {"sr_no": 11, "description": "Bib Cock", "annexure": None, "basic_value": 1965.25},
        {"sr_no": 20, "description": "Health Faucet.", "annexure": None, "basic_value": 982.63},
        {"sr_no": None, "description": "Labour Cost (B)", "annexure": "III", "basic_value": 333949.0},
        {"sr_no": 1, "description": "Complete Pull Rod for Hand Brake", "annexure": "III",
         "quantity": 1, "unit": "Set", "unit_rate": 405.02, "basic_value": 405.02},
    ]
    out = bps._recover_annexure_labour_rows(rows, _PAGE_13, "II")
    labour = [(r["annexure"], r["basic_value"]) for r in out if "abour" in r["description"]]
    assert labour == [("II", 333949.0), ("III", 36386.76)]
    assert [r["description"] for r in out] == [
        "Bib Cock", "Health Faucet.", "Labour Cost (B)", "Complete Pull Rod for Hand Brake", "Labour cost"]
    assert bps._recover_annexure_labour_rows(out, _PAGE_13, "II") == out


@pytest.mark.asyncio
async def test_each_chunk_is_told_the_annexure_its_first_page_continues(monkeypatch):
    pages = [
        {"page_num": 1, "text": "ANNEXURE-II\n1 Channel 2 kg 7.856 ₹ 29.29 ₹ 460.21"},
        {"page_num": 2, "text": "11 Bib Cock ₹ 1,965.25\nANNEXURE-III\n1 Pull Rod 1 Set ₹ 405.02 ₹ 405.02"},
    ]
    seen = []

    async def fake_call(db, chunk_pages, carry):
        seen.append([p.get("annexure_carry") for p in chunk_pages])
        return []

    monkeypatch.setattr(bps, "_call_boq_chunk", fake_call)
    await bps._walk_pages_chunked(None, pages, [None, None], [0, 1], pages_per_chunk=4, max_retries=0)
    assert seen == [[None], ["II"]]


def test_a_numbered_priced_row_ending_in_a_full_stop_is_kept():
    for desc, value in (("Health Faucet.", 982.63), ("SS mug with Chain and Ring.", 1023.57)):
        assert bps._is_valid_schedule_item(
            {"sr_no": 20, "description": desc, "annexure_ref": "II", "basic_value": value}, table_context=True)
    clause = ("The contractor shall ensure that all the material supplied is as per the approved "
              "drawing and specification and shall bear the cost of any rejection.")
    assert not bps._is_valid_schedule_item(
        {"sr_no": 5, "description": clause, "basic_value": 500}, table_context=True)


# --- a schedule captured before this fix is held to its totals at costing ---

@pytest.mark.parametrize("args, expected", [
    ((279048.32, "kg", 4360.13, 4360.13), (1.0, "Lot (printed total)", 4360.13)),
    ((4398614.464, "kg", 39.18, 22523.19), (574.864, "kg", 39.18)),
    ((None, None, None, 1965.25), (1.0, "Lot (printed total)", 1965.25)),
    ((12.0, "kg", 29.29, 351.48), (12.0, "kg", 29.29)),
    # A clean power of ten apart: a shifted digit, side unknown -- left.
    ((2440.0, "kg", 49.81, 1215387.2), (2440.0, "kg", 49.81)),
    ((None, "LTR", 200.0, 4000.0), (None, "LTR", 200.0)),
    ((8.0, None, 181.58, 0.0), (8.0, None, 181.58)),
])
def test_component_basis_from_printed_total(args, expected):
    assert cbs.component_basis_from_printed_total(*args) == expected


def test_the_costing_skeleton_prices_a_misread_component_by_its_printed_total(db):
    tid = next(_ids)
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="Liluah 5157", source_url="x"))
    parent = BOQItem(tender_id=tid, sr_no=3, item_code="CONVERSION MAT", schedule_name="A", quantity=15,
                     unit="Coach Set", estimated_rate=816051, description="Material Cost (As per Annexure-II)")
    db.add(parent)
    db.flush()
    db.add_all([
        BOQItem(tender_id=tid, sr_no=1, item_code="ANX-II-35", annexure_ref="II", parent_item_id=parent.id,
                quantity=279048.32, unit="kg", estimated_rate=4360.13, basic_value=4360.13,
                description="SL to CSK HD Screw M6x30"),
        BOQItem(tender_id=tid, sr_no=2, item_code="ANX-II-54", annexure_ref="II", parent_item_id=parent.id,
                quantity=4398614.464, unit="kg", estimated_rate=39.18, basic_value=22523.19,
                description="Chequered Plate 6 x 682 x 19017"),
        # A schedule row is never rewritten, whatever its value says.
        BOQItem(tender_id=tid, sr_no=4, item_code="CONVERSION MAT", schedule_name="A", quantity=300,
                unit="Metre", estimated_rate=9.48, basic_value=1.0, description="Steel wire rope"),
    ])
    db.commit()
    bd = cbs.build_skeleton_from_boq(db, tid)
    lines = {ln.item_code: ln for ln in bd.lines}
    by_desc = {ln.description: ln for ln in bd.lines}
    screw, plate = lines["ANX-II-35"], lines["ANX-II-54"]
    assert (screw.quantity, screw.unit, screw.tender_rate) == (1.0, "Lot (printed total)", 4360.13)
    assert (plate.quantity, plate.tender_rate) == (574.864, 39.18)
    assert by_desc["Steel wire rope"].quantity == 300


@pytest.mark.asyncio
async def test_a_zero_total_is_believed_only_as_often_as_the_page_prints_one(monkeypatch):
    import json
    from app.services import ai_service
    text = ("ANNEXURE-II\n"
            "3  louvre frame  8  RDSO/CG/DRG  ₹ 1,213.04  ₹ 0.00\n"
            "4  Step sheet at Doorn pillar  8  ICF/STD1-0-012  ₹ 181.58\n")
    base = {"annexure": "II", "weight_per_item": None, "unit": None}
    reply = [
        {**base, "sr_no": 3, "description": "louvre frame", "printed_quantity": 8, "quantity": 8,
         "unit_rate": 1213.04, "basic_value": 0.0},
        {**base, "sr_no": 4, "description": "Step sheet at Doorn pillar", "printed_quantity": 8, "quantity": 8,
         "unit_rate": 181.58, "basic_value": 0.0},
    ]

    async def respond(**kwargs):
        return json.dumps(reply)

    monkeypatch.setattr(ai_service, "call_ai_with_documents", respond)
    rows = await bps._call_boq_chunk(None, [{"page_num": 2, "text": text}], None)
    # Two zero totals, one printed: neither is believed.
    assert not any(r.get("_printed_nil_total") for r in rows)

    reply.pop()
    rows = await bps._call_boq_chunk(None, [{"page_num": 2, "text": text}], None)
    assert [r.get("_printed_nil_total") for r in rows] == [True]


# --- the second 5157 workbook ----------------------------------------------

def test_a_count_printed_under_kg_with_no_weight_is_a_count():
    # "Angle 6 mm x 3249 x 64*48 | 4 | kg | IS: 2062 | (no weight) | ₹ 536.38 | ₹ 2,145.53"
    (row,) = _settle({"description": "Angle 6 mm x 3249 x 64*48", "printed_quantity": 4, "weight_per_item": None,
                      "unit": "kg", "unit_rate": 536.38, "basic_value": 2145.53})
    assert (row["quantity"], row["unit"]) == (4, "Nos")
    (weighed,) = _settle({"description": "Channel", "printed_quantity": 4, "weight_per_item": 3.355,
                          "unit": "kg", "unit_rate": 29.29, "basic_value": 393.08})
    assert weighed["unit"] == "kg"


def test_a_nil_total_component_is_costed_at_nil_and_not_sent_for_research(db):
    from app.services.langchain.graphs.enhanced_costing_agent import split_costable_rows
    tid = next(_ids)
    db.add(Tender(id=tid, portal="ireps", tender_id=str(tid), title="Liluah 5157", source_url="x"))
    parent = BOQItem(tender_id=tid, sr_no=3, item_code="CONVERSION MAT", schedule_name="A", quantity=15,
                     unit="Coach Set", estimated_rate=816051, description="Material Cost (As per Annexure-II)")
    db.add(parent)
    db.flush()
    db.add_all([
        BOQItem(tender_id=tid, sr_no=3, item_code="ANX-II-9", annexure_ref="II", parent_item_id=parent.id,
                quantity=0.0, unit="Nos", estimated_rate=1213.04, basic_value=0.0,
                description="louvre frame arrangement 3.15 mm (printed total Rs 0.00)"),
        BOQItem(tender_id=tid, sr_no=5, item_code="ANX-II-11", annexure_ref="II", parent_item_id=parent.id,
                quantity=27.3, unit="kg", estimated_rate=55.26, basic_value=1508.72, description="Door corner sheet"),
    ])
    db.commit()
    bd = cbs.build_skeleton_from_boq(db, tid)
    nil = next(ln for ln in bd.lines if ln.item_code == "ANX-II-9")
    assert nil.needs_input is False
    assert nil.rate_source == cbs.PRINTED_NIL_SOURCE
    assert nil.amount == 0 and nil.quantity == 0
    other = next(ln for ln in bd.lines if ln.item_code == "ANX-II-11")
    assert other.needs_input is True
    # The reference fallback leaves it alone; the summary does not call it incomplete.
    cbs.normalize_copied_rates(db, bd.id, overhead_pct=10, margin_pct=15)
    db.expire_all()
    assert nil.rate_source == cbs.PRINTED_NIL_SOURCE
    summary = cbs.build_strategic_summary(db, bd, tender_id=tid)
    assert not any("need manual pricing" in o and o.startswith("2 ") for o in summary["key_observations"])
    rows, _ = split_costable_rows([
        {"boq_item_id": nil.boq_item_id, "parent_item_id": parent.id, "quantity": 0.0},
        {"boq_item_id": other.boq_item_id, "parent_item_id": parent.id, "quantity": 27.3},
    ])
    assert [r["boq_item_id"] for r in rows] == [other.boq_item_id]


def test_the_summary_says_how_much_of_the_cost_is_formula_not_research(db):
    from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
    bd = CostBreakdown(tender_id=next(_ids))
    db.add(bd)
    db.flush()
    db.add_all([
        CostBreakdownLine(cost_breakdown_id=bd.id, sr_no=1, schedule_name="B", description="Conversion",
                          quantity=20, tender_rate=2430440, tender_amount=48608800, rate=1944352,
                          amount=38887040, rate_source="derived_estimate", needs_input=False),
        CostBreakdownLine(cost_breakdown_id=bd.id, sr_no=2, schedule_name="B", description="Paint",
                          quantity=20, tender_rate=22547.44, tender_amount=450948.8, rate=36880,
                          amount=737600, rate_source="component_buildup", needs_input=False),
    ])
    db.commit()
    db.refresh(bd)
    obs = cbs.build_strategic_summary(db, bd)["key_observations"]
    line = next(o for o in obs if "by formula" in o)
    assert "1 line item(s), Rs 38,887,040 (98% of the estimated cost)" in line
    assert "Margin on the researched lines alone: -63.6%" in line
