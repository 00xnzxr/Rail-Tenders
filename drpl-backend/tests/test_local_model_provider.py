"""The self-hosted model: reachable, attributable, and fenced off.

A local model earns its place by being cheaper than a hosted one, which means
the ways it can go wrong are all about money and silence rather than about
crashes:

* falling FORWARD off it spends money the operator thinks they are saving,
* falling BACK to it answers a Claude-tier request with a 7B model,
* a missing base URL sends the call to api.openai.com under a model id that
  provider does not have,
* a missing pricing entry reports every call at Sonnet's $3/$15,
* and the startup model refresh rewriting its row stops the endpoint being
  called at all, with nothing failing.

Each of those is a test here. The point of the probe agent is that it sits on
no production path; the point of these tests is that it stays that way.
"""

import pytest

from app.services.langchain import provider_config as pc
from app.services.langchain.model_factory import create_chat_model


class TestProviderDetection:
    def test_the_served_model_name_resolves_to_the_local_provider(self):
        # What the endpoint actually serves. Not the Hugging Face repo id:
        # measured 2026-09-22, this vLLM worker 500s on "Qwen/Qwen2.5-7B-Instruct".
        assert pc.detect_provider("qwen2.5-7b") == "runpod"

    def test_a_bare_repo_id_also_resolves_locally(self):
        assert pc.detect_provider("Qwen/Qwen2.5-7B-Instruct") == "runpod"

    @pytest.mark.parametrize("model,provider", [
        ("claude-haiku-4-5", "anthropic"),
        ("gpt-5.6-luna", "openai"),
        ("gemini-3.7-flash", "google"),
    ])
    def test_the_hosted_providers_are_unaffected(self, model, provider):
        assert pc.detect_provider(model) == provider


class TestCredentials:
    def test_the_chat_key_is_preferred_over_the_ocr_key(self, monkeypatch):
        """They are different endpoints and, measured, different keys: the OCR
        key returns 403 on the chat endpoint."""
        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "chat-key", raising=False)
        monkeypatch.setattr(pc.settings, "runpod_api_key", "ocr-key", raising=False)
        assert pc.get_api_key(None, "runpod") == "chat-key"

    def test_the_ocr_key_is_the_fallback_when_no_chat_key_is_set(self, monkeypatch):
        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "", raising=False)
        monkeypatch.setattr(pc.settings, "runpod_api_key", "ocr-key", raising=False)
        assert pc.get_api_key(None, "runpod") == "ocr-key"


class TestBaseUrl:
    def test_no_endpoint_means_no_url_rather_than_a_url_that_404s(self, monkeypatch):
        monkeypatch.setattr(pc.settings, "runpod_chat_endpoint_id", "", raising=False)
        assert pc.get_base_url(None, "runpod") is None

    def test_the_endpoint_id_is_the_whole_address(self, monkeypatch):
        monkeypatch.setattr(pc.settings, "runpod_chat_endpoint_id", "abc123", raising=False)
        assert pc.get_base_url(None, "runpod") == (
            "https://api.runpod.ai/v2/abc123/openai/v1"
        )

    def test_the_hosted_providers_have_none(self, monkeypatch):
        monkeypatch.setattr(pc.settings, "runpod_chat_endpoint_id", "abc123", raising=False)
        for provider in ("anthropic", "openai", "google"):
            assert pc.get_base_url(None, provider) is None

    def test_the_endpoint_ships_as_a_default_so_only_the_key_needs_setting(self):
        """An endpoint id is an identifier, not a credential: it does nothing
        without the key, and RUNPOD_OCR_ENDPOINT_ID is already committed the
        same way. Shipping it leaves production exactly one field to fill in,
        which is the difference between a setting someone forgets and a
        setting someone forgets three of."""
        from app.core.config import Settings

        assert Settings().runpod_chat_endpoint_id == "9d05slartfxnql"
        assert Settings().runpod_chat_model == "qwen2.5-7b"

    def test_the_admin_setting_still_overrides_the_shipped_default(self, monkeypatch):
        """What a replaced or redeployed endpoint needs."""
        monkeypatch.setattr(pc.settings, "runpod_chat_endpoint_id", "shipped", raising=False)

        import app.services.settings_service as ss
        original = ss.get_setting_value
        try:
            ss.get_setting_value = lambda db, key, default=None: (
                "from-admin" if key == "runpod_chat_endpoint_id" else default
            )
            assert pc.get_base_url(object(), "runpod") == (
                "https://api.runpod.ai/v2/from-admin/openai/v1"
            )
        finally:
            ss.get_setting_value = original


class TestFactory:
    def test_a_missing_base_url_raises_rather_than_reaching_openai(self):
        """The failure that would otherwise be a wrong bill, not an error:
        ChatOpenAI with no base_url talks to api.openai.com, which does not
        serve this model id."""
        with pytest.raises(ValueError, match="runpod_chat_endpoint_id"):
            create_chat_model(
                provider="runpod", model="qwen2.5-7b", api_key="k",
                temperature=0.0, max_tokens=256, base_url=None,
            )

    def test_it_builds_a_client_pointed_at_the_endpoint(self):
        llm = create_chat_model(
            provider="runpod", model="qwen2.5-7b", api_key="k",
            temperature=0.0, max_tokens=256,
            base_url="https://api.runpod.ai/v2/abc123/openai/v1",
        )
        assert llm.model_name == "qwen2.5-7b"
        assert str(llm.openai_api_base).rstrip("/").endswith("/v2/abc123/openai/v1")

    def test_an_unknown_provider_is_still_rejected(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            create_chat_model(provider="nonsense", model="x", api_key="k")


class TestFailoverIsolation:
    """Neither direction. Both are silent, and both are wrong."""

    def test_a_local_model_never_falls_forward_to_a_paid_provider(self, monkeypatch):
        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "chat-key", raising=False)
        monkeypatch.setattr(pc.settings, "anthropic_api_key", "ak", raising=False)
        monkeypatch.setattr(pc.settings, "openai_api_key", "ok", raising=False)
        monkeypatch.setattr(pc.settings, "google_api_key", "gk", raising=False)
        monkeypatch.setattr(pc.settings, "failover_enabled", True, raising=False)
        chain = pc.build_fallback_chain(None, "qwen2.5-7b")
        assert [c[0] for c in chain] == ["runpod"]

    def test_nothing_falls_back_onto_the_local_model(self, monkeypatch):
        """Even if someone lists it in failover_providers."""
        monkeypatch.setattr(pc.settings, "anthropic_api_key", "ak", raising=False)
        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "chat-key", raising=False)
        monkeypatch.setattr(pc.settings, "openai_api_key", "", raising=False)
        monkeypatch.setattr(pc.settings, "google_api_key", "", raising=False)
        monkeypatch.setattr(pc.settings, "failover_enabled", True, raising=False)
        monkeypatch.setattr(pc.settings, "failover_providers", "runpod,openai", raising=False)
        chain = pc.build_fallback_chain(None, "claude-haiku-4-5")
        assert "runpod" not in [c[0] for c in chain]

    def test_a_pinned_local_provider_is_a_chain_of_one(self, monkeypatch):
        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "chat-key", raising=False)
        chain = pc.build_fallback_chain(None, "qwen2.5-7b", "runpod")
        assert len(chain) == 1 and chain[0][1] == "qwen2.5-7b"

    def test_the_platform_kill_switch_cannot_force_the_local_model(self):
        """`force_provider_override` routes EVERY agent through one provider.
        A 7B model must never be reachable that way."""
        from app.services.ai_service import _apply_force_provider_override

        class FakeSetting:
            pass

        cfg = {"provider": "anthropic", "model": "claude-haiku-4-5"}
        import app.services.settings_service as ss
        original = ss.get_effective_setting
        try:
            ss.get_effective_setting = lambda db, key, default=None: (
                "runpod" if key == "force_provider_override" else default
            )
            _apply_force_provider_override(FakeSetting(), cfg)
        finally:
            ss.get_effective_setting = original
        assert cfg["provider"] == "anthropic"


class TestCostReporting:
    def test_a_self_hosted_model_is_not_priced_per_token(self):
        assert pc.PRICING["qwen2.5-7b"] == {"input": 0.0, "output": 0.0}

    def test_the_dashboard_charges_zero_rather_than_the_unknown_model_default(self):
        """Without the pricing entry this would report DEFAULT_PRICING —
        Sonnet's $3/$15 — for a model running on our own GPU."""
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        assert pc.estimate_cost("qwen2.5-7b", usage) == 0.0
        assert pc.estimate_cost("some-model-nobody-added", usage) > 0

    def test_it_has_a_declared_output_ceiling(self):
        from app.services.langchain.model_limits import (
            MODEL_MAX_OUTPUT_TOKENS, clamp_max_tokens,
        )
        assert MODEL_MAX_OUTPUT_TOKENS["qwen2.5-7b"] == 8_192
        # Asking vLLM for more than its max_model_len is a 400, not a clamp.
        assert clamp_max_tokens("qwen2.5-7b", 128_000) == 8_192


class TestTheProbeAgentStaysOffEveryProductionPath:
    AGENT_KEY = "local_model_probe"

    def _entry(self):
        from app.services.agent_builder_service import SYSTEM_AGENTS
        return next(a for a in SYSTEM_AGENTS if a["agent_key"] == self.AGENT_KEY)

    def test_it_is_seeded_pinned_to_the_local_provider(self):
        entry = self._entry()
        assert entry["provider"] == "runpod"
        assert entry["model"] == "qwen2.5-7b"
        # Deterministic, and small: a probe is not a place to spend tokens.
        assert entry["temperature"] == 0.0
        assert entry["max_tokens"] <= 1024

    def test_it_has_no_tools(self):
        """No tools means no database, no documents, no platform state."""
        assert not self._entry().get("tools")

    def test_nothing_delegates_to_it(self):
        """The Master's roster is every enabled `custom_agents` row minus the
        excluded keys, so the probe is unreachable from chat only because it
        is excluded there -- absence from the canonical registry alone never
        did it (the old assertion here passed while call_local_model_probe
        sat on the Master's belt)."""
        from app.services.langchain.canonical_registry import is_registered
        from app.services.langchain.graphs.orchestrator_tools import (
            _PIPELINE_ONLY_AGENT_KEYS,
        )
        assert not is_registered(self.AGENT_KEY)
        assert self.AGENT_KEY in _PIPELINE_ONLY_AGENT_KEYS

    def test_the_startup_refresh_leaves_its_model_alone(self):
        """The regression that would silently stop the local endpoint being
        called: `target_model` falls back to Anthropic's tier map for an
        unknown provider, so without a runpod entry this row would be
        rewritten to claude-haiku-4-5 on every boot and nothing would fail."""
        from app.services.seed_agent_models import (
            PREFERRED_AGENT_MODELS, target_model,
        )
        assert self.AGENT_KEY not in PREFERRED_AGENT_MODELS
        assert target_model(self.AGENT_KEY, "runpod") == "qwen2.5-7b"

    def test_no_platform_agent_is_rostered_onto_the_local_provider(self):
        """The roster is the only thing that moves real work between models."""
        from app.services.seed_agent_models import PREFERRED_AGENT_MODELS
        assert all(p != "runpod" for p, _ in PREFERRED_AGENT_MODELS.values())


class TestCallAiRunpodBranch:
    @pytest.mark.asyncio
    async def test_it_names_the_missing_setting_instead_of_failing_obscurely(
        self, monkeypatch
    ):
        from app.services import ai_service

        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "k", raising=False)
        monkeypatch.setattr(pc.settings, "runpod_chat_endpoint_id", "", raising=False)
        with pytest.raises(ValueError, match="runpod_chat_endpoint_id"):
            await ai_service._call_runpod("sys", "user", None, None, {"model": "qwen2.5-7b"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status,expect", [
        # Measured in both directions on 2026-09-22: RunPod scopes keys per
        # endpoint on this account, so the runpod_api_key fallback lands on a
        # 403. Blaming the model name for that sends someone to the wrong
        # setting, which is worse than saying nothing.
        (403, "runpod_chat_api_key"),
        (401, "runpod_chat_api_key"),
        # The dashboard shows "Qwen/Qwen2.5-7B-Instruct"; the endpoint answers
        # to "qwen2.5-7b" and 500s on the other.
        (500, "runpod_chat_model"),
        (503, "runpod_chat_model"),
    ])
    async def test_it_names_the_setting_that_matches_the_refusal(
        self, monkeypatch, status, expect
    ):
        from app.services import ai_service

        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "k", raising=False)
        monkeypatch.setattr(pc.settings, "runpod_chat_endpoint_id", "ep1", raising=False)

        import httpx

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                request = httpx.Request("POST", url)
                response = httpx.Response(status, request=request)
                raise httpx.HTTPStatusError(
                    f"HTTP {status}", request=request, response=response
                )

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        with pytest.raises(Exception) as ei:
            await ai_service._call_runpod(
                "sys", "user", None, None, {"model": "qwen2.5-7b"}
            )
        message = str(ei.value)
        assert expect in message, message
        # And never the other one, so the message points at one thing.
        other = "runpod_chat_model" if expect == "runpod_chat_api_key" else "runpod_chat_api_key"
        assert other not in message, message
        # The key itself is never in an error message.
        assert "rpa_" not in message

    @pytest.mark.asyncio
    async def test_a_credential_error_names_the_endpoint_not_the_key(self, monkeypatch):
        from app.services import ai_service

        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "secret-key", raising=False)
        monkeypatch.setattr(pc.settings, "runpod_chat_endpoint_id", "ep-xyz", raising=False)

        import httpx

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                request = httpx.Request("POST", url)
                raise httpx.HTTPStatusError(
                    "HTTP 403", request=request,
                    response=httpx.Response(403, request=request),
                )

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        with pytest.raises(Exception) as ei:
            await ai_service._call_runpod(
                "sys", "user", None, None, {"model": "qwen2.5-7b"}
            )
        assert "ep-xyz" in str(ei.value)
        assert "secret-key" not in str(ei.value)

    @pytest.mark.asyncio
    async def test_it_sends_the_agents_own_model_never_an_equivalent(self, monkeypatch):
        """There is no hosted equivalent of a model you host yourself, and
        substituting one moves both the work and the bill."""
        from app.services import ai_service

        monkeypatch.setattr(pc.settings, "runpod_chat_api_key", "k", raising=False)
        monkeypatch.setattr(pc.settings, "runpod_chat_endpoint_id", "ep1", raising=False)
        sent = {}

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            @staticmethod
            def json():
                return {
                    "choices": [{"message": {"content": "  hello  "}}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                }

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                sent["url"] = url
                sent["body"] = json
                return FakeResponse()

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        out = await ai_service._call_runpod(
            "sys", "user", None, None,
            {"model": "qwen2.5-7b", "temperature": 0.0, "max_tokens": 256},
        )
        assert out == "hello"
        assert sent["url"] == "https://api.runpod.ai/v2/ep1/openai/v1/chat/completions"
        assert sent["body"]["model"] == "qwen2.5-7b"
        assert sent["body"]["max_tokens"] == 256
