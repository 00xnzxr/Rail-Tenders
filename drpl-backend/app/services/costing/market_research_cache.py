"""Recent market research, reused instead of searched again.

Every costing ran one Claude web-search call per market-priceable row --
about Rs 300 on the Mid-Life NIT -- and a re-cost of the same tender (the
ordinary case: a re-upload, a corrected annexure, "run it again") paid all of
it a second time for the same items on the same day. Market prices do not
move in a fortnight, and items recur across tenders (4 sq mm cable, LED
fittings, standard valves), so a row whose exact description and unit were
researched on the same model within ``costing.market_research_cache_days``
(default 14; 0 turns this off) is answered from that research.

What is stored is the research outcome, not a guess: the accepted evidence
(price, the URL that search returned, match grade) or a recorded "no market
price" -- which is worth as much, because RDSO/EDTS-spec items essentially
never have a public listing and were searched on every run. A call that
*failed* is never stored, so a transient outage cannot freeze a miss in.

Stored in the page-vision cache table under its own ``mkt:`` key namespace,
like annexure discovery: that table is only read by exact key, so nothing
that reads pages can ever see these rows. The evidence is public web data,
so it is shared across users like the tender facts it prices.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

MARKET_RESEARCH_METHOD = "market_research"
_PAGE_INDEX = -2


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().strip('"')).lower()


def item_key(row: dict, model: str) -> str:
    """Cache identity: the exact item text and unit, and the research model."""
    basis = f"{_normal(row.get('description'))}\n{_normal(row.get('unit') or 'unit')}\n{model or ''}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def _source_key(key: str) -> str:
    return f"mkt:{key}"[:64]


def lookup_many(rows: list[dict], model: str, ttl_days: float) -> dict:
    """{item_key: evidence-or-None} for rows researched within ``ttl_days``.

    A key absent from the result was not researched recently. A key present
    with ``None`` was researched and had no acceptable market price.
    """
    from app.core.database import SessionLocal
    from app.models.pdf_vision_cache import DocumentPageVisionCache

    keys = {item_key(r, model) for r in rows}
    if not keys:
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=float(ttl_days))
    out: dict = {}
    session = SessionLocal()
    try:
        source_keys = [_source_key(k) for k in keys]
        for i in range(0, len(source_keys), 500):
            chunk = source_keys[i:i + 500]
            rows_ = (
                session.query(DocumentPageVisionCache)
                .filter(
                    DocumentPageVisionCache.source_key.in_(chunk),
                    DocumentPageVisionCache.page_index == _PAGE_INDEX,
                    DocumentPageVisionCache.method == MARKET_RESEARCH_METHOD,
                )
                .all()
            )
            for rec in rows_:
                created = rec.created_at
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created is None or created < cutoff:
                    continue
                try:
                    payload = json.loads(rec.text or "{}")
                except ValueError:
                    continue
                out[rec.page_sha1] = payload.get("evidence") or None
    except Exception as e:
        logger.warning(f"[market_research] cache lookup failed: {e}")
        return {}
    finally:
        session.close()
    return out


def store_many(answered: list[tuple[dict, Optional[dict]]], model: str) -> None:
    """Record each answered row's outcome (evidence, or None for no price)."""
    from app.core.database import SessionLocal
    from app.models.pdf_vision_cache import DocumentPageVisionCache

    if not answered:
        return
    session = SessionLocal()
    try:
        seen: set[str] = set()
        for row, evidence in answered:
            key = item_key(row, model)
            if key in seen:
                continue
            seen.add(key)
            session.query(DocumentPageVisionCache).filter(
                DocumentPageVisionCache.source_key == _source_key(key),
                DocumentPageVisionCache.page_index == _PAGE_INDEX,
                DocumentPageVisionCache.page_sha1 == key,
            ).delete(synchronize_session=False)
            session.add(DocumentPageVisionCache(
                source_key=_source_key(key),
                page_index=_PAGE_INDEX,
                page_sha1=key,
                text=json.dumps({"evidence": evidence}, ensure_ascii=False, default=str),
                method=MARKET_RESEARCH_METHOD,
                model=(model or "")[:100],
            ))
        session.commit()
    except Exception as e:
        logger.warning(f"[market_research] could not store research results: {e}")
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()
