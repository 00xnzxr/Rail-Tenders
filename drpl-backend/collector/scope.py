"""
DRPL Collector - the scope profile.

What counts as "a tender for us" is already configured in DRPL, on the admin's
Tender Scope page: ``TenderScopeProfile.keyword_groups``, ``target_ministries``,
``exclusion_terms``, the value range. The collector reads that same profile
rather than keeping a second list, so an admin editing keywords in the UI
changes what the server-side sweep searches for -- with no deploy and no second
place to remember.

It is fetched over HTTP from ``GET /api/extension/config``, the endpoint the
extension already uses, rather than by querying ``tender_scope_profiles``
directly. Reading another service's table is a contract nobody wrote down; the
endpoint is one that exists, is versioned and is already tested.

Every failure here degrades to the configured fallback terms rather than
raising: a sweep that runs "railway" because the config call timed out is worth
far more than a sweep that did not run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from collector.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class ScopeProfile:
    keyword_groups: list[dict] = field(default_factory=list)
    exclusion_terms: list[str] = field(default_factory=list)
    target_ministries: list[str] = field(default_factory=list)
    value_min: Optional[float] = None
    value_max: Optional[float] = None
    relevance_threshold: float = 0.6
    max_pages_per_keyword: int = 5
    historical_fallback: dict = field(default_factory=dict)
    is_active: bool = True

    @classmethod
    def from_config(cls, data: dict) -> "ScopeProfile":
        p = (data or {}).get("scope_profile") or {}
        return cls(
            keyword_groups=list(p.get("keyword_groups") or []),
            exclusion_terms=list(p.get("exclusion_terms") or []),
            target_ministries=list(p.get("target_ministries") or []),
            value_min=p.get("value_min"),
            value_max=p.get("value_max"),
            relevance_threshold=float(p.get("relevance_threshold") or 0.6),
            max_pages_per_keyword=int(p.get("max_pages_per_keyword") or 5),
            historical_fallback=dict(p.get("historical_fallback") or {}),
            is_active=bool(p.get("is_active", True)),
        )

    def search_terms(self, limit: int = 24) -> list[str]:
        """The terms to drive GeM's full-text search with, in priority order.

        Admin-curated keywords first, then the historical fallback DRPL mines
        from past tenders, de-duplicated case-insensitively. Capped because
        each term is its own paged sweep and the run has a 30-minute budget.

        Deliberately NOT the group labels: a label like "AMC / Maintenance
        Services" is a heading for humans, and searching it full-text finds
        nothing. The keywords inside it are the searchable part.
        """
        out: list[str] = []
        seen: set[str] = set()

        def add(term: Optional[str]) -> None:
            t = (term or "").strip()
            if not t or len(t) < 3:
                return
            k = t.lower()
            if k in seen:
                return
            seen.add(k)
            out.append(t)

        for group in self.keyword_groups:
            for kw in (group or {}).get("keywords") or []:
                add(kw)
        for kw in self.historical_fallback.get("keywords") or []:
            add(kw)
        return out[:limit]

    def ministries(self, default: list[str]) -> list[str]:
        return self.target_ministries or default

    def excluded(self, text: str) -> bool:
        """True when a title/description hits an exclusion term.

        Applied as a cheap pre-filter before shipping. It is not the last word
        -- DRPL's own relevance agent scores every ingested row -- but there is
        no reason to POST a batch we already know the platform will discard.
        """
        hay = (text or "").lower()
        if not hay:
            return False
        return any(t.strip().lower() in hay for t in self.exclusion_terms if t and t.strip())


async def fetch_profile(client: Optional[httpx.AsyncClient] = None) -> ScopeProfile:
    """Read the live scope profile. Never raises; returns an empty one on failure.

    Inside drpl-backend the profile is read straight from the database, in
    the same shape ``GET /api/extension/config`` serialises it, so the sweep
    searches for what the admin's Tender Scope page says. The HTTP path below
    remains for a collector running outside the backend.
    """
    try:
        return await asyncio.to_thread(_profile_from_db)
    except Exception as e:  # noqa: BLE001
        logger.info("scope: in-process profile unavailable (%s); trying HTTP.", e)
    s = get_settings()
    if not s.drpl_service_token:
        logger.info("scope: no service token -- using configured fallback terms.")
        return ScopeProfile()

    own = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0))
    try:
        r = await client.get(
            s.config_url,
            headers={"Authorization": f"Bearer {s.drpl_service_token}"},
        )
        if r.status_code != 200:
            logger.warning("scope: /extension/config returned %s -- using fallback.", r.status_code)
            return ScopeProfile()
        return ScopeProfile.from_config(r.json())
    except Exception as e:  # noqa: BLE001 -- a config blip must not stop a sweep
        logger.warning("scope: could not fetch profile (%s) -- using fallback.", e)
        return ScopeProfile()
    finally:
        if own:
            await client.aclose()


def _profile_from_db() -> ScopeProfile:
    from app.core.database import SessionLocal
    from app.services.scope_profile_service import get_active_profile, serialize_for_extension

    db = SessionLocal()
    try:
        profile = get_active_profile(db)
        return ScopeProfile.from_config({"scope_profile": serialize_for_extension(profile, db=db)})
    finally:
        db.close()


def resolve_terms(profile: ScopeProfile, params: dict) -> list[str]:
    """Search terms for this run: explicit request > always + profile > config.

    An explicit ``terms`` list in the run params wins outright, because that is
    a person asking for something specific and they should get exactly it.

    Otherwise the configured "always" terms lead, then the admin's scope
    keywords. That ordering was measured, not guessed: on the first live run
    the scope keywords alone ("bogie", "compressor", "AMC" ...) filtered out
    273 of 281 rows, because a full-text search for a spare-part name mostly
    finds other ministries buying that part. "railway" is the net that
    actually catches railway tenders; the scope keywords then decide which of
    them DRPL cares about, which is what they were written for.
    """
    explicit = [t for t in (params.get("terms") or []) if t and str(t).strip()]
    if explicit:
        return _dedupe([str(t).strip() for t in explicit])

    s = get_settings()
    terms = list(s.always_terms)
    terms.extend(profile.search_terms())
    terms = _dedupe(terms)
    return terms or s.fallback_terms


def _dedupe(terms: list[str]) -> list[str]:
    """Order-preserving, case-insensitive."""
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        k = (t or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(t.strip())
    return out
