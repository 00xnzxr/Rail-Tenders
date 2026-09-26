"""
DRPL LangChain Tool - Memory Store & Retrieve
Provides long-term memory capabilities for agents across sessions.
"""

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# --- Memory Store Tool ---

class MemoryStoreInput(BaseModel):
    """Input schema for the Memory Store tool."""
    content: str = Field(..., description="The fact, learning, or preference to remember")
    memory_type: str = Field(
        "fact",
        description="Type: 'fact' (domain knowledge), 'preference' (user/org preference), "
                     "'learning' (pattern learned from feedback), 'decision' (past decision + outcome)",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Keywords for later retrieval (e.g., ['costing', 'GST', 'railway'])",
    )
    importance: float = Field(
        0.5,
        description="Importance score 0.0-1.0 (higher = more important to remember)",
    )
    context: Optional[str] = Field(
        None,
        description="What situation or analysis led to this memory",
    )
    tender_id: Optional[int] = Field(
        None,
        description="Associate this memory with a specific tender",
    )


class MemoryStoreTool(BaseTool):
    """
    Store a fact, learning, preference, or decision in long-term memory.
    Memories persist across sessions and can be retrieved by future agent runs.
    Use this to remember important patterns, rates, preferences, or lessons learned.
    """
    name: str = "memory_store"
    description: str = (
        "Store information in long-term memory for future use. "
        "Use this to remember important facts (e.g., 'Railway Zone X prefers format Y'), "
        "learned patterns (e.g., 'Cost estimates should include 18% GST'), "
        "preferences, or past decisions with outcomes. "
        "Provide keywords for easy retrieval later."
    )
    args_schema: Type[BaseModel] = MemoryStoreInput

    db: Optional[Session] = None
    agent_key: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        content: str,
        memory_type: str = "fact",
        keywords: list[str] = None,
        importance: float = 0.5,
        context: Optional[str] = None,
        tender_id: Optional[int] = None,
    ) -> str:
        if not self.db:
            return "Error: Database session not available"

        from app.services.langchain.memory_service import store_memory

        try:
            memory = store_memory(
                db=self.db,
                agent_key=self.agent_key,
                memory_type=memory_type,
                content=content,
                keywords=keywords or [],
                importance=importance,
                # Marked as captured from a run, so an admin's agent writes
                # are not mistaken for curated, shared knowledge.
                context=f"[agent] {context}" if context else "[agent] stored during a run",
                tender_id=tender_id,
            )
            return json.dumps({
                "status": "stored",
                "memory_id": memory.id,
                "memory_type": memory_type,
                "keywords": keywords or [],
            })
        except Exception as e:
            try:
                self.db.rollback()
            except Exception:
                pass
            return f"Error storing memory: {str(e)}"


# --- Memory Retrieve Tool ---

class MemoryRetrieveInput(BaseModel):
    """Input schema for the Memory Retrieve tool."""
    query: str = Field(..., description="Search query to find relevant memories")
    top_k: int = Field(10, description="Maximum number of memories to return")
    memory_type: Optional[str] = Field(
        None,
        description="Filter by type: 'fact', 'preference', 'learning', 'decision', or None for all",
    )
    tender_id: Optional[int] = Field(
        None,
        description="Filter memories associated with a specific tender",
    )


class MemoryRetrieveTool(BaseTool):
    """
    Retrieve relevant memories from long-term storage.
    Searches by keyword relevance, importance, and recency.
    Use this to recall past learnings, preferences, and domain knowledge.
    """
    name: str = "memory_retrieve"
    description: str = (
        "Search and retrieve information from long-term memory. "
        "Use this to recall past learnings, domain knowledge, preferences, "
        "and past decisions. Searches by keyword relevance and importance."
    )
    args_schema: Type[BaseModel] = MemoryRetrieveInput

    db: Optional[Session] = None
    agent_key: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        query: str,
        top_k: int = 10,
        memory_type: Optional[str] = None,
        tender_id: Optional[int] = None,
    ) -> str:
        if not self.db:
            return "Error: Database session not available"

        from app.services.langchain.memory_service import retrieve_memories

        try:
            memories = retrieve_memories(
                db=self.db,
                agent_key=self.agent_key,
                query=query,
                top_k=top_k,
                memory_type=memory_type,
                tender_id=tender_id,
            )

            if not memories:
                return f"No relevant memories found for: {query}"

            results = []
            for mem in memories:
                results.append({
                    "id": mem.id,
                    "type": mem.memory_type,
                    "content": mem.content,
                    "context": mem.context,
                    "keywords": mem.keywords,
                    "importance": mem.importance,
                    "created_at": str(mem.created_at),
                    "access_count": mem.access_count,
                })

            return json.dumps({
                "query": query,
                "count": len(results),
                "memories": results,
            }, default=str, indent=2)
        except Exception as e:
            try:
                self.db.rollback()
            except Exception:
                pass
            return f"Error retrieving memories: {str(e)}"
