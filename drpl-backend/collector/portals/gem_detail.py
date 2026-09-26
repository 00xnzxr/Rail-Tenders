"""
DRPL Collector - the GeM detail stage.

Takes a tender that the listing produced and fills in everything the listing
could not, by fetching that bid's own document and reading it with
``collector.gemdoc``. This is where ``WORTH --`` and ``EMD --`` stop being dashes
on the DRPL tender card, and where the eligibility block comes from.

WHY THE DETAIL STAGE IS NOT OPTIONAL
------------------------------------
GeM's listing endpoint returns 34 fields and not one of them is a price, an EMD,
a qualifying condition or a delivery term. Everything an analyst needs in order
to decide whether to bid is inside the PDF. A collector that ships listing rows
is shipping a list of things to go and read by hand.

TWO THINGS IT GETS RIGHT THAT ARE EASY TO GET WRONG
---------------------------------------------------
**A reverse auction is followed to its parent bid.** An RA document is one page
and states almost nothing -- no EMD, no ePBG, no consignee, no specifications.
It names the bid it came from, and *that* document has the terms. Reading the RA
alone and calling it enriched produces a tender that looks fetched and is empty.

**A failure enriches nothing rather than half of something.** If the document
404s, is not a PDF, or parses to noise, the tender ships exactly as the listing
described it, with ``isDetailExtracted`` still false -- so DRPL's own detail
pipeline knows there is still work to do, and the next sweep tries again. The
one thing never done is writing a partial parse over a good listing value.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from collector.config import get_settings
from collector.gemdoc import GemBidDocument, parse_bid_document

logger = logging.getLogger(__name__)

__all__ = ["DetailResult", "apply_detail", "fetch_detail", "enrich_with_details"]


@dataclass
class DetailResult:
    """What one detail pass did, for the run event and the ledger."""

    attempted: int = 0
    parsed: int = 0
    no_document: int = 0
    unparseable: int = 0
    followed_to_parent: int = 0
    needs_ai: int = 0
    ai_enriched: int = 0
    fields_filled: dict[str, int] = field(default_factory=dict)

    def note(self, key: str) -> None:
        self.fields_filled[key] = self.fields_filled.get(key, 0) + 1

    def merge(self, other: "DetailResult") -> None:
        self.attempted += other.attempted
        self.parsed += other.parsed
        self.no_document += other.no_document
        self.unparseable += other.unparseable
        self.followed_to_parent += other.followed_to_parent
        self.needs_ai += other.needs_ai
        self.ai_enriched += other.ai_enriched
        for k, v in other.fields_filled.items():
            self.fields_filled[k] = self.fields_filled.get(k, 0) + v

    def summary(self) -> dict:
        return {
            "attempted": self.attempted,
            "parsed": self.parsed,
            "no_document": self.no_document,
            "unparseable": self.unparseable,
            "followed_to_parent": self.followed_to_parent,
            "needs_ai": self.needs_ai,
            "ai_enriched": self.ai_enriched,
            "fields_filled": dict(sorted(self.fields_filled.items())),
        }


async def _get_pdf(session, url: str) -> Optional[bytes]:
    """Fetch one document. Returns None for anything that is not a PDF.

    GeM answers a document URL with a 200 and an HTML error page often enough
    that the status code alone is not evidence of success -- the magic bytes
    are. Uses the sweep's own warm session because the document endpoints sit
    behind the same WAF cookies as the listing.
    """
    from collector.enrich.documents import fetch_document

    doc = await fetch_document(session.client, url)
    return doc.content if doc else None


async def fetch_detail(session, doc: dict) -> tuple[Optional[GemBidDocument], bool]:
    """Fetch and parse the bid document for one listing row.

    Returns ``(detail, followed_parent)``. ``detail`` is None when there was no
    readable document -- never a half-filled object.
    """
    from collector.portals.gem import _first, document_url, parent_bid_url

    bid_no = _first(doc, "b_bid_number")
    bid_no = str(bid_no).strip() if bid_no else None

    data = await _get_pdf(session, document_url(doc))
    detail = None
    if data:
        try:
            detail = parse_bid_document(data, bid_number=bid_no)
        except ValueError as e:
            logger.debug("gem-detail: %s did not parse: %s", bid_no, e)

    # A reverse auction states almost nothing. Its parent bid is the document
    # that carries the terms, so follow it and keep the richer of the two.
    followed = False
    parent_url = parent_bid_url(doc)
    wants_parent = detail is None or (
        detail.document_kind == "ra" or len(detail.fields_found) < 15
    )
    if parent_url and wants_parent:
        parent_data = await _get_pdf(session, parent_url)
        if parent_data:
            try:
                parent_detail = parse_bid_document(parent_data, bid_number=bid_no)
            except ValueError:
                parent_detail = None
            if parent_detail is not None and (
                detail is None
                or len(parent_detail.fields_found) > len(detail.fields_found)
            ):
                # Keep the RA's own schedule -- it is the live one, and the
                # parent bid's dates have already passed.
                if detail is not None:
                    parent_detail.bid_end_date = detail.bid_end_date or parent_detail.bid_end_date
                    parent_detail.bid_opening_date = (
                        detail.bid_opening_date or parent_detail.bid_opening_date
                    )
                    parent_detail.parent_bid_number = detail.parent_bid_number
                detail = parent_detail
                followed = True

    return detail, followed


def _set(tender: dict, key: str, value, result: Optional[DetailResult] = None) -> bool:
    """Fill a tender field, never overwriting something already there.

    Fill-if-empty rather than assign: the listing is authoritative for the
    handful of fields it does carry (the title, the ministry), and a document
    that words them differently should not quietly replace them.
    """
    if value is None or value == "" or value == []:
        return False
    if tender.get(key) not in (None, "", [], 0):
        return False
    tender[key] = value
    if result is not None:
        result.note(key)
    return True


def apply_detail(tender: dict, detail: GemBidDocument,
                 result: Optional[DetailResult] = None) -> None:
    """Map a parsed bid document onto drpl-backend's ``TenderInput`` shape.

    Only keys that are real fields on that model are written -- an extra key
    would be dropped silently by pydantic, which is the worst way to discover a
    typo.
    """
    _set(tender, "estimatedValue", detail.estimated_value, result)
    _set(tender, "emdAmount", detail.emd_amount, result)
    _set(tender, "performanceGuaranteePercent", detail.epbg_percentage, result)
    _set(tender, "eligibilityCriteria", detail.eligibility_text(), result)
    _set(tender, "technicalSpecifications", detail.technical_specifications, result)
    _set(tender, "deliveryLocation", detail.delivery_location, result)
    _set(tender, "deliveryTimeline", detail.delivery_timeline, result)
    _set(tender, "preBidDate", detail.pre_bid_date, result)
    _set(tender, "preBidMeetingLocation", detail.pre_bid_venue, result)

    if detail.buyer_emails:
        _set(tender, "buyerContactEmail", detail.buyer_emails[0], result)

    # Both sides are true UTC instants: gemdoc converts the document's IST
    # wall-clock, and gem.ist_instant converts the listing's. They are NOT
    # interchangeable, though, and the listing wins on purpose -- a corrigendum
    # that moves a deadline updates the listing while the PDF keeps the
    # original date. Sampling 40 live bids, 5 had a document already stale by
    # whole days for exactly that reason. So these fill a gap and must never
    # overwrite; _set is fill-if-empty, which is what makes that safe.
    _set(tender, "closingDate", detail.bid_end_date, result)
    _set(tender, "openingDate", detail.bid_opening_date, result)

    evaluation = " | ".join(
        p for p in (
            detail.evaluation_method,
            f"Bid type: {detail.type_of_bid}" if detail.type_of_bid else None,
            f"Technical clarification: {detail.technical_clarification_time}"
            if detail.technical_clarification_time else None,
        ) if p
    )
    _set(tender, "evaluationCriteria", evaluation or None, result)

    full = "\n\n".join(
        p for p in (
            f"Scope of supply: {detail.scope_of_supply}" if detail.scope_of_supply else None,
            f"Buyer added terms and conditions:\n{detail.buyer_added_terms}"
            if detail.buyer_added_terms else None,
            f"Buyer added ATC:\n{detail.buyer_added_atc}" if detail.buyer_added_atc else None,
            f"Payment timelines: {detail.payment_timelines}" if detail.payment_timelines else None,
        ) if p
    )
    _set(tender, "fullDescription", full[:20000] or None, result)

    if detail.consignees:
        location = detail.consignees[0].address or detail.consignees[0].name
        _set(tender, "location", (location or "")[:255] or None, result)

    # The buyer's own attachments -- scope of work, BOQ, price breakup, SLA.
    # GeM prints "Click here to view the file" and puts the address only in the
    # PDF's link annotation, so these reach DRPL nowhere else. Appended rather
    # than assigned: documentLinks already holds the bid document itself.
    if detail.attachments:
        existing = list(tender.get("documentLinks") or [])
        added = 0
        for a in detail.attachments:
            if a.url not in existing:
                existing.append(a.url)
                added += 1
        if added:
            tender["documentLinks"] = existing
            if result is not None:
                for _ in range(added):
                    result.note("documentLinks")

        # The buyer-uploaded documents are the ones carrying scope and ATC, so
        # they belong on nitDocumentLinks too -- that is the list DRPL's
        # nit_link_fetch_service downloads and the analyzer reads.
        nit = list(tender.get("nitDocumentLinks") or [])
        for a in detail.attachments:
            if a.kind in ("buyer_document", "boq") and a.url not in nit:
                nit.append(a.url)
        if nit != (tender.get("nitDocumentLinks") or []):
            tender["nitDocumentLinks"] = nit

    # Claimed only when the parse actually produced the detail fields. DRPL's
    # `_update_existing_tender` gates its own enrichment on this flag, so a
    # premature True stops the row ever being filled in properly.
    if not detail.needs_ai_fallback and len(detail.fields_found) >= 12:
        tender["isDetailExtracted"] = True


async def enrich_with_details(
    session,
    docs: list[dict],
    tenders: list[dict],
    *,
    concurrency: Optional[int] = None,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> DetailResult:
    """Fetch and apply the bid document for every tender in a batch.

    Concurrency is bounded and deliberately modest. These are 100-200 KB PDFs
    from a portal that is being asked for nothing else at the time, and the
    politeness budget is shared with the listing walk -- a detail stage that
    saturates the connection is how a collector earns a 429 for the sweep that
    follows it.

    Pass ``semaphore`` to share that bound across batches. The full sweep
    reads documents for several pages at once, and a semaphore created per
    call would multiply the limit by however many batches are in flight.
    """
    s = get_settings()
    limit = concurrency or s.gem_detail_concurrency
    if not tenders or not s.gem_fetch_details:
        return DetailResult()

    by_id: dict[str, dict] = {}
    for d in docs:
        no = d.get("b_bid_number")
        no = no[0] if isinstance(no, list) and no else no
        if no:
            by_id[str(no).strip()] = d

    result = DetailResult()
    sem = semaphore if semaphore is not None else asyncio.Semaphore(max(1, limit))

    async def one(tender: dict) -> None:
        doc = by_id.get(tender.get("tenderId"))
        if doc is None:
            return
        async with sem:
            result.attempted += 1
            try:
                detail, followed = await fetch_detail(session, doc)
            except Exception as e:  # noqa: BLE001 - a detail is never worth a sweep
                logger.debug("gem-detail: %s failed: %s", tender.get("tenderId"), e)
                result.no_document += 1
                return
            if detail is None:
                result.no_document += 1
                return
            if followed:
                result.followed_to_parent += 1
            if detail.needs_ai_fallback:
                result.needs_ai += 1
            if not detail.fields_found:
                result.unparseable += 1
            else:
                result.parsed += 1
                apply_detail(tender, detail, result)
            if detail.needs_ai_fallback:
                # The parser said so itself rather than returning a thin result
                # quietly. Reading it with Claude costs about $0.023, so this
                # is opt-in and only ever runs on the documents the template
                # could not account for -- a scan, or the day GeM changes it.
                await _ai_fallback(session, tender, result)
            await asyncio.sleep(s.gem_detail_delay_seconds)

    await asyncio.gather(*(one(t) for t in tenders))
    return result


async def _ai_fallback(session, tender: dict, result: DetailResult) -> None:
    """Hand one document to the model when the parser could not read it."""
    s = get_settings()
    if not s.gem_detail_ai_fallback:
        return
    try:
        from collector.enrich import enrich_tenders, is_enabled

        if not is_enabled():
            return
        before = set(k for k, v in tender.items() if v not in (None, "", [], 0))
        outcome = await enrich_tenders(session.client, [tender])
        gained = {k for k, v in tender.items()
                  if v not in (None, "", [], 0)} - before
        for key in gained:
            result.note(f"ai:{key}")
        if outcome.enriched:
            result.ai_enriched += 1
    except Exception as e:  # noqa: BLE001 - a fallback is never worth a sweep
        logger.debug("gem-detail: AI fallback failed for %s: %s",
                     tender.get("tenderId"), e)
