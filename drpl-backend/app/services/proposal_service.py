"""
DRPL Backend - Proposal Service
Business logic for proposal session management
"""

import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tender import Tender
from app.models.proposal import ProposalSession, ProposalMessage, ProposalDocument
from app.services.storage_service import get_storage_service, key_proposal_doc

settings = get_settings()


def create_session(db: Session, tender_id: int, user_id: int) -> ProposalSession:
    """Create a new proposal session for a tender."""
    # Check if an active session already exists
    existing = db.query(ProposalSession).filter(
        ProposalSession.tender_id == tender_id,
        ProposalSession.status.in_(["draft", "revision_requested"]),
    ).first()
    if existing:
        return existing

    session = ProposalSession(
        tender_id=tender_id,
        created_by=user_id,
        status="draft",
    )
    db.add(session)

    # Advance tender workflow status
    tender = db.query(Tender).filter(Tender.id == tender_id).first()
    if tender and tender.workflow_status in ("new", "in_progress", "checklist_ready"):
        tender.workflow_status = "proposal_draft"

    db.commit()
    db.refresh(session)

    # Add system welcome message
    welcome = ProposalMessage(
        session_id=session.id,
        role="assistant",
        content="Welcome to the proposal creation assistant. I'll help you create a comprehensive tender proposal. Let's start with the Executive Summary. Could you tell me about your company's key strengths relevant to this tender?",
        message_type="text",
    )
    db.add(welcome)
    db.commit()

    return session


def get_session(db: Session, session_id: int) -> Optional[ProposalSession]:
    """Get a proposal session by ID."""
    return db.query(ProposalSession).filter(ProposalSession.id == session_id).first()


def get_session_by_tender(db: Session, tender_id: int) -> Optional[ProposalSession]:
    """Get the active proposal session for a tender."""
    return db.query(ProposalSession).filter(
        ProposalSession.tender_id == tender_id,
    ).order_by(ProposalSession.created_at.desc()).first()


def get_messages(db: Session, session_id: int) -> list[ProposalMessage]:
    """Get all messages for a session."""
    return db.query(ProposalMessage).filter(
        ProposalMessage.session_id == session_id,
    ).order_by(ProposalMessage.created_at).all()


def add_message(db: Session, session_id: int, role: str, content: str, message_type: str = "text") -> ProposalMessage:
    """Add a message to a session."""
    msg = ProposalMessage(
        session_id=session_id,
        role=role,
        content=content,
        message_type=message_type,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def save_proposal_document(
    db: Session,
    session_id: int,
    file_name: str,
    file_data: bytes,
) -> ProposalDocument:
    """Save a generated proposal document."""
    session = get_session(db, session_id)
    if not session:
        raise ValueError("Session not found")

    storage = get_storage_service()
    key = key_proposal_doc(session_id, file_name)
    storage.upload_file_sync(key, file_data)

    doc = ProposalDocument(
        session_id=session_id,
        version=session.current_version,
        file_path=key,
        file_name=file_name,
        file_size=len(file_data),
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def get_latest_document(db: Session, session_id: int) -> Optional[ProposalDocument]:
    """Get the latest proposal document for a session."""
    return db.query(ProposalDocument).filter(
        ProposalDocument.session_id == session_id,
    ).order_by(ProposalDocument.generated_at.desc()).first()


def create_standalone_session(db: Session, user_id: int, title: str, context_data: dict = None) -> ProposalSession:
    """Create a standalone proposal session (not linked to a tender)."""
    session = ProposalSession(
        tender_id=None,
        created_by=user_id,
        status="draft",
        title=title,
        agent_type="standalone",
        context_data=context_data or {},
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    # Add welcome message
    welcome = ProposalMessage(
        session_id=session.id,
        role="assistant",
        content=f"Welcome! I'm your proposal writing assistant. I'll help you create a proposal for: **{title}**. Let's start — what are the key requirements or objectives for this proposal?",
        message_type="text",
    )
    db.add(welcome)
    db.commit()

    return session


def get_user_sessions(db: Session, user_id: int) -> list[ProposalSession]:
    """Get all proposal sessions for a user."""
    return db.query(ProposalSession).filter(
        ProposalSession.created_by == user_id,
    ).order_by(ProposalSession.updated_at.desc()).all()
