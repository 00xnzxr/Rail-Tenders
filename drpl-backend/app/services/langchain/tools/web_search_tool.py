"""
DRPL LangChain Tool - Web Search
Searches the web for market rates, tender information, and domain-specific research.
Uses Gemini Search Grounding as primary, Tavily as secondary, Claude web search
(the platform's Anthropic key) third, DuckDuckGo as the last fallback.
"""

import hashlib
import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.redis_client import cache_get_json, cache_key, cache_set_json

logger = logging.getLogger(__name__)
settings = get_settings()

# Same query often runs multiple times within a single agent plan and across
# sibling agents in a routed run. 5-minute TTL is short enough to stay fresh
# for market-rates / tender lookups; long enough to absorb the duplicate
# fan-out. No-ops when Redis is off.
_WEB_SEARCH_CACHE_TTL_SECONDS = 300


def _get_search_config(db: Optional[Session] = None) -> dict:
    """
    Resolve search API keys from platform settings (DB) with .env fallback.
    Priority: Platform Setting → .env → empty string
    """
    config = {
        "google_api_key": settings.google_api_key,
        "gemini_search_model": settings.gemini_search_model,
        "tavily_api_key": settings.tavily_api_key,
    }

    if db:
        try:
            from app.models.platform_setting import PlatformSetting
            for key in ["google_api_key", "tavily_api_key", "gemini_search_model"]:
                setting = db.query(PlatformSetting).filter(
                    PlatformSetting.key == key
                ).first()
                if setting and setting.value and setting.value.strip() and setting.value != "••••••••":
                    config[key] = setting.value
        except Exception as e:
            logger.debug(f"Could not read platform settings for search config: {e}")

    return config

def _build_search_cache_key(
    namespace: str,
    query: str,
    *,
    max_results: int,
    allowed_domains: Optional[list[str]],
) -> str:
    """Stable cache key for a search call. Hash the query so long/odd inputs
    don't blow up Redis key length, and include the args that change results."""
    domains_part = ",".join(sorted(allowed_domains)) if allowed_domains else ""
    fingerprint = hashlib.sha1(
        f"{query}|{max_results}|{domains_part}".encode("utf-8")
    ).hexdigest()
    return cache_key(namespace, fingerprint)


# Default domains relevant to Indian government tenders
DEFAULT_TENDER_DOMAINS = [
    "gem.gov.in",
    "ireps.gov.in",
    "cppp.nic.in",
    "eprocure.gov.in",
    "indianrailways.gov.in",
]


class WebSearchInput(BaseModel):
    """Input schema for the Web Search tool."""
    query: str = Field(..., description="Search query string")
    max_results: int = Field(5, description="Maximum number of results to return")
    allowed_domains: Optional[list[str]] = Field(
        None,
        description=(
            "Restrict search to specific domains (e.g., ['gem.gov.in', 'ireps.gov.in']). "
            "Leave empty for unrestricted search."
        ),
    )
    search_type: str = Field(
        "general",
        description="Type of search: 'general', 'tender', 'market_rates', 'technical'",
    )


class WebSearchTool(BaseTool):
    """
    Search the web for information relevant to tender analysis and proposal preparation.
    Supports domain-targeted search for Indian government procurement portals.
    Useful for researching market rates, competitor pricing, technical specifications,
    and tender-specific requirements.

    Uses a 4-tier provider chain:
    1. Google Gemini Search Grounding (best quality — grounded, cited results)
    2. Tavily API (good for AI agents — structured snippets)
    3. Claude web search (grounded, cited; on the Anthropic key)
    4. DuckDuckGo (free fallback — no API key needed)
    """
    name: str = "web_search"
    description: str = (
        "Search the web for information. Useful for researching market rates, DSR rates, "
        "competitor pricing, technical specifications, government procurement rules, "
        "and tender-specific requirements. Supports domain filtering to target specific "
        "sites like gem.gov.in, ireps.gov.in, cppp.nic.in. "
        "Returns a grounded summary with cited sources when available."
    )
    args_schema: Type[BaseModel] = WebSearchInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        query: str,
        max_results: int = 5,
        allowed_domains: Optional[list[str]] = None,
        search_type: str = "general",
    ) -> str:
        # Personal identifiers (PAN, GSTIN, IFSC, phone, Aadhaar, bank account)
        # never leave in a search query, whichever agent wrote it. The costing
        # path's anonymizing wrapper also swaps the tender's names; this is the
        # floor under every other caller.
        try:
            from app.services.redaction_service import redact_text
            query = redact_text(query, self.db)
        except Exception:
            pass

        # Enhance query based on search type
        enhanced_query = self._enhance_query(query, search_type)

        # Cache lookup — keyed on the post-enhanced query plus the args that
        # actually change provider behaviour. Short TTL keeps results fresh.
        cache_k = _build_search_cache_key(
            "web_search",
            enhanced_query,
            max_results=max_results,
            allowed_domains=allowed_domains,
        )
        cached = cache_get_json(cache_k)
        if cached is not None and isinstance(cached, str) and cached:
            logger.debug(f"web_search cache hit: {enhanced_query[:80]}")
            return cached

        # Resolve API keys from platform settings (DB-first) with .env fallback
        search_config = _get_search_config(self.db)

        # Priority 1: Gemini Search Grounding (best quality — grounded + cited)
        if search_config["google_api_key"]:
            try:
                result = self._search_gemini(enhanced_query, max_results, allowed_domains, search_config)
                cache_set_json(cache_k, result, ttl=_WEB_SEARCH_CACHE_TTL_SECONDS)
                return result
            except Exception as e:
                logger.warning(f"Gemini search failed, falling back to Tavily: {e}")

        # Priority 2: Tavily (good for AI agents)
        if search_config["tavily_api_key"]:
            try:
                result = self._search_tavily(enhanced_query, max_results, allowed_domains, search_config)
                cache_set_json(cache_k, result, ttl=_WEB_SEARCH_CACHE_TTL_SECONDS)
                return result
            except Exception as e:
                logger.warning(f"Tavily search failed, falling back to DuckDuckGo: {e}")

        # Priority 3: Claude web search, on the platform's Anthropic key.
        # Without it a deployment with neither Gemini nor Tavily configured
        # fell to DuckDuckGo, whose library (duckduckgo_search, since renamed)
        # answers "4 sq mm cable price per metre" with dictionary entries for
        # "flexible": the costing agent then priced 286 rows with no market
        # evidence at all and guessed every one.
        anthropic_key = self._anthropic_key()
        if anthropic_key:
            try:
                result = self._search_anthropic(enhanced_query, max_results, allowed_domains, anthropic_key)
                cache_set_json(cache_k, result, ttl=_WEB_SEARCH_CACHE_TTL_SECONDS)
                return result
            except Exception as e:
                logger.warning(f"Claude web search failed, falling back to DuckDuckGo: {e}")

        # Priority 4: DuckDuckGo (free, no API key)
        try:
            result = self._search_duckduckgo(enhanced_query, max_results, allowed_domains)
            cache_set_json(cache_k, result, ttl=_WEB_SEARCH_CACHE_TTL_SECONDS)
            return result
        except Exception as e:
            logger.error(f"All search providers failed: {e}")
            return f"Web search failed: {str(e)}. Query was: {enhanced_query}"

    def _enhance_query(self, query: str, search_type: str) -> str:
        """Enhance the search query based on the type of search."""
        prefixes = {
            "tender": "India government tender procurement ",
            "market_rates": "current market rates pricing India ",
            "technical": "technical specifications standards India ",
        }
        prefix = prefixes.get(search_type, "")
        return f"{prefix}{query}".strip()

    # ── Gemini Search Grounding ────────────────────────────────────────────

    def _search_gemini(
        self,
        query: str,
        max_results: int,
        domains: Optional[list[str]],
        search_config: Optional[dict] = None,
    ) -> str:
        """
        Search using Google Gemini with Search Grounding.

        Gemini acts as a 'search oracle' — it searches the web, synthesizes a
        grounded answer with inline citations, and returns structured metadata
        including source URLs and citation mappings. The grounded summary and
        citations are then passed to Claude agents for reasoning.
        """
        from google import genai
        from google.genai import types

        cfg = search_config or _get_search_config(self.db)
        client = genai.Client(api_key=cfg["google_api_key"])

        # Build search query with domain hints (same approach as DuckDuckGo)
        search_query = query
        if domains:
            domain_hint = " OR ".join(f"site:{d}" for d in domains)
            search_query = f"{query} ({domain_hint})"

        # Configure Gemini with google_search grounding tool
        grounding_tool = types.Tool(google_search=types.GoogleSearch())
        config = types.GenerateContentConfig(tools=[grounding_tool])

        response = client.models.generate_content(
            model=cfg.get("gemini_search_model", "gemini-2.5-flash"),
            contents=search_query,
            config=config,
        )

        # Extract grounded text
        grounded_text = ""
        try:
            grounded_text = response.text or ""
        except Exception:
            # Some responses may not have .text if blocked
            grounded_text = ""

        # Extract grounding metadata from the response candidate
        sources = []
        citations = []
        search_queries_used = []

        candidate = response.candidates[0] if response.candidates else None
        grounding_meta = getattr(candidate, "grounding_metadata", None) if candidate else None

        if grounding_meta:
            # Extract web search queries that Gemini executed
            web_queries = getattr(grounding_meta, "web_search_queries", None)
            if web_queries:
                search_queries_used = list(web_queries)

            # Extract source chunks (URLs + titles)
            grounding_chunks = getattr(grounding_meta, "grounding_chunks", None)
            for chunk in (grounding_chunks or []):
                web = getattr(chunk, "web", None)
                if web:
                    sources.append({
                        "title": getattr(web, "title", "") or "",
                        "url": getattr(web, "uri", "") or "",
                    })

            # Extract grounding supports (text segments mapped to source indices)
            grounding_supports = getattr(grounding_meta, "grounding_supports", None)
            for support in (grounding_supports or []):
                segment = getattr(support, "segment", None)
                segment_text = getattr(segment, "text", "") if segment else ""
                chunk_indices = list(getattr(support, "grounding_chunk_indices", []) or [])
                if segment_text or chunk_indices:
                    citations.append({
                        "text": segment_text,
                        "source_indices": chunk_indices,
                    })

        # Build results in the standard format (backward-compatible with Tavily/DDG)
        results = []
        for i, src in enumerate(sources[:max_results]):
            # Find citation text that references this source
            relevant_text_parts = []
            for cite in citations:
                if i in cite.get("source_indices", []):
                    text = cite.get("text", "")
                    if text:
                        relevant_text_parts.append(text)

            relevant_text = " ".join(relevant_text_parts)
            results.append({
                "title": src["title"],
                "url": src["url"],
                # Structured provenance for costing lines (Piece 3): source_url is
                # the guaranteed link; oem_manufacturer is best-effort (the agent
                # infers the brand from title/content — the tool can't reliably).
                "source_url": src["url"],
                "oem_manufacturer": None,
                "content": relevant_text[:500] if relevant_text else "",
            })

        output = {
            "query": query,
            "provider": "gemini_search_grounding",
            "results_count": len(results),
            "grounded_summary": grounded_text[:3000],
            "results": results,
            "citations": citations,
            "search_queries": search_queries_used,
        }

        if not results and not grounded_text:
            return f"No results found for: {query}"

        return json.dumps(output, indent=2)

    # ── Claude web search ──────────────────────────────────────────────────

    def _anthropic_key(self) -> Optional[str]:
        try:
            from app.services.langchain.provider_config import get_api_key
            return get_api_key(self.db, "anthropic")
        except Exception:
            return settings.anthropic_api_key or None

    def _search_anthropic(
        self,
        query: str,
        max_results: int,
        domains: Optional[list[str]],
        api_key: str,
    ) -> str:
        """Search with Claude's server-side web search tool, the same "search
        oracle" shape as the Gemini path: a short answer that quotes prices
        with their source URLs, plus the result list. Located in India so
        prices come back in rupees from Indian sellers."""
        import time as _time

        import anthropic

        tool: dict = {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": max(1, int(settings.anthropic_search_max_uses or 3)),
            "user_location": {
                "type": "approximate", "country": "IN", "timezone": "Asia/Kolkata",
            },
        }
        if domains:
            tool["allowed_domains"] = list(domains)
        model = settings.anthropic_search_model or "claude-haiku-4-5"
        client = anthropic.Anthropic(api_key=api_key)
        started = _time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            tools=[tool],
            system=(
                "You are a web researcher. Search the web and answer from what you "
                "find. For a price question, quote each price found with its unit, "
                "date if shown, and the URL it came from; say plainly when no page "
                "gives a price for the exact item asked about, and never estimate "
                "one yourself."
            ),
            messages=[{"role": "user", "content": query}],
        )
        elapsed_ms = int((_time.monotonic() - started) * 1000)
        try:
            from app.services.ai_service import _log_usage
            from app.services.langchain.provider_config import web_search_requests_of
            _log_usage(
                None, "anthropic", model, "web_search",
                {"input_tokens": response.usage.input_tokens,
                 "output_tokens": response.usage.output_tokens,
                 "web_search_requests": web_search_requests_of(response.usage)},
                elapsed_ms, True,
            )
        except Exception:
            pass

        results: list[dict] = []
        answer: list[str] = []
        seen: set[str] = set()
        for block in response.content:
            if block.type == "web_search_tool_result":
                # A list on success; an error object otherwise (no exception).
                if isinstance(block.content, list):
                    for item in block.content:
                        url = getattr(item, "url", "") or ""
                        if url and url not in seen:
                            seen.add(url)
                            results.append({
                                "title": getattr(item, "title", "") or "",
                                "url": url,
                                "source_url": url,
                                "oem_manufacturer": None,
                                "content": f"page age: {getattr(item, 'page_age', None) or 'unknown'}",
                            })
            elif block.type == "text":
                answer.append(block.text)
        summary = "".join(answer).strip()
        if not results and not summary:
            return f"No results found for: {query}"
        return json.dumps({
            "query": query,
            "provider": "anthropic_web_search",
            "results_count": min(len(results), max_results),
            "grounded_summary": summary[:3000],
            "results": results[:max_results],
        }, indent=2, ensure_ascii=False)

    # ── Tavily ─────────────────────────────────────────────────────────────

    def _search_tavily(self, query: str, max_results: int, domains: Optional[list[str]], search_config: Optional[dict] = None) -> str:
        """Search using Tavily API."""
        from tavily import TavilyClient

        cfg = search_config or _get_search_config(self.db)
        client = TavilyClient(api_key=cfg["tavily_api_key"])

        search_kwargs = {
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
        }

        if domains:
            search_kwargs["include_domains"] = domains

        response = client.search(**search_kwargs)

        results = []
        for item in response.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source_url": item.get("url", ""),   # guaranteed link (Piece 3)
                "oem_manufacturer": None,            # best-effort — agent infers
                "content": item.get("content", "")[:500],
                "score": item.get("score", 0),
            })

        if not results:
            return f"No results found for: {query}"

        return json.dumps({
            "query": query,
            "provider": "tavily",
            "results_count": len(results),
            "results": results,
        }, indent=2)

    # ── DuckDuckGo ─────────────────────────────────────────────────────────

    def _search_duckduckgo(self, query: str, max_results: int, domains: Optional[list[str]]) -> str:
        """Search using DuckDuckGo (free, no API key needed)."""
        from duckduckgo_search import DDGS

        # Add site: filters for domain restriction
        if domains:
            domain_filter = " OR ".join(f"site:{d}" for d in domains)
            query = f"{query} ({domain_filter})"

        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "source_url": r.get("href", ""),   # guaranteed link (Piece 3)
                    "oem_manufacturer": None,          # best-effort — agent infers
                    "content": r.get("body", "")[:500],
                })

        if not results:
            return f"No results found for: {query}"

        return json.dumps({
            "query": query,
            "provider": "duckduckgo",
            "results_count": len(results),
            "results": results,
        }, indent=2)
