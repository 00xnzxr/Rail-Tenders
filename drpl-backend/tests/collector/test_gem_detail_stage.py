"""
The detail stage: bid document -> the fields DRPL's tender card shows as dashes.

The interesting behaviour is not the happy path. It is what happens when the
document is missing, is not a PDF, or is a reverse auction that states nothing
-- because each of those has a wrong answer that looks like success.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from collector.gemdoc import parse_text
from collector.portals import gem_detail
from collector.portals.gem_detail import DetailResult, apply_detail

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return io.open(FIXTURES / name, encoding="utf-8").read()


@pytest.fixture(scope="module")
def emd_detail():
    return parse_text(_fixture("gem_bid_document_with_emd.txt"))


@pytest.fixture(scope="module")
def railway_detail():
    return parse_text(_fixture("gem_bid_document_railway.txt"))


def _tender(**over) -> dict:
    base = {
        "portal": "gem",
        "tenderId": "GEM/2026/B/8013435",
        "title": "Cab hiring",
        "organisation": "North Central Railway",
        "isDetailExtracted": False,
    }
    base.update(over)
    return base


class TestApplyDetail:
    def test_fills_the_numbers_the_listing_never_carries(self, emd_detail):
        t = _tender()
        apply_detail(t, emd_detail)
        assert t["estimatedValue"] == 8533728.0
        assert t["emdAmount"] == 170680.0
        assert t["performanceGuaranteePercent"] == 5.0

    def test_fills_the_eligibility_block(self, emd_detail):
        t = _tender()
        apply_detail(t, emd_detail)
        assert "43 Lakh (s)" in t["eligibilityCriteria"]
        assert "EMD amount" in t["eligibilityCriteria"]

    def test_marks_the_row_as_detail_extracted(self, emd_detail):
        t = _tender()
        apply_detail(t, emd_detail)
        assert t["isDetailExtracted"] is True

    def test_never_overwrites_a_value_the_listing_already_set(self, emd_detail):
        # Fill-if-empty. The listing is authoritative for what it carries, and
        # a document that words a field differently must not silently replace
        # it -- nor may a re-run flip a value an analyst corrected.
        t = _tender(deliveryLocation="Set by the listing")
        apply_detail(t, emd_detail)
        assert t["deliveryLocation"] == "Set by the listing"

    def test_writes_only_real_tenderinput_keys(self, emd_detail):
        # A key that is not on TenderInput is dropped silently by pydantic,
        # which is the worst possible way to discover a typo.
        allowed = {
            "portal", "tenderId", "title", "department", "organisation",
            "description", "estimatedValue", "currency", "openingDate",
            "closingDate", "emdAmount", "preBidDate", "status",
            "documentLinks", "sourceUrl", "extractedAt", "detailUrl",
            "fullDescription", "eligibilityCriteria", "technicalSpecifications",
            "evaluationCriteria", "performanceGuarantee",
            "performanceGuaranteePercent", "preBidMeetingLocation",
            "deliveryLocation", "deliveryTimeline", "buyerContactName",
            "buyerContactEmail", "buyerContactPhone", "numberOfAmendments",
            "corrigendaLinks", "nitDocumentLinks", "amendmentLinks",
            "isDetailExtracted", "searchMatchKeyword", "isEligibleIndicator",
            "location", "bidType", "sourcePortal", "category",
        }
        t = _tender()
        apply_detail(t, emd_detail)
        assert set(t) <= allowed, f"not on TenderInput: {set(t) - allowed}"

    def test_a_thin_parse_does_not_claim_detail_extracted(self):
        # DRPL gates its own enrichment on this flag, so claiming it early
        # stops the row ever being filled in properly.
        thin = parse_text("Nothing useful here at all.")
        t = _tender()
        apply_detail(t, thin)
        assert t["isDetailExtracted"] is False

    def test_records_which_fields_it_filled(self, railway_detail):
        result = DetailResult()
        apply_detail(_tender(), railway_detail, result)
        assert result.fields_filled["eligibilityCriteria"] == 1
        assert "deliveryLocation" in result.fields_filled


class TestFetchDetail:
    @pytest.mark.asyncio
    async def test_missing_document_yields_nothing_rather_than_a_blank(self, monkeypatch):
        async def no_pdf(session, url):
            return None

        monkeypatch.setattr(gem_detail, "_get_pdf", no_pdf)
        doc = {"b_bid_number": ["GEM/2026/B/1"], "b_id": [1], "b_bid_type": [1]}
        detail, followed = await gem_detail.fetch_detail(object(), doc)
        assert detail is None and followed is False

    @pytest.mark.asyncio
    async def test_a_reverse_auction_is_followed_to_its_parent_bid(self, monkeypatch):
        # An RA document is one page and states almost nothing. Reading it and
        # calling the tender enriched produces a row that looks fetched and is
        # empty; the parent bid is where the terms actually are.
        ra_text = _fixture("gem_ra_document.txt")
        parent_text = _fixture("gem_bid_document_with_emd.txt")
        calls = []

        async def fake_pdf(session, url):
            calls.append(url)
            return b"%PDF-ra" if "showradocumentPdf" in url else b"%PDF-parent"

        def fake_parse(data, bid_number=None):
            text = ra_text if data == b"%PDF-ra" else parent_text
            return parse_text(text, bid_number=bid_number)

        monkeypatch.setattr(gem_detail, "_get_pdf", fake_pdf)
        monkeypatch.setattr(gem_detail, "parse_bid_document", fake_parse)

        doc = {
            "b_bid_number": ["GEM/2026/R/733372"],
            "b_id": [9884087], "b_bid_type": [2], "b_eval_type": [0],
            "b_id_parent": [9791086],
        }
        detail, followed = await gem_detail.fetch_detail(object(), doc)
        assert followed is True
        assert len(calls) == 2, "both the RA and its parent should be fetched"
        # The parent's terms, but the RA's own live schedule.
        assert detail.emd_amount == 170680.0

    @pytest.mark.asyncio
    async def test_detail_failure_never_breaks_the_batch(self, monkeypatch):
        async def explode(session, url):
            raise RuntimeError("network gone")

        monkeypatch.setattr(gem_detail, "_get_pdf", explode)
        docs = [{"b_bid_number": ["GEM/2026/B/1"], "b_id": [1], "b_bid_type": [1]}]
        tenders = [_tender(tenderId="GEM/2026/B/1")]
        result = await gem_detail.enrich_with_details(object(), docs, tenders)
        assert result.no_document == 1
        assert tenders[0]["isDetailExtracted"] is False


class TestAttachmentsReachTheTender:
    def test_buyer_documents_are_appended_not_substituted(self, emd_detail):
        from collector.gemdoc.parser import Attachment

        emd_detail.attachments = [
            Attachment("https://bidplus.gem.gov.in/resources/upload_nas/x/biddoc/bid-1/scope.pdf",
                       "buyer_document"),
            Attachment("https://bidplus.gem.gov.in/resources/upload_nas/x/excel/bid-1/boq.xlsx",
                       "boq"),
            Attachment("https://admin.gem.gov.in/apis/v1/gtc/pdfByDate/?date=1", "gtc"),
        ]
        t = _tender(documentLinks=["https://bidplus.gem.gov.in/showbidDocument/1"],
                    nitDocumentLinks=["https://bidplus.gem.gov.in/showbidDocument/1"])
        apply_detail(t, emd_detail)

        # The bid document itself is still first.
        assert t["documentLinks"][0].endswith("/showbidDocument/1")
        assert len(t["documentLinks"]) == 4

        # Only the buyer's own files join nitDocumentLinks -- that is the list
        # DRPL downloads and the analyzer reads, and the GeM general terms are
        # the same boilerplate on every tender in the country.
        assert t["nitDocumentLinks"] == [
            "https://bidplus.gem.gov.in/showbidDocument/1",
            "https://bidplus.gem.gov.in/resources/upload_nas/x/biddoc/bid-1/scope.pdf",
            "https://bidplus.gem.gov.in/resources/upload_nas/x/excel/bid-1/boq.xlsx",
        ]


class TestSharedSemaphore:
    @pytest.mark.asyncio
    async def test_a_shared_semaphore_bounds_reads_across_batches(self, monkeypatch):
        # The full sweep reads documents for several batches at once. If each
        # call made its own semaphore, the configured bound would be per batch
        # and the portal would see it multiplied.
        import asyncio

        import collector.config as cfg

        monkeypatch.setenv("GEM_DETAIL_DELAY_SECONDS", "0")
        cfg.get_settings.cache_clear()

        live = peak = 0

        async def slow(session, doc):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.005)
            live -= 1
            return None, False

        monkeypatch.setattr(gem_detail, "fetch_detail", slow)

        def rows(lo: int, hi: int):
            docs = [{"b_bid_number": [f"GEM/2026/B/{i}"]} for i in range(lo, hi)]
            tenders = [{"tenderId": f"GEM/2026/B/{i}"} for i in range(lo, hi)]
            return docs, tenders

        shared = asyncio.Semaphore(2)
        first, second = rows(0, 4), rows(4, 8)
        await asyncio.gather(
            gem_detail.enrich_with_details(object(), *first, semaphore=shared),
            gem_detail.enrich_with_details(object(), *second, semaphore=shared),
        )
        assert peak == 2, f"{peak} documents in flight against a shared bound of 2"
