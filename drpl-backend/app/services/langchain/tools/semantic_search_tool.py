"""
DRPL Backend - Semantic Search Tool
LangChain BaseTool for vector-based semantic search across embedded documents.
Falls back to keyword-based RAG if Voyage AI is not configured.
"""

import hashlib
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.core.redis_client import cache_get_json, cache_key, cache_set_json

logger = logging.getLogger(__name__)

# Decision-maker plans frequently fan out the same query across sibling
# agents within a single run; the embedding call is the bottleneck. 5-min
# TTL is short enough that a tender doc upload won't show stale results
# next session, and a no-op when Redis is off.
_SEMANTIC_SEARCH_CACHE_TTL_SECONDS = 300


def _build_semantic_cache_key(query: str, *, tender_id: Optional[int], top_k: int) -> str:
    """Stable cache key. Hash the query so long inputs don't bloat the key."""
    fingerprint = hashlib.sha1(
        f"{query}|{tender_id or 0}|{top_k}".encode("utf-8")
    ).hexdigest()
    return cache_key("semantic_search", fingerprint)


class SemanticSearchInput(BaseModel):
    """Input schema for the semantic search tool."""
    query: str = Field(description="The search query to find relevant document content")
    tender_id: Optional[int] = Field(default=None, description="Optional tender ID to scope the search to a specific tender's documents")
    top_k: int = Field(default=5, description="Number of top results to return (default 5)")


class SemanticSearchTool(BaseTool):
    """Search across all embedded tender documents and RAG corpus using semantic similarity."""

    name: str = "semantic_search"
    description: str = (
        "Search across all embedded tender documents and RAG corpus using semantic similarity. "
        "Returns the most relevant text chunks ranked by relevance score. "
        "Use this when you need to find specific information across large document collections, "
        "or when keyword search might miss relevant content due to different phrasing. "
        "Optionally filter by tender_id to scope search to one tender's documents."
    )
    args_schema: Type[BaseModel] = SemanticSearchInput
    db: Optional[Session] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _run(
        self,
        query: str,
        tender_id: Optional[int] = None,
        top_k: int = 5,
    ) -> str:
        """Execute semantic search across embedded documents."""
        cache_k = _build_semantic_cache_key(query, tender_id=tender_id, top_k=top_k)
        cached = cache_get_json(cache_k)
        if isinstance(cached, str) and cached:
            logger.debug(f"semantic_search cache hit: {query[:80]}")
            return cached

        try:
            from app.services.embedding_service import semantic_search

            results = semantic_search(
                db=self.db,
                query=query,
                top_k=top_k,
                tender_id=tender_id,
            )

            if not results:
                fallback = self._fallback_search(query, top_k)
                cache_set_json(cache_k, fallback, ttl=_SEMANTIC_SEARCH_CACHE_TTL_SECONDS)
                return fallback

            # Format results for the agent
            formatted = []
            for r in results:
                page_info = ""
                if r.get("metadata") and r["metadata"].get("page_num"):
                    page_info = f" [Page: {r['metadata']['page_num']}]"

                source = r.get("source_name", "unknown")
                score = r.get("score", 0)
                text = r.get("chunk_text", "")

                formatted.append(
                    f"[Score: {score}] [Source: {source}]{page_info}\n{text}"
                )

            output = "\n\n---\n\n".join(formatted)
            cache_set_json(cache_k, output, ttl=_SEMANTIC_SEARCH_CACHE_TTL_SECONDS)
            return output

        except ValueError as e:
            # Voyage AI not configured — fall back to keyword search
            logger.info(f"Semantic search unavailable ({e}), falling back to keyword search")
            return self._fallback_search(query, top_k)

        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return self._fallback_search(query, top_k)

    def _fallback_search(self, query: str, top_k: int) -> str:
        """Fall back to keyword-based RAG search."""
        try:
            from app.services.rag_service import retrieve_context
            result = retrieve_context(query, top_k=top_k)
            if result:
                return f"[Keyword search fallback - semantic search unavailable]\n\n{result}"
            return "No relevant documents found."
        except Exception as e:
            logger.error(f"Fallback keyword search also failed: {e}")
            return "No relevant documents found (both semantic and keyword search unavailable)."
