"""
DRPL LangChain Tools - Canvas Workspace Operations
Provides tools for initializing workspaces, checking status,
listing items, and generating documents from the Command Center.
"""

import json
import logging
from typing import Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# --- Input Schemas ---

class WorkspaceInitInput(BaseModel):
    """Input schema for workspace initialization."""
    tender_id: int = Field(..., description="The tender ID to initialize a workspace for")


class WorkspaceStatusInput(BaseModel):
    """Input schema for workspace status check."""
    tender_id: int = Field(..., description="The tender ID to check workspace status for")


class WorkspaceListItemsInput(BaseModel):
    """Input schema for listing workspace items."""
    tender_id: int = Field(..., description="The tender ID to list workspace items for")
    status_filter: Optional[str] = Field(
        None,
        description="Filter by review status: not_started, drafting, in_review, approved, rejected",
    )


class WorkspaceGenerateDocumentInput(BaseModel):
    """Input schema for generating a workspace document."""
    tender_id: int = Field(..., description="The tender ID")
    item_name_or_id: str = Field(
        ...,
        description="The document name (partial match supported) or checklist item ID to generate",
    )


# --- Tool Classes ---

class WorkspaceInitTool(BaseTool):
    """Initialize a canvas workspace for a tender, creating a document workspace entry
    for each checklist item with auto-matched format templates."""
    name: str = "workspace_init"
    description: str = (
        "Initialize the canvas workspace for a tender. Creates a workspace entry "
        "for each checklist item, auto-assigns format templates and AI agents. "
        "Returns the workspace overview with item count and progress stats. "
        "Idempotent — returns the existing workspace if already initialized."
    )
    args_schema: Type[BaseModel] = WorkspaceInitInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, tender_id: int) -> str:
        if not self.db:
            return "Error: Database session not available"

        try:
            from app.services.workspace_service import init_workspace

            overview = init_workspace(self.db, tender_id, user_id=None)

            config = overview.get("config", {})
            stats = overview.get("stats", {})
            items = overview.get("items", [])

            return json.dumps({
                "status": "success",
                "message": f"Workspace initialized for tender {tender_id}",
                "total_items": len(items),
                "completion_percent": stats.get("completion_percent", 0),
                "by_category": {
                    cat: {"total": v.get("total", 0), "completed": v.get("completed", 0)}
                    for cat, v in stats.get("by_category", {}).items()
                },
                "view_mode": config.get("view_mode", "grid"),
            }, default=str)

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"WorkspaceInitTool failed: {e}")
            return f"Error initializing workspace: {str(e)}"


class WorkspaceStatusTool(BaseTool):
    """Check the current status and progress of a tender's canvas workspace."""
    name: str = "workspace_status"
    description: str = (
        "Get the current status and progress of a tender's canvas workspace. "
        "Returns completion stats, items by status, and category breakdown. "
        "If no workspace exists, returns a message to initialize first."
    )
    args_schema: Type[BaseModel] = WorkspaceStatusInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, tender_id: int) -> str:
        if not self.db:
            return "Error: Database session not available"

        try:
            from app.services.workspace_service import get_workspace_overview

            overview = get_workspace_overview(self.db, tender_id)

            stats = overview.get("stats", {})
            items = overview.get("items", [])

            # Summary by status
            status_summary = {}
            for item in items:
                status = item.get("review_status", "not_started")
                if status not in status_summary:
                    status_summary[status] = []
                status_summary[status].append(item.get("item_name", "Unknown"))

            return json.dumps({
                "workspace_exists": True,
                "total_items": stats.get("total_items", 0),
                "completed_items": stats.get("completed_items", 0),
                "completion_percent": stats.get("completion_percent", 0),
                "by_status": {
                    status: {"count": len(names), "items": names[:5]}
                    for status, names in status_summary.items()
                },
                "by_category": stats.get("by_category", {}),
            }, default=str)

        except ValueError:
            return json.dumps({
                "workspace_exists": False,
                "message": "No workspace found for this tender. Use workspace_init to create one first.",
            })
        except Exception as e:
            logger.error(f"WorkspaceStatusTool failed: {e}")
            return f"Error checking workspace status: {str(e)}"


class WorkspaceListItemsTool(BaseTool):
    """List all document items in a tender's workspace with their current status."""
    name: str = "workspace_list_items"
    description: str = (
        "List all document items in a tender's workspace. Shows each item's name, "
        "category, review status, content version, and assigned agent. "
        "Optionally filter by review status (not_started, drafting, in_review, approved, rejected)."
    )
    args_schema: Type[BaseModel] = WorkspaceListItemsInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, tender_id: int, status_filter: Optional[str] = None) -> str:
        if not self.db:
            return "Error: Database session not available"

        try:
            from app.services.workspace_service import get_workspace_overview

            overview = get_workspace_overview(self.db, tender_id)
            items = overview.get("items", [])

            # Apply status filter if provided
            if status_filter:
                items = [i for i in items if i.get("review_status") == status_filter]

            result = []
            for item in items:
                result.append({
                    "id": item.get("id"),
                    "name": item.get("item_name"),
                    "category": item.get("item_category"),
                    "review_status": item.get("review_status", "not_started"),
                    "content_version": item.get("content_version", 0),
                    "agent_key": item.get("agent_key"),
                    "is_not_required": item.get("is_not_required", False),
                    "has_content": bool(item.get("draft_content_html")),
                })

            return json.dumps({
                "total": len(result),
                "filter": status_filter,
                "items": result,
            }, default=str)

        except ValueError:
            return "Error: No workspace found for this tender. Initialize it first with workspace_init."
        except Exception as e:
            logger.error(f"WorkspaceListItemsTool failed: {e}")
            return f"Error listing workspace items: {str(e)}"


class WorkspaceGenerateDocumentTool(BaseTool):
    """Generate content for a specific document in the workspace using its assigned AI agent."""
    name: str = "workspace_generate_document"
    description: str = (
        "Generate content for a specific document in the workspace. "
        "Provide the document name (partial match) or checklist item ID. "
        "Uses the document's assigned AI agent with format template context to produce content. "
        "The generated content is saved as a draft in the workspace."
    )
    args_schema: Type[BaseModel] = WorkspaceGenerateDocumentInput
    db: Optional[Session] = None

    class Config:
        arbitrary_types_allowed = True

    def _run(self, tender_id: int, item_name_or_id: str) -> str:
        if not self.db:
            return "Error: Database session not available"

        try:
            from app.models.checklist import ChecklistItem
            from app.models.workspace import DocumentWorkspace

            # Try to find item by ID first
            item = None
            try:
                item_id = int(item_name_or_id)
                item = self.db.query(ChecklistItem).filter(
                    ChecklistItem.id == item_id,
                    ChecklistItem.tender_id == tender_id,
                ).first()
            except (ValueError, TypeError):
                pass

            # Search by name (case-insensitive partial match)
            if not item:
                search_term = item_name_or_id.lower()
                candidates = self.db.query(ChecklistItem).filter(
                    ChecklistItem.tender_id == tender_id
                ).all()

                # Exact match first
                for c in candidates:
                    if c.item_name.lower() == search_term:
                        item = c
                        break

                # Partial match
                if not item:
                    for c in candidates:
                        if search_term in c.item_name.lower():
                            item = c
                            break

                # Fuzzy: check if any word from search matches
                if not item:
                    search_words = set(search_term.split())
                    best_match = None
                    best_score = 0
                    for c in candidates:
                        name_words = set(c.item_name.lower().split())
                        overlap = len(search_words & name_words)
                        if overlap > best_score:
                            best_score = overlap
                            best_match = c
                    if best_match and best_score > 0:
                        item = best_match

            if not item:
                # Return available items to help the user
                available = self.db.query(ChecklistItem).filter(
                    ChecklistItem.tender_id == tender_id
                ).all()
                names = [f"- {c.item_name} (ID: {c.id})" for c in available[:15]]
                return (
                    f"Could not find a document matching '{item_name_or_id}'. "
                    f"Available items:\n" + "\n".join(names)
                )

            # Check if workspace entry exists
            ws = self.db.query(DocumentWorkspace).filter(
                DocumentWorkspace.checklist_item_id == item.id
            ).first()
            if not ws:
                return (
                    f"Found '{item.item_name}' but it has no workspace entry. "
                    "Please initialize the workspace first using workspace_init."
                )

            # Generate using the workspace agent service
            import asyncio
            from app.services.workspace_agent_service import generate_document_with_agent

            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an async context — run sync in thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        self._sync_generate, self.db, item.id, tender_id
                    )
                    result = future.result(timeout=120)
            else:
                result = self._sync_generate(self.db, item.id, tender_id)

            return json.dumps({
                "status": "success",
                "item_name": item.item_name,
                "item_id": item.id,
                "content_version": result.get("content_version", 0),
                "review_status": result.get("review_status", "drafting"),
                "content_preview": (result.get("content_html", "") or "")[:300],
                "message": f"Successfully generated content for '{item.item_name}'",
            }, default=str)

        except Exception as e:
            logger.error(f"WorkspaceGenerateDocumentTool failed: {e}")
            return f"Error generating document: {str(e)}"

    @staticmethod
    def _sync_generate(db: Session, item_id: int, tender_id: int) -> dict:
        """Synchronous wrapper for document generation."""
        from app.services.workspace_agent_service import generate_document_with_agent
        import asyncio

        # Create a new event loop for the thread
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                generate_document_with_agent(db, item_id, user_id=None)
            )
        finally:
            loop.close()
