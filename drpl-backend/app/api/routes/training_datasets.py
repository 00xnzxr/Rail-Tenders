"""
DRPL Backend - Training Dataset Routes
Admin endpoints for managing named training datasets, file uploads,
and agent-dataset assignments.
"""

from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_master_admin
from app.models.user import User
from app.services.training_dataset_service import (
    create_dataset, list_datasets, get_dataset_with_files, update_dataset, delete_dataset,
    upload_file_to_dataset, delete_file, get_file,
    get_dataset_context,
    assign_dataset_to_agent, unassign_dataset_from_agent, get_agent_datasets,
    update_assignment_priority,
)

router = APIRouter(prefix="/training-datasets", tags=["training-datasets"])


# ──────────────────────────────────────────────
# Dataset CRUD
# ──────────────────────────────────────────────

@router.get("/")
def api_list_datasets(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """List all training datasets with file counts."""
    return list_datasets(db, status_filter=status)


@router.post("/")
def api_create_dataset(
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Create a new training dataset."""
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Dataset name is required")

    try:
        ds = create_dataset(
            db=db,
            name=name,
            description=body.get("description"),
            tags=body.get("tags", []),
            created_by=admin.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {
        "id": ds.id,
        "name": ds.name,
        "description": ds.description,
        "tags": ds.tags,
        "status": ds.status,
    }


@router.get("/{dataset_id}")
def api_get_dataset(
    dataset_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get a dataset with all its file metadata."""
    result = get_dataset_with_files(db, dataset_id)
    if not result:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return result


@router.put("/{dataset_id}")
def api_update_dataset(
    dataset_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update dataset metadata."""
    try:
        ds = update_dataset(
            db=db,
            dataset_id=dataset_id,
            name=body.get("name"),
            description=body.get("description"),
            tags=body.get("tags"),
            status=body.get("status"),
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")

    return {"id": ds.id, "name": ds.name, "status": ds.status}


@router.delete("/{dataset_id}")
def api_delete_dataset(
    dataset_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Delete a dataset and all its files and agent associations."""
    if not delete_dataset(db, dataset_id):
        raise HTTPException(status_code=404, detail="Dataset not found")
    return {"deleted": True}


# ──────────────────────────────────────────────
# File Management
# ──────────────────────────────────────────────

@router.post("/{dataset_id}/files")
async def api_upload_files(
    dataset_id: int,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Upload one or more files to a dataset."""
    results = []
    errors = []

    for file in files:
        try:
            content = await file.read()
            f = upload_file_to_dataset(
                db=db,
                dataset_id=dataset_id,
                file_name=file.filename or "unknown",
                file_content_bytes=content,
                uploaded_by=admin.id,
            )
            results.append({
                "id": f.id,
                "file_name": f.file_name,
                "file_type": f.file_type,
                "file_size": f.file_size,
                "extraction_status": f.extraction_status,
                "extraction_error": f.extraction_error,
            })
        except ValueError as e:
            errors.append({"file_name": file.filename, "error": str(e)})
        except Exception as e:
            errors.append({"file_name": file.filename, "error": f"Upload failed: {str(e)}"})

    return {"uploaded": results, "errors": errors}


@router.delete("/files/{file_id}")
def api_delete_file(
    file_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Delete a single file from a dataset."""
    if not delete_file(db, file_id):
        raise HTTPException(status_code=404, detail="File not found")
    return {"deleted": True}


@router.get("/files/{file_id}/content")
def api_get_file_content(
    file_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get full content of a file (raw and parsed)."""
    f = get_file(db, file_id)
    if not f:
        raise HTTPException(status_code=404, detail="File not found")
    return {
        "id": f.id,
        "file_name": f.file_name,
        "file_type": f.file_type,
        "raw_content": f.raw_content,
        "parsed_content": f.parsed_content,
        "extraction_status": f.extraction_status,
    }


# ──────────────────────────────────────────────
# Context Preview
# ──────────────────────────────────────────────

@router.get("/{dataset_id}/preview")
def api_preview_context(
    dataset_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Preview the formatted context string for a dataset."""
    context = get_dataset_context(db, dataset_id)
    if not context:
        raise HTTPException(status_code=404, detail="Dataset not found or has no completed files")
    return {"context": context, "char_count": len(context), "estimated_tokens": len(context) // 4}


# ──────────────────────────────────────────────
# Agent Assignment
# ──────────────────────────────────────────────

@router.get("/agent/{agent_id}")
def api_get_agent_datasets(
    agent_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Get all datasets assigned to an agent."""
    return get_agent_datasets(db, agent_id)


@router.post("/{dataset_id}/assign/{agent_id}")
def api_assign_dataset(
    dataset_id: int,
    agent_id: int,
    body: dict = {},
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Assign a dataset to an agent."""
    try:
        link = assign_dataset_to_agent(
            db=db,
            agent_id=agent_id,
            dataset_id=dataset_id,
            priority=body.get("priority", 0),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"assignment_id": link.id, "agent_id": agent_id, "dataset_id": dataset_id, "priority": link.priority}


@router.delete("/{dataset_id}/assign/{agent_id}")
def api_unassign_dataset(
    dataset_id: int,
    agent_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Remove a dataset assignment from an agent."""
    if not unassign_dataset_from_agent(db, agent_id, dataset_id):
        raise HTTPException(status_code=404, detail="Assignment not found")
    return {"unassigned": True}


@router.put("/{dataset_id}/assign/{agent_id}/priority")
def api_update_priority(
    dataset_id: int,
    agent_id: int,
    body: dict,
    db: Session = Depends(get_db),
    admin: User = Depends(require_master_admin),
):
    """Update the priority of an agent-dataset assignment."""
    priority = body.get("priority", 0)
    if not update_assignment_priority(db, agent_id, dataset_id, priority):
        raise HTTPException(status_code=404, detail="Assignment not found")
    return {"updated": True, "priority": priority}
