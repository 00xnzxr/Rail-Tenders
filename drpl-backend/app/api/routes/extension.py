"""
DRPL Backend - Extension API Routes
Endpoints consumed by the Chrome extension for data ingestion and config
"""

import os
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.core.database import get_db
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.models.user import User
from app.models.tender import Tender, TenderDocument, ScrapeLog
from app.schemas import (
    TenderBatchInput,
    BatchUploadResponse,
    ScrapeStatusInput,
    ExtensionConfigResponse,
    ExtensionStatusResponse,
)
from app.services.tender_service import ingest_tender_batch, log_scrape_session
from app.services.ai_service import check_eligibility, _build_tender_context
from app.services.scope_profile_service import (
    get_active_profile,
    serialize_for_extension,
)
from app.core.redis_client import get_queue

router = APIRouter(prefix="/extension", tags=["extension"])
settings = get_settings()


def _score_inline(tender_ids: list[int]) -> None:
    """Fallback scoring when no RQ queue (local dev). Opens its own session."""
    if not tender_ids:
        return
    from app.core.database import SessionLocal
    from app.services.auto_scoring_service import score_tenders_batch
    db = SessionLocal()
    try:
        score_tenders_batch(db, tender_ids)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("inline auto-scoring failed: %s", e)
    finally:
        db.close()


def _dispatch_scoring(new_ids: list[int], background_tasks) -> None:
    """Enqueue scoring on the worker when Redis is present; else run inline."""
    if not new_ids:
        return
    from app.core.config import get_settings
    if not get_settings().auto_scoring_enabled:
        return
    q = get_queue()
    if q is not None:
        try:
            q.enqueue("app.worker.auto_scoring_tasks.score_new_tenders", new_ids)
            return
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "auto-scoring RQ enqueue failed; falling back to inline", exc_info=True)
    if background_tasks is not None:
        background_tasks.add_task(_score_inline, new_ids)


def _fetch_nit_inline(new_ids: list[int]) -> None:
    """Inline fallback: fetch public NIT links for newly-ingested tenders."""
    from app.core.database import SessionLocal
    from app.services.nit_link_fetch_service import fetch_document_links_for_tender
    db = SessionLocal()
    try:
        for tid in new_ids:
            try:
                fetch_document_links_for_tender(db, tid)
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    f"eager NIT fetch failed for tender {tid}", exc_info=True)
    finally:
        db.close()


def _dispatch_nit_fetch(new_ids: list[int], background_tasks) -> None:
    """Enqueue eager public-link NIT fetch for new tenders (worker if Redis,
    else inline). Flag-gated; no-op when disabled or no new ids."""
    if not new_ids:
        return
    from app.core.config import get_settings
    if not get_settings().nit_link_fetch_enabled:
        return
    q = get_queue()
    if q is not None:
        try:
            q.enqueue("app.services.nit_link_fetch_service.fetch_links_for_tenders_job", new_ids)
            return
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "NIT fetch RQ enqueue failed; falling back to inline", exc_info=True)
    if background_tasks is not None:
        background_tasks.add_task(_fetch_nit_inline, new_ids)


@router.post("/tenders", response_model=BatchUploadResponse)
def upload_tenders(
    batch: TenderBatchInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Receive a batch of extracted tenders from the Chrome extension.
    Deduplicates and stores in the database.

    On ingest, schedules async relevance scoring against the configured
    Tender Scope Profile so the UI can surface only high-fit tenders.
    """
    try:
        result = ingest_tender_batch(db, batch.tenders, current_user.id)
        # Phase 7 — fan out scope-aware relevance + eligibility scoring
        new_ids = (result.new_ids or []) if hasattr(result, "new_ids") else []
        _dispatch_scoring(new_ids, background_tasks)
        _dispatch_nit_fetch(new_ids, background_tasks)
        return result
    except Exception as e:
        print(f"[DRPL] upload_tenders unhandled error: {e}")
        return BatchUploadResponse(
            received=len(batch.tenders),
            new=0,
            duplicates=0,
            errors=len(batch.tenders),
        )


def _embed_document_background(document_id: int, file_path: str, tender_id: int, file_name: str):
    """Background task: extract text from uploaded document and embed it via Voyage AI.

    `file_path` is a storage key (R2 object key or path under upload_dir).
    It is resolved to a temporary local file via StorageService.as_local_file.
    """
    import logging
    _logger = logging.getLogger(__name__)
    try:
        from app.core.database import SessionLocal
        from app.services.advanced_document_parser import extract_text_from_file_advanced
        from app.services.storage_service import get_storage_service

        db = SessionLocal()
        try:
            storage = get_storage_service()
            with storage.as_local_file(file_path) as local_path:
                result = extract_text_from_file_advanced(local_path)
            if result and result.get("pages"):
                from app.services.embedding_service import embed_document_pages
                count = embed_document_pages(
                    db=db,
                    document_id=document_id,
                    pages=result["pages"],
                    tender_id=tender_id,
                    source_name=file_name,
                )
                _logger.info(f"Background embedding complete: {file_name} -> {count} chunks")
            else:
                _logger.info(f"No text extracted from {file_name}, skipping embedding")
        finally:
            db.close()
    except Exception as e:
        _logger.warning(f"Background embedding failed for {file_name}: {e}")


@router.post("/documents")
async def upload_document(
    tender_id: str = Form(...),
    file: UploadFile = File(...),
    document_type: str = Form(None),
    portal: str = Form(None),
    source_url: str = Form(None),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a tender document (PDF/NIT) from the extension.

    `tender_id` may be either:
      - a numeric DB id (legacy callers), OR
      - the portal's tender id (e.g., "L9265359A") when `portal` is also
        provided. We then look up the Tender row by (portal, tender_id).

    Idempotent on (tender_id, file_name, file_size): repeated uploads of the
    same file (which happens routinely now that the auto-search chain re-walks
    every doc table on each run) are short-circuited so we don't pile up
    duplicate `tender_documents` rows or rewrite storage.
    """
    from app.services.storage_service import get_storage_service, key_tender_doc

    # Read file content once
    file_name = file.filename or "document.pdf"
    content = await file.read()

    # Resolve to a DB tender id. If the caller passed a non-numeric portal
    # tender_id, look the row up by (portal, tender_id). Without this, the
    # legacy `tid = int(...) if isdigit() else 0` path silently wrote every
    # document against tender 0.
    tid = 0
    if tender_id.isdigit():
        tid = int(tender_id)
    elif portal:
        row = (
            db.query(Tender)
            .filter(Tender.portal == portal, Tender.tender_id == tender_id)
            .first()
        )
        if row:
            tid = row.id
        else:
            raise HTTPException(
                status_code=404,
                detail=f"No tender found for portal={portal} tender_id={tender_id}",
            )

    # Skip if we already have this (tender, source_url) — a re-scrape
    # re-downloads the exact same URL, so this is a cheaper and more
    # reliable dedup signal than the size-based check below.
    if source_url:
        by_url = (
            db.query(TenderDocument)
            .filter(
                TenderDocument.tender_id == tid,
                TenderDocument.source_url == source_url,
            )
            .first()
        )
        if by_url:
            return {
                "success": True,
                "file_name": file_name,
                "size": len(content),
                "deduplicated": True,
                "document_id": by_url.id,
            }

    # Skip if we already have this (tender, file_name, file_size). Size match
    # gives us cheap byte-level confidence without hashing — sufficient since
    # GeM's portal serves the same blob at a stable URL.
    existing = (
        db.query(TenderDocument)
        .filter(
            TenderDocument.tender_id == tid,
            TenderDocument.file_name == file_name,
            TenderDocument.file_size == len(content),
        )
        .first()
    )
    if existing:
        return {
            "success": True,
            "file_name": file_name,
            "size": len(content),
            "deduplicated": True,
            "document_id": existing.id,
        }

    # Upload to storage (R2 or local) under a deterministic key
    storage_key = key_tender_doc(tid, file_name)
    get_storage_service().upload_file_sync(
        storage_key, content,
        content_type=file.content_type or "application/pdf",
    )

    # Record in database
    doc = TenderDocument(
        tender_id=tid,
        file_name=file_name,
        file_path=storage_key,
        file_size=len(content),
        mime_type=file.content_type or "application/pdf",
        document_type=document_type or "other",
        source_url=source_url,
        uploaded_by=current_user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Trigger background embedding
    if background_tasks:
        background_tasks.add_task(
            _embed_document_background, doc.id, storage_key, tid, doc.file_name,
        )

        # Extract and download linked documents from PDFs
        if file_name.lower().endswith(".pdf"):
            from app.services.document_link_service import process_document_links_background
            background_tasks.add_task(
                process_document_links_background,
                source_type="tender_document",
                source_id=doc.id,
                file_path=storage_key,
                tender_id=tid,
                session_id=None,
                user_id=current_user.id,
            )

    return {"success": True, "file_name": file.filename, "size": len(content)}


@router.post("/status")
def report_scrape_status(
    status: ScrapeStatusInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Receive scraping session status reports from the extension."""
    log = log_scrape_session(db, current_user.id, status)
    return {"success": True, "log_id": log.id}


@router.get("/config", response_model=ExtensionConfigResponse)
def get_extension_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return current extension configuration.
    The extension calls this on startup and periodically to check for updates.

    The `scope_profile` block is consumed by the GeM auto-search driver and
    surfaces in the popup KeywordPanel.
    """
    settings = get_settings()
    profile = get_active_profile(db)
    return ExtensionConfigResponse(
        selectors_version="1.1.0",
        scrape_interval_minutes=360,
        enabled_portals=["ireps", "gem", "tendertiger", "bidassist"],
        features={
            "passive_mode": True,
            "guided_scrape": True,
            "scheduled_scrape": True,
            "document_download": True,
            "gem_auto_search": True,
            "ireps_blue_tick_only": True,
            "auto_capture": settings.extension_auto_capture_enabled,
        },
        scope_profile=serialize_for_extension(profile, db=db),
    )


@router.get("/selectors/{portal}")
def get_portal_selectors(
    portal: str,
    current_user: User = Depends(get_current_user),
):
    """
    Return the latest CSS/XPath selector configuration for a specific portal.
    This enables OTA selector updates without re-deploying the extension.
    """
    selectors_file = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "selector_configs", f"{portal}.json"
    )

    if os.path.exists(selectors_file):
        with open(selectors_file) as f:
            return json.load(f)

    # Return default selectors from the bundled config
    raise HTTPException(status_code=404, detail=f"No selector config found for portal: {portal}")


@router.get("/status/live", response_model=ExtensionStatusResponse)
def get_extension_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Real-time extension connection status for the dashboard."""
    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)

    # Last completed scrape session
    last_scrape = db.query(ScrapeLog).filter(
        ScrapeLog.user_id == current_user.id,
        ScrapeLog.status == "completed",
    ).order_by(ScrapeLog.started_at.desc()).first()

    # Active portals (portals with activity in last 7 days)
    last_7d = now - timedelta(days=7)
    active_portals_query = db.query(ScrapeLog.portal).filter(
        ScrapeLog.user_id == current_user.id,
        ScrapeLog.started_at >= last_7d,
    ).distinct().all()
    active_portals = [p[0] for p in active_portals_query]

    # Tenders uploaded in last 24h
    tenders_24h = db.query(Tender).filter(
        Tender.extracted_by == current_user.id,
        Tender.created_at >= last_24h,
    ).count()

    # Connected = any activity within last 24h
    connected = last_scrape is not None and last_scrape.started_at >= last_24h

    return ExtensionStatusResponse(
        connected=connected,
        last_sync=last_scrape.started_at if last_scrape else None,
        active_portals=active_portals,
        tenders_uploaded_24h=tenders_24h,
    )


@router.post("/check-eligibility")
async def check_tender_eligibility(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Check AI-based eligibility for a tender."""
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Tender not found")

    tender_text = _build_tender_context(tender)
    result = await check_eligibility(tender_text)

    # Update tender with eligibility info
    tender.eligibility_status = "eligible" if result["eligible"] else "not_eligible"
    tender.eligibility_score = result["score"]
    tender.eligibility_notes = result["notes"]
    db.commit()

    return result
