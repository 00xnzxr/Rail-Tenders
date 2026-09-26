"""
DRPL Backend - Template Routes
Admin endpoints for managing proposal templates, document format templates,
costing templates, and unified template listing.
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.models.user import User
from app.schemas import (
    ProposalTemplateResponse,
    DocumentFormatTemplateResponse,
    CostingTemplateResponse,
    UnifiedTemplateResponse,
)
from app.services.template_service import (
    # Proposal (existing)
    list_templates, get_template,
    upload_and_parse_template,
    update_template, deactivate_template,
    # Unified
    list_unified_templates,
    upload_and_parse_unified,
    get_template_preview,
    # Document format
    list_document_templates, get_document_template,
    create_document_template, update_document_template, delete_document_template,
    # Costing
    list_costing_templates, get_costing_template,
    create_costing_template, update_costing_template, delete_costing_template,
)

router = APIRouter(prefix="/templates", tags=["templates"])


# ── Unified Endpoints ───────────────────────────────────────────────────────

@router.get("/unified")
def list_all_unified(
    kind: Optional[str] = Query(None, description="Filter by kind: proposal, document, costing"),
    category: Optional[str] = Query(None, description="Filter by category/type"),
    zone: Optional[str] = Query(None, description="Filter costing templates by zone"),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List templates across all three template types."""
    return list_unified_templates(db, kind=kind, category=category, zone=zone)


@router.post("/upload/unified")
async def upload_template_unified(
    template_kind: str = Query(..., description="Template kind: proposal, document, costing"),
    category: Optional[str] = Query(None, description="Document category (for document templates)"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Upload a template file (PDF, DOCX, XLSX) and parse based on template_kind."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    allowed_extensions = (".pdf", ".docx", ".xlsx", ".xls")
    if not file.filename.lower().endswith(allowed_extensions):
        raise HTTPException(status_code=400, detail="Supported formats: PDF, DOCX, XLSX")

    file_data = await file.read()
    if len(file_data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    if template_kind not in ("proposal", "document", "costing"):
        raise HTTPException(status_code=400, detail="template_kind must be proposal, document, or costing")

    try:
        result = await upload_and_parse_unified(
            db, file.filename, file_data, admin.id, template_kind, category,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Template parsing failed: {str(e)}")


@router.get("/{kind}/{template_id}/preview")
def preview_template(
    kind: str,
    template_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get a rich preview of a template's structure."""
    if kind not in ("proposal", "document", "costing"):
        raise HTTPException(status_code=400, detail="kind must be proposal, document, or costing")

    preview = get_template_preview(db, kind, template_id)
    if not preview:
        raise HTTPException(status_code=404, detail="Template not found")
    return preview


# ── Proposal Template Endpoints (existing, backward-compatible) ─────────────

@router.get("/", response_model=list[ProposalTemplateResponse])
def list_all_templates(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all proposal templates."""
    return list_templates(db)


@router.post("/upload", response_model=ProposalTemplateResponse)
async def upload_template(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Upload a PDF template and trigger AI parsing."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    allowed_extensions = (".pdf",)
    if not file.filename.lower().endswith(allowed_extensions):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    file_data = await file.read()
    if len(file_data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    template = await upload_and_parse_template(db, file.filename, file_data, admin.id)
    return template


@router.get("/{template_id}", response_model=ProposalTemplateResponse)
def get_template_details(
    template_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get a template's full details including parsed structure."""
    template = get_template(db, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.put("/{template_id}", response_model=ProposalTemplateResponse)
def update_template_endpoint(
    template_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update a template's metadata or structure."""
    template = update_template(db, template_id, body)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.delete("/{template_id}")
def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Deactivate a template (soft delete)."""
    if not deactivate_template(db, template_id):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"status": "deactivated"}


# ── Document Format Template Endpoints ──────────────────────────────────────

@router.get("/document/", response_model=list[DocumentFormatTemplateResponse])
def list_document_templates_endpoint(
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all document format templates (system + user)."""
    return list_document_templates(db)


@router.post("/document/", response_model=DocumentFormatTemplateResponse)
def create_document_template_endpoint(
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Create a user-defined document format template."""
    return create_document_template(db, body, admin.id)


@router.get("/document/{template_id}", response_model=DocumentFormatTemplateResponse)
def get_document_template_endpoint(
    template_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get a document format template by ID."""
    template = get_document_template(db, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.put("/document/{template_id}", response_model=DocumentFormatTemplateResponse)
def update_document_template_endpoint(
    template_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update a document format template."""
    try:
        template = update_document_template(db, template_id, body)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.delete("/document/{template_id}")
def delete_document_template_endpoint(
    template_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Soft-delete a document format template."""
    try:
        if not delete_document_template(db, template_id):
            raise HTTPException(status_code=404, detail="Template not found")
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {"status": "deactivated"}


# ── Costing Template Endpoints ──────────────────────────────────────────────

@router.get("/costing/", response_model=list[CostingTemplateResponse])
def list_costing_templates_endpoint(
    zone: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all costing templates, optionally filtered by zone."""
    return list_costing_templates(db, zone=zone)


@router.post("/costing/", response_model=CostingTemplateResponse)
def create_costing_template_endpoint(
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Create a costing template."""
    return create_costing_template(db, body, admin.id)


@router.get("/costing/{template_id}", response_model=CostingTemplateResponse)
def get_costing_template_endpoint(
    template_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get a costing template by ID."""
    template = get_costing_template(db, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.put("/costing/{template_id}", response_model=CostingTemplateResponse)
def update_costing_template_endpoint(
    template_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update a costing template."""
    template = update_costing_template(db, template_id, body)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    return template


@router.delete("/costing/{template_id}")
def delete_costing_template_endpoint(
    template_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Delete a costing template."""
    if not delete_costing_template(db, template_id):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"status": "deleted"}
