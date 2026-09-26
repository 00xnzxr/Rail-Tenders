"""Artifact routes must not serve one user's artifacts to another.

artifact_service.get_artifact() filters on id alone, so every artifact route was
readable by any authenticated user who guessed an id. GET /download-url makes
that worse than a normal IDOR: it returns a presigned R2 URL that carries no
bearer token and stays valid for 300s, so it can leave the application.
"""
import pytest
from fastapi import HTTPException

from app.models.artifact import CommandCenterArtifact
from app.models.proposal import ProposalSession
from app.services.artifact_authz import assert_artifact_access

OWNER_ID = 1
INTRUDER_ID = 2


@pytest.fixture
def owned_artifact(db):
    session = ProposalSession(created_by=OWNER_ID, status="draft", title="s")
    db.add(session)
    db.commit()
    db.refresh(session)
    a = CommandCenterArtifact(
        session_id=session.id,
        artifact_type="cost_breakdown_xlsx",
        title="Cost Breakdown",
        content="{}",
        file_path="generated_docs/book.xlsx",
        file_name="book.xlsx",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    yield a


def test_owner_is_allowed(db, owned_artifact):
    assert_artifact_access(db, owned_artifact, OWNER_ID)  # must not raise


def test_other_user_is_denied(db, owned_artifact):
    with pytest.raises(HTTPException) as exc:
        assert_artifact_access(db, owned_artifact, INTRUDER_ID)
    assert exc.value.status_code == 403


def test_admin_gets_no_bypass(db, owned_artifact):
    """Matches the session-delete precedent (command_center.py:533), which has
    no role exemption. The sessions list is already per-user, so the UI never
    surfaces another user's artifacts."""
    with pytest.raises(HTTPException) as exc:
        assert_artifact_access(db, owned_artifact, INTRUDER_ID)
    assert exc.value.status_code == 403


def test_artifact_with_no_session_is_denied(db):
    """Fail closed: an unattached artifact is not public.

    `command_center_artifacts.session_id` is NOT NULL at the schema level (see
    app/models/artifact.py), so this state can't be persisted -- it can only
    arise from a transient/detached object. We exercise the defense-in-depth
    branch directly without committing, since assert_artifact_access only
    reads the attribute and never requires the artifact to be persisted.
    """
    a = CommandCenterArtifact(
        session_id=None, artifact_type="analysis", title="t", content="{}",
    )
    with pytest.raises(HTTPException) as exc:
        assert_artifact_access(db, a, OWNER_ID)
    assert exc.value.status_code == 403


def test_artifact_whose_session_vanished_is_denied(db):
    """Fail closed on a dangling session_id rather than allowing."""
    a = CommandCenterArtifact(
        session_id=999999, artifact_type="analysis", title="t", content="{}",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    with pytest.raises(HTTPException) as exc:
        assert_artifact_access(db, a, OWNER_ID)
    assert exc.value.status_code == 403


ROUTES = [
    ("get", "/api/command-center/artifacts/{id}"),
    ("get", "/api/command-center/artifacts/{id}/versions"),
    ("get", "/api/command-center/artifacts/{id}/download"),
    ("get", "/api/command-center/artifacts/{id}/download-url"),
]


@pytest.mark.parametrize("method,path", ROUTES)
def test_routes_deny_a_non_owner(method, path, db, owned_artifact, intruder_client):
    r = getattr(intruder_client, method)(path.format(id=owned_artifact.id))
    assert r.status_code == 403, f"{method.upper()} {path} leaked another user's artifact"


def test_put_denies_a_non_owner(db, owned_artifact, intruder_client):
    r = intruder_client.put(
        f"/api/command-center/artifacts/{owned_artifact.id}",
        json={"content": "hacked"},
    )
    assert r.status_code == 403


def test_export_denies_a_non_owner(db, owned_artifact, intruder_client):
    r = intruder_client.post(
        f"/api/command-center/artifacts/{owned_artifact.id}/export",
        json={"format": "docx"},
    )
    assert r.status_code == 403


def test_missing_artifact_is_404_not_403(intruder_client):
    """Do not collapse 404 into 403 -- they mean different things."""
    r = intruder_client.get("/api/command-center/artifacts/999999")
    assert r.status_code == 404


# ── Session-scoped routes ────────────────────────────────────────────────────
# Task 2b guarded the by-artifact-id routes. These two return the same artifact
# data keyed by SESSION id, so without a guard they walk around that fix.

from app.services.artifact_authz import assert_session_access


def test_session_helper_allows_the_owner(db, owned_artifact):
    assert_session_access(db, owned_artifact.session_id, OWNER_ID)  # must not raise


def test_session_helper_denies_another_user(db, owned_artifact):
    with pytest.raises(HTTPException) as exc:
        assert_session_access(db, owned_artifact.session_id, INTRUDER_ID)
    assert exc.value.status_code == 403


def test_session_helper_404s_on_missing_session(db):
    with pytest.raises(HTTPException) as exc:
        assert_session_access(db, 999999, OWNER_ID)
    assert exc.value.status_code == 404


def test_session_artifacts_route_denies_a_non_owner(db, owned_artifact, intruder_client):
    """The route that leaks full artifact content by session id."""
    r = intruder_client.get(
        f"/api/command-center/sessions/{owned_artifact.session_id}/artifacts"
    )
    assert r.status_code == 403, "session artifact list leaked another user's artifacts"


def test_session_detail_route_denies_a_non_owner(db, owned_artifact, intruder_client):
    """Its payload embeds the session's artifacts."""
    r = intruder_client.get(
        f"/api/command-center/sessions/{owned_artifact.session_id}"
    )
    assert r.status_code == 403


def test_session_owner_still_gets_their_artifacts(db, owned_artifact, auth_client):
    """The guard must not lock the legitimate owner out."""
    r = auth_client.get(
        f"/api/command-center/sessions/{owned_artifact.session_id}/artifacts"
    )
    assert r.status_code == 200
    assert any(a["id"] == owned_artifact.id for a in r.json())
