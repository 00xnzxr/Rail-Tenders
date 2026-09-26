"""
DRPL Backend - Checklist Service
Business logic for document checklist management
"""

import os
import re
import shutil
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tender import Tender, TenderDocument
from app.models.checklist import ChecklistItem
from app.services.ai_service import parse_document_checklist, _build_tender_context
from app.services.document_parser import extract_text_from_file
from app.services.checklist_classification_service import classify_checklist_items
from app.services.storage_service import get_storage_service, key_checklist_upload

settings = get_settings()


_SECTION_6_RE = re.compile(
    r"#{1,6}\s*SECTION\s*6\b[^\n]*\n(?P<body>.+?)(?=\n#{1,6}\s*SECTION\s*\d+\b|\Z)",
    re.IGNORECASE | re.DOTALL,
)
#: Section 6 of a large tender runs to a few dozen table rows.
_SECTION_6_MAX_CHARS = 12_000


#: Section 2 of the 7-section chat-upload report ("## 2. COMPLETE DOCUMENT
#: REQUIREMENTS"), up to the next level-2 heading.
_CHAT_SECTION_2_RE = re.compile(
    r"^#{1,6}\s*2\.\s*COMPLETE DOCUMENT REQUIREMENTS[^\n]*\n(?P<body>.+?)(?=^#{1,6}\s*\d+\.\s|\Z)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_MIN_SECTION_CHARS = 150


def required_documents_from_analysis(db: Session, tender_id: int) -> str:
    """The tender's analysed list of required documents, or "".

    The analysis synthesis reconciles the required documents across every
    PDF -- master RFP, schedules, annexures -- into Section 6 of its report;
    a tender analysed from chat uploads has the same list as Section 2 of the
    chat report instead. The checklist used to be generated from the first
    4,000 characters of the documents typed ``tender_notice`` only, and a chat
    upload is never typed that, so a chat tender's checklist was a guess from
    its metadata.
    """
    try:
        from app.models.document_analysis import (
            DocumentExtractionResult,
            TenderAnalysisSummary,
        )

        row = (
            db.query(TenderAnalysisSummary)
            .filter(TenderAnalysisSummary.tender_id == tender_id)
            .first()
        )
        report = (row.requirement_summary or "") if row else ""
        m = _SECTION_6_RE.search(report)
        body = (m.group("body") or "").strip() if m else ""
        # A table with no rows is a heading, not a list.
        if body.count("|") >= 8:
            return body

        chat = (
            db.query(DocumentExtractionResult.raw_text)
            .filter(
                DocumentExtractionResult.tender_id == tender_id,
                DocumentExtractionResult.document_name == "chat_attachment_analysis",
                DocumentExtractionResult.extraction_type == "full_analysis",
            )
            .order_by(DocumentExtractionResult.id.desc())
            .first()
        )
        m = _CHAT_SECTION_2_RE.search((chat[0] or "") if chat else "")
        body = (m.group("body") or "").strip() if m else ""
        return body if len(body) >= _MIN_SECTION_CHARS else ""
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return ""


async def generate_checklist(db: Session, tender_id: int) -> list[ChecklistItem]:
    """Generate a document checklist for a tender using AI."""
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise ValueError("Tender not found")

    # Build tender context
    tender_text = _build_tender_context(tender)

    # The analysis's own required-documents table when there is one: it was
    # built from every document and has already been paid for.
    required_docs = required_documents_from_analysis(db, tender_id)
    if required_docs:
        items_data = await parse_document_checklist(
            tender_text, required_docs, db=db,
            document_label=(
                "Required documents identified by the tender analysis "
                "(reconciled across every tender document)"
            ),
            max_document_chars=_SECTION_6_MAX_CHARS,
        )
    else:
        # Extract text from any existing tender documents
        doc_text = ""
        documents = db.query(TenderDocument).filter(
            TenderDocument.tender_id == tender_id,
            TenderDocument.document_type == "tender_notice",
        ).all()
        storage = get_storage_service()
        for doc in documents:
            with storage.as_local_file(doc.file_path) as local_path:
                text = extract_text_from_file(local_path)
            if text:
                doc_text += text + "\n\n"

        # Call AI to generate checklist — thread the request's db session through so
        # parse_document_checklist can resolve the Anthropic API key from Platform
        # Settings (settings.anthropic_api_key env-var fallback is empty by design).
        items_data = await parse_document_checklist(tender_text, doc_text, db=db)

    # Clear existing auto-generated checklist items (keep manual ones AND items
    # owned by other agents — e.g., annexure_finder, whose rows are paired with
    # DocumentWorkspace and should not be wiped when the checklist is regenerated).
    db.query(ChecklistItem).filter(
        ChecklistItem.tender_id == tender_id,
        ChecklistItem.is_uploaded == False,
        ~ChecklistItem.source_section.like("annexure_finder:%"),
    ).delete(synchronize_session=False)
    db.flush()

    # Classify items before persisting
    classifications = await classify_checklist_items(
        db=db,
        checklist_items=items_data,
        tender_id=tender_id,
    )

    # Create new checklist items with classification data
    items = []
    for i, item_data in enumerate(items_data):
        cls = classifications[i] if i < len(classifications) else {}
        item = ChecklistItem(
            tender_id=tender_id,
            item_name=item_data.get("name", "Unknown Document"),
            item_description=item_data.get("description", ""),
            is_required=item_data.get("is_required", True),
            display_order=i,
            item_category=cls.get("category", "standard"),
            ai_instructions=cls.get("ai_instructions"),
            source_section=cls.get("source_section"),
            agent_key="checklist_generator",
        )
        db.add(item)
        items.append(item)

    db.commit()
    for item in items:
        db.refresh(item)

    return items


def get_checklist(db: Session, tender_id: int) -> list[ChecklistItem]:
    """Get all checklist items for a tender."""
    return db.query(ChecklistItem).filter(
        ChecklistItem.tender_id == tender_id,
    ).order_by(ChecklistItem.display_order).all()


def get_checklist_completion(db: Session, tender_id: int) -> dict:
    """Get checklist completion stats."""
    items = get_checklist(db, tender_id)
    total = len(items)
    uploaded = sum(1 for i in items if i.is_uploaded)
    required = sum(1 for i in items if i.is_required)
    required_uploaded = sum(1 for i in items if i.is_required and i.is_uploaded)
    return {
        "total": total,
        "uploaded": uploaded,
        "remaining": total - uploaded,
        "required": required,
        "required_uploaded": required_uploaded,
        "complete": required_uploaded >= required and required > 0,
    }


def upload_checklist_document(
    db: Session,
    tender_id: int,
    checklist_item_id: int,
    file_name: str,
    file_data: bytes,
    mime_type: str,
    user_id: int,
) -> TenderDocument:
    """Upload a document for a checklist item."""
    item = db.query(ChecklistItem).filter(
        ChecklistItem.id == checklist_item_id,
        ChecklistItem.tender_id == tender_id,
    ).first()
    if not item:
        raise ValueError("Checklist item not found")

    # Upload to storage (R2 or local) via the abstraction
    storage = get_storage_service()
    key = key_checklist_upload(tender_id, file_name)
    storage.upload_file_sync(key, file_data, content_type=mime_type)

    # Create document record (file_path stores the storage key, not a disk path)
    doc = TenderDocument(
        tender_id=tender_id,
        file_name=file_name,
        file_path=key,
        file_size=len(file_data),
        mime_type=mime_type,
        document_type="checklist_upload",
        checklist_item_id=checklist_item_id,
        uploaded_by=user_id,
    )
    db.add(doc)

    # Update checklist item
    item.is_uploaded = True
    item.document_id = doc.id

    db.commit()
    db.refresh(doc)

    # Fix: document_id needs the actual id after commit
    item.document_id = doc.id
    db.commit()

    return doc


def delete_checklist_document(db: Session, tender_id: int, checklist_item_id: int) -> bool:
    """Remove an uploaded document from a checklist item."""
    item = db.query(ChecklistItem).filter(
        ChecklistItem.id == checklist_item_id,
        ChecklistItem.tender_id == tender_id,
    ).first()
    if not item or not item.document_id:
        return False

    # Delete the document record and file
    doc = db.query(TenderDocument).filter(TenderDocument.id == item.document_id).first()
    if doc:
        if doc.file_path:
            try:
                get_storage_service().delete_file_sync(doc.file_path)
            except Exception:
                pass
        db.delete(doc)

    item.is_uploaded = False
    item.document_id = None
    db.commit()
    return True


def add_checklist_item(
    db: Session,
    tender_id: int,
    item_name: str,
    item_description: str = "",
    is_required: bool = True,
) -> ChecklistItem:
    """Manually add a checklist item."""
    max_order = db.query(ChecklistItem).filter(
        ChecklistItem.tender_id == tender_id,
    ).count()

    item = ChecklistItem(
        tender_id=tender_id,
        item_name=item_name,
        item_description=item_description,
        is_required=is_required,
        display_order=max_order,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_checklist_item(
    db: Session,
    tender_id: int,
    item_id: int,
    item_name: Optional[str] = None,
    item_description: Optional[str] = None,
    is_required: Optional[bool] = None,
) -> Optional[ChecklistItem]:
    """Update a checklist item."""
    item = db.query(ChecklistItem).filter(
        ChecklistItem.id == item_id,
        ChecklistItem.tender_id == tender_id,
    ).first()
    if not item:
        return None

    if item_name is not None:
        item.item_name = item_name
    if item_description is not None:
        item.item_description = item_description
    if is_required is not None:
        item.is_required = is_required

    db.commit()
    db.refresh(item)
    return item
