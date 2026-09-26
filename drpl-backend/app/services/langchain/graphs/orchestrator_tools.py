"""
DRPL LangChain - Decision Maker Orchestrator Tools
Four families of tools for the master agent:

  A. Agent wrappers     — call specialized agents (deep_analyzer, checklist, ...).
  B. Diagnostic tools   — read-only platform state inspection.
  C. Action tools       — narrowly-scoped repair / re-run operations.
  D. LLM meta-tools     — invoke any LLM provider, run web_search / document_reader.

Each factory returns a list of LangChain BaseTool instances configured with
the current request's context (session_id, user_id, file_metadata, etc.).
Tools open fresh SQLAlchemy sessions internally — they never share the
master's db session, to prevent transaction cross-contamination.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re as _re
from typing import Any, Callable, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.agent_builder import CustomAgent

logger = logging.getLogger(__name__)


class PlanCapture(dict):
    """Mutable shared container passed into ``build_plan_proposal_tool``.

    The ``propose_plan`` tool writes the submitted plan into this dict. After
    the ReAct loop finishes, ``run_decision_maker`` inspects it and — if a plan
    was captured — returns a ``pending_plan`` status instead of trying to
    execute further.

    Using a mutable container (rather than a raised exception) is necessary
    because LangGraph's ToolNode catches tool exceptions and converts them
    into ToolMessages, which would keep the agent loop going.
    """


class WorkerOutputStore(dict):
    """Every worker output this Master run produced, in full, by handle.

    The Master is handed a budgeted slice of a worker's answer inline; when the
    answer is longer than that, the whole of it stays here and
    ``read_worker_output`` pages through it. Without this the remainder simply
    did not exist anywhere the Master could reach — the delegation returned a
    stub and the stub was all the user ever got.

    Bounded at ``_MAX_STORED_WORKER_OUTPUTS`` entries, oldest evicted first: a
    long run must not accumulate megabytes of analysis text for the sake of a
    remainder nobody asked to read.
    """

    def __init__(self) -> None:
        super().__init__()
        self._issued = 0

    def put(self, agent_key: str, text: str) -> str:
        # A monotonic counter, not `len(self)`: once eviction starts, the
        # length stops growing and a length-based handle would be re-issued —
        # overwriting text the Master still holds a handle to, so a later
        # read returned some other call's output under the first call's name.
        self._issued += 1
        handle = f"{agent_key}:{self._issued}"
        self[handle] = text
        while len(self) > _MAX_STORED_WORKER_OUTPUTS:
            self.pop(next(iter(self)))
        return handle


_MAX_STORED_WORKER_OUTPUTS = 12


class AskUserCapture(dict):
    """Mutable shared container for the autonomous-mode ``ask_user`` tool.

    The autonomous Master Agent acts without an approval gate; ``ask_user`` is
    its single escape hatch — used ONLY when it is genuinely blocked by missing
    information it cannot derive. The tool writes the question here; after the
    loop, ``run_decision_maker`` returns ``needs_input`` with that question.
    """


# ────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ────────────────────────────────────────────────────────────────────────────

def _acting_for(user_id: Optional[int]):
    """The user a diagnostic or repair tool acts for.

    The ambient actor (opened by streaming_handler with the role read from
    the database) wins; the builder's ``user_id`` is the fallback. With
    neither, the ownership queries below fail closed.
    """
    from app.core.actor_context import Actor, current_actor

    actor = current_actor()
    if actor is not None:
        return actor
    return Actor(user_id=user_id) if user_id is not None else None


def _fresh_session() -> Session:
    """Open a fresh SQLAlchemy session from the pool."""
    from app.core.database import SessionLocal
    return SessionLocal()


#: Default ceiling on one tool result handed back to the Master, in characters.
#: Diagnostic and action tools return small structured payloads and stay well
#: under it; the worker-call payload passes its own, larger limit.
_TOOL_RESULT_LIMIT = 8000


def _trim_marker(dropped: int) -> str:
    return (
        f"\n\n[... {dropped:,} characters trimmed to fit the tool result ...]"
    )


def _dropped_marker(size: int) -> str:
    return f"[dropped to fit the tool result: {size:,} characters]"


def _entries_marker(dropped: int) -> str:
    return f"[... {dropped:,} more entries omitted to fit the tool result ...]"


def _serialized_len(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str, ensure_ascii=False))
    except Exception:
        return len(str(value))


def _shrink_field(value: Any, target: int) -> Any:
    """Shrink one field to at most ``target`` serialized characters.

    Three shapes, three honest answers:

    - A **string** keeps its head and says how much was cut.
    - A **list** keeps the leading entries that fit and appends a marker
      counting the rest — whole entries only. This is how every paginated API
      answers the question, and it is what keeps a diagnostic listing useful
      instead of absent.
    - A **mapping** is replaced whole. Half an object is the thing this module
      exists not to hand a model: there is no way to say which half survived,
      and a partial line-item map reads as the complete breakdown.
    """
    if isinstance(value, str):
        keep = target - len(_trim_marker(len(value))) - 2  # 2 for the quotes
        if keep <= 0:
            return _dropped_marker(len(value))
        return value[:keep] + _trim_marker(len(value) - keep)

    if isinstance(value, list):
        kept: list = []
        used = 2  # the brackets
        for i, item in enumerate(value):
            cost = _serialized_len(item) + 2  # the item and its separator
            if used + cost > target:
                remaining = len(value) - i
                marker = _entries_marker(remaining)
                # Only worth saying if the marker itself fits; otherwise the
                # caller's verification pass drops the field outright.
                if used + len(marker) + 4 <= target:
                    kept.append(marker)
                return kept
            kept.append(item)
            used += cost
        return kept

    return _dropped_marker(_serialized_len(value))


#: A field allocated less than this cannot say anything useful — the marker
#: explaining the cut would cost more than the value. Such fields are dropped
#: whole and counted, which keeps the fields that DO fit intact.
_MIN_USEFUL_FIELD = 80

#: Key under which a fitted mapping reports the fields it had to drop. Leading
#: underscore so it cannot collide with a payload key.
_OMITTED_KEY = "_fields_omitted"


def _allocate(sizes: dict, keys: list, available: int) -> dict:
    """Max-min fair shares of ``available`` across ``keys``.

    Smallest first: each field may keep up to an equal share of what is left,
    and whatever a small field does not need raises the share for the larger
    ones.
    """
    allocation: dict = {}
    remaining = available
    left = len(keys)
    for key in sorted(keys, key=lambda k: sizes[k]):
        share = remaining // left if left else 0
        allocation[key] = min(sizes[key], share)
        remaining -= allocation[key]
        left -= 1
    return allocation


def _fit_mapping(payload: dict, limit: int) -> dict:
    """Budget a dict's fields so the whole serializes inside ``limit``.

    Allocation is max-min fair and applied in a single pass, deliberately. An
    iterative largest-first trim re-trims the same field on a later pass and
    reports that second cut against the length of the first cut — so the
    payload claims it dropped 900 characters when it dropped 40,000. Every
    marker here is measured against the original value.

    When a payload has more fields than the budget can say anything about, the
    fields are dropped whole from the end and counted. Spreading a 2,000
    character budget across 120 fields gives each one six characters, which
    buys 120 markers saying nothing and no data at all — the shape that made
    the first version of this function return an error instead of a result.
    """
    sizes = {k: _serialized_len(v) for k, v in payload.items()}
    keys = list(payload)
    omitted: list[str] = []

    while keys:
        skeleton = len(json.dumps({k: "" for k in keys}, ensure_ascii=False))
        available = max(limit - skeleton, 0)
        allocation = _allocate(sizes, keys, available)
        starved = [
            k for k in keys
            if allocation[k] < min(sizes[k], _MIN_USEFUL_FIELD)
        ]
        if not starved or len(keys) == 1:
            break
        # Drop from the end: a payload's leading fields are its identifying
        # ones (status, agent_key, error), authored in that order.
        omitted.append(keys.pop())

    fitted = {
        k: (payload[k] if sizes[k] <= allocation[k]
            else _shrink_field(payload[k], allocation[k]))
        for k in keys
    }
    if omitted:
        fitted[_OMITTED_KEY] = (
            f"{len(omitted)} field(s) too large to return: "
            f"{', '.join(sorted(omitted)[:12])}"
        )
    return fitted


def _safe_dump(payload: Any, limit: int = _TOOL_RESULT_LIMIT) -> str:
    """Serialize a tool result into JSON that always parses.

    This used to slice the serialized string: ``json.dumps(payload)[:8000]``.
    Anything larger reached the model as JSON cut mid-token — a rate with its
    last digits missing, a line-item array with no closing bracket — and the
    model read it as fact rather than as damage. A 375-row costing payload
    crosses 8,000 characters easily, so this was the ordinary case for exactly
    the results that matter most.

    Two properties, both of which the sliced version failed and one of which a
    naive rewrite fails just as badly:

    1. **It always parses.** Trimming happens per field, and a marker says what
       was cut, so an incomplete result announces itself.
    2. **It never returns nothing when it could return something.** Refusing to
       answer because the payload was four characters over is a worse failure
       than the one being fixed: the Master loses the diagnostic entirely and
       then troubleshoots blind.
    """
    try:
        text = json.dumps(payload, default=str, ensure_ascii=False)
        if len(text) <= limit:
            return text
    except Exception:
        return json.dumps(
            {"status": "failed", "error": "Result could not be serialized."}
        )

    # Verification, not trust: JSON escaping can cost more than the raw
    # characters an allocation counted, so measure the result and shrink again
    # against the real overshoot rather than assuming the arithmetic held.
    target = limit
    for _ in range(6):
        if isinstance(payload, dict):
            fitted: Any = _fit_mapping(payload, target)
        elif isinstance(payload, list):
            fitted = _shrink_field(payload, target)
        else:
            fitted = _shrink_field(text, target)
        try:
            rendered = json.dumps(fitted, default=str, ensure_ascii=False)
        except Exception:
            break
        if len(rendered) <= limit:
            return rendered
        # Escaping inflates roughly in proportion, so scale the target by how
        # far over we landed. Subtracting the raw overshoot instead collapses a
        # heavily-escaped payload to a fraction of the room it actually had.
        scaled = int(target * limit / len(rendered))
        target = min(scaled, target - 1)
        if target <= 0:
            break

    # Last resort: the identifying scalars, so the Master still learns which
    # call this was and whether it succeeded.
    minimal: dict = {"status": "truncated"}
    if isinstance(payload, dict):
        for key in ("status", "agent_key", "tender_id", "session_id"):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)) and len(str(value)) <= 200:
                minimal[key] = value
    minimal["error"] = (
        "The result was too large to return and could not be trimmed to fit."
    )
    return json.dumps(minimal, default=str, ensure_ascii=False)


def _log_audit(user_id: Optional[int], tool_name: str, args: dict, outcome: str) -> None:
    """Write an AuditLog row for master-agent action tool invocations."""
    try:
        from app.models.audit_log import AuditLog
        with _fresh_session() as db:
            row = AuditLog(
                user_id=user_id,
                action=f"decision_maker.{tool_name}",
                resource_type=args.get("resource_type") or "tender",
                resource_id=str(args.get("tender_id") or args.get("item_id") or ""),
                details={"args": args, "outcome": outcome, "source": "decision_maker"},
            )
            db.add(row)
            db.commit()
    except Exception as e:
        logger.debug(f"AuditLog write failed (non-fatal): {e}")


# ────────────────────────────────────────────────────────────────────────────
# Family A: Agent-as-tool wrappers
# ────────────────────────────────────────────────────────────────────────────

class _AgentCallInput(BaseModel):
    message: str = Field(..., description="The natural-language instruction to pass to the sub-agent")
    tender_id: Optional[int] = Field(None, description="Tender ID context (optional)")
    extra_tools: Optional[list[str]] = Field(
        None,
        description=(
            "Optional tool keys to grant this worker for THIS CALL ONLY, on top "
            "of what it normally runs with. Use when a task needs a capability "
            "the worker would not otherwise reach — e.g. giving a writer "
            "web_search for one fact-check. Leave empty unless the task needs "
            "it; workers already reach the shared tool repo by default."
        ),
    )


def _validate_extra_tools(agent_name: str, requested: Optional[list[str]]) -> list[str]:
    """Validate tool keys the master granted a worker for one call.

    Unknown keys are dropped and logged rather than passed on: an LLM inventing
    a tool name should not silently become "the worker got nothing extra", and
    it must not reach the loader as a key that quietly resolves to nothing.
    """
    if not requested:
        return []
    from app.services.langchain.tools.tool_loader import get_available_tool_keys

    available = set(get_available_tool_keys())
    granted = [k for k in requested if k in available]
    unknown = [k for k in requested if k not in available]
    if unknown:
        logger.warning(
            "Master granted %s unknown tool(s) %s — ignored. Available: %d keys.",
            agent_name, unknown, len(available),
        )
    if granted:
        logger.info("Master granted %s extra tools for one call: %s", agent_name, granted)
    return granted


def _worker_output_budget() -> int:
    """How much of one worker's answer the Master receives inline."""
    from app.core.config import get_settings

    return int(get_settings().master_worker_output_max_chars)


#: Serialized characters of ``structured_data`` a worker may return alongside
#: its prose. Given its own budget rather than left to the generic trimmer:
#: that trims the largest field, and for a costing the largest field is the
#: line items — the one part of the answer the user most wants back.
_STRUCTURED_DATA_BUDGET = 12000


def _worker_payload_limit() -> int:
    """Ceiling on the whole serialized worker result."""
    return _worker_output_budget() + _STRUCTURED_DATA_BUDGET + 4000


def worker_artifacts(result: Any) -> list[dict]:
    """The downloadable files a worker's result carries, as artifact cards.

    Today that is the costing worker's ``xlsx_artifact`` (see
    `chat_costing_research`); a worker that starts attaching another file
    adds it here and both the Master's payload and the live
    `artifact_created` event pick it up.
    """
    if not isinstance(result, dict):
        return []
    out: list[dict] = []
    xlsx = result.get("xlsx_artifact")
    if isinstance(xlsx, dict) and xlsx.get("artifact_id"):
        card = {
            "artifact_id": xlsx["artifact_id"],
            "artifact_type": xlsx.get("artifact_type", "cost_breakdown_xlsx"),
            "title": xlsx.get("title", "Cost Breakdown"),
            "version": xlsx.get("version", 1),
        }
        if xlsx.get("file_name"):
            card["file_name"] = xlsx["file_name"]
        if result.get("cost_breakdown_id") is not None:
            card["cost_breakdown_id"] = result["cost_breakdown_id"]
        out.append(card)
    return out


def announce_worker_artifacts(result: Any, stream_callback: Optional[Callable]) -> None:
    """Emit `artifact_created` for each file a worker attached.

    The streaming handler emits this event for the router path, and the
    Master's plan-execution path mirrors it; the Master's ReAct path -- the
    default `chat_engine` -- emitted nothing, so the workbook the costing
    worker had just written appeared in the artifacts pane only after the
    next refresh, if the user thought to look.
    """
    if not stream_callback:
        return
    for card in worker_artifacts(result):
        try:
            stream_callback("artifact_created", card)
        except Exception:
            pass


def _worker_result_payload(
    agent_key: str,
    result: dict,
    store: Optional[WorkerOutputStore],
) -> dict:
    """The delegation result as the Master sees it.

    The field used to be ``output_preview`` and it was ``result["output"][:1500]``.
    A forensic tender analysis, an item-wise costing narrative, an eligibility
    breakdown — all of them run to tens of thousands of characters, and all of
    them reached the Master as the first page and nothing else. The Master's
    message is what the user reads, so the platform did the work correctly and
    then threw away everything past the opening paragraphs. That is the whole
    of "the agent is not providing correct or detailed output".

    The budget is real — a worker answer cannot be allowed to consume the
    Master's context on its own — but it is now large enough to carry a whole
    answer in the ordinary case, the field is named ``output`` rather than
    ``preview`` so the model is not told a stub is all there is, and when the
    budget does bite the rest is retrievable rather than gone.
    """
    full = result.get("output") or ""
    budget = _worker_output_budget()
    payload = {
        "status": result.get("status", "completed"),
        "agent_key": agent_key,
        "output_type": result.get("output_type"),
        "output": full[:budget],
        "output_total_chars": len(full),
        "output_complete": len(full) <= budget,
        "structured_data": result.get("structured_data"),
    }

    # A worker that produced a FILE has already delivered it. The costing
    # worker attaches its Excel workbook as `xlsx_artifact`; this used to be
    # dropped here, so the Master -- told nothing of it -- ended a finished
    # costing by offering to "generate the Excel workbook" it already had.
    artifacts = worker_artifacts(result)
    if artifacts:
        payload["artifacts"] = artifacts
        payload["artifacts_note"] = (
            "These files are ALREADY produced and attached to this chat as "
            "downloadable artifacts. Tell the user they are attached (by "
            "title). Never offer to generate, create or export them again."
        )

    structured = result.get("structured_data")
    if structured is not None:
        size = _serialized_len(structured)
        if size > _STRUCTURED_DATA_BUDGET:
            # No partial structure. Half a line-item array is worse than none:
            # the model would read the surviving rows as the whole breakdown.
            payload["structured_data"] = None
            payload["structured_data_omitted_chars"] = size
            payload["structured_data_note"] = (
                f"{size:,} characters of structured data were too large to "
                "return inline. The worker persisted it — read it back with "
                "the capability for that artifact type (cost_breakdown_read "
                "for a costing, for example) if you need the individual rows. "
                "Do not state or estimate figures you have not read."
            )
    if not payload["output_complete"] and store is None:
        # No store on this path (a caller that binds no reader). Say so, rather
        # than leaving the model to invent a handle for a tool it does not have.
        payload["how_to_continue"] = (
            f"This output stops at character {budget:,} of {len(full):,}, and "
            "the remainder is not retrievable from this run. Answer from what "
            "you have and say the specialist's output was longer than you "
            "could read."
        )
    elif not payload["output_complete"]:
        handle = store.put(agent_key, full)
        payload["output_handle"] = handle
        payload["next_offset"] = budget
        payload["how_to_continue"] = (
            f"This output stops at character {budget:,} of "
            f"{len(full):,}. Call read_worker_output(handle=\"{handle}\", "
            f"offset={budget}) for the rest before writing your answer."
        )
    return payload


class _ReadWorkerOutputInput(BaseModel):
    handle: str = Field(
        ..., description="The output_handle from the worker result you want to continue reading"
    )
    offset: int = Field(
        0, description="Character offset to read from — use next_offset from the previous read"
    )
    length: Optional[int] = Field(
        None, description="How many characters to read (defaults to one full budget)"
    )


def build_worker_output_reader(store: WorkerOutputStore) -> BaseTool:
    """The rest of a worker answer that did not fit in the delegation result.

    Bound only on the Master's surface and closed over this run's store, the
    same way ``propose_plan`` and ``ask_user`` are closed over their captures.
    """

    class _ReadWorkerOutput(BaseTool):
        name: str = "read_worker_output"
        description: str = (
            "Read the remainder of a worker agent's output that was too long to "
            "return whole. Pass the output_handle from that worker's result and "
            "the offset to continue from. Call it until output_complete is true "
            "whenever the answer you owe the user depends on what comes after "
            "the part you were handed."
        )
        args_schema: Type[BaseModel] = _ReadWorkerOutputInput

        class Config:
            arbitrary_types_allowed = True

        def _read(self, handle: str, offset: int = 0, length: Optional[int] = None) -> str:
            text = store.get(handle)
            if text is None:
                return _safe_dump({
                    "status": "failed",
                    "error": (
                        f"No stored output for handle {handle!r}. Known handles: "
                        f"{sorted(store) or 'none'}."
                    ),
                })
            span = int(length or _worker_output_budget())
            start = max(int(offset), 0)
            chunk = text[start:start + span]
            end = start + len(chunk)
            return _safe_dump(
                {
                    "status": "completed",
                    "handle": handle,
                    "offset": start,
                    "next_offset": end,
                    "total_chars": len(text),
                    "output_complete": end >= len(text),
                    "output": chunk,
                },
                limit=_worker_payload_limit(),
            )

        def _run(self, handle: str, offset: int = 0, length: Optional[int] = None) -> str:
            return self._read(handle, offset, length)

        async def _arun(self, handle: str, offset: int = 0, length: Optional[int] = None) -> str:
            return self._read(handle, offset, length)

    return _ReadWorkerOutput()


def _build_agent_call_tool(
    name: str,
    display_name: str,
    description: str,
    handler_import_path: str,  # e.g. "chat_agent_wrappers.chat_document_analysis"
    session_id: Optional[str],
    proposal_session_id: Optional[int],
    user_id: Optional[int],
    conversation_history: Optional[list[dict]],
    file_metadata: Optional[dict],
    stream_callback: Optional[Callable],
    output_store: Optional[WorkerOutputStore] = None,
) -> BaseTool:
    """Build a single agent-call tool bound to request context."""
    # Tool names must be [a-zA-Z0-9_-]{1,64}. Admin agent_keys are slugs and
    # may contain hyphens; normalise so a key like "doc-letter-writer" cannot
    # produce a name the provider rejects, and truncate rather than 400.
    _safe = _re.sub(r"[^a-zA-Z0-9_]", "_", name)[:58]
    _tool_name = f"call_{_safe}"
    _tool_description = description

    class _T(BaseTool):
        name: str = _tool_name
        description: str = _tool_description
        args_schema: Type[BaseModel] = _AgentCallInput

        class Config:
            arbitrary_types_allowed = True

        def _run(self, message: str, tender_id: Optional[int] = None,
                 extra_tools: Optional[list[str]] = None) -> str:
            return asyncio.run(
                self._arun(message=message, tender_id=tender_id, extra_tools=extra_tools)
            )

        async def _arun(self, message: str, tender_id: Optional[int] = None,
                        extra_tools: Optional[list[str]] = None) -> str:
            # Tools the master granted for this one call. Validated against the
            # repo so a hallucinated key is reported rather than silently
            # dropped, and passed through the same write gate as any other tool.
            granted = _validate_extra_tools(name, extra_tools)
            from app.services.langchain.graphs import chat_agent_wrappers as cw

            handler = (
                getattr(cw, handler_import_path, None) if handler_import_path else None
            )
            if handler is None and handler_import_path:
                return _safe_dump(
                    {"status": "failed", "error": f"Unknown handler: {handler_import_path}"}
                )

            if handler is None:
                # An agent registered in the admin section with no purpose-built
                # chat wrapper. Run it through the generic executor so admins can
                # add a worker without a code change — the whole point of driving
                # the roster from the registry.
                if stream_callback:
                    try:
                        stream_callback("sub_agent_start", {
                            "agent_key": name,
                            "display_name": display_name,
                            "message_preview": (message or "")[:200],
                        })
                    except Exception:
                        pass
                from app.services.agent_execution_service import execute_agent

                gdb = _fresh_session()
                try:
                    result = await execute_agent(
                        db=gdb,
                        agent_key_or_id=name,
                        input_data={
                            "message": message,
                            "tender_id": tender_id,
                            "_extra_tool_keys": granted,
                        },
                        user_id=user_id,
                        session_id=session_id,
                    )
                    _log_audit(user_id, f"call_{name}", {"message": message[:200]}, "ok")
                    return _safe_dump(
                        _worker_result_payload(name, result, output_store),
                        limit=_worker_payload_limit(),
                    )
                except Exception as e:
                    logger.error("Generic agent call %s failed: %s", name, e, exc_info=True)
                    _log_audit(user_id, f"call_{name}", {"message": message[:200]}, "failed")
                    return _safe_dump({"status": "failed", "error": str(e)})
                finally:
                    gdb.close()

            if stream_callback:
                try:
                    stream_callback("sub_agent_start", {
                        "agent_key": name,
                        "display_name": display_name,
                        "message_preview": (message or "")[:200],
                    })
                except Exception:
                    pass

            db = _fresh_session()
            try:
                try:
                    result = await handler(
                        db=db,
                        message=message,
                        tender_id=tender_id,
                        session_id=session_id,
                        conversation_history=conversation_history or [],
                        file_metadata=file_metadata or {},
                        proposal_session_id=proposal_session_id,
                    )
                    payload = _worker_result_payload(
                        name, result, output_store
                    )
                    announce_worker_artifacts(result, stream_callback)
                    if stream_callback:
                        try:
                            stream_callback("sub_agent_end", {
                                "agent_key": name,
                                "status": payload["status"],
                                "output_preview": payload["output"][:500],
                            })
                        except Exception:
                            pass
                    return _safe_dump(
                        payload, limit=_worker_payload_limit()
                    )
                except Exception as e:
                    logger.error(f"[decision_maker] Sub-agent {name} failed: {e}")
                    return _safe_dump({"status": "failed", "agent_key": name, "error": str(e)[:500]})
            finally:
                try:
                    db.close()
                except Exception:
                    pass

    return _T()


class _PlanStepInput(BaseModel):
    description: str = Field(..., description="What this step will accomplish in one sentence")
    agent: Optional[str] = Field(
        None,
        description=(
            "Which specialized agent will handle it (deep_analyzer, checklist_generator, "
            "proposal_creator, costing_researcher, annexure_finder, workspace_manager), "
            "or null if a direct LLM / diagnostic / action tool will be used."
        ),
    )
    rationale: Optional[str] = Field(None, description="Why this step is needed (optional)")


class _ProposePlanInput(BaseModel):
    title: str = Field(..., description="Short human-readable title for the overall plan (under 80 chars)")
    steps: list[_PlanStepInput] = Field(..., description="Ordered list of steps you intend to execute")
    reasoning: str = Field(..., description="Brief paragraph explaining the overall approach")


def build_plan_proposal_tool(
    capture: PlanCapture,
    stream_callback: Optional[Callable],
) -> BaseTool:
    """Build the `propose_plan` tool — the ONLY tool available in planning mode.

    When invoked, it writes the structured plan into ``capture`` and emits a
    ``plan_proposed`` SSE event via ``stream_callback``. Returns a simple
    acknowledgement string so the LLM produces a final answer and stops;
    after the ReAct loop finishes, ``run_decision_maker`` inspects ``capture``
    to decide whether to enter pending-plan state.
    """

    class _ProposePlanTool(BaseTool):
        name: str = "propose_plan"
        description: str = (
            "Propose a step-by-step plan for handling the user's request. This MUST be "
            "the first and only tool you call in planning mode. Provide a short title, "
            "an ordered list of steps (each naming the agent or action that will handle it), "
            "and a short reasoning paragraph. After calling this tool, respond with a single "
            "short sentence confirming the plan was submitted (e.g. 'Plan submitted for "
            "approval.') and STOP. Do not call any more tools."
        )
        args_schema: Type[BaseModel] = _ProposePlanInput

        class Config:
            arbitrary_types_allowed = True

        def _run(self, title: str, steps: list, reasoning: str) -> str:  # type: ignore[override]
            return asyncio.run(self._arun(title=title, steps=steps, reasoning=reasoning))

        async def _arun(self, title: str, steps: list, reasoning: str) -> str:  # type: ignore[override]
            # steps may arrive as list[dict] OR list[_PlanStepInput] depending on validator behaviour
            norm_steps: list[dict] = []
            for s in (steps or []):
                if isinstance(s, dict):
                    norm_steps.append({
                        "description": str(s.get("description") or "").strip(),
                        "agent": s.get("agent") or None,
                        "rationale": s.get("rationale") or None,
                    })
                else:
                    norm_steps.append({
                        "description": str(getattr(s, "description", "") or "").strip(),
                        "agent": getattr(s, "agent", None),
                        "rationale": getattr(s, "rationale", None),
                    })
            norm_steps = [s for s in norm_steps if s["description"]]
            plan = {
                "title": (title or "").strip()[:200],
                "steps": norm_steps,
                "reasoning": (reasoning or "").strip(),
            }
            # Record for the outer run loop.
            capture["plan"] = plan
            if stream_callback:
                try:
                    stream_callback("plan_proposed", plan)
                except Exception:
                    pass
            return (
                "Plan submitted. Respond with a single confirmation sentence "
                "and do not call any more tools."
            )

    return _ProposePlanTool()


class _AskUserInput(BaseModel):
    question: str = Field(
        ...,
        description=(
            "The specific question to ask the user. Ask only when genuinely "
            "blocked by missing information you cannot derive from the tender, "
            "the attached files, or your tools."
        ),
    )


def build_ask_user_tool(
    capture: "AskUserCapture",
    stream_callback: Optional[Callable],
) -> BaseTool:
    """Build the `ask_user` escape-hatch tool for autonomous mode.

    The autonomous Master Agent executes without an approval gate. This is its
    ONLY way to pause: when it is truly blocked by missing information it cannot
    derive, it asks one focused question and stops. The question is captured so
    ``run_decision_maker`` can return ``needs_input``. It is NOT for routine
    approval — the agent should act, not ask permission.
    """

    class _AskUserTool(BaseTool):
        name: str = "ask_user"
        description: str = (
            "Ask the user ONE focused question — use ONLY when you are genuinely "
            "blocked by missing information you cannot derive from the tender, the "
            "attached files, or your other tools (e.g. a required business decision "
            "or a value that exists nowhere in the inputs). Do NOT use this to ask "
            "for approval of a plan or permission to proceed — you are autonomous; "
            "act first. After calling it, respond with a single sentence and STOP."
        )
        args_schema: Type[BaseModel] = _AskUserInput

        class Config:
            arbitrary_types_allowed = True

        def _run(self, question: str) -> str:  # type: ignore[override]
            return asyncio.run(self._arun(question=question))

        async def _arun(self, question: str) -> str:  # type: ignore[override]
            q = (question or "").strip()
            capture["question"] = q
            if stream_callback:
                try:
                    stream_callback("ask_user", {"question": q})
                except Exception:
                    pass
            return (
                "Question recorded. Respond with a single sentence and do not "
                "call any more tools."
            )

    return _AskUserTool()


#: Worker agents that have a purpose-built chat handler in
#: `chat_agent_wrappers`. Anything registered in the admin section that is NOT
#: listed here is still callable — it runs through the generic
#: `execute_agent` path instead.
_BUILTIN_AGENT_HANDLERS: dict[str, str] = {
    "deep_analyzer": "chat_document_analysis",
    "tender_doc_analyzer": "chat_document_analysis",
    "checklist_generator": "chat_checklist_generation",
    "proposal_creator": "chat_proposal_writing",
    "costing_researcher": "chat_costing_research",
    "annexure_finder": "chat_annexure_finder",
    "workspace_manager": "chat_workspace_operations",
}

#: Agents that ARE the master, or exist only to route to it. The master must
#: never be able to call itself — that is an unbounded recursion with a
#: per-call price tag.
#: Legacy alias rows in `custom_agents` that name the same specialist. Exposing
#: all of them gave the master 22 call_* tools with four near-identical
#: analyzer descriptions — duplicate tools are how a model picks the wrong one.
#: The roster collapses each alias into its canonical worker.
WORKER_ALIASES: dict[str, str] = {
    "tender_doc_analyzer": "deep_analyzer",
    "document_analyzer": "deep_analyzer",
    "tender_analysis": "deep_analyzer",
    "checklist": "checklist_generator",
    "proposal": "proposal_creator",
}

_NON_WORKER_AGENT_KEYS = {
    "decision_maker",
    "proposal_router",
    # general_assistant is deliberately NOT here: under chat_engine=master it
    # is the cheap worker the Master delegates ordinary conversational and
    # research work to instead of answering on the flagship model. Its
    # self-call guard lives in its own _build_tools, which excludes
    # call_general_assistant from its own belt.
}

#: System rows that exist in `custom_agents` for Agent Builder visibility but
#: are not chat workers. Every enabled row used to become a `call_*` tool, so
#: the Master carried ~19 tool schemas on every step -- the scoring stubs
#: (called by the ingest pipeline, never from chat), a pipeline stage
#: (`costing_scope_extractor`, internal to the costing worker), an orchestrator
#: row with no handler (`tender_pipeline`, one generic call under a four-step
#: description), the RunPod probe (a text-only 7B model behind a 152 s cold
#: start, documented as unreachable from chat), and the workspace document
#: writers (reached through the workspace tools; `doc-costing-analyst` would
#: otherwise price a BOQ with none of the costing worker's evidence rules).
#: An agent an admin creates is still callable without a code change.
_PIPELINE_ONLY_AGENT_KEYS = frozenset({
    "classifier",
    "relevance",
    "risk",
    "summary",
    "eligibility",
    "tender_scorer",
    "scoring_digest",
    "costing_scope_extractor",
    "tender_pipeline",
    "local_model_probe",
    "doc-letter-writer",
    "doc-technical-writer",
    "doc-costing-analyst",
    "doc-compliance-writer",
})

#: Fallback descriptions for built-in workers whose admin row has no
#: description filled in. The LLM picks tools by description, so an empty one
#: means that worker effectively does not exist.
_BUILTIN_AGENT_DESCRIPTIONS: dict[str, str] = {
    "deep_analyzer": (
        "Perform a forensic 7-section analysis of a tender (requirements, eligibility, "
        "rejection risks, missing items, regulatory intelligence, next steps). Pass "
        "tender_id if known, or leave None when the user has uploaded files."
    ),
    "tender_doc_analyzer": (
        "Analyse a tender's documents and extract its requirements, eligibility rules "
        "and risks."
    ),
    "checklist_generator": (
        "Generate a submission checklist of every required document for a tender."
    ),
    "proposal_creator": (
        "Draft proposal documents, cover letters, declarations, or compliance certificates."
    ),
    "costing_researcher": (
        "Research market rates and produce a detailed cost breakdown with GST."
    ),
    "annexure_finder": (
        "Extract every annexure / schedule / proforma / appendix / declaration from a "
        "tender's PDFs and materialize each as an editable workspace document."
    ),
    "workspace_manager": (
        "Manage the canvas workspace: initialize, list items, check progress, or generate "
        "a specific named document."
    ),
}


def list_worker_agents(db: Session) -> list[dict]:
    """The worker agents the master is allowed to call, from the admin registry.

    Source of truth is the `custom_agents` table — the same rows the admin
    Agent Builder shows. An agent that is disabled there is not callable here,
    and an agent added there becomes callable without a code change.

    Returns dicts of ``{agent_key, display_name, description, handler}``, where
    ``handler`` is the chat-wrapper attribute name or None (generic path).
    """
    rows = (
        db.query(CustomAgent)
        .filter(CustomAgent.is_enabled.is_(True))
        .order_by(CustomAgent.display_name)
        .all()
    )
    workers: list[dict] = []
    seen_canonical: set[str] = set()
    # Canonical rows first, so an alias never shadows the real worker when
    # both exist; aliases only fill in when their canonical row is absent.
    rows.sort(key=lambda r: r.agent_key in WORKER_ALIASES)
    for row in rows:
        key = row.agent_key
        if key in _NON_WORKER_AGENT_KEYS or key in _PIPELINE_ONLY_AGENT_KEYS:
            continue
        canonical = WORKER_ALIASES.get(key, key)
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)
        if key != canonical:
            # An alias row with no canonical row: expose it under the
            # canonical name so the master sees one stable identity.
            key = canonical
        description = (
            (row.description or "").strip()
            or _BUILTIN_AGENT_DESCRIPTIONS.get(key, "")
            or f"Run the {row.display_name} agent."
        )
        workers.append(
            {
                "agent_key": key,
                "display_name": row.display_name or key,
                "description": description,
                "handler": _BUILTIN_AGENT_HANDLERS.get(key),
            }
        )
    return workers


def build_agent_wrapper_tools(
    session_id: Optional[str],
    proposal_session_id: Optional[int],
    user_id: Optional[int],
    conversation_history: Optional[list[dict]] = None,
    file_metadata: Optional[dict] = None,
    stream_callback: Optional[Callable] = None,
    db: Optional[Session] = None,
    output_store: Optional[WorkerOutputStore] = None,
) -> list[BaseTool]:
    """Family A — one `call_<agent_key>` tool per enabled admin-registered agent.

    Previously this was a hardcoded list of six. That meant an agent added in
    the admin section was invisible to the master agent, and an agent disabled
    there was still callable — the admin UI and the master's actual
    capabilities had no relationship to each other.

    `db` is optional so existing callers keep working; without it we fall back
    to the built-in roster rather than returning nothing, because a master
    agent with no workers is worse than one with a stale list.

    What comes back is workers and nothing else — the Agent Builder enumerates
    this list as the assignable roster. A caller that wants the remainder of an
    over-budget worker answer passes its own `output_store` and binds
    `build_worker_output_reader` over the same one; a reader wired to a
    different store reports every handle as unknown. See `build_master_catalog`.
    """
    own_db = None
    try:
        if db is None:
            own_db = _fresh_session()
            db = own_db
        try:
            workers = list_worker_agents(db)
        except Exception as e:
            logger.warning(
                "Could not read the agent registry (%s); falling back to the "
                "built-in worker roster.", e
            )
            workers = [
                {
                    "agent_key": key,
                    "display_name": key.replace("_", " ").title(),
                    "description": _BUILTIN_AGENT_DESCRIPTIONS[key],
                    "handler": handler,
                }
                for key, handler in _BUILTIN_AGENT_HANDLERS.items()
                if key != "tender_doc_analyzer"  # duplicate of deep_analyzer
            ]

        # No substitute store when the caller passed none. A store implies a
        # reader bound over it; handing the generalist a handle for
        # `read_worker_output` — a tool it does not have — sends it into a
        # tool-not-found error instead of the honest "not retrievable here"
        # that `_worker_result_payload` writes when there is no store.
        store = output_store
        return [
            _build_agent_call_tool(
                name=w["agent_key"],
                display_name=w["display_name"],
                description=w["description"],
                handler_import_path=w["handler"],
                session_id=session_id,
                proposal_session_id=proposal_session_id,
                user_id=user_id,
                conversation_history=conversation_history,
                file_metadata=file_metadata,
                stream_callback=stream_callback,
                output_store=store,
            )
            for w in workers
        ]
    finally:
        if own_db is not None:
            own_db.close()


# ────────────────────────────────────────────────────────────────────────────
# Family B: Diagnostic / read-only platform tools
# ────────────────────────────────────────────────────────────────────────────

class _TenderIdInput(BaseModel):
    tender_id: int = Field(..., description="The tender ID to inspect")


class _SessionIdInput(BaseModel):
    session_id: int = Field(..., description="The proposal_session_id (numeric, Command Center session)")


class _RecentErrorsInput(BaseModel):
    tender_id: Optional[int] = Field(None)
    session_id: Optional[int] = Field(None, description="proposal_session_id")
    limit: int = Field(20, description="Max rows to return")


def build_diagnostic_tools(user_id: Optional[int]) -> list[BaseTool]:
    """Family B — read-only inspection tools. Safe to call anytime."""

    class InspectWorkspaceTool(BaseTool):
        name: str = "inspect_workspace"
        description: str = (
            "Read-only: return the current state of a tender's canvas workspace — "
            "total items, review statuses, orphaned rows, missing checklist items. "
            "Call this FIRST when the user reports workspace trouble."
        )
        args_schema: Type[BaseModel] = _TenderIdInput

        def _run(self, tender_id: int) -> str:
            db = _fresh_session()
            try:
                from app.models.workspace import DocumentWorkspace
                from app.models.checklist import ChecklistItem

                items = db.query(ChecklistItem).filter(
                    ChecklistItem.tender_id == tender_id,
                ).all()
                workspaces = db.query(DocumentWorkspace).filter(
                    DocumentWorkspace.tender_id == tender_id,
                ).all()
                ws_by_item = {w.checklist_item_id: w for w in workspaces if w.checklist_item_id}
                missing_ws = [i.id for i in items if i.id not in ws_by_item]
                orphan_ws = [w.id for w in workspaces if w.checklist_item_id and w.checklist_item_id not in {i.id for i in items}]

                return _safe_dump({
                    "tender_id": tender_id,
                    "checklist_item_count": len(items),
                    "workspace_row_count": len(workspaces),
                    "items_missing_workspace": missing_ws[:25],
                    "orphan_workspace_rows": orphan_ws[:25],
                    "review_status_counts": {
                        s: sum(1 for w in workspaces if (w.review_status or "none") == s)
                        for s in {w.review_status or "none" for w in workspaces} or {"none"}
                    },
                    "needs_checklist": len(items) == 0,
                })
            finally:
                db.close()

    class InspectTenderTool(BaseTool):
        name: str = "inspect_tender"
        description: str = (
            "Read-only: return tender metadata, document list, analysis status, "
            "and artifact count. Useful to verify a tender exists and has docs "
            "before calling a writer agent."
        )
        args_schema: Type[BaseModel] = _TenderIdInput

        def _run(self, tender_id: int) -> str:
            db = _fresh_session()
            try:
                from app.models.tender import Tender, TenderDocument
                from app.models.checklist import ChecklistItem
                t = db.query(Tender).filter(Tender.id == tender_id).first()
                if not t:
                    return _safe_dump({"error": "tender_not_found", "tender_id": tender_id})
                docs = db.query(TenderDocument).filter(TenderDocument.tender_id == tender_id).all()
                checklist_count = db.query(ChecklistItem).filter(ChecklistItem.tender_id == tender_id).count()
                return _safe_dump({
                    "tender_id": tender_id,
                    "title": getattr(t, "title", None),
                    "workflow_status": getattr(t, "workflow_status", None),
                    "ai_category": getattr(t, "ai_category", None),
                    "document_count": len(docs),
                    "documents": [{"id": d.id, "name": d.file_name} for d in docs[:25]],
                    "checklist_item_count": checklist_count,
                    "has_analysis": bool(getattr(t, "ai_summary", None)),
                })
            finally:
                db.close()

    class InspectSessionTool(BaseTool):
        name: str = "inspect_session"
        description: str = (
            "Read-only: list artifacts and last agent runs for a Command Center "
            "session (proposal_session_id). Use when the user references 'this "
            "session' or prior work that might have gone wrong."
        )
        args_schema: Type[BaseModel] = _SessionIdInput

        def _run(self, session_id: int) -> str:
            db = _fresh_session()
            try:
                from app.core.ownership import scoped_query
                from app.models.agent_run import AgentRun
                from app.models.proposal import ProposalSession
                from app.services.artifact_service import list_artifacts

                # A session is somebody's work: another user's session is
                # reported exactly like one that does not exist.
                actor = _acting_for(user_id)
                owned = scoped_query(db, ProposalSession, actor).filter(
                    ProposalSession.id == session_id
                ).first()
                if owned is None:
                    return _safe_dump({"error": "session_not_found", "session_id": session_id})
                artifacts = list_artifacts(db, session_id)
                runs = (
                    scoped_query(db, AgentRun, actor)
                    .filter(AgentRun.proposal_session_id == session_id)
                    .order_by(AgentRun.created_at.desc())
                    .limit(10)
                    .all()
                )
                return _safe_dump({
                    "session_id": session_id,
                    "artifact_count": len(artifacts),
                    "artifacts": [
                        {"id": a.get("id"), "type": a.get("artifact_type"), "title": a.get("title"),
                         "agent_key": a.get("agent_key")}
                        for a in artifacts[:20]
                    ],
                    "recent_runs": [
                        {"id": r.id, "status": r.status, "selected_agents": r.selected_agents,
                         "error": r.error_message[:200] if r.error_message else None}
                        for r in runs
                    ],
                })
            finally:
                db.close()

    class ListRecentErrorsTool(BaseTool):
        name: str = "list_recent_errors"
        description: str = (
            "Read-only: return up to N most recent failed AgentRun rows, optionally "
            "scoped to a tender or session. Use for troubleshooting: 'why did X break?'"
        )
        args_schema: Type[BaseModel] = _RecentErrorsInput

        def _run(self, tender_id: Optional[int] = None, session_id: Optional[int] = None, limit: int = 20) -> str:
            db = _fresh_session()
            try:
                from app.core.ownership import scoped_query
                from app.models.agent_run import AgentRun
                # The caller's own failed runs (master_admin: everyone's).
                q = scoped_query(db, AgentRun, _acting_for(user_id)).filter(
                    AgentRun.status == "failed"
                )
                if tender_id is not None:
                    q = q.filter(AgentRun.tender_id == tender_id)
                if session_id is not None:
                    q = q.filter(AgentRun.proposal_session_id == session_id)
                rows = q.order_by(AgentRun.created_at.desc()).limit(max(1, min(limit, 50))).all()
                return _safe_dump([
                    {
                        "run_id": r.id,
                        "tender_id": r.tender_id,
                        "session_id": r.proposal_session_id,
                        "selected_agents": r.selected_agents,
                        "error": (r.error_message or "")[:500],
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ])
            finally:
                db.close()

    class CheckAnnexureExtractionTool(BaseTool):
        name: str = "check_annexure_extraction"
        description: str = (
            "Read-only: report on annexure extraction status for a tender — how "
            "many annexure workspace rows exist and whether the last extraction "
            "run succeeded."
        )
        args_schema: Type[BaseModel] = _TenderIdInput

        def _run(self, tender_id: int) -> str:
            db = _fresh_session()
            try:
                from app.models.checklist import ChecklistItem
                from app.models.workspace import DocumentWorkspace
                annex_items = db.query(ChecklistItem).filter(
                    ChecklistItem.tender_id == tender_id,
                    ChecklistItem.source_section.like("annexure_finder:%"),
                ).all()
                ws = db.query(DocumentWorkspace).filter(
                    DocumentWorkspace.tender_id == tender_id,
                    DocumentWorkspace.agent_key == "annexure_finder",
                ).all()
                return _safe_dump({
                    "tender_id": tender_id,
                    "annexure_checklist_items": len(annex_items),
                    "annexure_workspace_rows": len(ws),
                    "workspace_statuses": {
                        s: sum(1 for w in ws if (w.review_status or "none") == s)
                        for s in {w.review_status or "none" for w in ws} or {"none"}
                    },
                })
            finally:
                db.close()

    return [
        InspectWorkspaceTool(),
        InspectTenderTool(),
        InspectSessionTool(),
        ListRecentErrorsTool(),
        CheckAnnexureExtractionTool(),
    ]


# ────────────────────────────────────────────────────────────────────────────
# Family C: Action / repair tools
# ────────────────────────────────────────────────────────────────────────────

class _InitWorkspaceInput(BaseModel):
    tender_id: int


class _RegenerateChecklistInput(BaseModel):
    tender_id: int


class _RegenerateAnnexuresInput(BaseModel):
    tender_id: int


class _FinalizeDocumentInput(BaseModel):
    tender_id: int
    item_id: int


class _RetryRunInput(BaseModel):
    agent_run_id: str = Field(..., description="UUID of the AgentRun row to retry")


def build_action_tools(user_id: Optional[int]) -> list[BaseTool]:
    """Family C — narrowly scoped repair tools. Writes AuditLog rows."""

    class InitWorkspaceForceTool(BaseTool):
        name: str = "init_workspace_force"
        description: str = (
            "Re-runs workspace initialization for a tender. Idempotent — safe to "
            "call even if a workspace already exists; creates missing DocumentWorkspace "
            "rows without duplicating existing ones."
        )
        args_schema: Type[BaseModel] = _InitWorkspaceInput

        def _run(self, tender_id: int) -> str:
            db = _fresh_session()
            try:
                from app.services.workspace_service import init_workspace
                overview = init_workspace(db, tender_id, user_id=user_id or 0)
                _log_audit(user_id, "init_workspace_force", {"tender_id": tender_id}, "completed")
                return _safe_dump({
                    "status": "completed",
                    "tender_id": tender_id,
                    "total_items": len(overview.get("items", [])),
                    "completion_percent": overview.get("stats", {}).get("completion_percent", 0),
                })
            except Exception as e:
                _log_audit(user_id, "init_workspace_force", {"tender_id": tender_id}, f"failed: {e}")
                return _safe_dump({"status": "failed", "error": str(e)[:500]})
            finally:
                db.close()

    class RegenerateChecklistTool(BaseTool):
        name: str = "regenerate_checklist"
        description: str = (
            "Re-runs the checklist generator for a tender from its current analysis. "
            "Preserves annexure_finder-owned items."
        )
        args_schema: Type[BaseModel] = _RegenerateChecklistInput

        def _run(self, tender_id: int) -> str:
            return asyncio.run(self._arun(tender_id=tender_id))

        async def _arun(self, tender_id: int) -> str:
            db = _fresh_session()
            try:
                from app.services.checklist_service import generate_checklist
                items = await generate_checklist(db, tender_id)
                _log_audit(user_id, "regenerate_checklist", {"tender_id": tender_id}, "completed")
                return _safe_dump({"status": "completed", "tender_id": tender_id, "count": len(items)})
            except Exception as e:
                _log_audit(user_id, "regenerate_checklist", {"tender_id": tender_id}, f"failed: {e}")
                return _safe_dump({"status": "failed", "error": str(e)[:500]})
            finally:
                db.close()

    class RegenerateAnnexuresTool(BaseTool):
        name: str = "regenerate_annexures"
        description: str = (
            "Re-runs annexure extraction for a tender. Idempotent — approved "
            "workspace rows are preserved."
        )
        args_schema: Type[BaseModel] = _RegenerateAnnexuresInput

        def _run(self, tender_id: int) -> str:
            return asyncio.run(self._arun(tender_id=tender_id))

        async def _arun(self, tender_id: int) -> str:
            db = _fresh_session()
            try:
                from app.services.langchain.graphs.annexure_finder_agent import run_annexure_extraction
                result = await run_annexure_extraction(db, tender_id)
                _log_audit(user_id, "regenerate_annexures", {"tender_id": tender_id}, "completed")
                return _safe_dump({
                    "status": result.get("status", "completed"),
                    "tender_id": tender_id,
                    "counts": result.get("counts", {}),
                })
            except Exception as e:
                _log_audit(user_id, "regenerate_annexures", {"tender_id": tender_id}, f"failed: {e}")
                return _safe_dump({"status": "failed", "error": str(e)[:500]})
            finally:
                db.close()

    class FinalizeDocumentTool(BaseTool):
        name: str = "finalize_document"
        description: str = (
            "Finalize a workspace document by checklist_item_id — converts the "
            "current draft markdown into the final DOCX artifact."
        )
        args_schema: Type[BaseModel] = _FinalizeDocumentInput

        def _run(self, tender_id: int, item_id: int) -> str:
            db = _fresh_session()
            try:
                from app.services.workspace_service import finalize_document
                result = finalize_document(db, item_id=item_id, user_id=user_id or 0)
                _log_audit(user_id, "finalize_document",
                           {"tender_id": tender_id, "item_id": item_id}, "completed")
                return _safe_dump({"status": "completed", **(result or {})})
            except Exception as e:
                _log_audit(user_id, "finalize_document",
                           {"tender_id": tender_id, "item_id": item_id}, f"failed: {e}")
                return _safe_dump({"status": "failed", "error": str(e)[:500]})
            finally:
                db.close()

    class RetryFailedRunTool(BaseTool):
        name: str = "retry_failed_agent_run"
        description: str = (
            "Re-dispatches a previously-failed AgentRun using the same prompt + "
            "agent selection. Returns immediately after the retry completes."
        )
        args_schema: Type[BaseModel] = _RetryRunInput

        def _run(self, agent_run_id: str) -> str:
            return asyncio.run(self._arun(agent_run_id=agent_run_id))

        async def _arun(self, agent_run_id: str) -> str:
            db = _fresh_session()
            try:
                from app.core.ownership import scoped_query
                from app.models.agent_run import AgentRun
                from app.services.langchain.graphs.agent_router_graph import route_and_execute
                # Re-running another user's prompt as that user, into their
                # session, with the preview handed to the caller, was a read
                # and a write of someone else's work in one tool call.
                row = scoped_query(db, AgentRun, _acting_for(user_id)).filter(
                    AgentRun.id == agent_run_id
                ).first()
                if not row:
                    return _safe_dump({"status": "failed", "error": "AgentRun not found"})
                if row.status != "failed":
                    return _safe_dump({"status": "skipped", "reason": f"run status = {row.status}, not failed"})
                result = await route_and_execute(
                    db=db,
                    message=row.prompt or "",
                    session_id=row.router_session_id,
                    tender_id=row.tender_id,
                    user_id=row.user_id,
                )
                _log_audit(user_id, "retry_failed_agent_run",
                           {"agent_run_id": agent_run_id}, result.get("output_type", "unknown"))
                return _safe_dump({
                    "status": "completed",
                    "new_output_type": result.get("output_type"),
                    "new_agents_used": result.get("agents_used"),
                    "preview": (result.get("output") or "")[:1000],
                })
            except Exception as e:
                _log_audit(user_id, "retry_failed_agent_run",
                           {"agent_run_id": agent_run_id}, f"failed: {e}")
                return _safe_dump({"status": "failed", "error": str(e)[:500]})
            finally:
                db.close()

    return [
        InitWorkspaceForceTool(),
        RegenerateChecklistTool(),
        RegenerateAnnexuresTool(),
        FinalizeDocumentTool(),
        RetryFailedRunTool(),
    ]


# ────────────────────────────────────────────────────────────────────────────
# Family D: LLM meta-tools + web_search + document_reader
# ────────────────────────────────────────────────────────────────────────────

class _RunWithLLMInput(BaseModel):
    provider: str = Field(..., description="One of: anthropic, openai, google")
    prompt: str = Field(..., description="User prompt to send")
    system: Optional[str] = Field(None, description="Optional system prompt")
    model: Optional[str] = Field(None, description="Specific model (e.g. 'gpt-4.1-mini', 'gemini-2.5-flash')")
    temperature: float = Field(0.3)
    max_tokens: int = Field(2048)


class _WebSearchInput(BaseModel):
    query: str = Field(..., description="Search query")
    max_results: int = Field(5)


class _DocumentReaderInput(BaseModel):
    tender_id: int = Field(..., description="Tender ID whose documents to read")
    max_chars: int = Field(15000)


_DEFAULT_MODEL_PER_PROVIDER = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.5-flash",
}


def build_llm_meta_tools() -> list[BaseTool]:
    """Family D — cross-provider LLM calls + web_search + document_reader."""

    class RunWithLLMTool(BaseTool):
        name: str = "run_with_llm"
        description: str = (
            "One-shot call to ANY LLM provider (anthropic / openai / google). "
            "Use when a specific provider/model is best for a subtask — e.g. "
            "OpenAI for strict JSON extraction, Gemini Flash for cheap vision, "
            "Claude Haiku for quick reasoning. Returns the model's text output."
        )
        args_schema: Type[BaseModel] = _RunWithLLMInput

        def _run(self, provider: str, prompt: str, system: Optional[str] = None,
                 model: Optional[str] = None, temperature: float = 0.3,
                 max_tokens: int = 2048) -> str:
            return asyncio.run(self._arun(
                provider=provider, prompt=prompt, system=system,
                model=model, temperature=temperature, max_tokens=max_tokens,
            ))

        async def _arun(self, provider: str, prompt: str, system: Optional[str] = None,
                        model: Optional[str] = None, temperature: float = 0.3,
                        max_tokens: int = 2048) -> str:
            db = _fresh_session()
            try:
                from app.services.langchain.provider_config import get_api_key
                from app.services.langchain.model_factory import create_chat_model
                from langchain_core.messages import SystemMessage, HumanMessage

                provider = (provider or "").lower().strip()
                if provider not in _DEFAULT_MODEL_PER_PROVIDER:
                    return _safe_dump({"status": "failed", "error": f"Unsupported provider: {provider}"})
                api_key = get_api_key(db, provider)
                if not api_key:
                    return _safe_dump({"status": "failed",
                                       "error": f"No API key configured for provider '{provider}'"})
                resolved_model = model or _DEFAULT_MODEL_PER_PROVIDER[provider]
                llm = create_chat_model(
                    provider=provider,
                    model=resolved_model,
                    api_key=api_key,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                msgs = []
                if system:
                    msgs.append(SystemMessage(content=system))
                msgs.append(HumanMessage(content=prompt))
                result = await llm.ainvoke(msgs)
                return _safe_dump({
                    "status": "completed",
                    "provider": provider,
                    "model": resolved_model,
                    "content": (getattr(result, "content", "") or "")[:6000],
                })
            except Exception as e:
                logger.error(f"[decision_maker] run_with_llm failed: {e}")
                return _safe_dump({"status": "failed", "error": str(e)[:500]})
            finally:
                db.close()

    class WebSearchTool(BaseTool):
        name: str = "web_search"
        description: str = (
            "Search the web for market rates, government portals, technical specs, "
            "regulations. Uses Gemini Search Grounding → Tavily → DuckDuckGo."
        )
        args_schema: Type[BaseModel] = _WebSearchInput

        def _run(self, query: str, max_results: int = 5) -> str:
            db = _fresh_session()
            try:
                from app.services.langchain.tools.web_search_tool import WebSearchTool as _Inner
                inner = _Inner(db=db)
                return inner._run(query=query, max_results=max_results)
            except Exception as e:
                return _safe_dump({"status": "failed", "error": str(e)[:500]})
            finally:
                db.close()

    class DocumentReaderTool(BaseTool):
        name: str = "document_reader"
        description: str = (
            "Read and return the text content of a tender's documents. Useful when "
            "the master needs to inspect raw text before routing to a writer agent."
        )
        args_schema: Type[BaseModel] = _DocumentReaderInput

        def _run(self, tender_id: int, max_chars: int = 15000) -> str:
            db = _fresh_session()
            try:
                from app.services.langchain.tools.document_reader_tool import DocumentReaderTool as _Inner
                inner = _Inner(db=db)
                # The inner tool's _run has its own arg schema; forward compatibly.
                out = inner._run(tender_id=tender_id)  # type: ignore[arg-type]
                if isinstance(out, str):
                    return out[:max_chars]
                # The limit goes INTO the dump. Slicing its output would cut
                # the JSON mid-token again — the exact defect _safe_dump exists
                # to prevent — whenever max_chars is below the default ceiling.
                return _safe_dump(out, limit=max(max_chars, 200))
            except Exception as e:
                return _safe_dump({"status": "failed", "error": str(e)[:500]})
            finally:
                db.close()

    return [RunWithLLMTool(), WebSearchTool(), DocumentReaderTool()]
