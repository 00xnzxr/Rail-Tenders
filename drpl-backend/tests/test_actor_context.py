"""The ambient actor — who the platform is acting for, inside the agent layer.

Both the usage meter and the ownership firewall need the acting user deep in
code that has no request and no `current_user`: `DRPLCallbackHandler` is built
at eight sites, most of them in graphs that were never handed a user id, and
agent tools query the database with a raw session and no notion of who is
asking. Threading a parameter through all of them would silently miss the sites
that have no user to pass — which is the failure mode that matters, because
both features fail *open* when the actor is missing.

Follows the pattern `app/core/run_context.py` already establishes.
"""

import asyncio

import pytest

from app.core.actor_context import Actor, actor_scope, current_actor


def test_no_actor_outside_a_scope():
    assert current_actor() is None


def test_scope_binds_and_restores():
    with actor_scope(user_id=7, role="costing_research"):
        actor = current_actor()
        assert actor == Actor(user_id=7, role="costing_research")
        assert actor.user_id == 7
        assert actor.role == "costing_research"
    assert current_actor() is None


def test_nested_scopes_restore_the_outer_actor():
    with actor_scope(user_id=1, role="master_admin"):
        with actor_scope(user_id=2, role="costing_research"):
            assert current_actor().user_id == 2
        assert current_actor().user_id == 1


def test_scope_restores_after_an_exception():
    with pytest.raises(ValueError):
        with actor_scope(user_id=3, role="tender_search"):
            raise ValueError("boom")
    assert current_actor() is None


def test_role_is_optional():
    with actor_scope(user_id=9):
        assert current_actor() == Actor(user_id=9, role=None)


def test_binding_survives_into_a_task():
    """create_task copies the context — the same property policy_scope relies on.

    The agent layer runs inside `asyncio.create_task(run_decision_maker(...))`
    opened under the caller's scopes, so the actor has to survive that copy or
    it is useless exactly where it is needed.
    """
    seen = {}

    async def _drive():
        async def _inner():
            seen["actor"] = current_actor()

        with actor_scope(user_id=42, role="master_admin"):
            task = asyncio.create_task(_inner())
        await task

    asyncio.run(_drive())
    assert seen["actor"] == Actor(user_id=42, role="master_admin")


def test_concurrent_tasks_do_not_leak_actors():
    """Two runs in one event loop must not see each other's user."""
    seen = {}

    async def _one(uid: int):
        with actor_scope(user_id=uid, role="costing_research"):
            await asyncio.sleep(0)
            seen[uid] = current_actor().user_id

    async def _drive():
        await asyncio.gather(*(asyncio.create_task(_one(u)) for u in (11, 22, 33)))

    asyncio.run(_drive())
    assert seen == {11: 11, 22: 22, 33: 33}
