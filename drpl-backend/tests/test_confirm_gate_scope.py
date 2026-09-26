"""confirm_gate_scope: the write gate, loosened deliberately.

The owner's choice: ordinary writes run uninterrupted; destructive actions
still raise the confirmation card. A prompt is being traded for a log, so
every write the scope lets through must land in the audit trail — the log is
only useful if it is complete. `all_writes` restores today's behaviour from
the admin panel, no deploy.
"""

import app.models  # noqa: F401
import app.models.agent_memory  # noqa: F401
import pytest

from app.core.database import Base, engine
from app.services.langchain.tool_policy import should_gate


@pytest.fixture(autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


def test_destructive_only_lets_an_ordinary_write_through():
    assert should_gate("regenerate_checklist", scope="destructive_only") is False
    assert should_gate("xlsx_generator", scope="destructive_only") is False
    assert should_gate("docx_generator", scope="destructive_only") is False


def test_destructive_only_still_stops_the_destructive_ones():
    assert should_gate("finalize_document", scope="destructive_only") is True
    assert should_gate("init_workspace_force", scope="destructive_only") is True


def test_all_writes_restores_todays_behaviour():
    assert should_gate("regenerate_checklist", scope="all_writes") is True
    assert should_gate("finalize_document", scope="all_writes") is True


def test_reads_are_never_gated_under_either_scope():
    for scope in ("destructive_only", "all_writes"):
        assert should_gate("web_search", scope=scope) is False
        assert should_gate("call_costing_researcher", scope=scope) is False
        assert should_gate("use_capability", scope=scope) is False


def test_an_unknown_tool_still_gates_under_either_scope():
    """The fail-safe survives the loosening: an unregistered tool is a write,
    and a write under destructive_only runs ungated — so unknown must be
    treated as gate-worthy regardless, because nobody classified its blast
    radius."""
    assert should_gate("brand_new_unclassified_tool", scope="all_writes") is True
    assert should_gate("brand_new_unclassified_tool", scope="destructive_only") is True


def test_an_ungated_write_is_audited(db, monkeypatch):
    """Every write the scope lets through gets an audit row."""
    from app.models.audit_log import AuditLog
    from app.services.langchain import tool_policy

    monkeypatch.setattr(
        "app.core.database.SessionLocal", lambda: db,
    )
    before = db.query(AuditLog).count()
    tool_policy.audit_ungated_write(1, "regenerate_checklist", {"tender_id": 7})
    db.commit()
    after = db.query(AuditLog).count()
    assert after == before + 1
    row = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    assert "regenerate_checklist" in row.action
    assert row.details["gate"] == "bypassed_by_scope"
