"""
DRPL LangChain Tool - Ratecard Lookup

Structured lookup against the ratecard tables (Cummins/Fleetguard price lists).
This is the PRIMARY rate source for bottom-up component build-up costing — the
agent should call it before falling back to training-data text search or the
web.

Three modes:
  - "expand_check": expand a B/C/D-check kit for an engine type into its full
        list of constituent spare parts (qty + rate + source). This is how an
        NIT scope item like "D-check on VTA 28L" becomes 80+ priced lines.
  - "part": exact lookup by Part No.
  - "fuzzy": description search when the Part No is unknown.
"""

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class RatecardLookupInput(BaseModel):
    """Input schema for the Ratecard Lookup tool."""
    mode: str = Field(
        ...,
        description=(
            "One of: 'expand_check' (get all parts of a B/C/D-check kit for an "
            "engine type), 'part' (exact Part No lookup), 'fuzzy' (search by "
            "description)."
        ),
    )
    engine_type: Optional[str] = Field(
        None,
        description="Engine type, e.g. 'VTA 28L' or 'NTA 855R'. Required for expand_check.",
    )
    check_level: Optional[str] = Field(
        None,
        description="Check level 'B', 'C', or 'D'. Required for expand_check.",
    )
    part_no: Optional[str] = Field(
        None, description="Exact Part No for mode='part'.",
    )
    query: Optional[str] = Field(
        None, description="Description keywords for mode='fuzzy'.",
    )
    include_optional: bool = Field(
        True,
        description="For expand_check: include optional/needs-basis spares (default True).",
    )
    max_results: int = Field(8, description="Max results for mode='fuzzy'.")


class RatecardLookupTool(BaseTool):
    """Look up exact part rates or expand a check-schedule kit from the structured ratecard."""
    name: str = "ratecard_lookup"
    description: str = (
        "PRIMARY rate source for component costing. Look up exact OEM/Fleetguard "
        "part rates from the structured ratecard, OR expand a check-schedule "
        "(B/C/D check) for an engine type into its constituent spare parts with "
        "qty + rate + source. Use mode='expand_check' with engine_type + "
        "check_level to get the full kit (e.g. 'D-check on VTA 28L'); mode='part' "
        "for an exact Part No; mode='fuzzy' to search by description. Always try "
        "this before web search."
    )
    args_schema: Type[BaseModel] = RatecardLookupInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(
        self,
        mode: str,
        engine_type: Optional[str] = None,
        check_level: Optional[str] = None,
        part_no: Optional[str] = None,
        query: Optional[str] = None,
        include_optional: bool = True,
        max_results: int = 8,
    ) -> str:
        if not self.db:
            return json.dumps({"error": "Database session not available"})

        try:
            from app.services import ratecard_ingest_service as svc

            mode = (mode or "").strip().lower()

            if mode == "expand_check":
                if not (engine_type and check_level):
                    return json.dumps({
                        "status": "bad_input",
                        "message": "expand_check requires engine_type and check_level",
                        "results": [],
                    })
                results = svc.expand_check_schedule(
                    self.db, engine_type, check_level, include_optional=include_optional
                )
            elif mode == "part":
                if not part_no:
                    return json.dumps({
                        "status": "bad_input",
                        "message": "part mode requires part_no",
                        "results": [],
                    })
                results = svc.lookup_by_part_no(self.db, part_no, engine_type=engine_type)
            elif mode == "fuzzy":
                if not query:
                    return json.dumps({
                        "status": "bad_input",
                        "message": "fuzzy mode requires query",
                        "results": [],
                    })
                results = svc.fuzzy_lookup(
                    self.db, query, engine_type=engine_type, max_results=max_results
                )
            else:
                return json.dumps({
                    "status": "bad_input",
                    "message": f"unknown mode '{mode}' (use expand_check | part | fuzzy)",
                    "results": [],
                })

            return json.dumps({
                "status": "found" if results else "not_found",
                "mode": mode,
                "engine_type": engine_type,
                "check_level": check_level,
                "result_count": len(results),
                "results": results,
            })

        except Exception as e:
            logger.error(f"Ratecard lookup failed: {e}")
            try:
                self.db.rollback()
            except Exception:
                pass
            return json.dumps({"error": f"Ratecard lookup failed: {str(e)}"})
