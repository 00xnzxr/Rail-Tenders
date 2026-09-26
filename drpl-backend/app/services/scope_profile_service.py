"""
DRPL Backend - Tender Scope Profile Service

CRUD + auto-seed for the singleton TenderScopeProfile row. Also provides a
serialiser used by /api/extension/config to ship scope to the extension and
a context-builder fed into the relevance agent prompt at ingest time.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.tender import Tender
from app.models.tender_scope_profile import TenderScopeProfile


DEFAULT_PROFILE_NAME = "default"

# Conservative starter set — admin can edit via the new Tender Scope page.
# Reflects DRPL's stated focus: railway mechanical/electrical AMC + spares.
_SEED_KEYWORD_GROUPS = [
    {
        "label": "AMC / Maintenance Services",
        "keywords": ["AMC", "CMC", "Annual Maintenance", "Comprehensive Maintenance", "Custom Bid for Services"],
    },
    {
        "label": "Mechanical Spares",
        "keywords": ["bogie", "coupler", "brake", "axle", "wheelset", "compressor"],
    },
    {
        "label": "Electrical / Traction",
        "keywords": ["traction motor", "transformer", "signaling", "power supply", "electrical spares"],
    },
]
_SEED_TARGET_MINISTRIES = ["Ministry of Railways"]


def get_active_profile(db: Session) -> TenderScopeProfile:
    """Return the singleton 'default' profile, auto-creating it on first call."""
    profile = (
        db.query(TenderScopeProfile)
        .filter(TenderScopeProfile.name == DEFAULT_PROFILE_NAME)
        .first()
    )
    if profile:
        return profile

    profile = TenderScopeProfile(
        name=DEFAULT_PROFILE_NAME,
        keyword_groups=_SEED_KEYWORD_GROUPS,
        exclusion_terms=[],
        target_ministries=_SEED_TARGET_MINISTRIES,
        value_min=None,
        value_max=None,
        relevance_threshold=0.6,
        is_active=True,
        updated_by=None,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def upsert_profile(
    db: Session,
    payload: dict,
    user_id: Optional[int],
) -> TenderScopeProfile:
    """Update fields on the singleton profile. Creates it if missing."""
    profile = get_active_profile(db)

    if "keyword_groups" in payload:
        profile.keyword_groups = _normalise_keyword_groups(payload["keyword_groups"])
    if "exclusion_terms" in payload:
        profile.exclusion_terms = [s.strip() for s in (payload["exclusion_terms"] or []) if s and s.strip()]
    if "target_ministries" in payload:
        profile.target_ministries = [s.strip() for s in (payload["target_ministries"] or []) if s and s.strip()]
    if "value_min" in payload:
        profile.value_min = payload["value_min"]
    if "value_max" in payload:
        profile.value_max = payload["value_max"]
    if "relevance_threshold" in payload:
        threshold = payload["relevance_threshold"]
        if threshold is not None:
            profile.relevance_threshold = max(0.0, min(1.0, float(threshold)))
    if "max_pages_per_keyword" in payload:
        raw = payload["max_pages_per_keyword"]
        if raw is not None:
            try:
                profile.max_pages_per_keyword = max(1, int(raw))
            except (TypeError, ValueError):
                pass
    if "is_active" in payload:
        profile.is_active = bool(payload["is_active"])

    profile.updated_by = user_id
    profile.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(profile)
    return profile


def _normalise_keyword_groups(groups: list) -> list:
    """Drop empty rows; trim whitespace; ensure each group has label + keywords."""
    out = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        label = (g.get("label") or "").strip()
        keywords = [
            (k or "").strip()
            for k in (g.get("keywords") or [])
            if isinstance(k, str) and k.strip()
        ]
        if not label and not keywords:
            continue
        out.append({"label": label or "Group", "keywords": keywords})
    return out


def serialize_for_extension(
    profile: TenderScopeProfile,
    db: Optional[Session] = None,
) -> dict:
    """Shape consumed by the extension via /api/extension/config.

    When `db` is provided, also includes a `historical_fallback` block derived
    from past GeM tenders — the extension uses it when the scope profile's
    keyword_groups are sparse (see gem-search-driver.ts).
    """
    return {
        "name": profile.name,
        "keyword_groups": profile.keyword_groups or [],
        "exclusion_terms": profile.exclusion_terms or [],
        "target_ministries": profile.target_ministries or [],
        "value_min": profile.value_min,
        "value_max": profile.value_max,
        "relevance_threshold": profile.relevance_threshold or 0.6,
        "max_pages_per_keyword": int(profile.max_pages_per_keyword or 5),
        "historical_fallback": _build_historical_fallback(db) if db is not None else {"keywords": [], "ministries": [], "departments": []},
        "is_active": profile.is_active,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


# Stopwords + boilerplate tokens we never want to push into BOQ Title search.
_HISTORICAL_STOPWORDS = {
    "for", "the", "of", "and", "to", "a", "in", "on", "by", "at", "with",
    "from", "or", "as", "is", "an", "be", "this", "that", "tender", "bid",
    "supply", "supplying", "providing", "procurement", "purchase", "various",
    "items", "item", "ministry", "department", "ltd", "limited", "private",
    "indian", "railway", "railways", "south", "north", "east", "west", "central",
    "zone", "zonal", "division", "divisional", "office", "company", "corporation",
}


def _build_historical_fallback(db: Session) -> dict:
    """Derive fallback search hints from past GeM tenders.

    Returns:
      - keywords: top recurring tokens from past titles (for BOQ-style search)
      - ministries: top distinct buyer orgs / railway zones
      - departments: top distinct department values (for bidnext Department filter)

    Used by the extension's auto-search driver when the admin-curated scope
    profile is sparse. Best-effort; on any failure returns an empty payload
    so the driver can still run with whatever the admin has configured.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=180)

        # Top ministries / buyer organisations (Railway Zone on bidnext)
        ministry_rows = (
            db.query(Tender.organisation, func.count(Tender.id).label("n"))
            .filter(Tender.portal == "gem")
            .filter(Tender.organisation.isnot(None))
            .filter(Tender.organisation != "")
            .filter(Tender.created_at >= cutoff)
            .group_by(Tender.organisation)
            .order_by(func.count(Tender.id).desc())
            .limit(5)
            .all()
        )
        ministries = [r.organisation for r in ministry_rows if r.organisation]

        # Top departments (Department dropdown on bidnext: Stores, Mechanical, etc.)
        department_rows = (
            db.query(Tender.department, func.count(Tender.id).label("n"))
            .filter(Tender.portal == "gem")
            .filter(Tender.department.isnot(None))
            .filter(Tender.department != "")
            .filter(Tender.created_at >= cutoff)
            .group_by(Tender.department)
            .order_by(func.count(Tender.id).desc())
            .limit(5)
            .all()
        )
        departments = [r.department for r in department_rows if r.department]

        # Mine recurring tokens from titles + departments (1-grams + 2-grams)
        title_rows = (
            db.query(Tender.title, Tender.department)
            .filter(Tender.portal == "gem")
            .filter(Tender.created_at >= cutoff)
            .limit(2000)
            .all()
        )

        token_counts: dict = {}
        for title, department in title_rows:
            text = " ".join(filter(None, [title, department])).lower()
            words = [w.strip(".,;:()[]{}\"'/-_") for w in text.split()]
            words = [w for w in words if w and len(w) > 2 and not w.isdigit() and w not in _HISTORICAL_STOPWORDS]
            for w in words:
                token_counts[w] = token_counts.get(w, 0) + 1
            for i in range(len(words) - 1):
                bigram = f"{words[i]} {words[i + 1]}"
                token_counts[bigram] = token_counts.get(bigram, 0) + 1

        ranked = sorted(token_counts.items(), key=lambda kv: kv[1], reverse=True)
        # Require the token to appear in at least 3 tenders to count.
        keywords = [tok for tok, n in ranked if n >= 3][:10]

        return {"keywords": keywords, "ministries": ministries, "departments": departments}
    except Exception:
        return {"keywords": [], "ministries": [], "departments": []}


def serialize_for_admin(profile: TenderScopeProfile, db: Optional[Session] = None) -> dict:
    """Same shape as serialize_for_extension but with bookkeeping fields."""
    base = serialize_for_extension(profile, db=db)
    base["id"] = profile.id
    base["updated_by"] = profile.updated_by
    base["created_at"] = profile.created_at.isoformat() if profile.created_at else None
    return base


def build_relevance_prompt_context(profile: TenderScopeProfile) -> str:
    """Render scope profile into a system-prompt suffix appended to the
    canonical relevance agent prompt at scoring time.
    """
    if not profile or not profile.is_active:
        return ""

    lines = ["", "DRPL TENDER SCOPE PROFILE (admin-configured):"]

    groups = profile.keyword_groups or []
    if groups:
        lines.append("Target work areas:")
        for g in groups:
            kws = ", ".join(g.get("keywords") or [])
            lines.append(f"- {g.get('label', 'Group')}: {kws}")

    if profile.target_ministries:
        lines.append(f"Preferred buyers: {', '.join(profile.target_ministries)}")

    if profile.exclusion_terms:
        lines.append(
            "Score LOW (≤0.3) if the tender title or description prominently "
            f"matches any of these exclusion terms: {', '.join(profile.exclusion_terms)}."
        )

    if profile.value_min is not None or profile.value_max is not None:
        lo = f"{profile.value_min:.0f} INR" if profile.value_min is not None else "no min"
        hi = f"{profile.value_max:.0f} INR" if profile.value_max is not None else "no max"
        lines.append(f"Preferred estimated value range: {lo} – {hi}.")

    lines.append(
        "Weight the score primarily on how closely the tender's scope matches "
        "the target work areas above; secondary on buyer match."
    )
    return "\n".join(lines)
