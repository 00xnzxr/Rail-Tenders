"""Lean read endpoints for cost breakdowns not tied to a single tender path."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.models.tender import Tender
from app.models.cost_breakdown import CostBreakdown

router = APIRouter(prefix="/cost-breakdowns", tags=["cost-breakdowns"])


@router.get("/recent")
def recent_costings(
    limit: int = Query(5, ge=1, le=50),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Most recently updated cost breakdowns, joined to their tender title."""
    rows = (
        db.query(CostBreakdown, Tender.title)
        .join(Tender, Tender.id == CostBreakdown.tender_id)
        .order_by(CostBreakdown.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "tender_id": cb.tender_id,
            "title": title,
            "grand_total": cb.grand_total,
            "margin_pct": cb.margin_pct,
            "status": cb.status,
            "updated_at": cb.updated_at.isoformat() if cb.updated_at else None,
        }
        for cb, title in rows
    ]
