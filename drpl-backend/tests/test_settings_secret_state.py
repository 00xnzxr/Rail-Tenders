"""A secret setting has to say whether it is set.

The stored value never reaches the browser -- every secret is served as the
same `••••••••` mask and the page renders an empty password box. So a key that
had never been configured looked exactly like one that had. On a tab carrying
55 settings that is not cosmetic: it is how a master admin concludes a key is
in place, runs the thing it powers, gets an authorisation error, and goes
looking for the cause somewhere other than the empty field.

`is_set` is computed from the stored value before masking, so the page can
distinguish the two without the secret ever leaving the server.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_user
from app.core.database import Base, get_db
from app.main import app
from app.models.platform_setting import PlatformSetting
from app.models.user import User

MASK = "•" * 8


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


@pytest.fixture
def client(db):
    user = User(email="admin@x", name="A", hashed_password="x", role="master_admin")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    yield TestClient(app)
    app.dependency_overrides.clear()


def _by_key(rows):
    return {r["key"]: r for r in rows}


class TestSecretState:
    def test_an_unset_secret_is_reported_as_unset(self, client):
        rows = _by_key(client.get("/api/admin/settings/ai").json())
        row = rows["runpod_chat_api_key"]
        assert row["is_secret"] is True
        assert row["value"] == MASK, "the value must still never leave the server"
        assert row["is_set"] is False

    def test_a_set_secret_is_reported_as_set_and_still_masked(self, client):
        assert client.put(
            "/api/admin/settings/runpod_chat_api_key", json={"value": "rpa_example"}
        ).status_code == 200
        row = _by_key(client.get("/api/admin/settings/ai").json())["runpod_chat_api_key"]
        assert row["is_set"] is True
        assert row["value"] == MASK
        assert "rpa_example" not in str(row)

    def test_whitespace_is_not_a_value(self, client, db):
        db.add(PlatformSetting(
            key="whitespace_key", value="   ", value_type="string",
            category="ai", description="d", is_secret=True,
        ))
        db.commit()
        row = _by_key(client.get("/api/admin/settings/ai").json())["whitespace_key"]
        assert row["is_set"] is False

    def test_the_two_states_are_actually_distinguishable(self, client):
        """The whole point: before `is_set`, these two rows were identical."""
        client.put("/api/admin/settings/runpod_chat_api_key", json={"value": "k"})
        rows = _by_key(client.get("/api/admin/settings/ai").json())
        configured = rows["runpod_chat_api_key"]
        blank = rows["openai_api_key"]
        assert configured["value"] == blank["value"] == MASK
        assert configured["is_set"] != blank["is_set"]

    def test_the_unfiltered_list_reports_it_too(self, client):
        """Both list routes build the response, and only one was ever tested."""
        rows = _by_key(client.get("/api/admin/settings/").json())
        assert rows["runpod_chat_api_key"]["is_set"] is False
        assert "is_set" in rows["ai_model"]

    def test_a_non_secret_still_shows_its_value(self, client):
        rows = _by_key(client.get("/api/admin/settings/ai").json())
        assert rows["runpod_chat_endpoint_id"]["value"] == "9d05slartfxnql"
        assert rows["runpod_chat_endpoint_id"]["is_set"] is True
        assert rows["runpod_chat_model"]["value"] == "qwen2.5-7b"


class TestTheSettingsAMasterAdminNeedsForTheLocalModel:
    """These three are what the Local Model Probe reads. A missing row means an
    admin cannot configure it at all, whatever the code supports."""

    def test_all_three_are_offered_in_the_ai_category(self, client):
        rows = _by_key(client.get("/api/admin/settings/ai").json())
        for key in ("runpod_chat_api_key", "runpod_chat_endpoint_id", "runpod_chat_model"):
            assert key in rows, key
            assert rows[key]["category"] == "ai"

    def test_only_the_key_is_left_to_fill_in(self, client):
        """The endpoint id and model ship with values, so a fresh platform has
        exactly one blank to fill."""
        rows = _by_key(client.get("/api/admin/settings/ai").json())
        assert rows["runpod_chat_endpoint_id"]["is_set"] is True
        assert rows["runpod_chat_model"]["is_set"] is True
        assert rows["runpod_chat_api_key"]["is_set"] is False

    def test_the_ocr_key_is_a_separate_row(self, client):
        """RunPod scopes keys per endpoint: setting the chat key must not touch
        the OCR credential, which the costing path depends on."""
        rows = _by_key(client.get("/api/admin/settings/ai").json())
        assert rows["runpod_api_key"]["key"] != rows["runpod_chat_api_key"]["key"]
        client.put("/api/admin/settings/runpod_chat_api_key", json={"value": "chat"})
        rows = _by_key(client.get("/api/admin/settings/ai").json())
        assert rows["runpod_chat_api_key"]["is_set"] is True
        assert rows["runpod_api_key"]["is_set"] is False, "OCR key untouched"

    def test_a_saved_key_is_what_the_provider_reads(self, client, db):
        from app.services.langchain.provider_config import get_api_key

        client.put("/api/admin/settings/runpod_chat_api_key", json={"value": "rpa_live"})
        assert get_api_key(db, "runpod") == "rpa_live"

    def test_the_mask_is_never_mistaken_for_a_key(self, client, db):
        """If the masked value were ever written back -- a UI that round-trips
        what it was served -- it must not be sent to the provider as a key."""
        from app.services.langchain.provider_config import get_api_key

        client.put("/api/admin/settings/runpod_chat_api_key", json={"value": "real"})
        row = db.query(PlatformSetting).filter_by(key="runpod_chat_api_key").first()
        row.value = MASK
        db.commit()
        assert get_api_key(db, "runpod") != MASK
