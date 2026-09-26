"""
The contract tests. These are the ones that protect the integration.

The collector shares exactly three things with drpl-backend and imports none of
them, so nothing but a test can tell you when one side has moved:

  1. the ``agent_runs`` table
  2. the Redis key formats and the event field names
  3. the ``TenderInput`` shape that ``POST /api/extension/tenders`` validates

When drpl-backend is on disk (the ordinary case for anyone developing this),
these read its source and compare. When it is not -- CI, a fresh clone -- they
skip rather than fail, because a missing sibling repo is not a broken contract.
Set DRPL_BACKEND_PATH to point at it explicitly.

If one of these fails, do NOT loosen it. It is telling you that deploying the
collector as-is will break something in production.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from collector import runbus
from collector.models import AgentRun


def _find_backend() -> Path | None:
    """Locate drpl-backend, if it is anywhere obvious."""
    explicit = os.getenv("DRPL_BACKEND_PATH")
    if explicit and Path(explicit).is_dir():
        return Path(explicit)
    here = Path(__file__).resolve()
    for parent in here.parents:
        for candidate in (
            parent / "drpl-backend",
            parent / "DRPL-latest101" / "drpl-backend",
            parent / "DRPL-latest101" / "DRPL-latest101" / "drpl-backend",
        ):
            if (candidate / "app" / "models" / "agent_run.py").is_file():
                return candidate
    return None


BACKEND = _find_backend()
needs_backend = pytest.mark.skipif(
    BACKEND is None,
    reason="drpl-backend not on disk; set DRPL_BACKEND_PATH to run the contract tests",
)


def _class_columns(source: str, class_name: str) -> dict[str, str]:
    """{column name: the Column(...) call as source} for one model class."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            out = {}
            for stmt in node.body:
                if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                    continue
                target = stmt.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                if not (isinstance(stmt.value, ast.Call)
                        and getattr(stmt.value.func, "id", "") == "Column"):
                    continue
                out[target.id] = ast.unparse(stmt.value)
            return out
    raise AssertionError(f"class {class_name} not found")


# -- 1. agent_runs ------------------------------------------------------


@needs_backend
def test_agent_run_mirror_has_no_columns_the_backend_lacks():
    """Every column the collector maps must exist on the real table.

    The collector's mirror is deliberately allowed to be a SUBSET -- it maps
    what it touches. What it must never do is invent a column, because the
    first UPDATE would then fail against the real database.
    """
    src = (BACKEND / "app" / "models" / "agent_run.py").read_text(encoding="utf-8")
    backend_cols = set(_class_columns(src, "AgentRun"))
    mirror_cols = {c.name for c in AgentRun.__table__.columns}
    invented = mirror_cols - backend_cols
    assert not invented, (
        f"collector's AgentRun mirror has column(s) drpl-backend does not: {invented}"
    )


@needs_backend
def test_agent_run_mirror_covers_what_the_collector_writes():
    """The columns tasks.py assigns must all be present in the mirror."""
    written = {
        "status", "started_at", "finished_at", "error_message",
        "result_summary", "partial_trace", "meta", "user_id", "id",
    }
    mirror_cols = {c.name for c in AgentRun.__table__.columns}
    assert written <= mirror_cols


@needs_backend
def test_agent_runs_table_name_matches():
    src = (BACKEND / "app" / "models" / "agent_run.py").read_text(encoding="utf-8")
    assert '__tablename__ = "agent_runs"' in src
    assert AgentRun.__tablename__ == "agent_runs"


# -- 2. Redis keys and the event shape ----------------------------------


@needs_backend
def test_redis_key_formats_match():
    """A drifted key means the UI subscribes to a stream nobody writes to."""
    src = (BACKEND / "app" / "core" / "redis_client.py").read_text(encoding="utf-8")
    assert 'f"drpl:run:{run_id}:events"' in src, "backend stream key changed"
    assert 'f"drpl:run:{run_id}:cancel"' in src, "backend cancel key changed"
    assert runbus.stream_key("abc") == "drpl:run:abc:events"
    assert runbus.cancel_key("abc") == "drpl:run:abc:cancel"


@needs_backend
def test_stream_ttl_matches():
    src = (BACKEND / "app" / "core" / "redis_client.py").read_text(encoding="utf-8")
    m = re.search(r"RUN_STREAM_TTL_SECONDS\s*=\s*([0-9*\s]+)$", src, re.M)
    assert m, "RUN_STREAM_TTL_SECONDS not found in the backend"
    assert eval(m.group(1).strip()) == runbus.STREAM_TTL_SECONDS  # noqa: S307


@needs_backend
def test_user_active_key_matches():
    """The concurrency slot the enqueue takes is the one the worker gives back."""
    src = (BACKEND / "app" / "api" / "routes" / "runs.py").read_text(encoding="utf-8")
    assert 'f"drpl:user:{user_id}:active_runs"' in src
    assert runbus.user_active_key(7) == "drpl:user:7:active_runs"


@needs_backend
def test_event_field_names_match_xadd_event():
    """THE silent-failure guard.

    The SSE endpoint plucks 'event', 'data' and 'sse' out of each stream
    entry. If the collector wrote different field names, _field() would return
    None and every event would vanish with no error anywhere.
    """
    src = (BACKEND / "app" / "worker" / "run_tasks.py").read_text(encoding="utf-8")
    assert 'payload: dict[str, Any] = {"event": event}' in src
    assert 'payload["data"] = json.dumps(data, default=str)' in src

    routes = (BACKEND / "app" / "api" / "routes" / "runs.py").read_text(encoding="utf-8")
    assert '_field(fields, "event")' in routes
    assert '_field(fields, "data")' in routes


@needs_backend
def test_run_done_still_terminates_the_stream():
    """finish() emits exactly one run_done because that is what closes the UI."""
    routes = (BACKEND / "app" / "api" / "routes" / "runs.py").read_text(encoding="utf-8")
    assert 'if event_name in ("run_done",):' in routes


def test_emit_writes_the_expected_field_map():
    """Same assertion from the other side, with no backend needed."""
    written = {}

    class FakeRedis:
        def xadd(self, key, payload):
            written["key"] = key
            written["payload"] = payload

    runbus.emit(FakeRedis(), "run-1", "tenders_ingested", {"portal": "gem", "new": 3})
    assert written["key"] == "drpl:run:run-1:events"
    assert set(written["payload"]) == {"event", "data"}
    assert written["payload"]["event"] == "tenders_ingested"
    import json

    assert json.loads(written["payload"]["data"])["new"] == 3


def test_emit_omits_data_when_there_is_none():
    written = {}

    class FakeRedis:
        def xadd(self, key, payload):
            written.update(payload)

    runbus.emit(FakeRedis(), "run-1", "run_started")
    assert written == {"event": "run_started"}


def test_emit_never_raises():
    """Progress reporting must never fail a sweep."""

    class Exploding:
        def xadd(self, *a, **k):
            raise RuntimeError("redis is on fire")

    runbus.emit(Exploding(), "run-1", "x", {"a": 1})  # must not raise
    assert runbus.is_cancelled(Exploding(), "run-1") is False


# -- 3. TenderInput -----------------------------------------------------


@needs_backend
def test_mapped_tender_fields_all_exist_on_tender_input():
    """Every key the GeM mapper emits must be a real TenderInput field.

    Pydantic ignores unknown keys by default, so a typo here would not raise --
    it would silently drop the field and the column would be empty forever.
    """
    from collector.portals.gem import to_tender

    src = (BACKEND / "app" / "schemas" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fields: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TenderInput":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.add(stmt.target.id)
    assert fields, "could not read TenderInput"

    doc = {
        "b_bid_number": ["GEM/2026/B/123"],
        "b_id": [9],
        "bd_category_name": ["Traction motor spares"],
        "ba_official_details_minName": ["Ministry of Railways"],
        "ba_official_details_deptName": ["Indian Railways"],
        "final_start_date_sort": ["2026-09-01T10:00:00Z"],
        "final_end_date_sort": ["2026-09-20T10:00:00Z"],
    }
    mapped = to_tender(doc, term="traction motor")
    unknown = set(mapped) - fields
    assert not unknown, f"to_tender emits key(s) TenderInput does not accept: {unknown}"


@needs_backend
def test_ireps_mapped_fields_all_exist_on_tender_input():
    from collector.portals.ireps import rows_to_tenders

    src = (BACKEND / "app" / "schemas" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fields = {
        stmt.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "TenderInput"
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    html = (Path(__file__).parent / "fixtures" / "ireps_results_page.html").read_text(
        encoding="utf-8"
    )
    rows = rows_to_tenders(html, "https://www.ireps.gov.in")
    assert rows
    unknown = set(rows[0]) - fields
    assert not unknown, f"rows_to_tenders emits key(s) TenderInput rejects: {unknown}"


@needs_backend
def test_dedupe_key_matches_the_backend_unique_constraint():
    """The ledger's primary key IS the backend's uniqueness rule.

    If these drift, "seen here" and "stored there" stop meaning the same thing
    and the coverage numbers become fiction.
    """
    from collector.models import CollectSeen

    src = (BACKEND / "app" / "models" / "tender.py").read_text(encoding="utf-8")
    assert 'Index("ix_tenders_portal_tender_id", "portal", "tender_id", unique=True)' in src
    pk = [c.name for c in CollectSeen.__table__.primary_key.columns]
    assert pk == ["portal", "tender_id"]


@needs_backend
def test_ingest_endpoint_still_exists_and_returns_new_ids():
    src = (BACKEND / "app" / "api" / "routes" / "extension.py").read_text(encoding="utf-8")
    assert '@router.post("/tenders"' in src
    assert "router = APIRouter(prefix=\"/extension\"" in src
    schemas = (BACKEND / "app" / "schemas" / "__init__.py").read_text(encoding="utf-8")
    assert "class BatchUploadResponse" in schemas
    assert "new_ids" in schemas


@needs_backend
def test_collector_does_not_import_backend_code():
    """The boundary, enforced. Break it and you can no longer roll back one side."""
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts or ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in (r"^\s*from\s+app[\.\s]", r"^\s*import\s+app[\.\s]"):
            if re.search(pattern, text, re.M):
                offenders.append(str(path.relative_to(root)))
                break
    assert not offenders, f"these import drpl-backend code: {offenders}"
