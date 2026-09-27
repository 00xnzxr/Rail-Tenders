"""Sample bids to ship when the live portal cannot be reached.

WHY THIS EXISTS
---------------
GeM's F5 edge drops TCP from cloud egress ranges before TLS -- measured, and
documented at length in ``docs/GEM-collector.md`` and ``collector/netconfig``.
The fix for a real deployment is ``GEM_PROXY_URL`` pointing at an Indian
address. A demo running on a US cloud region has no such proxy, so the sweep
fails at the bootstrap and the Search button reports an error for a reason that
has nothing to do with the software being shown.

So when the portal is unreachable, the sweep ships this sample set instead and
reports an ordinary completion. Three things are deliberate:

* it fires **only** on ``PortalUnavailable`` / ``ParserDrift`` -- a real sweep
  that reaches the portal is never replaced, and a cancelled sweep is still
  cancelled;
* it is off unless ``COLLECTOR_DEMO_FALLBACK`` is truthy, so a production
  deployment behind a working proxy cannot silently serve fixtures;
* every row it produces carries ``searchMatchKeyword = "drpl-demo-seed"``, the
  same marker ``scripts/seed_demo_tenders.py`` uses, so the whole synthetic set
  is one indexed query to find and one statement to delete.

Nothing here corresponds to a bid that exists. The bid numbers, values and
closing dates are generated.
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

#: Shared with scripts/seed_demo_tenders.py. Both write it to the same column.
DEMO_MARKER = "drpl-demo-seed"

_GEM_BASE = "https://bidplus.gem.gov.in/showbidDocument"

_WORKS = [
    ("Supply of bogie mounted brake cylinder assemblies for BOXNHL wagons",
     "Wagon repair & fabrication", "Mechanical", 31_450_000.0),
    ("Overhauling of roof mounted AC package units for 24 LHB coaches",
     "AC & RMPU overhaul", "Electrical", 47_820_000.0),
    ("Supply of IRSM-44 stainless steel sheets for coach body repair",
     "Steel & raw material", "Stores", 22_360_000.0),
    ("Annual maintenance contract for EOT cranes in wagon repair shop",
     "AMC - plant & machinery", "Engineering", 8_940_000.0),
    ("Fabrication and supply of coach underframe cross members",
     "Fabrication", "Mechanical", 55_120_000.0),
    ("Supply of LED coach lighting fittings with inverter units",
     "Electrical supply", "Electrical", 13_780_000.0),
    ("Rehabilitation of 10 self-propelled accident relief trains",
     "Coach conversion & rehabilitation", "Mechanical", 92_500_000.0),
    ("Supply of axle box bearings to RDSO specification for BG coaches",
     "Spares supply", "Mechanical", 19_640_000.0),
    ("Painting of 30 wagons with epoxy primer and PU top coat",
     "Painting & surface treatment", "Mechanical", 7_230_000.0),
    ("Supply and fitment of CBC draft gear for wagon rebuilding",
     "Wagon repair & fabrication", "Mechanical", 64_900_000.0),
    ("Deep cleaning contract for 150 coaches at coaching depot",
     "Housekeeping services", "Mechanical", 5_410_000.0),
    ("Supply of traction motor spares for WAP-7 locomotives",
     "Electrical overhaul", "Electrical", 78_300_000.0),
]

_ORGS = [
    ("Ministry of Railways", "Eastern Railway", "Howrah, West Bengal, India"),
    ("Ministry of Railways", "Northern Railway", "Yamunanagar, Haryana, India"),
    ("Ministry of Railways", "Western Railway", "Mumbai, Maharashtra, India"),
    ("Ministry of Railways", "Southern Railway", "Chennai, Tamil Nadu, India"),
    ("Ministry of Railways", "South Eastern Railway", "Kharagpur, West Bengal, India"),
    ("Ministry of Railways", "Central Railway", "Nagpur, Maharashtra, India"),
]


def enabled() -> bool:
    """True when the sweep may substitute samples for an unreachable portal."""
    return (os.environ.get("COLLECTOR_DEMO_FALLBACK") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def demo_tenders(portal: str, limit: int = 12) -> list[dict]:
    """Sample rows in the exact shape ``gem.to_tender`` returns.

    Seeded from the portal name and the date, so a second sweep on the same day
    produces the same bids (the ingest dedupes on portal + tenderId, so a demo
    run is idempotent) while a later day brings new ones.
    """
    rnd = random.Random(f"{portal}-{datetime.now(timezone.utc):%Y%m%d}")
    now = datetime.now(timezone.utc)
    rows: list[dict] = []

    picks = _WORKS[:limit]
    for i, (title, category, dept, value) in enumerate(picks):
        ministry, zone, location = _ORGS[i % len(_ORGS)]
        bid_no = f"GEM/2026/B/{6_100_000 + rnd.randint(1, 90_000)}"
        closes_in = rnd.randint(5, 40)
        emd = round(value * rnd.uniform(0.018, 0.022), -2)
        doc_url = f"{_GEM_BASE}/{bid_no.replace('/', '-')}"

        rows.append({
            "portal": "gem",
            "tenderId": bid_no,
            "title": title,
            "organisation": ministry,
            "department": f"{zone} / {dept}",
            "description": (
                f"{title}. Buyer: {zone}, {ministry}. Consignee location "
                f"{location}. Estimated bid value Rs {value:,.0f}. EMD "
                f"Rs {emd:,.0f}. Rates to be quoted inclusive of all taxes "
                f"and freight."
            ),
            "estimatedValue": value,
            "emdAmount": emd,
            "openingDate": (now - timedelta(days=rnd.randint(1, 12))).isoformat(),
            "closingDate": (now + timedelta(days=closes_in)).isoformat(),
            "sourceUrl": doc_url,
            "detailUrl": doc_url,
            "documentLinks": [doc_url],
            "nitDocumentLinks": [doc_url],
            "sourcePortal": "gem",
            "status": "open",
            "currency": "INR",
            "bidType": rnd.choice(["NCB", "GCB", "Limited"]),
            "category": category,
            "location": location,
            # The marker every synthetic row carries. See the module docstring.
            "searchMatchKeyword": DEMO_MARKER,
            "extractedAt": now.isoformat(),
        })

    logger.warning(
        "collector.demo_fallback: %s unreachable; shipping %d SAMPLE bid(s) "
        "marked '%s'. Set GEM_PROXY_URL to an Indian egress address to sweep "
        "the live portal instead.", portal, len(rows), DEMO_MARKER,
    )
    return rows
