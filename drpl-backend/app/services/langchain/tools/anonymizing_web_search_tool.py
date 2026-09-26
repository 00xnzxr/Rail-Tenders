"""
DRPL LangChain Tool - Anonymizing Web Search
Wraps WebSearchTool with a two-pass redaction layer that strips company names,
tender references, and DRPL identifiers before any query leaves the system.

Pass 1: regex-based PII redaction (existing redaction_service patterns)
Pass 2: entity-map substitution (built at agent startup from LLM extraction)
"""

import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class AnonymizingWebSearchInput(BaseModel):
    """Input schema for the Anonymizing Web Search tool."""
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


class AnonymizingWebSearchTool(BaseTool):
    """
    Search the web for costing information with automatic anonymization.

    Strips DRPL company names, client company names, tender reference numbers,
    and internal project codes from every query before the search is executed.
    This ensures no identifying information about the tendering organisation or
    the specific tender reaches external search providers.

    Internally delegates to the standard WebSearchTool after sanitising the query.
    Use this tool instead of web_search for all costing research.
    """
    name: str = "anonymizing_web_search"
    description: str = (
        "Search the web for market rates, DSR rates, material costs, labour rates, "
        "and other costing data. Automatically anonymizes all queries before searching "
        "— company names, tender references, and project codes are stripped. "
        "Use this for ALL external research; never use web_search directly. "
        "Returns a grounded summary with cited sources when available."
    )
    args_schema: Type[BaseModel] = AnonymizingWebSearchInput

    db: Optional[Session] = None
    # Mutable dict reference shared with CostingAgentState — populated at agent startup.
    # Keys are placeholder tokens (e.g. "[ENTITY-1]"), values are the original strings.
    # We apply reverse substitution: replace original strings with placeholders in queries.
    anonymization_map: dict = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        query: str,
        max_results: int = 5,
        allowed_domains: Optional[list[str]] = None,
        search_type: str = "general",
    ) -> str:
        from app.services.langchain.tools.web_search_tool import WebSearchTool
        from app.services.redaction_service import redact_costing_query

        sanitized = redact_costing_query(query, self.anonymization_map, self.db)

        if sanitized != query:
            logger.debug(
                "Query anonymized before web search. "
                f"Original length: {len(query)}, sanitized length: {len(sanitized)}"
            )

        inner = WebSearchTool(db=self.db)
        return inner._run(
            query=sanitized,
            max_results=max_results,
            allowed_domains=allowed_domains,
            search_type=search_type,
        )
