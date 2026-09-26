"""
DRPL Collector - document enrichment.

One stage, run on NEW in-scope tenders only, before they are shipped:

    bid PDF  ->  Claude Haiku 4.5  ->  value, EMD, eligibility, scope, contact

Three properties it holds to, and each is a decision rather than an accident:

**Only new tenders.** The cost of a sweep scales with what it *found*, not with
how many pages it walked. A re-run that finds nothing new spends nothing.

**Before the first POST.** `tender_service._update_existing_tender` never
assigns `estimated_value` or `emd_amount` -- only the insert path does. A value
discovered after the row exists would be silently dropped, so it has to be
there when the row is created.

**Never fatal.** No document, no API key, a refusal, a timeout -- the tender
still ships with its listing data. Enrichment can only ever add.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from collector.config import get_settings
from collector.enrich import ai as ai_mod
from collector.enrich.documents import fetch_document

logger = logging.getLogger(__name__)

__all__ = ["EnrichmentResult", "enrich_tenders", "is_enabled"]


@dataclass
class EnrichmentResult:
    attempted: int = 0
    enriched: int = 0
    no_document: int = 0
    no_extraction: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    fields_filled: dict[str, int] = field(default_factory=dict)

    def merge(self, other: "EnrichmentResult") -> None:
        self.attempted += other.attempted
        self.enriched += other.enriched
        self.no_document += other.no_document
        self.no_extraction += other.no_extraction
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        for k, v in other.fields_filled.items():
            self.fields_filled[k] = self.fields_filled.get(k, 0) + v

    def summary(self) -> dict:
        return {
            "attempted": self.attempted,
            "enriched": self.enriched,
            "no_document": self.no_document,
            "no_extraction": self.no_extraction,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 4),
            "fields_filled": self.fields_filled,
        }


def is_enabled() -> bool:
    """Enrichment runs only when it is switched on AND there is a key."""
    return bool(get_settings().enrich_documents_enabled) and ai_mod.is_available()


async def enrich_tenders(
    client: httpx.AsyncClient,
    tenders: list[dict],
    known: Optional[set[str]] = None,
) -> EnrichmentResult:
    """Fill in what the listing could not say, in place.

    ``tenders`` are TenderInput dicts. ``known`` is the ledger's set for this
    portal; anything in it is skipped, because it has been enriched already or
    is about to be reported as a duplicate.
    """
    result = EnrichmentResult()
    if not tenders or not is_enabled():
        return result

    s = get_settings()
    known = known or set()
    candidates = [
        t for t in tenders
        if t.get("tenderId") not in known and (t.get("nitDocumentLinks") or t.get("documentLinks"))
    ][: s.enrich_max_per_page]

    if not candidates:
        return result

    semaphore = asyncio.Semaphore(max(1, s.enrich_concurrency))

    async def one(tender: dict) -> None:
        async with semaphore:
            await _enrich_one(client, tender, result)

    # gather with return_exceptions: one tender's failure must not cancel the
    # siblings it is running alongside.
    outcomes = await asyncio.gather(*(one(t) for t in candidates), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            logger.debug("enrich: a tender failed in gather: %s", outcome)

    if result.attempted:
        logger.info(
            "enrich: %s/%s tenders enriched, %s in / %s out tokens, $%.4f",
            result.enriched, result.attempted,
            result.input_tokens, result.output_tokens, result.cost_usd,
        )
    return result


async def _enrich_one(client: httpx.AsyncClient, tender: dict, result: EnrichmentResult) -> None:
    links = tender.get("nitDocumentLinks") or tender.get("documentLinks") or []
    if not links:
        return
    result.attempted += 1

    doc = await fetch_document(client, links[0])
    if doc is None:
        result.no_document += 1
        return

    extraction = await ai_mod.extract_from_pdf(
        doc.content,
        tender_id=str(tender.get("tenderId") or ""),
        title=str(tender.get("title") or ""),
    )
    if extraction is None:
        result.no_extraction += 1
        return

    result.input_tokens += extraction.input_tokens
    result.output_tokens += extraction.output_tokens
    result.cost_usd += extraction.cost_usd

    fields = extraction.to_tender_fields()
    if not fields:
        result.no_extraction += 1
        return

    for key, value in fields.items():
        # Never overwrite something the listing already stated -- the portal's
        # own structured field beats a reading of its prose.
        if tender.get(key) in (None, "", [], 0) or key not in tender:
            tender[key] = value
            result.fields_filled[key] = result.fields_filled.get(key, 0) + 1

    # The document was read, so the tender now carries detail-page-grade data.
    # This is what lets drpl-backend's _update_existing_tender apply the rest
    # of the enrichment block on a later pass.
    tender["isDetailExtracted"] = True
    result.enriched += 1
