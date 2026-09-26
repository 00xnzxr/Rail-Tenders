"""
DRPL Backend - Settings Service
Runtime settings management with DB-first, .env fallback
"""

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.platform_setting import PlatformSetting

_env_settings = get_settings()

# Default settings to seed on first run
DEFAULT_SETTINGS = [
    # AI Settings
    {"key": "anthropic_api_key", "value": "", "value_type": "string", "category": "ai", "description": "Anthropic (Claude) API key — set here to override .env", "is_secret": True},
    {"key": "openai_api_key", "value": "", "value_type": "string", "category": "ai", "description": "OpenAI API key — set here to override .env (if using OpenAI provider)", "is_secret": True},
    {"key": "google_api_key", "value": "", "value_type": "string", "category": "ai", "description": "Google Gemini API key — enables Search Grounding for web research (primary search provider)", "is_secret": True},
    {"key": "tavily_api_key", "value": "", "value_type": "string", "category": "ai", "description": "Tavily API key — secondary web search provider (falls back to DuckDuckGo if not set)", "is_secret": True},
    {"key": "gemini_search_model", "value": "gemini-3.7-flash", "value_type": "string", "category": "ai", "description": "Gemini model for Search Grounding. gemini-3.7-flash is the current worker tier; gemini-3.1-pro-preview for harder synthesis, gemini-3.5-flash-lite for cost.", "is_secret": False},
    {"key": "ai_provider", "value": "anthropic", "value_type": "string", "category": "ai", "description": "Primary AI provider (anthropic, openai, or google) — used as the DEFAULT when an agent has no provider pinned. Per-agent overrides in Agent Builder still take precedence unless force_provider_override is set.", "is_secret": False},
    {"key": "force_provider_override", "value": "auto", "value_type": "string", "category": "ai", "description": "Kill-switch that forces ALL agents to use one provider, overriding every per-agent provider setting. Values: 'auto' (default — use per-agent), 'anthropic', 'openai', 'google'. Set to a specific provider when one provider is unavailable (e.g. Claude credits depleted) to route everything through another.", "is_secret": False},
    {"key": "failover_enabled", "value": "true", "value_type": "bool", "category": "ai", "description": "Enable automatic provider failover on rate limits (Claude -> OpenAI -> Gemini)", "is_secret": False},
    {"key": "failover_providers", "value": "openai,google", "value_type": "string", "category": "ai", "description": "Comma-separated fallback provider order when primary hits rate limits", "is_secret": False},
    {"key": "ai_model", "value": "claude-haiku-4-5", "value_type": "string", "category": "ai", "description": "Default model for any agent that has no model of its own in Agent Builder. claude-haiku-4-5 since the two-model rollout: Haiku for work that has to be right, Luna for short structurally-simple work. The Master Agent runs on Haiku too.", "is_secret": False},
    {"key": "ai_batch_size", "value": "10", "value_type": "int", "category": "ai", "description": "Number of tenders per batch analysis", "is_secret": False},
    {"key": "ai_max_context_chars", "value": "4000", "value_type": "int", "category": "ai", "description": "Maximum characters sent to AI per request", "is_secret": False},
    {"key": "ai_temperature", "value": "0.7", "value_type": "float", "category": "ai", "description": "Default temperature for AI responses (0.0-2.0)", "is_secret": False},
    {"key": "ai_max_tokens", "value": "4096", "value_type": "int", "category": "ai", "description": "Default max tokens per AI response", "is_secret": False},
    # Voyage AI Embeddings
    {"key": "voyage_api_key", "value": "", "value_type": "string", "category": "ai", "description": "Voyage AI API key for document embeddings (Anthropic's recommended embedding provider)", "is_secret": True},
    {"key": "voyage_embedding_model", "value": "voyage-3.5", "value_type": "string", "category": "ai", "description": "Embedding model: voyage-3.5 (balanced), voyage-3-large (best quality), voyage-3.5-lite (fast/cheap), voyage-code-3, voyage-finance-2, voyage-law-2", "is_secret": False},
    {"key": "embedding_chunk_size", "value": "512", "value_type": "int", "category": "ai", "description": "Token count per text chunk for embedding (default 512)", "is_secret": False},
    {"key": "embedding_chunk_overlap", "value": "64", "value_type": "int", "category": "ai", "description": "Token overlap between adjacent chunks (default 64)", "is_secret": False},
    # Claude PDF Native Support
    {"key": "claude_pdf_native_enabled", "value": "true", "value_type": "bool", "category": "ai", "description": "Send PDFs natively to Claude as document blocks (enables visual understanding of tables, charts, layouts)", "is_secret": False},
    {"key": "claude_pdf_max_pages", "value": "200", "value_type": "int", "category": "ai", "description": "Max pages per PDF for native Claude processing (API max: 600, each page ~1500-3000 tokens)", "is_secret": False},
    {"key": "claude_pdf_cache_enabled", "value": "true", "value_type": "bool", "category": "ai", "description": "Enable prompt caching for PDF document blocks (reduces cost on repeated analysis)", "is_secret": False},
    # Per-page PDF vision fallback (scanned docs)
    {"key": "pdf_vision_fallback_enabled", "value": "true", "value_type": "bool", "category": "ai", "description": "When a PDF page has very little extractable text (likely scanned), re-read that single page via Claude native PDF vision before falling back to Tesseract OCR.", "is_secret": False},
    {"key": "pdf_vision_density_min_chars", "value": "40", "value_type": "int", "category": "ai", "description": "Minimum character count per page from pdfplumber below which we treat the page as scanned and try Claude vision fallback.", "is_secret": False},
    {"key": "pdf_vision_model", "value": "claude-haiku-4-5", "value_type": "string", "category": "ai", "description": "Model for per-page scanned-PDF vision extraction. High volume, one page at a time — bulk tier. Override to claude-sonnet-5 for accuracy on important docs.", "is_secret": False},
    {"key": "pdf_ocr_provider", "value": "claude_then_runpod", "value_type": "string", "category": "ai", "description": "Which reader a scanned page goes to, in order: claude_then_runpod (Claude vision, RunPod PaddleOCR-VL when that fails or the page cap is spent), runpod_then_claude, runpod, claude. Tesseract remains the last resort in every mode.", "is_secret": False},
    {"key": "runpod_ocr_endpoint_id", "value": "", "value_type": "string", "category": "ai", "description": "RunPod serverless endpoint id hosting PaddleOCR-VL — set here to override RUNPOD_OCR_ENDPOINT_ID in .env", "is_secret": False},
    {"key": "runpod_api_key", "value": "", "value_type": "string", "category": "ai", "description": "RunPod API key — set here to override RUNPOD_API_KEY in .env", "is_secret": True},
    {"key": "runpod_chat_endpoint_id", "value": "9d05slartfxnql", "value_type": "string", "category": "ai", "description": "RunPod serverless endpoint id hosting the self-hosted chat model behind vLLM (OpenAI-compatible). Defaults to DRPL's own endpoint; an endpoint id is an identifier, not a credential. Empty here falls through to RUNPOD_CHAT_ENDPOINT_ID and then to the shipped default; with no endpoint anywhere the 'runpod' provider cannot be built and the Local Model Probe says so instead of calling anything.", "is_secret": False},
    {"key": "runpod_chat_model", "value": "qwen2.5-7b", "value_type": "string", "category": "ai", "description": "Model id the vLLM endpoint serves, EXACTLY as the endpoint names it — not the Hugging Face repo id. Measured 2026-09-22: this endpoint answers to qwen2.5-7b and returns 500 for Qwen/Qwen2.5-7B-Instruct. Text-only: nothing that reads a PDF page can be pointed at this provider.", "is_secret": False},
    {"key": "runpod_chat_api_key", "value": "", "value_type": "string", "category": "ai", "description": "RunPod API key for the chat endpoint. Separate from runpod_api_key because a key scoped to the OCR endpoint returns 403 on this one; empty falls back to runpod_api_key. Overrides RUNPOD_CHAT_API_KEY in .env.", "is_secret": True},
    # GeM Search (server-side collector)
    {"key": "gem_proxy_url", "value": "", "value_type": "string", "category": "general", "description": "Outbound proxy for GeM Search, e.g. http://user:pass@host:port or socks5://user:pass@host:port. GeM's firewall drops cloud egress addresses, so the worker needs an Indian address to come from. Overrides GEM_PROXY_URL in .env; empty = direct.", "is_secret": True},
    {"key": "pdf_vision_max_pages_per_doc", "value": "40", "value_type": "int", "category": "ai", "description": "Safety cap on how many scanned pages per document we'll re-read via Claude vision in one extraction run.", "is_secret": False},
    # Advisor tool (second-opinion extended thinking)
    {"key": "advisor_model", "value": "claude-opus-5", "value_type": "string", "category": "ai", "description": "Model for the Advisor tool (second-opinion calls). Must be at least as capable as the model that asks for the advice, so keep it on the flagship tier — a weaker advisor than executor is rejected by the API.", "is_secret": False},
    # Extended Thinking & Effort
    {"key": "thinking_mode", "value": "auto", "value_type": "string", "category": "ai", "description": "Default thinking mode: auto (adaptive for 4.6, disabled for older), adaptive, enabled (manual with budget), disabled", "is_secret": False},
    {"key": "thinking_budget_tokens", "value": "10000", "value_type": "int", "category": "ai", "description": "Token budget for manual extended thinking (used when thinking_mode is 'enabled')", "is_secret": False},
    {"key": "effort_level", "value": "", "value_type": "string", "category": "ai", "description": "Default effort level: low, medium, high, max (empty = API default 'high'). Controls thinking depth and token usage.", "is_secret": False},
    # Batch Processing
    {"key": "batch_default_size", "value": "50", "value_type": "int", "category": "ai", "description": "Default number of tenders per batch (Claude Batches API, 50% cost discount)", "is_secret": False},
    {"key": "batch_auto_poll", "value": "true", "value_type": "bool", "category": "ai", "description": "Auto-poll active batches for completion (background task)", "is_secret": False},
    # Cost-control kill-switches (Phase 3a) — flip to revert if quality regresses
    {"key": "agent_models_tier", "value": "haiku", "value_type": "string", "category": "ai", "description": "Cost tier for extractive agents that have NO model set in Agent Builder (deep_analyzer, checklist_generator, annexure_finder, proposal_router): 'haiku' uses claude-haiku-4-5, 'sonnet' uses claude-sonnet-5. Since the two-model rollout every one of those agents is rostered onto Haiku or Luna explicitly, so this only affects an agent added later with no roster entry. An explicit model in Agent Builder always wins.", "is_secret": False},
    {"key": "agent_model_allocation_version", "value": "", "value_type": "string", "category": "ai", "description": "Internal rollout marker for the per-agent model roster. Updated automatically; do not edit.", "is_secret": False},
    {"key": "costing_thinking_enabled", "value": "false", "value_type": "bool", "category": "ai", "description": "Enable extended thinking on the Costing Researcher. Default off (saves ~$0.10-0.15/run). Flip to true if costing quality drops on edge-case tenders that need exploratory reasoning.", "is_secret": False},
    # Command Center bridge — route specialized agents through execute_agent()
    {"key": "confirm_gate_scope", "value": "all_writes", "value_type": "str", "category": "ai", "description": "Which agent writes need the user's confirmation. 'all_writes' (shipped default): every write confirms. 'destructive_only': ordinary writes run uninterrupted and are audit-logged; only destructive actions (workspace reset, document finalize) raise the confirmation card. Deliberately ships conservative — loosening the gate is an owner's explicit choice made here, never a deploy side effect. Read live per call.", "is_secret": False},
    {"key": "chat_engine", "value": "master", "value_type": "str", "category": "ai", "description": "Who answers Command Center chat. 'master' (default) sends every message to the Master Agent — full platform reach, capability manual, delegation doctrine. 'router' restores the classify-then-dispatch path. Read live; flipping it needs no deploy. The kill switch exists because 'master' puts the flagship model on every message.", "is_secret": False},
    {"key": "command_center_use_execute_agent", "value": "true", "value_type": "bool", "category": "ai", "description": "When true, Command Center invokes specialized agents (costing_researcher, deep_analyzer, etc.) through the standard execute_agent() entrypoint — same path Agent Builder Test uses. Creates AgentExecution rows for monitoring and surfaces the agent's raw output without canned 'scope clarification' fallbacks. Flip to false to revert to the legacy chat_* path if quality regresses.", "is_secret": False},
    {"key": "costing_auto_chain_analyzer", "value": "true", "value_type": "bool", "category": "ai", "description": "When true, costing requests on a tender that hasn't been analyzed yet auto-run the deep_analyzer first to produce a structured TenderAnalysisSummary, then feed that summary into costing. Solves the multi-PDF input-too-long problem (analyzer output is ~5K chars vs raw PDFs at 200K+). Adds ~15s on the first costing request per tender; subsequent requests reuse the cached analysis. Flip to false to disable auto-chain.", "is_secret": False},
    {"key": "costing_auto_parse_boq", "value": "true", "value_type": "bool", "category": "ai", "description": "When true, a costing request on a tender whose NIT bidding schedule hasn't been parsed yet auto-extracts it (lightweight pdfplumber + vision BOQ parse — NOT the full tender analyzer) before costing, so you get a 1:1 NIT-mirror cost breakdown directly from the uploaded NIT without running tender analysis. ~5-15s on the first costing per tender. Flip to false to revert to freeform PDF costing.", "is_secret": False},
    # Router thinking — extended-thinking on the intent classifier (Layer 3)
    {"key": "router_thinking_mode", "value": "auto", "value_type": "string", "category": "ai", "description": "Thinking mode for the intent-classifier router: 'auto' (default — adaptive on 4.6/4.7, off on older), 'enabled', 'disabled'. Adaptive thinking improves routing on ambiguous / multi-step requests at +~3s latency.", "is_secret": False},
    # Context Management
    {"key": "prompt_caching_enabled", "value": "true", "value_type": "bool", "category": "ai", "description": "Enable prompt caching (90% cost reduction on cache hits). Recommended for all production use.", "is_secret": False},
    {"key": "cache_ttl", "value": "5m", "value_type": "string", "category": "ai", "description": "Cache TTL: '5m' (default, 1.25x write cost) or '1h' (2x write cost, better for infrequent calls)", "is_secret": False},
    {"key": "compaction_enabled", "value": "false", "value_type": "bool", "category": "ai", "description": "Enable server-side compaction for long conversations (beta, Claude 4.6 only). Auto-summarizes when context limit approached.", "is_secret": False},
    {"key": "compaction_trigger_tokens", "value": "150000", "value_type": "int", "category": "ai", "description": "Token threshold to trigger compaction (min 50,000)", "is_secret": False},
    {"key": "compaction_instructions", "value": "", "value_type": "string", "category": "ai", "description": "Custom compaction summarization prompt (empty = use default). Replaces default entirely when set.", "is_secret": False},
    {"key": "tool_clearing_enabled", "value": "false", "value_type": "bool", "category": "ai", "description": "Enable tool result clearing in agent workflows (beta). Clears old tool results when context grows.", "is_secret": False},
    {"key": "tool_clearing_trigger_tokens", "value": "100000", "value_type": "int", "category": "ai", "description": "Token threshold to trigger tool result clearing", "is_secret": False},
    {"key": "tool_clearing_keep_uses", "value": "5", "value_type": "int", "category": "ai", "description": "Number of recent tool uses to keep after clearing", "is_secret": False},
    {"key": "thinking_clearing_enabled", "value": "false", "value_type": "bool", "category": "ai", "description": "Enable thinking block clearing (beta). Manages extended thinking blocks in long conversations.", "is_secret": False},
    # General Settings
    {"key": "database_url", "value": "", "value_type": "string", "category": "general", "description": "Neon PostgreSQL connection string (read-only display — change in .env, requires app restart)", "is_secret": True},
    {"key": "platform_name", "value": "DRPL Tender Intelligence", "value_type": "string", "category": "general", "description": "Platform display name", "is_secret": False},
    {"key": "jwt_expiry_hours", "value": "720", "value_type": "int", "category": "general", "description": "JWT token expiry in hours", "is_secret": False},
    {"key": "max_upload_size_mb", "value": "50", "value_type": "int", "category": "general", "description": "Maximum file upload size in MB", "is_secret": False},
    {"key": "scrape_interval_minutes", "value": "360", "value_type": "int", "category": "general", "description": "Default scrape interval for extension (minutes)", "is_secret": False},
    # Security Settings
    {"key": "ai_send_tender_description", "value": "true", "value_type": "bool", "category": "security", "description": "Send tender descriptions to AI", "is_secret": False},
    {"key": "ai_send_document_content", "value": "true", "value_type": "bool", "category": "security", "description": "Send document content to AI", "is_secret": False},
    {"key": "ai_send_financial_data", "value": "false", "value_type": "bool", "category": "security", "description": "Send financial data (EMD, values) to AI", "is_secret": False},
    # Notifications
    {"key": "enable_notifications", "value": "true", "value_type": "bool", "category": "notifications", "description": "Enable platform notifications", "is_secret": False},
    # Notifications — Resend (email delivery)
    {"key": "resend_api_key", "value": "", "value_type": "string", "category": "notifications", "description": "Resend API key — when empty, email sending is a no-op (in-app notifications still fire). Get one at https://resend.com/api-keys", "is_secret": True},
    {"key": "resend_from_email", "value": "DRPL Platform <notifications@drpl.local>", "value_type": "string", "category": "notifications", "description": "From address for transactional emails. Must be on a Resend-verified domain in production.", "is_secret": False},
    {"key": "notifications_digest_hour_utc", "value": "13", "value_type": "int", "category": "notifications", "description": "Hour of day (UTC, 0-23) at which the daily digest email runs. 13 UTC ≈ 18:30 IST.", "is_secret": False},
    {"key": "notifications_closing_date_warning_hours", "value": "48", "value_type": "int", "category": "notifications", "description": "How far in advance to warn assigned users about a tender's closing date.", "is_secret": False},
    {"key": "notifications_app_base_url", "value": "http://localhost:5173", "value_type": "string", "category": "notifications", "description": "Public URL of the SPA, used to build action links inside email bodies. In production: https://app.yourdomain.com", "is_secret": False},
    # Costing — org-wide defaults applied by the costing agent and renderer
    {"key": "costing.default_overhead_percent", "value": "10.0", "value_type": "float", "category": "costing", "description": "Default overhead & contingencies % applied to subtotal (typical: 8–12% for site overheads + admin)", "is_secret": False},
    {"key": "costing.default_margin_percent", "value": "15.0", "value_type": "float", "category": "costing", "description": "Default contractor profit margin % applied after overhead (typical: 10–20% for government tenders)", "is_secret": False},
    {"key": "costing.default_gst_percent", "value": "18.0", "value_type": "float", "category": "costing", "description": "Default GST % applied to pre-tax total (18% for most works contracts; 12% for some service categories)", "is_secret": False},
    # Costing — large-schedule (50–500 line) batched costing controls
    {"key": "costing.batch_size", "value": "60", "value_type": "int", "category": "costing", "description": "Line items per LLM costing pass AND the single-vs-batched threshold. Tenders with more BOQ rows than this are costed in batches of this size (deterministic skeleton built first, then rates filled batch-by-batch). Smaller tenders use the single-call path. Sized for what the worker-tier model comfortably emits in one pass (~60 fully-costed lines on claude-sonnet-5).", "is_secret": False},
    {"key": "costing.max_line_items", "value": "2000", "value_type": "int", "category": "costing", "description": "Safety ceiling on how many BOQ line items the costing pipeline will auto-cost for one tender. This is a guardrail against pathological extractions (thousands of spurious rows), NOT a normal operating limit — set well above any real tender (500+ line NITs cost fully). When it trips it is surfaced loudly (reliability event + an assumptions note on the breakdown), never a silent slice. Raise further only if you have legitimately larger schedules; it bounds worst-case cost/latency.", "is_secret": False},
    {"key": "costing.batch_concurrency", "value": "4", "value_type": "int", "category": "costing", "description": "How many costing batches (of costing.batch_size rows) are researched at the same time. Batches used to run one after another: four batches of 60 took ~29 min and the worker job is killed at 30. With 4, a ~190-row tender is one wave. The costing's time budget is per wave, capped below the job and Master Agent limits. 1 = sequential. Max 6 (each batch holds a pooled DB connection for its whole run).", "is_secret": False},
    {"key": "costing.costing_max_sweeps", "value": "2", "value_type": "int", "category": "costing", "description": "After the main batched costing pass, how many bounded re-cost SWEEPS to run over rows still marked 'needs input' (rows a batch failed/truncated on, or whose item_code the agent mangled). Each sweep re-prices only the leftover rows and is idempotent. 0 disables sweeps. Higher = more thorough on flaky runs, at extra LLM cost. The batched run timeout scales by (1 + this) so long tenders aren't cut off mid-sweep.", "is_secret": False},
    {"key": "costing.model_skips_reference_rows", "value": "true", "value_type": "bool", "category": "costing", "description": "When the firm has no rate data of its own for a costing (no costing training data, no rate notes a person wrote into memory, no rate-card match for the row), schedule rows that print a published rate are not sent to the costing agent's batches: the platform's own cost build-up (costing.platform_buildup) prices them, from the schedule banner, verified wages and current material prices, and a verified market price still wins. false = send every row to the costing agent as before.", "is_secret": False},
    {"key": "costing.platform_buildup", "value": "true", "value_type": "bool", "category": "costing", "description": "The platform builds up its own cost for every schedule row that prints a published rate and has neither the firm's rate data nor a verified market price: what one unit is, its materials at verified current prices, labour hours priced at the central minimum wages plus statutory costs, consumables and transport -- summed by the platform, never taken from the railway's rate. A build-up more than 2x from the railway's figure is re-derived once and the nearer of the two stands. false = such rows take the railway's estimate less overhead and margin (the rates Sahil reported as 'the same railway rate').", "is_secret": False},
    {"key": "costing.buildup_model", "value": "claude-opus-5", "value_type": "string", "category": "costing", "description": "Model for the platform's cost build-up (costing.platform_buildup) and its rate-basis research. It decides the quantities, weights and hours behind every such row, so it is the strongest model rather than the cheapest; a 286-row NIT is about forty calls.", "is_secret": False},
    {"key": "costing.buildup_concurrency", "value": "8", "value_type": "int", "category": "costing", "description": "How many cost build-up calls run at once (1-12). Each call builds up a few rows of one schedule; none holds a database connection while it waits on the model.", "is_secret": False},
    {"key": "costing.buildup_rows_per_call", "value": "8", "value_type": "int", "category": "costing", "description": "Rows per cost build-up call (1-20), always from one schedule so the call reads one banner. Fewer rows = more attention per row and more calls.", "is_secret": False},
    {"key": "costing.buildup_second_look", "value": "true", "value_type": "bool", "category": "costing", "description": "A build-up more than 2x above or below the railway's figure (its rate less overhead and margin, and less GST where the schedule banner says the rate includes it) is re-derived once from scratch, told only which direction it is off; of the two, the one nearer the railway's figure stands. Both are the platform's own work -- the railway's number is never the figure.", "is_secret": False},
    {"key": "costing.labour_statutory_loading_pct", "value": "30", "value_type": "float", "category": "costing", "description": "Statutory and leave costs added to the minimum daily wage to price an hour of labour in the platform's cost build-up: employer PF (13%), ESI (3.25%), bonus (8.33%) and paid leave/holidays (~6%). The wage itself is the central minimum wage for the work's area, researched per costing.", "is_secret": False},
    {"key": "costing.market_research_cache_days", "value": "14", "value_type": "float", "category": "costing", "description": "Per-row market-price research (one Claude web-search call per market-priceable row) is reused for an item with the same description and unit researched within this many days, found or not, instead of searching again. A re-cost of the same tender otherwise paid the whole research bill twice. 0 = always search afresh.", "is_secret": False},
    {"key": "costing.nit_single_sheet", "value": "false", "value_type": "bool", "category": "costing", "description": "Default layout for the NIT-mirror XLSX export when the request doesn't specify. false = one sheet per schedule + Summary (legacy). true = all schedules stacked in a single sheet/tab that mirrors the NIT exactly. The 'Download single-sheet XLSX' button always requests single-sheet regardless of this default.", "is_secret": False},
    {"key": "costing.boq_pages_per_chunk", "value": "4", "value_type": "int", "category": "costing", "description": "Pages per LLM call when extracting the NIT bidding schedule in the page-range fallback (schedule-aware chunking is preferred). Smaller = more calls but safer against output-token truncation on dense schedules. 4 keeps each call's JSON output well under the model ceiling.", "is_secret": False},
    {"key": "costing.boq_min_rows_per_page", "value": "4", "value_type": "int", "category": "costing", "description": "Legacy completeness heuristic for NIT schedule extraction (kept as an OR-trigger). If the captured BOQ row count is below (NIT page count × this value), the extraction is treated as incomplete — the full chunked AI extractor runs (or re-runs with force) so all line items are captured. Raise if NITs are sparse; lower if many non-schedule pages. Superseded as the primary trigger by costing.boq_force_ai_extraction + schedule sub-total reconciliation.", "is_secret": False},
    {"key": "costing.boq_force_ai_extraction", "value": "true", "value_type": "bool", "category": "costing", "description": "When true, ALWAYS run the chunked AI extractor for NIT-class docs (pdfplumber becomes a fast supplement, unioned in). Stops the partial-pdfplumber leak where the simple service schedules (A1–A6) are captured but the dense spares schedules (A7/B7/C7, 50–138 items each) are silently dropped. Adds ~ceil(pages/chunk) extra LLM calls per tender even when pdfplumber succeeded — set false to revert to pdfplumber-first + heuristic fallback on clean-table tenants.", "is_secret": False},
    {"key": "costing.boq_reconcile_tolerance_pct", "value": "2.0", "value_type": "float", "category": "costing", "description": "Allowed gap (percent) between the sum of extracted line-item values for a schedule and the schedule sub-total the NIT prints (e.g. 'Schedule A = 3604012.65'). A schedule whose extracted sum is short beyond this tolerance (or that has 0 extracted rows) is flagged unreconciled and re-extracted (see costing.boq_reconcile_max_passes). The reconciliation is the hard 'did we capture every row' signal.", "is_secret": False},
    {"key": "costing.boq_reconcile_max_passes", "value": "2", "value_type": "int", "category": "costing", "description": "How many targeted re-extraction passes to run per unreconciled schedule (scoped to just that schedule's page range, at a smaller chunk size so dense schedules don't overflow output tokens). Bounds the work so a genuinely mis-priced NIT can't loop forever; schedules still short after this are flagged low extraction_confidence.", "is_secret": False},
    {"key": "costing.boq_chunk_max_retries", "value": "2", "value_type": "int", "category": "costing", "description": "Per-chunk retry budget in the AI BOQ extractor. On an LLM-call error OR an empty parse when the chunk text plausibly contains schedule rows, the chunk is retried by HALVING its page range (split in two) — truncated JSON is almost always output-token overflow on a too-dense chunk. After this budget is spent the chunk's page range is logged (not dropped silently) and its schedule flagged low confidence.", "is_secret": False},
    {"key": "costing.boq_vision_reconcile_fallback", "value": "true", "value_type": "bool", "category": "costing", "description": "Last-resort: for a schedule still unreconciled after the text-based re-extraction passes (likely a genuinely scanned/image schedule), do a final extraction pass forcing Claude vision on that schedule's page range. Most expensive per page, so gated to only unreconciled schedules. Set false to skip vision entirely.", "is_secret": False},
    {"key": "tender_analyzer.reuse_document_results", "value": "true", "value_type": "bool", "category": "ai", "description": "Reuse document-level results when a PDF has not changed: the per-document vision read (same bytes, model and prompt), the annexure discovery pass, and a chat request to analyze a tender whose stored analysis was built from exactly its current documents. Nothing is re-read until a document changes or the user asks to re-analyze. Set false to read every document on every run.", "is_secret": False},
    {"key": "tender_analyzer.chat_uploads_per_doc", "value": "true", "value_type": "bool", "category": "ai", "description": "PDFs attached in the Command Center are read one document at a time by the per-document extractor (cached by their bytes, so the same PDF is never read twice) and the usual 7-section report is written from those extracts. This works for tenders over the 100-page single-request limit, leaves per-document records the schedule parser and costing reuse, and fills in an auto-created tender's reference and value. false = send all attached PDFs in one call, as before (that path remains the fallback either way).", "is_secret": False},
    {"key": "tender_analyzer.chat_upload_parallel", "value": "3", "value_type": "int", "category": "ai", "description": "How many attached PDFs are read at once when chat uploads are analysed one document at a time (1-4). Reads also share a 64 MB budget of PDF bytes in flight, so large files run alone. 1 = one after another.", "is_secret": False},
    {"key": "costing.single_call_pdf_max_chars", "value": "120000", "value_type": "int", "category": "costing", "description": "Max characters of tender PDF text injected into the SINGLE-CALL costing path (the freeform fallback used only when no structured NIT schedule could be captured). The old 30000 (~7.5K tokens) truncated mid-schedule on large NITs, so the agent never saw the dense spares rows and emitted a single 'summary' line instead. 120000 (~30K tokens) fits a full ~341-row schedule; the downstream context-budget trim still protects pathological multi-100-page tenders.", "is_secret": False},
]


def _deserialize(value: str, value_type: str) -> Any:
    """Deserialize a stored string value to its Python type."""
    if value_type == "int":
        return int(value)
    elif value_type == "float":
        return float(value)
    elif value_type == "bool":
        return value.lower() in ("true", "1", "yes")
    elif value_type == "json":
        return json.loads(value)
    return value


def _serialize(value: Any) -> str:
    """Serialize a Python value to a storable string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def seed_defaults(db: Session):
    """Seed missing settings, and keep existing rows' descriptions current.

    Values on existing rows are left alone — those are an admin's choices, and
    only `_KNOWN_STALE_VALUES` may forward-migrate one. Descriptions are the
    opposite: they are documentation, nobody edits them through the UI, and a
    stale one actively misleads. `agent_models_tier` still described switching
    between "Haiku 4.5" and "Sonnet 4.6" long after both the models and the
    mechanism had changed, which is exactly the sentence an admin reads before
    deciding whether to touch the setting.
    """
    import logging
    logger = logging.getLogger(__name__)

    refreshed = 0
    for default in DEFAULT_SETTINGS:
        existing = db.query(PlatformSetting).filter(PlatformSetting.key == default["key"]).first()
        if not existing:
            setting = PlatformSetting(
                key=default["key"],
                value=default["value"],
                value_type=default["value_type"],
                category=default["category"],
                description=default["description"],
                is_secret=default.get("is_secret", False),
            )
            db.add(setting)
            continue

        if (existing.description or "") != (default["description"] or ""):
            existing.description = default["description"]
            db.add(existing)
            refreshed += 1

    db.commit()
    if refreshed:
        logger.info("[settings] refreshed %d stale setting description(s).", refreshed)
    _forward_migrate_known_stale(db)


# Settings whose values were updated in a later release and where the old
# value is *known* to be wrong / stale. Each entry: stale → new. Applied
# idempotently at startup so users on an upgraded backend don't keep using
# old model snapshots silently.
_KNOWN_STALE_VALUES = {
    # ── Model settings (2026-09-02 refresh) ──
    # PlatformSetting rows override the config defaults in `_get_effective_model`,
    # so a stale row here silently undoes a model upgrade no matter what
    # `config.py` says. Two were doing exactly that: `ai_model` sat on the
    # cheapest tier and `gemini_search_model` on a superseded Gemini, both
    # winning over the current defaults.
    "ai_model": {
        # Previous defaults, all superseded.
        "claude-sonnet-4-5-20250929": "claude-haiku-4-5",
        "claude-sonnet-4-5": "claude-haiku-4-5",
        "claude-sonnet-4-6": "claude-haiku-4-5",
        # The two-model rollout. This row is the one that matters most: a
        # PlatformSetting beats the config default in `_get_effective_model`,
        # so a live row still saying sonnet-5 would keep every unconfigured
        # agent on Sonnet no matter what `config.py` was changed to. The
        # previous version of this map migrated in the opposite direction
        # (haiku -> sonnet), which would have undone the rollout on every
        # startup.
        "claude-sonnet-5": "claude-haiku-4-5",
        "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    },
    "advisor_model": {
        # The advisor must be at least as capable as the model asking for the
        # advice, or the API rejects the pair. It stays on the flagship tier
        # through the two-model rollout on purpose: a second opinion from the
        # same model that asked for it is not a second opinion. It is an
        # explicit, rarely-used escalation, not a per-run cost.
        "claude-opus-4-7": "claude-opus-5",
        "claude-opus-4-6": "claude-opus-5",
        "claude-opus-4-5-20251101": "claude-opus-5",
        "claude-opus-4-1-20250805": "claude-opus-5",
    },
    "pdf_vision_model": {
        # Date-suffixed IDs are stale copies; the bare ID is the current one.
        "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    },
    "gemini_search_model": {
        "gemini-2.5-pro": "gemini-3.7-flash",
        "gemini-2.5-flash": "gemini-3.7-flash",
        "gemini-2.0-flash": "gemini-3.7-flash",
    },
    "costing.max_line_items": {
        # 600 was the old hard cap that silently sliced off line items beyond it
        # for large tenders (500+ line NITs lost rows). Raised to 2000 as a
        # guardrail-only ceiling. Forward-migrate the old default so upgraded
        # backends don't keep silently capping. (A tenant who deliberately wants
        # a lower cap can set any non-600 value and it is preserved.)
        "600": "2000",
    },
}


def _forward_migrate_known_stale(db: Session):
    """Auto-update PlatformSetting values that we know are stale.

    Reads safely. Logs a WARNING per migration so the user sees what changed.
    """
    import logging
    logger = logging.getLogger(__name__)
    for key, mapping in _KNOWN_STALE_VALUES.items():
        try:
            row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
            if not row or not row.value:
                continue
            new_value = mapping.get(row.value.strip())
            if new_value and new_value != row.value:
                logger.warning(
                    f"[settings] forward-migration: PlatformSetting('{key}') "
                    f"was '{row.value}' (stale) → updating to '{new_value}'. "
                    f"This was likely silently downgrading agent calls."
                )
                row.value = new_value
                db.add(row)
        except Exception as e:
            logger.debug(f"[settings] forward-migrate '{key}' failed (non-fatal): {e}")
    db.commit()


def get_setting(db: Session, key: str) -> Optional[PlatformSetting]:
    """Get a single setting by key."""
    return db.query(PlatformSetting).filter(PlatformSetting.key == key).first()


def get_setting_value(db: Session, key: str, default: Any = None) -> Any:
    """Get a setting's deserialized value, with fallback."""
    setting = get_setting(db, key)
    if setting:
        return _deserialize(setting.value, setting.value_type)
    return default


def set_setting(db: Session, key: str, value: Any, updated_by: int) -> PlatformSetting:
    """Update a setting's value, creating the row if the key is a known default.

    Rows are created by `seed_defaults`, which runs at startup and on every
    settings GET -- so a setting added by a release exists only once something
    has read the list. Until then a save raised "Setting 'x' not found" and the
    route turned that into a 404, which reads as *this setting does not exist*
    about a setting that does. A master admin who deep-links to the page, or
    whose browser served a cached copy, hit exactly that.

    A key that is NOT in DEFAULT_SETTINGS still raises. That boundary is the
    point: this creates the rows the platform ships, it does not let the API
    invent arbitrary settings.
    """
    setting = get_setting(db, key)
    if not setting:
        default = next((d for d in DEFAULT_SETTINGS if d["key"] == key), None)
        if default is None:
            raise ValueError(f"Setting '{key}' not found")
        setting = PlatformSetting(
            key=default["key"],
            value=default["value"],
            value_type=default["value_type"],
            category=default["category"],
            description=default["description"],
            is_secret=default.get("is_secret", False),
        )
        db.add(setting)
        db.flush()
    setting.value = _serialize(value)
    setting.updated_by = updated_by
    setting.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(setting)
    return setting


def get_settings_by_category(db: Session, category: str) -> list[PlatformSetting]:
    """Get all settings in a category."""
    return db.query(PlatformSetting).filter(PlatformSetting.category == category).order_by(PlatformSetting.key).all()


def get_all_settings(db: Session) -> list[PlatformSetting]:
    """Get all settings."""
    return db.query(PlatformSetting).order_by(PlatformSetting.category, PlatformSetting.key).all()


def reset_setting(db: Session, key: str, updated_by: int) -> Optional[PlatformSetting]:
    """Reset a setting to its default value."""
    for default in DEFAULT_SETTINGS:
        if default["key"] == key:
            return set_setting(db, key, default["value"], updated_by)
    return None


def get_effective_setting(db: Session, key: str, fallback: Any = None) -> Any:
    """Get a setting value from DB, fall back to .env config."""
    value = get_setting_value(db, key)
    if value is not None:
        return value
    # Fall back to env config
    env_map = {
        "ai_provider": _env_settings.ai_provider,
        "ai_model": _env_settings.ai_model,
        "ai_batch_size": _env_settings.ai_batch_size,
        "jwt_expiry_hours": _env_settings.jwt_expiry_hours,
        "anthropic_api_key": _env_settings.anthropic_api_key,
        "openai_api_key": _env_settings.openai_api_key,
        "google_api_key": _env_settings.google_api_key,
        "tavily_api_key": _env_settings.tavily_api_key,
        "gemini_search_model": _env_settings.gemini_search_model,
        "voyage_api_key": _env_settings.voyage_api_key,
        "database_url": _env_settings.database_url,
        # Notification center — Resend + scheduler
        "resend_api_key": _env_settings.resend_api_key,
        "resend_from_email": _env_settings.resend_from_email,
        "notifications_digest_hour_utc": _env_settings.notifications_digest_hour_utc,
        "notifications_closing_date_warning_hours": _env_settings.notifications_closing_date_warning_hours,
        "notifications_app_base_url": _env_settings.notifications_app_base_url,
    }
    return env_map.get(key, fallback)
