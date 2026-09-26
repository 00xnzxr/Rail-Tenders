"""The Platform Capability Manual: the instruction set the Master reads.

The Master Agent does not infer what the platform's functions are — it reads a
manual generated from the capability registry, grouped by domain, stating for
each capability what it does, when to use it, when NOT to, and what it
produces. Generated, so it cannot drift from what exists.
"""

from app.services.langchain.capability_registry import (
    CAPABILITIES,
    keys_for_surface,
    render_capability_manual,
)

_WORKERS = [
    {"agent_key": "deep_analyzer", "display_name": "Tender Doc Analyzer",
     "description": "Forensic analysis of tender documents."},
    {"agent_key": "costing_researcher", "display_name": "Costing Researcher",
     "description": "Produces a complete itemised cost breakdown."},
]


def test_the_manual_covers_every_capability_the_surface_can_reach():
    manual = render_capability_manual("master", "master_admin")
    for key in keys_for_surface("master", "master_admin"):
        assert key in manual, f"{key} missing from the manual"


def test_the_manual_is_grouped_by_domain():
    manual = render_capability_manual("master", "master_admin")
    used = {c.domain for c in CAPABILITIES.values() if "master" in c.surfaces}
    for domain in used:
        assert domain in manual, domain


def test_the_manual_states_when_not_to_use_a_capability():
    """'Not for' is what stops the Master pricing a schedule itself."""
    manual = render_capability_manual("master", "master_admin")
    assert manual.lower().count("not for") >= 5


def test_the_manual_withholds_what_the_role_cannot_have():
    admin = render_capability_manual("master", "admin")
    assert "update_platform_setting" not in admin
    assert "list_platform_users" not in admin


def test_workers_appear_with_their_descriptions():
    manual = render_capability_manual("master", "master_admin", include_workers=_WORKERS)
    assert "call_deep_analyzer" in manual
    assert "call_costing_researcher" in manual
    assert "Forensic analysis" in manual


def test_the_costing_doctrine_is_explicit():
    from app.services.langchain.capability_registry import DELEGATION_DOCTRINE

    for cue in ("bidding schedule", "BOQ", "every row", "call_costing_researcher"):
        assert cue.lower() in DELEGATION_DOCTRINE.lower(), cue
    # The doctrine explains WHY, not just what — that is what makes an
    # instruction survive a persuasive-looking shortcut.
    assert "collaps" in DELEGATION_DOCTRINE.lower()
