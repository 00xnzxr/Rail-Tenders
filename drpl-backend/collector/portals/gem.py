"""
DRPL Collector - GeM fetcher.

Start here. No credentials, no browser in the hot path, no blockers -- real
railway tenders can be in DRPL today.

WHAT WAS VERIFIED AGAINST THE LIVE ENDPOINT (2026-09-10)
--------------------------------------------------------
1. ``GET https://bidplus.gem.gov.in/all-bids`` returns 200 to a plain httpx
   client with an ordinary User-Agent. It sets ``csrf_gem_cookie``, the F5 WAF
   cookies (``TS0174a79d``), ``ci_session`` and ``GeM``.

2. The CSRF token the POST wants IS the ``csrf_gem_cookie`` value. It also
   appears inline in the page's jQuery as ``csrf_bd_gem_nk': '<32 hex>'`` --
   note the JS-object form, NOT the ``value="..."`` form. Reading the cookie is
   both simpler and the primary source; the HTML scrape is the fallback for the
   day GeM stops echoing it into the cookie.

3. ``POST /all-bids-data`` with ``payload=<json>&csrf_bd_gem_nk=<token>`` and
   ``X-Requested-With: XMLHttpRequest`` returns
   ``{"response": {"response": {"numFound": N, "docs": [...]}}}``. Ten docs per
   page, server-fixed. numFound came back 47,517 for an unfiltered
   ``ongoing_bids`` query.

4. ``/all-bids-data`` has NO ministry filter in its request payload -- the only
   filters are searchBid/searchType, bidStatusType, byEndDate, byType,
   byStatus, highBidValue and sort. So on THIS endpoint the ministry match
   happens client-side on the STRUCTURED ``ba_official_details_minName``
   field, and full-text search is what keeps the page count sane:

       search=""                -> 47,516 results  (4,700 pages: not viable)
       search="railway"         ->  1,769 results
       search="bogie"           ->     18 results
       search="traction motor"  ->     82 results

   Hence: one search pass per scope-profile keyword, ministry-filtered.

5. A DIFFERENT endpoint does filter by ministry, and point 4 above was read for
   a while as "GeM has no ministry filter", which is not the same claim.
   ``POST /search-bids`` -- what the portal's own Advanced Search page calls --
   takes ``searchType: "ministry-search"`` and a ministry name, answers an
   anonymous request, and returns the same Solr envelope. Measured 2026-09-22:
   "Ministry of Railways" gives numFound 1,703 against 43,770 live bids, which
   is 171 pages instead of 4,378 for the identical set of tenders.

   It is not a drop-in replacement for the walk, for one measured reason: it
   ignores the sort key and pages unstably, so a single pass returned 1,426 of
   1,703. Repeated passes converge on GeM's own count and then sit on it. See
   ``build_ministry_payload`` here and ``mode="ministry"`` in gem_full.

NO BROWSER IS USED HERE. The build sheet's bootstrap-via-managed-browser is
kept as ``bootstrap_via_browser`` below for the day the WAF starts demanding a
real Chrome handshake, but the steady-state path is httpx and should stay that
way -- it is faster, it is simpler, and it cannot leak a Chromium process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

import httpx

from collector import netconfig
from collector.config import get_settings
from collector.portals.base import Batch, ParserDrift, PortalUnavailable

logger = logging.getLogger(__name__)

PORTAL = "gem"
#: Bump this when the mapping changes. It lands on every row as
#: ``sourcePortal`` so the ledger can attribute a miss to a specific fetcher.
FETCHER_VERSION = "gem-allbids-v1"

#: The token as it appears inline in the /all-bids jQuery. Fallback only --
#: the cookie is the primary source. Deliberately NOT the build sheet's
#: ``value="..."`` pattern, which does not match the live page.
_TOKEN_RE = re.compile(r"""csrf_bd_gem_nk['"]?\s*[:=]\s*['"]([0-9a-f]{32})['"]""")

#: GEM/<year>/B/<n> is a bid; GEM/<year>/R/<n> is a reverse auction. Different
#: objects with different deadlines -- both are kept, tagged, rather than
#: silently mixed.
_BID_NO_RE = re.compile(r"^GEM/(\d{4})/([BR])/(\d+)$", re.IGNORECASE)


# -- Bootstrap -----------------------------------------------------------


class GemSession:
    """A warm httpx client plus the CSRF token that goes with its cookies.

    The token is bound to the session cookie, so the two travel together and
    are refreshed together on a 403.
    """

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self.client = client
        self.token = token

    async def aclose(self) -> None:
        await self.client.aclose()


#: Bootstrap attempts before giving up. Observed live: /all-bids intermittently
#: answers 500 or 502 -- an F5 WAF in front of a busy origin does that, and it
#: clears on its own within seconds. Failing the whole sweep on the first blip
#: would mean a Search button that fails for reasons the portal has already
#: forgotten about.
_BOOTSTRAP_ATTEMPTS = 4


async def bootstrap(client: Optional[httpx.AsyncClient] = None) -> GemSession:
    """Load /all-bids once, harvest the cookies and the CSRF token.

    Retries a transient refusal with backoff, then raises PortalUnavailable so
    the caller sees the one exception type it knows how to report. Re-run only
    on a 403 -- one bootstrap serves a whole sweep.
    """
    s = get_settings()
    own = client is None
    if client is None:
        client = netconfig.make_client(
            timeout=httpx.Timeout(s.sink_timeout_seconds, connect=20.0),
            headers={"User-Agent": s.user_agent},
        )

    r = None
    last_error = "unknown"
    connect_failed = False
    for attempt in range(1, _BOOTSTRAP_ATTEMPTS + 1):
        try:
            r = await client.get(f"{s.gem_base_url}/all-bids")
            if r.status_code == 200:
                break
            last_error = f"HTTP {r.status_code}"
            # 4xx other than 429 is a refusal that will not change on retry.
            if r.status_code < 500 and r.status_code != 429:
                break
        except httpx.HTTPError as e:
            last_error = f"{type(e).__name__}: {e}"
            connect_failed = isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))

        if attempt < _BOOTSTRAP_ATTEMPTS:
            logger.info(
                "gem: bootstrap attempt %s/%s failed (%s) -- backing off",
                attempt, _BOOTSTRAP_ATTEMPTS, last_error,
            )
            await asyncio.sleep(s.request_delay_seconds * attempt)

    if r is None or r.status_code != 200:
        if own:
            await client.aclose()
        msg = (f"gem: /all-bids did not answer after {_BOOTSTRAP_ATTEMPTS} attempts "
               f"-- {last_error}")
        if connect_failed:
            # httpx says "all connection attempts failed" for a refused, a
            # dropped SYN and an unreachable alike. Say which, per address,
            # and what an admin does about it.
            host = httpx.URL(s.gem_base_url).host
            try:
                detail = await asyncio.to_thread(netconfig.diagnose_connect, host)
            except Exception as e:  # noqa: BLE001
                detail = f"diagnosis failed: {e}"
            logger.error("gem: connect diagnosis for %s -> %s", host, detail)
            via = netconfig.gem_proxy_url()
            if via:
                msg = (f"{msg}. The configured GeM proxy did not connect "
                       f"({netconfig.redact(via)}): check its URL and credentials. "
                       f"[direct: {detail}]")
            else:
                msg = f"{msg}. {netconfig.PROXY_HINT} [{detail}]"
        raise PortalUnavailable(msg)

    # Primary: the cookie. Verified equal to the inline token on the live page.
    token = client.cookies.get("csrf_gem_cookie") or ""
    if not token:
        # Fallback: scrape it out of the page's jQuery call.
        m = _TOKEN_RE.search(r.text)
        token = m.group(1) if m else ""
    if not token:
        if own:
            await client.aclose()
        raise ParserDrift(
            "gem: no csrf token in the csrf_gem_cookie cookie or the /all-bids HTML"
        )
    return GemSession(client, token)


async def bootstrap_via_browser() -> tuple[dict, str]:
    """Cookie-jar bootstrap through a real Chromium, via Crawl4AI.

    NOT the steady-state path -- ``bootstrap()`` above is, and it needs no
    browser. This exists for the day GeM's F5 WAF starts requiring a genuine
    TLS/JS handshake before it hands out usable cookies. Imports Crawl4AI
    lazily so the GeM path never pays for Playwright.

    Returns ``(cookies, csrf_token)`` for handing to httpx.
    """
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    s = get_settings()
    jar: dict[str, str] = {}

    async def grab_cookies(page, context=None, **_):
        # Take the cookies off the Playwright context inside the hook rather
        # than off the crawl result: the hook signature is stable
        # (execute_hook passes page positionally, context by keyword) and it
        # gives the real browser API.
        await page.goto(f"{s.gem_base_url}/all-bids", wait_until="domcontentloaded")
        if context is not None:
            for c in await context.cookies():
                jar[c["name"]] = c["value"]

    cfg = BrowserConfig(headless=True, browser_type="chromium", user_agent=s.user_agent)
    async with AsyncWebCrawler(config=cfg) as crawler:
        crawler.crawler_strategy.set_hook("on_page_context_created", grab_cookies)
        res = await crawler.arun(
            f"{s.gem_base_url}/all-bids",
            config=CrawlerRunConfig(cache_mode=CacheMode.BYPASS),
        )

    token = jar.get("csrf_gem_cookie") or ""
    if not token:
        m = _TOKEN_RE.search(getattr(res, "html", "") or "")
        token = m.group(1) if m else ""
    if not token:
        raise ParserDrift("gem: csrf token not found via browser bootstrap")
    return jar, token


# -- One page ------------------------------------------------------------


def build_payload(
    page: int,
    search: str = "",
    sort: str = "Bid-Start-Date-Latest",
    bid_status_type: str = "ongoing_bids",
) -> dict:
    """The exact body /all-bids-data accepts. Verified live."""
    return {
        "page": page,
        "param": {"searchBid": search, "searchType": "fullText"},
        "filter": {
            "bidStatusType": bid_status_type,
            "byEndDate": {"from": "", "to": ""},
            "byType": "all",
            "highBidValue": "",
            "sort": sort,
        },
    }


#: Where GeM's own Advanced Search sends a ministry query, and the page it
#: sends it from. A different endpoint from /all-bids-data, with a different
#: body shape -- see build_ministry_payload.
MINISTRY_PATH = "/search-bids"
MINISTRY_REFERER = "/advance-search"


def build_ministry_payload(
    page: int,
    ministry: str,
    *,
    buyer_state: str = "",
    organization: str = "",
    department: str = "",
    end_from: str = "",
    end_to: str = "",
) -> dict:
    """The exact body ``/search-bids`` accepts. Verified live 2026-09-22.

    GeM's /all-bids-data really does have no ministry filter -- but its
    Advanced Search page does, and it answers an anonymous request. Asking for
    "Ministry of Railways" returns numFound 1703 against 43,770 live bids: the
    same 1703 a 4,378-page walk of the whole list arrives at, for 171 pages of
    traffic.

    The portal's form marks Ministry, State and both dates required, but that
    is browser-side only -- its own validator accepts a ministry with
    everything else blank (``if(!ministry && !buyerState)`` is the real rule),
    which is what this sends.

    The value is the ministry name exactly as ``ba_official_details_minName``
    spells it, so the filter and the scope check agree by construction.
    """
    return {
        "searchType": "ministry-search",
        "ministry": ministry,
        "buyerState": buyer_state,
        "organization": organization,
        "department": department,
        "bidEndFromMin": end_from,
        "bidEndToMin": end_to,
        "page": page,
    }


#: Attempts for a single data request. Observed live on 2026-09-10: GeM
#: intermittently answers 500 on an otherwise-identical request and succeeds on
#: the next one seconds later -- an F5 WAF in front of a busy origin does that.
#: The list endpoint tolerated eight back-to-back requests without complaint, so
#: this is transient upstream failure rather than throttling of us.
_REQUEST_ATTEMPTS = 4


class _Retryable(Exception):
    """Internal: this attempt failed in a way another attempt may fix."""


async def _post_json(
    session: GemSession,
    path: str,
    data: dict,
    *,
    what: str,
    referer: str = "/all-bids",
) -> dict:
    """POST a form and return parsed JSON, retrying a transient refusal.

    Every GeM data call goes through here. Three things it handles that a bare
    ``raise_for_status`` did not, and each was observed live:

      * a 5xx that clears on its own within seconds -- retried with backoff
      * a 200 whose body is not JSON. GeM serves JSON as
        ``Content-Type: text/html``, so a WAF interstitial arrives as a
        perfectly ordinary 200 and only fails when you try to parse it.
      * 403/419, meaning the CSRF token or WAF cookie went stale. Raised as
        PermissionError so the caller re-bootstraps rather than retrying a
        credential that will keep being rejected.
    """
    s = get_settings()
    last_error = "unknown"

    for attempt in range(1, _REQUEST_ATTEMPTS + 1):
        try:
            r = await session.client.post(
                f"{s.gem_base_url}{path}",
                data=data,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{s.gem_base_url}{referer}",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
            )

            if r.status_code in (403, 419):
                raise PermissionError("gem: csrf/session rejected")
            if r.status_code == 429 or r.status_code >= 500:
                raise _Retryable(f"HTTP {r.status_code}")
            if r.status_code != 200:
                raise PortalUnavailable(f"gem: {what} returned HTTP {r.status_code}")

            try:
                return r.json()
            except ValueError as e:
                # A 200 that is not JSON is the WAF, not the application.
                raise _Retryable(f"non-JSON 200 ({e})") from e

        except (_Retryable, httpx.HTTPError) as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt >= _REQUEST_ATTEMPTS:
                break
            # Exponential-ish, and never faster than the configured politeness
            # delay -- a retry storm is how a transient 500 becomes a ban.
            delay = max(s.request_delay_seconds, 1.0) * attempt
            logger.info(
                "gem: %s attempt %s/%s failed (%s) -- retrying in %.1fs",
                what, attempt, _REQUEST_ATTEMPTS, last_error, delay,
            )
            await asyncio.sleep(delay)

    raise PortalUnavailable(
        f"gem: {what} failed after {_REQUEST_ATTEMPTS} attempts -- {last_error}"
    )


async def fetch_page(
    session: GemSession,
    page: int,
    search: str = "",
    sort: str = "Bid-Start-Date-Latest",
    ministry: Optional[str] = None,
) -> tuple[int, list[dict]]:
    """One page. Returns ``(numFound, docs)``.

    ``ministry`` switches to GeM's Advanced Search endpoint, which filters on
    the buyer's ministry server-side. Both endpoints answer in the same Solr
    envelope, so everything downstream of here is identical -- which is the
    reason it is a parameter and not a second fetcher.
    """
    if ministry:
        payload = build_ministry_payload(page, ministry)
        path, referer = MINISTRY_PATH, MINISTRY_REFERER
        what = f"page {page} of ministry {ministry!r}"
    else:
        payload = build_payload(page, search=search, sort=sort)
        path, referer = "/all-bids-data", "/all-bids"
        what = f"page {page}" + (f" of {search!r}" if search else "")
    body = await _post_json(
        session,
        path,
        {"payload": json.dumps(payload), "csrf_bd_gem_nk": session.token},
        what=what,
        referer=referer,
    )
    try:
        inner = body["response"]["response"]
        return int(inner["numFound"]), list(inner["docs"])
    except (KeyError, ValueError, TypeError) as e:
        raise ParserDrift(f"gem: unexpected /all-bids-data shape: {e}") from e


# -- Scope + mapping -----------------------------------------------------


def _first(doc: dict, key: str):
    """GeM returns Solr-style single-element lists for almost every field."""
    v = doc.get(key)
    if isinstance(v, list):
        return v[0] if v else None
    return v


#: IST is UTC+5:30, no DST, ever. Same constant gemdoc's ``as_datetime`` uses.
_IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_instant(value):
    """GeM's listing wall-clock, returned as a true UTC instant.

    ``final_start_date_sort`` and ``final_end_date_sort`` arrive with a "Z"
    they have not earned. Measured against the portal's own UI on 2026-09-22:
    the bid GeM displays as closing "19-09-2026 10:00 PM" comes back from
    ``/all-bids-data`` as ``2026-09-19T22:00:00Z`` -- the *same wall clock*, so
    the numbers are IST and the Z is decoration. Sampling 40 live bids, the
    listing's closing time ran ahead of the same bid's PDF (which gemdoc
    already converts correctly) by exactly 5h30m on 35, and by 5h30m plus whole
    days on the other 5 -- those being bids a corrigendum had since extended,
    where the listing is right and the document is stale.

    So the wall clock is correct and the label is a lie. Read literally it puts
    every deadline five and a half hours late, which on a tender system is the
    one field that must not be wrong.

    Two deliberate choices:

    * An explicit non-zero offset is trusted as written. Only "Z", "+00:00" and
      a bare naive timestamp are read as IST wall-clock -- so if GeM ever
      starts stating the real offset, this stops rewriting it.
    * Anything unparseable is returned **unchanged**, not dropped. An
      unrecognised shape means GeM moved, and silently losing every date is a
      worse failure than the one being fixed here; the warning is how that
      surfaces.

    Apply this EXACTLY ONCE, to a raw listing field. It is not idempotent and
    cannot be: a genuine "+00:00" and GeM's counterfeit one are byte-identical,
    so a second application shifts by another 5h30m. ``to_tender`` is the only
    caller. ``test_ist_instant_is_deliberately_not_idempotent`` pins this.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    raw = value.strip()
    # Only the trailing zone designator, never a bare replace: that would
    # rewrite any other Z in the string and hand fromisoformat something worse
    # than what it was given.
    iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        logger.warning(
            "gem: listing timestamp %r is not ISO-8601 -- passed through "
            "unconverted; the portal may have changed shape", raw,
        )
        return value
    if dt.tzinfo is None or dt.utcoffset() == timedelta(0):
        # Naive, or that unearned Z: these digits are Indian Standard Time.
        dt = dt.replace(tzinfo=timezone.utc) - _IST_OFFSET
    return dt.astimezone(timezone.utc).isoformat()


def ministry_of(doc: dict) -> str:
    return (_first(doc, "ba_official_details_minName") or "").strip()


def in_scope(doc: dict, ministries: list[str]) -> bool:
    """Match the STRUCTURED ministry field exactly.

    This is the whole fix for the keyword problem: a full-text search for
    "railway" also returns Ministry of Chemicals and Fertilizers rows that
    happen to mention a railway siding. The ministry field does not guess.

    An empty ``ministries`` list means "keep everything", which is what a
    deliberate unfiltered audit sweep wants.
    """
    if not ministries:
        return True
    m = ministry_of(doc).lower()
    if not m:
        return False
    return any(m == want.strip().lower() for want in ministries)


def kind(bid_no: str) -> str:
    """bid | ra | other. Kept as a tag, never used to drop a row."""
    m = _BID_NO_RE.match((bid_no or "").strip())
    if not m:
        return "other"
    return "bid" if m.group(2).upper() == "B" else "ra"


def sequence_parts(bid_no: str) -> tuple[Optional[str], Optional[int]]:
    """('GEM/2026/B', 7916525) -- the prefix and number gap detection needs."""
    m = _BID_NO_RE.match((bid_no or "").strip())
    if not m:
        return None, None
    year, letter, num = m.group(1), m.group(2).upper(), m.group(3)
    try:
        return f"GEM/{year}/{letter}", int(num)
    except ValueError:
        return None, None


# -- Document URLs -------------------------------------------------------
#
# Read straight off GeM's own card renderer on /all-bids, which picks the
# endpoint from the bid type:
#
#     b_bid_type == 5              -> showdirectradocumentPdf
#     b_bid_type == 2, eval == 0   -> showradocumentPdf
#     b_bid_type == 2, eval  > 0   -> list-ra-schedules   (a page, not a PDF)
#     anything else                -> showbidDocument
#
# Both PDF forms were fetched live on 2026-09-10 and returned real
# application/pdf bodies. Sending every bid to showbidDocument -- which is what
# a naive mapper does -- gives reverse auctions a URL that is not their
# document.

BID_TYPE_DIRECT_RA = 5
BID_TYPE_RA = 2


def document_path(doc: dict) -> str:
    """The portal path holding this bid's own document."""
    bid_id = _first(doc, "b_id")
    bid_type = _as_int(_first(doc, "b_bid_type"))
    eval_type = _as_int(_first(doc, "b_eval_type"))
    if bid_id is None:
        return "/all-bids"
    if bid_type == BID_TYPE_DIRECT_RA:
        return f"/showdirectradocumentPdf/{bid_id}"
    if bid_type == BID_TYPE_RA:
        if eval_type and eval_type > 0:
            return f"/list-ra-schedules/{bid_id}"
        return f"/showradocumentPdf/{bid_id}"
    return f"/showbidDocument/{bid_id}"


def document_url(doc: dict) -> str:
    return get_settings().gem_base_url + document_path(doc)


def parent_bid_url(doc: dict) -> Optional[str]:
    """An RA derived from a bid links back to that bid's document."""
    parent_id = _first(doc, "b_id_parent")
    if not parent_id:
        return None
    return f"{get_settings().gem_base_url}/showbidDocument/{parent_id}"


def _as_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _truthy(doc: dict, key: str) -> bool:
    v = _first(doc, key)
    return bool(v) and str(v) not in ("0", "False", "false")


def bid_type_label(doc: dict) -> str:
    """A human label for the card: what kind of bid this is.

    Mirrors what GeM prints on its own card -- "BID" / "RA", with the Rate
    Contract and Global Tender qualifiers it appends -- rather than the bare
    b/ra code, because this lands in ``Tender.bid_type``, which the DRPL tender
    card renders verbatim next to NCB / GCB / Limited from other portals.
    """
    bid_type = _as_int(_first(doc, "b_bid_type"))
    label = "RA" if bid_type in (BID_TYPE_RA, BID_TYPE_DIRECT_RA) else "Bid"
    if _truthy(doc, "is_rc_bid"):
        label += " (Rate Contract)"
    elif _truthy(doc, "ba_is_global_tendering"):
        label += " (Global Tender)"
    return label


def build_description(doc: dict) -> str:
    """A readable summary of everything the listing states.

    The listing carries facts that have no column of their own on the DRPL
    tender -- quantity, the BOQ title, the packet mode, whether it is a
    high-value bid. Dropping them loses information the portal gave us for
    free; folding them into the description keeps them searchable and puts
    them in front of whoever opens the tender.
    """
    parts: list[str] = []
    title = _first(doc, "bd_category_name") or _first(doc, "b_category_name")
    if title:
        parts.append(str(title).strip())

    boq = _first(doc, "bbt_title")
    if boq:
        parts.append(f"BOQ: {boq}")

    qty = _first(doc, "b_total_quantity")
    if qty not in (None, ""):
        parts.append(f"Quantity: {qty}")

    parts.append(f"Type: {bid_type_label(doc)}")

    if _truthy(doc, "ba_is_single_packet"):
        parts.append("Single packet")
    if _truthy(doc, "is_high_value"):
        parts.append("High value bid")

    parent = _first(doc, "b_bid_number_parent")
    if parent:
        parts.append(f"Derived from bid {parent}")

    dept = _first(doc, "ba_official_details_deptName")
    ministry = ministry_of(doc)
    buyer = " / ".join(p for p in (ministry, dept) if p)
    if buyer:
        parts.append(f"Buyer: {buyer}")

    return " | ".join(parts)


def to_tender(doc: dict, term: Optional[str] = None) -> Optional[dict]:
    """Map one GeM doc to drpl-backend's TenderInput shape.

    camelCase because that is what ``app/schemas/__init__.py::TenderInput``
    declares and what ``POST /api/extension/tenders`` validates against.
    Unknown keys are rejected by nothing -- pydantic ignores them -- but every
    key here is a real field on that model.
    """
    bid_no = _first(doc, "b_bid_number")
    if not bid_no:
        return None
    bid_no = str(bid_no).strip()

    # bd_category_name is the full value; b_category_name is GeM's own
    # 100-char truncation of it. Prefer the full one.
    title = _first(doc, "bd_category_name") or _first(doc, "b_category_name") or ""

    doc_url = document_url(doc)
    # On GeM the bid document IS the NIT: one PDF carrying the scope, the
    # eligibility conditions, the EMD and the schedule. Putting it in
    # nitDocumentLinks as well as documentLinks is what makes drpl-backend's
    # existing `nit_link_fetch_service` download it and the tender analyzer
    # read it -- which is where estimatedValue and EMD actually come from.
    links = [doc_url]
    parent = parent_bid_url(doc)
    if parent and parent != doc_url:
        links.append(parent)

    return {
        "portal": PORTAL,                                    # dedupe key, part 1
        "tenderId": bid_no,                                  # dedupe key, part 2
        "title": str(title).strip(),
        "organisation": ministry_of(doc),
        "department": (_first(doc, "ba_official_details_deptName") or "").strip(),
        "description": build_description(doc),
        # Converted, never passed through: see ist_instant. The portal states
        # these in IST under a UTC label.
        "openingDate": ist_instant(_first(doc, "final_start_date_sort")),
        "closingDate": ist_instant(_first(doc, "final_end_date_sort")),
        "sourceUrl": doc_url,
        "detailUrl": doc_url,
        "documentLinks": links,
        "nitDocumentLinks": [doc_url],
        # WHICH fetcher produced this row, not just which portal. The audit
        # needs it; drpl-backend stores it on Tender.source_portal.
        "sourcePortal": FETCHER_VERSION,
        "status": "open",
        "currency": "INR",
        "bidType": bid_type_label(doc),
        "category": (_first(doc, "bbt_title") or _first(doc, "b_cat_id") or None),
        "searchMatchKeyword": term,
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        # isDetailExtracted stays False on purpose: this is listing data plus a
        # document link, not a parsed detail page. Claiming otherwise would
        # stop drpl-backend's `_update_existing_tender` from ever enriching the
        # row when the real detail arrives.
        #
        # estimatedValue and EMD are NOT on the listing -- they are inside that
        # PDF. Left unset so a missing field routes to "fetch the document and
        # find out" downstream. It must never route to "out of scope": the old
        # extension's scope gate treated an unparseable value as a failed
        # check, which quietly excluded exactly the tenders that most needed a
        # detail fetch. Enrichment is a downstream stage, never a gate.
    }


# -- Corrigenda ----------------------------------------------------------
#
# GeM's card has a "View Corrigendum/Representation" control that POSTs to
# /public-bid-other-details/{b_id} and gets back
# {"corrigendum": bool, "representation": bool}. A corrigendum changes the
# scope or the dates of a live tender, so missing one means bidding against
# superseded terms -- which is exactly the kind of thing the old extension
# captured and a listing-only sweep would drop.
#
# Deliberately ONE request per in-scope tender, never per row: enriching all
# 281 rows of a sweep instead of the 8 that matter would be a 35x increase in
# traffic for no benefit, and volume is the thing that gets a collector
# blocked.


async def fetch_other_details(session: GemSession, bid_id) -> dict:
    """Corrigendum / representation flags for one bid. Never raises."""
    try:
        body = await _post_json(
            session,
            f"/public-bid-other-details/{bid_id}",
            {"csrf_bd_gem_nk": session.token},
            what=f"other-details {bid_id}",
        )
        return dict(body.get("response") or {})
    except Exception as e:  # noqa: BLE001 -- an extra is never worth a sweep
        logger.debug("gem: other-details %s unavailable: %s", bid_id, e)
        return {}


async def enrich(session: GemSession, docs: list[dict], tenders: list[dict]) -> int:
    """Attach corrigendum info to the tenders we are about to ship.

    Best-effort and bounded. Returns how many tenders gained something, so the
    caller can report it. A failure here leaves the tender exactly as the
    listing described it, which is still worth shipping.
    """
    s = get_settings()
    if not s.gem_fetch_corrigenda or not tenders:
        return 0

    by_id = {}
    for d in docs:
        no = _first(d, "b_bid_number")
        if no:
            by_id[str(no).strip()] = d

    enriched = 0
    budget = s.gem_enrich_max_per_page
    for tender in tenders[:budget]:
        doc = by_id.get(tender.get("tenderId"))
        if not doc:
            continue
        bid_id = _first(doc, "b_id")
        # `is None`, not a truthiness check: an id of 0 is a real id, and
        # `if not bid_id` would silently skip it. Same trap as treating a
        # quantity of 0 as absent.
        if bid_id is None:
            continue

        details = await fetch_other_details(session, bid_id)
        if not details:
            continue

        if details.get("corrigendum"):
            # GeM does not expose a direct corrigendum file URL on this
            # endpoint -- the flag is the signal, and the bid document itself
            # is republished with the change folded in. Point at the document
            # so a human (and the analyzer) can see the current terms.
            tender["corrigendaLinks"] = [tender["sourceUrl"]]
            tender["numberOfAmendments"] = 1
            tender["description"] += " | CORRIGENDUM ISSUED"
            enriched += 1
        if details.get("representation"):
            tender["description"] += " | Representation filed"
            enriched += 1

        await asyncio.sleep(s.gem_enrich_delay_seconds)

    return enriched


# -- The sweep -----------------------------------------------------------


class GemFetcher:
    """Newest-first, one pass per search term, stopping on known ground."""

    portal = PORTAL
    version = FETCHER_VERSION

    #: Fields we expect the listing to populate, and how often. Drift is
    #: measured against these -- a portal that renames bd_category_name
    #: returns a valid page full of empty titles, and nothing throws.
    fill_baseline = {
        "tenderId": 1.0,
        "title": 0.95,
        "organisation": 0.9,
        "closingDate": 0.95,
    }

    def __init__(self, session: Optional[GemSession] = None) -> None:
        self._session = session

    async def _ensure_session(self) -> GemSession:
        if self._session is None:
            self._session = await bootstrap()
        return self._session

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None

    async def sweep(
        self, known: set[str], params: dict[str, Any]
    ) -> AsyncIterator[Batch]:
        """Walk each search term newest-first until caught up.

        ``params``:
          terms      list[str]  search terms (from the scope profile)
          mode       "incremental" (default) | "full"
          max_pages  int        per-term page cap
          ministries list[str]  override the configured ministry filter
          sort       str        GeM sort key

        Incremental mode stops a term after ``gem_stop_after_known``
        consecutive already-known bid numbers -- three clean pages in a row --
        so the work per run is bounded no matter how big the portal gets. A
        nightly ``full`` pass runs the same loop with that condition disabled,
        which is the only way to catch back-dated publications and status flips
        that an incremental pass structurally cannot see.
        """
        s = get_settings()
        terms: list[str] = [t for t in (params.get("terms") or []) if t] or s.fallback_terms
        mode = (params.get("mode") or "incremental").lower()
        max_pages = int(params.get("max_pages") or s.gem_max_pages_per_term)
        ministries = params.get("ministries")
        if ministries is None:
            ministries = s.ministries
        sort = params.get("sort") or "Bid-Start-Date-Latest"
        stop_after_known = 0 if mode == "full" else s.gem_stop_after_known

        session = await self._ensure_session()
        pages_done = 0

        for term in terms:
            consecutive_known = 0
            for page in range(1, max_pages + 1):
                try:
                    total, docs = await fetch_page(session, page, search=term, sort=sort)
                except PermissionError:
                    # The token or the WAF cookie went stale mid-sweep. One
                    # re-bootstrap, then give up on this page rather than
                    # looping: a second refusal is the portal telling us
                    # something we should listen to.
                    logger.info("gem: session rejected on page %s -- re-bootstrapping", page)
                    await self.aclose()
                    session = await self._ensure_session()
                    total, docs = await fetch_page(session, page, search=term, sort=sort)

                if not docs:
                    break

                batch = Batch(
                    portal=PORTAL,
                    page=page,
                    pages_done=pages_done + 1,
                    rows_seen=len(docs),
                    expected_total=total,
                    term=term or None,
                    fetcher=FETCHER_VERSION,
                    raw=docs,
                )

                for doc in docs:
                    if not in_scope(doc, ministries):
                        batch.rows_skipped += 1
                        continue
                    tender = to_tender(doc, term=term or None)
                    if tender is None:
                        batch.rows_skipped += 1
                        continue
                    batch.tenders.append(tender)
                    if tender["tenderId"] in known:
                        consecutive_known += 1
                    else:
                        consecutive_known = 0

                # One extra request per IN-SCOPE tender, after the page has
                # been filtered -- so a railway sweep enriches the handful
                # that matter rather than all ten rows.
                if batch.tenders:
                    try:
                        await enrich(session, docs, batch.tenders)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("gem: enrichment skipped on page %s: %s", page, e)

                pages_done += 1
                yield batch

                if stop_after_known and consecutive_known >= stop_after_known:
                    logger.info(
                        "gem: term %r caught up after %s consecutive known ids (page %s)",
                        term, consecutive_known, page,
                    )
                    break

                # numFound tells us when there is simply no page N+1.
                if total is not None and page * s.gem_page_size >= total:
                    break

                await asyncio.sleep(s.request_delay_seconds)

            await asyncio.sleep(s.request_delay_seconds)
