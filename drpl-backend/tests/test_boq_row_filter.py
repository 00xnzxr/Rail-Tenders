"""The schedule-row filter must not throw away priced lines.

On a Rs 12.9 crore IREPS tender the extractor produced rows that reconciled to
the printed schedule total EXACTLY (Rs 129,099,224.00, delta 0.0%). The filter
then dropped 17 of the surviving 18 as "non-schedule/prose", leaving one row to
cost against. The run reported success and produced a plausible-looking Excel
that was missing nearly the whole scope — the worst shape of failure, because
nothing looks wrong.

The dropped rows carried a value and a schedule but no quantity (lump-sum
lines) and no separately-parsed item code (it sat inside the description). The
filter's branches required a code, or a quantity AND a rate, so a priced row
with neither fell through.

Widening the filter puts more weight on the prose guard, so these tests spend
most of their effort proving prose is still rejected.
"""

import pytest

from app.services.boq_parser_service import _is_valid_schedule_item


def row(**kw):
    base = {
        "description": "Supply and fitment of SS metal trough",
        "schedule_name": "A",
    }
    base.update(kw)
    return base


# ── the regression ──────────────────────────────────────────────────────────


def test_priced_schedule_row_without_qty_or_code_is_kept():
    """The exact shape that was being discarded."""
    assert _is_valid_schedule_item(
        row(item_code=None, quantity=None, basic_value=3_604_012.65)
    )


def test_lump_sum_row_with_an_amount_is_kept():
    assert _is_valid_schedule_item(
        row(item_code=None, quantity=None, amount=129_099_224.00)
    )


def test_row_with_a_rate_but_no_quantity_is_kept():
    assert _is_valid_schedule_item(
        row(item_code=None, quantity=None, unit_rate=1500.0)
    )


# ── the guard I am now leaning on ────────────────────────────────────────────


@pytest.mark.parametrize(
    "prose",
    [
        "All the bidders should ensure they are GST compliant before bidding",
        "The bidder shall submit an undertaking that the firm is not blacklisted",
        "Documents attached with tender are to be signed on each page",
    ],
)
def test_prose_is_still_rejected_even_with_a_value(prose):
    """Widening the filter puts the weight on this check. A stray number
    mis-parsed onto an eligibility clause must not become a priced line."""
    assert not _is_valid_schedule_item(
        row(description=prose, item_code=None, quantity=None, basic_value=50_000.0)
    )


def test_prose_is_rejected_even_when_it_mentions_tax():
    """Eligibility text trips the tax-line keyword detector but is not a tax
    row — the prose check runs first for exactly this reason."""
    assert not _is_valid_schedule_item(
        row(
            description="All bidders should ensure they are GST compliant",
            item_code=None, quantity=None, basic_value=1000.0,
        )
    )


# ── the new branch must not admit unpriced rows ─────────────────────────────


def test_row_with_no_value_at_all_is_still_rejected():
    """A schedule tag alone is not a pricing signal."""
    assert not _is_valid_schedule_item(
        row(item_code=None, quantity=None, basic_value=None)
    )


def test_zero_value_row_is_rejected():
    assert not _is_valid_schedule_item(
        row(item_code=None, quantity=None, basic_value=0)
    )


def test_priced_row_outside_any_schedule_is_rejected():
    """Without a schedule tag the row did not come from the schedule section,
    so a bare number is not enough to call it a line item."""
    assert not _is_valid_schedule_item(
        {"description": "Some heading", "schedule_name": None,
         "item_code": None, "quantity": None, "basic_value": 5000.0}
    )


def test_non_numeric_value_does_not_count_as_priced():
    assert not _is_valid_schedule_item(
        row(item_code=None, quantity=None, basic_value="not a number")
    )


# ── previously-working cases must keep working ──────────────────────────────


def test_alphanumeric_code_is_still_enough_on_its_own():
    assert _is_valid_schedule_item(
        row(item_code="A71", quantity=None, basic_value=None)
    )


def test_word_code_with_a_pricing_signal_is_still_kept():
    assert _is_valid_schedule_item(
        row(item_code="Mechanical", quantity=None, basic_value=1200.0)
    )


def test_qty_and_rate_without_a_code_is_still_kept():
    assert _is_valid_schedule_item(
        row(item_code=None, quantity=1700.0, unit_rate=250.0, schedule_name=None)
    )


def test_tax_line_is_still_kept():
    assert _is_valid_schedule_item(
        {"description": "GST @ 18%", "is_tax_line": True,
         "item_code": None, "quantity": None}
    )


def test_the_row_that_survived_the_real_run_still_survives():
    """The single row the broken filter kept must not regress."""
    assert _is_valid_schedule_item({
        "description": "Supply & Fitment SS Metal RMPU Trough below RMPU in "
                       "place of FRP Trough",
        "quantity": 1700.0, "item_code": None, "schedule_name": "A",
        "basic_value": 129_099_224.00,
    })


# ── the widened prose guard must not eat real work items ────────────────────
#
# The asymmetry matters: a prose row that slips through becomes a visible line
# the user can delete, while a real row dropped here is invisible and silently
# shrinks the scope. So this guard is biased toward keeping.


@pytest.mark.parametrize(
    "description",
    [
        "Supply & Fitment SS Metal RMPU Trough below RMPU in place of FRP Trough",
        "Engine removal and refitment including all labour and consumables",
        "Provision of GST on the above schedule",
        "Cost of spares required during overhaul",
        "Copies of approved drawings to be supplied in triplicate",
        "Copy of test certificates for each fitted assembly",
        "Documentation charges for as-built drawings",
        "General purpose bracket fabrication and fitment",
        "Special tools required for RMPU removal",
        "Termination of cables and glanding work",
    ],
)
def test_real_schedule_items_are_not_treated_as_prose(description):
    assert _is_valid_schedule_item(
        row(description=description, item_code=None, quantity=None,
            basic_value=125_000.0)
    ), f"a real work item was rejected as prose: {description!r}"


# ── the price-bid shape: the tender prints the scope, the BIDDER prices it ──
#
# Issue #7, the Liluah tender. An annexure of 30 items contributed 6 to the
# costing. The 6 were the ones that happened to print a serial in the Item
# Code column; the other 24 were the ordinary shape of a schedule of
# quantities — description, quantity, unit, and Rate/Amount left EMPTY because
# quoting them is the bidder's job. Every branch of this filter demanded
# either a code or a printed number, so the rows this platform exists to price
# were the exact rows it discarded.


def bid(**kw):
    """A row as a price bid prints it: no code, no rate, no amount."""
    base = {
        "description": "Supply, installation and commissioning of 415V LT panel",
        "item_code": None,
        "quantity": 10,
        "unit": "Nos",
        "unit_rate": None,
        "basic_value": None,
        "amount": None,
        "schedule_name": None,
    }
    base.update(kw)
    return base


def test_the_price_bid_row_is_kept():
    """The shape that was being discarded — and the whole of issue #7."""
    assert _is_valid_schedule_item(bid())


def test_it_is_kept_inside_a_schedule_too():
    assert _is_valid_schedule_item(bid(schedule_name="A"))


@pytest.mark.parametrize(
    "description,quantity,unit",
    [
        ("Supply of MS structural steel work", 25.5, "MT"),
        ("Earthwork in excavation for foundation", 120, "Cum"),
        ("Dismantling of existing shed including disposal", 1, "Job"),
        ("Providing and laying cement concrete flooring", 340.75, "Sqm"),
        ("Laying of underground cable", 2.4, "Km"),
        ("Painting of steel structure two coats", 900, "Square Metre"),
        ("Supply of transformer oil", 1500, "Litre"),
    ],
)
def test_the_ordinary_shapes_of_a_schedule_of_quantities(description, quantity, unit):
    assert _is_valid_schedule_item(
        bid(description=description, quantity=quantity, unit=unit)
    ), f"a bidder-priced work item was rejected: {description!r}"


def test_the_reported_annexure_is_now_read_whole():
    """Annexure 2 as reported: 30 items, 6 of them printing a serial in the
    Item Code column. Before this branch the filter kept those 6."""
    rows = [
        bid(description=f"Item {n} of the bidding schedule",
            quantity=n, unit="Nos",
            item_code=str(n) if n % 5 == 0 else None)
        for n in range(1, 31)
    ]
    kept = [r for r in rows if _is_valid_schedule_item(r)]
    coded = [r for r in rows if r["item_code"]]

    assert len(coded) == 6, "fixture no longer mirrors the report"
    assert len(kept) == 30, (
        f"{30 - len(kept)} of 30 annexure items would still be dropped"
    )


# ── the quantity alone is deliberately not enough ───────────────────────────


def test_a_quantity_with_no_unit_is_not_enough():
    """A stray number lands on an eligibility clause often enough that this
    branch must not rest on the quantity by itself."""
    assert not _is_valid_schedule_item(bid(unit=None))
    assert not _is_valid_schedule_item(bid(unit=""))


def test_a_unit_with_no_quantity_is_not_enough():
    assert not _is_valid_schedule_item(bid(quantity=None))


def test_a_zero_or_negative_quantity_is_not_a_quantity():
    assert not _is_valid_schedule_item(bid(quantity=0))
    assert not _is_valid_schedule_item(bid(quantity=-5))


def test_a_row_with_no_description_is_not_a_line_item():
    assert not _is_valid_schedule_item(bid(description=""))
    assert not _is_valid_schedule_item(bid(description=None))


def test_a_sentence_that_landed_in_the_unit_column_is_not_a_unit():
    """Merged cells put strange things in the unit column. A unit of measure
    is short; a clause is not."""
    assert not _is_valid_schedule_item(
        bid(unit="as per the specification enclosed with this tender document")
    )


def test_a_number_that_landed_in_the_unit_column_is_not_a_unit():
    """A mis-mapped column can put the rate where the unit belongs. Requiring
    a letter rejects that without rejecting 'Nos' or 'Cum'."""
    assert not _is_valid_schedule_item(bid(unit="1500"))
    assert not _is_valid_schedule_item(bid(unit="-"))


# ── the prose guard still comes first ───────────────────────────────────────


@pytest.mark.parametrize(
    "prose",
    [
        "All the bidders should ensure they are GST compliant before bidding",
        "The bidder shall submit an undertaking that the firm is not blacklisted",
        "Documents attached with tender are to be signed on each page",
        "Note:- rates quoted shall be inclusive of all taxes",
    ],
)
def test_prose_with_a_quantity_and_a_unit_is_still_rejected(prose):
    """The widened branch puts more weight here, so this is the check that
    has to hold: an eligibility table row carrying '3 Nos' must not become a
    priced line."""
    assert not _is_valid_schedule_item(
        bid(description=prose, quantity=3, unit="Nos")
    )


def test_the_extraction_prompt_agrees_with_the_filter():
    """Both told the model the same wrong thing: a row needs a code or a
    printed rate. Widening the filter alone would leave the AI pass never
    emitting the rows for the filter to keep."""
    from app.services.boq_parser_service import _BOQ_AI_SYSTEM_PROMPT as P

    assert "both an Item Qty and a Unit Rate" not in P
    assert "A BLANK Unit Rate does NOT make a row invalid" in P
    # The prose exclusions must survive the rewrite.
    assert "DO NOT extract prose" in P
    assert "eligibility criteria" in P


# ── a materials list with no quantity column ────────────────────────────────
#
# The third Liluah report. The annexures were material lists, and a material
# list can print `Sr | Description of Material | Specification` and nothing
# else: no code, no rate, no quantity. Every branch of the filter needed a
# number the table does not print, so every row was discarded. Costing needs
# those rows -- the rate is researched per item; the amount is the user's.
#
# The serial number is what makes such a row a table row. It is a weaker
# signal than a quantity, so the prose guard learned the openers of the
# numbered clauses it could otherwise admit, and most of these tests are
# about what must STILL be rejected.


def mat(**kw):
    base = {
        "sr_no": 3,
        "item_code": None,
        "description": "Synthetic enamel paint, signal red, conforming to IS 2932",
        "quantity": None,
        "unit": None,
        "unit_rate": None,
        "basic_value": None,
        "amount": None,
        "schedule_name": None,
    }
    base.update(kw)
    return base


def test_a_serial_numbered_material_row_is_kept():
    assert _is_valid_schedule_item(mat())


def test_a_serial_numbered_row_with_a_unit_but_no_quantity_is_kept():
    assert _is_valid_schedule_item(mat(unit="Litre"))


def test_the_serial_may_arrive_as_text():
    assert _is_valid_schedule_item(mat(sr_no="7"))
    assert _is_valid_schedule_item(mat(sr_no=7.0))


def test_a_materials_list_with_no_quantity_column_is_read_whole():
    rows = [
        mat(sr_no=n, description=f"Material item {n}, conforming to IS {2000 + n}")
        for n in range(1, 31)
    ]
    assert sum(map(_is_valid_schedule_item, rows)) == 30


# ── what makes it a table row is the serial, and the description's shape ──


def test_a_description_with_no_serial_is_not_enough():
    assert not _is_valid_schedule_item(mat(sr_no=None))
    assert not _is_valid_schedule_item(mat(sr_no=0))       # pdfplumber: column absent
    assert not _is_valid_schedule_item(mat(sr_no="n/a"))


def test_a_sentence_is_not_a_cell():
    assert not _is_valid_schedule_item(mat(
        description="The material shall conform to the relevant IS specification "
                    "and be supplied with a test certificate from the manufacturer."
    ))


def test_a_long_run_of_words_is_not_a_cell():
    assert not _is_valid_schedule_item(mat(description=" ".join(["word"] * 25)))


def test_a_cell_ending_in_a_full_stop_is_treated_as_prose():
    assert not _is_valid_schedule_item(mat(description="Primer, red oxide."))


# ── the numbered clauses this branch could admit, and must not ───────────────


@pytest.mark.parametrize(
    "clause",
    [
        "Minimum annual turnover of Rs 50 lakh in any of the last three years",
        "Minimum turnover Rs 2 crore",
        "Similar work experience of three years",
        "Past experience in railway coach painting",
        "Earnest money deposit Rs 1,25,000",
        "EMD as per NIT",
        "Security deposit 5% of contract value",
        "Performance guarantee 10% of contract value",
        "Validity of offer 90 days",
        "Payment terms 30 days after receipt",
        "Delivery period 45 days from date of order",
        "Liquidated damages 0.5% per week",
        "Penalty for delay as per clause 12",
        "Warranty period 24 months",
        "The successful tenderer shall submit the agreement within 15 days",
        "Tenderers must quote for all items",
        "Rates quoted shall be inclusive of GST",
        "Taxes and duties extra as applicable",
        "Completion period 6 months",
        "Arbitration as per Indian Arbitration Act",
        "Jurisdiction Howrah courts only",
        "Force majeure clause applies",
        "All the bidders should ensure they are registered vendors",
        "Documents attached with tender to be signed",
        "Note:- rates quoted shall be inclusive of all taxes",
    ],
)
def test_a_numbered_clause_is_still_rejected(clause):
    """Each of these has a serial and a short description -- exactly the shape
    branch (g) accepts -- and none of them is a line item."""
    assert not _is_valid_schedule_item(mat(description=clause)), clause


# ── the prompt agrees ────────────────────────────────────────────────────────


def test_the_prompt_emits_rows_from_a_table_with_no_qty_column():
    from app.services.boq_parser_service import _BOQ_AI_SYSTEM_PROMPT as P

    assert "NO Qty column" in P and "quantity: null" in P and "sr_no" in P


# ── real material rows must not be caught by the tighter prose guard ─────────


@pytest.mark.parametrize(
    "description",
    [
        "Red oxide zinc chromate primer, IS 2074",
        "Synthetic enamel paint, signal red, IS 2932",
        "Thinner for synthetic enamel, RDSO spec",
        "Brake block, composite, K-type",
        "Air hose with coupling, 1 inch",
        "Distributor valve C3W with control reservoir",
        "MS angle 50x50x6 mm",
        "Rubber gasket for door, EPDM",
        "Warranty card holder bracket",          # contains "warranty" mid-cell
        "Penalty box marking stencil set",       # contains "penalty" mid-cell
    ],
)
def test_material_rows_survive_the_tighter_prose_guard(description):
    assert _is_valid_schedule_item(mat(description=description)), description
