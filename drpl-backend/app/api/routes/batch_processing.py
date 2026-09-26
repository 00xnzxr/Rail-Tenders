"""
DRPL Backend - Batch Processing Routes
Endpoints for managing Claude Message Batches API batch jobs.
50% cost discount on bulk AI processing.
"""

import json
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user, require_admin
from app.models.user import User
from app.services.batch_service import (
    create_tender_analysis_batch,
    poll_batch_status,
    process_batch_results,
    cancel_batch,
    list_batches,
    get_batch,
    get_batch_items,
    get_batch_stats,
    poll_and_process_if_ready,
)

router = APIRouter(prefix="/batch", tags=["batch-processing"])


# --- Response helpers ---

def _batch_to_dict(batch) -> dict:
    """Convert MessageBatch record to response dict."""
    metadata = {}
    if batch.metadata_json:
        try:
            metadata = json.loads(batch.metadata_json)
        except json.JSONDecodeError:
            pass

    return {
        "id": batch.id,
        "batch_id": batch.batch_id,
        "batch_type": batch.batch_type,
        "status": batch.status,
        "model": batch.model,
        "total_requests": batch.total_requests,
        "succeeded_count": batch.succeeded_count,
        "errored_count": batch.errored_count,
        "expired_count": batch.expired_count,
        "canceled_count": batch.canceled_count,
        "processing_count": batch.processing_count,
        "total_input_tokens": batch.total_input_tokens,
        "total_output_tokens": batch.total_output_tokens,
        "estimated_cost": batch.estimated_cost,
        "results_url": batch.results_url,
        "results_processed": batch.results_processed,
        "metadata": metadata,
        "error_message": batch.error_message,
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "ended_at": batch.ended_at.isoformat() if batch.ended_at else None,
        "expires_at": batch.expires_at.isoformat() if batch.expires_at else None,
    }


def _item_to_dict(item) -> dict:
    """Convert MessageBatchItem to response dict."""
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "custom_id": item.custom_id,
        "item_type": item.item_type,
        "tender_id": item.tender_id,
        "result_status": item.result_status,
        "result_text": item.result_text[:200] if item.result_text else None,  # Truncated preview
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "error_type": item.error_type,
        "error_message": item.error_message,
    }


# --- Endpoints ---

@router.post("/tender-analysis")
async def create_batch_analysis(
    batch_size: int = Query(50, ge=1, le=500, description="Number of tenders to include"),
    agents: Optional[str] = Query(
        None,
        description="Comma-separated agent names (classifier,relevance,risk,summary). Default: all 4",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create and submit a batch of tender analysis requests.

    Uses Claude's Message Batches API for 50% cost reduction.
    Each tender gets up to 4 AI agent analyses (classify, relevance, risk, summary).
    Batches typically complete within 1 hour.
    """
    agent_list = None
    if agents:
        agent_list = [a.strip() for a in agents.split(",")]

    try:
        batch = await create_tender_analysis_batch(
            db=db,
            batch_size=batch_size,
            agents=agent_list,
            user_id=current_user.id,
        )
        return _batch_to_dict(batch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch creation failed: {str(e)}")


@router.get("/")
def list_all_batches(
    status: Optional[str] = Query(None, description="Filter by status"),
    batch_type: Optional[str] = Query(None, description="Filter by batch type"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all batch jobs with optional filters."""
    batches = list_batches(db, status=status, batch_type=batch_type, limit=limit, offset=offset)
    return [_batch_to_dict(b) for b in batches]


@router.get("/stats")
def batch_statistics(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get overall batch processing statistics and cost savings."""
    return get_batch_stats(db)


@router.get("/{batch_id}")
def get_batch_detail(
    batch_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get detailed status of a specific batch."""
    batch = get_batch(db, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return _batch_to_dict(batch)


@router.get("/{batch_id}/items")
def get_batch_item_list(
    batch_id: str,
    status: Optional[str] = Query(None, description="Filter by result status"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get individual items/results within a batch."""
    items = get_batch_items(db, batch_id)
    if status:
        items = [i for i in items if i.result_status == status]
    # Pagination
    items = items[offset:offset + limit]
    return [_item_to_dict(i) for i in items]


@router.post("/{batch_id}/poll")
async def poll_batch(
    batch_id: str,
    auto_process: bool = Query(True, description="Auto-process results when batch ends"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Poll batch status from the Anthropic API.

    If auto_process is True and the batch has ended, results are automatically
    downloaded and applied to tenders.
    """
    try:
        if auto_process:
            result = await poll_and_process_if_ready(db, batch_id)
            return result
        else:
            batch = await poll_batch_status(db, batch_id)
            return _batch_to_dict(batch)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Poll failed: {str(e)}")


@router.post("/{batch_id}/process")
async def process_results(
    batch_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Manually trigger result processing for a completed batch.

    Downloads results from Anthropic and applies AI analysis to tenders.
    """
    try:
        result = await process_batch_results(db, batch_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")


@router.post("/{batch_id}/cancel")
async def cancel_batch_job(
    batch_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Cancel an in-progress batch (admin only)."""
    try:
        batch = await cancel_batch(db, batch_id)
        return _batch_to_dict(batch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cancel failed: {str(e)}")
