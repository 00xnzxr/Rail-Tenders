"""
DRPL Backend - Template Service
CRUD operations for ProposalTemplate, DocumentFormatTemplate, CostingTemplate,
plus unified listing across all template types.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.proposal_template import ProposalTemplate
from app.models.workspace import DocumentFormatTemplate
from app.models.costing_template import CostingTemplate
from app.services.template_parser import parse_uploaded_template

logger = logging.getLogger(__name__)
settings = get_settings()


def list_templates(db: Session) -> list[ProposalTemplate]:
    """List all proposal templates (active first, then by name)."""
    return db.query(ProposalTemplate).order_by(
        ProposalTemplate.is_active.desc(),
        ProposalTemplate.name,
    ).all()


def get_template(db: Session, template_id: int) -> Optional[ProposalTemplate]:
    """Get a single proposal template by ID."""
    return db.query(ProposalTemplate).filter(ProposalTemplate.id == template_id).first()


async def upload_and_parse_template(
    db: Session,
    file_name: str,
    file_data: bytes,
    user_id: int,
) -> ProposalTemplate:
    """
    Save an uploaded PDF to uploads/templates/, run AI parsing,
    and store the resulting ProposalTemplate record.
    """
    # Save file to disk
    upload_dir = os.path.join(settings.upload_dir, "templates")
    os.makedirs(upload_dir, exist_ok=True)

    # Sanitize file name and make unique
    safe_name = file_name.replace(" ", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    saved_name = f"{timestamp}_{safe_name}"
    file_path = os.path.join(upload_dir, saved_name)

    with open(file_path, "wb") as f:
        f.write(file_data)

    # Parse with AI
    structure = await parse_uploaded_template(file_path, db)
    metadata = structure.get("metadata", {})

    # Create template record
    template = ProposalTemplate(
        name=os.path.splitext(file_name)[0],
        description=f"Uploaded template: {file_name}",
        template_type=metadata.get("template_type", "custom"),
        source="uploaded",
        structure_json=structure,
        original_file_path=file_path,
        original_file_name=file_name,
        railway_zone=metadata.get("railway_zone"),
        contract_type=metadata.get("contract_type"),
        bidding_system=metadata.get("bidding_system"),
        is_active=True,
        created_by=user_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)

    return template


def update_template(db: Session, template_id: int, data: dict) -> Optional[ProposalTemplate]:
    """Update a proposal template's metadata."""
    template = db.query(ProposalTemplate).filter(ProposalTemplate.id == template_id).first()
    if not template:
        return None

    allowed_fields = (
        "name", "description", "template_type", "railway_zone",
        "contract_type", "bidding_system", "is_active", "structure_json",
    )
    for key, value in data.items():
        if key in allowed_fields and hasattr(template, key):
            setattr(template, key, value)

    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(template)
    return template


def deactivate_template(db: Session, template_id: int) -> bool:
    """Delete a template permanently."""
    template = db.query(ProposalTemplate).filter(ProposalTemplate.id == template_id).first()
    if not template:
        return False

    # Remove uploaded file if it exists
    if template.original_file_path and os.path.exists(template.original_file_path):
        try:
            os.remove(template.original_file_path)
        except OSError:
            pass

    db.delete(template)
    db.commit()
    return True


# ── Unified Template Listing ────────────────────────────────────────────────

def _summarize_proposal(t: ProposalTemplate) -> dict:
    """Build a unified template dict from a ProposalTemplate."""
    structure = t.structure_json or {}
    sections = structure.get("sections", [])
    field_count = sum(len(s.get("fields", [])) for s in sections) if isinstance(sections, list) else 0
    return {
        "id": t.id,
        "template_kind": "proposal",
        "name": t.name,
        "description": t.description,
        "category": t.template_type,
        "source": t.source or "uploaded",
        "is_system": t.source == "system",
        "is_active": t.is_active,
        "output_format": getattr(t, "output_format", None) or "docx",
        "original_file_name": t.original_file_name,
        "structure_summary": {
            "sections": len(sections) if isinstance(sections, list) else 0,
            "fields": field_count,
        },
        "metadata": {
            "railway_zone": t.railway_zone,
            "contract_type": t.contract_type,
            "bidding_system": t.bidding_system,
            "template_type": t.template_type,
        },
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _summarize_document(t: DocumentFormatTemplate) -> dict:
    """Build a unified template dict from a DocumentFormatTemplate."""
    structure = t.structure_json or {}
    sections = structure.get("sections", [])
    return {
        "id": t.id,
        "template_kind": "document",
        "name": t.name,
        "description": t.description,
        "category": t.document_category,
        "source": "system" if t.is_system else "user",
        "is_system": t.is_system,
        "is_active": t.is_active,
        "output_format": getattr(t, "output_format", None) or "docx",
        "original_file_name": getattr(t, "original_file_name", None),
        "structure_summary": {
            "sections": len(sections) if isinstance(sections, list) else 0,
            "rules": len(t.format_rules) if t.format_rules else 0,
            "patterns": len(t.match_patterns) if t.match_patterns else 0,
        },
        "metadata": {
            "document_category": t.document_category,
            "match_patterns": t.match_patterns or [],
            "format_rules": t.format_rules or [],
            "required_sections": t.required_sections or [],
        },
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _summarize_costing(t: CostingTemplate) -> dict:
    """Build a unified template dict from a CostingTemplate."""
    cols = t.column_definitions or []
    footers = t.footer_rows or []
    return {
        "id": t.id,
        "template_kind": "costing",
        "name": t.name,
        "description": t.description,
        "category": t.format_type,
        "source": "system" if t.is_default else "user",
        "is_system": t.is_default,
        "is_active": True,
        "output_format": getattr(t, "output_format", None) or "xlsx",
        "original_file_name": getattr(t, "original_file_name", None),
        "structure_summary": {
            "columns": len(cols),
            "footer_rows": len(footers),
        },
        "metadata": {
            "zone": t.zone,
            "format_type": t.format_type,
            "column_definitions": cols,
            "footer_rows": footers,
        },
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def list_unified_templates(
    db: Session,
    kind: Optional[str] = None,
    category: Optional[str] = None,
    zone: Optional[str] = None,
) -> list[dict]:
    """List templates across all three template tables, optionally filtered."""
    results: list[dict] = []

    # Proposal templates
    if not kind or kind == "proposal":
        query = db.query(ProposalTemplate)
        if category:
            query = query.filter(ProposalTemplate.template_type == category)
        for t in query.order_by(ProposalTemplate.name).all():
            results.append(_summarize_proposal(t))

    # Document format templates
    if not kind or kind == "document":
        query = db.query(DocumentFormatTemplate).filter(DocumentFormatTemplate.is_active == True)
        if category:
            query = query.filter(DocumentFormatTemplate.document_category == category)
        for t in query.order_by(DocumentFormatTemplate.is_system.desc(), DocumentFormatTemplate.name).all():
            results.append(_summarize_document(t))

    # Costing templates
    if not kind or kind == "costing":
        query = db.query(CostingTemplate)
        if zone:
            query = query.filter(CostingTemplate.zone == zone)
        if category:
            query = query.filter(CostingTemplate.format_type == category)
        for t in query.order_by(CostingTemplate.name).all():
            results.append(_summarize_costing(t))

    return results


# ── DocumentFormatTemplate CRUD ─────────────────────────────────────────────

def list_document_templates(db: Session) -> list[DocumentFormatTemplate]:
    """List all document format templates."""
    return db.query(DocumentFormatTemplate).filter(
        DocumentFormatTemplate.is_active == True,
    ).order_by(
        DocumentFormatTemplate.is_system.desc(),
        DocumentFormatTemplate.name,
    ).all()


def get_document_template(db: Session, template_id: int) -> Optional[DocumentFormatTemplate]:
    return db.query(DocumentFormatTemplate).filter(DocumentFormatTemplate.id == template_id).first()


def create_document_template(db: Session, data: dict, user_id: int) -> DocumentFormatTemplate:
    """Create a user-defined document format template."""
    template = DocumentFormatTemplate(
        name=data["name"],
        description=data.get("description"),
        document_category=data.get("document_category", "custom"),
        structure_json=data.get("structure_json"),
        content_template_markdown=data.get("content_template_markdown"),
        content_template_html=data.get("content_template_html"),
        format_rules=data.get("format_rules", []),
        required_sections=data.get("required_sections", []),
        match_patterns=data.get("match_patterns", []),
        output_format=data.get("output_format", "docx"),
        is_system=False,
        is_active=True,
        created_by=user_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def update_document_template(db: Session, template_id: int, data: dict) -> Optional[DocumentFormatTemplate]:
    """Update a document format template (system templates are read-only)."""
    template = db.query(DocumentFormatTemplate).filter(DocumentFormatTemplate.id == template_id).first()
    if not template:
        return None
    if template.is_system:
        raise ValueError("System templates cannot be modified")

    allowed = (
        "name", "description", "document_category", "structure_json",
        "content_template_markdown", "content_template_html", "format_rules",
        "required_sections", "match_patterns", "is_active", "output_format",
    )
    for key, value in data.items():
        if key in allowed:
            setattr(template, key, value)

    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(template)
    return template


def delete_document_template(db: Session, template_id: int) -> bool:
    """Soft-delete a document format template (system templates cannot be deleted)."""
    template = db.query(DocumentFormatTemplate).filter(DocumentFormatTemplate.id == template_id).first()
    if not template:
        return False
    if template.is_system:
        raise ValueError("System templates cannot be deleted")

    template.is_active = False
    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True


# ── CostingTemplate CRUD ────────────────────────────────────────────────────

def list_costing_templates(db: Session, zone: Optional[str] = None) -> list[CostingTemplate]:
    """List all costing templates, optionally filtered by zone."""
    query = db.query(CostingTemplate)
    if zone:
        query = query.filter(CostingTemplate.zone == zone)
    return query.order_by(CostingTemplate.zone, CostingTemplate.name).all()


def get_costing_template(db: Session, template_id: int) -> Optional[CostingTemplate]:
    return db.query(CostingTemplate).filter(CostingTemplate.id == template_id).first()


def create_costing_template(db: Session, data: dict, user_id: int) -> CostingTemplate:
    """Create a costing template."""
    template = CostingTemplate(
        name=data["name"],
        zone=data["zone"],
        format_type=data.get("format_type", "boq"),
        description=data.get("description"),
        column_definitions=data.get("column_definitions", []),
        footer_rows=data.get("footer_rows", []),
        html_template=data.get("html_template"),
        markdown_template=data.get("markdown_template"),
        output_format="xlsx",
        is_default=False,
        created_by=user_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def update_costing_template(db: Session, template_id: int, data: dict) -> Optional[CostingTemplate]:
    """Update a costing template."""
    template = db.query(CostingTemplate).filter(CostingTemplate.id == template_id).first()
    if not template:
        return None

    allowed = (
        "name", "zone", "format_type", "description", "column_definitions",
        "footer_rows", "html_template", "markdown_template", "is_default",
    )
    for key, value in data.items():
        if key in allowed:
            setattr(template, key, value)

    template.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(template)
    return template


def delete_costing_template(db: Session, template_id: int) -> bool:
    """Delete a costing template."""
    template = db.query(CostingTemplate).filter(CostingTemplate.id == template_id).first()
    if not template:
        return False
    db.delete(template)
    db.commit()
    return True


# ── Unified Upload ──────────────────────────────────────────────────────────

async def upload_and_parse_unified(
    db: Session,
    file_name: str,
    file_data: bytes,
    user_id: int,
    template_kind: str,
    category: Optional[str] = None,
) -> dict:
    """Upload a template file (PDF, DOCX, XLSX) and parse it based on template_kind."""
    upload_dir = os.path.join(settings.upload_dir, "templates")
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = file_name.replace(" ", "_")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    saved_name = f"{timestamp}_{safe_name}"
    file_path = os.path.join(upload_dir, saved_name)

    with open(file_path, "wb") as f:
        f.write(file_data)

    ext = os.path.splitext(file_name)[1].lower()

    if template_kind == "proposal":
        return await _upload_proposal_template(db, file_name, file_path, user_id)
    elif template_kind == "document":
        return await _upload_document_template(db, file_name, file_path, ext, user_id, category)
    elif template_kind == "costing":
        return await _upload_costing_template(db, file_name, file_path, ext, user_id)
    else:
        raise ValueError(f"Unknown template_kind: {template_kind}")


async def _upload_proposal_template(db: Session, file_name: str, file_path: str, user_id: int) -> dict:
    """Parse and create a ProposalTemplate from an uploaded file."""
    from app.services.template_parser import parse_template_file
    structure = await parse_template_file(file_path, "proposal", db)
    metadata = structure.get("metadata", {})

    template = ProposalTemplate(
        name=os.path.splitext(file_name)[0],
        description=f"Uploaded template: {file_name}",
        template_type=metadata.get("template_type", "custom"),
        source="uploaded",
        structure_json=structure,
        original_file_path=file_path,
        original_file_name=file_name,
        railway_zone=metadata.get("railway_zone"),
        contract_type=metadata.get("contract_type"),
        bidding_system=metadata.get("bidding_system"),
        is_active=True,
        created_by=user_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _summarize_proposal(template)


async def _upload_document_template(
    db: Session, file_name: str, file_path: str, ext: str, user_id: int, category: Optional[str],
) -> dict:
    """Parse and create a DocumentFormatTemplate from an uploaded file."""
    from app.services.template_parser import parse_template_file
    structure = await parse_template_file(file_path, "document", db)
    metadata = structure.get("metadata", {})

    doc_category = category or metadata.get("document_category", "custom")
    output_format = "xlsx" if doc_category == "boq" else "docx"

    template = DocumentFormatTemplate(
        name=os.path.splitext(file_name)[0],
        description=f"Uploaded template: {file_name}",
        document_category=doc_category,
        structure_json=structure,
        content_template_markdown=structure.get("content_template_markdown"),
        format_rules=metadata.get("format_rules", []),
        required_sections=metadata.get("required_sections", []),
        match_patterns=metadata.get("match_patterns", []),
        output_format=output_format,
        original_file_path=file_path,
        original_file_name=file_name,
        is_system=False,
        is_active=True,
        created_by=user_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _summarize_document(template)


async def _upload_costing_template(
    db: Session, file_name: str, file_path: str, ext: str, user_id: int,
) -> dict:
    """Parse and create a CostingTemplate from an uploaded file (XLSX preferred)."""
    from app.services.template_parser import parse_template_file
    structure = await parse_template_file(file_path, "costing", db)
    metadata = structure.get("metadata", {})

    template = CostingTemplate(
        name=os.path.splitext(file_name)[0],
        zone=metadata.get("zone", "GENERAL"),
        format_type=metadata.get("format_type", "boq"),
        description=f"Uploaded template: {file_name}",
        column_definitions=structure.get("column_definitions", []),
        footer_rows=structure.get("footer_rows", []),
        sample_pdf_path=file_path if ext == ".pdf" else None,
        output_format="xlsx",
        original_file_name=file_name,
        is_default=False,
        created_by=user_id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _summarize_costing(template)


# ── Template Preview ────────────────────────────────────────────────────────

def get_template_preview(db: Session, kind: str, template_id: int) -> Optional[dict]:
    """Get a rich preview for a template based on its kind."""
    if kind == "proposal":
        t = get_template(db, template_id)
        if not t:
            return None
        structure = t.structure_json or {}
        sections = structure.get("sections", [])
        return {
            "kind": "proposal",
            "name": t.name,
            "sections": [
                {
                    "title": s.get("title", f"Section {s.get('order', i+1)}"),
                    "description": s.get("description", ""),
                    "fields": s.get("fields", []),
                    "tables": s.get("tables", []),
                }
                for i, s in enumerate(sections) if isinstance(s, dict)
            ],
            "metadata": structure.get("metadata", {}),
        }

    elif kind == "document":
        t = get_document_template(db, template_id)
        if not t:
            return None
        return {
            "kind": "document",
            "name": t.name,
            "category": t.document_category,
            "content_template": t.content_template_markdown,
            "structure": t.structure_json,
            "format_rules": t.format_rules or [],
            "required_sections": t.required_sections or [],
            "match_patterns": t.match_patterns or [],
        }

    elif kind == "costing":
        t = get_costing_template(db, template_id)
        if not t:
            return None
        return {
            "kind": "costing",
            "name": t.name,
            "zone": t.zone,
            "format_type": t.format_type,
            "columns": t.column_definitions or [],
            "footer_rows": t.footer_rows or [],
            "sample_table": _build_costing_sample_table(t),
        }

    return None


def _build_costing_sample_table(t: CostingTemplate) -> list[list[str]]:
    """Build a sample table representation from costing template columns."""
    cols = t.column_definitions or []
    if not cols:
        return []
    headers = [c.get("label", c.get("name", "")) for c in cols]
    sample_row = []
    for c in cols:
        col_type = c.get("type", "text")
        if col_type in ("serial", "number"):
            sample_row.append("1")
        elif col_type in ("currency",):
            sample_row.append("₹ 0.00")
        elif col_type == "formula":
            sample_row.append(f"={c.get('formula', 'SUM()')}")
        else:
            sample_row.append("...")
    return [headers, sample_row]
