"""
DRPL Backend - Admin Audit Log Routes
Audit log viewing and export (master_admin only)
"""

from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel
import csv
import io

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.models.user import User
from app.models.audit_log import AuditLog

router = APIRouter(prefix="/admin/audit-logs", tags=["admin-audit"])


class AuditLogResponse(BaseModel):
    id: int
    user_id: Optional[int]
    user_email: Optional[str]
    action: str
    resource_type: Optional[str]
    resource_id: Optional[str]
    details: Optional[dict]
    ip_address: Optional[str]
    created_at: Optional[str]

    class Config:
        from_attributes = True


@router.get("/", response_model=list[AuditLogResponse])
def list_audit_logs(
    action: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    resource_type: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List audit log entries with filters."""
    query = db.query(AuditLog)

    if action:
        query = query.filter(AuditLog.action == action)
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if date_from:
        try:
            query = query.filter(AuditLog.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(AuditLog.created_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass

    logs = query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()
    return [AuditLogResponse(
        id=l.id, user_id=l.user_id, user_email=l.user_email, action=l.action,
        resource_type=l.resource_type, resource_id=l.resource_id, details=l.details,
        ip_address=l.ip_address,
        created_at=l.created_at.isoformat() if l.created_at else None,
    ) for l in logs]


@router.get("/actions")
def list_action_types(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List distinct action types for filter dropdown."""
    actions = db.query(AuditLog.action).distinct().all()
    return [a[0] for a in actions]


@router.get("/export")
def export_audit_logs(
    action: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Export audit logs as CSV."""
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    if date_from:
        try:
            query = query.filter(AuditLog.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(AuditLog.created_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass

    logs = query.order_by(AuditLog.created_at.desc()).limit(10000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "User ID", "User Email", "Action", "Resource Type", "Resource ID", "Details", "IP Address"])
    for l in logs:
        writer.writerow([
            l.created_at.isoformat() if l.created_at else "",
            l.user_id or "", l.user_email or "", l.action,
            l.resource_type or "", l.resource_id or "",
            str(l.details) if l.details else "", l.ip_address or "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_logs.csv"},
    )
