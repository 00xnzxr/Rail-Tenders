"""
DRPL Backend - Ratecard Routes

Master-admin endpoints for managing structured ratecards (Cummins/Fleetguard
Part-No price lists + B/C/D-check kit schedules) used as the primary source
for bottom-up component build-up costing. Mirrors training_datasets.py.
"""

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.models.user import User
from app.services import ratecard_ingest_service as svc

router = APIRouter(prefix="/ratecards", tags=["ratecards"])


@router.get("/")
def api_list_ratecards(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List ratecards with item + check-schedule counts."""
    return svc.list_ratecards(db, status_filter=status)


@router.post("/")
def api_create_ratecard(
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Create a new (empty) ratecard. Upload a file next to populate it."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Ratecard name is required")
    try:
        rc = svc.create_ratecard(
            db=db,
            name=name,
            source_label=body.get("source_label"),
            description=body.get("description"),
            created_by=admin.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "id": rc.id,
        "name": rc.name,
        "source_label": rc.source_label,
        "description": rc.description,
        "status": rc.status,
    }


@router.get("/{ratecard_id}")
def api_get_ratecard(
    ratecard_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Ratecard metadata + counts + check schedules (not the full item list)."""
    rc = svc.get_ratecard_with_items(db, ratecard_id)
    if not rc:
        raise HTTPException(status_code=404, detail="Ratecard not found")
    return rc


@router.delete("/{ratecard_id}")
def api_delete_ratecard(
    ratecard_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Delete a ratecard and all its items + check schedules."""
    if not svc.delete_ratecard(db, ratecard_id):
        raise HTTPException(status_code=404, detail="Ratecard not found")
    return {"deleted": True}


@router.post("/{ratecard_id}/upload")
async def api_upload_ratecard(
    ratecard_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Parse an Excel ratecard and persist its items + check schedules."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in svc.ALLOWED_RATECARD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {sorted(svc.ALLOWED_RATECARD_EXTENSIONS)}",
        )
    content = await file.read()
    if len(content) > svc.MAX_RATECARD_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large (max 15 MB)")
    try:
        return svc.ingest_ratecard_xlsx(db, ratecard_id, content, file.filename or "ratecard.xlsx")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(e)}")


@router.post("/preview")
async def api_preview_ratecard(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Parse a ratecard WITHOUT committing — for upload preview."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in svc.ALLOWED_RATECARD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {sorted(svc.ALLOWED_RATECARD_EXTENSIONS)}",
        )
    content = await file.read()
    try:
        return svc.preview_ratecard_xlsx(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Preview failed: {str(e)}")


@router.get("/{ratecard_id}/items")
def api_list_items(
    ratecard_id: int,
    engine_type: Optional[str] = None,
    check_level: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Paged item listing for review."""
    return svc.list_items(
        db, ratecard_id, engine_type=engine_type, check_level=check_level,
        limit=min(limit, 2000), offset=offset,
    )


@router.delete("/items/{item_id}")
def api_delete_item(
    item_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Delete a single ratecard item."""
    if not svc.delete_item(db, item_id):
        raise HTTPException(status_code=404, detail="Item not found")
    return {"deleted": True}
