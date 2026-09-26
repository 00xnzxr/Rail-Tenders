"""Regression tests for word-style NIT item codes in the BOQ parser.

Background: some IREPS NITs (e.g. ER-Liluah RA-Coach) print a WORD in the
"Item Code" column ("Mechanical", "ELEC FUR") instead of an alphanumeric code
(A1, B7). The old row-validity gate only accepted ``^[A-Za-z]{1,2}\\d+`` codes,
so these rows survived only when BOTH qty AND rate parsed — a single mis-aligned
column silently dropped them, collapsing the costing into freeform mode (empty
Item Code column + hallucinated rows). These tests lock in the fix.
"""

from app.services.boq_parser_service import (
    _is_valid_schedule_item,
    _boq_row_key,
    _is_instruction_prose,
)


class TestWordStyleItemCodes:
    def test_word_code_with_qty_and_rate_accepted(self):
        item = dict(item_code="Mechanical", quantity=40800, unit_rate=939.75,
                    schedule_name="A", description="Wall panel")
        assert _is_valid_schedule_item(item) is True

    def test_word_code_with_only_basic_value_accepted(self):
        item = dict(item_code="ELEC FUR", quantity=20400, basic_value=669120,
                    schedule_name="B", description="Cable 1.5 sq.mm")
        assert _is_valid_schedule_item(item) is True

    def test_alphanumeric_code_still_accepted(self):
        assert _is_valid_schedule_item(
            dict(item_code="A1", description="Part", schedule_name="A")
        ) is True

    def test_bare_word_code_without_pricing_rejected(self):
        # A word code alone (no qty/rate/value) is a header-ish artefact, not a row.
        assert _is_valid_schedule_item(
            dict(item_code="Mechanical", description="header", schedule_name="A")
        ) is False

    def test_priced_row_without_code_accepted(self):
        assert _is_valid_schedule_item(
            dict(item_code="", quantity=17, unit_rate=19426.35, description="Stripping")
        ) is True


class TestInstructionProseRejected:
    def test_all_the_bidders_prose_rejected_even_with_stray_numbers(self):
        # The exact junk row that leaked into Schedule C costing.
        item = dict(item_code="", quantity=1, unit_rate=1, schedule_name="C",
                    description="All the bidders/tenders should ensure that all documents")
        assert _is_instruction_prose(item["description"]) is True
        assert _is_valid_schedule_item(item) is False

    def test_i_we_the_tenderer_prose_rejected(self):
        assert _is_valid_schedule_item(
            dict(item_code="", description="I/we the tenderer hereby declare", quantity=0)
        ) is False

    def test_pure_prose_no_price_rejected(self):
        assert _is_valid_schedule_item(
            dict(item_code="", description="General instructions for bidders")
        ) is False

    def test_gst_compliance_prose_rejected_despite_gst_keyword(self):
        # Eligibility prose mentioning GST must NOT be accepted as a tax line.
        # (The exact junk row that leaked into Schedule C of tender 1197.)
        item = dict(
            item_code=None, quantity=None, unit_rate=None, schedule_name="C",
            description=("All the bidders/tenders should ensure that they are GST "
                         "complaint and their quoted tax structure/rates are as per "
                         "GST Law.(Please upload certificate of GSTIN registration)."),
        )
        assert _is_valid_schedule_item(item) is False

    def test_real_gst_provision_tax_line_still_accepted(self):
        # A genuine GST provision row is still kept (not instruction prose).
        assert _is_valid_schedule_item(
            dict(item_code="", description="Provision of GST @ 18% on SCHEDULE-A",
                 is_tax_line=True)
        ) is True


class TestTaxLines:
    def test_gst_tax_line_accepted(self):
        assert _is_valid_schedule_item(
            dict(item_code="", description="Provision of GST @ 18% on SCHEDULE-A",
                 is_tax_line=True)
        ) is True


class TestDedupKey:
    def test_alphanumeric_code_keys_on_code(self):
        # True per-row codes dedup by (schedule, code), case/space-insensitive.
        assert _boq_row_key(dict(schedule_name="a", item_code="A1")) == \
               _boq_row_key(dict(schedule_name="A", item_code=" a1 "))

    def test_word_code_does_NOT_collapse_distinct_rows(self):
        # The critical regression guard: a repeated category label ("Mechanical")
        # printed on every Schedule-A row must NOT make all those rows share a key
        # (that collapse destroyed 145 rows -> 9). Distinct sr_no/description must
        # yield distinct keys even with identical word codes.
        r1 = _boq_row_key(dict(schedule_name="A", item_code="Mechanical",
                               sr_no=1, description="Wall panel"))
        r2 = _boq_row_key(dict(schedule_name="A", item_code="Mechanical",
                               sr_no=2, description="Wooden flooring"))
        assert r1 != r2

    def test_word_code_same_row_still_dedups(self):
        # Two extraction passes returning the SAME word-code row (same sr_no +
        # description) should still collapse to one key.
        assert _boq_row_key(dict(schedule_name="B", item_code="ELEC FUR",
                                 sr_no=1, description="Cable 1.5")) == \
               _boq_row_key(dict(schedule_name="B", item_code="ELEC FUR",
                                 sr_no=1, description="Cable 1.5"))
