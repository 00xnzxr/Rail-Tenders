"""DRPL — the Command Center's general assistant.

Every message the intent classifier cannot place as a specialist job lands
here: research, drafting an email or a letter, explaining a costing the
platform already produced, an ordinary factual question about a tender.

Until this module existed, that path was a bare ``llm.ainvoke`` with no tools
and no ``tender_id``. It is how a user came to be shown three market-rate
sources — infralens.in, tendertiger.com, asiantender.com — that the assistant
had never visited, followed by an honest admission that they "were
illustrative examples ... not actual search results I retrieved". The model
was not being evasive; it had no hands.

Two design points that are easy to get wrong later:

1. **The belt is explicit, not the whole repo.** The full tool catalog is
   roughly 7,900 tokens of schema, and large tool sets measurably degrade tool
   selection. A generalist needs reach, not everything.
2. **It grows by tools, not by agents.** A new user need — a new lookup, a new
   file format — belongs in ``GENERAL_ASSISTANT_TOOL_KEYS`` and in
   ``tool_policy``, not in a new agent plus a new router rule.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

AGENT_KEY = "general_assistant"

#: The conversational toolbelt.
#:
#: Every key here is already classified in ``tool_policy`` — ten reads, plus
#: the two generators, which are writes and therefore still raise the
#: confirmation card. Adding a key means classifying it there too: an
#: unclassified tool is gated as a write, which would make the assistant demand
#: a confirmation before an ordinary web search.
GENERAL_ASSISTANT_TOOL_KEYS: list[str] = [
    # Research — the half that was missing entirely.
    "web_search",
    "web_fetch",
    # This tender, as stored, rather than as remembered from the chat.
    "document_reader",
    "tender_lookup",
    "semantic_search",
    # What was actually said earlier, when it has fallen out of the window.
    "conversation_history_search",
    "ratecard_lookup",
    # Arithmetic that should not be done in the model's head.
    "cost_calculator",
    # Continuity across turns.
    "memory_store",
    "memory_retrieve",
    # Ask rather than guess.
    "clarify",
    # Drafting to a file when the user wants one.
    "docx_generator",
    "xlsx_generator",
]

GENERAL_ASSISTANT_SYSTEM_PROMPT = """You are DRPL's assistant inside the Command Center.

DRPL bids on Indian government tenders — mostly Indian Railways (IREPS) and GeM.
The person you are talking to is a working bid manager, not a visitor. Answer
like a capable colleague: direct, specific, no throat-clearing.

## What you can do

You are a generalist and you have tools. Use them.

- **Research** — `web_search` for market rates, suppliers, specifications,
  regulations, anything external. `web_fetch` to read a specific page.
- **This tender** — `tender_lookup` for the stored tender record (values, dates,
  EMD, status), `document_reader` for its documents, `semantic_search` to find a
  passage across them, `ratecard_lookup` for DRPL's own rates.
- **Arithmetic** — `cost_calculator`. Do not do multi-step money maths in your head.
- **Drafting** — write emails, letters, notes, summaries and replies directly in
  chat. When the user wants a file, use `docx_generator` or `xlsx_generator`.
- **Continuity** — `memory_store` / `memory_retrieve` for things worth carrying
  across turns.
- **Ask** — `clarify` when a genuine ambiguity would change the answer. Once, not
  as a habit.

## Sources: the rule that matters most

Any claim about a market rate, a price, a supplier, a specification, or anything
else external MUST come from an actual `web_search` or `web_fetch` observation in
this conversation.

- When you searched, end with a `## Sources` section listing the URLs those
  observations actually returned.
- When you did not search, say so plainly — "I have not verified this against
  live sources" — and give the reasoning for what it is: an estimate.
- When a search fails or returns nothing, report that. Do not fill the gap with
  a plausible-sounding site name.

Never write a URL or a domain you did not receive from a tool. A named source
the user cannot open is worse than no source at all.

## About prior output

When the user asks why a costing came out a certain way, or what an analysis
said, read the stored record first — `tender_lookup`, then `document_reader` or
`semantic_search`. The chat scrollback is a summary of the work, not the work.
Answering from it produces confident reconstruction instead of fact.

## Handing off

Some jobs belong to a specialist, and you can call one directly when the user
genuinely wants that output produced — a full cost breakdown, a forensic
document analysis, a submission checklist, annexure extraction, a drafted
proposal. Explaining or adjusting existing output is your own job; producing a
new one is theirs. Say what you are doing before you hand off.

## Style

Markdown. Lead with the answer. Tables for anything with rows and columns.
Amounts in ₹ with Indian digit grouping. No filler openers, no summary of what
you are about to say. If you do not know and cannot find out, say that in one
sentence.
"""


def _build_tools(
    db: Session,
    *,
    proposal_session_id: Optional[int],
    session_id: Optional[str],
    user_id: Optional[int],
    conversation_history: Optional[list[dict]],
    file_metadata: Optional[dict],
    stream_callback: Optional[Callable[[str, dict], None]],
) -> list:
    """The belt, plus one `call_<agent>` tool per enabled specialist.

    `load_tools_by_keys` already routes everything through
    `tool_policy._apply_policy`, so the writes in the belt stay gated.
    """
    from app.services.langchain.tools.tool_loader import load_tools_by_keys

    tools = load_tools_by_keys(
        db,
        GENERAL_ASSISTANT_TOOL_KEYS,
        agent_key=AGENT_KEY,
        proposal_session_id=proposal_session_id,
        router_session_id=session_id,
    )

    # Specialist handoff. Reused from the orchestrator rather than
    # reimplemented — `costing_researcher` resolves to `chat_costing_research`,
    # the canonical path that parses the NIT schedule and prices every row, so
    # a chat handoff cannot collapse a 138-row spares schedule into a summary.
    try:
        from app.services.langchain.graphs.orchestrator_tools import (
            build_agent_wrapper_tools,
        )

        from app.services.langchain.tools.tool_loader import TOOL_FREE_AGENTS

        handoff = build_agent_wrapper_tools(
            session_id=session_id,
            proposal_session_id=proposal_session_id,
            user_id=user_id,
            conversation_history=conversation_history,
            file_metadata=file_metadata,
            stream_callback=stream_callback,
            db=db,
        )
        # Drop the machinery agents — scoring a tender 0-1, picking a category,
        # writing a two-sentence summary, extracting a costing scope. They are
        # pipeline internals with no meaning as a chat request, and every one
        # of them is schema the model has to read past to find the tool it
        # actually wants. Same reasoning as the belt being explicit.
        # ...and never itself: it is on the worker roster now (so the Master
        # can delegate to it), which means its own roster contains it too —
        # unbounded recursion with a per-call price tag without this line.
        hidden = {f"call_{key}" for key in TOOL_FREE_AGENTS}
        hidden.add(f"call_{AGENT_KEY}")
        tools = tools + [t for t in handoff if t.name not in hidden]
    except Exception as e:
        # A generalist that can still search and read is far better than no
        # answer at all, so a broken roster degrades rather than fails.
        logger.warning("[general_assistant] specialist handoff unavailable: %s", e)

    return tools


def _context_block(
    db: Session,
    tender_id: Optional[int],
    file_metadata: Optional[dict],
) -> str:
    """What the agent should know before it starts choosing tools."""
    lines: list[str] = []
    if tender_id:
        lines.append(
            f"Active tender: id={tender_id}. Pass this tender_id to any tool that "
            f"takes one. Read it before answering questions about it."
        )
    if file_metadata:
        names = []
        for f in (file_metadata or {}).get("files") or []:
            name = f.get("name") if isinstance(f, dict) else None
            if name:
                names.append(str(name))
        if names:
            lines.append(
                f"User attached {len(names)} file(s): {', '.join(names[:10])}. "
                "They are already uploaded — never ask for a re-upload."
            )
    return ("\n".join(lines) + "\n\n") if lines else ""


async def run_general_assistant(
    db: Session,
    message: str,
    *,
    tender_id: Optional[int] = None,
    session_id: Optional[str] = None,
    proposal_session_id: Optional[int] = None,
    user_id: Optional[int] = None,
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    stream_callback: Optional[Callable[[str, dict], None]] = None,
    max_iterations: int = 8,
    max_execution_time: float = 180.0,
) -> dict[str, Any]:
    """Answer an open-ended Command Center message, with tools.

    Returns the specialized-agent shape the streaming handler already knows:

        {
            "output": str,
            "output_type": "general",
            "agent_key": "general_assistant",
            "tool_calls": list[dict],
            "sources": list[str],
            "status": "completed" | "needs_confirmation" | "failed",
            "pending_action": dict | None,
        }

    Events fired through ``stream_callback``:

        token        {content}       - streamed text of the final answer
        token_reset  {}              - discard streamed text: that turn called a tool
        agent_status {phase, message, run_id, started_at_ms}
    """
    from langchain_core.messages import SystemMessage
    from langgraph.prebuilt import create_react_agent

    from app.core.run_context import run_id_scope
    from app.services.langchain.callback_handler import DRPLCallbackHandler
    from app.services.langchain.llm_factory import get_chat_model
    from app.services.langchain.canonical_registry import resolve_system_prompt
    from app.services.langchain.tool_policy import PendingActionCapture, policy_scope

    started_at = time.monotonic()
    run_id = uuid.uuid4().hex
    tool_calls: list[dict] = []
    sources: list[str] = []

    def _emit(event: str, data: dict) -> None:
        if not stream_callback:
            return
        try:
            stream_callback(event, data)
        except Exception:
            pass

    with run_id_scope(run_id):
        capture = PendingActionCapture()
        try:
            tools = _build_tools(
                db,
                proposal_session_id=proposal_session_id,
                session_id=session_id,
                user_id=user_id,
                conversation_history=conversation_history,
                file_metadata=file_metadata,
                stream_callback=stream_callback,
            )

            # Honours a prompt edited in Agent Builder; falls back to the
            # canonical constant through this module's registry entry.
            base_prompt, _src = resolve_system_prompt(db, AGENT_KEY)

            llm = get_chat_model(db, agent_name=AGENT_KEY, temperature=0.3)
            agent = create_react_agent(
                model=llm,
                tools=tools,
                prompt=SystemMessage(content=base_prompt),
            )

            # Shared with the Master. This loop was correct on the happy path
            # and wrong in the two ways any hand-rolled version is: it could
            # lead with an assistant turn when the window opened mid-exchange,
            # and it left the unanswered user turn from a failed run adjacent
            # to the new one — two user messages in a row, which some providers
            # reject outright. See `chat_messages`.
            from app.core.config import get_settings as _get_settings
            from app.services.langchain.chat_messages import (
                build_conversation_messages,
            )

            _st = _get_settings()
            conversation = build_conversation_messages(
                conversation_history,
                _context_block(db, tender_id, file_metadata) + message,
                max_turns=int(_st.master_history_max_turns),
                max_chars_per_turn=int(_st.master_history_max_chars_per_turn),
                max_total_chars=int(_st.master_history_max_total_chars),
            )

            config = {
                "callbacks": [DRPLCallbackHandler(db, agent_name=AGENT_KEY)],
                "recursion_limit": max_iterations * 2 + 2,
            }

            answer_parts: list[str] = []

            async def _drive() -> None:
                """Stream the run, keeping only the final turn's text.

                A ReAct agent has several model turns. Anything streamed before
                a tool call is narration on the way to an answer, not the
                answer — `token_reset` tells the client to drop it, so the
                saved message is the reply and nothing else.
                """
                async for ev in agent.astream_events(
                    {"messages": conversation}, config=config, version="v2",
                ):
                    kind = ev.get("event")

                    if kind == "on_chat_model_stream":
                        chunk = (ev.get("data") or {}).get("chunk")
                        text = _chunk_text(chunk)
                        if text:
                            answer_parts.append(text)
                            _emit("token", {"content": text})

                    elif kind == "on_chat_model_end":
                        output = (ev.get("data") or {}).get("output")
                        if _has_tool_calls(output):
                            answer_parts.clear()
                            _emit("token_reset", {})

                    elif kind == "on_tool_start":
                        name = ev.get("name") or "tool"
                        _emit("agent_status", {
                            "phase": "tool_running",
                            "run_id": run_id,
                            "message": _tool_status_message(name),
                            "started_at_ms": int(time.time() * 1000),
                        })

                    elif kind == "on_tool_end":
                        name = ev.get("name") or "tool"
                        output = (ev.get("data") or {}).get("output")
                        tool_calls.append({
                            "tool": name,
                            "input": (ev.get("data") or {}).get("input"),
                        })
                        sources.extend(_extract_urls(output))
                        _emit("agent_status", {"phase": "tool_done", "run_id": run_id})

            with policy_scope(capture=capture, stream_callback=stream_callback):
                await asyncio.wait_for(_drive(), timeout=max_execution_time)

            output = "".join(answer_parts).strip()

            if capture.is_pending():
                return {
                    "output": output or (
                        "I need your confirmation before doing that."
                    ),
                    "output_type": "general",
                    "agent_key": AGENT_KEY,
                    "tool_calls": tool_calls,
                    "sources": _dedupe(sources),
                    "status": "needs_confirmation",
                    "pending_action": dict(capture),
                }

            if not output:
                output = (
                    "I wasn't able to produce an answer for that. Try rephrasing, "
                    "or tell me which part you want me to focus on."
                )

            logger.info(
                "[general_assistant] completed in %.1fs, %d tool call(s)",
                time.monotonic() - started_at, len(tool_calls),
            )
            return {
                "output": output,
                "output_type": "general",
                "agent_key": AGENT_KEY,
                "tool_calls": tool_calls,
                "sources": _dedupe(sources),
                "status": "completed",
                "pending_action": None,
            }

        except asyncio.TimeoutError:
            logger.warning(
                "[general_assistant] timed out after %.0fs", max_execution_time,
            )
            return {
                "output": (
                    "_That took longer than I'm allowed to spend on one answer._\n\n"
                    "Try a narrower question, or ask me to look at one thing at a time."
                ),
                "output_type": "general",
                "agent_key": AGENT_KEY,
                "tool_calls": tool_calls,
                "sources": _dedupe(sources),
                "status": "failed",
                "pending_action": None,
            }
        except Exception as e:
            logger.error("[general_assistant] failed: %s", e, exc_info=True)
            raise


# ──────────────────────────────────────────────────────────────────────────
# Small helpers, kept module-level so the tests can reach them directly.
# ──────────────────────────────────────────────────────────────────────────

def _chunk_text(chunk: Any) -> str:
    """Text out of a streamed chunk, whatever shape the provider used.

    Anthropic emits content as a list of typed blocks once tool use or
    thinking is in play, so `chunk.content` is not always a string.
    """
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts)
    return ""


def _has_tool_calls(message: Any) -> bool:
    calls = getattr(message, "tool_calls", None)
    if calls:
        return True
    chunks = getattr(message, "tool_call_chunks", None)
    return bool(chunks)


def _extract_urls(output: Any) -> list[str]:
    """URLs a tool actually returned — the only ones allowed to be cited."""
    import re

    text = output if isinstance(output, str) else str(output or "")
    if not text:
        return []
    return re.findall(r"https?://[^\s\"'<>\\)\]}]+", text)[:20]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


_TOOL_STATUS_MESSAGES = {
    "web_search": "Searching the web",
    "web_fetch": "Reading a page",
    "document_reader": "Reading the tender documents",
    "tender_lookup": "Looking up the tender",
    "semantic_search": "Searching the documents",
    "ratecard_lookup": "Checking DRPL rate cards",
    "cost_calculator": "Calculating",
    "docx_generator": "Generating a Word document",
    "xlsx_generator": "Generating a spreadsheet",
}


def _tool_status_message(tool_name: str) -> str:
    if tool_name.startswith("call_"):
        return f"Handing off to {tool_name[5:].replace('_', ' ')}"
    return _TOOL_STATUS_MESSAGES.get(tool_name, "Working")
