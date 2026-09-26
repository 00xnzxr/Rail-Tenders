"""
DRPL Backend - Proposal Routes
Endpoints for proposal creation, chat, approval, and RAG corpus management
"""

import os
import json as json_module
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user, require_admin
from app.models.user import User
from app.services.audit_service import log_action
from app.schemas import (
    ProposalSessionResponse, ProposalMessageResponse, ProposalDocumentResponse,
    ChatMessageInput, CreateProposalSessionInput, CreateStandaloneSessionInput,
    ProposalReviewResponse, ReviewActionInput, RAGDocumentResponse,
    SetTemplateInput,
)
from app.services.proposal_service import (
    create_session, get_session, get_session_by_tender,
    get_messages, add_message, get_latest_document,
    create_standalone_session, get_user_sessions,
)
from app.services.proposal_agent import stream_chat_response, generate_structured_proposal
from app.services.approval_service import (
    submit_for_review, get_pending_reviews,
    approve_proposal, reject_proposal, request_changes,
)
from app.services.rag_service import (
    list_corpus_documents, add_corpus_document, delete_corpus_document,
)

router = APIRouter(prefix="/proposals", tags=["proposals"])


# --- Session Management ---

@router.get("/sessions", response_model=list[ProposalSessionResponse])
def list_my_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all proposal sessions for the current user."""
    return get_user_sessions(db, current_user.id)


@router.post("/sessions/standalone", response_model=ProposalSessionResponse)
def create_standalone_proposal_session(
    body: CreateStandaloneSessionInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a standalone proposal session (not linked to a tender)."""
    session = create_standalone_session(db, current_user.id, body.title, body.context)
    return session


@router.post("/sessions", response_model=ProposalSessionResponse)
def create_proposal_session(
    body: CreateProposalSessionInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new proposal session for a tender."""
    session = create_session(db, body.tender_id, current_user.id)
    return session


@router.get("/sessions/by-tender/{tender_id}", response_model=ProposalSessionResponse)
def get_session_for_tender(
    tender_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the active proposal session for a tender."""
    session = get_session_by_tender(db, tender_id)
    if not session:
        raise HTTPException(status_code=404, detail="No proposal session found for this tender")
    return session


@router.get("/sessions/{session_id}", response_model=ProposalSessionResponse)
def get_proposal_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a proposal session."""
    session = get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("/sessions/{session_id}/messages", response_model=list[ProposalMessageResponse])
def get_session_messages(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all messages for a proposal session."""
    session = get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return get_messages(db, session_id)


# --- Chat (SSE Streaming) ---

@router.post("/sessions/{session_id}/chat")
async def chat_with_agent(
    session_id: int,
    body: ChatMessageInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a message and get a streaming AI response."""
    session = get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status not in ("draft", "revision_requested"):
        raise HTTPException(status_code=400, detail=f"Cannot chat in session with status '{session.status}'")

    # Save user message
    add_message(db, session_id, "user", body.message)

    # Stream response — JSON-encode chunks to preserve newlines in SSE
    async def generate():
        full_response = []
        async for chunk in stream_chat_response(db, session, body.message):
            full_response.append(chunk)
            yield f"data: {json_module.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

        # Save assistant response
        complete_text = "".join(full_response)
        if complete_text:
            add_message(db, session_id, "assistant", complete_text)

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- Template & Structured Generation ---

@router.put("/sessions/{session_id}/template", response_model=ProposalSessionResponse)
def set_session_template(
    session_id: int,
    body: SetTemplateInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set the template_id on a proposal session."""
    from app.models.proposal import ProposalSession as PSModel
    session = db.query(PSModel).filter(PSModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.template_id = str(body.template_id)
    db.commit()
    db.refresh(session)
    return session


@router.post("/sessions/{session_id}/generate")
async def generate_proposal_from_template(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger structured proposal generation using the session's template (streaming SSE)."""
    session = get_session(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.template_id:
        raise HTTPException(status_code=400, detail="No template set on this session. Use PUT /sessions/{id}/template first.")

    template_id = int(session.template_id)

    async def generate():
        full_response = []
        async for chunk in generate_structured_proposal(db, session, template_id):
            full_response.append(chunk)
            yield f"data: {json_module.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

        # Save assistant response
        complete_text = "".join(full_response)
        if complete_text:
            add_message(db, session_id, "assistant", complete_text, "proposal_section")

    return StreamingResponse(generate(), media_type="text/event-stream")


# --- Document Generation ---

@router.get("/sessions/{session_id}/document")
def download_proposal_document(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download the latest proposal document."""
    doc = get_latest_document(db, session_id)
    if not doc or not os.path.exists(doc.file_path):
        raise HTTPException(status_code=404, detail="No proposal document found")
    return FileResponse(
        doc.file_path,
        filename=doc.file_name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# --- Approval Workflow ---

@router.post("/sessions/{session_id}/submit", response_model=ProposalReviewResponse)
def submit_proposal_for_review(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit a proposal for admin review."""
    try:
        review = submit_for_review(db, session_id, current_user.id)
        log_action(db, current_user.id, current_user.email, "proposal.submitted", "proposal", str(session_id))
        return review
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/reviews")
def list_pending_reviews(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """List all pending proposal reviews (admin only)."""
    return get_pending_reviews(db)


@router.post("/reviews/{review_id}/approve", response_model=ProposalReviewResponse)
def approve_review(
    review_id: int,
    body: ReviewActionInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Approve a proposal (admin only)."""
    try:
        log_action(db, admin.id, admin.email, "proposal.approved", "proposal_review", str(review_id), {"comments": body.comments})
        return approve_proposal(db, review_id, admin.id, body.comments)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/reviews/{review_id}/reject", response_model=ProposalReviewResponse)
def reject_review(
    review_id: int,
    body: ReviewActionInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Reject a proposal (admin only)."""
    try:
        log_action(db, admin.id, admin.email, "proposal.rejected", "proposal_review", str(review_id), {"comments": body.comments})
        return reject_proposal(db, review_id, admin.id, body.comments)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/reviews/{review_id}/request-changes", response_model=ProposalReviewResponse)
def request_review_changes(
    review_id: int,
    body: ReviewActionInput,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Request changes to a proposal (admin only)."""
    try:
        log_action(db, admin.id, admin.email, "proposal.changes_requested", "proposal_review", str(review_id), {"comments": body.comments})
        return request_changes(db, review_id, admin.id, body.comments)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- RAG Corpus Management ---

@router.get("/rag/documents", response_model=list[RAGDocumentResponse])
def list_rag_documents(
    current_user: User = Depends(get_current_user),
):
    """List all RAG corpus documents."""
    return list_corpus_documents()


@router.post("/rag/upload", response_model=RAGDocumentResponse)
async def upload_rag_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Upload a document to the RAG corpus (admin only). Also embeds via Voyage AI if configured."""
    file_data = await file.read()
    result = add_corpus_document(file.filename or "document", file_data, db=db)
    return result


@router.delete("/rag/{filename}")
def delete_rag_document(
    filename: str,
    admin: User = Depends(require_admin),
):
    """Delete a RAG corpus document (admin only)."""
    if not delete_corpus_document(filename):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "deleted"}
