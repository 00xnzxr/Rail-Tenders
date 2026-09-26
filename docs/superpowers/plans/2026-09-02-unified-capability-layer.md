# Unified Capability Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one registry the single source of truth for every agent capability, give the Master Agent total platform reach plus an authored manual telling it what each capability is for, and make it the chat entrypoint — fixing three long-standing tool-distribution defects at their shared root.

**Architecture:** A frozen-dataclass registry describes every capability (target, risk tier, minimum role, surface, and authored manual text). `tool_loader`, `tool_policy`, `platform_tools`, `agent_tools_service` and the canonical graphs keep their public APIs but read their data from it. The Master Agent binds ~20 tools directly and reaches everything else through one validating `use_capability` dispatcher, guided by a Platform Capability Manual rendered from the registry into its prompt.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.x, Pydantic v2, LangChain / LangGraph (`create_react_agent`), RQ, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-unified-capability-layer-design.md` — read it before Task 1. It carries the measurements and the reasoning behind every decision here.

## Global Constraints

- Run everything from `drpl-backend/`. Python is `.venv/bin/python`; tests are `.venv/bin/python -m pytest`.
- **`.env` `DATABASE_URL` points at the LIVE production Neon database.** `tests/conftest.py` forces a throwaway SQLite file for the test session; never bypass it. Any manual verification script that writes must be run knowingly against production.
- `tool_policy.classify_tool()` must keep its signature and its behaviour, **including that an unknown tool classifies as a write**. `tests/test_tool_policy.py` and `tests/test_tool_policy_integration.py` are drift tests the Constitution forbids silencing; they must pass unmodified.
- Model selection goes through `llm_factory.get_chat_model(db, agent_name=...)`. Never hard-code a provider or model ID.
- Role checks are read from the database inside the tool at call time, never from the request. Build-time filtering is a convenience, not the control.
- All file IO goes through `app/services/storage_service.py`.
- New background entrypoints run inside `app/core/run_context.run_id_scope`.
- Every task ends with its tests green and one commit.

## Known Environment Quirks

Two things will waste your time if you meet them cold:

1. **Test schema.** `conftest.py` creates tables session-scoped, before a test module's imports have registered every model. A test that touches `platform_settings` or `agent_conversation_history` must start with:

```python
import app.models  # noqa: F401
import app.models.agent_memory  # noqa: F401 - not re-exported by app.models
from app.core.database import Base, engine

@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)
```

2. **Patching.** `streaming_handler` imports its collaborators *inside* the function. Patch the source module (`app.services.langchain.graphs.agent_router_graph.classify_intent_node`), not the attribute on `streaming_handler`. See `tests/test_general_assistant_routing.py` for a working example.

## File Structure

**Create:**
- `app/services/langchain/capability_registry.py` — the registry, the resolver, the manual renderer.
- `app/services/langchain/tools/cost_breakdown_tool.py` — `cost_breakdown_read`.
- `app/services/langchain/graphs/capability_dispatcher.py` — `use_capability`.
- `tests/test_capability_registry.py`, `tests/test_capability_parity.py`, `tests/test_master_agent_catalog.py`, `tests/test_chat_engine_switch.py`, `tests/test_confirm_gate_scope.py`, `tests/test_capability_manual.py`

**Modify:**
- `app/services/langchain/tool_policy.py` — tiers and summaries read from the registry.
- `app/services/langchain/tools/tool_loader.py` — classes read from the registry.
- `app/services/agent_tools_service.py` — `SYSTEM_TOOLS` generated.
- `app/services/langchain/canonical_registry.py` — `resolve_tool_keys` intersection removed.
- `app/services/langchain/graphs/document_analysis_agent.py`, `proposal_agent.py`, `enhanced_costing_agent.py` — stop passing hardcoded `default_keys`.
- `app/services/langchain/graphs/orchestrator_tools.py` — worker alias dedupe.
- `app/services/langchain/graphs/decision_maker_agent.py` — catalog from the registry, manual + doctrine in the prompt.
- `app/services/langchain/streaming_handler.py` — `stream_master_response` seam.
- `app/services/settings_service.py` — `chat_engine`, `confirm_gate_scope`.

---

### Task 1: The registry, data only

No consumer changes. This task adds the data and the tests that keep it honest.

**Files:**
- Create: `app/services/langchain/capability_registry.py`
- Test: `tests/test_capability_registry.py`

**Interfaces:**
- Produces: `Capability` (frozen dataclass), `CAPABILITIES: dict[str, Capability]`, `DOMAINS: tuple[str, ...]`, `get(key) -> Capability | None`, `keys_for_surface(surface, user_role) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_capability_registry.py
import importlib
import pytest
from app.services.langchain.capability_registry import (
    CAPABILITIES, DOMAINS, Capability, get, keys_for_surface,
)
from app.services.langchain.tool_policy import READ, WRITE, DESTRUCTIVE

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
        assert cap.surfaces <= VALID_SURFACES and cap.surfaces, key
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_capability_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: capability_registry`.

- [ ] **Step 3: Write the registry**

Define `Capability` exactly as in the spec's section 1. Define `DOMAINS` as the nine domains in the spec's table. Populate `CAPABILITIES` with **every** capability that exists today. Source them from:

- `tool_loader._TOOL_CLASS_REGISTRY` — 25 class-kind entries.
- `orchestrator_tools.build_diagnostic_tools` — `inspect_workspace`, `inspect_tender`, `inspect_session`, `list_recent_errors`, `check_annexure_extraction` (factory, domain `diagnostics`).
- `orchestrator_tools.build_action_tools` — `init_workspace_force`, `regenerate_checklist`, `regenerate_annexures`, `finalize_document`, `retry_failed_agent_run` (factory, domain `diagnostics`).
- `orchestrator_tools.build_llm_meta_tools` — `run_with_llm`, `web_search`, `document_reader`.
- `platform_tools` — `list_platform_settings`, `get_platform_health`, `list_recent_agent_runs` (`min_role="admin"`); `list_platform_users`, `update_platform_setting` (`min_role="master_admin"`).
- `quality_tools` — `diagnose_tender_outputs`.

Copy each tier from `tool_policy`'s existing sets, each `min_role` from `platform_tools`' specs, and each `summary` from `tool_policy._SUMMARIES` where one exists. **Where a summary does not exist, write one** — plain language, no tool names.

`purpose` / `use_when` / `not_for` / `produces` are newly authored. Write them for a reader who has never seen this platform. The costing entry must match the spec's section 2 wording.

`keys_for_surface(surface, user_role)` filters by `surface in cap.surfaces` and `platform_tools.role_allows(user_role, cap.min_role)`.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_capability_registry.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/services/langchain/capability_registry.py tests/test_capability_registry.py
git commit -m "feat(capabilities): one registry describing every agent capability"
```

---

### Task 2: `tool_policy` reads its tiers from the registry

Behaviour-preserving. The proof is that the untouched drift tests still pass.

**Files:**
- Modify: `app/services/langchain/tool_policy.py`
- Test: append to `tests/test_capability_registry.py`

**Interfaces:**
- Consumes: `CAPABILITIES` (Task 1).
- Produces: `classify_tool` and `summarize_action` unchanged in signature and behaviour.

- [ ] **Step 1: Write the failing test**

```python
def test_policy_tiers_come_from_the_registry():
    from app.services.langchain import tool_policy

    for key, cap in CAPABILITIES.items():
        assert tool_policy.classify_tool(key) == cap.tier, key


def test_an_unregistered_tool_is_still_gated_as_a_write():
    """Fail-safe. Do not weaken this: it is what makes forgetting to register a
    new write tool harmless."""
    from app.services.langchain.tool_policy import WRITE, classify_tool

    assert classify_tool("brand_new_unclassified_tool") == WRITE


def test_delegation_is_still_read_by_prefix():
    from app.services.langchain.tool_policy import READ, classify_tool

    assert classify_tool("call_costing_researcher") == READ
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_capability_registry.py -k policy -v`

- [ ] **Step 3: Implement**

Replace the bodies of `_READ_TOOLS` / `_WRITE_TOOLS` / `_DESTRUCTIVE_TOOLS` with sets derived from `CAPABILITIES` at import time. Keep the module-level names — other modules and tests import them. Keep the `call_*` prefix rule and the unknown-tool-is-a-write default exactly as they are. `_SUMMARIES` falls back to `CAPABILITIES[key].summary`.

Import lazily inside a function if a circular import appears: `capability_registry` must not import `tool_policy` at module scope for its constants — copy the three tier strings locally if needed.

- [ ] **Step 4: Verify the drift tests still pass, unmodified**

```bash
.venv/bin/python -m pytest tests/test_tool_policy.py tests/test_tool_policy_integration.py tests/test_confirm_gate_e2e.py tests/test_capability_registry.py -v
git diff --stat tests/test_tool_policy.py tests/test_tool_policy_integration.py   # must be empty
```

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(policy): risk tiers come from the capability registry"
```

---

### Task 3: `tool_loader` and `SYSTEM_TOOLS` read from the registry

**Files:**
- Modify: `app/services/langchain/tools/tool_loader.py`, `app/services/agent_tools_service.py`
- Test: append to `tests/test_capability_registry.py`

- [ ] **Step 1: Write the failing tests**

```python
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


def test_db_backed_tools_still_get_a_session(db):
    """Regression: hasattr(cls,'db') is False for Pydantic v2 fields, so every
    tool once ran with db=None and web_search silently used DuckDuckGo."""
    from app.services.langchain.tools.tool_loader import load_tools_by_keys

    tool = load_tools_by_keys(db, ["web_search"], agent_key="test")[0]
    assert tool.db is not None
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

`_ensure_registry()` populates `_TOOL_CLASS_REGISTRY` by importing each `kind="class"` capability's `target`. Keep `_wants_db()` exactly as it is — it checks `model_fields`, and that is deliberate. `SYSTEM_TOOLS` is generated from the registry: `tool_key`, `display_name`, `description` (use `purpose`), `tool_type` (use `domain`), `handler_module` (the target's module), `config_schema` preserved from the existing literal where one exists.

- [ ] **Step 4: Run the affected suites**

```bash
.venv/bin/python -m pytest tests/test_capability_registry.py tests/test_agent_tool_sync.py tests/test_effective_tools.py tests/test_tool_db_injection.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -am "refactor(tools): the loader and the assignable list come from the registry"
```

---

### Task 4: Kill the hardcoded graph lists and the intersection filter

This is deferred defects #1 and #2.

**Files:**
- Modify: `app/services/langchain/canonical_registry.py` (`resolve_tool_keys`), `app/services/langchain/graphs/document_analysis_agent.py:~430`, `proposal_agent.py:~86`, `enhanced_costing_agent.py:~1080`
- Test: `tests/test_capability_parity.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_capability_parity.py
import app.models  # noqa: F401
import pytest
from app.core.database import Base, engine


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


def test_deep_analyzer_runs_the_tools_the_registry_declares(db):
    """It ran 4 tools while canonical_registry declared 6 — web_search and
    memory_store were declared and never passed."""
    from app.services.langchain.canonical_registry import (
        CANONICAL_AGENTS, resolve_tool_keys,
    )

    declared = set(CANONICAL_AGENTS["tender_doc_analyzer"]["default_tools"])
    resolved, _src = resolve_tool_keys(db, "tender_doc_analyzer")
    assert set(resolved) == declared


def test_an_assigned_tool_is_never_silently_dropped(db):
    """resolve_tool_keys intersected the user's Agent Builder choice with the
    graph's hardcoded list, so an assignment could vanish with only a log line."""
    from app.models.agent_builder import AgentTool, CustomAgent
    from app.services.langchain.canonical_registry import resolve_tool_keys

    row = db.query(AgentTool).filter(AgentTool.tool_key == "xlsx_generator").first()
    agent = db.query(CustomAgent).filter(
        CustomAgent.agent_key == "tender_doc_analyzer"
    ).first()
    agent.is_user_customized = True
    agent.tools = [{"tool_id": row.id, "config": {}}]
    db.commit()

    resolved, source = resolve_tool_keys(db, "tender_doc_analyzer")
    assert "xlsx_generator" in resolved
    assert source == "user_customized"


def test_an_unknown_assigned_key_is_reported_not_dropped(db):
    from app.services.langchain.canonical_registry import resolve_unknown_tool_keys

    unknown = resolve_unknown_tool_keys(["web_search", "not_a_real_tool"])
    assert unknown == ["not_a_real_tool"]
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

Change `resolve_tool_keys(db, agent_key, default_keys=None)`: `default_keys` becomes optional and defaults to `CANONICAL_AGENTS[agent_key]["default_tools"]`. **Delete the intersection.** An explicit user assignment wins whole; validate each key against the registry and return unknown keys separately via the new `resolve_unknown_tool_keys(keys) -> list[str]` so the API layer can surface them in Agent Builder instead of logging into the void.

Then delete the `default_keys=[...]` literals at the three graph call sites so each takes the registry's answer.

- [ ] **Step 4: Run the affected suites**

```bash
.venv/bin/python -m pytest tests/test_capability_parity.py tests/test_effective_tools.py tests/test_agent_tool_sync.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -am "fix(agents): a tool the registry declares is a tool the agent gets"
```

---

### Task 5: `cost_breakdown_read`, and the worker alias dedupe

**Files:**
- Create: `app/services/langchain/tools/cost_breakdown_tool.py`
- Modify: `app/services/langchain/capability_registry.py`, `app/services/langchain/graphs/orchestrator_tools.py`
- Test: `tests/test_master_agent_catalog.py`

**Interfaces:**
- Produces: `CostBreakdownReadTool` (`name = "cost_breakdown_read"`), and `orchestrator_tools.WORKER_ALIASES: dict[str, str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_master_agent_catalog.py
import app.models  # noqa: F401
import pytest
from app.core.database import Base, engine


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


def test_a_stored_costing_can_be_read_back(db):
    """cost_calculator computes and xlsx_generator exports; nothing read back
    what was saved, which is why 'why did this costing come out this way' could
    only ever be answered rhetorically."""
    from app.models.cost_breakdown import CostBreakdown, CostBreakdownLine
    from app.services.langchain.tools.cost_breakdown_tool import CostBreakdownReadTool

    cb = CostBreakdown(tender_id=4242, version=1, status="draft",
                       overhead_percent=10.0, margin_percent=15.0, gst_percent=18.0,
                       subtotal=1000.0, grand_total=1298.0)
    db.add(cb)
    db.flush()
    db.add(CostBreakdownLine(cost_breakdown_id=cb.id, sr_no=1,
                             description="SS 304 trough", quantity=2, unit="no",
                             rate=500.0, amount=1000.0))
    db.commit()

    out = CostBreakdownReadTool(db=db)._run(tender_id=4242)
    assert "SS 304 trough" in out
    assert "1298" in out.replace(",", "")


def test_the_worker_roster_has_no_duplicate_specialists(db):
    """22 call_* tools included four aliases for one analyzer. Duplicate tools
    with near-identical descriptions are how a model picks the wrong one."""
    from app.services.langchain.graphs.orchestrator_tools import list_worker_agents

    keys = [w["agent_key"] for w in list_worker_agents(db)]
    assert len(keys) == len(set(keys))
    analyzers = {"deep_analyzer", "tender_doc_analyzer",
                 "document_analyzer", "tender_analysis"} & set(keys)
    assert len(analyzers) <= 1, f"still exposing aliases: {analyzers}"
    assert len(keys) <= 12, f"roster is still bloated: {len(keys)}"
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

`CostBreakdownReadTool`: input `tender_id: int`, optional `version: int`. Reads the newest `CostBreakdown` for that tender (or the named version), returns a markdown table of `CostBreakdownLine` rows (sr_no, description, quantity, unit, rate, amount) plus the totals block and `needs_input_count`. Use `db: Optional[Session] = None` as a Pydantic field so `_wants_db` injects it. Register it: domain `costing`, tier `READ`, surfaces `{"master", "generalist", "specialist"}`.

`WORKER_ALIASES` maps each alias to its canonical key (`tender_doc_analyzer` → `deep_analyzer`, `document_analyzer` → `deep_analyzer`, `tender_analysis` → `deep_analyzer`, `checklist` → `checklist_generator`, `proposal` → `proposal_creator`). `list_worker_agents` collapses aliases, keeping the canonical row, and continues to exclude `_NON_WORKER_AGENT_KEYS` and `TOOL_FREE_AGENTS`.

- [ ] **Step 4: Run**

```bash
.venv/bin/python -m pytest tests/test_master_agent_catalog.py tests/test_general_assistant_tools.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(capabilities): read a stored costing; stop exposing four names for one analyzer"
```

---

### Task 6: The Platform Capability Manual

**Files:**
- Modify: `app/services/langchain/capability_registry.py`
- Test: `tests/test_capability_manual.py`

**Interfaces:**
- Produces: `render_capability_manual(surface: str, user_role: str, *, include_workers: list[dict] | None = None) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_capability_manual.py
from app.services.langchain.capability_registry import (
    CAPABILITIES, DOMAINS, render_capability_manual,
)


def test_the_manual_covers_every_capability_the_surface_can_reach():
    manual = render_capability_manual("master", "master_admin")
    from app.services.langchain.capability_registry import keys_for_surface

    for key in keys_for_surface("master", "master_admin"):
        assert key in manual, f"{key} missing from the manual"


def test_the_manual_is_grouped_by_domain():
    manual = render_capability_manual("master", "master_admin")
    used = {c.domain for c in CAPABILITIES.values() if "master" in c.surfaces}
    for domain in used:
        assert domain in manual


def test_the_manual_states_when_not_to_use_a_capability():
    """'Not for' is what stops the Master pricing a schedule itself."""
    manual = render_capability_manual("master", "master_admin")
    assert manual.lower().count("not for") >= 5


def test_the_manual_withholds_what_the_role_cannot_have():
    admin = render_capability_manual("master", "admin")
    assert "update_platform_setting" not in admin


def test_the_costing_doctrine_is_explicit():
    manual = render_capability_manual("master", "master_admin")
    assert "call_costing_researcher" in manual
    for cue in ("bidding schedule", "BOQ", "every"):
        assert cue.lower() in manual.lower(), cue
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

Render markdown grouped by domain. Per capability:

```
- **<key>** — <purpose>
  - Use when: <use_when>
  - Not for: <not_for>
  - Produces: <produces>
```

Worker `call_*` entries come from `include_workers` (the deduped roster) and use the registry's `delegation`-domain text where present, falling back to the worker's own description.

- [ ] **Step 4: Run**

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(capabilities): a manual the Master reads instead of guessing"
```

---

### Task 7: The `use_capability` dispatcher

**Files:**
- Create: `app/services/langchain/graphs/capability_dispatcher.py`
- Test: append to `tests/test_master_agent_catalog.py`

**Interfaces:**
- Produces: `build_capability_dispatcher(db, *, user_id, user_role, surface, context) -> BaseTool` named `use_capability`.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_dispatcher_lists_tier_two_capabilities_in_its_description(db):
    from app.services.langchain.graphs.capability_dispatcher import (
        build_capability_dispatcher,
    )

    tool = build_capability_dispatcher(db, user_id=1, user_role="master_admin",
                                       surface="master", context={})
    assert "ratecard_lookup" in tool.description
    assert "platform_ops" in tool.description


def test_an_unknown_key_returns_guidance_not_a_crash(db):
    from app.services.langchain.graphs.capability_dispatcher import (
        build_capability_dispatcher,
    )

    tool = build_capability_dispatcher(db, user_id=1, user_role="master_admin",
                                       surface="master", context={})
    out = tool._run(key="nope", args={})
    assert "nope" in out and "available" in out.lower()


def test_bad_arguments_return_the_expected_shape(db):
    from app.services.langchain.graphs.capability_dispatcher import (
        build_capability_dispatcher,
    )

    tool = build_capability_dispatcher(db, user_id=1, user_role="master_admin",
                                       surface="master", context={})
    out = tool._run(key="ratecard_lookup", args={"wrong_field": 1})
    assert "ratecard_lookup" in out
    assert "expects" in out.lower()


def test_the_dispatcher_is_not_a_way_around_the_role_check(db):
    from app.services.langchain.graphs.capability_dispatcher import (
        build_capability_dispatcher,
    )

    tool = build_capability_dispatcher(db, user_id=1, user_role="admin",
                                       surface="master", context={})
    assert "update_platform_setting" not in tool.description
    out = tool._run(key="update_platform_setting", args={"key": "x", "value": "y"})
    assert "administrator" in out.lower()


def test_the_dispatcher_is_not_a_way_around_the_write_gate(db):
    """A gated write called through the dispatcher must still suspend."""
    from app.services.langchain.graphs.capability_dispatcher import (
        build_capability_dispatcher,
    )
    from app.services.langchain.tool_policy import PendingActionCapture, policy_scope

    tool = build_capability_dispatcher(db, user_id=1, user_role="master_admin",
                                       surface="master", context={})
    capture = PendingActionCapture()
    with policy_scope(capture=capture):
        tool._run(key="finalize_document", args={"document_id": 1})
    assert capture.is_pending()
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

Build the underlying tool through the same resolver Tier 1 uses, wrap it with `tool_policy.wrap_tools_with_policy`, and invoke it. The description is generated: a domain-grouped index of Tier 2 keys with their `purpose` only. Validate `args` against the resolved tool's `args_schema` and return `f"{key} expects: {fields}"` on a mismatch. Refuse an out-of-role key with `platform_tools._refusal(min_role)`.

- [ ] **Step 4: Run**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(capabilities): total reach without a fifty-tool catalog"
```

---

### Task 8: The Master Agent's catalog and prompt

**Files:**
- Modify: `app/services/langchain/graphs/decision_maker_agent.py`
- Test: append to `tests/test_capability_parity.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_master_loses_no_capability_it_had(db):
    """Parity by enumeration, not by review: the new catalog must be a superset
    of the old one unioned with the general assistant's belt."""
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_catalog,
    )
    from app.services.langchain.graphs.general_assistant_agent import (
        GENERAL_ASSISTANT_TOOL_KEYS,
    )

    reachable = build_master_catalog(
        db, user_id=1, user_role="master_admin", context={}, reachable_only=True,
    )
    previously = {
        "inspect_workspace", "inspect_tender", "inspect_session",
        "list_recent_errors", "check_annexure_extraction",
        "init_workspace_force", "regenerate_checklist", "regenerate_annexures",
        "finalize_document", "retry_failed_agent_run",
        "run_with_llm", "web_search", "document_reader",
        "list_platform_settings", "get_platform_health", "list_recent_agent_runs",
        "list_platform_users", "update_platform_setting",
        "diagnose_tender_outputs",
    }
    missing = (previously | set(GENERAL_ASSISTANT_TOOL_KEYS)) - set(reachable)
    assert not missing, f"capability lost in the move: {sorted(missing)}"


def test_the_bound_catalog_stays_small(db):
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_catalog,
    )

    bound = build_master_catalog(db, user_id=1, user_role="master_admin", context={})
    assert len(bound) <= 24, f"bound catalog is {len(bound)} tools"
    names = {t.name for t in bound}
    assert "use_capability" in names
    assert "call_costing_researcher" in names
    assert "tender_lookup" in names
    assert "cost_breakdown_read" in names
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

Add `build_master_catalog(db, *, user_id, user_role, context, reachable_only=False)`. With `reachable_only=True` it returns the key list Tier 1 ∪ Tier 2; otherwise it returns bound `BaseTool` instances for Tier 1 plus the dispatcher. `run_decision_maker` calls it in place of the five inline builders.

Prepend to `DECISION_MAKER_AUTONOMOUS_SYSTEM` and `DECISION_MAKER_PLANNING_SYSTEM`:
1. `render_capability_manual("master", user_role, include_workers=roster)`;
2. the delegation doctrine from the spec's section 3, verbatim.

- [ ] **Step 4: Run**

```bash
.venv/bin/python -m pytest tests/test_capability_parity.py tests/test_platform_tools.py tests/test_quality_tools.py tests/test_global_assistant.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(master): total platform reach, and a manual telling it what everything is for"
```

---

### Task 9: `chat_engine` — the Master becomes the entrypoint

**Files:**
- Modify: `app/services/langchain/streaming_handler.py`, `app/services/settings_service.py`
- Test: `tests/test_chat_engine_switch.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chat_engine_switch.py
import asyncio
import app.models  # noqa: F401
import app.models.agent_memory  # noqa: F401
import pytest
from unittest.mock import AsyncMock, patch
from app.core.database import Base, engine
from app.services.langchain import streaming_handler as sh


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


async def _drain(agen):
    return [e async for e in agen]


def test_chat_engine_master_reaches_the_master_agent(db):
    called = {}

    async def fake_master(*a, **k):
        called["yes"] = True
        return {"output": "ok", "output_type": "decision_maker_trace",
                "agent_key": "decision_maker", "tool_calls": [], "trace": [],
                "metrics": {}, "status": "completed"}

    with patch.object(sh, "_fresh_db", return_value=db), \
         patch.object(sh, "get_effective_setting", return_value="master"), \
         patch("app.services.langchain.graphs.decision_maker_agent.run_decision_maker",
               new=fake_master):
        asyncio.run(_drain(sh.stream_router_response(
            db, message="hello", session_id="s", user_id=1)))

    assert called.get("yes"), "chat did not reach the Master Agent"


def test_chat_engine_router_restores_the_old_path_exactly(db):
    classified = {}

    async def fake_classify(state, db_):
        classified["yes"] = True
        return {"intent": "general_query", "selected_agents": [], "metadata": {}}

    with patch.object(sh, "_fresh_db", return_value=db), \
         patch.object(sh, "get_effective_setting", return_value="router"), \
         patch("app.services.langchain.graphs.agent_router_graph.classify_intent_node",
               new=fake_classify), \
         patch("app.services.langchain.graphs.general_assistant_agent.run_general_assistant",
               new=AsyncMock(return_value={"output": "ok", "output_type": "general",
                                           "agent_key": "general_assistant",
                                           "tool_calls": [], "sources": [],
                                           "status": "completed"})):
        asyncio.run(_drain(sh.stream_router_response(
            db, message="hello", session_id="s", user_id=1)))

    assert classified.get("yes"), "the router kill switch did not restore the old path"
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

Add the `chat_engine` setting to `settings_service` (`value_type` `str`, default `"master"`, category `ai`, description naming the kill switch). `stream_router_response` keeps its signature and dispatches on it; the Master branch delegates to `stream_master_response`, which reuses the existing decision-maker SSE branch — plan cards, `agent_status`, `token`, `token_reset`, persistence to both `AgentConversationHistory` and `ProposalMessage`.

Remove `general_assistant` from `_NON_WORKER_AGENT_KEYS` so the Master can delegate cheap conversational work to it; keep the guard that stops it appearing in its **own** roster.

- [ ] **Step 4: Run**

```bash
.venv/bin/python -m pytest tests/test_chat_engine_switch.py tests/test_general_assistant_routing.py tests/test_chat_stream_first_byte.py tests/test_detached_stream.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(chat): the Master Agent answers, with chat_engine=router as the way back"
```

---

### Task 10: `confirm_gate_scope`, and an audit trail for what it lets through

**Files:**
- Modify: `app/services/langchain/tool_policy.py`, `app/services/settings_service.py`
- Test: `tests/test_confirm_gate_scope.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_confirm_gate_scope.py
import app.models  # noqa: F401
import pytest
from app.core.database import Base, engine
from app.services.langchain.tool_policy import (
    PendingActionCapture, policy_scope, should_gate,
)


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


def test_destructive_only_lets_an_ordinary_write_through():
    assert should_gate("regenerate_checklist", scope="destructive_only") is False
    assert should_gate("xlsx_generator", scope="destructive_only") is False


def test_destructive_only_still_stops_the_destructive_ones():
    assert should_gate("finalize_document", scope="destructive_only") is True
    assert should_gate("init_workspace_force", scope="destructive_only") is True


def test_all_writes_restores_todays_behaviour():
    assert should_gate("regenerate_checklist", scope="all_writes") is True


def test_reads_are_never_gated_under_either_scope():
    for scope in ("destructive_only", "all_writes"):
        assert should_gate("web_search", scope=scope) is False
        assert should_gate("call_costing_researcher", scope=scope) is False


def test_an_ungated_write_is_audited(db, caplog):
    """The prompt is being traded for a log. The log has to be complete."""
    from app.services.langchain.graphs.orchestrator_tools import _log_audit

    with policy_scope(capture=PendingActionCapture()):
        _log_audit(1, "regenerate_checklist", {"tender_id": 7}, "ungated")
    assert "regenerate_checklist" in caplog.text
```

- [ ] **Step 2: Run to verify it fails**

- [ ] **Step 3: Implement**

Add `should_gate(tool_name, *, scope)` to `tool_policy`: `READ` never gates; `DESTRUCTIVE` always gates; `WRITE` gates only when `scope == "all_writes"`. `wrap_tools_with_policy` reads the scope once per run via `settings_service.get_effective_setting(db, "confirm_gate_scope", "destructive_only")` and calls `should_gate`. `ASSISTANT_CONFIRM_GATE_ENABLED=false` still disables everything. Every write that proceeds ungated calls `_log_audit(user_id, tool, args, "ungated")`.

- [ ] **Step 4: Run**

```bash
.venv/bin/python -m pytest tests/test_confirm_gate_scope.py tests/test_confirm_gate_e2e.py tests/test_tool_policy.py tests/test_tool_policy_integration.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -am "feat(policy): confirm_gate_scope, with an audit line for every write it lets through"
```

---

### Task 11: Full verification and rollout

- [ ] **Step 1: Whole suite**

```bash
.venv/bin/python -m pytest tests/ -q
```

Expected: all pass. `tests/test_boq_progress.py::test_chunk_loop_reports_progress` fails on `main` today and is **not** caused by this work — confirm it is the only failure and leave it alone.

- [ ] **Step 2: Frontend untouched, but verify the contract still holds**

```bash
cd ../drpl-frontend && npm test && npx tsc -b
```

- [ ] **Step 3: End-to-end against the running servers**

Start `uvicorn app.main:app --reload --port 8000` and mint a token:

```bash
.venv/bin/python -c "
from app.core.auth import create_access_token
print(create_access_token(2, 'admin@drppl.com'))"
```

Then, against the global assistant session from `GET /api/command-center/assistant/session`, send each of these and confirm the described behaviour:

| Message | Expected |
|---|---|
| "Can you check the latest run of tender in the command center?" | Real rows from the database; no "I don't have access" |
| "What can you do?" | Answers from the capability manual, naming real capabilities |
| "Build the full costing for tender \<id with a BOQ\>" | Delegates: `call_costing_researcher` appears in the trace |
| "Why is the rate on line 3 what it is?" | Reads with `cost_breakdown_read`; does **not** delegate |
| "Regenerate the checklist for tender \<id\>" | Runs without a confirmation card; an audit line is logged |
| "Finalize document \<id\>" | Still raises the confirmation card |

- [ ] **Step 4: Watch the first day**

`GET /health/capacity` for queue depth and 429s; the Anthropic console for Opus spend. If cost is worse than the benefit, set `master_agent_model` to a cheaper tier, or `chat_engine=router` to revert entirely — both from the admin UI, no deploy.

- [ ] **Step 5: Update CLAUDE.md and commit**

Document: the registry as the single source of truth; the two-tier catalog and why; `chat_engine` and `confirm_gate_scope` as the two kill switches; and that adding a capability now means one registry entry with manual text, not four edits.

---

## Self-Review

**Spec coverage:** registry → Tasks 1–3; deferred defects #1 and #2 → Task 4; Master reach and dedupe → Task 5; manual → Task 6; two-tier catalog → Task 7; Master prompt and parity → Task 8; deferred defect #3 (unreachable Master) → Task 9; gate scope → Task 10; verification → Task 11.

**Placeholders:** none. Every step names exact files, exact symbols, and runnable commands. The two `<id>` slots in Task 11 are runtime values the operator supplies, not unwritten content.

**Type consistency:** `Capability`, `CAPABILITIES`, `keys_for_surface`, `render_capability_manual`, `build_capability_dispatcher`, `build_master_catalog`, `should_gate` and `resolve_unknown_tool_keys` are named identically wherever they appear across tasks.

**Ordering:** each task leaves the suite green. Tasks 1–3 change no behaviour, 4–8 add capability, 9 flips the entrypoint behind a kill switch, 10 changes the gate. Stopping after any task leaves a working platform.
