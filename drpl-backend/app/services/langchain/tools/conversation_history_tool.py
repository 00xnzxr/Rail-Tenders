"""Search this conversation's own transcript.

The ambient window an agent carries is deliberately small — a dozen turns,
budgeted per turn — because it is paid for on every message. That is the right
default and the wrong answer to "what did I tell you about the escalation
clause an hour ago", which is precisely the request the platform kept failing.

`memory_retrieve` does not cover this: `AgentMemory` holds curated long-term
facts an agent chose to write down, not what was actually said. Until this tool
there was no way for any agent to read a turn that had fallen out of the
window, so it answered from what it could see and sounded confident about it.

The session is bound by the run, never passed by the model. An agent cannot
name another conversation to read, so there is no scope for it to reach a
transcript its caller does not own — the binding is the firewall here, not a
check the tool performs.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Characters of a matching turn returned per hit. Long enough to carry a
#: figure with the sentence that gives it meaning, short enough that a
#: ten-result search does not cost more than the answer it supports.
_EXCERPT_CHARS = 1200

#: Characters either side of a match when the turn is longer than the excerpt.
#: Returning the head of a 20,000-character analysis when the match is at
#: character 14,000 answers a different question than the one asked.
_MATCH_CONTEXT = 400

#: LIKE escape character, as its own constant so the doubled-backslash
#: soup of writing it inline cannot drift between the pattern and the
#: `escape=` argument that has to agree with it.
_LIKE_ESCAPE = '\\'


class ConversationHistorySearchInput(BaseModel):
    query: Optional[str] = Field(
        None,
        description=(
            "Words to look for in earlier turns. Leave empty to page back "
            "through the conversation in order instead of searching."
        ),
    )
    limit: int = Field(
        8, description="How many turns to return (1-25)."
    )
    before_id: Optional[int] = Field(
        None,
        description=(
            "Return only turns older than this turn id — pass `oldest_id` from "
            "a previous result to keep reading further back."
        ),
    )


class ConversationHistorySearchTool(BaseTool):
    """Read earlier turns of the current conversation."""

    name: str = "conversation_history_search"
    description: str = (
        "Search or page back through earlier turns of THIS conversation, "
        "including turns older than the ones you were given. Use it whenever "
        "the user refers to something said earlier that you cannot see — a "
        "figure, a decision, a document they named, a correction they made. "
        "Pass `query` to search, or leave it empty to read back in order. "
        "Answering from what you can see, when the user is pointing at "
        "something you cannot, is the mistake this exists to prevent."
    )
    args_schema: Type[BaseModel] = ConversationHistorySearchInput

    db: Optional[Session] = None
    router_session_id: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _excerpt(content: str, query: Optional[str]) -> tuple[str, bool]:
        """The part of a turn worth returning, and whether it is the whole turn."""
        if len(content) <= _EXCERPT_CHARS:
            return content, True

        start = 0
        if query:
            hit = content.lower().find(query.lower())
            if hit > _MATCH_CONTEXT:
                start = hit - _MATCH_CONTEXT

        end = start + _EXCERPT_CHARS
        piece = content[start:end]
        if start:
            piece = "[...] " + piece
        if end < len(content):
            piece = piece + " [...]"
        return piece, False

    def _search(
        self,
        query: Optional[str] = None,
        limit: int = 8,
        before_id: Optional[int] = None,
    ) -> str:
        if not self.db:
            return json.dumps(
                {"status": "failed", "error": "No database session available."}
            )
        if not self.router_session_id:
            return json.dumps({
                "status": "failed",
                "error": (
                    "No conversation is bound to this run, so there is no "
                    "history to read. Answer from what you were given, and say "
                    "you cannot see earlier turns rather than guessing at them."
                ),
            })

        from app.models.agent_memory import AgentConversationHistory

        limit = max(1, min(int(limit or 8), 25))
        q = (
            self.db.query(AgentConversationHistory)
            .filter(AgentConversationHistory.session_id == self.router_session_id)
        )
        if before_id:
            q = q.filter(AgentConversationHistory.id < int(before_id))
        if query:
            # Substring, not full-text: SQLite and Postgres both run this the
            # same way, and a transcript is small enough per session that the
            # scan is cheaper than a search index nothing else would use.
            #
            # `%` and `_` are LIKE wildcards, and a model searching for "50%
            # escalation" or "GST_rate" would otherwise get a pattern match
            # rather than the phrase it asked for.
            pattern = (
                query.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
                .replace("%", _LIKE_ESCAPE + "%")
                .replace("_", _LIKE_ESCAPE + "_")
            )
            q = q.filter(
                AgentConversationHistory.content.ilike(
                    f"%{pattern}%", escape=_LIKE_ESCAPE
                )
            )

        try:
            rows = (
                q.order_by(AgentConversationHistory.id.desc())
                .limit(limit)
                .all()
            )
        except Exception as e:
            logger.error("conversation_history_search failed: %s", e, exc_info=True)
            return json.dumps(
                {"status": "failed", "error": f"History search failed: {e}"}
            )

        rows = list(reversed(rows))
        turns = []
        for r in rows:
            excerpt, whole = self._excerpt(r.content or "", query)
            turns.append({
                "id": r.id,
                "role": r.role,
                "agent": r.routed_from or r.agent_key,
                "output_type": r.output_type,
                "at": r.created_at.isoformat() if r.created_at else None,
                "content": excerpt,
                "content_complete": whole,
                "total_chars": len(r.content or ""),
            })

        payload = {
            "status": "completed",
            "query": query,
            "turns_returned": len(turns),
            "turns": turns,
        }
        if turns:
            payload["oldest_id"] = turns[0]["id"]
            payload["read_further_back"] = (
                f"Pass before_id={turns[0]['id']} to continue reading backwards."
            )
        else:
            payload["note"] = (
                "Nothing in this conversation matched. Say so rather than "
                "inferring what was probably said."
            )
        return json.dumps(payload, default=str, ensure_ascii=False)

    def _run(
        self,
        query: Optional[str] = None,
        limit: int = 8,
        before_id: Optional[int] = None,
    ) -> str:
        return self._search(query, limit, before_id)

    async def _arun(
        self,
        query: Optional[str] = None,
        limit: int = 8,
        before_id: Optional[int] = None,
    ) -> str:
        return self._search(query, limit, before_id)
