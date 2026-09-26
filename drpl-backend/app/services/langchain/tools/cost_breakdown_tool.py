"""DRPL LangChain Tool — Cost Breakdown Read.

Reads back a stored cost breakdown. `cost_calculator` computes and
`xlsx_generator` exports, but until this tool nothing could read what was
*saved* — so "why did this costing come out this way" was answered from the
chat scrollback, a confident reconstruction instead of the record.
"""

import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class CostBreakdownReadInput(BaseModel):
    tender_id: int = Field(..., description="The tender whose cost breakdown to read")
    version: Optional[int] = Field(
        None, description="A specific version. Omit for the newest (working) version."
    )


class CostBreakdownReadTool(BaseTool):
    """Read the saved cost breakdown for a tender: every line, the rates, the totals."""

    name: str = "cost_breakdown_read"
    description: str = (
        "Read the stored cost breakdown for a tender — every line item with its "
        "quantity, rate and amount, plus overhead, margin, GST and totals. Use this "
        "to explain or answer questions about a costing the platform already "
        "produced. It reads; it never creates or changes a costing."
    )
    args_schema: Type[BaseModel] = CostBreakdownReadInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, tender_id: int, version: Optional[int] = None) -> str:
        if self.db is None:
            return "Cost breakdown lookup unavailable: no database session."

        from app.core.actor_context import current_actor
        from app.core.ownership import scoped_query
        from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine

        # A cost breakdown is one person's work, and the tender it hangs off is
        # shared — so "read the breakdown for tender N" is exactly the request
        # that would hand a costing researcher a colleague's numbers if this
        # query were unscoped. Scoping lives here because the agent reaches the
        # database without passing through any route.
        query = scoped_query(self.db, CostBreakdown, current_actor()).filter(
            CostBreakdown.tender_id == tender_id
        )
        if version is not None:
            query = query.filter(CostBreakdown.version == version)
        breakdown = query.order_by(CostBreakdown.version.desc()).first()

        if breakdown is None:
            scope = f" version {version}" if version is not None else ""
            return (
                f"No cost breakdown exists for tender {tender_id}{scope}. "
                "One has to be produced (by the costing researcher) before it can be read."
            )

        lines = (
            self.db.query(CostBreakdownLine)
            .filter(CostBreakdownLine.cost_breakdown_id == breakdown.id)
            .order_by(CostBreakdownLine.sr_no)
            .all()
        )

        def _n(value: Optional[float]) -> str:
            return f"{value:,.2f}" if value is not None else "—"

        out = [
            f"# Cost Breakdown — Tender {tender_id} (v{breakdown.version}, {breakdown.status})",
        ]
        if breakdown.title:
            out.append(f"Title: {breakdown.title}")
        out += [
            "",
            "| Sr | Description | Qty | Unit | Rate | Amount |",
            "|---|---|---|---|---|---|",
        ]
        for line in lines:
            desc = (line.description or "").replace("|", "/")
            rate = _n(line.rate)
            if line.rate is None and line.rate_low is not None:
                rate = f"{_n(line.rate_low)}–{_n(line.rate_high)}"
            out.append(
                f"| {line.sr_no} | {desc} | {_n(line.quantity)} | {line.unit or '—'} "
                f"| {rate} | {_n(line.amount)} |"
            )
        out += [
            "",
            f"Subtotal: {_n(breakdown.subtotal)}",
            f"Overhead ({breakdown.overhead_percent}%): {_n(breakdown.overhead_amount)}",
            f"Margin ({breakdown.margin_percent}%): {_n(breakdown.margin_amount)}",
            f"GST ({breakdown.gst_percent}%): {_n(breakdown.gst_amount)}",
            f"Grand total: {_n(breakdown.grand_total)}",
        ]
        if breakdown.needs_input_count:
            out.append(
                f"{breakdown.needs_input_count} line(s) still need a rate from the user."
            )
        return "\n".join(out)
