"""
DRPL Backend - Generated Document Routes
Standalone document creation tool + proposal export
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.roles import require_surface
from app.core.auth import get_current_user
from app.models.user import User
from app.models.letterhead import GeneratedDocument
from app.services.pdf_generation_service import (
    generate_pdf, generate_preview, generate_proposal_pdf,
)
from app.services.document_template_definitions import get_template, get_available_types
from app.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"],
    # Role gate: `document_generator` is outside the costing_research surface
    # (app/core/roles.py). Enforced here rather than per-endpoint so a new
    # route on this router cannot be added ungated by accident.
    dependencies=[Depends(require_surface("document_generator"))])


@router.get("/templates/")
def list_document_types(
    current_user: User = Depends(get_current_user),
):
    """List available document type templates."""
    return get_available_types()


@router.get("/templates/{document_type}")
def get_document_type_template(
    document_type: str,
    current_user: User = Depends(get_current_user),
):
    """Get template structure for a document type."""
    template = get_template(document_type)
    if not template:
        raise HTTPException(status_code=404, detail=f"No template found for type: {document_type}")
    return template


@router.post("/")
def create_document(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new standalone document."""
    orientation = (body.get("page_orientation") or "portrait").strip().lower()
    if orientation not in ("portrait", "landscape"):
        orientation = "portrait"
    doc = GeneratedDocument(
        title=body.get("title", "Untitled Document"),
        document_type=body.get("document_type", "custom"),
        tender_id=body.get("tender_id"),
        letterhead_template_id=body.get("letterhead_template_id"),
        content_html=body.get("content_html"),
        content_markdown=body.get("content_markdown"),
        template_variables=body.get("template_variables", {}),
        signatures=body.get("signatures", []),
        page_orientation=orientation,
        created_by=current_user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    return {
        "id": doc.id, "title": doc.title, "status": doc.status,
        "created_at": doc.created_at,
    }


@router.get("/")
def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List user's generated documents."""
    docs = db.query(GeneratedDocument).filter(
        GeneratedDocument.created_by == current_user.id
    ).order_by(GeneratedDocument.updated_at.desc()).all()

    return [
        {
            "id": d.id, "title": d.title, "document_type": d.document_type,
            "tender_id": d.tender_id, "status": d.status,
            "letterhead_template_id": d.letterhead_template_id,
            "has_file": bool(d.generated_file_path),
            "file_size": d.file_size,
            "created_at": d.created_at, "updated_at": d.updated_at,
        }
        for d in docs
    ]


@router.post("/bulk-delete")
def bulk_delete_documents(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete multiple documents at once."""
    document_ids = body.get("document_ids", [])
    if not document_ids or not isinstance(document_ids, list):
        raise HTTPException(status_code=400, detail="document_ids must be a non-empty list")
    if len(document_ids) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 documents per bulk delete")

    docs = db.query(GeneratedDocument).filter(
        GeneratedDocument.id.in_(document_ids),
        GeneratedDocument.created_by == current_user.id,
    ).all()

    deleted_count = 0
    storage = get_storage_service()
    for doc in docs:
        # Remove the generated PDF from R2 (best-effort).
        if doc.generated_file_path:
            try:
                storage.delete_file_sync(doc.generated_file_path)
            except Exception as e:
                logger.warning(
                    f"[documents] failed to delete generated PDF {doc.generated_file_path}: {e}"
                )
        db.delete(doc)
        deleted_count += 1

    db.commit()
    return {"status": "deleted", "deleted_count": deleted_count, "requested_count": len(document_ids)}


@router.get("/{document_id}")
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get document details."""
    doc = db.query(GeneratedDocument).filter(GeneratedDocument.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    return {
        "id": doc.id, "title": doc.title, "document_type": doc.document_type,
        "tender_id": doc.tender_id, "proposal_session_id": doc.proposal_session_id,
        "letterhead_template_id": doc.letterhead_template_id,
        "content_html": doc.content_html, "content_markdown": doc.content_markdown,
        "template_variables": doc.template_variables, "signatures": doc.signatures,
        "page_orientation": doc.page_orientation or "portrait",
        "status": doc.status, "generated_file_name": doc.generated_file_name,
        "file_size": doc.file_size,
        "created_at": doc.created_at, "updated_at": doc.updated_at,
    }


@router.put("/{document_id}")
def update_document(
    document_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update document content and settings."""
    doc = db.query(GeneratedDocument).filter(GeneratedDocument.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    updatable = [
        "title", "document_type", "letterhead_template_id",
        "content_html", "content_markdown", "template_variables", "signatures",
    ]
    for field in updatable:
        if field in body:
            setattr(doc, field, body[field])

    # Orientation — validate before writing
    if "page_orientation" in body:
        po = (body.get("page_orientation") or "portrait").strip().lower()
        if po in ("portrait", "landscape"):
            doc.page_orientation = po

    # Reset status to draft if content changed
    if any(f in body for f in ["content_html", "content_markdown", "letterhead_template_id", "signatures", "page_orientation"]):
        doc.status = "draft"

    db.commit()
    db.refresh(doc)

    return {"id": doc.id, "status": doc.status, "updated_at": doc.updated_at}


@router.delete("/{document_id}")
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a single document."""
    doc = db.query(GeneratedDocument).filter(
        GeneratedDocument.id == document_id,
        GeneratedDocument.created_by == current_user.id,
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Remove the generated PDF from R2 (best-effort).
    if doc.generated_file_path:
        try:
            get_storage_service().delete_file_sync(doc.generated_file_path)
        except Exception as e:
            logger.warning(
                f"[documents] failed to delete generated PDF {doc.generated_file_path}: {e}"
            )

    db.delete(doc)
    db.commit()
    return {"status": "deleted", "id": document_id}


@router.post("/{document_id}/generate")
def generate_document_pdf(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate final PDF for a document."""
    try:
        file_path = generate_pdf(db, document_id)
        doc = db.query(GeneratedDocument).filter(GeneratedDocument.id == document_id).first()
        return {
            "status": "generated",
            "file_name": doc.generated_file_name,
            "file_size": doc.file_size,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{document_id}/preview")
def preview_document_pdf(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate and return preview PDF bytes."""
    try:
        pdf_bytes = generate_preview(db, document_id)
        if not pdf_bytes or len(pdf_bytes) < 50:
            raise HTTPException(status_code=500, detail="PDF generation returned empty output")
        return Response(content=pdf_bytes, media_type="application/pdf")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF preview failed: {str(e)}")


@router.get("/{document_id}/download")
def download_document_pdf(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download the generated PDF file."""
    doc = db.query(GeneratedDocument).filter(GeneratedDocument.id == document_id).first()
    if not doc or not doc.generated_file_path:
        raise HTTPException(status_code=404, detail="Generated PDF not found")

    try:
        pdf_bytes = get_storage_service().download_file_sync(doc.generated_file_path)
    except Exception as e:
        logger.warning(
            f"[documents] download failed for {doc.generated_file_path}: {e}"
        )
        raise HTTPException(status_code=404, detail="PDF file missing from storage")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{doc.generated_file_name}"'},
    )


@router.post("/{document_id}/ai-generate")
async def ai_generate_content(
    document_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate or enhance document content using AI."""
    from app.services.document_ai_service import generate_document_content

    doc = db.query(GeneratedDocument).filter(GeneratedDocument.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    prompt = body.get("prompt", "")
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt is required")

    mode = body.get("mode", "generate")
    document_type = body.get("document_type", doc.document_type or "custom")

    try:
        content = await generate_document_content(
            db=db,
            prompt=prompt,
            document_type=document_type,
            existing_content=doc.content_markdown or doc.content_html or "",
            template_variables=doc.template_variables or {},
            mode=mode,
        )
        return {"content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI generation failed: {str(e)}")


@router.post("/from-proposal/{session_id}")
def create_from_proposal(
    session_id: int,
    body: dict = {},
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a document from a proposal session."""
    try:
        doc = generate_proposal_pdf(
            db=db,
            session_id=session_id,
            letterhead_id=body.get("letterhead_template_id"),
            signature_ids=body.get("signature_ids", []),
        )
        return {
            "id": doc.id, "title": doc.title, "status": doc.status,
            "generated_file_name": doc.generated_file_name,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
