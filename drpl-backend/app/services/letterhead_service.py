"""
DRPL Backend - Letterhead Template Service
CRUD for letterhead templates and asset management (Cloudflare R2)
"""

import mimetypes
import os
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.letterhead import LetterheadTemplate
from app.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)


_LETTERHEAD_CACHE_PREFIX = "drpl:cache:letterhead_templates"


def _invalidate_letterhead_cache() -> None:
    """Drop all cached /templates responses. Called after any template mutation."""
    try:
        from app.core.redis_client import cache_delete_prefix
        cache_delete_prefix(_LETTERHEAD_CACHE_PREFIX)
    except Exception as e:
        logger.debug(f"letterhead cache invalidate: {e}")


def _asset_key(template_id: int, asset_type: str, file_name: str) -> str:
    ext = os.path.splitext(file_name)[1]
    return f"letterheads/{template_id}/{asset_type}{ext}"


def _letterhead_pdf_key(template_id: int, file_name: str) -> str:
    ext = os.path.splitext(file_name)[1] or ".pdf"
    return f"letterheads/{template_id}/letterhead{ext}"


def _guess_mime(file_name: str, fallback: str = "application/octet-stream") -> str:
    return mimetypes.guess_type(file_name)[0] or fallback


def create_template(db: Session, data: dict, user_id: int) -> LetterheadTemplate:
    """Create a new letterhead template."""
    template = LetterheadTemplate(
        name=data.get("name", "Untitled Template"),
        description=data.get("description"),
        is_default=data.get("is_default", False),
        margin_top_mm=data.get("margin_top_mm", 30.0),
        margin_bottom_mm=data.get("margin_bottom_mm", 25.0),
        margin_left_mm=data.get("margin_left_mm", 20.0),
        margin_right_mm=data.get("margin_right_mm", 20.0),
        page_size=data.get("page_size", "A4"),
        font_family=data.get("font_family", "Times New Roman"),
        font_size_pt=data.get("font_size_pt", 12),
        line_height=data.get("line_height", 1.5),
        header_html=data.get("header_html"),
        footer_html=data.get("footer_html"),
        css_overrides=data.get("css_overrides"),
        company_name_override=data.get("company_name_override"),
        logo_position=data.get("logo_position", "top-left"),
        watermark_opacity=data.get("watermark_opacity", 0.1),
        created_by=user_id,
    )

    # If marking as default, unset other defaults
    if template.is_default:
        db.query(LetterheadTemplate).filter(
            LetterheadTemplate.is_default == True
        ).update({"is_default": False})

    db.add(template)
    db.commit()
    db.refresh(template)
    _invalidate_letterhead_cache()
    return template


def update_template(db: Session, template_id: int, data: dict) -> Optional[LetterheadTemplate]:
    """Update a letterhead template."""
    template = db.query(LetterheadTemplate).filter(LetterheadTemplate.id == template_id).first()
    if not template:
        return None

    updatable_fields = [
        "name", "description", "is_default", "margin_top_mm", "margin_bottom_mm",
        "margin_left_mm", "margin_right_mm", "page_size", "font_family", "font_size_pt",
        "line_height", "header_html", "footer_html", "css_overrides",
        "company_name_override", "logo_position", "watermark_opacity", "is_active",
    ]

    for field in updatable_fields:
        if field in data:
            setattr(template, field, data[field])

    # Handle default toggle
    if data.get("is_default"):
        db.query(LetterheadTemplate).filter(
            LetterheadTemplate.id != template_id,
            LetterheadTemplate.is_default == True,
        ).update({"is_default": False})

    db.commit()
    db.refresh(template)
    _invalidate_letterhead_cache()
    return template


def upload_asset(db: Session, template_id: int, asset_type: str, file_data: bytes, file_name: str) -> str:
    """Upload a letterhead asset (header, footer, watermark, logo) to R2."""
    valid_types = ["header", "footer", "watermark", "logo"]
    if asset_type not in valid_types:
        raise ValueError(f"Invalid asset type. Must be one of: {valid_types}")

    template = db.query(LetterheadTemplate).filter(LetterheadTemplate.id == template_id).first()
    if not template:
        raise ValueError(f"Template {template_id} not found")

    field_map = {
        "header": "header_image_path",
        "footer": "footer_image_path",
        "watermark": "watermark_image_path",
        "logo": "logo_path",
    }

    key = _asset_key(template_id, asset_type, file_name)
    storage = get_storage_service()

    old_key = getattr(template, field_map[asset_type], None)
    storage.upload_file_sync(key, file_data, content_type=_guess_mime(file_name, "image/png"))

    if old_key and old_key != key:
        try:
            storage.delete_file_sync(old_key)
        except Exception as e:
            logger.warning(f"[letterhead] failed to delete superseded asset {old_key}: {e}")

    setattr(template, field_map[asset_type], key)
    db.commit()
    _invalidate_letterhead_cache()

    return key


def upload_letterhead_pdf(db: Session, template_id: int, file_data: bytes, file_name: str) -> str:
    """Upload a full PDF letterhead to R2."""
    template = db.query(LetterheadTemplate).filter(LetterheadTemplate.id == template_id).first()
    if not template:
        raise ValueError(f"Template {template_id} not found")

    key = _letterhead_pdf_key(template_id, file_name)
    storage = get_storage_service()

    old_key = template.letterhead_pdf_path
    storage.upload_file_sync(key, file_data, content_type="application/pdf")

    if old_key and old_key != key:
        try:
            storage.delete_file_sync(old_key)
        except Exception as e:
            logger.warning(f"[letterhead] failed to delete superseded PDF {old_key}: {e}")

    template.letterhead_pdf_path = key
    db.commit()
    _invalidate_letterhead_cache()

    return key


def get_templates(db: Session, active_only: bool = False) -> list[LetterheadTemplate]:
    """List all letterhead templates."""
    query = db.query(LetterheadTemplate)
    if active_only:
        query = query.filter(LetterheadTemplate.is_active == True)
    return query.order_by(LetterheadTemplate.is_default.desc(), LetterheadTemplate.name).all()


def get_template(db: Session, template_id: int) -> Optional[LetterheadTemplate]:
    """Get a single letterhead template."""
    return db.query(LetterheadTemplate).filter(LetterheadTemplate.id == template_id).first()


def get_default_template(db: Session) -> Optional[LetterheadTemplate]:
    """Get the default letterhead template."""
    return db.query(LetterheadTemplate).filter(
        LetterheadTemplate.is_default == True,
        LetterheadTemplate.is_active == True,
    ).first()


def delete_template(db: Session, template_id: int) -> bool:
    """Delete a letterhead template and its R2 assets."""
    template = db.query(LetterheadTemplate).filter(LetterheadTemplate.id == template_id).first()
    if not template:
        return False

    storage = get_storage_service()
    for attr in (
        "letterhead_pdf_path",
        "header_image_path",
        "footer_image_path",
        "watermark_image_path",
        "logo_path",
    ):
        key = getattr(template, attr, None)
        if not key:
            continue
        try:
            storage.delete_file_sync(key)
        except Exception as e:
            logger.warning(f"[letterhead] failed to delete {attr}={key}: {e}")

    db.delete(template)
    db.commit()
    _invalidate_letterhead_cache()
    return True
