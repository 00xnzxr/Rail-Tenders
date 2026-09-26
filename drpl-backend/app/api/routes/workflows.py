"""
DRPL Backend - Workflow Builder Routes
CRUD, graph operations, publishing, versioning, and execution endpoints.
"""

import os
import time
from typing import Optional, List
from fastapi import APIRouter, Depends, Query, HTTPException, Body, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user, require_master_admin
from app.models.user import User
from app.services.workflow_service import (
    create_workflow, get_workflow, list_workflows, update_workflow,
    delete_workflow, save_workflow_graph, publish_workflow,
    get_workflow_versions, rollback_workflow, clone_workflow,
    validate_workflow, list_workflow_executions, get_workflow_execution,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])


# ─── Workflow CRUD ─────────────────────────────────────────────────────────


@router.get("")
def api_list_workflows(
    status: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List all workflows."""
    return list_workflows(db, status=status, category=category)


@router.post("")
def api_create_workflow(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create a new workflow with default start/end nodes."""
    workflow = create_workflow(
        db,
        display_name=body["display_name"],
        description=body.get("description"),
        category=body.get("category"),
        trigger_type=body.get("trigger_type", "manual"),
        user_id=current_user.id,
    )
    return get_workflow(db, workflow.id)


@router.get("/{workflow_id}")
def api_get_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get workflow with all nodes and edges."""
    result = get_workflow(db, workflow_id)
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@router.put("/{workflow_id}")
def api_update_workflow(
    workflow_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Update workflow metadata."""
    result = update_workflow(db, workflow_id, body)
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@router.delete("/{workflow_id}")
def api_delete_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Delete a workflow and all related records."""
    deleted = delete_workflow(db, workflow_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"status": "deleted"}


# ─── Graph Operations ──────────────────────────────────────────────────────


@router.post("/{workflow_id}/graph")
def api_save_graph(
    workflow_id: int,
    body: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Save the full workflow graph (nodes + edges) from the frontend canvas."""
    result = save_workflow_graph(
        db,
        workflow_id,
        nodes=body.get("nodes", []),
        edges=body.get("edges", []),
        canvas_state=body.get("canvas_state"),
    )
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@router.get("/{workflow_id}/graph")
def api_get_graph(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get the workflow graph for rendering."""
    result = get_workflow(db, workflow_id)
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"nodes": result["nodes"], "edges": result["edges"], "canvas_state": result.get("canvas_state")}


# ─── Publishing & Versioning ──────────────────────────────────────────────


@router.post("/{workflow_id}/publish")
def api_publish_workflow(
    workflow_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Publish workflow with version snapshot. Validates before publishing."""
    try:
        result = publish_workflow(
            db, workflow_id,
            user_id=current_user.id,
            change_description=body.get("change_description"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


@router.get("/{workflow_id}/versions")
def api_get_versions(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List all versions of a workflow."""
    return get_workflow_versions(db, workflow_id)


@router.post("/{workflow_id}/rollback/{version_number}")
def api_rollback_workflow(
    workflow_id: int,
    version_number: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Rollback to a specific version."""
    result = rollback_workflow(db, workflow_id, version_number)
    if not result:
        raise HTTPException(status_code=404, detail="Version not found")
    return result


@router.post("/{workflow_id}/clone")
def api_clone_workflow(
    workflow_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Clone a workflow."""
    result = clone_workflow(db, workflow_id, new_name=body.get("display_name"), user_id=current_user.id)
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return result


# ─── Validation ────────────────────────────────────────────────────────────


@router.post("/{workflow_id}/validate")
def api_validate_workflow(
    workflow_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Validate graph integrity."""
    return validate_workflow(db, workflow_id)


# ─── Execution Queries ─────────────────────────────────────────────────────


@router.get("/{workflow_id}/executions")
def api_list_executions(
    workflow_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """List executions for a workflow."""
    return list_workflow_executions(db, workflow_id, limit=limit)


@router.get("/executions/{execution_id}")
def api_get_execution(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Get execution detail with node traces."""
    result = get_workflow_execution(db, execution_id)
    if not result:
        raise HTTPException(status_code=404, detail="Execution not found")
    return result


@router.post("/executions/{execution_id}/approve")
def api_approve_execution(
    execution_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Approve a paused workflow execution at a user_approval node."""
    from app.models.workflow import WorkflowExecution as WE
    execution = db.query(WE).filter(WE.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if not execution.pending_approval:
        raise HTTPException(status_code=400, detail="Execution is not awaiting approval")

    execution.pending_approval = False
    execution.approval_response = body.get("response", "approved")
    execution.status = "completed"  # Resume handled by re-execution in future
    db.commit()
    return get_workflow_execution(db, execution_id)


@router.post("/executions/{execution_id}/reject")
def api_reject_execution(
    execution_id: int,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Reject a paused workflow execution."""
    from app.models.workflow import WorkflowExecution as WE
    execution = db.query(WE).filter(WE.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if not execution.pending_approval:
        raise HTTPException(status_code=400, detail="Execution is not awaiting approval")

    execution.pending_approval = False
    execution.approval_response = body.get("reason", "rejected")
    execution.status = "cancelled"
    from datetime import datetime, timezone
    execution.completed_at = datetime.now(timezone.utc)
    db.commit()
    return get_workflow_execution(db, execution_id)


@router.post("/executions/{execution_id}/cancel")
def api_cancel_execution(
    execution_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Cancel a running or paused workflow execution."""
    from app.models.workflow import WorkflowExecution as WE
    execution = db.query(WE).filter(WE.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution.status not in ("running", "paused"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel execution in '{execution.status}' status")

    execution.status = "cancelled"
    execution.pending_approval = False
    from datetime import datetime, timezone
    execution.completed_at = datetime.now(timezone.utc)
    db.commit()
    return get_workflow_execution(db, execution_id)


# ─── Test Execution (SSE Streaming) ────────────────────────────────────


@router.post("/{workflow_id}/test")
async def api_test_workflow(
    workflow_id: int,
    message: str = Form(""),
    tender_id: Optional[int] = Form(None),
    files: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Run a test execution of a workflow with SSE streaming. Accepts optional file uploads."""
    from app.services.workflow_streaming_handler import stream_workflow_test
    from app.core.config import get_settings

    wf = get_workflow(db, workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    input_data: dict = {
        "message": message,
        "tender_id": tender_id,
    }

    # Handle file uploads
    if files and any(f.filename for f in files):
        settings = get_settings()
        upload_dir = os.path.join(
            getattr(settings, "upload_dir", "uploads"),
            "workflow_tests", str(workflow_id),
        )
        os.makedirs(upload_dir, exist_ok=True)

        saved_files = []
        file_contents = []
        for f in files:
            if not f.filename:
                continue
            content = await f.read()
            safe_name = f"{int(time.time())}_{f.filename}"
            file_path = os.path.join(upload_dir, safe_name)
            with open(file_path, "wb") as out:
                out.write(content)

            saved_files.append({
                "file_name": f.filename,
                "file_path": file_path,
                "file_type": f.content_type,
                "file_size": len(content),
            })

            # Extract text for non-binary files
            try:
                from app.services.advanced_document_parser import extract_text_from_file_advanced, get_full_text
                result = extract_text_from_file_advanced(file_path)
                extracted = get_full_text(result)
                if extracted:
                    file_contents.append(f"[FILE CONTENT: {f.filename}]\n{extracted[:50000]}")
            except Exception:
                pass

        input_data["__uploaded_files"] = saved_files
        input_data["__file_metadata"] = {
            "attachment_paths": [
                {
                    "path": sf["file_path"],
                    "name": sf["file_name"],
                    "is_pdf": sf["file_name"].lower().endswith(".pdf"),
                }
                for sf in saved_files
            ],
        }
        # Enrich message with file content so agents have context
        if file_contents:
            input_data["message"] = (
                message + "\n\n--- Uploaded Document Content ---\n" + "\n\n".join(file_contents)
            )

    return StreamingResponse(
        stream_workflow_test(
            db=db,
            workflow_id=workflow_id,
            input_data=input_data,
            user_id=current_user.id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Migration ─────────────────────────────────────────────────────────


@router.post("/seed-default")
def api_seed_default_router(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create the Default Tender Router workflow from the hardcoded agent_router_graph."""
    from app.services.workflow_migration_service import create_default_tender_router
    workflow = create_default_tender_router(db, user_id=current_user.id)
    return get_workflow(db, workflow.id)


@router.post("/seed-tender-processing")
def api_seed_tender_processing(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create the GEM Tender Processing Pipeline workflow template."""
    from app.services.workflow_migration_service import create_gem_tender_processing_workflow
    workflow = create_gem_tender_processing_workflow(db, user_id=current_user.id)
    return get_workflow(db, workflow.id)


@router.post("/seed-checklist")
def api_seed_checklist(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create the Tender Checklist Pipeline workflow template."""
    from app.services.workflow_migration_service import create_checklist_workflow
    workflow = create_checklist_workflow(db, user_id=current_user.id)
    return get_workflow(db, workflow.id)


@router.post("/seed-costing")
def api_seed_costing(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_master_admin),
):
    """Create the Costing Analysis Pipeline workflow template."""
    from app.services.workflow_migration_service import create_costing_workflow
    workflow = create_costing_workflow(db, user_id=current_user.id)
    return get_workflow(db, workflow.id)
