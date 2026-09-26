"""
DRPL Collector - IREPS fetcher.

The only genuinely hard part, and the reason it is second rather than first:
it can block on a SIM and a volume, and it must not hold up the portal that
cannot.

VERIFIED AGAINST THE LIVE PORTAL (2026-09-10)
---------------------------------------------
``GET /epsn/anonymSearch.do?searchParam=showPageClosed&language=en`` returns
200 with ``ADM.004`` and "Authenticate Yourself" in the body -- i.e. everything
useful sits behind ``epsn/guestLogin.do`` asking for a mobile number and an
OTP. ``/epsn/advancedSearch.do`` answers the same way. So the design is one
warm session that almost never needs to log in again:

  * a persistent Chromium profile on a Railway volume (session survives deploys)
  * a login attempt only when a cheap check says the session is gone
  * ``ADM.004`` as that check -- a substring test, not a guess
  * the OTP delivered by webhook to a blocked future, never typed by a person

Extraction reads tables by HEADER TEXT (``parsing/tables.py``), never by class
name. That is the direct replacement for ``selector_configs/ireps.json``, whose
every selector is a positional bet on markup IREPS never promised to keep --
and which has been stale since March.

STATUS: the session, login and paging machinery is complete and unit-tested
against saved fixtures. It has NOT been run against a live logged-in IREPS
session, because that needs the registered mobile number and the forwarding
handset (IREPS_MOBILE + the /internal/otp webhook). Until those exist, a run
that includes "ireps" raises IrepsNotConfigured with that message rather than
half-working. See INTEGRATION.md, "Bringing IREPS up".
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional
from urllib.parse import urljoin

from collector.config import get_settings
from collector.parsing import tables
from collector.portals.base import (
    Batch,
    ParserDrift,
    PortalUnavailable,
    SessionExpired,
)
from collector.session import otp as otp_mod

logger = logging.getLogger(__name__)

PORTAL = "ireps"
FETCHER_VERSION = "ireps-search-v1"

#: IREPS answers an expired session with this error code. A substring test,
#: not a guess -- confirmed on the live portal.
_EXPIRED_MARKERS = ("ADM.004", "Authenticate Yourself", "guestLogin.do")

#: Where a search lands. Kept as data so a path change is a one-line edit.
SEARCH_PATH = "/epsn/anonymSearch.do?searchParam=showPageClosed&language=en"
ADVANCED_SEARCH_PATH = "/epsn/advancedSearch.do"
LOGIN_PATH = "/epsn/guestLogin.do"

_DATE_RE = re.compile(r"(\d{2})[/-](\d{2})[/-](\d{4})")


class IrepsNotConfigured(Exception):
    """IREPS needs a registered mobile and an OTP webhook before it can run."""


def session_expired(html: str) -> bool:
    """Cheap check: is the warm session gone?"""
    return any(marker in (html or "") for marker in _EXPIRED_MARKERS)


def _parse_date(raw: str) -> Optional[str]:
    """IREPS renders dd/mm/yyyy. Return ISO, or None when it is not a date.

    Returning None rather than guessing matters: drpl-backend's ``_parse_date``
    would otherwise read 03/04/2026 as March 4th, and a closing date off by
    nine months is worse than a closing date that is missing.
    """
    m = _DATE_RE.search(raw or "")
    if not m:
        return None
    dd, mm, yyyy = m.groups()
    try:
        return datetime(int(yyyy), int(mm), int(dd), tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None


def _parse_value(raw: str) -> Optional[float]:
    """Rupee value from a cell, or None.

    None is a routing decision, not a rejection: a missing value routes to
    "fetch the detail page and find out" downstream. It must never route to
    "out of scope" -- the old extension's scope gate treated an unparseable
    value as a failed check, which quietly excluded exactly the tenders that
    most needed a detail fetch.
    """
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    if not cleaned or cleaned == ".":
        return None
    try:
        v = float(cleaned)
    except ValueError:
        return None
    return v if v > 0 else None


def rows_to_tenders(html: str, base_url: str, term: Optional[str] = None) -> list[dict]:
    """Parse one results page into TenderInput dicts. Pure -- fixture-testable."""
    table, mapping = tables.find_results_table(html, portal=PORTAL)
    out: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for row in tables.data_rows(table):
        values = tables.row_values(row, mapping)
        tid = (values.get("tenderId") or "").strip()
        if len(tid) < 3:
            continue
        # Header text that slipped through as a data row.
        if re.match(r"^(tender|search|dept|please|select|status|all\s)", tid, re.I):
            continue

        links = tables.row_links(row, base_url)
        title = (values.get("title") or "").strip()
        out.append(
            {
                "portal": PORTAL,
                "tenderId": tid,
                "title": title or f"{PORTAL} #{tid}",
                "organisation": (values.get("organisation") or "").strip(),
                "department": (values.get("department") or "").strip(),
                "description": title,
                "estimatedValue": _parse_value(values.get("estimatedValue", "")),
                "emdAmount": _parse_value(values.get("emdAmount", "")),
                "openingDate": _parse_date(values.get("openingDate", "")),
                "closingDate": _parse_date(values.get("closingDate", "")),
                "status": (values.get("status") or "open").strip().lower() or "open",
                "sourceUrl": urljoin(base_url, SEARCH_PATH),
                "sourcePortal": FETCHER_VERSION,
                "currency": "INR",
                "documentLinks": links,
                "nitDocumentLinks": [u for u in links if re.search(r"nit", u, re.I)],
                "searchMatchKeyword": term,
                "extractedAt": now,
            }
        )
    return out


# -- The warm session ----------------------------------------------------


class IrepsSession:
    """One Crawl4AI managed browser on a persistent profile.

    Login happens from the ``on_page_context_created`` hook, and only when the
    cheap ADM.004 check says the session is gone -- so the ordinary case is
    zero OTPs and the extraordinary case is one.
    """

    def __init__(self, headless: Optional[bool] = None) -> None:
        s = get_settings()
        self.headless = s.ireps_headless if headless is None else headless
        self.base_url = s.ireps_base_url.rstrip("/")
        self._crawler = None
        self._profile_path = None
        self._logged_in = False

    async def __aenter__(self) -> "IrepsSession":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def start(self) -> None:
        from crawl4ai import AsyncWebCrawler

        from collector.session import profiles

        self._profile_path = profiles.profile_dir(PORTAL)
        # Fails loudly if a second replica is already holding this profile,
        # rather than corrupting it and burning an OTP to find out.
        profiles.acquire_profile_lock(self._profile_path)

        cfg = profiles.browser_config(PORTAL, headless=self.headless)
        self._crawler = AsyncWebCrawler(config=cfg)
        await self._crawler.__aenter__()
        self._crawler.crawler_strategy.set_hook(
            "on_page_context_created", self._ensure_session_hook
        )

    async def aclose(self) -> None:
        from collector.session import profiles

        if self._crawler is not None:
            try:
                await self._crawler.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.debug("ireps: crawler close failed: %s", e)
            self._crawler = None
        if self._profile_path is not None:
            profiles.release_profile_lock(self._profile_path)
            self._profile_path = None

    async def _ensure_session_hook(self, page, context=None, **_):
        """Log in only when a cheap check says we have to.

        Crawl4AI calls this as ``hook(page, context=..., config=...)``, so
        ``page`` is positional and everything else is a keyword.
        """
        try:
            await page.goto(
                self.base_url + SEARCH_PATH, wait_until="domcontentloaded", timeout=45000
            )
            html = await page.content()
        except Exception as e:  # noqa: BLE001
            logger.warning("ireps: session probe failed: %s", e)
            return
        if session_expired(html):
            logger.info("ireps: session expired (ADM.004) -- logging in")
            await self.guest_login(page)
        else:
            self._logged_in = True

    async def guest_login(self, page) -> None:
        """Mobile number, then an OTP delivered by webhook. Never typed by a person."""
        s = get_settings()
        if not s.ireps_mobile:
            raise IrepsNotConfigured(
                "IREPS_MOBILE is not set. IREPS requires a registered mobile "
                "number and a running /internal/otp webhook before it can be swept."
            )

        await page.goto(self.base_url + LOGIN_PATH, wait_until="domcontentloaded", timeout=45000)

        html = await page.content()
        if re.search(r"captcha|verify you are|are you (a )?human", html, re.I):
            # A CAPTCHA exists to keep automation out. Pause and ask; never solve.
            raise otp_mod.CaptchaEncountered(
                "IREPS is asking for human verification. Solve it in a headful "
                "session, then re-run -- the profile keeps the result."
            )

        await page.fill("input[name='mobileNo']", s.ireps_mobile)
        await page.click("input[name='imageField']")  # Generate OTP

        code = await otp_mod.await_code(PORTAL, timeout=s.otp_wait_seconds)

        await page.fill("input[name='otp']", code)
        await page.click("input[name='imageField']")
        await page.wait_for_load_state("domcontentloaded", timeout=45000)

        if session_expired(await page.content()):
            raise SessionExpired("ireps: login did not take -- still seeing ADM.004")
        self._logged_in = True
        logger.info("ireps: logged in; session persisted to the profile volume")

    async def fetch(self, url: str, js: Optional[str] = None) -> str:
        """Fetch one page through the warm session, returning its HTML."""
        from crawl4ai import CacheMode, CrawlerRunConfig

        if self._crawler is None:
            raise RuntimeError("IrepsSession.start() was not called")

        cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            page_timeout=60000,
            js_code=js or None,
            # A results table is server-rendered; there is nothing to scroll for.
            scan_full_page=False,
        )
        result = await self._crawler.arun(url, config=cfg)
        html = getattr(result, "html", "") or ""
        if not html:
            raise PortalUnavailable(f"ireps: empty response from {url}")
        if session_expired(html):
            raise SessionExpired(f"ireps: session gone while fetching {url}")
        return html

    async def heartbeat(self) -> bool:
        """Cheap liveness poke, so expiry is found here and not mid-sweep."""
        try:
            html = await self.fetch(self.base_url + SEARCH_PATH)
            return not session_expired(html)
        except SessionExpired:
            return False
        except Exception as e:  # noqa: BLE001
            logger.debug("ireps: heartbeat failed: %s", e)
            return False


# -- The fetcher ---------------------------------------------------------


class IrepsFetcher:
    portal = PORTAL
    version = FETCHER_VERSION

    fill_baseline = {
        "tenderId": 1.0,
        "title": 0.9,
        "closingDate": 0.9,
        "organisation": 0.7,
    }

    def __init__(self, session: Optional[IrepsSession] = None) -> None:
        self._session = session
        self._owns_session = session is None

    async def aclose(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.aclose()
            self._session = None

    def _search_url(self, page: int) -> str:
        s = get_settings()
        base = s.ireps_base_url.rstrip("/") + SEARCH_PATH
        return base if page <= 1 else f"{base}&pageNo={page}"

    async def sweep(
        self, known: set[str], params: dict[str, Any]
    ) -> AsyncIterator[Batch]:
        s = get_settings()
        if not s.ireps_mobile:
            raise IrepsNotConfigured(
                "IREPS is not configured. Set IREPS_MOBILE and run the OTP "
                "webhook (api.py) before including 'ireps' in a collect run. "
                "GeM sweeps are unaffected."
            )

        max_pages = int(params.get("max_pages") or s.ireps_max_pages)
        mode = (params.get("mode") or "incremental").lower()
        stop_after_known = 0 if mode == "full" else 30
        term = None

        if self._session is None:
            self._session = IrepsSession()
            await self._session.start()

        consecutive_known = 0
        pages_done = 0

        for page in range(1, max_pages + 1):
            url = self._search_url(page)
            try:
                html = await self._session.fetch(url)
            except SessionExpired:
                # The heartbeat should normally have caught this. Re-enter the
                # session once -- the hook logs back in -- then give up rather
                # than looping a login that is clearly not sticking.
                logger.info("ireps: re-establishing session mid-sweep")
                await self._session.aclose()
                self._session = IrepsSession()
                await self._session.start()
                html = await self._session.fetch(url)

            try:
                tenders = rows_to_tenders(html, s.ireps_base_url, term=term)
            except ParserDrift:
                if page == 1:
                    raise
                # A later page with no table is the end of the results, not a
                # parser failure.
                break

            if not tenders:
                break

            batch = Batch(
                portal=PORTAL,
                page=page,
                pages_done=pages_done + 1,
                rows_seen=len(tenders),
                term=term,
                fetcher=FETCHER_VERSION,
                tenders=tenders,
            )
            for t in tenders:
                if t["tenderId"] in known:
                    consecutive_known += 1
                else:
                    consecutive_known = 0

            pages_done += 1
            yield batch

            if stop_after_known and consecutive_known >= stop_after_known:
                logger.info("ireps: caught up after %s known ids", consecutive_known)
                break

            await asyncio.sleep(s.request_delay_seconds)
