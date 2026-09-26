"""What one run cost, from the ledger rather than a separate counter.

`_log_usage` already writes an `APIUsageLog` row per LLM call, attributed to
the acting user. It records no run, so the platform could say what a user spent
this month but not what any single run spent — and a run is the unit a user
actually recognises ("that costing cost me forty cents").

`run_id_scope` is already open at every agent entry point for logging, so the
run id needs no new plumbing: it is stamped the same way `actor_context`
stamps the user.
"""

import pytest

from app.core.actor_context import actor_scope
from app.core.run_context import run_id_scope
from app.models.api_usage import APIUsageLog  # noqa: F401 — registers the table
from app.models.user import User  # noqa: F401 — same
from app.services.ai_service import _log_usage
from app.services.run_cost_service import run_cost, run_costs_for


USAGE = {"input_tokens": 1000, "output_tokens": 500}


@pytest.fixture
def ledger(db):
    """Clears the rows this module writes, before and after."""
    def clear():
        db.query(APIUsageLog).filter(
            APIUsageLog.agent_name.like("run_cost_test%")
        ).delete(synchronize_session=False)
        db.commit()

    clear()
    yield
    clear()


def _rows(db, agent_name):
    db.expire_all()
    return db.query(APIUsageLog).filter(APIUsageLog.agent_name == agent_name).all()


def test_a_call_inside_a_run_scope_records_the_run(db, ledger):
    with run_id_scope("run-alpha"):
        _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_a", USAGE, 120, True)

    rows = _rows(db, "run_cost_test_a")
    assert len(rows) == 1
    assert rows[0].run_id == "run-alpha"


def test_a_call_outside_any_run_scope_is_unattributed(db, ledger):
    """Seeders, the archive sweep and scheduled jobs spend without a run. They
    record a NULL rather than being forced into someone else's total."""
    _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_b", USAGE, 120, True)

    assert _rows(db, "run_cost_test_b")[0].run_id is None


def test_run_cost_sums_every_call_the_run_made(db, ledger):
    """A run is many calls — the Master, each worker it delegates to, each
    retry. The number a user sees has to be the whole run, not one call."""
    with run_id_scope("run-gamma"):
        for _ in range(3):
            _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_c", USAGE, 120, True)

    cost = run_cost(db, "run-gamma")

    assert cost.calls == 3
    assert cost.tokens_input == 3000
    assert cost.tokens_output == 1500
    assert cost.cost_usd == pytest.approx(3 * (1000 / 1e6 * 1.0 + 500 / 1e6 * 5.0))


def test_run_cost_counts_only_its_own_run(db, ledger):
    with run_id_scope("run-delta"):
        _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_d", USAGE, 120, True)
    with run_id_scope("run-epsilon"):
        _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_d", USAGE, 120, True)

    assert run_cost(db, "run-delta").calls == 1


def test_an_unknown_run_costs_nothing_rather_than_raising(db, ledger):
    """The Command Center asks for a cost on every finished run. A run whose
    calls never reached the ledger must render as zero, not as an error page."""
    cost = run_cost(db, "no-such-run")

    assert cost.calls == 0
    assert cost.cost_usd == 0.0


def test_failed_calls_are_counted_too(db, ledger):
    """A call that errored after the model produced tokens still costs money.
    Excluding it would under-report exactly the runs a user complains about."""
    with run_id_scope("run-zeta"):
        _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_f", USAGE, 120, False, "boom")

    assert run_cost(db, "run-zeta").calls == 1


def test_recent_runs_are_listed_newest_first(db, ledger):
    """What the admin screen renders: one row per run, not per call."""
    for name in ("run-eta", "run-theta"):
        with run_id_scope(name):
            with actor_scope(user_id=1, role="master_admin"):
                _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_g", USAGE, 120, True)

    runs = run_costs_for(db, agent_name="run_cost_test_g", limit=10)

    assert [r.run_id for r in runs] == ["run-theta", "run-eta"]
    assert runs[0].calls == 1
    assert runs[0].user_id == 1


# --- the column has to reach existing deployments, not just new ones --------


def test_run_id_is_in_both_self_heal_lists():
    """`create_all` never alters an existing table.

    CLAUDE.md's rule for a new column is an Alembic revision *and* an entry in
    both startup self-heal paths — `_apply_schema_drift_fixes` for Postgres and
    `_add_missing_columns` for SQLite — so a deployment that predates the
    column repairs itself instead of erroring on every write.
    """
    import inspect

    from app import main

    postgres = inspect.getsource(main._apply_schema_drift_fixes)
    sqlite = inspect.getsource(main._add_missing_columns)

    assert "api_usage_logs" in postgres and "run_id" in postgres
    assert '("api_usage_logs", "run_id"' in sqlite


def test_an_alembic_revision_adds_the_column():
    """The Postgres path of record. The self-heal lists are a safety net for
    deployments that miss a migration, not a replacement for one."""
    from pathlib import Path

    versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    sources = [p.read_text(encoding="utf-8") for p in versions.glob("*.py")]

    assert any(
        "api_usage_logs" in src and "run_id" in src for src in sources
    ), "no Alembic revision adds api_usage_logs.run_id"


# --- what the user sees: the cost rides on the saved message ----------------


def test_attach_run_cost_stamps_the_metadata(db, ledger):
    """Stored on the message, not streamed.

    The Command Center concatenates SSE events into what it saves; a cost that
    only ever existed as a live event would vanish on reload. Putting it in
    `metadata_json` means the number survives a refresh and needs no endpoint.
    """
    from app.services.run_cost_service import attach_run_cost

    with run_id_scope("run-iota"):
        _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_h", USAGE, 120, True)

    meta = attach_run_cost(db, {"agents_used": ["decision_maker"]}, "run-iota")

    assert meta["agents_used"] == ["decision_maker"]
    assert meta["run_cost"]["calls"] == 1
    assert meta["run_cost"]["tokens_total"] == 1500
    assert meta["run_cost"]["cost_usd"] > 0


def test_attach_run_cost_defaults_to_the_ambient_run(db, ledger):
    """Two of the five save sites have no run-id local, only the scope."""
    from app.services.run_cost_service import attach_run_cost

    with run_id_scope("run-kappa"):
        _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_i", USAGE, 120, True)
        meta = attach_run_cost(db, {})

    assert meta["run_cost"]["run_id"] == "run-kappa"


def test_attach_run_cost_never_breaks_the_save(db):
    """A message that failed to save is a message the user loses. Costing it is
    decoration; it must not be able to take the message down with it."""
    from app.services.run_cost_service import attach_run_cost

    class _Broken:
        def query(self, *a, **k):
            raise RuntimeError("connection gone")

    meta = attach_run_cost(_Broken(), {"agents_used": ["x"]}, "run-lambda")

    assert meta == {"agents_used": ["x"]}


def test_every_assistant_message_carries_its_run_cost():
    """A drift test over the five save sites in `streaming_handler`.

    Each branch of the router builds its own `metadata_json`; a new branch that
    forgets the cost is silent — the message saves fine and simply has no
    number, on exactly the runs someone is trying to account for.
    """
    import re
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "app" / "services" / "langchain" / "streaming_handler.py"
    ).read_text(encoding="utf-8")

    saves = len(re.findall(r'role="assistant"', src))
    stamped = len(re.findall(r"attach_run_cost\(", src))

    assert stamped >= saves, (
        f"{saves} assistant messages saved, {stamped} carry a run cost"
    )


# --- the admin surface ------------------------------------------------------


def test_admin_runs_endpoint_lists_recent_runs(auth_client, db, ledger):
    """Per-run rows on Admin -> Usage & Budgets. Master admin only: the rows
    name other users' runs, and `all_usage` beside it is already gated."""
    with run_id_scope("run-mu"):
        with actor_scope(user_id=1, role="master_admin"):
            _log_usage(db, "anthropic", "claude-haiku-4-5", "run_cost_test_j", USAGE, 120, True)

    body = auth_client.get("/api/admin/usage/runs").json()

    run = next(r for r in body["runs"] if r["run_id"] == "run-mu")
    assert run["calls"] == 1
    assert run["tokens_total"] == 1500
    assert run["cost_usd"] > 0
    assert run["agent_name"] == "run_cost_test_j"


def test_admin_runs_endpoint_is_master_admin_only():
    """A costing researcher must not be able to enumerate other people's runs."""
    import inspect

    from app.api.routes import usage as usage_routes

    source = inspect.getsource(usage_routes.recent_runs)
    assert "require_master_admin" in source
