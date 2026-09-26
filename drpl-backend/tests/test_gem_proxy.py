"""GeM Search reaches the portal through a proxy when told to, and says why
a connection failed when it could not.

Pins the fix for the first deploy, where the Railway worker got
``ConnectError: All connection attempts failed`` from GeM's edge while the
same request worked from India: every GeM client is built by
``collector.netconfig.make_client``, the proxy resolves admin setting >
environment, and a connect failure names the per-address OS error and the
setting to change instead of anyio's one-line shrug.
"""
import asyncio

import httpx
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
from collector import netconfig
from collector.config import get_settings
from collector.portals import gem
from collector.portals.base import PortalUnavailable


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(netconfig.ENV_KEY, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    app.dependency_overrides.clear()


def test_the_admin_setting_beats_the_environment_and_empty_means_direct(monkeypatch):
    db = _session()
    assert netconfig.gem_proxy_url(db) is None
    monkeypatch.setenv(netconfig.ENV_KEY, "http://u:p@env-proxy:3128")
    get_settings.cache_clear()
    assert netconfig.gem_proxy_url(db) == "http://u:p@env-proxy:3128"
    db.add(PlatformSetting(key="gem_proxy_url", value="socks5://a:b@in-proxy:1080",
                           value_type="string", category="general", is_secret=True))
    db.commit()
    assert netconfig.gem_proxy_url(db) == "socks5://a:b@in-proxy:1080"


def test_the_setting_is_registered_as_a_secret():
    from app.services.settings_service import DEFAULT_SETTINGS
    row = next(r for r in DEFAULT_SETTINGS if r["key"] == "gem_proxy_url")
    assert row["is_secret"] is True and row["value"] == ""


def test_make_client_hands_the_proxy_to_httpx_and_redacts_it_in_logs(monkeypatch, caplog):
    captured = {}

    class FakeClient:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(netconfig.httpx, "AsyncClient", FakeClient)
    with caplog.at_level("INFO", logger="collector.netconfig"):
        netconfig.make_client(timeout=httpx.Timeout(5), headers={"User-Agent": "x"},
                              proxy="http://user:secret@proxy.example:3128")
    assert captured["proxy"] == "http://user:secret@proxy.example:3128"
    assert captured["follow_redirects"] is True and captured["headers"] == {"User-Agent": "x"}
    assert "secret" not in caplog.text and "proxy.example" in caplog.text

    captured.clear()
    netconfig.make_client(timeout=httpx.Timeout(5), proxy=None)
    assert "proxy" not in captured


def test_every_gem_client_is_built_through_netconfig(monkeypatch):
    from collector import tasks
    built = []
    monkeypatch.setattr(netconfig, "make_client", lambda **kw: built.append(kw) or object())
    tasks._fallback_http()
    assert len(built) == 1 and built[0]["headers"]["User-Agent"]
    src = open(gem.__file__, encoding="utf-8").read()
    assert "httpx.AsyncClient(" not in src.split("async def bootstrap_via_browser")[0]


def test_diagnose_connect_tells_an_open_port_from_a_dead_one_and_a_bad_name():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert netconfig.diagnose_connect("127.0.0.1", port=port, timeout=2.0) == f"127.0.0.1: TCP {port} open"
    finally:
        srv.close()
    # Closed now: refused on Linux, a silent timeout on Windows -- either is
    # named per address, never a bare "all attempts failed".
    dead = netconfig.diagnose_connect("127.0.0.1", port=port, timeout=2.0)
    assert dead.startswith("127.0.0.1:") and "open" not in dead
    assert "DNS for" in netconfig.diagnose_connect("no-such-host.invalid")


class _Refusing:
    """An httpx client whose every GET is a connect failure."""

    def __init__(self):
        self.cookies = httpx.Cookies()
        self.calls = 0

    async def get(self, url):
        self.calls += 1
        raise httpx.ConnectError("All connection attempts failed")

    async def aclose(self):
        return None


def _bootstrap(monkeypatch, proxy):
    monkeypatch.setattr(get_settings(), "request_delay_seconds", 0.0)
    monkeypatch.setattr(netconfig, "diagnose_connect",
                        lambda host, port=443, timeout=6.0: f"160.187.232.18: no reply in 6s (SYN dropped)")
    monkeypatch.setattr(netconfig, "gem_proxy_url", lambda db=None: proxy)
    client = _Refusing()
    with pytest.raises(PortalUnavailable) as exc:
        asyncio.run(gem.bootstrap(client))
    return client, str(exc.value)


def test_a_connect_failure_says_what_to_set_when_no_proxy_is_configured(monkeypatch):
    client, msg = _bootstrap(monkeypatch, proxy=None)
    assert client.calls == gem._BOOTSTRAP_ATTEMPTS
    assert "did not answer after 4 attempts" in msg
    assert "GEM_PROXY_URL" in msg and "gem_proxy_url" in msg
    assert "SYN dropped" in msg


def test_a_connect_failure_through_a_proxy_blames_the_proxy_without_leaking_it(monkeypatch):
    _, msg = _bootstrap(monkeypatch, proxy="http://user:hunter2@in-proxy.example:3128")
    assert "configured GeM proxy did not connect" in msg
    assert "in-proxy.example" in msg and "hunter2" not in msg
    assert "GEM_PROXY_URL" not in msg


def test_an_http_refusal_is_not_treated_as_a_connect_failure(monkeypatch):
    monkeypatch.setattr(get_settings(), "request_delay_seconds", 0.0)
    called = []
    monkeypatch.setattr(netconfig, "diagnose_connect", lambda *a, **k: called.append(1) or "")

    class Forbidden(_Refusing):
        async def get(self, url):
            self.calls += 1
            return httpx.Response(403, request=httpx.Request("GET", url))

    with pytest.raises(PortalUnavailable) as exc:
        asyncio.run(gem.bootstrap(Forbidden()))
    assert "HTTP 403" in str(exc.value) and not called and "GEM_PROXY_URL" not in str(exc.value)


def test_coverage_creates_the_ledger_before_reading_it(monkeypatch):
    from app.api.routes import collect  # noqa: F401 -- route registered
    from collector import db as collector_db
    db = _session()
    user = User(email="a@x", name="A", hashed_password="x", role="tender_search")
    db.add(user)
    db.commit()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    ensured = []

    def fake_ensure():
        # The real one targets the backend's engine; the test session has its own.
        ensured.append(1)
        from collector.models import create_ledger_tables
        create_ledger_tables(db.get_bind())

    monkeypatch.setattr(collector_db, "ensure_ledger", fake_ensure)
    body = TestClient(app).get("/api/collect/coverage?portal=gem").json()
    assert ensured == [1]
    assert body.get("available") is True and body.get("tenders_seen") == 0
