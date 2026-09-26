"""OTP webhook verification, the scope profile, and the sink's retry rules."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from collector.config import get_settings
from collector.scope import ScopeProfile, resolve_terms
from collector.session import otp
from collector.sink import Sink, SinkError

SECRET = "s3cret"


@pytest.fixture(autouse=True)
def _clean():
    otp.reset_for_tests()
    yield
    otp.reset_for_tests()


# -- OTP webhook ---------------------------------------------------------


def test_a_correctly_signed_request_verifies():
    body = json.dumps({"portal": "ireps", "verification_code": "123456"}).encode()
    ts = str(int(time.time()))
    sig = otp.sign(SECRET, ts, body)
    assert otp.verify_signature(SECRET, ts, body, sig)


def test_a_tampered_body_fails():
    ts = str(int(time.time()))
    sig = otp.sign(SECRET, ts, b'{"portal":"ireps","verification_code":"111111"}')
    assert not otp.verify_signature(
        SECRET, ts, b'{"portal":"ireps","verification_code":"999999"}', sig
    )


def test_the_timestamp_is_inside_the_signature():
    """Otherwise a captured signature could be replayed with a fresh header."""
    body = b'{"portal":"ireps"}'
    sig = otp.sign(SECRET, "1000", body)
    assert not otp.verify_signature(SECRET, "2000", body, sig)


def test_a_stale_timestamp_is_rejected():
    old = str(int(time.time()) - 600)
    assert not otp.timestamp_fresh(old, 120)
    assert otp.timestamp_fresh(str(int(time.time())), 120)


def test_a_future_timestamp_is_rejected_too():
    assert not otp.timestamp_fresh(str(int(time.time()) + 600), 120)


def test_a_missing_signature_never_verifies():
    assert not otp.verify_signature(SECRET, "1", b"{}", "")
    assert not otp.verify_signature("", "1", b"{}", "abc")


def test_a_wrong_secret_never_verifies():
    body, ts = b"{}", str(int(time.time()))
    assert not otp.verify_signature(SECRET, ts, body, otp.sign("other", ts, body))


# -- OTP delivery --------------------------------------------------------


def test_a_code_reaches_the_waiting_login():
    async def scenario():
        waiter = asyncio.create_task(otp.await_code("ireps", timeout=5))
        await asyncio.sleep(0.05)
        assert otp.waiting_for() == ["ireps"]
        assert otp.deliver("ireps", "424242") is True
        return await waiter

    assert asyncio.run(scenario()) == "424242"


def test_a_code_with_nobody_waiting_is_dropped_not_an_error():
    """An SMS the portal sent for another reason, or a retry."""
    assert otp.deliver("ireps", "111111") is False


def test_a_timeout_raises_loudly():
    """A silent OTP timeout means IREPS quietly stopped being collected."""
    with pytest.raises(otp.OtpTimeout):
        asyncio.run(otp.await_code("ireps", timeout=1))


def test_nothing_is_left_waiting_after_a_timeout():
    with pytest.raises(otp.OtpTimeout):
        asyncio.run(otp.await_code("ireps", timeout=1))
    assert otp.waiting_for() == []


# -- Scope profile -------------------------------------------------------


def test_search_terms_come_from_keywords_not_group_labels():
    """A label like "AMC / Maintenance Services" is a heading for humans;
    full-text searching it finds nothing."""
    p = ScopeProfile(keyword_groups=[
        {"label": "AMC / Maintenance Services", "keywords": ["AMC", "Annual Maintenance"]},
        {"label": "Mechanical Spares", "keywords": ["bogie", "coupler"]},
    ])
    terms = p.search_terms()
    assert "AMC / Maintenance Services" not in terms
    assert "Annual Maintenance" in terms and "bogie" in terms


def test_terms_are_deduplicated_case_insensitively():
    p = ScopeProfile(
        keyword_groups=[{"label": "g", "keywords": ["Bogie", "bogie", "BOGIE"]}],
        historical_fallback={"keywords": ["bogie"]},
    )
    assert p.search_terms().count("Bogie") == 1
    assert len(p.search_terms()) == 1


def test_very_short_terms_are_dropped():
    """A two-character full-text search matches most of the portal."""
    p = ScopeProfile(keyword_groups=[{"label": "g", "keywords": ["AC", "AMC"]}])
    assert p.search_terms() == ["AMC"]


def test_historical_fallback_comes_after_curated_keywords():
    p = ScopeProfile(
        keyword_groups=[{"label": "g", "keywords": ["curated"]}],
        historical_fallback={"keywords": ["mined"]},
    )
    assert p.search_terms() == ["curated", "mined"]


def test_explicit_run_terms_beat_the_profile():
    """A person asking for something specific should get exactly it."""
    p = ScopeProfile(keyword_groups=[{"label": "g", "keywords": ["bogie"]}])
    assert resolve_terms(p, {"terms": ["axle box"]}) == ["axle box"]


def test_an_empty_profile_still_sweeps_the_always_terms():
    """"railway" is the net that actually catches railway tenders.

    Measured on the first live run: the scope keywords alone filtered out 273
    of 281 rows, because a full-text search for a spare-part name mostly finds
    other ministries buying that part.
    """
    terms = resolve_terms(ScopeProfile(), {})
    assert "railway" in [t.lower() for t in terms]
    assert terms == get_settings().always_terms


def test_always_terms_lead_and_scope_keywords_follow():
    p = ScopeProfile(keyword_groups=[{"label": "g", "keywords": ["bogie", "traction motor"]}])
    terms = resolve_terms(p, {})
    assert terms[0].lower() == "railway"
    assert "bogie" in terms and "traction motor" in terms


def test_always_terms_are_not_duplicated_by_the_profile():
    p = ScopeProfile(keyword_groups=[{"label": "g", "keywords": ["Railway", "bogie"]}])
    terms = resolve_terms(p, {})
    assert [t.lower() for t in terms].count("railway") == 1


def test_explicit_terms_still_beat_everything():
    """A person asking for something specific gets exactly it -- no net added."""
    p = ScopeProfile(keyword_groups=[{"label": "g", "keywords": ["bogie"]}])
    assert resolve_terms(p, {"terms": ["axle box"]}) == ["axle box"]


def test_clearing_always_terms_falls_back_to_the_profile_alone(monkeypatch):
    monkeypatch.setenv("GEM_ALWAYS_TERMS", "")
    get_settings.cache_clear()
    try:
        p = ScopeProfile(keyword_groups=[{"label": "g", "keywords": ["bogie"]}])
        assert resolve_terms(p, {}) == ["bogie"]
    finally:
        get_settings.cache_clear()


def test_exclusion_terms_are_matched_case_insensitively():
    p = ScopeProfile(exclusion_terms=["Stationery"])
    assert p.excluded("Supply of STATIONERY items")
    assert not p.excluded("Supply of traction motors")


def test_no_exclusion_terms_excludes_nothing():
    assert not ScopeProfile().excluded("anything at all")


# -- Sink ----------------------------------------------------------------


def _sink_with(handler) -> Sink:
    return Sink(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_an_empty_batch_is_a_no_op_not_a_request():
    called = {"n": 0}

    def handler(request):
        called["n"] += 1
        return httpx.Response(200, json={})

    sink = _sink_with(handler)
    result = asyncio.run(sink.post([]))
    assert called["n"] == 0
    assert result.new == 0


def test_a_successful_post_returns_the_backends_counts(monkeypatch):
    monkeypatch.setenv("DRPL_SERVICE_TOKEN", "drpl_test")
    get_settings.cache_clear()

    def handler(request):
        assert request.headers["Authorization"] == "Bearer drpl_test"
        body = json.loads(request.content)
        assert list(body) == ["tenders"]
        return httpx.Response(200, json={
            "received": 2, "new": 1, "duplicates": 1, "errors": 0, "new_ids": [7],
        })

    sink = _sink_with(handler)
    result = asyncio.run(sink.post([{"tenderId": "a"}, {"tenderId": "b"}]))
    assert (result.new, result.duplicates, result.new_ids) == (1, 1, [7])
    get_settings.cache_clear()


def test_a_bad_token_fails_immediately_without_retrying(monkeypatch):
    """A 401 will still be a 401 in four seconds; retrying just burns the budget."""
    monkeypatch.setenv("DRPL_SERVICE_TOKEN", "drpl_bad")
    get_settings.cache_clear()
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(401, json={"detail": "Invalid or revoked API token"})

    sink = _sink_with(handler)
    with pytest.raises(SinkError, match="service token"):
        asyncio.run(sink.post([{"tenderId": "a"}]))
    assert attempts["n"] == 1
    get_settings.cache_clear()


def test_a_server_error_is_retried_then_raises(monkeypatch):
    monkeypatch.setenv("DRPL_SERVICE_TOKEN", "drpl_test")
    monkeypatch.setenv("SINK_MAX_RETRIES", "2")
    get_settings.cache_clear()
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(502, text="bad gateway")

    sink = _sink_with(handler)
    with pytest.raises(SinkError):
        asyncio.run(sink.post([{"tenderId": "a"}]))
    assert attempts["n"] == 2, "retried exactly the configured number of times"
    get_settings.cache_clear()


def test_no_service_token_is_a_clear_error_not_a_401(monkeypatch):
    monkeypatch.setenv("DRPL_SERVICE_TOKEN", "")
    get_settings.cache_clear()
    sink = _sink_with(lambda r: httpx.Response(200, json={}))
    with pytest.raises(SinkError, match="DRPL_SERVICE_TOKEN"):
        asyncio.run(sink.post([{"tenderId": "a"}]))
    get_settings.cache_clear()
