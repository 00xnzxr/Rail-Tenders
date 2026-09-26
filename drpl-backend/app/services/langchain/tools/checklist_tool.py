"""
DRPL LangChain Tool - Checklist Reader
Reads and returns the document checklist for a tender.
Wraps the existing checklist_service.
"""

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class ChecklistReaderInput(BaseModel):
    """Input schema for the Checklist Reader tool."""
    tender_id: int = Field(..., description="The tender ID to read checklist for")
    include_completion_stats: bool = Field(True, description="Include completion statistics")


class ChecklistReaderTool(BaseTool):
    """
    Read the document checklist for a tender, including which documents
    are required, optional, already uploaded, and pending.
    """
    name: str = "checklist_reader"
    description: str = (
        "Read the document submission checklist for a specific tender. "
        "Returns all required and optional documents with their upload status. "
        "Use this to understand what documents need to be prepared for a tender submission."
    )
    args_schema: Type[BaseModel] = ChecklistReaderInput

    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        tender_id: int,
        include_completion_stats: bool = True,
    ) -> str:
        if not self.db:
            return "Error: Database session not available"

        from app.models.checklist import ChecklistItem
        from app.services.checklist_service import get_checklist_completion

        items = self.db.query(ChecklistItem).filter(
            ChecklistItem.tender_id == tender_id
        ).order_by(ChecklistItem.display_order).all()

        if not items:
            return f"No checklist items found for tender ID {tender_id}. The checklist may not have been generated yet."

        result = {
            "tender_id": tender_id,
            "items": [
                {
                    "id": item.id,
                    "name": item.item_name,
                    "description": item.item_description,
                    "is_required": item.is_required,
                    "is_uploaded": item.is_uploaded,
                    "document_path": item.document_path,
                }
                for item in items
            ],
        }

        if include_completion_stats:
            stats = get_checklist_completion(self.db, tender_id)
            result["completion_stats"] = stats

        return json.dumps(result, default=str)
