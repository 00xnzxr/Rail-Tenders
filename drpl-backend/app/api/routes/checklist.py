"""
DRPL Backend - Checklist Routes
Endpoints for document checklist management
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.models.tender import Tender
from app.schemas import (
    ChecklistItemResponse, ChecklistCompletionResponse,
    AddChecklistItemInput, UpdateChecklistItemInput,
)
from pydantic import BaseModel

from app.models.checklist import ChecklistItem as ChecklistItemModel
from app.services.checklist_service import (
    generate_checklist, get_checklist, get_checklist_completion,
    upload_checklist_document, delete_checklist_document,
    add_checklist_item, update_checklist_item,
)

router = APIRouter(prefix="/tenders/{tender_id}/checklist", tags=["checklist"])


def _verify_tender(tender_id: int, db: Session) -> Tender:
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Tender not found")
    return tender


@router.post("/generate", response_model=list[ChecklistItemResponse])
async def generate_checklist_endpoint(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a document checklist for a tender using AI."""
    _verify_tender(tender_id, db)
    items = await generate_checklist(db, tender_id)
    return items


@router.get("/", response_model=list[ChecklistItemResponse])
def get_checklist_endpoint(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all checklist items for a tender."""
    _verify_tender(tender_id, db)
    return get_checklist(db, tender_id)


@router.get("/completion", response_model=ChecklistCompletionResponse)
def get_completion_endpoint(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get checklist completion statistics."""
    _verify_tender(tender_id, db)
    return get_checklist_completion(db, tender_id)


@router.post("/{item_id}/upload", response_model=ChecklistItemResponse)
async def upload_document_endpoint(
    tender_id: int,
    item_id: int,
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a document for a checklist item."""
    _verify_tender(tender_id, db)
    file_data = await file.read()
    upload_checklist_document(
        db, tender_id, item_id,
        file_name=file.filename or "document",
        file_data=file_data,
        mime_type=file.content_type or "application/octet-stream",
        user_id=current_user.id,
    )
    # Return updated checklist item
    from app.models.checklist import ChecklistItem
    from app.models.tender import TenderDocument
    item = db.query(ChecklistItem).filter(ChecklistItem.id == item_id).first()

    # Trigger background embedding if document was uploaded
    if background_tasks and item and item.document_id:
        doc = db.query(TenderDocument).filter(TenderDocument.id == item.document_id).first()
        if doc and doc.file_path:
            from app.api.routes.extension import _embed_document_background
            background_tasks.add_task(
                _embed_document_background, doc.id, doc.file_path, tender_id, doc.file_name,
            )

    return item


@router.delete("/{item_id}/document")
def delete_document_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove an uploaded document from a checklist item."""
    _verify_tender(tender_id, db)
    success = delete_checklist_document(db, tender_id, item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Checklist item or document not found")
    return {"status": "deleted"}


@router.post("/items", response_model=ChecklistItemResponse)
def add_item_endpoint(
    tender_id: int,
    body: AddChecklistItemInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually add a checklist item."""
    _verify_tender(tender_id, db)
    return add_checklist_item(db, tender_id, body.item_name, body.item_description, body.is_required)


@router.patch("/{item_id}", response_model=ChecklistItemResponse)
def update_item_endpoint(
    tender_id: int,
    item_id: int,
    body: UpdateChecklistItemInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a checklist item."""
    _verify_tender(tender_id, db)
    item = update_checklist_item(
        db, tender_id, item_id,
        item_name=body.item_name,
        item_description=body.item_description,
        is_required=body.is_required,
    )
    if not item:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    return item


# --- Document Generation Endpoints ---


@router.post("/generate-documents")
async def generate_documents_endpoint(
    tender_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger document generation for all non-standard checklist items."""
    _verify_tender(tender_id, db)

    # Find all non-standard items with pending status
    items = db.query(ChecklistItemModel).filter(
        ChecklistItemModel.tender_id == tender_id,
        ChecklistItemModel.item_category.in_(["generated", "analysis"]),
        ChecklistItemModel.generation_status == "pending",
    ).all()

    if not items:
        return {"triggered": 0, "message": "No pending items to generate"}

    # Mark as queued
    for item in items:
        item.generation_status = "queued"
    db.commit()

    # Trigger generation in background
    background_tasks.add_task(
        _run_document_generation_background,
        tender_id, current_user.id,
    )

    return {"triggered": len(items)}


@router.post("/{item_id}/generate", response_model=ChecklistItemResponse)
async def generate_single_document_endpoint(
    tender_id: int,
    item_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger document generation for a single checklist item."""
    _verify_tender(tender_id, db)

    item = db.query(ChecklistItemModel).filter(
        ChecklistItemModel.id == item_id,
        ChecklistItemModel.tender_id == tender_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Checklist item not found")

    if item.item_category == "standard":
        raise HTTPException(status_code=400, detail="Standard items do not need generation — upload them manually")

    # Mark as queued
    item.generation_status = "queued"
    item.generation_error = None
    db.commit()
    db.refresh(item)

    # Trigger generation in background
    background_tasks.add_task(
        _run_single_item_generation_background,
        tender_id, item_id, current_user.id,
    )

    return item


@router.get("/{item_id}/preview")
async def preview_document_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Preview the generated document PDF for a checklist item."""
    from fastapi.responses import FileResponse

    _verify_tender(tender_id, db)

    item = db.query(ChecklistItemModel).filter(
        ChecklistItemModel.id == item_id,
        ChecklistItemModel.tender_id == tender_id,
    ).first()
    if not item or not item.generated_document_id:
        raise HTTPException(status_code=404, detail="No generated document found for this item")

    from app.models.letterhead import GeneratedDocument
    doc = db.query(GeneratedDocument).filter(GeneratedDocument.id == item.generated_document_id).first()
    if not doc or not doc.generated_file_path:
        raise HTTPException(status_code=404, detail="Generated document PDF not found")

    import os
    if not os.path.exists(doc.generated_file_path):
        raise HTTPException(status_code=404, detail="PDF file not found on disk")

    return FileResponse(doc.generated_file_path, media_type="application/pdf", filename=f"{item.item_name}.pdf")


class _UpdateCategoryInput(BaseModel):
    item_category: str


@router.patch("/{item_id}/category", response_model=ChecklistItemResponse)
def update_category_endpoint(
    tender_id: int,
    item_id: int,
    body: _UpdateCategoryInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Override the category of a checklist item."""
    _verify_tender(tender_id, db)

    if body.item_category not in ("standard", "generated", "analysis"):
        raise HTTPException(status_code=400, detail="Invalid category. Must be 'standard', 'generated', or 'analysis'")

    item = db.query(ChecklistItemModel).filter(
        ChecklistItemModel.id == item_id,
        ChecklistItemModel.tender_id == tender_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Checklist item not found")

    item.item_category = body.item_category
    # Reset generation status when category changes
    item.generation_status = "pending"
    item.generation_error = None
    db.commit()
    db.refresh(item)

    return item


# --- Background generation helpers ---

async def _run_document_generation_background(tender_id: int, user_id: int):
    """Run document generation for all queued items in background."""
    import logging
    from app.core.database import SessionLocal

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        items = db.query(ChecklistItemModel).filter(
            ChecklistItemModel.tender_id == tender_id,
            ChecklistItemModel.generation_status == "queued",
        ).all()

        for item in items:
            try:
                await _generate_single_item(db, tender_id, item, user_id)
            except Exception as e:
                logger.error(f"Background generation failed for item {item.id}: {e}")
                item.generation_status = "failed"
                item.generation_error = str(e)
                db.commit()
    except Exception as e:
        logger.error(f"Background document generation failed: {e}")
    finally:
        db.close()


async def _run_single_item_generation_background(tender_id: int, item_id: int, user_id: int):
    """Run document generation for a single item in background."""
    import logging
    from app.core.database import SessionLocal

    logger = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        item = db.query(ChecklistItemModel).filter(ChecklistItemModel.id == item_id).first()
        if item:
            await _generate_single_item(db, tender_id, item, user_id)
    except Exception as e:
        logger.error(f"Background single item generation failed: {e}")
        if item:
            item.generation_status = "failed"
            item.generation_error = str(e)
            db.commit()
    finally:
        db.close()


async def _generate_single_item(db: Session, tender_id: int, item: ChecklistItemModel, user_id: int):
    """Generate a single checklist item document based on its category."""
    item.generation_status = "generating"
    db.commit()

    try:
        if item.item_category == "generated":
            from app.services.document_ai_service import generate_document_content
            from app.services.pdf_generation_service import generate_pdf
            from app.models.letterhead import GeneratedDocument

            # Build prompt from ai_instructions or item name
            prompt = item.ai_instructions or f"Generate a professional {item.item_name} document for a railway tender submission."

            # Generate AI content
            content = await generate_document_content(
                db=db,
                prompt=prompt,
                document_type=item.item_name.lower().replace(" ", "_"),
            )

            # Create GeneratedDocument record
            doc = GeneratedDocument(
                title=item.item_name,
                document_type=item.item_name.lower().replace(" ", "_"),
                tender_id=tender_id,
                content_markdown=content,
            )
            db.add(doc)
            db.flush()

            # Generate PDF
            generate_pdf(db=db, document_id=doc.id)
            item.generated_document_id = doc.id

        elif item.item_category == "analysis":
            from app.services.analysis_document_service import generate_analysis_document

            doc_id = await generate_analysis_document(
                db=db,
                tender_id=tender_id,
                item_name=item.item_name,
                ai_instructions=item.ai_instructions,
                user_id=user_id,
            )
            item.generated_document_id = doc_id

        item.generation_status = "generated"
        db.commit()

    except Exception as e:
        item.generation_status = "failed"
        item.generation_error = str(e)
        db.commit()
        raise
