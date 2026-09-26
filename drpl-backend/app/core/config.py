"""
DRPL Backend - Configuration
Loads settings from environment variables / .env file
"""

import os

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database (defaults to SQLite for local testing — no PostgreSQL needed)
    database_url: str = "sqlite:///./drpl_local.db"

    # JWT
    jwt_secret_key: str = "change-this-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 720  # 30 days

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = True

    # File storage
    upload_dir: str = "./uploads"

    # Storage backend: "local" for dev filesystem, "r2" for Cloudflare R2
    storage_backend: str = "local"

    # Cloudflare R2 (S3-compatible) — required when storage_backend == "r2"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "drpl-platform-prod"
    r2_endpoint_url: str = ""  # optional override; else derived from account id
    presigned_url_ttl_seconds: int = 3600
    # Download links are short-lived: a browser follows them immediately, so
    # they do not need the hour that embed URLs get.
    presigned_download_ttl_seconds: int = 300

    # AI Configuration
    ai_provider: str = "anthropic"  # "anthropic" or "openai"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    # RunPod serverless OCR (PaddleOCR-VL behind vLLM). Empty = not configured;
    # the page-vision cascade then skips that provider. See runpod_ocr_service.
    runpod_api_key: str = ""
    runpod_ocr_endpoint_id: str = ""
    # A self-hosted chat model on a second RunPod serverless endpoint (vLLM,
    # OpenAI-compatible). This is the whole local-model surface, and it is
    # deliberately inert: no platform agent is rostered onto it
    # (`seed_agent_models.PREFERRED_AGENT_MODELS` does not name
    # `local_model_probe`), nothing fails over to it, and an unset endpoint id
    # means the provider simply cannot be built. It exists so the
    # OpenAI-compatible transport is real, monitored and priced correctly
    # BEFORE anything that matters is ever moved onto it.
    # DRPL's own vLLM endpoint. Shipped as a default because an endpoint id is
    # an identifier, not a credential -- it does nothing without the key, and
    # RUNPOD_OCR_ENDPOINT_ID is already committed in .env.example the same way.
    # The effect is that production has exactly ONE thing to configure: the
    # key. The PlatformSetting of the same name still overrides this, which is
    # what a redeployed or replaced endpoint needs.
    runpod_chat_endpoint_id: str = "9d05slartfxnql"
    # A second key, because the endpoints are separate and so are their
    # credentials. Measured 2026-09-22: each key returns 403 on the other's
    # endpoint, in BOTH directions -- RunPod scopes keys per endpoint on this
    # account. The fallback to `runpod_api_key` is kept for an account with one
    # account-wide key, but on this one it lands on a 403, which is why the
    # error path names the credential rather than the model name for that
    # status. Configuring the chat key never touches the OCR credential.
    runpod_chat_api_key: str = ""
    # The name the ENDPOINT serves, which is not the Hugging Face repo id.
    # Measured 2026-09-22: this vLLM worker answers to "qwen2.5-7b" and returns
    # HTTP 500 for "Qwen/Qwen2.5-7B-Instruct" -- a wrong model name here looks
    # exactly like a broken endpoint.
    runpod_chat_model: str = "qwen2.5-7b"
    # Serverless cold start, not steady-state latency: the OCR endpoint's first
    # call measured 160 s against 1.5 s warm. A shorter timeout here would make
    # a cold endpoint look broken.
    runpod_chat_timeout_s: float = 240.0
    # Platform default for ordinary worker-agent work. Haiku since the
    # two-model rollout — see seed_agent_models.PREFERRED_AGENT_MODELS.
    ai_model: str = "claude-haiku-4-5"
    ai_batch_size: int = 10

    # Auto tender-scoring agent (mandatory automation)
    auto_scoring_enabled: bool = True
    # High-volume, runs on a 120s timer over batches of 25 — throughput tier.
    auto_scoring_model: str = "claude-haiku-4-5"
    auto_scoring_interval_seconds: int = 120
    auto_scoring_batch_size: int = 25
    auto_scoring_max_concurrency: int = 3
    auto_scoring_max_retries: int = 3
    value_threshold_inr: float = 5_000_000   # ₹50 lakh — tenders below are flagged below_threshold

    # Tender segmentation thresholds (score is 0-1)
    segment_discard_below: float = 0.40   # score < this -> discarded
    segment_bidable_at: float = 0.60      # score >= this (and value ok) -> to_bid
    # Weekly auto-discard cleanup
    auto_discard_enabled: bool = True
    auto_discard_days: int = 7
    auto_discard_interval_hours: int = 24

    # Archive sweep — past-due untouched tenders. Ships OFF; enable from admin
    # after checking scripts/archive_sweep_dryrun.py output.
    archive_sweep_enabled: bool = False
    archive_grace_days: int = 3         # days past closing_date before archiving
    archive_purge_days: int = 7         # days in archive before hard delete
    archive_sweep_interval_hours: int = 6
    archive_sweep_batch_size: int = 500

    # Extension auto-capture (gated auto-capture of tenders from the browser extension)
    extension_auto_capture_enabled: bool = False

    # Eager tender analysis (auto-run v2 analyzer for in-scope tenders on first pass)
    eager_analysis_enabled: bool = False
    eager_analysis_min_score: float = 0.70
    eager_analysis_interval_seconds: int = 180
    eager_analysis_batch_size: int = 5
    eager_analysis_max_concurrency: int = 1
    eager_analysis_daily_cap: int = 200
    eager_analysis_queue_ceiling: int = 20

    # LangChain / Agent Configuration
    tavily_api_key: str = ""
    langchain_max_iterations: int = 10

    # Google Gemini (Search Grounding - primary web search provider)
    google_api_key: str = ""
    gemini_search_model: str = "gemini-3.7-flash"
    # Claude web search (server-side tool) -- the tier after Gemini and Tavily,
    # on the Anthropic key the platform already holds. Haiku 4.5 takes the
    # basic `web_search_20250305` tool; each search is billed per use.
    anthropic_search_model: str = "claude-haiku-4-5"
    anthropic_search_max_uses: int = 3

    # Multi-Provider Failover
    failover_enabled: bool = True
    failover_providers: str = "openai,google"

    # Voyage AI (Embeddings)
    voyage_api_key: str = ""

    # MCP Server
    mcp_server_enabled: bool = False
    mcp_server_port: int = 8001

    # Master Admin (used by seed.py)
    admin_email: str = "admin@drpl.com"
    admin_password: str = ""

    # Tender Analyzer v2 (vision-first, parallel, tiered)
    # Default ON: every PDF is sent as a Claude vision document block in parallel
    # batches (handles scanned PDFs natively), with per-doc Haiku extraction and
    # one final Sonnet synthesis pass. Set to false to fall back to the legacy
    # single-bundle v1 path (text-only fallback for scanned PDFs).
    tender_analyzer_v2_enabled: bool = True
    # Per-PDF vision extraction: high volume, one document at a time.
    tender_analyzer_per_doc_model: str = "claude-haiku-4-5"
    # Synthesis reasons across every per-doc result. On Haiku with the rest of
    # the tender-analysis path since the two-model rollout.
    tender_analyzer_synthesis_model: str = "claude-haiku-4-5"
    # Annexure extraction runs entirely on Haiku (cheap, vision-capable). The
    # annexure_finder agent otherwise inherits the platform default (Sonnet),
    # which is far more expensive for what is a high-volume per-PDF vision
    # extraction. Forced via model_override at every extraction call so it can't
    # be silently bumped back to Sonnet by the agent record. Override with
    # ANNEXURE_FINDER_MODEL if a different tier is ever needed.
    annexure_finder_model: str = "claude-haiku-4-5"
    # Two-pass annexure extraction (design 2026-07-23-annexure-verbatim-fidelity).
    # Pass 1 (discovery) stays on `annexure_finder_model` (Haiku) and only locates
    # each form — identifier/title/page_range/orientation, NO body text. Pass 2
    # re-reads ONLY that annexure's own pages and transcribes it verbatim.
    #
    # Why the split: the old single-pass call had to discover AND transcribe every
    # form on a 30-page batch inside one 24576-token budget. On dense tenders that
    # budget cannot hold verbatim text for a dozen forms, so the model silently
    # compressed — dropping numbered clauses, collapsing address blocks, and
    # substituting descriptive "[Name of Bidder]" placeholders where the source had
    # dotted rules. Pass 2 gives each annexure its own full budget over a handful of
    # pages, which removes the pressure that caused the paraphrasing.
    #
    # Sonnet for pass 2: verbatim transcription of dense legal text (bank guarantee
    # bonds, indemnity clauses) is exactly where Haiku's instruction-following
    # degrades. Discovery stays on Haiku so the expensive model only ever sees a few
    # pages at a time.
    # Verbatim legal transcription. This was pinned to Opus because a
    # paraphrased annexure is a submission risk — the `[Name of Bidder]`
    # placeholder failure that `quality_tools.diagnose_tender_outputs` checks
    # for. Moved to Haiku with the rest of the platform on the owner's explicit
    # call; the diagnostic still catches the failure at runtime, and this is the
    # first pin to raise if transcription quality drops.
    annexure_transcription_model: str = "claude-haiku-4-5"
    # The model the SECOND attempt uses, and only the second attempt.
    #
    # Pass 2 already rejects its own output and retries when the transcription
    # comes back with descriptive placeholders, an unusable envelope or an
    # empty body — but both attempts used to run on the same model. The
    # documented failure is precisely that "verbatim transcription of dense
    # legal text is exactly where Haiku's instruction-following degrades", so
    # asking the same model again with a sterner prompt is asking the model
    # that just failed to try harder. Nine successive Liluah reports are what
    # that cost: paraphrased annexures with `[Name of Bidder]` where the page
    # printed a dotted rule, which `quality_tools` can detect but not repair.
    #
    # This is the pin CLAUDE.md says to raise -- raised ONLY where it is
    # earned. The happy path is untouched and still costs a Haiku call; the
    # escalation fires per-annexure, on a detected defect, so the two-model
    # economics survive. Set empty to restore same-model retries.
    annexure_transcription_escalation_model: str = "claude-sonnet-5"
    annexure_two_pass_enabled: bool = True
    # Pages either side of a detected annexure to include in its transcription
    # window, so a form whose page_range is off by one is not truncated mid-clause.
    annexure_transcription_page_padding: int = 1
    # Concurrent pass-2 transcription calls. Kept modest: each call carries a PDF
    # vision payload, and the tender analyzer's sequential-by-default note in this
    # file documents what memory pressure does to parallel PDF work.
    annexure_transcription_concurrency: int = 3

    # Backend NIT public-link fetch fallback (design 2026-07-21-backend-nit-link-fetch)
    nit_link_fetch_enabled: bool = False
    nit_link_fetch_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    nit_link_fetch_timeout_s: int = 25
    nit_link_fetch_max_docs_per_tender: int = 8
    nit_link_fetch_delay_s: float = 0.5

    # Decision Maker (Master Agent) autonomy. When True (default), complex
    # requests routed to the decision_maker run in AUTONOMOUS mode: it forms its
    # own plan, executes it with the full agent/tool catalog, and only pauses to
    # ask the user (via the ask_user tool) when genuinely blocked — no plan
    # approval click. Set DECISION_MAKER_AUTONOMOUS=false to restore the legacy
    # propose-plan-then-approve workflow.
    decision_maker_autonomous: bool = True
    # The Master coordinates and delegates rather than doing domain work
    # itself, so it runs on the same model as the workers it calls.
    master_agent_model: str = "claude-haiku-4-5"
    # Wall-clock and iteration budget for one Master Agent run.
    #
    # These were literals of 15 / 120.0 inside decision_maker_agent, which made
    # the orchestrator's budget SMALLER than a single one of its delegations:
    # `_run_deep_analyzer_for_tender` alone is allowed 1200s. Any request that
    # actually produced something (analyze a tender, then cost it) was killed
    # by `asyncio.wait_for` while a worker was still legitimately running, and
    # the user was told to "continue from where I left off" — a restart that
    # hit the same wall again. The budget must outlast the longest delegation.
    #
    # Lower it only for a deployment where chat is answering questions rather
    # than running production jobs (MASTER_AGENT_MAX_EXECUTION_TIME_S).
    master_agent_max_execution_time_s: float = 1800.0
    master_agent_max_iterations: int = 25
    # How much of one worker agent's answer the Master receives inline when it
    # delegates (MASTER_WORKER_OUTPUT_MAX_CHARS).
    #
    # This was a 1,500-character literal named `output_preview`, and the
    # Master's message is what the user reads — so a 30,000-character forensic
    # analysis was produced correctly, handed over as its opening paragraphs,
    # and summarized from those. 20,000 characters is roughly 5k tokens, which
    # carries a whole worker answer in the ordinary case and still leaves the
    # Master room for several delegations inside one run. Past it, the rest is
    # not discarded: `read_worker_output` pages through the remainder.
    master_worker_output_max_chars: int = 20000
    # How much of the conversation the Master is given on each turn
    # (MASTER_HISTORY_MAX_TURNS / MASTER_HISTORY_MAX_CHARS_PER_TURN).
    #
    # `run_decision_maker` accepted `conversation_history`, forwarded it to the
    # worker tools, and then invoked itself with a single HumanMessage — so the
    # platform's default chat entrypoint had no memory of the conversation at
    # all. Every turn was turn one, which is why asking it to use what was said
    # earlier produced an answer to a question nobody had asked.
    #
    # Per-turn cap because an assistant turn stores the whole answer it gave: a
    # dozen of those unbudgeted is a bigger prompt than the work it precedes.
    master_history_max_turns: int = 12
    master_history_max_chars_per_turn: int = 4000
    # Ceiling on the whole history block, because the per-turn cap does not
    # bound the prompt on its own: twelve turns at four thousand characters is
    # fifty thousand characters of history before the request is even read, and
    # a conversation of long answers reaches that every time. ~10k tokens.
    # Oldest turns are dropped first; what falls out of the window is still
    # reachable through `conversation_history_search`.
    master_history_max_total_chars: int = 40000
    # Output ceiling for one Master answer (MASTER_AGENT_MAX_OUTPUT_TOKENS),
    # clamped per-model by `model_limits.clamp_max_tokens` (Haiku 4.5 tops out
    # at 32k). This was an 8192 literal, and the Master is the agent that
    # relays a specialist's work — a run that delegates twice can want more
    # room than that. Raising the ceiling costs nothing on its own: providers
    # bill the tokens generated, not the cap. `create_react_agent` runs its own
    # message loop, so the reliability layer's auto-continuation never sees
    # this call and a truncated answer would otherwise be returned as if it
    # were whole; `run_decision_maker` says so explicitly when it happens.
    master_agent_max_output_tokens: int = 16384
    # Per-user monthly AI budget (app/services/budget_service.py). Spend is
    # derived from the APIUsageLog ledger for the calendar month; these are the
    # policy knobs. A user with no `user_budgets` row uses the default, so the
    # common case needs no row.
    default_monthly_budget_usd: float = 30.0
    budget_warning_threshold: float = 0.8   # amber band starts here
    # Set BUDGET_ENFORCEMENT_ENABLED=false to meter without blocking anyone —
    # the same no-deploy escape hatch the confirm gate has. Useful for
    # collecting real numbers before the cap goes live.
    budget_enforcement_enabled: bool = True
    # Startup refresh that moves agent rows off superseded/invalid models onto
    # their tier's current one (app/services/seed_agent_models.py). Rows an
    # admin deliberately pinned to another *current* model are left alone; only
    # stale, unknown, or empty ones move. Set false to freeze agent models.
    agent_model_refresh_enabled: bool = True
    # Startup pass that clears an agent's tool assignment when it exactly
    # matches that agent's canonical default (app/services/seed_agent_tools.py).
    # An empty assignment now means "the whole shared tool repo", so those
    # copied lists cap an agent rather than equip it. A human's own assignment
    # differs from the canonical list and is never touched.
    agent_tool_release_enabled: bool = True
    # Write-confirmation gate (app/services/langchain/tool_policy.py). The agent
    # stays autonomous — it plans and acts on its own — but any tool that
    # changes the user's data suspends for an explicit confirmation first.
    # Applies to every decision_maker surface (global assistant popup, Command
    # Center, agent chat) so there is one safety model rather than several.
    # Set ASSISTANT_CONFIRM_GATE_ENABLED=false to restore the previous
    # act-without-asking behaviour without a deploy.
    assistant_confirm_gate_enabled: bool = True
    # Per-tender document concurrency. Default lowered to 1 (sequential) on
    # 2 May 2026 after multi-PDF tenders exhibited silent per-doc failures
    # under parallel load (3 PDFs simultaneously occasionally produced empty
    # synthesis output even though each individual call succeeded — likely a
    # combination of memory pressure on long PDFs + per-coroutine SQLAlchemy
    # session contention).
    #
    # Tradeoffs of sequential mode:
    #   + Each per-doc call gets the full LLM/network budget — no rate-limit
    #     risk, no concurrent context-window pressure, easier to debug from logs.
    #   + Wall-clock for N PDFs is roughly N × 30–60s. For typical 3-PDF
    #     tenders this is ~3 minutes, which fits within the auto-chain's
    #     480s prerequisite-timeout (see chat_agent_wrappers.py:_ensure_tender_analysis).
    #   - Slower than parallel for 5+ PDF tenders. If you have a tender with
    #     8+ PDFs and accept the failure-mode risk, raise this back to 4-8 via
    #     env var TENDER_ANALYZER_MAX_PARALLEL.
    tender_analyzer_max_parallel: int = 1
    # Overall per-doc page cap. Anthropic's native PDF endpoint actually
    # rejects anything over 100 pages, so >100-page PDFs route through the
    # text-extraction fallback in _analyze_single_document_native
    # (pdfplumber → Claude vision per-page → Tesseract OCR cascade). The
    # 1500 ceiling well exceeds any tender we've seen and bounds the
    # text-extraction cost. PDFs > 1500 pages are marked unreadable —
    # they should be split before upload anyway.
    tender_analyzer_per_doc_max_pages: int = 1500
    # Batch-analysis concurrency across multiple tenders (used by the batch
    # endpoint). Each concurrent tender itself fans out to tender_analyzer_max_parallel
    # per-doc calls, so the worst-case outstanding-request count is the product.
    tender_analyzer_batch_max_parallel: int = 3

    # Phase C: Redis + RQ (optional — absent URL disables queue/cache, code degrades gracefully)
    redis_url: str = ""
    rq_queue_name: str = "drpl-runs"
    redis_cache_ttl_seconds: int = 300

    # Notification Center (Resend + scheduled jobs)
    # When resend_api_key is empty, email sending is a no-op (logs and drops) so
    # local dev keeps working. In-app notifications and SSE delivery are
    # independent of this config.
    resend_api_key: str = ""
    resend_from_email: str = "DRPL Platform <notifications@drpl.local>"
    # 13:00 UTC ≈ 18:30 IST — adjust via env var for other timezones.
    notifications_digest_hour_utc: int = 13
    notifications_closing_date_warning_hours: int = 48
    # Public-facing app base URL used when building action links inside emails.
    # In production set to e.g. "https://app.drpl.com" so links land on the SPA.
    notifications_app_base_url: str = "http://localhost:5173"

    class Config:
        # Overridable so the test suite can opt out of the developer's local
        # .env entirely. Without this, any flag someone sets locally (e.g.
        # AUTO_SCORING_ENABLED=false) silently leaks into tests that assert
        # the shipped defaults, and they fail on that machine only.
        env_file = os.environ.get("DRPL_ENV_FILE", ".env")
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


def platform_build() -> str:
    """The git commit this process runs, for logs and the costing reply.

    Railway sets RAILWAY_GIT_COMMIT_SHA on every deploy; a build that never
    reached the worker (the one that reads the NIT) is otherwise invisible
    from the output -- two costings on the same tender read as "the fix
    changed nothing" when the fix had not shipped.
    """
    for var in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA", "SOURCE_COMMIT"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v[:7]
    return "local"
