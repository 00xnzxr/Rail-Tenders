"""Market-price research: one focused web search per schedule row.

The costing agent prices sixty rows per call and decides for itself when to
search. On the Mid-Life Rehabilitation NIT it searched eleven times for 286
rows and guessed the rest -- cables, light fittings, heaters and valves
included, which are sold openly with prices on the page. Leaving research
to the agent's discretion cannot give an accurate sheet, so the platform
does it: every row that can have a market price gets its own Claude call
with the server-side web search tool, asked for the price of one unit of
that item, the page it came from, and how closely the product found
matches the specification.

A price is used only when
  * the model says the product matches the item exactly or closely -- a
    generic LED lamp is not an RDSO-specification coach fitting;
  * its URL is one the search actually returned in that same call, so a
    cited page cannot be invented; and
  * the figure is positive.
`settle_rates_on_evidence` then holds it to the plausibility band against
the railway's estimate like any other figure.

Rows that cannot have a market price -- drawing-specific parts, labour and
fitting lots, per-coach charges -- are not searched (`not_a_market_item`);
the model can also answer "no market price" for any row.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

#: Descriptions that name a job or a drawing, not something a shop sells.
_NOT_A_MARKET_ITEM_RE = re.compile(
    r"\blabou?r\b|\bcharges?\b|\bdrg\b\.?|\bdrawing\b|\bstripping\b|\bdismantl"
    r"|\bmodification\b|\bsegregation\b|\bfitment\b|\bcommissioning\b",
    re.IGNORECASE,
)
_GOOD_MATCH = {"exact", "close"}
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM = (
    "You research current Indian market prices for items in a railway tender. "
    "Search the web, then answer ONLY with one JSON object, no prose:\n"
    '{"found": true|false, "price_inr": number|null, "per_unit": "<unit the price is for>", '
    '"price_per_tender_unit": number|null, "url": "<the page the price is on>", '
    '"product": "<product and seller as listed>", "price_date": "<date shown, or empty>", '
    '"match": "exact"|"close"|"generic"|"none", "note": "<one short sentence>"}\n'
    "Rules: price_per_tender_unit is the price of ONE unit of the tender item in the "
    "tender's unit -- convert from the listing's unit (per 100 m coil to per metre, "
    "per tonne to per kg) and say how in note. match is exact when the listing is the "
    "same item and specification, close when it is the same item with a minor "
    "difference (make, rating within the same class), generic when it is only the same "
    "kind of product, none when nothing relevant was found. Railway items made to an "
    "RDSO / RCF / ICF specification or drawing are only exact or close when the listing "
    "names that specification or a railway-approved equivalent. Never estimate a price "
    "yourself: when no page gives one, answer found=false."
)


def not_a_market_item(row: dict) -> bool:
    """True for rows no shop sells: drawing-specific parts, labour and
    fitting lots, per-coach charges, and annexure components (their printed
    material-list rate is their basis)."""
    if row.get("is_tax_line") or row.get("component_of"):
        return True
    return bool(_NOT_A_MARKET_ITEM_RE.search(row.get("description") or ""))


def anonymize_description(text: str, anonymization_map: Optional[dict]) -> str:
    """The row text with the tender's identifying names swapped out.

    Entity substitution only: this text is a product description, and the
    PII regexes (a ten-digit "phone", a PAN-shaped code) would blank real
    catalogue and part numbers the search needs.
    """
    out = text or ""
    for placeholder, original in (anonymization_map or {}).items():
        if original and len(original) > 2:
            try:
                out = re.sub(re.escape(original), placeholder, out, flags=re.IGNORECASE)
            except re.error:
                continue
    return out


def _query(row: dict, anonymization_map: Optional[dict] = None) -> str:
    desc = re.sub(r"\s+", " ", (row.get("description") or "")).strip().strip('"')
    # The model writes its own search queries from this text, so whatever is
    # here can leave for the search backend: keep the tender's identity out.
    desc = anonymize_description(desc, anonymization_map)
    unit = (row.get("unit") or "unit").strip()
    return (
        f"Tender item: {desc[:600]}\n"
        f"Tender unit: {unit}\n"
        f"Find the current price in India for one {unit} of this item."
    )


def _parse(text: str) -> Optional[dict]:
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    return out if isinstance(out, dict) else None


def _accept(answer: Optional[dict], searched_urls: set[str]) -> Optional[dict]:
    """The research result as a costing line's evidence, or None."""
    if not answer or not answer.get("found"):
        return None
    if str(answer.get("match") or "").lower() not in _GOOD_MATCH:
        return None
    url = str(answer.get("url") or "").strip()
    if not url or url not in searched_urls:
        return None
    try:
        price = float(answer.get("price_per_tender_unit"))
    except (TypeError, ValueError):
        return None
    if not price > 0:
        return None
    return {
        "rate": round(price, 2),
        "url": url,
        "product": str(answer.get("product") or "")[:200],
        "price_date": str(answer.get("price_date") or "")[:40],
        "match": str(answer.get("match")).lower(),
        "note": str(answer.get("note") or "")[:300],
        "listed": answer.get("price_inr"),
        "per_unit": str(answer.get("per_unit") or "")[:40],
    }


async def _research_one(
    client, model: str, max_uses: int, row: dict, anonymization_map: Optional[dict] = None,
) -> Optional[dict]:
    started = time.monotonic()
    response = await client.messages.create(
        model=model,
        max_tokens=1200,
        system=_SYSTEM,
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max_uses,
            "user_location": {"type": "approximate", "country": "IN", "timezone": "Asia/Kolkata"},
        }],
        messages=[{"role": "user", "content": _query(row, anonymization_map)}],
    )
    try:
        from app.services.ai_service import _log_usage
        from app.services.langchain.provider_config import web_search_requests_of
        await asyncio.to_thread(
            _log_usage, None, "anthropic", model, "market_price_research",
            {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens,
             # Each search is billed on top of the tokens ($10 / 1,000);
             # up to max_uses per row, and it was never in the ledger.
             "web_search_requests": web_search_requests_of(response.usage)},
            int((time.monotonic() - started) * 1000), True,
        )
    except Exception:
        pass
    urls: set[str] = set()
    text: list[str] = []
    for block in response.content:
        if block.type == "web_search_tool_result" and isinstance(block.content, list):
            urls.update(getattr(item, "url", "") or "" for item in block.content)
        elif block.type == "text":
            text.append(block.text)
    return _accept(_parse("".join(text)), urls)


async def research_market_prices(
    rows: list[dict],
    *,
    api_key: str,
    model: str,
    max_uses: int = 3,
    concurrency: int = 8,
    deadline_s: float = 600.0,
    anonymization_map: Optional[dict] = None,
    cache_ttl_days: float = 0.0,
) -> dict:
    """{boq_item_id: evidence} for every row whose market price was found.

    Runs `concurrency` rows at a time and stops issuing new ones when
    `deadline_s` is spent; a row still in flight then is abandoned, never
    half-applied. A failed call is that row's loss only.

    With ``cache_ttl_days`` > 0 an item researched within that many days --
    same description, unit and model, found or not -- is answered from the
    stored result instead of a new web search (see `market_research_cache`).
    """
    import anthropic

    todo = [r for r in rows if r.get("boq_item_id") is not None and not not_a_market_item(r)]
    if not todo:
        return {}
    found: dict = {}
    cached_hits = 0
    if cache_ttl_days and cache_ttl_days > 0:
        from app.services.costing import market_research_cache as mrc
        remembered = await asyncio.to_thread(mrc.lookup_many, todo, model, cache_ttl_days)
        fresh: list[dict] = []
        for r in todo:
            key = mrc.item_key(r, model)
            if key in remembered:
                cached_hits += 1
                if remembered[key]:
                    found[r["boq_item_id"]] = remembered[key]
            else:
                fresh.append(r)
        todo = fresh
        if not todo:
            logger.info(
                f"[market_research] all {cached_hits} market-priceable row(s) answered "
                f"from research within {cache_ttl_days:g} days"
            )
            return found
    client = anthropic.AsyncAnthropic(api_key=api_key)
    slots = asyncio.Semaphore(max(1, int(concurrency)))
    stop_at = time.monotonic() + max(0.0, deadline_s)
    failed = 0
    answered: list[tuple[dict, Optional[dict]]] = []

    async def one(row: dict) -> None:
        nonlocal failed
        async with slots:
            left = stop_at - time.monotonic()
            if left <= 5:
                return
            try:
                ev = await asyncio.wait_for(
                    _research_one(client, model, max_uses, row, anonymization_map), timeout=left,
                )
            except Exception as e:  # noqa: BLE001 -- one row's failure is its own
                failed += 1
                logger.info(f"[market_research] row {row.get('boq_item_id')}: {type(e).__name__}: {e}")
                return
            # Found or not, the answer is worth remembering; a failed call is not.
            answered.append((row, ev))
            if ev:
                found[row["boq_item_id"]] = ev

    t0 = time.monotonic()
    await asyncio.gather(*(one(r) for r in todo))
    if cache_ttl_days and cache_ttl_days > 0 and answered:
        from app.services.costing import market_research_cache as mrc
        await asyncio.to_thread(mrc.store_many, answered, model)
    logger.info(
        f"[market_research] {len(found)} market-priceable row(s) priced from the web "
        f"({len(todo)} searched, {cached_hits} from recent research, {failed} failed) in "
        f"{time.monotonic() - t0:.0f}s"
    )
    return found


def as_batch_lines(found: dict) -> list[dict]:
    """The research results in the shape `merge_batch_rates` takes, marked
    as verified so `settle_rates_on_evidence` can tell them from a price
    the costing agent cited on its own."""
    from app.services.cost_breakdown_service import VERIFIED_WEB_PRICE
    lines = []
    for bid, ev in found.items():
        dated = f", {ev['price_date']}" if ev.get("price_date") else ""
        listed = ""
        if ev.get("listed") not in (None, "") and ev.get("per_unit"):
            listed = f" (listed Rs {ev['listed']} per {ev['per_unit']})"
        lines.append({
            "boq_item_id": bid,
            "rate": ev["rate"],
            "rate_source": "web_search",
            "source_url": ev["url"],
            "source_ref": f"{ev['product']}{dated}{listed} -- {ev['url']}",
            "cost_buildup_note": (
                f"{VERIFIED_WEB_PRICE}, {ev['match']} match: {ev['product']}{dated}. {ev['note']}".strip()
            ),
            "confidence": "high" if ev["match"] == "exact" else "medium",
        })
    return lines
