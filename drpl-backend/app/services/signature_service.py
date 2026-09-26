"""
DRPL Backend - Digital Signature Service
CRUD for digital signatures with image management (Cloudflare R2)
"""

import mimetypes
import os
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.letterhead import DigitalSignature
from app.services.storage_service import get_storage_service

logger = logging.getLogger(__name__)


def _signature_key(user_id: int, sig_id: int, filename: str) -> str:
    ext = os.path.splitext(filename)[1] or ".png"
    return f"signatures/{user_id}/sig_{sig_id}{ext}"


def _stamp_key(user_id: int, sig_id: int, filename: str) -> str:
    ext = os.path.splitext(filename)[1] or ".png"
    return f"signatures/{user_id}/stamp_{sig_id}{ext}"


def _guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "image/png"


def create_signature(
    db: Session,
    user_id: int,
    name: str,
    designation: Optional[str] = None,
    default_position: str = "bottom-right",
    default_position_x: Optional[float] = None,
    default_position_y: Optional[float] = None,
    signature_image: Optional[tuple[bytes, str]] = None,
    stamp_image: Optional[tuple[bytes, str]] = None,
) -> DigitalSignature:
    """Create a new digital signature."""
    sig = DigitalSignature(
        user_id=user_id,
        name=name,
        designation=designation,
        default_position=default_position,
        default_position_x=default_position_x,
        default_position_y=default_position_y,
        created_by=user_id,
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)

    storage = get_storage_service()

    if signature_image:
        data, filename = signature_image
        key = _signature_key(user_id, sig.id, filename)
        storage.upload_file_sync(key, data, content_type=_guess_mime(filename))
        sig.signature_image_path = key

    if stamp_image:
        data, filename = stamp_image
        key = _stamp_key(user_id, sig.id, filename)
        storage.upload_file_sync(key, data, content_type=_guess_mime(filename))
        sig.stamp_image_path = key

    db.commit()
    db.refresh(sig)
    return sig


def get_user_signatures(db: Session, user_id: int) -> list[DigitalSignature]:
    """Get all signatures for a user."""
    return db.query(DigitalSignature).filter(
        DigitalSignature.user_id == user_id,
        DigitalSignature.is_active == True,
    ).order_by(DigitalSignature.name).all()


def get_all_signatures(db: Session) -> list[DigitalSignature]:
    """Get all signatures (admin view)."""
    return db.query(DigitalSignature).filter(
        DigitalSignature.is_active == True,
    ).order_by(DigitalSignature.user_id, DigitalSignature.name).all()


def get_signature(db: Session, signature_id: int) -> Optional[DigitalSignature]:
    """Get a single signature."""
    return db.query(DigitalSignature).filter(DigitalSignature.id == signature_id).first()


def delete_signature(db: Session, signature_id: int, user_id: int) -> bool:
    """Soft-delete a signature (mark inactive)."""
    sig = db.query(DigitalSignature).filter(
        DigitalSignature.id == signature_id,
        DigitalSignature.user_id == user_id,
    ).first()
    if not sig:
        return False

    sig.is_active = False
    db.commit()
    return True
