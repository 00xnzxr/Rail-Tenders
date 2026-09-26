"""DRPL — the capability registry.

One source of truth for every capability an agent can hold: what it is, how it
is built, how risky it is, who may hold it, and — authored, not inferred — what
it is *for*. Four places used to decide this independently (the tool loader's
class registry, each canonical graph's hardcoded key list, the intersection
filter in ``resolve_tool_keys``, and five inline builders in
``run_decision_maker``), and their divergence produced three separate defects:
an analyzer running four tools while the registry declared six, Agent Builder
assignments silently dropped at runtime, and a Master Agent that could not
reach most of the platform it was built to orchestrate.

Two fields carry most of the weight:

- ``kind`` — catalogs used to hold two incompatible species: *classes* the
  loader instantiates with ``db``, and *factories* that close over
  ``user_id`` / ``session_id`` / ``stream_callback``. Naming both is what
  allows one resolver, and therefore one catalog.
- the manual fields (``purpose`` / ``use_when`` / ``not_for`` / ``produces``) —
  rendered into the Master Agent's prompt as the Platform Capability Manual.
  The Master does not infer what the costing researcher is; it reads what it
  is, when to use it, and when not to. A capability added without manual text
  fails a drift test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# Tier constants. These are string-identical to tool_policy's READ / WRITE /
# DESTRUCTIVE — defined locally because tool_policy derives its tier sets FROM
# this module, and importing it back here at module scope would be circular.
READ = "read"
WRITE = "write"
DESTRUCTIVE = "destructive"

#: The structured bifurcation. Every capability belongs to exactly one domain;
#: the Master's manual and the dispatcher's index are grouped by these.
DOMAINS: tuple[str, ...] = (
    "tender_intel",   # finding, reading and understanding tenders + documents
    "costing",        # rates, calculations, stored cost breakdowns
    "documents",      # producing and managing deliverable files
    "workspace",      # the canvas: checklists, annexures, generated documents
    "research",       # the outside world: web search, page fetch
    "platform_ops",   # health, settings, runs, users — the platform about itself
    "diagnostics",    # inspecting and repairing state that has gone wrong
    "delegation",     # handing work to a specialist agent
    "meta",           # memory, clarification, arbitrary LLM calls
)


@dataclass(frozen=True)
class Capability:
    key: str
    kind: Literal["class", "factory"]
    target: str                    # import path: "module:Attr"
    tier: str                      # READ | WRITE | DESTRUCTIVE
    min_role: str                  # operator | admin | master_admin
    surfaces: frozenset[str]       # subset of {"master", "generalist", "specialist"}
    domain: str
    purpose: str                   # one line: what it does
    use_when: str                  # when an agent should reach for it
    not_for: str                   # what it must not be used for
    produces: str                  # what comes back
    summary: str                   # plain language, for the confirmation card


_ALL = frozenset({"master", "generalist", "specialist"})
_MS = frozenset({"master", "specialist"})
_M = frozenset({"master"})

_TOOLS_PKG = "app.services.langchain.tools"
_GRAPHS_PKG = "app.services.langchain.graphs"


def _cap(
    key: str,
    kind: str,
    target: str,
    tier: str,
    domain: str,
    purpose: str,
    use_when: str,
    not_for: str,
    produces: str,
    summary: str,
    *,
    min_role: str = "operator",
    surfaces: frozenset[str] = _MS,
) -> Capability:
    return Capability(
        key=key, kind=kind, target=target, tier=tier, min_role=min_role,
        surfaces=surfaces, domain=domain, purpose=purpose, use_when=use_when,
        not_for=not_for, produces=produces, summary=summary,
    )


_CAPABILITY_LIST: list[Capability] = [
    # ── research ────────────────────────────────────────────────────────────
    _cap(
        "web_search", "class", f"{_TOOLS_PKG}.web_search_tool:WebSearchTool",
        READ, "research",
        purpose="Searches the live web, preferring Gemini search grounding with cited results.",
        use_when="Any claim about the outside world: market rates, suppliers, specifications, regulations, competitor activity.",
        not_for="Facts already stored on the platform — read the tender or the costing instead of searching for them.",
        produces="A grounded summary with the source URLs it actually came from.",
        summary="Search the web and report what was found, with sources.",
        surfaces=_ALL,
    ),
    _cap(
        "web_fetch", "class", f"{_TOOLS_PKG}.web_fetch_tool:WebFetchTool",
        READ, "research",
        purpose="Fetches and reads one specific web page by URL.",
        use_when="A search result or the user names a page and its full content matters.",
        not_for="Open-ended discovery — that is a search, not a fetch.",
        produces="The readable text content of the page.",
        summary="Open one web page and read its content.",
        surfaces=_ALL,
    ),
    _cap(
        "anonymizing_web_search", "class",
        f"{_TOOLS_PKG}.anonymizing_web_search_tool:AnonymizingWebSearchTool",
        READ, "research",
        purpose="Web search that strips tender-identifying details from the query before sending it.",
        use_when="Researching rates for a specific live tender, so the query cannot leak which tender is being priced.",
        not_for="General research with nothing sensitive in the query — plain web search is simpler.",
        produces="Search results with the identifying details removed from the query that fetched them.",
        summary="Search the web without revealing which tender is being researched.",
    ),
    # ── tender_intel ────────────────────────────────────────────────────────
    _cap(
        "tender_lookup", "class", f"{_TOOLS_PKG}.tender_lookup_tool:TenderLookupTool",
        READ, "tender_intel",
        purpose="Searches and reads the tender database: by id, keyword, portal or department, with AI scores.",
        use_when="Anything that starts with finding a tender — the latest arrivals, a specific id, everything from one portal.",
        not_for="Reading the PDFs themselves — that is the document reader's job.",
        produces="Tender records: title, dates, values, status, AI analysis scores.",
        summary="Look up tenders in the database.",
        surfaces=_ALL,
    ),
    _cap(
        "document_reader", "class", f"{_TOOLS_PKG}.document_reader_tool:DocumentReaderTool",
        READ, "tender_intel",
        purpose="Reads the stored documents attached to a tender, including scanned PDFs.",
        use_when="A question hinges on what a tender document actually says.",
        not_for="Finding which tender to read — look it up first.",
        produces="The extracted text of the tender's documents.",
        summary="Read the documents attached to a tender.",
        surfaces=_ALL,
    ),
    _cap(
        "semantic_search", "class", f"{_TOOLS_PKG}.semantic_search_tool:SemanticSearchTool",
        READ, "tender_intel",
        purpose="Finds passages across a tender's documents by meaning rather than exact words.",
        use_when="Locating a clause, requirement or figure somewhere in a large document set.",
        not_for="Reading a whole document top to bottom — use the document reader.",
        produces="The most relevant passages with where they came from.",
        summary="Find the relevant passage inside the tender documents.",
        surfaces=_ALL,
    ),
    _cap(
        "checklist_reader", "class", f"{_TOOLS_PKG}.checklist_tool:ChecklistReaderTool",
        READ, "workspace",
        purpose="Reads a tender's submission checklist and each item's status.",
        use_when="Answering what is required, what is done, and what is still missing for a submission.",
        not_for="Creating or rebuilding the checklist — that is the checklist generator's job.",
        produces="The checklist items with their completion status.",
        summary="Read the submission checklist for a tender.",
    ),
    _cap(
        "tender_scoring_status", "class", f"{_TOOLS_PKG}.scoring_status_tool:ScoringStatusTool",
        READ, "tender_intel",
        purpose="Reports how the automatic tender scoring pipeline is doing, overall or for one tender.",
        use_when="A question about whether or why tenders are being scored, or one tender's scoring state.",
        not_for="The content of a tender — scoring status is about the pipeline, not the tender.",
        produces="Scoring pipeline status, attempts, and recent outcomes.",
        summary="Check the automatic tender scoring status.",
    ),
    # ── costing ─────────────────────────────────────────────────────────────
    _cap(
        "cost_breakdown_read", "class",
        f"{_TOOLS_PKG}.cost_breakdown_tool:CostBreakdownReadTool",
        READ, "costing",
        purpose="Reads back a stored cost breakdown: every line, the rates, and the totals.",
        use_when="Explaining or answering questions about a costing the platform already produced — why a rate is what it is, what a line covers.",
        not_for="Producing a new costing — delegate that to the costing researcher.",
        produces="The saved breakdown as a table of lines plus overheads, margin, GST and totals.",
        summary="Read the saved cost breakdown for a tender.",
        surfaces=_ALL,
    ),
    _cap(
        "cost_calculator", "class", f"{_TOOLS_PKG}.cost_calculator_tool:CostCalculatorTool",
        READ, "costing",
        purpose="Does deterministic cost arithmetic: quantities, rates, overheads, margins, GST.",
        use_when="Any multi-step money calculation — never do these in your head.",
        not_for="Finding what a rate should be — that is research, not arithmetic.",
        produces="The computed figures with the working shown.",
        summary="Calculate costs, margins and taxes precisely.",
        surfaces=_ALL,
    ),
    _cap(
        "ratecard_lookup", "class", f"{_TOOLS_PKG}.ratecard_lookup_tool:RatecardLookupTool",
        READ, "costing",
        purpose="Looks up known unit rates from DRPL's own stored rate cards.",
        use_when="Pricing an item DRPL may have priced before — check here before searching the web.",
        not_for="Items DRPL has never handled — those need web research.",
        produces="Matching rate card entries with their unit rates.",
        summary="Look up DRPL's own stored rates.",
        surfaces=_ALL,
    ),
    _cap(
        "costing_training_retrieval", "class",
        f"{_TOOLS_PKG}.costing_training_retrieval_tool:CostingTrainingRetrievalTool",
        READ, "costing",
        purpose="Retrieves comparable priced lines from DRPL's historical costing training data.",
        use_when="Pricing an item similar to one in past costings — historical evidence beats a fresh guess.",
        not_for="Items with no plausible historical match.",
        produces="Comparable historical lines with their prices and context.",
        summary="Find how similar items were priced before.",
    ),
    _cap(
        "delegate_travel_research", "class",
        f"{_TOOLS_PKG}.costing_training_retrieval_tool:DelegateToTravelAgentTool",
        READ, "costing",
        purpose="Delegates crew travel and accommodation cost research to a dedicated helper.",
        use_when="A costing includes crew travel, lodging or transport components.",
        not_for="Non-travel cost lines.",
        produces="Researched travel cost estimates for the crew requirement.",
        summary="Research crew travel costs for a job.",
    ),
    # ── documents ───────────────────────────────────────────────────────────
    _cap(
        "document_generator", "class",
        f"{_TOOLS_PKG}.document_generator_tool:DocumentGeneratorTool",
        WRITE, "documents",
        purpose="Generates a formatted proposal document from content and a template.",
        use_when="The user wants a formal proposal artefact produced and saved.",
        not_for="Quick drafts that belong in chat — just write those in the reply.",
        produces="A saved, formatted document in the proposal workspace.",
        summary="Generate and save a formatted proposal document.",
    ),
    _cap(
        "docx_generator", "class", f"{_TOOLS_PKG}.docx_generator_tool:DocxGeneratorTool",
        WRITE, "documents",
        purpose="Produces a downloadable Word document from drafted content.",
        use_when="The user asks for a file — a letter, declaration or annexure they can download and edit.",
        not_for="Content the user only wants to read in chat.",
        produces="A .docx file saved to storage with a download link.",
        summary="Create a Word document the user can download.",
        surfaces=_ALL,
    ),
    _cap(
        "xlsx_generator", "class", f"{_TOOLS_PKG}.xlsx_generator_tool:XlsxGeneratorTool",
        WRITE, "documents",
        purpose="Produces a downloadable Excel workbook, typically a cost breakdown or schedule.",
        use_when="The user asks for a spreadsheet, or a costing needs to leave the platform as a file.",
        not_for="Small tables that read fine in chat.",
        produces="An .xlsx file saved to storage with a download link.",
        summary="Create an Excel workbook the user can download.",
        surfaces=_ALL,
    ),
    # ── workspace ───────────────────────────────────────────────────────────
    _cap(
        "workspace_init", "class", f"{_TOOLS_PKG}.workspace_tools:WorkspaceInitTool",
        WRITE, "workspace",
        purpose="Initializes the document workspace for a tender so documents can be generated into it.",
        use_when="A tender needs its workspace set up for the first time.",
        not_for="A tender whose workspace already exists — check its status first.",
        produces="A ready workspace with its planned document list.",
        summary="Set up the document workspace for a tender.",
    ),
    _cap(
        "workspace_status", "class", f"{_TOOLS_PKG}.workspace_tools:WorkspaceStatusTool",
        READ, "workspace",
        purpose="Reports a tender workspace's progress: which documents exist and their states.",
        use_when="Answering how far along a tender's document preparation is.",
        not_for="The content of the documents — read those individually.",
        produces="Per-document status across the workspace.",
        summary="Check how a tender workspace is progressing.",
    ),
    _cap(
        "workspace_list_items", "class", f"{_TOOLS_PKG}.workspace_tools:WorkspaceListItemsTool",
        READ, "workspace",
        purpose="Lists every item in a tender's workspace with ids and titles.",
        use_when="Finding which document to open, generate or discuss.",
        not_for="Progress summaries — the status tool is for that.",
        produces="The workspace's items with their identifiers.",
        summary="List everything in a tender workspace.",
    ),
    _cap(
        "workspace_generate_document", "class",
        f"{_TOOLS_PKG}.workspace_tools:WorkspaceGenerateDocumentTool",
        WRITE, "workspace",
        purpose="Generates one named document inside a tender's workspace.",
        use_when="The user asks for a specific named deliverable — a compliance certificate, a covering letter.",
        not_for="Rebuilding everything at once, or drafts that belong in chat.",
        produces="The generated document saved into the workspace.",
        summary="Generate one named document in the workspace.",
    ),
    # ── meta ────────────────────────────────────────────────────────────────
    _cap(
        "memory_store", "class", f"{_TOOLS_PKG}.memory_tool:MemoryStoreTool",
        READ, "meta",
        purpose="Stores a fact worth remembering across turns and sessions.",
        use_when="The user states a lasting preference or a hard-won fact surfaces mid-task.",
        not_for="Anything already stored in the platform's own tables.",
        produces="Confirmation that the fact was kept.",
        summary="Remember a fact for later conversations.",
        surfaces=_ALL,
    ),
    _cap(
        "memory_retrieve", "class", f"{_TOOLS_PKG}.memory_tool:MemoryRetrieveTool",
        READ, "meta",
        purpose="Recalls facts stored in earlier turns and sessions.",
        use_when="Context from earlier work would change the answer.",
        not_for="Platform data — query the real tables instead of memory.",
        produces="The stored facts relevant to the query.",
        summary="Recall facts remembered from earlier conversations.",
        surfaces=_ALL,
    ),
    _cap(
        "conversation_history_search", "class",
        f"{_TOOLS_PKG}.conversation_history_tool:ConversationHistorySearchTool",
        READ, "meta",
        purpose="Searches or pages back through earlier turns of the current conversation.",
        use_when="The user refers to something said earlier that is not in the turns you were given.",
        not_for="Facts about the platform or a tender — this reads what was said, not what is true.",
        produces="Matching turns with their excerpt, role, time and id to keep reading backwards.",
        summary="Look back through this conversation.",
        surfaces=_ALL,
    ),
    _cap(
        "clarify", "class", f"{_TOOLS_PKG}.clarify_tool:ClarifyTool",
        READ, "meta",
        purpose="Asks the user one clarifying question when a genuine ambiguity would change the answer.",
        use_when="Two readings of the request lead to materially different work.",
        not_for="Routine judgement calls an experienced colleague would just make.",
        produces="The user's answer to the question.",
        summary="Ask the user a clarifying question.",
        surfaces=_ALL,
    ),
    _cap(
        "code_execution", "class", f"{_TOOLS_PKG}.code_execution_tool:GeminiCodeExecutionTool",
        READ, "meta",
        purpose="Runs short computations in a sandbox for analysis too complex for the calculator.",
        use_when="A computation needs real code — statistics, parsing, transformation.",
        not_for="Simple arithmetic — the cost calculator is cheaper and auditable.",
        produces="The computation's output.",
        summary="Run a short computation in a sandbox.",
    ),
    _cap(
        "structured_output", "class", f"{_TOOLS_PKG}.structured_output_tool:StructuredOutputTool",
        READ, "meta",
        purpose="Extracts data from text into a caller-supplied JSON structure.",
        use_when="Downstream code needs a precise machine-readable shape.",
        not_for="Answers meant for a human to read.",
        produces="JSON matching the requested structure.",
        summary="Extract structured data from text.",
    ),
    _cap(
        "advisor", "class", f"{_TOOLS_PKG}.advisor_tool:AdvisorTool",
        READ, "meta",
        purpose="Consults a second model for an independent opinion on a judgement call.",
        use_when="A high-stakes judgement benefits from a second view before committing.",
        not_for="Facts — look those up rather than polling models.",
        produces="A second opinion with its reasoning.",
        summary="Get a second opinion on a judgement call.",
    ),
    _cap(
        "run_with_llm", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_llm_meta_tools",
        READ, "meta",
        purpose="Runs one prompt on a chosen model and provider, for subtasks that suit a different model.",
        use_when="A subtask clearly favours another model — bulk summarisation on a cheap one, a hard passage on a stronger one.",
        not_for="Ordinary reasoning the current model handles fine.",
        produces="That model's completion for the prompt.",
        summary="Run one prompt on a specific model.",
        surfaces=_M,
    ),
    # ── diagnostics (read) ──────────────────────────────────────────────────
    _cap(
        "inspect_workspace", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_diagnostic_tools",
        READ, "diagnostics",
        purpose="Inspects a tender workspace's internal state when something about it looks wrong.",
        use_when="A workspace misbehaves — documents missing, generation stuck — and the cause is not visible from the normal status.",
        not_for="Routine progress checks — the workspace status tool covers those.",
        produces="The workspace's internal state in diagnostic detail.",
        summary="Inspect a workspace that is misbehaving.",
        surfaces=_M,
    ),
    _cap(
        "inspect_tender", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_diagnostic_tools",
        READ, "diagnostics",
        purpose="Inspects one tender's full platform state: documents, analysis, artefacts, flags.",
        use_when="Diagnosing why a tender is not behaving as expected anywhere in the pipeline.",
        not_for="Reading tender content for its own sake — use the lookup and reader.",
        produces="A diagnostic view of everything the platform holds for the tender.",
        summary="Inspect everything the platform holds for one tender.",
        surfaces=_M,
    ),
    _cap(
        "inspect_session", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_diagnostic_tools",
        READ, "diagnostics",
        purpose="Inspects a Command Center session: its messages, artifacts and pipeline state.",
        use_when="Diagnosing what happened in a session — a lost result, a stuck pipeline step.",
        not_for="Ordinary conversation history, which is already in context.",
        produces="The session's stored state in diagnostic detail.",
        summary="Inspect a chat session's stored state.",
        surfaces=_M,
    ),
    _cap(
        "list_recent_errors", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_diagnostic_tools",
        READ, "diagnostics",
        purpose="Lists recent agent errors, optionally scoped to one tender or session.",
        use_when="Something failed and the first question is what broke, where, and how often.",
        not_for="Successes — this reads the error log only.",
        produces="Recent errors with their agents, times and messages.",
        summary="List the platform's recent errors.",
        surfaces=_M,
    ),
    _cap(
        "check_annexure_extraction", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_diagnostic_tools",
        READ, "diagnostics",
        purpose="Checks whether a tender's annexure extraction completed and what it produced.",
        use_when="Annexures look missing, empty or wrong for a tender.",
        not_for="Running the extraction — that is the annexure finder's job.",
        produces="The extraction's status and its outputs.",
        summary="Check how annexure extraction went for a tender.",
        surfaces=_M,
    ),
    _cap(
        "diagnose_tender_outputs", "factory", f"{_GRAPHS_PKG}.quality_tools:build_quality_tools",
        READ, "diagnostics",
        purpose="Checks generated outputs against known failure modes: empty synthesis, placeholder annexures, blank documents, collapsed costing schedules.",
        use_when="After production work completes, or when the user doubts an output's quality.",
        not_for="Judging writing style — it detects structural failure, deterministically.",
        produces="A pass or a named failure mode per output, from deterministic checks.",
        summary="Check generated outputs for known failure modes.",
        surfaces=_M,
    ),
    # ── diagnostics (write / destructive) ───────────────────────────────────
    _cap(
        "regenerate_checklist", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_action_tools",
        WRITE, "diagnostics",
        purpose="Rebuilds a tender's submission checklist from its analysis.",
        use_when="The checklist is missing, stale or visibly wrong.",
        not_for="Small manual edits the user could make in the UI.",
        produces="A fresh checklist replacing the current one.",
        summary="Rebuild the submission checklist. The current checklist will be replaced.",
        surfaces=_M,
    ),
    _cap(
        "regenerate_annexures", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_action_tools",
        WRITE, "diagnostics",
        purpose="Re-extracts every annexure from the tender documents.",
        use_when="Annexure extraction failed or produced placeholders instead of real content.",
        not_for="Fixing one annexure — regeneration replaces them all.",
        produces="Fresh annexure documents replacing the existing ones.",
        summary="Re-extract every annexure. Existing annexure documents will be replaced.",
        surfaces=_M,
    ),
    _cap(
        "retry_failed_agent_run", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_action_tools",
        WRITE, "diagnostics",
        purpose="Runs a failed background job again.",
        use_when="A run failed for a transient reason — a timeout, a provider error.",
        not_for="Runs that failed for a cause still present; fix the cause first.",
        produces="A new attempt at the failed job.",
        summary="Run the failed job again.",
        surfaces=_M,
    ),
    _cap(
        "init_workspace_force", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_action_tools",
        DESTRUCTIVE, "diagnostics",
        purpose="Resets a workspace and rebuilds it from scratch, discarding its current contents.",
        use_when="A workspace is corrupted beyond repair and the user accepts losing what is in it.",
        not_for="Anything a normal regeneration could fix — this destroys hand-edited work.",
        produces="A brand-new workspace; everything previously in it is gone.",
        summary="Reset this workspace and rebuild it from scratch. Everything currently in it will be lost.",
        surfaces=_M,
    ),
    _cap(
        "finalize_document", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_action_tools",
        DESTRUCTIVE, "documents",
        purpose="Marks a document as final, superseding the current finalized version.",
        use_when="The user explicitly signs off a document as the submission copy.",
        not_for="Drafts still in review — finalizing replaces a version someone may have hand-edited.",
        produces="The document locked as the finalized version.",
        summary="Mark this document as final. It will replace the current finalized version.",
        surfaces=_M,
    ),
    # ── platform_ops ────────────────────────────────────────────────────────
    _cap(
        "list_platform_settings", "factory", f"{_GRAPHS_PKG}.platform_tools:build_platform_tools",
        READ, "platform_ops",
        purpose="Lists platform configuration settings, with secrets redacted.",
        use_when="A behaviour question comes down to how the platform is configured.",
        not_for="Changing settings — that is a separate, gated capability.",
        produces="The settings and their current values, secrets hidden.",
        summary="List the platform's configuration settings.",
        min_role="admin", surfaces=_M,
    ),
    _cap(
        "get_platform_health", "factory", f"{_GRAPHS_PKG}.platform_tools:build_platform_tools",
        READ, "platform_ops",
        purpose="Reports platform health: queue depth, active runs, database pool, recent errors.",
        use_when="The platform feels slow, stuck or broken and the first question is its vital signs.",
        not_for="Diagnosing one tender or session — use the inspectors.",
        produces="A health snapshot across queues, workers and the database.",
        summary="Check the platform's health and capacity.",
        min_role="admin", surfaces=_M,
    ),
    _cap(
        "list_recent_agent_runs", "factory", f"{_GRAPHS_PKG}.platform_tools:build_platform_tools",
        READ, "platform_ops",
        purpose="Lists recent agent runs across the platform with their status and timing.",
        use_when="Answering what the platform has been doing lately, or finding a specific recent run.",
        not_for="One run's deep detail — inspect the session it belongs to.",
        produces="Recent runs with agent, status, timing and outcome.",
        summary="List the platform's recent agent runs.",
        min_role="admin", surfaces=_M,
    ),
    _cap(
        "list_platform_users", "factory", f"{_GRAPHS_PKG}.platform_tools:build_platform_tools",
        READ, "platform_ops",
        purpose="Lists the platform's user accounts and their roles.",
        use_when="An access or assignment question needs the actual account list.",
        not_for="Anything outside user administration.",
        produces="The user accounts with their roles and status.",
        summary="List the platform's user accounts.",
        min_role="master_admin", surfaces=_M,
    ),
    _cap(
        "update_platform_setting", "factory", f"{_GRAPHS_PKG}.platform_tools:build_platform_tools",
        WRITE, "platform_ops",
        purpose="Changes one platform configuration setting — the broadest-blast-radius write there is.",
        use_when="A master administrator explicitly asks for a configuration change.",
        not_for="Secrets, which are rejected on write, or anything the user did not explicitly request.",
        produces="The setting updated to the new value.",
        summary="Change a platform configuration setting.",
        min_role="master_admin", surfaces=_M,
    ),
    # ── delegation / conversation control ───────────────────────────────────
    _cap(
        "propose_plan", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_plan_proposal_tool",
        READ, "delegation",
        purpose="Proposes a multi-step plan for the user to approve before execution.",
        use_when="The request genuinely spans several agents or steps and the user should see the shape first.",
        not_for="Single-step work — just do it.",
        produces="A structured plan card the user can approve, change or deny.",
        summary="Propose a plan for approval before running it.",
        surfaces=_M,
    ),
    _cap(
        "ask_user", "factory", f"{_GRAPHS_PKG}.orchestrator_tools:build_ask_user_tool",
        READ, "delegation",
        purpose="Pauses autonomous work to ask the user one blocking question.",
        use_when="Genuinely blocked — proceeding under any assumption would waste the run.",
        not_for="Questions whose answer would not change what happens next.",
        produces="The run paused with the question surfaced to the user.",
        summary="Pause and ask the user a blocking question.",
        surfaces=_M,
    ),
    _cap(
        "read_worker_output", "factory",
        f"{_GRAPHS_PKG}.orchestrator_tools:build_worker_output_reader",
        READ, "delegation",
        purpose="Reads the rest of a worker agent's output when it was too long to return whole.",
        use_when="A delegation came back with output_complete false and the answer you owe the user depends on what follows.",
        not_for="Re-running the worker — the work is already done and stored; this only reads it.",
        produces="The next span of that worker's output, with the offset to continue from.",
        summary="Read the rest of a specialist's answer.",
        surfaces=_M,
    ),
]


CAPABILITIES: dict[str, Capability] = {c.key: c for c in _CAPABILITY_LIST}

if len(CAPABILITIES) != len(_CAPABILITY_LIST):  # pragma: no cover - drift guard
    _seen: set[str] = set()
    _dupes = [c.key for c in _CAPABILITY_LIST if c.key in _seen or _seen.add(c.key)]
    raise RuntimeError(f"duplicate capability keys: {_dupes}")


def get(key: str) -> Optional[Capability]:
    """The capability registered under ``key``, or None."""
    return CAPABILITIES.get(key)


def keys_for_surface(surface: str, user_role: Optional[str]) -> list[str]:
    """Every capability key this surface may hold at this role.

    Role filtering here is a convenience so an agent does not plan around
    capabilities it will never be granted — the call-time check inside each
    tool remains the actual control.
    """
    from app.services.langchain.graphs.platform_tools import role_allows

    def _allowed(cap: Capability) -> bool:
        # "operator" means unrestricted: before the registry, only the
        # platform_ops builders role-checked at all, and a run whose user row
        # cannot be resolved (user_role None) still got every ordinary tool.
        # Losing the whole catalog to a missing role row would be a parity
        # regression, not a security improvement — the call-time check inside
        # each role-bounded tool remains the control either way.
        if cap.min_role == "operator":
            return True
        return role_allows(user_role, cap.min_role)

    return [c.key for c in _CAPABILITY_LIST if surface in c.surfaces and _allowed(c)]


#: Section headers for the manual, in a deliberate order: what the Master will
#: reach for most often comes first.
_DOMAIN_TITLES: dict[str, str] = {
    "delegation": "Delegation — handing work to a specialist",
    "tender_intel": "Tender intelligence — finding and reading tenders",
    "costing": "Costing — rates, calculations, stored breakdowns",
    "research": "Research — the world outside the platform",
    "documents": "Documents — producing deliverable files",
    "workspace": "Workspace — checklists, annexures, generated documents",
    "diagnostics": "Diagnostics — inspecting and repairing platform state",
    "platform_ops": "Platform operations — the platform about itself",
    "meta": "Meta — memory, clarification, model calls",
}

#: The delegation doctrine. Rendered into the Master's prompt directly after
#: the manual. This is doctrine, not routing: there is no pre-dispatch guard in
#: front of the Master, so this text is what stands between a user's costing
#: and a freeform pass that silently summarises it.
DELEGATION_DOCTRINE = """\
## Delegation doctrine

**Produce vs. explain.** Producing a NEW artefact — a cost breakdown, a
forensic document analysis, annexure extraction, a submission checklist, a
proposal — is always a specialist's job: use the `call_<agent>` tool. Explaining,
adjusting, or answering questions about an artefact that already EXISTS is your
own job: read it with the reading tools and answer directly.

**Costing is never done by you. No exceptions.** When the user wants a costing
produced — a bidding schedule, a BOQ, a NIT with line items, "price every
item", "full costing", "estimate for this tender" — call
`call_costing_researcher` and let it run. The canonical costing path parses the
tender's schedule and prices every row in batches, so no row can be dropped. A
freeform pass done in your own head collapses a dense 138-row spares schedule
into a single summary line, and nothing raises an error when it does — the
failure is silent and lands in the number the user actually bids with. If you
are ever tempted to "just estimate it quickly", that temptation is the failure
mode this paragraph exists to stop.

**A costing request begins with the costing call.** When the user asks for a
costing, `call_costing_researcher` is your FIRST tool call -- not
`tender_lookup`, not `document_reader`, not `semantic_search`,
`memory_retrieve` or `call_deep_analyzer`. The costing worker runs the tender
analysis it needs itself, reads the schedule and every annexure itself, and
prices every row; nothing you read beforehand reaches it, so reading first
only spends your step budget. A real run did exactly that -- twenty-five
tool calls of reading, the budget gone, and no costing -- and the user had
to type Continue. Attached documents are already uploaded to the tender the
worker is called with; pass the tender_id and the user's request, and let it
run. Ask for the analysis separately only if the user asked for it.

**Answer the question asked.** A status or informational question — "check the
latest run", "what happened with tender X", "how is the platform doing" — is
answered by READING: one or two lookups, then the report. Never launch a
specialist job (analysis, costing, extraction, checklist, proposal) to answer a
question the user did not ask you to produce. Those jobs run for minutes, cost
real money, and change platform state; firing one uninvited turns a ten-second
question into a stalled conversation. When your reading shows production work
is genuinely needed, SAY so in your answer and let the user ask for it — or
propose it as a plan. Unrequested initiative here is a failure mode, not
diligence. Stop reading as soon as you can answer: two tools that answer the
question beat eight that decorate it.

**Questions about an existing costing are yours.** "Why is line 3 priced like
that", "where did this rate come from", "what does the margin cover" — read the
saved breakdown with `cost_breakdown_read` and answer. Do not re-run the
costing to answer a question about it.

**After production work completes**, run `diagnose_tender_outputs` on the
result when quality is in doubt — it detects the known failure modes
(collapsed schedules, placeholder annexures, blank documents) deterministically
and tells you which delegation to redo.
"""


def render_capability_manual(
    surface: str,
    user_role: Optional[str],
    *,
    include_workers: Optional[list[dict]] = None,
) -> str:
    """The Platform Capability Manual for one surface at one role.

    Rendered into the agent's system prompt. Generated from the registry so it
    cannot drift from what exists: a capability added without manual text fails
    a drift test before it can ship half-described.
    """
    allowed = set(keys_for_surface(surface, user_role))
    by_domain: dict[str, list[Capability]] = {}
    for cap in _CAPABILITY_LIST:
        if cap.key in allowed:
            by_domain.setdefault(cap.domain, []).append(cap)

    out: list[str] = [
        "# Platform Capability Manual",
        "",
        "Every function of this platform you can reach, what each is for, and "
        "when not to use it. Choose from this manual — never guess at a "
        "capability it does not list.",
    ]

    for domain in _DOMAIN_TITLES:
        caps = by_domain.get(domain)
        workers_here = include_workers if (domain == "delegation" and include_workers) else None
        if not caps and not workers_here:
            continue
        out += ["", f"## {_DOMAIN_TITLES[domain]} `{domain}`", ""]
        if workers_here:
            for worker in workers_here:
                out.append(
                    f"- **call_{worker['agent_key']}** — {worker.get('description') or worker.get('display_name')}"
                )
        for cap in caps or []:
            out += [
                f"- **{cap.key}** — {cap.purpose}",
                f"  - Use when: {cap.use_when}",
                f"  - Not for: {cap.not_for}",
                f"  - Produces: {cap.produces}",
            ]

    return "\n".join(out)
