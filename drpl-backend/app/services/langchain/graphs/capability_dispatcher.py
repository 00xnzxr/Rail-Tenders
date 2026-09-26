"""DRPL — the `use_capability` dispatcher.

Total reach without a fifty-tool catalog. The Master Agent binds a small
always-on set directly; every other capability on its surface is reachable
through this one tool, whose description carries a domain-grouped index (each
key with its one-line purpose) so the model can see what exists without paying
schema for each.

Two properties are load-bearing, and both are pinned by tests:

- **Not a way around the role check.** An out-of-role key is withheld from the
  index AND refused at call time with the same wording `platform_tools` uses.
- **Not a way around the write gate.** The resolved tool is wrapped with
  `wrap_tools_with_policy` before it runs, so a gated write called through the
  dispatcher suspends into the ambient `policy_scope` exactly like a directly
  bound call.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any, Callable, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.services.langchain.capability_registry import (
    CAPABILITIES,
    Capability,
    keys_for_surface,
)

logger = logging.getLogger(__name__)

#: Bound directly on the Master rather than reached through the dispatcher:
#: the cross-cutting reads every job starts from, plus conversation control
#: (which needs its capture containers wired by run_decision_maker, not built
#: here). Everything else on the surface is Tier 2.
MASTER_ALWAYS_BOUND: frozenset[str] = frozenset({
    "tender_lookup",
    "document_reader",
    "cost_breakdown_read",
    "semantic_search",
    "web_search",
    "propose_plan",
    "ask_user",
    "read_worker_output",
    "conversation_history_search",
    "clarify",
})


class _DispatchInput(BaseModel):
    key: str = Field(..., description="The capability key from the index in this tool's description")
    args: dict = Field(default_factory=dict, description="Arguments for that capability, as named in its index entry")


def _tier2_keys(surface: str, user_role: Optional[str]) -> list[str]:
    return [
        k for k in keys_for_surface(surface, user_role)
        if k not in MASTER_ALWAYS_BOUND
    ]


def _index(surface: str, user_role: Optional[str]) -> str:
    """The domain-grouped index rendered into the tool description."""
    by_domain: dict[str, list[Capability]] = {}
    for key in _tier2_keys(surface, user_role):
        cap = CAPABILITIES[key]
        by_domain.setdefault(cap.domain, []).append(cap)

    lines: list[str] = []
    for domain in sorted(by_domain):
        lines.append(f"[{domain}]")
        for cap in by_domain[domain]:
            lines.append(f"  {cap.key}: {cap.purpose}")
    return "\n".join(lines)


def _expected_shape(tool: BaseTool) -> str:
    """A precise correction the model can act on, from the tool's own schema."""
    schema = tool.args_schema
    if schema is None:
        return "no arguments"
    parts = []
    for name, field_info in schema.model_fields.items():
        required = "" if field_info.is_required() else "?"
        parts.append(f"{name}{required}")
    return ", ".join(parts)


def _build_one(
    cap: Capability,
    db: Session,
    *,
    user_id: Optional[int],
    user_role: Optional[str],
    context: dict,
) -> Optional[BaseTool]:
    """Resolve one capability to a live tool instance.

    The two kinds the registry names: a class is instantiated (with a db
    session when it declares one); a factory is called with the arguments that
    family takes, and the tool picked out of its output by name.
    """
    module_path, attr = cap.target.split(":")
    module = importlib.import_module(module_path)
    target = getattr(module, attr)

    if cap.kind == "class":
        from app.services.langchain.tools.tool_loader import load_tools_by_keys

        # Through the loader, not a bare constructor — it injects db and the
        # per-tool scoping (memory agent keys, clarify session ids) and applies
        # the policy wrapper.
        tools = load_tools_by_keys(
            db, [cap.key],
            agent_key="decision_maker",
            proposal_session_id=context.get("proposal_session_id"),
            router_session_id=context.get("session_id"),
        )
        return tools[0] if tools else None

    # Factory kind. Each family has its own signature; the registry's target
    # names the builder, and the tool is picked from its output by name.
    builder_name = attr
    if builder_name in ("build_diagnostic_tools", "build_action_tools"):
        built = target(user_id=user_id)
    elif builder_name == "build_llm_meta_tools":
        built = target()
    elif builder_name == "build_platform_tools":
        built = target(db=db, user_id=user_id, user_role=user_role)
    elif builder_name == "build_quality_tools":
        built = target(db=db, user_id=user_id)
    else:
        # propose_plan / ask_user need capture containers wired by the run
        # loop; they are Tier 1 by definition and never resolved here.
        logger.warning("Capability '%s' has no dispatcher path (%s)", cap.key, builder_name)
        return None

    for tool in built:
        if tool.name == cap.key:
            from app.core.config import get_settings
            from app.services.langchain.tool_policy import wrap_tools_with_policy

            # Same kill switch as every other wrap: the dispatcher must not be
            # the one path where the gate cannot be switched off.
            return wrap_tools_with_policy(
                [tool], enabled=get_settings().assistant_confirm_gate_enabled,
            )[0]
    return None


def build_capability_dispatcher(
    db: Session,
    *,
    user_id: Optional[int],
    user_role: Optional[str],
    surface: str,
    context: Optional[dict] = None,
    stream_callback: Optional[Callable] = None,
) -> BaseTool:
    """One tool that reaches every Tier 2 capability on this surface."""
    ctx = context or {}
    index = _index(surface, user_role)
    description = (
        "Use any platform capability from the index below that is not bound as "
        "its own tool. Pass its key and its arguments. The index groups "
        "capabilities by domain; full guidance for each is in the Platform "
        "Capability Manual above.\n\n" + index
    )

    class _UseCapability(BaseTool):
        # A class body does not close over the enclosing function's locals, so
        # the generated description is passed at instantiation below.
        name: str = "use_capability"
        description: str = "Use any platform capability by key."
        args_schema: Type[BaseModel] = _DispatchInput

        class Config:
            arbitrary_types_allowed = True

        def _invoke(self, key: str, args: dict) -> str:
            cap = CAPABILITIES.get(key)
            if cap is None or surface not in cap.surfaces:
                available = ", ".join(sorted(_tier2_keys(surface, user_role)))
                return (
                    f"'{key}' is not a capability. Available keys: {available}"
                )

            from app.services.langchain.graphs.platform_tools import (
                _refusal, role_allows,
            )

            if cap.min_role != "operator" and not role_allows(user_role, cap.min_role):
                # Same wording as a direct call would produce — the dispatcher
                # is not a second opinion on the role model.
                return _refusal(cap.min_role)

            try:
                tool = _build_one(
                    cap, db, user_id=user_id, user_role=user_role, context=ctx,
                )
            except Exception as e:
                logger.error("use_capability could not build '%s': %s", key, e)
                tool = None
            if tool is None:
                return f"'{key}' could not be prepared. Report this rather than retrying."

            if tool.args_schema is not None:
                try:
                    tool.args_schema(**(args or {}))
                except ValidationError:
                    return f"'{key}' expects: {_expected_shape(tool)}"

            try:
                return str(tool.run(args or {}))
            except NotImplementedError:
                return str(asyncio.run(tool.arun(args or {})))
            except Exception as e:
                logger.error("use_capability '%s' failed: %s", key, e)
                return f"'{key}' failed: {e}"

        def _run(self, key: str, args: Optional[dict] = None) -> str:
            return self._invoke(key, args or {})

        async def _arun(self, key: str, args: Optional[dict] = None) -> str:
            return await asyncio.to_thread(self._invoke, key, args or {})

    return _UseCapability(description=description)
