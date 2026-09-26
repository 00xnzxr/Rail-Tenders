"""What `ai_service` actually puts on the wire.

Four defects that each spent money or quality without raising anything:

- ``get("temperature") or 0.7`` sent every deliberate 0.0 (BOQ number
  transcription, verbatim annexure transcription, per-doc analysis) as 0.7.
- ``call_ai_with_documents`` always posts to api.anthropic.com but took the
  model from the agent's roster row, so an OpenAI-pinned agent sent
  ``gpt-5.6-luna`` there -- a guaranteed failure before a text-only fallback.
- A ``cache_control`` on every PDF block, plus the system block and the
  top-level marker, passed Anthropic's cap of four breakpoints on any
  three-PDF call.
- On a 429 a document request failed over to a text-only provider and was
  answered without the document.
"""

import httpx
import pytest

from app.services import ai_service


def _ok_body(text="ok"):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


class _Recorder:
    """An httpx.AsyncClient stand-in that answers from a script of statuses."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.bodies: list[dict] = []
        self.urls: list[str] = []

    def factory(self):
        rec = self

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                rec.urls.append(url)
                rec.bodies.append(json)
                status = rec.statuses.pop(0) if rec.statuses else 200
                request = httpx.Request("POST", url)
                if status == 200:
                    return httpx.Response(200, json=_ok_body(), request=request)
                return httpx.Response(
                    status, json={"error": {"message": f"HTTP {status}"}},
                    headers={"retry-after": "1"}, request=request,
                )

        return FakeClient


@pytest.fixture
def anthropic_key(monkeypatch):
    monkeypatch.setattr(ai_service.settings, "anthropic_api_key", "test-key", raising=False)
    monkeypatch.setattr(ai_service.settings, "ai_model", "claude-haiku-4-5", raising=False)


@pytest.fixture
def no_sleep(monkeypatch):
    async def _instant(_s):
        return None
    monkeypatch.setattr(ai_service.asyncio, "sleep", _instant)


# ── temperature ──────────────────────────────────────────────────────────────


def test_configured_temperature_keeps_a_real_zero():
    assert ai_service._configured_temperature({"temperature": 0.0}) == 0.0
    assert ai_service._configured_temperature({"temperature": 0}) == 0.0
    assert ai_service._configured_temperature({"temperature": 0.3}) == 0.3
    assert ai_service._configured_temperature({}) == 0.7
    assert ai_service._configured_temperature(None) == 0.7
    assert ai_service._configured_temperature({"temperature": None}) == 0.7


@pytest.mark.asyncio
async def test_temperature_zero_reaches_the_wire(monkeypatch, anthropic_key):
    rec = _Recorder([200])
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())
    await ai_service.call_ai(
        "sys", "user", db=None, agent_name=None,
        model_override="claude-haiku-4-5", temperature_override=0.0,
    )
    assert rec.bodies[0]["temperature"] == 0.0


def test_no_request_builder_uses_the_falsy_temperature_fallback():
    import inspect
    src = inspect.getsource(ai_service)
    assert '.get("temperature") or 0.7' not in src


# ── documents go to a model that can read them ───────────────────────────────


def test_an_openai_pinned_agent_reads_documents_on_anthropic():
    cfg = {"model": "gpt-5.6-luna", "provider": "openai"}
    model = ai_service._document_capable_model(None, "checklist_generator", cfg, "gpt-5.6-luna")
    assert model.startswith("claude-")
    assert cfg["provider"] == "anthropic" and cfg["model"] == model


def test_an_anthropic_agent_keeps_its_own_model():
    cfg = {"model": "claude-sonnet-5"}
    assert ai_service._document_capable_model(None, "x", cfg, "claude-sonnet-5") == "claude-sonnet-5"
    assert cfg == {"model": "claude-sonnet-5"}


@pytest.mark.asyncio
async def test_documents_never_post_a_non_claude_model(monkeypatch, anthropic_key, tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setattr(ai_service, "_get_pdf_page_count", lambda _p: 1)
    rec = _Recorder([200])
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())
    await ai_service.call_ai_with_documents(
        "sys", "user", [str(pdf)], db=None, agent_name="checklist_generator",
        model_override="gpt-5.6-luna",
    )
    assert rec.urls == ["https://api.anthropic.com/v1/messages"]
    assert rec.bodies[0]["model"].startswith("claude-")


# ── one cache breakpoint for the document prefix ─────────────────────────────


def test_only_the_last_document_keeps_its_cache_breakpoint():
    blocks = [
        {"type": "document", "source": {}, "cache_control": {"type": "ephemeral"}},
        {"type": "document", "source": {}, "cache_control": {"type": "ephemeral"}},
        {"type": "document", "source": {}, "cache_control": {"type": "ephemeral"}},
    ]
    ai_service._limit_document_cache_breakpoints(blocks)
    assert ["cache_control" in b for b in blocks] == [False, False, True]


@pytest.mark.asyncio
async def test_a_three_pdf_request_stays_within_four_breakpoints(monkeypatch, anthropic_key, tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"{i}.pdf"
        p.write_bytes(b"%PDF-1.4\n%%EOF\n")
        paths.append(str(p))
    monkeypatch.setattr(ai_service, "_get_pdf_page_count", lambda _p: 1)
    rec = _Recorder([200])
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())
    await ai_service.call_ai_with_documents(
        "S" * 5000, "user", paths, db=None, agent_name=None,
    )
    body = rec.bodies[0]
    marks = sum(1 for b in body["messages"][0]["content"] if "cache_control" in b)
    if isinstance(body["system"], list):
        marks += sum(1 for b in body["system"] if "cache_control" in b)
    marks += 1 if "cache_control" in body else 0
    assert marks <= 4, body


# ── rate limits ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_429_is_retried_on_the_same_model(monkeypatch, anthropic_key, no_sleep):
    rec = _Recorder([429, 200])
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())

    async def _no_fallback(*a, **k):
        raise AssertionError("failed over before retrying the same model")

    monkeypatch.setattr(ai_service, "_call_with_fallback", _no_fallback)
    out = await ai_service.call_ai("sys", "user", model_override="claude-haiku-4-5")
    assert out == "ok"
    assert len(rec.bodies) == 2


@pytest.mark.asyncio
async def test_a_rate_limited_document_request_is_not_answered_without_the_document(
    monkeypatch, anthropic_key, no_sleep,
):
    rec = _Recorder([429, 429, 429])
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())
    called = []

    async def _fallback(*a, **k):
        called.append(True)
        return "an answer that never saw the PDF"

    monkeypatch.setattr(ai_service, "_call_with_fallback", _fallback)
    blocks = [{"type": "document", "source": {"type": "base64", "data": "x",
                                              "media_type": "application/pdf"}},
              {"type": "text", "text": "extract"}]
    with pytest.raises(Exception, match="reading the documents"):
        await ai_service._call_anthropic(
            "sys", "extract", None, None, {"model": "claude-haiku-4-5"},
            content_blocks=blocks,
        )
    assert called == []


@pytest.mark.asyncio
async def test_a_rate_limited_text_request_still_fails_over(monkeypatch, anthropic_key, no_sleep):
    rec = _Recorder([429, 429, 429])
    monkeypatch.setattr(httpx, "AsyncClient", rec.factory())

    async def _fallback(*a, **k):
        return "fallback answer"

    monkeypatch.setattr(ai_service, "_call_with_fallback", _fallback)
    out = await ai_service.call_ai("sys", "user", model_override="claude-haiku-4-5")
    assert out == "fallback answer"


# ── nothing is kept at OpenAI ────────────────────────────────────────────────


def test_responses_api_does_not_store_by_default():
    import inspect
    from app.services.openai_agents.responses_client import call_openai_responses

    assert inspect.signature(call_openai_responses).parameters["store"].default is False


def test_langchain_openai_client_does_not_store():
    from app.services.langchain.model_factory import _create_openai

    llm = _create_openai("gpt-5.6-luna", "sk-test", None, 256)
    assert getattr(llm, "store", None) is False


def test_agents_sdk_runs_are_not_traced_or_stored():
    pytest.importorskip("agents")
    from agents import RunConfig
    from app.services.openai_agents.execution_service import (
        _build_model_settings,
        _private_run_config,
    )

    rc = _private_run_config(RunConfig, 5)
    assert rc.tracing_disabled is True

    class _A:
        temperature = 0.2
        max_tokens = 100

    assert _build_model_settings(_A()).store is False


def test_sql_echo_never_prints_bound_parameters():
    from app.core.database import engine

    assert engine.hide_parameters is True
