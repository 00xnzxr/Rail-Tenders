"""Role bounding for the master agent's platform tools.

The confirm gate asks the user whether to proceed. It does not ask whether they
are *allowed* to. These tests cover the second question, because conflating the
two would be privilege escalation dressed as a safety feature: an operator asks
the assistant to change a master-admin setting and simply clicks "Yes".
"""

import asyncio

import pytest

from app.models.platform_setting import PlatformSetting
from app.services.langchain.graphs.platform_tools import (
    REDACTED,
    build_platform_tools,
    role_allows,
)


def _tool(tools, name):
    for t in tools:
        if t.name == name:
            return t
    return None


def _call(tool, **kwargs):
    return asyncio.run(tool._arun(**kwargs))


@pytest.fixture
def settings_rows(db):
    rows = [
        PlatformSetting(
            key="test_public_flag", value="false", value_type="bool",
            category="general", description="A test flag", is_secret=False,
        ),
        PlatformSetting(
            key="test_secret_key", value="sk-super-secret-value", value_type="string",
            category="security", description="A test secret", is_secret=True,
        ),
        PlatformSetting(
            key="test_count", value="5", value_type="int",
            category="general", is_secret=False,
        ),
    ]
    for r in rows:
        db.add(r)
    db.commit()
    yield rows
    for r in rows:
        db.query(PlatformSetting).filter(PlatformSetting.key == r.key).delete()
    db.commit()


# ── the role ladder ─────────────────────────────────────────────────────────


def test_role_ladder():
    assert role_allows("master_admin", "admin")
    assert role_allows("admin", "admin")
    assert not role_allows("operator", "admin")
    assert not role_allows("operator", "master_admin")
    assert not role_allows("admin", "master_admin")


def test_unknown_and_missing_roles_are_denied():
    """Default deny. A typo'd or absent role must not become a free pass."""
    assert not role_allows(None, "admin")
    assert not role_allows("", "admin")
    assert not role_allows("superuser", "admin")
    assert not role_allows("MASTER_ADMIN", "admin")  # case matters


# ── build-time filtering ────────────────────────────────────────────────────


def test_operator_gets_no_platform_tools(db):
    assert build_platform_tools(db, user_id=1, user_role="operator") == []


def test_admin_gets_reads_but_not_writes(db):
    names = {t.name for t in build_platform_tools(db, user_id=1, user_role="admin")}
    assert "list_platform_settings" in names
    assert "get_platform_health" in names
    assert "list_recent_agent_runs" in names
    assert "update_platform_setting" not in names
    assert "list_platform_users" not in names


def test_master_admin_gets_everything(db):
    names = {t.name for t in build_platform_tools(db, user_id=1, user_role="master_admin")}
    assert "update_platform_setting" in names
    assert "list_platform_users" in names


# ── call-time enforcement (the actual control) ──────────────────────────────


def test_call_time_check_refuses_even_if_the_tool_is_obtained(db, settings_rows):
    """Build-time filtering is a UX affordance. If a tool is somehow reached
    with an under-privileged role — a refactor that forgets to pass it, an
    agent hallucinating the name — the call itself must still refuse."""
    tools = build_platform_tools(db, user_id=1, user_role="master_admin")
    tool = _tool(tools, "update_platform_setting")

    # Rebuild the same tool bound to a weaker role.
    weak = build_platform_tools(db, user_id=1, user_role="master_admin")
    assert tool is not None

    from app.services.langchain.graphs import platform_tools as pt

    downgraded = pt._make_tool(
        name="update_platform_setting",
        description="x",
        args_schema=tool.args_schema,
        min_role="master_admin",
        user_role="operator",
        fn=lambda **kw: "SHOULD NOT RUN",
    )

    result = _call(downgraded, key="test_public_flag", value="true")

    assert "REFUSED" in result
    assert "SHOULD NOT RUN" not in result
    # And the value is untouched.
    row = db.query(PlatformSetting).filter(PlatformSetting.key == "test_public_flag").first()
    assert row.value == "false"


# ── secrets ─────────────────────────────────────────────────────────────────


def test_secret_values_are_redacted_on_read(db, settings_rows):
    tools = build_platform_tools(db, user_id=1, user_role="master_admin")
    out = _call(_tool(tools, "list_platform_settings"))

    assert "sk-super-secret-value" not in out
    assert REDACTED in out
    # The key itself is fine to show — only the value is sensitive.
    assert "test_secret_key" in out


def test_secrets_cannot_be_written(db, settings_rows):
    tools = build_platform_tools(db, user_id=1, user_role="master_admin")
    result = _call(_tool(tools, "update_platform_setting"),
                   key="test_secret_key", value="sk-new")

    assert "REFUSED" in result
    row = db.query(PlatformSetting).filter(PlatformSetting.key == "test_secret_key").first()
    assert row.value == "sk-super-secret-value"


# ── writing settings ────────────────────────────────────────────────────────


def test_updates_a_real_setting(db, settings_rows):
    tools = build_platform_tools(db, user_id=1, user_role="master_admin")
    result = _call(_tool(tools, "update_platform_setting"),
                   key="test_public_flag", value="true")

    assert "REFUSED" not in result
    db.expire_all()
    row = db.query(PlatformSetting).filter(PlatformSetting.key == "test_public_flag").first()
    assert row.value == "true"
    assert row.updated_by == 1


def test_unknown_key_is_refused_not_created(db, settings_rows):
    """An agent inventing a key would write a setting nothing reads, and the
    user would believe it took effect."""
    tools = build_platform_tools(db, user_id=1, user_role="master_admin")
    result = _call(_tool(tools, "update_platform_setting"),
                   key="totally_made_up_key", value="1")

    assert "REFUSED" in result
    assert db.query(PlatformSetting).filter(
        PlatformSetting.key == "totally_made_up_key"
    ).first() is None


def test_type_is_preserved(db, settings_rows):
    """A bool silently becoming the string 'maybe' reads as truthy everywhere
    downstream — the worst kind of config bug."""
    tools = build_platform_tools(db, user_id=1, user_role="master_admin")

    assert "REFUSED" in _call(_tool(tools, "update_platform_setting"),
                              key="test_public_flag", value="maybe")
    assert "REFUSED" in _call(_tool(tools, "update_platform_setting"),
                              key="test_count", value="not-a-number")

    db.expire_all()
    assert db.query(PlatformSetting).filter(
        PlatformSetting.key == "test_public_flag").first().value == "false"


def test_users_listing_never_exposes_password_material(db):
    tools = build_platform_tools(db, user_id=1, user_role="master_admin")
    out = _call(_tool(tools, "list_platform_users"))

    assert "hashed_password" not in out
    assert "$2b$" not in out  # bcrypt prefix
