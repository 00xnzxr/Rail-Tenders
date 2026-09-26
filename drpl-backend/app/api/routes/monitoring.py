"""
DRPL Backend - Monitoring Routes
Portal health dashboard and alert endpoints
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.schemas import PortalHealthInfo, PortalAlert
from app.services.monitoring_service import get_portal_health, get_portal_alerts

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


@router.get("/portal-health", response_model=list[PortalHealthInfo])
def portal_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get health status for all portals."""
    return get_portal_health(db)


@router.get("/alerts", response_model=list[PortalAlert])
def portal_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get active alerts for portal issues."""
    return get_portal_alerts(db)
