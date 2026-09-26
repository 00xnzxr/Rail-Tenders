"""Claude web search is the tier between Tavily and DuckDuckGo.

A deployment with no Gemini or Tavily key used to fall straight to
DuckDuckGo, whose (renamed, deprecated) library answered "flexible
elastomeric cable 4 sq mm price per metre" with dictionary entries for
"flexible": the costing agent had no market evidence at all. The platform
already holds an Anthropic key, and Claude's server-side web search returns
cited prices from Indian sellers. No network here: the client is faked.
"""
import json
from types import SimpleNamespace as NS

import pytest

from app.services.langchain.tools import web_search_tool as wst


class _FakeMessages:
    def __init__(self, content):
        self._content = content
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return NS(content=self._content, usage=NS(input_tokens=10, output_tokens=5),
                  stop_reason="end_turn")


class _FakeClient:
    last = None

    def __init__(self, content):
        self.messages = _FakeMessages(content)
        _FakeClient.last = self


@pytest.fixture
def no_other_keys(monkeypatch):
    monkeypatch.setattr(wst, "_get_search_config", lambda db=None: {
        "google_api_key": "", "gemini_search_model": "", "tavily_api_key": ""})
    monkeypatch.setattr(wst, "cache_get_json", lambda k: None)
    monkeypatch.setattr(wst, "cache_set_json", lambda *a, **k: None)
    monkeypatch.setattr(wst.WebSearchTool, "_anthropic_key", lambda self: "sk-test")


def _patch_client(monkeypatch, content):
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: _FakeClient(content))


def test_claude_search_answers_before_duckduckgo(monkeypatch, no_other_keys):
    _patch_client(monkeypatch, [
        NS(type="server_tool_use"),
        NS(type="web_search_tool_result", content=[
            NS(url="https://dir.indiamart.com/cable-4sqmm", title="4 sq mm cable", page_age="June 2026"),
            NS(url="https://dir.indiamart.com/cable-4sqmm", title="dup", page_age=None),
        ]),
        NS(type="text", text="Rs 45 per metre (https://dir.indiamart.com/cable-4sqmm)"),
    ])
    monkeypatch.setattr(wst.WebSearchTool, "_search_duckduckgo",
                        lambda *a, **k: pytest.fail("DuckDuckGo must not be reached"))
    out = json.loads(wst.WebSearchTool(db=None)._run("4 sq mm cable price per metre"))
    assert out["provider"] == "anthropic_web_search"
    assert [r["url"] for r in out["results"]] == ["https://dir.indiamart.com/cable-4sqmm"]
    assert "Rs 45 per metre" in out["grounded_summary"]
    call = _FakeClient.last.messages.calls[0]
    tool = call["tools"][0]
    assert tool["type"] == "web_search_20250305" and tool["user_location"]["country"] == "IN"


def test_a_search_error_block_is_not_a_crash(monkeypatch, no_other_keys):
    _patch_client(monkeypatch, [
        NS(type="web_search_tool_result", content=NS(type="web_search_tool_result_error",
                                                     error_code="max_uses_exceeded")),
        NS(type="text", text="No page gives a price for this exact item."),
    ])
    out = json.loads(wst.WebSearchTool(db=None)._run("RDSO LED fitting price"))
    assert out["results"] == [] and "No page gives a price" in out["grounded_summary"]


def test_domains_are_passed_through(monkeypatch, no_other_keys):
    _patch_client(monkeypatch, [NS(type="text", text="nothing")])
    wst.WebSearchTool(db=None)._run("tender", allowed_domains=["ireps.gov.in"])
    assert _FakeClient.last.messages.calls[0]["tools"][0]["allowed_domains"] == ["ireps.gov.in"]


def test_a_failed_claude_search_still_falls_back(monkeypatch, no_other_keys):
    import anthropic

    def boom(api_key=None):
        raise RuntimeError("network down")
    monkeypatch.setattr(anthropic, "Anthropic", boom)
    monkeypatch.setattr(wst.WebSearchTool, "_search_duckduckgo", lambda self, q, n, d: "ddg")
    assert wst.WebSearchTool(db=None)._run("anything") == "ddg"
