"""Write-confirmation gate at the tool boundary.

The Decision Maker runs autonomously (``decision_maker_autonomous=True``): it
plans and acts without asking permission. That is fine for reading and
analysing, but it also means a multi-step run can regenerate a checklist,
overwrite a finalized document, or force-reinitialize a workspace with no human
in the loop — and, once the assistant is reachable from every page by users who
cannot evaluate what it is about to do, that is not a safe default.

This module gates *writes* without touching the reasoning loop. Every tool is
classified, and a gated tool does not execute: it records the intended call and
returns an observation telling the agent to stop and wait. The caller
(``run_decision_maker``) sees the pending action and surfaces a confirmation
card; approving it replays the call for real.

Two properties make this trustworthy, and both are pinned by tests:

1. **Reads pass through untouched** — no latency, no behaviour change, no
   confirmation fatigue that trains users to click through the gate.
2. **Unknown tools are treated as writes** — the policy table is a whitelist,
   so a tool added next year is gated by default rather than silently
   bypassing the gate. Failing closed is the whole point.

Why an observation string rather than an exception: the existing ``ask_user``
tool already suspends the agent this way, and it is proven against LangGraph's
ReAct loop. Raising through that loop risks the framework catching the error and
retrying the same call — turning a safety gate into a retry storm.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Optional

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

READ = "read"
WRITE = "write"
DESTRUCTIVE = "destructive"

#: The three tier sets, derived from the capability registry — the single
#: source of truth for what each capability risks. These names are kept because
#: other modules and the drift tests import them; only their provenance
#: changed. A capability added to the registry classifies correctly here with
#: no second edit, which is the entire point: the old parallel copies agreed
#: with the registry on the day they were checked and had no reason to keep
#: agreeing.
#:
#: ``propose_plan`` and ``ask_user`` are READ deliberately: they are how the
#: agent talks to the user, so gating them would deadlock the run. Delegation
#: (``call_*``) is READ by prefix in ``classify_tool`` — the worker's own
#: writes are gated where they run.
def _tiers_from_registry() -> tuple[set, set, set]:
    from app.services.langchain.capability_registry import CAPABILITIES

    reads, writes, destructive = set(), set(), set()
    for key, cap in CAPABILITIES.items():
        if cap.tier == READ:
            reads.add(key)
        elif cap.tier == DESTRUCTIVE:
            destructive.add(key)
        else:
            writes.add(key)
    return reads, writes, destructive


_READ_TOOLS, _WRITE_TOOLS, _DESTRUCTIVE_TOOLS = _tiers_from_registry()

#: Plain-language descriptions for the confirmation card. Keyed by tool name;
#: the card is read by people who do not know what a "tool" is, so these must
#: never contain the tool name or any other jargon.
_SUMMARIES = {
    "regenerate_checklist": "Rebuild the submission checklist. The current checklist will be replaced.",
    "regenerate_annexures": "Re-extract every annexure from the tender documents. Existing annexure documents will be replaced.",
    "retry_failed_agent_run": "Run the failed job again.",
    "init_workspace_force": "Reset this workspace and rebuild it from scratch. Everything currently in it will be lost.",
    "finalize_document": "Mark this document as final. It will replace the current finalized version.",
    "call_deep_analyzer": "Run a full analysis of the tender documents. This takes a few minutes.",
    "call_checklist_generator": "Generate a submission checklist for this tender.",
    "call_proposal_creator": "Draft proposal documents for this tender.",
    "call_costing_researcher": "Research market rates and build a cost breakdown.",
    "call_annexure_finder": "Extract the annexures from the tender documents and save each as an editable document.",
    "call_workspace_manager": "Make changes to the workspace for this tender.",
    "document_generator": "Create a new document and save it to the workspace.",
    "workspace_generate_document": "Write a document into the workspace. If one with this name exists it will be replaced.",
    "workspace_init": "Set up the workspace for this tender.",
    "xlsx_generator": "Create an Excel file and attach it to this session.",
    "docx_generator": "Create a Word document and attach it to this session.",
    "update_platform_setting": "Change a platform-wide setting. This affects every user, not just you.",
}

_DEFAULT_SUMMARY = "Make a change to your data."


def _registry_summary(name: str):
    from app.services.langchain.capability_registry import get as _get_capability

    cap = _get_capability(name)
    return cap.summary if cap else None


def classify_tool(name: str) -> str:
    """Return ``read`` / ``write`` / ``destructive`` for a tool name.

    Unknown names return ``write``. This is a whitelist on purpose — see the
    module docstring.
    """
    if name in _READ_TOOLS:
        return READ
    # Delegation to any worker agent, including ones registered later in the
    # admin section. See the note in _READ_TOOLS.
    if name.startswith("call_"):
        return READ
    # The Tier 2 dispatcher. Dispatching is not the write: the tool it
    # resolves is wrapped with this same policy before it runs, so gating the
    # dispatcher too would confirm twice — and record "use_capability" on the
    # card instead of the action the user is actually approving.
    if name == "use_capability":
        return READ
    if name in _DESTRUCTIVE_TOOLS:
        return DESTRUCTIVE
    if name in _WRITE_TOOLS:
        return WRITE
    logger.warning(
        "tool_policy: unknown tool %r — gating it as a write. Add it to "
        "app/services/langchain/tool_policy.py to classify it explicitly.",
        name,
    )
    return WRITE


def should_gate(tool_name: str, *, scope: str) -> bool:
    """Does this call need the user's confirmation under this gate scope?

    - READ never gates, under either scope.
    - DESTRUCTIVE always gates: it destroys work a human may have hand-edited.
    - A registered WRITE gates only under ``all_writes`` — ``destructive_only``
      is the owner's deliberate loosening (a regenerated checklist can simply
      be regenerated again).
    - An UNREGISTERED tool gates under BOTH scopes. It classifies as a write
      by the fail-safe default, which means nobody has assessed its blast
      radius — and a scope that lets ordinary writes through must not also
      wave through the ones nobody classified.
    """
    tier = classify_tool(tool_name)
    if tier == READ:
        return False
    if tier == DESTRUCTIVE:
        return True
    if tool_name not in _WRITE_TOOLS:
        return True  # unclassified: gate regardless of scope
    return scope == "all_writes"


def resolve_gate_scope() -> str:
    """The live ``confirm_gate_scope`` PlatformSetting.

    An absent row and a read error both resolve to ``all_writes``: no decision
    on record means confirm everything, and the loosened scope only ever
    arrives as the owner's explicit, seeded choice. Failing toward MORE
    confirmation, never less.
    """
    try:
        from app.core.database import SessionLocal
        from app.services.settings_service import get_effective_setting

        with SessionLocal() as db:
            value = get_effective_setting(db, "confirm_gate_scope", "all_writes")
        return str(value or "all_writes").lower()
    except Exception:
        return "all_writes"


def audit_ungated_write(user_id: Optional[int], tool_name: str, args: dict[str, Any]) -> None:
    """One audit row per write the gate scope let through.

    The owner traded a confirmation prompt for a log; the log is only worth
    the trade if it is complete. Failure here is logged loudly but does not
    block the write — a broken audit table must not take the platform's write
    path down with it.
    """
    try:
        from app.core.database import SessionLocal
        from app.models.audit_log import AuditLog

        db = SessionLocal()
        try:
            db.add(AuditLog(
                user_id=user_id,
                action=f"ungated_write.{tool_name}",
                resource_type="tender",
                resource_id=str(args.get("tender_id") or ""),
                details={"args": args, "gate": "bypassed_by_scope"},
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.error(
            "AUDIT WRITE FAILED for ungated %s %s — the write proceeded without "
            "a trail.", tool_name, args, exc_info=True,
        )


def summarize_action(name: str, args: dict[str, Any]) -> str:
    """A one-line, jargon-free description of what approving this will do."""
    summary = _SUMMARIES.get(name) or _registry_summary(name) or _DEFAULT_SUMMARY
    tender_id = args.get("tender_id")
    if tender_id:
        summary = f"{summary} (Tender #{tender_id})"
    return summary


class PendingActionCapture(dict):
    """Holds the first gated call of a run.

    Only the first is kept: the confirmation card shows one specific action, and
    letting a later call overwrite it would mean the user approves something
    other than what they read.
    """

    def is_pending(self) -> bool:
        return bool(self.get("tool"))

    def record(self, tool: str, args: dict[str, Any], tier: str) -> None:
        if self.is_pending():
            return
        self["tool"] = tool
        self["args"] = args
        self["tier"] = tier
        self["summary"] = summarize_action(tool, args)


#: Returned to the agent in place of the tool's real result.
_NO_SCOPE_OBSERVATION = (
    "BLOCKED: this action changes data but no confirmation channel is open, so "
    "it cannot be approved and has NOT been performed. Tell the user this "
    "action is unavailable here and stop."
)

_SUSPEND_OBSERVATION = (
    "AWAITING_USER_CONFIRMATION: this action changes the user's data and needs "
    "their approval first. It has NOT been performed. Do not call any more "
    "tools. Reply with one short sentence telling the user what you are waiting "
    "to do, and stop."
)

# Kwargs LangChain injects into _arun that are plumbing, not user arguments.
_NON_ARG_KEYS = {"run_manager", "callbacks", "config"}


def _clean_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if k not in _NON_ARG_KEYS}


class ApprovalGrant(dict):
    """A single user approval, consumed the first time it matches.

    Single-use on purpose. If an approval stayed valid for the whole run, an
    agent that decided to call the same tool twice would perform the second
    write with no confirmation — the user approved one action, not a licence.
    """

    def __init__(self, tool: str, args: dict[str, Any]):
        super().__init__(tool=tool, args=args or {}, consumed=False)

    def matches(self, tool: str, args: dict[str, Any]) -> bool:
        return (
            not self["consumed"]
            and self["tool"] == tool
            and self["args"] == args
        )

    def consume(self) -> None:
        self["consumed"] = True


def _wrap_one(
    inner: BaseTool,
    capture: Optional[PendingActionCapture],
    stream_callback: Optional[Callable[[str, dict], None]],
    grant: Optional[ApprovalGrant] = None,
) -> BaseTool:
    tier = classify_tool(inner.name)
    if tier == READ:
        return inner

    # Closure over `inner` rather than a pydantic field: BaseTool is a
    # BaseModel, and holding another tool as a field fights its validation.
    _name = inner.name
    _description = inner.description
    _args_schema = inner.args_schema

    class _GatedTool(BaseTool):
        name: str = _name
        description: str = _description
        args_schema: Any = _args_schema

        class Config:
            arbitrary_types_allowed = True

        def _run(self, *args, **kwargs):  # type: ignore[override]
            import asyncio

            return asyncio.run(self._arun(*args, **kwargs))

        async def _arun(self, *args, **kwargs):  # type: ignore[override]
            clean = _clean_args(kwargs)

            # Bind late: an explicit capture wins, otherwise use whatever
            # policy_scope is open for this run. Tools built inside the
            # specialist agents only ever get the ambient one.
            active_capture = capture if capture is not None else _current_capture.get()
            active_grant = grant if grant is not None else _current_grant.get()
            active_cb = stream_callback or _current_stream_cb.get()

            # No scope at all means nothing can surface a confirmation to the
            # user. Executing anyway would be a silent ungated write, so refuse
            # instead — failing closed is the whole point of this module.
            if active_capture is None:
                logger.error(
                    "tool_policy: %s called with no policy_scope open; refusing "
                    "to execute an ungated write.",
                    _name,
                )
                return _NO_SCOPE_OBSERVATION

            # The live gate scope may let this write through without asking —
            # the owner's destructive_only setting. Decided at run time so the
            # admin-panel change applies to tools already wrapped, and audited
            # because a prompt was traded for a log.
            if not should_gate(_name, scope=resolve_gate_scope()):
                audit_ungated_write(_current_user_id.get(), _name, clean)
                logger.info(
                    "tool_policy: %s ran ungated (confirm_gate_scope) — audited",
                    _name,
                )
                return await inner._arun(*args, **kwargs)

            # The user already approved exactly this call — run it for real and
            # burn the approval, so a repeat call is gated again.
            if active_grant is not None and active_grant.matches(_name, clean):
                active_grant.consume()
                logger.info("tool_policy: executing approved action %s", _name)
                return await inner._arun(*args, **kwargs)

            active_capture.record(_name, clean, tier)
            logger.info(
                "tool_policy: gated %s (tier=%s) — awaiting user confirmation",
                _name,
                tier,
            )
            if active_cb:
                try:
                    active_cb(
                        "confirm_required",
                        {
                            "tool": _name,
                            "tier": tier,
                            "summary": summarize_action(_name, clean),
                            "args": clean,
                        },
                    )
                except Exception:  # streaming must never break the run
                    logger.debug("tool_policy: stream_callback failed", exc_info=True)
            return _SUSPEND_OBSERVATION

    return _GatedTool()


# ────────────────────────────────────────────────────────────────────────────
# Run-scoped policy context
#
# The orchestrator can thread a capture through explicitly, but the specialist
# agents cannot: the router calls them directly and they build their own tools
# deep inside `chat_agent_wrappers` (a ~194KB module), so there is no seam to
# pass one through. A ContextVar gives every tool loaded during a run access to
# that run's capture without threading state through every call site — and,
# because it is a ContextVar rather than a global, concurrent runs in the same
# worker do not see each other's pending actions.
# ────────────────────────────────────────────────────────────────────────────

_current_capture: "ContextVar[Optional[PendingActionCapture]]" = ContextVar(
    "drpl_pending_action_capture", default=None
)
#: Who this run acts for — read by the ungated-write audit row. Set by
#: policy_scope; None when the caller did not say.
_current_user_id: "ContextVar[Optional[int]]" = ContextVar(
    "drpl_policy_user_id", default=None
)
_current_grant: "ContextVar[Optional[ApprovalGrant]]" = ContextVar(
    "drpl_approval_grant", default=None
)
_current_stream_cb: "ContextVar[Optional[Callable[[str, dict], None]]]" = ContextVar(
    "drpl_policy_stream_callback", default=None
)


@contextmanager
def policy_scope(
    capture: Optional[PendingActionCapture] = None,
    grant: Optional[ApprovalGrant] = None,
    stream_callback: Optional[Callable[[str, dict], None]] = None,
    user_id: Optional[int] = None,
):
    """Bind a capture/grant to everything that runs inside this block.

    Any tool wrapped by `wrap_tools_with_policy` without an explicit capture
    records into this one, so a gated write inside a specialist agent surfaces
    to the caller the same way an orchestrator write does.
    """
    cap = capture if capture is not None else PendingActionCapture()
    tokens = (
        _current_capture.set(cap),
        _current_grant.set(grant),
        _current_stream_cb.set(stream_callback),
        _current_user_id.set(user_id) if user_id is not None else None,
    )
    try:
        yield cap
    finally:
        _current_capture.reset(tokens[0])
        _current_grant.reset(tokens[1])
        _current_stream_cb.reset(tokens[2])
        if tokens[3] is not None:
            _current_user_id.reset(tokens[3])


def current_capture() -> Optional[PendingActionCapture]:
    """The capture for the run in progress, if a `policy_scope` is open."""
    return _current_capture.get()


def wrap_tools_with_policy(
    tools: list[BaseTool],
    capture: Optional[PendingActionCapture] = None,
    stream_callback: Optional[Callable[[str, dict], None]] = None,
    enabled: bool = True,
    grant: Optional[ApprovalGrant] = None,
) -> list[BaseTool]:
    """Wrap ``tools`` so write/destructive calls suspend for confirmation.

    ``enabled=False`` returns the tools untouched, so a surface can opt out
    (and so the gate can be switched off in production without a deploy).

    ``grant`` carries a single user approval through: the matching call executes
    for real, once, and every other write still suspends.

    With no explicit ``capture``, the wrapper binds late to whatever
    `policy_scope` is open when the tool actually runs. That late binding is
    what lets tools built deep inside the specialist agents participate without
    any call site having to pass state down.
    """
    if not enabled:
        return tools
    return [_wrap_one(t, capture, stream_callback, grant) for t in tools]


def execute_pending_action(
    tools: list[BaseTool],
    pending: dict[str, Any],
) -> Any:
    """Replay an approved call against the UNWRAPPED tools.

    Raises ``KeyError`` if the tool is not in ``tools`` — an approved action
    naming a tool that no longer exists must fail loudly, not silently no-op.
    """
    name = pending.get("tool")
    args = pending.get("args") or {}
    for tool in tools:
        if tool.name == name:
            return tool._arun(**args)
    raise KeyError(f"Cannot execute approved action: no tool named {name!r}")
