"""The capability registry: one source of truth for what agents can do.

Before this module existed, capability was decided in four places that had
diverged: tool_loader's class registry, each canonical graph's hardcoded
default_keys, the intersection filter in resolve_tool_keys, and five inline
builders in run_decision_maker. Three separate defects traced back to that one
cause. These tests keep the single source honest.
"""

import importlib

from app.services.langchain.capability_registry import (
    CAPABILITIES,
    DOMAINS,
    get,
    keys_for_surface,
)
from app.services.langchain.tool_policy import DESTRUCTIVE, READ, WRITE

VALID_TIERS = {READ, WRITE, DESTRUCTIVE}
VALID_ROLES = {"operator", "admin", "master_admin"}
VALID_SURFACES = {"master", "generalist", "specialist"}


def test_every_capability_is_well_formed():
    assert CAPABILITIES, "registry is empty"
    for key, cap in CAPABILITIES.items():
        assert cap.key == key, f"{key} disagrees with its own key"
        assert cap.kind in ("class", "factory"), key
        assert ":" in cap.target, f"{key}: target must be 'module:Attr'"
        assert cap.tier in VALID_TIERS, key
        assert cap.min_role in VALID_ROLES, key
        assert cap.surfaces and cap.surfaces <= VALID_SURFACES, key
        assert cap.domain in DOMAINS, f"{key}: unknown domain {cap.domain}"


def test_every_capability_has_a_manual_entry():
    """The Master reasons from authored text, not from a tool name. A capability
    with no manual is one it will misuse."""
    for key, cap in CAPABILITIES.items():
        for field in ("purpose", "use_when", "not_for", "produces", "summary"):
            value = getattr(cap, field)
            assert value and value.strip(), f"{key}: {field} is empty"
            assert len(value) > 15, f"{key}: {field} is too thin to be useful"


def test_every_target_is_importable():
    for key, cap in CAPABILITIES.items():
        module_path, attr = cap.target.split(":")
        module = importlib.import_module(module_path)
        assert hasattr(module, attr), f"{key}: {cap.target} does not exist"


def test_summaries_are_written_for_humans():
    """The confirmation card is read by people who do not know what a tool is."""
    for key, cap in CAPABILITIES.items():
        assert "_" not in cap.summary, f"{key}: summary leaks a tool name"


def test_surface_filtering_respects_role():
    master_admin = set(keys_for_surface("master", "master_admin"))
    admin = set(keys_for_surface("master", "admin"))
    assert "update_platform_setting" in master_admin
    assert "update_platform_setting" not in admin
    assert admin < master_admin


def test_get_returns_none_for_unknown():
    assert get("no_such_capability") is None


def test_policy_tiers_come_from_the_registry():
    from app.services.langchain import tool_policy

    for key, cap in CAPABILITIES.items():
        assert tool_policy.classify_tool(key) == cap.tier, key


def test_an_unregistered_tool_is_still_gated_as_a_write():
    """Fail-safe. Do not weaken this: it is what makes forgetting to register a
    new write tool harmless."""
    from app.services.langchain.tool_policy import classify_tool

    assert classify_tool("brand_new_unclassified_tool") == WRITE


def test_delegation_is_still_read_by_prefix():
    from app.services.langchain.tool_policy import classify_tool

    assert classify_tool("call_costing_researcher") == READ


def test_confirmation_card_summaries_fall_back_to_the_registry():
    from app.services.langchain.tool_policy import summarize_action

    for key, cap in CAPABILITIES.items():
        if cap.tier in (WRITE, DESTRUCTIVE):
            text = summarize_action(key, {})
            assert text and "_" not in text, key


def test_policy_sets_are_derived_not_duplicated():
    """The point of the refactor: tool_policy's sets must BE the registry's
    tiers, not a parallel copy that happens to agree today. Strict equality is
    what makes a registry-only addition classify correctly with no second edit."""
    from app.services.langchain import tool_policy

    by_tier = {READ: set(), WRITE: set(), DESTRUCTIVE: set()}
    for key, cap in CAPABILITIES.items():
        by_tier[cap.tier].add(key)

    assert tool_policy._READ_TOOLS == by_tier[READ]
    assert tool_policy._WRITE_TOOLS == by_tier[WRITE]
    assert tool_policy._DESTRUCTIVE_TOOLS == by_tier[DESTRUCTIVE]


def test_loader_registry_matches_the_capability_registry():
    from app.services.langchain.tools.tool_loader import get_available_tool_keys

    loadable = set(get_available_tool_keys())
    class_kind = {k for k, c in CAPABILITIES.items() if c.kind == "class"}
    assert loadable == class_kind


def test_every_runnable_capability_is_assignable():
    """A capability that can run but cannot be assigned is invisible in Agent
    Builder — which is how costing_researcher showed 'Assigned Tools (0)'."""
    from app.services.agent_tools_service import SYSTEM_TOOLS

    seeded = {t["tool_key"] for t in SYSTEM_TOOLS}
    class_kind = {k for k, c in CAPABILITIES.items() if c.kind == "class"}
    assert class_kind <= seeded, f"not assignable: {sorted(class_kind - seeded)}"
