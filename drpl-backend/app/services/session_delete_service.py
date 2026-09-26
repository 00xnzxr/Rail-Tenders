"""
DRPL Backend - Session Delete Service
Cascade deletion of a Command Center session and all related data.
"""

import logging
import os

from sqlalchemy.orm import Session

from app.models.proposal import ProposalSession, ProposalMessage
from app.models.artifact import CommandCenterArtifact
from app.models.chat_attachment import ChatAttachment
from app.models.agent_memory import AgentConversationHistory
from app.models.checklist import ChecklistItem
from app.models.tender import Tender

logger = logging.getLogger(__name__)


def delete_session(db: Session, session: ProposalSession) -> dict:
    """
    Delete a Command Center session and all related data.

    Does NOT commit — the caller is responsible for committing the transaction
    and then cleaning up files from the returned `files_to_delete` list.

    Returns a dict with deletion counts and file paths to clean up.
    """
    session_id = session.id
    router_session_id = session.router_session_id
    tender_id = session.tender_id

    files_to_delete: list[str] = []
    counts = {
        "attachments_deleted": 0,
        "artifacts_deleted": 0,
        "messages_deleted": 0,
        "history_deleted": 0,
        "checklist_items_deleted": 0,
        "tender_deleted": False,
    }

    # 1. Chat attachments — collect file paths, then delete rows
    attachments = db.query(ChatAttachment).filter(
        ChatAttachment.session_id == session_id
    ).all()
    for att in attachments:
        if att.file_path and os.path.exists(att.file_path):
            files_to_delete.append(att.file_path)
    counts["attachments_deleted"] = (
        db.query(ChatAttachment)
        .filter(ChatAttachment.session_id == session_id)
        .delete(synchronize_session=False)
    )

    # 2. Artifacts — collect exported file paths, then delete rows
    artifacts = db.query(CommandCenterArtifact).filter(
        CommandCenterArtifact.session_id == session_id
    ).all()
    for art in artifacts:
        if art.file_path and os.path.exists(art.file_path):
            files_to_delete.append(art.file_path)
    counts["artifacts_deleted"] = (
        db.query(CommandCenterArtifact)
        .filter(CommandCenterArtifact.session_id == session_id)
        .delete(synchronize_session=False)
    )

    # 3. Proposal messages
    counts["messages_deleted"] = (
        db.query(ProposalMessage)
        .filter(ProposalMessage.session_id == session_id)
        .delete(synchronize_session=False)
    )

    # 4. Agent conversation history (keyed by router_session_id string)
    if router_session_id:
        counts["history_deleted"] = (
            db.query(AgentConversationHistory)
            .filter(AgentConversationHistory.session_id == router_session_id)
            .delete(synchronize_session=False)
        )

    # 5. Auto-created tender cleanup
    #    Only delete tenders created by the Command Center (portal='command_center', tender_id starts with 'cc-')
    if tender_id:
        tender = db.query(Tender).filter(Tender.id == tender_id).first()
        if tender and tender.portal == "command_center" and (tender.tender_id or "").startswith("cc-"):
            # Delete checklist items for this tender
            counts["checklist_items_deleted"] = (
                db.query(ChecklistItem)
                .filter(ChecklistItem.tender_id == tender_id)
                .delete(synchronize_session=False)
            )
            # Delete the tender itself
            db.delete(tender)
            counts["tender_deleted"] = True

    # 6. Delete the session
    db.delete(session)

    logger.info(
        f"Staged deletion of session {session_id}: "
        f"{counts['attachments_deleted']} attachments, "
        f"{counts['artifacts_deleted']} artifacts, "
        f"{counts['messages_deleted']} messages, "
        f"{counts['history_deleted']} history turns, "
        f"{counts['checklist_items_deleted']} checklist items, "
        f"tender_deleted={counts['tender_deleted']}, "
        f"{len(files_to_delete)} files to clean up"
    )

    return {
        **counts,
        "files_to_delete": files_to_delete,
    }


def cleanup_files(files: list[str]) -> int:
    """
    Best-effort file cleanup after a successful DB commit.
    Returns the number of files successfully removed.
    """
    removed = 0
    for path in files:
        try:
            if os.path.exists(path):
                os.remove(path)
                removed += 1
        except OSError as e:
            logger.warning(f"Failed to remove file {path}: {e}")
    return removed
