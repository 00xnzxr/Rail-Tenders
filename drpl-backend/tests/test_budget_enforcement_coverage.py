"""Every endpoint that starts agent work must check the budget.

An ungated entry point is not a visible bug — it is a hole a user routes all
their spend through while the bar still reads 40%. This is the same shape as
`test_tool_policy.py`'s unclassified-tool test: adding a new way to start a run
fails the suite rather than quietly bypassing the cap.

Add a new run entry point? Add it here and gate it. Do not delete a row to make
this pass.
"""

import ast
import pathlib

import pytest

ROUTES = pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "routes"

#: (module, endpoint function) that spend money on the user's behalf.
RUN_ENTRY_POINTS = [
    ("command_center.py", "chat_stream"),
    ("command_center.py", "respond_to_decision_plan"),
    ("command_center.py", "respond_to_pending_action"),
    ("runs.py", "enqueue_run_endpoint"),
    ("langchain_agents.py", "proposal_chat_stream"),
]


def _function(module: str, name: str) -> ast.AST:
    tree = ast.parse((ROUTES / module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{module}: no function named {name} — did it get renamed?")


@pytest.mark.parametrize("module,func", RUN_ENTRY_POINTS)
def test_run_entry_point_checks_the_budget(module, func):
    node = _function(module, func)
    called = {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "assert_within_budget" in called, (
        f"{module}:{func} starts agent work without checking the user's budget — "
        "spend through it is uncapped."
    )


@pytest.mark.parametrize("module,func", RUN_ENTRY_POINTS)
def test_entry_point_gates_before_it_streams(module, func):
    """The check must precede the StreamingResponse, or the 402 arrives as a
    mid-stream error the frontend renders as a broken run rather than a limit."""
    node = _function(module, func)
    gate_line = min(
        (n.lineno for n in ast.walk(node)
         if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
         and n.func.id == "assert_within_budget"),
        default=None,
    )
    stream_lines = [
        n.lineno for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "StreamingResponse"
    ]
    assert gate_line is not None
    for line in stream_lines:
        assert gate_line < line, f"{module}:{func} streams before checking the budget"
