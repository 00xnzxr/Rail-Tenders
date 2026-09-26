"""
DRPL Backend - Suggestion Service
Rule-based context-aware next-action suggestions for Command Center sessions.
No LLM calls — purely deterministic based on session state.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def generate_suggestions(
    db: Session,
    session_id: int,
    tender_id: Optional[int],
    pipeline_state: Optional[dict],
    last_output_type: Optional[str] = None,
) -> list[dict]:
    """
    Generate context-aware suggestions based on session state.

    Returns list of suggestion dicts:
        {
            "text": str,           # Display text / chat input
            "action": str,         # "chat", "pipeline_step", "navigate"
            "step": str|None,      # Pipeline step name (if action=pipeline_step)
            "icon": str,           # Icon key for frontend
            "priority": int,       # Lower = higher priority
        }
    """
    suggestions = []
    state = pipeline_state or {}

    has_tender = tender_id is not None
    has_analysis = state.get("analyze_documents", False)
    has_checklist = state.get("generate_checklist", False)
    has_documents = state.get("generate_documents", False)
    has_costing = state.get("research_costing", False)

    if has_tender:
        # Tender-linked pipeline suggestions
        if not has_analysis:
            suggestions.append({
                "text": "Analyze the tender documents and identify key requirements",
                "action": "chat",
                "step": "analyze_documents",
                "icon": "search",
                "priority": 1,
            })

        if has_analysis and not has_checklist:
            suggestions.append({
                "text": "Generate the submission checklist from the analysis",
                "action": "chat",
                "step": "generate_checklist",
                "icon": "clipboard-list",
                "priority": 2,
            })

        if has_checklist and not has_documents:
            suggestions.append({
                "text": "Generate proposal documents for each checklist item",
                "action": "chat",
                "step": "generate_documents",
                "icon": "file-text",
                "priority": 3,
            })

        if (has_analysis or has_checklist) and not has_costing:
            suggestions.append({
                "text": "Estimate project costs with detailed breakdown",
                "action": "chat",
                "step": "research_costing",
                "icon": "dollar-sign",
                "priority": 4,
            })

        # Workspace suggestions
        has_workspace = False
        if tender_id:
            try:
                from app.models.workspace import WorkspaceConfig
                ws_config = db.query(WorkspaceConfig).filter(
                    WorkspaceConfig.tender_id == tender_id
                ).first()
                has_workspace = ws_config is not None
            except Exception:
                pass

        if has_checklist and not has_workspace:
            suggestions.append({
                "text": "Initialize canvas workspace to manage document generation",
                "action": "chat",
                "step": "workspace_setup",
                "icon": "layers",
                "priority": 3,
            })

        if has_workspace:
            suggestions.append({
                "text": "Check workspace progress and generate pending documents",
                "action": "chat",
                "step": None,
                "icon": "layout-grid",
                "priority": 5,
            })

        if has_analysis and has_checklist and has_documents and has_costing:
            suggestions.append({
                "text": "Submit for review",
                "action": "navigate",
                "step": None,
                "icon": "send",
                "priority": 6,
            })

    else:
        # Standalone mode suggestions
        suggestions.append({
            "text": "Link a tender to access its documents and data",
            "action": "navigate",
            "step": None,
            "icon": "link",
            "priority": 1,
        })

    # Contextual suggestions based on last output
    if last_output_type == "document_analysis" and not has_checklist:
        # Already suggested above for tender-linked, add for standalone
        if not has_tender:
            suggestions.append({
                "text": "Generate a checklist based on this analysis",
                "action": "chat",
                "step": None,
                "icon": "clipboard-list",
                "priority": 2,
            })

    if last_output_type == "cost_breakdown":
        suggestions.append({
            "text": "Format the cost breakdown as a Bill of Quantities",
            "action": "chat",
            "step": None,
            "icon": "table",
            "priority": 6,
        })

    if last_output_type == "proposal_document":
        suggestions.append({
            "text": "Review and refine this document",
            "action": "chat",
            "step": None,
            "icon": "edit",
            "priority": 6,
        })

    if last_output_type == "workspace_operations":
        suggestions.append({
            "text": "Open workspace to edit documents individually",
            "action": "navigate",
            "step": None,
            "icon": "external-link",
            "priority": 3,
        })

    # Always available general suggestions (lower priority)
    suggestions.append({
        "text": "Ask a question about tender requirements",
        "action": "chat",
        "step": None,
        "icon": "help-circle",
        "priority": 10,
    })

    # Sort by priority and limit
    suggestions.sort(key=lambda s: s["priority"])
    return suggestions[:5]
