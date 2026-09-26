"""Short, present-tense descriptions of what an agent is doing right now.

Both surfaces show live progress while a request runs, and both were rendering
whatever the tool happened to be called. Only 7 of 25 tools had a human label;
the rest fell through to a mechanical `name.replace("_", " ")`, so users watched
"Using anonymizing web search" and — worse, because delegation is the most
common step the Master Agent takes — "Using call costing researcher".

One vocabulary, used by the LangChain callback handler (every specialist agent)
and by the Master Agent's timeline streamer, so the two cannot describe the same
work differently.

Rules for these strings:
- Present continuous: the user is watching it happen, not reading a log.
- No tool names, no snake_case, no internal jargon.
- Say what it means for the user's work, not what the code is doing:
  "Looking up known rates for these items", not "Querying ratecard table".
"""

from __future__ import annotations

import re
from typing import Any, Optional

#: tool_key -> what the user sees while it runs.
TOOL_ACTIVITY: dict[str, str] = {
    # ── Reading and research ──
    "document_reader": "Reading the tender documents",
    "document_generator": "Writing the document",
    "tender_lookup": "Looking up the tender details",
    "checklist_reader": "Reading the submission checklist",
    "semantic_search": "Searching the tender for relevant sections",
    "web_search": "Searching the web",
    "anonymizing_web_search": "Researching market rates (tender details hidden)",
    "web_fetch": "Reading a page from the web",
    "costing_training_retrieval": "Checking past DRPL costings for comparable work",
    "ratecard_lookup": "Looking up known rates for these items",
    "delegate_travel_research": "Working out crew travel and lodging costs",
    "advisor": "Getting a second opinion",
    "tender_scoring_status": "Checking where this tender is in scoring",
    # ── Computation ──
    "cost_calculator": "Calculating quantities, rates and GST",
    "cost_breakdown_read": "Reading the saved cost breakdown",
    "code_execution": "Running the numbers",
    "structured_output": "Organising the results",
    # ── Workspace ──
    "workspace_init": "Setting up the workspace",
    "workspace_status": "Checking workspace progress",
    "workspace_list_items": "Listing the workspace documents",
    "workspace_generate_document": "Writing a workspace document",
    "xlsx_generator": "Building the Excel workbook",
    "docx_generator": "Building the Word document",
    # ── Memory and conversation ──
    "memory_store": "Noting that for later",
    "memory_retrieve": "Recalling what we established earlier",
    "clarify": "Preparing a question for you",
    "ask_user": "Waiting on your answer",
    "read_worker_output": "Reading the rest of the specialist's answer",
    "conversation_history_search": "Looking back through this conversation",
    # ── Master agent: diagnostics ──
    "inspect_workspace": "Inspecting the workspace",
    "inspect_tender": "Inspecting the tender",
    "inspect_session": "Reviewing this conversation's history",
    "list_recent_errors": "Checking what has been failing",
    "check_annexure_extraction": "Checking how the annexures came out",
    "diagnose_tender_outputs": "Checking this tender's output for problems",
    # ── Master agent: repair ──
    "init_workspace_force": "Rebuilding the workspace",
    "regenerate_checklist": "Rebuilding the checklist",
    "regenerate_annexures": "Re-extracting the annexures",
    "finalize_document": "Finalising the document",
    "retry_failed_agent_run": "Retrying the job that failed",
    # ── Master agent: platform ──
    "list_platform_settings": "Reading the platform settings",
    "update_platform_setting": "Changing a platform setting",
    "get_platform_health": "Checking platform health",
    "list_recent_agent_runs": "Reviewing recent agent runs",
    "list_platform_users": "Listing platform users",
    # ── Master agent: planning ──
    "propose_plan": "Putting together a plan",
    "run_with_llm": "Thinking it through",
}

#: Worker agent keys the Master delegates to -> how that hand-off reads.
#: Keyed without the `call_` prefix.
DELEGATION_ACTIVITY: dict[str, str] = {
    "deep_analyzer": "Analysing the tender documents in depth",
    "tender_doc_analyzer": "Analysing the tender documents in depth",
    "document_analyzer": "Analysing the documents",
    "checklist_generator": "Building the submission checklist",
    "checklist": "Building the submission checklist",
    "proposal_creator": "Drafting the proposal",
    "proposal": "Drafting the proposal",
    "costing_researcher": "Researching rates and building the costing",
    "doc-costing-analyst": "Building the BOQ and cost breakdown",
    "annexure_finder": "Extracting the annexures",
    "workspace_manager": "Organising the workspace",
    "costing_scope_extractor": "Summarising the scope for costing",
    "eligibility": "Checking eligibility",
    "relevance": "Scoring how relevant this tender is",
    "risk": "Assessing the risks",
    "classifier": "Categorising the tender",
    "summary": "Summarising the tender",
    "doc-letter-writer": "Writing the letter",
    "doc-technical-writer": "Writing the technical response",
    "doc-compliance-writer": "Writing the compliance documents",
    "tender_pipeline": "Running the full tender pipeline",
}

#: Non-tool phases, so the same vocabulary covers the whole run.
PHASE_ACTIVITY: dict[str, str] = {
    "llm_thinking": "Thinking",
    "routing": "Working out how to handle this",
    "planning": "Planning the steps",
    "synthesis": "Pulling the findings together",
    "done": "Finishing up",
}


def _titleise(key: str) -> str:
    """`doc-costing-analyst` -> `Doc Costing Analyst`."""
    return re.sub(r"[-_]+", " ", key).strip().title()


def describe_tool(name: str, args: Optional[dict[str, Any]] = None) -> str:
    """What to show the user while `name` runs.

    Never returns an empty string, and never leaks a raw tool name — an unknown
    tool reads as generic work rather than as internals.
    """
    if not name:
        return "Working on it"

    if name.startswith("call_"):
        agent_key = name[len("call_"):]
        known = DELEGATION_ACTIVITY.get(agent_key)
        if known:
            return known
        # An admin-registered agent we have no phrasing for. Its display name
        # is still more useful to a user than the raw key.
        return f"Asking the {_titleise(agent_key)} to help"

    known = TOOL_ACTIVITY.get(name)
    if known:
        # A tender id makes the step concrete when several are in play.
        tender_id = (args or {}).get("tender_id")
        return f"{known} (Tender #{tender_id})" if tender_id else known

    return "Working on it"


def describe_phase(phase: str) -> str:
    return PHASE_ACTIVITY.get(phase, "Working on it")
