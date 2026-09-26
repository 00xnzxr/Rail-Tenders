"""Platform-wide inspection and configuration tools for the master agent.

Sub-project B of the autonomous-platform work: widen what the governing agent
can see and change beyond the five repair actions it started with.

The important part is not the extra reach — it is that the reach is **bounded
by the calling user's role, server-side**.

The write-confirmation gate (`tool_policy`) asks the *user* whether to proceed.
It does not ask whether they are *allowed* to. Those are different questions,
and conflating them would be a privilege-escalation bug wearing a safety
feature's clothes: an operator could ask the assistant to change a
master-admin-only setting and simply click "Yes". So every tool here declares a
minimum role, and that role is checked **when the tool runs**, not only when it
is offered. Both matter:

- Filtering at build time keeps tools the user cannot use out of the prompt, so
  the agent does not plan around capabilities it will never get.
- Checking at call time is the actual control. Build-time filtering is a UX
  affordance; an agent that hallucinates a tool name, or a future refactor that
  forgets to pass the role, must still be refused.

Refusals are returned as observation strings rather than raised, matching how
the gate suspends: the agent reads the refusal and explains it to the user
instead of crashing the run.

Secrets are never readable. `PlatformSetting.is_secret` values are redacted on
read and rejected on write — an agent that can print an API key into a chat
transcript has exfiltrated it, no matter how the conversation started.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: Ordered so a higher rank satisfies a lower requirement.
_ROLE_RANK = {"operator": 0, "admin": 1, "master_admin": 2}

REDACTED = "[hidden — this is a secret value]"


def role_allows(user_role: Optional[str], min_role: str) -> bool:
    """True when ``user_role`` meets or exceeds ``min_role``.

    An unknown or missing role ranks below everything, so the default is deny.
    """
    return _ROLE_RANK.get(user_role or "", -1) >= _ROLE_RANK[min_role]


def _refusal(min_role: str) -> str:
    human = "an administrator" if min_role == "admin" else "a master administrator"
    return (
        f"REFUSED: this needs {human}. Tell the user you cannot do this with "
        "their current permissions, and do not try another way round it."
    )


def _make_tool(
    *,
    name: str,
    description: str,
    args_schema: Type[BaseModel],
    min_role: str,
    user_role: Optional[str],
    fn,
) -> BaseTool:
    """Build one role-checked tool."""

    _name, _description, _schema = name, description, args_schema

    class _T(BaseTool):
        name: str = _name
        description: str = _description
        args_schema: Any = _schema

        class Config:
            arbitrary_types_allowed = True

        def _run(self, **kwargs):  # type: ignore[override]
            import asyncio

            return asyncio.run(self._arun(**kwargs))

        async def _arun(self, **kwargs):  # type: ignore[override]
            # The real control. Never rely on the tool merely not being offered.
            if not role_allows(user_role, min_role):
                logger.warning(
                    "platform_tools: %s refused for role %r (needs %s)",
                    _name,
                    user_role,
                    min_role,
                )
                return _refusal(min_role)
            kwargs.pop("run_manager", None)
            try:
                return fn(**kwargs)
            except Exception as e:  # never surface a traceback to the chat
                logger.error("platform_tools: %s failed: %s", _name, e, exc_info=True)
                return f"That didn't work: {e}"

    return _T()


# ── argument schemas ────────────────────────────────────────────────────────


class _CategoryInput(BaseModel):
    category: Optional[str] = Field(
        None, description="Optional category filter, e.g. 'ai', 'security'."
    )


class _SettingUpdateInput(BaseModel):
    key: str = Field(..., description="The exact setting key to change.")
    value: str = Field(..., description="The new value, as a string.")


class _RecentRunsInput(BaseModel):
    status: Optional[str] = Field(
        None, description="Filter by status: running, completed, failed, timeout."
    )
    limit: int = Field(20, description="How many to return (max 100).")


class _EmptyInput(BaseModel):
    pass


# ── the tools ───────────────────────────────────────────────────────────────


def build_platform_tools(
    db,
    user_id: Optional[int],
    user_role: Optional[str],
) -> list[BaseTool]:
    """Platform inspection + configuration, bounded by ``user_role``.

    Tools above the caller's role are omitted from the returned list AND refuse
    at call time. See the module docstring for why both.
    """
    from app.models.agent_builder import AgentExecution
    from app.models.platform_setting import PlatformSetting
    from app.models.user import User

    def list_settings(category: Optional[str] = None) -> str:
        q = db.query(PlatformSetting)
        if category:
            q = q.filter(PlatformSetting.category == category)
        rows = q.order_by(PlatformSetting.category, PlatformSetting.key).limit(200).all()
        if not rows:
            return "No settings found."
        out = []
        for r in rows:
            value = REDACTED if r.is_secret else r.value
            out.append(
                f"- {r.key} ({r.category}) = {value}"
                + (f"  — {r.description}" if r.description else "")
            )
        return "\n".join(out)

    def update_setting(key: str, value: str) -> str:
        row = db.query(PlatformSetting).filter(PlatformSetting.key == key).first()
        if not row:
            # Refuse rather than create: an agent inventing a key would write a
            # setting nothing reads, and the user would believe it took effect.
            return (
                f"REFUSED: there is no platform setting called '{key}'. "
                "List the settings first and use an exact key."
            )
        if row.is_secret:
            return (
                f"REFUSED: '{key}' holds a secret and cannot be changed from "
                "chat. Ask the user to change it in Admin → Settings."
            )

        # Keep the stored type stable — a bool setting silently becoming the
        # string "false" reads as truthy everywhere downstream.
        coerced: Any = value
        try:
            if row.value_type == "bool":
                if value.strip().lower() not in ("true", "false"):
                    return "REFUSED: this setting is true/false. Use 'true' or 'false'."
                coerced = value.strip().lower()
            elif row.value_type == "int":
                coerced = str(int(value))
            elif row.value_type == "float":
                coerced = str(float(value))
            elif row.value_type == "json":
                json.loads(value)
                coerced = value
        except (TypeError, ValueError):
            return f"REFUSED: '{value}' is not a valid {row.value_type} value."

        previous = row.value
        row.value = coerced
        row.updated_by = user_id
        db.add(row)
        db.commit()
        logger.info(
            "platform_tools: setting %s changed by user %s: %r -> %r",
            key,
            user_id,
            previous,
            coerced,
        )
        return f"Changed '{key}' from {previous} to {coerced}."

    def platform_health() -> str:
        from app.core.database import engine

        lines = []
        try:
            pool = engine.pool
            lines.append(
                f"- Database connections in use: {pool.checkedout()} of {pool.size()}"
            )
        except Exception:
            lines.append("- Database pool: unavailable")

        try:
            from app.core.config import get_settings
            from redis import Redis
            from rq import Queue

            settings = get_settings()
            if settings.redis_url:
                q = Queue(settings.rq_queue_name, connection=Redis.from_url(settings.redis_url))
                lines.append(f"- Jobs waiting in the queue: {q.count}")
            else:
                lines.append("- Queue: not configured (jobs run immediately)")
        except Exception as e:
            lines.append(f"- Queue: could not be read ({e})")

        running = (
            db.query(AgentExecution)
            .filter(AgentExecution.status == "running")
            .count()
        )
        lines.append(f"- Agent jobs running right now: {running}")
        return "\n".join(lines)

    def recent_runs(status: Optional[str] = None, limit: int = 20) -> str:
        limit = max(1, min(int(limit or 20), 100))
        q = db.query(AgentExecution)
        if status:
            q = q.filter(AgentExecution.status == status)
        rows = q.order_by(AgentExecution.created_at.desc()).limit(limit).all()
        if not rows:
            return "No agent runs found."
        out = []
        for r in rows:
            when = r.created_at.isoformat() if r.created_at else "unknown time"
            line = f"- #{r.id} {r.status} at {when}"
            if r.latency_ms:
                line += f" ({r.latency_ms}ms)"
            if r.error_message:
                line += f" — {str(r.error_message)[:200]}"
            out.append(line)
        return "\n".join(out)

    def list_users() -> str:
        rows = db.query(User).order_by(User.id).limit(200).all()
        if not rows:
            return "No users found."
        # Never any password material, secret or otherwise.
        return "\n".join(
            f"- #{u.id} {u.email} ({u.role}){'' if u.is_active else ' — deactivated'}"
            for u in rows
        )

    specs = [
        (
            "list_platform_settings",
            "List platform configuration settings and their current values. "
            "Secret values are hidden. Use this before changing any setting so "
            "you have the exact key.",
            _CategoryInput,
            "admin",
            list_settings,
        ),
        (
            "get_platform_health",
            "Check how the platform is doing right now: database connections, "
            "queued jobs, and agent jobs currently running.",
            _EmptyInput,
            "admin",
            platform_health,
        ),
        (
            "list_recent_agent_runs",
            "List recent agent runs with their status, duration and any error. "
            "Use status='failed' to investigate what has been going wrong.",
            _RecentRunsInput,
            "admin",
            recent_runs,
        ),
        (
            "list_platform_users",
            "List the platform's user accounts and their roles.",
            _EmptyInput,
            "master_admin",
            list_users,
        ),
        (
            "update_platform_setting",
            "Change one platform configuration setting. Secrets cannot be "
            "changed this way. Always list the settings first to get the exact key.",
            _SettingUpdateInput,
            "master_admin",
            update_setting,
        ),
    ]

    tools: list[BaseTool] = []
    for name, description, schema, min_role, fn in specs:
        # Omit tools the caller cannot use, so the agent does not plan around
        # capabilities it will never be granted. The call-time check inside
        # _make_tool remains the actual control.
        if not role_allows(user_role, min_role):
            continue
        tools.append(
            _make_tool(
                name=name,
                description=description,
                args_schema=schema,
                min_role=min_role,
                user_role=user_role,
                fn=fn,
            )
        )
    return tools
