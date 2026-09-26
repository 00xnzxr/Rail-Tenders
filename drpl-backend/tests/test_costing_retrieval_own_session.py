"""The costing rate-card retrieval reads on a session of its own.

The ReAct costing agent issues its tool calls in parallel (the log shows
`costing_training_retrieval` and `anonymizing_web_search` starting in the same
millisecond), and every tool of a batch shares one injected session. The
retrieval therefore failed under real load -- "This session is provisioning a
new connection; concurrent operations are not permitted" and, once the
recycling wrapper ended the shared transaction under it, "cursor already
closed" -- and the batch priced without its rate-card evidence. Nothing raised;
the tool returned an error string and the model moved on.

The tool is read-only, so it now opens its own short session per call and
leaves the injected one untouched.
"""

import json
import threading

from app.services.langchain.tools.costing_training_retrieval_tool import (
    CostingTrainingRetrievalTool,
)


def _poison(session):
    """Any query on the injected session is the old bug. The tool's `db`
    field is typed Session, so the real fixture session is poisoned rather
    than replaced by a stand-in."""
    def _query(*a, **k):
        raise AssertionError("retrieval queried the shared session")
    session.query = _query
    return session


def test_retrieval_does_not_touch_the_injected_session(db):
    tool = CostingTrainingRetrievalTool(db=_poison(db))
    out = json.loads(tool._run("chequered plate"))
    assert "error" not in out
    assert out["status"] in ("no_agent", "no_datasets", "not_found", "found")


def test_retrieval_still_requires_a_database():
    tool = CostingTrainingRetrievalTool(db=None)
    assert "error" in json.loads(tool._run("x"))


def test_concurrent_retrievals_do_not_collide(db):
    """Four batches x parallel tool calls: no session-concurrency error."""
    tool = CostingTrainingRetrievalTool(db=_poison(db))
    results: list[dict] = []
    lock = threading.Lock()

    def call():
        out = json.loads(tool._run("primer paint"))
        with lock:
            results.append(out)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert all("error" not in r for r in results), results
