"""
DRPL Backend - Document Analysis Routes
Endpoints for deep document analysis, critical clause detection, and accuracy feedback
"""

import asyncio
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal, get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.models.document_analysis import (
    DocumentExtractionResult,
    CriticalClauseFlag,
    TenderAnalysisSummary,
)
from app.services.tender_analysis_service import (
    analyze_all_documents,
    submit_feedback,
    get_accuracy_stats,
)

logger = logging.getLogger(__name__)
_settings = get_settings()

router = APIRouter(prefix="/tenders/{tender_id}/analysis", tags=["document-analysis"])

# Separate router for batch operations (not bound to a single tender_id path param).
batch_router = APIRouter(prefix="/tenders/analysis", tags=["document-analysis"])


@router.post("/")
async def trigger_analysis(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger full deep analysis on all documents for a tender."""
    # Check if analysis is already in progress
    existing = db.query(TenderAnalysisSummary).filter(
        TenderAnalysisSummary.tender_id == tender_id
    ).first()
    if existing and existing.analysis_status == "in_progress":
        raise HTTPException(status_code=409, detail="Analysis already in progress for this tender")

    summary = await analyze_all_documents(db, tender_id)
    return {
        "tender_id": tender_id,
        "status": summary.analysis_status,
        "total_requirements": summary.total_requirements,
        "total_critical_flags": summary.total_critical_flags,
        "completeness_score": summary.completeness_score,
        "documents_analyzed": summary.documents_analyzed,
    }


@router.get("/")
def get_analysis_summary(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the analysis summary for a tender."""
    summary = db.query(TenderAnalysisSummary).filter(
        TenderAnalysisSummary.tender_id == tender_id
    ).first()

    if not summary:
        return {
            "tender_id": tender_id,
            "status": "not_analyzed",
            "total_requirements": 0,
            "total_critical_flags": 0,
            "completeness_score": None,
            "documents_analyzed": 0,
            "category_counts": {},
            "cross_document_conflicts": [],
            "last_analyzed_at": None,
        }

    return {
        "tender_id": tender_id,
        "status": summary.analysis_status,
        "total_requirements": summary.total_requirements,
        "total_critical_flags": summary.total_critical_flags,
        "completeness_score": summary.completeness_score,
        "documents_analyzed": summary.documents_analyzed,
        "category_counts": summary.category_counts or {},
        "cross_document_conflicts": summary.cross_document_conflicts or [],
        "requirement_summary": summary.requirement_summary,
        "last_analyzed_at": summary.last_analyzed_at,
        "error_message": summary.error_message,
    }


@router.get("/extractions")
def list_extractions(
    tender_id: int,
    extraction_type: Optional[str] = Query(None, description="Filter by type: requirements, eligibility, etc."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all extraction results for a tender, optionally filtered by type."""
    query = db.query(DocumentExtractionResult).filter(
        DocumentExtractionResult.tender_id == tender_id
    )
    if extraction_type:
        query = query.filter(DocumentExtractionResult.extraction_type == extraction_type)

    results = query.order_by(DocumentExtractionResult.extraction_type).all()

    return [
        {
            "id": r.id,
            "tender_id": r.tender_id,
            "document_id": r.document_id,
            "document_name": r.document_name,
            "extraction_type": r.extraction_type,
            "items": r.items or [],
            "item_count": len(r.items) if r.items else 0,
            "completeness_score": r.completeness_score,
            "extraction_model": r.extraction_model,
            "created_at": r.created_at,
        }
        for r in results
    ]


@router.get("/extractions/{extraction_id}")
def get_extraction_detail(
    tender_id: int,
    extraction_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single extraction result with full details."""
    result = db.query(DocumentExtractionResult).filter(
        DocumentExtractionResult.id == extraction_id,
        DocumentExtractionResult.tender_id == tender_id,
    ).first()

    if not result:
        raise HTTPException(status_code=404, detail="Extraction result not found")

    return {
        "id": result.id,
        "tender_id": result.tender_id,
        "document_id": result.document_id,
        "document_name": result.document_name,
        "extraction_type": result.extraction_type,
        "items": result.items or [],
        "item_count": len(result.items) if result.items else 0,
        "raw_text": result.raw_text,
        "completeness_score": result.completeness_score,
        "extraction_model": result.extraction_model,
        "created_at": result.created_at,
    }


@router.get("/critical-flags")
def list_critical_flags(
    tender_id: int,
    severity: Optional[str] = Query(None, description="Filter by severity: critical, high, medium"),
    acknowledged: Optional[bool] = Query(None, description="Filter by acknowledgement status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all critical clause flags for a tender."""
    query = db.query(CriticalClauseFlag).filter(
        CriticalClauseFlag.tender_id == tender_id
    )
    if severity:
        query = query.filter(CriticalClauseFlag.severity == severity)
    if acknowledged is not None:
        query = query.filter(CriticalClauseFlag.is_acknowledged == acknowledged)

    flags = query.order_by(
        # critical first, then high, then medium
        CriticalClauseFlag.severity.asc(),
        CriticalClauseFlag.created_at.desc(),
    ).all()

    return [
        {
            "id": f.id,
            "tender_id": f.tender_id,
            "document_id": f.document_id,
            "document_name": f.document_name,
            "clause_text": f.clause_text,
            "surrounding_context": f.surrounding_context,
            "flag_type": f.flag_type,
            "severity": f.severity,
            "keyword_matched": f.keyword_matched,
            "page_number": f.page_number,
            "ai_explanation": f.ai_explanation,
            "is_acknowledged": f.is_acknowledged,
            "acknowledged_by": f.acknowledged_by,
            "acknowledged_at": f.acknowledged_at,
            "created_at": f.created_at,
        }
        for f in flags
    ]


@router.post("/critical-flags/{flag_id}/acknowledge")
def acknowledge_critical_flag(
    tender_id: int,
    flag_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark a critical flag as reviewed/acknowledged."""
    from datetime import datetime, timezone

    flag = db.query(CriticalClauseFlag).filter(
        CriticalClauseFlag.id == flag_id,
        CriticalClauseFlag.tender_id == tender_id,
    ).first()

    if not flag:
        raise HTTPException(status_code=404, detail="Critical flag not found")

    flag.is_acknowledged = True
    flag.acknowledged_by = current_user.id
    flag.acknowledged_at = datetime.now(timezone.utc)
    db.commit()

    return {"status": "acknowledged", "flag_id": flag_id}


@router.post("/extractions/{extraction_id}/feedback")
def submit_extraction_feedback(
    tender_id: int,
    extraction_id: int,
    item_index: int = Query(..., description="Index of the item in the extraction's items array"),
    feedback_type: str = Query(..., description="correct, incorrect, missing, or duplicate"),
    corrected_text: Optional[str] = Query(None, description="Corrected text if feedback_type is incorrect"),
    notes: Optional[str] = Query(None, description="Additional notes"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit feedback on an extraction item's accuracy."""
    # Validate extraction exists
    extraction = db.query(DocumentExtractionResult).filter(
        DocumentExtractionResult.id == extraction_id,
        DocumentExtractionResult.tender_id == tender_id,
    ).first()

    if not extraction:
        raise HTTPException(status_code=404, detail="Extraction result not found")

    valid_types = ["correct", "incorrect", "missing", "duplicate"]
    if feedback_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"feedback_type must be one of: {valid_types}")

    feedback = submit_feedback(
        db=db,
        extraction_result_id=extraction_id,
        item_index=item_index,
        feedback_type=feedback_type,
        user_id=current_user.id,
        corrected_text=corrected_text,
        notes=notes,
    )

    return {
        "id": feedback.id,
        "extraction_result_id": feedback.extraction_result_id,
        "item_index": feedback.item_index,
        "feedback_type": feedback.feedback_type,
        "created_at": feedback.created_at,
    }


@router.get("/accuracy")
def get_analysis_accuracy(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get accuracy statistics based on human feedback for this tender."""
    return get_accuracy_stats(db, tender_id=tender_id)


# ────────────────────────────────────────────────────────────────────────────
# Batch analysis — run deep analysis on N tenders in bounded parallel.
# ────────────────────────────────────────────────────────────────────────────

MAX_BATCH_SIZE = 50


class BatchAnalysisRequest(BaseModel):
    tender_ids: list[int] = Field(..., min_length=1, max_length=MAX_BATCH_SIZE)


async def _analyze_one_tender(tender_id: int, sem: asyncio.Semaphore) -> dict:
    """Analyze a single tender with its own DB session. SQLAlchemy sessions are
    not safe to share across concurrent tasks, so each task opens and closes
    its own session. Semaphore caps the number of concurrent tender analyses."""
    async with sem:
        task_db = SessionLocal()
        try:
            # Skip if already running — avoids stacking analyses on the same tender.
            existing = task_db.query(TenderAnalysisSummary).filter(
                TenderAnalysisSummary.tender_id == tender_id
            ).first()
            if existing and existing.analysis_status == "in_progress":
                return {
                    "tender_id": tender_id,
                    "status": "skipped",
                    "error": "Analysis already in progress for this tender",
                }

            summary = await analyze_all_documents(task_db, tender_id)
            return {
                "tender_id": tender_id,
                "status": summary.analysis_status,
                "total_requirements": summary.total_requirements,
                "total_critical_flags": summary.total_critical_flags,
                "completeness_score": summary.completeness_score,
                "documents_analyzed": summary.documents_analyzed,
            }
        except Exception as e:
            logger.exception(f"Batch analysis failed for tender {tender_id}")
            return {
                "tender_id": tender_id,
                "status": "failed",
                "error": str(e)[:500],
            }
        finally:
            task_db.close()


@batch_router.post("/batch")
async def trigger_batch_analysis(
    payload: BatchAnalysisRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Run deep document analysis on up to 50 tenders concurrently.

    Concurrency is bounded by `tender_analyzer_batch_max_parallel` (default 3)
    so we don't thundering-herd the Anthropic API. Each concurrent tender itself
    fans out internally to `tender_analyzer_max_parallel` per-doc calls, so with
    defaults you get 3 × 8 = up to 24 outstanding provider requests.

    This endpoint blocks until all tenders finish. Expect it to take roughly
    `ceil(N / batch_parallel) * per_tender_time`. For large batches consider
    a polling / SSE variant in a follow-up.
    """
    # Dedupe while preserving order.
    seen: set[int] = set()
    ordered_ids: list[int] = []
    for tid in payload.tender_ids:
        if tid not in seen:
            seen.add(tid)
            ordered_ids.append(tid)

    batch_parallel = max(1, _settings.tender_analyzer_batch_max_parallel)
    sem = asyncio.Semaphore(batch_parallel)

    logger.info(
        "Batch analysis starting: %d tenders, concurrency=%d, user=%s",
        len(ordered_ids), batch_parallel, getattr(current_user, "email", "?"),
    )

    results = await asyncio.gather(
        *[_analyze_one_tender(tid, sem) for tid in ordered_ids]
    )

    succeeded = sum(1 for r in results if r.get("status") == "completed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    skipped = sum(1 for r in results if r.get("status") == "skipped")

    return {
        "batch_size": len(ordered_ids),
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }
