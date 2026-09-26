"""
DRPL Backend - Workspace Routes
Endpoints for per-tender canvas workspace management
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.models.tender import Tender
from app.schemas import (
    WorkspaceOverviewResponse, DocumentWorkspaceResponse,
    UpdateWorkspaceConfigInput, UpdateWorkspaceLayoutInput,
    UpdateDocumentWorkspaceInput, SaveDocumentContentInput,
    DocumentFormatTemplateResponse, CreateFormatTemplateInput,
    UpdateFormatTemplateInput, DocumentDependenciesResponse,
)
from app.services.workspace_service import (
    init_workspace, get_workspace_overview, get_document_workspace,
    update_workspace_config, save_workspace_layout,
    update_document_workspace, save_document_content,
    finalize_document, toggle_not_required, get_workspace_stats,
    get_dependency_graph, migrate_markdown_content,
    create_manual_annexure, delete_workspace_item,
)

router = APIRouter(prefix="/tenders/{tender_id}/workspace", tags=["workspace"])


def _verify_tender(tender_id: int, db: Session) -> Tender:
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if not tender:
        raise HTTPException(status_code=404, detail="Tender not found")
    return tender


# ── Workspace-level endpoints ─────────────────────────────────────────────────


@router.post("/init")
def init_workspace_endpoint(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Initialize canvas workspace for a tender. Idempotent."""
    _verify_tender(tender_id, db)
    try:
        result = init_workspace(db, tender_id, current_user.id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/")
def get_workspace_endpoint(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get workspace overview (config + items + stats)."""
    _verify_tender(tender_id, db)
    result = get_workspace_overview(db, tender_id)
    if not result:
        raise HTTPException(status_code=404, detail="Workspace not initialized. Call POST /init first.")
    return result


@router.patch("/config")
def update_config_endpoint(
    tender_id: int,
    data: UpdateWorkspaceConfigInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update workspace configuration (view mode, defaults)."""
    _verify_tender(tender_id, db)
    try:
        return update_workspace_config(db, tender_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/stats")
def get_stats_endpoint(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get aggregate workspace stats."""
    _verify_tender(tender_id, db)
    return get_workspace_stats(db, tender_id)


@router.patch("/layout")
def save_layout_endpoint(
    tender_id: int,
    data: UpdateWorkspaceLayoutInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save canvas layout positions."""
    _verify_tender(tender_id, db)
    try:
        return save_workspace_layout(db, tender_id, data.layout_json)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Per-document endpoints ────────────────────────────────────────────────────


@router.get("/items/{item_id}")
def get_document_workspace_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get full document workspace detail."""
    _verify_tender(tender_id, db)
    try:
        return get_document_workspace(db, tender_id, item_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/items/{item_id}")
def update_document_workspace_endpoint(
    tender_id: int,
    item_id: int,
    data: UpdateDocumentWorkspaceInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update document workspace settings (agent, format, notes, status)."""
    _verify_tender(tender_id, db)
    try:
        return update_document_workspace(db, item_id, data.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/items/{item_id}/content")
def save_content_endpoint(
    tender_id: int,
    item_id: int,
    data: SaveDocumentContentInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save draft content (HTML and/or markdown)."""
    _verify_tender(tender_id, db)
    try:
        return save_document_content(
            db, item_id,
            content_html=data.content_html,
            content_markdown=data.content_markdown,
            user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/items/{item_id}/preview-pdf")
def preview_workspace_pdf_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Render a live PDF preview from the workspace draft.

    Unlike /checklist/{item_id}/preview (which requires a finalized
    GeneratedDocument), this hits the DocumentWorkspace row directly so the
    user can iterate on letterhead / signatures / content before locking it
    in via Finalize.

    Also surfaces the actual rendered page count in an `X-PDF-Page-Count`
    response header so the workspace editor's signature placement board can
    line up its page boundaries with the real PDF (the in-browser HTML
    estimate is wrong whenever WeasyPrint pagination differs from natural
    HTML scroll height — e.g. letterheads with deep top margins).
    """
    from app.services.pdf_generation_service import generate_workspace_preview

    _verify_tender(tender_id, db)
    try:
        pdf_bytes = generate_workspace_preview(db, tender_id, item_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview failed: {e}")

    # Count pages from the rendered PDF so the frontend placement UI can
    # use the authoritative count instead of estimating from HTML height.
    page_count: int | None = None
    try:
        import io as _io
        from PyPDF2 import PdfReader
        page_count = len(PdfReader(_io.BytesIO(pdf_bytes)).pages)
    except Exception:
        page_count = None  # frontend will fall back to its HTML estimate

    headers = {
        "Content-Disposition": 'inline; filename="preview.pdf"',
        # CORS: make the custom header readable by the browser fetch.
        "Access-Control-Expose-Headers": "X-PDF-Page-Count",
    }
    if page_count is not None and page_count > 0:
        headers["X-PDF-Page-Count"] = str(page_count)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers=headers,
    )


@router.get("/items/{item_id}/preview-pages.png")
def preview_workspace_pages_png_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return all preview pages as one tall PNG (pages stacked vertically).

    Powers the signature placement board on the workspace editor — by
    rasterising the same letterhead-merged PDF that the right-side preview
    pane shows, we eliminate the coordinate mismatch between "where the
    user dropped the chip in the board" and "where the signature lands in
    the final PDF". Headers `X-PDF-Page-Count` and `X-Image-Page-Height-Px`
    let the client compute page boundaries without re-measuring.
    """
    from app.services.pdf_generation_service import generate_workspace_preview

    _verify_tender(tender_id, db)
    try:
        pdf_bytes = generate_workspace_preview(db, tender_id, item_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Preview failed: {e}")

    from app.services.pdf_render_service import (
        rasterize_pdf_pages,
        raster_response_headers,
    )

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


@router.post("/items/{item_id}/finalize")
def finalize_document_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Finalize document: lock content, create GeneratedDocument, generate PDF."""
    _verify_tender(tender_id, db)
    try:
        return finalize_document(db, item_id, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class OrientationInput(BaseModel):
    orientation: str  # "portrait" | "landscape"


@router.patch("/items/{item_id}/orientation")
def set_orientation_endpoint(
    tender_id: int,
    item_id: int,
    data: OrientationInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle the page orientation of a document. Used to widen the editor for
    wide annexure tables and to set the PDF page orientation on finalize.
    """
    _verify_tender(tender_id, db)
    orientation = (data.orientation or "").strip().lower()
    if orientation not in ("portrait", "landscape"):
        raise HTTPException(status_code=400, detail="orientation must be 'portrait' or 'landscape'")

    from app.models.workspace import DocumentWorkspace
    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id,
        DocumentWorkspace.tender_id == tender_id,
    ).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Document workspace not found")
    ws.page_orientation = orientation
    db.commit()
    return {"item_id": item_id, "page_orientation": orientation}


class AnnexureLetterheadInput(BaseModel):
    # None = clear the tender default (annexures then render bare).
    letterhead_template_id: int | None = None


@router.put("/annexures/letterhead")
def set_annexure_letterhead_default(
    tender_id: int,
    data: AnnexureLetterheadInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set the tender-wide default letterhead applied to every annexure on
    export/preview.

    Stored on WorkspaceConfig.default_letterhead_id. Per-annexure overrides
    (item-level letterhead_template_id / letterhead_disabled) still win.
    """
    _verify_tender(tender_id, db)

    from app.models.workspace import WorkspaceConfig
    from app.models.letterhead import LetterheadTemplate

    lh_id = data.letterhead_template_id
    if lh_id is not None:
        exists = db.query(LetterheadTemplate).filter(
            LetterheadTemplate.id == lh_id,
            LetterheadTemplate.is_active == True,  # noqa: E712
        ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Letterhead template not found")

    cfg = db.query(WorkspaceConfig).filter(
        WorkspaceConfig.tender_id == tender_id
    ).first()
    if not cfg:
        cfg = WorkspaceConfig(tender_id=tender_id)
        db.add(cfg)
    cfg.default_letterhead_id = lh_id
    db.commit()
    return {"tender_id": tender_id, "default_letterhead_id": lh_id}


@router.get("/annexures/letterhead")
def get_annexure_letterhead_default(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read the tender-wide annexure letterhead default."""
    _verify_tender(tender_id, db)
    from app.models.workspace import WorkspaceConfig
    cfg = db.query(WorkspaceConfig).filter(
        WorkspaceConfig.tender_id == tender_id
    ).first()
    return {
        "tender_id": tender_id,
        "default_letterhead_id": cfg.default_letterhead_id if cfg else None,
    }


class ItemLetterheadInput(BaseModel):
    # Tri-state override for ONE annexure:
    #   disabled=True                      -> never apply a letterhead
    #   disabled=False, id=<n>             -> use template <n>
    #   disabled=False, id=None            -> inherit the tender default
    letterhead_template_id: int | None = None
    disabled: bool = False


@router.patch("/items/{item_id}/letterhead")
def set_item_letterhead(
    tender_id: int,
    item_id: int,
    data: ItemLetterheadInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Override the letterhead for a single annexure/document.

    Used for forms that belong on someone else's letterhead — a bank guarantee
    bond goes on the issuing bank's letterhead, not the bidder's.
    """
    _verify_tender(tender_id, db)

    from app.models.workspace import DocumentWorkspace
    from app.models.letterhead import LetterheadTemplate

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id,
        DocumentWorkspace.tender_id == tender_id,
    ).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Document workspace not found")

    if data.letterhead_template_id is not None:
        exists = db.query(LetterheadTemplate).filter(
            LetterheadTemplate.id == data.letterhead_template_id,
            LetterheadTemplate.is_active == True,  # noqa: E712
        ).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Letterhead template not found")

    ws.letterhead_disabled = bool(data.disabled)
    ws.letterhead_template_id = None if data.disabled else data.letterhead_template_id
    db.commit()
    return {
        "item_id": item_id,
        "letterhead_template_id": ws.letterhead_template_id,
        "letterhead_disabled": ws.letterhead_disabled,
    }


class CreateAnnexureInput(BaseModel):
    title: str
    identifier: str | None = None


@router.post("/annexures")
def create_manual_annexure_endpoint(
    tender_id: int,
    data: CreateAnnexureInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually add a blank annexure to the tender's All Annexures view — for
    forms the bidder must submit that aren't present in the tender documents."""
    _verify_tender(tender_id, db)
    try:
        return create_manual_annexure(
            db, tender_id, title=data.title, identifier=data.identifier,
            user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/items/{item_id}")
def delete_workspace_item_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a workspace item (annexure/document) that doesn't apply — removes
    its ChecklistItem and paired DocumentWorkspace."""
    _verify_tender(tender_id, db)
    try:
        return delete_workspace_item(db, tender_id, item_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/items/{item_id}/re-extract")
async def re_extract_annexure_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-run the annexure_finder for THIS specific annexure-derived document
    and overwrite its draft content with the latest layout-preserving output.

    Only works for annexure_finder-owned docs in drafting / not_started status.
    Locked docs (in_review / approved) are refused — user must reject/re-open
    before re-extraction.
    """
    _verify_tender(tender_id, db)

    from app.models.workspace import DocumentWorkspace
    from app.models.checklist import ChecklistItem

    item = db.query(ChecklistItem).filter(
        ChecklistItem.id == item_id,
        ChecklistItem.tender_id == tender_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    if not (item.source_section or "").startswith("annexure_finder:"):
        raise HTTPException(status_code=400, detail="Only annexure_finder-owned documents can be re-extracted")

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()
    if ws and ws.review_status in ("in_review", "approved"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot re-extract a document in '{ws.review_status}' status. Reopen it first.",
        )

    # Re-run annexure extraction for the entire tender (cheapest idempotent
    # path — _upsert_annexure merges by identifier so only matching rows are
    # touched). Callers that want per-annexure extraction can filter after.
    from app.services.langchain.graphs.annexure_finder_agent import run_annexure_extraction
    result = await run_annexure_extraction(db, tender_id)
    if result.get("status") != "completed":
        raise HTTPException(
            status_code=500,
            detail=result.get("message") or f"Re-extraction failed: {result.get('error')}",
        )

    # Return the updated workspace so the editor reloads.
    db.refresh(ws) if ws else None
    return {
        "item_id": item_id,
        "workspace_id": ws.id if ws else None,
        "page_orientation": ws.page_orientation if ws else "portrait",
        "content_version": ws.content_version if ws else 0,
        "annexures_refreshed": result.get("counts", {}),
    }


@router.patch("/items/{item_id}/not-required")
def toggle_not_required_endpoint(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle is_not_required flag on a checklist item."""
    _verify_tender(tender_id, db)
    try:
        return toggle_not_required(db, item_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Format template endpoints ─────────────────────────────────────────────────


@router.get("/format-templates", response_model=list[DocumentFormatTemplateResponse])
def list_format_templates(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all active format templates."""
    _verify_tender(tender_id, db)
    from app.models.workspace import DocumentFormatTemplate
    templates = db.query(DocumentFormatTemplate).filter(
        DocumentFormatTemplate.is_active == True
    ).all()
    return templates


@router.post("/format-templates", response_model=DocumentFormatTemplateResponse)
def create_format_template(
    tender_id: int,
    body: CreateFormatTemplateInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a user-defined format template."""
    _verify_tender(tender_id, db)
    from app.models.workspace import DocumentFormatTemplate

    template = DocumentFormatTemplate(
        name=body.name,
        description=body.description,
        document_category=body.document_category,
        structure_json=body.structure_json,
        content_template_markdown=body.content_template_markdown,
        content_template_html=body.content_template_html,
        format_rules=body.format_rules or [],
        required_sections=body.required_sections or [],
        match_patterns=body.match_patterns or [],
        is_system=False,
        is_active=True,
        created_by=current_user.id,
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


@router.get("/format-templates/{template_id}", response_model=DocumentFormatTemplateResponse)
def get_format_template(
    tender_id: int,
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single format template by ID."""
    _verify_tender(tender_id, db)
    from app.models.workspace import DocumentFormatTemplate

    template = db.query(DocumentFormatTemplate).filter(
        DocumentFormatTemplate.id == template_id
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Format template not found")
    return template


@router.patch("/format-templates/{template_id}", response_model=DocumentFormatTemplateResponse)
def update_format_template(
    tender_id: int,
    template_id: int,
    body: UpdateFormatTemplateInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a format template. System templates cannot be modified."""
    _verify_tender(tender_id, db)
    from app.models.workspace import DocumentFormatTemplate

    template = db.query(DocumentFormatTemplate).filter(
        DocumentFormatTemplate.id == template_id
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail="Format template not found")
    if template.is_system:
        raise HTTPException(status_code=403, detail="System templates cannot be modified")

    updates = body.model_dump(exclude_none=True)
    for key, value in updates.items():
        setattr(template, key, value)

    db.commit()
    db.refresh(template)
    return template


@router.get("/items/{item_id}/dependencies", response_model=DocumentDependenciesResponse)
def get_document_dependencies(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the dependency graph for a document workspace item."""
    _verify_tender(tender_id, db)
    try:
        return get_dependency_graph(db, item_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Agent endpoints ───────────────────────────────────────────────────────────


class AgentChatInput(BaseModel):
    message: str


class AgentEnhanceInput(BaseModel):
    prompt: str


@router.post("/items/{item_id}/agent/chat")
async def agent_chat_stream(
    tender_id: int,
    item_id: int,
    body: AgentChatInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSE streaming chat with the document-assigned agent."""
    _verify_tender(tender_id, db)
    from app.services.workspace_agent_service import execute_document_agent_chat

    return StreamingResponse(
        execute_document_agent_chat(db, item_id, body.message, current_user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/items/{item_id}/agent/history")
def get_agent_history(
    tender_id: int,
    item_id: int,
    limit: int = Query(50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get agent conversation history for this document."""
    _verify_tender(tender_id, db)
    from app.models.workspace import DocumentWorkspace
    from app.services.langchain.memory_service import get_conversation_history

    ws = db.query(DocumentWorkspace).filter(
        DocumentWorkspace.checklist_item_id == item_id
    ).first()
    if not ws or not ws.conversation_session_id:
        return []

    turns = get_conversation_history(db, ws.conversation_session_id, limit=limit)
    return [
        {
            "id": t.id,
            "role": t.role,
            "content": t.content,
            "output_type": t.output_type,
            "agent_key": t.agent_key,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in turns
    ]


@router.post("/items/{item_id}/agent/generate")
async def agent_generate(
    tender_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One-shot: agent generates full document content and saves as draft."""
    _verify_tender(tender_id, db)
    from app.services.workspace_agent_service import generate_document_with_agent
    try:
        return await generate_document_with_agent(db, item_id, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/items/{item_id}/agent/enhance")
async def agent_enhance(
    tender_id: int,
    item_id: int,
    body: AgentEnhanceInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent improves existing draft based on user prompt."""
    _verify_tender(tender_id, db)
    from app.services.workspace_agent_service import enhance_document_with_agent
    try:
        return await enhance_document_with_agent(db, item_id, body.prompt, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/agents/available")
def list_available_agents(
    tender_id: int,
    category: str = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List agents that can handle document workspace items."""
    _verify_tender(tender_id, db)
    from app.services.workspace_agent_service import get_available_agents
    return get_available_agents(db, document_category=category)


@router.post("/agents/auto-assign")
async def auto_assign_agents_endpoint(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AI-based agent mapping for unassigned documents."""
    _verify_tender(tender_id, db)
    from app.services.workspace_agent_service import auto_assign_agents
    return await auto_assign_agents(db, tender_id)


@router.post("/migrate-content")
async def migrate_content_endpoint(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Convert existing markdown-in-HTML content to proper HTML for all workspace documents.
    Safe to call multiple times — skips already-converted documents."""
    _verify_tender(tender_id, db)
    return migrate_markdown_content(db)
