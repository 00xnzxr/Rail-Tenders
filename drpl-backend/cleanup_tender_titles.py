"""
One-time, idempotent repair of corrupt tender titles already in the DB.

Fixes IREPS titles like '...of......\tTender Type:\tOpen' by stripping the
ellipsis + 'Tender Type:' suffix. GeM rows stored as 'Quantity: 5370' have NO
recoverable title — these are reported as "needs re-scrape" and left unchanged
(re-scraping with the fixed extension repairs them). Safe to run multiple times.

Usage:  python cleanup_tender_titles.py
"""

import logging

from app.core.database import SessionLocal
from app.models.tender import Tender
from app.services.tender_service import clean_tender_title, is_corrupt_title

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("cleanup_tender_titles")


def main() -> None:
    db = SessionLocal()
    fixed = 0
    needs_rescrape = 0
    unchanged = 0
    try:
        # Pull only candidate rows (corrupt markers). Broad LIKE prefilter; the
        # is_corrupt_title() check below is the real gate.
        candidates = (
            db.query(Tender)
            .filter(
                (Tender.title.like("%......%"))
                | (Tender.title.like("%Tender Type:%"))
                | (Tender.title.like("Quantity:%"))
                | (Tender.title.like("%\xa0%"))
            )
            .all()
        )
        log.info("Found %d candidate rows", len(candidates))

        for t in candidates:
            cleaned = clean_tender_title(t.title)
            # Unrecoverable: cleaning still leaves a corrupt/quantity-only value.
            if not cleaned or is_corrupt_title(cleaned):
                needs_rescrape += 1
                log.info("NEEDS RE-SCRAPE id=%s portal=%s tender_id=%s title=%r",
                         t.id, t.portal, t.tender_id, t.title)
                continue
            if cleaned != t.title:
                t.title = cleaned
                fixed += 1
            else:
                unchanged += 1

        if fixed:
            db.commit()
        log.info("Done. fixed=%d needs_rescrape=%d unchanged=%d", fixed, needs_rescrape, unchanged)
    finally:
        db.close()


if __name__ == "__main__":
    main()
