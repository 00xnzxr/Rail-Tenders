"""
DRPL Backend - Letterhead Template Routes
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user, require_master_admin
from app.models.user import User
from app.services.letterhead_service import (
    create_template, update_template, upload_asset, upload_letterhead_pdf,
    get_templates, get_template, delete_template,
)
from app.services.pdf_generation_service import generate_letterhead_preview
from app.core.redis_client import cache_get_json, cache_set_json, cache_key

router = APIRouter(prefix="/letterhead", tags=["letterhead"])


@router.get("/templates")
def list_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all letterhead templates."""
    ck = cache_key("letterhead_templates", "all")
    cached = cache_get_json(ck)
    if cached is not None:
        return cached

    templates = get_templates(db)
    result = []
    for t in templates:
        try:
            result.append({
                "id": t.id, "name": t.name, "description": t.description,
                "is_default": t.is_default, "is_active": t.is_active,
                "logo_position": t.logo_position, "page_size": t.page_size,
                "font_family": t.font_family, "font_size_pt": t.font_size_pt,
                "margin_top_mm": t.margin_top_mm, "margin_bottom_mm": t.margin_bottom_mm,
                "margin_left_mm": t.margin_left_mm, "margin_right_mm": t.margin_right_mm,
                "has_letterhead_pdf": bool(getattr(t, 'letterhead_pdf_path', None)),
                "has_header": bool(t.header_image_path or t.header_html),
                "has_footer": bool(t.footer_image_path or t.footer_html),
                "has_watermark": bool(t.watermark_image_path),
                "has_logo": bool(t.logo_path),
                "created_at": t.created_at,
            })
        except Exception:
            # Gracefully handle any column access issues
            result.append({
                "id": t.id, "name": getattr(t, 'name', 'Unknown'),
                "description": None, "is_default": False, "is_active": True,
                "logo_position": "left", "page_size": "A4",
                "font_family": "Helvetica", "font_size_pt": 11,
                "margin_top_mm": 25, "margin_bottom_mm": 25,
                "margin_left_mm": 20, "margin_right_mm": 20,
                "has_letterhead_pdf": False, "has_header": False,
                "has_footer": False, "has_watermark": False, "has_logo": False,
                "created_at": getattr(t, 'created_at', None),
            })
    cache_set_json(ck, result)
    return result


@router.post("/templates")
def create_new_template(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create a new letterhead template (master admin only)."""
    template = create_template(db, body, current_user.id)
    return {"id": template.id, "name": template.name, "status": "created"}


@router.put("/templates/{template_id}")
def update_existing_template(
    template_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Update a letterhead template."""
    template = update_template(db, template_id, body)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"id": template.id, "name": template.name, "status": "updated"}


@router.delete("/templates/{template_id}")
def delete_existing_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Delete a letterhead template."""
    if not delete_template(db, template_id):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"status": "deleted"}


@router.post("/templates/{template_id}/upload-pdf")
async def upload_template_pdf(
    template_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Upload a full PDF letterhead to use as the base template for all documents."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    file_data = await file.read()
    if len(file_data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        path = upload_letterhead_pdf(db, template_id, file_data, file.filename)
        return {"status": "uploaded", "path": path}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/templates/{template_id}/upload/{asset_type}")
async def upload_template_asset(
    template_id: int,
    asset_type: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Upload a letterhead asset (header, footer, watermark, logo)."""
    file_data = await file.read()
    try:
        path = upload_asset(db, template_id, asset_type, file_data, file.filename)
        return {"status": "uploaded", "asset_type": asset_type, "path": path}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/templates/{template_id}/preview")
def preview_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a preview PDF of the letterhead template."""
    try:
        pdf_bytes = generate_letterhead_preview(db, template_id)
        return Response(content=pdf_bytes, media_type="application/pdf")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
