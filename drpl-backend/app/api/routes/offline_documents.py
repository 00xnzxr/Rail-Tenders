"""
DRPL Backend - Offline Document Signing Routes

Lets a user upload an offline PDF, place the signatures/stamps already
configured on the platform onto specific positions/pages, apply them, and
download the signed result. Reuses the existing signature-overlay engine and
the shared PDF rasteriser.

Persistence: rows live in `generated_documents` with `document_type="offline_upload"`.
`source_file_path` holds the original (clean) upload; signing always re-applies
from it so repeated re-signs never accumulate ghost overlays. `generated_file_path`
holds the latest signed output. `signatures` JSON holds the placement configs.
"""

import logging
import os
import re
import time

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.roles import require_surface
from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.letterhead import GeneratedDocument
from app.models.user import User
from app.services.pdf_generation_service import apply_signatures_to_pdf
from app.services.pdf_render_service import rasterize_pdf_pages, raster_response_headers
from app.services.storage_service import get_storage_service, key_offline_doc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/offline-documents", tags=["offline-documents"],
    # Role gate: `documents` is outside the costing_research surface
    # (app/core/roles.py). Enforced here rather than per-endpoint so a new
    # route on this router cannot be added ungated by accident.
    dependencies=[Depends(require_surface("documents"))])

DOC_TYPE = "offline_upload"
ALLOWED_EXT = {".pdf"}


def _max_upload_bytes(db: Session) -> int:
    """Upload cap in bytes: platform `max_upload_size_mb`, capped at 50MB."""
    try:
        from app.services.settings_service import get_setting_value
        configured = int(get_setting_value(db, "max_upload_size_mb", 50) or 50)
    except Exception:
        configured = 50
    return min(configured, 50) * 1024 * 1024


def _safe_filename(name: str) -> str:
    """Strip path parts and unsafe chars from an uploaded filename."""
    base = os.path.basename(name or "document.pdf")
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return base or "document.pdf"


def _owned_doc(db: Session, document_id: int, user: User) -> GeneratedDocument:
    """Fetch an offline document owned by the current user, or 404."""
    doc = (
        db.query(GeneratedDocument)
        .filter(
            GeneratedDocument.id == document_id,
            GeneratedDocument.document_type == DOC_TYPE,
            GeneratedDocument.created_by == user.id,
        )
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Offline document not found")
    return doc


def _serialize(doc: GeneratedDocument) -> dict:
    return {
        "id": doc.id,
        "title": doc.title,
        "document_type": doc.document_type,
        "status": doc.status,
        "signatures": doc.signatures or [],
        "has_signed_output": bool(doc.generated_file_path),
        "file_size": doc.file_size,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


@router.post("/upload")
async def upload_offline_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a source PDF and create a draft offline document."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    max_bytes = _max_upload_bytes(db)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {max_bytes // (1024 * 1024)}MB",
        )

    title = _safe_filename(file.filename)
    storage = get_storage_service()
    key = key_offline_doc(current_user.id, f"{int(time.time())}_{title}")
    storage.upload_file_sync(key, content, content_type="application/pdf")

    # Determine page count for the client (best-effort).
    page_count = None
    try:
        import pdfplumber
        import io as _io
        with pdfplumber.open(_io.BytesIO(content)) as pdf:
            page_count = len(pdf.pages)
    except Exception as e:
        logger.warning(f"[offline-docs] page count failed for {key}: {e}")

    doc = GeneratedDocument(
        title=title,
        document_type=DOC_TYPE,
        source_file_path=key,
        signatures=[],
        status="draft",
        file_size=len(content),
        created_by=current_user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    result = _serialize(doc)
    result["page_count"] = page_count
    return result


@router.get("/")
def list_offline_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the current user's offline documents, newest first."""
    docs = (
        db.query(GeneratedDocument)
        .filter(
            GeneratedDocument.document_type == DOC_TYPE,
            GeneratedDocument.created_by == current_user.id,
        )
        .order_by(GeneratedDocument.created_at.desc())
        .all()
    )
    return [_serialize(d) for d in docs]


@router.get("/{document_id}")
def get_offline_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a single offline document, including saved placement."""
    return _serialize(_owned_doc(db, document_id, current_user))


@router.get("/{document_id}/preview-pages.png")
def preview_offline_pages_png(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rasterise the SOURCE PDF to one tall PNG for the placement board."""
    doc = _owned_doc(db, document_id, current_user)
    if not doc.source_file_path:
        raise HTTPException(status_code=404, detail="Source PDF not found")

    try:
        pdf_bytes = get_storage_service().download_file_sync(doc.source_file_path)
    except Exception as e:
        logger.warning(f"[offline-docs] source download failed {doc.source_file_path}: {e}")
        raise HTTPException(status_code=404, detail="Source PDF missing from storage")

    try:
        raster = rasterize_pdf_pages(pdf_bytes, dpi=100)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return Response(
        content=raster.png_bytes,
        media_type="image/png",
        headers=raster_response_headers(raster),
    )


@router.patch("/{document_id}")
def update_offline_document(
    document_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Autosave placement configs (`signatures`) and/or `title`."""
    doc = _owned_doc(db, document_id, current_user)

    if "signatures" in body:
        sigs = body.get("signatures")
        if sigs is None:
            sigs = []
        if not isinstance(sigs, list):
            raise HTTPException(status_code=400, detail="`signatures` must be a list")
        doc.signatures = sigs

    if "title" in body and body.get("title"):
        doc.title = _safe_filename(str(body["title"]))

    db.commit()
    db.refresh(doc)
    return _serialize(doc)


@router.post("/{document_id}/apply")
def apply_offline_signatures(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stamp saved signatures onto the SOURCE PDF and store the signed output.

    Re-applies from the untouched source every time, so this is idempotent and
    safe to call repeatedly after repositioning.
    """
    doc = _owned_doc(db, document_id, current_user)
    if not doc.source_file_path:
        raise HTTPException(status_code=404, detail="Source PDF not found")

    storage = get_storage_service()
    try:
        source_bytes = storage.download_file_sync(doc.source_file_path)
    except Exception as e:
        logger.warning(f"[offline-docs] source download failed {doc.source_file_path}: {e}")
        raise HTTPException(status_code=404, detail="Source PDF missing from storage")

    configs = doc.signatures or []
    if not configs:
        raise HTTPException(status_code=400, detail="No signatures placed yet")

    signed_bytes = apply_signatures_to_pdf(db, source_bytes, configs)

    signed_name = f"signed_{os.path.splitext(doc.title)[0]}.pdf"
    signed_key = key_offline_doc(
        current_user.id, f"{int(time.time())}_{_safe_filename(signed_name)}"
    )
    storage.upload_file_sync(signed_key, signed_bytes, content_type="application/pdf")

    # Drop any previous signed output to avoid orphaned objects.
    if doc.generated_file_path and doc.generated_file_path != signed_key:
        try:
            storage.delete_file_sync(doc.generated_file_path)
        except Exception:
            pass

    doc.generated_file_path = signed_key
    doc.generated_file_name = signed_name
    doc.file_size = len(signed_bytes)
    doc.status = "signed"
    db.commit()
    db.refresh(doc)
    return _serialize(doc)


@router.get("/{document_id}/download")
def download_offline_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download the signed PDF (falls back to the source if not yet signed)."""
    doc = _owned_doc(db, document_id, current_user)
    key = doc.generated_file_path or doc.source_file_path
    if not key:
        raise HTTPException(status_code=404, detail="No file available")

    try:
        pdf_bytes = get_storage_service().download_file_sync(key)
    except Exception as e:
        logger.warning(f"[offline-docs] download failed {key}: {e}")
        raise HTTPException(status_code=404, detail="PDF file missing from storage")

    filename = doc.generated_file_name or doc.title
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{document_id}")
def delete_offline_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete an offline document and its storage objects."""
    doc = _owned_doc(db, document_id, current_user)
    storage = get_storage_service()
    for key in (doc.source_file_path, doc.generated_file_path):
        if key:
            try:
                storage.delete_file_sync(key)
            except Exception:
                pass
    db.delete(doc)
    db.commit()
    return {"success": True}
