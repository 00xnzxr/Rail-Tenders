"""Pytest session setup: bind the app to a disposable SQLite DB.

CRITICAL: local .env DATABASE_URL is the LIVE Neon prod DB. This file forces
a throwaway SQLite file for the whole test session. It MUST set the env var
before any `app.*` import (pytest imports conftest before test modules, so
this top-level assignment runs first).
"""
import os
import tempfile

# Disposable on-disk SQLite (on-disk, not :memory:, so the app's create_all
# and the request-scoped sessions share one DB across connections/threads).
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "drpl_test.db")
if os.path.exists(_TEST_DB_PATH):
    os.remove(_TEST_DB_PATH)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
# Ignore the developer's local .env entirely. Tests assert the SHIPPED config
# defaults, so a local override (e.g. AUTO_SCORING_ENABLED=false, set to keep a
# dev box from mutating data on a timer) would otherwise fail the suite on that
# machine and pass in CI. See Settings.Config.env_file.
os.environ["DRPL_ENV_FILE"] = ""
# Neutralize any prod-oriented pool env that could leak in.
os.environ.pop("DB_POOL_SIZE", None)
os.environ.pop("DB_MAX_OVERFLOW", None)

import pytest  # noqa: E402
from app.core.database import Base, engine, SessionLocal  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """Create all tables once on the SQLite test DB."""
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    """A session on the isolated test DB. Rolls back at teardown."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def auth_client():
    """TestClient with authentication dependency overridden to a stub user."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.auth import get_current_user
    from app.models.user import User

    def _stub_user():
        return User(id=1, email="test@drpl.local", role="master_admin", is_active=True)

    app.dependency_overrides[get_current_user] = _stub_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def intruder_client():
    """TestClient authenticated as a DIFFERENT user than the artifact owner."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.auth import get_current_user
    from app.models.user import User

    def _stub_user():
        # An ordinary second user, not an admin. The role used to be
        # master_admin, which was incidental — the point of this fixture is
        # "somebody else", and master_admin now legitimately sees every user's
        # work (app/core/roles.py), so leaving it here would have made these
        # tests assert the opposite of the role model.
        return User(id=2, email="intruder@drpl.local", role="costing_research", is_active=True)

    app.dependency_overrides[get_current_user] = _stub_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
