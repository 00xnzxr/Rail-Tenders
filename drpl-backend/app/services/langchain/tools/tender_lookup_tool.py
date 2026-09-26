"""
DRPL LangChain Tool - Tender Lookup
Queries the tender database by ID, keyword, portal, or filters.
Wraps the existing Tender model queries.
"""

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class TenderLookupInput(BaseModel):
    """Input schema for the Tender Lookup tool."""
    tender_id: Optional[int] = Field(None, description="Look up a specific tender by its database ID")
    keyword: Optional[str] = Field(None, description="Search tenders by keyword in title or description")
    portal: Optional[str] = Field(None, description="Filter by portal name (e.g., GeM, IREPS, CPPP)")
    department: Optional[str] = Field(None, description="Filter by department name")
    max_results: int = Field(20, description="Maximum number of results to return")
    include_ai_analysis: bool = Field(True, description="Include AI analysis scores in results")


class TenderLookupTool(BaseTool):
    """
    Query the DRPL tender database to find tenders by ID, keyword, portal, or department.
    Returns tender metadata including title, description, dates, values, and AI analysis scores.
    """
    name: str = "tender_lookup"
    description: str = (
        "Search and retrieve tender information from the DRPL database. "
        "Can look up a specific tender by ID, or search by keyword, portal, or department. "
        "Returns tender metadata including title, description, dates, estimated values, "
        "and AI analysis scores (relevance, risk, category)."
    )
    args_schema: Type[BaseModel] = TenderLookupInput

    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        tender_id: Optional[int] = None,
        keyword: Optional[str] = None,
        portal: Optional[str] = None,
        department: Optional[str] = None,
        max_results: int = 20,
        include_ai_analysis: bool = True,
    ) -> str:
        if not self.db:
            return "Error: Database session not available"

        from app.models.tender import Tender

        query = self.db.query(Tender)

        # Specific tender lookup
        if tender_id:
            tender = query.filter(Tender.id == tender_id).first()
            if not tender:
                return f"Tender with ID {tender_id} not found"
            return json.dumps(self._tender_to_dict(tender, include_ai_analysis), default=str)

        # Search filters
        if keyword:
            query = query.filter(
                Tender.title.ilike(f"%{keyword}%") |
                Tender.description.ilike(f"%{keyword}%")
            )
        if portal:
            query = query.filter(Tender.portal.ilike(f"%{portal}%"))
        if department:
            query = query.filter(Tender.department.ilike(f"%{department}%"))

        tenders = query.order_by(Tender.created_at.desc()).limit(max_results).all()

        if not tenders:
            return "No tenders found matching the search criteria"

        results = [self._tender_to_dict(t, include_ai_analysis) for t in tenders]
        return json.dumps(results, default=str)

    def _tender_to_dict(self, tender, include_ai: bool = True) -> dict:
        """Convert a Tender object to a dictionary."""
        d = {
            "id": tender.id,
            "title": tender.title,
            "portal": tender.portal,
            "tender_id": tender.tender_id,
            "department": tender.department,
            "organisation": tender.organisation,
            "description": (tender.description or "")[:500],
            "estimated_value": tender.estimated_value,
            "currency": tender.currency,
            "emd_amount": tender.emd_amount,
            "opening_date": str(tender.opening_date) if tender.opening_date else None,
            "closing_date": str(tender.closing_date) if tender.closing_date else None,
            "status": tender.status,
        }
        if include_ai:
            d.update({
                "ai_category": tender.ai_category,
                "ai_relevance_score": tender.ai_relevance_score,
                "ai_risk_score": tender.ai_risk_score,
                "ai_summary": tender.ai_summary,
            })
        return d
