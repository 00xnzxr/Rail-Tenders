"""The entity map that keeps a tender's identity out of costing searches.

`redact_costing_query` swaps each original string in this map for its
placeholder before a query leaves the platform. The map used to come from an
LLM call per costing that read 3,000 characters of the analysis JSON and was
asked to name the companies, tender references and sites in it. That call
cost a model round-trip on every costing, saw only the start of the analysis,
and when it failed (or there was no analysis) the map fell back to "DRPL"
alone -- while the tender's own reference number and buyer sat in the
database the whole time.

This builds the map from the records that *are* the tender's identity, which
are complete and cannot be missed by a model:

* the tender row: portal reference, buying organisation, buyer contact;
* the firm's own name, from its letterheads;
* reference numbers the analysis labels as the tender's ("Tender No.",
  "NIT No", "Bid Number" -- ``RCT/2024-25/0178``, ``GEM/2025/B/6512345``).

Deliberately *not* included: the tender title and item descriptions (they are
what the search is for), and a one-word department such as "Mechanical",
which would blank the word out of an honest query ("mechanical seal price").
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Always anonymised, whatever the records say.
ALWAYS = ("DRPL Manufacturing", "DRPL")

#: A reference number the document itself labels as the tender's: "Tender
#: No. RCT/2024-25/0178", "NIT No: 05241234/2026", "Bid Number:
#: GEM/2025/B/6512345". Labelled only, on purpose -- a drawing number such as
#: ICF/SK-3-6-002 has the same shape and is exactly what a costing search
#: needs to keep.
_REF_RE = re.compile(
    r"(?:e-?\s*tender|tender|nit|bid|rfp|rfq|ref(?:erence)?)\s*"
    r"(?:no\.?|number|num|#|id)\s*[:.\-]?\s*"
    r"([A-Z0-9][A-Z0-9/\-_.]{5,}[A-Z0-9])",
    re.IGNORECASE,
)

_MIN_CHARS = 4


def _usable(value: Optional[str]) -> Optional[str]:
    """A string worth anonymising, or None."""
    if not value:
        return None
    value = " ".join(str(value).split())
    if len(value) < _MIN_CHARS:
        return None
    # A single ordinary word ("Mechanical", "Stores") is a search term too.
    if " " not in value and not re.search(r"[\d/@.]", value):
        return None
    return value


def reference_numbers(text: str) -> list[str]:
    """Labelled tender reference numbers in ``text`` that carry a digit."""
    found = {m.group(1) for m in _REF_RE.finditer(text or "") if re.search(r"\d", m.group(1))}
    return sorted(found, key=len, reverse=True)


def build_anonymization_map(
    db: Optional[Session],
    tender_id: Optional[int],
    analysis_result: Optional[dict] = None,
    extra: Iterable[str] = (),
) -> dict:
    """Placeholder -> original, longest originals first (so a longer name is
    replaced before a shorter one it contains)."""
    originals: list[str] = []

    if db is not None and tender_id:
        try:
            from app.models.tender import Tender

            t = db.query(Tender).filter(Tender.id == tender_id).first()
            if t is not None:
                originals += [
                    getattr(t, "tender_id", None),
                    getattr(t, "organisation", None),
                    getattr(t, "department", None),
                    getattr(t, "buyer_contact_name", None),
                    getattr(t, "buyer_contact_email", None),
                    getattr(t, "buyer_contact_phone", None),
                ]
        except Exception as e:
            logger.warning(f"[anonymization] tender {tender_id} unreadable: {e}")
            try:
                db.rollback()
            except Exception:
                pass

    if db is not None:
        try:
            from app.models.letterhead import LetterheadTemplate

            for (override,) in db.query(LetterheadTemplate.company_name_override).all():
                originals.append(override)
        except Exception as e:
            logger.debug(f"[anonymization] letterheads unreadable: {e}")
            try:
                db.rollback()
            except Exception:
                pass

    if analysis_result:
        try:
            originals += reference_numbers(json.dumps(analysis_result, default=str))
        except Exception:
            pass

    originals += list(extra)

    seen: set[str] = set()
    cleaned: list[str] = []
    for value in originals:
        v = _usable(value)
        if v and v.lower() not in seen:
            seen.add(v.lower())
            cleaned.append(v)
    cleaned.sort(key=len, reverse=True)

    mapping: dict = {}
    for i, v in enumerate(cleaned, start=1):
        mapping[f"[ENTITY-{i}]"] = v
    # The firm's short name is not "usable" by the one-word rule, and it must
    # always go: add both forms last so they never shadow a longer entity.
    for j, v in enumerate(ALWAYS, start=1):
        if v.lower() not in seen:
            mapping[f"[REDACTED-COMPANY-{j}]"] = v
    return mapping
