"""
DRPL LangChain Tools - Costing Training Retrieval & Travel Delegation

CostingTrainingRetrievalTool:
    On-demand keyword search over training datasets assigned to the costing_researcher
    agent. Use when you need to look up a specific rate, unit, or historical cost item
    without loading the entire training corpus.

DelegateToTravelAgentTool:
    Stable delegation interface for the future travel research sub-agent.
    Currently returns a structured stub response with IRCTC rate guidance.
    When the travel agent is built, only _run() changes — the input contract is frozen.
"""

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Costing Training Retrieval ──────────────────────────────────────────────

class CostingTrainingRetrievalInput(BaseModel):
    """Input schema for the Costing Training Retrieval tool."""
    query: str = Field(
        ...,
        description=(
            "Keyword or phrase to search for in the training datasets. "
            "Examples: 'fitter daily rate', 'MS plate per kg', 'DSR 2023 excavation', "
            "'railway overhead line equipment'"
        ),
    )
    max_results: int = Field(
        5,
        description="Maximum number of matching excerpts to return",
    )


class CostingTrainingRetrievalTool(BaseTool):
    """
    Search the costing training datasets for specific rates, unit costs, or historical data.

    Use this to look up:
    - Labour rates (fitter, welder, rigger, electrician, helper)
    - Material rates (steel, copper, concrete, cables)
    - DSR (Delhi Schedule of Rates) items
    - Historical project costs for similar Railway work
    - Equipment hire rates

    Returns matching excerpts from uploaded rate cards and historical data files.
    """
    name: str = "costing_training_retrieval"
    description: str = (
        "Search training datasets for specific costing rates, unit costs, or historical data. "
        "Use to look up labour rates, material rates, DSR items, equipment hire rates, "
        "or historical project costs from uploaded rate cards. "
        "Provide a specific item or keyword (e.g., 'fitter daily rate', 'MS plate per kg')."
    )
    args_schema: Type[BaseModel] = CostingTrainingRetrievalInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, query: str, max_results: int = 5) -> str:
        if not self.db:
            return json.dumps({"error": "Database session not available"})

        # Read on a session of this call's own. The ReAct agent issues its
        # tool calls in parallel, and this tool used to query the session it
        # shares with every other tool of the batch -- so a retrieval that
        # overlapped a web search failed with "This session is provisioning a
        # new connection; concurrent operations are not permitted" or "cursor
        # already closed" (the recycling wrapper ending the shared transaction
        # under it), and the batch priced without its rate-card evidence. The
        # reads here are all that this tool does, so nothing else is affected.
        from app.core.database import SessionLocal
        session = SessionLocal()
        try:
            from app.models.agent_builder import CustomAgent
            from app.models.training_dataset import TrainingDataset, TrainingDatasetFile, AgentTrainingDataset

            # Find the costing_researcher agent
            agent = (
                session.query(CustomAgent)
                .filter(CustomAgent.agent_key == "costing_researcher")
                .first()
            )
            if not agent:
                return json.dumps({
                    "status": "no_agent",
                    "message": "costing_researcher agent not found in database",
                    "results": [],
                })

            # Get assigned datasets
            assignments = (
                session.query(AgentTrainingDataset)
                .filter(AgentTrainingDataset.agent_id == agent.id)
                .order_by(AgentTrainingDataset.priority.asc())
                .all()
            )

            if not assignments:
                return json.dumps({
                    "status": "no_datasets",
                    "message": "No training datasets assigned to costing_researcher",
                    "results": [],
                })

            query_lower = query.lower()
            query_terms = [t.strip() for t in query_lower.split() if len(t.strip()) > 2]
            results = []

            for assignment in assignments:
                dataset = session.query(TrainingDataset).filter(
                    TrainingDataset.id == assignment.dataset_id,
                    TrainingDataset.status == "active",
                ).first()
                if not dataset:
                    continue

                files = session.query(TrainingDatasetFile).filter(
                    TrainingDatasetFile.dataset_id == dataset.id,
                    TrainingDatasetFile.extraction_status == "completed",
                ).all()

                for f in files:
                    content = f.parsed_content or f.raw_content or ""
                    if not content:
                        continue

                    # Score lines by term overlap
                    lines = content.splitlines()
                    for i, line in enumerate(lines):
                        line_lower = line.lower()
                        matches = sum(1 for term in query_terms if term in line_lower)
                        if matches >= max(1, len(query_terms) // 2):
                            # Include a small window of surrounding lines for context
                            start = max(0, i - 1)
                            end = min(len(lines), i + 3)
                            excerpt = "\n".join(lines[start:end]).strip()
                            results.append({
                                "dataset": dataset.name,
                                "file": f.file_name,
                                "excerpt": excerpt,
                                "relevance_score": matches,
                            })

                        if len(results) >= max_results * 3:
                            break
                    if len(results) >= max_results * 3:
                        break

            # Sort by relevance and deduplicate
            results.sort(key=lambda x: x["relevance_score"], reverse=True)
            results = results[:max_results]

            if not results:
                return json.dumps({
                    "status": "not_found",
                    "query": query,
                    "message": f"No training data found for query: '{query}'. "
                               "Try a more specific keyword or check web search for current rates.",
                    "results": [],
                })

            return json.dumps({
                "status": "found",
                "query": query,
                "result_count": len(results),
                "results": results,
            })

        except Exception as e:
            logger.error(f"Costing training retrieval failed: {e}")
            try:
                session.rollback()
            except Exception:
                pass
            return json.dumps({"error": f"Training retrieval failed: {str(e)}"})
        finally:
            try:
                session.close()
            except Exception:
                pass


# ── Travel Agent Delegation ─────────────────────────────────────────────────

class TravelDelegationInput(BaseModel):
    """Input schema for travel research delegation."""
    origin: str = Field(..., description="Origin city or station (e.g., 'Delhi', 'New Delhi')")
    destination: str = Field(..., description="Destination city or station (e.g., 'Lucknow', 'Patna')")
    crew_count: int = Field(..., description="Number of crew members travelling")
    travel_class: str = Field(
        "3AC",
        description="Travel class: '3AC', '2AC', '1AC', 'SL' (Sleeper), 'Air'",
    )
    travel_dates: Optional[str] = Field(
        None,
        description="Approximate travel dates or duration (e.g., '3 days', 'April 2025')",
    )
    purpose: str = Field(
        ...,
        description="Purpose of travel for context (use generic terms, no company/tender names)",
    )


class DelegateToTravelAgentTool(BaseTool):
    """
    Delegate travel cost research to the travel research sub-agent.

    Use this when you need train fares, flight costs, or travel time estimates
    for crew mobilization in your cost estimate. Provide origin, destination,
    crew count, and travel class.

    Returns estimated travel costs and travel time per person and in total.
    NOTE: The travel research agent is currently in stub mode — it returns
    estimated rates based on standard IRCTC guidelines. Replace manual estimates
    with the returned values in your cost breakdown.
    """
    name: str = "delegate_travel_research"
    description: str = (
        "Get travel cost and time estimates for crew mobilization. "
        "Provide origin city, destination city, number of crew, and travel class. "
        "Returns estimated train/air fares and travel duration. "
        "Use these figures in the Transport line items of your cost estimate."
    )
    args_schema: Type[BaseModel] = TravelDelegationInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    # IRCTC approximate rates per person (INR, as of 2024-25 — apply 5% escalation per year)
    _APPROX_RATES = {
        "SL":  {"base_per_100km": 35,  "min": 150,  "max": 600},
        "3AC": {"base_per_100km": 90,  "min": 400,  "max": 2200},
        "2AC": {"base_per_100km": 140, "min": 600,  "max": 3500},
        "1AC": {"base_per_100km": 250, "min": 1000, "max": 6000},
        "Air": {"base_per_100km": 500, "min": 2500, "max": 15000},
    }

    # Approximate distances (km) between major Indian cities for estimation
    _APPROX_DISTANCES = {
        frozenset({"delhi", "lucknow"}):   520,
        frozenset({"delhi", "mumbai"}):    1400,
        frozenset({"delhi", "kolkata"}):   1450,
        frozenset({"delhi", "chennai"}):   2180,
        frozenset({"delhi", "bangalore"}): 2150,
        frozenset({"delhi", "hyderabad"}): 1570,
        frozenset({"delhi", "patna"}):     1000,
        frozenset({"delhi", "varanasi"}):  820,
        frozenset({"mumbai", "kolkata"}):  2000,
        frozenset({"mumbai", "chennai"}):  1330,
    }

    def _run(
        self,
        origin: str,
        destination: str,
        crew_count: int,
        travel_class: str = "3AC",
        travel_dates: Optional[str] = None,
        purpose: str = "",
    ) -> str:
        # Future: when the travel research agent is built, call it here:
        #   from app.services.langchain.graphs.travel_agent import run_travel_research
        #   return run_travel_research(db=self.db, origin=origin, destination=destination, ...)
        #
        # For now, return a structured stub with guideline rates.

        travel_class = travel_class.upper() if travel_class else "3AC"
        if travel_class not in self._APPROX_RATES:
            travel_class = "3AC"

        rate_info = self._APPROX_RATES[travel_class]

        # Estimate distance
        origin_key = origin.lower().split()[0]
        dest_key = destination.lower().split()[0]
        pair = frozenset({origin_key, dest_key})
        distance_km = self._APPROX_DISTANCES.get(pair, 600)  # default 600km if unknown

        # Estimate fare per person (simple linear model)
        estimated_fare = min(
            rate_info["max"],
            max(rate_info["min"], int(distance_km / 100 * rate_info["base_per_100km"]))
        )

        # Estimate travel time (rough: 60 km/h avg for train, 150 km/h for air including airport)
        speed_kmh = 150 if travel_class == "Air" else 60
        travel_hours = round(distance_km / speed_kmh, 1)

        total_fare = estimated_fare * crew_count
        # Assume return journey for mobilization/demobilization
        total_with_return = total_fare * 2

        result = {
            "status": "stub",
            "agent_note": (
                "Travel research agent not yet deployed. "
                "These are guideline estimates based on IRCTC 2024-25 rates. "
                "Verify current fares on IRCTC website before finalizing."
            ),
            "origin": origin,
            "destination": destination,
            "estimated_distance_km": distance_km,
            "travel_class": travel_class,
            "crew_count": crew_count,
            "travel_dates": travel_dates or "Not specified",
            "purpose": purpose,
            "estimated_fare_per_person_inr": estimated_fare,
            "estimated_travel_time_hours": travel_hours,
            "total_fare_one_way_inr": total_fare,
            "total_fare_with_return_inr": total_with_return,
            "recommended_line_items": [
                {
                    "description": f"Travel — {origin} to {destination} ({travel_class}) × {crew_count} persons (one-way)",
                    "quantity": crew_count,
                    "unit": "persons",
                    "rate": estimated_fare,
                    "amount": total_fare,
                    "note": "Guideline rate — verify on IRCTC",
                },
                {
                    "description": f"Return travel — {destination} to {origin} ({travel_class}) × {crew_count} persons",
                    "quantity": crew_count,
                    "unit": "persons",
                    "rate": estimated_fare,
                    "amount": total_fare,
                    "note": "Guideline rate — verify on IRCTC",
                },
            ],
            "assumptions": [
                f"Distance estimated at ~{distance_km} km",
                f"Rates based on IRCTC 2024-25 {travel_class} guidelines",
                "Does not include tatkal surcharge, food, or local transport at destination",
            ],
        }

        return json.dumps(result)
