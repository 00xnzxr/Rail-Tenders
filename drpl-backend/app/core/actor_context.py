"""Task-local identity of the user the platform is currently acting for.

Two subsystems need to know who is asking, in code that has neither a request
nor a `current_user`:

- **Usage metering** attributes every LLM call to a user. `DRPLCallbackHandler`
  is constructed at eight sites, most of them inside graphs that were never
  handed a user id.
- **The ownership firewall** scopes what agent tools may read. Tools query with
  a raw `db` session and no notion of who is asking, so without an ambient
  actor a user could ask the Master Agent for another user's cost breakdown and
  get it — through the front door, with no HTTP route involved.

Threading a parameter through every construction site would silently skip the
sites that have no user to pass, and both features fail *open* when the actor
is missing. An ambient scope makes the omission visible in one place instead.

Mirrors `app/core/run_context.py`, and composes with it and with
`tool_policy.policy_scope` — `asyncio.create_task` copies the context, so the
binding survives into the task the agent layer actually runs in.

Usage:
    from app.core.actor_context import actor_scope, current_actor

    with actor_scope(user_id=user.id, role=user.role):
        ...  # metering and ownership scoping resolve the actor from here
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import NamedTuple, Optional


class Actor(NamedTuple):
    """Who the platform is acting for. ``role`` is advisory.

    Read the role from the database for any decision that grants access — a
    role carried in a context is only as trustworthy as whoever opened the
    scope, and `platform_tools` already establishes that authorization reads
    the role from the DB rather than from the caller.
    """

    user_id: Optional[int]
    role: Optional[str] = None


_current_actor: contextvars.ContextVar[Optional[Actor]] = contextvars.ContextVar(
    "drpl_actor", default=None
)


@contextmanager
def actor_scope(user_id: Optional[int], role: Optional[str] = None):
    """Bind the acting user for the duration of the block."""
    token = _current_actor.set(Actor(user_id=user_id, role=role))
    try:
        yield
    finally:
        _current_actor.reset(token)


def current_actor() -> Optional[Actor]:
    """The acting user, or None when no scope is open.

    None is meaningful: it marks work with no human behind it — seeders, the
    archive sweep, scheduled jobs. Metering records such rows unattributed;
    the firewall refuses to serve owned data at all. Neither may guess.
    """
    return _current_actor.get()
