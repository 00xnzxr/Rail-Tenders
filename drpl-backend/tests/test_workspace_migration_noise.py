"""The workspace content migration must not re-announce itself on every boot.

`_migrate_workspace_content` converts markdown that was stored in
`draft_content_html` into real HTML. It is genuinely one-time work, but it had
no memory of having run: every startup it loaded all 725 workspace rows (2.7 MB
of HTML) to re-decide they were fine, and printed a line for each one it
*skipped*. `railway.json` starts uvicorn with `--workers 4` and `main.py`
executes this at import, so that was ~2,900 lines per deploy.

Nothing was corrupted — every line was the migration correctly skipping. The
cost was the log: a production log that is 100% skip-notices is a log nobody
can diagnose from, which is exactly what happened when a stale deployment
needed diagnosing.

The manual route (`POST /tenders/{id}/workspace/migrate-markdown`) still calls
`migrate_markdown_content` directly and must still do the work — the marker
gates the *startup* call, not the function.
"""

import pytest

from app.models.platform_setting import PlatformSetting
from app.models.workspace import DocumentWorkspace  # noqa: F401 — registers the table
from app.services.workspace_service import migrate_markdown_content

MARKDOWN = "# Annexure-IX\n\nReference clause 4.2 of the tender."
VALID_HTML = "<h1>Annexure-IX</h1>\n<p>Reference clause 4.2 of the tender.</p>"


@pytest.fixture
def workspaces(db):
    """Two rows: one already converted, one still markdown."""
    made = []

    def make(content):
        row = DocumentWorkspace(
            checklist_item_id=90000 + len(made),
            tender_id=90000,
            draft_content_html=content,
            letterhead_disabled=False,
            page_orientation="portrait",
        )
        db.add(row)
        db.commit()
        made.append(row.id)
        return row

    yield make
    db.query(DocumentWorkspace).filter(DocumentWorkspace.id.in_(made)).delete(
        synchronize_session=False
    )
    db.commit()


# ── the function itself keeps working ───────────────────────────────────────


def test_markdown_is_still_converted(db, workspaces):
    row = workspaces(MARKDOWN)

    migrate_markdown_content(db)

    db.refresh(row)
    assert row.draft_content_html.strip().startswith("<h1>")
    assert row.draft_content_markdown == MARKDOWN


def test_valid_html_is_left_alone(db, workspaces):
    row = workspaces(VALID_HTML)

    migrate_markdown_content(db)

    db.refresh(row)
    assert row.draft_content_html == VALID_HTML


def test_a_skipped_row_does_not_print_a_line_of_its_own(db, workspaces, capsys):
    """The 2,900 lines. One row skipped must produce no per-row output."""
    row = workspaces(VALID_HTML)

    migrate_markdown_content(db)

    out = capsys.readouterr().out
    assert f"id={row.id}" not in out, f"per-row skip line still printed:\n{out}"


def test_the_summary_is_still_reported(db, workspaces):
    """Quieter, not silent — the counts are the point of running it."""
    workspaces(VALID_HTML)

    result = migrate_markdown_content(db)

    assert result["skipped"] >= 1
    assert "converted" in result and "total_checked" in result


# ── the startup call remembers it ran ───────────────────────────────────────


def test_startup_migration_runs_once_then_marks_itself(db, monkeypatch):
    """Second boot must not re-scan the table."""
    from app import main

    db.query(PlatformSetting).filter(
        PlatformSetting.key == main.WORKSPACE_MIGRATION_SETTING_KEY
    ).delete()
    db.commit()

    calls = []
    monkeypatch.setattr(
        "app.services.workspace_service.migrate_markdown_content",
        lambda _db: calls.append(1) or {"converted": 0, "skipped": 0, "total_checked": 0},
    )

    main._migrate_workspace_content()
    main._migrate_workspace_content()

    assert calls == [1], "the migration re-scanned on the second startup"


def test_bumping_the_version_lets_it_run_again(db, monkeypatch):
    """A marker you cannot clear is a migration you can never re-run."""
    from app import main

    row = db.query(PlatformSetting).filter(
        PlatformSetting.key == main.WORKSPACE_MIGRATION_SETTING_KEY
    ).first()
    if row:
        row.value = "some-older-version"
    else:
        db.add(PlatformSetting(
            key=main.WORKSPACE_MIGRATION_SETTING_KEY, value="some-older-version",
            value_type="string", category="system", description="test", is_secret=False,
        ))
    db.commit()

    calls = []
    monkeypatch.setattr(
        "app.services.workspace_service.migrate_markdown_content",
        lambda _db: calls.append(1) or {"converted": 0, "skipped": 0, "total_checked": 0},
    )

    main._migrate_workspace_content()

    assert calls == [1], "a stale marker version should let the migration run again"
