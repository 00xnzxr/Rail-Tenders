"""A detached run may not borrow anything scoped to its request.

`detached_stream` keeps the run alive after the client leaves, but the run was
handed the request's SQLAlchemy session. `get_db` closes that session in a
`finally` which fires the moment the response ends — which, for a client that
walked away, is while the run is still working. The run then issued queries on
a session whose connection had gone back to the pool, with any transaction it
had open rolled back underneath it.

The existing `test_detached_stream.py` cannot catch this: it drives the module
directly and asserts nothing about databases. So this is a source-level check
that the endpoints which detach open their own session, in the same spirit as
the repo's other drift tests.
"""
import ast
import io
import pathlib

import pytest

ROUTES = pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "routes"
COMMAND_CENTER = ROUTES / "command_center.py"


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found — was it renamed?")


@pytest.fixture(scope="module")
def tree():
    return ast.parse(io.open(COMMAND_CENTER, encoding="utf-8").read())


def _names_used(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _detaching_sources(tree):
    """Every `_source` nested inside a handler that calls `detached_stream`."""
    found = []
    for outer in ast.walk(tree):
        if not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = {
            n.func.id
            for n in ast.walk(outer)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if "detached_stream" not in calls:
            continue
        for inner in ast.walk(outer):
            if (
                isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                and inner.name == "_source"
            ):
                found.append((outer.name, inner))
    return found


def test_the_chat_stream_still_detaches(tree):
    """Guards the fixture below from silently testing nothing."""
    names = [outer for outer, _ in _detaching_sources(tree)]
    assert names, "no handler calls detached_stream any more"


def test_no_detached_source_uses_the_request_session(tree):
    """`db` is the request's session. A detached run must not touch it."""
    offenders = [
        outer for outer, src in _detaching_sources(tree) if "db" in _names_used(src)
    ]
    assert not offenders, (
        "these detached runs still use the request-scoped session `db`, which "
        f"is closed as soon as the client disconnects: {offenders}"
    )


def test_the_chat_run_opens_a_session_of_its_own(tree):
    """Not merely avoiding `db` — the chat run does real database work.

    It resolves attachments and drives the router, both of which query and
    write, so it has to bring its own session rather than have none at all.
    (The approval handlers legitimately open none: everything below them goes
    through `stream_decision_maker_directly`, which manages its own.)
    """
    src = dict(_detaching_sources(tree))["chat_stream"]
    calls = {
        n.func.id
        for n in ast.walk(src)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "SessionLocal" in calls


def test_the_approval_endpoints_are_detached(tree):
    """Executing an approved plan or write is the work the user said yes to.

    Both ran undetached, so a closed tab cancelled them mid-execution — and in
    the action case the single-use grant had already been spent, so the write
    the user approved simply never happened.
    """
    for handler in ("respond_to_decision_plan", "respond_to_pending_action"):
        fn = _function(tree, handler)
        calls = {
            n.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "detached_stream" in calls, f"{handler} is not detached"
