"""
DRPL LangChain Tool - Clarify

Lets an agent pause its run and ask the user a targeted follow-up question
instead of guessing. The tool persists a PendingClarification row keyed to
the current Command Center session; the streaming handler picks up the
persisted rows and emits an SSE ``clarification_request`` event so the
frontend can surface a toast + modal.

Because Phase C (LangGraph checkpointer + Redis) is not live yet, the agent
does not literally "resume" from the tool call. It returns a short marker
message to the LLM so the run ends cleanly; the user's answer is merged
back in as extra context on the next router turn via the resume endpoint.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class ClarifyInput(BaseModel):
    """Input schema for the clarify tool."""
    question: str = Field(
        ...,
        description=(
            "The single, specific question to ask the user. Keep it to one "
            "sentence. Only ask when the answer is truly required to continue "
            "and cannot be reasonably inferred from context."
        ),
    )
    options: Optional[list[str]] = Field(
        None,
        description=(
            "Optional short list of suggested answers the user can pick from. "
            "Use for closed-ended questions (e.g., GST rate choices)."
        ),
    )
    reason: Optional[str] = Field(
        None,
        description="One-line reason the agent needs this info (for the UI modal).",
    )


class ClarifyTool(BaseTool):
    """Pause the run and ask the user a clarifying question."""

    name: str = "clarify"
    description: str = (
        "Pause your work and ask the user ONE targeted clarification question. "
        "Call this ONLY when you genuinely cannot proceed without the answer "
        "(e.g., missing scope, ambiguous quantity, unknown preference) and the "
        "answer cannot be inferred from the tender, prior artifacts, or chat "
        "history. After calling, stop — the run will pause, the user will be "
        "prompted, and a new run will resume with the answer injected. Do NOT "
        "call this for stylistic preferences you can pick a sensible default for."
    )
    args_schema: Type[BaseModel] = ClarifyInput

    db: Optional[Session] = None
    session_id: Optional[int] = None            # ProposalSession.id (Command Center session)
    router_session_id: Optional[str] = None     # AgentConversationHistory.session_id
    agent_key: Optional[str] = None             # e.g. costing_researcher

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        question: str,
        options: Optional[list[str]] = None,
        reason: Optional[str] = None,
    ) -> str:
        if not self.db or not self.session_id:
            return json.dumps({
                "status": "error",
                "message": "Clarify tool not wired: missing db/session_id. Skipping.",
            })

        from app.models.clarification import PendingClarification

        try:
            row = PendingClarification(
                session_id=self.session_id,
                router_session_id=self.router_session_id,
                agent_key=self.agent_key or "unknown",
                question=question.strip(),
                options=options or None,
                context={"reason": reason} if reason else None,
                status="pending",
            )
            self.db.add(row)
            self.db.commit()
            self.db.refresh(row)
        except Exception as e:
            logger.error(f"clarify: failed to persist clarification: {e}", exc_info=True)
            try:
                self.db.rollback()
            except Exception:
                pass
            return json.dumps({"status": "error", "message": str(e)})

        return json.dumps({
            "status": "awaiting_user",
            "clarification_id": row.id,
            "question": row.question,
            "instruction": (
                "A clarification request has been queued for the user. Stop "
                "working now and return a brief message telling the user you "
                "are waiting for their answer. Do not invent the answer."
            ),
        })
