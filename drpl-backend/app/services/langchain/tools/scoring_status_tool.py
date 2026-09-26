"""LangChain tool: report the tender-scoring backlog and optionally drain it.

Scoring only (ai_relevance_score). Returns compact JSON so agent token use
stays low — never dumps tender lists into the model context.
"""
import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.services.scoring_backlog_service import backlog_stats, drain_backlog

logger = logging.getLogger(__name__)


class ScoringStatusInput(BaseModel):
    action: str = Field(
        "status",
        description="'status' to report the backlog, 'drain' to score pending tenders",
    )
    mode: str = Field(
        "live",
        description="Drain mode when action='drain': 'live' (fast, full price) or 'batch' (cheap, async)",
    )
    limit: Optional[int] = Field(None, description="Optional cap on how many tenders to drain")


class ScoringStatusTool(BaseTool):
    name: str = "tender_scoring_status"
    description: str = (
        "Report how many tenders are scored vs pending relevance scoring, and optionally "
        "trigger scoring of the pending backlog. Use action='status' to check the backlog, "
        "action='drain' to score pending tenders (mode='batch' is cheapest for large backlogs, "
        "mode='live' is immediate for a few)."
    )
    args_schema: Type[BaseModel] = ScoringStatusInput

    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, action: str = "status", mode: str = "live",
             limit: Optional[int] = None) -> str:
        if not self.db:
            return "Error: Database session not available"
        try:
            if action == "drain":
                return json.dumps(drain_backlog(self.db, mode=mode, limit=limit), default=str)
            return json.dumps(backlog_stats(self.db), default=str)
        except Exception as e:  # noqa: BLE001
            logger.warning("tender_scoring_status tool failed: %s", e)
            return json.dumps({"error": str(e)})
