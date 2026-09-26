"""
DRPL LangChain Agent Router Graph
Classifies user intent and routes messages to specialized agents
(document analysis, checklist generation, proposal writing, costing).

Uses a 3-node LangGraph StateGraph:
  classify_intent → execute_agents → compose_response
"""

import json
import logging
import time
import uuid
from typing import Optional, TypedDict, Annotated
from operator import add

from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, END
from sqlalchemy.orm import Session

from app.services.langchain.llm_factory import get_chat_model
from app.services.langchain.memory_service import (
    save_conversation_turn,
    get_conversation_history,
)

logger = logging.getLogger(__name__)


# --- Router State ---

class RouterState(TypedDict):
    """Shared state for the agent router graph."""
    session_id: str
    user_message: str
    tender_id: Optional[int]
    intent: str
    selected_agents: list[str]
    agent_results: dict           # agent_key → { output, output_type, tool_calls, metrics }
    final_response: str
    output_type: str              # rendering hint for the frontend
    metadata: dict                # routing metadata (agents, timings, etc.)
    file_metadata: dict           # attached file info from command center
    errors: Annotated[list, add]
    conversation_history: list    # prior turns for context
    session_artifacts: list       # artifacts created earlier in this session


# --- Intent Classification ---

INTENT_CLASSIFIER_PROMPT = """You are an intent classifier for DRPL's AI tender intelligence platform.

Given a user message and conversation history, classify the user's intent and select exactly ONE specialized agent to invoke.

## Available Agents:
- **deep_analyzer**: Tender Document Analyzer — performs forensic analysis of tender documents, producing a 7-section critical report: tender summary, complete document requirements, eligibility GO/NO-GO assessment, negative keyword & rejection risk analysis, missing items & gaps, regulatory intelligence, and prioritized next steps. Also extracts and analyzes linked/child documents from master PDFs.
- **checklist_generator**: Generates a submission checklist of required documents from tender analysis
- **proposal_creator**: Writes professional tender proposal documents, cover letters, declarations, certificates
- **costing_researcher**: Researches market rates and produces detailed cost breakdowns with GST
- **workspace_manager**: Manages the canvas workspace — initializes workspace, generates specific documents by name, shows workspace progress and document statuses
- **annexure_finder**: Extracts every annexure / schedule / appendix / proforma / form / declaration from the tender's PDFs and materializes each as an editable workspace document with fillable placeholders (downloadable as Word).
- **decision_maker**: Master orchestrator for MULTI-STEP / CROSS-AGENT / TROUBLESHOOTING / CONDITIONAL requests. Can call any other agent, inspect platform state (workspaces, checklists, annexures), repair broken data, and pick the best LLM per subtask. Use this when a single specialized agent above is NOT enough.
- **general_assistant**: The everyday assistant, and a fully capable agent in its own right — NOT a fallback. It searches the web and cites real sources, reads this tender's stored record and documents, does calculations, drafts emails / letters / notes / replies, generates Word and Excel files, and explains output the platform has already produced. Use it for anything conversational or open-ended.

## Intent Categories:
- `analyze_documents` → deep_analyzer
- `generate_checklist` → checklist_generator
- `write_proposal` → proposal_creator
- `estimate_costing` → costing_researcher
- `workspace_operations` → workspace_manager
- `extract_annexures` → annexure_finder
- `orchestrate_complex` → decision_maker
- `general_query` → general_assistant (research, drafting, questions, explanations)

## Core Rule — ALWAYS select exactly ONE agent:
Normally: if the user's request could involve multiple steps, route to the FIRST logical step only
and let its response suggest next steps.

BUT when the request is GENUINELY multi-step / cross-agent / troubleshooting / conditional
(see Escalation Rules below), select `decision_maker` — the master orchestrator will plan the
full workflow itself.

Priority order when the request is broad but clearly sequential (single agent):
1. deep_analyzer (always first — analysis informs everything else)
2. checklist_generator (depends on analysis)
3. proposal_creator / costing_researcher (depend on checklist)

## Escalation Rules — when to pick `decision_maker`:
E1. The user chains TWO OR MORE distinct agent domains in one message with coordination verbs
    ("and then", "also", "plus", "as well as", "both", "after that", comma-joined multi-asks).
    Example: "Analyze this tender AND extract the annexures AND generate a cost estimate."
E2. The user reports a PLATFORM problem or asks for a FIX/DIAGNOSE — phrases like
    "not working", "broken", "won't load", "won't open", "won't generate", "stuck", "missing",
    "error", "fix", "why isn't this working". The decision maker inspects state and repairs.
    This is about the platform misbehaving, NOT about the content of its output: "why is this
    rate so high" is a question about a costing (→ general_query), while "why is the costing
    stuck" is a platform fault (→ decision_maker).
E3. The user's request mentions TWO OR MORE of: analysis, annexure, checklist, proposal, costing,
    workspace — in a single ask.
E4. The request is CONDITIONAL / EXPLORATORY: "if eligible then draft proposal", "check X and
    decide Y", "figure out whether we should …".
E5. The request is ambiguous across agents and the user wants the platform to just "handle it".

Do NOT escalate to `decision_maker` for simple single-domain requests — those are faster via
the specialized agent directly. The decision maker costs more and is slower.

## Routing Rules:
1. If the user asks about analyzing, reviewing, or understanding tender documents → analyze_documents
2. If the user asks about checklists, required documents, or what to submit → generate_checklist
3. If the user asks about writing proposals, drafting documents, or generating content → write_proposal
4. If the user asks about costs, pricing, rates, or financial estimates → estimate_costing
5. If the user asks to "process this tender", "generate all documents", "automate this tender", or wants end-to-end
   processing → analyze_documents (start with analysis; suggest next steps after)
6. If the user is greeting, asking a general question, or requesting help → general_query
6b. If the user asks for WEB RESEARCH, market rates, supplier or competitor information, a
    regulation, or "search for X" → general_query (the general assistant searches and cites
    real sources; it is not a lesser path)
6c. If the user asks to DRAFT correspondence — an email, a reply, a letter to the buyer, a
    note, a summary, meeting points → general_query. `write_proposal` is for formal tender
    submission documents (proposals, cover letters for a bid, declarations, certificates);
    everyday writing is the general assistant's job.
6d. If the user asks a question ABOUT existing output — why a cost is what it is, what a
    clause means, where a number came from, "explain this" — → general_query. The general
    assistant reads the stored record to answer.
7. If the user asks about workspace, initializing workspace, or managing document generation workflow → workspace_operations
8. If the user asks to generate a specific named document (e.g., "generate the compliance certificate",
   "draft the covering letter", "create the BOQ") → workspace_operations
8b. If the user asks to find / extract / list / pull out annexures, schedules, appendices, proformas, forms,
    or declarations from the tender (e.g., "find all annexures", "extract the schedules", "pull out every
    proforma as an editable doc") → extract_annexures

## File Attachment Rules:
9. When files are attached, they serve as the document source — agents do NOT need a tender_id to work
10. If the user uploads a file and asks for analysis → analyze_documents
11. If the user uploads a file and the request is broad or unclear → analyze_documents (ALWAYS start with analysis first — NEVER skip to checklist or costing when no prior analysis exists in the session)
12. If the user uploads a file and asks specifically for a checklist → generate_checklist
13. If the user uploads a file and asks specifically for costing → estimate_costing
13b. If the user uploads a file and asks specifically to find / extract / list annexures,
     schedules, appendices, proformas, declarations, forms, templates, or editable copies
     of these (e.g., "find the annexures in this tender", "give me editable templates of
     every annexure", "extract all schedules as Word docs") → extract_annexures
13c. If the user uploads a file and asks specifically to write a proposal, cover letter,
     or other drafted document → write_proposal
14. NEVER classify as general_query when files are attached and the user asks for
    analysis, costing, checklists, annexures, or drafting

## Session Context Rules:
15. If the user references prior work ("this analysis", "the document I uploaded", "based on that", "from above"),
    check session state. If a prior analysis/artifact exists, route to the appropriate next agent —
    do NOT classify as general_query or ask for re-upload.
16. If the user asks for a follow-up task (checklist after analysis, costing after analysis),
    route to the appropriate agent. Prior context will be provided automatically.
17. When session state shows prior work was done, agents have access to that context.
    Do NOT require the user to re-upload or re-specify documents.
18. For conversational follow-ups about prior outputs ("what did the analysis say about...", "explain the risks"),
    classify as general_query — the general assistant has conversation history.

## Reasoning Approach (apply BEFORE picking an agent):
When extended thinking is enabled, USE that thinking budget to reason carefully — do not jump to a conclusion. Walk through:

1. **What does the user literally want?** Quote the verb (analyze, generate, find, draft, estimate). Verbs map directly to agents:
   - analyze/review/understand → deep_analyzer
   - check/list/required → checklist_generator
   - cost/price/estimate/budget → costing_researcher
   - draft/write/generate proposal/letter → proposal_creator
   - find/extract annexures/schedules → annexure_finder
   - generate <specific document> → workspace_manager
2. **How many distinct outputs are they asking for?** One verb + one noun = single agent. Two+ coordination verbs ("and", "then", "plus") OR two+ output types in one ask = decision_maker.
3. **What state already exists?** If the session has a prior analysis, costing-after-analysis is a follow-up (single agent, not multi-step). If nothing exists yet and they ask for end-to-end automation, deep_analyzer first (the response can suggest the next step).
4. **Did they upload a file but not specify intent?** → deep_analyzer (analysis informs everything else). The deterministic override above already handles this case before you even see the message.
5. **Are they asking ABOUT prior output or asking to PRODUCE new output?** "What did the analysis say about X" is general_query (conversational); "give me the costing for that" is estimate_costing (production).

Common misroutes to avoid:
- "Give me the rates for these items" with a tender attached → estimate_costing (NOT analyze_documents)
- "Find the eligibility section" with a tender attached → analyze_documents (it's an analysis sub-question, not a search)
- "What's the EMD?" after a prior analysis exists → general_query (factual lookup from prior context)
- "Draft an email to the buyer asking for a deadline extension" → general_query (everyday
  correspondence, not a formal submission document)
- "Where did this rate come from?" / "why is this so expensive?" → general_query (the general
  assistant reads the stored costing and can search to verify)
- "Generate everything I need to bid" → decision_maker (genuine multi-step orchestration)

Respond with ONLY a JSON object (no prose, no markdown):
{"intent": "<intent>", "agent": "<agent_key>", "multi_step": <true|false>, "reasoning": "<one-sentence reasoning>"}

The `reasoning` field should be a concise final justification — your detailed walk-through happens in extended thinking, not here. Set `multi_step: true` whenever you picked `decision_maker` (or whenever the request spans two+ agent domains)."""


AGENT_OUTPUT_TYPE_MAP = {
    "deep_analyzer": "document_analysis",
    "checklist_generator": "checklist",
    "proposal_creator": "proposal_document",
    "costing_researcher": "cost_breakdown",
    "workspace_manager": "workspace_operations",
    "annexure_finder": "annexures_extracted",
    "decision_maker": "decision_maker_trace",
}

AGENT_DISPLAY_NAMES = {
    "deep_analyzer": "Tender Document Analyzer",
    "checklist_generator": "Checklist Generator",
    "proposal_creator": "Proposal Creator",
    "costing_researcher": "Costing Researcher",
    "workspace_manager": "Workspace Manager",
    "annexure_finder": "Annexure Finder",
    "decision_maker": "Decision Maker",
}


# --- Node Functions ---

async def classify_intent_node(state: RouterState, db: Session) -> dict:
    """Classify user intent and select agent(s) to invoke.

    Phase 5 — extended thinking on the router. Routing decisions are the
    single most-impactful judgment in the platform: every chat message
    flows through here, and a wrong agent pick wastes the user's time +
    burns tokens on the wrong workflow. Adaptive thinking adds ~3s of
    latency but produces materially better decisions on ambiguous /
    multi-step / file-attached requests (where the user complaint
    "decision maker doesn't pick the right agent" originated).

    The previous 256-token budget was a relic from when the classifier
    had no reasoning step. With thinking enabled, the model needs room
    for both internal reasoning AND the JSON output; 8K leaves generous
    headroom even on the longest agent inventories.

    Override the platform's default thinking config via the
    `router_thinking_mode` PlatformSetting:
      - "auto" (default): adaptive on 4.6/4.7 models, off on older
      - "disabled": revert to the old behavior
      - "enabled": force enabled with the platform's thinking_budget_tokens
    """
    try:
        from app.services.settings_service import get_effective_setting
        _router_thinking = get_effective_setting(db, "router_thinking_mode", "auto")
        if isinstance(_router_thinking, str):
            _router_thinking = _router_thinking.lower()
        else:
            _router_thinking = "auto"

        llm = get_chat_model(
            db,
            agent_name="proposal_router",
            temperature=0.1,
            max_tokens=8192,  # room for thinking + JSON classification output
            thinking_mode_override=_router_thinking,
        )

        # Build history context (last 10 turns with output_type metadata)
        history_text = ""
        for turn in state.get("conversation_history", [])[-10:]:
            role = turn.get("role", "user")
            content = turn.get("content", "")[:1000]
            output_type = turn.get("output_type", "")
            routed_tag = f" [{output_type}]" if output_type else ""
            history_text += f"{role}{routed_tag}: {content}\n"

        tender_context = ""
        if state.get("tender_id"):
            tender_context = f"\nThe user is working on tender ID: {state['tender_id']}"

        # Build file attachment context for the classifier
        file_context_note = ""
        fm = state.get("file_metadata") or {}
        if fm.get("has_files"):
            file_names = [f.get("name", "unknown") for f in fm.get("files", [])]
            file_context_note = (
                f"\nThe user has attached {fm['file_count']} file(s): {', '.join(file_names)}."
                f" File content has been extracted ({fm.get('total_extracted_chars', 0):,} chars)."
                " These files can be used directly by agents — no tender_id is required."
            )

        # Deterministic override: files uploaded + no prior analysis + neutral
        # request → deep_analyzer. The override exists to guarantee a first-pass
        # analysis for the canonical "user drops a tender PDF, say nothing
        # specific" flow. It must NOT fire when the user has explicitly asked
        # for a different agent's output (annexures, checklist, costing, a
        # specific document, a proposal) — otherwise those intents get silently
        # converted to a generic analysis. Observed complaint 2026-04-21: a
        # user uploaded a tender and asked for "annexure templates" and still
        # got deep_analyzer because this override preempted the LLM classifier.
        session_artifacts = state.get("session_artifacts", [])
        has_prior_analysis = any(sa.get("type") == "analysis" for sa in session_artifacts)

        import re as _re
        user_msg_lower = (state.get("user_message") or "").lower()
        _explicit_intent_patterns = [
            # Annexures / schedules / proformas / templates
            r'\bannexure(s)?\b', r'\bschedule(s)?\b', r'\bproforma(s)?\b',
            r'\bappendix\b', r'\bappendices\b', r'\bdeclaration(s)?\b',
            r'\btemplate(s)?\b', r'\beditable\b', r'\bfillable\b',
            # Checklist
            r'\bcheck[- ]?list\b', r'\brequired document(s)?\b',
            r'\bsubmission (list|checklist)\b',
            # Costing
            r'\bcost(ing)?\b', r'\bprice\b', r'\bbudget\b', r'\bbom\b',
            r'\bboq\b', r'\brate analysis\b', r'\bestimate(s)?\b',
            # Proposal / specific document generation
            r'\bproposal\b', r'\bcover(ing)? letter\b',
            r'\bcompliance certificate\b', r'\bdraft (the|a|my)\b',
            r'\bgenerate (the|a|my)\b',
            # Workspace ops
            r'\bworkspace\b', r'\binit(ialize)? workspace\b',
        ]
        has_explicit_intent = any(
            _re.search(p, user_msg_lower) for p in _explicit_intent_patterns
        )

        if fm.get("has_files") and not has_prior_analysis and not has_explicit_intent:
            logger.info(
                "Deterministic routing: files uploaded, no prior analysis, "
                "neutral request → deep_analyzer"
            )
            return {
                "intent": "analyze_documents",
                "selected_agents": ["deep_analyzer"],
                "metadata": {
                    "classification": {
                        "intent": "analyze_documents",
                        "agent": "deep_analyzer",
                        "reasoning": "Files uploaded with no prior analysis and no explicit agent intent — deterministic override",
                    },
                    "deterministic_override": True,
                },
            }

        if fm.get("has_files") and has_explicit_intent:
            logger.info(
                "Explicit intent detected in user message — skipping deep_analyzer "
                "override, letting LLM classifier route"
            )

        # Build session artifact context
        session_state_note = ""
        if session_artifacts:
            completed_items = []
            for sa in session_artifacts:
                sa_type = sa.get("type", "unknown")
                sa_title = sa.get("title", "")
                sa_agent = sa.get("agent_key", "")
                completed_items.append(f"  - {sa_title} ({sa_type}, by {sa_agent})")
            session_state_note = (
                "\n\nSession state — work already completed in this session:\n"
                + "\n".join(completed_items)
                + "\nAgents have access to all prior outputs. Do NOT require re-upload or re-specification."
            )

        # Dynamically add custom agents to the classifier prompt
        custom_agent_section = ""
        try:
            from app.models.agent_builder import CustomAgent
            custom_agents = db.query(CustomAgent).filter(
                CustomAgent.is_enabled == True,
                CustomAgent.is_published == True,
                CustomAgent.agent_type.in_(["react", "tool_use", "chain_of_thought"]),
                CustomAgent.agent_key.notin_([
                    "deep_analyzer", "checklist_generator", "proposal_creator",
                    "costing_researcher", "proposal_router", "tender_pipeline",
                    "classifier", "relevance", "risk", "summary", "eligibility",
                    "checklist", "proposal", "document_analyzer",
                    "checklist_generator",
                ]),
            ).all()
            if custom_agents:
                custom_lines = ["\n\n## Additional Custom Agents:"]
                for ca in custom_agents:
                    custom_lines.append(f"- **{ca.agent_key}**: {ca.description or ca.display_name}")
                custom_lines.append("\nYou may route to these custom agents when the user's request matches their description.")
                custom_agent_section = "\n".join(custom_lines)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        classifier_prompt = INTENT_CLASSIFIER_PROMPT + custom_agent_section

        user_msg = (
            f"Conversation history:\n{history_text}\n"
            f"Current message: {state['user_message']}"
            f"{tender_context}"
            f"{file_context_note}"
            f"{session_state_note}\n\n"
            f"Classify the intent and select agent(s)."
        )

        result = await llm.ainvoke([
            SystemMessage(content=classifier_prompt),
            HumanMessage(content=user_msg),
        ])

        # Parse the classification
        response_text = result.content.strip()
        # Extract JSON from possible markdown code block
        if "```" in response_text:
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.split("```")[0].strip()

        classification = json.loads(response_text)

        intent = classification.get("intent", "general_query")

        # Support both new singular "agent" and legacy "agents" array format
        agent = classification.get("agent", "")
        if not agent:
            # Fallback: if LLM returned old array format, take the first one
            legacy_agents = classification.get("agents", [])
            agent = legacy_agents[0] if legacy_agents else ""

        # Validate agent key — built-in + custom published agents
        valid_agents = {"deep_analyzer", "checklist_generator", "proposal_creator", "costing_researcher", "workspace_manager", "annexure_finder", "decision_maker"}
        try:
            from app.models.agent_builder import CustomAgent
            custom_keys = {
                a.agent_key for a in
                db.query(CustomAgent.agent_key).filter(
                    CustomAgent.is_enabled == True,
                    CustomAgent.is_published == True,
                    CustomAgent.agent_type.in_(["react", "tool_use", "chain_of_thought"]),
                ).all()
            }
            valid_agents = valid_agents | custom_keys
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        if intent == "general_query" or agent not in valid_agents:
            intent = "general_query"
            selected = []
        else:
            selected = [agent]  # Always a single-element list

        return {
            "intent": intent,
            "selected_agents": selected,
            "metadata": {
                "classification": classification,
                "classifier_reasoning": classification.get("reasoning", ""),
            },
        }

    except Exception as e:
        logger.error(f"Intent classification failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return {
            "intent": "general_query",
            "selected_agents": [],
            "errors": [f"Classification error: {str(e)}"],
            "metadata": {"classification_error": str(e)},
        }


async def execute_agents_node(state: RouterState, db: Session) -> dict:
    """Execute selected agent(s) sequentially, passing results forward."""
    from app.services.langchain.graphs.chat_agent_wrappers import (
        chat_document_analysis,
        chat_checklist_generation,
        chat_proposal_writing,
        chat_costing_research,
        chat_annexure_finder,
    )

    agent_dispatch = {
        "deep_analyzer": chat_document_analysis,
        "checklist_generator": chat_checklist_generation,
        "proposal_creator": chat_proposal_writing,
        "costing_researcher": chat_costing_research,
        "annexure_finder": chat_annexure_finder,
    }

    agent_results = {}
    errors = []

    # Single-agent execution — always exactly one agent
    agent_key = state["selected_agents"][0] if state["selected_agents"] else None
    if not agent_key:
        return {"agent_results": {}, "errors": ["No agent selected"]}

    # Special-case: decision_maker is not a chat wrapper — it's the master orchestrator
    if agent_key == "decision_maker":
        from app.services.langchain.graphs.decision_maker_agent import run_decision_maker
        from app.core.config import get_settings
        # Autonomous by default: the master agent plans + executes itself and only
        # asks when blocked. Toggle off via DECISION_MAKER_AUTONOMOUS to restore the
        # legacy propose-plan-then-approve gate.
        dm_mode = "autonomous" if get_settings().decision_maker_autonomous else "planning"
        start = time.time()
        try:
            result = await run_decision_maker(
                db=db,
                message=state["user_message"],
                session_id=state.get("session_id"),
                tender_id=state.get("tender_id"),
                user_id=(state.get("metadata") or {}).get("user_id"),
                conversation_history=state.get("conversation_history", []),
                file_metadata=state.get("file_metadata", {}),
                proposal_session_id=(state.get("metadata") or {}).get("proposal_session_id"),
                stream_callback=(state.get("metadata") or {}).get("stream_callback"),
                mode=dm_mode,
            )
            result["latency_ms"] = int((time.time() - start) * 1000)
            return {"agent_results": {"decision_maker": result}, "errors": []}
        except Exception as e:
            logger.error(f"Decision maker failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass
            from app.services.langchain.error_utils import format_user_error
            return {
                "agent_results": {"decision_maker": {
                    "output": format_user_error(e),
                    "output_type": "decision_maker_trace",
                    "agent_key": "decision_maker",
                    "tool_calls": [],
                    "trace": [],
                    "metrics": {},
                    "latency_ms": int((time.time() - start) * 1000),
                    "status": "failed",
                }},
                "errors": [f"decision_maker error: {str(e)}"],
            }

    handler = agent_dispatch.get(agent_key)
    if not handler:
        # Try custom agent
        try:
            from app.services.langchain.graphs.chat_agent_wrappers import chat_custom_agent
            from app.models.agent_builder import CustomAgent
            custom_agent = db.query(CustomAgent).filter(
                CustomAgent.agent_key == agent_key,
                CustomAgent.is_enabled == True,
            ).first()
            if custom_agent:
                handler = lambda db, message, tender_id, session_id, _key=agent_key, **kwargs: chat_custom_agent(
                    db=db, message=message, tender_id=tender_id,
                    session_id=session_id, agent_key=_key,
                )
                AGENT_DISPLAY_NAMES[agent_key] = custom_agent.display_name
                AGENT_OUTPUT_TYPE_MAP[agent_key] = "general"
            else:
                return {"agent_results": {}, "errors": [f"Unknown agent: {agent_key}"]}
        except Exception as e:
            return {"agent_results": {}, "errors": [f"Failed to load custom agent {agent_key}: {str(e)}"]}

    start = time.time()
    try:
        result = await handler(
            db=db,
            message=state["user_message"],
            tender_id=state.get("tender_id"),
            session_id=state["session_id"],
            conversation_history=state.get("conversation_history", []),
            file_metadata=state.get("file_metadata", {}),
        )

        elapsed_ms = int((time.time() - start) * 1000)
        result["latency_ms"] = elapsed_ms
        agent_results[agent_key] = result

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        logger.error(f"Agent {agent_key} failed: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        from app.services.langchain.error_utils import format_user_error
        errors.append(f"{agent_key} error: {str(e)}")
        agent_results[agent_key] = {
            "output": format_user_error(e),
            "output_type": "general",
            "agent_key": agent_key,
            "tool_calls": [],
            "metrics": {},
            "latency_ms": elapsed_ms,
            "status": "failed",
        }

    return {
        "agent_results": agent_results,
        "errors": errors,
    }


async def compose_response_node(state: RouterState, db: Session) -> dict:
    """Compose the final response from agent results."""
    agent_results = state.get("agent_results", {})
    selected_agents = state.get("selected_agents", [])

    if not agent_results:
        return {
            "final_response": "I couldn't process your request. Please try again.",
            "output_type": "general",
        }

    # Single-agent passthrough (router always selects exactly one agent)
    agent_key = list(agent_results.keys())[0]
    result = agent_results[agent_key]
    return {
        "final_response": result.get("output", ""),
        "output_type": result.get("output_type", "general"),
        "metadata": {
            **state.get("metadata", {}),
            "agents_used": [agent_key],
            "agent_display_names": [AGENT_DISPLAY_NAMES.get(agent_key, agent_key)],
            "agent_latencies": {agent_key: result.get("latency_ms", 0)},
        },
    }


async def general_response_node(state: RouterState, db: Session) -> dict:
    """Handle general queries — the non-streaming twin of the branch in
    `streaming_handler`.

    Both used to be a bare LLM call with no tools and no tender_id, and had
    already drifted apart: two copies of the same prompt, one of them a line
    shorter. They now share `run_general_assistant`, so a capability added to
    the assistant reaches both without anyone remembering to copy it.
    """
    try:
        from app.services.langchain.graphs.general_assistant_agent import (
            run_general_assistant,
        )

        result = await run_general_assistant(
            db,
            state["user_message"],
            tender_id=state.get("tender_id"),
            session_id=state.get("session_id"),
            proposal_session_id=state.get("proposal_session_id"),
            user_id=state.get("user_id"),
            conversation_history=state.get("conversation_history"),
            file_metadata=state.get("file_metadata"),
        )

        return {
            "final_response": result.get("output") or "",
            "output_type": "general",
            "metadata": {
                **state.get("metadata", {}),
                "agents_used": ["general_assistant"],
                "agent_display_names": ["DRPL Assistant"],
                "tool_calls": result.get("tool_calls") or [],
                "sources": result.get("sources") or [],
            },
        }

    except Exception as e:
        logger.error(f"General response failed: {e}")
        return {
            "final_response": "I'm sorry, I encountered an error. Please try again.",
            "output_type": "general",
            "errors": [f"General response error: {str(e)}"],
        }


# --- Router Decision ---

def should_route_to_agents(state: RouterState) -> str:
    """Decide whether to route to specialized agents or handle generally."""
    if state.get("intent") == "general_query" or not state.get("selected_agents"):
        return "general"
    return "agents"


# --- Graph Builder ---

def build_agent_router(db: Session) -> StateGraph:
    """Build and compile the agent router graph."""

    # Create node functions that close over db
    async def classify(state):
        return await classify_intent_node(state, db)

    async def execute(state):
        return await execute_agents_node(state, db)

    async def compose(state):
        return await compose_response_node(state, db)

    async def general(state):
        return await general_response_node(state, db)

    # Build graph
    graph = StateGraph(RouterState)

    graph.add_node("classify_intent", classify)
    graph.add_node("execute_agents", execute)
    graph.add_node("compose_response", compose)
    graph.add_node("general_response", general)

    graph.set_entry_point("classify_intent")

    graph.add_conditional_edges(
        "classify_intent",
        should_route_to_agents,
        {
            "agents": "execute_agents",
            "general": "general_response",
        },
    )

    graph.add_edge("execute_agents", "compose_response")
    graph.add_edge("compose_response", END)
    graph.add_edge("general_response", END)

    return graph.compile()


# --- Public Entry Point ---

async def route_and_execute(
    db: Session,
    message: str,
    session_id: Optional[str] = None,
    tender_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> dict:
    """
    Route a user message to the appropriate agent(s) and return the response.

    Args:
        db: Database session
        message: User's message
        session_id: Optional existing session ID
        tender_id: Optional tender context
        user_id: User making the request

    Returns:
        Dict with output, session_id, output_type, agent_key, tool_calls, metadata
    """
    sess_id = session_id or str(uuid.uuid4())

    # Load conversation history for context
    history = []
    if session_id:
        turns = get_conversation_history(db, session_id, limit=20)
        history = [
            {"role": t.role, "content": t.content, "output_type": t.output_type}
            for t in turns
        ]

    # Save user turn
    save_conversation_turn(
        db, sess_id, "proposal_router", "user", message,
        output_type=None, routed_from=None,
    )

    # Build and execute the router graph
    router = build_agent_router(db)

    initial_state = {
        "session_id": sess_id,
        "user_message": message,
        "tender_id": tender_id,
        "intent": "",
        "selected_agents": [],
        "agent_results": {},
        "final_response": "",
        "output_type": "general",
        "metadata": {},
        "errors": [],
        "conversation_history": history,
        "session_artifacts": [],
    }

    start_time = time.time()
    result = await router.ainvoke(initial_state)
    total_latency = int((time.time() - start_time) * 1000)

    # Extract response
    final_response = result.get("final_response", "")
    output_type = result.get("output_type", "general")
    metadata = result.get("metadata", {})
    agents_used = metadata.get("agents_used", [])

    # Save assistant turn with routing info
    routed_from = ",".join(agents_used) if agents_used else "general"
    save_conversation_turn(
        db, sess_id, "proposal_router", "assistant", final_response,
        tool_calls=None,
        metadata={"routing": metadata, "latency_ms": total_latency},
        output_type=output_type,
        routed_from=routed_from,
    )

    return {
        "output": final_response,
        "session_id": sess_id,
        "output_type": output_type,
        "intent": result.get("intent", "general_query"),
        "agents_used": agents_used,
        "agent_display_names": metadata.get("agent_display_names", []),
        "metadata": metadata,
        "latency_ms": total_latency,
        "errors": result.get("errors", []),
    }
