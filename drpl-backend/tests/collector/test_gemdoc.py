"""
The GeM bid-document parser, pinned against real documents.

The three fixtures are the extracted text of PDFs pulled from the live portal
on 2026-09-12 -- not hand-written samples. That matters: almost every bug this
parser had came from a shape nobody would have thought to invent, like the
reverse-auction template printing its labels with no slash in front of them, or
GeM omitting the "EMD Required" line entirely on the bids that actually charge
an EMD. A fixture that was written rather than captured tests the parser
against our own assumptions.

Each test states what GeM printed and asserts we read it back. Where a document
is silent, the assertion is ``is None`` -- "not stated" is a result, and the
tests treat a fabricated zero as the failure it would be downstream.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from collector.gemdoc import GemBidDocument, parse_text
from collector.gemdoc.normalize import (
    NormalizedDocument,
    is_junk_line,
    normalize_lines,
)
from collector.gemdoc.parser import (
    as_datetime,
    csv_list,
    emails,
    integer,
    money_inr,
    percent,
    yes_no,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return io.open(FIXTURES / name, encoding="utf-8").read()


@pytest.fixture(scope="module")
def railway_bid() -> GemBidDocument:
    """GEM/2026/B/7977682 -- Eastern Railway, no EMD, no estimated value."""
    return parse_text(_fixture("gem_bid_document_railway.txt"))


@pytest.fixture(scope="module")
def emd_bid() -> GemBidDocument:
    """GEM/2026/B/8013435 -- North Central Railway, EMD and ePBG both real."""
    return parse_text(_fixture("gem_bid_document_with_emd.txt"))


@pytest.fixture(scope="module")
def ra_doc() -> GemBidDocument:
    """GEM/2026/R/733372 -- a reverse auction, the awkward template."""
    return parse_text(_fixture("gem_ra_document.txt"))


# -- value coercion ------------------------------------------------------


class TestCoercion:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("15094560", 15094560.0),
            ("6.5 Lakh (s)", 650000.0),
            ("2 Lakh(s)", 200000.0),
            ("1.5 Crore", 15000000.0),
            ("Rs. 2,55,840", 255840.0),
            ("Nil", 0.0),
            ("N/A", 0.0),
            (None, None),
            ("to be decided by the buyer", None),
        ],
    )
    def test_money(self, text, expected):
        assert money_inr(text) == expected

    def test_percent_rejects_impossible_values(self):
        # A number above 100 in a percentage field is a misread, not a term.
        assert percent("3") == 3.0
        assert percent("15094560") is None

    @pytest.mark.parametrize(
        "text,expected",
        [("Yes", True), ("Yes | Complete", True), ("No", False),
         ("Not Required", False), ("maybe", None), (None, None)],
    )
    def test_yes_no(self, text, expected):
        assert yes_no(text) is expected

    def test_dates_are_converted_from_ist(self):
        # GeM prints IST and states no offset. 13:00 IST is 07:30 UTC.
        assert as_datetime("22-09-2026 13:00:00") == "2026-09-22T07:30:00+00:00"

    def test_date_absent(self):
        assert as_datetime("as per schedule") is None

    def test_csv_list_stops_at_gems_footnote(self):
        text = ("Experience Criteria,Bidder Turnover,Certificate (Requested in ATC)"
                "*In case any bidder is seeking exemption from Experience / Turnover")
        assert csv_list(text) == [
            "Experience Criteria", "Bidder Turnover", "Certificate (Requested in ATC)"
        ]

    def test_emails_deduplicate_case_insensitively(self):
        assert emails("a@b.gov.in and A@B.GOV.IN and c@d.in") == ["a@b.gov.in", "c@d.in"]

    def test_integer(self):
        assert integer("90 (Days)") == 90
        assert integer("no number here") is None


# -- normalisation -------------------------------------------------------


class TestNormalize:
    def test_page_furniture_is_dropped(self):
        text = "\n".join([
            "Ministry/State Name",
            "Ministry Of Railways",
            ":/Bid Number: GEM/2026/B/7977682",
            "; /Dated: 12-09-2026",
            "3 / 8",
            "Department Name",
        ])
        lines = normalize_lines(text)
        assert lines == ["Ministry/State Name", "Ministry Of Railways", "Department Name"]

    @pytest.mark.parametrize("line", ["'% 2 % 9", "+/ 56", ". % 4", "A A", "5<", ""])
    def test_mojibake_is_junk(self, line):
        assert is_junk_line(line) is True

    @pytest.mark.parametrize(
        "line",
        ["No", "Yes", "Nil", "1", "30", "22-09-2026 13:00:00", "90 (Days)",
         "Ministry Of Railways", "Yes | Complete"],
    )
    def test_real_values_are_not_junk(self, line):
        # The regression this pins: an earlier rule required a three-letter
        # word, which threw away every "No" in the document and reported EMD
        # and MSE relaxation as unstated on bids that state them plainly.
        assert is_junk_line(line) is False

    def test_flat_and_lines_stay_consistent(self):
        doc = NormalizedDocument.from_text("Office Name\nEastern Railway Hq\n")
        assert doc.flat == " ".join(doc.lines)


# -- a real bid document -------------------------------------------------


class TestRailwayBid:
    def test_identity(self, railway_bid):
        assert railway_bid.bid_number == "GEM/2026/B/7977682"
        assert railway_bid.document_kind == "bid"
        assert railway_bid.needs_ai_fallback is False

    def test_buyer(self, railway_bid):
        assert railway_bid.ministry == "Ministry Of Railways"
        assert railway_bid.department == "Indian Railways"
        assert railway_bid.organisation == "Eastern Railway"
        assert railway_bid.office == "Eastern Railway Hq"

    def test_buyer_emails_are_both_captured(self, railway_bid):
        assert railway_bid.buyer_emails == ["alok.shakti@gov.in", "sanjeev.0586@gov.in"]

    def test_schedule(self, railway_bid):
        assert railway_bid.bid_end_date == "2026-09-22T07:30:00+00:00"
        assert railway_bid.bid_opening_date == "2026-09-23T07:30:00+00:00"
        assert railway_bid.bid_offer_validity_days == 90

    def test_subject(self, railway_bid):
        assert railway_bid.item_category == "Multimedia Projector (MMP) (Q2)"
        assert railway_bid.total_quantity == 1

    def test_no_emd_is_recorded_as_zero_not_unknown(self, railway_bid):
        # The document says "EMD Detail / Required: No". That is a fact about
        # the bid, and it must not read the same as never having looked.
        assert railway_bid.emd_required is False
        assert railway_bid.emd_amount == 0.0

    def test_estimated_value_absent_stays_none(self, railway_bid):
        # This document states no value. The boilerplate paragraph beginning
        # "Estimated Bid Value indicated above is being declared solely for
        # the purpose of guidance" is present and must NOT be mistaken for one.
        assert railway_bid.estimated_value is None

    def test_eligibility_fields(self, railway_bid):
        assert railway_bid.mse_relaxation == "No"
        assert railway_bid.startup_relaxation == "No"
        assert railway_bid.documents_required_from_seller == [
            "Certificate (Requested in ATC)", "OEM Authorization Certificate"
        ]

    def test_consignee_and_delivery(self, railway_bid):
        assert railway_bid.consignees, "the consignee table should parse"
        c = railway_bid.consignees[0]
        assert c.name == "Girish Kumar Verma"
        assert c.quantity == 1
        # 30 days is the stated delivery period. The PIN code 811214 sits on
        # the same row and an earlier version reported it as the period.
        assert c.delivery_days == 30
        assert railway_bid.delivery_timeline == "30 days"
        assert "Jamalpur" in railway_bid.delivery_location

    def test_process_terms(self, railway_bid):
        assert railway_bid.type_of_bid == "Single Packet Bid"
        assert railway_bid.bid_to_ra_enabled is False
        assert railway_bid.arbitration_clause is False
        assert railway_bid.inspection_required is True

    def test_eligibility_text_reports_only_stated_facts(self, railway_bid):
        text = railway_bid.eligibility_text()
        # The block is column-aligned, so match the pair rather than spacing.
        assert re.search(r"MSE relaxation \(experience & turnover\)\s+: No", text)
        assert "EMD required" in text
        # Nothing was stated about turnover, so no turnover line appears.
        assert "Minimum average annual turnover" not in text


# -- a bid that actually charges an EMD ----------------------------------


class TestBidWithEmd:
    def test_money_fields(self, emd_bid):
        assert emd_bid.estimated_value == 8533728.0
        assert emd_bid.emd_amount == 170680.0
        assert emd_bid.epbg_percentage == 5.0

    def test_required_flags_are_inferred_from_the_amounts(self, emd_bid):
        # GeM omits the "Required" line entirely on a bid that has an EMD --
        # it prints the advisory bank and the amount instead. Leaving the flag
        # unset would show a real EMD next to "EMD required: unknown".
        assert emd_bid.emd_required is True
        assert emd_bid.epbg_required is True
        assert emd_bid.emd_advisory_bank

    def test_turnover_and_experience(self, emd_bid):
        assert emd_bid.min_average_annual_turnover_text == "43 Lakh (s)"
        assert emd_bid.min_average_annual_turnover == 4300000.0
        assert emd_bid.years_of_past_experience == "3 Year (s)"

    def test_eligibility_text_includes_the_numbers(self, emd_bid):
        text = emd_bid.eligibility_text()
        assert "43 Lakh (s)" in text
        assert "3 Year (s)" in text
        assert "170680" in text


# -- the reverse-auction template ----------------------------------------


class TestReverseAuction:
    def test_identity_and_parent(self, ra_doc):
        assert ra_doc.document_kind == "ra"
        assert ra_doc.bid_number == "GEM/2026/R/733372"
        # An RA carries almost no terms of its own; the parent bid has them,
        # and the fetcher follows this to get the real detail.
        assert ra_doc.parent_bid_number == "GEM/2026/B/7948345"

    def test_labels_without_a_slash_still_parse(self, ra_doc):
        # The RA template's Hindi extracts to nothing at all, so its labels
        # arrive with no slash in front of them. Requiring one -- which the
        # first version did -- returned an empty document for every RA.
        assert ra_doc.ministry == "Ministry Of Railways"
        assert ra_doc.organisation == "Konkan Railway Corporation Limited"
        assert ra_doc.total_quantity == 5107

    def test_exemption_wording_is_recognised(self, ra_doc):
        # A bid says "Relaxation", an RA says "Exemption". Same field.
        assert ra_doc.mse_relaxation == "No"
        assert ra_doc.startup_relaxation == "No"

    def test_turnover_requirement(self, ra_doc):
        assert ra_doc.min_average_annual_turnover == 200000.0

    def test_value_capture_stops_at_the_boilerplate(self, ra_doc):
        # There is no label after the last field, so without an explicit stop
        # the value ran on into GTC clause 26 -- six hundred words of it.
        assert len(ra_doc.startup_relaxation) < 40


# -- failure modes -------------------------------------------------------


class TestDegradation:
    def test_empty_document_asks_for_help_rather_than_returning_blanks(self):
        doc = parse_text("")
        assert doc.needs_ai_fallback is True
        assert doc.parse_notes

    def test_unrecognisable_document_is_flagged(self):
        doc = parse_text("This page intentionally left blank.\nNothing to see.")
        assert doc.needs_ai_fallback is True

    def test_non_pdf_bytes_raise(self):
        from collector.gemdoc import parse_bid_document

        with pytest.raises(ValueError):
            parse_bid_document(b"<html>Service Unavailable</html>")

    def test_to_dict_is_json_shaped(self, railway_bid):
        import json

        d = railway_bid.to_dict()
        json.dumps(d)  # must not raise
        assert d["delivery_location"]
        assert d["bid_number"] == "GEM/2026/B/7977682"


# -- attachments ---------------------------------------------------------


class TestAttachments:
    """The buyer's own files, reachable only through the PDF's link annotations.

    GeM prints "Buyer uploaded ATC document Click here to view the file" and
    puts the address nowhere in the text. The scope of work, the BOQ and the
    price breakup are all behind those annotations, so a collector that reads
    only the text ships the cover sheet.
    """

    def test_classifies_the_paths_gem_actually_uses(self):
        from collector.gemdoc.parser import classify_links

        attachments, mail = classify_links([
            "https://bidplus.gem.gov.in/resources/upload_nas/SepQ326/bidding/excel/bid-1/a.xlsx",
            "https://bidplus.gem.gov.in/resources/upload_nas/SepQ326/bidding/biddoc/bid-1/b.pdf",
            "https://fulfilment.gem.gov.in/contract/slafds?fileDownloadPath=X",
            "https://admin.gem.gov.in/apis/v1/gtc/pdfByDate/?date=20260908",
            "https://bidplus.gem.gov.in/showbidDocument/999",
            "mailto:buyer@gov.in",
        ])
        assert [a.kind for a in attachments] == [
            "boq", "buyer_document", "sla", "gtc", "bid_document"
        ]
        assert mail == ["buyer@gov.in"]

    def test_drops_the_endpoint_that_is_on_every_document_and_always_404s(self):
        from collector.gemdoc.parser import classify_links

        attachments, _ = classify_links(
            ["https://bidplus.gem.gov.in/bidding/downloadOmppdfile/"]
        )
        # Shipping it would give every single tender one broken attachment.
        assert attachments == []

    def test_ignores_relative_and_javascript_hrefs(self):
        from collector.gemdoc.parser import classify_links

        attachments, _ = classify_links(["#top", "javascript:void(0)", "/relative"])
        assert attachments == []

    def test_filename_is_readable(self):
        from collector.gemdoc.parser import Attachment

        a = Attachment(url="https://x.gov.in/a/b/1788866298.xlsx?t=1", kind="boq")
        assert a.filename == "1788866298.xlsx"

    def test_mailto_addresses_join_the_buyer_contacts(self):
        doc = parse_text(
            "Ministry/State Name\nMinistry Of Railways\n",
            links=["mailto:rupam@bhel.in", "mailto:mm@bhel.in"],
        )
        assert doc.buyer_emails == ["rupam@bhel.in", "mm@bhel.in"]

    def test_a_document_with_no_links_is_ordinary(self):
        doc = parse_text("Ministry/State Name\nMinistry Of Railways\n")
        assert doc.attachments == []

    def test_extract_links_never_raises_on_rubbish(self):
        from collector.gemdoc.normalize import extract_links

        assert extract_links(b"not a pdf") == []
        assert extract_links(b"") == []


# -- text fidelity -------------------------------------------------------


class TestTextFidelity:
    def test_typographic_ligatures_survive(self):
        from collector.gemdoc.normalize import _strip_to_ascii

        # TCPDF sets GeM's English with real ligatures, so "Specification"
        # arrives as "Speci<U+FB01>cation". A bare ASCII filter turned it into
        # "Specication" in the technical specifications block of every
        # document -- the text an analyst actually reads.
        assert _strip_to_ascii("GeM Category Speciﬁcation") == (
            "GeM Category Specification"
        )
        assert _strip_to_ascii("Oﬀer and ﬂow") == "Offer and flow"

    def test_devanagari_is_dropped_not_mangled_into_a_value(self):
        from collector.gemdoc.normalize import _strip_to_ascii

        assert _strip_to_ascii("बड ववरण/Bid Details") == (
            "/Bid Details"
        )

    def test_a_heading_is_a_heading_only_where_it_is_one(self, railway_bid):
        from collector.gemdoc.parser import _is_heading

        # The live heading line, with the Hindi reduced to one stray letter.
        assert _is_heading("S /Technical Specifications", r"Technical\s+Specifications")
        # The same words inside the MSE relaxation clause, on most documents.
        prose = ("subject to their meeting of quality and technical specifications. "
                 "The bidder seeking Relaxation from Experience Criteria shall upload")
        assert not _is_heading(prose, r"Technical\s+Specifications")

    def test_technical_specifications_are_the_table_not_the_policy_text(self, emd_bid):
        specs = emd_bid.technical_specifications
        assert specs
        # The bug this pins: the block used to open with the MSE relaxation
        # paragraph because "technical specifications" appears inside it.
        assert "seeking Relaxation" not in specs
        assert "Vehicle Type" in specs


class TestContractPeriod:
    def test_a_service_bid_states_a_contract_period_instead_of_delivery_days(self):
        # Header of GEM/2026/B/7802351 (AMC of desktops, Ministry of Railways)
        # as it comes out of the PDF: no consignee delivery days anywhere, the
        # engagement stated once, in the header.
        text = "\n".join([
            "/Bid Offer Validity (From End Date)",
            "90 (Days)",
            "/Contract Period",
            "2 Year(s) 1 Day(s)",
            "%/Years of Past Experience Required for same/similar service",
            "1 Year (s)",
        ])
        doc = parse_text(text)
        assert doc.bid_offer_validity_days == 90
        assert doc.contract_period == "2 Year(s) 1 Day(s)"
        assert doc.delivery_timeline == "contract period 2 Year(s) 1 Day(s)"

    def test_delivery_days_still_win_when_a_bid_states_both(self, railway_bid):
        # A goods bid with a consignee delivery period keeps reporting it.
        assert railway_bid.delivery_timeline == "30 days"

    def test_a_goods_bid_has_no_contract_period(self, railway_bid):
        assert railway_bid.contract_period is None
