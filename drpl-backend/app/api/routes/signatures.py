"""
DRPL Backend - Digital Signature Routes
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from typing import Optional
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.roles import require_surface
from app.core.auth import get_current_user, require_master_admin
from app.models.user import User
from app.services.signature_service import (
    create_signature, get_user_signatures, get_all_signatures, delete_signature,
)

router = APIRouter(prefix="/signatures", tags=["signatures"],
    # Role gate: `signatures` is outside the costing_research surface
    # (app/core/roles.py). Enforced here rather than per-endpoint so a new
    # route on this router cannot be added ungated by accident.
    dependencies=[Depends(require_surface("signatures"))])


@router.get("/")
def list_my_signatures(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List current user's signatures."""
    sigs = get_user_signatures(db, current_user.id)
    return [
        {
            "id": s.id, "name": s.name, "designation": s.designation,
            "default_position": s.default_position,
            "default_position_x": s.default_position_x,
            "default_position_y": s.default_position_y,
            "has_signature_image": bool(s.signature_image_path),
            "has_stamp_image": bool(s.stamp_image_path),
            "created_at": s.created_at,
        }
        for s in sigs
    ]


@router.get("/all")
def list_all_signatures(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List all signatures (admin only)."""
    sigs = get_all_signatures(db)
    return [
        {
            "id": s.id, "user_id": s.user_id, "name": s.name,
            "designation": s.designation, "default_position": s.default_position,
            "default_position_x": s.default_position_x,
            "default_position_y": s.default_position_y,
            "has_signature_image": bool(s.signature_image_path),
            "has_stamp_image": bool(s.stamp_image_path),
            "created_at": s.created_at,
        }
        for s in sigs
    ]


@router.get("/{signature_id}/image-data-uri")
def get_signature_image_data_uri(
    signature_id: int,
    kind: str = "signature",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a signature/stamp image as a base64 data URI for inline-embed
    inside the workspace editor (TipTap Image node). Used by the inline
    "Insert signature" toolbar dropdown — the embed is self-contained so the
    document HTML continues to render the same picture even if the
    signature record is later renamed or its image replaced.

    `kind` = "signature" (default) returns signature_image_path; "stamp"
    returns stamp_image_path. Falls back to the other if the requested one
    is missing.
    """
    import base64
    import mimetypes
    from app.models.letterhead import DigitalSignature
    from app.services.storage_service import get_storage_service

    sig = db.query(DigitalSignature).filter(
        DigitalSignature.id == signature_id,
        DigitalSignature.is_active == True,  # noqa: E712
    ).first()
    if not sig:
        raise HTTPException(status_code=404, detail="Signature not found")

    primary = sig.signature_image_path if kind != "stamp" else sig.stamp_image_path
    fallback = sig.stamp_image_path if kind != "stamp" else sig.signature_image_path
    path = primary or fallback
    if not path:
        raise HTTPException(status_code=404, detail="No image available for this signature")

    try:
        data = get_storage_service().download_file_sync(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch image: {e}")

    mime = mimetypes.guess_type(path)[0] or "image/png"
    return {
        "data_uri": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
        "name": sig.name,
        "designation": sig.designation,
        "mime": mime,
    }


@router.post("/")
async def create_new_signature(
    name: str = Form(...),
    designation: Optional[str] = Form(None),
    default_position: str = Form("bottom-right"),
    default_position_x: Optional[float] = Form(None),
    default_position_y: Optional[float] = Form(None),
    signature_image: Optional[UploadFile] = File(None),
    stamp_image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new digital signature with optional image uploads."""
    sig_data = None
    stamp_data = None

    if signature_image:
        sig_data = (await signature_image.read(), signature_image.filename)
    if stamp_image:
        stamp_data = (await stamp_image.read(), stamp_image.filename)

    sig = create_signature(
        db=db,
        user_id=current_user.id,
        name=name,
        designation=designation,
        default_position=default_position,
        default_position_x=default_position_x,
        default_position_y=default_position_y,
        signature_image=sig_data,
        stamp_image=stamp_data,
    )

    return {
        "id": sig.id, "name": sig.name, "designation": sig.designation,
        "status": "created",
    }


@router.delete("/{signature_id}")
def delete_existing_signature(
    signature_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete (deactivate) a signature."""
    if not delete_signature(db, signature_id, current_user.id):
        raise HTTPException(status_code=404, detail="Signature not found or not owned by you")
    return {"status": "deleted"}
