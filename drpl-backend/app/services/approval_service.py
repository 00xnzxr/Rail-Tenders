"""
DRPL Backend - Approval Service
Business logic for proposal review/approve/reject workflow
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.tender import Tender
from app.models.proposal import ProposalSession, ProposalReview
from app.services import notification_service


log = logging.getLogger(__name__)


def _safe_notify(fn, **kwargs):
    """Wrap notification dispatch so a notification failure never aborts the
    primary review action. Errors are logged, not raised."""
    try:
        fn(**kwargs)
    except Exception as e:
        log.warning("approval_service: notification dispatch failed: %s: %s",
                    type(e).__name__, e)


def submit_for_review(db: Session, session_id: int, user_id: int) -> ProposalReview:
    """Submit a proposal for admin review."""
    session = db.query(ProposalSession).filter(ProposalSession.id == session_id).first()
    if not session:
        raise ValueError("Session not found")
    if session.status not in ("draft", "revision_requested"):
        raise ValueError(f"Cannot submit session with status '{session.status}'")

    review = ProposalReview(
        session_id=session_id,
        reviewer_id=0,  # Will be set when admin acts
        status="pending",
    )
    db.add(review)

    session.status = "submitted"
    tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
    if tender:
        tender.workflow_status = "proposal_review"

    # Notify all admins that a proposal awaits review.
    tender_title = tender.title if tender else f"Session #{session_id}"
    _safe_notify(
        notification_service.dispatch_to_admins,
        db=db,
        kind="proposal.submitted",
        title=f"Proposal submitted for review — {tender_title}",
        body=f"A proposal for tender '{tender_title}' has been submitted and is waiting for review.",
        action_url="/reviews",
        tender_id=session.tender_id,
        exclude_user_id=user_id,
    )

    db.commit()
    db.refresh(review)
    return review


def get_pending_reviews(db: Session) -> list[dict]:
    """Get all pending reviews with tender info."""
    reviews = db.query(ProposalReview).filter(
        ProposalReview.status == "pending",
    ).order_by(ProposalReview.created_at.desc()).all()

    results = []
    for review in reviews:
        session = db.query(ProposalSession).filter(ProposalSession.id == review.session_id).first()
        tender = db.query(Tender).filter(Tender.id == session.tender_id).first() if session else None
        results.append({
            "id": review.id,
            "session_id": review.session_id,
            "tender_id": session.tender_id if session else None,
            "tender_title": tender.title if tender else "Unknown",
            "tender_portal": tender.portal if tender else "",
            "status": review.status,
            "created_at": review.created_at.isoformat() if review.created_at else None,
        })
    return results


def approve_proposal(db: Session, review_id: int, reviewer_id: int, comments: str = "") -> ProposalReview:
    """Approve a proposal."""
    review = db.query(ProposalReview).filter(ProposalReview.id == review_id).first()
    if not review:
        raise ValueError("Review not found")

    review.status = "approved"
    review.reviewer_id = reviewer_id
    review.comments = comments
    review.reviewed_at = datetime.now(timezone.utc)

    session = db.query(ProposalSession).filter(ProposalSession.id == review.session_id).first()
    if session:
        session.status = "approved"
        tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
        if tender:
            tender.workflow_status = "approved"

        tender_title = tender.title if tender else f"Session #{session.id}"
        _safe_notify(
            notification_service.create,
            db=db,
            user_id=session.created_by,
            kind="proposal.approved",
            title=f"Proposal approved — {tender_title}",
            body=(comments or "Your proposal has been approved.").strip(),
            action_url=f"/tenders/{session.tender_id}/command-center" if session.tender_id else None,
            tender_id=session.tender_id,
        )

    db.commit()
    db.refresh(review)
    return review


def reject_proposal(db: Session, review_id: int, reviewer_id: int, comments: str = "") -> ProposalReview:
    """Reject a proposal."""
    review = db.query(ProposalReview).filter(ProposalReview.id == review_id).first()
    if not review:
        raise ValueError("Review not found")

    review.status = "rejected"
    review.reviewer_id = reviewer_id
    review.comments = comments
    review.reviewed_at = datetime.now(timezone.utc)

    session = db.query(ProposalSession).filter(ProposalSession.id == review.session_id).first()
    if session:
        session.status = "rejected"

        tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
        tender_title = tender.title if tender else f"Session #{session.id}"
        _safe_notify(
            notification_service.create,
            db=db,
            user_id=session.created_by,
            kind="proposal.rejected",
            title=f"Proposal rejected — {tender_title}",
            body=(comments or "Your proposal was rejected.").strip(),
            action_url=f"/tenders/{session.tender_id}/command-center" if session.tender_id else None,
            tender_id=session.tender_id,
        )

    db.commit()
    db.refresh(review)
    return review


def request_changes(db: Session, review_id: int, reviewer_id: int, comments: str = "") -> ProposalReview:
    """Request changes to a proposal."""
    review = db.query(ProposalReview).filter(ProposalReview.id == review_id).first()
    if not review:
        raise ValueError("Review not found")

    review.status = "changes_requested"
    review.reviewer_id = reviewer_id
    review.comments = comments
    review.reviewed_at = datetime.now(timezone.utc)

    session = db.query(ProposalSession).filter(ProposalSession.id == review.session_id).first()
    if session:
        session.status = "revision_requested"
        tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
        if tender:
            tender.workflow_status = "proposal_draft"

        tender_title = tender.title if tender else f"Session #{session.id}"
        _safe_notify(
            notification_service.create,
            db=db,
            user_id=session.created_by,
            kind="proposal.changes_requested",
            title=f"Changes requested — {tender_title}",
            body=(comments or "Reviewer requested changes on your proposal.").strip(),
            action_url=f"/tenders/{session.tender_id}/command-center" if session.tender_id else None,
            tender_id=session.tender_id,
        )

    db.commit()
    db.refresh(review)
    return review
