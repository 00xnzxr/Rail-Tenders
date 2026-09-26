"""
DRPL Collector - Configuration.

Deliberately its own settings object rather than an import from
``app.core.config``. The collector shares exactly three contracts with
drpl-backend (the ``agent_runs`` table, two Redis key formats, and
``POST /api/extension/tenders``) and nothing else, so either side can be
deployed, broken and rolled back alone.

Every value has a default that works on a laptop with no Redis and no
Postgres, so `scripts/first90.py` runs before any infrastructure exists.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Shared contract 1: the database holding agent_runs ──────────────
    # Same URL drpl-backend uses. The collector reads/writes the AgentRun
    # row it was handed and owns three tables of its own (collect_*).
    database_url: str = "sqlite:///./collector_local.db"

    # ── Shared contract 2: Redis (run event stream + cancel flag) ───────
    # Empty means "no queue, no streaming" — tasks.py then runs a sweep
    # synchronously and drops progress events on the floor, which is what
    # the standalone script wants.
    redis_url: str = ""
    #: The collector's OWN queue. Never the backend's `drpl-runs`: a sweep
    #: runs for minutes and would otherwise starve costing/analysis of the
    #: twelve shared worker slots. See §09 of the build sheet.
    rq_queue_name: str = "collect"
    #: Match run_service._RUN_JOB_TIMEOUT_SECONDS so a collect run cannot
    #: outlive what drpl-backend's reaper considers alive.
    rq_job_timeout_seconds: int = 60 * 30
    rq_result_ttl_seconds: int = 60 * 60

    # ── Shared contract 3: the ingest endpoint ──────────────────────────
    #: Base URL of drpl-backend, WITHOUT a trailing /api.
    drpl_api_url: str = "http://localhost:8000"
    #: Service-account credential. Either a `drpl_...` API token (preferred
    #: — revocable without locking anyone out) or a raw JWT. Sent verbatim
    #: as `Authorization: Bearer <token>`; get_current_user accepts both.
    drpl_service_token: str = ""
    sink_timeout_seconds: float = 60.0
    sink_max_retries: int = 4

    # ── Portal politeness ───────────────────────────────────────────────
    #: Seconds between requests to one portal. 3-5s per the scope line in
    #: §03b — one session per portal, backoff on 429/5xx.
    request_delay_seconds: float = 3.0
    #: A contactable UA. Portals that want to reach us should be able to.
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
        "(+DRPL tender collector; contact aniket@letstutecreation.com)"
    )

    # ── GeM ─────────────────────────────────────────────────────────────
    gem_base_url: str = "https://bidplus.gem.gov.in"
    #: Outbound proxy for every GeM request (http://, https:// or socks5://,
    #: credentials in the URL). Empty means direct. GeM's edge drops TCP from
    #: cloud egress ranges, so a worker on Railway needs an Indian address to
    #: come from; see collector/netconfig.py. The admin setting of the same
    #: name overrides this.
    gem_proxy_url: str = ""
    #: Server-fixed. Do not try to raise it — the endpoint ignores it.
    gem_page_size: int = 10
    #: Three clean pages in a row (30 already-known bid numbers) means the
    #: incremental sweep has caught up with the ledger and can stop.
    gem_stop_after_known: int = 30
    #: Hard ceiling per search term, so one bad term cannot walk 4,700 pages.
    gem_max_pages_per_term: int = 40
    #: Search terms used when the scope profile is unreachable or bare.
    #:
    #: "railway" leads deliberately. Measured against the live endpoint: an
    #: unfiltered ongoing-bids query returns ~47,500 rows (4,700 pages, not
    #: viable), while "railway" returns ~1,770 -- and unlike the generic spare
    #: names below, almost all of them really are Ministry of Railways. On the
    #: first live run the generic terms alone filtered out 273 of 281 rows.
    gem_fallback_terms: str = (
        "railway,indian railways,traction,locomotive,coach,wagon,"
        "bogie,wheelset,axle,traction motor,pantograph,signalling,"
        "overhead equipment,rolling stock,AMC,annual maintenance"
    )
    #: Terms ALWAYS swept, on top of whatever the scope profile says.
    #:
    #: The Tender Scope page is curated for relevance scoring -- its keywords
    #: describe the work DRPL wants ("bogie", "compressor"). They make poor
    #: full-text queries on their own because most hits belong to other
    #: ministries. "railway" is the broad net that actually finds railway
    #: tenders; the scope keywords then decide which of them matter.
    #: Set GEM_ALWAYS_TERMS="" to sweep only what the profile lists.
    gem_always_terms: str = "railway,indian railways"
    #: Ask /public-bid-other-details whether each shipped tender carries a
    #: corrigendum. A corrigendum changes the scope or the dates of a live
    #: tender, so missing one means bidding against superseded terms.
    gem_fetch_corrigenda: bool = True
    #: Ceiling on enrichment requests per page. Only in-scope tenders are
    #: enriched, so on a railway sweep this is single digits -- but a run with
    #: no ministry filter must not turn into ten extra requests per page.
    gem_enrich_max_per_page: int = 12
    #: Gap between enrichment requests. Shorter than the page delay because
    #: these are small reads against a lighter endpoint, but never zero.
    gem_enrich_delay_seconds: float = 1.0

    # ── GeM: complete enumeration ───────────────────────────────────────
    #
    # The keyword sweep's coverage is a property of the word list, which is
    # not something you can audit. Enumerating the whole ongoing-bids list
    # and filtering on GeM's own ministry field makes coverage arithmetic
    # instead: 4,725 pages at ten rows, every one examined.
    #
    # Measured against the live portal 2026-09-12: 0.2-0.6s per page at
    # best, up to 2.5s later the same afternoon. The walk keeps this many
    # pages in flight at all times rather than fetching gathered windows.
    #: Pages in flight during enumeration. Six is well inside what the
    #: endpoint tolerated and leaves headroom for the detail stage, which
    #: shares the same connection pool and the same politeness budget.
    gem_concurrency: int = 6
    #: Each slot rests this long after its own page, so the aggregate rate is
    #: gem_concurrency / (latency + pause) and a slow request holds up only
    #: itself.
    gem_page_delay_seconds: float = 0.4
    #: Independent portal sessions a full sweep opens: one for the detail
    #: stage, the rest shared round-robin by the page walk. GeM serves about
    #: four requests at a time per session cookie and queues the rest --
    #: measured 2026-09-12: six concurrent pages on one session, four back in
    #: 0.5 s and two in 1.6 s; on six sessions, all six in 0.5 s -- so
    #: concurrency past that only pays across sessions, and the PDF reads
    #: stop competing with the walk for the same queue. Set to 1 to run
    #: everything through a single session (a borrowed Chrome login, tests).
    gem_sessions: int = 4
    #: After a full enumeration, walk the configured search terms to the end
    #: and count what they find that the enumeration did not. Closes the one
    #: loss a live list still has under the stable sort -- a row that moved
    #: up a page when a bid behind the walker closed (4 in 1,785, measured)
    #: -- for the cost of about 420 pages. Off for an audit that wants the
    #: bare enumeration's number.
    gem_full_repair_pass: bool = True

    # ── GeM: the ministry-filtered sweep ────────────────────────────────
    #
    # GeM's Advanced Search filters by the buyer's ministry server-side, on
    # the same field the scope check reads, and answers an anonymous request.
    # For Ministry of Railways that is 171 pages instead of 4,378 -- the same
    # tenders for about a quarter of the traffic.
    #
    # The catch, measured on 2026-09-22: /search-bids ignores the sort key and
    # its paging is not stable, so ONE pass returned 1,426 of 1,703 distinct
    # bids. Repeated passes converge -- 1405, 1628, 1684, 1703 -- and then sit
    # exactly on the portal's own numFound for four more passes.
    #
    # That is why this mode is written as "repeat until the distinct count
    # reaches the portal's own total" rather than "read every page once". The
    # denominator is GeM's, not ours, so the mode cannot quietly under-collect
    # the way a single pass would: it either reaches numFound and says so, or
    # it stops short and reports coverage below 1.
    #: Hard ceiling on convergence passes. Four sufficed when measured; this
    #: bounds the pathological case rather than describing the normal one.
    gem_ministry_max_passes: int = 10
    #: Stop early after this many consecutive passes that found nothing new.
    #: Guards the case where numFound is unreachable (a row the query counts
    #: but never serves), which would otherwise spend the whole ceiling.
    gem_ministry_settle_passes: int = 2

    # ── GeM: the detail stage ───────────────────────────────────────────
    #
    # Every number the DRPL tender card shows as a dash -- worth, EMD, ePBG --
    # and the whole eligibility block live in the bid PDF, never in the
    # listing. This stage fetches it and reads it with collector.gemdoc.
    #: Fetch and parse each in-scope bid document. Off means listing-only
    #: rows, which is what the collector shipped before and is still a valid
    #: fast mode for a smoke run.
    gem_fetch_details: bool = True
    #: Documents in flight. Modest on purpose: these are 100-200KB PDFs and
    #: the politeness budget is shared with the listing walk.
    gem_detail_concurrency: int = 4
    gem_detail_delay_seconds: float = 0.25
    #: Batches whose documents may still be in flight while the page walk
    #: continues. The detail stage is pipelined behind the enumeration rather
    #: than run inline per page, so the two overlap instead of adding up; this
    #: bounds how far the walk can run ahead of the documents being read.
    gem_detail_lookahead: int = 16
    #: Ask Claude to read a document the deterministic parser could not.
    #: Costs money, so it is off by default and bounded by enrich_max_per_run
    #: when on; `needs_ai_fallback` is how gemdoc asks for it.
    gem_detail_ai_fallback: bool = False

    # ── IREPS ───────────────────────────────────────────────────────────
    ireps_base_url: str = "https://www.ireps.gov.in"
    #: Mount a Railway volume here. Without it every deploy costs an OTP
    #: and a human; with it, logins are rare. See §09.
    ireps_profile_dir: str = "./.ireps-profile"
    ireps_mobile: str = ""
    ireps_max_pages: int = 20
    ireps_headless: bool = True
    #: How long guest_login blocks waiting for the webhook to deliver a code.
    otp_wait_seconds: int = 90

    # ── OTP webhook (api.py) ────────────────────────────────────────────
    otp_shared_secret: str = ""
    otp_max_clock_skew_seconds: int = 120

    # ── AI enrichment (Claude Haiku 4.5) ────────────────────────────────
    #
    # Worth and EMD are not on any listing page, on either portal -- they are
    # inside the bid PDF. One Haiku call per NEW tender reads them out, along
    # with the eligibility criteria, the scope and the buyer's contact.
    #
    # Everything degrades cleanly: no key means no enrichment, and the sweep
    # still ships tenders with exactly the listing data it has today.
    anthropic_api_key: str = ""
    #: Claude Haiku 4.5. Matches drpl-backend's own `ai_model` default, so both
    #: services bill against the same tier.
    ai_model: str = "claude-haiku-4-5"
    enrich_documents_enabled: bool = True
    #: Concurrent document reads. Each holds a PDF in memory and one API call;
    #: 3 keeps a worker's footprint small and stays far inside rate limits.
    enrich_concurrency: int = 3
    #: Ceiling per page of results, so one unusual page cannot turn into a
    #: hundred model calls.
    enrich_max_per_page: int = 12
    #: Whole-run ceiling -- the stop that actually bounds spend. Measured on
    #: live GeM bid PDFs: ~20,600 input + ~530 output tokens each, about $0.023
    #: at Haiku list price. 200 caps a single run near $4.60; only NEW tenders
    #: are read, so an ordinary re-run spends a few cents or nothing.
    enrich_max_per_run: int = 200
    enrich_timeout_seconds: float = 180.0
    enrich_max_document_bytes: int = 12 * 1024 * 1024

    # ── Scope ───────────────────────────────────────────────────────────
    #: Only rows whose portal-stated ministry matches one of these are kept.
    #: Comma-separated. Matched case-insensitively against the STRUCTURED
    #: field, never as a keyword — that is the whole fix for the keyword
    #: problem in §04b.
    target_ministries: str = "Ministry of Railways"

    class Config:
        env_file = os.environ.get("COLLECTOR_ENV_FILE", ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"

    # ── Derived helpers ─────────────────────────────────────────────────

    @property
    def ministries(self) -> list[str]:
        return [m.strip() for m in (self.target_ministries or "").split(",") if m.strip()]

    @property
    def fallback_terms(self) -> list[str]:
        return [t.strip() for t in (self.gem_fallback_terms or "").split(",") if t.strip()]

    @property
    def always_terms(self) -> list[str]:
        return [t.strip() for t in (self.gem_always_terms or "").split(",") if t.strip()]

    @property
    def ingest_url(self) -> str:
        return self.drpl_api_url.rstrip("/") + "/api/extension/tenders"

    @property
    def config_url(self) -> str:
        return self.drpl_api_url.rstrip("/") + "/api/extension/config"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
