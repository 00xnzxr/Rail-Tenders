"""
DRPL Backend - Command Center Routes
Unified chat interface with artifact management, pipeline integration,
and context-aware suggestions.
"""

import asyncio
import os
import re as _re
import uuid
import json
import logging
import time
from typing import Optional, List

from fastapi import APIRouter, BackgroundTasks, Depends, Query, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.auth import get_current_user
from app.core import roles
from app.core.actor_context import Actor
from app.core.ownership import assert_can_read
from app.services.artifact_authz import assert_session_access
from app.services.budget_service import assert_within_budget
from app.core.config import get_settings
from app.models.user import User
from app.models.proposal import ProposalSession, ProposalMessage
from app.models.tender import Tender
from app.models.chat_attachment import ChatAttachment
from app.services.artifact_authz import assert_artifact_access, assert_session_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/command-center", tags=["command-center"])


def _load_owned_session(db: Session, session_id: int, current_user: User) -> ProposalSession:
    """Load a Command Center session the caller is allowed to see.

    These endpoints used to load by id alone, so any authenticated user could
    read or act on somebody else's conversation — and the costings, documents
    and artifacts hanging off it — by changing the number in the URL. Seventeen
    of them did.

    Delegates to `artifact_authz.assert_session_access` rather than to
    `ownership.assert_can_read`, so the Command Center answers a non-owner the
    one way it already documents and tests: 404 when the session is absent, 403
    when it belongs to somebody else. `ownership` prefers 404 for both (a 403
    confirms the row exists), which is the better posture — but forking two
    answers to one question across the same feature is worse than either, and
    unifying on 404 is a platform-wide decision, not a side effect of this one.

    The master_admin exemption is the difference from the plain helper: an owner
    can see every user's work by design.
    """
    if roles.normalize(current_user.role) == roles.MASTER_ADMIN:
        return db.query(ProposalSession).filter(
            ProposalSession.id == session_id
        ).first()
    assert_session_access(db, session_id, current_user.id)
    return db.query(ProposalSession).filter(ProposalSession.id == session_id).first()


#: Discriminator for the one persistent thread behind the global assistant
#: popup. It is a ProposalSession like any other (tender_id is already
#: nullable), but it must never appear in the Command Center session list and
#: stays out of the normal Command Center list and delete flow. The popup owns
#: listing and switching these saved conversations. Both `list_sessions` and
#: `bulk_delete_sessions` filter them out explicitly.
GLOBAL_ASSISTANT_AGENT_TYPE = "global_assistant"


def _create_global_session(db: Session, user: User) -> ProposalSession:
    """Create a fresh assistant conversation for ``user``."""
    session = ProposalSession(
        tender_id=None,
        created_by=user.id,
        status="draft",
        title="New conversation",
        agent_type=GLOBAL_ASSISTANT_AGENT_TYPE,
        mode="standalone",
        router_session_id=f"global-{user.id}-{uuid.uuid4().hex[:8]}",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    logger.info("global assistant session created for user %s: %s", user.id, session.id)
    return session


def _get_or_create_global_session(db: Session, user: User) -> ProposalSession:
    """Return the caller's latest assistant conversation, creating the first.

    Pinned to status ``draft`` forever: `chat_stream` refuses any session whose
    status is not draft/revision_requested, so advancing it would silently break
    the popup for that user with a 400 they cannot clear.
    """
    session = (
        db.query(ProposalSession)
        .filter(
            ProposalSession.created_by == user.id,
            ProposalSession.agent_type == GLOBAL_ASSISTANT_AGENT_TYPE,
        )
        .order_by(ProposalSession.created_at.desc(), ProposalSession.id.desc())
        .first()
    )
    if session:
        return session
    return _create_global_session(db, user)


def _detect_portal_from_upload_filenames(filenames: list[str]) -> Optional[str]:
    """Return 'gem' or 'ireps' if any filename signals the portal. GEM takes priority."""
    found_ireps = False
    for name in filenames:
        upper = (name or "").upper()
        if "GEM" in upper:
            return "gem"
        if "IREPS" in upper:
            found_ireps = True
    return "ireps" if found_ireps else None


# Matches GEM references in filenames where slashes may be /, _, or -
# No \b anchors — _ is a word char so \b fails after _ prefix
_GEM_FILENAME_RE = _re.compile(
    r"(?<![A-Za-z])(?:GEM|GeM)[_\-/]?\d{4}[_\-/][A-Z][_\-/]\d+", _re.IGNORECASE
)
# Canonical GEM reference pattern (slash-separated, from analysis text)
_GEM_TEXT_RE = _re.compile(r"\b(?:GEM|GeM)/\d{4}/[A-Z]/\d+\b", _re.IGNORECASE)
_IREPS_TEXT_RE = _re.compile(r"\be-?Tender[-/][A-Za-z0-9\-/]+\b", _re.IGNORECASE)
_NAME_OF_WORK_RE = _re.compile(
    r"(?:Name of Work|Scope of Work|Work Description|Item Description)"
    r"[\s\S]{0,200}?[\|:]\s*([^\|\n]{15,150})",
    _re.IGNORECASE,
)


def _best_session_title(
    tender_title: Optional[str],
    analysis_summary: Optional[str],
    per_doc_facts: list[dict],
    filenames: list[str],
) -> Optional[str]:
    """Return the best available display title for a Command Center session.

    Priority:
    1. tender.title if non-generic
    2. GEM/IREPS reference + name of work from analysis summary
    3. Per-doc key_facts (tender_reference + scope_summary)
    4. GEM reference extracted from document filenames
    5. Cleaned first filename as last resort
    """
    from app.services.tender_enrichment_service import _is_default_title

    # 1. Real tender title (non-generic)
    if not _is_default_title(tender_title):
        return tender_title

    # 2. Analysis summary text — extract reference + name of work
    if analysis_summary:
        ref = None
        m = _GEM_TEXT_RE.search(analysis_summary)
        if m:
            ref = m.group(0).upper()
        else:
            m = _IREPS_TEXT_RE.search(analysis_summary)
            if m:
                ref = m.group(0)

        name_m = _NAME_OF_WORK_RE.search(analysis_summary)
        name = name_m.group(1).strip().rstrip("|").strip()[:100] if name_m else None

        if ref and name:
            return f"{ref} — {name}"
        if ref:
            return ref
        if name:
            return name

    # 3. Per-doc key_facts
    for blob in per_doc_facts:
        kf = blob.get("key_facts") if isinstance(blob, dict) else None
        if not isinstance(kf, dict):
            continue
        ref = (kf.get("tender_reference") or "").strip()
        scope = (kf.get("scope_summary") or "").strip()
        if ref and scope:
            return f"{ref} — {scope[:100]}"
        if ref:
            return ref
        if scope:
            return scope[:120]

    # 4. GEM reference in filenames
    for name in filenames:
        m = _GEM_FILENAME_RE.search(name or "")
        if m:
            raw = m.group(0)
            normalized = _re.sub(r"[_\-]", "/", raw).upper()
            return normalized

    # 5. Cleaned filename as absolute last resort
    for name in filenames:
        if not name:
            continue
        # Strip timestamp prefix added during upload (e.g. "1746123456_doc.pdf" → "doc")
        stem = _re.sub(r"^\d{8,}_", "", name.rsplit(".", 1)[0])
        stem = stem.replace("_", " ").replace("-", " ").strip()
        if len(stem) > 4:
            return stem[:120]

    return None


# --- Session Management ---

@router.post("/sessions")
def create_session(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a Command Center session.
    Body: { tender_id?: int, title?: str, context?: str }
    """
    tender_id = body.get("tender_id")
    title = body.get("title", "")
    context = body.get("context", "")

    mode = "tender_linked" if tender_id else "standalone"
    router_session_id = str(uuid.uuid4())

    # For tender-linked: check if a draft session exists
    if tender_id:
        existing = db.query(ProposalSession).filter(
            ProposalSession.tender_id == tender_id,
            ProposalSession.router_session_id.isnot(None),
            ProposalSession.status.in_(["draft", "revision_requested"]),
        ).first()
        if existing:
            return _session_to_dict(existing)

    # Check for a default workflow to use
    default_workflow_id = None
    try:
        from app.models.workflow import Workflow
        default_wf = db.query(Workflow).filter(
            Workflow.is_default_router == True,
            Workflow.status == "published",
        ).first()
        if default_wf:
            default_workflow_id = default_wf.id
    except Exception:
        pass  # Table may not exist yet during initial setup

    session = ProposalSession(
        tender_id=tender_id,
        created_by=current_user.id,
        status="draft",
        title=title or (f"Standalone Session" if not tender_id else None),
        agent_type="tender_proposal" if tender_id else "standalone",
        context_data={"description": context} if context else None,
        router_session_id=router_session_id,
        pipeline_state={},
        mode=mode,
        active_workflow_id=default_workflow_id,
    )
    db.add(session)

    # Advance tender workflow if needed
    if tender_id:
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if tender and tender.workflow_status in ("new", "in_progress", "checklist_ready"):
            tender.workflow_status = "proposal_draft"

    db.commit()
    db.refresh(session)

    # Add welcome message
    welcome = ProposalMessage(
        session_id=session.id,
        role="assistant",
        content=(
            "Welcome to the DRPL Command Center. I can help you analyze tenders, "
            "generate checklists, write proposals, and estimate costs. "
            "What would you like to work on?"
        ),
        message_type="text",
    )
    db.add(welcome)
    db.commit()

    return _session_to_dict(session)


@router.post("/sessions/bulk-delete")
def bulk_delete_sessions(
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Bulk delete Command Center sessions.
    Body: { "session_ids": [1, 2, 3] }
    Deletes all related data (messages, artifacts, attachments, history).
    """
    from app.services.session_delete_service import delete_session, cleanup_files

    session_ids = body.get("session_ids", [])
    if not session_ids or not isinstance(session_ids, list):
        raise HTTPException(status_code=400, detail="session_ids is required and must be a list")
    if len(session_ids) > 50:
        raise HTTPException(status_code=400, detail="Cannot delete more than 50 sessions at once")

    # Verify all sessions exist and belong to current user
    sessions = (
        db.query(ProposalSession)
        .filter(
            ProposalSession.id.in_(session_ids),
            ProposalSession.created_by == current_user.id,
            ProposalSession.router_session_id.isnot(None),
            # Never deletable: losing it would orphan the user's whole
            # assistant history. It reports as "not found" rather than
            # erroring, which is what an unlisted session should look like.
            ProposalSession.agent_type != GLOBAL_ASSISTANT_AGENT_TYPE,
        )
        .all()
    )

    found_ids = {s.id for s in sessions}
    missing_ids = [sid for sid in session_ids if sid not in found_ids]
    if missing_ids:
        raise HTTPException(
            status_code=403,
            detail=f"Sessions not found or not authorized: {missing_ids}",
        )

    deleted_count = 0
    all_files: list[str] = []
    errors: list[dict] = []

    for sess in sessions:
        try:
            result = delete_session(db, sess)
            all_files.extend(result.get("files_to_delete", []))
            db.commit()
            deleted_count += 1
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to delete session {sess.id}: {e}")
            errors.append({"session_id": sess.id, "error": str(e)})

    # Best-effort file cleanup
    cleanup_files(all_files)

    logger.info(f"Bulk deleted {deleted_count}/{len(session_ids)} sessions for user {current_user.id}")

    return {
        "status": "deleted",
        "deleted_count": deleted_count,
        "requested_count": len(session_ids),
        "errors": errors,
    }


@router.get("/sessions")
def list_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all Command Center sessions for the current user.

    Batched to avoid N+1: one query per related table (Tender, artifact
    counts, message counts, workspace config, workspace docs) instead of
    per-session.
    """
    sessions = (
        db.query(ProposalSession)
        .filter(
            ProposalSession.created_by == current_user.id,
            ProposalSession.router_session_id.isnot(None),
            # The global assistant thread is a popup, not a Command Center
            # session — it must not appear as a phantom row in the sidebar.
            ProposalSession.agent_type != GLOBAL_ASSISTANT_AGENT_TYPE,
        )
        .order_by(ProposalSession.updated_at.desc())
        .all()
    )
    if not sessions:
        return []

    from app.models.artifact import CommandCenterArtifact
    from app.models.workspace import WorkspaceConfig, DocumentWorkspace
    from sqlalchemy import func

    session_ids = [s.id for s in sessions]
    tender_ids = list({s.tender_id for s in sessions if s.tender_id})

    # Batch 1: tender metadata
    tenders_by_id: dict[int, Tender] = {}
    if tender_ids:
        for t in db.query(Tender).filter(Tender.id.in_(tender_ids)).all():
            tenders_by_id[t.id] = t

    # Batch 2: artifact counts grouped by session
    artifact_counts: dict[int, int] = dict(
        db.query(
            CommandCenterArtifact.session_id,
            func.count(CommandCenterArtifact.id),
        )
        .filter(CommandCenterArtifact.session_id.in_(session_ids))
        .group_by(CommandCenterArtifact.session_id)
        .all()
    )

    # Batch 3: message counts grouped by session
    message_counts: dict[int, int] = dict(
        db.query(
            ProposalMessage.session_id,
            func.count(ProposalMessage.id),
        )
        .filter(ProposalMessage.session_id.in_(session_ids))
        .group_by(ProposalMessage.session_id)
        .all()
    )

    # Batch 4 & 5: workspace progress (only for tender-linked sessions)
    workspace_tender_ids: set[int] = set()
    workspace_total: dict[int, int] = {}
    workspace_done: dict[int, int] = {}
    if tender_ids:
        workspace_tender_ids = {
            tid for (tid,) in db.query(WorkspaceConfig.tender_id)
            .filter(WorkspaceConfig.tender_id.in_(tender_ids))
            .all()
        }
        workspace_total = dict(
            db.query(
                DocumentWorkspace.tender_id,
                func.count(DocumentWorkspace.id),
            )
            .filter(DocumentWorkspace.tender_id.in_(tender_ids))
            .group_by(DocumentWorkspace.tender_id)
            .all()
        )
        workspace_done = dict(
            db.query(
                DocumentWorkspace.tender_id,
                func.count(DocumentWorkspace.id),
            )
            .filter(
                DocumentWorkspace.tender_id.in_(tender_ids),
                DocumentWorkspace.review_status.in_(["approved", "finalized"]),
            )
            .group_by(DocumentWorkspace.tender_id)
            .all()
        )

    from app.services.tender_enrichment_service import _is_default_title
    from app.models.tender import TenderDocument
    from app.models.document_analysis import TenderAnalysisSummary, DocumentExtractionResult

    # Batch 6: doc filenames + analysis summaries for tenders that still have
    # generic titles — we use these to infer a real display title.
    filenames_by_tender: dict[int, list[str]] = {}
    analysis_summary_by_tender: dict[int, str] = {}
    per_doc_facts_by_tender: dict[int, list[dict]] = {}

    if tender_ids:
        for tid, fname in db.query(TenderDocument.tender_id, TenderDocument.file_name).filter(
            TenderDocument.tender_id.in_(tender_ids)
        ).all():
            filenames_by_tender.setdefault(tid, []).append(fname or "")

        # Optional title-inference data — if these tables have schema drift on
        # the live DB, just skip and fall back to filenames + tender title.
        try:
            for tid, summary in db.query(
                TenderAnalysisSummary.tender_id,
                TenderAnalysisSummary.requirement_summary,
            ).filter(
                TenderAnalysisSummary.tender_id.in_(tender_ids),
                TenderAnalysisSummary.requirement_summary.isnot(None),
            ).all():
                if summary:
                    analysis_summary_by_tender[tid] = summary
        except Exception as e:
            db.rollback()
            logger.warning(f"list_sessions: skipping analysis summary lookup ({type(e).__name__}: {e})")

        try:
            for tid, summary_json in db.query(
                DocumentExtractionResult.tender_id,
                DocumentExtractionResult.summary_json,
            ).filter(
                DocumentExtractionResult.tender_id.in_(tender_ids),
                DocumentExtractionResult.extraction_type == "per_doc_summary",
            ).all():
                if isinstance(summary_json, dict):
                    per_doc_facts_by_tender.setdefault(tid, []).append(summary_json)
        except Exception as e:
            db.rollback()
            logger.warning(f"list_sessions: skipping per-doc facts lookup ({type(e).__name__}: {e})")

    result = []
    for s in sessions:
        d = _session_to_dict(s)
        if s.tender_id:
            tender = tenders_by_id.get(s.tender_id)
            if tender:
                d["tender_title"] = tender.title
                d["tender_organisation"] = tender.organisation
                # Derive the best available title for this tender-linked session,
                # checking: (1) real tender title, (2) analysis data, (3) filenames.
                best = _best_session_title(
                    tender.title,
                    analysis_summary_by_tender.get(s.tender_id),
                    per_doc_facts_by_tender.get(s.tender_id, []),
                    filenames_by_tender.get(s.tender_id, []),
                )
                if best:
                    d["title"] = best
                    d["tender_title"] = best
        d["artifact_count"] = int(artifact_counts.get(s.id, 0))
        d["message_count"] = int(message_counts.get(s.id, 0))
        if s.tender_id:
            if s.tender_id in workspace_tender_ids:
                d["workspace_progress"] = {
                    "total": int(workspace_total.get(s.tender_id, 0)),
                    "completed": int(workspace_done.get(s.tender_id, 0)),
                    "has_workspace": True,
                }
            else:
                d["workspace_progress"] = {"total": 0, "completed": 0, "has_workspace": False}
        else:
            d["workspace_progress"] = None
        result.append(d)
    return result


@router.get("/sessions/{session_id}")
def get_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a Command Center session with full details."""
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    result = _session_to_dict(session)

    # Add tender info
    if session.tender_id:
        tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
        if tender:
            result["tender_title"] = tender.title
            result["tender_organisation"] = tender.organisation
            result["tender_closing_date"] = tender.closing_date.isoformat() if tender.closing_date else None

    # Add artifact count
    from app.models.artifact import CommandCenterArtifact
    result["artifact_count"] = (
        db.query(CommandCenterArtifact)
        .filter(CommandCenterArtifact.session_id == session_id)
        .count()
    )

    return result


@router.patch("/sessions/{session_id}")
def update_session(
    session_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update session (link tender, change title, etc.)."""
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if "tender_id" in body and body["tender_id"]:
        session.tender_id = body["tender_id"]
        session.mode = "tender_linked"
        session.agent_type = "tender_proposal"

    if "title" in body:
        session.title = body["title"]

    db.commit()
    db.refresh(session)
    return _session_to_dict(session)


@router.delete("/sessions/{session_id}")
def delete_single_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete a Command Center session and all related data
    (messages, artifacts, attachments, conversation history, auto-created tender).
    """
    from app.services.session_delete_service import delete_session, cleanup_files

    session = db.query(ProposalSession).filter(
        ProposalSession.id == session_id,
        ProposalSession.router_session_id.isnot(None),
    ).first()
    # Deleting is the most destructive thing an endpoint here does, and this
    # one loads by a different filter than _load_owned_session — so it gets the
    # same ownership check explicitly rather than by inheritance.
    assert_can_read(session, Actor(user_id=current_user.id, role=current_user.role))
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # The global assistant thread is not a Command Center session and has no
    # delete affordance in the UI; refuse explicitly so a hand-rolled request
    # cannot wipe a user's entire assistant history.
    if session.agent_type == GLOBAL_ASSISTANT_AGENT_TYPE:
        raise HTTPException(
            status_code=400,
            detail="The assistant conversation cannot be deleted.",
        )

    result = delete_session(db, session)
    db.commit()

    # Best-effort file cleanup after successful commit
    cleanup_files(result.get("files_to_delete", []))

    logger.info(f"Deleted session {session_id} for user {current_user.id}")

    return {
        "status": "deleted",
        "session_id": session_id,
        "attachments_deleted": result["attachments_deleted"],
        "artifacts_deleted": result["artifacts_deleted"],
        "messages_deleted": result["messages_deleted"],
        "history_deleted": result["history_deleted"],
    }


# --- Workspace Integration ---

@router.post("/sessions/{session_id}/init-workspace")
def init_session_workspace(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Initialize a canvas workspace for a Command Center session.
    If the session has no linked tender, auto-creates one.
    Returns { session, workspace, needs_checklist? }.
    """
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Refresh session to ensure we see the latest tender_id
    # (checklist generation may have auto-created a tender in a prior request)
    db.refresh(session)
    tender_id = session.tender_id

    # Auto-create tender for standalone sessions
    if not tender_id:
        from app.models.checklist import ChecklistItem
        new_tender = Tender(
            portal="command_center",
            tender_id=f"cc-{str(uuid.uuid4())[:8]}",
            title=session.title or f"Command Center Workspace #{session.id}",
            status="open",
            workflow_status="proposal_draft",
            workspace_enabled=True,
        )
        db.add(new_tender)
        db.flush()

        tender_id = new_tender.id
        session.tender_id = tender_id
        session.mode = "tender_linked"
        session.agent_type = "tender_proposal"
        db.flush()

    # Check if checklist items exist for workspace init
    from app.models.checklist import ChecklistItem
    checklist_count = db.query(ChecklistItem).filter(
        ChecklistItem.tender_id == tender_id
    ).count()

    if checklist_count == 0:
        # Try recovering checklist items from session's checklist artifact
        try:
            from app.models.artifact import CommandCenterArtifact
            checklist_artifact = db.query(CommandCenterArtifact).filter(
                CommandCenterArtifact.session_id == session_id,
                CommandCenterArtifact.artifact_type == "checklist",
            ).order_by(CommandCenterArtifact.created_at.desc()).first()

            if checklist_artifact:
                items_data = checklist_artifact.structured_data
                # If structured_data is missing or empty, try recovering from
                # the artifact's markdown content. First attempt the JSON
                # parsers (harmless if they miss), then fall back to scavenging
                # checkbox lines. The content has already been marker-stripped
                # by ``extract_artifact_from_output`` when the artifact was
                # created, so the JSON branch almost never hits — the markdown
                # fallback is what actually recovers pre-fix rows.
                if (not items_data or not isinstance(items_data, list) or len(items_data) == 0) and checklist_artifact.content:
                    try:
                        from app.services.langchain.graphs.chat_agent_wrappers import (
                            _parse_checklist_structured_data,
                            _parse_checklist_markdown_fallback,
                        )
                        parsed = _parse_checklist_structured_data(checklist_artifact.content)
                        if not parsed:
                            parsed = _parse_checklist_markdown_fallback(checklist_artifact.content)
                        if parsed:
                            items_data = parsed
                    except Exception:
                        pass
                if isinstance(items_data, list) and len(items_data) > 0:
                    for i, item_data in enumerate(items_data):
                        item = ChecklistItem(
                            tender_id=tender_id,
                            item_name=item_data.get("name", "Unknown Document"),
                            item_description=item_data.get("description", ""),
                            is_required=item_data.get("is_required", True),
                            display_order=i,
                            item_category=item_data.get("category", "standard"),
                        )
                        db.add(item)
                    db.flush()
                    checklist_count = len(items_data)
                    logger.info(
                        f"Recovered {checklist_count} checklist items from artifact "
                        f"for session {session_id} (tender {tender_id}) — "
                        f"markdown-fallback recovery path"
                    )
        except Exception as e:
            logger.warning(f"Checklist recovery from artifact failed: {e}")

    if checklist_count == 0:
        # No checklist items — workspace init would fail.
        # Commit the tender link and return guidance.
        pipeline = session.pipeline_state or {}
        db.commit()
        db.refresh(session)
        return {
            "session": _session_to_dict(session),
            "workspace": None,
            "needs_checklist": True,
        }

    # Initialize workspace
    from app.services.workspace_service import init_workspace
    try:
        workspace_overview = init_workspace(db, tender_id, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Update pipeline state
    pipeline = session.pipeline_state or {}
    pipeline["workspace_setup"] = True
    session.pipeline_state = pipeline
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(session, "pipeline_state")
    db.commit()
    db.refresh(session)

    return {
        "session": _session_to_dict(session),
        "workspace": workspace_overview,
        "needs_checklist": False,
    }


# --- File Attachments ---

ALLOWED_FILE_TYPES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif",
    ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt",
}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


@router.post("/sessions/{session_id}/attachments")
async def upload_attachments(
    session_id: int,
    files: List[UploadFile] = File(...),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload file attachments for a Command Center chat session."""
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    from app.services.storage_service import get_storage_service, key_command_center_attachment
    storage = get_storage_service()

    results = []
    for f in files:
        # Validate extension
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in ALLOWED_FILE_TYPES:
            logger.warning(f"Rejected file type {ext}: {f.filename}")
            continue

        # Read and validate size
        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            logger.warning(f"File too large ({len(content)} bytes): {f.filename}")
            continue

        # Upload to storage under a namespaced key
        safe_name = f"{int(time.time())}_{f.filename}"
        storage_key = key_command_center_attachment(session_id, safe_name)
        storage.upload_file_sync(
            storage_key, content,
            content_type=f.content_type or "application/octet-stream",
        )

        # Create DB record (file_path stores the storage key)
        attachment = ChatAttachment(
            session_id=session_id,
            file_name=f.filename,
            file_path=storage_key,
            file_type=f.content_type,
            file_size=len(content),
            uploaded_by=current_user.id,
        )
        db.add(attachment)
        db.flush()  # Get ID

        results.append({
            "id": attachment.id,
            "file_name": attachment.file_name,
            "file_type": attachment.file_type,
            "file_size": attachment.file_size,
            "file_path": attachment.file_path,
        })

    db.commit()

    # Dual-write: create TenderDocument records so uploads appear in Documents tab
    if session.tender_id:
        from app.models.tender import TenderDocument
        for att_result in results:
            file_path = att_result.get("file_path", "")
            if not file_path:
                continue
            existing_td = db.query(TenderDocument).filter(
                TenderDocument.tender_id == session.tender_id,
                TenderDocument.file_name == att_result["file_name"],
            ).first()
            if not existing_td:
                db.add(TenderDocument(
                    tender_id=session.tender_id,
                    file_name=att_result["file_name"],
                    file_path=file_path,
                    file_size=att_result.get("file_size"),
                    mime_type=att_result.get("file_type", "application/pdf"),
                    document_type="chat_upload",
                    uploaded_by=current_user.id,
                    extraction_status="completed",
                ))
        try:
            db.commit()
        except Exception:
            db.rollback()

    # Quick portal detection from filenames for Command Center tenders
    if session.tender_id and results:
        tender = db.query(Tender).filter(Tender.id == session.tender_id).first()
        if tender and tender.portal == "command_center":
            detected = _detect_portal_from_upload_filenames([r["file_name"] for r in results])
            if detected:
                tender.portal = detected
                try:
                    db.commit()
                except Exception:
                    db.rollback()

    # Trigger link extraction for PDF attachments
    if background_tasks:
        from app.services.document_link_service import process_document_links_background
        for att_result in results:
            if att_result.get("file_path", "").lower().endswith(".pdf"):
                background_tasks.add_task(
                    process_document_links_background,
                    source_type="chat_attachment",
                    source_id=att_result["id"],
                    file_path=att_result["file_path"],
                    tender_id=session.tender_id,
                    session_id=session_id,
                    user_id=current_user.id,
                )

    # Remove file_path from response (internal detail)
    for r in results:
        r.pop("file_path", None)

    return results


# --- Chat Streaming ---


async def resolve_file_attachments(
    db: Session, session_id: int, file_ids: list[int]
) -> tuple[str, dict]:
    """Resolve ChatAttachment rows into (file_context_text, file_metadata).

    Shared between the legacy /chat/stream path and the new /runs/enqueue
    path so both honour the same extraction, truncation, and
    native-PDF-passthrough rules. Returns ("", {}) when there are no
    attachments. Never raises — per-file extraction errors are logged and
    whatever could be read is returned.
    """
    if not file_ids:
        return "", {}
    attachments = db.query(ChatAttachment).filter(
        ChatAttachment.id.in_(file_ids),
        ChatAttachment.session_id == session_id,
    ).all()
    if not attachments:
        return "", {}

    from app.services.test_document_service import extract_text
    from app.services.storage_service import get_storage_service
    storage = get_storage_service()

    MAX_TOTAL_CHARS = 600_000  # ~150K tokens — fits within Claude's 200K context
    remaining_chars = MAX_TOTAL_CHARS
    file_lines = ["\n[ATTACHED FILES]"]
    file_info_list = []
    attachment_paths = []
    file_count = len(attachments)
    per_file_budget = MAX_TOTAL_CHARS // max(file_count, 1)
    files_remaining = file_count

    for att in attachments:
        file_lines.append(f"- {att.file_name} ({att.file_type}, {att.file_size} bytes)")
        ext = os.path.splitext(att.file_name or "")[1].lower()
        is_pdf = ext == ".pdf"
        attachment_paths.append({
            "path": att.file_path,
            "name": att.file_name,
            "type": att.file_type,
            "is_pdf": is_pdf,
            # Lets the per-document reader cap how many PDF bytes it holds at once.
            "size": att.file_size,
        })

        extracted = {"text": "", "status": "skipped"}
        try:
            def _extract(path=att.file_path):
                with storage.as_local_file(path) as local_path:
                    return extract_text(local_path)
            extracted = await asyncio.to_thread(_extract)
        except Exception as e:
            logger.warning(f"File extraction failed for {att.file_name}: {e}")
            extracted = {"text": "", "status": "failed", "error": str(e)}

        if extracted.get("status") == "failed":
            logger.warning(
                f"Text extraction FAILED for {att.file_name}: "
                f"{extracted.get('error', 'unknown')} — "
                f"native PDF analysis will be attempted via file_metadata.attachment_paths"
            )

        content = extracted.get("text", "")
        page_count = extracted.get("page_count", 0)
        has_content = bool(content and content.strip())

        if has_content and remaining_chars > 0:
            file_limit = max(per_file_budget, remaining_chars // max(files_remaining, 1))
            if len(content) > file_limit:
                content = content[:file_limit] + (
                    f"\n\n[... Document truncated at {file_limit:,} characters"
                    f"{f' (showing partial content of {page_count} pages)' if page_count else ''} ...]"
                )
            remaining_chars -= len(content)
            files_remaining -= 1
            file_lines.append(f"\n[FILE CONTENT: {att.file_name}]\n{content}")
        elif has_content:
            files_remaining -= 1
            file_lines.append(f"\n[FILE CONTENT: {att.file_name}]\n[Content truncated — file limit reached]")

        file_info_list.append({
            "id": att.id,
            "name": att.file_name,
            "type": att.file_type,
            "size": att.file_size,
            "has_content": has_content,
            "content_preview": content[:200] if has_content else "",
            "page_count": page_count,
            "extraction_status": extracted.get("status", "unknown"),
        })

    file_context = "\n".join(file_lines)
    file_metadata = {
        "has_files": True,
        "file_count": len(attachments),
        "files": file_info_list,
        "attachment_paths": attachment_paths,
        "total_extracted_chars": MAX_TOTAL_CHARS - remaining_chars,
    }
    return file_context, file_metadata


async def _with_keepalive(agen, interval: float = 15.0):
    # Emit an SSE comment every `interval` seconds of silence from `agen` to
    # keep Railway's ~30s idle-TCP timeout from cancelling long agent runs.
    # We must NOT cancel the inner __anext__() on timeout — that would kill
    # in-flight LLM calls. asyncio.wait(..., timeout=...) lets the task keep
    # running past the timeout instead of cancelling it.
    it = agen.__aiter__()
    next_task = asyncio.ensure_future(it.__anext__())
    try:
        while True:
            done, _pending = await asyncio.wait({next_task}, timeout=interval)
            if next_task in done:
                try:
                    event = next_task.result()
                except StopAsyncIteration:
                    return
                next_task = asyncio.ensure_future(it.__anext__())
                yield event
            else:
                yield ": keepalive\n\n"
    finally:
        if not next_task.done():
            next_task.cancel()
            try:
                await next_task
            except BaseException:
                pass


@router.get("/assistant/session")
def get_assistant_session(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Resolve the caller's global assistant thread, creating it on first open.

    The popup calls this once on mount and then uses the normal
    `/sessions/{id}/chat/stream` and `/sessions/{id}/history` endpoints.
    """
    session = _get_or_create_global_session(db, current_user)
    return {
        "session_id": session.id,
        "router_session_id": session.router_session_id,
        "title": session.title,
    }


@router.post("/assistant/sessions")
def create_assistant_session(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start a new assistant conversation without deleting earlier chats."""
    session = _create_global_session(db, current_user)
    return {
        "session_id": session.id,
        "router_session_id": session.router_session_id,
        "title": session.title,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        "message_count": 0,
    }


@router.get("/assistant/sessions")
def list_assistant_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the caller's saved assistant conversations, newest first."""
    sessions = (
        db.query(ProposalSession)
        .filter(
            ProposalSession.created_by == current_user.id,
            ProposalSession.agent_type == GLOBAL_ASSISTANT_AGENT_TYPE,
        )
        .order_by(ProposalSession.created_at.desc(), ProposalSession.id.desc())
        .all()
    )
    if not sessions:
        return []

    from sqlalchemy import func

    ids = [session.id for session in sessions]
    counts = dict(
        db.query(ProposalMessage.session_id, func.count(ProposalMessage.id))
        .filter(ProposalMessage.session_id.in_(ids))
        .group_by(ProposalMessage.session_id)
        .all()
    )
    first_user_ids = dict(
        db.query(ProposalMessage.session_id, func.min(ProposalMessage.id))
        .filter(
            ProposalMessage.session_id.in_(ids),
            ProposalMessage.role == "user",
        )
        .group_by(ProposalMessage.session_id)
        .all()
    )
    previews: dict[int, str] = {}
    if first_user_ids:
        first_messages = db.query(ProposalMessage).filter(
            ProposalMessage.id.in_(list(first_user_ids.values()))
        ).all()
        previews = {
            message.session_id: message.content[:160]
            for message in first_messages
        }

    return [
        {
            "session_id": session.id,
            "router_session_id": session.router_session_id,
            "title": session.title or "New conversation",
            "preview": previews.get(session.id),
            "message_count": int(counts.get(session.id, 0)),
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        }
        for session in sessions
    ]


def _format_page_context(page_context: Optional[dict]) -> str:
    """Render the popup's page context as a preamble for the LLM.

    Injected the same way `file_context` is, so no new plumbing. This is what
    lets a user ask "why is this wrong?" without naming what they are looking
    at — the agent already knows which page and which record is on screen.
    """
    if not page_context or not isinstance(page_context, dict):
        return ""

    lines: list[str] = []
    route = page_context.get("route")
    if route:
        lines.append(f"- Page: {route}")
    if page_context.get("tender_id"):
        lines.append(f"- Tender on screen: #{page_context['tender_id']}")
    if page_context.get("workspace_tab"):
        lines.append(f"- Workspace tab: {page_context['workspace_tab']}")

    entity = page_context.get("visible_entity")
    if isinstance(entity, dict) and entity.get("type"):
        entity_id = entity.get("id")
        suffix = f" #{entity_id}" if entity_id else ""
        lines.append(f"- Item in focus: {entity['type']}{suffix}")

    last_error = page_context.get("last_error")
    if last_error:
        # Truncated: an unbounded UI error string would eat the context window.
        lines.append(f"- Error shown on the page: {str(last_error)[:500]}")

    if not lines:
        return ""

    return (
        "\n\n[Where the user is right now — use this to resolve vague "
        "references like \"this\", \"here\", or \"why is this wrong\":\n"
        + "\n".join(lines)
        + "\n]"
    )


@router.post("/sessions/{session_id}/chat/stream")
async def chat_stream(
    session_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Main SSE streaming chat endpoint.
    Body: { message: str, file_ids?: int[] }
    """
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status not in ("draft", "revision_requested"):
        raise HTTPException(status_code=400, detail=f"Session is {session.status}, cannot chat")

    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    # Budget is checked here, before any work starts — never mid-run. A run
    # already executing is allowed to finish; killing it would waste what it
    # had already spent.
    assert_within_budget(db, current_user)

    # File attachments are resolved INSIDE the stream, not here. Extracting a
    # couple of large tender PDFs takes ~20s and produces ~80k tokens of
    # context; doing it before returning the StreamingResponse meant the
    # browser waited that long for the first byte — no headers, no events,
    # nothing to distinguish "working" from "hung". Any timeout or navigation
    # in that dead window dropped the connection and the session came back
    # blank with the user's message no longer on screen.
    file_ids = body.get("file_ids", [])

    # Save user message to ProposalMessage table
    user_msg = ProposalMessage(
        session_id=session_id,
        role="user",
        content=message,
        message_type="text",
        metadata_json={"file_ids": file_ids} if file_ids else None,
    )
    if (
        session.agent_type == GLOBAL_ASSISTANT_AGENT_TYPE
        and (session.title or "") in ("", "Assistant", "New conversation")
    ):
        # A useful conversation label without spending another model call.
        session.title = message[:72] + ("…" if len(message) > 72 else "")
    db.add(user_msg)
    db.commit()

    # Link attachments to message
    if file_ids:
        db.query(ChatAttachment).filter(
            ChatAttachment.id.in_(file_ids),
            ChatAttachment.session_id == session_id,
        ).update({"message_id": user_msg.id}, synchronize_session="fetch")
        db.commit()

    page_context = body.get("page_context") if isinstance(body.get("page_context"), dict) else None

    from app.services.langchain.streaming_handler import stream_router_response

    # Capture ORM values eagerly — inside the except block the DB session may be
    # in a failed-transaction state, and lazy-loading attributes would crash.
    _router_session_id = session.router_session_id
    # The global assistant thread has no tender of its own, so fall back to
    # whichever tender the user is looking at. Only a fallback: a session that
    # owns a tender keeps it, so navigating cannot hijack a real session.
    _tender_id = session.tender_id or (page_context or {}).get("tender_id")
    _user_id = current_user.id
    _proposal_session_id = session.id
    _active_workflow_id = getattr(session, 'active_workflow_id', None)

    async def generate():
        import json as _json
        from app.services.langchain.error_utils import format_user_error

        # EVERYTHING slow lives inside the detached task — attachment
        # extraction as well as the agent run.
        #
        # An earlier version of this detached only the agent run and left
        # attachment resolution here, in the request-coupled part of the
        # generator. Reading a couple of large tender PDFs takes ~20s, and
        # `detached_stream` was not reached until after it, so a client that
        # disconnected during that window killed the generator before the task
        # was ever created: the message was saved, both attachments were
        # linked, and then nothing ran at all. No routing, no reply, no error.
        # Session 297 died exactly that way — one PDF extracted, then silence.
        from app.services.detached_stream import detached_stream

        def _sse(event: str, payload: dict) -> str:
            return f"event: {event}\ndata: {_json.dumps(payload)}\n\n"

        async def _source():
            """Attachments, then the agent — all of it inside the task.

            On its OWN database session, not the request's. `get_db` closes the
            request session in a `finally` that fires the moment the response
            ends — which, for a client that walked away, is while this task is
            still running. The task would then be issuing queries on a session
            whose connection had already gone back to the pool, and any
            transaction it had open was rolled back underneath it. The run
            outliving the request is the whole point of detaching it, so it
            cannot borrow anything scoped to that request. This is the same
            pattern the RQ worker uses in `run_tasks._pump`.
            """
            run_db = SessionLocal()
            try:
                yield _sse("session", {"session_id": _router_session_id})

                file_context, file_metadata = "", {}
                if file_ids:
                    count = len(file_ids)
                    yield _sse("agent_status", {
                        "phase": "tool_running",
                        "run_id": "attachments",
                        "message": f"Reading {count} attached document{'s' if count != 1 else ''}",
                    })
                    try:
                        file_context, file_metadata = await resolve_file_attachments(
                            run_db, _proposal_session_id, file_ids
                        )
                    except Exception as e:
                        logger.error(f"Attachment resolution failed: {e}", exc_info=True)
                        yield _sse("agent_warning", {
                            "message": "I could not read one of the attachments.",
                        })
                    yield _sse("agent_status", {
                        "phase": "tool_done", "run_id": "attachments",
                    })

                enriched_message = message + file_context if file_context else message
                enriched_message += _format_page_context(page_context)

                if _active_workflow_id:
                    from app.services.workflow_streaming_handler import stream_workflow_response
                    inner = stream_workflow_response(
                        db=run_db, workflow_id=_active_workflow_id, message=enriched_message,
                        session_id=_router_session_id, tender_id=_tender_id,
                        user_id=_user_id,
                        file_metadata=file_metadata if file_metadata else None,
                        display_message=message,
                    )
                else:
                    inner = stream_router_response(
                        db=run_db, message=enriched_message, session_id=_router_session_id,
                        tender_id=_tender_id, user_id=_user_id,
                        proposal_session_id=_proposal_session_id,
                        file_metadata=file_metadata if file_metadata else None,
                        display_message=message,
                    )
                async for event in inner:
                    yield event
            finally:
                try:
                    run_db.close()
                except Exception:  # noqa: BLE001 — teardown must not mask the run
                    pass

        def _on_error(e: Exception) -> list[str]:
            friendly = format_user_error(e)
            return [
                _sse("error", {"message": friendly}),
                _sse("done", {"session_id": _router_session_id, "error": friendly}),
            ]

        async for event in detached_stream(
            _source, label=f"session {_proposal_session_id}", on_error=_on_error
        ):
            yield event

    return StreamingResponse(
        _with_keepalive(generate(), interval=15.0),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Decision Maker plan approval ---

@router.post("/sessions/{session_id}/decision/respond")
async def respond_to_decision_plan(
    session_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Handle the user's response to a decision_maker pending plan.

    Body: ``{action: "approve" | "change" | "deny" | "next", feedback?: str}``

    - **approve**: starts execution of the plan's first step (pauses after it).
    - **next**: runs the next step of an in-flight plan execution (pauses again
      until the user clicks "Run next step" on the progress card, or until the
      plan is complete).
    - **change**: re-runs planning mode with the user's feedback injected.
    - **deny**: clears the pending plan, emits a short assistant message, and stops.

    Returns an SSE stream in the same format as `/chat/stream` so the frontend
    can reuse its existing event handler.
    """

    # Approving a plan starts real agent work, so it is a run entry point too.
    assert_within_budget(db, current_user)
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    action = (body.get("action") or "").strip().lower()
    feedback = (body.get("feedback") or "").strip()
    if action not in ("approve", "change", "deny", "next"):
        raise HTTPException(status_code=400, detail="action must be approve, change, deny, or next")

    state = dict(session.pipeline_state or {})
    pending = state.get("pending_plan") or {}
    plan_exec_state = state.get("plan_execution") or {}

    if action == "next":
        # Resuming an in-flight plan — no pending_plan here, the approved plan
        # lives inside plan_execution.
        plan = plan_exec_state.get("plan")
        original_message = plan_exec_state.get("original_message") or ""
        stored_file_metadata = plan_exec_state.get("file_metadata") or {}
        stored_tender_id = plan_exec_state.get("tender_id")
        if not plan:
            raise HTTPException(status_code=400, detail="No in-flight plan execution to advance")
    else:
        plan = pending.get("plan")
        original_message = pending.get("original_message") or ""
        # Restore execution context stored when the plan was proposed. Without
        # these, the resumed handlers run blind (no attachments, no tender).
        stored_file_metadata = pending.get("file_metadata") or {}
        stored_tender_id = pending.get("tender_id")
        if not plan:
            raise HTTPException(status_code=400, detail="No pending plan to respond to")

    # Save a user-facing ProposalMessage recording the decision so the chat
    # transcript reflects what happened. "next" is a silent advance (no new
    # chat bubble) — it just advances the plan execution.
    decision_labels = {
        "approve": "Approved the plan.",
        "change": "Requested changes.",
        "deny": "Denied the plan.",
        "next": "",
    }
    decision_content = decision_labels[action]
    if action == "change" and feedback:
        decision_content = f"Requested changes:\n\n{feedback}"
    elif action == "deny" and feedback:
        decision_content = f"Denied the plan: {feedback}"
    if action != "next":
        db.add(ProposalMessage(
            session_id=session_id,
            role="user",
            content=decision_content,
            message_type="text",
            metadata_json={"decision_action": action, "feedback": feedback or None},
        ))
        db.commit()

    _router_session_id = session.router_session_id
    _tender_id = session.tender_id
    _user_id = current_user.id
    _proposal_session_id = session.id

    if action == "approve":
        # On approve, seed plan_execution state so subsequent "next" calls
        # can resume without re-reading the (now-cleared) pending_plan.
        state["plan_execution"] = {
            "plan": plan,
            "file_metadata": stored_file_metadata,
            "tender_id": stored_tender_id,
            "original_message": original_message,
            "cursor": 0,
            "completed_steps": [],
            "status": "running",
        }
        state.pop("pending_plan", None)
    elif action in ("change", "deny"):
        state.pop("pending_plan", None)
    # "next" leaves state unchanged; the driver will update it.
    session.pipeline_state = state
    db.add(session)
    db.commit()

    # Mark the most recent assistant message that carried this pending_plan
    # with the user's decision, so that after a refresh the DecisionPlanCard
    # renders in the correct "approved / changes requested / denied" state
    # instead of showing the approve/deny buttons again. Skip for "next" —
    # the card's status doesn't change on step advance.
    if action != "next":
        try:
            last_plan_msg = (
                db.query(ProposalMessage)
                .filter(
                    ProposalMessage.session_id == session_id,
                    ProposalMessage.role == "assistant",
                )
                .order_by(ProposalMessage.id.desc())
                .first()
            )
            if last_plan_msg and isinstance(last_plan_msg.metadata_json, dict):
                md = dict(last_plan_msg.metadata_json)
                if md.get("pending_plan"):
                    md["plan_status"] = action  # "approve" | "change" | "deny"
                    if feedback:
                        md["plan_feedback"] = feedback
                    last_plan_msg.metadata_json = md
                    db.add(last_plan_msg)
                    db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        # Also update the AgentConversationHistory turn because that's what
        # the history endpoint prefers to return on reload. Without this,
        # the DecisionPlanCard renders with the wrong (or missing) status
        # even though the ProposalMessage has been updated correctly.
        try:
            from app.models.agent_memory import AgentConversationHistory
            router_session_id = session.router_session_id
            if router_session_id:
                last_turn = (
                    db.query(AgentConversationHistory)
                    .filter(
                        AgentConversationHistory.session_id == router_session_id,
                        AgentConversationHistory.role == "assistant",
                    )
                    .order_by(AgentConversationHistory.id.desc())
                    .first()
                )
                if last_turn and isinstance(last_turn.metadata_json, dict):
                    tmd = dict(last_turn.metadata_json)
                    if tmd.get("pending_plan"):
                        tmd["plan_status"] = action
                        if feedback:
                            tmd["plan_feedback"] = feedback
                        last_turn.metadata_json = tmd
                        db.add(last_turn)
                        db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

    async def generate():
        import json as _json
        from app.services.langchain.error_utils import format_user_error
        from app.services.detached_stream import detached_stream

        async def _source():
            if action == "deny":
                # No agent run — just emit a short acknowledgement and done.
                msg = (
                    "Plan denied. Let me know what you'd like to do instead, and I'll propose "
                    "a new plan."
                )
                if feedback:
                    msg = f"Plan denied (reason: {feedback}). Send a new request with any adjustments and I'll draft a fresh plan."
                # Its own session: this body is detached, and the request's is
                # closed the moment the response ends. See `_source` in
                # chat_stream for the full reasoning.
                deny_db = SessionLocal()
                try:
                    deny_db.add(ProposalMessage(
                        session_id=session_id,
                        role="assistant",
                        content=msg,
                        message_type="text",
                        metadata_json={"decision_action": "deny"},
                    ))
                    deny_db.commit()
                finally:
                    try:
                        deny_db.close()
                    except Exception:  # noqa: BLE001
                        pass
                sse_session = _router_session_id or str(session_id)
                yield f"event: session\ndata: {_json.dumps({'session_id': sse_session})}\n\n"
                yield f"event: agent_start\ndata: {_json.dumps({'agent_key': 'decision_maker', 'display_name': 'Decision Maker'})}\n\n"
                # Stream msg as tokens for a natural UX
                for i in range(0, len(msg), 200):
                    yield f"event: token\ndata: {_json.dumps({'content': msg[i:i+200]})}\n\n"
                    await asyncio.sleep(0.005)
                yield f"event: agent_complete\ndata: {_json.dumps({'agent_key': 'decision_maker', 'output_type': 'general', 'status': 'completed'})}\n\n"
                yield f"event: done\ndata: {_json.dumps({'session_id': sse_session, 'output_type': 'general'})}\n\n"
                return

            # approve / change → invoke decision_maker directly, bypassing the
            # router classifier (we already know the right agent).
            from app.services.langchain.streaming_handler import stream_decision_maker_directly
            resume_message = original_message or decision_content

            async for event in stream_decision_maker_directly(
                message=resume_message,
                display_message=decision_content,
                session_id=_router_session_id or str(session_id),
                tender_id=stored_tender_id or _tender_id,
                user_id=_user_id,
                proposal_session_id=_proposal_session_id,
                file_metadata=stored_file_metadata,
                mode=("execution" if action in ("approve", "next") else "planning"),
                approved_plan=(plan if action == "approve" else None),
                change_feedback=(feedback or None if action == "change" else None),
            ):
                yield event

        def _on_error(e: Exception) -> list[str]:
            logger.error(f"Decision response failed: {e}", exc_info=True)
            friendly = format_user_error(e)
            return [
                f"event: error\ndata: {_json.dumps({'message': friendly})}\n\n",
                f"event: done\ndata: {_json.dumps({'error': friendly})}\n\n",
            ]

        # Executing an approved plan is the most expensive turn the platform
        # runs — it is the work the user just said yes to. Undetached, a closed
        # tab cancelled the generator mid-execution and everything it produced
        # was discarded with nothing written, which from the session is
        # indistinguishable from never having approved the plan at all.
        async for event in detached_stream(
            _source, label=f"decision {_proposal_session_id}", on_error=_on_error
        ):
            yield event

    return StreamingResponse(
        _with_keepalive(generate(), interval=15.0),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Conversation History ---

@router.post("/sessions/{session_id}/action/respond")
async def respond_to_pending_action(
    session_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approve or deny a write the agent asked to perform.

    Body: ``{action: "approve" | "deny"}``

    On **approve** the agent re-runs with a single-use grant authorising exactly
    that call — same tool, same arguments. It executes for real, the gate
    re-arms behind it, and any further write suspends again.

    On **deny** the pending action is cleared and nothing is executed.

    Returns SSE in the same format as `/chat/stream` and `/decision/respond`, so
    the frontend reuses its existing event handler.
    """

    # Approving a gated write re-runs the agent — same spend as a fresh turn.
    assert_within_budget(db, current_user)
    assert_session_access(db, session_id, current_user.id)
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    action = (body.get("action") or "").strip().lower()
    if action not in ("approve", "deny"):
        raise HTTPException(status_code=400, detail="action must be approve or deny")

    state = dict(session.pipeline_state or {})
    pending = state.get("pending_action") or {}
    approved_action = pending.get("action")
    if not approved_action:
        raise HTTPException(status_code=400, detail="No pending action to respond to")

    # Clear it up front, whichever way the user answered. A pending action left
    # in place could be replayed by a second request — one approval must
    # authorise exactly one execution.
    state.pop("pending_action", None)
    session.pipeline_state = state
    db.add(session)
    db.commit()

    _router_session_id = session.router_session_id
    _tender_id = pending.get("tender_id") or session.tender_id
    _user_id = current_user.id
    _proposal_session_id = session.id
    _original_message = pending.get("original_message") or ""
    _file_metadata = pending.get("file_metadata") or {}
    _summary = (approved_action or {}).get("summary") or "that action"

    if action == "deny":
        declined = f"Okay — I have not done that. ({_summary})"
        try:
            db.add(ProposalMessage(
                session_id=_proposal_session_id,
                role="assistant",
                content=declined,
                message_type="text",
                metadata_json={"action_status": "denied"},
            ))
            db.commit()
        except Exception:
            db.rollback()

        async def deny_stream():
            yield f"event: token\ndata: {json.dumps({'content': declined})}\n\n"
            yield f"event: done\ndata: {json.dumps({'session_id': _router_session_id})}\n\n"

        return StreamingResponse(
            deny_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    from app.services.langchain.streaming_handler import stream_decision_maker_directly

    async def approve_stream():
        from app.services.langchain.error_utils import format_user_error
        from app.services.detached_stream import detached_stream

        async def _source():
            async for event in stream_decision_maker_directly(
                message=_original_message,
                display_message=None,
                session_id=_router_session_id,
                tender_id=_tender_id,
                user_id=_user_id,
                proposal_session_id=_proposal_session_id,
                mode="autonomous",
                file_metadata=_file_metadata,
                approved_action=approved_action,
            ):
                yield event

        def _on_error(e: Exception) -> list[str]:
            logger.error(f"Approved-action run failed: {e}", exc_info=True)
            friendly = format_user_error(e)
            return [
                f"event: error\ndata: {json.dumps({'message': friendly})}\n\n",
                f"event: done\ndata: {json.dumps({'session_id': _router_session_id, 'error': friendly})}\n\n",
            ]

        # The grant is already spent — `pending_action` was popped and committed
        # above, so one approval authorises exactly one execution. Undetached, a
        # closed tab cancelled that execution mid-flight and the grant was gone
        # with it: the write the user approved never happened, and approving it
        # again was not possible without asking for the whole thing afresh.
        async for event in detached_stream(
            _source, label=f"action {_proposal_session_id}", on_error=_on_error
        ):
            yield event

    return StreamingResponse(
        _with_keepalive(approve_stream(), interval=15.0),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/history")
def get_history(
    session_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get conversation history for a session."""
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.router_session_id:
        # Use AgentConversationHistory for richer data
        from app.services.langchain.memory_service import get_conversation_history
        turns = get_conversation_history(db, session.router_session_id, limit=limit)

        # Load all attachments for this session to enrich user messages
        session_attachments = (
            db.query(ChatAttachment)
            .filter(ChatAttachment.session_id == session_id)
            .all()
        )
        # Group attachments by message_id
        attachments_by_msg: dict[int, list] = {}
        # Count linked children per parent attachment
        linked_counts: dict[int, int] = {}
        for att in session_attachments:
            if att.parent_attachment_id:
                linked_counts[att.parent_attachment_id] = linked_counts.get(att.parent_attachment_id, 0) + 1

        for att in session_attachments:
            if att.message_id and not att.parent_attachment_id:  # Only show root attachments
                attachments_by_msg.setdefault(att.message_id, []).append({
                    "id": att.id,
                    "file_name": att.file_name,
                    "file_type": att.file_type,
                    "file_size": att.file_size,
                    "extraction_status": att.extraction_status,
                    "linked_document_count": linked_counts.get(att.id, 0),
                })

        # Match ProposalMessage IDs to conversation turns by created_at for user messages
        user_proposal_msgs = (
            db.query(ProposalMessage)
            .filter(
                ProposalMessage.session_id == session_id,
                ProposalMessage.role == "user",
            )
            .order_by(ProposalMessage.created_at)
            .all()
        )
        # Build a lookup: index of user turn -> attachments
        user_turn_attachments: dict[int, list] = {}
        for pm in user_proposal_msgs:
            if pm.id in attachments_by_msg:
                user_turn_attachments[pm.id] = attachments_by_msg[pm.id]

        # Align the two lists from the END, not the start. `turns` is the most
        # recent `limit` turns of the conversation, while `user_proposal_msgs`
        # is every user message in the session from the beginning. Walking both
        # from index 0 hands the newest turns the oldest messages' attachments,
        # so on a session past the limit a user reloading the page sees their
        # files hanging off the wrong messages. The window is the tail, so the
        # rows that belong to it are the tail.
        user_turns_in_window = sum(1 for t in turns if t.role == "user")
        aligned_msgs = (
            user_proposal_msgs[-user_turns_in_window:]
            if user_turns_in_window else []
        )
        pm_idx = 0
        result = []
        for t in turns:
            entry = {
                "id": t.id,
                "role": t.role,
                "content": t.content,
                "tool_calls": t.tool_calls,
                "output_type": t.output_type,
                "routed_from": t.routed_from,
                "metadata": t.metadata_json,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "attachments": [],
            }
            if t.role == "user" and pm_idx < len(aligned_msgs):
                pm = aligned_msgs[pm_idx]
                entry["attachments"] = user_turn_attachments.get(pm.id, [])
                pm_idx += 1
            result.append(entry)
        return result

    # Fallback to ProposalMessage. Newest `limit`, then back into chronological
    # order — for the same reason the AgentConversationHistory path does it:
    # ordering ascending and then limiting shows a long session its opening and
    # hides everything the user most recently said.
    messages = list(reversed(
        db.query(ProposalMessage)
        .filter(ProposalMessage.session_id == session_id)
        .order_by(ProposalMessage.created_at.desc(), ProposalMessage.id.desc())
        .limit(limit)
        .all()
    ))
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "tool_calls": None,
            "output_type": m.message_type,
            "routed_from": None,
            "metadata": m.metadata_json,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in messages
    ]


# --- Approval ---

@router.post("/sessions/{session_id}/submit-review")
def submit_for_review(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit session for review. Delegates to existing approval service."""
    from app.services.approval_service import submit_for_review as do_submit
    try:
        review = do_submit(db, session_id, current_user.id)
        return {
            "id": review.id,
            "session_id": review.session_id,
            "status": review.status,
            "created_at": review.created_at.isoformat() if review.created_at else None,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# --- Artifacts ---

@router.get("/sessions/{session_id}/artifacts")
def list_artifacts(
    session_id: int,
    artifact_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List artifacts for a session (latest version of each)."""
    assert_session_access(db, session_id, current_user.id)
    from app.services.artifact_service import list_artifacts as do_list
    return do_list(db, session_id, artifact_type=artifact_type)


@router.get("/artifacts/{artifact_id}")
def get_artifact(
    artifact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a single artifact."""
    from app.services.artifact_service import get_artifact as do_get, _artifact_to_dict
    artifact = do_get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    assert_artifact_access(db, artifact, current_user.id)
    return _artifact_to_dict(artifact)


@router.put("/artifacts/{artifact_id}")
def update_artifact(
    artifact_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit an artifact (creates a new version)."""
    from app.services.artifact_service import (
        get_artifact as do_get,
        update_artifact as do_update,
        _artifact_to_dict,
    )
    content = body.get("content")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    artifact = do_get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    assert_artifact_access(db, artifact, current_user.id)

    new_version = do_update(
        db, artifact_id, content,
        structured_data=body.get("structured_data"),
        updated_by=current_user.id,
    )
    if not new_version:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _artifact_to_dict(new_version)


@router.get("/artifacts/{artifact_id}/versions")
def get_artifact_versions(
    artifact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get all versions of an artifact."""
    from app.services.artifact_service import get_artifact as do_get, get_artifact_versions as do_versions
    artifact = do_get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    assert_artifact_access(db, artifact, current_user.id)
    return do_versions(db, artifact_id)


@router.get("/artifacts/{artifact_id}/download")
def download_artifact_file(
    artifact_id: int,
    single_sheet: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download the binary file attached to an artifact (e.g. cost_breakdown_xlsx).

    Storage-aware: serves the object via ``storage_service`` (local OR R2,
    handling both new keys and legacy ``./uploads/...`` paths). When the stored
    file is MISSING — never written, or lost on an R2/deploy — and the artifact
    is a ``cost_breakdown_xlsx``, the workbook is REGENERATED from the persisted
    ``CostBreakdown`` on demand, so the user can always download a complete Excel.
    """
    import io
    import os
    from fastapi.responses import StreamingResponse
    from app.services.artifact_service import get_artifact as do_get
    from app.services.storage_service import get_storage_service, StorageService

    artifact = do_get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    assert_artifact_access(db, artifact, current_user.id)

    storage = get_storage_service()
    XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _stream(data: bytes, name: str):
        media_type = XLSX_MIME if name.lower().endswith(".xlsx") else "application/octet-stream"
        return StreamingResponse(
            io.BytesIO(data),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    # 1) Serve the stored object via storage_service.
    if artifact.file_path:
        key = StorageService.normalize_key(artifact.file_path)
        try:
            if storage.file_exists_sync(key):
                return _stream(
                    storage.download_file_sync(key),
                    artifact.file_name or os.path.basename(key),
                )
        except Exception as e:
            logger.warning(f"[artifact download] storage read failed for {key!r}: {e}")
        # Legacy absolute path still present on local disk (pre-storage artifacts).
        if os.path.isabs(artifact.file_path) and os.path.exists(artifact.file_path):
            with open(artifact.file_path, "rb") as f:
                return _stream(
                    f.read(), artifact.file_name or os.path.basename(artifact.file_path)
                )

    # 2) File missing → regenerate the cost-breakdown XLSX from the DB.
    if artifact.artifact_type == "cost_breakdown_xlsx":
        from app.services import cost_breakdown_service
        from app.models.cost_breakdown import CostBreakdown

        meta = artifact.metadata_json or {}
        sd = artifact.structured_data if isinstance(artifact.structured_data, dict) else {}
        cb_id = meta.get("cost_breakdown_id") or sd.get("cost_breakdown_id")
        breakdown = None
        if cb_id:
            breakdown = db.query(CostBreakdown).filter(CostBreakdown.id == cb_id).first()
        if breakdown is None and meta.get("tender_id"):
            breakdown = cost_breakdown_service.get_latest_for_tender(db, meta["tender_id"])
        if breakdown is not None:
            rendered = cost_breakdown_service.render_breakdown_xlsx_bytes(
                db, breakdown, single_sheet=single_sheet
            )
            if rendered:
                new_fname, data, _summary, _rows = rendered
                key = f"generated_docs/{new_fname}"
                try:
                    storage.upload_file_sync(key, data, content_type=XLSX_MIME)
                    artifact.file_path = key
                    artifact.file_name = new_fname
                    db.commit()
                    # The stored workbook just changed under the same preview
                    # key (previews/artifact_{id}/v{version}.pdf) — without
                    # this, a `ready` manifest would keep serving the PDF
                    # rendered from the workbook that was just replaced,
                    # indefinitely. Wrapped so a render-enqueue failure can
                    # never break the download itself.
                    try:
                        from app.worker.preview_tasks import enqueue_preview_render
                        enqueue_preview_render(artifact.id)
                    except Exception as e:
                        logger.warning(f"[artifact download] preview re-enqueue failed: {e}")
                except Exception as e:
                    logger.warning(f"[artifact download] regen persist failed: {e}")
                    try:
                        db.rollback()
                    except Exception:
                        pass
                logger.info(
                    f"[artifact download] regenerated XLSX for artifact {artifact_id} "
                    f"from cost breakdown {breakdown.id} ({len(_rows)} rows)"
                )
                return _stream(data, new_fname)

    raise HTTPException(
        status_code=404,
        detail="Artifact file not found and could not be regenerated",
    )


@router.get("/artifacts/{artifact_id}/download-url")
async def get_artifact_download_url(
    artifact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hand the browser a short-lived presigned URL for the artifact's file.

    The bytes go R2 -> browser directly. The previous `/download` route pulled
    the whole object into backend memory and re-streamed it, which crossed
    Railway twice and held a threadpool slot for the entire transfer. That route
    is kept for backward compatibility and as the local-backend fallback.

    Always 200. `url: null` means "use /download instead" — either the backend
    cannot presign (local filesystem) or the object is missing and /download's
    regenerate-from-CostBreakdown branch needs to run.
    """
    from app.services.artifact_service import get_artifact_file_ref
    from app.services.storage_service import get_storage_service, StorageService

    artifact = get_artifact_file_ref(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    assert_artifact_access(db, artifact, current_user.id)

    file_name = artifact.file_name or "download.xlsx"
    if not artifact.file_path:
        return {"url": None, "file_name": file_name, "regenerate_required": True}

    storage = get_storage_service()
    key = StorageService.normalize_key(artifact.file_path)

    try:
        # `file_exists_sync` is a blocking boto3 HEAD. Calling it from an
        # `async def` runs it ON the event loop, so every other request this
        # worker is serving stalls for the length of an R2 round trip — and
        # botocore's default retry budget means a wedged HEAD can hold the
        # loop for minutes. The async sibling exists for exactly this.
        exists = await storage.file_exists(key)
    except Exception as e:
        logger.warning(f"[artifact download-url] existence check failed for {key!r}: {e}")
        exists = False
    if not exists:
        return {"url": None, "file_name": file_name, "regenerate_required": True}

    if getattr(storage, "_backend", "local") != "r2":
        return {"url": None, "file_name": file_name, "regenerate_required": False}

    settings = get_settings()
    url = await storage.get_presigned_url(
        key,
        expires_in=settings.presigned_download_ttl_seconds,
        response_content_disposition=f'attachment; filename="{file_name}"',
    )
    return {"url": url, "file_name": file_name, "regenerate_required": False}


@router.get("/artifacts/{artifact_id}/preview")
async def get_artifact_preview(
    artifact_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manifest + presigned PDF URL for the rendered workbook preview.

    Always 200 with a status. `pending` (never rendered — including every
    artifact created before this feature) and `failed` are expected states; the
    UI falls back to its table view for anything that is not `ready`.
    """
    from app.services.artifact_service import get_artifact_file_ref
    from app.services.storage_service import get_storage_service

    artifact = get_artifact_file_ref(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    assert_artifact_access(db, artifact, current_user.id)

    preview = (artifact.metadata_json or {}).get("preview") or {}
    status = preview.get("status") or "pending"
    body = {
        "status": status,
        "pdf_url": None,
        "page_count": preview.get("page_count", 0),
        "sheets": preview.get("sheets", []),
        "error": preview.get("error"),
    }

    pdf_key = preview.get("pdf_key")
    if status == "ready" and pdf_key:
        storage = get_storage_service()
        if getattr(storage, "_backend", "local") == "r2":
            # Longer-lived embed TTL, not the short download TTL: pdf.js keeps
            # this URL alive (range requests) for as long as the preview pane
            # stays open, which can outlast a short-lived download link.
            body["pdf_url"] = await storage.get_presigned_url(
                pdf_key, expires_in=get_settings().presigned_url_ttl_seconds
            )
        else:
            body["status"] = "unavailable"
    return body


@router.post("/artifacts/{artifact_id}/export")
def export_artifact(
    artifact_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export artifact to DOCX/PDF."""
    from app.services.artifact_service import get_artifact as do_get
    artifact = do_get(db, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    assert_artifact_access(db, artifact, current_user.id)

    export_format = body.get("format", "docx")

    # Use existing document generation for DOCX
    try:
        from app.services.proposal_service import generate_document_from_content
        file_path = generate_document_from_content(
            content=artifact.content,
            title=artifact.title,
            format=export_format,
        )
        from fastapi.responses import FileResponse
        return FileResponse(
            file_path,
            filename=f"{artifact.title}.{export_format}",
            media_type="application/octet-stream",
        )
    except (ImportError, AttributeError):
        # If no document generation service available, return content as-is
        raise HTTPException(status_code=501, detail="Document export not yet implemented")


# --- Linked Documents ---

@router.get("/sessions/{session_id}/attachments/{attachment_id}/linked-documents")
def get_linked_attachments(
    session_id: int,
    attachment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get child attachments extracted from a parent master document."""
    parent = db.query(ChatAttachment).filter(
        ChatAttachment.id == attachment_id,
        ChatAttachment.session_id == session_id,
    ).first()
    if not parent:
        raise HTTPException(status_code=404, detail="Attachment not found")

    children = db.query(ChatAttachment).filter(
        ChatAttachment.parent_attachment_id == attachment_id,
    ).order_by(ChatAttachment.created_at.desc()).all()

    return {
        "parent": {
            "id": parent.id,
            "file_name": parent.file_name,
            "extraction_status": parent.extraction_status,
        },
        "linked_documents": [
            {
                "id": c.id,
                "file_name": c.file_name,
                "file_size": c.file_size,
                "file_type": c.file_type,
                "source_url": c.source_url,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in children
        ],
        "total": len(children),
    }


# --- Session Documents (Tender Library) ---

@router.get("/sessions/{session_id}/documents")
def get_session_documents(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all tender documents for this session's linked tender."""
    from app.models.tender import TenderDocument

    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session.tender_id:
        return {"documents": [], "total": 0}

    docs = db.query(TenderDocument).filter(
        TenderDocument.tender_id == session.tender_id,
    ).order_by(TenderDocument.uploaded_at.desc()).all()

    return {
        "documents": [
            {
                "id": d.id,
                "file_name": d.file_name,
                "file_size": d.file_size,
                "mime_type": d.mime_type,
                "document_type": d.document_type,
                "extraction_status": d.extraction_status,
                "gem_file_id": d.gem_file_id,
                "source_url": d.source_url,
                "parent_document_id": d.parent_document_id,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
            }
            for d in docs
        ],
        "total": len(docs),
        "pending_count": sum(1 for d in docs if d.extraction_status in ("pending", "processing")),
    }


@router.post("/sessions/{session_id}/documents/{doc_id}/download")
async def trigger_document_download(
    session_id: int,
    doc_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-attempt download for a failed/pending GEM document."""
    from app.models.tender import TenderDocument

    session = _load_owned_session(db, session_id, current_user)
    if not session or not session.tender_id:
        raise HTTPException(status_code=404, detail="Session not found or no tender linked")

    doc = db.query(TenderDocument).filter(
        TenderDocument.id == doc_id,
        TenderDocument.tender_id == session.tender_id,
    ).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not doc.gem_file_id:
        raise HTTPException(status_code=400, detail="Not a GEM referenced document")

    doc.extraction_status = "pending"
    db.commit()

    from app.services.gem_file_service import process_gem_file_ids_background
    # Build minimal analysis text with just this file ID for the background processor
    fake_text = f"{doc.file_name} (file: {doc.gem_file_id})"
    background_tasks.add_task(
        process_gem_file_ids_background,
        session.tender_id, fake_text, current_user.id,
    )

    return {"status": "download_queued", "doc_id": doc_id}


@router.post("/sessions/{session_id}/documents/upload")
async def upload_session_document(
    session_id: int,
    file: UploadFile = File(...),
    gem_file_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a document to the session's tender library.
    If gem_file_id is provided, updates an existing pending record."""
    from app.models.tender import TenderDocument
    from app.services.storage_service import get_storage_service, key_tender_doc

    session = _load_owned_session(db, session_id, current_user)
    if not session or not session.tender_id:
        raise HTTPException(status_code=404, detail="Session not found or no tender linked")

    content = await file.read()
    storage = get_storage_service()
    storage_key = key_tender_doc(session.tender_id, file.filename)
    storage.upload_file_sync(
        storage_key, content,
        content_type=file.content_type or "application/pdf",
    )

    # Check if updating an existing pending GEM document
    if gem_file_id:
        existing = db.query(TenderDocument).filter(
            TenderDocument.tender_id == session.tender_id,
            TenderDocument.gem_file_id == gem_file_id,
        ).first()
        if existing:
            existing.file_path = storage_key
            existing.file_name = file.filename
            existing.file_size = len(content)
            existing.mime_type = file.content_type or "application/pdf"
            existing.extraction_status = "completed"
            db.commit()
            return {"id": existing.id, "file_name": existing.file_name, "status": "updated"}

    # Create new document record
    doc = TenderDocument(
        tender_id=session.tender_id,
        file_name=file.filename,
        file_path=storage_key,
        file_size=len(content),
        mime_type=file.content_type or "application/pdf",
        document_type="manual_upload",
        uploaded_by=current_user.id,
        extraction_status="completed",
        gem_file_id=gem_file_id,
    )
    db.add(doc)
    db.commit()

    return {"id": doc.id, "file_name": doc.file_name, "status": "created"}


@router.post("/sessions/{session_id}/documents/download-all")
async def trigger_bulk_download(
    session_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trigger download for all pending/failed GEM documents."""
    from app.models.tender import TenderDocument

    session = _load_owned_session(db, session_id, current_user)
    if not session or not session.tender_id:
        raise HTTPException(status_code=404, detail="Session not found or no tender linked")

    pending = db.query(TenderDocument).filter(
        TenderDocument.tender_id == session.tender_id,
        TenderDocument.gem_file_id.isnot(None),
        TenderDocument.extraction_status.in_(["pending", "failed", "failed_auth"]),
    ).all()

    if not pending:
        return {"status": "no_pending", "count": 0}

    # Reset all to pending
    for doc in pending:
        doc.extraction_status = "pending"
    db.commit()

    # Build fake analysis text with all file IDs
    lines = [f"{d.file_name} (file: {d.gem_file_id})" for d in pending]
    fake_text = "\n".join(lines)

    from app.services.gem_file_service import process_gem_file_ids_background
    background_tasks.add_task(
        process_gem_file_ids_background,
        session.tender_id, fake_text, current_user.id,
    )

    return {"status": "download_queued", "count": len(pending)}


# --- Pipeline ---

@router.post("/sessions/{session_id}/pipeline/{step}")
async def trigger_pipeline_step(
    session_id: int,
    step: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Trigger a pipeline step for a tender-linked session.
    Valid steps: analyze_documents, generate_checklist, generate_documents, research_costing
    """
    valid_steps = ["analyze_documents", "generate_checklist", "generate_documents", "research_costing"]
    if step not in valid_steps:
        raise HTTPException(status_code=400, detail=f"Invalid step. Valid: {valid_steps}")

    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not session.tender_id:
        raise HTTPException(status_code=400, detail="Pipeline requires a linked tender")

    try:
        from app.services.langchain.langchain_execution_service import run_tender_pipeline
        result = await run_tender_pipeline(
            db=db,
            tender_id=session.tender_id,
            user_id=current_user.id,
            steps=[step],
        )

        # Update pipeline state
        pipeline_state = session.pipeline_state or {}
        if result.get("status") in ("completed", "completed_with_errors"):
            pipeline_state[step] = True
            session.pipeline_state = pipeline_state
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(session, "pipeline_state")
            db.commit()

        # Auto-create artifact from pipeline result
        _create_pipeline_artifact(db, session.id, step, result, current_user.id)

        return result

    except Exception as e:
        logger.error(f"Pipeline step '{step}' failed for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions/{session_id}/pipeline/status")
def get_pipeline_status(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get pipeline completion state."""
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "tender_id": session.tender_id,
        "pipeline_state": session.pipeline_state or {},
        "has_tender": session.tender_id is not None,
    }


# --- Suggestions ---

@router.get("/sessions/{session_id}/suggestions")
def get_suggestions(
    session_id: int,
    last_output_type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get context-aware suggestions for a session."""
    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    from app.services.suggestion_service import generate_suggestions
    return generate_suggestions(
        db=db,
        session_id=session_id,
        tender_id=session.tender_id,
        pipeline_state=session.pipeline_state,
        last_output_type=last_output_type,
    )


# --- Clarifications (Phase B) ---

@router.get("/sessions/{session_id}/clarifications")
def list_clarifications(
    session_id: int,
    status: Optional[str] = Query(None, pattern="^(pending|answered|cancelled)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List clarifications for a session (default: all statuses)."""
    from app.models.clarification import PendingClarification

    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    q = db.query(PendingClarification).filter(PendingClarification.session_id == session_id)
    if status:
        q = q.filter(PendingClarification.status == status)
    rows = q.order_by(PendingClarification.created_at.desc()).limit(50).all()
    return [
        {
            "id": r.id,
            "agent_key": r.agent_key,
            "question": r.question,
            "options": r.options or [],
            "context": r.context or {},
            "status": r.status,
            "answer": r.answer,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "answered_at": r.answered_at.isoformat() if r.answered_at else None,
        }
        for r in rows
    ]


@router.post("/sessions/{session_id}/clarifications/{clarification_id}/answer")
async def answer_clarification(
    session_id: int,
    clarification_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Record a user answer to a pending clarification and resume the agent.

    Body: { answer: str }

    Since the LangGraph checkpointer is deferred to Phase C, "resume" here means:
    open a new streaming chat turn where the original question + user answer are
    injected as context, and let the router re-run. The frontend reuses its
    existing SSE plumbing — this endpoint returns a streaming response.
    """
    from datetime import datetime, timezone
    from app.models.clarification import PendingClarification

    session = _load_owned_session(db, session_id, current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    row = db.query(PendingClarification).filter(
        PendingClarification.id == clarification_id,
        PendingClarification.session_id == session_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Clarification not found")
    if row.status != "pending":
        raise HTTPException(status_code=400, detail=f"Clarification already {row.status}")

    answer = (body.get("answer") or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="Answer is required")

    row.answer = answer
    row.status = "answered"
    row.answered_at = datetime.now(timezone.utc)
    db.commit()

    # Resume: synthesise a follow-up user turn and stream through the router.
    from app.services.langchain.streaming_handler import stream_router_response
    from app.services.langchain.error_utils import format_user_error
    import json as _json

    _router_session_id = session.router_session_id or str(uuid.uuid4())
    if not session.router_session_id:
        session.router_session_id = _router_session_id
        db.commit()

    agent_hint = row.agent_key or "the same agent"
    resume_message = (
        f"[Clarification answer]\n"
        f"Earlier {agent_hint} asked: {row.question}\n"
        f"The user answered: {answer}\n\n"
        f"Continue the original task using this answer. Do not ask the same "
        f"question again."
    )

    _tender_id = session.tender_id
    _user_id = current_user.id
    _proposal_session_id = session.id

    async def _with_keepalive(stream, interval=15.0):
        """Yield stream events; insert SSE comments as keepalives during quiet periods."""
        queue: asyncio.Queue = asyncio.Queue()

        async def _producer():
            try:
                async for event in stream:
                    await queue.put(event)
            finally:
                await queue.put(None)

        producer_task = asyncio.create_task(_producer())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=interval)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                yield item
        finally:
            if not producer_task.done():
                producer_task.cancel()

    async def generate():
        try:
            async for event in stream_router_response(
                db=db,
                message=resume_message,
                session_id=_router_session_id,
                tender_id=_tender_id,
                user_id=_user_id,
                proposal_session_id=_proposal_session_id,
                file_metadata=None,
                display_message=f"(answered: {answer[:140]})",
            ):
                yield event
        except Exception as e:
            logger.error(f"Clarification resume stream failed: {e}", exc_info=True)
            friendly = format_user_error(e)
            yield f"event: error\ndata: {_json.dumps({'message': friendly})}\n\n"
            yield f"event: done\ndata: {_json.dumps({'session_id': _router_session_id, 'error': friendly})}\n\n"

    return StreamingResponse(
        _with_keepalive(generate(), interval=15.0),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_id}/clarifications/{clarification_id}/cancel")
def cancel_clarification(
    session_id: int,
    clarification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Dismiss a pending clarification without answering."""
    from datetime import datetime, timezone
    from app.models.clarification import PendingClarification

    row = db.query(PendingClarification).filter(
        PendingClarification.id == clarification_id,
        PendingClarification.session_id == session_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Clarification not found")
    if row.status != "pending":
        return {"id": row.id, "status": row.status}

    row.status = "cancelled"
    row.answered_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": row.id, "status": row.status}


# --- Helpers ---

def _session_to_dict(session: ProposalSession) -> dict:
    """Convert ProposalSession to dict for API responses."""
    return {
        "id": session.id,
        "tender_id": session.tender_id,
        "title": session.title,
        "status": session.status,
        "mode": session.mode or ("tender_linked" if session.tender_id else "standalone"),
        "agent_type": session.agent_type,
        "router_session_id": session.router_session_id,
        "pipeline_state": session.pipeline_state or {},
        "template_id": session.template_id,
        "current_version": session.current_version,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _create_pipeline_artifact(
    db: Session, session_id: int, step: str, result: dict, user_id: int
):
    """Create an artifact from a pipeline step result."""
    from app.services.artifact_service import create_artifact

    step_artifact_map = {
        "analyze_documents": ("analysis", "Tender Analysis"),
        "generate_checklist": ("checklist", "Submission Checklist"),
        "generate_documents": ("document", "Generated Documents"),
        "research_costing": ("cost_breakdown", "Cost Breakdown"),
    }

    mapping = step_artifact_map.get(step)
    if not mapping:
        return

    artifact_type, title = mapping

    # Extract content from result
    if step == "analyze_documents":
        content = json.dumps(result.get("analysis_result", {}), indent=2, default=str)
        structured = result.get("analysis_result")
    elif step == "generate_checklist":
        items = result.get("checklist_items", [])
        content = _format_checklist_markdown(items)
        structured = items
    elif step == "generate_documents":
        docs = result.get("generated_documents", [])
        content = _format_documents_markdown(docs)
        structured = docs
    elif step == "research_costing":
        costing = result.get("costing_data", {})
        content = json.dumps(costing, indent=2, default=str)
        structured = costing
    else:
        return

    if content:
        create_artifact(
            db=db,
            session_id=session_id,
            artifact_type=artifact_type,
            title=title,
            content=content,
            structured_data=structured,
            agent_key=step,
            created_by=user_id,
        )


def _format_checklist_markdown(items: list) -> str:
    """Format checklist items as markdown table."""
    if not items:
        return "No checklist items generated."

    lines = ["# Submission Checklist\n", "| # | Document | Required | Status |", "|---|----------|----------|--------|"]
    for i, item in enumerate(items, 1):
        name = item.get("name", item.get("item_name", "Unknown"))
        required = "✅ Required" if item.get("is_required", True) else "Optional"
        status = item.get("status", "pending")
        lines.append(f"| {i} | {name} | {required} | {status} |")
    return "\n".join(lines)


def _format_documents_markdown(docs: list) -> str:
    """Format generated documents list as markdown."""
    if not docs:
        return "No documents generated."

    lines = ["# Generated Documents\n"]
    for doc in docs:
        name = doc.get("item_name", doc.get("name", "Document"))
        status = doc.get("status", "unknown")
        lines.append(f"- **{name}**: {status}")
        if doc.get("content"):
            lines.append(f"\n{doc['content'][:500]}...\n")
    return "\n".join(lines)
