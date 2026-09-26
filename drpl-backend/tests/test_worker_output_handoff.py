"""What the Master is handed when a worker finishes.

The Master's message is what the user reads. Everything a delegation drops on
the way back is therefore something the platform computed correctly and then
did not say — which is exactly how "the agent is not providing correct or
detailed output" looked from the outside.

Two defects are pinned here:

1. The delegation returned ``result["output"][:1500]`` under the name
   ``output_preview``. A forensic analysis or an item-wise costing runs to tens
   of thousands of characters; the Master saw the opening paragraphs and
   summarized those.
2. ``_safe_dump`` sliced the *serialized JSON* at 8,000 characters, so a large
   ``structured_data`` reached the model as JSON cut mid-token — a rate with
   its digits missing, an array with no closing bracket — and was read as fact.
"""

import json

import pytest

from app.services.langchain.graphs import orchestrator_tools as ot


# ── the JSON always parses ──────────────────────────────────────────────────


def _big_costing_payload():
    return {
        "status": "completed",
        "agent_key": "costing_researcher",
        "output_type": "cost_breakdown",
        "output": "x" * 1500,
        "structured_data": {
            "line_items": [
                {"desc": f"item {i}", "qty": i, "rate": 1234.56} for i in range(200)
            ]
        },
    }


def test_an_oversized_result_is_still_valid_json():
    """The old slice cut mid-token; this is the bug's direct signature."""
    dumped = ot._safe_dump(_big_costing_payload())
    assert len(dumped) <= ot._TOOL_RESULT_LIMIT
    json.loads(dumped)  # would raise on the sliced string


def test_trimming_says_what_it_cut():
    """A silently shortened result is one the model reports as complete."""
    dumped = ot._safe_dump(_big_costing_payload())
    assert "dropped to fit" in dumped or "trimmed to fit" in dumped


def test_a_container_is_replaced_whole_not_halved():
    """Half a line-item array reads as the whole breakdown."""
    parsed = json.loads(ot._safe_dump(_big_costing_payload()))
    assert not isinstance(parsed["structured_data"], (list, dict))


def test_a_small_result_is_untouched():
    payload = {"status": "completed", "output": "short"}
    assert json.loads(ot._safe_dump(payload)) == payload


def test_an_unserializable_payload_returns_valid_json():
    class Unserializable:
        def __repr__(self):
            raise RuntimeError("boom")

    dumped = ot._safe_dump({"x": Unserializable()})
    assert json.loads(dumped)["status"] == "failed"


# ── the worker's answer survives the handoff ────────────────────────────────


def _analysis_result(chars):
    return {
        "status": "completed",
        "output": "A" * chars,
        "output_type": "document_analysis",
        "structured_data": None,
    }


def test_a_whole_answer_comes_through_whole():
    """A 12,000-character analysis used to arrive as 1,500."""
    payload = ot._worker_result_payload("deep_analyzer", _analysis_result(12000), None)
    assert len(payload["output"]) == 12000
    assert payload["output_complete"] is True
    assert "output_handle" not in payload


def test_the_field_is_not_called_a_preview():
    """Naming it `preview` told the model a stub was all there was."""
    payload = ot._worker_result_payload("deep_analyzer", _analysis_result(500), None)
    assert "output" in payload
    assert "output_preview" not in payload


def test_an_over_budget_answer_says_so_and_keeps_the_rest():
    store = ot.WorkerOutputStore()
    budget = ot._worker_output_budget()
    payload = ot._worker_result_payload(
        "deep_analyzer", _analysis_result(budget * 2), store
    )

    assert payload["output_complete"] is False
    assert payload["output_total_chars"] == budget * 2
    assert payload["next_offset"] == budget
    assert store[payload["output_handle"]] == "A" * (budget * 2)
    assert "read_worker_output" in payload["how_to_continue"]


def test_the_reader_pages_through_the_remainder():
    store = ot.WorkerOutputStore()
    budget = ot._worker_output_budget()
    payload = ot._worker_result_payload(
        "deep_analyzer",
        {"status": "completed", "output": "A" * budget + "B" * 10, "output_type": "x"},
        store,
    )
    reader = ot.build_worker_output_reader(store)

    result = json.loads(
        reader._run(handle=payload["output_handle"], offset=payload["next_offset"])
    )
    assert result["output"] == "B" * 10
    assert result["output_complete"] is True
    assert result["next_offset"] == budget + 10


def test_the_reader_names_the_handles_it_knows():
    """An unknown handle must be a correction the model can act on."""
    store = ot.WorkerOutputStore()
    store.put("deep_analyzer", "text")
    result = json.loads(ot.build_worker_output_reader(store)._run(handle="nope"))
    assert result["status"] == "failed"
    assert "deep_analyzer:1" in result["error"]


def test_the_store_is_bounded():
    """A long run must not accumulate megabytes for a remainder nobody reads."""
    store = ot.WorkerOutputStore()
    for i in range(ot._MAX_STORED_WORKER_OUTPUTS + 5):
        store.put("deep_analyzer", f"output {i}")
    assert len(store) == ot._MAX_STORED_WORKER_OUTPUTS


# ── structured_data gets its own budget ─────────────────────────────────────


def test_large_structured_data_is_omitted_with_a_note_not_a_number():
    result = {
        "status": "completed",
        "output": "narrative",
        "output_type": "cost_breakdown",
        "structured_data": {
            "line_items": [
                {"desc": f"item {i}", "qty": i, "rate": 1234.56} for i in range(600)
            ]
        },
    }
    payload = ot._worker_result_payload("costing_researcher", result, None)

    assert payload["structured_data"] is None
    assert payload["structured_data_omitted_chars"] > ot._STRUCTURED_DATA_BUDGET
    assert "cost_breakdown_read" in payload["structured_data_note"]


def test_structured_data_within_budget_is_kept():
    result = {
        "status": "completed",
        "output": "narrative",
        "output_type": "cost_breakdown",
        "structured_data": {"line_items": [{"desc": "a", "qty": 1, "rate": 2.0}]},
    }
    payload = ot._worker_result_payload("costing_researcher", result, None)
    assert payload["structured_data"] == result["structured_data"]


def test_the_whole_worker_payload_fits_its_own_limit_and_parses():
    """Output at budget and structured_data at budget together, serialized."""
    result = {
        "status": "completed",
        "output": "A" * (ot._worker_output_budget() * 2),
        "output_type": "cost_breakdown",
        "structured_data": {"rows": ["r" * 100 for _ in range(50)]},
    }
    store = ot.WorkerOutputStore()
    dumped = ot._safe_dump(
        ot._worker_result_payload("costing_researcher", result, store),
        limit=ot._worker_payload_limit(),
    )
    parsed = json.loads(dumped)
    assert len(dumped) <= ot._worker_payload_limit()
    assert len(parsed["output"]) == ot._worker_output_budget()
    assert parsed["structured_data"] == result["structured_data"]


# ── the Master can actually reach the reader ────────────────────────────────


def test_the_master_catalog_binds_the_reader_over_the_same_store(db):
    """A reader wired to a different store reports every handle as unknown."""
    from app.services.langchain.graphs.decision_maker_agent import (
        build_master_catalog,
    )

    tools = build_master_catalog(
        db, user_id=1, user_role="master_admin", context={},
    )
    readers = [t for t in tools if t.name == "read_worker_output"]
    assert len(readers) == 1


def test_the_assignable_roster_stays_workers_only():
    """Agent Builder enumerates this list; the reader is not assignable."""
    tools = ot.build_agent_wrapper_tools(
        session_id=None, proposal_session_id=None, user_id=None,
    )
    assert all(t.name.startswith("call_") for t in tools)


def test_read_worker_output_is_a_registered_read_capability():
    from app.services.langchain.capability_registry import CAPABILITIES, READ
    from app.services.langchain.graphs.capability_dispatcher import (
        MASTER_ALWAYS_BOUND,
    )

    cap = CAPABILITIES["read_worker_output"]
    assert cap.tier == READ
    assert cap.surfaces == frozenset({"master"})
    # Tier 1: it closes over this run's store, so the dispatcher must never
    # try to build it from the registry target alone.
    assert "read_worker_output" in MASTER_ALWAYS_BOUND


@pytest.mark.parametrize(
    "prompt_name",
    [
        "DECISION_MAKER_SYSTEM",
        "DECISION_MAKER_AUTONOMOUS_SYSTEM",
        "DECISION_MAKER_EXECUTION_SYSTEM",
    ],
)
def test_every_master_prompt_carries_the_relay_rule(prompt_name):
    """The budget is only half the fix: a Master told to summarize will."""
    from app.services.langchain.graphs import decision_maker_agent as dm

    prompt = getattr(dm, prompt_name)
    assert "output_complete" in prompt
    assert "read_worker_output" in prompt


def test_handles_stay_unique_after_eviction():
    """`len(self)` stops growing once eviction starts, so a length-based handle
    would be re-issued — overwriting text the Master still holds a handle to."""
    store = ot.WorkerOutputStore()
    issued = [store.put("deep_analyzer", f"output {i}")
              for i in range(ot._MAX_STORED_WORKER_OUTPUTS + 5)]
    assert len(set(issued)) == len(issued)
    # The newest handle still reads back its own text, not another call's.
    assert store[issued[-1]] == f"output {ot._MAX_STORED_WORKER_OUTPUTS + 4}"


def test_a_roster_built_without_a_store_never_advertises_a_reader():
    """The generalist binds the worker roster but not `read_worker_output`.
    Handing it a handle sends it into a tool-not-found error; the honest
    answer is that the remainder is not retrievable from that run."""
    result = {"status": "completed", "output": "A" * (ot._worker_output_budget() * 2),
              "output_type": "document_analysis"}
    payload = ot._worker_result_payload("deep_analyzer", result, None)

    assert payload["output_complete"] is False
    assert "output_handle" not in payload
    assert "not retrievable" in payload["how_to_continue"]
    assert "read_worker_output" not in payload["how_to_continue"]


def test_the_roster_passes_a_missing_store_through():
    """A substitute store nobody can read from is a handle nobody can use."""
    import inspect

    source = inspect.getsource(ot.build_agent_wrapper_tools)
    assert "else WorkerOutputStore()" not in source
