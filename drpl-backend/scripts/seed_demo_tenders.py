"""Sample railway tenders, so a fresh deployment has something to show.

Run standalone or from the boot script:

    python scripts/seed_demo_tenders.py

WHAT THIS IS, AND HOW TO GET RID OF IT
--------------------------------------
Every row written here is **synthetic**. The organisations, zones, work
descriptions and the shape of the money are modelled on real Indian Railways
tenders so the screens look like the product rather than like a fixture, but no
row corresponds to a bid that exists, and none of them should ever be acted on.

So every row is traceable and removable:

* ``portal`` / ``source_portal`` are the real values (``gem`` / ``ireps``),
  because the list view, the funnel counts and the portal filters all key on
  them and a made-up portal would make the demo look broken instead of real;
* ``search_match_keyword`` is set to ``DEMO_MARKER`` on every row. That column
  is indexed, is not rendered anywhere in the tender list, and nothing in the
  scoring or costing path branches on its value -- which makes it the one field
  that can carry this without changing behaviour.

To remove the whole sample set later:

    DELETE FROM tenders WHERE search_match_keyword = 'drpl-demo-seed';

``scripts/seed_demo_tenders.py --purge`` does exactly that, and reports the
count. Idempotent: a row is matched on (portal, tender_id) and updated rather
than duplicated, so this is safe on every boot.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal  # noqa: E402
from app.models.tender import Tender  # noqa: E402

#: The marker every synthetic row carries. See the module docstring.
DEMO_MARKER = "drpl-demo-seed"

_ZONES = [
    ("Eastern Railway", "Liluah Workshop, Howrah, West Bengal"),
    ("Northern Railway", "Jagadhri Workshop, Yamunanagar, Haryana"),
    ("Western Railway", "Lower Parel Workshop, Mumbai, Maharashtra"),
    ("Southern Railway", "Perambur Carriage Works, Chennai, Tamil Nadu"),
    ("Central Railway", "Matunga Workshop, Mumbai, Maharashtra"),
    ("South Eastern Railway", "Kharagpur Workshop, West Bengal"),
    ("North Eastern Railway", "Gorakhpur Workshop, Uttar Pradesh"),
    ("Integral Coach Factory", "ICF Perambur, Chennai, Tamil Nadu"),
    ("Rail Coach Factory", "RCF Kapurthala, Punjab"),
    ("Modern Coach Factory", "MCF Raebareli, Uttar Pradesh"),
]

_DEPARTMENTS = ["Mechanical", "Electrical", "S&T", "Engineering", "Stores"]

# (title, category, department hint, value in rupees, bid type)
_WORKS = [
    ("Conversion of 15 ICF coaches into Automobile Carrier-cum-Brake Van "
     "(NMGHSR) including supply of material as per Annexure-II",
     "Coach conversion & rehabilitation", "Mechanical", 73_321_140.60, "NCB"),
    ("Mid-life rehabilitation of 12 LHB AC 3-Tier coaches including interior "
     "panelling, wiring and bogie overhaul",
     "Coach conversion & rehabilitation", "Mechanical", 173_151_893.83, "NCB"),
    ("Supply and fitment of stainless steel roof sheets IRSM-44 for 40 BCNA "
     "wagons",
     "Wagon repair & fabrication", "Mechanical", 28_640_500.00, "NCB"),
    ("Annual maintenance contract for air brake overhauling of 300 BOXNHL "
     "wagons",
     "AMC - rolling stock", "Mechanical", 41_275_000.00, "NCB"),
    ("Supply of ELRS elastomeric cable 4 sq mm to RDSO spec for EMU "
     "rewiring work",
     "Electrical supply", "Electrical", 9_842_300.00, "Limited"),
    ("Overhauling of 4.5 kW roof mounted package unit (RMPU) for 60 LHB "
     "coaches",
     "AC & RMPU overhaul", "Electrical", 98_412_000.00, "NCB"),
    ("Fabrication and supply of underframe members to drawing LE11185 for "
     "coach shell assembly",
     "Fabrication", "Mechanical", 54_780_900.00, "NCB"),
    ("Painting of 25 BG coaches with polyurethane paint system as per "
     "Annexure-VII material list",
     "Painting & surface treatment", "Mechanical", 12_567_400.00, "NCB"),
    ("Supply, installation and commissioning of LED lighting in 80 non-AC "
     "coaches",
     "Electrical supply", "Electrical", 22_190_000.00, "NCB"),
    ("Comprehensive AMC for workshop overhead cranes (5T to 20T) for three "
     "years",
     "AMC - plant & machinery", "Engineering", 18_450_000.00, "NCB"),
    ("Supply of CBC couplers and draft gear assemblies for wagon rebuilding "
     "programme",
     "Wagon repair & fabrication", "Mechanical", 67_920_000.00, "GCB"),
    ("Rehabilitation of 8 diesel electric tower cars including engine "
     "overhaul",
     "Coach conversion & rehabilitation", "Mechanical", 88_300_000.00, "NCB"),
    ("Supply of EDTS-200 crimping sockets and terminal fittings for "
     "electrical workshop",
     "Electrical supply", "Electrical", 3_128_000.00, "Limited"),
    ("Sheet metal repair and flooring replacement of 30 goods brake vans",
     "Wagon repair & fabrication", "Mechanical", 34_670_000.00, "NCB"),
    ("Annual maintenance of signalling relay room and point machines at "
     "workshop yard",
     "AMC - S&T", "S&T", 7_890_000.00, "Limited"),
    ("Supply of low alloy high tensile (LAHT) steel plates IRSM-41 for "
     "wagon body repair",
     "Steel & raw material", "Stores", 45_330_000.00, "GCB"),
    ("Manufacture and supply of bogie bolster springs to RDSO "
     "specification",
     "Fabrication", "Mechanical", 15_760_000.00, "NCB"),
    ("Deep cleaning and pest control of 200 coaches on contract basis for "
     "two years",
     "Housekeeping services", "Mechanical", 4_320_000.00, "NCB"),
    ("Supply and fitment of modular toilet units in 45 LHB coaches",
     "Coach interiors", "Mechanical", 61_200_000.00, "NCB"),
    ("Overhaul of traction motors TM4906AZ for electric locomotives",
     "Electrical overhaul", "Electrical", 112_480_000.00, "GCB"),
    ("Supply of GFRP interior panels and window assemblies for coach "
     "refurbishment",
     "Coach interiors", "Mechanical", 38_950_000.00, "NCB"),
    ("Providing and fixing of vestibule gangway assemblies in 20 rakes",
     "Coach interiors", "Mechanical", 26_780_000.00, "NCB"),
    ("Supply of multimedia projectors and display units for training "
     "centre",
     "IT & office equipment", "Stores", 1_240_000.00, "Limited"),
    ("Annual maintenance contract for workshop compressed air system and "
     "pipelines",
     "AMC - plant & machinery", "Engineering", 5_670_000.00, "Limited"),
    ("Rebuilding of 50 BOXN wagons including bogie and brake rigging "
     "overhaul",
     "Wagon repair & fabrication", "Mechanical", 148_900_000.00, "GCB"),
    ("Supply of axle mounted disc brake pads for LHB coach maintenance",
     "Spares supply", "Mechanical", 8_970_000.00, "Limited"),
    ("Conversion of 20 Hybrid coaches to SNMGHSR configuration with "
     "material as per Annexure-IV",
     "Coach conversion & rehabilitation", "Mechanical", 95_640_000.00, "NCB"),
    ("Supply and commissioning of CNC plasma cutting machine for wagon shop",
     "Plant & machinery", "Engineering", 32_100_000.00, "GCB"),
]


def _bid_number(portal: str, index: int) -> str:
    """A plausible portal reference. The number is synthetic."""
    if portal == "gem":
        return f"GEM/2026/B/{5_840_000 + index * 137}"
    return f"{index % 9 + 1}0{index:03d}-{2026}-DRPL"


def _rows() -> list[dict]:
    # Fixed seed: every container instance generates the identical set, so the
    # demo does not change between page loads when the platform is running more
    # than one instance (each has its own local database).
    rnd = random.Random(20260927)
    now = datetime.now(timezone.utc)
    out: list[dict] = []

    for i, (title, category, dept, value, bid_type) in enumerate(_WORKS):
        portal = "gem" if i % 3 != 2 else "ireps"
        org, location = _ZONES[i % len(_ZONES)]
        closes_in = rnd.randint(4, 45)
        emd = round(value * rnd.uniform(0.018, 0.022), -2)

        # Score, then let the platform's own thresholds decide the pile, so the
        # funnel counts on the dashboard are internally consistent rather than
        # asserted separately.
        score = round(rnd.uniform(0.28, 0.94), 2)
        below = value < 5_000_000
        if score >= 0.60 and not below:
            segment = "to_bid"
        elif score < 0.40:
            segment = "discarded"
        else:
            segment = "not_bidable"

        out.append(dict(
            portal=portal,
            tender_id=_bid_number(portal, 100 + i),
            source_portal=portal,
            title=title,
            department=dept or _DEPARTMENTS[i % len(_DEPARTMENTS)],
            organisation=org,
            location=location,
            category=category,
            bid_type=bid_type,
            description=(
                f"{title}. Work to be executed at {location} under the "
                f"supervision of the {dept} department, {org}. Rates to be "
                f"quoted inclusive of all taxes, freight and insurance unless "
                f"stated otherwise in the schedule."
            ),
            estimated_value=value,
            emd_amount=emd,
            currency="INR",
            opening_date=now - timedelta(days=rnd.randint(2, 20)),
            closing_date=now + timedelta(days=closes_in),
            status="open",
            ai_category=category,
            ai_relevance_score=score,
            ai_risk_score=round(rnd.uniform(0.1, 0.6), 2),
            ai_summary=(
                f"{category} work for {org}. Estimated value "
                f"Rs {value:,.0f}, EMD Rs {emd:,.0f}, closing in "
                f"{closes_in} days."
            ),
            eligibility_status="eligible" if score >= 0.5 else "unknown",
            eligibility_score=score,
            segment=segment,
            below_threshold=below,
            scoring_attempts=1,
            priority="high" if score >= 0.75 else ("medium" if score >= 0.5 else "low"),
            workflow_status="new",
            performance_guarantee_percent=5.0,
            delivery_location=location,
            delivery_timeline=f"{rnd.choice([6, 9, 12, 18, 24])} months from LOA",
            fit_reasoning=(
                "Scope matches the firm's coach and wagon rehabilitation "
                "capability; workshop location is within the operating region."
                if score >= 0.6 else
                "Scope is outside the firm's core rolling-stock work."
            ),
            # The marker. Indexed, never rendered, and nothing branches on it.
            search_match_keyword=DEMO_MARKER,
        ))
    return out


def seed(db) -> dict:
    """Write the sample set. Returns {created, updated}."""
    created = updated = 0
    for row in _rows():
        existing = (
            db.query(Tender)
            .filter(Tender.portal == row["portal"],
                    Tender.tender_id == row["tender_id"])
            .first()
        )
        if existing:
            # Only ever refresh a row this script owns.
            if existing.search_match_keyword == DEMO_MARKER:
                for key, value in row.items():
                    setattr(existing, key, value)
                updated += 1
            continue
        db.add(Tender(**row))
        created += 1
    db.commit()
    return {"created": created, "updated": updated}


def purge(db) -> int:
    n = db.query(Tender).filter(Tender.search_match_keyword == DEMO_MARKER).delete()
    db.commit()
    return n


def main() -> int:
    db = SessionLocal()
    try:
        if "--purge" in sys.argv:
            print(f"[seed_demo_tenders] purged {purge(db)} sample tender(s)")
            return 0
        result = seed(db)
        total = db.query(Tender).count()
        print(f"[seed_demo_tenders] created {result['created']}, "
              f"updated {result['updated']}; {total} tender(s) in total")
    except Exception as exc:  # noqa: BLE001 -- never block a boot
        db.rollback()
        print(f"[seed_demo_tenders] FAILED: {type(exc).__name__}: {exc}")
        return 0
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
