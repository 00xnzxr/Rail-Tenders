"""
DRPL - Human-in-the-Loop Tool Approval
Provides tool approval gates for OpenAI Agents SDK executions.
When a tool requires approval, execution pauses and waits for
human review before proceeding.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_builder import AgentExecution

logger = logging.getLogger(__name__)

# In-memory store for pending approvals (keyed by approval_id)
# In production, this should be backed by Redis or a DB table.
_pending_approvals: dict[str, dict] = {}


class ToolApprovalRequired(Exception):
    """Raised when a tool call needs human approval before proceeding."""

    def __init__(self, approval_id: str, tool_name: str, arguments: str, reason: str = ""):
        self.approval_id = approval_id
        self.tool_name = tool_name
        self.arguments = arguments
        self.reason = reason
        super().__init__(f"Tool '{tool_name}' requires approval (id={approval_id})")


def create_approval_request(
    execution_id: int,
    tool_name: str,
    arguments: str,
    agent_key: str,
    user_id: Optional[int] = None,
    reason: str = "",
) -> dict:
    """
    Create a pending approval request for a tool call.

    Returns the approval record dict.
    """
    import uuid
    approval_id = str(uuid.uuid4())

    approval = {
        "approval_id": approval_id,
        "execution_id": execution_id,
        "tool_name": tool_name,
        "arguments": arguments,
        "agent_key": agent_key,
        "user_id": user_id,
        "reason": reason,
        "status": "pending",  # pending, approved, rejected
        "created_at": datetime.now(timezone.utc).isoformat(),
        "resolved_at": None,
        "resolved_by": None,
        # Event used to signal approval/rejection to waiting coroutine
        "_event": asyncio.Event(),
    }
    _pending_approvals[approval_id] = approval
    logger.info(f"Created approval request {approval_id} for tool '{tool_name}'")
    return approval


def resolve_approval(
    approval_id: str,
    approved: bool,
    resolved_by: Optional[int] = None,
) -> Optional[dict]:
    """
    Approve or reject a pending tool call.

    Args:
        approval_id: The approval request ID
        approved: True to approve, False to reject
        resolved_by: User ID of the reviewer

    Returns:
        The updated approval record, or None if not found
    """
    approval = _pending_approvals.get(approval_id)
    if not approval:
        return None

    approval["status"] = "approved" if approved else "rejected"
    approval["resolved_at"] = datetime.now(timezone.utc).isoformat()
    approval["resolved_by"] = resolved_by

    # Signal the waiting coroutine
    event = approval.get("_event")
    if event:
        event.set()

    logger.info(
        f"Approval {approval_id} {'approved' if approved else 'rejected'} "
        f"by user {resolved_by}"
    )
    return _serialize_approval(approval)


async def wait_for_approval(approval_id: str, timeout: float = 300.0) -> bool:
    """
    Wait for a pending approval to be resolved.

    Args:
        approval_id: The approval request ID
        timeout: Max seconds to wait (default 5 minutes)

    Returns:
        True if approved, False if rejected or timed out
    """
    approval = _pending_approvals.get(approval_id)
    if not approval:
        return False

    event = approval.get("_event")
    if not event:
        return False

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        approval["status"] = "timeout"
        logger.warning(f"Approval {approval_id} timed out after {timeout}s")
        return False

    return approval["status"] == "approved"


def get_pending_approvals(
    execution_id: Optional[int] = None,
    agent_key: Optional[str] = None,
) -> list[dict]:
    """Get all pending approval requests, optionally filtered."""
    results = []
    for approval in _pending_approvals.values():
        if approval["status"] != "pending":
            continue
        if execution_id and approval["execution_id"] != execution_id:
            continue
        if agent_key and approval["agent_key"] != agent_key:
            continue
        results.append(_serialize_approval(approval))
    return results


def get_approval(approval_id: str) -> Optional[dict]:
    """Get a specific approval request by ID."""
    approval = _pending_approvals.get(approval_id)
    if not approval:
        return None
    return _serialize_approval(approval)


def cleanup_old_approvals(max_age_seconds: int = 3600):
    """Remove resolved/timed-out approvals older than max_age."""
    cutoff = time.time() - max_age_seconds
    to_remove = []
    for aid, approval in _pending_approvals.items():
        if approval["status"] != "pending":
            created = approval.get("created_at", "")
            try:
                created_dt = datetime.fromisoformat(created)
                if created_dt.timestamp() < cutoff:
                    to_remove.append(aid)
            except (ValueError, TypeError):
                to_remove.append(aid)
    for aid in to_remove:
        del _pending_approvals[aid]


def _serialize_approval(approval: dict) -> dict:
    """Serialize an approval record for API responses (strip internal fields)."""
    return {k: v for k, v in approval.items() if not k.startswith("_")}


def build_approval_tool_wrapper(
    tool,
    execution_id: int,
    agent_key: str,
    user_id: Optional[int] = None,
    requires_approval_tools: Optional[set[str]] = None,
):
    """
    Wrap a FunctionTool's on_invoke_tool callback to require approval.

    If the tool's name is in requires_approval_tools, the wrapper will
    create an approval request and wait for it before executing.

    Args:
        tool: OpenAI Agents SDK FunctionTool
        execution_id: Current execution ID
        agent_key: Agent key for the approval record
        user_id: User who triggered the execution
        requires_approval_tools: Set of tool names that need approval

    Returns:
        The tool (modified in-place with wrapped callback)
    """
    if not requires_approval_tools:
        return tool

    if tool.name not in requires_approval_tools:
        return tool

    original_callback = tool.on_invoke_tool

    async def _approval_wrapper(ctx, args_json: str) -> str:
        # Create approval request
        approval = create_approval_request(
            execution_id=execution_id,
            tool_name=tool.name,
            arguments=args_json,
            agent_key=agent_key,
            user_id=user_id,
            reason=f"Tool '{tool.name}' requires human approval before execution",
        )

        # Wait for approval
        approved = await wait_for_approval(approval["approval_id"])

        if not approved:
            status = _pending_approvals.get(approval["approval_id"], {}).get("status", "rejected")
            return json.dumps({
                "error": f"Tool execution {'timed out' if status == 'timeout' else 'rejected'} by human reviewer",
                "tool": tool.name,
                "approval_id": approval["approval_id"],
            })

        # Proceed with original execution
        return await original_callback(ctx, args_json)

    tool.on_invoke_tool = _approval_wrapper
    return tool
